# Triage Issues

Use this reference for both fast provisional scanning and strict fix-ready classification. Keep the two modes visibly separate.

## Contents

- [Inputs](#inputs)
- [Mode Selection](#mode-selection)
- [Evidence Completeness](#evidence-completeness)
- [Report Quality](#report-quality)
- [Ownership](#ownership)
- [Effort](#effort)
- [Readiness](#readiness)
- [Risk](#risk)
- [Ranking](#ranking)
- [Triage Output](#triage-output)

## Inputs

- Preview: candidate/list JSON, current-user assignee filter, available descriptions/summaries, attachment metadata, and any cheaply available key evidence.
- Fix-ready: normalized issue JSON plus evidence intake containing full detail, all relevant inbound comments/activities, attachment inspection summaries, and explicit `evidence_fetch` completeness.
- Fix-ready only: report-quality assessment containing confirmed facts, evidence references, missing fields/conflicts, exact questions, feedback targets, and a safe local draft when allowed.
- Project config.
- Requirement-to-repository match result for strict fix-ready classification; preview may only state a provisional repository/owner hint.
- Repository docs such as `AGENTS.md`.
- Code search results normally belong to fix-ready. Preview may use at most one narrowly scoped exact `rg` query when it is likely to settle repository/frontend/backend ownership immediately; do not open an implementation investigation or scan the repo broadly.

## Mode Selection

- Use `preview/scan` when the user asks only to scan, triage, rank, or produce a daily overview. Return provisional ownership, risk, priority/order, evidence basis, and suspected information gaps. Do not create per-issue artifacts, bind report quality, inspect the implementation, run build/browser checks, or call the result final.
- Use `fix-ready` only for issue numbers the user selects for strict evaluation or repair. Then complete both evidence and report-quality gates, requirement/repository matching, and final classification before planning a fix.
- Preview cannot yield `auto-fix-candidate`, a repair fingerprint, lightweight verification eligibility, or a publishable clarification draft. Its closest recommendation is “建议进入严格评估”.
- A sparse preview is not automatically a defective bug report. Label missing items as “疑似缺口/升级后需核对”; only the bound fix-ready assessment may set `needs-clarification` or `conflicting`.

## Evidence Completeness

Run `evidence-intake.md` before final fix-ready classification. Use the title/list row and available summaries only for preview discovery.

- `evidence_fetch.status: complete` means every decision-relevant source is `complete` or genuinely `not-applicable`, and every decision-relevant attachment was inspected.
- `partial|error|unknown|skipped` means the classification is provisional. Keep readiness `ask-for-confirmation`, effort `blocked`, and list the exact missing comment page, activity window, attachment, video segment, access, or tool capability.
- Do not infer “no comments” from a missing comments field or empty list. Require `evidence_fetch.comments: complete|not-applicable`.
- Do not infer visual behavior from a filename or thumbnail. Image/video evidence counts only when its attachment record says `inspection_state: inspected` and includes a factual summary.
- Reading inbound comments is read-only. Posting comments is not part of triage and still requires completion-action authorization.

Newer comments or attachments may narrow or supersede a stale title. Preserve the conflict and cite the supporting comment id, attachment name/timestamp, or activity record instead of silently choosing one version.

## Report Quality

Run `report-quality.md` only in fix-ready, after evidence intake. These are separate gates: evidence completeness proves sources were read; report quality proves the combined content is implementable and independently testable. Preview does not calculate `report_quality.input_hash` and must not assign a strict quality status.

- `sufficient`: the combined description, comments, inspected media, activities, and authoritative requirements define the observable actual result, expected result, trigger/reproduction, and acceptance criteria. Environment, role, and safe test data are required only when relevant.
- `needs-clarification|conflicting|unknown`: keep readiness `ask-for-confirmation`, effort `blocked`, and do not create a repair plan or use lightweight verification. List the exact questions and generate a local unpublished feedback draft for test/product/owner.
- Do not require testers to specify code changes. A wrong implementation suggestion does not make a clear report insufficient; correct the ownership or reject the workaround during triage.
- Comments or inspected video can make an initially sparse report sufficient. Do not ask again for facts already evidenced.

User approval cannot substitute for a sufficient report. A conflict remains blocking until an authorized source resolves the acceptance boundary.

## Ownership

Classify exactly one:

- `frontend-owned`: UI layout, CSS, React/admin page behavior, routing, tables, forms, modals, drawers, upload/editor/preview behavior, mock data, or frontend API adapter mapping in the current repo.
- `backend-owned`: Backend query behavior, response ordering, database filtering/aggregation, server authorization, or an API contract that should be corrected at the source.
- `likely-frontend`: Screenshot or description points to UI behavior, but the exact component or data source still needs quick confirmation.
- `needs-confirmation`: Report quality is not sufficient, expected behavior is unclear, product copy/status semantics are ambiguous, reproduction is incomplete, requirement-to-repository ownership is ambiguous, or the fix could alter an API contract.
- `not-current-repo`: The fix belongs to another repo, mobile/native app, miniprogram, backend-only service, database/data cleanup, permission config, workflow config, deployment, or infrastructure.

Project config can add project-specific aliases, but keep these five buckets stable for workflow decisions.

Do not implement an unreasonable frontend workaround merely because the issue was assigned to the frontend. In particular, when a list endpoint is expected to return descending data, prefer a backend sort/order contract over reversing or re-sorting the response in each client. Classify it as `backend-owned` + `redirect-to-owner` unless the API contract explicitly delegates ordering to the client or product/backend approves a temporary frontend workaround.

If the expected sort field, direction, scope, and tie-breaker are clear, an incorrect request to implement frontend `reverse` is still report-quality `sufficient`; ownership and implementation feasibility are separate judgments.

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

Evidence completeness and `report_quality.status: sufficient` are prerequisites for `auto-fix-candidate`, any high-confidence fix plan, and lightweight verification. User approval cannot substitute for unread comments, unseen decision-relevant media, missing acceptance detail, or unresolved source conflicts.

Never enter repair from classification alone. Every code change still requires `fix_approved(issue)`; `execution_policy.auto_fix_allowed` defaults to `false`.

If requirement-to-repository matching is `unmatched`, `multi-repo-unclear`, or `low-confidence`, readiness must be `ask-for-confirmation` or `redirect-to-owner`.

## Risk

Classify exactly one:

- `low`: Isolated, reversible, and covered by targeted verification.
- `medium`: Shared component or user-visible workflow with manageable verification.
- `high`: Cross-system, permission, data, auth, payment, irreversible, or hard-to-verify behavior with uncertain ownership/behavior or a large blast radius.

Difficulty automating verification does not by itself make a fix high risk. A high-confidence, current-repo, clearly frontend-owned, reversible low/medium-risk fix may use plan-approved lightweight verification; see `fix-and-verify.md`.

## Ranking

In preview, recommend only which issues should enter strict evaluation first, using tracker priority, apparent impact/ownership, blocking dependencies, and recency. Prefix ownership/risk with “疑似/暂定”; never call an item safe to auto-fix.

In fix-ready, recommend execution order:

1. Evidence-backed `auto-fix-candidate` before other ready items, while leaving the user to select what to repair.
2. `easy` before `medium`.
3. Lower risk before higher risk.
4. Higher priority before lower priority.
5. More recently updated before older issues.

Do not hide blocked or unclear items. Put them in a separate "needs input" group with the exact missing information.

## Triage Output

### Preview output

Keep batch output compact and explicitly provisional:

```json
{
  "number": "BUG-88",
  "title": "Issue title",
  "priority": "P1",
  "ownership": "frontend-owned",
  "risk_hint": "medium",
  "recommendation": "建议优先进入严格评估",
  "information_hints": ["评论和截图内容尚未核对；进入 fix-ready 后确认验收参照。"],
  "provisional": true,
  "repair_allowed": false,
  "next_step": "fix-ready"
}
```

Treat `ownership` and `risk_hint` as internal provisional hints even when their values reuse strict enums; the user-facing table must label them “初步/暂定”. Do not include `feedback_draft`, `report_quality_status: sufficient`, `evidence_status: complete`, final repair readiness, or an implementation plan. If only metadata/thumbnail is available, say so directly. The preview report itself is the output; do not create per-issue directories.

### Fix-ready output

After complete evidence intake and bound report-quality assessment, produce for each selected issue:

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
  "report_quality_status": "sufficient",
  "report_quality_facts": [
    "The inspected recording and comment define trigger, actual, expected, and acceptance results."
  ],
  "report_quality_questions": [],
  "feedback_targets": [],
  "feedback_draft": "",
  "feedback_publish_status": "draft-only",
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

For `needs-clarification` or `conflicting`, show each known fact and source, each missing/conflicting item, the exact answerable question, its intended recipient, what work is blocked, and the ready-to-send local draft. Never imply that the draft was posted.
