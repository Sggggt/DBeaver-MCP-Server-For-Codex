"""Audit executable dependencies independently of the account's read ACLs."""

from __future__ import annotations

import re
from uuid import uuid4

from pglast import ast, parse_sql
from pglast.visitors import Visitor
from psycopg import sql

from ...core.errors import QueryRejected, error_stage
from .query import Table, relation_path, requested_relations

SAFE_VOLATILE = {
    "clock_timestamp",
    "timeofday",
    "random",
    "gen_random_uuid",
    "uuidv4",
    "pg_backend_pid",
}
FORBIDDEN_FUNCTIONS = {
    "set_config",
    "nextval",
    "setval",
    "query_to_xml",
    "query_to_xmlschema",
    "query_to_xml_and_xmlschema",
}


class CatalogGuard:
    def __init__(self, connection):
        self.connection = connection
        self.relations, self.functions, self.types = set(), set(), set()
        self.catalog = {}
        self.native_functions = None
        self.token = uuid4().hex

    def rows(self, statement, parameters=()):
        with self.connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            return cursor.fetchall()

    def reject(self, message, code="UNSAFE_DEPENDENCY", details=None):
        raise QueryRejected(
            message,
            code=code,
            stage="safety",
            hint="这是执行能力的安全限制，不是账号读取权限不足；请审查相关对象的依赖定义。",
            details=details,
        )

    def function(self, oid):
        oid = int(oid or 0)
        if not oid or oid in self.functions:
            return
        if len(self.functions) > 512:
            self.reject("函数依赖数量超过安全校验上限。", "DEPENDENCY_LIMIT")
        self.functions.add(oid)
        if oid < 16384:
            if self.native_functions is None:
                self.native_functions = {
                    r["oid"]
                    for r in self.rows(
                        "SELECT p.oid FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace "
                        "WHERE p.oid<16384 AND n.nspname='pg_catalog' AND NOT p.prosecdef "
                        "AND (p.provolatile IN ('i','s') OR p.proname=ANY(%s)) AND NOT p.proname=ANY(%s)",
                        (sorted(SAFE_VOLATILE), sorted(FORBIDDEN_FUNCTIONS)),
                    )
                }
            if oid in self.native_functions:
                return
        rows = self.rows(
            "SELECT p.oid, n.nspname, p.proname, p.provolatile, p.prosecdef, l.lanname, "
            "p.prosrc, p.proconfig, p.proargtypes::pg_catalog.oid[] AS argtypes, p.prorettype, "
            "pg_catalog.to_jsonb(p)->>'prosqlbody' AS sql_body "
            "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace "
            "JOIN pg_catalog.pg_language l ON l.oid=p.prolang WHERE p.oid=%s",
            (oid,),
        )
        if not rows:
            self.reject("函数定义在校验期间发生变化。")
        row = rows[0]
        if (
            row["lanname"] == "sql"
            and row["provolatile"] in {"i", "s"}
            and not row["prosecdef"]
            and not row["proconfig"]
        ):
            for type_oid in [*row["argtypes"], row["prorettype"]]:
                self.type(type_oid)
            if row["sql_body"]:
                self.node_tree(row["sql_body"])
            else:
                self.sql_function_body(row["prosrc"])
            return
        self.reject(
            "对象依赖未经封装的易变函数、SECURITY DEFINER 函数或不支持的函数语言。",
            "UNSAFE_FUNCTION",
            {
                "function_oid": oid,
                "schema": row["nspname"],
                "function": row["proname"],
                "language": row["lanname"],
                "security_definer": row["prosecdef"],
                "volatility": row["provolatile"],
            },
        )

    def operator(self, oid):
        for row in self.rows(
            "SELECT oprcode::pg_catalog.oid AS function FROM pg_catalog.pg_operator WHERE oid=%s",
            (oid,),
        ):
            self.function(row["function"])

    def type(self, oid):
        oid = int(oid or 0)
        if not oid or oid in self.types:
            return
        if len(self.types) > 512:
            self.reject("类型依赖数量超过校验上限。", "DEPENDENCY_LIMIT")
        self.types.add(oid)
        rows = self.rows(
            "SELECT t.oid,t.typtype,t.typelem,t.typbasetype,t.typrelid,t.typinput::pg_catalog.oid AS input, "
            "t.typoutput::pg_catalog.oid AS output,t.typreceive::pg_catalog.oid AS receive,t.typsend::pg_catalog.oid AS send, "
            "n.nspname FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace WHERE t.oid=%s",
            (oid,),
        )
        if not rows:
            self.reject("无法解析输出或表达式的数据类型。", "UNSUPPORTED_TYPE")
        row = rows[0]
        for key in ("input", "output", "receive", "send"):
            self.function(row[key])
        for key in ("typelem", "typbasetype"):
            self.type(row[key])
        if row["typrelid"]:
            for col in self.rows(
                "SELECT atttypid FROM pg_catalog.pg_attribute WHERE attrelid=%s AND attnum>0 AND NOT attisdropped",
                (row["typrelid"],),
            ):
                self.type(col["atttypid"])
        for rng in (
            self.rows(
                "SELECT rngsubtype,rngcanonical::pg_catalog.oid AS canonical,rngsubdiff::pg_catalog.oid AS diff "
                "FROM pg_catalog.pg_range WHERE rngtypid=%s OR (pg_catalog.to_jsonb(pg_range)->>'rngmultitypid')::pg_catalog.oid=%s",
                (oid, oid),
            )
            if row["typtype"] in {"r", "m"}
            else []
        ):
            self.type(rng["rngsubtype"])
            self.function(rng["canonical"])
            self.function(rng["diff"])
        if row["typtype"] == "d":
            for check in self.rows(
                "SELECT conbin::pg_catalog.text AS expression FROM pg_catalog.pg_constraint WHERE contypid=%s",
                (oid,),
            ):
                self.node_tree(check["expression"])
        if oid >= 16384:
            for cast in self.rows(
                "SELECT castfunc::pg_catalog.oid AS function FROM pg_catalog.pg_cast "
                "WHERE (castsource=%s OR casttarget=%s) AND castcontext='i'",
                (oid, oid),
            ):
                self.function(cast["function"])

    def node_tree(self, text):
        if not text:
            return
        if len(text) > 2_000_000:
            self.reject("对象定义超过安全校验大小上限。", "DEPENDENCY_LIMIT")
        if re.search(
            r":commandType\s+(?!1\b)\d+|\{NEXTVALUEEXPR\b|\{ROWMARKCLAUSE\b", text
        ):
            self.reject("对象定义包含写入、序列修改或锁行操作。")
        # Unlike pg_depend, native analyzed trees retain OIDs for pinned builtins.
        for oid in set(
            re.findall(
                r":(?:funcid|opfuncid|aggfnoid|winfnoid|hashfuncid|negfuncid)\s+(\d+)",
                text,
            )
        ):
            self.function(oid)
        for oid in set(re.findall(r":(?:opno|eqop|sortop)\s+(\d+)", text)):
            self.operator(int(oid))
        for group in re.findall(r":opnos\s+\(([^)]*)\)", text):
            for oid in re.findall(r"\b\d+\b", group):
                self.operator(int(oid))
        for oid in set(
            re.findall(
                r":(?:vartype|consttype|paramtype|resulttype|casetype|array_typeid|element_typeid|typeId|row_typeid)\s+(\d+)",
                text,
            )
        ):
            self.type(oid)
        for oid in set(re.findall(r":relid\s+(\d+)", text)):
            self.relation(int(oid), inherited=True)

    def sql_function_body(self, body):
        try:
            statements = parse_sql(body)
        except Exception:
            self.reject("SQL 函数体不能按只读语法解析。", "UNSAFE_FUNCTION")
        if (
            not statements
            or len(statements) > 8
            or any(not isinstance(s.stmt, ast.SelectStmt) for s in statements)
        ):
            self.reject("SQL 函数体只允许 SELECT。", "UNSAFE_FUNCTION")
        guard = self

        class Audit(Visitor):
            def visit_SelectStmt(self, ancestors, node):
                if node.intoClause or node.lockingClause:
                    guard.reject("SQL 函数体包含 INTO 或锁行。", "UNSAFE_FUNCTION")

            def visit_CommonTableExpr(self, ancestors, node):
                if not isinstance(node.ctequery, ast.SelectStmt):
                    guard.reject("SQL 函数体包含修改型 CTE。", "UNSAFE_FUNCTION")

            def visit_FuncCall(self, ancestors, node):
                names = [n.sval for n in node.funcname]
                if len(names) > 2:
                    guard.reject(
                        "不支持 SQL 函数中的跨数据库函数引用。", "UNSAFE_FUNCTION"
                    )
                namespace, name = names if len(names) == 2 else ("pg_catalog", names[0])
                rows = guard.rows(
                    "SELECT p.oid FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE n.nspname=%s AND p.proname=%s",
                    (namespace, name),
                )
                if not rows:
                    guard.reject(
                        "SQL 函数体包含无法静态解析的函数。", "UNSAFE_FUNCTION"
                    )
                for row in rows:
                    guard.function(row["oid"])

            def visit_RangeVar(self, ancestors, node):
                namespace = node.schemaname or "pg_catalog"
                rows = guard.rows(
                    "SELECT c.oid FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname=%s AND c.relname=%s",
                    (namespace, node.relname),
                )
                if not rows:
                    guard.reject(
                        "SQL 函数体中的关系不能安全解析；建议改用结构化子查询。",
                        "UNSAFE_FUNCTION",
                    )
                for row in rows:
                    guard.relation(row["oid"], inherited=node.inh)

            def visit_A_Expr(self, ancestors, node):
                if (
                    node.name
                    and len(node.name) > 1
                    and node.name[0].sval != "pg_catalog"
                ):
                    guard.reject(
                        "SQL 函数体引用了未封装的操作符 schema。", "UNSAFE_FUNCTION"
                    )

            def visit_TypeName(self, ancestors, node):
                if (
                    node.names
                    and len(node.names) > 1
                    and node.names[0].sval != "pg_catalog"
                ):
                    guard.reject(
                        "SQL 函数体引用了未封装的自定义 cast。", "UNSAFE_FUNCTION"
                    )

        Audit()(statements)

    def relation(self, oid, *, inherited):
        if not oid:
            return
        key = (oid, inherited)
        if key in self.relations:
            return
        if len(self.relations) > 256:
            self.reject("视图、分区或策略依赖超过 256 个对象。", "DEPENDENCY_LIMIT")
        self.relations.add(key)
        rows = self.rows(
            "SELECT c.oid,c.relkind,c.relrowsecurity,c.relam,n.nspname,c.relname,am.amname "
            "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "LEFT JOIN pg_catalog.pg_am am ON am.oid=c.relam WHERE c.oid=%s",
            (oid,),
        )
        if not rows:
            self.reject("对象定义在安全校验期间发生变化。")
        row = rows[0]
        if row["relkind"] not in {"r", "p", "v", "m", "f"}:
            self.reject("此类对象没有结构化表读取语义。", "UNSUPPORTED_RELATION")
        if row["relkind"] in {"r", "m"} and row["amname"] != "heap":
            self.reject(
                "自定义表访问方法尚未完成只读封装。", "UNSUPPORTED_ACCESS_METHOD"
            )
        if row["relkind"] == "f":
            wrappers = self.rows(
                "SELECT w.fdwname FROM pg_catalog.pg_foreign_table t JOIN pg_catalog.pg_foreign_server s ON s.oid=t.ftserver "
                "JOIN pg_catalog.pg_foreign_data_wrapper w ON w.oid=s.srvfdw WHERE t.ftrelid=%s",
                (oid,),
            )
            if not wrappers or wrappers[0]["fdwname"] != "file_fdw":
                self.reject(
                    "该外部表访问器无法在本连接验证远端副作用；本地 READ ONLY 不能证明远端只读。",
                    "UNVERIFIED_FOREIGN_READER",
                )
        columns = self.rows(
            "SELECT a.attname,a.atttypid,pg_catalog.has_column_privilege(a.attrelid,a.attnum,'SELECT') AS selectable "
            "FROM pg_catalog.pg_attribute a WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum",
            (oid,),
        )
        self.catalog[(row["nspname"], row["relname"])] = Table(
            oid,
            {c["attname"]: c["atttypid"] for c in columns},
            frozenset(c["attname"] for c in columns if c["selectable"]),
        )
        for column in columns:
            self.type(column["atttypid"])
        if row["relkind"] == "v":
            for view in self.rows(
                "SELECT ev_action::pg_catalog.text AS expression FROM pg_catalog.pg_rewrite WHERE ev_class=%s AND rulename='_RETURN'",
                (oid,),
            ):
                self.node_tree(view["expression"])
        if row["relrowsecurity"]:
            for policy in self.rows(
                "SELECT polqual::pg_catalog.text AS expression FROM pg_catalog.pg_policy WHERE polrelid=%s AND polcmd IN ('*','r')",
                (oid,),
            ):
                self.node_tree(policy["expression"])
        for index in self.rows(
            "SELECT i.indexprs::pg_catalog.text AS expression,i.indpred::pg_catalog.text AS predicate,am.amname,i.indclass::pg_catalog.oid[] AS classes "
            "FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class c ON c.oid=i.indexrelid JOIN pg_catalog.pg_am am ON am.oid=c.relam WHERE i.indrelid=%s",
            (oid,),
        ):
            if index["amname"] not in {
                "btree",
                "hash",
                "gist",
                "gin",
                "brin",
                "spgist",
            }:
                self.reject(
                    "自定义索引访问方法尚未完成只读封装。", "UNSUPPORTED_ACCESS_METHOD"
                )
            self.node_tree(index["expression"])
            self.node_tree(index["predicate"])
            for opclass in self.rows(
                "SELECT n.nspname FROM pg_catalog.pg_opclass c JOIN pg_catalog.pg_namespace n ON n.oid=c.opcnamespace WHERE c.oid=ANY(%s)",
                (index["classes"],),
            ):
                if opclass["nspname"] != "pg_catalog":
                    self.reject(
                        "自定义索引操作符类尚未完成只读封装。",
                        "UNSUPPORTED_OPERATOR_CLASS",
                    )
        if row["relkind"] == "p":
            for partition in self.rows(
                "SELECT partexprs::pg_catalog.text AS expression FROM pg_catalog.pg_partitioned_table WHERE partrelid=%s",
                (oid,),
            ):
                self.node_tree(partition["expression"])
        if inherited:
            for child in self.rows(
                "SELECT inhrelid FROM pg_catalog.pg_inherits WHERE inhparent=%s", (oid,)
            ):
                self.relation(child["inhrelid"], inherited=True)

    def prepare(self, query, schema):
        with error_stage("catalog"):
            for index, (namespace, name, inherited) in enumerate(
                requested_relations(query, schema)
            ):
                rows = self.rows(
                    "SELECT c.oid FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s",
                    (namespace, name),
                )
                if not rows:
                    raise QueryRejected(
                        "指定表或视图不存在。",
                        code="TABLE_NOT_FOUND",
                        path=relation_path(query, schema, namespace, name),
                        hint="用 search_catalog 核验 schema 与对象名称。",
                    )
                with self.connection.cursor() as cursor:
                    # PREPARE performs parse/rewrite and holds the relation's normal
                    # AccessShare locks without planning/executing functions. Unlike
                    # LOCK TABLE, this also works for materialized views. Never EXECUTE
                    # these internal statements; the connection closes after the call.
                    cursor.execute(
                        sql.SQL("PREPARE {} AS SELECT 1 FROM {}{}.{}").format(
                            sql.Identifier(f"__dbeaver_{self.token}_{index}"),
                            sql.SQL("") if inherited else sql.SQL("ONLY "),
                            sql.Identifier(namespace),
                            sql.Identifier(name),
                        )
                    )
                with error_stage("safety"):
                    self.relation(rows[0]["oid"], inherited=inherited)
        return self.catalog
