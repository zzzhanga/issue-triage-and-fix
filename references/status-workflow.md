# Status Workflow

Use this reference before changing a remote issue status.

## Defaults

- Do not change remote status unless project config or the user allows it.
- Prefer changing to in-progress before a controlled repair when allowed.
- Require explicit permission or config for resolved, completed, or terminated transitions.
- Never transition blocked, unclear, or cross-owner issues as if they were repaired.
- `close-local` only writes `closure.md`; it is not a remote status update.

## Config Fields

Read:

- `remote_status_policy.update_status_allowed`
- `remote_status_policy.default_change_to_in_progress`
- `remote_status_policy.default_resolve_for_acceptance`
- `remote_status_policy.default_complete`
- `remote_status_policy.default_terminate`
- `statuses`
- `status_transitions`

Common Feishu status labels are `待修复`, `修复中`, `已解决，待验收`, `重新打开`, `已完成`, and `已终止`. Prefer stable ids from field config; labels alone are not enough for API updates.

## Start Fix

Allowed only when:

- issue is selected for repair,
- readiness is `auto-fix-candidate` or the user explicitly selected it,
- transition `start_fix` exists,
- `require_confirmation` is false or the user approved it.

If allowed, update from open/pending to in-progress before code edits.

## Resolve For Acceptance

Allowed only when:

- local verification passed,
- browser verification passed when required,
- transition `resolve_for_acceptance` exists,
- the transition target is `已解决，待验收` or equivalent,
- `default_resolve_for_acceptance` is true or the user approved it.

Legacy configs may use `default_change_to_fixed`; treat it as an alias for `default_resolve_for_acceptance`.

If verification is partial, leave the issue in progress and comment with remaining risk.

## Complete Or Terminate

Move to `已完成` only after acceptance is confirmed by the configured owner or the user explicitly requests it.

Move to `已终止` only when the issue is cancelled/invalid and the configured policy or user explicitly allows it.

Handle `重新打开` as a valid incoming state for rework; do not treat it as a fixed state.

## Mark Blocked

Use blocked status only when:

- config defines a blocked transition,
- the blocker is concrete,
- the user approves the transition or config allows it.

Otherwise, leave a comment or final report without changing status.

## Transition Summary

Every remote status update summary must include:

- issue id/number
- old status
- new status
- reason
- whether the transition was config-driven or user-approved
