#!/usr/bin/env python3
"""Initialize and inspect bugflow issue artifact directories."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any


ARTIFACTS = [
    {
        "id": "issue-intake",
        "file": "issue.json",
        "requires": [],
        "template": None,
    },
    {
        "id": "requirement-match",
        "file": "requirement-match.md",
        "requires": ["issue-intake"],
        "template": """# Requirement Match

## Linked Requirement

- ID:
- Title:
- URL:

## Candidate Repositories

| Repo | Match reason | Confidence |
| --- | --- | --- |

## Decision

- Repository match:
- Confidence:
- Confirmation required:

## Customer/Product Confirmation Question

""",
    },
    {
        "id": "triage-report",
        "file": "triage.md",
        "requires": ["requirement-match"],
        "template": """# Triage Report

## Classification

- Ownership:
- Effort:
- Readiness:
- Risk:

## Reasoning

## Missing Information

""",
    },
    {
        "id": "fix-plan",
        "file": "fix-plan.md",
        "requires": ["triage-report"],
        "template": """# Fix Plan

## Scope

## Files To Inspect

## Planned Changes

## Verification Plan

## Risks

""",
    },
    {
        "id": "implementation",
        "file": "implementation.md",
        "requires": ["fix-plan"],
        "template": """# Implementation

## Remote Status Changes

## Files Changed

## Notes

## Deviations From Plan

""",
    },
    {
        "id": "verification",
        "file": "verification.md",
        "requires": ["implementation"],
        "template": """# Verification

## Commands

| Command | Result | Notes |
| --- | --- | --- |

## Browser Checks

| Route | Actions | Result | Evidence |
| --- | --- | --- | --- |

## Remaining Risk

""",
    },
    {
        "id": "closure",
        "file": "closure.md",
        "requires": ["verification"],
        "template": """# Closure

## Remote Comment

## Final Status Decision

## Residual Risk

## Follow-Up

""",
    },
]


WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def issue_slug(issue: str) -> str:
    """Return a readable, collision-resistant directory name for an issue key."""

    original = str(issue).strip()
    if not original or original in {".", ".."}:
        raise SystemExit("Issue id/number is required and cannot be '.' or '..'.")
    normalized = unicodedata.normalize("NFKC", original)
    reserved_stem = normalized.rstrip(" .").split(".", 1)[0].upper()
    if reserved_stem in WINDOWS_RESERVED_NAMES:
        raise SystemExit(f"Issue id/number uses a reserved Windows name: {reserved_stem}")
    readable = re.sub(r"[^\w.-]+", "-", normalized, flags=re.UNICODE).strip(" .-_")
    if not readable or readable in {".", ".."}:
        readable = "issue"
    if readable == original and len(readable) <= 80:
        return readable
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:10]
    return f"{readable[:80]}--{digest}"


def issue_dir(root: Path, issue: str) -> Path:
    root = Path(root)
    target = root / issue_slug(issue)
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    if not resolved_target.is_relative_to(resolved_root):
        raise SystemExit(f"Refusing issue directory outside configured root: {issue!r}")
    return target


def frontmatter_status(path: Path) -> str:
    if not path.exists():
        return "missing"
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return "pending"
    lines = text.splitlines()
    for line in lines[1:20]:
        if line.strip() == "---":
            break
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip() or "pending"
    return "pending"


def frontmatter_metadata(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    metadata: dict[str, str] = {}
    for line in text.splitlines()[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip()
    return metadata


def replace_frontmatter(path: Path, updates: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    body = text
    metadata = frontmatter_metadata(path)
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            body = parts[2].lstrip("\r\n")
    metadata.update(updates)
    artifact_id = metadata.pop("artifact", "")
    status = metadata.pop("status", "pending")
    lines = ["---"]
    if artifact_id:
        lines.append(f"artifact: {artifact_id}")
    lines.append(f"status: {status}")
    lines.extend(f"{key}: {value}" for key, value in metadata.items() if value != "")
    lines.extend(["---", "", body])
    path.write_text("\n".join(lines), encoding="utf-8")


def artifact_definition(artifact_id: str) -> dict[str, Any]:
    for artifact in ARTIFACTS:
        if artifact["id"] == artifact_id:
            return artifact
    raise KeyError(artifact_id)


def artifact_file_hash(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return "missing"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dependency_fingerprint(issue_root: Path, artifact_id: str) -> str:
    artifact = artifact_definition(artifact_id)
    dependencies: list[dict[str, str]] = []
    for dependency_id in artifact["requires"]:
        dependency = artifact_definition(dependency_id)
        dependency_path = issue_root / dependency["file"]
        dependencies.append(
            {
                "artifact": dependency_id,
                "file": dependency["file"],
                "sha256": artifact_file_hash(dependency_path),
            }
        )
    payload = json.dumps(dependencies, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def downstream_artifact_ids(artifact_id: str) -> list[str]:
    downstream: list[str] = []
    reached = {artifact_id}
    for artifact in ARTIFACTS:
        if any(dependency in reached for dependency in artifact["requires"]):
            reached.add(artifact["id"])
            downstream.append(artifact["id"])
    return downstream


def invalidate_downstream(issue_root: Path, artifact_id: str) -> list[str]:
    invalidated: list[str] = []
    for downstream_id in downstream_artifact_ids(artifact_id):
        artifact = artifact_definition(downstream_id)
        path = issue_root / artifact["file"]
        if not path.exists() or artifact["file"] == "issue.json":
            continue
        replace_frontmatter(
            path,
            {
                "status": "pending",
                "dependency_hash": "",
                "invalidated_by": artifact_id,
            },
        )
        invalidated.append(downstream_id)
    return invalidated


def effective_artifact_status(issue_root: Path, artifact_id: str) -> str:
    artifact = artifact_definition(artifact_id)
    path = issue_root / artifact["file"]
    if artifact["file"] == "issue.json":
        return "done" if issue_json_done(path) else "missing"
    explicit = frontmatter_status(path)
    if explicit != "done":
        return explicit
    for dependency_id in artifact["requires"]:
        if effective_artifact_status(issue_root, dependency_id) != "done":
            return "stale"
    metadata = frontmatter_metadata(path)
    expected = dependency_fingerprint(issue_root, artifact_id)
    if metadata.get("dependency_hash") != expected:
        return "stale"
    return "done"


def write_if_absent(path: Path, content: str) -> bool:
    if path.exists():
        return False
    path.write_text(content, encoding="utf-8")
    return True


def artifact_template(artifact: dict[str, Any]) -> str:
    body = artifact["template"] or ""
    return f"---\nartifact: {artifact['id']}\nstatus: pending\n---\n\n{body}"


def init_artifacts(args: argparse.Namespace) -> int:
    root = Path(args.root)
    target = issue_dir(root, args.issue)
    target.mkdir(parents=True, exist_ok=True)

    issue_json = {
        "source": args.source,
        "id": args.issue,
        "number": args.issue,
        "title": args.title or "",
        "status": args.status or "",
        "requirements": [],
        "attachments": [],
        "comments": [],
        "activities": [],
        "evidence_fetch": {
            "status": "unknown",
            "detail": "unknown",
            "comments": "unknown",
            "activities": "unknown",
            "media": "unknown",
            "fetched_at": None,
            "findings": [],
            "missing": ["Evidence intake has not been completed."],
        },
        "raw": {},
    }

    created: list[str] = []
    for artifact in ARTIFACTS:
        path = target / artifact["file"]
        if artifact["file"] == "issue.json":
            if write_if_absent(path, json.dumps(issue_json, ensure_ascii=False, indent=2) + "\n"):
                created.append(artifact["file"])
        elif write_if_absent(path, artifact_template(artifact)):
            created.append(artifact["file"])

    result = {
        "issue": args.issue,
        "directory": str(target),
        "created": created,
        "existing": [artifact["file"] for artifact in ARTIFACTS if (target / artifact["file"]).exists()],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def issue_json_done(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return bool(data.get("id") or data.get("number"))


def status_artifacts(args: argparse.Namespace) -> int:
    target = issue_dir(Path(args.root), args.issue)
    done: set[str] = set()
    raw_items: list[dict[str, Any]] = []

    for artifact in ARTIFACTS:
        path = target / artifact["file"]
        if artifact["file"] == "issue.json":
            artifact_done = issue_json_done(path)
            explicit_status = "done" if artifact_done else "missing"
        else:
            explicit_status = frontmatter_status(path)
            artifact_done = effective_artifact_status(target, artifact["id"]) == "done"
        if artifact_done:
            done.add(artifact["id"])
        raw_items.append(
            {
                "id": artifact["id"],
                "file": artifact["file"],
                "path": str(path),
                "requires": artifact["requires"],
                "explicit_status": explicit_status,
                "exists": path.exists(),
                "done": artifact_done,
            }
        )

    items: list[dict[str, Any]] = []
    for item in raw_items:
        missing_deps = [dep for dep in item["requires"] if dep not in done]
        effective_status = effective_artifact_status(target, item["id"])
        if item["done"]:
            state = "done"
        elif effective_status in ("stale", "partial"):
            state = effective_status
        elif item["explicit_status"] == "blocked":
            state = "blocked"
        elif missing_deps:
            state = "blocked"
        else:
            state = "ready"
        item["state"] = state
        item["missing_dependencies"] = missing_deps
        items.append(item)

    result = {"issue": args.issue, "directory": str(target), "artifacts": items}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for item in items:
            deps = f" missing deps: {', '.join(item['missing_dependencies'])}" if item["missing_dependencies"] else ""
            print(f"{item['state']:7} {item['id']:24} {item['file']}{deps}")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create an issue artifact directory.")
    init_parser.add_argument("--root", default=".bugflow/issues")
    init_parser.add_argument("--issue", required=True)
    init_parser.add_argument("--title", default="")
    init_parser.add_argument("--status", default="")
    init_parser.add_argument("--source", default="feishu-project")
    init_parser.set_defaults(func=init_artifacts)

    status_parser = subparsers.add_parser("status", help="Show artifact readiness.")
    status_parser.add_argument("--root", default=".bugflow/issues")
    status_parser.add_argument("--issue", required=True)
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(func=status_artifacts)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
