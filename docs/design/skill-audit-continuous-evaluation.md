# Skill 审计持续评测与经验治理设计

> 状态：Proposed design（2026-07-22）
>
> 关联决策：
> [ADR 0074](../adr/0074-govern-skill-experience-and-continuous-evaluation.md)
> 当前仍为 Proposed；其中 Experience View、Evaluation Protocol/result/artifact store、
> governance evaluation 和部分 proposal 已分期实现，AuditOperation/RunRuntime 接入仍是目标设计。
>
> 现状验收真源：
> [Skill 审计系统：设计基线与验收规格](../reviews/2026-07-13-skill-audit-system-design-assessment.md)

## 1. 背景

OmicsClaw 已经建立了比一般自动 Skill 系统更严格的治理底座：

- `skill.yaml` 是唯一机器真源；
- Shared Runner 动态验证结果信封、声明产物和方法级保证；
- Skill 执行事件绑定版本、manifest、执行 source 和环境；
- 验证晋升、降级、Gotcha、弃用与替代都由 Backend 产生候选；
- 人工批准后才允许通过固定验证门和 CAS 写回；
- 中断审批有持久 journal 和显式 reconciliation；
- OmicsClaw-App 只是严格解析 Backend 快照、提交人工决定和刷新 catalog 的薄 Adapter。

MUSE-Autoskill 提出的关键补充是：Skill 不应只是一组静态文件，而应在创建、使用、
评测、管理和改进之间积累可复用经验。其最有价值的思想包括：

1. 评测是 Skill 生命周期的一部分，而不是一次性发布检查；
2. 每个 Skill 需要跨任务积累经验；
3. 测试和真实执行反馈应能触发改进；
4. 重复执行的稳定性本身是质量信号；
5. 重叠、长期失败或低价值 Skill 需要管理策略。

这些思想不能直接照搬。MUSE 的自由文本 `.memory.md`、自动 refinement、自动 merge 和
forget 对多组学科学软件过于宽松：它们不能证明经验仍适用于当前代码，难以约束隐私，
也会绕过 OmicsClaw 已建立的人工审批、精确 evidence binding 和唯一机器真源。

本设计引入一个深 `SkillAuditRuntime` Module，把 MUSE 的持续学习思想收敛为结构化、
版本绑定、可重建且人工门控的审计闭环。

## 2. 目标与非目标

### 2.1 目标

1. 用一个统一 Interface 接收运行、评测、路由和人工决策证据。
2. 为每个精确 Skill revision 生成可重建的 `Skill Experience View`。
3. 让 demo、fixture、benchmark 和稳定性评测由版本化协议约束。
4. 区分 manifest 声明等级和当前证据实际支持的有效等级。
5. 从真实证据生成 Gotcha、参数、路由、合并和弃用候选，但不自动写回。
6. 复用现有 `SkillEvolutionGovernance` 作为唯一 mutation authority。
7. 保持旧 OmicsClaw-App 与新 Backend、新 App 与旧 Backend 的双向兼容。
8. 让昂贵评测复用 RunRuntime、Dispatcher 和 Resource Scheduler，而不是建立第二队列。

### 2.2 非目标

- 自动修改科学算法或默认参数；
- 自动批准、合并、删除或隐藏 Skill；
- 将 Graph Memory 变成审计真源；
- 引入通用向量 Skill Memory 或 RAG；
- 实现公共 Skill Hub 或跨 Agent 发布；
- 移植 MUSE 的上下文压缩机制；
- 在本设计阶段实现 OmicsClaw-App 页面或 Backend 代码。

## 3. 词汇

### Skill revision

一个可评测的精确 Skill 状态：

`skill_id + version + manifest_hash + source_hash`

`environment_id`、`protocol_digest` 和 `run_id` 是证据维度，不能替代 Skill revision。

### Evaluation Protocol

一个版本化、可摘要的评测声明，说明评测种类、执行入口、数据引用、重复次数、允许的
指标和通过条件。协议实现可以位于 `tests/`，但只有 `skill.yaml` 声明的协议可以挣得
验证等级。

### Skill Experience View

从权威审计事件派生的、面向维护者和 App 的每 Skill revision 经验视图。它不是自由文本
记忆，不拥有治理事实，并且可以从 ledger 完整重建。

### Declared validation level

`skill.yaml` 中最后一次人工批准写入的验证等级。

### Effective validation level

当前 Skill revision 和当前协议证据实际支持的最高验证等级。它可能低于 declared level，
但不会在没有人工决定时改写 manifest。

### Validation state

当前证据的新鲜度状态：`current`、`stale`、`evaluation_required` 或 `review_required`。

### Remediation brief

由 Backend 生成的结构化修订输入，包含问题、证据、反例、允许变更范围、风险等级和必须
通过的回归协议。AutoAgent 可以消费 brief 准备候选，但无权批准或发布。

## 4. 架构与权威

```text
Shared Runner / RunRuntime / CI / Router / human review
                         |
                         v
                 Evidence Adapters
                         |
                         v
                 SkillAuditRuntime
                /        |         \
               v         v          v
      append-only     Evaluation   Skill Experience
      Audit Ledger     Snapshot        View
               \         |          /
                \        v         /
                 Evolution Candidates
                         |
                         v
             SkillEvolutionGovernance
                         |
                         v
                human approve/reject
                         |
                         v
               skill.yaml / SKILL.md
```

### 4.1 `SkillAuditRuntime`

该 Module 负责：

- 证据 schema 校验和身份归一化；
- 隐私最小化与 reason-code 分类；
- 幂等事件摄入；
- health、evaluation、stability 和 experience 聚合；
- declared/effective validation 计算；
- 证据新鲜度判断；
- 评测操作编排；
- 演化候选生成；
- Desktop 读模型构建。

它不负责：

- 直接修改 Skill 文件；
- 重新实现科学进程调度；
- 在 App 中复制治理策略；
- 用自然语言推断未声明的科学通过条件。

现有 `SkillHealthLedger` 先作为兼容 evidence Adapter 接入。历史 JSONL 不需要在第一阶段
重写；新 Module 必须明确哪些历史字段可证明什么，不能为缺失字段补造身份。

### 4.2 mutation authority

`SkillEvolutionGovernance` 继续独占：

- proposal persistence；
- representation、execution、retrieval 三门；
- human decision；
- guarded CAS、projection refresh、rollback 和 reconciliation；
- canonical `skill.yaml` / `SKILL.md` 写回。

`SkillAuditRuntime` 只能提交确定性 proposal input，不接受产品调用者提供 path、patch
function、validator callback 或 validation level。

把候选合成从 `SkillEvolutionGovernance.refresh()` 迁入 `SkillAuditRuntime` 是对已上线
ADR 0066/0068/0069 代码的谨慎重构：AuditRuntime 只产确定性候选**输入**，proposal 持久化、
确定性 proposal id、幂等 refresh 以及 ledger 独占锁下的证据复检仍留在 Governance，语义不得改变。

### 4.3 Graph Memory

Graph Memory 可以接收 Experience View 的可重建只读投影，但不能拥有：

- validation level；
- proposal state；
- approval decision；
- audit event；
- Skill lifecycle mutation。

Memory 投影失败不能改变审计事实或阻断 governance snapshot。

## 5. 统一证据模型

### 5.1 事件种类

| Kind | 生产者 | 主要用途 |
| --- | --- | --- |
| `execution` | Shared Runner / RunRuntime | 运行健康、缺陷归因、环境表现 |
| `evaluation` | demo / fixture / benchmark / stability evaluator | 挣得等级、评测新鲜度 |
| `routing` | resolver / Candidate Plan / explicit selection | 误选、歧义、冗余和适用范围 |
| `decision` | Governance approve/reject/reconcile | 治理历史和 causal decision |

### 5.2 公共事件信封

概念 schema：

```yaml
schema_version: 1
event_id: opaque-id
occurred_at: ISO-8601
kind: execution|evaluation|routing|decision
skill_revision:
  skill_id: canonical-id
  version: semantic-version
  manifest_hash: sha256:...
  source_hash: sha256:...
environment_id: opaque-environment-id
run_id: optional-authoritative-run-id
protocol_ref:
  protocol_id: optional-id
  protocol_digest: optional-sha256
outcome: succeeded|failed|canceled|interrupted|skipped
reason_code: closed-enum
metrics: bounded-allowlisted-map
evidence_refs: privacy-safe-identifiers
producer: runner|ci|router|governance|system
```

约束：

- `event_id` 幂等；相同 id 不同内容是冲突，不是重复成功；
- `skipped` 永远不能挣得验证等级；
- 原始矩阵、完整 prompt、绝对路径、凭据和任意 stderr 不得进入事件；
- traceback 只允许保存已认证 canonical entry 的 basename/line anchor；
- 指标只接受协议声明的字段、有限数值和有界字符串枚举；
- 未绑定 `source_hash` 或协议摘要的旧事件只能支持其明确可证明的低等级结论。

### 5.3 归因

失败至少区分：

- `script_defect`
- `contract_failure`
- `protocol_invalid`
- `dependency_missing`
- `bad_input`
- `resource_exhausted`
- `timeout`
- `environment_failure`
- `framework_failure`
- `canceled`
- `unknown`

只有精确 revision 的重复 `script_defect` / `contract_failure` 能支持 Skill 缺陷候选。
协议、环境、资源、框架和取消失败不得触发验证降级。

### 5.4 保留与可重建

统一逐次执行事件会随使用增长，而 AUD-02 要求 Experience View 可从 ledger 重建。二者以
持久化的聚合 checkpoint + 有界近期原始事件窗调和：压实只在保证 checkpoint 可导出的前提下
丢弃已聚合的原始事件，Experience View 由「checkpoint + 窗内事件」重建。必须定义显式保留
策略，ledger 不得无界增长，也不得因压实丢失重建所需的聚合状态。

## 6. Evaluation Protocol 与验证等级

### 6.1 `skill.yaml` 扩展

目标 schema：

```yaml
validation:
  level: fixture-validated
  evidence:
  - audit://evaluation/<opaque-id>
  protocols:
  - id: pbmc3k-fixture-v1
    kind: fixture
    entry: tests/test_pbmc3k_fixture.py
    runner: command
    dataset_ref:
      store: repository
      path: data/benchmarks/example-suite
      members:
      - inputs/pbmc3k.h5ad
      - graders/pbmc3k.py
      content_sha256: sha256:<64-lowercase-hex>
    repeats: 1
```

协议声明是 Skill 机器契约的一部分；测试代码、fixture 和基准数据仍是实现资源。协议摘要
必须覆盖 entry、声明资产、关键依赖、通过条件和数据引用，避免只摘要一份薄 YAML。

`dataset_ref` 绑定内容身份（复用 ADR 0064 dataset-observation identity：store id + 相对
路径 + `content_sha256`），数据内容变则协议摘要变，`fixture-validated`/`benchmarked` 才
可复现；小 fixture 提交入库，代表性 benchmark 数据置于持久存储而非临时路径。系统按 Skill
类型提供可覆盖的默认协议模板，避免每个 Skill 手写稳定性/环境阈值导致
`benchmarked`/`production` 实际不可达。

对 10–80 GiB 的 suite，`dataset_ref.members` 只绑定一个 case 实际消费的 input、prompt、
oracle 和 grader。成员必须唯一、不重叠、不可为 symlink；目录枚举失败和任一成员在整个
bundle hash 窗口内变化都 fail closed。未声明的同 suite 大文件不进入该 case digest，不能
据此声称整个 suite 的内容已验证。

### 6.2 等级语义

| Level | 当前证据要求 |
| --- | --- |
| `smoke-only` | 兼容底线；不解释为 demo 已通过 |
| `demo-validated` | 显式 demo 通过 Shared Runner 动态契约 |
| `fixture-validated` | 已提交 fixture、确定性断言和绑定协议通过 |
| `benchmarked` | 代表性数据、统计不变量、固定工具版本和可复现协议通过 |
| `production` | benchmarked，加稳定性、目标环境覆盖、无开放高风险审计项和人工批准 |

`production` 的具体环境矩阵和稳定性阈值由 Skill 类型/协议声明，不设置一套虚假的全域
默认值。

### 6.3 稳定性

稳定性是正交视图，不压缩成单一 validation level。每个协议可声明重复次数和容差，聚合：

- 重复执行成功率；
- result envelope 和 artifact inventory 一致性；
- 协议允许公开的科学指标离散度；
- runtime / memory / temporary disk 分位数；
- 目标环境间的一致性；
- non-constant 和 low-variance 次数。

MUSE 使用五次独立运行评估稳定性；OmicsClaw 采用协议级 repeats，避免把合理随机性误判为
失败，也避免把所有算法强制成同一统计模型。

### 6.3b Suite-level Benchmark Campaign

Per-Skill Evaluation Protocol 只回答 exact Skill revision 的科学 conformance。Agent 的
`no_skill / curated_skill / self_created_skill` 净效用由独立 Benchmark Campaign 评估，
后者用显式 `experiment_kind` 冻结完整 case x condition x repeat 矩阵、
model/runtime/Agent/environment/tool-policy/budget identity、每个 condition config digest，
以及 coverage manifest digest 和预注册 anchor case IDs。

Campaign 的严格 denominator 不丢弃 missing、unsupported、uncovered、timeout 或失败单元；
它们均记 0。各条件 coverage 单列。所有条件另在 manifest 预冻结的同一 anchor case set
上计算诊断分，不能从 Phase-2 到达的结果行反推，也禁止各用各的 covered subset。每个实际
attempt 绑定唯一 trial/isolation identity 和 content-addressed artifact；graded record 还绑定
grader evidence。covered Skill 的失败行同样必须绑定 exact revision，不能借 timeout 绕过
memory/refinement identity 检查。

`main_skill_effect`、`memory_ablation`、`refinement_ablation`、`transfer` 各自使用固定 condition
集合，不能混轴。Memory on/off 保持同一 exact revision，R0/R1 保持同一 Skill ID。异构 suite
native score 不直接 pooled。

Campaign 结果不进入某个 Skill 的 Experience View，也不能挣 validation level。当前
`omicsclaw.skill.benchmark_campaign` 只实现严格合同与离线分析，输出明确标记为
`matrix-integrity-only`；Agent 执行 harness 尚未完成。因此它尚不能证明 Phase-1→Phase-2
creation、memory two-pass state/leakage、R0→R1 parent/held-out 或 transfer source→target 的
typed causal provenance，opaque condition digest 也不能单独证明只改变了目标实验变量。

### 6.4 新鲜度与有效等级

以下变化使旧证据失去对当前 revision 的直接支持：

- manifest 或 execution source 改变；
- evaluation protocol 或 fixture 改变；
- 证据绑定的关键依赖/工具版本改变；
- 已批准 Gotcha、参数或适用范围修改改变了评测前提；
- replacement 或 lifecycle authority 改变。

变化发生时：

1. 不自动改写 manifest；
2. 不自动删除历史证据；
3. 计算 `effective_validation_level` 为当前证据可证明的最高等级；
4. 设置 `validation_state=stale|evaluation_required|review_required`；
5. App 和审计 snapshot 同时展示 declared/effective level；`catalog.json` 仍只出
   declared level（至多加一个 stale 指针），因为它是 approval 时经 guarded CAS 重生成的
   投影（ADR 0066/0068），而 effective 随证据变化、无 manifest 写入，不能进入该 CAS 投影；
6. router 只在现有 resolver 已认定兼容的候选集合中，把 current/effective evidence 作为
   **有界、确定性**软 tiebreaker；它不发明新的等价关系、不排除候选、且排序不得自增强
   （少路由的 Skill 不能仅因累积证据更少而掉档）。没有替代时保留显式候选和过期提示。

## 7. Skill Experience View

每个精确 Skill revision 的视图至少包含：

```yaml
skill_revision: {...}
declared_validation_level: benchmarked
effective_validation_level: fixture-validated
validation_state: evaluation_required
last_observed_at: ISO-8601
usage:
  execution_count: 0
  routing_count: 0
  explicit_count: 0
health:
  successes: 0
  skill_defects: 0
  environment_failures: 0
  framework_failures: 0
stability:
  protocol_id: optional
  repeats: 0
  success_rate: optional
  metric_dispersion: bounded-map
approved_gotchas: []
coverage_gaps: []
pending_proposal_ids: []
evidence_refs: []
```

Experience View 只能表达事件能证明的结论。LLM 生成的经验叙述必须走现有 Gotcha 风格的
结构化 draft、source anchor、counterexample 和人工批准流程，不能作为无来源 note 追加。
这条纪律针对任何**全局自由文本经验沉淀**（agent 追加、注入每个 prompt 的自由记忆），
`.memory.md` 只是其最窄形态；Experience View 是 typed、证据派生的投影，永不退化为自由 note 字段。

## 8. Skill 管理与演化

### 8.1 proposal kinds

| Kind | 证据 | 批准前必须重验 |
| --- | --- | --- |
| validation promotion/demotion | 评测证据 | 对应协议 |
| Gotcha | 重复缺陷 + source anchor + success counterexample | representation/execution/retrieval |
| applicability revision | routing error / rejection / collision | routing oracle |
| parameter revision | fixture/benchmark 对照 | baseline/treatment scientific invariants |
| protocol revision | coverage gap / invalid protocol | protocol self-test + history replay |
| merge/replacement | overlap + routing collision + compatible Interface | replacement acquisition + execution + routing |
| strategic deprecation | low utility + replacement evidence | exact replacement revalidation |

每个 proposal 必须包含支持事件、反例、风险等级、目标 revision、预期影响、验证协议和
stale 条件。

### 8.2 merge

合并是两阶段治理：

1. `merge_candidate` 只说明能力重叠、Interface 差异、路由冲突和建议替代形态；
2. 通过受控 Acquisition 创建或选择替代 Skill；
3. 替代 Skill 至少达到 `demo-validated` 并通过 routing regression；
4. 对每个旧 Skill 分别产生 replacement-backed deprecation proposal；
5. 人工批准后旧 Skill 保留审计历史，但退出自动路由和执行。

系统不得直接拼接两个 Skill 的代码或方法学文本。

### 8.3 pruning / forgetting

长期未使用只能产生 review signal，不能直接删除、隐藏或降级。低频科学方法可能仍有明确
价值。战略弃用必须具备不同 canonical replacement，并使用与 ADR 0068 一致的人工审批、
重验和 runtime consequence。

### 8.4 remediation brief 与 AutoAgent

SkillAuditRuntime 可生成：

```yaml
problem_kind: routing_collision|parameter_regression|script_defect|protocol_gap
target_revision: {...}
support_event_ids: []
counterexample_event_ids: []
allowed_change_scope: narrative|routing|parameters|tests|implementation
required_protocol_ids: []
risk: low|medium|high
```

AutoAgent 只能在受控 Workspace 中准备候选 patch 或 replacement。审批、canonical source
mutation、projection refresh 和 lifecycle consequence 仍由 Backend governance 完成。

## 9. OmicsClaw-App 兼容设计

### 9.1 当前约束

当前 App：

- 严格解析 `/skill-evolution` 的 `proposals` 和 `health`；
- 通过 Next.js server-only proxy 调用 Backend；
- 删除 Renderer `Authorization`；
- 本地使用 Electron 交付的 evolution token，远程使用显式 Backend authority；
- decision 后若不能重载权威 snapshot，冻结全部审核操作；
- App 不计算 eligibility 或修改 manifest。

这些行为保持不变。

### 9.2 兼容快照

`GET /skill-evolution` 保留现有字段，并做 additive 扩展：

```json
{
  "proposals": [],
  "health": [],
  "schema_version": 1,
  "authority_epoch": "opaque-backend-epoch",
  "snapshot_revision": 1,
  "generated_at": "2026-07-22T00:00:00Z",
  "capabilities": [],
  "summary": {}
}
```

- 旧 App 忽略新增字段并继续工作；
- 新 App 将缺失 `schema_version` 视为 legacy contract；
- `snapshot_revision` 只在同一 `authority_epoch` 内单调增加；Backend
  重启或权威替换产生新 epoch，新 App 必须把 `(authority_epoch,
  snapshot_revision)` 作为一个因果位置；
- 新 App 连接旧 Backend 时隐藏 capability-gated 控件，不模拟 Backend policy；
- 在 v1 内只允许 additive change；breaking change 使用新的 major route 或 media type。

### 9.3 分页读模型

为避免 whole-ledger snapshot 无限增长，新增：

- `GET /skill-evolution/skills?cursor=&limit=&domain=&state=`
- `GET /skill-evolution/skills/{skill_id}`
- `GET /skill-evolution/proposals/{proposal_id}`
- `GET /skill-evolution/operations/{operation_id}`

cursor 必须不透明、内容无关且有固定 page limit。Skill detail 返回 Experience View、评测
覆盖和 evidence identifiers，不返回原始审计 payload。

### 9.4 评测命令

- `POST /skill-evolution/evaluations` 返回 `202 AuditOperationReceipt`；
- App 通过 operation detail 观察进度；
- observer 断开不取消操作；
- `POST /skill-evolution/operations/{operation_id}/cancel` 是唯一显式取消命令，
  并映射到底层 RunRuntime cancel；
- 现有 `/skill-evolution/refresh` 只从已有证据刷新候选，不隐式启动昂贵评测。

### 9.5 decision consistency

现有 proposal hash/CAS 继续是兼容权威。新 App 可以额外发送
`expected_authority_epoch + expected_snapshot_revision`，但 Backend 不要求旧 App 提供这些
字段。任何不可确认 decision 结果都必须通过同一 epoch 中更高 revision，或新 epoch 的完整
权威 snapshot 解除 quarantine；缓存快照不能返回 `200` 并解除冻结。

### 9.6 App view model

现有“技能验证审核”页面可逐步扩展为：

- Overview：领域健康、待评测、证据新鲜度；
- Skills：Skill Experience View；
- Evaluations：操作、协议、进度和稳定性；
- Proposals：统一治理候选。

App 可以有 kind-specific presentation，但不能包含 promotion threshold、defect attribution、
merge eligibility、replacement validity 或 lifecycle policy。

## 10. AuditOperation 与执行复用

AuditOperation 状态：

`accepted -> running -> succeeded | failed | canceled | interrupted`

AuditOperation 是评测编排和观察记录，不是新的 executable queue。实际 demo、fixture、
benchmark 和 stability repetition 必须通过现有 RunRuntime、Run Dispatcher 和 Execution
Resource Scheduler。评测 Run 不携带所属 Project assignment（或仅 governance-only 上下文）：
照常过 Dispatcher 准入（ADR 0061）、领取新鲜 output claim（ADR 0070），但**冻结零个 ADR
0064 analysis-lineage Memory 投影**——评测不是用户科学，绝不能进入某个 Project 的科学
连续性。它不得从 restart snapshot 重建可执行 payload，也不得授予第二个 Assignment。

App 断线只释放 observer；显式 cancel 才能请求取消底层 Run。终态必须绑定完整子 Run 集、
协议摘要和评测结果摘要。

## 11. 错误处理

### 11.1 ledger 不可用

普通科学 Run 的既有成功不能因事后 ledger 写失败而改写为 failed；该 Run 不能形成晋级
证据，并记录 Backend framework incident。Governance refresh、proposal creation 和 approval
必须 fail closed。

### 11.2 ledger 损坏

不能跳过坏行继续计算健康或 eligibility。审计查询和治理命令返回非成功状态，普通显式
Skill 执行可以继续，但 UI 必须显示 audit unavailable。

### 11.3 protocol failure

协议缺失、摘要漂移、entry 无法加载、指标 schema 非法或 evaluator 崩溃归类为
`protocol_invalid` / `framework_failure`，不能生成 Skill demotion。

### 11.4 snapshot failure

只有完整构建并验证一致性的 snapshot 可以返回 `200`。构建失败必须返回非成功响应；不得
用旧缓存冒充权威快照。

### 11.5 privacy

Backend 在事件摄入时完成脱敏，而不是依赖 App。公开读模型只包含 closed reason code、计数、
时间、opaque identity、允许的 bounded metrics 和 evidence identifiers。

## 12. 验收矩阵

| ID | 验收项 | 通过证据 |
| --- | --- | --- |
| AUD-01 | 所有新证据绑定精确 Skill revision | schema、缺字段/错 hash/重复 id 冲突测试 |
| AUD-02 | Experience View 可从 ledger 重建 | rebuild 与 projection deletion/recovery 测试 |
| AUD-03 | demo/fixture/benchmark/stability 绑定协议 | protocol digest、asset drift、skipped 不晋级测试 |
| AUD-04 | declared/effective validation 和 freshness 分离 | source/protocol/dependency drift 对照测试 |
| AUD-05 | 失败只能生成候选 | product caller 无 path/patch/validator Interface 测试 |
| AUD-06 | merge 使用 replacement + deprecation 两阶段 | 无 replacement、低 validation、routing regression 反例 |
| AUD-07 | App/Backend 双向兼容 | old/new contract fixtures 和 capability gating 测试 |
| AUD-08 | 隐私、认证、分页和 audit failure fail closed | serialization、auth-before-body、cursor bound、corrupt ledger 测试 |
| AUD-09 | 评测复用 RunRuntime | no-second-queue、Assignment、cancel、disconnect 测试 |
| AUD-10 | 稳定性和净效用可复现 | repeated-run、metric allowlist、baseline/treatment 测试 |

## 13. 跨仓合同策略

Backend 是 HTTP contract 和 JSON Schema/OpenAPI 的权威。实现阶段应导出版本化合同资产；
OmicsClaw-App 保存由该资产生成或同步的固定 snapshot 及摘要，并用严格 runtime parser 测试。

两仓按独立里程碑实施：

1. Backend additive snapshot、read models 和 contract fixtures——**历史首切片已完成**：
   Skill Experience View + declared/effective 分离 + additive snapshot 字段先基于现有
   `SkillHealthLedger` 落地并验证 AUD-01/02/04 与 AUD-07；后续 Backend 里程碑已加入
   Evaluation Protocol/result store、真实 case adapters 和离线 Benchmark Campaign analyzer；
2. App capability negotiation、Experience/Evaluation 读视图；
3. Backend AuditOperation 和 protocol execution；
4. App evaluation observer/cancel UX；
5. 新 proposal kind 和 AutoAgent remediation handoff。

每个里程碑必须保持旧 App/新 Backend 与新 App/旧 Backend 的明确行为，不能假定双仓同时
发布。

## 14. 采用 MUSE 思想的理由与调整

> 说明：本节的实验结构与定量结论以官方 MUSE-Autoskill 论文及附录为准。非官方参考实现
> 只能提供实现观察，不能覆盖论文声明或作为 benchmark 结果来源。

| MUSE 思想 | 采用方式 | 为什么调整 |
| --- | --- | --- |
| Skill-level memory | 结构化 Experience View | 论文的 `.memory.md` 是自由文本且不随 transfer 包转移；OmicsClaw 需要 exact-revision、可重建、隐私可控的证据投影 |
| unit-test-driven evaluation | 协议化 demo/fixture/benchmark/stability | 论文把生成测试称为 validation signal 和 audit path，不是 correctness guarantee；真实数据不变量更强 |
| failed test triggers refinement | 生成 remediation brief 和候选 | 自动改科学方法会绕过人工判断、held-out 回归与 CAS 治理 |
| merge / prune | replacement + deprecation 两阶段 | 描述相似不证明科学等价，低使用也不证明低价值；替代式治理保留历史与证据 |
| repeated-run stability | 协议级 repeats 和 dispersion | 论文主 Agent benchmark 用 5 次运行并报告 std/MAD；确定性 command grader 可声明 1 次，随机算法按协议声明 |

该设计不是给现有 governance 增加更多脚本，而是建立一个深 Module：调用者只需要提交
typed evidence 或读取 typed snapshot，身份、归因、聚合、评测、新鲜度和候选策略集中在
一个 Interface 后面，从而提高 Locality 和 Leverage。

## 15. 当前状态

本文和 ADR 0074 的决策状态仍为 Proposed，但实现已进入分期落地，不能再写成“未开始”。

已实现并有 contract test 的切片：

- `SkillAuditRuntime`、可重建 Experience View、declared/effective/evidence-supported 分离；
- Evaluation Protocol schema、protocol digest、schema-v2 result store 与保守 v1 读取；
- 本地 content-addressed Evaluation Artifact Store，在 scratch 清理前保存有界日志、result
  envelope 与 hash-matched verifier evidence，并以 opaque ref 进入 result evidence；单对象
  64 MiB、最多枚举 4,096 files，过大或未匹配 evidence 在 bundle 中标记 unresolved；
- demo/fixture/benchmark/stability 编排、协议内 repeat batch 完整性校验与聚合、metric dispersion；
- content-bound multi-member case bundles、suite-level Benchmark Campaign 严格分母分析；
- Campaign 的 experiment-specific condition 集、pre-frozen coverage anchor、run artifact/trial/
  isolation identity，以及明确的 matrix-only causal-claim fence；
- merge advisory、protocol revision 和既有 validation/gotcha/deprecation proposal 治理；
- Desktop additive audit snapshot，以及 OmicBench A02/A03、scAgentBench PAGA 和
  BiomniBench-DA 12-2 deterministic preflight 三套 partial-coverage evidence。

仍未完成、不得根据本文宣称存在的切片：

- RunRuntime-backed `AuditOperation`、artifact retention/GC 与观察 Interface、取消和资源调度；
- 完整 OS 级 protocol sandbox（当前 Linux dataset-backed command 仅有 bubblewrap 只读挂载、
  PID namespace 与 digest-guard fallback）；
- parameter proposal 与 AutoAgent remediation 闭环；
- 完整的新 App Overview/Skills/Evaluations 观察与取消体验；
- 三套 suite 的完整覆盖及真正的 no-Skill/curated/self-created Agent Campaign。
- Campaign execution harness 的 creation/memory/refinement/transfer typed causal provenance。
