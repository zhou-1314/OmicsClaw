# 计划 0079：对话以 assistant 开头时 Anthropic 适配器怎么发（首条消息的形状）

**状态**：第 1 版（2026-10-08）。待独立审核和 owner 裁定。本计划没有改生产代码和测试。

**基线**：`main` 的 `5daf5482`，行号以它为准，实施时按符号名重新定位。计划 0078 读的是本地分支 `docs/plan-0078-fallback-truncation-order` 的 `065ad974`（第 1 版），它还在修订。

**编号**：约束 A1 至 A10，路径 P1 至 P9，层 L1 至 L4，做法 N1 至 N4，测试 T1 至 T11，真实请求 V1 至 V8，风险 R1 至 R8，待裁定问题 Q1 至 Q5。

**证据**：探针脚本和输出在 `/tmp/claude-0/-workspace-dataset-private-zhouwg-data-OmicsClaw/58ae2e35-0792-4055-933c-1cfb79c727a6/scratchpad/plan-0079/`，清单见 §11。没有调用任何真实模型接口，没有读取主检出的 `.env`。官方文档是 2026-10-08 抓取的。

## 0. 摘要

1. Anthropic 公开的 API 参考里没有"首条消息必须是 user"这句话。写这句话的是 Anthropic 随 Claude Code 分发的 API 参考，网上流传的服务端报错也是这个意思。本机没有 Anthropic 凭据，这条约束没有对真实接口核实（R1）。
2. 今天会把以 assistant 开头的对话交给适配器的产品路径只有一条：SOFT、FULL 摘要失败后的降级截断把 head 整个丢掉。EMERGENCY 不会，它无条件保留 pin 之后的第一条。用真实装配复现了一次：第 9 次模型调用时，`AnthropicProvider._request_params` 组出的请求第一轮是 `assistant`，带 `tool_use`。
3. 如果接口按 A1 拒绝，这次交换以 400 结束。400 不重试，历史不变，这次交换里已经做完的工具调用全部作废。
4. 推荐在 `omicsclaw/provider/anthropic_provider.py` 的 `encode_conversation` 里处理：编码结果的第一轮不是 `user` 时，在最前面补一条固定文字的 user 轮，原文是 `[earlier conversation unavailable]`，其余轮次原样发出。逻辑一共 3 行。
5. 放弃的做法里，"丢掉开头的 assistant 轮"最不可取：以 assistant 开头的视图有 89% 第一轮带工具调用，丢掉它之后它的结果成了孤儿，连带删下去，74% 的视图被删空。
6. 这次还看到三种别的违规形状，都不在首条消息上。其中一种两个方言都会被拒，日常用的 DeepSeek 也在内：一次交换被输出上限截断时，没被回答的工具调用留在了历史里，下一条消息带着它发出去。建议另开计划（Q5）。
7. 改动是一个文件的一个函数加一个常量，几处 docstring，两份功能文档。新测试 11 条（参数化后 50 个用例），在今天的代码上 19 红 31 绿，在原型上全绿。现有测试有 3 条要各改一行。按 `SPEC.md` 属于第 4 档。
8. 和计划 0078 没有顺序依赖。0078 的方案 D 落地后，随机探针里以 assistant 开头的降级视图从 5.1%、6.0% 降到 3.3%、3.5%，不会到零。
9. 需要 owner 裁定 5 条，见 §10。

## 1. 约束

每条约束后面标了依据的种类。"文档"指 platform.claude.com 上的公开页面。"参考"指 Anthropic 随 Claude Code 分发的 API 参考（`claude-api` skill，缓存日期 2026-09-25）。"SDK"指本机 CI 虚拟环境里的 `anthropic` 1.11.0 和 `openai` 3.24.0 的源码，`rapids_singlecell` 环境里两个 SDK 都没装。"注释"指本仓库的注释和 docstring。"第三方"指论坛、issue 和错误库。"推断"是本计划的推断。文档页面是经抓取工具读的，引文以页面为准。OpenAI 的两条报错和 A1 的那条报错取自搜索结果的摘要，原页面没有逐个打开。

### 1.1 Anthropic 原生接口（Messages API）

| 编号 | 约束 | 依据 |
|---|---|---|
| A1 | 首条消息必须是 `user`。没有对真实接口核实 | 参考：`python/claude-api/README.md` 的 Rules 写 "First message must be `user`"，`shared/error-codes.md` 的表里有一行 "First message is `assistant` \| 400 \| First message must be `user`"。文档：API 参考里 `messages` 参数的说明没有这句话；"Using the Messages API" 一页只写了 `role: "system"` 的消息 "cannot be the first entry in `messages`"。第三方：报错原文 `messages: first message must use the "user" role`（lobe-chat issue 1601 等）。SDK：`MessageParam` 是 `TypedDict`，客户端不校验序列形状，SDK 源码里搜不到这类检查 |
| A2 | 不要求角色交替。连续的同角色轮次由服务端合并成一轮 | 文档：API 参考写 "Consecutive `user` or `assistant` turns in your request will be combined into a single turn."。SDK：`types/message_create_params.py:56-57` 同文。参考自相矛盾：README 写 "Consecutive same-role messages are allowed"，`shared/error-codes.md` 却把它列为 400，还给了报错 `messages: roles must alternate between "user" and "assistant"`。注释：`engine/loop.py:432-435` 是按"必须交替"写的 |
| A3 | assistant 轮里的每个 `tool_use`，紧接着的那条 user 消息里必须有它的 `tool_result`，中间不能夹别的消息 | 文档："Handle tool calls" 一页的 formatting requirements，报错 "tool_use ids were found without tool_result blocks immediately after"。注释：`engine/loop.py:730-733`、`context/transcript.py:70-72` |
| A4 | 带工具结果的 user 消息里，`tool_result` 块排在最前，文字排在后面 | 文档：同一页，给了一个会 400 的例子 |
| A5 | 找不到对应 `tool_use` 的 `tool_result` 会被拒 | 推断：A3 那句 "Tool result blocks must immediately follow their corresponding tool use blocks" 的另一个方向。具体报错没在文档里找到。注释：`context/transcript.py:70-72` |
| A6 | 并行调用的结果放在同一条 user 消息里 | 参考：拆开会 "silently trains Claude to stop making parallel calls"。注释：`anthropic_provider.py:253-260` 说拆开会被 API 拒绝。按 A2，拆开大概不会 400（推断）。适配器已经合并 |
| A7 | text 块不能为空 | 文档：API 参考里 text 块的 `text` 标了 `minLength: 1`。只含空白字符的 text 块会不会被拒没查到 |
| A8 | `tool_result` 的 `content` 可以省略 | 文档：同页有 "Example of empty tool result"。`content` 是空字符串时是否接受没查到。引擎已经把空输出换成占位（`engine/executor.py:268` 的 `observations`） |
| A9 | 末条消息是 assistant 时，Claude 4.6 及之后的模型返回 400；更早的模型把它当作续写 | 文档：errors 页的 "Prefill not supported"，报错 "This model does not support assistant message prefill. The conversation must end with a user message."。预设的默认模型 `claude-sonnet-4-6` 属于会拒绝的一类 |
| A10 | `messages` 不能是空数组 | 参考：`shared/error-codes.md` 把 "Empty messages array" 列为 400 的原因。SDK：`messages` 是 `Required` |

### 1.2 OpenAI 兼容接口和各预设

`omicsclaw/provider/config.py` 有 13 个预设，只有 `anthropic` 走 Messages 方言，其余 12 个走 `OpenAIProvider`。`_model_limits.py` 只记窗口和输出上限，不涉及序列形状。

| 后端 | 以 assistant 开头 | 工具调用配对 | 依据 |
|---|---|---|---|
| OpenAI（`openai`） | 没找到拒绝它的文字，推断接受 | 强制，两个方向都报 400 | SDK：`ChatCompletionToolMessageParam.tool_call_id` 的说明是 "Tool call that this message is responding to"，assistant 的 `content` "Required unless `tool_calls` or `function_call` is specified"，类型不限制顺序。第三方：报错 "messages with role 'tool' must be a response to a preceeding message with 'tool_calls'" 和 "An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'"（LangChain 排错文档、Portkey 错误库） |
| DeepSeek（`deepseek`） | 没核实 | 没核实，推断同 OpenAI | 文档：API 参考对 `messages` 只要求至少 1 条，没有顺序规则；assistant 消息有 beta 的 `prefix` 续写。注释：`openai_provider.py:131-138` 记了 thinking 模式下历史里的 `reasoning_content` 必须回传 |
| Gemini（`gemini`，OpenAI 兼容端点） | 没核实。原生接口很可能拒绝以函数调用轮开头的对话 | 没核实 | 第三方：Google AI 开发者论坛上的报错 "Please ensure that function call turns come immediately after a user turn or after a function response turn"，回帖的社区成员说对话要以 user 开头并严格交替。经兼容端点时是否一样不知道 |
| OpenRouter（`openrouter`，默认模型 `anthropic/claude-sonnet-4.6`） | 没核实。网关把请求转成 Anthropic 的格式，替不替调用方补首条 user 没有文档 | 没核实 | 文档：message-transforms 一页没有提到这类规整 |
| `nvidia`、`siliconflow`、`volcengine`、`dashscope`、`moonshot`、`zhipu` | 没查到文档，没核实 | 没核实 | |
| `ollama` | 推断不校验，本地按模板拼接 | 推断不校验 | 注释：`engine/loop.py:745-748` 记了它会省略工具调用的 id |
| `custom` | 取决于端点 | 取决于端点 | |

两个方言都要求配对（A3 和 OpenAI 的两条报错）。以 assistant 开头这一条，查到依据的只有 Anthropic，Gemini 有第三方说法。

### 1.3 与派发本计划时的说法有出入的地方

1. 派发时说"Messages API 文档写的是第一条消息必须是 `user`"。公开的 API 参考没有这句话，出处见 A1。
2. 派发时说 EMERGENCY 截断的某些情形也会产生这种视图。没有复现。`_emergency_survivors` 无条件保留 `rest[0]`（`context/transcript.py:309-319`），`repair_tool_pairs` 只删 `Role.TOOL` 消息（`:133-137`），所以 `rest[0]` 是 user 时视图一定以 user 开头。3000 段随机会话里 0 次。EMERGENCY 产生的是另一种形状：末条是 assistant（§2.3）。库调用方自己给一段以回复开头的历史时，EMERGENCY 和别的档一样原样留着它。
3. 派发时说 `encode_conversation` 只做三件事。还有第四件：既没有文字也没有工具调用的 assistant 消息被丢掉（`anthropic_provider.py:286-288`），丢掉之后前后两条 user 消息相邻。按 A2 这是合法的。
4. 派发时说计划 0078 的新规则"会减少这种视图，但不能消除"。属实，量到的数字见 §8。

## 2. 今天哪些路径会发出违规的序列

下面用标签表示消息：`S` 是 system 消息，`U<n>` 是第 n 次交换的用户请求，`a<n>.<k>` 和 `t<n>.<k>` 是第 n 次交换第 k 轮的调用和结果，`A<n>` 是最终答复，`SUM` 是压缩摘要。线上的形状写成 `u[text] a[text,tool_use] u[tool_result]`，一个词是一轮。

### 2.1 所有会调 provider 的路径

全仓库调用 `generate` 或 `generate_stream` 的地方有五处：引擎的两个 turn 策略（`engine/loop.py:569`、`:622`），摘要器（`entry/assembly.py:711`），Desktop 标题（`entry/desktop/title.py:112`），Desktop 的 provider 连通测试（`entry/desktop/providers.py:562`）。`observability/provider.py` 和 `evals/live.py` 是包装层，原样转发。

| 编号 | 路径 | 交给适配器的序列 | 探针结果 |
|---|---|---|---|
| P1 | 主循环，压力在 NONE、WARN | `[S, *history, U]`，之后每轮追加。不经过 `repair_tool_pairs` | 历史合法时合法。历史本身有问题时原样发出，见 §2.3 |
| P2 | SOFT、FULL，摘要成功 | `[S, SUM, *tail]`。摘要是 user 消息（`context/summary.py:284`） | 6000 次里 0 次以 assistant 开头 |
| P3 | SOFT、FULL，摘要失败，降级截断 | head 留得下一部分时是 `[S, rest[0], …]`；一条都留不下时是 `[S, *tail]` | 后一种情形以 assistant 开头，见 §2.2 |
| P4 | EMERGENCY | `[S, rest[0], 放得下的其余消息]` | 3000 次里 0 次以 assistant 开头，5 次末条是 assistant |
| P5 | augmentor（计划块、规划闸门、记忆提醒） | 在末尾追加 user 消息（`planning/injector.py:166-173`、`context/nudge.py:56`） | 工具结果后面跟一条文字，线上是 `u[tool_result] u[text]`，两条 user 相邻。按 A2 合法，合并后 `tool_result` 在前，满足 A4 |
| P6 | 恢复的会话 | `SqliteSessionStore._load` 读回的历史，逐字段还原，角色是 `Role` 枚举 | 存进去什么取出来就是什么，不做任何规整 |
| P7 | 子代理 | `[S, U(prompt)]` 起步，没有压缩器，没有 augmentor（`entry/subagent.py:393`） | 合法。空 prompt 在 `subagent/task_tool.py:160-165` 就被拒了 |
| P8 | 摘要器和记忆提取 | `[S, U]`（`entry/assembly.py:705-708`） | 合法。提取在没有可渲染文字时不发请求 |
| P9 | Desktop 标题和连通测试 | `[S, U]` | 合法。空白文本返回 422，不发请求 |

产品自己的 surface 写不出以回复开头的历史：每次交换都以用户消息开头，写回的压缩结果以摘要或 `rest[0]` 开头。P6 读回的历史如果本来就以回复开头（手工改过的数据，或者库调用方存进去的），会原样到适配器，探针的 G 段演示了这种输入。

### 2.2 以 assistant 开头

触发条件。`fit_to_budget` 从 `head[:len(head)]` 试到 `head[:1]`，都放不下时返回 `repair_tool_pairs((*kept, *tail))`（`context/transcript.py:245-250`）。也就是 pin、`head[0]` 和 tail 三者加起来超过降级目标时，head 整个丢掉。降级目标是 `usable` 的 60%（SOFT）或 70%（FULL）。tail 是最后 6 条消息，它的第一条如果是孤儿工具结果会被修复删掉，剩下的第一条在工具循环里是一条带调用的 assistant 消息。system 提示很大、或者最近几条工具结果很大时会走到这里。生产里多常见没有量过。

真实装配的一次（`probe_03_assembly.py` 的 A 段，`out_03_assembly.log`）。会话和计划 0078 §1.4 的一样：第一次交换读三遍小文件后答复，第二次交换读三遍小文件再同时读两张表，窗口定在第 9 次模型调用前到 FULL 档，摘要器返回空串。从 `SessionRegistry.submit` 一路走到 `AnthropicProvider._request_params`：

```
第 8 次调用   roles=suatatatauatatat，12931 token
              请求 15 轮，first=user

第 9 次调用   roles=satatt，16235 token（降级的 FULL 压缩，没写回）
              请求的键：max_tokens、messages、model、system、temperature、tools
              system 47030 字符，9 个工具，messages 4 轮：
              a[tool_use] u[tool_result] a[tool_use,tool_use] u[tool_result,tool_result]
              first=assistant
```

OpenAI 适配器对同一个视图发出的角色序列是 `satatt`。

随机会话（`probe_02_fuzz.py`，3000 段，`out_02_fuzz.log`）。每段会话都以 user 消息开头，和 entry 层组装的一样。每段分别走六条路径：

| 路径 | 以 assistant 开头 | 末条是 assistant |
|---|---|---|
| 不压缩 | 0 | 0 |
| SOFT，摘要成功 | 0 | 0 |
| SOFT，降级 | 153（5.1%） | 0 |
| FULL，摘要成功 | 0 | 0 |
| FULL，降级 | 180（6.0%） | 0 |
| EMERGENCY | 0 | 5（0.2%） |

333 个以 assistant 开头的视图里，298 个（89%）第一轮带 `tool_use`，35 个只有文字。这些比例随生成器的参数变，生产里的频率没有量过（R7）。

10 段有名字的对话逐条路径的结果在 `out_01_tiers.log`。四种降级原因（没有摘要器、抛错、返回空串、摘要超过 `usable`）得到的视图相同。

### 2.3 首条消息之外的违规形状

| 形状 | 哪条路径 | Anthropic | OpenAI 方言 |
|---|---|---|---|
| 没被回答的工具调用后面直接是 user 消息 | P1。上一次交换的 `stop_reason` 是 `TRUNCATED`，被截断的那条 assistant 消息带着工具调用进了历史（`engine/loop.py:700-721`），entry 层原样提交（`entry/session.py:814-816`） | 违反 A3。参数被截在 JSON 中间时，适配器的 `decode_arguments` 先抛错，请求没发出去 | 违反配对规则 |
| 末条是 assistant | P4：最新的用户消息太大，EMERGENCY 把它跳过。P1：`submit(session, "")` 时 `_opening` 不追加消息（`engine/loop.py:666-667`） | 违反 A9，限 Claude 4.6 及之后 | OpenAI 推断接受 |
| `messages` 为空 | P1：新会话上 `submit(session, "")`，序列只有 `[S]` | 违反 A10 | 只发一条 system |
| 两条 user 相邻 | P5，以及没有内容的 assistant 轮被适配器丢掉之后 | 按 A2 合法 | 合法 |

第一种的探针结果（`probe_03_assembly.py` 的 C 段）：第一次交换 `terminal=converged`、`stop=truncated`，历史是 `uata`；第二次交换的第一次调用，Anthropic 请求是 `u[text] a[tool_use] u[tool_result] a[text,tool_use] u[text]`。存进 SQLite 再读回来也一样（G 段）。这种历史在压力到 SOFT 以上时会被 `repair_tool_pairs` 补上占位，低于 SOFT 时每次都原样发出。`docs/core-features/agent-loop.md:499` 把它记成了已知限制："调用方如果要从这里恢复，必须自己补上 Observation 或丢弃这些调用"，entry 层没有做这件事。

空文本那两种情形，CLI 和 Desktop 到不了：CLI 跳过空行（`entry/cli/_repl.py:585-587`），Desktop 提交要求 `content` 非空（`entry/desktop/turn_submission.py:312-314`）。channel 的各个平台适配器没有逐个核对。`SessionRegistry.submit` 自己不检查文本，库调用方直接调它或 `run_turn` 就会出现。

### 2.4 请求被拒之后

`engine/retry.py:209-234` 对 429 和 5xx 以外的状态码不重试。交换以 `terminal="failed"` 结束，`session.history` 保持交换开始前的样子（`entry/session.py:817-819`）。

`probe_03_assembly.py` 的 B 段用一个替身 provider 模拟"拒绝以 assistant 开头的请求"，替身是探针自己写的。同一段会话：第二次提交在第 9 次调用上失败，报错没有重试，历史留在 8 条，第二次交换的请求和四轮工具调用都没留下。用户重发同一条消息时模型要从头再做一遍。如果摘要器还在失败、最近的工具结果还是那么大，会停在同一个地方；这一句是推断，脚本化的模型演示不了。

## 3. 在哪一层处理

| 编号 | 做法 | 谁知道约束 | 管得到哪些请求 | 别的后端 |
|---|---|---|---|---|
| L1 | Anthropic 适配器里规整 | 适配器。它的模块 docstring 列的就是 schema 和 Anthropic 在结构上的每一处不同，并且写明这些转换只在这个文件里 | 发给这个后端的每一个请求，不管是谁拼的：P1 至 P9，加上库调用方 | 不受影响 |
| L2 | 引擎在调 provider 之前统一规整 | 引擎要替一个后端记住一条规则。引擎现有的两处规整（`_answer_every_call`、`observations`）管的是两个方言都要求的事 | 只有经过引擎的请求，P8、P9 不经过 | 12 个 OpenAI 方言预设的降级视图里都会多一条消息 |
| L3 | 压缩层保证不产出 | context 层是叶子层，不认识后端 | 只有压缩产出的视图。库调用方给的历史、P6 读回的历史管不到 | 都受影响 |
| L4 | 压缩层尽量不产出，适配器兜底 | 各管各的 | 同 L1 | 压缩规则对所有后端生效，兜底只对 Anthropic |

L3 做不彻底。计划 0078 的方案 D 保住了请求，但请求在 tail 中间时视图仍以回复开头（0078 的 R6），因为 `min_tail` 条不动是现有约定。要在压缩层彻底解决，得往回扩 tail（可能超预算）、砍 tail（丢最近的工作）或者在压缩层造一条消息。

L2、L3 把补的消息放进了 `sent`，引擎里在它后面运行的东西都看得见它。规划闸门往回数到第一条 user 消息就停（`planning/injector.py:228-232`），补在最前面的占位会被当成本次交换的请求。token 估算也会算上它。补在适配器里，这些都看不到它。

对提示前缀缓存的影响：

- 原生 Anthropic 今天不打 `cache_control`（`docs/core-features/provider.md` §9），没有缓存可失。以后加断点时，占位排在 system 和工具声明之后，不动那一段前缀。
- 触发规整的视图是没写回、每次调用重截的降级视图，会话部分的前缀本来就在变（0078 §3.3 量过）。占位是固定字符串，同一个视图每次编码结果相同，不增加变动。
- 对合法的序列，规整是恒等的，正常调用的请求一个字节都不变。T3 钉住这一点。
- 走 L2 或 L3 时，按前缀自动缓存的后端（OpenAI、DeepSeek）也只在降级视图上多一条消息，结论相同。

L4 就是 0078 和本计划都落地之后的样子。本计划做的是 L1 这一半。

## 4. 怎么规整

以 §2.2 第 9 次调用的视图为例，四种做法发出的 `messages`：

```
今天        a[tool_use] u[tool_result] a[tool_use,tool_use] u[tool_result,tool_result]
N1 补占位   u[text] a[tool_use] u[tool_result] a[tool_use,tool_use] u[tool_result,tool_result]
N2 只丢     u[tool_result] a[tool_use,tool_use] u[tool_result,tool_result]
N2 连带丢   （空）
N3 并进去   u[text] a[tool_use,tool_use] u[tool_result,tool_result]
```

| 编号 | 做法 | 模型看到什么 | 丢了什么 | 配对 |
|---|---|---|---|---|
| N1 | 最前面补一条占位 user 轮 | 一句说明前面的对话看不到了，然后是原样的工具往返 | 不丢 | 不变 |
| N2 | 丢掉开头的 assistant 轮，直到遇到 user | 剩下的部分 | 开头那一轮。它带 `tool_use` 时，下一轮里它的 `tool_result` 成了孤儿（A5），必须连带删；删空的 user 轮后面又是一轮 assistant，继续删 | 不连带就违反 A5，连带就可能删空 |
| N3 | 把开头的 assistant 轮和回答它的结果并成一条 user 文字 | 一段转述：`[tool_call read_file(call_6)]: {"path": "small.txt"}`，下一行 `[tool_result call_6]: …` | 结构：这一轮不再是模型自己的调用，`is_error` 标记丢失 | 不变，被并的那一对一起变成文字 |
| N4 | 不规整，适配器在本地抛 `ProviderError` | 请求不发 | 这次交换 | |

在 333 个以 assistant 开头的视图上（`out_02_fuzz.log`）：

| 做法 | 规整后合法 | 删空 | 平均删掉的内容 |
|---|---|---|---|
| N1 | 333 | 0 | 0，多出 34 个字符 |
| N2，不连带 | 35，另外 298 个留下孤儿结果 | 0 | 1.5% |
| N2，连带 | 88 | 245（74%） | 83.1% |
| N3 | 333 | 0 | 0 |

N2 连带删的结果取决于视图里有没有 user 文字消息。纯工具循环的 tail 里没有，一路删到底。0078 的方案 D 落地后请求一定在视图里，N2 不再删空，但平均仍删掉 35.2%（`out_02b_fuzz_with_0078d.log`），删的是请求之前最近的工具往返。

N3 的问题在转述。`render_for_summary` 的 docstring（`context/transcript.py:342-353`）记过一条发现：在模型自己的视图里用文字转述历史工具调用，模型会开始用文字叙述调用而不去真的调用。那个渲染器因此只给摘要器用，docstring 要求任何人加第二个渲染器或把它的输出送回对话之前先读那一段。N3 要加的就是第二个渲染器，输出直接进对话。

N4 给出一条明确的本地报错。没有 `status_code` 的 `ProviderError` 会被引擎按一般预算再试（`generate_retries` 默认 3，`docs/core-features/provider.md` §9 的"确定性错误也会被重试"），每次结果相同，交换照样失败。

## 5. 推荐

### 5.1 规则

推荐 N1，放在 L1。`encode_conversation` 在编码完成之后看结果的第一轮：

1. 没有任何轮次时不动。空对话不替它造消息。
2. 第一轮的角色是 `user` 时不动。
3. 否则在最前面插入 `{"role": "user", "content": [{"type": "text", "text": OPENING_PLACEHOLDER}]}`。

判断看的是编码之后的第一轮。没有内容的 assistant 消息在编码时已经被丢掉，它后面如果是请求，第一轮就是 user，不需要补（T4）；按 schema 消息的角色判断会在这里多补一条。

占位原文：

```
[earlier conversation unavailable]
```

用英文，和仓库里其他给模型看的文字一致（`MISSING_TOOL_RESULT`、`PLANNING_GATE_TEXT`、`MEMORY_NUDGE_TEXT`）。用方括号，句式照 `MISSING_TOOL_RESULT` 的 `[tool result unavailable: the context was compacted]`，读起来是一条说明，不像用户提的要求。没有写原因，因为适配器不知道原因：降级截断会走到这里，库调用方给的一段以回复开头的历史也会。带原因的写法见 Q2。

暂存副本里的原型（`apply_prototype.py`）：

```python
OPENING_PLACEHOLDER = "[earlier conversation unavailable]"
"""Text of the user turn put in front of a conversation that opens on an assistant turn."""

    # the end of encode_conversation
    flush_tool_results()
    if turns and turns[0]["role"] != "user":
        opening = {"role": "user", "content": [{"type": "text", "text": OPENING_PLACEHOLDER}]}
        turns.insert(0, opening)
    return "\n\n".join(system_parts), turns
```

### 5.2 理由

- 不丢任何东西。视图里剩下的每一条消息、每一对调用和结果都原样发出。
- 不会造出新的配对问题。333 个视图规整后没有一个违反 A3 至 A5。
- 对合法序列是恒等的，别的后端完全不受影响。
- 补的那一轮只存在于请求里。历史、压缩记录、规划闸门、记忆提醒和 token 估算都看不到它，下一次调用重新判断。
- 管得到每一个发给 Anthropic 的请求，包括 P6 读回的历史和库调用方的输入。
- 改动小。暂存副本里跑 `tests/provider tests/context tests/engine tests/planning tests/entry tests/evals`，基线 3215 passed，换上原型后只有 3 条现有测试转红，都是拿单独一条 assistant 消息去编码的适配器测试（§6.2）。

### 5.3 放弃了什么

- 模型会多看到一条不是用户说的 user 消息。它对这句话的反应没有在真实模型上看过（R2）。
- 请求不在视图里时，占位救不回请求。0078 落地前的降级视图里请求可能已经丢了，模型看到占位后只能凭计划块和最近的工具结果往下做，或者反问用户。这是 0078 要解决的事。
- 占位不计入 token 估算。它是 9 个估算 token，预算有 10% 的安全余量。
- 追踪和压缩记录里看不到它。`TracedProvider` 包在适配器外面，记的是编码之前的消息。适配器做的每一种转换都是这样。

### 5.4 这道防线的账

防的是什么：SOFT、FULL 降级截断把 head 丢光之后，发给 Anthropic 原生接口的请求被 400 拒绝，交换失败，已经做完的工具调用作废。

旁路在哪：经 OpenAI 方言访问的 Claude（OpenRouter 的默认模型）和 Gemini 的兼容端点走的是另一个适配器，这道防线管不到，它们拒不拒绝也没有核实（Q3）。末条是 assistant 的 400 不归它管（Q4）。

值不值：owner 日常用的是 DeepSeek，走不到这段代码，眼下没有人因为它失败。成本是 3 行逻辑和一个常量，对别的路径没有影响。约束如果不存在，代价是降级视图最前面多一句话。

## 6. 改动范围

### 6.1 代码

| 文件 | 改动 |
|---|---|
| `omicsclaw/provider/anthropic_provider.py` | 新增模块常量 `OPENING_PLACEHOLDER`，加进本模块的 `__all__`。`encode_conversation` 按 §5.1 加 3 行。`encode_conversation` 的 docstring 从 "Two structural moves" 改成三件，新增一段写明：编码结果以 assistant 轮开头时在最前面补一条 user 轮，什么时候会出现，补的是哪句话，不删任何消息，判断在丢掉空 assistant 轮之后做。模块 docstring 的对照表加一行：首轮，schema 不限，Anthropic 要求是 user |

`omicsclaw/provider/__init__.py` 的 `__all__` 不变，`encode_conversation` 本来就不在包的公开面上。`openai_provider.py`、`engine/`、`context/`、`entry/` 都不改。

如果审核方更愿意让 `encode_conversation` 保持逐条翻译，另一种写法是把这 3 行放进一个私有函数，由 `_request_params` 在 `encode_conversation` 之后调用。这样 §6.2 的 3 条现有测试不用改，但 `tests/context` 里 4 处拿 `encode_conversation` 检查线上形状的地方看到的就不再是完整的请求。本计划不推荐。

### 6.2 测试

T1 至 T11 写在 `test_plan0079_proposed.py` 里跑过。参数化后 50 个用例，在今天的代码上 19 红 31 绿，在原型上 50 绿（`out_05_proposed_tests_today.log`、`out_06_proposed_tests_prototype.log`）。对原型做了 9 处定点变异，每一处都至少让一条新测试转红（`out_09_mutations.log`）。

`tests/provider/test_anthropic_provider.py`：

| 编号 | 测试 | 钉住的行为 | 今天 |
|---|---|---|---|
| T1 | `test_a_conversation_that_opens_on_an_assistant_turn_gets_a_user_turn_in_front`，角色是枚举和普通字符串各一条 | 第一轮是占位，占位原文逐字相等，其余轮次不变，system 照常提出去 | 2 条全红 |
| T2 | `test_the_turn_in_front_leaves_every_tool_call_with_its_results` | §2.2 的形状，含一轮并行调用：占位之后的每一轮和字面量逐块相等 | 红 |
| T3 | `test_a_conversation_that_opens_on_a_user_turn_is_encoded_as_before`，4 种形状 | 以 user 开头的对话编码结果和字面量相等，没有占位 | 4 条全绿。它防的是多补，"总是补"的变异让它转红 |
| T4 | `test_an_assistant_turn_with_nothing_to_send_does_not_count_as_the_opening` | 开头是一条空的 assistant 消息、后面是请求时不补 | 绿。"按 schema 角色判断"的变异让它转红 |
| T5 | `test_a_conversation_with_nothing_to_send_stays_empty` | 只有 system 消息或什么都没有时，轮次列表是空的 | 绿。"空对话也补"的变异让它转红 |
| T6 | `test_the_turn_in_front_reaches_the_blocking_request` | `generate` 交给 `messages.create` 的 `messages` 以占位开头 | 红 |
| T7 | `test_the_turn_in_front_reaches_the_streaming_request` | `generate_stream` 同上 | 红 |
| T8 | `test_encoding_the_same_conversation_twice_gives_the_same_turns` | 同一个视图编码两次结果相等，第一轮是那条固定的占位 | 红 |

`tests/provider/test_openai_provider.py`：

| 编号 | 测试 | 钉住的行为 | 今天 |
|---|---|---|---|
| T9 | `test_the_openai_dialect_sends_a_leading_assistant_message_as_it_is` | OpenAI 方言对以 assistant 开头的对话原样编码 | 绿。它记下"这个方言不补"是有意的，Q3 改了裁定就改它 |

新文件 `tests/entry/test_request_shape.py`（entry 是同时接压缩器和 provider 的那一层）：

| 编号 | 测试 | 钉住的行为 | 今天 |
|---|---|---|---|
| T10 | `test_whatever_a_compaction_leaves_the_anthropic_request_opens_on_a_user_turn`，3 段对话乘 4 种摘要器结果乘 SOFT、FULL、EMERGENCY | 真实的 `compact()` 返回的视图交给 `encode_conversation`，第一轮是 user | 36 条里 12 条红：两段对话的 SOFT、FULL 降级 |
| T11 | `test_the_turn_in_front_is_not_part_of_the_view_a_compaction_returns` | 降级压缩返回的视图里没有占位，编码之后才有 | 红 |

现有测试里要改的 3 条都在 `tests/provider/test_anthropic_provider.py`，它们拿单独一条 assistant 消息去编码再取 `turns[0]`：`test_an_assistant_tool_call_becomes_a_tool_use_block`（`:153`）、`test_assistant_reasoning_is_not_replayed_as_an_unsigned_thinking_block`（`:184`）、`test_a_round_trip_through_both_directions_preserves_a_tool_call`（`:420`）。各在前面加一条 user 消息，断言改取 assistant 那一轮。原型上的第一次运行是 4 failed、3211 passed，第 4 条是 `tests/entry/test_runtime_contract.py` 里读 `CLAUDE.md` 的测试，原因是暂存副本当时没导出这个文件；补上之后重跑 `tests/provider` 和那一条，3 failed、379 passed（`out_08_layers_prototype.log`、`out_08b_prototype_provider_and_contract.log`）。

不加脚本化 eval。脚本化 eval 的模型是 `ScriptedProvider`，适配器不在这条路上，eval 能做的只是把记下来的消息再编码一遍，T10 做的就是这件事。`tests/evals/test_dataset_floor.py` 的 `BASELINE` 不变。

### 6.3 文档

| 文件 | 改动 |
|---|---|
| `docs/core-features/provider.md` | §3.2 加一条：编码结果以 assistant 轮开头时补一条 user 轮，原文是什么，为什么判断放在编码之后。§9 加四行：这条规则没有对真实接口核实；OpenAI 方言不补，OpenRouter 上的 Claude 和 Gemini 兼容端点没有核实；末条是 assistant 时不处理；补的那一轮不在追踪和压缩记录里。§10 文件索引里 `anthropic_provider.py` 一行加上 `OPENING_PLACEHOLDER` |
| `docs/core-features/progressive-compactor.md` | §12 已知限制加一条：降级截断的视图可能以回复开头，Anthropic 适配器在请求最前面补一轮，别的后端原样发 |
| `docs/plans/0079-first-message-shape.md` | 加交付记录一节 |
| `CHANGELOG.md` | 合并时在顶部加一条 |

`README.md` 不动。`tests/entry/golden/` 是 system 提示和工具表，这次不变，不用重录。

估计工作量：代码和测试两小时，文档一小时。§7.3 的真实请求另算，取决于凭据。

## 7. 验证

### 7.1 档位

`SPEC.md` 第 4 档。provider 适配器在第 4 档的清单里。

### 7.2 要跑的

改动前在分支起点跑一次基线，改完再跑一次。命令照 `.github/workflows/eval.yml` 的 `unit-tests` 和 `eval` 两个 job：

```
/opt/conda/envs/rapids_singlecell/bin/python -m pytest tests/engine tests/entry tests/provider tests/tools tests/context tests/permission tests/hooks tests/memory tests/planning tests/observability tests/schema tests/skills tests/subagent tests/sandbox tests/skillenv tests/mcp tests/sdk tests/evals tests/launch tests/attachments tests/bench tests/test_*.py -m "not slow and not demo and not eval and not scripted_eval and not skill_example" -p no:cacheprovider
/opt/conda/envs/rapids_singlecell/bin/python -m pytest tests/evals/dataset -m scripted_eval -p no:cacheprovider
```

另外：

- 写测试时先在没改的代码上跑新测试，确认红绿和 §6.2 的表一致，再改代码。
- 变异检查：对落地的实现重做 9 处变异（`mutate_prototype.py` 改一下路径可以直接用），每处至少一条测试转红。
- 探针 `probe_02_fuzz.py` 和 `probe_03_assembly.py` 的 A 段在落地的实现上重跑，降级路径上以 assistant 开头的计数应当是 0。
- `Eval CI` 的四个 skill 示例 job 不经过 provider 适配器，本地不跑，合并时由 CI 跑。需要 fastapi 的 Desktop 用例在 `rapids_singlecell` 环境里跳过。两者汇报时都按"没跑"写。
- 合并到 `main` 由 owner 下令，合并前开 PR 让 `Eval CI` 跑完。

### 7.3 只能靠真实接口确认的

下面每个请求把 `max_tokens` 设成 16，请求体是完整的最小形式。V1 至 V5 要 Anthropic 凭据，本机没有。V6 用得到主检出 `.env` 里的 DeepSeek 凭据，本计划没有读也没有用，发不发由 owner 定。V7、V8 要 OpenRouter 和 Google 的凭据。

| 编号 | 要确认的 | 请求 | 预期 |
|---|---|---|---|
| V1 | A1 | `POST /v1/messages`，`{"model": "claude-haiku-4-5", "max_tokens": 16, "messages": [{"role": "assistant", "content": "Earlier answer."}, {"role": "user", "content": "Reply with OK."}]}`。换成 `claude-sonnet-4-6` 再发一次 | 400，报错提到首条消息的角色 |
| V2 | A1，第一轮带 `tool_use` 的形状 | 同上，加一个 `read_file` 工具声明，`messages` 是 `[{"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_01", "name": "read_file", "input": {"path": "a.txt"}}]}, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_01", "content": "hello"}]}]` | 400 |
| V3 | N1 被接受 | V2 的 `messages` 最前面加 `{"role": "user", "content": [{"type": "text", "text": "[earlier conversation unavailable]"}]}` | 200 |
| V4 | A2 | `messages` 是两条相邻的 user：`[{"role": "user", "content": "Hi."}, {"role": "user", "content": "Reply with OK."}]`。再发一次工具结果后面跟文字的：V3 的 `messages` 末尾加 `{"role": "user", "content": "Reply with OK."}` | 200 |
| V5 | A9 | `{"model": "claude-sonnet-4-6", "max_tokens": 16, "messages": [{"role": "user", "content": "Say hi."}, {"role": "assistant", "content": "Hi"}]}`。换成 `claude-haiku-4-5` 再发一次 | 前者 400，后者 200 |
| V6 | DeepSeek 对这两种形状的态度 | `POST /chat/completions`，`{"model": "deepseek-v4-flash", "max_tokens": 16, "messages": [{"role": "system", "content": "Be terse."}, {"role": "assistant", "content": "Earlier answer."}, {"role": "user", "content": "Reply with OK."}]}`。再发一次带未答调用的：`messages` 是 system、一条 user、一条带 `tool_calls`（id `c1`）的 assistant、一条 user | 前者推断 200，后者推断 400 |
| V7 | OpenRouter 上的 Claude | V6 的第一个请求，`model` 换成 `anthropic/claude-sonnet-4.6` | 不知道 |
| V8 | Gemini 兼容端点 | V6 的第一个请求，assistant 那条换成带 `tool_calls` 的并补上对应的 tool 消息，`model` 换成 `gemini-2.5-flash` | 不知道 |

有计划块的会话里，每次工具结果之后的调用发的都是 `u[tool_result] u[text]`，所以 Anthropic 上今天就依赖 A2。V4 确认的是这一点。

模型看到占位之后是否接着做事，要一段真实会话才看得到。办法和计划 0078 §6.3 的驱动脚本一样，provider 换成 `anthropic`，窗口定在 FULL，摘要器返回空串，看降级那次调用之后的回复。这也要 Anthropic 凭据。本计划没有做。

## 8. 与计划 0078 的关系

两份计划改的文件不重叠。0078 改 `omicsclaw/context/transcript.py`、`tests/context`、`tests/entry/test_planning.py`、`tests/evals/dataset/test_compaction.py` 和四份功能文档；本计划改 `omicsclaw/provider/anthropic_provider.py`、`tests/provider`、新文件 `tests/entry/test_request_shape.py` 和两份功能文档。两边都动的只有 `docs/core-features/progressive-compactor.md` 的 §12（各加一条）和 `CHANGELOG.md` 的顶部。

谁先落地都可以，没有顺序依赖。先后只影响 T10 里哪些用例在本计划落地前是红的（`out_10_proposed_tests_today_with_0078d.log`、`out_11_proposed_tests_prototype_with_0078d.log`，0078 的规则用的是它的审核方那份原型副本）：

| | 今天的适配器 | 本计划的原型 |
|---|---|---|
| 今天的 `fit_to_budget` | 19 红 31 绿 | 50 绿 |
| 0078 的方案 D | 13 红 37 绿 | 50 绿 |

0078 先落地的话，"system 提示大、tail 大"那段对话的 6 条降级用例在本计划落地前就绿了，因为方案 D 把请求留在了视图最前面。钉住本计划的是"请求在 tail 里、tail 以回复开头"那段对话的 6 条和 T11，它们在两种顺序下都是红的。

0078 落地之后本计划还剩多少事（`out_02b_fuzz_with_0078d.log`，同样的 3000 段会话）：

| | SOFT 降级 | FULL 降级 |
|---|---|---|
| 今天 | 153（5.1%） | 180（6.0%） |
| 0078 的方案 D | 98（3.3%） | 106（3.5%） |

剩下的都是 0078 的 R6：head 丢光，请求在 tail 里但不在 tail 的第一条。这时第一轮带 `tool_use` 的占一半左右（204 个里 108 个）。

本计划会影响 0078 的 Q2。那一条问要不要"更早的交换整段去留"，列的理由之一是避免视图以回复开头被 Anthropic 拒绝，并说明这条约束没有核实。本计划落地后适配器已经保证了首轮，这个理由不再需要。它的另外两条理由（更早的交换不会只剩没有请求的半截、多交换会话里切点稳定）不受影响。

## 9. 风险和没验证的部分

- R1 A1 没有对真实接口核实。依据是分发的 API 参考和第三方报错，公开 API 参考里没有这句话，参考自己在 A2 上还有一处矛盾。接口如果接受以 assistant 开头的对话，本计划补的那一轮就没有必要，多出来的只是一条 user 消息。
- R2 模型对占位的反应没有看过。可能的坏结果是模型把它当成一句需要回应的话，回一段解释而不接着调用工具。
- R3 `encode_conversation` 在模块的 `__all__` 里，名字和签名不变，行为变了。仓库内的调用方只有 `_request_params` 和测试；仓库外有没有调用方不知道。
- R4 Anthropic 兼容的第三方端点也走这个适配器（`base_url` 指过去）。它们对多出来的一条 user 消息的态度没有核实，按 A2 应当接受。
- R5 首条消息之外的三种形状本计划都不处理（§2.3）。其中未答调用那一种今天在两个方言上都会让会话卡住，直到一次写回的压缩把它修好。
- R6 OpenAI 方言不补。如果 OpenRouter 不替调用方规整，或者 Gemini 的兼容端点沿用原生接口的规则，这两个预设在降级视图上会遇到同一个问题。
- R7 §2.2 的 5.1% 和 6.0% 来自随机生成器，生产里降级截断把 head 丢光的频率没有量过。触发条件是 system 提示、head 的第一条和最近 6 条消息合起来超过 `usable` 的 60% 或 70%，窗口越大越难到。
- R8 合并冲突。本地还没合并进 `main` 的分支（`feat/ask-user`、`feat/ask-user-r3`、`feat/subagent-usage-desktop`、`backup/subagent-usage-reviewed`）相对 `main` 没有改 §6 列的文件。

这次顺带看到、不在本计划范围里的两处文字：

- `engine/loop.py:432-435` 的注释说丢了 assistant 消息会破坏 "Anthropic's user/assistant alternation"。按 A2，服务端不要求交替。
- `docs/core-features/provider.md` §9 说 `anthropic` 没有在任何清单里声明。`pyproject.toml:54-55` 和 `environment.yml:291-292` 现在都声明了。

## 10. 需要 owner 裁定的问题

Q1 现在做，还是等有了 Anthropic 凭据再做。现在做：按分发的参考和第三方报错落地，V1 至 V5 留到有凭据时补测，占位文字没有经过真实会话。等：先发 V1、V2，确认 A1 是真的再动手；这期间用 Anthropic 原生接口的人在降级截断时可能遇到 400。owner 以往的做法是措辞和行为改动先做真实会话对比，这次没有凭据做不了对比。建议现在做，理由是这条路径上今天没有一个能用的行为可以拿来比，改动对合法序列是恒等的，回退只是删 3 行。

Q2 占位文字。甲：`[earlier conversation unavailable]`，不说原因。乙：`[earlier conversation unavailable: the context was compacted]`，和 `MISSING_TOOL_RESULT` 同一个句式，模型在同一个视图里可能同时看到两者；但适配器不知道原因，库调用方给的以回复开头的历史也会带上这句话。建议甲。

Q3 OpenAI 方言要不要一起处理。做：在 `OpenAIProvider._request_payload` 里对 `is_anthropic_family` 为真的后端做同样的事，这个判断在 `apply_cache_breakpoints` 里已经用着。它管得到 OpenRouter 上的 Claude，管不到 Gemini。不做：等 V7、V8 证实了再说。建议不做，记进 `provider.md` §9。升级条件是出现第二个经真实请求证实需要这条规则的后端，那时把 3 行提成 `omicsclaw/provider/` 下两个适配器共用的函数。

Q4 末条是 assistant 和 `messages` 为空这两种形状归谁。它们要么来自 EMERGENCY 跳过了过大的最新请求，要么来自库调用方传空文本。前者的根因是请求被丢，0078 的 Q4 提议另开一份计划改 EMERGENCY 的任务锚点。建议本计划不处理，前者并入那份后续计划，后者记为已知限制。

Q5 被截断的交换留下的未答调用（§2.3 第一行）要不要另开计划。它不在适配器层：两个方言都中，修的地方在 entry 提交历史时或者引擎下一次开场时，做法是补一条占位结果或者丢掉那次调用。`agent-loop.md:499` 已经把责任写给了调用方。建议另开一份计划，并且排在本计划前面，因为它在日常用的 DeepSeek 上就会发生。

## 11. 证据清单

目录：`/tmp/claude-0/-workspace-dataset-private-zhouwg-data-OmicsClaw/58ae2e35-0792-4055-933c-1cfb79c727a6/scratchpad/plan-0079/`。探针用 `/opt/conda/envs/rapids_singlecell/bin/python` 跑，`PYTHONPATH` 指向 `5daf5482` 的检出，每份输出的第一行是 `omicsclaw.__file__`。`proto/` 和 `base/` 是 `git archive 5daf5482` 导出的两份副本。`apply_prototype.py` 只改 `proto/omicsclaw/provider/anthropic_provider.py`，原文留在旁边的 `.orig` 里。`base/` 里的这个文件用 `diff` 核对过，和 `5daf5482` 的相同。

| 脚本 | 输出 | 内容 |
|---|---|---|
| `p79_common.py` | | 对话构造器，两个方言的线上形状检查 |
| `probe_01_tiers.py` | `out_01_tiers.log` | §2：10 段对话，逐档、逐种摘要器结果的视图和两个方言的形状 |
| `probe_02_fuzz.py`、`p79_options.py` | `out_02_fuzz.log` | §2.2、§4：3000 段随机会话的六条路径，四种做法在 333 个视图上的结果 |
| 同上，`fit_to_budget` 换成 0078 的方案 D | `out_02b_fuzz_with_0078d.log` | §4、§8 |
| `probe_03_assembly.py` | `out_03_assembly.log` | §2.2 至 §2.4：真实装配的 A 至 G 段 |
| `test_plan0079_proposed.py`、`run_proposed.py`、`conftest.py` | `out_05_proposed_tests_today.log`、`out_06_proposed_tests_prototype.log` | §6.2：T1 至 T11 |
| 同上，`fit_to_budget` 换成 0078 的方案 D | `out_10_proposed_tests_today_with_0078d.log`、`out_11_proposed_tests_prototype_with_0078d.log` | §8 |
| 现有测试，六个目录 | `out_07_layers_baseline.log`、`out_08_layers_prototype.log`、`out_08b_prototype_provider_and_contract.log` | §5.2、§6.2 |
| `apply_prototype.py`、`mutate_prototype.py` | `out_09_mutations.log` | §6.2：9 处变异 |

只读引用的既有证据：`…/scratchpad/review-plan-0078/` 下的 `rv_out8_edges.log`、`rv_out4_alternatives.log`、`rv_out3b_assembly_request_in_tail.log`、`rv_out7b_fuzz_compare.log`，以及同目录的 `rv_strategies.py`（`out_02b` 和 `out_10`、`out_11` 用了它的 `plan_prototype`）。它们关于 `encode_conversation` 会编出首轮 assistant 的结论和这次重跑的一致。

官方文档页面：`platform.claude.com/docs/en/api/messages/create`、`…/build-with-claude/working-with-messages`、`…/agents-and-tools/tool-use/handle-tool-calls`、`…/api/errors`、`…/build-with-claude/mid-conversation-system-messages`，`api-docs.deepseek.com/api/create-chat-completion`，`openrouter.ai/docs/guides/features/message-transforms`。第三方：`discuss.ai.google.dev/t/…/46213`，`oss.issuehunt.io/r/lobehub/lobe-chat/issues/1601`。分发的参考在 Claude Code 的 `claude-api` skill 里，文件是 `python/claude-api/README.md` 和 `shared/error-codes.md`。SDK 源码读的是 `/tmp/omicsclaw-0075-ci.huDrtZ/venv/lib/python3.11/site-packages/` 下的 `anthropic`（1.11.0）和 `openai`（3.24.0）。
