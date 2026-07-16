---
name: issue-triage-and-fix
description: Use when Codex 需要快速扫描或严格分诊飞书 Project 缺陷、仅查看指派给当前用户的工单、检查详情/评论/图片/视频证据、判断工单信息是否足以实施与验收、生成澄清草稿、纠正前后端或跨仓归属，或按单次/批次授权以“AI 验证并提交后标记修复中”的无人值守模式或“AI 快速修改、人工验证后标记修复中”的有人模式修复多个 Bug；也适用于 Jira、TAPD、禅道、GitLab Issues 等平台导出的 JSON，但不应声称原生连接非飞书平台。
---
# 缺陷分诊与受控修复

## 默认边界

把工作流明确拆成两个入口层：

- `preview/scan`：默认用于“只分诊”“扫描一下”“生成日报”等批量只读请求。只过滤指派给当前用户的候选工单，读取列表字段、已有摘要和做初步判断所必需且可快速取得的关键证据，输出**暂定**归属、风险、优先级、推荐顺序和疑似信息缺口。不要为每个候选写完整工件，不要计算 `report_quality.input_hash`，不要运行构建、lint、测试或浏览器，也不要修改代码、Git 或远程状态。默认不搜索实现代码；仅当一个精确 `rg` 查询很可能立即解决仓库/前后端归属时，允许做一次有边界的只读搜索，不展开实现评审。
- `fix-ready`：仅在用户选中具体工单，或明确要求对具体工单做严格评估/准备修复时进入。此层才读取完整详情、所有相关评论/活动和决策相关附件，写入 `issue.json`，绑定并严格评估 `report_quality`，完成需求-仓库匹配、最终分诊、修复计划、验证、提交和获授权的状态动作。

`preview/scan` 的快，不等于可以把标题当最终证据。若现有摘要不足，只写“疑似缺少什么/升级后需核对什么”；不要把暂定结果写成已核对结论，也不要生成声称已读全资料的对外反馈草稿。除非用户明确选中具体工单，否则停在 preview。

只把飞书 Project 视为原生适配平台。对 Jira、TAPD、禅道、GitLab Issues 或其他平台，只处理用户提供或项目工具生成的 JSON 导出；先用 `scripts/normalize_issue_payload.py` 或 runner 标准化，不要声称能直接查询、评论或流转这些平台。

把这个 Skill 当作编排入口：runner 负责配置、标准化、工件和门禁，不负责自动编辑业务代码、push 或调用远程状态更新接口。运行时明确区分 `--repo-root`（配置、代码路径和 Git）与 `--artifact-root`（逐工单工件）；不要依赖进程当前目录猜仓库。

标题和列表行只用于发现候选工单，不能直接作为最终分诊证据。最终分诊前必须按 `references/evidence-intake.md` 获取完整详情、读取入站评论/活动记录、枚举并实际检查决策相关附件；图片要看原图，视频要查看覆盖问题发生过程的关键帧/片段，不能只看缩略图。证据无法读取时明确标为 `partial|error` 并降级为待确认，不得给出高置信、`easy`、`auto-fix-candidate`、修复计划或轻量验证结论。

把“证据是否读全”和“内容是否足以实施/验收”作为两道独立门禁。证据完整后仍须按 `references/report-quality.md` 评估可观察的实际结果、期望结果、复现/触发条件和验收标准；条件相关时再要求环境、账号角色或安全测试数据。测试不需要指定代码实现，评论、已检查的视频/图片或权威 PRD 可以补足描述。`report_quality` 非 `sufficient` 时生成精确的本地反馈草稿并阻断修复、修复计划和轻量验证。

## 修复运行模式

进入 `fix-ready` 后按用户意图选择模式；不要把“快速分诊”和“快速修改”混为一谈：

- `autonomous`（默认）：用于“修复 #123”“无人值守”“自动依次处理”。AI 逐 Bug 严格分诊、修改、运行 `standard` 或合规的 `lightweight` 验证、创建单独 commit；commit 成功后才把飞书改为“修复中”，并以此作为本次自动修复的最终状态，再继续下一个。不要在修改或 commit 前改状态，也不要自动改为“已解决，待验收”。
- `assisted`：用于“有人模式”“快速批量修改”“我来验证”。仍完整执行证据、信息质量、归属和风险门禁，但计划使用 `deferred-to-user`：AI 不运行修复验证、测试、构建或浏览器检查，也不提前修改飞书状态；逐 Bug 修改并创建单独 commit，然后暂停在待人工验证。用户明确反馈通过后，用 `verified_by: user` 记录结果并把飞书改为“修复中”；失败项保持原飞书状态并进入返修。

用户未说明模式时读取 `execution_policy.default_repair_mode`，缺省为 `autonomous`。`lightweight` 是 AI 的轻量检查，不是人工验证模式；不得用它冒充 `deferred-to-user`。阅读 `references/execution-modes.md` 获取批次冻结、失败隔离、人工交接和动作时序。

## 批准与授权

统一使用以下谓词，不要把分类、配置或命令参数本身当作用户授权：

- `repair_run_authorized(scope, mode)`：用户在当前任务中明确要求修复单个/多个编号，或要求无人值守/有人模式处理本次扫描中所有可自主修复项。单个编号或本次扫描冻结的候选编号集合构成 scope；授权覆盖该模式的默认动作包，不覆盖后续新工单、其他仓库、push、评论、完成或终止。
- `fix_approved(issue)`：该编号被用户直接点名，或属于当前 `repair_run_authorized` 的冻结集合并通过自主修复门禁；仅“看看/分诊/计划”不成立。
- `completion_action_authorized(issue, action)`：`fix_approved(issue)` 成立，动作属于当前模式的默认动作包并写入该工单计划，项目配置允许、本地覆盖未禁用；状态动作还必须核验目标状态 id、transition 和来源状态。

`autonomous` 默认授权 `commit -> start-fix`：验证和 commit 成功后才把飞书改为“修复中”；`assisted` 默认授权 `commit -> 人工验证通过后 start-fix`。两种模式都不默认包含 `resolve-for-acceptance`。单次批量请求只确认一次：为每个工单生成的 `plan_fingerprint` 用于内部绑定范围和审计，若计划仍在已授权 scope、模式和默认动作包内，可立即以该 fingerprint 记录批准并继续，不要逐工单或逐动作再次询问。只有文件/仓库/动作/风险超出批次授权时才停下请求批准。

项目配置可以声明团队能力；本地覆盖只能把权限从 `true` 收紧为 `false`，不能把项目配置的 `false` 放宽为 `true`。当前运行授权只对当前任务、冻结工单集合、模式和动作包生效，不覆盖本地 deny。对同一人工验证交接，已记录在工件中的精确计划授权可继续约束闭环；用户的“验证通过/失败”是验证证据，不要求重新批准原动作包。

因此：

- `auto-fix-candidate` 只是排序/候选标签，不代表可以改代码。
- `auto_fix_allowed: false` 时仍可修复用户点名工单；只有用户明确发起批量修复运行授权时，才可从本次扫描快照中选择符合门禁的工单。
- `--approved` 不能绕过未解决的产品确认、非当前仓库归属、缺失上游工件、验证失败或 Git 隔离门禁。
- `--approved` 也不能绕过未完成的详情、评论或附件证据检查；读取已有评论属于只读分诊，发布评论仍是单独的远程动作。
- `--approved` 不能绕过 `report_quality: needs-clarification|conflicting|unknown`。信息不足时只生成本地反馈草稿；发布这份澄清评论需要对精确草稿的独立授权，不能复用修复计划授权。
- 本地提交仍须用当前计划指纹通过 `--authorized <plan_fingerprint>`，且 `commit` 必须列在计划中；这是对当前运行授权的内部绑定，不是第二轮用户确认。
- `autonomous` 与 `assisted` 的默认动作包都只含 `commit`、`start-fix`，但时序不同：前者在 AI 验证和 commit 后执行 `start-fix`，后者在 commit 且人工验证通过后执行。`resolve-for-acceptance`、评论、push、完成、终止或其他动作仍须后续另行明确授权。

## 运行时连接预检

先根据用户输入和项目配置区分数据来源：实时读取飞书 Project 才要求 MCP/OpenAPI；用户提供的导出 JSON 不要求 MCP。不要把某个客户端生成的工具前缀当成协议本身：Codex、Cursor、Claude Code 或其他 MCP 客户端可能暴露不同前缀，按工具 schema 和动作语义匹配 `search_by_mql`、`get_workitem_brief` 等能力。

对实时飞书任务只做一次有边界的能力预检：Preview 至少需要 MQL 查询能力；Fix-ready 还需要详情、评论、活动和决策相关附件读取能力；远程状态动作只在执行前检查状态查询与流转能力。若当前客户端没有本阶段的必需能力，立即停止并指向 `references/mcp-client-setup.md`，不得自行安装、重配、反复探测 MCP，也不得用 Python 依赖安装冒充 MCP 配置。若首次连接返回未认证、`401/403`、缺少环境变量、启动超时或等待批准，报告客户端可见的精确状态并停止 agent 级重试；只有用户明确要求配置连接时才协助设置。浏览器仍只作为用户授权的一次性读取回退。

Codex 的 `agents/openai.yaml` 只为 Codex 声明非敏感 MCP 依赖；Cursor、Claude Code 和其他客户端应忽略它并使用自己的 MCP 配置。无论客户端如何命名服务器，都不要把真实 token 写入 skill、仓库或工件。

## 依赖与配置

在项目认可的虚拟环境中解析 Python 3.10+ 解释器为 `<python>`。优先复用已经通过检查的解释器并直接运行 runner；只有命令明确报告缺少 `PyYAML` 时，才一次性安装 `requirements.txt`，不要在每次调用、日报或 MCP 故障时重复安装，也不要静默修改全局 Python：

```powershell
<python> -m pip install -r <skill-dir>\requirements.txt
```

读取配置栈时遵循：Skill 安全默认值 → 项目配置 → 本地 deny-only 覆盖 → 当前任务的单次批准。若项目尚未配置，阅读 `references/project-config.md`，然后运行：

```powershell
<python> <skill-dir>\scripts\bugflow_runner.py init-project --platform feishu-project --project-name my-project --project-key my-feishu-project-key
<python> <skill-dir>\scripts\bugflow_runner.py doctor
```

不要把真实密码、token、cookie、session secret 或带凭证/个人化的 MCP URL 写入配置、JSON、工件或提交。固定公开的 MCP endpoint 只允许出现在客户端连接元数据/说明中；认证值使用环境变量、客户端密钥存储或已有连接。

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

6. 仓库归属明确、`evidence_fetch.status` 为 `complete`、`report_quality.status` 为 `sufficient` 且所有确认阻塞已解决后，运行 `plan-fix` 获取 `plan_fingerprint`。计划必须声明文件范围、`standard|lightweight|deferred-to-user` 验证模式和默认 `completion_actions`。直接/批次修复请求已经授权且计划未越界时，立即用相同参数和 `--approved <plan_fingerprint>` 重建计划，不再询问；只有规划请求或越界计划才展示并等待批准。
7. `autonomous` 默认运行计划生成的 `required_checks`。Standard 必须逐项通过；合规的 `lightweight` 必须记录检查证据和剩余风险。`assisted` 不运行这些修复验证，使用 `deferred-to-user` 并在人工反馈后记录 `verified_by: user`。两种模式都不得绕过证据、信息质量、归属、风险和 Git 隔离门禁。
8. 提交前确认 Git index 预先为空；如果已有暂存内容，停止当前工单并继续处理不受影响的后续工单，不要混入已有暂存。只接受真实、单个、修复相关的 literal file path。`autonomous` 只在验证完成后提交；`assisted` 可在项目允许 `allow_deferred_user_verification` 且计划已批准时先提交，但必须标记 `verification_pending: true`，并保持飞书原状态。
9. 按模式执行已授权动作：`autonomous` 先修改、验证并逐 Bug commit，commit 成功后才执行 `start-fix`，以“修复中”作为默认最终状态；`assisted` 先只修改和逐 Bug commit，人工验证通过后才执行 `start-fix`。两种模式都不自动执行 `resolve-for-acceptance`，runner 不 push。

远程状态更新返回错误或超时时，不要盲目重放：先重新读取当前工单状态；若已是目标状态，按幂等成功记录；若仍是原状态，只重试一次；若已变成其他状态，立即停止并报告并发变化。

## 工件与失效

Preview/scan 默认不创建逐工单工件；它最多原子写入一个受保护的 Markdown 汇总报告，例如 `.bugflow/reports/daily-preview.md`。`--report` 不能写进逐工单工件、`.codex`、`.git` 或配置/代码路径。只有进入 fix-ready 后才使用 `.bugflow/issues/<safe-issue-key>/`；已有安全 canonical 编号保持原目录名，只在清洗或截断编号时附加短哈希。严格链路为：

```text
issue-intake -> requirement-match -> triage-report -> fix-plan -> implementation -> verification -> closure
```

阅读 `references/bugflow-artifacts.md` 了解 schema、状态和命令。把 `.bugflow/` 默认加入宿主仓库 `.gitignore`。

不得让旧结论在上游变化后继续显示为有效。runner 为工件写入 schema/runner 版本元数据并原子替换输出；刷新且发现 `issue.json` 内容变化时使下游工件失效。兼容的纯元数据升级可运行 `migrate-artifacts --issue <编号>`，但它仍要求重新生成分诊；证据哈希不匹配或语义规则变化时不得自动迁移结论。更新需求匹配、分诊、修复计划或实现内容时，也要把所有下游工件标为待重建。verification 失效后，closure 和 commit 门禁也必须失效。不要只依赖文件是否存在。

## 结构化验证

`verification.md` 支持三种计划绑定模式：

- `standard`：计划列出的每项 `required_checks` 都有对应的结构化 `passed` 结果；可见 UI 问题必须包含浏览器检查。无关命令即使 passed 也不能放行。
- `lightweight`：计划已明确批准该模式，修复把握为 `high`，分诊置信度为 `high`，并记录自动验证不可行原因、至少一项人工检查证据和剩余风险；允许把不适用的命令/浏览器检查记为 `skipped`。
- `deferred-to-user`：AI 修改阶段不生成 `done` 验证；只允许在用户明确反馈结果后由 `verified_by: user` 加至少一项通过/失败证据。提交可先发生；人工通过后才执行已授权的 `start-fix` 并记录本地交接完成，不自动执行 `resolve-for-acceptance`。
- verification 绑定的 implementation 内容指纹仍然匹配。

有 `failed` 或未获明确豁免的 `blocked` 时，不得标记解决/完成或把 closure 写成成功；`standard|lightweight` 也不得提交。只有已批准的 `deferred-to-user` 可在验证前创建待验收 commit，且人工通过前不得修改飞书状态。

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
<python> <skill-dir>\scripts\bugflow_runner.py plan-fix --issue BUG-123 --files src/file.ts --completion-action commit --completion-action start-fix
```

若用户只要求规划，把计划和 `plan_fingerprint` 交给用户；若用户已经直接要求单个或批次修复且计划未越界，直接复用本次运行授权，以完全相同的范围参数重新运行：

```powershell
<python> <skill-dir>\scripts\bugflow_runner.py plan-fix --issue BUG-123 --files src/file.ts --completion-action commit --completion-action start-fix --approved <plan_fingerprint>
<python> <skill-dir>\scripts\bugflow_runner.py record-implementation --issue BUG-123 --summary "Scoped fix" --files src/file.ts
<python> <skill-dir>\scripts\bugflow_runner.py record-verification --issue BUG-123 --verified-by agent --verification-note "本次本地验证" --check "lint=passed: pnpm exec eslint src/file.ts" --check "browser=passed: affected route verified" --browser passed --browser-note "Affected route verified"
<python> <skill-dir>\scripts\bugflow_runner.py commit-fix --issue BUG-123 --files src/file.ts --authorized <plan_fingerprint>
# commit 成功后，通过飞书 MCP/OpenAPI 核验并执行已授权的 start-fix，再记录本地交接完成。
<python> <skill-dir>\scripts\bugflow_runner.py close-local --issue BUG-123 --summary "AI verified and committed; marked in progress" --remote-status "修复中"
```

人工验收通过后，用户可以手动把飞书改为“已解决，待验收”，或明确通知 AI 对精确编号执行该状态动作。后者是新的验收动作授权，必须重新读取当前状态、核验 transition/目标状态与已提交实现，不得由原修复批次默认推导。

高把握但难以自动验证的低/中风险前端修复，在首次和批准后的 `plan-fix` 中都加 `--verification-mode lightweight`，然后记录轻量证据：

```powershell
<python> <skill-dir>\scripts\bugflow_runner.py record-verification --issue BUG-123 --mode lightweight --confidence high --verified-by agent --verification-note "精确 diff 与契约审查" --exemption-reason "缺少可重复的外部回调环境" --evidence "已审查精确 diff、调用边界和错误分支" --browser skipped --browser-note "需在真实回调环境验收"
```

有人模式用 `deferred-to-user` 计划，默认动作只含 `commit` 和延后的 `start-fix`。记录实现后直接创建待人工验证 commit，不运行 `record-verification`、不改飞书状态；用户反馈通过后再记录验证、执行 `start-fix` 并完成本地交接：

```powershell
<python> <skill-dir>\scripts\bugflow_runner.py plan-fix --issue BUG-123 --files src/file.ts --verification-mode deferred-to-user --completion-action commit --completion-action start-fix
<python> <skill-dir>\scripts\bugflow_runner.py plan-fix --issue BUG-123 --files src/file.ts --verification-mode deferred-to-user --completion-action commit --completion-action start-fix --approved <plan_fingerprint>
<python> <skill-dir>\scripts\bugflow_runner.py record-implementation --issue BUG-123 --summary "Scoped fix; waiting for user verification" --files src/file.ts
<python> <skill-dir>\scripts\bugflow_runner.py commit-fix --issue BUG-123 --files src/file.ts --authorized <plan_fingerprint>
<python> <skill-dir>\scripts\bugflow_runner.py record-verification --issue BUG-123 --mode deferred-to-user --verified-by user --verification-note "用户在当前任务确认通过" --check "acceptance=passed: 原问题人工复测通过"
# 通过飞书 MCP/OpenAPI 核验原状态并执行已授权的 start-fix 后，再记录本地交接。
<python> <skill-dir>\scripts\bugflow_runner.py close-local --issue BUG-123 --summary "人工验证通过，飞书已改为修复中" --remote-status "修复中"
```

## 按需读取

- `references/project-config.md`：配置栈、deny-only 合并、安全默认值和依赖。
- `references/fetch-issues.md`：飞书/导出 JSON 获取、标准化和 raw 脱敏。
- `references/evidence-intake.md`：完整详情、历史评论、活动记录和图片/视频附件的获取、检查、完整度与安全门禁。
- `references/report-quality.md`：工单可实施性/可验收性、信息冲突、精确澄清问题和本地反馈草稿。
- `references/feishu-project.md`：飞书 Project 原生适配、字段与 MQL。
- `references/mcp-client-setup.md`：仅在实时飞书连接缺失或认证失败时读取；包含 Codex、Cursor、Claude Code 和其他 MCP 客户端的安全配置与快速失败规则。
- `references/requirement-repo-mapping.md`：需求与仓库匹配。
- `references/triage-issues.md`：分诊枚举、证据和排序。
- `references/bugflow-artifacts.md`：工件链、内容指纹和失效规则。
- `references/execution-modes.md`：单次/批次授权、无人值守与有人模式、人工验证交接和失败隔离。
- `references/fix-and-verify.md`：批准后的单工单修复、结构化验证和 Git 隔离。
- `references/browser-verification.md`：可见交互验证与登录策略。
- `references/status-workflow.md`：远程动作授权谓词与状态流转。
- `references/scheduled-automation.md`：只读定时分诊；不要硬编码模型或运行时。

## 输出

Preview 输出优先使用紧凑表格，包含工单编号/标题、优先级、暂定归属/风险、推荐顺序、现有判断依据和疑似信息缺口，并在标题或表头明确标注“快速扫描/暂定结论”。不要塞入逐工单命令日志、完整工件状态或未经核对的对外反馈草稿。

Fix-ready 的严格分诊输出再包含关联需求、证据完整度与关键证据、工单信息质量、仓库匹配与置信度、最终归属、工作量、准备度、风险、推荐顺序和待确认问题。明确区分“已检查附件内容”和“只拿到附件元数据”；对证据完整且信息不足/冲突的工单列出已知事实、缺失或冲突、反馈对象和未发布反馈草稿；证据不完整时只给内部阻塞提示。用中文展示结论，不暴露无助于用户决策的内部枚举或命令日志。

修复输出包含工单编号/标题、运行模式、批次范围、用户授权的动作、修改文件、验证状态、提交/远程动作实际结果和剩余风险。有人模式把每项明确标为“待人工验证/人工通过/人工失败”，未通过前不得暗示已闭环；未执行的动作明确写“未执行”。
