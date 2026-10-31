<!-- ppt-master-schema: design-spec/v1 -->
# OmicsClaw Group Meeting - Design Spec

## I. Project Information

| Item | Value |
| --- | --- |
| Project Name | OmicsClaw_GroupMeeting |
| Canvas Format | PPT 16:9, 1280 × 720 |
| Page Count | 45 |
| Primary Language | zh-CN |
| Target Audience | 课题组 PI、研究生与生信研究人员 |
| Communication Intent | 汇报当前架构，解释组件职责、协作与取舍，讨论验证重点 |
| Desired Audience Outcome | 能说明分析请求如何变为可回放、可审核的模块结果，区分当前实现与验证范围 |
| Core Message / Ask / Action | 单向分层框架、独立技能和模块证据链连接自然语言请求与科研输出 |
| Delivery Context | 现场中文组会；正文 40 页约 40 分钟，5 页技术备份 |
| Artifact Afterlife | 附代码来源与讲稿的可编辑组内参考 |
| Reading Mode | balanced |
| Content Strategy | 依据本地 90a3bec3 代码与交付记录，不将历史状态或计划当作当前成果 |
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

本轮沿用原稿视觉体系，参考 Tony Bai《从0开始构建 Agent Harness》本地教程第01至20讲的“问题、机制、实现、验证”顺序。用户此前对连续完成的授权仍有效；新增九页机制解释，并按最新源码更新状态。教程作为讲解依据，不复制其性能或安全结论。

用户已在聊天中授权所有后续设计决定，使用 free_design，不创建用户确认回执。

1. 组件逐层讲解：instructional 主线，swiss-minimal 栅格；微软雅黑与 Arial 的同一字体系统，白底深蓝与青绿。先建立地图再逐层放大，适合混合背景听众，选用此方案。
2. 进展简报：briefing 主线，editorial 排版；宋体标题配微软雅黑正文，米白底、墨黑与暗红。按能力、证据和缺口平行展开，便于会后检索，但现场理解组件依赖需要更多解释。
3. 案例叙事：narrative 主线，blueprint 线条；黑体标题配微软雅黑正文，深蓝底、白字与青色。跟随一次失败、修复和验收展开，便于讲故事，但难以完整覆盖每个基础组件。

三方案均可用本地文档和原生图形，无需图片生成。最终制作使用第一种方案；45 页，正文 26 px，讲稿开启，逐对象动画和旁白关闭。

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

#### Slide 01 - 参考 Harness 讲解路径，拆解 OmicsClaw 当前实现
- **Audience move**: 从项目名字进入本次架构主题。
- **Relationships**: order: 请求、执行、证据；membership: 88 技能与 7 个领域。
- **Title**: 参考 Harness 讲解路径，拆解 OmicsClaw 当前实现
- **Core message**: 框架和技能共同服务可追溯的科研分析。
- **Content**: 请求；执行；证据；88 个技能 · 7 个领域 · 83 个函数库；本地代码快照 90a3bec3
- **Cover impact**: 以“请求 → 执行 → 证据”为主视觉，原生文字作为标题。

#### Slide 02 - 分析助手为什么需要运行时支撑
- **Audience move**: 从功能集合转向要解决的科研执行问题。
- **Relationships**: contrast: 方法和环境的复杂性，与过程和证据的复杂性。
- **Title**: 分析助手为什么需要运行时支撑
- **Core message**: 方法调用和过程追溯都需要明确边界。
- **Content**: 方法与环境；同一问题有多种分析方法Python、R 与外部 CLI 混用依赖、数据库和输入契约不同；需要：可发现的方法与可执行的环境；过程与证据；参数改动后，下游是否过期？结果能否脱离原会话再运行？报告里的结论对应哪份输出？；需要：显式步骤、回放与审查；设计目标：把方法执行接到可检查的分析过程上；逐层追问：缺少这个组件会怎样？它接收什么、改变什么、如何验证？

#### Slide 03 - 88 个技能覆盖七个领域
- **Audience move**: 掌握当前功能范围和精确规模。
- **Relationships**: membership: 七域属于技能目录；单细胞与空间含已迁移函数库。
- **Title**: 88 个技能覆盖七个领域
- **Core message**: 技能数量与函数库迁移程度分别统计。
- **Content**: 88；SKILL.md；0 个被跳过；按加载器结果统计按实际目录递归核对接口；单细胞 30　　空间 17；Bulk RNA-seq 14　　基因组 10；蛋白组 8　　代谢组 8　　文献 1；领域规模是目录覆盖，不等于所有后端均已实测。数字仅作标签；区域面积不表示技能占比。；83 个函数库 + 5 个 CLI-only，覆盖全部七域

#### Slide 04 - 系统地图：交互、决策、执行与证据
- **Audience move**: 形成后续各组件的整体定位。
- **Relationships**: link: 入口连接 Agent 框架与工具，工具调用技能与步骤；membership: 上下文和策略支持执行。
- **Title**: 系统地图：交互、决策、执行与证据
- **Core message**: entry 集中组装，科学代码与框架通过文件契约协作。
- **Content**: CLI　/　Desktop　/　Channel；entry：集中组装与会话管理；engine 循环　·　provider 适配　·　schema 共同契约；tools　/　permission　/　sandbox　/　MCP；工具调用、权限决定与命令执行；skills + _sdk + 分析模块；函数与步骤 → 图表 / 台账 / 回放 / 审查；支撑能力；contextmemory / planningsubagent / skillenvhooks / telemetry；这里表示运行职责。严格导入方向由各层边界测试约束。

#### Slide 05 - 一次分析请求怎样流过系统
- **Audience move**: 能按先后顺序复述运行链条。
- **Relationships**: order: 接入、组装、决策、审批执行、观察、交付；link: 工具结果回到模型。
- **Title**: 一次分析请求怎样流过系统
- **Core message**: 对话循环产生动作，模块机制保留分析证据。
- **Content**: 1；2；3；4；接入会话；组装上下文；模型决策；审批执行；规范输入、排队保存会话边界；契约、历史、计划技能目录与预算；use_skill 读说明选择工具与参数；bash 执行步骤返回观察结果；结果回到模型，必要时继续；分析交付：步骤 → 输出 → replay → 报告 → 按需审查与接受；对话完成与模块被用户接受，是两个不同的状态。

#### Slide 06 - schema：先统一组件之间的语言
- **Audience move**: 理解公共叶子层为何独立。
- **Relationships**: link: provider、engine、tools 交换共同 schema；membership: 消息与工具结果。
- **Title**: schema：先统一组件之间的语言
- **Core message**: 供应商中立的数据契约降低跨层耦合。
- **Content**: 共同交换契约；Message · ToolCall · ToolResultUsage · StreamChunk；provider；engine；tools；各层依赖 schema；schema 不依赖其他项目层。；为什么放在独立叶子层；标准库即可导入供应商字段留在适配器减少循环导入的机会；工具参数保留原始 JSON 文本，避免无意改写协议证据。reasoning_content 与 tool 角色支持需要历史推理字段的端点。

#### Slide 07 - provider：把模型差异收在适配层
- **Audience move**: 理解更换模型所影响的边界。
- **Relationships**: link: 统一接口对应 OpenAI-compatible 与 Anthropic 两类适配器。
- **Title**: provider：把模型差异收在适配层
- **Core message**: 模型配置与 wire 协议不进入核心循环。
- **Content**: engine；LLMProvider；generate(messages, tools)generate_stream(...)bind(**overrides)；OpenAI-compatible；Anthropic Messages；配置留在实例；模型、温度、输出上限在 provider按需 bind，engine 无须读取厂商配置；协议归一，能力仍有差别；统一流式事件、工具调用与错误模型的路由与推理质量仍需实测

#### Slide 08 - engine：一个内核维持 ReAct 循环
- **Audience move**: 理解模型与工具如何交替。
- **Relationships**: order: Message、模型、ToolCall、ToolResult、Message 形成反馈；contrast: 返回最终结果与继续调用。
- **Title**: engine：一个内核维持 ReAct 循环
- **Core message**: run 与 run_stream 共用循环逻辑。
- **Content**: Message；模型判断；ToolCall；ToolResult；观察进入下一轮；模型不再请求工具时，返回最终回答。；一个内核，两种输出；run：返回完整结果run_stream：逐步给事件；预算与终止由循环管理；失败抛出，取消继续传播。提示构造、会话存储和具体工具分别交给外部组件。

#### Slide 09 - Thinking：模型能力与额外阶段要分开
- **Audience move**: 理解问题触发机制，以及教程与当前实现的区别。
- **Relationships**: link: 问题连接接口、执行机制与验证；contrast: 教程示例和本项目实现。
- **Title**: Thinking：模型能力与额外阶段要分开
- **Core message**: 以实际源码解释组件职责和设计取舍。
- **Content**: 教程的双阶段方案；请求 1：不给工具，只作规划；保留规划文本，作为后续输入；请求 2：提供工具，采取行动；额外调用增加延迟与上下文消耗；它是可比较的设计方案，收益需要实验。；OmicsClaw 当前实现；单个 ReAct 轮次调用 provider原生推理由模型与适配器处理工具调用继续进入同一循环；保留 reasoning_content 等协议字段；engine 没有强制的“先无工具、后有工具”阶段。；取舍：把额外思考变成可评估的策略，而非默认翻倍调用。；组学例子：复杂批次设计可能需要规划；读取一个 QC 表不一定需要第二次模型请求。


#### Slide 10 - ToolRegistry：注册、描述、执行集中在一处
- **Audience move**: 理解注册、并发与超时的分工。
- **Relationships**: order: register、available_tools、execute；contrast: 安全并发与独占写入。
- **Title**: ToolRegistry：注册、描述、执行集中在一处
- **Core message**: 可并发动作并行，未声明安全的动作独占执行。
- **Content**: 1 注册实现；register(tool)；一个对象同时持有定义、策略和执行方法；→；2 暴露定义；available_tools()；名称、描述、参数 Schema交给 provider，再传给模型；→；3 路由执行；execute(call)；按名称找到实现返回 ToolResult；模型选择工具，registry 查找实现，engine 接收观察；主循环不认识具体科学方法。；模型可见的定义，与本机执行策略分开。；风险、审批和并发声明保留在工具层；部署决定哪些工具可见。；定义按注册顺序稳定输出；通过 Protocol 形状接入 engine，避免反向导入。

#### Slide 11 - 四个基础工具怎样执行八十八种方法
- **Audience move**: 理解问题触发机制，以及教程与当前实现的区别。
- **Relationships**: link: 问题连接接口、执行机制与验证；contrast: 教程示例和本项目实现。
- **Title**: 四个基础工具怎样执行八十八种方法
- **Core message**: 以实际源码解释组件职责和设计取舍。
- **Content**: read_file；读取说明与结果；write_file；写入新步骤；edit_file；局部修改参数；bash；执行分析；基础原语之外，仍有 use_skill、计划、记忆、web、MCP 等按职责提供的工具。；use_skill → 方法说明 → 写分析 step → bash 运行；step 内通过 load_skill 组合函数库；CLI-only 方法通过 run_cli 调用。；增加科学方法主要增加技能文档与实现，不要求在 engine 中增加算法分支。；减少工具描述负担，同时保留权限和执行环境边界。；教程的 YOLO 讨论不能直接等同于本项目默认免审批；bash 仍受部署策略控制。


#### Slide 12 - edit_file：容错要以唯一匹配为底线
- **Audience move**: 理解问题触发机制，以及教程与当前实现的区别。
- **Relationships**: link: 问题连接接口、执行机制与验证；contrast: 教程示例和本项目实现。
- **Title**: edit_file：容错要以唯一匹配为底线
- **Core message**: 以实际源码解释组件职责和设计取舍。
- **Content**: path + source_text + target_text；L1 精确匹配；→；L2 忽略换行差异；→；L3 忽略首尾空白；→；L4 忽略缩进；每一级都必须只匹配一处；多处命中 → 拒绝并要求更多上下文空锚点、纯空白锚点 → 拒绝；审批与写入之间再次核对；先读文件、匹配并展示 diff批准后在写锁内复读，变了就拒绝；fuzzy 命中后重新读取：Python 缩进变化可能改变语义。；容错减少无效重试；唯一性与复读检查防止改错位置、覆盖并发修改。


#### Slide 13 - 并发调用需要声明安全边界
- **Audience move**: 理解问题触发机制，以及教程与当前实现的区别。
- **Relationships**: link: 问题连接接口、执行机制与验证；contrast: 教程示例和本项目实现。
- **Title**: 并发调用需要声明安全边界
- **Core message**: 以实际源码解释组件职责和设计取舍。
- **Content**: OmicsClaw 的调度示意：时间向右，块宽不代表耗时；读取样本表 A；读取元数据 B；→；独占写步骤；等前批结束，再执行；→；后续安全批次；结果按调用顺序回填；教程演示 Fork-Join；项目通过 concurrency_safe 声明控制并发。；未声明安全的调用独占；engine 支持并发数配置，registry 提供策略，审批等待暂停工具计时。；创建脚本后再运行仍有先后关系；模型的并行请求不能替代依赖与副作用分析。


#### Slide 14 - PromptAssembler：把规则按职责组装
- **Audience move**: 理解问题触发机制，以及教程与当前实现的区别。
- **Relationships**: link: 问题连接接口、执行机制与验证；contrast: 教程示例和本项目实现。
- **Title**: PromptAssembler：把规则按职责组装
- **Core message**: 以实际源码解释组件职责和设计取舍。
- **Content**: 有序的 Section；运行契约 OMICSCLAW.md安全规则、工具与计划指导沙箱说明、技能目录、环境长期记忆摘要；entry 决定顺序，source 按需要读取内容；render → 一个 system 消息；空段落跳过，保留加入顺序统计每段的 token 估计和历史、当前请求一起发给模型；教程使用 AGENTS.md 演示工作区规范；本项目分析 Agent 的契约是 OMICSCLAW.md。；取舍：可检查、可重组；动态块变化也会影响提示前缀缓存。；稳定工具顺序有助于复用前缀，但不能保证整个 system prompt 的缓存始终命中。


#### Slide 15 - skills：先看目录，再加载方法
- **Audience move**: 理解模型如何选择技能。
- **Relationships**: order: name/description 索引、use_skill、正文与目录、运行；contrast: 选择提示与确定执行。
- **Title**: skills：先看目录，再加载方法
- **Core message**: 延迟加载控制提示长度，描述同时写明适用与跳过条件。
- **Content**: 系统提示中的目录；name + description；→；use_skill；→；完整方法与目录；CLI / API / Dependencies；模型按 description 选择；trigger、tags 仅服务人工搜索。；SKILL.md 是单一事实源；说明何时加载、何时跳过重复名称或缺必要字段会被跳过；加载说明后才执行；通过 bash 运行脚本或 step runner当前入口没有 oc run 命令

#### Slide 16 - context：把有限窗口留给当前判断
- **Audience move**: 理解上下文为何需要渐进压缩。
- **Relationships**: order: 正常、卸载、局部总结、全面总结、紧急截断。
- **Title**: context：把有限窗口留给当前判断
- **Core message**: 显式预算驱动不同强度的上下文处理。
- **Content**: 可用预算 = 模型窗口 − 输出预留 − 工具定义 − 安全余量；正常；卸载长输出；局部总结；全面总结；紧急截断；保持历史；以文件引用替代；压缩较旧部分；保留最近消息；不再调模型；压缩仍要保留工具调用与结果配对，防止对话失去可解释性。；压力阈值是沿用的工程默认值，尚未按本项目真实流量校准。

#### Slide 17 - memory 与 planning 保存不同信息
- **Audience move**: 区分会话记忆、长期事实和任务状态。
- **Relationships**: contrast: 记忆回答知道什么，计划回答还需做什么；link: 两者进入后续模型请求。
- **Title**: memory 与 planning 保存不同信息
- **Core message**: 历史压缩后，知识与任务进度仍有独立来源。
- **Content**: memory：已经知道什么；SQLite 保存工作区会话长期事实经 FTS5 检索MEMORY.md 提供有界摘要；压缩前先提取长期事实；摘要通过闭包在后续轮次重新读取。；planning：接下来还要做什么；PlanBook 独立于聊天历史每次模型调用前重新注入恢复会话时还原任务状态；避免历史压缩后遗忘任务目标；通过 plan_write 更新进展。；计划里的“完成”，不等于模块已通过科学验证与用户验收。

#### Slide 18 - PlanInjector：在决策前重新呈现目标
- **Audience move**: 理解问题触发机制，以及教程与当前实现的区别。
- **Relationships**: link: 问题连接接口、执行机制与验证；contrast: 教程示例和本项目实现。
- **Title**: PlanInjector：在决策前重新呈现目标
- **Core message**: 以实际源码解释组件职责和设计取舍。
- **Content**: history；已有对话；→；compactor；先处理上下文压力；→；sent + augment → 模型；提醒只追加到本次请求副本；压缩之后再注入，避免提醒刚加入就被压掉；不写回 history，避免每轮累积副本。；没有计划，却持续只读；默认 8 轮后提醒调用 plan_write一次 exchange 最多触发一次；已有计划，需要防止遗忘；每次调用重新读取未完成项plan_write 更新后，下轮立即可见；当前是计划提醒机制；不能据此宣称具备检测所有重复调用和死循环的通用系统。


#### Slide 19 - 失败之后，谁负责恢复？
- **Audience move**: 理解问题触发机制，以及教程与当前实现的区别。
- **Relationships**: link: 问题连接接口、执行机制与验证；contrast: 教程示例和本项目实现。
- **Title**: 失败之后，谁负责恢复？
- **Core message**: 以实际源码解释组件职责和设计取舍。
- **Content**: 工具参数或输入错了；is_error 观察回到模型 → 重新读取、修正参数，再决定是否调用；模型请求出现暂时故障；网络错误、429 / 5xx → 有限预算与退避；其他 4xx 不盲目重试；总 deadline 已耗尽，或用户取消；结束请求或传播取消 → 不追加重试，不回滚已完成的工具副作用；教程演示统一恢复提示模板；当前项目采用工具反馈与分层重试，未实现同款模板仓库。


#### Slide 20 - entry 把组件接成可管理的会话
- **Audience move**: 理解组合根和运行生命周期。
- **Relationships**: parent: AgentApp组合依赖；order: 同会话串行；contrast: 跨会话并行。
- **Title**: entry 把组件接成可管理的会话
- **Core message**: 单一组装点与显式队列让入口共享行为。
- **Content**: build_app：统一接线；provider / registry / prompt / memory / permission / sandbox / subagents；同会话串行，跨会话并发；会话 A；exchange 1；→；排队 2；会话 B；exchange 1；每个 exchange 独立 Task，立即返回可管理 handle。；队列、事件和历史各有边界；队列有上限，可取消等待项事件缓冲有界，支持观察与续接成功交换才替换会话历史；失败的部分对话不保存，完成的文件操作仍存在。

#### Slide 21 - 三个入口共用后端契约
- **Audience move**: 理解CLI、桌面、聊天平台的职责。
- **Relationships**: link: 三入口汇聚同一AgentApp；contrast: 本仓库后端与App仓库UI。
- **Title**: 三个入口共用后端契约
- **Core message**: 界面转换输入与事件，执行政策归后端。
- **Content**: oc cli；oc desktop；oc channel；终端 REPL、审批与会话恢复；HTTP / SSE、审批与文件服务；七种平台，owner allowlist；AgentApp；本仓库负责后端；政策、执行、持久化与 HTTP 契约；已具备审批、中止端点；独立审查由当前回合显式开启。；OmicsClaw-App 负责界面；Electron / Next.js 与 SSH 隧道；本次未检查 App 源码，不宣称前端全流程验收。

#### Slide 22 - permission：策略决定，入口负责询问
- **Audience move**: 区分权限规则与审批界面。
- **Relationships**: order: 工具调用、规则/模式/危险模式判定、审批、执行或拒绝。
- **Title**: permission：策略决定，入口负责询问
- **Core message**: 权限决定在工具执行前收敛。
- **Content**: 工具调用；→；PermissionGate；模式 + 规则 + 危险命令模式；决定 allow / ask / deny；→；执行 / 拒绝；需要时发起审批；入口显示卡片并收回决定；一次允许 / 会话允许 / 记住匹配规则，分别对应不同的授权范围。；运行姿态；default / read-only / auto-approve；read-only 不允许 task 委派不受限的子任务。；仍保留的边界；危险调用与保护路径仍可要求审批；命令模式匹配不能穷尽所有绕过方式。

#### Slide 23 - sandbox 与 local-first 的边界
- **Audience move**: 能准确描述数据和进程的位置。
- **Relationships**: membership: bash 在容器，文件工具在宿主；link: provider 发送模型上下文。
- **Title**: sandbox 与 local-first 的边界
- **Core message**: 本地科学处理、命令隔离和模型通信是不同边界。
- **Content**: 宿主机 / 工作区；文件工具由 Workspace 限定路径；Docker / Podman：只接管 bash；默认无网络、代码只读挂载；工作区供分析读写，内部状态另有遮蔽与交换路径；模型通信边界；provider 发送上下文消息与工具结果可能进入远程模型；local-first 描述分析位置，不是绝对的数据不出机承诺。；取消对话不会回滚已完成的工具副作用。

#### Slide 24 - skillenv：补依赖时保留基础环境
- **Audience move**: 理解探测和安装为何分开。
- **Relationships**: order: use_skill探测、明确缺包、审批、overlay、指定解释器执行。
- **Title**: skillenv：补依赖时保留基础环境
- **Core message**: 探测默认开启，安装必须由部署启用并按权限决策。
- **Content**: use_skill；→；缺包说明；→；权限审批；→；overlay；默认 probe；基于 Dependencies；部署须启用 install；返回明确解释器路径；框架以 AST 读取依赖注册表的纯字面量，不导入科学技能模块。；overlay：增加当前任务所需包；基础环境：保持不变；仅安装白名单中请求的依赖固定 wheel，检查 pip 配置新增 pip check 问题则回滚；同一模块保持解释器一致；切换需记录原因。

#### Slide 25 - MCP 与 subagent 扩展两类能力
- **Audience move**: 区分工具集成和任务委派。
- **Relationships**: contrast: MCP 外部工具与 task 子运行；link: 都经过同一工具体系。
- **Title**: MCP 与 subagent 扩展两类能力
- **Core message**: 外部服务成为工具，委派成为有范围的第二次运行。
- **Content**: MCP：接入外部工具；.mcp.json 声明服务stdio 或 Streamable HTTP转换成普通 Tool 注册；engine 无须区分本地与外部工具；权限与工具定义仍走统一接口。；subagent：运行有范围的子任务；角色提示 + 缩小的工具集独立上下文，复用同一 engineentry 构造子运行并接收回复；用户请求后，module-reviewer 检查材料；其职责是审查，不替用户接受模块。；独立任务上下文能减少干扰，不能自动消除模型的共同盲点。

#### Slide 26 - hooks 与 observability 观察已有执行
- **Audience move**: 理解审计和性能观测如何接入。
- **Relationships**: order: permission 外侧、hook链内侧、tool；parent: interaction/turn/model/tool跨度。
- **Title**: hooks 与 observability 观察已有执行
- **Core message**: 复用已有事件与工具包装，核心循环不承担日志职责。
- **Content**: 工具执行链；permission：先决定能否执行；hooks：拦截与 JSONL 审计；tool：实际动作；运行跨度与度量；interaction；turn；llm_request / tool；LLM 耗时、输入 token、输出 token、工具次数、工具耗时、轮次数。消费现有 EngineEvent；OTEL 可选。默认不采集对话和工具正文载荷。；被权限拒绝的调用不会进入内侧 hook。

#### Slide 27 - 评估要检查动作与副作用
- **Audience move**: 理解问题触发机制，以及教程与当前实现的区别。
- **Relationships**: link: 问题连接接口、执行机制与验证；contrast: 教程示例和本项目实现。
- **Title**: 评估要检查动作与副作用
- **Core message**: 以实际源码解释组件职责和设计取舍。
- **Content**: Case → build_app → SessionRegistry → 轨迹与文件差异 → 断言；走生产组装路径；临时工作区记录执行前后状态，而不只检查最终回答。；ScriptedProvider：机制是否守约；预设工具调用与回复检查 ToolCalled / ToolArgs检查审批、越界写入、调用次数；真实模型：是否选对技能；26 个路由种子，手动启用重复 trial，记录成功率与失败服务故障与路由质量分开报告；回归与路由正确，仍需检验生物学结论和统计假设。；实用指标：是否完成、是否越界、耗时与 token；没有本次实测，就不画性能提升曲线。


#### Slide 28 - 框架与科学代码通过文件契约协作
- **Audience move**: 理解双向禁止导入的价值。
- **Relationships**: contrast: omicsclaw 框架与 skills 科学代码；link: SKILL.md、依赖字面量、结果文件。
- **Title**: 框架与科学代码通过文件契约协作
- **Core message**: 科学依赖不会随框架导入，技能也能独立执行。
- **Content**: omicsclaw/；模型、权限、会话工具与运行调度；不导入 skills 科学代码；SKILL.md；依赖字面量；结果与文件契约；skills/；方法函数与脚本_sdk + domain/_lib；不导入 omicsclaw 框架；科学依赖按任务加载；同一技能可以脱离 Agent 单独执行。；代价：跨边界数据必须明确序列化，接口变化需要同步核对文件契约。

#### Slide 29 - 同一算法，函数库与 CLI 各负其责
- **Audience move**: 理解从脚本迁到 API 的目的。
- **Relationships**: link: _api.py 同时服务 step 与薄 CLI；contrast: 数据返回与文件报告。
- **Title**: 同一算法，函数库与 CLI 各负其责
- **Core message**: 分离计算和文件副作用，使步骤组合与回放可行。
- **Content**: _api.py；返回数据对象或 Figure；分析 step / load_skill；组合方法、验证结果、显式写输出；薄 CLI；解析参数、读写文件、生成报告；已迁移：83 个函数库；网络 fetch_* 与计算分开；5 个技能保留 CLI；DataFrame 诊断在 attrs，写为 CSV 后不会保留。；XCMS 仅支持合成 demo；真实 raw-MS 输入会被拒绝。

#### Slide 30 - 例如：QC、聚类、注释。
- **Audience move**: 建立 module/step/result 的实体模型。
- **Relationships**: parent: 课题包含策略、数据、分析模块和结果；link: 步骤对应输出。
- **Title**: 例如：QC、聚类、注释。
- **Core message**: 每个能独立汇报的分析阶段有可定位的代码与证据。
- **Content**: Project；docs/analysis_strategy/STRATEGY.md；问题、数据、跨模块计划与决策；analysis/01_qc/；README.md + Python / R 步骤；方法、输入、参数选择与验证代码；results/01_qc/；图表、中间文件、日志与 REPORT；notebooks / provenance / reviews / baseline；模块是分析交付单元；技能是可复用的方法单元。

#### Slide 31 - run、status、replay 分别回答不同问题
- **Audience move**: 理解过期检测与干净重放的区别。
- **Relationships**: order: 上游文件变更使下游 stale，再 run 或 replay；contrast: 增量执行与全量重放。
- **Title**: run、status、replay 分别回答不同问题
- **Core message**: 哈希发现依赖变化，新进程重放检验执行顺序。
- **Content**: 上游文件变化 → 输入哈希变化 → 下游步骤 stale；步骤代码变化也会触发过期；判断依据保存在 manifest 与运行台账。；status；哪里过期，为什么？；只观察，不执行分析；run；执行当前过期步骤；适合迭代修改；replay；全部步骤重新执行；Python validate 最后运行；显式 IO 才进入台账；文件回放成功仍需检查统计假设。；read_input / write_output 为依赖追踪提供证据，不是任意脚本的全量审计。

#### Slide 32 - 先交付验证结果，审查与接受由用户触发
- **Audience move**: 理解最终交付的控制点。
- **Relationships**: order: replay与报告先交付，用户请求后才审查，明确接受后冻结；contrast: 审查授权与修改、接受授权。
- **Title**: 先交付验证结果，审查与接受由用户触发
- **Core message**: 验证、解释、审查与接受是独立环节。
- **Content**: replay + REPORT；验证后立即展示结果；→；用户请求 review；检查材料，报告 findings；→；用户明确 accept；接受并冻结模块；独立审查默认关闭；Desktop 按钮只为新开启的那一轮授权，下一普通轮恢复关闭。；审查绑定具体材料；原文 + receipt 绑定 replay 与哈希材料改动后，旧审查不再适用；请求审查不授权自动修复、重跑或接受。；用户决定是否接受；未审查结果不能称已批准改已接受模块先 revise，保留 baseline；跳过审查接受须用户明确授权 --skip-review。

#### Slide 33 - Python 与 R 共享文件证据
- **Audience move**: 理解混合语言分析如何复现。
- **Relationships**: link: Python 输出文件供 R 输入，R 输出供最终 Python validate。
- **Title**: Python 与 R 共享文件证据
- **Core message**: 语言之间用显式文件交换数据。
- **Content**: Python step；每步一个新 kernel；代码不依赖前一步内存变量；→；显式文件；MTX / CSV 等；输入、输出和哈希；→；R step；一个 Rscript 进程；同一步内按 cell 顺序运行；R 输出仍由最后的 Python validate 步骤检查，形成同一条模块证据链。；记录运行环境；解释器、R 版本、已加载包版本文件哈希与执行日志；保留语言边界；拼接 notebook 以 Markdown 保留 R重放按原始步骤语言执行

#### Slide 34 - 部署把科学栈与轻量框架分开
- **Audience move**: 理解两种安装方式覆盖范围。
- **Relationships**: contrast: conda完整科学环境与pip框架环境；membership: overlay是增量补充。
- **Title**: 部署把科学栈与轻量框架分开
- **Core message**: 能启动聊天与能运行所有分析有不同依赖条件。
- **Content**: conda：完整科学环境；bash 0_setup_env.sh；Python 科学栈 + R + 外部 CLI包含 Desktop 后端所需依赖；pip：先启动框架；pip install -e .；终端聊天与模型调用科学方法按环境另行准备；oc cli　　oc desktop　　oc channel；Python 依赖以 pyproject.toml 为准；R、CLI 与科学环境看 environment.yml。；聊天能启动，不代表可选算法后端与所需数据库已就绪。

#### Slide 35 - 四模块案例：检查应落在实际输出上
- **Audience move**: 把抽象架构落到已有实际验收。
- **Relationships**: order: QC过滤、预处理聚类、marker富集、R交叉核对；link: 上游变更使下游过期。
- **Title**: 四模块案例：检查应落在实际输出上
- **Core message**: 0074 的干净课题通过六项核心检查，仍保留额外偏差记录。
- **Content**: 01 QC 与过滤；→；02 预处理聚类；→；03 marker 富集；→；04 R 核对；前三模块使用函数库；第四模块用 Seurat 对 B 细胞结果作交叉核对。；验收记录支持什么；四模块均 accepted / frozenR 独立运行的 CSV 与原件一致上游改变后，下游 R 显示过期；审查实际改变了什么；补充 counts 逐元素验证纠正基因与效应值的错误配对；干净课题通过六项核心检查，额外 prompt 偏差仍保留在记录中。

#### Slide 36 - 可靠性修复落在具体失败边界
- **Audience move**: 理解近期改进的触发问题和范围。
- **Relationships**: contrast: provider总deadline与review receipt处理不同失效。
- **Title**: 可靠性修复落在具体失败边界
- **Core message**: 请求有总时限，审查绑定所看的那份材料。
- **Content**: 模型请求：统一总 deadline；覆盖 SDK 重试、首响应和流式读取预算耗尽后 engine 不再次重试流清理有独立且有界的时间额度；异步 deadline 无法打断阻塞事件循环的同步代码。；审查材料：保存原文与绑定；审查前后比较同一组文件哈希框架原样保存 reviewer 回复accept 检查 receipt 与 replay；receipt 不是签名；不能抵抗同时改写材料与校验记录的工作区写入者。；尚未实现中断交换的自动恢复，也不回滚已完成的工具副作用。

#### Slide 37 - 四层验证回答四种问题
- **Audience move**: 不再用测试总数代替科学有效性。
- **Relationships**: membership: 契约、行为、真实运行、科学有效性四类证据；order: 验证范围逐层扩展。
- **Title**: 四层验证回答四种问题
- **Core message**: 边界测试、回归、实跑和科学评估不能相互替代。
- **Content**: 契约边界；类型、导入方向、公开 API 是否守约？；行为回归；脚本化 provider 与旧新 CLI/API 是否一致？；真实运行；真实模型会选吗？Python/R 步骤能回放吗？；科学有效性；真实数据结论、统计假设与泛化是否成立？；已有记录：远端 CI 7,175 项；干净 checkout 7,206 项与 41 个示例回放。范围不同，不相加。

#### Slide 38 - 七领域迁移已形成 83 个函数库
- **Audience move**: 清楚哪些已经完成、哪些尚在进行。
- **Relationships**: contrast: 已交付、当前起点、后续批次。
- **Title**: 七领域迁移已形成 83 个函数库
- **Core message**: 当前83个函数库和5个CLI-only已覆盖七域，仍须明确后端与真实输入边界。
- **Content**: 83 + 5；函数库　　 CLI-only；合计 88 个技能，0 skipped。函数库与 CLI 都有明确的输入边界。；函数库分域数量；单细胞 27　　空间 16Bulk RNA-seq 14　　基因组 10蛋白组 8　　代谢组 7　　文献 1；0074、0075、0076 保留交付与边界记录。；CLI-only：sc-count、sc-fastq-qc、sc-velocity-prep、spatial-raw-processing、；metabolomics-xcms-preprocessing。XCMS 仅支持合成 demo，拒绝真实 raw-MS 输入。；真实 FASTQ、原始 MS 搜索与真实实验数据仍须按方法核验，不能用接口计数代替。

#### Slide 39 - 下一步把工程证据接到科研证据
- **Audience move**: 看见可讨论的验证优先级。
- **Relationships**: order: 补齐迁移、真实环境、科学有效性与消融；contrast: 已实现机制与待验证收益。
- **Title**: 下一步把工程证据接到科研证据
- **Core message**: 重放与审查的收益需要独立实验测量。
- **Content**: 补齐可执行范围；在真实课题中检验现有函数库逐方法核对可选后端与数据库补充真实数据与容器隔离验证；已有迁移保留 CLI 基线、API 行为与示例；先查依赖和输入条件，再列验收范围。；测量对科学结果的影响；真实数据上的稳定性与统计有效性重放门与持久 kernel 的对照审查能发现哪些错误、代价多大；0072 涉及伪重复与 double dipping；0073 规划 replay 与审查相关评估。这些是研究计划，不能当成实验结果。；组会可决定：优先用哪个真实课题，检验哪些可量化收益。

#### Slide 40 - 下一阶段优先验证什么？
- **Audience move**: 能带着系统理解提出具体评估选择。
- **Relationships**: link: 组件边界、文件证据与科学有效性组成设计主张。
- **Title**: 下一阶段优先验证什么？
- **Core message**: 系统已经能保留执行证据，下一阶段需测量它对研究结果的帮助。
- **Content**: 组件边界让框架可以替换，；函数库让方法可以组合，；模块证据让分析交付可以检查。；选一个真实课题，测量重放和审查对结果可靠性的帮助。；可讨论：空间数据优先级 · 真实后端覆盖 · 重现与统计有效性的评估指标
- **Closing impact**: 留下“优先验证哪个真实课题”的讨论问题，并保留设计主张。

#### Slide 41 - 组件定位速查
- **Audience move**: 可在问答时定位关键目录。
- **Relationships**: membership: 决策执行、任务状态、部署扩展、科学代码四组组件。
- **Title**: 组件定位速查
- **Core message**: 组件目录映射清晰的职责边界。
- **Content**: 决策与执行；schema：交换契约　provider：模型适配engine：循环　tools：注册与调用permission：执行决定　sandbox：命令环境；知识与任务状态；context：提示与预算　skills：说明加载memory：会话与长期事实planning：目标与进展；组装与扩展；entry / launch：应用与入口mcp / subagent：服务与委派hooks / observability / evals：审计、观测、评估；方法与环境；skillenv：依赖探测与 overlayskills/_sdk：机械辅助与 step runnerdomain/_lib：域内复用　_api.py：方法函数；common、attachments 仍在；remote、routing、surfaces、ensemble、runtime 已不在当前代码树。

#### Slide 42 - 七领域的代表任务
- **Audience move**: 能说明技能范围而不暗示统一质量。
- **Relationships**: membership: 每领域含代表任务。
- **Title**: 七领域的代表任务
- **Core message**: 技能目录覆盖分析环节，方法可用性依赖环境。
- **Content**: 单细胞 · 30；QC、双细胞、聚类、marker、注释批次整合、轨迹、velocity、扰动与 ATAC；空间 · 17；预处理、空间分区、基因空间统计注释、解卷积、配准、通信与轨迹；Bulk RNA-seq · 14；差异、富集、共表达、去卷积与生存分析；基因组 · 10；QC、比对结果、变异结果、CNV 与组装统计；方法实际可用性依赖包、外部工具和数据库。；蛋白组 · 8 / 代谢组 · 8；DIA/DDA、PTM、网络与生物标志物表格处理、标准化、注释与通路；文献 · 1；PDF、DOI、PubMed、GEO 解析与数据线索

#### Slide 43 - 可定位的模块命令
- **Audience move**: 会后可以按真实入口查询使用方式。
- **Relationships**: order: new/run/status/replay/accept/revise。
- **Title**: 可定位的模块命令
- **Core message**: step runner 是模块执行入口，oc 选择交互surface。
- **Content**: python skills/_sdk/notebook/run.py <command>；new qc；run analysis/01_qc；status；replay analysis/01_qc；accept analysis/01_qc --review <file>；创建模块、目录与策略模板；执行过期步骤；显示过期状态与原因；全部重跑，validate 最后执行；用户确认后接受并冻结；修改已接受模块先 revise；执行器须在前台运行；使用完整路径或实际配置的 step runner 路径。；无审查接受须用户明确授权 --skip-review；独立审查默认关闭。

#### Slide 44 - 教程二十讲与 OmicsClaw 对应关系
- **Audience move**: 理解问题触发机制，以及教程与当前实现的区别。
- **Relationships**: link: 问题连接接口、执行机制与验证；contrast: 教程示例和本项目实现。
- **Title**: 教程二十讲与 OmicsClaw 对应关系
- **Core message**: 以实际源码解释组件职责和设计取舍。
- **Content**: 01–05：先搭建可替换的循环；01 架构分层 → entry 与单向组件边界02 主循环 → schema + engine03 Thinking → 区分原生推理与额外阶段04 模型适配 → provider05 工具注册 → ToolRegistry；06–10：与环境交互；06 基础工具 → read / write / edit / bash07 模糊编辑 → 唯一匹配与复读保护08 并发调用 → 安全批次与独占边界09 飞书与输出 → 三入口 + EngineEvent10 提示与技能 → PromptAssembler + use_skill；11–15：管理上下文与目标；11 会话 → SessionRegistry + SQLite12 压缩 → token 预算与渐进降级13 记忆与待办 → memory + PlanBook14 恢复 → 工具观察与分层重试15 提醒 → PlanInjector 的当次注入；16–20：约束与验证执行；16 审批 → PermissionGate + entry 审批通道17 委派 → task + 独立子上下文18 成本 → Usage 与六类运行度量19 追踪 → interaction / turn / llm / tool20 评估 → scripted case 与真实路由评估


#### Slide 45 - 来源与验证口径
- **Audience move**: 能审计汇报的事实依据。
- **Relationships**: contrast: 本次实测与已有交付记录；membership: 代码、运行契约和交付文档。
- **Title**: 来源与验证口径
- **Core message**: 每项数量和进展都有代码快照或明确交付记录。
- **Content**: 本次实际核对；代码快照 90a3bec388 个技能，0 skipped七领域 83 个函数库、5 个 CLI-only；逐层接口、目录和 Desktop 审批端点；全部二十讲已与当前实现逐项对照。；引用已有交付记录；远端 CI 单元检查：7,175 passed干净 checkout：7,206 项单元检查41 个示例执行与 fresh replay；命令选择、环境与时间各不相同；本次未重跑全套测试或科学实验。；讲解参考：Tony Bai《从0开始构建 Agent Harness》01–20；项目依据：当前源码与 0074–0076。；来源与文件哈希：repository_snapshot.json、tutorial_manifest.json；逐页解释见演讲者备注。；制作：ppt-master 6.6.0（hugohe3/ppt-master）；原生文字与图形，可在 PowerPoint 继续编辑。

## X. Speaker Notes Requirements

- **Generation**: enabled
- **Filename**: match each SVG filename under `notes/`
- **Content**: 每页解释机制、设计动机与限制，给出可定位源码。数字注明本次核对或历史记录。案例只引用0074已存记录。
- **Total duration**: 正文约40分钟，备份页按问答使用。
- **Notes style**: 中文口语讲解，保留技术术语，避免夸大。
- **Presentation purpose**: 解释当前组件与设计取舍，并讨论后续验证重点。
