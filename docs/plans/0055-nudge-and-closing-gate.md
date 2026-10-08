# 计划 0055 — nudge/gate 家族：组合接缝、收尾提示与记忆提醒

**状态**：第二版（2026-09-23）。经架构审核与 harness9 对照，owner 按推荐裁定，已据此返工。B1、B3 已于 2026-10-08 交付（分支 `feat/memory-search-and-nudge`，待审核与 owner 验收），交付记录见 §10；B2 `ClosingGate` 未做。

**2026-10-07 核对**：本计划的任务都还没有实现。

**缘起**。owner 裁定 2（2026-09-23）自 0047 拆出。**0027 §12.2.2**（:647-648）把
`WithStallNudge`、`WithMemoryNudge` 推迟到"一个 `TurnAugmentor` 实现"，§12.7 表第 8 行（:1082）把
nudge 归 `TurnAugmentor`；**0030 §11.B-12**（:2273-2311）以 nudge/gate 家族替代输出预算轴，并警告
`10 / 12 / 5` 与"进展工具 = `edit_file`/`write_file`"是 SWE-bench 标定的；**0035**（:183）列为
"另一步"；**0040 §10-4**（:425-431）：提取只在压缩时跑，从不压缩的短会话什么都记不下。今天只
落地了规划闸门（`omicsclaw/planning/injector.py`）。第二版按架构审核 M5、M6、O2、S10 与 harness9
对照返工（§9）。**前置**：无硬前置；B2 的子代理部分在缺陷修复 D2 之后；子代理工具表以 G1 为准（已在工作树，§2、§3.4）。

---

## 1. 目标

| # | 目标 | 可观察的结果 |
|---|---|---|
| B1 | 多个 `TurnAugmentor` 的组合接缝 | 引擎仍只收一个 augmentor，`omicsclaw/engine/` 一行不改；只有规划时交给引擎的**就是**今天那个 `PlanInjector` 实例 |
| B2 | 收尾提示 `ClosingGate` | 主代理与子代理在剩余调用数降到阈值时收到一次"收尾"提示；此后不再有记忆提醒 |
| B3 | 长期记忆提醒 `MemoryNudge` | 由短交换组成的会话也会被提醒调用 `memory_write`；子代理不挂 |
| — | 停滞提醒 `StallNudge` | **不做**（Q3），附可证伪的重开判据 |

## 2. 现状（行号以 2026-09-23 工作树为准，按符号名定位）

**augmentor 接缝。** `TurnAugmentor`（`engine/augmentor.py:12-65`）只追加到**发送副本**、不进
`RunResult.messages`、不累积（:38-45），每轮调用一次（:16-19），异常外抛（:60-63）。`AgentEngine` 的
四个运行入口都只收**一个** `augmentor`（`engine/loop.py:189-194`、:226-231、:254-261、:299-306）。
`_kernel`：`turns += 1`（:387）→ compactor（:394-399）→ `augment`（:400-409）→ `generate_with_retry`
（:411-417），**一个实例被调用的次数就是当前轮号**；`max_turns <= 0` 不设上限（:386）；每轮向 history
追加恰好一条 assistant 消息（:436）。`engine.__all__` 由 `test_engine_is_a_leaf_layer.py:228` 钉住。

**entry 接线与规划闸门。** `_Exchange.augmentor`（`entry/turn.py:237-250`）；`_assemble` 里是
`build_injector(app, session_id=session_id) if plan_block else None`（:278），`TurnRunner._sequence`
传 `plan_block=not self._force`（:653-661），`run_turn`、`stream_turn` 用默认值（:386、:429）。
`build_injector`（`entry/planning.py:60`）在 `app.plans is None` 时返回 `None`；该模块只在
`TYPE_CHECKING` 下 import `AgentApp`（:31-32）。`entry/assembly.py` 在模块顶层 import `.subagent`
（:161），`AgentApp` 定义在其后（:742）。`PlanInjector`（`planning/injector.py:110-226`）每交换
一个实例，先闸门后计划块（:166-173）；**`_gate_fires` 在已有计划时直接返回 `False`**（:212-213）；
进展工具集 `{"write_file", "edit_file"}`，刻意不含 `bash`（:92-107）；默认 8 轮由 harness9 的 12
按预算比例重推（:51-71）；文案英文、工具名插值（:73-90）。`planning.__all__` 连同常量一起公开。

**context 层。** `context/__init__.py` 自述 "what the model is shown, and what it costs"；
`ProgressiveCompactor` 结构上满足引擎的 `HistoryCompactor`、不 import 引擎；`MemoryExtractor` 在
`context/compaction.py`。`tests/context/test_context_is_a_leaf_layer.py`：`_ALLOWED_INTERNAL` 只有
schema 与 context 自身（:49），`_TEMPTING_NEIGHBOURS` 含 `omicsclaw.memory`（:59-65），模块按 `rglob`
收集（:79-86）；`test_the_public_surface_is_exactly_what_plan_0030_delivered`（:463-545）钉住有序
的 `__all__`；行为探针 `_BEHAVIOUR_PROBE`（:292-410）写明"公开函数增加时清单要跟着长"。

**子代理。** `ChildRunner.delegate`（`entry/subagent.py:212-254`）的 `engine.exchange_stream(...)`
（:244-246）不带 augmentor 也不带 compactor；子引擎预算来自 `ChildRunner._engine_config`
（:276-281）：父的 `engine_config()`，定义设了 `max_turns` 时替换它。缺陷修复 G1（工作树，未提交）
已把不下发给子代理的工具改为带理由的映射 `_WITHHELD_FROM_SUB_AGENTS`（:55-60，`plan_write` 与
`memory_write`），由 `_child_registry`（:256-274）剔除；`subagent.py` 因此已 import `.memory`（:43）。

**记忆与配置。** 提取只在压缩时发生（`entry/compaction.py:109-111`、:143）；
`MEMORY_WRITE_TOOL_NAME`（`entry/memory.py:81`）；记忆工具只在 `memory` 非 `None` 时由
`foundation_tools` 挂载（`entry/assembly.py:360-361`）。旋钮先例 `AppConfig.planning_gate_turns`
（`entry/config.py:246-254`，默认值自 `omicsclaw.planning` 导入，:74；该模块已 import
`omicsclaw.context`，:71）+ `_Option`（:707-712）；`max_turns = 50`（:169）；`engine_config()`（:491-500）
也是 `AgentApp.engine` 的来源（`assembly.py:1233-1235`）。`.env.example` §9（:243-249）；`READ_BY_THE_STACK`。

**参考实现**（harness9，第三轮复核）：交互产品只接 `WithMemoryNudge(10, …)`（`cmd/harness9/main.go:440-446`），
停滞、规划、收尾只在 SWE-bench 跑道接线（`cmd/swebench/runner.go:316-322`；常量 :31-58：上限 80、
停滞 10、收尾 5、规划 12）。全部 nudge 写在引擎 `prepareTurnInput`（`internal/engine/loop_phases.go:154-218`，
nudge 在 :176-216），与压缩同一阶段，互不感知，没有组合器。记忆按 `lc.turns % interval`（:177，
每 interaction 从零数）；收尾 `maxTurns - turns <= threshold`、每 interaction 一次（:188-195）；轮数语义
与本仓库一致（`loop.py:386-387`、:400-417）。

## 3. 设计

### 3.1 分层（O2、M5）

| 放什么 | 在哪 | 理由 |
|---|---|---|
| `ClosingGate`、`MemoryNudge` 与四个常量 | `omicsclaw/context/nudge.py`，经 `context.__all__` 公开 | 属"发给模型的视图"，与 `ProgressiveCompactor` 同类；白名单同为只有 schema |
| `AugmentorChain`、`chain()`、`closing_gate()`、`build_augmentor()` | `omicsclaw/entry/nudges.py` | 链也组合 planning 的 `PlanInjector`，是 `TurnAugmentor` 接缝的通用组合器而不是 nudge；成员与顺序是组合根的知识 |
| `TurnAugmentor` | `engine/augmentor.py`，不动 | 0027 §12.7 第 8 行：引擎不该知道"什么叫收尾""哪个工具是记忆" |

- `context/nudge.py` 只描述"什么时候说什么"，文案与阈值是参数；两个类**结构上**满足
  `TurnAugmentor`，不 import 引擎。`MemoryNudge` 只以字符串收 `write_tool`（entry 传
  `MEMORY_WRITE_TOOL_NAME`），不 import `omicsclaw.memory`——既有的诱惑邻居测试直接执行这一条。
  常量名比第一版多了 `_GATE`/`_NUDGE`：进了 `context` 的平铺命名空间，`MEMORY_TEXT` 与
  `MemoryExtractor` 并列会指代不明；新名与旋钮名对应。
- **不新增分层测试（S10）**：`nudge.py` 一落地就受既有五条约束（白名单、诱惑邻居、动态 import、
  标准库、无 tokenizer）。只改 `__all__` 断言（加六个名字，docstring 的 "Widened once" 补上本计划）
  与 `_BEHAVIOUR_PROBE`（各跑一次两个类的 `augment`）。第一版的 `test_nudge_is_a_leaf_layer.py`
  删去，已有 12 份的 `_imported_modules`、11 份的 `_probe` 不再多一份；抽共享 helper 不在本计划。
- **`entry/nudges.py` 不在运行时 import `.assembly`**：`AgentApp` 只在 `TYPE_CHECKING` 下引用
  （仿 `entry/planning.py:31-32`）。B2 让 `subagent.py` import `.nudges`，而 `assembly.py:161` 先
  import `.subagent`；否则成 `subagent → nudges → assembly → subagent` 的环，`import omicsclaw.entry`
  直接失败。运行时只 import `.config`、`.planning`、`.memory`、`omicsclaw.context`、`omicsclaw.engine`。
- **组合器的升级条件**：entry 以外出现第二个调用方时，按 `hooks/chain.py` 的先例（链机制与它
  组合的协议同包，零个 hook 不包装，顺序留在组合根）把 `AugmentorChain`/`chain` 提到
  `engine/augmentor.py`，`engine.__all__` 加两个名字。今天只有 entry 一个调用方，不扩钉住的公开面。

### 3.2 B1 组合接缝

```python
# omicsclaw/entry/nudges.py
class AugmentorChain:
    """Asks each member in order and returns everything they append, in member order."""
    def __init__(self, members: Sequence[TurnAugmentor]) -> None: ...
    async def augment(self, history, tools=()) -> tuple[Message, ...]:
        """Every member's messages for this call; each member sees the same history."""

def chain(*members: TurnAugmentor | None) -> TurnAugmentor | None:
    """The given members as one augmentor: None, the only member itself, or a chain."""

def build_augmentor(app: AgentApp, *, session_id: str = "") -> TurnAugmentor | None:
    """The augmentor for one main-agent exchange of *session_id*, or None."""
```

- `chain()` 先丢掉 `None`：零个成员返回 `None`，一个**原样返回该成员**，多个才包成链。B1 单独
  交付时，`_assemble` 交给引擎的就是 `build_injector` 返回的那个 `PlanInjector`，行为逐字不变。
- 成员顺序 `ClosingGate` → `MemoryNudge` → `PlanInjector`：计划块仍是模型最后读到的东西；前两者
  的先后是 §3.5 的抑制所需，两者永不同轮出现，与 harness9（记忆在前）没有可观察的差别。
- `_assemble` 的形参 `plan_block` 改名 `augment`（它现在决定整条链），compaction-only 交换仍是
  `None`。链不吞异常；本计划的成员都不抛。

### 3.3 B2 `ClosingGate`

```python
# omicsclaw/context/nudge.py
DEFAULT_CLOSING_GATE_TURNS = 3
CLOSING_GATE_TEXT = (
    "You are close to this run's limit: {remaining} model calls remain, this one "
    "included. Stop exploring. Check what you have, then write your final answer — "
    "what you did, what you found, and what is still unsettled. A run that reaches "
    "the limit in the middle of a step ends with no answer at all."
)

class ClosingGate:
    """Tells the model once that the run is about to reach its turn limit."""
    def __init__(self, *, max_turns: int, within: int = DEFAULT_CLOSING_GATE_TURNS,
                 text: str = CLOSING_GATE_TEXT) -> None: ...
    def closing(self) -> bool:
        """Whether the run has reached its closing window."""
    async def augment(self, history, tools=()) -> tuple[Message, ...]:
        """The reminder on the first call with at most the window's calls left after it."""

# omicsclaw/entry/nudges.py
def closing_gate(config: AppConfig, max_turns: int) -> ClosingGate | None:
    """The closing gate for a run of at most *max_turns* model calls, or None if it never fires."""
```

- **`closing` 是方法，不是 property（M6）**：`MemoryNudge` 收的是谓词，`quiet=gate.closing` 传的
  是绑定方法。写成 property 时传进去的是构造时的 `False`，抑制永不生效。
- 计数：每次 `augment` 自增，等于当前轮号 k。有效窗口 `w = min(within, max(1, max_turns // 16))`
  （16 = harness9 的 80 / 5，Q4），`max_turns - k <= w` 时触发、每实例一次，
  `remaining = max_turns - k + 1`。例：`50` → 第 47 次调用、`remaining=4`；`8` → 第 7 次；`5` →
  第 4 次；`2` → 第 1 次。关闭：`within <= 0`、`max_turns <= 0` 或 `w >= max_turns`（如 `1`），
  此时 `closing_gate` 返回 `None`。
- **`max_turns` 与被提醒的引擎同源（S9）**：主代理取 `app.config.engine_config().max_turns`。子代理：
  `ChildRunner.delegate` 先算 `config = self._engine_config(definition)`，用它构造子引擎，再把
  `closing_gate(self._config, config.max_turns)` 作为 `augmentor=` 传给 `exchange_stream`；只有一个
  成员，不经 `chain()`。不反读 `AgentEngine._config`；构造规则只在 `closing_gate` 一处。
- 子代理最需要它（结论是唯一产出；D2 让撞上限的失败说真话，B2 让它少发生）。旋钮
  `closing_gate_turns`（`--closing-gate-turns` / `OMICSCLAW_CLOSING_GATE_TURNS`），`0` 关闭。

### 3.4 B3 `MemoryNudge`

字面移植（每交换第 10、20… 轮）解决不了 0040 §10-4：一次普通的组学问答几轮就收敛
（`injector.py:63-66`）。触发改为**从历史推导的跨交换累计**，与 `PlanInjector` 同一思路：

```python
# omicsclaw/context/nudge.py
DEFAULT_MEMORY_NUDGE_TURNS = 10
MEMORY_NUDGE_TEXT = (
    "If this conversation has produced something worth keeping across sessions — "
    "a preference the user stated, a stable fact about this project, a decision and "
    "why it was made — record it now with `{tool}`. Otherwise ignore this note."
)

class MemoryNudge:
    """Reminds the model, every few model turns, to keep what should outlive the session."""
    def __init__(self, *, write_tool: str, every: int = DEFAULT_MEMORY_NUDGE_TURNS,
                 text: str = MEMORY_NUDGE_TEXT, quiet: Callable[[], bool] | None = None) -> None: ...
    async def augment(self, history, tools=()) -> tuple[Message, ...]:
        """The reminder when the turns since the last write reach a multiple of *every*."""
```

- `n` = 可见历史里、最后一条调用 `write_tool` 的 assistant 消息**之后**的 assistant 消息数（从未
  调用过就从头数）；`n > 0 and n % every == 0` 时触发。
- **每个倍数恰好被看到一次，无需跨交换状态**：交换内每次调用 `n` 加 1（`loop.py:436`）；上一次
  交换以答复结束时又加 1，下一次交换的第一次调用看到的正是这个值。重启进程后照样成立。
- 调用 `write_tool` 即清零；本轮 `tools` 里没有它时不触发（"提醒一个没挂载的工具比不提醒更糟"，
  `injector.py:84-86`）；`quiet()` 为真时不触发（§3.5）。
- 写明而不修的偏差：压缩改变 `n`（同 `PlanInjector`，`injector.py:190-197`）；取消或失败的交换
  不落盘，下一次会重看同一段 `n`，可能再提醒一次。
- **只给主代理；子代理不挂，两层互为兜底。** 挂载层：`build_augmentor` 在 `app.memory is not None`
  （与 `memory_write` 被挂载同一条件）且 `memory_nudge_turns > 0` 时加它；子代理只有
  `closing_gate(...)`，`ChildRunner` 从不构造 `MemoryNudge`。触发层：G1 之后子代理注册表没有
  `memory_write`，子引擎每轮的 `tools` 里也就没有，即使将来有人把它接上子链也不会触发；反过来
  G1 若被回滚，挂载层仍保证子代理不挂。旋钮 `--memory-nudge-turns` / `OMICSCLAW_MEMORY_NUDGE_TURNS`，
  `0` 关闭。

### 3.5 收尾窗口里不提醒记忆（S9、M6）

收尾窗口里模型该做的只有写结论。`MemoryNudge` 不认识 `ClosingGate`，只收一个谓词：`build_augmentor`
传 `quiet=gate.closing`（绑定方法；没有闸门时 `None`）。链里 `ClosingGate` 在前，触发的那次调用上先
进入窗口，记忆提醒随即被压住直到交换结束，被压住的倍数不补发；不在记忆 nudge 里重算窗口，免得阈值
公式有两份。harness9 的记忆与收尾从未同时配置，所以没有这条；本仓库两者默认都开，必须处理。

### 3.6 `StallNudge`：不做（Q3）

(1) harness9 交互产品没接它（`main.go:440-446`）。(2) **规划闸门不覆盖"有计划之后的空转"**
（`injector.py:212-213`）；计划块重述、B2、`turn_timeout_s` 都不是停滞检测——**明确接受的缺口**。
(3) **组学语境里没有"进展"的定义（决定性）**：跑分析用的就是 `bash`，`PROGRESS_TOOL_NAMES` 刻意
不含它；算作进展，停滞检测对只 grep 的模型失效；不算，合法的 skill 运行会被反复打断（0030
§11.B-12 的警告）；按命令文本区分是启发式，无语料可验证。(4) 没有本仓库的轨迹语料来标定窗口。
**重开判据（可证伪）**：真实轨迹中"已有计划、以 `MAX_TURNS` 退出、最后 10 轮既无写文件也无
`skills/*/` 脚本运行"的交换占比不可忽略——它量化理由 2，也给出理由 3、4 缺的定义与窗口。

## 4. 开放问题（均已按推荐裁定，2026-09-23）

| Q | 问题 | 裁定与理由 |
|---|---|---|
| Q1 | nudge 放在哪？(a) `context/nudge.py` + `entry/nudges.py`；(b) 新叶子包 `nudge/` + `entry/nudges.py`；(c) 全放 `entry/nudges.py` | **(a)**（第一版推荐 (b)，按 O2 改）。`context/` 自述就是 "what the model is shown"，`ProgressiveCompactor` 是同类、`MemoryExtractor` 也在这里；白名单完全相同，(b) 只是多一个层、多一份约 300 行的分层测试（S10）。harness9 旁证：nudge 与压缩写在同一阶段 `prepareTurnInput`，都属"发给模型的视图"。(c) 让判定与接线混在一起，也没有叶子测试挡住对 `omicsclaw.memory` 的 import |
| Q2 | `MemoryNudge` 触发规则与默认值 | **跨交换累计、写入清零、每 10 轮**。字面移植在本仓库几乎不触发；"每 K 次交换第一轮提醒"在长交换里一次也不提醒。10 沿用 harness9 交互产品，单位同为"模型轮"；3–5 轮一次的问答约每 2–3 次交换提醒一次；未经语料验证 |
| Q3 | `StallNudge` | **不做**，附重开判据（§3.6）。"实现但默认关"是没有调用者的代码，0027 §12.2.3 的复核（`EngineObserver`）已立下"没有消费者就不做"的先例 |
| Q4 | `ClosingGate` 默认与阈值 | **开，`within=3`，按 `min(within, max(1, max_turns // 16))` 缩放**。harness9 交互产品没开，但那里上限 500，这里 50；子代理更需要。3 按 harness9 5/80 的比例重推（50 × 6.25% ≈ 3.1），推法同 `DEFAULT_GATE_TURNS`；5 本身未经验证（§8） |
| Q5 | 收尾窗口里要不要也压住规划闸门？ | **不压**。要压就得改 `PlanInjector` 或拆开它的闸门与计划块，planning 层不在本计划范围；规划闸门每交换至多一次，落进收尾窗口要"前面一直在写、最后 8 轮只读"，罕见。记入风险 |

## 5. 任务拆分

体例：每个任务单独提交、单独回滚；"定点变异"是交付时必须实测的变异。顺序 B1 → B3 → B2：
B2 最后落地，负责把收尾窗口接到记忆提醒上（§3.5），且排在修复批次 D2 之后。

**B1 组合接缝**（依赖：无；不碰 `omicsclaw/context/`）
- 改：新增 `entry/nudges.py`（`AugmentorChain`、`chain`、`build_augmentor`）；`entry/turn.py` 的
  `_assemble` 改调 `build_augmentor`、形参改名。测试：`tests/entry/test_nudge_wiring.py`。
- 验收：只有规划时 `_assemble(...).augmentor is` `build_injector` 返回的实例；无成员时为 `None`；
  多个成员按顺序拼接、各收到同一份 `history`；`isinstance(AugmentorChain(...), TurnAugmentor)`；
  `tests/planning`、既有 `tests/entry` 不改即绿。
- 定点变异：(1) 链内反转成员顺序 → 顺序测试红；(2) `chain()` 永远包成 `AugmentorChain` → 身份
  测试红；(3) 把前一成员的输出拼进后一成员的 `history` → "同一份 history"测试红。

**B3 `MemoryNudge`**（依赖：B1）
- 改：新增 `context/nudge.py`（`MemoryNudge` 与两个常量）及 `context/__init__.py` 导出；
  `tests/context/test_context_is_a_leaf_layer.py`（`__all__` 断言、行为探针）；`entry/nudges.py`；
  `entry/config.py`（`memory_nudge_turns`、`_Option`）；`.env.example` §9；`READ_BY_THE_STACK`。
- 验收：(1) **三次各 4 轮的交换组成的会话，在第三次交换的第三次调用被提醒，全程恰好一次**
  （0040 §10-4 的核心用例）；(2) 调过 `memory_write` 后计数从零重来；(3) 本轮工具定义里没有
  `memory_write` 时从不提醒；(4) 提醒只在发送副本里；(5) `memory=false` 或 `memory_nudge_turns=0`
  时不挂；(6) `quiet` 谓词为真时不提醒（桩谓词）；(7) context 分层测试全绿。
- 测试：`tests/context/test_nudge.py`；`tests/entry/test_nudge_wiring.py` 一条跨交换端到端（经
  `SessionRegistry`，脚本化 provider 记录每次调用收到的消息）。
- 定点变异：(1) 按本实例调用次数计数（字面移植）→ 验收 1 红；(2) 写入不清零 → 验收 2 红；
  (3) 不看 `tools` → 验收 3 红；(4) 忽略 `quiet` → 验收 6 红；(5) `nudge.py` import
  `omicsclaw.memory` → 诱惑邻居测试红。

**B2 `ClosingGate`**（依赖：B1、B3；子代理部分在修复批次 D2 之后）
- 改：`context/nudge.py`（`ClosingGate` 与两个常量）、导出、`__all__` 断言、行为探针；`entry/nudges.py`
  （`closing_gate`；`build_augmentor` 加成员并传 `quiet=gate.closing`）；`ChildRunner.delegate`；
  `entry/config.py`（`closing_gate_turns`、`_Option`）；`.env.example` §9；`READ_BY_THE_STACK`。
- 验收：(1) `max_turns=50, within=3` 恰在第 47 次调用触发一次，文案含 `4`；(2) `5` 在第 4 次、
  `2` 在第 1 次触发；`1`、`within=0`、`max_turns<=0` 从不触发且链上没有它；(3) 提示只在发送副本
  里；(4) **`closing` 可调用，进入窗口前返回假、进入后返回真**；(5) 主代理 `--max-turns 8` → 第 7
  次调用，`remaining=2`；(6) `max_turns: 5` 的自定义子代理：第 4 次子调用带提示，其余不带，子代理
  发送副本里从不出现记忆提醒；(7) `--max-turns 8 --memory-nudge-turns 3` 的新会话：第 4 次调用有
  记忆提醒（`n=3`），第 7 次只有收尾提示（`n=6`，两者本会同轮出现）；(8) 旋钮可读，`0` 关闭。
- 测试：`tests/context/test_nudge.py`（边界、只一次、缩放、关闭条件、`closing`）；
  `tests/entry/test_nudge_wiring.py`（主代理、与记忆提醒的交互）；`tests/entry/test_subagent_wiring.py`。
- 定点变异：(1) `<=` 改 `<` → 验收 1 红；(2) 去掉"只一次" → 验收 1 红；(3) `closing` 改回
  `@property` → 验收 4 红；(4) `ChildRunner` 不传 augmentor → 验收 6 红；(5) 主代理写死 50 →
  验收 5 红；(6) 不传 `quiet` → 验收 7 红；(7) `MemoryNudge` 排到 `ClosingGate` 之前 → 验收 7 红；
  (8) `entry/nudges.py` 在运行时 import `.assembly` → `import omicsclaw.entry` 报循环 import。

测试命令（`rapids_singlecell` 环境）：`PYTHONDONTWRITEBYTECODE=1 /opt/conda/envs/rapids_singlecell/bin/python
-m pytest tests/context tests/planning tests/engine tests/entry tests/test_env_example.py -p no:cacheprovider
-q -o addopts=""`；第二版基线（工作树含未提交的修复批次）1993 passed, 6 skipped，实施前重测。交付时
同步：README 里程碑、`FRAMEWORK-REBUILD.md` 新增一步、0030 §11.B-12 / 0035 :183 / 0040 §10-4 改指本计划。

## 6. 与 0047 及其他计划的共改点

| 文件 | 本计划 | 对方 | 性质 |
|---|---|---|---|
| `entry/subagent.py` 的 `ChildRunner.delegate` | B2：子交换传 `augmentor=`，`max_turns` 取自同一 `EngineConfig`；import `.nudges` | 0047 A3：同一事件循环加 `TURN_END` 分支；修复批次 D2、G1（剔除映射）；0054 绑 `question=None` | 文本相邻；B2 在 D2 之后；G1 不改变 B2 的写法，只让 §3.4 的触发层成立 |
| `entry/turn.py` | B1：`_assemble` | 0047 A3：`TurnRunner`、`TurnHandle` | 不同符号 |
| `entry/config.py`、`.env.example`、`tests/test_env_example.py` | B2/B3：两个旋钮 | 0047 只在恢复后台委派时才加旋钮；0052/0054 改 `.env.example` :91 附近 | 新变量放 §9、紧挨 `OMICSCLAW_PLANNING_GATE_TURNS` |

两份计划没有语义依赖，可以独立交付、独立回滚，后落地的 rebase。

## 7. 非目标

`StallNudge`（§3.6）；修改 `PlanInjector` 或规划闸门的默认值、把它迁进 `context/`；子代理的
压缩器、规划能力与 `MemoryNudge`；引擎改动（组合器提升见 §3.1 的升级条件）；抽共享的分层测试
helper（S10）；按 surface 区分 nudge 或把它上线格式；自动提取之后清零记忆计数（先观察）。

## 8. 风险

| 风险 | 缓解 |
|---|---|
| 两个默认值（3、10）都是判断，未在本仓库语料上验证（0030 §11.B-12）。3 由 harness9 的收尾阈值 5（上限 80）按比例重推，而 5 本身在 harness9 也未经验证：验证轮只记录"final_edit_unverified 捕获 2 例（可观测）"，同子集翻转在 n=47 的 ±10pp 置信区间内，没有因果证据（harness9 仓库 `docs/技术调研/swebench-v4-质量分析与内核优化建议.md:252-256`） | 都有旋钮、`0` 关闭；交付记录写明 |
| `MemoryNudge` 与压缩时的自动提取写出重复记忆 | `memory_write` 支持更新；观察后再决定提取之后是否清零计数 |
| `max_turns` 很小的既有子代理用例（`test_subagent_wiring.py` 的 `max_turns: 2`）会在第 1 次调用收到收尾提示 | 这些用例只断言输出；实施时 grep `max_turns` 逐条确认 |
| 规划闸门与收尾提示罕见地同轮出现 | Q5；观察到再议 |
| 并发会话同改 `ChildRunner.delegate`（D2、G1、0047 A3、0054） | B2 最后落地，按符号名 rebase |
| 回滚 | 每个任务独立提交；两个旋钮设为 `0` 即回到今天 |

## 9. 审核处置

"0047 审核"的 M1–M9 都属于 A 部分，处置见 0047 §10。下表覆盖与本计划有关的条目。

| 审核条目 | 处置 | 位置或理由 |
|---|---|---|
| 第一轮总体结论"B1–B3 基本可以实施，只需小修"；S14 篇幅；Q1（拆分） | 采纳 | 全文；单独成篇；裁定 2 |
| S9-1 收尾窗口抑制 `MemoryNudge` | 采纳 | §3.5；B2 验收 7、变异 6–7 |
| S9-2 B1 补"永远包成链"变异；S9-3 `max_turns` 同源 | 采纳；子代理同理取自同一 `EngineConfig` | B1 变异 2；§3.3；B2 变异 5 |
| S9-4 / 事实核对：Q15 理由 2 不成立；第一版"唯一共同改动点"；引"0027 §12.8 第 6 条" | 采纳，更正；结论保留 | §3.6；§6；改引 0027 §12.2.3 |
| Q13 → Q1、Q14 → Q2、Q15 → Q3、Q16 → Q4 | Q2–Q4 保留；Q1 按 O2 改为 `context/` | §4 |
| 接缝：`entry/config.py` 等共改；推荐顺序 | 采纳；顺序改为 B1 → B3 → B2 | §5、§6 |
| 第二轮总体结论（M5、M6）、逐项结论第 4 项；M5 组合器放错包 | 采纳：`entry/nudges.py`；零/一/多成员语义；升级条件按 `hooks/chain.py` 先例；`AgentApp` 只在 `TYPE_CHECKING` 下引用 | §3.1、§3.2；B1；B2 变异 8 |
| 第二轮 M6 `quiet=gate.closing` 契约错 | 采纳：`closing` 改为方法，原写法因此成立 | §3.3、§3.5；B2 验收 4、变异 3 |
| 第二轮 O2 nudge 放哪；S10 分层 helper 副本 | owner 裁定 `context/`；S10 随之消解：不新建包、不复制 helper，抽共享 helper 不在本计划 | §3.1；Q1；§7 |
| 第二轮逐项结论第 9 项"0055 引擎不改成立" | 保持 | §1、§7 |
| 第三轮总结论（维持原方案）；O2、M5 行 | 采纳细化（旁证、测试位置、`write_tool` 字符串、组合器语义与位置） | §3.1、§3.2；Q1；B1、B3 |
| 第三轮 M6 行（保留抑制、方法、收尾 5 未经验证、轮数语义一致） | 采纳 | §2、§3.5、§8 |
| 第三轮 O3 行"子代理不给 `memory_write`"（缺陷修复 G1） | 已确认：子代理在挂载与触发两层都不会收到记忆提醒 | §3.4；B2 验收 6 |

## 10. 交付记录（2026-10-08：B1、B3，以及 0040 §10-3 的中文检索）

分支 `feat/memory-search-and-nudge`，基线 `90a3bec3`。中文检索、B1、B3 各一个提交，可单独回滚。B2 `ClosingGate` 与 `StallNudge` 没做。

### 10.1 实际做法

B1 按 §3.2：`entry/nudges.py` 提供 `AugmentorChain`、`chain`、`build_augmentor`，`_assemble` 改调 `build_augmentor`。只有规划时，引擎拿到的仍是 `PlanInjector` 实例本身。

B3 按 §3.4：`context/nudge.py` 的 `MemoryNudge` 数历史里最后一次 `memory_write` 之后的 assistant 消息，到 `every` 的倍数时追加一条 user 消息。`build_augmentor` 在 `app.memory` 非空且 `memory_nudge_turns > 0` 时挂上它，排在 `PlanInjector` 之前。旋钮 `memory_nudge_turns` / `--memory-nudge-turns` / `OMICSCLAW_MEMORY_NUDGE_TURNS`，默认 10，`0` 关闭。`quiet` 参数保留，目前没有调用方传它。

中文检索没有用 `tokenize='trigram'`，建表语句不变。写索引时在每个汉字与相邻字符之间插一个空格（`store.py` 的 `_spaced`），默认分词器就把每个汉字当成一个 token。查询里的一串汉字拆成相邻两字的短语，用 `OR` 连接；单个汉字就查这个字；其余部分照旧整词加引号。英文的分词和原来一样。

### 10.2 短查询方案及其代价

两字词是一个两 token 的短语查询，"QC""DE" 仍是整词 token，都走 FTS 索引，没有 `LIKE` 扫描，也没有按长度分叉的路径。笔记里不逐字出现的中文短语（"空间域识别方法"）靠共有的两字对命中，bm25 排序。

不用 trigram 的依据是 15 条笔记、22 条查询的对比（scratch 的 `proto_compare.py`）：旧实现 9 条达标，trigram 加短词 `LIKE` 14 条，本方案 21 条（剩下那条是只有 `%` 的查询，标点不进索引）。trigram 方案丢的是 "DE"（按子串会淹没在 leiden、model 里）、英文整句（"is" 走 `LIKE` 把无关笔记排到前面）、混合词 "leiden聚类" 和 5 条非逐字的中文短语。给它补词边界匹配和两字拆分能追回一部分，但两字对只能全表扫描。

代价（SQLite 3.51.1，内存库，每行约 250 字节的中英混合合成数据）：

| 行数 | 每次打开扫描索引 | 一次性重写旧索引 | 一次 14 字中文查询 |
|---|---|---|---|
| 1,000 | 15 ms | 0.14 s | 4 ms |
| 3,000 | 45 ms | 0.5 s | 8 ms |
| 10,000 | 150 到 170 ms | 1.8 s | 25 ms |

扫描随索引文本量线性增长，每次构造 `LongTermStore` 都要付。查询数字取自几乎每行都命中的合成数据。两字对匹配偏松："差异分析" 会命中只含"分析"的笔记。汉字在索引里约占原文 1.3 倍的字节。排序上，含汉字的笔记 token 变多，同一个英文词在中英笔记之间的先后可能和以前不同。去重没动。

### 10.3 迁移

没有 DDL。构造 `LongTermStore` 时读一遍索引，找出文本与 `_spaced` 结果不同的行。有这样的行才 `BEGIN IMMEDIATE`，在写锁下重读，按 rowid 删旧行并从 `long_term_memories` 重建；对应条目已删除或已停用的索引行直接丢掉。整个重写是一个事务，失败就回滚并把 `sqlite3.Error` 抛给调用方，下次打开重试。没有旧行的库只读不写，所以不需要版本标记。文件不重建，0600 权限不受影响。

旧版本进程之后写进来的行是未分隔的，新版本下次打开时补上。旧版本读新索引，英文照常。新版本进程运行期间旧版本写入的行，要等新版本下次启动才能按中文子串搜到。

### 10.4 和计划的差异

- §3.2 要把 `_assemble` 的形参 `plan_block` 改名为 `augment`，没改：改名要动 `TurnRunner._sequence` 的调用行，那里另有两个分支在改。
- `MemoryNudge` 多一条 `every <= 0` 时不提醒的判断，否则取模会除零。`quiet` 只在本该提醒的那次调用上被问到。
- `MEMORY_NUDGE_TEXT` 照 §3.4 原文，含破折号；它是发给模型的文本，没有过 humanizer。
- `.env.example` 的新变量放在 `OMICSCLAW_MEMORY` 下一行，和 `OMICSCLAW_PLANNING_GATE_TURNS` 隔一行，避开别的分支在节尾的追加。
- §5 末尾要求同步 README 里程碑、`FRAMEWORK-REBUILD.md`、0030 §11.B-12 与 0035 的指向，这次没做；改了本文件、0040 §10，以及 `docs/core-features/` 里四份文档中已不成立的句子。
- §2 的行号多数已漂移，`_WITHHELD_FROM_SUB_AGENTS` 已是提交过的代码。`READ_BY_THE_STACK` 是抽样清单，原本没有 `OMICSCLAW_PLANNING_GATE_TURNS`，新变量照计划加了。

### 10.5 验证

- 约定范围的测试：基线 1803 passed、1 failed、14 skipped；交付后 1897 passed、1 failed、14 skipped。那 1 条是 `test_golden_deployment`，基线就红（`1540fca7` 改了 `task` 工具描述而没更新 golden 文件）；改动后 system prompt 与 golden 逐字相同，工具表只差那一行。新增与改动的测试在 Python 3.11.15 / SQLite 3.53.0 下也全绿。
- 29 条脚本化 eval 全绿。默认间隔 10 下，`skill_routing/review_then_accept` 的第 14 次调用带上了提醒，其余 28 条没有。
- 定点变异全部变红并逐字节还原：检索与迁移 16 条（去掉单字分支、不回填、引号不转义、不在写锁下重读等），B1 9 条（含 §5 的三条），B3 22 条（含 §5 的五条）。
- 迁移：线上库经 backup API 复制到 scratch。它有 26 个会话、443 条消息、0 条长期记忆，新代码打开后没有任何写入。在副本上用基线代码写入笔记后，新旧版本轮流打开五次：条目与消息的哈希不变，`integrity_check` 为 ok，三个文件保持 0600。3000 行重写到一半 `SIGKILL`：索引全部回滚，下次打开完成重写。
- 真实模型（deepseek-v4-flash，`python -m omicsclaw cli`，`--memory-nudge-turns 2`）：提醒出现在线上请求体里对应那次调用的末尾，下一次请求里没有它，`memory.db` 里也搜不到。会话 A 里模型在提醒之前已自己写了记忆，之后两次提醒都没有重复写，道别那轮的回复多了一段话交代哪些没有记。会话 B 里用户顺口提到常用数据和方法，模型在带提醒的那次调用上回答了问题并写了记忆。提醒关闭的对照会话里模型在最后一轮也自己写了，所以这组对比说明不了提醒的净效果。

### 10.6 没验证的

- CI 没跑（没有 push）。runner 的 SQLite 版本是查文档得到的：ubuntu-24.04 镜像的 libsqlite3 是 3.45.1，setup-python 的 CPython 链接系统库，trigram 自 3.34.0 起可用。本方案只用到基线已在用的 FTS5 功能。
- 默认间隔 10 没有在真实会话上跑过，真实会话用的是 2。
- 只跑了 CLI。Desktop 与 channel 走同一个 `_assemble`，没有实际跑。
- §3.4 记下的偏差里，"取消或失败的交换会再提醒一次" 没有测试。
- 两个进程同时迁移只用同进程的两个连接测过。日文假名和谚文没有分隔，行为和以前一样。

### 10.7 等 owner 定

1. 检索用"汉字逐字加两字对"，还是回到 trigram 加短词扫描。
2. 每次打开全量扫描索引是否可接受；要 O(1) 就得加一个记录已检查 rowid 的标记。
3. 迁移失败时让 `LongTermStore` 构造抛错，还是记日志后继续用旧索引。
4. 假名与谚文要不要一并分隔。
5. 道别轮次上模型会向用户交代记忆情况，提醒文案要不要改。
