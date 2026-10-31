<!-- ppt-master-schema: design-spec/v1 -->
# OmicsClaw Group Meeting - Design Spec

## I. Project Information

| Item | Value |
| --- | --- |
| Project Name | OmicsClaw_GroupMeeting |
| Canvas Format | PPT 16:9, 1280 × 720 |
| Page Count | 36 |
| Primary Language | zh-CN |
| Target Audience | 课题组 PI、研究生与生信研究人员 |
| Communication Intent | 汇报当前架构，解释组件职责、协作与取舍，讨论验证重点 |
| Desired Audience Outcome | 能说明分析请求如何变为可回放、可审核的模块结果，区分当前实现与验证范围 |
| Core Message / Ask / Action | 单向分层框架、独立技能和模块证据链连接自然语言请求与科研输出 |
| Delivery Context | 现场中文组会；正文 32 页约 35 分钟，4 页技术备份 |
| Artifact Afterlife | 附代码来源与讲稿的可编辑组内参考 |
| Reading Mode | balanced |
| Content Strategy | 依据本地 43e9c034 代码与交付记录，不将历史状态或计划当作当前成果 |
| Design Style | 清晰的技术分层图与大留白；按教学顺序拆解组件 |
| AI Image Acquisition Path | not applicable |
| Generation Mode | continuous |
| Spec Refinement | disabled |
| Speaker Notes | enabled; user delegated production decisions and requested notes through accepted proposal |
| Custom Animations | disabled; delegated decision |
| Narration Audio | disabled; delegated decision |
| Created Date | 2026-10-07 |

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

用户已在聊天中授权所有后续设计决定，使用 free_design，不创建用户确认回执。

1. 组件逐层讲解：instructional 主线，swiss-minimal 栅格；微软雅黑与 Arial 的同一字体系统，白底深蓝与青绿。先建立地图再逐层放大，适合混合背景听众，选用此方案。
2. 进展简报：briefing 主线，editorial 排版；宋体标题配微软雅黑正文，米白底、墨黑与暗红。按能力、证据和缺口平行展开，便于会后检索，但现场理解组件依赖需要更多解释。
3. 案例叙事：narrative 主线，blueprint 线条；黑体标题配微软雅黑正文，深蓝底、白字与青色。跟随一次失败、修复和验收展开，便于讲故事，但难以完整覆盖每个基础组件。

三方案均可用本地文档和原生图形，无需图片生成。最终制作使用第一种方案；36 页，正文 26 px，讲稿开启，逐对象动画和旁白关闭。

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

#### Slide 01 - 从分析请求到可审核结果
- **Audience move**: 从项目名字进入本次架构主题。
- **Relationships**: order: 请求、执行、证据；membership: 88 技能与 7 个领域。
- **Title**: OmicsClaw：从分析请求到可审核结果
- **Core message**: 框架和技能共同服务可追溯的科研分析。
- **Content**: 项目定位；88 技能、7 领域；本地代码快照与日期。
- **Cover impact**: 以“请求 → 执行 → 证据”为主视觉，原生文字作为标题。

#### Slide 02 - 多组学分析同时面对两类复杂性
- **Audience move**: 从功能集合转向要解决的科研执行问题。
- **Relationships**: contrast: 方法和环境的复杂性，与过程和证据的复杂性。
- **Title**: 多组学分析同时面对两类复杂性
- **Core message**: 方法调用和过程追溯都需要明确边界。
- **Content**: 多方法与 Python/R/CLI 环境；参数变化与残留状态；文件、报告和结论的一致性；平台目标。

#### Slide 03 - 88 个技能覆盖七个领域
- **Audience move**: 掌握当前功能范围和精确规模。
- **Relationships**: membership: 七域属于技能目录；单细胞与空间含已迁移函数库。
- **Title**: 88 个技能覆盖七个领域
- **Core message**: 技能数量与函数库迁移程度分别统计。
- **Content**: 单细胞30、空间17、bulk14、基因组10、蛋白8、代谢8、文献1；0 skipped；单细胞27与空间1函数库。

#### Slide 04 - 系统地图：交互、决策、执行与证据
- **Audience move**: 形成后续各组件的整体定位。
- **Relationships**: link: 入口连接 Agent 框架与工具，工具调用技能与步骤；membership: 上下文和策略支持执行。
- **Title**: 系统地图：交互、决策、执行与证据
- **Core message**: entry 集中组装，科学代码与框架通过文件契约协作。
- **Content**: 三入口；entry/engine/provider/schema；context/memory/planning；tools/permission/sandbox/mcp；skills/_sdk/modules；功能地图非严格导入图。

#### Slide 05 - 一次分析请求怎样流过系统
- **Audience move**: 能按先后顺序复述运行链条。
- **Relationships**: order: 接入、组装、决策、审批执行、观察、交付；link: 工具结果回到模型。
- **Title**: 一次分析请求怎样流过系统
- **Core message**: 对话循环产生动作，模块机制保留分析证据。
- **Content**: 用户请求；会话；提示词与技能目录；模型工具调用；审批与 bash；use_skill/step runner；结果返回与模块交付。

#### Slide 06 - schema：先统一组件之间的语言
- **Audience move**: 理解公共叶子层为何独立。
- **Relationships**: link: provider、engine、tools 交换共同 schema；membership: 消息与工具结果。
- **Title**: schema：先统一组件之间的语言
- **Core message**: 供应商中立的数据契约降低跨层耦合。
- **Content**: Message/ToolCall/ToolResult/Usage/StreamChunk；标准库；参数保留 JSON 文本；reasoning_content 与工具角色。

#### Slide 07 - provider：把模型差异收在适配层
- **Audience move**: 理解更换模型所影响的边界。
- **Relationships**: link: 统一接口对应 OpenAI-compatible 与 Anthropic 两类适配器。
- **Title**: provider：把模型差异收在适配层
- **Core message**: 模型配置与 wire 协议不进入核心循环。
- **Content**: generate/generate_stream/bind；配置在实例；流式归一化；厂商参数与错误；统一接口不能消除能力差异。

#### Slide 08 - engine：一个内核维持 ReAct 循环
- **Audience move**: 理解模型与工具如何交替。
- **Relationships**: order: Message、模型、ToolCall、ToolResult、Message 形成反馈；contrast: 返回最终结果与继续调用。
- **Title**: engine：一个内核维持 ReAct 循环
- **Core message**: run 与 run_stream 共用循环逻辑。
- **Content**: 观察结果进入下一轮；预算和终止；异常抛出、取消传播；不负责提示、存储和具体工具。

#### Slide 09 - tools：稳定注册，按声明调度
- **Audience move**: 理解注册、并发与超时的分工。
- **Relationships**: order: register、available_tools、execute；contrast: 安全并发与独占写入。
- **Title**: tools：稳定注册，按声明调度
- **Core message**: 可并发动作并行，未声明安全的动作独占执行。
- **Content**: 基础文件/bash/web 工具；稳定定义顺序；写屏障；结果按调用顺序；审批等待暂停工具计时。

#### Slide 10 - context：把有限窗口留给当前判断
- **Audience move**: 理解上下文为何需要渐进压缩。
- **Relationships**: order: 正常、卸载、局部总结、全面总结、紧急截断。
- **Title**: context：把有限窗口留给当前判断
- **Core message**: 显式预算驱动不同强度的上下文处理。
- **Content**: 窗口扣除输出/工具/安全余量；五档压力；保留最近消息与工具配对；阈值是工程默认未校准。

#### Slide 11 - memory 与 planning 保存不同信息
- **Audience move**: 区分会话记忆、长期事实和任务状态。
- **Relationships**: contrast: 记忆回答知道什么，计划回答还需做什么；link: 两者进入后续模型请求。
- **Title**: memory 与 planning 保存不同信息
- **Core message**: 历史压缩后，知识与任务进度仍有独立来源。
- **Content**: SQLite/FTS5/MEMORY.md；压缩前提取；PlanBook 与逐轮注入；计划完成不等于科学验收。

#### Slide 12 - skills：先看目录，再加载方法
- **Audience move**: 理解模型如何选择技能。
- **Relationships**: order: name/description 索引、use_skill、正文与目录、运行；contrast: 选择提示与确定执行。
- **Title**: skills：先看目录，再加载方法
- **Core message**: 延迟加载控制提示长度，描述同时写明适用与跳过条件。
- **Content**: 四个前置字段；SKILL.md 唯一事实源；trigger 不自动触发；重复和缺字段跳过；无 oc run。

#### Slide 13 - 框架与科学代码通过文件契约协作
- **Audience move**: 理解双向禁止导入的价值。
- **Relationships**: contrast: omicsclaw 框架与 skills 科学代码；link: SKILL.md、依赖字面量、结果文件。
- **Title**: 框架与科学代码通过文件契约协作
- **Core message**: 科学依赖不会随框架导入，技能也能独立执行。
- **Content**: SDK 机械辅助；domain/_lib 域内复用；AST 读纯字面量；边界测试；接口受限但部署解耦。

#### Slide 14 - 同一算法，函数库与 CLI 各负其责
- **Audience move**: 理解从脚本迁到 API 的目的。
- **Relationships**: link: _api.py 同时服务 step 与薄 CLI；contrast: 数据返回与文件报告。
- **Title**: 同一算法，函数库与 CLI 各负其责
- **Core message**: 分离计算和文件副作用，使步骤组合与回放可行。
- **Content**: 27 单细胞+1空间 API；CLI_ONLY 三项；数据/Figure 返回；旧新 CLI/API parity；不等于所有后端验证。

#### Slide 15 - 分析模块把代码与输出放在明确位置
- **Audience move**: 建立 module/step/result 的实体模型。
- **Relationships**: parent: 课题包含策略、数据、分析模块和结果；link: 步骤对应输出。
- **Title**: 分析模块把代码与输出放在明确位置
- **Core message**: 每个能独立汇报的分析阶段有可定位的代码与证据。
- **Content**: STRATEGY.md；analysis/NN_slug/README、步骤；results/NN_slug/图表中间文件与provenance；模块例子。

#### Slide 16 - run、status、replay 分别回答不同问题
- **Audience move**: 理解过期检测与干净重放的区别。
- **Relationships**: order: 上游文件变更使下游 stale，再 run 或 replay；contrast: 增量执行与全量重放。
- **Title**: run、status、replay 分别回答不同问题
- **Core message**: 哈希发现依赖变化，新进程重放检验执行顺序。
- **Content**: step/input 哈希；run stale；status原因；replay全部且validate最后；显式IO才能记录；重放不证明统计正确。

#### Slide 17 - 审查之后，由人接受并冻结模块
- **Audience move**: 理解最终交付的控制点。
- **Relationships**: order: replay、REPORT、review、accept；link: revise 回到可修改状态。
- **Title**: 审查之后，由人接受并冻结模块
- **Core message**: 验证、解释、审查与接受是独立环节。
- **Content**: review_brief；reviewer 原文与receipt；APPROVE/REVISE；用户accept；frozen与baseline；哈希非签名。

#### Slide 18 - Python 与 R 共享文件证据
- **Audience move**: 理解混合语言分析如何复现。
- **Relationships**: link: Python 输出文件供 R 输入，R 输出供最终 Python validate。
- **Title**: Python 与 R 共享文件证据
- **Core message**: 语言之间用显式文件交换数据。
- **Content**: Python 每步新 kernel；R一步一个Rscript；输入输出哈希与R包版本；混合notebook中R以markdown保存；不共享隐式变量。

#### Slide 19 - permission：策略决定，入口负责询问
- **Audience move**: 区分权限规则与审批界面。
- **Relationships**: order: 工具调用、规则/模式/危险模式判定、审批、执行或拒绝。
- **Title**: permission：策略决定，入口负责询问
- **Core message**: 权限决定在工具执行前收敛。
- **Content**: default/read-only/auto；once/session/always；危险与保护路径仍问；规则匹配的能力边界；subagent不是read-only。

#### Slide 20 - sandbox 与 local-first 的边界
- **Audience move**: 能准确描述数据和进程的位置。
- **Relationships**: membership: bash 在容器，文件工具在宿主；link: provider 发送模型上下文。
- **Title**: sandbox 与 local-first 的边界
- **Core message**: 本地科学处理、命令隔离和模型通信是不同边界。
- **Content**: Docker/Podman、默认无网；只读代码与workspace；文件Workspace；云模型消息可离机；已完成副作用不回滚。

#### Slide 21 - skillenv：补依赖时保留基础环境
- **Audience move**: 理解探测和安装为何分开。
- **Relationships**: order: use_skill探测、明确缺包、审批、overlay、指定解释器执行。
- **Title**: skillenv：补依赖时保留基础环境
- **Core message**: 探测默认开启，安装必须由部署启用并按权限决策。
- **Content**: Dependencies白名单；字面量注册表；system-site-packages overlay；锁定wheels；pip check失败回滚；模块保持解释器。

#### Slide 22 - MCP 与 subagent 扩展两类能力
- **Audience move**: 区分工具集成和任务委派。
- **Relationships**: contrast: MCP 外部工具与 task 子运行；link: 都经过同一工具体系。
- **Title**: MCP 与 subagent 扩展两类能力
- **Core message**: 外部服务成为工具，委派成为有范围的第二次运行。
- **Content**: stdio/HTTP；普通Tool；子角色、缩小工具集、独立上下文；entry构造；审核任务与同模型盲点。

#### Slide 23 - hooks 与 observability 观察已有执行
- **Audience move**: 理解审计和性能观测如何接入。
- **Relationships**: order: permission 外侧、hook链内侧、tool；parent: interaction/turn/model/tool跨度。
- **Title**: hooks 与 observability 观察已有执行
- **Core message**: 复用已有事件与工具包装，核心循环不承担日志职责。
- **Content**: JSONL审计；六类度量；可选OTEL；默认不采集正文；被权限拒绝的调用不进入hook。

#### Slide 24 - entry 把组件接成可管理的会话
- **Audience move**: 理解组合根和运行生命周期。
- **Relationships**: parent: AgentApp组合依赖；order: 同会话串行；contrast: 跨会话并行。
- **Title**: entry 把组件接成可管理的会话
- **Core message**: 单一组装点与显式队列让入口共享行为。
- **Content**: build_app；每exchange独立Task；queue handle；bounded event stream；取消与保存；失败部分对话不持久化。

#### Slide 25 - 三个入口共用后端契约
- **Audience move**: 理解CLI、桌面、聊天平台的职责。
- **Relationships**: link: 三入口汇聚同一AgentApp；contrast: 本仓库后端与App仓库UI。
- **Title**: 三个入口共用后端契约
- **Core message**: 界面转换输入与事件，执行政策归后端。
- **Content**: CLI/desktop/channel；七平台owner allowlist；审批和abort已存在；SSE与文件；SSH模式；未审查App源码。

#### Slide 26 - 部署把科学栈与轻量框架分开
- **Audience move**: 理解两种安装方式覆盖范围。
- **Relationships**: contrast: conda完整科学环境与pip框架环境；membership: overlay是增量补充。
- **Title**: 部署把科学栈与轻量框架分开
- **Core message**: 能启动聊天与能运行所有分析有不同依赖条件。
- **Content**: Python3.11+；environment.yml、pyproject.toml、R/CLI；三入口命令；可选后端与数据库；不改基础环境。

#### Slide 27 - 四模块实跑串起分析与验收
- **Audience move**: 把抽象架构落到已有实际验收。
- **Relationships**: order: QC过滤、预处理聚类、marker富集、R交叉核对；link: 上游变更使下游过期。
- **Title**: 四模块实跑串起分析与验收
- **Core message**: 0074 的干净课题通过六项核心检查，仍保留额外偏差记录。
- **Content**: 四模块accepted/frozen；前三API、第四R；审查要求补counts和基因效应证据；R独立一致；本次未重跑。

#### Slide 28 - 可靠性修复落在具体失败边界
- **Audience move**: 理解近期改进的触发问题和范围。
- **Relationships**: contrast: provider总deadline与review receipt处理不同失效。
- **Title**: 可靠性修复落在具体失败边界
- **Core message**: 请求有总时限，审查绑定所看的那份材料。
- **Content**: SDK重试首响应流式deadline；不engine重试；清理有界；review前后哈希；无自动恢复/副作用回滚/签名。

#### Slide 29 - 四层验证回答四种问题
- **Audience move**: 不再用测试总数代替科学有效性。
- **Relationships**: membership: 契约、行为、真实运行、科学有效性四类证据；order: 验证范围逐层扩展。
- **Title**: 四层验证回答四种问题
- **Core message**: 边界测试、回归、实跑和科学评估不能相互替代。
- **Content**: schema/导入边界；scripted provider与parity；live routing与混合步骤；真实数据统计评估；历史7024/1242来源口径。

#### Slide 30 - 迁移进展按已交付范围汇报
- **Audience move**: 清楚哪些已经完成、哪些尚在进行。
- **Relationships**: contrast: 已交付、当前起点、后续批次。
- **Title**: 迁移进展按已交付范围汇报
- **Core message**: 单细胞函数化完成分类，空间从S1开始。
- **Content**: 开工快照为27个单细胞API、3个CLI和1个空间API；空间S1已验收，后续16个技能正在实施与验收，S6保留CLI；制作期间状态另注，不并入已交付计数。

#### Slide 31 - 下一步把工程证据接到科研证据
- **Audience move**: 看见可讨论的验证优先级。
- **Relationships**: order: 补齐迁移、真实环境、科学有效性与消融；contrast: 已实现机制与待验证收益。
- **Title**: 下一步把工程证据接到科研证据
- **Core message**: 重放与审查的收益需要独立实验测量。
- **Content**: 空间后续；后端数据库和容器CI；真实数据；0072/0073属计划；伪重复double dipping、replay消融；建议非已排期承诺。

#### Slide 32 - 组会讨论：下一阶段优先验证什么
- **Audience move**: 能带着系统理解提出具体评估选择。
- **Relationships**: link: 组件边界、文件证据与科学有效性组成设计主张。
- **Title**: 组会讨论：下一阶段优先验证什么
- **Core message**: 系统已经能保留执行证据，下一阶段需测量它对研究结果的帮助。
- **Content**: 可替换组件；可组合方法；可检查交付；讨论真实空间数据、迁移优先级和重放/审查评估。
- **Closing impact**: 留下“优先验证哪个真实课题”的讨论问题，并保留设计主张。

#### Slide 33 - 备份：组件定位速查
- **Audience move**: 可在问答时定位关键目录。
- **Relationships**: membership: 决策执行、任务状态、部署扩展、科学代码四组组件。
- **Title**: 备份：组件定位速查
- **Core message**: 组件目录映射清晰的职责边界。
- **Content**: 20组件列表；common/attachments仍在；remote/routing/surfaces/ensemble/runtime已不在当前树。

#### Slide 34 - 备份：七领域的代表任务
- **Audience move**: 能说明技能范围而不暗示统一质量。
- **Relationships**: membership: 每领域含代表任务。
- **Title**: 备份：七领域的代表任务
- **Core message**: 技能目录覆盖分析环节，方法可用性依赖环境。
- **Content**: 单细胞QC聚类marker轨迹；空间预处理分区解卷积；bulk DE；基因变异；蛋白DIA/PTM；代谢峰和通路；文献解析。

#### Slide 35 - 备份：可定位的模块命令
- **Audience move**: 会后可以按真实入口查询使用方式。
- **Relationships**: order: new/run/status/replay/accept/revise。
- **Title**: 备份：可定位的模块命令
- **Core message**: step runner 是模块执行入口，oc 选择交互surface。
- **Content**: skills/_sdk/notebook/run.py命令；accept需要用户确认；新解释器与前台运行；无oc run。

#### Slide 36 - 备份：来源与验证口径
- **Audience move**: 能审计汇报的事实依据。
- **Relationships**: contrast: 本次实测与已有交付记录；membership: 代码、运行契约和交付文档。
- **Title**: 备份：来源与验证口径
- **Core message**: 每项数量和进展都有代码快照或明确交付记录。
- **Content**: 43e9c034；88/0与28API本次实测；7024/846/88及1242为历史记录；不重跑科学实验；文档冲突纠正；ppt-master工具版本。

## X. Speaker Notes Requirements

- **Generation**: enabled
- **Filename**: match each SVG filename under `notes/`
- **Content**: 每页解释机制、设计动机与限制，给出可定位源码。数字注明本次核对或历史记录。案例只引用0074已存记录。
- **Total duration**: 正文约35分钟，备份页按问答使用。
- **Notes style**: 中文口语讲解，保留技术术语，避免夸大。
- **Presentation purpose**: 解释当前组件与设计取舍，并讨论后续验证重点。
