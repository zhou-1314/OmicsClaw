# §2 技能获取（Acquisition）落地方案 —— 飞轮引擎

> **文档角色（2026-07-13 校准）：**本文是 acquisition 的历史诊断与分期方案，
> 代码已落地 P0/P1、P2a、P3、P4、P5，并于 2026-07-13 新增 P2 的第一条
> **结构化泛化纵切**：读取 `skill_calls.jsonl` + manifest `metadata.steps`，对可证明的
> call-composition workflow 生成无 kernel facade 的 `run_skill` 脚本；任意 Python/歧义
> lineage 仍 fail-closed 回退到 sandbox-gated verbatim 路径。因此这是可验收子集，不代表
> 所有模型生成 Python 都已可泛化。
> 当前实现状态与完成判据以
> [`2026-07-13-skill-audit-system-design-assessment.md`](../reviews/2026-07-13-skill-audit-system-design-assessment.md)
> §5/§8 为准。
> 2026-07-13 首轮修复已将 skipped promotion 改入不可发现 quarantine，并禁用全局
> `promote_from_latest` 晋升入口；本文后文的旧 skip-not-fail 正式入库描述仅作历史记录。
>
> 状态：草案 v0.2（承接 [`skill-lifecycle-redesign.md`](./skill-lifecycle-redesign.md) §2；§1 表示层已在 main 落地：95/95 `skill.yaml`、0 `parameters.yaml`、schema/CI 门齐备）。
> 现状核验由一次 7-agent 并行 code-map 完成，所有行号对照本 worktree（v2、post-migration）实测，非提案原文引用。
> **v0.2 已纳入 Codex/gpt-5.5(xhigh) docs review 的 6 条 must-fix + 关键 should-fix（裁决：SOUND-WITH-FIXES）——见文末「Codex 复核修订记录」。**

---

## 0. 一句话结论与飞轮映射

**飞轮 = 用户日常分析 → 自适应决定转成 skill →（在相似请求上）复用 → 治理保鲜 → 更多分析。**

代码核验证实：飞轮的**引擎**（把一次分析变成**可复用**的 skill）目前是**逐字回放**，不是泛化——它只能在"和原始那次一模一样的数据+阈值"上重跑；且晋升出的 skill **未经任何执行校验**就被原子移入 `skills/` 并热注册。**这两点是飞轮转不起来的根因**，不是表示层。

降低工作量的既有底座（**但 Codex 提醒："底座就位"≠"工作量小"——真正缺的部分很实**）：
1. **入库结构骨架已在**：隔离暂存 `isolated_workspace`、原子 `shutil.move`、`ArtifactRequirement`/`build_completion_report` 门、`validation.level='demo-validated'`/`Origin` 枚举——**结构槽位齐全**。但真正要新建的都不小：可执行 demo 门、result 契约校验器、抽象/字面量提升、resolver 显式接线、**真沙箱**、surface/catalog 传播。故**不宜说"只差执行一步"**（原 v0.1 措辞已下调）。
2. **泛化所需的结构化原料已持久化但被忽略**：`skill_calls.jsonl`（每次 `oc.run` 的 `{skill,method,params,flags,input,output}`；`run_layout.py:64` 定义布局）+ manifest `metadata['steps']`（per-cell purpose/reasoning/new_variables）。晋升当前却去 re-regex 扁平化的 `analysis.py`（`_load_mini_agent_bundle:1067`）。
3. **adaptive-env resolver 已在 main**（#25）——但 **`resolve_skill_runtime` 只经 `run_skill` + registry `skill_info` 接线（`runner.py:253`）**；入库门跑的是**未注册的 staged 脚本**，raw `subprocess` **不会**命中 resolver。要享用它须显式构造临时 `skill_info` 调 resolver，否则第一版门只能在 base env 跑、对缺重依赖的 demo **跳过而非置备**（见 P1）。

---

## 1. 现状核验总表（A1–A7）

| # | 子面 | 现状（实测） | §2 差距 | 工作量 |
|---|---|---|---|---|
| A1 | 任务衍生脚手架 | `_render_v2_description` (`scaffolder.py:304`) 产**循环描述**"Load when 用户要创建一个新 {domain} skill…"；`build_scaffold_manifest:526-536` 硬编码同款 load/skip_when，且它才是经 `lazy_metadata._reconstruct_description` 上卡片/catalog 的**权威副本**；`--request` 只喂 `infer_skill_name`，对描述**死值**。`render_skill_script:667` 出 `status="scaffold"` 占位；`render_skill_test:940` 只断言两个文件存在。 | §2.3-(2)(5) 确认 | M |
| A2 | **经验衍生晋升（飞轮核心）** | `render_promoted_skill_script:814` 唯一变换 = `_normalize_promoted_code:1215` 一次 `str.replace(source_dir→AUTONOMOUS_OUTPUT_DIR)`；cells 逐字 `textwrap.indent` 进 `main()`；mini_agent 引擎前置 `_MINI_AGENT_FACADE_BOOTSTRAP:789` 重建 `oc/adata/show/ReturnAnswer`；`--method/--species` **死 flag**，阈值/基因表/`oc.run` kwargs **全硬编码**。 | §2.3-(3) 四项全确认 | L |
| A3 | 运行时自著闭环 | `create_omics_skill`(agent.py:1187)+`autonomous_analysis_execute`(:1266)→`create_skill_scaffold`→原子移入→`refresh_registry` 热刷。**"是否转 skill" 100% 靠 LLM 对用户关键词判断**（`should_create_skill` = query 关键词回显，非成功/重复信号）；`execute_autonomous_analysis_execute:2263` **不做 lineage 捕获**（`execute_omicsclaw` 会）。 | §2.3-(4) 确认；无自适应信号 | L |
| A4 | 入库前治理门 | `create_skill_scaffold:1316` 全程隔离暂存+原子移入，但门（`verify_workspace_artifacts:156`）**只查文件存在**；**从不执行**新 skill。`validation.level` 恒 `smoke-only`。scaffold/promoted 的 `result.json` **未用** `write_result_json`（`summary` 是 str、无 `data`），与规范契约不符。 | §2.3-(5) 确认 | M |
| A5 | catalog 来源/成熟度 | schema 已全建模（Provenance/Lifecycle/Validation）；write 侧 ~60%（`build_scaffold_manifest:555` 已盖 origin，但不设 lifecycle.status、migrate 误标 human 非 migrated、level 从不挣得）；**read/surface 侧 0%**：`lazy_metadata._basic_from_v2` 丢弃 origin/status，`generate_catalog.py:104` 用 `has_script` 假造 status（还 emit 非法值 `planned`），**无 origin 字段**。 | §2.3-(6) 确认 | L |
| A6 | 自治分析原料 | "accepted cell" = `mini_agent.py:237` 里 `cell.ok` 的**原始 Python 源码串**；依赖 kernel facade（`oc`/`adata`/`show`/`ReturnAnswer`，`build_init_code:270`）。**结构化原料已持久化但晋升未用**：`skill_calls.jsonl`（skill+params）、manifest `steps[]`。 | 为 A2 泛化提供底座 | L |
| A7 | 语料衍生（paper→skill） | `literature/core/extractor.py` 只抽 GEO/organism/tissue/technology 元数据；**零方法学抽取、零 `--from-paper` 路径**。schema 有 Provenance/Validation/`hints` 空位可落。 | §2.3-(1) 确认；greenfield | L |

---

## 2. 落地相位

> 依赖铁律（A2/A4 风险给出）：**泛化（P2）不能脱离执行门（P1）单独上**——无门的泛化会静默回归；无契约对齐（P0）的门会误判。故 **P0→P1+P2 是飞轮核心、须成套交付**；P3/P4/P5 为后续。

### P0 —— 契约 + provenance 底盘（前置，解锁一切）
**目标**：让"门能断言的契约"和"来源/成熟度可见性"就位。无新概念、无 schema 改动。
**改动面**：
- **result.json 契约收敛（envelope 形状，非"标 ok"）**：`scaffolder.py:render_skill_script`(`:758-767`)/`render_promoted_skill_script`(`:913-930`) 改用 `omicsclaw/common/report.py::write_result_json`（规范 `summary`+`data` dict）统一 envelope。**⚠️ Codex MF1**：占位脚手架**不得**被 `mark_result_status('ok')`——它当前用 `status:"scaffold"` 作为"未实现"的**唯一信号**，若 P0 把它抹成 ok，P1 的门就失去了识别空壳的依据。故占位脚本保留一个"未实现"信号（`ok:false` 或 `status:'scaffold'`），只有**真实/晋升 body** 才发 ok。
- **契约校验器**（**Codex MF2**：`report.py` **没有** `RESULT_CONTRACT_KEYS` 常量，勿引用）：新增一个真实的共享校验器/常量（如 `report.py::validate_result_envelope()` 或 `RESULT_CONTRACT_KEYS` 集合），或在 P1 门里**内联**校验 envelope 键/类型。P0 先落这个校验器，P1 复用。
- `scaffolder.py:build_scaffold_manifest`(`:555`) 设 `lifecycle=Lifecycle(status='draft')`，让 TODO 壳/晋升初品不再被误标 `mvp`（origin 已正确）。
- **surface 传播（Codex MF5——原 v0.1 漏了 desktop）**：`lazy_metadata.py:_basic_from_v2`(~:195-220) 加 `origin`/`lifecycle_status` 映射 + @property + v1 fallback 默认；`generate_catalog.py:104` 的 `status='mvp' if has_script else 'planned'` 改读 `lazy.lifecycle_status`（保留 has_script/has_tests 作**能力/可用性**位）；**`omicsclaw/surfaces/desktop/server.py:2729`** 同样按脚本存在派生 `ready/planned`——须一并改为读 lifecycle，并**显式区分「可用性 availability」与「生命周期 lifecycle」两个维度**（别混为一谈）；`registry.py:254` 可选透传 origin/status 给运行时 skill_info。
- `migrate_to_skill_yaml.py:436-438`：**建议保留 `origin='human'`**（它们确是人写 v1，"migrated" 仅描述搬运），改在 catalog 另加 `migrated_from` 标记，而非批量改 origin。
**验收**：catalog + **desktop DTO** 带 `origin` + 真实 `lifecycle_status`（availability 与 lifecycle 分离）；`generate_catalog --check` 绿；占位脚手架 `result.json` 仍显式标"未实现"（不被误判 ok）。
**风险**：catalog 全量 churn（95 条加 origin、status 值空间 {mvp,planned}→{draft,mvp,stable,deprecated}）——同 PR 重生成 + 排查 desktop(`server.py:2729`)/registry/前端 是否有消费 `planned`/`mvp`/`ready` 的地方。**工作量：M（Codex：因 desktop DTO 一并改，略偏 M 上限）**

### P1 —— 入库前 `--demo` 冒烟门（质量刹车，与 P2 成套）
**目标**：新 skill 在原子移入前必须 `--demo` 跑通并产出合规 `result.json`，通过才移入并挣 `demo-validated`。
**改动面**：
- `scaffolder.py:create_skill_scaffold`：在 `build_completion_report`(~:1544) 与 `shutil.move`(:1586) 之间、**仍在 `with isolated_workspace` 内**插入冒烟门（失败即 raise，暂存自动 rmtree，复用 :1556 的 RuntimeError 路径）。
- 新 helper `_run_demo_smoke_gate(script_path, staging_root)`：`subprocess [sys.executable, script, '--demo', '--output', <staging 内 tmp>]`，env `PYTHONPATH=OMICSCLAW_DIR + PYTHONNOUSERSITE=1`（对齐 `runner.py:245`）、**有界 timeout**；断言 `returncode==0` ∧ `result.json` 可解析 ∧ 过 **P0 的契约校验器**（非 `RESULT_CONTRACT_KEYS` 幻觉常量）∧ **`status != 'scaffold'` / `ok==true`**（堵占位壳假通过）。
- **⚠️ Codex MF1——门只对"真实 body"有意义**：占位脚手架（P3 前）发"未实现"信号，本就**不该**过门→保持 `draft`/`smoke-only`，**不因门失败而拒绝创建**（占位创建是合法动作）。硬 demo 门只作用于**晋升/真实实现**；空壳过不了 demo-validated 但仍可作为 draft 落地。
- **⚠️ Codex MF4——"demo 校验" ≠ "沙箱校验"，勿夸大隔离**：`run_skill` 的 env 只设 `PYTHONPATH/PYTHONNOUSERSITE`，**不提供 OS 级网络/文件系统隔离**；真正的强隔离在 autonomous 侧、且仅当 `OMICSCLAW_AUTONOMOUS_REQUIRE_SANDBOX` 时启用。故分两级：**(a) demo 校验**（轻量、base env、验"能跑+契约"，用于 scaffolded/自写实现）；**(b) 沙箱校验**（执行 **model-authored 晋升代码**这种不可信来源前，须走 autonomous 的沙箱层再当作可信）。文档不再宣称 demo 门"无网/沙箱"。
- **⚠️ Codex MF3——adaptive-env 需显式接线**：staged 脚本用 raw subprocess 跑**命不中** `resolve_skill_runtime`（它只经 `run_skill`+registry `skill_info`）。要么构造临时 `skill_info` 显式调 resolver，要么**明说第一版门在 base env 跑、缺重依赖的 demo 跳过（不置备）**。
- **promoted 路径 skip-not-fail**：晋升脚本 `--demo` 常需重依赖(scanpy…)+原始 `--input`；因无关原因失败会**恰好挡住飞轮**→ 缺依赖/输入时**跳过门、留 `smoke-only`、不阻断移入**。
- 通过则 `manifest.validation = Validation(level='demo-validated', evidence=[…])` 在 move 前重写 `skill.yaml`。**⚠️ Codex SF1——证据要持久**：`isolated_workspace` 退出即 `rmtree`（`verification.py:367`），故 evidence **不能指向 tmp 路径**——须把 demo 命令/关键输出摘要拷进 `references/validation.md` 或直接摘要进 `skill.yaml`。`SKILL.md` 无需重渲（已核）。
- `render_skill_test:940` 升级为真 `--demo` 断言（持久化到 `tests/`，为 `fixture-validated` 铺路）。
**验收**：崩溃 skill **被拒**并清暂存；真实/晋升 skill 挣 `demo-validated`；占位 draft **不因门失败被拒**；promoted 缺依赖优雅降级 `smoke-only`；evidence 落在 durable 位置。
**风险**：每次 create 加 subprocess 增延时（desktop/bot 超时）——合理 timeout + 复用 `format_completion_summary` 失败消息。**工作量：M**

### P2 —— 经验衍生泛化（飞轮引擎，与 P1 同批）
**目标**：把晋升从"逐字回放"变成"参数化、去 facade、可在相似数据上复用"的独立 skill。
**改动面**：
- **消费结构化原料**（而非 re-regex 扁平 `analysis.py`）：`scaffolder.py:_load_mini_agent_bundle:1067` / `_extract_accepted_cells:1143` 扩展为同时读 manifest `metadata['steps']` + `skill_calls.jsonl`；`AutonomousAnalysisBundle:205` 增字段承载 steps + 组合轨迹。
- **抽象 pass**：`_normalize_promoted_code:1215` → 换成 `_abstract_promoted_code(bundle, *, llm=None) -> AbstractedSkill(rewritten_body, [LiftedParam(name,flag,default,type,help)])`。AST 扫描 cells 里的字面量（路径、基因表、阈值、`oc.run` kwargs）→ 提为 flag；`llm` 提供时做一步"泛化此轨迹"，**不提供时退化为确定性 AST literal-lift**（保留纯 AST fallback）。
- `render_promoted_skill_script:814` 消费 `AbstractedSkill`：**动态**构建 `parse_args`（替换 `:855-862` 死 `--method/--species` 块）；注入 facade-free body（替换 `:884` 的 `{facade_bootstrap}{indented_code}`）；`result.json` 由 body 真实产出派生（走 P0 的 `write_result_json`）。
- **⚠️ Codex MF6——退役 facade 不是机械清理**：`_MINI_AGENT_FACADE_BOOTSTRAP:789` 里的 `oc.run(...)` 承载**真实嵌套语义**——物化输入、调 `run_skill`、reload AnnData、append `skill_calls.jsonl`（`skill_facade.py:91`/`:254`），**没有等价的 in-process skill API**。所以抽象 pass 须**先把 `oc.run('<skill>', …)` 翻译成显式的 `run_skill(...)` 调用**（保留 provenance/输出/reload 行为），再把 `show()`/`ReturnAnswer()` 译为显式图/答案写出——而非"删掉 facade"。**注意**：现有 `tests/test_skill_scaffolder.py:253` **断言晋升脚本里保留 facade 调用**，本改动须同步更新该测试。
- **接线 LLM seam**：`agent_executors.py:execute_create_omics_skill:1968` 把 agent loop 的 LLM handle 透传进 `create_skill_scaffold`（LLM 已在此作用域）。
**铁律（A2 风险，必须）**：抽象是 LLM/AST 改写，**可能静默改坏本来能跑的代码**——**抽象后必须重跑 P1 的 `--demo` 门 + 重 lint**；任一失败**回退到 verbatim**（保留为确定性 fallback）。持久化生成源 + abstraction 记录（可复现、非确定性可审计）。
**验收**：一个 promoted skill 的字面阈值/基因表被提为 flag；不带 facade 独立运行；改 `--input`/参数能在相似数据复用；抽象失败自动回退 verbatim 且仍过门。
**风险**：跨 cell 数据流（naive per-cell lift 会把字面量提出定义域）；facade 误译丢行为；LLM 非确定性——全部由"AST fallback + 抽象后重跑门 + 重 lint"兜底。**工作量：L**

### P3 —— 任务衍生真正合成正文
**目标**：脚手架描述从循环模板变成从 `--request` 合成的真实能力条款。
**改动面**：两个调用方（`agent_executors.py:1978`、`omics_skill_builder.py:95`）**已传 `request`+`summary`**，只是在 `build_scaffold_manifest` 前被丢。→ `_render_v2_description` / `build_scaffold_manifest`（`:526-536`）/`render_skill_yaml:559`/`create_skill_scaffold:1424` 全部**透传 request+summary**，用它合成 `load_when`（"当用户需要 <request 归一化> 时"）+ 指向既有 {domain} skill 的 `skip_when`。
**验收**：新脚手架 skill 的 catalog/卡片描述反映真实意图，不再循环。
**风险**：自由文本 → load_when 需归一化/裁剪（`_reconstruct_description` 逐字渲染）；`check_description_drift`/`generate_catalog` 需重生成（低 blast，循环串未被任何测试 pin）。**工作量：S–M**

### P4 —— 自适应晋升信号（让"自适应决定"名副其实）
**目标**：把"是否转 skill"从纯用户关键词，升级为由**成功/重复轨迹**驱动的建议。
**改动面**：
- `agent_executors.py:execute_autonomous_analysis_execute:2263` 加 lineage 捕获（镜像 `execute_omicsclaw` 的 `_auto_capture_analysis:489/570`），给 ledger 造数据。
- 新 `omicsclaw/skill/promotion_signal.py`（或扩 capability_resolver）：从 lineage ledger 算晋升建议（N 次相似成功 / 重复 goal），替代纯关键词 `should_create_skill`（`capability_resolver.py:339/687/747/793`）；在 autonomous digest 里 append "promotion candidate" 提示。
- **⚠️ Codex SF3——建议须带显式 run 身份**：晋升建议/`promote_from_latest` 不要靠 `find_latest_autonomous_analysis:1275` 的 mtime 猜；让 autonomous 执行结果**回传显式 workspace/run/thread id**，建议直接锚定那次 run（并发会话安全）。
**验收**：同一分析成功 N 次后系统主动提示"是否沉淀为 skill"，而非只靠用户说"创建 skill"；晋升锚定确定的 run 而非"最近一个"。
**风险**：`find_latest_autonomous_analysis:1275` 按 mtime 全局选——auto-promote 在并发会话会选错 run，**须按 thread/session_id 定界**（request 已带）。**工作量：M–L**

### P5 —— 语料衍生（paper/tool-docs → skill）｜独立高价值轨
**目标**：从论文/工具文档抽方法学 → 预填 skill 骨架，铁律"不幻觉"入 schema/lint。
**改动面**（greenfield，建议单开子方案）：
- `literature/core/extractor.py` 加 `extract_methodology(text)`：候选 method/param/阈值/基因关联，**每个带 `source_ref={quote,char_span}`；无 span 只出 `{value:None, todo:True}`，绝不编造**。
- `schema.py`：`Origin` 加 `'corpus'`；`Provenance.source_ref`（doc 级 DOI/URL/PMID）；`Parameters.hints` 里约定**保留键**承载 per-param `source_ref`——**带默认值的 hint 必须带 source_ref**，加 lint 校验（否则退化为约定）。
- `scaffolder.py` 加 `from_paper`/`from_tool_docs` + `CorpusDerivedBundle` + `render_corpus_skill_script`（sourced 默认旁注 `# source_ref:`，unsourced 显式 TODO）；`omics_skill_builder.py` 加 `--from-paper`/`--from-tool-docs`。
**验收**：paper→skill 骨架，所有阈值/关联要么带来源、要么 TODO；无来源不成为运行默认值。
**风险**：PDF 抽取低精度（阈值常在表/图）——铁律即缓解（宁 TODO 勿猜）；LLM 抽取须返回 span 非转述，renderer 拒绝无 span 的值。**工作量：L**

---

## 3. 关键设计决策（map 揭示的必答项）

1. **占位壳"假通过"陷阱 + P0 别抹信号（MF1）**：占位脚本 `--demo` 会干净跑完并写 `result.json{status:'scaffold'}`——这是识别空壳的**唯一信号**。P0 收敛 envelope 时**必须保留**该"未实现"信号（占位不发 `ok`）；P1 门 `status!='scaffold'`/断言真实 `data` 才认 `demo-validated`。占位仍可作 `draft` 落地，不因过不了门被拒。
2. **契约先对齐再断言，且校验器要真实存在（MF2）**（P0↔P1 耦合）：现模板不走 `write_result_json`（`report.py:307`），P0 先统一 envelope；断言用**新建的真实校验器/常量**（`report.py` 当前**无** `RESULT_CONTRACT_KEYS`）或内联校验，勿引用不存在的符号。
3. **promoted `--demo` = skip-not-fail；adaptive-env 非自动（MF3）**：晋升脚本因重依赖/原始输入缺失失败 ≠ 不正确 → 降级 `smoke-only` 不阻断。想用 adaptive-env 置备依赖，须**显式构造临时 `skill_info` 调 `resolve_skill_runtime`**（raw subprocess 命不中它）；否则第一版门明说在 base env 跑、缺依赖跳过。
4. **"demo 校验"≠"沙箱校验"，勿夸大隔离（MF4）**：`run_skill` 只设 `PYTHONPATH/PYTHONNOUSERSITE`，**无 OS 级网络/文件系统隔离**；真沙箱在 autonomous 侧且仅 `OMICSCLAW_AUTONOMOUS_REQUIRE_SANDBOX` 时启。→ scaffolded/自写实现走轻量 demo 校验即可；**执行 model-authored 晋升代码前须走 autonomous 沙箱层**再当可信。不用 `run_skill()`（未注册），按路径调 staged 脚本；tmp 落 `staging_root` 随 `isolated_workspace` 清理（故 evidence 要另存 durable 位置）。
5. **泛化 = 非确定性 → 必带 fallback**：LLM 抽象后必重跑门 + 重 lint，失败回退 verbatim；持久化生成源。
6. **schema 支撑 P0–P2 几乎零改动**（Codex 确认枚举全在 `schema.py:71`）：`demo-validated`/`Origin`/`Lifecycle`/`Validation` 已定义；**但"零改动"仅指 schema**——真正工作量在门/校验器/抽象/resolver 接线/沙箱/surface 传播（见 §0 已下调的"底座"措辞）。P5 需**真正的 schema/design pass**（`Origin+='corpus'` + per-param `source_ref`，且要 lint 强制）。

---

## 4. 建议交付顺序

```
P0 契约+provenance 底盘        [M]  ← 前置，单独可 PR
P1 --demo 入库门 + P2 泛化      [M+L] ← 飞轮核心，成套一个 PR（互相依赖）
P3 任务衍生合成正文            [S-M] ← 独立小 PR
P4 自适应晋升信号             [M-L] ← 让飞轮自主，独立
P5 语料衍生                   [L]  ← 独立高价值轨，单开子方案
```

**第一刀建议**：P0 单独落地（低风险、解锁后续），再 P1+P2 成套。P0 完成后飞轮就有了"契约 + 可见性"，P1+P2 完成后飞轮真正开始转（可复用 + 有质量刹车）。

### 4.1 2026-07-13 P2 结构化泛化落地记录

- `_load_mini_agent_bundle` 读取 append-only `skill_calls.jsonl`（优先）及 manifest
  `metadata.steps`，损坏/不一致以 warning 留证而不是静默猜测。
- `build_acquisition_abstraction` 仅接受能够由 AST 证明输入 lineage 的顶层 `oc.run`
  组合；控制流、任意后处理、动态 skill 名或失败 call 均不进入结构化路径。
- 生成脚本直接调用共享 `omicsclaw.skill.runner.run_skill`，参数来自实际 call trace，
  上游 `.h5ad` 通过显式 `step:N` 关系传递，不再注入 `oc/adata/show/ReturnAnswer`。
- `references/acquisition_abstraction.json` 持久化 source hash、原始 calls/steps、参数绑定、
  lineage、是否 applied 与 fallback reason；结构化脚本仍须通过同一 sandbox/demo gate，
  gate 拒绝时重试 verbatim 并更新依赖/参数派生物。
- ACQ-06 由一个生成后的脚本在 **2 inputs × 2 parameter sets** 上执行四次验证；每次
  input、flag、最终 artifact 和 `result.json` 均独立核对。
- 线性组合另有独立端到端验证：第二个 `oc.run` 的输入必须解析为第一个 call 生成的
  `step:1` `.h5ad`，并验证两步参数、嵌套输出和最终 artifact 内容，而非只检查 JSON。

未覆盖边界：任意 Python 科学后处理的语义抽象、复杂分支 workflow、非 `.h5ad` 跨步
artifact 映射，以及 run/thread acquisition event identity；这些仍是后续 P2 工作。

---

## 5. Codex 复核修订记录（v0.2）

Codex/gpt-5.5(xhigh) 对本方案做了 read-only、逐条对照真实代码的 docs review。**裁决：SOUND-WITH-FIXES**。已纳入的修订：

**Must-fix（6，均已应用）**
1. **P0/P1 自相矛盾**：P0 若把占位脚本 `mark_result_status('ok')`，会抹掉 P1 门赖以识别空壳的 `status:'scaffold'` 信号 → P0 保留"未实现"信号，硬门只作用于真实/晋升 body（§P0、§3.1）。
2. **`RESULT_CONTRACT_KEYS` 是幻觉**：`report.py` 只有 `write_result_json`/`mark_result_status`/`read_result_status` → 改为"新建真实校验器/常量或内联校验"（§P0、§P1、§3.2）。
3. **门命不中 adaptive-env**：`resolve_skill_runtime` 只经 `run_skill`+registry；staged raw subprocess 不触发 → 需显式构造 `skill_info` 或明说 base-env 跑（§P1、§3.3）。
4. **沙箱声明过强**：`run_skill` 无 OS 级网络/文件隔离；真沙箱仅 autonomous 侧 `OMICSCLAW_AUTONOMOUS_REQUIRE_SANDBOX` → 分"demo 校验"与"沙箱校验"，model-authored 晋升代码须先过沙箱（§P1、§3.4）。
5. **P0 surface 传播漏了 desktop**：`server.py:2729` 也按脚本存在派生 `ready/planned` → 一并改，且区分 availability vs lifecycle（§P0）。
6. **"退役 facade"非机械**：`oc.run` 承载真实嵌套语义（物化输入/`run_skill`/reload/`skill_calls.jsonl`），无 in-process 等价 → 先翻译为显式 `run_skill` 调用；且 `test_skill_scaffolder.py:253` 断言 facade 仍在，须同步改（§P2）。

**Should-fix（已应用）**：demo 证据要落 durable 位置（`isolated_workspace` 退出即删，§P1）；autonomous 与 skill 的 `result.json` 形状分开勿混用；晋升建议带显式 run 身份而非 mtime（§P4）；下调"~90% 底座就位"措辞（§0）；P5 需真正 schema/design pass（§3.6）。

**Codex 确认为可靠的承重判断**（无需改）：v2 表示已落地（95 skill.yaml / 0 parameters.yaml / validate + catalog --check 绿）；当前入库门仅查文件存在（`verification.py:156`）；晋升近乎逐字（`_normalize_promoted_code:1215` 仅替换 source_dir）；结构化原料 `skill_calls.jsonl`（`run_layout.py:64`）+ manifest `steps` 确实存在但 `_load_mini_agent_bundle:1067` 未用；`should_create_skill` 确为关键词判断、P4 是真缺层。

---

*OmicsClaw 是多组学研究与教育工具，非医疗器械，不提供临床诊断。基于本方案的工程决策请经领域专家复核。*
