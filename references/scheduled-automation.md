# Codex 已安排任务

当用户希望每天、每周或定时执行缺陷分诊时，可以把本技能配置为 Codex“已安排”任务。

一次交互任务中的 `autonomous` 无人值守修复只授权当前扫描快照，不等于持久化定时授权。已安排任务仍默认只读 preview；只有用户明确要求创建“定时自动修复”并单独定义工单范围、模式、动作包、失败策略和有效期时，才评估扩展，不能复用某次交互运行授权。

## 触发规则

- 用户明确要求创建、更新、查看或删除已安排任务时，使用 Codex 自动化工具处理。
- 用户只是在讨论 workflow 或 skill 能力时，只说明建议配置，不要静默创建任务。
- 如果项目已经存在同类已安排任务，优先更新原任务，不要创建重复任务。
- 创建或更新前，保留用户已有的时间、项目路径、状态和 prompt，除非用户要求修改。

## 默认任务配置

- 运行环境：本地。
- 项目路径：当前代码库根目录。
- 模型与推理级别：保留用户已有设置；新任务使用当前环境可用的推荐默认值，不在 Skill 中写死型号。
- 模式：默认 `preview/scan` 快速日报；不进入逐工单 fix-ready，不改业务代码、不改远程状态、不提交分支。

## 推荐 prompt 要点

已安排任务 prompt 应包含：

- 使用 `$issue-triage-and-fix`。
- 读取 `AGENTS.md` 和 `.codex/bugflow/` 下的项目配置。
- 实时飞书任务先按工具 schema/动作语义确认当前客户端已暴露等价的 `search_by_mql` 查询能力；缺失、未认证、等待客户端批准或首次连接超时时立即报告并停止，不安装、不重配、不反复探测 MCP。导出 JSON 任务跳过此检查。
- 复用项目已配置且曾通过检查的 Python 3 解释器，直接运行 `bugflow_runner.py doctor`；只有首次配置或明确报依赖缺失时才安装 `requirements.txt`，不要每次日报重复安装。报告 warn/error，有 error 时停止。
- 区分 `doctor` 的本地 `field-mapping` 与 `remote-field-verification`；不要把映射存在误当成远端字段已验证，也不要每次做全量字段发现。
- 使用 `bugflow_runner.py feishu-mql --profile preview --json` 从配置生成本次最小 SELECT；可选字段只取 `field_verification.verified_keys` 中已确认的 key。
- 只有 MQL 报字段错误、配置缺字段，或用户明确要求重新发现字段时，才用字段配置工具做精确查询。
- 拉取分配给当前用户、状态为待修复或重新打开的工单；runner 还要检查每条负责人并拒绝无法证明当前用户范围的混合批次。
- 候选列表只取生成总览所需的关联需求、标题、状态、优先级、描述摘要、附件摘要、负责人和更新时间。不要默认逐条获取完整详情、分页读全评论/活动或下载全部媒体；只有某条初筛判断离不开一个易取得的关键摘要/证据时才补取。
- 对候选 JSON 运行 `bugflow_runner.py preview --input <json> --report .bugflow/reports/daily-preview.md`。该命令只在内存中标准化、过滤和初步分类，不写逐工单工件。
- 输出暂定归属、风险、优先级、推荐顺序和疑似信息缺口，并明确标为“快速扫描/暂定结论”。不得把未读取的评论/附件写成已核对，不得设置或要求 `report_quality.input_hash`，不得生成对外反馈草稿。
- 不生成或更新 `.bugflow/issues/<safe-issue-key>/`；不扫描历史 bugflow 目录，不运行 requirement-match、严格 triage、build/lint、浏览器或代码搜索。
- 只有用户随后选中具体 bug 时，才在交互任务中用 preview 返回的编号/id 直接升级到 fix-ready，不重新扫描整批候选：读取该工单的完整详情/评论/活动/附件、写 `issue.json`、绑定 `report_quality` 并严格分诊。已经严格评估的日报使用 `daily-existing --issue <编号> --assignee <当前用户名称或ID>`，不要用 `daily --input` 二次导入覆盖评估。
- 输出每日扫描报告：先给缺陷总览表格，再给推荐顺序和“升级后需核对项”；不要用大段过程日志替代表格。
- 明确安全边界：不索要密钥、不改远程状态、不发布评论、不改代码、不提交。

## 输出格式

最终回复应表格优先，适合每天扫一眼：

```markdown
本次 Feishu Project MCP 查询到 `project.work_item_type` 当前登录用户负责、状态“待修复/重新打开”的缺陷共 N 条：

| 缺陷 | 标题 | 优先级 | 状态 | 暂定归属/风险 | 提出/更新 | 报告人/负责人 | 推荐 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 28079 / 7028343260 | 标题 | P1 | 待修复 | 疑似前端 / 中风险（暂定） | 创建 ...<br>更新 ... | 报告人 / 负责人 | 建议优先进入严格评估 |

现有摘要与判断：...

推荐修复顺序：...

升级后需核对：...

本次为快速扫描，未逐条读全评论/附件，结论均为暂定；未生成逐工单工件、未发布飞书评论、未修改飞书状态、未修改代码、未创建分支或提交。
```

推荐列必须使用中文，不要暴露 `manual-review-first`、`auto-fix-candidate`、`medium` 等内部枚举。只在异常时简述 `doctor`、MCP、字段发现等过程细节。正常成功时不要把命令执行过程放在正文前面。

## 快路径

定时分诊应优先走快路径，避免把一次日报变成探索式会话：

1. 对实时飞书确认当前客户端已有等价 MQL 查询能力；失败即快速停止，导出 JSON 跳过。
2. 用已解析并完成依赖检查的 Python 3 解释器跑 `doctor`。
3. 用 `feishu-mql --profile preview --json` 生成本次最小查询。
4. 用 MCP 查本次待处理 issue 候选列表。
5. 保留列表字段、已有描述摘要和附件元数据；仅在某条初筛离不开一个易取得的关键摘要/证据时补取。不要默认 full detail、分页评论、活动记录或媒体下载。
6. 运行 `bugflow_runner.py preview --input <json> --report .bugflow/reports/daily-preview.md`；它只做内存标准化、当前负责人过滤和初步分类，不生成 `issue.json` 或其他逐工单工件。
7. 输出暂定日报；疑似信息不足只写“升级后需核对项”，不运行 `report-quality-hash`，不生成声称已核对资料的反馈草稿。

定时任务必须使用 `feishu-mql` 生成的 `current_login_user()` 负责人过滤结果；不要把全项目 JSON 当作“我的工单”导入。非飞书导出任务必须配置当前用户并向 `preview` 传 `--assignee <name-or-id>`。

`search_by_mql` 返回单条 `moql_field_list`，或 `data -> 分组 ID -> moql_field_list[]` 时，都把完整响应交给 runner；它会提取分组内的每条记录并按字段 key 标准化。Preview 的 SELECT 只强制候选识别/过滤所需 core 字段；优先级、更新时间、需求和描述等只在远端已验证时加入。报告人、创建时间和附件等 fix-ready 字段不要为追求表格“完整”而塞进快速查询。

不要在快路径中读取 automation memory、重新探索可用工具、扫描历史 bugflow 目录、运行 build/lint、打开浏览器、搜索实现代码或做代码修复。若 preview 只拿到附件元数据/缩略图，就明确写“附件内容未核对”；不要为了完成日报而强行下载全部附件，也不要把它标成严格的 `partial|error` 工件状态。

## 字段发现策略

避免在每次定时任务里重复做全量字段发现。全量字段发现返回空列表时，不应立刻判定“映射不清”，尤其当 `doctor` 已经确认配置完整、MQL 查询也能返回数据时。

推荐顺序：

1. 先跑 `doctor`。
2. `doctor` 无 error 时，运行 `feishu-mql --profile preview --json`；本地映射未做远端验证的可选 key 会被安全排除。
3. 如果 MQL 报字段错误，只修正报错字段。
4. 需要校验字段配置时，使用 `feishu-mql --profile preview --json` 返回的 `exact_field_config_keys` 做精确查询，不要先做模糊或全量查询。
5. 把“字段映射不清”和“需求/仓库归属不清”分开报告；前者是配置问题，后者是业务判断问题。

## 何时升级到修复任务

每日分诊任务默认只做 `preview/scan`。只有当用户明确指定某个 bug 进入严格评估或修复，并且项目配置允许时，才创建单独任务或在当前线程进入 fix-ready。fix-ready 必须补齐完整证据、`report_quality.hash_version/input_hash` 和严格工件；需要汇总时传入本次明确编号和具体负责人运行 `daily-existing --issue <编号> --assignee <当前用户名称或ID>`，绝不自动扫描历史目录，也不再用 `daily --input` 覆盖已评估内容。
