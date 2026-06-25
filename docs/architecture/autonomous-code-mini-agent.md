# Autonomous Code 子系统架构设计

> 适用范围：`omicsclaw/autonomous/`。这是 OmicsClaw 在 **skill 没覆盖** 时的"自己写代码"回退路径。
> 关联决策：[ADR 0013](../adr/0013-autonomous-analysis-path.md)（引入回退路）、[ADR 0014](../adr/0014-outer-owned-autonomous-understanding.md)（理解归外层、两个判断接缝）、[ADR 0032](../adr/0032-autonomous-code-mini-agent.md)（持久 kernel mini-agent，本文主体）。
> 术语权威定义见 [`docs/CONTEXT.md`](../CONTEXT.md) §"Analysis routing" 与 [`omicsclaw/runtime/CONTEXT.md`](../../omicsclaw/runtime/CONTEXT.md) 的 `Dynamic workflow` / `Autonomous one-off analysis glue`。

## 0. 一句话定位

当 Analysis Router 判为 **No skill match / Partial skill match** 时，由本子系统**生成并执行受限本地代码**完成分析。它只有**一套引擎、自动生效、无 flag**（2026-06-22 单引擎合并）：**Autonomous Code Mini-Agent** —— 一个在**常驻 Jupyter kernel**（有 bwrap 走完整 OS envelope、无 bwrap 走进程内 guard 的**分层隔离**）上、用 `Purpose/Reasoning/Next Goal/Code` 多步迭代写代码的战术 agent，**主调 vetted skill（而非裸 scanpy）**、被 **AST lint + 隔离层**双重夹住、并以 **replay 重放**为验收闸门。

**核心原则（ADR 0032）**："交执行、不交判断" —— mini-agent 拥有战术循环（写→跑→看→改），但 *inspect / 向用户提问 / 最终结果是否算数* 这三件判断仍归外层 agent loop（ADR 0014 的两个接缝）。

## 1. 它在系统里的位置

```
用户请求
  └─ 外层 agent loop (omicsclaw/runtime/agent/)
       └─ Analysis Router → 分类 exact / partial / no_skill
            ├─ exact   → 确定性 skill 派发（assisted parameterization）  [不在本子系统]
            ├─ partial → 先跑最近 skill，再把 artifacts 交给回退路处理     [skill-first composition]
            └─ no_skill→ 回退路（本子系统）
                 └─ execute_autonomous_analysis_execute   (runtime/tools/builders/agent_executors.py:2059)
                      └─ run_autonomous_code_loop_async(request, *, llm_client, request_tool_approval, runtime_context)
                                                          (autonomous/code_loop.py)   ← 唯一入口
```

**契约不变量**：无论内部走哪套引擎，入口签名与返回的 `AutonomousRunResult`（`.ok / .attempts / .workspace_root / .manifest_path / .completion_report_path / .error / .metadata`）形状不变，所以 `agent_executors.py` 的渲染、Surface、job UI 都无需改动（output-shape parity）。

## 2. 模块速查

```
omicsclaw/autonomous/
  code_loop.py          唯一入口 + 瘦派发器（能力门控 → run/refuse）；ProviderChatClient
  capability.py         模型能力门控（ADR §8）：pre-flight 行为探针 + run/refuse 决策
  mini_agent_runner.py  mini-agent 顶层编排：workspace → envelope 门 → kernel → loop → replay → records
  mini_agent.py         战术循环本体：注入 namespace → 出 turn → lint → 执行 → 反馈 → ReturnAnswer + warmup 护栏
  kernel_session.py     常驻 Jupyter kernel（ZMQ-IPC）：启动/执行/introspect/超时重启/关闭
  kernel_envelope.py    bubblewrap 安全 envelope：--unshare-net + ro/rw bind + 剥密钥 env
  skill_facade.py       注入 kernel 的 `oc` 句柄：把 vetted skill 暴露成可调用函数（Model A 子进程版）
  replay.py             验收闸门：拼 analysis.py，在全新沙箱 kernel 重放，过了才算成功
  protocol.py           每步 LLM 响应契约：Purpose/Reasoning/Next Goal/Code 解析 + ReturnAnswer 检测
  budget.py             预算/台账：步数/失败/skill 调用/token/墙钟 + 终止原因 + 步骤追踪

  # 支撑（ADR 0013 既有，mini-agent 复用）
  contracts.py          AutonomousRunRequest / Result / Attempt / Workspace / PermissionTier
  workspace.py          create_workspace → autonomous-code__<ts>__<id>/
  runner.py             write_run_records（manifest/completion_report/result_summary，output-shape parity）
  validation.py         AST 静态 lint：validate_generated_code（禁 subprocess/os/socket/network/file-IO）
  runtime_guard.py      非 bwrap 层的进程内 guard：build_kernel_guard_code（断网 + 挡破坏性 os + chdir）
```

## 3. 单引擎与派发（`code_loop.py`）

入口 `run_autonomous_code_loop_async` 是个瘦派发器 —— **mini-agent 是唯一引擎、自动生效、无 flag**（2026-06-22 单引擎合并）：

```
run_autonomous_code_loop_async(request, ...)
  capability gate  (capability.mini_agent_gate, 经 asyncio.to_thread)
    ├─ action="run"    → run_mini_agent_request_async(request)  → §4
    └─ action="refuse" → refused_result(request, diagnostic)    （干净 FAILED，不启 kernel）
```

- **无 flag、自动**：autonomous 路径只有 mini-agent 一条引擎；`OMICSCLAW_AUTONOMOUS_MINI_AGENT` 开关与 legacy 一次性引擎都已移除。能力门控对不胜任的模型直接 **refuse**（没有可降级的简单引擎）。
- legacy 一次性引擎及其 `executor.py` / `permissions.py` / `policy.py` / 一次性 `runtime_guard` 模块已**删除**；跨平台运行由 mini-agent 的**分层隔离**承担（§5、§8）。
- `custom_analysis_execute`（LLM 自带 `python_code` 的旧直调工具）已在单引擎合并中**删除**；`autonomous_analysis_execute`（mini-agent）是唯一的生成式代码兜底路径。

## 4. Mini-agent 执行流水线（`mini_agent_runner.run_mini_agent_request`）

```
1. create_workspace(request)                 workspace.py
     → output/autonomous-code__<ts>__<id>/{scripts,logs,figures,tables,artifacts,inputs,upstream}

2. envelope 能力门            kernel_envelope.envelope_available()
     require_sandbox 且无 bwrap ⇒ FAILED（fail-closed，ADR §4：无硬 envelope 不跑裸代码）

3. KernelSession.start()      kernel_session.py        → §5
     bwrap 包裹的常驻 Jupyter kernel，ZMQ-IPC 通道

4. run_mini_agent(...)        mini_agent.py            → §6（战术循环）
     输入：goal + data_schema + analysis_plan（来自外层 ADR 0014 接缝）+ budget
     输出：MiniAgentOutcome(answer, termination, steps, accepted_cells, ledger)

5. validate_replay(...)       replay.py                → §7（仅当 outcome.succeeded）
     accepted_cells 拼成 analysis.py，在全新沙箱 kernel 重放；过 + 写了 answer 文件 才接受

6. write_run_records(...)     runner.py
     manifest.json + completion_report.json + result_summary.md
     metadata 含 step traces / termination / replay_ok / skill_calls / ledger / answer

7. return AutonomousRunResult   contracts.py
     status = SUCCEEDED 当且仅当 ReturnAnswer 且 replay 通过
```

**验收双闸门**：一次成功必须同时满足 ① 循环内 `ReturnAnswer` 写了 sentinel 文件 ② replay 在全新进程重放通过。活循环到了 `ReturnAnswer` 但 replay 失败 = 失败（杜绝"假可复现"）。

## 5. 常驻 kernel 与安全 envelope

### 5.1 KernelSession（`kernel_session.py`）

一个 autonomous run 一个常驻 kernel，**状态跨步存活**（大 h5ad 只加载一次，迭代便宜）。

- **通道走 ZMQ-IPC（unix socket）而非 TCP**：这样 `--unshare-net` 能真断网而不破坏 kernel↔client 通信。
- **IPC socket 路径放在独立的短 `/tmp/ock-*` 目录**（bind 进沙箱），因为 **AF_UNIX 路径上限 107 字符**，而真实 `output/autonomous-code__<ts>__<id>/…` 目录名很长会顶破（`_short_tmp_base` + start() 里的长度护栏）。
- `execute(code, timeout)`：发 cell → 排 iopub（按 msg_id 匹配，收集 stdout/stderr/error/result_text，到 `idle` 结束）→ 超时则 `_interrupt()` + **`_restart_after_timeout()`**（关旧 kernel、清连接目录、尝试重启）——ADR §4"超时若不能 idle 就重启"，状态不泄漏到后续 cell。
- `introspect()`：跑探针 cell 返回 `{var: {type, shape?}}`，循环用它做 before/after 变量 diff。
- `kernel_ipc_available()`：探测本环境能否建 IPC socket；CI/沙箱禁 IPC 时测试据此优雅 skip。
- `sandbox=False`：无 bwrap 时的非-OS-envelope 模式；默认（`require_sandbox=False`，分层）由 mini-agent 追加**进程内 guard**兜底，仅 `require_sandbox=True`（strict）时 fail-closed。

### 5.2 Kernel Safety Envelope（`kernel_envelope.py`）

ADR §4 的硬边界。`build_bwrap_argv(config, inner_argv)` 构造：

- `--unshare-net`（默认）—— 无网络（基因数据不出本机）；`--unshare-pid/uts/ipc/cgroup`；`--die-with-parent`；
- `--ro-bind`：系统路径 + 解释器 prefix + 仓库 + **声明的 input/upstream**（只读）；
- `--bind`：workspace（唯一可写）+ ipc 目录；`--proc/--dev/--tmpfs /tmp`；
- env 由 `build_launch_env` 经 `Popen(env=...)` 注入（bwrap **0.4.0 无 `--clearenv`**，故走父进程环境透传）：白名单保留 PATH/CONDA/locale/线程数等，**剥掉一切 `*_KEY/_TOKEN/_SECRET`**，`HOME` 钉在 workspace。
- **本机隔离原语 = bubblewrap**（ADR open question 据实定）。无 bwrap → 默认降级到**进程内 guard**（分层，跨平台）；仅 `OMICSCLAW_AUTONOMOUS_REQUIRE_SANDBOX=1` 时 fail-closed。

## 6. 战术循环（`mini_agent.py`）

```
init cell (build_init_code): 注入 sys.path(repo) / matplotlib(Agg) /
   oc = build_facade(workspace, max_skill_calls, skill_timeout)   ← skill_facade
   ReturnAnswer(text)  → 写 workspace/_oc_answer.txt + 打印哨兵
   show(...)           → 存 figures/
   adata               → 若有 .h5ad 输入则 anndata.read_h5ad 预绑定

while True:
   [warmup 护栏]   未产出"可解析+过 lint"的 turn 且 步数≥min(3,max_steps) ⇒ MODEL_INCAPABLE
                   （排在预算检查之前，故报精确的 MODEL_INCAPABLE 而非巧合的 CONSECUTIVE_FAILURES）
   [预算护栏]      ledger.exhausted_reason ⇒ STEP/CONSECUTIVE_FAILURES/SKILL/TOKEN/WALL_CLOCK
   raw = llm.complete(system_prompt + transcript)        token 计入 ledger
   turn = parse_turn(raw)            protocol         失败 → 回灌格式错误，continue
   issues = validate_generated_code(turn.code)         lint 拦截 → 回灌，continue   （此后标记 produced_usable_turn）
   timeout = 1800 if 引用 oc else 120
   cell = session.execute(turn.code, timeout)
   new_vars = introspect 差集 → 组装 feedback 回灌
   若 cell.timed_out          ⇒ ENGINE_ERROR（kernel 已重启、状态丢，止）
   若 ReturnAnswer 且写了哨兵文件 ⇒ RETURNED_ANSWER（answer 从文件读，非静态解析）
```

要点：
- **ReturnAnswer 必须真写哨兵文件**才接受 —— 防止写在未执行函数里的 `ReturnAnswer` 假成功。
- **per-cell 超时分两档**：引用 `oc`（内含 skill 子进程，可能分钟级）给长超时；纯裸代码给短超时。
- transcript 把每步 stdout/error/新变量摘要回灌给下一步（多步反馈式）。
- 外层接缝（ADR 0014）不在这里：`data_schema` / `analysis_plan` 是外层 inspect + 一个 preflight 问题 + schema-grounded plan 的产物，作为只读上下文喂进来。

### `oc` 技能句柄（`skill_facade.py`）

裸 LLM 代码**只能经 `oc` 跑 skill**（lint 禁裸 subprocess）；facade 是*受信注入代码*，豁免 lint、可合法 spawn skill 子进程。`oc.run(skill, adata, method=..., **params)`（或 sugar `oc.spatial_preprocess(adata, ...)`）：

```
materialize adata → skill_calls/NN_<skill>/input.h5ad
→ run_skill(skill, input_path, output_dir, extra_args, cancel_event)   # 子进程，沙箱内；Timer 限时
→ reload 主 artifact（processed.h5ad / 唯一 *.h5ad）→ SkillHandleResult(.adata/.tables/.figures/...)
→ 追加 skill_calls.jsonl（index/skill/method/params/input/output/primary/status/manifest）
```

v1 是 **Model A（子进程版）**：不重构 95 个 skill，直接包既有可 import 的 `run_skill`，代价是一次 write→run→reload 双盘 I/O。`max_skill_calls` 超额抛 `SkillBudgetError`（0 = 禁止，非无限）。

## 7. 能力门控（`capability.py`，ADR §8）

把"模型够不够格驱动 code 契约"从启发式升级为**显式行为门控**，两层：

| 层 | 位置 | 机制 |
|---|---|---|
| pre-flight 行为探针 | 派发处，启 kernel 前 | 1–2 次廉价调用测模型能否产出合法 turn → CAPABLE/INCAPABLE |
| 决策 | `mini_agent_gate` | CAPABLE→run；INCAPABLE→**refuse**（清晰诊断；单引擎、无可降级的简单引擎）|
| 循环内 warmup 护栏 | `mini_agent.py` | 头 `WARMUP_STEPS=3` 步从未产出可用 turn ⇒ `MODEL_INCAPABLE` 早停（探针漏网的兜底）|

`OMICSCLAW_MINI_AGENT_PROBE=0` 可关探针（信任调用方）。`refused_result` 产出 output-shape 兼容的干净 FAILED。

## 8. 安全模型（纵深防御）

| 层 | 机制 | 是边界还是 lint |
|---|---|---|
| OS 隔离 | bubblewrap：`--unshare-net` + ro/rw bind + 剥密钥 env | **硬边界** |
| 静态检查 | AST `validate_generated_code`：禁裸 subprocess/os/socket/网络/文件 IO | lint（可被绕过，非边界）|
| 受控表面 | 裸代码只能调 `oc`；vetted skill 子进程在沙箱内跑 | 把"重活"锁进 vetted 方法学 |
| 能力门控 | 探针 + warmup 护栏 | 防弱模型烧预算 |
| 进程内 guard | 无 bwrap 兜底：断网 + 挡破坏性 os + chdir workspace；`REQUIRE_SANDBOX=1` 才 fail-closed | 跨平台 best-effort（非硬边界）|
| 复现闸门 | replay 在全新进程重放 | "trace to script output" |

> 关键认识：让 SpatialClaw 式"任意写代码"安全的不是沙箱本身，而是**受控工具表面**。OmicsClaw 的受控工具表面 = 它的 95 个 skill（经 `oc`）。AST lint 只是早拦，真正边界是 bwrap 进程隔离。

> **写入约束按隔离层不同**：**OS envelope（bwrap）**用 bind-mount 把可写面钉死在 workspace（硬边界）；**进程内 guard 兜底**只做断网 + 挡破坏性 os + `chdir` 到 workspace，**不**拦截 `open()`/绝对路径写（IPython kernel 里 monkeypatch `open` 无效且会破坏库初始化）。故无 bwrap 时写入受限是 *best-effort* 而非硬保证 —— 需要硬写入边界请用 bwrap 或 `OMICSCLAW_AUTONOMOUS_REQUIRE_SANDBOX=1`。

## 9. 产物与可复现（output-shape parity）

一个 run 的 workspace（`autonomous-code__<ts>__<id>/`）：

```
manifest.json            workspace 清单（runner.write_run_records）
completion_report.json   完成报告 + 状态 + 错误
result_summary.md        计算结果/解释/disclaimer（含 OmicsClaw 免责声明）
analysis.py              replay 制品：init + 已接受 cell，可从 input 重跑到 result
skill_calls.jsonl        有序嵌套 skill 调用 manifest（provenance）
replay/                  replay 用的独立子 workspace
scripts/ logs/ figures/ tables/ artifacts/ inputs/ upstream/
```

每次 skill 调用本身留下完整 vetted 输出目录（被引用、不拷贝），所以 provenance 比裸 codegen 更强。

## 10. 配置 flags

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `OMICSCLAW_AUTONOMOUS_REQUIRE_SANDBOX` | `0`（分层）| 无 bwrap 时的行为：默认 `0`=降级到进程内 guard（跨平台）；`1`=严格 fail-closed（仅 Linux/远程）|
| `OMICSCLAW_MINI_AGENT_PROBE` | `1`（开）| 是否跑 pre-flight 能力探针；关则信任调用方直接 run |

> mini-agent 是唯一引擎、自动生效，**没有开/关 flag**；不胜任的模型由探针直接 refuse（无 `ON_INCAPABLE` 降级开关）。

预算可经 `request.metadata["mini_agent_budget"]` 覆盖（`budget.py` 的 `MiniAgentBudget.with_overrides`，再 clamp）。

## 11. 测试与验证

`tests/test_mini_agent_*.py`（7 文件，62 测试）：
- `foundations` 纯逻辑（protocol/budget/envelope argv）；
- `kernel` / `loop` / `replay` / `runner` 真启 kernel 的集成（无 IPC 环境 `kernel_ipc_available()` 优雅 skip）；
- `facade` mock run_skill 的编排；`capability` 探针/gate/warmup。

验证基线：mini-agent 全套 + autonomous workspace + bot 路由共 78 passed；ruff 干净。

## 12. 关键设计决策 / open questions（ADR 0032）

**已定**：仅 fallback（不替换 skill-first）；runner 有"战术脑"但 supersede ADR 0014 仅限此、保留外层两接缝；nested skill 子进程版（不重构 95 skill）；真常驻 kernel + replay 闸门；**单引擎、自动、无 flag + 分层隔离（bwrap / 进程内 guard）**（2026-06-22 修订）。

**已实现的能力门控**：探针 + warmup 护栏 + refuse（ADR §8 由 open question 收敛为实现）。

**仍 open（按 ADR）**：真·in-process skill API；replay 复用嵌套 skill 输出（现重跑）；循环内暂停/resume 活 kernel（现交接前问）；生成 R 代码；模型能力的 benchmark 套件/provider 能力标签（探针之外的预判）。
