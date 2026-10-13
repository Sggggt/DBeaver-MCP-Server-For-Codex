"""Conservative dependency guard for ordinary local SQL Server tables."""

from ...core.catalog import Table, requested_relations
from ...core.errors import QueryRejected
from .query import identifier

SAFE_TYPES = {
    "bigint",
    "int",
    "smallint",
    "tinyint",
    "bit",
    "decimal",
    "numeric",
    "money",
    "smallmoney",
    "float",
    "real",
    "char",
    "varchar",
    "text",
    "nchar",
    "nvarchar",
    "ntext",
    "binary",
    "varbinary",
    "image",
    "timestamp",
    "rowversion",
    "date",
    "datetime",
    "smalldatetime",
    "datetime2",
    "datetimeoffset",
    "time",
    "uniqueidentifier",
}


class CatalogGuard:
    def __init__(self, session):
        self.session = session

    def reject(self, code="UNSAFE_DEPENDENCY"):
        raise QueryRejected(
            "SQL Server 对象无法通过只读依赖检查。",
            code=code,
            stage="safety",
            hint="当前仅执行可核验的本地普通表；不执行视图、同义词、计算列、CLR、外部表或行安全策略。",
        )

    def columns(self, object_id):
        return self.session.rows(
            "SELECT TOP (1025) c.column_id, c.name, t.name AS data_type, c.max_length, "
            "c.precision, c.scale, c.is_nullable, c.is_computed, t.is_assembly_type, t.is_user_defined, "
            "HAS_PERMS_BY_NAME(QUOTENAME(OBJECT_SCHEMA_NAME(c.object_id))+'.'+QUOTENAME(OBJECT_NAME(c.object_id)), "
            "'OBJECT','SELECT',c.name,'COLUMN') AS can_select "
            "FROM sys.columns c JOIN sys.types t ON t.user_type_id=c.user_type_id "
            "WHERE c.object_id=%(id)s ORDER BY c.column_id",
            {"id": object_id},
            limit=1025,
        )

    def prepare(self, query, schema):
        relations = requested_relations(query, schema, max_identifier_bytes=512)
        catalog = {}
        if relations:
            # Object visibility alone cannot prove that no hidden policy exists.
            permission = self.session.rows(
                "SELECT HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','VIEW DEFINITION') AS allowed",
                limit=1,
            )[0]["allowed"]
            if permission != 1:
                self.reject("DEPENDENCY_VISIBILITY_REQUIRED")
        for namespace, name, _ in relations:
            identifier(namespace)
            identifier(name)
            rows = self.session.rows(
                "SELECT o.object_id, o.type, o.is_ms_shipped FROM sys.objects o "
                "JOIN sys.schemas s ON s.schema_id=o.schema_id WHERE s.name=%(schema)s AND o.name=%(table)s",
                {"schema": namespace, "table": name},
                limit=2,
            )
            if len(rows) != 1:
                self.reject("TABLE_NOT_FOUND")
            row = rows[0]
            if row["type"].strip() != "U" or row["is_ms_shipped"]:
                self.reject("UNVERIFIED_VIEW_OR_SYNONYM")
            oid = row["object_id"]
            if self.session.major_version >= 13:
                if self.session.rows(
                    "SELECT TOP (1) object_id FROM sys.external_tables WHERE object_id=%(id)s",
                    {"id": oid},
                    limit=1,
                ):
                    self.reject("UNVERIFIED_FOREIGN_READER")
                if self.session.rows(
                    "SELECT TOP (1) p.object_id FROM sys.security_predicates p "
                    "JOIN sys.security_policies s ON s.object_id=p.object_id "
                    "WHERE p.target_object_id=%(id)s AND s.is_enabled=1",
                    {"id": oid},
                    limit=1,
                ):
                    self.reject("UNSAFE_POLICY")
            columns = self.columns(oid)
            if not columns or len(columns) > 1024:
                self.reject("DEPENDENCY_LIMIT")
            if any(c["is_computed"] for c in columns):
                self.reject("UNSAFE_COMPUTED_COLUMN")
            if any(
                c["is_assembly_type"]
                or c["is_user_defined"]
                or c["data_type"] not in SAFE_TYPES
                for c in columns
            ):
                self.reject("UNSUPPORTED_TYPE")
            catalog[namespace, name] = Table(
                oid,
                {c["name"]: c["data_type"] for c in columns},
                frozenset(c["name"] for c in columns if c["can_select"] == 1),
            )
        return catalog
