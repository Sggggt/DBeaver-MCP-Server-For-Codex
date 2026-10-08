"""Explicit adapter registration; caller input never becomes a module path."""

from importlib import import_module

from .adapter import DatabaseAdapter
from .errors import QueryRejected

# Add a database implementation here and its provider IDs below. No reflection
# over caller-provided names, filesystem scans or dynamic SQL fallbacks.
ADAPTERS = {
    "postgresql": ("postgresql.adapter", "PostgreSQLAdapter"),
    "oracle": ("oracle.adapter", "OracleAdapter"),
}
PROVIDERS = {"postgresql": "postgresql", "oracle": "oracle"}


def adapter_class(database_type: str) -> type[DatabaseAdapter]:
    registration = ADAPTERS.get(database_type)
    if registration is None:
        raise QueryRejected(
            "数据库类型没有注册适配器。",
            code="UNSUPPORTED_DATABASE_TYPE",
            path="database_type",
            hint="使用已注册的数据库类型。",
        )
    module, name = registration
    return getattr(import_module(f"..adapters.{module}", __package__), name)
