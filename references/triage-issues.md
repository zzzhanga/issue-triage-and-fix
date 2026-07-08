# Triage Issues

Use this reference after issues are normalized and before making code or remote workflow changes.

## Inputs

- Normalized issue JSON.
- Project config.
- Requirement-to-repository match result.
- Repository docs such as `AGENTS.md`.
- Code search results when needed to confirm ownership.

## Ownership

Classify exactly one:

- `frontend-owned`: UI layout, CSS, React/admin page behavior, routing, tables, forms, modals, drawers, upload/editor/preview behavior, mock data, or frontend API adapter mapping in the current repo.
- `likely-frontend`: Screenshot or description points to UI behavior, but the exact component or data source still needs quick confirmation.
- `needs-confirmation`: Expected behavior is unclear, product copy/status semantics are ambiguous, reproduction is incomplete, requirement-to-repository ownership is ambiguous, or the fix could alter an API contract.
- `not-current-repo`: The fix belongs to another repo, mobile/native app, miniprogram, backend-only service, database/data cleanup, permission config, workflow config, deployment, or infrastructure.

Project config can add project-specific aliases, but keep these four buckets stable for workflow decisions.

## Effort

Classify exactly one:

- `easy`: Clear reproduction, one page/component/style/API mapping, low blast radius, obvious verification path.
- `medium`: Touches shared components, cross-page behavior, editor/upload/table internals, permissions, or needs both code and browser verification.
- `hard`: Broad workflow, unclear repro, cross-system dependency, backend contract change, migration, or high regression risk.
- `blocked`: Missing repo, credentials, environment, product decision, test data, or local verification path.

## Readiness

Classify exactly one:

- `auto-fix-candidate`: Safe to fix now under project policy.
- `manual-review-first`: Likely fixable, but the plan or blast radius should be reviewed first.
- `ask-for-confirmation`: Needs product, test, backend, or user confirmation.
- `redirect-to-owner`: Not owned by the current repo/team.

Only `auto-fix-candidate` items should enter repair automatically.

If requirement-to-repository matching is `unmatched`, `multi-repo-unclear`, or `low-confidence`, readiness must be `ask-for-confirmation` or `redirect-to-owner`.

## Risk

Classify exactly one:

- `low`: Isolated, reversible, and covered by targeted verification.
- `medium`: Shared component or user-visible workflow with manageable verification.
- `high`: Cross-system, permission, data, auth, payment, irreversible, or hard-to-verify behavior.

## Ranking

Recommend execution order:

1. `auto-fix-candidate` before all others.
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
