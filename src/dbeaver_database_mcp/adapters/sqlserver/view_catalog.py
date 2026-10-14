"""View catalog inspection never invokes the SELECT execution safety guard."""

from ...core.adapter import object_definition, resolve_view, view_dependencies
from ...core.errors import QueryRejected, error_stage
from .session import open_session


def read_definition(session, oid, offset, chars):
    modern = session.major_version >= 11
    expression = (
        "m.definition COLLATE Latin1_General_100_CI_AS_SC" if modern else "m.definition"
    )
    rows = session.rows(
        "SELECT LEN(" + expression + "+N'#')-1 AS total_chars, "
        "CONVERT(varbinary(max),SUBSTRING("
        + expression
        + ",%(start)s,%(chars)s)) AS fragment_bytes "
        "FROM sys.sql_modules m WHERE m.object_id=%(id)s",
        {"id": oid, "start": offset + 1, "chars": chars if modern else chars * 2 + 1},
        limit=1,
    )
    row = rows[0] if rows else None
    if not row or row["total_chars"] is None or row["fragment_bytes"] is None:
        return object_definition(None, None, offset, "unavailable")
    raw = bytes(row["fragment_bytes"])
    # Pre-2012 SUBSTRING counts UTF-16 units. Overfetch (at most 16002 bytes,
    # below TEXTSIZE), trim a split pair and advance by consumed UTF-16 units.
    if (
        not modern
        and len(raw) >= 2
        and 0xD800 <= int.from_bytes(raw[-2:], "little") <= 0xDBFF
    ):
        raw = raw[:-2]
    try:
        text = raw.decode("utf-16-le")[:chars]
    except UnicodeError:
        raise QueryRejected(
            "视图定义分页位置无法解码。",
            code="INVALID_PARAMETER",
            path="definition_offset",
            stage="catalog",
            hint="使用上次返回的 next_definition_offset 继续读取。",
        ) from None
    consumed = len(text) if modern else len(text.encode("utf-16-le")) // 2
    return object_definition(text, row["total_chars"], offset, "available", consumed)


def read_view_context(
    adapter,
    source,
    database,
    schema,
    view,
    *,
    include_definition=True,
    definition_offset=0,
    max_definition_chars=4000,
    include_dependencies=True,
    dependencies_offset=0,
    dependencies_limit=100,
):
    definition = object_definition(None, None, definition_offset, "not_requested")
    dependencies, dependency_status = [], "not_requested"
    with open_session(source, adapter.settings, database) as session:
        with error_stage("catalog"):
            adapter._schema(session, schema)
            identity = resolve_view(
                session.rows(
                    "SELECT CONVERT(varchar(20),o.object_id) AS view_id,s.name AS [schema],o.name,'view' AS object_type, "
                    "HAS_PERMS_BY_NAME(QUOTENAME(s.name)+'.'+QUOTENAME(o.name),'OBJECT','VIEW DEFINITION') AS can_view_definition, "
                    "OBJECTPROPERTYEX(o.object_id,'IsEncrypted') AS encrypted, "
                    "m.is_schema_bound,m.uses_quoted_identifier "
                    "FROM sys.objects o JOIN sys.schemas s ON s.schema_id=o.schema_id "
                    "LEFT JOIN sys.sql_modules m ON m.object_id=o.object_id "
                    "WHERE s.name=%(schema)s AND o.name=%(name)s AND o.type='V'",
                    {"schema": schema, "name": view},
                    limit=2,
                )
            )
            oid = int(identity["view_id"])
            if include_definition:
                if identity["encrypted"]:
                    definition = object_definition(
                        None, None, definition_offset, "encrypted"
                    )
                elif identity["can_view_definition"] != 1:
                    definition = object_definition(
                        None, None, definition_offset, "permission_denied"
                    )
                else:
                    definition = read_definition(
                        session,
                        oid,
                        definition_offset,
                        min(max_definition_chars, adapter.settings.max_cell_chars),
                    )
            definition["definition_offset_units"] = (
                "database_characters"
                if session.major_version >= 11
                else "utf16_code_units"
            )
            if include_dependencies:
                access = session.rows(
                    "SELECT HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','VIEW DEFINITION') AS view_allowed, "
                    "HAS_PERMS_BY_NAME('sys.sql_expression_dependencies','OBJECT','SELECT') AS select_allowed",
                    limit=1,
                )[0]
                dependency_status = "permission_denied"
                if access["view_allowed"] == 1 and access["select_allowed"] == 1:
                    dependencies = session.rows(
                        "SELECT * FROM (SELECT d.*,ROW_NUMBER() OVER (ORDER BY "
                        "[server],[database],[schema],name,dependency_class,object_id) AS row_number "
                        "FROM (SELECT DISTINCT d.referenced_server_name AS [server], "
                        "d.referenced_database_name AS [database], "
                        "COALESCE(d.referenced_schema_name,OBJECT_SCHEMA_NAME(d.referenced_id)) AS [schema], "
                        "d.referenced_entity_name AS name,CONVERT(varchar(20),d.referenced_id) AS object_id, "
                        "d.referenced_class_desc AS dependency_class, "
                        "COALESCE(o.type_desc,d.referenced_class_desc) AS object_type, "
                        "CASE WHEN d.referenced_server_name IS NULL THEN 0 ELSE 1 END AS remote, "
                        "CASE WHEN d.referenced_database_name IS NULL THEN 0 ELSE 1 END AS cross_database, "
                        "CASE WHEN d.referenced_id IS NULL THEN 1 ELSE 0 END AS unresolved, "
                        "d.is_caller_dependent,d.is_ambiguous FROM sys.sql_expression_dependencies d "
                        "LEFT JOIN sys.objects o ON d.referenced_class=1 AND o.object_id=d.referenced_id "
                        "WHERE d.referencing_id=%(id)s) d) page "
                        "WHERE row_number>%(offset)s AND row_number<=%(end)s ORDER BY row_number",
                        {
                            "id": oid,
                            "offset": dependencies_offset,
                            "end": dependencies_offset + dependencies_limit + 1,
                        },
                        limit=dependencies_limit + 1,
                    )
                    for dependency in dependencies:
                        dependency.pop("row_number", None)
                        for flag in (
                            "remote",
                            "cross_database",
                            "unresolved",
                            "is_caller_dependent",
                            "is_ambiguous",
                        ):
                            dependency[flag] = bool(dependency[flag])
                    dependency_status = "available"
        return adapter._response(
            source,
            database,
            schema,
            view=identity,
            definition_scope="view",
            definition_format="module",
            **definition,
            **view_dependencies(
                dependencies, dependencies_offset, dependencies_limit, dependency_status
            ),
            executed=False,
            permission_scope="database_account",
            read_only=adapter.capabilities()["read_only"]
            | {"database_read_only": session.database_read_only},
        )
