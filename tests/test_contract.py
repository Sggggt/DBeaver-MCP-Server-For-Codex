"""Portable contract and routing checks; no saved connections are accessed."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from dbeaver_database_mcp import __version__
from dbeaver_database_mcp.core import mcp_server
from dbeaver_database_mcp.core.config import Settings
from dbeaver_database_mcp.core.connection import (
    DBeaverConnection,
    DBeaverConnectionStore,
)
from dbeaver_database_mcp.core.errors import QueryRejected
from dbeaver_database_mcp.core.registry import ADAPTERS, adapter_class
from dbeaver_database_mcp.core.service import DatabaseService


def saved_connection(database_type="oracle"):
    return DBeaverConnection(
        "id",
        "name",
        database_type,
        "driver",
        "service",
        "S",
        False,
        "localhost",
        1521,
        "reader",
        "",
        options={"service_name": "service"},
    )


def test_all_tools_have_closed_schemas_and_typed_routing():
    for tool in mcp_server.TOOLS:
        schema = tool["inputSchema"]
        Draft202012Validator.check_schema(schema)
        assert schema["additionalProperties"] is False
        assert schema["properties"]["database_type"]["enum"] == list(ADAPTERS)
        assert not {"sql", "password", "host", "dsn"} & set(schema["properties"])
        assert tool["annotations"]["readOnlyHint"] is True


@pytest.mark.parametrize("database_type", ADAPTERS)
def test_concrete_adapters_implement_entire_contract(database_type):
    assert not adapter_class(database_type).__abstractmethods__
    result = mcp_server._handle_tool(
        "get_query_capabilities", {"database_type": database_type}
    )
    assert result["database_type"] == database_type
    assert result["raw_sql"] is False


def test_mismatched_type_is_rejected_before_connect(monkeypatch):
    class Store:
        def resolve(self, identifier):
            return saved_connection()

    monkeypatch.setattr(
        adapter_class("oracle"), "status", lambda *a: pytest.fail("must not connect")
    )
    service = DatabaseService(Settings(Path("unused")), Store(), "postgresql")
    with pytest.raises(QueryRejected, match="database_type") as caught:
        service.status("id", "service")
    assert caught.value.code == "DATABASE_TYPE_MISMATCH"


def test_unknown_database_type_rejected_before_configuration(monkeypatch):
    monkeypatch.setattr(
        mcp_server, "_service", lambda *a: pytest.fail("must not load credentials")
    )
    with pytest.raises(QueryRejected):
        mcp_server._handle_tool(
            "list_databases", {"connection": "id", "database_type": "../../module"}
        )


def test_resolution_does_not_parse_unrelated_connections(monkeypatch):
    store = DBeaverConnectionStore(Settings(Path("unused")))
    monkeypatch.setattr(
        store,
        "_read_data_sources",
        lambda: {
            "id": {
                "provider": "postgresql",
                "name": "name",
                "configuration": {"host": "localhost", "database": "db"},
            },
            "other": {
                "provider": "oracle",
                "name": "unsupported",
                "configuration": {"url": "unsupported"},
            },
        },
    )
    monkeypatch.setattr(
        store,
        "_read_credentials",
        lambda: {"id": {"#connection": {"user": "reader", "password": ""}}},
    )
    assert store.resolve("id").database_type == "postgresql"
    assert len(store.connections()) == 1
    assert store.unavailable[0]["connection_id"] == "other"


def test_stable_stdio_entry_point():
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "get_query_capabilities",
                "arguments": {"database_type": "oracle"},
            },
        },
    ]
    process = subprocess.run(
        [sys.executable, "-m", "dbeaver_database_mcp.mcp_server"],
        input="".join(json.dumps(m) + "\n" for m in messages),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=15,
        check=True,
    )
    responses = [json.loads(line) for line in process.stdout.splitlines()]
    assert responses[0]["result"]["serverInfo"]["version"] == __version__
    assert not responses[1]["result"].get("isError")
    assert process.stderr == ""
