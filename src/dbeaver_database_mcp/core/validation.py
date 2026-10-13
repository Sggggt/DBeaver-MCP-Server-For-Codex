"""Compile a closed relational JSON language into parameterized SELECTs."""

from __future__ import annotations

import math
from typing import Any

from jsonschema import Draft202012Validator

from .errors import QueryRejected, error_stage
from .query_schema import (
    DEFS,
    QUERY_SCHEMA,
)


def path_string(parts) -> str:
    # Paths contain schema keys, never data values. Unknown keys are redacted.
    keys = {
        "query",
        "connection",
        "database",
        "database_type",
        "schema",
        "table",
        "levels",
        "since",
        "max_bytes",
        "timeout_ms",
        "routine",
        "routine_id",
        "package",
        "routine_types",
        "name_pattern",
        "include_definition",
        "definition_part",
        "definition_offset",
        "max_definition_chars",
    }

    def collect(node):
        if isinstance(node, dict):
            keys.update(node.get("properties", {}))
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(DEFS)
    result = ""
    for part in parts:
        if isinstance(part, int):
            result += f"[{part}]"
        else:
            result += ("." if result else "") + (part if part in keys else "[unknown]")
    return result or "$"


def bounded_json(value: Any, depth: int = 0, budget=None, path=()) -> None:
    budget = [20000] if budget is None else budget
    budget[0] -= 1
    if depth > 64 or budget[0] < 0:
        raise QueryRejected(
            "结构化请求超过复杂度上限。",
            code="INPUT_TOO_COMPLEX",
            path=path_string(path),
        )
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise QueryRejected(
                "字符串包含无效 Unicode。",
                code="INVALID_STRING",
                path=path_string(path),
            ) from None
        if len(value) > 16000 or "\x00" in value:
            raise QueryRejected(
                "字符串超过 16000 字符或包含 NUL。",
                code="INVALID_STRING",
                path=path_string(path),
            )
    elif type(value) is float:
        if not math.isfinite(value):
            raise QueryRejected(
                "数值必须是有限数。", code="INVALID_NUMBER", path=path_string(path)
            )
    elif type(value) is int:
        if abs(value) > 10**100:
            raise QueryRejected(
                "数值超出支持范围。", code="INVALID_NUMBER", path=path_string(path)
            )
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise QueryRejected(
                    "对象键必须是字符串。",
                    code="INVALID_PARAMETER",
                    path=path_string(path),
                )
            bounded_json(item, depth + 1, budget, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            bounded_json(item, depth + 1, budget, (*path, index))
    elif value is not None and type(value) is not bool:
        raise QueryRejected(
            "参数必须是 JSON 值。", code="INVALID_PARAMETER", path=path_string(path)
        )


def _best_leaf(error):
    if not error.context:
        return error
    # Prefer the branch whose required discriminators are present in the input.
    candidates = list(error.context)
    if isinstance(error.instance, dict):
        present = set(error.instance)
        candidates.sort(
            key=lambda e: (
                len(set(e.schema.get("properties", {})) & present),
                len(e.absolute_path),
                e.validator != "required",
            ),
            reverse=True,
        )
    else:
        candidates.sort(key=lambda e: len(e.absolute_path), reverse=True)
    return _best_leaf(candidates[0])


def validate_schema(value: Any, schema: dict, prefix=()) -> None:
    bounded_json(value, path=prefix)
    issues = []
    for error in Draft202012Validator(schema).iter_errors(value):
        error = _best_leaf(error)
        path = (*prefix, *error.absolute_path)
        expected = None
        code = "INVALID_PARAMETER"
        if error.validator == "required":
            missing = [
                key for key in error.validator_value if key not in error.instance
            ]
            reported = (
                error.message.split("'")[1] if error.message.startswith("'") else None
            )
            if reported in missing:
                missing = [reported]
            path = (*path, missing[0]) if missing else path
            message, code = "缺少必填参数。", "MISSING_PARAMETER"
        elif error.validator == "enum":
            message, code, expected = (
                "参数不在允许列表中。",
                "INVALID_ENUM",
                error.validator_value,
            )
        elif error.validator == "additionalProperties":
            unknown = sorted(
                set(error.instance) - set(error.schema.get("properties", {}))
            )
            path = (*path, unknown[0]) if unknown else path
            message, code = (
                "包含工具不支持的字段；不接受 SQL 或表达式文本。",
                "UNKNOWN_PARAMETER",
            )
        elif error.validator == "type":
            message, expected = "参数类型不正确。", error.validator_value
        else:
            message, expected = (
                "参数不满足长度、数量或范围限制。",
                {error.validator: error.validator_value},
            )
        issues.append(
            {
                "path": path_string(path),
                "code": code,
                "message": message,
                "expected": expected,
            }
        )
        if len(issues) == 8:
            break
    if issues:
        first = issues[0]
        raise QueryRejected(
            first["message"],
            code=first["code"],
            path=first["path"],
            issues=issues,
            hint="根据 issues 中的参数位置与 expected 修正输入；不要加入自由 SQL。",
        )


def validate_query(query: dict) -> None:
    with error_stage("validate"):
        validate_schema(query, QUERY_SCHEMA, ("query",))
        if "limit" not in query:
            raise QueryRejected(
                "顶层查询必须指定 limit。", code="MISSING_PARAMETER", path="query.limit"
            )
        if type(query["limit"]) is not int or type(query.get("offset", 0)) is not int:
            raise QueryRejected(
                "limit/offset 必须是整数。",
                code="INVALID_PARAMETER",
                path="query.limit",
            )
