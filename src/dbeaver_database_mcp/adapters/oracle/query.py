"""Compile the shared JSON language into Oracle SQL with named binds."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ...core.catalog import Table
from ...core.errors import QueryRejected
from ...core.validation import path_string, validate_query

FUNCTIONS = {
    "abs": "ABS",
    "ceil": "CEIL",
    "ceiling": "CEIL",
    "floor": "FLOOR",
    "round": "ROUND",
    "trunc": "TRUNC",
    "sqrt": "SQRT",
    "power": "POWER",
    "mod": "MOD",
    "sign": "SIGN",
    "exp": "EXP",
    "ln": "LN",
    "log": "LOG",
    "lower": "LOWER",
    "upper": "UPPER",
    "length": "LENGTH",
    "char_length": "LENGTH",
    "octet_length": "LENGTHB",
    "ltrim": "LTRIM",
    "rtrim": "RTRIM",
    "btrim": "TRIM",
    "substring": "SUBSTR",
    "substr": "SUBSTR",
    "replace": "REPLACE",
    "concat": "CONCAT",
    "strpos": "INSTR",
    "coalesce": "COALESCE",
    "nullif": "NULLIF",
    "greatest": "GREATEST",
    "least": "LEAST",
    "to_char": "TO_CHAR",
    "to_date": "TO_DATE",
    "to_timestamp": "TO_TIMESTAMP",
    "regexp_replace": "REGEXP_REPLACE",
}
CONTEXT = {
    "current_date": "CURRENT_DATE",
    "current_timestamp": "CURRENT_TIMESTAMP",
    "current_user": "SYS_CONTEXT('USERENV','CURRENT_USER')",
    "session_user": "SYS_CONTEXT('USERENV','SESSION_USER')",
}
AGGREGATES = {
    name: name.upper()
    for name in (
        "count",
        "sum",
        "avg",
        "min",
        "max",
        "stddev",
        "stddev_pop",
        "stddev_samp",
        "variance",
        "var_pop",
        "var_samp",
        "corr",
        "covar_pop",
        "covar_samp",
    )
}
WINDOWS = {
    name: name.upper()
    for name in (
        "row_number",
        "rank",
        "dense_rank",
        "percent_rank",
        "cume_dist",
        "ntile",
        "lag",
        "lead",
        "first_value",
        "last_value",
        "nth_value",
        "count",
        "sum",
        "avg",
        "min",
        "max",
    )
}
TYPES = {
    "text": "VARCHAR2(4000)",
    "varchar": "VARCHAR2(4000)",
    "numeric": "NUMBER",
    "int2": "NUMBER(5)",
    "int4": "NUMBER(10)",
    "int8": "NUMBER(19)",
    "float4": "BINARY_FLOAT",
    "float8": "BINARY_DOUBLE",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
    "timestamptz": "TIMESTAMP WITH TIME ZONE",
}
COMPARISONS = {"eq": "=", "ne": "<>", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
OPERATORS = {"add": "+", "sub": "-", "mul": "*", "div": "/", "concat": "||"}
CONDITIONS = list(COMPARISONS) + [
    "in",
    "not_in",
    "between",
    "not_between",
    "like",
    "not_like",
    "ilike",
    "not_ilike",
    "regex",
    "iregex",
    "not_regex",
    "not_iregex",
    "is_null",
    "is_not_null",
    "is_distinct",
    "is_not_distinct",
    "in_query",
    "not_in_query",
]


def identifier(name: str, max_bytes: int = 30) -> str:
    if (
        not isinstance(name, str)
        or not name
        or "\x00" in name
        or len(name.encode("utf-8")) > max_bytes
    ):
        raise QueryRejected(
            "Oracle 对象名称无效或超过当前版本长度限制。", code="INVALID_IDENTIFIER"
        )
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class BuiltQuery:
    statement: str
    parameters: dict[str, Any]
    outputs: tuple[str, ...]
    limit: int
    relations: tuple[tuple[str, str], ...]

    def summary(self) -> dict:
        return {
            "relations": [{"schema": s, "table": t} for s, t in self.relations],
            "columns": list(self.outputs),
            "limit": self.limit,
            "parameter_count": len(self.parameters),
            "raw_sql": False,
        }


class Compiler:
    def __init__(
        self,
        schema: str,
        catalog: dict[tuple[str, str], Table],
        max_identifier_bytes: int,
    ):
        self.schema, self.catalog = schema, catalog
        self.max_identifier_bytes = max_identifier_bytes
        self.parameters: dict[str, Any] = {}
        self.relations: set[tuple[str, str]] = set()
        self.nodes = 0

    def ident(self, name: str) -> str:
        return identifier(name, self.max_identifier_bytes)

    def fail(self, path, code="UNSUPPORTED_CAPABILITY"):
        raise QueryRejected(
            "Oracle 不支持该查询结构或参数组合。",
            code=code,
            path=path_string(path),
            stage="compile",
            hint="调用 get_query_capabilities 查看 Oracle 查询能力。",
        )

    def tick(self, path):
        self.nodes += 1
        if self.nodes > 4000 or len(path) > 64:
            self.fail(path, "QUERY_TOO_COMPLEX")

    def bind(self, value, type_name=None, path=()):
        if type(value) is bool or isinstance(value, (list, dict)):
            self.fail(path, "UNSUPPORTED_TYPE")
        if type_name and type_name not in TYPES:
            self.fail(path, "UNSUPPORTED_TYPE")
        if type(value) is float:
            value = Decimal(str(value))
        if isinstance(value, str) and type_name in {"date", "timestamp", "timestamptz"}:
            try:
                value = (
                    date.fromisoformat(value)
                    if type_name == "date"
                    else datetime.fromisoformat(value)
                )
            except ValueError:
                self.fail(path, "PARAMETER_ADAPTATION_FAILED")
        name = f"p{len(self.parameters) + 1}"
        self.parameters[name] = value
        result = ":" + name
        return f"CAST({result} AS {TYPES[type_name]})" if type_name else result

    def expr(self, node, scope, outputs, ctes, path):
        self.tick(path)
        if "source" in node:
            source, column = node["source"], node["column"]
            if source not in scope:
                self.fail((*path, "source"), "UNKNOWN_SOURCE")
            if column not in scope[source]:
                self.fail((*path, "column"), "COLUMN_NOT_FOUND")
            return self.ident(source) + "." + self.ident(column)
        if "value" in node:
            return self.bind(node["value"], node.get("type"), (*path, "value"))
        if "output" in node:
            if node["output"] not in outputs:
                self.fail(path, "UNKNOWN_OUTPUT")
            return outputs[node["output"]]
        if "call" in node:
            name = node["call"]
            args = [
                self.expr(a, scope, outputs, ctes, (*path, "args", i))
                for i, a in enumerate(node.get("args", []))
            ]
            if name in CONTEXT and not args:
                return CONTEXT[name]
            if name not in FUNCTIONS:
                self.fail((*path, "call"), "UNSUPPORTED_FUNCTION")
            return FUNCTIONS[name] + "(" + ", ".join(args) + ")"
        if "op" in node:
            left = self.expr(node["left"], scope, outputs, ctes, (*path, "left"))
            right = self.expr(node["right"], scope, outputs, ctes, (*path, "right"))
            if node["op"] == "mod":
                return f"MOD({left}, {right})"
            if node["op"] not in OPERATORS:
                self.fail((*path, "op"))
            return f"({left} {OPERATORS[node['op']]} {right})"
        if "cast" in node:
            if node["cast"] not in TYPES:
                self.fail((*path, "cast"), "UNSUPPORTED_CAST")
            inner = self.expr(node["expr"], scope, outputs, ctes, (*path, "expr"))
            return f"CAST({inner} AS {TYPES[node['cast']]})"
        if "case" in node:
            clauses = ["CASE"]
            for i, branch in enumerate(node["case"]):
                condition = self.condition(
                    branch["when"], scope, outputs, ctes, (*path, "case", i, "when")
                )
                value = self.expr(
                    branch["then"], scope, outputs, ctes, (*path, "case", i, "then")
                )
                clauses.append(f"WHEN {condition} THEN {value}")
            if "else" in node:
                clauses.append(
                    "ELSE "
                    + self.expr(node["else"], scope, outputs, ctes, (*path, "else"))
                )
            return " ".join(clauses) + " END"
        if "aggregate" in node or "window" in node:
            key = "aggregate" if "aggregate" in node else "window"
            name = node[key]
            allowed = AGGREGATES if key == "aggregate" else WINDOWS
            if (
                name not in allowed
                or node.get("filter")
                or node.get("within_group")
                or (key == "aggregate" and node.get("order_by"))
            ):
                self.fail(path, "UNSUPPORTED_AGGREGATE")
            args = [
                self.expr(a, scope, outputs, ctes, (*path, "args", i))
                for i, a in enumerate(node.get("args", []))
            ]
            if not args and name == "count":
                args = ["*"]
            if node.get("distinct") and (len(args) != 1 or args == ["*"]):
                self.fail(path)
            result = (
                allowed[name]
                + "("
                + ("DISTINCT " if node.get("distinct") else "")
                + ", ".join(args)
                + ")"
            )
            if key == "window":
                parts = []
                if node.get("partition_by"):
                    parts.append(
                        "PARTITION BY "
                        + ", ".join(
                            self.expr(
                                e, scope, outputs, ctes, (*path, "partition_by", i)
                            )
                            for i, e in enumerate(node["partition_by"])
                        )
                    )
                if node.get("order_by"):
                    parts.append(
                        "ORDER BY "
                        + self.orders(
                            node["order_by"], scope, outputs, ctes, (*path, "order_by")
                        )
                    )
                if "frame" in node:
                    frame = node["frame"]
                    if frame["unit"] not in {"rows", "range"}:
                        self.fail((*path, "frame"))
                    parts.append(
                        frame["unit"].upper()
                        + " BETWEEN "
                        + self.bound(frame["start"], path)
                        + " AND "
                        + self.bound(frame["end"], path)
                    )
                result += " OVER (" + " ".join(parts) + ")"
            return result
        if "subquery" in node:
            result, names = self.query(
                node["subquery"], ctes, scope, (*path, "subquery")
            )
            if len(names) != 1:
                self.fail(path)
            return "(" + result + ")"
        self.fail(path)

    def bound(self, node, path):
        kind = node["kind"]
        if kind in {"preceding", "following"} and "offset" in node:
            return self.bind(node["offset"], path=path) + " " + kind.upper()
        if (
            kind in {"unbounded_preceding", "current_row", "unbounded_following"}
            and "offset" not in node
        ):
            return kind.replace("_", " ").upper()
        self.fail(path)

    def condition(self, node, scope, outputs, ctes, path):
        self.tick(path)
        if "combine" in node:
            terms = [
                self.condition(c, scope, outputs, ctes, (*path, "conditions", i))
                for i, c in enumerate(node["conditions"])
            ]
            if node["combine"] == "not":
                if len(terms) != 1:
                    self.fail(path)
                return "(NOT " + terms[0] + ")"
            return "(" + (" " + node["combine"].upper() + " ").join(terms) + ")"
        if "exists" in node:
            result, _ = self.query(node["exists"], ctes, scope, (*path, "exists"))
            return ("NOT " if node.get("not") else "") + "EXISTS (" + result + ")"
        op = node["op"]
        if ("field" in node) == ("left" in node):
            self.fail(path, "INVALID_QUERY")
        left = self.expr(
            node.get("field", node.get("left")), scope, outputs, ctes, (*path, "field")
        )
        right_keys = set(node) & {"value", "values", "right", "query"}
        if op in {"is_null", "is_not_null"}:
            if right_keys:
                self.fail(path, "INVALID_QUERY")
            return f"({left} IS {'NOT ' if op == 'is_not_null' else ''}NULL)"
        if len(right_keys) != 1:
            self.fail(path, "INVALID_QUERY")
        if op in {"in_query", "not_in_query"} and "query" in node:
            right, names = self.query(node["query"], ctes, scope, (*path, "query"))
            if len(names) != 1:
                self.fail(path)
            return f"({left} {'NOT ' if op == 'not_in_query' else ''}IN ({right}))"
        if op in {"in", "not_in"} and "values" in node:
            values = ", ".join(
                self.bind(v, path=(*path, "values", i))
                for i, v in enumerate(node["values"])
            )
            return f"({left} {'NOT ' if op == 'not_in' else ''}IN ({values}))"
        if (
            op in {"between", "not_between"}
            and "values" in node
            and len(node["values"]) == 2
        ):
            a, b = [
                self.bind(v, path=(*path, "values", i))
                for i, v in enumerate(node["values"])
            ]
            return (
                f"({left} {'NOT ' if op == 'not_between' else ''}BETWEEN {a} AND {b})"
            )
        if not right_keys <= {"value", "right"}:
            self.fail(path)
        right = (
            self.expr(node["right"], scope, outputs, ctes, (*path, "right"))
            if "right" in node
            else self.bind(node["value"], path=(*path, "value"))
        )
        if op in COMPARISONS:
            return f"({left} {COMPARISONS[op]} {right})"
        if op in {"like", "not_like", "ilike", "not_ilike"}:
            if "ilike" in op:
                left, right = f"LOWER({left})", f"LOWER({right})"
            return f"({left} {'NOT ' if op.startswith('not_') else ''}LIKE {right})"
        if op in {"regex", "iregex", "not_regex", "not_iregex"}:
            flag = self.bind("i" if "iregex" in op else "c", path=path)
            return f"({'NOT ' if op.startswith('not_') else ''}REGEXP_LIKE({left}, {right}, {flag}))"
        if op in {"is_distinct", "is_not_distinct"}:
            # DECODE considers two NULLs equal and is available on Oracle 12.1.
            return (
                f"(DECODE({left}, {right}, 0, 1) = {1 if op == 'is_distinct' else 0})"
            )
        self.fail((*path, "op"))

    def orders(self, orders, scope, outputs, ctes, path):
        result = []
        for i, item in enumerate(orders):
            if ("expr" in item) == ("field" in item):
                self.fail((*path, i), "INVALID_QUERY")
            expression = item.get("expr", {"output": item.get("field")})
            part = (
                self.expr(expression, scope, outputs, ctes, (*path, i))
                + " "
                + item["direction"].upper()
            )
            if "nulls" in item:
                part += " NULLS " + item["nulls"].upper()
            result.append(part)
        return ", ".join(result)

    def source(self, node, scope, ctes, path):
        kinds = set(node) & {"table", "query", "cte", "values", "function"}
        if (
            len(kinds) != 1
            or node.get("only")
            or node.get("lateral")
            or node.get("columns")
        ):
            self.fail(path)
        alias = node["alias"]
        if alias in scope:
            self.fail((*path, "alias"), "DUPLICATE_ALIAS")
        if "table" in node:
            key = (node.get("schema", self.schema), node["table"])
            table = self.catalog.get(key)
            if table is None:
                self.fail(path, "TABLE_NOT_FOUND")
            self.relations.add(key)
            names = tuple(c for c in table.columns if c in table.selectable)
            sql = self.ident(key[0]) + "." + self.ident(key[1])
        elif "query" in node:
            sql, names = self.query(node["query"], ctes, {}, (*path, "query"))
            sql = "(" + sql + ")"
        elif "cte" in node and node["cte"] in ctes:
            names, sql = ctes[node["cte"]], self.ident(node["cte"])
        else:
            self.fail(path)
        scope[alias] = names
        return sql + " " + self.ident(alias)

    def query(self, query, inherited_ctes=None, outer=None, path=("query",), top=False):
        self.tick(path)
        previous = self.schema
        self.schema = query.get("schema", previous)
        try:
            return self._query(query, inherited_ctes, outer, path, top)
        finally:
            self.schema = previous

    def _query(self, query, inherited_ctes, outer, path, top):
        if query.get("recursive") or query.get("distinct_on") or query.get("only"):
            self.fail(path)
        ctes, prefix = dict(inherited_ctes or {}), ""
        if query.get("with"):
            definitions, declared = [], set()
            for i, item in enumerate(query["with"]):
                if item["name"] in declared or "materialized" in item:
                    self.fail((*path, "with", i))
                declared.add(item["name"])
                body, names = self.query(
                    item["query"], ctes, {}, (*path, "with", i, "query")
                )
                columns = tuple(item.get("columns", names))
                if len(columns) != len(names) or len(set(columns)) != len(columns):
                    self.fail((*path, "with", i, "columns"))
                ctes[item["name"]] = columns
                definitions.append(
                    self.ident(item["name"])
                    + " ("
                    + ", ".join(self.ident(c) for c in columns)
                    + ") AS ("
                    + body
                    + ")"
                )
            prefix = "WITH " + ", ".join(definitions) + " "
        if query.get("offset") and not query.get("order_by"):
            self.fail((*path, "offset"), "INVALID_QUERY")
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
            }:
                self.fail(path, "INVALID_QUERY")
            operations = {
                "union": "UNION",
                "union_all": "UNION ALL",
                "intersect": "INTERSECT",
                "except": "MINUS",
            }
            if query["set"]["op"] not in operations:
                self.fail((*path, "set", "op"))
            branches, names = [], None
            for i, branch in enumerate(query["set"]["queries"]):
                sql, columns = self.query(
                    branch, ctes, outer, (*path, "set", "queries", i)
                )
                if names is not None and len(names) != len(columns):
                    self.fail(path, "INVALID_QUERY")
                names = names or columns
                branches.append("SELECT * FROM (" + sql + ")")
            body = (" " + operations[query["set"]["op"]] + " ").join(branches)
            scope, outputs = {}, {n: self.ident(n) for n in names}
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
            }:
                self.fail(path)
            width = len(query["values"][0])
            names = tuple(
                query.get("value_columns", [f"column{i + 1}" for i in range(width)])
            )
            if (
                len(names) != width
                or len(set(names)) != width
                or any(len(r) != width for r in query["values"])
            ):
                self.fail(path, "INVALID_QUERY")
            branches = []
            for i, row in enumerate(query["values"]):
                projections = [
                    self.expr(e, outer or {}, {}, ctes, (*path, "values", i, j))
                    + " AS "
                    + self.ident(names[j])
                    for j, e in enumerate(row)
                ]
                branches.append("SELECT " + ", ".join(projections) + " FROM SYS.DUAL")
            body = " UNION ALL ".join(branches)
            scope, outputs = {}, {n: self.ident(n) for n in names}
        else:
            scope, outputs, projections = dict(outer or {}), {}, []
            source = query.get("from")
            if "table" in query:
                if source:
                    self.fail(path, "INVALID_QUERY")
                source = {
                    k: query[k]
                    for k in ("table", "schema", "alias", "only")
                    if k in query
                }
                source.setdefault("alias", "t")
            local_aliases = set()
            if source:
                scope.pop(source["alias"], None)
                local_aliases.add(source["alias"])
                from_sql = " FROM " + self.source(source, scope, ctes, (*path, "from"))
            else:
                from_sql = " FROM SYS.DUAL"
            for i, join in enumerate(query.get("joins", [])):
                if not source or join["alias"] in local_aliases:
                    self.fail((*path, "joins", i), "INVALID_QUERY")
                local_aliases.add(join["alias"])
                scope.pop(join["alias"], None)
                relation = self.source(join, scope, ctes, (*path, "joins", i))
                from_sql += " " + join["kind"].upper() + " JOIN " + relation
                if "on" in join and "using" in join:
                    self.fail((*path, "joins", i))
                if join["kind"] == "cross":
                    if "on" in join or "using" in join:
                        self.fail((*path, "joins", i))
                elif "on" in join:
                    on = join["on"]
                    if isinstance(on, list):
                        terms = [
                            self.expr(
                                pair["left"],
                                scope,
                                outputs,
                                ctes,
                                (*path, "joins", i, "on", j),
                            )
                            + " = "
                            + self.expr(
                                pair["right"],
                                scope,
                                outputs,
                                ctes,
                                (*path, "joins", i, "on", j),
                            )
                            for j, pair in enumerate(on)
                        ]
                        predicate = " AND ".join(terms)
                    else:
                        predicate = self.condition(
                            on, scope, outputs, ctes, (*path, "joins", i, "on")
                        )
                    from_sql += " ON " + predicate
                else:
                    self.fail((*path, "joins", i))
            for i, column in enumerate(query.get("columns", [])):
                expression = self.expr(
                    {k: column[k] for k in ("source", "column")},
                    scope,
                    outputs,
                    ctes,
                    (*path, "columns", i),
                )
                name = column.get("as", column["column"])
                if name in outputs:
                    self.fail((*path, "columns", i), "DUPLICATE_OUTPUT")
                outputs[name] = expression
                projections.append(expression + " AS " + self.ident(name))
            for i, item in enumerate(query.get("select", [])):
                if item["as"] in outputs:
                    self.fail((*path, "select", i), "DUPLICATE_OUTPUT")
                expression = self.expr(
                    item["expr"], scope, outputs, ctes, (*path, "select", i, "expr")
                )
                outputs[item["as"]] = expression
                projections.append(expression + " AS " + self.ident(item["as"]))
            for i, item in enumerate(query.get("aggregates", [])):
                if item["as"] in outputs or ("field" in item and "args" in item):
                    self.fail((*path, "aggregates", i), "INVALID_QUERY")
                args = item.get("args", [item["field"]] if "field" in item else [])
                expression = self.expr(
                    {
                        "aggregate": item["function"],
                        "args": args,
                        **{
                            k: item[k]
                            for k in ("distinct", "filter", "order_by")
                            if k in item
                        },
                    },
                    scope,
                    outputs,
                    ctes,
                    (*path, "aggregates", i),
                )
                outputs[item["as"]] = expression
                projections.append(expression + " AS " + self.ident(item["as"]))
            if not projections:
                self.fail(path, "INVALID_QUERY")
            names = tuple(outputs)
            body = (
                "SELECT "
                + ("DISTINCT " if query.get("distinct") else "")
                + ", ".join(projections)
                + from_sql
            )
            if "where" in query:
                body += " WHERE " + self.condition(
                    query["where"], scope, {}, ctes, (*path, "where")
                )
            if query.get("group_by"):
                body += " GROUP BY " + ", ".join(
                    self.expr(e, scope, outputs, ctes, (*path, "group_by", i))
                    for i, e in enumerate(query["group_by"])
                )
            if "having" in query:
                body += " HAVING " + self.condition(
                    query["having"], scope, outputs, ctes, (*path, "having")
                )
        if query.get("order_by"):
            body += " ORDER BY " + self.orders(
                query["order_by"], scope, outputs, ctes, (*path, "order_by")
            )
        if "limit" in query:
            if (
                type(query["limit"]) is not int
                or type(query.get("offset", 0)) is not int
            ):
                self.fail((*path, "limit"), "INVALID_PARAMETER")
            offset = self.bind(query.get("offset", 0), path=(*path, "offset"))
            count = self.bind(query["limit"] + (1 if top else 0), path=(*path, "limit"))
            body += f" OFFSET {offset} ROWS FETCH NEXT {count} ROWS ONLY"
        elif query.get("offset"):
            self.fail((*path, "offset"))
        return prefix + body, names


def compile_query(schema, query, catalog, max_identifier_bytes=30):
    from ...core.validation import reject_qualified_targets

    validate_query(query)
    reject_qualified_targets(query)
    compiler = Compiler(schema, catalog, max_identifier_bytes)
    statement, names = compiler.query(query, top=True)
    return BuiltQuery(
        statement,
        compiler.parameters,
        names,
        query["limit"],
        tuple(sorted(compiler.relations)),
    )
