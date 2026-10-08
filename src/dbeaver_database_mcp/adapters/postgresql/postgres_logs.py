"""Read the current PostgreSQL JSON/CSV log using fixed internal templates.

No caller-supplied filenames, paths, SQL, function names, or raw log text are
accepted or returned. Error text can contain patient data, so output contains
safe event classifications and SQLSTATE descriptions instead of free text.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg

from ...core.tool_schema import LEVELS, LOG_FILTER_SCHEMA
from .query import QueryRejected, validate_schema


def parse_since(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(UTC)
    except (TypeError, ValueError):
        raise QueryRejected(
            "since 必须是带时区的 ISO 8601 时间。",
            code="INVALID_DATETIME",
            path="since",
            hint="例如 2026-09-08T00:00:00+08:00。",
        ) from None


def validate_log_filters(filters: dict) -> None:
    validate_schema(filters, LOG_FILTER_SCHEMA)
    for key in ("limit", "max_bytes"):
        if key in filters and type(filters[key]) is not int:
            raise QueryRejected(
                "日志数量和字节上限必须是整数。", code="INVALID_PARAMETER", path=key
            )
    parse_since(filters.get("since"))


def safe_event(message: str, code: str) -> str:
    # Return constants only. Regex-based redaction of arbitrary database errors
    # cannot guarantee that patient identifiers or credentials are removed.
    if code and code != "00000":
        try:
            return psycopg.errors.lookup(code).__name__
        except KeyError:
            return "DatabaseError"
    lower = message.lower()
    for prefix, event in (
        ("duration:", "StatementDuration"),
        ("checkpoint starting:", "CheckpointStarting"),
        ("checkpoint complete:", "CheckpointComplete"),
        ("connection received:", "ConnectionReceived"),
        ("connection authenticated:", "ConnectionAuthenticated"),
        ("connection authorized:", "ConnectionAuthorized"),
        ("disconnection:", "Disconnection"),
        ("database system is ready to accept connections", "DatabaseReady"),
        ("database system is shut down", "DatabaseShutdown"),
        ("automatic vacuum of table", "AutomaticVacuum"),
        ("automatic analyze of table", "AutomaticAnalyze"),
        ("statement:", "StatementLogged"),
        ("execute ", "StatementLogged"),
    ):
        if lower.startswith(prefix):
            return event
    return "ServerMessage"


def _timestamp(value: object, log_timezone: str) -> datetime | None:
    if not isinstance(value, str):
        return None
    match = re.match(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)(?: (\S+))?$", value
    )
    if not match:
        return None
    try:
        local = datetime.fromisoformat(match[1])
        suffix = match[2] or ""
        if suffix in {"UTC", "GMT"}:
            tz = UTC
        elif re.fullmatch(r"[+-]\d{2}(?::?\d{2})?", suffix):
            return datetime.fromisoformat(match[1] + suffix).astimezone(UTC)
        else:
            tz = ZoneInfo(log_timezone)
        return local.replace(tzinfo=tz).astimezone(UTC)
    except (ValueError, ZoneInfoNotFoundError):
        return None


def parse_log_tail(
    data: bytes,
    log_format: str,
    database: str,
    log_timezone: str,
    *,
    offset: int = 0,
    levels: list[str] | None = None,
    since: str | None = None,
    limit: int = 50,
) -> dict:
    text = data.decode("utf-8", errors="replace")
    since_time = parse_since(since)
    wanted = set(levels or LEVELS)
    entries = []
    skipped = 0
    records = []
    if log_format == "jsonlog":
        lines = text.splitlines()
        if offset and lines:
            lines = lines[1:]  # Conservative: the first line may be partial.
        for line in lines:
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    records.append(row)
                else:
                    skipped += 1
            except (ValueError, RecursionError):
                skipped += 1
    elif log_format == "csvlog":
        # Recover complete CSV records after a bounded tail begins mid-record.
        starts = list(re.finditer(r'(?m)^"?\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', text))
        for index, start in enumerate(starts):
            end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
            try:
                rows = list(
                    csv.reader(io.StringIO(text[start.start() : end]), strict=True)
                )
                if len(rows) != 1 or len(rows[0]) < 23:
                    skipped += 1
                    continue
                row = rows[0]
                records.append(
                    {
                        "timestamp": row[0],
                        "dbname": row[2],
                        "pid": row[3],
                        "error_severity": row[11],
                        "state_code": row[12],
                        "message": row[13],
                    }
                )
            except csv.Error:
                skipped += 1
    else:
        raise QueryRejected("only PostgreSQL jsonlog and csvlog are supported")
    for row in records:
        level = row.get("error_severity")
        timestamp = _timestamp(row.get("timestamp"), log_timezone)
        dbname = row.get("dbname")
        if timestamp is None or level not in LEVELS:
            skipped += 1
            continue
        if dbname not in (None, "", database) or level not in wanted:
            continue
        if since_time and timestamp < since_time:
            continue
        code = row.get("state_code", "")
        code = (
            code if isinstance(code, str) and re.fullmatch(r"[A-Z0-9]{5}", code) else ""
        )
        message = row.get("message", "")
        message = message if isinstance(message, str) else ""
        entry = {
            "timestamp": timestamp.isoformat(),
            "level": level,
            "sqlstate": code,
            "event": safe_event(message, code),
            "scope": "database" if dbname else "instance",
        }
        # Extract only a bounded numeric duration from a fixed known message shape.
        duration = re.match(r"^duration: ([0-9]{1,10}(?:\.[0-9]{1,6})?) ms", message)
        if duration:
            entry["duration_ms"] = float(duration[1])
        entries.append(entry)
    entries.sort(key=lambda item: item["timestamp"], reverse=True)
    return {
        "entries": entries[:limit],
        "returned": min(limit, len(entries)),
        "truncated": offset > 0 or len(entries) > limit,
        "skipped_records": skipped,
        "raw_messages_returned": False,
        "sql_text_returned": False,
        "sensitive_fields_returned": False,
        "persisted": False,
    }


def read_logs(
    connection: psycopg.Connection,
    database: str,
    *,
    levels: list[str] | None = None,
    since: str | None = None,
    limit: int = 50,
    max_bytes: int = 65536,
) -> dict:
    filters = {"limit": limit, "max_bytes": max_bytes}
    if levels is not None:
        filters["levels"] = levels
    if since is not None:
        filters["since"] = since
    validate_log_filters(filters)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.current_setting('log_destination') AS destinations, "
            "pg_catalog.current_setting('logging_collector') AS collector, "
            "pg_catalog.current_setting('log_timezone') AS timezone, "
            "pg_catalog.current_setting('server_encoding') AS encoding"
        )
        config = cursor.fetchone() or {}
        destinations = set(
            str(config.get("destinations", "")).replace(" ", "").split(",")
        )
        log_format = next(
            (fmt for fmt in ("jsonlog", "csvlog") if fmt in destinations), None
        )
        if config.get("collector") != "on" or log_format is None:
            raise QueryRejected(
                "数据库未配置可读取的 JSON/CSV 结构化日志。",
                code="LOG_CONFIGURATION_UNSUPPORTED",
                hint="由管理员核验 logging_collector 和 jsonlog/csvlog；工具不自动修改数据库配置。",
            )
        if config.get("encoding") != "UTF8":
            raise QueryRejected(
                "当前日志读取器仅支持 UTF8 服务器编码。",
                code="LOG_ENCODING_UNSUPPORTED",
                hint="使用受支持的结构化日志编码或扩展对应日志解码器。",
            )
        cursor.execute(
            "SELECT pg_catalog.pg_current_logfile(%s) AS filename", (log_format,)
        )
        current = (cursor.fetchone() or {}).get("filename")
        if not isinstance(current, str) or not current or "\x00" in current:
            raise QueryRejected(
                "数据库没有当前结构化日志文件。",
                code="LOG_NOT_AVAILABLE",
                hint="检查日志收集器运行状态。",
            )
        # Filename is obtained only from the server, then matched against its log directory.
        basename = current.replace("\\", "/").rsplit("/", 1)[-1]
        cursor.execute(
            "SELECT size FROM pg_catalog.pg_ls_logdir() WHERE name = %s", (basename,)
        )
        info = cursor.fetchone()
        if not info:
            raise QueryRejected(
                "当前日志文件未能通过服务器日志目录核验。",
                code="LOG_FILE_UNVERIFIED",
                retryable=True,
                hint="日志可能正在轮转；稍后重试，不要提交文件路径。",
            )
        offset = max(0, int(info["size"]) - max_bytes)
        cursor.execute(
            "SELECT pg_catalog.pg_read_binary_file(%s, %s, %s, true) AS content",
            (current, offset, max_bytes),
        )
        content = (cursor.fetchone() or {}).get("content")
        if content is None:
            raise QueryRejected(
                "日志在读取期间发生轮转。",
                code="LOG_ROTATED",
                retryable=True,
                hint="重新提交同一日志读取请求。",
            )
        data = bytes(content)
        if len(data) > max_bytes:
            raise QueryRejected(
                "日志返回量超过请求的字节上限。",
                code="LOG_LIMIT_EXCEEDED",
                hint="停止读取并核验日志读取实现。",
            )
    result = parse_log_tail(
        data,
        log_format,
        database,
        str(config["timezone"]),
        offset=offset,
        levels=levels,
        since=since,
        limit=limit,
    )
    return {
        **result,
        "log_format": log_format,
        "bytes_read": len(data),
        "scope_description": "current log tail for the selected database plus instance-wide events",
    }
