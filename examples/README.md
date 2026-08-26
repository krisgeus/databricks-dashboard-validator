# Example dashboards

Two copies of the same three dashboards:

| Directory | What is in it |
| --- | --- |
| `raw/` | Exactly what the Databricks UI exported, sqlfluff violations and all |
| `cleaned/` | The same dashboards with every violation fixed |

Only `cleaned/` is checked by the hook in `.pre-commit-config.yaml`. The `raw/` copies keep
their violations on purpose — they are what this document describes the cleanup against, so
linting them would fail every commit by design.

To see the difference for yourself:

```shell
./validate-dashboard-sql.sh examples/raw/student_grades.lvdash.json      # exits 1
./validate-dashboard-sql.sh examples/cleaned/student_grades.lvdash.json  # exits 0
```

Nothing outside `datasets[].queryLines` was touched. Every widget, layout, field expression
and dataset name is byte for byte what the export produced, so the cleaned dashboards import
back into Databricks unchanged:

```shell
diff <(jq 'del(.. | .queryLines?)' examples/raw/student_grades.lvdash.json) \
     <(jq 'del(.. | .queryLines?)' examples/cleaned/student_grades.lvdash.json)
```

## What was fixed

Counts are violations in `raw/`, all of which are gone in `cleaned/`.

| Rule | What it flagged | student_grades | pipeline_runs | job_runs |
| --- | --- | --- | --- | --- |
| AL01 | implicit table aliasing | 16 | | |
| AL03 | column expression without alias | 2 | | |
| AL05 | alias never used | 3 | | |
| AL08 | column alias reused | 2 | | |
| AL10 | derived table without alias | 1 | | |
| AM05 | join not fully qualified | 7 | | |
| ST07 | `USING` instead of `ON` | 7 | | |
| CP03 | function name not upper case | 1 | 2 | 2 |
| RF04 | keyword used as identifier | | | 1 |

The `sqlfluff-defaults.cfg` shipped with the hook was **not** changed to make any of this
pass. The cleaned dashboards are clean against the rule set as it is published.

### ST07 (structure.using)

`USING` was rewritten as an explicit `ON`. This is the change that pulled most of the others
along with it, because `ON` needs both sides of the join spelled out.

```sql
-- raw
FROM grades g
JOIN students s USING (student_id)

-- cleaned
FROM grades
INNER JOIN students
  ON grades.student_id = students.student_id
```

Note that `USING` and `ON` differ for `SELECT *`: `USING` collapses the join column into one,
`ON` keeps both. Every query here selects an explicit column list, so the result sets are
unchanged.

### AL01 (aliasing.table)

Short table aliases were dropped and the column references qualified with the full table
name instead. Once a join is written as `ON`, a bare `student_id` would be ambiguous, so the
qualification is load bearing rather than style:

```sql
-- raw
SELECT s.department, ROUND(AVG(g.score), 1) AS avg_score
FROM grades g

-- cleaned
SELECT students.department, ROUND(AVG(grades.score), 1) AS avg_score
FROM grades
```

Columns that exist in only one of the joined tables kept their unqualified form, as the
export had them — `semester` in `enrollment_semester`, for instance.

One alias had to stay. In `enrollment_forecast` the outer relation and the relation inside
its own `WHERE` subquery are both `dated`, which sqlfluff reports as AL04 (duplicate table
alias) once the outer alias is removed. It keeps an explicit alias, which is what AL01 asks
for anyway:

```sql
FROM dated AS d
WHERE d.ds = (SELECT MAX(ds) FROM dated)
```

### AM05 (ambiguous.join)

`JOIN` spelled out as `INNER JOIN`. No semantic change.

### AL05 (aliasing.unused)

Three aliases were declared and never referenced, for example `FROM enrollments e` where
nothing said `e.`. Rewriting the join as `ON enrollments.student_id = students.student_id`
uses both relations by name, so these resolved themselves.

### AL10 (aliasing.required)

The derived table in `enrollment_forecast` had no alias:

```sql
-- raw
FROM ( SELECT semester, COUNT(*) AS enrollment_count FROM enrollments ... )

-- cleaned
FROM ( SELECT semester, COUNT(*) AS enrollment_count FROM enrollments ... ) AS semester_counts
```

### AL03 and AL08 (aliasing.expression, aliasing.unique.column)

In the `combined` CTE of `enrollment_forecast`, the second and third `UNION ALL` branches
left their columns unaliased. Two were bare `CAST(NULL AS DOUBLE)` (AL03) and three in a row
inherited the name `total_enrollment` (AL08):

```sql
-- raw
SELECT d.semester, d.ds,
  CAST(NULL AS DOUBLE),
  d.total_enrollment, d.total_enrollment, d.total_enrollment
FROM dated d

-- cleaned
SELECT d.semester, d.ds,
  CAST(NULL AS DOUBLE) AS original,
  d.total_enrollment AS prediction,
  d.total_enrollment AS prediction_upper,
  d.total_enrollment AS prediction_lower
FROM dated AS d
```

A `UNION` takes its output column names from the first branch, so naming the later branches
documents the intent without changing a single result column — which matters here, because
the widgets reference `original`, `prediction`, `prediction_upper` and `prediction_lower`
by name.

### CP03 (capitalisation.functions)

`date_format(` in `pipeline_runs` and `job_runs`, `ai_forecast(` in `student_grades`, upper
cased to match every other function call in those queries. Databricks function names are
case insensitive.

### RF04 (references.keywords)

`job_runs` aliases a column to `name`, which is a reserved word. Renaming it would break the
widgets that reference the field, so it is quoted instead:

```sql
-- raw
COALESCE(job.name, jrt.run_name) AS name,

-- cleaned
COALESCE(job.name, jrt.run_name) AS `name`,
```

## Formatting

The `student_grades` export stored its queries with a blank line between every SQL line and
trailing spaces at the end of each. The six queries that were rewritten came out reformatted
to one SQL line per `queryLines` element with no trailing whitespace. This is cosmetic —
sqlfluff's layout rules are excluded by default, so none of it was required to make the
dashboards pass.

Its seventh query, `overall_metrics`, had no violations and was not touched at all. It still
carries the blank lines and trailing spaces of the original export, which is the clearest
evidence that the cleanup only went where a rule pointed.

## A caveat

These queries were verified by linting, not by running them against a Databricks warehouse.
The rewrites are mechanical and each one is explained above, but if you adapt them, check
the result sets.
