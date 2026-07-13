---
name: issue-triage-and-fix
description: 飞书 Project 缺陷工单分诊与受控修复工作流，默认只关注指派给当前用户的工单，并在读取完整详情、历史评论及图片/视频等附件证据后再判断归属与风险；原生支持飞书 MCP/OpenAPI，也可把 Jira、TAPD、禅道、GitLab Issues 等平台导出的 JSON 标准化后过滤与分诊。Use when Codex needs evidence-backed triage, ownership/risk classification, rejection or redirection of unreasonable/cross-owner fixes, or—after approval of an exact plan—repair one issue with standard or lightweight verification and execute the plan-listed commit/status completion actions. Do not imply native adapters for non-Feishu trackers.
---
# 缺陷分诊与受控修复

## 默认边界

默认执行 `triage`：只读取、标准化、匹配仓库、分诊和排序。除非用户明确批准具体工单和动作，否则不要修改代码、创建提交、评论或更新远程状态。

只把飞书 Project 视为原生适配平台。对 Jira、TAPD、禅道、GitLab Issues 或其他平台，只处理用户提供或项目工具生成的 JSON 导出；先用 `scripts/normalize_issue_payload.py` 或 runner 标准化，不要声称能直接查询、评论或流转这些平台。

把这个 Skill 当作编排入口：runner 负责配置、标准化、工件和门禁，不负责自动编辑业务代码、push 或调用远程状态更新接口。

标题和列表行只用于发现候选工单，不能直接作为最终分诊证据。最终分诊前必须按 `references/evidence-intake.md` 获取完整详情、读取入站评论/活动记录、枚举并实际检查决策相关附件；图片要看原图，视频要查看覆盖问题发生过程的关键帧/片段，不能只看缩略图。证据无法读取时明确标为 `partial|error` 并降级为待确认，不得给出高置信、`easy`、`auto-fix-candidate`、修复计划或轻量验证结论。

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

## 标准流程

1. 读取当前仓库的 `AGENTS.md`、项目文档和 `.codex/bugflow/` 配置；检查工作区与暂存区，保留用户和其他 agent 的改动。
2. 获取工单。
   - 飞书：按需读取 `references/feishu-project.md`，从配置生成最小 MQL，再用已配置的 MCP/OpenAPI 获取候选列表。
   - 其他平台：接受 JSON 导出，不尝试不存在的原生 adapter。
3. 对每个候选执行 `references/evidence-intake.md`：获取 full detail，分页读取历史评论，按需读取操作记录；汇总字段、富文本和评论里的附件，在安全的本地目录实际检查图片/视频/文档内容。把脱敏后的 `comments`、`activities`、附件检查摘要及 `evidence_fetch` 完整度并入输入；不得把临时下载 `sign`、signed URL 或鉴权头落盘到工件。
4. 按 `references/fetch-issues.md` 标准化数据，并只保留指派给当前用户/配置别名的工单。非飞书 JSON 无法解析当前负责人时必须停止，要求配置 `query_policy.assigned_to`/`assignee_aliases` 或传 `--assignee`；只有用户明确要求全量时才用 `--include-all-assignees`。默认脱敏 `raw`。没有显式 `evidence_fetch.status: complete` 时只允许生成受阻的初步分诊。
5. 按 `references/requirement-repo-mapping.md` 匹配需求与仓库。匹配不明确时生成精确确认问题，不要猜。
6. 按 `references/triage-issues.md` 基于描述、评论、附件检查摘要、活动记录、需求和代码证据判断归属、工作量、准备度、风险和推荐顺序。明显属于后端/API 契约、其他仓库或方案本身不合理的工单标为转交/拒修，不做前端兜底；例如不要为“列表接口应倒序”在前端 `reverse` 数据。默认停在这里并向用户报告。
7. 仓库归属明确、`evidence_fetch.status` 为 `complete` 且所有确认阻塞已解决后，先运行未批准的 `plan-fix` 获取 `plan_fingerprint`。计划必须同时声明文件范围、`standard|lightweight` 验证模式和批准后可连续执行的 `completion_actions`。用户批准后，用完全相同的参数和 `--approved <plan_fingerprint>` 重建计划，再修改代码并记录实现。
8. 默认运行适用的格式化、lint、测试、构建和浏览器检查。只有高置信、当前仓库、明确前端归属、低/中风险且难以可靠自动验证的修复，才可使用计划已批准的 `lightweight` 模式；记录难以自动验证的原因、至少一项代码/差异/契约检查证据和剩余风险。任何失败、阻塞、高风险、归属不清或入站证据不完整都不能轻量放行。
9. 只有当前 verification 有效且满足必需检查时，才允许闭环或提交。提交前确认 Git index 预先为空；如果已有暂存内容，停止并报告，不要把它们混入提交。只接受真实、单个、修复相关的 literal file path；不要传 `.`、目录、glob 或仓库外路径。
10. verification 完成后，连续执行已批准计划中列出的本地提交和飞书状态动作；未列出的动作不执行。runner 不 push。

## 工件与失效

默认工件目录为 `.bugflow/issues/<safe-issue-key>/`；已有安全 canonical 编号保持原目录名，只在清洗或截断编号时附加短哈希。链路为：

```text
issue-intake -> requirement-match -> triage-report -> fix-plan -> implementation -> verification -> closure
```

阅读 `references/bugflow-artifacts.md` 了解 schema、状态和命令。把 `.bugflow/` 默认加入宿主仓库 `.gitignore`。

不得让旧结论在上游变化后继续显示为有效。runner 在刷新且发现 `issue.json` 内容变化时使下游工件失效；更新需求匹配、分诊、修复计划或实现内容时，也要把所有下游工件标为待重建。verification 失效后，closure 和 commit 门禁也必须失效。不要只依赖文件是否存在。

## 结构化验证

`verification.md` 可通过两种已批准模式成为 `done`：

- `standard`：至少一个适用检查为 `passed`，每项必需检查有结构化结果；可见 UI 问题完成浏览器验证。
- `lightweight`：计划已明确批准该模式，修复把握为 `high`，分诊置信度为 `high`，并记录自动验证不可行原因、至少一项人工检查证据和剩余风险；允许把不适用的命令/浏览器检查记为 `skipped`。
- verification 绑定的 implementation 内容指纹仍然匹配。

有 `failed` 或未获明确豁免的 `blocked` 时，不得提交、标记解决/完成或把 closure 写成成功。不要使用跳过验证或部分闭环参数绕过默认安全路径，除非用户明确批准该例外并在最终回复中显著披露。

## 常用命令

飞书查询与只读分诊：

```powershell
<python> <skill-dir>\scripts\bugflow_runner.py doctor
<python> <skill-dir>\scripts\bugflow_runner.py feishu-mql --json
<python> <skill-dir>\scripts\bugflow_runner.py daily --input feishu-bugs.json --report .bugflow/daily-report.md
```

导入任意平台导出的 JSON：

```powershell
<python> <skill-dir>\scripts\normalize_issue_payload.py --input exported-issues.json --output normalized-issues.json --platform exported-json
<python> <skill-dir>\scripts\bugflow_runner.py fetch-json --input normalized-issues.json --assignee <current-user-name-or-id>
<python> <skill-dir>\scripts\bugflow_runner.py triage
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
<python> <skill-dir>\scripts\bugflow_runner.py record-verification --issue BUG-123 --command "pnpm exec eslint src/file.ts => passed" --browser passed --browser-note "Affected route verified"
<python> <skill-dir>\scripts\bugflow_runner.py close-local --issue BUG-123 --summary "Verified locally"
```

高把握但难以自动验证的低/中风险前端修复，在首次和批准后的 `plan-fix` 中都加 `--verification-mode lightweight`，然后记录轻量证据：

```powershell
<python> <skill-dir>\scripts\bugflow_runner.py record-verification --issue BUG-123 --mode lightweight --confidence high --exemption-reason "缺少可重复的外部回调环境" --evidence "已审查精确 diff、调用边界和错误分支" --browser skipped --browser-note "需在真实回调环境验收"
```

如果已批准计划列出 `commit`，验证完成后直接运行，不再重复询问：

```powershell
<python> <skill-dir>\scripts\bugflow_runner.py commit-fix --issue BUG-123 --files src/file.ts --authorized <plan_fingerprint>
```

## 按需读取

- `references/project-config.md`：配置栈、deny-only 合并、安全默认值和依赖。
- `references/fetch-issues.md`：飞书/导出 JSON 获取、标准化和 raw 脱敏。
- `references/evidence-intake.md`：完整详情、历史评论、活动记录和图片/视频附件的获取、检查、完整度与安全门禁。
- `references/feishu-project.md`：飞书 Project 原生适配、字段与 MQL。
- `references/requirement-repo-mapping.md`：需求与仓库匹配。
- `references/triage-issues.md`：分诊枚举、证据和排序。
- `references/bugflow-artifacts.md`：工件链、内容指纹和失效规则。
- `references/fix-and-verify.md`：批准后的单工单修复、结构化验证和 Git 隔离。
- `references/browser-verification.md`：可见交互验证与登录策略。
- `references/status-workflow.md`：远程动作授权谓词与状态流转。
- `references/scheduled-automation.md`：只读定时分诊；不要硬编码模型或运行时。

## 输出

分诊输出优先使用紧凑表格，包含工单编号/标题、关联需求、证据完整度与关键证据、仓库匹配与置信度、归属、工作量、准备度、风险、推荐顺序和待确认问题。明确区分“已检查附件内容”和“只拿到附件元数据”；用中文展示结论，不暴露无助于用户决策的内部枚举或命令日志。

修复输出包含工单编号/标题、用户批准的动作、修改文件、结构化验证结果、浏览器证据、提交/远程动作实际结果和剩余风险。未执行的动作明确写“未执行”，不要暗示已获授权或已完成。
