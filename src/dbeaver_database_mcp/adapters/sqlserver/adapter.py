"""SQL Server adapter using the public DatabaseAdapter contract and TDS driver."""

import json
import math
from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

from pytds import tds_base

from ...core.adapter import (
    DatabaseAdapter,
    resolve_routine,
    routine_definition,
    unsupported,
)
from ...core.errors import QueryRejected, error_stage, register_error_translator
from ...core.validation import validate_query
from .catalog_guard import CatalogGuard
from .connection import parse_configuration
from .errors import translate_database_error
from .plan import explain
from .query import (
    AGGREGATES,
    CONDITIONS,
    CONTEXT,
    FUNCTIONS,
    TYPES,
    WINDOWS,
    compile_query,
    identifier,
)
from .session import open_session
from .view_catalog import read_view_context

register_error_translator(translate_database_error)

OBJECT_TYPES = {"table": "U", "view": "V"}
READ_ONLY = {
    "transaction": False,
    "session_default": None,
    "application_intent": "ReadOnly",
    "enforcement": "structured_select_and_dependency_guard",
    "rollback_on_exit": True,
}
TEXT_IDS = {
    tds_base.SYBCHAR,
    tds_base.SYBVARCHAR,
    tds_base.SYBTEXT,
    tds_base.SYBNTEXT,
    tds_base.XSYBCHAR,
    tds_base.XSYBVARCHAR,
    tds_base.XSYBNCHAR,
    tds_base.XSYBNVARCHAR,
}
BINARY_IDS = {
    tds_base.SYBBINARY,
    tds_base.SYBVARBINARY,
    tds_base.SYBIMAGE,
    tds_base.XSYBBINARY,
    tds_base.XSYBVARBINARY,
}
CATALOG = (
    "FROM sys.objects o JOIN sys.schemas s ON s.schema_id=o.schema_id "
    "LEFT JOIN sys.extended_properties ep ON ep.class=1 AND ep.major_id=o.object_id "
    "AND ep.minor_id=0 AND ep.name='MS_Description' "
    "WHERE o.type IN ('U','V') AND o.is_ms_shipped=0 "
)
ROUTINES = (
    "SELECT CONVERT(varchar(20),o.object_id) AS routine_id,s.name AS [schema],o.name, "
    "CASE WHEN o.type IN ('P','PC','RF','X') THEN 'procedure' ELSE 'function' END AS routine_type, "
    "CASE WHEN o.type IN ('PC','FS','FT','X') THEN 'external' ELSE 'tsql' END AS language, "
    "HAS_PERMS_BY_NAME(QUOTENAME(s.name)+'.'+QUOTENAME(o.name),'OBJECT','VIEW DEFINITION') AS can_view_definition "
    "FROM sys.objects o JOIN sys.schemas s ON s.schema_id=o.schema_id "
    "WHERE o.type IN ('P','PC','RF','X','FN','IF','TF','FS','FT') AND o.is_ms_shipped=0"
)


def safe_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return {"binary": True, "content_returned": False}
    if isinstance(value, dict):
        return {key: safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_value(item) for item in value]
    return value


class SQLServerAdapter(DatabaseAdapter):
    database_type = "sqlserver"
    parse_configuration = staticmethod(parse_configuration)

    @classmethod
    def capabilities(cls):
        return {
            "database_type": cls.database_type,
            "minimum_version": "2008 (10.x)",
            "driver_mode": "python-tds",
            "functions": sorted(set(FUNCTIONS) | set(CONTEXT)),
            "aggregates": list(AGGREGATES),
            "conditions": CONDITIONS,
            "casts": list(TYPES),
            "windows": sorted(set(WINDOWS) | set(AGGREGATES)),
            "object_types": list(OBJECT_TYPES),
            "features": [
                "joins_on",
                "group_by",
                "having",
                "distinct",
                "case",
                "top",
                "projected_output_ordering",
                "view_execution",
                "cte",
                "subquery",
                "correlated_subquery",
                "set_operations",
                "windows",
                "cross_database_dependencies",
                "linked_server_dependencies",
                "alias_types",
            ],
            "conditional_features": {
                "offset": "SQL Server 2012+ and explicit projected output ordering",
                "view_execution": "visible definition parsed with its saved QUOTED_IDENTIFIER semantics; recursively verified table/view dependencies; database VIEW DEFINITION and SELECT on sys.sql_expression_dependencies in every target database",
                "linked_server_dependencies": "existing SQL Server link with allowlisted Microsoft provider, DATA ACCESS and RPC OUT; fixed parameterized catalog SELECTs through sys.sp_executesql under the linked login; no second linked-server hop",
                "windows": "ranking on 2008+; offset/value/distribution functions and ROWS/RANGE frames on 2012+",
                "explain": "estimated SHOWPLAN_XML; native SHOWPLAN permissions in all referenced databases; no business-query execution",
            },
            "explain": True,
            "database_settings": "database_properties",
            "postgresql_logs": False,
            "view_catalog": {
                "context": True,
                "definition": True,
                "dependencies": "catalog_recorded_direct",
                "dependency_granularity": "object",
                "definition_pagination": "characters",
                "dependency_pagination": "offset",
                "types": ["view"],
                "execute": False,
                "conditional_features": {
                    "definition_offset_units": "SQL Server 2008 uses UTF-16 code units; SQL Server 2012+ uses database characters"
                },
            },
            "read_only": READ_ONLY,
            "unsupported": [
                "server_enforced_read_only_transaction",
                "encrypted_or_unverifiable_views",
                "user_defined_functions",
                "synonyms",
                "computed_columns",
                "clr_types",
                "table_types",
                "multihop_linked_servers",
                "row_security_policies",
                "external_tables",
                "recursive_cte",
                "intersect_all",
                "except_all",
                "aggregate_filter",
                "joins_using",
                "lateral",
                "table_functions",
                "arrays",
                "json",
                "tls",
                "integrated_authentication",
                "named_instances",
                "unprojected_ordering",
            ],
            "numeric_binding": "decimal(38,10); scales above 10 rejected",
            "routine_catalog": {
                "list": True,
                "parameters": True,
                "definition": True,
                "types": ["procedure", "function"],
                "definition_pagination": "characters",
                "dependencies": "catalog_recorded_direct",
                "execute": False,
            },
        }

    def _response(self, source, database, schema="", **payload):
        return safe_value(
            source.for_database(database).model_identity(schema) | payload
        )

    def _schema(self, session, schema=""):
        if schema:
            if not session.rows(
                "SELECT name FROM sys.schemas WHERE name=%(schema)s",
                {"schema": schema},
                limit=1,
            ):
                raise QueryRejected(
                    "SQL Server schema 不存在或不可见。",
                    code="SCHEMA_NOT_ACCESSIBLE",
                    path="schema",
                )
            return schema
        return session.rows("SELECT SCHEMA_NAME() AS name", limit=1)[0]["name"] or "dbo"

    def list_databases(self, source):
        with open_session(source, self.settings, source.database) as session:
            schema = self._schema(session)
            rows = session.rows(
                "SELECT TOP (1001) name, is_read_only FROM sys.databases "
                "WHERE state=0 AND HAS_DBACCESS(name)=1 ORDER BY name",
                limit=1001,
            )
        databases = [
            {**r, "is_default": r["name"] == source.database, "is_template": False}
            for r in rows[:1000]
        ]
        return self._response(
            source,
            source.database,
            schema,
            default_database=source.database,
            databases=databases,
            returned=len(databases),
            truncated=len(rows) > 1000,
            read_only=READ_ONLY | {"database_read_only": session.database_read_only},
        )

    def _schemas(self, session):
        return session.rows(
            "SELECT TOP (1001) s.name, SUM(CASE WHEN o.type='U' THEN 1 ELSE 0 END) AS table_count, "
            "SUM(CASE WHEN o.type='V' THEN 1 ELSE 0 END) AS view_count, COUNT(o.object_id) AS object_count "
            "FROM sys.schemas s LEFT JOIN sys.objects o ON o.schema_id=s.schema_id "
            "AND o.type IN ('U','V') AND o.is_ms_shipped=0 WHERE s.name NOT IN ('sys','INFORMATION_SCHEMA') "
            "GROUP BY s.name ORDER BY s.name",
            limit=1001,
        )

    def list_schemas(self, source, database):
        with open_session(source, self.settings, database) as session:
            schema, rows = self._schema(session), self._schemas(session)
        return self._response(
            source,
            database,
            schema,
            schemas=[r["name"] for r in rows[:1000]],
            schema_contexts=rows[:1000],
            returned=min(len(rows), 1000),
            truncated=len(rows) > 1000,
        )

    def status(self, source, database):
        with open_session(source, self.settings, database) as session:
            schema = self._schema(session)
            info = session.rows(
                "SELECT DB_NAME() AS database_name, CAST(SERVERPROPERTY('ProductVersion') AS varchar(32)) AS server_version, "
                "CAST(DATABASEPROPERTYEX(DB_NAME(),'Updateability') AS varchar(32)) AS updateability",
                limit=1,
            )[0]
            rows = self._schemas(session)
        return self._response(
            source,
            database,
            schema,
            **info,
            schemas=[r["name"] for r in rows[:1000]],
            schema_contexts=rows[:1000],
            read_only=READ_ONLY | {"database_read_only": session.database_read_only},
            truncated=len(rows) > 1000,
            limits={
                "max_rows": self.settings.hard_max_rows,
                "timeout_ms": self.settings.statement_timeout_ms,
            },
        )

    def scan_database(
        self, source, database, schema, *, name_pattern="", object_types=None, limit=200
    ):
        selected = list(OBJECT_TYPES) if object_types is None else object_types
        if not selected or set(selected) - set(OBJECT_TYPES):
            unsupported("object_type")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise QueryRejected("目录行数上限无效。", code="INVALID_PARAMETER")
        conditions = (
            "AND s.name=%(schema)s AND o.name LIKE %(pattern)s AND o.type IN ("
            + ",".join(f"%(type{i})s" for i in range(len(selected)))
            + ") "
        )
        params = {
            "schema": schema,
            "pattern": name_pattern or "%",
            "limit": limit + 1,
            **{f"type{i}": OBJECT_TYPES[t] for i, t in enumerate(selected)},
        }
        with open_session(source, self.settings, database) as session:
            self._schema(session, schema)
            counts = session.rows(
                "SELECT COUNT(*) AS count " + CATALOG + conditions, params, limit=1
            )[0]["count"]
            rows = session.rows(
                "SELECT TOP (%(limit)s) s.name AS [schema], o.name, o.type, "
                "LEFT(CONVERT(nvarchar(max),ep.value),4000) AS comment, "
                "(SELECT SUM(p.rows) FROM sys.partitions p WHERE p.object_id=o.object_id AND p.index_id IN (0,1)) AS approximate_rows "
                + CATALOG
                + conditions
                + "ORDER BY o.name",
                params,
                limit=limit + 1,
            )
        for r in rows:
            r["object_type"] = "table" if r.pop("type").strip() == "U" else "view"
        return self._response(
            source,
            database,
            schema,
            objects=rows[:limit],
            total_objects=counts,
            returned=len(rows[:limit]),
            truncated=len(rows) > limit,
        )

    def search_catalog(self, source, database, query, *, schema="", limit=50):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise QueryRejected("目录行数上限无效。", code="INVALID_PARAMETER")
        params = {"schema": schema, "pattern": "%" + query + "%", "limit": limit + 1}
        with open_session(source, self.settings, database) as session:
            self._schema(session, schema)
            rows = session.rows(
                "SELECT TOP (%(limit)s) s.name AS [schema], o.name AS table_name, "
                "LEFT(CONVERT(nvarchar(max),ep.value),4000) AS comment "
                + CATALOG
                + "AND (%(schema)s='' OR s.name=%(schema)s) AND (o.name LIKE %(pattern)s OR "
                "CONVERT(nvarchar(max),ep.value) LIKE %(pattern)s OR EXISTS (SELECT 1 FROM sys.columns c "
                "LEFT JOIN sys.extended_properties cp ON cp.class=1 AND cp.major_id=c.object_id "
                "AND cp.minor_id=c.column_id AND cp.name='MS_Description' WHERE c.object_id=o.object_id "
                "AND (c.name LIKE %(pattern)s OR CONVERT(nvarchar(max),cp.value) LIKE %(pattern)s))) ORDER BY s.name,o.name",
                params,
                limit=limit + 1,
            )
        return self._response(
            source,
            database,
            schema,
            matches=rows[:limit],
            returned=len(rows[:limit]),
            truncated=len(rows) > limit,
        )

    def table_context(self, source, database, schema, table):
        with open_session(source, self.settings, database) as session:
            self._schema(session, schema)
            objects = session.rows(
                "SELECT o.object_id, o.type FROM sys.objects o JOIN sys.schemas s ON s.schema_id=o.schema_id "
                "WHERE s.name=%(schema)s AND o.name=%(table)s AND o.type IN ('U','V')",
                {"schema": schema, "table": table},
                limit=1,
            )
            if not objects:
                raise QueryRejected(
                    "未找到 SQL Server 表或视图。", code="TABLE_NOT_FOUND"
                )
            oid = objects[0]["object_id"]
            columns = CatalogGuard(session).columns(oid)
            keys = session.rows(
                "SELECT TOP (1025) i.name, i.is_primary_key, i.is_unique, c.name AS column_name, ic.key_ordinal "
                "FROM sys.indexes i JOIN sys.index_columns ic ON ic.object_id=i.object_id AND ic.index_id=i.index_id "
                "JOIN sys.columns c ON c.object_id=ic.object_id AND c.column_id=ic.column_id "
                "WHERE i.object_id=%(id)s AND ic.key_ordinal>0 ORDER BY i.index_id,ic.key_ordinal",
                {"id": oid},
                limit=1025,
            )
        return self._response(
            source,
            database,
            schema,
            table=table,
            columns=columns[:1024],
            indexes=keys[:1024],
            truncated=len(columns) > 1024 or len(keys) > 1024,
        )

    def view_context(self, source, database, schema, view, **options):
        return read_view_context(self, source, database, schema, view, **options)

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
        params = {
            "schema": schema,
            "pattern": name_pattern or "%",
            "limit": limit + 1,
            **{f"kind{i}": kind for i, kind in enumerate(selected)},
        }
        markers = ",".join(f"%(kind{i})s" for i in range(len(selected)))
        with open_session(source, self.settings, database) as session:
            if schema:
                self._schema(session, schema)
            rows = session.rows(
                "SELECT TOP (%(limit)s) * FROM (" + ROUTINES + ") AS r "
                "WHERE (%(schema)s='' OR r.[schema]=%(schema)s) AND r.name LIKE %(pattern)s "
                "AND r.routine_type IN ("
                + markers
                + ") ORDER BY r.[schema],r.name,r.routine_id",
                params,
                limit=limit + 1,
            )
        return self._response(
            source,
            database,
            schema,
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
        params = {"schema": schema, "name": routine, "routine_id": routine_id}
        with open_session(source, self.settings, database) as session:
            self._schema(session, schema)
            row = resolve_routine(
                session.rows(
                    "SELECT TOP (2) * FROM ("
                    + ROUTINES
                    + ") AS r WHERE r.[schema]=%(schema)s "
                    "AND r.name=%(name)s AND (%(routine_id)s='' OR r.routine_id=%(routine_id)s)",
                    params,
                    limit=2,
                )
            )
            oid = int(row["routine_id"])
            parameters = session.rows(
                "SELECT TOP (201) p.parameter_id AS ordinal,p.name, "
                "CASE WHEN p.parameter_id=0 THEN 'return' WHEN p.is_output=1 THEN 'inout' ELSE 'in' END AS direction, "
                "t.name AS data_type,SCHEMA_NAME(t.schema_id) AS type_schema,p.max_length,p.precision,p.scale, "
                "CASE WHEN o.type IN ('PC','FS','FT') THEN p.has_default_value ELSE NULL END AS has_default "
                "FROM sys.parameters p JOIN sys.types t ON t.user_type_id=p.user_type_id "
                "JOIN sys.objects o ON o.object_id=p.object_id WHERE p.object_id=%(id)s ORDER BY p.parameter_id",
                {"id": oid},
                limit=201,
            )
            access = session.rows(
                "SELECT HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','VIEW DEFINITION') AS view_allowed, "
                "HAS_PERMS_BY_NAME('sys.sql_expression_dependencies','OBJECT','SELECT') AS select_allowed",
                limit=1,
            )[0]
            dependencies = []
            dependencies_status = "permission_denied"
            if access["view_allowed"] == 1 and access["select_allowed"] == 1:
                dependencies = session.rows(
                    "SELECT DISTINCT TOP (201) d.referenced_server_name AS [server], d.referenced_database_name AS [database], "
                    "d.referenced_schema_name AS [schema],d.referenced_entity_name AS name, "
                    "CASE WHEN d.referenced_server_name IS NULL THEN 0 ELSE 1 END AS remote "
                    "FROM sys.sql_expression_dependencies d WHERE d.referencing_id=%(id)s "
                    "ORDER BY d.referenced_server_name,d.referenced_database_name,d.referenced_schema_name,d.referenced_entity_name,remote",
                    {"id": oid},
                    limit=201,
                )
                dependencies_status = "available"
            definition = routine_definition(
                None, None, definition_offset, "not_requested"
            )
            if include_definition:
                # A supplementary-character-aware collation keeps source paging
                # aligned with Unicode code points on SQL Server 2012+.
                expression = (
                    "m.definition COLLATE Latin1_General_100_CI_AS_SC"
                    if session.major_version >= 11
                    else "m.definition"
                )
                fragment = "SUBSTRING(" + expression + ",%(start)s,%(chars)s)"
                source_row = session.rows(
                    "SELECT OBJECTPROPERTYEX(o.object_id,'IsEncrypted') AS encrypted, "
                    "LEN("
                    + expression
                    + "+N'#')-1 AS total_chars,"
                    + fragment
                    + " AS fragment, "
                    "LEN(" + fragment + "+N'#')-1 AS returned_chars FROM sys.objects o "
                    "LEFT JOIN sys.sql_modules m ON m.object_id=o.object_id WHERE o.object_id=%(id)s",
                    {
                        "id": oid,
                        "start": definition_offset + 1,
                        "chars": min(
                            max_definition_chars, self.settings.max_cell_chars
                        ),
                    },
                    limit=1,
                )[0]
                status = (
                    "encrypted"
                    if source_row["encrypted"]
                    else "external"
                    if row["language"] == "external"
                    else "permission_denied"
                    if row["can_view_definition"] != 1
                    else "available"
                    if source_row["total_chars"] is not None
                    else "unavailable"
                )
                definition = routine_definition(
                    source_row["fragment"],
                    source_row["total_chars"],
                    definition_offset,
                    status,
                    source_row["returned_chars"] or 0,
                )
            return self._response(
                source,
                database,
                schema,
                routine=row,
                parameters=parameters[:200],
                parameters_truncated=len(parameters) > 200,
                dependencies=dependencies[:200],
                dependencies_truncated=len(dependencies) > 200,
                dependencies_status=dependencies_status,
                dependency_scope="catalog_recorded_direct",
                dependencies_complete=False,
                definition_scope="routine",
                **definition,
                executed=False,
                permission_scope="database_account",
                read_only=READ_ONLY
                | {"database_read_only": session.database_read_only},
            )

    def _transport(self, built, description):
        projections = []
        for name, type_id, *_ in description:
            column = "q." + identifier(name)
            if type_id in TEXT_IDS:
                projections.append(
                    f"LEFT(CONVERT(nvarchar(max),{column}),{self.settings.max_cell_chars + 1}) AS {identifier(name)}"
                )
            elif type_id in BINARY_IDS:
                projections.append(
                    f"CASE WHEN {column} IS NULL THEN NULL ELSE 0 END AS {identifier(name)}"
                )
            elif type_id in {tds_base.SYBVARIANT, tds_base.SYBMSXML, tds_base.SYBMSUDT}:
                unsupported("result_type")
            else:
                projections.append(column)
        statement = built.wrap("SELECT " + ", ".join(projections) + " FROM (", ") AS q")
        if built.ordering:
            statement += " ORDER BY " + ", ".join(
                "q." + identifier(name) + " " + direction.upper()
                for name, direction in built.ordering
            )
        return statement

    def structured_query(
        self, source, database, schema, query, *, mode="execute", timeout_ms=None
    ):
        validate_query(query)
        if mode not in {"analyze", "execute", "explain"}:
            raise QueryRejected("未知查询模式。", code="INVALID_PARAMETER")
        if query["limit"] > self.settings.hard_max_rows:
            raise QueryRejected(
                "查询超过行数上限。", code="INVALID_PARAMETER", path="query.limit"
            )
        with open_session(source, self.settings, database, timeout_ms) as session:
            self._schema(session, schema)
            with error_stage("safety"):
                catalog = CatalogGuard(session).prepare(query, schema)
            built = compile_query(schema, query, catalog, session.major_version)
            if mode == "explain":
                with error_stage("explain"):
                    plan = explain(
                        session, built, min(self.settings.max_result_bytes // 2, 262144)
                    )
                return self._response(
                    source,
                    database,
                    schema,
                    analysis=built.summary(),
                    columns=list(built.outputs),
                    plan=plan,
                    read_only=READ_ONLY
                    | {"database_read_only": session.database_read_only},
                    permission_scope="database_account",
                )
            with error_stage("describe"), session.connection.cursor() as cursor:
                session.execute(
                    cursor,
                    built.wrap("SELECT TOP (0) * FROM (", ") AS q"),
                    built.parameters,
                )
                description = cursor.description
            statement = self._transport(built, description)
            metadata = [
                {
                    "name": d[0],
                    "tds_type": d[1],
                    "precision": d[4],
                    "scale": d[5],
                    "nullable": bool(d[6]),
                }
                for d in description
            ]
            envelope = self._response(
                source,
                database,
                schema,
                analysis=built.summary(),
                columns=list(built.outputs),
                columns_metadata=metadata,
                read_only=READ_ONLY
                | {"database_read_only": session.database_read_only},
                permission_scope="database_account",
                row_security="database_native",
            )
            if mode == "analyze":
                return envelope
            rows, size, truncated, reason = [], 0, False, None
            byte_budget = (
                self.settings.max_result_bytes
                - len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
                - 512
            )
            with error_stage("execute"), session.connection.cursor() as cursor:
                session.execute(cursor, statement, built.parameters)
                for _ in range(built.limit + 1):
                    session.check()
                    record = cursor.fetchone()
                    if record is None:
                        break
                    if len(rows) == built.limit:
                        truncated, reason = True, "row_limit"
                        break
                    row = []
                    for value, description_item in zip(
                        record, description, strict=True
                    ):
                        if value is not None and description_item[1] in BINARY_IDS:
                            value = {"binary": True, "content_returned": False}
                        elif (
                            isinstance(value, str)
                            and len(value) > self.settings.max_cell_chars
                        ):
                            value = {
                                "truncated": True,
                                "preview": value[: self.settings.max_cell_chars],
                            }
                        row.append(safe_value(value))
                    row_size = len(
                        json.dumps(row, ensure_ascii=False, allow_nan=False).encode(
                            "utf-8"
                        )
                    )
                    if size + row_size + 1 > byte_budget:
                        truncated, reason = True, "response_bytes"
                        break
                    rows.append(row)
                    size += row_size + 1
                    session.check()
            return envelope | {
                "rows": rows,
                "returned_rows": len(rows),
                "row_limit": built.limit,
                "truncated": truncated,
                "truncation_reason": reason,
            }

    def read_database_settings(self, source, database, names=None):
        allowed = {
            "collation",
            "compatibility_level",
            "is_read_only",
            "snapshot_isolation_state_desc",
            "is_read_committed_snapshot_on",
        }
        selected = sorted(allowed) if names is None else names
        if set(selected) - allowed:
            unsupported("database_setting")
        with open_session(source, self.settings, database) as session:
            row = session.rows(
                "SELECT collation_name AS collation, compatibility_level, is_read_only, snapshot_isolation_state_desc, "
                "is_read_committed_snapshot_on FROM sys.databases WHERE name=DB_NAME()",
                limit=1,
            )[0]
        return self._response(
            source, database, settings=[{"name": k, "value": row[k]} for k in selected]
        )
