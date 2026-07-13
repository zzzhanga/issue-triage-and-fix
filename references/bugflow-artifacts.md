# Bugflow Artifacts

Use this reference when the issue workflow should be resumable, reviewable, or auditable.

## Contents

- [Core Idea](#core-idea)
- [Status Model](#status-model)
- [Actions](#actions)
- [Dependency And Invalidation Rules](#dependency-and-invalidation-rules)
- [Safety Rules](#safety-rules)
- [Commands](#commands)

## Core Idea

Represent each bug as a small work directory with explicit artifacts. Actions can happen in a flexible order, but each artifact records what is known and what is blocked.

Default path:

```text
.bugflow/issues/<safe-issue-key>/
```

Recommended layout:

| Path | Purpose | Git policy |
| --- | --- | --- |
| `.codex/bugflow/` | Local workflow config, schema, and user-local overrides. | Usually ignored with `.codex/`. |
| `.bugflow/` | Generated run artifacts such as daily reports, issue intake, triage, fix plans, verification, and closure notes. | Add to the project `.gitignore` by default. |

Keep generated artifacts outside `.codex/bugflow/` so configuration does not mix with daily run output, and so the workflow can remain readable for non-Codex agents or scripts. Commit `.bugflow/` only when the team intentionally wants bug evidence and repair notes reviewed in git.

Default artifacts:

| Artifact | File | Purpose |
| --- | --- | --- |
| `issue-intake` | `issue.json` | Normalized tracker issue plus sanitized comments, activities, attachment inspection summaries, and `evidence_fetch` completeness; raw source fields are redacted unless explicitly retained. |
| `requirement-match` | `requirement-match.md` | Requirement-to-repository match, confidence, candidate repos, and customer confirmation question. |
| `triage-report` | `triage.md` | Ownership, effort, readiness, risk, and recommended order. |
| `fix-plan` | `fix-plan.md` | Scoped implementation plan, verification mode, and approved completion actions before code edits; hard evidence/ownership/risk blockers produce only a planning diagnostic with no fingerprint or implementation steps. |
| `implementation` | `implementation.md` | Files changed, key decisions, remote status changes, and notes while editing. |
| `verification` | `verification.md` | Standard checks or plan-approved lightweight inspection evidence, browser results, residual risk, and failures. |
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

The issue JSON counts as done only when it contains a safe issue key (`id` or `number`) and the normalized payload passes validation. File existence alone is not completion.

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

## Dependency And Invalidation Rules

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

Use dependencies as hard gates for plan approval, implementation, verification, commit, closure, and remote actions. User approval does not make a missing or stale upstream artifact complete; regenerate it first.

When an upstream artifact changes materially, invalidate every descendant. The runner performs this check when refreshed normalized issue content changes; for manually edited Markdown artifacts, explicitly reset downstream status before continuing:

- A changed `issue.json` invalidates requirement match through closure. New/edited comments, attachments, media summaries, activities, or evidence completeness are material changes.
- A changed repository match invalidates triage through closure.
- A changed triage result invalidates fix plan through closure.
- A changed fix plan invalidates implementation through closure.
- A changed implementation invalidates verification and closure.

Mark invalidated Markdown artifacts `pending` (or regenerate them). A stale `verification.md` must not satisfy commit or closure gates. Do not infer currentness from timestamps or file existence alone.

## Safety Rules

- Do not store passwords, tokens, cookies, MCP URLs, or session secrets in artifacts.
- Redact raw tracker payloads by default. `--retain-raw` preserves the original payload and may preserve secrets; use it only after explicit user request, keep the output in ignored local storage, and never copy it into comments, logs, git, or the final response.
- Keep screenshots local unless the repo policy allows committing them.
- Keep downloaded evidence under `.bugflow/issues/<safe-issue-key>/evidence/` (or another ignored local directory). Store only safe relative paths, hashes, inspection state, and summaries in `issue.json`; never store temporary download `sign`, signed URLs, or authorization headers.
- Do not mark evidence complete after seeing only an attachment name, thumbnail, or video cover frame. If decision-relevant media cannot be inspected, keep triage blocked/provisional.
- When evidence, repository ownership, confirmation, effort, or risk has a hard blocker, `plan-fix` may record a blocked diagnostic in `fix-plan.md` for auditability, but it must not emit an approvable fingerprint, implementation steps, verification plan, or completion actions. This diagnostic is not a repair plan.
- Do not mark `closure.md` done until verification and remote workflow decisions are recorded.
- If customer/product confirmation is needed, mark `requirement-match.md` or `triage.md` as `blocked` and include the exact question.

## Commands

Use the same Python 3 interpreter that passed dependency setup. Replace `python` below with that interpreter. Use `scripts/bugflow_artifacts.py` to initialize and inspect issue work directories:

```powershell
python <skill-dir>\scripts\bugflow_artifacts.py init --root .bugflow/issues --issue BUG-28814 --title "Image display bug"
python <skill-dir>\scripts\bugflow_artifacts.py status --root .bugflow/issues --issue BUG-28814 --json
```

Use `scripts/bugflow_runner.py` for setup and daily triage automation:

```powershell
python <skill-dir>\scripts\bugflow_runner.py init-project --platform feishu-project --project-name my-project --project-key my-feishu-project-key
python <skill-dir>\scripts\bugflow_runner.py doctor
python <skill-dir>\scripts\bugflow_runner.py feishu-mql --json
python <skill-dir>\scripts\bugflow_runner.py fetch-json --input exported-issues.json --assignee <current-user-name-or-id>
python <skill-dir>\scripts\bugflow_runner.py triage
python <skill-dir>\scripts\bugflow_runner.py daily --input exported-issues.json --assignee <current-user-name-or-id> --report .bugflow/daily-report.md
python <skill-dir>\scripts\bugflow_runner.py plan-fix --issue BUG-28814 --files src/file.ts --completion-action commit --completion-action resolve-for-acceptance
python <skill-dir>\scripts\bugflow_runner.py plan-fix --issue BUG-28814 --files src/file.ts --completion-action commit --completion-action resolve-for-acceptance --approved <plan_fingerprint>
python <skill-dir>\scripts\bugflow_runner.py record-implementation --issue BUG-28814 --summary "..." --files src/file.ts
python <skill-dir>\scripts\bugflow_runner.py record-verification --issue BUG-28814 --command "pnpm exec eslint src/file.ts => passed"
python <skill-dir>\scripts\bugflow_runner.py commit-fix --issue BUG-28814 --files src/file.ts --authorized <plan_fingerprint>
python <skill-dir>\scripts\bugflow_runner.py close-local --issue BUG-28814 --summary "Fixed locally and verified"
```

The runner creates starter config with `init-project`, checks local setup with `doctor`, generates Feishu MQL from config with `feishu-mql`, filters imported JSON to the current assignee by default, creates or updates artifacts, performs deterministic requirement-to-repository matching, records plan-approved standard/lightweight verification, and can create one local commit after verification and Git isolation checks. It does not edit code, push, or update remote issue status.
