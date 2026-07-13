#!/usr/bin/env python3
"""Bugflow v2 runner：初始化配置、导入工单、分诊、记录受控修复闭环。

这个 runner 故意不修改代码、不修改远程工单状态。它的职责是把飞书/MCP/导出的
工单 JSON 转成可恢复的 bugflow 工件，并用项目配置做需求-仓库匹配、初步分诊、
修复计划、实现记录、验证记录和本地闭环摘要。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from bugflow_artifacts import (
    ARTIFACTS,
    artifact_template,
    dependency_fingerprint,
    effective_artifact_status,
    frontmatter_metadata,
    invalidate_downstream,
    issue_dir,
    write_if_absent,
)
from normalize_issue_payload import normalize_issue


SKILL_ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = SKILL_ROOT / "assets"
CURRENT_LOGIN_USER = "current_login_user()"
LIGHTWEIGHT_VERIFICATION_MODE = "lightweight"
STANDARD_VERIFICATION_MODE = "standard"
COMPLETION_ACTIONS = {
    "commit",
    "start-fix",
    "resolve-for-acceptance",
    "comment",
    "complete",
    "terminate",
}


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - 环境错误提示
        requirements = SKILL_ROOT / "requirements.txt"
        raise SystemExit(f"Missing PyYAML. Install dependencies with: python -m pip install -r {requirements}") from exc
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data or {}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


LOCAL_RESTRICTIVE_BOOLEAN_PATHS = (
    "remote_status_policy.update_status_allowed",
    "remote_status_policy.update_comments_allowed",
    "remote_status_policy.default_change_to_in_progress",
    "remote_status_policy.default_resolve_for_acceptance",
    "remote_status_policy.default_complete",
    "remote_status_policy.default_terminate",
    "execution_policy.auto_fix_allowed",
    "execution_policy.auto_fix_low_risk_frontend",
    "execution_policy.allow_lightweight_verification",
    "git_policy.auto_commit_after_fix",
    "git_policy.push_after_commit",
    "login_policy.allow_qr_login",
    "issue_source.status_ids_verified",
)


def set_config_value(config: dict[str, Any], path: str, value: Any) -> None:
    current = config
    parts = path.split(".")
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def restrictive_local_merge(base: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
    """Merge local preferences without allowing them to broaden automation authority."""

    merged = deep_merge(base, local)
    local_denies: list[str] = []
    for path in LOCAL_RESTRICTIVE_BOOLEAN_PATHS:
        local_value = config_value(local, path, None)
        if local_value is None:
            continue
        project_allowed = bool(config_value(base, path, False))
        set_config_value(merged, path, project_allowed and bool(local_value))
        if not bool(local_value):
            local_denies.append(path)

    if config_value(local, "login_policy.never_request_password", None) is not None:
        set_config_value(
            merged,
            "login_policy.never_request_password",
            bool(config_value(base, "login_policy.never_request_password", True))
            or bool(config_value(local, "login_policy.never_request_password", True)),
        )

    local_effort = config_value(local, "execution_policy.max_auto_fix_effort", None)
    if local_effort is not None:
        project_effort = str(config_value(base, "execution_policy.max_auto_fix_effort", "easy"))
        effective = min(
            (str(project_effort), str(local_effort)),
            key=lambda value: effort_rank(value),
        )
        set_config_value(merged, "execution_policy.max_auto_fix_effort", effective)

    local_confirmations = config_value(local, "execution_policy.require_confirmation_for", None)
    if isinstance(local_confirmations, list):
        project_confirmations = config_value(base, "execution_policy.require_confirmation_for", [])
        if not isinstance(project_confirmations, list):
            project_confirmations = []
        combined = list(dict.fromkeys([*project_confirmations, *local_confirmations]))
        set_config_value(merged, "execution_policy.require_confirmation_for", combined)

    base_transitions = base.get("status_transitions") or {}
    local_transitions = local.get("status_transitions") or {}
    for transition, local_settings in local_transitions.items():
        if not isinstance(local_settings, dict) or "require_confirmation" not in local_settings:
            continue
        project_required = bool((base_transitions.get(transition) or {}).get("require_confirmation", True))
        set_config_value(
            merged,
            f"status_transitions.{transition}.require_confirmation",
            project_required or bool(local_settings["require_confirmation"]),
        )
    merged["_bugflow_safety"] = {"local_denies": local_denies}
    return merged


def load_config(config_path: Path, local_config_path: Path | None) -> dict[str, Any]:
    config = load_yaml(config_path)
    if local_config_path and local_config_path.exists():
        config = restrictive_local_merge(config, load_yaml(local_config_path))
    return config


def config_value(config: dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = config
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return default
    return current


def load_json_payload(path: str | None) -> Any:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    return json.load(sys.stdin)


def yaml_scalar(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def write_text_file(path: Path, text: str, force: bool = False) -> str:
    existed = path.exists()
    if existed and not force:
        return "skipped"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return "updated" if existed else "created"


def read_asset(name: str) -> str:
    path = ASSETS_DIR / name
    if not path.exists():
        raise SystemExit(f"Missing skill asset: {path}")
    return path.read_text(encoding="utf-8")


def iter_payload_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("issues", "data", "items", "records"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    raise SystemExit("Input must be a JSON object or array.")


def field_mapping(config: dict[str, Any]) -> dict[str, str]:
    mapping = config.get("field_mapping") or {}
    return {str(key): str(value) for key, value in mapping.items() if value not in (None, "")}


def issue_key(issue: dict[str, Any]) -> str:
    key = issue.get("number") or issue.get("id")
    if not key:
        source = str(issue.get("source") or "unknown")
        fields = ", ".join(sorted(str(key) for key in issue if key != "raw")) or "none"
        raise SystemExit(f"Issue missing number/id (source={source}; available fields: {fields}).")
    return str(key)


def display_scalar(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, dict):
        return str(value.get("label") or value.get("name") or value.get("title") or value.get("id") or "")
    if isinstance(value, list):
        return " / ".join(display_scalar(item) for item in value if display_scalar(item))
    return str(value)


def issue_requirement_label(issue: dict[str, Any]) -> str:
    requirements = issue.get("requirements") or []
    labels: list[str] = []
    for req in requirements:
        if isinstance(req, dict):
            req_id = req.get("id") or ""
            title = req.get("title") or ""
            label = " / ".join(part for part in (str(req_id), str(title)) if part)
            if label:
                labels.append(label)
        elif req:
            labels.append(str(req))
    return "；".join(labels)


def issue_people(issue: dict[str, Any], key: str) -> str:
    value = issue.get(key)
    if isinstance(value, list):
        people: list[str] = []
        for item in value:
            if isinstance(item, dict):
                people.append(str(item.get("name") or item.get("id") or ""))
            elif item:
                people.append(str(item))
        return " / ".join(person for person in people if person)
    if isinstance(value, dict):
        return str(value.get("name") or value.get("id") or "")
    return str(value or "")


def identity_token(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def person_identity_tokens(value: Any) -> set[str]:
    if isinstance(value, list):
        return set().union(*(person_identity_tokens(item) for item in value)) if value else set()
    if isinstance(value, dict):
        tokens: set[str] = set()
        for key in ("name", "user_key", "id", "key", "email"):
            token = identity_token(value.get(key))
            if token:
                tokens.add(token)
        return tokens
    token = identity_token(value)
    return {token} if token else set()


def import_assignee_filter(
    config: dict[str, Any], args: argparse.Namespace, platform: str
) -> tuple[set[str], str]:
    if bool(getattr(args, "include_all_assignees", False)):
        return set(), "explicit-all-assignees"

    raw_cli_assignees = getattr(args, "assignee", None) or []
    if isinstance(raw_cli_assignees, str):
        raw_cli_assignees = [raw_cli_assignees]
    cli_assignees = [
        identity_token(value)
        for value in raw_cli_assignees
        if identity_token(value)
    ]
    configured_assignee = str(config_value(config, "query_policy.assigned_to", "") or "").strip()
    configured_aliases = config_value(config, "query_policy.assignee_aliases", []) or []
    if not isinstance(configured_aliases, list):
        raise SystemExit("query_policy.assignee_aliases must be a list of current-user names or ids.")

    candidates = cli_assignees or [identity_token(configured_assignee), *map(identity_token, configured_aliases)]
    candidates = [value for value in candidates if value and value != identity_token(CURRENT_LOGIN_USER)]
    if candidates:
        return set(candidates), "matched-current-assignee"

    if platform == "feishu-project" and configured_assignee == CURRENT_LOGIN_USER:
        return set(), "native-query-current-user"

    raise SystemExit(
        "Cannot safely import every assignee. Configure query_policy.assigned_to/assignee_aliases "
        "for the current user, pass --assignee, or explicitly pass --include-all-assignees."
    )


def filter_imported_issues(
    issues: list[dict[str, Any]], assignee_tokens: set[str], mode: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not assignee_tokens:
        included = list(issues)
    else:
        included = [
            issue
            for issue in issues
            if person_identity_tokens(issue.get("assignee")) & assignee_tokens
        ]
    return included, {
        "mode": mode,
        "input_count": len(issues),
        "included_count": len(included),
        "skipped_assignee_count": len(issues) - len(included),
        "assignees": sorted(assignee_tokens),
    }


def issue_date_label(issue: dict[str, Any]) -> str:
    created = issue.get("created_at")
    updated = issue.get("updated_at")
    if created and updated and str(created) != str(updated):
        return f"创建 {created}<br>更新 {updated}"
    return str(updated or created or "")


def recommendation_label(readiness: str, effort: str, risk: str) -> str:
    readiness_labels = {
        "auto-fix-candidate": "可批准后修复",
        "manual-review-first": "需人工评审",
        "ask-for-confirmation": "需进一步确认",
        "redirect-to-owner": "建议转交",
    }
    effort_labels = {
        "easy": "低难度",
        "medium": "中等难度",
        "hard": "高难度",
        "blocked": "暂不可修",
    }
    risk_labels = {
        "low": "低风险",
        "medium": "中风险",
        "high": "高风险",
    }
    return " / ".join(
        (
            readiness_labels.get(readiness, readiness),
            effort_labels.get(effort, effort),
            risk_labels.get(risk, risk),
        )
    )


def triage_display_label(value: str) -> str:
    labels = {
        "current-repo": "当前仓库",
        "multi-repo-unclear": "多仓库归属待确认",
        "other-repo": "其他仓库",
        "unmatched": "未匹配",
        "low-confidence": "低置信匹配",
        "frontend-owned": "前端负责",
        "backend-owned": "后端负责",
        "needs-confirmation": "需要确认",
        "not-current-repo": "非当前仓库",
        "auto-fix-candidate": "可批准后修复",
        "manual-review-first": "需人工评审",
        "ask-for-confirmation": "需进一步确认",
        "redirect-to-owner": "建议转交",
    }
    return labels.get(value, value)


def markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "无"
    header = "| " + " | ".join(headers) + " |"
    separator = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(cell.replace("\n", "<br>") for cell in row) + " |" for row in rows]
    return "\n".join([header, separator, *body])


def artifact_path(issue_root: Path, artifact_id: str) -> Path:
    for artifact in ARTIFACTS:
        if artifact["id"] == artifact_id:
            return issue_root / artifact["file"]
    raise KeyError(artifact_id)


def set_frontmatter_status(markdown: str, artifact_id: str, status: str) -> str:
    body = markdown
    if markdown.startswith("---"):
        parts = markdown.split("---", 2)
        if len(parts) == 3:
            body = parts[2].lstrip("\n")
    return f"---\nartifact: {artifact_id}\nstatus: {status}\n---\n\n{body}"


def write_markdown_artifact(
    path: Path,
    artifact_id: str,
    status: str,
    body: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    old_content = path.read_bytes() if path.exists() else None
    fields: dict[str, Any] = {
        "artifact": artifact_id,
        "status": status,
        "dependency_hash": dependency_fingerprint(path.parent, artifact_id),
    }
    fields.update(metadata or {})
    frontmatter = ["---"]
    for key, value in fields.items():
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (dict, list)):
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        else:
            rendered = str(value)
        frontmatter.append(f"{key}: {rendered}")
    frontmatter.extend(["---", "", body])
    path.write_text("\n".join(frontmatter), encoding="utf-8")
    if old_content is not None and old_content != path.read_bytes():
        invalidate_downstream(path.parent, artifact_id)


def artifact_frontmatter_status(path: Path) -> str:
    if not path.exists():
        return "missing"
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return "unknown"
    parts = text.split("---", 2)
    if len(parts) < 3:
        return "unknown"
    for line in parts[1].splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip()
    return "unknown"


def artifact_effective_status(issue_root: Path, artifact_id: str) -> str:
    return effective_artifact_status(issue_root, artifact_id)


def require_artifact_done(issue_root: Path, artifact_id: str, action: str) -> None:
    status = artifact_effective_status(issue_root, artifact_id)
    if status != "done":
        raise SystemExit(f"Cannot {action}: {artifact_id} is {status}; regenerate it first.")


def load_issue(issue_root: Path) -> dict[str, Any]:
    issue_path = issue_root / "issue.json"
    if not issue_path.exists():
        raise SystemExit(f"Missing issue.json: {issue_path}")
    return json.loads(issue_path.read_text(encoding="utf-8"))


def scaffold_issue_dir(root: Path, issue: dict[str, Any]) -> Path:
    target = issue_dir(root, issue_key(issue))
    target.mkdir(parents=True, exist_ok=True)
    for artifact in ARTIFACTS:
        path = target / artifact["file"]
        if artifact["file"] == "issue.json":
            continue
        write_if_absent(path, artifact_template(artifact))
    return target


def write_issue_json(root: Path, issue: dict[str, Any]) -> Path:
    target = scaffold_issue_dir(root, issue)
    issue_path = target / "issue.json"
    content = json.dumps(issue, ensure_ascii=False, indent=2) + "\n"
    old_content = issue_path.read_text(encoding="utf-8") if issue_path.exists() else None
    issue_path.write_text(content, encoding="utf-8")
    if old_content is not None and old_content != content:
        invalidate_downstream(target, "issue-intake")
    return target


def combined_text(issue: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("title", "description", "number", "id", "status", "priority"):
        if issue.get(key):
            parts.append(str(issue[key]))
    for req in issue.get("requirements") or []:
        if isinstance(req, dict):
            parts.extend(str(req.get(key) or "") for key in ("id", "title", "url"))
        else:
            parts.append(str(req))
    for comment in issue.get("comments") or []:
        if isinstance(comment, dict) and comment.get("content_text"):
            parts.append(str(comment["content_text"]))
    for activity in issue.get("activities") or []:
        if isinstance(activity, dict) and activity.get("summary"):
            parts.append(str(activity["summary"]))
    evidence = issue.get("evidence_fetch") or {}
    if isinstance(evidence, dict):
        parts.extend(str(item) for item in evidence.get("findings") or [] if str(item).strip())
    attachment_groups = [issue.get("attachments") or []]
    attachment_groups.extend(
        comment.get("attachments") or []
        for comment in issue.get("comments") or []
        if isinstance(comment, dict)
    )
    for attachments in attachment_groups:
        for attachment in attachments if isinstance(attachments, list) else []:
            if (
                isinstance(attachment, dict)
                and str(attachment.get("inspection_state") or "").lower() == "inspected"
                and attachment.get("summary")
            ):
                parts.append(str(attachment["summary"]))
    return "\n".join(parts).lower()


def issue_evidence_state(issue: dict[str, Any]) -> dict[str, Any]:
    evidence = issue.get("evidence_fetch")
    if not isinstance(evidence, dict):
        evidence = {}
    allowed_complete = {"complete", "not-applicable"}
    missing = [str(item) for item in evidence.get("missing") or [] if str(item).strip()]
    source_states: dict[str, str] = {}
    for source in ("detail", "comments", "activities", "media"):
        state = str(evidence.get(source) or "unknown").strip().lower()
        source_states[source] = state
        if state not in allowed_complete:
            missing.append(f"{source} evidence is {state}")

    aggregate = str(evidence.get("status") or "unknown").strip().lower()
    if aggregate != "complete":
        missing.append(f"evidence_fetch.status is {aggregate}")

    attachment_groups = [issue.get("attachments") or []]
    attachment_groups.extend(
        comment.get("attachments") or []
        for comment in issue.get("comments") or []
        if isinstance(comment, dict)
    )
    for attachments in attachment_groups:
        for attachment in attachments if isinstance(attachments, list) else []:
            if isinstance(attachment, dict):
                if attachment.get("decision_relevant") is False:
                    continue
                name = attachment.get("name") or attachment.get("title") or attachment.get("id") or "unnamed attachment"
                inspection_state = str(attachment.get("inspection_state") or "unknown").strip().lower()
            else:
                name = str(attachment) or "unnamed attachment"
                inspection_state = "unknown"
            if inspection_state != "inspected":
                missing.append(f"attachment {name} inspection is {inspection_state}")

    deduped_missing = list(dict.fromkeys(missing))
    findings = [str(item) for item in evidence.get("findings") or [] if str(item).strip()]
    complete = aggregate == "complete" and not deduped_missing
    effective_status = "complete" if complete else ("partial" if aggregate == "complete" else aggregate)
    return {
        "complete": complete,
        "status": effective_status,
        "sources": source_states,
        "findings": findings,
        "missing": deduped_missing,
    }


def repository_by_key(config: dict[str, Any], repo_key: str) -> dict[str, Any]:
    mapping = config.get("requirement_mapping") or {}
    current = (mapping.get("current_repo") or {})
    if current.get("repo_key") == repo_key:
        return current
    for repo in mapping.get("related_repositories") or []:
        if repo.get("repo_key") == repo_key:
            return repo
    return {"repo_key": repo_key, "aliases": []}


def match_requirement(config: dict[str, Any], issue: dict[str, Any]) -> dict[str, Any]:
    mapping = config.get("requirement_mapping") or {}
    current_repo = mapping.get("current_repo") or {}
    current_key = current_repo.get("repo_key") or config_value(config, "project.name", "current-repo")
    requirements = issue.get("requirements") or []
    text = combined_text(issue)

    matched_rules: list[dict[str, Any]] = []
    for rule in mapping.get("demand_rules") or []:
        needle = str(rule.get("match_title_contains") or "").strip().lower()
        if needle and needle in text:
            matched_rules.append(rule)

    candidate_keys: list[str] = []
    for rule in matched_rules:
        for repo_key in rule.get("repo_keys") or []:
            if repo_key not in candidate_keys:
                candidate_keys.append(repo_key)

    alias_hits: list[str] = []
    for alias in current_repo.get("aliases") or []:
        alias_text = str(alias).strip().lower()
        if alias_text and alias_text in text:
            alias_hits.append(str(alias))
            if current_key not in candidate_keys:
                candidate_keys.append(current_key)

    if not requirements:
        match_state = "unmatched"
        confidence = "low"
        confirmation_required = True
        reason = "工单没有可识别的关联需求。"
    elif current_key in candidate_keys and len(candidate_keys) == 1:
        match_state = "current-repo"
        confidence = "high" if alias_hits else "medium"
        confirmation_required = False
        reason = "关联需求和配置规则指向当前仓库。"
    elif current_key in candidate_keys and len(candidate_keys) > 1:
        if alias_hits:
            match_state = "current-repo"
            confidence = "medium"
            confirmation_required = False
            reason = "需求关联多个仓库，但工单文本命中了当前仓库别名。"
        else:
            match_state = "multi-repo-unclear"
            confidence = "medium"
            confirmation_required = True
            reason = "需求关联多个仓库，当前仓库归属不够明确。"
    elif candidate_keys:
        match_state = "other-repo"
        confidence = "medium"
        confirmation_required = False
        reason = "需求规则指向其他仓库。"
    else:
        match_state = "low-confidence"
        confidence = "low"
        confirmation_required = True
        reason = "没有命中需求-仓库映射规则。"

    candidate_repos = [
        {
            "repo_key": key,
            "aliases": repository_by_key(config, key).get("aliases") or [],
            "path": repository_by_key(config, key).get("path"),
        }
        for key in candidate_keys
    ]
    question = ""
    if confirmation_required:
        req_title = ""
        if requirements and isinstance(requirements[0], dict):
            req_title = requirements[0].get("title") or requirements[0].get("id") or ""
        question = (
            f"请确认工单 {issue.get('number') or issue.get('id')}（{issue.get('title') or ''}）"
            f"关联需求“{req_title or '未识别'}”应由哪个代码库处理："
            f"{', '.join(candidate_keys) if candidate_keys else '当前仓库/其他仓库'}？"
        )

    return {
        "repository_match": match_state,
        "confidence": confidence,
        "confirmation_required": confirmation_required,
        "reason": reason,
        "matched_requirement": requirements[0] if requirements else {},
        "candidate_repositories": candidate_repos,
        "matched_rules": matched_rules,
        "alias_hits": alias_hits,
        "customer_confirmation_question": question,
    }


def render_requirement_match(match: dict[str, Any]) -> str:
    req = match.get("matched_requirement") or {}
    repos = match.get("candidate_repositories") or []
    repo_lines = "\n".join(
        f"| {repo.get('repo_key') or ''} | {repo.get('path') or ''} | {', '.join(repo.get('aliases') or [])} |"
        for repo in repos
    )
    if not repo_lines:
        repo_lines = "|  |  |  |"
    return f"""# 需求与仓库匹配

## 关联需求

- ID: {req.get('id') or ''}
- 标题: {req.get('title') or ''}
- URL: {req.get('url') or ''}

## 候选仓库

| 仓库 | 路径 | 别名 |
| --- | --- | --- |
{repo_lines}

## 判断结果

- 仓库匹配: {match['repository_match']}
- 置信度: {match['confidence']}
- 需要确认: {str(match['confirmation_required']).lower()}
- 原因: {match['reason']}

## 客户/产品确认问题

{match.get('customer_confirmation_question') or '无'}
"""


def backend_owned_reason(config: dict[str, Any], title_desc: str) -> str:
    ordering_sources = (
        "列表接口",
        "接口返回",
        "返回顺序",
        "后端排序",
        "数据库排序",
        "order by",
        "order_by",
        "server sort",
    )
    ordering_changes = ("正序", "倒序", "升序", "降序", "排序", "reverse", " asc", " desc")
    if any(value in title_desc for value in ordering_sources) and any(
        value in title_desc for value in ordering_changes
    ):
        return (
            "列表接口的数据顺序属于后端查询/API 契约；不应通过前端 reverse 或本地重排掩盖接口问题。"
            "建议后端按明确排序字段返回，除非接口契约明确规定由客户端排序。"
        )

    configured = config_value(config, "ownership_rules.backend_owned", []) or []
    if isinstance(configured, list):
        evidence = [
            str(value).strip()
            for value in configured
            if str(value).strip() and str(value).strip().lower() in title_desc
        ]
        if evidence:
            return f"命中后端职责证据：{', '.join(evidence)}。建议转交后端，不做前端兜底。"
    return ""


def classify_issue(config: dict[str, Any], issue: dict[str, Any], match: dict[str, Any]) -> dict[str, Any]:
    title_desc = combined_text(issue)
    match_state = match["repository_match"]
    if match_state == "current-repo":
        ownership = config_value(config, "project.role_assumption", "current-repo")
        if ownership == "frontend":
            ownership = "frontend-owned"
        effort = "medium"
        risk = "medium"
        readiness = "manual-review-first"
        reason = "需求与当前仓库匹配，但没有足够的纯前端低风险证据，默认先人工评审。"
        backend_reason = backend_owned_reason(config, title_desc)
        if backend_reason:
            ownership = "backend-owned"
            effort = "blocked"
            risk = "low"
            readiness = "redirect-to-owner"
            reason = backend_reason
        low_risk_evidence = [
            word
            for word in (
                "样式",
                "布局",
                "间距",
                "对齐",
                "颜色",
                "字号",
                "溢出",
                "遮挡",
                "固定列",
                "图标",
                "css",
                "style",
                "layout",
                "overflow",
                "align",
            )
            if word in title_desc
        ]
        high_risk_evidence = [
            word
            for word in (
                "权限",
                "登录",
                "认证",
                "接口",
                "api",
                "数据库",
                "支付",
                "迁移",
                "数据丢失",
                "丢失",
                "删除",
                "保存失败",
                "安全",
                "后端",
                "schema",
                "contract",
                "定时",
                "发布日期",
                "发布时间",
                "发布日",
                "schedule",
            )
            if word in title_desc
        ]
        policies_allow = bool(config_value(config, "execution_policy.auto_fix_allowed", False)) and bool(
            config_value(config, "execution_policy.auto_fix_low_risk_frontend", False)
        )
        if ownership == "frontend-owned" and low_risk_evidence and not high_risk_evidence and policies_allow:
            ownership = "frontend-owned"
            effort = "easy"
            risk = "low"
            readiness = "auto-fix-candidate"
            reason = f"当前仓库匹配，且仅命中纯前端低风险证据：{', '.join(low_risk_evidence)}。"
        elif ownership != "backend-owned" and high_risk_evidence:
            effort = "medium"
            risk = "high" if any(word in high_risk_evidence for word in ("权限", "认证", "支付", "数据丢失", "丢失", "删除", "安全")) else "medium"
            readiness = "manual-review-first"
            reason = f"当前仓库可能相关，但描述包含较高风险信号：{', '.join(high_risk_evidence)}。"
    elif match_state in ("multi-repo-unclear", "low-confidence", "unmatched"):
        ownership = "needs-confirmation"
        effort = "blocked"
        risk = "medium"
        readiness = "ask-for-confirmation"
        reason = "需求与仓库归属不明确，需先确认责任边界。"
    else:
        ownership = "not-current-repo"
        effort = "blocked"
        risk = "low"
        readiness = "redirect-to-owner"
        reason = "需求映射结果指向其他仓库。"

    evidence_state = issue_evidence_state(issue)
    missing_information = [match["customer_confirmation_question"]] if match.get("confirmation_required") else []
    if not evidence_state["complete"]:
        preliminary = ownership
        effort = "blocked"
        readiness = "ask-for-confirmation"
        risk = "high" if risk == "high" else "medium"
        reason = f"证据获取不完整，当前仅为初步归属判断（{preliminary}）；读取缺失证据后必须重新分诊。"
        missing_information.extend(evidence_state["missing"] or ["Complete evidence intake before final triage."])

    return {
        "ownership": ownership,
        "effort": effort,
        "readiness": readiness,
        "risk": risk,
        "reason": reason,
        "recommended_order": None,
        "missing_information": list(dict.fromkeys(missing_information)),
        "evidence_status": evidence_state["status"],
        "evidence_complete": evidence_state["complete"],
        "evidence_sources": evidence_state["sources"],
        "evidence_findings": evidence_state["findings"],
    }


def render_triage(issue: dict[str, Any], match: dict[str, Any], triage: dict[str, Any]) -> str:
    missing = "\n".join(f"- {item}" for item in triage.get("missing_information") or []) or "- 无"
    findings = "\n".join(f"- {item}" for item in triage.get("evidence_findings") or []) or "- 无"
    evidence_sources = triage.get("evidence_sources") or {}
    return f"""# 分诊报告

## 工单

- 编号: {issue.get('number') or issue.get('id')}
- 标题: {issue.get('title') or ''}
- 状态: {issue.get('status') or ''}
- 优先级: {issue.get('priority') or ''}

## 需求与仓库

- 仓库匹配: {match['repository_match']}
- 置信度: {match['confidence']}
- 需要确认: {str(match['confirmation_required']).lower()}

## 分类

- 责任归属: {triage['ownership']}
- 难度: {triage['effort']}
- 执行准备度: {triage['readiness']}
- 风险: {triage['risk']}

## 证据覆盖

- 总体: {triage.get('evidence_status') or 'unknown'}
- 完整详情: {evidence_sources.get('detail') or 'unknown'}
- 历史评论: {evidence_sources.get('comments') or 'unknown'}
- 活动记录: {evidence_sources.get('activities') or 'unknown'}
- 附件内容: {evidence_sources.get('media') or 'unknown'}

### 关键证据

{findings}

## 理由

{triage['reason']}

## 缺失信息

{missing}
"""


def import_json_issues(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    config = load_config(Path(args.config), Path(args.local_config) if args.local_config else None)
    platform = config_value(config, "issue_source.platform", args.platform)
    root = Path(config_value(config, "bugflow.root", args.root))
    payload = load_json_payload(args.input)
    items = iter_payload_items(payload)
    normalized = [
        normalize_issue(
            item,
            platform,
            field_mapping(config),
            retain_raw=bool(getattr(args, "retain_raw", False)),
        )
        for item in items
    ]
    assignee_tokens, filter_mode = import_assignee_filter(config, args, str(platform))
    included, filter_summary = filter_imported_issues(normalized, assignee_tokens, filter_mode)
    for issue in included:
        write_issue_json(root, issue)
    return included, filter_summary


def fetch_json(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config), Path(args.local_config) if args.local_config else None)
    root = Path(config_value(config, "bugflow.root", args.root))
    normalized, filter_summary = import_json_issues(args)
    directories = [str(issue_dir(root, issue_key(issue))) for issue in normalized]
    result = {"count": len(normalized), "directories": directories, "assignee_filter": filter_summary}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def triage_issue_dir(config: dict[str, Any], issue_root: Path) -> dict[str, Any]:
    issue_path = issue_root / "issue.json"
    if not issue_path.exists():
        raise SystemExit(f"Missing issue.json: {issue_path}")
    issue = json.loads(issue_path.read_text(encoding="utf-8"))
    scaffold_issue_dir(issue_root.parent, issue)
    match = match_requirement(config, issue)
    match_status = "blocked" if match["confirmation_required"] else "done"
    write_markdown_artifact(
        artifact_path(issue_root, "requirement-match"),
        "requirement-match",
        match_status,
        render_requirement_match(match),
    )
    triage = classify_issue(config, issue, match)
    triage_status = "blocked" if triage["readiness"] in ("ask-for-confirmation", "redirect-to-owner") else "done"
    write_markdown_artifact(
        artifact_path(issue_root, "triage-report"),
        "triage-report",
        triage_status,
        render_triage(issue, match, triage),
        {
            "repository_match": match["repository_match"],
            "confidence": match["confidence"],
            "confirmation_required": match["confirmation_required"],
            "ownership": triage["ownership"],
            "effort": triage["effort"],
            "readiness": triage["readiness"],
            "risk": triage["risk"],
            "evidence_status": triage["evidence_status"],
            "evidence_complete": triage["evidence_complete"],
        },
    )
    return {
        "issue": issue.get("number") or issue.get("id"),
        "id": issue.get("id") or "",
        "title": issue.get("title") or "",
        "status": display_scalar(issue.get("status")),
        "priority": display_scalar(issue.get("priority")),
        "reporter": issue_people(issue, "reporter"),
        "assignee": issue_people(issue, "assignee"),
        "date": issue_date_label(issue),
        "requirement": issue_requirement_label(issue),
        "repository_match": match["repository_match"],
        "confidence": match["confidence"],
        "ownership": triage["ownership"],
        "effort": triage["effort"],
        "readiness": triage["readiness"],
        "risk": triage["risk"],
        "evidence_status": triage["evidence_status"],
        "evidence_complete": triage["evidence_complete"],
        "evidence_findings": triage["evidence_findings"],
        "confirmation_required": match["confirmation_required"],
        "question": match.get("customer_confirmation_question") or "",
    }


def triage(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config), Path(args.local_config) if args.local_config else None)
    root = Path(config_value(config, "bugflow.root", args.root))
    if args.issue:
        targets = [issue_dir(root, args.issue)]
    elif root.exists():
        targets = sorted(path for path in root.iterdir() if path.is_dir())
    else:
        targets = []
    results = [triage_issue_dir(config, target) for target in targets]
    print(json.dumps({"count": len(results), "items": results}, ensure_ascii=False, indent=2))
    return 0


def render_daily_markdown(results: list[dict[str, Any]]) -> str:
    auto = [item for item in results if item["readiness"] == "auto-fix-candidate"]
    manual = [item for item in results if item["readiness"] == "manual-review-first"]
    needs = [item for item in results if item["confirmation_required"] or item["readiness"] == "ask-for-confirmation"]
    redirects = [item for item in results if item["readiness"] == "redirect-to-owner"]

    def table(items: list[dict[str, Any]]) -> str:
        if not items:
            return "无"
        return markdown_table(
            ["缺陷", "标题", "优先级", "状态", "提出/更新", "报告人/负责人", "证据", "推荐"],
            [
                [
                    "<br>".join(str(part) for part in (item.get("id"), item.get("issue")) if part),
                    item.get("title") or "",
                    item.get("priority") or "",
                    item.get("status") or "",
                    item.get("date") or "",
                    " / ".join(part for part in (item.get("reporter"), item.get("assignee")) if part),
                    "完整" if item.get("evidence_complete") else "不完整",
                    recommendation_label(item["readiness"], item["effort"], item["risk"]),
                ]
                for item in items
            ],
        )

    questions = "\n".join(f"- {item['issue']}: {item['question']}" for item in needs if item.get("question")) or "无"
    summary_parts = [
        f"本次查询到 {len(results)} 个缺陷",
        f"安全候选 {len(auto)} 个",
        f"需人工评审 {len(manual)} 个",
        f"需确认 {len(needs)} 个",
        f"建议转交 {len(redirects)} 个",
    ]
    evidence = "；".join(
        f"{item['issue']}：证据{'完整' if item.get('evidence_complete') else '不完整'} / "
        f"{triage_display_label(item['repository_match'])} / {triage_display_label(item['ownership'])} / "
        f"{triage_display_label(item['readiness'])}"
        for item in results
    ) or "无"
    return f"""# 每日 bug 分诊报告

{ "，".join(summary_parts) }。本次未修改飞书状态、未修改代码、未提交/建分支。

## 缺陷总览

{table(results)}

## 可人工批准后修复

{table(auto)}

## 需人工评审后再决定

{table(manual)}

## 需要客户/产品确认

{table(needs)}

## 建议转交

{table(redirects)}

## 证据与判断

{evidence}

## 确认问题

{questions}
"""


def daily(args: argparse.Namespace) -> int:
    imported, filter_summary = import_json_issues(args)
    config = load_config(Path(args.config), Path(args.local_config) if args.local_config else None)
    root = Path(config_value(config, "bugflow.root", args.root))
    results = [triage_issue_dir(config, issue_dir(root, issue_key(issue))) for issue in imported]
    report = render_daily_markdown(results)
    report += (
        "\n\n## 负责人过滤\n\n"
        f"- 模式: {filter_summary['mode']}\n"
        f"- 输入: {filter_summary['input_count']}\n"
        f"- 纳入: {filter_summary['included_count']}\n"
        f"- 跳过其他负责人: {filter_summary['skipped_assignee_count']}\n"
    )
    if args.report:
        Path(args.report).write_text(report, encoding="utf-8")
    print(report)
    return 0


def effort_rank(effort: str) -> int:
    return {"easy": 1, "medium": 2, "hard": 3, "blocked": 99}.get(effort, 99)


def hard_repair_blockers(item: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    repository_match = str(item.get("repository_match") or "")
    ownership = str(item.get("ownership") or "")
    effort = str(item.get("effort") or "blocked")
    risk = str(item.get("risk") or "medium")

    if item.get("evidence_complete") is not True:
        blockers.append("Inbound detail/comments/attachment evidence is incomplete; approval cannot bypass evidence intake.")

    if repository_match != "current-repo":
        blockers.append(f"Repository match is {repository_match or 'unknown'}; only the current repo can be repaired.")
    if item.get("confirmation_required"):
        blockers.append("Requirement/repository ownership still needs confirmation; plan approval cannot bypass it.")
    if ownership in ("needs-confirmation", "not-current-repo", "backend-owned"):
        blockers.append(f"Ownership is {ownership}; confirm or redirect before repair.")
    if effort in ("hard", "blocked"):
        blockers.append(f"Effort is {effort}; this high-impact gate cannot be bypassed by plan approval.")
    if risk == "high":
        blockers.append("Risk is high; narrow and re-triage the plan before repair.")
    return blockers


def repair_gate(config: dict[str, Any], item: dict[str, Any], approved: bool) -> tuple[str, list[str]]:
    blockers = hard_repair_blockers(item)
    readiness = str(item.get("readiness") or "")
    effort = str(item.get("effort") or "blocked")

    if not approved:
        blockers.append("This exact fix plan has not been approved with its plan fingerprint.")
    if not bool(config_value(config, "execution_policy.auto_fix_allowed", False)) and not approved:
        blockers.append("execution_policy.auto_fix_allowed is false; needs explicit approval.")
    if readiness != "auto-fix-candidate" and not approved and readiness not in ("ask-for-confirmation", "redirect-to-owner"):
        blockers.append(f"Readiness is {readiness}; needs approval bound to this exact fix plan.")
    max_effort = str(config_value(config, "execution_policy.max_auto_fix_effort", "medium"))
    if effort_rank(effort) > effort_rank(max_effort) and effort not in ("hard", "blocked") and not approved:
        blockers.append(f"Effort is {effort}; exceeds max_auto_fix_effort={max_effort}.")
    return ("done" if not blockers else "blocked", blockers)


def normalize_completion_actions(config: dict[str, Any], args: argparse.Namespace) -> list[str]:
    requested = getattr(args, "completion_action", None)
    values = requested if requested else config_value(config, "execution_policy.approved_completion_actions", [])
    if values in (None, ""):
        return []
    if not isinstance(values, list):
        raise SystemExit("execution_policy.approved_completion_actions must be a list.")
    actions = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
    invalid = sorted(set(actions) - COMPLETION_ACTIONS)
    if invalid:
        raise SystemExit(f"Invalid completion action(s): {', '.join(invalid)}")
    return sorted(actions)


def fix_plan_fingerprint(issue: dict[str, Any], item: dict[str, Any], args: argparse.Namespace) -> str:
    issue_for_approval = {key: value for key, value in issue.items() if key != "raw"}
    issue_payload = json.dumps(issue_for_approval, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload = {
        "issue": issue.get("number") or issue.get("id"),
        "issue_id": issue.get("id"),
        "issue_title": issue.get("title"),
        "issue_hash": hashlib.sha256(issue_payload.encode("utf-8")).hexdigest(),
        "repository_match": item.get("repository_match"),
        "ownership": item.get("ownership"),
        "readiness": item.get("readiness"),
        "effort": item.get("effort"),
        "risk": item.get("risk"),
        "files": sorted(str(file) for file in (args.files or [])),
        "route": args.route or "",
        "notes": args.notes or "",
        "verification_mode": getattr(args, "verification_mode", STANDARD_VERIFICATION_MODE),
        "completion_actions": sorted(getattr(args, "completion_action", None) or []),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def issue_summary_line(issue: dict[str, Any]) -> str:
    return f"{issue.get('number') or issue.get('id')} / {issue.get('id') or ''} - {issue.get('title') or ''}".strip()


def configured_verification_steps(config: dict[str, Any]) -> list[str]:
    steps: list[str] = []
    verification = config.get("verification") or {}
    for key in ("format_write", "format_check", "lint", "stylelint", "test"):
        value = verification.get(key)
        if value:
            steps.append(f"{key}: {value}")
    build = verification.get("build")
    if build and verification.get("run_build_by_default"):
        steps.append(f"build: {build}")
    elif build:
        steps.append(f"build: {build}（按风险或用户要求执行）")
    if config_value(config, "browser_verification.enabled", False):
        steps.append(f"browser: {config_value(config, 'browser_verification.app_url', '<app-url>')}")
    return steps


def render_fix_plan(
    config: dict[str, Any],
    issue: dict[str, Any],
    item: dict[str, Any],
    approved: bool,
    fingerprint: str,
    blockers: list[str],
    args: argparse.Namespace,
    planning_diagnostic: bool = False,
) -> str:
    blocker_lines = "\n".join(f"- {blocker}" for blocker in blockers) or "- 无"
    if planning_diagnostic:
        return f"""# 规划阻塞诊断

## 工单

- {issue_summary_line(issue)}
- 状态: {display_scalar(issue.get('status'))}
- 优先级: {display_scalar(issue.get('priority'))}
- 关联需求: {issue_requirement_label(issue) or '无'}

## 硬阻塞

- 仓库匹配: {triage_display_label(str(item.get('repository_match') or ''))}
- 责任归属: {triage_display_label(str(item.get('ownership') or ''))}
- 推荐: {recommendation_label(str(item.get('readiness') or ''), str(item.get('effort') or ''), str(item.get('risk') or ''))}
- 可批准计划指纹: 未生成
- 阻塞项:
{blocker_lines}

## 下一步

先补齐证据、确认归属或缩小风险范围，刷新 `issue.json` 并重新分诊。硬阻塞解除前，不生成实施步骤、验证方案或收尾动作，也不修改代码、提交或更新远程状态。
"""

    files = "\n".join(f"- {file}" for file in args.files) if args.files else "- 待代码搜索后确认"
    verification = "\n".join(f"- {step}" for step in configured_verification_steps(config)) or "- 按项目配置选择适用验证命令"
    approval = "已按计划指纹批准" if approved else "未批准"
    route = args.route or "待确认"
    notes = args.notes or "无"
    verification_mode = getattr(args, "verification_mode", STANDARD_VERIFICATION_MODE)
    completion_actions = getattr(args, "completion_action", None) or []
    completion_lines = "\n".join(f"- {action}" for action in completion_actions) or "- 无"
    return f"""# 修复计划

## 工单

- {issue_summary_line(issue)}
- 状态: {display_scalar(issue.get('status'))}
- 优先级: {display_scalar(issue.get('priority'))}
- 关联需求: {issue_requirement_label(issue) or '无'}

## 修复门禁

- 仓库匹配: {triage_display_label(str(item.get('repository_match') or ''))}
- 责任归属: {triage_display_label(str(item.get('ownership') or ''))}
- 推荐: {recommendation_label(str(item.get('readiness') or ''), str(item.get('effort') or ''), str(item.get('risk') or ''))}
- 批准状态: {approval}
- 计划指纹: {fingerprint}
- 阻塞项:
{blocker_lines}

## 预计改动范围

{files}

## 实施步骤

1. 读取项目规则和相关代码，确认不覆盖用户未提交改动。
2. 复现或定位工单描述中的问题路径。
3. 做最小范围修复，优先复用项目现有组件、工具函数和样式模式。
4. 更新必要的 mock、类型、测试或回归脚本。
5. 运行下面的验证计划，并把结果写入 `verification.md`。

## 验证计划

- 模式: {verification_mode}
{verification}

## 浏览器路线

- 路由/场景: {route}
- 登录策略: 按项目 `login_policy.method_priority`，不在聊天或工件中保存密码。

## 远程工单策略

- 本计划批准后允许连续执行的收尾动作：
{completion_lines}
- 只有动作在本计划中列出、项目配置允许且本地未禁止时，才可执行；未列出的动作仍需另行批准。

## 备注

{notes}
"""


def plan_fix(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config), Path(args.local_config) if args.local_config else None)
    root = Path(config_value(config, "bugflow.root", args.root))
    target = issue_dir(root, args.issue)
    issue = load_issue(target)
    item = triage_issue_dir(config, target)
    planned_files = normalize_planned_files(args.files or [])
    args.files = planned_files
    args.verification_mode = getattr(args, "verification_mode", STANDARD_VERIFICATION_MODE)
    args.completion_action = normalize_completion_actions(config, args)
    candidate_fingerprint = fix_plan_fingerprint(issue, item, args)
    hard_blockers = hard_repair_blockers(item)
    planning_diagnostic = bool(hard_blockers) or not planned_files
    fingerprint = "" if planning_diagnostic else candidate_fingerprint
    approval_value = getattr(args, "approved", "")
    approved = (
        not planning_diagnostic
        and type(approval_value) is str
        and bool(approval_value)
        and approval_value == fingerprint
    )
    if planning_diagnostic:
        status, blockers = "blocked", list(hard_blockers)
    else:
        status, blockers = repair_gate(config, item, approved)
    if not planned_files:
        blockers.append("The fix plan must name at least one literal repository file before it can be approved.")
        status = "blocked"
    if approval_value and not approved:
        blockers.append(
            "Approval is ignored while hard planning blockers remain."
            if planning_diagnostic
            else "Approval fingerprint does not match this exact plan."
        )
        status = "blocked"
    recorded_files = [] if planning_diagnostic else sorted(planned_files)
    recorded_verification_mode = "" if planning_diagnostic else args.verification_mode
    recorded_completion_actions = [] if planning_diagnostic else args.completion_action
    write_markdown_artifact(
        artifact_path(target, "fix-plan"),
        "fix-plan",
        status,
        render_fix_plan(
            config,
            issue,
            item,
            approved,
            fingerprint,
            blockers,
            args,
            planning_diagnostic=planning_diagnostic,
        ),
        {
            "plan_fingerprint": fingerprint,
            "fix_approved": approved and status == "done",
            "planning_diagnostic": planning_diagnostic,
            "planned_files": recorded_files,
            "verification_mode": recorded_verification_mode,
            "completion_actions": recorded_completion_actions,
        },
    )
    result = {
        "issue": args.issue,
        "artifact": normalize_relative_path(artifact_path(target, "fix-plan")),
        "status": status,
        "approved": approved,
        "plan_fingerprint": fingerprint,
        "planning_diagnostic": planning_diagnostic,
        "verification_mode": recorded_verification_mode,
        "completion_actions": recorded_completion_actions,
        "blockers": blockers,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if status == "done" else 2


def render_implementation(issue: dict[str, Any], args: argparse.Namespace) -> str:
    files = "\n".join(f"- {file}" for file in args.files) if args.files else "- 未记录"
    summaries = args.summary or ["未填写"]
    summary = "\n".join(f"- {item}" for item in summaries)
    notes = args.notes or "无"
    blocked = args.blocked or "无"
    return f"""# 实现记录

## 工单

- {issue_summary_line(issue)}

## 修改摘要

{summary}

## 修改文件

{files}

## 远程状态

- {args.remote_status or '未修改'}

## 本地提交

- {args.commit or '未创建'}

## 阻塞/异常

{blocked}

## 备注

{notes}
"""


def record_implementation(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config), Path(args.local_config) if args.local_config else None)
    root = Path(config_value(config, "bugflow.root", args.root))
    target = issue_dir(root, args.issue)
    issue = load_issue(target)
    require_artifact_done(target, "fix-plan", "record implementation")
    plan_metadata = frontmatter_metadata(artifact_path(target, "fix-plan"))
    if plan_metadata.get("fix_approved") != "true":
        raise SystemExit("Cannot record implementation: the current fix plan has no plan-bound user approval.")
    summaries = [str(item).strip() for item in (args.summary or []) if str(item).strip()]
    files = [str(item).strip() for item in (args.files or []) if str(item).strip()]
    if not args.blocked and not summaries:
        raise SystemExit("record-implementation requires at least one non-empty --summary.")
    if not args.blocked and not files:
        raise SystemExit("record-implementation requires at least one changed file in --files.")
    normalized_files = ensure_files_inside_cwd(files) if not args.blocked else files
    try:
        planned_files = json.loads(plan_metadata.get("planned_files") or "[]")
    except json.JSONDecodeError as exc:
        raise SystemExit("Cannot record implementation: planned file evidence is malformed; regenerate the fix plan.") from exc
    if not args.blocked and (
        not isinstance(planned_files, list)
        or set(str(item) for item in planned_files) != set(normalized_files)
    ):
        raise SystemExit(
            "Cannot record implementation: changed files must exactly match the files in the approved fix plan."
        )
    status = "blocked" if args.blocked else "done"
    write_markdown_artifact(
        artifact_path(target, "implementation"),
        "implementation",
        status,
        render_implementation(issue, args),
        {
            "plan_fingerprint": plan_metadata.get("plan_fingerprint", ""),
            "summary_count": len(summaries),
            "file_count": len(normalized_files),
            "files": sorted(normalized_files),
        },
    )
    print(json.dumps({"issue": args.issue, "artifact": normalize_relative_path(artifact_path(target, "implementation")), "status": status}, ensure_ascii=False, indent=2))
    return 0 if status == "done" else 2


VERIFICATION_COMMAND_PATTERN = re.compile(
    r"^\s*(?P<command>.+?)\s*=>\s*(?P<result>passed|failed|blocked|skipped)(?:\s*:\s*(?P<notes>.*))?\s*$",
    re.IGNORECASE,
)


def parsed_verification_commands(args: argparse.Namespace) -> list[dict[str, str]]:
    parsed: list[dict[str, str]] = []
    for value in getattr(args, "command", None) or []:
        match = VERIFICATION_COMMAND_PATTERN.match(str(value))
        if not match:
            parsed.append({"command": str(value), "result": "invalid", "notes": "expected '<command> => <result>'"})
            continue
        parsed.append(
            {
                "command": match.group("command").strip(),
                "result": match.group("result").lower(),
                "notes": (match.group("notes") or "").strip(),
            }
        )
    return parsed


def render_verification(issue: dict[str, Any], args: argparse.Namespace) -> str:
    commands = parsed_verification_commands(args)
    command_lines = (
        "\n".join(
            f"| {item['command']} | {item['result']} | {item['notes'] or '无'} |"
            for item in commands
        )
        or "| 未运行 | pending | 无 |"
    )
    evidence = "\n".join(f"- {item}" for item in (args.evidence or [])) or "- 无"
    blocked = args.blocked or "无"
    mode = getattr(args, "mode", STANDARD_VERIFICATION_MODE)
    confidence = getattr(args, "confidence", "") or "未声明"
    exemption_reason = getattr(args, "exemption_reason", "") or "无"
    return f"""# 验证记录

## 工单

- {issue_summary_line(issue)}

## 验证模式

- 模式: {mode}
- 修复把握: {confidence}
- 轻量验证原因: {exemption_reason}

## 命令验证

| 命令 | 结果 | 说明 |
| --- | --- | --- |
{command_lines}

## 浏览器验证

- 结果: {args.browser}
- 说明: {args.browser_note or '无'}

## 证据

{evidence}

## 剩余风险

{args.residual_risk or '无'}

## 阻塞/失败

{blocked}
"""


def verification_status(args: argparse.Namespace) -> str:
    mode = getattr(args, "mode", STANDARD_VERIFICATION_MODE)
    if args.blocked or args.failed or args.browser in ("failed", "blocked"):
        return "blocked"
    commands = parsed_verification_commands(args)
    results = [item["result"] for item in commands]
    if any(result in ("failed", "blocked", "invalid") for result in results):
        return "blocked"
    if mode == LIGHTWEIGHT_VERIFICATION_MODE:
        if getattr(args, "confidence", "") != "high":
            return "pending"
        if not str(getattr(args, "exemption_reason", "") or "").strip():
            return "pending"
        if not [item for item in (args.evidence or []) if str(item).strip()]:
            return "pending"
        return "done"
    if args.browser == "skipped":
        return "pending"
    if "skipped" in results:
        return "pending"
    if "passed" not in results and args.browser != "passed":
        return "pending"
    return "done"


def lightweight_verification_blockers(
    config: dict[str, Any], target: Path, args: argparse.Namespace
) -> list[str]:
    if getattr(args, "mode", STANDARD_VERIFICATION_MODE) != LIGHTWEIGHT_VERIFICATION_MODE:
        return []

    blockers: list[str] = []
    if not bool(config_value(config, "execution_policy.allow_lightweight_verification", True)):
        blockers.append("Project policy disables lightweight verification.")
    local_denies = config_value(config, "_bugflow_safety.local_denies", []) or []
    if "execution_policy.allow_lightweight_verification" in local_denies:
        blockers.append("Local deny-only config disables lightweight verification.")

    plan_metadata = frontmatter_metadata(artifact_path(target, "fix-plan"))
    if plan_metadata.get("fix_approved") != "true":
        blockers.append("The current fix plan is not approved.")
    if plan_metadata.get("verification_mode") != LIGHTWEIGHT_VERIFICATION_MODE:
        blockers.append("The approved fix plan did not declare lightweight verification.")

    if artifact_effective_status(target, "triage-report") != "done":
        blockers.append("Triage is not current and complete.")
        return blockers
    triage_metadata = frontmatter_metadata(artifact_path(target, "triage-report"))
    if triage_metadata.get("repository_match") != "current-repo":
        blockers.append("Lightweight verification is limited to the current repository.")
    if triage_metadata.get("ownership") != "frontend-owned":
        blockers.append("Lightweight verification is limited to clearly frontend-owned fixes.")
    if triage_metadata.get("confidence") != "high":
        blockers.append("Repository/ownership confidence is not high.")
    if triage_metadata.get("effort") not in ("easy", "medium"):
        blockers.append("Effort is too high for lightweight verification.")
    if triage_metadata.get("risk") not in ("low", "medium"):
        blockers.append("Risk is too high for lightweight verification.")
    if triage_metadata.get("confirmation_required") == "true":
        blockers.append("Unresolved confirmation cannot use lightweight verification.")
    if triage_metadata.get("readiness") in ("ask-for-confirmation", "redirect-to-owner"):
        blockers.append("The triage recommendation does not permit repair.")
    if triage_metadata.get("evidence_complete") != "true":
        blockers.append("Inbound evidence is incomplete; lightweight verification is not permitted.")
    return blockers


def record_verification(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config), Path(args.local_config) if args.local_config else None)
    root = Path(config_value(config, "bugflow.root", args.root))
    target = issue_dir(root, args.issue)
    issue = load_issue(target)
    require_artifact_done(target, "implementation", "record verification")
    policy_blockers = lightweight_verification_blockers(config, target, args)
    if policy_blockers:
        existing_blocker = str(args.blocked or "").strip()
        args.blocked = "; ".join([*policy_blockers, *([existing_blocker] if existing_blocker else [])])
    status = verification_status(args)
    commands = parsed_verification_commands(args)
    passed_checks = sum(item["result"] == "passed" for item in commands) + int(args.browser == "passed")
    evidence_count = passed_checks + len(args.evidence or [])
    write_markdown_artifact(
        artifact_path(target, "verification"),
        "verification",
        status,
        render_verification(issue, args),
        {
            "passed_checks": passed_checks,
            "evidence_count": evidence_count,
            "verification_mode": getattr(args, "mode", STANDARD_VERIFICATION_MODE),
            "confidence": getattr(args, "confidence", ""),
            "lightweight_approved": (
                getattr(args, "mode", STANDARD_VERIFICATION_MODE) == LIGHTWEIGHT_VERIFICATION_MODE
                and status == "done"
                and not policy_blockers
            ),
        },
    )
    print(json.dumps({"issue": args.issue, "artifact": normalize_relative_path(artifact_path(target, "verification")), "status": status}, ensure_ascii=False, indent=2))
    return 0 if status == "done" else 2


def render_closure(issue: dict[str, Any], verification_state: str, args: argparse.Namespace) -> str:
    return f"""# 闭环记录

## 工单

- {issue_summary_line(issue)}

## 本地结论

- {args.summary or '未填写'}

## 验证状态

- {verification_state}

## 远程评论

- {args.remote_comment or '未发布'}

## 远程状态

- {args.remote_status or '未修改'}

## 本地提交

- {args.commit or '未创建'}

## 剩余风险

{args.residual_risk or '无'}

## 后续事项

{args.follow_up or '无'}
"""


def close_local(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config), Path(args.local_config) if args.local_config else None)
    root = Path(config_value(config, "bugflow.root", args.root))
    target = issue_dir(root, args.issue)
    issue = load_issue(target)
    verification_state = artifact_effective_status(target, "verification")
    if verification_state == "done":
        status = "done"
    elif args.allow_partial:
        status = "partial"
    else:
        status = "blocked"
    write_markdown_artifact(
        artifact_path(target, "closure"),
        "closure",
        status,
        render_closure(issue, verification_state, args),
    )
    print(
        json.dumps(
            {
                "issue": args.issue,
                "artifact": normalize_relative_path(artifact_path(target, "closure")),
                "status": status,
                "verification_status": verification_state,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if status in ("done", "partial") else 2


def run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def ensure_files_inside_cwd(files: list[str]) -> list[str]:
    cwd = Path.cwd().resolve()
    normalized: list[str] = []
    for file in files:
        raw = str(file)
        if (
            not raw
            or raw.startswith(("-", ":"))
            or raw.strip() in (".", "..")
            or any(character in raw for character in ("*", "?", "[", "]", "\x00", "\n", "\r"))
        ):
            raise SystemExit(f"Invalid file path for commit-fix: {file}")
        source = (cwd / raw) if not Path(raw).is_absolute() else Path(raw)
        if source.is_symlink():
            raise SystemExit(f"Refusing symbolic-link path for commit-fix: {file}")
        resolved = source.resolve()
        if not resolved.is_relative_to(cwd):
            raise SystemExit(f"Refusing to stage path outside repository cwd: {file}")
        if not resolved.exists():
            raise SystemExit(f"Refusing missing path for commit-fix: {file}")
        if not resolved.is_file():
            raise SystemExit(f"Refusing non-file path for commit-fix: {file}")
        relative = resolved.relative_to(cwd).as_posix()
        if relative not in normalized:
            normalized.append(relative)
    return normalized


def normalize_planned_files(files: list[str]) -> list[str]:
    """Normalize literal plan paths while allowing not-yet-created files."""

    cwd = Path.cwd().resolve()
    normalized: list[str] = []
    for file in files:
        raw = str(file).strip()
        if (
            not raw
            or raw.startswith(("-", ":"))
            or raw in (".", "..")
            or any(character in raw for character in ("*", "?", "[", "]", "\x00", "\n", "\r"))
        ):
            raise SystemExit(f"Invalid planned file path: {file}")
        source = (cwd / raw) if not Path(raw).is_absolute() else Path(raw)
        if source.is_symlink():
            raise SystemExit(f"Refusing symbolic-link path in fix plan: {file}")
        resolved = source.resolve()
        if not resolved.is_relative_to(cwd):
            raise SystemExit(f"Refusing planned path outside repository cwd: {file}")
        if resolved.exists() and not resolved.is_file():
            raise SystemExit(f"Refusing non-file path in fix plan: {file}")
        relative = resolved.relative_to(cwd).as_posix()
        if relative not in normalized:
            normalized.append(relative)
    return normalized


def commit_message_from_issue(config: dict[str, Any], issue: dict[str, Any], template: str | None) -> str:
    issue_number = str(issue.get("number") or issue.get("id") or "issue")
    title = str(issue.get("title") or "fix issue").strip()
    message_template = template or str(config_value(config, "git_policy.commit_message_template", "fix({issue}): {title}"))
    message = message_template.format(issue=issue_number, id=issue.get("id") or issue_number, title=title)
    return " ".join(message.split())


def commit_fix(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config), Path(args.local_config) if args.local_config else None)
    if not args.files:
        raise SystemExit("commit-fix requires --files so only fix-related files are staged.")
    repository = run_git(["rev-parse", "--show-toplevel"])
    if repository.returncode != 0:
        raise SystemExit(repository.stderr.strip() or repository.stdout.strip() or "commit-fix must run inside a git repository")
    if Path(repository.stdout.strip()).resolve() != Path.cwd().resolve():
        raise SystemExit("commit-fix must run from the git repository root so literal file paths remain unambiguous.")
    files = ensure_files_inside_cwd(args.files)

    root = Path(config_value(config, "bugflow.root", args.root))
    target = issue_dir(root, args.issue)
    issue = load_issue(target)
    require_artifact_done(target, "fix-plan", "authorize commit")
    plan_metadata = frontmatter_metadata(artifact_path(target, "fix-plan"))
    authorization = str(getattr(args, "authorized", "") or "")
    one_time_authorized = bool(authorization) and authorization == plan_metadata.get("plan_fingerprint")
    if "completion_actions" in plan_metadata:
        try:
            completion_actions = json.loads(plan_metadata.get("completion_actions") or "[]")
        except json.JSONDecodeError as exc:
            raise SystemExit("Fix-plan completion actions are malformed; regenerate the plan.") from exc
        if not isinstance(completion_actions, list) or "commit" not in completion_actions:
            raise SystemExit("The approved fix plan does not authorize a local commit completion action.")
    local_denies = config_value(config, "_bugflow_safety.local_denies", []) or []
    if "git_policy.auto_commit_after_fix" in local_denies:
        raise SystemExit("Local deny-only config disables commit-fix; one-time authorization cannot override it.")
    if not one_time_authorized:
        policy = "enabled" if bool(config_value(config, "git_policy.auto_commit_after_fix", False)) else "disabled"
        raise SystemExit(
            "Explicit commit authorization must match the current plan fingerprint "
            f"(project auto-commit capability is {policy} and does not replace plan-bound action authorization)."
        )
    require_artifact_done(target, "implementation", "commit fix")
    require_artifact_done(target, "verification", "commit fix")
    implementation_metadata = frontmatter_metadata(artifact_path(target, "implementation"))
    verification_metadata = frontmatter_metadata(artifact_path(target, "verification"))
    if int(implementation_metadata.get("summary_count") or 0) < 1 or int(implementation_metadata.get("file_count") or 0) < 1:
        raise SystemExit("Implementation evidence is empty; record a non-empty summary and changed-file list first.")
    try:
        implementation_files = json.loads(implementation_metadata.get("files") or "[]")
    except json.JSONDecodeError as exc:
        raise SystemExit("Implementation file evidence is malformed; record implementation again.") from exc
    if not isinstance(implementation_files, list) or set(str(item) for item in implementation_files) != set(files):
        raise SystemExit("commit-fix --files must exactly match the files recorded in the verified implementation.")
    verification_mode = verification_metadata.get("verification_mode") or STANDARD_VERIFICATION_MODE
    evidence_count = int(verification_metadata.get("evidence_count") or 0)
    if verification_mode == LIGHTWEIGHT_VERIFICATION_MODE:
        if verification_metadata.get("lightweight_approved") != "true" or evidence_count < 1:
            raise SystemExit("Lightweight verification lacks an approved exception or inspection evidence.")
    elif int(verification_metadata.get("passed_checks") or 0) < 1 or evidence_count < 1:
        raise SystemExit("Verification has no structured passing evidence; record verification again.")

    staged_before = run_git(["diff", "--cached", "--name-only", "-z"])
    if staged_before.returncode != 0:
        raise SystemExit(staged_before.stderr.strip() or staged_before.stdout.strip() or "git staged-file check failed")
    if staged_before.stdout:
        raise SystemExit("Refusing commit-fix because the git index already contains pre-staged work.")

    status = run_git(["--literal-pathspecs", "status", "--porcelain", "--", *files])
    if status.returncode != 0:
        raise SystemExit(status.stderr.strip() or status.stdout.strip() or "git status failed")
    if not status.stdout.strip():
        raise SystemExit("No changes found in the specified files.")

    message = commit_message_from_issue(config, issue, args.message)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "issue": args.issue,
                    "message": message,
                    "files": files,
                    "status": status.stdout.splitlines(),
                    "dry_run": True,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    add = run_git(["--literal-pathspecs", "add", "--", *files])
    if add.returncode != 0:
        run_git(["reset", "--mixed", "--quiet", "HEAD"])
        raise SystemExit(add.stderr.strip() or add.stdout.strip() or "git add failed")
    staged_after = run_git(["diff", "--cached", "--name-only", "-z"])
    staged_files = {item for item in staged_after.stdout.split("\x00") if item}
    if staged_after.returncode != 0 or staged_files != set(files):
        run_git(["reset", "--mixed", "--quiet", "HEAD"])
        detail = staged_after.stderr.strip() or f"staged paths differ: {sorted(staged_files)}"
        raise SystemExit(f"Refusing commit because exact-file staging could not be verified: {detail}")
    commit = run_git(["commit", "-m", message])
    if commit.returncode != 0:
        run_git(["reset", "--mixed", "--quiet", "HEAD"])
        raise SystemExit(commit.stderr.strip() or commit.stdout.strip() or "git commit failed")
    rev = run_git(["rev-parse", "--short", "HEAD"])
    commit_hash = rev.stdout.strip() if rev.returncode == 0 else ""
    print(
        json.dumps(
            {
                "issue": args.issue,
                "message": message,
                "files": files,
                "commit": commit_hash,
                "pushed": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def normalize_relative_path(path: Path) -> str:
    return str(path).replace("\\", "/").removeprefix("./")


def top_ignore_pattern(path: Path) -> str:
    normalized = normalize_relative_path(path)
    first = normalized.split("/", 1)[0]
    return f"{first}/" if first else normalized


def gitignore_contains(path: Path, pattern: str) -> bool:
    if not path.exists():
        return False
    candidates = {pattern, pattern.rstrip("/"), f"{pattern}**"}
    for line in path.read_text(encoding="utf-8").splitlines():
        clean = line.strip()
        if clean and not clean.startswith("#") and clean in candidates:
            return True
    return False


def append_gitignore_pattern(path: Path, pattern: str) -> str:
    if gitignore_contains(path, pattern):
        return "exists"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    path.write_text(f"{existing}{prefix}{pattern}\n", encoding="utf-8")
    return "added"


def build_project_template(args: argparse.Namespace) -> str:
    template_name = "feishu-project-config.template.yaml" if args.platform == "feishu-project" else "project-config.template.yaml"
    text = read_asset(template_name)
    project_name = args.project_name or Path.cwd().name
    replacements = {
        "name: example-project": f"name: {yaml_scalar(project_name)}",
        "project_key: example-key": f"project_key: {yaml_scalar(args.project_key or 'your-project-key')}",
        "project_key: your-project-key": f"project_key: {yaml_scalar(args.project_key or 'your-project-key')}",
        "work_item_type: issue": f"work_item_type: {yaml_scalar(args.work_item_type)}",
        "role_assumption: frontend": f"role_assumption: {yaml_scalar(args.role)}",
        "repo_key: current-repo": f"repo_key: {yaml_scalar(args.repo_key)}",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"(?m)^(\s*platform:)\s*.*$", rf"\1 {yaml_scalar(args.platform)}", text, count=1)
    project_key = args.project_key or ("your-project-key" if args.platform == "feishu-project" else "")
    text = re.sub(r"(?m)^(\s*project_key:)\s*.*$", rf"\1 {yaml_scalar(project_key)}", text, count=1)
    text = re.sub(r"(?m)^(\s*root:)\s*.*$", rf"\1 {yaml_scalar(normalize_relative_path(Path(args.root)))}", text, count=1)
    text = re.sub(r"(?m)^(\s*schema:)\s*.*$", rf"\1 {yaml_scalar(normalize_relative_path(Path(args.schema)))}", text, count=1)
    return text


def build_local_template(args: argparse.Namespace) -> str:
    text = read_asset("local-overrides.template.yaml")
    if args.platform != "feishu-project":
        text = re.sub(r"(?m)^(\s*assigned_to:)\s*.*$", r'\1 ""', text, count=1)
    project_path = normalize_relative_path(Path(args.config))
    text = re.sub(
        r"(?m)^(\s*-\s*)\.codex/bugflow/issue-triage\.project\.yaml\s*$",
        rf"\1{project_path}",
        text,
        count=1,
    )
    for key in (
        "update_status_allowed",
        "update_comments_allowed",
        "default_change_to_in_progress",
        "default_resolve_for_acceptance",
        "default_complete",
        "default_terminate",
        "auto_commit_after_fix",
        "push_after_commit",
    ):
        text = re.sub(rf"(?m)^(\s*{re.escape(key)}:)\s*true\s*$", r"\1 false", text)
    return text


def build_schema_template(args: argparse.Namespace) -> str:
    text = read_asset("bugflow-schema.template.yaml")
    return re.sub(
        r"(?m)^(\s*root:)\s*.*$",
        rf"\1 {yaml_scalar(normalize_relative_path(Path(args.root)))}",
        text,
        count=1,
    )


def init_project(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    local_config_path = Path(args.local_config)
    schema_path = Path(args.schema)
    root = Path(args.root)

    results: list[dict[str, str]] = []
    results.append(
        {
            "path": normalize_relative_path(config_path),
            "action": write_text_file(config_path, build_project_template(args), args.force),
        }
    )
    results.append(
        {
            "path": normalize_relative_path(local_config_path),
            "action": write_text_file(local_config_path, build_local_template(args), args.force),
        }
    )
    results.append(
        {
            "path": normalize_relative_path(schema_path),
            "action": write_text_file(schema_path, build_schema_template(args), args.force),
        }
    )

    if not args.skip_gitignore:
        pattern = top_ignore_pattern(root)
        action = append_gitignore_pattern(Path(".gitignore"), pattern)
        results.append({"path": ".gitignore", "action": f"{action} {pattern}"})
        local_pattern = normalize_relative_path(local_config_path)
        local_action = append_gitignore_pattern(Path(".gitignore"), local_pattern)
        results.append({"path": ".gitignore", "action": f"{local_action} {local_pattern}"})

    print(json.dumps({"initialized": results}, ensure_ascii=False, indent=2))
    return 0


def unique_values(values: list[str | None]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value:
            continue
        normalized = str(value)
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def feishu_status_values(config: dict[str, Any], prefer_labels: bool) -> list[str]:
    query_status = config_value(config, "query_policy.status", config_value(config, "issue_source.default_status"))
    values = query_status if isinstance(query_status, list) else [query_status]
    statuses = config.get("statuses") or {}
    output: list[str] = []
    for value in values:
        if value in (None, ""):
            continue
        raw = str(value)
        matched = None
        for status_key, status_data in statuses.items():
            if not isinstance(status_data, dict):
                continue
            if raw in (str(status_key), str(status_data.get("id")), str(status_data.get("label"))):
                matched = status_data
                break
        if matched:
            mapped_value = matched.get("label") if prefer_labels else matched.get("id")
            output.append(str(mapped_value or raw))
        else:
            output.append(raw)
    return unique_values(output)


def quote_sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


MQL_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,127}$")


def validate_mql_identifier(value: Any, label: str) -> str:
    identifier = str(value or "").strip()
    if not MQL_IDENTIFIER_PATTERN.fullmatch(identifier):
        raise SystemExit(f"Invalid {label} for Feishu MQL: expected a simple identifier.")
    return identifier


def format_order_by(order_by: str) -> str:
    parts = order_by.strip().split()
    if len(parts) in (1, 2):
        field = validate_mql_identifier(parts[0], "query_policy.order_by field")
        direction = parts[1].upper() if len(parts) == 2 else "DESC"
        if direction in ("ASC", "DESC"):
            return f"`{field}` {direction}"
    raise SystemExit("Invalid query_policy.order_by: expected '<field> [asc|desc]'.")


def build_feishu_mql(config: dict[str, Any], prefer_labels: bool = True) -> dict[str, Any]:
    mapping = field_mapping(config)
    project_key = validate_mql_identifier(
        config_value(config, "issue_source.project_key", "PROJECT_KEY"),
        "issue_source.project_key",
    )
    work_item_type = validate_mql_identifier(
        config_value(config, "issue_source.work_item_type", "issue"),
        "issue_source.work_item_type",
    )
    raw_select_fields = unique_values(
        [
            mapping.get("id"),
            mapping.get("number"),
            mapping.get("title"),
            mapping.get("status"),
            mapping.get("priority"),
            mapping.get("reporter"),
            mapping.get("assignee"),
            mapping.get("created_at"),
            mapping.get("updated_at"),
            mapping.get("requirements"),
            mapping.get("description"),
            mapping.get("attachments"),
        ]
    )
    select_fields = [validate_mql_identifier(field, "field_mapping value") for field in raw_select_fields]
    status_field = validate_mql_identifier(mapping.get("status") or "work_item_status", "status field")
    assignee_field = validate_mql_identifier(
        mapping.get("assignee") or "current_status_operator",
        "assignee field",
    )
    assigned_to = str(config_value(config, "query_policy.assigned_to", "current_login_user()"))
    status_values = feishu_status_values(config, prefer_labels)
    order_by = format_order_by(str(config_value(config, "query_policy.order_by", f"{mapping.get('updated_at') or 'updated_at'} desc")))
    raw_limit = config_value(config, "query_policy.limit", 20)
    if isinstance(raw_limit, bool) or not re.fullmatch(r"[0-9]+", str(raw_limit).strip()):
        raise SystemExit("Invalid query_policy.limit: expected an integer from 1 to 100.")
    limit = int(str(raw_limit).strip())
    if not 1 <= limit <= 100:
        raise SystemExit("Invalid query_policy.limit: expected an integer from 1 to 100.")
    select_clause = ", ".join(f"`{field}`" for field in select_fields) or "`work_item_id`, `name`, `work_item_status`"
    if assigned_to == "current_login_user()":
        assignee_condition = f"array_contains(`{assignee_field}`, current_login_user())"
    else:
        assignee_condition = f"array_contains(`{assignee_field}`, {quote_sql_string(assigned_to)})"
    status_condition = ", ".join(quote_sql_string(value) for value in status_values)
    if not status_condition:
        raise SystemExit("query_policy.status must contain at least one status value.")
    mql = (
        f"SELECT {select_clause}\n"
        f"FROM `{project_key}`.`{work_item_type}`\n"
        f"WHERE {assignee_condition}\n"
        f"  AND `{status_field}` IN ({status_condition})\n"
        f"ORDER BY {order_by}\n"
        f"LIMIT {limit}"
    )
    exact_field_config_keys = unique_values([status_field, mapping.get("requirements"), mapping.get("attachments")])
    return {
        "project_key": project_key,
        "work_item_type": work_item_type,
        "select_fields": select_fields,
        "status_filter_values": status_values,
        "exact_field_config_keys": exact_field_config_keys,
        "mql": mql,
    }


def feishu_mql(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config), Path(args.local_config) if args.local_config else None)
    if config_value(config, "issue_source.platform") != "feishu-project":
        raise SystemExit("feishu-mql requires issue_source.platform: feishu-project")
    result = build_feishu_mql(config, prefer_labels=not args.use_status_ids)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("MQL:")
        print(result["mql"])
        print()
        print("Exact field config keys:")
        print(", ".join(result["exact_field_config_keys"]) or "none")
    return 0


def doctor(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    local_config_path = Path(args.local_config) if args.local_config else None
    config = load_config(config_path, local_config_path)
    checks: list[dict[str, str]] = []

    def add(level: str, item: str, detail: str) -> None:
        checks.append({"level": level, "item": item, "detail": detail})

    if config_path.exists():
        add("ok", "project-config", f"found {normalize_relative_path(config_path)}")
    else:
        add("error", "project-config", f"missing {normalize_relative_path(config_path)}")

    if local_config_path:
        if local_config_path.exists():
            add("ok", "local-config", f"found {normalize_relative_path(local_config_path)}")
        else:
            add("info", "local-config", f"optional file not found: {normalize_relative_path(local_config_path)}")

    platform = config_value(config, "issue_source.platform")
    project_key = config_value(config, "issue_source.project_key")
    work_item_type = config_value(config, "issue_source.work_item_type")
    starter_project_keys = {"your-project-key", "example-key", "example", "changeme"}
    if platform != "feishu-project" and platform:
        detail = f"{platform} exported JSON"
        if project_key:
            detail += f" ({project_key})"
        add("ok", "issue-source", detail)
    elif platform and project_key and work_item_type and str(project_key).strip().lower() not in starter_project_keys:
        add("ok", "issue-source", f"{platform} {project_key}.{work_item_type}")
    elif platform and project_key and work_item_type:
        add("warn", "issue-source", f"project_key={project_key} is a starter placeholder; configure the real project key")
    else:
        add("error", "issue-source", "missing issue_source.platform/project_key/work_item_type")

    assigned_to = str(config_value(config, "query_policy.assigned_to", "") or "").strip()
    assignee_aliases = config_value(config, "query_policy.assignee_aliases", []) or []
    if platform == "feishu-project" and assigned_to == CURRENT_LOGIN_USER:
        add("ok", "assignee-filter", "native query uses current_login_user()")
    elif assigned_to and assigned_to != CURRENT_LOGIN_USER:
        add("ok", "assignee-filter", f"export import restricted to {assigned_to}")
    elif isinstance(assignee_aliases, list) and any(str(item).strip() for item in assignee_aliases):
        add("ok", "assignee-filter", "export import restricted to configured current-user aliases")
    else:
        add(
            "error",
            "assignee-filter",
            "configure query_policy.assigned_to/assignee_aliases before importing exported JSON",
        )

    mapping = field_mapping(config)
    missing_fields = [key for key in ("id", "number", "title", "status") if key not in mapping]
    if missing_fields:
        add("warn", "field-mapping", f"missing recommended fields: {', '.join(missing_fields)}")
    else:
        add("ok", "field-mapping", "id/number/title/status are mapped")

    requirement_field = mapping.get("requirements")
    if not requirement_field:
        add("warn", "requirement-field", "missing field_mapping.requirements")
    elif platform == "feishu-project" and requirement_field == "requirement":
        add("warn", "requirement-field", "configured as requirement; Feishu linked story is often _field_linked_story")
    else:
        add("ok", "requirement-field", f"requirements -> {requirement_field}")

    statuses = config.get("statuses") or {}
    status_codes: list[str] = []
    missing_status_codes: list[str] = []
    for status_key, status_value in statuses.items():
        if not isinstance(status_value, dict):
            continue
        status_id = status_value.get("id")
        status_label = status_value.get("label") or status_key
        if status_id:
            status_codes.append(f"{status_key}={status_id}({status_label})")
        else:
            missing_status_codes.append(str(status_key))
    starter_status_ids = {"OPEN", "IN PROGRESS", "RESOLVED", "REOPENED", "CLOSED", "SYSTEMENDED"}
    unverified_status_codes = [
        item for item in status_codes if item.split("=", 1)[1].split("(", 1)[0].strip().upper() in starter_status_ids
    ]
    status_ids_verified = bool(config_value(config, "issue_source.status_ids_verified", False))
    if status_codes and not missing_status_codes and (not unverified_status_codes or status_ids_verified):
        add("ok", "status-codes", ", ".join(status_codes))
    elif status_codes:
        detail_parts: list[str] = []
        if unverified_status_codes and not status_ids_verified:
            detail_parts.append("starter ids are unverified: " + ", ".join(unverified_status_codes))
        if missing_status_codes:
            detail_parts.append("missing ids: " + ", ".join(missing_status_codes))
        add("warn", "status-codes", "; ".join(detail_parts) or ", ".join(status_codes))
    elif missing_status_codes:
        add("warn", "status-codes", f"missing ids: {', '.join(missing_status_codes)}")

    root = Path(config_value(config, "bugflow.root", args.root))
    if config_value(config, "bugflow.enabled", True):
        add("ok", "bugflow-root", normalize_relative_path(root))
    else:
        add("warn", "bugflow-root", "bugflow.enabled is false")

    schema = config_value(config, "bugflow.schema")
    if schema:
        schema_path = Path(schema)
        if schema_path.exists():
            add("ok", "bugflow-schema", normalize_relative_path(schema_path))
        else:
            add("warn", "bugflow-schema", f"configured schema does not exist: {normalize_relative_path(schema_path)}")
    else:
        add("info", "bugflow-schema", "not configured")

    commit_artifacts = bool(config_value(config, "bugflow.commit_artifacts_by_default", False))
    if commit_artifacts:
        add("info", "artifact-git-policy", "bugflow artifacts may be committed by project policy")
    else:
        gitignore_path = Path(".gitignore")
        pattern = top_ignore_pattern(root)
        if gitignore_contains(gitignore_path, pattern):
            add("ok", "artifact-git-policy", f"{pattern} is ignored")
        else:
            add("warn", "artifact-git-policy", f"add {pattern} to .gitignore or set commit_artifacts_by_default=true")

    if platform == "feishu-project" and project_key and work_item_type:
        try:
            query = build_feishu_mql(config)
            add("ok", "feishu-mql", f"{len(query['select_fields'])} fields, limit {config_value(config, 'query_policy.limit', 20)}")
            if query["exact_field_config_keys"]:
                add("ok", "field-discovery-keys", ", ".join(query["exact_field_config_keys"]))
        except (Exception, SystemExit) as exc:  # pragma: no cover - defensive diagnostics
            add("warn", "feishu-mql", f"could not build query: {exc}")

    if args.json:
        print(json.dumps({"checks": checks}, ensure_ascii=False, indent=2))
    else:
        for check in checks:
            print(f"[{check['level']}] {check['item']}: {check['detail']}")
    return 1 if any(check["level"] == "error" for check in checks) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=".codex/bugflow/issue-triage.project.yaml")
    parser.add_argument("--local-config", default=".codex/bugflow/issue-triage.local.yaml")
    parser.add_argument("--root", default=".bugflow/issues")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-project", help="Create starter bugflow config files for this repository.")
    init_parser.add_argument("--platform", default="feishu-project")
    init_parser.add_argument("--project-name", default="")
    init_parser.add_argument("--project-key", default="")
    init_parser.add_argument("--work-item-type", default="issue")
    init_parser.add_argument("--repo-key", default="current-repo")
    init_parser.add_argument("--role", default="frontend")
    init_parser.add_argument("--schema", default=".codex/bugflow/schema.yaml")
    init_parser.add_argument("--force", action="store_true", help="Overwrite existing config files.")
    init_parser.add_argument("--skip-gitignore", action="store_true", help="Do not add the bugflow artifact root to .gitignore.")
    init_parser.set_defaults(func=init_project)

    fetch_parser = subparsers.add_parser("fetch-json", help="Import tracker JSON into bugflow issue artifacts.")
    fetch_parser.add_argument("--input", help="Raw or normalized issue JSON file. Reads stdin when omitted.")
    fetch_parser.add_argument("--platform", default="feishu-project")
    fetch_parser.add_argument(
        "--retain-raw",
        action="store_true",
        help="Keep the full raw payload; default storage recursively redacts sensitive values.",
    )
    fetch_parser.add_argument(
        "--assignee",
        action="append",
        help="Current-user assignee name/id for exported JSON. Repeat for aliases.",
    )
    fetch_parser.add_argument(
        "--include-all-assignees",
        action="store_true",
        help="Explicitly import issues assigned to other users as well.",
    )
    fetch_parser.set_defaults(func=fetch_json)

    triage_parser = subparsers.add_parser("triage", help="Generate requirement-match and triage artifacts.")
    triage_parser.add_argument("--issue", help="Issue number/id. Defaults to all issue directories.")
    triage_parser.set_defaults(func=triage)

    plan_parser = subparsers.add_parser("plan-fix", help="Create a controlled fix plan for one issue.")
    plan_parser.add_argument("--issue", required=True, help="Issue number/id.")
    plan_parser.add_argument(
        "--approved",
        metavar="PLAN_FINGERPRINT",
        default="",
        help="Approve only the exact plan fingerprint printed by an earlier blocked plan-fix run.",
    )
    plan_parser.add_argument("--files", nargs="*", default=[], help="Expected files or areas to inspect/change.")
    plan_parser.add_argument("--route", default="", help="Browser route or workflow to verify.")
    plan_parser.add_argument("--notes", default="", help="Additional planning notes.")
    plan_parser.add_argument(
        "--verification-mode",
        choices=[STANDARD_VERIFICATION_MODE, LIGHTWEIGHT_VERIFICATION_MODE],
        default=STANDARD_VERIFICATION_MODE,
        help="Use standard checks or a plan-approved lightweight path for hard-to-automate low-risk fixes.",
    )
    plan_parser.add_argument(
        "--completion-action",
        action="append",
        choices=sorted(COMPLETION_ACTIONS),
        help="Action covered by approval of this exact plan. Repeat as needed.",
    )
    plan_parser.set_defaults(func=plan_fix)

    impl_parser = subparsers.add_parser("record-implementation", help="Record implementation notes after code edits.")
    impl_parser.add_argument("--issue", required=True, help="Issue number/id.")
    impl_parser.add_argument("--summary", action="append", help="Implementation summary bullet. Repeat as needed.")
    impl_parser.add_argument("--files", nargs="*", default=[], help="Changed files.")
    impl_parser.add_argument("--remote-status", default="未修改", help="Remote status action summary.")
    impl_parser.add_argument("--commit", default="", help="Local git commit hash or message.")
    impl_parser.add_argument("--notes", default="", help="Additional notes.")
    impl_parser.add_argument("--blocked", default="", help="Concrete blocker or failure, if any.")
    impl_parser.set_defaults(func=record_implementation)

    verify_parser = subparsers.add_parser("record-verification", help="Record local and browser verification results.")
    verify_parser.add_argument("--issue", required=True, help="Issue number/id.")
    verify_parser.add_argument(
        "--mode",
        choices=[STANDARD_VERIFICATION_MODE, LIGHTWEIGHT_VERIFICATION_MODE],
        default=STANDARD_VERIFICATION_MODE,
        help="Use the verification mode approved in the current fix plan.",
    )
    verify_parser.add_argument(
        "--confidence",
        choices=["low", "medium", "high"],
        default="",
        help="Implementation confidence; lightweight verification requires high.",
    )
    verify_parser.add_argument(
        "--exemption-reason",
        default="",
        help="Why reliable automated/browser verification is impractical for this fix.",
    )
    verify_parser.add_argument(
        "--command",
        action="append",
        help="Structured check '<command> => passed|failed|blocked|skipped'. Repeat as needed.",
    )
    verify_parser.add_argument("--browser", choices=["not-required", "passed", "failed", "blocked", "skipped"], default="not-required")
    verify_parser.add_argument("--browser-note", default="", help="Browser verification note.")
    verify_parser.add_argument("--evidence", action="append", help="Evidence note or screenshot path. Repeat as needed.")
    verify_parser.add_argument("--residual-risk", default="无", help="Residual risk after verification.")
    verify_parser.add_argument("--failed", action="store_true", help="Mark verification as failed.")
    verify_parser.add_argument("--blocked", default="", help="Concrete blocker, if any.")
    verify_parser.set_defaults(func=record_verification)

    close_parser = subparsers.add_parser("close-local", help="Write local closure summary without changing remote state.")
    close_parser.add_argument("--issue", required=True, help="Issue number/id.")
    close_parser.add_argument("--summary", default="", help="Local closure conclusion.")
    close_parser.add_argument("--remote-comment", default="未发布", help="Remote comment action summary.")
    close_parser.add_argument("--remote-status", default="未修改", help="Remote status action summary.")
    close_parser.add_argument("--commit", default="", help="Local git commit hash or message.")
    close_parser.add_argument("--residual-risk", default="无", help="Residual risk.")
    close_parser.add_argument("--follow-up", default="", help="Follow-up items.")
    close_parser.add_argument("--allow-partial", action="store_true", help="Allow closure when verification is not done.")
    close_parser.set_defaults(func=close_local)

    commit_parser = subparsers.add_parser("commit-fix", help="Create a local git commit for one verified fix.")
    commit_parser.add_argument("--issue", required=True, help="Issue number/id.")
    commit_parser.add_argument("--files", nargs="+", required=True, help="Fix-related files to stage and commit.")
    commit_parser.add_argument("--message", default="", help="Override commit message.")
    commit_parser.add_argument(
        "--authorized",
        metavar="PLAN_FINGERPRINT",
        default="",
        help="Record required one-time commit authorization bound to the current fix plan.",
    )
    commit_parser.add_argument("--dry-run", action="store_true", help="Show staged files and message without committing.")
    commit_parser.set_defaults(func=commit_fix)

    daily_parser = subparsers.add_parser("daily", help="Import JSON, triage, and print a daily report.")
    daily_parser.add_argument("--input", help="Raw or normalized issue JSON file. Reads stdin when omitted.")
    daily_parser.add_argument("--platform", default="feishu-project")
    daily_parser.add_argument(
        "--retain-raw",
        action="store_true",
        help="Keep the full raw payload; default storage recursively redacts sensitive values.",
    )
    daily_parser.add_argument(
        "--assignee",
        action="append",
        help="Current-user assignee name/id for exported JSON. Repeat for aliases.",
    )
    daily_parser.add_argument(
        "--include-all-assignees",
        action="store_true",
        help="Explicitly import issues assigned to other users as well.",
    )
    daily_parser.add_argument("--report", help="Optional markdown report output path.")
    daily_parser.set_defaults(func=daily)

    mql_parser = subparsers.add_parser("feishu-mql", help="Print a minimal Feishu Project MQL query from config.")
    mql_parser.add_argument("--json", action="store_true", help="Print machine-readable query metadata.")
    mql_parser.add_argument("--use-status-ids", action="store_true", help="Use configured status ids instead of labels in WHERE.")
    mql_parser.set_defaults(func=feishu_mql)

    doctor_parser = subparsers.add_parser("doctor", help="Check config, schema, and artifact git-ignore policy.")
    doctor_parser.add_argument("--json", action="store_true", help="Print machine-readable checks.")
    doctor_parser.set_defaults(func=doctor)
    return parser


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
