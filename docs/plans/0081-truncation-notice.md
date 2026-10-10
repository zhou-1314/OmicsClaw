# 计划 0081：回复被输出上限截断时，CLI 显示一句话

状态：定稿，第 2 版（2026-10-09）。第 1 版（提交 `83c245b9`）经独立审核，结论是"可以交 owner 批准"。owner 2026-10-09 批准了计划并作了裁定（§9）。这一版把裁定和审核意见并进正文，没有再审一轮。实现还没有派发：按裁定要等 `feat/ask-user-r4` 和计划 0080 的实现都合入 `main`，从合入后的 `main` 开分支。本计划没有改生产代码和测试。

基线：行号以 `main` 的 `5daf5482` 为准。实现开工时的 `main` 会比它新，按符号名重新定位，三处改动在合并后的树上落在哪里见 §10。

范围：只做"截断时 CLI 显示一句"。只是显示，不碰给模型看的文字，不碰 Desktop 的线上契约。

证据：探针、原型、草稿测试和输出在 `/tmp/claude-0/-workspace-dataset-private-zhouwg-data-OmicsClaw/9885f10a-3799-4309-9dfb-0981c166422a/scratchpad/plan-0081/`，下面记作 `SCR`，第 2 版新做的以 `r2_` 开头。审核方的脚本和输出在同级的 `review-plan-0081/`，记作 `RV81`，只读引用。模型都是脚本化的替身，两版都没有对真实模型发请求。计划 0080 的事实写作"0080 §x"，指它的定稿第 3 版（`fe684b26`）。清单见 §12，各版之间改了什么见 §11。

## 0. 摘要

1. 截断的信号今天已经在 CLI 手里。交换结束后 `TurnHandle.outcome.result.stop_reason` 是 `truncated`，CLI 等到了这个 handle，但没有读这个字段，所以屏幕上什么都没有。
2. 做法：`Repl._drive` 在交换结束后读一次 handle 上的结果，是截断就打印一行黄字。改动只在 `omicsclaw/entry/cli/_repl.py`，原型新增 52 行、改 1 行。不给交换结果或事件帧加字段，Desktop 和 Channel 的代码不经过这里。
3. 这句话有两种形式，看被截断的那条回复带不带工具调用，原文是 owner 裁定的（§3）。两种形式都以 `The reply was cut off at the output limit` 开头，这个开头是脚本可以匹配的固定文字。
4. REPL 和一次性执行走同一段代码，显示同一行。一次性执行时它在标准输出上，退出码不变。子代理被截断、摘要调用被截断都不另外显示（§4）。
5. 草稿测试 14 条（19 个用例）：在今天的代码上 15 红 4 绿，在原型上全绿，在"`main` 加两条在办分支加原型"合并出来的树上也全绿。23 处定点变异里 22 处有测试转红，剩下 1 处决定不钉（§6.2）。按 `SPEC.md` 属于第 2 档。
6. 合并的硬条件：0080 的实现先合入 `main`。给实现方的要点集中在 §10。

## 1. 现状

### 1.1 信号走到了哪里

| 环节 | 位置 | 截断的信号 |
|---|---|---|
| provider | `Completion.finish_reason`（`provider/base.py:94`），流式是 `StreamChunk.finish_reason`（`schema/stream.py:64`） | 厂商的原词，`length` 或 `max_tokens` |
| 引擎 | `_stop_reason_for`（`engine/loop.py:700-721`），`_TRUNCATING_FINISH_REASONS`（`:93`） | 判成 `StopReason.TRUNCATED`，不执行工具（`:438-452`），写进 `RunResult.stop_reason`（`engine/types.py:155`），随 `DONE` 事件交出（`loop.py:454-461`） |
| entry 的事件帧 | `TurnEvent.from_engine`（`entry/events.py:323-325`） | `DONE` 不转成帧。`EXCHANGE_END` 只带 `terminal` 和 `error`（`:470-487`），`TurnRunner.run` 对有结果的交换一律写 `converged`（`entry/turn.py:583`）。帧里没有这个信号 |
| entry 的结果 | `_outcome`（`turn.py:317-338`）把 `RunResult` 放进 `TurnOutcome.result`；`SessionRegistry._attempt` 用 `handle._settle(terminal, error, outcome)` 交给 handle（`entry/session.py:822`，`turn.py:934-946`） | 在这里：`handle.outcome.result.stop_reason` |
| 遥测 | `observability/scope.py:594` | 写进 `agent.stop_reason`，bench 读的是它（`bench/adapters/omicsclaw.py:298`） |
| CLI | `Repl._drive`（`entry/cli/_repl.py:1100-1120`） | `await handle.wait()`（`:1115`）之后只读了 `handle.delegated`（`:1116`），没有读 `outcome` |

所以没有哪一层把信号丢了。事件帧按设计不带它，持有 handle 的界面在 `wait()` 之后都拿得到，CLI 只是没有读。`omicsclaw/entry` 里读 `StopReason.TRUNCATED` 的只有子代理的 `_conclusion`（`entry/subagent.py:471`）。

### 1.2 CLI 在哪里渲染一次交换的结束

REPL：`Repl.ask`（`_repl.py:1081-1098`）提交后进 `_drive`。`_drive` 先调 `_pump`（`:1122-1245`）把帧画到屏幕上，再 `handle.wait()`，最后在 `finally` 里用 `_reap_asking` 收掉还开着的审批提示。`_pump` 遇到 `EXCHANGE_END` 时，`converged` 不打印（`:1207-1213`），`cancelled` 和 `failed` 打印 `TextRenderer` 给的 `Cancelled.` 或 `Failed: <类型名>`（`entry/render.py:415-423`），帧流结束后补一个空行（`_repl.py:1238-1245`）。`_pump` 手里只有帧；`_drive` 在 `wait()` 之后手里有整个 `TurnOutcome`。

`_drive` 返回后读结果再打印一行，已经有先例：`/compact` 读 `handle.outcome.compaction`，打印压缩的结论（`_compact`，`_repl.py:958-961`）。

一次性执行：`run_once`（`_repl.py:1869-1897`）建一个 `Repl` 调 `ask`，走同一个 `_drive`。屏幕是 `Screen()`（`launch/_surfaces.py:759`），即标准输出上的 `rich` 控制台（`entry/cli/_screen.py:97-98`）。退出码只看 `handle.terminal == "converged"`（`_surfaces.py:772-773`），所以被截断的交换退出码是 0。

### 1.3 今天屏幕上有什么

脚本化的模型报 `finish_reason="length"`，经真实的装配和 `SessionRegistry`，四种形状：只有文字、文字加一个参数被截的 `write_file`、没有文字只有被截的调用、第二次模型调用被截。三条路径各跑一遍：REPL 和 `run_once` 写进缓冲（`SCR/logs/out_01_shapes_base.log`），真实的 `oc cli -- --prompt` 进程（`out_02_oneshot_base.log`），pty 里的交互式 REPL（`out_09_repl_pty_base.log`）。

每一种形状 handle 上都是 `terminal=converged stop_reason=truncated`，屏幕上没有一行提到截断。没有文字的那一种，屏幕上除了模型的推理（开着显示时，`_repl.py:1247-1251`）只有 `Context: …` 和 `Turn 1 done, …` 两行。审核方另跑了一遍，结果相同（`RV81/logs/facts_base.log`）。

## 2. 做法

### 2.1 放法

owner 裁定用放法 A：CLI 在交换结束后自己读 `handle.outcome.result.stop_reason` 并打印，只改 `omicsclaw/entry/cli/_repl.py`，不加跨层字段。

第 1 版比过另外两种，都没有选。B 是给 `EXCHANGE_END` 帧加 `stop_reason`，由 `TextRenderer._terminal_line` 出这一行：要动 CLI、Channel、Desktop 共用的 `TurnEvent`，`to_wire`（`render.py:502-511`）和 `desktop_terminal_frames`（`entry/desktop/turn_observation.py:252-272`）都得补测试证明新字段没有上线，换来的同样是 CLI 上的一行，而 Channel 的 `_pump_reply` 在渲染之前就跳过了 `converged` 的 `EXCHANGE_END`（`entry/channel/runtime.py:604-610`），仍然不会显示。C 是给 `Terminal` 加一个值：`terminal` 在 Desktop 线上（`render.py:505`），还决定历史提交不提交（`entry/session.py:814`）、一次性执行的退出码（`_surfaces.py:772`）、Channel 发不发回答（`runtime.py:650`），改它是改契约。以后 Desktop 或 Channel 要做这件事时再看 B。

### 2.2 规则

实现以这九条为准。

1. 只有 `outcome.result.stop_reason is StopReason.TRUNCATED` 时出这一行。`outcome` 是 `None`（交换被取消或失败）、`CONVERGED`、`MAX_TURNS` 都不出。
2. 用哪种形式只看被截断的那条回复，也就是 `outcome.result.final_message`。引擎先把被截断的回复追加进轨迹再判定（`loop.py:436-438`），所以它是轨迹的最后一条。它带 `tool_calls` 就用第二种形式，否则用第一种。同一次交换里更早的模型调用带过的工具调用已经执行了，不看也不列。
3. 第二种形式里每个调用列一个名字，按模型写的顺序，用 `, ` 隔开。参数完整的调用也列，同名的调用不合并。
4. 名字过 `inert_line`，再去掉首尾空白。空的名字显示成 `?`。名字不设长度上限。
5. 这一行用 `Text(notice, style="yellow")` 交给 `self._screen.print`，不拼成 markup 字符串。
6. 打印在 `_drive` 的 `finally` 之后、`return handle` 之前，一次交换最多一次。它后面不补空行。
7. 两种形式的原文是 §3.1 的两句，都以 `The reply was cut off at the output limit` 开头。
8. `_drive` 因异常离开时不打印。
9. 不改 `terminal`、退出码、事件帧、历史和发给模型的任何内容。

`/compact` 也走 `_drive`，它的结果写死是 `CONVERGED`（`turn.py:709`），按规则 1 不会出这一行。

### 2.3 代码

原型的完整 diff 在 `SCR/r2_prototype.diff`。三处改动如下，可以照抄，也可以另写。

import 段加两个名字。entry 可以 import 引擎（`tests/entry/test_entry_is_the_top_layer.py`），`entry/turn.py` 和 `entry/subagent.py` 已经这样做；`omicsclaw.entry.cli` 的公开面不变。

```python
from omicsclaw.engine import StopReason
from omicsclaw.entry.turn import TurnHandle, TurnOutcome
```

`_compaction_verdict` 之后、`class Repl` 之前：

```python
_CUT_OFF_NOTICE = (
    "The reply was cut off at the output limit and is incomplete. "
    "Ask for a shorter answer, or for it in parts."
)
"""Printed under a reply the output limit cut off that held no tool call."""

_CUT_OFF_CALLS_NOTICE = (
    "The reply was cut off at the output limit, so the tool calls in it "
    "were not run: {names}. Ask for the work in smaller pieces."
)
"""Printed when the reply that was cut off held tool calls.

None of them ran, the complete ones included. *names* has one name per
call, in the order the model wrote them.

Both forms open with the same words, ``The reply was cut off at the
output limit``. ``oc cli --prompt`` prints this line on standard output
and still exits 0, so those words are what a script can match."""


def _cut_off_notice(outcome: TurnOutcome | None) -> str:
    """The line for an exchange the output limit cut off, or ``""``.

    ``""`` for every other ending: an exchange that finished on its own,
    one that hit the turn ceiling, and one that was cancelled or failed
    and so has no outcome.

    Only the reply that was cut decides which form is used. The calls of
    an earlier model call in the same exchange ran, and are not named. A
    name is made inert for the terminal, and an empty or blank one is
    shown as ``?``.
    """
    if outcome is None or outcome.result.stop_reason is not StopReason.TRUNCATED:
        return ""
    final = outcome.result.final_message
    calls = final.tool_calls if final is not None else ()
    if not calls:
        return _CUT_OFF_NOTICE
    names = ", ".join(inert_line(call.name).strip() or "?" for call in calls)
    return _CUT_OFF_CALLS_NOTICE.format(names=names)
```

`Repl._drive` 的末尾，docstring 另加一句说明这一行：

```python
        finally:
            self._running = None
            await self._reap_asking()
        # Once the cards are taken down: the line of a prompt the exchange
        # left open would otherwise land between this one and the next prompt.
        notice = _cut_off_notice(handle.outcome)
        if notice:
            # ``Text``, not markup: a tool name may hold square brackets.
            self._screen.print(Text(notice, style="yellow"))
        return handle
```

打印的位置和打印的方式各有依据。放在 `finally` 之后：审批提示可能在交换结束后还开着，`_reap_asking` 负责收掉它。审核方在 pty 里造了一张超时后仍开着的审批卡，原型里残留的那行提示落在这句话之前，把打印挪到 `_reap_asking` 之前则落在这句话和下一个提示符之间（`RV81/logs/pty_card_left_open_80_proto.log`、`pty_card_left_open_80_mut_before_reap.log`）。用 `Text`：拼成 `f"[yellow]{notice}[/yellow]"` 时，名字里带 `[/]` 的调用会让 `rich` 抛 `MarkupError`，异常从 `_drive` 抛出去（`RV81/logs/notice_shapes.log`）。

### 2.4 原型

在 `git archive 5daf5482` 的导出副本上打了这三处改动，同一批探针在基线和原型上各跑一遍（基线是 `SCR/logs/out_01_shapes_base.log`、`out_02_oneshot_base.log`、`out_09_repl_pty_base.log`，第 2 版原型是 `r2_out_01_shapes_r2_proto.log`、`r2_out_02_oneshot_r2_proto.log`、`r2_out_09_repl_pty_r2_proto.log`）：

| 形状 | 今天 | 原型 |
|---|---|---|
| 只有文字，`length` 或 `max_tokens` | 没有说明 | 第一种形式，出现一次 |
| 文字加被截的 `write_file` | 没有说明 | 第二种形式，列出 `write_file` |
| 没有文字，只有被截的调用 | 推理之外只有两行统计 | 第二种形式 |
| 第二次模型调用被截（第一次的工具已执行） | 没有说明 | 出现一次，在工具结果之后 |
| 正常回答、轮数上限、失败、`/compact` | 没有这一行 | 没有这一行 |
| 截断之后再问一次，正常回答 | | 这一行只在被截断的那次出现 |

pty 里 80 列时这一行是黄色（`\e[33m`），折成两行，位于回复和下一个提示符之间。一次性执行的进程里它在 `stdout.txt` 的末尾，`stderr.txt` 没有变化，退出码仍是 0。审核方在 40、80、120 列下看过十几种情形（`RV81/logs/pty_*.log`），和这里一致。

原型没有证明的有两件。真实模型没有跑，理由在 §6.4。本计划探针里的替身不校验调用配对，截断之后的下一条消息在探针里总能发出去；这一段以 0080 的证据和审核方在合并树上的那次运行为准（`RV81/logs/pty_follow_advice_80_all2.log`：截断、照这句话把活拆小再提、下一条消息正常）。

## 3. 这句话

### 3.1 原文

owner 裁定的原文，两种形式：

```
The reply was cut off at the output limit and is incomplete. Ask for a shorter answer, or for it in parts.
The reply was cut off at the output limit, so the tool calls in it were not run: write_file. Ask for the work in smaller pieces.
```

第一种用在被截断的回复只有文字时，第二种用在它带工具调用时，`write_file` 的位置是名字的列表。

这两句守住的事实：

- 被截断的那一轮里没有工具被执行，参数完整的那几个也没有（0080 §1.1）。工具调用只在执行前才上屏，所以用户看不到模型本来要调什么，第二种形式因此点名。
- "cut off at the output limit" 沿用子代理那条路径已有的说法（`entry/subagent.py:474`、`:478`）。
- 后半句说的是用户自己能做的事，没有承诺产品做什么。产品不会自动续写，CLI 也不知道上限是多少（OpenAI 方言不发 `max_tokens`，0080 §3）。在 REPL 里它是下一条消息，在一次性执行里是换个说法再跑一次。没有写 "ask for the rest"，一次性执行里做不到（`--session` 和 `--prompt` 不能同用，`_surfaces.py:515-519`）。
- 第二种形式的后半句以 0080 已经落地为前提。0080 落地之前，带调用的截断之后下一条消息在 DeepSeek 上被 400 拒绝（0080 §0 第 2 条）。所以 0080 先合入是合并的硬条件（§7）。

第 1 版还列了两个备选（一句话不分形式，只陈述不给建议），都没有选，原文在 `83c245b9` 的 §3。

### 3.2 显示上定下来的小处

| 小处 | 定法 | 钉不钉 |
|---|---|---|
| 颜色 | 黄色，CLI 里要用户留意的提示都是它（`_repl.py:946`、`:955`、`:1032`） | 钉，T13 |
| 同名的调用 | 不合并，一个调用一个名字。三次 `write_file` 就列三次，列了几个就是几个调用没有执行 | 钉，T9 |
| 空的或只含空白的名字 | 显示成 `?`，和 `render.py:361` 的写法一致。第 1 版对只含空白的名字原样输出，成了 `not run:    .` | 钉，T10 |
| 名字的长度 | 不设上限。注册的工具名都短，超长的名字只可能来自坏掉的输出，`inert_line` 已经保证它不能作用于终端，后果是多折几行 | 不钉 |
| 这一行后面的空行 | 不补。它和下一个提示符之间没有空行，和 `/compact` 的结论行一样；`Cancelled.`、`Failed: …` 后面有一个 | 不钉，见下 |
| 只有思考、没有正文也没有调用的截断 | 走第一种形式。"Ask for a shorter answer" 对这种情况不太贴切，但不算错 | 不另立形式 |

空行要不要对齐，我的建议是 0081 里不动。这一行和 `/compact` 的结论行是同一类：都在 `_drive` 返回之后打印，前面隔着 `_pump` 补的那个空行，后面紧跟提示符。黄色已经把它和上下文分开了。要对齐的话是在打印之后加一句 `self._screen.print()`，草稿测试没有一条会因此变红；更一致的做法是把 `/compact` 的那一行一起改，单独做。

### 3.3 进文件时的样子，和开头这几个字

输出不是终端时 `rich` 按 80 列硬折，折出来的行有的行尾带一个空格（`SCR/logs/r2_out_02_oneshot_r2_proto.log` 的 `stdout.txt`，`r2_out_13_notice_shapes_r2_proto.log`）。第一种形式固定折成两行，前一行行尾有空格。第二种形式到 `were not run:` 正好 80 个字符，名字从第二行开始，折成几行随名字的多少变，九个并行调用时是三行。所以脚本不能拿整句去比。

一次性执行时这一行在标准输出上，退出码又不变，开头的 `The reply was cut off at the output limit` 就成了脚本判断"这次回复被截断了"的事实上的接口。它 41 个字符，总在行首，折行切不到它。以后改这几个字要当接口改：T14 把两句原文和这个开头都写死了，改措辞必须改这条测试；`docs/core-features/cli.md` 里也要写明（§5）。

## 4. 哪些场合显示

| 场合 | 这次 | 依据 |
|---|---|---|
| 交互式 REPL | 显示 | §2 |
| 一次性执行 | 显示，同一行，在标准输出；退出码不变 | owner 裁定，见下 |
| 子代理被截断 | 不另外显示 | 见下 |
| 摘要调用被截断 | 不算，只记录 | owner 裁定，见下 |
| Desktop | 不做 | `converged` 的交换只发一个 `done` 帧（`turn_observation.py:266-267`），线上没有 `stop_reason` |
| Channel | 不做 | 交换结束后从轨迹里取最后一条有文字的 assistant 消息发出去（`_answer`，`runtime.py:649-655`），半截回答原样发出，没有说明 |

一次性执行。标准输出今天就不只有回答：`out_02_oneshot_base.log` 的 `stdout.txt` 里有 `Context: …`、回答、`Turn 1 done, …`，工具行和 `Failed: …` 也在这里，bench 从它读 `Failed:` 和 `Approval` 行（`bench/adapters/omicsclaw.py:67-68`）。这一行和 `Failed: …` 是同一类东西，放在同一个 `Screen` 上不需要新参数，输出重定向到文件时它跟在被截断的回答后面。bench 的两个正则都锚在行首的 `Failed:` 和 `Approval`，这一行以 `The reply` 开头，不会被误读，审核方把这两个正则在原型的输出上跑过（`RV81/logs/bench_regex_on_proto_stdout.log`）。退出码不变：bench 把非零退出码判成 `INFRA_FAILURE`，这个判断排在读 `truncated` 之前（`bench/outcome.py:215-220`）。

子代理。`_conclusion` 已经处理了 `TRUNCATED`（`entry/subagent.py:471-480`）：子代理写了文字时，把 `[name] was cut off at the output limit; …` 放在结论最前面交给父代理；没写文字时抛 `DelegationIncomplete`。两种情况父代理的 CLI 今天都看得到，因为 `task` 的结果预览显示输出的前三行（`out_03_subagent_base.log` 的 A、B 段）。父代理的模型也读到了这句话，由它决定怎么办。父代理自己的回复没有被截断，它的 `stop_reason` 是它自己的，所以不会为子代理出这一行；父代理随后自己也被截断时才出（C 段）。要另外显示，子代理的停止原因就得从 `ChildRunner.delegate` 传到界面，那是一个新的跨层字段，换来的信息屏幕上已经有了。

摘要。摘要调用不是一次交换。`_ProviderSummarizer.summarize` 只取 `completion.message.content`，`finish_reason` 没有往外传（`entry/assembly.py:711-718`），`Summarizer` 协议返回的是 `str`。被截断的摘要今天被当成完整的摘要写回，`/compact` 照常打印 `Compacted: …`（`out_03_subagent_base.log` 的 D 段）。要显示就得改 `omicsclaw.context` 的协议，出了"只是显示"的范围。

## 5. 改动范围

| 文件 | 改动 |
|---|---|
| `omicsclaw/entry/cli/_repl.py` | §2.3 的三处 |
| `tests/entry/test_cli_repl.py` | §6.1 的 14 条，单独一节，放在文件末尾 |
| `docs/core-features/cli.md` | §9"事件渲染"的表后面加一段，内容见下 |
| `docs/plans/0081-truncation-notice.md` | 加交付记录 |
| `CHANGELOG.md` | 合并时在顶部加一条 |

`cli.md` 那一段要写到这几件事：交换结束后 `_drive` 读 `handle.outcome`，回复被输出上限截断时打印一行黄字，位置在回复之后、下一个提示符之前；两种形式的原文，以及什么时候用哪种；被截断的回复里的工具调用一个都没有执行；一次性执行时这一行在标准输出，退出码不变；两种形式都以 `The reply was cut off at the output limit` 开头，脚本按这个开头匹配，不要按整句，因为进文件时会按 80 列折行，改这个开头等于改接口；不显示的场合（子代理、摘要、轮数上限、Desktop、Channel）。

不改的：`omicsclaw/engine/`、`omicsclaw/provider/`、`entry/turn.py`、`entry/events.py`、`entry/render.py`、`entry/session.py`、`entry/desktop/`、`entry/channel/`、`launch/_surfaces.py`、`OMICSCLAW.md`、工具描述。`tests/entry/golden/` 不用重录。`README.md` 不动。

## 6. 测试和验证

### 6.1 新增的测试

草稿在 `SCR/r2_proposed/test_plan0081_r2_proposed.py`，落地时放进 `tests/entry/test_cli_repl.py`。T1 至 T7、T11 至 T13 用真实的装配、真实的 `SessionRegistry` 和 `Repl`，屏幕写进缓冲；T8 至 T10、T14 直接调 `_cut_off_notice` 或读常量。能报 `finish_reason` 的替身 `Finishing` 用 0080 的实现放在 `tests/entry/test_turn_runner.py` 里的那一个，直接 import；草稿里为了能在没有它的树上跑，带了一份一样的，落地时删掉。

| 编号 | 测试 | 钉住什么 | 今天 |
|---|---|---|---|
| T1 | `test_a_reply_cut_off_at_the_output_limit_is_reported_under_it`（`length`、`max_tokens`） | 只有文字的截断：以固定开头起头的行恰好一行，就是第一种形式；在被截断的文字之后、退出语之前；提示符照常回来 | 红 |
| T2 | `test_a_cut_off_reply_names_the_tool_calls_that_did_not_run`（有文字、没有文字） | 带调用的截断是第二种形式并点出 `write_file`；屏幕上没有 `-> write_file` | 红 |
| T3 | `test_a_cut_in_a_later_model_call_is_reported_once_at_the_end` | 第一次模型调用的工具执行了，第二次带着调用被截：只有一行，在工具结果之后，只列被截断那条回复里的调用 | 红 |
| T4 | `test_an_exchange_that_was_not_cut_off_prints_no_such_line`（`stop`、没有 `finish_reason`、轮数上限、失败） | 别的结束方式不出这一行 | 绿，是护栏 |
| T5 | `test_the_line_is_not_repeated_by_compact_or_by_the_next_answer` | 截断之后的 `/compact` 和下一次正常回答都不再出这一行 | 红 |
| T6 | `test_a_single_shot_run_reports_the_cut_and_still_converges` | `run_once` 出同一行，`handle.terminal` 仍是 `converged` | 红 |
| T7 | `test_the_line_is_printed_after_open_cards_are_taken_down` | 这一行在 `_reap_asking` 之后 | 红 |
| T8 | `test_the_notice_is_empty_for_every_ending_but_a_cut` | `None`、`CONVERGED`、`MAX_TURNS` 返回空串；`TRUNCATED` 返回第一种形式，轨迹为空时也是，前面有一轮带调用的消息时也是 | 红 |
| T9 | `test_the_notice_lists_every_call_of_the_cut_message_in_order` | 被截断那条消息里的每个调用列一个名字，按原顺序，同名的不合并；更早轮次的调用不列 | 红 |
| T10 | `test_a_tool_name_cannot_act_on_the_terminal_through_the_notice` | 工具名里的控制字符不原样上屏；空的和只含空白的名字显示成 `?` | 红 |
| T11 | `test_a_text_only_cut_after_a_tool_ran_does_not_name_that_tool` | 前面轮次的工具已执行、最后被截断的回复只有文字：第一种形式，屏幕上没有 "were not run" | 红 |
| T12 | `test_a_tool_name_with_square_brackets_reaches_the_screen_as_it_is` | 被截断的调用名是 `write[/]file` 和 `[red]x`：经 REPL 原样上屏 | 红 |
| T13 | `test_the_line_is_handed_to_the_screen_as_yellow_text` | 交给 `Screen.print` 的是一个 `Text`，样式是 `yellow`，没有以字符串形式交出去 | 红 |
| T14 | `test_the_two_forms_read_as_agreed_and_open_with_the_same_words` | 两句原文逐字等于 §3.1；都以 `The reply was cut off at the output limit` 开头 | 红 |

19 个用例。在基线副本上 15 红 4 绿，绿的是 T4 的四个；在第 2 版原型上 19 绿（`SCR/logs/r2_out_04_proposed_base.log`、`r2_out_04_proposed_r2_proto.log`，日志里的总数各多 1，是打印 `omicsclaw.__file__` 的那一条）。在第 1 版原型上 T10 和 T14 红，对应第 2 版改的两处：只含空白的名字，第二种形式的措辞（`r2_out_04_proposed_r1_proto.log`）。

T11 和 T12 是审核方要求补的，T13 和 T14 是这一版自己加的，各自堵上的变异见 §6.2。原文只在 T14 里写了一遍，其余的测试比对模块里的常量；写死的另有那个固定开头，用来数这一行出现了几次、出现在哪里。

不加脚本化 eval。`ScriptedProvider` 报不出 `length`（0080 §1.3 的 P5），而且 eval 的 Runner 不经过 CLI。

### 6.2 变异

对第 2 版原型做了 23 处定点变异（`SCR/r2_mutate.py`，`SCR/logs/r2_out_05_mutations.log`）。M 是第 1 版自己的，R 是审核方的（`RV81/probes/rv_mutate.py`，在第 2 版原型上重新定位；它的 R9 是一个探针，没有对应的变异，由 N1 代替），N 是这一版新加的。

| 编号 | 变异 | 转红的测试 |
|---|---|---|
| M1 | 算出这一行却不打印 | T1、T2、T3、T5、T6、T7、T11、T12、T13 |
| M2 | 凡不是 `CONVERGED` 都显示 | T4、T8 |
| M3 | 从不点名 | T2、T3、T9、T10、T12 |
| M4 | 工具名不过 `inert_line` | T10 |
| M5 | 空名字不显示成 `?` | T10 |
| M6 | 从整段轨迹收集调用 | T3、T8、T9、T11 |
| M7 | 在回复上屏之前打印 | T1、T3、T7 |
| M8 | 一次性执行不打印 | T6 |
| M9 | 在收审批提示之前打印 | T7 |
| R1 | 去掉颜色 | T13 |
| R2 | 拼成 markup 字符串打印 | T12、T13 |
| R3 | 同名的调用合并 | T9、T10 |
| R4 | 在 `handle.wait()` 之前读结果 | T1、T2、T3、T5、T6、T7、T11、T12、T13 |
| R5 | 被截断的消息有文字就用第一种形式 | T2、T3 |
| R6 | 打印两次 | T1、T2、T3、T5、T6、T11、T12、T13 |
| R7 | 挪进 `finally` 里打印 | 没有 |
| R8 | 取轨迹里最近一条带调用的消息，不取最后一条 | T8、T11 |
| R10 | 只列第一个名字 | T9、T10、T12 |
| R11 | 打到标准错误 | T1、T2、T3、T5、T6、T7、T11、T12、T13 |
| R12 | `/compact` 也出这一行 | T5、T8 |
| N1 | 只含空白的名字原样输出 | T10 |
| N2 | 第二种形式用第 1 版的措辞 | T14 |
| N3 | 换掉开头那几个字 | T1、T5、T6、T7、T11、T13、T14 |

审核方的变异在第 1 版上有五处全绿：R1、R2、R3、R7、R8（`RV81/logs/mutations_reviewer.log`）。这一版堵上四处：R1 由 T13，R2 由 T12 和 T13，R3 由 T9 和 T10，R8 由 T8 和 T11。

R7 决定不钉。它和原型只在一种情况下有差别：`_drive` 因异常离开，而 handle 上已经有一个被截断的结果。走到那里要屏幕代码自己在交换结束之后抛错，这时 REPL 正带着那个异常往外走，这一行打不打都不影响用户看到的结局。规则 8 写的是原型的行为，没有测试钉它。

### 6.3 档位和要跑的

`SPEC.md` 第 2 档。改动在一个包里，不改别的代码 import 的东西，不改 agent 看到的任何文字。provider 适配器、权限门、Desktop 线上契约都没有碰。合并到 `main` 属于第 4 档，由 PR 上的 `Eval CI` 跑。

第 2 档要跑新增的测试和这个包的测试目录。一次性执行从 `omicsclaw/launch` 进来，bench 读 CLI 的标准输出，所以把 `tests/launch` 和 bench 的两个文件也带上：

```
/opt/conda/envs/rapids_singlecell/bin/python -m pytest tests/entry tests/launch -q -p no:randomly
/opt/conda/envs/rapids_singlecell/bin/python -m pytest tests/bench/test_oc_cli_contract.py tests/bench/test_omicsclaw_adapter.py -q -p no:randomly
```

量过的数字，都不含草稿测试：

| 树 | 第一条 | 第二条 |
|---|---|---|
| 基线 `5daf5482` 的导出副本 | 2163 passed、7 skipped、3 xfailed | 58 passed、2 failed |
| 第 2 版原型 | 2163 passed、7 skipped、3 xfailed | 58 passed、2 failed |
| 合并树（§7）加第 2 版原型 | 2481 passed、7 skipped、3 xfailed | 58 passed、2 failed |

第二条里红的两条（`test_real_runs_can_be_graded_from_another_checkout`、`test_the_launch_says_which_code_the_run_uses`）要读 git 的提交号，导出的副本不是 git 检出，所以在三棵树上同样失败。在真实的检出里（本计划的 worktree，`5daf5482`）这两个文件是 60 passed。日志是 `SCR/logs/out_08_level2_base.log`、`r2_out_08_level2_r2_proto.log`、`r2_out_08_level2_combo.log`、`r2_out_12_bench_{base,r2_proto,combo}.log`；审核方跑的见 `RV81/logs/t_level2_proto.log`、`t_bench_contract_{base,proto}.log`。

另外：先在没改的代码上跑新测试，确认是 15 红 4 绿，再改代码。落地后把 23 处变异重做一遍，除 R7 外每处至少一条测试转红。

### 6.4 真实终端和真实模型

真实终端。原型在 pty 里看过两条路径：一次性执行（`r2_out_02_oneshot_r2_proto.log` 的 pty 段）和带真实输入源的交互式 REPL（`r2_out_09_repl_pty_r2_proto.log`，`probe_04_repl_pty.py` 在提示符回来之后逐行输入）；合并树上各再跑了一遍（`r2_out_02_oneshot_combo.log`、`r2_out_09_repl_pty_combo.log`）。落地后重跑这两个探针就够，不需要人再看一遍。`SCR` 在 `/tmp` 下，不保证一直在，丢了就照 §2.4 的形状重写：一个报 `finish_reason="length"` 的替身 provider，经 `omicsclaw.launch.main` 起真实的 `oc cli`，放进 pty 里读回屏幕。

真实模型不需要。这一行出不出现只取决于 `stop_reason`。`finish_reason` 到 `StopReason.TRUNCATED` 这一段由引擎的测试钉着，0080 用真实 DeepSeek 确认过真实的 `length` 会走到 `stop=truncated`（0080 §1.5 和 V8）。这次没有给模型看的文字，没有措辞要对比。

## 7. 与在办分支的关系和落地条件

owner 裁定的两个条件：

- 0080 合入 `main` 之后才合 0081，这是合并的硬条件。第二种形式劝用户把活拆小了再提，而 0080 落地之前，带调用的截断之后下一条消息在 DeepSeek 上会失败，用户照着做只会看到 `Failed: ProviderError`。第 1 版写过"0081 先落地就换一种措辞"的退路，有了这个硬条件就不适用了。
- 实现等 `feat/ask-user-r4` 和 0080 都合入 `main` 之后再派，从合入后的 `main` 开分支。

两条分支写这一版时的位置：`feat/ask-user-r4` 在 `b2e73358`，0080 的实现在分支 `fix/unanswered-tool-calls` 的 `daa04fd7`。两者都还会各有一个小的收尾提交。

`feat/ask-user-r4` 在 `_repl.py` 里改动很多，离本计划三处改动最近的是 import 段、`Repl.__slots__` 和 `_pump` 开头的局部变量；它没有碰 `_drive`、`_compaction_verdict` 和 `run_once`。它也改 `tests/entry/test_cli_repl.py` 和 `docs/core-features/cli.md` §9 的事件表。0080 的实现改 `omicsclaw/context/transcript.py`、`omicsclaw/context/__init__.py`、`omicsclaw/entry/turn.py`、`entry/session.py` 和几个测试文件，其中 `tests/entry/test_turn_runner.py` 加了 `Finishing`；和本计划没有共同的代码文件。计划 0078 的实现只改 `transcript.py`，和本计划无关。

合并试过两遍，都没有冲突：

- 这一版按文件做了一次三方合并（`SCR/r2_merge_tree.py`，`SCR/logs/r2_out_06_merge_tree.log`）：`main` 的 `5daf5482`，加 `feat/ask-user-r4` 的 `b2e73358`，加 `fix/unanswered-tool-calls` 的 `daa04fd7`，加第 2 版原型。`diff3 -m` 合了六个文件，没有冲突，结果在 `SCR/r2_combo/`。这棵树上草稿的 19 个用例全绿，`Finishing` 是从 `tests/entry/test_turn_runner.py` import 的（`r2_out_07_combo_proposed.log`）；§6.3 的两条命令的结果在那张表的第三行。
- 审核方用真实的 `git merge` 在同样的两个 tip 上按计划的顺序合过第 1 版的原型，没有冲突（`RV81/logs/merge_trials.log` 的第二轮）。

## 8. 这次不做的，和顺带看到的

owner 被问到时没有选、这次不做的：

- 给模型的截断提示。模型仍然不知道自己被截断过（0080 的 K1）。
- 引擎内续写或重试。
- 补 Anthropic 方言的输出上限表（0080 §3）。
- 非 `length` 的中断不算截断。这一行跟的是引擎的 `TRUNCATED`，引擎只认 `length` 和 `max_tokens`（`loop.py:93`）；`refusal`、`content_filter` 这些不会出这一行（`RV81/logs/facts_base.log` 的 `content_filter` 一行）。后端截了输出却不报 `length` 时也不会出。
- 把 provider 报错的原文显示出来。

顺带看到、不在本计划里的。前两条 owner 已经定了去向（§9）：

- 到达轮数上限的交换，CLI 上同样没有任何说明（`out_01_shapes_base.log` 的 h 段）。`TurnOutcome.hit_the_turn_ceiling`（`turn.py:142-145`）在 `omicsclaw/` 里没有调用方。
- Channel 上，一次交换没有产出文字时，会把上一次交换的回答再发一遍。`_answer` 在整段轨迹里倒着找最后一条有文字的 assistant 消息，轨迹包含带进来的历史；`TurnOutcome.reply`（`turn.py:134-140`）是同样的找法。第 1 版把触发条件写成"被截断且没有文字"，写窄了：审核方跑出一次没有被截断、回复为空的交换同样重发（`RV81/logs/channel_replay_base.log`），和截断无关。
- 摘要调用被截断时没有人知道（§4）。
- `CLI_USAGE`（`_surfaces.py:275-278`）和 `docs/core-features/cli.md:113` 说 `oc cli --prompt … > answer.txt` 只写答案。实际还有 `Context: …`、`Turn N done, …` 和工具行（`out_02_oneshot_base.log`）。
- 0080 §1.3 的 P2 写"之后用同一个 `--session` 再执行就卡住"。`--session` 和 `--prompt` 一起用会被拒绝（`_surfaces.py:515-519`，`RV81/logs/session_with_prompt.log`），按字面这条路走不到。第 1 版只写到这里，漏了一点：一次性执行留下的会话可以用 `oc cli -- --session <id>` 在 REPL 里续上，续上之后同样卡住（`RV81/logs/oneshot_session_resumed_in_repl.log`）。所以 P2 说的后果是成立的，走到那里的方式和它写的不一样。按 0080 §0 第 6 条，它落地后这样续上的会话也会在开场时被清理。

## 9. 裁定

owner 2026-10-09 批准了计划，并作了下面的裁定，由派发方转述。

1. 措辞用第 1 版的甲，第二种形式加限定，原文是 §3.1 的两句。
2. 放法 A：只改 `omicsclaw/entry/cli/_repl.py`，不加跨层字段。
3. 一次性执行时这一行打到标准输出。
4. 一次性执行被截断时退出码不变。
5. `/compact` 的摘要调用被截断这次不算，只记录。
6. 0080 合入 `main` 之后才合 0081，这是合并的硬条件。
7. 实现等 `feat/ask-user-r4` 和 0080 都合入 `main` 之后再派，从合入后的 `main` 开分支。
8. 轮数上限的提示不并进 0081；0081 落地后单独做，不另写计划。
9. Channel 上"本次交换没有产出文字就把上一次的回答再发一遍"另开一项直接修，不在 0081 里。

仍要 owner 定的：没有。§3.2 里空行的那条是我的建议，没有裁定，按建议就是不动。

## 10. 给实现方

- 开工的前提：`feat/ask-user-r4` 和 0080 的实现都已经合入 `main`。从那时的 `main` 开分支。0080 没有合入就不要合 0081。
- 依据是 §2.2 的九条规则、§3.1 的原文和 §6.1 的测试表。三者有出入时以规则和原文为准，并把出入报回来。
- 三处改动在合并后的树上的落点，按符号找（行号是 `SCR/r2_combo/` 里已经打上原型的那个文件的，只作参考）：import 段，`from omicsclaw.entry.turn import TurnHandle` 那一行（约第 130 行）；`_compaction_verdict` 之后、`class Repl` 之前（约第 536 到 602 行之间）；`Repl._drive` 的 `finally` 之后（这个函数约从第 1235 行起）。
- §2.3 的代码可以照抄。草稿测试是 `SCR/r2_proposed/test_plan0081_r2_proposed.py`，一个文件，落地时并进 `tests/entry/test_cli_repl.py` 的末尾，删掉其中自带的 `Finishing` 和打印 `omicsclaw.__file__` 的那一条，改成 `from tests.entry.test_turn_runner import Finishing`。`SCR` 在 `/tmp` 下，不保证一直在，丢了就照 §2.3 和 §6.1 的表重写。
- 先在没改的代码上跑新测试，确认是 15 红 4 绿，再改代码。
- 代码、测试、`docs/core-features/cli.md` 的那一段（§5 列了要写到的内容）和 `CHANGELOG.md` 的一条在一次改动里完成。
- 合并之前：§6.3 的两条命令；23 处变异重做，除 R7 外都要有测试转红；§6.4 的两个 pty 探针；PR 上的 `Eval CI`。不需要真实模型。
- 不在范围里的：给模型的任何文字，事件帧和 `terminal`，退出码，标准错误，Desktop，Channel，摘要调用，轮数上限的提示，`_compaction_verdict` 的措辞，这一行前后的空行。

## 11. 第 1 版到第 2 版改了什么

审核方的结论是"可以交 owner 批准"。它核实成立的有：§1 的事实，原型在真实 pty 里各情形的显示，测试的红绿，与两条在办分支的三方合并没有冲突，被截断的回复里的调用一个都不执行，标准输出今天就不只有回答，bench 的两个正则不受影响。下面是它的意见和这一版的处理，每一条都先读了它的证据。

| 意见 | 核对 | 处理 |
|---|---|---|
| P2-1 前面轮次的工具已执行、最后被截断的回复只有文字时，没有测试钉住用第一种形式。变异 R8 全绿，屏幕上却对一个已经执行的工具说 not run | 属实（`RV81/logs/mutations_reviewer.log`、`mutation_r8_on_screen.log`）。在第 2 版原型上重做 R8，T8 和 T11 转红 | 加 T11，T8 加一条断言 |
| P2-2 没有测试钉住用 `Text` 打印。变异 R2 全绿，而工具名带 `[/]` 时 markup 字符串会抛 `MarkupError` | 属实（`RV81/logs/notice_shapes.log`） | 加 T12；T13 也覆盖它 |
| P2-3 第二种形式加限定 | owner 裁定了原文 | 常量、测试和 §3.1 跟着改；T14 把原文写死 |
| P2-4 0080 先合入是硬条件 | owner 裁定 | 写进 §7、§9、§10；第 1 版"0081 先落地就换措辞"的退路标成不适用 |
| P3 `finally` 之后打印的顺序 | 审核方用一张真的还开着的卡验证了，第 1 版的 T7 用的只是标记 | §2.3 引用它的两份日志；"原型没有证明的"里去掉这一条 |
| P3 这一行和提示符之间没有空行 | 属实 | 给了建议：不动（§3.2） |
| P3 进文件时按 80 列硬折、行尾带空格；开头成了脚本的接口 | 属实，自己另跑了一遍（`r2_out_13_notice_shapes_r2_proto.log`） | 新增 §3.3；`cli.md` 的要求写进 §5；T14 钉住开头 |
| P3 没钉住的小处：颜色、同名去重、只含空白的名字、名字长度 | R1、R3 属实；只含空白的名字确实显示成 `not run:    .` | 逐条定了（§3.2）：颜色钉（T13），不去重并钉（T9），空白显示成 `?` 并钉（T10，代码加了 `.strip()`），长度不设上限、不钉 |
| P3 只有思考的截断走第一种形式 | 属实（`RV81/logs/pty_thinking_only_80_proto.log`） | 记在 §3.2 |
| P3 分支信息、`Finishing` 的位置 | 核对了两个 tip | §7 更新；自己按文件合了一棵树，草稿测试从 `test_turn_runner.py` import `Finishing` 通过 |
| P3 落地时跑 bench 的两个文件 | 同意 | 写进 §6.3，并补了导出副本上有两条因为不是 git 检出而失败的说明 |
| 范围外：Channel 重发的条件写窄了；0080 P2 那条路可以从 REPL 续上 | 都属实 | §8 改写 |

没有不同意的意见。审核方的变异在第 1 版上全绿的五处里，这一版堵上了四处，R7（挪进 `finally` 里打印）仍然不钉，理由在 §6.2。

这一版自己另外改的：

- 新增 §2.2 的规则和 §2.3 的代码，定稿之后实现方不依赖 `/tmp` 下的原型。
- 新增 T13、T14 和变异 N1 至 N3。测试从 10 条 15 个用例变成 14 条 19 个用例，变异从 9 处变成 23 处。
- 原型从新增 37 行变成 52 行，多出来的是 `.strip()`、两处注释和 docstring。
- 第 1 版的 §9 是待裁定的问题，这一版改成裁定；Q1 至 Q7 都有了结论。
- 删掉了备选措辞乙、丙的原文和放法 B、C 的展开，只留结论。

## 12. 证据清单

`SCR` 下，第 2 版的文件以 `r2_` 开头。每个探针和测试日志的第一行是 `omicsclaw.__file__`，指向被测的那棵树。

| 文件 | 内容 |
|---|---|
| `base/`、`r2_proto/` | `git archive 5daf5482` 的导出和第 2 版原型（`proto/` 是第 1 版的） |
| `r2_prototype.diff` | 第 2 版原型的完整 diff |
| `r2_proposed/test_plan0081_r2_proposed.py`，`logs/r2_out_04_proposed_{base,r2_proto,r1_proto}.log` | 草稿测试和三棵树上的结果 |
| `r2_mutate.py`，`logs/r2_out_05_mutations.log` | 23 处变异 |
| `r2_trees/`，`r2_merge_tree.py`，`r2_combo/`，`logs/r2_out_06_merge_tree.log`，`logs/r2_out_07_combo_proposed.log` | 两条在办分支的导出、按文件的三方合并、合并树和上面的草稿测试 |
| `logs/out_08_level2_base.log`，`logs/r2_out_08_level2_{r2_proto,combo}.log`，`logs/r2_out_12_bench_{base,r2_proto,combo}.log` | §6.3 两条命令在三棵树上的结果 |
| `probes/p81_common.py` | 脚本化的 provider 和装配 |
| `probes/probe_01_cli_shapes.py`，`logs/out_01_shapes_base.log`，`logs/r2_out_01_shapes_r2_proto.log` | 各种结束方式下 REPL 和 `run_once` 的屏幕 |
| `probes/probe_02_oneshot_process.py`，`logs/out_02_oneshot_base.log`，`logs/r2_out_02_oneshot_{r2_proto,combo}.log` | 真实的 `oc cli -- --prompt` 进程：`stdout`、`stderr`、退出码，以及 pty 下的样子 |
| `probes/probe_03_subagent_and_summary.py`，`logs/out_03_subagent_base.log`，`logs/r2_out_03_subagent_r2_proto.log` | 子代理被截断、摘要被截断时父代理的屏幕 |
| `probes/probe_04_repl_pty.py`，`logs/out_09_repl_pty_base.log`，`logs/r2_out_09_repl_pty_{r2_proto,combo}.log` | pty 里带真实输入源的交互式 REPL |
| `probes/probe_05_channel_answer.py`，`logs/out_10_channel_answer_base.log` | Channel 的 `_answer` 对被截断的交换返回什么 |
| `probes/r2_probe_06_notice_shapes.py`，`logs/r2_out_13_notice_shapes_r2_proto.log` | 各种名字下这一行的样子，以及进文件时怎么折 |
| `merge/ref_0080_final.md` | 引用的 0080 定稿（`fe684b26`） |

第 1 版的原型、草稿测试、变异和合并试验（`proto/`、`prototype.diff`、`proposed/`、`mutate.py`、`merge_r4.sh` 和不带 `r2_` 的日志）都还在，对应提交 `83c245b9` 里的数字。

`RV81` 下引用到的：`RV81/logs/` 里的 `facts_base.log`、`mutations_reviewer.log`、`mutation_r8_on_screen.log`、`notice_shapes.log`、`pty_*.log`、`bench_regex_on_proto_stdout.log`、`merge_trials.log`、`channel_replay_base.log`、`session_with_prompt.log`、`oneshot_session_resumed_in_repl.log`、`t_level2_proto.log`、`t_bench_contract_{base,proto}.log`，以及 `RV81/probes/rv_mutate.py`。
