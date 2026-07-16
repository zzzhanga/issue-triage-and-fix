# Execution Modes

Use this reference when the user requests one or more fixes, unattended execution, fast batch modification, or a human-verification handoff.

## Intent Routing

Choose exactly one repair mode after strict triage:

| User intent | Mode | AI verification | Commit timing | Feishu timing |
| --- | --- | --- | --- | --- |
| “修复 #123”、无人值守、自动依次处理 | `autonomous` | `standard` or eligible `lightweight` | after AI verification | keep original status during edits and verification; run `start-fix` only after commit; end at in-progress |
| 有人模式、快速批量修改、我来验证 | `assisted` | none during modification; later `deferred-to-user` evidence | before user verification | keep original status during edits; run `start-fix` only after user passes it |

Use `execution_policy.default_repair_mode`, default `autonomous`, only when the user did not express a mode. Never reinterpret preview-only language as repair authorization.

## One Run Authorization

Treat an explicit repair request as one current-run authorization:

- A named issue request freezes those exact issue ids.
- A batch request freezes the candidate ids returned by the current scan after assignee filtering.
- Exclude newly arriving issues until a later run.
- Bind every included issue to its own files, mode, completion actions, and `plan_fingerprint`.
- Reuse that run authorization to record each in-scope plan fingerprint without asking again.
- Stop only the affected issue when its plan expands to another repository, a different mode/action set, high risk, or new product confirmation.

Do not extend run authorization to push, comments, complete, terminate, unrelated files, another repository, or another task. Local deny-only configuration always wins.

## Autonomous Sequence

For each eligible issue:

1. Complete evidence intake, report quality, requirement/repository matching, and strict triage.
2. Generate and internally approve the in-scope plan with default actions `commit` and `start-fix`.
3. Keep the remote issue in its original state while modifying the plan-bound files.
4. Run `standard` checks or an eligible `lightweight` inspection.
5. If verification passes, create one issue-specific commit.
6. Re-read the current remote state, then run `start-fix`; `修复中` is the final state of this repair run.
7. Do not automatically run `resolve-for-acceptance`; continue to the next issue.
8. If any gate, verification, commit, or status update fails, leave the issue in its truthful current state, report it, and continue with independent issues.

## Assisted Sequence

For each eligible issue:

1. Complete the same evidence, report-quality, ownership, and risk gates as autonomous mode.
2. Generate an in-scope `deferred-to-user` plan. For native Feishu use only `commit` and delayed `start-fix`; do not include `resolve-for-acceptance`.
3. Keep the Feishu issue in its original status.
4. Modify only the plan-bound files and record implementation.
5. Do not run lint, tests, builds, browser verification, or AI diff/contract verification as a repair-verification substitute. Apply only mandatory low-cost formatting required by repository instructions; do not report it as Bug verification.
6. With `allow_deferred_user_verification: true`, create one issue-specific commit and record `verification_pending: true`.
7. Return a compact handoff list with issue id, commit, change summary, manual steps supplied by the issue, and current Feishu status.

After the user reports results:

- For each passed issue, record `--mode deferred-to-user --verified-by user` with at least one concrete passed check/evidence, then verify and execute `start-fix`. Record local handoff completion; do not execute `resolve-for-acceptance` automatically.
- For each failed issue, record the failure, keep the original Feishu status, repair it in a new scoped iteration/commit, and wait for another user result.
- If the user gives one batch result, apply it only to the explicitly named/frozen issues; do not infer results for omitted issues.

## Failure Isolation

Keep one issue per commit. Require a clean Git index before each commit. If unrelated staged changes, hard blockers, conflicting evidence, or a remote transition conflict affect one issue, stop that issue without discarding user work and continue only with issues whose files and state remain independent.

`resolve-for-acceptance` is a separate post-acceptance action for both modes. After later human acceptance, the user may update Feishu manually or explicitly authorize AI for exact issue ids. Before AI performs it, re-read the current status, verify the target transition, and confirm the implementation commit is still current. Never infer it from the original repair authorization.

At the end, report each issue as one of:

- `autonomous-committed-in-progress`
- `awaiting-user-verification`
- `user-verified-started`
- `user-verification-failed`
- `skipped-blocked`
