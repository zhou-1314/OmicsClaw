# 技能表示（skill.yaml v2）对"飞轮"的就绪度评估

> 评估对象：`omicsclaw/skill/schema.py`（ADR 0037 的 `SkillManifest`）作为**飞轮式交互框架的基础组件**是否够通用/系统。
> 参照：[`skill-acquisition-plan.md`](../proposals/skill-acquisition-plan.md) 的飞轮映射（用户分析 → 自适应转 skill → 复用相似分析 → 治理保鲜）。
> 所有判断对照本 worktree 的 `schema.py` 实测行号 + 真实 skill.yaml 样本，非记忆。
> **已经 Codex/gpt-5.5(xhigh) read-only 交叉验证：裁决 AGREE-WITH-CORRECTIONS——G1–G4 全部 file:line 确认，另补 3 个更根本缺口（G5–G7）并校正优先级；见 §7。**

---

## 0. 结论（TL;DR）

**skill.yaml v2 是一份"优秀的、被严格校验的**声明式契约**——用于给一批相互独立的分析脚本做编目 / 路由 / 治理 / 依赖置备。作为**静态技能目录**的基础组件，它很强、应原样保留。**

**但它是一份"迁移形态（migration-shaped）"的表示——从 95 个已有脚本逆向而来，忠实建模了"一个手写分析脚本需要什么"，却系统性地**欠建模**"一个自增长、可组合的技能生态需要什么"。** 对飞轮而言，有 **2 个承重结构缺口**（G1 组合/技能图、G2 参数无类型）+ 2 个次要缺口（G3 I/O 类型的 AnnData 偏置、G4 封闭词表/无扩展命名空间/无接口兼容）。**飞轮 P1 之后的每一个相位都会撞上 G1/G2。**

**建议**：在把获取引擎（acquisition，P2+）做硬之前，先做一次 **v3「飞轮就绪」设计 pass**（加法式、经保留命名空间与可选块保持向后兼容）——**先定形状，再增量实现**，否则 P2/P5 会与表示打架、把不一致复制进主库。

---

## 1. 当前表示的本质与承重优点（先明确"不要动什么"）

`SkillManifest`（`schema.py:293`）是一个 `extra='forbid'` 的严格闭合契约，顶层 19 块。它把一个 skill 建模为 **`(身份, C 适用条件, I/O 契约, 单入口执行, 安装通道, 兼容性, 资源, 治理三件套, 安全, MCP)`**。承重优点（飞轮同样依赖，务必保留）：

- **C（适用条件）是一等公民且机器可解析**：`Summary.load_when/skip_when[]/trigger_keywords`（`:104-109`），`SkipRule.use` 还能指向"改用哪个 skill"。这是飞轮"复用/检索"半程的强项，业界罕见。
- **单一真源 + 单向生成**：skill.yaml → catalog/SKILL.md/parameters.md/路由表，全 `--check` 门控，零漂移。
- **治理槽位齐备**：`Lifecycle/Validation/Provenance`（`:262-277`）——`origin∈{human,scaffolded,promoted,migrated}`、`validation.level` 五档、`superseded_by`。**结构已就绪**（缺的是把信号跑活的 plumbing，见 acquisition plan P0/P1/P4，属实现非表示）。
- **安全铁律 schema 化**：`Security{data_egress,network,writes}`（`:279-284`）——对"接纳 model-authored 技能"的飞轮尤其关键。
- **`schema_version`**：格式本身可演进（v2→v3 的逃生口，本评估的落点）。

> 换言之：**表示的"骨架 + 路由 + 治理 + 安全"层面已达生产级，且是飞轮的资产，不应重写。缺口都在"组合"和"参数"两个加法维度。**

---

## 2. 飞轮对"基础组件"的 5 个硬需求 → 满足度

| # | 飞轮对表示的硬需求 | 满足？ | 依据 |
|---|---|---|---|
| **D1 可生成** | 获取引擎能*产出*一份合法 skill.yaml（从分析/论文） | ✅ 大体满足 | 严格 schema + scaffolder 生成器已在；**但产出质量受 G2 拖累**（P2 提升的参数、P5 的 source_ref 无结构化落点） |
| **D2 可组合** | skill 是可*链接/组合*的组件；框架与检索需知道 skill→skill 边 + 类型化 I/O 流 | ❌ **未满足** | **G1**：无组合字段；连 consensus 的成员映射都在 runtime |
| **D3 可复用/可检索** | "复用相似分析"需机器可比的能力签名 + "and-then" 组合查询 | ⚠️ 部分 | 路由（C）强；但**链式/相似度**依赖 G1 的技能图与能力签名 |
| **D4 可治理** | 挣得的 validation/lifecycle/provenance 让自增长库保鲜 | ✅ 表示就绪 | 槽位全在；plumbing 待做（非表示问题） |
| **D5 可扩展** | 生态增长需能加参数类型/输出类型/来源/成熟度轴而**不必每次改 schema** | ❌ 未满足 | **G4**：全封闭 Literal + `extra=forbid` + 无保留命名空间 |

---

## 3. 差距分析（对照 schema.py）

### G1 —— 无组合 / 技能图 / 可组合能力签名 ★承重
飞轮的本质是"技能作为基础组件相互组合、复用"。但**声明式表示里完全没有 skill→skill 的组合边**：
- 全 schema 仅两处引用别的 skill：`SkipRule.use`（`:100`，路由"改用 X"）与 `Lifecycle.superseded_by`（`:264`，弃用重定向）——**都是路由/治理边，均非组合边**。
- `type` 含 `workflow`，但注释（`:66-68`）明说它是**为未来组合类型保留、当前无任何 skill 使用、且无 steps/DAG schema**。
- 连**已存在的组合类**`consensus`，其成员技能映射也在 **runtime**（`runtime/consensus/source_registry.py::CONSENSUS_SOURCES`）而非 skill.yaml——`consensus-domains/skill.yaml` 里查无成员声明。→ **组合被系统性地挡在表示层之外**。
- 更深一层：schema *记录了* I/O 形状（`inputs.preconditions.data_shape.obsm` 与 `outputs.anndata.obsm`），却**没把它组织成可组合的接口类型**——无法机械地问"什么 skill 能接在 A 之后（A 的 output obsm ⊇ B 的 input 需求）"。飞轮"复用/链式"所需的**能力签名（typed inputs → typed outputs）**不存在。

**为何对飞轮致命**：P2 晋升的其实是*组合*（promoted 脚本经 facade 调 sc-clustering/sc-de… 见 acquisition plan §P2 + Codex MF6）；若表示不记录这条组合，晋升只能把子技能**内联为不可见的黑盒**，无法作为"组合"被治理、被复用、被 diff。§3.4 的"and-then 复合查询 / 技能 DAG"、P4 的"相似分析"匹配，也都要这张图。

**Codex 校正（更强而非更弱）——"组合脑裂 split-brain"**：组合并非在**整个 repo** 缺失，而是**散落在三处、无一是 canonical 表示**：(a) `SkillManifest` 无组合边；(b) `pipelines/spatial-pipeline.yaml` 有 `steps`（`pipeline_runner.py:107` 消费，但**靠文件名接力**串联、非产物类型兼容，`pipeline_runner.py:103`）；(c) consensus 成员在 `runtime/consensus/sources.py:38`（`member_skill="spatial-domains"`）。→ 获取引擎将被迫**同时协调这三种表示**。这比"完全缺失"更棘手：飞轮既要*引入* canonical 组合表示，又要把既有 pipeline/consensus 映射*迁移或引用*进来。

### G2 —— 参数无类型（free-form `hints`）★承重
`Parameters`（`:139-156`）= `allowed_extra_flags: list[str]`（**仅 flag 名**）+ `hints: dict`（**完全自由的嵌套 dict**）。实测 `sc-clustering`：默认值 `{cluster_method: leiden, resolution: 1.0, n_neighbors: 15}` 藏在 `hints.<method>.defaults` 里，**无类型、无范围、无 choices、无 source_ref，每个 skill 自创 hints 形状**。
- 飞轮**最需要操纵的正是参数**：P2 的核心动作 = 把字面量提升为参数（提升成什么结构？无处安放）；P5 的铁律 = 每个默认值带 `source_ref`（`hints` 无类型化位置，acquisition plan 自己也标了这条）；
- **MCP 变薄**：`Mcp.input_schema_strategy` 只是个策略名字符串（`:290`），**无法从 free-form hints 自动派生真实的 JSON tool schema**——"skill 作为可导出工具"的野心被无类型参数拖住。

**为何对飞轮致命**：参数是飞轮的"可操纵单元"与 MCP/自动链接的底料，却是整个契约里**最不结构化**的部分。

### G3 —— I/O 类型的 AnnData 偏置（跨域不对称）

> **2026-07-14 状态更新：部分关闭。** `interface.inputs/outputs.artifacts` 已提供 kind + format +
> 真实相对路径的通用产物契约，并为 genomics、proteomics、metabolomics、Bulk RNA 建立代表性
> handoff；这里保留的是原始诊断，长尾格式/schema 仍需增量覆盖。

`Outputs`（`:172-175`）给了一个**类型化**的 `anndata{obs/obsm/var/layers/uns}`，其余一律 `files: list[str]`（无类型）；`DataShape`（`:117-125`）更是把 `obs/obsm` 作命名字段、并**唯一地** `extra='allow'`（`:121`，全 schema 唯一放松严格性处）——这是为容纳非 AnnData 形状开的"泄压阀"，本身即偏置的自证。实测：spatial `anndata=YES`，genomics/proteomics `anndata=None` 只有 4 个无类型 files。
→ OmicsClaw 号称 7 域，但 genomics(VCF/BAM)、proteomics/metabolomics(表/mzML)、bulkrna 的 skill **机器契约明显更薄**。跨域飞轮会系统性地厚此薄彼。

### G4 —— 封闭词表 + 无扩展命名空间 + 无接口兼容
- `Origin/ValidationLevel/LifecycleStatus/SkillType` 全是**封闭 `Literal`**：加 P5 的 `corpus` 来源、或任何新成熟度轴，都要**改 schema + 迁移**。
- `extra='forbid'` + **无保留扩展命名空间**（无 `x-`/`ext:`）：生态想加实验字段只能改核心 schema。对**自增长**系统，这意味着每次扩展都是一次侵入式改动。
- `version: str` 是自由串，**无接口 semver / 无破坏性变更检测**——一旦 skill 相互组合（G1 补齐后），上游 skill 改接口会静默打断下游。

### G5 —— 能力与实现耦合（Codex 补漏，我原稿低估）
`SkillManifest`（`:293`）把 summary / interface / runtime-entry / deps / security / provenance / lifecycle **全塞进一个对象**。飞轮很可能需要**把"能力契约"与"某一份实现"解耦**：同一能力可有 R/Python 双实现、可由 promoted 组合实现、可只是纯知识（`knowledge` 空槽）。当前"一个 skill = 一份 skill.yaml = 一个入口脚本"的耦合，会让"同能力多实现 / 能力级检索"难做。

### G6 —— 单入口脚本假设（Codex 补漏）
`Runtime.entry: str`（`:185`）+ 协议假定 `main()`（`protocol.py:33`）——**单入口**。而飞轮的输入恰恰是 **promoted notebook / 组合 workflow / adapter / 多入口工具**；单入口假设对这些形态偏窄。

### G7 —— 契约无错误分类（Codex 补漏，对 P4 关键）

> **2026-07-14 状态更新：地基已关闭。** `SkillRunResult.error_kind`、统一分类器与 privacy-minimal
> run ledger 已落地；legacy result dict 保持兼容。proposal 自动改写与晋升策略仍是后续治理层工作。

`SkillRunResult`（`result.py:11`）只有 success/exit/stderr/files，**无 typed `error_kind`**；子进程状态只有 `ok/partial/failed`（`subprocess_driver.py:172`）。→ 飞轮 P4"重复成功→晋升 / 反复失败→蒸馏 Gotcha"需要**结构化错误分类来打分/学习**，当前契约给不出。这是表示层缺口（不只是 §4 治理的 plumbing）。

> 附：`lazy_metadata.py:27` 仍列 `knowledge/adapter` 而 `SkillManifest` 只允许 `leaf/workflow/consensus`——遗留词表不一致（迁移收尾项）。

---

## 4. 与飞轮各相位的耦合（差距不是抽象的）

| 相位 | 撞上的缺口 |
|---|---|
| P0 契约/provenance | 无（表示就绪，纯 plumbing） |
| P1 `--demo` 入库门 | 无（`validation` 槽已在） |
| **P2 晋升泛化** | **G2**（参数提升无落点）+ **G1**（组合被内联为黑盒，Codex MF6 的 `oc.run→run_skill` 翻译若不记录组合边就丢失可治理性） |
| **P3 任务衍生正文** | G2（合成的参数无类型化处） |
| **P4 自适应晋升信号** | **G1**（"相似分析"匹配需能力签名/技能图） |
| **P5 语料衍生** | **G2**（per-param source_ref）+ G4（`origin+='corpus'` 要改 schema） |
| §3.4 检索链式复用 | **G1**（技能 DAG） |

→ **P0/P1 可在现表示上直接做；但飞轮的引擎（P2+）与复用（检索链式）被 G1/G2 卡住。** 这就是"够不够通用"的精确答案：**够做静态目录 + 治理门，不够做可组合、自增长的飞轮。**

---

## 5. 建议：v3「飞轮就绪」设计 pass（加法、先设计后实现）

**原则**：全部加法式、经"可选块 + 保留命名空间"保持 v2 向后兼容；`schema_version` 升 3 但 v2 文件仍可读。**先把形状定下来（在 P2 做硬之前），再增量实现**——否则获取引擎会产出与表示不一致的东西。

**Must-decide（P2 前必须定形，否则引擎画进死角）**
1. **G2 类型化参数模型**：`interface.parameters.params: [{name, type, default, range/choices, required, source_ref?, description}]`（保留 `hints` 作叙事补充）。一举解锁 P2 参数提升、P5 source_ref 铁律、MCP 真 input_schema。
2. **G1 组合 / 技能图 + 能力签名**：给 `workflow`/`promoted` 一等的组合表示 `composition.steps: [{skill, params_binding, ...}]` 或 `composes: [skill_id]`；并考虑一个**可组合能力签名**（把 `inputs.preconditions` 与 `outputs` 提为类型化的 `provides/requires`，使"什么能接在 A 后"可机械推导）。把 consensus 成员映射也收回表示层。

**Should-decide（次要，可稍后）**
3. **G3 I/O 类型通用化**：把 `outputs.anndata` 泛化为可插拔的类型化产物注册（`typed_outputs: {kind: anndata|vcf|table|mzml|…, schema}`），或**显式承认 AnnData 偏置为既定设计**并记录之。
4. **G4 扩展纪律**：加保留命名空间（`ext:`/`x-`）让生态无侵入扩展；`origin`/成熟度轴改为"封闭核心 + 可扩展"；组合补齐后引入**接口 semver / 破坏性变更检测**。

**Codex 校正后的优先级（采纳，取代上面粗分）——决定顺序：**
1. **类型化参数契约**：`{name, type, required, default, choices/range, repeatable, source_ref?}`（并排保留 `hints` 作叙事，不替换）。
2. **最小 manifest-native 组合**：`steps` + skill 引用 + 参数绑定 + 产物绑定；**迁移/引用**既有 pipeline 与 consensus 映射（收编脑裂）。**不建全 DAG 引擎**——线性 pipeline + fan-out consensus 足以解锁获取。
3. **小能力签名**：required/provided 的**产物 kind + 数据状态键**，从现有 AnnData 状态 + 通用 file 产物起步（**不必**一上来给所有域做类型化输出，只类型化**可链接**的产物）。
4. **v3 版本派发 + 一个保留扩展命名空间**，同时保持 v2 可读。
5. **结构化 result/error 分类**（G7），喂养获取反馈/晋升打分。
6. **决定 AnnData-hub vs 通用多产物模型**（G3）；非 AnnData 域**增量**补。
7. **推迟**：完整接口 semver、MCP schema 导出、dense 检索、全域类型化输出——待核心飞轮表示稳定后再做。

**明确不要动**：C/路由（`Summary`）、治理三槽结构、`Security`、`Deps` 通道模型、单一真源单向生成——这些是资产。缺口都是**加字段**，不是重设计。

---

## 6. 待 Codex 交叉验证的承重判断

1. 组合边在声明式表示中确实缺失（仅 `SkipRule.use` + `superseded_by`；consensus 成员在 runtime）——**是否属实、是否真的阻断飞轮 P2/P4/检索链式**？
2. `hints` 无类型是否真的阻断 P2 参数提升 / P5 source_ref / MCP schema 派生？有没有我忽略的既有类型化路径？
3. "AnnData 偏置"对跨域飞轒是否为真问题，还是可接受的 hub-format 设计？
4. v3 加法式扩展（typed params + composition + 保留命名空间）是否真能保持 v2 向后兼容、且**足以**支撑飞轮——还是我遗漏了更根本的表示学缺陷（例如：是否应把"能力"与"实现"彻底解耦）？
5. 我是否**过度设计**（typed-output-for-all-domains、interface-semver 是否为时过早）？哪些该现在定、哪些该等真实消费者？
6. 结论"够做静态目录、不够做可组合飞轮"是否公允，还是低估/高估了现表示？

---

## 7. Codex 交叉验证结果（gpt-5.5 / xhigh / read-only）

**裁决：AGREE-WITH-CORRECTIONS。** Codex 独立结论与本文一致：*"`SkillManifest` v2 作为静态目录/路由/治理/迁移契约扎实；但尚不够通用/系统到能当飞轮的基础表示。两个真正的拦路石就是 manifest-native 的组合/能力签名 与 类型化参数契约。"*

**对 §6 六问的核验**
1. **组合缺失（G1）**：确认——manifest 内仅 `skip_when[].use` + `superseded_by`；95 个 manifest = 91 leaf/4 consensus/0 workflow；consensus 成员在 `sources.py:38`。**校正**：组合在 repo 里**存在但脑裂**（pipeline YAML + consensus registry，靠文件名接力、非表示层）——见 G1 已并入。
2. **参数无类型（G2）**：确认——`hints: dict`，消费方各取所需（`parameters_md.py:67` / `plan.py:104` / `argv_builder.py:114` 只 allow-list flag 名）；无 typed model / range / choices / source_ref。
3. **AnnData 偏置（G3）**：确认——spatial 17/19、sc 30/34 用类型化 AnnData 输出；genomics/proteomics/metabolomics/bulkrna/literature/orchestrator **全 0**。是否问题**取决于产品选择**（若 AnnData 是有意的 hub 就可接受；做通用多组学飞轮则不足）。
4. **扩展/兼容（G4）**：确认——`extra=forbid` + 封闭 Literal + 拒绝非 2 的 schema_version；ADR 声称有保留命名空间但**schema 里查无**；`version` 无接口 semver。
5. **MCP 派生（G2 子项）细化**：不是"派生坏了"，而是**MCP 导出根本未实现**（`input_schema_strategy` 仅字段、所有 skill.yaml `mcp.expose:false`、ADR 已推迟）；但现参数表示确实不足以将来派生有用 JSON schema。
6. **公允性**：公允；唯一校正是"repo 内组合脑裂"而非"完全缺失"。

**Codex 补的 3 个更根本缺口**（已并入 §3 G5–G7）：能力↔实现耦合、单入口脚本假设、契约无错误分类。

**Codex 的 over-engineering 反推（已采纳进 §5 优先级）**：不要 P2 前就给所有域做类型化输出（只类型化可链接产物）、不要先建全 DAG 引擎（最小 `steps` 足够）、接口 semver 先保留字段后强制、MCP 不上关键路径、**不替换 `hints`（并排加 typed params）**。

**关于向后兼容**：Codex 未否定"加法式 v3 + 保留 v2 可读"的可行性，但提醒 `extra=forbid` + 封闭 Literal + `schema_version` 硬拒非 2 意味着 v3 派发必须显式加（`_is_v2` 需放宽为 v2/v3 双读），否则不是"纯加法"。

---

*OmicsClaw 是多组学研究与教育工具，非医疗器械，不提供临床诊断。*
