from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import QueryRejected
from .validation import path_string, validate_query


@dataclass(frozen=True)
class Table:
    oid: int
    columns: dict[str, Any]
    selectable: frozenset[str]


def requested_relations(
    query: dict, default_schema: str, *, max_identifier_bytes: int = 128
) -> list[tuple[str, str, bool]]:
    validate_query(query)
    found = {}

    def walk(node, schema):
        if not isinstance(node, dict):
            return
        schema = node.get("schema", schema)
        if "table" in node:
            key = (schema, node["table"])
            if any(
                not name
                or "\x00" in name
                or len(name.encode("utf-8")) > max_identifier_bytes
                for name in key
            ):
                raise QueryRejected(
                    "对象名称无效或超过长度限制。", code="INVALID_IDENTIFIER"
                )
            found[key] = found.get(key, False) or not node.get("only", False)
        for key, value in node.items():
            if key in {
                "value",
                "values",
            }:  # Literal JSON content is never query syntax.
                if key == "values" and isinstance(value, list):
                    for row in value:
                        if isinstance(row, list):
                            for expr in row:
                                walk(expr, schema)
                continue
            if isinstance(value, dict):
                walk(value, schema)
            elif isinstance(value, list):
                for item in value:
                    walk(item, schema)

    walk(query, default_schema)
    if len(found) > 64:
        raise QueryRejected(
            "单次请求最多引用 64 个数据库对象。", code="QUERY_TOO_COMPLEX"
        )
    return [(schema, name, inherited) for (schema, name), inherited in found.items()]


def requested_tables(query: dict) -> list[str]:
    return [name for _, name, _ in requested_relations(query, "public")]


def relation_path(query, default_schema, namespace, name):
    """Locate a missing relation without echoing its name or literal values."""

    def walk(node, schema, path):
        if not isinstance(node, dict):
            return None
        schema = node.get("schema", schema)
        if schema == namespace and node.get("table") == name:
            return path_string((*path, "table"))
        for key, value in node.items():
            if key == "value":
                continue
            if key == "values":
                if isinstance(value, list):
                    for i, row in enumerate(value):
                        if isinstance(row, list):
                            for j, expr in enumerate(row):
                                if found := walk(expr, schema, (*path, key, i, j)):
                                    return found
                continue
            if isinstance(value, dict):
                if found := walk(value, schema, (*path, key)):
                    return found
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    if found := walk(item, schema, (*path, key, i)):
                        return found
        return None

    return walk(query, default_schema, ("query",)) or "query"
