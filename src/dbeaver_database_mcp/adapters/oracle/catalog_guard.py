"""Fail-closed audit of Oracle objects that can execute code during SELECT."""

from sqlglot import ErrorLevel, exp, parse
from sqlglot.optimizer.scope import traverse_scope

from ...core.catalog import Table, relation_path, requested_relations
from ...core.errors import QueryRejected, error_stage
from .query import AGGREGATES, FUNCTIONS, WINDOWS

SCALAR_TYPES = {
    "NUMBER",
    "FLOAT",
    "BINARY_FLOAT",
    "BINARY_DOUBLE",
    "DATE",
    "CHAR",
    "NCHAR",
    "VARCHAR2",
    "NVARCHAR2",
    "CLOB",
    "NCLOB",
    "RAW",
    "BLOB",
}
SAFE_SYNTAX = {
    "Select",
    "From",
    "Table",
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
    "Paren",
    "Order",
    "Ordered",
    "Group",
    "Having",
    "Join",
    "Distinct",
    "Limit",
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
    "DPipe",
    "Case",
    "If",
    "Cast",
    "DataType",
    "DataTypeParam",
    "Window",
    "WindowSpec",
    "Like",
    "ILike",
    "Tuple",
}
SAFE_FUNCTIONS = (
    set(FUNCTIONS.values())
    | set(AGGREGATES.values())
    | set(WINDOWS.values())
    | {
        "CURRENT_DATE",
        "CURRENT_TIMESTAMP",
        "COALESCE",
        "NVL",
        "DECODE",
        "TRIM",
        "SUBSTRING",
    }
)


class CatalogGuard:
    def __init__(self, session):
        self.session = session
        self.catalog = {}
        self.visited = set()

    def reject(self, code="UNSAFE_DEPENDENCY"):
        raise QueryRejected(
            "Oracle 对象依赖无法通过只读安全检查。",
            code=code,
            stage="safety",
            hint="检查视图、同义词、虚拟列、VPD 策略、函数索引和远程依赖；无法核验的对象不执行。",
        )

    def view_definition(self, owner, name):
        rows = self.session.rows(
            "SELECT text_length FROM SYS.ALL_VIEWS WHERE owner=:owner AND view_name=:name",
            {"owner": owner, "name": name},
            limit=1,
        )
        if not rows or rows[0]["text_length"] > 64000:
            self.reject("DEPENDENCY_LIMIT")
        rows = self.session.rows(
            "SELECT text FROM SYS.ALL_VIEWS WHERE owner=:owner AND view_name=:name",
            {"owner": owner, "name": name},
            limit=1,
        )
        try:
            statements = parse(
                rows[0]["text"], read="oracle", error_level=ErrorLevel.RAISE
            )
            if len(statements) != 1 or not isinstance(
                statements[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)
            ):
                self.reject()
            statement = statements[0]
            for index, node in enumerate(statement.walk()):
                if index >= 4000:
                    self.reject("DEPENDENCY_LIMIT")
                if type(node).__name__ in SAFE_SYNTAX:
                    if (
                        isinstance(node, exp.DataType)
                        and node.this == exp.DataType.Type.USERDEFINED
                    ):
                        self.reject("UNSUPPORTED_TYPE")
                elif isinstance(node, exp.Func):
                    function = (
                        node.name.upper()
                        if isinstance(node, exp.Anonymous)
                        else node.sql_name().upper()
                    )
                    if function not in SAFE_FUNCTIONS:
                        self.reject("UNSAFE_FUNCTION")
                else:
                    self.reject()
                if isinstance(node, exp.Column) and node.name.upper() in {
                    "NEXTVAL",
                    "CURRVAL",
                }:
                    self.reject("UNSAFE_FUNCTION")
            for scope in traverse_scope(statement):
                for source in scope.sources.values():
                    if isinstance(source, exp.Table):
                        if (
                            source.catalog
                            or not isinstance(source.this, exp.Identifier)
                            or "@" in source.name
                        ):
                            self.reject("UNVERIFIED_FOREIGN_READER")
                        schema = source.db or owner
                        if source.db and not source.args["db"].args.get("quoted"):
                            schema = schema.upper()
                        table = (
                            source.name
                            if source.this.args.get("quoted")
                            else source.name.upper()
                        )
                        self.relation(schema, table, public_synonym=not source.db)
        except QueryRejected:
            raise
        except Exception:
            self.reject()

    def relation(self, owner, name, *, public_synonym=False):
        key = (owner, name)
        if key in self.visited:
            return
        if len(self.visited) >= 256:
            self.reject("DEPENDENCY_LIMIT")
        self.visited.add(key)
        binds = {"owner": owner, "name": name}
        rows = self.session.rows(
            "SELECT object_id, object_type FROM SYS.ALL_OBJECTS WHERE owner=:owner AND object_name=:name "
            "AND object_type IN ('TABLE','VIEW','MATERIALIZED VIEW','SYNONYM') ORDER BY object_type",
            binds,
            limit=3,
        )
        if not rows or rows[0]["object_type"] == "SYNONYM":
            synonyms = self.session.rows(
                "SELECT table_owner,table_name,db_link FROM SYS.ALL_SYNONYMS "
                "WHERE synonym_name=:name AND (owner=:owner OR (owner='PUBLIC' AND :public=1)) "
                "ORDER BY CASE WHEN owner=:owner THEN 0 ELSE 1 END",
                binds | {"public": int(public_synonym)},
                limit=2,
            )
            if not synonyms:
                self.reject("TABLE_NOT_FOUND")
            target = synonyms[0]
            if target["db_link"]:
                self.reject("UNVERIFIED_FOREIGN_READER")
            target_key = (target["table_owner"], target["table_name"])
            if target_key in self.visited and target_key not in self.catalog:
                self.reject()
            self.relation(*target_key)
            self.catalog[key] = self.catalog[target_key]
            return
        kinds = {row["object_type"] for row in rows}
        if not kinds <= {"TABLE", "VIEW", "MATERIALIZED VIEW"}:
            self.reject("UNSUPPORTED_RELATION")
        columns = self.session.rows(
            "SELECT column_name,data_type,data_type_owner,virtual_column,hidden_column "
            "FROM SYS.ALL_TAB_COLS WHERE owner=:owner AND table_name=:name ORDER BY internal_column_id",
            binds,
            limit=1001,
        )
        if not columns or len(columns) > 1000:
            self.reject("DEPENDENCY_LIMIT")
        for column in columns:
            dtype = column["data_type"]
            if column["data_type_owner"] or (
                dtype not in SCALAR_TYPES and not dtype.startswith("TIMESTAMP")
            ):
                self.reject("UNSUPPORTED_TYPE")
            if column["virtual_column"] == "YES":
                self.reject("UNSAFE_VIRTUAL_COLUMN")
        visible = {
            column["column_name"]: column["data_type"]
            for column in columns
            if column["hidden_column"] != "YES"
        }
        self.catalog[key] = Table(rows[0]["object_id"], visible, frozenset(visible))
        if owner == "SYS" and name == "DUAL":
            return
        if self.session.rows(
            'SELECT 1 AS "found" FROM SYS.ALL_EXTERNAL_TABLES WHERE owner=:owner AND table_name=:name',
            binds,
            limit=1,
        ):
            self.reject("UNVERIFIED_FOREIGN_READER")
        if self.session.rows(
            "SELECT 1 AS \"found\" FROM SYS.ALL_POLICIES WHERE object_owner=:owner AND object_name=:name AND enable='YES' AND sel='YES'",
            binds,
            limit=1,
        ):
            self.reject("UNSAFE_POLICY")
        indexes = self.session.rows(
            "SELECT index_type FROM SYS.ALL_INDEXES WHERE table_owner=:owner AND table_name=:name",
            binds,
            limit=513,
        )
        if len(indexes) > 512:
            self.reject("DEPENDENCY_LIMIT")
        if any(
            index["index_type"] not in {"NORMAL", "NORMAL/REV", "BITMAP", "IOT - TOP"}
            for index in indexes
        ):
            self.reject("UNSAFE_INDEX")
        if "VIEW" in kinds:
            dependencies = self.session.rows(
                "SELECT referenced_owner,referenced_name,referenced_type,referenced_link_name "
                "FROM SYS.ALL_DEPENDENCIES WHERE owner=:owner AND name=:name AND type='VIEW'",
                binds,
                limit=257,
            )
            if len(dependencies) > 256:
                self.reject("DEPENDENCY_LIMIT")
            for dependency in dependencies:
                if dependency["referenced_link_name"]:
                    self.reject("UNVERIFIED_FOREIGN_READER")
                if dependency["referenced_type"] not in {
                    "TABLE",
                    "VIEW",
                    "MATERIALIZED VIEW",
                    "SYNONYM",
                }:
                    self.reject("UNSAFE_FUNCTION")
                self.relation(
                    dependency["referenced_owner"], dependency["referenced_name"]
                )
            self.view_definition(owner, name)

    def prepare(self, query, schema):
        with error_stage("safety"):
            for owner, name, _ in requested_relations(query, schema):
                try:
                    self.relation(owner, name)
                except QueryRejected as exc:
                    if exc.code == "TABLE_NOT_FOUND":
                        exc.path = relation_path(query, schema, owner, name)
                    raise
        return self.catalog
