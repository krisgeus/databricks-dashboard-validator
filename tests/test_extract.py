"""Unit tests for the snippet extractor.

The extractor replaced an equivalent jq program, and these pin the behaviour that was
verified against it: which shapes count as SQL, how an array element is named, and the
order snippets come out in.
"""

from __future__ import annotations

import json

import pytest

from databricks_dashboard_validator.cli import safe_name
from databricks_dashboard_validator.extract import extract_snippets
from tests.conftest import FIXTURES


def ids_of(document) -> list[str]:
    return [snippet.id for snippet in extract_snippets(document)]


# --- which shapes hold SQL ----------------------------------------------------------------


def test_query_lines_are_joined_as_stored():
    # The lines carry their own newlines, so they are joined with nothing between them.
    document = {"datasets": [{"name": "d", "queryLines": ["SELECT 1\n", "FROM t\n"]}]}
    (snippet,) = extract_snippets(document)
    assert snippet.sql == "SELECT 1\nFROM t\n"
    assert snippet.kind == "query"


def test_a_query_stored_as_one_string_is_a_query():
    (snippet,) = extract_snippets({"datasets": [{"name": "d", "query": "SELECT 1"}]})
    assert snippet.kind == "query"
    assert snippet.sql == "SELECT 1"


def test_an_expression_is_its_own_kind():
    (snippet,) = extract_snippets({"fields": [{"name": "f", "expression": "COUNT(x)"}]})
    assert snippet.kind == "expression"


def test_non_string_lines_inside_query_lines_are_dropped():
    document = {"datasets": [{"name": "d", "queryLines": ["SELECT ", 3, None, "1"]}]}
    (snippet,) = extract_snippets(document)
    assert snippet.sql == "SELECT 1"


@pytest.mark.parametrize(
    "value",
    [
        {"fields": [{"expression": "x"}]},  # a widget query object, not a string
        123,
        True,
        None,
    ],
)
def test_a_query_key_of_the_wrong_shape_is_not_sql(value):
    # A widget's .query is an object holding the fields; only a string is a dataset query.
    assert [
        s for s in extract_snippets({"widget": {"query": value}}) if s.kind == "query"
    ] == []


@pytest.mark.parametrize("blank", ["", "   ", "\n\t ", []])
def test_whitespace_only_sql_is_not_a_snippet(blank):
    assert extract_snippets({"datasets": [{"name": "d", "query": blank}]}) == []


def test_an_unlisted_key_is_ignored():
    assert extract_snippets({"datasets": [{"name": "d", "sql": "SELECT 1"}]}) == []


# --- naming array elements ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("element", "expected"),
    [
        ({"name": "named"}, "datasets[named].query"),
        ({}, "datasets[0].query"),
        ({"name": ""}, "datasets[0].query"),
        ({"name": 3}, "datasets[0].query"),
        ({"name": None, "displayName": "disp"}, "datasets[disp].query"),
        ({"name": False, "widget": {"name": "wname"}}, "datasets[wname].query"),
        ({"widget": {"name": "wn"}, "displayName": "disp"}, "datasets[wn].query"),
        ({"displayName": "only-disp"}, "datasets[only-disp].query"),
    ],
)
def test_an_array_element_is_named_by_the_first_usable_candidate(element, expected):
    assert ids_of({"datasets": [{**element, "query": "SELECT 1"}]}) == [expected]


# The jq extractor this replaced died with "Cannot index string with string \"name\"" on
# this shape, taking the whole run with it rather than checking the dashboard.
def test_an_element_whose_widget_is_not_an_object_is_still_numbered():
    document = {"datasets": [{"widget": "not-an-object", "query": "SELECT 1"}]}
    assert ids_of(document) == ["datasets[0].query"]


def test_a_non_object_array_element_is_numbered():
    document = {"datasets": ["a string", {"name": "second", "query": "SELECT 1"}]}
    assert ids_of(document) == ["datasets[second].query"]


def test_nested_paths_are_rendered_in_full():
    document = {
        "pages": [
            {
                "name": "p",
                "layout": [
                    {
                        "widget": {
                            "name": "w",
                            "queries": [
                                {
                                    "name": "q",
                                    "query": {
                                        "fields": [{"name": "f", "expression": "COUNT(1)"}]
                                    },
                                }
                            ],
                        }
                    }
                ],
            }
        ]
    }
    assert ids_of(document) == [
        "pages[p].layout[w].widget.queries[q].query.fields[f].expression"
    ]


# --- ordering and numbering ---------------------------------------------------------------


def test_snippets_are_numbered_from_one_in_document_order():
    document = {
        "datasets": [{"name": "a", "query": "SELECT 1"}, {"name": "b", "query": "SELECT 2"}],
    }
    assert [(s.seq, s.id) for s in extract_snippets(document)] == [
        (1, "datasets[a].query"),
        (2, "datasets[b].query"),
    ]


def test_the_real_world_example_is_fully_extracted():
    document = json.loads((FIXTURES / "clean.lvdash.json").read_text())
    snippets = extract_snippets(document)
    assert [s.kind for s in snippets] == ["query", "expression", "expression"]


# --- scratch filenames --------------------------------------------------------------------


def test_safe_name_keeps_the_identifying_tail():
    assert safe_name("datasets[runs].queryLines") == "datasets_runs_.queryLines"


def test_safe_name_replaces_every_unsafe_byte():
    assert safe_name("a/b c:d") == "a_b_c_d"


def test_safe_name_is_bounded():
    assert len(safe_name("x" * 500)) == 60


def test_safe_name_survives_non_ascii():
    # tr worked on bytes, so each byte of a multi-byte character became one underscore.
    assert safe_name("café") == "caf__"
