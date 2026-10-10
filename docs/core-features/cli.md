# CLI —— `oc` 进程入口与终端 REPL

> OmicsClaw **没有**全屏 TUI（Textual TUI 未移植），终端交互是一个流式 REPL。
> 事实来源：`omicsclaw/launch/`、`omicsclaw/entry/cli/`、`omicsclaw/entry/config.py`、`tests/launch/`、`tests/entry/test_cli_*.py`。

---

## 1. 概述

OmicsClaw 的命令行由两层组成：

| 层 | 包 | 职责 |
|---|---|---|
| 进程外壳 | `omicsclaw/launch/` | 唯一读取 `sys.argv` 与 `os.environ` 的地方；加载 `.env`；按 `--` 切分命令行；决定退出码；处理信号 |
| 终端界面 | `omicsclaw/entry/cli/` | 一个**库**而不是进程：REPL 循环、斜杠命令、审批卡片、事件渲染、`!` Shell、配置向导 |

`oc` 有且仅有三个子命令，对应三个界面（`omicsclaw/launch/_grammar.py` 的 `COMMANDS`，
`tests/launch/test_grammar.py::test_the_entry_points_are_exactly_three` 钉住数量）：

```
oc cli        终端 REPL，或 --prompt/--prompt-file 的一次性执行
oc desktop    OmicsClaw-App 的 HTTP 后端      （见 surfaces.md）
oc channel    即时通讯适配器                  （见 surfaces.md）
```

`oc run <skill>` 被 owner 裁定**不保留**：执行 skill 是 agent 在会话里做的事（见 agent-skills.md §9），不是第四个入口。

---

## 2. 进程入口：`omicsclaw/launch/`

### 2.1 四种写法，同一个 `main`

| 写法 | 定义处 |
|---|---|
| `oc ...` / `omicsclaw ...` | `pyproject.toml` `[project.scripts]` → `omicsclaw.launch:main` |
| `python -m omicsclaw.launch ...` | `omicsclaw/launch/__main__.py` |
| `python -m omicsclaw ...` | `omicsclaw/__main__.py` |
| `python omicsclaw.py ...` | 仓库根 `omicsclaw.py`（源码 checkout 的哨兵文件，勿删） |

`main(argv=None, env=None) -> int` **不抛任何异常**，只返回退出码：

| 退出码 | 含义 | 常量 |
|---|---|---|
| `0` | 收敛 / 正常结束 | `EXIT_OK` |
| `1` | 界面运行了但没收敛，或出现未预料的异常（只打印 `omicsclaw: <Type>: <msg>`，不打 traceback） | `EXIT_FAILED` |
| `2` | 命令行或部署配置被拒（`AppConfigError`），或缺可选依赖（`MissingSurfaceDependency`） | `EXIT_REFUSED` |
| `130` | `SIGINT` | `EXIT_INTERRUPTED` |
| `143` | `SIGTERM` | `EXIT_TERMINATED` |

`env` 显式传入时表示"这就是全部部署"：不读 `.env`、不碰进程环境——测试用它驱动整个部署。

### 2.2 `.env` 加载：`_dotenv.py`

```
main(env=None)
  └─ _adopt_dotenv()
       for candidate in dotenv_candidates():      # 去重后的两处，按序
           load_env_file(candidate, override=False)
```

- **两个位置，项目根优先**：`resolve_omicsclaw_dir()/.env`（依次取 `OMICSCLAW_DIR`、含 `omicsclaw.py` 的源码根、
  用户级回退目录），然后 `<cwd>/.env`。
- **已导出的变量永远赢**（`override=False`），所以两个文件里先出现的那个决定取值。
- 写回用 `dotenv_target()`：第一个已存在的候选；都不存在时取第一个候选。`oc cli --configure` 和 `/auto` 都写这个文件——
  "向导配置了一个文件、外壳却加载另一个"的 bug 由这一处共享搜索杜绝。
- 为什么写进进程环境而不是只放进映射：`omicsclaw.provider.provider_from_env` 直接从进程环境读 API key（plan 0031 Q8 唯一声明的例外）。

优先级：**命令行 flag > 已导出变量 > `.env` 文件 > 默认值**。

### 2.3 命令行文法：`--` 规则

```
oc <surface> [deployment flags] [-- surface flags]
```

`split_command_line(tokens)`：

1. 在第一个 `--` 处切开：左边是**部署 flag**，原样交给 `omicsclaw.entry.resolve_app_config`；右边是**界面 flag**，交给该界面自己的解析器。
2. 左半中处于 flag 位置的 `--help` / `-h` 被移到右半，所以 `oc cli --help` 与 `oc cli -- --help` 等价。
   "flag 位置"用 `_surfaces.flag_stride` 行走判断——`oc cli --workspace --help /data` 里的 `--help` 是 `--workspace` 的值，不会被当成求助。
3. **界面 flag 可以写在 `--` 任一侧**（plan 0048）：`_claim_surface_flags` 先从部署半边里认领本界面的 flag
   （两族 flag 不相交，不会抢走部署 flag），所以 `oc cli --session run-7` 与 `oc cli -- --session run-7` 相同；
   同一 flag 两边都写时，`--` 之后的更显式写法胜出。
4. `--` 仍有用处：值本身长得像 flag 时，例如 `oc cli -- --prompt --model`。

部署半边**总是**先被解析（即使随后只是回答 `--help`），所以部署 flag 的拼写错误不会被某个提前返回的界面 flag 吞掉：

```bash
$ oc cli --bogus x
omicsclaw: unknown option '--bogus'; pass a surface's own flags after '--'
usage: oc cli [deployment flags] [-- surface flags]
...
```

---

## 3. `oc cli` 的界面 flag

`CLI_FLAGS` / `ReplOptions`（`omicsclaw/launch/_surfaces.py`，手写解析器，错误统一抛 `AppConfigError` → 退出码 2）：

| flag | 作用 |
|---|---|
| `--session <id>` | 仅 REPL：继续一个已存储的会话；默认新开 |
| `--prompt <text>` | 回答一次后退出，不进 REPL |
| `--prompt-file <path>` | 同上，整个文件作为**一条**用户消息（避免多行任务被拆成多轮）；空文件被拒 |
| `--show-reasoning` / `--hide-reasoning` | 是否打印模型推理；二者互斥 |
| `--configure` | 运行配置向导写 `.env`，不启动 agent |
| `--help` / `-h` | 打印 `CLI_USAGE` |

组合规则：`--session` 与 `--prompt`/`--prompt-file` 互斥（一次性执行没有会话可继续）；`--configure` 与三者都互斥。

推理默认显示；例外是一次性执行且 stdout 不是终端时默认不显示，保证 `oc cli --prompt "…" > answer.txt` 只写答案
（`ReplOptions.shows_reasoning`）。

> 不要混淆：**部署 flag** `--system-prompt-file`（`OMICSCLAW_SYSTEM_PROMPT_FILES`）替换 system prompt 的前置文件；
> **界面 flag** `--prompt-file` 是用户消息。旧名已被拒绝且没有别名（`test_the_deployment_half_no_longer_answers_to_the_surface_flag_name`）。

---

## 4. 启动流程

```
oc cli [...]
  │
  main()  ── _adopt_dotenv() ── split_command_line()
  │
  start_cli(deployment, surface, env)
  ├─ _claim_surface_flags + ReplOptions.parse
  ├─ resolve_app_config(deployment, _with_the_cli_permission_mode(env))   ← 部署半边总被解析
  ├─ --help → 打印 CLI_USAGE，返回 0
  ├─ --configure → run_configuration_wizard(dotenv_target())，返回 0
  ├─ _report_a_missing_credential(env)          ← 没配 key：stderr 一行 "oc cli --configure"
  └─ with terminal_owned_logging() as records:  ← REPL 期间 root logger 的 handler 换成内存缓冲，几个嘈杂 logger 调到 ERROR
        asyncio.run(_run_cli(config, options, ...))
          ├─ app = attach_sessions(await open_app(config))      ← 组合根：见 surfaces.md
          ├─ 有 prompt → run_once(app, text)  → 收敛返回 0，否则 1
          └─ 否则 → source = open_prompt_source()
                    repl = Repl(app, source=..., screen=Screen(), ...)
                    repl.welcome()                 ← Logo + session/workspace/model/provider 横幅
                    with _interrupts(repl, task):  ← SIGINT → Repl.interrupt()
                        await repl.run()
          finally: _release(app)                   ← asyncio.shield(app.aclose())，吸收一次额外 Ctrl-C
     finally: _replay(records)                     ← 退出后把日志末尾 4000 字符打到 stderr
```

值得注意的三点：

- **一次性执行不接审批**：`run_once` 用空的 `ScriptedSource(())` 作为输入源，任何需要审批的工具调用都会被拒（失败关闭）。
  在默认权限模式下 `bash` / `write_file` / `edit_file` / `web_fetch` / `web_search` 都是 `ASK`，所以脚本化使用时
  通常要配 `--permission-mode auto-approve`（最好同时开沙箱）。
- **关闭是受保护的**：`_release` 用 `asyncio.shield` 包住 `AgentApp.aclose()`（排空会话、停 MCP 子进程、删沙箱容器），
  第二次 Ctrl-C 不会把它截断；若仍未完成，退出码报 1 而不是 130。
- **日志不丢**：REPL 占用终端时日志被重定向，退出时回放尾部，便于诊断。

---

## 5. REPL 循环

`omicsclaw/entry/cli/_repl.py` 的 `Repl`。提示符是 `❯ `（`PROMPT`）。

```python
while self.state.running:
    line = await self._source.read(PROMPT)      # EOF → "Goodbye!" 退出
    if not line.strip(): continue
    if not await self._dispatch(line.strip()): break
```

`_dispatch` 的判定顺序：

```
以 "!" 开头        → _shell(...)                 不调模型（§8）
匹配完整命令目录     → /exit 退出；未实现的名字 → "<name> is not available in this build."
                     已实现的 → _command(name, arg)
"/name" 但没人认领  → "No command named /name."（若恰是 skill 名，提示让 agent 选 skill），不调模型
其余               → ask(text)                  提交给会话注册表
```

一条以路径开头的行（首个 token 里还有 `/` 或 `\`，如 `/data/run7/matrix.h5ad 这是什么？`）不算命令名，照常发给模型
（`slash_token`）。

`ask` 通过 `app.sessions.submit(session_id, text)` 提交，拿到 `TurnHandle`，再由 `_drive` → `_pump` 把事件流画到屏幕，
最后 `await handle.wait()` 等注册表完成持久化，避免下一个提示与保存赛跑。回复被输出上限截断时，`_drive` 在这之后再打印一行（§9.2）。

**Ctrl-C 取消的是本次 exchange，不是进程**：`Repl.interrupt()` 取消正在运行的 handle，注册表发布
`EXCHANGE_END(terminal="cancelled")`，循环回到提示符；对话历史保持**逐字节不变**（取消的 exchange 不保留半截轨迹）。
空闲时按 Ctrl-C 则结束 REPL（`_interrupts._fire`）。信号处理器用 `signal.signal` 安装，因为 prompt_toolkit
每次读提示都会增删自己的 loop 级 SIGINT 处理器（plan 0051 修复的"Ctrl-C 失效"问题）。

---

## 6. 斜杠命令

命令目录分两层（`_constants.py`、`_slash_command_support.py`）：`SLASH_COMMANDS` 是从旧 CLI 逐字移植的**完整目录**
（含本构建不实现的 `/run`、`/research`、`/memory`、`/install-skill` 等），`ADDED_SLASH_COMMANDS` 是新增的 `/compact`、`/auto`；
REPL 真正实现的是 `REPL_SLASH_COMMAND_SPECS` 这个子集——共 14 个：

| 命令 | 行为（均从已组装的 `AgentApp` 回答） |
|---|---|
| `/help` | 列出这 14 个命令 |
| `/skills [query]` | 按领域分组列出已索引 skill；`query` 走 `SkillIndex.search`（name/domain/tags/triggers） |
| `/new` | 新会话 id（`new_turn_id()[:8]`），旧会话仍在存储里可 `/resume` |
| `/clear` | 与 `/new` 实现相同，只是提示文字为 "Cleared; new session"（放弃旧 session 而不是就地清空） |
| `/current` | 当前 session id、workspace、权限模式 |
| `/sessions` | 最近 10 个会话（`SESSION_LIST_LIMIT`），按最近活动倒序，带首条用户消息预览；并说明本部署是否持久化 |
| `/resume [id\|number]` | 无参数且在终端、prompt_toolkit ≥ 3.0.52 时弹出方向键选择器（↑/↓、Enter、Esc）；管道中打印列表；恢复后回显上一问一答 |
| `/compact` | 立即压缩本会话，保留近期消息，报告节省情况；本会话有 exchange 在跑时拒绝 |
| `/plan`、`/tasks` | 只读显示本会话的执行计划（`AgentApp.plans`），图标 `▶ ✔ ⊘ ○` |
| `/usage` | 本 REPL 累计的输入/输出 tokens（仅累加后端报告过的 usage）。总量含子代理花掉的；子代理有花费时在括号里单列，如 `Session total: 307 in / 33 out (sub-agents: 7 in / 3 out)` |
| `/mcp` | 读取实时 `MCPManager.statuses()`：每个 `.mcp.json` 服务器的状态与工具数 |
| `/auto [on\|off\|status]` | 在 `default` 与 `auto-approve` 间切换（§7.3） |
| `/exit` | 退出；别名 `/quit`、`/q` |

目录里其余名字（`/run`、`/research`、`/doctor`、`/export` …）回答 "not available in this build"，而不是被当成问题发给模型。
skill 不是命令：`/spatial-de` 会得到 "No command named /spatial-de." 加一行 "Skills are picked by the agent" 的提示。

Tab 补全（`_input.build_completer`）只补 14 个命令；首个 token 含第二个 `/` 时改走路径补全。

---

## 7. 审批卡片

需要审批的工具调用会在事件流里出现 `APPROVAL_REQUIRED`。REPL 在**独立的 Task** 里提问（`_ask_human` → `_ask`），
事件泵绝不等人——否则同一条模型消息里的第二个工具的审批请求永远到不了（plan 0031 trap 1）。

### 7.1 卡片与回答

提示格式 `approve {name} [{card}]? [y/N/a=always] `，`card` 是请求在本 exchange 内的编号（如 `#1`），
与卡片上 `[<turn id>#1]` 对应，便于区分同一工具的两次调用。卡片上方打印图例：

| 回答 | 效果 | 持久化 |
|---|---|---|
| `y` / `yes` / `ok` / `allow` | 允许这一次 | 无 |
| `s` / `session` | 允许，并在**本会话**内不再询问这个**工具**（plan 0049 起按工具而非按精确调用） | 仅内存 `Repl._granted`，随会话与进程消失 |
| `a` / `always` | 允许，并为**这一个精确调用**写一条 `allow` 规则 | `<workspace>/.omicsclaw/settings.json`（`AgentApp.remember_approval`） |
| `/auto` 或 `/auto on` | 切到 `auto-approve` 并允许这张卡 | 同 §7.3 |
| 其他任何输入（含空行） | 拒绝 | — |

`a` 刻意只覆盖精确调用：规则优先于危险命令模式，`bash(git *)` 会放过 `git status; rm -rf /`。
要"别再问 bash"，用 `s` 或 `--permission-mode auto-approve`。

回答要在卡片的提示符打开之后键入。工具还在运行时提前敲的 `y` 不会批准随后出现的卡片：它被丢弃，卡片上方多一行
`input typed before this prompt was discarded`，见 §7.5。

`s` 和 `a` 只在这一行结算了这张卡时才生效。卡片设了期限（`--approval-timeout`）并且已经到期时，它的提示符还留在屏幕上，
在那里读到的行不记授权、不写规则，见 §7.4 里"审批卡片到期后"一条。

### 7.2 总是询问的卡片

- `ApprovalRequest.ask_every_time` 为真（危险命令模式、显式 `ask` 规则、改动 `.omicsclaw/`、规则文件或 `.env`、
  声明 `DENY_UNLESS_TRUSTED` 的工具）：图例换成 "this call is always asked about: y or s = allow it once · a = always …"，`s` 只放行这一次。
- 受保护文件（规则文件、`.omicsclaw/`、`.env`）的改动在读取任何规则之前就被判定，`a` 写的规则永远不会被查阅，
  所以卡片只提供 `y`（`AgentApp.can_remember_approval` 返回 `False`）。
- 问不出来（输入源出错）的请求一律拒绝；在卡片上按 Ctrl-C 以 `interrupted at the terminal` 拒绝。CLI 默认没有审批期限
  （`approval_timeout_s=None`）。

### 7.3 `/auto` 与权限模式

`PermissionMode`：`default`（规则决定，未命中时按工具自身策略）、`auto-approve`（未命中即允许，`deny` 规则与危险模式仍生效）、
`read-only`（拒绝所有未声明 `read_only=True` 的工具，包括 `bash` 与 `web_search`）、`bypass-all`（无任何检查）。

`/auto`（`_auto.py`，plan 0050）做两件事：

1. **现在**：`AgentApp.set_permission_mode` 立刻切换本进程唯一的权限门，下一个工具调用即生效；只允许在 `default` ⇄ `auto-approve` 之间切换，
   `read-only` / `bypass-all` 是部署承诺，运行中不可变。
2. **下次启动**：把 `OMICSCLAW_CLI_PERMISSION_MODE` 写进外壳加载的 `.env`。这是 CLI 专用键，Channel / Desktop 不读，
   避免在终端里的一次切换让无人值守的 IM 机器人开始自动批准。

`oc cli` 权限模式的优先级：`--permission-mode` flag > `OMICSCLAW_PERMISSION_MODE`（任何来源）> `OMICSCLAW_CLI_PERMISSION_MODE` > `default`。
被更高优先级压住时 `/auto status` 会说明。`auto-approve` 下仍会询问：危险命令、显式 `ask` 规则、对 `.omicsclaw/` 或 `.env` 的改动。

### 7.4 提问卡片（`ask_user`）

模型缺一个只有你知道或只能由你决定的事实时（哪一组是对照、用哪个基因组版本、要不要覆盖已有报告），可以调用 `ask_user`
提一个问题，拿到回答后在同一次 exchange 里继续。事件流里出现 `QUESTION_ASKED`，REPL 同样在独立的 Task 里读回答
（`_ask_question` → `_question`）。卡片与提示如下：

```
Question [<turn id>#2]: Which clustering?
  │ 1. Leiden - recommended
  │ 2. Louvain
Reply with an option number, or in your own words.
(3) -> ask_user
  empty line skips · Ctrl-C cancels the request
answer [#2]>
```

`(3) -> ask_user` 是这次工具调用在转写稿里的那一行，它排在卡片之后、图例之前，与审批卡片上 `-> bash` 的位置相同。

| 输入 | 模型读到的结果 |
|---|---|
| 选项编号（卡片写明可多选时用逗号或空格分隔多个） | `answered`，`selected` 是对应的 label，`reply` 是原文 |
| 与某个 label 相同的整行（不分大小写） | `answered`，`selected` 是这个 label |
| 其他任何文字，包括越界的编号、单选题上的多个编号 | `answered`，`selected` 为空，`reply` 是原文 |
| 空行 | `declined`：模型被告知不要再问，按自己的判断继续并写明假设 |
| Ctrl-C | 取消这次 exchange，回到提示符，对话历史不变 |

- 提问提示符上**没有斜杠命令**：`/data/ref.h5ad` 是回答，`/auto`、`/exit` 在这里也只是文字。在这个提示符上键入的 `y`、`s`、`a`
  同样只是文字，不授予任何权限。提示符收回之后键入的内容不再归这个问题，它有可能落到后面的审批卡上，见下面"到期之后键入的内容"一条。
- 审批与提问共用 `#n` 编号，同一次 exchange 里不重号；`ask_user` 单独成批执行，所以提问卡打开时没有别的卡在等回答。
  屏幕上可能还留着一张已经到期的审批卡的提示符，那时先读到一行的是它，见下面"审批卡片到期后"一条。
- 回答和其他输入一样进入 `~/.config/omicsclaw/history`。问题与回答留在对话历史里，不写日志。
- 没有期限（`approval_timeout_s=None`）时提问会一直等。设了 `--approval-timeout` 则到期返回 `no_answer`，本次 exchange 之后的提问不再等待。
- 提问到期后提示符会收回：`_pump` 收到 `QUESTION_SETTLED` 时取消读回答的 Task 并等它结束（`_retract_question`），Task 结束时让输入源
  收起提示符（`FreshSource.withdraw`）。两种终端输入源下 `No answer [...]` 都另起一行，活动行随后恢复。两者做法不同：
  - prompt_toolkit：`prompt_async` 被取消时自己把提示符画成结束状态并换行。到期前键入、还没回车的字随提示符一起丢弃，
    `PromptToolkitSource` 同时记下这里有一行没敲完，后面的卡片据此多丢一截（§7.5）。
  - 没装 prompt_toolkit 时的 `StreamSource`：提示符是直接写到标准输出的，后面没有换行。`withdraw` 补上这个换行，并清空终端的输入队列
    （`termios.tcflush`），到期前键入的半行因此丢弃，不会和之后的回车拼成一行。它看不见这半行，也就记不下有过半行（§7.5）。
- 同一条模型消息里排在这个提问后面、需要审批的调用，在提问到期后不再询问，直接拒绝。从到期的 `QUESTION_SETTLED` 到这条消息的
  `TURN_END`，`_pump` 把每个 `APPROVAL_REQUIRED` 带着 `unasked` 交给 `Repl._ask`：不打印图例，不打开提示符，以
  `nobody was asked, because the question earlier in the same message got no answer; make the call again in a later message if it is still needed`
  拒绝。屏幕上是卡片正文，下面一行 `Approval denied [...]: nobody was asked, …`。这张卡照常打开的话，会和提问到期落在同一刻，
  晚半秒敲给提问的 `yes` 是在它打开之后键入的，§7.5 的丢弃拦不住。模型在工具结果里读到这句话，可以在后面的消息里重新调用，
  那时卡片照常打开。
  - 只看到期。提问被回答、被空行跳过、或者因为输入源出错而问不出来时，后面的审批卡照常打开；Ctrl-C 取消的是整个 exchange。
  - 只看本来会打开提示符的调用。不需要审批的调用照常执行；`auto-approve` 下不询问的调用、`allow` 规则放行的调用、
    已经用 `s` 放行的工具也照常执行。`auto-approve` 下仍要询问的调用（§7.3 末尾）同样被拒绝。
  - 子代理发起的审批不在此列。这条消息里的 `task` 启动子代理后，子代理要先调用一次模型才会用到工具，
    它的卡片和下一条消息里的卡片是同一种情形。
- 到期之后键入的内容不再是这个问题的回答。它去哪里，看键入的那一刻有没有卡片开着：
  - 没有卡片开着，这次 exchange 里之后也没有卡片打开：整行留到 exchange 结束，由主提示符读走，作为下一条消息发给模型；
  - 没有卡片开着，之后有卡片打开：卡片打开时把它丢弃，卡片照常等待，见 §7.5。这样的卡片来自模型后面的消息，
    或者来自子代理；
  - 已经有卡片开着：这一行就是那张卡的回答。这张卡和 `No answer` 之间隔着至少一次模型调用，但没有保证的间隔。
    提问到期后再敲 `y` 之前，先看屏幕上开着的是什么。
- 审批卡片到期后**不会**收回：`approve …?` 提示符留在屏幕上，还在等一行，这段时间没有活动行。没人键入的话它留到 exchange 结束。
  - 之后的卡片（审批或提问）照常打印，但它的提示符要等前一张的提示符读完一行才打开。看着后一张卡键入的那一行，
    是被到期那张卡的提示符读走的。
  - 这一行什么也不批准。请求已经由期限结算，`TurnHandle.approve` 返回 `False`，`Repl._ask` 这时不记 `s` 的授权、
    不写 `a` 的规则，打印一行弱化的 `<tool> [#n] was already settled: this line changed nothing.`，
    后一张卡的提示符随后打开，回答要在那里重新键入：

    ```
    Approval required [<turn id>#2]: write_file (risk high) - create …/note.txt and write 5 bytes
    (2) -> write_file  path="note.txt" content="hello"
      y = allow once · s = allow this tool for the rest of the conversation · …
    approve bash [#1]? [y/N/a=always] s
    bash [#1] was already settled: this line changed nothing.
    approve write_file [#2]? [y/N/a=always]
    ```

  - `/auto` 是命令，在这个提示符上照样切换权限模式（§7.3），之后不属于"总是询问"的调用不再询问。
    到期的那张卡仍然是被拒的，已经打印出来的后一张卡也不会因此放行，仍要在它自己的提示符上回答。
  - 到期的卡是普通卡时，它的提示符读完 `/auto` 就收起。它属于"总是询问"（§7.2）时还要再读一行：
    图例重新打印，提示符重新出现，在那里读到的下一行同样只打印上面那句说明。后面已经有卡在排队的话，
    先打开的是后一张卡的提示符，到期卡的提示符排在它之后才重新出现：

    ```
    approve every [#1]? [y/N/a=always] /auto
    Auto-approve is on for this whole process …
      this call is always asked about: y or s = allow it once · a = always, and write a rule · anything else denies
    approve danger2 [#2]? [y/N/a=always] y
    approve every [#1]? [y/N/a=always] Approval granted [<turn id>#2]
    ```

    这时紧挨着 `#2` 提示符的是 `#1` 的图例。`#2` 是普通卡的话，`s` 在它上面放行的仍是整个工具，
    以 `#2` 自己那张卡上的图例为准。
- 只有终端里的 REPL 会提问。`--prompt` / `--prompt-file`、管道输入、`oc desktop`、`oc channel` 下不挂载这个工具
  （`launch/_surfaces.py` 的 `surface_config`），子代理也没有它。
- `--ask-user false` 或 `OMICSCLAW_ASK_USER=false` 整体关闭。开了 `/auto` 之后要离开终端的会话建议关掉，否则模型一问就停在那里。

### 7.5 卡片只接受它出现之后键入的内容

终端会留住没人读的输入。审批卡和提问卡都只读自己的提示符打开之后键入的内容（`Repl._read_at_card` 经 `FreshSource.read_fresh` 读）。
提示符打开之前键入的内容既不结算这张卡，也不拼进它的回答。下面三种来路都算"之前"：

- 工具还在运行、没有任何提示符时键入的；
- 提问到期、提示符收回之后，下一张卡打开之前补的回答；
- 一条模型消息里连着两张卡时，第一张答完之后、第二张的提示符打开之前键入的，包括在第一张的提示符上一口气键入的第二行。

有输入被丢弃时，卡片的图例与提示符之间多一行弱化的 `input typed before this prompt was discarded`。
提前键入 `y` 的人看到卡片还在等，从这一行知道原因，在提示符上重新回答即可。丢弃发生在这张卡拿到终端的那一刻，
所以两张卡排队时，第二张等到自己的提示符打开才丢弃，在那之前键入的都算提前。

主提示符（`❯`）不受影响：没有卡片打开时，提前或迟到键入的行照旧由主提示符读走，作为下一条消息。

| 提示符打开之前已有的输入 | prompt_toolkit（`PromptToolkitSource`） | 没装 prompt_toolkit（终端上的 `StreamSource`） |
|---|---|---|
| 敲完并回车的行 | 丢弃，打印提示行 | 丢弃，打印提示行 |
| 敲了一半、没回车的字 | 丢弃，打印两行提示；之后键入的内容直到下一个回车（含）也丢弃 | 丢弃，没有提示行；之后键入的后半截加回车是这张卡的回答 |
| 上一个提示符多读到、prompt_toolkit 留给下一个提示符的键 | 丢弃，打印提示行；半行同上 | 不适用 |
| 被取消的读留下的 `readline` 已经读走的那一行 | 不适用 | 丢弃，打印提示行 |
| 提问的提示符到期收回时上面还没回车的字 | 随提示符丢弃并记下；下一张卡打印两行提示，之后键入的内容直到下一个回车（含）也丢弃 | 随提示符丢弃，没有记录；之后键入的后半截加回车是那张卡的回答 |
| 方向键、Esc 这类不往行里放字的键 | 丢弃，打印提示行；不算开了一行 | 留在终端的行缓冲里，和半行一样被清掉，没有提示行 |
| 终端自己发来的完整控制序列：光标位置报告、焦点进出（`ESC [ I`、`ESC [ O`）、设备属性报告 | 丢弃，没有提示行；不算开了一行 | 留在终端的行缓冲里，和半行一样被清掉，没有提示行 |

prompt_toolkit 下，`_drop_keys` 先清掉它自己的预读缓存（`get_typeahead`），再在 raw 模式里把终端里等着的键读空。
`StreamSource` 下，`_drop_typed` 先用 `termios.tcflush` 清空终端输入队列，再给被取消的读留下的那个 `readline` 线程
`_HANDOVER_S`（0.05 秒）来报告。这段时间内报告的行是提示符打开之前键入的，丢弃。没有报告的线程还在等下一行，留给这张卡用。
这 0.05 秒在提示符上屏之前，所以期间键入的行同样算提前。

敲到一半的行还要多丢一截。`ye` 在卡片打开之前键入，`s` 加回车在卡片打开之后键入，合起来是一个 `yes`。只读后半截，读到的是 `s`，
在审批卡上的意思是"本次对话内不再询问这个工具"。prompt_toolkit 下，`_drop_keys` 丢弃时看最后一个回车之后有没有字符或粘贴，
有就是一行没敲完（`read_fresh` 的 `unfinished` 回调）。这时卡片在第一行提示下面再打印一行
`its last line had no Enter: what is typed up to the next Enter is discarded too`，提示符照常打开，
在它上面键入的内容直到下一个回车（含）被读走丢弃，提示符再出现一次，这一次键入的才是回答：

```
  input typed before this prompt was discarded
  its last line had no Enter: what is typed up to the next Enter is discarded too
approve bash [#2]? [y/N/a=always] s
approve bash [#2]? [y/N/a=always]
```

提前只敲了 `y` 没回车、到卡片上只按回车的人，这个回车也在丢弃之列，卡片继续等。等那个回车的时候 Ctrl-C 照常取消。
被丢弃的后半截和其他输入一样写进历史文件。

丢弃到那个回车为止。回车之后键入的内容是在卡片的提示符上、那一行结束之后键入的，算回答，紧跟着回车一口气键入的也算：
`ye` 提前键入，卡片上连着键入 `s`、回车、`y`、回车，读到的回答是 `y`。

提问的提示符被收回时上面有没回车的字（§7.4），按同一条处理。`PromptToolkitSource` 在读被取消的那一刻看输入框里有没有字，
有就记下一行没敲完。之后打开的第一张卡不论来自哪一条消息、是不是子代理的，即使这期间一个键都没按，也打印上面两行提示，
并把直到下一个回车（含）的内容丢弃。提问到期之后本次 exchange 不再等别的提问，所以这张卡实际上总是审批卡。
期间按过回车的话那一行已经结束，卡片只打印第一行，不多丢。这个记录不带出这次 exchange：主提示符读输入时把它清掉。
排队等终端的读被取消时不留记录，留记录的只有当时占着终端的那个提示符。

终端自己发来的控制序列不是人按的键。prompt_toolkit 把它不认识的序列拆成 Esc、`[` 和后面每个字节各一个键，
`_drop_keys` 把完整的一串（参数字节、中间字节、再加一个结尾字节）整个略过：不打印提示行，不算开了一行。
焦点报告因此不会让卡片多等一个回车。认不出来的有四种：还没收全的序列（只到了 `ESC [ 2 0 ;`），`ESC ]` 开头的报告（OSC），
`ESC P` 开头的回复（DCS），以及 prompt_toolkit 不认识的 `ESC O x`。它们和人敲的字分不开，按一行没敲完处理，
卡片打印两行提示并多等一个回车。这四种只会让卡片多等，不会放行。

人按的键凑成一个完整序列的样子时也被略过：Esc 之后敲 `[y` 或 `[s`，或者一个 prompt_toolkit 不认识的功能键（`ESC [ 9 9 ~`）。
还没收全的报告后面紧跟人敲的一个字母时，字母被当成结尾字节一起略过（`ESC [ 2 0 ; s`）。
这些键本来就在丢弃之列，少的是那一行提示，卡片也不把它们算作开了一行。

没装 prompt_toolkit 时没有这一步。`StreamSource` 让终端留在行模式，内核在回车之前不把半行算进可读的字节
（`FIONREAD` 是 0，`select` 不就绪），`tcflush` 清得掉它，看不见它。`ye` 被清掉之后，卡片上键入的 `s` 加回车是完整的一行，
被当作回答：在审批卡上批准这次调用并放行这个工具，在提问卡上作为自由文本交给模型。提前只敲了 `y` 没回车、到卡片上只按回车，
读到的是空行：审批卡拒绝，提问卡记为跳过（`declined`）。要避开，装上 prompt_toolkit，或者等卡片出现再键入。

这条规则按键入的时刻判断。卡片打开之后键入的内容就是它的回答，键入的人有没有看到卡片，规则分辨不出。下面几种情形它拦不住：

- 卡片已经打开之后键入的内容，包括工具刚结束那一刻、或者提问到期几秒之后才敲的 `yes`。同一条消息里紧跟提问的审批卡
  因此不打开（§7.4），其他卡片照常打开。
- 没装 prompt_toolkit 时的半行，不论它是在卡片打开之前敲的，还是在提问的提示符到期之前敲的。`StreamSource` 清得掉它，
  留不下"有一行没敲完"的记录，之后在审批卡上键入的后半截是那张卡的回答。prompt_toolkit 下这两种都已经关上。
- 到期的审批卡留在屏幕上的提示符（§7.4）。看着后一张卡键入的那一行由它读走：这一行不批准任何调用，
  但 `/auto` 在那里照样切换权限模式，后一张卡的提示符也要等这一行读完才打开。
- 到期的卡属于"总是询问"、又在它的提示符上敲了 `/auto` 时，后一张卡的提示符上方是到期卡的图例（§7.4）。
  图例说 `y or s = allow it once`，后一张卡是普通卡的话，在那里敲 `s` 放行的是整个工具。

标准输入不是终端时没有这条规则：

- 管道和文件（`oc cli < questions.txt`）：行是写脚本的人事先按顺序排好的，没有先后可言，卡片照旧读下一行。
  丢弃的话，管道里每张审批卡都会读到输入结束而被拒。`ask_user` 在管道下不挂载，所以这里只有审批卡。
- `ScriptedSource`（测试与一次性执行）不实现 `FreshSource`，卡片读它的下一行。

伪终端是终端，规则照常生效。用 `pexpect`、`expect`、`script` 这类工具驱动 `oc cli` 的脚本，如果在卡片出现之前就把回答写进去，
回答会被丢弃，卡片一直等到期限；没设期限就一直等。脚本要等提示符（`approve … [y/N/a=always]` 或 `answer [#n]>`）
出现在输出里再写回答。

没有 `termios` 的平台上，`StreamSource` 清不了终端的输入队列：整行和半行都留着，会回答接下来的卡片，§7.4 里到期前的半行
也会和之后的回车拼成一行。prompt_toolkit 那一路走它自己的输入接口（`Input.raw_mode`、`Input.read_keys`），不调用 `termios.tcflush`；
它在没有 `termios` 的平台（Windows）上没有实测过。

---

## 8. Shell 模式：`!<cmd>`

实现位于 `omicsclaw/entry/cli/_shell.py`：

| 项 | 取值 |
|---|---|
| 执行方式 | `bash -c`，工作目录为 workspace；stdout+stderr 合并；stdin 为 `/dev/null`；独立进程组，超时整组杀掉 |
| 超时 | `SHELL_TIMEOUT_S = 60.0`（与 `tool_timeout_s` 无关：这是人盯着光标等的命令） |
| 屏幕截断 | `DISPLAY_LIMIT = 4096` 字节 |
| 给模型的截断 | `CONTEXT_LIMIT = 2048` 字节 |
| 注入方式 | 记录累积在 `_shell_records`，以 `[Shell commands run by the operator at the terminal]` 为头**前置到下一个问题**，读后清空 |
| 交互式程序 | `wants_a_terminal` 只看首词（`NEEDS_A_TERMINAL`），命中则提示"去另一个窗口运行"；漏网的由超时兜底 |
| 权限门 | **不经过**：操作者本人就坐在一个 shell 前，给他自己的命令加确认没有意义；门是给模型提出的命令的 |

例：`!ls runs/pp/figures` 然后问"哪张图说明聚类效果最好？"，模型会同时看到你刚才列出的目录。

---

## 9. 事件渲染

`Repl._pump` 在 `async with handle.observe() as observation` 中消费一次 exchange 的 `TurnEvent`
（`async with` 保证中途 break 时观察者被摘除，否则放弃计时不会开始）：

| 事件 | 终端行为 |
|---|---|
| `REASONING_DELTA` | 由 `ReasoningStreamWriter` 以弱化样式流式写出（`--hide-reasoning` 时丢弃）；回答中途再次思考会先收尾 Markdown |
| `TEXT_DELTA` | 由 `MarkdownStreamFormatter` 流式渲染；与推理块之间空一行 |
| `TOOL_START` / `TOOL_RESULT` | `TextRenderer` 给出头行，`ToolTranscript` 加调用编号、参数预览（`ARGUMENT_CHARS = 120`）、输出预览（`OUTPUT_LINES = 3`、`OUTPUT_CHARS = 160`），结果按 `tool_call_id` 配对；控制字符被替换 |
| `TOOL_RESULT` of `plan_write` | 计划有变化时打印完整计划快照（与上次打印的比较，未变则不打印） |
| `APPROVAL_REQUIRED` | 独立 Task 弹审批卡片（§7） |
| `QUESTION_ASKED` | 卡片按行以正常样式打印（不弱化），独立 Task 读回答（§7.4） |
| `PROGRESS` | 更新活动行的细节 |
| `CONTEXT` / `COMPACTION` / `QUEUED` / `APPROVAL_SETTLED` / `QUESTION_SETTLED` / `GAP` | `TextRenderer` 的一行控制文本；已回答的提问不出字。`QUESTION_SETTLED` 到达时提示符还开着的话先收回它（§7.4） |
| `TURN_END` | 累加 usage 供 `/usage`（`usage=None` 跳过，零值照加）。子代理的轮次不在这条流里，它们的用量在 exchange 结束后从 `TurnHandle.delegated` 读一次 |
| `EXCHANGE_END` | `converged` 不打印（每个回答下面一行 "Done." 是噪音）；`cancelled` / `failed` 打印。被输出上限截断的 exchange 也是 `converged`，它的说明在帧流结束后由 `_drive` 打印（§9.2） |

`TextRenderer`（`omicsclaw/entry/render.py`）在 CLI 以 `batched=False` 使用：token 一到就上屏。它从不渲染工具参数或输出的原始载荷
（审批行例外，展示给做决定的人，且不写日志）；工具耗时带 `ELAPSED_INCLUDES_APPROVAL_WAIT` 后缀——这个时间包含人思考审批的时间。

### 9.1 活动行：`_activity.py`

两个 token 之间可能静默几十秒（模型思考、`bash` 跑几分钟），`ActivityLine` 在输出底部维护**一行**状态：
动词轮换（`VERBS = ("thinking", "analysing", "working", "reasoning", "weighing")`）+ spinner（`⠋⠙⠹…`）+ 当前工具与耗时 + `ctrl-c to interrupt`。

- 不是 TUI：只用 `\r` + 擦除，结束后 scrollback 与没有它时逐字节相同（`ActivityLine.close` 保证）。
- 与流式文本共享终端的规则是"**不画**"：文本开始流出时 `hold()`，停止时 `release()`；审批期间也 `hold()`（计数式，支持多张卡）。
- 模型调用期间也显示（不只工具期间）——一个只返回 tool call 的模型不产生任何 `TEXT_DELTA`。
- 非终端（管道、文件）不做动画，而是在同一活动持续 `HEARTBEAT_S = 30.0` 秒后追加一行纯文本心跳；短的 `oc cli --prompt ... > answer.txt` 不会出现心跳。
- 刻意不显示 token 计数（plan 0041 裁定：`/usage` 按需打印即为最终答案）。

### 9.2 回复被输出上限截断时的一行

模型的回复撞到输出上限时，引擎把这次 exchange 的停止原因记为 `truncated`（[agent-loop.md](agent-loop.md) §4.1）。`terminal` 仍是 `converged`，
事件帧不带停止原因，所以 `_pump` 看不到。`Repl._drive` 在 `handle.wait()` 之后读 `handle.outcome.result.stop_reason`，是 `truncated` 就用黄字打印一行
（`_cut_off_notice`）。这一行在回复和 `Turn N done` 之后、下一个提示符之前。exchange 留下的卡片提示符先收掉再打印，一次 exchange 至多一行，后面不补空行。

原文有两种，看被截断的那条回复带不带工具调用：

```
The reply was cut off at the output limit and is incomplete. Ask for a shorter answer, or for it in parts.
The reply was cut off at the output limit, so the tool calls in it were not run: write_file. Ask for the work in smaller pieces.
```

- 不带调用（只有文字，或只有推理）用第一种。
- 带调用用第二种，`write_file` 的位置是调用名的列表：每个调用一个名字，按模型写的顺序，用 `, ` 隔开，同名的不合并。名字过 `inert_line`，空的或只含空白的显示成 `?`。
- 被截断的回复里的工具调用一个都没有执行，参数完整的也没有。工具调用在执行前才上屏，所以用户只能从这一行知道模型本来要调什么。
  同一次 exchange 里更早的模型调用带的工具已经执行，不在列表里。
- 照建议再发一条消息时，没执行的调用已经在开场被去掉（[context-engineering.md](context-engineering.md) §7），请求能正常发出去。

一次性执行（`--prompt`、`--prompt-file`）走同一个 `_drive`，这一行在标准输出上，跟在被截断的回答后面，退出码仍是 0。

两种形式都以 `The reply was cut off at the output limit` 开头。这个开头是脚本匹配的事实接口，改这几个字要当接口改，
`tests/entry/test_cli_repl.py::test_the_two_forms_read_as_agreed_and_open_with_the_same_words` 把两句原文和这个开头都写死了。
脚本判断"这次回复被截断了"时匹配行首的这个开头，不要比整句：这一行按 rich 探到的宽度折行，宽度小于整句的长度时整句不在一行上，折出来的行有的行尾带空格。

- 宽度从哪里来：设了 `COLUMNS`（正整数）就用它，不再看终端。没设时 rich 依次问 stdin、stdout、stderr，用第一个连着终端的流的宽度；三个都不连终端才是 80 列。
  所以 stdout 进了文件或管道，折行位置也不一定是 80 列。在终端里敲 `oc cli --prompt … > answer.txt` 时 stdin 和 stderr 还连着终端，用的是那个终端的宽度。
- 宽度放得下整句时不折行。第一种形式 106 个字符；第二种带一个 `write_file` 时 128 个字符，名字越多越长。
- 宽度太小时开头也会被折断，按行首匹配就找不到它：第一种形式在宽度小于 41 列时，第二种在宽度小于 42 列时（`limit` 后面的逗号和它不分行）。
- 要稳妥，调用时设一个比整句长的 `COLUMNS`，如 `COLUMNS=1000 oc cli --prompt … > answer.txt`。整句在一行上，行首就是这个开头。回答的正文不按这个宽度折行（`MarkdownStreamFormatter` 用 `soft_wrap` 写），不受这个值影响。

这些是在 Linux 上用 rich 14.2.0 和 15.0.0 量到的，Windows 上没有量。

下面这些场合不出这一行：

| 场合 | 现在的行为 |
|---|---|
| 子代理的回复被截断 | 父代理的 exchange 没有被截断，不出这一行。子代理写了文字时，`task` 的结果以 `[<name>] was cut off at the output limit; …` 开头，结果预览里看得到；没写文字时 `task` 报错 |
| 摘要调用被截断（自动压缩、`/compact`） | 没有提示。摘要器只取回复的文字，被截断的摘要按完整的写回 |
| 到达轮数上限（`--max-turns`） | 没有提示 |
| `length`、`max_tokens` 之外的中断（如 `content_filter`） | 引擎不算截断，不出这一行 |
| 取消、失败 | 只有 `Cancelled.` 或 `Failed: <类型名>` |
| Desktop、Channel | 不经过 `_drive`，没有这一行 |

---

## 10. 输入源

`omicsclaw/entry/cli/_input.py`，REPL 从不直接读 `sys.stdin`：

| 实现 | 何时使用 |
|---|---|
| `PromptToolkitSource` | stdin 是终端且装了 prompt_toolkit：历史（`~/.config/omicsclaw/history`）、历史自动建议、命令补全；多个并发读者排队 |
| `StreamSource` | 非终端（`oc cli < questions.txt`）或没装 prompt_toolkit；`readline` 在工作线程执行，不阻塞事件循环 |
| `ScriptedSource` | 固定列表：测试与一次性执行 |

`open_prompt_source()` 按上述顺序降级而不是失败；prompt_toolkit 在函数体内导入，不是硬依赖。

`PromptToolkitSource` 与 `StreamSource` 还实现可选协议 `FreshSource`：`read_fresh(prompt, discarded=…, unfinished=…)` 只返回提示符打开之后键入的行，
`withdraw()` 收起一个被取消的读留下的提示符。卡片用 `read_fresh` 读（§7.5），主提示符仍用 `read`。`unfinished` 只有
`PromptToolkitSource` 会调用：它看得见提示符打开之前敲了一半的行，并把这一行的后半截一起丢掉。`StreamSource` 的流不是终端时，
`read_fresh` 与 `read` 相同。`ScriptedSource` 不实现这个协议。

---

## 11. 配置向导：`oc cli --configure`

`omicsclaw/entry/cli/_configure.py`（移植自旧 `setup_wizard.py`，问题从约 30 个砍到 5 个 + 3 个可选段）：

1. LLM：provider（13 个预设：deepseek、openai、anthropic、gemini、nvidia、siliconflow、openrouter、volcengine、dashscope、moonshot、zhipu、ollama、custom；默认 `deepseek`）、key、model、endpoint；
2. workspace；
3. 可选：Telegram、Feishu、Desktop token。

写入规则：只改用户回答过的键，**保留注释、空行、顺序和不认识的变量**；先备份为 `.env.backup-<timestamp>`，再写临时文件原子替换；
密钥从不回显（只显示末 4 位），也不进日志。保存后回读文件，按 `AppConfig` 的顺序（`OMICSCLAW_PROVIDER` → `LLM_PROVIDER`，`OMICSCLAW_MODEL` → `LLM_MODEL`）取 provider 与模型交给 `resolve_config`，打印"本部署现在解析为：provider / model / endpoint / api key"。

变量名规则（与 Desktop `PUT /providers` 共用 `names_to_write`）：provider 写文件里已有的 `OMICSCLAW_PROVIDER` 和 `LLM_PROVIDER`（两个都在就都写成同一个值），都没有时写 `LLM_PROVIDER`；
模型（`OMICSCLAW_MODEL`、`LLM_MODEL`）与端点（`<PROVIDER>_BASE_URL`、`LLM_BASE_URL`、`OMICSCLAW_BASE_URL`）同理，已有的写法全部写成同一个值，
所以留空的回答不会让低优先级的旧值漏出生效；密钥写已有的最高优先级写法。
picker 的默认项按 `OMICSCLAW_PROVIDER` → `LLM_PROVIDER` → 按 key 探测取当前 provider；provider 不变时，模型、端点、已存密钥的默认值取第一个非空的写法。
向导只读写 `.env` 文件，看不到启动前导出的变量：导出的同名或其他写法的变量仍会覆盖写入的值（Desktop 的保存会在 `shadowed_by_environment` 里列出，CLI 不会提示）。
没装 questionary 时回退到标准库的 `StreamPrompter`。

没有配置任何 key 时（ollama 等无 key 后端除外），`oc cli` 启动时在 stderr 打印一行：
`omicsclaw: no LLM API key is configured. Set one up with:\n    oc cli --configure`。

---

## 12. 会话与工作区

| 路径 | 内容 | 来源 |
|---|---|---|
| `<workspace>/.omicsclaw/memory.db` | 会话（`SqliteSessionStore`）与长期记忆，一个库 | `entry/memory.py`；`--memory false` 时会话只在内存 |
| `<workspace>/.omicsclaw/MEMORY.md` | 长期记忆的摘要（précis） | `entry/memory.py` `PRECIS_FILENAME` |
| `<workspace>/.omicsclaw/settings.json` | 权限规则（`a` 写入处） | `AppConfig.permission_rules_path()` |
| `<workspace>/.omicsclaw/plans/` | 每会话执行计划 | `AppConfig.plans_root()` |
| `<workspace>/.omicsclaw/agents/` | 子代理定义 | `AppConfig.agents_root()` |
| `<workspace>/.omicsclaw/tool_results/` | 压缩时 offload 的大工具结果 | `entry/compaction.py` `TOOL_RESULTS_DIRNAME` |
| `<workspace>/.omicsclaw/compaction_records/` | 压缩记录（JSONL） | `RECORDS_DIRNAME` |
| `~/.config/omicsclaw/history` | REPL 输入历史 | `_input._history_path()` |

workspace 默认是启动时的当前目录（`--workspace` / `OMICSCLAW_WORKSPACE` 可改），它同时是文件工具的沙箱根、
`skills/` / `.mcp.json` 的查找位置；运行时契约 `OMICSCLAW.md` 从 `skills/` 旁读。**在数据目录里启动 `oc cli` 会找不到仓库的 `skills/` 和契约**，
此时用 `--skills-dir` / `OMICSCLAW_SKILLS_DIR` 指回仓库，两者同时就位；把它写进仓库根的 `.env`，每次启动都生效。

`/sessions` 会说明本部署是否持久化会话；`oc cli --session <id>` 或 `/resume` 继续旧会话。

---

## 13. 常用部署 flag 与环境变量

全部定义在 `omicsclaw/entry/config.py` 的 `_OPTIONS`；每个 flag 都有对应环境变量，flag 胜出。节选：

| flag | 环境变量 | 默认 | 说明 |
|---|---|---|---|
| `--workspace` | `OMICSCLAW_WORKSPACE` | 当前目录 | 沙箱根 |
| `--model` | `OMICSCLAW_MODEL`, `LLM_MODEL` | `""`（provider 预设默认） | |
| `--provider` | `OMICSCLAW_PROVIDER`, `LLM_PROVIDER` | `""`（按 key 探测） | |
| `--tool-timeout` | `OMICSCLAW_TOOL_TIMEOUT_S` | `600` | 工具上限的唯一来源；`bash` 用它减 15 s |
| `--max-turns` | `OMICSCLAW_MAX_TURNS` | `50` | 每次 exchange 的模型调用上限 |
| `--turn-timeout` | `OMICSCLAW_TURN_TIMEOUT_S` | 无 | 单次 exchange 墙钟上限 |
| `--approval-timeout` | `OMICSCLAW_APPROVAL_TIMEOUT_S` | 无 | 单次审批期限，到期拒绝；提问共用这个期限，到期为 `no_answer` |
| `--ask-user` | `OMICSCLAW_ASK_USER` | 开 | `ask_user` 工具；只在终端 REPL 生效（§7.4） |
| `--system-prompt-file` | `OMICSCLAW_SYSTEM_PROMPT_FILES` | skill 树旁的 `OMICSCLAW.md` | 替换契约，`:` 分隔 |
| `--skills-dir` / `--skills-index` | `OMICSCLAW_SKILLS_DIR` / `OMICSCLAW_SKILLS_INDEX` | `<workspace>/skills` / `full` | 见 agent-skills.md |
| `--compact-at` | `OMICSCLAW_COMPACT_AT` | `warn` | 触发压缩的最低压力档 |
| `--memory` / `--planning` / `--subagents` | `OMICSCLAW_MEMORY` / `OMICSCLAW_PLANNING` / `OMICSCLAW_SUBAGENTS` | 均为开 | |
| `--permission-mode` / `--permission-rules` | `OMICSCLAW_PERMISSION_MODE` / `OMICSCLAW_PERMISSION_RULES` | `default` / `.omicsclaw/settings.json` | |
| `--sandbox` 及 `--sandbox-*` | `OMICSCLAW_SANDBOX*` | `off` | Docker 隔离 `bash` |
| `--mcp-config` | `OMICSCLAW_MCP_CONFIG` | `<workspace>/.mcp.json` | |
| `--audit-log` | `OMICSCLAW_AUDIT_LOG` | 无 | 每次工具调用一行 JSONL |
| — | `OMICSCLAW_CLI_PERMISSION_MODE` | — | 仅 `oc cli` 读取，由 `/auto` 写 |
| — | `LLM_API_KEY` / `LLM_BASE_URL` / 各厂商 `*_API_KEY` | — | 由 `omicsclaw/provider` 读取，见 `.env.example` §1 |

非法值直接拒绝（`AppConfigError`，退出码 2），而不是回退默认值——`OMICSCLAW_TOOL_TIMEOUT_S=6OO` 悄悄变成 60 s 正是要避免的。
完整清单见 `.env.example`（按 12 节组织）。

---

## 14. 已知限制

1. **没有 TUI**。Textual TUI 未移植（会拖入整个 `RunRuntime` 与旧记忆族），`textual` 也未安装。
2. **一次性执行无法审批，也不提问**：`--prompt` 下所有 `ASK` 工具被拒，需要 `--permission-mode auto-approve`；`ask_user` 在 `--prompt` 与管道输入下不挂载。
3. **日志只保留尾部**：退出后只回放最后 4000 字符；`--log-file` 部署 flag 记为 plan 0037 附录 A 的债务。
4. **目录中有大量不实现的旧命令**（`/run`、`/research`、`/memory`、`/install-skill` …），出于移植保真测试保留，只回答"不可用"。
5. **会话 id 与 `/clear`**：`/clear` 并不清空当前会话，而是开一个新 id；旧会话仍可 `/resume`。
6. **没有常驻上下文窗口占用显示**（FRAMEWORK-REBUILD 记录 context-window reporting #20 仍开放）。
7. **没有跑过真实 provider 的端到端验收之外的持续测试**：界面层对真实 provider / 真实终端的覆盖仍是债务（FRAMEWORK-REBUILD "Debts carried forward"）。

---

## 15. 文件索引

| 文件 | 内容 |
|---|---|
| `omicsclaw/launch/__init__.py` | `main`、`_adopt_dotenv`、退出码语义 |
| `omicsclaw/launch/__main__.py`、`omicsclaw/__main__.py`、`omicsclaw.py` | 其他启动写法 |
| `omicsclaw/launch/_grammar.py` | `COMMANDS`、`split_command_line`、`HELP_FLAGS`、`usage` |
| `omicsclaw/launch/_dotenv.py` | `DOTENV_FILE`、`dotenv_candidates`、`dotenv_target` |
| `omicsclaw/launch/_surfaces.py` | `CLI_USAGE`、`CLI_FLAGS`、`ReplOptions`、`start_cli`、`_run_cli`、`_release`、`_interrupts`、`_replay`；Desktop/Channel 启动 |
| `omicsclaw/entry/config.py` | `AppConfig`、`resolve_app_config`、全部部署 flag |
| `omicsclaw/entry/cli/__init__.py` | 包说明与公开符号 |
| `omicsclaw/entry/cli/_repl.py` | `Repl`、`run_once`、审批、提问卡片、截断提示（`_cut_off_notice`）、`/sessions` `/resume` `/compact` 等 |
| `omicsclaw/entry/question.py` | `QuestionBroker`、`question_card`、`reply_hint`、`read_reply` |
| `omicsclaw/entry/cli/_slash_command_support.py`、`_constants.py` | 命令目录、`REPL_SLASH_COMMAND_SPECS`、`slash_token` |
| `omicsclaw/entry/cli/_activity.py` | `ActivityLine` 活动行 |
| `omicsclaw/entry/cli/_transcript.py` | `ToolTranscript` |
| `omicsclaw/entry/cli/_markdown.py`、`_reasoning.py`、`_screen.py` | 流式 Markdown、推理块、横幅与日志重定向 |
| `omicsclaw/entry/cli/_input.py` | `PromptSource` 三种实现、补全、历史 |
| `omicsclaw/entry/cli/_shell.py` | `!` Shell |
| `omicsclaw/entry/cli/_auto.py` | `/auto`、`OMICSCLAW_CLI_PERMISSION_MODE` |
| `omicsclaw/entry/cli/_configure.py` | 配置向导、`missing_credential_hint` |
| `omicsclaw/entry/render.py` | `TextRenderer` |
| `tests/launch/` | 文法、flag 认领、`.env`、环境读取位置、分层 |
| `tests/entry/test_cli_*.py` | REPL、命令、审批范围、活动行、Shell、配置、补全、推理、transcript |
| `docs/plans/0037-launch-and-entry-points.md`、`0048`、`0049`、`0050`、`0051` | 外壳设计、flag 认领、会话授权、`/auto`、撤回 `/skill-name` 与选择器 |
