"""Behavioural tests for the Databricks dashboard SQL validator.

These cover extraction of each SQL shape, the exit statuses, the mapping of violations
back to json paths, the configuration switches, and a regression test for a query too
large to pass as a command line argument.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from databricks_dashboard_validator.cli import DEFAULT_EXPRESSION_CONFIG, DEFAULT_QUERY_CONFIG
from tests.conftest import FIXTURES

# --- extraction -------------------------------------------------------------------------


def test_a_clean_dashboard_passes(run):
    result = run(str(FIXTURES / "clean.lvdash.json"))
    assert result.returncode == 0
    assert result.snippets() == 3


def test_dataset_queries_and_widget_expressions_are_both_extracted(run):
    result = run(str(FIXTURES / "clean.lvdash.json"))
    assert result.returncode == 0
    assert result.snippets("query") == 1
    assert result.snippets("expression") == 2


def test_a_dataset_query_stored_as_a_single_string_is_extracted(run):
    result = run(str(FIXTURES / "query-string.lvdash.json"))
    assert result.returncode == 0
    assert result.snippets("query") == 1


def test_a_dashboard_without_sql_is_a_no_op(run):
    result = run(str(FIXTURES / "no-sql.lvdash.json"))
    assert result.returncode == 0
    assert result.snippets() == 0
    assert "No SQL snippets found" in result.output


def test_multiple_dashboards_are_all_processed(run):
    result = run(
        str(FIXTURES / "clean.lvdash.json"),
        str(FIXTURES / "query-string.lvdash.json"),
    )
    assert result.returncode == 0
    assert result.snippets() == 4


def test_every_sql_snippet_in_the_real_world_example_is_found(run, examples):
    result = run(str(examples / "cleaned" / "pipeline_runs.lvdash.json"))
    assert result.returncode == 0
    assert result.snippets() == 28
    assert result.snippets("query") == 1


def test_the_cleaned_examples_all_pass(run, examples):
    result = run(*[str(path) for path in sorted((examples / "cleaned").glob("*.lvdash.json"))])
    assert result.returncode == 0


# The raw copies keep their violations on purpose: they are what examples/README.md
# documents the cleanup against, so a "fix" that quietly edits them is a regression.
def test_the_raw_examples_still_fail(run, examples):
    for raw in sorted((examples / "raw").glob("*.lvdash.json")):
        assert run(str(raw)).returncode == 1, raw.name


# --- violations -------------------------------------------------------------------------


def test_a_dataset_query_that_does_not_parse_fails(run):
    result = run(str(FIXTURES / "bad-query.lvdash.json"))
    assert result.returncode == 1
    assert "PRS" in result.output


def test_a_widget_expression_that_does_not_parse_fails(run):
    result = run(str(FIXTURES / "bad-expression.lvdash.json"))
    assert result.returncode == 1
    assert "PRS" in result.output


def test_violations_are_reported_against_the_json_path_not_a_scratch_file(run):
    result = run(str(FIXTURES / "bad-query.lvdash.json"))
    assert result.returncode == 1
    assert "datasets[broken].queryLines" in result.output
    assert ".sql]" not in result.output


def test_one_broken_dashboard_does_not_stop_the_others_from_being_checked(run):
    result = run(
        str(FIXTURES / "bad-query.lvdash.json"),
        str(FIXTURES / "clean.lvdash.json"),
    )
    assert result.returncode == 1
    assert result.snippets() == 4


def test_invalid_json_fails_instead_of_being_silently_skipped(run):
    result = run(str(FIXTURES / "invalid-json.lvdash.json"))
    assert result.returncode == 1
    assert "not valid json" in result.output


def test_a_missing_file_is_reported_rather_than_ignored(run, tmp_path):
    result = run(str(tmp_path / "absent.lvdash.json"))
    assert result.returncode == 1
    assert "not valid json" in result.output


# --- verbosity --------------------------------------------------------------------------


# pre-commit shows a hook's output when it fails, so a failing run is exactly where the
# snippet inventory is most in the way of the violations someone is trying to read.
def test_the_snippet_inventory_is_quiet_by_default(run_quiet):
    result = run_quiet(str(FIXTURES / "bad-query.lvdash.json"))
    assert result.returncode == 1
    assert "Found #" not in result.output
    assert "Extracting SQL from" not in result.output


def test_violations_are_still_reported_when_quiet(run_quiet):
    result = run_quiet(str(FIXTURES / "bad-query.lvdash.json"))
    assert result.returncode == 1
    assert "PRS" in result.output
    assert "datasets[broken].queryLines" in result.output
    assert "Linting 1 query snippet(s)" in result.output


def test_verbose_brings_the_snippet_inventory_back(run_quiet):
    result = run_quiet("--verbose", str(FIXTURES / "clean.lvdash.json"))
    assert result.returncode == 0
    assert result.snippets() == 3
    assert "Extracting SQL from" in result.output


def test_short_v_is_accepted_as_well(run_quiet):
    result = run_quiet("-v", str(FIXTURES / "clean.lvdash.json"))
    assert result.returncode == 0
    assert result.snippets() == 3


def test_no_arguments_is_a_usage_error(run):
    assert run().returncode == 2


# --- configuration ----------------------------------------------------------------------


# Everything is configured through the command line. pre-commit does not forward the
# environment into a `language: docker` hook, so an environment variable would apply to a
# local run and silently not to a containerised one.
def test_the_environment_configures_nothing(run_quiet):
    result = run_quiet(
        str(FIXTURES / "clean.lvdash.json"),
        env={
            "DASHBOARD_SQL_DIALECT": "nonsense",
            "DASHBOARD_SQL_QUERY_MODE": "off",
            "DASHBOARD_SQL_VERBOSE": "1",
            "DASHBOARD_SQL_KEEP_TMP": "1",
        },
    )
    assert result.returncode == 0
    assert "Found #" not in result.output
    assert "Skipping query snippets" not in result.output
    assert "Keeping extracted snippets" not in result.output


def test_dialect_configures_the_dialect(run):
    result = run("--dialect", "ansi", str(FIXTURES / "query-string.lvdash.json"))
    assert result.returncode == 1
    assert "PRS" in result.output


def test_query_mode_off_skips_the_queries(run):
    result = run("--query-mode", "off", str(FIXTURES / "bad-query.lvdash.json"))
    assert result.returncode == 0
    assert "Skipping query snippets" in result.output


def test_expression_mode_off_skips_the_expressions(run):
    result = run("--expression-mode", "off", str(FIXTURES / "bad-expression.lvdash.json"))
    assert result.returncode == 0
    assert "Skipping expression snippets" in result.output


def test_an_unsupported_mode_is_a_usage_error(run):
    result = run("--query-mode", "fix", str(FIXTURES / "clean.lvdash.json"))
    assert result.returncode == 2


def test_an_option_missing_its_value_is_a_usage_error(run):
    result = run("--dialect")
    assert result.returncode == 2
    assert "needs a value" in result.output


def test_the_default_config_hides_the_layout_rules(run):
    result = run(str(FIXTURES / "messy-layout.lvdash.json"))
    assert result.returncode == 0
    assert "LT02" not in result.output


def test_config_selects_the_sqlfluff_config(run):
    result = run(
        "--config",
        str(FIXTURES / "strict.sqlfluff"),
        str(FIXTURES / "messy-layout.lvdash.json"),
    )
    assert result.returncode == 1
    assert "LT02" in result.output


def test_a_non_existent_config_is_a_usage_error(run, tmp_path):
    result = run("--config", str(tmp_path / "nope.cfg"), str(FIXTURES / "clean.lvdash.json"))
    assert result.returncode == 2


def test_expression_config_selects_the_expression_config(run):
    result = run(
        "--expression-config",
        str(FIXTURES / "strict.sqlfluff"),
        str(FIXTURES / "clean.lvdash.json"),
    )
    assert result.returncode == 0
    assert f"expression snippet(s) with {FIXTURES / 'strict.sqlfluff'}" in result.output
    assert f"query snippet(s) with {DEFAULT_QUERY_CONFIG}" in result.output


def test_extra_sqlfluff_arguments_are_passed_through(run):
    result = run(
        "--sqlfluff-arg",
        "--format",
        "--sqlfluff-arg",
        "json",
        str(FIXTURES / "clean.lvdash.json"),
    )
    assert result.returncode == 0
    assert '"violations"' in result.output


def test_sqlfluff_arg_is_repeatable(run):
    result = run(
        "--sqlfluff-arg",
        "--rules",
        "--sqlfluff-arg",
        "layout",
        "--config",
        str(FIXTURES / "strict.sqlfluff"),
        str(FIXTURES / "messy-layout.lvdash.json"),
    )
    assert result.returncode == 1
    assert "LT02" in result.output
    assert "AM04" not in result.output


# --- picking up the checked repository's own config ---------------------------------------

# The snippets are linted from a scratch directory outside the repository, so sqlfluff has
# to be told about the repository's config explicitly. These cover the search order and, in
# particular, a project that configures a templater: left to itself sqlfluff keeps the
# templater from the working directory and loses the nested section that parameterises it,
# then dies with "No param_regex nor param_style was provided to the placeholder templater!"
# before linting anything.


@pytest.fixture
def project(tmp_path: Path) -> Path:
    directory = tmp_path / "project"
    directory.mkdir()
    shutil.copy(FIXTURES / "messy-layout.lvdash.json", directory / "dash.lvdash.json")
    return directory


def test_a_repository_sqlfluff_is_used_as_the_config(run_in, project):
    shutil.copy(FIXTURES / "strict.sqlfluff", project / ".sqlfluff")
    result = run_in(project, "dash.lvdash.json")
    assert result.returncode == 1
    assert "LT02" in result.output


def test_a_pyproject_that_configures_sqlfluff_is_used_as_the_config(run_in, project):
    (project / "pyproject.toml").write_text(
        '[project]\nname = "dashboards"\nversion = "0.1.0"\n\n'
        '[tool.sqlfluff.core]\ndialect = "databricks"\n'
    )
    result = run_in(project, "dash.lvdash.json")
    assert result.returncode == 1
    assert f"query snippet(s) with {project / 'pyproject.toml'}" in result.output
    assert "LT02" in result.output


def test_a_pyproject_that_says_nothing_about_sqlfluff_is_left_alone(run_in, project):
    (project / "pyproject.toml").write_text(
        '[project]\nname = "dashboards"\nversion = "0.1.0"\n\n[tool.ruff]\nline-length = 100\n'
    )
    result = run_in(project, "dash.lvdash.json")
    assert result.returncode == 0
    assert f"query snippet(s) with {DEFAULT_QUERY_CONFIG}" in result.output
    assert "LT02" not in result.output


# Regression test for the placeholder templater crash.
def test_a_project_that_configures_a_templater_does_not_break_the_run(run_in, project):
    (project / "pyproject.toml").write_text(
        '[project]\nname = "dashboards"\nversion = "0.1.0"\n\n'
        '[tool.sqlfluff.core]\ndialect = "databricks"\ntemplater = "placeholder"\n\n'
        "[tool.sqlfluff.templater.placeholder]\n"
        "param_regex = 'IDENTIFIER\\s*\\([^)]*\\)|\\$\\{[^}]*\\}|\\{\\{[^}]*\\}\\}'\n"
        'param_placeholder = "sandbox"\n'
    )
    result = run_in(project, "dash.lvdash.json")
    assert result.returncode == 1
    assert "param_regex nor param_style" not in result.output
    assert "Traceback" not in result.output
    assert "LT02" in result.output


# The expressions are always checked with the bundled syntax-only config, so the project's
# templater must not reach them either. messy-layout has no expressions in it, so this one
# needs a dashboard that does.
def test_a_project_templater_does_not_reach_the_expression_run(run_in, project):
    (project / "pyproject.toml").write_text(
        '[project]\nname = "dashboards"\nversion = "0.1.0"\n\n'
        '[tool.sqlfluff.core]\ndialect = "databricks"\ntemplater = "placeholder"\n\n'
        "[tool.sqlfluff.templater.placeholder]\n"
        "param_regex = 'IDENTIFIER\\s*\\([^)]*\\)'\nparam_placeholder = \"sandbox\"\n"
    )
    shutil.copy(FIXTURES / "clean.lvdash.json", project / "with-expressions.lvdash.json")
    result = run_in(project, "--query-mode", "off", "with-expressions.lvdash.json")
    assert result.returncode == 0
    assert f"expression snippet(s) with {DEFAULT_EXPRESSION_CONFIG}" in result.output
    assert "param_regex nor param_style" not in result.output


def test_config_still_wins_over_the_repositorys_own_config(run_in, project):
    shutil.copy(FIXTURES / "strict.sqlfluff", project / ".sqlfluff")
    result = run_in(project, "--config", str(DEFAULT_QUERY_CONFIG), "dash.lvdash.json")
    assert result.returncode == 0
    assert f"query snippet(s) with {DEFAULT_QUERY_CONFIG}" in result.output


# --- odds and ends ------------------------------------------------------------------------


def test_keep_tmp_leaves_the_extracted_snippets_behind(run):
    result = run("--keep-tmp", str(FIXTURES / "clean.lvdash.json"))
    assert result.returncode == 0
    assert "Keeping extracted snippets in" in result.output

    kept = Path(result.output.split("Keeping extracted snippets in ", 1)[1].splitlines()[0])
    try:
        # The point of the flag is that the validator does not clean up, so the test has to.
        assert list(kept.rglob("*.sql"))
    finally:
        shutil.rmtree(kept, ignore_errors=True)


def test_the_scratch_directory_is_removed_by_default(run, tmp_path):
    # mkdtemp honours TMPDIR, so pointing it at an empty directory makes "cleaned up"
    # something the test can see rather than infer from the absence of a message.
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    result = run(str(FIXTURES / "clean.lvdash.json"), env={"TMPDIR": str(scratch)})
    assert result.returncode == 0
    assert "Keeping extracted snippets" not in result.output
    assert list(scratch.iterdir()) == []


def test_double_dash_ends_the_options_so_a_filename_may_start_with_a_dash(run):
    result = run("--", str(FIXTURES / "clean.lvdash.json"))
    assert result.returncode == 0
    assert result.snippets() == 3


def test_an_unknown_option_is_a_usage_error(run):
    result = run("--nope", str(FIXTURES / "clean.lvdash.json"))
    assert result.returncode == 2
    assert "unknown option" in result.output


def test_help_succeeds(run):
    result = run("--help")
    assert result.returncode == 0
    assert "Usage: validate-dashboard-sql" in result.output


# --- size limits --------------------------------------------------------------------------


# Regression test: passing the snippet as a command line argument would fail with
# 'Argument list too long' well before a megabyte.
def test_a_query_too_large_for_a_command_line_argument_is_checked(run, tmp_path):
    lines = ["SELECT 1 AS `c`\n"] + [f"-- padding comment line {i}\n" for i in range(20000)]
    dashboard = tmp_path / "long.lvdash.json"
    dashboard.write_text(json.dumps({"datasets": [{"name": "long", "queryLines": lines}]}))

    result = run(str(dashboard))
    assert result.returncode == 0
    assert result.snippets() == 1
    assert "too long" not in result.output
