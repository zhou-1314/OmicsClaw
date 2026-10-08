# 计划 0078：降级截断先丢什么（`fit_to_budget`）

**状态**：第 2 版（2026-10-08）。第 1 版（提交 `065ad974`）经独立审核，结论是"修改后可以做"，owner 就待裁定的问题给了答复。这一版按审核意见和裁定修订，待同一审核方复核、owner 批准。本计划没有改生产代码和测试。

**基线**：`main` 的 `5daf5482`，行号以它为准，实施时按符号名重新定位。参考实现 harness9 读的是本机 `/workspace/dataset/private/zhouwg_data/harness9`（`f92e839`）。

**编号**：方案 A 至 G+，场景 S1 至 S14，边界情形 E1 至 E12，测试 T1 至 T18，变异 M1 至 M17，风险 R1 至 R10，仍待裁定的问题 N1。

**证据**：探针脚本和输出在 `/tmp/claude-0/-workspace-dataset-private-zhouwg-data-OmicsClaw/58ae2e35-0792-4055-933c-1cfb79c727a6/scratchpad/plan-0078/`，第 2 版新做的以 `r2_` 开头，清单见 §11。审核方的证据在同级的 `review-plan-0078/`，下面记作 `RV`。正文里的 token 数都是 `estimate_messages_tokens` 的估算值，没有调用真实模型。第 1 版相对这一版改了什么见 §10。

## 0. 摘要

1. `fit_to_budget` 保留 head 里最旧的一段，从靠近尾部的一端开始丢。它的 docstring 和两份功能文档写的是反方向。名字叫"最旧的先走"的那条测试，对话是 849 token，预算是 900，函数在第一个判断就原样返回，剥离循环没有执行。
2. 多交换会话里，本次交换的请求在 head 靠后的位置，会连同它前后的几条一起被丢，更早的交换反而留着。head 和 tail 的边界切开一轮工具调用时，最先丢的就是那条调用，tail 里它的结果随即被修复删掉。
3. 参考实现的代码是同一个方向。它自己的功能文档写的是"保留放得下的最大后缀"，和代码不一致。
4. owner 裁定用方案 G+：压缩摘要、开头的 system 消息和当前请求不丢；其余从最旧的开始逐条丢；一次交换只要还有回复留着，它的请求就留着；tail 以回复开头时，这条回复所属交换的请求也留着。规则条文和边界情形在 §4。
5. G+ 的代价是降级期间会话部分基本不命中前缀缓存（探针里可复用前缀从 73% 至 82% 降到 4% 至 5%），以及丢到底时 tail 里的工具结果仍可能被删。后一条是这次修订新看到的，列为 N1 请 owner 定。
6. 改动是 `omicsclaw/context/transcript.py` 的一个函数加两个私有辅助函数，几处 docstring，四份功能文档，15 条单元测试（其中 2 条替换现有测试）加两组参数化用例，1 条脚本化 eval。按 `SPEC.md` 属于第 3 档。合并之前要做一对真实模型会话的对比。
7. G+ 落地后，SOFT、FULL 降级截断导致的闸门误触发不再发生。EMERGENCY 的任务锚点这次不改。
8. "发给模型的第一条消息必须是 user"不在本计划内，另行立项在 provider 适配器层处理；G+ 之后还会出现这种视图的情形写在 R1。

## 1. 现状

### 1.1 各函数保留什么、丢什么

| 函数 | 位置 | 保留 | 丢掉 |
|---|---|---|---|
| `split_head_tail(messages, pinned=, min_tail=)` | `transcript.py:155` | 只切分：前 `pinned` 条，最后 `min_tail` 条，中间是 head。pin 之后不超过 `min_tail` 条时没有 head | 不丢。按位置切，不看角色和工具配对，head 与 tail 的边界可以落在一次调用和它的结果之间 |
| `repair_tool_pairs(messages)` | `transcript.py:60` | 带工具调用的 assistant 消息后面紧跟的那一串 `Role.TOOL` 消息里，id 对得上的结果 | 其他位置的 `Role.TOOL` 消息删掉；缺结果的调用补一条占位 `MISSING_TOOL_RESULT` |
| `fit_to_budget(...)` | `transcript.py:192` | pinned、tail，以及 head 最旧的一段 `head[:head_end]` | head 里从靠近 tail 的一端起逐条丢。`head[:1]` 也放不下时整个 head 丢掉，只剩 pinned 加 tail |
| `emergency_fit` / `_emergency_survivors` | `transcript.py:253`、`:292` | pinned；pin 之后的第一条，无条件；其余从新到旧，放得下的留 | 放不下的那条跳过，接着看更旧的 |
| `plan_compaction(...)` | `compaction.py:249` | NONE、WARN 全留；SOFT 把 head 较旧的一半交给摘要；FULL 把整个 head 交给摘要；EMERGENCY 用 `_emergency_survivors`，目标是 `usable × full_at` | |
| `apply_compaction(plan, ...)` | `compaction.py:310` | pinned、摘要消息、`plan.tail`（SOFT 的 `plan.tail` 含 head 较新的一半），然后修复配对 | `plan.head`，由摘要代替 |
| `fall_back(reason)` | `compaction.py:680`，`compact` 的内部函数 | 调 `fit_to_budget(working, budget, pinned=, min_tail=, counter=, target_tokens=_fallback_target(budget, plan.pressure))`，`working` 是 offload 之后的对话 | 同 `fit_to_budget` |
| `_fallback_target(budget, pressure)` | `compaction.py:577` | SOFT 取 `usable × warn_at`（0.60），FULL 取 `usable × soft_at`（0.70），其他档取 `usable` | |

`fall_back` 有四个调用点：没有摘要器（`:699`）、摘要器抛错（`:712`）、摘要器返回的内容既没有锚点也没有摘要（`:720`，摘要超时返回空串走这里）、摘要后的结果超过 `usable`（`:736`）。全仓库只有 `fall_back` 调用 `fit_to_budget`。

降级的结果不写回历史（`progressive.py:41` 的 `should_write_back`），下一次模型调用从完整历史重新截一遍。EMERGENCY 的结果写回。

`fit_to_budget` 先判断候选放不放得下，再修复配对，修复补的占位不计入。

下面用标签表示消息：`S` 是 system 消息，`U<n>` 是第 n 次交换的用户请求，`a<n>.<k>` 和 `t<n>.<k>` 是第 n 次交换第 k 轮的 assistant 调用和工具结果（并行调用的结果是 `t<n>.<k>a`、`t<n>.<k>b`），`A<n>` 是第 n 次交换的最终答复，`SUM` 是压缩摘要，带 `*` 的是修复补的占位。没有另说时都是 `pinned=1`、`min_tail=6`，和 `entry/compaction.py` 的 `build_compactor` 一致。

### 1.2 单次长交换（S1）

一条用户请求后跟 12 轮工具往返，26 条，1411 token。输出在 `out_01_current.log`。

```
截断前        S U1 a1.1 t1.1 a1.2 t1.2 ... a1.12 t1.12
切分          pinned: S
              head:   U1 a1.1 t1.1 ... a1.9 t1.9            （19 条）
              tail:   a1.10 t1.10 a1.11 t1.11 a1.12 t1.12

SOFT 正常     S SUM a1.5 t1.5 ... a1.12 t1.12               摘要代替 U1 和前 4 轮
FULL 正常     S SUM a1.10 t1.10 a1.11 t1.11 a1.12 t1.12     摘要代替 U1 和前 9 轮

SOFT 降级     S U1 a1.1 t1.1 ... a1.6 t1.6 a1.7 t1.7* a1.10 t1.10 a1.11 t1.11 a1.12 t1.12
（目标 1129） 丢掉 t1.7 a1.8 t1.8 a1.9 t1.9，补占位 t1.7*，1411 降到 1101
FULL 降级     与 SOFT 降级相同（目标 1122）

EMERGENCY     S U1 a1.1 t1.1* a1.2 t1.2* a1.3 t1.3* a1.4 t1.4 ... a1.12 t1.12
（目标 1164） 丢掉 t1.1 t1.2 t1.3，补三条占位，26 条还是 26 条，1411 降到 1141
```

四种降级原因（没有摘要器、抛错、返回空串、摘要超过 `usable`）得到的视图相同。

降级视图里请求还在，因为它是 `head[0]`，最后才轮到。模型看得到最早的 6 轮和最近的 3 轮，中间第 7 轮的结果到第 9 轮看不到，第 7 轮的调用后面是一条"结果不可用"的占位。

EMERGENCY 的贪心是逐条判断的：预算快用完时约 100 token 的工具结果放不下，被跳过，只有几个 token 的调用消息还放得下，被留下，修复再给它们补占位。

### 1.3 多交换会话（S2）

两次已结束的交换各 4 轮，本次交换进行到第 5 轮，32 条，1676 token。

```
截断前        S U1 a1.1 t1.1 ... a1.4 t1.4 A1 U2 a2.1 t2.1 ... a2.4 t2.4 A2 U3 a3.1 t3.1 ... a3.5 t3.5
切分          pinned: S
              head:   U1 ... A1 U2 ... A2 U3 a3.1 t3.1 a3.2 t3.2   （25 条）
              tail:   a3.3 t3.3 a3.4 t3.4 a3.5 t3.5

SOFT 正常     S SUM a2.2 t2.2 a2.3 t2.3 a2.4 t2.4 A2 U3 a3.1 t3.1 ... a3.5 t3.5
FULL 正常     S SUM a3.3 t3.3 a3.4 t3.4 a3.5 t3.5                  U3 由摘要代替

SOFT 降级     S U1 a1.1 t1.1 ... a1.4 t1.4 A1 U2 a2.1 t2.1 a2.2 t2.2 a2.3 t2.3 a2.4 t2.4* a3.3 t3.3 a3.4 t3.4 a3.5 t3.5
（目标 1341） 丢掉 t2.4 A2 U3 a3.1 t3.1 a3.2 t3.2，补占位 t2.4*，1676 降到 1284
FULL 降级     与 SOFT 降级相同（目标 1333）

EMERGENCY     S U1 a1.1 t1.1* a1.2 t1.2* a1.3 t1.3* a1.4 t1.4 A1 U2 ... A2 U3 a3.1 t3.1 ... a3.5 t3.5
（目标 1382） 丢掉 t1.1 t1.2 t1.3，补三条占位，结果 1406，比目标多出的是占位
```

降级视图留下了第一次交换的全部和第二次交换的大部分，丢了第二次交换的答复、本次交换的请求 `U3` 和本次交换的前两轮。规划闸门从末尾往回数到 `U2` 才停，数到 7 轮，本次交换实际 5 轮。

派发本计划时的问题描述说请求"正好在 head 的末尾，最先被丢"。这只在本次交换恰好有 3 轮（请求之后正好 `min_tail` 条）时成立。轮次更多时，先丢的是请求和尾部之间的那几轮，然后才是请求。轮次更少时请求在 tail 里，不会丢。

### 1.4 经过真实装配的一次

用脚本化 eval 的 Runner（`SessionRegistry` 到 `TurnRunner` 到引擎，system 提示和工具表是真实的，模型是 `ScriptedProvider`）跑两次交换：第一次读三遍小文件后答复，第二次读三遍小文件再同时读两张表。窗口用 `Headroom` 定在第 9 次模型调用前到 FULL 档，摘要器返回空串（摘要超时的返回值）。用例在 `eval_plan0078_scratch.py`，输出在 `out_15_eval_today.log`。

第 9 次模型调用收到 6 条消息，角色依次是 system、assistant、tool、assistant、tool、tool。两次交换的请求都不在里面。截断前的对话是 17,174 token，其中第一次调用时的 system 提示加第一条请求就有 11,808，两张表在 tail 里，head 再怎么丢也到不了目标，循环走到最后一行，把整个 head 丢掉。

### 1.5 head 和 tail 的边界切开一轮时

边界落在一轮工具调用中间时（调用在 head 末尾，结果在 tail 开头；或者一条并行调用的结果一半在 head、一半在 tail），今天最先丢的就是这一轮留在 head 里的部分。调用一丢，tail 里它的结果成了孤儿，被修复删掉。并行调用越宽越容易出现，宽度不小于 `min_tail` 时整个 tail 都是它的结果。S13 是一个例子：最后一轮是 6 个并行调用，今天的降级视图是 `S U1 ... A1 U2 a2.1 t2.1 a2.2 t2.2*`，tail 的 6 条结果一条不剩，视图只有目标的 59%。

`r2_probe_tail.py` 按生产的目标范围量了这件事的频率：格式完好的随机对话 6,000 段（调用宽度 1 到 6，目标取对话的 0.74 到 0.875，对应 SOFT、FULL 两档的回退目标），边界切开一轮的有 3,793 段，今天的截断在 3,474 段里删掉了 tail 的消息，其中 684 段把 tail 删光（`r2_out_05_tail.log`）。这个生成器里并行调用比真实会话多，数字偏高；它说明的是边界切开一轮时今天几乎必然丢 tail。

### 1.6 文字和代码的出入

- `transcript.py:201` 的 docstring 写 "Peel messages off the front"，`:203` 写 "The oldest compactible message goes first"，`:229` 写 "peeling the front off can orphan a tool result"。模块 docstring `:18` 也写 "peels them off the front"。代码丢的是 head 的后端。
- `tests/context/test_summary_gates.py:188` 的 docstring 写 "It peels the oldest compactible messages one at a time"。
- `docs/core-features/context-engineering.md:284` 和 `docs/core-features/progressive-compactor.md:342` 都写"从 head 最旧处逐条剥离"。
- `docs/plans/0030-context-assembly-layer.md` 写的是"逐条剥头"（`:349`、`:1273`、`:2517` 等），没有说哪一端。`:1273` 说它取代的旧层是"块感知的最新后缀裁剪"，也就是保留最新的。
- `tests/context/test_transcript.py:556` 的 `test_the_oldest_compactible_message_goes_first_and_one_at_a_time`：对话 849 token，预算 900，`fit_to_budget` 返回的就是输入本身（`out_13_existing_test.log`）。三条断言对任何实现都成立。把预算调到 500 至 800 让它真的要截，这三条断言在两个方向上仍然都成立。
- `tests/context/test_transcript.py:591` 的 `test_peeling_the_head_strands_a_tool_call_and_the_repair_papers_over_it` 是唯一一条结果取决于方向的现有测试：它断言留下一条调用和一条占位，这只在从 head 后端丢时成立。

## 2. 参考实现

读到的：

- `internal/memory/compaction.go:95-132` 的 `TokenBudgetCompactor.Compact` 是 `rest[:headEnd]`，`headEnd` 递减，和这里的代码同向。把它的源码复制到暂存目录跑同样两段对话（`out_02_reference_go.log`），同样是留 head 最旧的一段、丢靠近尾部的几条，切点落在调用和结果之间时补一条占位。
- 循环上的注释 `:116` 是"逐条剥离头部旧消息，直到 token 预算满足或头部清空"，没有说哪一端。它在 `610898a`（2026-05-30，注释中文化）之前是 "Shrink head one message at a time"（`f4299b3`，2026-05-19）。类型上的注释 `:76` 是"从中间删除旧消息，保留 system 消息和最近 MinTailMessages 条消息"。
- 它的功能文档 `docs/核心功能/context-engineering.md` §6.2 写的算法是二分搜索最大的 `tailLen`，使 `[system] + msgs[N-tailLen:]` 不超过 `MaxTokens`，也就是保留放得下的最大后缀，和它的代码不一致。
- 它的测试也没有钉方向。`compaction_test.go:235` 的 `TestTokenBudgetCompactor_CompactsOldMessages` 里 head 被整个丢掉，两个方向都通过。
- `cmd/swebench/runner.go:293-299` 把 `TokenBudgetCompactor` 直接当作基准测试的压缩器，预算是窗口的 55%，`MinTailMessages` 是 8。每个实例从一个任务开始，`rest[0]` 就是任务。`:391-395` 的验证关卡在 agent 没跑过测试就结束时，会在同一个会话里再注入一条 user 提示，所以一个实例也可能有两条 user 消息。
- 产品入口 `cmd/harness9/main.go:425` 用 `ProgressiveCompactor`，`TokenBudgetCompactor` 只在摘要失败时兜底。`internal/engine/history.go:50-67` 每次运行加载整段会话历史再追加新的用户输入，所以产品路径上有多交换会话。
- 规划闸门是引擎里的计数器（`internal/engine/loop_phases.go:201-205` 的 `lc.turnsSincePlanWork`），不读历史。

据此的判断：

- 派发本计划时的问题描述说"这个不一致是从参考实现带过来的"。带过来的是代码的方向。参考实现循环上的注释没有说先丢最旧的，"off the front"和"the oldest goes first"是移植时写的。参考实现也有文字和代码的不一致，在它的功能文档里。
- 在它的基准测试里这个方向保住了任务：`rest[:headEnd]` 一直留着 `rest[0]`。验证关卡注入的那条提示在 head 靠后的位置，和这里多交换会话的形状一样，它被丢时模型回到原来的任务上。按它功能文档写的算法反而会先丢任务，而丢任务的后果它在 issue #117 里量过（连续 143 轮 EMERGENCY，任务失败）。
- 在它的产品路径上是同样的问题。用它基准测试的参数跑 S2，结果是 `S U1 a1.1 ... A1 U2 a3.2 t3.2 a3.3 ... t3.5`，本次请求 `U3` 被丢。闸门的后果在它那里不存在，因为计数不读历史。
- 这个方向是有意选的还是无意的，没有读到说明。提交记录和注释里没有提"保住任务"，任务锚点是 `d449cdd`（2026-09-15）才加到 `CompactForce` 上的，功能文档写的又是另一种算法，所以更像是无意的。这是推断。

## 3. 方案比较

### 3.1 方案

| 方案 | 规则 |
|---|---|
| A | 照 docstring：head 里从最旧的开始丢，没有例外 |
| B | 今天的代码 |
| C | 当前请求不丢，其余从最旧的开始丢 |
| C+ | C，加上压缩摘要不丢 |
| D | 第 1 版的推荐：C+，加上"本次交换之前的交换整段去留"（候选视图以一条请求已不在的回复开头时跳过） |
| E | 当前请求不丢，其余沿用今天的方向 |
| F | 直接用 EMERGENCY 的算法，目标换成降级目标 |
| G | 审核方提出：C+，加上"每次交换的请求是该交换里最后被丢的一条" |
| G+ | owner 裁定采用：G，加上"head 丢光而 tail 以回复开头时，保留 tail 所在交换的请求" |

"当前请求"指对话里位置最靠后的 user 角色消息。进入持久历史的 user 消息只有两种：一次交换的用户输入，和压缩摘要（计划 0039 §9.3 核对过，这次重新 grep 了 `Message.user(` 和 `Role.USER`，结论相同）。所以它要么是本次交换的请求，要么是代替了这条请求的摘要，和规划闸门往回数时停下的是同一条。

G 和 G+ 是用这份计划自己的实现跑的（`r2_rules.py`），和审核方的对照实现的比对见 §4.4。表里的 G+ 是 §4 的定稿规则，比裁定的字面多两个条件（system 消息、摘要要看角色），这两个条件在下面的场景里不起作用。

### 3.2 十四个场景

每个方案都替换进真实的 `compact()` 跑（探针进程里换掉 `omicsclaw.context.compaction.fit_to_budget` 这个模块属性），offload、定档、降级目标和记录都是生产代码。摘要器抛错，SOFT 和 FULL 各一次，每个方案 28 次降级压缩。脚本是 `r2_probe_scenarios.py`，输出在 `r2_out_02_scenarios.log`。

场景：S1 单次长交换；S2 三次交换，本次 5 轮；S3 较早的一次交换 9 轮只读，本次 4 轮；S4 之前写回过摘要，之后来了新请求；S5 请求已被摘要代替；S6 三次交换，本次 2 轮；S7 并行工具调用；S8、S9 本次请求很大；S10 对话里没有 user 消息；S11 较早的一次交换 12 轮，本次 1 轮；S12 较早的一次交换 30 轮，新请求刚发出；S13 最后一轮是 6 个并行调用，结果占满 tail；S14 较早交换的请求很大。S11 至 S14 是这一版加的，S6、S11、S12 是请求在 tail 里的形状。

S1，SOFT 降级，目标 1129：

| 方案 | 发送的视图 | 丢掉 |
|---|---|---|
| A | `S a1.4 t1.4 ... a1.12 t1.12` | `U1` 和前 3 轮。请求没了 |
| B、E | `S U1 a1.1 ... a1.6 t1.6 a1.7 t1.7* a1.10 ... t1.12` | `t1.7` 和第 8、9 轮，补一条占位 |
| C、C+、D、G、G+ | `S U1 a1.4 t1.4 ... a1.12 t1.12` | 前 3 轮 |
| F | `S U1 a1.1 t1.1* a1.2 t1.2* a1.3 t1.3* a1.4 ... t1.12` | 前 3 轮的结果，补三条占位，1141 超过目标 |

S2，SOFT 降级，目标 1341：

| 方案 | 发送的视图 | 丢掉 |
|---|---|---|
| A、C、C+ | `S a1.4 t1.4 A1 U2 a2.1 ... A2 U3 a3.1 ... t3.5` | `U1` 和第一次交换的前 3 轮。视图以 assistant 消息开头 |
| B | `S U1 ... A1 U2 a2.1 ... a2.4 t2.4* a3.3 ... t3.5` | `t2.4 A2 U3 a3.1 t3.1 a3.2 t3.2`，补一条占位。请求没了 |
| D | `S U2 a2.1 ... A2 U3 a3.1 ... t3.5` | 整个第一次交换。1154，比目标少 187 |
| E | `S U1 ... A1 U2 a2.1 ... a2.4 t2.4* U3 a3.3 ... t3.5` | `t2.4 A2 a3.1 t3.1 a3.2 t3.2`，补一条占位 |
| F | `S U1 A1 U2 a2.1 ... A2 U3 a3.1 ... t3.5` | 第一次交换的 4 轮 |
| G、G+ | `S U1 A1 U2 a2.1 ... A2 U3 a3.1 ... t3.5` | 第一次交换的 4 轮，请求 `U1` 和答复 `A1` 留着。1236 |

28 次的合计。每一列都是次数：

| 方案 | 当前请求丢失 | 摘要丢失 | 视图里有占位 | 修复后超目标 | 第一轮不是 user | 闸门提前触发 | 闸门该触发而没触发 | tail 的消息被删 | 低于目标的 60% | 保留量与目标之比的均值 |
|---|---|---|---|---|---|---|---|---|---|---|
| A | 6 | 4 | 0 | 0 | 28 | 0 | 0 | 0 | 2 | 0.90 |
| B | 10 | 0 | 24 | 3 | 2 | 6 | 2 | 4 | 4 | 0.87 |
| C | 0 | 2 | 0 | 0 | 22 | 0 | 0 | 0 | 0 | 0.95 |
| C+ | 0 | 0 | 0 | 0 | 20 | 0 | 0 | 0 | 0 | 0.96 |
| D | 0 | 0 | 0 | 0 | 6 | 0 | 0 | 0 | 8 | 0.72 |
| E | 0 | 0 | 26 | 5 | 2 | 0 | 2 | 4 | 2 | 0.95 |
| F | 2 | 0 | 23 | 21 | 2 | 2 | 0 | 0 | 2 | 0.96 |
| G | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 0 | 0.97 |
| G+ | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 0 | 0.97 |

"第一轮不是 user"按 `omicsclaw.provider.anthropic_provider.encode_conversation` 编码后的第一轮算。B、E、F、G、G+ 的 2 次都是 S10，对话里本来就没有 user 消息。D 的 6 次是 S10、S11、S12。最后一列是每次 `min(保留 token ÷ 目标, 1)` 的平均。

### 3.3 请求在 tail 里的形状

`min_tail` 是 6，所以每次新交换的前 0 到 2 轮，当前请求都在 tail 里，tail 的开头还是上一次交换最后的几条回复。第 1 版的 10 个场景里只有 S6 是这个形状，而且 S6 的 head 里有两次较早的交换，没有暴露问题。审核方指出了它（`RV/rv_out9_request_in_tail.log`），这里用自己的探针重做：较早的一次交换 6、12、30 轮，本次 0 到 3 轮，SOFT 和 FULL，共 24 次（`r2_out_02_scenarios.log` 的第二部分）。

| 方案 | 第一轮不是 user | 当前请求丢失 | 视图里有占位 | 保留量与目标之比 |
|---|---|---|---|---|
| A、C、C+ | 24 | 0 | 0 | 0.87 至 1.00 |
| B | 0 | 6 | 24 | 0.94 至 1.02 |
| D | 18 | 0 | 0 | 0.12 至 0.54 |
| E | 0 | 0 | 24 | 0.94 至 1.02 |
| F | 0 | 0 | 21 | 0.96 至 1.03 |
| G、G+ | 0 | 0 | 0 | 0.89 至 1.00 |

D 的问题：当前请求在 tail 里时，head 里唯一的 user 消息在最前面，除了原样输入以外的每个候选都以回复开头，全被跳过，结果整个 head 丢掉，剩下的 tail 自己又以回复开头。较早的交换 12 轮、本次 1 轮时，D 的视图是 `S a1.12 t1.12 A1 U2 a2.1 t2.1`，G+ 是 `S U1 a1.4 t1.4 ... a1.12 t1.12 A1 U2 a2.1 t2.1`。第 1 版推荐 D 是错的，规则 4 达不到它的目的。

G 和 G+ 在这 24 次和上面的 28 次里结果相同。两者的差别只在 head 丢光的时候，见 §4.2 的 E4b。

### 3.4 对四件事的影响

工具调用配对修复。从 head 后端丢（B、E）时，切点前面可能留下一条结果被丢的调用，修复给它补占位，28 次里 B 有 24 次、E 有 26 次出现占位。从最旧的开始丢时，切点后面可能留下调用已被丢的结果，修复把它删掉，不补任何东西。输入里本来就缺结果的调用，在任何方案下都仍由修复补占位。边界切开一轮时的情况见 §1.5 和 N1。

提示前缀缓存。降级结果不写回，每次调用重新截。`r2_probe_sequence.py` 用真实的 `ProgressiveCompactor` 按引擎的方式逐次调用，摘要器一直抛错，统计降级调用发送的 token 里，与上一次请求逐条相同的前缀占多少（`r2_out_06_sequence.log`）：

| 方案 | 单次长交换，9 次降级调用 | 两次较早的交换后一次长交换，12 次 | 一次 16 轮的交换后新交换从头开始，11 次 |
|---|---|---|---|
| B | 77% | 82% | 73% |
| E | 77% | 81% | 72% |
| D | 5% | 64% | 47% |
| C+ | 5% | 3% | 3% |
| G、G+ | 5% | 4% | 4% |

B、E 的切点在尾部一侧，head 前面那一段每次都一样。从最旧的开始丢时，历史每长一轮，切点就往后挪一轮，切点之后的内容每次都变。D 在多交换会话里的 64% 来自切点停在交换边界上不动，去掉规则 4 之后这一点没有了。探针里的 system 消息只有 51 token；真实部署里 system 提示和工具声明在任何方案下都是稳定前缀（§1.4 那次会话的第一次调用是 11,808 token），真实比例会比表里高，差别在会话部分。只有按前缀自动缓存的后端（OpenAI、DeepSeek）受影响。经 OpenAI 兼容网关访问 Anthropic 系模型时断点只打在 system 和最后一个工具上（`provider/openai_provider.py` 的 `apply_cache_breakpoints`），会话部分本来就不缓存；`anthropic_provider.py` 里没有打缓存断点的代码。

规划闸门。只要当前请求一定在视图里，往回数就越不过它。C、C+、D、E、G、G+ 都满足。差别在数得准不准：G+ 留的是本次交换最近的轮次；E 先丢本次交换自己的轮次，数得偏少。多交换序列里不压缩时闸门在第 9 次调用触发，G+ 也是第 9 次，E 是第 12 次，B 是第 6 次。第三个序列里 B 在第 4 次调用就触发，那时本次交换只有 3 轮，数到的是 15。细节见 §7。

记忆提醒。`MemoryNudge` 数的是发送视图里最后一次写记忆之后的 assistant 消息数，是 `every` 的倍数就提醒（`context/nudge.py:59`）。任何截断都会改变这个数。持续降级时视图大小基本不变，这个数也基本不变，结果是要么一直不提醒，要么连续提醒。多交换序列里，不压缩时在第 7、17 次调用提醒；B 和 E 在第 17、18、19 次连续提醒三次；G+ 一次也没有。这是现有行为的性质，各方案只是落点不同，按裁定只记录，不处理。

### 3.5 `emergency_fit` 的"任务"

`_emergency_survivors` 把 pin 之后的第一条 `rest[0]` 当作任务，按位置取，无条件保留（`transcript.py:309`）。docstring 说它"carries the task"。它在三种历史形状里分别是：

| 历史的形状 | `rest[0]` 是什么 | 当前请求是什么 |
|---|---|---|
| 新会话的单次交换 | 这次的请求 | 同一条 |
| 之前写回过摘要 | 摘要消息 | 之后来过新请求时是新请求，否则是摘要 |
| 多交换，从未写回摘要 | 第一次交换的请求 | 本次交换的请求 |

issue #117 的实测是第一种形状。第三种形状里 `rest[0]` 是一条过时的请求。`pinned=0` 而首条是 system 消息时，`rest[0]` 是 system 消息。

降级截断不用 `rest[0]` 这个定义。EMERGENCY 的任务锚点按裁定这次不改，以后另开计划，届时它的"不丢"要用 §4.1 第 2 条的同一个定义。第 1 版为它做的对照探针（`probe_06_gate.py`）留着备查。

## 4. 定稿的规则（G+）

### 4.1 条文

用到的词：请求是 user 角色的消息，压缩摘要也是 user 角色，规则里和请求同样对待；回复是 assistant 和 tool 角色的消息；一次交换是一条请求和它后面直到下一条请求之前的回复。

1. 原样返回输入，不重建也不修复：对话为空；已经不超过目标；没有 head；head 里每一条都不能丢。
2. 不丢的消息：
   - a. 前 `pinned` 条和最后 `min_tail` 条。
   - b. head 最前面连续的 system 消息。`pinned` 盖住了 system 消息时这一条不起作用，它管的是 `pinned=0` 这类调用。
   - c. head 里的压缩摘要：user 角色，并且 `is_summary_message` 为真。
   - d. 当前请求，也就是对话里最后一条请求，当它在 head 里时。
   - e. tail 的第一条是回复时，这条回复所属交换的请求，也就是 head 里最后一条请求。
   
   d 和 e 合起来是一句话：head 里最后一条请求不丢，除非 tail 的第一条本身就是请求。
3. head 里其余的消息从最旧的开始丢，一次一条，每丢一条量一次，不超过目标就停。
4. 请求跟着回复走：一次交换只要还有回复留着，它的请求就留着。轮到一条请求时，它后面还有自己的回复没丢，就先留着它，接着丢它的回复；它最后一条回复被丢的那一步，它一起被丢。后面没有回复的请求（紧跟着另一条请求）轮到时直接丢。
5. 能丢的都丢完还超过目标，就返回 pinned、第 2 条列的那些消息和 tail。这时结果高于目标。
6. 最后过一遍 `repair_tool_pairs`。量 token 在修复之前。

很多次交换时的先后：先丢最旧那次交换的回复，从它最旧的一条开始；这次交换的回复丢完时它的请求一起走；然后是下一次交换，依此类推；最后是本次交换自己的回复，从最旧的开始，本次的请求不丢。`r2_out_04_edges.log` 的 E11 把目标一步步调低，看到的顺序是：

```
输入          S U1 a1.1 t1.1 a1.2 t1.2 A1 U2 a2.1 ... A2 U3 ... A3 U4 ... A4 U5 a5.1 t5.1 ... a5.5 t5.5
目标 1839     S U1 a1.2 t1.2 A1 U2 ...                 第一次交换最旧的一轮先走，U1 留着
目标 1719     S U1 A1 U2 ...                           只剩答复时 U1 还在
目标 1619     S U2 a2.1 t2.1 a2.2 t2.2 A2 U3 ...       A1 和 U1 一起走
目标 1419     S U2 A2 U3 ...
目标 1299     S U3 a3.1 ...
...
目标  699     S U5 a5.1 t5.1 ... a5.5 t5.5             较早的交换都走完
目标  619     S U5 a5.2 t5.2 ... a5.5 t5.5             本次交换最旧的一轮开始走，U5 不丢
```

### 4.2 边界情形

每条都在 `r2_probe_edges.py` 里用一段对话跑过，输出在 `r2_out_04_edges.log`。"今天"是今天的函数在同一段对话上的结果。

| 情形 | 规则怎么走 | 例子 |
|---|---|---|
| 没有 user 消息（E1） | 第 2 条只有 a 起作用，回复从最旧的开始逐条丢。视图以回复开头，输入本身就是这样 | 12 轮，目标是 70%：`S a1.5 t1.5 ... a1.12 t1.12`。今天：`S a1.1 ... a1.6 t1.6* a1.10 ... t1.12` |
| head 只有一条摘要（E2a） | head 里没有可丢的，原样返回，哪怕远高于目标 | `S SUM a1.1 t1.1 a1.2 t1.2 a1.3 t1.3` 原样返回。今天把摘要丢了：`S a1.1 ... t1.3` |
| 摘要之后没有新请求（E2b） | 摘要是最后一条请求，不丢；它后面的回复从最旧的丢 | `S SUM a1.5 t1.5 ... a1.9 t1.9` |
| 当前请求本身超过目标（E3） | 走到第 5 条，结果是 pinned、请求和 tail | 请求 1,500 token，目标 800：`S U2 a2.4 t2.4 a2.5 t2.5 a2.6 t2.6`，1882。今天：`S U1 a1.1 t1.1 a1.2 t1.2 A1 a2.4 ... t2.6`，683，请求没了 |
| 当前请求在 tail 里，tail 以回复开头，目标够得着（E4a） | 较早的交换照第 3、4 条丢，head 里最后一条请求按 2e 不丢 | `S U1 a1.5 t1.5 a1.6 t1.6 A1 U2 a2.1 ... A2 U3 a3.1 t3.1 a3.2 t3.2` |
| 同上，目标够不着（E4b） | head 丢到只剩 2e 那一条 | `S U2 A2 U3 a3.1 t3.1 a3.2 t3.2`。G 没有 2e，结果和今天一样是 `S A2 U3 ...`，以回复开头 |
| tail 的第一条就是当前请求（E4c，T12） | 2d、2e 都不起作用，较早交换的请求按第 4 条和它最后一条回复一起走 | 目标够不着时结果是 pinned 加 tail |
| `pinned=0`，首条是 system（E5） | 2b，system 不丢 | `S U1 a1.4 t1.4 A1 U2 a2.1 ... t2.5` |
| 切点落在一条并行调用和它的结果之间（E6a） | 调用被丢，留下的结果是孤儿，修复删掉，不补占位 | 今天在这里补了一条占位 `t2.1c*` |
| head 和 tail 的边界切开一轮（E6c、E6e） | 那条调用在 head 最后，按第 3 条最后才轮到。轮到它被丢时，tail 里它的结果成孤儿被删 | E6e，6 个并行调用占满 tail，目标够不着：结果是 `S U2`。今天是 `S`。见 N1 |
| 两条请求连着（E7a） | 前一条后面没有回复，轮到时直接丢；后一条是当前请求 | `S U2b a2.1 ... t2.5` |
| 一问一答的闲聊之后一次带工具的交换（E7b） | 每次较早的交换是一条请求一条答复，成对走 | `S U4 A4 U5 a5.1 ... t5.4` |
| 一条 assistant 消息恰好以压缩标记开头（E8） | 不是 user 角色，不算摘要，和别的回复一样丢 | 它夹在第 3、4 轮之间：`S U1 a1.5 t1.5 ... a1.9 t1.9`。今天也不特殊对待它；审核方的对照实现会留着它 |
| 较早的交换 30 轮，只超目标 5%（E9） | 只丢最旧的两轮 | 3747 对目标 3768。审核方在同样的形状上量到第 1 版的 D 只剩 522（`RV/rv_out8_edges.log`） |
| 较早交换的请求很大（E10） | 请求按第 4 条留到它最后一条回复，占着位置 | 请求 600 token：`S U1 A1 U2 a2.1 ... t2.4`，6 轮都丢了，1174 对目标 1283 |
| pinned 前缀以一条调用结尾，结果在 head 开头（E12） | 这些结果是 head 里最旧的，最先丢，修复给 pinned 里的调用补占位 | `pinned=3`：`S U1 a1.1 t1.1a* t1.1b* a1.3 ...`。entry 层的 `pinned` 是 1，盖的是 system 消息，走不到这里。见 N1 |
| 角色是普通字符串（T15） | 比较一律用 `==`，理由同 `repair_tool_pairs` 的 docstring | |

### 4.3 原型

暂存副本里的原型由 `r2_apply_prototype.py` 写入，实施时可以另写，以 §5.2 的测试为准：

```python
def fit_to_budget(messages, budget, *, pinned=0, min_tail, counter=None, target_tokens=None):
    usable = budget.usable_tokens if target_tokens is None else target_tokens
    if not messages:
        return tuple(messages)
    if estimate_messages_tokens(messages, counter=counter) <= usable:
        return tuple(messages)

    kept, head, tail = split_head_tail(messages, pinned=pinned, min_tail=min_tail)
    if not head:
        return tuple(messages)

    held = _held(head, tail)
    if len(held) == len(head):
        return tuple(messages)
    candidate: tuple[Message, ...] = ()
    for cut in range(1, len(head) + 1):
        if cut - 1 in held:
            continue
        staying = held | _request_of_oldest_reply(head, held, cut)
        survivors = (*(head[i] for i in range(cut) if i in staying), *head[cut:])
        candidate = (*kept, *survivors, *tail)
        if estimate_messages_tokens(candidate, counter=counter) <= usable:
            break
    return repair_tool_pairs(candidate)


def _held(head: tuple[Message, ...], tail: tuple[Message, ...]) -> frozenset[int]:
    """Positions in *head* that a truncation never drops.

    A system message that opens the head, every compaction summary, and
    the last user message in the head unless the tail itself opens on a
    user message. That last one is the request in flight when the tail
    holds no request, and otherwise the request whose replies the tail
    opens on.
    """
    held: set[int] = set()
    for index, message in enumerate(head):
        if message.role != Role.SYSTEM:
            break
        held.add(index)
    held.update(
        index
        for index, message in enumerate(head)
        if message.role == Role.USER and is_summary_message(message)
    )
    if not (tail and tail[0].role == Role.USER):
        for index in range(len(head) - 1, -1, -1):
            if head[index].role == Role.USER:
                held.add(index)
                break
    return frozenset(held)


def _request_of_oldest_reply(
    head: tuple[Message, ...], held: frozenset[int], cut: int
) -> frozenset[int]:
    """The request that stays because a reply to it does.

    Everything from *cut* on survives. When the oldest of those that is
    not held is a reply, the nearest user message before it is its
    request, and the two stay together.
    """
    for index in range(cut, len(head)):
        if index in held:
            continue
        if head[index].role == Role.USER:
            return frozenset()
        for owner in range(index - 1, -1, -1):
            if head[owner].role == Role.USER:
                return frozenset({owner})
        return frozenset()
    return frozenset()
```

裁定里 G+ 那一条的字面是"head 丢光而 tail 以回复开头时，保留 tail 所在交换的请求"。原型把它写成"tail 以回复开头时 head 里最后一条请求从一开始就不丢"。两种写法的结果相同：head 里最后一条请求后面只有它自己的回复，按第 4 条它本来就要到 head 丢光那一步才会被丢。§4.4 的比对证实了这一点。

### 4.4 和审核方对照实现的比对

`r2_probe_fuzz.py` 在两个生成器上各跑 40,000 段随机对话（这份计划自己写的一个，审核方 `rv_probe7_fuzz.py` 的一个），目标取对话的 0.2 到 0.99，`min_tail` 取 0 到 8。输出在 `r2_out_01_fuzz.log`。审核方的 `rv_strategies.py` 复制到 `rv_copy/` 使用。

- 定稿规则的三种写法：打过补丁的生产函数、`r2_rules.gplus`、按"先丢前 n 条再放回请求"重写的 `gplus_second_writing`。前两者 64,322 次比对差异 0 次。第三种在 9 次上不同，都是同一种情况：输入里没有可丢的消息，前两者原样返回，第三种把输入修复一遍再返回。
- 照裁定字面写的 G 对审核方的 `anchor_request`：差异 0 次。
- 照裁定字面写的 G+ 对审核方的 `anchor_request_tail`：在审核方的生成器上差异 0 次，在自己的生成器上 2 次。`r2_probe_reading.py` 专门去找，60,000 段里找到 308 段，每一段都有一条以压缩标记开头的 assistant 消息排在 head 的最后。审核方的代码把这种消息当摘要留着，并且在它后面不再给 tail 找请求；字面读法会给。定稿规则里摘要要看角色，这种消息按普通回复处理，两种读法的分歧也就不存在了。
- 定稿规则对审核方的 `anchor_request_tail`：自己的生成器上 11,475 次不同，全部落在三类里，没有解释不了的：head 以 system 消息开头的 6,989 次（裁定 3，定稿规则留着它）；有一条非 user 的消息以标记开头的 4,484 次（定稿规则不留它）；输入里没有可丢的消息、一边原样返回一边修复后返回的 2 次。审核方的生成器上只有第三类，7 次。

所以定稿规则和审核方的 G+ 有三处不同，都是有意的：system 消息不丢；摘要看角色；没有可丢的消息时原样返回而不是修复后返回。第三处请审核方复核时确认：这里取的是"原样返回的输入不修复"，和函数已有的另外三种原样返回一致。

同一个探针在定稿规则的每个截断结果上数了规则声称的性质（自己的生成器 32,980 次截断，审核方的生成器 30,137 次）：

| 性质 | 违反次数 |
|---|---|
| 当前请求在结果里 | 0、0 |
| user 角色的摘要在结果里 | 0、0 |
| head 开头的 system 消息在结果里 | 0、0 |
| 留下的回复，它的请求（输入里它前面最近的 user 消息）也留着 | 0、0 |
| 结果里的占位都对应输入里本来就没应答的调用 | 0、0 |
| 留下的消息保持原来的先后 | 0、0 |
| 高于目标时，要么已经丢到第 5 条的底线，要么只多出输入里未应答调用的占位 | 0、0 |
| 第一轮不是 user 时，输入里那条回复前面确实没有请求 | 0、0 |

高于目标的有 3,722 次和 5,570 次，其中 3,162 次和 5,244 次是丢到了底线，560 次和 326 次是只多出占位。第一轮不是 user 的有 681 次和 1,128 次。

### 4.5 理由

- 请求一定在视图里，摘要也在。
- 留下的是最近的工作。SOFT 的正常路径摘要较旧的一半、原样留较新的一半，EMERGENCY 从新到旧装，今天只有降级截断留旧丢新。计划 0030 §5.2 说它取代的旧层是"最新后缀裁剪"，docstring 写"最旧的先走"，两处的本意都是留新的。
- 截断点不再造占位，模型不会看到自己发过、结果却写着"不可用"的调用。例外是 E12 那种 `pinned` 前缀以调用结尾的调用方式。
- 最新的一轮不再是最先丢的。§1.5 的探针里，今天在 6,000 段中的 3,474 段删掉了 tail 的消息，G+ 是 236 段（`r2_out_05_tail.log`）。剩下的 236 段见 N1。这一条是审核方补的理由（`RV/rv_out10_parallel_tail.log`），这里用自己的探针重做了。
- 较早的交换可以只留一部分，留下的部分带着它的请求。28 次场景里 G+ 平均用到目标的 97%，没有一次低于 60%；D 是 72%，有 8 次低于 60%。
- 视图以 user 消息开头，除非输入里第一条留下的回复前面本来就没有请求。
- 改动小：一个函数的循环，两个辅助函数。暂存副本里跑 `tests/context tests/planning tests/engine tests/entry tests/evals`，基线 2842 passed，换上原型后 2841 passed、1 failed，红的是 §1.6 最后那一条（`out_07_baseline_layers.log`、`r2_out_17_prototype_layers.log`）。

### 4.6 代价

- 降级期间的前缀缓存。三个序列里可复用前缀从 77%、82%、73% 降到 5%、4%、4%。单次长交换里每次降级调用重新读的 token 从 371 升到 1,522（每次发送约 1,600）。影响限于摘要器持续失败、压力又在 SOFT 以上的那些调用，每次这样的调用本来也要先付一次失败的摘要请求（超时的话是 90 秒）。有一种缓解办法这份计划没有评估：让切点分步挪动而不是每次挪一条。审核方试过每 8 条挪一次（`RV/rv_out5_sequence.log`），单次长交换回到 54%，多交换序列里 G 是 48%，代价是每次少发约 7% 到 14% 的 token。这次不做。
- 单次长交换里最早的几轮先丢。今天看不到的是中间几轮，G+ 之后看不到的是最早几轮。
- 较早交换的请求按第 4 条留到它最后一条回复，请求很大时占位置（E10、S14：S14 里 G+ 用到目标的 95%，较早的 6 轮丢了 4 轮）。
- 丢到底时，tail 里的工具结果仍可能被修复删掉。条件是 head 和 tail 的边界切开一轮，并且 head 丢到了那条调用。见 N1。
- 视图仍可能以回复开头，见 R1。
- `pinned` 前缀以一条调用结尾的调用方，它的结果会最先丢并被补上占位（E12）。entry 层不是这种调用方式。

## 5. 改动范围

### 5.1 代码

| 文件 | 改动 |
|---|---|
| `omicsclaw/context/transcript.py` | `fit_to_budget` 的循环按 §4.3 改；新增私有函数 `_held` 和 `_request_of_oldest_reply`；从 `.summary` 引入 `is_summary_message`（`summary.py` 只依赖 `offload.py` 和 schema，没有环）。`fit_to_budget` 的 docstring 重写，写清 §4.1 的六条、结果可能高于目标、切点只会留下孤儿结果。模块 docstring `:18` 的 "peels them off the front" 改掉。签名和 `__all__` 不变 |
| `omicsclaw/context/compaction.py` | 没有逻辑改动。`compact` 的 docstring 在讲回退的那一段加一句：回退不丢压缩摘要、开头的 system 消息和当前请求 |
| `omicsclaw/planning/injector.py` | 只改 `_gate_fires` 的 docstring：`:187-190` 那一段现在把 SOFT、FULL 的回退和 EMERGENCY 并列，改成只剩 EMERGENCY |

`pinned=0` 而首条是 system 消息时，`compact()` 仍会在 `advisories` 里写"it was treated as compactible history"。这句话对摘要路径仍然成立（system 消息在 head 里，会被摘要掉），对降级截断不再成立。这次不改它的措辞，在 `fit_to_budget` 的 docstring 里写明降级截断留着它。

如果审核方不接受 `transcript.py` 依赖 `summary.py`，另一种写法是给 `fit_to_budget` 加一个只能按关键字传的 `keep` 谓词，由 `fall_back` 传 `is_summary_message`。这会改公开函数的签名，本计划不推荐。

### 5.2 测试

T1 至 T17 写在 `r2_test_proposed.py` 里跑过，共 27 条用例（含参数化）：在今天的代码上 25 红 2 绿，在原型上 27 条全绿，在第 1 版的 D 上 5 红（`r2_out_08_proposed_today.log`、`r2_out_09_proposed_prototype.log`、`r2_out_11_proposed_first_version_rule.log`）。

`tests/context/test_transcript.py`：

| 编号 | 测试 | 钉住的行为 | 今天 |
|---|---|---|---|
| T1 | `test_the_oldest_rounds_go_first_and_one_message_at_a_time`，替换 `test_the_oldest_compactible_message_goes_first_and_one_at_a_time` | 先断言夹具确实超预算；请求之后留下的是对话最新的一段；再多留一轮就放不下 | 红 |
| T2 | `test_the_request_in_flight_survives_a_truncation_that_drops_earlier_exchanges` | 三次交换，截断后当前请求还在 | 红 |
| T3 | `test_the_request_is_kept_even_when_it_alone_misses_the_target` | 请求本身大于目标时，结果是 pinned、请求和 tail，高于目标 | 红 |
| T4 | `test_a_compaction_summary_survives_a_truncation` | 摘要在请求之前时两条都留，摘要仍紧跟 system | 绿。今天因为方向相反本来就留着；变异 M2 让它转红 |
| T5 | `test_an_earlier_request_stays_as_long_as_one_of_its_replies_does` | 两次较早的交换：第一次整个走，第二次丢掉最旧的两轮、留着请求和剩下的回复 | 红 |
| T6 | `test_an_earlier_request_goes_with_its_last_reply` | 目标容得下第一次交换的请求、容不下请求加答复时，请求不单独留下 | 红 |
| T7 | `test_a_truncation_never_fabricates_a_tool_result`，替换 `test_peeling_the_head_strands_a_tool_call_and_the_repair_papers_over_it` | 并行三个调用，切点落在调用和结果之间：结果里没有占位，每条消息都是输入里的，孤儿结果随调用一起消失 | 红 |
| T8 | `test_a_wide_parallel_call_keeps_its_results` | 最后一轮是 6 个并行调用、只需要丢一点时，这一轮完整留着，丢的是最旧的一轮 | 红 |
| T9 | `test_a_conversation_with_no_user_message_is_still_peeled_one_message_at_a_time` | 没有 user 消息的对话逐条丢，留下的是最新的一段，比 tail 多 | 红 |
| T10 | `test_a_head_with_nothing_droppable_is_handed_back_untouched` | head 只有一条摘要时返回输入本身，不重建（夹具带一条孤儿结果，重建会把它删掉） | 红 |
| T11 | `test_a_tail_that_opens_on_a_reply_keeps_the_request_it_answers` | 当前请求在 tail 里、tail 以上一次交换的答复开头、目标够不着：结果是 pinned、上一次交换的请求和 tail | 红 |
| T12 | `test_a_tail_that_opens_on_the_request_in_flight_keeps_no_earlier_request` | tail 的第一条就是当前请求时，较早的请求不受保护 | 绿。今天整个 head 被丢，碰巧成立；变异 M5、M17 让它转红 |
| T13 | `test_a_system_message_outside_the_pinned_prefix_is_kept` | `pinned=0` 时 system 消息和请求都留着，后面是最新的一段 | 红 |
| T14 | `test_a_message_that_only_quotes_the_marker_is_dropped_like_any_other` | 以标记开头的 assistant 消息和别的回复一样丢 | 红 |
| T15 | `test_a_request_whose_role_arrives_as_a_plain_string_is_still_a_request` | 角色是普通字符串时，较早的请求照样跟着它的回复留下，当前请求照样不丢 | 红 |

`tests/context/test_summary_gates.py`：

| 编号 | 测试 | 钉住的行为 | 今天 |
|---|---|---|---|
| T16 | `test_every_degraded_compaction_keeps_the_request_in_view`，五种降级原因乘 SOFT、FULL 两档 | 经过 `compact()` 的每一种降级，当前请求都在结果里，记录里 `degraded` 非空、`summarized` 为 0 | 10 条全红 |

同一个文件 `:188` 的 docstring（`test_the_fallback_still_keeps_the_pinned_prefix_and_the_tail`）写"peels the oldest compactible messages one at a time"并引 `compaction.go:117-131`。改动之后前半句成立，引用不再对应，改成指向 `fit_to_budget` 自己的 docstring。

`tests/entry/test_planning.py`（entry 是同时接压缩器和注入器的那一层）：

| 编号 | 测试 | 钉住的行为 | 今天 |
|---|---|---|---|
| T17 | `test_the_gate_counts_only_this_exchange_after_a_degraded_compaction`，SOFT、FULL 两档 | 上一次交换 9 轮只读，本次 4 轮，工具结果 40 字符，降级压缩后的视图交给 `PlanInjector`，不带闸门文本 | 2 条全红 |

`tests/evals/dataset/test_compaction.py`：

| 编号 | 测试 | 钉住的行为 | 今天 |
|---|---|---|---|
| T18 | 脚本化 eval `compaction/degraded_truncation_keeps_requests_and_recent_rounds` | 见下 | 红 |

第 1 版的 T13 按 §1.4 的形状写，审核方指出它钉不住方向：head 在那里被丢光，D、E、C+、G 在触发的那次调用上得到同一个视图，"没有占位"那条断言在今天的代码上也是绿的。T18 换成 head 不被丢光的形状，做法参考了审核方的 R2（`RV/rv_probe3_assembly.py`）：第一次交换读 6 个各不相同的中等文件（每个约 800 token，低于 offload 的 1,000 token 阈值，WARN 档不会把它们移走）再答复，第二次交换读 7 遍小文件。SOFT 落在第 15 次模型调用（`trigger_call=14`）。每个中等文件第一行有自己的标记，所以断言能分辨留下的是哪几个。

在第 15 次调用上断言：user 消息里有第二次交换的请求；user 消息里有第一次交换的请求；最新的中等文件的标记出现一次；最旧的中等文件的标记不出现；没有占位结果；system 消息在最前；有一条没写回的 SOFT 降级记录。`r2_probe_assembly.py` 把每个方案换进去跑同一个用例（`r2_out_13_assembly.log`）：

| 方案 | 第 15 次调用的角色序列 | 视图里的中等文件 | 没通过的断言 |
|---|---|---|---|
| 今天 | `suatatatatatatat` | 1、2、3 | 第二次的请求、最新文件、最旧文件、占位 |
| E | `suatatatatuatatat` | 1、2、3 | 最新文件、最旧文件、占位 |
| C+ | `satatauatatatatatatat` | 5、6 | 第一次的请求 |
| D | `suatatatatatatat` | 无 | 第一次的请求、最新文件 |
| G、G+ | `suatatauatatatatatatat` | 5、6 | 无 |

用 pytest 的形式在暂存副本里也跑了：今天红在 4 条断言上，换上原型后通过（`r2_eval_scratch.py`、`r2_out_14_eval_today.log`、`r2_out_16_eval_prototype.log`）。G 和 G+ 在这个用例上没有区别，两者的区别由 T11 和变异 M4 钉住。

`Headroom` 用的是 `target=SOFT, trigger_call=14, trigger_tokens=6150`：量到第 14 次调用的增长是 6,031，第 15 次是 6,213（`r2_out_12_assembly_measure.log`），取两者之间。实施时 system 提示或工具表变了要重量。`tests/evals/test_dataset_floor.py` 的 `BASELINE` 从 30 调到 31。

变异。对原型做了 17 处定点变异（`r2_mutate.py`，`r2_out_10_mutations.log`），每一处都至少让一条新测试转红，变异后文件按内容核对恢复：

| 变异 | 转红的新测试 |
|---|---|
| M1 改回今天的方向 | T1、T5、T7、T8、T9、T13、T14、T15 等 |
| M2 摘要不再不丢 | T4 |
| M3 head 里最后一条请求从不保留 | T3、T11、T15 |
| M4 去掉 2e（即 G） | T11 |
| M5 head 里最后一条请求无论 tail 怎么开头都保留 | T12 |
| M6 请求不跟着回复留（即 C+） | T5、T6、T15 |
| M7 请求在没有回复之后仍然留着 | T6 |
| M8 head 开头的 system 消息不保留 | T13 |
| M9 以标记开头的消息不看角色都算摘要 | T14 |
| M10、M11 两处角色比较改成 `is` | T15 |
| M12 回复的请求取 head 里第一条请求而不是最近的一条 | T5 |
| M13 没有可丢的消息时重建 | T10 |
| M14 第一个候选就返回 | T1、T3、T5、T11 等 |
| M15 不修复 | T7 |
| M16 head 里每条 user 消息都保留 | T5、T6、T15 |
| M17 2e 看 tail 的最后一条而不是第一条 | T12 |

现有测试里只有 §1.6 最后那一条会红，由 T7 替换。`test_a_history_that_fits_exactly_is_still_a_history_that_fits`、`test_every_transform_honours_an_injected_counter`、`test_every_way_a_summarizer_can_fail_degrades_and_leaves_a_trace` 等在原型上都通过，不用改。

### 5.3 文档

| 文件 | 改动 |
|---|---|
| `docs/core-features/context-engineering.md` | `:284` 表格里 `fit_to_budget` 那一行改成新规则 |
| `docs/core-features/progressive-compactor.md` | §5.3 最后一句（`:342`）改成新规则；§11 设计决策表加一行；§12 已知限制加四条：降级期间会话部分不命中前缀缓存；降级视图的第一条仍可能是回复，另行立项在 provider 适配器层处理；丢到底时 tail 里的工具结果可能被删（N1 裁定后按结果写）；EMERGENCY 的任务锚点仍按位置取 `rest[0]`，以后另开计划，届时用降级截断的同一个定义 |
| `docs/core-features/planning.md` | §13 第 3 条：去掉 SOFT、FULL 降级截断那一种，只留 EMERGENCY |
| `docs/core-features/eval.md` | 用例数有两处，`:7` 和 `:254`，都从 30 改成 31；§2.1 的表加 T18 |
| `docs/plans/0039-planning-layer.md` | §9.5 末尾加一条带日期的记录：SOFT、FULL 降级那一种由本计划关闭。原文不动 |
| `docs/plans/0055-nudge-and-closing-gate.md` | §10.6 的已知行为里记一条：持续降级时 `MemoryNudge` 可能连续提醒（§3.4） |
| `docs/plans/0078-fallback-truncation-order.md` | 加交付记录一节 |
| `CHANGELOG.md` | 合并时在顶部加一条 |

### 5.4 不改的

`emergency_fit`、`_emergency_survivors`、`plan_compaction`、`apply_compaction`、`_fallback_target`、`should_write_back`、`MemoryNudge`、`PlanInjector` 的逻辑都不动。provider 适配器不动。`docs/plans/0030-context-assembly-layer.md` 不动，"逐条剥头"没有说哪一端。`README.md` 不动。`tests/entry/golden/` 是 system 提示和工具表，这次不会变，不用重录。

按裁定，代码、测试、文档在一次改动里完成，不拆成先改文字再改行为两次。估计工作量：代码和测试半天，文档和变异检查半天，§6.3 的真实会话半天。

## 6. 验证

### 6.1 档位

`SPEC.md` 第 3 档。改动在一个包里，但第 2 档要求不改变 agent 看到的内容，这次改的是降级压缩后发给模型的消息；`fit_to_budget` 又在 `omicsclaw.context` 的公开面上。两档都沾边时取高的一档。

### 6.2 要跑的

改动前在分支起点跑一次基线，改完再跑一次：

```
/opt/conda/envs/rapids_singlecell/bin/python -m pytest tests/context tests/planning tests/engine tests/entry tests/evals -q -p no:randomly
/opt/conda/envs/rapids_singlecell/bin/python -m pytest tests/*/test_*layer*.py tests/sdk/test_boundary.py tests/sdk/test_public_surface.py tests/test_*.py -q -p no:randomly
```

在 `5daf5482` 的暂存副本上，两条命令的结果是：

| 命令 | 基线 | 换上 G+ 原型 |
|---|---|---|
| 第一条 | 2842 passed、11 skipped、26 deselected、3 xfailed，约 130 秒 | 2841 passed、1 failed，其余相同。红的是 T7 要替换的那一条 |
| 第二条 | 842 passed、14 skipped、1 deselected、1 xpassed，约 120 秒 | 相同 |

第一条里 deselected 的 26 条是 `tests/evals/live` 的真实模型路由用例，被 `pyproject.toml` 的 `addopts` 按 `eval` 标记排除。命令行上覆盖掉 `addopts` 时它们改为 skipped，跳过数是 37，审核方量到的就是这个数。用时取决于机器当时的负载，审核方同一条命令量到约 6 分钟。

另外：

- 写测试时先在没改的代码上跑新测试，确认红绿和 §5.2 的表一致，再改代码。
- 变异检查：对落地的实现重做 §5.2 的 17 处变异（`r2_mutate.py` 改一下路径可以直接用），每处至少一条测试转红。
- 探针 `r2_probe_scenarios.py`、`r2_probe_sequence.py`、`r2_probe_gate.py` 在落地的实现上重跑，"B today"那一行应当和"G+"那一行相同。`r2_probe_fuzz.py` 重跑，三种写法之间和 §4.4 的性质表都应当是 0。
- 合并到 `main` 属于第 4 档，由 `Eval CI` 跑。

需要 fastapi 的 Desktop 用例不单独跑：`CompactionRecord` 的字段和 COMPACTION 事件都没变。汇报时按"没跑"写。

### 6.3 真实模型的会话：合并的前置条件

按裁定，一对真实模型会话的对比是合并的前置条件，并且要有一条依赖上一次交换答复的追问。保留什么、丢什么是确定性的，上面的测试已经覆盖；这对会话看的是拿到新视图的模型怎么接着做。

怎么构造。DeepSeek 的窗口在 `_model_limits.py` 里是 1M，自然压力到不了 SOFT。不用改代码也不用线上抓包，脚本化 eval 的 Runner 已经够用：

1. 像 `tests/evals/live/test_live_routing.py` 那样，把 `.env` 的 provider 包进 `omicsclaw.evals.live.RecordingProvider`，交给一个 `Case`，用 `run_case` 跑。`RecordingProvider` 记下每次主线调用发出的消息。
2. `Case` 带 `followups`、`network=True`、`compaction=True` 和一个 `Headroom(target=Pressure.SOFT, ...)`，Runner 会据此换掉 `app.budget`。`trigger_tokens` 决定窗口大小；`trigger_call` 只用来检查有没有压缩过早写回，摘要一直超时就没有写回，取一个大致的值即可。
3. `config` 里设 `summary_timeout_s` 为一个极小的值（比如 `0.001`）和 `memory=False`。真实的摘要调用会超时，`_ProviderSummarizer` 返回空串，`compact()` 走降级截断。
4. 工作区放 T18 的 6 个中等文件和小文件，其中一个中等文件的阈值和别的不同。

三次用户输入：

- 第一次：读 6 个 panel 文件，说出哪一个的阈值和别的不一样。
- 第二次，依赖上一次的答复：针对刚才指出的那个 panel，再读小文件，然后回答那个 panel 的编号和阈值。不重读文件的话，这一问只有看得到上一次的答复才答得出。
- 第三次，依赖一条会被丢掉的旧工具结果：把第 1 个 panel 文件第 3 行的原文抄出来。G+ 下最旧的读取最先丢，这一问看的是模型发现内容不在视图里时怎么办。

同一组输入在 `main` 和修复分支上各跑一次。记录和判断：

- 每条 `degraded` 非空、`written_back` 为假的压缩记录之后的那次请求里，有没有当前请求、上一次交换的请求和答复、哪几个文件的读取结果。
- 第二问答对没有，之前有没有重读 panel 文件。
- 第三问是重读了文件、明说看不到，还是编了一行。重读一次是这条规则可以接受的代价，编造不是。
- 两个分支上模型调用的次数和输入 token（`RecordingProvider.usage`），用来看前缀缓存的代价在真实后端上有多大。

真实模型每次走的轮次和推理文字长度不同，`trigger_tokens` 可能要调一两次才让降级落在第二次交换里。把窗口再收紧一档、让第一次交换在第二问时被整段丢掉，可以再跑一对，用来看最坏的情况；这一对建议做，不算前置条件。

这对会话本计划没有做。

## 7. 对规划闸门残留误触发的影响

SOFT、FULL 降级那一种，G+ 落地后不再发生。当前请求要么在 tail 里，要么按 2d 留在 head 里，`_gate_fires` 往回数一定先遇到它。

`r2_probe_gate.py` 用计划 0039 §9.5 的形状（上一次交换 9 轮只读加一条答复，本次 3、4、5、7 轮，两档，摘要器抛错或返回空串，每个夹具 16 次）。今天误触发多少次取决于夹具，所以把工具结果的大小和 FULL 档取的比例都扫了一遍（`r2_out_07_gate.log`）：

| 工具结果的字符数 | FULL 取 0.85 | FULL 取 0.88 |
|---|---|---|
| 40 | 今天 16 次误触发，数到 12 或 13 | 16 次，12 或 13 |
| 100 | 14 次 | 16 次 |
| 200 | 12 次 | 12 次 |
| 400 | 12 次，数到 10、11、12，另有 4 次数到 4 没触发 | 12 次 |
| 800 | 12 次 | 12 次 |

10 个夹具共 160 次：今天 134 次误触发，请求被丢的也是 134 次；G+ 是 0 次，数到的正好是 3、4、5、7；E 也是 0 次，但数得偏少，本次 7 轮时只数到 3 到 5。第 1 版写的"16 次都误触发，数到 12 或 13"只对 40 字符那一行成立，审核方在 400 字符的夹具上量到的是 12 次（`RV/rv_out6_gate.log`）。

逐次调用的序列里（§3.4）：多交换序列今天在第 6 次调用触发，G+ 和不压缩时一样在第 9 次；第三个序列今天在第 4 次调用触发，G+ 在第 9 次。

降级截断之后计数仍可能偏少：丢到本次交换自己的轮次时，可见轮次少于实际轮次，凑不满 `gate_turns` 就不触发。这是计划 0039 §4.4 已经接受的"压缩后可见轮次不足"，方向是少提醒。

有两种情况计数仍会数进更早的交换，都不是降级截断造成的：库调用方用 `run_turn(app, history)` 而不给 `user_text`（计划 0039 §9.3 按同一次请求算）；之前某次 EMERGENCY 已经把请求从历史里丢掉并写回，之后的降级截断恢复不了它。

EMERGENCY 那一种不变。EMERGENCY 不经过 `fit_to_budget`，探针里三个场景在今天、E、G+ 下的视图逐条相同，其中两个误触发：当前请求很大、前面是一次 9 轮只读的交换时数到 11（实际 1）；当前请求 300 token、本次交换的结果很大时，贪心走到请求时剩余预算不够，请求被跳过，更旧更小的消息还放得下，数到 14（实际 6）。按裁定这次不改。

## 8. 风险和没验证的部分

- R1 视图的第一条仍可能是回复。G+ 之后，这只在输入里第一条留下的回复前面本来就没有请求时出现：对话里根本没有 user 消息（S10、E1），或者 head 开头是一段请求已经不在的回复。随机探针的 32,980 次截断里有 681 次，全部属于这一类。Anthropic 的 Messages API 是否拒绝这种对话没有核实，`anthropic_provider.py` 的 `encode_conversation` 也不处理它。按裁定这不并进本计划，另行立项在 provider 适配器层处理；本计划只把它记进已知限制。
- R2 "留新的比留旧的有用"是判断。§6.3 的会话是它唯一的真实模型证据，还没有做。
- R3 前缀缓存的数字来自探针里按消息逐条比较的前缀，system 消息只有 51 token。真实后端的缓存命中没有量，§6.3 会顺带量一次。
- R4 丢到底时 tail 里的工具结果可能被删，见 N1。
- R5 结果可能高于降级目标。原因有两种：不丢的那几条加 tail 本身就超过目标（当前请求很大、较早的请求按 2e 留着、摘要很大）；输入里本来就有没应答的调用，占位在判断之后补上。第 1 版只写了第一种，漏了第二种。两种都不会让结果高于 `usable`：能走到回退的对话压力低于 EMERGENCY，总量低于 `usable × 0.95`，占位每条只有十几个 token。
- R6 tail 的第一条是回复、head 里又没有任何请求时（比如一次很长的交换，它的请求之前被 EMERGENCY 丢掉并写回了），2e 没有可留的，视图以回复开头。归入 R1。
- R7 `fit_to_budget` 在 `omicsclaw.context` 的公开面上，名字和签名不变，行为变了。仓库内只有一个调用方；仓库外有没有调用方不知道。`pinned` 前缀以调用结尾的调用方见 E12。
- R8 合并冲突。本地还没合并进 `main` 的分支（`feat/ask-user`、`feat/ask-user-r3`、`feat/subagent-usage-desktop`）相对 `main` 没有改 §5 列的文件，在 `tests/evals` 下只动了 `test_runner.py`。它们如果新增脚本化 eval，`BASELINE` 和 `docs/core-features/eval.md` 的用例数要以合并时的数字为准。
- R9 Desktop 和 channel 没有跑。它们和 CLI 一样经 `SessionRegistry` 到 `TurnRunner`，T18 覆盖的是这一段共用路径。
- R10 裁定 3 只管降级截断。`pinned=0` 而首条是 system 消息的调用方，摘要成功时 system 消息仍在 head 里、仍会被摘要掉；EMERGENCY 时它是 `rest[0]`，被当成任务无条件保留，真正的请求反而没有锚点。这两条路径这次都不动。

按裁定只记录、不处理的两件事：

- 持续降级时 `MemoryNudge` 可能连续提醒。今天的代码在多交换序列的第 17、18、19 次调用连续提醒了三次（§3.4）。记进计划 0055 §10.6。
- `_emergency_survivors` 会留下结果被跳过的调用消息，每次补 3 到 5 条占位，补完之后可能高于它自己的目标（§1.2、§1.3）。留给以后的 EMERGENCY 计划。

## 9. 裁定

### 9.1 已裁定

1. 做法用 G+，第 1 版的规则 4（更早的交换整段去留）删掉。条文见 §4.1。
2. "发给模型的第一条消息必须是 user"不并进 0078，另行立项在 provider 适配器层处理。0078 把它写成已知限制（R1，`docs/core-features/progressive-compactor.md` §12）。
3. 开头的 system 消息列为不丢，`pinned=0` 时同样（§4.1 的 2b）。这里把"开头的"读成 head 最前面连续的 system 角色消息；出现在对话中间的 system 消息不在此列，本仓库的流程不会产生它。
4. 一对真实会话的对比是合并的前置条件，要有一条依赖上一次交换答复的追问（§6.3）。
5. EMERGENCY 的任务锚点这次不改，以后另开计划，届时定义和 §4.1 第 2 条一致。
6. 代码、测试、文档一起改，不拆两次。第 1 版 Q5 的两件范围外观察只记录（§8 末尾）。

### 9.2 仍要 owner 定的

N1 被固定边界切开的一轮工具调用，要不要整轮不丢。

这是修订时新看到的后果，裁定里没有覆盖。`min_tail` 的含义是最后几条消息原样留着（`split_head_tail` 的 docstring 写的是 "kept whatever happens"，`test_the_fallback_still_keeps_the_pinned_prefix_and_the_tail` 断言了 `result[-6:]`）。但 head 的最后一条是调用、它的结果在 tail 开头时，这条调用一旦被丢，tail 里的结果就被修复删掉。今天这是最先发生的事；G+ 之下要等 head 里别的都丢完才轮到它，也就是只在丢到底的时候。

发生的频率，按生产的目标范围（`r2_out_05_tail.log`，各 6,000 段，并行调用比真实会话多）：

| | 今天 | G+ | G+ 加这一条 |
|---|---|---|---|
| system 提示 50 token：tail 的消息被删 | 3,474 | 236 | 0 |
| 其中 tail 被删光 | 684 | 10 | 0 |
| 高于目标 | 245 | 107 | 315 |
| system 提示 10,000 token：tail 的消息被删 | 3,829 | 802 | 0 |
| 其中 tail 被删光 | 682 | 86 | 0 |
| 高于目标 | 878 | 791 | 1,188 |

system 提示大的时候更容易丢到底，脚本化 eval 的装配里 system 提示就有约 11,800 token。最坏的例子是 E6e：最后一轮 6 个并行调用占满 tail，目标够不着，G+ 发出去的是 `S U2`，模型只看得到请求；加上这一条是 `S U2 a2.5 t2.5a ... t2.5f`，589 对目标 500。E6c 是代价的例子：加上这一条之后结果是 1232 对目标 600，不加是 302。

加的话是 §4.1 第 2 条多一项：tail 以工具结果开头时，head 末尾发出这次调用的 assistant 消息和它在 head 里的结果不丢。`pinned` 前缀以调用结尾时 head 开头属于它的结果同理（E12），这一侧在 entry 层走不到，可以一起加也可以不加。`r2_rules.py` 的 `gplus_straddle` 是两侧都加的原型，约 25 行；没有为它跑测试矩阵和变异，采纳的话要补 2 到 3 条测试。

不加的话，`fit_to_budget` 的 docstring 和 `docs/core-features/progressive-compactor.md` §12 要写明：tail 里失去调用的工具结果会被修复删掉。

建议加 tail 这一侧。理由是 `min_tail` 的承诺已经写在 docstring 和测试里，最新一轮的结果是模型最需要的东西，而代价只是丢到底时多超出目标一些，仍低于 `usable`。`pinned` 那一侧建议不加，只写明。

## 10. 第 1 版到第 2 版改了什么

| 审核意见或裁定 | 处理 |
|---|---|
| P1-1 规则 4 达不到目的 | 属实，用自己的探针重现（§3.3：D 在 24 次里 18 次以 assistant 开头，保留量是目标的 12% 到 54%）。规则换成 G+。场景集加了 S11、S12 和 24 次的这一组 |
| P2-1 代价写得偏轻 | §4.6 按 G+ 的实际代价重写，数字重量：前缀复用 5%、4%、4%；S3 里 D 是目标的 41%，G+ 是 99% |
| P2-2 方案比较要包含 G 和 G+ | §3 的每张表都加了这两行，数字来自 `r2_probe_scenarios.py`、`r2_probe_sequence.py` |
| P2-3 T13 钉不住方向 | 换成 T18，head 不被丢光的形状。今天、E、C+、D 各有断言不过，G 和 G+ 全部通过（§5.2） |
| P2-4 真实会话不检验丢旧交换的代价 | §6.3 重写：前置条件，加了依赖上一次答复的追问和依赖旧工具结果的追问 |
| P3 §7 的数字只在特定夹具下成立 | §7 扫了 10 个夹具，写明每个数字的夹具 |
| P3 "每个实例只有一个任务"不准确 | §0、§2 改了，写明 `runner.go:391-395` 的验证关卡 |
| P3 `_held` 的摘要判定要看角色 | 2c 加了角色条件，T14、M9 钉住 |
| P3 R5 不全 | R5 补了第二种原因，§4.4 的性质表里分开计数 |
| P3 §5 漏的文字 | 补了 `test_summary_gates.py:188` 和 `eval.md` 的两处 |
| P3 §6.2 只报了一条命令的数字 | 两条都报了，写明 skipped 与 deselected 的差别和用时的差别 |
| P3 "补的占位"一列口径不一 | §3.2、§3.3 的每一列都是次数 |
| P3 前缀缓存的缓解办法 | §4.6 写成没有评估，引了审核方的数字，这次不做 |
| P3 补一条支持改方向的理由 | §1.5、§4.5，用自己的探针重做 |
| 裁定 1 至 6 | §9.1 |

第 1 版的 §3.4 末尾（EMERGENCY 的对照做法）、Q1 至 Q5、测试 T1 至 T13 的编号都不再沿用。第 1 版的探针和输出留在原处，文件名不带 `r2_`。

## 11. 证据清单

目录：`/tmp/claude-0/-workspace-dataset-private-zhouwg-data-OmicsClaw/58ae2e35-0792-4055-933c-1cfb79c727a6/scratchpad/plan-0078/`。探针用 `/opt/conda/envs/rapids_singlecell/bin/python` 跑，`PYTHONPATH` 指向 `5daf5482` 的检出；标"原型"的指向暂存副本 `proto_g/`（`git archive 5daf5482` 导出的包和三个测试目录，`r2_apply_prototype.py` 只改了其中的 `transcript.py`）。`proto_full/` 是整棵树的导出，两条命令的基线和原型运行、T18 的 pytest 形式在里面跑，跑完后它的 `transcript.py` 已恢复成 `5daf5482` 的内容。`rv_copy/` 是审核方 `rv_strategies.py`、`rv_common.py`、`rv_probe7_fuzz.py` 的副本。

第 2 版：

| 脚本 | 输出 | 内容 |
|---|---|---|
| `r2_rules.py` | | G、G+ 的实现：定稿规则、照裁定字面的两条、第二种写法、N1 的变体 |
| `r2_apply_prototype.py` | | 把定稿规则写进暂存副本 |
| `r2_probe_fuzz.py` | `r2_out_01_fuzz.log` | §4.4：三种写法、对审核方实现的比对、性质表（原型） |
| `r2_probe_reading.py` | `r2_out_03_reading.log` | §4.4：裁定字面和审核方代码的分歧在哪 |
| `r2_probe_scenarios.py` | `r2_out_02_scenarios.log`、`.json` | §3.2、§3.3：九个方案，14 个场景加请求在 tail 里的 24 次 |
| `r2_probe_edges.py` | `r2_out_04_edges.log` | §4.1、§4.2：E1 至 E12 |
| `r2_probe_tail.py` | `r2_out_05_tail.log` | §1.5、N1：tail 的消息被删的频率 |
| `r2_probe_sequence.py` | `r2_out_06_sequence.log`、`.json` | §3.4：三个逐次调用的序列 |
| `r2_probe_gate.py` | `r2_out_07_gate.log` | §7：闸门，10 个夹具 |
| `r2_test_proposed.py` | `r2_out_08_proposed_today.log`、`r2_out_09_proposed_prototype.log`、`r2_out_11_proposed_first_version_rule.log` | §5.2：T1 至 T17 |
| `r2_mutate.py` | `r2_out_10_mutations.log` | §5.2：17 处变异 |
| `r2_probe_assembly.py` | `r2_out_12_assembly_measure.log`、`r2_out_13_assembly.log` | §5.2：T18 的形状在每个方案下 |
| `r2_eval_scratch.py` | `r2_out_14_eval_today.log`、`r2_out_16_eval_prototype.log` | T18 的 pytest 形式 |
| 现有测试 | `out_07_baseline_layers.log`、`r2_out_15_baseline_guards.log`、`r2_out_17_prototype_layers.log`、`r2_out_18_prototype_guards.log` | §6.2：两条命令的基线和原型 |

第 1 版仍在引用的：`common.py` 和 `strategies.py`（对话的搭法和方案 A 至 F 的实现，第 2 版的探针接着用）、`probe_01_current.py`（`out_01_current.log`，§1.2、§1.3）、`h9ref/internal/memory/zz_probe0078_test.go`（`out_02_reference_go.log`，§2）、`probe_07_existing_test.py`（`out_13_existing_test.log`，§1.6）、`eval_plan0078_scratch.py`（`out_15_eval_today.log`，§1.4）。

只读引用的：审核方的 `RV/rv_out9_request_in_tail.log`、`rv_out3_assembly.log`、`rv_probe3_assembly.py`、`rv_out5_sequence.log`、`rv_out6_gate.log`、`rv_out8_edges.log`、`rv_out10_parallel_tail.log`；更早的 `.../scratchpad/review-gate/evidence/fit_direction.log`、`tiers.log`、`tiers2.log`。
