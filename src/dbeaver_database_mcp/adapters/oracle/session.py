"""Bounded Oracle Thin sessions; READ ONLY is the first transaction statement."""

from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from threading import Event, Timer
from time import monotonic

import oracledb

from ...core.errors import QueryRejected, error_stage, translate_error


class Session:
    def __init__(self, connection, timeout_ms):
        self.connection = connection
        self.deadline = monotonic() + timeout_ms / 1000
        self.expired = Event()
        self.timer = Timer(timeout_ms / 1000, self.cancel)
        self.timer.daemon = True
        self.timer.start()

    def cancel(self):
        self.expired.set()
        try:
            self.connection.cancel()
        except Exception:
            pass

    def check(self):
        remaining = int((self.deadline - monotonic()) * 1000)
        if remaining <= 0 or self.expired.is_set():
            raise QueryRejected(
                "Oracle 请求超过执行时间上限。", code="QUERY_TIMEOUT", retryable=True
            )
        self.connection.call_timeout = max(1, remaining)

    def execute(self, cursor, statement, parameters=None):
        self.check()
        cursor.arraysize = 1
        cursor.prefetchrows = 1
        if parameters:
            types = {}
            for name, value in parameters.items():
                if isinstance(value, datetime):
                    types[name] = (
                        oracledb.DB_TYPE_TIMESTAMP_TZ
                        if value.tzinfo
                        else oracledb.DB_TYPE_TIMESTAMP
                    )
                elif isinstance(value, date):
                    types[name] = oracledb.DB_TYPE_DATE
            if types:
                cursor.setinputsizes(**types)
        cursor.execute(statement, parameters or {})
        self.check()

    def rows(self, statement, parameters=None, *, limit=1000):
        with self.connection.cursor() as cursor:
            self.execute(cursor, statement, parameters)
            names = [description[0].lower() for description in cursor.description]
            rows = []
            for _ in range(limit):
                self.check()
                row = cursor.fetchone()
                if row is None:
                    break
                rows.append(dict(zip(names, row, strict=True)))
            self.check()
            return rows

    def stop(self):
        self.timer.cancel()
        self.timer.join()


def number_handler(cursor, metadata):
    if metadata.type_code == oracledb.DB_TYPE_NUMBER:
        return cursor.var(
            str, size=128, arraysize=cursor.arraysize, outconverter=Decimal
        )


@contextmanager
def open_session(source, settings, database, timeout_ms=None):
    if database != source.database:
        raise QueryRejected(
            "Oracle 只能访问当前已保存连接指定的服务。",
            code="DATABASE_NOT_ACCESSIBLE",
            path="database",
            hint="为其他服务保存独立的 DBeaver 连接。",
        )
    budget = settings.statement_timeout_ms if timeout_ms is None else timeout_ms
    if type(budget) is not int or not 100 <= budget <= min(
        settings.statement_timeout_ms, 30000
    ):
        raise QueryRejected(
            "timeout_ms 超出允许范围。", code="INVALID_PARAMETER", path="timeout_ms"
        )
    with error_stage("connect"):
        connection = oracledb.connect(
            host=source.host,
            port=source.port,
            user=source.username,
            password=source.password,
            tcp_connect_timeout=settings.connect_timeout_seconds,
            retry_count=0,
            **source.options,
        )
    session = None
    try:
        connection.autocommit = False
        connection.outputtypehandler = number_handler
        version = tuple(int(part) for part in connection.version.split(".")[:2])
        if version < (12, 1):
            raise QueryRejected(
                "Oracle 适配器要求数据库版本 12.1 或以上。",
                code="UNSUPPORTED_DATABASE_VERSION",
            )
        session = Session(connection, budget)
        with error_stage("session"), connection.cursor() as cursor:
            session.execute(cursor, "ALTER SESSION SET NLS_NUMERIC_CHARACTERS = '.,'")
            session.execute(cursor, "ALTER SESSION SET QUERY_REWRITE_ENABLED = FALSE")
            connection.rollback()
            session.execute(cursor, "SET TRANSACTION READ ONLY")
        yield session
    except Exception as exc:
        error = translate_error(exc)
        if session:
            session.stop()
        try:
            connection.rollback()
        except Exception:
            error.cleanup_failed = True
        try:
            connection.close()
        except Exception:
            error.cleanup_failed = True
        if error is exc:
            raise
        raise error from exc
    else:
        session.stop()
        cleanup_error = None
        for operation in (connection.rollback, connection.close):
            try:
                operation()
            except Exception as exc:
                cleanup_error = cleanup_error or translate_error(exc, "cleanup")
                cleanup_error.cleanup_failed = True
        if cleanup_error:
            raise cleanup_error
