# 计划 0054 交付记录：`ask_user` 第一阶段（CLI）

**日期**：2026-10-08。**分支**：本地 `feat/ask-user`，基线 `main` 的 `90a3bec3`，未 push，未开 PR。
**范围**：0052 T1、0052 §4.7 显示层前置步、0054 任务 A、B、C。0052 的 T0、T2、T3 与 0054 任务 D 没做，`entry/channel/` 零改动。
**状态**：独立审核通过（2026-10-08），审核之后的修复与合并见 §8；第二轮审核有条件通过，它的修复见 §9；第三轮复核认定还有两条路开着，owner 裁定后的修复见 §10；第四轮审核有条件通过，owner 再作三条裁定，修复见 §11；第五轮复核通过，文档和测试上的补正见 §12，待 owner 过目。

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

这一节下面两条实测都只在 prompt_toolkit 下做过。第二轮审核指出：没装 prompt_toolkit 时 `No answer` 仍在提示符那一行；迟到的那一行若遇上的是审批卡而不是主提示符，会替审批卡作答。两处的修复和两种输入源各自的行为见 §9。

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

- 测试：`tests/tools tests/entry tests/launch tests/permission tests/subagent tests/skillenv tests/memory tests/context tests/planning tests/evals tests/bench tests/test_*.py` 为 5447 passed、23 skipped、27 deselected、3 xfailed、2 xpassed、0 failed；Desktop 为 569 passed、3 skipped。这条命令没有带 `-o addopts=""`，`pyproject.toml` 默认的标记过滤排除了 27 条：`tests/evals/live/test_live_routing.py` 的 26 条和 `tests/test_setup_env_script.py` 的 1 条 slow。带上这个参数时这 27 条被收集，然后各自跳过，所以同一组测试是 50 skipped。
- golden：`deployment_prompt.txt` 与 `main` 相同；`deployment_tools.json` 相对 `main` 只多 `ask_user` 一项和 `task` 描述里的半句。
- 真实的 `oc cli -- --prompt-file` 进程（bench 的启动方式，标准输入关闭）交给模型 11 个工具，没有 `ask_user`，加 `--ask-user true` 也一样。
- 记忆提醒只在发给模型的副本里。一次 exchange 里先后出审批卡和提问卡、`memory_nudge_turns=2` 时，提醒只出现在第 3 次模型调用，不进保存的历史，也不上屏。
- pty（DeepSeek）：原来的四种情形通过；`--approval-timeout 12` 下无人作答、到期后再输入的情形通过，表现同上面"修后"。

## 9. 第二轮审核之后（2026-10-08）

第二轮独立审核对 `26f13823..31bea84a` 的结论是有条件通过，有一个必须修的问题。修复追加在本地分支 `feat/ask-user-r3` 上，从 `31bea84a` 开出。`31bea84a` 及之前的提交没有改写，没有 rebase，没有再合并 `main`，没有 push。

| 提交 | 内容 |
|---|---|
| `f2d26dc5` | 输入源新增可选协议 `FreshSource`：`read_fresh` 只读提示符打开之后键入的行，`withdraw` 收起被取消的读留下的提示符。两种终端输入源各自实现，这一步还没有调用方 |
| `9a4edd80` | 卡片改用 `read_fresh` 读，有输入被丢弃时打印一行提示；提问到期收回提示符时调用 `withdraw` |
| `6a5e4094` | 第二轮存活的 6 处变异各补一条测试，不改生产代码 |
| `7cce3650` | `question_card` 的 docstring 写清哪种 width 抛哪种错，附测试，行为不变 |
| `1baae5b9` | `cli.md` 新增 §7.5，改写 §7.4 的相关几条；`AGENTS.md` 的 CLI 一节同步。`AGENTS.md` 里两段的先后在本记录之后的一个提交里理顺，只动文字 |

### 9.1 卡片只接受它出现之后键入的内容

审核方发现：提问到期、提示符收回之后，补敲的那一行留在终端里，被下一个打开的提示符读走。下一个若是审批卡，这一行就替它作了答。对照实验显示，没有提问时在工具运行中提前敲 `yes` 回车，同样会批准下一张审批卡，这在 `26f13823` 上就有；§8 的收回提示符给它加了一条由产品自己引出的触发路径。owner 选定的修法是卡片只接受它出现之后键入的内容。

动手之前在 `31bea84a` 的导出树上复现过。真实模型（DeepSeek，`--approval-timeout 15`）下，prompt_toolkit 与回退源都是提问到期后敲 `yes` 回车，bash 审批卡一打开就 `Approval granted`，卡上没有键入任何东西。

做法分两层。输入源一层（`entry/cli/_input.py`）新增可选协议 `FreshSource`，用法与 `ChoiceSource` 相同：`Repl._read_at_card` 发现输入源实现了它，就用 `read_fresh` 读卡片，否则退回 `read`。丢弃发生在这个读拿到终端之后、提示符上屏之前，所以两张卡排队时，第二张丢弃的是它自己的提示符打开之前键入的全部内容。主提示符仍用 `read`，没有卡片打开时提前或迟到键入的行照旧是下一条消息。

| 提示符打开之前已有的输入 | prompt_toolkit | 没装 prompt_toolkit（终端上的 `StreamSource`） |
|---|---|---|
| 敲完并回车的行 | 丢弃，有提示行 | 丢弃，有提示行 |
| 敲了一半、没回车的字 | 丢弃，有提示行 | 丢弃，没有提示行 |
| 上一个提示符多读到、prompt_toolkit 留给下一个提示符的键 | 丢弃，有提示行 | 不适用 |
| 被取消的读留下的 `readline` 已经读走的那一行 | 不适用 | 丢弃，有提示行 |

表里说的是提示符打开之前的那一半。半行被丢弃之后，在卡片上键入的后半截这一轮仍然是这张卡的回答，§10.3 改了 prompt_toolkit 那一路。

- prompt_toolkit：`_drop_keys` 先取走它自己的预读缓存（`get_typeahead`），再在 raw 模式里用 `Input.read_keys` 把终端里等着的键读空，半行在 raw 模式下一并读出。终端自己回的光标位置报告不算键入，丢弃但不提示。
- `StreamSource`：`_drop_typed` 先 `termios.tcflush` 清空终端输入队列，整行和半行都清掉。能数出来的只有整行（`FIONREAD`），所以半行被丢弃时没有提示行。然后给被取消的读留下的 `readline` 线程 0.05 秒报告：这段时间内报告的行是提示符打开之前键入的，丢弃；没有报告的线程还在等下一行，留给这张卡用，不另起第二个线程。

**标准输入不是终端时没有这条规则。** 管道和文件（`oc cli < questions.txt`）里的行是写脚本的人事先按顺序排好的，没有先后可言；丢弃的话，管道里每张审批卡都会读到输入结束而被拒。所以 `StreamSource.read_fresh` 在流不是终端时与 `read` 相同。`ScriptedSource` 不实现 `FreshSource`，测试和一次性执行里的卡片读它的下一行。`tests/entry` 里经卡片作答的既有用例都用 `ScriptedSource` 或它的子类，这一轮没有一条既有的审批测试需要改，也没有哪条既有测试钉住了提前键入。

**提示行。** 有输入被丢弃时，卡片的图例与提示符之间打印一行弱化的 `input typed before this prompt was discarded`。提前敲了 `y` 的人看到卡片还在等，从这一行知道原因。输入源看不到丢了什么的时候不打印，目前只有回退源上的半行是这种情况。

### 9.2 回退输入源的显示

没装 prompt_toolkit 时，提问到期后 `No answer` 接在提示符那一行后面，到期前敲了一半的字留在终端里，和之后的回车拼成一行发给了模型。现在 `_retract_question` 在读回答的 Task 结束时调用输入源的 `withdraw`：`StreamSource` 补上提示符后面缺的换行，并 `tcflush` 清掉半行；prompt_toolkit 在 `prompt_async` 被取消时自己换行并丢掉缓冲区里的字，它的 `withdraw` 什么都不做。只在收回提问时调用，exchange 结束时的清理不调用，所以审批卡那一侧的显示没有变化。回退源上在提问处按 Ctrl-C 的显示也没有变化，修前修后都是 `answer [#1]> ^CCancelled.`。

没有 `termios` 的平台上 `StreamSource` 清不了终端输入队列，§9.1 与本节在那里都做不到，`cli.md` §7.5 写了这一条。

### 9.3 变异

审核方 `mutate2.py` 里存活的 6 处，用它的脚本原样在 `1baae5b9` 上重做，全部转红。`_retract_question` 新增的那一步挂在 Task 的 done 回调上，`task.cancel()` 与 `gather` 两行没有动，所以审核方两张表的 115 处变异在新树上都能原样套用。

| 变异 | 转红的测试 |
|---|---|
| N5：去掉清 `_replying` 的 done 回调 | `test_cli_question.py::test_a_question_cut_short_with_the_repl_is_no_longer_held` |
| N6：先 `activity.clear()` 再收回 | `test_cli_question.py::test_a_frame_painted_as_the_prompt_comes_down_is_erased_before_no_answer` |
| N13：审批卡也登记并在 `APPROVAL_SETTLED` 时收回 | `test_cli_repl.py::test_an_approval_card_s_prompt_stays_open_past_its_deadline` |
| N14：取消后只让出一次 | `test_cli_question.py::test_no_answer_waits_for_a_prompt_that_is_slow_to_come_down` |
| N15：`gather(return_exceptions=True)` 改成 `await task` | `test_cli_question.py::test_a_reading_task_that_fails_as_it_is_taken_down_does_not_end_the_exchange` |
| N21：报错里表头长度少算 `": "` | `test_question.py::test_the_refusal_says_how_long_the_card_s_header_is` |

N5 只在泵没有处理到 `QUESTION_SETTLED` 的路径上才有区别，因为 `_retract_question` 自己也会把这一项弹出。测试取的是运行 REPL 的 Task 被取消的情形，也就是 `SIGTERM` 的路径。

审核方 N 系列另有 5 处在它的记录和这一轮都存活，不在要求补测的 6 处之内：N3、N9、N10、N11、N22。前四处在现有代码下行为等价（已结束的 Task 再取消一次无效；多收回一次时该项已经弹出；审批 Task 登记了也没有人读；`get` 之后 done 回调照样弹出），N22 的分支在 `width=None` 时走不到。其余 7 处仍然转红。第一轮表里落在这一轮改过的三个文件上的 18 处也重跑了，全部转红。

这一轮新写的生产代码自己做了 43 处变异：prompt_toolkit 源 12 处、`StreamSource` 23 处、REPL 8 处。每次还原后比对 SHA-256，在 `1baae5b9` 上全部转红，转红的都是针对该处的测试。

### 9.4 验收

真实终端（pty）里两种输入源各走一遍。每次运行先用同一环境的探针确认输入源和代码来路：一路是 `PromptToolkitSource`；另一路在 `PYTHONPATH` 最前面放一个导入即抛 `ImportError` 的 `prompt_toolkit` 桩，得到 `StreamSource`。两路的 `omicsclaw.__file__` 都指向本 worktree。

真实模型（DeepSeek，经 `AI_PROXY`），两种输入源结果相同：

- 迟到的 `yes` 遇上审批卡（`--approval-timeout 15`）：审批卡在模型的下一条消息里，`yes` 敲在它打开之前。提问到期后敲 `yes` 回车，bash 审批卡打开后 3 秒内没有被结算，提示行出现一次；之后在卡上敲 `y` 才 `Approval granted`，命令执行。
- 工具运行中提前敲 `yes` 回车：第一张卡出现后敲 `y` 正常批准；它的命令还在跑时敲 `yes` 回车；第二张卡打开后 3 秒内没有被结算，有提示行，卡上敲 `y` 才批准。
- 第一阶段的五种情形：选项作答；`/data/ref.h5ad` 作自由文本并被原样复述；一条模型消息里两个 `ask_user`，第二张卡出现后才键入，编号 `#1`、`#2`；提问处 Ctrl-C 取消后 REPL 接下一行并作答；设期限无人作答，到期后键入的 `2` 由主提示符读走，成为下一条消息。退出码都是 0。

脚本化后端（真实终端，时序确定）12 种情形乘两种输入源，其中 10 种在 `31bea84a` 上也跑了一遍，屏幕归一化后逐一比对：

- 不该变的情形修前修后屏幕相同：审批卡到期后再键入、卡片出现后批准、期限内作答，以及 prompt_toolkit 下的迟到行和到期前的半行。
- 回退源上，迟到行那一例只多了提示符后面的换行；到期前的半行那一例多了换行，`Lou` 不再发给模型。
- 提前键入整行、提前键入半行、一次键入两行、两张提问卡之间键入、迟到的 `yes` 遇上审批卡：修前卡片被提前键入的内容结算，半行那一例是拼进了回答；修后卡片保持打开，卡上键入的才算。半行那一例验的是提前敲的半行不再拼进回答：prompt_toolkit 有提示行，回退源没有，在卡上只按回车，审批卡得到拒绝，提问卡记为跳过（`declined`）。半行之后在卡上把词敲完的情形当时没有验。
- 只在修后跑的两种：提问到期后什么都不敲，等审批卡出现再敲 `y`，正常批准，之后主提示符照常读下一行；提问到期后敲半行，等审批卡出现只按回车，得到拒绝。

审核方首轮的 `late_answer.py`：`question` 与 `approval` 两种在 `31bea84a` 与本分支上的输出，归一化 turn id 之后逐行相同。它的输入源是 `ScriptedSource` 的子类，不实现 `FreshSource`，卡片照旧用 `read` 读。

测试按 Risk-Matched Verification 第 3 档选：改动落在 `entry/cli`，被 `launch` 导入，没有碰权限门、工具表和契约文本。命令是 `python -m pytest -q -p no:randomly -p no:cacheprovider -o addopts="" <路径>`。

| 范围 | `31bea84a` | `1baae5b9` |
|---|---|---|
| `tests/entry tests/launch`，不含 `tests/entry/test_desktop_*.py` | 1954 passed、2 skipped、3 xfailed | 2002 passed、2 skipped、3 xfailed |
| `tests/entry/test_desktop_*.py`（OmicsClaw 解释器） | 569 passed、3 skipped | 569 passed、3 skipped |
| 15 个分层守卫、顶层 `tests/test_*.py`、`tests/evals`，加 `-m "not slow and not demo and not eval and not skill_example"` | 950 passed、14 skipped、27 deselected、1 xpassed | 950 passed、14 skipped、27 deselected、1 xpassed |

`tests/entry/golden/` 零 diff。第一行在提交之前、机器负载很高时还跑过一次，有 2 条失败，都不在这一轮改动的路径上：一条是 §9.5 里记的那条靠 `sleep(0)` 计数的测试，另一条是 `tests/launch/test_channel_command.py::test_the_channel_command_really_assembles_an_agent_before_it_serves`，子进程在打出 `ChannelManager started` 之前就收到了信号。后一条没有进一步查。

没有跑的：全量测试；`tests/tools`、`tests/permission`、`tests/subagent` 等这一轮没有碰到的层；Anthropic、OpenAI 两个 provider；Windows。

这一节验过的"迟到的 `yes`"和"提前敲 `yes`"，都是 `yes` 在卡片打开之前敲完的情形。第三轮复核在真实模型下找到另外两种当时没有关上的：
审批卡和提问在同一条模型消息里，卡片在提问到期的同一刻打开，迟到的 `yes` 是敲在已经打开的卡上的；以及卡片打开时一个词敲到一半，
后半截落在卡上。修复和仍然开着的部分见 §10。

### 9.5 只记录，这一轮不改

- **读回答的 Task 在第一步之前被取消时，活动行的占用不释放。** 直接构造能复现；审核方经真实 broker 压测 600 次没有出现。
- **Ctrl-C 与收回落在同一轮事件循环时 Ctrl-C 丢失。** 审核方模拟出来的，没有用真实 prompt_toolkit 复现。
- **`rendezvous.py` 的 `settle()` 返回 True 之后，同一轮里期限仍会赢。** 回答丢失，结果是 `no_answer`。`26f13823` 上就有。
- **回退源上的半行没有提示行。** 终端在回车之前不上报这半行，`tcflush` 能清掉它，数不出它。提前敲了 `y` 没回车的人，在卡片上只按回车：审批卡得到一次拒绝，提问卡记为跳过（`declined`），模型被告知不要再问。这一轮结束时两种输入源都是这样；§10.3 之后 prompt_toolkit 下这个回车被丢弃，卡片继续等。
- **回退源上被读线程拿走、0.05 秒内没报告的行会被当作卡片的回答。** 需要读线程在这段时间里一直没被调度到，而且回车恰好落在卡片打开之前的一瞬间。没有复现。
- **回退源上收回提示符时可能留下一帧活动行。** 读回答的 Task 结束到 `withdraw` 之间若正好落下一次 tick，这一帧画在提示符那一行，换行之后不再被擦掉。只是推演，没有复现；prompt_toolkit 下这一帧会被 `activity.clear()` 擦掉。
- **两条既有测试在高负载下会偶发失败。** `test_cli_question.py::test_a_question_nobody_answered_does_not_outlive_its_exchange` 与 `test_cli_repl.py::test_an_approval_nobody_answered_does_not_outlive_its_exchange` 用固定次数的 `sleep(0)` 等提示符出现，机器忙时等不到。同一时段在 `31bea84a` 的导出树和本分支上各跑 25 次，两边都是 11 次失败。没有改。
- **没有 `termios` 的平台没有验证。** prompt_toolkit 那一路走它自己的输入接口，不调用 `termios.tcflush`，但没有在 Windows 上跑过。

## 10. 第三轮复核之后（2026-10-09）

第三轮复核认定 §9 之后还有两条路能让一张审批卡被没看到它的人批准，都在真实模型下复现过，另提了三条 P3。owner 2026-10-09 裁定只治这两处。修复追加在本地分支 `feat/ask-user-r4` 上，从 `d69edae4` 开出。`d69edae4` 是 `feat/ask-user-r3` 的 tip，那个分支名被另一个 worktree 占着。`d69edae4` 及之前的提交没有改写，没有 rebase，没有再合并 `main`，没有 push。

| 提交 | 内容 |
|---|---|
| `2198c929` | 提问到期后，同一条模型消息里排在它后面、需要审批的调用直接拒绝，不打开提示符 |
| `ff0ac1c8` | `FreshSource.read_fresh` 新增 `unfinished` 回调。prompt_toolkit 输入源发现提示符打开前有一行敲到一半，就把后半截一起丢到下一个回车，卡片多一行提示 |
| `22facde6` | 第三轮存活的 3 处变异各补一条测试，不改生产代码 |
| `f54d2dd9` | 这一轮自己的变异里存活的 1 处：把对应的测试改到能发现它，不改生产代码 |
| `70beafc6` | `cli.md` §7.4、§7.5 与 `AGENTS.md` 的 CLI 一节改成实际关到的程度 |

本记录在这之后的一个提交里。`entry/approval.py`、`entry/question.py`、`entry/turn.py`、`omicsclaw/permission/`、`omicsclaw/tools/` 和 `tests/entry/golden/` 相对 `d69edae4` 零 diff。这是 §10 结束时的状态，`entry/turn.py` 在 §11 里改了。

### 10.1 修前复现

在 `d69edae4` 的导出树上，四种情形两种输入源各走一遍。

真实模型（DeepSeek，`--approval-timeout 15`）：

| 情形 | prompt_toolkit | 没装 prompt_toolkit |
|---|---|---|
| 一条消息里 `ask_user` 加 `bash`，提问到期后 0.5 秒敲 `yes` 回车 | `Approval granted`，命令执行，没有提示行 | 同左 |
| 提问到期后敲 `ye`，下一条消息的 bash 卡打开 0.25 秒后敲 `s` 回车 | 批准，并打印 `Will not ask about bash again in this conversation`；有提示行 | 同样批准并放行，没有提示行 |

脚本化后端（真实终端），按 100 毫秒一个键敲 `y`、`e`、`s`、回车，起点相对提问期限或工具结束挪动：

| 情形 | 偏移 | 每种输入源的结果，两种源相同 |
|---|---|---|
| 提问与审批在同一条消息（`typing_through_deadline`） | 9 个，-0.40 到 +1.00 秒 | 7 次批准：5 次是整个 `yes`，2 次是剩下的 `s`，放行了整个工具。2 次拒绝：一次只剩回车，一次剩 `es` |
| 工具结束后下一条消息的审批卡（`straddle_timed`） | 12 个，-0.45 到 +0.15 秒 | 6 次批准：3 次是四个键都敲在卡片打开之后，3 次是剩下的 `s`。6 次拒绝：3 次只剩回车，3 次剩 `es` |

### 10.2 同一条消息里排在到期提问之后的调用：直接拒绝

`Repl._pump` 读到理由是 `QUESTION_TIMEOUT_REASON` 的 `QUESTION_SETTLED` 时记下一个标记，到这条消息的 `TURN_END` 清掉。标记在的时候到达的 `APPROVAL_REQUIRED`，`_ask_human` 带着 `unasked` 交给 `_ask`：不打印图例，不打开提示符，以下面这句话拒绝。

```
nobody was asked, because the question earlier in the same message got no answer; make the call again in a later message if it is still needed
```

模型在工具结果里读到的是 `tool 'bash' raised ApprovalDenied: bash was not approved: nobody was asked, …`。"same message" 和 "later message" 是 `ask_user` 的工具描述里已有的说法。屏幕上，卡片正文照常打印，下面一行 `Approval denied [...]: nobody was asked, …`。

放在 REPL 这一层，理由三条：

- 这条路是终端造成的。到期的提示符和新开的提示符读同一个键盘，键入的一行不带卡片编号。`ask_user` 现在也只在终端的 REPL 里挂载。
- 一个调用本来会不会打开提示符，只有 REPL 知道全：用 `s` 放行的工具记在 `Repl._granted` 里，broker 看不到。
- 生产代码只动 `entry/cli/_repl.py` 一个文件，所有入口共用的审批通道没有变。

"同一条消息"靠帧的顺序判定。`ask_user` 独占一批（`concurrency_safe=False`），引擎跑完一条消息的全部工具才发 `TURN_END`（`engine/loop.py`），所以到期的 `QUESTION_SETTLED` 和下一个 `TURN_END` 之间的 `APPROVAL_REQUIRED` 都来自同一条消息。标记是 `_pump` 的局部变量，随 exchange 结束。子代理的回合不在父流里发 `TURN_END`，标记不会在 `task` 运行中途被清掉。

"因期限到期"靠 `status is NO_ANSWER` 加理由等于 `QUESTION_TIMEOUT_REASON` 判定，§7 已经写明这个常量会被比对。Ctrl-C、输入结束、输入源出错、exchange 结束时的 `no_answer` 理由不同，不触发。

不变的行为逐条核实过：

| 情形 | 行为 | 怎么核实的 |
|---|---|---|
| 提问被回答，或被空行跳过 | 审批卡照常打开 | `test_a_question_that_got_a_reply_leaves_the_call_after_it_asked_about` 两例；pty `in_time_same_message` 两种源修前修后相同 |
| 提问因输入源出错而问不出来 | 审批卡照常打开 | `test_a_question_unanswered_for_another_reason_leaves_the_call_asked_about` |
| 提问处 Ctrl-C | 取消整个 exchange，没有后面的调用 | 既有测试，没有改 |
| 不需要审批的调用 | 照常执行 | 既有的 `test_a_question_whose_deadline_passes_has_its_prompt_taken_down`：到期提问之后的 `gated` 工具照常运行 |
| `auto-approve` 下不询问的调用 | 照常执行，没有 `APPROVAL_REQUIRED` 帧 | `test_under_auto_approve_the_call_after_the_question_runs` |
| `auto-approve` 下仍要询问的调用（危险命令、`ask` 规则、受保护文件） | 和 `default` 下一样被拒绝 | §10 当时没有单独跑。§11 补了 `test_a_call_that_is_always_asked_about_is_refused_in_either_mode`，用 `ask` 规则在两种模式下各跑一例 |
| 被 `allow` 规则放行的调用 | 照常执行 | 没有单独跑。门直接放行，不产生帧 |
| 用 `s` 放行过的工具 | 照常执行，打印 `allowed for this conversation` | `test_a_tool_allowed_for_the_conversation_runs_after_the_question` |
| 子代理发起的审批 | 照常询问 | `test_a_sub_agent_s_call_after_the_question_is_asked_about` |
| 下一条消息里的审批 | 照常询问 | `test_the_call_made_again_in_a_later_message_is_asked_about`，既有的 `test_a_late_reply_to_a_question_does_not_approve_the_card_that_follows` |
| Desktop、Channel、管道、`--prompt` | 不挂载 `ask_user`，没有会到期的提问 | 相关代码没有动 |

子代理这一条裁定里没有写，是我定的。它的审批不拒绝，是因为它的卡片要等子代理先调用一次模型才会打开，和下一条消息里的卡片是同一种情形。拒绝的话，子代理读到"在后面的消息里重新调用"，重新调用时父消息的 `task` 还没结束，又被拒绝。

考虑过又放弃的做法：

- **放在 `ApprovalBroker`，连 `APPROVAL_REQUIRED` 帧都不发。** 屏幕更干净，但 broker 看不到 `s` 放行，已经放行的工具也会被拒；改的是所有入口共用的通道，等于替第二阶段的 Channel 先做了决定。
- **放在工具层或权限门。** 每个调用在自己的 Task 里拿到的是上下文的副本，"这条消息里有提问到期"传不过去，要动引擎或上下文；碰权限门还要把验证升到第 4 档。
- **延后打开提示符。** owner 已经否决。
- **连卡片正文也不打印。** 泵里要再判一次这个调用是不是已经被 `s` 放行，而且来的人看不到被拒的是什么。现在的做法和 `s` 放行对称：正文照打，一行说明，不开提示符。owner 2026-10-09 裁定维持现状（§11.3）。
- **在 `ask_user` 的工具描述里写明这条规则。** 会改契约文本和 golden。模型从被拒调用的结果里得知，实测见 §10.6。

### 10.3 提示符打开时敲到一半的行

`_drop_keys` 现在返回两个值：有没有人按过键，以及最后一个回车之后有没有字符或粘贴。回车认 `ControlM` 和 `ControlJ`：终端在行模式下把回车存成换行，raw 模式读出来是后者。方向键、Esc 这类不往行里放字的键不算开了一行，所以提前按过 Esc 或方向键之后，卡片上敲的第一个 `y` 照旧批准：既有的 Escape 用例没有改，pty `arrow_before` 修前修后相同。

`PromptToolkitSource.read_fresh` 在一次持锁之内做完三件事：丢弃并调用 `discarded`；有半行时调用 `unfinished`，打开提示符读一行丢掉；再打开提示符读真正的回答。两次提示符之间不放锁，排在后面的卡片插不进来。卡片在第一行提示下面多打印一行 `its last line had no Enter: what is typed up to the next Enter is discarded too`。

用"多读一行丢掉"而没有在提示符里吞键，是因为吞键要拦住每一个按键绑定，Ctrl-C 也在其中。多读的那一行就是一个普通的提示符：回显、Ctrl-C、输入结束都照旧，pty 里验过等回车时按 Ctrl-C 得到 `interrupted at the terminal` 并取消 exchange。代价有两条：被丢弃的后半截回显在第一行提示符上，并且和其他输入一样写进历史文件。

提前敲了 `y` 没回车、到卡片上只按回车的情形随之变了：prompt_toolkit 下这个回车属于被丢弃的那一行，审批卡和提问卡都继续等。

**回退输入源做不到，确认过。** `StreamSource` 让终端留在行模式，用 `readline` 读。探针在内核 5.4 的伪终端上敲 `ye` 不回车：`FIONREAD` 是 0，`select` 不就绪；`tcflush` 之后敲 `s` 回车，`readline` 得到 `s\n`。它数不出半行，也分不出后来的一行是不是后半截。简报说"做不到"，准确的说法是在行模式里做不到：同一个探针关掉 `ICANON` 之后 `FIONREAD` 是 2。那样做等于给回退源另造一套 raw 模式的读法，还要处理被取消的读留下的 `readline` 线程：模式一切换它可能被唤醒并把半行读走，这一点是按内核的行为推的，没有实测。按裁定没有做，只写进文档。

### 10.4 文档与三条 P3

- `cli.md` §7.4：删掉"提示符收回之后才键入的 `y` 也批准不了后面的审批卡"和"到期之后的整行遇到卡片一定被丢弃"两处说法，改成按键入那一刻有没有卡片开着分三种去向；新增同一条消息里直接拒绝的规则。
- `cli.md` §7.5：表里半行一行改写；新增敲到一半的行、回退源的限制、规则拦不住的三种情形、伪终端脚本。
- `AGENTS.md` 的 CLI 一节同步。
- 本记录 §9.1、§9.4、§9.5 各补一处，说明当时验的是哪种情形。

三条 P3：

- **提问卡上"提前敲了没回车、到卡上只按回车"被记成跳过。** `d69edae4` 上两种输入源都是这样（pty `ask_half_enter`：`Skipped`）。§9.4、§9.5 补上了提问卡这一半。这一轮之后 prompt_toolkit 下卡片继续等，回退源不变。
- **三处没被测试钉住的代码**，见 §10.5。
- **用伪终端提前写入回答的脚本。** 写进 `cli.md` §7.5 末尾和 `AGENTS.md`：回答被丢弃，卡片一直等，脚本要等提示符出现再写。

### 10.5 变异

审核方 `mutate3.py` 里存活的 3 处，各补一条测试：

| 变异 | 转红的测试 |
|---|---|
| X12c：队列里已经清出一行时，不再等被取消的读留下的 `readline` | `test_cli_input.py::test_a_line_the_cancelled_read_took_is_dropped_while_another_still_waits` |
| X19：`tcflush` 失败时报告"丢弃了" | `test_cli_input.py::test_a_queue_that_could_not_be_emptied_is_not_reported_as_dropped`（两例） |
| X22：`_drop_keys` 不进 raw 模式 | `test_cli_input.py::test_keys_waiting_at_a_real_terminal_are_read_in_raw_mode` |

X12c 的测试等到终端队列的字节数显示线程已经取走第一行才开始读，变异不会因为时序侥幸通过。X22 的测试在行模式的伪终端上敲半行，那是两个提示符之间终端所处的状态。

审核方 X 系列 46 处在 `f54d2dd9` 的导出树上原样重跑：39 处转红。另 7 处的原文被这一轮改写，套不上，各由这一轮的一处同义变异覆盖，也都转红：X23 对 I9，X25b 对 I3b，X26 对 I26，X27 对 I27，X28 对 I28，X31 对 R25，X32 对 R24。

这一轮新写的生产代码自己做了 56 处变异：`_repl.py` 27 处（拒绝 21 处，第二行提示 6 处），`_input.py` 29 处。每次还原后比对 SHA-256。在 `22facde6` 上 55 处转红，存活 1 处：I18，把两次提示符之间的锁放开再重新取。原来的测试只看两张卡按顺序作答，放锁之后每个读者都同样让出一次，顺序没变；变的是排队的读者丢弃输入的时刻提前到了第一张卡作答之前。`f54d2dd9` 把第一张卡的回答和它后面的一行一起送进去，排队的卡片必须在自己的提示符打开时丢掉那一行。在 `f54d2dd9` 上三张表重跑，56 处全部转红。

变异跑的是 `tests/entry/test_cli_*.py` 加 `tests/launch/test_cli_signals.py`、`tests/launch/test_surfaces.py`，带 `-x`。每张表先在没改动的树上跑一遍，确认是绿的再开始。

### 10.6 验收

按 Risk-Matched Verification 第 3 档：改动落在 `omicsclaw/entry/cli`，被 `launch` 导入；没有碰权限门、工具表、契约文本和 golden。新增的是一条不询问就拒绝的路和多丢弃的一截输入，两处都只减少能批准的方式。命令是 `python -m pytest -q -p no:randomly -p no:cacheprovider -o addopts="" <路径>`。

| 范围 | `d69edae4`（导出树） | 分支末端 |
|---|---|---|
| `tests/entry tests/launch`，不含 `tests/entry/test_desktop_*.py` | 2002 passed、2 skipped、3 xfailed | 2025 passed、2 skipped、3 xfailed |
| `tests/entry/test_desktop_*.py`（OmicsClaw 解释器） | 569 passed、3 skipped | 569 passed、3 skipped |
| 15 个分层守卫、顶层 `tests/test_*.py`、`tests/evals`，加 `-m "not slow and not demo and not eval and not skill_example"` | 937 passed、14 skipped、27 deselected、1 xpassed | 937 passed、14 skipped、27 deselected、1 xpassed |

第一行多出的 23 条是这一轮新增的测试。第一行在 `f54d2dd9` 上跑，后两行在 `22facde6` 上跑，之后的提交只动了 `tests/entry/test_cli_input.py` 的一条测试和文档。第三行比 §9.4 的表少 13 条：那一轮用的清单我没有找到，这一轮按 `SPEC.md` 的写法取 `tests/*/test_*layer*.py`（不含 `tests/entry` 下已经在第一行里的那个）、`tests/sdk/test_boundary.py`、`tests/sdk/test_public_surface.py`，修前修后用的是同一份清单。

既有测试改了一处：`test_cli_repl.py::test_a_failed_approval_task_is_logged_and_not_merely_dropped` 里顶替 `Repl._ask` 的函数多收一个关键字参数。

真实终端（pty）里两种输入源各走一遍，做法同 §9.4：每次运行先用同一环境的探针确认输入源和 `omicsclaw.__file__`。

真实模型（DeepSeek，`--approval-timeout 15`），修后：

| 情形 | prompt_toolkit | 没装 prompt_toolkit |
|---|---|---|
| 一条消息里 `ask_user` 加 `bash`，提问到期后 0.5 秒敲 `yes` 回车 | 审批提示符没有打开，`No answer` 之后紧跟 `Approval denied [...]: nobody was asked, …`。`yes` 由主提示符读走成为下一条消息，模型重新调用 bash，这次卡片打开，3 秒内没有被结算，敲 `y` 才批准 | 同左 |
| 提问到期后敲 `ye`，下一条消息的 bash 卡打开 0.25 秒后敲 `s` 回车 | 两行提示，卡片 6 秒内没有被结算，没有放行；之后敲 `y` 才批准 | **仍然开着**：批准并放行整个工具，没有提示行 |
| 回归 §9.4：迟到的 `yes` 遇上下一条消息的审批卡 | 卡片 4 秒内没有被结算，有提示行，敲 `y` 才批准 | 同左 |
| 回归 §9.4：工具运行中提前敲 `yes` 回车 | 第二张卡 4 秒内没有被结算，有提示行，敲 `y` 才批准 | 同左 |

前两种情形在分支末端的生产代码上跑。后两种在加上"子代理不拒绝"那一行之前的工作树上跑，那一行只在帧带子代理名时起作用，这两种情形里没有子代理。

同一条消息那种情形修后一共跑了 6 次，每次都是提示符没有打开、调用被拒。被拒之后，1 次模型在同一次 exchange 的下一条消息里直接重新调用（卡片在 `No answer` 之后 3.1 秒打开，先前敲的 `yes` 被丢弃，有提示行），5 次先用文字说明原因，等 `yes` 作为下一条消息到达后才重新调用。这 6 次里有 3 次驱动脚本没跟上模型自己开始的第二次 exchange：一次在它运行中途发了 Ctrl-D，两次把审批卡出现的那段输出读掉了没有作答，卡片到自己的期限被拒，其中一次连着错了五张卡。表里用的是脚本改对之后的两次。这一轮真实模型共 18 次运行、67 次模型调用，脚本没跟上的 3 次占 22 次，其中一次 14 次。

脚本化后端（真实终端），分支末端：

| 情形 | prompt_toolkit | 没装 prompt_toolkit |
|---|---|---|
| `typing_through_deadline`，9 个偏移 | 9 次都没有打开审批提示符，以 `nobody was asked` 拒绝 | 同左 |
| `straddle_timed`，12 个偏移 | 前 9 个偏移（词跨在卡片打开的时刻，或只剩回车）卡片都保持打开；后 3 个偏移四个键都敲在卡片打开之后，批准 | 和修前相同：3 次 `s` 放行，6 次拒绝，3 次批准 |

另有 23 种不计时的情形乘两种输入源，在 `d69edae4` 和分支末端各跑一遍，逐项比对结果：

- 两种源都变了的 3 种，都是提问和审批在同一条消息里：迟到的 `yes`，到期前敲 `ye`、到期后敲 `s` 回车，到期前敲 `yes`、到期后只按回车。修前审批提示符打开并被结算（批准、放行、拒绝各一），修后提示符不打开。
- 只在 prompt_toolkit 下变了的 4 种：提前敲 `ye`、卡上敲 `s` 回车；提前敲了字没回车、卡上只按回车（审批卡、提问卡、提问到期后的审批卡各一）。修前卡片被后半截结算，修后卡片保持打开，再敲的才算。
- 其余 36 项修前修后相同，其中有提问在期限内作答之后，同一条消息里的审批卡照常打开并由 `y` 批准。

以上 pty 运行都关着光标位置报告。prompt_toolkit 那一路另把报告打开、由驱动脚本像终端那样应答，在分支末端再走 6 种（半行两种、整行、卡片出现后作答、提前按方向键、同一条消息里迟到的 `yes`），结果和关着时相同。

没有跑的：全量测试；`tests/tools`、`tests/permission`、`tests/subagent` 等这一轮没有碰到的层；`Eval CI`；Anthropic、OpenAI 两个 provider；Windows 和没有 `termios` 的平台。

### 10.7 没关上的和只记录的

这一轮按裁定只治两处。下面这些路仍然能让一张审批卡被没看到它的人结算，都没有动：

- **没装 prompt_toolkit 时的半行。** 上表第二行。`ye` 被 `tcflush` 清掉，`s` 加回车是卡片的回答。没有提示行。
- **卡片打开之后才键入的内容。** 规则按键入的时刻判断，分辨不出键入的人看没看到卡片。下一条消息里的审批卡在 `No answer` 之后隔一次模型调用打开，DeepSeek 下实测 2.7 到 3.4 秒；到期之后隔了这么久才敲的 `yes` 落在卡上。被拒的调用告诉模型可以重新发起，重新发起的卡片也属于这一种。脚本化后端下这个间隔接近零，`straddle_timed` 偏移不小于 0 的 3 例两种源都是批准。
- **提问的提示符还开着时敲了一半的字。** 提示符到期收回时半行被丢弃，没有留下记录；之后下一条消息的审批卡打开，在卡上敲完后半截，`s` 放行了整个工具。两种输入源都是这样（pty `question_half_then_card`），修前修后相同。要关上，prompt_toolkit 那一路可以在 `withdraw` 时看一眼被取消的提示符里有没有字，留给下一次 `read_fresh`。§11.4 在 prompt_toolkit 下关上了这一条，回退源仍然开着。
- **子代理的审批卡。** 这一轮决定不拒绝，它和下一条消息里的卡片是同一种情形。
- **没有 `termios` 的平台。** `StreamSource` 清不了输入队列，§9 和这一轮的规则在那里都不成立。不把 prompt_toolkit 声明为依赖。
- **§9.5 里的一条仍在**：回退源上被读线程拿走、0.05 秒内没报告的行会被当作卡片的回答。

只记录的：

- 回退源上半行被丢时没有提示行，按裁定不做。
- 被丢弃的后半截写进历史文件。
- 被拒的调用仍然打印卡片正文，见 §10.2。owner 2026-10-09 裁定维持（§11.3）。
- 0052/0054 第二阶段（Channel）不在这一轮。这一轮的规则只在 CLI 的 REPL 里；Channel 若用文字回复作答，同样的问题要在那里另行处理。
- 两条在高负载下偶发失败的既有测试这一轮遇到过一次：一张变异表开始前的基线跑里失败了其中一条，脚本按审核方的做法把两条都排除后重跑，569 passed。没有改。
- README、CHANGELOG 没改，和前几轮一样留给合并时处理。

## 11. 第四轮审核之后（2026-10-09）

第四轮审核的结论是有条件通过。§10 的两处修复按裁定做到了；另外发现到期的审批卡留下的提示符会把之后键入的 `s`、`a` 记成授权，并提了一条文档 P1、一条测试 P2 和三条 P3。owner 2026-10-09 就此作了三条裁定。修复追加在 `feat/ask-user-r4` 上，`6ae40805` 及之前的提交没有改写，没有 rebase，没有合并 `main`，没有 push。

| 提交 | 内容 |
|---|---|
| `dee4dee0` | `TurnHandle.approve` 返回这次回答有没有结算请求；`Repl._ask` 只在结算了的时候记 `s` 的授权、写 `a` 的规则，否则打印一行说明 |
| `8b3dfd3a` | `_drop_keys` 不把终端自己发来的完整控制序列算作人按的键 |
| `0f26b8ef` | prompt_toolkit 输入源记下提示符被收回时上面有没回车的字，下一张卡把后半截丢到下一个回车 |
| `f662ea2d` | 第四轮存活的 R22、R30、R31、I22 各补测试，不改生产代码 |
| `fe0206ad` | 这一轮自己的变异里存活的两处和靠超时才转红的一处，各补测试，不改生产代码 |
| `183978fe` | `cli.md` §7.1、§7.4、§7.5，`AGENTS.md` 的 CLI 一节，`human-in-the-loop.md` §7.2、§8.1 |

本记录在这之后的一个提交里。相对 `6ae40805`，生产代码动了三个文件：`omicsclaw/entry/turn.py`、`omicsclaw/entry/cli/_repl.py`、`omicsclaw/entry/cli/_input.py`。`entry/approval.py`、`entry/question.py`、`entry/rendezvous.py`、`entry/desktop/`、`entry/channel/`、`omicsclaw/evals/`、`omicsclaw/permission/`、`omicsclaw/tools/` 和 `tests/entry/golden/` 零 diff。既有测试没有改断言，只加了用例、断言和 docstring。

### 11.1 修前复现

在 `6ae40805` 的导出树上，脚本化后端加真实终端。除最后一行外两种输入源结果相同。

| 情形 | `6ae40805` |
|---|---|
| 审批卡 #1 到期，后面的卡片打印出来之后敲 `s` 回车 | 打印 `Will not ask about danger again in this conversation`。这个工具之后的调用不再询问，下一次 exchange 里也是 |
| 同上，敲 `a` 回车 | 打印 `Remembered`，`settings.json` 里多了 `danger({})`，之后的调用不再询问 |
| 同上，敲 `y` 回车 | 什么都没发生，屏幕上没有说明 |
| 同上，敲 `/auto` 回车 | 切到 `auto-approve`，之后的调用不再询问 |
| 审批卡 #1 到期，另一个工具的卡 #2 打印出来之后敲 `s` 回车 | 放行的是 #1 的工具 |
| 审批卡 #1 到期，之后是一张提问卡，选项里有一个叫 `s`，敲 `s` 回车 | 同样放行了 #1 的工具，提问卡的提示符随后才打开 |
| `ye` 敲在提问的提示符上，提问到期，模型在后面的消息里重新调用，卡片打开 0.3 秒后敲 `s` 回车 | 批准，并放行整个工具 |
| 提问的提示符上敲了半句话，同样的卡片上敲 `yes` 回车 | 批准 |
| 工具运行期间终端发来一个焦点报告（`ESC [ I`），卡片出现后敲 `y` 回车 | prompt_toolkit 下两行提示，这个 `y` 被当成那一行的后半截丢掉，要再敲一次。回退源不受影响 |

真实模型（DeepSeek，`--approval-timeout 15`，prompt_toolkit）下，半个词那一条在 `6ae40805` 上跑了一次：`ye` 敲在提问的提示符上，提问到期，下一条消息的 bash 卡在 `No answer` 之后 8.4 秒打开，0.25 秒后敲 `s` 回车，卡片被批准并打印 `Will not ask about bash again in this conversation`。到期审批卡那一条的真实模型修前证据是审核方在同一个提交上的运行：`s` 被 #1 的提示符读走，打印了同一句放行 bash 的话。这一轮没有重跑它。

### 11.2 裁定 1：到期审批卡的提示符不再产生授权

裁定的做法是只在 `approve` 真的结算了请求时才记 `s`、才写 `a` 的规则，不在到期时收回提示符。

- `TurnHandle.approve` 原来丢掉 `ApprovalBroker.settle` 的返回值，只记一条 debug 日志。现在把它返回：`True` 是这次回答结算了请求，`False` 是 id 未知，或者请求已经由先到的回答或期限结算。
- `Repl._ask` 原来先记授权、再结算。现在先 `await handle.approve(...)`，返回 `True` 才往 `Repl._granted` 里加、才调用 `_remember`。两步之间没有 `await`，记账和结算之间没有别的任务插得进来，`Will not ask about …` 一行仍然印在 `Approval granted` 之前。
- 返回 `False` 时打印一行弱化的 `<tool> [#n] was already settled: this line changed nothing.` 然后返回，后一张卡的提示符随即打开。

屏幕上打印什么是我定的。到期提示符上读到的每一行都打印这一句，`y`、`n` 和别的文字也一样，不单是 `s` 和 `a`。这一行多半是看着后一张卡敲的，人需要知道它被哪张卡读走了、什么都没改、后一张卡还在等。修前 `y` 在这里是无声消失的。句子带工具名和卡片编号，对得上屏幕上那张到期的卡。`/auto` 不打印这一句：它确实生效了，有自己的输出。

`TurnHandle.approve` 的调用方核对过。`omicsclaw/` 里只有两处调用它：`entry/cli/_repl.py` 的 `_ask`，和 `evals/runner.py`，后者不看返回值。Desktop（`entry/desktop/interactions.py`）和 Channel（`entry/channel/runtime.py`）直接调用 `handle.approvals.settle`，不经过它，行为不变。

`/auto` 在到期的提示符上照样生效，核实过，按裁定只记录不改：脚本化后端两种输入源下，敲 `/auto` 之后切到 `auto-approve`，同一次 exchange 里之后的调用不再询问。到期的那张卡本身不受影响。

钉住"到期不收回提示符"的 `test_cli_repl.py::test_an_approval_card_s_prompt_stays_open_past_its_deadline` 没有改，仍然通过。

### 11.3 裁定 2：直接拒绝的调用，显示维持现状

没有改。卡片正文照打，没有图例，没有提示符，下面一行拒绝理由。审核方存活的 R22（拒绝前多打印一行图例）现在由 `test_a_call_after_a_question_nobody_answered_is_refused_without_a_prompt` 里新增的两条断言钉住。

### 11.4 裁定 3：提问提示符上敲了一半的字

prompt_toolkit 下关上了，用的是 §10.3 那一套：下一张卡打印两行提示，把直到下一个回车（含）的内容读走丢弃，再读真正的回答。

`prompt_async` 被取消之后，`session.default_buffer.text` 里还留着没回车的字，到下一个提示符开始时才清空；一行被接受之后那里是被接受的文字。所以只能在读被取消的那一刻看，由被取消的那次读自己记（`PromptToolkitSource._prompt` 捕到 `CancelledError` 时），不放在 `withdraw` 里。排队等终端、还没轮到自己就被取消的读因此不留记录，那时终端上的字属于还开着的那个提示符。

记录是输入源上的一个布尔值。`read_fresh` 取走并清掉它，交给 `_drop_keys(keys, begun)` 作为"有一行没敲完"的起始状态：其间按过回车就归零，没按过就保持。范围是我定的：

- 用它的是之后打开的第一张卡，审批卡和提问卡都算，不限是哪一条消息的，子代理的也算。只管"下一条消息的审批卡"不够：模型可能先发一条只有文字的消息，先调一个不需要审批的工具，或者像 §10.6 里那样隔几秒才重新调用。提问到期之后本次 exchange 不再等别的提问，所以实际拿到它的总是审批卡。
- 只用一次。那张卡读完后半截的回车，这一行就结束了，再后面的卡片照常读第一行。
- 其间按过回车，或者主提示符读了一行，它就作废。主提示符的每一次 `read` 都先把它清掉，所以它带不出这次 exchange，不会落到无关的后续对话上。
- 卡片在等后半截回车时又被收回的话，这一行仍然没敲完，记录保留，留给再下一张卡。

即使其间一个键都没按，那张卡也打印两行提示并多等一个回车。这是有意的：人在提问的提示符上敲了 `ye`，提示符没了，他接下来敲的多半是 `s` 加回车。

回退输入源做不到，原因同 §10.3：`StreamSource` 让终端留在行模式，`withdraw` 的 `tcflush` 清得掉半行，看不见它。只写进文档。

### 11.5 审核意见

P1-1（文档）。`cli.md` §7.4 原来说到期审批卡的提示符上"键入的内容被丢弃"，并说"同一时刻只有一张卡"。两句都改成实际的行为：提示符还在读一行；后一张卡照常打印，它的提示符要等这一行读完才打开；这一行什么也不批准，打印哪一句；`/auto` 照样生效。§7.1 加一句指过去。§7.5 的表加两行，"拦不住"的清单改成现在的三条，`AGENTS.md` 的 "stay open" 一句和 `human-in-the-loop.md` 同步。

P2-2（`auto-approve` 下仍要询问的调用同样被拒绝）。新增 `test_a_call_that_is_always_asked_about_is_refused_in_either_mode`，`default` 和 `auto-approve` 各一例，用工作区里的 `ask` 规则让调用在两种模式下都要询问。R30（总是询问的调用不拒绝）在 `default` 一例上转红，R31（`auto-approve` 下什么都不拒绝）在 `auto-approve` 一例上转红。

P3（终端自己发来的序列）。做了能干净排除的那一半。prompt_toolkit 把它不认识的 CSI 序列拆成 Esc、`[`、后面每个字节各一个键。`_pressed_by_a_person` 把完整的一串（参数字节 `0x30` 到 `0x3f`、中间字节 `0x20` 到 `0x2f`、一个 `0x40` 到 `0x7e` 的结尾字节）整个略过，光标位置报告照旧略过。焦点报告 `ESC [ I`、`ESC [ O` 和设备属性报告因此不算人按的键，不打印提示行，不算开了一行。没有排除的四种，都选了"当作一行没敲完"：

- 被截断的序列，比如只到了 `ESC [ 2 0 ;`。它的后半截稍后会作为字符落到提示符上，和人敲的字分不开。
- `ESC ]` 开头的报告（OSC）。prompt_toolkit 把它拆成 Esc、`]` 和一串字符，结尾不固定，没有去认。
- `ESC P` 开头的回复（DCS），拆法和 OSC 一样。
- prompt_toolkit 不认识的 `ESC O x`，拆成 Esc、`O`、`x`。

这四种的后果是卡片打印两行提示、多等一个回车，不会放行。Esc 后面跟的不是 `[` 时不当作序列，Alt 加字母和它后面的字照旧算人敲的。后两种是第五轮复核补出来的，§11 初稿只列了前两种。

反方向也有：人按的键凑成一个完整 CSI 序列的样子时被略过，没有提示行。Esc 之后敲 `[y` 或 `[s`，一个不认识的功能键（`ESC [ 9 9 ~`），都是这样；被截断的报告后面紧跟人敲的一个字母时，字母被当成结尾字节一起略过（`ESC [ 2 0 ; s`）。这些键本来就在丢弃之列，少的是提示行和"开了一行"的记录。复核方的探针测出来的，我在分支上用自己的探针核对过，没有改代码。

P3（R22）。见 §11.3。

P3（I22）。定为：被丢弃的那一行的回车之后、同一口气键入的下一行就是回答。它是在卡片的提示符上、那一行结束之后键入的；再丢一次，卡片就对自己开着的提示符上敲的字不作声地不理。`test_the_line_typed_after_the_discarded_one_is_the_answer` 钉住：`ye` 提前键入，卡片上一次送进 `s`、回车、`y`、回车，读到 `y`。写进了 `cli.md` §7.5。

### 11.6 变异

这一轮新写的生产代码自己做了 59 处变异：`turn.py` 4 处，`_repl.py` 10 处，`_input.py` 里收回提示符的记录 16 处、终端序列 13 处，另有 16 处顶替审核方表里套不上的行。每次还原后比对 SHA-256，每张表先在没改动的树上跑一遍。跑的测试是 §10.5 那一组加 `tests/entry/test_session.py`。

在 `f662ea2d` 上第一遍，存活 3 处，另有 1 处靠 900 秒超时才转红：

| 变异 | 处理 |
|---|---|
| C9：`_control_sequence_end` 不检查 Esc 后面是不是 `[` | 补一例：Alt-y 后面跟 `e`，三个键都算人按的。`fe0206ad` |
| Q15：`PromptToolkitSource.read` 直接调用 `prompt_async`，主提示符被取消时不留记录 | REPL 里走不到：每次 exchange 都从主提示符成功读到一行开始，那次读会清掉记录。输入源自己的约定是哪个提示符被收回都一样，把收回提示符的那条测试参数化成两种提示符，钉住它。`fe0206ad` |
| Q13：`_prompt` 吞掉 `CancelledError` | 原来靠后面的测试挂到超时才发现。收回提示符的测试辅助函数现在断言那次读是以取消结束的，8 秒内转红。`fe0206ad` |
| P9：`approve` 返回之后、记账之前让出一轮事件循环（`await asyncio.sleep(0)`） | 没有补测试，见下 |

P9 只让出一轮循环，测试看不出区别，REPL 上也看不出：结算之后工具要先跑完，同一个工具的下一次调用再发出审批请求，中间隔着不止一轮。让出的时间够别的任务跑时测试能发现：P9b 把 `sleep(0)` 换成 `sleep(0.01)`，同一个工具的下一次调用到达时授权还没记下，`test_a_tool_allowed_for_the_conversation_runs_after_the_question` 转红。P9 走偏的方向是多问一次，少问不了。

在 `fe0206ad` 上重跑，59 处里 58 处转红，存活的是 P9。

审核方的表（`mutate_r4.py`，原样，63 行加基线）在 `f662ea2d` 上重跑，基线是绿的：44 行转红，16 行套不上，3 行存活。上一轮存活的 R22、R30、R31 都在转红的里面。套不上的 16 行是 I10 到 I23 和 I26，原文被这一轮改写，各由上面顶替的 16 处同义变异覆盖，全部转红，其中顶替 I22 的一处红在 `test_the_line_typed_after_the_discarded_one_is_the_answer` 上。存活的 3 行是 R2、R4b 和空操作对照 R33。R2（只比理由、不看状态）和 R4b（标记用赋值而不是只置位）审核方已经判为等价：带这个理由的 `QUESTION_SETTLED` 状态一定是 `no_answer`，一条消息里也只有一个提问在等。

### 11.7 验收

这一轮按 Risk-Matched Verification 第 4 档。改动落在授权记录上（会话内放行和写进 `settings.json` 的规则），并且改了 `entry/turn.py` 里所有入口共用的 `TurnHandle.approve` 的返回值。第 4 档要跑 `Eval CI` 跑的全部，Desktop 牵涉在内时加 `Desktop compatibility`。分支不能 push，没有开 PR，在本地按 `.github/workflows/eval.yml` 取同样的选择：

| 范围 | `6ae40805`（导出树） | `183978fe` |
|---|---|---|
| `unit-tests` 作业的目录清单和 marker 表达式，不含 `tests/entry/test_desktop_*.py` | 7386 passed、2 failed、25 skipped、157 deselected、3 xfailed、3 xpassed | 7424 passed、25 skipped、157 deselected、3 xfailed、3 xpassed |
| `tests/entry/test_desktop_*.py`（OmicsClaw 解释器），其中有 `Desktop compatibility` 公开作业的两个文件 | 569 passed、3 skipped | 569 passed、3 skipped |
| `eval` 作业：`tests/evals/dataset -m scripted_eval` | 29 passed | 29 passed |

`6ae40805` 那一列失败的 2 条是 `tests/bench` 里读当前提交号的测试，导出树不是 git 检出，读不到；在分支的检出上它们通过。除去这 2 条，多出的 36 条通过是这一轮新增的测试。同一组选择在 `f662ea2d` 上也跑过一遍，7422 passed，其余相同。

第 4 档里没有跑的：`eval.yml` 的四个 skill 示例作业（`spatial`、`singlecell`、`bulkrna`、`genomics`），skill 不导入 `omicsclaw.entry`；`Desktop compatibility` 里需要私有 App 仓库的配对作业；带真实密钥的 live eval。Anthropic、OpenAI 两个 provider，Windows 和没有 `termios` 的平台也没有跑。

真实终端（pty），做法同 §9.4，每次运行先确认输入源和 `omicsclaw.__file__`。脚本化后端，`f662ea2d`，之后的提交没有动生产代码：

| 情形 | prompt_toolkit | 没装 prompt_toolkit |
|---|---|---|
| 到期提示符上敲 `s` | 没有放行的那一句，打印 `danger [#1] was already settled: this line changed nothing.`；之后的调用照常询问，下一次 exchange 里也是 | 同左 |
| 到期提示符上敲 `a` | 同一句说明，没有 `Remembered`，`settings.json` 没有产生 | 同左 |
| 到期提示符上敲 `y` | 同一句说明 | 同左 |
| 到期提示符上敲 `/auto` | 切到 `auto-approve`，之后的调用不再询问。和修前一样 | 同左 |
| #1 到期，另一个工具的卡 #2 打印之后敲 `s` | 同一句说明，没有放行任何工具；#2 的提示符随后打开 | 同左 |
| #1 到期，之后的提问卡上敲 `s` | 同一句说明，提问的提示符随后打开 | 同左 |
| `ye` 敲在提问的提示符上，重新调用的卡片上敲 `s` 回车 | 两行提示，卡片保持打开，没有放行 | 仍然开着：批准并放行整个工具 |
| 提问的提示符上敲半句话，卡片上敲 `yes` 回车 | 两行提示，卡片保持打开 | 仍然开着：批准 |
| `ye` 敲在提问的提示符上，后半截和回车敲在没有提示符的时候 | 一行提示，卡片上敲的第一个 `y` 批准。和修前一样 | 同左 |
| 卡片之前终端发来焦点报告 | 没有提示行，卡片上敲的第一个 `y` 批准 | 和修前一样，第一个 `y` 批准 |

到期提示符敲 `s`、`a` 和半个词这三种，另用 15 秒的期限和 3 秒的模型延迟在两棵树、两种输入源上各走一遍，结果和上表一致。

§10 关掉的两条路在 `f662ea2d` 上回归，两种输入源。同一条消息里迟到的 `yes`、跨期限的 `ye` 和 `s`、到期后只按回车、`typing_through_deadline` 的 9 个偏移，都是提示符不打开、以 `nobody was asked` 拒绝；`auto-approve` 加 `ask` 规则时同样拒绝，单是 `auto-approve` 时调用照常执行。半行那一条，prompt_toolkit 下 `straddle_timed` 前 9 个偏移卡片保持打开，后 3 个批准；整行加半行、粘贴、多字节、Esc 加半行、两张并发卡、等回车时 Ctrl-C 都和 §10.6 相同。回退源和 §10.6 相同。光标位置报告打开时再走 4 种，结果不变。这一批共 121 次运行。

真实模型（DeepSeek，`--approval-timeout 15`），生产代码是 `f662ea2d` 的：

| 情形 | 输入源 | 结果 |
|---|---|---|
| bash 卡 #1 到期，write_file 卡 #2 打印 1.5 秒后敲 `s` 回车 | prompt_toolkit | `bash [#1] was already settled: this line changed nothing.`，没有放行的那一句；#2 的提示符随即打开，敲 `y` 批准。第二次 exchange 里再调用 bash，卡片照常打开 |
| `ye` 敲在提问的提示符上，提问到期，之后的 bash 卡打开 0.25 秒后敲 `s` 回车 | prompt_toolkit | 两行提示，卡片 6 秒内没有被结算，没有放行；再敲 `y` 才批准 |
| 同上 | 没装 prompt_toolkit | 仍然开着：批准并放行 bash |
| 回归：迟到的 `yes` 遇上下一条消息的审批卡 | prompt_toolkit | 卡片 4 秒内没有被结算，有提示行，敲 `y` 才批准 |
| 回归：工具运行中提前敲 `yes` 回车 | prompt_toolkit | 第二张卡 4 秒内没有被结算，有提示行，敲 `y` 才批准 |

半个词那两次里，模型把 `ask_user` 和 bash 放进了同一条消息：bash 先按 §10.2 被拒，模型在后面的消息里重新调用，卡片在 `No answer` 之后 4.5 秒和 3.5 秒打开。这是审核方指出的那条路。

这一轮真实模型共 6 次运行（上表 5 次加 §11.1 的 1 次）、19 次模型调用。驱动脚本先在脚本化后端上用同样的期限调通，再接真实模型。到期提示符敲 `a` 没有用真实模型跑，只有脚本化后端的结果。两条回归只在 prompt_toolkit 下各跑了一次。

### 11.8 没关上的和只记录的

仍然能让一张审批卡被没看到它的人结算的路：

- 没装 prompt_toolkit 时的半行，包括这一轮在 prompt_toolkit 下关上的那一种：提问的提示符上敲了一半、到期后在审批卡上敲完。上表两处"仍然开着"。
- 卡片打开之后才键入的内容。同 §10.7，没有变。
- §9.5 里的一条仍在：回退源上被读线程拿走、0.05 秒内没报告的行。
- 没有 `termios` 的平台，同 §10.7。
- 图例对不上卡的一种情形。总是询问的卡到期后在它的提示符上敲 `/auto`，它的图例（`y or s = allow it once`）重新打印，紧挨着图例打开的却是后一张卡的提示符。后一张卡是普通卡时，在那里敲 `s` 放行的是整个工具：实测打印 `Will not ask about danger2 again in this conversation`。修前就是这样，裁定是 `/auto` 只核实和记录，没有改。经过见下面第三条。

只记录、没有改的：

- `/auto` 在到期的提示符上照样切换模式。按裁定。它不结算到期的卡，也不放行已经打印出来的后一张卡。
- 到期的提示符吃掉给后一张卡的第一行。按裁定不收回。人要敲两次；后一张卡的期限在它的提示符打开之前就开始走。连着两张卡到期时要敲三行：前两行各被一张到期的卡读走，各打印一句说明，第三行才是第三张卡的回答。复核方跑过（它的 `stale_two`），我照样跑了一遍，两种输入源相同；修前前两行无声消失。
- 总是询问的卡到期后在它的提示符上敲 `/auto`：模式切换，这张卡的图例重新打印，它的提示符还要再读一行。接下来打开哪个提示符由终端的锁决定，先到先得。
  - 后面没有卡在排队时（比如正跑着一个不需要审批的工具），重新出现的是这张到期卡的提示符，下一行打印那句说明。
  - 后面已经有一张卡打印出来时，那张卡排在前面：先打开的是它的提示符，下一行是它的真实回答，没有那句说明。到期卡的提示符在这之后才重新出现，再敲的一行打印说明。

  两种输入源、修前修后顺序都相同，区别只在修后有那句说明。§11 初稿这一条只写了第一种，还标的是"没有跑"；第二种是第五轮复核跑出来的，我用 `ask` 规则造出总是询问的卡，在两棵树、两种输入源上复现了两种。到期的卡是普通卡时没有这一段，它的提示符读完 `/auto` 就收起。
- `approve` 返回 `True` 说的是这次回答放进去了，不保证请求按这个回答收场。同一轮事件循环里回答先到、另一件事后到，收场的是后一件事，有三种：
  - 期限的定时器。工具被告知到期拒绝，屏幕上这张卡显示被拒。
  - 等待的任务被取消。调用没有执行。
  - exchange 结束时的 `abandon`。工具拿到的是批准，流上发出的结算帧却是 `the exchange ended before this was answered`。

  三种情形里 `s` 的授权、`a` 的规则都已经记下。复核方在 broker 层用探针复现了三种，期限那一种 200 次里 200 次；我在分支上重跑它的探针，结果相同。反过来，定时器先到、回答后到时 `approve` 返回 `False`，200 次里 200 次，不记账。三种情形里人都是对着一张还开着的卡敲的，修前授权无条件记，所以没有变差。§11 初稿只写了期限一种，并且写的是没有复现。
- 被截断的终端序列、OSC 报告、DCS 回复和不认识的 `ESC O x` 让卡片多等一个回车；形状像控制序列的按键被略过时没有提示行。见 §11.5。
- P9 存活，见 §11.6。
- 被丢弃的后半截写进历史文件；回退源上半行被丢时没有提示行；0052/0054 第二阶段不在这一轮；README、CHANGELOG 没改。都同 §10.7。

## 12. 第五轮复核之后（2026-10-09）

第五轮复核对增量 `6ae40805..b2e73358` 的结论是通过，没有 P1。一条 P2 是 §11.8 里一句没跑过的推断和实际不符，另有几处 P3。这一轮只动文档、本记录和测试，生产代码相对 `b2e73358` 零 diff，没有用真实模型。提交追加在 `feat/ask-user-r4` 上，没有改写已有提交，没有合并 `main`，没有 push。

| 提交 | 内容 |
|---|---|
| `1f74f5ab` | 到期提示符上读到一行的那条测试加两例：`n` 和别的文字 |
| `93f5543c` | 新增一条测试：被收回的提示符上只有一个空格，也算一行没敲完 |
| `1923422d` | `cli.md` §7.4、§7.5，`AGENTS.md` 的 CLI 一节 |

本记录在这之后的一个提交里。§11.5 和 §11.8 里被这一轮查出不准或不全的几处已经就地改正，改过的地方各有一句说明。

### 12.1 P2：总是询问的卡到期后敲 `/auto`

§11.8 原来写的是这张到期的卡被再问一次、下一行打印那句说明，标着"按代码推的，没有跑"。后面没有卡排队时是这样；后面已经有一张卡打印出来时不是。复核方跑出来之后我先复现，再改文字。脚本化后端加真实终端，`ask` 规则造出总是询问的卡，`6ae40805` 和分支末端、两种输入源，一共四组，顺序都相同：

```
approve every [#1]? [y/N/a=always] /auto
Auto-approve is on for this whole process …
  this call is always asked about: y or s = allow it once · a = always, and write a rule · anything else denies
approve danger2 [#2]? [y/N/a=always] y
approve every [#1]? [y/N/a=always] Approval granted [<turn id>#2]
(3) -> slow
approve every [#1]? [y/N/a=always] x
every [#1] was already settled: this line changed nothing.
```

`/auto` 之后重新打印的是 `#1` 的图例，打开的是 `#2` 的提示符：`#2` 的读在 `#1` 敲 `/auto` 之前就排在终端的锁上了。`y` 是 `#2` 的真实回答。`#1` 的提示符在这之后才重新出现，第三行 `x` 打印说明。修前同样的顺序，只是第三行什么都不打印。把第二行换成 `s`，打印的是 `Will not ask about danger2 again in this conversation`：图例说的是 `#1`，放行的是 `#2` 的整个工具。后面没有卡排队时（`#1` 到期后跑的是一个不需要审批的工具），`/auto` 之后重新出现的是 `#1` 自己的提示符，下一行打印说明。到期的卡是普通卡时，`/auto` 之后它的提示符收起，没有这一段。

改了三处文字：§11.8 的那一条按两种情形重写，图例错位记进"仍然能让一张审批卡被没看到它的人结算的路"；`cli.md` §7.4 "到期的那张卡不受影响"一句拆成普通卡和总是询问的卡两种，并加上上面的屏幕；`AGENTS.md` 在 "until one line is read there" 之后补上总是询问的卡的例外。按裁定 `/auto` 只核实和记录，代码没有改。

### 12.2 P3

- `approve` 说结算了，不等于请求按这个回答收场。§11.8 那一条从只写期限一种、注明没复现，改成复核方在 broker 层复现的三种：期限、取消、exchange 结束。我在分支上重跑了它的探针，结果相同。
- 到期提示符上敲 `n` 或别的文字也打印那句说明，原来的测试只参数化了 `s`、`a`、`y`，复核方的 G7（拒绝的行不打印说明）和 G11（没结算的拒绝当作结算了）存活。`test_a_line_typed_at_a_card_past_its_deadline_allows_nothing` 加了 `n` 和 `not now` 两例。两处变异按原意重做，在 `1f74f5ab` 的导出树上都红在 `n` 一例上。
- `cli.md` §7.5 "认不出来的有两种"改成四种，补上 `ESC P` 开头的回复和不认识的 `ESC O x`。后果相同：多等一个回车，不放行。
- 人按的键形状像控制序列时被略过且没有提示行，被截断的报告后面紧跟的一个字母被当成结尾字节。写进 `cli.md` §7.5 和本记录 §11.5，没有改代码。
- "连着两张卡到期要敲三行"去掉了"没有跑"。复核方跑过，我也跑了一遍：两种输入源都是前两行各打印一句说明，第三行批准第三张卡；`6ae40805` 上前两行无声消失。
- 复核方其余存活的四处变异：
  - G10 就是 §11.6 的 P9。
  - H3（被收回的提示符上只有空白时不算开了一行）。复核意见是不用补，我还是补了一条测试（`93f5543c`）：这处变异走偏的方向是少丢，提示符上敲了一个空格、到下一张卡上敲 `s` 回车，`s` 会成为那张卡的回答。按原意重做的变异在 `93f5543c` 的导出树上转红。
  - B5c（提示符上有字时按 Ctrl-C 也留下"一行没敲完"的记录）。没有补。Ctrl-C 取消整个 exchange，接下来读输入的是主提示符，它把记录清掉，REPL 里看不出区别。
  - G13（批准时把键入的词当作理由带上）。没有补。屏幕上会变成 `Approval granted [...]: y`；这一行早于这几轮，测试只看 `Approval granted [` 这个前缀。

### 12.3 验证

按 Risk-Matched Verification，这一轮是测试和文字：跑了改动的两个测试文件和它们所在的那一组，文字做了名称存在性检查。

| 范围 | 结果 |
|---|---|
| `tests/entry/test_cli_card_input.py` | 29 passed |
| `tests/entry/test_cli_input.py` | 90 passed |
| `tests/entry/test_cli_*.py`、`tests/entry/test_session.py`、`tests/launch/test_cli_signals.py`、`tests/launch/test_surfaces.py` | 656 passed、1 xfailed |

第三行比 §11 结束时多 3 条，是这一轮加的两例和一条测试。

没有重跑第 4 档的选择：生产代码没有动。复核方在合并 `main` 之后的树上跑过那一组选择，结果在它的报告里。
