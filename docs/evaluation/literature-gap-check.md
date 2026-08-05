# 文献 Gap 核查：OmicsClaw 论文选题的可辩护性

> 状态：核查完成，2026-08-03。
>
> 目的：在投入 benchmark 工程之前，验证"组学领域 skill 与 agent 解耦、无法复用"
> 这一 gap 论证是否成立。
>
> **结论：原定 gap 论证不成立。** 三处独立证据表明"给组学 agent 配可执行 skill 库"
> 已是 2026 年的既有实践，且 skill lifecycle 的治理机制也已被占位。
> 修订后的可辩护论点见 §4。
>
> 方法学提示：本次核查中 WebFetch 对 SkillOps 的二手摘要**确认了提问里的每一项**，
> 与原文核对后发现 revision 绑定、依赖版本绑定、adherence 度量三项均为幻觉。
> **凡进入 related work 的判断必须读原文。** 本文标注 ✅ 的是已读原文核实的。

---

## 1. 原 gap 论证的三处反证

### 1.1 omicos（omicverse）——同域竞品已在做 agent + skill 路由 ✅

`data/benchmarks/biomnibench-da/OmicOS-BiomniBench/configs/agents.yaml` 与 `models.yaml`
（本地已下载，直接读取）：

- `vertical_agent_selector` 用 `agent_select` 工具对 worker roster 打分，
  **评分依据明确包含 `skills` 元数据**，再 `call_agent` 委派专家 agent。
- BiomniBench-DA 50 任务 × 7 模型结果已公开，含 6 维能力雷达与成本分析。
- omicos 本体尚未开源（README：*"coming soon — public release pending"*）。

**影响**：同领域、同数据集上，"agent 按 skill 元数据路由"已经在跑并有公开数据。

### 1.2 ClawBio——自称首个生物信息学原生 agent skill 库 ✅

[github.com/ClawBio/ClawBio](https://github.com/ClawBio/ClawBio)，
自我描述：*"The first bioinformatics-native AI agent skill library. Local-first.
Reproducible. Open. Free."*

| 维度 | ClawBio v0.5.0 | OmicsClaw |
|---|---|---|
| skill 形态 | 自包含目录：`SKILL.md` + validated Python + demo data + reproducibility 支持 | 同构 |
| 规模 | 95 skills（89 带 demo） | 95 skills |
| 调用 | `clawbio run <skill>` / Python API / agent routing | `oc run` / agent tool |
| 成熟度阶梯 | `spec-only → scripted → tested → cli-registered → ci-validated → bench-validated` | `smoke-only → demo-validated → fixture-validated → benchmarked` |
| 验证 | 74 benchmark tests，168/182 通过，live leaderboard | 协议驱动，4/95 声明协议 |
| 发表 | 无同行评议论文；Zenodo DOI 10.5281/zenodo.19420648 | — |

**这是最直接的威胁。** 成熟度阶梯与 OmicsClaw 的 validation level 阶梯高度同构，
定位口号几乎逐字相同。

**可区分之处**（必须在 related work 显式写出）：
- ClawBio 的 tier 是**静态标签**；OmicsClaw 的 level 由**协议证据挣得**，
  且 protocol digest 绑定依赖版本——装包即失效（见 §9.1 的 igraph 案例）。
- ClawBio README 自陈 *"The exact contents can vary by skill, and some replays still
  require the original external inputs"*，reproducibility 是 **skill-optional**；
  OmicsClaw 是 manifest 级强制 + CAS + 人工门。
- 域重心不同：ClawBio 偏药物基因组/群体遗传/临床变异；OmicsClaw 偏空间/单细胞。

### 1.3 AtomisticSkills——同架构，另一个科学域 ✅

[arXiv:2605.24002](https://arxiv.org/abs/2605.24002)：*"an open-source harness framework
that empowers **general-purpose AI coding agents** to conduct atomistic research"*，
**100+ 人工策划 skill**，覆盖材料科学/化学/药物发现。

注意措辞——"赋能通用 coding agent"正是 OmicsClaw 的架构主张。

**关键弱点（也是 OmicsClaw 的机会）**：其验证方式是
*"functional coverage against scientific literature and robust orchestration
capabilities across diverse scientific campaigns"*——**文献覆盖度 + 案例式 campaign，
没有对照消融，没有确定性 grader**。

---

## 2. "完整 skill lifecycle"同样已被占位

| 工作 | 覆盖 | 与 OmicsClaw 的重叠 | 差异 |
|---|---|---|---|
| MUSE-Autoskill（本地 PDF） | 五阶段生命周期 + `no_skill/human/self-created` 消融 | 生命周期框架、消融设计 | 90% 技能只有 `SKILL.md`；全自动 refinement |
| [SkillOps](https://arxiv.org/abs/2605.13716) ✅ | library-time 维护；Skill Contract `(P,O,A,V,F)`；HSEG 类型化图；五维健康诊断 | **Validation-Gap 维度 `G(s)=1[V_s=∅]` 就是蓝图 §5.1 的 S3「零协议信号」**；merge/retire ≈ merge_candidate/skill_deprecation；redundancy ≈ capability resolver | 在 **ALFWorld** 评测（非科学域）；全自动无人工门；**无** revision 绑定、**无**依赖版本证据失效、**未**度量 adherence（二手摘要曾谎称有，已核实为幻觉） |
| [Agent Skills 综述](https://arxiv.org/abs/2605.07358) | 表示/获取/检索/执行/演化/安全/治理 | `docs/proposals/skill-lifecycle-redesign.md` 已映射 | — |
| [Reference Architecture](https://arxiv.org/abs/2606.20631) | skill-mediated agent 参考架构 | 蓝图的 L1–L8 分层需对照 | — |
| [SkillResolve-Bench](https://arxiv.org/abs/2606.10388) / [Group of Skills](https://arxiv.org/abs/2605.06978) / [SkillWiki](https://arxiv.org/abs/2606.16523) / [Reranking](https://arxiv.org/abs/2607.06283) | 检索与组织 | routing oracle / capability resolver | 均非科学域 |

**净结论**：五阶段框架、契约化表示、库健康诊断、检索组织——都有人做了。
"我们有完整 lifecycle"作为论点已无立足点。

---

## 3. 上一轮建议的"方差/可复现性"novelty 也被部分占位

这是本次核查最重要的自我修正。

| 工作 | 已做的事 | 残留缺口 |
|---|---|---|
| [蛋白集功能注释 benchmark](https://www.biorxiv.org/content/10.64898/2026.04.18.719404v1)（bioRxiv 2026-04-18）⚠️ | 3 个 agent 配置、同一 verbatim prompt、73 orthogroup / 1,705 序列。结论：*"Skill-enabled agents improved file handling, evidence traceability, and reproducibility of computational checking, but they did not eliminate biological overinterpretation."* 同一 prompt 同一证据下，high-confidence call 数在 **1 到 12** 之间波动 | **单一任务类型**、无确定性 grader、无多域 suite、无参数层分析。其自身建议（deterministic evidence tables + explicit scoring rules + provenance columns + run-to-run replication + expert review）恰好是 OmicsClaw 架构的描述 |
| [How Consistent Are LLM Agents?](https://arxiv.org/abs/2605.28840) ✅（仅摘要） | 度量结构化 tool-calling 的行为可复现性：**是否选同样的工具、同样顺序、同样参数** | 摘要未给域与数字。搜索摘要称其发现"工具选择一致但**参数差异大**"——**此点未经原文核实，写进论文前必须读全文** |
| [On Randomness in Agentic Evals](https://arxiv.org/abs/2602.07150) | agentic eval 的随机性方法学 | 需引作重复次数设计依据 |
| [Budget-Constrained Web Agents](https://arxiv.org/abs/2606.15017) | skill/memory 模块的 token 성价比消融 | 非科学域 |

⚠️ bioRxiv 正文 403，以上引文来自检索摘要，**写 related work 前须取得全文核实**。

---

## 4. 修订后的可辩护论点

交叉上述工作后，真正没被占的位置：

### 4.1 主论点（建议）

> 组学 agent 已经普遍配备 skill 库（omicos、ClawBio、AtomisticSkills、Biomni 系），
> 但**没有人在多域确定性 grader 上重复测量过 skill 究竟带来了什么**。
> 我们发现 skill 的主要收益不在准确率均值，而在**参数层面的科学可复现性**——
> 并给出证据绑定机制，使每条结论可追溯到精确 skill revision 与其依赖版本。

为什么这站得住：

1. **参数是组学里的科学本身。** 2605.28840 的发现是"工具选择一致、参数差异大"。
   在组学里 `resolution` / `n_top_genes` / `min_cells` / `target_sum` / FDR 阈值
   **就是**科学结论。这个交叉（结构化 agent 的参数方差 × 组学的参数敏感性）
   两边都没做。OmicBench 的 grader 恰好检查 `obs`/`var`/`uns` slot——参数选择直接决定这些值。
2. **验证严格度是空位。** 现有科学 skill 系统的验证方式：
   AtomisticSkills = 文献覆盖 + 案例 campaign；ClawBio = 168/182 单元测试；
   omicos = 单次 LLM judge rubric；bioRxiv = 单任务 3 配置。
   **44 题多域确定性 grader × skills-on/off × 重复测量，没有人做过。**
3. bioRxiv 那篇的结论本身就是对本论点的背书：skill 改善了可追溯性
   **但不足以取代显式评分规则与专家复核**——这正是 OmicsClaw 人工门治理的存在理由。

### 4.2 第二贡献（建议保留）

**证据失效的科学后果。** SkillOps 的 Validation-Gap 是静态的（`V=∅`）；
OmicsClaw 的 protocol digest 绑定**已安装依赖版本**，装个包既有验证即失效。
可测且组学特有（软件栈迭代极快）：
*一个科学 skill 库中，依赖漂移在多长时间内让多大比例的既有验证证据过期？*
蓝图 §9.1 的 igraph 案例（新增声明后 `validation_state` 自动变 `stale`）是活证据。

### 4.3 明确放弃

- ❌ "我们首创了组学 agent skill 库"——ClawBio/omicos/AtomisticSkills 在前。
- ❌ "我们有完整 skill lifecycle"——MUSE/SkillOps/综述在前，且自身实测 0/95、4/95。
- ❌ "skill 提升可复现性"作为**主**论点——bioRxiv 2026-04 已在生信域提出。
  降级为被复现并被推进的既有发现。
- ❌ OmicsClaw vs Claude Code 作为主实验——见上一轮，模型混淆。

---

## 5. 对实验设计的三条强制修改

1. **≥2 个 backbone。** [MASA](https://arxiv.org/abs/2605.30723) ✅ 的核心结论是
   *"skill effectiveness is strongly model-dependent: a skill that benefits one
   backbone can harm another"*（3 环境 × 4 backbone）。单模型消融会被直接质疑。
2. **参数层面的度量必须预先定义。** 不能只报 grader 分数。至少要从 trajectory 中
   抽出关键参数（resolution / n_top_genes / target_sum / FDR），
   报告其跨重复的取值分布与熵。这是主论点的核心证据，不是附属分析。
3. **seen-set 剔除。** OmicBench A02/A03、scAgentBench PAGA、BiomniBench da-12-2
   已写进 `skill.yaml` 协议与 `skills/*/tests/` 适配器，对 OmicsClaw 是调过的题。

---

## 6. 下一步的 go/no-go 门

Pilot 必须先回答一个问题：**参数方差现象真的存在，且 skill 真的能压塌它吗？**

- 8–10 题 × `no_skill`/`curated_skill` × ≥5 重复 × 2 backbone
- 主指标：关键参数取值的跨重复分布；次指标：grader score 的 mean 与 pstdev
- **若 skill 不压参数方差 → §4.1 论点作废，必须回到本文重新选题。**

在这个 pilot 出结果之前，不要动跨 agent harness（那是外部效度臂，不是主实验）。

---

## 7. 待补核查

- [ ] bioRxiv 719404 全文（403，需换渠道）——它离主论点最近
- [ ] arXiv 2605.28840 全文——"参数差异大"这一关键结论未经原文核实
- [ ] Biomni 的 tool library 与复用机制（本次未查）
- [ ] ClawBio 实际代码规模与 benchmark 真实性（README 可能高估）
- [ ] [Agent Skills in the Wild](https://arxiv.org/abs/2601.10338)（安全）——
      对应蓝图 §5.2b 中 `security` 是唯一被审计读模型忽略的治理字段

---

OmicsClaw 是多组学研究与教育工具，非医疗器械，不提供临床诊断。
