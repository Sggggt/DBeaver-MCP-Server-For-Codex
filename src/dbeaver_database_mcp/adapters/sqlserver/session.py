"""Per-call TDS transactions with request deadlines and rollback/close cleanup.

SQL Server has no SET TRANSACTION READ ONLY. readonly=True is routing intent;
the closed SELECT compiler and dependency guard enforce the MCP read boundary.
"""

from contextlib import contextmanager
from time import monotonic

import pytds

from ...core.errors import QueryRejected, error_stage, translate_error


class Session:
    def __init__(self, connection, timeout_ms):
        self.connection = connection
        self.deadline = monotonic() + timeout_ms / 1000
        self.major_version = 0

    def check(self):
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise QueryRejected(
                "SQL Server 请求超时。", code="QUERY_TIMEOUT", retryable=True
            )
        # python-tds uses a socket timeout for each round trip. Recompute it
        # before every execute/fetch, so catalog work cannot reset the budget.
        self.connection._tds_socket.sock.settimeout(remaining)

    def execute(self, cursor, statement, parameters=None):
        self.check()
        cursor.execute(statement, parameters or {})
        self.check()

    def rows(self, statement, parameters=None, *, limit=1000):
        with self.connection.cursor() as cursor:
            self.execute(cursor, statement, parameters)
            names = [column[0].lower() for column in cursor.description]
            rows = []
            for _ in range(limit):
                self.check()
                row = cursor.fetchone()
                if row is None:
                    break
                rows.append(dict(zip(names, row, strict=True)))
            self.check()
            return rows


@contextmanager
def open_session(source, settings, database, timeout_ms=None):
    target = source.for_database(database)
    budget = settings.statement_timeout_ms if timeout_ms is None else timeout_ms
    if type(budget) is not int or not 100 <= budget <= min(
        settings.statement_timeout_ms, 30000
    ):
        raise QueryRejected(
            "timeout_ms 超出允许范围。", code="INVALID_PARAMETER", path="timeout_ms"
        )
    with error_stage("connect"):
        connection = pytds.connect(
            server=target.host,
            port=target.port,
            database=target.database,
            user=target.username,
            password=target.password,
            login_timeout=settings.connect_timeout_seconds,
            timeout=budget / 1000,
            appname="DBeaver Database MCP",
            autocommit=False,
            readonly=True,
            pooling=False,
            use_mars=False,
            disable_connect_retry=True,
        )
    error = None
    try:
        session = Session(connection, budget)
        with error_stage("session"), connection.cursor() as cursor:
            # Fixed settings only; no persistent database changes.
            lock_timeout = max(1, min(settings.lock_timeout_ms, budget))
            session.execute(
                cursor,
                f"SET LOCK_TIMEOUT {lock_timeout}; SET XACT_ABORT ON; SET TEXTSIZE 16384",
            )
        observed = session.rows(
            "SELECT CAST(SERVERPROPERTY('ProductVersion') AS varchar(32)) AS version, "
            "CAST(DATABASEPROPERTYEX(DB_NAME(),'Updateability') AS varchar(32)) AS updateability",
            limit=1,
        )[0]
        session.major_version = int(observed["version"].split(".")[0])
        session.database_read_only = observed["updateability"] == "READ_ONLY"
        if session.major_version < 10:
            raise QueryRejected(
                "SQL Server 适配器要求 SQL Server 2008 或以上。",
                code="UNSUPPORTED_DATABASE_VERSION",
            )
        yield session
    except BaseException as exc:
        error = translate_error(exc) if isinstance(exc, Exception) else exc
        if error is exc:
            raise
        raise error from exc
    finally:
        # Restore a small, independent cleanup budget even after a timeout.
        try:
            connection._tds_socket.sock.settimeout(
                min(2, settings.connect_timeout_seconds)
            )
        except Exception:
            pass
        cleanup_error = None
        for operation in (connection.rollback, connection.close):
            try:
                operation()
            except BaseException as exc:
                cleanup_error = cleanup_error or (
                    translate_error(exc, "cleanup")
                    if isinstance(exc, Exception)
                    else exc
                )
        if cleanup_error:
            if error is not None:
                error.cleanup_failed = True
            else:
                cleanup_error.cleanup_failed = True
                raise cleanup_error
