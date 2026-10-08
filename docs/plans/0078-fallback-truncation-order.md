# 计划 0078：降级截断先丢什么（`fit_to_budget`）

**状态**：第 1 版（2026-10-08）。待独立审核和 owner 裁定。本计划没有改生产代码和测试。

**基线**：`main` 的 `5daf5482`，行号以它为准，实施时按符号名重新定位。参考实现 harness9 读的是本机 `/workspace/dataset/private/zhouwg_data/harness9`（`f92e839`），它的 `internal/memory/compaction.go` 与 owner 派发本计划时指的那份副本逐字节相同。

**编号**：方案 A 至 F，场景 S1 至 S10，测试 T1 至 T13，风险 R1 至 R9，待裁定问题 Q1 至 Q5。

**证据**：探针脚本和输出在 `/tmp/claude-0/-workspace-dataset-private-zhouwg-data-OmicsClaw/58ae2e35-0792-4055-933c-1cfb79c727a6/scratchpad/plan-0078/`，清单见 §10。正文里的 token 数都是 `estimate_messages_tokens` 的估算值，没有调用真实模型。

## 0. 摘要

1. `fit_to_budget` 保留 head 里最旧的一段，从靠近尾部的一端开始丢。它的 docstring 和两份功能文档写的是反方向。名字叫"最旧的先走"的那条测试，对话是 849 token，预算是 900，函数在第一个判断就原样返回，剥离循环没有执行。
2. 多交换会话里，本次交换的请求在 head 靠后的位置，会连同它前后的几条一起被丢，更早的交换反而留着。用脚本化 eval 的装配跑一次两次交换的会话，FULL 档摘要器超时，发给模型的是 system 消息加最近两轮工具往返，没有任何 user 消息。
3. 参考实现的代码是同一个方向。它自己的功能文档写的是"保留放得下的最大后缀"，和代码不一致。它的基准测试每个实例只有一个任务，`rest[0]` 就是任务，这个方向在那里正好保住任务。它的规划闸门在引擎里计数，不读历史，所以没有闸门那个后果。
4. 推荐方案 D：压缩摘要和对话里最后一条 user 消息不丢，其余从最旧的开始逐条丢，本次交换之前的交换整段去留。
5. 方案 D 放弃了降级期间的前缀缓存（单次长交换的探针里，可复用前缀从 77% 降到 5%），也放弃了"更早的交换只留一部分"。
6. 改动是 `omicsclaw/context/transcript.py` 的一个函数加一个私有辅助函数，几处 docstring，四份功能文档，12 条单元测试（其中 2 条替换现有测试）和 1 条脚本化 eval。按 `SPEC.md` 属于第 3 档。
7. 方案 D 落地后，SOFT、FULL 降级截断导致的闸门误触发不再发生。EMERGENCY 那一种不变，建议另开计划。
8. 需要 owner 裁定 5 条，见 §9。

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

`fit_to_budget` 先判断候选放不放得下，再修复配对，修复补的占位不计入。§3.2 的场景 S5 在 FULL 档就因此比目标多了 7 token（1240 对 1233）。

下面用标签表示消息：`S` 是 system 消息，`U<n>` 是第 n 次交换的用户请求，`a<n>.<k>` 和 `t<n>.<k>` 是第 n 次交换第 k 轮的 assistant 调用和工具结果，`A<n>` 是第 n 次交换的最终答复，`SUM` 是压缩摘要，带 `*` 的是修复补的占位。全部用 `pinned=1`、`min_tail=6`，和 `entry/compaction.py` 的 `build_compactor` 一致。

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

### 1.5 文字和代码的出入

- `transcript.py:201` 的 docstring 写 "Peel messages off the front"，`:203` 写 "The oldest compactible message goes first"，`:229` 写 "peeling the front off can orphan a tool result"。模块 docstring `:18` 也写 "peels them off the front"。代码丢的是 head 的后端。
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
- `cmd/swebench/runner.go:293-299` 把 `TokenBudgetCompactor` 直接当作基准测试的压缩器，预算是窗口的 55%，`MinTailMessages` 是 8。每个实例一个任务，`rest[0]` 就是任务。
- 产品入口 `cmd/harness9/main.go:425` 用 `ProgressiveCompactor`，`TokenBudgetCompactor` 只在摘要失败时兜底。`internal/engine/history.go:50-67` 每次运行加载整段会话历史再追加新的用户输入，所以产品路径上有多交换会话。
- 规划闸门是引擎里的计数器（`internal/engine/loop_phases.go:201-205` 的 `lc.turnsSincePlanWork`），不读历史。

据此的判断：

- 派发本计划时的问题描述说"这个不一致是从参考实现带过来的"。带过来的是代码的方向。参考实现循环上的注释没有说先丢最旧的，"off the front"和"the oldest goes first"是移植时写的。参考实现也有文字和代码的不一致，在它的功能文档里。
- 在它的基准测试里这个方向没有问题：`rest[:headEnd]` 一直留着 `rest[0]`，也就是任务。按它功能文档写的算法反而会先丢任务，而丢任务的后果它在 issue #117 里量过（连续 143 轮 EMERGENCY，任务失败）。
- 在它的产品路径上是同样的问题。用它基准测试的参数跑 S2，结果是 `S U1 a1.1 ... A1 U2 a3.2 t3.2 a3.3 ... t3.5`，本次请求 `U3` 被丢。闸门的后果在它那里不存在，因为计数不读历史。
- 这个方向是有意选的还是无意的，没有读到说明。提交记录和注释里没有提"保住任务"，任务锚点是 `d449cdd`（2026-09-15）才加到 `CompactForce` 上的，功能文档写的又是另一种算法，所以更像是无意的。这是推断。

## 3. 该丢什么

### 3.1 方案

| 方案 | 规则 |
|---|---|
| A | 照 docstring：head 里从最旧的开始丢，没有例外 |
| B | 代码不动，把 docstring、文档和测试名改成代码实际做的事 |
| C | 对话里最后一条 user 消息不丢，其余从最旧的开始丢 |
| D | C，加两条：压缩摘要消息不丢；本次交换之前的交换整段去留 |
| E | 对话里最后一条 user 消息不丢，其余沿用今天的方向（从靠近尾部的一端丢） |
| F | 直接用 EMERGENCY 的算法（`_emergency_survivors`），目标换成降级目标 |

"最后一条 user 消息"指对话里位置最靠后的 user 角色消息。进入持久历史的 user 消息只有两种：一次交换的用户输入，和压缩摘要（计划 0039 §9.3 核对过，这次重新 grep 了 `Message.user(` 和 `Role.USER`，结论相同）。所以它要么是本次交换的请求，要么是代替了这条请求的摘要，和规划闸门往回数时停下的是同一条。它在 tail 里时已经受 `min_tail` 保护，head 里不再额外保留任何请求。

"整段去留"的做法：候选视图在 pinned 之后的第一条如果是一条请求已经不在的回复（assistant 或 tool 消息），就不采用这个候选，接着往后丢。对话里一条 user 消息都没有时不适用这条，逐条丢。

下面把去掉"整段去留"的 D 记作 C+。

### 3.2 同一组对话上的结果

每个方案都替换进真实的 `compact()` 跑（探针进程里换掉 `omicsclaw.context.compaction.fit_to_budget` 这个模块属性），offload、定档、降级目标和记录都是生产代码。输出在 `out_03_alternatives.log`。

S1，单次长交换，SOFT 降级，目标 1129：

| 方案 | 发送的视图 | 丢掉 |
|---|---|---|
| A | `S a1.4 t1.4 ... a1.12 t1.12` | `U1` 和前 3 轮。请求没了 |
| B、E | `S U1 a1.1 ... a1.6 t1.6 a1.7 t1.7* a1.10 ... t1.12` | `t1.7` 和第 8、9 轮，补一条占位 |
| C、C+、D | `S U1 a1.4 t1.4 ... a1.12 t1.12` | 前 3 轮 |
| F | `S U1 a1.1 t1.1* a1.2 t1.2* a1.3 t1.3* a1.4 ... t1.12` | 前 3 轮的结果，补三条占位，1141 超过目标 |

S2，多交换会话，SOFT 降级，目标 1341：

| 方案 | 发送的视图 | 丢掉 |
|---|---|---|
| A、C、C+ | `S a1.4 t1.4 A1 U2 a2.1 ... A2 U3 a3.1 ... t3.5` | `U1` 和第一次交换的前 3 轮。视图以 assistant 消息开头 |
| B | `S U1 ... A1 U2 a2.1 ... a2.4 t2.4* a3.3 ... t3.5` | `t2.4 A2 U3 a3.1 t3.1 a3.2 t3.2`，补一条占位。请求没了 |
| D | `S U2 a2.1 ... A2 U3 a3.1 ... t3.5` | 整个第一次交换。1154，比目标少 187 |
| E | `S U1 ... A1 U2 a2.1 ... a2.4 t2.4* U3 a3.3 ... t3.5` | `t2.4 A2 a3.1 t3.1 a3.2 t3.2`，补一条占位 |
| F | `S U1 A1 U2 a2.1 ... A2 U3 a3.1 ... t3.5` | 第一次交换的 4 轮 |

10 个场景各跑 SOFT 和 FULL，共 20 次降级压缩的合计。10 个场景是：S1、S2；S3 较早的一次交换有 9 轮只读，本次 4 轮；S4 之前写回过摘要，之后来了新请求；S5 请求已被摘要代替；S6 请求在 tail 里；S7 并行工具调用；S8、S9 本次请求很大；S10 对话里没有 user 消息。

| 方案 | 请求丢失 | 摘要丢失 | 补的占位 | 修复后超目标 | 视图以回复开头 | 闸门提前触发 | 闸门该触发而没触发 | 保留 token 与目标之比 |
|---|---|---|---|---|---|---|---|---|
| A | 6 | 4 | 0 | 0 | 20 | 0 | 0 | 0.86 |
| B | 8 | 0 | 16 | 1 | 2 | 4 | 2 | 0.83 |
| C | 0 | 2 | 0 | 0 | 14 | 0 | 0 | 0.96 |
| C+ | 0 | 0 | 0 | 0 | 12 | 0 | 0 | 0.96 |
| D | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0.85 |
| E | 0 | 0 | 18 | 1 | 2 | 0 | 2 | 0.97 |
| F | 2 | 0 | 54 | 14 | 2 | 2 | 0 | 0.94 |

"视图以回复开头"那一列里 B、D、E、F 的 2 次都是 S10，对话里本来就没有 user 消息。

### 3.3 对四件事的影响

工具调用配对修复。从 head 后端丢（B、E）时，切点前面可能留下一条结果被丢的调用，修复给它补占位，20 次里 B 有 16 次、E 有 18 次出现占位。模型看到的是自己发过的调用加一句"结果不可用"。从最旧的开始丢（A、C、C+、D）时，切点后面可能留下调用已被丢的结果，修复把它删掉，不补任何东西，20 次里占位都是 0。F 的占位最多，原因见 §1.2 末尾。输入里本来就缺结果的调用，在任何方案下都仍由修复补占位。

提示前缀缓存。降级结果不写回，每次调用重新截。探针 `probe_04_sequence.py` 用真实的 `ProgressiveCompactor` 按引擎的方式逐次调用，摘要器一直抛错，统计每次降级调用发送的消息里，与上一次请求逐条相同的前缀占多少 token（`out_04_sequence.log`、`out_16_summary_figures.log`）：

| 方案 | 单次长交换，9 次降级调用 | 两次较早的交换后一次长交换，12 次降级调用 |
|---|---|---|
| B、E | 77% | 82%、81% |
| A、C、C+ | 3% 至 5% | 3% |
| D | 5% | 64% |
| F | 5% | 5% |

B、E 的切点在尾部一侧，head 前面那一段每次都一样。从最旧的开始丢时，历史每长一轮，切点就往后挪一轮，pinned 和请求之后的内容每次都变。D 在多交换会话里好一些，因为切点停在交换边界上不动，直到开始丢本次交换自己的轮次。探针里的 system 消息只有 51 token；真实部署里 system 提示和工具声明在任何方案下都是稳定前缀（§1.4 那次会话的第一次调用是 11,808 token），真实比例会比表里高，差别在会话部分。只有按前缀自动缓存的后端（OpenAI、DeepSeek）受影响。经 OpenAI 兼容网关访问 Anthropic 系模型时断点只打在 system 和最后一个工具上（`provider/openai_provider.py` 的 `apply_cache_breakpoints`），会话部分本来就不缓存；`anthropic_provider.py` 里没有打缓存断点的代码。

规划闸门。只要最后一条 user 消息一定在视图里，往回数就越不过它。C、C+、D、E 都满足。差别在数得准不准：D 留的是本次交换最近的轮次，数到的是"可见的本次交换轮次"；E 先丢本次交换自己的轮次，数得偏少，上面的多交换序列里 E 在第 12 次调用才触发，不压缩时和 D 都是第 9 次，B 是第 6 次。细节见 §7。

记忆提醒。`MemoryNudge` 数的是发送视图里最后一次写记忆之后的 assistant 消息数，是 `every` 的倍数就提醒（`context/nudge.py:59`）。任何截断都会改变这个数。持续降级时视图大小基本不变，这个数也基本不变，结果是要么一直不提醒，要么连续提醒。上面的多交换序列里，不压缩时在第 7、17 次调用提醒；B 和 E 在第 17、18、19 次连续提醒三次；D 一次也没有。这是现有行为的性质，各方案只是落点不同，本计划不处理。

### 3.4 `emergency_fit` 的"任务"

`_emergency_survivors` 把 pin 之后的第一条 `rest[0]` 当作任务，按位置取，无条件保留（`transcript.py:309`）。docstring 说它"carries the task"。它在三种历史形状里分别是：

| 历史的形状 | `rest[0]` 是什么 | 最后一条 user 消息是什么 |
|---|---|---|
| 新会话的单次交换 | 这次的请求 | 同一条 |
| 之前写回过摘要 | 摘要消息 | 之后来过新请求时是新请求，否则是摘要 |
| 多交换，从未写回摘要 | 第一次交换的请求 | 本次交换的请求 |

issue #117 的实测是第一种形状。第三种形状里 `rest[0]` 是一条过时的请求。

降级截断不该用 `rest[0]` 这个定义：S2 里它是 `U1`。方案 D 保留的"压缩摘要加最后一条 user 消息"，在前两种形状里包含了 `rest[0]`，在第三种里换成了本次请求。

EMERGENCY 自己要不要也换成这个定义，探针做了一个对照（`probe_06_gate.py`，`out_08_gate_today.log`）：在 `rest[0]` 之外把最后一条 user 消息也设为无条件保留。5 个场景里，今天有 4 个看不到本次请求、2 个闸门误触发；对照做法 5 个都看得到请求、没有误触发。代价出现在请求本身很大的 3 个场景：请求一条就用掉了目标，视图只剩 `S U1 U<n>`，正在进行的那一轮也被丢。这涉及写回的截断和计划 0030 的裁定 Q10，建议另开计划，见 Q4。

## 4. 推荐

### 4.1 规则

推荐方案 D。`fit_to_budget` 改成：

1. 已经放得下、没有 head、或者 head 里每一条都不能丢时，原样返回输入。
2. 不丢的消息：前 `pinned` 条，最后 `min_tail` 条，head 里的压缩摘要消息（`is_summary_message`），以及对话里最后一条 user 消息（它在 head 里时）。
3. head 里其余的消息从最旧的开始逐条丢，直到放得下。
4. 候选视图在 pinned 之后的第一条是 assistant 或 tool 消息、而且 head 里还有可丢的消息时，跳过这个候选接着丢。对话里没有任何 user 消息时不做这一步。
5. 能丢的都丢完还放不下，就返回 pinned、不丢的那几条和 tail，这时结果高于目标。
6. 最后 `repair_tool_pairs`。

暂存副本里的原型（`apply_prototype.py`）是这样写的，实施时可以另写，以 §5.2 的测试为准：

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
    has_request = any(m.role == Role.USER for m in (*head, *tail))
    candidate: tuple[Message, ...] = ()
    for cut in range(1, len(head) + 1):
        if cut - 1 in held:
            continue
        survivors = (*(m for i, m in enumerate(head[:cut]) if i in held), *head[cut:])
        candidate = (*kept, *survivors, *tail)
        if has_request and survivors and survivors[0].role != Role.USER:
            # A reply whose request is gone would open the view.
            continue
        if estimate_messages_tokens(candidate, counter=counter) <= usable:
            break
    return repair_tool_pairs(candidate)


def _held(head: tuple[Message, ...], tail: tuple[Message, ...]) -> frozenset[int]:
    """Positions in *head* that a truncation never drops.

    Compaction summaries, and the last user message of the conversation
    when it is in the head: the request the exchange in flight is
    answering, or the summary standing in for it.
    """
    held = {i for i, m in enumerate(head) if is_summary_message(m)}
    if not any(m.role == Role.USER for m in tail):
        for index in range(len(head) - 1, -1, -1):
            if head[index].role == Role.USER:
                held.add(index)
                break
    return frozenset(held)
```

角色比较用 `==`，理由和 `repair_tool_pairs` 的 docstring 写的一样：`Message` 不校验字段，角色可能是普通字符串。

### 4.2 理由

- 请求一定在视图里。随机生成 58,690 段对话、41,770 次真的截了的结果里，最后一条 user 消息丢失 0 次，摘要丢失 0 次（`out_05_prototype_fuzz.log`）。同一个探针里原型和另一份独立写的实现（`strategies.py` 的 `whole_exchanges`）逐条比对，差异 0 次。
- 留下的是最近的工作。SOFT 的正常路径摘要较旧的一半、原样留较新的一半，EMERGENCY 从新到旧装，今天只有降级截断留旧丢新。计划 0030 §5.2 说它取代的旧层是"最新后缀裁剪"，docstring 写"最旧的先走"，两处的本意都是留新的。
- 截断点不再造占位，模型不会看到自己发过、结果却写着"不可用"的调用。
- 视图以 user 消息开头。摘要成功的两条路径以摘要消息开头，EMERGENCY 以 `rest[0]` 开头，今天的降级截断除了 head 丢光的情况也以 `rest[0]` 开头。C、C+ 会让"以回复开头"在多交换会话里成为常态（20 次里 14 次、12 次）。Anthropic 的 Messages API 历来要求第一条消息是 user 角色，本仓库的 `anthropic_provider.py` 的 `encode_conversation` 不处理这种情况。这条约束这次没有对真实 API 核实，见 R1。
- 改动小：一个函数的循环，一个十行的辅助函数。暂存副本里跑 `tests/context tests/planning tests/engine tests/entry tests/evals`，基线 2842 passed，换上原型后 2841 passed、1 failed，红的是 §1.5 最后那一条（`out_07_baseline_layers.log`、`out_10_prototype_layers.log`）。

### 4.3 放弃了什么

- 降级期间的前缀缓存。单次长交换里每次降级调用要重新读会话部分的几乎全部，B 只重新读尾部。影响限于摘要器持续失败、压力又在 SOFT 以上的那些调用，每次这样的调用本来也要先付一次失败的摘要请求（超时的话是 90 秒）。
- 更早的交换不能只留一部分。上一次交换很长时，它要么整段留，要么整段丢，连同它的最终答复。20 次里 D 平均只用到目标的 85%，C+ 是 96%。§7 的闸门探针里有一个更极端的例子：目标 323，D 的视图只有 142。接着上一次交换往下问的请求（"按刚才的做法再处理 B 样本"）在降级调用里会看不到上一次的做法。今天的行为在这种情况下丢的是这条请求本身。
- `pinned=0` 而第一条是 system 消息的调用方，今天降级截断最后才丢 system 消息，D 最先丢它（`out_18_pinned_zero_today.log`、`out_19_pinned_zero_prototype.log`）。entry 层固定传 `pinned=1`，不受影响。这种调用方式在摘要成功时 system 消息本来就会被摘要掉，`compact()` 也已经在 `advisories` 里提醒，所以没有为它单独加保护。

## 5. 改动范围

### 5.1 代码

| 文件 | 改动 |
|---|---|
| `omicsclaw/context/transcript.py` | `fit_to_budget` 的循环按 §4.1 改；新增私有函数 `_held`；从 `.summary` 引入 `is_summary_message`（`summary.py` 只依赖 `offload.py` 和 schema，没有环）。`fit_to_budget` 的 docstring 重写：不丢什么、其余怎么丢、什么时候原样返回、结果可能高于目标、截断点只会留下孤儿结果而不会留下缺结果的调用。模块 docstring `:18` 的 "peels them off the front" 改掉。签名和 `__all__` 不变 |
| `omicsclaw/context/compaction.py` | 没有逻辑改动。`compact` 的 docstring 在讲回退的那一段加一句：回退保留压缩摘要和最后一条 user 消息 |
| `omicsclaw/planning/injector.py` | 只改 `_gate_fires` 的 docstring：`:187-190` 那一段现在把 SOFT、FULL 的回退和 EMERGENCY 并列，改成只剩 EMERGENCY |

如果审核方不接受 `transcript.py` 依赖 `summary.py`，另一种写法是给 `fit_to_budget` 加一个只能按关键字传的 `keep` 谓词，由 `fall_back` 传 `is_summary_message`。这会改公开函数的签名，本计划不推荐。

### 5.2 测试

T1 至 T12 写在 `test_plan0078_proposed.py` 里跑过：在今天的代码上 22 条用例（含参数化）20 红 2 绿，在原型上 22 条全绿（`out_11_proposed_tests_today.log`、`out_12_proposed_tests_prototype.log`）。对原型做了 12 处定点变异，每一处都至少让一条新测试转红，变异后文件按内容核对恢复（`out_17_mutations.log`）。

`tests/context/test_transcript.py`：

| 编号 | 测试 | 钉住的行为 | 今天 |
|---|---|---|---|
| T1 | `test_the_oldest_rounds_go_first_and_one_message_at_a_time`，替换 `test_the_oldest_compactible_message_goes_first_and_one_at_a_time` | 先断言夹具确实超预算；请求之后留下的是对话最新的一段；再多留一轮就放不下 | 红 |
| T2 | `test_the_request_in_flight_survives_a_truncation_that_drops_earlier_exchanges` | 三次交换，截断后本次请求还在 | 红 |
| T3 | `test_the_request_is_kept_even_when_it_alone_misses_the_target` | 请求本身大于目标时，结果是 pinned、请求和 tail，高于目标 | 红 |
| T4 | `test_a_compaction_summary_survives_a_truncation` | 摘要在请求之前时两条都留，摘要仍紧跟 system | 绿。今天因为方向相反本来就留着；它防的是新方向下丢摘要，去掉摘要保护的变异会让它转红 |
| T5 | `test_an_earlier_exchange_goes_whole_or_stays_whole` | 目标只容得下第一次交换的一半时，第一次交换整段丢，第二次整段留，视图以 user 消息开头 | 红 |
| T6 | `test_a_truncation_never_fabricates_a_tool_result`，替换 `test_peeling_the_head_strands_a_tool_call_and_the_repair_papers_over_it` | 并行三个调用，切点落在调用和结果之间：结果里没有占位，每条消息都是输入里的，孤儿结果随调用一起消失 | 红 |
| T7 | `test_a_conversation_with_no_user_message_is_still_peeled_one_message_at_a_time` | 没有 user 消息的对话逐条丢，留下的是最新的一段，比 tail 多 | 红 |
| T8 | `test_a_head_with_nothing_droppable_is_handed_back_untouched` | head 只有一条摘要时返回输入本身，不重建（夹具带一条孤儿结果，重建会把它删掉） | 红 |
| T9 | `test_the_request_inside_the_tail_needs_no_other_request_kept` | 请求在 tail 里时，head 里更早的请求不受保护 | 绿。今天因为整个 head 被丢而成立；"每条 user 消息都保留"的变异会让它转红 |
| T12 | `test_a_request_whose_role_arrives_as_a_plain_string_is_still_the_request` | 角色是普通字符串时规则照样生效；目标极小时结果是 pinned、请求和 tail | 红 |

`tests/context/test_summary_gates.py`：

| 编号 | 测试 | 钉住的行为 | 今天 |
|---|---|---|---|
| T10 | `test_every_degraded_compaction_keeps_the_request_in_view`，五种降级原因乘 SOFT、FULL 两档 | 经过 `compact()` 的每一种降级，本次请求都在结果里，记录里 `degraded` 非空、`summarized` 为 0 | 10 条全红 |

`tests/entry/test_planning.py`（entry 是同时接压缩器和注入器的那一层）：

| 编号 | 测试 | 钉住的行为 | 今天 |
|---|---|---|---|
| T11 | `test_the_gate_counts_only_this_exchange_after_a_degraded_compaction`，SOFT、FULL 两档 | 上一次交换 9 轮只读，本次 4 轮，降级压缩后的视图交给 `PlanInjector`，不带闸门文本 | 2 条全红 |

`tests/evals/dataset/test_compaction.py`：

| 编号 | 测试 | 钉住的行为 | 今天 |
|---|---|---|---|
| T13 | 脚本化 eval `compaction/degraded_truncation_keeps_the_request` | §1.4 那次会话：第 9 次模型调用的 user 消息里有第二次交换的请求，没有占位结果，system 消息在最前，有一条没写回的 FULL 降级记录 | 红。在暂存副本里跑过，今天红在"请求没有发出"，换上原型后通过（`out_15_eval_today.log`、`out_14_eval_prototype.log`） |

T13 的 `Headroom` 用的是 `target=FULL, trigger_call=8, trigger_tokens=5300`，是按这次量到的增长（17,174 减 11,808）往下取的；实施时 system 提示或工具表变了要重量。`tests/evals/test_dataset_floor.py` 的 `BASELINE` 从 30 调到 31。

现有测试里只有 §1.5 最后那一条会红，由 T6 替换。`test_a_history_that_fits_exactly_is_still_a_history_that_fits`、`test_every_transform_honours_an_injected_counter`、`test_every_way_a_summarizer_can_fail_degrades_and_leaves_a_trace` 等在原型上都通过，不用改。

### 5.3 文档

| 文件 | 改动 |
|---|---|
| `docs/core-features/context-engineering.md` | `:284` 表格里 `fit_to_budget` 那一行改成新规则 |
| `docs/core-features/progressive-compactor.md` | §5.3 最后一句（`:342`）改成新规则；§11 设计决策表加一行"降级截断留请求和摘要、留新丢旧"；§12 已知限制加两条：降级期间会话部分不命中前缀缓存，更早的交换整段去留 |
| `docs/core-features/planning.md` | §13 第 3 条：去掉 SOFT、FULL 降级截断那一种，只留 EMERGENCY |
| `docs/core-features/eval.md` | 用例数 30 改 31，§2.1 的表加 T13 |
| `docs/plans/0039-planning-layer.md` | §9.5 末尾加一条带日期的记录：SOFT、FULL 降级那一种由本计划关闭。原文不动 |
| `docs/plans/0078-fallback-truncation-order.md` | 加交付记录一节 |
| `CHANGELOG.md` | 合并时在顶部加一条 |

### 5.4 不改的

`emergency_fit`、`_emergency_survivors`、`plan_compaction`、`apply_compaction`、`_fallback_target`、`should_write_back`、`MemoryNudge`、`PlanInjector` 的逻辑都不动。`docs/plans/0030-context-assembly-layer.md` 不动，"逐条剥头"没有说哪一端。`README.md` 不动。`tests/entry/golden/` 是 system 提示和工具表，这次不会变，不用重录。

估计工作量：代码和测试半天，文档、变异检查和 §6.3 的那对会话半天。

## 6. 验证

### 6.1 档位

`SPEC.md` 第 3 档。改动在一个包里，但第 2 档要求不改变 agent 看到的内容，这次改的是降级压缩后发给模型的消息；`fit_to_budget` 又在 `omicsclaw.context` 的公开面上。两档都沾边时取高的一档。

### 6.2 要跑的

改动前在分支起点跑一次基线，改完再跑一次：

```
/opt/conda/envs/rapids_singlecell/bin/python -m pytest tests/context tests/planning tests/engine tests/entry tests/evals -q -p no:randomly
/opt/conda/envs/rapids_singlecell/bin/python -m pytest tests/*/test_*layer*.py tests/sdk/test_boundary.py tests/sdk/test_public_surface.py tests/test_*.py -q -p no:randomly
```

第一条在 `5daf5482` 的暂存副本上是 2842 passed、11 skipped、3 xfailed，约 130 秒。另外：

- 写测试时先在没改的代码上跑新测试，确认红绿和 §5.2 的表一致，再改代码。
- 变异检查：对落地的实现重做 §5.2 提到的 12 处变异（`mutate_prototype.py` 改一下路径可以直接用），每处至少一条测试转红。
- 探针 `probe_03_alternatives.py`、`probe_04_sequence.py`、`probe_06_gate.py` 在落地的实现上重跑，"B status quo"那一行应当和"D"那一行相同。
- 合并到 `main` 属于第 4 档，由 `Eval CI` 跑。

需要 fastapi 的 Desktop 用例不单独跑：`CompactionRecord` 的字段和 COMPACTION 事件都没变。汇报时按"没跑"写。

### 6.3 真实模型的会话

保留什么、丢什么是确定性的，上面的测试和 eval 已经覆盖。真实模型的会话用来看测试看不到的一件事：拿到新视图的模型是否接着做本次请求。这是行为改动，建议做一对会话作为验收的一部分，同一组提示在 `main` 和修复分支上各跑一次。

构造降级截断的办法：DeepSeek 的窗口在 `_model_limits.py` 里是 1M，命令行上没有缩小窗口的开关，自然压力到不了 SOFT。写一个驱动脚本，照 `omicsclaw/evals/runner.py` 的 `arun_case` 的做法装配：

1. 用 `.env` 的 provider 调 `build_app`，然后 `dataclasses.replace(app, budget=..., summarizer=...)`。`budget` 用 `headroom_budget(baseline, tool_tokens, Headroom(target=Pressure.FULL, ...))` 算，`baseline` 取第一次调用的估算 token；`summarizer` 换成一个 `summarize` 直接返回 `""` 的对象。
2. 工作区放 §1.4 用的三个文件。第一次交换让模型读小文件并复述；第二次交换让它读两张表并回答一个只能从表里得到的问题（比如两张表第 3 行的基因名）。
3. 用 `review-gate/scripts/spy/sitecustomize.py` 那样的线上记录抓每次请求体，压缩记录看 `<workspace>/.omicsclaw/compaction_records/`。

判断：出现 `degraded` 非空、`written_back` 为假的 FULL 记录之后的那次请求里，`main` 上没有第二次交换的请求，修复分支上有；最终答复是否回答了第二次交换的问题。真实模型每次走的轮次不同，窗口可能要调一两次才落在 FULL。

这一对会话本计划没有做。

## 7. 对规划闸门残留误触发的影响

SOFT、FULL 降级那一种，方案 D 落地后不再发生。对话里最后一条 user 消息要么在 tail 里，要么被规则留在 head 里，`_gate_fires` 往回数一定先遇到它。

- 计划 0039 §9.5 的场景（上一次交换 9 轮只读加一条答复，本次 3、4、5、7 轮，两档，摘要器抛错或返回空串）共 16 次：今天 16 次都误触发，数到 12 或 13 轮；原型上 0 次，数到的正好是 3、4、5、7（`out_08_gate_today.log`、`out_09_gate_prototype.log`）。§9.5 记的是 3 轮时数到 7、不触发，这次的夹具工具结果更短，3 轮时也触发了；两次都是计数越过了本次请求。
- 逐次调用的多交换序列里，今天闸门在第 6 次调用触发，原型和不压缩时一样在第 9 次（§3.3）。

降级截断之后计数仍可能偏少：丢到本次交换自己的轮次时，可见轮次少于实际轮次，凑不满 `gate_turns` 就不触发。这是计划 0039 §4.4 已经接受的"压缩后可见轮次不足"，方向是少提醒。

有两种情况计数仍会数进更早的交换，都不是降级截断造成的：库调用方用 `run_turn(app, history)` 而不给 `user_text`（计划 0039 §9.3 按同一次请求算）；之前某次 EMERGENCY 已经把请求从历史里丢掉并写回，之后的降级截断恢复不了它。

EMERGENCY 那一种不变。EMERGENCY 不经过 `fit_to_budget`。探针的 5 个场景里今天有 2 个误触发：本次请求很大、前面是一次 9 轮只读的交换时数到 11；本次请求中等大小（300 token）、本次交换的结果很大时，贪心走到请求时剩余预算不够，请求被跳过，更旧更小的消息还放得下，数到 14（实际 6）。原型上这两个结果相同。§3.4 的对照做法能消掉它们，是否采用见 Q4。

## 8. 风险和没验证的部分

- R1 Anthropic 首条消息的约束没有核实。"整段去留"的理由之一是避免视图以 assistant 消息开头被 Anthropic 原生接口拒绝。这是按该接口历来的规则写的，这次没有对真实 API 发请求确认。今天的代码在 head 丢光时也会发出这种视图（§1.4），仓库里没有处理它的地方，也没有看到相关的失败记录；日常用的是 DeepSeek。
- R2 "留新的比留旧的有用"是判断，没有真实模型的对比。§6.3 的那对会话只看"请求在不在、答没答"，不比较留新和留旧。
- R3 前缀缓存的数字来自探针里按消息逐条比较的前缀，system 消息只有 51 token。真实后端的缓存命中没有量。
- R4 更早的交换整段丢，视图可能明显低于目标，模型在这一次调用里看不到上一次交换的答复。
- R5 请求本身很大时，不丢的那几条加 tail 就可能高于降级目标。它不会高于 `usable`：能走到回退的对话压力低于 EMERGENCY，总量低于 `usable × 0.95`。随机探针里 41,770 次截断有 6,609 次高于目标，全部是这种情况。
- R6 tail 的第一条可能是回复。head 丢光、请求又在 tail 中间时，视图仍以 assistant 消息开头。`min_tail` 条不动是现有约定，本计划不改。
- R7 `fit_to_budget` 在 `omicsclaw.context` 的公开面上，名字和签名不变，行为变了。仓库内只有一个调用方；仓库外有没有调用方不知道。`pinned=0` 的调用方见 §4.3。
- R8 合并冲突。本地还没合并进 `main` 的分支（`feat/ask-user`、`feat/ask-user-r3`、`feat/subagent-usage-desktop`）相对 `main` 没有改 §5 列的文件，在 `tests/evals` 下只动了 `test_runner.py`。它们如果新增脚本化 eval，`BASELINE` 和 `docs/core-features/eval.md` 的用例数要以合并时的数字为准。
- R9 Desktop 和 channel 没有跑。它们和 CLI 一样经 `SessionRegistry` 到 `TurnRunner`，T13 覆盖的是这一段共用路径。

这次顺带看到、不在本计划范围里的两件事，见 Q5：

- 持续降级时 `MemoryNudge` 可能连续提醒。今天的代码在多交换序列的第 17、18、19 次调用连续提醒了三次（§3.3）。
- `_emergency_survivors` 会留下结果被跳过的调用消息，每次补 3 到 5 条占位，补完之后可能高于它自己的目标（§1.2、§1.3）。

## 9. 需要 owner 裁定的问题

Q1 用哪个方案。D 留请求和摘要、留新丢旧、更早的交换整段去留，代价是 §4.3 的三条。E 只加"最后一条 user 消息不丢"，方向不变，改动最小，前缀缓存不受影响，但仍然先丢本次交换自己的轮次、截断点仍然造占位（20 次里 18 次）、闸门偏晚。B 只改文字，§1.3、§1.4 的后果都还在。建议 D。

Q2 要不要"更早的交换整段去留"这一条。要：视图不以回复开头，更早的交换不会只剩没有请求的半截，多交换会话里切点稳定（前缀复用 64% 对 3%）。不要（即 C+）：多留一些旧内容（用到目标的 96% 对 85%），20 次里有 12 次视图以回复开头。它防的是 R1，那条约束没有核实；旁路是 R6。如果只在 OpenAI 兼容后端上用，可以不要。建议要。

Q3 行为改动要不要等真实会话对比之后再落地。一种做法是代码、测试、文档一起改，§6.3 的那对会话作为验收的一部分。另一种是先只落方案 B 的文字修正，把 docstring、两份功能文档和那条测试改成今天代码的实际行为，行为改动等会话对比之后再做。后者要把文字改两遍。建议一起改。

Q4 EMERGENCY 的任务锚点要不要改成同一个定义。不改：`docs/core-features/planning.md` §13 第 3 条留着 EMERGENCY 那一种。改：§3.4 的对照做法能消掉探针里的误触发，但请求很大时视图只剩两条 user 消息，而且这是写回的截断，动的是计划 0030 的裁定 Q10。建议这次不改，另开一份计划，把 Q5 的第二件事一起看。

Q5 §8 末尾两件范围外的事要不要另行处理。建议 `MemoryNudge` 那一件记进计划 0055 §10.6 的已知行为；`_emergency_survivors` 那一件并入 Q4 的后续计划。

## 10. 证据清单

目录：`/tmp/claude-0/-workspace-dataset-private-zhouwg-data-OmicsClaw/58ae2e35-0792-4055-933c-1cfb79c727a6/scratchpad/plan-0078/`。探针用 `/opt/conda/envs/rapids_singlecell/bin/python` 跑，`PYTHONPATH` 指向 `5daf5482` 的检出；标"原型"的指向暂存副本 `proto/`（`git archive 5daf5482` 导出的包和三个测试目录，`apply_prototype.py` 只改了其中的 `transcript.py`）。`proto_full/` 是整棵树的导出，五个测试目录的两次运行和 T13 在里面跑，跑完后它的 `transcript.py` 已恢复成 `5daf5482` 的内容。

| 脚本 | 输出 | 内容 |
|---|---|---|
| `probe_01_current.py` | `out_01_current.log` | §1.2、§1.3：今天各档、各种降级原因的视图 |
| `h9ref/internal/memory/zz_probe0078_test.go` | `out_02_reference_go.log` | §2：参考实现源码副本上的同样两段对话 |
| `probe_03_alternatives.py`、`strategies.py` | `out_03_alternatives.log`、`.json` | §3.2：六个方案加 C+，10 个场景，两档 |
| `probe_04_sequence.py` | `out_04_sequence.log`、`.json` | §3.3：逐次调用的前缀复用、闸门、记忆提醒 |
| `summarize_outputs.py` | `out_16_summary_figures.log` | §3 表里的合计数字 |
| `probe_05_prototype_fuzz.py` | `out_05_prototype_fuzz.log` | §4.2：原型与独立实现的随机比对（原型） |
| `probe_06_gate.py` | `out_08_gate_today.log`、`out_09_gate_prototype.log` | §3.4、§7：闸门，今天和原型 |
| `probe_07_existing_test.py` | `out_13_existing_test.log` | §1.5：现有那条方向测试没有进循环 |
| `probe_08_pinned_zero.py` | `out_18_pinned_zero_today.log`、`out_19_pinned_zero_prototype.log` | §4.3：`pinned=0` 时的 system 消息 |
| `test_plan0078_proposed.py` | `out_11_proposed_tests_today.log`、`out_12_proposed_tests_prototype.log` | §5.2：T1 至 T12 |
| `mutate_prototype.py` | `out_17_mutations.log` | §5.2：12 处变异 |
| `eval_plan0078_scratch.py` | `out_15_eval_today.log`、`out_14_eval_prototype.log` | §1.4、T13 |
| 现有测试 | `out_07_baseline_layers.log`、`out_10_prototype_layers.log`、`out_06_prototype_existing_tests.log` | §4.2：五个测试目录的基线与原型 |

只读引用的既有证据：`.../scratchpad/review-gate/evidence/fit_direction.log`、`tiers.log`、`tiers2.log`，脚本在同级的 `scripts/`。它们的结论和这次重跑的一致。
