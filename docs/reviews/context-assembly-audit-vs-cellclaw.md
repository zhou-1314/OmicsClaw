# OmicsClaw 上下文 / Payload 组装设计审计 —— 对标 cellclaw

> 审计对象：OmicsClaw 的 LLM 请求（system + messages + tools）组装链路
> 参照系：`cellclaw_source` 的 `ContextAssembler` 模式
> 方法：`diagnose` 纪律（可证伪假设 → 逐一取证 → 影响评估 → 修复建议；不靠直觉下结论）
> 日期：2026-07-01
> 代码位置：OmicsClaw `/work/zhouweige_data/project/OmicsClaw`（= 软链 `/home/weige/project/OmicsClaw`，含未提交改动）；cellclaw `/home/weige/project/repo_learn/cellclaw_source`
> 状态：可落地项**全部完成并已合并到 `main`**（PR #22，merge commit `5d3e926`，17 个审计 commit `cb33fb9..0fa9ae5`）。逐条完成状态见 **§1.5 实现状态总览**。§8 为初稿的 Codex 定稿记录。
>
> **阅读说明（行号锚定）**：§2–§9 的正文与"证据" `file:line` 是 2026-07-01 基于 `main@cb33fb9`（**实现前基线**）的**审计快照**——此后代码已随 17 个 commit 位移，这些历史行号会偏，属**时点记录**而非当前坐标（刻意不重写，以免用实现后的行号歪曲"当时所见"）。**实现现状 + 可导航的当前行号**见 **§1.5 实现状态总览**、各 finding 标题的 ✅/⏸ 标记、**§9 落地实况**、以及 **§10 附录（已刷新到 `main@0fa9ae5`）**。

---

## 1. 执行摘要（TL;DR）

**一句话结论**：OmicsClaw 的组装链路在**缓存工程、工具列表稳定性、历史裁剪安全性、并行装配、渐进压缩**上明显比 cellclaw **更先进**；但在**预算准确性（字符 vs token）、预算状态可决策性、摘要保真度、装配路径可读性**上比 cellclaw **更弱或自相矛盾**。用户"cellclaw 更清晰"的直觉，对**消息装配的可读性**成立，但要打两个折扣：cellclaw 的"清晰"部分是**表象**（9 段 sections 里 4 段是生产路径死代码），而 OmicsClaw 的"复杂"换来了 cellclaw 没有的真实能力（缓存分级、多 surface 复用）。

**三条最值得改的**（细节见 §3，已并入 Codex 交叉审核 §8）：
1. **F2（高）**——compaction 摘要格式前后不一致（当轮 `## Context Collapse` vs 下轮 hoist 成 `## Persisted Compacted Context`）导致**一次压缩两次刷缓存前缀**。ADR 0024 本允许一次 deliberate re-warm，问题在实现多刷了一次。最高性价比修复。
2. **F4（中，Codex 建议前移）**——内联 base64 图像块对字符预算近乎隐形（只计 ~9 字符，附件确生成 data URI `_attachments.py:191-197`），组织切片图 → 预算严重低估 → 撞窗回落 reactive 兜底。低成本高确定性。
3. **F5（中）**——无显式预算 status/block 门（`OK/WARN/COMPRESS/CRITICAL/BLOCK`），输入侧只能静默降级。（初列 top-3 的 F8"prompt pack 前缀 churn"经取证**证伪**——loader `del` 掉 query、pack 内容会话稳定，system placement 正确；详见 §3b。）

> 取证纪律留痕：初稿 F1"字符预算固定、不随模型窗口"经取证**证伪**（`resolve_max_prompt_chars` 按模型推导，`engine/loop.py:181-195`）→ 下修为中并重写；F2"直接违反 ADR"经 Codex 复核**收窄**（ADR 允许一次 re-warm）→ 重构为"双 re-warm"。完整裁决 + Codex 补充的 F8–F13 见 §8 与 §3。

> **用户定向（2026-07-01）——三条采纳方向，已提升优先级，设计草图见 §9：**
> - **决策 1**：摘要不走"纯确定性"取舍，**采纳 cellclaw 的 LLM 浓缩总结**（无损优先 → LLM → 校验），确定性模板降为 fallback（重塑 F6，见 §9.1）。
> - **决策 2**：cellclaw 的 `user_profile / project_state` 等 section 是**用户明确的扩展方向**，作为**可选槽位**保留（重塑 §6 对"9 段"的定性，见 §9.2）。
> - **决策 3**：cellclaw 的上下文预算控制设计"非常清晰"，**采纳其控制平面**（token 化 `effective_capacity` + 五级 status + 压到目标；合并 F1+F5，见 §9.3）。

---

## 1.5 实现状态总览（2026-07-02 复审 · 已逐条对活代码 grep 取证）

> 本审计的**可落地项基本落地**。下表每一行的"状态"均已对**当前代码**核实（grep 符号/证据点，非仅凭 commit message），Commit 对应 `cb33fb9..HEAD` 的 16 个 commit。图例：✅ 已完成并 push 到 PR #22 · ⏸ 合理但**有意暂缓**（附条件）· 🔜 合理且**推荐作下一步**。

| 项 | 合理性 | 状态 | Commit | 活代码核实点 |
|---|---|---|---|---|
| **F2** 摘要 canonical 化（一次压缩=一次 re-warm） | 合理·高 | ✅ 完成 | `ccc0b05` | 单一 `## Persisted Compacted Context`（compaction.py） |
| **F4** 多模态计入预算 | 合理·中 | ✅ 完成 | `2db1ec8` | `_IMAGE_BUDGET_CHARS=4000` 计入 `estimate_message_size` |
| **F3** 单趟 system 装配 + 删 legacy builder | 合理·中 | ✅ 完成 | `1378370`+`19fcc55` | assembler 中 `system_prompt_builder` 已归零 |
| **F5** 观测态预算 status | 合理·中 | ✅ 完成（观测态） | `45eb3dc`+`dea4d90`+`72f140f` | `ContextBudgetStatus`/`classify_context_budget`/`local_budget_status` |
| **§9.3 slice 3** 压到目标压缩 | 合理·决策3 | ✅ 完成 | `584f710` | `collapse/auto_compact_target_ratio` 驱动 `_collapse_with_target` |
| **B3** budget status→CompactionEvent/SSE | 合理 | ✅ 完成 | `e47bbcd` | `_budget_status_str`/`build_compaction_status_payload` |
| **F11** snip 不改写历史 tool_call 参数 | 合理·中 | ✅ 完成 | `4de8794` | `snip_tool_argument_chars` 已删（=0） |
| **F10** sanitize 保住已成功 tool 结果 | 合理·中 | ✅ 完成（根因+repair） | `7c29f88`+`5e18e8d` | `_INTERRUPTED_TOOL_PLACEHOLDER` 修复不完整 bundle |
| **F8** prompt-pack 前缀 churn | **已证伪** | ✅ 护栏注释 | `b4f3872` | loader `del query`；`extensions/runtime.py` 前瞻护栏注释 |
| **96000→256000 预算政策** | 合理·§7 | ✅ 完成 | `edc6c7e` | 单一常量 `DEFAULT_MAX_PROMPT_CHARS=256000` |
| **F6 / 决策1** LLM 浓缩摘要（opt-in） | 合理·中 | ✅ 完成 | `c3bf709` | 三层+长度/字节 cap+反-mimicry；codex 3 轮 VERIFIED |
| **F9** 并发装配 cancel cleanup | 合理·中 | ✅ 完成 | `3ba9449` | `_spawn`+`finally` reap；codex CORRECT |
| **F1** chars/token 校准 | 合理但**边际**（96000 部分已由 256000 政策解决） | ⏸ 可选未做 | — | 需离线抽样 CJK/JSON/图像真实 chars/token；256000 上限下近乎只影响观测态诚实度 |
| **F7** thin/fat 队列解耦 | 合理但**规模化才需** | ⏸ 暂缓 | — | 桌面/CLI 内联够用；高并发 web 才下沉 worker |
| **F12** 缓存诊断加 base_url/model 指纹 | 合理但**低价值** | ⏸ 暂缓（触发式） | — | model/provider 已被 identity anchor 覆盖，残留仅 base_url swap |
| **F13** append/mode/stage 登记 sanctioned re-warm | 合理·低（near-free） | ⏸ 暂缓（ADR doc-note） | — | 编相关 ADR 时顺手登记 + 诊断白名单标签 |
| **F14** transcript write-through 落盘 | 合理但**视部署** | ⏸ 暂缓（部署门） | — | 当前单用户桌面不必；多用户 channels/重启韧性成目标才做 |
| **§9.3 slice 4** 硬 BLOCK 门 | 合理但**价值窄/有误拒风险** | ⏸ 暂缓（用户定） | — | 粗字符估算硬 refuse 会误拒可服务请求，reactive-413 兜底更优 |
| **决策2** memory_context placement 拆分 | 合理（用户扩展方向） | ✅ 已实现（未提交，codex runtime CORRECT） | — | 拆分而非加空槽：durable identity（project+prefs）留 system `## Your Memory`；volatile work-state（dataset/analysis/insight）→ message `## Current Work State`（`project_state_context`, order 44）。`session.py:load_context_layers`；`load_context` 字节不变（codex 1024 组合 mismatches=0）。TDD 复现 churn 红→绿 |

**§9.3 slice 1/2**（预算 status 原语 + 观测态接进 `PreparedModelMessages`）：✅ 完成（`45eb3dc`/`dea4d90`/`72f140f`）。

> **2026-07-03 · 范式重构决策（ADR 0039 / 0040）**：本审计原本 deferred 的两项已被用户提升为正式重构决策，超越本文档当时的保守排序——
> - **F1（char↔token）+ §7（96000/256000 成本策略）+ 控制平面五个半成品 → [ADR 0039](../adr/0039-token-native-context-budget.md)（Proposed）**：token-native 控制平面（一个 token 预算驱动 budget/status/compaction），**退役** char 机制（`CHARS_PER_TOKEN`/char 版 `resolve_max_prompt_chars`/恒 OK 的 window-relative `budget_status`）+ **舍弃 256000 char 成本策略**，改为 **85k token 延迟护栏** `min(85_000, 0.5×effective_capacity)`；压缩目标保持静态 0.55/0.40（F2 护栏）；**LLM collapse 摘要默认 ON**（用户 2026-07-03 定，重 LLM 语义质量、放弃默认确定性；仍长度/字节封顶保字节稳定，reactive-413 仍确定性）。本审计 §7/§9.3 的"char 校准边际、deferred"结论**被 ADR 0039 取代**。
> - **F14（transcript 持久化）→ [ADR 0040](../adr/0040-restart-resilient-transcript-persistence.md)（Proposed）**：write-through **P-state 派生态镜像**进独立 `transcripts.db`（SQLite，非 memory.db）+ **冷启动/miss 时一次 rehydrate**（非每轮重载，保 ADR 0024 字节稳定）；tool blob 仍留 ToolResultStore 文件。
>
> 两个 ADR 均**完整保留 ADR 0024 缓存不变量**。设计经 grill-with-docs + codex(gpt-5.5 xhigh) 三轮交叉核验落定。

### 1.5.1 下一步优化方向（复审推荐）

审计的 **P0/P1 高价值项全部完成且 codex-clean**；剩余项要么低价值、要么有意暂缓（附条件）。按性价比推荐：

1. **【首选·收尾】合并 PR #22**。16 个 commit 均已 codex 交叉审核通过，缓存框架（ADR 0024）的自反噬问题（F2/F10/F11）+ 预算控制平面（§9.3）+ 装配债（F3/F9）+ LLM 摘要（F6）全部落地。**把已验证的成果先 ship，胜过继续 stack**——大 stack 越长评审/回归成本越高。
2. ✅ **【已实现 2026-07-02】决策2 = `memory_context` placement 拆分**：durable identity（project+prefs）留 system、volatile work-state（dataset/analysis/insight）→ message `project_state_context`。TDD 复现 churn 红→绿,`load_context` 字节不变,codex runtime CORRECT（1024 组合 mismatches=0），P2 文档漂移已修（ADR 0024 §3 + CONTEXT.md）。未提交。详见 §9.2。
3. **【near-free 观测态收尾】F13 + F12 合并成一个小 commit**：F13 把 `system_prompt_append`/`mode`/`stage` 在 ADR 里登记为 sanctioned re-warm 点 + 诊断白名单标签；F12 给缓存诊断加 `base_url` 指纹。二者都是"缓存可观测诚实度"的低成本闭环。
4. **【可选·非 keystone】F1 chars/token 校准**：需离线抽样 CJK/JSON/图像真实 chars/token；256000 上限下主要影响观测态诚实度 + <170k-token 小窗模型，边际价值。

> 我的建议序：先 **①合并 PR #22** 收束这一大批；若要再投一项实现，选 **②placement 拆分**（唯一有实质缓存正确性收益的剩余项）。③/④ 视精力顺手做。**F7/F14/slice4 在触发条件（规模化/多用户/校准完成）到来前不动。**

### 1.5.2 Codex 交叉验证（2026-07-02 · gpt-5.5 xhigh · read-only）

Codex 独立 grep 活代码复核了 §1.5 每一行——**12 个 ✅ 全部 CONFIRMED**（逐条 file:line 证据）、**所有 ⏸ 确认未做**、**决策2 的"已注入 / 拆分而非加槽"判断 CONFIRMED**（`session.py:load_context` 把 dataset/analysis/preference/insight/project 装进单块 `memory_context` → system 层；`scoped_memory_context` 是另一条 query/workspace-scoped recall，非被动块）。下一步排序判为 **sound**。**无标记被判错。** 两点细化（非阻塞）：
- **F2**：单一 `## Persisted Compacted Context` 外层块内仍保留 `### Context Collapse` / `### Auto Compacted Context` 子标题——**设计如此**（子段在 sent / persisted / hoist 三处走同一 `_combine_persisted_summaries`，跨轮字节稳定），F2 ✅ 不变。
- **F13**：仅 `mode` 已在 ADR 0024 描述为 sanctioned；`append` / `stage` 的完整登记 + 诊断白名单**未做**——与本表 F13 ⏸ 一致（顺手补齐即闭环）。
- Codex 声明沙箱内未跑 pytest；"codex-clean / CI 绿"由本地 **179 tests green** + 各功能的独立 codex 轮次背书。

---

## 2. 两侧架构对照

### 2.1 cellclaw 的组装模型（已独立取证）

**（a）两个 ContextAssembler（不是三个）+ 队列解耦。** 全仓仅两个同名类：
- `web/services/context_assembler.py:65` —— "瘦 payload"侧。类名叫 `FatPayload`（历史遗留命名）但**实为 thin**：只装 `task_id / session_id / user_input / user_id / project_id …` 等标量 id/引用，**不取任何内容**（`assemble_fat_payload` `:122-138` 只 mint 一个 `task_id` 就返回；遗留的 `_get_history_async` 等 `:140-234` 保留但不调用）。**无预算感知。**
- `agent/core/context_assembler.py:39` —— "真装配"侧，在 **worker** 内每个决策回合跑一次，产出真正的 OpenAI `messages[]`，**有 token 预算**。

  rehydration 路径：web 入队 thin payload（`chat_dispatch.py:432`）→ worker `FatPayload.from_dict`（`stateless_worker.py:335-367`）→ 用 `session_id` 从 DB 查历史（`_get_history_from_db` `:1339`，调用点 `:1676`）→ 交给 `MetaAgent` 跑真装配。**ingress 与重装配被消息队列解耦。**

  > 用户提到的"第三个双胞胎"在代码里**不存在同名类**；最可能指 `memory/context_budget.py`（预算引擎）或 `agent/prompting.py`（系统提示构建器）。

**（b）真 `assemble()`（`agent/core/context_assembler.py:46`）五步：**
1. **分区**（`:63-105`）——有序插入 9 段：`system_prompt, user_profile, project_state, relevant_memories, dialog_summaries, dialog_history, loop_conversation, user_input, memory_context`。
   ⚠️ **但生产路径只有 5 段是活的**：`MetaAgent._build_decision_messages` 调 `assemble()` 时只传 `system_prompt / history / user_input / loop_conversation / memory_context`（`meta_agent.py:670-684`），另外 4 段（`user_profile/project_state/relevant_memories/dialog_summaries`）**从不被生产代码传入**（仅测试传）。所以那套"优雅 sections"里 **4/9 是死代码**。
2. **算 token**（`:107-112`）——`estimate_tokens`（**tiktoken**，`memory/context_budget.py:167-192`，按模型选 `o200k_base`/`cl100k_base`，encoding 对象 `@lru_cache`）。
3. **超预算整条驱逐**（`:116-140`）——`overage = total_raw - effective`；`while overage>0: history_payloads.pop(0)`（**丢最老的整条，不截断**），不够再丢 `loop_payloads`；重建 section 串。`effective = context_window - reserved_output(4096) - safety_margin(2048)`（`model_context.py:32-38`，**按模型窗口计算**）。
4. **`_build_messages`（`:180-228`）线性拼装**（顺序清晰、可自上而下读）：
   - `[system#1]` = `system_prompt + user_profile + project_state`
   - `[system#2]` = `relevant_memories + dialog_summaries`（**单独第二条 system，仅当非空**；生产路径为空）
   - `[history…]` 逐条**原样**还原（保留 `tool_calls/tool_call_id`）
   - `[user]` = `memory_context + user_input`（**memory 挂在 user turn**）
   - `[loop…]` 本轮工具往返**放在最末尾**
5. **预算快照**（`:150-176`）——`ContextBudgetEvaluator` 产 `ContextUsageSnapshot`（五级 status）+ `CompressionRecommendation`（含 `target_reduction_tokens`）。

**（c）五级预算状态**（`model_context.py:40-50`，按 `used/effective`）：`OK <65% ≤ WARNING <80% ≤ COMPRESS <90% ≤ CRITICAL <96% ≤ BLOCK`。`_maybe_compress_context`（`meta_agent.py:1159-1222`）在 status ∈ {compress,critical,block} 时触发，**目标压到 effective 的 75%**，并向前端发"压缩中/完成"卡片。

**（d）摘要是 LLM 生成 + 无损优先 + 校验**：`compression_engine.py` 三级——`LosslessReducers`（压工具输出、折叠重复日志/观察、截超长，保留文件路径/标记）→ 分 episode `_llm_summarize_episode`（带确定性 fallback）→ `validate_summary`（校验关键文件路径/待办 TODO 未丢）。

**（e）缓存稳定性——弱且非系统化**：**仅一处**显式考虑（`meta_agent.py:645-650`：动态执行态塞进 `memory_context`→user turn，"keep system prompt stable for provider prompt/KV caching"）。**无 `cache_control`**；且**工具每轮 `to_schemas()` 重建**（`meta_agent.py:992/1000`，无 memo）**并被渲染进 system 的动态 cheat-sheet**（`prompting.py:40+`）——工具集一变，system 前缀就变。历史 `pop(0)` 也会让前缀之后的缓存失效。

### 2.2 OmicsClaw 的组装模型（已独立取证）

**链路**：`Surface → dispatcher.dispatch()(dispatcher.py:60) → llm_tool_loop(loop.py:781) → run_engine_loop(engine/loop.py:215) → assemble_chat_context(assembler.py:305) → run_query_engine → prepare_model_messages(compaction.py:454) → chat.completions.create(query_engine.py:1197)`

**（a）分层注入器 + placement 装配**（不是线性 sections）：
- `assemble_prompt_context`（`assembler.py:282`）遍历 `DEFAULT_CONTEXT_LAYER_INJECTORS`，每个 injector `applies()` 判定后 `render()` 出一个 `ContextLayer`，按 `(order, name)` 排序，`"\n\n".join`（`_render_layers` `assembler.py:126`）。
- 每层带 `placement ∈ {system, message, attachment}`（`ContextLayer` 默认 `system`，`layers/__init__.py:478`）。
- **ADR 0024 已把易变层再分级到 `message`**（`layers/__init__.py:1092-1168`）：`skill_context(42) / scoped_memory(45) / capability(50) / knowledge(52) / plan(55) / transcript(58) / knowhow(60)` + 所有 predicate 规则(12-19) 全在 `message`；system 里只剩 `base_persona(10) / research_stance(10) / surface_voice_rules(11) / output_format(15) / extension_prompt_packs(35) / memory_context(40) / workspace(70) / mcp(80)`。
- `memory_context` 虽在 system，但 `load_context(session_id, thread_id)`（`assembler.py:340`）**不传 query** → 非按 query 变。〔**决策2 校正**：此"缓存安全"仅对 query-无关成立;记忆会 **mid-session 写入**(dataset/analysis/insight/preference/project),写入后 system 前缀仍 churn。故 volatile 部分(dataset/analysis/insight)已拆到 message 层,见 §9.2 / §1.5〕

**（b）ADR 0024 缓存三区**（`docs/adr/0024-prompt-prefix-caching.md`）：对标 DeepSeek-Reasonix 99.82% 命中——immutable prefix（system+tools 会话初冻结）/ append-only log / volatile scratch；**工具列表按 surface 冻结**（`engine/loop.py:328` `to_openai_tools_for_request(surface_only=True)`，**逆转了此前的每轮工具压缩**）；缓存诊断 `compute_segment_hash / infer_miss_reason / extract_cache_tokens`（DeepSeek+Anthropic 双支持）把不变量从"愿望"变成可观测信号。

**（c）字符预算 + 五级压缩**（`compaction.py`）：
- 预算单位 = **字符**（`estimate_message_size` `budget.py:9`、`estimate_prompt_chars` `compaction.py:95`）；`max_prompt_chars` **按模型窗口推导**：`resolve_max_prompt_chars(model)`（`engine/loop.py:181-195`，调用点 `:400`）= `min(96000, window_tokens × _CHARS_PER_TOKEN(3.0) × _PROMPT_BUDGET_FRACTION(0.5))`，另有 env `OMICSCLAW_MAX_PROMPT_CHARS` 覆盖；`auto_compact_trigger_ratio=0.92`、`collapse_trigger_ratio`、各级 preserve 计数（`compaction.py:58-76`）。
- 每轮 `prepare_model_messages`：先把 `messages[0]` 里的 persisted summary **提进 system**（`:475-484`）→ snip → micro → **主动** collapse（超 `collapse_threshold`，`:532-561`）→ **主动** auto_compact（超 0.92 阈值，`:563-582`）；provider 报 "prompt too long" 再 `force_reactive_compact=True` 重试一次兜底（`query_engine.py:1220`）。
- 历史裁剪 `trim_history_to_budget`（`budget.py:61`）**block-aware**：`_group_history_blocks`（`:42`）把 assistant+其 tool 结果捆成块、整块取舍、保留最新后缀——**不会把 tool_call/tool_result 拆散**。
- 摘要 `_build_collapse_summary`（`compaction.py:340`）**确定性模板 / 无 LLM**（`compact_history` `:409` 注释 "No LLM call"）：每 role ≤3 条 highlight + ≤3 条工具引用。

**（d）并行异步装配**：`assemble_chat_context`（`assembler.py:305`）把 memory / capability / prompt-pack / scoped-memory / skill context 用 `asyncio.create_task` **并发**拉取（`:342,355,398,408,438`）。

**（e）另一条预算轴（勿混淆）**：`TokenBudgetTracker`（`budget.py:124-256`）是**输出/续写** token 预算（"+500k" 式续跑/停机判定），与上面的**输入上下文窗口**预算是两回事。OmicsClaw 输入侧无 status 枚举，输出侧才有 tracker。

### 2.3 对照表

| 维度 | cellclaw | OmicsClaw | 谁更优 |
|---|---|---|---|
| 装配形态 | 线性 `_build_messages`（9 段，4 段死码） | 注入器 + placement（+ 一次被丢弃的重复装配，见 F3） | **可读性 cellclaw**；扩展性/多 surface OmicsClaw |
| 预算单位 | **token**（tiktoken，按模型 encoding） | **字符**（`estimate_message_size`，`len(str)`） | **cellclaw**（token 更准） |
| 预算依据 | `effective = window − reserved − margin`（按模型 token） | `min(96000, window×3.0×0.5)` 字符（按窗口推导 + 全局 chars/token 常量 + 封顶） | 平 / cellclaw（见 §3 F1） |
| 预算状态 | 五级枚举 + `target_reduction` + block 门 | 无（估算 char + applied_stages + toast） | **cellclaw** |
| 驱逐/压缩 | 单级 `pop(0)` 整条丢（有孤儿风险） | 五级渐进（snip→micro→collapse→auto→reactive），block-aware | **OmicsClaw** |
| 摘要 | LLM（无损优先 + 校验关键路径） | 确定性抽取式模板（无 LLM、无校验） | **cellclaw**（保真）→ **拟采纳**（决策 1，§9.1；确定性作 fallback） |
| 摘要落点 | user turn / 折进 history（缓存友好） | **提进 system 串**（缓存不友好，见 F2） | **cellclaw** |
| 缓存框架 | 一处注释 + 隐式；工具每轮重建且进 system | ADR 0024 三区 + 诊断 + 工具按 surface 冻结 | **OmicsClaw** |
| 装配位置 | web 入队 thin payload → worker 重装配（队列解耦） | dispatch 内联、进程内、每 surface | **cellclaw**（规模解耦）；OmicsClaw（桌面/CLI 够用） |
| 历史存储 | 原始历史存 **DB**，worker 每轮 rehydrate | 原始历史 **进程内内存**（LRU 不落盘）；蒸馏记忆存 sqlite | 各有取舍（见 F14）：OmicsClaw 内存 append-only 利于缓存，但无重启 durability |
| 多模态预算 | 文本 only（无多模态诉求） | 文本 only，但要过组织切片图 → 预算盲区（见 F4） | 平（对 OmicsClaw 是缺口） |
| 并行装配 | 未见 | memory/skill/capability 并发拉取 | **OmicsClaw** |

---

## 3. 发现（按严重度排序 · 现象 → 证据 → 影响 → 修复）

### F2 ✅（高）compaction 摘要 canonicalization 不一致 → 一次压缩两次 re-warm 〔已完成 `ccc0b05`〕
- **现象**：ADR 0024 **允许** collapse 把摘要折进 system 作为**一次 deliberate re-warm**（`docs/adr/0024-prompt-prefix-caching.md:130-134`）。真正的问题是实现产生**两种不一致的 system 形态**：压缩**当轮**追加 `## Context Collapse` / `## Auto Compacted Context`，持久化时 `_combine_persisted_summaries` 写成 `### heading`，**下一轮**再 hoist 成 `## Persisted Compacted Context` → **同一次压缩造成两次 `system-changed` re-warm**，而非 ADR 设想的一次。此外 snip 就地改写历史 tool 参数（见 F11）也侵蚀 append-only。
- **证据**：`compaction.py:475-484`（下一轮 hoist 为 `## Persisted Compacted Context`）、`:552-578`（当轮 `## Context Collapse`/`## Auto Compacted Context`）、`:149-159` + `query_engine.py:618-633`（持久化写 `### heading`）；ADR 允许条款 `docs/adr/0024-prompt-prefix-caching.md:130-134`。（Codex 核实并纠正了初稿"直接违反 ADR"的过猛表述。）
- **影响**：对 DeepSeek 一个 miss token ≈ 10× hit token；长会话每次压缩多刷一次前缀，累积可观。**公允注**：cellclaw 把摘要放 user turn 只在 **system 段**上更优，其 `pop(0)` 同样破坏 **history 段**缓存——两者都非干净赢家，差异仅在 system 前缀这一段。
- **修复**（采纳 Codex 分阶段）：**第一步**统一当轮与持久化/hoist 的 canonical 摘要格式，保证**一次压缩只一次 re-warm**（低风险、直接见效）；**第二步**再评估把摘要移出 system（作 messages 首条 pinned message），注意 provider 对第二条 system message 的兼容性、以及"降为 user 会损其权威"的取舍。

### F1 ⏸/部分（中）char↔token 桥是单一全局常量，且 96000 封顶抹平大窗模型 〔96000 封顶已解决 `edc6c7e`→256000；chars/token 校准可选未做〕
- **现象**：预算按字符；`max_prompt_chars` **确按模型窗口推导**，但 token↔char 换算用**单一全局常量 `_CHARS_PER_TOKEN=3.0`**，且结果**封顶 96000**。
- **证据**：`resolve_max_prompt_chars`（`engine/loop.py:181-195`，调用 `:400`）= `min(_DEFAULT_MAX_PROMPT_CHARS=96000, get_context_window(model) × 3.0 × 0.5)`，env `OMICSCLAW_MAX_PROMPT_CHARS` 可覆盖；窗口表 `providers/models.py:105-148,212`（默认 DeepSeek 系为 **1,000,000** token）；字符估算 `budget.py:9-39`。
- **影响**（均为标定问题，非 bug）：
  1. **单一 3.0 chars/token 不反映内容语言**：英文实际 ~4 char/tok、中文 ~1.5–2.5。用 3.0 换算 → 触发点**英文偏早**（真实占比低于设定的 50% 目标就压缩）、**中文偏晚**（真实约 75% 才压）。对一个明确服务中文用户的工具，压缩触发点随语言漂移；小窗模型 + 中文时更靠近撞窗、回落 reactive 兜底。
  2. **96000 封顶抹平大窗**：默认 DeepSeek 窗口 1,000,000 token，`window×1.5=1.5M` 字符恒被压到 96000 → 无论窗口多大，实际工作预算 ≈ 96000 字符（~24–48k token，占 1M 窗口个位数百分比）。可能是**刻意的成本/延迟控制**（超大 prompt 又贵又慢），但也让"上下文窗口"与"实际使用量"解耦——为 1M 模型付费只用到 ~96k 字符。
- **修复**：主动阈值的 char↔token 换算按内容语言/类型校准（CJK 用更低 chars/token，或直接用 provider tokenizer 估 token）；把 96000 封顶做成**显式可配置的成本策略**而非隐式常量。cellclaw 的 `effective = window − reserved − margin` + tiktoken 是更 principled 的参照。
- **修正说明**：初稿据装配路径误判为"固定常量、不随模型"，深入取证发现 `resolve_max_prompt_chars` 的按模型推导，故严重度由高下修为中——保留此更正以示取证纪律。

### F3 ✅（中）双重 system 装配（可读性 + 无谓计算 + 迁移债）〔已完成 `1378370`+`19fcc55`〕
- **现象**：注入器算出的 `system_prompt` **被算出来又丢弃**，再由 legacy builder 重算一遍（内部**又跑一次** `assemble_prompt_context`）。
- **证据**：`assembler.py:488`（`system_prompt = prompt_context.system_prompt`）→ `:500-534`（`if system_prompt_builder is not None:` 用 legacy builder 覆盖，`run_engine_loop` 恒传 `build_system_prompt`）；`build_system_prompt`（`system_prompt.py`）内部再调 `assemble_prompt_context`。第一趟只有 `message_context` 和 knowledge 层被后续用到（`:524-529,544`），其 system 输出纯属浪费。
- **影响**：这正是"cellclaw 更清晰"的**实体来源**——一个没收尾的迁移：注入器模型与 legacy builder 并存，system 侧算两遍。认知负担 + 每轮多一趟装配。
- **修复**：收尾迁移——**单趟装配、按 placement 切分**、删掉 legacy 覆盖分支。
- **实现（2026-07-01 · TDD · 已过 codex，裁决 EQUIVALENT）**：research_stance 折进**单趟**请求（`assembler.py`）+ 引擎默认**不传** `system_prompt_builder`（`engine/loop.py`）→ 默认路径只装配一趟。legacy builder 块**暂留**给显式 custom builder（目前仅测试用），并加**字节等价 contract test**（`build_system_prompt(kwargs) == single assembly` 的 system_prompt）锁住不变量。**F3 收尾已完成（已 push commit `19fcc55`）**：8 个 fake-builder 测试改为直接断言真实 `AssembledChatContext`（system 层→`system_prompt`、message 层→`message_context`、标量→`request.*`），彻底删 `system_prompt_builder` 参数 + `_invoke_legacy_prompt_builder` + `import inspect` + legacy 覆盖块（共 56 行），字节等价的 2 个不变量测试保留。

### F4 ✅（中）多模态负载对预算近乎隐形 〔已完成 `2db1ec8`〕
- **现象**：`estimate_message_size` 只累加顶层 `text/type`；嵌套图像块（`image_url` 内联 base64）几乎不计入（约 9 字符）。
- **证据**：`budget.py:13-23`（content 为 list 时只取 block 的 `type`+`text`；图像块无 `text` 键）。
- **影响**：Channels 会把组织切片照片路由进分析（见 `CLAUDE.md` 频道说明），内联图像可能极大，但预算视其为 ~0 → 低估 → 撞窗 → 落到 reactive 兜底。
- **修复**：为图像/附件块加固定 token/char 附加量（按图数或分辨率），并递归计入嵌套内容。

### F5 ✅（中）缺显式预算 status / block 门 〔观测态已完成 `45eb3dc`+`dea4d90`+`72f140f`；硬 BLOCK=slice4 暂缓〕
- **现象**：输入侧只有 `estimated_chars + applied_stages + toast`，无 cellclaw 的 `OK/WARNING/COMPRESS/CRITICAL/BLOCK` 决策枚举与 `target_reduction`。
- **证据**：`compaction.py:583-592`（只返回 `estimated_chars/applied_stages`）；对比 cellclaw `ContextStatus`（`model_context.py:11-18`）+ `_maybe_compress_context` 按目标 75% 压（`meta_agent.py:1200-1207`）。
- **影响**：只能静默降级——压缩用固定 preserve 计数、不朝 token 目标收敛；agent/surface 无法在"94%、正在压到 75%"这种显式信号上分支（提示用户、强制总结、开新 thread）。
- **修复**：从 `prepare_model_messages` 暴露预算 status 枚举 + 目标削减量，供上层决策（对齐 cellclaw evaluator）。

### F6 ✅（中 · 用户已定向采纳 LLM 摘要）确定性摘要保真不足 〔已完成 `c3bf709`，opt-in，codex 3 轮 VERIFIED〕
- **现象**：collapse 摘要是确定性模板，非 LLM，无"关键产物是否保住"的校验。
- **证据**：`compaction.py:340-389`（`_collect_role_highlights` 抽取式）、`:409`（"No LLM call"）；对比 cellclaw `compression_engine.py`（无损优先 → LLM episode 摘要 → `validate_summary` 校验文件路径/TODO）。
- **影响**：确定性/零成本/低延迟（热路径优点），但一次大 collapse 会把大量推理压成骨架摘要、细节流失。
- **修复（用户决策 1）**：**采纳 cellclaw 的 LLM 浓缩总结**——collapse/大丢弃时走"无损优先 → LLM episode 摘要 → 校验关键产物"，**确定性模板降为 fallback**（LLM 不可用/超时/校验失败时）。仅在低频 collapse 触发、不在每轮热路径调 LLM。落地草图见 §9.1。

### F7 ⏸（低 · 架构轴 · 规模化才做）内联装配、无 thin/fat 队列解耦
- **现象**：chat 装配在 `dispatch()`/loop 内联、进程内、每 surface 各跑。
- **证据**：`dispatcher.py:60`、`engine/loop.py:215` 同步在请求路径装配；对比 cellclaw web 入队 thin payload → worker 重装配（`chat_dispatch.py:432` → `stateless_worker.py:1676`）。
- **影响**：桌面/CLI 完全够用；但高并发 web ingress 会把请求延迟耦合到装配开销。（`omicsclaw/remote/jobs` 另有 job/worker 面，但 chat 主路仍内联。）
- **修复**：仅当要做高并发托管时，考虑把重装配下沉到 worker，ingress 只入 thin payload。**当前不必动**，标注为规模化选项。

---

## 3b. Codex 交叉审核补充的发现（F8–F13）

> 以下 6 条由 Codex(gpt-5.5 xhigh) 独立核对 OmicsClaw 源码后发现，初稿未覆盖。

### F8 ✅（已证伪 · 保留为前瞻护栏）extension_prompt_packs 在 system —— 当前无 churn 〔护栏注释已 push `b4f3872`〕
- **原始担心**：`assemble_chat_context` 把 `query` 传给 prompt pack loader（`assembler.py:386-398`），layer 又固定 `system` placement（`layers/__init__.py:811-816`）→ 疑似 system 前缀随 query churn。
- **证伪（2026-07-01 取证）**：`load_prompt_pack_runtime_context`（`extensions/runtime.py:1178`）**第一行就 `del surface, skill, query, domain`**，docstring 明说"当前实现激活所有已启用的本地 pack"，内容**与 query 无关、会话内稳定**。故放在 system 是**正确且缓存友好**的；移到 message 反而丢缓存、是回归。codex 看到 query 被传入即推断相关，漏了 loader 把它 `del` 了。
- **前瞻护栏（已落地）**：在 `del ...` 处加注释——**若将来实现 query/skill/domain 过滤（docstring 的 "future filtering"），必须同时把该 layer 移到 `message` placement**，否则前缀 churn。当前无需改代码。

### F9 ✅（中）并发装配无 pending-task 清理 → 取消时后台任务泄漏 〔已完成 `3ba9449`，codex CORRECT〕
- **证据**：`assemble_chat_context` 多处 `asyncio.create_task()`（`assembler.py:342,355,398,408,438`），无 `finally` 取消未 await 的任务。
- **失败场景**：用户取消/断连时，`to_thread` 的 capability / scoped-memory / prompt-pack 仍跑完，占资源、写日志。
- **修复**：`TaskGroup` 或 `finally` 里 cancel 未完成任务。

### F10 ✅（中 · 根因修 + 后续均已做，已 push `7c29f88`+`5e18e8d`）sanitize_tool_history 连"已成功的" tool 结果一起丢弃不完整 bundle
> **已解决（2026-07-02）**：① **根因修**（commit `7c29f88`）——查明「引擎把 tool 记在 assistant 之后」其实是主循环**双重 append** assistant-with-tool-calls（1459+938），sanitize 丢带内容的那个副本;修法 = `_execute_planned_tool_calls(append_assistant=False)` 主循环不再重复记（见 [[engine-tool-call-recording-order]]）。这**除掉了下面「实现受阻」记录里的 confound**（人为不完整 bundle）。② **后续修**（TDD + codex，Option 1）——`sanitize_tool_history` 从「整块丢」改为「修复」：保留 assistant + 已成功结果 + 按 tool_calls 声明顺序为缺失 id 补**确定性占位**（去重 by id），**幂等**（占位下轮满足 pending → 字节不变，写回 store 也 OK）。以下为原始记录。

- **证据**：`transcript.py:10-52`（不完整多工具 bundle 整块丢）；`prepare_history` 把 sanitized 结果**写回 store**（`transcript.py:466-481`）；测试锁死此行为（`tests/test_transcript_store.py:22-43`）。
- **失败场景**：assistant 发两个 tool_calls，一个结果已落库、另一个因取消/异常缺失 → 下一轮把**已成功**的结果也丢了，模型失去真实观察，且写回后不可逆。
- **修复**：请求期临时补合成占位、保住已成功结果——但**不能**简单改 `sanitize_tool_history`（见下受阻记录）。
- **实现受阻（2026-07-01 TDD，已回退）**：把 sanitize 改为"补占位"会回归——① `prepare_history` 用 `history[:] = sanitizer(...)` **原地写回 store**，请求期修复会污染持久存储；② 引擎会把**工具结果记录在下一个 assistant 之后**（插桩实测 sanitize 输入为 `[user, A(call-x), A(done), tool(call-x)]`），连续-bundle 的 sanitize 视其"被打断"→补占位并把真实结果当 orphan 丢/重复,回归 6 个 query_engine 测试。**正确修法是更大的设计**：repair 必须**请求期临时化（不写回 store）** + 处理**错位工具结果**（先厘清引擎为何把 tool 记录在 assistant 之后）,而非 sanitize 小改。

### F11 ✅（中 · 已修，已 push `4de8794`）snip 改写历史 assistant 的 tool_call arguments
- **证据**：`_apply_snip_compaction` 截断旧 `function.arguments`（超 `snip_tool_argument_chars=1200`）。
- **失败场景**：截断 JSON 参数 → **非法 JSON 每轮重发给模型**（一旦某历史 tool call 参数 >1200 字符）；且同轮 collapse 触发时 `_persist_prepared_compaction` 经 `replace_history` 把截断参数**写回 store**（`[len-16,len-4)` 窗口既截断又持久化）→ 永久污染 append-only 历史。与 F2/F10 同源。
- **修复（已做）**：删掉 `_apply_snip_compaction` 的 tool_calls 截断块 + 删 `snip_tool_argument_chars` 配置字段（grep 确认无其它引用,构造点全 keyword)。零能力损失:snip 仍压 message content + tool 结果(micro),超大 tool call 由 collapse 阶段折叠(正确的地方)。测试翻为断言参数逐字保留 + 仍是合法 JSON。经 4-agent 评估 workflow 确认 L 风险。

### F12 ⏸（低-中 · 触发式暂缓）缓存诊断归因粒度不足
- **证据**：真实 `create()` 参数含 `model/max_tokens/messages/tools/**kwargs`（`query_engine.py:1197-1203`），诊断只 hash tool/system（`:1337-1345`、`cache_diagnostics.py:147-176`）。
- **失败场景**：provider 参数 / base_url / TTL / 模型端策略导致的 miss 被粗归为 `history-shifted`，掩盖真实成因。
- **修复**：诊断纳入 model/max_tokens/kwargs 指纹与 provider 元信息。

### F13 ⏸（低 · near-free · ADR doc-note 暂缓）system_prompt_append / mode / stage 是 sanctioned re-warm 入口
- **证据**：`system_prompt_append` 来自 Desktop request（`server.py:569-587,1969-1985`）→ 追加 system（`engine/loop.py:82-85,314`）；`mode`/`stage` 也追加 system（`:73-79,158-169,315-318`）；`stage` 还**子过滤工具列表**（`registry.py:139-147`）——即"工具按 surface 冻结"有 stage 例外。
- **说明**：均为有意设计，但应**显式登记为 sanctioned re-warm 点**，而非默认前缀恒稳；诊断对其触发的 miss 打白名单标签。

---

## 3c. 追加发现（用户提问触发 · transcript 持久化）

### F14 ⏸（中 · 视部署目标 · 暂缓）原始 transcript 仅进程内内存，重启/多进程即丢
- **现象**：逐轮原始对话历史存于进程内单例 `TranscriptStore`（内存字典 `messages_by_chat[chat_id]`，LRU 上限 `MAX_CONVERSATIONS`），**不落盘、不跨进程**。重启后仅蒸馏记忆（`memory_context`，sqlite）与工具结果（`ToolResultStore` storage_dir）存活，**原始逐轮历史丢失**；桌面 `/chat` 也不接收客户端回传历史（后端内存是唯一真源）。
- **证据**：`runtime/storage/transcript.py:373`（docstring "In-memory transcript store"；`messages_by_chat` `:388`；LRU `evict_lru_conversations` `:419`；append-only `prepare_history` `:466`）；单例 `runtime/agent/state.py:297`；持久层仅在 `memory/`（`database.py` + `migrations/` 建表）与 `ToolResultStore`（`state.py:304`）。
- **失败场景**：① Channels(IM 多用户) 后端重启/发版 → 所有在聊会话原始上下文丢，用户续聊但 bot"忘了"最近 N 轮（只剩蒸馏记忆）；② 活跃 bot 会话数超 LRU 上限 → 暂时空闲但仍进行中的会话被悄悄淘汰；③ 多进程（channels + desktop 同逻辑用户）transcript 不共享。
- **对照 cellclaw**：cellclaw 把原始历史存 **DB**、stateless worker 每轮 rehydrate（`worker/stateless_worker.py:1676`），天然重启 durable + 可横向扩展；但那是为 worker 集群设计，且**每轮 fetch 与 prefix caching 冲突**（见 §9.4 为何不照抄）。
- **修复**：write-through 持久日志 + 冷启动 lazy-rehydrate（**不**每轮 fetch），保 ADR 0024 字节稳定。设计见 §9.4。
- **优先级**：视部署目标——unified-platform / 多用户 channels 要"重启不失忆"则值得（按需 P1）；单用户桌面且 App 自持历史则低。

---

## 4. OmicsClaw 已领先之处（保持平衡）

1. **缓存三区框架（ADR 0024）**——system/message 按 placement 分级、工具按 surface 冻结、逆转每轮工具压缩：整体领先 cellclaw（后者工具每轮重建且进 system）。
2. **缓存可观测**——`compute_segment_hash / infer_miss_reason` 把 miss 归因到 `tool-list-changed / system-changed`；cellclaw 无。（注：归因不含 model/max_tokens/kwargs/base_url，粒度偏粗——见 F12。）
3. **block-aware 历史裁剪**——tool 捆绑不拆（`budget.py:42-107`）；cellclaw `pop(0)` 逐条丢**有孤儿 tool_result 风险**（丢了带 tool_calls 的 assistant、留下其 tool 结果 → OpenAI 报错）。
4. **五级渐进压缩**——snip→micro→collapse→auto→reactive，比 cellclaw 单级 `pop(0)` 更细腻。
5. **并行异步装配**——多路 context 并发拉取。（注：无统一 gather/cancel cleanup——见 F9；credit 打折。）
6. **确定性摘要**——热路径零 LLM 成本/延迟/不确定性（是 F6 的另一面，取舍而非纯缺陷）。

---

## 5. 优化建议（按优先级 · 可落地）

> 用户 2026-07-01 定向的三条——**LLM 浓缩摘要（决策 1）/ 可选 section 扩展（决策 2）/ cellclaw 预算控制模型（决策 3）**——已提升优先级并给出设计草图，见 §9。

**P0（先做，性价比最高）**
- **[F2] 统一摘要格式 → 消除双 re-warm**：先把当轮 collapse 摘要与持久化/hoist 的摘要统一成同一 canonical 格式（保证一次压缩只一次 re-warm）；再评估移出 system（pinned message，注意 provider 兼容/权威性）。收益：压缩不再多刷前缀；与 ADR 0024 自洽。
- **[F4] 多模态计入预算**：递归计入 `image_url` data URI，或按图数/尺寸估固定量。低成本高确定性（Codex 建议前移）。
- **[F8] 已证伪**：prompt pack 内容与 query 无关（loader `del` 掉 query），system placement 正确、缓存友好；仅加了前瞻护栏注释（将来加过滤才需移 message）。详见 §3b。
- **[F10 · 需专项设计，非小修]** 保住已成功 tool 结果：repair 必须请求期临时化（`prepare_history` 不写回）+ 处理错位工具结果。2026-07-01 的"改 sanitize 补占位"尝试已回退（回归 query_engine，详见 §3b F10 受阻记录）。

**P1（含用户定向，设计详见 §9）**
- **[F1+F5] 采纳 cellclaw 预算控制模型（决策 3）**：token 化 `effective_capacity`（复用 `get_context_window`）+ 五级 status + `target_reduction` 驱动压缩深度；分阶段"先观测态、再目标压缩、最后硬 BLOCK"。详见 §9.3。
- **[F6] LLM 浓缩摘要（决策 1）**：collapse/大丢弃时走 cellclaw 三级（无损→LLM→校验），确定性模板作 fallback，仅低频触发、不在每轮热路径调 LLM。详见 §9.1。
- **[扩展] 可选 section/slot（决策 2）**：新增 `user_profile`（→system 稳定）/ `project_state`（→message 易变）注入器。详见 §9.2。
- **[F3] 收尾装配迁移**：单趟按 placement 切分、删 legacy builder 覆盖（保留 message_context / knowledge layer / research stance）。
- **[F9] 并发装配加 cancel cleanup**；**[F11] snip 不改写 call 参数**。

**P2**
- **[F7]（规模化才做）** thin/fat 装配下沉 worker。
- **[F14]（多用户/重启 resilience 才做）** transcript write-through 持久日志 + 冷启动 rehydrate（**不**每轮 fetch，保 ADR 0024）；详见 §9.4。

---

## 6. cellclaw 值得借鉴 vs 不必照搬

**借鉴（含用户 2026-07-01 定向，设计见 §9）**：
① **上下文预算控制模型**（决策 3）——token 化 `effective_capacity` + 五级 status + `target_reduction` + 压到目标；用户认为这套"非常清晰"，采纳其**控制平面**（但保留 OmicsClaw 的 block-aware 裁剪 / append-only，不照搬 `pop(0)` 驱逐）。
② **LLM 无损优先摘要 + 校验**（决策 1）——采纳 cellclaw 三级（lossless → LLM → validate），确定性模板降为 fallback。
③ **可选 section/slot（user_profile / project_state）**（决策 2）——作为**扩展位**采纳其槽位设计，但用 OmicsClaw 注入器实现（比硬编码 sections 更适合），按 placement 分级避免缓存回退。
④ 摘要作为 **message**（而非进 system）——OmicsClaw 反而更该学这条（见 F2）。

**不必照搬**：① `pop(0)` 逐条驱逐——有 tool_result 孤儿风险，OmicsClaw 的 block-aware 更安全。② 工具每轮 `to_schemas()` 重建 + 进 system——缓存不友好，OmicsClaw 按 surface 冻结更好。③ cellclaw 无系统化缓存框架——OmicsClaw 的 ADR 0024 是净胜。④ cellclaw sections 里 **4 段目前是死代码**——借鉴的是**槽位设计**（决策 2），不是照抄空实现（要么填活、要么不引入）。⑤ **每轮从 DB rehydrate 历史**——与 prefix caching 冲突（见 F14/§9.4），OmicsClaw 内存 append-only 更优；但其"持久化 + 冷启动 rehydrate"的**思路**值得借鉴（write-through）。

---

## 7. 待验证 / 开放问题（诚实标注不确定性）

1. **F1 的 chars/token 标定**：`resolve_max_prompt_chars` 已确认按模型窗口推导（`engine/loop.py:181-195`），全局常量 `_CHARS_PER_TOKEN=3.0`。落地校准前应对 OmicsClaw 默认模型 + 中文/JSON 混合内容抽样测真实 chars/token。〔**96000 封顶问题已定案（2026-07-02）**：3-agent workflow + git 考古证实它是 **legacy**（`48fe581` 无注释默认值、= 整 32k token、从未标定；ADR 0024 只是包成安全天花板且其 window-derived shrink 对全 fleet 死代码）。已改为单一常量 `DEFAULT_MAX_PROMPT_CHARS=256000`（`compaction.py`，dataclass 默认 + `resolve_max_prompt_chars` 共用），`min(256000,window×1.5)` 顺带激活小窗 window-relative 分支；ratio-invariant 故 F2/§9.3 不受影响，reactive-413 兜底，env 可覆盖。理由：prefix caching 下抬高主要是便宜的 cache-hit 计费 + 几乎不增延迟 + 更少 re-warm，换回长会话能力。F1 的 chars/token 校准（CJK 漂移）仍是独立可选 follow-up。〕
2. **F2 的缓存实测**：应查 `cache_diagnostics` 遥测确认——压缩事件后是否确有 `system-changed` 类 miss 尖峰？（有诊断数据即可证伪/证实。）
3. **cellclaw 死码**：`relevant_memories/dialog_summaries` 等仅测试传入的判断基于 grep，可能有其它入口（ACP/CLI）未覆盖。
4. **F7**：`omicsclaw/remote` 是否已对某些路径做了 worker 下沉，需再核。

---

## 8. Codex 交叉审核（gpt-5.5 · xhigh）

> 由 `codex exec -m gpt-5.5 -c model_reasoning_effort=xhigh --sandbox read-only` 对本文档 + OmicsClaw 源码交叉审核（消耗 ~328k tokens）。以下为裁决与**据此对本文档所做的修订**。

### 8.1 逐条裁决（F1–F7）
| 发现 | Codex 裁决 | 严重度 | 处理 |
|---|---|---|---|
| F1 字符预算 | **PARTIALLY**——"固定常量"不成立，`resolve_max_prompt_chars` 按模型推导（`engine/loop.py:186-195,398-401`）；DeepSeek 现为 1M 窗口 | 中 | 与审前自我更正一致，已下修+重写 |
| F2 摘要进 system | **PARTIALLY / 升级为高**——代码属实，但"直接违反 ADR"过猛：ADR 0024:130-134 允许 collapse→system 作一次 deliberate re-warm。真问题是格式不一致致**双 re-warm** | **高** | **已重构 F2** |
| F3 双重装配 | **CONFIRMED**（`assembler.py:466-534` + `system_prompt.py:41-70` 内部再装配一次） | 中 | 保留 |
| F4 图像预算隐形 | **CONFIRMED**（附件生成 base64 data URI `_attachments.py:191-197`） | 中 | **提升至 P0** |
| F5 无 status/block | **CONFIRMED**（`compaction.py:79-86,583-601`） | 中 | 保留；改"先观测后 block" |
| F6 抽取式摘要 | **CONFIRMED**（`compaction.py:340-410` "No LLM call"） | 中 | 保留；LLM 作可选路径 |
| F7 内联装配 | **CONFIRMED**（`dispatcher.py:59-143`、`engine/loop.py:283-306`） | 低 | 保留 |

**Codex 额外事实核对**：placement 中 `knowhow_constraints` 实为 `message`（§2.2a 原文已正确）；主动/被动压缩描述正确（主动 `compaction.py:532-581`、reactive `query_engine.py:1211-1229`）；`memory_context` 的 `load_context` 确不传 query（`assembler.py:337-340`）。

### 8.2 Codex 补充的发现 → 已并入 §3b（F8–F13）
F8（高·prompt pack 前缀 churn）、F9（中·并发无 cancel cleanup）、F10（中·sanitize 丢成功 tool 结果）、F11（中·snip 改写历史 call 参数）、F12（低-中·缓存归因偏粗）、F13（低·append/mode/stage 是 sanctioned re-warm 且 stage 子过滤工具）。详见 §3b。

### 8.3 Codex 对建议的批判（已采纳进 §5）
- **F2 分阶段**：先统一摘要 canonical 格式（一次压缩一次 re-warm），再评估移出 system——直接改 pinned message 有 provider 兼容/权威性风险。
- **F1 非最高 P0**：主链路已按模型收缩预算 + env override；先加 CJK/JSON/image 倍率 + provider-usage 离线校准，不必全量接 tokenizer。
- **F5 先观测后 BLOCK**：别在 char 估算上硬 BLOCK。
- **F4 前移**（低成本高确定性）；**F6** LLM 摘要不默认热路径，做成 `/compact --deep` 可选。
- **F3 收尾迁移**注意保留 `message_context` / knowledge layer / research stance 行为，勿绕过 custom injectors。

> **用户定向（§9）在此之上调整了取舍**：决策 1（LLM 摘要）与决策 3（cellclaw 预算控制）优先级高于 codex 的保守排序；但 codex 的稳妥约束——LLM 仅低频 collapse 触发 + 确定性 fallback、预算"先观测态 → 后目标压缩 → 最后才硬 BLOCK"——已**内建**进 §9 的落地顺序，二者不冲突。

### 8.4 公允性修订（Codex 指出）
- F1（"provider-blind fixed 96000"主链路已解决）、F2（"直接违反 ADR"夸大）措辞已收窄。
- "并行装配"表述已打折（C5 注 F9）；"cellclaw 摘要落 user turn 守住前缀"已加公允注（cellclaw `pop(0)` 同样破坏 history 段缓存，两者都非干净赢家）。
- 缓存可观测（C2）补注归因不完整（F12）。

### 8.5 Codex 一句话总评
> "整体方向可靠，但 F1 和 F2 的措辞需要收窄；最优先落地的是 **compaction summary 的 canonicalization/placement 修复**，其次是 **多模态预算计入**。"

### 8.6 残留分歧 / 局限
- Codex 无法访问 cellclaw 源码，§2.1 的 cellclaw file:line 仅由本方独立通读核实（Explore agent），未经第二方代码级复核。
- F8/F10/F11 的**触发频率**（进而实际影响）尚无遥测；建议用 `cache_diagnostics` + 一次真实长会话回放实测（见 §7）。

---

## 9. 采纳方向 · 设计草图（用户 2026-07-01 定向）

> 三条为用户产品决策。codex 的稳妥约束（LLM 仅低频触发 + fallback、预算先观测后 BLOCK）已内建进各自落地顺序——是在 §8 基础上**按用户偏好上调优先级**，非推翻。

### 9.1 采纳 cellclaw 式 LLM 浓缩摘要（决策 1 · 重塑 F6） ✅ 已实现（`c3bf709`）
> **落地实况（2026-07-02，与下方草图的差异）**：采用**方案 A**（LLM 调用内置 `_collapse_with_target`，仅 target-active collapse/auto 触发；`/compact` + reactive-413 结构上保持确定性）。三层 = tier-1 lossless skip（`llm_summary_min_omitted`）/ tier-2 bounded LLM（`asyncio.wait_for`）/ tier-3 校验。**关键与草图不同点**：F2 承重机制是**长度上限**——LLM 摘要被 cap 在模板长度（code points **且** UTF-8 bytes），使 LLM 路径被确定性模板路径**支配**（`len(chosen)≤len(template)` → 重挂载的 system prompt 不大于模板已产出的 → N+1 不会新越 collapse trigger）；另加结构性 tool-call 检测反 mimicry。默认 **OFF/opt-in**（`collapse_llm_summary_enabled`）。codex 3 轮 VERIFIED。以下为原始设计草图。
- **目标**：collapse / auto / reactive 真正丢弃大量历史时，用 LLM 生成高保真摘要，替代当前纯抽取式模板。
- **cellclaw 参照**（`memory/compression_engine.py` 三级）：`LosslessReducers`（压工具输出、折叠重复日志/观察、截超长，**保留文件路径/标记**）→ `_llm_summarize_episode`（分 episode，带 `_fallback_summarize_episode` 确定性兜底）→ `validate_summary`（校验关键文件路径/TODO 未丢）。
- **OmicsClaw 落点**：把 `_build_collapse_summary`（`compaction.py:340-389`）从单层抽取式改为三层——
  1. **无损优先**：现有 snip/micro 已是 lossless-ish，保留为第一层；
  2. **LLM 摘要**：`omitted_count/omitted_chars` 超阈值时，对 `omitted_history` 调 LLM（复用会话 `deps.llm`，或可配置的更小 summarizer 模型）产结构化摘要（user goals / tool refs / workspace 产物 / pending plan）；
  3. **校验**：生成后检查关键工件（文件路径 / workspace 产物 / plan）是否仍在摘要中，缺失 → 重试或回退确定性模板。
- **约束（内建 codex 稳妥）**：不在每轮热路径调 LLM（snip/micro 仍无 LLM）；确定性模板恒作 fallback，保成本/延迟下限；与 F2 协同——摘要 canonical 化后再叠加 LLM 内容，保证一次压缩仍只一次 re-warm。
- **F4 联动（codex F4 审核指出，本项承接）**：`_flatten_message_content`（`compaction.py:101`）当前忽略图像块 → collapse 后摘要不留"此处曾有图"痕迹 → 对图失忆；F4 让图像更频繁被 collapse，放大此问题。本设计的摘要（含确定性 fallback）应至少为图像块留 `[image]` marker，校验层确认图像存在被记录。

### 9.2 memory_context placement 拆分（决策 2 · 重塑 §6） ✅ 已实现（2026-07-02，未提交，codex runtime CORRECT）
> **落地实况（2026-07-02）**：采纳"拆分而非加空槽"。**验证先行**：`session.py:load_context` 把 5 类记忆装单块进 system 层,且 dataset/analysis(skill 运行)、preference/insight/project(agent 记忆工具)**均会 mid-session 写入** → `assemble_chat_context` 每 user turn 重算 → 写入后 system 前缀 churn（TDD 实证:analysis status running→complete 改变了 system_prompt）。**§2.2a 原判"会话级稳定"只对 query-无关成立,漏了 write-volatility**。**实现**：`session.py` 抽 `_load_memory_blocks` + `load_context_layers()->(stable,volatile)`(`load_context` 字节不变,codex 1024 组合 mismatches=0);**stable=project_context+preference→system `## Your Memory`**(durable identity,cache-warm+authoritative)、**volatile=dataset+analysis+insight→message `## Current Work State`**(`project_state_context` injector,order 44,placement=message);assembler `hasattr(load_context_layers)` fallback 保 legacy 字节等价;更新 ADR 0024 §3 + CONTEXT.md 消除文档漂移(codex P2)。**不要**照下方原草图加空 `user_profile`/`project_state` 注入器——数据已注入,空槽=死码(§244④)。以下为原始草图。
- **目标**：把 cellclaw 的 `user_profile / project_state` 作为**可选上下文槽位**引入（用户明确的扩展方向），而非当死代码回避。
- **OmicsClaw 优势**：不必照抄 cellclaw 硬编码 sections——**注入器模型天生适合**：每个新槽位 = 一个 `ContextLayerInjector`（`layers/__init__.py`），`applies()` 决定何时出现、`render()` 产内容、`placement` 决定去向；槽位空 = 无层 = byte-identical，不动现有默认。
- **具体设计**：
  - `user_profile` injector：来源接 long-term memory / 用户设置；**`placement=system`**（会话级稳定 → 缓存安全，order≈38，介于 persona 与 memory_context 之间）。
  - `project_state` injector：来源接 workspace/pipeline 进展；**`placement=message`**（随分析推进而变 → 按 ADR 0024 走 volatile 避免刷前缀；与 cellclaw 把 state 塞 user turn 的直觉一致，`meta_agent.py:645-650`）。
  - `ContextAssemblyRequest`（`assembler.py`）加 `user_profile / project_state` 字段；`assemble_chat_context` 并行装配阶段拉取（复用 `create_task`，但补 F9 的 cancel cleanup）。
- **收益**：为"记住用户是谁 / 项目到哪"打好可选、可关、缓存友好的扩展位；缓存纪律不倒退。

### 9.3 采纳 cellclaw 式预算控制模型（决策 3 · 合并 F1+F5）
- **为何学**：cellclaw 把"还剩多少窗口 / 该不该压 / 压到哪"变成显式可决策信号——正是 OmicsClaw 输入侧缺的"控制平面"。
- **移植三件套**：
  1. **token 化 `effective_capacity`**（治 F1）：`effective = get_context_window(model) − reserved_output − safety_margin`（cellclaw `model_context.py:32-38`；OmicsClaw 已有 `get_context_window` `providers/models.py:212`）。把 `resolve_max_prompt_chars` 的字符封顶升级/并存为 token 预算。
  2. **五级 status + 阈值**（治 F5）：`OK<65% / WARNING<80% / COMPRESS<90% / CRITICAL<96% / BLOCK`（cellclaw `model_context.py:40-50`）；新增 evaluator 产 status + `target_reduction`。
  3. **压到目标**：现 collapse/auto 用固定 `preserve_*` 计数；改为按 status 的 `target_reduction` 驱动压缩深度（cellclaw `_maybe_compress_context` 朝 `effective×0.75` 收敛，`meta_agent.py:1200-1207`）。
- **状态外显**：经现有 `CompactionEvent`/SSE（`compaction.py:595-636`）把 status 暴露给 surface（"已用 82%，正压到 75%"），让 agent 据 CRITICAL/BLOCK 决策（提示 / 开新 thread）。
- **落地顺序（内建 codex 稳妥）**：① 先并存 token 预算 + 暴露**观测态**，不改压缩行为，用真实会话校准 char↔token（含 CJK/JSON/图像倍率，F1/F4）；② token 估算可信后切到 `target_reduction` 驱动；③ 最后才启用硬 **BLOCK**。
- **与缓存纪律协同**：只借鉴其**评估/状态/目标**这套控制平面；**保留** OmicsClaw 的 block-aware 裁剪 + append-only，不照搬 cellclaw 破坏 history 段缓存的 `pop(0)` 驱逐。
- **实现进度（TDD · codex CORRECT · 均已 push）**：slice 1（预算 status 原语 `ContextBudgetStatus`/`effective_context_capacity`/`classify_context_budget`，`budget.py`，`45eb3dc`）+ slice 2（观测态接进 `PreparedModelMessages`，引擎传 `get_context_window`，`dea4d90`）+ `CHARS_PER_TOKEN` 统一防漂移（`72f140f`）✅；**slice 3 压到目标压缩（`584f710`）✅**（`collapse/auto_compact_target_ratio` 0.55/0.40 收敛总量）；**B3 status→SSE（`e47bbcd`）✅**。**slice 4 硬 BLOCK 门：暂缓**（见下）。
- **⚠️ slice 3 关键设计要点（codex B1）**：**窗口相对** status 对大窗模型几乎恒 OK——字符预算已把上下文压到 `min(DEFAULT_MAX_PROMPT_CHARS, window×1.5)` 字符（2026-07-02 后 = 256000 ≈ 85k token ≈ 1M 窗口的 ~8.5%;仍远低于 65% WARNING 阈值,故窗口 status 依旧恒 OK,结论不变）。故 **slice 3 的压缩驱动必须用"相对 `max_prompt_chars` 的本地压缩压力"，而非窗口相对 status**（窗口 status 保留作溢出风险信号，或两者都报 pre/post-compaction 压力）。**B3（已完成，已 push `e47bbcd`）**：`budget_status`/`local_budget_status` 经 `CompactionEvent`→SSE payload 暴露（条件 key + `.value` 字符串 via `_budget_status_str`;桥接 `_coerce_compaction_event` 重建;两 emit 点从 `PreparedModelMessages` 转发）。纯读+通知,零 F2/缓存影响。

- **⚠️ slice 4（硬 BLOCK 门）已暂缓（用户定,2026-07-01）**：4-agent understand workflow 调查 + codex 交叉验证确认——**硬 refuse 不安全**（信 `CHARS_PER_TOKEN=3.0`/`_IMAGE_BUDGET_CHARS=4000` 粗估算,false-positive 会误拒可服务请求,劣于现有 reactive-413 兜底,= codex 初衷 §278）;**soft 代目标门安全但价值窄**（normal compaction 已 0.55/0.40,post-prepare `local BLOCK` 是边缘 case,且多为不可约内容——超大 system 或单条超大 newest block,任何压缩都缩不动）。保留 reactive-413 兜底 + B3 遥测,待 char↔token 校准（§248/§277）后再议硬门。**codex 前瞻建议**：真要做 slice 4,更高价值的安全形式不是「local BLOCK 时拒绝」,而是「更早压缩 / 更响的告警」——把 proactive 预算推导从 `resolve_max_prompt_chars` 的裸窗口分数改为绕 `effective_context_capacity(...)`(尤其小窗模型),`engine/loop.py:195` + `budget.py:33` + `query_engine.py:1215`。

### 9.4 transcript 持久化：write-through + 冷启动 rehydrate（F14 · 追加）
- **目标**：让原始逐轮历史在重启/换进程后可恢复、可选跨进程共享，**且不损 ADR 0024 的历史段缓存稳定**。
- **为何不照抄 cellclaw 的"每轮从 DB rehydrate"**：cellclaw stateless worker 每个决策回合 `_get_history_from_db(session_id)` 重建 message 列表（`worker/stateless_worker.py:1676`）。OmicsClaw 若照抄，每轮重建的序列化/排序/sanitize 差异会打断"prefix 后历史段字节稳定"→ 退回缓存 miss，**反噬刚做的 ADR 0024**（`prepare_history` 已特意去掉每轮滑窗）。cellclaw 无 prefix-caching 纪律，故不受此累；OmicsClaw 有，故必须区别对待。
- **设计（cellclaw 思路，OmicsClaw 化）**：
  1. **write-behind 持久日志**：给 `TranscriptStore` 的 `append_user/assistant/tool_message` 与 `replace_history` 加一层 append-only 落盘（复用现成 sqlite 栈 `memory/database.py`，或每 chat_id 一个 JSONL）；写在后台、不挡请求路径。
  2. **lazy rehydrate on miss**：`get_history(chat_id)` 在内存 miss（冷启动/换进程/LRU 淘汰后重访）时，**一次性**从持久日志载回内存，之后仍走内存 append-only——**不是每轮 fetch**。
  3. **真源仍是内存**：构建请求始终用内存 `prepare_history`，保证 ADR 0024 字节稳定；持久层只做 durability + 冷启动兜底。
- **与现有栈协同**：工具结果已落盘（`ToolResultStore` storage_dir），日志可只存 tool-ref（与 micro-compaction 的 tool-ref 一致）；LRU 淘汰改为"淘汰内存、保留磁盘"，重访再 rehydrate。
- **落地顺序**：① 先加 write-behind 日志（纯增益、不改读路径）；② 再加 miss 时 rehydrate；③ 需跨进程共享时才把日志后端换成共享 sqlite/DB。
- **不做**：不引入 per-turn DB 读；不把 transcript 塞进 prompt 缓存前缀之外的任何每轮可变位置。

---

## 10. 附录：关键 file:line 索引

**OmicsClaw**（行号 as-of `main@0fa9ae5` —— 本审计全部落地合并后）
- 分发中枢：`omicsclaw/runtime/agent/dispatcher.py:59`；核心循环 `omicsclaw/runtime/agent/loop.py:781`
- 装配主函数：`omicsclaw/engine/loop.py:243`（`run_engine_loop`；tools 按 surface 冻结 `:359`；`resolve_max_prompt_chars:199`）
- 层装配：`omicsclaw/runtime/context/assembler.py:263`（`assemble_prompt_context`）；`assemble_chat_context:286`（F3 后单趟、legacy builder 已删；F9 的 `_spawn`+`finally` reap）
- 注入器/placement/order：`omicsclaw/runtime/context/layers/__init__.py:1023`（`DEFAULT_CONTEXT_LAYER_INJECTORS`）；`ContextLayer:489`；决策2 新增 `project_state_context` injector（`placement="message"`、order 44）
- 压缩：`omicsclaw/runtime/context/compaction.py:833`（`prepare_model_messages`）；`ContextCompactionConfig:83`（`DEFAULT_MAX_PROMPT_CHARS=256000`、`collapse/auto_compact_target_ratio`）；persisted→system hoist `:859`；主动 collapse `:934`、auto `:957`；F6 `_summarize_episode_llm` / `_collapse_with_target`
- 预算（字符）：`omicsclaw/runtime/context/budget.py:102`（`estimate_message_size`，含 F4 `_IMAGE_BUDGET_CHARS`）；block 裁剪 `_group_history_blocks:140` / `trim_history_to_budget:159`；输出预算 `TokenBudgetTracker:223`；§9.3 status 原语 `ContextBudgetStatus` / `effective_context_capacity` / `classify_context_budget` / `local_budget_status`；模型窗口→char 预算 `omicsclaw/engine/loop.py:199`（窗口表 `omicsclaw/providers/models.py:105`、`get_context_window:212`）
- API 调用：`omicsclaw/runtime/agent/query_engine.py:1213`（reactive 兜底 `force_reactive_compact` `:1247`）
- transcript（F14）：`omicsclaw/runtime/storage/transcript.py:416`（`TranscriptStore` 内存单例）；`sanitize_tool_history` 修复不完整 bundle（F10，`_INTERRUPTED_TOOL_PLACEHOLDER`）
- ADR：`docs/adr/0024-prompt-prefix-caching.md`（§3 含决策2 refinement）

**cellclaw**（`/home/weige/project/repo_learn/cellclaw_source`）
- thin payload：`web/services/context_assembler.py:65`（`assemble_fat_payload:122`）；rehydrate `worker/stateless_worker.py:1676`
- 真装配：`agent/core/context_assembler.py:46`（驱逐 `:116-140`；`_build_messages:180-228`；快照 `:150-176`）
- 预算：`memory/context_budget.py:79`（`estimate_tokens:167`）；`agent/core/model_context.py:32-50`（effective + 五级阈值）
- 决策装配：`agent/meta/meta_agent.py:630-707`（缓存注释 `:645-650`；压缩 `:1159-1222`）
- LLM 摘要：`memory/compression_engine.py`（无损优先 → LLM → 校验）
