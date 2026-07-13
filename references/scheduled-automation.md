# Codex 已安排任务

当用户希望每天、每周或定时执行缺陷分诊时，可以把本技能配置为 Codex“已安排”任务。

## 触发规则

- 用户明确要求创建、更新、查看或删除已安排任务时，使用 Codex 自动化工具处理。
- 用户只是在讨论 workflow 或 skill 能力时，只说明建议配置，不要静默创建任务。
- 如果项目已经存在同类已安排任务，优先更新原任务，不要创建重复任务。
- 创建或更新前，保留用户已有的时间、项目路径、状态和 prompt，除非用户要求修改。

## 默认任务配置

- 运行环境：本地。
- 项目路径：当前代码库根目录。
- 模型与推理级别：保留用户已有设置；新任务使用当前环境可用的推荐默认值，不在 Skill 中写死型号。
- 模式：每日分诊优先，不改业务代码、不改远程状态、不提交分支。

## 推荐 prompt 要点

已安排任务 prompt 应包含：

- 使用 `$issue-triage-and-fix`。
- 读取 `AGENTS.md` 和 `.codex/bugflow/` 下的项目配置。
- 从任务环境解析一个可用的 Python 3 解释器，安装 `requirements.txt` 后用同一解释器运行 `bugflow_runner.py doctor`；报告 warn/error，有 error 时停止。
- 如果 `doctor` 中 `field-mapping`、`requirement-field` 和 `status-codes` 都是 ok，后续拉取应信任项目配置，不要再做全量字段发现。
- 使用 `bugflow_runner.py feishu-mql --json` 从配置生成本次最小 SELECT 和精确字段配置 key。
- 只有 MQL 报字段错误、配置缺字段，或用户明确要求重新发现字段时，才用字段配置工具做精确查询。
- 拉取分配给当前用户、状态为待修复或重新打开的工单。
- 候选列表之后逐条获取完整详情、历史评论和相关操作记录；包含关联需求、标题、状态、优先级、描述、截图/附件、负责人和更新时间。
- 按 `evidence-intake.md` 实际检查决策相关图片/视频/文档内容并记录 `evidence_fetch`。只拿到附件元数据、缩略图或视频封面时不得标记证据完整；无法读取则进入“需要确认”，不生成高置信/自动修复候选。
- 生成或更新 `.bugflow/issues/<safe-issue-key>/` 工件。
- 只处理本次拉取到的 bug；不要扫描 `.bugflow/issues` 下的历史目录混入日报。
- 输出每日分诊报告：先给缺陷总览表格，再给证据与判断、推荐修复顺序、需要确认事项；不要用大段过程日志替代表格。
- 明确安全边界：不索要密钥、不改远程状态、不改代码、不提交。

## 输出格式

最终回复应表格优先，适合每天扫一眼：

```markdown
本次 Feishu Project MCP 查询到 `project.work_item_type` 当前登录用户负责、状态“待修复/重新打开”的缺陷共 N 条：

| 缺陷 | 标题 | 优先级 | 状态 | 提出/更新 | 报告人/负责人 | 推荐 |
| --- | --- | --- | --- | --- | --- | --- |
| 28079 / 7028343260 | 标题 | P1 | 待修复 | 创建 ...<br>更新 ... | 报告人 / 负责人 | 需人工评审 / 中等难度 / 中风险 |

证据与判断：...

推荐修复顺序：...

需要进一步确认：...

本次未修改飞书状态、未修改代码、未创建分支或提交；仅更新了自动化记忆和 bugflow 工件。
```

推荐列必须使用中文，不要暴露 `manual-review-first`、`auto-fix-candidate`、`medium` 等内部枚举。只在异常时简述 `doctor`、MCP、字段发现等过程细节。正常成功时不要把命令执行过程放在正文前面。

## 快路径

定时分诊应优先走快路径，避免把一次日报变成探索式会话：

1. 用已解析并完成依赖检查的 Python 3 解释器跑 `doctor`。
2. 用 `feishu-mql --json` 生成本次最小查询。
3. 用 MCP 查本次待处理 issue 候选列表。
4. 对每个候选获取 full detail，分页读取评论，按需读取活动记录，并检查决策相关附件；把完整度、摘要和缺失项写入标准 JSON。
5. 用 `bugflow_runner.py daily --input <json> --report .bugflow/daily-report.md` 更新工件和日报。
6. 输出日报摘要；证据不完整的工单只给初步判断和精确阻塞项。

定时任务必须使用 `feishu-mql` 生成的 `current_login_user()` 负责人过滤结果；不要把全项目 JSON 当作“我的工单”导入。非飞书导出任务必须配置当前用户并向 `daily` 传 `--assignee <name-or-id>`。

`search_by_mql` 返回 `moql_field_list` 时，可以直接把该记录传给 runner；runner 会保留顶层字段并按字段 key 扁平化。为保证表格信息完整，SELECT 至少包含配置里映射的 `id`、`number`、`title`、`status`、`priority`、`reporter`、`assignee`、`created_at`、`updated_at`、`requirements`、`description` 和 `attachments`。

不要在快路径中读取 automation memory、重新探索可用工具、扫描历史 bugflow 目录、运行 build/lint、打开浏览器或做代码修复。附件检查应使用现有 MCP 下载能力和安全的本地媒体工具；工具/权限缺失时记录 `partial|error`，不要退化为只看标题。

## 字段发现策略

避免在每次定时任务里重复做全量字段发现。全量字段发现返回空列表时，不应立刻判定“映射不清”，尤其当 `doctor` 已经确认配置完整、MQL 查询也能返回数据时。

推荐顺序：

1. 先跑 `doctor`。
2. `doctor` 全 ok 时，运行 `feishu-mql --json`，按配置中的字段 key 执行最小 SELECT。
3. 如果 MQL 报字段错误，只修正报错字段。
4. 需要校验字段配置时，使用 `feishu-mql --json` 返回的 `exact_field_config_keys` 做精确查询，不要先做模糊或全量查询。
5. 把“字段映射不清”和“需求/仓库归属不清”分开报告；前者是配置问题，后者是业务判断问题。

## 何时升级到修复任务

每日分诊任务默认只做 triage。只有当用户明确指定某个 bug 进入 `fix-one`，并且项目配置允许时，才创建单独的修复任务或在当前线程继续修复。
