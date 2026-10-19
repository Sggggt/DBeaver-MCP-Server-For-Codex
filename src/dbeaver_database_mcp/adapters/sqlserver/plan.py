"""Bounded estimated-plan summaries; omit statement text, literals and XML."""

from xml.etree import ElementTree

from ...core.errors import QueryRejected

NS = "{http://schemas.microsoft.com/sqlserver/2004/07/showplan}"


def summarize_plan(xml, max_bytes):
    if not isinstance(xml, str) or len(xml.encode("utf-8")) > max_bytes:
        raise QueryRejected(
            "估算计划超过读取上限。", code="PLAN_LIMIT", stage="explain"
        )
    if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        raise QueryRejected(
            "估算计划格式无法核验。", code="INVALID_PLAN", stage="explain"
        )
    try:
        root = ElementTree.fromstring(xml)
        if root.tag != NS + "ShowPlanXML":
            raise ValueError
        operators = []
        for index, node in enumerate(root.iter()):
            if index >= 16000:
                raise QueryRejected(
                    "估算计划超过节点上限。", code="PLAN_LIMIT", stage="explain"
                )
            if node.tag != NS + "RelOp":
                continue
            if len(operators) == 256:
                return {
                    "operators": operators,
                    "truncated": True,
                    "estimated": True,
                    "executed": False,
                }
            item = {
                key: node.get(attr)
                for key, attr in (
                    ("node_id", "NodeId"),
                    ("physical_op", "PhysicalOp"),
                    ("logical_op", "LogicalOp"),
                    ("estimated_rows", "EstimateRows"),
                    ("estimated_subtree_cost", "EstimatedTotalSubtreeCost"),
                )
            }
            # Object identities are useful for access paths. Predicates,
            # ScalarString, StatementText and parameter lists can contain data.
            objects = []
            for child in node:
                for obj in child.findall(NS + "Object"):
                    objects.append(
                        {
                            k.lower(): obj.get(k)
                            for k in ("Server", "Database", "Schema", "Table", "Index")
                            if obj.get(k) is not None
                        }
                    )
            item["objects"] = objects[:16]
            operators.append(item)
        if not operators:
            raise ValueError
        return {
            "operators": operators,
            "truncated": False,
            "estimated": True,
            "executed": False,
        }
    except (ElementTree.ParseError, ValueError, RecursionError):
        raise QueryRejected(
            "估算计划不完整或格式无法核验。", code="INVALID_PLAN", stage="explain"
        ) from None


def explain(session, built, max_bytes):
    error = None
    with session.connection.cursor() as cursor:
        # SHOWPLAN must occupy its own batch. Never use STATISTICS XML, which
        # executes the business SELECT. ON failure must not submit the query.
        session.execute(cursor, f"SET TEXTSIZE {max_bytes}")
        try:
            session.execute(cursor, "SET SHOWPLAN_XML ON")
            session.execute(cursor, built.statement, built.parameters)
            session.check()
            row = cursor.fetchone()
            session.check()
            if row is None:
                raise QueryRejected(
                    "未返回估算计划。", code="INVALID_PLAN", stage="explain"
                )
            return summarize_plan(row[0], max_bytes)
        except BaseException as exc:
            error = exc
            raise
        finally:
            try:
                session.execute(cursor, "SET SHOWPLAN_XML OFF")
            except BaseException:
                if error is None:
                    raise
                error.cleanup_failed = True
            # open_session always rolls back and closes this unpooled connection
            # even if OFF fails or the deadline has expired.
