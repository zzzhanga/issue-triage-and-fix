# Project Config

Use a config stack to keep generic workflow, repo-scoped project rules, and user-local settings separate.

## Config Stack

Read configuration in this order. Later layers override earlier layers.

1. Skill defaults from `SKILL.md` and references.
2. Repo-scoped project config, committed to the repository.
3. Optional local override config, ignored by git.
4. The current user request.

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

The repo-scoped config should contain project/team facts that should not be rewritten by every user:

- `project`: repository path, docs, role assumption, and local conventions.
- `issue_source`: tracker platform and project/work-item identifiers.
- `field_mapping`: source fields for the normalized issue shape.
- `requirement_mapping`: how tracker requirements/demands map to this repo and related repos.
- `query_policy`: assignee, status, limit, and ordering.
- `ownership_rules`: project-specific ownership hints.
- `verification`: local commands and when to run them.
- `browser_verification`: app URL, routes, and visible workflows requiring browser checks.
- `login_policy`: allowed login methods and default secret environment variable names.
- `remote_status_policy`: team-level status/comment update policy.
- `status_transitions`: named transitions such as start and finish.
- `execution_policy`: what may be auto-fixed.
- `comment_template`: standard remote comment shape.
- `bugflow`: artifact root, schema path, and whether issue work artifacts are committed.

Requirement mappings, status ids, and status labels belong in the repo-scoped config, not in every user's local file.

Use a relative `project.repo_path` such as `.` for repo-scoped configs whenever possible. Put machine-specific absolute paths in local overrides only when a tool cannot resolve the repository root.

## Local Override Config

Use `assets/local-overrides.template.yaml` as the starter.

The local override should contain only user or machine-specific settings:

- local app URL or port
- login method preference
- test account environment variable names
- user-specific assignee override when `current_login_user()` is unavailable
- stricter automation choices, such as disabling remote status updates

Do not duplicate repo-scoped field mappings, requirement mappings, statuses, transitions, or project ownership rules in the local file.

## Requirement Mapping

Use `requirement_mapping` to tell the skill how tracker requirements or demands relate to local repositories.

Example:

```yaml
requirement_mapping:
  enabled: true
  issue_requirement_field: requirement
  current_repo:
    repo_key: admin
    path: .
    aliases:
      - admin
      - management backend
      - 管理后台
  related_repositories:
    - repo_key: miniprogram
      path: ../miniprogram
      aliases:
        - miniprogram
        - 小程序
  demand_rules:
    - match_title_contains: 上海教育出版社小程序+管理后台
      repo_keys:
        - admin
        - miniprogram
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
  schema: .codex/bugflow/schema.yaml
  commit_artifacts_by_default: false
```

Recommended split:

- `.codex/bugflow/` stores config and schema.
- `.bugflow/` stores generated daily reports and per-issue artifacts.
- Add `.bugflow/` to the host project's `.gitignore` when `commit_artifacts_by_default` is false.

Keep `commit_artifacts_by_default` false when issue descriptions, screenshots, customer names, or tracker metadata should stay local. Set it true only when the team wants bugflow artifacts reviewed in git.

## Secret Handling

Never put real credentials in config. Use environment variable names:

```yaml
login_policy:
  test_account_env:
    username: PROJECT_TEST_USERNAME
    password: PROJECT_TEST_PASSWORD
```

## Status Labels

Prefer stable status ids over display labels. Display labels are useful for humans but should not be the only value used for API calls.

## Verification Commands

Commands may include placeholders:

- `<changed-files>`
- `<changed-style-files>`
- `<issue-route>`

Resolve placeholders before running. If a command is not applicable, explain why.

## Project Overrides

Repo-scoped project config may tighten the generic rules, but should not weaken safety defaults unless the team explicitly accepts the risk.

Local overrides may make automation stricter for a user, but should not silently make remote workflow changes more permissive than the repo-scoped project config.
