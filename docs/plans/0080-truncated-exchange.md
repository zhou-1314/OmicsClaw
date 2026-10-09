# 计划 0080：被输出上限截断的交换留下没被回答的工具调用，会话此后卡死

**状态**：已实现，待独立审核和 owner 下令合并（2026-10-09）。实现在分支 `fix/unanswered-tool-calls` 上，没有合并，没有 push；提交、与计划的差异和验证结果见 §13。计划是定稿的第 3 版：第 2 版（提交 `f317fa37`）经同一审核方复核，owner 2026-10-09 批准了计划，并就 Q1 至 Q10 作了裁定（§10），这一版把裁定和复核意见并进正文。§0 至 §12 是派发实现之前写的，没有随实现改动。

**基线**：`main` 的 `5daf5482`，行号以它为准，实施时按符号名重新定位。

**编号**：环节 C1 至 C9，入口 P1 至 P8，处理方式 X1 至 X5，位置 L1 至 L6，测试 T1 至 T23，变异 M1 至 M15（第 1 版自己的）、N1 至 N15（审核方第 1 轮的）、R1 至 R6（第 2 版新加的）、W1 至 W8（审核方第 2 轮的），真实请求 V1 至 V10，风险 K1 至 K10，问题 Q1 至 Q10（都已裁定）。实施记录（§13）另有测试 T24 至 T29、变异 S1 至 S13、真实请求 V11 至 V13。

**证据**：探针、原型和输出在 `/tmp/claude-0/-workspace-dataset-private-zhouwg-data-OmicsClaw/9885f10a-3799-4309-9dfb-0981c166422a/scratchpad/plan-0080/`，下面记作 `SCR`，第 2 版新做的以 `r2_` 开头，第 3 版新做的以 `r3_` 开头，清单见 §12。审核方的脚本和输出在同级的 `review-plan-0080/`，记作 `RV80`，只读引用；它第 2 轮的文件名也以 `r2_` 开头。第 1 版对真实 DeepSeek 接口发了 10 次请求，逐条记在 `SCR/logs/ledger.txt`，其中 2 次没有得到信息；第 2、3 版没有再发。审核方另发了 8 次（`RV80/logs/rv80_ledger.txt`）。本机没有 Anthropic 凭据，Anthropic 一侧的结论只有文档和本仓库适配器代码两种依据，文中逐处标明。官方文档是 2026-10-09 抓取的。更早的既有证据在 `/tmp/claude-0/-workspace-dataset-private-zhouwg-data-OmicsClaw/58ae2e35-0792-4055-933c-1cfb79c727a6/scratchpad/review-plan-0079/`，记作 `RV79`。各版之间改了什么见 §11。

## 0. 摘要

1. 引擎遇到被输出上限截断的回复时，把整条 assistant 消息记进轨迹，不执行它带的工具调用，然后结束这次运行。它的 docstring 写明：从这里接着往下走的调用方要自己回答或丢掉这些调用。entry 层是这个调用方，它两样都没做，把轨迹原样提交进 `session.history` 并存库。
2. 下一条用户消息排在那条没被回答的调用后面。DeepSeek 返回 400，这次交换失败，历史不变，所以这个会话之后每条消息都同样失败，重启后也一样。失去的是这段会话的上下文：`/new` 和 `/clear` 一直可用，工作区里已经写出的文件不受影响。低压力时压缩器不修配对；失败的交换不让历史增长，压力升不上去。用户在 CLI 上只看到 `Failed: ProviderError`。
3. 用真实 DeepSeek 走完了整条路径：把输出上限绑到 2,000，一次真实交换被截断在 `write_file` 的参数中间，存进 SQLite；今天的代码发下一条消息得到 400，原型发同一条消息得到 200。
4. 按现有数据，它在 owner 日常的配置下很难自然发生。OpenAI 方言默认不发 `max_tokens`，DeepSeek 文档写的默认值是思考模式 64K，实测一条回复写到 12,934 个输出 token 正常结束。主检出的会话库样本很薄（19 个有消息的会话，27 条用户消息），里面没有这种形状，最大的一条回复估算是 64K 的 8%。Anthropic 方言近得多：适配器对 `claude-haiku-4-5` 和输出上限表里没有的每个型号发的 `max_tokens` 都是 8,192。库里有一条回复估算过了 8,192 的一半；那是 DeepSeek 会话里的回复，模型和分词都不同，只能当估计。
5. 推荐做法没有变：每次交换开场时，从带进来的历史里去掉没被回答的工具调用，那一轮的文字留着；一轮既没有剩下的调用、也没有空白以外的文字时整轮去掉。不补任何结果，不加任何给模型看的文字。改动是 `omicsclaw/context/transcript.py` 的一个纯函数和 `omicsclaw/entry/turn.py` 的两处调用。
6. 已经卡死的会话不需要迁移：下一条消息或 `/compact` 都会经过同一处清理，成功后存回去的就是干净的历史。
7. 规则里有两处是第 2 版才写死的：同一个 id 出现多次时一条结果只回答一个调用；只含空白的文字不算文字。新测试 23 条（参数化后 36 个用例），在今天的代码上 34 红 2 绿，在原型上全绿。42 处定点变异里 40 处有测试转红，剩下 2 处在产品路径上和原型等价，审核方同意（§6.2）。现有测试要改 1 条。按 `SPEC.md` 属于第 3 档。
8. 这个做法不告诉模型和用户"回复被截断了"，今天也不告诉。审核方在真实 DeepSeek 上各取了 1 个样本：去掉调用之后用户说"继续"，模型没有提到截断，打算照原样一次写完；换成补一条"被输出上限截断、没有执行"的结果时，模型的思考里提到了截断和再撞上限的风险。owner 裁定先接受这一点（Q9）；截断时 CLI 显示一句话另开一项，编号预留 0081（Q4）。
9. 值得做的理由是它便宜、独立、消掉一个进去就出不来的失败状态，不是因为它影响面大。owner 2026-10-09 批准了计划，合并的前置条件是 3 次 DeepSeek 请求的真实模型验收（§7.3）。
10. 与计划 0078 和规划闸门没有顺序依赖，改的不是同一个函数。和 0078 第 3 版的原型按文本合并没有冲突，合并后两边的提议测试都过。Q1 至 Q10 都已裁定，见 §10。

## 1. 现状

下面用标签表示消息：`S` 是 system 消息，`U` 是用户请求，`A` 是只有文字的 assistant 消息，`a1` 是带 1 个调用、没有文字的 assistant 消息，`A+a1` 是既有文字又带调用的，`t` 是工具结果。`!` 表示这一轮有调用没被紧跟其后的工具结果回答，`~` 表示有调用的参数解析不成 JSON 对象。探针输出用的是同一套标签。

### 1.1 根因链

| 编号 | 环节 | 函数和位置 | 做了什么，为什么没拦住 |
|---|---|---|---|
| C1 | provider 解码被截断的回复 | OpenAI 流式：`OpenAIProvider._stream`（`provider/openai_provider.py:560-631`）把参数分片交给 `ToolCallAccumulators`（`:613-614`），`finalize`（`provider/_accumulator.py:83`）把收到的分片原样拼起来，`finish_reason` 取最后一个非空值（`:597-599`）。阻塞路径 `decode_arguments`（`:304-320`）原样保留字符串。Anthropic 流式：`_stream`（`provider/anthropic_provider.py:732-811`）同样累积 `partial_json`（`:761-765`）。Anthropic 阻塞路径 `decode_content`（`:121-153`）把接口给的 `input` 对象重新 `json.dumps`（`:156-164`），这条路径上的参数一定能解析 | 适配器只翻译，不判断截断。截到一半的参数字符串连同 `finish_reason` 一起交给引擎 |
| C2 | 引擎记下这条回复 | `_kernel` 先 `history.append(completion.message)`（`engine/loop.py:436`），再 `_stop_reason_for`（`:438`、`:700-721`）。截断排在最前面判断（`:717-718`），于是不进执行工具的分支（`:439`），`break`（`:450-452`），`RunResult.messages` 以这条消息结尾（`:454-461`） | 有意的设计。docstring（`:710-715`）写明这条消息照样记进历史，调用方要自己回答或丢掉这些调用，引擎不为它拒绝执行的调用编造 Observation |
| C3 | 引擎交还轨迹 | `_settle`（`:671-697`）按角色去掉 system 消息后 `conversation.commit`（`:696`） | 交还的是整段轨迹 |
| C4 | entry 收下 | `_Carried.commit`（`entry/turn.py:226`），`_outcome` 取 `exchange.conversation.carried`（`:333`）。`TurnRunner.run` 对任何返回了结果的交换都记 `terminal = "converged"`（`:583`），不看 `stop_reason` | entry 层就是 C2 说的调用方。这里没有回答，也没有丢 |
| C5 | 提交进会话 | `SessionRegistry._attempt`：`terminal == "converged"` 时 `session.history = outcome.history`（`entry/session.py:814-815`） | 原样提交 |
| C6 | 存库 | `self._store.save(session)`（`:821`）。`SqliteSessionStore._save`（`memory/sessions.py:161`）经 `_dump_tool_calls`（`:25`）把参数字符串原样写进 `messages.tool_calls`，`_load`（`:124`）逐字段读回 | 存取都不看配对，存储层也不该看 |
| C7 | 下一次交换开场 | `_attempt` 用 `session.history` 建 `TurnRunner`（`:773`），`_assemble` 包成 `_Carried(tuple(history))`（`entry/turn.py:273`），引擎的 `_opening`（`engine/loop.py:645-668`）拼成 `[system, *history, user]` | 新的 user 消息紧跟在没被回答的调用后面 |
| C8 | 压缩器 | `ProgressiveCompactor.compact` 在压力低于 `compact_at` 时返回 `None`（`context/progressive.py:147-148`），默认 `compact_at` 是 WARN（`entry/config.py:341`）。到了 WARN，`plan_compaction` 给出原样通过的计划（`context/compaction.py:289-290`），`apply_compaction` 走最后一行，不修复（`:333`） | 见 §1.2 |
| C9 | 适配器编码 | OpenAI 方言 `encode_message`（`openai_provider.py:123-165`）逐条翻译，`encode_tool_call`（`:107-120`）把参数字符串原样发出。Anthropic 方言 `encode_conversation`（`anthropic_provider.py:242-291`）经 `_assistant_blocks`（`:217-239`）调 `decode_arguments`（`:172-198`），参数解析不了就抛没有状态码的 `ProviderError`，请求不发 | 两个适配器都不检查配对 |

请求被拒之后：`_wrap` 把状态码带上（`openai_provider.py:470-484`），`_budget_for` 对 429 和 5xx 以外的状态码不重试（`engine/retry.py:209-234`），交换以 `terminal="failed"` 结束（`entry/turn.py:589`），`session.history` 不变（`entry/session.py:814-819`）。下一条消息从同一段历史开始。Anthropic 一侧本地抛的错没有状态码，按一般预算重试（`generate_retries` 默认 3，`engine/config.py:102`），每次结果相同。

截断的那一轮里没有工具被执行过：`_stop_reason_for` 在执行之前就返回了（`loop.py:438-448`）。一条消息里有几个调用时，前面参数完整的那几个同样没有执行。同一次交换里更早的轮次照常执行过，调用和结果都在历史里，配对完好。

### 1.2 `repair_tool_pairs` 在哪些档位跑

| 档位 | 走到的代码 | 修不修 | 结果写不写回历史 |
|---|---|---|---|
| NONE | `ProgressiveCompactor.compact` 返回 `None`（`progressive.py:147-148`） | 不修 | 不涉及 |
| WARN | `plan_compaction` 原样通过（`context/compaction.py:289-290`），`apply_compaction` 最后一行（`:333`） | 不修 | 只在 offload 了东西时写回 |
| SOFT、FULL，摘要成功 | `apply_compaction` 的 `needs_summary` 分支（`:325-330`） | 修，补占位 | 写回 |
| SOFT、FULL，降级截断 | `fall_back`（`:680`）调 `fit_to_budget`；已经放得下或没有 head 时原样返回（`transcript.py:238-243`），真的截了才修（`:245-250`） | 截了才修 | 不写回（`should_write_back`，`progressive.py:41`） |
| SOFT、FULL，pin 之后不超过 6 条 | `split_head_tail` 没有 head（`transcript.py:186-187`），`needs_summary` 为假，走 `:333` | 不修 | 不写回 |
| EMERGENCY | `apply_compaction` 的 EMERGENCY 分支（`:331-332`） | 修，补占位 | 写回 |

低档位不修是有意的。`apply_compaction` 的 docstring（`context/compaction.py:319-323`）写的理由是：原样通过的档位必须和输入逐字节相同，否则每个不需要压缩的轮次都要重新预热前缀缓存。

卡住的会话到不了 SOFT。失败的交换不往历史里加东西，历史不增长，压力不变；DeepSeek 的窗口在 `_model_limits.py:89` 是 1M。只有截断发生时已经在 SOFT 以上的会话，下一次调用才会被补上占位。

补上的占位是 `[tool result unavailable: the context was compacted]`，`is_error=False`（`SCR/logs/r2_out_10_facts_today.log` 的 d 段）。它对模型说的是：这次调用跑过，结果在压缩时被拿掉了。对被截断的调用来说这句话是错的，调用从来没有执行。

补上占位之后（`SCR/logs/out_01_stuck_today.log` 的 E 段，逐档直接调 `compact()`；审核方的 `RV80/logs/rv80_out10_tiers_base.log` 结果相同）：参数完整的调用在两个方言上都合法；参数被截的调用，DeepSeek 接受（V2），Anthropic 适配器在每一档都仍然本地抛错，因为修复只补结果，不碰参数。

`/compact` 救不回短会话，原因是上表第五行：强制 FULL 时 pin 之后不超过 6 条消息就没有 head，不摘要也不修复，`_compact_only` 把原样的历史交回去（`entry/turn.py:699-715`）。会话够长、摘要成功时，`/compact` 会给那条调用补上占位并写回。

### 1.3 各入口

| 编号 | 入口 | 代码 | 走不走 C4 至 C7 |
|---|---|---|---|
| P1 | CLI 交互 | `Repl.ask` 调 `registry.submit`（`entry/cli/_repl.py:1092`） | 走 |
| P2 | CLI 一次性执行（`--prompt`、`--prompt-file`） | `run_once` 调 `repl.ask`（`:1869-1897`） | 走。之后用同一个 `--session` 再执行就卡住 |
| P3 | Desktop | `registry.deliver`（`entry/desktop/server.py:264`），`/compact` 在 `:342` | 走 |
| P4 | Channel | `registry.deliver`（`entry/channel/runtime.py:413`） | 走 |
| P5 | 脚本化 eval 和 live eval 的 Runner | `app.sessions.submit`（`evals/runner.py:350`） | 走。`ScriptedProvider` 报不出 `length`（`evals/provider.py:316` 写死了 `tool_use` 或 `end_turn`），所以没有 eval 碰到过 |
| P6 | 库调用方 `run_turn`、`stream_turn` | `_assemble`（`entry/turn.py:389`、`:432`） | 不经过 `SessionRegistry`，但 `TurnOutcome.history` 同样以没被回答的调用结尾，传回去就失败（`out_01` 的 D 段） |
| P7 | 子代理 | `engine.exchange_stream(prompt, prompt=…)`，不给 `conversation`（`entry/subagent.py:393`） | 不走。轨迹用完就丢，`_conclusion` 自己处理了 `TRUNCATED`（`:471-481`） |
| P8 | bench | 每次运行一个 `oc cli --prompt-file` 进程（`bench/adapters/omicsclaw.py:230`） | 单次交换，没有下一条消息。`bench/outcome.py:219-220` 把它记成 `TRUNCATED` |

P1 至 P6 最终都经过 `entry/turn.py` 的 `_assemble`。全仓库直接调 `engine.exchange`、`exchange_stream` 的只有 `entry/turn.py`（`:295`、`:308`）和子代理。审核方另查了一遍，没有找到绕过这两处的模型调用路径。

### 1.4 用户今天看到什么

截断的那一次：文字和思考照常流出来，工具调用的参数分片从不外露（`docs/core-features/agent-loop.md:160`），交换以 `converged` 结束，CLI 对 `converged` 不打印结束行（`entry/cli/_repl.py:1208`）。`omicsclaw/entry` 里读 `StopReason.TRUNCATED` 的只有子代理的 `_conclusion`。用户看到的是回复停了，工具没有跑，没有任何说明。

之后的每一条消息：CLI 打印 `Failed: ProviderError`。`_terminal_line`（`entry/render.py:415-423`）只取异常的类型名，400 的原文不显示。Desktop 和 channel 上显示什么没有逐个看。用户能做的是 `/new` 或 `/clear`，代价是这段会话的上下文。

### 1.5 复现

脚本化模型，真实装配（`build_app`、`attach_sessions`、`SessionRegistry`，system 提示和工具表是真实的），后端是探针自己写的替身，对没被回答的调用返回 DeepSeek 的那句 400。脚本 `SCR/probes/probe_01_stuck.py`，输出 `SCR/logs/out_01_stuck_today.log`。

| 段 | 形状 | 今天 |
|---|---|---|
| A-a | 第一轮：文字加一个参数被截的 `write_file` | 交换 1 `converged`、`stop=truncated`，历史 `U A+a1!~`。交换 2、3 `failed`，历史不变 |
| A-b | 第一轮：只有思考和一个参数被截的调用 | 历史 `U a1!~`，之后同上 |
| A-c | 第二轮（前面一轮工具已执行）：文字加一个参数完整的调用 | 历史 `U a1 t A+a1!`，之后同上。Anthropic 请求能组出来，违反配对 |
| A-d | 第二轮：只有一个参数被截的调用 | 历史 `U a1 t a1!~`，之后同上 |
| A-e | 一轮两个调用，前一个完整，后一个被截 | 历史 `U a2!~`，之后同上 |
| A-f | 同 A-a，`finish_reason` 写成 Anthropic 的 `max_tokens` | 同 A-a |
| A-g | 对照：被截断的一轮不带工具调用 | 历史 `U A`，之后的交换正常 |
| B | 会话存在 SQLite，关掉 app 再建一个 | 读回 `U a1 t A+a1!~`，之后的交换 `failed` |
| C | 短会话上 `/compact` | `converged`，历史不变，下一条消息 `failed` |
| D | 阻塞路径：`run_turn` 之后把 `TurnOutcome.history` 传给下一次 `run_turn` | 第二次抛 `ProviderError` |

A 段 a、b、d、e、f 五种形状里，Anthropic 适配器连请求都组不出来，报 `unparseable arguments`。

真实模型（§2.1 的 V8、V9）：把 `OpenAIProvider` 的 `max_tokens` 绑到 2,000，经同样的装配提交一条"用 `write_file` 写一篇 3000 词综述"的请求。DeepSeek 返回 `finish_reason='length'`，没有正文，3,050 个字符的思考，一个 `write_file` 调用，参数 6,368 个字符、解析不了。交换 `converged`、`stop=truncated`，SQLite 里存下 `U a1!~`。把工作区复制一份，在今天的代码上提交下一条消息：请求的角色序列是 `suau`，返回 400，`terminal=failed`，历史不变。

### 1.6 和派发本计划时的说法有出入的地方

1. 派发时说"参数 JSON 被截在中间时，补一条占位结果不够，适配器本地解析参数就会抛错"。这只对 Anthropic 适配器成立。OpenAI 方言把参数字符串原样发出，真实 DeepSeek 接受了一条带占位结果、参数被截的调用（V2，200）。
2. 派发时说"一旦发生，这个会话此后每条消息都失败"。在压力低于 SOFT 时成立，而卡住的会话压力不会再升（§1.2），所以在 1M 窗口上实际就是一直失败。截断发生时已经在 SOFT 以上的会话，下一次调用会被压缩补上占位，在 DeepSeek 上能继续。
3. 派发时说"默认配置下需要一次回复撞上输出上限"。补充一点：OpenAI 方言默认不发 `max_tokens`（§3），上限是厂商的默认值，OmicsClaw 没有给用户设置它的入口。
4. 派发时说"发生的频率没有量过"。在主检出的会话库上量了一次，是 0（§3）。
5. 派发时给的三个位置（`engine/loop.py:700-721`、`entry/session.py:814-816`、`docs/core-features/agent-loop.md:499`）核对无误。计划 0079 §2.3 关于这种形状的描述也核对无误，要补的一条是 §1.2 表里的第五行。

## 2. 两种方言对这些形状的态度

### 2.1 OpenAI 兼容接口：DeepSeek 实测

模型 `deepseek-v4-flash`（接口返回的名字是 `deepseek-flash`），思考模式是默认开着的。V1 至 V7 的请求体由产品自己的 `OpenAIProvider._request_payload` 组出来，用 httpx 发；V8 至 V10 经过完整装配和真实的 `OpenAIProvider`，SDK 重试关掉，一次模型调用就是一次请求。

| 编号 | 请求 | 返回 | 说明了什么 |
|---|---|---|---|
| V1 | `su`，`max_tokens=700`，要求用 `write_file` 写长文，非流式 | 200，`finish_reason='length'`，正文为空，思考 2,673 字符，1 个调用，参数 635 字符、解析不了。`completion_tokens=700`，其中思考 550 | 被截断的回复确实带着半截调用。思考占用输出上限 |
| V2 | `suatu`：参数被截的调用，后面有一条占位结果 | 200 | DeepSeek 不校验历史里调用参数是不是合法 JSON |
| V3 | `suau`：assistant 一轮只有思考、正文为空、没有调用 | 传输中断（`RemoteProtocolError`），没有得到信息 | |
| V4 | 重发 V3 | 200 | DeepSeek 接受正文为空的 assistant 消息 |
| V5 | `suu`：两条 user 相邻 | 200 | 推荐做法会产生的形状之一 |
| V6 | 探针写错：带了 `stream_options` 没带 `stream` | 400，报的就是这个错 | 没有得到信息。产品把 `stream=True` 另外传给 SDK，不受影响 |
| V7 | `su`，不带 `max_tokens`，流式，要求逐行输出 1 到 4000 | 200，`finish_reason='stop'`，`completion_tokens=12,934`，其中思考 1,933 | 默认上限高于 8,192 |
| V8 | 完整装配，`max_tokens=2000`，`su`，11 个工具，流式 | 200，`finish_reason='length'`，见 §1.5 | 流式路径经累积器得到的也是半截参数 |
| V9 | 今天的代码，V8 存下的会话，下一条消息，`suau` | 400：`An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'. (insufficient tool messages following tool_calls message)` | 缺陷本身 |
| V10 | 第 1 版原型，同一个会话的另一份副本，下一条消息，`suu` | 200，`finish_reason='stop'` | 原型不卡死 |

既有的两条（`RV79/rv_out3_deepseek.log`，2026-10-08，本计划没有重发）：`suau` 带一条参数完整的未答调用返回 400，同样的对话补上结果（`suatu`）返回 200。

审核方另发的 8 次（`RV80/logs/rv80_out13_live_a.log`、`rv80_out13_live_b.log`）全部返回 200，形状是 `suau`、`suu`、`suatu`、`suatau`、`suatatu`。它们看的是清理之后模型下一轮怎么做，结果写在 K1。

推荐做法发出的三种形状（§5.1）今天就有：有计划的会话里，注入器每次调用都在末尾追加一条 user 消息（`planning/injector.py:166-173`），工具结果后面跟 user、user 后面跟 user 都是日常请求的样子。

别的 OpenAI 兼容后端没有测。

### 2.2 Anthropic：只有文档和代码依据

没有对真实接口发过请求。

| 结论 | 依据 |
|---|---|
| assistant 轮里的每个 `tool_use`，紧接着的 user 消息里必须有它的 `tool_result` | 文档 "Handle tool calls" 的 formatting requirements："Tool result blocks must immediately follow their corresponding tool use blocks in the message history."，以及报错 "tool_use ids were found without tool_result blocks immediately after" |
| 回复在工具调用中间被 `max_tokens` 截断时，官方建议是调高 `max_tokens` 重发同一个请求 | 文档 "Handling stop reasons" 的 "Incomplete tool use blocks" 一节："you'll need to retry the request with a higher `max_tokens` value to get the full tool use"。示例代码重发的是原来的 `messages`，被截断的回复没有放进去 |
| 相邻的同角色轮次由服务端合并 | API 参考的 `messages` 参数说明："Consecutive `user` or `assistant` turns in your request will be combined into a single turn." 随 Claude Code 分发的参考在这一点上自相矛盾（计划 0079 §1.1 的 A2 记过） |
| 只含空白的 text block 会被拒 | 第三方。报错原文 `messages: text content blocks must contain non-whitespace text` 在几个项目的问题报告和一个错误库里一致出现（agno 的 issue 3137、Portkey 的错误库等，都是搜索结果的摘要）。官方 API 参考只写了 `text` 的 `minLength: 1`（计划 0079 §1.1 的 A7）。没有在真实接口上核实 |
| 参数被截的调用，这个适配器根本发不出去 | 代码：`decode_arguments`（`anthropic_provider.py:172-198`）。docstring 写明它有意不用 `parsed_arguments()` 的 `{}`，因为那等于把历史重放成模型没发过的一次调用 |
| 没有内容的 assistant 轮不会出现在请求里；只含空白的会 | 代码：`_assistant_blocks` 按 `message.content` 的真假值决定要不要 text block（`:228-229`），`encode_conversation` 只在有 block 时追加（`:286-288`） |
| 流式路径上参数会被截成解析不了的字符串，阻塞路径上不会 | 代码：C1。阻塞路径上接口对一个没写完的 `tool_use` 返回什么样的 `input`，文档里没有查到 |

## 3. 频率和触发条件

输出上限从哪来。`ProviderConfig.max_tokens` 默认是 0，表示没设（`provider/config.py:413`）。主循环的 provider 由 `provider_from_env(config.provider, config.model)` 建（`entry/assembly.py:1134`），不传 `max_tokens`；`AppConfig`、命令行和环境变量里都没有设它的地方。主检出的 `.env` 里和 provider 有关的只有 `LLM_PROVIDER=deepseek` 和密钥，解析出来的 `max_tokens` 是 0（`SCR/logs/live_00_settings.log`）。

OpenAI 方言：`max_tokens` 为 0 时请求里没有这个键（`openai_provider.py:507-512`），用厂商的默认值。DeepSeek 的 API 文档写的是：不设时非思考模式 8K，思考模式 64K（`reasoning_effort` 为 `max` 时 128K），可设范围 1 到 384K；`thinking` 的默认值是 `enabled`。V7 实测超过 12,934。审核意见里另给了两个预设的默认值：智谱 65,536，Kimi K3 131,072，我没有复核。其余预设没有查。

Anthropic 方言：每次必发，值是 `config.max_output_tokens`（`anthropic_provider.py:633-636`），取自 `_model_limits.py` 的表。这是适配器发的数，不是模型的上限，表落后于模型：

| 模型 | 适配器发的 `max_tokens` | 参考文档写的模型上限 |
|---|---|---|
| `claude-sonnet-4-6`（预设的默认模型） | 64,000 | 128K |
| `claude-opus-4-6`、`claude-opus-4-7` | 32,000 | 128K |
| `claude-haiku-4-5` | 8,192 | 64K |
| 表里没有的型号，4.7 之后的：`claude-opus-4-8`、`claude-opus-5`、`claude-opus-5-5`、`claude-sonnet-5`、`claude-sonnet-5-5`、`claude-fable-5`、`claude-fable-5-1` | 8,192（落到 `DEFAULT_MODEL_LIMITS`，`_model_limits.py:52`） | 128K |
| 表里没有的型号，更早的：`claude-opus-4-5`、`claude-opus-4-1` | 8,192，同上 | 参考文档的旧型号表没有列输出上限 |

左边一列是空跑 `AnthropicProvider._request_params` 得到的（`SCR/logs/r3_out_10_facts.log` 的 a 段；审核方的 `RV80/logs/r2_out9_anthropic_max_tokens.log` 相同）。右边一列出自随 Claude Code 分发的 API 参考（`claude-api` skill 的 `shared/models.md`，缓存日期 2026-09-25），没有对真实接口或 Models API 核实。

什么样的回复会撞上。一条回复的输出是思考、正文和工具调用参数三者之和（V1 里 700 个 token 有 550 个是思考）。主检出的库里，参数最长的工具是 `write_file`（17 次调用，中位数 2,497 字符，最大 11,711），其次是 `task`、`edit_file`、`bash`。撞上限的会是一次写很长的文件，或者很长的思考后面跟一个调用。

发生过几次。把主检出的 `.omicsclaw/memory.db` 连同 WAL 复制到 `SCR/dbcopy/`，只读打开副本（`SCR/probes/freq_01_db.py`、`freq_02_sizes.py`；审核方的 `RV80/logs/rv80_out8_freq_db.log` 数字相同）：

| | 数 |
|---|---|
| 会话 | 26（2026-09-20 至 10-08），其中有消息的 19 个，用户消息一共 27 条 |
| assistant 消息 | 174，其中 147 条带工具调用，共 242 个调用；242 条工具结果里 6 条是失败 |
| 有调用没被回答的会话 | 0 |
| 参数解析不了的调用 | 0 |
| 同一轮里重复的调用 id、空的调用 id、只含空白的文字 | 都是 0 |
| 最大的一条回复 | 22,624 字符（思考 6,269，两个 `write_file` 的参数 16,235），估算约 4,900 token |
| 每条回复的估算 token | 中位数 226，95 分位 2,187 |

估算用的是 ASCII 每 4.7 个字符一个 token（V1 和 V8 两条真实回复量出来的都是 4.7），非 ASCII 每个字符算一个 token，偏高估。按这个估算，174 条里没有一条达到 8,192，只有一条超过它的一半；最大的一条是 64K 的 8%。

能说的有这些。样本很薄，19 个会话说明不了"很少发生"，只说明这个库里没发生过。站得住的一句是：owner 日常的配置下（DeepSeek，思考模式，不设上限），库里最大的回复是文档上限的 8%，差一个数量级。Anthropic 方言不一样：用 haiku 或表里没有的任何型号时适配器发的都是 8,192。库里有一条回复估算过了这个数的一半，但那些回复是 DeepSeek 写的，换一个模型回复的长短会不同，分词也不同，所以这只是一个估计，说明量级上离得不远。把表补上能把这一侧的上限抬到 64K 或 128K，那是降频，救不了已经存进库的会话；owner 裁定这次不立项，记在 §9 末尾。

量不到的部分：其他工作区和 Desktop 用户的库不在这台机器上。`/tmp` 下还有几百个测试和验收遗留的 `memory.db`，没有扫（本会话的权限层拒绝了批量读取，没有绕过）。没有找到带 `stop_reason` 的落盘记录：`agent.stop_reason` 只写进 OTel span（`observability/scope.py:594`），主检出里没有 bench 或 live eval 的结果文件带这个字段。派发时说的"已复现"是脚本化模型加真实接口对形状的拒绝，V8 至 V10 是把上限人为绑低之后的真实复现，都不是默认配置下自然发生的。

## 4. 方案比较

### 4.1 截断的那条回复怎么处理

| 编号 | 做法 | 用户看到 | 模型下一轮看到 | 参数被截的调用 | 部分完整、最后一个被截 |
|---|---|---|---|---|---|
| X1 | 丢掉没被回答的调用，留文字 | 截断那次和今天一样：回复停了，没有说明。下一条消息正常得到答复 | 自己那一轮说过的文字，没有那次调用，也没有结果。那一轮没有文字时整轮不出现 | 随调用一起去掉，不需要解析 | 全部去掉。完整的那几个也没有执行过 |
| X2 | 给没被回答的调用补一条占位结果 | 同 X1 | 自己的调用，后面一条"没有执行"的结果。信息最全 | DeepSeek 接受（V2）。Anthropic 适配器发不出去，这类调用得另外丢掉，或者把参数改写成 `{}` | 完整的补结果，被截的另行处理 |
| X3 | 整条回复不记入历史 | 同 X1 | 看不到截断那一轮的任何东西，包括用户已经看到的文字 | 不存在这个问题 | 不存在 |
| X4 | 截断后在同一次交换里让模型续写或重试 | 交换不结束，模型接着做 | 取决于给它的那句话 | 丢掉，让模型重发 | 同上 |
| X5 | 提高输出上限 | 少遇到 | 不变 | 不变 | 不变 |

已经执行过的工具在五种做法下都一样：截断的那一轮没有执行任何调用（§1.1），更早轮次的调用和结果原样留着。

X1 还改掉一件今天的错事。压力在 SOFT 以上时，今天的压缩给被截断的调用补的是 `[tool result unavailable: the context was compacted]`，不标错误（§1.2），等于告诉模型调用跑过了。清理放在开场、压缩之前，这种占位不再出现在被截断的调用后面（`SCR/logs/r2_out_10_facts_prototype.log` 的 d 段，占位从 1 条变成 0 条）。

X2 的问题有三处。典型的截断正好断在参数中间（V1、V8 都是），这时在 Anthropic 上要么退化成 X1，要么得改写参数，而改写参数是 `decode_arguments` 的 docstring 明确不肯做的事。占位结果是一条编出来的 Observation，引擎的 docstring（`loop.py:712-715`）不做这件事的理由是它和工具真的这样返回分不开。被截的参数留在历史里，DeepSeek 默认上限下可能是几万 token 的残片，之后每次请求都带着，直到被压缩。把原型的清理换成 `repair_tool_pairs`（变异 M12）就是 X2 不处理参数的版本：参数被截的 5 种形状在 Anthropic 编码器上全红，参数完整的那一种通过。X2 的好处是模型知道发生了什么，审核方的样本见 K1。

X3 让带调用的截断丢文字，不带调用的截断（今天合法，文字留着）不丢，两种截断不一致，用户看到过的文字模型却不记得。

X4 是 Anthropic 文档建议的方向，也是能把用户的任务做完的做法。它要改引擎循环、定重试次数和一句给模型的话。上限不变时原样重试会再截一次，再烧一次上限那么多的输出 token，所以那句话得让模型换一种做法（比如分块写）。这句话如果进持久历史，就是第三种 user 消息，而规划闸门（计划 0039 §9.3）和计划 0078 的规则都建立在"持久历史里的 user 消息只有请求和摘要"之上；只进发送副本的话要给 augmentor 加状态。这是行为和措辞改动，按惯例要先做真实会话对比。它也救不了已经存进库的会话。

X5 只降低频率，救不了已经存进库的会话。两个方言的情况不同。DeepSeek 不设时已经是文档上的 64K。Anthropic 方言发的是 `_model_limits.py` 表里的数，这张表落后于模型（§3 的表）：haiku 和表里没有的型号都只发 8,192。把表补上是 Anthropic 一侧便宜的降频手段，但有两件事要另算。这个数同时是上下文预算的输出预留（`entry/assembly.py:663`），调大会缩小可用窗口。参考文档写 128K 这么大的 `max_tokens` 要用流式请求，而 `run_turn` 和摘要器（`entry/assembly.py:711`）走的都是阻塞的 `generate`。所以它是一项单独的改动，替代不了本计划。owner 裁定这次不立项（Q7）。

### 4.2 在哪里处理：每道防线的账

| 编号 | 位置 | 防什么 | 旁路 | 代价 | 值不值 |
|---|---|---|---|---|---|
| L1 | 引擎源头：`_kernel` 在 `TRUNCATED` 时改写或不记这条消息 | 今后的截断，覆盖所有引擎调用方 | 已经存进库的会话；调用方自己给的历史 | 改引擎的契约（`_stop_reason_for` 的 docstring、`agent-loop.md:229`、`:499`）。`RunResult.messages` 不再是模型的原话，追踪和 eval 读的是它 | 不值。救不了存量；有了 L3 之后它没有新增的覆盖，仓库里不经过 `entry/turn.py` 的引擎调用方只有不带历史的子代理 |
| L2 | entry 提交时：`_outcome` 在 `stop_reason` 是 `TRUNCATED` 时清理 `TurnOutcome.history` | 今后的截断；库里存的历史始终合法 | 存量会话；调用方自己给的历史 | 一处调用。`TurnOutcome.history` 不再等于 `result.messages` 去掉 system 消息 | 单独不够。和 L3 一起时只多管"两次交换之间库里的样子"，读这段历史的只有 CLI 恢复会话时的回顾（`entry/cli/_repl.py:402`、`:918`）。不做 |
| L3 | entry 开场时：`_assemble` 和 `compose` | 今后的截断（在下一次交换开场时）、存量会话、`run_turn` 和 `stream_turn` 的库调用方、`/compact` | 直接调 `engine.exchange` 或 `engine.run` 的库调用方，引擎的契约照旧由他们自己处理。从截断到下一次交换之间，库里存的仍是原样 | 一个纯函数，两处调用，每次交换开场把历史扫一遍。没有问题的历史原对象返回，请求字节不变 | 值。推荐 |
| L4 | 每次发送前：压缩器在所有档位都修，或者两个适配器各自检查 | 任何来源的未答调用 | 适配器方案没有旁路 | 压缩器方案违背低档位逐字节原样通过的约定（`context/compaction.py:319-323`）。适配器方案要在两个文件里各写一遍，provider 层不能 import context 层（`tests/provider/test_provider_layering.py:94`），改的是 `SPEC.md` 第 4 档的代码，Anthropic 一侧还要处理参数 | 现在不值。交换进行中引擎保证每个调用都有结果（`_answer_every_call`，`loop.py:724-811`），压缩删消息之后已有修复。L4 比 L3 多防的只有直接调引擎的库调用方 |
| L5 | 读库时：`SqliteSessionStore._load` | 存量会话 | `InMemorySessionStore` 和别的 `SessionStore` 实现；调用方自己给的历史 | 让存储层懂线上协议 | 不值 |
| L6 | 一次性迁移，或者一条修复命令 | 存量会话 | 迁移之后再发生的；命令要用户先知道原因，而界面只显示 `Failed: ProviderError` | 一段只跑一次的代码，或者一个新的 surface | 不值。已知的存量是 0 个会话 |

只在源头处理不够：L1、L2 都救不了已经存进库的会话，得另配 L5 或 L6。L3 一处就同时管住新发生的和存量的。有了 L3，源头再加一道只改变库里那段历史在两次交换之间的样子。发送前那一道防的是今天仓库里不存在的调用方式。所以推荐只做 L3。

## 5. 推荐

### 5.1 规则

处理方式用 X1，位置用 L3，owner 已裁定（Q1、Q2、Q10）。第 2 条的重复 id、第 4 条的空白和范围是第 2 版写进条文的，第 3 版给空白补了定义。

1. 能回答一轮调用的，只有紧跟在那条 assistant 消息后面的那一串连续的 `Role.TOOL` 消息。相邻这一点和 `repair_tool_pairs` 相同。
2. 一条结果回答一个调用。按调用在消息里的先后，每个调用认领一条带着它 id 的结果，认领不到的就是没被回答。id 原样比较，空 id 也是 id。`is_error` 的结果照样算回答。
3. 没被回答的调用从它那条消息上去掉。消息的文字、思考和被回答了的调用不动。
4. 被去掉过调用的那条消息，如果剩下的既没有调用、文字去掉空白后也是空的，整条去掉，思考随它一起去掉。空白按 `str.strip()` 判断，制表符和全角空格也算。这一条只管被去掉过调用的消息：本来就没有调用的 assistant 消息不动，不管它有没有文字。
5. 不补任何结果，不加任何文字。
6. 没有可去掉的东西时，返回的是原来那些消息对象，顺序不变。
7. 找不到调用的工具结果不归它管，留给 `repair_tool_pairs`。
8. 角色比较用 `==`，理由同 `repair_tool_pairs` 的 docstring。
9. 这个函数在两个地方调用：`_assemble` 建 `_Carried` 时，以及 `compose` 把历史交给 `assemble` 时。前者管所有带模型调用的交换，后者管 `/compact` 和 `prepare`。
10. `_assemble` 去掉了调用时写一行 INFO 日志，带会话 id 和去掉的调用数。它不是记录：CLI 和 Desktop 上默认看不到（Q6）。

第 2 条为什么选"一条结果回答一个调用"。第 1 版的原型用集合判断：只要有一条结果带着这个 id，带这个 id 的调用就都算回答了，于是两个同 id 的调用配一条结果时两个都留下，发出去的是两个调用一条结果。按一条对一条来配，清理之后每个留下的调用都有自己的一条结果。引擎写出来的历史里结果数等于调用数（`_answer_every_call` 按位置配并把 id 盖成调用的 id），两种读法在产品路径上没有差别；调用的 id 为空时也一样，两个空 id 的调用配两条空 id 的结果，两种读法都原样留着。空 id 是这样来的：厂商不给 id 时两个适配器都把它留空（`openai_provider.py:336`，`anthropic_provider.py:143`、`:772`）。`loop.py:745-748` 的 docstring 说 `ollama` 预设会走到这里，没有人对真实的 Ollama 测过。主检出的库里没有重复的 id，也没有空 id。owner 裁定用一条对一条的读法（Q10）。

它和 `repair_tool_pairs` 的判据不完全相同，第 1 版说"是同一个"不准。相邻这一半相同。重复 id 这一半，`repair_tool_pairs` 用 `setdefault` 存结果、用 `pop` 取（`transcript.py:143-150`），同一个 id 的第二条结果被忽略：两个同 id 的调用配两条结果时，它留下第一条结果，把第二条换成占位（`SCR/logs/r2_out_10_facts_today.log` 的 e 段）。那是它今天的行为，不在本计划范围里，记在 §9 末尾。

第 4 条的理由。Anthropic 适配器本来就不发没有内容的 assistant 轮（§2.2），整条去掉之后两个方言看到的是同一段对话；留着的话 DeepSeek 上会把几千字符的思考当 `reasoning_content` 重放，对模型没有用处。DeepSeek 接受正文为空的 assistant 消息（V4），所以空的这一半不是为了躲 400。空白那一半是为了 Anthropic：只含空白的文字会被适配器编成一个 text block，第三方的报告说接口会拒绝它（§2.2，没有核实）。第 1 版的原型按 `message.content` 的真假值判断，这样的一轮去掉调用之后会被留下。空白只在这一处判断，用的是 `str.strip()`，留下来的消息不改文字。

去掉之后，下一次请求里那个位置有三种样子，都是今天就在发的形状（§2.1）：那一轮有文字时是 `… A U`；没有文字、前面是工具结果时是 `… a t U`；没有文字、前面就是请求时是 `U U`。

### 5.2 原型

`SCR/r2_apply_prototype.py` 写进 `SCR/r2_proto/`（`git archive 5daf5482` 的导出）。实施时可以另写，以 §6.2 的测试为准。和第 1 版的原型（`SCR/proto/`）相比，函数里改了两处：认领结果的循环，和 `message.content.strip()`；`entry/turn.py` 的改动没有变。第 3 版没有改原型。

`omicsclaw/context/transcript.py`，从 schema 多引入一个 `ToolCall`，函数加进本模块和 `omicsclaw.context` 的 `__all__`：

```python
def drop_unanswered_calls(messages: Sequence[Message]) -> tuple[Message, ...]:
    """Remove every tool call that no tool result answers.

    The results that can answer a turn's calls are the ``Role.TOOL``
    messages directly behind it, the adjacency :func:`repair_tool_pairs`
    pairs by. One result answers one call: each call, in order, takes a
    result that carries its id, and a call left without one is
    unanswered. Ids are compared as they are, the empty id included.

    An unanswered call is taken off its turn. The turn keeps its text,
    its reasoning and its answered calls. A turn that this leaves with
    no call and no text other than whitespace is removed. A turn that
    lost no call is never touched, whatever it holds.

    No result is written in a removed call's place. The call never ran,
    and its arguments may be cut off mid-JSON, which the Anthropic
    adapter refuses to encode.

    Such a turn is what a run cut off by the output ceiling ends on: the
    engine records the turn and does not run its calls. Both API
    dialects reject a request that carries it.

    A conversation with nothing to remove comes back with the same
    message objects in the same order. A tool result whose call is
    missing is left for :func:`repair_tool_pairs`.
    """
    kept: list[Message] = []
    index = 0
    total = len(messages)
    while index < total:
        message = messages[index]
        index += 1
        if message.role != Role.ASSISTANT or not message.tool_calls:
            kept.append(message)
            continue
        run_end = index
        while run_end < total and messages[run_end].role == Role.TOOL:
            run_end += 1
        unclaimed = [answer.tool_call_id for answer in messages[index:run_end]]
        calls: list[ToolCall] = []
        for call in message.tool_calls:
            if call.id in unclaimed:
                unclaimed.remove(call.id)
                calls.append(call)
        if len(calls) == len(message.tool_calls):
            kept.append(message)
        elif calls or message.content.strip():
            kept.append(message.replace(tool_calls=calls))
    return tuple(kept)
```

`omicsclaw/entry/turn.py`：

```python
    # compose
    return assemble(prompt, drop_unanswered_calls(history), user_text), prompt

    # _assemble
    carried = drop_unanswered_calls(history)
    removed = sum(len(m.tool_calls) for m in history) - sum(
        len(m.tool_calls) for m in carried
    )
    if removed:
        _log.info(
            "session %s: %d tool call(s) with no result left out of the history",
            session_id or "-",
            removed,
        )
    return _Exchange(
        conversation=_Carried(carried),
        ...
```

原型上的结果（`SCR/logs/r2_out_02_stuck_prototype.log`，同一个探针、同一个替身后端；和第 1 版原型的输出逐行相同）：

| 段 | 今天 | 原型 |
|---|---|---|
| A-a、A-f | 交换 2、3 `failed` | `converged`，交换 2 的请求是 `S U A U` |
| A-b、A-e | `failed` | `converged`，请求是 `S U U` |
| A-c | `failed` | `converged`，请求是 `S U a1 t A U` |
| A-d | `failed` | `converged`，请求是 `S U a1 t U` |
| A-g 对照 | `converged` | `converged`，请求的形状和今天相同 |
| B SQLite 重启 | `failed` | `converged`，存回去的是 `U a1 t A U A` |
| C `/compact` | 历史不变，下一条 `failed` | `/compact` 之后历史是 `U a1 t A`，下一条 `converged` |
| D `run_turn` | 第二次抛错 | 第二次 `stop=converged` |

今天 A、B、C 三段里有 15 次交换 `failed`，D 段第二次 `run_turn` 抛错；原型上没有一次失败，替身后端没有拒绝过任何请求。原型上每一次请求在两个方言的检查里都没有违规，Anthropic 请求都组得出来。真实接口上是 V9 对 V10，V10 用的是第 1 版原型，两版在那个会话上做的事相同（去掉唯一的一轮）。

另外三项检查，都在第 2 版原型上：

- 主检出会话库的副本里 19 个有消息的会话，逐个过这个函数，返回的都是原来的消息对象（`SCR/logs/r2_out_10_facts_prototype.log` 的 f 段）。审核方对第 1 版原型做过同样的检查（`RV80/logs/rv80_out18_real_sessions_identity.log`）。
- 20 万段随机历史（游离的结果、放错位置的结果、重复和空的 id、只含空白的文字、字符串角色），原型和照条文另写的一份实现比对，差异 0 次；清理后还有未答调用、第二遍还会再改、非 assistant 消息被改动、OpenAI 载荷里调用多于结果，都是 0 次（`SCR/probes/r2_probe_03_fuzz.py`，`SCR/logs/r2_out_16_fuzz.log`）。
- 审核方照条文独立写的实现。第 1 轮按第 1 版条文的字面读，和第 1 版原型在 20 万段上差异 0 次（`RV80/logs/rv80_out1_fuzz.log`），并量出条文没写死的三处各会差多少段：空白 81,770，范围 53,712，重复 id 3,029。第 2、4 条就是冲着这三处写的。第 2 轮只照第 2 版的条文另写，和第 2 版原型在 20 万段上差异 0 次；把空白读成只有空格、制表符和换行时差 48,349 段（`RV80/logs/r2_out1_fuzz.log`），所以第 3 版在规则 4 里写明了 `str.strip()`。

### 5.3 理由

- 会话不再卡死，存量会话不需要迁移，`/compact` 也能救。
- 做的是引擎契约已经写给调用方的两个选项之一（"answer or drop"），引擎、适配器、存储层和压缩器都不改。
- 两个方言一条规则。参数被截的调用不需要解析、不需要改写，随调用一起去掉。
- 不编结果，不往历史里加 user 消息，规划闸门和计划 0078 依赖的"持久历史里的 user 消息只有请求和摘要"不受影响。
- 今天压力高时压缩给被截断的调用补的那条占位，说的是调用跑过了；清理放在压缩之前，这条占位不再出现（§4.1）。
- 不加给模型看的文字，没有措辞要调。
- 对没有问题的历史是恒等的：原对象返回，请求字节不变，前缀缓存不受影响。有问题的那条消息在历史的末尾，去掉它不动它前面的前缀。
- 几万 token 的半截参数不留在上下文里。
- 改动小：一个二十几行的纯函数，两处调用，一行日志。暂存副本里跑第 3 档的命令，只有公开面清单那一条测试转红（§7.2）。

### 5.4 放弃了什么

- 模型不知道自己上一轮被截断了。用户说"继续"时它很可能照原样再来一次；内容确实超过上限时会再截一次，再花一次上限那么多的输出 token（K1）。今天的代码在这里是这段会话的上下文不能再用。
- 用户仍然看不到"回复被截断了"。这是今天就有的缺口，不带工具调用的截断也一样。
- 从截断到下一次交换之间，库里存的历史仍然以没被回答的调用结尾。
- 整轮去掉时，那一轮的思考不再留在历史里。
- 直接调 `engine.exchange` 的库调用方不受保护，引擎的契约没有变。
- CLI 的 `/compact` 有两句话会和事实对不上。短会话上它仍然打印 "Nothing to compact: this conversation is already short."，摘要失败时仍然打印 "…the conversation was left as it was"（`entry/cli/_repl.py:446-467`），而历史里没被回答的调用其实已经去掉了。前一句见 `SCR/logs/r2_out_10_facts_prototype.log` 的 g 段，后一句见审核方的 `RV80/logs/r2_out11_compact_texts.log`。这两句话读的是压缩记录，记录不知道清理的事。本计划不改它们，记进已知限制。

## 6. 改动范围

### 6.1 代码

| 文件 | 改动 |
|---|---|
| `omicsclaw/context/transcript.py` | 新增 `drop_unanswered_calls`，加进 `__all__`；从 `omicsclaw.schema` 多引入 `ToolCall` |
| `omicsclaw/context/__init__.py` | import 和 `__all__` 各加一个名字 |
| `omicsclaw/entry/turn.py` | `compose` 和 `_assemble` 按 §5.2 改。docstring 要改五处：`compose`、`_assemble`、`TurnOutcome.history` 各加一句，没被回答的工具调用在开场时去掉，`TurnOutcome.history` 在被截断的运行之后仍以它们结尾、传回来时去掉；`TurnRunner` 的类 docstring（`:464-467`）和 `_compact_only` 的 docstring（`:705`）现在写的是"历史只在压缩写回时才被替换"，落地后不再成立，要补上"没被回答的调用总是去掉" |
| `omicsclaw/entry/session.py` | 只改 `SessionRegistry.compact` 的 docstring（`:400-402`），同一句话 |

`omicsclaw/engine/`、`omicsclaw/provider/`、`omicsclaw/memory/`、`repair_tool_pairs` 和 `entry/session.py` 的逻辑都不改。`_stop_reason_for` 的 docstring 说的仍然成立，不动。`entry/cli/_repl.py` 的 `_compaction_verdict` 不动（§5.4）。

### 6.2 测试

T1 至 T23 写在 `SCR/r3_proposed/test_plan0080_r3_proposed.py` 里，参数化后 36 个用例（`SCR/logs/r3_out_03_proposed_base.log`、`r3_out_03_proposed_r2_proto.log`）：今天 34 红 2 绿，原型 36 绿。日志里的总数各多 1，是打印 `omicsclaw.__file__` 的那一条。第 2 版的 33 个用例审核方重跑过，结果和第 2 版写的一致（`RV80/logs/r2_out2_proposed_base.log`、`r2_out2_proposed_r2_proto.log`）。第 3 版多出的 3 个用例是 T4 的两种形状和 T23，另外改了 T20 的样本。

`tests/context/test_transcript.py`，纯函数。今天的红是函数不存在。括号里是这条测试堵上的审核方变异：

| 编号 | 测试 | 钉住的规则 |
|---|---|---|
| T1 | `test_an_unanswered_call_is_taken_off_its_turn_and_the_text_stays` | 规则 3：调用去掉，文字和思考留着 |
| T2 | `test_a_turn_left_with_no_text_and_no_call_is_removed` | 规则 4 |
| T3 | `test_only_the_unanswered_call_of_a_turn_goes`，那一轮带文字和思考（N11） | 规则 3：留着已答调用的那一轮，文字、思考和结果都在 |
| T4 | `test_a_conversation_with_every_call_answered_comes_back_message_for_message`，9 种形状。第 2 版加了 3 种：一个空 id 的调用和它的结果（N7）；两个空 id 的调用和两条结果；没有文字也没有调用的 assistant 消息。第 3 版加了 2 种：结果的先后和调用相反（W2）；一轮后面多出一条没有调用认领的结果（W8） | 规则 6：返回的是原对象。规则 2：空 id 也是 id，结果不必按调用的先后排。规则 4 的范围。规则 7 |
| T5 | `test_an_unanswered_call_in_the_middle_of_a_conversation_is_removed_too` | 不只看末尾 |
| T6 | `test_a_result_that_is_not_directly_behind_its_turn_does_not_answer_it` | 规则 1、7：相邻才算；游离的结果不动 |
| T7 | `test_roles_that_arrive_as_plain_strings_are_read_the_same` | 规则 8 |
| T16 | `test_a_failed_tool_result_answers_its_call`（N14） | 规则 2：`is_error` 的结果算回答 |
| T17 | `test_a_call_is_matched_to_its_result_by_id_not_by_position`（N1） | 规则 2：按 id 配，不按位置 |
| T18 | `test_every_turn_with_an_unanswered_call_is_cleaned`（N6） | 一段历史里两处未答调用都处理 |
| T19 | `test_one_result_answers_one_call_when_two_calls_share_an_id`（N9） | 规则 2：两个同 id 的调用配一条结果，留下先发的那一个 |
| T20 | `test_a_turn_whose_only_text_is_whitespace_is_removed_with_its_call`（N2、W1）。样本是换行、制表符、全角空格和空格 | 规则 4：空白不算文字，空白按 `str.strip()` |
| T21 | `test_whitespace_text_stays_on_a_turn_that_keeps_a_call` | 规则 3：留下来的消息不改文字 |

`tests/entry/test_session.py`，经 `SessionRegistry`，后端是能报 `finish_reason` 的替身（现有的 `Scripted` 报不出，要加一个子类）：

| 编号 | 测试 | 钉住的行为 | 今天 |
|---|---|---|---|
| T8 | `test_the_message_after_a_cut_off_tool_call_reaches_the_model_without_that_call`，6 种形状（§1.5 的 A-a 至 A-f） | 截断之后的下一次请求里没有未答调用；用真实的 `encode_messages` 和 `encode_conversation` 编码，两个方言都配对完好，Anthropic 不抛错；交换 `converged`；存回去的历史干净 | 6 条全红 |
| T9 | `test_a_stored_session_that_ends_on_an_unanswered_call_works_after_a_restart` | 直接往 SQLite 写一行今天的代码会留下的历史，另建一个 app 提交消息：不需要迁移，文字留着 | 红 |
| T10 | `test_compacting_a_session_that_ends_on_an_unanswered_call_stores_it_without_the_call` | 短会话上 `/compact` 之后历史是干净的 | 红 |
| T11 | `test_a_session_with_every_call_answered_is_sent_as_it_was_stored` | 对照：并行调用的正常会话，发出去的就是存着的那些消息对象 | 绿。变异 M6、M10、M11 让它转红 |
| T12 | `test_a_cut_off_exchange_still_reports_the_turn_as_the_model_wrote_it` | 截断的那次交换自己的 `result.messages` 仍以原话结尾，`stop_reason` 是 `TRUNCATED` | 绿。它记下 L1、L2 没有做是有意的 |
| T15 | `test_leaving_a_call_out_is_logged_with_the_session_and_the_count` | 规则 10：三次交换只在去掉调用的那一次写一行，数的是调用。它用 `caplog` 把级别调到 INFO，说明不了默认配置下有没有这条输出 | 红 |
| T22 | `test_compacting_a_long_session_after_a_cut_off_turn_stores_no_placeholder` | 8 轮工具之后被截断的会话，`/compact` 摘要成功并写回：存回去的历史里没有 `MISSING_TOOL_RESULT`，最后一条是那一轮的文字 | 红。今天存回去的是那条调用加一条占位 |
| T23 | `test_a_stored_session_with_an_unanswered_call_in_its_middle_is_cleaned_too`（W7），第 3 版加的 | 未答调用在存着的历史中间、后面还有一问一答：下一次请求里没有它，两个方言都接受，存回去的历史干净。对应的存量是宽松的后端上截断之后又聊了下去、再换到严格后端的会话 | 红 |

`tests/entry/test_turn.py`：

| 编号 | 测试 | 钉住的行为 | 今天 |
|---|---|---|---|
| T13 | `test_run_turn_accepts_the_history_a_cut_off_run_handed_back` | 阻塞路径 | 红 |
| T14 | `test_compose_leaves_out_a_call_nothing_answered` | 规则 9 的后一半 | 红 |

变异。四组一共 42 处，在原型和 T1 至 T23 上跑（`SCR/r3_mutate.py`，`SCR/logs/r3_out_05_mutations.log`），文件按 SHA-256 核对恢复。40 处有测试转红，2 处存活。第 2 版的 34 处审核方重跑过，结果一致（`RV80/logs/r2_out4_planner_mutations_rerun.log`）。

第 1 版自己的 15 处，全部转红：

| 变异 | 转红的测试 |
|---|---|
| M1 `_assemble` 不清理 | T8、T9、T13、T15、T23 |
| M2 `compose` 不清理 | T10、T14、T22 |
| M3 还有文字的那一轮也整条去掉 | T1、T5、T6、T7、T9、T10、T14、T18、T22、T23 |
| M4 去空了的那一轮留着 | T2、T20 |
| M5 对话里任何位置的结果都算回答 | T6 |
| M6 所有调用都当成没被回答 | T3、T4、T7、T11、T16、T17、T18、T19、T21 |
| M7、M8 两处角色比较改成 `is` | T7 |
| M9 只看最后一条消息 | T3、T5、T6、T7、T17、T18、T19、T21、T23 |
| M10 没动过的消息也重建一份 | T4、T7、T11、T16、T18 |
| M11 只读紧跟的第一条结果 | T4、T11、T16 |
| M12 改成补占位（X2） | T8 的 5 种形状、T9、T13、T15、T23 |
| M13 留下文字的那一轮丢掉思考 | T1、T3 |
| M14 不写日志 | T15 |
| M15 日志数的是消息 | T15 |

审核方第 1 轮的 15 处（定义在 `RV80/scripts/rv80_mutate.py`）。在第 1 版的原型和测试上有 9 处存活。第 2 版改了它们所在的几行，变异按原意重写：

| 变异 | 第 1 版 | 现在 |
|---|---|---|
| N1 按位置配：有几条结果就留前几个调用 | 存活 | T17 转红 |
| N2 只含空白的文字不算文字 | 存活 | 成了规则 4。反过来的变异是 R1 |
| N3 只有思考、没有文字的一轮留着 | 转红 | T2 |
| N4 清理了、记了日志，带进去的却是原样的历史 | 转红 | T8、T9、T13、T15、T23 |
| N5 `compose` 只在有用户文字时清理 | 转红 | T10、T22 |
| N6 只处理第一处未答调用 | 存活 | T18 转红 |
| N7 空 id 的调用一律算没回答 | 存活 | T4 转红 |
| N8 非 assistant 消息上的调用也清理 | 存活 | 存活，见下 |
| N9 一条结果只回答一个调用 | 存活 | 成了规则 2。反过来的变异是 R2 |
| N10 丢了调用的那一轮，结果也一起丢 | 转红 | T3、T17、T19 |
| N11 还留着已答调用的那一轮丢了文字 | 存活 | T3、T21 转红 |
| N12 日志写在 DEBUG | 转红 | T15 |
| N13 只做压缩的交换用原样的历史建 `_Carried` | 存活 | 存活，见下 |
| N14 结果串在第一条失败的结果处断开 | 存活 | T16 转红 |
| N15 去空的那一轮连同它前面的请求一起去掉 | 转红 | T2、T20 |

第 2 版新加的 6 处，全部转红：

| 变异 | 转红的测试 |
|---|---|
| R1 空白算文字（第 1 版的做法） | T20 |
| R2 有一条结果带这个 id，带这个 id 的调用就都算回答了（第 1 版的做法） | T19 |
| R3 没有文字也没有调用的 assistant 消息一律去掉，不管有没有被去掉过调用 | T4 |
| R4 同一个 id 的第二条结果不回答任何调用（`repair_tool_pairs` 的做法） | T4 |
| R5 留下来的一轮，只含空白的文字被清成空串 | T21 |
| R6 id 重复时留下的是后发的那个调用 | T19 |

审核方第 2 轮的 8 处（定义在 `RV80/scripts/r2_mutate.py`，照原样拿来用）。在第 2 版的测试上有 4 处存活（W1、W2、W7、W8，`RV80/logs/r2_out3_mutations.log`）。第 3 版四处都补了用例，没有哪一处是靠"接受"过去的，现在 8 处都转红：

| 变异 | 第 2 版 | 现在 |
|---|---|---|
| W1 只把空格和换行当空白，制表符和全角空格算文字 | 存活 | T20 转红 |
| W2 结果必须按调用的先后出现才算回答 | 存活 | T4 转红 |
| W3 第一个带这个 id 的调用认领所有带它的结果 | 转红 | T4 |
| W4 留下来的一轮，文字被去掉首尾空白 | 转红 | T21 |
| W5 id 重复时留下的是后发的调用 | 转红 | T19 |
| W6 `/compact` 先压缩原样的历史，之后才清理 | 转红 | T22 |
| W7 只有历史以带调用的一轮结尾时才清理 | 存活 | T23 转红 |
| W8 一轮后面没有调用认领的结果被丢掉 | 存活 | T4 转红 |

存活的两处在产品路径上和原型等价，没有补测试，审核方复核时同意：

- N8。调用只出现在 assistant 消息上：`Message.tool_calls` 的 docstring 这样规定（`schema/message.py:193-195`），`Message.user`、`Message.system`、`Message.tool` 三个构造器都不接受调用，存储层按原样写回。要让这处变异现形，得手工造一条带调用的 user 消息。
- N13。只做压缩的交换不调模型，`_sequence` 在那条路径上用的是 `compose` 交出的对话（`entry/turn.py:678-680`），`_assemble` 建的 `_Carried` 没有人读。这处变异只让 `/compact` 少写规则 10 的那一行日志，而那行日志不当记录用（Q6）。

现有测试要改的只有一条：`tests/context/test_context_is_a_leaf_layer.py:473` 的 `test_the_public_surface_is_exactly_what_plan_0030_delivered`，清单里加 `"drop_unanswered_calls"`。`tests/engine/test_loop.py:408` 的 `test_a_truncated_turn_does_not_execute_the_tool_calls_it_carried` 钉的是引擎照样记下这一轮，不用改。

不加脚本化 eval。`ScriptedTurn` 没有 `finish_reason` 字段（§1.3 的 P5），加一条 eval 要先改 `omicsclaw/evals/provider.py`。T8 至 T10、T22、T23 走的就是 eval Runner 走的那段 `SessionRegistry` 路径。`tests/evals/test_dataset_floor.py` 的 `BASELINE` 不变。

### 6.3 文档

| 文件 | 改动 |
|---|---|
| `docs/core-features/agent-loop.md` | `:229` 那一段补一句：entry 层在下一次交换开场时去掉这些调用。`:499` 的已知限制改写成：`RunResult` 和 `TurnOutcome.history` 仍以没被回答的调用结尾；经 `entry/turn.py` 的路径在下一次开场时去掉它们；直接调 `engine.exchange` 的调用方要自己处理。`:444` 的伪代码里 `_Carried(history)` 一行同步 |
| `docs/core-features/context-engineering.md` | §7 的函数表（`:280` 起）加一行 `drop_unanswered_calls`，写明一条结果回答一个调用、空白（按 `str.strip()`）不算文字；`:45`、`:68`、`:94`、`:323`、`:466` 几处列函数名和 `_Carried(history)` 的地方同步 |
| `docs/core-features/progressive-compactor.md` | §9.2（`:467`）补一句：`compose` 交出的对话里没有未答调用，所以 `/compact` 即使没有写回，也会把它们从历史里去掉。§12 已知限制加一条：这种情况下 CLI 仍然打印"Nothing to compact"或"left as it was"。§13 文件索引（`:541`）同步 |
| `docs/plans/0080-truncated-exchange.md` | 加交付记录一节 |
| `CHANGELOG.md` | 合并时在顶部加一条 |

`README.md` 不动。`tests/entry/golden/` 是 system 提示和工具表，这次不变，不用重录。

估计工作量：代码和测试三小时，文档一小时。

### 6.4 给实现方

- 依据是 §5.1 的十条规则和 §6.2 的测试表。两者有出入时以规则的条文为准，并把出入报回来。
- 原型（§5.2）是参考，可以另写。草稿测试在 `SCR/r3_proposed/test_plan0080_r3_proposed.py`，是一个文件，落地时按 §6.2 分到三个测试文件里；`Finishing` 这个替身放在 `tests/entry/test_turn_runner.py` 的 `Scripted` 旁边，两个测试文件都要用。`SCR` 在 `/tmp` 下，不保证一直在，丢了就照 §6.2 的表重写。
- 先在没改的代码上跑新测试，确认是 34 红 2 绿，再改代码。
- 代码、测试、§6.1 列的 docstring 和 §6.3 的文档在一次改动里完成。
- 不在范围里的：给模型或用户的任何提示文字，`_compaction_verdict` 的措辞，`repair_tool_pairs`，两个适配器，`_model_limits.py`，引擎。
- 合并之前：§7.2 的三条命令，§7.3 的真实模型验收（3 次 DeepSeek 请求），PR 上的 `Eval CI`。

## 7. 验证

### 7.1 档位

`SPEC.md` 第 3 档。`omicsclaw.context` 是几层都 import 的包，公开面多了一个名字；发给模型的历史在一种情形下变了。provider 适配器、权限门、Desktop 线上契约都没有碰，不到第 4 档。合并到 `main` 属于第 4 档，由 `Eval CI` 跑。

### 7.2 要跑的

改动前在分支起点跑一次基线，改完再跑一次。前两条用 `rapids_singlecell` 环境；第三条是 Desktop 的用例，要 fastapi，用 `OmicsClaw` 环境。Desktop 的 `/compact` 走的也是这段代码，行为同样变了，所以第 2 版把它列为要跑的：

```
/opt/conda/envs/rapids_singlecell/bin/python -m pytest tests/context tests/planning tests/engine tests/entry tests/evals -q -p no:randomly
/opt/conda/envs/rapids_singlecell/bin/python -m pytest tests/*/test_*layer*.py tests/sdk/test_boundary.py tests/sdk/test_public_surface.py tests/test_*.py -q -p no:randomly
/opt/conda/envs/OmicsClaw/bin/python -m pytest tests/entry/test_desktop_*.py -q -p no:randomly
```

在 `5daf5482` 的暂存副本上（基线是 `SCR/logs/out_06`、`out_08`、`r2_out_12`，第 2 版原型是 `r2_out_07`、`r2_out_09`、`r2_out_13`）：

| 命令 | 基线 | 换上第 2 版原型 |
|---|---|---|
| 第一条 | 2842 passed、11 skipped、26 deselected、3 xfailed，约 130 秒 | 2841 passed、1 failed，其余相同 |
| 第二条 | 842 passed、14 skipped、1 deselected、1 xpassed，约 100 秒 | 841 passed、1 failed，其余相同 |
| 第三条 | 566 passed、3 skipped，约 9 秒 | 566 passed、3 skipped |

前两条里红的是同一条：§6.2 要改的公开面清单。审核方在第 2 版原型上把三条都重跑了，数字相同（`RV80/logs/r2_out10_level3_r2_proto.log`）。第 3 版没有改原型，没有重跑。

另外：

- 写测试时先在没改的代码上跑新测试，确认红绿和 §6.2 的表一致，再改代码。
- 变异检查：对落地的实现重做 42 处变异（`r3_mutate.py` 改一下锚点可以直接用），除 N8、N13 外每处至少一条测试转红。
- `probe_01_stuck.py` 在落地的实现上重跑，被拒绝的调用和 `failed` 的交换都应当是 0。`r2_probe_03_fuzz.py` 重跑，每一行都应当是 0。

### 7.3 真实模型的验收

owner 裁定它是合并的前置条件（Q5），3 次 DeepSeek 请求。保留什么、去掉什么是确定性的，上面的测试已经覆盖；这一步确认的是真实接口接受清理之后的请求。

做法是在实施分支上重跑 `SCR/probes/live_02_e2e.py`，3 次请求：`truncate` 一次，造出一个真实的截断会话；把工作区复制两份，`continue` 在 `main` 和实施分支上各一次。预期和 V8 至 V10 相同：`main` 上 400、`failed`、历史不变；分支上 200、`converged`、存回去的历史没有未答调用。

这次没有措辞要对比，推荐做法不加任何给模型看的文字。

## 8. 与在办事项的关系

计划 0078。下面对的是它的第 3 版（分支 `docs/plan-0078-r3`，`c9726a8a`），规则是 G+ 加 §4.1 的 2f：tail 以工具结果开头时，被 head 和 tail 的边界切开的那一轮整轮不丢。它的第 4 版正在出，这里只引用已经提交的第 3 版和派发时转述的一条裁定：

- 没有顺序依赖，谁先落地都可以。
- 改的不是同一个函数。0078 改 `fit_to_budget`，新增私有函数 `_held`、`_round_the_tail_finishes`、`_request_of_oldest_reply`，从 `.summary` 引入 `is_summary_message`。本计划新增 `drop_unanswered_calls`，从 schema 多引入 `ToolCall`，改 `entry/turn.py` 的 `compose` 和 `_assemble`。两边都动的文件是 `omicsclaw/context/transcript.py`、`tests/context/test_transcript.py`、`docs/core-features/context-engineering.md` §7 的表、`docs/core-features/progressive-compactor.md` §12 和 `CHANGELOG.md` 的顶部，动的都是不同的行。
- 两份原型的 `transcript.py` 用 `diff3 -m` 三方合并，没有冲突。合并出来的树上，本计划的 36 个用例和 0078 第 3 版的 35 个用例都通过（`SCR/r2_combo/`，`SCR/logs/r3_out_14_combo_0080_tests.log`、`r2_out_15_combo_0078_tests.log`）。审核方两轮各做了一次，第 2 轮用的也是 0078 第 3 版的原型，结果相同（`RV80/logs/rv80_out7_combo_tests.log`、`r2_out6_combo_tests.log`）。
- 0078 的降级截断末尾会过 `repair_tool_pairs`，对"输入里本来就没应答的调用"补占位（它的 R5）。本计划落地后，会话历史在进压缩器之前已经清理过，这个来源的占位不再出现；T22 钉的是摘要成功那条路径上的同一件事。
- 本计划可能让历史里出现两条相邻的请求（`U U`）。0078 的规则 4 和边界情形 E7a 已经覆盖：后面没有回复的请求轮到时直接丢，它的 T20 钉着。
- 被截断的交换清理之后，可能以一轮工具调用结束、没有最终答复（§5.2 的 A-d：`U a1 t`，然后是下一条请求）。0078 第 3 版的 R11 说的就是这种交换：它的请求在没有写回的降级视图里可能独自留下。审核方在两份原型合并出来的树上看到了这一情形（`RV80/logs/r2_out7_r11_source.log`）。派发第 3 版时转述的裁定是：owner 已决定把 R11 的改法并进 0078 的定稿，请求在它最后一条 assistant 回复被丢的那一步一起丢。按这句话推断，本计划清理出来的这种交换不会再留下一条孤零零的旧请求；这一点没有在 0078 新的原型上跑过。本计划不依赖它先落地。

规划闸门（`docs/plans/0039-planning-layer.md` §9，已在基线里）。

- `_gate_fires` 往回数到最近一条 user 消息为止（`planning/injector.py:228-232`）。本计划不往历史里加 user 消息，去掉的 assistant 消息属于上一次交换，在这条边界之外，计数不受影响。
- §9.3 的前提是"会进入持久历史的 user 消息只有两种"。X4 和"往历史里放一条截断说明"的做法会破坏它，这是 §4.1 没有选它们的原因之一。

计划 0079 已搁置，和本计划无关。它推荐的首条消息占位在 provider 适配器里，本计划不碰适配器。

排期。owner 2026-10-09 批准了本计划，实现在定稿后派发。它不需要等 0078。Q4 另开的那一项编号预留 0081，由别的计划方写。

## 9. 风险和没验证的部分

- K1 修完之后，触发截断的那个任务会不声不响地原样重来。审核方在真实 DeepSeek 上补测了 8 次，system 提示和工具表是产品自己的，历史是按清理后的形状手工搭的，每次回复限 1,500 token，只读回复的开头（`RV80/logs/rv80_out13_live_a.log`、`rv80_out13_live_b.log`）。看到的两件事：
  - 留下的那句文字（"现在把说明写入 …"）没有让模型以为文件已经写了。留着文字的 5 次里，没有一次把文件当成写过了：4 次明确写出调用没有发生，`x1_rich_go` 那 1 次没有提，直接着手去写。
  - 用户说"继续"时两种做法各 1 个样本。X1 下模型的思考里没有提到截断，打算一次调用写完约 3000 字。X2（补一条"被输出上限截断、没有执行"的结果）下模型的思考里写了上次是被输出上限截断、这次有再撞上的风险、要控制篇幅，随后仍打算一次调用写完。证据只支持到这里：模型知道上次被截断，知道有再撞上的风险。篇幅有没有真的变小看不到，那次回复在动笔之前就结束了。审核方复核时同意这个读法。
  
  这说明 X1 之下内容确实超过上限时会再截一次，再花一次上限那么多的输出 token，用户仍然看不到原因。样本量是 1 对 1，不够定措辞。owner 裁定先接受这一点（Q9）。
- K2 用户仍然看不到截断的说明。本计划没有改这一点。
- K3 Anthropic 一侧没有对真实接口核实。依据是文档里的配对规则、适配器代码和第三方的报错原文。接口如果比文档宽松，本计划的清理对它是多余的，没有害处。
- K4 OpenAI 兼容后端只测了 DeepSeek。`U U`、`a t U` 这两种相邻今天有计划块的会话每次调用都在发，别的后端如果拒绝，今天就已经在拒绝。
- K5 频率只在一个工作区的库上量过，样本很薄（§3）。默认上限 64K 来自 DeepSeek 文档，实测只确认了它高于 12,934。
- K6 `omicsclaw.context` 的公开面多一个函数。`compose` 的名字和签名不变，行为在一种输入上变了：带未答调用的历史。仓库外有没有调用方依赖旧行为不知道；想不出依赖它的用法，因为引擎从不执行历史里的调用。
- K7 重复 id 的语义（规则 2）在产品路径上走不到，没有真实数据能说明哪种读法更好；owner 裁定用一条对一条（Q10）。清理之后 `repair_tool_pairs` 仍可能给重复 id 的一轮补占位：20 万段随机历史里有 1,271 段，每一段都是同一轮里有重复的 id（`SCR/logs/r2_out_16_fuzz.log`）。那是 `repair_tool_pairs` 自己对重复 id 的处理，见本节末尾。
- K8 "已经回答、参数却解析不了"的调用，本计划不处理，它在 Anthropic 方言上仍然发不出去。它有两个来源。
  - 修复之前已经被压缩补过占位的截断调用。要同时满足流式截断、截断时压力已在 SOFT 以上或对长会话用过 `/compact`。已知 0 例。本计划落地后不会再产生，因为清理在压缩之前。
  - 不算截断的中断。`_TRUNCATING_FINISH_REASONS` 只有 `length` 和 `max_tokens`（`loop.py:93`）。`finish_reason` 是 `tool_calls`、`content_filter`、`insufficient_system_resource`、`refusal`、`pause_turn` 而回复里带着半截调用时，引擎把调用交给工具层，工具层回一条 `is_error` 的结果（`ToolArgumentError: the arguments were not valid JSON`），调用算被回答了，模型在同一次交换里接着做。OpenAI 方言下这次交换正常结束，历史里留下 `A+a1~ t`；Anthropic 方言下适配器在同一次交换的下一次调用上本地抛错，试满 3 次后 `failed`，什么都不提交，不卡死（`SCR/logs/r2_out_10_facts_today.log` 的 c 段；审核方的 `RV80/logs/rv80_out9_paths_base.log` 还跑了 `''`、`aborted`、`model_context_window_exceeded`，结果相同）。
  
  残余的卡死路径是：这样的历史在 OpenAI 方言下产生，之后换到 Anthropic 方言续接同一个会话。出路是 `/clear` 或 `/new`。owner 裁定这次不立项（Q8）。
- K9 channel 没有跑。它和 CLI 走同一个 `SessionRegistry`，T8 至 T10、T22、T23 覆盖的是这一段共用路径。Desktop 的用例跑了（§7.2），真实的 Desktop 会话没有跑。
- K10 规则 10 的日志在 CLI 和 Desktop 上默认没有输出，只有 `oc channel` 会把它打到 stderr（Q6），所以落地之后仍然没有手段知道它在用户那里发生过几次。

这次顺带看到、不在本计划范围里的。前两条 owner 裁定这次不立项，只记录（Q7、Q8）：

- Anthropic 方言的输出上限表落后于模型（§3）。补它的时候要算上这个数的另外两处用途：它是上下文预算的输出预留（`entry/assembly.py:663`）；它进每一次阻塞的 `generate`，摘要器（`entry/assembly.py:711`）和 `run_turn` 都是，而参考文档写 128K 这么大的 `max_tokens` 要用流式请求。OpenAI 方言没有给用户设输出上限的入口；`_model_limits.py:89` 里 `deepseek-v4-flash` 的输出上限是 8,192，表里的意思是"未知"，和文档上的 64K 默认值对不上。
- 不算截断的中断怎么处理（K8 的第二个来源）。Anthropic 文档列了 7 个停止原因，引擎只把 `max_tokens` 当截断。`model_context_window_exceeded` 的说明是 "Claude stopped because it reached the model's context window limit"。随 Claude Code 分发的参考对 `refusal` 的说法是它可能把 `tool_use` 截在一半，那一轮的工具不要执行（`shared/tool-use-concepts.md`）。今天这两种情况下半截调用都会交给工具层。
- `repair_tool_pairs` 对重复 id 的处理会丢真实的结果。两个空 id 的调用配两条结果，是厂商不给 id 时引擎自己写出来的形状（§5.1），`repair_tool_pairs` 留下第一条结果，把第二条换成占位（`SCR/logs/r2_out_10_facts_today.log` 的 e 段；审核方的 `RV80/logs/r2_out5_repair_duplicate_ids.log`）。它只在压缩删了消息的路径上跑：压力到 SOFT 以上，或者对够长的会话用 `/compact`。
- 没有配置日志时，`omicsclaw.entry` 在 Desktop 上哪一级都不输出，原因和细节在 Q6。CLI 上 INFO 和 WARNING 不产生，ERROR 进一个内存缓冲。`exchange started`、`exchange ended` 这两行现有的 INFO 日志也是这样。

## 10. 裁定

### 10.1 已裁定

owner 2026-10-09 批准了计划，并就第 2 版列的 Q1 至 Q10 作了裁定。实现在定稿后派发。

1. Q1 截断留下的调用：去掉未答调用，留文字；只含空白的文字不算文字。条文是 §5.1 的规则 3、4。
2. Q2 位置：只在交换开场清理。条文是规则 9。
3. Q3 函数放在 `omicsclaw/context/transcript.py` 的公开面。
4. Q4 另开一项，范围只有"截断时 CLI 显示一句"：只是显示，不碰给模型的文字和 Desktop 的线上契约。由别的计划方另写，编号预留 0081。给模型的提示、循环内续写这次没有立项，只记录（§4.1 的 X4，K1）。
5. Q5 真实模型验收（3 次 DeepSeek 请求）是合并的前置条件。做法在 §7.3。
6. Q6 规则 10 的那行日志留在 INFO，不当记录用。它在哪里看得到见下面的事实。
7. Q7（Anthropic 输出上限表）、Q8（不算截断的中断）这次没有立项，只记录在 §9 末尾的范围外观察里。
8. Q9 修完之后"继续"会不声不响地原样重来（K1），这一点先接受。
9. Q10 重复 id：一条结果回答一个调用，按调用先后认领。条文是规则 2。

Q6 的事实。第 1 版说这行日志是"今后知道这件事发生过几次的唯一落盘记录"，不成立。第 2 版改了大半，Desktop 那一条仍然写错，这一版再改（`SCR/logs/r2_out_11_log_levels.log`，`r3_out_10_facts.log` 的 b 段，`r3_out_15_facts_base.log`；审核方的 `RV80/logs/rv80_out17_log_levels.log`、`r2_out8_desktop_warning.log`、`r2_out8_desktop_stderr.txt`）：

- CLI 占着终端时，`terminal_owned_logging` 把 `omicsclaw.entry` 压到 ERROR，根 logger 的处理器换成一个内存缓冲（`entry/cli/_screen.py:179-213`，`launch/_surfaces.py:618`）。INFO 和 WARNING 都不产生，ERROR 进那个缓冲。
- Desktop 用 `uvicorn.Config(log_level="info")`（`launch/_surfaces.py:1245-1247`），它不动根 logger。INFO 级别不够，不产生。WARNING 和 ERROR 级别够，也没有输出：`omicsclaw.entry` 上挂着一个 `NullHandler`（`entry/assembly.py:193`），而 logging 的兜底处理器只在整条链上一个处理器都没有时才用。照 `oc desktop` 的方式配置日志之后，从 `omicsclaw.entry.turn` 发 INFO、WARNING、ERROR 各一条，stderr 上一条都没有；同一时刻，一个链上没有任何处理器的 logger 发的 WARNING 到了 stderr。在基线和原型的树上各跑了一遍，结果相同。第 2 版写的"WARNING 会经兜底处理器打到 stderr"是只量了 `isEnabledFor` 得出的，级别够不等于有输出。审核方上一轮"升到 WARNING 后 Desktop 会打到 stderr"出自同一个推断，它撤回了。
- 只有 `oc channel` 调了 `basicConfig(level=INFO)`（`launch/_surfaces.py:1373-1376`），根 logger 有了处理器，INFO 会打到 stderr。
- T15 用 `caplog` 把级别调到了 INFO，所以它是绿的，说明不了默认配置下有输出。

所以这行日志默认只有 channel 部署看得到，而且在 stderr 上，谈不上落盘。它的用处是有人配置了日志来排查时，能看到发出去的历史和存着的不一样是这里造成的。

### 10.2 仍要 owner 定的

没有。

## 11. 各版之间改了什么

### 11.1 第 1 版到第 2 版

推荐的做法、位置和改动的文件没有变。审核方核实成立、这一版没有动的：根因链，X1 加 L3，与 0078 和规划闸门没有顺序依赖，没有绕过清理的模型调用路径，没有误伤，94 处代码位置（`RV80/logs/rv80_out15_locations.log`），第 1 版的 15 处变异。

| 审核意见 | 核实的结果 | 处理 |
|---|---|---|
| P1-1 提议的测试钉不住规则，它的 15 处变异有 9 处存活 | 属实。用它的脚本在第 1 版原型上重跑，同样是 9 处（`r2_out_04_reviewer_mutations_on_r1.log`） | 加 T16 至 T22，改 T3，T4 加 3 种形状。34 处变异重跑，存活 2 处（N8、N13），逐条说明在 §6.2。长会话 `/compact` 不留占位由 T22 钉住 |
| P1-2 Q6 的前提不成立 | 属实。自己的探针结果和审核方相同（`r2_out_11_log_levels.log`） | Q6 按事实重写；§0、§5.3、K10 里和它有关的说法都改了；T15 的说明加了一句 |
| P1-3 §4.1 对 X5 的说法会误导 | 属实。表里的数是适配器发的，不是模型的上限；自己空跑了一遍（`r2_out_10_facts_prototype.log` 的 a 段） | §3 加了对照表，§4.1 的 X5 重写，补表列为 Q7 和范围外观察 |
| P2-1 修完之后任务会原样重来 | 读了 8 次请求的回复原文。留着文字的 5 次里没有一次把文件当成写过了（细分见 §11.2）。转给我的意见里说 X2 下模型会主动缩小篇幅，我读到的是它意识到风险，没有看到篇幅变小 | 写进 K1，样本量和这处读法的出入都照实写了；列为 Q9 |
| P2-2 别的中断方式没有查完 | 属实。自己跑了 5 种停止原因和 Anthropic 一侧（`r2_out_10_facts_today.log` 的 c 段），结果和审核方一致 | K8 重写，写明两个来源和残余的卡死路径；列为 Q8 |
| P2-3 规则 3 的"文字"没定义空白 | 属实。第 1 版原型会留下只含空白的一轮。依据是第三方的报错原文，没有在真实接口上核实 | 规则 4 写死空白不算文字；原型改成 `content.strip()`；T20、T21、变异 R1、R5 |
| P2-4 R7 对 `repair_tool_pairs` 的描述不准 | 属实。两个函数只有相邻这一半相同 | §5.1 改了措辞，规则 2 定下一条结果回答一个调用；原型改了；T19、变异 R2、R4、R6；列为 Q10 |
| P2-5 Desktop 的用例该跑 | 属实 | §7.2 加了第三条命令，基线和第 2 版原型都是 566 passed、3 skipped |
| P3 两处 docstring 落地后不成立 | 属实，另找到一处：`SessionRegistry.compact`（`entry/session.py:400-402`） | §6.1 列了三处 |
| P3 短会话上 CLI 仍打印"Nothing to compact" | 属实（`r2_out_10_facts_prototype.log` 的 g 段）。另有一句"left as it was"同样对不上，g 段没有跑到它，证据是审核方第 2 轮的 `r2_out11_compact_texts.log` | §5.4、§6.3 |
| P3 规则 3 的范围有歧义 | 属实 | 规则 4 写死只管被去掉过调用的消息；T4 的新形状和变异 R3 |
| P3 今天的占位等于告诉模型调用跑过了 | 属实（`r2_out_10_facts_today.log` 的 d 段） | §1.2、§4.1、§5.3 |
| P3 "无法恢复"指的是会话上下文 | 属实 | §0、§1.4、§5.4 |
| 频率和优先级的写法 | 数字无误，样本薄这一点第 1 版没有写 | §0、§3 重写；优先级只写了我的看法 |
| §10 不要写成已裁定 | | Q1 至 Q6 并列两边的建议，加 Q7 至 Q10 |
| 对着 0078 第 3 版重新核一遍 | 用它的第 3 版原型重做了三方合并 | §8 重写，多了 R11 那一条 |

编号的变化：风险从 R 改成 K，因为 R1 至 R6 给了第 2 版的变异。第 1 版的规则 3 现在是规则 4，规则 8、9 现在是规则 9、10。

### 11.2 第 2 版到第 3 版

规则、原型和改动的文件没有变。复核方重跑后和第 2 版一致的：只照条文另写的实现在 20 万段随机历史上和原型差异 0；33 个用例今天 31 红 2 绿、原型全绿；34 处变异 32 处转红，N8、N13 存活，它同意是等价的；§7.2 的三条命令；与 0078 第 3 版原型的三方合并没有冲突。

| 复核意见或裁定 | 核实的结果 | 处理 |
|---|---|---|
| 必须改：Desktop 上 WARNING 会打到 stderr，不成立 | 属实。自己在原型和基线的树上各跑了一遍，三条记录在 stderr 上一条都没有，对照的那一条有（`r3_out_10_facts.log` 的 b 段，`r3_out_15_facts_base.log`） | §10.1 的 Q6 事实、K10、§9 末尾 |
| owner 的裁定 | | §10 分成已裁定和仍要定的；状态行、摘要、§4.1、§5.1、§7.3、§8、§9 里"待裁定"的说法都按裁定改了 |
| K1 两处措辞 | 重读了回复原文，属实：4 次明确写出没有调用，`x1_rich_go` 没有提 | K1；去掉了加在转述上的引号 |
| 四处存活的变异 W1、W2、W7、W8 | 属实，在第 2 版的测试上都存活 | 四处都补了用例：T20 换样本，T4 加两种形状，新增 T23。42 处变异重跑，存活的只剩 N8、N13 |
| 规则 4 写明空白的定义 | | 规则 4、§5.1 的说明、§6.3 |
| "要压力到 SOFT 以上才会碰到"不全 | 属实，够长的会话用 `/compact` 也走修复 | §9 末尾 |
| "`ollama` 预设给空 id"没有人实测 | 属实。代码事实是厂商不给 id 时两个适配器都留空 | §5.1、§9 末尾 |
| g 段只覆盖 "Nothing to compact" | 属实 | §5.4 和 §11.1 的那一行另引了审核方的 `r2_out11_compact_texts.log` |
| "4.7 之后的每个型号"不全 | 属实。自己跑了 `claude-opus-4-5`、`claude-opus-4-1`，也是 8,192（`r3_out_10_facts.log` 的 a 段） | §0、§3 的表、§4.1 |
| 范围外观察补两句 | 核对了 `entry/assembly.py:711` 和参考文档原文 | §4.1、§9 末尾 |
| 用 DeepSeek 的回复大小比 Anthropic 的 8,192 | 属实，只能当估计 | §0、§3 |
| 0078 那边 R11 的改法并进定稿 | 它的第 4 版没有提交，没有读到 | §8 那一条只引用转述的裁定，推断的部分标明没有跑过 |

另外加了 §6.4，写给实现方。

## 12. 证据清单

目录：`SCR`，即 `/tmp/claude-0/-workspace-dataset-private-zhouwg-data-OmicsClaw/9885f10a-3799-4309-9dfb-0981c166422a/scratchpad/plan-0080/`。`base/` 是 `git archive 5daf5482` 的导出。`proto/` 是第 1 版原型，`r2_proto/` 是第 2 版原型，各由 `apply_prototype.py`、`r2_apply_prototype.py` 改三个文件，原文留在旁边的 `.orig` 里。`proto_mut/`、`r2_proto_mut/`、`r3_proto_mut/` 是做变异用的副本，做完和原型用 `diff -rq` 核对相同。第 3 版没有新的原型，`r3_proto_mut/` 是 `r2_proto/` 的副本。`r2_combo/` 是第 2 版原型加上 0078 第 3 版原型的 `transcript.py`。探针用 `/opt/conda/envs/rapids_singlecell/bin/python` 跑，`PYTHONPATH` 指向要测的那棵树，每份输出的第一行是 `omicsclaw.__file__`。要用 `openai` SDK、uvicorn 或 fastapi 的用 `/opt/conda/envs/OmicsClaw/bin/python`，同样由 `PYTHONPATH` 指向这些树。

第 3 版（输出都在 `logs/` 下）：

| 脚本 | 输出 | 内容 |
|---|---|---|
| `r3_proposed/test_plan0080_r3_proposed.py` | `r3_out_03_proposed_base.log`、`r3_out_03_proposed_r2_proto.log` | §6.2：T1 至 T23，在今天和原型上 |
| `r3_mutate.py` | `r3_out_05_mutations.log` | §6.2：42 处变异 |
| `probes/r3_probe_04_facts.py` | `r3_out_10_facts.log`、`r3_out_15_facts_base.log` | §3 的表、Q6：a、b 段，在原型的树上；后一份是 b 段在基线上 |
| `r3_proposed/test_plan0080_r3_proposed.py`，在 `r2_combo/` 上 | `r3_out_14_combo_0080_tests.log` | §8：与 0078 第 3 版原型合并后的树 |

第 2 版：

| 脚本 | 输出 | 内容 |
|---|---|---|
| `r2_apply_prototype.py` | | 第 2 版原型 |
| `r2_proposed/test_plan0080_r2_proposed.py` | `r2_out_03_proposed_base.log`、`r2_out_03_proposed_proto.log`、`r2_out_03_proposed_r2_proto.log` | 第 2 版的 T1 至 T22，在今天、第 1 版原型、第 2 版原型上。数字已被第 3 版的取代 |
| `RV80/scripts/rv80_mutate.py`，只读使用 | `r2_out_04_reviewer_mutations_on_r1.log` | §6.2：审核方的 15 处变异在第 1 版上，9 处存活 |
| `r2_mutate.py` | `r2_out_05_mutations.log` | 第 2 版的 34 处变异。已被第 3 版的取代 |
| `probes/probe_01_stuck.py` | `r2_out_02_stuck_prototype.log` | §5.2：第 2 版原型上的 A 至 D 段 |
| 现有测试 | `r2_out_07_layers_prototype.log`、`r2_out_09_guards_prototype.log`、`r2_out_12_desktop_baseline.log`、`r2_out_13_desktop_prototype.log` | §7.2 |
| `probes/r2_probe_02_facts.py` | `r2_out_10_facts_today.log`、`r2_out_10_facts_prototype.log`、`r2_out_11_log_levels.log` | §1.2、§3、§5.1、§5.4、K8、Q6：a 至 g 段 |
| `probes/r2_probe_03_fuzz.py` | `r2_out_16_fuzz.log` | §5.2、K7：20 万段随机历史 |
| `diff3 -m`，文件在 `r2_combo_tests/` | `r2_out_14_combo_0080_tests.log`、`r2_out_15_combo_0078_tests.log` | §8：与 0078 第 3 版原型合并。前一份是第 2 版的用例，已被 `r3_out_14` 取代 |

第 1 版仍在引用的：

| 脚本 | 输出 | 内容 |
|---|---|---|
| `probes/p80_common.py` | | 脚本化 provider、替身后端、两个方言的线上形状检查、装配 |
| `probes/probe_01_stuck.py` | `out_01_stuck_today.log` | §1.2、§1.5：今天的 A 至 E 段 |
| 现有测试 | `out_06_layers_baseline.log`、`out_08_guards_baseline.log` | §7.2 的基线 |
| `probes/freq_01_db.py`、`freq_02_sizes.py` | `freq_01_db.log`、`freq_02_sizes.log` | §3：主检出会话库的副本（`dbcopy/`） |
| `probes/live_00_settings.py` | `live_00_settings.log` | §3：`.env` 里非密钥的设置，不发请求 |
| `probes/p80_live.py`、`probes/live_01_shapes.py` | `live_01a_shapes_first_run.log`（V1 至 V3）、`live_01b_shapes_second_run.log`（V4、V5）、`live_01c_ceiling_first_try_probe_bug.log`（V6）、`live_01c_ceiling.log`（V7）；`live_01_cut_tool_call_message.json` 是 V1 返回的消息原文 | §2.1 |
| `probes/live_02_e2e.py` | `live_02a_truncate_today.log`（V8）、`live_02b_continue_today.log`（V9）、`live_02c_continue_prototype.log`（V10） | §1.5、§2.1 |
| | `ledger.txt` | 10 次真实请求，一行一次。V3 那一行是手工补的，当时探针还没有处理传输错误 |

第 1 版的 `proposed/test_plan0080_proposed.py`、`mutate_prototype.py` 和 `out_02` 至 `out_05`、`out_07`、`out_09` 留在原处，数字已被第 2 版的取代。

密钥从主检出的 `.env` 读进探针进程，没有打印，没有写进任何文件。真实交换里工具在只读权限模式下运行，工作区在 `SCR/runs/live/`。

只读引用的审核方证据，`RV80/logs/` 下：`rv80_out1_fuzz.log`、`rv80_out5_mutations.log`、`rv80_out7_combo_tests.log`、`rv80_out8_freq_db.log`、`rv80_out9_paths_base.log`、`rv80_out10_tiers_base.log`、`rv80_out12_payload_dry.log`、`rv80_out13_live_a.log`、`rv80_out13_live_b.log`、`rv80_out14_layers_proto.log`、`rv80_out15_locations.log`、`rv80_out16_desktop_proto.log`、`rv80_out17_log_levels.log`、`rv80_out18_real_sessions_identity.log`、`rv80_ledger.txt`，以及 `RV80/scripts/rv80_mutate.py`。它第 2 轮的：`r2_out1_fuzz.log`、`r2_out2_proposed_base.log`、`r2_out2_proposed_r2_proto.log`、`r2_out3_mutations.log`、`r2_out4_planner_mutations_rerun.log`、`r2_out5_repair_duplicate_ids.log`、`r2_out6_combo_tests.log`、`r2_out7_r11_source.log`、`r2_out8_desktop_warning.log`、`r2_out8_desktop_stderr.txt`、`r2_out9_anthropic_max_tokens.log`、`r2_out10_level3_r2_proto.log`、`r2_out11_compact_texts.log`，以及 `RV80/scripts/r2_mutate.py`。

更早的既有证据：`RV79/rv_out3_deepseek.log`（r4、r5）、`rv_out2c_truncated.log`、`rv_out2d_force_sqlite.log`，以及计划 0079 的 §2.3、§2.4（`git show docs/plan-0079-first-message-shape:docs/plans/0079-first-message-shape.md`）。计划 0078 第 3 版读的是 `git show docs/plan-0078-r3:docs/plans/0078-fallback-truncation-order.md`，它的原型和提议测试在 `SCR` 同级的 `plan-0078-r3/`（`r3_proto/`、`r3_test_proposed.py`）。

官方文档页面：`api-docs.deepseek.com/api/create-chat-completion`，`platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls`、`…/build-with-claude/handling-stop-reasons`、`…/api/messages/create`。页面是经抓取工具读的，引文以页面为准。模型上限出自 `claude-api` skill 的 `shared/models.md`，`refusal` 那一句出自同一个 skill 的 `shared/tool-use-concepts.md`。只含空白的 text block 那条报错取自搜索结果的摘要，原页面没有逐个打开。

## 13. 实施记录

2026-10-09，实现方写。分支 `fix/unanswered-tool-calls` 从计划分支的 tip `fe684b26` 开出，`fe684b26` 的代码和 `main` 的 `5daf5482` 相同。脚本和日志在 `SCR` 同级的 `impl-0080/`，下面记作 `IMP`，日志在 `IMP/logs/`。`SCR` 里的东西只读使用。探针复制到 `IMP/probes/` 再跑，内容没有改，因为 `p80_live.py` 把请求记在自己旁边的 `logs/ledger.txt` 里。

### 13.1 提交和文件

| 提交 | 内容 |
|---|---|
| `c17a9062` `fix(entry): drop unanswered tool calls when an exchange opens` | `drop_unanswered_calls`，`compose` 和 `_assemble` 的两处调用，日志，docstring，T1 至 T23，公开面清单，三份 core-features 文档 |
| `4e4a74d3` `test(context): pin six mutations of the unanswered-call cleaning` | 追加的 6 个用例 T24 至 T29（§13.5） |
| `17700f03` `test(context): say where plain string roles come from` | T7 的 docstring 一行（§13.8 第 6 条） |
| 本提交 | 这一节、状态行、编号行 |

动过的文件：

- 生产代码：`omicsclaw/context/transcript.py`、`omicsclaw/context/__init__.py`、`omicsclaw/entry/turn.py`；`omicsclaw/entry/session.py` 只改了 `SessionRegistry.compact` 的 docstring。
- 测试：`tests/context/test_transcript.py`、`tests/context/test_context_is_a_leaf_layer.py`、`tests/entry/test_session.py`、`tests/entry/test_turn.py`、`tests/entry/test_turn_runner.py`。
- 文档：`docs/core-features/agent-loop.md`、`context-engineering.md`、`progressive-compactor.md`，和本文件。

`omicsclaw/engine/`、`omicsclaw/provider/`、`omicsclaw/memory/`、`repair_tool_pairs`、`entry/cli/`、`entry/channel/` 没有动。`CHANGELOG.md` 没有写，留给合并时。

### 13.2 与计划的差异

1. 函数体用的是 §5.2 原型的写法，没有另写。docstring 重写了：原型写的 "Both API dialects reject a request that carries it" 改成 DeepSeek 返回 400、Anthropic 的文档写着同样的要求，因为 Anthropic 一侧没有在真实接口上核实（K3）；另外写明了空白按 `str.strip`、`is_error` 的结果算回答、返回的是新 tuple、为什么不用 `repair_tool_pairs` 的占位。
2. §6.1 没有列的一处 docstring：`transcript.py` 的模块 docstring 写着 "Nothing here truncates a message"，新函数会从一条消息上去掉调用，所以在它末尾加了一段，说明这是本模块唯一改动单条消息的函数，去掉的是整个调用。§6.1 列的六处（`compose`、`_assemble`、`TurnOutcome.history`、`TurnRunner`、`_compact_only`、`SessionRegistry.compact`）都改了。
3. 测试落地时和草稿不一样的地方：
   - 草稿经模块属性取函数，`tests/context/test_transcript.py` 按这个文件的惯例直接 import。所以在没改生产代码时，这个文件是整个收集失败，逐条的红绿是用一份只改了 import 的副本量的（`IMP/make_red_copy.py`，§13.3）。
   - 两个测试文件共用的东西放在 `tests/entry/test_turn_runner.py`：`Finishing`、`CUT_ARGUMENTS`、`tool_call`、`requesting`、`result_for`、`unanswered_calls`、`assert_both_dialects_accept`。`tests/context/test_transcript.py` 里是另一套带下划线的私有帮助函数，没有跨到 `tests/entry` 去 import。
   - `unanswered_calls` 和 `assert_both_dialects_accept` 按规则 2 一条结果对一个调用来数，草稿按"有没有带这个 id 的结果"数。在这些测试的输入上两种数法结果相同。
   - T15 多断言了日志级别是 INFO。草稿靠 `caplog` 的级别挡住 DEBUG，把日志写成 WARNING 时测不出来（S2）。
   - T13、T14 用 `tests/entry/test_turn.py` 自己的 `make_app`（默认工具表，memory 打开），草稿用的是 `test_turn_runner.py` 的。T7 用 `dataclasses.replace` 把角色换成 `str`，和同一个文件里 `repair_tool_pairs` 的测试做法相同。T11 的断言从"切片相等加逐个 `is`"改成"长度相等加逐个 `is`"。
   - 测试的 docstring 里没有写 T 编号，编号和测试名的对应以 §6.2 和 §13.5 的表为准。
4. 追加了 6 个用例（T24 至 T29），原因和对应的变异在 §13.5。
5. 文档比 §6.3 多写的：`progressive-compactor.md` §12 的那一条把 Channel 也写上了，因为 `entry/channel/commands/builtins.py` 的 `_describe_compaction` 有同样的两句话，§5.4 只提了 CLI。两处的文字本身都没有改。`context-engineering.md` §7 除了表里的一行，另加了一段规则；§8.2 的要点加了一条。
6. 公开面清单那条测试，除了清单里加一个名字，docstring 加了一句。

规则条文和 §6.2 的测试表之间，没有发现互相矛盾的地方。条文里写了而表里的测试没有钉住的有四处：规则 2 的两处（S3、S9），规则 3 的一处（S5），规则 4 的一处（S4）。另外 S1 对应的是 `transcript.py` 模块自己的约定（返回值都是新 tuple，不是调用方的列表），S10、S13 对应的是规则 9 在 `stream_turn` 这条路径上。都补了用例，见 §13.5。

### 13.3 先红后绿

| 步骤 | 结果 | 日志 |
|---|---|---|
| 草稿 `SCR/r3_proposed/test_plan0080_r3_proposed.py` 原样在 `fe684b26` 上 | 34 failed、3 passed。3 条里有 1 条是打印 `omicsclaw.__file__` 的 | `01_draft_on_unchanged_tree.log` |
| 落地的三个测试文件，生产代码没改 | `tests/context/test_transcript.py` 收集失败：`ImportError: cannot import name 'drop_unanswered_calls'` | `02_landed_tests_before_the_change.log` |
| 同上，`test_transcript.py` 换成只改了 import 的副本 | 36 个新用例 34 红 2 绿，绿的是 T11、T12。红的 21 条是函数不存在，另外 13 条是未答调用到了模型或存进了历史、压缩给它补了占位、没有那行日志。日志里多出的 1 条 passed 是 `-k` 顺带选中的既有测试 `test_a_tool_call_left_unanswered_gets_a_placeholder_result` | `03_landed_tests_before_the_change_per_case.log` |
| `c17a9062` | 36 个新用例和公开面清单那一条都绿 | `04_landed_tests_after_the_change.log` |
| `4e4a74d3` | 新用例 42 个，都绿 | `21_mutations_on_4e4a74d3.log` 的第 2 行 |

### 13.4 第 3 档的三条命令和两个探针

基线是 `git archive fe684b26` 的导出（`IMP/base/`），分支是这个 worktree。每份日志的开头是 `omicsclaw.__file__`。

| 命令 | 基线 | `c17a9062` | `4e4a74d3` |
|---|---|---|---|
| 第一条 | 2842 passed、11 skipped、26 deselected、3 xfailed，114 秒 | 2878 passed，其余相同，115 秒 | 2884 passed，其余相同，114 秒 |
| 第二条 | 842 passed、14 skipped、1 deselected、1 xpassed，85 秒 | 相同，87 秒 | 没有重跑 |
| 第三条 | 566 passed、3 skipped，8 秒 | 相同，9 秒 | 没有重跑 |

基线的三个数和 §7.2 写的一致。第一条多出来的 36 和 42 就是新增的用例。日志是 `10_baseline_*.log`、`11_branch_c17a9062_*.log`、`12_branch_4e4a74d3_1_layers.log`。第二、三条没有在 `4e4a74d3` 上重跑：`4e4a74d3` 和 `17700f03` 只改了 `tests/context/test_transcript.py` 和 `tests/entry/test_turn.py`，这两条命令不收集它们。`17700f03` 之后只跑了 `tests/context/test_transcript.py`，62 passed。`tests/entry/test_cli_repl.py::test_an_approval_nobody_answered_does_not_outlive_its_exchange` 靠 `asyncio.sleep(0)` 让出执行，机器负载高时在别的分支上偶发失败过，在这几次里都过了。

`probe_01_stuck.py` 的 A 至 D 段在 `4e4a74d3` 上：27 次交换都是 `converged`，被替身后端拒绝的调用 0 次，`failed` 0 次，D 段没有抛错，Anthropic 请求都组得出来。输出除第一行外和 `SCR/logs/r2_out_02_stuck_prototype.log` 逐行相同（`30_stuck_on_branch.log`）。

`r2_probe_03_fuzz.py`，20 万段，种子 80，在 `4e4a74d3` 上：152,960 段被改动，十行检查都是 0，`repair_tool_pairs` 之后仍补占位的 1,271 段都是同一轮里有重复 id 的，和 `SCR/logs/r2_out_16_fuzz.log` 的数字相同（`31_fuzz_on_branch.log`）。

### 13.5 变异和追加的用例

变异在 `git archive` 的导出上做（`IMP/mut/`），不在 worktree 里做，脚本是 `IMP/mutate_landed.py`，每处做完按 SHA-256 核对恢复。测试只选新增的用例。

计划的 42 处，在 `c17a9062` 和 `4e4a74d3` 上各跑一遍，结果相同：40 处转红，存活的是 N8、N13。每处转红的测试和 §6.2 的表一致，`4e4a74d3` 上多出来的是追加的用例。

自选的 13 处。S1 至 S10 先在 `c17a9062` 上跑，6 处存活；补了用例之后和 S11 至 S13 一起在 `4e4a74d3` 上跑，13 处都转红。

| 变异 | `c17a9062` | `4e4a74d3` |
|---|---|---|
| S1 没有可去掉的东西时，交回的是调用方自己的那个序列 | 存活 | T28 |
| S2 日志写在 WARNING | T15 | T15 |
| S3 不带工具名的结果不算回答 | 存活 | T24 |
| S4 去空的一轮只在它是最后一条消息时才去掉 | 存活 | T27 |
| S5 留下来的已答调用顺序颠倒 | 存活 | T26 |
| S6 `/compact` 没有写回时交回原样的历史 | T10 | T10 |
| S7 日志行不带会话 id | T15 | T15 |
| S8 留着文字的一轮连已答调用也丢掉 | T3、T21 | T3、T21 |
| S9 空 id 的调用一律算已回答 | 存活 | T25 |
| S10 交换只在有 session id 时才清理 | 存活 | T29 |
| S11 每次交换都写那行日志 | 没有跑 | T15 |
| S12 日志数的是丢了调用的轮数 | 没有跑 | T15 |
| S13 `stream_turn` 用原样的历史建交换 | 没有跑 | T29 |

追加的用例。前五个在 `tests/context/test_transcript.py`，T29 在 `tests/entry/test_turn.py`：

| 编号 | 测试 | 钉住的 |
|---|---|---|
| T24 | T4 的第 10 种形状 `answered-by-a-compaction-placeholder` | 规则 2：结果只看 id。`repair_tool_pairs` 补的占位不带工具名，它回答的调用要留着 |
| T25 | `test_an_unanswered_call_with_an_empty_id_is_removed` | 规则 2：空 id 的调用没有结果时照样去掉。厂商不给 id 时两个适配器都把 id 留空 |
| T26 | `test_the_answered_calls_of_a_turn_keep_their_order` | 规则 3：留下来的调用保持原来的先后 |
| T27 | `test_a_turn_emptied_in_the_middle_of_a_conversation_is_removed_too` | 规则 4：整轮去掉不看位置，前后两条请求变成相邻 |
| T28 | `test_the_cleaned_conversation_is_a_tuple_that_is_not_the_callers_list` | 没有可去掉的东西时返回的也是新 tuple。`_Carried` 以前拿到的是 `tuple(history)` |
| T29 | `test_streaming_a_turn_leaves_out_a_call_nothing_answered` | 规则 9：`stream_turn` 这条路径，不带 session id；规则 10 的日志在没有 id 时写的是 `session -:` |

S3 的后果比别的几处重。把不带工具名的结果当成不算回答，会把压缩补过占位的调用去掉，留下一条找不到调用的占位结果。按 `repair_tool_pairs` 的 docstring，这是 Anthropic 会拒绝的另一种不配对；在真实接口上没有测过。

日志是 `20_mutations_on_c17a9062.log` 和 `21_mutations_on_4e4a74d3.log`。

### 13.6 真实模型验收

按 §7.3 重跑 `live_02_e2e.py`（复制到 `IMP/probes/`，没有改），对真实 DeepSeek 发了 3 次请求，一次模型调用就是一次请求，逐条记在 `IMP/logs/ledger.txt`。分支一侧跑的是 `4e4a74d3`，它的 `omicsclaw/` 和 `c17a9062` 相同。

| 编号 | 请求 | 返回 | 交换和存下的历史 |
|---|---|---|---|
| V11 | `truncate`，基线树，`max_tokens=2000`，`su`，11 个工具 | 200，`finish_reason='length'`，`in=15176 out=2000`。正文为空，思考 1,292 字符，一个 `write_file` 调用，参数 9,274 字符、解析不了 | `converged`、`stop=truncated`，存下 `U a1!~` |
| V12 | `continue`，基线树，V11 工作区的一份副本，`suau` | 400：`An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'. (insufficient tool messages following tool_calls message)` | `failed`，历史不变 |
| V13 | `continue`，实施分支，V11 工作区的另一份副本，`suu` | 200，`finish_reason='stop'`，`in=15169 out=37`，正文 2 个字符 | `converged`，存回去的是 `U U A` |

三次都和 §7.3 的预期一致。另外只读打开两份副本的 `memory.db` 看了 `messages` 表：基线那份是 `user` 和一条带 9,434 字符 `tool_calls` 的 `assistant`；分支那份是 `user`、`user`、`assistant`，`tool_calls` 列都是空的。日志是 `40_live_truncate_baseline.log`、`41_live_continue_baseline.log`、`42_live_continue_branch.log`、`43_live_db_rows.log`。密钥从主检出的 `.env` 读进探针进程，没有打印，没有写进任何文件。

### 13.7 没有跑的

- 全量测试。按 `SPEC.md` 第 3 档不需要。
- PR 上的 `Eval CI`。分支没有 push，没有开 PR。
- §7.2 的第二、三条命令在 `4e4a74d3` 之后没有重跑，理由在 §13.4。
- Anthropic 的真实接口，DeepSeek 以外的 OpenAI 兼容后端。
- 真实的 `oc` CLI、Desktop、channel 进程。跑的是测试、探针和探针里的真实装配。
- 计划 0078：只把落地的 `transcript.py` 和它第 3 版原型的 `transcript.py` 做了三方合并，没有冲突，合并出来的文件上本计划的 42 个用例都过。0078 的提议测试没有在合并出来的文件上跑，它的第 4 版没有看。

### 13.8 实施中看到的、计划没有写的

1. Channel 的 `/compact` 和 CLI 一样有两句话会和事实对不上。`_describe_compaction` 在短会话上回 "Nothing to compact yet: the conversation is already short."，摘要失败时回 "Compaction failed and the conversation was left as it was: …"，而未答调用已经去掉了。文字没有改，只记进了 `progressive-compactor.md` §12。
2. `transcript.py` 的模块 docstring 那一句（§13.2 第 2 条）。
3. 测试表没有钉住的那几处（§13.2 末尾、§13.5）。
4. 规则 10 的日志在没有 session id 时写的是 `session -:`。`run_turn`、`stream_turn` 的 `session_id` 默认是空串，库调用方不传时看到的就是这个。
5. `_Carried.carried` 在引擎没有 commit 时返回的是它拿到的历史，现在那是清理过的一份，不再是调用方给的原样。今天走不到这里：`_outcome` 只在拿到结果之后才读它，而引擎交出结果之前已经 commit 过。
6. 规则 8 的理由在今天的 SQLite 存储上走不到。`SqliteSessionStore._load` 用 `Role(m["role"])` 重建角色，读回来的不是 `str`。`==` 仍然要留着，别的 `SessionStore` 实现和库调用方可以交来字符串角色。T7 的 docstring 原先照草稿写的是"从存储行重建的历史"，`17700f03` 改成了"调用方或某个会话存储"。
7. `/compact` 的交换里清理算了两遍，`_assemble` 一遍，`compose` 一遍，日志只在 `_assemble` 那一遍写。`prepare` 和直接调 `compose` 的调用方去掉调用时没有日志。
8. T10 的测试名让 `def` 那一行有 89 列，超过 black 的 88。标识符折不了行，仓库里别的测试文件也有这样的行。跑测试用的两个 conda 环境里都没有 black 和 ruff，新代码是手工按 88 列排的，没有用工具核过格式。
9. `tests/entry/test_turn.py` 现在从 `tests/entry/test_turn_runner.py` import 替身和帮助函数，以前只有 `test_session.py` 这样做。
