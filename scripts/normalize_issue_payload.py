#!/usr/bin/env python3
"""Normalize issue tracker payloads into the issue-triage-and-fix JSON shape."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


STANDARD_FIELDS = {
    "id": ["id", "work_item_id", "issue_id", "key"],
    "number": ["number", "auto_number", "issue_key", "key"],
    "title": ["title", "name", "summary"],
    "status": ["status", "work_item_status", "state"],
    "priority": ["priority", "severity"],
    "assignee": ["assignee", "current_status_operator", "owner"],
    "reporter": ["reporter", "owner", "created_by", "creator"],
    "description": ["description", "body", "content"],
    "requirements": ["requirements", "_field_linked_story", "requirement", "demand", "story", "related_requirement", "需求"],
    "attachments": ["attachments", "files", "field_696151"],
    "created_at": ["created_at", "created", "created_time", "start_time", "field_eea32c"],
    "updated_at": ["updated_at", "updated", "modified_at"],
    "source_url": ["source_url", "url", "link", "web_url"],
}


def load_json(path: str | None) -> Any:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
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
            "raw": item,
        }
    return {"id": None, "title": str(item), "url": None, "raw": item}


def normalize_requirements(value: Any) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [normalize_requirement_item(item) for item in value]
    return [normalize_requirement_item(value)]


def normalize_issue(issue: dict[str, Any], platform: str, mapping: dict[str, str]) -> dict[str, Any]:
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
    normalized["attachments"] = normalized.get("attachments") or []
    normalized["raw"] = raw_issue.get("raw", raw_issue) if raw_issue.get("source") == platform else raw_issue
    return normalized


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="JSON file to read. Reads stdin when omitted.")
    parser.add_argument("--output", help="JSON file to write. Writes stdout when omitted.")
    parser.add_argument("--platform", default="unknown", help="Issue source platform name.")
    parser.add_argument(
        "--mapping",
        help="Optional JSON object or JSON file mapping standard fields to source paths.",
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

    normalized = [normalize_issue(issue, args.platform, mapping) for issue in issues]
    output = json.dumps(normalized, ensure_ascii=False, indent=2)

    if args.output:
        Path(args.output).write_text(output + "\n", encoding="utf-8")
    else:
        print(output)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
