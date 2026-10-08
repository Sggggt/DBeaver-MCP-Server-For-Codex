"""Dependency, session and transfer checks with in-memory driver doubles."""

from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import oracledb
import pytest
from sqlglot import parse
from test_contract import saved_connection

from dbeaver_database_mcp.adapters.oracle import adapter, session
from dbeaver_database_mcp.adapters.oracle.catalog_guard import CatalogGuard
from dbeaver_database_mcp.core.catalog import Table
from dbeaver_database_mcp.core.config import Settings
from dbeaver_database_mcp.core.errors import QueryRejected, error_payload


class CatalogSession:
    def __init__(
        self, *, virtual=False, policy=False, external=False, index="NORMAL", view=None
    ):
        self.virtual, self.policy, self.external, self.index, self.view = (
            virtual,
            policy,
            external,
            index,
            view,
        )

    def rows(self, statement, binds, *, limit):
        assert "SYS.ALL_" in statement
        assert f"'{binds.get('name')}'" not in statement
        if "SYS.ALL_OBJECTS" in statement:
            return [
                {
                    "object_id": 1,
                    "object_type": "VIEW" if binds["name"] == "V" else "TABLE",
                }
            ]
        if "SYS.ALL_TAB_COLS" in statement:
            return [
                {
                    "column_name": "C",
                    "data_type": "VARCHAR2",
                    "data_type_owner": None,
                    "virtual_column": "YES" if self.virtual else "NO",
                    "hidden_column": "NO",
                }
            ]
        if "SYS.ALL_EXTERNAL_TABLES" in statement:
            return [{"found": 1}] if self.external else []
        if "SYS.ALL_POLICIES" in statement:
            return [{"found": 1}] if self.policy else []
        if "SYS.ALL_INDEXES" in statement:
            return [{"index_type": self.index}]
        if "SYS.ALL_DEPENDENCIES" in statement:
            return []
        if "text_length" in statement:
            return [{"text_length": len(self.view)}]
        if "SYS.ALL_VIEWS" in statement:
            return [{"text": self.view}]
        pytest.fail("unexpected catalog operation")


@pytest.mark.parametrize(
    "options,code",
    [
        ({"virtual": True}, "UNSAFE_VIRTUAL_COLUMN"),
        ({"policy": True}, "UNSAFE_POLICY"),
        ({"external": True}, "UNVERIFIED_FOREIGN_READER"),
        ({"index": "FUNCTION-BASED NORMAL"}, "UNSAFE_INDEX"),
        ({"index": "DOMAIN"}, "UNSAFE_INDEX"),
    ],
)
def test_executable_or_remote_dependencies_are_rejected(options, code):
    with pytest.raises(QueryRejected) as caught:
        CatalogGuard(CatalogSession(**options)).relation("S", "T")
    assert caught.value.code == code


@pytest.mark.parametrize(
    "definition",
    [
        "SELECT C FROM S.T",
        "SELECT LOWER(C) AS C FROM S.T",
        "SELECT CASE WHEN C IS NULL THEN 'x' ELSE C END AS C FROM S.T",
        "SELECT COUNT(*) AS C FROM S.T",
    ],
)
def test_simple_view_definitions_are_audited(definition):
    guard = CatalogGuard(CatalogSession(view=definition))
    guard.relation("S", "V")
    assert ("S", "T") in guard.catalog


@pytest.mark.parametrize(
    "definition",
    [
        "SELECT dangerous(C) FROM S.T",
        "SELECT S.dangerous(C) FROM S.T",
        "SELECT seq.NEXTVAL FROM S.T",
        "SELECT C FROM T@remote",
        "SELECT C FROM S.T FOR UPDATE",
        "SELECT C FROM S.T; DELETE FROM S.T",
    ],
)
def test_view_sql_cannot_hide_side_effects(definition):
    with pytest.raises(QueryRejected):
        CatalogGuard(CatalogSession(view=definition)).relation("S", "V")


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = [
            ("C", oracledb.DB_TYPE_VARCHAR, None, None, None, None, True)
        ]
        self.records = iter(())

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, statement, parameters):
        self.connection.calls.append((statement, parameters))
        if statement.startswith("SELECT") and '"__result"' in statement:
            self.records = iter(self.connection.records)

    def fetchone(self):
        return next(self.records, None)

    def setinputsizes(self, **types):
        self.connection.input_types.update(types)


class Connection:
    version = "19.0.0"

    def __init__(self, records=()):
        self.calls, self.records, self.input_types = [], records, {}
        self.rollback_calls = 0
        self.closed = False
        self.cancelled = False

    def cursor(self):
        return Cursor(self)

    def rollback(self):
        self.rollback_calls += 1

    def close(self):
        self.closed = True

    def cancel(self):
        self.cancelled = True


def test_read_only_setup_and_cleanup_on_failure(monkeypatch):
    connection = Connection()
    monkeypatch.setattr(oracledb, "connect", lambda **kwargs: connection)
    with pytest.raises(QueryRejected):
        with session.open_session(
            saved_connection(), Settings(Path("unused")), "service"
        ):
            raise QueryRejected("rejected")
    commands = [sql for sql, _ in connection.calls]
    assert commands[-1] == "SET TRANSACTION READ ONLY"
    assert connection.autocommit is False and connection.closed
    assert connection.rollback_calls == 2


def test_service_switch_is_rejected_before_connect(monkeypatch):
    monkeypatch.setattr(
        oracledb, "connect", lambda **kwargs: pytest.fail("must not connect")
    )
    with pytest.raises(QueryRejected):
        with session.open_session(
            saved_connection(), Settings(Path("unused")), "other"
        ):
            pass


@pytest.mark.parametrize("timeout", [0, True, "100", 99, 30001])
def test_invalid_timeout_is_rejected_before_connect(monkeypatch, timeout):
    monkeypatch.setattr(
        oracledb, "connect", lambda **kwargs: pytest.fail("must not connect")
    )
    with pytest.raises(QueryRejected):
        with session.open_session(
            saved_connection(), Settings(Path("unused")), "service", timeout
        ):
            pass


def test_success_cleanup_failure_is_reported_and_connection_closed(monkeypatch):
    connection = Connection()

    def rollback():
        connection.rollback_calls += 1
        if connection.rollback_calls > 1:
            raise RuntimeError("private cleanup value")

    connection.rollback = rollback
    monkeypatch.setattr(oracledb, "connect", lambda **kwargs: connection)
    with pytest.raises(Exception) as caught:
        with session.open_session(
            saved_connection(), Settings(Path("unused")), "service"
        ):
            pass
    assert caught.value.cleanup_failed and caught.value.stage == "cleanup"
    assert connection.closed and "private" not in str(caught.value)


def test_timeout_cancel_and_expiry():
    connection = Connection()
    bounded = session.Session(connection, 10000)
    try:
        bounded.cancel()
        assert connection.cancelled
        with pytest.raises(QueryRejected) as caught:
            bounded.check()
        assert caught.value.code == "QUERY_TIMEOUT"
    finally:
        bounded.stop()


def test_query_failure_survives_cleanup_failure(monkeypatch):
    connection = Connection()

    def rollback():
        connection.rollback_calls += 1
        if connection.rollback_calls > 1:
            raise RuntimeError("private cleanup value")

    connection.rollback = rollback
    monkeypatch.setattr(oracledb, "connect", lambda **kwargs: connection)
    with pytest.raises(QueryRejected) as caught:
        with session.open_session(
            saved_connection(), Settings(Path("unused")), "service"
        ):
            raise QueryRejected("rejected")
    assert caught.value.cleanup_failed and connection.closed
    assert "private" not in str(error_payload(caught.value))


def test_bounded_execution_and_clipped_transport(monkeypatch):
    connection = Connection(records=[("abcd", Decimal(20)), ("next", Decimal(4))])

    @contextmanager
    def opened(*args):
        bounded = session.Session(connection, 10000)
        try:
            yield bounded
        finally:
            bounded.stop()

    monkeypatch.setattr(adapter, "open_session", opened)
    monkeypatch.setattr(adapter.OracleAdapter, "_context", lambda *a: ("S", "db"))
    monkeypatch.setattr(
        CatalogGuard,
        "prepare",
        lambda *a: {("S", "T"): Table(1, {"C": "VARCHAR2"}, frozenset({"C"}))},
    )
    oracle = adapter.OracleAdapter(Settings(Path("unused"), max_cell_chars=4))
    query = {
        "table": "T",
        "alias": "t",
        "columns": [{"source": "t", "column": "C"}],
        "limit": 1,
    }
    result = oracle.structured_query(saved_connection(), "service", "S", query)
    assert result["rows"] == [[{"truncated": True, "preview": "abcd"}]]
    assert result["truncated"] and result["truncation_reason"] == "row_limit"
    assert connection.calls[-1][1]["cell_limit"] == 4
    assert "SUBSTR" in connection.calls[-1][0]


def test_oracle_explain_never_writes(monkeypatch):
    monkeypatch.setattr(
        adapter, "open_session", lambda *a: pytest.fail("must not open a session")
    )
    query = {"select": [{"as": "x", "expr": {"value": 1}}], "limit": 1}
    with pytest.raises(QueryRejected) as caught:
        adapter.OracleAdapter(Settings(Path("unused"))).structured_query(
            saved_connection(), "service", "S", query, mode="explain"
        )
    assert caught.value.code == "UNSUPPORTED_CAPABILITY"


def test_catalog_metadata_uses_common_response_fields(monkeypatch):
    class MetadataSession:
        def rows(self, statement, parameters=None, *, limit):
            assert len(parse(statement, read="oracle")) == 1
            if parameters:
                assert "; DELETE" not in statement
            if "GROUP BY owner" in statement:
                return [
                    {
                        "name": "S",
                        "table_count": Decimal(1),
                        "view_count": Decimal(0),
                        "object_count": Decimal(1),
                    }
                ]
            if 'AS "object"' in statement:
                return [
                    {
                        "schema": "S",
                        "object": "T",
                        "object_type": "TABLE",
                        "column_name": "C",
                        "data_type": "VARCHAR2",
                        "object_comment": "",
                        "column_comment": "",
                    }
                ]
            if 'AS "approximate_rows"' in statement:
                return [
                    {
                        "name": "T",
                        "type": "TABLE",
                        "comment": "",
                        "approximate_rows": Decimal(1),
                    }
                ]
            pytest.fail("unexpected metadata query")

    @contextmanager
    def opened(*args):
        yield MetadataSession()

    monkeypatch.setattr(adapter, "open_session", opened)
    monkeypatch.setattr(adapter.OracleAdapter, "_context", lambda *a: ("S", "db"))
    oracle = adapter.OracleAdapter(Settings(Path("unused")))
    source = saved_connection()
    schemas = oracle.list_schemas(source, "service")
    assert schemas["schema_contexts"][0]["table_count"] == 1
    catalog = oracle.scan_database(source, "service", "S", name_pattern="'; DELETE")
    assert catalog["objects"][0]["type"] == "table"
    assert catalog["objects"][0]["approximate_rows"] == 1
    search = oracle.search_catalog(source, "service", "'; DELETE", schema="S")
    assert search["matches"][0]["object"] == "T"
    assert search["matches"][0]["column"] == "C"
