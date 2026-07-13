---
name: issue-triage-and-fix
description: 飞书 Project 缺陷快速扫描与受控修复工作流，默认只关注指派给当前用户的工单；“只分诊/扫描/日报”先走轻量 preview，用户选中具体工单后再完整读取详情、评论及图片/视频证据，严格评估是否足以实施和验收。原生支持飞书 MCP/OpenAPI，也可把 Jira、TAPD、禅道、GitLab Issues 等平台导出的 JSON 标准化后过滤。Use when Codex needs fast provisional triage, evidence-backed fix readiness, report-quality clarification drafts, ownership/risk classification, rejection or redirection of unreasonable/cross-owner fixes, or—after approval of an exact plan—repair one issue with standard or lightweight verification and execute the plan-listed completion actions. Do not imply native adapters for non-Feishu trackers.
---
# 缺陷分诊与受控修复

## 默认边界

把工作流明确拆成两层：

- `preview/scan`：默认用于“只分诊”“扫描一下”“生成日报”等批量只读请求。只过滤指派给当前用户的候选工单，读取列表字段、已有摘要和做初步判断所必需且可快速取得的关键证据，输出**暂定**归属、风险、优先级、推荐顺序和疑似信息缺口。不要为每个候选写完整工件，不要计算 `report_quality.input_hash`，不要运行构建、lint、测试或浏览器，也不要修改代码、Git 或远程状态。默认不搜索实现代码；仅当一个精确 `rg` 查询很可能立即解决仓库/前后端归属时，允许做一次有边界的只读搜索，不展开实现评审。
- `fix-ready`：仅在用户选中具体工单，或明确要求对具体工单做严格评估/准备修复时进入。此层才读取完整详情、所有相关评论/活动和决策相关附件，写入 `issue.json`，绑定并严格评估 `report_quality`，完成需求-仓库匹配、最终分诊、修复计划、验证、提交和获授权的状态动作。

`preview/scan` 的快，不等于可以把标题当最终证据。若现有摘要不足，只写“疑似缺少什么/升级后需核对什么”；不要把暂定结果写成已核对结论，也不要生成声称已读全资料的对外反馈草稿。除非用户明确选中具体工单，否则停在 preview。

只把飞书 Project 视为原生适配平台。对 Jira、TAPD、禅道、GitLab Issues 或其他平台，只处理用户提供或项目工具生成的 JSON 导出；先用 `scripts/normalize_issue_payload.py` 或 runner 标准化，不要声称能直接查询、评论或流转这些平台。

把这个 Skill 当作编排入口：runner 负责配置、标准化、工件和门禁，不负责自动编辑业务代码、push 或调用远程状态更新接口。运行时明确区分 `--repo-root`（配置、代码路径和 Git）与 `--artifact-root`（逐工单工件）；不要依赖进程当前目录猜仓库。

标题和列表行只用于发现候选工单，不能直接作为最终分诊证据。最终分诊前必须按 `references/evidence-intake.md` 获取完整详情、读取入站评论/活动记录、枚举并实际检查决策相关附件；图片要看原图，视频要查看覆盖问题发生过程的关键帧/片段，不能只看缩略图。证据无法读取时明确标为 `partial|error` 并降级为待确认，不得给出高置信、`easy`、`auto-fix-candidate`、修复计划或轻量验证结论。

把“证据是否读全”和“内容是否足以实施/验收”作为两道独立门禁。证据完整后仍须按 `references/report-quality.md` 评估可观察的实际结果、期望结果、复现/触发条件和验收标准；条件相关时再要求环境、账号角色或安全测试数据。测试不需要指定代码实现，评论、已检查的视频/图片或权威 PRD 可以补足描述。`report_quality` 非 `sufficient` 时生成精确的本地反馈草稿并阻断修复、修复计划和轻量验证。

## 批准与授权

统一使用以下谓词，不要把分类、配置或命令参数本身当作用户授权：

- `fix_approved(issue)`：用户在当前任务中明确要求修复该编号工单，或明确批准该工单的具体修复计划。`--approved` 只记录这份批准，不会创造授权。
- `completion_action_authorized(issue, action)`：该动作明确列在当前已批准计划的 `completion_actions` 中，并且项目配置允许、本地覆盖未禁用；状态动作还必须核验目标状态 id、transition 和来源状态。批准这份完整计划后，列出的提交、`start-fix`、`resolve-for-acceptance` 或评论可以连续执行，无需修复后重复询问。

项目配置可以声明团队能力；本地覆盖只能把权限从 `true` 收紧为 `false`，不能把项目配置的 `false` 放宽为 `true`。当前用户批准只对当前任务中的具体工单和动作生效，不持久化，也不覆盖本地 deny。

因此：

- `auto-fix-candidate` 只是排序/候选标签，不代表可以改代码。
- `auto_fix_allowed: false` 时仍可在 `fix_approved(issue)` 成立后进行人工受控修复，但不得自动选择工单。
- `--approved` 不能绕过未解决的产品确认、非当前仓库归属、缺失上游工件、验证失败或 Git 隔离门禁。
- `--approved` 也不能绕过未完成的详情、评论或附件证据检查；读取已有评论属于只读分诊，发布评论仍是单独的远程动作。
- `--approved` 不能绕过 `report_quality: needs-clarification|conflicting|unknown`。信息不足时只生成本地反馈草稿；发布这份澄清评论需要对精确草稿的独立授权，不能复用修复计划授权。
- 本地提交仍须用当前计划指纹通过 `--authorized <plan_fingerprint>`，且 `commit` 必须列在已批准计划中；这记录同一份计划授权，不是第二轮确认。
- 评论、改为“修复中”、解决、完成、终止仍是不同动作；只有已批准计划明确列出的动作可连续执行，未列出的动作另行批准。

## 依赖与配置

先在项目认可的虚拟环境中解析 Python 3.10+ 解释器为 `<python>`，再安装 runner 依赖；不要静默修改全局 Python：

```powershell
<python> -m pip install -r <skill-dir>\requirements.txt
```

读取配置栈时遵循：Skill 安全默认值 → 项目配置 → 本地 deny-only 覆盖 → 当前任务的单次批准。若项目尚未配置，阅读 `references/project-config.md`，然后运行：

```powershell
<python> <skill-dir>\scripts\bugflow_runner.py init-project --platform feishu-project --project-name my-project --project-key my-feishu-project-key
<python> <skill-dir>\scripts\bugflow_runner.py doctor
```

不要把真实密码、token、cookie、session secret 或 MCP URL 写入配置、JSON、工件或提交。使用环境变量名或已有连接。

`field_mapping` 只是本地字段映射，不代表远端项目确实存在这些 key。只有用精确字段发现验证过的 key 才写入 `field_verification.verified_keys/source/verified_at`；Preview/Fix-ready MQL 的可选字段只使用已验证 key。可用 `query_policy.requirement_ids` 或命令行 `--requirement-id` 限定需求范围；除非需求字段和 pushdown 语义都已验证，MQL 只做宽查询，runner 再做严格后过滤。

## 两层流程

### 1. Preview / Scan（默认）

1. 读取当前仓库的 `AGENTS.md` 和 `.codex/bugflow/` 配置；飞书用 `feishu-mql --profile preview` 生成最小 MQL，其他平台只接受 JSON 导出。
2. 只拉取并保留指派给当前用户/配置别名的候选工单。即使查询用了 `current_login_user()`，也要对返回记录做负责人后过滤：每条必须有可读负责人，批次必须存在共同的当前用户身份；混入其他负责人且无法证明共同身份时 fail closed。非飞书 JSON 无法解析当前负责人时停止并要求配置 `query_policy.assigned_to`/`assignee_aliases` 或传 `--assignee`；只有用户明确要求全量时才允许 `--include-all-assignees`。
3. 使用候选列表已有字段、可用摘要和少量关键证据做初筛；只有某条判断离不开一个容易读取的摘要/关键附件时才补取它，不要默认逐条分页读全评论、活动和媒体。运行 `preview`，在内存中标准化、过滤和初步分类：

   ```powershell
   <python> <skill-dir>\scripts\bugflow_runner.py preview --input feishu-bugs.json --report .bugflow/reports/daily-preview.md
   ```

4. 输出暂定归属、风险、优先级、推荐顺序、判断依据和“疑似信息缺口/升级后需核对项”。缺少证据时写 `未核对/待进入 fix-ready`，不要给 `evidence_fetch: complete`、`report_quality: sufficient`、高置信可修复或对外反馈草稿。
5. Preview 不写 `.bugflow/issues/<issue>/`，不生成 `issue.json`、`requirement-match.md`、`triage.md` 或修复计划；不计算 `report-quality-hash`，除上述单次精确归属查询外不搜索实现代码，不运行格式化/lint/测试/构建/浏览器，不修改代码、Git、评论或远程状态。用户未选中具体工单时到此结束。

### 2. Fix-ready（选中具体工单后）

1. 直接复用 preview 返回的编号/id 定位用户选中的工单，不要重新扫描全部候选。需要重新查询时使用 `feishu-mql --profile fix-ready`，且只加入已远端验证的可选字段。只对该工单执行 `references/evidence-intake.md`：获取 full detail，分页读取完整历史评论和相关活动；汇总字段、富文本和评论里的附件，在安全的本地目录实际检查决策相关图片/视频/文档。把脱敏后的 `comments`、`activities`、附件检查摘要及 `evidence_fetch` 完整度并入输入；不得把临时下载 `sign`、signed URL 或鉴权头落盘。
2. 按 `references/fetch-issues.md` 标准化该工单，并写入 `.bugflow/issues/<safe-issue-key>/issue.json`。默认脱敏 `raw`；证据未显式完整时只能生成受阻的严格评估，不得进入修复。
3. 运行 `report-quality-hash --issue <编号>`，再按 `references/report-quality.md` 汇总描述、评论、附件、活动和需求证据；把返回的 `hash_version` 和 `input_hash` 原样写入 `report_quality`，同时记录 `assessed_at`、事实和来源。证据或哈希版本变化会使旧评估失效，必须重新计算并评估。若状态为 `needs-clarification|conflicting|unknown`，列出已确认事实、缺失/冲突、反馈对象和精确问题并停止修复路径。只有证据完整且评估绑定当前快照时，才可生成标为未发布的对外反馈草稿。
4. 按 `references/requirement-repo-mapping.md` 匹配需求与仓库，再按 `references/triage-issues.md` 形成最终归属、工作量、准备度、风险和推荐顺序。明显属于后端/API 契约、其他仓库或方案本身不合理的工单标为转交/拒修，不做前端兜底；例如不要为“列表接口应倒序”在前端 `reverse` 数据。错误的实现建议不降低已经明确的工单信息质量，但必须纠正归属。
5. 如果需要汇总已经完成严格评估的本次工单，使用 `daily-existing --issue <编号> --assignee <当前用户名称或ID>`；不要再次用 `daily --input` 导入，否则可能用原始输入覆盖已补齐的评论、附件摘要或 `report_quality`。已有工件不能重新信任 `current_login_user()` 查询上下文，因此必须传具体负责人/配置别名，或由用户显式批准 `--include-all-assignees`：

   ```powershell
   <python> <skill-dir>\scripts\bugflow_runner.py daily-existing --issue BUG-123 --assignee <current-user-name-or-id> --report .bugflow/reports/daily-report.md
   ```

6. 仓库归属明确、`evidence_fetch.status` 为 `complete`、`report_quality.status` 为 `sufficient` 且所有确认阻塞已解决后，先运行未批准的 `plan-fix` 获取 `plan_fingerprint`。计划必须声明文件范围、`standard|lightweight` 验证模式和批准后可连续执行的 `completion_actions`。用户批准后，用完全相同的参数和 `--approved <plan_fingerprint>` 重建计划，再修改代码并记录实现。
7. 默认运行计划生成的 `required_checks`。Standard 模式必须逐项命中并通过这些具名检查；任意一条无关的 `passed` 命令不能代替 lint/test/build/browser 等计划要求。记录 `verified_by: user|agent|ci`、自动生成的 UTC 时间和来源说明。只有高置信、当前仓库、明确前端归属、低/中风险且难以可靠自动验证的修复，才可使用计划已批准的 `lightweight` 模式；记录难以自动验证的原因、至少一项代码/差异/契约检查证据和剩余风险。任何失败、阻塞、高风险、归属不清、入站证据不完整或工单信息不足/冲突都不能轻量放行。
8. 只有当前 verification 有效且满足必需检查时，才允许闭环或提交。提交前确认 Git index 预先为空；如果已有暂存内容，停止并报告，不要把它们混入提交。只接受真实、单个、修复相关的 literal file path；不要传 `.`、目录、glob 或仓库外路径。
9. verification 完成后，连续执行已批准计划中列出的本地提交和飞书状态动作；未列出的动作不执行。runner 不 push。

远程状态更新返回错误或超时时，不要盲目重放：先重新读取当前工单状态；若已是目标状态，按幂等成功记录；若仍是原状态，只重试一次；若已变成其他状态，立即停止并报告并发变化。

## 工件与失效

Preview/scan 默认不创建逐工单工件；它最多原子写入一个受保护的 Markdown 汇总报告，例如 `.bugflow/reports/daily-preview.md`。`--report` 不能写进逐工单工件、`.codex`、`.git` 或配置/代码路径。只有进入 fix-ready 后才使用 `.bugflow/issues/<safe-issue-key>/`；已有安全 canonical 编号保持原目录名，只在清洗或截断编号时附加短哈希。严格链路为：

```text
issue-intake -> requirement-match -> triage-report -> fix-plan -> implementation -> verification -> closure
```

阅读 `references/bugflow-artifacts.md` 了解 schema、状态和命令。把 `.bugflow/` 默认加入宿主仓库 `.gitignore`。

不得让旧结论在上游变化后继续显示为有效。runner 为工件写入 schema/runner 版本元数据并原子替换输出；刷新且发现 `issue.json` 内容变化时使下游工件失效。兼容的纯元数据升级可运行 `migrate-artifacts --issue <编号>`，但它仍要求重新生成分诊；证据哈希不匹配或语义规则变化时不得自动迁移结论。更新需求匹配、分诊、修复计划或实现内容时，也要把所有下游工件标为待重建。verification 失效后，closure 和 commit 门禁也必须失效。不要只依赖文件是否存在。

## 结构化验证

`verification.md` 可通过两种已批准模式成为 `done`：

- `standard`：计划列出的每项 `required_checks` 都有对应的结构化 `passed` 结果；可见 UI 问题必须包含浏览器检查。无关命令即使 passed 也不能放行。
- `lightweight`：计划已明确批准该模式，修复把握为 `high`，分诊置信度为 `high`，并记录自动验证不可行原因、至少一项人工检查证据和剩余风险；允许把不适用的命令/浏览器检查记为 `skipped`。
- verification 绑定的 implementation 内容指纹仍然匹配。

有 `failed` 或未获明确豁免的 `blocked` 时，不得提交、标记解决/完成或把 closure 写成成功。不要使用跳过验证或部分闭环参数绕过默认安全路径，除非用户明确批准该例外并在最终回复中显著披露。

## 常用命令

飞书查询与快速只读扫描：

```powershell
<python> <skill-dir>\scripts\bugflow_runner.py doctor
<python> <skill-dir>\scripts\bugflow_runner.py feishu-mql --profile preview --json
<python> <skill-dir>\scripts\bugflow_runner.py preview --input feishu-bugs.json --report .bugflow/reports/daily-preview.md
```

导入任意平台导出的 JSON：

```powershell
<python> <skill-dir>\scripts\bugflow_runner.py preview --input exported-issues.json --platform exported-json --assignee <current-user-name-or-id> --requirement-id <optional-requirement-id> --report .bugflow/reports/daily-preview.md
```

用户选中具体工单后，进入 fix-ready 并生成严格工件：

```powershell
<python> <skill-dir>\scripts\bugflow_runner.py fetch-json --input selected-issue.json --assignee <current-user-name-or-id>
<python> <skill-dir>\scripts\bugflow_runner.py report-quality-hash --issue BUG-123
<python> <skill-dir>\scripts\bugflow_runner.py triage --issue BUG-123
<python> <skill-dir>\scripts\bugflow_runner.py daily-existing --issue BUG-123 --assignee <current-user-name-or-id> --report .bugflow/reports/daily-report.md
```

不要默认加 `--retain-raw`；只有满足 raw 显式保留条件时才使用它。

为单个工单生成具体计划：

```powershell
<python> <skill-dir>\scripts\bugflow_runner.py plan-fix --issue BUG-123 --files src/file.ts --completion-action commit --completion-action start-fix --completion-action resolve-for-acceptance
```

把输出的计划和 `plan_fingerprint` 交给用户。用户明确批准该计划后，使用完全相同的范围参数重新运行：

```powershell
<python> <skill-dir>\scripts\bugflow_runner.py plan-fix --issue BUG-123 --files src/file.ts --completion-action commit --completion-action start-fix --completion-action resolve-for-acceptance --approved <plan_fingerprint>
<python> <skill-dir>\scripts\bugflow_runner.py record-implementation --issue BUG-123 --summary "Scoped fix" --files src/file.ts
<python> <skill-dir>\scripts\bugflow_runner.py record-verification --issue BUG-123 --verified-by agent --verification-note "本次本地验证" --check "lint=passed: pnpm exec eslint src/file.ts" --check "browser=passed: affected route verified" --browser passed --browser-note "Affected route verified"
<python> <skill-dir>\scripts\bugflow_runner.py close-local --issue BUG-123 --summary "Verified locally"
```

高把握但难以自动验证的低/中风险前端修复，在首次和批准后的 `plan-fix` 中都加 `--verification-mode lightweight`，然后记录轻量证据：

```powershell
<python> <skill-dir>\scripts\bugflow_runner.py record-verification --issue BUG-123 --mode lightweight --confidence high --verified-by agent --verification-note "精确 diff 与契约审查" --exemption-reason "缺少可重复的外部回调环境" --evidence "已审查精确 diff、调用边界和错误分支" --browser skipped --browser-note "需在真实回调环境验收"
```

如果已批准计划列出 `commit`，验证完成后直接运行，不再重复询问：

```powershell
<python> <skill-dir>\scripts\bugflow_runner.py commit-fix --issue BUG-123 --files src/file.ts --authorized <plan_fingerprint>
```

## 按需读取

- `references/project-config.md`：配置栈、deny-only 合并、安全默认值和依赖。
- `references/fetch-issues.md`：飞书/导出 JSON 获取、标准化和 raw 脱敏。
- `references/evidence-intake.md`：完整详情、历史评论、活动记录和图片/视频附件的获取、检查、完整度与安全门禁。
- `references/report-quality.md`：工单可实施性/可验收性、信息冲突、精确澄清问题和本地反馈草稿。
- `references/feishu-project.md`：飞书 Project 原生适配、字段与 MQL。
- `references/requirement-repo-mapping.md`：需求与仓库匹配。
- `references/triage-issues.md`：分诊枚举、证据和排序。
- `references/bugflow-artifacts.md`：工件链、内容指纹和失效规则。
- `references/fix-and-verify.md`：批准后的单工单修复、结构化验证和 Git 隔离。
- `references/browser-verification.md`：可见交互验证与登录策略。
- `references/status-workflow.md`：远程动作授权谓词与状态流转。
- `references/scheduled-automation.md`：只读定时分诊；不要硬编码模型或运行时。

## 输出

Preview 输出优先使用紧凑表格，包含工单编号/标题、优先级、暂定归属/风险、推荐顺序、现有判断依据和疑似信息缺口，并在标题或表头明确标注“快速扫描/暂定结论”。不要塞入逐工单命令日志、完整工件状态或未经核对的对外反馈草稿。

Fix-ready 的严格分诊输出再包含关联需求、证据完整度与关键证据、工单信息质量、仓库匹配与置信度、最终归属、工作量、准备度、风险、推荐顺序和待确认问题。明确区分“已检查附件内容”和“只拿到附件元数据”；对证据完整且信息不足/冲突的工单列出已知事实、缺失或冲突、反馈对象和未发布反馈草稿；证据不完整时只给内部阻塞提示。用中文展示结论，不暴露无助于用户决策的内部枚举或命令日志。

修复输出包含工单编号/标题、用户批准的动作、修改文件、结构化验证结果、浏览器证据、提交/远程动作实际结果和剩余风险。未执行的动作明确写“未执行”，不要暗示已获授权或已完成。
