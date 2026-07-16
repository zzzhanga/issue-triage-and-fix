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

Use two storage levels. A batch `preview/scan` is ephemeral: it may write one aggregate preview report, but it must not create one work directory per candidate. Only a user-selected issue entering `fix-ready` becomes a small work directory with explicit artifacts. Actions can happen in a flexible order, but each strict artifact records what is known and what is blocked.

Default path:

```text
.bugflow/issues/<safe-issue-key>/
```

Recommended layout:

| Path | Purpose | Git policy |
| --- | --- | --- |
| `.codex/bugflow/` | Local workflow config, schema, and user-local overrides. | Usually ignored with `.codex/`. |
| `.bugflow/` | Generated run artifacts such as daily reports, issue intake, triage, fix plans, verification, and closure notes. | Add to the project `.gitignore` by default. |

Use `--repo-root` for config/code/Git resolution and `--artifact-root` only for the per-issue storage root. Relative artifact/report paths resolve from the repository root, not an arbitrary process CWD. Configure aggregate reports under `bugflow.report_root` (default recommendation `.bugflow/reports`). A `preview --report` path must be Markdown inside that report root and cannot overwrite issue artifacts, `.codex`, `.git`, or code/config paths.

Keep generated artifacts outside `.codex/bugflow/` so configuration does not mix with daily run output, and so the workflow can remain readable for non-Codex agents or scripts. Commit `.bugflow/` only when the team intentionally wants bug evidence and repair notes reviewed in git.

### Preview Is Not An Artifact Chain

Use `bugflow_runner.py preview` for “只分诊/扫描/日报”. It standardizes and filters in memory, produces provisional ownership/risk/priority and suspected information gaps, and may write `.bugflow/reports/daily-preview.md`. It does not create `.bugflow/issues/<issue>/`, `issue.json`, report-quality hashes, requirement matches, triage reports, plans, or downstream artifacts. It also does not inspect code, run build/browser checks, or mutate Git/remote state.

Treat preview output as disposable discovery data. Do not infer strict `evidence_fetch` or `report_quality` status from it, and do not publish clarification drafts based on unverified preview data. Upgrade only the issue numbers the user selects to fix-ready.

Fix-ready artifacts:

| Artifact | File | Purpose |
| --- | --- | --- |
| `issue-intake` | `issue.json` | Normalized tracker issue plus sanitized comments, activities, attachment inspection summaries, independent `evidence_fetch`/`report_quality` gates, and clarification data; raw source fields are redacted unless explicitly retained. |
| `requirement-match` | `requirement-match.md` | Requirement-to-repository match, confidence, candidate repos, and customer confirmation question. |
| `triage-report` | `triage.md` | Evidence/report quality, ownership, effort, readiness, risk, recommended order, and any local unpublished feedback draft. |
| `fix-plan` | `fix-plan.md` | Scoped implementation plan, verification mode, and approved completion actions before code edits; hard evidence/ownership/risk blockers produce only a planning diagnostic with no fingerprint or implementation steps. |
| `implementation` | `implementation.md` | Files changed, key decisions, remote status changes, and notes while editing. |
| `verification` | `verification.md` | Standard checks, plan-approved lightweight inspection, or direct user results for a `deferred-to-user` assisted plan; includes provenance, residual risk, and failures. |
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

Runner-owned JSON/Markdown output is written by same-directory temporary file plus atomic replace. New output includes `artifact_schema_version` and `runner_revision`; report-quality bindings also include `hash_version`. Do not hand-copy a `done` marker across versions.

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

This dependency chain begins only after a selected issue enters fix-ready. Use dependencies as hard gates for plan approval, implementation, verification, closure, and remote actions. Autonomous commits also require verification. The only commit exception is an approved `deferred-to-user` assisted plan with project permission; record it as verification pending and keep the remote issue unchanged. User approval does not make a missing or stale upstream artifact complete; regenerate it first.

When an upstream artifact changes materially, invalidate every descendant. The runner performs this check when refreshed normalized issue content changes; for manually edited Markdown artifacts, explicitly reset downstream status before continuing:

- A changed `issue.json` invalidates requirement match through closure. New/edited comments, attachments, media summaries, activities, evidence completeness, report-quality facts/status/questions, or acceptance details are material changes.
- A changed repository match invalidates triage through closure.
- A changed triage result invalidates fix plan through closure.
- A changed fix plan invalidates implementation through closure.
- A changed implementation invalidates verification and closure.
- A report-quality hash-version change invalidates strict triage and every downstream artifact even when the evidence input hash text is unchanged.

Mark invalidated Markdown artifacts `pending` (or regenerate them). A stale `verification.md` must not satisfy commit or closure gates. Do not infer currentness from timestamps or file existence alone.

For a metadata-only compatible upgrade, run:

```powershell
python <skill-dir>\scripts\bugflow_runner.py migrate-artifacts --issue BUG-28814
```

The migrator may add the current hash/schema metadata only when the stored assessment still matches the current evidence snapshot. It invalidates downstream work and requires re-triage. If the evidence hash differs, the existing hash version is unknown, or workflow semantics changed, migration fails closed and the issue must be reassessed.

## Safety Rules

- Do not store passwords, tokens, cookies, MCP URLs, or session secrets in artifacts.
- Redact raw tracker payloads by default. `--retain-raw` preserves the original payload and may preserve secrets; use it only after explicit user request, keep the output in ignored local storage, and never copy it into comments, logs, git, or the final response.
- Keep screenshots local unless the repo policy allows committing them.
- Keep downloaded evidence under `.bugflow/issues/<safe-issue-key>/evidence/` (or another ignored local directory). Store only safe relative paths, hashes, inspection state, and summaries in `issue.json`; never store temporary download `sign`, signed URLs, or authorization headers.
- Do not mark evidence complete after seeing only an attachment name, thumbnail, or video cover frame. If decision-relevant media cannot be inspected, keep triage blocked/provisional.
- Do not treat `evidence_fetch.status: complete` as proof that the report is sufficient. Require `report_quality.status: sufficient`; otherwise keep repair and all three verification modes blocked and include exact clarification questions.
- Preview never claims either gate is complete. Suspected missing information in preview is an internal “upgrade and inspect” note, not a bound report-quality verdict or ready-to-publish feedback draft.
- When evidence, report quality, repository ownership, confirmation, effort, or risk has a hard blocker, `plan-fix` may record a blocked diagnostic in `fix-plan.md` for auditability, but it must not emit an approvable fingerprint, implementation steps, verification plan, or completion actions. This diagnostic is not a repair plan.
- Do not mark `closure.md` done until verification and remote workflow decisions are recorded.
- If tester/product/owner confirmation is needed, mark `requirement-match.md` or `triage.md` as `blocked`, include the exact question and local feedback draft, and mark that draft not published. Posting requires separate authorization under `status-workflow.md`.

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
python <skill-dir>\scripts\bugflow_runner.py feishu-mql --profile preview --json
python <skill-dir>\scripts\bugflow_runner.py preview --input exported-issues.json --assignee <current-user-name-or-id> --report .bugflow/reports/daily-preview.md
python <skill-dir>\scripts\bugflow_runner.py fetch-json --input selected-issue.json --assignee <current-user-name-or-id>
python <skill-dir>\scripts\bugflow_runner.py report-quality-hash --issue BUG-28814
python <skill-dir>\scripts\bugflow_runner.py triage --issue BUG-28814
python <skill-dir>\scripts\bugflow_runner.py daily-existing --issue BUG-28814 --assignee <current-user-name-or-id> --report .bugflow/reports/daily-report.md
python <skill-dir>\scripts\bugflow_runner.py plan-fix --issue BUG-28814 --files src/file.ts --completion-action commit --completion-action start-fix
python <skill-dir>\scripts\bugflow_runner.py plan-fix --issue BUG-28814 --files src/file.ts --completion-action commit --completion-action start-fix --approved <plan_fingerprint>
python <skill-dir>\scripts\bugflow_runner.py record-implementation --issue BUG-28814 --summary "..." --files src/file.ts
python <skill-dir>\scripts\bugflow_runner.py record-verification --issue BUG-28814 --verified-by agent --check "lint=passed: pnpm exec eslint src/file.ts"
python <skill-dir>\scripts\bugflow_runner.py commit-fix --issue BUG-28814 --files src/file.ts --authorized <plan_fingerprint>
python <skill-dir>\scripts\bugflow_runner.py close-local --issue BUG-28814 --summary "Fixed locally and verified"
```

The runner creates starter config with `init-project`, checks local setup with `doctor`, generates Feishu MQL from config with `feishu-mql`, and filters input to the current assignee by default. `preview` performs an in-memory scan without issue artifacts. For selected fix-ready issues, the runner creates or updates artifacts, keeps evidence completeness separate from report sufficiency, performs deterministic requirement-to-repository matching, records standard/lightweight/direct-user verification, and can create one isolated local commit. Autonomous commits require verification; assisted `deferred-to-user` commits may precede it only under the explicit policy gate. `daily-existing` renders a report only from explicitly listed, already assessed issue artifacts; use it after strict evaluation so a second `daily --input` import cannot overwrite enriched evidence/report-quality data. The runner does not edit code, publish clarification drafts, push, or update remote issue status.
