---
name: issue-triage-and-fix
description: Fetch assigned bugs or issues, map issue requirements to local code repositories, triage ownership and effort, recommend repair order, fix safe candidates, verify locally and in a browser, and optionally comment or update remote issue workflow status. Use when Codex needs an internal engineering workflow for Feishu Project, Jira, TAPD, ZenTao, GitLab Issues, or similar trackers, especially when tracker fields, requirement-to-repository mappings, login policy, verification commands, and status transitions should be read from config.
---

# Issue Triage And Fix

## Overview

Use this skill to turn issue tracker bugs into controlled engineering work: fetch the issues, classify them, repair only the safe candidates, verify the result, and close the loop with a status/comment when policy allows it.

This is the orchestrating workflow. Keep platform details, requirement-to-repository matching, classification rules, browser verification, and repo-scoped config in references so another project can reuse the same process with different settings.

## Artifact-Guided Bugflow

Use bugflow artifacts when a bug needs more than a quick answer, when multiple repositories may be involved, or when work should be resumable and auditable.

Default issue work directory:

```text
.bugflow/issues/<issue-number>/
```

Default artifact chain:

```text
issue-intake -> requirement-match -> triage-report -> fix-plan -> implementation -> verification -> closure
```

Actions are fluid, not rigid phases. Use the next ready artifact when the path is clear; update earlier artifacts when new information changes the requirement, repo match, or fix plan.

## Required Inputs

Before acting, locate or ask for:

- The current repository path.
- A config stack: skill defaults, a repo-scoped project config, an optional local override config, and the current user request.
- The issue source: Feishu Project, Jira, TAPD, ZenTao, GitLab Issues, or a supplied JSON export.
- The user's requested mode: list only, triage only, fix one issue, or run the full controlled workflow.

If no repo-scoped project config exists, read `references/project-config.md` and use `assets/project-config.template.yaml` or `assets/feishu-project-config.template.yaml` to draft one before making remote workflow changes.

## Workflow

1. Read project rules.
   - Read repository guidance such as `AGENTS.md`, `README.md`, and configured project docs.
   - Read the config stack in this order: repo-scoped project config, optional local override config, then current user request.
   - Treat repo-scoped project config as the source for issue fields, requirement-to-repository mappings, status ids, team workflow, verification commands, and project ownership rules.
   - Treat local override config as the source for user-specific login preference, local ports, secret environment variable names, and stricter automation preferences.

2. Fetch issues.
   - Read `references/fetch-issues.md`.
   - For artifact-guided work, create or update `issue.json` under the issue work directory.
   - Load platform-specific instructions only when needed, for example `references/feishu-project.md`.
   - Fetch only actionable issues first: assigned to the target user and in the configured open/pending status.
   - Fetch requirement links or requirement fields when the tracker provides them.
   - Normalize the result to the standard issue shape before triage.

3. Resolve requirement and repository association.
   - Read `references/requirement-repo-mapping.md`.
   - For artifact-guided work, create or update `requirement-match.md`.
   - Match the issue's requirement, title, links, screenshots, and configured demand aliases to candidate repositories.
   - Proceed only when the current repository is a confident match or the user explicitly selected it.
   - If a requirement maps to multiple repositories and ownership is unclear, prepare a customer/product confirmation question instead of guessing.

4. Triage issues.
   - Read `references/triage-issues.md`.
   - For artifact-guided work, create or update `triage.md`.
   - Classify ownership, effort, readiness, risk, and recommended execution order.
   - Do not change code or remote statuses during triage-only requests.

5. Select repair candidates.
   - For artifact-guided work, create or update `fix-plan.md` before editing code.
   - Fix only issues classified as `auto-fix-candidate` unless the user explicitly chooses another issue.
   - Do not auto-fix `hard`, `blocked`, `needs-confirmation`, `not-current-repo`, or cross-owner issues.

6. Start repair workflow.
   - Read `references/status-workflow.md` and `references/fix-and-verify.md`.
   - Change the remote issue to the configured in-progress status only when project config allows it.
   - Never update remote status when credentials, project identity, or transition policy are unclear.

7. Implement the fix.
   - Keep edits scoped to the issue.
   - Follow the repository's component, style, testing, formatting, and branch conventions.
   - Do not revert unrelated user changes.

8. Verify.
   - Run the configured targeted format, lint, test, style, build, or regression commands.
   - For artifact-guided work, create or update `verification.md`.
   - For visible UI behavior, read `references/browser-verification.md` and verify in a browser unless the user opted out.
   - Use configured login policy; never ask the user to paste a password.

9. Close the loop.
   - Post a comment only when config or the user allows remote comments.
   - For artifact-guided work, create or update `closure.md`.
   - Include changed files, verification commands, browser evidence when available, and residual risk.
   - Move to resolved-for-acceptance, completed, or terminated only when config allows it or the user explicitly approves.

## Operating Modes

- `list`: Fetch and summarize assigned issues without triage details.
- `triage`: Fetch, normalize, classify, and rank issues without code or remote status changes.
- `fix-one`: Repair one user-selected issue with verification.
- `full-controlled`: Triage, select safe candidates, optionally mark in progress, fix, verify, comment, and optionally transition.

Default to `triage` when the user is exploring, and to `fix-one` when the user names a specific issue.

## Resource Map

- `references/fetch-issues.md`: Issue retrieval and normalized payload contract.
- `references/bugflow-artifacts.md`: Artifact-guided workflow, action rules, and status model.
- `references/requirement-repo-mapping.md`: Requirement-to-repository matching and customer confirmation rules.
- `references/triage-issues.md`: Ownership, effort, readiness, risk, and ranking rules.
- `references/fix-and-verify.md`: Code repair, local validation, browser validation, and closing comments.
- `references/feishu-project.md`: Feishu Project adapter rules.
- `references/browser-verification.md`: Browser verification and login policy.
- `references/status-workflow.md`: Remote status transition policy.
- `references/project-config.md`: Config stack schema and merge rules.
- `scripts/normalize_issue_payload.py`: Convert tracker payloads to the standard issue JSON shape.
- `scripts/bugflow_artifacts.py`: Initialize issue work directories and report artifact readiness.
- `assets/bugflow-schema.template.yaml`: Copyable artifact dependency schema.
- `assets/project-config.template.yaml`: Copyable repo-scoped project config starter.
- `assets/feishu-project-config.template.yaml`: Copyable Feishu-specific config starter with requirement mapping and status labels.
- `assets/local-overrides.template.yaml`: Copyable local user override starter.

## Safety Rules

- Do not request, echo, store, or commit passwords, tokens, MCP URLs, cookies, or session secrets.
- Prefer official API/MCP access over browser scraping. Use browser login only for one-off interactive access when the user allows it.
- Do not change issue status by default. The project config must allow the specific transition, or the user must approve it.
- Do not mark an issue fixed solely because code was edited. Verify first, then report any unverified portions.
- Do not batch-fix multiple issues unless the user requests a batch and the issues are independently low risk.

## Final Output

For triage, report issue id/title, ownership, effort, readiness, risk, and recommended order.

For fixes, report:

- Issue id/title.
- Remote status changes or comments made.
- Files changed.
- Verification commands and browser checks.
- Residual risks or blocked items.
