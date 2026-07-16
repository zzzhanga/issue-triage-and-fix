#!/usr/bin/env python3
"""Bugflow v2 runner：初始化配置、导入工单、分诊、记录受控修复闭环。

这个 runner 故意不修改代码、不修改远程工单状态。它的职责是把飞书/MCP/导出的
工单 JSON 转成可恢复的 bugflow 工件，并用项目配置做需求-仓库匹配、初步分诊、
修复计划、实现记录、验证记录和本地闭环摘要。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
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
DEFERRED_USER_VERIFICATION_MODE = "deferred-to-user"
REPORT_QUALITY_POLICY_VERSION = "2"
REPORT_HASH_VERSION = "1"
ARTIFACT_SCHEMA_VERSION = "4"
REPORT_QUALITY_GATED_ARTIFACTS = {
    "triage-report",
    "fix-plan",
    "implementation",
    "verification",
    "closure",
}
COMPLETION_ACTIONS = {
    "commit",
    "start-fix",
    "resolve-for-acceptance",
    "comment",
    "complete",
    "terminate",
}
_RUNNER_REVISION: str | None = None


def runner_revision() -> str:
    global _RUNNER_REVISION
    if _RUNNER_REVISION is None:
        digest = hashlib.sha256()
        for name in ("bugflow_runner.py", "bugflow_artifacts.py", "normalize_issue_payload.py"):
            digest.update((SKILL_ROOT / "scripts" / name).read_bytes())
        _RUNNER_REVISION = digest.hexdigest()[:12]
    return _RUNNER_REVISION


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
    "execution_policy.allow_deferred_user_verification",
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


def cli_repo_root(args: argparse.Namespace) -> Path:
    raw = str(getattr(args, "repo_root", "") or ".").strip()
    return Path(raw).expanduser().resolve()


def argument_path(args: argparse.Namespace, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (cli_repo_root(args) / path).resolve()


def load_runtime_config(args: argparse.Namespace) -> dict[str, Any]:
    config_path = argument_path(args, args.config)
    local_value = str(getattr(args, "local_config", "") or "").strip()
    local_path = argument_path(args, local_value) if local_value else None
    return load_config(config_path, local_path)


def repository_root(config: dict[str, Any], args: argparse.Namespace) -> Path:
    cli_value = str(getattr(args, "repo_root", "") or "").strip()
    raw = cli_value or str(config_value(config, "project.repo_path", ".") or ".")
    path = Path(raw).expanduser()
    resolved = path.resolve() if path.is_absolute() else (cli_repo_root(args) / path).resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise SystemExit(f"Repository root does not exist or is not a directory: {resolved}")
    return resolved


def artifact_root(config: dict[str, Any], args: argparse.Namespace) -> Path:
    explicit = str(getattr(args, "artifact_root", "") or "").strip()
    legacy = str(getattr(args, "root", "") or "").strip()
    raw = explicit or legacy or str(config_value(config, "bugflow.root", "") or "") or ".bugflow/issues"
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (repository_root(config, args) / path).resolve()


def protected_report_path(
    config: dict[str, Any], args: argparse.Namespace, value: str | Path
) -> Path:
    repo = repository_root(config, args)
    artifacts = artifact_root(config, args)
    configured_root = str(config_value(config, "bugflow.report_root", "") or "").strip()
    reports_root = Path(configured_root).expanduser() if configured_root else artifacts.parent
    if not reports_root.is_absolute():
        reports_root = repo / reports_root
    reports_root = reports_root.resolve()
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = repo / candidate
    candidate = candidate.resolve()
    if candidate.suffix.lower() != ".md":
        raise SystemExit("Report output must be a Markdown (.md) file.")
    if not candidate.is_relative_to(reports_root):
        raise SystemExit(f"Report output must stay inside {reports_root}.")
    if candidate.is_relative_to(artifacts):
        raise SystemExit("Report output cannot overwrite a per-issue artifact path.")
    if any(part.casefold() in {".git", ".codex"} for part in candidate.parts):
        raise SystemExit("Report output cannot write into .git or .codex configuration paths.")
    if candidate.exists() and candidate.is_symlink():
        raise SystemExit("Report output cannot replace a symbolic link.")
    return candidate


def write_report(config: dict[str, Any], args: argparse.Namespace, report: str) -> Path | None:
    value = str(getattr(args, "report", "") or "").strip()
    if not value:
        return None
    path = protected_report_path(config, args, value)
    atomic_write_text(path, report)
    return path


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(text)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name and Path(temporary_name).exists():
            Path(temporary_name).unlink()


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


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
    atomic_write_text(path, text)
    return "updated" if existed else "created"


def read_asset(name: str) -> str:
    path = ASSETS_DIR / name
    if not path.exists():
        raise SystemExit(f"Missing skill asset: {path}")
    return path.read_text(encoding="utf-8")


def _wrapper_payload_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    if isinstance(value.get("moql_field_list"), list):
        return [value]
    record_keys = {
        "id",
        "issue_id",
        "work_item_id",
        "number",
        "auto_number",
        "title",
        "name",
        "summary",
    }
    if record_keys & value.keys():
        return [value]
    for key in ("issues", "items", "records"):
        if key in value:
            records = _wrapper_payload_items(value[key])
            if records:
                return records
    records: list[dict[str, Any]] = []
    for child in value.values():
        if isinstance(child, (dict, list)):
            records.extend(_wrapper_payload_items(child))
    return records


def iter_payload_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        raise SystemExit("Input must be a JSON object or array.")
    if isinstance(payload.get("moql_field_list"), list):
        return [payload]
    for key in ("issues", "items", "records"):
        if key in payload:
            records = _wrapper_payload_items(payload[key])
            if records:
                return records
    if "data" in payload:
        records = _wrapper_payload_items(payload["data"])
        if records:
            return records
        raise SystemExit(
            "Input data wrapper contains no readable issue records; expected a list or grouped moql_field_list records."
        )
    return [payload]


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
    effective_tokens = set(assignee_tokens)
    if not effective_tokens and mode == "native-query-current-user":
        identity_sets = [person_identity_tokens(issue.get("assignee")) for issue in issues]
        if any(not identities for identities in identity_sets):
            raise SystemExit(
                "Cannot verify current-user scope: a Feishu record has no readable assignee. "
                "Configure query_policy.assignee_aliases or pass --assignee."
            )
        if identity_sets:
            effective_tokens = set.intersection(*identity_sets)
        if issues and not effective_tokens:
            raise SystemExit(
                "Cannot verify current-user scope: the Feishu payload contains mixed assignees with no common identity. "
                "Configure the current user's name/id alias or pass --assignee instead of trusting the batch."
            )
        mode = "native-query-current-user-verified"
    if not effective_tokens:
        included = list(issues)
    else:
        included = [
            issue
            for issue in issues
            if person_identity_tokens(issue.get("assignee")) & effective_tokens
        ]
    return included, {
        "mode": mode,
        "input_count": len(issues),
        "included_count": len(included),
        "skipped_assignee_count": len(issues) - len(included),
        "assignees": sorted(effective_tokens),
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
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "runner_revision": runner_revision(),
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
    content = "\n".join(frontmatter)
    atomic_write_text(path, content)
    if old_content is not None and old_content != content.encode("utf-8"):
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
    status = effective_artifact_status(issue_root, artifact_id)
    if status != "done" or artifact_id not in REPORT_QUALITY_GATED_ARTIFACTS:
        return status

    issue_path = issue_root / "issue.json"
    if not issue_path.exists():
        return "blocked"
    try:
        issue = json.loads(issue_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "blocked"
    evidence_state = issue_evidence_state(issue)
    report_state = issue_report_quality_state(issue)
    if not evidence_state["complete"] or not report_state["complete"]:
        return "blocked"

    triage_metadata = frontmatter_metadata(artifact_path(issue_root, "triage-report"))
    if (
        triage_metadata.get("triage_policy_version") != REPORT_QUALITY_POLICY_VERSION
        or triage_metadata.get("report_quality_complete") != "true"
        or triage_metadata.get("evidence_complete") != "true"
        or triage_metadata.get("report_quality_hash_version") != REPORT_HASH_VERSION
        or triage_metadata.get("report_quality_input_hash") != report_state["expected_input_hash"]
    ):
        return "blocked"
    return status


def require_artifact_done(issue_root: Path, artifact_id: str, action: str) -> None:
    status = artifact_effective_status(issue_root, artifact_id)
    if status != "done":
        if artifact_id in REPORT_QUALITY_GATED_ARTIFACTS:
            issue_path = issue_root / "issue.json"
            if issue_path.exists():
                try:
                    issue = json.loads(issue_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    issue = {}
                evidence_state = issue_evidence_state(issue)
                quality_state = issue_report_quality_state(issue)
                if not evidence_state["complete"]:
                    raise SystemExit(
                        f"Cannot {action}: inbound evidence is incomplete; refresh detail/comments/attachments and re-triage first."
                    )
                if not quality_state["complete"]:
                    raise SystemExit(
                        f"Cannot {action}: report-quality is {quality_state['status']} or stale; "
                        "bind a complete assessment to the current evidence hash and re-triage first."
                    )
                triage_metadata = frontmatter_metadata(artifact_path(issue_root, "triage-report"))
                if triage_metadata.get("triage_policy_version") != REPORT_QUALITY_POLICY_VERSION:
                    raise SystemExit(
                        f"Cannot {action}: triage uses an older report-quality policy; regenerate triage first."
                    )
                if triage_metadata.get("report_quality_hash_version") != REPORT_HASH_VERSION:
                    raise SystemExit(
                        f"Cannot {action}: triage uses an older report-quality hash version; migrate and regenerate triage first."
                    )
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
    stored_issue = dict(issue)
    stored_issue["_bugflow_meta"] = {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "runner_revision": runner_revision(),
    }
    content = json.dumps(stored_issue, ensure_ascii=False, indent=2) + "\n"
    old_content = issue_path.read_text(encoding="utf-8") if issue_path.exists() else None
    atomic_write_text(issue_path, content)
    if old_content is not None and old_content != content:
        invalidate_downstream(target, "issue-intake")
    return target


def combined_text(issue: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "title",
        "description",
        "reproduction_steps",
        "actual_result",
        "expected_result",
        "acceptance_criteria",
        "environment",
        "test_data",
        "number",
        "id",
        "status",
        "priority",
    ):
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
        if attachments and not isinstance(attachments, list):
            missing.append("attachment container shape is not a readable list")
            continue
        for attachment in attachments if isinstance(attachments, list) else []:
            if isinstance(attachment, dict):
                if attachment.get("decision_relevant") is False:
                    continue
                name = attachment.get("name") or attachment.get("title") or attachment.get("id") or "unnamed attachment"
                inspection_state = str(attachment.get("inspection_state") or "unknown").strip().lower()
                summary = str(attachment.get("summary") or "").strip()
            else:
                name = str(attachment) or "unnamed attachment"
                inspection_state = "unknown"
                summary = ""
            if inspection_state != "inspected":
                missing.append(f"attachment {name} inspection is {inspection_state}")
            elif not summary:
                missing.append(f"attachment {name} was inspected but its factual summary is empty")

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


REPORT_QUALITY_QUESTION_DEFAULTS = {
    "reproduction_steps": "请补充从进入页面到问题出现的最短复现步骤，以及触发问题所需的前置条件。",
    "actual_result": "请明确当前实际发生了什么，并说明可以观察到的异常表现。",
    "expected_result": "请明确期望用户最终看到或得到什么结果。",
    "acceptance_criteria": "请补充可判定修复通过的验收标准或正常页面/设计稿参考。",
    "environment": "请补充出现问题的环境、版本、设备、窗口尺寸或网络条件。",
    "test_data": "请提供可安全共享的工单编号、样例数据或账号角色；不要发送密码、令牌或会话信息。",
}
REPORT_QUALITY_INPUT_FIELDS = (
    "source",
    "id",
    "number",
    "title",
    "description",
    "reproduction_steps",
    "actual_result",
    "expected_result",
    "acceptance_criteria",
    "environment",
    "test_data",
    "requirements",
    "attachments",
    "comments",
    "activities",
    "updated_at",
)


def report_quality_input_hash(issue: dict[str, Any]) -> str:
    evidence = issue.get("evidence_fetch") or {}
    evidence_snapshot = {
        key: evidence.get(key)
        for key in ("status", "detail", "comments", "activities", "media", "findings", "missing")
    } if isinstance(evidence, dict) else evidence
    snapshot = {key: issue.get(key) for key in REPORT_QUALITY_INPUT_FIELDS}
    requirements = issue.get("requirements") or []
    snapshot["requirements"] = [
        {
            key: item.get(key)
            for key in ("id", "number", "title", "url")
        }
        if isinstance(item, dict)
        else item
        for item in requirements
    ]
    snapshot["evidence_fetch"] = evidence_snapshot
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def text_list(value: Any) -> list[str]:
    values = value if isinstance(value, list) else ([] if value in (None, "") else [value])
    return [str(item).strip() for item in values if str(item).strip()]


def report_quality_item_question(item: Any, *, conflict: bool = False) -> str:
    if isinstance(item, dict):
        explicit = str(item.get("question") or "").strip()
        if explicit:
            return explicit
        if conflict:
            topic = str(item.get("topic") or item.get("field") or "验收口径").strip()
            return f"请确认“{topic}”的唯一最终口径，并由需求负责人更新工单或验收标准。"
        field = str(item.get("field") or item.get("code") or "unspecified").strip()
        return REPORT_QUALITY_QUESTION_DEFAULTS.get(
            field,
            "请补充缺失的可观察现象、期望结果或验收口径。",
        )
    text = str(item or "").strip()
    return text


def report_quality_feedback_draft(
    issue: dict[str, Any],
    status: str,
    questions: list[str],
    facts: list[str],
    evidence_refs: list[str],
    conflicts: list[Any],
) -> str:
    issue_label = issue.get("number") or issue.get("id") or "该工单"
    if status == "unknown":
        return "尚未完成工单信息完整度评估；请先核对详情、评论和附件后再决定是否需要对外追问。"
    if not questions:
        return ""
    opening = (
        f"已核对 {issue_label} 的详情、评论和附件，当前存在相互冲突的验收口径。请确认："
        if status == "conflicting"
        else f"已核对 {issue_label} 的详情、评论和附件，当前信息尚不足以进入修复。请补充："
    )
    context_lines: list[str] = []
    if facts:
        context_lines.append(f"已确认事实：{'；'.join(facts)}")
    if evidence_refs:
        context_lines.append(f"依据：{'；'.join(evidence_refs)}")
    for conflict in conflicts:
        if not isinstance(conflict, dict):
            continue
        sources = text_list(conflict.get("sources") or conflict.get("source_refs"))
        if sources:
            context_lines.append(
                f"冲突来源（{conflict.get('topic') or '验收口径'}）：{'；'.join(sources)}"
            )
    context = ("\n" + "\n".join(context_lines)) if context_lines else ""
    question_lines = "\n".join(f"{index}. {question}" for index, question in enumerate(questions, start=1))
    return (
        f"{opening}{context}\n{question_lines}\n"
        "无需指定前后端、代码文件或具体实现；请明确可观察的实际表现、期望结果和验收标准即可。"
    )


def issue_report_quality_state(issue: dict[str, Any]) -> dict[str, Any]:
    quality = issue.get("report_quality")
    if not isinstance(quality, dict):
        quality = {}

    aliases = {
        "complete": "sufficient",
        "ready": "sufficient",
        "incomplete": "needs-clarification",
        "clarification-required": "needs-clarification",
        "needs-confirmation": "needs-clarification",
        "needs_clarification": "needs-clarification",
        "needs_confirmation": "needs-clarification",
        "conflict": "conflicting",
    }
    declared_status = str(quality.get("status") or "unknown").strip().lower()
    declared_status = aliases.get(declared_status, declared_status)
    if declared_status not in {"sufficient", "needs-clarification", "conflicting", "unknown"}:
        declared_status = "unknown"
    status = declared_status

    missing_fields = quality.get("missing_fields") or quality.get("blocking_gaps") or []
    if not isinstance(missing_fields, list):
        missing_fields = [missing_fields]
    conflicts = quality.get("conflicts") or []
    if not isinstance(conflicts, list):
        conflicts = [conflicts]
    missing_fields = [item for item in missing_fields if item not in (None, "")]
    conflicts = [item for item in conflicts if item not in (None, "")]
    assessed_at = str(quality.get("assessed_at") or "").strip()
    hash_version = str(quality.get("hash_version") or "").strip()
    expected_input_hash = report_quality_input_hash(issue)
    input_hash = str(quality.get("input_hash") or quality.get("assessment_input_hash") or "").strip()
    assessment_current = (
        declared_status != "unknown"
        and hash_version == REPORT_HASH_VERSION
        and bool(input_hash)
        and input_hash == expected_input_hash
    )
    facts = text_list(quality.get("facts"))
    evidence_refs = text_list(quality.get("evidence_refs"))
    questions = text_list(quality.get("questions"))
    questions.extend(report_quality_item_question(item) for item in missing_fields)
    questions.extend(report_quality_item_question(item, conflict=True) for item in conflicts)
    questions = list(dict.fromkeys(question for question in questions if question))
    stale_assessment = declared_status != "unknown" and not assessment_current
    if stale_assessment:
        status = "unknown"
        facts = []
        evidence_refs = []
        missing_fields = []
        conflicts = []
        if hash_version != REPORT_HASH_VERSION:
            questions = [
                "工单完整度评估缺少当前 report_quality.hash_version；请先运行 migrate-artifacts，若迁移无法确认兼容则重新核对证据并分诊。"
            ]
        else:
            questions = [
                "工单证据已变化或本次评估未绑定当前证据快照；请重新核对详情、评论和附件后更新 report_quality.input_hash。"
            ]
    elif not assessed_at and declared_status != "unknown":
        assessment_current = False
        status = "unknown"
        questions.insert(0, "请记录本次工单信息完整度评估时间 assessed_at。")
    elif not evidence_refs and declared_status != "unknown":
        assessment_current = False
        status = "unknown"
        questions.insert(0, "请记录支撑本次评估的工单描述、评论、需求文档或附件引用。")
    elif assessment_current and conflicts:
        status = "conflicting"
    elif assessment_current and missing_fields:
        status = "needs-clarification"
    if status == "sufficient" and questions:
        status = "needs-clarification"
    if status == "sufficient" and not facts:
        assessment_current = False
        status = "unknown"
        questions.insert(0, "请先提炼至少一条已确认事实，再将工单标记为信息充分。")
    if status == "unknown" and not questions:
        questions = ["先基于完整详情、历史评论和附件评估复现条件、实际结果、期望结果与验收标准是否明确。"]
    elif status == "needs-clarification" and not questions:
        questions = ["请补充当前缺失的复现条件、实际表现、期望结果或可判定的验收标准。"]
    elif status == "conflicting" and not questions:
        questions = ["请由需求负责人确认唯一的最终目标和验收口径，并同步更新工单。"]

    targets = text_list(quality.get("feedback_targets"))
    for item in [*missing_fields, *conflicts]:
        if isinstance(item, dict) and str(item.get("target") or "").strip():
            targets.append(str(item["target"]).strip())
    target_aliases = {
        "tester": "测试",
        "qa": "测试",
        "product": "产品",
        "product-owner": "产品",
        "reporter": "报告人",
        "backend": "后端",
    }
    targets = [target_aliases.get(target.lower(), target) for target in targets]
    targets = list(dict.fromkeys(targets))
    if status in {"needs-clarification", "conflicting"} and not targets:
        targets = ["测试/产品"]

    evidence_complete = issue_evidence_state(issue)["complete"]
    feedback_draft = str(quality.get("feedback_draft") or "").strip()
    if not evidence_complete:
        feedback_draft = "证据尚未读取完整，暂不生成可对外发送的澄清草稿；请先补齐详情、评论和决策相关附件。"
        feedback_publish_status = "blocked-by-evidence"
    elif not assessment_current or status == "unknown":
        feedback_draft = "工单信息完整度评估尚未绑定当前证据快照，暂不生成可对外发送的澄清草稿。"
        feedback_publish_status = "blocked-by-assessment"
    elif status == "sufficient":
        feedback_draft = ""
        feedback_publish_status = "not-needed"
    else:
        if not feedback_draft:
            feedback_draft = report_quality_feedback_draft(
                issue,
                status,
                questions,
                facts,
                evidence_refs,
                conflicts,
            )
        feedback_publish_status = "draft-only"

    return {
        "status": status,
        "complete": (
            status == "sufficient"
            and assessment_current
            and bool(assessed_at)
            and bool(facts)
            and bool(evidence_refs)
            and not missing_fields
            and not conflicts
            and not questions
        ),
        "assessed_at": assessed_at,
        "hash_version": hash_version,
        "input_hash": input_hash,
        "expected_input_hash": expected_input_hash,
        "assessment_current": assessment_current,
        "facts": facts,
        "evidence_refs": evidence_refs,
        "missing_fields": missing_fields,
        "conflicts": conflicts,
        "questions": questions,
        "feedback_targets": targets,
        "feedback_draft": feedback_draft,
        "feedback_publish_status": feedback_publish_status,
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


def classify_issue(
    config: dict[str, Any],
    issue: dict[str, Any],
    match: dict[str, Any],
    *,
    enforce_readiness_gates: bool = True,
) -> dict[str, Any]:
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

    if enforce_readiness_gates:
        evidence_state = issue_evidence_state(issue)
        report_quality_state = issue_report_quality_state(issue)
    else:
        # Preview deliberately skips the strict evidence and report-quality gates.
        # In particular, do not calculate report_quality_input_hash for every
        # candidate in a batch scan; the selected issue will do that in fix-ready.
        evidence_state = {
            "status": "not-assessed",
            "complete": False,
            "sources": {},
            "findings": [],
            "missing": [],
        }
        report_quality_state = {
            "status": "not-assessed",
            "complete": False,
            "assessed_at": "",
            "hash_version": "",
            "input_hash": "",
            "expected_input_hash": "",
            "assessment_current": False,
            "facts": [],
            "evidence_refs": [],
            "missing_fields": [],
            "conflicts": [],
            "questions": [],
            "feedback_targets": [],
            "feedback_draft": "",
            "feedback_publish_status": "not-generated",
        }
    missing_information = [match["customer_confirmation_question"]] if match.get("confirmation_required") else []
    blocking_reasons: list[str] = []
    if not evidence_state["complete"]:
        blocking_reasons.append("证据获取不完整")
        missing_information.extend(evidence_state["missing"] or ["Complete evidence intake before final triage."])
    if not report_quality_state["complete"]:
        if report_quality_state["status"] == "conflicting":
            blocking_reasons.append("工单中的目标或验收口径相互冲突")
        elif report_quality_state["status"] == "unknown":
            blocking_reasons.append("尚未完成工单信息完整度评估")
        else:
            blocking_reasons.append("工单缺少足以实施或验收的信息")
        missing_information.extend(report_quality_state["questions"])

    if blocking_reasons and enforce_readiness_gates:
        preliminary = ownership
        effort = "blocked"
        readiness = "ask-for-confirmation"
        risk = "high" if risk == "high" else "medium"
        reason = (
            f"{'；'.join(blocking_reasons)}，当前仅保留初步归属判断（{preliminary}）；"
            "补齐证据并形成唯一、可观察的验收口径后必须重新分诊。"
        )

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
        "report_quality_status": report_quality_state["status"],
        "report_quality_complete": report_quality_state["complete"],
        "report_quality_assessed_at": report_quality_state["assessed_at"],
        "report_quality_hash_version": report_quality_state["hash_version"],
        "report_quality_input_hash": report_quality_state["input_hash"],
        "report_quality_expected_input_hash": report_quality_state["expected_input_hash"],
        "report_quality_assessment_current": report_quality_state["assessment_current"],
        "report_quality_facts": report_quality_state["facts"],
        "report_quality_evidence_refs": report_quality_state["evidence_refs"],
        "report_quality_missing_fields": report_quality_state["missing_fields"],
        "report_quality_conflicts": report_quality_state["conflicts"],
        "report_quality_questions": report_quality_state["questions"],
        "feedback_targets": report_quality_state["feedback_targets"],
        "feedback_draft": report_quality_state["feedback_draft"],
        "feedback_publish_status": report_quality_state["feedback_publish_status"],
    }


def render_triage(issue: dict[str, Any], match: dict[str, Any], triage: dict[str, Any]) -> str:
    missing = "\n".join(f"- {item}" for item in triage.get("missing_information") or []) or "- 无"
    findings = "\n".join(f"- {item}" for item in triage.get("evidence_findings") or []) or "- 无"
    evidence_sources = triage.get("evidence_sources") or {}
    quality_facts = "\n".join(f"- {item}" for item in triage.get("report_quality_facts") or []) or "- 无"
    quality_refs = "\n".join(f"- {item}" for item in triage.get("report_quality_evidence_refs") or []) or "- 无"
    quality_gaps = "\n".join(
        f"- {item.get('field') or 'unspecified'}: {item.get('reason') or item.get('question') or ''}"
        if isinstance(item, dict)
        else f"- {item}"
        for item in triage.get("report_quality_missing_fields") or []
    ) or "- 无"
    quality_conflicts = "\n".join(
        (
            f"- {item.get('topic') or 'unspecified'}: {item.get('reason') or item.get('question') or ''}"
            f"；来源: {', '.join(text_list(item.get('sources') or item.get('source_refs'))) or '未记录'}"
        )
        if isinstance(item, dict)
        else f"- {item}"
        for item in triage.get("report_quality_conflicts") or []
    ) or "- 无"
    quality_questions = "\n".join(
        f"- {item}" for item in triage.get("report_quality_questions") or []
    ) or "- 无"
    feedback_targets = ", ".join(triage.get("feedback_targets") or []) or "未指定"
    feedback_draft = triage.get("feedback_draft") or "无"
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

## 工单信息完整度

- 状态: {triage.get('report_quality_status') or 'unknown'}
- 足以实施与验收: {str(bool(triage.get('report_quality_complete'))).lower()}
- 评估时间: {triage.get('report_quality_assessed_at') or '未记录'}
- 绑定当前证据快照: {str(bool(triage.get('report_quality_assessment_current'))).lower()}

### 已确认事实

{quality_facts}

### 证据来源

{quality_refs}

### 缺失项

{quality_gaps}

### 冲突项

{quality_conflicts}

### 精确确认问题

{quality_questions}

### 反馈草稿

- 反馈对象: {feedback_targets}
- 发布状态: {triage.get('feedback_publish_status') or 'blocked-by-assessment'}

{feedback_draft}

## 理由

{triage['reason']}

## 缺失信息

{missing}
"""


def import_json_issues(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    config = load_runtime_config(args)
    platform = config_value(config, "issue_source.platform", args.platform)
    root = artifact_root(config, args)
    payload = load_json_payload(str(argument_path(args, args.input)) if args.input else None)
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
    assigned, filter_summary = filter_imported_issues(normalized, assignee_tokens, filter_mode)
    included, requirement_summary = filter_requirement_scope(
        assigned, requested_requirement_tokens(config, args)
    )
    filter_summary["requirement_filter"] = requirement_summary
    filter_summary["included_count"] = len(included)
    for issue in included:
        if getattr(args, "input", None):
            input_path = argument_path(args, args.input)
            output_path = issue_dir(root, issue_key(issue)) / "issue.json"
            if input_path == output_path.resolve():
                raise SystemExit("Refusing to use the destination issue.json as its own import input.")
        write_issue_json(root, issue)
    return included, filter_summary


def fetch_json(args: argparse.Namespace) -> int:
    config = load_runtime_config(args)
    root = artifact_root(config, args)
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
            "report_quality_status": triage["report_quality_status"],
            "report_quality_complete": triage["report_quality_complete"],
            "report_quality_input_hash": triage["report_quality_input_hash"],
            "report_quality_hash_version": triage["report_quality_hash_version"],
            "report_quality_assessment_current": triage["report_quality_assessment_current"],
            "triage_policy_version": REPORT_QUALITY_POLICY_VERSION,
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
        "report_quality_status": triage["report_quality_status"],
        "report_quality_complete": triage["report_quality_complete"],
        "report_quality_assessed_at": triage["report_quality_assessed_at"],
        "report_quality_input_hash": triage["report_quality_input_hash"],
        "report_quality_expected_input_hash": triage["report_quality_expected_input_hash"],
        "report_quality_assessment_current": triage["report_quality_assessment_current"],
        "report_quality_facts": triage["report_quality_facts"],
        "report_quality_evidence_refs": triage["report_quality_evidence_refs"],
        "report_quality_missing_fields": triage["report_quality_missing_fields"],
        "report_quality_conflicts": triage["report_quality_conflicts"],
        "questions": list(
            dict.fromkeys(
                [
                    *([match.get("customer_confirmation_question")] if match.get("customer_confirmation_question") else []),
                    *triage["report_quality_questions"],
                ]
            )
        ),
        "feedback_targets": triage["feedback_targets"],
        "feedback_draft": triage["feedback_draft"],
        "feedback_publish_status": triage["feedback_publish_status"],
        "confirmation_required": match["confirmation_required"],
        "question": match.get("customer_confirmation_question") or "",
    }


def triage(args: argparse.Namespace) -> int:
    config = load_runtime_config(args)
    root = artifact_root(config, args)
    if args.issue:
        targets = [issue_dir(root, args.issue)]
    elif root.exists():
        targets = sorted(path for path in root.iterdir() if path.is_dir())
    else:
        targets = []
    results = [triage_issue_dir(config, target) for target in targets]
    print(json.dumps({"count": len(results), "items": results}, ensure_ascii=False, indent=2))
    return 0


def preview_report_quality(issue: dict[str, Any]) -> tuple[str, list[str]]:
    quality = issue.get("report_quality") or {}
    if not isinstance(quality, dict):
        quality = {}
    aliases = {
        "complete": "sufficient",
        "ready": "sufficient",
        "incomplete": "needs-clarification",
        "clarification-required": "needs-clarification",
        "needs-confirmation": "needs-clarification",
        "needs_confirmation": "needs-clarification",
        "conflict": "conflicting",
    }
    status = str(quality.get("status") or "unknown").strip().lower()
    status = aliases.get(status, status)
    missing_fields = quality.get("missing_fields") or quality.get("blocking_gaps") or []
    conflicts = quality.get("conflicts") or []
    if conflicts:
        status = "conflicting"
    elif missing_fields:
        status = "needs-clarification"
    if status not in {"sufficient", "needs-clarification", "conflicting", "unknown"}:
        status = "unknown"

    hints: list[str] = []
    for item in missing_fields if isinstance(missing_fields, list) else [missing_fields]:
        if isinstance(item, dict):
            hint = item.get("reason") or item.get("question") or item.get("field")
        else:
            hint = item
        if str(hint or "").strip():
            hints.append(str(hint).strip())
    for item in conflicts if isinstance(conflicts, list) else [conflicts]:
        if isinstance(item, dict):
            sources = text_list(item.get("sources") or item.get("source_refs"))
            hint = f"{item.get('topic') or '验收口径'}存在冲突"
            if sources:
                hint += f"（{', '.join(sources)}）"
        else:
            hint = str(item)
        if hint:
            hints.append(hint)

    if status == "unknown":
        has_narrative = bool(
            str(issue.get("description") or "").strip()
            or issue.get("comments")
            or (issue.get("evidence_fetch") or {}).get("findings")
        )
        hints.append(
            "已有描述，进入 fix-ready 后核对复现、实际、期望与验收口径"
            if has_narrative
            else "缺少可用问题描述，进入 fix-ready 前需补充"
        )
    preview_status = {
        "sufficient": "appears-complete",
        "needs-clarification": "suspected-gaps",
        "conflicting": "suspected-conflict",
        "unknown": "not-assessed",
    }[status]
    return preview_status, list(dict.fromkeys(hints))


def preview_recommendation(item: dict[str, Any]) -> str:
    if item["ownership"] in {"backend-owned", "not-current-repo"}:
        return "建议转交；确认后不在当前仓库兜底"
    if item["readiness_hint"] == "needs-ownership-check":
        return "先确认需求/仓库，再进入 fix-ready"
    if item["readiness_hint"] == "likely-low-risk":
        return (
            "可优先进入 fix-ready；先确认仓库归属"
            if item.get("repository_match") != "current-repo"
            else "可优先进入 fix-ready"
        )
    return "选中后进入 fix-ready 详细评审"


def preview_priority_rank(value: Any) -> int:
    normalized = str(value or "").strip().casefold()
    ranks = {
        "p0": 0,
        "urgent": 0,
        "紧急": 0,
        "p1": 1,
        "high": 1,
        "高": 1,
        "p2": 2,
        "medium": 2,
        "中": 2,
        "p3": 3,
        "low": 3,
        "低": 3,
        "p4": 4,
    }
    return ranks.get(normalized, 9)


def render_preview_markdown(items: list[dict[str, Any]], filter_summary: dict[str, Any]) -> str:
    quality_labels = {
        "appears-complete": "描述看起来较完整（待复核）",
        "suspected-gaps": "疑似待补充",
        "suspected-conflict": "发现口径冲突",
        "not-assessed": "未做正式评估",
    }
    risk_labels = {"low": "低", "medium": "中", "high": "高"}
    rows = [
        [
            str(item.get("preview_order") or ""),
            "<br>".join(str(part) for part in (item.get("id"), item.get("issue")) if part),
            item.get("title") or "",
            item.get("priority") or "",
            item.get("status") or "",
            item.get("requirement") or "",
            item.get("date") or "",
            item.get("assignee") or "",
            triage_display_label(item.get("ownership") or ""),
            risk_labels.get(item.get("risk_hint"), "待判断"),
            quality_labels.get(item.get("preview_report_quality"), "未做正式评估"),
            item.get("reason") or "",
            item.get("recommendation") or "",
        ]
        for item in items
    ]
    table = markdown_table(
        [
            "顺序",
            "缺陷",
            "标题",
            "优先级",
            "状态",
            "关联需求",
            "更新",
            "负责人",
            "初步归属",
            "初步风险",
            "工单信息",
            "初步依据",
            "建议",
        ],
        rows,
    ) if rows else "无"
    hints = "\n".join(
        f"- {item['issue']}: {'；'.join(item.get('information_hints') or [])}"
        for item in items
        if item.get("information_hints")
    ) or "无"
    scope_label = (
        "显式要求纳入的候选工单"
        if filter_summary.get("mode") == "explicit-all-assignees"
        else "指派给当前用户的候选工单"
    )
    return f"""# Bug 快速扫描

本次仅做 preview/scan：纳入 {len(items)} 个{scope_label}。结论均为初步判断；未生成每工单完整工件、未计算正式信息质量哈希、未修改代码/状态、未提交。

## 扫描结果

{table}

## 进入 fix-ready 后需核对

{hints}

## 负责人过滤

- 模式: {filter_summary.get('mode') or ''}
- 输入: {filter_summary.get('input_count') or 0}
- 纳入: {filter_summary.get('included_count') or 0}
- 跳过其他负责人: {filter_summary.get('skipped_assignee_count') or 0}
- 需求范围: {', '.join((filter_summary.get('requirement_filter') or {}).get('requirement_ids') or []) or '全部'}
- 跳过其他需求: {(filter_summary.get('requirement_filter') or {}).get('skipped_requirement_count') or 0}
"""


def preview(args: argparse.Namespace) -> int:
    config = load_runtime_config(args)
    platform = config_value(config, "issue_source.platform", args.platform)
    payload = load_json_payload(str(argument_path(args, args.input)) if args.input else None)
    normalized = [
        normalize_issue(item, platform, field_mapping(config), retain_raw=False, include_raw=False)
        for item in iter_payload_items(payload)
    ]
    assignee_tokens, filter_mode = import_assignee_filter(config, args, str(platform))
    assigned, filter_summary = filter_imported_issues(normalized, assignee_tokens, filter_mode)
    included, requirement_summary = filter_requirement_scope(
        assigned, requested_requirement_tokens(config, args)
    )
    filter_summary["requirement_filter"] = requirement_summary
    filter_summary["included_count"] = len(included)
    items: list[dict[str, Any]] = []
    for issue in included:
        match = match_requirement(config, issue)
        triage = classify_issue(config, issue, match, enforce_readiness_gates=False)
        preview_text = combined_text(issue)
        backend_reason = backend_owned_reason(config, preview_text)
        low_risk_words = (
            "样式",
            "布局",
            "间距",
            "对齐",
            "颜色",
            "字号",
            "溢出",
            "遮挡",
            "图标",
            "css",
            "style",
            "layout",
            "overflow",
            "align",
        )
        high_risk_words = (
            "权限",
            "认证",
            "接口",
            "api",
            "数据库",
            "支付",
            "数据丢失",
            "删除",
            "保存失败",
            "安全",
            "后端",
        )
        if match["repository_match"] != "other-repo" and backend_reason:
            triage.update(
                ownership="backend-owned",
                effort="blocked",
                risk="low",
                readiness="redirect-to-owner",
            )
        elif (
            match["repository_match"] != "other-repo"
            and config_value(config, "project.role_assumption", "") == "frontend"
            and any(word in preview_text for word in low_risk_words)
            and not any(word in preview_text for word in high_risk_words)
        ):
            triage.update(
                ownership="frontend-owned",
                effort="easy",
                risk="low",
                readiness="auto-fix-candidate",
            )
        quality_status, information_hints = preview_report_quality(issue)
        item = {
            "issue": issue.get("number") or issue.get("id"),
            "id": issue.get("id") or "",
            "title": issue.get("title") or "",
            "priority": display_scalar(issue.get("priority")),
            "status": display_scalar(issue.get("status")),
            "requirement": issue_requirement_label(issue),
            "date": issue_date_label(issue),
            "reporter": issue_people(issue, "reporter"),
            "assignee": issue_people(issue, "assignee"),
            "repository_match": match["repository_match"],
            "ownership": triage["ownership"],
            "effort_hint": triage["effort"],
            "risk_hint": triage["risk"],
            "readiness_hint": {
                "auto-fix-candidate": "likely-low-risk",
                "manual-review-first": "needs-fix-ready-review",
                "ask-for-confirmation": "needs-ownership-check",
                "redirect-to-owner": "redirect-candidate",
            }.get(triage["readiness"], "needs-fix-ready-review"),
            "preview_report_quality": quality_status,
            "information_hints": information_hints,
            "reason": triage["reason"],
            "provisional": True,
            "repair_allowed": False,
            "next_step": "fix-ready",
        }
        item["recommendation"] = preview_recommendation(item)
        items.append(item)

    risk_ranks = {"high": 0, "medium": 1, "low": 2}
    items.sort(
        key=lambda item: (
            preview_priority_rank(item.get("priority")),
            risk_ranks.get(str(item.get("risk_hint") or ""), 9),
            str(item.get("issue") or ""),
        )
    )
    for index, item in enumerate(items, start=1):
        item["preview_order"] = index

    report = render_preview_markdown(items, filter_summary)
    write_report(config, args, report)
    if args.json:
        print(
            json.dumps(
                {"mode": "preview", "count": len(items), "items": items, "assignee_filter": filter_summary},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(report)
    return 0


def report_quality_hash_command(args: argparse.Namespace) -> int:
    config = load_runtime_config(args)
    root = artifact_root(config, args)
    target = issue_dir(root, args.issue)
    issue = load_issue(target)
    print(
        json.dumps(
            {
                "issue": issue.get("number") or issue.get("id"),
                "hash_version": REPORT_HASH_VERSION,
                "input_hash": report_quality_input_hash(issue),
                "instruction": (
                    "Bind report_quality.hash_version and report_quality.input_hash to these exact values."
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def migrate_artifacts(args: argparse.Namespace) -> int:
    """Apply only compatibility-preserving metadata migrations for one issue."""
    config = load_runtime_config(args)
    root = artifact_root(config, args)
    target = issue_dir(root, args.issue)
    issue = load_issue(target)
    quality = issue.get("report_quality")
    if not isinstance(quality, dict):
        quality = {}
        issue["report_quality"] = quality

    declared_status = str(quality.get("status") or "unknown").strip().lower()
    existing_version = str(quality.get("hash_version") or "").strip()
    expected_hash = report_quality_input_hash(issue)
    existing_hash = str(quality.get("input_hash") or quality.get("assessment_input_hash") or "").strip()

    if existing_version and existing_version != REPORT_HASH_VERSION:
        raise SystemExit(
            f"Cannot migrate {args.issue}: unsupported report_quality.hash_version={existing_version}; re-assess and re-triage."
        )
    if declared_status != "unknown" and existing_hash != expected_hash:
        raise SystemExit(
            f"Cannot migrate {args.issue}: the stored report-quality hash does not match current evidence; re-assess and re-triage."
        )

    metadata = issue.get("_bugflow_meta") if isinstance(issue.get("_bugflow_meta"), dict) else {}
    changed = (
        existing_version != REPORT_HASH_VERSION
        or metadata.get("artifact_schema_version") != ARTIFACT_SCHEMA_VERSION
        or metadata.get("runner_revision") != runner_revision()
    )
    quality["hash_version"] = REPORT_HASH_VERSION
    target = write_issue_json(root, issue)
    print(
        json.dumps(
            {
                "issue": issue.get("number") or issue.get("id"),
                "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
                "report_quality_hash_version": REPORT_HASH_VERSION,
                "changed": changed,
                "requires_retriage": changed,
                "path": str(target),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def render_daily_markdown(results: list[dict[str, Any]]) -> str:
    auto = [item for item in results if item["readiness"] == "auto-fix-candidate"]
    manual = [item for item in results if item["readiness"] == "manual-review-first"]
    needs = [item for item in results if item["confirmation_required"] or item["readiness"] == "ask-for-confirmation"]
    redirects = [item for item in results if item["readiness"] == "redirect-to-owner"]
    report_clarifications = [item for item in results if item.get("report_quality_complete") is not True]

    quality_labels = {
        "sufficient": "完整",
        "needs-clarification": "待补充",
        "conflicting": "有冲突",
        "unknown": "未评估",
    }

    def quality_label(item: dict[str, Any]) -> str:
        return quality_labels.get(str(item.get("report_quality_status") or "unknown"), "未评估")

    def table(items: list[dict[str, Any]]) -> str:
        if not items:
            return "无"
        return markdown_table(
            ["缺陷", "标题", "优先级", "状态", "提出/更新", "报告人/负责人", "证据", "工单信息", "推荐"],
            [
                [
                    "<br>".join(str(part) for part in (item.get("id"), item.get("issue")) if part),
                    item.get("title") or "",
                    item.get("priority") or "",
                    item.get("status") or "",
                    item.get("date") or "",
                    " / ".join(part for part in (item.get("reporter"), item.get("assignee")) if part),
                    "完整" if item.get("evidence_complete") else "不完整",
                    quality_label(item),
                    recommendation_label(item["readiness"], item["effort"], item["risk"]),
                ]
                for item in items
            ],
        )

    questions = "\n".join(
        f"- {item['issue']}: {question}"
        for item in needs
        for question in item.get("questions") or ([item["question"]] if item.get("question") else [])
    ) or "无"

    clarification_blocks: list[str] = []
    for item in report_clarifications:
        gaps = [
            entry.get("reason") or entry.get("question") or entry.get("field") or "未说明"
            if isinstance(entry, dict)
            else str(entry)
            for entry in item.get("report_quality_missing_fields") or []
        ]
        conflicts = [
            (
                f"{entry.get('reason') or entry.get('question') or entry.get('topic') or '未说明'}"
                f"（来源：{', '.join(text_list(entry.get('sources') or entry.get('source_refs'))) or '未记录'}）"
            )
            if isinstance(entry, dict)
            else str(entry)
            for entry in item.get("report_quality_conflicts") or []
        ]
        detail_lines = [
            f"### {item['issue']} {item.get('title') or ''}",
            "",
            f"- 信息状态: {quality_label(item)}",
            f"- 已确认事实: {'；'.join(item.get('report_quality_facts') or []) or '无'}",
            f"- 依据: {'；'.join(item.get('report_quality_evidence_refs') or []) or '未记录'}",
            f"- 缺失/冲突: {'；'.join([*gaps, *conflicts]) or '尚未完成信息完整度评估'}",
            f"- 反馈对象: {', '.join(item.get('feedback_targets') or []) or '待判断'}",
            f"- 发布状态: {item.get('feedback_publish_status') or 'blocked-by-assessment'}",
            "",
            item.get("feedback_draft") or "尚未生成反馈草稿。",
        ]
        clarification_blocks.append("\n".join(detail_lines))
    clarification_drafts = "\n\n".join(clarification_blocks) or "无"
    summary_parts = [
        f"本次查询到 {len(results)} 个缺陷",
        f"安全候选 {len(auto)} 个",
        f"需人工评审 {len(manual)} 个",
        f"需确认 {len(needs)} 个",
        f"工单信息待补充/确认 {len(report_clarifications)} 个",
        f"建议转交 {len(redirects)} 个",
    ]
    evidence = "；".join(
        f"{item['issue']}：证据{'完整' if item.get('evidence_complete') else '不完整'} / 工单信息{quality_label(item)} / "
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

## 工单描述待补充

{clarification_drafts}
"""


def daily(args: argparse.Namespace) -> int:
    imported, filter_summary = import_json_issues(args)
    config = load_runtime_config(args)
    root = artifact_root(config, args)
    results = [triage_issue_dir(config, issue_dir(root, issue_key(issue))) for issue in imported]
    report = render_daily_markdown(results)
    report += (
        "\n\n## 负责人过滤\n\n"
        f"- 模式: {filter_summary['mode']}\n"
        f"- 输入: {filter_summary['input_count']}\n"
        f"- 纳入: {filter_summary['included_count']}\n"
        f"- 跳过其他负责人: {filter_summary['skipped_assignee_count']}\n"
    )
    write_report(config, args, report)
    print(report)
    return 0


def daily_existing(args: argparse.Namespace) -> int:
    config = load_runtime_config(args)
    root = artifact_root(config, args)
    issue_keys = list(dict.fromkeys(str(key) for key in args.issue))
    targets = [issue_dir(root, key) for key in issue_keys]
    missing = [str(target) for target in targets if not (target / "issue.json").exists()]
    if missing:
        raise SystemExit(f"Missing current issue artifact(s): {', '.join(missing)}")
    loaded = [load_issue(target) for target in targets]
    identity_mismatches = [
        f"{target} declares {issue_key(issue)}"
        for target, issue in zip(targets, loaded)
        if issue_dir(root, issue_key(issue)).resolve() != target.resolve()
    ]
    if identity_mismatches:
        raise SystemExit(
            "Stored issue identity does not match the explicitly selected directory: "
            + "; ".join(identity_mismatches)
        )
    if bool(getattr(args, "include_all_assignees", False)):
        assignee_tokens: set[str] = set()
        filter_mode = "explicit-all-assignees"
    else:
        raw_cli_assignees = getattr(args, "assignee", None) or []
        if isinstance(raw_cli_assignees, str):
            raw_cli_assignees = [raw_cli_assignees]
        configured_assignee = str(config_value(config, "query_policy.assigned_to", "") or "")
        configured_aliases = config_value(config, "query_policy.assignee_aliases", []) or []
        if not isinstance(configured_aliases, list):
            raise SystemExit("query_policy.assignee_aliases must be a list of current-user names or ids.")
        candidates = raw_cli_assignees or [configured_assignee, *configured_aliases]
        assignee_tokens = {
            identity_token(value)
            for value in candidates
            if identity_token(value) and identity_token(value) != identity_token(CURRENT_LOGIN_USER)
        }
        if not assignee_tokens:
            raise SystemExit(
                "daily-existing cannot trust current_login_user() for already stored artifacts. "
                "Pass --assignee <current-user-name-or-id>, configure a concrete assignee alias, "
                "or explicitly pass --include-all-assignees."
            )
        filter_mode = "matched-existing-current-assignee"

    filtered_pairs = [
        (target, issue)
        for target, issue in zip(targets, loaded)
        if not assignee_tokens or person_identity_tokens(issue.get("assignee")) & assignee_tokens
    ]
    assignee_included_count = len(filtered_pairs)
    requirement_tokens = requested_requirement_tokens(config, args)
    if requirement_tokens:
        filtered_pairs = [
            (target, issue)
            for target, issue in filtered_pairs
            if requirement_identity_tokens(issue) & requirement_tokens
        ]
    filtered_targets = [target for target, _issue in filtered_pairs]
    filter_summary = {
        "mode": filter_mode,
        "input_count": len(loaded),
        "included_count": len(filtered_pairs),
        "skipped_assignee_count": len(loaded) - assignee_included_count,
        "assignees": sorted(assignee_tokens),
        "requirement_ids": sorted(requirement_tokens),
        "skipped_requirement_count": assignee_included_count - len(filtered_pairs),
    }
    results = [triage_issue_dir(config, target) for target in filtered_targets]
    report = render_daily_markdown(results)
    report += (
        "\n\n## 输入范围\n\n"
        f"- 模式: existing-current-run / {filter_summary['mode']}\n"
        f"- 显式工单数: {len(targets)}\n"
        f"- 纳入当前负责人及需求范围: {len(filtered_targets)}\n"
        f"- 跳过其他负责人: {filter_summary['skipped_assignee_count']}\n"
        f"- 需求范围: {', '.join((filter_summary.get('requirement_filter') or {}).get('requirement_ids') or []) or '全部'}\n"
        f"- 跳过其他需求: {(filter_summary.get('requirement_filter') or {}).get('skipped_requirement_count') or 0}\n"
        f"- 跳过其他需求: {filter_summary['skipped_requirement_count']}\n"
        "- 说明: 仅使用命令显式列出的当前 issue.json，未扫描历史目录。\n"
    )
    write_report(config, args, report)
    print(report)
    return 0


def requirement_identity_tokens(issue: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for requirement in issue.get("requirements") or []:
        if isinstance(requirement, dict):
            for key in ("id", "number", "url"):
                token = identity_token(requirement.get(key))
                if token:
                    tokens.add(token)
        else:
            token = identity_token(requirement)
            if token:
                tokens.add(token)
    return tokens


def requested_requirement_tokens(config: dict[str, Any], args: argparse.Namespace) -> set[str]:
    cli_values = getattr(args, "requirement_id", None) or []
    if isinstance(cli_values, str):
        cli_values = [cli_values]
    configured = config_value(config, "query_policy.requirement_ids", []) or []
    if isinstance(configured, str):
        configured = [configured]
    values = cli_values or configured
    return {identity_token(value) for value in values if identity_token(value)}


def filter_requirement_scope(
    issues: list[dict[str, Any]], requirement_tokens: set[str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not requirement_tokens:
        return list(issues), {
            "mode": "all-requirements",
            "requirement_ids": [],
            "input_count": len(issues),
            "included_count": len(issues),
            "skipped_requirement_count": 0,
        }
    included = [
        issue for issue in issues if requirement_identity_tokens(issue) & requirement_tokens
    ]
    return included, {
        "mode": "matched-requirements",
        "requirement_ids": sorted(requirement_tokens),
        "input_count": len(issues),
        "included_count": len(included),
        "skipped_requirement_count": len(issues) - len(included),
    }


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
    if item.get("report_quality_complete") is not True:
        blockers.append(
            "Issue reproduction, actual/expected behavior, or acceptance criteria are not sufficiently clear; "
            "approval cannot bypass the report-quality gate."
        )

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
    if requested:
        values = requested
    elif (
        getattr(args, "verification_mode", STANDARD_VERIFICATION_MODE)
        == DEFERRED_USER_VERIFICATION_MODE
    ):
        values = config_value(
            config,
            "execution_policy.assisted_completion_actions",
            None,
        )
        if values is None:
            autonomous_values = config_value(
                config, "execution_policy.approved_completion_actions", []
            )
            if not isinstance(autonomous_values, list):
                raise SystemExit(
                    "execution_policy.approved_completion_actions must be a list."
                )
            values = [
                value
                for value in autonomous_values
                if str(value).strip() in ("commit", "start-fix")
            ]
    else:
        values = config_value(
            config, "execution_policy.approved_completion_actions", []
        )
        if not isinstance(values, list):
            raise SystemExit(
                "execution_policy.approved_completion_actions must be a list."
            )
        # Legacy project configs may still list resolve-for-acceptance in the
        # autonomous default bundle. Normal repair runs must end at in-progress;
        # a later resolved transition has to be requested explicitly.
        values = [
            value
            for value in values
            if str(value).strip() in ("commit", "start-fix")
        ]
    if values in (None, ""):
        return []
    if not isinstance(values, list):
        raise SystemExit("The selected execution-policy completion action bundle must be a list.")
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
        "report_quality_status": item.get("report_quality_status"),
        "report_quality_complete": item.get("report_quality_complete"),
        "report_quality_hash_version": item.get("report_quality_hash_version"),
        "report_quality_questions": item.get("report_quality_questions") or item.get("questions") or [],
        "triage_policy_version": REPORT_QUALITY_POLICY_VERSION,
        "files": sorted(str(file) for file in (args.files or [])),
        "route": args.route or "",
        "notes": args.notes or "",
        "verification_mode": getattr(args, "verification_mode", STANDARD_VERIFICATION_MODE),
        "required_checks": sorted(getattr(args, "required_checks", None) or []),
        "completion_actions": sorted(getattr(args, "completion_action", None) or []),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def issue_summary_line(issue: dict[str, Any]) -> str:
    return f"{issue.get('number') or issue.get('id')} / {issue.get('id') or ''} - {issue.get('title') or ''}".strip()


def required_verification_checks(
    config: dict[str, Any], issue: dict[str, Any], files: list[str], route: str
) -> list[str]:
    verification = config.get("verification") or {}
    suffixes = {Path(file).suffix.casefold() for file in files}
    code_suffixes = {".js", ".jsx", ".ts", ".tsx", ".vue", ".py", ".java", ".go"}
    style_suffixes = {".css", ".scss", ".sass", ".less", ".styl"}
    checks: list[str] = []
    if files and verification.get("format_check"):
        checks.append("format_check")
    if suffixes & code_suffixes and verification.get("lint"):
        checks.append("lint")
    if suffixes & style_suffixes and verification.get("stylelint"):
        checks.append("stylelint")
    if verification.get("test"):
        checks.append("test")
    if verification.get("build") and verification.get("run_build_by_default"):
        checks.append("build")
    browser = config.get("browser_verification") or {}
    required_terms = [str(value).casefold() for value in browser.get("required_for") or []]
    issue_text = combined_text(issue)
    visible_signal = bool(route) or any(term and term in issue_text for term in required_terms)
    if browser.get("enabled") and visible_signal:
        checks.append("browser")
    return list(dict.fromkeys(checks))


def configured_verification_steps(
    config: dict[str, Any], required_checks: list[str] | None = None
) -> list[str]:
    steps: list[str] = []
    verification = config.get("verification") or {}
    allowed = set(required_checks or [])
    for key in ("format_check", "lint", "stylelint", "test", "build"):
        if required_checks is not None and key not in allowed:
            continue
        value = verification.get(key)
        if value:
            steps.append(f"{key}: {value}")
    if required_checks is not None and "browser" in allowed:
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
    required_checks = getattr(args, "required_checks", None) or []
    verification_mode = getattr(args, "verification_mode", STANDARD_VERIFICATION_MODE)
    if verification_mode == DEFERRED_USER_VERIFICATION_MODE:
        verification = (
            "- AI 不运行修复验证、测试、构建或浏览器检查。\n"
            "- 修改可按已授权计划先提交，人工验证前保持远程工单原状态。\n"
            "- 等用户明确反馈验收结果后，以 `verified_by: user` 记录验证；通过后才执行 `start-fix`。"
        )
    else:
        verification = "\n".join(
            f"- {step}" for step in configured_verification_steps(config, required_checks)
        ) or "- 当前配置没有可执行的 Standard 检查；补充配置或改用获批的 lightweight 模式"
    approval = "已按计划指纹批准" if approved else "未批准"
    route = args.route or "待确认"
    notes = args.notes or "无"
    completion_actions = getattr(args, "completion_action", None) or []
    completion_lines = "\n".join(f"- {action}" for action in completion_actions) or "- 无"
    action_timing = (
        "先 commit；人工验证通过后再执行 start-fix；不自动 resolve-for-acceptance"
        if verification_mode == DEFERRED_USER_VERIFICATION_MODE
        else "先完成 AI 验证与 commit，再执行 start-fix；以修复中结束，不自动 resolve-for-acceptance"
    )
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
5. 按下面的验证模式执行；`deferred-to-user` 只记录待人工验证，不由 AI 运行验证。

## 验证计划

- 模式: {verification_mode}
{verification}

## 浏览器路线

- 路由/场景: {route}
- 登录策略: 按项目 `login_policy.method_priority`，不在聊天或工件中保存密码。

## 远程工单策略

- 本计划批准后允许连续执行的收尾动作：
{completion_lines}
- 执行时序: {action_timing}
- 只有动作在本计划中列出、项目配置允许且本地未禁止时，才可执行；未列出的动作仍需另行批准。

## 备注

{notes}
"""


def plan_fix(args: argparse.Namespace) -> int:
    config = load_runtime_config(args)
    root = artifact_root(config, args)
    repo_root = repository_root(config, args)
    target = issue_dir(root, args.issue)
    issue = load_issue(target)
    item = triage_issue_dir(config, target)
    planned_files = normalize_planned_files(args.files or [], repo_root)
    args.files = planned_files
    args.verification_mode = getattr(args, "verification_mode", STANDARD_VERIFICATION_MODE)
    args.required_checks = (
        []
        if args.verification_mode
        in (LIGHTWEIGHT_VERIFICATION_MODE, DEFERRED_USER_VERIFICATION_MODE)
        else required_verification_checks(config, issue, planned_files, args.route or "")
    )
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
            "user_verification_deferred": (
                recorded_verification_mode == DEFERRED_USER_VERIFICATION_MODE
            ),
            "required_checks": [] if planning_diagnostic else args.required_checks,
            "route": "" if planning_diagnostic else (args.route or ""),
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
        "user_verification_deferred": (
            recorded_verification_mode == DEFERRED_USER_VERIFICATION_MODE
        ),
        "required_checks": [] if planning_diagnostic else args.required_checks,
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
    config = load_runtime_config(args)
    root = artifact_root(config, args)
    repo_root = repository_root(config, args)
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
    normalized_files = ensure_files_inside_repo(files, repo_root) if not args.blocked else files
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
VERIFICATION_CHECK_PATTERN = re.compile(
    r"^\s*(?P<check>[a-z][a-z0-9_-]*)\s*=\s*(?P<result>passed|failed|blocked|skipped)"
    r"(?:\s*:\s*(?P<notes>.*))?\s*$",
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


def parsed_verification_checks(args: argparse.Namespace) -> list[dict[str, str]]:
    parsed: list[dict[str, str]] = []
    for value in getattr(args, "check", None) or []:
        match = VERIFICATION_CHECK_PATTERN.match(str(value))
        if not match:
            parsed.append(
                {
                    "check": str(value),
                    "result": "invalid",
                    "notes": "expected '<check>=<result>'",
                }
            )
            continue
        parsed.append(
            {
                "check": match.group("check").lower().replace("-", "_"),
                "result": match.group("result").lower(),
                "notes": (match.group("notes") or "").strip(),
            }
        )
    return parsed


def command_check_results(
    config: dict[str, Any], commands: list[dict[str, str]]
) -> dict[str, str]:
    results: dict[str, str] = {}
    verification = config.get("verification") or {}
    for item in commands:
        command = item.get("command") or ""
        for check in ("format_check", "lint", "stylelint", "test", "build"):
            template = str(verification.get(check) or "").strip()
            if not template:
                continue
            stable_prefix = template.split("<", 1)[0].strip()
            if command == template or (
                stable_prefix
                and (command == stable_prefix or command.startswith(f"{stable_prefix} "))
            ):
                results[check] = item.get("result") or "invalid"
                break
    return results


def render_verification(issue: dict[str, Any], args: argparse.Namespace) -> str:
    commands = parsed_verification_commands(args)
    checks = parsed_verification_checks(args)
    command_lines = (
        "\n".join(
            f"| {item['command']} | {item['result']} | {item['notes'] or '无'} |"
            for item in commands
        )
        or "| 未运行 | pending | 无 |"
    )
    evidence = "\n".join(f"- {item}" for item in (args.evidence or [])) or "- 无"
    check_lines = (
        "\n".join(
            f"| {item['check']} | {item['result']} | {item['notes'] or '无'} |"
            for item in checks
        )
        or "| 未记录 | pending | 无 |"
    )
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
- 验证来源: {getattr(args, 'verified_by', '') or '未声明'}
- 验证时间: {getattr(args, 'verified_at', '') or '未记录'}
- 来源说明: {getattr(args, 'verification_note', '') or '无'}

## 必需检查结果

| 检查 | 结果 | 说明 |
| --- | --- | --- |
{check_lines}

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


def verification_status(
    args: argparse.Namespace,
    required_checks: list[str] | None = None,
    check_results: dict[str, str] | None = None,
) -> str:
    mode = getattr(args, "mode", STANDARD_VERIFICATION_MODE)
    if args.blocked or args.failed or args.browser in ("failed", "blocked"):
        return "blocked"
    commands = parsed_verification_commands(args)
    results = [item["result"] for item in commands]
    explicit_checks = parsed_verification_checks(args)
    explicit_results = [item["result"] for item in explicit_checks]
    if any(
        result in ("failed", "blocked", "invalid")
        for result in [*results, *explicit_results]
    ):
        return "blocked"
    if mode == LIGHTWEIGHT_VERIFICATION_MODE:
        if getattr(args, "confidence", "") != "high":
            return "pending"
        if not str(getattr(args, "exemption_reason", "") or "").strip():
            return "pending"
        if not [item for item in (args.evidence or []) if str(item).strip()]:
            return "pending"
        return "done"
    if mode == DEFERRED_USER_VERIFICATION_MODE:
        if getattr(args, "verified_by", "") != "user":
            return "pending"
        passed_evidence = (
            any(item["result"] == "passed" for item in commands)
            or any(item["result"] == "passed" for item in explicit_checks)
            or args.browser == "passed"
            or bool([item for item in (args.evidence or []) if str(item).strip()])
        )
        return "done" if passed_evidence else "pending"
    if required_checks is not None:
        if not required_checks:
            return "pending"
        effective_results = dict(check_results or {})
        effective_results.update(
            {item["check"]: item["result"] for item in explicit_checks}
        )
        if "browser" in required_checks:
            effective_results["browser"] = args.browser
        if any(effective_results.get(check) != "passed" for check in required_checks):
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
        issue = load_issue(target)
        if not issue_evidence_state(issue)["complete"]:
            blockers.append("Inbound evidence is incomplete; lightweight verification is not permitted.")
        quality_state = issue_report_quality_state(issue)
        if not quality_state["complete"]:
            blockers.append(
                f"Report-quality is {quality_state['status']} or stale; lightweight verification is not permitted."
            )
        triage_metadata = frontmatter_metadata(artifact_path(target, "triage-report"))
        if triage_metadata.get("triage_policy_version") != REPORT_QUALITY_POLICY_VERSION:
            blockers.append("Triage uses an older report-quality policy and must be regenerated.")
        if triage_metadata.get("report_quality_hash_version") != REPORT_HASH_VERSION:
            blockers.append("Triage uses an older report-quality hash version and must be migrated/regenerated.")
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
    if triage_metadata.get("report_quality_complete") != "true":
        blockers.append("Issue information is not sufficient for implementation and acceptance; lightweight verification is not permitted.")
    if triage_metadata.get("report_quality_hash_version") != REPORT_HASH_VERSION:
        blockers.append("Report-quality hash version is stale; lightweight verification is not permitted.")
    return blockers


def record_verification(args: argparse.Namespace) -> int:
    config = load_runtime_config(args)
    root = artifact_root(config, args)
    target = issue_dir(root, args.issue)
    issue = load_issue(target)
    require_artifact_done(target, "implementation", "record verification")
    policy_blockers = lightweight_verification_blockers(config, target, args)
    if policy_blockers:
        existing_blocker = str(args.blocked or "").strip()
        args.blocked = "; ".join([*policy_blockers, *([existing_blocker] if existing_blocker else [])])
    plan_metadata = frontmatter_metadata(artifact_path(target, "fix-plan"))
    try:
        required_checks = json.loads(plan_metadata.get("required_checks") or "[]")
    except json.JSONDecodeError as exc:
        raise SystemExit("Fix-plan required_checks are malformed; regenerate the plan.") from exc
    if not isinstance(required_checks, list):
        raise SystemExit("Fix-plan required_checks must be a list; regenerate the plan.")
    planned_mode = plan_metadata.get("verification_mode") or STANDARD_VERIFICATION_MODE
    if planned_mode != getattr(args, "mode", STANDARD_VERIFICATION_MODE):
        raise SystemExit("Verification mode does not match the approved fix plan.")
    if (
        planned_mode == DEFERRED_USER_VERIFICATION_MODE
        and getattr(args, "verified_by", "") != "user"
    ):
        raise SystemExit(
            "Deferred-to-user verification requires direct human confirmation recorded with --verified-by user."
        )
    args.verified_at = utc_now_iso()
    if not str(getattr(args, "verified_by", "") or "").strip():
        raise SystemExit("record-verification requires --verified-by user|agent|ci.")
    commands = parsed_verification_commands(args)
    inferred_results = command_check_results(config, commands)
    status = verification_status(args, [str(item) for item in required_checks], inferred_results)
    explicit_checks = parsed_verification_checks(args)
    passed_checks = (
        sum(item["result"] == "passed" for item in commands)
        + sum(item["result"] == "passed" for item in explicit_checks)
        + int(args.browser == "passed")
    )
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
            "human_verified": (
                getattr(args, "mode", STANDARD_VERIFICATION_MODE)
                == DEFERRED_USER_VERIFICATION_MODE
                and args.verified_by == "user"
                and status == "done"
            ),
            "required_checks": required_checks,
            "verified_by": args.verified_by,
            "verified_at": args.verified_at,
            "verification_note": getattr(args, "verification_note", ""),
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
    config = load_runtime_config(args)
    root = artifact_root(config, args)
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


def run_git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def ensure_files_inside_repo(files: list[str], repo_root: Path) -> list[str]:
    cwd = repo_root.resolve()
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


def ensure_files_inside_cwd(files: list[str]) -> list[str]:
    return ensure_files_inside_repo(files, Path.cwd())


def normalize_planned_files(files: list[str], repo_root: Path | None = None) -> list[str]:
    """Normalize literal plan paths while allowing not-yet-created files."""

    cwd = (repo_root or Path.cwd()).resolve()
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
    config = load_runtime_config(args)
    repo_root = repository_root(config, args)
    if not args.files:
        raise SystemExit("commit-fix requires --files so only fix-related files are staged.")
    repository = run_git(["rev-parse", "--show-toplevel"], cwd=repo_root)
    if repository.returncode != 0:
        raise SystemExit(repository.stderr.strip() or repository.stdout.strip() or "commit-fix must run inside a git repository")
    if Path(repository.stdout.strip()).resolve() != repo_root:
        raise SystemExit("Configured repo root is not the Git repository root.")
    files = ensure_files_inside_repo(args.files, repo_root)

    root = artifact_root(config, args)
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
    implementation_metadata = frontmatter_metadata(artifact_path(target, "implementation"))
    planned_verification_mode = (
        plan_metadata.get("verification_mode") or STANDARD_VERIFICATION_MODE
    )
    verification_path = artifact_path(target, "verification")
    verification_pending = False
    verification_metadata: dict[str, str] = {}
    if planned_verification_mode == DEFERRED_USER_VERIFICATION_MODE:
        if not bool(
            config_value(
                config,
                "execution_policy.allow_deferred_user_verification",
                False,
            )
        ):
            raise SystemExit(
                "Project policy disables commit before deferred user verification."
            )
        if "execution_policy.allow_deferred_user_verification" in local_denies:
            raise SystemExit(
                "Local deny-only config disables commit before deferred user verification."
            )
        verification_state = artifact_effective_status(target, "verification")
        if verification_state == "done":
            verification_metadata = frontmatter_metadata(verification_path)
            if verification_metadata.get("human_verified") != "true":
                raise SystemExit(
                    "Deferred verification exists but lacks direct user confirmation."
                )
        elif verification_state == "pending":
            verification_pending = True
        else:
            raise SystemExit(
                "Deferred user verification is blocked or stale; record a valid human result before commit."
            )
    else:
        require_artifact_done(target, "verification", "commit fix")
        verification_metadata = frontmatter_metadata(verification_path)
    if int(implementation_metadata.get("summary_count") or 0) < 1 or int(implementation_metadata.get("file_count") or 0) < 1:
        raise SystemExit("Implementation evidence is empty; record a non-empty summary and changed-file list first.")
    try:
        implementation_files = json.loads(implementation_metadata.get("files") or "[]")
    except json.JSONDecodeError as exc:
        raise SystemExit("Implementation file evidence is malformed; record implementation again.") from exc
    if not isinstance(implementation_files, list) or set(str(item) for item in implementation_files) != set(files):
        raise SystemExit("commit-fix --files must exactly match the files recorded in the verified implementation.")
    verification_mode = (
        verification_metadata.get("verification_mode") or planned_verification_mode
    )
    if not verification_pending:
        evidence_count = int(verification_metadata.get("evidence_count") or 0)
        if verification_mode == LIGHTWEIGHT_VERIFICATION_MODE:
            if verification_metadata.get("lightweight_approved") != "true" or evidence_count < 1:
                raise SystemExit("Lightweight verification lacks an approved exception or inspection evidence.")
        elif verification_mode == DEFERRED_USER_VERIFICATION_MODE:
            if verification_metadata.get("human_verified") != "true" or evidence_count < 1:
                raise SystemExit("Deferred verification lacks direct user confirmation evidence.")
        elif int(verification_metadata.get("passed_checks") or 0) < 1 or evidence_count < 1:
            raise SystemExit("Verification has no structured passing evidence; record verification again.")

    staged_before = run_git(["diff", "--cached", "--name-only", "-z"], cwd=repo_root)
    if staged_before.returncode != 0:
        raise SystemExit(staged_before.stderr.strip() or staged_before.stdout.strip() or "git staged-file check failed")
    if staged_before.stdout:
        raise SystemExit("Refusing commit-fix because the git index already contains pre-staged work.")

    status = run_git(["--literal-pathspecs", "status", "--porcelain", "--", *files], cwd=repo_root)
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
                    "verification_mode": verification_mode,
                    "verification_pending": verification_pending,
                    "dry_run": True,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    add = run_git(["--literal-pathspecs", "add", "--", *files], cwd=repo_root)
    if add.returncode != 0:
        run_git(["reset", "--mixed", "--quiet", "HEAD"], cwd=repo_root)
        raise SystemExit(add.stderr.strip() or add.stdout.strip() or "git add failed")
    staged_after = run_git(["diff", "--cached", "--name-only", "-z"], cwd=repo_root)
    staged_files = {item for item in staged_after.stdout.split("\x00") if item}
    if staged_after.returncode != 0 or staged_files != set(files):
        run_git(["reset", "--mixed", "--quiet", "HEAD"], cwd=repo_root)
        detail = staged_after.stderr.strip() or f"staged paths differ: {sorted(staged_files)}"
        raise SystemExit(f"Refusing commit because exact-file staging could not be verified: {detail}")
    commit = run_git(["commit", "-m", message], cwd=repo_root)
    if commit.returncode != 0:
        run_git(["reset", "--mixed", "--quiet", "HEAD"], cwd=repo_root)
        raise SystemExit(commit.stderr.strip() or commit.stdout.strip() or "git commit failed")
    rev = run_git(["rev-parse", "--short", "HEAD"], cwd=repo_root)
    commit_hash = rev.stdout.strip() if rev.returncode == 0 else ""
    print(
        json.dumps(
            {
                "issue": args.issue,
                "message": message,
                "files": files,
                "commit": commit_hash,
                "pushed": False,
                "verification_mode": verification_mode,
                "verification_pending": verification_pending,
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


def path_relative_to_repo(path: Path, repo_root: Path) -> Path:
    resolved = path.resolve()
    try:
        return resolved.relative_to(repo_root.resolve())
    except ValueError:
        return resolved


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
    atomic_write_text(path, f"{existing}{prefix}{pattern}\n")
    return "added"


def build_project_template(args: argparse.Namespace) -> str:
    template_name = "feishu-project-config.template.yaml" if args.platform == "feishu-project" else "project-config.template.yaml"
    text = read_asset(template_name)
    project_name = args.project_name or cli_repo_root(args).name
    configured_artifact_root = args.artifact_root or args.root or ".bugflow/issues"
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
    text = re.sub(
        r"(?m)^(\s*root:)\s*.*$",
        rf"\1 {yaml_scalar(normalize_relative_path(Path(configured_artifact_root)))}",
        text,
        count=1,
    )
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
    configured_artifact_root = args.artifact_root or args.root or ".bugflow/issues"
    return re.sub(
        r"(?m)^(\s*root:)\s*.*$",
        rf"\1 {yaml_scalar(normalize_relative_path(Path(configured_artifact_root)))}",
        text,
        count=1,
    )


def init_project(args: argparse.Namespace) -> int:
    repo_root = cli_repo_root(args)
    config_path = argument_path(args, args.config)
    local_config_path = argument_path(args, args.local_config)
    schema_path = argument_path(args, args.schema)
    configured_artifact_root = args.artifact_root or args.root or ".bugflow/issues"
    root = Path(configured_artifact_root)
    if not root.is_absolute():
        root = repo_root / root

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
        relative_root = path_relative_to_repo(root, repo_root)
        pattern = top_ignore_pattern(relative_root)
        gitignore_path = repo_root / ".gitignore"
        action = append_gitignore_pattern(gitignore_path, pattern)
        results.append({"path": ".gitignore", "action": f"{action} {pattern}"})
        local_pattern = normalize_relative_path(path_relative_to_repo(local_config_path, repo_root))
        local_action = append_gitignore_pattern(gitignore_path, local_pattern)
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


def verified_remote_field_keys(config: dict[str, Any]) -> set[str]:
    values = config_value(config, "field_verification.verified_keys", []) or []
    if not isinstance(values, list):
        raise SystemExit("field_verification.verified_keys must be a list of remotely verified field keys.")
    return {str(value).strip() for value in values if str(value).strip()}


def build_feishu_mql(
    config: dict[str, Any], prefer_labels: bool = True, profile: str = "preview"
) -> dict[str, Any]:
    if profile not in {"preview", "fix-ready"}:
        raise SystemExit("MQL profile must be preview or fix-ready.")
    mapping = field_mapping(config)
    project_key = validate_mql_identifier(
        config_value(config, "issue_source.project_key", "PROJECT_KEY"),
        "issue_source.project_key",
    )
    work_item_type = validate_mql_identifier(
        config_value(config, "issue_source.work_item_type", "issue"),
        "issue_source.work_item_type",
    )
    core_names = ("id", "number", "title", "status", "assignee")
    optional_names = (
        ("priority", "updated_at", "requirements", "description")
        if profile == "preview"
        else (
            "priority",
            "reporter",
            "created_at",
            "updated_at",
            "requirements",
            "description",
            "attachments",
        )
    )
    verified_keys = verified_remote_field_keys(config)
    allow_unverified_optional = bool(
        config_value(config, "issue_source.allow_unverified_optional_fields", False)
    )
    selected_optional_names = [
        name
        for name in optional_names
        if mapping.get(name)
        and (allow_unverified_optional or mapping.get(name) in verified_keys)
    ]
    raw_select_fields = unique_values(
        [*(mapping.get(name) for name in core_names), *(mapping.get(name) for name in selected_optional_names)]
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
    raw_requirement_values = config_value(config, "query_policy.requirement_ids", []) or []
    if isinstance(raw_requirement_values, str):
        raw_requirement_values = [raw_requirement_values]
    if not isinstance(raw_requirement_values, list):
        raise SystemExit("query_policy.requirement_ids must be a list.")
    requirement_values = unique_values([str(value) for value in raw_requirement_values])
    requirement_field = mapping.get("requirements")
    requirement_pushdown = bool(
        config_value(config, "query_policy.requirement_mql_pushdown_verified", False)
    )
    requirement_clause = ""
    if requirement_values and requirement_pushdown:
        if not requirement_field or requirement_field not in verified_keys:
            raise SystemExit(
                "Requirement MQL pushdown requires a remotely verified field_mapping.requirements key."
            )
        requirement_identifier = validate_mql_identifier(requirement_field, "requirements field")
        expressions = [
            f"array_contains(`{requirement_identifier}`, {quote_sql_string(value)})"
            for value in requirement_values
        ]
        requirement_clause = f"  AND ({' OR '.join(expressions)})\n"
    mql = (
        f"SELECT {select_clause}\n"
        f"FROM `{project_key}`.`{work_item_type}`\n"
        f"WHERE {assignee_condition}\n"
        f"  AND `{status_field}` IN ({status_condition})\n"
        f"{requirement_clause}"
        f"ORDER BY {order_by}\n"
        f"LIMIT {limit}"
    )
    optional_configured_fields = unique_values([mapping.get(name) for name in optional_names])
    unverified_optional_fields = [
        field for field in optional_configured_fields if field not in verified_keys
    ]
    exact_field_config_keys = unique_values(
        [status_field, assignee_field, *optional_configured_fields]
    )
    return {
        "profile": profile,
        "project_key": project_key,
        "work_item_type": work_item_type,
        "select_fields": select_fields,
        "status_filter_values": status_values,
        "exact_field_config_keys": exact_field_config_keys,
        "verified_field_keys": sorted(verified_keys),
        "unverified_optional_fields": unverified_optional_fields,
        "requirement_filter_values": requirement_values,
        "requirement_filter_pushed_down": bool(requirement_clause),
        "requirement_post_filter_required": bool(requirement_values and not requirement_clause),
        "mql": mql,
    }


def feishu_mql(args: argparse.Namespace) -> int:
    config = load_runtime_config(args)
    if config_value(config, "issue_source.platform") != "feishu-project":
        raise SystemExit("feishu-mql requires issue_source.platform: feishu-project")
    cli_requirements = getattr(args, "requirement_id", None) or []
    if cli_requirements:
        set_config_value(config, "query_policy.requirement_ids", cli_requirements)
    result = build_feishu_mql(
        config,
        prefer_labels=not args.use_status_ids,
        profile=getattr(args, "profile", "preview"),
    )
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
    config_path = argument_path(args, args.config)
    local_config_path = argument_path(args, args.local_config) if args.local_config else None
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

    verified_fields = verified_remote_field_keys(config)
    verification_source = str(config_value(config, "field_verification.source", "") or "").strip()
    verified_at = str(config_value(config, "field_verification.verified_at", "") or "").strip()
    optional_fields = unique_values(
        [
            mapping.get("priority"),
            mapping.get("reporter"),
            mapping.get("created_at"),
            mapping.get("updated_at"),
            mapping.get("requirements"),
            mapping.get("description"),
            mapping.get("attachments"),
        ]
    )
    unverified_optional = [field for field in optional_fields if field not in verified_fields]
    if verified_fields and verification_source and verified_at:
        add(
            "ok",
            "remote-field-verification",
            f"{len(verified_fields)} keys verified at {verified_at} via {verification_source}",
        )
    else:
        add(
            "warn",
            "remote-field-verification",
            "local mappings are not proof of remote fields; record verified_keys/source/verified_at after exact field discovery",
        )
    if unverified_optional:
        add(
            "info",
            "preview-optional-fields",
            "excluded until remotely verified: " + ", ".join(unverified_optional),
        )

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

    root = artifact_root(config, args)
    if config_value(config, "bugflow.enabled", True):
        add("ok", "bugflow-root", normalize_relative_path(root))
    else:
        add("warn", "bugflow-root", "bugflow.enabled is false")

    schema = config_value(config, "bugflow.schema")
    if schema:
        schema_path = Path(schema)
        if not schema_path.is_absolute():
            schema_path = repository_root(config, args) / schema_path
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
        repo_root = repository_root(config, args)
        gitignore_path = repo_root / ".gitignore"
        relative_root = path_relative_to_repo(root, repo_root)
        if relative_root.is_absolute():
            add("warn", "artifact-git-policy", f"artifact root is outside repository: {root}")
            relative_root = Path(root.name)
        pattern = top_ignore_pattern(relative_root)
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
    parser.add_argument(
        "--repo-root",
        default="",
        help="Repository root used for config, code paths, and Git operations. Defaults to the current directory.",
    )
    parser.add_argument(
        "--artifact-root",
        default="",
        help="Per-issue artifact root. Relative paths resolve from --repo-root.",
    )
    parser.add_argument("--config", default=".codex/bugflow/issue-triage.project.yaml")
    parser.add_argument("--local-config", default=".codex/bugflow/issue-triage.local.yaml")
    parser.add_argument("--root", default="", help="Deprecated alias for --artifact-root.")
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

    preview_parser = subparsers.add_parser(
        "preview",
        aliases=["scan"],
        help="Quick assigned-issue triage without writing per-issue artifacts or requiring report-quality hashes.",
    )
    fetch_parser.add_argument(
        "--requirement-id",
        action="append",
        help="Import only issues linked to this requirement id/number/URL. Repeat as needed.",
    )
    preview_parser.add_argument("--input", help="Raw or normalized issue JSON file. Reads stdin when omitted.")
    preview_parser.add_argument("--platform", default="feishu-project")
    preview_parser.add_argument(
        "--assignee",
        action="append",
        help="Current-user assignee name/id for exported JSON. Repeat for aliases.",
    )
    preview_parser.add_argument(
        "--include-all-assignees",
        action="store_true",
        help="Explicitly preview issues assigned to other users as well.",
    )
    preview_parser.add_argument(
        "--requirement-id",
        action="append",
        help="Preview only issues linked to this requirement id/number/URL. Repeat as needed.",
    )
    preview_parser.add_argument("--report", help="Optional markdown preview output path.")
    preview_parser.add_argument("--json", action="store_true", help="Print machine-readable preview results.")
    preview_parser.set_defaults(func=preview)

    triage_parser = subparsers.add_parser("triage", help="Generate requirement-match and triage artifacts.")
    triage_parser.add_argument("--issue", help="Issue number/id. Defaults to all issue directories.")
    triage_parser.set_defaults(func=triage)

    quality_hash_parser = subparsers.add_parser(
        "report-quality-hash",
        help="Print the current evidence snapshot hash for a report_quality assessment.",
    )
    quality_hash_parser.add_argument("--issue", required=True, help="Issue number/id.")
    quality_hash_parser.set_defaults(func=report_quality_hash_command)

    migrate_parser = subparsers.add_parser(
        "migrate-artifacts",
        help="Apply safe metadata-only migrations; semantic changes still require re-triage.",
    )
    migrate_parser.add_argument("--issue", required=True, help="Issue number/id.")
    migrate_parser.set_defaults(func=migrate_artifacts)

    plan_parser = subparsers.add_parser("plan-fix", help="Create a controlled fix plan for one issue.")
    plan_parser.add_argument("--issue", required=True, help="Issue number/id.")
    plan_parser.add_argument(
        "--approved",
        metavar="PLAN_FINGERPRINT",
        default="",
        help=(
            "Record approval only for the exact fingerprint printed by an earlier plan-fix run, "
            "using a current direct/batch repair authorization or explicit plan approval."
        ),
    )
    plan_parser.add_argument("--files", nargs="*", default=[], help="Expected files or areas to inspect/change.")
    plan_parser.add_argument("--route", default="", help="Browser route or workflow to verify.")
    plan_parser.add_argument("--notes", default="", help="Additional planning notes.")
    plan_parser.add_argument(
        "--verification-mode",
        choices=[
            STANDARD_VERIFICATION_MODE,
            LIGHTWEIGHT_VERIFICATION_MODE,
            DEFERRED_USER_VERIFICATION_MODE,
        ],
        default=STANDARD_VERIFICATION_MODE,
        help=(
            "Use standard checks, plan-approved lightweight inspection, or defer all repair "
            "verification to the user in assisted mode."
        ),
    )
    plan_parser.add_argument(
        "--completion-action",
        action="append",
        choices=sorted(COMPLETION_ACTIONS),
        help="Action bound to this issue plan and its current run authorization. Repeat as needed.",
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
        choices=[
            STANDARD_VERIFICATION_MODE,
            LIGHTWEIGHT_VERIFICATION_MODE,
            DEFERRED_USER_VERIFICATION_MODE,
        ],
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
    verify_parser.add_argument(
        "--check",
        action="append",
        help="Named required check '<check>=passed|failed|blocked|skipped[: notes]'. Repeat as needed.",
    )
    verify_parser.add_argument(
        "--verified-by",
        choices=["user", "agent", "ci"],
        required=True,
        help="Who produced or directly confirmed this verification evidence.",
    )
    verify_parser.add_argument(
        "--verification-note",
        default="",
        help="Short provenance note, such as CI run id or user-confirmation context.",
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

    commit_parser = subparsers.add_parser(
        "commit-fix",
        help="Create one isolated local commit after autonomous verification or in approved assisted mode.",
    )
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

    daily_existing_parser = subparsers.add_parser(
        "daily-existing",
        help="Triage explicitly listed current issue artifacts and render a report without re-importing JSON.",
    )
    daily_parser.add_argument(
        "--requirement-id",
        action="append",
        help="Include only issues linked to this requirement id/number/URL. Repeat as needed.",
    )
    daily_existing_parser.add_argument(
        "--issue",
        action="append",
        required=True,
        help="Current-run issue number/id. Repeat for each issue; historical directories are never auto-scanned.",
    )
    daily_existing_parser.add_argument("--report", help="Optional markdown report output path.")
    daily_existing_parser.add_argument(
        "--assignee",
        action="append",
        help="Current-user assignee name/id. Repeat for aliases when native current-user identity is unavailable.",
    )
    daily_existing_parser.add_argument(
        "--include-all-assignees",
        action="store_true",
        help="Explicitly include listed artifacts assigned to other users.",
    )
    daily_existing_parser.add_argument(
        "--requirement-id",
        action="append",
        help="Include only listed artifacts linked to this requirement id/number/URL. Repeat as needed.",
    )
    daily_existing_parser.set_defaults(func=daily_existing)

    mql_parser = subparsers.add_parser("feishu-mql", help="Print a minimal Feishu Project MQL query from config.")
    mql_parser.add_argument(
        "--profile",
        choices=["preview", "fix-ready"],
        default="preview",
        help="Select the minimal preview field set or remotely verified fix-ready fields.",
    )
    mql_parser.add_argument(
        "--requirement-id",
        action="append",
        help="Declare an exact requirement scope. Repeat as needed.",
    )
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
