# Report Quality

Use this reference only after a user-selected issue enters fix-ready, after complete evidence intake and before final ownership, effort, readiness, or repair planning.

## Preview Does Not Run This Gate

For “只分诊/扫描/日报”, run `bugflow_runner.py preview`. Preview may point out suspected gaps from the currently available summary, but it does not write `issue.json`, calculate `report_quality.input_hash`, or set `sufficient|needs-clarification|conflicting`. Its notes must be labeled “疑似缺口/升级后需核对”, never as a final defect in the tester's report.

Only after the user selects a concrete issue should the agent read the full detail, every relevant comment/activity and decision-relevant attachment, write the strict intake, and run this gate. This preserves the gate for repair safety without paying its full cost for every candidate in a daily scan.

## Two Independent Gates

Do not confuse these checks:

- `evidence_fetch` answers whether full detail, comments, activities, and decision-relevant attachments were actually retrieved and inspected.
- `report_quality` answers whether the combined evidence now states a testable implementation target and acceptance boundary.

`evidence_fetch.status: complete` does not imply `report_quality.status: sufficient`. Run both gates. A repair plan, any verification mode, implementation, commit, or remote repair transition requires both evidence completeness and sufficient report quality.

## Sufficient Content

Judge the issue from all retrieved sources together. Require enough information to identify:

- observable actual result;
- expected result;
- reproduction steps or trigger conditions;
- concrete acceptance criteria;
- environment/version, account or permission role, and safe test data only when behavior depends on them.

Do not require the reporter or tester to prescribe code changes. An implementation suggestion is optional. If the goal is clear but the suggestion is wrong—for example, asking the frontend to `reverse` data that the backend contract should sort—keep report quality `sufficient`, then redirect ownership or reject the workaround.

Comments, inspected screenshots/videos, activity records, linked requirements, or an authoritative PRD can supply missing details. Do not mechanically demand written steps when an inspected video clearly shows the trigger, actual result, expected result, and acceptance boundary. Cite the evidence that supplied each fact.

## Status

Use exactly one status:

- `sufficient`: the combined evidence is implementable and independently testable.
- `needs-clarification`: one or more required facts are missing or ambiguous.
- `conflicting`: sources disagree on expected behavior, scope, or acceptance and no authoritative resolution exists.
- `unknown`: the assessment has not been completed.

Do not silently let a newer comment override the title, PRD, or another acceptance source. Record both sides and ask the appropriate tester/product owner to resolve the conflict. `sufficient` does not mean the issue belongs to the current repo or that a proposed implementation is correct.

## Bind The Assessment To Current Evidence

Every non-`unknown` assessment must be bound to the exact normalized evidence snapshot. After the refreshed detail, comments, activities, attachment inspection summaries, and `evidence_fetch` have been written to the current `issue.json`, run:

```powershell
python <skill-dir>\scripts\bugflow_runner.py report-quality-hash --issue BUG-123
```

Copy both returned values to `report_quality.hash_version` and `report_quality.input_hash`, then record the semantic assessment and `assessed_at`. The runner requires the current hash version plus at least one confirmed fact and evidence reference before accepting `sufficient`. If the description, requirements, comments, activities, inspected attachments, evidence findings, or hash algorithm version changes, the old assessment returns to `unknown`; re-read changed evidence, recompute the hash, and assess again. Never reuse an old verdict against a new evidence snapshot or hash version.

For an older artifact with no `hash_version`, `migrate-artifacts --issue <id>` may add current metadata only when its stored hash already matches the current evidence under the compatible algorithm. It still invalidates downstream triage. A mismatched hash or unsupported version requires a new semantic assessment; migration must not manufacture one.

## Canonical Shape

Store the assessment at top level in normalized `issue.json`:

```json
{
  "reproduction_steps": "Open the draft, then reopen it from the list.",
  "actual_result": "The first video frame is blank.",
  "expected_result": "The first frame is visible after reopening.",
  "environment": "Test environment, Chrome 126",
  "test_data": "Draft 29907; no credentials or secrets",
  "acceptance_criteria": "The first frame remains visible after save and reopen.",
  "implementation_suggestion": "Optional reporter suggestion",
  "report_quality": {
    "status": "needs-clarification",
    "assessed_at": "2026-07-13T10:10:00+08:00",
    "hash_version": "1",
    "input_hash": "<hash from report-quality-hash>",
    "facts": ["The inspected video shows a blank first frame at 00:08."],
    "evidence_refs": ["attachment repro.mp4@00:08"],
    "missing_fields": [
      {
        "field": "expected_result",
        "reason": "No source defines whether a cover image or the first video frame is expected.",
        "question": "保存并重新打开后，期望展示封面图还是视频第一帧？",
        "target": "产品/测试"
      }
    ],
    "conflicts": [],
    "questions": ["保存并重新打开后，期望展示封面图还是视频第一帧？"],
    "feedback_targets": ["产品", "测试"],
    "feedback_draft": "已确认视频在 00:08 出现空白首帧；请确认保存并重新打开后应展示封面图还是视频第一帧，确认前暂不进入修复。"
  }
}
```

Use empty strings/lists only when truly absent. Never place passwords, tokens, private account credentials, production personal data, or secret-bearing test data in the normalized issue or feedback draft.

## Clarification And Feedback

For `needs-clarification` or `conflicting`:

1. State the facts already confirmed and cite their sources.
2. List each missing field or conflict and why it blocks implementation or acceptance.
3. Ask exact, answerable questions; include sorting field/direction, scope, tie-breaker, visual reference, role, environment, or safe test record only when relevant.
4. Name the intended feedback target: tester, product, backend, or another owner.
5. Generate a concise local feedback draft and mark it not published.

Generate a ready-to-send draft only after `evidence_fetch` is complete and the assessment hash matches. While evidence is `partial|error`, suppress any explicit draft and report `blocked-by-evidence`; do not claim that all details, comments, or attachments were reviewed. A stale or unbound assessment is `blocked-by-assessment` and must not produce an external draft.

Do not create a repair fingerprint, enter implementation, or use lightweight/deferred-to-user verification while report quality is not `sufficient`. User approval cannot bypass this gate.

Publishing the clarification to Feishu is a separate external write. Require explicit current-task authorization for the exact draft and an enabled comment capability. Do not reuse a fix-plan approval, and never auto-publish clarification drafts from scheduled triage.

After several selected issues have completed this strict assessment, render their strict summary with explicit numbers:

```powershell
python <skill-dir>\scripts\bugflow_runner.py daily-existing --issue BUG-123 --issue BUG-456 --assignee <current-user-name-or-id> --report .bugflow/reports/daily-report.md
```

Do not run `daily --input` again for those issues: re-importing the original candidate payload can replace enriched comments, attachment summaries, and bound `report_quality` values with the earlier sparse snapshot.
