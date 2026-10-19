"""Public schema for a closed relational expression language, never SQL text."""

from __future__ import annotations


def obj(properties: dict, required=()) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


NAME = {"type": "string", "minLength": 1, "maxLength": 128}
ALIAS = NAME
VALUE = {"type": ["string", "number", "boolean", "null", "array", "object"]}
TYPES = [
    "text",
    "varchar",
    "bool",
    "int2",
    "int4",
    "int8",
    "numeric",
    "float4",
    "float8",
    "date",
    "time",
    "timestamp",
    "timestamptz",
    "interval",
    "uuid",
    "json",
    "jsonb",
    "bytea",
    "inet",
    "cidr",
    "text[]",
    "int4[]",
    "int8[]",
    "numeric[]",
    "jsonb[]",
    "uuid[]",
]
TABLE_FUNCTIONS = [
    "generate_series",
    "unnest",
    "json_array_elements",
    "jsonb_array_elements",
    "json_array_elements_text",
    "jsonb_array_elements_text",
    "json_each",
    "jsonb_each",
    "json_each_text",
    "jsonb_each_text",
    "json_object_keys",
    "jsonb_object_keys",
]
FUNCTIONS = TABLE_FUNCTIONS + [
    "abs",
    "ceil",
    "ceiling",
    "floor",
    "round",
    "trunc",
    "sqrt",
    "power",
    "mod",
    "sign",
    "exp",
    "ln",
    "log",
    "lower",
    "upper",
    "length",
    "char_length",
    "octet_length",
    "btrim",
    "ltrim",
    "rtrim",
    "substring",
    "substr",
    "replace",
    "split_part",
    "concat",
    "concat_ws",
    "left",
    "right",
    "repeat",
    "reverse",
    "strpos",
    "regexp_replace",
    "regexp_match",
    "regexp_matches",
    "coalesce",
    "nullif",
    "greatest",
    "least",
    "date_trunc",
    "date_part",
    "age",
    "to_char",
    "to_date",
    "to_timestamp",
    "make_date",
    "make_timestamp",
    "now",
    "clock_timestamp",
    "statement_timestamp",
    "transaction_timestamp",
    "current_date",
    "current_timestamp",
    "current_database",
    "current_schema",
    "current_user",
    "session_user",
    "pg_backend_pid",
    "json_extract_path",
    "json_extract_path_text",
    "jsonb_extract_path",
    "jsonb_extract_path_text",
    "json_array_length",
    "jsonb_array_length",
    "json_typeof",
    "jsonb_typeof",
    "to_json",
    "to_jsonb",
    "json_build_object",
    "jsonb_build_object",
    "json_build_array",
    "jsonb_build_array",
    "json_object_keys",
    "jsonb_object_keys",
    "array_length",
    "array_lower",
    "array_upper",
    "cardinality",
    "array_position",
    "array_positions",
    "array_to_string",
    "string_to_array",
    "unnest",
    "encode",
    "decode",
]
FUNCTIONS = list(dict.fromkeys(FUNCTIONS))
AGGREGATES = [
    "count",
    "sum",
    "avg",
    "min",
    "max",
    "bool_and",
    "bool_or",
    "every",
    "stddev",
    "stddev_pop",
    "stddev_samp",
    "variance",
    "var_pop",
    "var_samp",
    "string_agg",
    "array_agg",
    "json_agg",
    "jsonb_agg",
    "json_object_agg",
    "jsonb_object_agg",
    "percentile_cont",
    "percentile_disc",
    "mode",
    "corr",
    "covar_pop",
    "covar_samp",
]
WINDOWS = [
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
    "bool_and",
    "bool_or",
    "array_agg",
    "string_agg",
]
EXPR_OPS = [
    "add",
    "sub",
    "mul",
    "div",
    "mod",
    "concat",
    "json_get",
    "json_text",
    "json_path",
    "json_path_text",
    "contains",
    "contained_by",
    "overlap",
]
OPS = [
    "eq",
    "ne",
    "lt",
    "le",
    "gt",
    "ge",
    "in",
    "not_in",
    "between",
    "not_between",
    "like",
    "ilike",
    "not_like",
    "not_ilike",
    "regex",
    "iregex",
    "not_regex",
    "not_iregex",
    "is_null",
    "is_not_null",
    "is_distinct",
    "is_not_distinct",
    "contains",
    "contained_by",
    "overlap",
    "in_query",
    "not_in_query",
]
E = {"$ref": "#/$defs/expression"}
Q = {"$ref": "#/$defs/query"}
F = {"$ref": "#/$defs/filter"}


def array(items: dict, maximum: int = 100, minimum: int = 0) -> dict:
    return {"type": "array", "items": items, "maxItems": maximum, "minItems": minimum}


REF = obj({"source": NAME, "column": NAME}, ["source", "column"])
ORDER = obj(
    {
        "field": NAME,
        "expr": E,
        "direction": {"enum": ["asc", "desc"]},
        "nulls": {"enum": ["first", "last"]},
    },
    ["direction"],
)
BOUND = obj(
    {
        "kind": {
            "enum": [
                "unbounded_preceding",
                "preceding",
                "current_row",
                "following",
                "unbounded_following",
            ]
        },
        "offset": {"type": "integer", "minimum": 0, "maximum": 100000},
    },
    ["kind"],
)
EXPRESSIONS = {
    "oneOf": [
        REF,
        obj({"value": VALUE, "type": {"enum": TYPES}}, ["value"]),
        obj({"call": {"enum": FUNCTIONS}, "args": array(E, 20)}, ["call"]),
        obj({"op": {"enum": EXPR_OPS}, "left": E, "right": E}, ["op", "left", "right"]),
        obj({"cast": {"enum": TYPES}, "expr": E}, ["cast", "expr"]),
        obj(
            {
                "case": array(obj({"when": F, "then": E}, ["when", "then"]), 20, 1),
                "else": E,
            },
            ["case"],
        ),
        obj(
            {
                "aggregate": {"enum": AGGREGATES},
                "args": array(E, 4),
                "distinct": {"type": "boolean"},
                "filter": F,
                "order_by": array(ORDER, 10),
                "within_group": array(ORDER, 4),
            },
            ["aggregate"],
        ),
        obj(
            {
                "window": {"enum": WINDOWS},
                "args": array(E, 4),
                "partition_by": array(E, 20),
                "order_by": array(ORDER, 10),
                "frame": obj(
                    {
                        "unit": {"enum": ["rows", "range", "groups"]},
                        "start": BOUND,
                        "end": BOUND,
                    },
                    ["unit", "start", "end"],
                ),
            },
            ["window"],
        ),
        obj({"subquery": Q}, ["subquery"]),
        obj({"array": array(E, 100)}, ["array"]),
        obj({"output": NAME}, ["output"]),
    ]
}
FILTER = {
    "oneOf": [
        obj(
            {
                "field": E,
                "left": E,
                "op": {"enum": OPS},
                "value": VALUE,
                "values": array(VALUE, 200, 1),
                "right": E,
                "query": Q,
            },
            ["op"],
        ),
        obj(
            {"combine": {"enum": ["and", "or", "not"]}, "conditions": array(F, 50, 1)},
            ["combine", "conditions"],
        ),
        obj({"exists": Q, "not": {"type": "boolean"}}, ["exists"]),
    ]
}
SOURCE = obj(
    {
        "table": NAME,
        "database": NAME,
        "server": NAME,
        "schema": NAME,
        "alias": NAME,
        "function": obj(
            {"call": {"enum": TABLE_FUNCTIONS}, "args": array(E, 20)}, ["call"]
        ),
        "only": {"type": "boolean"},
        "query": Q,
        "cte": NAME,
        "values": array(array(E, 100, 1), 500, 1),
        "columns": array(NAME, 100, 1),
    },
    ["alias"],
)
JOIN = obj(
    {
        **SOURCE["properties"],
        "kind": {"enum": ["inner", "left", "right", "full", "cross"]},
        "on": {
            "oneOf": [F, array(obj({"left": E, "right": E}, ["left", "right"]), 20, 1)]
        },
        "using": array(NAME, 20, 1),
        "lateral": {"type": "boolean"},
    },
    ["alias", "kind"],
)
QUERY_PROPERTIES = {
    "table": NAME,
    "database": NAME,
    "server": NAME,
    "schema": NAME,
    "alias": NAME,
    "only": {"type": "boolean"},
    "from": SOURCE,
    "columns": array(obj({**REF["properties"], "as": NAME}, ["source", "column"]), 100),
    "select": array(obj({"expr": E, "as": NAME}, ["expr", "as"]), 100),
    "aggregates": array(
        obj(
            {
                "function": {"enum": AGGREGATES},
                "field": E,
                "args": array(E, 4),
                "distinct": {"type": "boolean"},
                "as": NAME,
                "filter": F,
                "order_by": array(ORDER, 10),
            },
            ["function", "as"],
        ),
        30,
    ),
    "joins": array(JOIN, 12),
    "where": F,
    "group_by": array(E, 50),
    "having": F,
    "order_by": array(ORDER, 30),
    "distinct": {"type": "boolean"},
    "distinct_on": array(E, 20),
    "limit": {"type": "integer", "minimum": 1, "maximum": 500},
    "offset": {"type": "integer", "minimum": 0, "maximum": 1000000},
    "with": array(
        obj(
            {
                "name": NAME,
                "query": Q,
                "columns": array(NAME, 100, 1),
                "materialized": {"type": "boolean"},
            },
            ["name", "query"],
        ),
        16,
    ),
    "recursive": {"type": "boolean"},
    "set": obj(
        {
            "op": {
                "enum": [
                    "union",
                    "union_all",
                    "intersect",
                    "intersect_all",
                    "except",
                    "except_all",
                ]
            },
            "queries": array(Q, 8, 2),
        },
        ["op", "queries"],
    ),
    "values": array(array(E, 100, 1), 500, 1),
    "value_columns": array(NAME, 100, 1),
}
DEFS = {
    "expression": EXPRESSIONS,
    "filter": FILTER,
    "query": obj(
        {
            **QUERY_PROPERTIES,
            "limit": {"type": "integer", "minimum": 1, "maximum": 10000000},
        }
    ),
}
QUERY_SCHEMA = {**obj(QUERY_PROPERTIES), "$defs": DEFS}
FILTER_DEFS = DEFS  # Kept for the MCP schema composition import.
