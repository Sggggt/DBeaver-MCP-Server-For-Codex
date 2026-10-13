from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row, tuple_row
from psycopg.types.json import set_json_loads

from ...core.adapter import (
    DatabaseAdapter,
    resolve_routine,
    routine_definition,
    unsupported,
)
from ...core.connection import DBeaverConnection
from ...core.errors import (
    error_stage,
    register_error_translator,
    translate_error,
)
from .catalog_guard import CatalogGuard
from .connection import parse_configuration
from .errors import translate_database_error
from .query import BuiltQuery, QueryRejected, compile_query, requested_relations

register_error_translator(translate_database_error)

RELKIND_NAMES = {
    "r": "table",
    "p": "partitioned_table",
    "v": "view",
    "m": "materialized_view",
    "f": "foreign_table",
}


class PostgreSQLAdapter(DatabaseAdapter):
    database_type = "postgresql"
    parse_configuration = staticmethod(parse_configuration)

    @classmethod
    def capabilities(cls):
        from ...core.query_schema import AGGREGATES, FUNCTIONS, OPS, TYPES, WINDOWS

        return {
            "database_type": cls.database_type,
            "functions": FUNCTIONS,
            "aggregates": AGGREGATES,
            "windows": WINDOWS,
            "conditions": OPS,
            "casts": TYPES,
            "object_types": list(RELKIND_NAMES.values()),
            "explain": True,
            "database_settings": True,
            "postgresql_logs": True,
            "routine_catalog": {
                "list": True,
                "parameters": True,
                "definition": True,
                "types": ["procedure", "function"],
                "definition_pagination": "characters",
                "conditional_types": {"procedure": "PostgreSQL 11+"},
                "dependencies": "catalog_recorded_direct",
                "execute": False,
            },
            "unsupported": [
                "unverified remote FDW side effects",
                "untrusted executable dependencies",
            ],
        }

    @contextmanager
    def _connection(
        self,
        identifier: DBeaverConnection,
        database: str = "",
        schema: str = "",
    ) -> Iterator[tuple[psycopg.Connection[Any], DBeaverConnection, dict[str, Any]]]:
        with error_stage("resolve_connection"):
            root = identifier
        target_database = database.strip() or root.database
        if target_database != root.database:
            with self._open_connection(root) as (discovery, _, _):
                available = {
                    item["name"] for item in self._visible_databases(discovery)
                }
            if target_database not in available:
                raise ValueError(
                    "database is not connectable through the selected DBeaver connection: "
                    + target_database
                )
        source = root.for_database(target_database)
        with self._open_connection(source, schema) as opened:
            yield opened

    @contextmanager
    def _open_connection(
        self,
        source: DBeaverConnection,
        schema: str = "",
    ) -> Iterator[tuple[psycopg.Connection[Any], DBeaverConnection, dict[str, Any]]]:
        with error_stage("connect"):
            connection = psycopg.connect(
                **self._connection_kwargs(source),
                row_factory=dict_row,
            )
        error = None
        try:
            set_json_loads(
                lambda data: json.loads(data, parse_float=Decimal), connection
            )
            connection.read_only = True
            connection.autocommit = False
            with error_stage("session"), connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("SET LOCAL search_path TO pg_catalog, pg_temp")
                cursor.execute("SET LOCAL row_security = on")
                cursor.execute("SET LOCAL constraint_exclusion = off")
                if schema:
                    cursor.execute(
                        "SELECT nspname,pg_catalog.has_schema_privilege(oid,'USAGE') AS usable "
                        "FROM pg_catalog.pg_namespace WHERE nspname=%s",
                        (schema,),
                    )
                    row = cursor.fetchone() or {}
                    if not row:
                        raise QueryRejected(
                            "指定 schema 不存在。",
                            code="SCHEMA_NOT_FOUND",
                            path="schema",
                            hint="调用 list_schemas 核验 database/schema。",
                        )
                    if not row["usable"]:
                        raise QueryRejected(
                            "当前账号没有该 schema 的 USAGE 权限。",
                            code="READ_PERMISSION_DENIED",
                            sqlstate="42501",
                            path="schema",
                            hint="使用数据库账号实际获准访问的 schema。",
                        )
                context = self._session_context(cursor, source)
                if schema:
                    context["schema"] = schema
                if context["database"] != source.database:
                    raise RuntimeError(
                        "database session does not match the DBeaver connection"
                    )
                if (
                    not context["transaction_read_only"]
                    or not context["default_read_only"]
                ):
                    raise RuntimeError("database session did not enter read-only mode")
            yield connection, source, context
        except BaseException as exc:
            error = translate_error(exc) if isinstance(exc, Exception) else exc
            if error is exc:
                raise
            raise error from exc
        finally:
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

    def _connection_kwargs(self, source: DBeaverConnection) -> dict[str, object]:
        options = " ".join(
            [
                "-c default_transaction_read_only=on",
                "-c search_path=pg_catalog,pg_temp",
                "-c row_security=on",
                f"-c statement_timeout={self.settings.statement_timeout_ms}",
                f"-c lock_timeout={self.settings.lock_timeout_ms}",
                "-c idle_in_transaction_session_timeout=30000",
            ]
        )
        kwargs: dict[str, object] = {
            "host": source.host,
            "port": source.port,
            "dbname": source.database,
            "user": source.username,
            "password": source.password,
            "connect_timeout": self.settings.connect_timeout_seconds,
            "application_name": "dbeaver-database-mcp-readonly",
            "options": options,
        }
        if source.sslmode:
            kwargs["sslmode"] = source.sslmode
        return kwargs

    @staticmethod
    def _session_context(
        cursor: psycopg.Cursor[Any], source: DBeaverConnection
    ) -> dict[str, Any]:
        cursor.execute(
            "SELECT current_database() AS database, current_schema() AS schema, "
            "current_setting('transaction_read_only') = 'on' AS transaction_read_only, "
            "current_setting('default_transaction_read_only') = 'on' AS default_read_only"
        )
        row = dict(cursor.fetchone() or {})
        active_schema = str(source.default_schema or "")
        if not active_schema:
            cursor.execute(
                "SELECT nspname AS schema FROM pg_namespace "
                "WHERE nspname <> 'information_schema' AND nspname !~ '^pg_' "
                "AND has_schema_privilege(current_user, nspname, 'USAGE') "
                "ORDER BY CASE WHEN nspname = 'public' THEN 0 ELSE 1 END, lower(nspname), nspname "
                "LIMIT 1"
            )
            active_schema = str((cursor.fetchone() or {}).get("schema") or "")
        if not active_schema:
            raise RuntimeError("DBeaver database has no visible schema")
        return {
            **source.model_identity(active_schema),
            "database": str(row.get("database") or source.database),
            "schema": active_schema,
            "transaction_read_only": bool(row.get("transaction_read_only")),
            "default_read_only": bool(row.get("default_read_only")),
        }

    @staticmethod
    def _response(context: dict[str, Any], **payload: Any) -> dict[str, Any]:
        return {
            "connection_id": context["connection_id"],
            "connection_name": context["connection_name"],
            "project_id": context.get("project_id", ""),
            "project_name": context.get("project_name", ""),
            "database": context["database"],
            "schema": context["schema"],
            "connection_source": "DBeaver workspace",
            "database_type": "postgresql",
            **payload,
        }

    def list_databases(self, identifier: DBeaverConnection) -> dict[str, Any]:
        source = identifier
        with self._connection(identifier, source.database) as (connection, _, context):
            databases = self._visible_databases(connection)
        return self._response(
            context,
            default_database=source.database,
            databases=databases,
            returned=len(databases),
        )

    def list_schemas(
        self, identifier: DBeaverConnection, database: str
    ) -> dict[str, Any]:
        if not database.strip():
            raise ValueError("database is required")
        with self._connection(identifier, database) as (connection, _, context):
            schema_contexts = self._visible_schema_contexts(connection)
        schemas = [item["name"] for item in schema_contexts]
        return self._response(
            context,
            schemas=schemas,
            schema_contexts=schema_contexts,
            returned=len(schemas),
        )

    @staticmethod
    def _visible_databases(connection: psycopg.Connection[Any]) -> list[dict[str, Any]]:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT d.datname AS name, d.datistemplate AS is_template, "
                "shobj_description(d.oid, 'pg_database') AS comment, "
                "d.datname = current_database() AS is_default "
                "FROM pg_database d "
                "WHERE d.datallowconn "
                "AND has_database_privilege(current_user, d.datname, 'CONNECT') "
                "ORDER BY CASE WHEN d.datname = current_database() THEN 0 ELSE 1 END, "
                "d.datistemplate, lower(d.datname), d.datname"
            )
            return [
                {
                    "name": str(row["name"]),
                    "is_default": bool(row["is_default"]),
                    "is_template": bool(row["is_template"]),
                    "comment": str(row["comment"] or ""),
                }
                for row in cursor.fetchall()
            ]

    @staticmethod
    def _visible_schemas(connection: psycopg.Connection[Any]) -> list[str]:
        return [
            item["name"]
            for item in PostgreSQLAdapter._visible_schema_contexts(connection)
        ]

    @staticmethod
    def _visible_schema_contexts(
        connection: psycopg.Connection[Any],
    ) -> list[dict[str, Any]]:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT n.nspname AS name, "
                "COUNT(c.oid) FILTER (WHERE c.relkind IN ('r','p','f')) AS table_count, "
                "COUNT(c.oid) FILTER (WHERE c.relkind IN ('v','m')) AS view_count, "
                "COUNT(c.oid) FILTER (WHERE c.relkind = ANY(%s)) AS object_count "
                "FROM pg_namespace n "
                "LEFT JOIN pg_class c ON c.relnamespace = n.oid "
                "WHERE n.nspname <> 'information_schema' AND n.nspname !~ '^pg_' "
                "AND has_schema_privilege(current_user, n.nspname, 'USAGE') "
                "GROUP BY n.nspname ORDER BY lower(n.nspname), n.nspname",
                (list(RELKIND_NAMES),),
            )
            return [
                {
                    "name": str(row["name"]),
                    "table_count": int(row["table_count"] or 0),
                    "view_count": int(row["view_count"] or 0),
                    "object_count": int(row["object_count"] or 0),
                }
                for row in cursor.fetchall()
            ]

    def status(self, identifier: DBeaverConnection, database: str) -> dict[str, Any]:
        if not database.strip():
            raise ValueError("database is required")
        with self._connection(identifier, database) as (connection, source, context):
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current_setting('server_version') AS server_version"
                )
                server_version = str(
                    (cursor.fetchone() or {}).get("server_version") or ""
                )
            schema_contexts = self._visible_schema_contexts(connection)
            schemas = [item["name"] for item in schema_contexts]
        return self._response(
            context,
            provider=source.provider,
            driver=source.driver,
            server_version=server_version,
            schemas=schemas,
            schema_contexts=schema_contexts,
            read_only={
                "transaction": context["transaction_read_only"],
                "session_default": context["default_read_only"],
                "enforced_by_mcp": True,
            },
            limits={
                "default_max_rows": self.settings.default_max_rows,
                "hard_max_rows": self.settings.hard_max_rows,
                "statement_timeout_ms": self.settings.statement_timeout_ms,
                "lock_timeout_ms": self.settings.lock_timeout_ms,
                "max_cell_chars": self.settings.max_cell_chars,
                "max_result_bytes": self.settings.max_result_bytes,
            },
            credentials_exposed=False,
            addresses_exposed=False,
            data_persistence="none; results are returned through MCP only",
        )

    def scan_database(
        self,
        identifier: DBeaverConnection,
        database: str,
        schema: str,
        *,
        name_pattern: str = "",
        object_types: list[str] | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        if not database.strip() or not schema.strip():
            raise ValueError("database and schema are required")
        limit = max(1, min(int(limit), 1000))
        wanted_types = set(object_types or RELKIND_NAMES.values())
        invalid = wanted_types - set(RELKIND_NAMES.values())
        if invalid:
            raise ValueError(f"unsupported object types: {', '.join(sorted(invalid))}")
        relkinds = [
            kind for kind, label in RELKIND_NAMES.items() if label in wanted_types
        ]

        filters = ["n.nspname = %s", "c.relkind = ANY(%s)"]
        parameters: list[Any] = [schema, relkinds]
        if name_pattern:
            filters.append("c.relname ILIKE %s")
            parameters.append(f"%{name_pattern}%")
        where_clause = " AND ".join(filters)

        with self._connection(identifier, database, schema) as (connection, _, context):
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT c.relkind, COUNT(*) AS object_count "
                    f"FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    f"WHERE {where_clause} GROUP BY c.relkind ORDER BY c.relkind",
                    parameters,
                )
                counts = [
                    {
                        "database": context["database"],
                        "schema": context["schema"],
                        "type": RELKIND_NAMES.get(row["relkind"], row["relkind"]),
                        "count": row["object_count"],
                    }
                    for row in cursor.fetchall()
                ]
                cursor.execute(
                    f"SELECT c.relname AS object_name, c.relkind, "
                    f"obj_description(c.oid, 'pg_class') AS comment, "
                    f"CASE WHEN c.relkind IN ('r','p','m') THEN c.reltuples::bigint END AS approximate_rows "
                    f"FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    f"WHERE {where_clause} ORDER BY c.relname LIMIT %s",
                    [*parameters, limit + 1],
                )
                rows = cursor.fetchall()

        truncated = len(rows) > limit
        objects = [
            {
                "database": context["database"],
                "schema": context["schema"],
                "name": row["object_name"],
                "type": RELKIND_NAMES.get(row["relkind"], row["relkind"]),
                "comment": row["comment"] or "",
                "approximate_rows": row["approximate_rows"],
            }
            for row in rows[:limit]
        ]
        return self._response(
            context,
            filters={
                "name_pattern": name_pattern,
                "object_types": sorted(wanted_types),
            },
            counts=counts,
            objects=objects,
            returned=len(objects),
            truncated=truncated,
        )

    def search_catalog(
        self,
        identifier: DBeaverConnection,
        database: str,
        query: str,
        *,
        schema: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise ValueError("query is required")
        if not database.strip():
            raise ValueError("database is required")
        limit = max(1, min(int(limit), 200))
        pattern = f"%{query}%"
        with self._connection(identifier, database, schema) as (connection, _, context):
            searched_schemas = [schema] if schema else self._visible_schemas(connection)
            schema_filter = (
                "n.nspname = %s"
                if schema
                else "n.nspname <> 'information_schema' AND n.nspname !~ '^pg_' "
                "AND has_schema_privilege(current_user, n.nspname, 'USAGE')"
            )
            schema_parameters: list[Any] = [schema] if schema else []
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT n.nspname AS schema_name, c.relname AS object_name, c.relkind, "
                    "obj_description(c.oid, 'pg_class') AS object_comment, "
                    "a.attname AS column_name, "
                    "CASE WHEN a.attnum IS NULL THEN NULL ELSE format_type(a.atttypid, a.atttypmod) END AS data_type, "
                    "CASE WHEN a.attnum IS NULL THEN NULL ELSE col_description(c.oid, a.attnum) END AS column_comment "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
                    f"WHERE {schema_filter} AND c.relkind = ANY(%s) AND "
                    "(c.relname ILIKE %s OR COALESCE(obj_description(c.oid, 'pg_class'), '') ILIKE %s "
                    "OR COALESCE(a.attname, '') ILIKE %s "
                    "OR COALESCE(col_description(c.oid, a.attnum), '') ILIKE %s) "
                    "ORDER BY CASE WHEN lower(c.relname) = lower(%s) THEN 0 "
                    "WHEN lower(a.attname) = lower(%s) THEN 1 ELSE 2 END, "
                    "n.nspname, c.relname, a.attnum "
                    "LIMIT %s",
                    [
                        *schema_parameters,
                        list(RELKIND_NAMES),
                        pattern,
                        pattern,
                        pattern,
                        pattern,
                        query,
                        query,
                        limit + 1,
                    ],
                )
                rows = cursor.fetchall()
        truncated = len(rows) > limit
        matches = [
            {
                "database": context["database"],
                "schema": row["schema_name"],
                "object": row["object_name"],
                "object_type": RELKIND_NAMES.get(row["relkind"], row["relkind"]),
                "object_comment": row["object_comment"] or "",
                "column": row["column_name"] or "",
                "data_type": row["data_type"] or "",
                "column_comment": row["column_comment"] or "",
            }
            for row in rows[:limit]
        ]
        return self._response(
            context,
            query=query,
            schema_scope=searched_schemas,
            matches=matches,
            returned=len(matches),
            truncated=truncated,
        )

    def table_context(
        self, identifier: DBeaverConnection, database: str, schema: str, table: str
    ) -> dict[str, Any]:
        if not database.strip() or not schema.strip() or not table.strip():
            raise ValueError("database, schema, and table are required")
        with self._connection(identifier, database, schema) as (connection, _, context):
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT c.oid, c.relkind, obj_description(c.oid, 'pg_class') AS comment, "
                    "c.reltuples::bigint AS approximate_rows, "
                    "CASE WHEN c.relkind IN ('r','p','m') THEN pg_total_relation_size(c.oid) END AS total_bytes "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = %s AND c.relname = %s AND c.relkind = ANY(%s)",
                    (schema, table, list(RELKIND_NAMES)),
                )
                relation = cursor.fetchone()
                if not relation:
                    raise ValueError(
                        f"database object not found: {context['database']}.{schema}.{table}"
                    )
                oid = relation["oid"]
                cursor.execute(
                    "SELECT a.attnum AS ordinal, a.attname AS name, format_type(a.atttypid, a.atttypmod) AS data_type, "
                    "NOT a.attnotnull AS nullable, pg_get_expr(ad.adbin, ad.adrelid) AS default_expression, "
                    "col_description(a.attrelid, a.attnum) AS comment "
                    "FROM pg_attribute a LEFT JOIN pg_attrdef ad "
                    "ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum "
                    "WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum",
                    (oid,),
                )
                columns = [dict(row) for row in cursor.fetchall()]
                cursor.execute(
                    "SELECT array_agg(a.attname ORDER BY key_position.ordinality) AS columns "
                    "FROM pg_index i CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS key_position(attnum, ordinality) "
                    "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = key_position.attnum "
                    "WHERE i.indrelid = %s AND i.indisprimary",
                    (oid,),
                )
                primary_key = (cursor.fetchone() or {}).get("columns") or []
                cursor.execute(
                    "SELECT conname AS name, pg_get_constraintdef(oid, true) AS definition "
                    "FROM pg_constraint WHERE conrelid = %s AND contype = 'f' ORDER BY conname",
                    (oid,),
                )
                foreign_keys = [dict(row) for row in cursor.fetchall()]
                cursor.execute(
                    "SELECT indexname AS name, indexdef AS definition FROM pg_indexes "
                    "WHERE schemaname = %s AND tablename = %s ORDER BY indexname",
                    (schema, table),
                )
                indexes = [dict(row) for row in cursor.fetchall()]

        time_candidates = [
            column["name"]
            for column in columns
            if any(
                token in str(column["name"]).lower()
                for token in ("time", "date", "_at", "etl", "modified")
            )
        ]
        return self._response(
            context,
            name=table,
            qualified_name=f"{context['database']}.{schema}.{table}",
            type=RELKIND_NAMES.get(relation["relkind"], relation["relkind"]),
            comment=relation["comment"] or "",
            approximate_rows=relation["approximate_rows"],
            total_bytes=relation["total_bytes"],
            columns=columns,
            column_count=len(columns),
            primary_key=list(primary_key),
            foreign_keys=foreign_keys,
            indexes=indexes,
            time_filter_candidates=time_candidates,
        )

    @staticmethod
    def _routine_select(connection):
        version = connection.info.server_version
        kind = (
            "CASE WHEN p.prokind='p' THEN 'procedure' ELSE 'function' END"
            if version >= 110000
            else "'function'::text"
        )
        predicate = (
            "p.prokind IN ('f','p')"
            if version >= 110000
            else "NOT p.proisagg"
            + (" AND NOT p.proiswindow" if version >= 80400 else "")
        )
        return (
            "SELECT p.oid::text AS routine_id,n.nspname AS schema,p.proname AS name,"
            + kind
            + " AS routine_type, "
            "l.lanname AS language,LEFT(pg_catalog.oidvectortypes(p.proargtypes),4000) AS signature "
            "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace "
            "JOIN pg_catalog.pg_language l ON l.oid=p.prolang WHERE "
            + predicate
            + " AND pg_catalog.has_schema_privilege(n.oid,'USAGE') "
            "AND n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_temp_%%'"
        )

    def list_routines(
        self,
        source,
        database,
        *,
        schema="",
        name_pattern="",
        routine_types=None,
        limit=50,
    ):
        selected = ["procedure", "function"] if routine_types is None else routine_types
        if not selected or set(selected) - {"procedure", "function"}:
            unsupported("routine_type")
        with self._connection(source, database, schema) as (connection, _, context):
            if (
                connection.info.server_version < 110000
                and "procedure" in selected
                and routine_types is not None
            ):
                unsupported("procedures_before_postgresql_11")
            with error_stage("catalog"), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM (" + self._routine_select(connection) + ") r "
                    "WHERE (%(schema)s='' OR r.schema=%(schema)s) AND r.name ILIKE %(pattern)s "
                    "AND r.routine_type=ANY(%(types)s::text[]) ORDER BY r.schema,r.name,r.routine_id LIMIT %(limit)s",
                    {
                        "schema": schema,
                        "pattern": name_pattern or "%",
                        "types": selected,
                        "limit": limit + 1,
                    },
                )
                rows = cursor.fetchmany(limit + 1)
            return self._response(
                context,
                routines=rows[:limit],
                returned=min(len(rows), limit),
                truncated=len(rows) > limit,
                executed=False,
            )

    def routine_context(
        self,
        source,
        database,
        schema,
        routine,
        *,
        routine_id="",
        package="",
        include_definition=True,
        definition_part="definition",
        definition_offset=0,
        max_definition_chars=4000,
    ):
        if package or definition_part != "definition":
            unsupported("routine_package_or_definition_part")
        with self._connection(source, database, schema) as (connection, _, context):
            with error_stage("catalog"), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM ("
                    + self._routine_select(connection)
                    + ") r WHERE r.schema=%(schema)s "
                    "AND r.name=%(name)s AND (%(routine_id)s='' OR r.routine_id=%(routine_id)s) LIMIT 2",
                    {"schema": schema, "name": routine, "routine_id": routine_id},
                )
                row = resolve_routine(cursor.fetchmany(2))
                oid = int(row["routine_id"])
                cursor.execute(
                    "SELECT g.i AS ordinal,p.proargnames[g.i] AS name, "
                    "CASE COALESCE(p.proargmodes[g.i],'i') WHEN 'o' THEN 'out' WHEN 'b' THEN 'inout' "
                    "WHEN 'v' THEN 'variadic' WHEN 't' THEN 'table' ELSE 'in' END AS direction, "
                    "pg_catalog.format_type(CASE WHEN p.proallargtypes IS NULL THEN p.proargtypes[g.i-1] "
                    "ELSE p.proallargtypes[g.i] END,NULL) AS data_type "
                    "FROM pg_catalog.pg_proc p CROSS JOIN pg_catalog.generate_series(1, "
                    "(SELECT COALESCE(pg_catalog.array_upper(proallargtypes,1),pronargs) FROM pg_catalog.pg_proc WHERE oid=%(id)s::oid)) g(i) "
                    "WHERE p.oid=%(id)s::oid ORDER BY g.i LIMIT 201",
                    {"id": oid},
                )
                parameters = cursor.fetchmany(201)
                cursor.execute(
                    "SELECT pg_catalog.format_type(p.prorettype,NULL) AS data_type FROM pg_catalog.pg_proc p WHERE p.oid=%(id)s::oid",
                    {"id": oid},
                )
                return_type = cursor.fetchone()["data_type"]
                cursor.execute(
                    "SELECT DISTINCT COALESCE(cn.nspname,pn.nspname,tn.nspname) AS schema, "
                    "COALESCE(c.relname,rp.proname,t.typname) AS name, "
                    "CASE WHEN c.oid IS NOT NULL THEN 'relation' WHEN rp.oid IS NOT NULL THEN 'routine' ELSE 'type' END AS object_type "
                    "FROM pg_catalog.pg_depend d "
                    "LEFT JOIN pg_catalog.pg_class c ON d.refclassid='pg_catalog.pg_class'::regclass AND c.oid=d.refobjid "
                    "LEFT JOIN pg_catalog.pg_namespace cn ON cn.oid=c.relnamespace "
                    "LEFT JOIN pg_catalog.pg_proc rp ON d.refclassid='pg_catalog.pg_proc'::regclass AND rp.oid=d.refobjid "
                    "LEFT JOIN pg_catalog.pg_namespace pn ON pn.oid=rp.pronamespace "
                    "LEFT JOIN pg_catalog.pg_type t ON d.refclassid='pg_catalog.pg_type'::regclass AND t.oid=d.refobjid "
                    "LEFT JOIN pg_catalog.pg_namespace tn ON tn.oid=t.typnamespace "
                    "WHERE d.classid='pg_catalog.pg_proc'::regclass AND d.objid=%(id)s::oid "
                    "AND d.refclassid IN ('pg_catalog.pg_class'::regclass,'pg_catalog.pg_proc'::regclass,'pg_catalog.pg_type'::regclass) "
                    "ORDER BY schema,name,object_type LIMIT 201",
                    {"id": oid},
                )
                dependencies = cursor.fetchmany(201)
                definition = routine_definition(
                    None, None, definition_offset, "not_requested"
                )
                if include_definition:
                    if row["language"] in {"c", "internal"}:
                        definition = routine_definition(
                            None, None, definition_offset, "external"
                        )
                    else:
                        expression = (
                            "pg_catalog.pg_get_functiondef(p.oid)"
                            if connection.info.server_version >= 80400
                            else "p.prosrc"
                        )
                        cursor.execute(
                            "SELECT pg_catalog.length("
                            + expression
                            + ") AS total_chars, "
                            "pg_catalog.substr("
                            + expression
                            + ",%(start)s,%(chars)s) AS fragment "
                            "FROM pg_catalog.pg_proc p WHERE p.oid=%(id)s::oid",
                            {
                                "id": oid,
                                "start": definition_offset + 1,
                                "chars": min(
                                    max_definition_chars, self.settings.max_cell_chars
                                ),
                            },
                        )
                        source_row = cursor.fetchone()
                        definition = routine_definition(
                            source_row["fragment"],
                            source_row["total_chars"],
                            definition_offset,
                            "available"
                            if source_row["total_chars"] is not None
                            else "unavailable",
                        )
            return self._response(
                context,
                routine=row,
                return_type=return_type,
                parameters=parameters[:200],
                parameters_truncated=len(parameters) > 200,
                dependencies=dependencies[:200],
                dependencies_truncated=len(dependencies) > 200,
                dependencies_status="available",
                dependency_scope="catalog_recorded_direct",
                dependencies_complete=False,
                definition_scope="routine"
                if connection.info.server_version >= 80400
                else "routine_body",
                **definition,
                executed=False,
                permission_scope="database_account",
                read_only={
                    "transaction": True,
                    "session_default": True,
                    "rollback_on_exit": True,
                },
            )

    def _prepare_query(self, connection, schema, query) -> BuiltQuery:
        guard = CatalogGuard(connection)
        catalog = guard.prepare(query, schema)
        return compile_query(schema, query, catalog, self.settings.max_cell_chars)

    def _describe_query(self, connection, built):
        with error_stage("describe"):
            with connection.cursor() as cursor:
                try:
                    cursor.execute(
                        sql.SQL("SELECT * FROM ({}) AS __describe LIMIT 0").format(
                            built.statement
                        ),
                        built.parameters,
                    )
                except psycopg.Error as exc:
                    error = translate_error(exc, "describe")
                    diag = getattr(exc, "diag", None)
                    diagnostic = (
                        str(getattr(diag, "context", "") or "")
                        + " "
                        + str(getattr(diag, "message_primary", "") or "")
                    )
                    match = re.search(r"(?:parameter|参数)\s+\$(\d+)", diagnostic)
                    if match and 0 < int(match[1]) <= len(built.parameter_paths):
                        error.path = built.parameter_paths[int(match[1]) - 1]
                    raise error from exc
                columns = [
                    {"name": d.name, "database_type_oid": d.type_code}
                    for d in cursor.description
                ]
        guard = CatalogGuard(connection)
        with error_stage("safety"):
            for column in columns:
                guard.type(column["database_type_oid"])
        return columns

    def _transport_query(self, built, columns):
        # Clip only AFTER DISTINCT/GROUP/ORDER/set operations and LIMIT.
        # All cells use JSONB transport; Decimal-aware decoding preserves precision.
        projections = []
        for column in columns:
            field = sql.Identifier("__result", column["name"].replace("%", "%%"))
            if column["database_type_oid"] == 17:
                expression = sql.SQL(
                    "CASE WHEN {} IS NULL THEN NULL ELSE pg_catalog.jsonb_build_object('binary',true,'bytes',pg_catalog.octet_length({}),'content_returned',false) END"
                ).format(field, field)
            else:
                expression = sql.SQL(
                    "CASE WHEN pg_catalog.char_length({}::pg_catalog.text) > {} "
                    "THEN pg_catalog.jsonb_build_object('truncated',true,'preview',pg_catalog.left({}::pg_catalog.text,{})) "
                    "ELSE pg_catalog.to_jsonb({}) END"
                ).format(
                    field,
                    sql.Literal(self.settings.max_cell_chars),
                    field,
                    sql.Literal(self.settings.max_cell_chars),
                    field,
                )
            projections.append(
                sql.SQL("{} AS {}").format(
                    expression, sql.Identifier(column["name"].replace("%", "%%"))
                )
            )
        return sql.SQL("SELECT {} FROM ({}) AS __result").format(
            sql.SQL(", ").join(projections), built.statement
        )

    @staticmethod
    def _safe_plan(node):
        names = {
            "Node Type",
            "Parent Relationship",
            "Join Type",
            "Strategy",
            "Partial Mode",
            "Relation Name",
            "Schema",
            "Alias",
            "Index Name",
            "Scan Direction",
            "CTE Name",
            "Subplan Name",
        }
        if isinstance(node, list):
            return [PostgreSQLAdapter._safe_plan(item) for item in node]
        if not isinstance(node, dict):
            return None
        result = {}
        omitted = []
        for key, value in node.items():
            if isinstance(value, (bool, int, float, Decimal)) or value is None:
                result[key] = float(value) if isinstance(value, Decimal) else value
            elif key in names and isinstance(value, str):
                result[key] = value[:200]
            elif key in {"Plans", "Plan"}:
                result[key] = PostgreSQLAdapter._safe_plan(value)
            else:
                omitted.append(key)
        if omitted:
            result["redacted_fields"] = omitted
        return result

    def structured_query(
        self,
        identifier: DBeaverConnection,
        database: str,
        schema: str,
        query: dict[str, Any],
        *,
        mode: str = "execute",
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        if mode not in {"analyze", "explain", "execute"}:
            raise QueryRejected("unknown structured query mode")
        if not identifier or not database or not schema:
            raise QueryRejected("connection, database and schema are required")
        requested_relations(
            query, schema
        )  # Reject malformed input before touching credentials or the network.
        timeout = (
            self.settings.statement_timeout_ms if timeout_ms is None else timeout_ms
        )
        if (
            type(timeout) is not int
            or not 100 <= timeout <= self.settings.statement_timeout_ms
        ):
            raise QueryRejected("timeout_ms is outside the server limit")
        with self._connection(identifier, database, schema) as (connection, _, context):
            built = self._prepare_query(connection, schema, query)
            columns_metadata = self._describe_query(connection, built)
            if mode == "analyze":
                return self._response(
                    context,
                    analysis=built.summary(),
                    columns_metadata=columns_metadata,
                    executed=False,
                )
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_catalog.set_config('statement_timeout', %s, true)",
                    (str(timeout),),
                )
            if mode == "explain":
                with error_stage("explain"), connection.cursor() as cursor:
                    cursor.execute(
                        sql.SQL("EXPLAIN (FORMAT JSON, COSTS TRUE, VERBOSE FALSE) ")
                        + built.statement,
                        built.parameters,
                    )
                    row = cursor.fetchone()
                # Plans can contain filter literals. Return only fixed planner metrics.
                plan = next(iter(row.values()))[0]["Plan"] if row else {}
                plan = self._safe_plan(plan)
                return self._response(
                    context,
                    analysis=built.summary(),
                    executed=False,
                    explain_analyze=False,
                    plan=plan,
                    plan_summary={
                        "startup_cost": plan.get("Startup Cost"),
                        "total_cost": plan.get("Total Cost"),
                        "estimated_rows": plan.get("Plan Rows"),
                        "estimated_row_width": plan.get("Plan Width"),
                    },
                )
            rows: list[list[Any]] = []
            size = 2
            truncated = False
            reason = ""
            # Server cursor and a generated LIMIT bound transfer. Text projections are
            # also clipped on the server; raw query strings cannot reach this sink.
            with (
                error_stage("execute"),
                connection.cursor(
                    name="dbeaver_structured_rows", row_factory=tuple_row
                ) as cursor,
            ):
                cursor.execute(
                    self._transport_query(built, columns_metadata), built.parameters
                )
                for _ in range(built.limit + 1):
                    raw = cursor.fetchone()
                    if raw is None:
                        break
                    if len(rows) == built.limit:
                        truncated, reason = True, "row_limit"
                        break
                    safe = [
                        self._transport_value(value, column["database_type_oid"])
                        for value, column in zip(raw, columns_metadata, strict=True)
                    ]
                    row_size = (
                        len(
                            json.dumps(
                                safe, ensure_ascii=False, allow_nan=False
                            ).encode("utf-8")
                        )
                        + 1
                    )
                    if size + row_size > self.settings.max_result_bytes // 2:
                        truncated, reason = True, "response_bytes"
                        break
                    rows.append(safe)
                    size += row_size
        return self._response(
            context,
            analysis=built.summary(),
            columns=list(built.outputs),
            columns_metadata=columns_metadata,
            permission_scope="database_account",
            row_security="database_native",
            rows=rows,
            returned_rows=len(rows),
            row_limit=built.limit,
            truncated=truncated,
            truncation_reason=reason,
            read_only_verified=True,
            persisted=False,
        )

    def read_postgresql_logs(
        self, identifier: DBeaverConnection, database: str, **filters: Any
    ) -> dict[str, Any]:
        from .postgres_logs import read_logs, validate_log_filters

        if not identifier or not database:
            raise QueryRejected("connection and database are required")
        validate_log_filters(filters)
        with self._connection(identifier, database) as (connection, _, context):
            with error_stage("read_logs"):
                result = read_logs(connection, database, **filters)
        return self._response(context, **result)

    def read_database_settings(self, identifier, database, names=None):
        if not identifier or not database:
            raise QueryRejected(
                "connection/database 为必填参数。", code="MISSING_PARAMETER"
            )
        sensitive = {
            "primary_conninfo",
            "ssl_passphrase_command",
            "archive_command",
            "restore_command",
            "archive_cleanup_command",
            "recovery_end_command",
        }
        with self._connection(identifier, database) as (connection, _, context):
            with error_stage("read_settings"), connection.cursor() as cursor:
                if names:
                    cursor.execute(
                        "SELECT name,setting,unit,context,vartype,source,pending_restart FROM pg_catalog.pg_settings WHERE name=ANY(%s) ORDER BY name",
                        (names,),
                    )
                else:
                    cursor.execute(
                        "SELECT name,setting,unit,context,vartype,source,pending_restart FROM pg_catalog.pg_settings ORDER BY name LIMIT 501"
                    )
                rows = [dict(row) for row in cursor.fetchall()]
        found = {row["name"] for row in rows}
        if names and set(names) - found:
            raise QueryRejected(
                "指定的配置项不存在或不可见。",
                code="SETTING_NOT_FOUND",
                path="names",
                hint="省略 names 列出可见配置项，然后使用精确名称。",
            )
        for row in rows:
            redacted = row["name"] in sensitive or any(
                token in row["name"] for token in ("password", "secret", "token")
            )
            if redacted:
                row["setting"] = "<redacted>"
            row["redacted"] = redacted
        return self._response(
            context,
            settings=rows[:500],
            returned=min(len(rows), 500),
            truncated=len(rows) > 500,
            permission_scope="database_account",
            persisted=False,
        )

    def _safe_value(self, value: Any) -> Any:
        if isinstance(value, float):
            import math

            return value if math.isfinite(value) else str(value)
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return {"binary": True, "bytes": len(value), "content_returned": False}
        if isinstance(value, (list, tuple)):
            return [self._safe_value(item) for item in value]
        if isinstance(value, dict):
            return {str(key): self._safe_value(item) for key, item in value.items()}
        text = str(value)
        if len(text) > self.settings.max_cell_chars:
            return text[: self.settings.max_cell_chars] + "…<truncated>"
        return text

    def _transport_value(self, value, oid):
        if value is None:
            return None
        if oid == 1700 and isinstance(value, (int, Decimal)):
            return str(value)
        if oid in {700, 701} and isinstance(value, Decimal):
            return self._safe_value(float(value))
        if oid in {114, 3802, 1021, 1022}:

            def json_value(item):
                if isinstance(item, Decimal):
                    number = float(item)
                    import math

                    return number if math.isfinite(number) else str(item)
                if isinstance(item, list):
                    return [json_value(x) for x in item]
                if isinstance(item, dict):
                    return {k: json_value(v) for k, v in item.items()}
                return item

            return json_value(value)
        if oid == 1231 and isinstance(value, list):

            def numeric_array(item):
                if isinstance(item, list):
                    return [numeric_array(x) for x in item]
                return None if item is None else str(item)

            return numeric_array(value)
        return self._safe_value(value)
