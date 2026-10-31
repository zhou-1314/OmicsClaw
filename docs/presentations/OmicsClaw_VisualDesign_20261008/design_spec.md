<!-- ppt-master-schema: design-spec/v1 -->
# OmicsClaw Group Meeting - Design Spec

## I. Project Information

| Item | Value |
| --- | --- |
| Project Name | OmicsClaw_VisualDesign |
| Canvas Format | PPT 16:9, 1280 × 720 |
| Page Count | 33 |
| Primary Language | zh-CN |
| Target Audience | 课题组 PI、研究生与生信研究人员 |
| Communication Intent | 汇报当前架构，解释组件职责、协作与取舍，讨论验证重点 |
| Desired Audience Outcome | 能说明分析请求如何变为可回放、可审核的模块结果，区分当前实现与验证范围 |
| Core Message / Ask / Action | 单向分层框架、独立技能和模块证据链连接自然语言请求与科研输出 |
| Delivery Context | 现场中文组会；33 页核心内容；按讨论深度安排约 35–40 分钟 |
| Artifact Afterlife | 附代码来源与讲稿的可编辑组内参考 |
| Reading Mode | presentation |
| Content Strategy | 依据本地 68970e06 代码与交付记录，不将历史状态或计划当作当前成果 |
| Design Style | 清晰的技术分层图与大留白；按教学顺序拆解组件 |
| AI Image Acquisition Path | not applicable |
| Generation Mode | continuous |
| Spec Refinement | disabled |
| Speaker Notes | enabled; user delegated production decisions and requested notes through accepted proposal |
| Custom Animations | disabled; delegated decision |
| Narration Audio | disabled; delegated decision |
| Created Date | 2026-10-08 |

## II. Canvas Specification

| Property | Value |
| --- | --- |
| Format | PPT 16:9 |
| Dimensions | 1280 × 720 |
| viewBox | `0 0 1280 720` |
| Margins | 60 px |
| Content Area | 正文在页内清楚分组，底部保留来源与页码 |

## III. Visual Theme

### Theme Style

- **Mode**: custom
- **Mode References**: instructional
- **Mode Behavior**: 先提出科研分析中的执行与证据问题，给出系统地图，再按请求流拆解核心组件；随后解释模块生命周期、安全边界与验收案例，最后区分当前进展与后续研究问题。
- **Visual style**: custom
- **Visual Style References**: swiss-minimal
- **Visual Style Behavior**: 以清晰的对齐、较大留白、直线与共享区域表达接口和所有权；深蓝用于框架，青绿用于技能与可追溯输出，琥珀色用于限制。文字、节点与连线保持原生可编辑。
- **Theme**: 贯穿全篇的“请求到证据”路径；每页只突出当前解释的关系。
- **Tone**: 克制、具体、适合组内技术讨论。

### Direction Decision

本轮沿用原稿视觉体系，以 OmicsClaw 为唯一主线。保留问题、机制与价值，合并重复解释，移除外围细节。用户已授权自主完成全部修订。30 页，正文 26 px，讲稿开启，逐对象动画与旁白关闭。

### Color Scheme

| Role | HEX | Purpose |
| --- | --- | --- |
| Background | #F8FAFC | 常规页面 |
| Secondary background | #E9F1F5 | 接口与分组区域 |
| Primary | #17324D | 框架、标题和关键层 |
| Accent | #007F7A | 科学执行和已实现能力 |
| Secondary accent | #AE641B | 范围、取舍和待验证项 |
| Body text | #20364B | 正文 |
| Secondary text | #526779 | 解释和来源 |
| Divider | #C9D7E2 | 关系线、区域边界 |
| Inverse | #FFFFFF | 深色区域文字 |
| Mint | #D9EEEA | 技能与证据区域 |

## IV. Typography System

### Font Plan

| Role | Character (Reference) | Primary | English if non-English | Fallback tail |
| --- | --- | --- | --- | --- |
| Title | 清晰无衬线 | Microsoft YaHei | Arial | sans-serif |
| Body | 清晰无衬线 | Microsoft YaHei | Arial | sans-serif |
| Code | 路径与调用标识 | Microsoft YaHei | Consolas | monospace |

- **Title stack**: Microsoft YaHei, Arial
- **Body stack**: Microsoft YaHei, Arial
- **Code stack**: Microsoft YaHei, Consolas
- **Role rationale**: Code 使调用路径与解释文字在视觉上可区分。

### Font Size Hierarchy

| Purpose | Anchor Size (px) |
| --- | ---: |
| Body | 26 |
| Title | 40 |
| Subtitle | 30 |
| Annotation | 20 |
| Footnote | 14 |
| Code | 20 |
| Display | 88 |
| Cover | 64 |

## V. Layout Principles

### Deck-wide Direction

- **Hierarchy direction**: 标题先给出问题或结论，主体展示机制，必要的限制附在对应关系旁。
- **Composition tendency**: 分层、路径、循环、对比和共享区域按内容选择；单个组件用职责和取舍配对。
- **Cross-page continuity**: 请求、执行、证据三个概念依次展开；深蓝和青绿保持含义。
- **Spacing posture**: 核心图页保留留白，备份页允许更高文字密度。
- **Spacing anchors**: page margin 60 px; block gap 24 px; column gutter 40 px; corner radius 0 px; body leading 40 px.

## VI. Icon Usage Specification

- **Primary bundled library**: none

| Icon Path | Suitable Scenarios |
| --- | --- |

## VIII. Image Resource List

| Filename | Dimensions | Ratio | Purpose | Type | Image pattern | Crop Policy | Acquire Via | Status | Reference | text_policy | page_role |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

| tracing_reference.png | 2354x1823 | 1.2913 | P19 调用树概念 | Diagram | 大图配左侧关键结论 | no-crop | user | Existing | Tony Bai，第19讲；示例数字 | preserve | evidence |

## IX. Content Outline

#### Slide 01 - OmicsClaw
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: OmicsClaw
- **Core message**: OmicsClaw
- **Content**: 从分析请求到可审核结果；核心组件为何存在，又如何共同支撑组学分析；请求；执行；证据；88 个技能 · 7 个领域 · 83 个函数库；当前实现 · 2026.10.08

#### Slide 02 - 模型能决定下一步，执行更需要支撑
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: 模型能决定下一步，执行更需要支撑
- **Core message**: 模型能决定下一步，执行更需要支撑
- **Content**: 工具调用、代码生成与多步推理发展 → 根据真实结果调整后续动作；Framework 中的预设工作流；开发者事先定义路径与分支条件；输入任务；路由；步骤 A；步骤 B；适合路径稳定、输入边界清楚的任务；Harness 支撑持续行动循环；模型根据观察选择下一步，运行时约束执行；模型判断；工具执行；结果观察；反馈进入下一轮；运行支撑：权限 · 上下文与状态 · 恢复 · 观测；OmicsClaw：让分析步骤随数据与结果调整，同时保留执行边界和证据。；两种方式可以组合：Framework 可承载行动循环，固定工作流可作为其中的工具。

#### Slide 03 - OmicsClaw 需要把每一步判断接稳
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: OmicsClaw 需要把每一步判断接稳
- **Core message**: OmicsClaw 需要把每一步判断接稳
- **Content**: 检查数据；选择方法；运行分析；检查真实输出；结果不合适，就调整下一步；信息不断累积；上下文 · 记忆 · 计划；记住依据与进度；动作可能失败或越界；权限 · 环境 · 恢复；让执行可控地继续；结论需要可靠依据；观测 · 验证 · 回放；把结果连回过程；这些持续运行的支撑，构成 OmicsClaw 的 Harness。

#### Slide 04 - 系统地图：交互、决策、执行与证据
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: 系统地图：交互、决策、执行与证据
- **Core message**: 系统地图：交互、决策、执行与证据
- **Content**: CLI　/　Desktop　/　Channel；entry：集中组装与会话管理；engine 循环　·　provider 适配　·　schema 共同契约；tools　/　permission　/　sandbox　/　MCP；工具调用、权限决定与命令执行；skills + _sdk + 分析模块；函数与步骤 → 图表 / 台账 / 回放 / 审查；支撑能力；contextmemory / planningsubagent / skillenvhooks / telemetry

#### Slide 05 - engine：一个内核维持 ReAct 循环
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: engine：一个内核维持 ReAct 循环
- **Core message**: engine：一个内核维持 ReAct 循环
- **Content**: Message；模型判断；ToolCall；ToolResult；观察进入下一轮；模型不再请求工具时，返回最终回答。；一个内核，两种输出；run：返回完整结果run_stream：逐步给事件

#### Slide 06 - schema：先统一组件之间的语言
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: schema：先统一组件之间的语言
- **Core message**: schema：先统一组件之间的语言
- **Content**: 共同交换契约；Message · ToolCall · ToolResultUsage · StreamChunk；provider；engine；tools；各层依赖 schema；schema 不依赖其他项目层。；为什么放在独立叶子层；标准库即可导入供应商字段留在适配器减少循环导入的机会

#### Slide 07 - provider：把接口差异收在适配层
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: provider：把接口差异收在适配层
- **Core message**: provider：把接口差异收在适配层
- **Content**: engine + schema；LLMProvider 统一调用接口；OpenAI-compatible；Anthropic Messages；归一后的结果；归一后的结果；模型可以替换；工具、会话和分析步骤继续复用。

#### Slide 08 - 通用工具连接模型与科学方法
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: 通用工具连接模型与科学方法
- **Core message**: 通用工具连接模型与科学方法
- **Content**: 模型调用；ToolRegistry；定义 · 查找 · 执行；read_file　读取说明与结果；write_file　写分析步骤；edit_file　修改参数与代码；bash　运行 Python / R / CLI；新增科学方法通过技能接入；主循环继续使用统一工具接口。

#### Slide 09 - PromptAssembler：把信息汇到一次请求
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: PromptAssembler：把信息汇到一次请求
- **Core message**: PromptAssembler：把信息汇到一次请求
- **Content**: 分析契约；OMICSCLAW.md + 安全规则；可用能力；工具 · 技能目录 · 环境；当前状态；记忆摘要 · 计划指导；PromptAssembler；system 消息 + 对话历史 + 当前请求 → 模型；规则各有来源，组装顺序由 entry 管理。

#### Slide 10 - skills：先看目录，再加载方法
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: skills：先看目录，再加载方法
- **Core message**: skills：先看目录，再加载方法
- **Content**: 系统提示中的目录；name + description；→；use_skill；→；完整方法与目录；CLI / API / Dependencies；只加载当前需要的方法，给数据与分析结果留下上下文空间。；SKILL.md 是单一事实源；说明何时加载、何时跳过方法、输入、依赖集中维护；加载说明后才执行；通过 bash 运行脚本或 step runner随后读取真实输出，决定下一步

#### Slide 11 - 压缩历史时，保留下一步需要的依据
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: 压缩历史时，保留下一步需要的依据
- **Core message**: 压缩历史时，保留下一步需要的依据
- **Content**: 累积历史；规则与目标；较早调用 + 大量输出；最近调用 + 结果；保留；卸载 · 总结；保留；规则与目标；文件引用 / 摘要；最近调用 + 结果；下一轮空间；工具调用与结果保持配对；按预算逐级处理：长输出卸载 → 局部总结 → 全面总结 → 紧急截断。

#### Slide 12 - memory 记事实，planning 记进度
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: memory 记事实，planning 记进度
- **Core message**: memory 记事实，planning 记进度
- **Content**: memory；工作区会话 · 长期事实；SQLite / 检索 / MEMORY.md；planning；目标 · 步骤 · 当前状态；PlanBook / plan_write；下一次模型判断；新事实与新进度回写；上下文服务当前判断；持久状态让长任务能够延续。

#### Slide 13 - entry 把组件接成可管理的会话
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: entry 把组件接成可管理的会话
- **Core message**: entry 把组件接成可管理的会话
- **Content**: provider / registry / prompt / memory / permission / sandbox / subagents；同会话串行，跨会话并发；会话 A；exchange 1；→；排队 2；会话 B；exchange 1；每个 exchange 独立 Task，立即返回可管理 handle。；队列、事件和历史各有边界；队列有上限，可取消等待项事件缓冲有界，支持观察与续接成功交换才替换会话历史

#### Slide 14 - 让错误进入判断，而不是重复同一动作
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: 让错误进入判断，而不是重复同一动作
- **Core message**: 让错误进入判断，而不是重复同一动作
- **Content**: 工具返回错误；模型读取错误与结果；修改输入 / 参数 / 代码；再执行，读取新的观察；暂时性模型服务故障；有限重试 + 退避；超出预算 / 用户取消；停止执行；恢复要有边界；已经完成的文件操作不会自动回滚。

#### Slide 15 - permission：先决定能否执行
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: permission：先决定能否执行
- **Core message**: permission：先决定能否执行
- **Content**: 模型调用；PermissionGate；用户 / 入口；工具实现；提交工具动作；ask：暂停并询问；允许 / 拒绝；允许后执行；模式 + 规则 + 风险检查；allow 直接执行，ask 等待授权，deny 阻止动作。

#### Slide 16 - sandbox 与 local-first 的边界
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: sandbox 与 local-first 的边界
- **Core message**: sandbox 与 local-first 的边界
- **Content**: 宿主机 / 工作区；文件工具由 Workspace 限定路径；Docker / Podman：只接管 bash；默认无网络、代码只读挂载；工作区供分析读写，内部状态另有遮蔽与交换路径；模型通信边界；provider 发送上下文消息与工具结果可能进入远程模型；local-first 描述分析位置，不是绝对的数据不出机承诺。

#### Slide 17 - skillenv：补依赖时保留基础环境
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: skillenv：补依赖时保留基础环境
- **Core message**: skillenv：补依赖时保留基础环境
- **Content**: use_skill；→；缺包说明；→；权限审批；→；overlay；默认 probe；基于 Dependencies；部署须启用 install；返回明确解释器路径；overlay：增加当前任务所需包；基础环境：保持不变

#### Slide 18 - MCP 扩展工具，subagent 分担子任务
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: MCP 扩展工具，subagent 分担子任务
- **Core message**: MCP 扩展工具，subagent 分担子任务
- **Content**: MCP 外部工具；服务调用；统一 Tool 接口；主 Agent；课题目标与进度；subagent 独立上下文；限定任务与工具；执行并返回摘要；委派；回传；子任务保留独立上下文，主任务接收结果后继续决策。

#### Slide 19 - observability：让过程可以定位
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: observability：让过程可以定位
- **Core message**: observability：让过程可以定位
- **Content**: OmicsClaw 记录；interaction → turn→ llm_request / tool；关注三件事；耗时 · token · 工具调用；hooks 另记录工具审计
- **Images**: tracing_reference.png；右侧完整原图，左侧项目要点；数字仅示例。

#### Slide 20 - 用任务结果判断是否完成
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: 用任务结果判断是否完成
- **Core message**: 用任务结果判断是否完成
- **Content**: 任务用例；独立工作区运行；工具轨迹 · 文件结果 · 用量；evals：机制与行为；工具参数 · 权限 · 文件变化；bench：真实任务运行；答案 · 评分 · 基础设施故障；任务完成之后，仍需检验统计假设与生物学结论。

#### Slide 21 - 框架与科学代码通过文件契约协作
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: 框架与科学代码通过文件契约协作
- **Core message**: 框架与科学代码通过文件契约协作
- **Content**: omicsclaw/；模型、权限、会话工具与运行调度；不导入 skills 科学代码；SKILL.md；依赖字面量；结果与文件契约；skills/；方法函数与脚本_sdk + domain/_lib；不导入 omicsclaw 框架；科学依赖按任务加载；同一技能可以脱离 Agent 单独执行。

#### Slide 22 - 同一算法，函数库与 CLI 各负其责
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: 同一算法，函数库与 CLI 各负其责
- **Core message**: 同一算法，函数库与 CLI 各负其责
- **Content**: _api.py；返回数据对象或 Figure；分析 step / load_skill；组合方法、验证结果、显式写输出；薄 CLI；解析参数、读写文件、生成报告；已迁移：83 个函数库；网络 fetch_* 与计算分开；5 个技能保留 CLI；函数结果可以直接交给下一个分析步骤。

#### Slide 23 - 分析模块把代码与输出放在明确位置
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: 分析模块把代码与输出放在明确位置
- **Core message**: 分析模块把代码与输出放在明确位置
- **Content**: 一个模块对应一个可独立汇报的阶段。；例如：QC、聚类、注释。；Project；docs/analysis_strategy/STRATEGY.md；问题、数据、跨模块计划与决策；analysis/01_qc/；README.md + Python / R 步骤；方法、输入、参数选择与验证代码；results/01_qc/；图表、中间文件、日志与 REPORT；模块是分析交付单元；技能是可复用的方法单元。

#### Slide 24 - run、status、replay 分别回答不同问题
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: run、status、replay 分别回答不同问题
- **Core message**: run、status、replay 分别回答不同问题
- **Content**: 上游文件变化 → 输入哈希变化 → 下游步骤 stale；status；哪里过期，为什么？；只观察，不执行分析；run；执行当前过期步骤；适合迭代修改；replay；全部步骤重新执行；Python validate 最后运行；显式 IO 才进入台账；文件回放成功仍需检查统计假设。

#### Slide 25 - 先交付验证结果，审查与接受由用户触发
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: 先交付验证结果，审查与接受由用户触发
- **Core message**: 先交付验证结果，审查与接受由用户触发
- **Content**: replay + REPORT；验证后立即展示结果；→；用户请求 review；检查材料，报告 findings；→；用户明确 accept；接受并冻结模块；用户明确跳过审查；→；审查绑定具体材料；原文 + receipt 绑定 replay 与哈希材料改动后，旧审查不再适用；用户决定是否接受；未审查结果不能称已批准改已接受模块先 revise，保留 baseline

#### Slide 26 - 四模块案例：检查应落在实际输出上
- **Audience move**: 通过图理解组件解决的问题。
- **Relationships**: order: 按连接线理解执行或信息流；membership: 区域内节点属于同一职责。
- **Title**: 四模块案例：检查应落在实际输出上
- **Core message**: 四模块案例：检查应落在实际输出上
- **Content**: 01 QC 与过滤；→；02 预处理聚类；→；03 marker 富集；→；04 R 核对；验收记录支持什么；四模块均 accepted / frozenR 独立运行的 CSV 与原件一致上游改变后，下游 R 显示过期；审查实际改变了什么；补充 counts 逐元素验证纠正基因与效应值的错误配对

#### Slide 27 - 科学分析需要连续判断，而现有评测常只看局部
- **Audience move**: 从论文动机和证据推导项目研究，再进入实验验证。
- **Relationships**: order: 诊断证据进入后续决策；contrast: 论文发现与拟验证方案、成对任务的不同决策。
- **Title**: 科学分析需要连续判断，评测也要覆盖全过程
- **Core message**: 科学分析需要连续判断，评测也要覆盖全过程
- **Content**: 常见局部评测；整理好的数据；明确指定的方法；局部结果；真实研究过程；原始数据；质控与探索；建模与诊断；科学结论；诊断反馈改变分析路径；必要性：检验数据能否支持结论，以及判断失误如何传到最终结果。

#### Slide 28 - GeneBench-Pro：把连续判断变成可检验任务
- **Audience move**: 从论文动机和证据推导项目研究，再进入实验验证。
- **Relationships**: order: 诊断证据进入后续决策；contrast: 论文发现与拟验证方案、成对任务的不同决策。
- **Title**: GeneBench-Pro：把连续判断变成可检验任务
- **Core message**: GeneBench-Pro：把连续判断变成可检验任务
- **Content**: 129 个任务；10 个领域 · 21 个子领域；简短背景 + 目标量；提供数据，不预设分析流程；多步判断；上游选择影响最终答案；诊断与方法选择；合理分析 → 可恢复的目标量；错误选择 → 偏离目标答案；受控模拟让答案可判定，也让错误路径能被单独检验。

#### Slide 29 - 发现异常之后，分析决策是否随之改变？
- **Audience move**: 从论文动机和证据推导项目研究，再进入实验验证。
- **Relationships**: order: 诊断证据进入后续决策；contrast: 论文发现与拟验证方案、成对任务的不同决策。
- **Title**: 发现异常之后，分析决策是否随之改变？
- **Core message**: 发现异常之后，分析决策是否随之改变？
- **Content**: 发现诊断线索；数据异常 · 统计假设受挑战；断点；调整后续分析；改模型 · 改目标 · 改路径；部分失败轨迹中，异常被注意到，却未改变关键选择。；结论边界；轨迹分析提供线索；尚未证明某种控制机制有效。；模拟任务的真实性有边界；终点二元评分压缩了中间过程信息。

#### Slide 30 - OmicsClaw：让诊断证据进入后续决策
- **Audience move**: 从论文动机和证据推导项目研究，再进入实验验证。
- **Relationships**: order: 诊断证据进入后续决策；contrast: 论文发现与拟验证方案、成对任务的不同决策。
- **Title**: 让诊断证据进入后续决策
- **Core message**: 让诊断证据进入后续决策
- **Content**: 已有基础；拟研究扩展；技能与分析步骤；记录目标量、假设与诊断证据；执行计划与工具循环；证据触发修正、补充信息或停止；回放、审查与评测记录；验证决策改变是否减少错误结论；同一模型与预算下，加入证据驱动的决策检查，能否减少无依据结论，同时保留有效任务的完成能力？

#### Slide 31 - 沿着一次分析，研究三类决策机制
- **Audience move**: 从论文动机和证据推导项目研究，再进入实验验证。
- **Relationships**: order: 诊断证据进入后续决策；contrast: 论文发现与拟验证方案、成对任务的不同决策。
- **Title**: 沿着一次分析，研究三类决策机制
- **Core message**: 沿着一次分析，研究三类决策机制
- **Content**: 分析前；分析中；形成结论时；可回答性检查；EstimandGuard；数据支持目标推断吗？；核对实验单位、混杂与重复；科学错误恢复；识别 → 修正 / 补充 / 停止；代码成功，推断有效吗？；检查并验证后续处理；结论稳定性；预算内主动选择分析分支；换条合理路径还成立吗？；比较目标一致的有效分析；首期：可回答性检查 + 科学恢复；结论稳定性随后扩展。

#### Slide 32 - 改变实验设计，Agent 会改变决策吗？
- **Audience move**: 从论文动机和证据推导项目研究，再进入实验验证。
- **Relationships**: order: 诊断证据进入后续决策；contrast: 论文发现与拟验证方案、成对任务的不同决策。
- **Title**: 改变实验设计，Agent 会改变决策吗？
- **Core message**: 改变实验设计，Agent 会改变决策吗？
- **Content**: 同一研究问题：比较疾病组与对照组的表达差异；A：目标可估计；独立 donor，条件与批次可分离；给出效应与不确定性；B：条件与批次完全混杂；疾病全在批次 1，对照全在批次 2；停止该效应推断，说明缺少的设计；科学成功率 · 无依据结论率 · 错误拒绝率；过程诊断：发现异常 → 采取动作 → 验证结论；终点评分独立。

#### Slide 33 - 先证明任务有效，再验证方法增益
- **Audience move**: 从论文动机和证据推导项目研究，再进入实验验证。
- **Relationships**: order: 诊断证据进入后续决策；contrast: 论文发现与拟验证方案、成对任务的不同决策。
- **Title**: 先证明任务有效，再验证方法增益
- **Core message**: 先证明任务有效，再验证方法增益
- **Content**: 20–30；题 Pilot；单细胞 + Bulk RNA-seq；1　校准任务；正确路径、错误路径与合理替代方法；2　冻结评测；独立评分与隐藏测试统一模型、工具与预算；3　消融验证；基线 → 单项机制→ 组合机制；目标：减少无依据结论，同时保持有效任务的完成能力。

## X. Speaker Notes Requirements

第 27–33 页依次讲论文背景、评测设计、发现与边界、项目启发、候选机制、成对实验与首期计划。论文事实与项目假设分别标注；讲稿记录来源及版本边界。
