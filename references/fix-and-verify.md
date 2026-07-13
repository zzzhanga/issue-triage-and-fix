# Fix And Verify

Use this reference only after the user explicitly requests repair of a concrete issue or approves that issue's fix plan. An `auto-fix-candidate` label alone is never approval.

## Contents

- [Preconditions And Approval](#preconditions-and-approval)
- [Controlled Fix-One Commands](#controlled-fix-one-commands)
- [Start Work](#start-work)
- [Code Repair](#code-repair)
- [Structured Verification](#structured-verification)
- [Git Isolation And Local Commit](#git-isolation-and-local-commit)
- [Browser Verification](#browser-verification)
- [Remote Closure](#remote-closure)
- [Finish Status](#finish-status)
- [Final Response](#final-response)

## Preconditions And Approval

Before editing:

- Confirm the issue is selected.
- Confirm `fix_approved(issue)`: the current user explicitly selected the issue for repair or approved the concrete plan.
- Read repository guidance and project config.
- Inspect relevant code and tests.
- Check the worktree for existing changes and avoid reverting user work.
- Confirm whether remote status may be changed to in progress.
- Put every intended completion action (`commit`, `start-fix`, `resolve-for-acceptance`, comment, complete, or terminate) in the exact plan shown to the user.

Do not repair any issue without `fix_approved(issue)`. Resolve every concrete blocker first. `--approved` must not bypass unresolved product/customer confirmation, `not-current-repo` ownership, missing upstream artifacts, failed verification, or Git isolation. Narrow and re-triage a `hard` or high-risk issue before repair; plan approval alone cannot bypass that gate.

## Controlled Fix-One Commands

Use the runner to keep fix-one work resumable:

```powershell
python <skill-dir>\scripts\bugflow_runner.py plan-fix --issue BUG-123 --files src/file.ts --completion-action commit --completion-action start-fix --completion-action resolve-for-acceptance
python <skill-dir>\scripts\bugflow_runner.py plan-fix --issue BUG-123 --files src/file.ts --completion-action commit --completion-action start-fix --completion-action resolve-for-acceptance --approved <plan_fingerprint>
python <skill-dir>\scripts\bugflow_runner.py record-implementation --issue BUG-123 --summary "..." --files src/file.ts
python <skill-dir>\scripts\bugflow_runner.py record-verification --issue BUG-123 --verified-by agent --verification-note "local run" --check "lint=passed: pnpm exec eslint src/file.ts" --browser passed --browser-note "Route checked"
python <skill-dir>\scripts\bugflow_runner.py commit-fix --issue BUG-123 --files src/file.ts --authorized <plan_fingerprint>
python <skill-dir>\scripts\bugflow_runner.py close-local --issue BUG-123 --summary "Fixed locally and verified"
```

Run `plan-fix` without `--approved` first. It prints a `plan_fingerprint` bound to the issue, triage result, files, route, verification mode, notes, and completion actions. Show that exact plan to the user. After approval, rerun with identical arguments plus `--approved <plan_fingerprint>`. Approval covers only the listed actions, but those actions may run consecutively without asking again after the fix.

Record implementation only for the exact literal files listed in the approved plan. If code search changes the file scope, regenerate the plan and obtain approval for the new fingerprint before editing those files.

## Start Work

Move the issue to in-progress only when `completion_action_authorized(issue, start-fix)` is true: `start-fix` is in the approved plan, project policy allows it, the local deny-only override does not disable it, and the target status id is verified. Otherwise, mention that no status change was made.

If the status update fails, continue only when local repair is still useful and report the failure.

## Code Repair

- Keep changes scoped to the issue.
- Prefer existing project components, helpers, mocks, and style patterns.
- Avoid broad refactors unless required for the bug.
- Add or update tests only when behavior risk justifies them.
- Do not introduce new global styles for page-specific issues unless the shared component is the true source.
- Pure frontend style/layout/display bugs may be ranked as low-risk candidates, but still require `fix_approved(issue)` before edits.
- Reject or redirect fixes that belong at the data source or API contract. Do not reverse a list response in the frontend merely because the endpoint returns the wrong order, unless client-side ordering is explicitly part of the contract or an approved temporary workaround.
- After edits, run `record-implementation` with the changed files and a concise summary. This command records what happened; it does not inspect git or change remote state.

## Structured Verification

Run the verification commands from project config. Use targeted commands first:

- formatter for touched files
- lint for touched files
- unit/regression tests for affected modules
- stylelint for touched styles
- build only when configured or risk warrants it

Record every applicable check with:

- command or check type
- result: `passed`, `failed`, or `blocked`
- concise evidence or failure reason

### Standard mode

`plan-fix` derives and stores exact `required_checks` from the planned files, configured test/build policy, route, and visible issue signals. Every required check must have a matching structured `passed` result. Use `--check "lint=passed: ..."`, `--check "test=passed: ..."`, and so on; exact configured `--command "... => passed"` values may also be recognized. An unrelated successful command, empty command list, browser default such as `not-required`, or prose without a result must not produce `status: done`. If a required check cannot run, mark it `blocked` and record the reason; do not silently omit it.

Every successful or attempted record must declare `--verified-by user|agent|ci`. The runner records UTC `verified_at` automatically; use `--verification-note` for a user-confirmation context, CI run id, or agent-run summary. This is provenance, not proof by itself.

### Lightweight mode

Use lightweight verification only when all conditions hold:

- The approved fix plan declares `--verification-mode lightweight`.
- Repository match and triage confidence are `high`, ownership is `frontend-owned`, risk is low/medium, effort is easy/medium, and no confirmation is unresolved.
- The change is well understood and reversible, but a reliable automated/browser path is unavailable or disproportionate.
- Record `--confidence high`, a concrete `--exemption-reason`, at least one `--evidence` item from scoped diff/code/API-contract inspection, and residual risk.
- No attempted check is `failed`, `blocked`, or invalid. `skipped` is acceptable only with its reason.

Example:

```powershell
python <skill-dir>\scripts\bugflow_runner.py record-verification --issue BUG-123 --mode lightweight --confidence high --verified-by agent --verification-note "scoped diff and contract review" --exemption-reason "No deterministic external callback fixture" --evidence "Reviewed the exact diff, null/error branches, and callback contract" --browser skipped --browser-note "Requires acceptance in the real callback environment" --residual-risk "Acceptance test still recommended"
```

Never use lightweight mode for high-risk, cross-owner, backend-owned, unclear, destructive, auth/payment/data-loss, or failed fixes.

After verification, run `record-verification`. Mark failed or blocked verification explicitly with `--failed`, `--blocked`, or `--browser failed/blocked`.

Bind verification to the current implementation content fingerprint. If the implementation or any upstream artifact changes, invalidate verification and rerun it before commit or closure.

## Git Isolation And Local Commit

Create one local commit when `commit` appears in the approved plan and the current standard or lightweight verification artifact is `done`:

```powershell
python <skill-dir>\scripts\bugflow_runner.py commit-fix --issue BUG-123 --files src/file.ts src/file.scss --authorized <plan_fingerprint>
```

Rules:

- Before staging, inspect the Git index. If any path is already staged, abort and report it; do not commit or unstage the user's work.
- Stage only literal fix-related files passed with `--files`.
- Resolve all file and Git operations from the explicit/configured repository root, never from the process CWD; artifact storage may be elsewhere.
- Require the commit file set to match the files recorded by the approved implementation exactly.
- Reject `.`, directories, glob patterns, deleted/renamed ambiguity not represented by an exact path, and paths outside the repository.
- Do not stage `.bugflow/` unless the project explicitly commits artifacts.
- Do not stage unrelated user changes.
- Do not push; the runner does not implement push.
- Use the configured `git_policy.commit_message_template`, defaulting to `fix({issue}): {title}`.
- Always pass the approved plan's `--authorized <plan_fingerprint>`. This reuses the plan authorization and does not require a second confirmation.
- The runner has no force bypass. Regenerate stale artifacts and complete the plan-approved standard or lightweight verification before committing.

## Browser Verification

For user-visible UI behavior, read `browser-verification.md`.

Verify the route or workflow from the issue:

1. Check the configured local URL and reuse an existing dev server when possible.
2. Prefer a matching local-project tab already open in the user's Chrome; otherwise reuse an in-app browser tab or open a new one according to `browser_verification.surface_priority`.
3. Reuse the selected browser's existing login state. Never copy authentication storage between browsers; if necessary, ask the user to sign in in the selected browser or use an approved configured test path.
4. Reproduce the original scenario.
5. Confirm the fixed behavior.
6. Capture screenshots or a concise evidence note when useful.

Browser verification is mandatory for styles, tables, modals, drawers, upload, rich text, routing, and visible interaction bugs unless the approved plan uses a valid lightweight exception with a concrete reason and residual risk.

## Remote Closure

Post a comment only when `completion_action_authorized(issue, comment)` is true. Use a concise body:

```markdown
Fix summary:
- ...

Verification:
- ...

Residual risk:
- ...
```

Mention mock-only or local-only verification clearly.

## Finish Status

Move the issue to resolved-for-acceptance, completed, or terminated only when:

- standard verification passed, or plan-approved lightweight verification is `done`,
- browser verification passed when required and not validly exempted by lightweight mode,
- `completion_action_authorized(issue, exact_transition)` is true.

If verification is partial, leave the issue in progress and comment with the remaining risk.

Use `close-local` only after current `verification.md` is `done`. Use `--allow-partial` only when the user explicitly approved partial local closure, the final answer clearly states verification is partial, and no remote resolved/completed transition was made.

## Final Response

Report:

- issue id/title
- status changes and comments made
- files changed
- verification commands
- browser verification result
- residual risk
