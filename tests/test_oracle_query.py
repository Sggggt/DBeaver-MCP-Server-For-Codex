"""Compiler checks use symbolic objects and never connect to a database."""

from copy import deepcopy
from datetime import date, datetime

import pytest
from sqlglot import exp, parse

from dbeaver_database_mcp.adapters.oracle.connection import parse_configuration
from dbeaver_database_mcp.adapters.oracle.query import compile_query
from dbeaver_database_mcp.core.catalog import Table
from dbeaver_database_mcp.core.errors import QueryRejected


def request():
    return {
        "table": "T",
        "alias": "t",
        "columns": [{"source": "t", "column": "C"}],
        "limit": 5,
    }


def catalog(name="T", column="C"):
    return {("S", name): Table(1, {column: "VARCHAR2"}, frozenset({column}))}


@pytest.mark.parametrize(
    "value",
    ["'; DELETE FROM T; --", "O'Reilly", "$x$ SELECT 1 $x$", "中文", None, 12, 0.125],
)
def test_values_remain_named_binds(value):
    query = request()
    query["where"] = {
        "field": {"source": "t", "column": "C"},
        "op": "eq",
        "value": value,
    }
    built = compile_query("S", query, catalog())
    assert value in built.parameters.values()
    if isinstance(value, str):
        assert value not in built.statement
    statements = parse(built.statement, read="oracle")
    assert len(statements) == 1 and isinstance(statements[0], exp.Select)
    assert not statements[0].find(exp.Insert) and not statements[0].find(exp.Delete)
    assert list(built.parameters.values())[-2:] == [0, 6]


@pytest.mark.parametrize("name", ['T";DROP TABLE T;--', "中文表", "x%y", "A.B"])
def test_identifiers_cannot_become_statements(name):
    query = request()
    query["table"] = name
    built = compile_query("S", query, catalog(name))
    statements = parse(built.statement, read="oracle")
    assert len(statements) == 1
    assert statements[0].find(exp.Table).name == name


@pytest.mark.parametrize(
    "change",
    [
        {"sql": "SELECT 1"},
        {"distinct_on": [{"source": "t", "column": "C"}]},
        {"recursive": True},
        {"only": True},
        {"select": [{"as": "x", "expr": {"call": "set_config", "args": []}}]},
    ],
)
def test_unsupported_or_raw_language_is_rejected(change):
    with pytest.raises(QueryRejected):
        compile_query("S", request() | change, catalog())


def test_cte_set_subqueries_and_joins_are_structured():
    inner = request()
    inner.pop("limit")
    query = {
        "with": [{"name": "q", "query": inner}],
        "from": {"cte": "q", "alias": "a"},
        "joins": [
            {
                "kind": "left",
                "table": "T",
                "alias": "b",
                "on": [
                    {
                        "left": {"source": "a", "column": "C"},
                        "right": {"source": "b", "column": "C"},
                    }
                ],
            }
        ],
        "columns": [{"source": "a", "column": "C"}],
        "limit": 5,
    }
    built = compile_query("S", query, catalog())
    assert "WITH" in built.statement and "LEFT JOIN" in built.statement
    assert len(parse(built.statement, read="oracle")) == 1
    union = {"set": {"op": "except", "queries": [inner, deepcopy(inner)]}, "limit": 5}
    assert " MINUS " in compile_query("S", union, catalog()).statement


def test_group_window_and_case_bind_values():
    query = {
        "table": "T",
        "alias": "t",
        "select": [
            {
                "as": "n",
                "expr": {
                    "window": "row_number",
                    "order_by": [
                        {"expr": {"source": "t", "column": "C"}, "direction": "asc"}
                    ],
                },
            },
            {
                "as": "x",
                "expr": {
                    "case": [
                        {
                            "when": {
                                "field": {"source": "t", "column": "C"},
                                "op": "eq",
                                "value": "v",
                            },
                            "then": {"value": 1},
                        }
                    ],
                    "else": {"value": 0},
                },
            },
        ],
        "limit": 5,
    }
    built = compile_query("S", query, catalog())
    assert "ROW_NUMBER() OVER" in built.statement and "CASE" in built.statement
    assert "v" in built.parameters.values()


def test_null_safe_and_case_insensitive_conditions():
    query = request()
    query["where"] = {
        "field": {"source": "t", "column": "C"},
        "op": "is_not_distinct",
        "value": None,
    }
    assert "DECODE(" in compile_query("S", query, catalog()).statement
    query["where"]["op"] = "ilike"
    query["where"]["value"] = "%x%"
    assert "LOWER(" in compile_query("S", query, catalog()).statement


def test_typed_dates_and_identifier_version_limits():
    for typename, value, expected in [
        ("date", "2025-01-01", date),
        ("timestamptz", "2025-01-01T10:00:00+08:00", datetime),
    ]:
        query = {
            "select": [{"as": "x", "expr": {"value": value, "type": typename}}],
            "limit": 1,
        }
        assert isinstance(compile_query("S", query, {}).parameters["p1"], expected)
    name = "A" * 31
    query = request() | {"table": name}
    with pytest.raises(QueryRejected):
        compile_query("S", query, catalog(name), 30)
    assert compile_query("S", query, catalog(name), 128)


@pytest.mark.parametrize(
    "configuration,key",
    [
        ({"url": "jdbc:oracle:thin:@//localhost:1521/service"}, "service_name"),
        ({"url": "jdbc:oracle:thin:@localhost:1521/service"}, "service_name"),
        ({"url": "jdbc:oracle:thin:@localhost:1521:instance"}, "sid"),
        (
            {
                "host": "localhost",
                "database": "service",
                "properties": {"@dbeaver-sid-service@": "SERVICE"},
            },
            "service_name",
        ),
    ],
)
def test_saved_oracle_connection_forms(configuration, key):
    result = parse_configuration(configuration)
    assert key in result.options and result.port == 1521
    assert "localhost" not in repr(result)


@pytest.mark.parametrize(
    "url",
    [
        "jdbc:postgresql://localhost/db",
        "jdbc:oracle:oci:@alias",
        "jdbc:oracle:thin:@(DESCRIPTION=x)",
        "jdbc:oracle:thin:@//localhost/db?wallet=path",
        "jdbc:oracle:thin:@//user:password@localhost/db",
    ],
)
def test_unsupported_transport_is_rejected(url):
    with pytest.raises(ValueError):
        parse_configuration({"url": url})
