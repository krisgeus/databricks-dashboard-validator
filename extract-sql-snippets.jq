# Extract every inline SQL snippet from a Databricks Lakeview dashboard (*.lvdash.json).
#
# The dashboard json carries SQL in more than one place, so instead of hard coding the
# handful of paths that today's schema uses, this walks the whole document and picks up
# anything that looks like SQL. New schema versions that move a query around keep working.
#
# Recognised shapes:
#   *.queryLines   array of strings   a dataset query, split over one string per line
#   *.query        string             a dataset query as a single string (older exports)
#   *.expression   string             a scalar SQL expression, e.g. a widget measure
#
# Output is JSON Lines (use jq -c), one compact object per snippet:
#
#   {"seq":1,"kind":"query","id":"datasets[foo].queryLines","sql":"SELECT ..."}
#
# Keeping the payload json encoded means a snippet may contain newlines, quotes or any
# other byte without the consumer needing to guess where one snippet ends and the next
# begins, and nothing has to be passed as a command line argument.
#
# The id is a dotted path into the document with array indices replaced by the name of the
# element where the schema provides one, e.g.
#   datasets[pipeline_runs].queryLines
#   pages[runs].layout[3].widget.queries[main_query].query.fields[duration_seconds].expression

# Which keys hold SQL, and what to call a snippet found under each. A key not listed here
# is ignored; a listed key whose value is the wrong shape (widget .query is an object, not
# a string) is dropped by sql_text below.
def kinds: { queryLines: "query", query: "query", expression: "expression" };

# The SQL at the current value: one string, or an array of line strings joined as stored
# (the lines carry their own newlines). Anything else yields nothing, dropping the match.
def sql_text:
  if type == "array" then map(strings) | join("") else strings end;

# Name to use for the array element at $i, falling back to its index.
def element_name($i):
  (objects | (.name // .widget.name // .displayName) | strings | select(. != ""))
  // ($i | tostring);

# Render a path array as a readable dotted id, resolving array indices to element names.
def id_for($root; $p):
  [ range(0; $p | length) as $i
    | $p[$i] as $seg
    | if ($seg | type) == "string"
      then "." + $seg
      else "[" + ($root | getpath($p[0:$i + 1]) | element_name($seg)) + "]"
      end
  ]
  | join("")
  | ltrimstr(".");

. as $root
| [ paths as $p
    | kinds[$p[-1] | strings] as $kind
    | select($kind)
    | (getpath($p) | sql_text) as $sql
    | select($sql | test("\\S"))
    | { kind: $kind, id: id_for($root; $p), sql: $sql }
  ]
| to_entries[]
| { seq: (.key + 1) } + .value
