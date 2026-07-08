#!/usr/bin/env python3
"""Bugflow v1 runner：导入工单、生成分诊工件、输出每日分诊报告。

这个 runner 故意不修改代码、不修改远程工单状态。它的职责是把飞书/MCP/导出的
工单 JSON 转成可恢复的 bugflow 工件，并用项目配置做需求-仓库匹配和初步分诊。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from bugflow_artifacts import ARTIFACTS, artifact_template, issue_dir, write_if_absent
from normalize_issue_payload import normalize_issue


SKILL_ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = SKILL_ROOT / "assets"


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - 环境错误提示
        raise SystemExit("Missing PyYAML. Run with Codex bundled Python or install PyYAML.") from exc
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


def load_config(config_path: Path, local_config_path: Path | None) -> dict[str, Any]:
    config = load_yaml(config_path)
    if local_config_path and local_config_path.exists():
        config = deep_merge(config, load_yaml(local_config_path))
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
        return json.loads(Path(path).read_text(encoding="utf-8"))
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
        raise SystemExit(f"Issue missing number/id: {issue}")
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


def write_markdown_artifact(path: Path, artifact_id: str, status: str, body: str) -> None:
    path.write_text(f"---\nartifact: {artifact_id}\nstatus: {status}\n---\n\n{body}", encoding="utf-8")


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
    (target / "issue.json").write_text(json.dumps(issue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
    return "\n".join(parts).lower()


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


def classify_issue(config: dict[str, Any], issue: dict[str, Any], match: dict[str, Any]) -> dict[str, Any]:
    title_desc = combined_text(issue)
    match_state = match["repository_match"]
    if match_state == "current-repo":
        ownership = config_value(config, "project.role_assumption", "current-repo")
        if ownership == "frontend":
            ownership = "frontend-owned"
        effort = "easy"
        risk = "low"
        readiness = "auto-fix-candidate"
        reason = "需求与当前仓库匹配，工单描述暂无明显跨系统风险。"
        if any(word in title_desc for word in ("权限", "登录", "接口", "api", "数据库", "支付", "迁移")):
            effort = "medium"
            risk = "medium"
            readiness = "manual-review-first"
            reason = "当前仓库可能相关，但描述包含接口/权限/数据等较高风险信号。"
        if any(word in title_desc for word in ("定时", "发布日期", "发布时间", "发布日", "schedule")):
            effort = "medium"
            risk = "medium"
            readiness = "manual-review-first"
            reason = "当前仓库可能相关，但定时发布/发布日期通常依赖产品语义或后端字段约定，需先人工评审。"
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

    return {
        "ownership": ownership,
        "effort": effort,
        "readiness": readiness,
        "risk": risk,
        "reason": reason,
        "recommended_order": None,
        "missing_information": [match["customer_confirmation_question"]] if match.get("confirmation_required") else [],
    }


def render_triage(issue: dict[str, Any], match: dict[str, Any], triage: dict[str, Any]) -> str:
    missing = "\n".join(f"- {item}" for item in triage.get("missing_information") or []) or "- 无"
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

## 理由

{triage['reason']}

## 缺失信息

{missing}
"""


def import_json_issues(args: argparse.Namespace) -> list[dict[str, Any]]:
    config = load_config(Path(args.config), Path(args.local_config) if args.local_config else None)
    platform = config_value(config, "issue_source.platform", args.platform)
    root = Path(config_value(config, "bugflow.root", args.root))
    payload = load_json_payload(args.input)
    items = iter_payload_items(payload)
    normalized = [normalize_issue(item, platform, field_mapping(config)) for item in items]
    for issue in normalized:
        write_issue_json(root, issue)
    return normalized


def fetch_json(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config), Path(args.local_config) if args.local_config else None)
    root = Path(config_value(config, "bugflow.root", args.root))
    normalized = import_json_issues(args)
    directories = [str(issue_dir(root, issue_key(issue))) for issue in normalized]
    result = {"count": len(normalized), "directories": directories}
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
            ["缺陷", "标题", "优先级", "状态", "提出/更新", "报告人/负责人", "推荐"],
            [
                [
                    "<br>".join(str(part) for part in (item.get("id"), item.get("issue")) if part),
                    item.get("title") or "",
                    item.get("priority") or "",
                    item.get("status") or "",
                    item.get("date") or "",
                    " / ".join(part for part in (item.get("reporter"), item.get("assignee")) if part),
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
        f"{item['issue']}：{triage_display_label(item['repository_match'])} / "
        f"{triage_display_label(item['ownership'])} / {triage_display_label(item['readiness'])}"
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
    imported = import_json_issues(args)
    config = load_config(Path(args.config), Path(args.local_config) if args.local_config else None)
    root = Path(config_value(config, "bugflow.root", args.root))
    results = [triage_issue_dir(config, issue_dir(root, issue_key(issue))) for issue in imported]
    report = render_daily_markdown(results)
    if args.report:
        Path(args.report).write_text(report, encoding="utf-8")
    print(report)
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
    return text


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
            "action": write_text_file(local_config_path, read_asset("local-overrides.template.yaml"), args.force),
        }
    )
    results.append(
        {
            "path": normalize_relative_path(schema_path),
            "action": write_text_file(schema_path, read_asset("bugflow-schema.template.yaml"), args.force),
        }
    )

    if not args.skip_gitignore:
        pattern = top_ignore_pattern(root)
        action = append_gitignore_pattern(Path(".gitignore"), pattern)
        results.append({"path": ".gitignore", "action": f"{action} {pattern}"})

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
    return "'" + value.replace("'", "\\'") + "'"


def format_order_by(order_by: str) -> str:
    parts = order_by.strip().split()
    if len(parts) in (1, 2) and parts[0].replace("_", "").isalnum():
        direction = parts[1].upper() if len(parts) == 2 else "DESC"
        if direction in ("ASC", "DESC"):
            return f"`{parts[0]}` {direction}"
    return order_by


def build_feishu_mql(config: dict[str, Any], prefer_labels: bool = True) -> dict[str, Any]:
    mapping = field_mapping(config)
    project_key = config_value(config, "issue_source.project_key", "PROJECT_KEY")
    work_item_type = config_value(config, "issue_source.work_item_type", "issue")
    select_fields = unique_values(
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
    status_field = mapping.get("status") or "work_item_status"
    assignee_field = mapping.get("assignee") or "current_status_operator"
    assigned_to = str(config_value(config, "query_policy.assigned_to", "current_login_user()"))
    status_values = feishu_status_values(config, prefer_labels)
    order_by = format_order_by(str(config_value(config, "query_policy.order_by", f"{mapping.get('updated_at') or 'updated_at'} desc")))
    limit = int(config_value(config, "query_policy.limit", 20) or 20)
    select_clause = ", ".join(f"`{field}`" for field in select_fields) or "`work_item_id`, `name`, `work_item_status`"
    if assigned_to == "current_login_user()":
        assignee_condition = f"array_contains(`{assignee_field}`, current_login_user())"
    else:
        assignee_condition = f"array_contains(`{assignee_field}`, {quote_sql_string(assigned_to)})"
    status_condition = ", ".join(quote_sql_string(value) for value in status_values)
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
    if platform and project_key and work_item_type:
        add("ok", "issue-source", f"{platform} {project_key}.{work_item_type}")
    else:
        add("error", "issue-source", "missing issue_source.platform/project_key/work_item_type")

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
    if status_codes:
        add("ok", "status-codes", ", ".join(status_codes))
    if missing_status_codes:
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
        except Exception as exc:  # pragma: no cover - defensive diagnostics
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
    fetch_parser.set_defaults(func=fetch_json)

    triage_parser = subparsers.add_parser("triage", help="Generate requirement-match and triage artifacts.")
    triage_parser.add_argument("--issue", help="Issue number/id. Defaults to all issue directories.")
    triage_parser.set_defaults(func=triage)

    daily_parser = subparsers.add_parser("daily", help="Import JSON, triage, and print a daily report.")
    daily_parser.add_argument("--input", help="Raw or normalized issue JSON file. Reads stdin when omitted.")
    daily_parser.add_argument("--platform", default="feishu-project")
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
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
