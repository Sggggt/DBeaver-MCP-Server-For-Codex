"""Compile a declared subset of the shared JSON language directly to T-SQL."""

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from uuid import UUID

from ...core.catalog import Table
from ...core.errors import QueryRejected
from ...core.validation import validate_query

FUNCTIONS = {
    "abs": ("ABS", 1, 1),
    "ceil": ("CEILING", 1, 1),
    "ceiling": ("CEILING", 1, 1),
    "floor": ("FLOOR", 1, 1),
    "round": ("ROUND", 2, 3),
    "sqrt": ("SQRT", 1, 1),
    "power": ("POWER", 2, 2),
    "sign": ("SIGN", 1, 1),
    "exp": ("EXP", 1, 1),
    "ln": ("LOG", 1, 1),
    "lower": ("LOWER", 1, 1),
    "upper": ("UPPER", 1, 1),
    "length": ("LEN", 1, 1),
    "char_length": ("LEN", 1, 1),
    "octet_length": ("DATALENGTH", 1, 1),
    "ltrim": ("LTRIM", 1, 1),
    "rtrim": ("RTRIM", 1, 1),
    "substring": ("SUBSTRING", 3, 3),
    "substr": ("SUBSTRING", 3, 3),
    "replace": ("REPLACE", 3, 3),
    "left": ("LEFT", 2, 2),
    "right": ("RIGHT", 2, 2),
    "reverse": ("REVERSE", 1, 1),
    "coalesce": ("COALESCE", 2, 20),
    "nullif": ("NULLIF", 2, 2),
}
CONTEXT = {
    "current_timestamp": "CURRENT_TIMESTAMP",
    "current_date": "CAST(CURRENT_TIMESTAMP AS date)",
}
AGGREGATES = {
    "count": "COUNT_BIG",
    "sum": "SUM",
    "avg": "AVG",
    "min": "MIN",
    "max": "MAX",
}
TYPES = {
    "text": "nvarchar(max)",
    "varchar": "nvarchar(max)",
    "bool": "bit",
    "int2": "smallint",
    "int4": "int",
    "int8": "bigint",
    "numeric": "decimal(38,10)",
    "float4": "real",
    "float8": "float",
    "date": "date",
    "time": "time",
    "timestamp": "datetime2",
    "timestamptz": "datetimeoffset",
    "uuid": "uniqueidentifier",
}
COMPARISONS = {"eq": "=", "ne": "<>", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
CONDITIONS = list(COMPARISONS) + [
    "like",
    "not_like",
    "in",
    "not_in",
    "between",
    "not_between",
    "is_null",
    "is_not_null",
]
OPERATORS = {"add": "+", "sub": "-", "mul": "*", "div": "/", "mod": "%", "concat": "+"}
WINDOWS = {
    name: (name.upper(), low, high, version)
    for name, low, high, version in [
        ("row_number", 0, 0, 10),
        ("rank", 0, 0, 10),
        ("dense_rank", 0, 0, 10),
        ("ntile", 1, 1, 10),
        ("percent_rank", 0, 0, 11),
        ("cume_dist", 0, 0, 11),
        ("lag", 1, 3, 11),
        ("lead", 1, 3, 11),
        ("first_value", 1, 1, 11),
        ("last_value", 1, 1, 11),
    ]
}
CONDITIONS += ["in_query", "not_in_query"]


def identifier(name):
    if (
        not isinstance(name, str)
        or not name
        or len(name.encode("utf-16-le")) > 256
        or "\x00" in name
        or "%" in name
    ):
        raise QueryRejected("SQL Server 标识符无效。", code="INVALID_IDENTIFIER")
    return "[" + name.replace("]", "]]") + "]"


@dataclass(frozen=True)
class BuiltQuery:
    statement: str
    parameters: dict
    outputs: tuple
    limit: int
    relations: tuple
    ordering: tuple
    prefix: str = ""

    @property
    def body(self):
        return self.statement[len(self.prefix) :]

    def wrap(self, before, after):
        # T-SQL WITH is statement-level and cannot occur inside a derived table.
        return self.prefix + before + self.body + after

    def summary(self):
        return {
            "relations": [
                dict(zip(("server", "database", "schema", "table")[-len(r) :], r))
                for r in self.relations
            ],
            "columns": list(self.outputs),
            "limit": self.limit,
            "parameter_count": len(self.parameters),
            "raw_sql": False,
        }


class Compiler:
    def __init__(
        self, schema, catalog, major_version, *, parent=None, correlated=False
    ):
        self.schema, self.catalog, self.major_version = schema, catalog, major_version
        self.root = parent.root if parent else self
        self.parameters = parent.parameters if parent else {}
        self.relations = parent.relations if parent else set()
        self.sources, self.outputs = {}, {}
        if correlated:
            shadowed = {name.casefold() for name in parent.sources}
            self.outer = {
                name: table
                for name, table in parent.outer.items()
                if name.casefold() not in shadowed
            }
            self.outer.update(parent.sources)
        else:
            self.outer = {}
        self.ctes = dict(parent.ctes) if parent else {}
        self.depth = parent.depth + 1 if parent else 0
        if self.depth > 16:
            self.fail("query_depth", "QUERY_TOO_COMPLEX")
        if not parent:
            self.nodes, self.definitions = 0, []

    def child(self, query, *, correlated=False):
        compiler = Compiler(
            self.schema,
            self.catalog,
            self.major_version,
            parent=self,
            correlated=correlated,
        )
        return compiler.build(query, nested=True)

    def virtual(self, names):
        return Table(0, dict.fromkeys(names), frozenset(names))

    def fail(self, feature, code="UNSUPPORTED_CAPABILITY"):
        raise QueryRejected(
            "SQL Server 不支持该查询结构或参数组合。",
            code=code,
            stage="compile",
            details={"feature": feature},
            hint="调用 get_query_capabilities 查看 SQL Server 能力。",
        )

    def bind(self, value, type_name=None):
        if isinstance(value, (list, dict)) or (type_name and type_name not in TYPES):
            self.fail("literal_type", "UNSUPPORTED_TYPE")
        try:
            if value is not None:
                if type_name == "numeric":
                    value = Decimal(str(value))
                    if not value.is_finite() or value.as_tuple().exponent < -10:
                        self.fail("numeric_scale_exceeds_10", "UNSUPPORTED_TYPE")
                elif type_name in {"date", "time", "timestamp", "timestamptz", "uuid"}:
                    parser = {
                        "date": date.fromisoformat,
                        "time": time.fromisoformat,
                        "timestamp": datetime.fromisoformat,
                        "timestamptz": datetime.fromisoformat,
                        "uuid": UUID,
                    }[type_name]
                    value = parser(value)
                elif type_name in {"int2", "int4", "int8"}:
                    if type(value) is bool or str(int(value)) != str(value):
                        self.fail("integer_literal", "UNSUPPORTED_TYPE")
                    value = int(value)
        except (ValueError, TypeError, InvalidOperation):
            self.fail("literal_type", "UNSUPPORTED_TYPE")
        if type(value) is int and not -(2**63) <= value < 2**63:
            self.fail("integer_range", "UNSUPPORTED_TYPE")
        if len(self.parameters) >= 2000:
            self.fail("parameter_count", "QUERY_TOO_COMPLEX")
        key = f"p{len(self.parameters)}"
        self.parameters[key] = value
        sql = f"%({key})s"
        return f"CAST({sql} AS {TYPES[type_name]})" if type_name else sql

    def source(self, node):
        if (
            set(node)
            - {
                "table",
                "database",
                "server",
                "schema",
                "alias",
                "kind",
                "on",
                "query",
                "cte",
            }
            or sum(k in node for k in ("table", "query", "cte")) != 1
        ):
            self.fail("table_source")
        alias = node["alias"]
        identifier(alias)
        if alias.casefold() in {a.casefold() for a in self.sources}:
            self.fail("duplicate_alias", "INVALID_IDENTIFIER")
        if "table" not in node:
            if any(k in node for k in ("schema", "database", "server")):
                self.fail("virtual_source_identity", "INVALID_PARAMETER")
            if "query" in node:
                child = self.child(node["query"])
                sql, names = "(" + child.body + ")", child.outputs
            else:
                if node["cte"] not in self.ctes:
                    self.fail("unknown_or_recursive_cte", "TABLE_NOT_FOUND")
                sql, names = self.ctes[node["cte"]]
                sql = identifier(sql)
            self.sources[alias] = self.virtual(names)
            return sql + " AS " + identifier(alias)
        schema, table, alias = (
            node.get("schema", self.schema),
            node["table"],
            node["alias"],
        )
        if "server" in node and "database" not in node:
            self.fail("remote_database_required", "MISSING_PARAMETER")
        relation = tuple(node[k] for k in ("server", "database") if k in node) + (
            schema,
            table,
        )
        context = self.catalog.get(relation)
        if context is None:
            self.fail("table", "TABLE_NOT_FOUND")
        self.sources[alias] = context
        self.relations.add(relation)
        return ".".join(identifier(n) for n in relation) + " AS " + identifier(alias)

    def expr(self, node, *, outputs=False):
        self.root.nodes += 1
        if self.root.nodes > 4000:
            self.fail("expression_count", "QUERY_TOO_COMPLEX")
        if "column" in node:
            source, column = node["source"], node["column"]
            # A case-insensitive SQL Server collation can bind an outer-looking
            # name to a differently cased inner alias. Never audit one relation
            # while the generated SQL resolves another.
            if source not in self.sources and any(
                name.casefold() == source.casefold() for name in self.sources
            ):
                self.fail("shadowed_source_alias", "COLUMN_NOT_FOUND")
            table = self.sources.get(source, self.outer.get(source))
            if table is None or column not in table.columns:
                self.fail("column", "COLUMN_NOT_FOUND")
            if column not in table.selectable:
                self.fail("column", "READ_PERMISSION_DENIED")
            return f"{identifier(source)}.{identifier(column)}"
        if "output" in node:
            if not outputs or node["output"] not in self.outputs:
                self.fail("output_alias", "COLUMN_NOT_FOUND")
            # T-SQL HAVING/GROUP BY cannot refer to projection aliases. Expand
            # the already-compiled expression instead of emitting the alias.
            return self.outputs[node["output"]]
        if "value" in node:
            return self.bind(node["value"], node.get("type"))
        if "subquery" in node:
            child = self.child(node["subquery"], correlated=True)
            if len(child.outputs) != 1:
                self.fail("scalar_subquery_columns", "INVALID_PARAMETER")
            return "(" + child.body + ")"
        if "window" in node:
            return self.window(node)
        if "call" in node:
            name, args = node["call"], node.get("args", [])
            if name in CONTEXT and not args:
                return CONTEXT[name]
            if name not in FUNCTIONS:
                self.fail("function")
            sql_name, low, high = FUNCTIONS[name]
            if not low <= len(args) <= high:
                self.fail("function_arity", "INVALID_PARAMETER")
            return (
                sql_name
                + "("
                + ", ".join(self.expr(a, outputs=outputs) for a in args)
                + ")"
            )
        if "aggregate" in node:
            name, args = node["aggregate"], node.get("args", [])
            if name not in AGGREGATES or set(node) - {"aggregate", "args", "distinct"}:
                self.fail("aggregate")
            if len(args) != 1 and not (
                name == "count" and not args and not node.get("distinct")
            ):
                self.fail("aggregate_arity", "INVALID_PARAMETER")
            argument = self.expr(args[0]) if args else "*"
            return (
                AGGREGATES[name]
                + "("
                + ("DISTINCT " if node.get("distinct") else "")
                + argument
                + ")"
            )
        if "op" in node and node["op"] in OPERATORS:
            operator = OPERATORS[node["op"]]
            # python-tds paramstyle processes percent characters.
            operator = "%%" if operator == "%" else operator
            return f"({self.expr(node['left'], outputs=outputs)} {operator} {self.expr(node['right'], outputs=outputs)})"
        if "cast" in node and node["cast"] in TYPES:
            return f"CAST({self.expr(node['expr'], outputs=outputs)} AS {TYPES[node['cast']]})"
        if "case" in node:
            return (
                "CASE "
                + " ".join(
                    "WHEN "
                    + self.condition(branch["when"], outputs=outputs)
                    + " THEN "
                    + self.expr(branch["then"], outputs=outputs)
                    for branch in node["case"]
                )
                + (
                    " ELSE " + self.expr(node["else"], outputs=outputs)
                    if "else" in node
                    else ""
                )
                + " END"
            )
        self.fail("expression")

    def condition(self, node, *, outputs=False):
        self.root.nodes += 1
        if self.root.nodes > 4000:
            self.fail("condition_count", "QUERY_TOO_COMPLEX")
        if "exists" in node:
            return (
                ("NOT " if node.get("not") else "")
                + "EXISTS ("
                + self.child(node["exists"], correlated=True).body
                + ")"
            )
        if "combine" in node:
            combine, conditions = node["combine"], node["conditions"]
            if combine == "not":
                if len(conditions) != 1:
                    self.fail("not_arity", "INVALID_PARAMETER")
                return "(NOT " + self.condition(conditions[0], outputs=outputs) + ")"
            return (
                "("
                + (" " + combine.upper() + " ").join(
                    self.condition(c, outputs=outputs) for c in conditions
                )
                + ")"
            )
        if "op" not in node or node["op"] not in CONDITIONS:
            self.fail("condition")
        if ("field" in node) == ("left" in node):
            self.fail("condition_operand", "INVALID_PARAMETER")
        left = self.expr(node.get("field", node.get("left")), outputs=outputs)
        op = node["op"]
        if op in {"in_query", "not_in_query"}:
            if set(node) - {"op", "left", "field", "query"} or "query" not in node:
                self.fail("subquery_condition", "INVALID_PARAMETER")
            child = self.child(node["query"], correlated=True)
            if len(child.outputs) != 1:
                self.fail("subquery_columns", "INVALID_PARAMETER")
            return f"({left} {'NOT ' if op == 'not_in_query' else ''}IN ({child.body}))"
        if op in {"is_null", "is_not_null"}:
            if set(node) - {"op", "left", "field"}:
                self.fail("null_condition", "INVALID_PARAMETER")
            return f"({left} IS {'NOT ' if op == 'is_not_null' else ''}NULL)"
        if op in {"in", "not_in", "between", "not_between"}:
            if set(node) - {"op", "left", "field", "values"} or not node.get("values"):
                self.fail("condition_values", "INVALID_PARAMETER")
            values = [self.bind(v) for v in node["values"]]
            if op in {"between", "not_between"}:
                if len(values) != 2:
                    self.fail("between_arity", "INVALID_PARAMETER")
                return f"({left} {'NOT ' if op.startswith('not_') else ''}BETWEEN {values[0]} AND {values[1]})"
            return (
                f"({left} {'NOT ' if op == 'not_in' else ''}IN ({', '.join(values)}))"
            )
        if set(node) - {"op", "left", "field", "value", "right"} or (
            "value" in node
        ) == ("right" in node):
            self.fail("condition_operand", "INVALID_PARAMETER")
        right = (
            self.expr(node["right"], outputs=outputs)
            if "right" in node
            else self.bind(node["value"])
        )
        operator = COMPARISONS.get(op, "NOT LIKE" if op == "not_like" else "LIKE")
        return f"({left} {operator} {right})"

    def window(self, node):
        name, args = node["window"], node.get("args", [])
        if name in AGGREGATES:
            sql = self.expr({"aggregate": name, "args": args})
        else:
            if name not in WINDOWS:
                self.fail("window")
            function, low, high, version = WINDOWS[name]
            if self.major_version < version or not low <= len(args) <= high:
                self.fail("window_version_or_arity")
            if not node.get("order_by"):
                self.fail("window_order_required", "INVALID_PARAMETER")
            sql = function + "(" + ", ".join(self.expr(a) for a in args) + ")"
        parts = []
        if node.get("partition_by"):
            parts.append(
                "PARTITION BY " + ", ".join(self.expr(e) for e in node["partition_by"])
            )
        if node.get("order_by"):
            order = []
            for item in node["order_by"]:
                if "nulls" in item or "expr" not in item or "field" in item:
                    self.fail("window_ordering")
                order.append(self.expr(item["expr"]) + " " + item["direction"].upper())
            parts.append("ORDER BY " + ", ".join(order))
        if "frame" in node:
            frame = node["frame"]
            if (
                self.major_version < 11
                or name not in {*AGGREGATES, "first_value", "last_value"}
                or not node.get("order_by")
                or frame["unit"] not in {"rows", "range"}
            ):
                self.fail("window_frame")
            ranks = []
            bounds = []
            for bound in (frame["start"], frame["end"]):
                kind = bound["kind"]
                has_offset = kind in {"preceding", "following"}
                if has_offset != ("offset" in bound) or (
                    frame["unit"] == "range" and has_offset
                ):
                    self.fail("window_bound", "INVALID_PARAMETER")
                ranks.append(
                    {
                        "unbounded_preceding": float("-inf"),
                        "preceding": -bound.get("offset", 0),
                        "current_row": 0,
                        "following": bound.get("offset", 0),
                        "unbounded_following": float("inf"),
                    }[kind]
                )
                bounds.append(
                    (str(bound["offset"]) + " " + kind.upper())
                    if has_offset
                    else kind.replace("_", " ").upper()
                )
            if (
                ranks[0] > ranks[1]
                or frame["start"]["kind"] == "unbounded_following"
                or frame["end"]["kind"] == "unbounded_preceding"
            ):
                self.fail("window_frame_order", "INVALID_PARAMETER")
            parts.append(
                frame["unit"].upper() + " BETWEEN " + bounds[0] + " AND " + bounds[1]
            )
        return sql + " OVER (" + " ".join(parts) + ")"

    def build(self, query, *, nested=False):
        self.schema = query.get("schema", self.schema)
        if set(query) - {
            "table",
            "schema",
            "alias",
            "from",
            "columns",
            "select",
            "aggregates",
            "joins",
            "where",
            "group_by",
            "having",
            "order_by",
            "distinct",
            "limit",
            "offset",
            "database",
            "server",
            "with",
            "set",
        }:
            self.fail("query_shape")
        for cte in query.get("with", []):
            if set(cte) - {"name", "query", "columns"} or cte["name"].casefold() in {
                n.casefold() for n in self.ctes
            }:
                self.fail("cte_definition", "INVALID_PARAMETER")
            child = self.child(cte["query"])
            names = tuple(cte.get("columns", child.outputs))
            if len(names) != len(child.outputs) or len(
                {n.casefold() for n in names}
            ) != len(names):
                self.fail("cte_columns", "INVALID_PARAMETER")
            # Hoist nested WITH definitions using private, collision-free names.
            name = f"__mcp_cte_{len(self.root.definitions)}"
            self.root.definitions.append(
                identifier(name)
                + " ("
                + ", ".join(identifier(n) for n in names)
                + ") AS ("
                + child.body
                + ")"
            )
            self.ctes[cte["name"]] = (name, names)
        if "set" in query:
            if set(query) - {"set", "with", "limit", "offset", "order_by"}:
                self.fail("set_query_shape", "INVALID_PARAMETER")
            operation = query["set"]["op"]
            if operation not in {"union", "union_all", "intersect", "except"}:
                self.fail("set_operation")
            branches = [self.child(q) for q in query["set"]["queries"]]
            if any(len(b.outputs) != len(branches[0].outputs) for b in branches):
                self.fail("set_column_count", "INVALID_PARAMETER")
            alias = "__mcp_set"
            body = (" " + operation.replace("_", " ").upper() + " ").join(
                "SELECT * FROM (" + b.body + ") AS " + identifier(f"__mcp_branch_{i}")
                for i, b in enumerate(branches)
            )
            self.sources[alias] = self.virtual(branches[0].outputs)
            sources = " FROM (" + body + ") AS " + identifier(alias)
            items = [
                (n, identifier(alias) + "." + identifier(n))
                for n in branches[0].outputs
            ]
        else:
            sources, items = self.projection(query)
        return self.finish(query, sources, items, nested)

    def projection(self, query):
        sources = ""
        if any(k in query for k in ("database", "server")) and "table" not in query:
            self.fail("qualified_table_required", "INVALID_PARAMETER")
        if "from" in query and any(
            k in query for k in ("table", "alias", "schema", "database", "server")
        ):
            self.fail("ambiguous_source", "INVALID_PARAMETER")
        if "table" in query or "from" in query:
            node = query.get("from") or {
                k: query[k]
                for k in ("table", "schema", "alias", "database", "server")
                if k in query
            }
            if "alias" not in node:
                self.fail("source_alias", "MISSING_PARAMETER")
            sources = " FROM " + self.source(node)
        for join in query.get("joins", []):
            if not sources:
                self.fail("join_source", "INVALID_PARAMETER")
            right = self.source(join)
            if join["kind"] == "cross":
                if "on" in join:
                    self.fail("cross_join_on", "INVALID_PARAMETER")
                sources += " CROSS JOIN " + right
            else:
                if "on" not in join:
                    self.fail("join_on", "MISSING_PARAMETER")
                on = join["on"]
                if isinstance(on, list):
                    condition = " AND ".join(
                        f"{self.expr(pair['left'])} = {self.expr(pair['right'])}"
                        for pair in on
                    )
                else:
                    condition = self.condition(on)
                sources += f" {join['kind'].upper()} JOIN {right} ON ({condition})"
        if "select" in query and any(query.get(k) for k in ("columns", "aggregates")):
            self.fail("mixed_projection", "INVALID_PARAMETER")
        items = [
            (c.get("as", c["column"]), self.expr(c)) for c in query.get("columns", [])
        ]
        items += [
            (item["as"], self.expr(item["expr"])) for item in query.get("select", [])
        ]
        for item in query.get("aggregates", []):
            if "field" in item and "args" in item:
                self.fail("aggregate_arguments", "INVALID_PARAMETER")
            expr = {
                "aggregate": item["function"],
                **{
                    k: v
                    for k, v in item.items()
                    if k not in {"function", "as", "field"}
                },
            }
            if "field" in item:
                expr["args"] = [item["field"]]
            items.append((item["as"], self.expr(expr)))
        return sources, items

    def finish(self, query, sources, items, nested):
        if not items:
            self.fail("projection", "MISSING_PARAMETER")
        for alias, expression in items:
            identifier(alias)
            if alias.casefold() in {a.casefold() for a in self.outputs}:
                self.fail("duplicate_output", "INVALID_IDENTIFIER")
            self.outputs[alias] = expression
        offset = query.get("offset", 0)
        if offset and (not query.get("order_by") or self.major_version < 11):
            self.fail("offset_requires_sqlserver_2012_and_order")
        if offset and "limit" not in query:
            self.fail("offset_limit_required", "MISSING_PARAMETER")
        if nested and query.get("order_by") and "limit" not in query:
            self.fail("nested_order_requires_limit", "INVALID_PARAMETER")
        limit = query.get("limit")
        fetch = limit + (0 if nested else 1) if limit is not None else None
        top = "" if offset or fetch is None else "TOP (" + self.bind(fetch) + ") "
        statement = "SELECT " + ("DISTINCT " if query.get("distinct") else "") + top
        statement += (
            ", ".join(expr + " AS " + identifier(name) for name, expr in items)
            + sources
        )
        if "where" in query:
            statement += " WHERE " + self.condition(query["where"])
        if query.get("group_by"):
            statement += " GROUP BY " + ", ".join(
                self.expr(e, outputs=True) for e in query["group_by"]
            )
        if "having" in query:
            statement += " HAVING " + self.condition(query["having"], outputs=True)
        if query.get("order_by"):
            ordering = []
            output_ordering = []
            for order in query["order_by"]:
                if "nulls" in order or ("field" in order) == ("expr" in order):
                    self.fail("ordering")
                value = (
                    self.expr(order["expr"], outputs=True)
                    if "expr" in order
                    else self.expr({"output": order["field"]}, outputs=True)
                )
                matched = next(
                    (
                        name
                        for name, expression in self.outputs.items()
                        if expression == value
                    ),
                    None,
                )
                if matched is None:
                    self.fail("ordering_must_reference_projected_output")
                output_ordering.append((matched, order["direction"]))
                ordering.append(identifier(matched) + " " + order["direction"].upper())
            statement += " ORDER BY " + ", ".join(ordering)
        if offset:
            statement += (
                " OFFSET "
                + self.bind(offset)
                + " ROWS FETCH NEXT "
                + self.bind(fetch)
                + " ROWS ONLY"
            )
        prefix = (
            "WITH " + ", ".join(self.root.definitions) + " "
            if not nested and self.root.definitions
            else ""
        )
        return BuiltQuery(
            prefix + statement,
            self.parameters,
            tuple(self.outputs),
            limit,
            tuple(sorted(self.relations)),
            tuple(output_ordering) if query.get("order_by") else (),
            prefix,
        )


def compile_query(schema, query, catalog, major_version=10):
    validate_query(query)
    identifier(schema)
    return Compiler(schema, catalog, major_version).build(query)
