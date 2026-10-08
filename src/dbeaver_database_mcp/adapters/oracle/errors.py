"""Oracle error classification without SQL, connection addresses or values."""

import errno
import re
import socket

import oracledb

from ...core.errors import ToolError

ERRORS = {
    1017: ("AUTHENTICATION_FAILED", "Oracle 认证失败。", False),
    1031: ("READ_PERMISSION_DENIED", "Oracle 账号缺少读取或目录检查权限。", False),
    942: ("TABLE_NOT_ACCESSIBLE", "Oracle 对象不存在或账号无读取权限。", False),
    904: ("COLUMN_NOT_FOUND", "Oracle 查询引用了无效字段。", False),
    1722: ("PARAMETER_ADAPTATION_FAILED", "Oracle 参数不能转换为所需类型。", False),
    1843: ("PARAMETER_ADAPTATION_FAILED", "Oracle 日期参数无效。", False),
    1858: ("PARAMETER_ADAPTATION_FAILED", "Oracle 日期参数无效。", False),
    146: ("DATABASE_ERROR", "Oracle 查询处理失败。", False),
    1013: ("QUERY_TIMEOUT", "Oracle 查询超时或已取消。", True),
    3113: ("CONNECTION_FAILED", "Oracle 连接中断。", True),
    3114: ("CONNECTION_FAILED", "Oracle 连接不可用。", True),
    8177: ("TRANSACTION_CONFLICT", "Oracle 事务冲突。", True),
    1456: ("READ_ONLY_VIOLATION", "Oracle 拒绝只读事务中的写入。", False),
    16000: ("READ_ONLY_VIOLATION", "Oracle 拒绝只读数据库中的写入。", False),
}

DRIVER_ERRORS = {
    "DPY-3010": (
        "UNSUPPORTED_DATABASE_VERSION",
        "Oracle 服务端版本不支持 Thin 驱动。",
        "核验实际 Oracle 版本；Thin 驱动要求 12.1 或以上。",
        False,
    ),
    "DPY-3015": (
        "UNSUPPORTED_AUTHENTICATION",
        "Oracle 账号的密码验证格式不支持 Thin 驱动。",
        "核验认证兼容性；不要自动修改账号密码或数据库安全设置。",
        False,
    ),
    "DPY-3001": (
        "UNSUPPORTED_NETWORK_ENCRYPTION",
        "当前 Thin 驱动不支持服务端要求的网络加密方式。",
        "使用支持该加密方式的驱动；不要关闭数据库安全策略。",
        False,
    ),
    "DPY-6001": (
        "SERVICE_NOT_REGISTERED",
        "Oracle 监听器未注册所选服务。",
        "核验保存连接的 Service Name 与连接标识类型。",
        False,
    ),
    "DPY-6003": (
        "SID_NOT_REGISTERED",
        "Oracle 监听器未注册所选 SID。",
        "核验保存连接的 SID 与连接标识类型。",
        False,
    ),
    "DPY-4024": (
        "QUERY_TIMEOUT",
        "Oracle 操作超过调用时间上限。",
        "检查连接或缩小查询范围后重试。",
        True,
    ),
    "DPY-4011": (
        "CONNECTION_FAILED",
        "Oracle 服务端或网络关闭了连接。",
        "核验驱动、认证和网络兼容性。",
        True,
    ),
    "DPY-6005": (
        "CONNECTION_FAILED",
        "无法建立 Oracle 连接。",
        "根据底层错误码和故障类别排查保存连接。",
        True,
    ),
}
ERROR_CODE = re.compile(r"(?:ORA-\d{5}|DPY-\d{4}|DPI-\d{4})")


def _diagnostics(exc: Exception, stage: str) -> dict:
    """Only coded diagnostics cross the boundary; messages remain private."""
    codes, seen, current = [], set(), exc
    os_kind = None
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        detail = current.args[0] if current.args else None
        candidates = [getattr(detail, "full_code", "")]
        if stage == "connect":
            message = getattr(detail, "message", "")
            if isinstance(message, str):
                candidates.extend(ERROR_CODE.findall(message[:65536]))
        for candidate in candidates:
            if (
                isinstance(candidate, str)
                and ERROR_CODE.fullmatch(candidate)
                and candidate not in codes
                and len(codes) < 8
            ):
                codes.append(candidate)
        if isinstance(current, socket.gaierror):
            os_kind = "name_resolution_failed"
        elif isinstance(current, TimeoutError):
            os_kind = "connection_timeout"
        elif isinstance(current, OSError):
            native = getattr(current, "winerror", None) or current.errno
            os_kind = {
                errno.ECONNREFUSED: "connection_refused",
                10061: "connection_refused",
                errno.ETIMEDOUT: "connection_timeout",
                10060: "connection_timeout",
                errno.ECONNRESET: "connection_reset",
                10054: "connection_reset",
                errno.ENETUNREACH: "network_unreachable",
                10051: "network_unreachable",
                errno.EHOSTUNREACH: "host_unreachable",
                10065: "host_unreachable",
                errno.EACCES: "network_access_denied",
                10013: "network_access_denied",
            }.get(native, "network_error")
        current = current.__cause__ or (
            current.__context__ if not current.__suppress_context__ else None
        )
    result = {}
    if codes:
        result = {"database_error_code": codes[0], "database_error_codes": codes}
        if len(codes) > 1:
            result["root_database_error_code"] = codes[-1]
    if os_kind:
        result["os_error_kind"] = os_kind
    return result


def translate_database_error(exc: Exception, stage: str) -> ToolError | None:
    if not isinstance(exc, oracledb.Error):
        return None
    detail = exc.args[0] if exc.args else None
    number = getattr(detail, "code", None)
    code, message, retryable = ERRORS.get(
        number, ("DATABASE_ERROR", "Oracle 拒绝或未能完成读取。", False)
    )
    if isinstance(number, int) and 12500 <= number <= 12699:
        code, message, retryable = "CONNECTION_FAILED", "无法建立 Oracle 连接。", True
    diagnostics = _diagnostics(exc, stage)
    hint = "核验 DBeaver 连接、账号权限和参数类型；不要提交自由 SQL。"
    for candidate in reversed(diagnostics.get("database_error_codes", [])):
        if candidate in DRIVER_ERRORS:
            code, message, hint, retryable = DRIVER_ERRORS[candidate]
            break
        if candidate.startswith("ORA-") and int(candidate[4:]) in ERRORS:
            code, message, retryable = ERRORS[int(candidate[4:])]
            break
    if diagnostics.get("os_error_kind"):
        code, message, retryable = (
            "CONNECTION_FAILED",
            "Oracle 网络连接未能建立或已中断。",
            True,
        )
        hint = "按 os_error_kind 核验保存连接及网络状态。"
    return ToolError(
        message,
        code=code,
        stage=stage,
        retryable=retryable,
        hint=hint,
        details=diagnostics,
    )
