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
paths that today's schema happens to use, `extract-sql-snippets.jq` walks the whole
document and picks up anything that looks like SQL. A schema version that moves a query
somewhere else keeps working.

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

### validate-dashboard-sql

Builds the image from this repository.

```yaml
- repo: https://github.com/krisgeus/databricks-dashboard-validator
  rev: v1.0.0
  hooks:
  - id: validate-dashboard-sql
```

### validate-dashboard-sql-docker-latest

Same, using the pre-built image tagged `latest`.

```yaml
- repo: https://github.com/krisgeus/databricks-dashboard-validator
  rev: v1.0.0
  hooks:
  - id: validate-dashboard-sql-docker-latest
```

### validate-dashboard-sql-docker-release

Same, using the pre-built image from the release (tag `v1.0.0`).

```yaml
- repo: https://github.com/krisgeus/databricks-dashboard-validator
  rev: v1.0.0
  hooks:
  - id: validate-dashboard-sql-docker-release
```

All three hooks are restricted to files matching `\.lvdash\.json$`. If your dashboards are
exported under a different name, override `files:` in your own config.

### Outside pre-commit

Against the pre-built image:

```shell
docker run --rm --volume "${PWD}:/src:ro" --workdir /src \
  ghcr.io/krisgeus/databricks-dashboard-sql-check:latest \
  dashboards/pipeline_runs.lvdash.json
```

Or straight from a checkout, with `jq` and `sqlfluff` on the `PATH`:

```shell
./validate-dashboard-sql.sh dashboards/*.lvdash.json
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
a containerised one — the command line is the one channel that behaves the same either way.
Pass options through `args:`.

```yaml
- repo: https://github.com/krisgeus/databricks-dashboard-validator
  rev: v1.0.0
  hooks:
  - id: validate-dashboard-sql-docker-release
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

sqlfluff resolves `.sqlfluff` relative to the file it is linting, and the extracted
snippets live in a scratch directory, so the config has to be passed explicitly. The order
is:

1. `--config FILE`, if given.
2. `.sqlfluff` in the working directory — the root of your repository, under pre-commit.
3. The bundled `sqlfluff-defaults.cfg`.

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
  rev: v1.0.0
  hooks:
  - id: validate-dashboard-sql-docker-release
```

## Tests

`tests/run-tests.sh` covers extraction of each SQL shape, the exit statuses, the mapping of
violations back to json paths, the configuration switches, and a regression test for a
query too large to pass as a command line argument.

Against the image, as the build pipeline runs them:

```shell
docker build -t databricks-dashboard-sql-check:test .
docker run --rm \
  --volume "${PWD}/tests:/tests:ro" \
  --volume "${PWD}/examples:/examples:ro" \
  --env DASHBOARD_SQL_REQUIRE_ALL=1 \
  --entrypoint /tests/run-tests.sh databricks-dashboard-sql-check:test
```

Against the checkout, which needs `jq` and `sqlfluff` on the `PATH`:

```shell
./tests/run-tests.sh
```
