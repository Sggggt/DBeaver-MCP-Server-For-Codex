"""Compile a closed relational JSON language into parameterized SELECTs."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from psycopg import sql
from psycopg.types.json import Json, Jsonb

from ...core.catalog import Table, relation_path, requested_relations, requested_tables
from ...core.errors import QueryRejected, error_stage
from ...core.query_schema import (
    AGGREGATES,
    FILTER_DEFS,
    FUNCTIONS,
    QUERY_SCHEMA,
    TYPES,
    WINDOWS,
    obj,
)
from ...core.validation import path_string, validate_query, validate_schema

__all__ = [
    "FILTER_DEFS",
    "QUERY_SCHEMA",
    "BuiltQuery",
    "QueryRejected",
    "Table",
    "compile_query",
    "obj",
    "requested_tables",
    "validate_query",
    "validate_schema",
    "relation_path",
    "requested_relations",
]


@dataclass(frozen=True)
class Fragment:
    text: sql.Composable
    params: tuple = ()
    paths: tuple = ()

    def __add__(self, other):
        return Fragment(
            self.text + other.text, self.params + other.params, self.paths + other.paths
        )


def literal(text: str) -> Fragment:
    return Fragment(sql.SQL(text))


def form(template: str, *args: Fragment) -> Fragment:
    return Fragment(
        sql.SQL(template).format(*(a.text for a in args)),
        tuple(p for a in args for p in a.params),
        tuple(p for a in args for p in a.paths),
    )


def joined(separator: str, items) -> Fragment:
    items = list(items)
    return Fragment(
        sql.SQL(separator).join(i.text for i in items),
        tuple(p for i in items for p in i.params),
        tuple(p for i in items for p in i.paths),
    )


def ident(*names: str) -> Fragment:
    if any(
        not isinstance(name, str)
        or not name
        or "\x00" in name
        or len(name.encode("utf-8")) > 63
        for name in names
    ):
        raise QueryRejected(
            "对象名称超过 PostgreSQL 的 63 字节限制。", code="INVALID_IDENTIFIER"
        )
    # Queries have bound LIMIT parameters: percent signs in quoted identifiers
    # must also be escaped for psycopg's placeholder parser.
    return Fragment(sql.Identifier(*(name.replace("%", "%%") for name in names)))


def parameter(value, type_name=None, path=None) -> Fragment:
    # PostgreSQL literal defaults avoid ambiguous smallint/float8 overloads.
    if type_name is None and type(value) is int:
        type_name = (
            "int4"
            if -(2**31) <= value < 2**31
            else ("int8" if -(2**63) <= value < 2**63 else "numeric")
        )
    elif type_name is None and type(value) is float:
        value, type_name = Decimal(str(value)), "numeric"
    if type_name in {"json", "jsonb"}:
        value = (Json if type_name == "json" else Jsonb)(value)
    elif isinstance(value, dict):
        value, type_name = Jsonb(value), "jsonb"
    fragment = Fragment(
        sql.Placeholder(), (value,), (path_string(path) if path else None,)
    )
    if type_name:
        fragment = form("{}::{}", fragment, cast_type(type_name))
    return fragment


def cast_type(name: str) -> Fragment:
    if name not in TYPES:
        raise QueryRejected("不支持该类型转换。", code="UNSUPPORTED_CAST")
    if name.endswith("[]"):
        return ident("pg_catalog", name[:-2]) + literal("[]")
    return ident("pg_catalog", name)


@dataclass(frozen=True)
class BuiltQuery:
    statement: sql.Composed
    parameters: tuple[Any, ...]
    limit: int
    outputs: tuple[str, ...]
    tables: tuple[str, ...]
    aggregate: bool
    parameter_paths: tuple = ()

    def summary(self):
        return {
            "operation": "select",
            "tables": list(self.tables),
            "columns": list(self.outputs),
            "limit": self.limit,
            "aggregate": self.aggregate,
            "parameter_count": len(self.parameters),
            "raw_sql_accepted": False,
            "permission_scope": "database_account",
        }


OPERATORS = {
    "eq": "=",
    "ne": "<>",
    "lt": "<",
    "le": "<=",
    "gt": ">",
    "ge": ">=",
    "add": "+",
    "sub": "-",
    "mul": "*",
    "div": "/",
    "mod": "%",
    "concat": "||",
    "json_get": "->",
    "json_text": "->>",
    "json_path": "#>",
    "json_path_text": "#>>",
    "contains": "@>",
    "contained_by": "<@",
    "overlap": "&&",
    "like": "~~",
    "ilike": "~~*",
    "not_like": "!~~",
    "not_ilike": "!~~*",
    "regex": "~",
    "iregex": "~*",
    "not_regex": "!~",
    "not_iregex": "!~*",
}


class Compiler:
    def __init__(self, schema, catalog):
        self.schema, self.catalog = schema, catalog
        self.tables = set()
        self.nodes = 0
        self.aggregate = False

    def tick(self, path):
        self.nodes += 1
        if self.nodes > 4000 or len(path) > 64:
            raise QueryRejected(
                "查询表达式超过复杂度上限。",
                code="QUERY_TOO_COMPLEX",
                path=path_string(path),
            )

    def fail(self, message, path, code="INVALID_QUERY"):
        raise QueryRejected(message, code=code, path=path_string(path), stage="compile")

    def binary(self, op, left, right):
        operator = OPERATORS[op]
        # Percent must be doubled in psycopg parameterized SQL text.
        operator = operator.replace("%", "%%")
        return form(
            "({}" + " OPERATOR(pg_catalog." + operator + ") " + "{})", left, right
        )

    def expr(self, node, scope, outputs, ctes, path):
        self.tick(path)
        if "source" in node:
            source, column = node["source"], node["column"]
            if source not in scope:
                self.fail("未知表或子查询别名。", (*path, "source"), "UNKNOWN_SOURCE")
            if column not in scope[source] and scope[source] is not None:
                self.fail("引用的字段不存在。", (*path, "column"), "COLUMN_NOT_FOUND")
            return ident(source, column)
        if "value" in node:
            return parameter(node["value"], node.get("type"), (*path, "value"))
        if "output" in node:
            if node["output"] not in outputs:
                self.fail("未知输出别名。", (*path, "output"), "UNKNOWN_OUTPUT")
            return outputs[node["output"]]
        if "call" in node:
            name = node["call"]
            if name not in FUNCTIONS:
                self.fail(
                    "函数不在已封装的只读函数列表中。",
                    (*path, "call"),
                    "UNSUPPORTED_FUNCTION",
                )
            args = [
                self.expr(a, scope, outputs, ctes, (*path, "args", i))
                for i, a in enumerate(node.get("args", []))
            ]
            if name in {
                "current_date",
                "current_timestamp",
                "current_user",
                "session_user",
            }:
                if args:
                    self.fail("该上下文表达式不接受参数。", path)
                return literal(name.upper())
            target = (
                literal(name.upper())
                if name in {"coalesce", "nullif", "greatest", "least"}
                else ident("pg_catalog", name)
            )
            return form("{}({})", target, joined(", ", args))
        if "op" in node:
            return self.binary(
                node["op"],
                self.expr(node["left"], scope, outputs, ctes, (*path, "left")),
                self.expr(node["right"], scope, outputs, ctes, (*path, "right")),
            )
        if "cast" in node:
            return form(
                "({})::{}",
                self.expr(node["expr"], scope, outputs, ctes, (*path, "expr")),
                cast_type(node["cast"]),
            )
        if "case" in node:
            parts = [literal("CASE")]
            for i, branch in enumerate(node["case"]):
                parts.append(
                    form(
                        " WHEN {} THEN {}",
                        self.condition(
                            branch["when"],
                            scope,
                            outputs,
                            ctes,
                            (*path, "case", i, "when"),
                        ),
                        self.expr(
                            branch["then"],
                            scope,
                            outputs,
                            ctes,
                            (*path, "case", i, "then"),
                        ),
                    )
                )
            if "else" in node:
                parts.append(
                    form(
                        " ELSE {}",
                        self.expr(node["else"], scope, outputs, ctes, (*path, "else")),
                    )
                )
            return joined("", parts) + literal(" END")
        if "aggregate" in node or "window" in node:
            name = node.get("aggregate", node.get("window"))
            is_window = "window" in node
            if name not in (WINDOWS if is_window else AGGREGATES):
                self.fail("聚合/窗口操作不受支持。", path, "UNSUPPORTED_FUNCTION")
            self.aggregate |= not is_window
            args = [
                self.expr(a, scope, outputs, ctes, (*path, "args", i))
                for i, a in enumerate(node.get("args", []))
            ]
            if not args and name == "count":
                if node.get("distinct"):
                    self.fail("distinct count 必须提供字段。", path)
                args = [literal("*")]
            inside = (
                literal("DISTINCT ") if node.get("distinct") else literal("")
            ) + joined(", ", args)
            if not is_window and node.get("order_by"):
                inside += literal(" ORDER BY ") + self.orders(
                    node["order_by"], scope, outputs, ctes, (*path, "order_by"), False
                )
            result = form("{}({})", ident("pg_catalog", name), inside)
            if node.get("within_group"):
                result += form(
                    " WITHIN GROUP (ORDER BY {})",
                    self.orders(
                        node["within_group"],
                        scope,
                        outputs,
                        ctes,
                        (*path, "within_group"),
                        False,
                    ),
                )
            if node.get("filter"):
                result += form(
                    " FILTER (WHERE {})",
                    self.condition(
                        node["filter"], scope, outputs, ctes, (*path, "filter")
                    ),
                )
            if is_window:
                over = []
                if node.get("partition_by"):
                    over.append(
                        literal("PARTITION BY ")
                        + joined(
                            ", ",
                            [
                                self.expr(
                                    e, scope, outputs, ctes, (*path, "partition_by", i)
                                )
                                for i, e in enumerate(node["partition_by"])
                            ],
                        )
                    )
                if node.get("order_by"):
                    over.append(
                        literal("ORDER BY ")
                        + self.orders(
                            node["order_by"],
                            scope,
                            outputs,
                            ctes,
                            (*path, "order_by"),
                            False,
                        )
                    )
                if "frame" in node:
                    frame = node["frame"]
                    over.append(
                        literal(frame["unit"].upper() + " BETWEEN ")
                        + self.bound(frame["start"], (*path, "frame", "start"))
                        + literal(" AND ")
                        + self.bound(frame["end"], (*path, "frame", "end"))
                    )
                result += form(" OVER ({})", joined(" ", over))
            return result
        if "subquery" in node:
            result, names = self.query(
                node["subquery"], ctes, scope, (*path, "subquery")
            )
            if len(names) != 1:
                self.fail("标量子查询必须返回一列。", path)
            return form("({})", result)
        if "array" in node:
            return form(
                "ARRAY[{}]",
                joined(
                    ", ",
                    [
                        self.expr(e, scope, outputs, ctes, (*path, "array", i))
                        for i, e in enumerate(node["array"])
                    ],
                ),
            )
        self.fail("未知结构化表达式。", path)

    def bound(self, bound, path):
        kind = bound["kind"]
        if kind in {"preceding", "following"}:
            if "offset" not in bound or type(bound["offset"]) is not int:
                self.fail("窗口边界必须有整数 offset。", path)
            return parameter(bound["offset"], path=(*path, "offset")) + literal(
                " " + kind.upper()
            )
        if "offset" in bound:
            self.fail("该窗口边界不接受 offset。", path)
        return literal(kind.replace("_", " ").upper())

    def condition(self, node, scope, outputs, ctes, path):
        self.tick(path)
        if "combine" in node:
            parts = [
                self.condition(n, scope, outputs, ctes, (*path, "conditions", i))
                for i, n in enumerate(node["conditions"])
            ]
            if node["combine"] == "not":
                if len(parts) != 1:
                    self.fail("NOT 条件只接受一个子条件。", path)
                return form("(NOT {})", parts[0])
            return form("({})", joined(" " + node["combine"].upper() + " ", parts))
        if "exists" in node:
            query, _ = self.query(node["exists"], ctes, scope, (*path, "exists"))
            return form(
                "(NOT EXISTS ({}))" if node.get("not") else "(EXISTS ({}))", query
            )
        if ("field" in node) == ("left" in node):
            self.fail("条件必须指定 field 或 left，且只能指定一个。", path)
        left = self.expr(
            node.get("field", node.get("left")),
            scope,
            outputs,
            ctes,
            (*path, "field" if "field" in node else "left"),
        )
        op = node["op"]
        operands = set(node) - {"field", "left", "op"}
        if op in {"is_null", "is_not_null"}:
            if operands:
                self.fail("空值判断不接受其他操作数。", path)
            return form("({} IS NULL)" if op == "is_null" else "({} IS NOT NULL)", left)
        if op in {"in_query", "not_in_query"}:
            if operands != {"query"}:
                self.fail("子查询集合条件只接受 query 操作数。", path)
            right, names = self.query(node["query"], ctes, scope, (*path, "query"))
            if len(names) != 1:
                self.fail("IN 子查询必须返回一列。", path)
            return form(
                "({} NOT IN ({}))" if op == "not_in_query" else "({} IN ({}))",
                left,
                right,
            )
        if op in {"in", "not_in", "between", "not_between"}:
            if operands != {"values"}:
                self.fail("该条件需要 values 操作数。", path)
            vals = [
                parameter(value, path=(*path, "values", i))
                for i, value in enumerate(node["values"])
            ]
            if "between" in op:
                if len(vals) != 2:
                    self.fail("BETWEEN 必须提供两个值。", (*path, "values"))
                result = (
                    self.binary("ge", left, vals[0])
                    + literal(" AND ")
                    + self.binary("le", left, vals[1])
                )
                return form("(NOT ({}))" if op == "not_between" else "({})", result)
            return form(
                "({})",
                joined(
                    " AND " if op == "not_in" else " OR ",
                    [
                        self.binary("ne" if op == "not_in" else "eq", left, v)
                        for v in vals
                    ],
                ),
            )
        if operands not in ({"value"}, {"right"}):
            self.fail("条件必须提供一个 value 或 right。", path)
        if "value" in node:
            if node["value"] is None and op not in {"is_distinct", "is_not_distinct"}:
                self.fail("空值请使用 is_null/is_not_null。", (*path, "value"))
            right = parameter(node["value"], path=(*path, "value"))
        else:
            right = self.expr(node["right"], scope, outputs, ctes, (*path, "right"))
        if op in {"is_distinct", "is_not_distinct"}:
            return form(
                "({} IS DISTINCT FROM {})"
                if op == "is_distinct"
                else "({} IS NOT DISTINCT FROM {})",
                left,
                right,
            )
        return self.binary(op, left, right)

    def orders(self, orders, scope, outputs, ctes, path, aliases=True):
        parts = []
        for i, order in enumerate(orders):
            if ("field" in order) == ("expr" in order):
                self.fail("排序必须指定 field 或 expr。", (*path, i))
            if "field" in order:
                if order["field"] not in outputs:
                    self.fail(
                        "排序引用了未知输出字段。",
                        (*path, i, "field"),
                        "UNKNOWN_OUTPUT",
                    )
                field = ident(order["field"]) if aliases else outputs[order["field"]]
            else:
                field = self.expr(
                    order["expr"], scope, outputs, ctes, (*path, i, "expr")
                )
            parts.append(
                field
                + literal(
                    " "
                    + order["direction"].upper()
                    + " NULLS "
                    + order.get("nulls", "last").upper()
                )
            )
        return joined(", ", parts)

    def source(self, node, scope, ctes, path, default_schema):
        keys = set(node) & {"table", "query", "cte", "values", "function"}
        if len(keys) != 1:
            self.fail(
                "数据源必须且只能选择 table/query/cte/values/function 中的一种。", path
            )
        alias = node["alias"]
        if alias in scope:
            self.fail("当前查询层的表别名重复。", (*path, "alias"), "DUPLICATE_ALIAS")
        if "table" in node:
            schema, name = node.get("schema", default_schema), node["table"]
            table = self.catalog.get((schema, name), self.catalog.get(name))
            if table is None:
                self.fail(
                    "指定数据库对象不存在或未完成目录校验。",
                    (*path, "table"),
                    "TABLE_NOT_FOUND",
                )
            self.tables.add(schema + "." + name)
            scope[alias] = tuple(table.columns)
            result = (literal("ONLY ") if node.get("only") else literal("")) + ident(
                schema, name
            )
        elif "cte" in node:
            if node["cte"] not in ctes:
                self.fail("引用了尚未定义的 CTE。", (*path, "cte"), "UNKNOWN_CTE")
            result, scope[alias] = ident(node["cte"]), ctes[node["cte"]]
        elif "query" in node:
            nested = dict(node["query"])
            if "schema" in node:
                nested.setdefault("schema", node["schema"])
            fragment, names = self.query(
                nested,
                ctes,
                scope if node.get("lateral") else {},
                (*path, "query"),
            )
            result, scope[alias] = form("({})", fragment), names
        elif "function" in node:
            result = self.expr(node["function"], scope, {}, ctes, (*path, "function"))
            name = node["function"]["call"]
            default = (
                ["key", "value"]
                if name
                in {"json_each", "jsonb_each", "json_each_text", "jsonb_each_text"}
                else (["value"] if "array_elements" in name else [name])
            )
            scope[alias] = tuple(node.get("columns") or default)
            return form(
                "{} AS {} ({})",
                result,
                ident(alias),
                joined(", ", [ident(n) for n in scope[alias]]),
            )
        else:
            fragment, names = self.query(
                {"values": node["values"], "value_columns": node.get("columns", [])},
                ctes,
                {},
                (*path, "values"),
            )
            result, scope[alias] = form("({})", fragment), names
        return form("{} AS {}", result, ident(alias))

    def query(self, query, inherited_ctes=None, outer=None, path=("query",), top=False):
        previous = self.schema
        self.schema = query.get("schema", previous)
        try:
            return self._query(query, inherited_ctes, outer, path, top)
        finally:
            self.schema = previous

    def _query(
        self, query, inherited_ctes=None, outer=None, path=("query",), top=False
    ):
        self.tick(path)
        ctes = dict(inherited_ctes or {})
        prefix = literal("")
        if query.get("with"):
            definitions = []
            declared = set()
            if query.get("recursive"):
                for item in query["with"]:
                    if not item.get("columns"):
                        self.fail("递归 CTE 必须显式声明 columns。", (*path, "with"))
                    ctes[item["name"]] = tuple(item["columns"])
            for i, item in enumerate(query["with"]):
                if item["name"] in declared:
                    self.fail("同一 WITH 的 CTE 名称重复。", (*path, "with", i, "name"))
                declared.add(item["name"])
                body, names = self.query(
                    item["query"], ctes, {}, (*path, "with", i, "query")
                )
                columns = tuple(item.get("columns", names))
                if len(columns) != len(names) or len(set(columns)) != len(columns):
                    self.fail(
                        "CTE 声明列与查询输出数量不一致或名称重复。",
                        (*path, "with", i, "columns"),
                    )
                ctes[item["name"]] = columns
                modifier = (
                    literal("MATERIALIZED ")
                    if item.get("materialized")
                    else (
                        literal("NOT MATERIALIZED ")
                        if item.get("materialized") is False
                        else literal("")
                    )
                )
                definitions.append(
                    form(
                        "{} ({}) AS {}({})",
                        ident(item["name"]),
                        joined(", ", [ident(n) for n in columns]),
                        modifier,
                        body,
                    )
                )
            prefix = (
                literal("WITH RECURSIVE " if query.get("recursive") else "WITH ")
                + joined(", ", definitions)
                + literal(" ")
            )
        if query.get("offset") and not query.get("order_by"):
            self.fail("非零 offset 需要 order_by。", (*path, "offset"))
        if "set" in query:
            if set(query) & {
                "table",
                "from",
                "select",
                "columns",
                "aggregates",
                "joins",
                "where",
                "group_by",
                "having",
                "values",
                "distinct",
                "distinct_on",
            }:
                self.fail("集合查询的筛选/分组需放在分支或外层子查询中。", path)
            branches, names = [], None
            for i, branch in enumerate(query["set"]["queries"]):
                fragment, branch_names = self.query(
                    branch, ctes, outer, (*path, "set", "queries", i)
                )
                if names is not None and len(names) != len(branch_names):
                    self.fail(
                        "集合各分支必须返回相同列数。", (*path, "set", "queries", i)
                    )
                names = names or branch_names
                branches.append(form("({})", fragment))
            op = query["set"]["op"].replace("_", " ").upper()
            body = joined(" " + op + " ", branches)
            scope, outputs = {}, {name: ident(name) for name in names}
        elif "values" in query:
            if set(query) & {
                "table",
                "from",
                "select",
                "columns",
                "aggregates",
                "joins",
                "where",
                "group_by",
                "having",
                "distinct",
                "distinct_on",
            }:
                self.fail("VALUES 的其他查询操作应放在外层子查询中。", path)
            rows = query["values"]
            width = len(rows[0])
            names = tuple(
                query.get("value_columns") or [f"column{i + 1}" for i in range(width)]
            )
            if (
                len(names) != width
                or len(set(names)) != width
                or any(len(r) != width for r in rows)
            ):
                self.fail("VALUES 行宽与列名数量必须一致，列名不能重复。", path)
            values = joined(
                ", ",
                [
                    form(
                        "({})",
                        joined(
                            ", ",
                            [
                                self.expr(
                                    e, outer or {}, {}, ctes, (*path, "values", i, j)
                                )
                                for j, e in enumerate(row)
                            ],
                        ),
                    )
                    for i, row in enumerate(rows)
                ],
            )
            body = form(
                "SELECT * FROM (VALUES {}) AS {} ({})",
                values,
                ident("__values"),
                joined(", ", [ident(n) for n in names]),
            )
            scope, outputs = {}, {name: ident(name) for name in names}
        else:
            scope = dict(outer or {})
            local_aliases = set()
            outputs, projections, legacy_groups = {}, [], []
            source = query.get("from")
            if "table" in query:
                if source is not None:
                    self.fail("table 与 from 不能同时使用。", path)
                source = {
                    k: query[k]
                    for k in ("table", "schema", "alias", "only")
                    if k in query
                }
                source.setdefault("alias", "t")
            from_part = literal("")
            if source:
                scope.pop(
                    source["alias"], None
                )  # Inner aliases may shadow outer aliases.
                local_aliases.add(source["alias"])
                from_part = literal(" FROM ") + self.source(
                    source,
                    scope,
                    ctes,
                    (*path, "from"),
                    query.get("schema", self.schema),
                )
            for i, join in enumerate(query.get("joins", [])):
                if not source:
                    self.fail("JOIN 需要主数据源。", (*path, "joins", i))
                kind = join["kind"]
                if join["alias"] in local_aliases:
                    self.fail(
                        "同一查询层的表别名重复。",
                        (*path, "joins", i, "alias"),
                        "DUPLICATE_ALIAS",
                    )
                local_aliases.add(join["alias"])
                scope.pop(join["alias"], None)
                relation = self.source(
                    join,
                    scope,
                    ctes,
                    (*path, "joins", i),
                    query.get("schema", self.schema),
                )
                from_part += (
                    literal(
                        " "
                        + kind.upper()
                        + " JOIN "
                        + ("LATERAL " if join.get("lateral") else "")
                    )
                    + relation
                )
                if kind == "cross":
                    if "on" in join or "using" in join:
                        self.fail("CROSS JOIN 不接受 ON/USING。", (*path, "joins", i))
                elif ("on" in join) == ("using" in join):
                    self.fail(
                        "JOIN 必须提供 ON 或 USING，且只能提供一种。",
                        (*path, "joins", i),
                    )
                elif "using" in join:
                    from_part += form(
                        " USING ({})", joined(", ", [ident(n) for n in join["using"]])
                    )
                else:
                    condition = join["on"]
                    if isinstance(condition, list):
                        condition = {
                            "combine": "and",
                            "conditions": [
                                {"left": p["left"], "op": "eq", "right": p["right"]}
                                for p in condition
                            ],
                        }
                    from_part += literal(" ON ") + self.condition(
                        condition, scope, outputs, ctes, (*path, "joins", i, "on")
                    )

            def output(expression, name, location):
                if name in outputs:
                    self.fail(
                        "输出名称重复；请指定不同的 as。", location, "DUPLICATE_OUTPUT"
                    )
                outputs[name] = expression
                projections.append(form("{} AS {}", expression, ident(name)))

            for i, column in enumerate(query.get("columns", [])):
                ref = {k: column[k] for k in ("source", "column")}
                if ref["column"] == "*":
                    if column.get("as") or ref["source"] not in scope:
                        self.fail(
                            "星号展开需要有效 source，且不能设置 as。",
                            (*path, "columns", i),
                        )
                    for name in scope[ref["source"]]:
                        expression = ident(ref["source"], name)
                        output(expression, name, (*path, "columns", i))
                        legacy_groups.append(expression)
                else:
                    expression = self.expr(
                        ref, scope, outputs, ctes, (*path, "columns", i)
                    )
                    output(
                        expression,
                        column.get("as", column["column"]),
                        (*path, "columns", i, "as"),
                    )
                    legacy_groups.append(expression)
            for i, item in enumerate(query.get("select", [])):
                output(
                    self.expr(
                        item["expr"], scope, outputs, ctes, (*path, "select", i, "expr")
                    ),
                    item["as"],
                    (*path, "select", i, "as"),
                )
            for i, item in enumerate(query.get("aggregates", [])):
                if "field" in item and "args" in item:
                    self.fail(
                        "aggregate 的 field 和 args 不能同时指定。",
                        (*path, "aggregates", i),
                    )
                node = {
                    "aggregate": item["function"],
                    "args": item.get(
                        "args", [item["field"]] if "field" in item else []
                    ),
                }
                node.update(
                    {
                        k: item[k]
                        for k in ("distinct", "filter", "order_by")
                        if k in item
                    }
                )
                output(
                    self.expr(node, scope, outputs, ctes, (*path, "aggregates", i)),
                    item["as"],
                    (*path, "aggregates", i, "as"),
                )
            if not projections:
                self.fail(
                    "必须指定 columns/select/aggregates。", path, "MISSING_PROJECTION"
                )
            distinct = literal("DISTINCT ") if query.get("distinct") else literal("")
            if query.get("distinct_on"):
                if query.get("distinct"):
                    self.fail("distinct 与 distinct_on 不能同时指定。", path)
                distinct = form(
                    "DISTINCT ON ({}) ",
                    joined(
                        ", ",
                        [
                            self.expr(
                                e, scope, outputs, ctes, (*path, "distinct_on", i)
                            )
                            for i, e in enumerate(query["distinct_on"])
                        ],
                    ),
                )
            body = literal("SELECT ") + distinct + joined(", ", projections) + from_part
            if "where" in query:
                body += literal(" WHERE ") + self.condition(
                    query["where"], scope, {}, ctes, (*path, "where")
                )
            groups = (
                [
                    self.expr(e, scope, outputs, ctes, (*path, "group_by", i))
                    for i, e in enumerate(query["group_by"])
                ]
                if "group_by" in query
                else (legacy_groups if query.get("aggregates") else [])
            )
            if groups:
                body += literal(" GROUP BY ") + joined(", ", groups)
            if "having" in query:
                body += literal(" HAVING ") + self.condition(
                    query["having"], scope, outputs, ctes, (*path, "having")
                )
            names = tuple(outputs)
        if query.get("order_by"):
            body += literal(" ORDER BY ") + self.orders(
                query["order_by"], scope, outputs, ctes, (*path, "order_by")
            )
        if "limit" in query:
            if type(query["limit"]) is not int:
                self.fail("limit 必须是整数。", (*path, "limit"))
            body += literal(" LIMIT ") + parameter(
                query["limit"] + (1 if top else 0), path=(*path, "limit")
            )
        if "offset" in query or top:
            if type(query.get("offset", 0)) is not int:
                self.fail("offset 必须是整数。", (*path, "offset"))
            body += literal(" OFFSET ") + parameter(
                query.get("offset", 0), path=(*path, "offset")
            )
        return prefix + body, tuple(names)


def compile_query(
    schema: str, query: dict, catalog: dict, max_cell_chars=4000
) -> BuiltQuery:
    validate_query(query)
    with error_stage("compile"):
        compiler = Compiler(schema, catalog)
        fragment, names = compiler.query(query, top=True)
    return BuiltQuery(
        fragment.text,
        fragment.params,
        query["limit"],
        names,
        tuple(sorted(compiler.tables)),
        compiler.aggregate,
        fragment.paths,
    )
