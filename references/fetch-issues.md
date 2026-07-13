# Fetch Issues

Use this reference when retrieving issues from a tracker or from an exported JSON file.

## Contents

- [Goals](#goals)
- [Access Order](#access-order)
- [Query Policy](#query-policy)
- [Standard Issue Shape](#standard-issue-shape)
- [Raw Payload Policy](#raw-payload-policy)
- [Detail Fetching](#detail-fetching)
- [Output](#output)

## Goals

- Fetch only actionable issues first and filter to the current assignee by default.
- For preview/scan, retrieve only list fields, available summaries, and any cheap key evidence needed for provisional ownership/risk/priority/ranking; run `bugflow_runner.py preview` without per-issue artifacts or strict gates.
- For a user-selected fix-ready issue, retrieve enough detail for final triage: title, description, reproduction steps/trigger conditions, expected and actual results, acceptance criteria, priority, assignee, status, update time, requirement links, all relevant inbound comments/activities, and the inspected content of decision-relevant screenshots/recordings/attachments. Retrieve environment, account role, and safe test data only when behavior depends on them.
- Normalize all sources to a shared JSON shape before classification.
- Assess `report_quality` only in fix-ready from all normalized evidence; do not equate successful retrieval with sufficient implementation/acceptance detail.

## Access Order

1. For Feishu Project, use the configured MCP or a project-approved OpenAPI adapter.
2. For every other tracker, use a local JSON export supplied by the user or produced by a project-approved tool.
3. Use browser automation only for one-off read-only extraction when the user authorizes interactive login; save the result as JSON before normalization.

Do not imply native Jira, TAPD, 禅道, GitLab Issues, or other tracker adapters. A shared normalized JSON shape is not a remote integration.

Never ask for a password. Keep API tokens and user keys in environment variables or secret storage, not in skill files or repositories.

## Query Policy

Read the project config:

- `issue_source.platform`
- `issue_source.project_key`
- `issue_source.work_item_type`
- `field_mapping`
- `requirement_mapping`
- `query_policy`
- `remote_status_policy`

Default to fetching:

- assigned to the current user or configured assignee
- in the configured open/pending status
- ordered by updated time descending
- limited to the configured count

Apply the assignee rule to imported JSON as well as native queries:

- Feishu native MQL uses `current_login_user()` before export.
- For exported JSON, configure `query_policy.assigned_to` and optional `assignee_aliases`, or pass one or more `--assignee` values.
- `fetch-json` and `daily` skip issues whose normalized `assignee` does not match the current-user values and report the skipped count.
- If the current assignee cannot be resolved, stop instead of importing everyone. Use `--include-all-assignees` only when the user explicitly asks to inspect other owners' issues.

If any configured field fails, fetch the tracker field schema before retrying. Use field keys or ids, not display labels, whenever the platform supports stable identifiers.

For Feishu Project, run `bugflow_runner.py feishu-mql --json` after `doctor` passes. Use the generated MQL for routine fetches, and use the returned `exact_field_config_keys` only when a field error requires schema confirmation.

## Standard Issue Shape

Normalize each issue to:

```json
{
  "source": "feishu-project",
  "source_url": "https://project.example/...",
  "id": "123456",
  "number": "BUG-88",
  "title": "Issue title",
  "status": "OPEN",
  "priority": "P1",
  "assignee": "current_user",
  "description": "Full issue description",
  "reproduction_steps": "Open the saved draft, then reopen it from the list.",
  "actual_result": "The first video frame is blank.",
  "expected_result": "The first video frame remains visible.",
  "environment": "Test environment, Chrome 126",
  "test_data": "Draft BUG-88; no credentials or secrets",
  "acceptance_criteria": "The first frame is visible after save and reopen.",
  "implementation_suggestion": "Optional reporter suggestion; not an acceptance requirement",
  "requirements": [
    {
      "id": "REQ-1",
      "title": "Requirement title",
      "url": "https://project.example/requirement/REQ-1"
    }
  ],
  "attachments": [],
  "comments": [],
  "activities": [],
  "evidence_fetch": {
    "status": "complete",
    "detail": "complete",
    "comments": "complete",
    "activities": "complete",
    "media": "complete",
    "fetched_at": "2026-07-07T10:05:00+08:00",
    "findings": [],
    "missing": []
  },
  "report_quality": {
    "status": "sufficient",
    "assessed_at": "2026-07-07T10:10:00+08:00",
    "input_hash": "<hash from report-quality-hash>",
    "facts": ["The inspected recording and comment define trigger, actual, and expected results."],
    "evidence_refs": ["attachment repro.mp4@00:08", "comment comment-7"],
    "missing_fields": [],
    "conflicts": [],
    "questions": [],
    "feedback_targets": [],
    "feedback_draft": ""
  },
  "updated_at": "2026-07-07T10:00:00+08:00",
  "raw": {}
}
```

Use `scripts/normalize_issue_payload.py` when converting a JSON export or API result.

## Raw Payload Policy

Keep `raw` redacted by default. Recursively replace secret-like keys such as authorization, token, password, cookie, session, API key, private key, download `sign`, and signed URL parameters, including nested requirement, attachment, comment, activity, and evidence data.

Retain the complete source payload only when the user explicitly requests it, the source is trusted, and the destination stays in ignored local `.bugflow/` storage. Invoke `normalize_issue_payload.py --retain-raw` only under those conditions. This flag preserves the original payload and may therefore preserve credentials or other sensitive values; never copy retained raw into git, remote comments, logs, or the final response. Record in the run summary when full raw data was retained.

## Detail Fetching

List APIs often omit fields. For every candidate that will receive final triage, fetch the full issue even when the list row looks complete. Then follow `evidence-intake.md` to page through inbound comments, fetch relevant activity records, discover attachments from fields/rich text/comments, and inspect decision-relevant content.

Do not classify an issue as easy only from a short title, filename, thumbnail, or attachment count. `comments: []` is not proof that comments were fetched; use explicit `evidence_fetch` source states.

When any decision-relevant source is unavailable, preserve the metadata that is safe to keep, set `evidence_fetch.status` to `partial|error`, add the exact reason to `missing`, and block high-confidence/fix-plan decisions. Reading inbound comments is read-only; posting a comment is governed separately by completion-action authorization.

After the evidence gate, follow `report-quality.md`. A sparse description may become sufficient through inspected video, comments, activities, or an authoritative PRD. Conversely, fully fetched sources may still be missing expected behavior, trigger conditions, or acceptance criteria. Set `report_quality` to `needs-clarification`, `conflicting`, or `unknown`, generate exact questions and a local feedback draft, and block repair planning until it becomes `sufficient`.

Do not require a proposed code change from the tester. Keep an incorrect implementation suggestion separate from the observable goal; route the repair to the correct owner when the goal itself is clear. Never normalize passwords, tokens, private account credentials, or secret-bearing test data.

## Output

Preview returns a compact provisional list with id/number, title, status, priority, assignee, updated time, provisional ownership/risk, recommendation, current basis, and suspected gaps. Do not attach strict evidence/report-quality states or feedback drafts.

Fix-ready returns the selected issue with:

- id/number
- title
- status
- priority
- requirements
- updated time
- whether full detail was retrieved
- evidence completeness and whether attachment contents were actually inspected
- report-quality status, confirmed facts, conflicts, and any missing fields that affect implementation or acceptance
- exact clarification questions, feedback target, and the local unpublished feedback draft when needed
