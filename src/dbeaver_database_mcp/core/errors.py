"""Stable, actionable diagnostics without echoing SQL, values or credentials."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

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


_error_translators: list[Callable[[Exception, str], ToolError | None]] = []


def register_error_translator(
    translator: Callable[[Exception, str], ToolError | None],
) -> None:
    if translator not in _error_translators:
        _error_translators.append(translator)


def translate_error(exc: Exception, stage: str | None = None) -> ToolError:
    if isinstance(exc, ToolError):
        return exc
    stage = stage or _stage.get()
    for translator in _error_translators:
        if translated := translator(exc, stage):
            return translated
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
            "DBeaver connection not found",
            "CONNECTION_NOT_FOUND",
            "未找到指定的 DBeaver 连接。",
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
        for key in ("connection", "database", "schema", "table", "database_type")
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
