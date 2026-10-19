"""Database contexts for fixed, parameterized catalog reads only.

No caller SQL reaches this module. The only RPC target is SQL Server's fixed
sys.sp_executesql; its statement is an adapter-owned catalog SELECT template.
Business queries use the ordinary closed SELECT compiler, never this RPC path.
"""

import re
from functools import lru_cache

from sqlglot import ErrorLevel, exp
from sqlglot.dialects.tsql import TSQL

from ...core.errors import QueryRejected
from .query import identifier

PROVIDERS = {
    "MSOLEDBSQL",
    "MSOLEDBSQL19",
    "SQLNCLI",
    "SQLNCLI10",
    "SQLNCLI11",
    "SQLOLEDB",
}
MARKER = re.compile(r"%\(([a-z_]+)\)s")
CATALOG_SYNTAX = {
    "Select",
    "Alias",
    "Literal",
    "Column",
    "Identifier",
    "From",
    "Table",
    "TableAlias",
    "Distinct",
    "Add",
    "Join",
    "EQ",
    "GT",
    "And",
    "Or",
    "Where",
    "Order",
    "Ordered",
    "Limit",
    "Paren",
    "Parameter",
    "Var",
    "DataType",
    "DataTypeParam",
    "Null",
}
CATALOG_FUNCTIONS = {
    "HAS_PERMS_BY_NAME",
    "DB_NAME",
    "QUOTENAME",
    "OBJECT_SCHEMA_NAME",
    "OBJECT_NAME",
    "SCHEMA_NAME",
    "CURRENT_SCHEMA",
    "DATALENGTH",
    "SUBSTRING",
    "CONVERT",
    "COALESCE",
    "SERVERPROPERTY",
    "CAST",
}


class CatalogParser(TSQL.Parser):
    def _warn_unsupported(self):
        raise ValueError("unsupported catalog syntax")


@lru_cache(maxsize=64)
def validate_catalog_select(statement):
    """Defense in depth before the fixed RPC: one pure sys-catalog SELECT."""
    try:
        tokens = TSQL().tokenize(statement)
        trees = CatalogParser(error_level=ErrorLevel.RAISE, dialect="tsql").parse(
            tokens, statement
        )
        if len(trees) != 1 or not isinstance(trees[0], exp.Select):
            raise ValueError
        for node in trees[0].walk():
            if isinstance(node, exp.Table) and (
                node.db != "sys"
                or node.catalog
                or not isinstance(node.this, exp.Identifier)
            ):
                raise ValueError
            if type(node).__name__ in CATALOG_SYNTAX:
                continue
            if isinstance(node, exp.Func):
                name = (
                    node.name.upper()
                    if isinstance(node, exp.Anonymous)
                    else node.sql_name()
                )
                if name in CATALOG_FUNCTIONS:
                    continue
            raise ValueError
    except Exception:
        raise QueryRejected(
            "内部目录语句未通过只读核验。", code="UNSAFE_DEPENDENCY", stage="safety"
        ) from None


class CatalogScope:
    def __init__(self, session, database, server=""):
        self.session, self.database, self.server = session, database, server
        self.major_version = session.major_version
        identifier(database)
        if server:
            identifier(server)
            rows = session.rows(
                "SELECT name, provider, product, is_linked, is_data_access_enabled, is_rpc_out_enabled "
                "FROM sys.servers WHERE name=%(server)s AND server_id>0",
                {"server": server},
                limit=2,
            )
            if (
                len(rows) != 1
                or rows[0]["provider"] not in PROVIDERS
                or rows[0]["product"] not in {"", "SQL Server"}
                or any(
                    rows[0][k] != 1
                    for k in (
                        "is_linked",
                        "is_data_access_enabled",
                        "is_rpc_out_enabled",
                    )
                )
            ):
                raise QueryRejected(
                    "链接目标必须是已有的 SQL Server 链接，且允许数据读取和 RPC OUT。",
                    code="UNVERIFIED_LINKED_SERVER",
                    stage="safety",
                )
            version = self.rows(
                "SELECT CAST(SERVERPROPERTY('ProductVersion') AS varchar(32)) AS version",
                limit=1,
            )
            try:
                self.major_version = int(version[0]["version"].split(".")[0])
            except (ValueError, IndexError, KeyError, TypeError, AttributeError):
                self.major_version = 0
            if self.major_version < 10:
                raise QueryRejected(
                    "无法核验链接 SQL Server 版本。",
                    code="UNSUPPORTED_DATABASE_VERSION",
                    stage="safety",
                )

    def rows(self, statement, parameters=None, *, limit=1000):
        # Private catalog call sites use only SELECT templates. Refuse any
        # accidental future mutation before sending the fixed system RPC.
        if not statement.startswith("SELECT "):
            raise QueryRejected(
                "只允许内部目录读取。", code="UNSAFE_DEPENDENCY", stage="safety"
            )
        params = parameters or {}
        if set(MARKER.findall(statement)) != set(params):
            raise QueryRejected("内部目录参数不一致。", code="INVALID_PARAMETER")
        template = MARKER.sub(lambda m: "@" + m[1], statement)
        validate_catalog_select(template)
        declarations = []
        bound = {"catalog_statement": "SET TEXTSIZE 16384; " + template}
        for name, value in params.items():
            if not re.fullmatch(r"[a-z_]+", name) or (
                value is not None and type(value) not in (int, str)
            ):
                raise QueryRejected("内部目录参数类型无效。", code="INVALID_PARAMETER")
            declarations.append(
                "@" + name + (" bigint" if type(value) is int else " nvarchar(4000)")
            )
            bound[name] = value
        procedure = ".".join(
            identifier(n)
            for n in (self.server, self.database, "sys", "sp_executesql")
            if n
        )
        sql = "EXEC " + procedure + " @stmt=%(catalog_statement)s"
        if params:
            bound["catalog_parameters"] = ", ".join(declarations)
            sql += ", @params=%(catalog_parameters)s" + "".join(
                ", @" + n + "=%(" + n + ")s" for n in params
            )
        return self.session.rows(sql, bound, limit=limit)
