# Feishu Project Adapter

Use this reference when `issue_source.platform` is `feishu-project` or the user provides a `project.feishu.cn` URL.

## Contents

- [URL Parsing](#url-parsing)
- [Access Order](#access-order)
- [Runtime Preflight](#runtime-preflight)
- [Client Setup](#client-setup)
- [Field Discovery](#field-discovery)
- [Common Feishu Fields](#common-feishu-fields)
- [Generate MQL From Config](#generate-mql-from-config)
- [Default MQL Shape](#default-mql-shape)
- [Inbound Evidence](#inbound-evidence)
- [Requirement Links](#requirement-links)
- [Common Status Labels](#common-status-labels)
- [Status Updates](#status-updates)
- [Comments](#comments)

## URL Parsing

Parse URLs such as:

```text
https://project.feishu.cn/PROJECT_KEY/issue/homepage
```

In this example:

- project key: `PROJECT_KEY`
- work item type: `issue`

If the URL is missing a project key or work item type, use repo-scoped project config. Ask only when neither source provides it.

## Access Order

1. Use an already configured Feishu Project MCP server exposed by the current client.
2. Use a project-approved OpenAPI script or SDK with credentials from environment variables.
3. Use browser automation only for one-off extraction after user authorization.

Do not ask for a password. Do not store tokens, cookies, secret-bearing/personal MCP URLs, or user keys in the repository. The fixed public endpoint may appear in client connection metadata, but authentication remains per-user and outside the skill.

## Runtime Preflight

Run this preflight only for live Feishu access. Exported JSON does not require MCP. Match tools by their schema and action semantics rather than an exact client-generated prefix; for example, Codex may expose `mcp__feishu_project__search_by_mql` while another client shows only the server and tool display names.

| Stage | Required capability |
| --- | --- |
| Preview | `search_by_mql` or an equivalent bounded MQL query tool |
| Exact field verification | `list_workitem_field_config` or an equivalent exact field-schema tool |
| Fix-ready evidence | full detail, paginated comments, relevant operation/activity records, and decision-related file download |
| Status transition | current/transitable status lookup plus the exact state-transition action |

Check only the current stage. Missing write tools must not block read-only Preview; missing fix-ready evidence tools must produce `partial|error` and block repair rather than trigger setup loops.

If no compatible query tool is visible, or the first connection reports missing credentials, `401/403`, startup timeout, pending client/workspace approval, or a missing environment variable, stop and report the exact client-visible state. Do not install packages, edit MCP configuration, rediscover all tools, or retry authentication unless the user explicitly asked to configure the connection. A client may perform one built-in reconnect/auth refresh; do not add an agent-level retry loop on top of it.

## Client Setup

Read `mcp-client-setup.md` only when the user asks to configure the live connection or the preflight fails. It contains separate Codex, Cursor, Claude Code, and generic MCP-client examples. Never copy configuration syntax from one client into another unchanged.

## Field Discovery

Use project config field mappings when present.

`doctor` reports local mapping and remote field verification separately. A mapped key is not considered remotely verified until an exact field-config response confirms it and the key/source/time are recorded under `field_verification`. Do not run broad field discovery on every scan, but do not pass unverified optional fields to MQL merely because they exist in `field_mapping`.

If a query fails or the project differs from the known mapping:

1. Fetch work item field config for the project and work item type with exact field keys first.
2. Confirm every status option id/code from the `work_item_status` field config. Do not infer status ids from labels or screenshots.
3. Confirm assignee/current-operator, requirement/demand, description, priority, attachment, and updated-time fields.
4. Record confirmed keys in `field_verification.verified_keys` with `source` and `verified_at`, then rebuild queries using field keys and option ids, not display labels.

When the project config is incomplete, query exact field keys first:

```text
project_key=<project-key>
work_item_type=issue
field_keys=["work_item_status", "<requirement-field>", "<attachment-field>"]
page_num=1
```

## Common Feishu Fields

Many Feishu Project issue schemas use this shape, but each target project must confirm it with field config:

- work item type: `issue`
- status field: `work_item_status`
- common status ids:
  - `OPEN`: `待修复`
  - `IN PROGRESS`: `修复中`
  - `RESOLVED`: `已解决，待验收`
  - `REOPENED`: `重新打开`
  - `CLOSED`: `已完成`
  - `systemEnded`: `已终止`
- current operator field: `current_status_operator`
- title field: `name`
- description field: `description`
- screenshot/recording field: project-specific, often a custom `field_*`
- updated time field: `updated_at`
- requirement/demand field: project-specific, often `_field_linked_story` or a custom `field_*`

Treat this as a default only. Project config overrides it.

## Generate MQL From Config

Use the runner to generate a profile-specific minimum query from project config:

```powershell
python <skill-dir>\scripts\bugflow_runner.py feishu-mql --profile preview
python <skill-dir>\scripts\bugflow_runner.py feishu-mql --profile fix-ready --json
```

`preview` keeps only candidate identity/filter fields plus remotely verified list fields. `fix-ready` may add remotely verified reporter, creation time, description, requirements, and attachments for one selected issue. The command prints:

- the MQL SELECT list
- the status filter values
- exact field keys to use when field config must be checked

Use `--json` when passing the query metadata to another script or scheduled task.

## Default MQL Shape

Use this shape for "my pending issues" only when every optional selected key was remotely verified:

```sql
SELECT `work_item_id`, `auto_number`, `name`, `current_status_operator`, `work_item_status`, `priority`, `description`, `<attachment-field>`, `updated_at`, `<requirement-field>`
FROM `PROJECT_KEY`.`WORK_ITEM_TYPE`
WHERE array_contains(`current_status_operator`, current_login_user())
  AND `work_item_status` IN ('待修复', '重新打开')
ORDER BY `updated_at` DESC
LIMIT 20
```

Replace `PROJECT_KEY`, `WORK_ITEM_TYPE`, field names, status values, and limit from project config. Feishu MQL may require display labels in `WHERE` even when field config exposes option ids; if MQL rejects the status condition, only adjust the `WHERE` status value and keep the verified field keys unchanged.

Add the configured requirement/demand field to the SELECT list only when it is remotely verified. If `query_policy.requirement_ids` or `--requirement-id` is set, keep `requirement_mql_pushdown_verified: false` by default and let the runner post-filter normalized requirement id/number/URL values. Enable pushdown only after the requirement field and matching semantics are both verified. If the list query omits linked requirements, fetch the full selected work item before repository matching.

`search_by_mql` may return records below `data -> <group-id> -> moql_field_list[]`. Pass the complete response to the runner; it extracts each grouped record instead of treating the response wrapper as one issue. After normalization, still enforce the current-user filter. When `current_login_user()` was used without a concrete alias, every record needs a readable assignee and all records must share a common current-user identity; otherwise stop rather than accepting a mixed batch.

## Inbound Evidence

The MQL list is only the candidate index. Before final triage of each candidate, follow `evidence-intake.md` and use the configured Feishu Project MCP tools when available:

1. Fetch full detail with `get_workitem_brief` (or the server's equivalent) instead of relying on the list row.
2. Page through `list_workitem_comments` until complete. Preserve sanitized comment ids, authors, times, text, and attachment references.
3. Read relevant `get_workitem_op_record` pages/time windows when status changes, reopening, reassignment, field changes, or newly added evidence may affect the current expectation.
4. Collect attachment/file references from the configured attachment field, description/rich text, and comments.
5. Resolve files with `get_download_url` or the equivalent tool, use the returned sign/header only for the immediate download request, and inspect the downloaded local file. Never put the sign, temporary URL, authorization header, or MCP URL into `issue.json`.

Tool names and pagination limits can vary by MCP version. Honor the tool schema returned by the active server. If comments, operation records, or downloads are unavailable, record that source as `partial|error`; do not convert “tool missing” into an empty list or a high-confidence conclusion.

For images, inspect the actual resolution. For recordings, inspect representative frames/segments around the demonstrated failure and audio/transcript when relevant. A cover frame or thumbnail alone is not reviewed evidence.

## Requirement Links

Many Feishu Project bug lists show a linked requirement/demand column. Fetch it as a structured field when possible, not only as display text.

Normalize linked requirements to:

```json
{
  "id": "REQ-1",
  "title": "Requirement title",
  "url": "https://project.feishu.cn/..."
}
```

When the requirement field key is unknown:

1. Call field config discovery.
2. Look for fields whose display label means `需求`, `关联需求`, `需求/项目`, or the team's custom demand field.
3. Add the field key to `field_mapping.requirements` or `requirement_mapping.issue_requirement_field`.
4. Fetch full issue detail before triage.

## Common Status Labels

The Feishu bug workflow may include these labels:

- `待修复`
- `修复中`
- `已解决，待验收`
- `重新打开`
- `已完成`
- `已终止`

Resolve every status id from Feishu field config before status updates. Labels are acceptable for display and sometimes for MQL filtering, but status update APIs should use the verified option id/code.

## Status Updates

The Feishu starter enables `update_status_allowed`, `start_fix`, and `resolve_for_acceptance` as normal repair capabilities. These defaults do not authorize or automatically execute an update.

Before changing status:

- Read `status-workflow.md`.
- Require `completion_action_authorized(issue, exact_transition)`; the transition must be listed in the approved fix plan.
- Confirm the target status id from config or field schema.
- Confirm the transition is allowed by project policy.
- Include the issue id, old status, new status, and reason in the operation summary.

Default safe behavior:

- Moving `待修复` to `修复中`: require plan action `start-fix`, effective project/local permission, an enabled transition, and a verified target status id. Autonomous mode runs it only after AI verification and commit; assisted mode runs it only after commit and direct user verification. `修复中` is the final state of the normal repair run.
- Moving to `已解决，待验收`, `已完成`, or `已终止`: never infer this from either repair mode. Require a separate explicit request for exact issue ids, re-read the current state, and satisfy the matching verification/acceptance prerequisites. Manual post-acceptance updates are also valid.

## Comments

Reading existing comments is mandatory read-only evidence intake before final triage and does not require completion-action authorization. Fetch all pages, order comments stably by time/id, and include only sanitized, decision-relevant text and attachment summaries in the normalized issue.

Posting a new comment is a different remote action. Only when `completion_action_authorized(issue, comment)` is true, include:

- what changed
- verification commands
- browser route and result when applicable
- residual risk

Do not paste secrets, full logs with tokens, or unrelated diffs into comments.
