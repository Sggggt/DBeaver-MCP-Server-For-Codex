"""Fail-closed dependency checks for local SQL Server tables and views."""

from sqlglot import ErrorLevel, exp
from sqlglot.dialects.tsql import TSQL
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.tokens import TokenType

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

MAX_OBJECTS = 256
MAX_DEPTH = 32
MAX_DEFINITION_BYTES = 128000
MAX_VIEW_NODES = 4000
SAFE_SYNTAX = {
    "Select",
    "From",
    "Table",
    "TableAlias",
    "Identifier",
    "Column",
    "Alias",
    "Star",
    "Where",
    "And",
    "Or",
    "Not",
    "EQ",
    "NEQ",
    "GT",
    "GTE",
    "LT",
    "LTE",
    "Between",
    "In",
    "Is",
    "Null",
    "Boolean",
    "Literal",
    "National",
    "Paren",
    "Order",
    "Ordered",
    "Group",
    "Having",
    "Join",
    "Distinct",
    "Limit",
    "LimitOptions",
    "Offset",
    "With",
    "CTE",
    "Subquery",
    "Union",
    "Intersect",
    "Except",
    "Add",
    "Sub",
    "Mul",
    "Div",
    "Mod",
    "Neg",
    "Case",
    "If",
    "DataType",
    "DataTypeParam",
    "Window",
    "WindowSpec",
    "Like",
    "Tuple",
    "Collate",
    "WithTableHint",
}
# sqlglot normalizes T-SQL builtins (LEN -> LENGTH, ISNULL -> COALESCE,
# GETDATE -> CURRENT_TIMESTAMP). Only these pure, unqualified builtins pass.
SAFE_FUNCTIONS = {
    "ABS",
    "CEIL",
    "CEILING",
    "FLOOR",
    "ROUND",
    "SQRT",
    "POWER",
    "SIGN",
    "EXP",
    "LN",
    "LOG",
    "LOWER",
    "UPPER",
    "LENGTH",
    "DATALENGTH",
    "LTRIM",
    "RTRIM",
    "TRIM",
    "SUBSTRING",
    "REPLACE",
    "LEFT",
    "RIGHT",
    "REVERSE",
    "COALESCE",
    "NULLIF",
    "CONCAT",
    "CAST",
    "TRY_CAST",
    "CONVERT",
    "TRY_CONVERT",
    "COUNT",
    "SUM",
    "AVG",
    "MIN",
    "MAX",
    "CURRENT_DATE",
    "CURRENT_TIMESTAMP",
    "DATE_ADD",
    "DATE_DIFF",
    "DATEDIFF",
    "TIMESTAMP_DIFF",
    "TIME_STR_TO_TIME",
    "EXTRACT",
    "TIME_TO_STR",
    "YEAR",
    "MONTH",
    "DAY",
    "ROW_NUMBER",
    "RANK",
    "DENSE_RANK",
    "NTILE",
    "LAG",
    "LEAD",
    "FIRST_VALUE",
    "LAST_VALUE",
    "EXISTS",
}
SAFE_CAST_TYPES = {
    "BIGINT",
    "INT",
    "SMALLINT",
    "TINYINT",
    "BIT",
    "BOOLEAN",
    "DECIMAL",
    "MONEY",
    "SMALLMONEY",
    "FLOAT",
    "DOUBLE",
    "CHAR",
    "VARCHAR",
    "TEXT",
    "NCHAR",
    "NVARCHAR",
    "NTEXT",
    "BINARY",
    "VARBINARY",
    "IMAGE",
    "ROWVERSION",
    "DATE",
    "DATETIME",
    "SMALLDATETIME",
    "DATETIME2",
    "TIMESTAMP",
    "TIMESTAMPTZ",
    "DATETIMEOFFSET",
    "TIME",
    "UUID",
}
DATE_PARTS = {
    "YEAR",
    "QUARTER",
    "MONTH",
    "DAYOFYEAR",
    "DAY",
    "WEEK",
    "WEEKDAY",
    "HOUR",
    "MINUTE",
    "SECOND",
    "MILLISECOND",
    "MICROSECOND",
    "NANOSECOND",
    "TZOFFSET",
    "ISO_WEEK",
    "MAX",
}
READ_HINTS = {
    "NOLOCK",
    "READUNCOMMITTED",
    "READCOMMITTED",
    "READCOMMITTEDLOCK",
    "READPAST",
    "NOWAIT",
}


class ViewParser(TSQL.Parser):
    def _warn_unsupported(self):
        # The default fallback logs source SQL. Never emit database definitions.
        raise QueryRejected(
            "SQL Server 视图定义包含无法核验的语法。",
            code="UNVERIFIED_VIEW_DEFINITION",
            stage="safety",
        )


class CatalogGuard:
    def __init__(self, session):
        self.session = session
        self.verified = {}
        self.heights = {}
        self.visiting = set()

    def reject(self, code="UNSAFE_DEPENDENCY"):
        raise QueryRejected(
            "SQL Server 对象无法通过只读依赖检查。",
            code=code,
            stage="safety",
            hint="仅执行依赖可核验的本地表和视图；视图定义须可见，不含跨库引用、同义词、自定义函数、计算列、CLR、外部表或行安全策略。",
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
            catalog[namespace, name] = self.object(rows[0])
        return catalog

    def object(self, row, depth=0):
        oid, kind = row["object_id"], row["type"].strip()
        if oid in self.visiting:
            self.reject("UNVERIFIED_VIEW_DEPENDENCY")
        if oid in self.verified:
            if depth + self.heights[oid] >= MAX_DEPTH:
                self.reject("DEPENDENCY_LIMIT")
            return self.verified[oid]
        if depth >= MAX_DEPTH or len(self.verified) + len(self.visiting) >= MAX_OBJECTS:
            self.reject("DEPENDENCY_LIMIT")
        if kind not in {"U", "V"} or row["is_ms_shipped"]:
            self.reject("UNVERIFIED_VIEW_OR_SYNONYM")
        self.visiting.add(oid)
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
        if kind == "U" and any(c["is_computed"] for c in columns):
            self.reject("UNSAFE_COMPUTED_COLUMN")
        if any(
            c["is_assembly_type"]
            or c["is_user_defined"]
            or c["data_type"] not in SAFE_TYPES
            for c in columns
        ):
            self.reject("UNSUPPORTED_TYPE")
        height = self.view(oid, depth) if kind == "V" else 0
        table = Table(
            oid,
            {c["name"]: c["data_type"] for c in columns},
            frozenset(c["name"] for c in columns if c["can_select"] == 1),
        )
        self.visiting.remove(oid)
        self.verified[oid] = table
        self.heights[oid] = height
        return table

    def definition(self, oid):
        rows = self.session.rows(
            "SELECT DATALENGTH(definition) AS definition_bytes, uses_quoted_identifier "
            "FROM sys.sql_modules WHERE object_id=%(id)s",
            {"id": oid},
            limit=1,
        )
        if not rows or rows[0]["definition_bytes"] is None:
            self.reject("UNVERIFIED_VIEW_DEFINITION")
        size = rows[0]["definition_bytes"]
        if not 0 < size <= MAX_DEFINITION_BYTES:
            self.reject("DEPENDENCY_LIMIT")
        if rows[0]["uses_quoted_identifier"] != 1:
            self.reject("UNVERIFIED_VIEW_DEFINITION")
        # TEXTSIZE is 16384 bytes. Fetch bounded binary chunks, then decode once:
        # this also avoids splitting surrogate pairs on pre-2012 collations.
        chunks = []
        for offset in range(0, size, 8000):
            rows = self.session.rows(
                "SELECT SUBSTRING(CONVERT(varbinary(max),definition),%(start)s,8000) AS fragment "
                "FROM sys.sql_modules WHERE object_id=%(id)s",
                {"id": oid, "start": offset + 1},
                limit=1,
            )
            fragment = rows[0]["fragment"] if rows else None
            if not isinstance(fragment, bytes) or len(fragment) != min(
                8000, size - offset
            ):
                self.reject("UNVERIFIED_VIEW_DEFINITION")
            chunks.append(fragment)
        try:
            return b"".join(chunks).decode("utf-16-le")
        except UnicodeError:
            self.reject("UNVERIFIED_VIEW_DEFINITION")

    def view_sources(self, definition):
        try:
            tokens = TSQL().tokenize(definition)
            if len(tokens) > MAX_VIEW_NODES * 2:
                self.reject("DEPENDENCY_LIMIT")
            # sys.sql_modules retains CREATE, ALTER or CREATE OR ALTER headers.
            # Parse only the SELECT body; no definition is ever executed.
            prefix = [t.token_type for t in tokens[:4]]
            if not (
                prefix[:2]
                in (
                    [TokenType.CREATE, TokenType.VIEW],
                    [TokenType.ALTER, TokenType.VIEW],
                )
                or prefix
                == [TokenType.CREATE, TokenType.OR, TokenType.ALTER, TokenType.VIEW]
            ):
                self.reject("UNVERIFIED_VIEW_DEFINITION")
            start = next(
                i for i, t in enumerate(tokens) if t.token_type == TokenType.ALIAS
            )
            body = tokens[start + 1 :]
            if body and body[-1].token_type == TokenType.SEMICOLON:
                body = body[:-1]
            if [t.text.upper() for t in body[-3:]] == ["WITH", "CHECK", "OPTION"]:
                body = body[:-3]
            statements = ViewParser(error_level=ErrorLevel.RAISE, dialect="tsql").parse(
                body, definition
            )
            if len(statements) != 1 or not isinstance(
                statements[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)
            ):
                self.reject("UNVERIFIED_VIEW_DEFINITION")
            statement = statements[0]
            for index, node in enumerate(statement.walk()):
                if index >= MAX_VIEW_NODES:
                    self.reject("DEPENDENCY_LIMIT")
                if isinstance(node, exp.DataType):
                    if node.this.name not in SAFE_CAST_TYPES:
                        self.reject("UNSUPPORTED_TYPE")
                elif isinstance(node, exp.Var):
                    allowed = (
                        READ_HINTS
                        if isinstance(node.parent, exp.WithTableHint)
                        else DATE_PARTS
                    )
                    if node.name.upper() not in allowed:
                        self.reject("UNVERIFIED_VIEW_DEFINITION")
                elif type(node).__name__ in SAFE_SYNTAX:
                    pass
                elif isinstance(node, exp.Func):
                    function = (
                        node.name.upper()
                        if isinstance(node, exp.Anonymous)
                        else node.sql_name()
                    )
                    if function.upper() not in SAFE_FUNCTIONS:
                        self.reject("UNSAFE_FUNCTION")
                else:
                    self.reject("UNVERIFIED_VIEW_DEFINITION")
                if isinstance(node, exp.Table) and (
                    node.catalog or not isinstance(node.this, exp.Identifier)
                ):
                    self.reject("UNVERIFIED_FOREIGN_READER")
            return {
                (source.db, source.name)
                for scope in traverse_scope(statement)
                for source in scope.sources.values()
                if isinstance(source, exp.Table)
            }
        except QueryRejected:
            raise
        except Exception:
            # Parser errors may contain source and literals; expose neither.
            self.reject("UNVERIFIED_VIEW_DEFINITION")

    def view(self, oid, depth):
        sources = self.view_sources(self.definition(oid))
        access = self.session.rows(
            "SELECT HAS_PERMS_BY_NAME('sys.sql_expression_dependencies','OBJECT','SELECT') AS allowed",
            limit=1,
        )
        if not access or access[0]["allowed"] != 1:
            self.reject("DEPENDENCY_VISIBILITY_REQUIRED")
        dependencies = self.session.rows(
            "SELECT DISTINCT TOP (257) referenced_class, referenced_id, referenced_server_name, "
            "referenced_database_name, is_caller_dependent, is_ambiguous "
            "FROM sys.sql_expression_dependencies WHERE referencing_id=%(id)s",
            {"id": oid},
            limit=257,
        )
        if len(dependencies) > MAX_OBJECTS:
            self.reject("DEPENDENCY_LIMIT")
        for dependency in dependencies:
            if (
                dependency["referenced_server_name"]
                or dependency["referenced_database_name"]
            ):
                self.reject("UNVERIFIED_FOREIGN_READER")
            if (
                dependency["referenced_class"] != 1
                or dependency["referenced_id"] is None
                or dependency["is_caller_dependent"]
                or dependency["is_ambiguous"]
            ):
                self.reject("UNVERIFIED_VIEW_DEPENDENCY")
        expected = {d["referenced_id"] for d in dependencies}
        resolved = set()
        for schema, name in sorted(sources):
            # Compare using the database's identifier collation, including
            # one-part names bound when the view was created. Do not guess dbo.
            rows = self.session.rows(
                "SELECT DISTINCT TOP (2) d.referenced_id FROM sys.sql_expression_dependencies d "
                "JOIN sys.objects o ON o.object_id=d.referenced_id "
                "JOIN sys.schemas s ON s.schema_id=o.schema_id "
                "WHERE d.referencing_id=%(id)s AND d.referenced_entity_name=%(name)s "
                "AND (%(schema)s='' OR s.name=%(schema)s)",
                {"id": oid, "schema": schema, "name": name},
                limit=2,
            )
            if len(rows) != 1:
                self.reject("UNVERIFIED_VIEW_DEPENDENCY")
            resolved.add(rows[0]["referenced_id"])
        if resolved != expected:
            self.reject("UNVERIFIED_VIEW_DEPENDENCY")
        height = 0
        for dependency_id in sorted(expected):
            rows = self.session.rows(
                "SELECT object_id, type, is_ms_shipped FROM sys.objects WHERE object_id=%(id)s",
                {"id": dependency_id},
                limit=1,
            )
            if len(rows) != 1:
                self.reject("UNVERIFIED_VIEW_DEPENDENCY")
            self.object(rows[0], depth + 1)
            height = max(height, 1 + self.heights[dependency_id])
        return height
