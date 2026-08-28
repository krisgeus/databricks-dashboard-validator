"""Extract the inline SQL from Databricks Lakeview dashboards and run sqlfluff over it.

Snippets are written to files in a scratch directory rather than being passed as
arguments, so a query of any size is handled, and sqlfluff is invoked once per snippet
kind rather than once per snippet, which keeps a dashboard with fifty widgets fast.

Everything is configured through the command line. Under pre-commit that is the only
channel that works anyway: it does not forward the environment into a `language: docker`
hook, so a variable set in a shell would apply to a local run and silently not to a
containerised one.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from databricks_dashboard_validator.extract import Snippet, extract_snippets

PROGRAM = "validate-dashboard-sql"

# The bundled configs ship inside the package, so they are found the same way whether the
# validator runs from a checkout, from a wheel in a pre-commit venv, or from the image.
DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_QUERY_CONFIG = DATA_DIR / "sqlfluff-defaults.cfg"
DEFAULT_EXPRESSION_CONFIG = DATA_DIR / "sqlfluff-expressions.cfg"

USAGE = """\
Usage: validate-dashboard-sql [options] <dashboard.lvdash.json>...

Options:
  --dialect DIALECT           sqlfluff dialect (default: databricks)
  --config FILE               sqlfluff config for dataset queries
  --expression-config FILE    sqlfluff config for widget field expressions
  --query-mode lint|off       how to check dataset queries (default: lint)
  --expression-mode lint|off  how to check widget expressions (default: lint)
  --sqlfluff-arg ARG          extra flag passed to sqlfluff lint, repeatable
  --keep-tmp                  leave the extracted .sql files behind for inspection
  -v, --verbose               also report which snippet came from where in the json
  -h, --help                  show this help

There are no environment variable equivalents. Under pre-commit the command line is the
only channel that works, because pre-commit does not forward the environment into a
`language: docker` hook, so anything else would apply to a local run and silently not to a
containerised one. Pass options through the hook's args:

  - id: validate-dashboard-sql
    args: [--dialect, sparksql, --expression-mode, 'off']

Note that pre-commit's own -v does not reach this script: pre-commit captures hook output
and shows it only when the hook fails or when -v is given, but the hook cannot tell which
mode it is in. To get the snippet inventory under pre-commit, put --verbose in the hook's
args.
"""

# Bytes tr keeps when turning a snippet id into a filename; everything else, including
# every byte above ASCII, becomes an underscore.
_FILENAME_SAFE = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")

# A pyproject.toml counts as sqlfluff configuration only when it actually configures
# sqlfluff, hence matching the section header rather than the file's existence.
_SQLFLUFF_SECTION = re.compile(r"^[ \t]*\[tool\.sqlfluff", re.MULTILINE)

# Only the "== [path] FAIL" header lines of sqlfluff's output carry a filename.
_VIOLATION_HEADER = re.compile(r"^== \[([^\]]*)\]")


class UsageError(Exception):
    """A problem with how the validator was invoked. Reported, then exit status 2."""


class Options:
    """The command line, parsed."""

    def __init__(self) -> None:
        self.dialect = "databricks"
        # lint | off. Applies to dataset queries.
        self.query_mode = "lint"
        # lint | off. Applies to widget field expressions.
        self.expression_mode = "lint"
        # Print which file each snippet came from and where in the json it was found. This
        # is debugging output: on a failing run the violations already name their json
        # path, so it only gets in the way of the thing you are trying to read.
        self.verbose = False
        # Leave the extracted .sql files behind instead of removing them on exit.
        self.keep_tmp = False
        # sqlfluff config for dataset queries. None means "work it out", see resolve_config.
        self.config: str | None = None
        # sqlfluff config for widget expressions. None means the bundled syntax-only one.
        self.expression_config: str | None = None
        self.sqlfluff_args: list[str] = []
        self.dashboards: list[str] = []


def parse_args(argv: Sequence[str]) -> Options | None:
    """Parse the command line, or return None when --help asked for the usage text."""
    options = Options()
    index = 0

    def value_for(flag: str) -> str:
        nonlocal index
        if index + 1 >= len(argv):
            raise UsageError(f"{flag} needs a value")
        index += 1
        return argv[index]

    while index < len(argv):
        argument = argv[index]
        if argument == "--dialect":
            options.dialect = value_for(argument)
        elif argument == "--config":
            options.config = value_for(argument)
        elif argument == "--expression-config":
            options.expression_config = value_for(argument)
        elif argument == "--query-mode":
            options.query_mode = value_for(argument)
        elif argument == "--expression-mode":
            options.expression_mode = value_for(argument)
        elif argument == "--sqlfluff-arg":
            options.sqlfluff_args.append(value_for(argument))
        elif argument == "--keep-tmp":
            options.keep_tmp = True
        elif argument in ("-v", "--verbose"):
            options.verbose = True
        elif argument in ("-h", "--help"):
            return None
        elif argument == "--":
            index += 1
            break
        elif argument.startswith("-") and argument != "-":
            raise UsageError(f"unknown option '{argument}' (try --help)")
        else:
            break
        index += 1

    options.dashboards = list(argv[index:])
    return options


def resolve_config(configured: str | None, fallback: Path) -> str:
    """Which sqlfluff config the dataset queries are linted with.

    sqlfluff resolves config relative to the file it is linting, and the snippets live in
    a scratch directory outside the repository, so the config has to be passed explicitly.

    Passing it is not only about picking the right rules. Left to itself sqlfluff still
    reads the working directory's config for core settings but resolves the nested
    sections against the snippet's own path, where there is no config at all. A project
    that sets

        [tool.sqlfluff.core]
        templater = "placeholder"
        [tool.sqlfluff.templater.placeholder]
        param_regex = '...'

    then keeps the templater and loses the parameters, and sqlfluff dies with "No
    param_regex nor param_style was provided to the placeholder templater!". Naming the
    file on the command line keeps the two halves together.

    Order: --config, then the repository's own config, then the bundled default. sqlfluff
    reads its settings from either a .sqlfluff or a [tool.sqlfluff.*] section in
    pyproject.toml, so both count as "the repository's own".
    """
    if configured:
        return configured

    working_directory = os.getcwd()

    dot_sqlfluff = os.path.join(working_directory, ".sqlfluff")
    if os.path.isfile(dot_sqlfluff):
        return dot_sqlfluff

    pyproject = os.path.join(working_directory, "pyproject.toml")
    if os.path.isfile(pyproject) and _configures_sqlfluff(pyproject):
        # Only when it actually configures sqlfluff: nearly every python project has a
        # pyproject.toml, and treating an unrelated one as the config would quietly drop
        # the bundled defaults.
        return pyproject

    return str(fallback)


def _configures_sqlfluff(pyproject: str) -> bool:
    try:
        with open(pyproject, encoding="utf-8", errors="replace") as handle:
            return _SQLFLUFF_SECTION.search(handle.read()) is not None
    except OSError:
        return False


def safe_name(snippet_id: str) -> str:
    """Turn a snippet id into something that survives being used as a filename.

    The tail is kept because that is the part that identifies the widget and the field.
    """
    raw = snippet_id.encode("utf-8", "surrogateescape")
    cleaned = bytes(byte if byte in _FILENAME_SAFE else ord("_") for byte in raw)
    return cleaned[-60:].decode("ascii")


class Validator:
    """One run: extract the snippets of every dashboard given, then lint them by kind."""

    def __init__(self, options: Options, work: Path, stream: TextIO) -> None:
        self.options = options
        self.work = work
        self.stream = stream
        self.status = 0
        # Snippet counter across all dashboards, so scratch filenames stay unique when
        # more than one file is checked in the same run.
        self.extracted = 0
        # Scratch filename -> the json path the snippet came from.
        self.origins: dict[str, str] = {}

    def say(self, message: str) -> None:
        self.stream.write(message)
        self.stream.flush()

    def note(self, message: str) -> None:
        """Progress detail, suppressed unless --verbose."""
        if self.options.verbose:
            self.say(message)

    def run(self) -> int:
        for dashboard in self.options.dashboards:
            self.note(f"Extracting SQL from {dashboard}\n")
            self.extract_file(dashboard)

        if not self.origins:
            self.say("No SQL snippets found.\n")
            return self.status

        self.check_kind(
            "query",
            self.options.query_mode,
            resolve_config(self.options.config, DEFAULT_QUERY_CONFIG),
        )
        # Expressions are fragments, so a repository's own rules would flag every one of
        # them. They are always checked with the bundled syntax-only config.
        self.check_kind(
            "expression",
            self.options.expression_mode,
            self.options.expression_config or str(DEFAULT_EXPRESSION_CONFIG),
        )
        return self.status

    def extract_file(self, dashboard: str) -> None:
        try:
            with open(dashboard, "rb") as handle:
                document: Any = json.loads(handle.read().decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            document = _INVALID

        # jq -e treats a document of null or false as a failure too, and reporting those
        # as unusable rather than as an empty dashboard is the more useful answer anyway.
        if document is _INVALID or document is None or document is False:
            sys.stderr.write(f"{dashboard}: not valid json\n")
            sys.stderr.flush()
            self.status = 1
            return

        for snippet in extract_snippets(document):
            self.write_snippet(dashboard, snippet)

    def write_snippet(self, dashboard: str, snippet: Snippet) -> None:
        self.extracted += 1
        directory = self.work / snippet.kind
        directory.mkdir(parents=True, exist_ok=True)

        name = f"{self.extracted:04d}_{safe_name(snippet.id)}.sql"
        if snippet.kind == "expression":
            # A field expression is not a statement, so wrap it into the smallest query
            # that makes it parsable. Reported positions on line 1 are shifted by the
            # seven characters of "SELECT ".
            body = "SELECT " + snippet.sql.rstrip("\n") + "\n"
        else:
            body = snippet.sql.rstrip("\n") + "\n"
        (directory / name).write_text(body, encoding="utf-8")

        self.origins[name] = f"{dashboard}: {snippet.id}"
        self.note(f"  Found #{snippet.seq} {snippet.kind:<10} {snippet.id}\n")

    def check_kind(self, kind: str, mode: str, config: str) -> None:
        """Lint every snippet of one kind in a single sqlfluff invocation."""
        directory = self.work / kind
        if not directory.is_dir():
            return
        if mode == "off":
            self.say(f"Skipping {kind} snippets (mode=off)\n")
            return
        if mode != "lint":
            raise UsageError(
                f"unsupported mode '{mode}' for {kind} snippets, expected 'lint' or 'off'"
            )
        if not os.access(config, os.R_OK) or not os.path.isfile(config):
            raise UsageError(f"sqlfluff config not found at {config}")

        count = len(list(directory.glob("*.sql")))
        self.say(f"Linting {count} {kind} snippet(s) with {config}\n")

        # --ignore-local-config stops sqlfluff searching its default locations on top of
        # the config named here, so the file reported above is genuinely the only one in
        # play. It does not suppress --config. Without it the working directory's config
        # is merged in half way: the snippets sit outside the repository, so core settings
        # are picked up but the nested sections they depend on are not, which is what
        # breaks a project that configures a templater. It also keeps a stray ~/.sqlfluff
        # from changing CI results.
        command = [
            *sqlfluff_command(),
            "lint",
            "--dialect",
            self.options.dialect,
            "--config",
            config,
            "--ignore-local-config",
            *self.options.sqlfluff_args,
            str(directory),
        ]
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        self.say(self.relabel(completed.stdout))
        if completed.returncode != 0:
            self.status = 1

    def relabel(self, output: str) -> str:
        """Map the scratch filenames in sqlfluff's output back to the json paths.

        A violation is only useful if it names the query someone can go and fix, and the
        file sqlfluff read is a temporary one with a generated name.
        """
        lines = []
        for line in output.splitlines(keepends=True):
            match = _VIOLATION_HEADER.match(line)
            if match:
                origin = self.origins.get(os.path.basename(match.group(1)))
                if origin is not None:
                    line = f"== [{origin}]" + line[match.end() :]
            lines.append(line)
        return "".join(lines)


# Sentinel for "this file could not be read as json at all", kept distinct from a document
# that is legitimately null.
_INVALID = object()


def sqlfluff_command() -> list[str]:
    """How to invoke sqlfluff.

    sqlfluff is a dependency of this package, so the interpreter running the validator is
    the one that has the pinned version installed — preferring `-m` over whatever
    `sqlfluff` the PATH happens to offer keeps a pre-commit hook, the image and a
    developer's shell on the same version.
    """
    if _has_sqlfluff_module():
        return [sys.executable, "-m", "sqlfluff"]
    executable = shutil.which("sqlfluff")
    if executable is None:
        raise UsageError("sqlfluff is not installed")
    return [executable]


def _has_sqlfluff_module() -> bool:
    try:
        import sqlfluff  # noqa: F401
    except ImportError:
        return False
    return True


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        options = parse_args(arguments)
        if options is None:
            sys.stdout.write(USAGE)
            return 0
        if not options.dashboards:
            raise UsageError("no dashboard files given")

        work = Path(tempfile.mkdtemp())
        if options.keep_tmp:
            sys.stdout.write(f"Keeping extracted snippets in {work}\n")
        try:
            return Validator(options, work, sys.stdout).run()
        finally:
            if not options.keep_tmp:
                shutil.rmtree(work, ignore_errors=True)
    except UsageError as error:
        sys.stdout.flush()
        sys.stderr.write(f"{PROGRAM}: {error}\n")
        sys.stderr.flush()
        return 2


if __name__ == "__main__":
    sys.exit(main())
