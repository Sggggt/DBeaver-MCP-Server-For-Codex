from __future__ import annotations

import json
import sys
from typing import Any
from uuid import uuid4

from . import __version__
from .config import Settings
from .database import DatabaseService
from .errors import emit_diagnostic, error_payload, error_stage
from .postgres_logs import LOG_FILTER_SCHEMA, validate_log_filters
from .query import (
    FILTER_DEFS,
    QUERY_SCHEMA,
    QueryRejected,
    validate_query,
    validate_schema,
)
from .query_schema import AGGREGATES, FUNCTIONS, OPS, TYPES, WINDOWS

SERVER_NAME = "dbeaver-database"
SERVER_VERSION = __version__
INSTRUCTIONS = (
    "DBeaver PostgreSQL结构化只读查询工具。LLM只能提交表、列、连接、聚合、排序和条件的JSON参数；"
    "不接受SQL或表达式文本、自定义函数调用或文件路径。先list_dbeaver_connections，再list_databases、"
    "search_catalog、get_table_context。使用analyze_query、explain_query、execute_query；"
    "所有访问必须明确connection/database，数据查询还须明确schema。条件只使用固定运算符，"
    "值绑定为参数；数据库强制只读事务、原生类型、对象依赖安全检查和数据库账号原生读权限、超时、行数和响应上限。"
    "read_postgresql_logs只读取服务端当前JSON/CSV日志，按时间/级别筛选并返回安全事件摘要，"
    "不返回SQL正文、凭据、原始日志消息或任意文件内容。业务数据及日志不在MCP本地落盘。"
)
MAX_REQUEST_BYTES = 131072
MAX_RESPONSE_BYTES = 524288


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
            **(
                {"$defs": FILTER_DEFS}
                if "query" in properties
                and isinstance(properties["query"], dict)
                and "table" in properties["query"].get("properties", {})
                else {}
            ),
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }


CONNECTION = {
    "type": "string",
    "minLength": 1,
    "maxLength": 200,
    "description": "DBeaver中已有PostgreSQL连接的显示名称或连接ID；不得传地址、账号或密码。",
}
DATABASE = {
    "type": "string",
    "minLength": 1,
    "maxLength": 128,
    "description": "通过list_databases发现的精确PostgreSQL数据库名；不得猜测或传地址。",
}
SCHEMA = {
    "type": "string",
    "minLength": 1,
    "maxLength": 128,
    "description": "所选database中的精确schema名字；未知时先用list_schemas或省略search_catalog的schema跨schema搜索。",
}
OBJECT_TYPES = [
    "table",
    "partitioned_table",
    "view",
    "materialized_view",
    "foreign_table",
]

TOOLS = [
    _tool(
        "list_dbeaver_connections",
        "列出DBeaver中已有且可用的PostgreSQL连接，并返回每个连接可进入的数据库摘要；不返回地址或账密。",
        {},
    ),
    _tool(
        "list_databases",
        "列出一个DBeaver PostgreSQL连接下当前账号可连接的全部数据库；返回默认库、模板库标志和注释。",
        {"connection": CONNECTION},
        ["connection"],
    ),
    _tool(
        "dbeaver_status",
        "验证一个DBeaver连接下的精确database、可见schemas、双重只读状态、服务端版本和硬限制；不返回地址或账密。",
        {"connection": CONNECTION, "database": DATABASE},
        ["connection", "database"],
    ),
    _tool(
        "list_schemas",
        "列出一个DBeaver连接下精确database中的可见schema及每个schema的表、视图和对象计数。",
        {"connection": CONNECTION, "database": DATABASE},
        ["connection", "database"],
    ),
    _tool(
        "scan_database",
        "扫描DBeaver连接内一个精确database/schema的表和视图目录，返回对象类型、注释、近似行数和计数；不读取业务行。",
        {
            "connection": CONNECTION,
            "database": DATABASE,
            "schema": SCHEMA,
            "name_pattern": {"type": "string", "maxLength": 200, "default": ""},
            "object_types": {
                "type": "array",
                "items": {"type": "string", "enum": OBJECT_TYPES},
                "uniqueItems": True,
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
        },
        ["connection", "database", "schema"],
    ),
    _tool(
        "search_catalog",
        "按表名、字段名或注释搜索精确database目录；schema可省略以跨全部可见非系统schema搜索。",
        {
            "connection": CONNECTION,
            "database": DATABASE,
            "schema": SCHEMA,
            "query": {"type": "string", "minLength": 1, "maxLength": 200},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
        },
        ["connection", "database", "query"],
    ),
    _tool(
        "get_table_context",
        "读取DBeaver连接内精确database/schema中的表或视图字段、注释、主外键、索引和近似行数；不读取业务行。",
        {
            "connection": CONNECTION,
            "database": DATABASE,
            "schema": SCHEMA,
            "table": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        ["connection", "database", "schema", "table"],
    ),
]
QUERY_PROPERTY = {k: v for k, v in QUERY_SCHEMA.items() if k != "$defs"}
for name, description in (
    (
        "analyze_query",
        "校验结构化查询的表、字段、权限和类型，返回查询结构摘要；不读取业务行。",
    ),
    (
        "explain_query",
        "对服务端生成的结构化SELECT读取计划成本摘要；不运行ANALYZE，返回脱敏的计划树与成本，隐藏条件值。",
    ),
    (
        "execute_query",
        "执行服务端组装的结构化SELECT；只读事务、账号原生读权限与RLS、依赖安全校验、参数绑定和有界结果。",
    ),
):
    TOOLS.append(
        _tool(
            name,
            description,
            {
                "connection": CONNECTION,
                "database": DATABASE,
                "schema": SCHEMA,
                "query": QUERY_PROPERTY,
                "timeout_ms": {"type": "integer", "minimum": 100, "maximum": 30000},
            },
            ["connection", "database", "schema", "query"],
        )
    )
TOOLS.append(
    _tool(
        "read_postgresql_logs",
        "读取精确database的PostgreSQL当前JSON/CSV日志尾部；仅安全事件摘要，不接收路径或返回SQL/原始消息。",
        {
            "connection": CONNECTION,
            "database": DATABASE,
            **LOG_FILTER_SCHEMA["properties"],
        },
        ["connection", "database"],
    )
)
TOOLS.append(
    _tool(
        "read_database_settings",
        "读取当前账号可见的数据库配置，替代SHOW/SHOW ALL；敏感配置值脱敏。",
        {
            "connection": CONNECTION,
            "database": DATABASE,
            "names": {
                "type": "array",
                "minItems": 1,
                "maxItems": 100,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1, "maxLength": 100},
            },
        },
        ["connection", "database"],
    )
)
TOOLS.append(
    _tool(
        "get_query_capabilities",
        "查看结构化查询能力、参数语言和安全限制；不连接数据库。",
        {},
    )
)
TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}


def _service() -> DatabaseService:
    with error_stage("configuration"):
        return DatabaseService(Settings.from_dbeaver())


def _handle_tool(name: str, arguments: dict[str, Any]) -> Any:
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        raise QueryRejected(
            "未知工具；自由 SQL 入口已移除。",
            code="UNKNOWN_TOOL",
            stage="dispatch",
            hint="使用 execute_query/analyze_query/explain_query 或目录与日志接口。",
        )
    with error_stage("validate"):
        validate_schema(arguments, tool["inputSchema"])
    if name == "get_query_capabilities":
        return {
            "version": SERVER_VERSION,
            "query_schema": QUERY_SCHEMA,
            "functions": FUNCTIONS,
            "aggregates": AGGREGATES,
            "windows": WINDOWS,
            "conditions": OPS,
            "casts": TYPES,
            "permission_scope": "database_account",
            "row_security": "database_native",
            "raw_sql": False,
            "unsupported": [
                "unverified remote FDW side effects",
                "untrusted procedural functions or custom access methods",
            ],
        }
    if name in {"analyze_query", "explain_query", "execute_query"}:
        validate_query(arguments["query"])
    if name == "read_postgresql_logs":
        validate_log_filters(
            {k: v for k, v in arguments.items() if k not in {"connection", "database"}}
        )
    service = _service()
    connection = arguments.get("connection", "")
    database = arguments.get("database", "")
    schema = arguments.get("schema", "")
    if name == "list_dbeaver_connections":
        return service.list_connections()
    if name == "list_databases":
        return service.list_databases(connection)
    if name == "dbeaver_status":
        return service.status(connection, database)
    if name == "list_schemas":
        return service.list_schemas(connection, database)
    if name == "scan_database":
        return service.scan_database(
            connection,
            database,
            schema,
            name_pattern=arguments.get("name_pattern", ""),
            object_types=arguments.get("object_types"),
            limit=arguments.get("limit", 200),
        )
    if name == "search_catalog":
        return service.search_catalog(
            connection,
            database,
            arguments["query"],
            schema=schema,
            limit=arguments.get("limit", 50),
        )
    if name == "get_table_context":
        return service.table_context(connection, database, schema, arguments["table"])
    if name == "read_database_settings":
        return service.read_database_settings(
            connection, database, arguments.get("names")
        )
    if name == "read_postgresql_logs":
        return service.read_postgresql_logs(
            connection,
            database,
            **{
                k: v
                for k, v in arguments.items()
                if k not in {"connection", "database"}
            },
        )
    mode = {
        "analyze_query": "analyze",
        "explain_query": "explain",
        "execute_query": "execute",
    }[name]
    return service.structured_query(
        connection,
        database,
        schema,
        arguments["query"],
        mode=mode,
        timeout_ms=arguments.get("timeout_ms"),
    )


def _encode(message: dict) -> bytes:
    return json.dumps(
        message, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _write(message: dict[str, Any]) -> None:
    sys.stdout.buffer.write(_encode(message) + b"\n")
    sys.stdout.buffer.flush()


def _result(request_id: Any, result: Any) -> None:
    _write({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id: Any, code: int, message: str) -> None:
    stable = {
        -32700: "INVALID_JSON",
        -32600: "INVALID_REQUEST",
        -32601: "METHOD_NOT_FOUND",
        -32603: "INTERNAL_ERROR",
    }.get(code, "PROTOCOL_ERROR")
    _write(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": code,
                "message": message,
                "data": {
                    "code": stable,
                    "stage": "protocol",
                    "retryable": False,
                    "request_id": uuid4().hex,
                    "hint": "修正 JSON-RPC 请求格式、请求大小或方法名称后重试。",
                },
            },
        }
    )


def _handle(message: dict[str, Any]) -> None:
    request_id = message.get("id")
    if request_id is None:
        return
    if type(request_id) not in (int, str) or len(str(request_id)) > 200:
        _error(None, -32600, "invalid request id")
        return
    method = message.get("method")
    if method == "initialize":
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        requested = params.get("protocolVersion", "2025-06-18")
        requested = (
            requested
            if isinstance(requested, str) and len(requested) <= 30
            else "2025-06-18"
        )
        _result(
            request_id,
            {
                "protocolVersion": requested,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS,
            },
        )
    elif method == "ping":
        _result(request_id, {})
    elif method == "tools/list":
        _result(request_id, {"tools": TOOLS})
    elif method == "tools/call":
        trace_id = uuid4().hex
        arguments = {}
        try:
            params = message.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                raise QueryRejected(
                    "工具调用格式不正确。", code="INVALID_TOOL_CALL", stage="protocol"
                )
            arguments = params.get("arguments", {})
            with error_stage("dispatch"):
                data = _handle_tool(params["name"], arguments)
            result = {
                "content": [
                    {"type": "text", "text": json.dumps(data, ensure_ascii=False)}
                ],
                "structuredContent": data,
                "isError": False,
            }
            if (
                len(_encode({"jsonrpc": "2.0", "id": request_id, "result": result}))
                > MAX_RESPONSE_BYTES
            ):
                raise QueryRejected(
                    "结果超过响应大小上限。",
                    code="RESULT_TOO_LARGE",
                    stage="serialize",
                    hint="减少列数、limit 或目录检索范围。",
                )
            _result(request_id, result)
        except Exception as exc:
            data = error_payload(exc, arguments=arguments, request_id=trace_id)
            emit_diagnostic(data, exc)
            _result(
                request_id,
                {
                    "content": [
                        {"type": "text", "text": json.dumps(data, ensure_ascii=False)}
                    ],
                    "structuredContent": data,
                    "isError": True,
                },
            )
    elif method in {"resources/list", "prompts/list"}:
        _result(
            request_id,
            {"resources": []} if method == "resources/list" else {"prompts": []},
        )
    elif method == "logging/setLevel":
        _result(request_id, {})
    else:
        _error(request_id, -32601, "method not found")


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def serve() -> None:
    while True:
        raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
        if not raw:
            return
        if len(raw) > MAX_REQUEST_BYTES:
            while raw and not raw.endswith(b"\n"):
                raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
            _error(None, -32600, "request exceeds byte limit")
            continue
        if not raw.strip():
            continue
        try:
            message = json.loads(
                raw.decode("utf-8"),
                parse_constant=_reject_constant,
                object_pairs_hook=_unique_object,
            )
            if not isinstance(message, dict):
                raise TypeError("request must be an object")
            _handle(message)
        except (UnicodeError, ValueError, TypeError, RecursionError):
            _error(None, -32700, "invalid JSON request")
        except Exception:
            _error(None, -32603, "internal error")


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
