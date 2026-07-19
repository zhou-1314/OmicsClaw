# Skill 审计系统：设计基线与验收规格

> 状态：验收基线 v1.20（2026-07-18，含 EVO-G2 Round 18 `SHIP`；
> 窄 Backend workspace-authority 里程碑关闭，四阶段总体仍未完成）
>
> 范围：技能表示（Representation）→ 技能获取（Acquisition）→ 技能检索
> （Retrieval）→ 技能演化（Evolution）。
>
> 文档角色：这是四阶段系统的**验收真源**。历史论证与分期仍见
> [`skill-lifecycle-redesign.md`](../proposals/skill-lifecycle-redesign.md)、
> [`skill-acquisition-plan.md`](../proposals/skill-acquisition-plan.md)、
> [`structure-aware-skill-retrieval.md`](../proposals/structure-aware-skill-retrieval.md)
> 和相关 ADR；这些文档不再各自定义“完成”。

## 1. 目标与完成定义

系统目标不是“拥有很多 lint 脚本”，而是建立一个可追溯的技能闭环：

```text
来源/分析轨迹
  → 隔离暂存与规范化
  → 入库验证
  → 可检索、可解释地选择
  → 统一执行并记录结果
  → 聚合健康信号
  → 产生演化候选
  → 人工审批与重新验证
  → 写回唯一技能真源
```

只有以下四级都达到，才能称为“达到最初的完整 skill 审计系统目标”：

| 级别 | 名称 | 必须具备 |
| --- | --- | --- |
| M0 | 可审计表示 | 单一机器真源、派生产物无漂移、来源/生命周期/验证/安全可读 |
| M1 | 受控获取 | 新技能在暂存区生成，按来源执行相称的门，失败不会污染正式目录 |
| M2 | 可度量检索 | 选择路径可解释、可回归，负条件与前置条件不会只停留在 prose |
| M3 | 人工门控演化 | 执行事件形成健康信号，能提出修订/晋升/降级/弃用候选，审批后重验 |

Dense embedding、中央 skill hub、MCP 全量导出、全自动科学参数改写不是 M0–M3
的完成条件；它们只有在基准证明净收益且安全边界就绪后才进入后续目标。

## 2. 状态词汇：禁止混用

文档与代码必须区分五个正交维度：

| 维度 | 允许值/示例 | 回答的问题 |
| --- | --- | --- |
| 设计实现状态 | `planned / partial / implemented / verified` | 功能是否实现并有证据？ |
| 生命周期 | `draft / mvp / stable / deprecated` | 技能在库中的治理阶段是什么？ |
| 验证等级 | `smoke-only / demo-validated / fixture-validated / benchmarked / production` | 技能挣得了多少可信证据？ |
| 可用性 | entry/demo/dependency 是否当前可运行 | 当前环境能否运行？ |
| 一次执行结局 | `passed / skipped / failed` + `error_kind` | 本次门或运行发生了什么？ |

约束：

- 有脚本不等于 `mvp`，能导入不等于 `demo-validated`。
- `skipped` 不是通过；只能在有明确、可枚举的跳过原因时使用。
- `validation.level` 必须由持久化 evidence 挣得，不能由文件存在或人工声称派生。
- 设计文档中的“已完成”必须指向命令、测试、产物或运行事件；否则只能写
  `implemented`，不能写 `verified`。

## 3. 系统边界与权威数据

### 3.1 唯一真源

- 每个技能的机器契约：`skills/**/skill.yaml`。
- 方法学叙事：由机器契约生成固定头部的 `SKILL.md`；自由正文不得重定义机器事实。
- 派生产物：catalog、INDEX、路由表、parameters 文档；必须支持 `--check`。
- 执行结果：规范 `result.json` envelope；验证器只信规范字段和状态。
- 审计证据：持久化文件或事件，不能引用会在 staging 退出时删除的临时路径。

### 3.2 运行证据模型

审计事件至少需要以下字段；可以分布在 result、run manifest、index 或专门 ledger，
但必须能按 `skill_id + version/hash + run_id` 重建：

```yaml
event_id: unique-id
timestamp: ISO-8601
skill_id: canonical-id
skill_version: semantic-version
skill_hash: content-hash
source_hash: optional-source-revision
action: scaffold|promote|validate|run|route|revise|deprecate
actor: human|agent|ci|system
origin: human|scaffolded|promoted|migrated|corpus
run_id: optional-explicit-run-id
thread_id: optional-session-or-project-id
source_ref: optional-durable-reference
environment_id: optional-actual-producer-identity
runtime_source: optional-selected-runtime-source
claim_sha256: optional-bound-claim-bytes
result_sha256: optional-bound-result-bytes
hard_gate_verdict: optional-structured-admission-result
outcome: passed|skipped|failed
error_kind: optional-typed-error
evidence: durable-artifact-references
before_hash: optional
after_hash: optional
```

`claim_id`、output-directory leaf 或 execution fingerprint 只能作为辅助关联，不能伪装为
authoritative `run_id`。生产者环境字段也必须注明证据范围，不能把局部 fingerprint 宣称为完整 lockfile。

事件不得记录原始组学矩阵或秘密；路径和摘要遵守 local-first 与最小披露原则。

## 4. 四阶段设计契约

### 4.1 技能表示

表示层负责“能否被一致理解”，不负责声称科学正确。

硬约束：

1. `skill.yaml` 是唯一机器真源，未知字段响亮失败。
2. identity、适用/排除条件、I/O、参数、运行入口、依赖、来源、生命周期、验证、
   安全均由同一 schema 校验。
3. catalog/INDEX/SKILL.md 头部/参数文档只能单向生成。
4. 版本、入口、argparse/runner flag、安全策略和声明产物之间有契约测试。
5. 组合能力必须在需要跨技能链接时有机器可读表示；在 v3 落地前，不得把 prose
   `next_steps` 当成可执行 DAG。
6. 参数值只有在存在类型化契约后才能被自动提升、比较或导出为工具 schema；自由
   `hints` 只能作为叙事提示。

### 4.2 技能获取

获取层负责“如何安全地进入正式库”。

硬约束：

1. 所有新产物先在隔离 staging 中生成，最终移动是单一、原子提交点。
2. 占位脚手架可以作为 `draft/smoke-only` 入库，但必须保持明确“未实现”信号。
3. 真实实现和 promoted/corpus 产物必须执行与信任级别相称的验证门。
4. `passed` 才能挣得验证等级；依赖缺失、输入陈旧等 `skipped` 不得晋级。
5. model-authored 代码必须在真实 OS 安全边界可用时执行；若安全边界不可用，系统只能
   保持隔离草稿或要求人工确认，不能把“未验证”解释为“可信”。
6. 晋升必须锚定显式 `run_id/thread_id`，不能依赖全局 mtime 猜“最近一次分析”。
7. 泛化必须保留原始轨迹与改写证据；改写失败只能回退到可识别的 verbatim 草稿，
   不能静默降格后仍宣称可复用。
8. corpus 参数/阈值/关联必须带可验证 `source_ref`；无来源只能是 TODO，不能成为默认值。
9. `draft/smoke-only` 可以进入治理目录或开发者 catalog，但不得进入普通自动检索和
   无隔离执行路径；只有显式 developer-preview 或人工批准路径可以访问。

### 4.3 技能检索

检索层负责“在当前意图和数据状态下选择什么”，不负责替代运行前验证。

硬约束：

1. exact alias、LLM direct selection 与 `skill=auto` resolver 必须可区分、可统计。
2. resolver 结果包含候选、分数、原因、置信度/平局信息；未知 alias 不得静默执行。
3. `skip_when` 是负路由信号；producer→consumer 是候选兼容边；二者不得混成一种边。
4. 前置条件默认 penalty-first，只有有反例评测证明“缺失必失败”时才允许硬过滤。
5. 复合查询只能返回机器可验证的候选链，并在执行前要求用户确认。
6. 评测至少分开报告 precision@1、top-k recall、消歧率、错误别名率、前置判断正确率、
   复合链正确率和执行后的 net utility。
7. 任何新语义召回层必须先在 no-skill/平局子集证明净收益，不得 flatten 域结构。

### 4.4 技能演化

演化层负责“从真实使用中提出有证据的改变”，不是让系统自动改科学结论。

硬约束：

1. 每次运行产生结构化结局；失败至少区分依赖、输入、超时、资源、取消、脚本缺陷、
   契约失败和未知异常。
2. 健康聚合按 `skill_id + version/hash + environment` 分桶，不能把环境缺依赖等同于技能缺陷。
3. 自动化只能产生候选：Gotcha、参数修订、验证升降级、晋升、弃用或替代关系。
4. 候选必须带支持事件和反例；人工批准后才能改 `skill.yaml`/SKILL.md。
5. 写回后必须重新走表示校验、针对性执行门和检索回归；失败则不提交。
6. `deprecated/superseded_by` 必须影响检索与 UI；不能只作为无人消费的元数据。
7. 系统必须能回答“为什么升/降级、谁批准、改了什么、如何回滚”。

## 5. 验收矩阵

### 5.1 表示（REP）

| ID | 验收项 | 通过证据 |
| --- | --- | --- |
| REP-01 | 全部正式技能由 canonical schema 读取 | 逐技能加载报告；无 silent fallback |
| REP-02 | 派生产物与真源零漂移 | catalog、SKILL.md、parameters、INDEX、routing 的 `--check` |
| REP-03 | runner flag 与入口 parser 一致且受安全阻断 | flag introspection + lint + runner 集成测试 |
| REP-04 | result 契约可静态声明、动态校验 | 合法/非法/scaffold envelope 测试 |
| REP-05 | provenance/lifecycle/validation/security 在 registry、catalog、surface 一致 | 跨层契约测试 |
| REP-06 | v3 typed params/composition 若宣称完成，则有真实消费者 | schema + lint + generator/runtime 测试；否则状态必须为 planned |

### 5.2 获取（ACQ）

| ID | 验收项 | 通过证据 |
| --- | --- | --- |
| ACQ-01 | staging 失败不会留下正式技能或 registry 条目 | 失败注入集成测试 |
| ACQ-02 | placeholder 保持 draft/smoke-only 与未实现状态 | scaffold 端到端测试 |
| ACQ-03 | 真实 demo 通过才挣 `demo-validated` | passed/skipped/failed 三态测试 + durable evidence |
| ACQ-04 | promoted 代码的安全门是真边界，不是目录隔离 | 恶意写/网络/秘密读取动态测试 |
| ACQ-05 | 晋升身份锚定显式 run/thread，不受并发 mtime 影响 | 两会话并发选择测试 |
| ACQ-06 | 泛化后可换输入/参数复用，且不依赖 kernel facade | 两数据/两参数端到端测试 |
| ACQ-07 | corpus 来源字段和逐参数 source_ref 被强制 | schema/lint 正反例 + corpus scaffold 测试 |

### 5.3 检索（RET）

| ID | 验收项 | 通过证据 |
| --- | --- | --- |
| RET-01 | 路由语料覆盖全部 8 domains 和关键跨域歧义 | 可枚举 corpus 报告 |
| RET-02 | 输出 precision@1/top-k/消歧/幻觉别名率 | 确定性评测报告与基线 |
| RET-03 | structured skip_when 被 resolver 消费 | 正向/负向最小例 |
| RET-04 | 数据状态进入 precondition penalty，合法自补算不被误杀 | 探针缓存测试 + 反例语料 |
| RET-05 | 复合查询输出有 provenance 的候选 topo 链 | DAG/链测试；执行前确认测试 |
| RET-06 | validation/deprecated 状态对排序或提示有可见后果 | 同相关度候选对照测试 |

当前状态（2026-07-14）：全库生成图包含 95 nodes / 74 edges（43 exact、20 generic、
11 semantic artifact、18 reviewed、无环）。singlecell/spatial 保持 AnnData 关系；genomics、
proteomics、metabolomics、Bulk RNA 以显式 kind+format+path 形成受审 handoff；literature 与
orchestrator 只声明终端 artifact，不捏造消费边。因此仍不能外推为“8 域 workflow ontology
已完成”，但 candidate-wide penalty、review reject 与真正 topo executor 已有可验收纵切。

### 5.4 演化（EVO）

| ID | 验收项 | 通过证据 |
| --- | --- | --- |
| EVO-01 | 运行结局有 typed error_kind | 分类器正反例 + 未知兜底 |
| EVO-02 | health ledger 区分技能缺陷、环境/框架缺陷和用户取消 | 聚合夹具测试；validator 崩溃不得归罪 Skill |
| EVO-03 | 精确 version/hash 的显式 demo 证据才产生具体的一步晋级候选 | ordinary/demo、去重、缺陷阻断与幂等 proposal 测试 |
| EVO-04 | 未审批候选绝不写回，产品调用者不能自选 path/patch/validator | 深 governance Interface + 审批边界集成测试 |
| EVO-05 | 批准写回前 CAS + 固定三门重验，失败恢复 manifest 与投影 | stale race、before/after hash、catalog/DAG 回滚测试 |
| EVO-06 | 降级/弃用/替代关系对检索和所有 Surface 生效 | 端到端状态消费测试 |
| EVO-07 | 系统可回答证据、决策人、原因、变更 hash 与回滚结果 | Backend snapshot/decision contract + App review UI 测试 |

### 5.5 横切（SYS）

| ID | 验收项 | 通过证据 |
| --- | --- | --- |
| SYS-01 | 任何“完成”声明均有可复现证据 | 审计报告附命令、退出码、计数 |
| SYS-02 | 用户原始数据与 secrets 不进入审计事件 | 序列化/脱敏测试 |
| SYS-03 | 同一 skill/run 身份贯穿获取、执行、演化 | 跨层 correlation 测试 |
| SYS-04 | cheap deterministic gates 在 PR，昂贵全量门分层运行 | CI workflow 与触发策略 |

## 6. 评测执行顺序

1. **静态一致性**：schema、canonical YAML、派生文档、依赖审计、lint。
2. **最小动态契约**：result envelope、staging 原子性、demo gate 三态。
3. **检索基线**：固定语料和黄金快照，先记录现状再调整权重/结构。
4. **安全与并发**：model-authored 代码越界、无 sandbox、双会话晋升身份。
5. **闭环证据**：run event → health → proposal → approval → revalidation。
6. **科学 sanity**：每域至少 1–2 个公开小数据集，只验证结构、统计方向和已知
   sanity，不把它包装成临床或普适科学正确性。

## 7. 设计评估结论

方向是合理的：单一声明式表示、隔离获取、确定性优先的混合检索、人工门控演化，
能够形成适合本地多组学分析的 skill 审计系统。

原设计的主要不足不在宏观方向，而在“完成语义”不清：

- 历史快照与目标状态混写，后续实现后没有反向更新总稿。
- 设计把静态校验、可运行、可信、科学正确混在同一成熟度叙述里。
- acquisition 有详细相位，但 retrieval/evolution 缺与之对称的、可执行验收编号。
- `skipped`、无 sandbox 降级、验证等级和正式 registry 可见性之间缺硬状态机。
- 演化描述了“失败→技能”，却没有先定义事件、聚合归因、审批和回滚契约。

本规格通过 M0–M3、五维状态词汇和 REP/ACQ/RET/EVO/SYS 编号消除这些歧义。
后续代码诊断只按本矩阵给出 `verified / partial / missing / unsafe`，不再用“基本完成”
这类无法复现的结论。

## 8. 实现诊断记录

诊断日期：2026-07-13。诊断方法遵循 `diagnose` 的
“复现 → 最小化 → 假设 → 取证 → 修复 → 回归”顺序。先建立验收基线，再实现并
复验首轮 trust-boundary / retrieval 修复；本节同时记录修复前事实和当前状态。

### 8.1 总体裁决

**尚未达到最初设定的完整 skill 审计闭环目标。** 当前实现已经形成质量较高的
v2 静态表示底座，并关闭了 acquisition 的首要信任边界；后续纵切又完成了
trace-provable call workflow 的 facade-free 泛化与 8 域 routing oracle；最新纵切又完成了
RET-04 的 `.h5ad` 探针、三态评估和 `skill=auto` 执行前硬门，并由 RET-04b 把同一 Gate
接到显式 skill/shared runner；RET-05 又完成了 AnnData 候选兼容图、选定计划 DAG 与硬确认门；
2026-07-14 继续完成全候选 penalty、显式 non-AnnData artifact、受审 topo executor、
method-scoped output/binding 执行门、digest-bound 多维资源准入与 evolution substrate。M1 的任意
Python/复杂 lineage 泛化仍有限；M2 已补齐 CSV/VCF/FASTQ/目录的核心 bounded content probes，
2026-07-15 又由统一 Skill Execution Contract 补齐 shared runner 的结果信封、required keys、
无条件 Semantic artifact 和匹配 method-scope 动态验证，并把 security 的“未审/声明式已审”
状态一致传播到 registry/catalog/Desktop；`saves_h5ad` 现要求实际 Skill Python runtime
可读取一个 owned primary AnnData container。仍缺声明的 `obs/obsm/var`、shape/value 和科学
内容动态验证、代表性数据上的资源实测/全库校准、更多专用格式与净效用评测；M3 已闭合首个
`smoke-only -> demo-validated` earned promotion Backend 纵切，并由 EVO-06 补齐仅显式 demo
Skill defect 可触发的 `demo-validated -> smoke-only` 候选，以及绑定精确替代 Skill 的弃用审批；
EVO-G2 又完成 exact-source ordinary defect cluster → 非审批 draft → 人工结构化 narrative →
canonical `SKILL.md` Gotcha → full runtime context 的治理闭环，并对批准后 source drift 产生显式
review draft。这里的 exact source 持续绑定仅对 Gotcha 成立：promotion/demotion/deprecation 的 fresh
demo 已有 demo→manifest commit 的 point-in-time source fence，deprecation 事件也已绑定 target source，
但 promotion/demotion 的 durable approval 尚未保存 validated source/fresh validation event；批准后的
source 或 replacement drift 也不会自动形成持久 `review-required` 状态。参数修订仍未完成。
OmicsClaw-App 的既有薄审核 UI 不改变 Backend 权威边界；
Gotcha draft materialization 的 App 交互属于单独跨仓里程碑。

| 阶段 | 当前状态 | 验收判断 | 核心证据 |
| --- | --- | --- | --- |
| M0 表示 | **verified（v2 声明范围）** | REP-01–05 verified；REP-06 planned、未宣称完成 | 95/95 v2 manifest schema 有效，正式树 invalid v2 fail-closed；当前 dirty snapshot 有 15 个仅 serialization 非 canonical 的 manifest 漂移，未伪报为规范化；SKILL.md/parameters/version、catalog/DAG/routing 派生检查通过；shared runner 动态验证 result/artifact/method guarantees 及 `saves_h5ad` primary container 的 owned/readable 状态；registry/catalog/Desktop 一致区分 unreviewed 与 declarative security；AnnData 字段、值与科学内容动态验证仍是明确后续 |
| M1 获取 | **partial，已有可验收泛化子集** | ACQ-01–06 verified（ACQ-06 限 trace-provable call workflow）；ACQ-07 partial | quarantine/earned 状态机、显式 source identity、structured calls+steps、facade-free `run_skill`、2×2 复用、线性 two-call `step:1` 执行与 fallback evidence 已落地；任意 Python/复杂 artifact lineage 未覆盖 |
| M2 检索 | **partial（六分析域结构纵切 + governed executor）** | RET-01/02/03/05 verified；RET-04/04b substantial；RET-06 substantial partial | 29-case/8-domain oracle；全候选三态软惩罚；95-node/74-edge candidate graph；accepted/rejected review；method-scoped binding；CSV/VCF/FASTQ/directory bounded content probes；digest-bound one-shot、资源感知 topo executor、artifact propagation 与 cancellation/failure cascade 已落地；6 个真实 skill 已接入静态 compute baseline；更多专用格式、代表性数据资源实测/全库校准与 net utility 仍缺 |
| M3 演化 | **partial（validation + lifecycle + Gotcha 治理闭环）** | EVO-01–06 的 point-in-time approval 策略 verified；EVO-G2 窄 Backend workspace-authority 里程碑经 Round 18 `SHIP`（0/0/0/0）关闭；EVO-07 partial | 显式 demo、distinct identity 与 exact manifest evidence；Gotcha 和 deprecation target 绑定 source；fresh demo source fence、精确 replacement、人工批准、CAS、固定三门、staged lint、runtime retrieval、认证 HTTP surface 已落地；AutoAgent baseline 仅取 stage-zero tracked regular bytes，candidate raw seed/blob、pre/post execution state、durable clean/trial-open Git authority、accepted ref、cleanup 与 promotion/recovery 非目标 tracked 状态均 fail closed；atomic writer 报错后只对 stable-read 的 exact requested state 收敛为成功，不同/不稳定状态仍持久化 latch；这只证明 tracked evaluated program + target CAS，不等于 source open-world 文件封印、governed parameter revision/writeback、same-UID seal、power-loss durability 或 OS 原子事务；durable validated-source/event、批准后 validation/replacement drift 状态、参数修订和 Gotcha App materialization UI 仍缺 |
| 横切 | **partial** | SYS-01/02/04 partial；SYS-03 execution→evolution verified 子集 | runner event 贯穿 skill/version/hash/environment 且不再伪造 Run ID，原始路径/secrets 有脱敏回归；acquisition/control-plane Run identity 尚未贯通 shared runner |

这里的“未闭合”不等于代码质量差。定向测试证明当前已声明合同的实现大体稳定；问题是
合同范围小于完整系统目标，且少数降级策略跨越了应有的信任边界。

### 8.2 已验证能力

- `validate_skill_yaml --check`：95 valid / 0 invalid。
- catalog、SKILL.md、parameters、routing table、8 个 domain INDEX 与 description
  drift 通过同步检查；当前 dirty snapshot 的 15 个 manifest 只通过 schema，未通过
  canonical serialization gate，作为显式基线漂移保留。
- `audit_skill_requires --check`：0 missing dependencies；7 个 skill 有 extra 声明警告。
- 2026-07-13 初始验收快照中，表示、获取、corpus、promotion、resolver、routing、
  lint、help contract 和 bot adapter 的定向测试（`conda run -n OmicsClaw`）为
  **425 passed / 2 skipped（427 collected）**；它是历史基线，不是当前扩大选择集的
  验收数字。
- 另行复核动态 skill listing 与全入口 protocol：**14 passed**；修复了写死的 30-skill
  断言及 4 个 consensus shim 缺失的 `SKILL_NAME/SKILL_VERSION`。
- 95 个技能中 52 个有技能本地测试，43 个没有；PR CI 的真实科学 demo 目前只覆盖
  `spatial-preprocess`，所以不能把框架合同测试等同为 95 个方法学实现均已验证。

### 8.3 诊断假设结论

| 假设 | 结论 | 证据 |
| --- | --- | --- |
| H1：整体只达到强 M0 底座 + 部分 M1 | **演进后 M0 v2 声明范围已验收；M1 仍为受限泛化** | registry/fail-closed、统一输出执行契约及 security 跨层状态已补齐；AnnData primary container existence/readability 已补，字段/值/科学内容验证、复杂获取泛化、M2 净效用和 M3 产品闭环仍有缺口 |
| H2：获取路径存在“未验证也可正式可见”的信任泄漏 | **确认，已修复并回归** | `skipped` promotion 现移动至 `skills/.quarantine/`，不 refresh registry；证据写入 `references/quarantine.md` |
| H3：P2 泛化实际只是 P2a 标量字面量提升 | **诊断时确认；现已关闭 call-composition 子集** | loader 现消费 `skill_calls.jsonl`/steps；可证明 lineage 的 workflow 生成 facade-free `run_skill` 脚本并过 2×2；任意 Python 仍安全回退 |
| H4：检索仍是 domain-first sparse，结构条件未进入决策 | **大部关闭，仍缺 domain-margin 回溯/更多专用格式** | resolver 对全部候选保留 semantic score、三态评估与软惩罚；六分析域有显式 compatibility；method-scoped 条件边只有 matching binding 才进入计划；whole-plan executor 只消费受审边；CSV/VCF/FASTQ/directory content facts 已进入同一 evaluator |
| H5：P4 提示不等于演化闭环 | **首个 earned promotion 关闭，广义演化仍部分** | typed result/event、health ledger、具体 demo promotion patch、Backend 人工审批、CAS、固定三门、projection rollback 与 App 薄审核 UI 已实现；其他变更策略仍缺 |

### 8.4 问题分级

#### P0 — 正式库信任边界未闭合（ACQ-03/04，RET-06）— **已修复**

修复前，promoted 代码在 bwrap 不可用时不会于入库门中无隔离执行，但 gate 返回
`skipped` 后仍会移入正式 `skills/` 并刷新 registry；当时测试也固定了“不授予
demo-validated、但不阻止创建”的行为。普通 skill runner 不依据 provenance 为 promoted
代码补 OS sandbox，因此该状态会把风险推迟到用户首次运行。

现状态机为：`rejected → 丢弃`；`skipped promoted → skills/.quarantine/<domain>/<skill>`；
`earned → demo-validated + mvp + 正式目录`。quarantine 被 `.gitignore` 和 registry
discovery 同时排除，并持久化 gate 原因；resolver 同时过滤 draft/deprecated。

#### P1 — 治理字段存在但不约束运行（REP-01/05，RET-06）

- `LazySkillMetadata` 现在对正式 `SKILLS_DIR` 使用 `strict_v2=True`；外部/legacy 根目录
  仍保留兼容 fallback。
- registry 已传播 `origin/lifecycle_status/superseded_by/skip_when/validation_level`；
  draft/deprecated 保持可审计、可显式开发，但不进入自动 resolver。
- security 缺失现在明确传播为 `reviewed=false/enforcement=undeclared`；显式完整 block
  传播为 `reviewed=true/enforcement=declarative`。当前只有 3/95 个 Skill 完成首批证据化
  校准，且该字段仍不构成 OS 网络/文件系统隔离或执行 admission。
- 95 个现有技能全部是 `origin=human`、`lifecycle=mvp`、`validation=smoke-only`；治理
  维度尚未形成有信息量的分布。

#### P1 — 全域 routing truth 已落地，结构前置仍缺（RET-01–06）

修复前最小复现及当前结果：

```text
query: Use sc-clustering, but QC normalization HVG and PCA have not run yet
actual: exact_skill → sc-clustering (17.1)
contract: sc-clustering.skip_when 要求改用 sc-preprocessing
current: structured skip redirect → sc-preprocessing
```

除 skip_when 正/反极性与跨域 redirect 回归外，现新增
`tests/fixtures/routing_oracle/v1.json`：29 cases、8 domains、每域至少 3 条；评估器输出
precision@1、top-3 recall、domain/decision accuracy、hallucinated alias rate 和
precondition accuracy，并执行逐域路由门。当前 v1 路由与 precondition accuracy 均
1.000（alias rate 0.000），CLI 已接入 PR CI。
该结果只对版本化语料成立，不等价于线上真实请求 100% 准确。

路由预算 ceiling 同样已版本化并进 CI。RET-04/04b 的 auto + explicit 安全纵切与 RET-05
AnnData 候选兼容图/复合计划均已实现；2026-07-14 又加入 candidate-wide penalty、四个
非 AnnData 分析域 semantic artifact handoff、accepted/rejected review 与专用 topo executor。
§8.11 又补齐 method-scoped contract/binding，§8.12 补齐首批非 H5AD Content precondition；
`sc_batch` 收敛尚未完成；
不要先上 dense retrieval 掩盖结构契约覆盖不足。

#### P1 — 晋升已证明 bounded reuse，复杂泛化仍有限（ACQ-05/06）

loader 现优先读取 append-only `skill_calls.jsonl` 并读取 manifest `metadata.steps`；
`build_acquisition_abstraction` 用 AST 仅接受可证明输入 lineage 的 call-composition workflow，
生成显式 `run_skill` 脚本并移除 `oc/adata/show/ReturnAnswer`。source hash、原始 calls/steps、
参数绑定、lineage、applied/fallback reason 持久化到
`references/acquisition_abstraction.json`。结构化脚本被 gate 拒绝时会重写为 verbatim、
更新 deps/parameters 并重新 gate。

ACQ-06 已由生成脚本在两个输入 × 两组参数的 4 次运行验收，核对 runner 收到的新输入/
flag、最终 artifact 和 result envelope。边界仍明确：控制流、任意科学后处理、动态 skill
名和歧义 lineage 不做乐观转换；复杂分支、非 h5ad artifact 与 `run_id/thread_id` 一等
event identity 仍待后续。

#### P1 — earned validation 与 lifecycle 已闭环，广义演化仍未完成（EVO-01–07）

`SkillRunResult.error_kind` 与 classifier 已覆盖 dependency/input/timeout/resource/cancel/
script/contract/framework-validator/unknown；shared runner 写 privacy-minimal event，按 Skill
version/hash/environment 聚合，不再把 output directory leaf 伪造为 Run ID。EVO-G1 只允许精确
hash 的显式 demo 成功挣得 `smoke-only -> demo-validated` 候选，普通成功、重复 execution identity
或同版本 Skill defect 不会晋级；proposal id 幂等。Backend 深 governance Interface 内部派生
path/patch 并固定 representation/execution/retrieval 三门，人工批准时重新跑 shared-runner demo，
CAS 阻断陈旧/并发修改，失败恢复 manifest、catalog 与 DAG；认证 `/skill-evolution/*` 提供审计
snapshot/refresh/approve/reject。OmicsClaw-App 已实现只展示/收集人工字段的薄审核 Surface；
对账失败时冻结旧提案动作，运行时 health 仅匹配精确 id/version/hash。异常回滚现与投影快照/
刷新处于同一排他审批事务，避免并发审批擦除已成功投影；精确 hash 的 defect 在批准前、demo 后
及 retrieval 前重查，并在 projection refresh 返回后再查一次；最终 manifest 写入在 Linux/macOS
使用 atomic exchange CAS，缺少该原语的 host/filesystem 直接拒绝 guarded approval，不能用
合作式 sidecar lock 冒充对非协作外部编辑安全的 CAS；JSONL 锁覆盖 POSIX/Windows，无可用 OS lock
时 fail closed。
App 用单调 request epoch 阻止旧 snapshot 解除新 quarantine，并严格解析 snapshot、decision receipt
和 catalog。持久 proposal store 故障时文件会恢复但 durable 状态可能仍为 `pending`。EVO-06
现仅让精确 demo Skill defect 产生一步 validation demotion，让默认三条精确 Skill defect 支持一个
绑定 replacement version/hash 的弃用候选；批准会重跑失败 demo 或 replacement demo，registry、
catalog、LLM tool enum、auto resolver 与 shared runner 都消费弃用状态。普通失败、环境失败和
framework 失败不会自动降级。EVO-G2 已补齐证据绑定 Gotcha，但尚缺参数修订策略，且多文件提交
仍不是单一 crash-atomic filesystem transaction，故不能宣称完整 self-evolution。

#### P2 — 证据、隔离最小化和 CI 覆盖仍可加强（ACQ-03/04，SYS-01/04）

- `skipped promoted` 原因现已持久化到 quarantine evidence；其他非可信来源的 skip
  证据仍可进一步统一为通用 acquisition event。
- sandbox 对文件输入绑定其 parent 目录为只读根，扩大了对同目录兄弟文件的可见范围。
- PR CI 现已直接运行 `skill_lint.py --all`、routing budget、全域 routing oracle，以及
  acquisition/scaffolder/resolver/oracle 的 pytest 回归集合；真实 demo 仍只覆盖一个
  spatial 技能，后续应补 nightly 全域 smoke 和按变更域 fixture 两层。
- 13 个技能没有 trigger keywords；resolver 只能依赖描述/alias，降低弱表述召回稳定性。

#### P3 — 文档状态漂移

诊断前，ADR 0037 的正文已写 95/95 rollout complete，仓库合同也把 v2 定为 SSOT，
但标题状态仍是 Proposed/Draft；acquisition/retrieval 文档混合了旧 live-tree 快照和
目标状态；golden test 模块注释也残留已经变化的 WGCNA 路由描述。ADR 状态、文档角色
和完成真源已在本轮校准；后续每个阶段只在本验收矩阵中变更 verified 状态。

### 8.5 推荐实施顺序

1. **Trust boundary（已完成首轮）**：quarantine/draft 过滤、持久化 skipped evidence、
   显式 source identity、正式 v2 fail-closed。
2. **Retrieval truth（已完成）**：structured skip、lifecycle/validation、budget 和全 8 域
   oracle/precision/top-k/逐域门均已落地。
3. **Reusable acquisition（bounded 子集已完成）**：structured calls+steps、输入/参数/输出
   lineage、facade-free runner 与双数据双参数验收已落地；复杂 Python 泛化继续扩展。
4. **Evolution substrate（已完成纵切）**：统一 run event + typed errors +
   version/hash/environment health ledger。
5. **Human-gated loop（earned validation + lifecycle + Gotcha 已完成纵切）**：具体 patch synthesis、Backend
   审批 surface、CAS、固定三门、projection rollback 与 App 薄审核 UI 已落地；EVO-06 已扩展
   demo demotion 与 evidence-bound deprecation/replacement，EVO-G2 已扩展 exact-source Gotcha
   narrative；下一步是 governed parameter revision。Gotcha draft 的 App materialization 仍是独立薄客户端里程碑。

上述 1–2 已完成，3 有 bounded 子集，RET-04/04b/05、candidate-wide penalty、六分析域
compatibility、method-scoped binding、whole-plan executor 与 4–5 的 substrate 有可验收纵切；
复杂 acquisition、更多专用格式 content probes、广义 evolution policy 与 net utility 通过矩阵后，才可
宣称达到原始四阶段目标。

### 8.6 RET-04/04b 前置条件安全纵切复验

- 完整 `interface.inputs` 已从 v2 manifest 传播到 lazy metadata 与 registry。
- `.h5ad` backed 探针报告 `obs/var/layers/obsm/uns`、OmicsClaw modality/matrix contract，
  并以路径、mtime、size 签名做缓存失效；环境变量按名称只读进入 profile。探测失败直接 blocked，
  未验证 file type/modality 不再伪装 eligible。
- evaluator 覆盖 `eligible / needs_preparation / blocked`，并对 data shape、file type、
  modality、env、config 给出确定性 missing/reason；推荐上游只对有证明的映射发出。
- resolver 保留 semantic top-1，但 `execution_ready=false` 会令 AnalysisRouter 标记 preflight，
  `skill=auto` 在调用 shared runner 前返回准备建议；`mode=file` 的探测与执行使用同一 session 文件。
  route context 同步发出 do-not-execute 规则。
- oracle 增加 eligible/raw/incompatible 三态及 `spatial-domains` 自动 PCA 负例；后者推动修正
  SSOT 中错误的 `X_pca` 硬前置，避免合法自补算被误杀。
- RET-04b 将同一 evaluator 接到 shared runner、sync/async、agent 与 pipeline，并在输出目录/
  Run 分配和 subprocess 之前拒绝；空 session、无输入与缺失路径返回 structured failure。
- `path_kinds=file|directory|freeform` 对称约束并进入生成的 SKILL.md；全库审计固定 12 个目录
  consumer 和公开 file types。`InputProfile.path_kind` 统一 routing/execution 对普通点号目录与
  suffix-typed `.zarr/` 的判断。四轮 Ask Codex 只读复核最终 PASS（无 Blocker/High）。
- 边界：caller-supplied profile 仅作规划，config 仍需 surface 显式注入；2026-07-14 已在
  RET-04/04b 之上加入所有候选软重排、candidate DAG executor 和 §8.12 的 opt-in bounded
  content probes。未声明契约的 PDF/text 与更多专用格式仍只做 identity/type 判断，不能声称
  已完成全格式内容感知检索。

### 8.7 首轮修复复验证据

- 新增回归覆盖：promoted skip quarantine、registry 不发现 quarantine、earned→mvp、
  strict v2、governance propagation、draft/deprecated filter、validation tie-break、
  structured skip 正向/反极性/跨域 redirect、global latest 拒绝、surface 提示。
- 相关扩大回归（`OmicsClaw` conda 环境）：
  **425 passed / 2 skipped（427 collected）**。
- 动态 listing + 全 skill protocol 补充回归：**14 passed**。
- routing budget：8 项全部在 ceiling 内；当前全 bot tools JSON 42,501 / 45,000 chars，
  48 / 50 tools。
- 95 个技能 `skill_lint.py --all` 全部通过。

### 8.8 Acquisition 泛化与全域 oracle 追加证据

- acquisition structured trace/abstraction/facade-free/2×2 纵切：生成脚本四次复用均产生
  独立 `processed.h5ad` 与 `status=ok` envelope；另有 two-call 测试证明第二步实际读取
  `step:1` 输出，6 类不可证明 AST/trace 情形均直接落入 fail-closed fallback。
- routing oracle 独立里程碑当时为 26 cases / 8 domains；precision@1=1.000、top-3 recall=1.000、
  domain accuracy=1.000、decision accuracy=1.000、hallucinated alias rate=0.000；8 域逐域
  top1/top3/domain/decision 均 1.000。
- oracle CLI `scripts/evaluate_routing_oracle.py` 对全局阈值、逐域阈值和 fixture 合法性
  fail-closed；重复 query、多 coverage、decision/coverage 不一致、非法 domain/alias/threshold、
  partial 分支、幻觉 alias 与 exit 1/2 均有负向回归。含 analysis-router 边界的 CI 同构集合
  **228 passed**，新增 bot
  接线集合 **32 passed**。
- Claude Code 2.1.207 第一轮只读交叉审查未发现 acquisition/oracle 核心正确性或安全缺陷；
  其提出的 CI 未运行回归、oracle 负向覆盖、多步 lineage 覆盖、AST fail-closed 分支和
  benchmark-near trigger 风险均已按上述证据整改。
- 第二轮复审另发现宽松 `route/choose + analysis/pipeline` 会硬切 orchestrator；现改为只接受
  `which/choose ... skill`、`route this query/request` 或显式 `orchestrate` 元意图，并将 RNA
  velocity 与 scRNA differential-expression 两条 hard negative 写入 oracle。
- 旧 golden routing snapshot 已在 oracle 通过后有意重生成；其角色仅为行为漂移快照，
  不再把已知误路由当作期望行为真源。

### 8.9 RET-05 AnnData compatibility graph / candidate plan 复验（历史快照）

> 本节记录 2026-07-13 RET-05 独立里程碑当时的边界；其中边数、review overlay 与“确认后
> 放行计划内普通 skill”的语义，已被 §8.10 的 2026-07-14 governed executor 纵切取代。

- `interface.outputs` 已进入 lazy metadata 与 registry；`outputs.anndata.processing_state` 是
  `raw | standardized | preprocessed` 显式枚举。generic preprocessing 边只有声明
  `processing_state=preprocessed`、输出 `processed.h5ad`、producer/consumer modality 均已知且相交时才生成，
  不再从文件名猜处理状态。
- `skills/skill_dag.json` 当时为 95 nodes / 52 edges（32 exact、20 generic、6 reviewed、无环）；
  52 条仅分布于 singlecell/spatial，明确是 AnnData 纵切。全库 compatibility graph 可报告环，
  只有选定技能的 induced plan 才执行 topo/cycle fail-closed；生成边默认 unreviewed alternative，
  `skill_dag_reviews.yaml` 以完整 edge identity 做 governed overlay，stale review 直接失败。
- registry 暴露 upstream/downstream/topological/candidate-plan 查询；candidate plan 返回请求顺序、
  topo 顺序、phase、完整 edge provenance 与 unresolved pairs。无边双 intent 保留为 unresolved/parallel，
  不静默丢弃，也不伪造执行顺序。
- 复合计划以 SHA-256 digest 绑定 chat-scoped pending state；当时在严格 standalone 确认前，execution hook
  在 executor 前同时阻断 `omicsclaw` 与 `autonomous_analysis_execute`。确认状态跨普通 CHAT turn
  保留，新 analysis/cancel 替换或清除；确认后只放行计划内普通 skill（此行为已由 §8.10 的统一专用
  executor 门取代）。比较、解释、how-to 与普通科学
  描述中的 `and` 不生成执行计划。物理 executor=0 回归已接入 PR CI。
- 第三轮 Ask Codex 独立复审最终 **PASS（无 Blocker/High）**；定向回归 202 passed，routing oracle
  全部指标 1.000（hallucinated alias rate 0.000），95 manifests 有效且 DAG/catalog/SKILL.md 生成物干净。
- 非阻断边界：review overlay 尚不能显式 reject 派生边；standard spatial 五步 pipeline 仍缺非空
  induced edge 锚点；method-scoped `condition_scope` 尚无实例；topo 顺序执行、失败级联、pending TTL、
  unresolved plan 执行策略与其余 6 域 compatibility 建模不计入本纵切。

### 8.10 2026-07-14 governed retrieval / execution / evolution 纵切

- `interface.inputs/outputs.artifacts` 以 exact semantic kind、format 和真实相对 output path
  表示非 AnnData handoff；schema 拒绝未出现在 `outputs.files` 的 path。genomics、proteomics、
  metabolomics、Bulk RNA 形成 11 条 artifact edges；literature/orchestrator 仅有 terminal output。
- review overlay 升为 schema v2，接受 explicit `accepted|rejected`；rejected edge 从 graph 移除并
  进入 diagnostics。standard spatial 前向边已有 reviewed anchor。生成图为 95 nodes / 74 edges：
  43 exact、20 generic、11 artifact、18 reviewed、0 rejected、无环。
- resolver 对每个保留候选记录 semantic score、precondition status 与命名 soft penalty；blocked
  候选仍可解释，`execution_ready` 继续由独立执行门强制，105 项 resolver/precondition/router/oracle
  定向回归通过。
- `candidate_plan_execute` 只接收 gate 中的完整 plan，模型只能回传 digest 和 input mode；确认是
  one-shot，首次 await 前消费。普通 skill/autonomous 旁路即使在确认后也被 hook 阻断。executor
  只使用 accepted reviewed dependency，按 phase 并发、传播声明 artifact、检查缺产物、允许独立
  sibling 结束并向 descendants 级联 `upstream_failed`；unresolved 默认拒绝。确认后的旁路与
  digest mismatch 使用 hard deny；缺失声明产物会以 `candidate-plan-contract` 来源写入健康账本。
- `SkillRunResult.error_kind` 与分类器覆盖 none/dependency/input/timeout/resource/cancel/script/
  contract/upstream/unknown，legacy dict 保持不变。shared runner 将 fingerprint-only evidence
  写入 append-only ledger，按 skill id + version/hash + environment 聚合；隐私回归证明事件不保存
  raw path/secret。
- 重复技能缺陷或成功可产生带 support/counterexample event id 的 pending proposal；环境失败和取消
  不触发 Gotcha。proposal 未批准不写文件；批准必须有 human approver 与 representation/execution/
  retrieval 恰好三类 validator，失败写回 exact before bytes 并记 rolled_back；JSONL 与审批状态
  转换使用跨进程文件锁，避免多个 Surface 并发重复批准。
- 最终验收：95/95 manifest 合法，SKILL.md/DAG/catalog 生成物无漂移，95-node/74-edge DAG
  无环；29-case、8-domain routing oracle 全指标 1.000（hallucinated alias rate 0.000）；覆盖
  representation/retrieval/execution/evolution 与 legacy registry shape 的定向集合 378 passed。
- 此纵切关闭了 RET-05 记录中的 reject、spatial anchor、candidate-wide penalty、non-AnnData
  handoff、专用 executor、失败级联和 evolution substrate 缺口。当时仍开放的 method-scoped
  outputs 已由 §8.11 的后续纵切关闭，首批 richer content probes 已由 §8.12 关闭；其余开放项为真实多域科学
  demo/net utility、其他 proposal synthesis/App 审批 UI、
  acquisition→run→evolution 的统一 correlation identity；首个 earned validation policy 见 §8.15。

### 8.11 2026-07-14 method-scoped compatibility / bounded execution 复验

- `outputs.method_scopes` 成为 skill.yaml 的正式条件输出契约。每个 method 必须是
  `interface.parameters.hints` 的 canonical key；scoped path 必须存在于全局 output inventory；
  scoped AnnData 必须有 `saves_h5ad=true`；method 不得跨 scope 重叠，artifact kind 不得在
  global/scoped 中重复。
- DAG 对 scoped AnnData 与 semantic artifact 生成不可变
  `condition_scope: {source_methods: [...]}`；review condition 必须与派生值完全一致，不能擦除或
  发明条件。候选链只有在 producer method binding 匹配时才纳入条件边；未绑定返回
  `method_binding_required`，错绑返回 `method_scope_mismatch`。
- resolver 仅在一个 clause 明确命中唯一 param hint 时自动绑定，也允许调用方显式传 binding；
  binding 进入 plan digest。统一 executor 再次校验 scope 和 canonical method，然后才传递
  `--method`，因此 router 失误或陈旧/篡改计划不能靠条件边获得执行权限。
- 执行 phase 使用默认上限 4 的 semaphore；父任务取消会取消正在运行及等待的 phase task，
  `CancelledError` 继续传到 `arun_skill`，由 async subprocess driver SIGTERM/SIGKILL 并回收
  process group，不会被归类为 `script_defect`。
- 真实清单校准：`sc-velocity` 只在 `scvelo_dynamical` 下保证 latent time；
  `spatial-velocity` 只在 `velovi` 下保证其 latent-time 字段/层；`sc-preprocessing` 不再把临时
  目录里的 `input.h5ad` 声称为最终输出。图摘要显示 2 个 method-scoped skill、0 条 conditional
  edge；这是因为当前没有真实 consumer 声明这些 latent-time 前置条件，未为追求边数捏造关系。
- 生成图保持 95 nodes / 74 edges（43 exact、20 generic、11 semantic artifact、18 reviewed、
  无环）。该纵切关闭 method-scope 表示→编译→计划→执行的代码路径；richer content probing
  的首批核心格式见 §8.12，但仍不等于全域科学 demo/net-utility 或自动科学演化。
- 最终验收：相关表示/检索/router/计划执行/evolution/tool-surface 集合 336 passed；95/95
  manifest、SKILL.md/catalog/DAG drift check 和 ruff/diff check 全通过；29-case/8-domain oracle
  全指标 1.000。全量套件为 4298 passed / 69 failed / 19 errors，相比上一轮基线
  4278 passed / 71 failed / 19 errors 无净新增失败；剩余失败集中在并行控制面改动、OAuth/provider/
  context 合同与缺失 `pydeseq2/squidpy/scvelo/paste` 等可选科学环境，不作为本纵切通过证据。

### 8.12 2026-07-14 非 H5AD Content precondition 复验

- `interface.inputs.preconditions.content` 是 opt-in 的格式结构契约，包含 typed `tabular`、`vcf`、
  `fastq` 和 `directory` 四类；schema 拒绝空约束、未知 Directory signature，以及与公开
  `file_types/path_kinds` 不相交的死契约。生成的 SKILL.md 会显示这些前置条件。
- 公开 seam 仍只有 `probe_input_profile()` 与 `evaluate_skill_preconditions()`：CSV/TSV 读取首行
  列名/列数；VCF 读取 `##fileformat`、`#CHROM`、INFO/FORMAT ids 与 sample count；FASTQ 校验
  首记录并识别 sibling/显式 companion 的 R1/R2 layout；目录以最大 2048 entries、depth 3 的
  非 symlink walk 生成 governed semantic signatures。单文件读取最多 1 MiB 解压文本。
- evaluator 只消费 manifest 声明且与观察格式匹配的 content facts。确定的缺列、坏 VCF header、
  坏 FASTQ record/mate 或签名不匹配进入 `blocked`；未观察事实进入 `needs_preparation`；目录遍历
  截断不能证明缺失，因此不会硬拒绝。未声明 content 的历史技能保持原行为。
- 6 个真实 consumer 已接入：`bulkrna-de`（最小表结构）、`genomics-vcf-operations`（VCF 核心
  header）、`sc-fastq-qc`（FASTQ record/目录）、`sc-count`、`sc-velocity-prep` 与
  `spatial-raw-processing`（paired FASTQ 与受支持上游目录布局）。这不是只在测试 fixture 中存在的
  decorative schema bucket。
- 代码诊断同时复现两项执行合同偏差：`.vcf.gz` 可被扩展名/Gate 接受但脚本以普通文本读取；
  `filtered.vcf` 被声明为全局 semantic artifact，却只在阈值非零时生成。现已支持 gzip reader，
  且每次成功执行都物化行宽与 `#CHROM` 一致的 `filtered.vcf`；runner 集成回归覆盖该路径。
- 最终验收：表示、探针、evaluator、registry/router、DAG/executor、evolution、共享 runner 与 tool
  surface 的定向跨层集合为 468 passed；95/95 manifest、skill lint、SKILL.md/catalog/DAG drift、
  requires audit、compile、ruff（仅检查本里程碑新增代码）与 diff check 均通过；29-case/8-domain
  routing oracle 全指标 1.000。全量套件为 4314 passed / 69 failed / 19 errors，较 §8.11 的
  4298 passed 新增 16 个通过项，失败/错误计数不变；剩余项仍集中在并行控制面契约漂移及缺失
  `pydeseq2/squidpy/scvelo/paste` 等可选科学后端，不作为本纵切通过证据。
- 边界保持明确：CSV 的首行结构不能证明下游参数化前缀/数值语义；FASTQ 只做 bounded record
  sampling 与 filename/explicit-companion mate 证据；PDF、BAM/CRAM/SAM、mzML/mzXML、BED/FASTA
  等尚无内容探针。下一阶段仍应是资源感知调度与真实多域 plan net-utility，而不是把这些轻量
  probes 宣称为完整科学验证。

### 8.13 2026-07-14 Candidate plan 资源感知调度复验

- `resources.compute` 新增严格的静态准入合同：`cpu_cores`、`memory_mib`、`gpu_devices`、
  `threads`、`temporary_disk_mib` 缺一不可，`threads <= cpu_cores`。该合同从 schema 经 lazy
  metadata/registry 进入 compatibility graph，并随每个 selected skill 的 request 进入完整 plan
  digest；不存在未绑定资源的可执行计划。
- executor 同时消费 `resource_ready`、`missing_resource_requests` 与 request 全集。合同缺失、字段
  篡改、请求大于 runtime budget 均在创建 output root 和调用 runner 前 fail closed。host/operator
  budget 是 runtime state，不进入 digest；公开 audit 只报告 GPU 数，不泄露物理 GPU id。
- Analysis Router 对 resource-unready plan 仍显示拓扑、digest 与缺失 skill，便于补合同，但明确标记
  execution blocked，且不创建 confirmation gate，避免用户先授权再收到必然失败的 runtime 错误。
- 新增独立 `resource_scheduler` Module：以单个 async condition 对 CPU、内存、GPU、线程、临时磁盘
  和 process slot 做原子 FIFO admission，避免多 semaphore 分段占有造成死锁。GPU id 唯一分配；
  同一 runtime event loop 的并发 Candidate plan 共享 budget。等待中的 task 被取消会移除 ticket，
  运行中的 task 无论成功、失败或取消都释放完整 lease，且等待期间不提前创建 step output。
- runner Adapter 只允许 `CUDA_VISIBLE_DEVICES`、4 个常用 BLAS/OpenMP thread 变量与 output-local
  `TMPDIR`，任意环境键和越界 TMPDIR 被忽略；CPU-only reservation 显式隐藏 GPU。GPU identifier
  使用可安全传递的 allowlist，同时兼容 MIG device path。
- 首批静态预留基线覆盖 `sc-preprocessing`、`sc-clustering`、`sc-count`、
  `genomics-vcf-operations`、`spatial-preprocess`、`spatial-domains`；两个已有真实链
  `sc-preprocessing→sc-clustering` 与 `spatial-preprocess→spatial-domains` 均生成 resource-ready
  计划。其余 skill 不继承虚构的一核/零内存默认值，而是保持不可执行等待资源合同。上述 6 组数值
  尚未在代表性数据规模上实测，不宣称为峰值预测或科学性能基线。
- 定向跨层集合 421 passed；95/95 manifest/lint、SKILL.md/catalog/DAG drift、requires audit、
  compile 与本纵切新增文件 ruff 均通过；29-case/8-domain routing oracle 全指标 1.000，
  hallucinated alias rate 0.000，routing budget 未超限。全量套件为 4338 passed / 69 failed /
  19 errors / 24 skipped / 17 deselected / 2 xfailed / 3 xpassed；较 §8.12 的 4314 passed 新增
  24 个通过项，失败/错误计数不变。剩余失败/错误仍集中在并行 control-plane/context/provider
  合同漂移与缺失 `pydeseq2/squidpy/scvelo/paste` 等可选科学后端，不作为本纵切通过证据。
- 边界：当前是 event-loop-local admission accounting，不是 cgroup/容器硬限额；CPU 默认探测遵循
  process affinity，但内存探测未建模 cgroup，且全部静态 request 不随数据规模变化，temporary disk
  也不是 quota；尚无跨进程/remote scheduler、等待超时、
  运行中用量遥测或自动资源回归拟合。下一阶段应以代表性 demo 测量扩充资源合同，再做真实多域
  plan net-utility，而不是为追求覆盖率填写猜测值。

### 8.14 2026-07-15 统一 Skill Execution Contract 复验

- 新 `omicsclaw.skill.execution_contract` Module 位于 shared runner 的完成 Seam：只有 subprocess
  exit 0 且规范 `result.json` 信封、`result_json.required_keys`、无条件 Semantic artifact、实际
  method 对应 Method-scoped file/artifact guarantee 全部通过，runner 才生成 README/notebook、
  标记 Project completion 并记录成功。`outputs.files` 保持 inventory，不误罚合法可选分支。
- 契约路径必须是具体 output-relative path，并在 symlink resolve 后仍位于输出根目录。失败统一为
  `success=false/exit_code=1/error_kind=contract_failure`，保留原始输出用于诊断且只写一条隐私最小
  event；validator 自身异常 fail closed 为 `contract_validator_failed`。
- 修复 6 个 ad-hoc result producer、literature demo 的非 output-dir 数据写入，以及 2 个带
  `output_dir/` 文档 token 的 artifact path；orchestrator、literature 两个真实 demo 已通过同一
  runner，均生成规范结果信封及 runner-owned guide/notebook。
- security schema 不再为未审 Skill 填充看似安全的默认值。缺失 block 在 registry/catalog/Desktop
  中明确为 `unreviewed/undeclared`；显式完整 block 为 `reviewed/declarative`。当前 3/95 个真实
  Skill 完成首批校准；这不是 OS sandbox、网络拦截、写入限制或 acquisition 安全门。
- 执行契约/表示/检索/DAG/资源/演化/文档定向回归为 321 passed，execution-contract Module
  为 11 passed；12 个无条件
  Semantic artifact producer 与 orchestrator/literature 共 14 个真实 shared-runner demo 通过，
  builder 由隔离 CLI 回归覆盖；`validate_skill_yaml --check` 为 95 valid / 0 invalid。该里程碑关闭 REP-04 的共享运行时纵切和
  REP-05 的 security 状态跨层一致性，但不证明 95 个科学实现均已运行。
- 剩余边界：`outputs.anndata.saves_h5ad/obs/obsm/var` 尚未做运行后结构验证；required keys 的
  fixture/demo 分层覆盖仍可增强；92 个 Skill 尚无逐项 security review；声明式 security 仍未与
  bwrap/container/firewall 等真实 enforcement 或 admission policy 绑定。

### 8.15 2026-07-15 EVO-G1 earned validation governance 复验

- 新 `SkillEvolutionGovernance` 深 Module 收口 `refresh/snapshot/approve/reject` Interface；产品
  调用者不再提供 target path、patch 或 validator。低层 `EvolutionProposalStore` 的通用 apply
  降为私有 Implementation，关闭“三个 no-op callback 即可批准任意写回”的浅边界。
- `SkillRunEvent` 新增显式 `evidence_kind` 与非权威 `execution_fingerprint`；没有控制面 Run ID 时
  `run_id` 保持空值，不再使用 output directory leaf。旧 JSONL 可读但不会被追认为 demo 证据。
  `contract_validator_failed` 单列 framework failure，不再计入 Skill defect。
- 首个正式策略只支持 `mvp|stable + smoke-only -> demo-validated`：证据必须是精确 Skill
  id/version/manifest hash 的成功 demo，按 Run ID 或 fingerprint 去重；同版本存在 script/contract
  defect 则阻断。默认一条真实 demo 可生成候选，因为人工批准还会再次执行 demo；proposal id 按
  transition 幂等，普通 success 不触发。
- 人工审批先做 manifest hash/version CAS，再由固定 representation/execution/retrieval 三门验证：
  staged manifest 必须只升一级并含 evidence ref；execution 通过 shared runner 的统一执行契约；
  retrieval 刷新并复验 registry、`catalog.json` 与 `skill_dag.json`。长 demo 后再次比较 live bytes，
  最终写入再做 guarded CAS；Linux/macOS 以 atomic path exchange 关闭 compare/replace 窗，缺少
  原语时直接拒绝写回。并发修改标记 `stale`；失败恢复 exact manifest 与两个投影快照，并在 proposal
  store 可写时记 `rolled_back`。精确 hash 的 defect 在 approval、demo 后、retrieval 前及 projection
  refresh 后重查。
- Desktop Backend 新增带既有 Bearer policy 的 `/skill-evolution/*` snapshot/refresh/approve/reject
  HTTP contract；`/skills` list/detail 增加 `validation_level`、`superseded_by` 和诚实名称
  `readiness`，保留 `health` 作为文件就绪度兼容别名。独立 `OmicsClaw-App` 已按边界增加薄
  Next.js proxy、运行时校验的 TypeScript view model 和 review UI，不复制策略或直接写 manifest；
  decision 后无法读取权威 snapshot 时会冻结全部旧提案动作，直至因果上更新且解析成功；单调
  request epoch 阻止旧请求解除隔离，decision receipt/catalog 也在使用前严格解析。
- PR CI 已纳入 ledger/governance 与 Desktop Backend contract 测试。新增定向集合覆盖证据语义、
  去重/幂等、defect 阻断、一步晋级、stale race、demo 失败、projection rollback、真实 catalog/DAG
  Adapter、远端认证及并发成功/失败审批的投影隔离。投影 snapshot/refresh/rollback 现全部位于
  proposal 排他事务内，不再发生锁外二次 restore。
- 边界：EVO-G1 不包含 Gotcha/参数修订、demotion、deprecation/replacement；
  acquisition/control-plane Run ID 尚未贯通 shared runner；manifest + 两个 JSON 投影的
  异常路径可回滚，但不是抗进程 kill 的单一 crash-atomic filesystem transaction；当前 Bearer
  是共享 secret，`approved_by` 是认证调用者自报 audit label，不是身份系统验证的 human principal。
  若 proposal store 持续不可用，文件恢复后 durable proposal 仍可能是 `pending`，需运维修复和对账。

### 8.16 2026-07-16 EVO-06 demotion/deprecation/replacement 复验

- `refresh()` 仅对精确 id/version/hash 的显式 demo `script_defect|contract_failure` 生成
  `demo-validated -> smoke-only` 候选；普通 Run、依赖/资源/取消和 framework validator 失败不会
  降级。批准必须由 shared runner 再次复现 demo Skill defect，成功 demo 或非 Skill failure 均保持
  live manifest 不变。
- `propose_deprecation()` 只接受 canonical source/replacement id、人工 proposer/reason 与 ledger
  event ids，不接受 path/patch/validator。默认三条 distinct exact-hash Skill defect 才能形成候选；
  replacement 必须先挣得 `demo-validated` 或更高等级；其 id/version/hash 被写入 deterministic
  proposal，批准前后重跑 replacement demo 并多次复核精确快照。
- schema 要求 `deprecated` 必有 `superseded_by`、非 deprecated 不得携带该字段且禁止 self-loop；
  registry 进一步 fail closed 于 missing/alias-only/draft/deprecated replacement。`catalog.json` 同步
  投影 replacement。
- runtime 后果已统一：LLM-facing Skill enum 与 auto resolver 排除 draft/deprecated；显式提及旧
  canonical/legacy alias 会路由到 replacement 并带 supersession reason；shared runner 在 output
  allocation/process spawn 前阻断旧 Skill 并返回 replacement hint。Desktop list/detail 继续展示
  lifecycle/replacement，现有 App proposal parser 对新增审计字段保持 tolerant；本轮未修改 App repo。
- promotion、demotion、deprecation 复用固定三门、guarded CAS、projection rollback 与 ADR 0067
  journal/reconcile。Backend 新增 Bearer-protected
  `POST /skill-evolution/proposals/deprecation`；proposal creation 与 manifest/projection authority 仍
  全部属于 Backend。
- 当前扩大定向集合为 375 passed，95/95 manifest、catalog/SKILL.md/DAG 漂移检查、8 域
  routing oracle、编译、Ruff 与 diff check 通过。独立 Ask Codex 首轮只发现空白审计字段会把
  request-shape 错误映射为 `409` 的 Low；以 HTTP 模型层 strip+bounded contract 和六条负例修复后，
  全新 `gpt-5.5` session `019f6975-10c3-7163-a8a7-dae6f381a972` 为 0 findings、
  `VERDICT: SHIP`。该闭环不包含 strategic no-defect deprecation、Gotcha 写回或参数修订，也不把
  shared Bearer audit label 表述为已验证的人类身份。

### 8.17 2026-07-16 EVO-G2 evidence-bound Gotcha governance 复验

- shared runner 在 process spawn 前冻结同一快照的 manifest hash、environment fingerprint 与
  conservative project execution revision。resolved runtime entry 无条件纳入；Python/R/Bash source、
  target Skill 的 bounded JSON/YAML/TSV/template assets、真实 domain/subdomain `_lib`、全部 sibling
  Skill source+manifest、`omicsclaw/` runtime、`scripts/` 与 root `omicsclaw.py` 都被稳定读取。
  `lstat`/nofollow fd 双读、前后 inventory、symlink 拒绝和 manifest 前后 fence 防止 torn identity；
  Registry 还绑定 candidate 构建时解析的 exact manifest bytes，发生修改必须 reload。Python/Bash/R
  分别经 Python/Bash/Rscript dispatch，非 Python 不进入 adaptive Python probe，缺失解释器归为
  `missing_dependency`。该 revision 安全偏保守，会因 unrelated sibling source 产生 false-stale；
  未声明的 CSV/TXT/binary 等 open-world runtime assets 仍可能漏掉，显式 `runtime.assets` 是后续 P0/P1
  表示合同。sync/async 正常完成、output-contract failure 与 driver exception 均复用冻结快照。trace evidence 只保留 canonical runtime
  entry basename，不把 traceback 中的用户文件名写入 ledger；完整 traceback path 必须先解析为精确
  canonical entry，同名外部文件不能伪造 anchor。`result.json` 任意 top-level key 不再进入 ledger。
  environment fingerprint 现绑定实际选定 executable 的内容/选择 identity，并由该 Python producer 在
  与 Skill 相同的 env/cwd 下探测 OS/Python/prefix 与声明依赖的实际版本；missing/unknown 会 fail closed。
  它不覆盖全部环境变量、传递/native dependency、driver 或未声明 runtime asset，非 Python 也只有有限
  executable/host evidence，因此仍不是完整 lockfile。
- `refresh()` 只从 exact id/version/manifest/source/environment 的 ordinary
  `script_defect|contract_failure` 中，以默认三条 distinct execution identity、共同 entry-file line
  anchor 和至少一条 exact-source success counterexample 生成不可审批 `gotcha_evidence:draft`；support
  与 counterexample identity 必须不相交。generic result envelope key、demo defect、consensus、依赖/
  资源/超时/取消/framework/unknown failure 不触发 Gotcha。旧低层 generator 不再生成第二套 Gotcha。
- 专用 fail-closed Bearer boundary 保护全部 `/skill-evolution/*`：token 未配置/空白即 503，missing/wrong
  credential 为 401，且不改变其他 remote route 的 local-first 约定。`POST /skill-evolution/proposals/gotcha`
  只接收 canonical Skill、proposer/reason、
  support ids 与结构化 `lead/condition/guidance/anchors`。Backend 从单次 manifest payload 派生 hash、
  path 与 source；拒绝任意 path/patch/hash/validator、Markdown/HTML、Unicode control/line separator、
  token-independent generic URI、跨标点 POSIX/UNC/Windows 绝对路径、NFKC scan-only security view 下的
  compound credential-key assignment 与 Markdown boundary underscore；同时保留 `HLA_DRA`、`p_value`
  一类科学标识符。所有 request model 对未知字段
  返回 422 而非静默丢弃。evidence candidate 与 narrative proposal 使用不同 ID；
  entry digest 形成可修订 proposal identity，pending revision 不被覆盖，rejected/rolled-back wording 可
  以新 ID 修正。
- canonical write target 仅为 `SKILL.md` 的 `## Gotchas`：固定单行 bullet、placeholder removal、
  duplicate refusal，不修改 `skill.yaml`。representation 在 live write/demo 前对 staged 完整文档执行
  generator idempotence、exact anchor 与 full targeted lint；execution 跑 fresh shared-runner demo；
  retrieval 以 fresh registry 验证完整 condition/guidance/evidence 已进入 runtime Skill context。
  Gotcha 不投影到 catalog/DAG，因此审批、失败回滚和 reconcile 均不触碰这两个无关文件。
- guarded CAS、ADR 0067 journal/reconcile、missing-target conflict 与 exact-byte rollback 继续复用。
  registry reload 对 missing/non-directory/empty root、unresolved runtime、manifest inventory 缺口与
  canonical/alias collision 均在 publish 前 fail closed，旧 `_state` 对象保持；有效 candidate 才以单一
  copy-on-write state publish。manifest/source/target path/content drift 会把旧 draft/pending 标 stale；
  已批准 Gotcha 遇到 lifecycle/type/source/target/manifest 的变化或缺失会产生 linked、non-approvable
  `gotcha_review:draft`。stable cluster identity 避免审批自身写入后立即重复提名同一 exact-source cluster。
- 复审暴露的相邻 authority seam 也已收口：plan schema v2 绑定每个 selected Skill 的 id/version/
  manifest/source、同一 review snapshot 构造的 selected graph hash、method binding 与完整静态 compute
  reservation；default executor 在 output root 创建前及每步前后重建 authority。resource request 即使连同
  plan digest 重哈希也不能降配；method 同时要求 profile、统一 `--method` flag 与受控 AST argparse
  choices，真实 `spatial-velocity/velovi`、`sc-velocity/scvelo_dynamical` 正例通过，错误的
  `sc-integrate-cluster/default` 在 plan gate fail closed。Round 5 又证明 direct shared-runner 曾从 raw
  extra args 派生 method；现已改为先过滤再派生，并在 output/spawn 前显式拒绝不受支持的
  `sc-clustering --method tsne`。runner 的最终 source
  fence 位于 Project `completed` 持久化之前；该持久化是终态边界，不再生成 completed index + failed
  ledger 的矛盾状态。promotion/demotion/deprecation fresh demo 的 source revision 经 planned-after hash
  一直 fenced 到 manifest publication 和 durable approval；deprecation support/counterexample 精确绑定
  current target source，replacement source 同样在 demo 与 final commit 前后复核。
- 真实公共接口 tracer 不再手工构造 event 或替换 execution adapter：临时严格 v2 Skill 由 shared runner
  产生三条 ordinary defect、一条 counterexample 与 fresh approval demo 的合法 result envelope，并以默认
  Backend factory 贯通 Bearer HTTP → ledger → evidence draft → human materialization → approve →
  canonical SKILL.md → fresh registry → runtime context，并证明 manifest/catalog/DAG byte-stable、完整审计
  字段可从 snapshot 回答且 refresh 幂等。首轮独立 `gpt-5.6-sol` Ask Codex session
  `019f6af5-2fd5-7422-b2d8-fe71921611cf` 以 2 High/5 Medium `NO SHIP` 暴露鉴权、Registry、隐私、drift、
  fake tracer 与文档缺口；上述问题均已增加对抗回归并修复，详情见
  [EVO-G2 review record](2026-07-16-evo-g2-ask-codex-review.md)。第二轮 `gpt-5.6-sol` session
  `019f6b23-e500-7e40-a256-c107b20f5291` 又以 1 High/1 Medium `NO SHIP` 复现 nested single-cell
  domain `_lib` 漏哈希和标点/credential/Markdown narrative 绕过；现已以真实 `scrna|scatac` 闭包、
  pending stale、approved review 及 policy+HTTP 422 对抗回归修复。第三轮 session
  `019f6b41-1ac4-7123-9255-11f877cc602a` 又以 2 Medium/1 Low `NO SHIP` 复现 canonical root、initial
  Registry partial publication 和 nondeterministic traversal；整改后全路径原子 publish、canonical root
  与排序回归均通过。第四轮 `gpt-5.5` session `019f6bbd-a5fd-7121-8a53-44dda59b9755` 以 1 Blocker
  `NO SHIP` 发现 executable plan 使用 cached DAG、graph revision 却来自 fresh reviews 的 mixed authority；
  现已改为一份 review bytes + frozen Registry 构造 graph、plan 与 revision，并加入 submitted-payload
  自证及 execution-time 重验。随后本地对抗审计发现 resource、method、runner terminal、approval source
  fence、Bash/assets 和 deprecation old-source 七类问题，均以可复现 RED→GREEN 回归关闭。

  第五轮 `gpt-5.6-sol` session `019f6c10-c5d2-7923-8fc4-86bb5b431fec` 以 1 High/1 Medium
  `NO SHIP` 复现 direct runner method 双事实源和空格/全角 credential assignment 绕过。整改后
  58-case RED 集、完整 runner/evolution/Desktop 339-case 集、扩大的 DAG/plan/capability 444-case 子集与
  最终跨层 951-case 集全绿，相关 Ruff 与 diff check 通过。95/95 manifest validation/skill lint、
  0 missing requires、全库 SKILL.md/parameters/version、catalog、95-node/74-edge DAG、routing surfaces、
  8 域 oracle、budget 与 compileall 通过。另有 15 个既有 dirty manifest 仅 canonical serialization
  gate 不通过，且 committed orchestrator SKILL.md 缺 count-generator marker；两项不被误报为 green，
  也不在本纵切覆盖用户改动。此前 4831 项 repository-wide run 在 optional spatial-registration native
  stack segmentation fault，且含无关 control/provider/science failures，不作为 green gate。第六轮
  `gpt-5.6-sol` session `019f6c36-0122-75b1-b45c-7d488c165722` 在中断、无最终 verdict 前已复现
  pipeline alias 提前 dispatch 静默吞掉全部 `extra_args`，以及 `SECRET_KEY` credential family 漏检。
  已将已知 pipeline 的未声明参数在 preflight/output/step 前统一 fail closed，未知 alias 仍返回
  `Unknown skill`；中央 Gotcha policy 增加精确 `secretkey` family 而不泛化为误伤科学字段的通用
  `key`。定向对抗集 75 passed，完整 pipeline/argv/runner/evolution/Desktop 文件 308 passed，
  扩大到 execution contract、DAG/plan/capability、Registry、precondition、scheduler 的 14 文件
  跨层集合为 563 passed；相关 Ruff、compileall、11 项文档契约及 diff check 通过。
  第七轮 `gpt-5.6-sol` session `019f6c47-b0f1-79b3-84a8-03ed13b98609` 以 1 Medium
  `NO SHIP` 证明 path-shaped pipeline alias 可逃逸 canonical `pipelines/` root；现已用 bounded
  lowercase kebab alias、resolved-root containment、外部 symlink 过滤及 config 内部 path-independent
  字段约束关闭。随后本地对抗审计发现更高风险的 Registry nested mutation：已发布 state/snapshot
  曾共享可变 dict/list/set，可在 manifest/source revision 不变时削弱 confirmed plan 的 output contract。
  现 execution-authority metadata 递归发布为 `MappingProxyType + tuple + frozenset`，初始态、invalidate、
  full/lightweight load、reload 与 embedding snapshot 共用 freeze boundary；DAG cache 移出 authority state，
  shallow proxy/`dataclasses.replace()` 不能继承 publication 身份。发布后 snapshot 以 exact-state identity
  作 O(1) 判断，95-Skill 实测约 0.0033 ms/call，不再每次递归扫描约 9.7 ms。
  后续对抗审计继续关闭了 pipeline 直接构造绕过、duplicate/unknown YAML schema、noncanonical Registry
  step、缺失/目录/symlink/越界 baton、跨 step Registry/source 混合版本，以及 review overlay 改写后 DAG
  cache 仍返回旧图的问题。pipeline 从一个 frozen snapshot 绑定完整 revision map，leaf 在 runtime resolve/spawn
  前校验，summary 持久化 schema、authority digest、bound revisions 与逐 step audit identity；成功但缺失/不匹配
  identity 会把 composite 判失败。
  真实 subprocess 又复现显式复用非空 output 时，旧 `status: ok` 可掩盖本次 exit 7 并让旧 artifact 满足
  contract。现按 [ADR 0070](../adr/0070-require-fresh-exclusively-claimed-run-output-directories.md) 在 shared
  sync/async prepare、pipeline root/leaf、candidate-plan root/leaf 统一要求 absent/empty directory + mode-0600
  `O_EXCL` durable claim；不删除用户文件，claim 不进入 public files/README，前一 step 事后污染未来 sibling
  也会在其 spawn 前拒绝。本地独立 adversarial reviewer 最终为 0 Blocker/High/Medium、322 passed/1 skipped；
  它不替代 Round 8 Ask Codex 终审。
  第八轮 `gpt-5.6-sol` session `019f6cb1-1cce-7860-a39f-fe29a4b8f2d4` 在本地只读 probe
  阶段被平台 safety filter 中止，无 final verdict；但其候选 finding 已独立复现：snapshot 公开字段可与
  published state 脱绑定、custom Candidate runner 可替换 claimed output root、claim/目录/越界 symlink
  可伪装 file artifact，Desktop/Memory 仍会暴露内部 marker。现 schema 保留 marker，legacy runtime 与
  Candidate handoff 只接受 claimed leaf 内的真实文件，runner result root 必须与 leaf 一致，snapshot
  构造验证所有字段精确绑定 published state，公开 inventory 统一过滤 marker；相关 284 passed/1 skipped。
  第九轮 session `019f6cca-d53c-7f92-a5fe-51da92cad6db` 以 1 High/2 Medium `NO SHIP`
  证明 pipeline 仍可把 claim marker 当 baton、snapshot `loaded_dir` 仍以 equality 而非 identity 绑定，且
  schema file path 对反斜杠形式未统一保留。现共享 predicate 统一路径分隔符，pipeline config + runtime
  baton 双门拒绝 marker，snapshot 要求 exact published `loaded_dir` object；相关 337 passed/1 skipped，
  95/95 schema/lint、catalog 95、DAG 95/74、目标 Ruff 与 diff check 通过。
  第十轮 `gpt-5.6-sol` session `019f6cdb-3a5a-77a1-a01f-c13d5c89f11e` 以 0 High/
  1 Medium `NO SHIP` 证明 Candidate handoff 仍可通过 leaf 内 symlink alias 消费 claim marker。
  现 runtime claim identity 同时检查 lexical/resolved name 与 inode，统一 scientific-output predicate
  还要求 contained regular file 和 `st_nlink == 1`；Candidate、execution contract、runner/pipeline、
  Memory/Desktop/report/notebook、AutoAgent、acquisition、manifest/completion 与 consensus evidence
  已复用同一事实。相邻审计又补齐实际 Skill runtime 的 primary AnnData 可读性、acquisition modern
  producer/completion authority、AutoAgent known-zero/recovery、consensus shape/Memory schema 与 Desktop
  Run inventory/freshness。合作式 claim 仍不是同 UID tamper seal，check/read 也不是 filesystem
  transaction；该边界已写入 ADR 0070。第十一轮 `gpt-5.6-sol` session
  `019f6d63-bfb0-7b53-979c-e4091a06873e` 以 6 Medium `NO SHIP` 发现 writer ancestor、pipeline
  summary、`python -P` 空路径、AutoAgent 固定 AnnData、Desktop alias 与 remote jobs-root 六类缺口；
  修复后第十二轮 session `019f6d84-e3cf-7140-811e-b400a458d548` 又以 4 Medium/2 Low
  `NO SHIP` 证明 `symlink/..` 归一化、重复 Backend `PYTHONPATH`、Registry fail-open、Desktop
  sidecar、symlink Project 和目录清单仍有派生证据问题。现 writer 在归一化前检查原始组件，
  verifier 清除所有 Backend-root 等价/空路径，AutoAgent 对 unknown/ambiguous/error 走 result-only，
  sidecar/Project/output-guide/remote read 均 fail closed 于相关 alias，且 remote GET 不再通过 alias
  创建外部目录。合并聚焦集 671 passed/1 skipped，扩大四阶段集合 2016 passed/3 skipped；95/95
  manifest、catalog 95、DAG 95/74、全生成物、requires、lint、routing budget 与 8 域 oracle 均通过。
  第十三轮 `gpt-5.6-sol/xhigh` session `019f6db9-8376-79f1-ab7b-55291db5a237` 以 0 Blocker/
  0 High/2 Medium/1 Low `NO SHIP` 证明 AutoAgent child Registry authority 可与父级 snapshot 分离、
  `project_meta.json` alias/non-mapping 可污染 Project authority，并指出 `PYTHONPATH` alias loop。
  Round 14 已统一 frozen Registry/Project path authority，并进一步令 baseline/candidate 共享 hard gates、
  证据重建或持久 trace 失败即拒绝；receipt 绑定 exact Skill/version/manifest/source/environment/runtime、
  claim/result digests，默认 edit surface 限 target narrative `SKILL.md` 与 primary Python entry。实际 producer
  probe 在 spawn 前由选定 executable+env+cwd 生成 environment identity，claim 再原子绑定该 audit identity；
  probe/binding 失败时 Skill 不 spawn。filesystem alias policy 同时覆盖 POSIX symlink 与 Windows reparse/
  name-surrogate。accepted patch 先持久化 artifact/manifest 再以 `git update-ref` CAS 推进，source promotion
  原候选要求精确 durable record、stable inode/digest、no-clobber install、rollback 与 interruption journal；但
  第十四轮 `gpt-5.6-sol/medium` session `019f6f25-3e76-7dd3-89e3-8d98c072010b` 仍以 0 Blocker/
  0 High/1 Medium/0 Low `NO SHIP` 证明实现只检查 accepted branch head，未认证持久 manifest/patch，且
  manual promotion endpoint 仍信任可变 result 文件列表。整改后 promotion 在任何 journal/source mutation 前逐提交认证完整
  linear accepted chain 的 canonical manifest 与 exact patch artifact，最终 supplied record 必须与 durable record
  完全一致，文件集从 baseline→accepted head Git 状态派生；该 endpoint 不再使用 result 中的
  `accepted_files` 或 `accepted_patches` 作为 promotion authority。整改当时的合并定向历史快照为
  743 passed/1 skipped。
  Round 14 后的本地 TDD/对抗审计（不冒充外部 Ask Codex finding）进一步证明旧 pre-CAS 路径未把
  PatchPlan 确定性绑定到 candidate Git bytes，恢复路径也可能从同摘要异 inode 的未绑定 stage 推断
  ownership，或在 parent preflight 后跟随替换目录。现严格要求 canonical/unique target_files 与 diffs
  精确相等、干净 registered worktree、unchanged regular mode 和 hunk→blob 重放；tree/commit 直接构造，
  完整 record+plan evidence trailer、candidate-chain 预认证、标准 accepted ref lock、post-CAS 逆向 CAS、
  UTF-8 与 iteration 单调性均已落地。promotion journal 持久绑定 expected mode、stage/installed inode 与
  source root→immediate parent identity；link→unlink 只接受 journal-bound exact two-link inode，cleanup
  在删除 backup 前及返回前重验 target，每个 path mutation 前重验 parent chain。同摘要异 inode、第三
  hard link、chmod、parent symlink/plain replacement 均 fail closed 并保留 recovery evidence。
  随后的内部只读复审以 2 Medium/1 Low 发现 whitespace-normalized hunk 多义位置会选首项、source
  executable class 未绑定 authenticated Git tree、stage 名消失后 applied journal 未强制
  `stage_identity == installed_identity`。现 exact-first 共享 matcher 对多 exact/normalized occurrence
  fail closed；baseline/accepted tree 各自严格解析唯一 `100644/100755` 且按 boolean executable class
  约束 source/journal，同时保留完整 `0600/0700`；applied/interrupted recovery 均强制两 identity 相等。
  随后 Round 15 Ask Codex（`gpt-5.6-sol:medium`，session
  `019f6fd1-9eec-7670-8bfb-5e8015f84529`）以 0 Blocker/0 High/1 Medium/0 Low
  `NO SHIP` 指出 ignored candidate-state 可不在 Git status 或 accepted tree 中，却在
  evaluation 期间被消费。整改后 baseline 在干净 detached `iter_0000` 中执行；trusted
  `apply_patch` 后、任何 candidate 试运行前冻结 raw inventory，并在执行后/pre-CAS
  精确重验路径、类型、inode、mode、nlink、size、mtime/ctime 与摘要；同时见证
  common `.git` control tree、worktree marker 和持久 Git config authority。任何 drift 都写入
  durable compromise latch，阻断后续 Git、rehydrate 与 promotion；worktree 删除和注册
  cleanup 也是 promotion 前必须成功的 fail-closed gate。`AcceptedPatchRecord` 另外绑定
  canonical source root 与 `(st_dev, st_ino)`，拒绝 foreign root 和同路径 inode 替换。
  该轮修正后精确扩大聚焦集合为 951 passed/1 skipped，13 个表示/生成/routing gates
  全通过。Round 16 首次会话被安全过滤中断、没有 verdict；收窄后的 Round 16b
  (`gpt-5.6-sol:medium`, session `019f7057-807a-7602-85da-828c74c3a08b`)
  以 0 Blocker/0 High/2 Medium/0 Low `NO SHIP` 证明 source copy-all baseline 会摄入
  ordinary/info/global-excluded untracked 文件，且 marker 写失败后缺少完整 durable Git authority。
  当前整改仅从 stage-zero tracked regular inventory 的当前稳定字节构造 baseline，批量验证 index
  OID 为现存 blob，并用 no-filter tree/checkout raw blob OID 认证关闭 EOL/filter/
  `working-tree-encoding` 转换；non-Git、symlink、gitlink、unmerged、missing/alias 均 fail closed。
  Git authority 改为 bounded `clean`/`trial_open` 状态机，`open_existing()` 只认证不 reseal，
  crashed trial/旧 output/外部 Git drift 均拒绝；clean 只在双快照、schema 与真实 accepted ref
  认证后作为最后 commit point 发布。promotion 在初始、install 前后、cleanup 后及 interrupted/applied
  recovery 重验 HEAD/index/全部非目标 tracked bytes，目标由 journal/CAS 单独认证。核心集合为
  185 passed；同一 24 文件扩大范围为 976 passed/1 skipped，仅有两条既有第三方 warning。
  Round 17 (`gpt-5.6-sol:medium`, session `019f70f1-6855-7803-b2ab-992ab05195bf`)
  以 0 Blocker/0 High/1 Medium/0 Low `NO SHIP` 确认上述 acquisition、seed、authority、
  accepted-ref、promotion/recovery、transaction chain 与 HTTP ordering 均成立，但发现
  `os.replace(clean)` 已可见而后续 directory `fsync` 报错时，cleanup 会误判 checkpoint 失败；
  若 marker 又在 replace 前失败，重启仍会接受已可见 clean。整改后 state writer 仅在 bounded/
  no-follow/single-link stable read 与 requested canonical bytes 完全相等时，把该 outcome-unknown
  收敛为成功；不同/缺失/alias/不稳定状态继续抛错并要求 durable latch。正反两个 post-replace
  注入回归分别证明 exact clean 可重开、different state 必须拒绝。刷新后核心 187 passed，
  同一 24 文件扩大范围为 978 passed/1 skipped；11 项文档契约和 13 项表示/routing gate 全通过。
  Round 18 (`gpt-5.6-sol:medium`, session `019f7112-e7dc-7531-96b7-b7d879158e44`)
  以 0 Blocker/0 High/0 Medium/0 Low `SHIP` 确认 properties 1–8、Round 17 Medium
  及 Round 16b 两个 Medium 全部闭合，并独立复跑 157/157 scoped tests。由此只关闭 EVO-G2
  窄 Backend workspace-authority 里程碑；M3 及四阶段系统总体仍为 partial。
  这仍只证明 tracked evaluated program + target CAS；source untracked/open-world 文件、同 UID writer、
  cooperative check→pathname mutation、HTTP session restart/TTL discovery、`dirfd/openat`、OS sandbox
  与 crash/power-loss 原子性均不在保证内。
- 边界：普通失败输入没有 durable replay descriptor，fresh demo 证明 governed baseline 仍健康，但不
  声称重放了每个失败输入；project revision 是保守 point-in-time identity，可能因无关 sibling source
  变更产生 false-stale，也不覆盖未声明的 open-world runtime assets；promotion/demotion durable approval
  尚不保存 fresh validated source/event，批准后 validation 或 replacement drift 不自动产生持久复审状态；
  profile→argv 尚无显式 manifest binding；shared Bearer label 仍不是 verified human principal。
  fresh-output claim 是合作式本地 filesystem ownership，不是 durable Run Assignment、restart/resume 或 OS sandbox。
  OmicsClaw-App 尚未增加 Gotcha draft
  materialization form；该 UI/TypeScript contract 应作为独立薄客户端里程碑，不得复制 Backend 策略。
  producer probe→claim binding→spawn→later read 不是同一个 OS/filesystem transaction，同 UID writer 仍可
  制造漂移；非 Python producer evidence 有限，ExperimentLedger 也不是跨进程/hash-chained 防篡改账本。
  source promotion 的最后一次 parent identity check 与 pathname syscall 之间仍有 OS 级 TOCTOU 窗口；
  Run index 不按 manifest mtime 自动 lazy rebuild，全局 Run lookup 假定 Run ID 唯一。参数修订、任意代码
  自修复、自动审批、完整环境 lockfile 与 OS 隔离仍未实现。

---

OmicsClaw 是多组学研究与教育工具，非医疗器械，不提供临床诊断。工程验收不能替代
领域专家对方法学和科学结果的复核。
