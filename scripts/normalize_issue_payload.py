#!/usr/bin/env python3
"""Normalize issue tracker payloads into the issue-triage-and-fix JSON shape."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


STANDARD_FIELDS = {
    "id": ["id", "work_item_id", "issue_id", "key"],
    "number": ["number", "auto_number", "issue_key", "key"],
    "title": ["title", "name", "summary"],
    "status": ["status", "work_item_status", "state"],
    "priority": ["priority", "severity"],
    "assignee": ["assignee", "current_status_operator", "owner"],
    "reporter": ["reporter", "owner", "created_by", "creator"],
    "description": ["description", "body", "content"],
    "reproduction_steps": [
        "reproduction_steps",
        "steps_to_reproduce",
        "repro_steps",
        "reproduce_steps",
        "复现步骤",
    ],
    "actual_result": ["actual_result", "actual_behavior", "actual", "实际结果", "实际表现"],
    "expected_result": [
        "expected_result",
        "expected_behavior",
        "expected",
        "预期结果",
        "期望结果",
    ],
    "acceptance_criteria": ["acceptance_criteria", "acceptance", "验收标准", "验收口径"],
    "environment": ["environment", "runtime_environment", "test_environment", "测试环境"],
    "test_data": ["test_data", "sample_data", "account_and_data", "测试数据", "测试账号"],
    "implementation_suggestion": [
        "implementation_suggestion",
        "suggested_fix",
        "fix_suggestion",
        "修改建议",
        "实现建议",
    ],
    "requirements": ["requirements", "_field_linked_story", "requirement", "demand", "story", "related_requirement", "需求"],
    "attachments": ["attachments", "files", "field_696151"],
    "comments": ["comments", "comment_list", "work_item_comments"],
    "activities": ["activities", "activity_list", "op_records", "operation_records"],
    "evidence_fetch": ["evidence_fetch", "evidence_review"],
    "report_quality": ["report_quality", "specification_quality", "issue_quality"],
    "created_at": ["created_at", "created", "created_time", "start_time", "field_eea32c"],
    "updated_at": ["updated_at", "updated", "modified_at"],
    "source_url": ["source_url", "url", "link", "web_url"],
}


REDACTED = "[REDACTED]"
SENSITIVE_KEYS_EXACT = {
    "accesskey",
    "apikey",
    "auth",
    "authkey",
    "sign",
    "sig",
    "xmeegofilesign",
}
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
SENSITIVE_HEADER_PATTERN = re.compile(
    r"(?im)(?<![A-Za-z0-9_])(authorization|cookie|mcp(?:\s+|[_-])?url)\s*[:：]\s*[^\r\n]+"
)
SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(authorization|cookie|password|passwd|pwd|access[_-]?token|refresh[_-]?token|"
    r"api[_-]?key|token|secret|session(?:[_-]?(?:id|token))?|mcp(?:\s+|[_-])?url)"
    r"\s*([:=：＝])\s*(?:(?:bearer|basic)\s+)?(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;，；]+)"
)
SENSITIVE_CJK_ASSIGNMENT_PATTERN = re.compile(
    r"(密码|令牌|密钥|会话)\s*([:=：＝])\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;，；]+)"
)
EVIDENCE_SOURCE_STATES = {"complete", "partial", "not-applicable", "skipped", "error", "unknown"}
REPORT_QUALITY_STATES = {"sufficient", "needs-clarification", "conflicting", "unknown"}
SENSITIVE_KEY_PARTS = {
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "mcpurl",
    "password",
    "passwd",
    "privatekey",
    "refreshtoken",
    "secret",
    "session",
    "signature",
    "token",
}


def normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def is_sensitive_key(value: Any) -> bool:
    key = normalized_key(value)
    return key in SENSITIVE_KEYS_EXACT or any(part in key for part in SENSITIVE_KEY_PARTS)


def redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if parsed.scheme not in ("http", "https") or not parsed.netloc or not parsed.query:
        return value
    query = [
        (key, REDACTED if is_sensitive_key(key) else item_value)
        for key, item_value in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def redact_text(value: str) -> str:
    redacted_urls: list[str] = []

    def stash_url(match: re.Match[str]) -> str:
        redacted_urls.append(redact_url(match.group(0)))
        return f"__BUGFLOW_REDACTED_URL_{len(redacted_urls) - 1}__"

    redacted = URL_PATTERN.sub(stash_url, value)
    redacted = SENSITIVE_HEADER_PATTERN.sub(lambda match: f"{match.group(1)}: {REDACTED}", redacted)
    redacted = re.sub(r"(?i)\bbearer\s+[^\s,;]+", f"Bearer {REDACTED}", redacted)
    redacted = SENSITIVE_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", redacted
    )
    redacted = SENSITIVE_CJK_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", redacted
    )
    for index, url in enumerate(redacted_urls):
        redacted = redacted.replace(f"__BUGFLOW_REDACTED_URL_{index}__", url)
    return redacted


def redact_sensitive_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): REDACTED if is_sensitive_key(key) else redact_sensitive_data(item_value)
            for key, item_value in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


def load_json(path: str | None) -> Any:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    return json.load(sys.stdin)


def get_path(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def first_present(data: dict[str, Any], candidates: list[str]) -> Any:
    for candidate in candidates:
        value = get_path(data, candidate)
        if value not in (None, ""):
            return value
    return None


def normalize_user(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": value.get("user_key") or value.get("id"),
        "name": value.get("name") or value.get("name_cn") or value.get("name_en") or value.get("email"),
    }


def extract_moql_value(field: dict[str, Any]) -> Any:
    value = field.get("value")
    if not isinstance(value, dict):
        return value
    for key in (
        "string_value",
        "long_value",
        "bool_value",
        "double_value",
        "date_value",
        "datetime_value",
        "timestamp_value",
        "text_value",
        "rich_text_value",
        "url_value",
    ):
        if key in value:
            return value[key]
    if "user_value" in value and isinstance(value["user_value"], dict):
        return normalize_user(value["user_value"])
    if "user_value_list" in value and isinstance(value["user_value_list"], list):
        return [normalize_user(item) if isinstance(item, dict) else item for item in value["user_value_list"]]
    if "option_value" in value and isinstance(value["option_value"], dict):
        option = value["option_value"]
        return {"id": option.get("key") or option.get("id"), "label": option.get("label") or option.get("name")}
    if "option_value_list" in value and isinstance(value["option_value_list"], list):
        return [
            {"id": item.get("key") or item.get("id"), "label": item.get("label") or item.get("name")}
            if isinstance(item, dict)
            else item
            for item in value["option_value_list"]
        ]
    if "work_item_value" in value and isinstance(value["work_item_value"], dict):
        return value["work_item_value"]
    if "work_item_value_list" in value and isinstance(value["work_item_value_list"], list):
        return value["work_item_value_list"]
    if "attachment_value_list" in value and isinstance(value["attachment_value_list"], list):
        return value["attachment_value_list"]
    if "file_value_list" in value and isinstance(value["file_value_list"], list):
        return value["file_value_list"]
    if len(value) == 1:
        return next(iter(value.values()))
    return value


def flatten_moql_record(issue: dict[str, Any]) -> dict[str, Any]:
    fields = issue.get("moql_field_list")
    if not isinstance(fields, list):
        return issue
    flattened: dict[str, Any] = {key: value for key, value in issue.items() if key != "moql_field_list"}
    for field in fields:
        if isinstance(field, dict) and field.get("key"):
            flattened[str(field["key"])] = extract_moql_value(field)
    return flattened


def normalize_assignee(value: Any) -> Any:
    if isinstance(value, list):
        names = []
        for item in value:
            if isinstance(item, dict):
                names.append(item.get("name") or item.get("user_key") or item.get("id") or item)
            else:
                names.append(item)
        return names
    if isinstance(value, dict):
        return value.get("name") or value.get("user_key") or value.get("id") or value
    return value


def normalize_requirement_item(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return {
            "id": item.get("id") or item.get("work_item_id") or item.get("auto_number") or item.get("key") or item.get("value"),
            "title": item.get("title") or item.get("name") or item.get("label") or item.get("text"),
            "url": item.get("url") or item.get("link") or item.get("web_url"),
            "number": item.get("auto_number") or item.get("number"),
            "raw": item.get("raw", item),
        }
    return {"id": None, "title": str(item), "url": None, "raw": item}


def normalize_requirements(value: Any) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [normalize_requirement_item(item) for item in value]
    return [normalize_requirement_item(value)]


def extract_plain_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := extract_plain_text(item)))
    if isinstance(value, dict):
        preferred = []
        for key in ("content_text", "plain_text", "text", "content", "body", "title"):
            if key in value and value[key] not in (None, ""):
                text = extract_plain_text(value[key])
                if text:
                    preferred.append(text)
        return "\n".join(dict.fromkeys(preferred))
    return str(value)


def list_payload_items(value: Any, keys: tuple[str, ...]) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in keys:
            if isinstance(value.get(key), list):
                return value[key]
    return [value]


def normalize_attachments(value: Any) -> list[Any]:
    return list_payload_items(
        value,
        ("items", "attachments", "files", "data", "results", "file_list", "attachment_list"),
    )


def normalize_comment_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {
            "id": None,
            "author": None,
            "created_at": None,
            "updated_at": None,
            "content_text": extract_plain_text(item),
            "attachments": [],
        }
    return {
        "id": item.get("id") or item.get("comment_id") or item.get("key"),
        "author": normalize_assignee(item.get("author") or item.get("creator") or item.get("user")),
        "created_at": item.get("created_at") or item.get("create_time") or item.get("created_time"),
        "updated_at": item.get("updated_at") or item.get("update_time") or item.get("updated_time"),
        "content_text": extract_plain_text(
            item.get("content_text") or item.get("plain_text") or item.get("content") or item.get("body") or item.get("text")
        ),
        "attachments": normalize_attachments(item.get("attachments") or item.get("files")),
    }


def normalize_comments(value: Any) -> list[dict[str, Any]]:
    items = list_payload_items(value, ("items", "comments", "comment_list", "data"))
    comments = [normalize_comment_item(item) for item in items]
    return sorted(comments, key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")))


def normalize_activity_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"id": None, "occurred_at": None, "operator": None, "summary": extract_plain_text(item)}
    return {
        "id": item.get("id") or item.get("record_id") or item.get("key"),
        "occurred_at": item.get("occurred_at") or item.get("created_at") or item.get("operate_time") or item.get("time"),
        "operator": normalize_assignee(item.get("operator") or item.get("user") or item.get("creator")),
        "module": item.get("module") or item.get("module_type"),
        "operation_type": item.get("operation_type") or item.get("type") or item.get("action"),
        "field_key": item.get("field_key") or item.get("field"),
        "summary": extract_plain_text(item.get("summary") or item.get("description") or item.get("content") or item.get("text")),
    }


def normalize_activities(value: Any) -> list[dict[str, Any]]:
    items = list_payload_items(value, ("items", "activities", "records", "op_records", "data"))
    activities = [normalize_activity_item(item) for item in items]
    return sorted(activities, key=lambda item: (str(item.get("occurred_at") or ""), str(item.get("id") or "")))


def normalize_evidence_fetch(value: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "unknown",
        "detail": "unknown",
        "comments": "unknown",
        "activities": "unknown",
        "media": "unknown",
        "fetched_at": None,
        "findings": [],
        "missing": ["Evidence intake has not been completed."],
    }
    if isinstance(value, dict):
        result.update(value)
    elif value not in (None, ""):
        result["status"] = str(value)
    for key in ("status", "detail", "comments", "activities", "media"):
        state = str(result.get(key) or "unknown").strip().lower()
        result[key] = state if state in EVIDENCE_SOURCE_STATES else "unknown"
    for key in ("findings", "missing"):
        current = result.get(key)
        if current in (None, ""):
            result[key] = []
        elif not isinstance(current, list):
            result[key] = [str(current)]
    return result


def normalize_string_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    values = value if isinstance(value, list) else [value]
    return [text for item in values if (text := extract_plain_text(item).strip())]


def normalize_report_quality_item(item: Any, *, conflict: bool = False) -> dict[str, Any]:
    if not isinstance(item, dict):
        text = extract_plain_text(item).strip()
        if conflict:
            return {"topic": "unspecified", "sources": [], "reason": text, "question": "", "target": ""}
        return {"field": "unspecified", "reason": text, "question": "", "target": ""}

    if conflict:
        return {
            "topic": str(item.get("topic") or item.get("field") or "unspecified"),
            "sources": normalize_string_list(item.get("sources") or item.get("source_refs")),
            "reason": extract_plain_text(item.get("reason") or item.get("detail")).strip(),
            "question": extract_plain_text(item.get("question")).strip(),
            "target": extract_plain_text(item.get("target") or item.get("owner")).strip(),
        }
    return {
        "field": str(item.get("field") or item.get("code") or "unspecified"),
        "reason": extract_plain_text(item.get("reason") or item.get("detail")).strip(),
        "question": extract_plain_text(item.get("question")).strip(),
        "target": extract_plain_text(item.get("target") or item.get("owner")).strip(),
    }


def normalize_report_quality(value: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "unknown",
        "assessed_at": None,
        "input_hash": "",
        "facts": [],
        "evidence_refs": [],
        "missing_fields": [],
        "conflicts": [],
        "questions": [],
        "feedback_targets": [],
        "feedback_draft": "",
    }
    if isinstance(value, dict):
        result.update(value)
    elif value not in (None, ""):
        result["status"] = str(value)

    status_aliases = {
        "complete": "sufficient",
        "ready": "sufficient",
        "incomplete": "needs-clarification",
        "clarification-required": "needs-clarification",
        "needs-confirmation": "needs-clarification",
        "needs_clarification": "needs-clarification",
        "needs_confirmation": "needs-clarification",
        "conflict": "conflicting",
    }
    status = str(result.get("status") or "unknown").strip().lower()
    status = status_aliases.get(status, status)
    result["status"] = status if status in REPORT_QUALITY_STATES else "unknown"

    for key in ("facts", "evidence_refs", "questions", "feedback_targets"):
        result[key] = normalize_string_list(result.get(key))

    missing = result.get("missing_fields") or result.get("blocking_gaps") or []
    if not isinstance(missing, list):
        missing = [missing]
    result["missing_fields"] = [normalize_report_quality_item(item) for item in missing]

    conflicts = result.get("conflicts") or []
    if not isinstance(conflicts, list):
        conflicts = [conflicts]
    result["conflicts"] = [normalize_report_quality_item(item, conflict=True) for item in conflicts]
    result["feedback_draft"] = extract_plain_text(result.get("feedback_draft")).strip()
    result["input_hash"] = str(
        result.get("input_hash") or result.get("assessment_input_hash") or ""
    ).strip()
    return result


def normalize_issue(
    issue: dict[str, Any],
    platform: str,
    mapping: dict[str, str],
    retain_raw: bool = False,
    include_raw: bool = True,
) -> dict[str, Any]:
    raw_issue = issue
    issue = flatten_moql_record(issue)
    normalized: dict[str, Any] = {"source": platform}

    for field, candidates in STANDARD_FIELDS.items():
        if field in mapping:
            value = get_path(issue, mapping[field])
            if value in (None, ""):
                value = first_present(issue, candidates)
        else:
            value = first_present(issue, candidates)
        normalized[field] = value

    normalized["assignee"] = normalize_assignee(normalized.get("assignee"))
    normalized["reporter"] = normalize_assignee(normalized.get("reporter"))
    normalized["requirements"] = normalize_requirements(normalized.get("requirements"))
    normalized["attachments"] = normalize_attachments(normalized.get("attachments"))
    normalized["comments"] = normalize_comments(normalized.get("comments"))
    normalized["activities"] = normalize_activities(normalized.get("activities"))
    normalized["evidence_fetch"] = normalize_evidence_fetch(normalized.get("evidence_fetch"))
    normalized["report_quality"] = normalize_report_quality(normalized.get("report_quality"))
    for field in (
        "reproduction_steps",
        "actual_result",
        "expected_result",
        "acceptance_criteria",
        "environment",
        "test_data",
        "implementation_suggestion",
    ):
        normalized[field] = extract_plain_text(normalized.get(field)).strip()
    raw_payload = raw_issue.get("raw", raw_issue) if raw_issue.get("source") == platform else raw_issue
    if retain_raw and include_raw:
        normalized["raw"] = raw_payload
    elif include_raw:
        normalized["raw"] = redact_sensitive_data(raw_payload)
    if not retain_raw:
        normalized["requirements"] = redact_sensitive_data(normalized["requirements"])
        normalized["attachments"] = redact_sensitive_data(normalized["attachments"])
        normalized["comments"] = redact_sensitive_data(normalized["comments"])
        normalized["activities"] = redact_sensitive_data(normalized["activities"])
        normalized["evidence_fetch"] = redact_sensitive_data(normalized["evidence_fetch"])
        normalized["report_quality"] = redact_sensitive_data(normalized["report_quality"])
        for field in (
            "description",
            "source_url",
            "reproduction_steps",
            "actual_result",
            "expected_result",
            "acceptance_criteria",
            "environment",
            "test_data",
            "implementation_suggestion",
        ):
            if normalized.get(field) not in (None, ""):
                normalized[field] = redact_sensitive_data(normalized[field])
    return normalized


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="JSON file to read. Reads stdin when omitted.")
    parser.add_argument("--output", help="JSON file to write. Writes stdout when omitted.")
    parser.add_argument("--platform", default="unknown", help="Issue source platform name.")
    parser.add_argument(
        "--mapping",
        help="Optional JSON object or JSON file mapping standard fields to source paths.",
    )
    parser.add_argument(
        "--retain-raw",
        action="store_true",
        help="Keep the full raw payload. By default sensitive keys and signed URL parameters are redacted.",
    )
    args = parser.parse_args()

    payload = load_json(args.input)

    mapping: dict[str, str] = {}
    if args.mapping:
        mapping_text = Path(args.mapping).read_text(encoding="utf-8") if Path(args.mapping).exists() else args.mapping
        mapping = json.loads(mapping_text)

    if isinstance(payload, dict):
        if isinstance(payload.get("issues"), list):
            issues = payload["issues"]
        elif isinstance(payload.get("data"), list):
            issues = payload["data"]
        elif isinstance(payload.get("items"), list):
            issues = payload["items"]
        else:
            issues = [payload]
    elif isinstance(payload, list):
        issues = payload
    else:
        raise SystemExit("Input must be a JSON object or array.")

    normalized = [normalize_issue(issue, args.platform, mapping, retain_raw=args.retain_raw) for issue in issues]
    output = json.dumps(normalized, ensure_ascii=False, indent=2)

    if args.output:
        Path(args.output).write_text(output + "\n", encoding="utf-8")
    else:
        print(output)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
