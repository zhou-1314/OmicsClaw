# OmicsClaw 结构感知技能检索（Structure-Aware Retrieval）可行性评估与优化计划

> **文档角色（2026-07-13 校准）：**本文是 retrieval 的可行性与实施方案，不是落地证明。
> 当前 resolver 仍以 domain-first 词法评分为主，但 2026-07-13 首轮修复已接入结构化
> `skip_when` redirect、lifecycle filter 和 validation tie-break。2026-07-13 又落地了
> 26-case、8-domain 的版本化 routing oracle，以及 precision@1/top-3 recall/domain/
> decision/alias-hallucination 全局与逐域门；preconditions 与候选兼容图仍未落地。当前验收状态以
> [`2026-07-13-skill-audit-system-design-assessment.md`](../reviews/2026-07-13-skill-audit-system-design-assessment.md)
> §5/§8 为准。
>
> 状态：草案 v0.2（v0.1 经 Codex/gpt-5.5(xhigh) 独立审阅，已纳入其"必须修正"6 项 + "建议优化"5 项；全记录见附录 C → 待维护者审核）
> 作者：OmicsClaw 维护团队（在 Claude 协助下，基于对三个外部参考系统 + 本仓 `omicsclaw/skill/` 的并行代码级审计生成）
> 日期：2026-06-30
> 评审：Codex（gpt-5.5, xhigh）独立审阅 717s，逐条核对了本仓 live tree 的 `file:line`；记录见附录 C
> 参考系统（均为代码级审计，证据索引见附录 A）：
> - **AgentSkillOS** — *Organizing, Orchestrating, and Benchmarking Agent Skills at Ecosystem Scale*（arXiv:2603.02176），`skill_selection_repo/AgentSkillOS`
> - **SkillRL** — *Evolving Agents via Recursive Skill-Augmented Reinforcement Learning*（arXiv:2602.08234），`skill_selection_repo/SkillRL`
> - **GraphSkill** — *Documentation-Guided Hierarchical Retrieval-Augmented Coding for Complex Graph Reasoning*（Wang et al., 2026），`skill_selection_repo/GraphSkill`
> 关系定位：本文是 [`skill-lifecycle-redesign.md`](./skill-lifecycle-redesign.md) **§3「检索与选择」的深化与可执行化**，并以 [ADR 0037](../adr/0037-unified-declarative-skill-representation.md) 的声明式 `skill.yaml v2` 为结构地基。**本文不重复 §3 的 gap 分析**，只在其结论上增量。

---

## 0. 背景与目的

### 0.1 触发问题
> "结构感知检索适合大型技能库：先按领域粗分，再按任务类型细分；或根据技能间依赖关系过滤掉前置条件不满足的候选。能否在 OmicsClaw 中使用？"

把"结构感知检索"拆成**两个正交机制**，全文据此评估：
- **(A) 层级粗到细收窄（hierarchy coarse-to-fine）**：domain → task-type → algorithm 逐层过滤候选集。
- **(B) 依赖/前置过滤（dependency / precondition filtering）**：用技能间的"产出→消费"关系与"前置条件"剪掉当前数据状态下不可用、或被显式排斥（"Skip when…use X"）的候选。

二者价值与成本差异很大，必须分开判断（§3）。

### 0.2 三个外部参考系统一句话定位
| 系统 | 实质 | 与"结构感知检索"的关系 | 对本计划的最大价值 |
|---|---|---|---|
| **AgentSkillOS** | 200k 技能的检索+编排"操作系统" | **机制 (A) 的成熟参考实现** + 真实 DAG 调度器 | LLM 粗到细树检索 / active-dormant 分层 / topo-sort+phase+cascade-skip 编排骨架；以及"边由 LLM 每次幻觉"的**反面教训** |
| **SkillRL** | RL 中"经验→技能"的记忆层 | 仅 2 级层级；embedding 模式**抹平**层级 | 技能记录 schema、预算感知注入、按类目成功率触发的**演化闭环** |
| **GraphSkill** | **图推理 benchmark**（非技能检索系统；名字误导） | 仅其 `retrieval_agent.py` 是 LLM 粗到细遍历 + **回溯** | 错误粗分类的**回溯纠错**思路 + **检索质量与终任务解耦评测**的模板 |

> 命名澄清（避免误导后续读者）：**GraphSkill 与"图结构依赖检索"无关**。它检索的是 NetworkX 文档树（API 分类法），"图"指 benchmark 的题目领域，不是检索索引结构；全仓**无任何依赖边/前置边**。详见 §1.4。

### 0.3 一句话结论（先给判断）
**可行，且大部分地基已在既有路线图内**——但价值高度集中在机制 **(B)**，机制 **(A)** 在 95 技能规模下边际收益有限。

- OmicsClaw 的 `capability_resolver` **本就是 domain-first 的粗到细稀疏召回**（`capability_resolver.py:701` 先按 detected domain 过滤候选再排序）。再加一层"task-type 中层"在 95 技能下收益有限，属**中等价值锦上添花**。
- 真正高价值的是 **(B)**：OmicsClaw 当前**完全没有机器可读的跨技能依赖图**（`next_steps`/`## See also`/"Skip when" 全是 prose，`skill-lifecycle-redesign.md` §3.3 #3/#5），导致"先 QC 再比对"这类顺序、"复合 and-then 查询"、"前置不满足就别选"都甩给 LLM 自觉。
- **关键设计抓手**：依赖关系应当从 ADR 0037 已规定的 `interface.outputs`→`interface.inputs.preconditions` **生产者-消费者匹配，自动派生为一张带出处/置信度的「候选兼容图」**（人工校订后才采信），而非手写、更非让 LLM 每次现编。边须带语义档位（`required|optional|preferred|alternative`）——因为许多技能在缺失输入时会自动补算（如 `spatial-domains` 缺 `X_pca` 时自算）或按 `--method` 条件产出（如 BBKNN 不产 `X_bbknn`），纯硬匹配会误判。`summary.skip_when` 是**路由负信号、不是 DAG 依赖边**，单独处理（已有 `scripts/extract_skip_when_cases.py` 可复用）。该设计**复用** AgentSkillOS 的 DAG 调度骨架、**规避**其"边由 planner 每次现编"的硬伤——但**不夸大为"确定性事实"**（Codex 审阅后最重要的口径修正，见 §3.3 / 附录 C）。
- **先决条件（数据就绪）**未补齐前，任何检索器都会被钝化：13 个 singlecell 技能无 `trigger_keywords`、`validation_level` 95/95 inert、路由预算门因 `ceiling.json` 缺失而不可运行（§2.4）。这些是 Phase 0 的硬门槛。

推荐落地顺序：**Phase 0（就绪）→ Phase 1a/1b（声明式结构）→ Phase 2（派生候选兼容图）+ Phase 2a（数据状态探针）→ Phase 3（检索消费，penalty-first）**为主线；Phase 4（编排执行）紧随；Phase 5（dense 兜底）与 Phase 6（演化闭环）按评测增益决定取舍。

### 0.4 与既有文档的关系（不重复造轮子）
本文**直接延续**以下两处既有结论，只做增量与可执行化：
- `skill-lifecycle-redesign.md` §3.4 已列出：①可选 dense-embedding 兜底（P4 默认关）②Skip-when 结构化为排除信号 ③`next_steps` 升级为真实技能 DAG ④补 13 个 keyword-less 技能 ⑤路由 eval（precision@1/消歧率）⑥统一两个 close-tie 阈值。
- ADR 0037 已规定目标 schema：`interface.inputs.preconditions`（data-shape/env/config）、`interface.outputs`（files / result_json / anndata 契约）、结构化 `summary.skip_when`（`{condition, use, rationale}`）。
本文把"③+②+⑤"组织成一条**以 (B) 依赖/前置过滤为核心**的端到端工程线，并用三个外部 repo 为每一步提供"可借鉴机制"与"反面教训"的对照。

---

## 1. 三个参考系统的结构感知检索：机制与优缺点

### 1.1 对比总表
| 维度 | AgentSkillOS | SkillRL | GraphSkill |
|---|---|---|---|
| 库规模 | ~200k（10k active + 190k dormant） | 数十条/库（alfworld 12 general+6 类） | 1,869 条 NetworkX 函数文档（76 章 3 级） |
| 层级深度 | LLM 构建的 5 根能力树（多级） | **2 级**（general + 1 平铺类目） | 文档分类法 3 级（章→子类→函数） |
| 召回主路径 | LLM 粗到细树遍历（节点选择→技能选择→去重重排） | template：关键词→类目；embedding：**平铺** top-k | 4 法：TF-IDF/SBERT/LlamaIndex（**平铺**）+ LLM 粗到细 agent |
| 结构感知 (A) 层级 | ✅ 有（`_search_node` 递归 + 分支并行 + early-stop） | ⚠️ 仅 template 单层；embedding 抹平 | ✅ 仅 `retrieval_agent.py`（+ **多轮回溯**排除已探分支） |
| 结构感知 (B) 依赖 | ❌ 检索期无；DAG 边由 planner **每次 LLM 现编** | ❌ 无边 | ❌ 无边（纯包含，无 cross-link） |
| 技能 schema 富度 | 薄：`id/name/description/path/stars`，**无 category/inputs/outputs/prereq 字段** | 薄：`skill_id/title/principle/when_to_apply`，无边 | 无"技能对象"（=函数+docstring） |
| 编排 | ✅ 真 DAG：topo-sort/环检测/分层并行/失败级联跳过 | ❌ 无 | ❌ 无 |
| 检索质量评测 | ❌ **无** recall@k（仅端到端 Bradley-Terry 质量分） | ❌ 无独立指标 | ✅ **有**：按"必需函数覆盖"算 recall/precision/F1（与终任务解耦） |
| 演化闭环 | ❌ | ✅ 失败轨迹→o3→新技能，按类目成功率<阈值触发 | ❌ |

### 1.2 AgentSkillOS（最相关）
**机制**：检索是可插拔 manager（2 方法 Protocol，`src/manager/base.py:14`），共 6 条路径，主力是 `tree` 粗到细：
- `Searcher._search_node`（`src/manager/tree/searcher.py:245-348`）递归下树：中间节点按分支因子决定"全展开 vs LLM 选子类"（`NODE_SELECTION_PROMPT`，`prompts.py:281`，原则"不确定就多选"），选中兄弟分支**线程池并行**，单分支瘦子树触发 **early-stop** 直达叶；叶层 `SKILL_SELECTION_PROMPT`（`prompts.py:296`）选技能；末端 `_prune_skills`（`searcher.py:436`）按 `SKILL_PRUNE_PROMPT`（`prompts.py:424`）**去重 + 多样性重排**（"避免把功能相近的技能堆在前面"）。
- **200k 分层**（`config/config_skill_10000.yaml`）：10k active 进 LLM 树 + 190k dormant 进 ChromaDB；`LayeredSearcher.search`（`layered_searcher.py:177`）先树检索 active，再向量召回 dormant 并去重。
- **编排（真 DAG）**：`orchestrator/dag/engine.py` planner 生成 3 套候选 DAG（质量/效率/极简，`_generate_plans:435`），`graph.py` 提供 `topological_sort:97`、`detect_cycle:64`、`get_execution_phases:120`（按层并行）、`fail_node:195`（失败级联 SKIPPED）。

**优点**：①真正的 O(depth) 粗到细，把 200k 剪到个位数；②active/dormant 分层把"高频可解释"与"长尾海量"分治；③显式多样性重排是轻量"集合覆盖"代理；④检索/编排是独立注册表，热插拔。

**缺点（也是 OmicsClaw 要规避的）**：①**技能 schema 薄**——category 是 LLM 构建时塞进 `path` 字符串，**无 inputs/outputs/prerequisites 字段**，无法按"产出 BAM/需要比对后 reads"过滤；②**依赖边由 planner 每次 LLM 现编**（`depends_on` 是任务级幻觉，非库级事实）→ 生信里"align→sort→dedup→call"是确定性的，让 LLM 现编既贵又不稳；③**每个导航步都是 LLM 调用**→延迟/成本/不确定性随树深膨胀，粗分类选错会静默丢整棵子树；④**无检索 recall 指标**，检索质量只能间接由端到端质量分反推。

**可借鉴**：把 OmicsClaw 的 domain/task-type/algorithm 映射到树节点即可复用 `_search_node` 的下降逻辑；但**层级要声明式（OmicsClaw 本就有 8 硬编码 domain），并补齐 AgentSkillOS 缺的 typed 字段**；DAG 调度器（topo/phase/cascade-skip）可几乎照搬，但**喂给它库级派生边而非 planner 幻觉边**。

### 1.3 SkillRL
**机制**：静态 JSON 技能库（`memory_data/<env>/claude_style_skills.json`），结构 = `general_skills`（平铺）+ `task_specific_skills{category→[skill]}`（**单层类目**）。检索两模式（`agent_system/memory/skills_only_memory.py`）：template = 硬编码关键词→类目（`_detect_task_type:115`）返回整类；embedding = Qwen3-Embedding 余弦 top-k，但**对全体技能平铺**（`_embedding_retrieve:253`，跨类目，`task_type` 仅作标签）。每 episode 在 reset 时检索一次注入。演化：失败轨迹（score≤0，截 10 条）→o3 生成≤3 新技能（`skill_updater.py:44`），按 per-category 成功率<0.4 触发（`ray_trainer.py:878`）。

**优点**：①干净的技能记录 schema（`skill_id/title/principle/when_to_apply`）；②**预算感知注入**——动态技能必留、再用静态填满 `top_k`（`skills_only_memory.py:362`）；③**按类目成功率触发的失败→技能演化闭环**，对"按 task-type 监测科学成功率"有直接映射。

**缺点**：①**embedding 模式彻底抹平层级**（先平铺再排，`task_type` 只是装饰）——正是 OmicsClaw 要避免的"加了 dense 却丢了结构"；②**只有 2 级**，无算法层、无父指针；③**演化出的新技能一律塞进平铺 general 桶**（`add_skills(...,'general')`，`ray_trainer.py:931`），长期稀释结构；④硬编码 `if/elif` 关键词分类，脆；⑤**无依赖/前置边**；⑥schema 命名不一致导致 search 库的 task-specific 层被静默丢弃（`query_type_skills` vs `task_specific_skills`）——多 schema 库"静默掉一层"的真实事故。

**可借鉴**：技能记录 schema 与**预算感知注入**可直接用于"先注入最具体的 algorithm 层、再回填 task-type/domain/general"的结构感知 token 预算；演化闭环按 per-task-type 成功率触发，是 OmicsClaw §4「演化」缺失闭环的现成蓝本（本文 Phase 6）。**反面教训**：dense 检索必须**先按子树过滤再排序**，演化技能必须**回灌到正确层级节点**。

### 1.4 GraphSkill（命名澄清 + 唯一可借鉴点）
**实质**：图推理 benchmark（`complexgraph`/`gtools`），让 LLM 生成 NetworkX 代码或文本推理后比对答案。**全仓无技能库、无技能对象、无依赖边**；检索语料是 `data/networkx_graph_functions_docs.json`（NetworkX 文档树）。三条 embedding baseline（TF-IDF/SBERT/LlamaIndex）都 `flatten_repo()` **丢弃层级**。

**唯一结构感知点**：`utils/retrieval_agent.py` 的 LLM 粗到细文档树遍历（`traverse_documentation_one_round:471`、`retrieve_doc:657`）：章→子类→函数逐层 LLM 选择，叶层 Yes/No 相关性门 + 终选（`get_most_relevant_doc.py:9`），且**多轮回溯**——失败的顶层类目加入 `explored_initial_categories` 并在下一轮 prompt 中排除（`retrieval_agent.py:743`）。另有一条**与终任务解耦的检索评测**：按"是否覆盖必需 NetworkX 函数名"算 recall/precision/F1（`utils/retrieval_eval_utils.py:25`）。

**可借鉴**：①**回溯纠错**——粗分类（domain detection）选错时，不要静默丢子树，而是排除该分支后重选（直接对应 OmicsClaw "domain 检测错→正确技能不可达"的风险，§2.2 的置信门可在低置信时触发"放宽/回溯"而非直接 no_skill）；②**检索质量独立评测**模板（GraphSkill 是三系统里**唯一**有 recall 指标的，正好补 AgentSkillOS 的空白）。**与 (B) 依赖过滤无关**——GraphSkill 这块零贡献。

### 1.5 三者共同的"反面教训"（什么不要学）
1. **不要"加了 dense 就丢结构"**（SkillRL embedding、GraphSkill 三 baseline 都 flatten）→ OmicsClaw 的 dense 兜底必须**先按 domain/task-type 子树过滤再排序**。
2. **不要让依赖边由 LLM 每次现编**（AgentSkillOS planner）→ 应从 `inputs/outputs` 契约**派生为带置信度/出处的候选兼容图**（人工校订）。注意生信前置**并非全确定**：技能常自动补算缺失输入、或按方法/参数条件产出，故派生边须带 `required|optional|preferred|alternative` 语义，不可当铁律硬过滤。
3. **不要让技能 schema 太薄**（三者都缺 typed inputs/outputs/prereq）→ OmicsClaw 必须先落 ADR 0037 的 `interface`，结构感知才有"可过滤的字段"。
4. **不要无检索指标**（AgentSkillOS/SkillRL 都没有）→ 借 GraphSkill 的解耦评测，先建 precision@1/recall@k 再谈优化（与 §3.4 ⑤一致）。

---

## 2. OmicsClaw 现状映射（结构感知视角）

### 2.1 层级现状："3 级"只是概念，实体只有 2 级
- **L1 组学域**：真实目录层 `skills/<domain>` + registry 域节点（8 域硬编码，`registry.py:452 _HARDCODED_DOMAINS`），加载器支持对**任意** domain 的一层通用 subdomain 嵌套（`registry.py:88`，目前仅 `singlecell/scrna|scatac` 用到）。✅ 干净。
- **L2 任务类型**：**非统一层**——编码在技能 slug 命名习惯里（`bulkrna-de`/`bulkrna-qc`/`sc-clustering`），**无机器可读 `task_type` 字段**，registry 把每个叶当 domain 的平铺子节点。
- **L3 任务算法**：塌缩进技能内部的 `--method` flag，声明在 `parameters.yaml` `param_hints`（如 `sc-batch-integration` 的 `harmony/scvi/scanvi/bbknn/...`），**非目录、非 registry 节点**。

> 结论：磁盘上实为 `domain → skill`，算法层在 `param_hints` 里。"3 级层级"是概念模型，**仅部分物化**。机制 (A) 要落地，第一步是**把 task_type 物化为字段**（见 Phase 1）。

### 2.2 当前检索：生成式上层 + 稀疏 resolver（已是 domain-first 粗到细）
- **混合双路**（沿用 `skill-lifecycle-redesign.md` §3.2 的再定性，不重复）：LLM 在 95 项 alias 枚举约束下直接 emit `skill='<alias>'`（生成式召回）；默认推荐 `skill='auto'` 交给 `capability_resolver` 手调稀疏召回。**路由路径无任何 dense/vector 检索**（grep `embedding|faiss|bm25|tfidf|cosine|vector` 命中为零）。
- **resolver 本就粗到细**：`_score_skills_and_detect_domain`（`capability_resolver.py:461`）单遍同时检测 domain 并打分；`resolve_capability`（`:637`）**先按 detected domain 过滤候选**（`:701 candidates=[c for c in all_candidates if c.domain==domain]`）再排序（字母序 tie-break `:711`）。打分权重（`_candidate_score:572`）：alias 命中 +12、legacy +9、描述词重叠 +0.85/词（cap 8）、trigger 关键词长度加权 1.5–4.5（≤3 个）、param-hint method 命中 +3。阈值（`:88-111`）：top1<3.0 → no_skill；close-second gap 1.5。
- **置信门控消歧**：auto 路径 top1−top2<2.0 拒跑返回 top3（`agent_executors.py:306`，`_AUTO_DISAMBIGUATE_GAP`，`orchestration.py:151`）。注意 resolver 内部用的是另一个 1.5（`_RESOLVE_CLOSE_SECOND_GAP`）——两阈值不一致（§3.4 ⑥/本文 Phase 0）。
- **渐进披露**：常驻 8 域 briefing（`domain_briefing.py:49`，~300–786 token）+ 95 alias 枚举；不足时 `list_skills_in_domain`（`listing.py:53`）翻页；选定后注入 SKILL.md body（`orchestration.py:803`，**8000 字符硬截断**漂移风险）。
- **治理**：21 条黄金快照守 re-rank（`tests/test_capability_resolver_golden.py`），改权重有回归网。

> 含义：机制 (A) 的"domain 粗分"**已存在且确定性**（比 AgentSkillOS 全 LLM 的导航更省更稳）。要补的是 **task-type 中层**（中价值）和**低置信回溯**（借 GraphSkill，避免选错 domain 静默丢技能）。

### 2.3 依赖/前置现状：无机器可读跨技能图，但"半成品边"已在 ADR 0037 里
当前**无 registry 级依赖 DAG**。已有的"边的原材料"：
- 每技能前置信号：`parameters.yaml` `requires_preprocessed: bool`（注入提示 `runtime/context/layers/__init__.py:376`）、`saves_h5ad: bool`（产出状态提示）。
- `param_hints[method].requires`：**算法级**前置（混 data-state 与包，如 `existing_PCA_or_computeable_PCA`/`labels_in_obs`/`scvi`），非 skill ID。
- **1 条声明式 pipeline**：`pipelines/spatial-pipeline.yaml`（preprocess→domains→de→genes→statistics，靠 `processed.h5ad` 串联）——唯一一等链。
- 脚本 emit `next_steps`（**仅文本，从不被遍历**）。可复现口径：`grep -rl next_steps skills/**/*.py` 命中 39 个 `.py`，其中经 `registry.iter_primary_skills()` 的**主技能脚本 31 个**（与 `skill-lifecycle-redesign.md:192` 的 39 口径一致，差异在是否计 `_lib`/非主脚本——以后引用统一用此命令口径）。`## See also` prose 命名上下游；description 的 "Skip when…use X instead" 是**跨技能负信号但仅 prose**，resolver 做整描述词袋重叠，**从不解析为排除信号**（已有 `scripts/extract_skip_when_cases.py` 抽取器/测试面可复用）。
- preflight 引擎只有 1 个技能（`preflight/sc_batch.py`），`__init__.py` 自承"通用引擎尚不存在"。

**关键机会（及其边界）**：[ADR 0037](../adr/0037-unified-declarative-skill-representation.md) 的 `skill.yaml v2` **已规定**：`interface.inputs.preconditions`（`data_shape`/`env`/`config` 三类，`ADR 0037:102`）、`interface.outputs`（`files`/`result_json.required_keys`/`anndata.{obs,obsm,var}` 契约，`:113`）、结构化 `summary.skip_when`（`{condition, use, rationale}`，`:96`）。**这是派生「候选兼容图」的原料**：A 技能 `outputs.anndata.obsm:[X_pca]` 可满足 B 技能 `inputs.preconditions.data_shape.obsm:[X_pca]` → 生成 A→B 候选边；`skip_when.use` → 路由负信号（**不入 DAG**）。**但 ADR 0037 只定义了字段、不足以从裸匹配推出可靠 DAG**（Codex must-fix #2）：技能会自动补算缺失输入（`spatial-domains/SKILL.md:80,90`）、按方法条件产出（BBKNN 不产 `X_bbknn`，`sc-batch-integration/SKILL.md:56,101`）、或只需"某个分组列"而非固定上游（`sc-markers/SKILL.md:43,65`）。故派生结果是**带置信度/出处、需人工校订的候选图**，不是确定性事实——这是本计划相对三个 repo 的核心增量，也是必须谨慎对待之处。

### 2.4 数据就绪缺口（会钝化任何检索器，Phase 0 硬门槛）
- **13 个 singlecell 技能无 `trigger_keywords`**（sc-cell-annotation/sc-clustering/sc-markers 等），经 `skill='auto'` 近乎不可达（§3.3 #2）。
- **`validation_level` 95/95 停在 smoke-only 且检索期从不被读**（`lazy_metadata.py:36` 串入但 inert）。
- **路由预算门不可运行**：`check_routing_budget.py` 因 `build/routing-baselines/ceiling.json` 缺失 exit 2。
- **两个不一致的 close-tie 阈值**（2.0 vs 1.5）。
- ADR 0037 schema 尚未落地（95 技能仍是 `SKILL.md` frontmatter + `parameters.yaml` 双源）。

---

## 3. 设计论证：OmicsClaw 该要哪种"结构感知"

### 3.1 把 (A) 与 (B) 分开估值
| 机制 | 在 OmicsClaw 的现状 | 95 技能下的边际价值 | 主要收益场景 |
|---|---|---|---|
| **(A) 层级粗到细** | domain 粗分**已有**且确定性；task-type 层缺失 | **中**（domain 过滤已吃掉大部分收益；补 task-type 主要利好 singlecell 34 技能的密集子域） | 大域内细分（singlecell/spatial）、库继续增长（knowledge_base 28 工作流待迁入） |
| **(B) 依赖/前置过滤** | **完全缺失**（全 prose） | **高** | 正确性（前置不满足就别选/先 QC 再比对）、复合 "and then" 查询、消歧（skip_when 负信号，非 DAG 边） |

### 3.2 关键判断
1. **(A) 不是瓶颈**：resolver 已 domain-first；硬塞一个全 LLM 的多级树（AgentSkillOS 式）会把当前**确定性、可快照、零额外 LLM 调用**的优势换成"每步一次 LLM、选错静默丢子树"的成本，在 95 技能下不划算。**task-type 应作为确定性过滤的一个新维度，而非新增 LLM 导航层**。
2. **(B) 是真正的缺口与价值所在**：它直接命中三类现实痛点——顺序正确性、复合查询、跨技能消歧——且**地基（ADR 0037 interface）已规划**。
3. **dense 只是兜底**：与 `skill-lifecycle-redesign.md` §3.4 一致，dense-embedding 是 **P4 默认关**的 no_skill/破平局救援，且必须**先按子树过滤再排序**（规避 SkillRL/GraphSkill 的 flatten 教训）。

### 3.3 核心设计：把依赖关系「派生为带置信度的候选兼容图」（非手写、非 LLM 现编）
```
ADR 0037 interface 契约            派生规则                  候选兼容图（人工校订后采信）
────────────────────         ─────────────          ─────────────────────────────
A.outputs.anndata.obsm:[X_pca]  ─(A 产出可满足 B 前置)─▶  candidate edge  A ──(A 是 B 的上游)──▶ B
B.inputs.preconditions.data_shape                        每条边携带：
   .obsm:[X_pca]                                          · edge_kind: required | optional | preferred | alternative
A.outputs.files:[processed.h5ad]                          · 出处: matched_output_key / matched_precondition_key
   + B.requires_preprocessed:true                         · confidence + reviewed(bool)
                                                          · condition_scope: 仅当 B 的 --method ∈ {...}
── skip_when 不入此图 ──  B.summary.skip_when:[{use:sc-de}] ─▶ 路由负信号（resolver 消费，extract_skip_when_cases.py 维护）
```
> **Topo 约定**：边 `A→B` 表示"A 应在 B 之前"（A 是 B 的上游）；执行序由 `topological_sort` 给出。（修正 v0.1 图中 `A──requires──▶B` 的方向标注——producer 不"requires"consumer，Codex must-fix #1。）

- **建图**：纯函数 `omicsclaw/skill/skill_dag.py::build_skill_dag(registry)` 扫所有 `interface` 做产出→前置匹配，输出**候选边**（每条带 `edge_kind`/出处/`confidence`/`reviewed`/`condition_scope`），落生成物 `skills/skill_dag.json`（`--check` 防漂移，复用 catalog/INDEX 的生成-校验范式）。**`skip_when` 不进此文件**——它是路由负信号，归 `extract_skip_when_cases.py` 维护、由 resolver 消费（Codex should-improve #2）。
- **边语义分档**（Codex must-fix #2，避免把"软依赖"当铁律）：`required`（缺则一定失败）/`preferred`（缺则自动补算但更慢更糙，如 `spatial-domains` 自算 `X_pca`）/`optional`/`alternative`（如 `scanvi` 可回退 `scvi`）。**默认不硬过滤**——见 §3.4 与 Phase 3 的 penalty-first。
- **借** AgentSkillOS 的 `graph.py`：`detect_cycle`/`topological_sort`/`get_execution_phases`/`fail_node` 照搬，但**输入是带档位的候选边**。
- **规避** AgentSkillOS 硬伤、且不矫枉过正：边是**库级派生 + 人工校订**的可治理产物（非 planner 每次幻觉、非纯手写、不会与 interface 真源漂移），但**不夸大为"确定性事实"**；`pipelines/spatial-pipeline.yaml` 只能作**兼容性 smoke test**（部分步骤是并行/相邻而非严格依赖，`spatial-domains/SKILL.md:130`），不是"派生图科学正确"的证明（Codex must-fix #5）。

### 3.4 检索期如何消费结构（resolver API 扩展 + penalty-first）
**注意：这不是"在排序前做一次本地 splice"就能完成的**（Codex must-fix #3）。当前 `CapabilityCandidate` 只带 `(skill, domain, score, reasons)`（`capability_resolver.py:318`），domain 检测只返回 `(best_domain, candidates)` 而**不含 domain margin/置信度**（`:461`），`resolve_capability` 入参只有 `query/file_path/domain_hint`（`:637`）。因此需先做 **resolver API 扩展**：①让候选携带 `task_type`/precondition 评估/skip 命中；②暴露 domain margin 以支持回溯；③注入"当前数据状态"（见 Phase 2a）。在此之上增加以下消费，**默认全为软信号（降权 + 解释），经 eval 证明安全后才把特定项升级为硬过滤**：

1. **task_type 软分区**（机制 A，中价值）：作为**排序/分区软特征，非硬过滤**——硬过滤会伤多意图/相邻任务（如 `sc-de` vs `sc-markers`，Codex should-improve #4）。
2. **skip_when 负权**（机制 B）：命中 skip-target 的候选降权（消费 `extract_skip_when_cases.py` 抽取结果，**不走 DAG**）。
3. **precondition 软过滤**（机制 B，核心）：候选 `inputs.preconditions.data_shape` 在**当前数据状态**（Phase 2a 探测）下不可满足时降权 + 给出解释；**仅当某前置被 eval 证明"不满足=必失败"才升级为硬剔除**（如 `sc-clustering` 缺 embedding 源会硬失败，但仍可用非默认 `--use-rep`，`sc-clustering/SKILL.md:66,77`——正是不能一刀切的例子）。
4. **低置信回溯**（借 GraphSkill）：domain margin 低或过滤后为空时，**排除该 domain 重选/放宽跨域**，而非直接 no_skill（对应 `retrieval_agent.py:743` 的 `explored_initial_categories` 排除重试）。
5. **复合查询走候选图**（机制 B）：含 "and then"/多阶段意图时返回候选图上的一条 topo 链，交 Phase 4 runner（人工确认后执行）。

> 设计原则：**结构感知在 OmicsClaw = 给确定性稀疏 resolver 增加"结构维度的软信号 + 候选兼容图"，默认 penalty-first、经 eval 才升级硬过滤**，而非用 LLM 树遍历替换它。这保住现有的确定性、黄金快照可治理、零额外 LLM 成本三大优势，同时不把派生边当铁律。

---

## 4. 分期优化计划

> 每期含：目标 / 借鉴来源 / 改动点（`file:line`）/ 风险 / 验收 / 工作量。**Phase 0 是决策门**：其基线指标决定是否继续。

### Phase 0 — 基线与就绪（决策门）｜工作量 S｜优先级 高
**目标**：先量化"现状到底缺多少"，并补齐会钝化检索器的数据缺口。无此期，后续优化无法证明净收益。
- 借鉴：GraphSkill 解耦检索评测（`retrieval_eval_utils.py:25`）。
- 动作：
  - 建标注路由 eval 语料（跨 7 分析域 query→expected_skill(s)），扩 `scripts/run_eval.py` 输出 **precision@1 / top-3 recall / 消歧触发率**（= §3.4 ⑤）。
  - 收集运行 trace：统计 alias-direct vs auto-resolve 实际占比（§3.2 悬而未决的"主路径"问题），并量化**枚举约束解码的幻觉别名率**。
  - 补 13 个 keyword-less 技能 `trigger_keywords` + `skill_lint.py` 加"空 trigger 即失败"规则（= §3.4 ④）。
  - 生成并提交 `ceiling.json`，把 `check_routing_budget.py` 接入 CI（= §3.4 ⑥前置）。
  - 统一两个 close-tie 阈值（2.0 vs 1.5）。
- 风险：标注语料需领域人力；trace 需有真实使用日志。
- 验收：eval 可重复产出 baseline 数值；CI 预算门转绿；13 技能可达性 eval 命中率显著上升。
- **决策门**：若 baseline precision@1 已很高且复合查询/前置错误占比低 → (A)/(B) 收益有限，可仅做 Phase 1 的 schema 收尾而暂缓 (A)。

**2026-07-13 落地状态（Phase 0 retrieval truth 已验收）：**

- `tests/fixtures/routing_oracle/v1.json` 固化 24 个预期行为 case，每个 8-domain 至少 3 条，
  并覆盖 route/no-skill boundary；fixture validation 拒绝未知域、非 canonical alias、域错配、
  重复 ID 和缺失阈值。
- `omicsclaw.skill.routing_oracle` 与 `scripts/evaluate_routing_oracle.py` 输出 precision@1、
  top-3 recall、domain accuracy、decision accuracy、hallucinated alias rate，以及逐域同类指标；
  全局或任一域低于门槛均非零退出，已接入 PR CI。
- 当前 v1 结果：全局五项均 `1.000`（alias hallucination `0.000`），8 域逐域 top1/top3/
  domain/decision 均 `1.000`。该数值只描述 26-case 人工 oracle，不外推为真实流量 100%。
- resolver 的相应根因修复包括：取消硬编码分析词表的前置误杀、domain-size 累加偏置、
  泛化 legacy alias 跨域抢占；增加显式 domain/task/control-plane 软信号，并把 literature
  自身的检索请求判为 exact coverage。

Phase 0 完成不代表结构检索完成：RET-04 precondition penalty 与 RET-05 candidate DAG/topo
chain 仍是下一主线，dense retrieval 仍不应提前启用。

### Phase 1 — 声明式结构落地（拆 1a 字段 / 1b 内容质量）｜工作量 M｜优先级 高
**目标**：把"结构感知需要的可过滤字段"真正物化——三个 repo 都缺、OmicsClaw 必须先有的地基。
- 借鉴：AgentSkillOS/SkillRL 的**反面教训**（schema 太薄 → 无可过滤字段）。
- **Phase 1a — schema 字段先行**：落 ADR 0037 `skill.yaml v2` 的 `interface.inputs.preconditions`（data_shape/env/config）、`interface.outputs`（files/result_json/anndata 契约）、结构化 `summary.skip_when`，及**新增 `task_type` 字段**（L2 物化，从 slug 习惯 + 人工校订生成：`qc`/`differential-expression`/`clustering`/`integration`/`enrichment`…）。`lazy_metadata.py` 扩 `_RUNTIME_FIELDS`（`:14`）；`skill_lint.py`（`:40/:44`）扩 schema 校验。
- **Phase 1b — interface 内容质量（不可机械迁移，Codex should-improve #3）**：从既有信号草拟 `requires_preprocessed`/`saves_h5ad`/`param_hints[*].requires` → preconditions、`## See also`/`next_steps`/"Skip when" → outputs/skip_when，**但必须人工校订**——现有元数据与 prose 已有不一致（如 `spatial-domains` 的 `parameters.yaml:181` 写 `requires_preprocessed: false`，而 `SKILL.md:40` prose 称需预处理输入）。此步是派生图质量的真正瓶颈：机械迁移会把错误元数据固化成假边。
- 风险：95 技能迁移工作量；ADR 0037 仍 draft（需先定稿）；过早 over-model（守 "no consumer, no bucket"）。
- 验收：95 技能含校验通过的 `interface`+`task_type`；1b 产出"prose↔元数据"一致性审计报告；drift 检查（`check_description_drift.py` 范式）守新字段。

### Phase 2 — 派生候选兼容图（producer-consumer）｜工作量 M｜优先级 高
**目标**：把"半成品边"自动编译成 registry 级**候选兼容图**（机制 B 的图），每条边带语义档位/出处/置信度/review 标记。
- 借鉴：AgentSkillOS `graph.py`（`topological_sort:97`/`detect_cycle:64`/`get_execution_phases:120`/`fail_node:195`）——照搬算法，喂派生候选边。
- 动作：
  - 新增纯函数 `omicsclaw/skill/skill_dag.py::build_skill_dag(registry)`：扫 `interface` 做产出→前置匹配，输出候选边（含 `edge_kind: required|optional|preferred|alternative`、`matched_output_key`、`matched_precondition_key`、`condition_scope`（`--method` 作用域）、`confidence`、`reviewed: bool`，Codex should-improve #5），落生成物 `skills/skill_dag.json`（`--check` 防漂移）。**`skip_when` 不进此文件**（路由负信号，归 `extract_skip_when_cases.py`）。
  - registry 暴露图查询（上游闭包/下游/topo 排序）；`generate_catalog.py` emit 图摘要。
  - 用 `pipelines/spatial-pipeline.yaml` 作**兼容性 smoke test**（非"科学正确"证明）：派生图须与之**兼容**（顺序边不冲突），但允许派生图多出并行/相邻关系（`spatial-domains/SKILL.md:130` 称 `spatial-genes` 为并行）。
- 风险：interface 契约不完整或与 prose 不一致 → 漏连/误连（spatial pipeline 当回归锚 + 人工 review 高置信边）；环（`detect_cycle` 拦下报警）；**把 preferred/optional 当 required 会过约束**。
- 验收：派生图与 spatial pipeline 兼容；DAG 单测（给定 interface → 期望候选边集 + 档位）；高置信边人工 review 抽样准确率达标。

### Phase 2a — 数据状态探测与缓存（precondition 过滤的前置）｜工作量 S–M｜优先级 高
**目标**：precondition 过滤需要"当前数据的实际状态"，但 resolver 现仅收 `query/file_path/domain_hint`（`capability_resolver.py:637`），且通用 preflight 引擎尚不存在（`preflight/__init__.py:6`，仅 `sc_batch.py:11` 一个消费者）。**Codex must-fix #4 指出这是被遗漏的前置**。
- 动作：实现轻量**数据状态探针**——读取 workspace 目标 `.h5ad`/上游产出的 `obs/obsm/uns/layers` 键（AnnData backed/只读 header + 缓存，避免每次全量加载），把"已有 / 可补算"状态喂给 resolver（Phase 3）与编排（Phase 4）。这也是 §4.4 "通用 preflight 引擎"的首个落地。
- 风险：大文件探测成本（backed 模式 + 缓存）；状态随上游运行变化（缓存失效策略）。
- 验收：给定一个 `.h5ad`，探针正确报告 `obsm` 等键集；缓存命中/失效有测试；`sc_batch` 改为该引擎的消费者之一。

### Phase 3 — 结构感知检索消费（resolver API 扩展 + penalty-first）｜工作量 M–L｜优先级 高
**目标**：让 resolver 用上 task_type / skip_when / precondition / 候选图（§3.4 的 1–5）。
- 借鉴：GraphSkill 回溯（`retrieval_agent.py:743`）；AgentSkillOS 多样性重排（`SKILL_PRUNE_PROMPT`）作可选去冗。
- 动作（**非局部 splice，需改 resolver API**，Codex must-fix #3）：扩 `CapabilityCandidate`（`:318`）携带 task_type/precondition 评估/skip 命中；让 domain 检测（`:461`）暴露 margin 以支持回溯；给 `resolve_capability`（`:637`）注入 Phase 2a 数据状态。在此之上加：task_type 软分区、skip_when 负权（新增 `_SCORE_SKIP_PENALTY`）、precondition **软降权 + 解释（默认不剔除）**、低置信回溯、复合查询返回 topo 链。新增权重提升为命名常量（沿用 `_SCORE_*` 风格）。
- 风险：改 re-rank 动黄金快照——**必须先扩 Phase 0 eval 与黄金快照再改**；precondition 硬过滤会误杀自动补算/可选输入的技能（默认软、经 eval 才个别升级硬）。
- 验收：Phase 0 eval 的 precision@1/消歧率/前置软信号正确率较 baseline 改善且无黄金快照回归；"复合查询→正确 topo 链"用例通过；"valid-without-upstream"负例不被误剔（见 §5）。

### Phase 4 — 依赖感知编排（泛化 pipeline runner）｜工作量 M｜优先级 中
**目标**：把 DAG 真正执行——topo 有序、按层并行、失败级联跳过。
- 借鉴：AgentSkillOS `engine.py` 执行模型（`get_execution_phases` 分层 + `ExecutionThrottler` + `fail_node` 级联）。
- 动作：泛化 `pipelines/` runner（`chain.py`）为消费派生候选图的执行器，跑 LLM 确认过的链（确认后即为可执行 DAG）；保留人工确认门（生信任务多分钟、不可逆，**不自动连跑**）。
- 风险：多分钟/不可逆任务的自动编排安全性——**必须人工确认 + 覆盖守卫**（与既有"覆盖前告警"一致）。
- 验收：spatial pipeline 经派生 DAG 端到端跑通；失败级联跳过行为有测试。

### Phase 5 — dense-embedding 兜底（实验性，默认关）｜工作量 S–M｜优先级 P4（先验证）
**目标**：救回 no_skill / 破子阈值平局，**不替换**稀疏主路径。
- 借鉴：**SkillRL/GraphSkill 的反面教训**——dense 必须**先按 domain/task_type 子树过滤再排序**，绝不 flatten。
- 动作：完全沿用 `skill-lifecycle-redesign.md` §3.4 第一行的铁律——仅本地 embedding 后端、默认 no-op、查询脱敏、缓存限 workspace-local；在 `_candidate_score` 加 `_SCORE_SEMANTIC_*`，**仅**用于 no_skill/平局救援；**须先用 Phase 0 eval 证明净收益再启用**。
- 风险：本地 embedding 依赖/隐私；无净收益则不启用。
- 验收：在 no_skill/平局子集上 recall 提升且不伤 precision@1。

### Phase 6 — 失败→技能演化闭环（远期，可选）｜工作量 L｜优先级 低
**目标**：补 `skill-lifecycle-redesign.md` §4 缺失的演化闭环。
- 借鉴：SkillRL 演化（`skill_updater.py:44` + 按 per-category 成功率<阈值触发 `ray_trainer.py:878`）——映射为**按 per-task_type 科学成功率**触发候选 Gotcha/技能草拟。
- 动作：与 §4.4 "失败→技能反馈闭环"合并实现（`result.py` 加 `error_kind` → `index.jsonl` 聚合 → 候选 Gotcha 须人工批准）。**反面教训**：演化产物必须**回灌到正确 task_type/algorithm 节点**，不可学 SkillRL 全塞 general 桶。
- 风险：自动改技能库的安全性——**仅产候选、强制人工批准**。
- 验收：失败聚合产出 per-skill 健康台账 + 候选 Gotcha（带 stderr/result key/file:line 证据）。

---

## 5. 评测方法（贯穿全程，借 GraphSkill 解耦思想）
- **检索质量（与科学正确性解耦）**：precision@1、top-3 recall、消歧触发率、**前置过滤正确率**（precondition 该剔的剔了/该留的留）、**复合查询 DAG 正确率**（topo 链与人工 pipeline 一致）。
- **语料**：跨 7 分析域的 (query → expected_skill(s) / expected_chain)；含"前置不满足"负例、"and then"复合正例，以及 Codex optional #3 强调的 **"无上游也合法"负例**——precondition 过滤最易误杀这些：`spatial-domains` 缺 `X_pca` 自算（`spatial/spatial-domains/SKILL.md:80`）、`scanvi` 可回退 `scvi`、用户自带 obs 列即满足 marker/DE 前置。这些用例专门压测"过激 precondition 过滤"。
- **回归网**：扩 `tests/test_capability_resolver_golden.py`（21→更多）+ 新增 DAG 单测 + skip_when/precondition 用例。
- **门控**：CI 跑 eval 阈值 + 路由预算门（`ceiling.json`）+ schema/drift 校验。
- **三系统对照教训**：AgentSkillOS/SkillRL 无检索指标→盲优化；本计划坚持"先指标后优化"。

## 6. 风险、反对意见与边界（honest）
1. **过度工程 vs 95 技能**：机制 (A) 在 95 技能下 ROI 有限——**故本计划把 (A) 降级为"确定性 task-type 过滤维度"，不引入全 LLM 树遍历**。若 Phase 0 显示 baseline 已足够好，(A) 可暂缓。
2. **LLM 调用成本/延迟**（AgentSkillOS 教训）：本计划的结构消费**全确定性、零额外 LLM 调用**，仅复合查询/低置信回溯可能多一次确认。
3. **schema 迁移风险**：ADR 0037 仍 draft；95 技能迁移有工作量。缓解——自动迁移 + 人工校订 + drift 门 + "no consumer, no bucket"。
4. **派生边不可靠/被夸大**：interface 契约缺失或与 prose 不一致会漏连/误连，且技能自动补算/方法条件产出使"硬依赖"假设站不住。缓解——派生结果定位为**带置信度的候选兼容图（非确定性事实）**、人工 review 高置信边、spatial pipeline 仅作兼容性 smoke test、precondition **默认软过滤（penalty-first）**、专设"无上游也合法"负例集回归。
5. **何时 NOT 做**：若库长期稳定在 ~95 且复合/前置错误占比低（Phase 0 数据），则只做 Phase 1（schema 收尾）+ §3.4 ④⑥（数据就绪），暂缓 Phase 2–6。
6. **安全边界**：依赖感知编排涉及多分钟/不可逆生信任务，**禁止自动连跑**，强制人工确认 + 覆盖守卫；演化闭环**仅产候选、人工批准**。
7. **Phase 3 非局部改动**（Codex must-fix #3）：低置信回溯与 precondition 消费需扩 resolver API（候选结构 / domain margin / 数据状态注入），不是排序前一处 splice——工作量按 M–L 计，须先扩 eval/黄金快照再改。

## 7. 决策建议与路线图
- **主线（推荐）**：Phase 0（就绪+基线，决策门）→ Phase 1a/1b（声明式 interface + task_type，含内容质量校订）→ Phase 2（派生候选兼容图）+ Phase 2a（数据状态探针）→ Phase 3（resolver 结构消费，penalty-first）。这条线把"结构感知"落在**可治理、软信号优先、零额外 LLM 成本**的轨道上，每步都有既有文档/repo 背书。
- **紧随**：Phase 4（编排执行，带安全门）。
- **按需**：Phase 5（dense 兜底，先验证净收益）、Phase 6（演化闭环，与 §4.4 合并）。
- **总判断**：**结构感知检索适合 OmicsClaw，但价值在依赖/前置 (B) 而非层级细分 (A)**；约 70% 地基（ADR 0037 interface、§3.4 路线）已在规划内，本计划主要是"把它编译成**带置信度的候选兼容图**，并让 resolver 以 **penalty-first** 方式消费"——刻意不把派生边当确定性事实，是 Codex 审阅后最重要的口径修正。

---

## 附录 A：三 repo 关键证据索引
**AgentSkillOS**：`src/manager/base.py:14`（Protocol）；`src/manager/tree/searcher.py:245`（`_search_node`）`:350`（选子类）`:436`（prune 重排）；`src/manager/tree/prompts.py:281/296/424`（节点/技能/prune prompt）；`src/manager/tree/layered_searcher.py:177`（active+dormant）；`src/manager/tree/dormant_searcher.py:96`；`src/orchestrator/dag/engine.py:435`（生成 3 DAG）；`src/orchestrator/dag/graph.py:64/97/120/195`（环检/topo/分层/级联跳过）；`src/manager/tree/models.py:119`（薄 Skill）；`config/config_skill_10000.yaml`（10k+190k 分层）。
**SkillRL**：`agent_system/memory/skills_only_memory.py:115`（`_detect_task_type`）`:253`（平铺 embedding）`:362`（预算注入）；`agent_system/memory/skill_updater.py:44`（演化）；`verl/trainer/ppo/ray_trainer.py:878/931`（成功率触发 / 塞 general 桶）；`memory_data/*/claude_style_skills.json`（2 级库）。
**GraphSkill**：`utils/retrieval_agent.py:471/657/743`（粗到细遍历 / retrieve / 回溯排除）；`utils/generation_functions/retrieve_doc_chapter.py:64`、`get_most_relevant_doc.py:9`；`utils/tfidf_retrieval.py`（flatten baseline）；`utils/retrieval_eval_utils.py:25`（解耦 recall/precision/F1）；`data/networkx_graph_functions_docs.json`（文档树语料）。

## 附录 B：OmicsClaw 关键证据索引
检索：`omicsclaw/skill/capability_resolver.py:318`（`CapabilityCandidate` 只带 skill/domain/score/reasons）`:461`（检测+打分，返回 `(best_domain, candidates)` 无 margin）`:572`（权重）`:637`（resolve，入参仅 query/file_path/domain_hint）`:701`（domain 过滤）`:711`（排序）`:88-111`（阈值）；`omicsclaw/runtime/tools/builders/agent_executors.py:306`（消歧）；`omicsclaw/skill/orchestration.py:151/803`（gap / load body）。层级与元数据：`omicsclaw/skill/registry.py:88/184/337/361/452`；`omicsclaw/skill/lazy_metadata.py:14/30/36`；`omicsclaw/skill/domain_briefing.py:49`；`omicsclaw/skill/listing.py:53`。依赖/前置：`parameters.yaml`（`requires_preprocessed`/`saves_h5ad`/`param_hints[*].requires`）；`runtime/context/layers/__init__.py:376`；`pipelines/spatial-pipeline.yaml:19`；`omicsclaw/skill/preflight/__init__.py:6`（自承通用引擎未建）+ `sc_batch.py:11`；`scripts/extract_skip_when_cases.py`（已有 skip 抽取面）。Codex 核验的"边失效"反例：`skills/spatial/spatial-domains/SKILL.md:80,90,130,40`、`skills/singlecell/scrna/sc-batch-integration/SKILL.md:56,101`、`skills/singlecell/scrna/sc-markers/SKILL.md:43,65`、`skills/singlecell/scrna/sc-clustering/SKILL.md:66,77`、`skills/spatial/spatial-domains/parameters.yaml:181`。结构地基：[ADR 0037](../adr/0037-unified-declarative-skill-representation.md) `interface.inputs.preconditions` / `interface.outputs` / `summary.skip_when`；[ADR 0030](../adr/0030-first-class-skill-type-system.md)。既有路线：[`skill-lifecycle-redesign.md`](./skill-lifecycle-redesign.md) §3.3/§3.4/§4.4。治理：`tests/test_capability_resolver_golden.py`；`scripts/{run_eval,generate_catalog,check_routing_budget,check_description_drift,skill_lint}.py`。

## 附录 C：Codex 交叉审阅记录
> Codex（gpt-5.5, xhigh）于 2026-06-30 对 v0.1 草案独立审阅（717s），在 live tree `/work/zhouweige_data/project/OmicsClaw`（与 `/home/weige/project/OmicsClaw` 同一 checkout）逐条核对 `file:line`。原文净评估：*"Core resolver citations are mostly correct, but the design needs several fixes before it is safe to treat as an implementation plan."* 下列条目均已纳入 v0.2 正文。

### C.1 必须修正（must-fix，全部已应用 ✅）
1. ✅ **派生图方向标注反了**：v0.1 图中 `A.outputs… → edge A──requires──▶B` 暗示 producer requires consumer，对 topo 执行语义错误。→ 已改为 `A→B = A 是 B 的上游` 并显式定义 topo 方向（§3.3）。
2. ✅ **"派生依赖是确定性事实"太强**：ADR 0037（`:102`/`:113`）只定义字段，不足以从裸匹配推可靠 DAG。反例：`spatial-domains` 缺 `X_pca` 自算（`spatial-domains/SKILL.md:80,90`）、BBKNN 不产 `X_bbknn`（`sc-batch-integration/SKILL.md:56,101`）、`sc-markers` 只需"某分组列"（`sc-markers/SKILL.md:43,65`）。→ 全文改为**带 provenance/confidence + `required|optional|preferred|alternative` 语义的候选兼容图**（§3.3/§0.3/§1.5/§2.3）。
3. ✅ **Phase 3 不能"在 :700 与 :711 间 splice"**：该处只有 `CapabilityCandidate(skill,domain,score,reasons)`（`capability_resolver.py:318`），domain 检测返回 `(best_domain, candidates)` 无 margin（`:461`），`resolve_capability` 仅收 `query/file_path/domain_hint`（`:637`）。→ §3.4/Phase 3 重写为 **resolver API 扩展**，工作量 M→M–L。
4. ✅ **遗漏前置阶段：数据状态探测/缓存**：precondition 过滤需当前 `obs/obsm/uns/layers` 状态，但通用 preflight 未建（`preflight/__init__.py:6`、`sc_batch.py:11`）。→ 新增 **Phase 2a**。
5. ✅ **spatial pipeline 不足以当"黄金 DAG"证明**：它是 `processed.h5ad` 上的有序链（`pipelines/spatial-pipeline.yaml:19`），但部分步骤并行/相邻（`spatial-domains/SKILL.md:130` 称 `spatial-genes` 并行）。→ 降级为**兼容性 smoke test**（Phase 2/§3.3）。
6. ✅ **计数陈旧**："43 个脚本 emit next_steps" → live 扫描 39 个 `.py`（主技能脚本 31 个）。→ §2.3 改用可复现命令口径。

### C.2 建议优化（should-improve，已应用 ✅）
1. ✅ **(B) 初期不做硬剔除**：false positive 高（`sc-clustering` 缺 embedding 硬失败但可用非默认 `--use-rep`，`sc-clustering/SKILL.md:66,77`）。→ §3.4/Phase 3 改 **penalty-first**。
2. ✅ **skip 边与依赖边分离**：`skip_when` 是路由负信号、非 DAG 依赖；复用已有 `scripts/extract_skip_when_cases.py`。→ `skip_when` 移出 `skill_dag.json`。
3. ✅ **拆 Phase 1**：1a schema 字段 → 1b interface 内容质量（元数据与 prose 有不一致，如 `spatial-domains` `parameters.yaml:181` `requires_preprocessed:false` vs `SKILL.md:40` 称需预处理）。
4. ✅ **`task_type` 作软特征**：硬过滤伤多意图/相邻（`sc-de` vs `sc-markers`）。→ §3.4/Phase 3 改排序/分区软特征。
5. ✅ **`skill_dag.json` 携带派生元数据**：source/matched key/condition scope/confidence/reviewed。→ Phase 2 边 schema 已加。

### C.3 可选增强（optional）
1. ✅ registry 嵌套措辞：代码支持**任意** domain 的一层通用 subdomain，非仅 `singlecell`（`registry.py:88`）。→ §2.1 已改。
2. ℹ️ **外部 repo 引用未在本仓校验**：该 checkout 无 `skill_selection_repo/`，Codex 只核对 OmicsClaw 侧；三 repo 的 `file:line`（附录 A）由本会话并行代码级审计 agent 提供，未经 Codex 二次核验（维护者如需可另行抽查）。
3. ✅ 新增"无上游也合法"负例集（`spatial-domains` 自算 PCA、`scanvi`→`scvi` 回退、用户 obs 列满足 marker/DE）。→ §5 已加。

> 结论：v0.1 的方向（结构感知 = 依赖/前置 (B) 高价值、层级 (A) 低 ROI）经 Codex 确认成立；但实现口径从"可信的方向性提案"收紧为"可执行但需 1a/1b 内容校订 + Phase 2a 前置、且全程 penalty-first 的工程计划"。
