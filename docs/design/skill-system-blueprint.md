# OmicsClaw Skill 系统蓝图

> 状态：设计蓝图（2026-07-29）。
>
> 角色：这是 Skill 系统的**总图**——它把 MUSE-Autoskill 的五阶段生命周期逐条映射到
> OmicsClaw 的组件，记录哪些机制被采纳、哪些被**刻意拒绝**，并给出目标架构、不可违反
> 的不变量和分期路线。
>
> 它**不是**验收真源。完成度判定仍以
> [Skill 审计系统：设计基线与验收规格](../reviews/2026-07-13-skill-audit-system-design-assessment.md)
> 的 M0–M3 与 REP/ACQ/RET/EVO/SYS 编号为准；持续评测的目标设计见
> [Skill 审计持续评测与经验治理设计](skill-audit-continuous-evaluation.md) 与
> [ADR 0074](../adr/0074-govern-skill-experience-and-continuous-evaluation.md)。
>
> 参照论文：Lin et al., *MUSE-Autoskill: Self-Evolving Agents via Skill Creation,
> Memory, Management, and Evaluation*, arXiv:2605.27366v2（2026-07-03）。本文引用的
> MUSE 数据全部来自**论文正文**；此前
> [设计文档 §14](skill-audit-continuous-evaluation.md) 的批判表基于一份**非官方参考
> 实现**，两者结论一致但证据来源不同，不可混用。

---

## 1. 定位：与 MUSE 的分歧只有一条

MUSE 论文提出的核心主张——Skill 不应是一次性生成物，而应是**跨创建、使用、评测、管理、
改进积累经验的长生命周期资产**——OmicsClaw 完全接受。分歧只在一处，但它决定了往下的
每一个取舍：

| | MUSE-Autoskill | OmicsClaw |
| --- | --- | --- |
| Skill 是什么 | 从一次成功轨迹蒸馏的**过程文档** | **可执行科学方法 + 唯一机器契约** |
| 正确性证明 | 同一个 LLM 自撰的 unit test | 协议绑定的 demo/fixture/benchmark + 真实数据不变量 |
| 失败后 | 自动 patch 并重跑 | 生成候选 → 人工批准 → 固定验证门 → CAS 写回 |
| 经验载体 | 每技能一份自由文本 `.memory.md` | 证据派生、可完全重建的 Experience View |
| 错误的代价 | 任务失败，**可观测** | **貌似合理的错误生物学，不可观测** |

论文自己的数据支持这个分歧：

- **MUSE 的 Skill 轻一个量级。** Table 7：MUSE 生成的包 **90% 只有 `SKILL.md`**，
  仅 8% 带 `scripts/`、8% 带 `tests/`、**0% 带 `references/`**；`SKILL.md` 中位数
  310 行。它的技能本质是过程文档，不是代码包。
- **论文的失败案例正是本项目治理机制的存在理由。** `hvac-control` 从 80% 掉到
  **20%**——生成的技能把一次成功轨迹里的标定窗口和增益估计当作通法，比不用技能更差。
  论文归因为「source-trajectory-specific assumptions limit out-of-distribution
  robustness」。
- **论文 Limitations 明确指出高风险部署需要的正是本项目已建成的东西**：
  「high-impact deployments need stronger provenance tracking, adversarial tests,
  and human review before skills are shared across teams or exposed to user-facing
  workflows」。

> **结论：OmicsClaw 不是「MUSE 减去自动化」，而是 MUSE 论文自己指定的、高风险科学场景
> 下的那个版本。分歧之外的机制应当尽可能采纳。**

---

## 2. 五阶段映射

| MUSE 阶段 | OmicsClaw 组件 | 相对强度 | 实测状态（2026-07-29） |
| --- | --- | --- | --- |
| **Creation** | `create_omics_skill`（Agent loop 内）+ `scaffolder.py` + staging demo gate（沙箱分级、原子发布） | ≥ MUSE | ❌ tracked 正式库 **0/95** 来自此路径 |
| **Memory** | `SkillAuditRuntime` → Skill Experience View | **强于 MUSE**：版本绑定、可重建、无第二真源 | 🟡 结构完整；3 个字段无生产者，usage 计数硬编码 |
| **Management** | capability resolver + routing oracle + `merge_candidate` + ADR 0068 替代式弃用 | **强于 MUSE**：两阶段替代而非文档重生成 | 🟡 仅 stage-one 重叠 advisory |
| **Evaluation** | Evaluation Protocol + digest（绑定 entry 字节与依赖版本）+ 稳定性离散度 | **强于 MUSE**：协议漂移即证据失效 | ❌ **0/95** 声明协议 |
| **Refinement** | 候选提案 + 人工批准 + 固定验证门 + 中断对账 | **刻意弱于 MUSE**（见 §7） | 🟡 remediation brief → AutoAgent 未接线 |
| *(MUSE 额外)* **Context** | 上下文装配 + 压缩 | **弱于 MUSE** | ⚠️ 已知债 |

五个阶段中三个设计更强、一个刻意更弱、一个确实更差。**而三个更强的阶段全部因为缺少
输入而空转。**

---

## 3. 目标架构

```text
┌─ L1 创作层（Agent 面向 —— 唯一该压缩的层）───────────────────┐
│  SkillAuthoringRequest                                       │
│    source.kind: intent | run | corpus     ← 唯一公开判别式     │
│      intent : Agent 表达适用条件 + I/O 语义 + 方法约束          │
│      run    : Backend 从已验证运行事实派生，Agent 不复述  ★主引擎│
│      corpus : 保留 source reference，默认值须有引用或 TODO      │
└───────────────────────────┬──────────────────────────────────┘
                            ▼  scaffolder.py（既有 Implementation，不重写）
┌─ L2 正式表示层（唯一机器真源 —— 不压缩）─────────────────────┐
│  skill.yaml v2/v3（ADR 0037）                                 │
│    身份 / summary(路由) / interface / runtime / deps          │
│    resources / lifecycle / validation / provenance / security │
│  + SKILL.md + references/ + <skill>.py + tests/               │
└───────────────────────────┬──────────────────────────────────┘
                            ▼  准入：staging → lint → demo gate → 原子发布
┌─ L3 检索层 ──────────────────────────────────────────────────┐
│  capability resolver · routing oracle · preconditions         │
│  skill DAG · candidate plan · 资源感知拓扑执行器               │
└───────────────────────────┬──────────────────────────────────┘
                            ▼
┌─ L4 执行层 ──────────────────────────────────────────────────┐
│  Shared Runner —— 输出契约动态验证（ADR 0065）                 │
│  Run Dispatcher + Execution Resource Scheduler（0061/0062）    │
│  fresh exclusively-claimed output dir（0070）                  │
└───────────────────────────┬──────────────────────────────────┘
                            ▼  ★ 发布后评测（ADR 0076 提案）
┌─ L5 证据层（append-only，永不改写）──────────────────────────┐
│  SkillHealthLedger      : SkillRunEvent                       │
│  EvaluationResultStore  : ProtocolEvaluationResult            │
│  身份 = skill_id + version + manifest_hash + source_hash      │
└───────────────────────────┬──────────────────────────────────┘
                            ▼  纯投影，可从 ledger 完全重建
┌─ L6 经验层（= MUSE 的 skill-level memory，但类型化）─────────┐
│  Skill Experience View                                        │
│    declared / effective validation + validation_state         │
│    usage · health · stability(离散度) · gotchas · coverage gaps│
└───────────────────────────┬──────────────────────────────────┘
                            ▼  唯一 mutation authority
┌─ L7 治理层 ──────────────────────────────────────────────────┐
│  SkillEvolutionGovernance                                     │
│    候选生成 → 人工批准 → 固定验证门 → CAS 写回 → 对账          │
│    kinds: validation_promotion / validation_demotion ·        │
│           gotcha · skill_deprecation · merge_candidate ·      │
│           protocol_revision · (计划) remediation_brief         │
└───────────────────────────┬──────────────────────────────────┘
                            ▼
┌─ L8 表面层（薄 Adapter，不拥有策略）─────────────────────────┐
│  Desktop / OmicsClaw-App · CLI · Channels                     │
└───────────────────────────────────────────────────────────────┘
```

三处结构性质决定了整个系统的安全边界：

- **L5 → L6 是投影**：Experience View 可删除并从 ledger 重建，不拥有治理事实。
- **L6 → L7 是建议**：证据只能产生候选，不能自动执行。
- **L7 → L2 是唯一写路径**：CAS + 人工门 + 固定验证器。

MUSE 在这三处都是直连可写——这正是它的 `hvac-control` 回归无法被拦截的结构原因。

---

## 4. 七条不可违反的不变量

1. **`skill.yaml` 是唯一机器真源**；catalog / DAG / routing 等派生产物单向生成（ADR 0037）。
2. **每条结论绑定精确 Skill revision**（`skill_id + version + manifest_hash +
   source_hash`）；`environment_id` / `protocol_digest` / `run_id` 是正交证据维度，
   不可替代 revision（AUD-01）。
3. **Agent 可提出，不可批准**；`governance-owned` 字段永不接受 Agent 输入，并提升为
   schema 级不变量与回归测试。
4. **经验是投影，不是笔记**；禁止任何自由文本第二真源——这是对 `.memory.md` 的直接拒绝。
5. **协议绑定等级**：`fixture-validated` 及以上必须有声明的 Evaluation Protocol；
   `demo-validated` 可由执行证据挣得（刻意保留的务实例外，否则系统零起点）。
6. **准入 ≠ 评测**：staging demo gate 回答「可否进库」，`governance.evaluate()` 回答
   「已发布 revision 挣到了什么」（ADR 0076 提案）。
7. **失败只生成候选**；永不自动修改科学算法或默认参数。

---

## 5. 缺口分类

区分「设计不对」和「没数据」是这份蓝图最重要的判断——前者要改架构，后者只要打开开关。

### 5.1 结构缺口（必须改设计）——共 4 个

| # | 缺口 | 证据 |
| --- | --- | --- |
| **S1** | Authoring Interface 丢失信息 | `scaffolder.py` 对所有技能写入同一句占位 `skip_when`；`interface.inputs` 建为空 `Inputs()`；`outputs` 硬编码 `report.md/result.json`；`input_formats`/`primary_outputs` 被显式 `del`，只进散文 |
| **S2** | 准入证据无法成为审计证据 | 创建 gate 走裸 subprocess（不经 Shared Runner、不产生 `SkillRunEvent`）；gate 之后改写 `validation`/`lifecycle`，导致执行时 manifest ≠ 发布 manifest |
| **S3** | 缺少「零协议」信号 | `coverage_gap` 只在**声明等级高于协议可挣得等级**时触发；全库 `smoke-only` 时永不触发，系统对库级实际状态沉默 |
| **S4** | run 身份用路径穿过 LLM | `SkillPromotionCandidate` 有 `runId` 却标记为 provenance-only 并丢弃，改把 `workspaceRoot` 拼进给 LLM 的自由文本指令 |

### 5.2 吞吐缺口（设计正确，缺数据）

Experience View 的 `approved_gotchas` / `coverage_gaps` / `pending_proposal_ids`
三个字段无生产者；`usage.routing_count` / `explicit_count` 硬编码 0；95 个技能零协议；
merge stage-two；remediation handoff；AUD-09 / AUD-10。**这些不需要改架构。**

### 5.2b 「缺少证据」被当成「负面判决」（同一错误的两个实例）

2026-07-29 诊断 App 上普遍出现的「权限未说明」徽章时，找到同一错误类的两处实例：

| 实例 | 表现 |
| --- | --- |
| `security` 未声明 | Backend 报中性事实 `reviewed:false / enforcement:"undeclared"`；**App 单方面**把它升级成红色警告徽章 + 关注排序 +100 权重 + 筛选维度。92/95 命中，筛选器返回 97% 语料 |
| `run_health` 缺失 | `faultRate()` 无运行记录时返回 `Infinity`（**faults 排序的哨兵**），而 `attentionScore` 拿它与阈值比较 ⇒ **「从未运行」与「每次都失败」同分** |

两者共享一条根因：**把「尚无证据」渲染成「已判定有问题」**。这与 L5→L6 投影层的
纪律正好相反——审计读模型对同一情形给出的是 `evaluation_required`（待评测），
不是失败。

结构上这属于 **L8 表面层拥有了策略**（违反 §4 不变量与 ADR 0074 §9 薄 Adapter 约束）：
Backend 从未签发「该技能有治理问题」这一判决，App 自行签发了。

根因是 F4 的真空：ADR 0037 自陈「full cross-layer security governance consumption
remain separate, unaccepted work」，`security` 因此成为 `skill.yaml` 中**唯一被审计
读模型完全忽略的治理字段**——Experience View 有 declared/effective validation、
health、stability、gotchas、coverage gaps，独独没有 security。App 填补了这个空缺。

**已修（App 侧）**：卡片只呈现**正向**状态（已声明，3/95，稀有即有信息量），
未声明退回筛选器与详情页；关注排序权重改为 `缺陷 100 > 不可运行 40 > 元数据缺口 10`；
新增 `hasReproducingDefects()` 使「无运行记录」不再计为缺陷；评分函数从 client
组件抽为纯模块 `skill-sort.ts` 并补 8 条回归测试（此前**无测试接缝**）。

**待做（P2，Backend 侧的真正修法）**：若 security 要成为治理信号，应由审计读模型
签发为一种 `coverage_gap`，与 `continuous_evaluation_unconfigured` 同批。做完之后
App 退回纯渲染，本节的越权自动消解。**在此之前，任何表面层都不得自行判定治理状态。**

### 5.3 环境缺口（横切，优先级被长期低估）

`missing_dependency` 属于 `_ENVIRONMENT_FAILURE_KINDS`，因此**不会**被误判为技能缺陷
——这是正确设计。但环境未装配会让每一次执行产出环境失败证据而非科学证据，直接卡住
飞轮。参见 [自适应环境供给提案](../proposals/adaptive-environment-provisioning.md)。

---

## 6. 路线图

### P1 · 让飞轮转起来（唯一有严格先后依赖的阶段）

**目标：让第一个技能走完 创建 → 发布 → 评测 → 挣得等级 → 进 Experience View 的整圈。**

1. **ADR 0075**：`SkillAuthoringRequest`，`source.kind` 为唯一公开判别式，
   **`run` 分支为主引擎**。
2. **修 S1**：`skip_when` 真实合成、`input_formats → interface.inputs` 打通、
   `primary_outputs → outputs` 打通。这同时是 ADR 0075 的形状验证，应先于 ADR 落地。
3. **ADR 0076**：先发布后评测；acquisition 在发布后调用 `governance.evaluate()`；
   系统派生默认 `kind: demo` 协议。
4. **修 S3**：新增 `continuous_evaluation_unconfigured` 缺口类型（**不修改**现有
   `coverage_gap` 语义）。
5. **修 S4**：App 提交 `run_id`，Backend 用现有 Run 权威解析已 claim 的输出目录
   （ADR 0070）；`promotion_ref` 作为后续加固。

**验收（可证伪）**：至少 1 个技能经 `run` 分支进入正式库，`declared=demo-validated`，
`validation_state=current`，Experience View 的 `stability` 非空。

### P2 · 让经验真正积累

6. Experience View 三个空字段接上生产者；`routing_count` / `explicit_count` 接路由证据。
7. 为高频技能声明 `fixture` 协议 → 第一批 `fixture-validated`。
8. **按 Skill 类型提供可覆盖的默认协议模板**——
   [设计文档 §6.1](skill-audit-continuous-evaluation.md) 早已预见「不给模板就没人写」，
   缺它是 0 采用率的直接成因。
9. AUD-10 净效用 baseline/treatment。

**验收**：`by_declared_level` 不再全是 `smoke-only`；`by_validation_state.current > 0`
且随使用增长。

### P3 · 补齐真正落后的与延期项

10. **上下文管理**——MUSE 唯一实打实领先之处：ReAct 轮次 DAG、L1 单节点压缩 /
    L2 跨轮合并、`KEEP_FIRST`/`KEEP_LAST` 钉住、`history_prev`/`history_next` 保全整条
    历史可重放。对照本项目已知的「压缩反噬 prompt cache」债，值得直接借鉴。
11. AUD-09：RunRuntime 队列迁移 + `AuditOperation` + App 的 observer/cancel。
12. remediation brief → AutoAgent 交接（保持人工门控）。
13. v3 G2 类型化参数——见
    [skill.yaml v3 G1/G2 草案](../proposals/skill-representation-v3-g1-g2.md)；
    Authoring Request 是它的天然首个消费者。
14. merge stage-two：替代技能通过受控获取、达到 `demo-validated`、通过路由回归后，
    旧技能才能收到替代式弃用提案。

---

## 7. 明确不做（含从 MUSE 拒绝的机制）

| 项 | 拒绝理由 |
| --- | --- |
| `.memory.md` 或任何自由文本经验槽 | 无版本绑定、难以脱敏、会成为第二真源；由 L6 类型化投影取代 |
| 自动 refinement 改写科学代码或默认参数 | `hvac-control` 回归即此路产物；绕过人工判断与 CAS 治理 |
| 自动 merge / forget | 描述相似 ≠ 方法等价；低使用频率 ≠ 低价值 |
| 以生成的 unit test 作为科学正确性证明 | 同一 LLM 自撰、可被 refiner 改绿；真实数据不变量更强 |
| 跨 Agent skill transfer / 公共 skill hub | 与「基因数据不出本机」冲突；本地优先 |
| 继续压缩 canonical `skill.yaml` | 复杂度不会消失，只会扩散到 Resolver / DAG / Runner / Desktop |
| 新建 `SkillAuthoringRuntime` Module | 既有 scaffolder 已实现生成、推导、staging、验证、隔离、原子发布；只加类型化前门 |
| 通用向量 Skill Memory / RAG | 见 [ADR 0074](../adr/0074-govern-skill-experience-and-continuous-evaluation.md) 非目标 |

---

## 8. 一个战略结论：创作引擎应当 run-promotion-first

论文数据：MUSE 在 75 个任务中 **28 个自创失败**，失败集中在没有成功轨迹的任务；
而**有成功轨迹时，自创技能在覆盖子集上达到 85.24%，超过人工技能的 81.17%**。论文原文：
「generated skills are strong when a successful source trajectory exists」。

OmicsClaw 的技能比 MUSE 重得多，`intent` 冷启动只会更难。因此：

> **创作引擎的主路应是 run-promotion：用户跑通一次自主分析 → 促成为技能。**
> 这正是 `SkillPromotionCard` 已在做的事，也正是「Agent 不复述、Backend 从事实派生」
> 最成立的分支。`intent` 分支保留，但不是主路。

---

## 9. 已验证的首个闭环：`spatial-domains`（2026-07-29）

P1 的验收目标已在一个既有技能上跑通（`创建` 阶段以「声明评测协议」代替——技能本身已存在）。

**改动**：`skills/spatial/spatial-domains/skill.yaml` 纯加法 6 行——

```yaml
validation:
  protocols:
  - id: spatial-domains-demo-v1
    kind: demo
    entry: spatial_domains.py
    repeats: 2
```

`validation.level` **未被手工修改**（它是 governance-owned，只能经提案 + 人工批准 + CAS 写回）。

**闭环证据**：

| 环节 | 结果 |
| --- | --- |
| `governance.evaluate("spatial-domains")` | 2 次运行经 Shared Runner，均 `succeeded`，同一 `protocol_digest` |
| Experience View | `validation_state: evaluation_required → **current**`；`usage.execution_count: 2`；`health.successes: 2`，0 缺陷 |
| `stability` | `{spatial-domains-demo-v1: {runs:2, success_rate:1.0, outcomes_consistent:true}}` |
| `effective_validation_level` | `smoke-only`——**被 declared 封顶**（`_min_level`），行为正确 |
| `refresh()` | 生成 `validation_promotion` 提案（`smoke-only → demo-validated`），待人工批准 |
| 库级 summary | `by_validation_state.current: 0 → 1` |

**为什么只声明 `demo` 而不是 `fixture`**：`tests/` 下全部是 `test_demo_*`——跑 demo 后断言产物存在，
属于 demo 契约测试，不是「已提交 fixture + 确定性科学断言」。声明为 `fixture` 即过度声称。
`fixture-validated` 需要先补真正的 fixture 协议。

### 9.1 依赖版本按 Skill runner 解析（已修复）

协议 digest 绑定 `deps.python` 的**已安装版本**。原实现在**编排进程内**解析，
但技能子进程由 `get_skill_runner_python()` 决定——`OMICSCLAW_RUN_PYTHON` 的存在
本就意味着两者可以合法不同。后果实测为：

```
编排=OmicsClaw 环境 : sha256:c7c0585...
编排=anaconda base  : sha256:b4d3faa...   ← 同一 Skill、同一 runner，digest 却不同
```

即 runner 挣得的证据会被编排进程按**自己的**环境判为过期，`stability` 静默变空。

**已修正**：`_runner_distribution_versions()` 按 `get_skill_runner_python()` 解析，
同解释器走进程内快路径、异解释器每进程一次有界子进程探测（按可执行文件路径记忆化）。
两条附带纪律：

- **首个 `sys.path` 条目优先**。`distributions()` 会产出被遮蔽的重复发行
  （例如 user-site 的 `torch` 排在 env 之前），而导入解析取**第一个**，
  digest 必须记录 Skill 真正运行的那一份。
- **探测失败记 `unresolved` 而非回落编排环境版本**。它与 `missing`（探测到但未安装）
  是两个不同的 digest 输入，因此异常环境让证据**变旧**，绝不伪装成新鲜。

**仍然成立的纪律**：安装或升级任何 `deps.python` 中声明的包都会改变 digest，
使已挣得的协议证据失效——这是 §6.4 新鲜度规则的预期行为，不是 bug。
本节的修复过程本身就触发了一次：新增 `igraph` 声明后 `validation_state`
自动变为 `stale`，重新评测后回到 `current`。

### 9.2 附带修正：`deps.python` 与运行期真相的分裂

`spatial-domains` 的 leiden 路径需要 `python-igraph`，但 `deps.python` 未声明——
而技能脚本内部的 `method_packages` 表**已经**记录了这个事实。同一事实两个源，
且治理用的那个（`skill.yaml`）不全，违反 ADR 0037「唯一机器真源」。

根因是 `deps.python` 由 `scripts/audit_skill_requires.py` 静态分析生成，
而 `sc.tl.leiden()` 对 igraph 的需要发生在 scanpy 内部，静态不可见。修法是在
`_lib/domains.py` 的 leiden 路径补一个与既有 louvain 路径同款的探针导入，
让生成器看得见，再由生成器写回——**不手工编辑 `deps.python`**。

顺带纠正了一个反向错误：`method_packages["leiden"]` 多声明了 `leidenalg`。
该路径传 `flavor="igraph"`，scanpy 只在 `flavor=="leidenalg"` 分支才导入
`leidenalg`，因此真实需求只有 igraph。

> 一般教训：**当运行期依赖发生在第三方库内部时，静态生成的依赖契约必然漏项。**
> 探针导入是把这类事实拉回单一真源的既有手段，应在同类方法分派处保持一致。

---

## 10. 文档关系

| 文档 | 角色 |
| --- | --- |
| 本文 | Skill 系统总图：MUSE 映射、目标架构、不变量、路线 |
| [验收基线与验收规格](../reviews/2026-07-13-skill-audit-system-design-assessment.md) | **验收真源**：M0–M3 与 REP/ACQ/RET/EVO/SYS 编号 |
| [持续评测与经验治理设计](skill-audit-continuous-evaluation.md) | ADR 0074 的详细设计（AUD-01–10） |
| [ADR 0037](../adr/0037-unified-declarative-skill-representation.md) | `skill.yaml` 为唯一机器真源 |
| [ADR 0065–0069](../adr/0065-verify-skill-output-guarantees-at-the-shared-runner.md) | 输出契约验证与演化治理 |
| [ADR 0074](../adr/0074-govern-skill-experience-and-continuous-evaluation.md) | 经验视图与持续评测 |
| ADR 0075（计划） | Agent Authoring Request 与 canonical manifest 分离 |
| ADR 0076（计划） | 发布后挣得获取证据（准入 ≠ 评测） |
| [v3 G1/G2 草案](../proposals/skill-representation-v3-g1-g2.md) | 组合签名与类型化参数 |
| [自适应环境供给提案](../proposals/adaptive-environment-provisioning.md) | 横切环境缺口 |

---

OmicsClaw 是多组学研究与教育工具，非医疗器械，不提供临床诊断。工程验收不能替代领域
专家对方法学和科学结果的复核。
