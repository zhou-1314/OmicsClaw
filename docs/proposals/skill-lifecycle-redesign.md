# OmicsClaw 技能体系重思考——对照《Agent Skills 综述》(arXiv:2605.07358) 的全生命周期重设计

> **文档角色（2026-07-13 校准）：**本文保留为问题分析、论文映射和设计演进记录，
> 其中“现状”“差距”“路线图”均是阶段性快照，不能单独作为完成证明。四阶段系统的
> 当前目标边界、状态词汇、验收编号和完成判据，以
> [`2026-07-13-skill-audit-system-design-assessment.md`](../reviews/2026-07-13-skill-audit-system-design-assessment.md)
> 为准；ADR 0037/0041 等已接受决策仍分别是其领域内的架构真源。

> 状态：草案 v0.4（v0.2 纳入 Codex 全文"必须修正"；v0.3 按维护者澄清重写 §1「技能表示」的 `knowledge_base` 整合路径；**v0.4 与维护者共同敲定「技能表示」目标结构（单一 `skill.yaml` + 叙事 `SKILL.md`），经 Codex/gpt-5.5(xhigh) 复核并落为 [ADR 0037](../adr/0037-unified-declarative-skill-representation.md)** → 待维护者审核）
> 作者：OmicsClaw 维护团队（在 Claude 协助下，基于代码级审计生成）
> 参考：
> - Yingli Zhou, Wang Shu, Yaodong Su, Wenchuan Du, Yixiang Fang, Xuemin Lin,
>   *"A Comprehensive Survey on Agent Skills: Taxonomy, Techniques, and Applications"*, arXiv:2605.07358
> - 配套清单：JayLZhou/Awesome-Agent-Skills
> - 本文的现状判断来自一次对 `omicsclaw/skill/`、`scripts/`、`skills/`、`docs/adr/` 的并行代码级审计（已交叉校验，事实勘误见附录 A）。

---

## 0. 背景与目的

### 0.1 OmicsClaw 项目背景

OmicsClaw 是一个**本地优先（local-first）的多组学 AI Agent**：LLM 负责规划与操作，Python/R/CLI 工具在本地或远程运行时处理数据，原始矩阵不出本机。它覆盖 **8 个 domains**（7 个分析/编排域：spatial / singlecell / genomics / proteomics / metabolomics / bulkrna / orchestrator，外加 literature），目前共 **95 个技能**，由同一个 agent loop 驱动 CLI、Desktop、Channel 三个 Surface。

每个技能本质是一个 **hybrid 技能**：
- 一张面向 LLM 的方法学卡片 `SKILL.md`（frontmatter + 6 个固定正文小节）；
- 一段确定性可执行脚本（`--input/--output/--demo` 契约）；
- 配套 `parameters.yaml` 运行时 sidecar、`references/` 文档、`r_visualization/` 三层可视化、demo 数据与 `tests/`。

项目已有相当成熟的技能子系统：`skills/catalog.json` 生成式索引（含 `status` / `validation_level` / `trigger_keywords` / `type`）、治理脚本群（`audit_skill_requires.py`、`skill_lint.py`、`validate_skills.py`、`check_description_drift.py`、`run_eval.py`）、统一执行运行时（`omicsclaw/skill/execution/`，含正在进行的自适应环境置备）、以及可安装扩展机制（`omicsclaw/extensions/`）。

### 0.2 论文的核心框架

综述把"技能"形式化为一个三元组：

> **S = (M, R, C)** ——
> **M**（Main instructions）：agent 加载并遵循的主指令文档；
> **R**（Resources）：辅助资源（参考文档、可复用模板、可执行脚本、领域产物）；
> **C**（Conditions）：适用条件，决定**何时**应当检索并应用该技能。

它强调技能不仅描述"能做什么（WHAT，工具层）"，更要描述"何时做（WHEN）"与"如何做（HOW，含失败处理）"——即填补 LLM 的**程序性知识缺口（procedural gap）**。Agent 与 Skill 互补：**Agent 负责高层推理与规划，Skill 是可靠、可复用、可组合执行的操作层**。

综述围绕**四个生命周期阶段**组织文献，并补充三类横切要素：

| 阶段 / 横切 | 论文章节 | 子分类 |
|---|---|---|
| ① 表示 Representation | §III | 文本型 / 代码型 / 混合型（按 R 的资源配置分类） |
| ② 获取 Acquisition | §IV | 人工 / 经验 / 任务 / 语料 衍生 |
| ③ 检索与选择 Retrieval & Selection | §V | 稠密/稀疏/生成式/结构感知 召回；上下文动态选择 / 组合 / 成本-效用感知 / 反馈重排 |
| ④ 演化与治理 Evolution & Governance | §VI | 修订 / 校验 / 策略耦合 / 仓库演化 / 运行时治理 |
| 基础 Foundations | §II | 工具 / 协议(MCP) / 运行时 / RAG / 记忆 |
| 基准 Benchmarks | §VI-B、§VII | 检索质量 / 执行鲁棒性 / 库健康度；**净效用（net-utility）**，非仅相关性 |
| 生态 Ecosystem | §II-E、表 I | 技能枢纽（hub）：发现 / 版本化发布 / 安装 / 信任与溯源 |

论文点名的**四大开放挑战**：质量控制、互操作性、安全更新、长期能力管理。

### 0.3 一句话结论（先给判断）

> **OmicsClaw 在"表示"与"执行运行时"上异常成熟，明显领先于综述里常见的单轴系统；但恰恰在综述最看重价值的"演化"阶段最薄弱——没有"失败→技能"的反馈闭环，5 级 `validation_level` 阶梯设计完备却全员冻结在最低档（95/95 = smoke-only），团队已造好的最强校验/反幻觉工具没有进 CI。最大的机会是：让这些"沉睡的机制活起来"——先用 CI 把已有工具门控起来，再让一个"被挣得（earned）且有后果（consequential）"的 `validation_level` 由失败反馈闭环驱动，把一个"表示精良但静态"的技能库，变成一个自我改进的技能库。**

下文按论文的 7 个部分逐一展开，每节统一为四块：**论文框架 → OmicsClaw 现状（带证据）→ 差距 → 建议（含优先级）**。

---

## 1. 技能表示（Representation，论文 §III）

### 1.1 论文框架
综述按 **R 的资源配置**把表示分三类：**文本型**（叙事指令、reflexion、thought-buffer）、**代码型**（可执行脚本，Voyager 式确定性）、**混合型**（叙事包裹可执行代码）。质量标准是：schema 一致、M/R/C 可分离、机器可读、可版本化——以便下游检索/获取/治理在统一产物上运作。

### 1.2 OmicsClaw 现状（S=(M,R,C) 的落地）

| 三元组 | OmicsClaw 对应物 | 证据 |
|---|---|---|
| **M**（主指令） | `SKILL.md` 正文（叙事 M，6 小节）＋ Python 脚本（代码 M，`protocol.py` 契约） | `omicsclaw/skill/protocol.py:33`（`SkillModule`，AST 校验 `SKILL_NAME/SKILL_VERSION/main()`） |
| **R**（资源） | `references/{methodology,output_contract,parameters}.md`＋`r_visualization/`（三层可视化 + `figure_data/manifest.json`）＋ demo 数据 + `tests/` | `skill_lint.py:38`（`REQUIRED_REFERENCES`）；`parameters_md.py:16`（`parameters.md` 自动生成） |
| **C**（适用条件） | frontmatter `description` 的 **"Load when… / Skip when…"** ＋ `trigger_keywords` ＋ `requires_preprocessed` / `saves_h5ad` ＋ 运行时 preflight | `skill_lint.py:74`（强制 `Load when` 开头 + `Skip` 子句，≤50 词） |

**亮点**（综述少见的工程化）：
- **C 是一等公民且被 CI 校验**："Load when / Skip when" 契约（`skill_lint.py:74-92`）正是综述的 C，在真实技能库里很罕见。
- **真正的混合表示**：叙事 M + 代码 M + 声明式机器可读元数据（`parameters.yaml`），同一产物既服务 LLM 选择又服务确定性执行。
- **渐进披露被结构强制**：正文 ≤200 行（`skill_lint.py:49`），深度内容下沉到 `references/` 三件套，其中 `parameters.md` 由 `parameters.yaml` 自动生成。
- **反幻觉 Gotchas**：每条 Gotcha 的 `file:line`、`result.json[key]`、表/图文件名在 lint 时被 grep 校验против真实脚本（`skill_lint.py:183-257`）。
- **依赖面机器可重建**：frontmatter `requires:` 由 `audit_skill_requires.py` 从静态 import 扫描重建，已喂给自适应环境置备（`dep_spec.py`）。

> 注：以上是**主库 `skills/`（95 个）**的表示；此外仓库里还并存着一个**未进 registry 的暂存技能区** `knowledge_base/`（28 个从其他 repo 挖来的 workflow）与一条 knowhow 旁路——它们与统一表示的关系及整合路径见 **§1.5**。

### 1.3 差距
1. **无声明式 schema**：frontmatter + sidecar 的 schema 仅以手写常量存在于 `skill_lint.py:40-47`，并被 `lazy_metadata.py`、`generate_catalog.py`、`dep_spec.py` **各自独立重新解析**——四处漂移面，没有机器可读契约（直接撞上综述"互操作性"挑战）。
2. **成熟度维度有槽位但无信号**：`validation_level` 全 95 个=`smoke-only`，`status` 全=`mvp`（`generate_catalog.py` 仅按"有无脚本"派生），治理/基准阶段拿不到任何表示层健康信号。
3. **表示不统一：主库 v2 形态之外还并存"暂存技能区"与"knowhow 旁路"**（这是本节最关键、也是维护者最关心的差距）：
   - 主库 `skills/`（95）是 v2 hybrid 形态；
   - `knowledge_base/`（**28 个从其他 repo 挖来的 workflow**：各含 SKILL.md + `scripts/`(R/Python) + `references/`；`INDEX.md` 描述了一个可选 `assets/` eval 约定，但经核查**当前 0/28 实际带 `assets/`**）**未作为一等 skill source 纳入技能发现/catalog**（`omicsclaw/skill/registry.py`/`lazy_metadata.py` 不把它解析为技能；但它**会被 knowledge 子系统索引/读取**，并非完全无人引用），且其 SKILL.md 形态（"YAML 元数据 + 完整执行指南"）与 v2 **不一致**——这是一个**待整合的孵化/暂存区**，不是一等技能；
   - **knowhow guardrail 旁路** = `knowledge_base/knowhows/`（49 篇 `KH-*.md`）+ `KnowHowInjector` + `read_knowhow` 工具，向 agent 注入科学约束；**另外**，`omicsclaw/knowledge/` 还含一套对更广义 `knowledge_base` 文档/脚本的本地 **FTS5 检索**（`KnowledgeAdvisor`/indexer），它**不等同于 knowhow，也不是 skill registry**；
   - `SKILL_TYPES` 里的 `type=knowledge/adapter`（`lazy_metadata.py:30`）则是**声明但从未实例化**的空槽。
   → 同一类程序性知识当前散落在「v2 技能 / 暂存 workflow / knowhow 旁路 / 空 type 槽」四处，**表示不统一**，正是综述"互操作性 / 长期能力管理"挑战的具体体现。整合方向见 §1.5。
   （附带事实：`has_script=95/95`，库中并无"仅 SKILL.md、无 Python"的纯文本一等技能，见附录 A。）
4. **版本未交叉校验、无生命周期字段**：frontmatter `version` 与脚本 `SKILL_VERSION` 可静默分叉；无 `deprecated/superseded_by/changelog`，不利于"安全更新"。
5. **M 仍可能漂移**：仅 Gotchas 被锚点校验，且跨文件 `_lib` 锚点被跳过（`skill_lint.py:227-231`）；Flow / Inputs&Outputs / Key CLI 为自由文本。
6. **`requires:` 同名两义**：frontmatter（pip 列表）vs `parameters.yaml`（`{bins,env,config}` 系统契约），在 `lazy_metadata.py:174-186` 被记为蓄意 footgun，仍是互操作隐患。
7. **注入 M 被硬截断**：`load_skill_md`（`orchestration.py:803`）按 8000 字符裸切，长技能静默丢尾（See also / 后段 Gotchas）。

### 1.4 建议

> **目标结构已与维护者敲定并经 Codex 复核，落为 [ADR 0037：统一声明式技能表示（`skill.yaml` v2）](../adr/0037-unified-declarative-skill-representation.md)。** 下表第 1 行即指向该 ADR；其余行是围绕它的配套改造。

| 建议 | 动作 | 优先级 |
|---|---|---|
| **收敛为单一声明式真源 `skill.yaml`**（结构见 ADR 0037） | 把 `parameters.yaml` **升级/改名为 `skill.yaml`**（唯一机器契约，**不**新增第二份）；写 `omicsclaw/skill/schema.py`(pydantic) 取代 `skill_lint.py`/`lazy_metadata.py`/`generate_catalog.py`/`dep_spec.py` 四处常量解析；采用 `interface{inputs,parameters,outputs}` 分组 + `deps` **收敛为安装通道**（`python` 必备；`r`/`cli` 前瞻、须配消费者；删 `conda`（中心派生）、`env`/`config`→`interface.inputs.preconditions`、`os`→顶层 `compatibility`；`bash` 归 `runtime.language` 而非 deps 桶）+ `security`/`provenance`/`validation`/`lifecycle`；`schema_version` 让 v1/v2 并存；catalog/parameters.md/路由表/SKILL.md 头部**单向生成**并 CI `--check`（`deps` 粒度详见 ADR 0037「deps granularity」） | 高 |
| **让 `validation_level`/`status` 携带挣得的信号** | 由 `run_eval.py` + per-skill `tests/` 结果驱动晋级并写回 `parameters.yaml`，在 `generate_catalog.py` 暴露；无法挣得的档位就从分类里删掉 | 高 |
| **版本交叉校验 + 生命周期字段** | 在 `skill_lint.py` 比对 frontmatter `version` 与 `protocol.py` 抽取的 `SKILL_VERSION`；扩展 `deprecated/superseded_by/changelog` | 中 |
| **以"统一目标表示"承接 `knowledge_base` 暂存区，而非新增并行 type**（维护者澄清后修订） | **不**把 `type=knowledge` 做成嵌入 agent 的一等类型；而是：(1) 以单一声明式 schema（见本表第 1 行）为**唯一目标表示**；(2) 把 `knowledge_base/` 的 28 个 workflow 当作**孵化/暂存区**，经 `migrate_skill.py` 风格迁移逐步整合进 `skills/`；(3) 把 `knowhows/` guardrail 作为**来源**汇入目标技能的 `## Gotchas`/`references/methodology.md`；(4) 整合到位后**退役** knowhow 旁路与空置的 `type=knowledge/adapter` 槽。详见 §1.5 | 高 |
| **把 M↔代码漂移校验扩到 Gotchas 之外** | 解决跨文件 `_lib` 锚点（已有 TODO）；把 `output_contract.md` 路径声明 grep 校验против脚本 | 中 |
| **消歧 `requires:` 同名键** | 把 `parameters.yaml` 系统块改名 `system_requires:`，同步 `lazy_metadata/dep_spec/env_resolver` + lint，并用 `migrate_skill.py` 迁移 | 中 |
| **显式 M/R/C 映射 + 结构化截断** | 在 scaffolder 模板里标注哪段是 M/R/C；`load_skill_md` 改为按小节优先级丢弃而非裸切 | 低 |

### 1.5 `knowledge_base/` 暂存区 → 统一表示的整合路径（维护者澄清后新增）

> **背景澄清**：`knowledge_base/` 下的 28 个 workflow 是**从其他 repo 挖掘、暂存待用**的技能；维护者的方向是**逐步整合进 `skills/` 统一表示**，而**不**打算把它们作为一条并行 `knowledge` 通道长期嵌入 agent。因此"做好一个有效且统一的技能表示"正是这条整合路线的前置条件——**表示越统一，整合越能"机械且无损"**。

**当前并存的四种载体（"表示不统一"的根因）**

| 资产 | 位置 | 进 registry/catalog？ | 形态 | 角色 |
|---|---|---|---|---|
| 主库技能（95） | `skills/<domain>/<skill>/` | 是（catalog 95） | v2 hybrid：SKILL.md(6 节)+脚本+`parameters.yaml`+`references/`+`r_visualization/` | 一等技能 |
| 挖掘 workflow（28） | `knowledge_base/<topic>/` | **否**（不进技能 registry/catalog；改由 knowledge 子系统 FTS5 索引/读取） | "YAML 元数据+完整执行指南"+`scripts/`(R/Python)+`references/`（`INDEX.md` 提及 `assets/` eval 约定，但当前 0/28 实际带） | **暂存/孵化，待整合** |
| knowhow guardrail（49） | `knowledge_base/knowhows/` + `KnowHowInjector` + `read_knowhow`（注入）；另：FTS5 `KnowledgeAdvisor` 索引整个 `knowledge_base` | 否（旁路） | `KH-*.md`：`critical_rule/domains/related_skills/phases/search_terms` | 科学约束注入旁路（FTS5 检索 ≠ knowhow） |
| 空 type 槽 | `SKILL_TYPES`（`lazy_metadata.py:30`） | — | `knowledge`/`adapter` 从未实例化 | **保留槽，不**落地为并行一等类型；迁移路线定后删除或 reserved + lint 禁用 |

**整合方案（建议，review-in-the-loop 逐个推进）**

1. **先定唯一目标表示**：落地 [ADR 0037](../adr/0037-unified-declarative-skill-representation.md) 的单一声明式 `skill.yaml` + `omicsclaw/skill/schema.py`，显式承载 M/R/C 字段——这是承接外来技能的"插槽标准"，没有它，整合只会把不一致复制进主库。
2. **迁移而非并存**：扩展 `scripts/migrate_skill.py`，新增 `knowledge_base/<topic>` → `skills/<domain>/<skill>` 迁移模式：把"YAML 元数据+执行指南"映射到 v2 frontmatter + 6 小节；R/Python 脚本接入既有运行时（R 走现有 R 运行时，参见 `omicsclaw/core/r_script_runner.py` 与 `tests/test_r_script_runner.py`）；**为迁入技能新建 §6 的执行测试/评测**（`knowledge_base/` 当前 0/28 自带 `assets/` eval，需补建，不能假设已有）；每整合一个就按真实信号打 `validation_level`，并入 catalog/registry。
   - **迁移前去重**：先生成 `new / merge-into-existing / supersedes / duplicate-ignore` 四态清单（28 个 workflow 与 95 个主库技能在 bulk/sc/spatial/proteomics 等域明显重叠，禁止盲目新增）。
   - **每个迁入技能的验收项**：`--help` 轻量可跑、`--demo` 本地可跑、R/Python 依赖与外部 CLI 声明、输出契约、disclaimer、`requires` 自动审计、allowed flags、路径白名单、失败不污染输出目录。
   - **迁移 manifest**：逐条记录 `source_topic`、目标 domain/skill、`migration_status`、去重判断、`source_hash`、license/provenance、依赖、网络行为、`validation_level`——比单纯计数更可审计。
3. **knowhow 归位（不强行 1:1）**：能归入具体技能的 KH 进入目标技能 `## Gotchas`（须带证据）或 `references/methodology.md`；**跨域/跨技能的 guardrail 进入共享 methodology/contract 并被相关技能引用**，不硬塞进单个技能。**退役硬门槛**：仅当所有 KH 已映射或明确标为全局保留、且 `read_knowhow`/`KnowHowInjector` 的运行时消费者已有替代路径并通过测试后，才分阶段禁用并删除旁路（迁移一开始就删会破坏注入）。
4. **清空旁路与空槽**：整合到位后退役并行 knowhow 通道、删除/收敛 `type=knowledge/adapter` 空槽，杜绝"同类程序性知识散落四处"。
5. **进度可见**：在 catalog / 健康仪表盘（§6.4）加 `origin=knowledge_base-migrated` / `migrated_from` / `source_hash` / `migration_status` 标记与"剩余待整合 workflow 计数"，并产出一份**迁移 dry-run 报告**（列出 28 个 workflow 的目标域、脚本语言、依赖、是否联网、是否已有相似技能、缺失契约），让整合进度可追踪、可收尾。

**与铁律一致**：
- **不幻觉（升级为 schema/lint 规则，而非仅说明）**：默认阈值 / marker / 基因关联 / 统计 cutoff / 数据库版本必须带 `source_ref` 或 `evidence`；无来源只能留 `TODO`/待确认，**不得**成为运行默认值（与 §2.4 语料衍生硬约束一致）。
- **本地优先 / 数据不出本机**：对外来脚本做**静态扫描**（`requests`/`curl`/`wget`/API client/上传函数）——默认**禁止上传用户原始数据**；下载公共参考数据须显式声明并 opt-in。这是迁移外来脚本时最易踩中铁律的点。

> 说明：这条路线把综述里 §IV「语料/迁移衍生获取」与本节「统一表示」打通——`knowledge_base` 既是表示问题（要统一），也是获取问题（要迁移），二者共用同一份目标 schema。

---

## 2. 技能获取（Acquisition，论文 §IV）

### 2.1 论文框架
技能从哪来，四条来源轴：**人工衍生**（专家手写）、**经验衍生**（从 agent 自身轨迹，尤其失败，蒸馏可复用技能）、**任务衍生**（由任务规格直接合成）、**语料衍生**（从论文/工具文档/API 抽取）。最佳实践是**多轴组合 + 闭环自著/自扩 + 溯源 + 入库前校验**；其中经验衍生（含从失败学习）与语料衍生被视为更高价值、更难的能力。

### 2.2 OmicsClaw 现状（罕见地多轴覆盖）

| 来源轴 | 状态 | 证据 |
|---|---|---|
| **人工衍生**（主路径） | ✅ 成熟 | `templates/skill/`（ADR 0033：仅人工复制起点）；全部 95 个技能均为专家手写 |
| **任务衍生**（NL→脚手架） | ⚠️ 只产空壳 | `scaffolder.py::create_skill_scaffold`；但 `render_skill_script` 写的是占位 `('implement_method','todo')`，且 `_render_v2_description` 生成**循环描述**"Load when 用户要创建一个新技能…" |
| **经验衍生**（晋升成功轨迹） | ⚠️ 存在但不泛化 | `scaffolder.py::find_latest_autonomous_analysis / render_promoted_skill_script`，把自治分析（ADR 0032 mini-agent）的 accepted cells 晋升为技能——但**逐字拼接**、重注入 kernel facade、不参数化 |
| **运行时自著（闭环）** | ✅ 有，但只能脚手架/晋升 | `create_omics_skill` 工具（`agent.py:1186`，high-risk + 审批门），成功后 `refresh_registry` 热刷新 |
| **创建期治理门** | ✅ 强 | 隔离暂存工作区 + `ArtifactRequirement` 校验 + 成功才原子移入 `skills/` + `scaffold_spec.json`/`manifest.json` 溯源 |
| **语料衍生**（论文/工具文档→技能） | ❌ 缺失 | `literature` 技能只抽 GEO/数据集元数据并交棒给已有技能；`migrate_skill.py` 只是 repo 内重排，不抽取新程序知识 |
| **从失败学习** | ❌ 与技能解耦 | `failure_memory.py`（`FailureBank`）只服务 repo 自修补的 Meta-Agent，不回灌技能 |

### 2.3 差距
1. **语料衍生整轴为零**——而零件已具备（论文解析器 + scaffolder），是价值最高的缺口。
2. **任务衍生只产 TODO 壳 + 错误的 C**：每个脚手架技能出生即带需手改的循环描述。
3. **经验衍生不泛化**：晋升=逐字回放脚本，绑死原始 `--input`，无参数化/去硬编码。
4. **不从失败或重复中学习**：没有"同一目标成功 N 次→建议晋升"或"反复失败→蒸馏 Gotcha"。
5. **获取物未做正确性校验**：`render_skill_test` 仅断言文件存在；`validation_level` 停在 smoke-only。
6. **catalog 无来源/成熟度**：无法区分 human/scaffolded/promoted/migrated，不利长期库健康。

### 2.4 建议
| 建议 | 动作 | 优先级 |
|---|---|---|
| **新增语料衍生模式（论文/工具文档→技能）** | 扩展 `skills/literature/core/extractor.py` 加方法学抽取，给 `create_skill_scaffold` 一条 `--from-paper`/`--from-tool-docs` 路径：填充 `methodology.md`、从抽取术语种子 `trigger_keywords`、预填算法脚本骨架。**硬约束（铁律"不幻觉"）：生成技能的参数默认值/阈值/基因关联必须带来源引用；无来源只能标 `TODO`，禁止编造数值或关联** | 高 |
| **任务衍生真正合成正文** | 用真实 `--request` 生成 "Load when…/Skip when…"；把 `create_omics_skill → autonomous_analysis_execute → 自动晋升` 串成一条流水线（任务衍生即转经验衍生） | 高 |
| **经验衍生泛化** | 在 `render_promoted_skill_script` 加抽象 pass：把字面输入/参数提升为 argparse flag、去 facade 依赖、由 LLM 做一步"泛化此轨迹" | 高 |
| **入库前最小执行门** | 把 `render_skill_test` 升级为 `--demo` 冒烟（断言 result.json 契约 + exit code），在暂存区作为 `ArtifactRequirement` 跑通才原子移入；通过则升 `demo-validated` | 中 |
| **来源/成熟度入 catalog** | `generate_catalog.py` 读 `manifest.json`，emit `origin ∈ {human, scaffolded, promoted, migrated}` + 生命周期 status | 中 |
| **失败/重复回灌获取** | 加分析运行台账：重复成功→提示晋升；反复失败→喂入 Gotcha 种子（复用 `migrate_skill.py::_mine_gotchas`） | 低 |

---

## 3. 技能检索与选择（Retrieval & Selection，论文 §V）

### 3.1 论文框架
**召回**：稠密（embedding）/ 稀疏（关键词/BM25）/ **生成式**（模型解码时直接 emit 目标技能 token）/ 结构感知（层级、依赖图）。**选择**：上下文动态选择 / 技能组合 / **成本-效用感知** / **反馈驱动重排**。最佳实践=常驻廉价索引 + embedding 兜底 + 置信度门控消歧 + 机器可读 C + 可度量检索质量。

### 3.2 OmicsClaw 现状（关键再定性见下）
> **重要再定性**（已按 Codex 意见弱化为"待 trace 验证"的判断）：把 OmicsClaw 的路由简单说成"纯稀疏"并不准确——它是**"生成式上层选择 + 稀疏 resolver 下层召回"的混合路由**：LLM 在一个 95 项的 alias 枚举约束下直接 emit `skill='<alias>'`（对应综述的生成式召回 V-A3），而默认推荐路径 `skill='auto'` 则交给 `capability_resolver` 做手调稀疏确定性召回。**究竟哪条是"主路径"，需先用运行 trace 统计 alias-direct 与 auto-resolve 的实际占比再下结论**，本文不预设。无论哪条为主，真正值得度量的可靠性指标是**枚举约束解码的"幻觉别名率"**与稀疏召回的命中率，而非单一词面匹配率。

- **双路选择**：LLM 读领域 briefing 后命名 alias（生成式）＋ 确定性 `capability_resolver._candidate_score`（稀疏，手调权重：alias 命中 +12、描述词重叠 +0.85/词、trigger keyword 加权等）。**未发现用于"技能路由召回"的 dense/vector 检索**（grep `embedding|faiss|bm25|tfidf|cosine|vector` 在路由路径命中为零）。注意：此处仅指技能召回；AnnData/方法输出里的生物学 embedding 与本判断无关，不计入。
- **两级 domain→skill 层级 + 懒加载**：常驻 8 领域 briefing（~300–786 token）+ 95 项 alias 枚举；不足时 `list_skills_in_domain` 翻页（真正的类目树收窄）。
- **`skill='auto'` + 只读 `resolve_capability` 工具**：让 LLM 先探查覆盖（exact/partial/no_skill）再决定跑多分钟任务。
- **置信度门控消歧**：top1−top2 < 2.0 拒跑并返回 top3（`orchestration.py:151`）。
- **文件模态检测**：识别 `.h5ad/.vcf/.mzml` 等扩展名作为最强领域信号（`capability_resolver.py:488`）。
- **组合最小**：仅 1 条声明式 pipeline（spatial 5 步）+ 39 个脚本 emit `next_steps`（仅文本建议，不被遍历）。
- **token 预算可测但门已死**：`measure_routing_tokens.py` 可测（48 工具≈10,665 token、omicsclaw spec≈1,941、alias 枚举≈516、CLAUDE.md 路由块≈781），但 `check_routing_budget.py` 因 `build/routing-baselines/ceiling.json` **不存在**而 exit 2——门**不可运行**。
- **确定性 + 黄金快照治理**：alias 字母序 tie-break；21 条黄金快照 + 18 例 eval。

### 3.3 差距
1. **无 dense/embedding 兜底**：与论文最大分歧；无共享词面的改写查询会静默跌破 3.0 的 no_skill 阈值。
2. **13 个 singlecell 技能无 `trigger_keywords`**（sc-cell-annotation、sc-clustering、sc-markers 等），经 `skill='auto'` 近乎不可达。
3. **Skip-when（C）仅 prose**：resolver 做整描述词袋重叠，从不把"use X instead"解析为**排除/负信号**——跨技能消歧全甩给 LLM。
4. **魔数权重仅~20 query 黄金快照背书**，无标注语料、无 precision@1/消歧率定量指标。
5. **技能图组合几乎缺失**：`next_steps` 是建议文本而非可遍历依赖图。
6. **预算门不可运行 + 真正大头被忽略**：精修的 781-token 路由块被 ~10,665-token 工具注册表淹没。
7. **两个不一致的 close-tie 阈值**：`_AUTO_DISAMBIGUATE_GAP=2.0`（orchestration）vs `_RESOLVE_CLOSE_SECOND_GAP=1.5`（resolver）。

### 3.4 建议
| 建议 | 动作 | 优先级 |
|---|---|---|
| **加可选 dense-embedding 兜底层**（实验性，**默认关闭**） | 注册时预算 SKILL.md 描述 embedding（落盘缓存），在 `_candidate_score` 加 `_SCORE_SEMANTIC_*`，**仅**用于救回 no_skill / 破子阈值平局。硬约束（铁律）：**仅本地 embedding 后端、默认 no-op、查询脱敏、缓存限 workspace-local**；并须**先用路由 eval 证明它在 no_skill/平局场景有净收益**再启用 | 实验性 / P4（先验证） |
| **补 13 个 keyword-less 技能的 `trigger_keywords` 并 lint** | 写入 frontmatter，重生成 catalog + INDEX；`skill_lint.py` 加"空 trigger_keywords 即失败"规则防回归 | 高 |
| **提交路由预算基线并接入 CI** | `measure_routing_tokens.py --save` 生成并提交 `ceiling.json`（含 ~10.6k 的工具注册表项），在 CI 跑 `check_routing_budget.py` | 高 |
| **Skip-when 结构化为排除信号** | `lazy_metadata` 解析 "Skip… use <alias> instead" 为 `skip_to:` 列表；resolver 命中 skip-target 模态时负向降权 | 中 |
| **建标注路由 eval（precision@1/消歧率）并统一阈值** | 扩 `run_eval.py` 加跨 7 领域 (query→expected) 语料，emit precision@1 / top-3 recall / 消歧触发率；用它统一两个 close-tie 常量 | 中 |
| **把 `next_steps` 升级为真实技能 DAG** | 聚合 `next_steps` + pipeline YAML 成 registry 级 DAG，暴露给 `resolve_capability` 支持"and then"复合查询；泛化 `pipeline_runner` 执行 LLM 确认的链 | 中 |
| **常驻工具面渐进披露** | 把少用工具放到 deferred/tool-search 面，仅常驻路由关键工具 | 低 |

---

## 4. 技能演化与治理（Evolution & Governance，论文 §VI）

### 4.1 论文框架
五个子域：**修订**（反馈改写持久技能对象）、**校验**（修订后须过执行/验证才被信任）、**策略耦合**（技能基底进入控制器训练态，与策略共演化）、**仓库演化**（仓库级扩张/过滤/连接）、**运行时治理**（运行期管控检索/信任/退役）。理想是闭环：作者化→分级校验→执行→**失败被捕获并回灌**，在强制安全策略与自动漂移门下进行。

### 4.2 OmicsClaw 现状
- **校验阶梯存在但 inert**：`VALIDATION_LEVELS`（smoke-only→demo→fixture→benchmarked→production，`lazy_metadata.py:36`）已串入 registry/catalog，但 **95/95 停在 smoke-only**，且在检索/执行中**从不被读取**——纯描述性元数据。
- **`status` 无弃用态**：只有 `mvp`/`planned`（按有无脚本派生）。
- **强 linter**（自校验）：description/正文/`allowed_extra_flags⊇argparse`/references 新鲜度/workflow-shim/**Gotcha 锚点 grep**。
- **漂移检测是最强治理面**：SKILL.md description 为单一真源，catalog/INDEX/CLAUDE.md 路由表全自动生成且有 `--check`（`check_description_drift.py`、`sync_skill_docs.py`）。
- **依赖 reconciliation 业界级**：`audit_skill_requires.py`（AST 传递 `_lib` 跟随、core-vs-optional 分类、viz 符号解析、`DEPENDENCY_REGISTRY` 规范化；`--check` 为阻塞 CI 门，`--write` 仅并集）。
- **阻塞 CI 门很窄**：`pr-ci.yml` 仅 `generate_catalog --check` + `audit_skill_requires --check` + 审计回归测试 + **仅 1 个技能**的 per-skill 测试（spatial-preprocess）。`skill_lint --all`、`validate_skills`、`check_description_drift`（路由表/INDEX 面）、`check_routing_budget` 均**不在 PR CI**。
- **per-skill 测试在 CI 几乎不跑**：52/95 有 `tests/`，CI 只跑 1 个。
- **行为 eval 是路由级、周跑、与科学正确性脱钩**：`run_eval.py` 校验路由是否命中，不校验科学输出；不回灌晋级 `validation_level`。
- **输入校验 preflight 仅 1 个技能**（`preflight/sc_batch.py`），`__init__.py` 自承"通用引擎尚不存在"。
- **安全规则是 prompt 级**：本地处理、强制免责声明、覆盖前告警均靠 agent 遵守；仅"不幻觉"有代码校验（且该校验也未进 CI）。
- **自适应环境子系统风险管理优秀**：probe-first、非致命、可一键关闭、内容寻址+指纹、降级回 base env（`env_resolver.py`、`docs/proposals/adaptive-environment-provisioning.md`）。
- **无失败→改进闭环**：`SkillRunResult` 带 `stderr/exit_code`，ADR 0035 写 per-run `index.jsonl`，但无任何聚合成技能健康信号；Gotchas 全人工。
- **仅 rename 重定向**（`legacy_aliases`）作为迁移路径，无 `superseded_by`/退役。

### 4.3 差距
1. **没有"失败→技能"反馈闭环**——综述的核心演化机制，在这里完全缺失（但基底全在）。
2. **`validation_level` 阶梯 inert**：无晋级标准，不门控任何东西，成熟度对 agent/用户均不可见。
3. **最强校验工具未进 CI**：Gotcha-锚点/output-contract/描述子句/路由漂移可静默落到 main。
4. **per-skill 执行未在 PR 验证**：51 套测试 + 全部无测试技能的回归仅靠运气或周跑 eval 捕获。
5. **无弃用生命周期**：库只增不减，不会引导 agent 避开陈旧技能。
6. **安全规则无代码门**：免责声明/本地处理/覆盖告警靠 LLM 自觉。
7. **preflight 仅 1 个技能**：`requires_preprocessed` 已在元数据但未被强制为门。

### 4.4 建议
| 建议 | 动作 | 优先级 |
|---|---|---|
| **把整套校验进阻塞 PR CI** | `pr-ci.yml` 加 `skill_lint.py --all --strict`、`validate_skills.py`、`check_description_drift.py`、`check_routing_budget.py` | 高 |
| **per-skill 测试用 CI matrix 跑** | 由 catalog `has_tests`（或 changed-files）生成 matrix，逐技能 `pytest tests/`，按其 reconciled `requires:` 安装 | 高 |
| **闭合失败→技能反馈闭环** | `result.py` 加 `error_kind`，`runner._finalize_skill_run` + `index.jsonl` 聚合成 per-skill 健康台账 → (a) 自动降 `validation_level`，(b) 自动草拟**候选** Gotcha（**须含 stderr / result.json key / file:line 证据，人工批准后才写回 SKILL.md**；lazy_metadata 抽取器已消费该节），(c) catalog 标 `flaky` | 高 |
| **激活 `validation_level`（挣得+有后果）** | 由 CI 信号派生档位（demo 绿=demo-validated；tests 过=fixture-validated；在 eval 中=benchmarked），`skill_lint` 防越级，`capability_resolver/listing` 在选择时按档位标注/降权 | 中 |
| **引入真正的弃用生命周期** | schema 加 `status: deprecated` + `superseded_by`，lint + 路由排除/降权 + 重定向提示 | 中 |
| **preflight 通用化为 schema 驱动引擎** | `omicsclaw/skill/preflight/engine.py` 读声明式前置（`requires_preprocessed`/期望 obs/uns 键/必需 flag），`sc_batch` 改为首个消费者 | 中 |
| **核心安全规则代码化** | 在 `omicsclaw/common/report.py` 统一发免责声明并 lint；`runner` 输出目录处加覆盖守卫 | 低 |
| **策略耦合（VI-C）显式判为 out-of-scope** | OmicsClaw 驱动冻结 API LLM、无控制器训练，应显式声明而非默默略过 | 低 |

---

## 5. 基础设施：执行运行时与协议（Foundations，论文 §II）

### 5.1 论文框架
四个子轴：**工具**（actuator）、**协议/MCP**（跨 agent 标准化发现/调用）、**运行时**（环境隔离/置备、依赖解析、结构化 I/O、流式可观测、错误/preflight、沙箱/资源边界、可复现）、**RAG**。对代码型技能，运行时正是把"一次性脚本"变成"可信可复用产物"的关键。综述还把"记忆"列为技能协调的操作基底之一。

### 5.2 OmicsClaw 现状（运行时是另一处高成熟区）
- **单一执行缝（sync+async）**：所有 Surface 经同一 `_prepare_skill_run/_finalize_skill_run`，仅 driver 分叉（`runner.py:115/385/475`）。
- **进程组生命周期 + 取消**：`start_new_session=True` 进程组 + reaper + SIGTERM→5s→SIGKILL（两 driver 一致），防泄漏 GPU/CPU 子进程。
- **自适应环境置备（新）**：probe 真实 `requires:` → 分类 pip/conda/non-pip/deny → 内容寻址 `--system-site-packages` overlay venv，仅装缺失叶子、venv 自带 pip `--no-deps`（ABI 安全；发现 uv pip 会遮蔽 base numpy）；指纹短路、flock 串行、全程非致命。
- **依赖解析纯元数据**：三源合并（frontmatter `requires:` + `DEPENDENCY_REGISTRY` + pyproject optional-deps），拒绝对 torch/scvi-tools 发起注定失败的 mega-solve。
- **结构化 result**：`SkillRunResult`（frozen/slots）带 `runtime_source` 溯源（base|skip|probe|venv:<key>）。
- **状态感知 exit code**：技能写 `status`（ok/partial/failed）到 result.json，runner 信任之优先于裸 exit code。
- **流式日志**：逐行回调驱动 desktop SSE（async 路径仅聚合、不支持逐行）。
- **可复现产物**：成功即 emit README + `analysis_notebook.ipynb` + `commands.sh` + 钉版 `requirements.txt`，并入 Project（manifest + index.jsonl）。
- **解释器/env 加固**：`OMICSCLAW_RUN_PYTHON` 覆盖；注入 PYTHONPATH + `PYTHONNOUSERSITE=1`；`argv_builder` 仅转发白名单 flag，硬挡 `--input/--output/--demo`。
- **MCP 仅消费**：加载外部 MCP 工具，但**不把自己 95 个技能暴露为 MCP server**。
- **无沙箱/无超时**：技能即普通本地子进程，全 FS + 网络访问，无 wall-clock/rlimit/seccomp/namespace。

### 5.3 差距
1. **无 bounded execution**：无超时、无 rlimit；失控/内存炸弹只能靠外部 cancel——综述视其为底线。
2. **无沙箱/出网控制**："数据不出本机"靠 policy，非进程边界。
3. **MCP 仅消费**：技能无法被其他 agent/hub 经标准协议发现/调用。
4. **复现漏掉真实 env**：overlay venv 时 `runtime_source` 未写入 `requirements.txt`/notebook，自适应运行**不可忠实复现**。
5. **overlay 未 hash 锁**：同 key 在不同机器/时间可解出不同传递依赖。
6. **错误分类粗**：仅 ok/partial/failed + stderr，治理/检索/恢复无法按错误类型分流。
7. **无重试/断点续跑**：pipeline 首错即停，baton 文件本可作检查点却被忽略。
8. **静默 bookkeeping**：Project index/manifest 写入用裸 `except: pass`。
9. **RAG/记忆未纳入**：无向量/RAG 子系统；`index.jsonl`/`MEMORY.md`/`failure_memory` 未被显式建模为技能读写的记忆基底。

### 5.4 建议
| 建议 | 动作 | 优先级 |
|---|---|---|
| **两 driver 加 bounded execution** | `OMICSCLAW_SKILL_TIMEOUT` 看门狗（复用 cancel-watcher）+ `resource.setrlimit(RLIMIT_AS/CPU/NPROC)`；超时作为新 result 结局 | 高 |
| **把真实运行时写进复现包** | 把 `runtime_source` + overlay `pip_specs`/key 透传到 `output_finalize` + notebook，`write_repro_requirements` 追加 overlay 实装规格 | 高 |
| **把 OmicsClaw 技能暴露为 MCP server**（**默认关闭**，与铁律有天然张力） | 新增 `omicsclaw/surfaces/mcp`（FastMCP/stdio），从 registry 枚举技能生成 tool schema，背靠 `arun_skill`，复用 argv 白名单。硬约束：**仅 localhost/stdio、默认关、技能 allowlist（不默认暴露全部 95 个）、逐次用户确认、路径白名单**，且应在执行层先有超时/rlimit/网络边界（见 §5.4）之后再考虑——否则外部 agent 可经 MCP 触达本地文件/长任务/网络/输出目录 | 实验性 / P4（须先有沙箱边界） |
| **preflight 通用化 + error_kind** | 见 §4.4；`result.py` 加 `error_kind`（missing-dependency/bad-input/oom/timeout/crash/cancelled） | 中 |
| **overlay hash 锁** | 装后 `pip freeze` 入 `.meta.json`；`OMICSCLAW_ENV_LOCK` 模式 `--require-hashes` | 中 |
| **pipeline 超时 + 断点续跑** | 检测既有 baton + per-step 状态 sidecar，从最后成功步恢复；加 per-step timeout/retry | 中 |
| **bookkeeping 可观测** | 裸 `except: pass` 换 `logger.warning` | 低 |
| **probe 记忆化** | 按 (base python real path+mtime, sorted import_names) 缓存 `_probe_missing` | 低 |

---

## 6. 基准与生态（Benchmarks & Ecosystem，论文 §VI-B / §II-E / 表 I）

### 6.1 论文框架
**基准**三轴：检索质量（precision@k/MRR/路由准确率）、执行鲁棒性（被调技能能否真跑且产出契约一致的正确结果）、库健康度（一致性/被测/无漂移/成熟度可追踪）。综述最清晰的实证动机是 **SkillsBench**：精心策划的技能在某些任务上甚至**净效用为负**；故标准检索指标（top-k recall）**不足以**衡量最终执行是否成功、是否净正向——需**执行感知 / 净效用**评分。**生态**：技能作为可共享产物，活在**技能枢纽（hub）**里——中央索引发现、版本化发布、安装入宿主、信任/溯源、依赖解析。

### 6.2 OmicsClaw 现状（评测真实但门控偏弱；生态比想象中更完整）
- **真 LLM-in-the-loop eval harness**：15 个 `EvalCase` + 3 个 audit 例（共 18），5 类（routing/adversarial/methodology/regression/ux），双语，must/should 语义（must 失败阻塞、should 仅告警），落盘 per-case JSON + REPORT.md，有成本守卫；但**周跑而非按 PR**。
- **确定性检索回归基准**：21 条黄金快照，甚至钉住已知误路由（便于 diff），但**无 precision@k/MRR**。
- **per-skill 执行测试**：52/95 子进程跑 `--demo` 断言 exit 0 + result.json 契约。
- **自适应 env 测试是 repo 最强**：dep 分类对**全体技能 requires 的真实并集**做快照、内容寻址 key、ABI 安全 overlay 安装、可选网络 E2E。
- **库健康元数据**：catalog 带 status/validation_level/has_*；但全员 smoke-only/mvp（死元数据）。
- **静态质量 linter**：见 §1/§4。
- **CI 仅跑一薄片**：见 §4.2；`skill_lint --all`、`validate_skills`、漂移、预算、黄金快照、dep/venv/env 测试、51 套 per-skill 测试**均不在 PR CI**。
- **生态——已有可安装 skill-pack 扩展系统（被低估的资产）**：`omicsclaw/extensions/`（`manifest.py`/`validators.py`/`loader.py`）+ `/install-skill`、`/install-extension` 支持**从本地或 GitHub URL 安装**，`omicsclaw-extension.json` 声明 pack 类型 + `trusted_capabilities`，**基于能力的信任模型**把不可信（GitHub）源降权到 skill-pack 且限定能力白名单；无 manifest 的旧目录自动识别为 skill-pack。（已定向核对到上述文件、两个安装命令、本地/GitHub 来源与 `trusted_capabilities`；**"7 种 pack 类型""路径穿越防护"的具体强度尚待补精确 `file:line` 核实**，核实前按"未独立核实"对待。）
- **生态——发现仅 repo 本地**：catalog + INDEX + CLAUDE.md 路由表，无中央/远程 hub、无可检索 registry、无版本发布流、无评分/热度、无签名溯源。

### 6.3 差距
1. **`validation_level` 阶梯全冻结**——为之设计的核心库健康指标是死的。
2. **CI 仅跑 1 个技能执行测试**；其余 51 套 + 全套 linter 不门控 PR。
3. **29 个技能 / 5 个完整领域零执行测试**（genomics 10、proteomics 8、metabolomics 8、orchestrator 2、literature 1）——见附录 A 的勘误。
4. **eval 语料小（15）且完全缺 proteomics + metabolomics 路由**；周跑使回归最多迟 7 天暴露。
5. **无定量检索指标**（precision@k/MRR）。
6. **无库健康仪表盘/趋势**：数据散落，无聚合、无随时间追踪。
7. **无中央 hub**：分享仅靠裸 GitHub URL，无版本兼容、依赖解析、签名。
8. **扩展 manifest 无 semver 宿主兼容字段、无跨 pack 依赖解析**（`manifest.dependencies` 只记录不解析）。
9. **net-utility 未测**（SkillsBench 负效用）：当前把"路由正确"等同于"任务成功"；**常驻注入的 8000 字符 SKILL.md 从未被 A/B 测过是否反而拖累答案**。

### 6.4 建议
| 建议 | 动作 | 优先级 |
|---|---|---|
| **把已有质量门接入 PR CI** | 见 §4.4（最高杠杆：工具已在，只是没门控） | 高 |
| **补 5 个未测领域的 per-skill 执行测试 + 13 个 keywords** | 用 `spatial-de/tests` 成熟范式生成；scaffolder/migrate 为每个新技能 emit 测试桩；CI 断言 has_script⇒has_tests | 高 |
| **激活 `validation_level` 为活的库健康指标** | `generate_catalog.py` 从观测信号派生档位而非静态 smoke-only；`skill_lint` 防越级 | 高 |
| **加库健康仪表盘 + 趋势** | `scripts/skill_health.py` join catalog + 最新 test/lint/eval 结果（per-skill + per-domain rollup + delta），emit 到 GitHub Step Summary 并快照入 `build/` | 中 |
| **扩展并量化检索/净效用 eval** | 加 proteomics+metabolomics 路由例；算 precision@k/MRR；**评分"被执行技能是否产出契约一致非错结果"（净效用）**；**A/B 8000 字符 SKILL.md 注入是否净负** | 中 |
| **把扩展机制推向真正的技能 hub** | manifest 加 semver 宿主兼容 + 解析 `dependencies`；把 catalog 发布为可查询索引 + 薄中央 registry，让 `/install-skill` 按名+版本解析（检索/浏览/签名溯源） | 低 |

---

## 7. 跨阶段优先级与路线图

### 7.1 总体成熟度判断（综述视角）
OmicsClaw 在**表示**（CI 可校验的 Load/Skip 适用条件、真正混合的 S=(M,R,C)、grep 锚定的反幻觉 Gotchas）与**执行运行时**（统一 sync/async 缝、进程组生命周期+取消、非致命内容寻址自适应环境）上明显领先。最弱处正是综述最看重价值之处：**演化阶段没有失败→技能闭环；5 级校验阶梯设计完备却全员冻结；团队已造好的最强正确性/反幻觉工具没进 CI**（PR CI 只跑 1 个技能测试，且路由预算基线 `ceiling.json` 根本不存在）。

### 7.2 跨阶段（多阶段联动）建议，按 ROI 排序
1. **让 CI 门控已有质量工具，并提交缺失的路由基线**（横跨 演化/检索/基准/表示）——否则下面所有建议都会在 main 上静默回归。
2. **端到端闭合"失败→技能"反馈闭环**（执行错误分类 → per-skill 健康台账 → 自动降级/自动草拟 Gotcha / 检索反馈重排 / 重复成功晋升）——综述的定义性演化机制，且全是连接已有零件的工作。
3. **让 `validation_level` 成为"挣得且有后果"的成熟度信号**（由 #1 的 CI 信号驱动，被检索/弃用消费）——治理、弃用、成熟度感知路由、未来 hub 都依赖它。
4. **schema 收敛为单一声明式真源 `skill.yaml`（[ADR 0037](../adr/0037-unified-declarative-skill-representation.md)）+ `schema_version` + 消歧 `requires:`**——消除四处解析漂移，是"互操作性"挑战与真 hub 的前提。
5. **补 29 个未测技能的执行测试 + 13 个无 keyword 技能的 `trigger_keywords`**——机械、有界、可由 scaffolder emit，立刻抬高可测库健康与检索可达。
6. **加净效用/执行感知 eval（precision@k/MRR + 被执行结果正确性），并测 SKILL.md 注入是否拖累答案**——落地综述最清晰的实证洞见。
7. **preflight 通用引擎 + 执行边界（超时/rlimit）+ 安全规则代码化 + 复现记录真实 env**——补齐执行期风险围栏。
8. **加语料衍生获取（论文/工具文档→技能）+ 让任务衍生真正合成正文**——补齐两条几近失效的获取轴。

### 7.3 建议分期
- **P0（地基，1–2 周）**：提交 `ceiling.json`；把 `skill_lint --all`/`validate_skills`/漂移/预算/黄金快照/dep-venv-env 测试进 PR CI；per-skill 测试 CI matrix。→ 解锁后续一切。
- **P1（让机制活起来）**：`error_kind` + 健康台账 + 失败闭环；由 CI 信号驱动的 `validation_level` 晋级 + 检索消费。
- **P2（互操作与覆盖）**：声明式 schema + `schema_version` + 消歧 requires；补 29 测试 + 13 keywords + 弃用生命周期。
- **P3（评测升级）**：net-utility / precision@k/MRR eval + proteomics/metabolomics 语料 + SKILL.md 注入 A/B；库健康仪表盘。
- **P4（前沿能力）**：dense 检索兜底；preflight 引擎 + 沙箱/超时；MCP server；技能 DAG 组合；语料衍生获取；真 hub。

### 7.4 对应综述四大开放挑战
| 综述挑战 | OmicsClaw 当前 | 本方案对策 |
|---|---|---|
| 质量控制 | 仅结构 lint，科学正确性盲（"form 比 substance 强制得多"） | net-utility eval + 入库执行门 + 无 ground-truth 的正确性预言（见下"待决"） |
| 互操作性 | 四处解析漂移、requires 同名两义、仅消费 MCP | 单一声明式 schema + MCP server + manifest semver |
| 安全更新 | 无 superseded_by、版本不交叉校验、最强校验未进 CI | 生命周期字段 + 版本校验 + CI 门控 + 弃用路由降权 |
| 长期能力管理 | validation_level 冻结、库只增不减、无健康趋势 | 活 validation_level + 弃用生命周期 + 健康仪表盘趋势 |

### 7.5 待决问题（需维护者拍板）
1. **无 ground-truth 的科学正确性校验**：组学输出正确性如何在缺金标准时被基准化？（综述最难挑战，且因 OmicsClaw 的"非医疗器械"定位尤为敏感。）建议先用"已知数据集 + 已发表预期方向"的弱预言起步。
2. **dense 检索 vs 本地优先**：embedding 后端是否接受可选本地模型？是否容忍多一份磁盘缓存？
3. **MCP server 暴露面**：把 95 个技能暴露给外部 agent 是否符合安全姿态？是否仅暴露白名单子集？
4. **闭环的"自动写回"边界**：自动草拟 Gotcha / 自动降级 `validation_level` 是否需人工审批门（类比 `create_omics_skill` 的 approval-gated）？

---

## 附录 A：事实勘误（基于 `skills/catalog.json` 当前快照）

> 多智能体审计中出现过若干彼此矛盾的计数，经对照 `skills/catalog.json` 等真源校正如下，本文正文已采用校正值。
> 说明：以下数字是**当前 catalog 快照**得出（非永恒真理），catalog 更新后需重新生成；带"待核实"标记的项请在定稿前补可复现命令。

- **`has_script` = 95/95**（并非 91/95）。`type` 分布才是 `leaf=91 / workflow=4`；`knowledge`/`adapter` 为**声明但未实例化**的保留槽（**本轮不落地为嵌入 agent 的并行一等类型**；迁移路线确定后应删除，或标记为 reserved 并加 lint 禁止使用，见 §1.5）。挖来的程序性知识当前另存于 `knowledge_base/`（28 个 workflow，不进技能 registry，但由 knowledge 子系统 FTS5 索引/读取）与 knowhow 旁路——见 §1.5 的整合路径。
- **per-skill 测试 = 52/95（aggregate 已核对）**。per-domain 明细（spatial 19/19、singlecell 28/34、bulkrna 5/13；genomics 0/10、proteomics 0/8、metabolomics 0/8、orchestrator 0/2、literature 0/1 → **29 个技能、5 个领域零执行测试**）**待用可复现命令独立复核**（如 `python scripts/generate_catalog.py` 的 `has_tests` 字段，或 `find skills -path '*/tests/test_*.py'` 按域汇总）；复核前 per-domain 数字标"待核实"。
- **eval 语料 = 15 个 EvalCase + 3 个 audit 例（共 18）**；**黄金路由快照 = 21 条**（**待核实**：定稿前补可复现路径与计数命令，如 `tests/eval/invariants.py::EVAL_CASES`、`tests/fixtures/golden_routing/snapshot.json`）。
- **检索召回再定性**：主机制是综述的**生成式召回（LLM 在 alias 枚举约束下解码）**，稀疏打分为确定性兜底——故真正的可靠性度量应是"幻觉别名率"，而非词面命中率。
- **路由预算门**：`build/routing-baselines/ceiling.json` **不存在且未被 git 跟踪**，`check_routing_budget.py` 因此 exit 2——门**既未门控也不可运行**；修复必须先 `--save` 提交基线，而不仅是加 CI 步骤。

## 附录 B：关键证据索引（便于审阅核对）
- 表示：`omicsclaw/skill/protocol.py:33`、`scripts/skill_lint.py:40-92,183-257`、`omicsclaw/skill/lazy_metadata.py:14-56,174-186`、`omicsclaw/skill/parameters_md.py:16`
- 获取：`omicsclaw/skill/scaffolder.py`（`create_skill_scaffold`/`render_skill_script`/`render_promoted_skill_script`/`find_latest_autonomous_analysis`）、`skills/orchestrator/omics-skill-builder/`、`docs/adr/0032-autonomous-code-mini-agent.md`、`docs/adr/0033-skill-template-is-human-copy-only.md`、`omicsclaw/autoagent/failure_memory.py`
- 检索：`omicsclaw/skill/capability_resolver.py`、`omicsclaw/skill/orchestration.py:151,782-805`、`omicsclaw/skill/domain_briefing.py`、`scripts/measure_routing_tokens.py`、`scripts/check_routing_budget.py`、`docs/adr/0013/0015/0016`
- 演化：`scripts/audit_skill_requires.py`、`scripts/skill_lint.py`、`scripts/check_description_drift.py`、`scripts/sync_skill_docs.py`、`omicsclaw/skill/preflight/`、`.github/workflows/pr-ci.yml`、`docs/adr/0030-first-class-skill-type-system.md`
- 执行：`omicsclaw/skill/runner.py`、`omicsclaw/skill/result.py`、`omicsclaw/skill/execution/{subprocess_driver,async_subprocess_driver,dep_spec,env_resolver,venv_provision,pipeline_runner,output_finalize}.py`、`omicsclaw/surfaces/cli/_mcp.py`
- 基准/生态：`scripts/run_eval.py`、`tests/eval/`、`tests/test_capability_resolver_golden.py`、`omicsclaw/extensions/{manifest,validators,loader}.py`、`omicsclaw/surfaces/cli/_skill_management_support.py`、`skills/catalog.json`

---

## 附录 C：Codex (gpt-5.5, xhigh) 独立审阅记录

> Codex 已读取仓库代码核对关键论断。总体判断：**方向成立，论文框架转述基本准确**（S=(M,R,C)、四阶段、检索召回 dense/sparse/generative/structure-aware 分类经核对无误）；最大问题是若干高风险表述过度定性，以及 §3.4/§5.4 把 dense 检索与 MCP server 置于高优先级——应降级为"有条件、需验证后启用"。
> 主线结论：**先让已有 CI / 评测 / `validation_level` 活起来，再谈 dense、MCP、hub 这类扩展面。**

### C.1 必须修正（本 v0.2 已应用 ✅）
1. ✅ §0.1 领域口径：7→**8 domains**（7 分析/编排域 + literature）。
2. ✅ §3.2 路由定性：去掉"生成式召回为主"的过度自信，改为"生成式上层选择 + 稀疏 resolver 下层召回的混合路由，主路径待 trace 统计后再判定"。
3. ✅ §3.2 "无 embedding/dense"措辞收窄到"技能路由召回"，排除生物学 embedding 误伤。
4. ✅ §3.4 dense-embedding 兜底：高 → **实验性/P4（默认关闭、仅本地后端、查询脱敏、workspace-local 缓存、先用 eval 证净收益）**。
5. ✅ §5.4 MCP server：高 → **实验性/P4（默认关、localhost/stdio、技能 allowlist、逐次确认、路径白名单、先有沙箱边界）**。
6. ✅ §6.2 扩展系统：核对属实的部分保留；"7 种 pack 类型 / 路径穿越防护"标注"待补 file:line 核实"。
7. ✅ 附录 A 标题：去掉"ground truth"，改为"基于 `skills/catalog.json` 当前快照"。
8. ✅ 附录 A per-domain 测试明细：aggregate(52/95) 已核实，per-domain 与"5 域为 0"标"待用可复现命令复核"。
9. ✅ 附录 A `eval=15+3 / golden=21`：标"待核实"并指向 `tests/eval/invariants.py`、`tests/fixtures/golden_routing/snapshot.json`。
10. ✅ 附录 A `ceiling.json`：保留（核对成立），修复动作已写明"先生成并提交 baseline 再接 CI"。

### C.2 建议优化（**待维护者审核时决定是否采纳**，尚未改动正文）
1. **§7.2/§7.3 P0 过重**：全量 per-skill matrix + dep/venv/env 测试可能拖垮 PR。建议拆为 **P0a**（ceiling + lint/drift/golden 等 cheap deterministic gates）/ **P0b**（仅 changed-skill demo 测试矩阵）/ **P0c**（全量 52/95 放 nightly 或 label 触发）。
2. **§6.3 net-utility 前移**：从 P3 提到 **P1/P2**，最小指标=路由正确 + 执行成功 + 产物契约非 failed + 未触发错误技能。
3. **§1.4 schema 收敛前移到 P1**（validation/hub/MCP/扩展安装的共同前置；只覆盖现有字段，不先做大迁移）。
4. **§7.5 无 ground-truth 科学正确性**：从"待决"升级为具体计划——每域选 1–2 个公开小数据集，定义弱预言（维度 / 关键列 / 统计方向 / 已知 marker·通路方向只作 sanity，不作诊断结论）。
5. **§3.3 成本-效用选择**（综述 V-B3，本文低估）：resolver 输出 estimated runtime / dependency risk / validation level，同等相关性下优先低成本、高验证等级技能。
6. **§5.3 记忆落地**：把 run index、failure bank、user accept/reject 统一为 skill memory events，先服务反馈重排 + 健康台账，不先做泛 RAG。

### C.3 可选增强（锦上添花，待维护者取舍）
1. §3 增"反馈重排"小节（综述 V-B4）：用取消/失败/rerun/手改 skill 的 trace 更新排序，**仅作软信号，不自动改科学参数**。
2. §6 增"评测分层"：routing / execution-contract / scientific-sanity / net-utility 分开报，避免把"能跑"误写成"科学正确"。
3. §5 bounded execution 落地顺序：优先 timeout + output overwrite guard；rlimit/seccomp/namespace 后置（跨平台成本更高）。
4. §6 生态：把"hub"明确为长期目标——先 signed local pack + manifest 兼容约束，再中央 registry；在未加签名/兼容前不宣传为成熟生态。

### C.4 §1「技能表示」专项复核（v0.3，Codex/gpt-5.5 xhigh，已读代码核对）

> 总体：§1.4 路线修正正确、§1.5 整合方案可行且不天然冲突三条铁律；以下事实/措辞已按复核改定。Codex 抽查确认：`knowledge_base/` 确有 28 个带 `SKILL.md` 的 workflow（均含 `scripts/`+`references/`）、`knowhows/` 49 篇、catalog 95 且 `has_script=95/95`、`type=leaf 91/workflow 4`、无 `knowledge/adapter` 实例、`SKILL_TYPES`/`KnowHowInjector`/`read_knowhow` 均属实。

**必须修正（v0.3 已应用 ✅）**
1. ✅ `assets/ eval`："部分带"与仓库不符——**0/28** 实际带；改为"INDEX 提及约定但当前 0/28 实际带，迁移需补建"。
2. ✅ 证据口径：去掉不存在的 `core/registry.py` 式引用，改为"未作为一等 skill source 进技能发现/catalog；但由 knowledge 子系统索引/读取，并非完全无人引用"。
3. ✅ 角色拆分：`knowhow 旁路`（`knowhows/`+`KnowHowInjector`+`read_knowhow`）与 `omicsclaw/knowledge/` 的**更广义 FTS5 检索**（`KnowledgeAdvisor`/indexer）分开表述，后者 ≠ knowhow、≠ registry。
4. ✅ `knowledge/adapter` 措辞：从"定义或清理"改为"保留槽，本轮不落地为并行一等类型；迁移后删除或 reserved+lint 禁用"，消除"继续实现并行类型"的歧义。
5. ✅ knowhow 退役**硬门槛**：仅当 KH 全部映射/保留、且 `read_knowhow`/`KnowHowInjector` 消费者有替代并过测后才分阶段删除。

**建议优化（v0.3 已并入 §1.5 ✅）**
- ✅ 迁移 manifest（source_topic/目标/状态/去重/source_hash/license/依赖/网络/验证等级）；
- ✅ 每个迁入技能验收清单（`--help`/`--demo`/依赖与外部 CLI/输出契约/disclaimer/requires 审计/allowed flags/路径白名单/失败不污染输出）；
- ✅ 迁移前 `new/merge/supersedes/duplicate-ignore` 四态去重；
- ✅ knowhow 不强行 1:1，跨域规则进共享 methodology/contract；
- ✅ "不幻觉"升级为 schema/lint 规则（默认阈值/marker/cutoff/DB 版本须带 `source_ref`）。

**可选增强（v0.3 已并入 ✅）**
- ✅ catalog 加 `migrated_from`/`source_hash`/`migration_status`；✅ 迁移 dry-run 报告；✅ 外来脚本本地优先静态扫描（`requests`/`curl`/`wget`/上传函数，默认禁上传原始数据、下载须 opt-in）。
- 待维护者定：`adapter` 若保留，需先界定它与 runtime/tool adapter 的边界，否则从枚举移除或 reserved 化。
