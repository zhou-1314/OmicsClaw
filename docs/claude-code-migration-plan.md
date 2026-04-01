# Claude Code Core Ideas -> OmicsClaw Migration Plan

> Status: Draft for review
> Date: 2026-03-31
> Scope: 仅制定迁移整合方案，不在本轮执行代码优化

## 1. 研究结论摘要

我对 `claude-code-source-code` 的理解结论是：它最有价值的不是某一个单点功能，而是把“最小 LLM tool loop”包裹成一套可生产化的 harness。其核心思想可以概括为：

1. 把复杂性从单个大 prompt 或单个大循环中拆出去，做成独立的运行时层。
2. 工具不是若干散落的 JSON 定义，而是带有并发属性、风险属性、渲染属性、权限属性的第一类对象。
3. 会话、计划、任务、上下文压缩、知识注入、插件、子代理，不直接耦合在主循环里，而是以可组合层的方式挂上去。
4. 真正决定上限的是“状态管理能力”，不是模型本身。

对 OmicsClaw 而言，最值得迁移的不是 Anthropic 私有能力，而是以下几类工程思想：

- 统一查询运行时
- 工具协议与并发调度
- 显式 plan mode + 结构化 task store
- 懒加载上下文与知识注入
- transcript / attachment / 大结果落盘
- 可验证的扩展系统
- 工作区隔离与完成前验证

不建议照搬的部分：

- Anthropic 私有 telemetry / GrowthBook / remote killswitch
- 内部 build-time feature gating 体系
- 产品化过重的 React/Ink UI 复杂度
- “undercover mode” 之类与 OmicsClaw 目标无关的行为逻辑

## 1.1 研读样本文件

本方案不是基于 README 印象写的，而是基于两个仓库的关键实现文件交叉比对后得出的。

`claude-code-source-code` 重点研读文件：

- `README.md`
- `src/query.ts`
- `src/services/tools/StreamingToolExecutor.ts`
- `src/services/tools/toolOrchestration.ts`
- `src/services/compact/autoCompact.ts`
- `src/utils/toolResultStorage.ts`
- `src/utils/sessionStart.ts`
- `src/utils/claudemd.ts`
- `src/utils/systemPrompt.ts`
- `src/utils/concurrentSessions.ts`
- `src/utils/plugins/pluginLoader.ts`
- `src/utils/plugins/loadPluginCommands.ts`
- `src/utils/plugins/loadPluginAgents.ts`
- `src/tools/EnterPlanModeTool/EnterPlanModeTool.ts`
- `src/tools/TaskUpdateTool/TaskUpdateTool.ts`
- `src/tools/EnterWorktreeTool/EnterWorktreeTool.ts`
- `src/tools/AgentTool/runAgent.ts`

OmicsClaw 重点研读文件：

- `bot/core.py`
- `bot/channels/middleware.py`
- `omicsclaw.py`
- `omicsclaw/core/registry.py`
- `omicsclaw/common/manifest.py`
- `omicsclaw/interactive/interactive.py`
- `omicsclaw/interactive/tui.py`
- `omicsclaw/interactive/_session.py`
- `omicsclaw/interactive/_mcp.py`
- `omicsclaw/agents/pipeline.py`
- `omicsclaw/agents/tools.py`
- `omicsclaw/agents/backends.py`
- `omicsclaw/memory/memory_client.py`
- `docs/architecture-analysis.md`
- `docs/research_pipeline_analysis.md`
- `docs/knowledge-system-optimization-plan.md`

## 2. 核心思想迁移判断表

| Claude Code 核心思想 | 参考实现 | OmicsClaw 当前对应面 | 迁移判断 | 优先级 |
| --- | --- | --- | --- | --- |
| 最小 loop + harness 分层 | `src/query.ts`, `src/main.tsx` | `bot/core.py::llm_tool_loop` 过于集中 | 强烈建议迁移 | P0 |
| 工具第一类对象化 | `Tool.ts`, `tools.ts`, `StreamingToolExecutor.ts` | `TOOLS` + `TOOL_EXECUTORS` 分离、类型弱 | 强烈建议迁移 | P0 |
| 并发安全工具批处理 | `toolOrchestration.ts`, `StreamingToolExecutor.ts` | 目前 tool calls 串行执行 | 建议迁移 | P1 |
| 显式 plan mode | `EnterPlanModeTool`, `planModeV2.ts` | 仅 research pipeline 内有 planner-agent | 建议迁移并泛化 | P1 |
| 持久化 task graph | `TaskUpdateTool`, task 工具族 | 目前仅有 `todos.md` + `.pipeline_checkpoint.json` | 强烈建议迁移 | P1 |
| 懒加载上下文 / 指令文件 | `claudemd.ts`, `attachments.ts`, `systemPrompt.ts` | 当前 `build_system_prompt()` 偏单体化 | 强烈建议迁移 | P1 |
| 上下文压缩 / 大结果落盘 | `autoCompact.ts`, `toolResultStorage.ts` | 当前仅有 `MAX_HISTORY` 截断 + history sanitize | 建议迁移，但先做轻量版 | P2 |
| 插件 manifest + 动态加载 | `pluginLoader.ts`, `loadPluginCommands.ts`, `loadPluginAgents.ts` | 当前 `/install-skill` 是 clone/copy 后直接注册 | 强烈建议迁移 | P2 |
| 会话生命周期 hooks | `sessionStart.ts`, hooks 体系 | 当前只有 channel middleware 与零散 pre/post 逻辑 | 建议迁移 | P2 |
| worktree / 隔离工作区 | `EnterWorktreeTool.ts` | 当前有 workspace，但隔离粒度不统一 | 适配迁移 | P3 |
| 子代理通信 / mailbox / team protocol | `runAgent.ts`, mailbox, teammate context | 研究流水线已有 sub-agent，但无统一协议层 | 延后迁移 | P3 |
| 完成前独立验证 | built-in verification agent 体系 | Research pipeline 有 reviewer，但 bot/interactive 无统一验证门 | 建议迁移 | P3 |

## 3. 当前 OmicsClaw 基线判断

OmicsClaw 不是从零开始，已有很多适合承接这些思想的基础设施：

- `omicsclaw/core/registry.py` 已经有稳定的 skill registry 和 SKILL.md 元数据读取能力。
- `omicsclaw/common/manifest.py` 已经有 pipeline lineage manifest，可作为 task/runtime ledger 的基础。
- `omicsclaw/interactive/_session.py` 已经有轻量会话持久化。
- `omicsclaw/interactive/_mcp.py` 已经有 MCP server 配置与加载入口。
- `omicsclaw/agents/pipeline.py` 已经有 workspace、checkpoint、planner/coder/reviewer 多阶段流程。
- `omicsclaw/agents/tools.py` 已经有 notebook execution 与 skill_search 体系。
- `bot/core.py` 已经实现 memory、capability resolver、knowledge injection、custom analysis、skill scaffolding 等能力。

真正的问题不在“缺功能”，而在“这些能力分散在多个入口里，没有统一 runtime 契约”：

1. `bot/core.py` 同时承担 system prompt 组装、tool schema 定义、tool executor、conversation history、memory 注入、knowledge 注入、progress 通知、tool loop。
2. interactive / TUI / bot / research pipeline 共享的是部分能力，不是统一运行时。
3. `/install-skill` 已支持 GitHub 安装，但缺 manifest 校验、来源声明、权限模型、热刷新契约，离真正的扩展系统还有明显差距。
4. research pipeline 的 `todos.md` 与 `.pipeline_checkpoint.json` 是阶段性状态文件，但不是结构化任务系统。
5. 当前 prompt 注入方式偏“拼接型”，长期会遇到 token 膨胀、职责混叠、复用困难的问题。

## 4. 目标架构

建议把 OmicsClaw 演进为“统一运行时 + 多入口适配器”的结构：

```text
omicsclaw/
  runtime/
    query_engine.py
    tool_spec.py
    tool_registry.py
    tool_executor.py
    transcript_store.py
    task_store.py
    context_assembler.py
    hooks.py
  extensions/
    manifest.py
    loader.py
    validators.py
  bot/
    core.py              -> 变成 channel adapter + runtime adapter
  interactive/
    interactive.py       -> 复用 runtime
    tui.py               -> 复用 runtime
  agents/
    pipeline.py          -> 复用 runtime/task/transcript/workspace 能力
```

关键原则：

- `bot/core.py` 不再是事实上的“主内核”，而是 runtime 的一个接入层。
- tool schema、tool execution、tool metadata、tool concurrency policy 必须同源。
- plan/task/transcript/context 都要成为跨入口共享的基础设施。
- 继续保留 OmicsClaw 的强项：skill registry、graph memory、knowledge advisor、reproducibility notebook、pipeline manifest。

## 5. 分阶段迁移整合计划

### Phase 0: 架构抽离准备层

目标：

- 在不改用户行为的前提下，先把未来演化边界定义清楚。

具体工作：

1. 新增 runtime ADR 文档，明确哪些逻辑从 `bot/core.py` 抽离。
2. 冻结当前对外兼容面：
   - `oc interactive`
   - `oc tui`
   - bot channels
   - `oc run`
   - research pipeline CLI / interactive commands
3. 建立迁移前基准测试集合：
   - tool history sanitize
   - session resume
   - capability resolver prompt block
   - install-skill 基础行为
   - research checkpoint resume

涉及文件：

- `docs/`
- `bot/tests/test_tools.py`
- `tests/test_session.py`
- `tests/test_manifest.py`
- `tests/test_phase2_validation.py`

验收标准：

- 无任何对外行为变化。
- 补齐覆盖统一 runtime 重构的回归测试基线。

### Phase 1: 统一 Tool Runtime

目标：

- 把当前散落的 `TOOLS` JSON 和 `TOOL_EXECUTORS` 映射升级为统一的 typed tool protocol。

具体工作：

1. 设计 `ToolSpec`:
   - `name`
   - `description`
   - `input_schema`
   - `executor`
   - `read_only`
   - `concurrency_safe`
   - `surfaces` (`bot`, `interactive`, `pipeline`)
   - `result_policy`
   - `progress_policy`
2. 新建 `tool_registry.py` 作为唯一真相源。
3. 从 `ToolSpec` 自动生成：
   - OpenAI function-calling schema
   - 运行时 executor lookup
4. 将 `bot/core.py` 的现有工具逐步迁移到 runtime registry。
5. 引入并发执行器：
   - 只读工具并发
   - 写工具串行
   - 输出顺序稳定
   - 工具失败不破坏完整 bundle

优先迁移的工具：

- `inspect_data`
- `consult_knowledge`
- `resolve_capability`
- `remember`
- `list_directory`
- `inspect_file`
- `omicsclaw`
- `web_method_search`

涉及文件：

- 新增 `omicsclaw/runtime/tool_spec.py`
- 新增 `omicsclaw/runtime/tool_registry.py`
- 新增 `omicsclaw/runtime/tool_executor.py`
- 重构 `bot/core.py`

验收标准：

- `TOOLS` 与 `TOOL_EXECUTORS` 不再手工双维护。
- 至少一组只读工具支持并发执行。
- 现有 bot tests 全部通过。

### Phase 2: Plan Mode + Task Store

目标：

- 把 research pipeline 内部已有的“planner + todos + checkpoint”思想泛化为 OmicsClaw 通用任务层。

具体工作：

1. 新建 `task_store.py`，支持：
   - task id
   - status
   - owner
   - dependencies
   - artifact refs
   - timestamps
2. 用结构化 task store 替代 research pipeline 里仅用于展示的 `todos.md`。
3. 保留 `todos.md`，但改为 task store 的投影视图。
4. 新增 plan mode 命令和交互协议：
   - `/plan`
   - `/tasks`
   - `/approve-plan`
   - `/resume-task`
5. 当用户请求复杂分析、创建 skill、或多步骤 pipeline 时，runtime 可进入显式 planning state。
6. 将 `PipelineState` 与 task store 对齐，避免一套阶段状态、一套 markdown todo 的双轨维护。

涉及文件：

- 新增 `omicsclaw/runtime/task_store.py`
- 重构 `omicsclaw/agents/pipeline.py`
- 重构 `omicsclaw/interactive/interactive.py`
- 视需要扩展 `omicsclaw/interactive/tui.py`

验收标准：

- research pipeline 的阶段状态可从 task store 重建。
- `todos.md` 由结构化任务自动导出。
- 复杂任务的计划得到显式保存和恢复。

### Phase 3: Transcript / Attachments / Context Budget

目标：

- 解决长会话、长工具输出、resume 一致性、prompt 膨胀问题。

具体工作：

1. 在现有 `interactive/_session.py` 之上扩展 transcript store：
   - assistant/tool bundle 持久化
   - artifact attachment
   - plan reference
   - advisory event
2. 大工具输出落盘，不直接塞进上下文：
   - 比如长报告、长日志、长 traceback、长表格预览
   - 保留 preview + file reference
3. 实现轻量版上下文预算器：
   - 第一阶段只做结果落盘 + selective replay
   - 第二阶段再做 conversation summary
4. interactive/bot resume 时，优先恢复结构化 transcript，而不是只恢复 message list。
5. 统一 `MAX_HISTORY` 截断逻辑为 budget-aware 策略。

明确不做的事情：

- 这一阶段不追求 1:1 复制 Claude 的 microcompact / snipCompact / contextCollapse。
- 先做保守版：落盘、引用、摘要，不做激进语义删除。

涉及文件：

- 新增 `omicsclaw/runtime/transcript_store.py`
- 新增 `omicsclaw/runtime/context_budget.py`
- 重构 `bot/core.py`
- 重构 `omicsclaw/interactive/_session.py`

验收标准：

- 长工具结果不会污染主对话上下文。
- resume 后 tool bundle 结构不损坏。
- 长会话稳定性明显提升。

### Phase 4: 懒加载 Context Assembler

目标：

- 把当前单体式 `build_system_prompt()` 演进为分层、按需、可测试的 context assembler。

具体工作：

1. 将 prompt 组装拆成独立 injector：
   - base persona
   - role guardrails
   - memory context
   - capability assessment
   - Know-How constraints
   - skill contract
   - workspace context
   - MCP instructions
2. 明确哪些内容放 system prompt，哪些放 attachment/message context。
3. 将当前 capability + KH 注入从字符串拼接升级为结构化 layer。
4. 为每个 layer 增加独立测试：
   - 触发条件
   - token 成本
   - 覆盖优先级
5. 对 research pipeline、bot、interactive 三个入口复用同一个 assembler。

涉及文件：

- 新增 `omicsclaw/runtime/context_assembler.py`
- 新增 `omicsclaw/runtime/context_layers/`
- 重构 `bot/core.py::build_system_prompt`
- 适配 `omicsclaw/agents/pipeline.py`

验收标准：

- 不再由 `bot/core.py` 直接拼接所有 prompt。
- 每种上下文来源都可单独开关、测试和统计。
- token 使用更稳定，可观察。

### Phase 5: 扩展系统升级

目标：

- 把当前“clone/copy 到 `skills/user/` 就算安装成功”的方式，升级为可验证、可描述、可审计的扩展系统。

具体工作：

1. 定义轻量扩展 manifest，例如：
   - `name`
   - `version`
   - `type` (`skill-pack`, `agent-pack`, `mcp-bundle`, `prompt-pack`)
   - `entrypoints`
   - `required_files`
   - `trusted_capabilities`
   - `dependencies`
2. `/install-skill` 改造成安装器而不是纯 clone/copy：
   - 拉取
   - 校验 manifest
   - 校验目录结构
   - 校验 SKILL.md/frontmatter
   - 注册
   - 写安装记录
3. 第二阶段扩展到：
   - 用户 agent 包
   - MCP bundle
   - prompt/rule bundle
4. 增加卸载、刷新、列举、禁用能力。
5. 对不受信任来源做能力收缩：
   - 只允许 skill pack
   - 不允许隐式注入 hooks
   - 不允许越权修改 runtime policy

涉及文件：

- 新增 `omicsclaw/extensions/manifest.py`
- 新增 `omicsclaw/extensions/loader.py`
- 新增 `omicsclaw/extensions/validators.py`
- 重构 `omicsclaw/interactive/interactive.py`
- 重构 `omicsclaw/interactive/tui.py`
- 视需要扩展 `omicsclaw/core/registry.py`

验收标准：

- 任意 GitHub skill 安装前都经过结构校验。
- 扩展来源、版本、能力范围可追踪。
- skill 安装从“实验功能”升级为“有契约的扩展机制”。

### Phase 6: Workspace Isolation + Verification Gate

目标：

- 把 OmicsClaw 已有的 workspace 思想进一步工程化，形成“可隔离执行、可恢复、可验证完成”的闭环。

具体工作：

1. 正式区分两类 workspace：
   - conversation workspace
   - analysis run workspace
2. 对 `create_omics_skill` / custom analysis promotion 增加隔离执行区：
   - 临时工作区
   - 可选 git worktree
3. 为以下场景增加 verification gate：
   - skill scaffold / promote
   - research pipeline final output
   - custom analysis result promotion
4. 将 reviewer-agent 从“文本性审查”提升为“结构化产物验证”：
   - `plan.md`
   - `manifest.json`
   - notebook
   - report
   - figure/table manifests
5. 输出统一 completion report，避免“模型说完成了，但产物不齐”。

涉及文件：

- 重构 `omicsclaw/agents/pipeline.py`
- 扩展 `omicsclaw/common/manifest.py`
- 视需要新增 `omicsclaw/runtime/verification.py`
- 视需要扩展 `omicsclaw/core/skill_scaffolder.py`

验收标准：

- 复杂分析和 skill promotion 都有明确完成门槛。
- 工作区中产物契约完整，可追踪、可恢复、可审计。

### Phase 7: 高级多代理协作层

目标：

- 在前述基础稳定后，再考虑 Claude Code 风格的 mailbox/team protocol/background tasks。

建议只在以下前提下进入：

- unified runtime 已稳定
- task store 已稳定
- transcript / attachments 已稳定
- 扩展系统已稳定

可做事项：

- agent mailbox
- background analysis jobs
- reviewer / planner / executor 间结构化消息协议
- 可选的 task auto-claim

当前建议：

- 延后，不进入第一轮迁移。

## 6. 建议的实施顺序

推荐顺序不是“功能越炫越先做”，而是先解耦主内核：

1. Phase 0
2. Phase 1
3. Phase 2
4. Phase 4
5. Phase 3
6. Phase 5
7. Phase 6
8. Phase 7

说明：

- `Phase 4` 之所以放在 `Phase 3` 前，是因为先拆 context assembler，才能清楚知道 transcript 和 budget 应该保留什么。
- `Phase 5` 不应早于 runtime/tool/context 稳定，否则会把不稳定内核暴露为外部扩展契约。
- `Phase 7` 必须最后做，否则会把当前尚未统一的状态管理复杂度放大。

## 7. 关键风险与规避策略

### 风险 1: 过早大改 `bot/core.py` 导致全入口回归

规避：

- 先做 runtime adapter，再逐步把 `bot/core.py` 逻辑迁出去。
- 每阶段保留兼容 shim。

### 风险 2: 模仿 Claude 的 context compaction 过深，造成科学结果丢失

规避：

- 第一版只做落盘和引用，不做激进 summary 删除。
- 所有数字类结果优先保留原文或文件引用。

### 风险 3: 插件化过快，扩大安全面

规避：

- 先做 manifest 验证和 capability scope。
- 第一期只开放 skill-pack，不开放任意 hook 注入。

### 风险 4: plan/task 系统与现有 research pipeline 双轨并存太久

规避：

- 把 `todos.md` 降级为导出视图，结构化状态只保留一套。

### 风险 5: 多入口各自演化，统一 runtime 失败

规避：

- 任何新能力先落 runtime，再由 bot/interactive/pipeline 接入。
- 禁止再在某一个入口里新增独占型核心逻辑。

## 8. 建议本轮审批重点

在你审核这份方案时，建议重点确认下面 5 个决策是否同意：

1. 是否同意把 `bot/core.py` 从“主内核”降级为 adapter。
2. 是否同意先做统一 tool runtime，再做插件系统。
3. 是否同意把 `todos.md` 升级为 task store 的投影视图，而不是继续手工维护。
4. 是否同意 context 注入改为分层 assembler，而不是继续扩展单体 prompt。
5. 是否同意第一轮不做重型 swarm/mailbox，而优先做 runtime、task、transcript、extensions。

## 9. 审批通过后的首轮执行建议

如果你批准，我建议第一轮优化只做以下内容，不跨太大步：

1. Phase 0 全量完成。
2. Phase 1 做到 unified tool runtime 骨架落地，并迁移 5 到 8 个高频工具。
3. Phase 2 先把 research pipeline 的 `todos.md` 结构化。
4. Phase 4 先把 `build_system_prompt()` 拆成可测试的 context layers。

这四步完成后，OmicsClaw 的主干会从“功能堆叠”变成“有内核、有边界、有扩展点”的系统，后续再继续做 transcript、插件、安全和验证，风险会低很多。
