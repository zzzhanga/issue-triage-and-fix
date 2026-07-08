# Fetch Issues

Use this reference when retrieving issues from a tracker or from an exported JSON file.

## Goals

- Fetch only actionable issues first.
- Retrieve enough detail for triage: title, description, reproduction steps, expected result, actual result, screenshots, priority, assignee, status, update time, requirement links, and related issue links.
- Normalize all sources to a shared JSON shape before classification.

## Access Order

1. Use configured MCP or official API tools.
2. Use a local script or exported JSON file supplied by the user.
3. Use browser automation only for one-off extraction when the user authorizes interactive login.

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

If any configured field fails, fetch the tracker field schema before retrying. Use field keys or ids, not display labels, whenever the platform supports stable identifiers.

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
  "updated_at": "2026-07-07T10:00:00+08:00",
  "raw": {}
}
```

Use `scripts/normalize_issue_payload.py` when converting a JSON export or API result.

## Detail Fetching

List APIs often omit fields. If a candidate issue lacks description, attachments, reproduction steps, requirement links, or status details, fetch the full issue before triage.

Do not classify an issue as easy only from a short title.

## Output

Return a compact issue list with:

- id/number
- title
- status
- priority
- requirements
- updated time
- whether full detail was retrieved
- any missing fields that affect triage
