# 计划 0080：被输出上限截断的交换留下没被回答的工具调用，会话此后卡死

**状态**：第 1 版（2026-10-09）。待独立审核和 owner 裁定。本计划没有改生产代码和测试。

**基线**：`main` 的 `5daf5482`，行号以它为准，实施时按符号名重新定位。

**编号**：环节 C1 至 C9，入口 P1 至 P8，处理方式 X1 至 X5，位置 L1 至 L6，测试 T1 至 T15，变异 M1 至 M15，真实请求 V1 至 V10，风险 R1 至 R9，待裁定问题 Q1 至 Q6。

**证据**：探针、原型和输出在 `/tmp/claude-0/-workspace-dataset-private-zhouwg-data-OmicsClaw/9885f10a-3799-4309-9dfb-0981c166422a/scratchpad/plan-0080/`，下面记作 `SCR`，清单见 §11。对真实 DeepSeek 接口发了 10 次请求，逐条记在 `SCR/logs/ledger.txt`，其中 2 次没有得到信息（一次传输中断，一次是探针自己把请求体写错）。本机没有 Anthropic 凭据，Anthropic 一侧的结论只有官方文档和本仓库适配器代码两种依据，文中逐处标明。官方文档是 2026-10-09 抓取的。只读引用的既有证据在 `/tmp/claude-0/-workspace-dataset-private-zhouwg-data-OmicsClaw/58ae2e35-0792-4055-933c-1cfb79c727a6/scratchpad/review-plan-0079/`，记作 `RV79`。

## 0. 摘要

1. 引擎遇到被输出上限截断的回复时，把整条 assistant 消息记进轨迹，不执行它带的工具调用，然后结束这次运行。它的 docstring 写明：从这里接着往下走的调用方要自己回答或丢掉这些调用。entry 层是这个调用方，它两样都没做，把轨迹原样提交进 `session.history` 并存库。
2. 下一条用户消息排在那条没被回答的调用后面。DeepSeek 返回 400，这次交换失败，历史不变，所以之后每条消息都从同一段历史开始、同样失败。重启后从 SQLite 读回的也是这段历史。低压力时压缩器不修配对；失败的交换不让历史增长，压力升不上去。用户在 CLI 上只看到 `Failed: ProviderError`。
3. 用真实 DeepSeek 走完了整条路径：把输出上限绑到 2,000，一次真实交换被截断在 `write_file` 的参数中间，存进 SQLite；今天的代码发下一条消息得到 400，原型发同一条消息得到 200。
4. 默认配置下撞上它的门槛比派发时想的高。OpenAI 方言默认不发 `max_tokens`，用的是厂商自己的默认值；DeepSeek 文档写的是思考模式 64K，实测一条回复写到 12,934 个输出 token 正常结束。主检出的会话库里 174 条 assistant 消息没有一条出现这种形状，最大的一条估算约 4,900 token。Anthropic 方言的 `claude-haiku-4-5` 和表里查不到的模型上限是 8,192，更容易撞上。
5. 推荐做法：每次交换开场时，从带进来的历史里去掉没被回答的工具调用，那一轮的文字留着；一轮既没有文字也没有剩下的调用时整轮去掉。不补任何结果，不加任何给模型看的文字。改动是 `omicsclaw/context/transcript.py` 的一个纯函数和 `omicsclaw/entry/turn.py` 的两处调用。
6. 已经卡死的会话不需要迁移：下一条消息或 `/compact` 都会经过同一处清理，成功后存回去的就是干净的历史。
7. 新测试 15 条（参数化后 23 个用例），在今天的代码上 21 红 2 绿，在原型上全绿；对原型做了 15 处定点变异，每处都有测试转红。现有测试要改 1 条（公开面清单加一个名字）。按 `SPEC.md` 属于第 3 档。
8. 这个做法不告诉模型和用户"回复被截断了"。今天也不告诉。要不要告诉、怎么告诉，是行为和措辞改动，建议另开计划（Q4）。
9. 与计划 0078 和规划闸门没有顺序依赖，改的不是同一个函数。需要 owner 裁定 6 条，见 §10。

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
| WARN | `plan_compaction` 原样通过（`compaction.py:289-290`），`apply_compaction` 最后一行（`:333`） | 不修 | 只在 offload 了东西时写回 |
| SOFT、FULL，摘要成功 | `apply_compaction` 的 `needs_summary` 分支（`:325-330`） | 修，补占位 | 写回 |
| SOFT、FULL，降级截断 | `fall_back`（`:680`）调 `fit_to_budget`；已经放得下或没有 head 时原样返回（`transcript.py:238-243`），真的截了才修（`:245-250`） | 截了才修 | 不写回（`should_write_back`，`progressive.py:41`） |
| SOFT、FULL，pin 之后不超过 6 条 | `split_head_tail` 没有 head（`transcript.py:186-187`），`needs_summary` 为假，走 `:333` | 不修 | 不写回 |
| EMERGENCY | `apply_compaction` 的 EMERGENCY 分支（`:331-332`） | 修，补占位 | 写回 |

低档位不修是有意的。`apply_compaction` 的 docstring（`compaction.py:319-323`）写的理由是：原样通过的档位必须和输入逐字节相同，否则每个不需要压缩的轮次都要重新预热前缀缓存。

卡住的会话到不了 SOFT。失败的交换不往历史里加东西，历史不增长，压力不变；DeepSeek 的窗口在 `_model_limits.py:89` 是 1M。只有截断发生时已经在 SOFT 以上的会话，下一次调用才会被补上占位。

补上占位之后（`SCR/logs/out_01_stuck_today.log` 的 E 段，逐档直接调 `compact()`）：参数完整的调用在两个方言上都合法；参数被截的调用，DeepSeek 接受（V2），Anthropic 适配器在每一档都仍然本地抛错，因为修复只补结果，不碰参数。

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

P1 至 P6 最终都经过 `entry/turn.py` 的 `_assemble`。全仓库直接调 `engine.exchange`、`exchange_stream` 的只有 `entry/turn.py`（`:295`、`:308`）和子代理。

### 1.4 用户今天看到什么

截断的那一次：文字和思考照常流出来，工具调用的参数分片从不外露（`docs/core-features/agent-loop.md:160`），交换以 `converged` 结束，CLI 对 `converged` 不打印结束行（`entry/cli/_repl.py:1208`）。`omicsclaw/entry` 里读 `StopReason.TRUNCATED` 的只有子代理的 `_conclusion`。用户看到的是回复停了，工具没有跑，没有任何说明。

之后的每一条消息：CLI 打印 `Failed: ProviderError`。`_terminal_line`（`entry/render.py:415-423`）只取异常的类型名，400 的原文不显示。Desktop 和 channel 上显示什么没有逐个看。

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
| V10 | 原型，同一个会话的另一份副本，下一条消息，`suu` | 200，`finish_reason='stop'` | 原型不卡死 |

既有的两条（`RV79/rv_out3_deepseek.log`，2026-10-08，本计划没有重发）：`suau` 带一条参数完整的未答调用返回 400，同样的对话补上结果（`suatu`）返回 200。

推荐做法发出的三种形状（§5.1）今天就有：有计划的会话里，注入器每次调用都在末尾追加一条 user 消息（`planning/injector.py:166-173`），工具结果后面跟 user、user 后面跟 user 都是日常请求的样子。

别的 OpenAI 兼容后端没有测。

### 2.2 Anthropic：只有文档和代码依据

没有对真实接口发过请求。

| 结论 | 依据 |
|---|---|
| assistant 轮里的每个 `tool_use`，紧接着的 user 消息里必须有它的 `tool_result` | 文档 "Handle tool calls" 的 formatting requirements："Tool result blocks must immediately follow their corresponding tool use blocks in the message history."，以及报错 "tool_use ids were found without tool_result blocks immediately after" |
| 回复在工具调用中间被 `max_tokens` 截断时，官方建议是调高 `max_tokens` 重发同一个请求 | 文档 "Handling stop reasons" 的 "Incomplete tool use blocks" 一节："you'll need to retry the request with a higher `max_tokens` value to get the full tool use"。示例代码重发的是原来的 `messages`，被截断的回复没有放进去 |
| 相邻的同角色轮次由服务端合并 | API 参考的 `messages` 参数说明："Consecutive `user` or `assistant` turns in your request will be combined into a single turn." 随 Claude Code 分发的参考在这一点上自相矛盾（计划 0079 §1.1 的 A2 记过） |
| 参数被截的调用，这个适配器根本发不出去 | 代码：`decode_arguments`（`anthropic_provider.py:172-198`）。docstring 写明它有意不用 `parsed_arguments()` 的 `{}`，因为那等于把历史重放成模型没发过的一次调用 |
| 没有内容的 assistant 轮不会出现在请求里 | 代码：`encode_conversation` 只在有 block 时追加（`:286-288`） |
| 流式路径上参数会被截成解析不了的字符串，阻塞路径上不会 | 代码：C1。阻塞路径上接口对一个没写完的 `tool_use` 返回什么样的 `input`，文档里没有查到 |

## 3. 频率和触发条件

输出上限从哪来。`ProviderConfig.max_tokens` 默认是 0，表示没设（`provider/config.py:413`）。主循环的 provider 由 `provider_from_env(config.provider, config.model)` 建（`entry/assembly.py:1134`），不传 `max_tokens`；`AppConfig`、命令行和环境变量里都没有设它的地方。主检出的 `.env` 里和 provider 有关的只有 `LLM_PROVIDER=deepseek` 和密钥，解析出来的 `max_tokens` 是 0（`SCR/logs/live_00_settings.log`）。

- OpenAI 方言：`max_tokens` 为 0 时请求里没有这个键（`openai_provider.py:507-512`），用厂商的默认值。DeepSeek 的 API 文档写的是：不设时非思考模式 8K，思考模式 64K（`reasoning_effort` 为 `max` 时 128K），可设范围 1 到 384K；`thinking` 的默认值是 `enabled`。V7 实测超过 12,934。别的预设各家默认值不同，没有查。
- Anthropic 方言：每次必发，值是 `config.max_output_tokens`（`anthropic_provider.py:633-636`），也就是 `_model_limits.py` 表里的数：`claude-sonnet-4-6` 是 64,000，`claude-haiku-4-5` 是 8,192，表里查不到的模型是 8,192（`:52`）。

什么样的回复会撞上。一条回复的输出是思考、正文和工具调用参数三者之和（V1 里 700 个 token 有 550 个是思考）。主检出的库里，参数最长的工具是 `write_file`（17 次调用，中位数 2,497 字符，最大 11,711），其次是 `task`、`edit_file`、`bash`。撞上限的会是一次写很长的文件，或者很长的思考后面跟一个调用。

发生过几次。把主检出的 `.omicsclaw/memory.db` 连同 WAL 复制到 `SCR/dbcopy/`，只读打开副本（`SCR/probes/freq_01_db.py`、`freq_02_sizes.py`，输出在 `SCR/logs/freq_01_db.log`、`freq_02_sizes.log`）：

| | 数 |
|---|---|
| 会话 | 26（2026-09-20 至 10-08），其中 7 个没有消息 |
| assistant 消息 | 174，其中 147 条带工具调用，共 242 个调用 |
| 有调用没被回答的会话 | 0 |
| 参数解析不了的调用 | 0 |
| 最大的一条回复 | 22,624 字符（思考 6,269，两个 `write_file` 的参数 16,235），估算约 4,900 token |
| 每条回复的估算 token | 中位数 226，95 分位 2,187 |

估算用的是 ASCII 每 4.7 个字符一个 token（V1 和 V8 两条真实回复量出来的都是 4.7），非 ASCII 每个字符算一个 token，偏高估。按这个估算，174 条里没有一条达到 8,192，只有一条超过它的一半；最大的一条是 64K 的 8%。

结论：在主检出的库里它发生过 0 次。按 owner 日常的配置（DeepSeek，思考模式，不设上限），要一条回复超过文档上的 64K token 才会触发，库里最大的回复离它差一个数量级。上限是 8,192 的配置（Anthropic 方言的 haiku 和未知模型；别的后端如果默认值小）离得近得多：库里已经有一条回复过了 8,192 的一半。

量不到的部分：这个库只是一个工作区的 26 个会话。其他工作区和 Desktop 用户的库不在这台机器上。`/tmp` 下还有几百个测试和验收遗留的 `memory.db`，这次没有扫（本会话的权限层拒绝了批量读取，没有绕过）。没有找到带 `stop_reason` 的落盘记录：`agent.stop_reason` 只写进 OTel span（`observability/scope.py:594`），主检出里没有 bench 或 live eval 的结果文件带这个字段。派发时说的"已复现"是脚本化模型加真实接口对形状的拒绝，V8 至 V10 是把上限人为绑低之后的真实复现，都不是默认配置下自然发生的。

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

X2 的问题有三处。典型的截断正好断在参数中间（V1、V8 都是），这时在 Anthropic 上要么退化成 X1，要么得改写参数，而改写参数是 `decode_arguments` 的 docstring 明确不肯做的事。占位结果是一条编出来的 Observation，引擎的 docstring（`loop.py:712-715`）不做这件事的理由是它和工具真的这样返回分不开。被截的参数留在历史里，DeepSeek 默认上限下可能是几万 token 的残片，之后每次请求都带着，直到被压缩。把原型的清理换成 `repair_tool_pairs`（变异 M12）就是 X2 不处理参数的版本：参数被截的 5 种形状在 Anthropic 编码器上全红，参数完整的那一种通过。

X3 让带调用的截断丢文字，不带调用的截断（今天合法，文字留着）不丢，两种截断不一致，用户看到过的文字模型却不记得。

X4 是 Anthropic 文档建议的方向，也是能把用户的任务做完的做法。它要改引擎循环、定重试次数和一句给模型的话。上限不变时原样重试会再截一次，再烧一次上限那么多的输出 token，所以那句话得让模型换一种做法（比如分块写）。这句话如果进持久历史，就是第三种 user 消息，而规划闸门（计划 0039 §9.3）和计划 0078 的规则都建立在"持久历史里的 user 消息只有请求和摘要"之上；只进发送副本的话要给 augmentor 加状态。这是行为和措辞改动，按惯例要先做真实会话对比。它也救不了已经存进库的会话。

X5 只降低频率。DeepSeek 的默认值已经是文档上的 64K，Anthropic 方言发的已经是表里的模型上限。

### 4.2 在哪里处理：每道防线的账

| 编号 | 位置 | 防什么 | 旁路 | 代价 | 值不值 |
|---|---|---|---|---|---|
| L1 | 引擎源头：`_kernel` 在 `TRUNCATED` 时改写或不记这条消息 | 今后的截断，覆盖所有引擎调用方 | 已经存进库的会话；调用方自己给的历史 | 改引擎的契约（`_stop_reason_for` 的 docstring、`agent-loop.md:229`、`:499`）。`RunResult.messages` 不再是模型的原话，追踪和 eval 读的是它 | 不值。救不了存量；有了 L3 之后它没有新增的覆盖，仓库里不经过 `entry/turn.py` 的引擎调用方只有不带历史的子代理 |
| L2 | entry 提交时：`_outcome` 在 `stop_reason` 是 `TRUNCATED` 时清理 `TurnOutcome.history` | 今后的截断；库里存的历史始终合法 | 存量会话；调用方自己给的历史 | 一处调用。`TurnOutcome.history` 不再等于 `result.messages` 去掉 system 消息 | 单独不够。和 L3 一起时只多管"两次交换之间库里的样子"，读这段历史的只有 CLI 恢复会话时的回顾（`entry/cli/_repl.py:402`、`:918`）。不做 |
| L3 | entry 开场时：`_assemble` 和 `compose` | 今后的截断（在下一次交换开场时）、存量会话、`run_turn` 和 `stream_turn` 的库调用方、`/compact` | 直接调 `engine.exchange` 或 `engine.run` 的库调用方，引擎的契约照旧由他们自己处理。从截断到下一次交换之间，库里存的仍是原样 | 一个纯函数，两处调用，每次交换开场把历史扫一遍。没有问题的历史原对象返回，请求字节不变 | 值。推荐 |
| L4 | 每次发送前：压缩器在所有档位都修，或者两个适配器各自检查 | 任何来源的未答调用 | 适配器方案没有旁路 | 压缩器方案违背低档位逐字节原样通过的约定（`compaction.py:319-323`）。适配器方案要在两个文件里各写一遍，provider 层不能 import context 层（`tests/provider/test_provider_layering.py:94`），改的是 `SPEC.md` 第 4 档的代码，Anthropic 一侧还要处理参数 | 现在不值。交换进行中引擎保证每个调用都有结果（`_answer_every_call`，`loop.py:724-811`），压缩删消息之后已有修复。L4 比 L3 多防的只有直接调引擎的库调用方 |
| L5 | 读库时：`SqliteSessionStore._load` | 存量会话 | `InMemorySessionStore` 和别的 `SessionStore` 实现；调用方自己给的历史 | 让存储层懂线上协议 | 不值 |
| L6 | 一次性迁移，或者一条修复命令 | 存量会话 | 迁移之后再发生的；命令要用户先知道原因，而界面只显示 `Failed: ProviderError` | 一段只跑一次的代码，或者一个新的 surface | 不值。已知的存量是 0 个会话 |

只在源头处理不够：L1、L2 都救不了已经存进库的会话，得另配 L5 或 L6。L3 一处就同时管住新发生的和存量的。有了 L3，源头再加一道只改变库里那段历史在两次交换之间的样子。发送前那一道防的是今天仓库里不存在的调用方式。所以推荐只做 L3。

## 5. 推荐

### 5.1 规则

处理方式用 X1，位置用 L3。

1. 一个调用算被回答了，当且仅当紧跟在它那条 assistant 消息后面的那一串连续的 `Role.TOOL` 消息里，有一条带着它的 id。这和 `repair_tool_pairs` 的相邻判据是同一个。
2. 没被回答的调用从它那条消息上去掉。消息的文字、思考和被回答了的调用不动。
3. 去掉之后既没有文字也没有调用的消息整条去掉，思考随它一起去掉。
4. 不补任何结果，不加任何文字。
5. 没有可去掉的东西时，返回的是原来那些消息对象，顺序不变。
6. 找不到调用的工具结果不归它管，留给 `repair_tool_pairs`。
7. 角色比较用 `==`，理由同 `repair_tool_pairs` 的 docstring。
8. 这个函数在两个地方调用：`_assemble` 建 `_Carried` 时，以及 `compose` 把历史交给 `assemble` 时。前者管所有带模型调用的交换，后者管 `/compact` 和 `prepare`。
9. `_assemble` 去掉了调用时写一行 INFO 日志，带会话 id 和去掉的调用数。

规则 3 的理由：Anthropic 适配器本来就不发没有内容的 assistant 轮（§2.2），整条去掉之后两个方言看到的是同一段对话；留着的话 DeepSeek 上会把几千字符的思考当 `reasoning_content` 重放，对模型没有用处。DeepSeek 接受正文为空的 assistant 消息（V4），所以这一条不是为了躲 400。

去掉之后，下一次请求里那个位置有三种样子，都是今天就在发的形状（§2.1）：那一轮有文字时是 `… A U`；没有文字、前面是工具结果时是 `… a t U`；没有文字、前面就是请求时是 `U U`。

### 5.2 原型

`SCR/apply_prototype.py` 写进 `SCR/proto/`（`git archive 5daf5482` 的导出）。实施时可以另写，以 §6.2 的测试为准。

`omicsclaw/context/transcript.py`，加进本模块和 `omicsclaw.context` 的 `__all__`：

```python
def drop_unanswered_calls(messages: Sequence[Message]) -> tuple[Message, ...]:
    """Remove every tool call that no tool result answers.

    A call is answered when one of the ``Role.TOOL`` messages directly
    behind its assistant turn carries its id, the adjacency
    :func:`repair_tool_pairs` pairs by. An unanswered call is taken off
    its turn; the turn keeps its text, its reasoning and its answered
    calls. A turn left with no text and no call is removed.

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
        answered = {answer.tool_call_id for answer in messages[index:run_end]}
        calls = tuple(call for call in message.tool_calls if call.id in answered)
        if len(calls) == len(message.tool_calls):
            kept.append(message)
        elif calls or message.content:
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

原型上的结果（`SCR/logs/out_02_stuck_prototype.log`，同一个探针、同一个替身后端）：

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

今天 A、B、C 三段里有 15 次交换 `failed`，D 段第二次 `run_turn` 抛错；原型上没有一次失败，替身后端没有拒绝过任何请求。原型上每一次请求在两个方言的检查里都没有违规，Anthropic 请求都组得出来。真实接口上是 V9 对 V10。

### 5.3 理由

- 会话不再卡死，存量会话不需要迁移，`/compact` 也能救。
- 做的是引擎契约已经写给调用方的两个选项之一（"answer or drop"），引擎、适配器、存储层和压缩器都不改。
- 两个方言一条规则。参数被截的调用不需要解析、不需要改写，随调用一起去掉。
- 不编结果，不往历史里加 user 消息，规划闸门和计划 0078 依赖的"持久历史里的 user 消息只有请求和摘要"不受影响。
- 不加给模型看的文字，没有措辞要调。
- 对没有问题的历史是恒等的：原对象返回，请求字节不变，前缀缓存不受影响。有问题的那条消息在历史的末尾，去掉它不动它前面的前缀。
- 几万 token 的半截参数不留在上下文里。
- 改动小：一个 20 行的纯函数，两处调用，一行日志。暂存副本里跑第 3 档的两条命令，只有公开面清单那一条测试转红（§7.2）。

### 5.4 放弃了什么

- 模型不知道自己上一轮被截断了。用户说"继续"时它很可能照原样再来一次；内容确实超过上限时会再截一次，再花一次上限那么多的输出 token。今天的代码在这里是会话直接报废。
- 用户仍然看不到"回复被截断了"。这是今天就有的缺口，不带工具调用的截断也一样。
- 从截断到下一次交换之间，库里存的历史仍然以没被回答的调用结尾。
- 整轮去掉时，那一轮的思考不再留在历史里。
- 直接调 `engine.exchange` 的库调用方不受保护，引擎的契约没有变。

## 6. 改动范围

### 6.1 代码

| 文件 | 改动 |
|---|---|
| `omicsclaw/context/transcript.py` | 新增 `drop_unanswered_calls`，加进 `__all__` |
| `omicsclaw/context/__init__.py` | import 和 `__all__` 各加一个名字 |
| `omicsclaw/entry/turn.py` | `compose` 和 `_assemble` 按 §5.2 改。`compose`、`_assemble`、`TurnOutcome.history` 的 docstring 各加一句：没被回答的工具调用在开场时去掉；`TurnOutcome.history` 在被截断的运行之后仍以它们结尾，传回来时去掉 |

`omicsclaw/engine/`、`omicsclaw/provider/`、`omicsclaw/memory/`、`omicsclaw/entry/session.py` 和 `repair_tool_pairs` 都不改。`_stop_reason_for` 的 docstring 说的仍然成立，不动。

### 6.2 测试

T1 至 T15 写在 `SCR/proposed/test_plan0080_proposed.py` 里，在两棵树上各跑过一遍（`SCR/logs/out_03_proposed_today.log`、`out_04_proposed_prototype.log`）。参数化后 23 个用例：今天 21 红 2 绿，原型 23 绿。日志里的总数各多 1，是打印 `omicsclaw.__file__` 的那一条。

`tests/context/test_transcript.py`，纯函数。今天的红是函数不存在：

| 编号 | 测试 | 钉住的规则 |
|---|---|---|
| T1 | `test_an_unanswered_call_is_taken_off_its_turn_and_the_text_stays` | 规则 2：调用去掉，文字和思考留着 |
| T2 | `test_a_turn_left_with_no_text_and_no_call_is_removed` | 规则 3 |
| T3 | `test_only_the_unanswered_call_of_a_turn_goes` | 规则 2：同一轮里被回答的调用和它的结果留着 |
| T4 | `test_a_conversation_with_every_call_answered_comes_back_message_for_message`，4 种形状 | 规则 5：返回的是原对象 |
| T5 | `test_an_unanswered_call_in_the_middle_of_a_conversation_is_removed_too` | 不只看末尾 |
| T6 | `test_a_result_that_is_not_directly_behind_its_turn_does_not_answer_it` | 规则 1、6：相邻才算；游离的结果不动 |
| T7 | `test_roles_that_arrive_as_plain_strings_are_read_the_same` | 规则 7 |

`tests/entry/test_session.py`，经 `SessionRegistry`，后端是能报 `finish_reason` 的替身（现有的 `Scripted` 报不出，要加一个子类）：

| 编号 | 测试 | 钉住的行为 | 今天 |
|---|---|---|---|
| T8 | `test_the_message_after_a_cut_off_tool_call_reaches_the_model_without_that_call`，6 种形状（§1.5 的 A-a 至 A-f） | 截断之后的下一次请求里没有未答调用；用真实的 `encode_messages` 和 `encode_conversation` 编码，两个方言都配对完好，Anthropic 不抛错；交换 `converged`；存回去的历史干净 | 6 条全红 |
| T9 | `test_a_stored_session_that_ends_on_an_unanswered_call_works_after_a_restart` | 直接往 SQLite 写一行今天的代码会留下的历史，另建一个 app 提交消息：不需要迁移，文字留着 | 红 |
| T10 | `test_compacting_a_session_that_ends_on_an_unanswered_call_stores_it_without_the_call` | 短会话上 `/compact` 之后历史是干净的 | 红 |
| T11 | `test_a_session_with_every_call_answered_is_sent_as_it_was_stored` | 对照：并行调用的正常会话，发出去的就是存着的那些消息对象 | 绿。变异 M6、M10、M11 让它转红 |
| T12 | `test_a_cut_off_exchange_still_reports_the_turn_as_the_model_wrote_it` | 截断的那次交换自己的 `result.messages` 仍以原话结尾，`stop_reason` 是 `TRUNCATED` | 绿。它记下 L1、L2 没有做是有意的，Q2 改了裁定就改它 |
| T15 | `test_leaving_a_call_out_is_logged_with_the_session_and_the_count` | 规则 9：三次交换只在去掉调用的那一次写一行，数的是调用 | 红 |

`tests/entry/test_turn.py`：

| 编号 | 测试 | 钉住的行为 | 今天 |
|---|---|---|---|
| T13 | `test_run_turn_accepts_the_history_a_cut_off_run_handed_back` | 阻塞路径 | 红 |
| T14 | `test_compose_leaves_out_a_call_nothing_answered` | 规则 8 的后一半 | 红 |

变异。对原型做了 15 处定点变异（`SCR/mutate_prototype.py`，`SCR/logs/out_05_mutations.log`），每处都至少让一条新测试转红，文件按 SHA-256 核对恢复：

| 变异 | 转红的测试 |
|---|---|
| M1 `_assemble` 不清理 | T8、T9、T13、T15 |
| M2 `compose` 不清理 | T10、T14 |
| M3 还有文字的那一轮也整条去掉 | T1、T5、T6、T7、T9、T10、T14 |
| M4 去空了的那一轮留着 | T2 |
| M5 对话里任何位置的结果都算回答 | T6 |
| M6 所有调用都当成没被回答 | T3、T4、T7、T11 |
| M7、M8 两处角色比较改成 `is` | T7 |
| M9 只看最后一条消息 | T3、T5、T6、T7 |
| M10 没动过的消息也重建一份 | T4、T7、T11 |
| M11 只读紧跟的第一条结果 | T4、T11 |
| M12 改成补占位（X2） | T8 的 5 种形状、T9、T13、T15 |
| M13 留下文字的那一轮丢掉思考 | T1 |
| M14 不写日志 | T15 |
| M15 日志数的是消息 | T15 |

现有测试要改的只有一条：`tests/context/test_context_is_a_leaf_layer.py:473` 的 `test_the_public_surface_is_exactly_what_plan_0030_delivered`，清单里加 `"drop_unanswered_calls"`。`tests/engine/test_loop.py:408` 的 `test_a_truncated_turn_does_not_execute_the_tool_calls_it_carried` 钉的是引擎照样记下这一轮，不用改。

不加脚本化 eval。`ScriptedTurn` 没有 `finish_reason` 字段（§1.3 的 P5），加一条 eval 要先改 `omicsclaw/evals/provider.py`。T8 至 T10 走的就是 eval Runner 走的那段 `SessionRegistry` 路径。`tests/evals/test_dataset_floor.py` 的 `BASELINE` 不变。

### 6.3 文档

| 文件 | 改动 |
|---|---|
| `docs/core-features/agent-loop.md` | `:229` 那一段补一句：entry 层在下一次交换开场时去掉这些调用。`:499` 的已知限制改写成：`RunResult` 和 `TurnOutcome.history` 仍以没被回答的调用结尾；经 `entry/turn.py` 的路径在下一次开场时去掉它们；直接调 `engine.exchange` 的调用方要自己处理。`:444` 的伪代码里 `_Carried(history)` 一行同步 |
| `docs/core-features/context-engineering.md` | §7 的函数表（`:280` 起）加一行 `drop_unanswered_calls`；`:45`、`:68`、`:94`、`:323`、`:466` 几处列函数名和 `_Carried(history)` 的地方同步 |
| `docs/core-features/progressive-compactor.md` | §9.2（`:467`）补一句：`compose` 交出的对话里没有未答调用，所以短会话上的 `/compact` 也会把它们从历史里去掉。§13 文件索引（`:541`）同步 |
| `docs/plans/0080-truncated-exchange.md` | 加交付记录一节 |
| `CHANGELOG.md` | 合并时在顶部加一条 |

`README.md` 不动。`tests/entry/golden/` 是 system 提示和工具表，这次不变，不用重录。

估计工作量：代码和测试两小时，文档一小时。

## 7. 验证

### 7.1 档位

`SPEC.md` 第 3 档。`omicsclaw.context` 是几层都 import 的包，公开面多了一个名字；发给模型的历史在一种情形下变了。provider 适配器、权限门、Desktop 线上契约都没有碰，不到第 4 档。合并到 `main` 属于第 4 档，由 `Eval CI` 跑。

### 7.2 要跑的

改动前在分支起点跑一次基线，改完再跑一次：

```
/opt/conda/envs/rapids_singlecell/bin/python -m pytest tests/context tests/planning tests/engine tests/entry tests/evals -q -p no:randomly
/opt/conda/envs/rapids_singlecell/bin/python -m pytest tests/*/test_*layer*.py tests/sdk/test_boundary.py tests/sdk/test_public_surface.py tests/test_*.py -q -p no:randomly
```

在 `5daf5482` 的暂存副本上（`SCR/logs/out_06` 至 `out_09`）：

| 命令 | 基线 | 换上原型 |
|---|---|---|
| 第一条 | 2842 passed、11 skipped、26 deselected、3 xfailed，约 130 秒 | 2841 passed、1 failed，其余相同 |
| 第二条 | 842 passed、14 skipped、1 deselected、1 xpassed，约 100 秒 | 841 passed、1 failed，其余相同 |

两条命令里红的是同一条：§6.2 要改的公开面清单。

另外：

- 写测试时先在没改的代码上跑新测试，确认红绿和 §6.2 的表一致，再改代码。
- 变异检查：对落地的实现重做 15 处变异，每处至少一条测试转红。
- `probe_01_stuck.py` 在落地的实现上重跑，被拒绝的调用和 `failed` 的交换都应当是 0。
- 需要 fastapi 的 Desktop 用例在 `rapids_singlecell` 环境里跳过。Desktop 的代码没有改，它和 CLI 一样经 `SessionRegistry` 到 `TurnRunner`。汇报时按"没跑"写。

### 7.3 真实模型的验收

建议作为合并的前置条件，量很小。保留什么、去掉什么是确定性的，上面的测试已经覆盖；这一步确认的是真实接口接受清理之后的请求。

做法是在实施分支上重跑 `SCR/probes/live_02_e2e.py`：`truncate` 一次（1 次请求，造出一个真实的截断会话），把工作区复制两份，`continue` 在 `main` 和实施分支上各一次（各 1 次请求）。预期和 V8 至 V10 相同：`main` 上 400、`failed`、历史不变；分支上 200、`converged`、存回去的历史没有未答调用。`SCR` 还在的话可以省掉 `truncate`，直接用 `SCR/runs/live/e2e_origin`。

这次没有措辞要对比，推荐做法不加任何给模型看的文字。V10 是在原型加上那行日志之前跑的，日志不影响请求。

## 8. 与在办事项的关系

计划 0078（分支 `docs/plan-0078-fallback-truncation-order`，tip `afba2c1e` 是第 2 版；派发本计划时的说明是定稿为 G+ 加"被尾部边界切开的一轮不丢"）。

- 没有顺序依赖，谁先落地都可以。
- 改的不是同一个函数。0078 改 `fit_to_budget` 并新增私有辅助函数，本计划新增 `drop_unanswered_calls`、改 `entry/turn.py` 的 `compose` 和 `_assemble`。两边都动的文件是 `omicsclaw/context/transcript.py`（不同的函数）、`tests/context/test_transcript.py`（各加各的测试）、`docs/core-features/context-engineering.md` §7 的表（不同的行）和 `CHANGELOG.md` 的顶部。后落地的一方按文本合并。
- 0078 的降级截断末尾会过 `repair_tool_pairs`，对"输入里本来就没应答的调用"补占位（它的 R5 第二种原因）。本计划落地后，会话历史在进压缩器之前已经清理过，这个来源的占位不再出现。
- 本计划可能让历史里出现两条相邻的请求（`U U`）。0078 的规则 4 和边界情形 E7a 已经覆盖：后面没有回复的请求轮到时直接丢。

规划闸门（`docs/plans/0039-planning-layer.md` §9，已在基线里）。

- `_gate_fires` 往回数到最近一条 user 消息为止（`planning/injector.py:228-232`）。本计划不往历史里加 user 消息，去掉的 assistant 消息属于上一次交换，在这条边界之外，计数不受影响。
- §9.3 的前提是"会进入持久历史的 user 消息只有两种"。X4 和"往历史里放一条截断说明"的做法会破坏它，这是 §4.1 没有选它们的原因之一。

计划 0079 已搁置，和本计划无关。它推荐的首条消息占位在 provider 适配器里，本计划不碰适配器。

排期上，owner 已裁定本计划排在在办事项的最前面。它不需要等 0078。

## 9. 风险和没验证的部分

- R1 模型不知道上一轮被截断，可能原样重来（§5.4）。只看过一次真实的后续交换（V10），那一次用户消息要求它只回一个词，说明不了它在真实长任务里怎么做。
- R2 用户仍然看不到截断的说明。本计划没有改这一点。
- R3 Anthropic 一侧没有对真实接口核实。依据是文档里的配对规则和适配器代码。接口如果比文档宽松，本计划的清理对它是多余的，没有害处。
- R4 OpenAI 兼容后端只测了 DeepSeek。`U U`、`a t U` 这两种相邻今天有计划块的会话每次调用都在发，别的后端如果拒绝，今天就已经在拒绝。
- R5 频率只在一个工作区的库上量过（§3）。默认上限 64K 来自 DeepSeek 文档，实测只确认了它高于 12,934。
- R6 `omicsclaw.context` 的公开面多一个函数。`compose` 的名字和签名不变，行为在一种输入上变了：带未答调用的历史。仓库外有没有调用方依赖旧行为不知道；想不出依赖它的用法，因为引擎从不执行历史里的调用。
- R7 两个调用带同一个 id、只回答了其中一个时，按 id 判断会把两个都当成被回答了。`ollama` 预设会给出空 id（`loop.py:745-748`）。引擎产生的历史里结果数等于调用数，走不到这里；`repair_tool_pairs` 在同一处也是按 id 配的。
- R8 修复前已经被压缩补过占位、参数又被截的会话，在 Anthropic 上仍然卡着：调用算被回答了，本计划不动它，适配器照样解析失败。要同时满足 Anthropic 方言、流式截断、截断时压力已在 SOFT 以上或对长会话用过 `/compact`。已知 0 例。出路是 `/clear` 或 `/new`。本计划落地后不会再产生这种历史，因为清理在压缩之前。
- R9 Desktop 和 channel 没有跑。它们和 CLI 走同一个 `SessionRegistry`，T8 至 T10 覆盖的是这一段共用路径。

这次顺带看到、不在本计划范围里的：

- OpenAI 方言没有给用户设输出上限的入口（§3）。`_model_limits.py:89` 里 `deepseek-v4-flash` 的输出上限是 8,192，表里的意思是"未知"，上下文预算拿它当输出预留（`entry/assembly.py:663`），和文档上的 64K 默认值对不上。
- `finish_reason` 不是 `length` 或 `max_tokens` 的中断（DeepSeek 文档列了 `content_filter`、`insufficient_system_resource`、`aborted`）不算截断，那一轮带的半截调用会交给工具层。每个调用都会得到一条结果，所以不会留下未答调用；参数解析不了时工具怎么回应没有在这次核对。

## 10. 需要 owner 裁定的问题

Q1 截断留下的调用怎么处理：X1（去掉，留文字）还是 X2（补一条"没有执行"的结果，参数被截的另外去掉）。X2 让模型知道发生了什么，但只在调用参数完整时有效，而实测的两次截断都断在参数中间；它还要定一句占位的措辞。建议 X1。以后做 X4 时模型自然会被告知。

Q2 位置：只在交换开场清理（L3），还是提交时也清理一次（L2 加 L3）。加 L2 的好处是库里存的历史始终合法，代价是 `TurnOutcome.history` 不再等于 `result.messages` 去掉 system 消息，多一处调用和一条测试。建议只做 L3。

Q3 函数放在哪。甲：`omicsclaw/context/transcript.py` 的公开函数，和 `repair_tool_pairs` 并排，要改公开面清单那一条测试，和 0078 同文件不同函数。乙：`omicsclaw/entry/turn.py` 的私有函数，不动 `omicsclaw.context` 的公开面，和 0078 没有任何文件重叠。建议甲：它是消息列表上的纯变换，配对的相邻判据写在那个文件里。

Q4 截断时不告诉用户、也不让模型续写，这件事要不要另开计划。可做的有两层：surface 显示一句"回复在输出上限处被截断，进行中的工具调用没有执行"；引擎在同一次交换里让模型换个做法重试（X4，上限固定次数）。两者都是行为和措辞改动，Desktop 那一侧还碰线上契约。建议另开一份计划，排在本计划之后，本计划不含。

Q5 真实模型验收（§7.3，1 到 3 次 DeepSeek 请求）要不要作为合并的前置条件。建议要。

Q6 开场清理时写的那行 INFO 日志（规则 9）留不留。它是今后知道这件事发生过几次的唯一落盘记录，也是"发出去的历史和存着的不一样"的唯一线索。建议留。

## 11. 证据清单

目录：`SCR`，即 `/tmp/claude-0/-workspace-dataset-private-zhouwg-data-OmicsClaw/9885f10a-3799-4309-9dfb-0981c166422a/scratchpad/plan-0080/`。`base/` 和 `proto/` 是 `git archive 5daf5482` 导出的两份副本，`apply_prototype.py` 只改 `proto/` 里的三个文件，原文留在旁边的 `.orig` 里。`proto_mut/` 是 `proto/` 的副本，变异在它上面做，做完和 `proto/` 用 `diff -rq` 核对相同。探针用 `/opt/conda/envs/rapids_singlecell/bin/python` 跑，`PYTHONPATH` 指向 `base/` 或 `proto/`，每份输出的第一行是 `omicsclaw.__file__`。要用 `openai` SDK 的 `live_02_e2e.py` 用 `/opt/conda/envs/OmicsClaw/bin/python`，同样由 `PYTHONPATH` 指向这两棵树。

| 脚本 | 输出（`logs/` 下） | 内容 |
|---|---|---|
| `probes/p80_common.py` | | 脚本化 provider、替身后端、两个方言的线上形状检查、装配 |
| `probes/probe_01_stuck.py` | `out_01_stuck_today.log`、`out_02_stuck_prototype.log` | §1.2、§1.5、§5.2：A 至 E 段 |
| `proposed/test_plan0080_proposed.py` | `out_03_proposed_today.log`、`out_04_proposed_prototype.log` | §6.2：T1 至 T15 |
| `apply_prototype.py`、`mutate_prototype.py` | `out_05_mutations.log` | §5.2、§6.2：原型和 15 处变异 |
| 现有测试 | `out_06_layers_baseline.log`、`out_07_layers_prototype.log`、`out_08_guards_baseline.log`、`out_09_guards_prototype.log` | §7.2 |
| `probes/freq_01_db.py`、`freq_02_sizes.py` | `freq_01_db.log`、`freq_02_sizes.log` | §3：主检出会话库的副本（`dbcopy/`） |
| `probes/live_00_settings.py` | `live_00_settings.log` | §3：`.env` 里非密钥的设置，不发请求 |
| `probes/p80_live.py`、`probes/live_01_shapes.py` | `live_01a_shapes_first_run.log`（V1 至 V3）、`live_01b_shapes_second_run.log`（V4、V5）、`live_01c_ceiling_first_try_probe_bug.log`（V6）、`live_01c_ceiling.log`（V7）；`live_01_cut_tool_call_message.json` 是 V1 返回的消息原文 | §2.1 |
| `probes/live_02_e2e.py` | `live_02a_truncate_today.log`（V8）、`live_02b_continue_today.log`（V9）、`live_02c_continue_prototype.log`（V10） | §1.5、§2.1 |
| | `ledger.txt` | 10 次真实请求，一行一次。V3 那一行是手工补的，当时探针还没有处理传输错误 |

密钥从主检出的 `.env` 读进探针进程，没有打印，没有写进任何文件。真实交换里工具在只读权限模式下运行，工作区在 `SCR/runs/live/`。

只读引用的既有证据：`RV79/rv_out3_deepseek.log`（r4、r5）、`rv_out2c_truncated.log`、`rv_out2d_force_sqlite.log`，以及计划 0079 的 §2.3、§2.4（`git show docs/plan-0079-first-message-shape:docs/plans/0079-first-message-shape.md`）。它们关于这种形状的结论和这次重跑的一致。

官方文档页面：`api-docs.deepseek.com/api/create-chat-completion`，`platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls`、`…/build-with-claude/handling-stop-reasons`、`…/api/messages/create`。页面是经抓取工具读的，引文以页面为准。
