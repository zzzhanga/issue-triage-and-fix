#!/usr/bin/env python3
"""Initialize and inspect bugflow issue artifact directories."""

from __future__ import annotations

import argparse
import json
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


def issue_dir(root: Path, issue: str) -> Path:
    safe_issue = issue.strip().replace("/", "-").replace("\\", "-")
    if not safe_issue:
        raise SystemExit("Issue id/number is required.")
    return root / safe_issue


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
            artifact_done = explicit_status == "done"
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
        if item["done"]:
            state = "done"
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
