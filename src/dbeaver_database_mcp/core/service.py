"""Database-neutral orchestration and verified adapter routing."""

from __future__ import annotations

from typing import Any

from .adapter import DatabaseAdapter, QueryMode
from .config import Settings
from .connection import DBeaverConnection, DBeaverConnectionStore
from .errors import QueryRejected, error_stage
from .registry import adapter_class


class DatabaseService:
    def __init__(
        self,
        settings: Settings,
        store: DBeaverConnectionStore | None = None,
        database_type: str | None = None,
    ):
        self.settings = settings
        self.store = store or DBeaverConnectionStore(settings)
        self.database_type = database_type
        if database_type is not None:
            adapter_class(database_type)

    def _route(self, identifier: str) -> tuple[DatabaseAdapter, DBeaverConnection]:
        with error_stage("resolve_connection"):
            source = self.store.resolve(identifier)
            actual = source.database_type
            if self.database_type is not None and self.database_type != actual:
                raise QueryRejected(
                    "database_type 与 DBeaver 连接的实际数据库类型不一致。",
                    code="DATABASE_TYPE_MISMATCH",
                    path="database_type",
                    hint="使用 list_dbeaver_connections 返回的 database_type。",
                )
            return adapter_class(actual)(self.settings), source

    def configured_context(self, identifier: str, database: str = "", schema: str = ""):
        _, source = self._route(identifier)
        return source.model_identity(schema or source.default_schema) | {
            "database": database or source.database,
        }

    def list_connections(self) -> dict[str, Any]:
        summaries, unavailable = [], []
        sources = self.store.connections(self.database_type)
        unavailable.extend(self.store.unavailable)
        for source in sources:
            adapter = adapter_class(source.database_type)(self.settings)
            try:
                result = adapter.list_databases(source)
                active_schema = result.get("schema") or source.default_schema
                summaries.append(
                    {
                        **source.model_identity(active_schema),
                        "default_database": source.database,
                        "default_schema": active_schema,
                        "databases": result["databases"],
                        "database_count": len(result["databases"]),
                        "provider": source.provider,
                        "driver": source.driver,
                        "dbeaver_read_only_flag": source.dbeaver_read_only,
                        "mcp_enforced_read_only": True,
                        "read_only": result.get(
                            "read_only",
                            adapter.capabilities().get(
                                "read_only", {"transaction": True}
                            ),
                        ),
                    }
                )
            except Exception as exc:
                from .errors import translate_error

                unavailable.append(
                    {
                        **source.model_identity(source.default_schema),
                        "code": translate_error(exc).code,
                    }
                )
        return {
            **self.store.project_info.identity(),
            "connections": summaries,
            "returned": len(summaries),
            "unavailable": unavailable,
        }

    def list_databases(self, identifier: str):
        adapter, source = self._route(identifier)
        return adapter.list_databases(source)

    def status(self, identifier: str, database: str):
        adapter, source = self._route(identifier)
        return adapter.status(source, database)

    def list_schemas(self, identifier: str, database: str):
        adapter, source = self._route(identifier)
        return adapter.list_schemas(source, database)

    def scan_database(self, identifier: str, database: str, schema: str, **options):
        adapter, source = self._route(identifier)
        return adapter.scan_database(source, database, schema, **options)

    def search_catalog(self, identifier: str, database: str, query: str, **options):
        adapter, source = self._route(identifier)
        return adapter.search_catalog(source, database, query, **options)

    def table_context(self, identifier: str, database: str, schema: str, table: str):
        adapter, source = self._route(identifier)
        return adapter.table_context(source, database, schema, table)

    def list_routines(self, identifier: str, database: str, **options):
        adapter, source = self._route(identifier)
        return adapter.list_routines(source, database, **options)

    def view_context(
        self, identifier: str, database: str, schema: str, view: str, **options
    ):
        adapter, source = self._route(identifier)
        return adapter.view_context(source, database, schema, view, **options)

    def routine_context(
        self, identifier: str, database: str, schema: str, routine: str, **options
    ):
        adapter, source = self._route(identifier)
        return adapter.routine_context(source, database, schema, routine, **options)

    def structured_query(
        self,
        identifier: str,
        database: str,
        schema: str,
        query: dict,
        *,
        mode: QueryMode = "execute",
        timeout_ms: int | None = None,
    ):
        adapter, source = self._route(identifier)
        return adapter.structured_query(
            source, database, schema, query, mode=mode, timeout_ms=timeout_ms
        )

    def read_database_settings(self, identifier: str, database: str, names=None):
        adapter, source = self._route(identifier)
        return adapter.read_database_settings(source, database, names)

    def read_postgresql_logs(self, identifier: str, database: str, **filters):
        adapter, source = self._route(identifier)
        return adapter.read_postgresql_logs(source, database, **filters)
