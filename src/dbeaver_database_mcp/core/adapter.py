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


def resolve_routine(rows: list[Result]) -> Result:
    """Resolve only candidates already matched by saved connection/schema/name."""
    if not rows:
        raise QueryRejected(
            "过程或函数不存在或当前账号不可见。",
            code="ROUTINE_NOT_FOUND",
            path="routine",
            stage="catalog",
        )
    if len(rows) != 1:
        raise QueryRejected(
            "过程或函数名称对应多个重载。",
            code="ROUTINE_AMBIGUOUS",
            path="routine_id",
            stage="catalog",
            hint="使用 list_routines 返回的精确 routine_id，并保持原 connection/database/schema/routine/package。",
        )
    return rows[0]


def routine_definition(text, total_chars, offset, status, returned_chars=None):
    """Normalize bounded source fragments; the returned SQL is data, never input."""
    available = status == "available"
    text = (text or "") if available else None
    end = offset + (len(text or "") if returned_chars is None else returned_chars)
    more = available and total_chars is not None and end < total_chars
    return {
        "definition": text,
        "definition_status": status,
        "definition_offset": offset,
        "definition_total_chars": total_chars if available else None,
        "definition_truncated": bool(more),
        "next_definition_offset": end if more else None,
        "definition_offset_units": "database_characters",
    }


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
    """Implementations own SQL, binding, catalog validation and read restrictions.

    Inputs are validated JSON and saved DBeaver connections. Implementations must
    enforce native privileges, verify executable dependencies before planning,
    bind every data value, bound transfers, and always roll back and close.
    Prefer server-enforced read-only transactions where available. Adapters for
    engines without that mode must disclose it and enforce a closed SELECT
    language plus executable dependency checks; routing intent is not proof.
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

    def list_routines(
        self,
        source: DBeaverConnection,
        database: str,
        *,
        schema: str = "",
        name_pattern: str = "",
        routine_types: list[str] | None = None,
        limit: int = 50,
    ) -> Result:
        unsupported("routine_catalog")

    def routine_context(
        self,
        source: DBeaverConnection,
        database: str,
        schema: str,
        routine: str,
        *,
        routine_id: str = "",
        package: str = "",
        include_definition: bool = True,
        definition_part: str = "definition",
        definition_offset: int = 0,
        max_definition_chars: int = 4000,
    ) -> Result:
        unsupported("routine_catalog")

    def read_postgresql_logs(
        self,
        source: DBeaverConnection,
        database: str,
        **filters: Any,
    ) -> Result:
        unsupported("postgresql_logs")
