# Triage Issues

Use this reference after issues are normalized and before making code or remote workflow changes.

## Inputs

- Normalized issue JSON.
- Evidence intake containing full detail, inbound comments, relevant activities, attachment inspection summaries, and explicit `evidence_fetch` completeness.
- Project config.
- Requirement-to-repository match result.
- Repository docs such as `AGENTS.md`.
- Code search results when needed to confirm ownership.

## Evidence Completeness

Run `evidence-intake.md` before final classification. Use the title/list row only to find candidates.

- `evidence_fetch.status: complete` means every decision-relevant source is `complete` or genuinely `not-applicable`, and every decision-relevant attachment was inspected.
- `partial|error|unknown|skipped` means the classification is provisional. Keep readiness `ask-for-confirmation`, effort `blocked`, and list the exact missing comment page, activity window, attachment, video segment, access, or tool capability.
- Do not infer “no comments” from a missing comments field or empty list. Require `evidence_fetch.comments: complete|not-applicable`.
- Do not infer visual behavior from a filename or thumbnail. Image/video evidence counts only when its attachment record says `inspection_state: inspected` and includes a factual summary.
- Reading inbound comments is read-only. Posting comments is not part of triage and still requires completion-action authorization.

Newer comments or attachments may narrow or supersede a stale title. Preserve the conflict and cite the supporting comment id, attachment name/timestamp, or activity record instead of silently choosing one version.

## Ownership

Classify exactly one:

- `frontend-owned`: UI layout, CSS, React/admin page behavior, routing, tables, forms, modals, drawers, upload/editor/preview behavior, mock data, or frontend API adapter mapping in the current repo.
- `backend-owned`: Backend query behavior, response ordering, database filtering/aggregation, server authorization, or an API contract that should be corrected at the source.
- `likely-frontend`: Screenshot or description points to UI behavior, but the exact component or data source still needs quick confirmation.
- `needs-confirmation`: Expected behavior is unclear, product copy/status semantics are ambiguous, reproduction is incomplete, requirement-to-repository ownership is ambiguous, or the fix could alter an API contract.
- `not-current-repo`: The fix belongs to another repo, mobile/native app, miniprogram, backend-only service, database/data cleanup, permission config, workflow config, deployment, or infrastructure.

Project config can add project-specific aliases, but keep these five buckets stable for workflow decisions.

Do not implement an unreasonable frontend workaround merely because the issue was assigned to the frontend. In particular, when a list endpoint is expected to return descending data, prefer a backend sort/order contract over reversing or re-sorting the response in each client. Classify it as `backend-owned` + `redirect-to-owner` unless the API contract explicitly delegates ordering to the client or product/backend approves a temporary frontend workaround.

## Effort

Classify exactly one:

- `easy`: Clear reproduction, one page/component/style/API mapping, low blast radius, obvious verification path.
- `medium`: Touches shared components, cross-page behavior, editor/upload/table internals, permissions, or needs both code and browser verification.
- `hard`: Broad workflow, unclear repro, cross-system dependency, backend contract change, migration, or high regression risk.
- `blocked`: Missing repo, credentials, environment, product decision, test data, or local verification path.

## Readiness

Classify exactly one:

- `auto-fix-candidate`: Evidence supports a narrowly scoped low-risk plan; this is a recommendation, not approval to edit.
- `manual-review-first`: Likely fixable, but the plan or blast radius should be reviewed first.
- `ask-for-confirmation`: Needs product, test, backend, or user confirmation.
- `redirect-to-owner`: Not owned by the current repo/team.

Default a current-repo issue to `manual-review-first`. Upgrade it to `auto-fix-candidate` only when all of these are evidenced: a complete reproduction/expected result, confident current-repo ownership, an isolated pure style/layout/local display defect, no shared-component or API/product/status semantics, and a concrete targeted plus browser verification path.

Evidence completeness is a prerequisite for `auto-fix-candidate`, any high-confidence fix plan, and lightweight verification. User approval cannot substitute for unread comments or unseen decision-relevant media.

Never enter repair from classification alone. Every code change still requires `fix_approved(issue)`; `execution_policy.auto_fix_allowed` defaults to `false`.

If requirement-to-repository matching is `unmatched`, `multi-repo-unclear`, or `low-confidence`, readiness must be `ask-for-confirmation` or `redirect-to-owner`.

## Risk

Classify exactly one:

- `low`: Isolated, reversible, and covered by targeted verification.
- `medium`: Shared component or user-visible workflow with manageable verification.
- `high`: Cross-system, permission, data, auth, payment, irreversible, or hard-to-verify behavior with uncertain ownership/behavior or a large blast radius.

Difficulty automating verification does not by itself make a fix high risk. A high-confidence, current-repo, clearly frontend-owned, reversible low/medium-risk fix may use plan-approved lightweight verification; see `fix-and-verify.md`.

## Ranking

Recommend execution order:

1. Evidence-backed `auto-fix-candidate` before other ready items, while leaving the user to select what to repair.
2. `easy` before `medium`.
3. Lower risk before higher risk.
4. Higher priority before lower priority.
5. More recently updated before older issues.

Do not hide blocked or unclear items. Put them in a separate "needs input" group with the exact missing information.

## Triage Output

For each issue, produce:

```json
{
  "id": "123456",
  "number": "BUG-88",
  "title": "Issue title",
  "ownership": "frontend-owned",
  "effort": "easy",
  "readiness": "auto-fix-candidate",
  "risk": "low",
  "repository_match": "current-repo",
  "evidence_status": "complete",
  "evidence_findings": [
    "Attachment repro.mp4 at 00:08 shows the first frame missing after reopening."
  ],
  "requirement": {
    "id": "REQ-1",
    "title": "Requirement title"
  },
  "recommended_order": 1,
  "reason": "The issue maps to a single table style in the current repo.",
  "missing_information": [],
  "customer_confirmation_question": ""
}
```

Keep reasons concrete and tied to issue evidence, code search, or project config.
