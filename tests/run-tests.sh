#!/bin/bash

# Unit tests for the Databricks dashboard SQL validator.
#
# Against the docker image (as the build pipeline runs them):
#   docker run --rm \
#     --volume "$PWD/tests:/tests:ro" --volume "$PWD/examples:/examples:ro" \
#     --entrypoint /tests/run-tests.sh <image>
#
# Against a checkout, with jq and sqlfluff on the PATH:
#   ./tests/run-tests.sh
#
# The tests that use examples/cleaned/pipeline_runs.lvdash.json are skipped when that
# directory is not available. Set DASHBOARD_SQL_REQUIRE_ALL=1 to turn those skips into
# failures, which is what the build pipeline does so a forgotten mount fails the build.
# Only examples/cleaned is expected to pass; examples/raw holds the unfixed originals.
set -u

here="$(cd "$(dirname "$0")" && pwd)"

# Where the validator and the configs the tests pin live. In the image everything is
# installed in /bin; otherwise it is the checkout this test script sits in. This is local
# to the harness — the validator finds its own siblings from its own path.
if [ -x /bin/validate-dashboard-sql ]; then
    bin_dir=/bin
else
    bin_dir="$(cd "${here}/.." && pwd)"
fi

fixtures="${here}/fixtures"
examples="$(cd "${here}/../examples" 2> /dev/null && pwd)"

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

passed=0
failed=0
skipped=0
out=""
rc=0

# The image installs the entrypoint without its .sh suffix.
if [ -x "${bin_dir}/validate-dashboard-sql" ]; then
    validate="${bin_dir}/validate-dashboard-sql"
elif [ -x "${bin_dir}/validate-dashboard-sql.sh" ]; then
    validate="${bin_dir}/validate-dashboard-sql.sh"
else
    printf 'validate-dashboard-sql not found in %s\n' "${bin_dir}" >&2
    exit 2
fi

start() {
    printf '%s\n' "- $1"
}

pass() {
    passed=$((passed + 1))
}

fail() {
    failed=$((failed + 1))
    printf '  FAILED: %s\n' "$1"
    printf '  --- output ---\n%s\n  --------------\n' "${out}"
}

skip() {
    # The build pipeline runs against an image that has everything mounted, so a skip
    # there means coverage was silently lost and should fail the build instead.
    if [ "${DASHBOARD_SQL_REQUIRE_ALL:-0}" = "1" ]; then
        failed=$((failed + 1))
        printf '  FAILED: %s (required when DASHBOARD_SQL_REQUIRE_ALL=1)\n' "$1"
        return
    fi
    skipped=$((skipped + 1))
    printf '  SKIPPED: %s\n' "$1"
}

# Run the validator, capturing combined output in ${out} and the exit code in ${rc}.
#
# Two defaults are prepended to every call. Nothing here should pick up a .sqlfluff from
# the directory the tests happen to run in, otherwise the expected violation counts depend
# on the caller's working directory, so the bundled config is pinned; and --verbose is what
# prints the snippet inventory that the counting assertions read.
#
# Both are only defaults: the validator lets a later flag win, so a test that passes its
# own --config or wants the quiet output just says so in its own arguments.
run_validator() {
    out="$("${validate}" --config "${bin_dir}/sqlfluff-defaults.cfg" --verbose "$@" 2>&1)"
    rc=$?
}

# Same, but without the --verbose default, for the tests that cover the quiet output.
run_validator_quiet() {
    out="$("${validate}" --config "${bin_dir}/sqlfluff-defaults.cfg" "$@" 2>&1)"
    rc=$?
}

# Run from inside ${1}, pinning nothing: these are the tests that cover how the validator
# picks a config out of the repository it is checking, which is a question about the
# working directory.
run_validator_in() {
    local dir="$1"
    shift
    out="$(cd "${dir}" && "${validate}" --verbose "$@" 2>&1)"
    rc=$?
}

assert_status() {
    if [ "${rc}" -eq "$1" ]; then
        return 0
    fi
    fail "expected exit status $1, got ${rc}"
    return 1
}

# Number of snippets the extractor reported, optionally filtered by kind.
snippet_count() {
    printf '%s\n' "${out}" | grep -c "^  Found #[0-9]* ${1:-}"
}

assert_snippets() {
    local count
    count="$(snippet_count "${2:-}")"
    if [ "${count}" -eq "$1" ]; then
        return 0
    fi
    fail "expected $1 ${2:-}snippet(s), got ${count}"
    return 1
}

assert_contains() {
    case "${out}" in
        *"$1"*) return 0 ;;
    esac
    fail "expected output to contain '$1'"
    return 1
}

assert_not_contains() {
    case "${out}" in
        *"$1"*)
            fail "expected output not to contain '$1'"
            return 1
            ;;
    esac
    return 0
}

# Build a dashboard holding one dataset query of roughly a megabyte, far beyond what fits
# in a command line argument.
generate_long_query_dashboard() {
    local target="${work}/long.lvdash.json"
    {
        # shellcheck disable=SC2016  # the backticks are SQL quoting, not a subshell
        printf '{"datasets":[{"name":"long","queryLines":["SELECT 1 AS `c`\\n"'
        awk 'BEGIN { for (i = 0; i < 20000; i++) printf ",\"-- padding comment line %d\\\\n\"", i }'
        printf ']}]}'
    } > "${target}"
    printf '%s\n' "${target}"
}

# --- extraction -----------------------------------------------------------------------

start "a clean dashboard passes"
run_validator "${fixtures}/clean.lvdash.json"
assert_status 0 && assert_snippets 3 && pass

start "dataset queries and widget expressions are both extracted"
run_validator "${fixtures}/clean.lvdash.json"
assert_status 0 && assert_snippets 1 "query" && assert_snippets 2 "expression" && pass

start "a dataset query stored as a single string is extracted"
run_validator "${fixtures}/query-string.lvdash.json"
assert_status 0 && assert_snippets 1 "query" && pass

start "a dashboard without SQL is a no-op"
run_validator "${fixtures}/no-sql.lvdash.json"
assert_status 0 && assert_snippets 0 && assert_contains "No SQL snippets found" && pass

start "multiple dashboards are all processed"
run_validator "${fixtures}/clean.lvdash.json" "${fixtures}/query-string.lvdash.json"
assert_status 0 && assert_snippets 4 && pass

start "every SQL snippet in the real world example is found"
if [ -z "${examples}" ]; then
    skip "examples directory is not available"
else
    run_validator "${examples}/cleaned/pipeline_runs.lvdash.json"
    assert_status 0 && assert_snippets 28 && assert_snippets 1 "query" && pass
fi

start "the cleaned examples all pass"
if [ -z "${examples}" ]; then
    skip "examples directory is not available"
else
    run_validator "${examples}"/cleaned/*.lvdash.json
    assert_status 0 && pass
fi

# The raw copies keep their violations on purpose: they are what examples/README.md
# documents the cleanup against, so a "fix" that quietly edits them is a regression.
start "the raw examples still fail"
if [ -z "${examples}" ]; then
    skip "examples directory is not available"
else
    for raw in "${examples}"/raw/*.lvdash.json; do
        run_validator "${raw}"
        assert_status 1 || break
    done
    [ "${rc}" -eq 1 ] && pass
fi

# --- violations -----------------------------------------------------------------------

start "a dataset query that does not parse fails"
run_validator "${fixtures}/bad-query.lvdash.json"
assert_status 1 && assert_contains "PRS" && pass

start "a widget expression that does not parse fails"
run_validator "${fixtures}/bad-expression.lvdash.json"
assert_status 1 && assert_contains "PRS" && pass

start "violations are reported against the json path, not a scratch file"
run_validator "${fixtures}/bad-query.lvdash.json"
assert_status 1 && assert_contains "datasets[broken].queryLines" && assert_not_contains ".sql]" && pass

start "one broken dashboard does not stop the others from being checked"
run_validator "${fixtures}/bad-query.lvdash.json" "${fixtures}/clean.lvdash.json"
assert_status 1 && assert_snippets 4 && pass

start "invalid json fails instead of being silently skipped"
run_validator "${fixtures}/invalid-json.lvdash.json"
assert_status 1 && assert_contains "not valid json" && pass

# --- verbosity ------------------------------------------------------------------------

# pre-commit shows a hook's output when it fails, so a failing run is exactly where the
# snippet inventory is most in the way of the violations someone is trying to read.
start "the snippet inventory is quiet by default"
run_validator_quiet "${fixtures}/bad-query.lvdash.json"
assert_status 1 \
    && assert_not_contains "Found #" \
    && assert_not_contains "Extracting SQL from" \
    && pass

start "violations are still reported when quiet"
run_validator_quiet "${fixtures}/bad-query.lvdash.json"
assert_status 1 \
    && assert_contains "PRS" \
    && assert_contains "datasets[broken].queryLines" \
    && assert_contains "Linting 1 query snippet(s)" \
    && pass

start "--verbose brings the snippet inventory back"
run_validator_quiet --verbose "${fixtures}/clean.lvdash.json"
assert_status 0 && assert_snippets 3 && assert_contains "Extracting SQL from" && pass

start "-v is accepted as well"
run_validator_quiet -v "${fixtures}/clean.lvdash.json"
assert_status 0 && assert_snippets 3 && pass

start "no arguments is a usage error"
run_validator
assert_status 2 && pass

# --- configuration --------------------------------------------------------------------

# Everything is configured through the command line. pre-commit does not forward the
# environment into a `language: docker` hook, so an environment variable would apply to a
# local run and silently not to a containerised one.
start "the environment configures nothing"
DASHBOARD_SQL_DIALECT=nonsense \
    DASHBOARD_SQL_QUERY_MODE=off \
    DASHBOARD_SQL_VERBOSE=1 \
    DASHBOARD_SQL_KEEP_TMP=1 \
    run_validator_quiet "${fixtures}/clean.lvdash.json"
assert_status 0 \
    && assert_not_contains "Found #" \
    && assert_not_contains "Skipping query snippets" \
    && assert_not_contains "Keeping extracted snippets" \
    && pass

start "--dialect configures the dialect"
run_validator --dialect ansi "${fixtures}/query-string.lvdash.json"
assert_status 1 && assert_contains "PRS" && pass

start "--query-mode off skips the queries"
run_validator --query-mode off "${fixtures}/bad-query.lvdash.json"
assert_status 0 && assert_contains "Skipping query snippets" && pass

start "--expression-mode off skips the expressions"
run_validator --expression-mode off "${fixtures}/bad-expression.lvdash.json"
assert_status 0 && assert_contains "Skipping expression snippets" && pass

start "an unsupported mode is a usage error"
run_validator --query-mode fix "${fixtures}/clean.lvdash.json"
assert_status 2 && pass

start "the default config hides the layout rules"
run_validator "${fixtures}/messy-layout.lvdash.json"
assert_status 0 && assert_not_contains "LT02" && pass

start "--config selects the sqlfluff config"
run_validator --config "${fixtures}/strict.sqlfluff" "${fixtures}/messy-layout.lvdash.json"
assert_status 1 && assert_contains "LT02" && pass

start "a non existent config is a usage error"
run_validator --config "${work}/nope.cfg" "${fixtures}/clean.lvdash.json"
assert_status 2 && pass

start "--expression-config selects the expression config"
run_validator --expression-config "${fixtures}/strict.sqlfluff" \
    "${fixtures}/clean.lvdash.json"
assert_status 0 \
    && assert_contains "expression snippet(s) with ${fixtures}/strict.sqlfluff" \
    && assert_contains "query snippet(s) with ${bin_dir}/sqlfluff-defaults.cfg" \
    && pass

start "extra sqlfluff arguments are passed through"
run_validator --sqlfluff-arg --format --sqlfluff-arg json "${fixtures}/clean.lvdash.json"
assert_status 0 && assert_contains '"violations"' && pass

start "--sqlfluff-arg is repeatable"
run_validator --sqlfluff-arg --rules --sqlfluff-arg layout \
    --config "${fixtures}/strict.sqlfluff" "${fixtures}/messy-layout.lvdash.json"
assert_status 1 && assert_contains "LT02" && assert_not_contains "AM04" && pass

# --- picking up the checked repository's own config ------------------------------------

# The snippets are linted from a scratch directory outside the repository, so sqlfluff has
# to be told about the repository's config explicitly. These cover the search order and,
# in particular, a project that configures a templater: left to itself sqlfluff keeps the
# templater from the working directory and loses the nested section that parameterises it,
# then dies with "No param_regex nor param_style was provided to the placeholder
# templater!" before linting anything.
project="${work}/project"
mkdir -p "${project}"
cp "${fixtures}/messy-layout.lvdash.json" "${project}/dash.lvdash.json"

start "a repository .sqlfluff is used as the config"
cp "${fixtures}/strict.sqlfluff" "${project}/.sqlfluff"
run_validator_in "${project}" dash.lvdash.json
rm -f "${project}/.sqlfluff"
assert_status 1 && assert_contains "LT02" && pass

start "a pyproject.toml that configures sqlfluff is used as the config"
cat > "${project}/pyproject.toml" << 'PYPROJECT'
[project]
name = "dashboards"
version = "0.1.0"

[tool.sqlfluff.core]
dialect = "databricks"
PYPROJECT
run_validator_in "${project}" dash.lvdash.json
assert_status 1 \
    && assert_contains "query snippet(s) with ${project}/pyproject.toml" \
    && assert_contains "LT02" \
    && pass

start "a pyproject.toml that says nothing about sqlfluff is left alone"
cat > "${project}/pyproject.toml" << 'PYPROJECT'
[project]
name = "dashboards"
version = "0.1.0"

[tool.ruff]
line-length = 100
PYPROJECT
run_validator_in "${project}" dash.lvdash.json
assert_status 0 \
    && assert_contains "query snippet(s) with ${bin_dir}/sqlfluff-defaults.cfg" \
    && assert_not_contains "LT02" \
    && pass

# Regression test for the placeholder templater crash.
start "a project that configures a templater does not break the run"
cat > "${project}/pyproject.toml" << 'PYPROJECT'
[project]
name = "dashboards"
version = "0.1.0"

[tool.sqlfluff.core]
dialect = "databricks"
templater = "placeholder"

[tool.sqlfluff.templater.placeholder]
param_regex = 'IDENTIFIER\s*\([^)]*\)|\$\{[^}]*\}|\{\{[^}]*\}\}'
param_placeholder = "sandbox"
PYPROJECT
run_validator_in "${project}" dash.lvdash.json
assert_status 1 \
    && assert_not_contains "param_regex nor param_style" \
    && assert_not_contains "Traceback" \
    && assert_contains "LT02" \
    && pass

# The expressions are always checked with the bundled syntax only config, so the project's
# templater must not reach them either. messy-layout has no expressions in it, so this one
# needs a dashboard that does.
start "a project templater does not reach the expression run"
cp "${fixtures}/clean.lvdash.json" "${project}/with-expressions.lvdash.json"
run_validator_in "${project}" --query-mode off with-expressions.lvdash.json
assert_status 0 \
    && assert_contains "expression snippet(s) with ${bin_dir}/sqlfluff-expressions.cfg" \
    && assert_not_contains "param_regex nor param_style" \
    && pass

start "--config still wins over the repository's own config"
run_validator_in "${project}" --config "${bin_dir}/sqlfluff-defaults.cfg" dash.lvdash.json
rm -f "${project}/pyproject.toml"
assert_status 0 \
    && assert_contains "query snippet(s) with ${bin_dir}/sqlfluff-defaults.cfg" \
    && pass

start "--keep-tmp leaves the extracted snippets behind"
run_validator --keep-tmp "${fixtures}/clean.lvdash.json"
if assert_status 0 && assert_contains "Keeping extracted snippets in"; then
    # The point of the flag is that the validator does not clean up, so the test has to.
    kept="$(printf '%s\n' "${out}" | sed -n 's/^Keeping extracted snippets in //p')"
    if [ -n "${kept}" ] && [ -n "$(find "${kept}" -name '*.sql' 2> /dev/null)" ]; then
        pass
    else
        fail "expected the extracted snippets to still be in '${kept}'"
    fi
    [ -n "${kept}" ] && rm -rf "${kept}"
fi

start "-- ends the options so a filename may start with a dash"
run_validator -- "${fixtures}/clean.lvdash.json"
assert_status 0 && assert_snippets 3 && pass

start "an unknown option is a usage error"
run_validator --nope "${fixtures}/clean.lvdash.json"
assert_status 2 && assert_contains "unknown option" && pass

start "--help succeeds"
run_validator --help
assert_status 0 && assert_contains "Usage: validate-dashboard-sql" && pass

# --- size limits ----------------------------------------------------------------------

# Regression test: passing the snippet as a command line argument would fail with
# 'Argument list too long' well before a megabyte.
start "a query too large for a command line argument is checked"
run_validator "$(generate_long_query_dashboard)"
assert_status 0 && assert_snippets 1 && assert_not_contains "too long" && pass

printf '\n%s passed, %s failed, %s skipped\n' "${passed}" "${failed}" "${skipped}"
[ "${failed}" -eq 0 ]
