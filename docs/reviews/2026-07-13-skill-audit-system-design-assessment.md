# Skill 审计系统：设计基线与验收规格

> 状态：验收基线 v1.1（2026-07-13，含首轮修复复验）
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
action: scaffold|promote|validate|run|route|revise|deprecate
actor: human|agent|ci|system
origin: human|scaffolded|promoted|migrated|corpus
run_id: optional-explicit-run-id
thread_id: optional-session-or-project-id
source_ref: optional-durable-reference
outcome: passed|skipped|failed
error_kind: optional-typed-error
evidence: durable-artifact-references
before_hash: optional
after_hash: optional
```

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

当前状态（2026-07-13）：RET-05 已在 **AnnData compatibility 纵切**内 verified。全库生成图
包含 95 nodes / 52 edges，其中 singlecell 28、spatial 24、其余 6 域为 0；因此不能把该
验收外推为“8 域 workflow graph 已完成”。

### 5.4 演化（EVO）

| ID | 验收项 | 通过证据 |
| --- | --- | --- |
| EVO-01 | 运行结局有 typed error_kind | 分类器正反例 + 未知兜底 |
| EVO-02 | health ledger 区分技能缺陷、环境缺陷和用户取消 | 聚合夹具测试 |
| EVO-03 | 重复成功/失败能生成带证据候选 | promotion/Gotcha proposal 测试 |
| EVO-04 | 未审批候选绝不写回正式技能 | 审批边界集成测试 |
| EVO-05 | 批准写回后自动重验，失败可回滚 | before/after hash + 回滚测试 |
| EVO-06 | 降级/弃用对检索和 surface 生效 | 端到端状态消费测试 |

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
接到显式 skill/shared runner；RET-05 又完成了 AnnData 候选兼容图、选定计划 DAG 与硬确认门。
M1 的任意 Python/复杂 lineage 泛化仍有限，M2 仍缺 candidate-wide penalty 与非 AnnData
结构覆盖，M3 仍只有重复成功提示原型。

| 阶段 | 当前状态 | 验收判断 | 核心证据 |
| --- | --- | --- | --- |
| M0 表示 | **partial，接近可验收** | REP-01–04 verified；REP-05 partial；REP-06 planned | 95/95 v2 manifest 有效、规范化且派生物同步；正式树 invalid v2 fail-closed；registry 已传播 origin/lifecycle/validation/skip，security 跨层一致性仍待补齐 |
| M1 获取 | **partial，已有可验收泛化子集** | ACQ-01–06 verified（ACQ-06 限 trace-provable call workflow）；ACQ-07 partial | quarantine/earned 状态机、显式 source identity、structured calls+steps、facade-free `run_skill`、2×2 复用、线性 two-call `step:1` 执行与 fallback evidence 已落地；任意 Python/复杂 artifact lineage 未覆盖 |
| M2 检索 | **partial（retrieval truth + AnnData 结构纵切）** | RET-01/02/03/05 verified；RET-04/04b verified 子集；RET-06 partial | 29-case/8-domain oracle 接入 CI；resolver 消费 lifecycle/validation/skip；`.h5ad` 探针与统一执行门已落地；95-node/52-edge AnnData compatibility graph、selected-plan DAG/provenance 与 digest-bound confirmation 已落地；candidate-wide penalty、其他 6 域边与更多内容探针仍缺 |
| M3 演化 | **missing（信号原型）** | EVO-03 partial；其余 missing | 记录 autonomous 成败并在同 thread 第 3 次相似成功时提示晋升；无 typed error、health ledger、审批写回与重验 |
| 横切 | **partial** | SYS-01/04 partial；SYS-02 未专项验证；SYS-03 missing | 静态门与定向测试充分，但没有贯穿 acquisition→run→evolution 的统一事件身份 |

这里的“未闭合”不等于代码质量差。定向测试证明当前已声明合同的实现大体稳定；问题是
合同范围小于完整系统目标，且少数降级策略跨越了应有的信任边界。

### 8.2 已验证能力

- `validate_skill_yaml --check`：95 valid / 0 invalid。
- canonical YAML、catalog、SKILL.md、parameters、routing table、8 个 domain INDEX、
  description drift 全部通过同步检查。
- `audit_skill_requires --check`：0 missing dependencies；7 个 skill 有 extra 声明警告。
- 表示、获取、corpus、promotion、resolver、routing、lint、help contract 和 bot adapter
  的当前扩大定向测试（`conda run -n OmicsClaw`）：
  **425 passed / 2 skipped（427 collected）**。
- 另行复核动态 skill listing 与全入口 protocol：**14 passed**；修复了写死的 30-skill
  断言及 4 个 consensus shim 缺失的 `SKILL_NAME/SKILL_VERSION`。
- 95 个技能中 52 个有技能本地测试，43 个没有；PR CI 的真实科学 demo 目前只覆盖
  `spatial-preprocess`，所以不能把框架合同测试等同为 95 个方法学实现均已验证。

### 8.3 诊断假设结论

| 假设 | 结论 | 证据 |
| --- | --- | --- |
| H1：整体只达到强 M0 底座 + 部分 M1 | **确认；首轮后 M0 接近验收、M1 信任边界闭合** | registry/fail-closed 已补齐，但 security 跨层消费、获取泛化、M2 完整结构检索和 M3 仍有缺口 |
| H2：获取路径存在“未验证也可正式可见”的信任泄漏 | **确认，已修复并回归** | `skipped` promotion 现移动至 `skills/.quarantine/`，不 refresh registry；证据写入 `references/quarantine.md` |
| H3：P2 泛化实际只是 P2a 标量字面量提升 | **诊断时确认；现已关闭 call-composition 子集** | loader 现消费 `skill_calls.jsonl`/steps；可证明 lineage 的 workflow 生成 facade-free `run_skill` 脚本并过 2×2；任意 Python 仍安全回退 |
| H4：检索仍是 domain-first sparse，结构条件未进入决策 | **部分关闭，RET-03/04/05/06 已有纵切** | resolver 现过滤 draft/deprecated、使用 validation tie-break、消费 structured `skip_when`，并对选中技能携带三态 precondition/`execution_ready`；AnnData compatibility graph 与复合 candidate plan 已实现，candidate-wide penalty、其他 6 域关系和 method-scoped filtering 仍未实现 |
| H5：P4 提示不等于演化闭环 | **确认** | `AutonomousRunMemory` 只有 goal/run/workspace/status/thread；失败未聚合，且无 proposal store、approval、writeback、revalidation |

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
AnnData 候选兼容图/复合计划均已实现，但 candidate-wide penalty、更多文件格式探针、其他
6 域 compatibility 关系与 `sc_batch` 收敛尚未完成；下一主线应是 RET-06/候选级结构消费，
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

#### P1 — 演化层尚未开始形成闭环（EVO-01–06）

`SkillRunResult` 无 `error_kind`；autonomous memory 虽保留 raw status，却没有
skill version/hash/environment/evidence。没有按版本和环境聚合的健康账本，也没有
Gotcha/参数修订/降级/弃用候选的数据模型、人工审批、写回重验和回滚。P4 的“相似成功
三次后给一段命令提示”只能算 acquisition trigger，不能算 evolution。

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
4. **Evolution substrate**：统一 run event + typed errors + version/environment health ledger。
5. **Human-gated loop**：proposal/approval/writeback/revalidation/rollback；最后接弃用/替代
   到 resolver 和 surfaces。

上述 1–2 已完成、3 与 RET-04/04b/05 有可验收纵切；4–5、RET-04 剩余内容探针、RET-06
候选级消费与 compatibility 跨域扩展完成并通过矩阵后，才可宣称达到原始四阶段目标。

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
- 边界：caller-supplied profile 仅作规划，config 仍需 surface 显式注入；当前也不是 candidate
  DAG 或所有候选重排，非 h5ad 只探测存在性/kind/type 而不读内容。因此 RET-04/04b 是已验证
  安全纵切，不宣称结构检索闭环完成。

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

### 8.9 RET-05 AnnData compatibility graph / candidate plan 复验

- `interface.outputs` 已进入 lazy metadata 与 registry；`outputs.anndata.processing_state` 是
  `raw | standardized | preprocessed` 显式枚举。generic preprocessing 边只有声明
  `processing_state=preprocessed`、输出 `processed.h5ad`、producer/consumer modality 均已知且相交时才生成，
  不再从文件名猜处理状态。
- `skills/skill_dag.json` 当前为 95 nodes / 52 edges（32 exact、20 generic、6 reviewed、无环）；
  52 条仅分布于 singlecell/spatial，明确是 AnnData 纵切。全库 compatibility graph 可报告环，
  只有选定技能的 induced plan 才执行 topo/cycle fail-closed；生成边默认 unreviewed alternative，
  `skill_dag_reviews.yaml` 以完整 edge identity 做 governed overlay，stale review 直接失败。
- registry 暴露 upstream/downstream/topological/candidate-plan 查询；candidate plan 返回请求顺序、
  topo 顺序、phase、完整 edge provenance 与 unresolved pairs。无边双 intent 保留为 unresolved/parallel，
  不静默丢弃，也不伪造执行顺序。
- 复合计划以 SHA-256 digest 绑定 chat-scoped pending state；严格 standalone 确认前，execution hook
  在 executor 前同时阻断 `omicsclaw` 与 `autonomous_analysis_execute`。确认状态跨普通 CHAT turn
  保留，新 analysis/cancel 替换或清除；确认后只放行计划内 skill。比较、解释、how-to 与普通科学
  描述中的 `and` 不生成执行计划。物理 executor=0 回归已接入 PR CI。
- 第三轮 Ask Codex 独立复审最终 **PASS（无 Blocker/High）**；定向回归 202 passed，routing oracle
  全部指标 1.000（hallucinated alias rate 0.000），95 manifests 有效且 DAG/catalog/SKILL.md 生成物干净。
- 非阻断边界：review overlay 尚不能显式 reject 派生边；standard spatial 五步 pipeline 仍缺非空
  induced edge 锚点；method-scoped `condition_scope` 尚无实例；topo 顺序执行、失败级联、pending TTL、
  unresolved plan 执行策略与其余 6 域 compatibility 建模不计入本纵切。

---

OmicsClaw 是多组学研究与教育工具，非医疗器械，不提供临床诊断。工程验收不能替代
领域专家对方法学和科学结果的复核。
