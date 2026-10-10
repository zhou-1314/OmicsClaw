# 计划 0078：降级截断先丢什么（`fit_to_budget`）

**状态**：已实现，等独立审核和 owner 下令合并（2026-10-10）。第 4 版（2026-10-09）经同一位复核方看过增量，结论是"可以作为实现的依据"，留给实现的四件事和实现的经过记在 §12。owner 2026-10-09 在第 3 版之后作了裁定：R11 的改法并进定稿；2f 第 3 步保留宽读法；实现在第 4 版复核通过后派发。§1 至 §11 是计划，写的时候没有改生产代码和测试；§12 是实施记录。实施时按复核意见改了正文的几处文字，列在 §12.2。

**基线**：`main` 的 `5daf5482`，行号以它为准，实施时按符号名重新定位。参考实现 harness9 读的是本机 `/workspace/dataset/private/zhouwg_data/harness9`（`f92e839`）。

**编号**：方案 A 至 G+，场景 S1 至 S14，边界情形 E1 至 E13，测试 T1 至 T30，变异 M1 至 M33，风险 R1 至 R11。实施时加了测试 T31，重做变异时带上了复核方的 Y1 至 Y7（§12）。N1 是第 2 版留给 owner 的问题，已裁定，条文是 §4.1 的 2f。R11 是第 3 版记下的后果，已裁定，条文是 §4.1 的第 4 条。

**证据**：探针脚本和输出在两个目录。第 1、2 版的在 `/tmp/claude-0/-workspace-dataset-private-zhouwg-data-OmicsClaw/58ae2e35-0792-4055-933c-1cfb79c727a6/scratchpad/plan-0078/`，第 2 版的以 `r2_` 开头。第 3、4 版的在 `/tmp/claude-0/-workspace-dataset-private-zhouwg-data-OmicsClaw/9885f10a-3799-4309-9dfb-0981c166422a/scratchpad/plan-0078-r3/`，第 3 版的以 `r3_` 开头，第 4 版的以 `r4_` 开头。清单见 §11。前两轮审核方的证据在第一个目录同级的 `review-plan-0078/` 和它下面的 `r2/`，下面记作 `RV` 和 `RV2`；第 3 轮复核方的在 `/tmp/claude-0/-workspace-dataset-private-zhouwg-data-OmicsClaw/9885f10a-3799-4309-9dfb-0981c166422a/scratchpad/review-plan-0078-r3/`，记作 `RV3`。正文里的 token 数都是 `estimate_messages_tokens` 的估算值。这份计划没有调用真实模型；R11 引的真实模型样本是复核方发的。各版之间改了什么见 §10。

## 0. 摘要

1. `fit_to_budget` 保留 head 里最旧的一段，从靠近尾部的一端开始丢。它的 docstring 和两份功能文档写的是反方向。名字叫"最旧的先走"的那条测试，对话是 849 token，预算是 900，函数在第一个判断就原样返回，剥离循环没有执行。
2. 多交换会话里，本次交换的请求在 head 靠后的位置，会连同它前后的几条一起被丢，更早的交换反而留着。head 和 tail 的边界切开一轮工具调用时，最先丢的就是那条调用，tail 里它的结果随即被修复删掉。
3. 参考实现的代码是同一个方向。它自己的功能文档写的是"保留放得下的最大后缀"，和代码不一致。
4. owner 裁定用方案 G+，再加 N1 的尾部一侧和 R11 的改法：压缩摘要、开头的 system 消息和当前请求不丢；其余从最旧的开始逐条丢；一次交换只要还有 assistant 回复留着，它的请求就留着，工具结果留不住请求；tail 以回复开头时，这条回复所属交换的请求也留着；tail 以工具结果开头时，被 head 和 tail 的边界切开的那一轮整轮不丢。规则条文和边界情形在 §4。
5. 主要的代价有三项。降级期间会话部分基本不命中前缀缓存，探针里可复用前缀从 73% 至 82% 降到 4% 至 5%。被切开的那一轮不丢以后，丢到底时结果高于目标的情形比第 2 版多：按生产的目标范围量 6,000 段对话，高于目标的从 107 段升到 315 段（system 提示 50 token）、从 791 段升到 1,188 段（10,000 token），结果仍不大于输入。`pinned` 前缀停在一轮中间的调用方式不在保护之列，这一轮余下的结果最先丢并被补上占位；entry 层不是这种调用方式。
6. 改动是 `omicsclaw/context/transcript.py` 的一个函数加三个私有辅助函数，几处 docstring，四份功能文档，`test_transcript.py` 里 27 条单元测试（其中 2 条替换现有测试，3 条各带两种角色写法），另外两个测试文件里各一组参数化用例，合起来 42 条用例，再加 1 条脚本化 eval。按 `SPEC.md` 属于第 3 档。合并之前要做一对真实模型会话的对比。
7. 规则落地后，SOFT、FULL 降级截断导致的闸门误触发不再发生。EMERGENCY 的任务锚点这次不改。
8. "发给模型的第一条消息必须是 user"不在本计划内，另行立项在 provider 适配器层处理；规则落地后还会出现这种视图的情形写在 R1。
9. 第 3 版看到一处第 2 版没有写的后果：较早的一次交换没有最终答复就结束，截断又正好停在它最后一轮的调用已经丢掉、结果还没丢完的地方时，这次交换的请求会独自留在视图里。owner 2026-10-09 裁定把改法并进定稿：请求在它最后一条 assistant 回复被丢的那一步一起丢（§4.1 第 4 条、E13、R11）。产品路径上这种交换只有跑满 `max_turns` 的那一种。

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

一条用户请求后跟 12 轮工具往返，26 条，1411 token。输出在 `r3_out_26_current.log`，是第 1 版的探针原样重跑，和当时的 `out_01_current.log` 逐行相同。

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

用脚本化 eval 的 Runner（`SessionRegistry` 到 `TurnRunner` 到引擎，system 提示和工具表是真实的，模型是 `ScriptedProvider`）跑两次交换：第一次读三遍小文件后答复，第二次读三遍小文件再同时读两张表。窗口用 `Headroom` 定在第 9 次模型调用前到 FULL 档，摘要器返回空串（摘要超时的返回值）。用例在 `r3_eval_section_1_4.py`，输出在 `r3_out_28_eval_section_1_4.log`。

第 9 次模型调用收到 6 条消息，角色依次是 system、assistant、tool、assistant、tool、tool。两次交换的请求都不在里面。截断前的对话是 17,174 token，其中第一次调用时的 system 提示加第一条请求就有 11,808，两张表在 tail 里，head 再怎么丢也到不了目标，循环走到最后一行，把整个 head 丢掉。

### 1.5 head 和 tail 的边界切开一轮时

边界落在一轮工具调用中间时（调用在 head 末尾，结果在 tail 开头；或者一条并行调用的结果一半在 head、一半在 tail），今天最先丢的就是这一轮留在 head 里的部分。调用一丢，tail 里它的结果成了孤儿，被修复删掉。并行调用越宽越容易出现，宽度不小于 `min_tail` 时整个 tail 都是它的结果。S13 是一个例子：最后一轮是 6 个并行调用，今天的降级视图是 `S U1 ... A1 U2 a2.1 t2.1 a2.2 t2.2*`，tail 的 6 条结果一条不剩，视图只有目标的 59%。

`r4_probe_tail.py` 的第一组按生产的目标范围量了这件事的频率：格式完好的随机对话 6,000 段（调用宽度 1 到 6，目标取对话的 0.74 到 0.875，对应 SOFT、FULL 两档的回退目标），边界切开一轮的有 3,793 段，今天的截断在 3,474 段里删掉了 tail 的消息，其中 684 段把 tail 删光（`r4_out_05_tail.log`）。这个生成器里并行调用比真实会话多，数字偏高；它说明的是边界切开一轮时今天几乎必然丢 tail。

### 1.6 文字和代码的出入

- `transcript.py:201` 的 docstring 写 "Peel messages off the front"，`:203` 写 "The oldest compactible message goes first"，`:229` 写 "peeling the front off can orphan a tool result"。模块 docstring `:18` 也写 "peels them off the front"。代码丢的是 head 的后端。
- `tests/context/test_summary_gates.py:188` 的 docstring 写 "It peels the oldest compactible messages one at a time"。
- `docs/core-features/context-engineering.md:284` 和 `docs/core-features/progressive-compactor.md:342` 都写"从 head 最旧处逐条剥离"。
- `docs/plans/0030-context-assembly-layer.md` 写的是"逐条剥头"（`:349`、`:1273`、`:2517` 等），没有说哪一端。`:1273` 说它取代的旧层是"块感知的最新后缀裁剪"，也就是保留最新的。
- `tests/context/test_transcript.py:556` 的 `test_the_oldest_compactible_message_goes_first_and_one_at_a_time`：对话 849 token，预算 900，`fit_to_budget` 返回的就是输入本身（`r3_out_27_existing_test.log`）。三条断言对任何实现都成立。把预算调到 500 至 800 让它真的要截，这三条断言在两个方向上仍然都成立。
- `tests/context/test_transcript.py:591` 的 `test_peeling_the_head_strands_a_tool_call_and_the_repair_papers_over_it` 是唯一一条结果取决于方向的现有测试：它断言留下一条调用和一条占位，这只在从 head 后端丢时成立。

## 2. 参考实现

读到的：

- `internal/memory/compaction.go:95-132` 的 `TokenBudgetCompactor.Compact` 是 `rest[:headEnd]`，`headEnd` 递减，和这里的代码同向。把它的源码复制到暂存目录跑同样两段对话（`out_02_reference_go.log`，第 1 版跑的，第 3 版没有重跑），同样是留 head 最旧的一段、丢靠近尾部的几条，切点落在调用和结果之间时补一条占位。
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

G 和 G+ 是用这份计划自己的实现跑的（`r3_r2_rules_copy.g_as_ruled`、`r4_rules.gplus_r2`），和审核方的对照实现的比对见 §4.4。表里的 G+ 是第 2 版的规则，比裁定的字面多两个条件（system 消息、摘要要看角色），这两个条件在下面的场景里不起作用。

定稿规则是 G+ 加 N1 裁定的 2f，再加 R11 裁定的第 4 条（§4.1）。带 2f 的规则和定稿规则各作为一行加进了同一个探针（`r4_probe_scenarios.py`，`r4_out_02_scenarios.log`）：§3.2 的 28 次和 §3.3 的 24 次里，这两行发出的视图都和 G+ 那一行逐条相同，所以这两节的表不用改。这些场景里目标都够得着，没有丢到被边界切开的那一轮；较早的交换也都有最终答复。

### 3.2 十四个场景

每个方案都替换进真实的 `compact()` 跑（探针进程里换掉 `omicsclaw.context.compaction.fit_to_budget` 这个模块属性），offload、定档、降级目标和记录都是生产代码。摘要器抛错，SOFT 和 FULL 各一次，每个方案 28 次降级压缩。脚本是 `r4_probe_scenarios.py`，输出在 `r4_out_02_scenarios.log`。

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

`min_tail` 是 6，所以每次新交换的前 0 到 2 轮，当前请求都在 tail 里，tail 的开头还是上一次交换最后的几条回复。第 1 版的 10 个场景里只有 S6 是这个形状，而且 S6 的 head 里有两次较早的交换，没有暴露问题。审核方指出了它（`RV/rv_out9_request_in_tail.log`），这里用自己的探针重做：较早的一次交换 6、12、30 轮，本次 0 到 3 轮，SOFT 和 FULL，共 24 次（`r4_out_02_scenarios.log` 的第二部分）。

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

工具调用配对修复。从 head 后端丢（B、E）时，切点前面可能留下一条结果被丢的调用，修复给它补占位，28 次里 B 有 24 次、E 有 26 次出现占位。从最旧的开始丢时，切点后面可能留下调用已被丢的结果，修复把它删掉，不补任何东西。输入里本来就缺结果的调用，在任何方案下都仍由修复补占位。边界切开一轮时的情况见 §1.5 和 §4.1 的 2f。

提示前缀缓存。降级结果不写回，每次调用重新截。`r4_probe_sequence.py` 用真实的 `ProgressiveCompactor` 按引擎的方式逐次调用，摘要器一直抛错，统计降级调用发送的 token 里，与上一次请求逐条相同的前缀占多少（`r4_out_06_sequence.log`）：

| 方案 | 单次长交换，9 次降级调用 | 两次较早的交换后一次长交换，12 次 | 一次 16 轮的交换后新交换从头开始，11 次 |
|---|---|---|---|
| B | 77% | 82% | 73% |
| E | 77% | 81% | 72% |
| D | 5% | 64% | 47% |
| C+ | 5% | 3% | 3% |
| G、G+ | 5% | 4% | 4% |

带 2f 的规则和定稿规则在这三个序列的每一次调用上发出的视图都和 G+ 相同。这三个序列每轮只有一个调用，tail 总是以调用开头，较早的交换都有最终答复，2f 和第 4 条里 tool 消息那一句都不起作用。

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

## 4. 定稿的规则

G+，加 N1 裁定的 2f，加 R11 裁定的第 4 条。

### 4.1 条文

用到的词：请求是 user 角色的消息，压缩摘要也是 user 角色，规则里和请求同样对待；回复是 assistant 和 tool 角色的消息；一次交换是一条请求和它后面直到下一条请求之前的回复。出现在对话中间的 system 消息按回复处理，本仓库的流程不会产生它。一轮是一条带 `tool_calls` 的 assistant 消息和紧跟在它后面的那一串 tool 消息，和 `repair_tool_pairs` 一样按相邻关系配对。

1. 原样返回输入，不重建也不修复：对话为空；已经不超过目标；没有 head；head 里每一条都不能丢。
2. 不丢的消息：
   - a. 前 `pinned` 条和最后 `min_tail` 条。截断不碰它们。第 6 条的修复只会删掉其中在输入里就没有配对的 tool 消息，也就是 `repair_tool_pairs` 直接作用在输入上也会删的那些。
   - b. head 最前面连续的 system 消息，有几条留几条。`pinned` 盖住了 system 消息时这一条不起作用，它管的是 `pinned=0` 这类调用。
   - c. head 里的压缩摘要：user 角色，并且 `is_summary_message` 为真。
   - d. 当前请求，也就是对话里最后一条请求，当它在 head 里时。
   - e. tail 的第一条是回复时，这条回复所属交换的请求，也就是 head 里最后一条请求。
   - f. 被 head 和 tail 的边界切开的那一轮留在 head 里的部分：tail 以工具结果开头时，head 末尾发出这次调用的 assistant 消息和它在 head 里的结果不丢。判定分三步，都成立才留：
     1. 从 head 的最后一条往前，跳过连续的 tool 消息，停在第一条不是 tool 的消息上。head 全是 tool 消息时停不下来，这一项不起作用。
     2. 停下的那一条是带 `tool_calls` 的 assistant 消息。
     3. tail 开头连续的 tool 消息里，至少有一条的 `tool_call_id` 是这条 assistant 消息发出的调用之一。

     成立时，这条 assistant 消息和它之后 head 里的所有消息都不丢。

   d 和 e 合起来是一句话：head 里最后一条请求不丢，除非 tail 的第一条本身就是请求。f 成立时 tail 的第一条是工具结果，所以那一轮所属交换的请求已经由这一句留着，第 4 条不用为 f 另作处理。

   f 只管 head 和 tail 的边界。`pinned` 前缀停在一轮中间时，也就是它以一条调用结尾，或者以这条调用的一部分结果结尾，而这一轮余下的结果在 head 开头，这些结果不在不丢之列（§4.2 的 E12）。owner 2026-10-09 裁定这一侧不加，只写明。以后若要加，`compaction.py` 的 `_answers_owed_by(pinned, head)` 是现成的起点，用之前有两件事要处理。它和 2f 的读法不一致：它从 head 开头数，遇到第一条对不上的结果就停，2f 的第 3 步看的是整串。不规整的输入上两者分得开，复核方量到 11,460 个截断里有 46 个造了占位而它数出来是 0（`RV3/rv3_out7_properties.log`），这份计划在同一族对话上量到 19,116 个里 83 个（§4.4）。另外 `compaction.py` 已经从 `transcript.py` 引入名字，反过来引入会成环，要先把这个函数挪到 `transcript.py`。
3. head 里其余的消息从最旧的开始丢，一次一条，每丢一条量一次，不超过目标就停。
4. 请求跟着 assistant 回复走：一次交换只要还有 assistant 回复留着，它的请求就留着。轮到一条请求时，它后面还有自己的 assistant 回复没丢，就先留着它，接着丢它的回复；它最后一条 assistant 回复被丢的那一步，它一起被丢。tool 消息不算留着的回复：调用被丢之后，它那些还没轮到的结果留不住请求，第 6 条的修复随后会把它们删掉（§4.2 的 E13）。后面没有 assistant 回复的请求轮到时直接丢，它后面要么紧跟着另一条请求，要么只有 tool 消息。对话中间的 system 消息在这一条里和 assistant 回复同样对待。
5. 能丢的都丢完还超过目标，就返回 pinned、第 2 条列的那些消息和 tail。这时结果高于目标。
6. 最后过一遍 `repair_tool_pairs`。量 token 在修复之前：修复补的占位不计入，修复要删的孤儿结果计入。

很多次交换时的先后：先丢最旧那次交换的回复，从它最旧的一条开始；这次交换最后一条 assistant 回复走的那一步它的请求一起走；然后是下一次交换，依此类推；最后是本次交换自己的回复，从最旧的开始，本次的请求不丢。`r4_out_04_edges.log` 的 E11 把目标一步步调低，看到的顺序是：

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

这段对话的 tail 以调用开头，每次交换都有最终答复，2f 和第 4 条里 tool 消息那一句都不起作用，每一个目标下的视图都和第 2 版相同。

### 4.2 边界情形

每条都在 `r4_probe_edges.py` 里用一段对话跑过，输出在 `r4_out_04_edges.log`。"今天"是今天的函数在同一段对话上的结果，"不加 2f"是第 2 版的规则。和第 3 版的规则相比，结果变了的只有 E13，探针逐条比过。和第 2 版的 G+ 相比，变了的是 E6c、E6e 和 E13。E6f、E6h、E6i、E12a、E12b、E13 是第 3 版加的。

| 情形 | 规则怎么走 | 例子 |
|---|---|---|
| 没有 user 消息（E1） | 第 2 条只有 a 起作用，回复从最旧的开始逐条丢。视图以回复开头，输入本身就是这样 | 12 轮，目标是 70%：`S a1.5 t1.5 ... a1.12 t1.12`。今天：`S a1.1 ... a1.6 t1.6* a1.10 ... t1.12` |
| head 只有一条摘要（E2a） | head 里没有可丢的，原样返回，哪怕远高于目标 | `S SUM a1.1 t1.1 a1.2 t1.2 a1.3 t1.3` 原样返回。今天把摘要丢了：`S a1.1 ... t1.3` |
| 摘要之后没有新请求（E2b） | 摘要是最后一条请求，不丢；它后面的回复从最旧的丢 | `S SUM a1.5 t1.5 ... a1.9 t1.9` |
| 当前请求本身超过目标（E3） | 走到第 5 条，结果是 pinned、请求和 tail | 请求 1,500 token，目标 800：`S U2 a2.4 t2.4 a2.5 t2.5 a2.6 t2.6`，1882。今天：`S U1 a1.1 t1.1 a1.2 t1.2 A1 a2.4 ... t2.6`，683，请求没了 |
| 当前请求在 tail 里，tail 以回复开头，目标够得着（E4a） | 较早的交换照第 3、4 条丢，head 里最后一条请求按 2e 不丢 | `S U1 a1.5 t1.5 a1.6 t1.6 A1 U2 a2.1 ... A2 U3 a3.1 t3.1 a3.2 t3.2` |
| 同上，目标够不着（E4b） | head 丢到只剩 2e 那一条 | `S U2 A2 U3 a3.1 t3.1 a3.2 t3.2`。G 没有 2e，结果和今天一样是 `S A2 U3 ...`，以回复开头 |
| tail 的第一条就是当前请求（E4c，T12） | 2d、2e 都不起作用，较早交换的请求按第 4 条和它最后一条 assistant 回复一起走 | 目标够不着时结果是 pinned 加 tail |
| `pinned=0`，首条是 system（E5） | 2b，system 不丢 | `S U1 a1.4 t1.4 A1 U2 a2.1 ... t2.5` |
| 切点落在 head 里一条并行调用和它的结果之间（E6a） | 调用被丢，留下的结果是孤儿，修复删掉，不补占位 | 今天在这里补了一条占位 `t2.1c*` |
| 边界切开一轮，目标够得着（E6b、E6d） | 还没丢到那一轮就停了，2f 不改变结果 | E6b：`S U2 a2.1 t2.1 a2.2 t2.2 a2.3 t2.3a t2.3b t2.3c a2.4 ... t2.5`，1452 对目标 1491。今天丢的正是这一轮：`... a2.2 t2.2* a2.4 ...`，844，tail 开头的 `t2.3b t2.3c` 被删 |
| 边界切开一轮，目标够不着（E6c，T23） | head 丢到只剩当前请求和按 2f 留着的那一轮，结果高于目标 | `S U2 a2.3 t2.3a t2.3b t2.3c a2.4 t2.4 a2.5 t2.5`，1232 对目标 600。不加 2f：`S U2 a2.4 t2.4 a2.5 t2.5`，302，`t2.3b t2.3c` 被删。今天：`S a2.4 t2.4 a2.5 t2.5`，271 |
| 6 个并行调用的结果占满 tail，目标够不着（E6e，T22） | 同上，那一轮在 head 里只有调用这一条 | `S U2 a2.5 t2.5a ... t2.5f`，589 对目标 500。不加 2f：`S U2`，82，模型只看得到请求。今天：`S` |
| head 只有请求和那条调用（E6f） | 2d、2f 把 head 占满，head 里每一条都不能丢，原样返回 | `S U1 a1.1 t1.1a ... t1.1f`，589 对目标 100 |
| 没有 user 消息，最后一轮的结果占满 tail（E6h） | 2f 照常起作用，没有请求可留 | `S a1.5 t1.5a ... t1.5f`，558 对目标 100，以回复开头。不加 2f 和今天都是 `S` |
| 输入不规整：tail 的第一条是没有调用的结果，后两条才应答 head 末尾的调用（E6i，T24） | 2f 第 3 步看的是 tail 开头那一串，那一轮留着；没有调用的那条由修复删掉 | `S U2 a2.3 t2.3a t2.3b a2.4 t2.4 A2`，414 对目标 300。只看 tail 第一条的写法得到 `S U2 a2.4 t2.4 A2`，两条配得上对的结果被删 |
| 两条请求连着（E7a，T20） | 前一条后面没有回复，轮到时直接丢；后一条是当前请求 | `S U2b a2.1 ... t2.5` |
| 一问一答的闲聊之后一次带工具的交换（E7b） | 每次较早的交换是一条请求一条答复，成对走 | `S U4 A4 U5 a5.1 ... t5.4` |
| 一条 assistant 消息恰好以压缩标记开头（E8） | 不是 user 角色，不算摘要，和别的回复一样丢 | 它夹在第 3、4 轮之间：`S U1 a1.5 t1.5 ... a1.9 t1.9`。今天也不特殊对待它 |
| 较早的交换 30 轮，只超目标 5%（E9） | 只丢最旧的两轮 | 3747 对目标 3768。审核方在同样的形状上量到第 1 版的 D 只剩 522（`RV/rv_out8_edges.log`） |
| 较早交换的请求很大（E10） | 请求按第 4 条留到它最后一条 assistant 回复，占着位置 | 请求 600 token：`S U1 A1 U2 a2.1 ... t2.4`，6 轮都丢了，1174 对目标 1283 |
| pinned 前缀以一条调用结尾，结果在 head 开头（E12） | 2f 不管这一侧。这些结果是 head 里最旧的，第一步就丢，修复给 pinned 里的调用补占位 | `pinned=3`：`S U1 a1.1 t1.1a* t1.1b* a1.3 ...`，893 对目标 906。entry 层的 `pinned` 是 1，盖的是 system 消息，走不到这里 |
| 同上，结果很小，目标只比输入少 1（E12a） | 占位比它顶替的结果大，结果比输入还大 | 输入 985，目标 984，结果 994 |
| pinned 前缀以一条 8 路并行调用结尾，head 是它的前两条结果，tail 是后六条（E12b） | head 全是 tool 消息，2f 第 1 步停不下来。head 的两条被丢并补占位，tail 的六条留着 | `S U1 a1.1 t1.1a* t1.1b* t1.1c ... t1.1h`，627 对目标 100。今天的结果相同 |
| 较早的交换以一轮工具调用结束，没有最终答复，截断停在这一轮的调用已丢、结果还没丢完的地方（E13，T25 至 T27） | 那条调用是这次交换最后一条 assistant 回复，它被丢的那一步请求一起丢。剩下的结果在候选里还占着 token，修复再把它们删掉 | `S U2 a2.1 ... t2.5`，632 对目标 823。第 3 版的规则在这里留下一条没有任何回复的旧请求：`S U1 U2 ...`，663。每轮只有一个调用时结果相同。这次交换后面有一条答复时是 `S U1 A1 U2 ...`，两版一样。见 R11 |
| 角色是普通字符串（T15、T22、T23、T25） | 比较一律用 `==`，理由同 `repair_tool_pairs` 的 docstring | |

### 4.3 原型

暂存副本里的原型由 `r4_apply_prototype.py` 写入，`r4_check_plan_block.py` 核对过下面这段和写进副本的文本逐字相同。实施时可以另写，以 §5.2 的测试为准：

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

    The system messages the head opens on, every compaction summary, and
    the last user message in the head unless the tail itself opens on a
    user message. That last one is the request in flight when the tail
    holds no request, and otherwise the request whose replies the tail
    opens on. The tool round the tail finishes is held too, so the
    results the tail opens on keep the call they answer.
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
    held.update(_round_the_tail_finishes(head, tail))
    return frozenset(held)


def _round_the_tail_finishes(
    head: tuple[Message, ...], tail: tuple[Message, ...]
) -> range:
    """Positions in *head* of a tool round whose results run on into *tail*.

    The call is the assistant message in front of the tool results the
    head ends on, and the round counts when one of the tool results the
    tail opens on answers it. Pairing is by adjacency, the way
    :func:`repair_tool_pairs` reads it. A call that sits in the pinned
    prefix is not looked for.
    """
    start = len(head)
    while start and head[start - 1].role == Role.TOOL:
        start -= 1
    if start == 0:
        return range(0)
    caller = head[start - 1]
    if caller.role != Role.ASSISTANT or not caller.tool_calls:
        return range(0)
    asked = {call.id for call in caller.tool_calls}
    for message in tail:
        if message.role != Role.TOOL:
            break
        if message.tool_call_id in asked:
            return range(start - 1, len(head))
    return range(0)


def _request_of_oldest_reply(
    head: tuple[Message, ...], held: frozenset[int], cut: int
) -> frozenset[int]:
    """The request that stays because a reply to it does.

    Everything from *cut* on survives. Tool results are passed over:
    the ones standing at the cut have lost their call to it and the
    repair is about to delete them, so they keep no request. When the
    oldest survivor that is neither held nor a tool result is a reply,
    the nearest user message before it is its request, and the two stay
    together.
    """
    for index in range(cut, len(head)):
        if index in held or head[index].role == Role.TOOL:
            continue
        if head[index].role == Role.USER:
            return frozenset()
        for owner in range(index - 1, -1, -1):
            if head[owner].role == Role.USER:
                return frozenset({owner})
        return frozenset()
    return frozenset()
```

和第 3 版的原型相比，只有 `_request_of_oldest_reply` 变了：跳过的条件多了 `head[index].role == Role.TOOL`，docstring 跟着改。第 3 版量这种改法时用的是三行的写法，设一个标记，只跳过紧挨切点的那几条工具结果（`r3_rules.final_orphans_do_not_count`）。复核方指出它可以写成这一个条件，三行写法里第二处 `orphaned = False` 是死代码。核实下来两点都成立：那一行执行之后循环必然在同一轮返回；两种写法在六个生成器共 155,405 段有 head 的对话上结果相同（§4.4）。定稿用一个条件的写法。两种写法结果相同的道理是：三行写法不跳过的 tool 消息只有一种，排在一条留住的消息后面的那种；它前面最近的请求要么就是那条留住的消息，要么不存在，把它当回复也不会多留下什么。

和第 2 版的原型相比，除了上面这一处，`_held` 的末尾多一行，另加一个辅助函数 `_round_the_tail_finishes`；`_held` 的 docstring 改了第一句（开头的 system 消息写成复数，对应 2b 的"有几条留几条"），末尾加了一句。第 2 版 `r2_rules.py` 里的 `gplus_straddle` 没有沿用：它建在"先丢前 n 条再放回请求"的那种写法上，那种写法在没有可丢的消息时把输入修复一遍再返回，和第 1 条不符，审核方第 2 轮在 `RV2/rv2_out10_plan_straddle_variant.log` 里量到了这个差别。

原型里有三处去掉以后结果不变，四个生成器共 67,846 段有 head 的对话里差异都是 0（`r4_probe_equivalent.py`，`r4_out_20_equivalent.log`）：

- `_request_of_oldest_reply` 里的 `index in held or`，被改的那一行走到过 77,347 次。它是第 2 版已批准的原型里的写法。
- `_round_the_tail_finishes` 里 `if start == 0: return range(0)` 这两行，走到过 336 次。去掉以后 `head[start - 1]` 取到的是 head 的最后一条，它是 tool 消息，下一行照样返回。
- 同一个函数里的 `or not caller.tool_calls`，走到过 4,028 次。没有调用的消息 `asked` 是空集，后面什么也对不上。

三处都原样留着，写出来读得明白，实施时去留都可以。变异检查里对应的三项不会有测试转红，见 §5.2。

裁定里 G+ 那一条的字面是"head 丢光而 tail 以回复开头时，保留 tail 所在交换的请求"。原型把它写成"tail 以回复开头时 head 里最后一条请求从一开始就不丢"。两种写法的结果相同：head 里最后一条请求后面只有它自己的回复，按第 4 条它本来就要到 head 丢光那一步才会被丢。第 2 版的比对证实了这一点（`r2_out_01_fuzz.log`）。

### 4.4 随机比对和性质表

`r4_probe_fuzz.py` 在六个生成器上跑随机对话，输出在 `r4_out_01_fuzz_own.log` 和 `r4_out_01_fuzz_rv3.log`。前三个是第 3 版用的那三个，各 40,000 段，种子和取数的顺序都没变：这份计划自己写的一个，第 1 轮审核方 `rv_probe7_fuzz.py` 的一个，以及在第一个的基础上让 `pinned` 前缀可以停在任何位置的一个。后三个是第 3 轮复核方的三族对话，各 20,000 段，生成器从 `RV3/rv3_common.py` 复制：A 族像 entry 层的会话，规整，`pinned=1`、`min_tail=6`；B 族规整，参数放宽，`pinned` 取 0 到 5；C 族是在 B 族上故意弄坏的，插进没有调用的结果、重复的结果、中间的 system 消息，调换相邻的两条，等等。有 head 的对话依次是 33,552、30,770、32,673、19,986、19,108、19,316 段，合计 155,405 段。下面每组六个数都按这个顺序。

- 定稿规则的三种写法：打过补丁的生产函数；`r4_rules.final`；只照 §4.1 的条文另写的 `r4_rules.from_text`，它先列出每一步丢哪几条，再逐步执行。六个生成器上差异都是 0。
- 第 4 条里 tool 消息那一句的两种写法：原型里的一个条件，和第 3 版量过的三行写法。六个生成器上差异都是 0。
- 定稿规则对第 3 版的规则：1,777、292、1,261、345、480、447 段不同。每一段的差别都只是少了请求，而且少掉的每一条在第 3 版规则的视图里都没有任何回复留着；没有别的差别。第 3 版的规则高于目标而定稿规则不高于的有 9、3、15、0、0、0 段，反过来的没有。
- 2f 的影响是第 3 版量的，规则的这一部分没有变：前三个生成器上，第 3 版的规则和不加 2f 的规则在 4,223、3,859、5,106 段上不同，每一段都是后者删掉了一条输入自己修复后还留着的 tail 消息（`r3_out_01_fuzz.log`）。

复核方第 3 轮只照第 3 版 §4.1 的文字另写了一份实现，在它的三族对话上各 20,000 段、角色用枚举和普通字符串各跑一遍，和第 3 版的原型没有差异（`RV3/rv3_out1_fuzz.log`；它的差异计数只在非零时打印，日志里没有这一项）。第 4 版改了第 4 条，等复核方照新条文再写一遍。第 2 版对第 1 轮审核方实现的比对没有重做，结论不变：定稿规则和它有三处有意的不同，system 消息不丢、摘要看角色、没有可丢的消息时原样返回而不是修复后返回（`r2_out_01_fuzz.log`）。

同一个探针在定稿规则的每个截断结果上数了规则声称的性质。截断的结果依次是 32,267、29,631、31,670、19,937、18,839、19,116 个。"输入规整"指输入自己过一遍 `repair_tool_pairs` 不变；这样的截断结果依次有 11,423、16,039、11,042、19,937、18,839、2,112 个。

| 性质 | 违反次数 |
|---|---|
| 当前请求在结果里 | 六个都是 0 |
| user 角色的摘要在结果里 | 六个都是 0 |
| head 开头连续的 system 消息都在结果里 | 六个都是 0 |
| 留下的回复，它的请求（输入里它前面最近的 user 消息）也留着 | 六个都是 0 |
| `pinned` 前缀没有停在一轮中间时，结果里的占位都对应输入里本来就没应答的调用 | 六个都是 0 |
| 输入规整时，留下的消息保持原来的先后 | 六个都是 0 |
| 留下的消息的先后，和输入自己修复后的先后一致 | 六个都是 0 |
| `pinned` 前缀的末尾不是一轮的调用或它的结果时，输入自己修复会删掉的工具结果不出现在结果里 | 六个都是 0 |
| 输入自己修复后还留着的 tail 消息都在结果里 | 六个都是 0 |
| 输入自己修复后还留着的 pinned 消息都在结果里 | 六个都是 0 |
| 高于目标时，要么已经丢到第 5 条的底线，要么只多出占位 | 六个都是 0 |
| 结果比输入大时，结果里有占位 | 六个都是 0 |
| 第一轮不是 user 时，那条回复要么在 pinned 前缀里，要么输入里它前面确实没有请求 | 六个都是 0 |
| 输入规整时，留在结果里、不在 pinned 前缀里的较早请求，至少有一条它的回复也在结果里。压缩摘要除外，它按 2c 无条件留着 | 六个都是 0 |

第 5 行的限定按复核意见改了说法。"停在一轮中间"按位置判定：`pinned` 前缀里最后一条不是 tool 的消息是带调用的 assistant 消息，而 head 以 tool 消息开头；前缀停在这一轮的部分结果上也算。第 3 版用的是 `_answers_owed_by(pinned, head) > 0`，那是窄读法，在不规整的输入上会漏。按位置判定，第三个生成器、B 族、C 族里停在一轮中间的截断结果是 11,876、5,993、5,053 个，其中带着截断造成的占位的是 11,866、5,993、4,973 个；这些带占位的结果里 `_answers_owed_by` 数出来是 0 的有 0、0、83 个。另外三个生成器的 `pinned` 只盖 system 消息，没有这类截断。

先后那三行是照复核方第 3 轮的日志补的，它在 C 族上数到过先后变了的结果（`RV3/rv3_out7_properties.log`）。这里量到 C 族里有 874 个结果的先后和输入不同：修复把一轮里的工具结果排成调用的顺序，输入自己修复也是这样排，所以只看输入自己修复后还留着的消息，先后没有一处不一致。C 族里另有 44 个结果带着输入自己修复会删掉的工具结果：截断丢掉了夹在调用和这条结果之间的那条消息，两者重新相邻，修复就把它留下了。这 44 个的那条调用都在 `pinned` 前缀的末尾，属于 `pinned` 一侧（`r4_probe_order.py`，`r4_out_33_order.log`）。

最后一行是 R11 裁定之后加的。不满足它的前提时，定稿规则下仍有请求独自留着，分三类：

| | 六个生成器 |
|---|---|
| 有较早请求独自留着的结果，第 3 版的规则 | 1,839、310、2,275、345、2,048、2,192 |
| 同上，定稿规则 | 67、18、1,050、0、1,616、1,792 |
| 其中：请求在 `pinned` 前缀里 | 0、0、999、0、1,616、1,720 |
| 其中：输入自己修复后这条请求本来就没有回复 | 58、11、50、0、0、71 |
| 其中：tail 以一条输入里就没有配对的工具结果开头，2e 留下了 head 里最后一条请求 | 9、7、4、0、0、9 |
| 其中：别的原因 | 六个都是 0 |

后两类只在不规整的输入上出现。一个结果里可以有不止一条这样的请求，所以第三个生成器和 C 族的三类加起来比上一行多 3 和 8。

其余的计数：

- 高于目标的有 5,366、6,325、8,427、436、3,222、2,772 个。其中丢到底线的 4,825、6,006、7,054、436、2,673、2,458 个，只多出输入里未应答调用的占位的 541、319、332、0、0、47 个，只多出占位、占位里有截断造成的 0、0、1,041、0、549、267 个。
- 结果比输入大的有 27、14、162、0、3、2 个，都带占位。
- 第一轮不是 user 的有 701、1,113、1,243、0、0、265 个，其中那条回复在 pinned 前缀里的是 0、0、959、0、0、245 个。
- tail 里有消息不在结果里的有 939、887、951、0、0、2,948 个，都是输入自己修复也会删的孤儿结果。

前三个生成器上和第 3 版不同的数：

| | 第 3 版 | 第 4 版 | 原因 |
|---|---|---|---|
| 高于目标 | 5,375、6,328、8,442 | 5,366、6,325、8,427 | 9、3、15 个结果在第 3 版的规则下只因为那条独自留着的请求才高于目标 |
| 其中丢到底线 | 4,825、6,006、7,052 | 4,825、6,006、7,054 | 第三个生成器里有 2 个结果去掉那条独自留着的请求以后正好是底线，仍因为截断造成的占位高于目标，从最后一类换到这一类 |
| 其中只多出输入里未应答调用的占位 | 550、322、336 | 541、319、332 | 同第一行 |
| 其中只多出占位、有截断造成的 | 0、0、1,054 | 0、0、1,041 | 少的 13 个里 11 个同第一行，2 个换到了"丢到底线"那一类 |
| 第 5 行那一类的截断结果 | 0、0、11,866 | 0、0、11,876 | 判定从 `_answers_owed_by` 换成按位置。多出的 10 个按位置算停在一轮中间，`_answers_owed_by` 数出来是 0，它们都没有截断造成的占位 |

第三个生成器的归类变化是 `r4_probe_reclassified.py` 重放同一串对话、把每个结果在两版规则下各归一次类比出来的：17 个结果换了类，4 个和 11 个不再高于目标，2 个换到底线那一类，每一个的定稿视图都只比第 3 版的视图少了请求（`r4_out_34_reclassified.log`）。

截断的结果数、第一轮不是 user 的次数、结果比输入大的次数和 tail 里孤儿结果的次数都没有变。第 3 版相对第 2 版的三处差别仍见 `r3_out_23_r2_to_r3.log`。

### 4.5 理由

- 请求一定在视图里，摘要也在。
- 较早的请求不会独自留在视图里：输入规整、请求不在 `pinned` 前缀里时，留在结果里的较早请求至少带着它的一条回复（§4.4 的性质表）。压缩摘要除外，它按 2c 无条件留着。复核方的真实模型样本里，模型看到一条没人回应过的旧请求就回头去做它，见 R11。
- 留下的是最近的工作。SOFT 的正常路径摘要较旧的一半、原样留较新的一半，EMERGENCY 从新到旧装，今天只有降级截断留旧丢新。计划 0030 §5.2 说它取代的旧层是"最新后缀裁剪"，docstring 写"最旧的先走"，两处的本意都是留新的。
- 截断点不再造占位，模型不会看到自己发过、结果却写着"不可用"的调用。例外是 E12 那种 `pinned` 前缀停在一轮中间的调用方式。
- 最新的一轮不再因为截断丢掉。§1.5 的探针里，今天在 6,000 段中的 3,474 段删掉了 tail 的消息，不加 2f 的 G+ 是 236 段，定稿规则是 0 段（`r4_out_05_tail.log`）。`min_tail` 的含义是最后几条消息原样留着，`split_head_tail` 的 docstring 写的是 "kept whatever happens"，加了 2f 降级截断才做到这一点。"最新一轮不该最先丢"是审核方第 1 轮补的理由（`RV/rv_out10_parallel_tail.log`）。
- 较早的交换可以只留一部分，留下的部分带着它的请求。28 次场景里平均用到目标的 97%，没有一次低于 60%；D 是 72%，有 8 次低于 60%。
- 视图以 user 消息开头，除非输入里第一条留下的回复前面本来就没有请求。
- 改动小：一个函数的循环，三个辅助函数。暂存副本里跑 `tests/context tests/planning tests/engine tests/entry tests/evals`，基线 2842 passed，换上定稿原型后 2841 passed、1 failed，红的是 §1.6 最后那一条（`r3_out_29_baseline_layers.log`、`r4_out_17_prototype_layers.log`）。

### 4.6 代价

- 降级期间的前缀缓存。三个序列里可复用前缀从 77%、82%、73% 降到 5%、4%、4%。单次长交换里每次降级调用重新读的 token 从 371 升到 1,522（每次发送约 1,600）。影响限于摘要器持续失败、压力又在 SOFT 以上的那些调用，每次这样的调用本来也要先付一次失败的摘要请求（超时的话是 90 秒）。有一种缓解办法这份计划没有评估：让切点分步挪动而不是每次挪一条。审核方试过每 8 条挪一次（`RV/rv_out5_sequence.log`），单次长交换回到 54%，多交换序列里 G 是 48%，代价是每次少发约 7% 到 14% 的 token。这次不做。
- 单次长交换里最早的几轮先丢。今天看不到的是中间几轮，规则落地后看不到的是最早几轮。
- 较早交换的请求按第 4 条留到它最后一条 assistant 回复，请求很大时占位置（E10、S14：S14 里用到目标的 95%，较早的 6 轮丢了 4 轮）。
- 结果高于目标的情形比不加 2f 时多。被边界切开的那一轮不丢，丢到底时这一轮的 token 留在结果里。`r4_probe_tail.py` 按生产的目标范围量了四组各 6,000 段格式完好的对话（`pinned=1`，`min_tail=6`，目标取对话的 0.74 到 0.875，`r4_out_05_tail.log`）：

  | | 今天 | 不加 2f | 定稿规则 |
  |---|---|---|---|
  | 并行调用多，system 提示 50 token：tail 的消息被删 | 3,474 | 236 | 0 |
  | 其中 tail 被删光 | 684 | 10 | 0 |
  | 高于目标 | 245 | 107 | 315 |
  | 并行调用多，system 提示 10,000 token：tail 的消息被删 | 3,829 | 802 | 0 |
  | 其中 tail 被删光 | 682 | 86 | 0 |
  | 高于目标 | 878 | 791 | 1,188 |
  | 多数是单个调用，system 提示 50 token：tail 的消息被删 | 1,629 | 200 | 0 |
  | 高于目标 | 367 | 275 | 411 |
  | 多数是单个调用，system 提示 10,000 token：tail 的消息被删 | 1,656 | 643 | 0 |
  | 高于目标 | 1,801 | 1,840 | 2,000 |

  2f 的代价拿第 3 版的规则和不加 2f 的规则比：两者在 236、802、200、643 段对话上不同，每一段都是后者删了 tail 的消息；其中 208、397、136、160 段的结果从不超过目标变成高于目标。2f 多发的 token，中位数是 1,420、1,318、410、410，最大 4,438、4,560、2,226、2,726。

  R11 的裁定不增加这一项。定稿规则和第 3 版的规则在 230、293、61、61 段上不同，每一段都只是少了那条独自留着的旧请求，少发的 token 中位数是 32，最大 602。高于目标的段数四组都没有变，所以表里"定稿规则"一列和第 3 版相同。

  结果最大是目标的 1.35 倍，那是目标取到对话的 0.74 而整段对话原样返回的情形；四组里结果都没有大于输入的。能走到回退的对话压力低于 EMERGENCY，总量低于 `usable × 0.95`，所以结果仍低于 `usable`。前两组的并行调用比真实会话多；后两组 20 个调用里 18 个是单个调用，接近一些。system 提示大的时候更容易丢到底，脚本化 eval 的装配里 system 提示约 11,800 token。单个例子见 E6c：1232 对目标 600，不加 2f 是 302。
- tail 的消息不再因为截断被删，这一项代价第 2 版有，第 3 版起没有了。修复仍会删掉输入里本来就没有配对的工具结果，那和截断无关，§4.1 的 2a 写的是同一件事。
- 视图仍可能以回复开头，见 R1。
- `pinned` 前缀停在一轮中间的调用方，这一轮在 head 开头的结果最先丢并被补上占位；结果很小时占位比结果大，视图可以比输入还大（E12、E12a）。entry 层不是这种调用方式。
- 跑满轮数的那次交换，它的请求比第 3 版的规则早几次调用从视图里消失。这是 R11 裁定的反面：用户在一次交换跑满 `max_turns` 之后只说一句"继续"时，那条旧请求就是任务本身。第 3 版的规则在调用已丢、结果还没丢完的那几次调用里留着它，定稿规则在调用被丢的那一步就把它丢了；再往后两版规则下它都不在。`r4_probe_maxturns.py` 用复核方的会话生成器和种子重放了这种会话，摘要器一直失败，每组 200 段（`r4_out_31_maxturns.log`）：

  | 第一次交换占 `usable` 的比例，最后一轮的调用数 | 旧请求早消失的会话 | 早几次调用 |
  |---|---|---|
  | 30%，1 个 | 1 段 | 1 次 |
  | 30%，4 个 | 48 段 | 1 到 7 次：15、15、7、4、6 段分别早 1、2、3、4、5 次，1 段早 7 次 |
  | 10%，1 个 | 0 段 | |
  | 10%，4 个 | 0 段 | |

  后两组里第一次交换只占 `usable` 的 10%，而降级时要丢的量不少于 `usable` 的 10%（SOFT 从 0.70 以上丢到 0.60，FULL 从 0.80 以上丢到 0.70），第一次交换每次都整段丢掉：评估的 13,176 和 13,756 次调用里，旧请求在两版规则的视图里都不在。四组的数字都和复核方用它自己的实现量到的相同（`RV3/rv3_out3b_maxturns.log`）。旧请求早消失的那几次调用里，第 3 版的规则发出去的正是那条没有任何回复的旧请求；按 R11 里那一个真实模型样本，那种视图让模型丢下当前请求。旧请求带着它最后一轮时模型也会回头去做它，这一点两版规则相同，见 R11。

## 5. 改动范围

### 5.1 代码

| 文件 | 改动 |
|---|---|
| `omicsclaw/context/transcript.py` | `fit_to_budget` 的循环按 §4.3 改；新增私有函数 `_held`、`_round_the_tail_finishes` 和 `_request_of_oldest_reply`；从 `.summary` 引入 `is_summary_message`（`summary.py` 只依赖 `offload.py` 和 schema，没有环）。`fit_to_budget` 的 docstring 重写，要写的内容列在 §5.3。模块 docstring `:18` 的 "peels them off the front" 改掉。签名和 `__all__` 不变 |
| `omicsclaw/context/compaction.py` | 没有逻辑改动。`compact` 的 docstring 在讲回退的那一段加一句：回退不丢压缩摘要、开头的 system 消息、当前请求，以及 tail 接着应答的那一轮；较早的请求随它最后一条 assistant 回复一起丢 |
| `omicsclaw/planning/injector.py` | 只改 `_gate_fires` 的 docstring：`:187-190` 那一段现在把 SOFT、FULL 的回退和 EMERGENCY 并列，改成只剩 EMERGENCY |

`pinned=0` 而首条是 system 消息时，`compact()` 仍会在 `advisories` 里写"it was treated as compactible history"。这句话对摘要路径仍然成立（system 消息在 head 里，会被摘要掉），对降级截断不再成立。这次不改它的措辞，在 `fit_to_budget` 的 docstring 里写明降级截断留着它。

如果审核方不接受 `transcript.py` 依赖 `summary.py`，另一种写法是给 `fit_to_budget` 加一个只能按关键字传的 `keep` 谓词，由 `fall_back` 传 `is_summary_message`。这会改公开函数的签名，本计划不推荐。

### 5.2 测试

T1 至 T17 和 T19 至 T30 写在 `r4_test_proposed.py` 里跑过，共 42 条用例（含参数化）。T1 至 T17 是第 2 版的 27 条，T19 至 T24 是第 3 版的 8 条，测试本身都没有改，只改了 T24 上面的一行注释。T25 至 T30 是第 4 版加的 7 条，其中 T25 至 T29 取自复核方第 3 轮的候选（`RV3/rv3_tests/rv3_test_extra.py`），换成了这份计划的编号。在今天的代码上 40 红 2 绿，在定稿原型上 42 条全绿（`r4_out_08_proposed_today.log`、`r4_out_09_proposed_prototype.log`）。在第 3 版的原型上 4 红，红的是 T25 至 T27；在不加 2f 的第 2 版原型上 9 红，多出来的是 T22 至 T24（`r4_out_11_proposed_r3_rule.log`、`r4_out_11b_proposed_r2_rule.log`）。第 2 版在第 1 版的 D 上量到 5 红（`r2_out_11_proposed_first_version_rule.log`），这一项没有重做。

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
| T19 | `test_every_system_message_that_opens_the_head_is_kept` | 2b。`pinned=0`，开头两条 system 消息：两条都留着，后面是请求和最新的一段 | 红 |
| T20 | `test_a_request_nothing_answers_goes_when_its_turn_comes` | 第 4 条里没有 assistant 回复的请求那一句。一条请求后面紧跟着重发的请求：目标只比输入少 1 时，丢的就是前一条；丢到底时它也不在 | 红 |
| T21 | `test_every_earlier_request_goes_with_its_last_reply` | 第 4 条，对不是最旧的那次交换。两次较早的交换：目标容得下第二次交换的请求、容不下请求加答复时，两次交换的请求都不留 | 红 |
| T22 | `test_a_round_the_tail_finishes_is_kept_whole`，角色用枚举和普通字符串各一条 | 2f。最后一轮是 6 个并行调用、结果占满 tail、目标够不着：结果是 pinned、当前请求、那条调用和 6 条结果，没有占位 | 2 条全红 |
| T23 | `test_the_round_the_tail_finishes_keeps_its_results_in_the_head`，同样两种角色写法 | 2f。3 个并行调用的一轮，一条结果在 head、两条在 tail，目标够不着：这一轮整轮留着，没有哪条结果被换成占位，结果高于目标 | 2 条全红 |
| T24 | `test_only_results_that_answer_the_call_hold_it` | 2f 的第 3 步，两个方向各一段对话。tail 第一条是没有调用的结果、后两条应答 head 末尾的调用时，那一轮留着；tail 开头的结果都不应答 head 末尾的调用时，那条调用照常丢 | 红 |
| T25 | `test_a_request_does_not_outlive_its_last_call`，角色用枚举和普通字符串各一条 | 第 4 条。较早的交换跑满轮数、没有答复，每轮一个调用：目标容得下它最后一条结果、容不下前面那条调用时，结果里没有这次交换的请求 | 2 条全红 |
| T26 | `test_a_request_does_not_stay_behind_half_a_round_of_orphans` | 第 4 条。同样的交换以 4 个并行调用结束，截断停在这一轮的结果中间：请求不留 | 红 |
| T27 | `test_a_request_in_front_of_the_tail_does_not_outlive_its_last_call` | 第 4 条，在 head 和 tail 的边界上。tail 以当前请求开头，head 的最后一轮属于没有答复的那次交换：请求不留 | 红 |
| T28 | `test_the_target_is_checked_before_the_repair` | 第 6 条。tail 里有一条输入本来就没应答的调用：返回的是修复前正好不超过目标的那个候选，结果比目标多出的正好是那条占位 | 红 |
| T29 | `test_a_call_further_back_in_the_head_is_not_the_round_the_tail_finishes` | 2f 的第 1 步。调用和 tail 开头的结果之间隔着一条别的消息：那条调用不算被 tail 接着应答，照常丢 | 红 |
| T30 | `test_a_call_id_reused_by_a_later_round_does_not_hold_an_earlier_one` | 2f 的第 3 步。每一轮的调用 id 都相同，tail 以调用开头：head 的最后一轮不因为 tail 里靠后的结果带着同一个 id 而留下 | 红 |

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

在第 15 次调用上断言：user 消息里有第二次交换的请求；user 消息里有第一次交换的请求；最新的中等文件的标记出现一次；最旧的中等文件的标记不出现；没有占位结果；system 消息在最前；有一条没写回的 SOFT 降级记录。`r4_probe_assembly.py` 把每个方案换进去跑同一个用例（`r4_out_13_assembly.log`）：

| 方案 | 第 15 次调用的角色序列 | 视图里的中等文件 | 没通过的断言 |
|---|---|---|---|
| 今天 | `suatatatatatatat` | 1、2、3 | 第二次的请求、最新文件、最旧文件、占位 |
| E | `suatatatatuatatat` | 1、2、3 | 最新文件、最旧文件、占位 |
| C+ | `satatauatatatatatatat` | 5、6 | 第一次的请求 |
| D | `suatatatatatatat` | 无 | 第一次的请求、最新文件 |
| G、G+、第 3 版的规则、定稿规则 | `suatatauatatatatatatat` | 5、6 | 无 |

T18 的用例第 3、4 版都没有改。用 pytest 的形式在暂存副本里重跑：今天红在 4 条断言上，换上定稿原型后通过（`r3_eval_scratch.py`、`r3_out_14_eval_today.log`、`r4_out_16_eval_prototype.log`）。这个用例每轮只有一个调用，每次交换都有答复，G、G+、第 3 版的规则和定稿规则在它上面没有区别。G 和 G+ 的区别由 T11 和变异 M4 钉住；2f 由 T22 至 T24、T29、T30 和变异 M18 至 M25、M32、M33 钉住；第 4 条里 tool 消息那一句由 T25 至 T27 和变异 M29、M30 钉住。

`Headroom` 用的是 `target=SOFT, trigger_call=14, trigger_tokens=6150`：量到第 14 次调用的增长是 6,031，第 15 次是 6,213（`r3_out_12_assembly_measure.log`），取两者之间。实施时 system 提示或工具表变了要重量。`tests/evals/test_dataset_floor.py` 的 `BASELINE` 从 30 调到 31。

审核方第 2 轮指出的三处缺口。审核方照 §4.1 的文字另写了一份实现，在上面做了 21 处变异，第 2 版的 27 条用例放过了 5 处（`RV2/rv2_out3_proposed_tests_matrix.log`）。派发第 3 版时点名了其中三处，用的是这三句话："只保留第一条开头的 system 消息""后面没有回复的请求永远不丢""第二次较早交换的请求在回复丢完后仍留着"。对照 §4.1 核实下来，这三句写的都是变异体的行为，规则要求的正好相反，所以测试钉的是规则那一面：

| 审核方的变异 | 变异体的行为 | 规则的哪一条 | 第 2 版为什么钉不住 | 补的测试 |
|---|---|---|---|---|
| m15 | 只保留第一条开头的 system 消息 | 2b：head 最前面连续的 system 消息有几条留几条 | T13 的夹具只有一条 system 消息 | T19 |
| m17 | 后面没有回复的请求永远不丢 | 第 4 条：后面没有 assistant 回复的请求轮到时直接丢 | 27 条用例里没有两条请求连着的对话，E7a 只在边界探针里 | T20 |
| m09 | 第二次较早交换的请求在回复丢完后仍留着 | 第 4 条：每一次交换的请求都在它最后一条 assistant 回复被丢的那一步一起丢 | T6 只钉了最旧的那次交换。这个变异把"回复的请求"取成 head 里第一条请求，对最旧的交换没有区别；T5 的断言里第二次交换的请求本来就该在 | T21 |

`r4_reviewer_matrix.py` 把审核方的 21 处变异原样拿来（`rv2_mutants.py` 没有改），用第 4 版的用例重跑（`r4_out_21_reviewer_mutants.log`）。审核方的那份实现是第 2 版的规则，没有 2f，第 4 条里也没有 tool 消息那一句，所以 T22 至 T27、T29、T30 在这一轮里排除，剩下 31 条用例：m09 红在 T20、T21，m15 红在 T19，m17 红在 T20，m18 红在 T28。

这个矩阵里第 3 版留着没钉的有 m16 和 m18 两处，第 4 版的处理不一样：

- m18，量 token 放在修复之后。补了 T28。第 6 条明写着量在修复之前，把它改到修复之后第 3 版的 35 条用例全绿，复核方指出这一条该钉。
- m16，head 里任何位置的 system 消息都不丢。不补。引擎提交对话之前按角色去掉 system 消息（`engine/loop.py:671-698` 的 `_settle`），entry 层的历史里不会有中间的 system 消息，加测试钉的是产品路径上不会出现的输入。

2f 的"相邻"第 3 版也没有钉住。复核方在第 3 版的原型上做了两处变异都不红：第 1 步改成往回找任意一条带调用的消息；第 3 步读过 tail 里第一条不是 tool 的消息（`RV3/rv3_out4_mut_own2f.log` 的 X9、X3）。两处只在不规整的输入或调用 id 不唯一时分得开。第 4 版各补一条：T29 是复核方的候选，T30 是这份计划写的，用的是每一轮都叫同一个 id 的调用。T24 上面的注释原来写的是"按相邻关系和 id 配对"，它实际钉的是第 3 步的两个方向，注释改成了这个。

变异。对定稿原型做了 33 处定点变异（`r4_mutate.py`，`r4_out_10_mutations.log`），每一处都至少让一条新测试转红，变异后文件按内容核对恢复。M1 至 M17 是第 2 版的；M18 至 M25 变的是 2f，M26 至 M28 是上面三处缺口在原型这种写法下的对应，都是第 3 版的；M29 至 M33 是第 4 版加的。复核方第 3 轮把 R11 的改法打进副本后重做了第 3 版的 28 处，也是每处至少一条转红（`RV3/rv3_out4_mut_planner.log`）。

| 变异 | 转红的新测试 |
|---|---|
| M1 改回今天的方向 | T1、T5 至 T9、T13 至 T15、T19 至 T21、T25 至 T28 |
| M2 摘要不再不丢 | T4 |
| M3 head 里最后一条请求从不保留 | T3、T11、T15、T20、T22 至 T24、T29、T30 |
| M4 去掉 2e（即 G） | T11 |
| M5 head 里最后一条请求无论 tail 怎么开头都保留 | T12、T27 |
| M6 请求不跟着回复留（即 C+） | T5、T6、T15、T21 |
| M7 请求在没有回复之后仍然留着 | T6 |
| M8 head 开头的 system 消息不保留 | T13、T19 |
| M9 以标记开头的消息不看角色都算摘要 | T14 |
| M10、M11 两处角色比较改成 `is` | T15 |
| M12 回复的请求取 head 里第一条请求而不是最近的一条 | T5、T21、T25、T26 |
| M13 没有可丢的消息时重建 | T10 |
| M14 第一个候选就返回 | T1、T2、T3、T5、T6、T8、T11、T12、T14 至 T17、T20 至 T30 |
| M15 不修复 | T7、T24 至 T29 |
| M16 head 里每条 user 消息都保留 | T5、T6、T15、T20 至 T26 |
| M17 2e 看 tail 的最后一条而不是第一条 | T12、T27 |
| M18 去掉 2f（即第 2 版的规则） | T22、T23、T24 |
| M19 2f 只留调用，不留它在 head 里的结果 | T23 |
| M20 2f 的第 3 步只看 tail 的第一条 | T24 |
| M21 2f 的第 3 步不比对 `tool_call_id` | T24 |
| M22 2f 第 1 步的角色比较改成 `is` | T23 的普通字符串那一条 |
| M23 2f 第 2 步的角色比较改成 `is` | T22、T23 的普通字符串那一条 |
| M24 2f 第 3 步的角色比较改成 `is` | T22、T23 的普通字符串那一条 |
| M25 不管 tail 接不接着应答，head 的最后一轮都留着 | T3、T11、T15、T20、T24、T27、T30 |
| M26 开头的 system 消息只留第一条（审核方的 m15） | T19 |
| M27 后面没有回复的请求不丢（审核方的 m17） | T20 |
| M28 只有 head 第一条位置上的请求会随回复一起丢，后面的请求都留着（审核方 m09 的症状） | T20、T21 |
| M29 tool 消息又算留着的回复（即第 3 版的规则） | T25、T26、T27 |
| M30 第 4 条里认 tool 消息的角色比较改成 `is` | T25 的普通字符串那一条 |
| M31 量 token 放在修复之后（审核方的 m18） | T28 |
| M32 2f 第 1 步往回找任意一条带调用的消息（复核方的 X9） | T29 |
| M33 2f 第 3 步读过 tail 里第一条不是 tool 的消息（复核方的 X3） | T30 |

同一轮里还试了三处不算变异的改动，就是 §4.3 列的那三处，脚本里记作 EQ1 至 EQ3。没有测试转红，结果也确实不变。实施方对落地的代码重做变异时会碰到它们，不用为它们补测试。

现有测试里只有 §1.6 最后那一条会红，由 T7 替换。`test_a_history_that_fits_exactly_is_still_a_history_that_fits`、`test_every_transform_honours_an_injected_counter`、`test_every_way_a_summarizer_can_fail_degrades_and_leaves_a_trace` 等在定稿原型上都通过，不用改。

### 5.3 文档

| 文件 | 改动 |
|---|---|
| `omicsclaw/context/transcript.py` 里 `fit_to_budget` 的 docstring | 要写清的有：§4.1 的六条；结果可能高于目标，原因是不丢的那几条加 tail 本身超过目标，或者修复补了占位；切点在 head 里只会留下孤儿结果，由修复删掉，不造占位；tail 接着应答的那一轮整轮留着；较早的请求随它最后一条 assistant 回复一起丢，工具结果留不住它；`pinned=0` 时开头的 system 消息照样留着；`pinned` 一侧的限制，见表后 |
| `docs/core-features/context-engineering.md` | `:284` 表格里 `fit_to_budget` 那一行改成新规则 |
| `docs/core-features/progressive-compactor.md` | §5.3 最后一句（`:342`）改成新规则；§11 设计决策表加一行；§12 已知限制加五条：降级期间会话部分不命中前缀缓存；降级视图的第一条仍可能是回复，另行立项在 provider 适配器层处理；`pinned` 一侧的两条限制，见表后；EMERGENCY 的任务锚点仍按位置取 `rest[0]`，以后另开计划，届时用降级截断的同一个定义。第 3 版列的第五条（较早的请求可能独自留在降级视图里）随 R11 的裁定去掉 |
| `docs/core-features/planning.md` | §13 第 3 条：去掉 SOFT、FULL 降级截断那一种，只留 EMERGENCY |
| `docs/core-features/eval.md` | 用例数有两处，`:7` 和 `:254`，都从 30 改成 31；§2.1 的表加 T18 |
| `docs/plans/0039-planning-layer.md` | §9.5 末尾加一条带日期的记录：SOFT、FULL 降级那一种由本计划关闭。原文不动 |
| `docs/plans/0055-nudge-and-closing-gate.md` | §10.6 的已知行为里记一条：持续降级时 `MemoryNudge` 可能连续提醒（§3.4） |
| `docs/plans/0078-fallback-truncation-order.md` | 加交付记录一节 |
| `CHANGELOG.md` | 合并时在顶部加一条，要求见表后 |

`pinned` 一侧有两条限制，docstring 和 `progressive-compactor.md` §12 都要写。第一条：降级截断只保住被 head 和 tail 的边界切开的那一轮。`pinned` 前缀停在一轮中间时，也就是它以一条工具调用结尾，或者以这条调用的一部分结果结尾，这一轮余下的结果是 head 里最旧的消息，每次截断都最先丢，修复给那条调用补占位；结果很小时占位比它顶替的结果大，返回的视图可以比输入还大。调用方让 `pinned` 停在一轮的边界上就不会遇到。第二条：`pinned` 前缀以一条请求结尾时（比如 `pinned=2`，盖住 system 消息和第一条请求），这条请求不会丢，它在 head 里的回复照常从最旧的丢，丢完以后它独自留在视图里。这种前缀本来就停在一轮的边界上，所以"停在一轮的边界上"避不开第二条；`pinned` 只盖 system 消息才两条都遇不到。`entry/compaction.py` 的 `build_compactor` 传的是 `pinned=1`，盖的是 system 消息，走不到这里。第 4 版把这两条写成了一条，说前缀停在一轮中间时"它的请求也可能留不住回复"，方向反了：前缀停在一轮中间时那条调用就在前缀里，请求总有回复。复核方指出，实施时核实后改成现在这样（§12.2）。§12 里另写一句：这一侧 2026-10-09 裁定不做；以后要做的话可以从 `_answers_owed_by` 出发，但它遇到第一条对不上的结果就停，和 2f 第 3 步的读法不一致，要先对齐（§4.1）。

`CHANGELOG.md` 的条目要写明 `fit_to_budget` 的行为变了。它在 `omicsclaw.context` 的公开面上，名字和签名都不变，变的是三件事：丢的顺序从 head 靠近 tail 的一端改成最旧的一端；有了不丢的消息，包括压缩摘要、开头的 system 消息、当前请求和 tail 接着应答的那一轮；较早的请求随它最后一条 assistant 回复一起丢。今天的函数在 pinned 加 tail 超过目标时结果也会高于目标，多了不丢的消息以后这种情形更多，条目里要提到。仓库外的调用方要靠这一条判断自己受不受影响。

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

| 命令 | 基线 | 换上定稿原型 |
|---|---|---|
| 第一条 | 2842 passed、11 skipped、26 deselected、3 xfailed，132 秒 | 2841 passed、1 failed，其余相同，145 秒。红的是 T7 要替换的那一条 |
| 第二条 | 842 passed、14 skipped、1 deselected、1 xpassed，103 秒 | 相同，104 秒 |

基线是第 3 版在 `r3_base/` 里跑的（`r3_out_29_baseline_layers.log`、`r3_out_30_baseline_guards.log`），基线提交没有变。定稿原型是第 4 版在 `r4_proto/` 里跑的（`r4_out_17_prototype_layers.log`、`r4_out_18_prototype_guards.log`）。通过和失败的条数和第 3 版的原型相同，也和复核方把 R11 的改法打进副本后跑到的相同（`RV3/rv3_out5_variant_layers.log`、`rv3_out5_variant_guards.log`）。

第一条里 deselected 的 26 条是 `tests/evals/live` 的真实模型路由用例，被 `pyproject.toml` 的 `addopts` 按 `eval` 标记排除。命令行上覆盖掉 `addopts` 时它们改为 skipped，跳过数是 37，审核方量到的就是这个数。用时取决于机器当时的负载，审核方同一条命令量到约 6 分钟。

另外：

- 写测试时先在没改的代码上跑新测试，确认红绿和 §5.2 的表一致，再改代码。
- 变异检查：对落地的实现重做 §5.2 的 33 处变异（`r4_mutate.py` 改一下路径可以直接用），每处至少一条测试转红。脚本末尾的 EQ1 至 EQ3 不算变异，不会有测试转红。
- 探针 `r4_probe_scenarios.py`、`r4_probe_sequence.py`、`r4_probe_gate.py` 在落地的实现上重跑，"B today"那一行应当和定稿规则那一行相同。`r4_probe_fuzz.py` 的两部分都重跑，三种写法之间的差异和 §4.4 性质表里的违反次数都应当是 0。`r4_probe_tail.py` 重跑，"today"那一行应当和"final rule"那一行相同。
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
- 第三次，依赖一条会被丢掉的旧工具结果：把第 1 个 panel 文件第 3 行的原文抄出来。规则落地后最旧的读取最先丢，这一问看的是模型发现内容不在视图里时怎么办。

同一组输入在 `main` 和修复分支上各跑一次。记录和判断：

- 每条 `degraded` 非空、`written_back` 为假的压缩记录之后的那次请求里，有没有当前请求、上一次交换的请求和答复、哪几个文件的读取结果。
- 第二问答对没有，之前有没有重读 panel 文件。
- 第三问是重读了文件、明说看不到，还是编了一行。重读一次是这条规则可以接受的代价，编造不是。
- 两个分支上模型调用的次数，和 `RecordingProvider.usage` 里的缓存命中，用来看前缀缓存的代价在真实后端上有多大。取哪几个数见下面第三条。

四个操作上的细节，每条都在 `5daf5482` 的代码里核对过：

- `run_case` 的 `timeout_s` 要显式传。默认值是 `CASE_TIMEOUT_S`，30 秒（`evals/runner.py:71`、`:503`），真实模型的三次交换跑不完。超时后这次运行记一条 `case_timeout`，后面的交换不再提交（`:423-428`）。`tests/evals/live/test_live_routing.py:37` 给单次交换的用例传的是 300 秒；这里是三次交换加十几次工具往返，从那个量级往上取。
- 压缩记录里出现 EMERGENCY，这次运行作废，放宽 `trigger_tokens` 重跑。记录在 `Result.compactions` 里，看每条的 `pressure`。`should_write_back` 对 EMERGENCY 的规则是有消息被删、token 变少或有内容被 offload 才写回（`context/progressive.py:41-56`）；EMERGENCY 本身就是在截断，这个条件实际上总成立。写回之后两个分支面对的历史不再是同一段，比的也就不再是 SOFT、FULL 的降级截断。真实模型的工具结果有多大不由脚本控制，窗口又是按 SOFT 收紧的，一条大结果就能把压力推过 EMERGENCY 的 0.95。
- 前缀缓存的代价看 `Usage.cache_read_tokens`。只看 `input_tokens` 看不出来：OpenAI 兼容的后端里它已经包含命中缓存的部分（`provider/openai_provider.py:375`）。缓存命中读自 `prompt_tokens_details.cached_tokens`，DeepSeek 读 `prompt_cache_hit_tokens`（`:386-392`）。`RecordingProvider.usage` 是整次运行所有调用的合计，侧线调用也在内，没有按调用拆开（`evals/live.py:258`、`:284`）。两个分支各取 `cache_read_tokens`、`input_tokens` 和 `uncached_input_tokens`（`schema/message.py:285`、`:295`）来比。
- 提示里要求一次调用只读一个文件，跑完检查每条压缩记录的 `offloaded`，非空就作废重跑。`Offloader.min_tokens` 是 1000（`context/offload.py:109`），head 里超过它的工具结果在 WARN 及以上各档都会被 offload（`context/compaction.py:642`），而 `compact_at` 默认就是 WARN（`entry/config.py:341`）。每个 panel 文件约 800 token，单读不会超。模型一次读两个以上时，比如一条 `bash` 里 `cat` 几个文件，那条结果超过 1000 token，等它排进 head、压力又到了 WARN，就被换成预览并写回历史。之后两个分支上模型手里的 panel 内容都只剩预览，第二、三问比的不再是截断顺序。写回发生在 `trigger_call` 之前时 Runner 还会记一条 `headroom_missed`（`evals/runner.py:201-228`）。

真实模型每次走的轮次和推理文字长度不同，`trigger_tokens` 可能要调一两次才让降级落在第二次交换里。把窗口再收紧一档、让第一次交换在第二问时被整段丢掉，可以再跑一对，用来看最坏的情况；这一对建议做，不算前置条件。

真实会话里并行调用是模型自己决定的，这对会话顺带能看到 2f 在真实形状下起不起作用：看降级记录之后的那次请求里，最新一轮的工具结果是不是都在。这一项只记录，不作为通过条件。

这对会话本计划没有做。实施时做了，见 §12.5。

## 7. 对规划闸门残留误触发的影响

SOFT、FULL 降级那一种，规则落地后不再发生。当前请求要么在 tail 里，要么按 2d 留在 head 里，`_gate_fires` 往回数一定先遇到它。带 2f 的规则和定稿规则都加进了下面这个探针，160 次里它们在每一个夹具上的触发次数和数到的轮数都和 G+ 相同。

`r4_probe_gate.py` 用计划 0039 §9.5 的形状（上一次交换 9 轮只读加一条答复，本次 3、4、5、7 轮，两档，摘要器抛错或返回空串，每个夹具 16 次）。今天误触发多少次取决于夹具，所以把工具结果的大小和 FULL 档取的比例都扫了一遍（`r4_out_07_gate.log`）：

| 工具结果的字符数 | FULL 取 0.85 | FULL 取 0.88 |
|---|---|---|
| 40 | 今天 16 次误触发，数到 12 或 13 | 16 次，12 或 13 |
| 100 | 14 次 | 16 次 |
| 200 | 12 次 | 12 次 |
| 400 | 12 次，数到 10、11、12，另有 4 次数到 4 没触发 | 12 次 |
| 800 | 12 次 | 12 次 |

10 个夹具共 160 次：今天 134 次误触发，请求被丢的也是 134 次；G+ 和定稿规则都是 0 次，数到的正好是 3、4、5、7；E 也是 0 次，但数得偏少，本次 7 轮时只数到 3 到 5。第 1 版写的"16 次都误触发，数到 12 或 13"只对 40 字符那一行成立，审核方在 400 字符的夹具上量到的是 12 次（`RV/rv_out6_gate.log`）。

逐次调用的序列里（§3.4）：多交换序列今天在第 6 次调用触发，G+ 和不压缩时一样在第 9 次；第三个序列今天在第 4 次调用触发，G+ 在第 9 次。

降级截断之后计数仍可能偏少：丢到本次交换自己的轮次时，可见轮次少于实际轮次，凑不满 `gate_turns` 就不触发。这是计划 0039 §4.4 已经接受的"压缩后可见轮次不足"，方向是少提醒。

有两种情况计数仍会数进更早的交换，都不是降级截断造成的：库调用方用 `run_turn(app, history)` 而不给 `user_text`（计划 0039 §9.3 按同一次请求算）；之前某次 EMERGENCY 已经把请求从历史里丢掉并写回，之后的降级截断恢复不了它。

EMERGENCY 那一种不变。EMERGENCY 不经过 `fit_to_budget`，探针里三个场景在今天、E、G+ 和定稿规则下的视图逐条相同，其中两个误触发：当前请求很大、前面是一次 9 轮只读的交换时数到 11（实际 1）；当前请求 300 token、本次交换的结果很大时，贪心走到请求时剩余预算不够，请求被跳过，更旧更小的消息还放得下，数到 14（实际 6）。按裁定这次不改。

## 8. 风险和没验证的部分

- R1 视图的第一条仍可能是回复。规则落地后，这只在输入里第一条留下的回复前面本来就没有请求时出现：对话里根本没有 user 消息（S10、E1、E6h），或者 head 开头是一段请求已经不在的回复。随机探针第一个生成器的 32,267 个截断结果里有 701 个，全部属于这一类（§4.4）。Anthropic 的 Messages API 是否拒绝这种对话没有核实，`anthropic_provider.py` 的 `encode_conversation` 也不处理它。按裁定这不并进本计划，另行立项在 provider 适配器层处理；本计划只把它记进已知限制。
- R2 "留新的比留旧的有用"是判断。§6.3 的会话是它唯一的真实模型证据，还没有做。实施时做了一对，见 §12.5。
- R3 前缀缓存的数字来自探针里按消息逐条比较的前缀，system 消息只有 51 token。真实后端的缓存命中没有量，§6.3 会顺带量一次。量到的数在 §12.5。
- R4 tail 里的工具结果被截断删掉。第 2 版把它列为 N1，2026-10-09 裁定采纳尾部一侧，条文是 §4.1 的 2f。定稿规则下，输入自己修复后还留着的 tail 消息都在结果里（§4.4 的性质表，六个生成器 0 次违反）。没有覆盖的是 `pinned` 一侧，见 R7。
- R5 结果可能高于降级目标。原因有三种：不丢的那几条加 tail 本身就超过目标（当前请求很大、较早的请求按 2e 留着、摘要很大、按 2f 留着的那一轮很大）；输入里本来就有没应答的调用，占位在判断之后补上；`pinned` 前缀停在一轮中间时截断造成的占位（R7）。第 1 版只写了第一种，第 2 版补了第二种，2f 和第三种是第 3 版补的。`pinned` 只盖 system 消息、输入里又没有未应答的调用时，结果不大于输入。能走到回退的对话压力低于 EMERGENCY，总量低于 `usable × 0.95`，所以结果仍低于 `usable`。有占位时结果可以比输入大，每条占位十几个 token；§4.4 的前三个生成器里这样的结果有 27、14、162 个。
- R6 tail 的第一条是回复、head 里又没有任何请求时（比如一次很长的交换，它的请求之前被 EMERGENCY 丢掉并写回了），2e 没有可留的，视图以回复开头。归入 R1。
- R7 `fit_to_budget` 在 `omicsclaw.context` 的公开面上，名字和签名不变，行为变了。仓库内只有一个调用方；仓库外有没有调用方不知道，所以 `CHANGELOG.md` 的条目要把变了什么写全（§5.3）。`pinned` 前缀停在一轮中间的调用方不在 2f 的保护之内：这一轮在 head 开头的结果每次截断都最先丢并被补上占位（E12、E12a、E12b；§4.4 第三个生成器里这样的 11,876 个截断结果有 11,866 个带着截断造成的占位）。`pinned` 一侧还有第二条限制，和前缀停不停在一轮中间无关：前缀以一条请求结尾时，这条请求不会丢，它在 head 里的回复丢完以后它独自留在视图里。复核方在 B 族里量到 1,649 个这样的结果，前缀的最后一条都是 user 消息（`RV3/r4_out2c_pinned_request.log`）；实施时另写探针核实，见 §12.2。第 4 版把这一条写成了前缀停在一轮中间的后果，方向反了。2026-10-09 裁定这一侧不做，只写明，写在哪里见 §5.3。
- R8 合并冲突。2026-10-09 重新核对：本地还没合并进 `main` 的分支里，改了代码、测试或功能文档的有 `feat/ask-user`、`feat/ask-user-r3`、`feat/ask-user-r4`、`feat/subagent-usage-desktop` 和 `backup/subagent-usage-reviewed`。它们相对 `main` 都没有改 §5 列的文件：`docs/core-features/` 下动的是 `cli.md`、`sub-agent.md` 等另外几份，`tests/evals` 下只动了 `test_runner.py`。它们如果新增脚本化 eval，`BASELINE` 和 `docs/core-features/eval.md` 的用例数要以合并时的数字为准。
- R9 Desktop 和 channel 没有跑。它们和 CLI 一样经 `SessionRegistry` 到 `TurnRunner`，T18 覆盖的是这一段共用路径。
- R10 裁定 3 只管降级截断。`pinned=0` 而首条是 system 消息的调用方，摘要成功时 system 消息仍在 head 里、仍会被摘要掉；EMERGENCY 时它是 `rest[0]`，被当成任务无条件保留，真正的请求反而没有锚点。这两条路径这次都不动。
- R11 较早的交换没有最终答复就结束时，它的请求独自留在视图里。第 3 版看到并记下了这件事，owner 2026-10-09 裁定把改法并进定稿：tool 消息不算留着的回复，请求在它最后一条 assistant 回复被丢的那一步一起丢（§4.1 第 4 条）。下面是改之前的情形、改的依据和改了之后剩下的。

  哪些交换是这个形状。在 `5daf5482` 上，产品路径里只有一种：跑满 `max_turns` 的交换。引擎的循环走到上限时轨迹以工具结果结尾（`engine/loop.py:384-386`、`:454-461`），entry 层把它当正常结束写进历史（`entry/turn.py:583`、`entry/session.py:814-816`），上限默认 50（`entry/config.py:189`）。被取消和失败的交换不写历史，连请求本身都不留（`entry/session.py:817-819`，`engine/loop.py:324-327`）。输出被截断的交换以 assistant 消息结尾（`engine/loop.py:717-718`），它的请求随这条消息走。第 3 版把用户中断和出错也列成来源，那两条写错了。计划 0080 落地后会多一个来源：被截断的交换在下一次开场清理之后可能以一轮工具调用结束、没有最终答复（分支 `docs/plan-0080-truncated-exchange`，它的 §8）。

  改之前发生了什么。那一轮的调用已经丢了、结果还剩至少一条时，第 3 版的规则把剩下的结果算作回复还留着，请求不丢；修复随后删掉这些孤儿结果，视图里剩一条没有任何回复的旧请求，后面紧跟下一条请求（E13）。§4.6 的四组对话里，第 3 版的规则有 230、293、61、61 段是这样，定稿规则都是 0 段（`r4_out_05_tail.log`）。那个生成器里多数长交换没有最终答复，比真实会话多得多；给每次较早的交换补上答复之后，第 3 版的规则也是 0 段（`r3_out_22_lone_request.log`）。今天的代码在这四组里是 0、11、10、15 段，其中 0、9、6、15 段连当前请求也不在视图里，是另一种更糟的形状。第 3 版只引了第一组的 0 段，不准确；复核方在它自己的生成器上量到今天是 3,000 段里 10 段，10 段的当前请求都不在视图里（`RV3/rv3_out3d_today_lone.log`）。

  为什么值得改。复核方在真实的 DeepSeek 接口上对三种视图各发了请求（`RV3/rv3_live_lone_request.py`，`rv3_out8_live_*.log`）。视图是 `S U1 U2` 加当前交换的三轮时，模型没有回答当前请求，转去做旧请求，发了一条 `bash` 调用。去掉 `U1` 的视图直接回答了当前请求。样本只有这么多：一个型号（`deepseek-v4-flash`），每种视图成功一次，system 提示是几行的替身，对话是手工搭的。这份计划没有再发请求。

  改法挡不住的。同一个探针的第三种视图是旧请求带着它最后一轮、没有答复，也就是比上面早一步的样子。模型同样回头接着做旧请求。这种视图两版规则都会发。根子在跑满轮数的交换没有一条收尾的消息，那是 entry 层的事，不在本计划里。

  改了之后还会独自留着的请求有三类，都不在 entry 层的产品路径上：请求在 `pinned` 前缀里而它的回复在 head 里（R7）；输入里这条请求本来就只有孤儿结果，输入自己修复之后它就没有回复；tail 以一条输入里就没有配对的工具结果开头，2e 把它当回复，留下了 head 里最后一条请求。随机比对里各有多少在 §4.4。代价见 §4.6 的最后一条。

范围外的观察，只记录、不处理：

- 持续降级时 `MemoryNudge` 可能连续提醒。今天的代码在多交换序列的第 17、18、19 次调用连续提醒了三次（§3.4）。记进计划 0055 §10.6。
- `_emergency_survivors` 会留下结果被跳过的调用消息，每次补 3 到 5 条占位，补完之后可能高于它自己的目标（§1.2、§1.3）。留给以后的 EMERGENCY 计划。
- 摘要成功的 FULL 档也会删掉 tail 里的工具结果，并且写回历史。复现条件：压力到 FULL，摘要器正常返回，head 和 tail 的边界切开一轮工具调用。FULL 把整个 head 交给摘要，那条调用在 head 里，被摘要代替；`apply_compaction` 把 pinned、摘要消息和 tail 拼起来再修复，tail 里失去调用的结果被删。最后一轮是 6 个并行调用、6 条结果正好占满 tail 时，压缩后的对话只剩 system 消息和摘要，最新的 6 条结果一条不剩。这次压缩不算降级，`should_write_back` 为真，被删的结果从历史里也没有了，后面的调用恢复不了。一轮 3 个并行调用、一条结果在 head 两条在 tail 时，丢的是 tail 里那两条。SOFT 的摘要路径和 EMERGENCY 在这两个形状上都不丢 tail：SOFT 只摘要 head 较旧的一半，那条调用在留下的一半里。这是审核方第 2 轮顺带发现的（`RV2/rv2_out9_boundary_other_paths.log`）。第 3 版用这份计划自己的夹具在 `5daf5482` 上重现了：1551 token 的对话压缩成 100 token，`ProgressiveCompactor.compact` 返回的写回标志为真，14 条工具结果剩 0 条（`r3_probe_full_tier.py`，`r3_out_19_full_tier.log`）。它不经过 `fit_to_budget`，本计划的改动碰不到它。本计划落地以后两条路径会不一致：摘要失败时，降级截断按 2f 保住被边界切开的那一轮；摘要成功的 FULL 路径仍把它删掉并写回。同一份夹具在定稿原型上，摘要失败时 6 条最新结果都在（1551 token 降到 1221，不写回），摘要成功时仍然只剩 `S SUM`（`r4_probe_full_tier.py`，`r4_out_19_full_tier_final.log`）。owner 2026-10-09 被问到哪些事另立项时没有选它。
- `repair_tool_pairs` 按 id 把调用和结果配对。一条 assistant 消息里两个调用的 id 相同时，一轮完好的并行调用过一遍修复，第二条结果会被换成占位。复核方在基线上重现了，这里也重现了（`r4_probe_duplicate_ids.py`，`r4_out_32_duplicate_ids.log`）。这是修复自己的行为，截断、摘要和 EMERGENCY 的结果都过这一步；不在本计划里改。

## 9. 裁定

### 9.1 已裁定

1. 做法用 G+，第 1 版的规则 4（更早的交换整段去留）删掉。条文见 §4.1。
2. "发给模型的第一条消息必须是 user"不并进 0078，另行立项在 provider 适配器层处理。0078 把它写成已知限制（R1，`docs/core-features/progressive-compactor.md` §12）。
3. 开头的 system 消息列为不丢，`pinned=0` 时同样（§4.1 的 2b）。这里把"开头的"读成 head 最前面连续的 system 角色消息；出现在对话中间的 system 消息不在此列，本仓库的流程不会产生它。
4. 一对真实会话的对比是合并的前置条件，要有一条依赖上一次交换答复的追问（§6.3）。
5. EMERGENCY 的任务锚点这次不改，以后另开计划，届时定义和 §4.1 第 2 条一致。
6. 代码、测试、文档一起改，不拆两次。第 1 版 Q5 的两件范围外观察只记录（§8 末尾）。
7. N1，2026-10-09：采纳尾部一侧。tail 以工具结果开头时，head 末尾发出这次调用的 assistant 消息和它在 head 里的结果不丢，也就是被 head 和 tail 的边界切开的那一轮整轮不丢。条文是 §4.1 的 2f。第 3 步采用宽读法：tail 开头连续的 tool 消息里至少有一条应答那条调用，那一轮就留着。N1 裁定的原话没有定到这一步，宽读法是第 3 版的计划方选的，汇报时说明了它和只看 tail 第一条的窄读法的差别。复核方在不规整的输入上量到两种读法分得开的有 43 段，窄读法在这 43 段里都删掉了输入自己修复后还留着的 tail 消息（`RV3/rv3_out1_fuzz.log`）；规整的输入上两者没有区别。owner 2026-10-09 在第 3 版之后裁定保留宽读法。`pinned` 一侧不加，只写明（§5.3）。
8. 2026-10-09：第 2 版批准。批准的是计划，实现另行下令派发。
9. 审核中顺带发现的"摘要成功的 FULL 档也会删掉尾部工具结果并写回历史"：owner 2026-10-09 被问到哪些事另立项时没有选它。记在 §8 末尾。
10. R11，2026-10-09，在第 3 版之后：改法并进定稿。tool 消息不算留着的回复，请求在它最后一条 assistant 回复被丢的那一步一起丢。条文是 §4.1 的第 4 条。
11. 2026-10-09：实现在第 4 版复核通过后派发。

### 9.2 仍要 owner 定的

没有。

## 10. 各版之间改了什么

### 10.1 第 1 版到第 2 版

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

### 10.2 第 2 版到第 3 版

| 裁定或复核意见 | 处理 |
|---|---|
| N1 采纳尾部一侧 | §4.1 加 2f，写成三步判定；§4.2 按定稿规则重写 E6c、E6e、E12，加 E6f、E6h、E6i、E12a、E12b；§4.3 的原型在 `_held` 末尾加一行、加一个辅助函数；§9.2 清空，N1 移到 §9.1 第 7 条 |
| `pinned` 一侧不加，只写明 | §4.1 写了以后要做时用 `_answers_owed_by`，并且要先把它挪到 `transcript.py`；§5.3 列了 docstring 和 `progressive-compactor.md` §12 要写的内容 |
| §4.1 的 2a 和 §4.6 矛盾 | 2a 写明修复只删输入里本来就没有配对的 tool 消息；§4.6 去掉"tail 里的工具结果仍可能被删"，换成 2f 的代价；§4.4 的性质表加了对应的一行，三个生成器 0 次违反 |
| `gplus_straddle` 建在错误的底子上 | 没有沿用。定稿原型在第 2 版 §4.3 那段代码上改，另照条文写了一份 `from_text` 来比对（§4.3、§4.4） |
| 性质表第 5 行只在 `pinned` 前缀不以调用结尾时成立 | 加了限定；加了第三个生成器专门量这种情况，11,866 个截断结果每一个都带截断造成的占位（§4.4） |
| 测试的三处缺口 | 对照 §4.1 核实：派发时的三句话写的是变异体的行为，规则相反。补 T19、T20、T21，审核方的 m15、m17、m09 转红。同一矩阵里的 m16、m18 没有点名，没有加测试，原因写在 §5.2 |
| N1 的测试 | 补 T22、T23、T24，其中 T22、T23 各带枚举和普通字符串两种角色写法；变异 M18 至 M25（§5.2） |
| docstring 和 §12 要写 `pinned` 一侧的限制 | §5.3 |
| §6.3 的四个操作细节 | 逐条在代码里核对后写进 §6.3，带文件和行号 |
| CHANGELOG 要写明 `fit_to_budget` 的行为变了 | §5.3 |
| FULL 档摘要成功时删尾部工具结果：owner 被问到哪些事另立项时没有选它 | 在 `5daf5482` 上重现后记在 §8 末尾和 §9.1 第 9 条 |
| 批准的是计划，实现没有下令派发 | 状态行、§9.1 第 8 条 |

修订时新看到、不在裁定和复核意见里的：

- 较早的交换没有最终答复就结束时，它的请求可能独自留在视图里（E13、R11）。来自第 2 版已批准的条文，这一版写明了，没有改规则。
- `_request_of_oldest_reply` 里有两行去掉以后结果不变（§4.3）。原样留着。
- `pinned` 一侧在结果很小时会让视图比输入大（E12a）。审核方第 2 轮的随机比对里有"结果比输入大"的计数（`RV2/rv2_out1_fuzz.log` 的 P9，32 次），第 2 版的正文没有写这种情形。
- R8 的分支列表按 2026-10-09 的状态重新核对，多了两个分支。

和第 2 版不同的数字：

- §4.4 的截断数、高于目标数、第一轮不是 user 的次数，原因在该节的表里。
- §4.6 "高于目标"的数字换成定稿规则的。前两组和第 2 版 §9.2 的表相同，后两组是新加的。
- §5.2 的用例数从 27 变成 35，变异从 17 处变成 28 处。M1、M3、M6、M8、M12、M14、M15、M16 转红的测试比第 2 版多，多出来的是新加的 T19 至 T24。
- §3、§7 的数字没有变：定稿规则在那些探针上发出的视图和 G+ 相同。

第 2 版的探针和输出留在原处。第 3 版要用的第 2 版脚本是复制到 `plan-0078-r3/` 之后再改的。

### 10.3 第 3 版到第 4 版

| 裁定或复核意见 | 处理 |
|---|---|
| R11 的改法并进定稿 | §4.1 第 4 条改写；§4.3 的 `_request_of_oldest_reply` 多一个条件，照条文另写的 `from_text` 跟着改；E13 的结果变成 `S U2 ...`，632 token；§4.4 的性质表加一行；§5.3 里 `progressive-compactor.md` §12 少一条限制；§0 第 9 条、R11、§9.1 第 10 条 |
| 改法写成一行还是三行 | 核实后用一行。两种写法在六个生成器 155,405 段有 head 的对话上结果相同，三行写法里第二处 `orphaned = False` 是死代码（§4.3） |
| R11 的改法自己要有测试 | 补 T25、T26、T27，取自复核方的候选；变异 M29、M30。把那个条件改回去，第 3 版的 35 条用例全绿，第 4 版红 4 条 |
| 反向的小代价、旁路、真实模型的依据 | §4.6 最后一条；R11 |
| P2-1 R11 的成因写错两条 | R11 重写：产品路径上只有跑满 `max_turns` 这一种，带代码位置；加了计划 0080 会带来的另一个来源 |
| P2-2 "今天 0 段"只是四组里的一组 | R11 写成四组的 0、11、10、15 段，并写明其中多数连当前请求也不在视图里 |
| P2-3 FULL 档那件事的措辞 | §8 末尾和 §9.1 第 9 条按"被问到哪些事另立项时没有选它"写；补了本计划落地后两条路径不一致这一句，在定稿原型上重现 |
| P2-4 补 m18 的测试 | T28，变异 M31。复核方的候选原样取用 |
| P3-1 2f 的"相邻"没有钉住 | 补 T29（复核方的候选）和 T30（这份计划写的），变异 M32、M33；T24 的注释改成它实际钉的 |
| P3-2 §9.1 第 7 条写明读法 | 写了宽读法和依据 |
| P3-3 `_answers_owed_by` 是窄读法 | §4.1、§5.3 里"以后可以从它出发"的话带上了限定；性质表第 5 行改成按位置判定的"停在一轮中间" |
| P3-4 m16 不补 | §5.2 写了理由：`_settle` 按角色去掉 system 消息 |
| P3-6 调用 id 不唯一时的占位 | 在基线上重现，记进 §8 的范围外观察 |
| §6.3 `should_write_back` 的措辞 | 改成有东西被删或被 offload 才写回，实际上总成立 |
| 状态、派发 | 状态行；§9.1 第 11 条；§9.2 写"没有" |

修订时新看到、不在裁定和复核意见里的：

- 性质表里"留下的消息保持原来的先后"在不规整的输入上不成立，复核方的日志里有这个计数，意见里没有单列。拆成了三行，各带前提（§4.4）。
- `pinned` 一侧在不规整的输入上还有一种现象：截断丢掉夹在调用和结果之间的消息以后，输入自己修复会删掉的结果反而被留下。C 族里 44 个（§4.4）。
- R11 改了之后，不规整的输入上还有一类请求会独自留着：tail 以一条没有配对的工具结果开头时 2e 留下的那一条。六个生成器里是 9、7、4、0、0、9 个。规则没有为它再改。
- 原型里去掉以后结果不变的地方从一处变成三处，另两处就是复核方的 X7、X10（§4.3）。

和第 3 版不同的数字：

- §4.4 改成六个生成器。前三个生成器上高于目标的结果少了 9、3、15 个，原因在该节的表里。
- §4.6 的表没有变；多了 R11 裁定前后对比的一段和反向代价的那张表。
- §5.2 的用例数从 35 变成 42，变异从 28 处变成 33 处。M1、M3、M5、M12、M14 至 M17、M25 转红的测试比第 3 版多，多出来的都在 T25 至 T30 里。
- §6.2 定稿原型的用时是这一版量的，145 秒和 104 秒，条数没有变。
- §3、§7 的数字没有变：定稿规则在那些探针上发出的视图和第 3 版的规则相同。

第 3 版的探针和输出留在原处，文件名以 `r3_` 开头。

## 11. 证据清单

### 11.1 第 4 版

目录和第 3 版是同一个，`/tmp/claude-0/-workspace-dataset-private-zhouwg-data-OmicsClaw/9885f10a-3799-4309-9dfb-0981c166422a/scratchpad/plan-0078-r3/`，第 4 版的文件以 `r4_` 开头。`r4_proto/` 是 `git archive 5daf5482` 重新导出的整棵树，`r4_apply_prototype.py` 只改了其中的 `omicsclaw/context/transcript.py`；`r4_mut/` 是变异用的副本，跑完后它的 `transcript.py` 和 `r4_proto/` 的逐字相同。基线仍用 `r3_base/`。第 3 版的原型 `r3_proto/` 和不带 2f 的 `r3_proto_no_n1/` 留着作对照。探针经 `r4_run.sh` 跑，`r4_common.py` 做和第 3 版同样的路径检查，每个输出的第一行是 `omicsclaw.__file__`。标"原型"的跑在 `r4_proto/` 上，其余跑在 `r3_base/` 上，规则由 `r4_rules.py` 提供。

| 脚本 | 输出 | 内容 |
|---|---|---|
| `r4_rules.py` | | 定稿规则 `final`；第 3 版的规则 `final_r3`；第 2 版的规则 `gplus_r2`；R11 改法的三行写法 `final_three_lines`；照 §4.1 的条文另写的 `from_text` |
| `r4_apply_prototype.py`、`r4_check_plan_block.py` | `r4_out_24_plan_block.log` | 把定稿规则写进 `r4_proto/`；核对 §4.3 的代码块和写进去的文本相同（原型） |
| `r4_probe_fuzz.py` | `r4_out_01_fuzz_own.log`、`r4_out_01_fuzz_rv3.log` | §4.4：三种写法、R11 改法的两种写法、对第 3 版规则的差别、性质表，六个生成器（原型） |
| `r4_probe_order.py` | `r4_out_33_order.log` | §4.4：C 族里带着输入自己修复会删掉的结果的 44 个截断结果（原型） |
| `r4_probe_reclassified.py` | `r4_out_34_reclassified.log` | §4.4：第三个生成器上高于目标的结果在两版规则下各属哪一类（原型） |
| `r4_probe_equivalent.py` | `r4_out_20_equivalent.log` | §4.3：去掉以后结果不变的三处（原型） |
| `r4_probe_scenarios.py`、`r4_probe_sequence.py`、`r4_probe_gate.py`、`r4_probe_assembly.py`，由 `r4_port_probes.py` 从第 3 版的探针生成 | `r4_out_02_scenarios.log`、`r4_out_06_sequence.log`，各带 `.json`；`r4_out_07_gate.log`；`r4_out_13_assembly.log` | §3.2 至 §3.4、§7、§5.2 的 T18：定稿规则和第 3 版的规则各占一行 |
| `r4_probe_edges.py` | `r4_out_04_edges.log` | §4.1、§4.2：E1 至 E13，逐条和第 3 版的规则比 |
| `r4_probe_tail.py` | `r4_out_05_tail.log` | §4.5、§4.6、R11：四条规则，四组对话 |
| `r4_probe_maxturns.py` | `r4_out_31_maxturns.log` | §4.6 最后一条：跑满轮数之后的会话重放 |
| `r4_test_proposed.py`、`r4_pytest.sh` | `r4_out_08_proposed_today.log`、`r4_out_09_proposed_prototype.log`、`r4_out_11_proposed_r3_rule.log`、`r4_out_11b_proposed_r2_rule.log`，各带一份 `.raw` | §5.2：T1 至 T17、T19 至 T30 |
| `r4_mutate.py`、`r4_mutation_table.py` | `r4_out_10_mutations.log` | §5.2：33 处变异和 EQ1 至 EQ3；后一个脚本把日志换成表里的测试编号 |
| `r4_reviewer_matrix.py` | `r4_out_21_reviewer_mutants.log` | §5.2：第 4 版的用例对第 2 轮审核方的 21 处变异 |
| `r3_eval_scratch.py`、`r4_eval.sh` | `r4_out_16_eval_prototype.log` | T18 在定稿原型上 |
| `r4_suite.sh` | `r4_out_17_prototype_layers.log`、`r4_out_18_prototype_guards.log` | §6.2：两条命令在定稿原型上 |
| `r4_probe_full_tier.py` | `r4_out_19_full_tier_final.log`、`r4_out_19_full_tier_today.log` | §8 末尾：FULL 档摘要成功和摘要失败两条路径，在定稿原型和基线上 |
| `r4_probe_duplicate_ids.py` | `r4_out_32_duplicate_ids.log`、`r4_out_32b_duplicate_ids_final.log` | §8 末尾：调用 id 相同时修复补的占位 |
| `r4_check_figures.py` | | 核对正文引用的主要数字都在输出里 |

复制来的：`r4_common.py` 是 `r3_common.py` 的副本，只放宽了路径检查；`r4_rv3_copy/rv3_common.py` 是复核方第 3 轮的生成器，只把路径检查换成这边的；`r4_rv3_copy/rv3_test_extra.py` 是复核方的候选测试，原样。`r4_plan_0080_tip.md` 和 `r4_plan_0080_f317fa37.md` 是从分支上取出来的计划 0080，核对 R11 里那一句用。`r4_plan_sec*.md`、`r4_edit_*.py`、`r4_extract_section.py`、`r4_splice_plan.py`、`r4_build_tail_sections.py` 是写这份文档用的草稿和脚本。`r4_wait_for.py`、`r4_find_message.py`、`r4_find_handback.py` 和两份 `r4_commit_message*.txt` 也在目录里，都不是证据：等后台的探针、回看派发的原文、存提交说明用的。

第 4 版没有重跑、正文仍引用第 3 版输出的：基线上的两条命令（`r3_out_29`、`r3_out_30`）；§1.2、§1.3、§1.4、§1.6 的现状（`r3_out_26`、`r3_out_27`、`r3_out_28`）；T18 的 `trigger_tokens` 量法和它在今天代码上的结果（`r3_out_12_assembly_measure.log`、`r3_out_14_eval_today.log`）；2f 相对第 2 版规则的差别（`r3_out_01_fuzz.log`、`r3_out_23_r2_to_r3.log`）；较早的交换都有答复时的对照（`r3_out_22_lone_request.log`）；FULL 档在基线上的重现（`r3_out_19_full_tier.log`）。这些说的是没有变的基线，或者没有变的那部分规则。

只读引用的第 3 轮复核方输出：`RV3/rv3_out1_fuzz.log`、`rv3_out3b_maxturns.log`、`rv3_out3d_today_lone.log`、`rv3_out4_mut_own2f.log`、`rv3_out4_mut_planner.log`、`rv3_out5_variant_layers.log`、`rv3_out5_variant_guards.log`、`rv3_out7_properties.log`、`rv3_out8_live_*.log`，以及它的 `rv3_live_lone_request.py` 和 `rv3_tests/rv3_test_extra.py`。

### 11.2 第 3 版

目录：`/tmp/claude-0/-workspace-dataset-private-zhouwg-data-OmicsClaw/9885f10a-3799-4309-9dfb-0981c166422a/scratchpad/plan-0078-r3/`。`r3_base/` 和 `r3_proto/` 是 `git archive 5daf5482` 导出的整棵树，`r3_apply_prototype.py` 只改了 `r3_proto/` 里的 `omicsclaw/context/transcript.py`。`r3_proto_no_n1/` 只有 `omicsclaw` 包，打的是不带 2f 的补丁。`r3_mut/` 是变异用的副本，跑完后它的 `transcript.py` 和 `r3_proto/` 的逐字相同。探针用 `/opt/conda/envs/rapids_singlecell/bin/python` 经 `r3_run.sh` 跑，`PYTHONPATH` 指向其中一个副本；`r3_common.py` 在 `omicsclaw.__file__` 不在该副本下时拒绝运行，每个输出的第一行是这个路径。标"原型"的跑在 `r3_proto/` 上，其余跑在 `r3_base/` 上，规则由 `r3_rules.py` 提供，需要经过 `compact()` 时临时换进去。

第 3 版把前两版里正文还在引用数字的探针都在这个目录里重跑了一遍。加上第 4 版重跑的，正文的数字除下面列出的三处以外都能在 `r3_` 或 `r4_` 开头的输出里找到。下表是第 3 版当时的清单；表里标着章节的探针，凡是 §11.1 有同名的 `r4_` 版本的，正文现在引用的是 `r4_` 的输出。

| 脚本 | 输出 | 内容 |
|---|---|---|
| `r3_rules.py` | | 定稿规则 `final`；不加 2f 的第 2 版规则 `gplus_r2`；照 §4.1 的条文另写的 `from_text`；2f 第 3 步只看 tail 第一条的读法；R11 里量的那种改法 |
| `r3_apply_prototype.py`、`r3_check_plan_block.py` | `r3_out_24_plan_block.log` | 把定稿规则写进暂存副本；核对 §4.3 的代码块和写进去的文本相同（原型） |
| `r3_probe_fuzz.py` | `r3_out_01_fuzz.log` | §4.4：三种写法、对审核方第 2 轮实现的比对、2f 改了什么、性质表（原型） |
| `r3_probe_r2_to_r3.py` | `r3_out_23_r2_to_r3.log` | §4.4：三个数和第 2 版不同的原因（原型） |
| `r3_probe_equivalent.py` | `r3_out_20_equivalent.log` | §4.3：去掉以后结果不变的那两行（原型） |
| `r3_probe_scenarios.py` | `r3_out_02_scenarios.log`、`.json` | §3.2、§3.3：十个规则，14 个场景加请求在 tail 里的 24 次 |
| `r3_probe_edges.py` | `r3_out_04_edges.log` | §4.1、§4.2：E1 至 E13 |
| `r3_probe_tail.py` | `r3_out_05_tail.log` | §1.5、§4.5、§4.6：tail 的消息被删和高于目标的频率，四组对话 |
| `r3_probe_sequence.py` | `r3_out_06_sequence.log`、`.json` | §3.4：三个逐次调用的序列 |
| `r3_probe_gate.py` | `r3_out_07_gate.log` | §7：闸门，10 个夹具 |
| `r3_test_proposed.py`、`r3_pytest.sh` | `r3_out_08_proposed_today.log`、`r3_out_09_proposed_prototype.log`、`r3_out_11_proposed_without_2f.log`，各带一份 `.raw` | §5.2：T1 至 T17、T19 至 T24 |
| `r3_mutate.py` | `r3_out_10_mutations.log` | §5.2：28 处变异，加一处不改结果的改动 |
| `r3_reviewer_matrix.py` | `r3_out_21_reviewer_mutants.log` | §5.2：第 3 版的用例对审核方的 21 处变异 |
| `r3_probe_m18.py` | `r3_out_25_m18.log` | §5.2：m18 什么时候分得开 |
| `r3_probe_assembly.py` | `r3_out_12_assembly_measure.log`、`r3_out_13_assembly.log` | §5.2：T18 的形状在每个方案下，以及 `trigger_tokens` 的量法 |
| `r3_eval_scratch.py`、`r3_eval.sh` | `r3_out_14_eval_today.log`、`r3_out_16_eval_prototype.log` | T18 的 pytest 形式 |
| `r3_suite.sh` | `r3_out_29_baseline_layers.log`、`r3_out_30_baseline_guards.log`、`r3_out_17_prototype_layers.log`、`r3_out_18_prototype_guards.log` | §6.2：两条命令在基线和定稿原型上 |
| `r3_probe_current.py` | `r3_out_26_current.log` | §1.2、§1.3：今天各函数留什么丢什么。第 1 版的探针原样重跑 |
| `r3_probe_existing_test.py` | `r3_out_27_existing_test.log` | §1.6：那条名字叫"最旧的先走"的测试。第 1 版的探针原样重跑 |
| `r3_eval_section_1_4.py`、`r3_eval.sh` | `r3_out_28_eval_section_1_4.log` | §1.4：经过真实装配的一次。第 1 版的用例原样重跑 |
| `r3_probe_full_tier.py` | `r3_out_19_full_tier.log` | §8 末尾：FULL 档摘要成功时删尾部工具结果 |
| `r3_probe_lone_request.py` | `r3_out_22_lone_request.log` | R11：请求独自留下的频率和成因 |

从前两版和审核方复制过来的：`r3_common.py`（原 `common.py`，只改了开头确认 `omicsclaw` 路径的几行）、`r3_strategies.py`（原 `strategies.py`）、`r3_r2_rules_copy.py`（原 `r2_rules.py`，只用其中照裁定字面写的 G），以及 `r3_rv_copy/` 里审核方的 `rv_common.py`、`rv_strategies.py`、`rv_probe7_fuzz.py`、`rv2_rules.py`、`rv2_plan_block.py`、`rv2_mutants.py`，内容没有改。`r3_plan_sec*.md`、`r3_splice_plan.py` 和 `r3_update_refs.py` 是写这份文档用的草稿和脚本。`r3_check_figures.py` 核对正文引用的主要数字都在这些输出里。

没有在第 3 版重跑、正文仍引用前两版输出的三处：

- §2 参考实现的 Go 运行（`out_02_reference_go.log`）。这次修订不涉及 harness9。
- §5.2 里"第 1 版的 D 上 5 红"（`r2_out_11_proposed_first_version_rule.log`）。
- §4.3、§4.4 里第 2 版对审核方第 1 轮实现、对裁定字面读法的比对（`r2_out_01_fuzz.log`）。那些比对的对象是不带 2f 的规则，结论是定性的，正文没有再引用其中的数字。

只读引用的审核方输出：第 1 轮的 `RV/rv_out3_assembly.log`、`rv_probe3_assembly.py`、`rv_out5_sequence.log`、`rv_out6_gate.log`、`rv_out8_edges.log`、`rv_out9_request_in_tail.log`、`rv_out10_parallel_tail.log`；第 2 轮的 `RV2/rv2_out1_fuzz.log`、`rv2_out3_proposed_tests_matrix.log`、`rv2_out5_n1.log`、`rv2_out9_boundary_other_paths.log`、`rv2_out10_plan_straddle_variant.log`。

### 11.3 第 1、2 版

目录：`/tmp/claude-0/-workspace-dataset-private-zhouwg-data-OmicsClaw/58ae2e35-0792-4055-933c-1cfb79c727a6/scratchpad/plan-0078/`，第 3 版没有动它。探针用 `/opt/conda/envs/rapids_singlecell/bin/python` 跑，`PYTHONPATH` 指向 `5daf5482` 的检出；标"原型"的指向暂存副本 `proto_g/`（`git archive 5daf5482` 导出的包和三个测试目录，`r2_apply_prototype.py` 只改了其中的 `transcript.py`）。`proto_full/` 是整棵树的导出，两条命令的基线和原型运行、T18 的 pytest 形式在里面跑，跑完后它的 `transcript.py` 已恢复成 `5daf5482` 的内容。`rv_copy/` 是审核方 `rv_strategies.py`、`rv_common.py`、`rv_probe7_fuzz.py` 的副本。

第 2 版，表里的章节号是第 2 版的：

| 脚本 | 输出 | 内容 |
|---|---|---|
| `r2_rules.py` | | G、G+ 的实现：第 2 版的规则、照裁定字面的两条、第二种写法、N1 的变体（第 3 版没有沿用） |
| `r2_apply_prototype.py` | | 把第 2 版的规则写进暂存副本 |
| `r2_probe_fuzz.py` | `r2_out_01_fuzz.log` | §4.4：三种写法、对审核方实现的比对、性质表（原型） |
| `r2_probe_reading.py` | `r2_out_03_reading.log` | §4.4：裁定字面和审核方代码的分歧在哪 |
| `r2_probe_scenarios.py` | `r2_out_02_scenarios.log`、`.json` | §3.2、§3.3：九个方案，14 个场景加请求在 tail 里的 24 次 |
| `r2_probe_edges.py` | `r2_out_04_edges.log` | §4.1、§4.2：E1 至 E12 |
| `r2_probe_tail.py` | `r2_out_05_tail.log` | §1.5 和第 2 版的 N1：tail 的消息被删的频率 |
| `r2_probe_sequence.py` | `r2_out_06_sequence.log`、`.json` | §3.4：三个逐次调用的序列 |
| `r2_probe_gate.py` | `r2_out_07_gate.log` | §7：闸门，10 个夹具 |
| `r2_test_proposed.py` | `r2_out_08_proposed_today.log`、`r2_out_09_proposed_prototype.log`、`r2_out_11_proposed_first_version_rule.log` | §5.2：T1 至 T17 |
| `r2_mutate.py` | `r2_out_10_mutations.log` | §5.2：第 2 版的 17 处变异 |
| `r2_probe_assembly.py` | `r2_out_12_assembly_measure.log`、`r2_out_13_assembly.log` | §5.2：T18 的形状在每个方案下 |
| `r2_eval_scratch.py` | `r2_out_14_eval_today.log`、`r2_out_16_eval_prototype.log` | T18 的 pytest 形式 |
| 现有测试 | `out_07_baseline_layers.log`、`r2_out_15_baseline_guards.log`、`r2_out_17_prototype_layers.log`、`r2_out_18_prototype_guards.log` | §6.2：两条命令的基线和 G+ 原型 |

第 1 版的：`common.py` 和 `strategies.py`（对话的搭法和方案 A 至 F 的实现）、`probe_01_current.py`（`out_01_current.log`）、`h9ref/internal/memory/zz_probe0078_test.go`（`out_02_reference_go.log`，§2）、`probe_07_existing_test.py`（`out_13_existing_test.log`）、`eval_plan0078_scratch.py`（`out_15_eval_today.log`）。

更早的只读引用：`.../scratchpad/review-gate/evidence/fit_direction.log`、`tiers.log`、`tiers2.log`。

## 12. 实施记录

2026-10-10 实施。分支 `feat/fallback-truncation-order` 从计划分支的 tip `fd12b9fc` 开出，第一步用 `--no-ff` 合入计划 0080 的实现 `f71977b4`，没有冲突，合并提交是 `48619b96`，之后只追加提交。本节说的"基线"都是 `48619b96` 这棵树，也就是合入 0080 之后、生产代码还没改的时候。

脚本和输出在 `/tmp/claude-0/-workspace-dataset-private-zhouwg-data-OmicsClaw/9885f10a-3799-4309-9dfb-0981c166422a/scratchpad/impl-0078/`，下面记作 `IMPL`。`IMPL/base/` 是 `git archive 48619b96` 导出的树，`IMPL/final/` 是 `git archive f2e66d8b`，`IMPL/mut/` 是变异用的副本（`git archive 3ab70f05`，跑完后它的 `transcript.py` 和分支上的逐字相同）。`IMPL/planner/` 和 `IMPL/reviewer/` 是计划方、复核方材料的副本，只改了其中两处路径检查（`patch_copies.py`）。探针经 `run.sh` 跑，`PYTHONPATH` 指向被测的树，每个输出的第一行是 `omicsclaw.__file__`。

### 12.1 提交

| 提交 | 内容 |
|---|---|
| `48619b96` | 合入 `f71977b4`（计划 0080 的实现） |
| `3ab70f05` | `fix(context): drop the oldest messages first when a compaction falls back`。`omicsclaw/context/transcript.py`：`fit_to_budget` 的循环、三个私有函数、docstring，模块 docstring 的一句。`omicsclaw/context/compaction.py` 的 `compact` 和 `omicsclaw/planning/injector.py` 的 `_gate_fires` 各改一段 docstring。测试在 `tests/context/test_transcript.py`、`tests/context/test_summary_gates.py`、`tests/entry/test_planning.py`、`tests/evals/dataset/test_compaction.py`，`tests/evals/test_dataset_floor.py` 的 `BASELINE` 从 30 调到 31 |
| `f2e66d8b` | `docs(context): describe the fallback truncation as it now works`。`docs/core-features/` 下的 `context-engineering.md`、`progressive-compactor.md`、`planning.md`、`eval.md`，以及 `docs/plans/0039-planning-layer.md`、`docs/plans/0055-nudge-and-closing-gate.md` 的补记 |
| 本节所在的提交 | 本节，§12.2 第 3、4 条列的正文修订，和 `progressive-compactor.md` §12 里一句话的措辞 |

`CHANGELOG.md` 没有写，留给合并时。`README.md`、`AGENTS.md`、`tests/entry/golden/` 没有动。`drop_unanswered_calls` 和 `repair_tool_pairs` 没有动。

### 12.2 与计划的差异，和复核留下的四件事

1. 生产代码照抄 §4.3 的原型。`check_same_code.py` 去掉 docstring 后比较四个函数的语法树，和 `r4_proto/` 里的原型相同。签名和 `__all__` 没有变。
2. 复核留下的第 1 件事，补一条测试钉"该留的时候留"。加了 `test_a_request_stays_while_a_later_call_of_its_exchange_does`，下面记作 T31，取自复核方的候选 `RV3/rv3_tests/r4_test_extra.py`：较早的交换三轮、没有答复，目标让截断停在第一条调用和它的结果之间，断言请求和后两轮都在。它在基线上是红的。复核方的变异 Y3（带调用的 assistant 消息也留不住请求）和 Y4（切点落在调用和结果之间时判定没有请求可留）在 §5.2 的 42 条用例上全绿，现在都红在 T31 上。
3. 复核留下的第 2 件事，`pinned` 一侧那句话。第 4 版写的是前缀停在一轮中间时"它的请求也可能留不住回复"，并说让 `pinned` 停在一轮的边界上就不会遇到。另写了探针 `probe_pinned_side.py` 核实：60,000 段规整的对话，`pinned` 取 0 到 14。把截断的结果按前缀停在哪里分三类：

   | 前缀停在哪里 | 截断的结果 | 带截断造成的占位 | 比输入大 | 前缀里有请求独自留着 |
   |---|---|---|---|---|
   | 一轮中间（最后一条不是 tool 的消息是带调用的 assistant 消息，head 以 tool 消息开头） | 30,255 | 30,255 | 243 | 0 |
   | 一条请求上 | 6,011 | 0 | 0 | 3,715 |
   | 别处 | 21,840 | 0 | 0 | 0 |

   独自留着的 3,715 条请求都是前缀的最后一条消息。复核方说得对：前缀停在一轮中间时那条调用就在前缀里，请求总有回复；独自留下的是前缀以请求结尾的情形，而这种前缀本来就停在一轮的边界上。所以这是两条限制，"停在一轮的边界上就不会遇到"只对第一条成立。`fit_to_budget` 的 docstring 和 `progressive-compactor.md` §12 按两条写，§12 一共加了五条而不是四条。正文的 R7 和 §5.3 改了，§5.3 里"它在 head 里的请求"那处笔误一并去掉。输出在 `out_pinned_side.log`。
4. 复核留下的第 3 件事，正文的几处文字：性质表最后一行和 §4.5 写明摘要除外；§10.2 那一行改成"owner 被问到哪些事另立项时没有选它"；§9.1 第 7 条写成 owner 裁定保留宽读法；E4c、E10 两行写成"最后一条 assistant 回复"；§0 第 5 条、§4.5、R5 的"以调用结尾"改成"停在一轮中间"；§5.3 对 CHANGELOG 条目的要求加上第三件事。另外状态行、编号行改成照实的说法，§6.3 末尾和 R2、R3 各加了一句指向 §12.5。裁定的意思没有动。
5. 复核留下的第 4 件事，T30 保留。
6. 测试文件里不写计划的编号，两份计划的编号都从 T1 起，靠函数名对应；函数名和 §5.2 的表相同。草稿里重复的夹具换成了几个共用的函数：T7、T8、T12 的并行调用用 `_wide`，T15 的普通字符串角色用 `_plain`，assistant 消息的文字因此从 "three at once" 变成 "3 at once" 这一类，断言没有变。T16 用了那个文件里现成的 `_budget_at`，比草稿的预算少 1 个 token。这些改写有没有改变测试钉住的东西，由变异核对：33 处变异转红的测试和 §5.2 的表逐项相同（§12.4）。
7. T18 的请求结尾从 `FIRST-0078`、`SECOND-0078` 改成 `FIRST-REQUEST`、`SECOND-REQUEST`。在基线上重新量了增长：第 14、15 次模型调用（下标 13、14）是 6,032 和 6,214，计划量到的是 6,031 和 6,213，`trigger_tokens=6150` 仍在两者之间，没有改（`out_t18_measure_repo.log`）。
8. §6.2 说 `r4_probe_tail.py` 重跑后 "today" 一行应当和 "final rule" 一行相同。那一行用的是旧循环的逐字副本 `RV.today_copy`，不随被测的树变，重跑它检验不到落地的函数。实施时在副本里加了一行 "landed"，用被测的树自己的 `fit_to_budget`，拿它和 "final rule" 比（`planner/r4_probe_tail_landed.py`）。另外三个探针的 "B today" 一行调的是被测的树的函数，照计划的说法用。
9. `fit_to_budget` 的 docstring 提到 entry 层时只写"entry 层用 `pinned=1` 盖一条 system 消息"，没有写 `entry/compaction.py` 这个路径。`omicsclaw/context` 是叶子层，现有的 docstring 都不指向上层的文件；文件名写在了 `progressive-compactor.md` §12。
10. `docs/core-features/planning.md` §13 第 3 条除了去掉 SOFT、FULL 降级截断那一种，还加了一句 §7 里的话：降级截断丢到本次交换自己的轮次时可见轮次变少，方向是少提醒。
11. §6.3 的会话有三处和计划写的不一样，都在 §12.5 说明：解释器换成 OmicsClaw 环境的；第二条请求后面加了一段背景文字；每行加了一个基因名。

### 12.3 先红后绿

新测试先落进 §5.2 指定的四个文件，在生产代码没改的时候跑（`out_red_first.log`）：§5.2 的 42 条用例 40 红 2 绿，绿的是 T4 和 T12，和计划一致；T31 红；T18 红在计划写的那 4 条断言上（第二次交换的请求、最新文件的标记、最旧文件的标记、占位）。合入 0080 不改变这些结果，它没有碰 `fit_to_budget`。

改了生产代码以后这四个文件加 `test_dataset_floor.py` 是 145 条全绿。新增的用例数：`test_transcript.py` 净增 26 个函数、29 条用例（28 个新函数里 2 个是替换，3 个函数各带两种角色写法），`test_summary_gates.py` 10 条，`tests/entry/test_planning.py` 2 条，脚本化 eval 1 条，合计 42 条。

### 12.4 验证

档位是 `SPEC.md` 第 3 档。§6.2 的两条命令，改前在 `IMPL/base/` 里跑，改后在 `IMPL/final/` 里跑，两棵树都是 `git archive` 导出的：

| 命令 | 基线 `48619b96` | 分支 `f2e66d8b` |
|---|---|---|
| 第一条（`tests/context tests/planning tests/engine tests/entry tests/evals`） | 2890 passed、11 skipped、26 deselected、3 xfailed，132 秒 | 2932 passed，其余相同，134 秒 |
| 第二条（各层的 layer 守卫、`tests/sdk` 的两个文件、`tests/test_*.py`） | 842 passed、14 skipped、1 deselected、1 xpassed，102 秒 | 相同，104 秒 |

第一条多出的 42 条就是 §12.3 的那 42 条。基线比计划里 `5daf5482` 上的 2842 多 48 条，是 0080 带来的。输出在 `out_suite_base_*.log` 和 `out_suite_final_*.log`。本节所在的提交只改这份文档，没有为它再跑。

计划 0080 的测试：它加的 31 个测试函数按名字选出来单独跑，48 条用例全绿；它们也在第一条命令里。

变异（`mutate.py`，`out_mutations.log`）。`r4_mutate.py` 的 33 处变异按原文本替换，全部适用，每一处都有新测试转红，转红的测试在 T1 至 T17、T19 至 T30 的范围里和 §5.2 的表逐项相同。EQ1 至 EQ3 没有测试转红。表之外：T18 在 M1、M6、M14 下转红，T31 在 M1、M6、M14、M15 下转红。复核方的七处：Y3、Y4 红在 T31；Y5 红在 T25 至 T27，Y6 红在 T25、T27；Y2、Y7 是复核方标明的等价写法，不红；Y1（对话中间的 system 消息也留不住请求）不红。Y1 没有补测试，理由和 §5.2 对 m16 的处理相同：引擎提交对话之前去掉 system 消息，entry 层的历史里不会有这种输入。这一句条文只有下面的随机比对在检验。

随机比对（`planner/r4_probe_fuzz.py` 的两部分，`out_fuzz_own3.log`、`out_fuzz_rv3.log`）。六个生成器，有 head 的对话 33,552、30,770、32,673、19,986、19,108、19,316 段，合计 155,405 段。落地的函数、`r4_rules.final`、照条文写的 `from_text` 三者的差异都是 0；一个条件的写法和三行写法的差异是 0；性质表 14 行的违反次数都是 0。截断的结果数（32,267、29,631、31,670、19,937、18,839、19,116）、高于目标的个数（5,366、6,325、8,427、436、3,222、2,772）和 §4.4 相同。

探针在落地的实现上重跑：

- `r4_probe_scenarios.py`：28 次加 24 次，"B today" 一行和定稿规则一行每个字段都相同（`out_probe_scenarios.log`，`compare_rows.py`）。
- `r4_probe_sequence.py`：三个序列的两行相同，可复用前缀 5%、4%、4%（`out_probe_sequence.log`）。
- `r4_probe_gate.py`：10 个夹具 160 次，误触发 0 次，请求被丢 0 次，数到的是 3、4、5、7（`out_probe_gate.log`）。
- `r4_probe_tail.py` 加了 "landed" 一行的副本：四组各 6,000 段，落地的函数和 `r4_rules.final` 的差异都是 0；tail 的消息被删 0 段，高于目标 315、1,188、411、2,000 段，和 §4.6 的表相同（`out_probe_tail.log`）。

没有跑的：

- 需要 fastapi 的 Desktop 用例（`tests/entry/test_desktop_*.py`）和 `tests/launch`。`CompactionRecord` 的字段和 COMPACTION 事件没有变。
- 全量测试，和 `Eval CI`。合并到 `main` 属于第 4 档，由 CI 跑。
- `tests/entry/golden/` 没有重录，system 提示和工具表没有变。
- §6.3 末尾建议的第二对会话（窗口再收紧一档）。
- Anthropic 后端。真实会话只用了 DeepSeek 的一个型号。
- `r4_probe_edges.py`、`r4_probe_maxturns.py`、`r4_probe_order.py`、`r4_probe_reclassified.py`、`r4_probe_equivalent.py`、`r4_probe_assembly.py`、`r4_reviewer_matrix.py` 没有重跑，§6.2 没有列它们；落地的代码和原型的语法树相同。

### 12.5 真实模型的会话

驱动是 `IMPL/live_pair.py`。照 §6.3 的做法：`.env` 的 provider（DeepSeek，`deepseek-v4-flash`）包进 `RecordingProvider`，交给一个带两条 `followups` 的 `Case`，`network=True`，`compaction=True`，`Headroom(target=Pressure.SOFT, trigger_call=7, trigger_tokens=5600)`，`config` 里 `summary_timeout_s=0.001`、`memory=False`，`run_case` 的 `timeout_s` 显式传 900。同一个脚本在 `IMPL/base/`（旧规则）和工作树（新规则，`3ab70f05` 之后 `transcript.py` 没有再改）上各跑。先用脚本化的模型把驱动调通（`out_live_scripted_*.log`）。

和 §6.3 写的不同的三处：

- 解释器用 `/opt/conda/envs/OmicsClaw/bin/python`。rapids_singlecell 环境没有 `openai` 包，第一次启动在发出请求之前就失败了，没有请求到达模型。OmicsClaw 环境里 `omicsclaw` 是指向主检出的 editable 安装，驱动开头断言 `omicsclaw.__file__` 在被测的树里。
- panel 文件每行多一个基因名，由 panel 号和行号算出来，这样第 1 个文件第 3 行的原文不能照着别的 panel 推出来。第 3 个 panel 的阈值是 0.40，其余是 0.25。每读一个文件对话增长约 835 token，结果本身低于 offload 的 1,000。
- 第二条请求后面有一段约 400 token 的背景说明。SOFT 的门槛要落在第一次交换最后一次调用和第二次交换第一次调用之间，这段文字把两者的间隔拉宽，不用事先知道模型每次推理写多长。

三条请求：读六个 panel 文件，说出哪一个不一样、为什么；读 `small.txt`，然后给出上一次答复里指出的那个 panel 的编号和阈值；把 `notes/m1.txt` 第 3 行原文抄出来。

跑之前定的作废条件：压缩记录里有 EMERGENCY、有 offload 或有写回；第二次交换之前就有降级的压缩；Runner 报失败。

一共跑了三次会话，都不作废：

| | 树 | 第一次交换怎么读的 | 主线模型调用 |
|---|---|---|---|
| `fix_1` | 新规则 | 一轮里并行发出六个 `read_file` | 6 |
| `base_1` | 旧规则 | 每轮一个 `read_file` | 11 |
| `fix_2` | 新规则 | 每轮一个 `read_file`，答复前多跑了一条 `bash` 里的 `grep` | 12 |

并行还是逐个读是模型自己定的，同一条请求三次里出现了两种。拿来比的一对是形状相同的 `base_1` 和 `fix_2`；`fix_1` 留作对并行形状的观察。主线调用合计 29 次。另有 12 次摘要调用在 1 毫秒后被取消，没有返回用量。

`base_1` 和 `fix_2` 各有 4 条压缩记录，都是 SOFT、`degraded` 非空、没有写回、没有 offload，落在第二、三次交换的四次调用上。`usable` 是 24,951 和 24,938，回退目标是 14,970 和 14,962。每条记录之后那次请求里的内容：

| | `base_1`，旧规则 | `fix_2`，新规则 |
|---|---|---|
| 第二次交换第 1 次调用 | `S U1 a1 t1* a5 t5 a6 t6 A1 U2`，17,975 降到 14,619 | `S U1 a5 t5 a6 t6 ab tb A1 U2`，17,991 降到 14,443 |
| 第二次交换第 2 次调用 | `S U1 a1 t1 a2 t2* a6 t6 A1 U2 as ts`，18,171 降到 14,815 | `S U1 a5 t5 a6 t6 ab tb A1 U2 as ts`，18,310 降到 14,762 |
| 第三次交换第 1 次调用 | `S U1 a1 t1 a2 t2 a3 t3* A1 U2 as ts A2 U3`，18,248 降到 14,891 | `S U1 a6 t6 ab tb A1 U2 as ts A2 U3`，18,514 降到 14,117 |
| 第三次交换第 2 次调用 | `S U1 a1 t1 a2 t2 a3 t3* as ts A2 U3 ar tr`，18,424 降到 14,228 | `S U1 a6 t6 ab tb A1 U2 as ts A2 U3 ar tr`，18,776 降到 14,379 |
| 当前请求在视图里 | 4 次都在 | 4 次都在 |
| 第一次交换的请求和答复 | 请求 4 次都在，答复 3 次；最后一次 `A1` 和 `U2` 都被丢了 | 4 次都在 |
| 占位结果 | 每次 1 条 | 没有 |
| 视图里的 panel 读取 | 5 和 6；1 和 6；1 和 2；1 和 2 | 5 和 6；5 和 6；6；6 和重读的第 1 个文件前 6 行 |

`as ts` 是读 `small.txt` 的那一轮，`ar tr` 是第三次交换里重读第 1 个文件的那一轮，`ab tb` 是模型在第一次交换末尾自己加的那条 `bash`，带 `*` 的是占位。

三问的结果，两边一样：

- 第一问都答第 3 个 panel，阈值 0.40，其余 0.25。这时还没有降级。
- 第二问都答对了编号和阈值，都没有重读 panel 文件。第 3 个 panel 的读取这时在两边的视图里都不在，靠的是上一次的答复。
- 第三问都先调 `read_file` 重读，再逐字答对，没有编造，也没有说看不到。`fix_2` 的视图里没有第 1 个文件，它读了前 6 行。`base_1` 的视图里第 1 个文件的全文还在，模型仍然读了第 3 行再答。所以"依赖旧工具结果的追问"在这个模型上分不出两条规则：内容在不在视图里，它都去读。

用量，`RecordingProvider.usage` 的合计和按调用拆开的数（`PerCall` 包在 provider 外面逐次记的，两者的和相同）：

| | `base_1` | `fix_2` | `fix_1` |
|---|---|---|---|
| 主线调用 | 11 | 12 | 6 |
| `input_tokens` | 199,563 | 219,999 | 111,663 |
| `cache_read_tokens` | 181,376 | 202,752 | 99,456 |
| `uncached_input_tokens` | 18,187 | 17,247 | 12,207 |
| 四次降级调用的 `input_tokens` | 72,260 | 70,906 | 75,636 |
| 四次降级调用的 `cache_read_tokens` | 64,640 | 64,256 | 73,728 |
| 四次降级调用的 `uncached_input_tokens` | 7,620 | 6,650 | 1,908 |

整次会话的合计不能直接比，调用次数不同是模型在第一次交换里的选择造成的。只看降级的四次调用，新旧规则读到缓存的 token 相近，新规则没读到缓存的还少一些。原因是这个后端上每次请求的大头是 system 提示加工具表，第一次调用的 `input_tokens` 是 14,558，这一段两条规则下都是稳定前缀；每次截掉的是三四千 token，第一条请求之后留下的会话部分按本仓库的估算是两三千 token。新规则下切点没有挪动的那次调用（第二次交换第 2 次、第三次交换第 2 次）几乎全部命中，切点挪了的那次只命中到 `U1` 为止。§4.6 的 5% 说的是会话部分，这对会话没有推翻它，只是降级只持续了四次调用，量不出它在总量里的份额。

`fix_1` 的并行形状：

- 第二次交换的两次调用，记录是 17,568 到 17,568、17,764 到 17,764，什么都没丢。第 1 次调用时那条调用和前两条结果在 head、后四条在 tail，第 2 次时前四条在 head、后两条在 tail。两次 2f 都把这一轮整轮留住，head 里再没有可丢的，视图比目标 14,962 高 17% 和 19%。最新一轮的工具结果都在。
- 第三次交换第 1 次调用，tail 换成了 `A1 U2 as ts A2 U3`，那一轮整个落在 head 里。最旧的是那条调用，它丢了以后六条结果在量的时候还算着，丢到第四条结果时候选不超过目标，修复再把剩下的两条孤儿删掉，视图是 `S U1 A1 U2 as ts A2 U3`，12,854 token，是目标的 86%。
- 模型重读了第 1 个文件的第 3 行，逐字答对。答复后面多了一段话：第一次的答复是"没有调用工具写出来的"，里面的内容"没有核实过"，建议把六个文件重读一遍再写方法部分。它把只剩请求和答复、中间的工具轮次都不在的那次交换，读成了自己当时没读文件。

不调用模型的重放（`replay_recorded.py`，`out_replay_*.log`）：从记录里重建每次调用之前的完整历史，交给两棵树的 `fit_to_budget`。在各自的树上，12 次降级调用重放出来的视图和当时发出去的逐条相同，token 数和压缩记录相同。交叉重放给出同一段历史在另一条规则下的视图：

- `fix_1` 的历史交给旧规则：第二次交换第 1 次调用是 `S A1 U2`，11 条剩 3 条，第一次交换的请求和六条结果都不在，system 之后的第一条是 assistant 消息；之后三次各带 4 条占位，最后一次 `A1` 和 `U2` 不在。
- `fix_2` 的历史交给旧规则：四次各带 1 条占位，最后一次 `A1` 和 `U2` 不在。
- `base_1` 的历史交给新规则：四次都是第 5、6 两个文件，没有占位，请求和答复都在。

这对会话能说明的和不能说明的。拿到新视图的模型三问都答对，没有编造，追问接得上。它没有显示新视图比旧视图更有用：这三问在旧视图下也都答对了。旧规则丢掉上一次交换的请求和答复、留下占位，这些在记录里都看得到，但这三问没有哪一问的答案因此出错。每条规则各一次会话，一个型号，R2 仍然是判断。

### 12.6 做的过程中看到的、计划没有写的

1. 较早的交换可以只剩请求和最终答复，中间的工具轮次全不在，比如 `S U1 A1 U2 ...`。这是规则要的结果（§3.2 的 S2、E7b、E10 都是这个形状）。`fix_1` 里模型把它读成"上一次的答复没读文件就写了"，并且告诉了用户。旧规则的视图里有占位，占位的文字写着 "the context was compacted"；新规则不造占位，视图里没有任何东西提示有内容被丢。样本只有这一次。要处理的话是在视图里留一句说明，那是另一件事，不在本计划里。
2. 并行调用的一轮在降级截断里是整轮去留的：被 2f 留住时一条不丢，结果高于目标（`fix_1` 里高 17%）；切点一到它的调用，整轮连同还没轮到的结果一起走，结果可以明显低于目标（`fix_1` 里是目标的 86%）。两种机制 §4.1 和 E6a 都写了，真实会话里这一步跳多大没有写。
3. 同一条请求，模型三次里一次并行读、两次逐个读。真实会话要凑成形状相同的一对，靠的是模型的选择；这次多跑了一次新规则的会话才凑齐。重放脚本可以不调用模型补上"同一段历史两条规则各发什么"。
4. 真实后端上这对会话没有显出前缀缓存的代价，数字在 §12.5。system 提示加工具表约 14,500 token，降级只持续了四次调用。更长的降级过程没有量。
5. 旧规则的视图里第 1 个文件全文还在的时候，模型也去重读了那一行。"内容不在视图里时模型怎么办"在这个模型上的答案是重读，而内容在的时候它也重读。
6. 对话中间的 system 消息按回复对待（§4.1 第 4 条最后一句）没有单元测试，见 §12.4 的 Y1。
7. 计划 0080 合入后，被截断的交换在开场清理之后可能以一轮工具调用结束、没有最终答复。T25 至 T27 和 T31 钉的就是这种形状的去留，没有为 0080 另加用例。
