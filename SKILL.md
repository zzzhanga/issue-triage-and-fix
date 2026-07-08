---
name: issue-triage-and-fix
description: 飞书/Jira/TAPD/禅道/GitLab Issues 等缺陷工单分诊与修复工作流。Use when Codex needs to fetch assigned bugs or issues, map linked requirements to local repositories, triage ownership and effort, recommend repair order, generate bugflow artifacts, fix safe candidates, verify locally and in a browser, and optionally comment or update remote issue status from config.
---
# 缺陷分诊与修复工作流

## 概览

使用这个技能把工单系统里的 bug 转成可控的工程任务：拉取工单、识别关联需求、判断对应代码库、分诊责任和难度、只修复低风险候选项、完成本地与浏览器验证，并在策略允许时评论或流转远程状态。

这个技能是一个编排入口。平台细节、需求与仓库匹配、分诊规则、浏览器验证、状态流转、项目配置都拆在 `references/`、`assets/` 和 `scripts/` 里，方便不同项目复用同一套流程。

## 当前定位

当前版本是：

```text
Skill + Workflow + 工件化执行框架 + v1 fetch/triage runner
```

它已经适合在 Codex 中手动或半自动执行 bug 分诊，也可以作为 Codex“已安排”任务的执行基础。当前 runner 支持从飞书/MCP/导出的 JSON 导入工单、生成 bugflow 工件、执行需求-仓库匹配和初步分诊，但不会自动改代码或远程状态。

如果用户希望每天自动分诊，阅读 `references/scheduled-automation.md`。技能可以提示并协助创建或更新 Codex“已安排”任务，但不能在用户未要求或未确认时静默创建定时任务。默认推荐任务配置为本地环境、`GPT-5.5`、推理级别 `超高/xhigh`。

## 工件化 Bugflow

当一个 bug 需要跨多仓库判断、需要客户/产品确认、需要可恢复执行，或需要保留过程证据时，使用 bugflow 工件。

默认 bug 工作目录：

```text
.bugflow/issues/<issue-number>/
```

推荐把 `.codex/bugflow/` 用于项目配置和 schema，把 `.bugflow/` 用于每日运行生成的分诊、修复和验证工件。`.bugflow/` 可能包含客户名称、工单描述、截图线索和过程判断，默认应加入所在项目的 `.gitignore`；只有团队明确希望在代码评审中共享这些工件时，才提交到仓库。

默认工件链：

```text
issue-intake -> requirement-match -> triage-report -> fix-plan -> implementation -> verification -> closure
```

动作不是僵硬阶段。信息明确时可以继续处理下一个 ready 工件；如果实现过程中发现需求、仓库归属或修复方案有变化，要回头更新前面的工件。

## 必要输入

执行前先定位或询问：

- 当前代码库路径。
- 配置栈：技能默认规则、项目级配置、本地覆盖配置、当前用户请求。
- 工单来源：飞书项目、Jira、TAPD、禅道、GitLab Issues，或用户提供的 JSON 导出。
- 用户期望模式：只列出、只分诊、修复单个 bug，或完整受控闭环。

如果项目还没有配置，先阅读 `references/project-config.md`，再使用 `assets/project-config.template.yaml` 或 `assets/feishu-project-config.template.yaml` 起草配置。没有明确配置前，不要修改远程工单状态。

## 标准流程

1. 读取项目规则。

   - 阅读 `AGENTS.md`、`README.md` 和项目配置中列出的文档。
   - 按顺序读取配置栈：项目级配置、本地覆盖配置、当前用户请求。
   - 项目级配置负责工单字段、需求与仓库映射、状态 id、团队流程、验证命令、项目归属规则。
   - 本地覆盖配置只放用户登录偏好、本地端口、密钥环境变量名、更严格的自动化策略。
2. 拉取工单。

   - 阅读 `references/fetch-issues.md`。
   - 工件化执行时，在 bug 工作目录创建或更新 `issue.json`。
   - 只在需要时加载平台适配文档，例如 `references/feishu-project.md`。
   - 优先拉取分配给目标用户、状态为待处理的可行动工单。
   - 如果平台提供关联需求字段，必须一起拉取。
   - 分诊前把工单标准化为统一 issue JSON。
3. 解析需求与代码库关系。

   - 阅读 `references/requirement-repo-mapping.md`。
   - 工件化执行时，创建或更新 `requirement-match.md`。
   - 根据工单里的需求、标题、链接、截图、配置中的需求别名，匹配候选代码库。
   - 只有当前代码库是高置信匹配，或用户明确指定当前代码库时，才继续进入修复。
   - 如果一个需求对应多个代码库且归属不清，生成客户/产品确认问题，不要猜。
4. 分诊工单。

   - 阅读 `references/triage-issues.md`。
   - 工件化执行时，创建或更新 `triage.md`。
   - 判断责任归属、修复难度、执行准备度、风险等级和推荐处理顺序。
   - 只分诊时，不改代码、不改远程状态。
5. 选择修复候选。

   - 工件化执行时，修改代码前创建或更新 `fix-plan.md`。
   - 默认只修复 `auto-fix-candidate`。
   - `hard`、`blocked`、`needs-confirmation`、`not-current-repo`、跨团队问题都不能自动修复，除非用户明确指定。
6. 开始修复流程。

   - 阅读 `references/status-workflow.md` 和 `references/fix-and-verify.md`。
   - 只有项目配置允许时，才把远程工单改为“修复中”。
   - 如果凭证、项目身份、状态流转策略不明确，不要修改远程状态。
7. 实现修复。

   - 保持改动范围只围绕当前 bug。
   - 遵守当前仓库的组件、样式、测试、格式化和分支约定。
   - 不回滚用户或其他 agent 的无关改动。
8. 验证。

   - 运行项目配置中的格式化、lint、测试、stylelint、构建或回归脚本。
   - 工件化执行时，创建或更新 `verification.md`。
   - 可见 UI 问题必须阅读 `references/browser-verification.md` 并做浏览器验证，除非用户明确说不需要。
   - 遵守登录策略，不要求用户在聊天里粘贴密码。
9. 闭环。

   - 只有配置或用户允许时，才评论远程工单。
   - 工件化执行时，创建或更新 `closure.md`。
   - 评论中说明改动文件、验证命令、浏览器证据和剩余风险。
   - 只有配置允许或用户明确确认时，才流转到“已解决，待验收”“已完成”或“已终止”。

## 运行模式

- `list`：拉取并摘要工单，不输出详细分诊。
- `triage`：拉取、标准化、需求匹配、分诊和排序，不改代码、不改远程状态。
- `fix-one`：修复用户指定的单个工单，并完成验证。
- `full-controlled`：分诊、选择安全候选、可选改为“修复中”、修复、验证、评论、可选流转状态。

用户只是探索时，默认使用 `triage`。用户明确指定某个 bug 时，默认使用 `fix-one`。

## 资源索引

- `references/fetch-issues.md`：工单获取和标准化 issue JSON。
- `references/bugflow-artifacts.md`：工件化流程、动作规则和状态模型。
- `references/requirement-repo-mapping.md`：需求与代码库匹配、客户确认规则。
- `references/triage-issues.md`：归属、难度、准备度、风险和排序规则。
- `references/fix-and-verify.md`：代码修复、本地验证、浏览器验证、闭环评论。
- `references/feishu-project.md`：飞书项目适配规则。
- `references/browser-verification.md`：浏览器验证与登录策略。
- `references/status-workflow.md`：远程状态流转策略。
- `references/project-config.md`：配置栈和合并规则。
- `references/scheduled-automation.md`：Codex“已安排”任务创建、更新和默认模型配置。
- `scripts/normalize_issue_payload.py`：把不同平台工单转成标准 issue JSON。
- `scripts/bugflow_artifacts.py`：初始化 bug 工作目录，查看工件 ready/blocked/done 状态。
- `scripts/bugflow_runner.py`：v1 分诊 runner，支持 `doctor`、`fetch-json`、`triage`、`daily`。
- `assets/bugflow-schema.template.yaml`：工件依赖图模板。
- `assets/project-config.template.yaml`：通用项目级配置模板。
- `assets/feishu-project-config.template.yaml`：飞书项目配置模板。
- `assets/local-overrides.template.yaml`：本地用户覆盖配置模板。

## 安全规则

- 不索要、不回显、不保存、不提交密码、token、MCP URL、cookie、session secret。
- 优先使用官方 API、MCP 或 SDK；只有用户允许时，才用浏览器登录做一次性读取。
- 默认不修改远程状态；必须由项目配置允许具体流转，或用户明确批准。
- 不能因为代码改了就认为 bug 已修复；必须先验证，再说明未验证部分。
- 不批量自动修复多个 bug，除非用户明确要求且每个 bug 都是低风险、相互独立。

## 输出要求

分诊输出应包含：

- 工单 id/编号和标题。
- 关联需求。
- 当前代码库匹配结果和置信度。
- 责任归属。
- 难度。
- 执行准备度。
- 风险。
- 推荐处理顺序。
- 需要客户/产品确认的问题。

每日分诊或已安排任务的最终回复应表格优先，先输出缺陷总览表，再输出证据与判断、推荐修复顺序和需要确认事项；推荐列使用中文，不暴露内部分类枚举；不要用命令过程日志替代表格摘要。

修复输出应包含：

- 工单 id/编号和标题。
- 远程状态变更或评论记录。
- 修改文件。
- 验证命令和浏览器检查。
- 剩余风险或阻塞项。
