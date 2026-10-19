"""Fail-closed dependency checks for local SQL Server tables and views."""

from sqlglot import ErrorLevel, exp
from sqlglot.dialects.tsql import TSQL
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.tokens import TokenType

from ...core.catalog import Table, requested_relations
from ...core.errors import QueryRejected
from .catalog_scope import CatalogScope
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


class LegacyTSQL(TSQL):
    class Tokenizer(TSQL.Tokenizer):
        # With QUOTED_IDENTIFIER OFF, double quotes delimit strings; brackets
        # still delimit identifiers. Tokenization preserves comments/escapes.
        IDENTIFIERS = [("[", "]")]


class CatalogGuard:
    def __init__(self, session, *, database="", server="", root=None):
        self.session = session
        self.database, self.server = (
            database or getattr(session, "database", ""),
            server,
        )
        self.root = root or self
        if root is None:
            self.verified, self.heights, self.visiting, self.scopes = {}, {}, set(), {}
        else:
            self.verified, self.heights, self.visiting = (
                root.verified,
                root.heights,
                root.visiting,
            )
        self.root.scopes[server, self.database] = self

    def key(self, oid):
        return self.server, self.database, oid

    def scope(self, database, server=""):
        key = server, database
        if key not in self.root.scopes:
            if len(self.root.scopes) >= 32:
                self.reject("DEPENDENCY_LIMIT")
            session = CatalogScope(self.root.session, database, server)
            guard = CatalogGuard(
                session, database=database, server=server, root=self.root
            )
            guard.visibility()
        return self.root.scopes[key]

    def visibility(self):
        rows = self.session.rows(
            "SELECT HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','VIEW DEFINITION') AS allowed",
            limit=1,
        )
        if not rows or rows[0]["allowed"] != 1:
            self.reject("DEPENDENCY_VISIBILITY_REQUIRED")

    def relation(self, schema, name):
        identifier(schema)
        identifier(name)
        rows = self.session.rows(
            "SELECT o.object_id, o.type, o.is_ms_shipped FROM sys.objects o "
            "JOIN sys.schemas s ON s.schema_id=o.schema_id WHERE s.name=%(schema)s AND o.name=%(table)s",
            {"schema": schema, "table": name},
            limit=2,
        )
        if len(rows) != 1:
            self.reject("TABLE_NOT_FOUND")
        return rows[0]

    def reject(self, code="UNSAFE_DEPENDENCY"):
        raise QueryRejected(
            "SQL Server 对象无法通过只读依赖检查。",
            code=code,
            stage="safety",
            hint="目标及依赖须有可核验目录权限；拒绝不可见定义、同义词、自定义函数、计算列、CLR、外部表及行安全策略。",
        )

    def columns(self, object_id):
        return self.session.rows(
            "SELECT TOP (1025) c.column_id, c.name, t.name AS data_type, c.max_length, "
            "c.precision, c.scale, c.is_nullable, c.is_computed, t.is_assembly_type, t.is_user_defined, "
            "bt.name AS base_type, t.is_table_type, SCHEMA_NAME(t.schema_id) AS type_schema, "
            "HAS_PERMS_BY_NAME(QUOTENAME(OBJECT_SCHEMA_NAME(c.object_id))+'.'+QUOTENAME(OBJECT_NAME(c.object_id)), "
            "'OBJECT','SELECT',c.name,'COLUMN') AS can_select "
            "FROM sys.columns c JOIN sys.types t ON t.user_type_id=c.user_type_id "
            "LEFT JOIN sys.types bt ON bt.user_type_id=t.system_type_id AND bt.system_type_id=bt.user_type_id "
            "WHERE c.object_id=%(id)s ORDER BY c.column_id",
            {"id": object_id},
            limit=1025,
        )

    def prepare(self, query, schema):
        # The shared collector handles all nested query forms. It deliberately
        # does not treat literal JSON content as syntax.
        requested_relations(query, schema, max_identifier_bytes=512)
        relations = set()

        def walk(node, namespace):
            if isinstance(node, dict):
                namespace = node.get("schema", namespace)
                if "table" in node:
                    if "server" in node and "database" not in node:
                        self.reject("MISSING_PARAMETER")
                    relations.add(
                        tuple(node[k] for k in ("server", "database") if k in node)
                        + (namespace, node["table"])
                    )
                for key, value in node.items():
                    if key not in {"value", "values"}:
                        walk(value, namespace)
            elif isinstance(node, list):
                for item in node:
                    walk(item, namespace)

        walk(query, schema)
        if len(relations) > 64:
            self.reject("DEPENDENCY_LIMIT")
        catalog = {}
        if relations:
            # Object visibility alone cannot prove that no hidden policy exists.
            self.visibility()
        for relation in sorted(relations):
            server, database, namespace, name = (
                relation
                if len(relation) == 4
                else ("", relation[0], *relation[1:])
                if len(relation) == 3
                else (self.server, self.database, *relation)
            )
            guard = self.scope(database, server) if len(relation) > 2 else self
            catalog[relation] = guard.object(guard.relation(namespace, name))
        return catalog

    def object(self, row, depth=0):
        oid, kind = row["object_id"], row["type"].strip()
        key = self.key(oid)
        if key in self.visiting:
            self.reject("UNVERIFIED_VIEW_DEPENDENCY")
        if key in self.verified:
            if depth + self.heights[key] >= MAX_DEPTH:
                self.reject("DEPENDENCY_LIMIT")
            return self.verified[key]
        if depth >= MAX_DEPTH or len(self.verified) + len(self.visiting) >= MAX_OBJECTS:
            self.reject("DEPENDENCY_LIMIT")
        if kind not in {"U", "V"} or row["is_ms_shipped"]:
            self.reject("UNVERIFIED_VIEW_OR_SYNONYM")
        self.visiting.add(key)
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
            or c.get("is_table_type", False)
            or (c.get("base_type") if c["is_user_defined"] else c["data_type"])
            not in SAFE_TYPES
            for c in columns
        ):
            self.reject("UNSUPPORTED_TYPE")
        height = self.view(oid, depth) if kind == "V" else 0
        table = Table(
            oid,
            {c["name"]: c.get("base_type") or c["data_type"] for c in columns},
            frozenset(c["name"] for c in columns if c["can_select"] == 1),
        )
        self.visiting.remove(key)
        self.verified[key] = table
        self.heights[key] = height
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
        if rows[0]["uses_quoted_identifier"] not in (0, 1):
            self.reject("UNVERIFIED_VIEW_DEFINITION")
        self.quoted_identifier = bool(rows[0]["uses_quoted_identifier"])
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

    def view_sources(self, definition, quoted_identifier=True):
        try:
            dialect = TSQL() if quoted_identifier else LegacyTSQL()
            tokens = dialect.tokenize(definition)
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
            statements = ViewParser(
                error_level=ErrorLevel.RAISE, dialect=dialect
            ).parse(body, definition)
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
                elif isinstance(node, exp.Dot):
                    # Four-part relation names are represented with Dot. A
                    # qualified function/member expression must still fail.
                    parent = node.parent
                    while isinstance(parent, exp.Dot):
                        parent = parent.parent
                    if not isinstance(parent, exp.Table):
                        self.reject("UNSAFE_FUNCTION")
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
                    not 1 <= len(node.parts) <= 4
                    or any(not isinstance(part, exp.Identifier) for part in node.parts)
                ):
                    self.reject("UNVERIFIED_FOREIGN_READER")
            return {
                tuple(part.name for part in source.parts)
                if len(source.parts) > 1
                else ("", source.name)
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
        definition = self.definition(oid)
        sources = self.view_sources(definition, self.quoted_identifier)
        access = self.session.rows(
            "SELECT HAS_PERMS_BY_NAME('sys.sql_expression_dependencies','OBJECT','SELECT') AS allowed",
            limit=1,
        )
        if not access or access[0]["allowed"] != 1:
            self.reject("DEPENDENCY_VISIBILITY_REQUIRED")
        dependencies = self.session.rows(
            "SELECT DISTINCT TOP (257) referenced_class, referenced_id, referenced_server_name, "
            "referenced_database_name, is_caller_dependent, is_ambiguous "
            ", referenced_schema_name, referenced_entity_name "
            "FROM sys.sql_expression_dependencies WHERE referencing_id=%(id)s",
            {"id": oid},
            limit=257,
        )
        if len(dependencies) > MAX_OBJECTS:
            self.reject("DEPENDENCY_LIMIT")
        if any(len(source) > 2 for source in sources) or any(
            d["referenced_database_name"] or d["referenced_server_name"]
            for d in dependencies
        ):
            return self.foreign_view(oid, depth, sources, dependencies)
        for dependency in dependencies:
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
            height = max(height, 1 + self.heights[self.key(dependency_id)])
        return height

    def foreign_view(self, oid, depth, sources, dependencies):
        resolved, height = set(), 0

        def identity(d):
            return tuple(
                d.get(k)
                for k in (
                    "referenced_class",
                    "referenced_id",
                    "referenced_server_name",
                    "referenced_database_name",
                    "referenced_schema_name",
                    "referenced_entity_name",
                )
            )

        for dependency in dependencies:
            if (
                dependency["referenced_class"] != 1
                or dependency["is_caller_dependent"]
                or dependency["is_ambiguous"]
            ):
                self.reject("UNVERIFIED_VIEW_DEPENDENCY")
        for source in sorted(sources):
            server, database, schema, name = (
                ("", "", *source)
                if len(source) == 2
                else ("", *source)
                if len(source) == 3
                else source
            )
            # Names on a second linked server belong to the remote server's
            # configuration, not the initiating server. Never guess a route.
            if self.server and server:
                self.reject("MULTIHOP_LINKED_SERVER")
            rows = self.session.rows(
                "SELECT DISTINCT TOP (2) d.referenced_class, d.referenced_id, d.referenced_server_name, "
                "d.referenced_database_name, d.referenced_schema_name, d.referenced_entity_name "
                "FROM sys.sql_expression_dependencies d LEFT JOIN sys.objects o ON o.object_id=d.referenced_id "
                "LEFT JOIN sys.schemas s ON s.schema_id=o.schema_id WHERE d.referencing_id=%(id)s "
                "AND d.referenced_entity_name=%(name)s AND COALESCE(d.referenced_server_name,'')=%(server)s "
                "AND COALESCE(d.referenced_database_name,'')=%(database)s "
                "AND (%(schema)s='' OR COALESCE(d.referenced_schema_name,s.name)=%(schema)s)",
                {
                    "id": oid,
                    "name": name,
                    "schema": schema,
                    "database": database,
                    "server": server,
                },
                limit=2,
            )
            if len(rows) != 1 or identity(rows[0]) not in {
                identity(d) for d in dependencies
            }:
                self.reject("UNVERIFIED_VIEW_DEPENDENCY")
            dependency = rows[0]
            resolved.add(identity(dependency))
            target = self.scope(database or self.database, server or self.server)
            target_schema = dependency.get("referenced_schema_name") or schema
            if not target_schema:
                self.reject("UNVERIFIED_VIEW_DEPENDENCY")
            row = target.relation(target_schema, dependency["referenced_entity_name"])
            if (
                not database
                and not server
                and dependency["referenced_id"] != row["object_id"]
            ):
                self.reject("UNVERIFIED_VIEW_DEPENDENCY")
            target.object(row, depth + 1)
            height = max(height, 1 + self.heights[target.key(row["object_id"])])
        if resolved != {identity(d) for d in dependencies}:
            self.reject("UNVERIFIED_VIEW_DEPENDENCY")
        return height
