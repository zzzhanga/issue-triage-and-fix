# Bugflow Artifacts

Use this reference when the issue workflow should be resumable, reviewable, or auditable.

## Core Idea

Represent each bug as a small work directory with explicit artifacts. Actions can happen in a flexible order, but each artifact records what is known and what is blocked.

Default path:

```text
.bugflow/issues/<issue-number>/
```

Default artifacts:

| Artifact | File | Purpose |
| --- | --- | --- |
| `issue-intake` | `issue.json` | Normalized tracker issue, including requirements and raw source fields. |
| `requirement-match` | `requirement-match.md` | Requirement-to-repository match, confidence, candidate repos, and customer confirmation question. |
| `triage-report` | `triage.md` | Ownership, effort, readiness, risk, and recommended order. |
| `fix-plan` | `fix-plan.md` | Scoped implementation plan and verification plan before code edits. |
| `implementation` | `implementation.md` | Files changed, key decisions, remote status changes, and notes while editing. |
| `verification` | `verification.md` | Commands run, browser checks, screenshots/evidence, and failures. |
| `closure` | `closure.md` | Comment text, final status decision, residual risk, and follow-up. |

## Status Model

Each Markdown artifact has a small frontmatter block:

```yaml
---
artifact: triage-report
status: pending
---
```

Allowed statuses:

- `pending`: scaffolded but not filled.
- `done`: complete enough for downstream work.
- `blocked`: cannot proceed without a concrete input.

The issue JSON counts as done when it exists and contains at least an id or number.

Artifact readiness:

- `done`: artifact itself is done.
- `ready`: artifact is not done and all dependencies are done.
- `blocked`: one or more dependencies are not done, or status is explicitly `blocked`.

## Actions

- `fetch`: create or refresh `issue.json`.
- `match`: create or update `requirement-match.md`.
- `triage`: create or update `triage.md`.
- `plan`: create or update `fix-plan.md`.
- `apply`: edit code and update `implementation.md`.
- `verify`: run configured validation and update `verification.md`.
- `close`: comment/status update and update `closure.md`.
- `update`: revise any earlier artifact when new information changes the story.
- `status`: show ready, blocked, and done artifacts.

Actions are not phases. If implementation reveals the repository match was wrong, update `requirement-match.md` and then continue from the newly ready artifact.

## Dependency Graph

Default dependency chain:

```text
issue-intake
  -> requirement-match
  -> triage-report
  -> fix-plan
  -> implementation
  -> verification
  -> closure
```

Dependencies are enablers, not hard gates. A user can explicitly override an action, but the agent must call out missing upstream evidence.

## Safety Rules

- Do not store passwords, tokens, cookies, MCP URLs, or session secrets in artifacts.
- Keep screenshots local unless the repo policy allows committing them.
- Do not mark `closure.md` done until verification and remote workflow decisions are recorded.
- If customer/product confirmation is needed, mark `requirement-match.md` or `triage.md` as `blocked` and include the exact question.

## Script

Use `scripts/bugflow_artifacts.py` to initialize and inspect issue work directories:

```powershell
python C:\Users\zhanghang\.codex\skills\issue-triage-and-fix\scripts\bugflow_artifacts.py init --root .bugflow/issues --issue BUG-28814 --title "Image display bug"
python C:\Users\zhanghang\.codex\skills\issue-triage-and-fix\scripts\bugflow_artifacts.py status --root .bugflow/issues --issue BUG-28814 --json
```
