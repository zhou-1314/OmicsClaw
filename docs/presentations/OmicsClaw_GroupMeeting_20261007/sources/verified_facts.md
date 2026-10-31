# OmicsClaw 组会材料：已核实事实

核对日期：2026-10-07。代码提交：`43e9c034b1f04773aa2121d9aab0944beec3ec57`。
分支：`feat/spatial-skill-migration`。这是当前本地代码快照，不代表远端发布版本。

## 项目范围

`load_skills('skills')` 实测加载 88 个技能，0 skipped：单细胞 30、空间 17、bulk RNA-seq 14、基因组 10、蛋白组 8、代谢组 8、文献 1。README 中空间 19、单细胞 34 的分域表已过时。

开工快照中有 27 个单细胞 `_api.py` 和 1 个空间 `_api.py`。空间首个迁移技能是 `spatial-preprocess`。其余 16 个空间技能在开工快照中尚未交付；制作期间的 S2–S6 在制状态另见 `concurrent_work_note.txt`。源码目录中还存在 `common`、`attachments`，而 `remote`、`routing`、`surfaces`、`ensemble`、`runtime` 已不在当前 `omicsclaw/` 下；较旧的 AGENTS.md 目录说明不能作为当前状态。

来源：技能加载器实测、文件枚举、`docs/plans/0075-spatial-skill-migration.md`；完整清单见 `repository_snapshot.json`。

## 组件与设计依据

| 组件 | 当前职责 | 设计依据与取舍 | 源码入口 |
|---|---|---|---|
| schema | 统一 Message、ToolCall、ToolResult、Usage、StreamChunk 等交换类型 | 标准库叶子层，避免供应商格式扩散；保留工具参数原始 JSON | `omicsclaw/schema/` |
| provider | OpenAI-compatible 与 Anthropic 模型适配 | 配置保留在实例，engine 只传消息和工具；以 bind 创建配置变体 | `omicsclaw/provider/base.py`、`factory.py` |
| engine | 模型请求、工具执行、观察结果的循环 | 同步结果与流式输出共用一个内核；不负责持久化、提示词构造和工具实现 | `omicsclaw/engine/loop.py` |
| tools | 工具注册、定义暴露和调度入口 | 稳定顺序；通过结构化 Protocol 接入 engine，避免反向导入 | `omicsclaw/tools/registry.py` |
| context | 提示词组装、预算、工具输出卸载、渐进压缩 | 可用预算显式扣除输出、工具定义和安全余量；保留工具调用与结果配对 | `omicsclaw/context/budget.py`、`progressive.py` |
| skills | 索引技能元数据，按需加载正文 | 系统提示仅含 name/description；use_skill 才读完整正文，控制上下文开销 | `omicsclaw/skills/index.py` |
| planning | 独立于聊天记录保存计划并逐轮注入 | 防止压缩历史后丢失任务目标；计划状态不等同于分析结果验收 | `omicsclaw/planning/` |
| memory | SQLite 会话、FTS5 长期记忆、MEMORY.md 摘要 | 工作区数据库形成隔离边界；压缩前提取可长期保留的信息 | `omicsclaw/memory/` |
| permission | 根据模式、规则与危险命令模式决定工具能否执行 | 决策与展示审批分开；模式匹配不是完整的安全证明 | `omicsclaw/permission/` |
| sandbox | 为 bash 提供 Docker/Podman 执行环境 | 只隔离 shell；文件工具仍在宿主机受工作区约束，不能称全部工具均在容器中 | `omicsclaw/sandbox/` |
| hooks | 工具执行前后的拦截与审计 | 挂在工具上并位于 permission 内侧，保留 registry 的并发与超时接口 | `omicsclaw/hooks/` |
| observability | interaction/turn/model/tool 跨度与六类度量 | 消费已有 EngineEvent，避免往核心循环加入日志职责；默认不采集正文载荷 | `omicsclaw/observability/` |
| mcp | stdio、Streamable HTTP 外部工具接入 | 转成普通 Tool，让 engine 无须区分工具来源 | `omicsclaw/mcp/` |
| subagent | task 委派、角色定义、缩小工具范围 | 子任务复用同一 engine；子运行由 entry 构造，子代理包不自行拼装运行时 | `omicsclaw/subagent/`、`omicsclaw/entry/subagent.py` |
| entry | 组装 provider、工具、上下文、会话、审批与事件 | build_app 集中接线；同会话串行、跨会话并发；失败交换不替换已保存历史 | `omicsclaw/entry/assembly.py`、`session.py` |
| launch | 解析 oc 的 surface 与部署参数 | cli/desktop/channel 共用同一应用，避免各入口出现独立执行语义 | `omicsclaw/launch/` |
| skillenv | 依赖探测、可选审批安装与 overlay | 读取依赖注册表字面量，不导入科学代码；安装不改基础环境 | `omicsclaw/skillenv/` |
| evals | 确定性脚本化 Agent 测试与真实模型路由评估 | 经生产 assembly 执行；协议测试、真实模型与科学正确性须分别报告 | `omicsclaw/evals/` |
| skills/_sdk | 报告、结果、R 调用、依赖注册与 notebook step runner | 不导入 omicsclaw；两边通过文档与文件契约协作 | `skills/_sdk/`、`tests/sdk/test_boundary.py` |
| domain/_lib | 单一组学域内共享算法辅助代码 | 只依赖本域和 SDK，避免不同域相互牵连 | `skills/<domain>/_lib/` |

## 分析模块生命周期

`analysis/<NN_slug>/` 保存 README 和 Python/R 步骤；`results/<NN_slug>/` 保存图、表、中间文件、日志、notebook、provenance 与审查。跨模块策略放在 `docs/analysis_strategy/STRATEGY.md`。

Python 每个步骤使用新 kernel，R 步骤通过一个 Rscript 进程执行其各个 cell。跨步骤依赖通过文件与显式 IO 表达。只有经 `read_input`、`write_output` 等接口记录的行为进入对应台账，不能宣称对任意脚本进行了完整自动溯源。

`run` 执行过期步骤；`status` 根据步骤和输入哈希报告过期原因；`replay` 从头重跑并把 Python validate 步骤放在最后；成功重放生成 `review_brief.md`。之后撰写 REPORT、调用 module-reviewer，并由人确认后 `accept` 冻结模块。`revise` 为后续修改保留 baseline。

27 个单细胞技能经 `load_skill` 暴露返回数据或 Figure 的函数。CLI 负责参数、读写、报告与展示。`sc-count`、`sc-fastq-qc`、`sc-velocity-prep` 保留 CLI，step 通过 `run_cli` 调用。

来源：`OMICSCLAW.md`、`skills/_sdk/notebook/__init__.py`、`contract.py`、0074 交付记录。

## 近期可靠性改进及边界

模型请求的总 deadline 覆盖 SDK 重试、首响应及流式读取；deadline 耗尽后 engine 不再重试。异步 deadline 不能中断阻塞事件循环的同步代码。失败或取消不会回滚已经完成的工具副作用，也未实现中断交换的自动恢复。

`task(review_module=...)` 保存 reviewer 原始 UTF-8 回复与 JSON receipt。receipt 绑定 replay、步骤、README、REPORT 与 brief 的哈希；`accept` 校验框架保留文件名前缀下的 receipt。receipt 没有签名，也不是针对可同时改写文件和 receipt 的攻击者的防篡改保证。

来源：`docs/FRAMEWORK-REBUILD.md` 开头、`omicsclaw/entry/review.py`。

## 入口的当前状态

CLI、Desktop 和七种聊天平台共享 AgentApp。Desktop 当前源码已提供 `/chat/permission`、`/chat/abort` 与会话权限配置接口，旧 AGENTS.md 所列“未移植”的缺口不应继续出现在当前汇报中。Electron/Next.js UI 属于单独的 OmicsClaw-App 仓库；本次没有检查其当前代码，不能把后端接口存在说成前端全流程验证通过。

local-first 描述科学处理位置。远程模型仍会接收发给它的消息与工具结果，不能写成所有数据绝不离开机器。容器网络、权限、工具返回内容及部署方式共同影响数据边界。

来源：`README.md`、`omicsclaw/entry/desktop/server.py`、`interactions.py`、provider 与 assembly 的调用边界。

## 验证证据的使用范围

本次制作实际执行：88 技能与 0 skipped 的索引检查、28 个函数库的目录枚举、关键接口与路径核对、ppt-master integrity guard、隔离环境 `pip check` 与导出 CLI 启动检查。

已有文档记录：可靠性分支广泛回归 7,024 passed；最后修正后 846 项受影响测试及 88 项 Python 3.11 检查通过。空间 S1 受影响回归 1,242 passed。0074 干净课题的四模块实跑通过六项核心验收，同时交付记录保留额外 prompt 偏差。这些数字来自已保存交付记录，本次没有重新运行相应科学任务或全套测试。

函数库数量、测试数量和一次 demo 成功不能替代真实数据上的生物学正确性、统计有效性或跨模型泛化证据。缺少后端、数据库、真实容器和远端 CI 的验证仍应保留范围说明。0072/0073 是研究计划，不能当作已完成的对照实验结果。
