# 技能获取飞轮 —— P0/P1 落地方案（执行级）

> 状态：落地方案 v1（承接 [`skill-acquisition-plan.md`](./skill-acquisition-plan.md) §P0/§P1，收敛为**当前 main 可执行**的形态）
> 所有行号对齐当前 `main`（去仪式后，commit 8cbb80a 之后）实测，非提案原文引用。
> 结论前提：飞轮真瓶颈是**引擎**（晋升逐字回放 + 无入库执行门），**不是表示**。P0/P1 只做「契约管道 + 入库门」，把已有空槽填活。

---

## 0. 原则

1. **不加 skill.yaml schema 字段**——槽全在：`Provenance`(origin/…)、`Lifecycle`(status)、`Validation`(level/evidence)、`ResultJson`(required_keys)。
2. **去仪式协同**：`SkillManifest.to_yaml()` 已是 `exclude_defaults`，所以写 `validation.level='demo-validated'` / `lifecycle.status='draft'`（非默认值）**自动持久化**，默认仍省略——挣得的信号 = 天然只写增量，且"字段在场"即"被挣得"。
3. **保持精简**：不加 CI 门；P1 只做轻量 demo 校验，不引入 OS 沙箱框架。

## 1. 当前状态（main 实测）

| 面 | 现状 | 缺口 |
|---|---|---|
| result.json 契约 | `common/report.py:307 write_result_json`（envelope=`{summary,data,status}`）；**无 `RESULT_CONTRACT_KEYS` 常量** | scaffold/promoted 脚本未走 write_result_json；无契约校验器 |
| 入库门 | `runtime/policy/verification.py:156 verify_workspace_artifacts` = **只查文件存在，从不执行** | 无执行门 |
| 晋升初品 | `skill/scaffolder.py:549` 已正确设 `origin`（scaffolded/promoted）；占位脚本 `:767 status:"scaffold"` | `build_scaffold_manifest`(472) **未设 lifecycle**（默认 mvp）；占位未走 write_result_json |
| 传播 | `skill/lazy_metadata.py:225` 已 surfaced `validation_level` | **未 surfaced `origin`/`lifecycle_status`**；`scripts/generate_catalog.py:104` status 按 `has_script` 派生、且 emit 非法值 `planned` |

---

## P0 —— 契约 + provenance 底盘（独立 PR，低风险，解锁 P1）

1. **result.json 收敛规范 envelope**：`render_skill_script`(scaffolder:674)、`render_promoted_skill_script`(:821) 改走 `report.py::write_result_json`（`summary`+`data` dict）。**⚠️ MF1**：占位脚手架**保留"未实现"信号**（`status:'scaffold'` 或 `ok:false`）——它是 P1 识别空壳的唯一依据，**不得**被抹成 ok；只有真实/晋升 body 发 ok。
2. **契约校验器**（**⚠️ MF2**：`report.py` 无 `RESULT_CONTRACT_KEYS`，勿引用）：新增 `report.py::validate_result_envelope(payload)->list[str]`（断言 `summary`/`data` 为 dict、`status∈{ok,partial,failed}` + 识别 `scaffold` 哨兵）。P0 落它，P1 复用。
3. **`build_scaffold_manifest`(472) 设 `lifecycle=Lifecycle(status='draft')`**（origin 已对）——占位/晋升初品不再误标 mvp；挣得后再升。去仪式下 `draft` 非默认 → 自动写出。
4. **传播（⚠️ MF5：别漏 desktop）**：`lazy_metadata._basic_from_v2`(215) 加 `origin`/`lifecycle_status` 映射 + @property；`generate_catalog.py:104` status 改读 `lifecycle_status`（**修掉非法 `planned`**），`has_script`/`has_demo` 保留为**可用性 availability** 维度——**显式区分 availability vs lifecycle**；desktop `server.py` 同源派生处一并改。

**验收**：catalog + desktop 带 `origin` + 真实 `lifecycle_status`（与 availability 分离）；占位 result.json 仍显式"未实现"；`generate_catalog --check` 绿；**零 schema 字段新增**。
**风险**：catalog 全量 churn（status 值空间 `{mvp,planned}`→`{draft,mvp,stable}`）→ 同 PR 重生成 + 排查 desktop/前端有无消费 `planned`/`ready`。**工作量 M**。

---

## P1 —— 入库前 `--demo` 冒烟门

1. **插门**：`create_skill_scaffold` 在写好 skill.yaml/脚本(scaffolder:1438 后) 与 `shutil.move`(:1593) 之间、**仍在 `with isolated_workspace`(:1398) 内**插门；失败即 raise → staging 自动 rmtree。
2. **`_run_demo_smoke_gate(script_path, staging_tmp)`**：`subprocess [sys.executable, script, '--demo', '--output', <staging tmp>]`，env `PYTHONPATH + PYTHONNOUSERSITE=1`（对齐 runner）、**有界 timeout**；断言 `returncode==0` ∧ result.json 可解析 ∧ 过 **P0 的 `validate_result_envelope`** ∧ `status!='scaffold'`。
3. **⚠️ MF1**：占位壳发"未实现"信号 → 本就不该过门 → 保持 `draft`/`smoke-only`，但**不因过不了门被拒**（占位创建合法）。硬门只作用于**真实/晋升 body**。
4. **⚠️ MF4——"demo 校验"≠"沙箱"**：`run_skill` env 只设 `PYTHONPATH/PYTHONNOUSERSITE`，**无 OS 级隔离**。分两级：**(a) demo 校验**（轻量、base env，用于 scaffolded/自写实现）；**(b) 沙箱校验**（执行 **model-authored 晋升代码**前须走 autonomous 沙箱层）。不宣称 demo 门"无网/沙箱"。
5. **⚠️ MF3——adaptive-env 非自动**：staged raw subprocess **命不中** `resolve_skill_runtime`（只经 `run_skill`+registry）。第一版**明说在 base env 跑、缺重依赖的 demo 跳过（不置备）**；要置备须显式构造临时 skill_info 调 resolver（后续）。
6. **promoted `--demo` = skip-not-fail**：晋升脚本 `--demo` 复用原始输入(scaffolder:866)+需重依赖 → 因无关原因失败会**恰好挡住飞轮** → 缺依赖/输入时**跳过门、留 `smoke-only`、不阻断移入**。
7. **过则挣得**：`manifest.validation = Validation(level='demo-validated', evidence=[…])` 在 move 前重写 skill.yaml（去仪式自动写出）。**⚠️ SF1**：`isolated_workspace` 退出即 rmtree → evidence **不能指向 tmp**，须把 demo 命令/输出摘要拷进 `references/validation.md` 或内联 skill.yaml。
8. **`render_skill_test`(947) 升级为真 `--demo` 断言**（持久化 `tests/`，为 `fixture-validated` 铺路）。

**验收**：崩溃 skill 被拒并清暂存；真实/晋升 skill 挣 `demo-validated`；占位 draft 不因过不了门被拒；promoted 缺依赖优雅降级 `smoke-only`；evidence 落 durable 位置。
**风险**：每次 create 加 subprocess 增延时（desktop/bot 超时）→ 合理 timeout + 复用失败摘要。**工作量 M**。

---

## 验证方案
- **单元**：`validate_result_envelope`（合规/缺键/scaffold 哨兵）；`_run_demo_smoke_gate`（过/崩溃/占位三态）。
- **集成**：`create_skill_scaffold` 端到端造一个真实域 skill 走完门 + move。
- **门**：`generate_catalog --check`、`validate_skill_yaml --check`、`skill_lint --all`、`canonicalize_skill_yaml.py --check`（去仪式仍规范）。

## 交付顺序
- **PR-1（P0）**：契约校验器 + write_result_json 收敛 + `lifecycle=draft` + 传播（origin/lifecycle_status，availability vs lifecycle 分离）。
- **PR-2（P1）**：`--demo` 入库门 + `demo-validated` 挣得 + skip-not-fail + evidence 持久化。依赖 P0 的校验器。

## 明确不做
不加 skill.yaml schema 字段（填已有槽）；不做 v3 G1/G2；不加 CI 门；P1 不做 OS 沙箱（model-authored 晋升代码的沙箱留 P2 引擎）；P2 晋升泛化不在本方案。

---

*OmicsClaw 是多组学研究与教育工具，非医疗器械，不提供临床诊断。基于本方案的工程决策请经领域专家复核。*
