# Feishu Project Adapter

Use this reference when `issue_source.platform` is `feishu-project` or the user provides a `project.feishu.cn` URL.

## URL Parsing

Parse URLs such as:

```text
https://project.feishu.cn/ai-rays/issue/homepage
```

In this example:

- project key: `ai-rays`
- work item type: `issue`

If the URL is missing a project key or work item type, use repo-scoped project config. Ask only when neither source provides it.

## Access Order

1. Use an available Feishu Project MCP server.
2. Use a project-approved OpenAPI script or SDK with credentials from environment variables.
3. Use browser automation only for one-off extraction after user authorization.

Do not ask for a password. Do not store tokens, cookies, MCP URLs, or user keys in the repository.

## Field Discovery

Use project config field mappings when present.

If `bugflow_runner.py doctor` confirms `field-mapping`, `requirement-field`, and `status-codes` as ok, trust the project config for routine daily triage. Do not run broad field discovery just because a field config query returns an empty list. Treat an empty broad field-config response as an MCP/query issue unless the actual MQL query also fails.

If a query fails or the project differs from the known mapping:

1. Fetch work item field config for the project and work item type with exact field keys first.
2. Confirm every status option id/code from the `work_item_status` field config. Do not infer status ids from labels or screenshots.
3. Confirm assignee/current-operator, requirement/demand, description, priority, attachment, and updated-time fields.
4. Rebuild queries using field keys and option ids, not display labels.

For `ai-rays.issue`, this exact field-config query is known to return the key fields:

```text
project_key=ai-rays
work_item_type=issue
field_keys=["work_item_status", "_field_linked_story", "field_696151"]
page_num=1
```

## Common Feishu Fields

For `ai-rays.issue`, the historical mapping is:

- work item type: `issue`
- status field: `work_item_status`
- status ids from field config:
  - `OPEN`: `待修复`
  - `IN PROGRESS`: `修复中`
  - `RESOLVED`: `已解决，待验收`
  - `REOPENED`: `重新打开`
  - `CLOSED`: `已完成`
  - `systemEnded`: `已终止`
- current operator field: `current_status_operator`
- title field: `name`
- description field: `description`
- screenshot/recording field: `field_696151`
- updated time field: `updated_at`
- requirement/demand field: `_field_linked_story`

Treat this as a default only. Project config overrides it.

## Default MQL Shape

Use this shape for "my pending issues" when the mapping matches:

```sql
SELECT `work_item_id`, `auto_number`, `name`, `current_status_operator`, `work_item_status`, `priority`, `description`, `field_696151`, `updated_at`
FROM `PROJECT_KEY`.`WORK_ITEM_TYPE`
WHERE array_contains(`current_status_operator`, current_login_user())
  AND `work_item_status` IN ('待修复', '重新打开')
ORDER BY `updated_at` DESC
LIMIT 20
```

Replace `PROJECT_KEY`, `WORK_ITEM_TYPE`, field names, status values, and limit from project config. Feishu MQL may require display labels in `WHERE` even when field config exposes option ids; if MQL rejects the status condition, only adjust the `WHERE` status value and keep the verified field keys unchanged.

Add the configured requirement/demand field to the SELECT list when available. If the list query omits linked requirements, fetch the full work item before repository matching.

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

Before changing status:

- Read `status-workflow.md`.
- Confirm the target status id from config or field schema.
- Confirm the transition is allowed by project policy.
- Include the issue id, old status, new status, and reason in the operation summary.

Default safe behavior:

- Moving `待修复` to `修复中`: allowed only when config enables `start_fix` and the target status id is known.
- Moving to `已解决，待验收`, `已完成`, or `已终止`: require config permission or user approval.

## Comments

When remote comments are allowed, include:

- what changed
- verification commands
- browser route and result when applicable
- residual risk

Do not paste secrets, full logs with tokens, or unrelated diffs into comments.
