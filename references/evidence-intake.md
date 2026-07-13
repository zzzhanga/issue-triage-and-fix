# Evidence Intake

Use this reference after finding candidate issues and before final requirement matching or triage.

## Evidence Gate

Treat the title and list row as an index, not as the complete bug report. Before marking an issue high-confidence, `easy`, `auto-fix-candidate`, ready for a fix plan, or safe for lightweight verification:

1. Fetch the full work-item detail, including rich description and current field values.
2. Read inbound comments/discussion history with pagination. Reading existing comments is part of read-only triage; posting a new comment remains a separately authorized completion action.
3. Fetch relevant operation/activity records when they can clarify changed acceptance criteria, status transitions, reassignment, reopening, or newly added evidence.
4. Inventory attachments from fields, rich text, and comments. Inspect every decision-relevant attachment rather than only listing its filename.
5. Record a sanitized `evidence_fetch` summary and concrete findings in the normalized issue.

A list response, attachment thumbnail, filename, or “attachment exists” flag does not satisfy this gate.

If a decision-relevant comment or attachment cannot be retrieved or inspected, mark evidence `partial` or `error`, state the exact blocker, and keep the triage provisional. Do not produce high confidence, `easy`, `auto-fix-candidate`, a fix plan, or lightweight verification from incomplete evidence.

## Feishu Read Order

When the configured Feishu Project MCP exposes the corresponding tools, use this order:

1. `search_by_mql` for the minimal current-user candidate list.
2. `get_workitem_brief` or the equivalent full-detail tool for each candidate.
3. `list_workitem_comments` until every page is read.
4. `get_workitem_op_record` for relevant time windows, following its pagination/window limits.
5. Collect file references from attachment fields, rich text, and comment bodies.
6. Resolve downloadable media with `get_download_url` or the equivalent tool, download to ignored local evidence storage, then inspect it with the appropriate local visual/media tool.

Tool availability varies by server version. If a tool is absent or access is denied, record that source as incomplete instead of pretending that no comments or attachments exist. Do not treat `comments: []` as proof that the comment endpoint was queried; `evidence_fetch.comments` must say `complete` or `not-applicable` explicitly.

## Media Review

- Image: inspect the actual image at sufficient resolution, not only the tracker thumbnail. Record the visible state, UI location, error text, and any mismatch with the title or description.
- Video: inspect metadata and representative frames, including the beginning, end, and the interval where the reporter demonstrates the problem. Review audio/transcript when it carries reproduction or acceptance details. Do not say a video was reviewed after seeing only its cover frame.
- Document/log: extract only the parts relevant to reproduction, expected behavior, actual behavior, ownership, or acceptance criteria.
- Unsafe executable/archive: do not execute it. Record it as uninspected and request a safe export or human summary when it is decision-relevant.

For large or unsupported media, sample only when the sampling still covers the demonstrated problem. Otherwise keep evidence incomplete and ask for the missing timestamp, safe export, or access.

## Canonical Evidence Shape

Store sanitized evidence in `issue.json`; never store temporary download headers, tokens, `sign`, signed URLs, cookies, or MCP URLs.

```json
{
  "attachments": [
    {
      "id": "file-1",
      "name": "repro.mp4",
      "source": "comment",
      "media_kind": "video",
      "mime_type": "video/mp4",
      "comment_id": "comment-7",
      "local_path": "evidence/repro.mp4",
      "sha256": "...",
      "inspection_state": "inspected",
      "summary": "At 00:08 the first video frame is hidden after reopening the draft."
    }
  ],
  "comments": [
    {
      "id": "comment-7",
      "author": "reporter",
      "created_at": "2026-07-13T10:00:00+08:00",
      "content_text": "The expected result is to show the first frame.",
      "attachments": []
    }
  ],
  "activities": [],
  "evidence_fetch": {
    "status": "complete",
    "detail": "complete",
    "comments": "complete",
    "activities": "complete",
    "media": "complete",
    "fetched_at": "2026-07-13T10:05:00+08:00",
    "findings": ["The video evidence narrows the problem to first-frame rendering."],
    "missing": []
  }
}
```

Allowed source states are `complete`, `partial`, `not-applicable`, `skipped`, `error`, and `unknown`. Aggregate `evidence_fetch.status` is `complete` only when every decision-relevant source is `complete` or genuinely `not-applicable`, and every decision-relevant attachment has `inspection_state: inspected`.

## Conflicts And Freshness

- Prefer newer explicit acceptance criteria in comments over a stale short title, but report the conflict instead of silently rewriting the issue.
- A reopen comment, new attachment, or changed operation record is material evidence. Refresh `issue.json` and invalidate downstream artifacts before continuing.
- Record which source supports each important conclusion: description, comment id, attachment name/timestamp, activity id, requirement, or code search.
- Keep the evidence summary factual. Do not infer unseen frames, missing comment pages, or inaccessible files.

## Security

- Keep downloaded evidence inside `.bugflow/issues/<safe-issue-key>/evidence/` or another ignored local directory.
- Validate paths, file size, and media type before opening. Never execute attachments.
- Use temporary download credentials only in memory/request headers. Redact `sign`, `signature`, authorization, token, cookie, and signed query parameters from normalized data and logs.
- Do not copy private comments or media into git, remote comments, or the final response unless project policy and the user explicitly allow it.
