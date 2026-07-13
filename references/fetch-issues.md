# Fetch Issues

Use this reference when retrieving issues from a tracker or from an exported JSON file.

## Goals

- Fetch only actionable issues first.
- Retrieve enough detail for triage: title, description, reproduction steps, expected result, actual result, priority, assignee, status, update time, requirement links, inbound comments, relevant activity records, and the inspected content of screenshots/recordings/attachments.
- Normalize all sources to a shared JSON shape before classification.

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

## Output

Return a compact issue list with:

- id/number
- title
- status
- priority
- requirements
- updated time
- whether full detail was retrieved
- evidence completeness and whether attachment contents were actually inspected
- any missing fields that affect triage
