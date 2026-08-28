"""Shared plumbing for the validator's tests.

The behavioural tests drive the validator as a subprocess rather than calling main()
in-process, because the exit status and the exact bytes on stdout and stderr are the
contract a pre-commit hook is judged by, and only a real process proves them.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

import pytest

from databricks_dashboard_validator.cli import DEFAULT_QUERY_CONFIG

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLES = Path(__file__).parent.parent / "examples"

_FOUND = "^  Found #[0-9]+ "


class Result(NamedTuple):
    """What one run of the validator produced."""

    returncode: int
    output: str

    def snippets(self, kind: str = "") -> int:
        """How many snippets the inventory reported, optionally filtered by kind."""
        return len(re.findall(_FOUND + kind, self.output, re.MULTILINE))


def invoke(
    args: Sequence[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> Result:
    completed = subprocess.run(
        [sys.executable, "-m", "databricks_dashboard_validator", *args],
        cwd=None if cwd is None else str(cwd),
        capture_output=True,
        text=True,
        env=None if env is None else {**os.environ, **env},
    )
    return Result(completed.returncode, completed.stdout + completed.stderr)


@pytest.fixture
def run():
    """Run the validator with the two defaults nearly every test wants.

    Nothing here should pick up a .sqlfluff from the directory the tests happen to run in,
    otherwise the expected violation counts depend on the caller's working directory, so
    the bundled config is pinned; and --verbose is what prints the snippet inventory that
    the counting assertions read.

    Both are only defaults: the validator lets a later flag win, so a test that passes its
    own --config or wants the quiet output just says so in its own arguments.
    """

    def _run(*args: str, **kwargs) -> Result:
        return invoke(["--config", str(DEFAULT_QUERY_CONFIG), "--verbose", *args], **kwargs)

    return _run


@pytest.fixture
def run_quiet():
    """Same, without the --verbose default, for the tests that cover the quiet output."""

    def _run(*args: str, **kwargs) -> Result:
        return invoke(["--config", str(DEFAULT_QUERY_CONFIG), *args], **kwargs)

    return _run


@pytest.fixture
def run_in():
    """Run from inside a directory, pinning nothing.

    These are the tests that cover how the validator picks a config out of the repository
    it is checking, which is a question about the working directory.
    """

    def _run(cwd: Path, *args: str) -> Result:
        return invoke(["--verbose", *args], cwd=cwd)

    return _run


@pytest.fixture
def examples() -> Path:
    """The example dashboards, skipped when they are not available.

    The build pipeline runs against an image that has everything copied in, so a skip
    there means coverage was silently lost and should fail the build instead.
    """
    if EXAMPLES.is_dir():
        return EXAMPLES
    if os.environ.get("DASHBOARD_SQL_REQUIRE_ALL") == "1":
        pytest.fail("examples are not available (required when DASHBOARD_SQL_REQUIRE_ALL=1)")
    return pytest.skip("examples are not available")
