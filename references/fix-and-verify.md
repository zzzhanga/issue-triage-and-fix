# Fix And Verify

Use this reference when the user requests repair or when a triaged issue is an allowed `auto-fix-candidate`.

## Preconditions

Before editing:

- Confirm the issue is selected.
- Read repository guidance and project config.
- Inspect relevant code and tests.
- Check the worktree for existing changes and avoid reverting user work.
- Confirm whether remote status may be changed to in progress.

Do not repair issues classified as `hard`, `blocked`, `needs-confirmation`, or `not-current-repo` without explicit approval for that issue.

## Controlled Fix-One Commands

Use the runner to keep fix-one work resumable:

```powershell
python <skill-dir>\scripts\bugflow_runner.py plan-fix --issue BUG-123
python <skill-dir>\scripts\bugflow_runner.py plan-fix --issue BUG-123 --approved
python <skill-dir>\scripts\bugflow_runner.py record-implementation --issue BUG-123 --summary "..." --files src/file.ts
python <skill-dir>\scripts\bugflow_runner.py record-verification --issue BUG-123 --command "pnpm exec eslint src/file.ts => passed" --browser passed --browser-note "Route checked"
python <skill-dir>\scripts\bugflow_runner.py close-local --issue BUG-123 --summary "Fixed locally and verified"
```

`plan-fix` must run before code edits. If it writes `fix-plan.md` with `status: blocked`, stop and ask for the missing approval or product/customer confirmation. Use `--approved` only when the user explicitly selected that issue for repair or approved the plan.

## Start Work

If `remote_status_policy.default_change_to_in_progress` is true and the `start_fix` transition is allowed, move the issue to the configured in-progress status before code edits. Otherwise, mention that no status change was made.

If the status update fails, continue only when local repair is still useful and report the failure.

## Code Repair

- Keep changes scoped to the issue.
- Prefer existing project components, helpers, mocks, and style patterns.
- Avoid broad refactors unless required for the bug.
- Add or update tests only when behavior risk justifies them.
- Do not introduce new global styles for page-specific issues unless the shared component is the true source.
- After edits, run `record-implementation` with the changed files and a concise summary. This command records what happened; it does not inspect git or change remote state.

## Local Verification

Run the verification commands from project config. Use targeted commands first:

- formatter for touched files
- lint for touched files
- unit/regression tests for affected modules
- stylelint for touched styles
- build only when configured or risk warrants it

If a command cannot run, capture the reason and continue with other applicable verification.

After verification, run `record-verification`. Mark failed or blocked verification explicitly with `--failed`, `--blocked`, or `--browser failed/blocked`.

## Browser Verification

For user-visible UI behavior, read `browser-verification.md`.

Verify the route or workflow from the issue:

1. Start or reuse the local dev server.
2. Open the configured app URL.
3. Complete login using the configured login policy.
4. Reproduce the original scenario.
5. Confirm the fixed behavior.
6. Capture screenshots or a concise evidence note when useful.

Browser verification is mandatory for styles, tables, modals, drawers, upload, rich text, routing, and visible interaction bugs unless the user says not to verify.

## Closing Comment

When comments are allowed, post a concise comment:

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

- local verification passed,
- browser verification passed when required,
- project config allows the transition or the user explicitly approves it.

If verification is partial, leave the issue in progress and comment with the remaining risk.

Use `close-local` only after `verification.md` is `done`. Use `--allow-partial` only when the final answer clearly states verification is partial and no remote resolved/completed transition was made.

## Final Response

Report:

- issue id/title
- status changes and comments made
- files changed
- verification commands
- browser verification result
- residual risk
