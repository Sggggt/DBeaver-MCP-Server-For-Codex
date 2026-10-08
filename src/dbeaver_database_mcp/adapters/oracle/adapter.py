"""Oracle 12.1+ implementation of the public database adapter contract."""

import json
import math
from datetime import date, datetime
from decimal import Decimal

import oracledb

from ...core.adapter import DatabaseAdapter, unsupported
from ...core.errors import QueryRejected, error_stage, register_error_translator
from ...core.validation import validate_query
from .catalog_guard import CatalogGuard
from .connection import parse_configuration
from .errors import translate_database_error
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

register_error_translator(translate_database_error)

OBJECT_TYPES = {
    "table": "TABLE",
    "view": "VIEW",
    "materialized_view": "MATERIALIZED VIEW",
}
CATALOG = (
    "FROM SYS.ALL_OBJECTS o "
    "LEFT JOIN SYS.ALL_TAB_COMMENTS c ON c.owner=o.owner AND c.table_name=o.object_name "
    "LEFT JOIN SYS.ALL_TABLES t ON t.owner=o.owner AND t.table_name=o.object_name "
    "WHERE o.object_type IN ('TABLE','VIEW','MATERIALIZED VIEW') "
    "AND (o.object_type<>'TABLE' OR NOT EXISTS (SELECT 1 FROM SYS.ALL_MVIEWS m "
    "WHERE m.owner=o.owner AND m.mview_name=o.object_name)) "
)
NLS_SETTINGS = {
    "NLS_LANGUAGE",
    "NLS_TERRITORY",
    "NLS_CURRENCY",
    "NLS_ISO_CURRENCY",
    "NLS_NUMERIC_CHARACTERS",
    "NLS_DATE_FORMAT",
    "NLS_DATE_LANGUAGE",
    "NLS_SORT",
    "NLS_CALENDAR",
    "NLS_TIMESTAMP_FORMAT",
    "NLS_TIMESTAMP_TZ_FORMAT",
    "NLS_COMP",
    "NLS_LENGTH_SEMANTICS",
    "NLS_NCHAR_CONV_EXCP",
}


def safe_value(value, max_chars=None):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, oracledb.LOB):
        # Only clipped CLOB projections reach this function; never read a BLOB.
        if value.type not in {oracledb.DB_TYPE_CLOB, oracledb.DB_TYPE_NCLOB}:
            return {"binary": True, "content_returned": False}
        return value.read(1, max_chars or 4000)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, str) and max_chars is not None and len(value) > max_chars:
        return {"truncated": True, "preview": value[:max_chars]}
    if isinstance(value, dict):
        return {key: safe_value(item, max_chars) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_value(item, max_chars) for item in value]
    if isinstance(value, bytes):
        return {"binary": True, "bytes": len(value), "content_returned": False}
    return value


class OracleAdapter(DatabaseAdapter):
    database_type = "oracle"
    parse_configuration = staticmethod(parse_configuration)

    @classmethod
    def capabilities(cls):
        return {
            "database_type": cls.database_type,
            "minimum_version": "12.1",
            "driver_mode": "thin",
            "functions": sorted(set(FUNCTIONS) | set(CONTEXT)),
            "aggregates": list(AGGREGATES),
            "windows": list(WINDOWS),
            "conditions": CONDITIONS,
            "casts": list(TYPES),
            "object_types": list(OBJECT_TYPES),
            "features": [
                "joins_on",
                "group_by",
                "having",
                "distinct",
                "case",
                "cte",
                "subquery",
                "union",
                "union_all",
                "intersect",
                "except",
                "values",
                "pagination",
            ],
            "explain": False,
            "database_settings": "nls_session",
            "postgresql_logs": False,
            "unsupported": [
                "explain_plan_writes",
                "recursive_cte",
                "distinct_on",
                "aggregate_filter",
                "joins_using",
                "lateral",
                "table_functions",
                "arrays",
                "jsonb",
                "SQL_boolean",
                "remote_database_links",
                "unverified_functions_or_policies",
                "virtual_columns",
                "function_or_domain_indexes",
                "wallet_or_custom_tns_transport",
            ],
        }

    def _response(self, source, schema="", **payload):
        return safe_value(
            source.model_identity(schema or source.default_schema) | payload
        )

    def _context(self, session, source, schema=""):
        row = session.rows(
            "SELECT SYS_CONTEXT('USERENV','CURRENT_SCHEMA') AS \"schema\", "
            "SYS_CONTEXT('USERENV','DB_NAME') AS \"database_name\" FROM SYS.DUAL",
            limit=1,
        )[0]
        selected = schema or source.default_schema or row["schema"]
        if schema and not session.rows(
            "SELECT owner FROM SYS.ALL_OBJECTS WHERE owner=:schema FETCH FIRST 1 ROWS ONLY",
            {"schema": schema},
            limit=1,
        ):
            raise QueryRejected(
                "Oracle schema 不存在或账号不可见。",
                code="SCHEMA_NOT_ACCESSIBLE",
                path="schema",
            )
        return selected, row["database_name"]

    def list_databases(self, source):
        with open_session(source, self.settings, source.database) as session:
            schema, name = self._context(session, source)
        return self._response(
            source,
            schema,
            default_database=source.database,
            databases=[
                {
                    "name": source.database,
                    "is_default": True,
                    "is_template": False,
                    "kind": "saved_service",
                    "database_name": name,
                }
            ],
            returned=1,
        )

    def _schemas(self, session):
        rows = session.rows(
            'SELECT owner AS "name", SUM(CASE WHEN object_type=\'TABLE\' THEN 1 ELSE 0 END) AS "table_count", '
            "SUM(CASE WHEN object_type IN ('VIEW','MATERIALIZED VIEW') THEN 1 ELSE 0 END) AS \"view_count\", "
            'COUNT(*) AS "object_count" FROM SYS.ALL_OBJECTS '
            "WHERE object_type IN ('TABLE','VIEW','MATERIALIZED VIEW') "
            "GROUP BY owner ORDER BY owner FETCH FIRST 1000 ROWS ONLY",
            limit=1000,
        )
        for row in rows:
            for key in ("table_count", "view_count", "object_count"):
                row[key] = int(row[key])
        return rows

    def list_schemas(self, source, database):
        with open_session(source, self.settings, database) as session:
            schema, _ = self._context(session, source)
            contexts = self._schemas(session)
        return self._response(
            source,
            schema,
            schemas=[row["name"] for row in contexts],
            schema_contexts=contexts,
            returned=len(contexts),
        )

    def status(self, source, database):
        with open_session(source, self.settings, database) as session:
            schema, name = self._context(session, source)
            version, contexts = session.connection.version, self._schemas(session)
        return self._response(
            source,
            schema,
            provider=source.provider,
            driver=source.driver,
            server_version=version,
            database_name=name,
            schemas=[row["name"] for row in contexts],
            schema_contexts=contexts,
            read_only={
                "transaction": True,
                "session_default": None,
                "enforced_by_mcp": True,
            },
            limits={
                "hard_max_rows": self.settings.hard_max_rows,
                "statement_timeout_ms": self.settings.statement_timeout_ms,
                "max_cell_chars": self.settings.max_cell_chars,
                "max_result_bytes": self.settings.max_result_bytes,
            },
            credentials_exposed=False,
            addresses_exposed=False,
            data_persistence="none",
        )

    def scan_database(
        self, source, database, schema, *, name_pattern="", object_types=None, limit=200
    ):
        types = object_types or ["table", "view", "materialized_view"]
        if not set(types) <= set(OBJECT_TYPES):
            unsupported("object_types")
        binds = {
            "schema": schema,
            "pattern": name_pattern or "%",
            "limit": min(limit, 1000) + 1,
        }
        markers = []
        for i, kind in enumerate(dict.fromkeys(OBJECT_TYPES[t] for t in types)):
            binds[f"type{i}"] = kind
            markers.append(f":type{i}")
        with open_session(source, self.settings, database) as session:
            self._context(session, source, schema)
            rows = session.rows(
                'SELECT o.object_name AS "name", o.object_type AS "type", SUBSTR(c.comments,1,4000) AS "comment", t.num_rows AS "approximate_rows" '
                + CATALOG
                + "AND o.owner=:schema AND LOWER(o.object_name) LIKE LOWER(:pattern) "
                "AND o.object_type IN ("
                + ",".join(markers)
                + ") ORDER BY o.object_name FETCH FIRST :limit ROWS ONLY",
                binds,
                limit=min(limit, 1000) + 1,
            )
        for row in rows:
            row["type"] = row["type"].lower().replace(" ", "_")
            row.update(database=database, schema=schema)
            if row["approximate_rows"] is not None:
                row["approximate_rows"] = int(row["approximate_rows"])
        return self._response(
            source,
            schema,
            objects=rows[:limit],
            returned=min(len(rows), limit),
            truncated=len(rows) > limit,
            filters={"name_pattern": name_pattern, "object_types": types},
        )

    def search_catalog(self, source, database, query, *, schema="", limit=50):
        with open_session(source, self.settings, database) as session:
            selected, _ = self._context(session, source, schema)
            rows = session.rows(
                'SELECT o.owner AS "schema",o.object_name AS "object",o.object_type AS object_type, '
                'a.column_name,a.data_type,SUBSTR(c.comments,1,4000) AS "object_comment",SUBSTR(cc.comments,1,4000) AS "column_comment" '
                "FROM SYS.ALL_OBJECTS o LEFT JOIN SYS.ALL_TAB_COMMENTS c ON c.owner=o.owner AND c.table_name=o.object_name "
                "LEFT JOIN SYS.ALL_TAB_COLUMNS a ON a.owner=o.owner AND a.table_name=o.object_name "
                "LEFT JOIN SYS.ALL_COL_COMMENTS cc ON cc.owner=a.owner AND cc.table_name=a.table_name AND cc.column_name=a.column_name "
                "WHERE o.object_type IN ('TABLE','VIEW','MATERIALIZED VIEW') AND (:schema IS NULL OR o.owner=:schema) "
                "AND (LOWER(o.object_name) LIKE :pattern OR LOWER(a.column_name) LIKE :pattern "
                "OR LOWER(c.comments) LIKE :pattern OR LOWER(cc.comments) LIKE :pattern) "
                "ORDER BY o.owner,o.object_name,a.column_id FETCH FIRST :limit ROWS ONLY",
                {
                    "schema": schema or None,
                    "pattern": "%" + query.lower() + "%",
                    "limit": min(limit, 200) + 1,
                },
                limit=min(limit, 200) + 1,
            )
        for row in rows:
            row["column"] = row.pop("column_name") or ""
            row["object_type"] = row["object_type"].lower().replace(" ", "_")
            row["database"] = database
        return self._response(
            source,
            selected,
            matches=rows[:limit],
            returned=min(len(rows), limit),
            truncated=len(rows) > limit,
            query=query,
            schema_scope=[schema]
            if schema
            else sorted({row["schema"] for row in rows}),
        )

    def table_context(self, source, database, schema, table):
        binds = {"owner": schema, "name": table}
        with open_session(source, self.settings, database) as session:
            self._context(session, source, schema)
            objects = session.rows(
                'SELECT o.object_name AS "name",o.object_type AS "type",SUBSTR(c.comments,1,4000) AS "comment",t.num_rows AS "approximate_rows" '
                + CATALOG
                + "AND o.owner=:owner AND o.object_name=:name",
                binds,
                limit=2,
            )
            if not objects:
                raise QueryRejected(
                    "Oracle 对象不存在或账号不可见。",
                    code="TABLE_NOT_FOUND",
                    path="table",
                )
            columns = session.rows(
                'SELECT a.column_id AS "ordinal",a.column_name AS "name",a.data_type,a.data_length,a.data_precision,a.data_scale, '
                'a.nullable,SUBSTR(c.comments,1,4000) AS "comment" FROM SYS.ALL_TAB_COLUMNS a '
                "LEFT JOIN SYS.ALL_COL_COMMENTS c ON c.owner=a.owner AND c.table_name=a.table_name AND c.column_name=a.column_name "
                "WHERE a.owner=:owner AND a.table_name=:name ORDER BY a.column_id",
                binds,
                limit=1000,
            )
            keys = session.rows(
                'SELECT c.constraint_name AS "name",c.constraint_type,k.column_name,k.position,c.r_owner AS "referenced_schema",c.r_constraint_name AS "referenced_constraint" '
                "FROM SYS.ALL_CONSTRAINTS c JOIN SYS.ALL_CONS_COLUMNS k ON k.owner=c.owner AND k.constraint_name=c.constraint_name "
                "WHERE c.owner=:owner AND c.table_name=:name AND c.constraint_type IN ('P','R','U') ORDER BY c.constraint_name,k.position",
                binds,
                limit=1000,
            )
            indexes = session.rows(
                'SELECT i.index_name AS "name",i.index_type,i.uniqueness,c.column_name,c.column_position '
                "FROM SYS.ALL_INDEXES i JOIN SYS.ALL_IND_COLUMNS c ON c.index_owner=i.owner AND c.index_name=i.index_name "
                "WHERE i.table_owner=:owner AND i.table_name=:name ORDER BY i.index_name,c.column_position",
                binds,
                limit=1000,
            )
        for column in columns:
            column["nullable"] = column["nullable"] == "Y"
            for key in ("ordinal", "data_length", "data_precision", "data_scale"):
                if column[key] is not None:
                    column[key] = int(column[key])
        objects[0]["type"] = objects[0]["type"].lower().replace(" ", "_")
        if objects[0]["approximate_rows"] is not None:
            objects[0]["approximate_rows"] = int(objects[0]["approximate_rows"])
        return self._response(
            source,
            schema,
            **objects[0],
            qualified_name=f"{database}.{schema}.{table}",
            columns=columns,
            column_count=len(columns),
            primary_key=[
                row["column_name"] for row in keys if row["constraint_type"] == "P"
            ],
            foreign_keys=[row for row in keys if row["constraint_type"] == "R"],
            indexes=indexes,
        )

    def _transport(self, built, description, max_identifier_bytes):
        projections, metadata = [], []
        for i, column in enumerate(description):
            field = '"__result".' + identifier(column[0], max_identifier_bytes)
            dtype = column[1]
            metadata.append({"name": column[0], "database_type": dtype.name})
            if dtype in {oracledb.DB_TYPE_RAW, oracledb.DB_TYPE_BLOB}:
                expression = f"CASE WHEN {field} IS NULL THEN NULL ELSE 1 END"
                length = "NULL"
            elif dtype in {
                oracledb.DB_TYPE_VARCHAR,
                oracledb.DB_TYPE_NVARCHAR,
                oracledb.DB_TYPE_CHAR,
                oracledb.DB_TYPE_NCHAR,
                oracledb.DB_TYPE_CLOB,
                oracledb.DB_TYPE_NCLOB,
            }:
                expression = f"SUBSTR({field},1,:cell_limit)"
                length = f"LENGTH({field})"
            elif dtype in {
                oracledb.DB_TYPE_NUMBER,
                oracledb.DB_TYPE_BINARY_FLOAT,
                oracledb.DB_TYPE_BINARY_DOUBLE,
                oracledb.DB_TYPE_DATE,
                oracledb.DB_TYPE_TIMESTAMP,
                oracledb.DB_TYPE_TIMESTAMP_TZ,
                oracledb.DB_TYPE_TIMESTAMP_LTZ,
            }:
                expression, length = field, "NULL"
            else:
                unsupported("result_type")
            # Positional output names keep wrapper identifiers within the 12.1 limit.
            projections.extend([f'{expression} AS "v{i}"', f'{length} AS "l{i}"'])
        sql = (
            "SELECT "
            + ",".join(projections)
            + " FROM ("
            + built.statement
            + ') "__result"'
        )
        parameters = dict(built.parameters)
        if ":cell_limit" in sql:
            parameters["cell_limit"] = self.settings.max_cell_chars
        return sql, parameters, metadata

    def structured_query(
        self, source, database, schema, query, *, mode="execute", timeout_ms=None
    ):
        validate_query(query)
        if mode == "explain":
            unsupported("explain_plan_writes")
        if mode not in {"analyze", "execute"}:
            raise QueryRejected("未知查询模式。", code="INVALID_PARAMETER")
        if query.get("limit", 501) > self.settings.hard_max_rows:
            raise QueryRejected(
                "查询超过行数上限。", code="INVALID_PARAMETER", path="query.limit"
            )
        with open_session(source, self.settings, database, timeout_ms) as session:
            self._context(session, source, schema)
            max_identifier_bytes = (
                30
                if tuple(int(p) for p in session.connection.version.split(".")[:2])
                < (12, 2)
                else 128
            )
            catalog = CatalogGuard(session).prepare(query, schema)
            built = compile_query(schema, query, catalog, max_identifier_bytes)
            with error_stage("describe"), session.connection.cursor() as cursor:
                session.execute(
                    cursor,
                    "SELECT * FROM (" + built.statement + ") WHERE 1=0",
                    built.parameters,
                )
                description = cursor.description
            statement, parameters, metadata = self._transport(
                built, description, max_identifier_bytes
            )
            if mode == "analyze":
                return self._response(
                    source,
                    schema,
                    analysis=built.summary(),
                    columns_metadata=metadata,
                    read_only_verified=True,
                )
            envelope = self._response(
                source,
                schema,
                analysis=built.summary(),
                columns=list(built.outputs),
                columns_metadata=metadata,
                permission_scope="database_account",
                row_security="database_native",
                read_only_verified=True,
            )
            byte_budget = (
                self.settings.max_result_bytes
                - len(
                    json.dumps(envelope, ensure_ascii=False, allow_nan=False).encode(
                        "utf-8"
                    )
                )
                - 256
            )
            rows, size, truncated, reason = [], 0, False, None
            with error_stage("execute"), session.connection.cursor() as cursor:
                session.execute(cursor, statement, parameters)
                for _ in range(built.limit + 1):
                    session.check()
                    record = cursor.fetchone()
                    if record is None:
                        break
                    if len(rows) == built.limit:
                        truncated, reason = True, "row_limit"
                        break
                    row = []
                    for i, column in enumerate(description):
                        value, length = record[i * 2 : i * 2 + 2]
                        if column[1] in {oracledb.DB_TYPE_RAW, oracledb.DB_TYPE_BLOB}:
                            value = (
                                {"binary": True, "content_returned": False}
                                if value is not None
                                else None
                            )
                        else:
                            value = safe_value(value, self.settings.max_cell_chars)
                            if (
                                length is not None
                                and length > self.settings.max_cell_chars
                            ):
                                value = {"truncated": True, "preview": value}
                        row.append(value)
                    session.check()
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
            return self._response(
                source,
                schema,
                analysis=built.summary(),
                columns=list(built.outputs),
                columns_metadata=metadata,
                permission_scope="database_account",
                row_security="database_native",
                rows=rows,
                returned_rows=len(rows),
                row_limit=built.limit,
                truncated=truncated,
                truncation_reason=reason,
                read_only_verified=True,
            )

    def read_database_settings(self, source, database, names=None):
        selected = names or sorted(NLS_SETTINGS)
        if not set(selected) <= NLS_SETTINGS:
            unsupported("non_nls_database_settings")
        parameters = {f"n{i}": name for i, name in enumerate(selected)}
        markers = ",".join(":" + name for name in parameters)
        with open_session(source, self.settings, database) as session:
            schema, _ = self._context(session, source)
            settings = session.rows(
                'SELECT parameter AS "name",value AS "setting" FROM SYS.NLS_SESSION_PARAMETERS WHERE parameter IN ('
                + markers
                + ") ORDER BY parameter",
                parameters,
                limit=100,
            )
        return self._response(
            source,
            schema,
            settings=settings,
            returned=len(settings),
            scope="nls_session",
        )
