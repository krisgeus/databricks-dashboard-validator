# Pre-commit hook for validating SQL in Databricks dashboards

A Databricks Lakeview dashboard is checked into git as a single `*.lvdash.json` file with
the SQL buried inside it. Nothing in a normal review catches a broken query in there — you
find out when the dashboard is published and a widget renders an error.

This hook pulls every SQL snippet out of the dashboard json and runs
[sqlfluff](https://sqlfluff.com/) over it, so a query that does not parse fails the commit
instead of the deploy.

The starting point is the one-liner that only covers dataset queries:

```shell
jq -r '.datasets[].queryLines | join("")' pipeline_runs.lvdash.json \
  | sqlfluff lint --dialect databricks -
```

## What gets extracted

The dashboard json carries SQL in more than one place, so rather than hard coding the
paths that today's schema happens to use, the extractor walks the whole document and picks
up anything that looks like SQL. A schema version that moves a query somewhere else keeps
working.

| Shape in the json | Kind | What it is |
| --- | --- | --- |
| `*.queryLines` (array of strings) | `query` | A dataset query, stored one string per line |
| `*.query` (string) | `query` | A dataset query as a single string, as older exports write it |
| `*.expression` (string) | `expression` | A scalar SQL expression, such as a widget measure or a filter field |

In practice that means the dataset queries under `datasets[]` plus every field expression
under `pages[].layout[].widget.queries[].query.fields[]` — for `pipeline_runs` in
`examples/`, one query and twenty seven expressions, where the `jq` one-liner above sees
only the query.

Every violation is reported against the path it came from, not against the scratch file
sqlfluff actually read:

```text
== [dashboards/pipeline_runs.lvdash.json: datasets[broken].queryLines] FAIL
L:   1 | P:  13 |  PRS | Couldn't find closing bracket for opening bracket.
```

### Queries and expressions are checked differently

A dataset query is written by a human and is linted with the full configured rule set.

A field expression is a fragment such as ``COUNT(`update_id`)`` that the Databricks UI
generates and rewrites whenever someone edits a visualisation. Style rules have nothing
useful to say about it, so expressions are checked with every rule switched off and only
the parser running — unparsable input is still reported as a `PRS` violation, which is the
part that matters. Each expression is wrapped in `SELECT <expression>` to make it a
statement, so reported positions on line 1 are shifted seven characters to the right by
the leading `SELECT` and the space after it.

## Example dashboards

`examples/` holds three real dashboards twice over: `examples/raw/` as the Databricks UI
exported them, and `examples/cleaned/` with every sqlfluff violation fixed. Only the
cleaned copies are checked by this repository's own pre-commit hook.

[examples/README.md](examples/README.md) records what changed between the two and why,
rule by rule — a worked example of what this hook asks of a dashboard, and a starting
point if you are about to clean up your own.

## Usage

There are four hook ids: one that needs nothing but python, and three that need docker.
They do the same work and print the same thing — pick whichever fits the machines your
repository is committed from.

### validate-dashboard-sql-python

No docker required. pre-commit builds a virtualenv for the hook and installs sqlfluff into
it, so there is nothing to have on the `PATH` beforehand.

```yaml
- repo: https://github.com/krisgeus/databricks-dashboard-validator
  rev: v1.2.0
  hooks:
  - id: validate-dashboard-sql-python
```

This is the one to reach for on a machine without docker, in a CI job that would rather not
start a container, and anywhere a locked-down laptop makes a container a nuisance. The
sqlfluff version is pinned by the hook, so it produces the same verdict everywhere.

### validate-dashboard-sql

Builds the image from this repository.

```yaml
- repo: https://github.com/krisgeus/databricks-dashboard-validator
  rev: v1.2.0
  hooks:
  - id: validate-dashboard-sql
```

### validate-dashboard-sql-docker-latest

Same, using the pre-built image tagged `latest`.

```yaml
- repo: https://github.com/krisgeus/databricks-dashboard-validator
  rev: v1.2.0
  hooks:
  - id: validate-dashboard-sql-docker-latest
```

### validate-dashboard-sql-docker-release

Same, using the pre-built image from the release (tag `v1.2.0`).

```yaml
- repo: https://github.com/krisgeus/databricks-dashboard-validator
  rev: v1.2.0
  hooks:
  - id: validate-dashboard-sql-docker-release
```

All four hooks are restricted to files matching `\.lvdash\.json$`. If your dashboards are
exported under a different name, override `files:` in your own config.

### Outside pre-commit

The validator is an ordinary python package, so `uvx` runs it without installing anything
permanently:

```shell
uvx --from git+https://github.com/krisgeus/databricks-dashboard-validator@v1.2.0 \
  validate-dashboard-sql dashboards/*.lvdash.json
```

Installed into an environment of its own:

```shell
pip install git+https://github.com/krisgeus/databricks-dashboard-validator@v1.2.0
validate-dashboard-sql dashboards/*.lvdash.json
```

Or against the pre-built image:

```shell
docker run --rm --volume "${PWD}:/src:ro" --workdir /src \
  ghcr.io/krisgeus/databricks-dashboard-sql-check:latest \
  dashboards/pipeline_runs.lvdash.json
```

## Configuration

| Flag | Default | Meaning |
| --- | --- | --- |
| `--dialect DIALECT` | `databricks` | sqlfluff dialect |
| `--query-mode lint\|off` | `lint` | How to check dataset queries |
| `--expression-mode lint\|off` | `lint` | How to check widget expressions |
| `--config FILE` | see below | sqlfluff config for dataset queries |
| `--expression-config FILE` | `sqlfluff-expressions.cfg` | sqlfluff config for expressions |
| `--sqlfluff-arg ARG` (repeatable) | empty | Extra flag passed straight to `sqlfluff lint` |
| `--keep-tmp` | off | Keep the extracted `.sql` files for inspection |
| `--verbose`, `-v` | off | Also report which snippet came from where in the json |

There are no environment variable equivalents. pre-commit does not forward the environment
into a `language: docker` hook, so a variable would configure a local run and silently not
a containerised one — the command line is the one channel that behaves the same for every
hook id here. Pass options through `args:`.

```yaml
- repo: https://github.com/krisgeus/databricks-dashboard-validator
  rev: v1.2.0
  hooks:
  - id: validate-dashboard-sql-python
    args: [--dialect, sparksql, --expression-mode, 'off']
```

### What the hook prints

By default the output is the sqlfluff run and nothing else: how many snippets of each kind
were linted, with which config, and every violation against the json path it came from.

`--verbose` adds an inventory of every snippet found and the file it came from. That is
debugging output — a violation already names its own path, so the inventory mostly gets in
the way of reading the failure:

```text
Extracting SQL from pipeline_runs.lvdash.json
  Found #1 query      datasets[pipeline_runs].queryLines
  Found #2 expression pages[runs].layout[total].widget.queries[main_query].query.fields[count(update_id)].expression
```

Pre-commit's own `-v` does not reach the hook. Pre-commit captures hook output and shows it
only when the hook fails or when `-v` is given, but it passes no signal to the hook about
which mode it is in — the environment is identical either way. To get the inventory under
pre-commit, ask for it explicitly:

```yaml
  - id: validate-dashboard-sql
    args: [--verbose]
```

### Choosing the rule set

sqlfluff resolves config relative to the file it is linting, and the extracted snippets
live in a scratch directory outside your repository, so the config has to be passed
explicitly. The order is:

1. `--config FILE`, if given.
2. `.sqlfluff` in the working directory — the root of your repository, under pre-commit.
3. `pyproject.toml` in the working directory, if it has a `[tool.sqlfluff...]` section.
   A `pyproject.toml` that says nothing about sqlfluff is ignored, so an unrelated one does
   not quietly displace the bundled defaults.
4. The bundled `sqlfluff-defaults.cfg`.

Whichever one wins is the only config in play: the validator passes
`--ignore-local-config` so sqlfluff does not search its default locations on top of it.
That keeps the config named in the output honest, and keeps a stray `~/.sqlfluff` from
changing results between a laptop and CI.

Passing the config explicitly matters for more than rule selection. Left to itself sqlfluff
reads the working directory's config for core settings but resolves the nested sections
against the snippet's own path — out in the scratch directory, where there is no config at
all. A project that configures a templater keeps the templater and loses the settings that
parameterise it:

```toml
[tool.sqlfluff.core]
templater = "placeholder"

[tool.sqlfluff.templater.placeholder]
param_regex = '...'
```

which fails before linting anything, with
`ValueError: No param_regex nor param_style was provided to the placeholder templater!`.
Naming the file on the command line keeps the two halves together.

Widget expressions are the exception. They are fragments rather than statements, so they
are always checked with the bundled syntax-only config regardless of what your repository
configures — including its templater.

One thing to watch for when your repository's config takes over the dataset queries: a
config written for dbt models or hand written SQL may switch off the very check this hook
is for. `ignore = "parsing"` in particular downgrades `PRS` to nothing, so a query with an
unbalanced bracket passes and the hook exits 0. If your repository config sets it, point
the hook at a config of its own instead:

```yaml
  - id: validate-dashboard-sql
    args: [--config, dashboards/.sqlfluff]
```

The bundled default is deliberately syntax first rather than style first. Dashboard SQL is
edited through the Databricks UI as often as it is edited by hand, so a hook that fails a
commit over indentation is a hook that gets disabled within a week. It switches off:

- `layout` — whitespace and indentation, which the Databricks editor owns anyway.
- `references.from` — false positives on struct access such as
  `putl.trigger_details.job_task.job_id`.
- `references.qualification` — unqualified column names are idiomatic in dashboard queries.
- `structure.column_order` — pure style.

Running the full rule set over the example dashboard produces about ninety violations,
nearly all of them indentation. To opt back in, drop a `.sqlfluff` in your repository root:

```ini
[sqlfluff]
dialect = databricks
```

## Validating the rest of the dashboard json

The SQL is the part with the sharpest failure mode, but it is not the only thing worth
checking. A useful `.pre-commit-config.yaml` for a repository holding dashboards:

```yaml
repos:
- repo: https://github.com/pre-commit/pre-commit-hooks
  rev: v4.6.0
  hooks:
  # A dashboard that is not valid json will not import at all.
  - id: check-json
    files: \.lvdash\.json$
  # Dashboards are large and are rewritten wholesale by the UI; without a canonical
  # format every export is a thousand line diff.
  - id: pretty-format-json
    args: [--autofix, --indent, '2', --no-sort-keys]
    files: \.lvdash\.json$
  - id: check-added-large-files
    args: [--maxkb, '2048']

# And the SQL inside it.
- repo: https://github.com/krisgeus/databricks-dashboard-validator
  rev: v1.2.0
  hooks:
  - id: validate-dashboard-sql-python
```

## Development

The project is managed with [uv](https://docs.astral.sh/uv/). One sync gets the validator,
its pinned sqlfluff, and the tooling:

```shell
uv sync --group dev
uv run validate-dashboard-sql dashboards/*.lvdash.json
```

[ruff](https://docs.astral.sh/ruff/) lints and formats, [ty](https://docs.astral.sh/ty/)
type checks, and both run as pre-commit hooks. `ty` and the dogfooding hook run out of this
environment, so `uv sync` has to have happened before `pre-commit run`.

```shell
uv run ruff check --fix .
uv run ruff format .
uv run ty check
```

## Tests

`tests/test_extract.py` covers the extractor on its own — which shapes count as SQL, how an
array element is named, the order snippets come out in. `tests/test_cli.py` drives the
built command as a subprocess and covers the exit statuses, the mapping of violations back
to json paths, the configuration switches, and a regression test for a query too large to
pass as a command line argument.

Against the checkout:

```shell
uv run pytest
```

Against the image, as the build pipeline runs them. The `test` stage is the published image
plus pytest and the tests, so the suite exercises the install that ships:

```shell
docker build --target test -t databricks-dashboard-sql-check:test .
docker run --rm --env DASHBOARD_SQL_REQUIRE_ALL=1 databricks-dashboard-sql-check:test
```

`DASHBOARD_SQL_REQUIRE_ALL=1` turns "the example dashboards are not available" from a skip
into a failure, which is what the build pipeline wants: a skip there would mean coverage
was silently lost.
