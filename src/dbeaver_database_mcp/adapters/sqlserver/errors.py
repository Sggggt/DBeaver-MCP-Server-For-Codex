"""Translate driver failures to fixed, redacted diagnostics."""

import re
import socket

import pytds

from ...core.errors import ToolError


def translate_database_error(exc, stage):
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return ToolError(
            "SQL Server 请求超时。", code="QUERY_TIMEOUT", stage=stage, retryable=True
        )
    if not isinstance(exc, pytds.Error):
        return None
    number = getattr(exc, "number", None)
    if not isinstance(number, int):
        number = getattr(exc, "msg_no", None)
    # Some driver exceptions retain the native number only in args[0]. Never
    # expose the remaining tuple or the raw server message.
    if not isinstance(number, int) and exc.args and isinstance(exc.args[0], int):
        number = exc.args[0]
    code = {
        18456: "AUTHENTICATION_FAILED",
        4060: "DATABASE_NOT_ACCESSIBLE",
        916: "DATABASE_NOT_ACCESSIBLE",
        229: "READ_PERMISSION_DENIED",
        230: "READ_PERMISSION_DENIED",
        262: "SHOWPLAN_PERMISSION_DENIED",
        8180: "QUERY_PREPARATION_FAILED",
        7202: "LINKED_SERVER_NOT_FOUND",
        7411: "LINKED_SERVER_CAPABILITY_REQUIRED",
        7416: "LINKED_SERVER_ACCESS_DENIED",
        208: "TABLE_NOT_FOUND",
        207: "COLUMN_NOT_FOUND",
        1222: "LOCK_TIMEOUT",
        1205: "DEADLOCK_DETECTED",
    }.get(number, "DATABASE_ERROR")
    if isinstance(exc, pytds.LoginError):
        code = "AUTHENTICATION_FAILED"
    # SQLSTATE, when present, must itself be a safe five-character identifier.
    state = getattr(exc, "sqlstate", None)
    state = (
        state
        if isinstance(state, str) and re.fullmatch(r"[A-Z0-9]{5}", state)
        else None
    )
    return ToolError(
        "SQL Server 调用失败。",
        code=code,
        stage=stage,
        sqlstate=state,
        retryable=code in {"LOCK_TIMEOUT", "DEADLOCK_DETECTED"},
        details={"database_error_code": number} if isinstance(number, int) else {},
        hint="检查已保存连接、数据库原生读取权限与适配器能力；错误正文不会回显。",
    )
