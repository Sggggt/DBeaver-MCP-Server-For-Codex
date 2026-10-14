"""Bounded view metadata reads, independent of executable dependency auditing."""

from ...core.adapter import object_definition, resolve_view, view_dependencies
from ...core.errors import error_stage


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
    with adapter._connection(source, database, schema) as (connection, _, context):
        with error_stage("catalog"), connection.cursor() as cursor:
            cursor.execute(
                "SELECT c.oid::text AS view_id,n.nspname AS schema,c.relname AS name, "
                "CASE WHEN c.relkind='m' THEN 'materialized_view' ELSE 'view' END AS object_type "
                "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=%(schema)s AND c.relname=%(name)s AND c.relkind IN ('v','m') LIMIT 2",
                {"schema": schema, "name": view},
            )
            identity = resolve_view(cursor.fetchmany(2))
            oid = int(identity["view_id"])
            if include_definition:
                cursor.execute(
                    "SELECT pg_catalog.has_function_privilege('pg_catalog.pg_get_viewdef(oid,boolean)','EXECUTE') "
                    "AND pg_catalog.has_table_privilege('pg_catalog.pg_rewrite','SELECT') AS allowed"
                )
                if not cursor.fetchone()["allowed"]:
                    definition = object_definition(
                        None, None, definition_offset, "permission_denied"
                    )
                else:
                    cursor.execute(
                        "SELECT pg_catalog.length(definition) AS total_chars, "
                        "pg_catalog.substr(definition,%(start)s,%(chars)s) AS fragment "
                        "FROM (SELECT pg_catalog.pg_get_viewdef(c.oid,false) AS definition "
                        "FROM pg_catalog.pg_class c WHERE c.oid=%(id)s::oid AND c.relkind IN ('v','m')) v",
                        {
                            "id": oid,
                            "start": definition_offset + 1,
                            "chars": min(
                                max_definition_chars, adapter.settings.max_cell_chars
                            ),
                        },
                    )
                    row = cursor.fetchone()
                    definition = object_definition(
                        row["fragment"] if row else None,
                        row["total_chars"] if row else None,
                        definition_offset,
                        "available"
                        if row and row["total_chars"] is not None
                        else "unavailable",
                    )
            if include_dependencies:
                modern = connection.info.server_version >= 90300
                function = (
                    "pg_catalog.pg_identify_object(oid,oid,integer)"
                    if modern
                    else "pg_catalog.pg_describe_object(oid,oid,integer)"
                )
                cursor.execute(
                    "SELECT pg_catalog.has_table_privilege('pg_catalog.pg_rewrite','SELECT') "
                    "AND pg_catalog.has_table_privilege('pg_catalog.pg_depend','SELECT') "
                    "AND pg_catalog.has_function_privilege(%(function)s,'EXECUTE') AS allowed",
                    {"function": function},
                )
                dependency_status = "permission_denied"
                if cursor.fetchone()["allowed"]:
                    projection = (
                        "i.schema,i.name,i.type AS object_type,pg_catalog.substr(i.identity,1,4000) AS identity, "
                        "pg_catalog.length(i.identity)>4000 AS identity_truncated "
                        if modern
                        else "NULL::text AS schema,NULL::text AS name,'object'::text AS object_type, "
                        "pg_catalog.substr(pg_catalog.pg_describe_object(d.refclassid,d.refobjid,0),1,4000) AS identity, "
                        "pg_catalog.length(pg_catalog.pg_describe_object(d.refclassid,d.refobjid,0))>4000 AS identity_truncated "
                    )
                    cursor.execute(
                        "SELECT d.refclassid::text AS catalog_id,d.refobjid::text AS object_id, "
                        + projection
                        + "FROM (SELECT DISTINCT d.refclassid,d.refobjid "
                        "FROM pg_catalog.pg_rewrite r JOIN pg_catalog.pg_depend d "
                        "ON d.classid='pg_catalog.pg_rewrite'::regclass AND d.objid=r.oid "
                        "WHERE r.ev_class=%(id)s::oid AND r.rulename='_RETURN' "
                        "AND NOT (d.refclassid='pg_catalog.pg_class'::regclass AND d.refobjid=%(id)s::oid) "
                        "ORDER BY d.refclassid,d.refobjid LIMIT %(limit)s OFFSET %(offset)s) d "
                        + (
                            "CROSS JOIN LATERAL pg_catalog.pg_identify_object(d.refclassid,d.refobjid,0) i "
                            if modern
                            else ""
                        )
                        + "ORDER BY d.refclassid,d.refobjid",
                        {
                            "id": oid,
                            "limit": dependencies_limit + 1,
                            "offset": dependencies_offset,
                        },
                    )
                    dependencies = cursor.fetchmany(dependencies_limit + 1)
                    dependency_status = "available"
        return adapter._response(
            context,
            view=identity,
            definition_scope="view",
            definition_format="select",
            **definition,
            **view_dependencies(
                dependencies, dependencies_offset, dependencies_limit, dependency_status
            ),
            executed=False,
            permission_scope="database_account",
            read_only={
                "transaction": True,
                "session_default": True,
                "rollback_on_exit": True,
            },
        )
