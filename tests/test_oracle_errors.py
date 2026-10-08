"""Wrapped connection errors retain coded causes without private messages."""

import errno
from types import SimpleNamespace

import oracledb
import pytest

from dbeaver_database_mcp.adapters.oracle.errors import translate_database_error


def error(code, message="private-address private-password private-sql"):
    return oracledb.DatabaseError(
        SimpleNamespace(full_code=code, code=0, message=message)
    )


@pytest.mark.parametrize(
    "root,expected",
    [
        ("DPY-3010", "UNSUPPORTED_DATABASE_VERSION"),
        ("DPY-3015", "UNSUPPORTED_AUTHENTICATION"),
        ("DPY-3001", "UNSUPPORTED_NETWORK_ENCRYPTION"),
        ("DPY-6001", "SERVICE_NOT_REGISTERED"),
        ("DPY-6003", "SID_NOT_REGISTERED"),
        ("ORA-01017", "AUTHENTICATION_FAILED"),
    ],
)
def test_wrapped_driver_cause_is_classified_without_private_text(root, expected):
    outer = error("DPY-6005")
    outer.__cause__ = error(root)
    result = translate_database_error(outer, "connect")
    assert result.code == expected
    assert result.details["root_database_error_code"] == root
    assert result.details["database_error_code"] == "DPY-6005"
    assert "private" not in str(result) + str(result.details)


def test_windows_socket_refusal_is_classified():
    outer = error("DPY-6005")
    cause = OSError(errno.ECONNREFUSED, "private endpoint")
    cause.winerror = 10061
    outer.__cause__ = cause
    result = translate_database_error(outer, "connect")
    assert result.code == "CONNECTION_FAILED"
    assert result.details["os_error_kind"] == "connection_refused"
    assert "private" not in str(result.details)


def test_flattened_connect_error_retains_only_codes():
    result = translate_database_error(
        error("DPY-6005", "private connection DPY-3010: private database"), "connect"
    )
    assert result.code == "UNSUPPORTED_DATABASE_VERSION"
    assert result.details["database_error_codes"] == ["DPY-6005", "DPY-3010"]
    assert "private" not in str(result.details)


def test_query_values_cannot_forge_connection_error_classification():
    result = translate_database_error(
        error("ORA-01722", "private value DPY-3010"), "execute"
    )
    assert result.details["database_error_codes"] == ["ORA-01722"]


def test_cyclic_causes_are_bounded():
    outer = error("DPY-6005")
    outer.__cause__ = outer
    assert translate_database_error(outer, "connect").code == "CONNECTION_FAILED"
