# 计划 0054 交付记录：`ask_user` 第一阶段（CLI）

**日期**：2026-10-08。**分支**：本地 `feat/ask-user`，基线 `main` 的 `90a3bec3`，未 push，未开 PR。
**范围**：0052 T1、0052 §4.7 显示层前置步、0054 任务 A、B、C。0052 的 T0、T2、T3 与 0054 任务 D 没做，`entry/channel/` 零改动。
**状态**：独立审核通过（2026-10-08），审核之后的修复与合并见 §8；待 owner 过目。

## 1. 提交

| 提交 | 内容 |
|---|---|
| `a50711a1` | 既有漂移：`1540fca7` 改了 reviewer 描述而 golden 没重录。这是协调者单独做的提交，原样放在分支最前面 |
| `d8f7eddf` | 0052 T1：`entry/rendezvous.py`、`ApprovalBroker` 改为组合、`TurnHandle` 的共用计数器、`TurnEvent.subagent`、`expires_at`、渲染行与 CLI 提示符显示子代理名 |
| `25b8c9b5` | 显示层前置步：`inert_body` / `inert_body_note`、`width`、`WRAP_PREFIX`；删除 `inert_block` 与 `MAX_BLOCK_*` |
| `fd90772d` | 任务 A：`ToolContext.question`、`ask_question`、七个类型名、`AskUserTool` |
| `090e5c60` | 任务 B：`entry/question.py`、两个事件类型、渲染、`TurnHandle.answer`、开关（默认关）、挂载、子代理、`surface_config`、`is_interactive` |
| `141798ac` | 任务 C：REPL 的提问卡片、默认翻为开、`repl` 入口放开、`.env.example` 与文档 |

## 2. 第 0 步：按当前代码核对

行号漂移不逐条列，都按符号名重新定位了。`_repl.py` 里计划引用的行号（`_pump`、`_ask_human`、`_read_card` 等）与现状完全一致。下面是计划与现状有出入、需要处理的几条；没有哪一条让已裁定的设计做不下去，第一条的取舍留给 owner 确认。

| 计划的说法 | 现状 | 处理 |
|---|---|---|
| `foundation_tools` 末尾是 `memory_*`，`ask_user` 追加其后 | 0061 之后 `skill_env=install` 时末尾还有 `install_skill_deps`，两条既有测试钉住它紧跟 `memory_write` | `ask_user` 放在 `memory_*` 之后、`install_skill_deps` 之前。见 §5 决定 1，**请 owner 确认** |
| `_POLICY_WORDS` 是 7 个词，含单独的 `permission` | 现为 11 个：`permission gate`、`permission mode`、`PermissionMode`、`auto-approve`、`auto_approve` 等，单独的 `permission` 不在其中 | 工具描述不含这 11 个；`tests/tools/test_ask_user.py` 另把单独的 `permission` 也列为禁词，计划的变异"描述写回 permission"因此仍然会红 |
| Desktop 无回传端点、不设期限 | 0064 之后审批有 `/chat/permission` 与 `/chat/abort`。提问没有路由，线协议也没有帧 | Desktop 仍关。App 的已发布词表里已有 `ask_user_question` 这个帧名，Desktop 的提问计划可以用它 |
| 组合根只有 launch 的三个入口 | 0067 之后 `omicsclaw/evals/runner.py` 的 `eval_config` 直接调 `build_app` | 见 §5 决定 2 |
| 任务 C 要改 `test_turn.py`、`test_session.py` 的压力档测试 | 默认翻开后这两个文件不改也全绿，基线的 3 条 xfail 仍是 xfail | 没改 |
| 任务 B、C 的"要改的既有测试"清单 | 另有 5 处：`tests/launch/test_surfaces.py` 与 `test_cli_signals.py` 里 `open_prompt_source` 的替身（要接受 `interactive=`）、`test_memory_wiring.py` 一条、`tests/skillenv/test_install_wiring.py` 两条邻居断言 | 已改，都是一两行 |
| golden 只在任务 C 变 | 任务 B 也变一行：`task` 的描述由 `_WITHHELD_FROM_SUB_AGENTS` 渲染，开关关着也会提到 `ask_user` | B、C 各重录一次 |
| `CLAUDE.md` 有 CLI 说明要更新 | 0063 之后 `CLAUDE.md` 只是仓库维护约定 | 没改；用户说明写进 `docs/core-features/` 与 `AGENTS.md` 的 CLI 一节 |

计划引用的其余前置都已核实存在：`inert_line`、`inert_prose`、`approval_body`、`escape_unsafe`、`redact_credentials`、`_read_card(prompt, *, subject, settled_as, refuse)`、`_WITHHELD_FROM_SUB_AGENTS`、`validate_arguments` 不查长度与个数、`render_conversation` 丢掉只有工具调用的助手消息。没有发现哪条裁定的前提失效。

## 3. 验收证据

测试命令：`/opt/conda/envs/rapids_singlecell/bin/python -m pytest -q -p no:randomly -p no:cacheprovider <路径>`。

| 范围 | 基线 `90a3bec3` | 分支末端 |
|---|---|---|
| `tests/tools tests/entry tests/launch tests/permission tests/subagent tests/evals tests/test_*.py` | 1 failed、3819 passed、17 skipped、3 xfailed、1 xpassed | 0 failed、4068 passed、17 skipped、3 xfailed、1 xpassed |
| `tests/entry/test_desktop_*.py`（OmicsClaw 解释器） | 566 passed、3 skipped | 569 passed、3 skipped |
| `tests/skillenv tests/hooks tests/mcp tests/memory tests/observability tests/parity tests/planning tests/sandbox tests/skills`（给定范围之外，见 §5 决定 6） | 3 failed、1840 passed、9 skipped、1 xpassed | 0 failed、1843 passed、9 skipped、1 xpassed |

基线的 4 条失败都是 golden 漂移（一条在给定范围内，三条在 `tests/skillenv`）。把基线和 `a50711a1` 的树各导出到临时目录单独跑这 4 条：基线 4 failed，`a50711a1` 4 passed。

各任务的定点变异都是真改代码、跑测试、还原并核对字节一致，脚本与结果在 scratchpad 的 `mut_*.json` / `mut_*.out`：

| 任务 | 变异数 | 结果 |
|---|---|---|
| T1 | 12 | 全部转红，含计划列的 7 个 |
| 显示层 | 9 | 全部转红，含"续段用 `CONTINUATION_PREFIX`""折行先于截断" |
| A | 14 | 全部转红，含计划列的 7 个 |
| B | 26 | 全部转红，含计划列的 12 个 |
| C | 15 | 全部转红，含计划列的 6 个（其中一个换过写法，见 §6） |

其他证据：

- `tests/entry/test_approval.py` 自基线零 diff，13 条全绿。任务 B、C 没有再改 `entry/approval.py` 与 `entry/rendezvous.py`。
- `approval_body` / `approval_body_note`：新旧模块对 60,000 个随机请求输出逐字相同；`inert_body` 的宽度性质（行宽、前缀、不拆转义、截断与宽度无关）60,000 例无违反。`test_display.py` 里 `approval_body` 的既有用例没有改动。
- 工具描述与 schema 同计划 §3.2、§3.4 的代码块逐字相同（脚本比对）。
- golden：B 的 diff 是 `task` 描述里 `reads.` 变成 `reads; ask_user, which asks a person, and a sub-agent has nobody to ask.`；C 的 diff 是新增 `ask_user` 一项，42 行，删除 0 行。`deployment_prompt.txt` 始终没变。
- pty 验收（真实 `python -m omicsclaw cli`，DeepSeek，经 `AI_PROXY`，子进程确认 import 的是 worktree）：选项作答、`/data/ref.h5ad` 作自由文本、一条模型消息里两个 `ask_user` 依次出卡（第二张在第一张作答后才出现，编号 `#1`、`#2`）、提问处 Ctrl-C 取消交换后 REPL 继续回答下一行，四种情形都通过，退出码 0。回答写进了临时 HOME 下的历史文件。主检出 `.env` 前后校验和相同。

## 4. 与计划的差异

1. **`approval_body` 没有字面上调用 `inert_body`**，两者共用内部的 `_body(parts)`。理由以 `\r` 结尾且附参数时，先拼接再切行会把这个 `\r` 和连接用的换行读成一个 CRLF，卡片就少显示一个字符；为了审批输出逐字不变，保留按"部分"切行。`test_display.py` 有一条用例记录这个取舍。
2. **`ChildRunner.delegate` 的重绑包住了整个方法体**（原方法体改名 `_run_child`），不只是子引擎的迭代。这样循环体没有重新缩进，和 `feat/subagent-usage` 对 `TURN_END` 分支的改动不冲突。行为上多包住的只有 reviewer 的开关检查和归档。
3. **`.env.example` 的超时注释**没有写"`oc channel` 用 600"，因为 T0 没做；只写了提问也受这个期限约束。
4. **`reply` 在 `declined` 时也带原文**（空白行本身）。工具结果不输出它。
5. **README、CHANGELOG 没改**，按派发要求留给 owner。

## 5. 我定的决定

1. **`ask_user` 与 `install_skill_deps` 的先后**。两份计划都想排在基础工具的最后。我选了 `memory_write` → `ask_user` → `install_skill_deps` → MCP → `task`：`install_skill_deps` "appended last" 的文档和"紧挨 `task`"的断言都不用动，只把它左邻的名字从 `memory_write` 改成 `ask_user`。默认部署（`skill_env=probe`）的 golden 不受这个选择影响。反过来放要改一行代码、两条断言和一句 docstring。
2. **eval 里关掉 `ask_user`**。`eval_config` 加了 `"ask_user": False`。Runner 只答审批帧，不答提问帧；不关的话 live 路由 eval 里模型一提问就等到 `case_timeout`，工具表也会多一项。case 的 `config` 仍可覆盖。这是计划之外的一行，owner 不同意可以直接删。
3. **上限常量沿用 `MAX_APPROVAL_BODY_*` / `TALL_APPROVAL_BODY_*` 的名字**（计划留给实施时定）。改名要动既有测试，没有收益。
4. **编号语法只写一份**：`tools/builtin/ask_user.py` 的 `option_numbers` 同时用于拒绝数字 label 和 `read_reply` 读编号，免得两处各写一个正则后漂移。只认 ASCII 数字，逗号或空白分隔；全角逗号的回复按自由文本交给模型。
5. **无通道时的措辞分两层**：`ask_question` 抛 `nobody can be asked in this session`，工具再接上 `do not call ask_user again …`，通用层不出现工具名。
6. **多跑了给定范围之外的测试目录**。协调者提醒 golden 还被 `tests/skillenv` 用到，我据此 grep 出所有碰到 entry、launch、tools 的测试目录各跑一遍，才发现决定 1 涉及的两条断言。
7. **`ask_user` 不设 `rule_argument`**，规则按 schema 的首个必填字符串（`question`）匹配。

## 6. 没验证的部分和已知的小毛病

- 模型在没有明确指示时会不会恰当地使用 `ask_user`（过度提问、该问不问）没有评估，pty 验收的四条提示都点名要求调用它。审核方之后补测了几例，见 §8。
- 只在 DeepSeek 上做了 pty 验收；Anthropic、OpenAI 没测。
- `--approval-timeout` 下提问到期的路径当时只有单元测试。审核之后在 pty 里走过，并修了一个缺陷，见 §8。
- `prompt_toolkit` 的补全菜单在提问提示符上照常弹出斜杠命令（计划接受的现状），没有在 pty 里专门看。
- 屏幕上工具调用行 `(n) -> ask_user` 落在卡片与图例之间，和审批卡片上 `-> bash` 的位置相同，是既有的帧顺序。
- `<- ask_user ok, 3.3s elapsed (includes any approval wait)`：这个后缀是共用常量，对提问来说包含的是等回答的时间，措辞没有改。
- `_forget_asking` 的 ERROR 日志对提问 Task 也写 `approval task failed`。为了审批路径逐字不变没有改。
- 定点变异 C-4 的第一个写法（在提问 Task 里调 `_dispatch`）让变异体自己死锁，靠超时判红，换成直接结算的写法后由三条斜杠用例转红。

## 7. 留给第二阶段的接入点

- **放开入口**：`launch/_surfaces.py` 的 `_ASKING_SURFACES` 加 `"channel"`；T0 在同一个 `surface_config` 里加 600 s 默认值。
- **broker**：`handle.questions` 与 `handle.approvals` 同形（`settle`、`abandon`、`pending`、`expires_at`），runtime 的 `_broker(kind, handle)` 不必按 kind 分支。
- **卡片**：`question_card(request, ref, width=…)` 返回 `(正文, 是否截断)`，`ref` 由 desk 给；`reply_hint(request)` 放进 `Card.legend`；`read_reply(request, text)` 读回答。`width` 不足以在标题旁留 10 个字符时抛 `ValueError`。
- **审批卡片**：`approval_body(request, width=…)` 已可折行；`TurnEvent.subagent` 已在帧上，卡片标题可直接用。
- **截断即不出卡**：`QUESTION_TOO_LONG_REASON` 与 `AskUserTool` 把它转成 `ToolArgumentError` 的分支**没有做**，属于任务 D。做的时候 `omicsclaw.tools.__all__` 的冻结清单要再加一个名字，且这次结算不能置 `QuestionBroker._closed`。
- **理由常量**：`QUESTION_TIMEOUT_REASON`、`QUESTION_ABANDONED_REASON` 的值在 `echo` 区分 LAPSED 时会被比对，之后不要随意改。
- **事件**：`QUESTION_ASKED` / `QUESTION_SETTLED` 不在 `DEFAULT_DELIVERED_TYPES` 里，pump 要在过滤之前把它们交给 desk。
- **结构测试**：`entry/replies.py` 落地后，`test_question.py` 里互不导入的断言自动生效。

## 8. 审核之后（2026-10-08）

独立审核的结论是通过，没有必须修的问题：88 处变异里 81 处转红。存活的 7 处和审核方指出的一个缺陷用追加提交处理，已审过的提交没有改写。

| 提交 | 内容 |
|---|---|
| `38f9cdc7` | 提问到期后收回提示符，附测试与 `cli.md` §7.4 的说明 |
| `d7b39fe2` | 存活的 7 处变异各补测试，不改生产代码 |
| `b8ccfd5f` | `question_card` 的宽度报错改报调用方传的值；`TurnHandle._numbering` 的说明改成注释；一条记忆工具测试改名 |

**提问到期后收回提示符。** 设了 `--approval-timeout` 时，提问会在提示符还开着的时候被期限结算。修之前，读回答的 Task 留到 exchange 结束：`No answer` 打在开着的提示符那一行，活动行不再出现，迟到的回答被读走后丢弃。现在 `_pump` 收到 `QUESTION_SETTLED` 先取消并等待这个 Task，再打印这一帧。只改了提问侧；审批卡片到期后提示符仍然留着，`cli.md` §7.4 两种情况都写了。

- 审核方的 `late_answer.py question`：修前 `events` 是 `["typed at the stale prompt 'answer [#1]> '"]`，修后是 `[]`；`late_answer.py approval` 前后相同。
- 真实终端（pty，DeepSeek，`--approval-timeout 12`）：修前 `No answer` 盖在提示符所在行，没有活动行，迟到的 `2` 混进模型正在输出的文字里，随后被丢弃；修后 `No answer` 另起一行，活动行恢复，迟到的 `2` 由主提示符读走，作为下一条消息发给模型。

**存活的 7 处变异**按审核方脚本里的原文重做，全部转红：

| 变异 | 转红的测试 |
|---|---|
| R6：`abandon` 不置 `_abandoning`，日志写成 settled | `test_approval.py::test_the_log_tells_an_abandoned_question_from_an_answered_one` |
| E17：`is_interactive` 的异常分支返回 `True` | `test_cli_input.py::test_only_a_stream_that_says_it_is_a_terminal_is_interactive`（两例）、`test_a_process_with_no_stdin_is_not_interactive` |
| C3：去掉 `finally: activity.release()` | `test_cli_question.py::test_the_live_line_comes_back_once_the_question_is_answered` |
| C4：提示符里的子代理名不经 `inert_line` | `test_cli_repl.py::test_a_sub_agent_s_name_reaches_the_prompt_as_inert_text` |
| C7：不调 `activity.hold()` | `test_cli_question.py::test_nothing_is_painted_while_a_question_is_open` |
| C8：`read_reply` 收到 `line.strip()` | `test_cli_question.py::test_a_line_typed_at_the_question_is_the_answer[a number with spaces around it]` |
| C9：提问 Task 不挂 `_forget_asking` | `test_cli_question.py::test_a_failed_question_task_is_logged_and_not_merely_dropped` |

`tests/entry/test_cli_input.py` 不在审核方脚本的 FOCUS 列表里，复跑 E17 时要把它加上，或用 `BROAD=1`。

只记录、这一轮不改的三条：

- **"`ask_user` 已关闭"的提示没人看得到。** `surface_config` 在不能提问的入口关掉它时写一条 INFO 日志，这条日志在 CLI 和 Desktop 上都不上屏。有人传 `--ask-user true --prompt …`，不会得到任何提示。
- **`/auto` 开着、没设期限、又没人作答时会一直等。** 审核方实测 90 秒内没有任何输出，Ctrl-C 能回到提示符。无人值守时设 `OMICSCLAW_ASK_USER=false`。
- **真实模型的使用情况**（审核方实测，DeepSeek）：6 个不同场景里 2 个提了问，两次都是先查看再问，问的都是只有人能定的事；同一个有歧义的请求跑 3 次，3 次都问了。

**合并 `main`。** `58edf3e6` 把 `main` 的 `e1be31c4` 合进分支，用的是合并提交，之前的提交原样保留。冲突两处：`entry/turn.py` 的 import 块（`main` 把 `build_injector` 换成 `build_augmentor`，分支在相邻一行加了 `QuestionBroker`，两条新 import 都留下）；`cli.md` 事件表相邻两行（控制帧一行留分支的 `QUESTION_SETTLED` 说明，`TURN_END` 一行留 `main` 关于子代理用量的说明）。其余两边都改过的 11 个文件自动合并。

合并后的树上各跑一次：

- 测试：`tests/tools tests/entry tests/launch tests/permission tests/subagent tests/skillenv tests/memory tests/context tests/planning tests/evals tests/bench tests/test_*.py` 为 5447 passed、23 skipped、3 xfailed、2 xpassed、0 failed；Desktop 为 569 passed、3 skipped。
- golden：`deployment_prompt.txt` 与 `main` 相同；`deployment_tools.json` 相对 `main` 只多 `ask_user` 一项和 `task` 描述里的半句。
- 真实的 `oc cli -- --prompt-file` 进程（bench 的启动方式，标准输入关闭）交给模型 11 个工具，没有 `ask_user`，加 `--ask-user true` 也一样。
- 记忆提醒只在发给模型的副本里。一次 exchange 里先后出审批卡和提问卡、`memory_nudge_turns=2` 时，提醒只出现在第 3 次模型调用，不进保存的历史，也不上屏。
- pty（DeepSeek）：原来的四种情形通过；`--approval-timeout 12` 下无人作答、到期后再输入的情形通过，表现同上面"修后"。
