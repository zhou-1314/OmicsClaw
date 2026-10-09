# 计划 0081：回复被输出上限截断时，CLI 显示一句话

状态：第 1 版（2026-10-09），待独立审核和 owner 裁定。本计划没有改生产代码，也没有往仓库里加测试。

基线：`main` 的 `5daf5482`，行号以它为准，实施时按符号名重新定位。

范围：owner 2026-10-09 的裁定（计划 0080 §10.1 第 4 条），只做"截断时 CLI 显示一句"。只是显示，不碰给模型看的文字，不碰 Desktop 的线上契约。

证据：探针、原型、提议的测试和输出在 `/tmp/claude-0/-workspace-dataset-private-zhouwg-data-OmicsClaw/9885f10a-3799-4309-9dfb-0981c166422a/scratchpad/plan-0081/`，下面记作 `SCR`，清单见 §10。模型都是脚本化的替身，本计划对真实模型发了 0 次请求。计划 0080 查清的事实直接引用，写作"0080 §x"，指 `docs/plan-0080-truncated-exchange` 分支上的定稿第 3 版（`fe684b26`）。

## 0. 摘要

1. 截断的信号今天已经在 CLI 手里。交换结束后 `TurnHandle.outcome.result.stop_reason` 是 `truncated`，CLI 等到了这个 handle，但没有读这个字段，所以屏幕上什么都没有。
2. 推荐做法：`Repl._drive` 在交换结束后读一次 handle 上的结果，是截断就打印一行黄字。改动只在 `omicsclaw/entry/cli/_repl.py`，新增 37 行、改 1 行。不给交换结果或事件帧加字段，Desktop 和 Channel 的代码不经过这里。
3. 这句话有两种形式，看被截断的那条回复带不带工具调用。带的时候点出没有执行的工具名，因为这些调用从不上屏，用户没有别的途径知道。原文和两个备选在 §3。
4. REPL 和一次性执行走同一段代码，显示同一行。一次性执行时它和 `Failed: …` 一样在标准输出上，退出码不变。子代理被截断、摘要调用被截断都不另外显示，理由在 §4。
5. 原型做在导出的基线副本上：今天不显示，原型显示。提议的 10 条测试（15 个用例）在今天的代码上 11 红 4 绿，在原型上全绿；9 处定点变异都有测试转红；和 `feat/ask-user-r4` 三方合并没有冲突。按 `SPEC.md` 属于第 2 档。
6. 建议排在 0080 的实现之后落地，原因在 §7。要 owner 定的问题 7 条，见 §9。

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

每一种形状 handle 上都是 `terminal=converged stop_reason=truncated`，屏幕上没有一行提到截断。没有文字的那一种，屏幕上除了模型的推理（开着显示时，`_repl.py:1247-1251`）只有 `Context: …` 和 `Turn 1 done, …` 两行。

## 2. 做法

### 2.1 三种放法

| | 放法 | 新增的跨层东西 |
|---|---|---|
| A | CLI 在交换结束后读 `handle.outcome.result.stop_reason`，自己打印 | 没有 |
| B | 给 `EXCHANGE_END` 帧加 `stop_reason`，由 `TextRenderer._terminal_line` 出这一行 | `TurnEvent` 多一个字段 |
| C | 被截断的交换不再报 `converged`，给 `Terminal` 加一个值 | `Terminal` 多一个值 |

推荐 A。

B 的账。帧是 CLI、Channel、Desktop 共用的。`to_wire` 的 `EXCHANGE_END` 载荷是逐字段写的（`render.py:502-511`），新字段不会自己上线，`desktop_terminal_frames`（`entry/desktop/turn_observation.py:252-272`）也只看 `terminal` 和 `error`，但这两处都要补测试钉住。Channel 的 `_pump_reply` 在渲染之前就跳过了 `converged` 的 `EXCHANGE_END`（`entry/channel/runtime.py:604-610`），所以 B 并不会让 Channel 显示。`feat/ask-user-r4` 同时在改 `events.py` 和 `render.py`。换来的和 A 一样，是 CLI 上的一行。以后 Desktop 或 Channel 要做这件事，它们也各自拿着 handle（Channel 的 `_answer` 在 `runtime.py:649` 等的就是它），到时候再看要不要 B。

C 出了 owner 定的范围。`terminal` 在 Desktop 线上（`render.py:505`），还决定历史提交不提交（`entry/session.py:814`）、一次性执行的退出码（`_surfaces.py:772`）、Channel 发不发回答（`runtime.py:650`），改它是改契约。

### 2.2 A 的三处改动

都在 `omicsclaw/entry/cli/_repl.py`，完整的 diff 在 `SCR/prototype.diff`。

1. 两个模块级常量，放这句话的两种形式（§3）。
2. 一个模块级函数 `_cut_off_notice(outcome) -> str`，和 `_compaction_verdict` 并排。`outcome` 是 `None`（交换被取消或失败）或者 `stop_reason` 不是 `TRUNCATED` 时返回空串。是截断时看 `outcome.result.final_message`：引擎先把被截断的回复追加进轨迹再判定（`loop.py:436-438`），所以它就是最后一条消息。这条消息带工具调用就列出名字，不带就用没有名字的那种形式。工具名是模型写的文字，过 `inert_line`；空名字显示成 `?`，和 `render.py:361` 的写法一致。
3. `_drive` 在 `finally` 之后、`return handle` 之前调它，非空就 `self._screen.print(Text(notice, style="yellow"))`。

放在 `finally` 之后有一个原因：审批提示可能在交换结束后还开着，`_reap_asking` 负责收掉它，这一行等它收完再打印，不会画在一个开着的提示上。它也因此和 `/compact` 的结论行落在同一个位置：回复，空一行，这一行，下一个提示符。

要多 import 两个名字：`omicsclaw.engine` 的 `StopReason`，`omicsclaw.entry.turn` 的 `TurnOutcome`。entry 可以 import 引擎（`tests/entry/test_entry_is_the_top_layer.py`），`entry/turn.py` 和 `entry/subagent.py` 已经这样做。`omicsclaw.entry.cli` 的公开面不变。

`/compact` 也走 `_drive`，它的结果写死是 `CONVERGED`（`turn.py:709`），所以不会出这一行。

### 2.3 原型

在 `git archive 5daf5482` 的导出副本上打了这三处改动，同一批探针在基线和原型上各跑一遍（`SCR/logs/out_01_shapes_proto.log`、`out_02_oneshot_proto.log`、`out_09_repl_pty_proto.log`）：

| 形状 | 今天 | 原型 |
|---|---|---|
| 只有文字，`length` 或 `max_tokens` | 没有说明 | 第一种形式，一行 |
| 文字加被截的 `write_file` | 没有说明 | 第二种形式，列出 `write_file` |
| 没有文字，只有被截的调用 | 推理之外只有两行统计 | 第二种形式 |
| 第二次模型调用被截（第一次的工具已执行） | 没有说明 | 一行，在工具结果之后 |
| 正常回答、轮数上限、失败、`/compact` | 没有这一行 | 没有这一行 |
| 截断之后再问一次，正常回答 | | 这一行只在被截断的那次出现 |

pty 里 80 列时这一行是黄色（`\e[33m`），折成两行，位于回复和下一个提示符之间。一次性执行的进程里它在 `stdout.txt` 的末尾，`stderr.txt` 没有变化，退出码仍是 0。输出不是终端时 `rich` 按 80 列折行，所以这一行在文件里也是两行，`cut off at the output limit` 在前一行。

原型没有证明的有三件。真实模型没有跑，理由在 §6.3。探针里的替身不校验调用配对，截断之后的下一条消息在探针里总能发出去，这一段以 0080 的证据为准。T7 用的是 `_reap_asking` 上的一个标记，没有造一个真的还开着的审批提示。

## 3. 这句话说什么

写这句话要守住的事实：

- 被截断的那一轮里没有工具被执行，参数完整的那几个也没有（0080 §1.1）。工具调用只在执行前才上屏，所以用户看不到模型本来要调什么。
- 回复里没有调用时，下一条消息照常能发（0080 §1.5 的 A-g）。有调用时，今天下一条消息在 DeepSeek 上被 400 拒绝（0080 §0 第 2 条）；0080 落地后能发出去，但模型看不到那次调用，也不知道自己被截断过（0080 的 K1）。
- 产品不会自动续写。CLI 不知道上限是多少，OpenAI 方言不发 `max_tokens`（0080 §3）。
- 一次性执行没有下一条消息，`--session` 和 `--prompt` 不能同用（`_surfaces.py:515-519`）。

推荐（甲），两种形式：

```
The reply was cut off at the output limit and is incomplete. Ask for a shorter answer, or for it in parts.
The reply was cut off at the output limit. Tool calls not run: write_file. Ask for the work in smaller pieces.
```

- "cut off at the output limit" 沿用子代理那条路径已有的说法（`entry/subagent.py:474`、`:478`）。
- 第二种形式点出工具名。被截断的消息里有几个调用就列几个，按模型写的顺序，用逗号隔开。
- 后半句说的是用户自己能做的事，没有承诺产品做什么。在 REPL 里它是下一条消息，在一次性执行里是换个说法再跑一次，两边都成立。没有写 "ask for the rest"，一次性执行里做不到。
- 第二种形式的后半句以 0080 已经落地为前提（§7）。

备选乙，一句话，不分形式，不点名：

```
The reply was cut off at the output limit and is incomplete; no tool call in it was run. Ask for it in smaller pieces.
```

少一个分支和两条测试（T9、T10）。代价是只有文字的截断也会读到 "no tool call"，带调用的截断不知道是哪个工具。

备选丙，只陈述，不给建议：

```
The reply was cut off at the output limit and is incomplete.
The reply was cut off at the output limit. Tool calls not run: write_file.
```

用户照建议说了，模型也可能照原样重来再截一次（0080 的 K1），丙不写建议。0081 如果先于 0080 落地，也应当用丙。

样式用黄色。CLI 里要用户留意的提示都是它，例如 `_repl.py:946`、`:955`、`:1032`。

## 4. 哪些场合显示

| 场合 | 这次 | 依据 |
|---|---|---|
| 交互式 REPL | 显示 | §2 |
| 一次性执行 | 显示，同一行，在标准输出 | 见下 |
| 子代理被截断 | 不另外显示 | 见下 |
| 摘要调用被截断 | 不算 | 见下 |
| Desktop | 不做 | `converged` 的交换只发一个 `done` 帧（`turn_observation.py:266-267`），线上没有 `stop_reason` |
| Channel | 不做 | 交换结束后从轨迹里取最后一条有文字的 assistant 消息发出去（`_answer`，`runtime.py:649-655`），半截回答原样发出，没有说明 |

一次性执行往哪打。标准输出今天就不只有回答：`out_02_oneshot_base.log` 的 `stdout.txt` 里有 `Context: …`、回答、`Turn 1 done, …`，工具行和 `Failed: …` 也在这里，bench 从它读 `Failed:` 和 `Approval` 行（`bench/adapters/omicsclaw.py:67-68`）。这一行和 `Failed: …` 是同一类东西，说的是这次交换怎么结束的。放在同一个 `Screen` 上不需要新参数，输出重定向到文件时它跟在被截断的回答后面。bench 的两个正则都锚在行首的 `Failed:` 和 `Approval`，这一行以 `The reply` 开头，不会被误读。

另一种放法是只在一次性执行时改打标准错误。好处是 `> answer.txt` 时终端前的人看得到，文件里少一行。代价是 `Repl` 和 `run_once` 各多一个参数，要改 `launch/_surfaces.py`（`feat/ask-user-r4` 也在改 `_run_cli`），bench 存下的 `stdout.txt` 里就没有这一行。我建议用标准输出，列为 Q3。退出码列为 Q4。

子代理。`_conclusion` 已经处理了 `TRUNCATED`（`entry/subagent.py:471-480`）：子代理写了文字时，把 `[name] was cut off at the output limit; …` 放在结论最前面交给父代理；没写文字时抛 `DelegationIncomplete`。两种情况父代理的 CLI 今天都看得到，因为 `task` 的结果预览显示输出的前三行（`out_03_subagent_base.log` 的 A、B 段）。父代理的模型也读到了这句话，由它决定怎么办。父代理自己的回复没有被截断，它的 `stop_reason` 是它自己的，推荐做法不会为子代理出这一行；父代理随后自己也被截断时才出（C 段）。要另外显示，子代理的停止原因就得从 `ChildRunner.delegate` 传到界面，那是一个新的跨层字段，换来的信息屏幕上已经有了。

摘要。摘要调用不是一次交换。`_ProviderSummarizer.summarize` 只取 `completion.message.content`，`finish_reason` 没有往外传（`entry/assembly.py:711-718`），`Summarizer` 协议返回的是 `str`。被截断的摘要今天被当成完整的摘要写回，`/compact` 照常打印 `Compacted: …`（`out_03_subagent_base.log` 的 D 段）。要显示就得改 `omicsclaw.context` 的协议，出了"只是显示"的范围，列为 Q5。

## 5. 改动范围

| 文件 | 改动 |
|---|---|
| `omicsclaw/entry/cli/_repl.py` | §2.2 的三处 |
| `tests/entry/test_cli_repl.py` | §6.1 的 10 条，单独一节 |
| `docs/core-features/cli.md` | §9 的事件表后面加一段：交换结束后 `_drive` 读 `handle.outcome`，回复被输出上限截断时打印一行，带上两种形式的原文和哪些场合不显示 |
| `docs/plans/0081-truncation-notice.md` | 加交付记录 |
| `CHANGELOG.md` | 合并时在顶部加一条 |

不改的：`omicsclaw/engine/`、`omicsclaw/provider/`、`entry/turn.py`、`entry/events.py`、`entry/render.py`、`entry/session.py`、`entry/desktop/`、`entry/channel/`、`launch/_surfaces.py`、`OMICSCLAW.md`、工具描述。`tests/entry/golden/` 不用重录。`README.md` 不动。

## 6. 测试和验证

### 6.1 新增的测试

草稿在 `SCR/proposed/test_plan0081_proposed.py`，落地时放进 `tests/entry/test_cli_repl.py`。前七条用真实的装配、真实的 `SessionRegistry` 和 `Repl`，屏幕写进缓冲；后三条直接调 `_cut_off_notice`。能报 `finish_reason` 的替身 `Finishing` 和 0080 §6.4 要放进 `tests/entry/test_turn_runner.py` 的是同一个，后落地的一方直接 import。

| 编号 | 测试 | 钉住什么 | 今天 |
|---|---|---|---|
| T1 | `test_a_reply_cut_off_at_the_output_limit_is_reported_under_it`（`length`、`max_tokens`） | 只有文字的截断：这一行出现一次，在被截断的文字之后、退出语之前，提示符照常回来 | 红 |
| T2 | `test_a_cut_off_reply_names_the_tool_calls_that_did_not_run`（有文字、没有文字） | 带调用的截断用第二种形式并点出 `write_file`；屏幕上没有 `-> write_file` | 红 |
| T3 | `test_a_cut_in_a_later_model_call_is_reported_once_at_the_end` | 第一次模型调用的工具执行了，第二次被截：只有一行，在工具结果之后，只列被截断那条回复里的调用 | 红 |
| T4 | `test_an_exchange_that_was_not_cut_off_prints_no_such_line`（`stop`、没有 `finish_reason`、轮数上限、失败） | 别的结束方式不出这一行 | 绿，是护栏 |
| T5 | `test_the_line_is_not_repeated_by_compact_or_by_the_next_answer` | 截断之后的 `/compact` 和下一次正常回答都不再出这一行 | 红 |
| T6 | `test_a_single_shot_run_reports_the_cut_and_still_converges` | `run_once` 出同一行，`handle.terminal` 仍是 `converged` | 红 |
| T7 | `test_the_line_is_printed_after_open_cards_are_taken_down` | 这一行在 `_reap_asking` 之后 | 红 |
| T8 | `test_the_notice_is_empty_for_every_ending_but_a_cut` | `None`、`CONVERGED`、`MAX_TURNS` 返回空串；`TRUNCATED` 返回第一种形式，轨迹为空时也是 | 红 |
| T9 | `test_the_notice_lists_every_call_of_the_cut_message_in_order` | 列出被截断那条消息里的每个调用，按原顺序；更早轮次的调用不列 | 红 |
| T10 | `test_a_tool_name_cannot_act_on_the_terminal_through_the_notice` | 工具名里的控制字符不原样上屏；空名字显示成 `?` | 红 |

15 个用例。在基线副本上 11 红 4 绿，绿的是 T4 的四个；在原型上 15 绿（`SCR/logs/out_04_proposed_base.log`、`out_04_proposed_proto.log`，日志里的总数各多 1，是打印 `omicsclaw.__file__` 的那一条）。

测试比对的是模块里的常量，没有另抄一份原文，owner 改措辞时不用改测试。写死的词只有 `cut off at the output limit`，用来数这一行出现了几次、出现在哪里；选乙或丙时它仍在句子里。

对原型做了 9 处定点变异，每一处至少有一条测试转红（`SCR/mutate.py`，`SCR/logs/out_05_mutations.log`）：算出这一行却不打印；凡不是 `CONVERGED` 都显示；从不点名；工具名不过 `inert_line`；空名字不显示成 `?`；从整段轨迹收集调用；在回复之前打印；一次性执行不打印；在收审批提示之前打印。

不加脚本化 eval。`ScriptedProvider` 报不出 `length`（0080 §1.3 的 P5），而且 eval 的 Runner 不经过 CLI。

### 6.2 档位和要跑的

`SPEC.md` 第 2 档。改动在一个包里，不改别的代码 import 的东西，不改 agent 看到的任何文字。provider 适配器、权限门、Desktop 线上契约都没有碰。合并到 `main` 属于第 4 档，由 PR 上的 `Eval CI` 跑。

第 2 档要跑新增的测试和这个包的测试目录。一次性执行从 `omicsclaw/launch` 进来，所以把 `tests/launch` 也带上：

```
/opt/conda/envs/rapids_singlecell/bin/python -m pytest tests/entry tests/launch -q -p no:randomly
```

在 `5daf5482` 的导出副本上，基线和原型都是 2163 passed、7 skipped、3 xfailed，约 107 秒（`SCR/logs/out_08_level2_base.log`、`out_08_level2_proto.log`，不含提议的测试）。

另外：先在没改的代码上跑新测试，确认是 11 红 4 绿，再改代码。落地后把 9 处变异重做一遍。

### 6.3 真实终端和真实模型

真实终端。原型已经在 pty 里看过两条路径：一次性执行（`out_02_oneshot_proto.log` 的 pty 段）和带真实输入源的交互式 REPL（`out_09_repl_pty_proto.log`，`probe_04_repl_pty.py` 在提示符回来之后逐行输入）。落地后重跑这两个探针就够，不需要人再看一遍。`SCR` 在 `/tmp` 下，不保证一直在，丢了就照 §2.3 的形状重写。

真实模型不需要。这一行出不出现只取决于 `stop_reason`。`finish_reason` 到 `StopReason.TRUNCATED` 这一段由引擎的测试钉着，0080 用真实 DeepSeek 确认过真实的 `length` 会走到 `stop=truncated`（0080 §1.5 和 V8）。这次没有给模型看的文字，没有措辞要对比。

## 7. 与在办分支的关系

`feat/ask-user-r4`，预计先合入。写本计划时它的 tip 是 `f662ea2d`。它在 `_repl.py` 里有 20 个改动块，离本计划三处改动最近的是：import 段（`:110-121`，本计划加的两个名字在同一段里），`Repl.__slots__`（`:488` 一带，在新函数之后二十来行），`_pump` 开头的局部变量（`:1152` 一带，在 `_drive` 之后三十多行）。它没有碰 `_drive`、`_compaction_verdict` 和 `run_once`。把原型的 `_repl.py` 和它的做三方合并（`diff3 -m`，共同祖先是基线的文件），没有冲突；合并出来的树上提议的 15 个用例全绿，那棵树上的四个 CLI 测试文件（`tests/entry/` 下的 `test_cli_repl.py`、`test_cli_commands.py`、`test_cli_question.py`、`test_cli_card_input.py`，后两个是它新增的）共 143 条全绿（`SCR/merge_r4.sh`，`SCR/logs/out_06_merge_r4.log`、`out_07_r4_combo_proposed.log`、`out_07_r4_combo_cli.log`）。它也改 `tests/entry/test_cli_repl.py`，本计划的测试自成一节，放在文件末尾。它还改了 `docs/core-features/cli.md` §9 那张事件表（`cli.md:297-299` 一带，加一行、改一行），本计划加的一段紧跟在这张表后面，写在它的版本上。

0080 的实现改 `omicsclaw/context/transcript.py`、`omicsclaw/context/__init__.py`、`omicsclaw/entry/turn.py` 和 `entry/session.py` 的一处 docstring（0080 §6.1）。和本计划没有共同的代码文件。相交的只有两处：`CHANGELOG.md` 的顶部，以及 `Finishing` 这个测试替身。

0078 的实现只改 `transcript.py`，和本计划无关。

建议的顺序是 `feat/ask-user-r4`、0080、0081。0081 排在 0080 之后的原因：第二种形式的后半句劝用户把活拆小了再提，而 0080 落地之前，带调用的截断之后下一条消息在 DeepSeek 上会失败，用户照着做只会看到 `Failed: ProviderError`。0081 如果要先落地，第二种形式用丙。0081 和 `feat/ask-user-r4` 之间没有这种依赖，谁先都行，后落地的一方按符号名重新定位。

## 8. 这次不做的，和顺带看到的

owner 被问到时没有选、这次不做的：

- 给模型的截断提示。模型仍然不知道自己被截断过（0080 的 K1）。
- 引擎内续写或重试。
- 补 Anthropic 方言的输出上限表（0080 §3）。
- 非 `length` 的中断不算截断。这一行跟的是引擎的 `TRUNCATED`，引擎只认 `length` 和 `max_tokens`（`loop.py:93`）；`refusal`、`content_filter` 这些不会出这一行。后端截了输出却不报 `length` 时也不会出。
- 把 provider 报错的原文显示出来。

顺带看到、不在范围里的：

- 到达轮数上限的交换，CLI 上同样没有任何说明（`out_01_shapes_base.log` 的 h 段）。`TurnOutcome.hit_the_turn_ceiling`（`turn.py:142-145`）在 `omicsclaw/` 里没有调用方。列为 Q6。
- 摘要调用被截断时没有人知道（§4）。
- Channel 上，被截断且没有文字的交换会把上一次交换的回答再发一遍。`_answer` 在整段轨迹里倒着找最后一条有文字的 assistant 消息，轨迹包含带进来的历史（`SCR/logs/out_10_channel_answer_base.log`）。`TurnOutcome.reply`（`turn.py:134-140`）是同样的找法。
- `CLI_USAGE`（`_surfaces.py:275-278`）和 `docs/core-features/cli.md:113` 说 `oc cli --prompt … > answer.txt` 只写答案。实际还有 `Context: …`、`Turn N done, …` 和工具行（`out_02_oneshot_base.log`）。
- 0080 §1.3 的 P2 写"之后用同一个 `--session` 再执行就卡住"。这条路走不到：`--session` 和 `--prompt` 一起用会被拒绝（`_surfaces.py:515-519`），`run_once` 每次用一个新的会话 id（`_repl.py:563-564`、`:1891-1896`）。不影响 0080 的结论。

## 9. 需要 owner 裁定的问题

Q1 这句话用哪一种（§3）。甲分两种形式、点出工具名、带一句建议；乙一句话到底；丙只陈述不给建议。
- 我的建议：甲。

Q2 放法（§2.1）。A 是 CLI 自己读 handle，不加跨层字段；B 是给 `EXCHANGE_END` 帧加 `stop_reason`。
- 我的建议：A。Desktop 或 Channel 立项时再看 B。

Q3 一次性执行时这一行打到哪（§4）。标准输出，和 `Failed: …` 同一处，不加参数；或者标准错误，`Repl` 和 `run_once` 各多一个参数，要改 `launch/_surfaces.py`。
- 我的建议：标准输出。

Q4 一次性执行被截断时退出码要不要变。今天是 0。
- 我的建议：不变。改了就不只是显示。bench 把非零退出码判成 `INFRA_FAILURE`，这个判断排在读 `truncated` 之前（`bench/outcome.py:215-220`），退出码一变，bench 里的 `TRUNCATED` 就都成了基础设施故障。

Q5 摘要调用被截断算不算（§4）。
- 我的建议：这次不算，只记录。

Q6 轮数上限要不要在同一处顺手显示一句（§8）。代码上是多一个分支和两三条测试，另外要定一句话。
- 我的建议：不在本计划做，范围是截断。

Q7 落地顺序（§7）。
- 我的建议：排在 0080 的实现之后。

## 10. 证据清单

都在 `SCR` 下。每个探针和测试日志的第一行是 `omicsclaw.__file__`，指向被测的那棵树。

| 文件 | 内容 |
|---|---|
| `base/`、`proto/` | `git archive 5daf5482` 的两份导出，后者打了原型 |
| `prototype.diff` | 原型的完整 diff |
| `probes/p81_common.py` | 脚本化的 provider 和装配 |
| `probes/probe_01_cli_shapes.py`，`logs/out_01_shapes_{base,proto}.log` | 各种结束方式下 REPL 和 `run_once` 的屏幕 |
| `probes/probe_02_oneshot_process.py`，`logs/out_02_oneshot_{base,proto}.log` | 真实的 `oc cli -- --prompt` 进程：`stdout`、`stderr`、退出码，以及 pty 下的样子 |
| `probes/probe_03_subagent_and_summary.py`，`logs/out_03_subagent_{base,proto}.log` | 子代理被截断、摘要被截断时父代理的屏幕 |
| `probes/probe_04_repl_pty.py`，`logs/out_09_repl_pty_{base,proto}.log` | pty 里带真实输入源的交互式 REPL |
| `probes/probe_05_channel_answer.py`，`logs/out_10_channel_answer_base.log` | Channel 的 `_answer` 对被截断的交换返回什么 |
| `proposed/test_plan0081_proposed.py`，`logs/out_04_proposed_{base,proto}.log` | 提议的测试和两棵树上的结果 |
| `mutate.py`，`logs/out_05_mutations.log` | 9 处变异 |
| `merge_r4.sh`，`r4_combo/`，`logs/out_06_merge_r4.log`，`logs/out_07_r4_combo_*.log` | 和 `feat/ask-user-r4`（`f662ea2d`）的三方合并 |
| `logs/out_08_level2_{base,proto}.log` | `tests/entry tests/launch` 在两棵树上的结果 |
| `merge/ref_0080_final.md` | 引用的 0080 定稿（`fe684b26`） |
