"""The stable contract between MCP orchestration and database implementations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, NoReturn

from .config import Settings
from .errors import QueryRejected

if TYPE_CHECKING:
    from .connection import DBeaverConnection

QueryMode = Literal["analyze", "explain", "execute"]
Result = dict[str, Any]


def unsupported(feature: str) -> NoReturn:
    raise QueryRejected(
        "该数据库适配器尚未支持此能力。",
        code="UNSUPPORTED_CAPABILITY",
        details={"feature": feature},
        hint="调用 get_query_capabilities 查看该数据库的能力范围。",
    )


@dataclass(frozen=True)
class ConnectionParameters:
    """Transport settings obtained only from the saved DBeaver configuration."""

    host: str = field(repr=False)
    port: int = field(repr=False)
    database: str
    default_schema: str = ""
    sslmode: str | None = field(default=None, repr=False)
    options: dict[str, str] = field(default_factory=dict, repr=False)


class DatabaseAdapter(ABC):
    """Implementations own SQL, binding, catalog validation and read-only sessions.

    Inputs are validated JSON and saved DBeaver connections. Implementations must
    enforce native privileges, verify executable dependencies before planning,
    bind every data value, bound transfers, and always roll back and close.
    Never accept SQL, transport overrides or credentials from tool arguments.
    """

    database_type: str

    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    @abstractmethod
    def parse_configuration(configuration: Result) -> ConnectionParameters:
        """Normalize saved transport settings; reject unsupported authentication/TLS."""

    @classmethod
    @abstractmethod
    def capabilities(cls) -> Result:
        """Return the implemented query subset and explicit limitations, without I/O."""

    @abstractmethod
    def list_databases(self, source: DBeaverConnection) -> Result: ...

    @abstractmethod
    def status(self, source: DBeaverConnection, database: str) -> Result: ...

    @abstractmethod
    def list_schemas(self, source: DBeaverConnection, database: str) -> Result: ...

    @abstractmethod
    def scan_database(
        self,
        source: DBeaverConnection,
        database: str,
        schema: str,
        *,
        name_pattern: str = "",
        object_types: list[str] | None = None,
        limit: int = 200,
    ) -> Result: ...

    @abstractmethod
    def search_catalog(
        self,
        source: DBeaverConnection,
        database: str,
        query: str,
        *,
        schema: str = "",
        limit: int = 50,
    ) -> Result: ...

    @abstractmethod
    def table_context(
        self,
        source: DBeaverConnection,
        database: str,
        schema: str,
        table: str,
    ) -> Result: ...

    @abstractmethod
    def structured_query(
        self,
        source: DBeaverConnection,
        database: str,
        schema: str,
        query: Result,
        *,
        mode: QueryMode = "execute",
        timeout_ms: int | None = None,
    ) -> Result: ...

    def read_database_settings(
        self,
        source: DBeaverConnection,
        database: str,
        names: list[str] | None = None,
    ) -> Result:
        unsupported("database_settings")

    def read_postgresql_logs(
        self,
        source: DBeaverConnection,
        database: str,
        **filters: Any,
    ) -> Result:
        unsupported("postgresql_logs")
