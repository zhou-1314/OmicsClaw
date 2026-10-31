<!-- ppt-master-schema: design-spec/v1 -->
# OmicsClaw Group Meeting - Design Spec

## I. Project Information

| Item | Value |
| --- | --- |
| Project Name | OmicsClaw_GroupMeeting |
| Canvas Format | PPT 16:9, 1280 × 720 |
| Page Count | 27 |
| Primary Language | zh-CN |
| Target Audience | 课题组 PI、研究生与生信研究人员 |
| Communication Intent | 汇报当前架构，解释组件职责、协作与取舍，讨论验证重点 |
| Desired Audience Outcome | 能说明分析请求如何变为可回放、可审核的模块结果，区分当前实现与验证范围 |
| Core Message / Ask / Action | 单向分层框架、独立技能和模块证据链连接自然语言请求与科研输出 |
| Delivery Context | 现场中文组会；27 页核心内容；按讨论深度安排约 30 分钟 |
| Artifact Afterlife | 附代码来源与讲稿的可编辑组内参考 |
| Reading Mode | balanced |
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

本轮沿用原稿视觉体系，以 OmicsClaw 为唯一主线。保留问题、机制与价值，合并重复解释，移除外围细节。用户已授权自主完成全部修订。27 页，正文 26 px，讲稿开启，逐对象动画与旁白关闭。

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

## IX. Content Outline

#### Slide 01 - OmicsClaw
- **Audience move**: 从请求到结果。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: OmicsClaw
- **Core message**: 从请求到结果。
- **Content**: 从分析请求到可审核结果；核心组件为何存在，又如何共同支撑组学分析；请求；执行；证据；88 个技能 · 7 个领域 · 83 个函数库；当前实现 · 2026.10.08

#### Slide 02 - 模型能决定下一步，执行更需要支撑
- **Audience move**: 理解模型能力发展如何引出持续执行的运行时需求。
- **Relationships**: contrast: 预设工作流与运行时决策；order: 判断、执行、观察再判断；parent: Harness 支撑行动循环。
- **Title**: 模型能决定下一步，执行更需要支撑
- **Core message**: 工具调用与多步推理使动态行动更可行，权限、状态和恢复让行动可持续。
- **Content**: 工具调用、代码生成与多步推理发展；预设路径依靠输入、路由与步骤；Harness 组织判断、执行和观察循环，提供权限、上下文、恢复与观测；两种方式可以组合；OmicsClaw 根据真实输出调整分析步骤。
- **Topology decision**: topology=yes; 左侧分支、右侧反馈回路。
- **Geometry signature**: 解释必要性 → 两种控制方式并列 → 分支与闭环连线。

#### Slide 03 - 分析助手为什么需要运行时支撑
- **Audience move**: 分析任务为什么需要持续管理。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: 分析助手为什么需要运行时支撑
- **Core message**: 分析任务为什么需要持续管理。
- **Content**: 方法与环境；同一问题有多种分析方法Python、R 与外部 CLI 混用依赖、数据库和输入契约不同；需要：可发现的方法与可执行的环境；过程与证据；参数改动后，下游是否过期？结果能否脱离原会话再运行？报告里的结论对应哪份输出？；需要：显式步骤、回放与审查；设计目标：把方法执行接到可检查的分析过程上；目标：方法找得到、过程接得上、结果查得清。

#### Slide 04 - 系统地图：交互、决策、执行与证据
- **Audience move**: 先建立组件之间的关系。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: 系统地图：交互、决策、执行与证据
- **Core message**: 先建立组件之间的关系。
- **Content**: CLI　/　Desktop　/　Channel；entry：集中组装与会话管理；engine 循环　·　provider 适配　·　schema 共同契约；tools　/　permission　/　sandbox　/　MCP；工具调用、权限决定与命令执行；skills + _sdk + 分析模块；函数与步骤 → 图表 / 台账 / 回放 / 审查；支撑能力；contextmemory / planningsubagent / skillenvhooks / telemetry；这里表示运行职责。严格导入方向由各层边界测试约束。

#### Slide 05 - engine：一个内核维持 ReAct 循环
- **Audience move**: 每一步都根据结果再决定。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: engine：一个内核维持 ReAct 循环
- **Core message**: 每一步都根据结果再决定。
- **Content**: Message；模型判断；ToolCall；ToolResult；观察进入下一轮；模型不再请求工具时，返回最终回答。；一个内核，两种输出；run：返回完整结果run_stream：逐步给事件；预算与终止由循环管理；失败抛出，取消继续传播。提示构造、会话存储和具体工具分别交给外部组件。

#### Slide 06 - schema：先统一组件之间的语言
- **Audience move**: 让组件交换同一种消息。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: schema：先统一组件之间的语言
- **Core message**: 让组件交换同一种消息。
- **Content**: 共同交换契约；Message · ToolCall · ToolResultUsage · StreamChunk；provider；engine；tools；各层依赖 schema；schema 不依赖其他项目层。；为什么放在独立叶子层；标准库即可导入供应商字段留在适配器减少循环导入的机会；共同契约减少组件耦合，模型适配、执行与工具可以分别演进。

#### Slide 07 - provider：把模型差异收在适配层
- **Audience move**: 更换模型时保留分析流程。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: provider：把模型差异收在适配层
- **Core message**: 更换模型时保留分析流程。
- **Content**: engine；LLMProvider；generate(messages, tools)generate_stream(...)bind(**overrides)；OpenAI-compatible；Anthropic Messages；配置留在实例；模型、温度、输出上限在 provider按需 bind，engine 无须读取厂商配置；协议归一，能力仍有差别；统一流式事件、工具调用与错误模型的路由与推理质量仍需实测

#### Slide 08 - ToolRegistry：注册、描述、执行集中在一处
- **Audience move**: 把模型选择变成真实动作。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: ToolRegistry：注册、描述、执行集中在一处
- **Core message**: 把模型选择变成真实动作。
- **Content**: 1 注册实现；register(tool)；一个对象同时持有定义、策略和执行方法；→；2 暴露定义；available_tools()；名称、描述、参数 Schema交给 provider，再传给模型；→；3 路由执行；execute(call)；按名称找到实现返回 ToolResult；模型选择工具，registry 查找实现，engine 接收观察；主循环不认识具体科学方法。；模型可见的定义，与本机执行策略分开。；风险、审批和并发声明保留在工具层；部署决定哪些工具可见。；read_file / write_file / edit_file / bash 承担读取、修改与执行。

#### Slide 09 - PromptAssembler：把规则按职责组装
- **Audience move**: 每次判断都能读到当前规则。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: PromptAssembler：把规则按职责组装
- **Core message**: 每次判断都能读到当前规则。
- **Content**: 有序的 Section；运行契约 OMICSCLAW.md安全规则、工具与计划指导沙箱说明、技能目录、环境长期记忆摘要；entry 决定顺序，source 按需要读取内容；render → 一个 system 消息；空段落跳过，保留加入顺序统计每段的 token 估计和历史、当前请求一起发给模型；项目分析规范与当前环境，在调用模型前统一组装。；规则有明确来源，组件变化时可以局部更新。

#### Slide 10 - skills：先看目录，再加载方法
- **Audience move**: 需要哪种方法，再读取哪份说明。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: skills：先看目录，再加载方法
- **Core message**: 需要哪种方法，再读取哪份说明。
- **Content**: 系统提示中的目录；name + description；→；use_skill；→；完整方法与目录；CLI / API / Dependencies；只加载当前需要的方法，给数据与分析结果留下上下文空间。；SKILL.md 是单一事实源；说明何时加载、何时跳过方法、输入、依赖集中维护；加载说明后才执行；通过 bash 运行脚本或 step runner随后读取真实输出，决定下一步

#### Slide 11 - context：把有限窗口留给当前判断
- **Audience move**: 分析越长，越要保留关键依据。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: context：把有限窗口留给当前判断
- **Core message**: 分析越长，越要保留关键依据。
- **Content**: 可用预算 = 模型窗口 − 输出预留 − 工具定义 − 安全余量；正常；卸载长输出；局部总结；全面总结；紧急截断；保持历史；以文件引用替代；压缩较旧部分；保留最近消息；不再调模型；压缩仍要保留工具调用与结果配对，防止对话失去可解释性。；意义：长任务继续推进时，仍能理解前面的动作与结果。

#### Slide 12 - memory 与 planning 保存不同信息
- **Audience move**: 分开保存事实与进度。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: memory 与 planning 保存不同信息
- **Core message**: 分开保存事实与进度。
- **Content**: memory：已经知道什么；SQLite 保存工作区会话长期事实经 FTS5 检索MEMORY.md 提供有界摘要；压缩前先提取长期事实；摘要通过闭包在后续轮次重新读取。；planning：接下来还要做什么；PlanBook 独立于聊天历史每次模型调用前重新注入恢复会话时还原任务状态；避免历史压缩后遗忘任务目标；通过 plan_write 更新进展。；计划里的“完成”，不等于模块已通过科学验证与用户验收。

#### Slide 13 - entry 把组件接成可管理的会话
- **Audience move**: 多种界面共享同一套运行逻辑。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: entry 把组件接成可管理的会话
- **Core message**: 多种界面共享同一套运行逻辑。
- **Content**: CLI / Desktop / Channel → build_app；provider / registry / prompt / memory / permission / sandbox / subagents；同会话串行，跨会话并发；会话 A；exchange 1；→；排队 2；会话 B；exchange 1；每个 exchange 独立 Task，立即返回可管理 handle。；队列、事件和历史各有边界；队列有上限，可取消等待项事件缓冲有界，支持观察与续接成功交换才替换会话历史；统一会话管理，避免不同入口各自维护一套分析逻辑。

#### Slide 14 - 失败之后，谁负责恢复？
- **Audience move**: 区分可以修正与必须停止的错误。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: 失败之后，谁负责恢复？
- **Core message**: 区分可以修正与必须停止的错误。
- **Content**: 工具参数或输入错了；is_error 观察回到模型 → 重新读取、修正参数，再决定是否调用；模型请求出现暂时故障；网络错误、429 / 5xx → 有限预算与退避；其他 4xx 不盲目重试；总 deadline 已耗尽，或用户取消；结束请求或传播取消 → 不追加重试，不回滚已完成的工具副作用；可修正的错误成为下一轮输入；取消与预算耗尽让执行及时停止。

#### Slide 15 - permission：策略决定，入口负责询问
- **Audience move**: 高风险动作需要明确授权。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: permission：策略决定，入口负责询问
- **Core message**: 高风险动作需要明确授权。
- **Content**: 工具调用；→；PermissionGate；模式 + 规则 + 危险命令模式；决定 allow / ask / deny；→；执行 / 拒绝；需要时发起审批；入口显示卡片并收回决定；一次允许 / 会话允许 / 记住匹配规则，分别对应不同的授权范围。；运行姿态；default / read-only / auto-approve；read-only 不允许 task 委派不受限的子任务。；仍保留的边界；危险调用与保护路径仍可要求审批；命令模式匹配不能穷尽所有绕过方式。

#### Slide 16 - sandbox 与 local-first 的边界
- **Audience move**: 限定分析命令可以接触的资源。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: sandbox 与 local-first 的边界
- **Core message**: 限定分析命令可以接触的资源。
- **Content**: 宿主机 / 工作区；文件工具由 Workspace 限定路径；Docker / Podman：只接管 bash；默认无网络、代码只读挂载；工作区供分析读写，内部状态另有遮蔽与交换路径；模型通信边界；provider 发送上下文消息与工具结果可能进入远程模型；local-first 描述分析位置，不是绝对的数据不出机承诺。；取消对话不会回滚已完成的工具副作用。

#### Slide 17 - skillenv：补依赖时保留基础环境
- **Audience move**: 为当前分析补齐缺失的能力。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: skillenv：补依赖时保留基础环境
- **Core message**: 为当前分析补齐缺失的能力。
- **Content**: use_skill；→；缺包说明；→；权限审批；→；overlay；默认 probe；基于 Dependencies；部署须启用 install；返回明确解释器路径；框架以 AST 读取依赖注册表的纯字面量，不导入科学技能模块。；overlay：增加当前任务所需包；基础环境：保持不变；仅安装白名单中请求的依赖固定 wheel，检查 pip 配置新增 pip check 问题则回滚；同一模块保持解释器一致；切换需记录原因。

#### Slide 18 - MCP 与 subagent 扩展两类能力
- **Audience move**: 接入工具与分解子任务。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: MCP 与 subagent 扩展两类能力
- **Core message**: 接入工具与分解子任务。
- **Content**: MCP：接入外部工具；.mcp.json 声明服务stdio 或 Streamable HTTP转换成普通 Tool 注册；engine 无须区分本地与外部工具；权限与工具定义仍走统一接口。；subagent：运行有范围的子任务；角色提示 + 缩小的工具集独立上下文，复用同一 engineentry 构造子运行并接收回复；用户请求后，module-reviewer 检查材料；其职责是审查，不替用户接受模块。；独立任务上下文能减少干扰，不能自动消除模型的共同盲点。

#### Slide 19 - hooks 与 observability 观察已有执行
- **Audience move**: 知道时间与调用花在哪里。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: hooks 与 observability 观察已有执行
- **Core message**: 知道时间与调用花在哪里。
- **Content**: 工具执行链；permission：先决定能否执行；hooks：拦截与 JSONL 审计；tool：实际动作；运行跨度与度量；interaction；turn；llm_request / tool；LLM 耗时、输入 token、输出 token、工具次数、工具耗时、轮次数。消费现有 EngineEvent；OTEL 可选。默认不采集对话和工具正文载荷。；被权限拒绝的调用不会进入内侧 hook。

#### Slide 20 - 评估要检查动作与副作用
- **Audience move**: 用实际行为检验任务完成情况。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: 评估要检查动作与副作用
- **Core message**: 用实际行为检验任务完成情况。
- **Content**: 任务用例 → 实际运行 → 工具轨迹与文件结果 → 检查；检查任务实际做了什么，避免只凭最终回答判断成功。；机制回归：规则是否守住；预设调用，控制测试条件检查工具选择与参数检查审批、越界写入、调用次数；真实任务：分析能否完成；独立工作区运行任务用例记录答案、用量与评分服务故障与任务得分分开；回归与路由正确，仍需检验生物学结论和统计假设。；evals 检查机制与行为；bench 组织真实任务批量运行和评分。

#### Slide 21 - 框架与科学代码通过文件契约协作
- **Audience move**: 方法可以脱离 Agent 独立使用。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: 框架与科学代码通过文件契约协作
- **Core message**: 方法可以脱离 Agent 独立使用。
- **Content**: omicsclaw/；模型、权限、会话工具与运行调度；不导入 skills 科学代码；SKILL.md；依赖字面量；结果与文件契约；skills/；方法函数与脚本_sdk + domain/_lib；不导入 omicsclaw 框架；科学依赖按任务加载；同一技能可以脱离 Agent 单独执行。；代价：跨边界数据必须明确序列化，接口变化需要同步核对文件契约。

#### Slide 22 - 同一算法，函数库与 CLI 各负其责
- **Audience move**: 让计算结果直接进入下一步。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: 同一算法，函数库与 CLI 各负其责
- **Core message**: 让计算结果直接进入下一步。
- **Content**: _api.py；返回数据对象或 Figure；分析 step / load_skill；组合方法、验证结果、显式写输出；薄 CLI；解析参数、读写文件、生成报告；已迁移：83 个函数库；网络 fetch_* 与计算分开；5 个技能保留 CLI；函数结果可以直接交给下一个分析步骤。；XCMS 仅支持合成 demo；真实 raw-MS 输入会被拒绝。

#### Slide 23 - 分析模块把代码与输出放在明确位置
- **Audience move**: 让每个阶段都能独立解释。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: 分析模块把代码与输出放在明确位置
- **Core message**: 让每个阶段都能独立解释。
- **Content**: 一个模块对应一个可独立汇报的阶段。；例如：QC、聚类、注释。；Project；docs/analysis_strategy/STRATEGY.md；问题、数据、跨模块计划与决策；analysis/01_qc/；README.md + Python / R 步骤；方法、输入、参数选择与验证代码；results/01_qc/；图表、中间文件、日志与 REPORT；notebooks / provenance / reviews / baseline；模块是分析交付单元；技能是可复用的方法单元。

#### Slide 24 - run、status、replay 分别回答不同问题
- **Audience move**: 修改后知道该重跑什么。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: run、status、replay 分别回答不同问题
- **Core message**: 修改后知道该重跑什么。
- **Content**: 上游文件变化 → 输入哈希变化 → 下游步骤 stale；步骤代码变化也会触发过期；判断依据保存在 manifest 与运行台账。；status；哪里过期，为什么？；只观察，不执行分析；run；执行当前过期步骤；适合迭代修改；replay；全部步骤重新执行；Python validate 最后运行；显式 IO 才进入台账；文件回放成功仍需检查统计假设。；read_input / write_output 为依赖追踪提供证据，不是任意脚本的全量审计。

#### Slide 25 - 先交付验证结果，审查与接受由用户触发
- **Audience move**: 让审查与接受有明确依据。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: 先交付验证结果，审查与接受由用户触发
- **Core message**: 让审查与接受有明确依据。
- **Content**: replay + REPORT；验证后立即展示结果；→；用户请求 review；检查材料，报告 findings；→；用户明确 accept；接受并冻结模块；用户明确跳过审查；→；先展示验证与报告；用户决定是否进一步审查、是否接受当前结果。；审查绑定具体材料；原文 + receipt 绑定 replay 与哈希材料改动后，旧审查不再适用；请求审查不授权自动修复、重跑或接受。；用户决定是否接受；未审查结果不能称已批准改已接受模块先 revise，保留 baseline；跳过审查也需要用户明确授权。

#### Slide 26 - 四模块案例：检查应落在实际输出上
- **Audience move**: 从单细胞分析到可检查的交付。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: 四模块案例：检查应落在实际输出上
- **Core message**: 从单细胞分析到可检查的交付。
- **Content**: 01 QC 与过滤；→；02 预处理聚类；→；03 marker 富集；→；04 R 核对；前三模块使用函数库；第四模块用 Seurat 对 B 细胞结果作交叉核对。；验收记录支持什么；四模块均 accepted / frozenR 独立运行的 CSV 与原件一致上游改变后，下游 R 显示过期；审查实际改变了什么；补充 counts 逐元素验证纠正基因与效应值的错误配对；干净课题通过六项核心检查，额外 prompt 偏差仍保留在记录中。

#### Slide 27 - 下一阶段优先验证什么？
- **Audience move**: 下一步验证这些设计是否有效。
- **Relationships**: 保留现有组件关系与流程图，解释问题如何由机制解决。
- **Title**: 下一阶段优先验证什么？
- **Core message**: 下一步验证这些设计是否有效。
- **Content**: 组件边界让框架可以替换，；函数库让方法可以组合，；模块证据让分析交付可以检查。；选一个真实课题，测量重放和审查对结果可靠性的帮助。；重点讨论：任务完成率、错误发现率，以及生物学结论是否可靠。

## X. Speaker Notes Requirements

每页解释背景、机制和意义；来源写入讲稿。
