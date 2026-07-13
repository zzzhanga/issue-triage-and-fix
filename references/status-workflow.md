# Status Workflow

Use this reference before changing a remote issue status.

## Contents

- [Defaults](#defaults)
- [Authorization Predicate](#authorization-predicate)
- [Report-Quality Clarification Comments](#report-quality-clarification-comments)
- [Config Fields](#config-fields)
- [Error Recovery And Idempotency](#error-recovery-and-idempotency)
- [Start Fix](#start-fix)
- [Resolve For Acceptance](#resolve-for-acceptance)
- [Complete Or Terminate](#complete-or-terminate)
- [Mark Blocked](#mark-blocked)
- [Transition Summary](#transition-summary)

## Defaults

- Do not post repair/closure comments or change remote status unless `completion_action_authorized(issue, action)` is true. Report-quality clarification comments use the separate exact-draft rules below.
- Treat code repair, local commit, comment, start-fix, resolve, complete, reopen, and terminate as distinct actions, but allow one exact fix-plan approval to cover several actions when every action is visibly listed in `completion_actions`.
- In the Feishu starter, enable status updates plus the normal `start_fix` and `resolve_for_acceptance` capabilities. Keep comments, complete, and terminate disabled. This permits an approved repair workflow; it does not authorize or automatically execute a transition.
- Keep every remote capability false for exported-JSON/non-native tracker starters.
- Never transition blocked, unclear, or cross-owner issues as if they were repaired.
- Treat a report-quality clarification draft as local-only by default. Do not post it merely because comments were read, a repair plan was approved, or comments are enabled.
- `close-local` only writes `closure.md`; it is not a remote status update.

## Authorization Predicate

`completion_action_authorized(issue, action)` is true only when all of these are true:

1. The user approves the exact fix plan and the action is explicitly listed in that plan's `completion_actions`. An action not listed in the approved plan requires a new plan/approval.
2. Repo-scoped project config enables status updates or comments as appropriate and enables the specific default action. The Feishu starter enables only `start_fix` and `resolve_for_acceptance` status actions.
3. A local deny-only override does not set the capability or action to `false`.
4. For status changes, the named transition exists, its source matches, and its target status id/code was verified from tracker field config.
5. Any transition-specific confirmation and verification prerequisites are satisfied.

Configuration alone is not user approval. A local `true` cannot override a project `false`, and plan approval cannot silently persist into later tasks. After approval, do not ask again between implementation, plan-approved verification, commit, and the listed normal status transitions.

## Report-Quality Clarification Comments

When `report_quality` is `needs-clarification` or `conflicting`, generate a local draft that states confirmed facts, cites evidence, lists missing/conflicting acceptance details, asks exact questions, names the intended tester/product/owner, and says what is blocked. Mark it not published.

Posting that draft is a separate external write, not a repair completion action. Permit it only when all are true:

1. The user explicitly authorizes the exact draft in the current task.
2. `remote_status_policy.update_comments_allowed` is true after deny-only overrides.
3. The target issue and audience are verified.
4. The draft contains no credentials, signed URLs, private media, secret test data, or unnecessary personal information.

Do not reuse `fix_approved(issue)`, `--approved`, a repair `plan_fingerprint`, or a plan-listed completion comment as this authorization. A modified draft requires fresh exact-draft authorization. The local runner does not post comments; use a configured native tracker capability only after these checks. Scheduled triage never posts clarification drafts.

## Config Fields

Read:

- `remote_status_policy.update_status_allowed`
- `remote_status_policy.default_change_to_in_progress`
- `remote_status_policy.default_resolve_for_acceptance`
- `remote_status_policy.default_complete`
- `remote_status_policy.default_terminate`
- `statuses`
- `status_transitions`

For comments, also read `remote_status_policy.update_comments_allowed`. This capability gate does not create exact-draft authorization. For any status change, require `remote_status_policy.update_status_allowed`. Treat missing values as `false`.

Common Feishu status labels are `待修复`, `修复中`, `已解决，待验收`, `重新打开`, `已完成`, and `已终止`. Prefer stable ids from field config; labels alone are not enough for API updates.

## Error Recovery And Idempotency

If a remote transition returns an error, timeout, or ambiguous response:

1. Re-read the issue's current status before deciding to retry.
2. If it already equals the authorized target, record idempotent success and do not retry.
3. If it still equals the verified source state, retry the exact same transition at most once.
4. If it changed to any other state, stop immediately and report the concurrent/divergent state; do not force or chain another transition.

Never infer failure only from the write response, and never loop retries. Include the re-read status and retry result in the transition summary.

## Start Fix

Allowed only when:

- `fix_approved(issue)` is true,
- plan action `start-fix` is authorized and maps to transition `start_fix`,
- transition `start_fix` exists,
- the verified source and target states match the transition.

If allowed, update from open/pending to in-progress before code edits.

## Resolve For Acceptance

Allowed only when:

- standard verification passed, or the approved lightweight verification artifact is `done`,
- browser verification passed when required and not validly exempted by lightweight mode,
- transition `resolve_for_acceptance` exists,
- the transition target is `已解决，待验收` or equivalent,
- plan action `resolve-for-acceptance` is authorized and maps to transition `resolve_for_acceptance`.

Legacy configs may use `default_change_to_fixed`; treat it as an alias for `default_resolve_for_acceptance`.

If verification is partial, leave the issue in progress and comment with remaining risk.

## Complete Or Terminate

Move to `已完成` only after acceptance is confirmed by the configured owner and plan action `complete` is authorized.

Move to `已终止` only when the issue is cancelled/invalid and plan action `terminate` is authorized.

Handle `重新打开` as a valid incoming state for rework; do not treat it as a fixed state.

## Mark Blocked

Use blocked status only when:

- config defines a blocked transition,
- the blocker is concrete,
- the exact blocked transition is listed in a newly approved plan/action.

Otherwise, leave a comment or final report without changing status.

## Transition Summary

Every remote status update summary must include:

- issue id/number
- old status
- new status
- reason
- the project/local policy gates and the current-task approval that authorized it
