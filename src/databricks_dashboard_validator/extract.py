"""Extract every inline SQL snippet from a Databricks Lakeview dashboard (*.lvdash.json).

The dashboard json carries SQL in more than one place, so instead of hard coding the
handful of paths that today's schema uses, this walks the whole document and picks up
anything that looks like SQL. New schema versions that move a query around keep working.

Recognised shapes:
    *.queryLines   array of strings   a dataset query, split over one string per line
    *.query        string             a dataset query as a single string (older exports)
    *.expression   string             a scalar SQL expression, e.g. a widget measure

The id is a dotted path into the document with array indices replaced by the name of the
element where the schema provides one, e.g.

    datasets[pipeline_runs].queryLines
    pages[runs].layout[3].widget.queries[main_query].query.fields[duration_seconds].expression

This is a port of the extract-sql-snippets.jq that earlier versions shelled out to, and
deliberately keeps its behaviour down to the details: which shapes are recognised, how an
array element is named, and the order snippets come out in.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any, NamedTuple

# Which keys hold SQL, and what to call a snippet found under each. A key not listed here
# is ignored; a listed key whose value is the wrong shape (a widget's .query is an object,
# not a string) is dropped by _sql_text below.
KINDS = {"queryLines": "query", "query": "query", "expression": "expression"}

_NON_SPACE = re.compile(r"\S")


class Snippet(NamedTuple):
    """One piece of SQL, and where in the dashboard json it came from."""

    seq: int
    kind: str
    id: str
    sql: str


def _sql_text(value: Any) -> str | None:
    """The SQL at a value: one string, or line strings joined as stored.

    The lines of a queryLines array carry their own newlines, so they are joined with
    nothing between them. Any other shape yields None, which drops the match.
    """
    if isinstance(value, list):
        return "".join(line for line in value if isinstance(line, str))
    # bool is a subclass of int rather than of str, so it falls through to None as it
    # does in jq, where `strings` passes only actual strings.
    if isinstance(value, str):
        return value
    return None


def _element_name(element: Any, index: int) -> str:
    """Name to use for an array element, falling back to its index.

    The first of .name, .widget.name and .displayName that is present wins, and it has to
    be a non-empty string to be used at all — an element carrying `"name": 3` is numbered
    rather than called "3", which is what the jq `strings` filter did.
    """
    if isinstance(element, dict):
        widget = element.get("widget")
        candidates = (
            element.get("name"),
            widget.get("name") if isinstance(widget, dict) else None,
            element.get("displayName"),
        )
        # jq's `//` takes the first alternative that is neither null nor false.
        chosen = next((c for c in candidates if c is not None and c is not False), None)
        if isinstance(chosen, str) and chosen != "":
            return chosen
    return str(index)


def _walk(node: Any, trail: list[str]) -> Iterator[tuple]:
    """Yield (id, kind, sql) for every SQL-bearing key at or below node, in document order."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = [*trail, "." + key]
            kind = KINDS.get(key)
            if kind is not None:
                sql = _sql_text(value)
                if sql is not None and _NON_SPACE.search(sql):
                    # The id is the path including the key itself. The separator leading
                    # the very first segment is not part of it.
                    yield ("".join(here)[1:], kind, sql)
            yield from _walk(value, here)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, [*trail, "[" + _element_name(value, index) + "]"])


def extract_snippets(document: Any) -> list[Snippet]:
    """Every SQL snippet in a parsed dashboard document, numbered from one."""
    return [
        Snippet(seq=seq, kind=kind, id=id_, sql=sql)
        for seq, (id_, kind, sql) in enumerate(_walk(document, []), start=1)
    ]
