"""Stable, actionable diagnostics without echoing SQL, values or credentials."""

from __future__ import annotations

import json
import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg

_stage: ContextVar[str] = ContextVar("dbeaver_stage", default="dispatch")


class ToolError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "INVALID_QUERY",
        path: str | None = None,
        stage: str | None = None,
        hint: str = "根据工具参数定义修正请求后重试。",
        retryable: bool = False,
        sqlstate: str | None = None,
        issues: list[dict] | None = None,
        details: dict | None = None,
    ):
        super().__init__(message)
        self.code, self.path = code, path
        self.stage = stage or _stage.get()
        self.hint, self.retryable, self.sqlstate = hint, retryable, sqlstate
        self.issues = issues or []
        self.details = details or {}
        self.cleanup_failed = False


class QueryRejected(ToolError):
    pass


SQL_ERRORS = {
    "42725": (
        "AMBIGUOUS_FUNCTION",
        "参数类型无法唯一确定查询函数。",
        "为 value 指定结构化 type，或对表达式使用 cast。",
        False,
    ),
    "42P18": (
        "INDETERMINATE_TYPE",
        "数据库无法推断参数的数据类型。",
        "为 NULL、数组或重载函数参数显式指定 type。",
        False,
    ),
    "22023": (
        "INVALID_ARGUMENT_VALUE",
        "参数值不符合该查询操作的要求。",
        "检查函数参数范围、步长、日期单位或 JSON 路径。",
        False,
    ),
    "42501": (
        "READ_PERMISSION_DENIED",
        "数据库账号没有执行该读取所需的权限。",
        "使用该账号获准读取的对象；MCP 不提升权限。",
        False,
    ),
    "42P01": (
        "TABLE_NOT_FOUND",
        "引用的表、视图或查询别名不存在。",
        "用 search_catalog 核验 schema、表名以及 CTE/子查询别名。",
        False,
    ),
    "42703": (
        "COLUMN_NOT_FOUND",
        "引用的字段不存在。",
        "用 get_table_context 核验字段和投影别名。",
        False,
    ),
    "42702": (
        "AMBIGUOUS_COLUMN",
        "字段引用不唯一。",
        "为字段指定 source，并为重复输出设置不同的 as 名称。",
        False,
    ),
    "3F000": (
        "SCHEMA_NOT_FOUND",
        "指定的 schema 不存在。",
        "先调用 list_schemas。",
        False,
    ),
    "3D000": (
        "DATABASE_NOT_FOUND",
        "指定数据库不存在或不可进入。",
        "先调用 list_databases。",
        False,
    ),
    "28P01": (
        "AUTHENTICATION_FAILED",
        "数据库身份认证失败。",
        "在 DBeaver 中检查该已保存连接的认证配置。",
        False,
    ),
    "28000": (
        "AUTHENTICATION_FAILED",
        "数据库拒绝该账号的认证或连接来源。",
        "由连接管理员核验 DBeaver 连接与服务端认证规则。",
        False,
    ),
    "57014": (
        "QUERY_CANCELED",
        "查询被取消或达到执行超时。",
        "缩小日期范围、简化关联或减少聚合规模后重试。",
        True,
    ),
    "55P03": (
        "LOCK_TIMEOUT",
        "等待数据库锁超时。",
        "稍后重试，或由管理员检查阻塞会话。",
        True,
    ),
    "40P01": (
        "DEADLOCK",
        "数据库检测到死锁。",
        "稍后重试；若持续发生请检查并发操作。",
        True,
    ),
    "40001": (
        "SERIALIZATION_FAILURE",
        "并发事务导致本次读取需要重试。",
        "重新提交同一结构化读取请求。",
        True,
    ),
    "25006": (
        "READ_ONLY_VIOLATION",
        "数据库只读事务阻止了查询中的写入副作用。",
        "检查视图、策略、函数或外部对象；不要绕过只读事务。",
        False,
    ),
    "42883": (
        "EXPRESSION_TYPE_MISMATCH",
        "表达式的函数或运算符与参数类型不匹配。",
        "核验字段类型，并使用结构化 cast 或正确的参数类型。",
        False,
    ),
    "42804": (
        "TYPE_MISMATCH",
        "表达式或集合分支的数据类型不兼容。",
        "对齐分支列数、顺序与类型；必要时使用结构化 cast。",
        False,
    ),
    "42803": (
        "GROUPING_ERROR",
        "聚合查询的分组字段不完整。",
        "将非聚合字段加入 group_by，或移除该输出。",
        False,
    ),
    "42P20": (
        "WINDOW_ERROR",
        "窗口函数的位置或窗口定义不合法。",
        "窗口函数用于 select/order_by；需要过滤窗口结果时使用子查询。",
        False,
    ),
    "22003": (
        "NUMBER_OUT_OF_RANGE",
        "数值超出数据库字段或运算支持的范围。",
        "检查数值范围或改用 numeric 类型。",
        False,
    ),
    "22007": (
        "INVALID_DATETIME",
        "日期或时间参数格式不正确。",
        "使用明确的 ISO 日期/时间，带时区字段应提供时区。",
        False,
    ),
    "22P02": (
        "INVALID_VALUE_FORMAT",
        "参数不能转换为目标字段类型。",
        "检查日期、数值、UUID、JSON 或数组参数格式。",
        False,
    ),
    "22012": (
        "DIVISION_BY_ZERO",
        "表达式发生除零。",
        "使用 nullif 分母与零比较，或在条件中排除零。",
        False,
    ),
    "42601": (
        "QUERY_SYNTAX_UNSUPPORTED",
        "数据库不接受当前结构化查询组合。",
        "核验集合、分组和窗口组合；持续失败时提供 request_id 排查编译器。",
        False,
    ),
    "54000": (
        "DATABASE_COMPLEXITY_LIMIT",
        "查询达到数据库复杂度限制。",
        "减少查询层级、字段或条件数量。",
        False,
    ),
    "53200": (
        "DATABASE_RESOURCE_LIMIT",
        "数据库资源不足，读取未完成。",
        "缩小查询范围并检查数据库资源。",
        True,
    ),
}


def translate_error(exc: Exception, stage: str | None = None) -> ToolError:
    if isinstance(exc, ToolError):
        return exc
    stage = stage or _stage.get()
    if isinstance(exc, psycopg.Error):
        state = getattr(exc, "sqlstate", None)
        if state in SQL_ERRORS:
            code, message, hint, retryable = SQL_ERRORS[state]
        elif isinstance(exc, psycopg.DataError) and not state:
            code, message, hint, retryable = (
                "PARAMETER_ADAPTATION_FAILED",
                "参数不能转换为 PostgreSQL 数据。",
                "数组元素应为兼容类型；为 JSON/数组提供明确的结构化 type。",
                False,
            )
        elif state and state.startswith("08"):
            code, message, hint, retryable = (
                "CONNECTION_FAILED",
                "数据库连接中断。",
                "检查 DBeaver 连接和网络后重试。",
                True,
            )
        elif isinstance(exc, psycopg.OperationalError) and not state:
            text = str(exc).lower()  # Classification only; this text is never returned.
            if "password authentication" in text or "authentication failed" in text:
                code, message, hint, retryable = SQL_ERRORS["28P01"]
            elif "certificate" in text or "ssl" in text:
                code, message, hint, retryable = (
                    "TLS_FAILED",
                    "数据库 TLS 连接校验失败。",
                    "在 DBeaver 中核验 TLS/证书配置。",
                    False,
                )
            else:
                code, message, hint, retryable = (
                    "CONNECTION_FAILED",
                    "无法建立数据库连接或连接已中断。",
                    "检查 DBeaver 连接、网络及数据库可用性。",
                    True,
                )
        else:
            code, message, hint, retryable = (
                "DATABASE_ERROR",
                "数据库拒绝或未能完成读取。",
                "根据 SQLSTATE、失败阶段和 request_id 排查；不要改用自由 SQL。",
                False,
            )
        return ToolError(
            message,
            code=code,
            stage=stage,
            hint=hint,
            retryable=retryable,
            sqlstate=state,
        )
    # Existing configuration exceptions are mapped by fixed prefixes, never echoed.
    text = str(exc)
    known = [
        (
            "database is required",
            "MISSING_PARAMETER",
            "缺少 database 参数。",
            "database",
            "使用 list_databases 的精确数据库名。",
        ),
        (
            "database and schema are required",
            "MISSING_PARAMETER",
            "缺少 database/schema 参数。",
            "schema",
            "补齐数据库和 schema 上下文。",
        ),
        (
            "DBeaver username is unavailable",
            "CREDENTIAL_STORAGE_ERROR",
            "DBeaver 连接未保存有效账号。",
            "connection",
            "在 DBeaver 中验证并保存连接。",
        ),
        (
            "DBeaver PostgreSQL connection not found",
            "CONNECTION_NOT_FOUND",
            "未找到指定的 DBeaver PostgreSQL 连接。",
            "connection",
            "调用 list_dbeaver_connections，使用精确连接 ID。",
        ),
        (
            "DBeaver connection name is ambiguous",
            "CONNECTION_AMBIGUOUS",
            "连接名称不唯一。",
            "connection",
            "改用 list_dbeaver_connections 返回的连接 ID。",
        ),
        (
            "database is not connectable",
            "DATABASE_NOT_ACCESSIBLE",
            "指定数据库不能通过该连接访问。",
            "database",
            "调用 list_databases 核验该账号可进入的数据库。",
        ),
        (
            "schema not found",
            "SCHEMA_NOT_ACCESSIBLE",
            "指定 schema 不存在或账号没有 USAGE 权限。",
            "schema",
            "调用 list_schemas 核验 schema 和数据库上下文。",
        ),
        (
            "database object not found",
            "TABLE_NOT_FOUND",
            "指定表或视图不存在。",
            "table",
            "调用 search_catalog 核验对象名称。",
        ),
        (
            "DBeaver network handlers",
            "CONNECTION_HANDLER_UNSUPPORTED",
            "该连接依赖当前 MCP 尚未支持的网络处理器。",
            "connection",
            "使用经过批准的可直连 DBeaver 连接；不要传入临时地址或凭据。",
        ),
        (
            "DBeaver credentials",
            "CREDENTIAL_STORAGE_ERROR",
            "DBeaver 已保存认证配置不可用。",
            "connection",
            "在 DBeaver 中重新验证并保存该连接。",
        ),
        (
            "DBeaver data-sources",
            "CONNECTION_STORAGE_ERROR",
            "无法读取 DBeaver 连接配置。",
            None,
            "检查 DBeaver 工作区配置完整性。",
        ),
        (
            "DBeaver connection storage",
            "CONNECTION_STORAGE_ERROR",
            "DBeaver 连接存储不完整。",
            None,
            "检查默认 DBeaver 工作区是否已保存连接和凭据。",
        ),
    ]
    for prefix, code, message, path, hint in known:
        if text.startswith(prefix):
            return ToolError(message, code=code, path=path, hint=hint, stage=stage)
    if isinstance(exc, (ValueError, TypeError, OSError)) and text.startswith("DBeaver"):
        return ToolError(
            "DBeaver 连接配置不完整或不兼容。",
            code="CONNECTION_CONFIG_ERROR",
            stage=stage,
            hint="在 DBeaver 中检查该连接的驱动、默认数据库、网络处理器和保存状态。",
        )
    return ToolError(
        "工具内部处理失败。",
        code="INTERNAL_ERROR",
        stage=stage,
        hint="保留 request_id 和失败阶段用于排查；不要重复扩大查询。",
    )


@contextmanager
def error_stage(name: str):
    token = _stage.set(name)
    try:
        yield
    except Exception as exc:
        translated = translate_error(exc, name)
        if translated is exc:
            raise
        raise translated from exc
    finally:
        _stage.reset(token)


def safe_context(arguments: Any) -> dict[str, str]:
    if not isinstance(arguments, dict):
        return {}
    return {
        key: value
        for key in ("connection", "database", "schema", "table")
        if isinstance(value := arguments.get(key), str)
        and re.fullmatch(r"[\w .()\-]{1,200}", value)
    }


def error_payload(
    exc: Exception, *, arguments: Any = None, request_id: str | None = None
) -> dict:
    error = translate_error(exc)
    return {
        "error": str(error),
        "error_type": type(exc).__name__,
        "code": error.code,
        "stage": error.stage,
        "path": error.path,
        "issues": error.issues,
        "details": error.details,
        "sqlstate": error.sqlstate,
        "retryable": error.retryable,
        "hint": error.hint,
        "request_id": request_id or uuid4().hex,
        "context": safe_context(arguments),
        "cleanup_failed": error.cleanup_failed,
    }


def safe_error_message(exc: Exception) -> str:
    error = translate_error(exc)
    return str(error) + (f" (SQLSTATE {error.sqlstate})" if error.sqlstate else "")


def emit_diagnostic(payload: dict, exc: Exception) -> None:
    """Stderr diagnostics correlate by request_id; never log locals or messages."""
    root, seen = exc, set()
    while root.__cause__ is not None and id(root.__cause__) not in seen:
        seen.add(id(root))
        root = root.__cause__
    frames = []
    traceback = root.__traceback__
    while traceback is not None:
        frames.append(
            {
                "file": Path(traceback.tb_frame.f_code.co_filename).name,
                "function": traceback.tb_frame.f_code.co_name,
                "line": traceback.tb_lineno,
            }
        )
        traceback = traceback.tb_next
    record = {
        key: payload.get(key)
        for key in ("request_id", "code", "stage", "sqlstate", "path", "cleanup_failed")
    }
    record.update(
        timestamp=datetime.now(UTC).isoformat(),
        error_type=type(root).__name__,
        frames=frames[-8:],
    )
    try:
        sys.stderr.write(json.dumps(record, ensure_ascii=False) + "\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass
