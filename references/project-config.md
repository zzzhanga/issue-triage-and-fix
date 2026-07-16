# Project Config

Use a config stack to keep generic workflow, repo-scoped project rules, and user-local settings separate.

## Contents

- [Config Stack](#config-stack)
- [Repo-Scoped Project Config](#repo-scoped-project-config)
- [Local Override Config](#local-override-config)
- [Requirement Mapping](#requirement-mapping)
- [Bugflow Artifacts](#bugflow-artifacts)
- [Secret And Raw Data Handling](#secret-and-raw-data-handling)
- [Status Labels](#status-labels)
- [Verification Commands](#verification-commands)
- [Remote And Git Policy](#remote-and-git-policy)
- [Project Overrides](#project-overrides)

## Config Stack

Read configuration in this order:

1. Skill defaults from `SKILL.md` and references.
2. Repo-scoped project config, kept local by default or committed only when repository policy explicitly shares it.
3. Optional local override config, ignored by git.
4. The current user request, which can approve a concrete action for the current task but is not persisted as configuration.

Merge ordinary descriptive values from earlier to later layers. Merge capability booleans conservatively:

- Treat a project-level `false` as denied.
- Let a local override change `true` to `false`, never `false` to `true`.
- Require an explicit single/batch repair request or approval of an exact plan in addition to configuration. Bind current-run authorization to each issue's exact plan; unlisted or out-of-mode actions remain unauthorized.
- Do not interpret a missing key as permission.

Preferred local-only repo-scoped project config paths:

```text
.codex/bugflow/issue-triage.project.yaml
```

Use this when the config is for the local agent workflow and should not be committed.

If a team intentionally wants to commit the same workflow config for every clone, use a repo-approved committed path such as:

```text
agent/issue-triage.project.yaml
config/issue-triage.project.yaml
```

Use a committed path only when the team intentionally wants every clone of the repository to share the same workflow config.

Preferred local override path:

```text
.codex/bugflow/issue-triage.local.yaml
```

Legacy single-file configs may exist at `.codex/issue-triage.config.yaml`; read them as a fallback, but prefer splitting repo-scoped and local configuration.

## Repo-Scoped Project Config

Use `assets/project-config.template.yaml` as the starter.

For a new repository, prefer the runner initializer instead of copying templates by hand:

```powershell
python <skill-dir>\scripts\bugflow_runner.py init-project --platform feishu-project --project-name my-project --project-key my-feishu-project-key
```

The initializer creates `.codex/bugflow/issue-triage.project.yaml`, `.codex/bugflow/issue-triage.local.yaml`, `.codex/bugflow/schema.yaml`, and adds `.bugflow/` to `.gitignore` unless told otherwise.

The repo-scoped config should contain project/team facts that should not be rewritten by every user:

- `project`: repository path, docs, role assumption, and local conventions.
- `issue_source`: tracker platform and project/work-item identifiers.
- `field_mapping`: source fields for the normalized issue shape.
- `field_verification`: remote field keys actually confirmed by exact schema discovery, with source and time; local mapping alone is not proof.
- `requirement_mapping`: how tracker requirements/demands map to this repo and related repos.
- `query_policy`: assignee, optional exact requirement scope, status, limit, and ordering.
- `ownership_rules`: project-specific ownership hints.
- `verification`: local commands and when to run them.
- `browser_verification`: app URL, browser-surface priority, routes, and visible workflows requiring browser checks.
- `login_policy`: existing-session priority, allowed login methods, and default secret environment variable names.
- `remote_status_policy`: team-level status/comment update policy.
- `status_transitions`: named transitions such as start and finish.
- `execution_policy`: what may be auto-fixed.
- `git_policy`: whether verified single-bug fixes should create a local commit, and how commit messages are formatted.
- `comment_template`: standard remote comment shape.
- `bugflow`: artifact root, schema path, and whether issue work artifacts are committed.

Use `issue_source.platform: feishu-project` only for the native Feishu adapter. For Jira, TAPD, 禅道, GitLab Issues, or another tracker, use `exported-json` (or the project-approved equivalent) and import an exported JSON payload; do not configure a native adapter that does not exist.

Requirement mappings, status ids, and status labels belong in the repo-scoped config, not in every user's local file.

`field_mapping.attachments` maps the work-item attachment field used by MQL/detail reads. Inbound comments and activity records are not ordinary MQL fields: fetch them through the tracker/MCP's dedicated read tools and merge sanitized `comments`, `activities`, and `evidence_fetch` into the normalized payload. Do not add fake `comments` or `activities` field mappings to make a query appear complete.

Use a relative `project.repo_path` such as `.` for repo-scoped configs whenever possible. For cross-directory operation pass `--repo-root <repo>` explicitly; pass `--artifact-root <path>` only when generated issue artifacts live elsewhere. These roots are independent and Git/code operations always use the repository root. Put machine-specific absolute paths in local overrides only when a tool cannot resolve the repository root.

Treat `field_mapping` as normalization configuration, not remote-schema verification. Populate this only after an exact field-config read succeeds:

```yaml
field_verification:
  verified_keys:
    - priority
    - updated_at
    - _field_linked_story
  source: list_workitem_field_config exact keys
  verified_at: 2026-07-13T10:00:00+08:00
```

The runner always keeps core id/number/title/status/assignee fields needed to identify and safely filter candidates. Optional preview/fix-ready SELECT fields are omitted until remotely verified. `doctor` reports local mapping and remote verification separately.

Limit a run to exact linked requirements when needed:

```yaml
query_policy:
  requirement_ids:
    - REQ-123
  requirement_mql_pushdown_verified: false
```

Keep pushdown false until both the remote field key and the connector's matching semantics are verified. With false, the runner post-filters normalized issues by requirement id/number/URL. A command-line `--requirement-id` overrides the configured list for that run.

## Local Override Config

Use `assets/local-overrides.template.yaml` as the starter.

The local override should contain only user or machine-specific settings:

- local app URL or port
- browser-surface and existing-session preference
- login method preference
- test account environment variable names
- user-specific assignee override when `current_login_user()` is unavailable
- `assignee_aliases` for tracker display-name/user-key/id variants used by exported JSON
- stricter automation choices, such as disabling remote status updates

Do not duplicate repo-scoped field mappings, requirement mappings, statuses, transitions, or project ownership rules in the local file.

Treat local automation fields as deny-only. `false` tightens a project permission. A local `true` must not grant remote updates, automatic repair, automatic commit, or push when the project config denies them. Prefer omitting an unchanged capability rather than copying `true` into the local file.

## Requirement Mapping

Use `requirement_mapping` to tell the skill how tracker requirements or demands relate to local repositories.

Example:

```yaml
requirement_mapping:
  enabled: true
  issue_requirement_field: requirement
  current_repo:
    repo_key: web
    path: .
    aliases:
      - web client
      - current repo
  related_repositories:
    - repo_key: mobile
      path: ../mobile
      aliases:
        - mobile app
        - companion app
  demand_rules:
    - match_title_contains: shared requirement keyword
      repo_keys:
        - web
        - mobile
      confirmation_owner: customer
  confirmation_policy:
    confidence_threshold: 0.75
    require_confirmation_when:
      - no_requirement
      - no_repo_match
      - multiple_repo_match
      - low_confidence
```

If a requirement can map to multiple repos, do not treat the mapping itself as enough to fix in the current repo. Use issue evidence and code search; ask the configured confirmation owner when unclear.

## Bugflow Artifacts

Use `bugflow` to configure resumable issue work:

```yaml
bugflow:
  enabled: true
  root: .bugflow/issues
  report_root: .bugflow/reports
  schema: .codex/bugflow/schema.yaml
  commit_artifacts_by_default: false
```

Recommended split:

- `.codex/bugflow/` stores config and schema.
- `.bugflow/` stores generated daily reports and per-issue artifacts.
- Add `.bugflow/` to the host project's `.gitignore` when `commit_artifacts_by_default` is false.

Keep `commit_artifacts_by_default` false when issue descriptions, screenshots, customer names, or tracker metadata should stay local. Set it true only when the team wants bugflow artifacts reviewed in git.

## Secret And Raw Data Handling

Never put real credentials in config. Use environment variable names:

```yaml
login_policy:
  test_account_env:
    username: PROJECT_TEST_USERNAME
    password: PROJECT_TEST_PASSWORD
```

For local-project verification, prefer a matching tab already open in the user's Chrome, then an existing in-app browser tab, then a new in-app browser tab. Reuse the selected browser's own authenticated session. Never inspect, export, copy, or inject cookies, localStorage, passwords, browser profiles, or session stores to move login state between browsers.

Keep normalized issue fields by default and redact sensitive keys from source `raw` payloads. This includes temporary media-download `sign`/headers and signed URLs embedded in comments or rich text. Retain full raw payloads only when the user explicitly asks, the source is trusted, and the resulting `.bugflow/` directory remains local and ignored by git. Because explicit retention preserves the original payload, treat it as sensitive and never copy it into git, remote comments, logs, or the final response.

## Status Labels

Prefer stable status ids over display labels. Display labels are useful for humans but should not be the only value used for API calls.

## Verification Commands

Commands may include placeholders:

- `<changed-files>`
- `<changed-style-files>`
- `<issue-route>`

Resolve placeholders before running. If a command is not applicable, record a structured exemption and its reason. Do not mark verification done from an empty command list or prose such as "looks good". Record each applicable result as `passed`, `failed`, or `blocked`, with the command/type and concise evidence.

Standard verification is plan-bound: `plan-fix` derives `required_checks` from the changed file types, configured test/build policy, browser route, and visible issue signals. Every named requirement must be `passed`; an unrelated passed command cannot satisfy lint/test/build/browser. `record-verification` also requires `--verified-by user|agent|ci`, records UTC time automatically, and accepts a concise `--verification-note`. Plan-approved lightweight verification may finish without an automated pass only when the runner confirms high-confidence current-repo frontend ownership, low/medium risk, easy/medium effort, no unresolved confirmation, a concrete automation-exemption reason, and inspection evidence. Keep `execution_policy.allow_lightweight_verification: true` to enable this path; a local `false` is a hard deny.

`deferred-to-user` is separate from lightweight verification. Enable it with `execution_policy.allow_deferred_user_verification: true` only when the team accepts plan-bound commits before manual verification. It skips AI repair verification, keeps Feishu unchanged during edits, and only accepts a direct `verified_by: user` result. After a pass, assisted mode may execute its delayed `start-fix`; it does not resolve the issue for acceptance.

Use `execution_policy.default_repair_mode` when user wording does not choose a mode. `approved_completion_actions` supplies the autonomous bundle: the Feishu starter uses `commit` then `start-fix`; exported JSON uses only `commit`. `assisted_completion_actions` supplies the human-verification bundle: Feishu uses `commit` and delayed `start-fix`, while exported JSON uses only `commit`. Neither bundle includes `resolve-for-acceptance`; the runner also filters this action out of legacy default bundles unless it is explicitly requested in a new plan. A direct single/batch repair request authorizes the matching bundle for its frozen scope; the runner still records an exact plan fingerprint for every issue.

## Remote And Git Policy

The native Feishu starter enables the two normal repair transitions as capabilities:

```yaml
remote_status_policy:
  update_status_allowed: true
  update_comments_allowed: false
  default_change_to_in_progress: true
  default_resolve_for_acceptance: true
  default_complete: false
  default_terminate: false
```

The exported-JSON starter keeps every remote capability false because it has no native remote adapter. For Feishu, enabled capability flags still require a current-run or exact-plan authorization, a plan that lists the transition, verified target status ids/transitions, and no local deny. Autonomous mode holds `start-fix` until AI verification and commit succeed; assisted mode holds it until commit and direct user verification succeed. The legacy-named `default_resolve_for_acceptance` field is only a capability gate for a separately authorized post-acceptance action, not a default repair action.

Use `git_policy` to control local commits after a verified fix:

```yaml
git_policy:
  auto_commit_after_fix: false
  commit_after_verification_only: true
  stage_policy: touched-files-only
  push_after_commit: false
  commit_message_template: "fix({issue}): {title}"
```

Keep `auto_commit_after_fix` and `push_after_commit` false by default. Every local commit requires `commit` in the approved plan and the matching plan fingerprint passed to `commit-fix`. Autonomous mode also requires current standard/lightweight verification. Assisted mode may commit with `verification_pending: true` only under an approved `deferred-to-user` plan and `allow_deferred_user_verification: true`. Abort if the index already contains staged changes; do not mix user-staged files into an automated commit. Accept only literal fix-related file paths, never `.`, a directory, a glob, or a path outside the repository.

## Project Overrides

Repo-scoped project config may enable a capability only through an intentional reviewed change; doing so does not replace a direct single/batch repair request or approval of an exact plan that visibly lists each completion action.

Local overrides may make automation stricter for a user, but must never make repair, commit, push, comments, or remote workflow changes more permissive than the repo-scoped project config.
