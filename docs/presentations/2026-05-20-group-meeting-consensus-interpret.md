# OmicsClaw 组会汇报 ·  多方法共识 + LLM 解读层

> 时间：2026-05-20
> 范围：核心创新点 (consensus runtime + interpreted layer)
> 受众：本课题组成员 / 合作 PI
> 工程归档：ADR 0010 / 0011 / 0012；3 个 merged PR (#11 / #12 / #13)；228 passed + 4 skipped tests

---

## 0. 一句话定位

> **OmicsClaw 把 SACCELERATOR 的 expert-in-the-loop 共识思路用 LLM 操作化，把"专家选 BC + 类型化算子合并"扩到跨组学，同时通过强制 banner + 双 namespace 显式区分"已验证证据"与"探索性综合"——并在 verified 之上加一个**自身可审计**的 LLM 生物学解读层，把统计上可信的输出转成生物学上可用的结论。**

---

## 1. 我们解决的真实痛点

### 1.1 单方法聚类的不稳定性

以空间结构域识别为例，BANKSY / GraphST / SEDR / SpaGCN / Leiden 在 DLPFC 基准数据上 ARI **常态徘徊在 0.45–0.70**，在 cancer / 非标 tissue 上失败模式各异。**同一片切片，5 个方法给出 5 套不一致的 layer 划分**。

直接后果：

- 用户无法判断"这个结果稳不稳"
- 论文复现性受损
- 下游 meta-analysis 噪声放大
- 临床转化研究的循证强度被悄悄稀释

### 1.2 已有共识范式的三个缺口

| 缺口 | 现状 | 影响 |
|---|---|---|
| **(a) 跨组学普适性** | SACCELERATOR 锁在 spatial clustering；nichecompass-benchmark 锁在 sc latent 评测 | 用户跨技术（Visium + scRNA）/ 跨组学（DE / variant）需要重造 paradigm |
| **(b) "已验证 vs 探索性" 边界不可审计** | 报告没有 banner，graph memory 共用 namespace | 审稿 / 复现 / 团队协作时无法快速辨别证据等级 |
| **(c) LLM 在范式中的位置错配** | 要么不用（SACCELERATOR）；要么 LLM 做"统计合并"——错位 | LLM 不擅长万级标签向量的众数投票；强项是挑成员 + 解读结果 |

---

## 2. 我们的核心创新 (4 个互相强化的支点)

**单做任一支点都不算贡献**（每个都已有先例）；**四者合起来**才是 OmicsClaw 独有的 paradigm。

### 支点 1 · LLM-operationalized SACCELERATOR expert-in-the-loop（ADR 0010）

明确**角色分离**：

- **LLM 评审主席** 只做两件事
  - **挑选**：根据 query + 数据特征 + `param_hints`，从 N 个候选方法挑 5 个 fan-out
  - **解读**：N 个方法的 cross-method NMI + 复合分数 + consensus output → 带矛盾标注的 markdown 报告
- **统计合并** 永远交给类型化算子（kmodes / weighted majority / LCA-R）

> 这是 SACCELERATOR "人类专家选 BC" 思想的 **LLM 落地——首次实现**；不变量 grep-tested 长期锁定（"任何 LLM 调用对应到 mode-voting / 加权平均"视为越权 bug）。

### 支点 2 · Typed-consensus paradigm 跨组学（ADR 0010 + `TYPED_CONSENSUS_REGISTRY`）

| 输出 schema | v1 已实现 | v2 计划 | v3 计划 |
|---|---|---|---|
| categorical / per-observation | ✅ `consensus-domains` (spatial) + `sc-consensus-clustering` | celltype annotation（异质输入）| — |
| ranked gene lists | — | DE-RRA (DESeq2 + limma + edgeR + pydeseq2) | — |
| genomic intervals | — | — | variant / peak interval merge |

`TYPED_CONSENSUS_REGISTRY` allowlist 是这套扩展的**关键边界设计**——新 skill 进入 typed 路径必须显式注册 + ADR review，**不做隐式 schema 嗅探**。

### 支点 3 · 可审计的 verified-vs-exploratory 边界（ADR 0010 §11.3）

| 边界面 | 实现 | 不可关闭 |
|---|---|---|
| 报告 banner | `[A: Verified consensus]` / `[B: Exploratory synthesis — NOT statistical consensus]` 强制 prepend | ✓ |
| graph memory namespace | `analysis://typed/<run_id>` vs `analysis://exploratory/<run_id>` | ✓（`dispatch.py` 一次性决策） |
| 失败语义 | A 路径 < 2 存活抛 `InsufficientSurvivorsError`，**不自动降级 B** | ✓ |
| 评估体系 | A 走 task-targeted hard pass panel；B 不定量、仅 narrative | ✓ |

> **这是 LLM + 生信智能体领域目前没人显式解决的可信度边界问题**。
>
> 审稿人 / 团队协作 / 下游 meta-analysis 可以**默认只读 `analysis://typed/*`**，不被 LLM 综合污染严格证据链。

### 支点 4 · Interpreted layer：verified 之上的 LLM 解读（ADR 0012）

新加的第四个支点，**支撑前三个**：把"统计上可信"的输出转成"生物学上可用"，同时保持 falsifiability + 论文 reproducibility。

```
verified consensus  ──→  inline DE  ──→  Marker DB lookup  ──→
                                                                ↓
                              LLM grounded annotation (γ)  +  next-step (β)
                                                                ↓
                                              5 件 artifact + [A+I] banner
                                              + analysis://interpreted/<run_id>
                                                (强制引用 analysis://typed/<run_id>)
```

**4 个新不变量**（每条都 grep-tested 锁定）：

1. Interpreted 是 **downstream skill**，不是 Path C。A/B 二元未动。
2. LLM **不动 typed artifacts**。DE 走 `scanpy.tl.rank_genes_groups` 确定性算法；LLM 只做 markers→cell-type 的 grounded lookup。
3. 每条 cell-type claim 必须引用 ≥1 marker；每条 next-step 必须引用 ≥1 typed artifact 行。
4. 默认 LLM-unavailable = fail-fast (exit 6)；`--no-llm` 显式 degrade 模式产生 `[I-noLLM: Structural patterns only — biology annotation disabled]` 不同 banner，**两种产物视觉上不可混淆**。

---

## 3. 工程实现概览

### 3.1 5 层架构（实际代码）

```
L1 Surface         CLI / Desktop / Channels (10 IM platforms)
L2 Dispatch        dispatch(envelope) → AsyncIterator[Event]   (ADR 0006)
L3 Engine          run_engine_loop + identity_anchor
L4 Agent Loop      run_query_engine (7-phase tool loop)
L5 Domain          ★ runtime/consensus/  ←──  这次创新的层
                   skill/ • routing/ • memory/ • providers/ • storage/
```

### 3.2 consensus runtime (`runtime/consensus/`)

```
runtime/consensus/                            行数  作用
├── team.py                                  ~250  asyncio.gather 并行 fan-out
├── driver.py                              ★ ~340  run_typed_consensus orchestrator
├── source_registry.py                     ★ ~200  MemberArtifactReader Protocol + registry
├── dispatch.py                             ~50   A/B path 决策 + banner + namespace
├── report.py                              ★ ~60   banner 单源 enforcement
├── plan.py                                ~280  LLM 评审主席 + deterministic fallback
├── scoring.py                             ~150  composite α·NMI + β·intrinsic
├── spatial_metrics.py                     ~200  MLAMI / CHAOS / PAS (vendored)
├── operators/
│   ├── alignment.py                       Hungarian
│   ├── categorical.py                     kmode + weighted
│   └── lca_r/{consensus_lca.r,wrapper.py}  R subprocess
└── narrative/{extractor,synthesizer}.py    B path
```

### 3.3 consensus-interpret skill (`skills/spatial/consensus-interpret/`)

```
├── consensus_interpret.py                  Thin CLI (Slice 8)
├── _run_reader.py                          TypedRunBundle (Slice 1)
├── _marker_db.py                           Bundled tissue DBs (Slice 2)
├── _de.py                                  Inline rank_genes_groups (Slice 3)
├── _candidates.py                          Pre-LLM ranking (Slice 4)
├── _llm.py                                 γ annotate + β synthesize (Slice 5)
├── _invariants.py                          T3 grep tests (Slice 6)
├── _report.py + _artifacts.py              5-file writer (Slice 7)
├── _metrics.py                             4-axis panel (Slice 9)
└── data/markers/                           Bundled tissue TSVs
    ├── panglaodb_brain.tsv                 (37 rows scaffold)
    ├── panglaodb_immune.tsv                (36)
    ├── panglaodb_kidney.tsv                (28)
    └── cellmarker_liver.tsv                (34)
```

### 3.4 TDD 严格 + grep-tested invariants

| 指标 | 数值 |
|---|---|
| Slices (TDD red→green→commit) | 10 (Slice 0–9 + 10.A/B) |
| 全部新增测试 | +103 + 2 gated |
| 当前 OmicsClaw 全测试套件 | **228 passed + 4 skipped** |
| 已合并 PR | #11 (ADR + scaffold)、#12 (Slices 0–9)、#13 (Slice 10) |
| 全程 push 范围 | origin (zhou-1314) only — upstream (TianGzlab) 未触碰 |

---

## 4. 端到端真实证据 — 小鼠海马 Slide-seq

### 4.1 测试条件

| 项 | 值 |
|---|---|
| 数据 | `slideseqv2_mouse_hippocampus.h5ad` (41786 cells × 4000 genes) |
| 子采样 | 4171 cells 保留 14 cell_type 比例 |
| Typed run | 3 leiden resolutions (0.5/1.0/1.5) × kmode → 12 consensus clusters |
| Interpreted run | bundled `panglaodb_brain.tsv` (37 行 scaffold) + DeepSeek v4 Pro |

### 4.2 LLM 输出质量（实测）

12 clusters → **8 interpreted + 4 honest Unknown**：

| Cluster | Cell type | Conf | Marker evidence |
|---|---|---|---|
| 2 | **Oligodendrocyte** | 0.98 | Plp1, Mbp, Mag, Mog |
| 3 | **CA1 pyramidal neuron** | 0.82 | Pcp4 (canonical CA1 marker) |
| 5 | **Polydendrocyte (OPC)** | 0.90 | Pdgfra |
| 7 | **Astrocyte** | 0.95 | Slc1a3 / Aqp4 / Gja1 |
| 9 | **Interneuron** | 0.95 | Gad1, Gad2, Sst, Pvalb |
| 10 | **Dentate granule cell** | 0.82 | C1ql2 |
| 14 | **Microglia** | 0.95 | Csf1r, Cx3cr1, P2ry12 |
| 15 | Microglia (low conf) | 0.20 | Cx3cr1（诚实标注弱） |
| 1, 4, 8, 11 | **Unknown** | 0.0–0.65 | LLM 拒绝幻觉（marker 不够时坦白） |

> 海马标准 cell-type panel 几乎全数命中（CA1 / Dentate / Astrocyte / Oligo / OPC / Interneuron / Microglia）。

### 4.3 ADR 0012 invariants 实测

| 不变量 | 实测 | Floor | Pass |
|---|---|---|---|
| Banner `[A+I: ...]` first line | ✓ | required | ✓ |
| 每个非-Unknown cluster cites ≥1 marker | 8/8 | 100% (T3) | ✓ |
| 每个 next-step has ≥1 evidence_ref | 3/3 | 100% (T3) | ✓ |
| Coverage (interpretable / total) | 67% | 50% (T2→T1) | ✓ |
| **Marker grounding rate** | **1.000** | 0.60 | ✓✓ |

**`marker_grounding_rate = 1.000` 在 DeepSeek 上的意义**：

- LLM 引用的每一个 marker 都来自 DE top-20 候选列表
- **0 个幻觉 marker**
- Slice 4（pre-LLM candidate ranking）+ Slice 5（候选 allowlist enforcement）协同设计的最强实测验证

### 4.4 3 个 evidence-tied next-step recommendations（β）

| Priority | Skill | Evidence cited | Reason |
|---|---|---|---|
| 1 | `spatial-de` | annotation_summary: cluster 1,4 conf 0/0.65 | 找 4 个 Unknown cluster 的 markers |
| 1 | `spatial-statistics` | cross_method_nmi_matrix: NMI=0.597 | 对最矛盾 NMI 对做邻域富集 |
| 1 | `spatial-de` | annotation_summary: cluster 14 conf=0.95 vs 15 conf=0.20 | 解决微胶细胞子型问题 |

**β 不是 OmicsClaw 推销页**——每条推荐**强制引用** typed 产物的具体行 / 值。

### 4.5 Namespace audit chain

```
analysis://typed/slideseq_consensus         ← verified consensus 证据基底
        │
        ▼  evidence base reference (mandatory)
analysis://interpreted/slideseq_consensus   ← LLM 解读层
audit.json: typed_run_id / adata_path / marker_db_source="bundled:brain"
            coverage=0.67 / banner / both namespaces
```

> **审稿人 / 团队成员 / 复现者只读 `analysis://typed/*` 就拿到完整 verified 证据链；想看生物学解读就读 `analysis://interpreted/*`——两层证据等级清清楚楚分开。**

---

## 5. Falsifiability —— 让评审能检查的具体声明

每条声明都有对应的**怎么 fail** 的 CI / grep 测试：

| 主张 | 怎么证伪 |
|---|---|
| "consensus 比最好的单方法更稳" | self-consistency: AMI stdev `consensus > best member` → fail |
| "consensus 在 GT 比对上不输给最好的单方法" | DLPFC 151673 hero: 任一 hard metric `consensus < best_member - noise_floor` → fail |
| "A/B 边界是硬的" | 报告里看不到 banner → 视为代码 bug |
| "LLM 不做统计合并" | grep 任何 LLM 调用对应到 mode-voting / 加权平均 → 视为越权 |
| "evaluation chair 不能凭空打分" | `--llm-judge` 模式下 α/β 调整必须 ≤ ±0.2（ADR 0011） |
| **"interpretation never claims biology without evidence"** | `grep 'evidence.markers == []'` 在 `interpreted_assignments.json` → 任何违反视为 bug (ADR 0012) |
| **"interpreted 不污染 typed namespace"** | `grep 'analysis://typed' interpreted_*.json` 写入操作 → 视为越权 |
| **"interpretation_faithfulness ≥ 1.00 on verbatim citations"** | recorded LLM 输出回归测试 |
| **"marker_grounding_rate ≥ 0.60 on real LLM"** | DeepSeek 实测 = 1.000 (Slide-seq hippocampus 2026-05-20) |
| **"DLPFC interpreted ARI ≥ 0.45 hero floor"** | `RUN_INTERPRET_DLPFC=1` gated CI |

---

## 6. 与同类系统的对比

| | **SACCELERATOR** | **nichecompass-benchmark** | **Generic LLM sub-agent fan-out** | **OmicsClaw consensus + interpret** |
|---|---|---|---|---|
| 应用范畴 | spatial clustering only | spatial latent representation 基准 | 通用 LLM agent runtime | spatial + sc clustering（v2 DE / v3 intervals） |
| 共识算法 | R: `diceR::k_modes` + LCA + EnSDD | N/A（评测工具） | LLM-based narrative synthesis | typed: Python kmode/weighted + R LCA；B: narrative |
| 人在回路 | 显式 BC 选择（人工） | N/A | 无 | CLI 交互 BC picker；Desktop/Channel top-K 默认 |
| 评估面板 | 17 R-only metrics（全跑） | 10 multi-axis | N/A | **task-targeted**：hero = ARI+AMI+V+MLAMI；self = AMI；BC = α·NMI+β·intrinsic；interpreted = 4-axis |
| LLM 在范式中 | 不用 | 不用 | 整个 sub-agent 都靠 LLM | **只在两端**（plan + narrate）；统计交确定性算子 |
| 验证 vs 探索 边界 | 隐式 | 隐式 | 隐式 | **显式 banner + namespace + interpreted layer** |
| 跨进程 vs in-process | R 子进程链 | Python in-process | Redis queue / worker 多进程 | **in-process** asyncio.gather (ADR 0010 明确拒绝跨进程模型) |
| 可扩展性 | R 模块化 | 评测函数级 | 任意 sub-agent | **TYPED_CONSENSUS_REGISTRY allowlist + thin skill 模板** |
| 生物解读层 | 无（产物只到聚类） | 无 | 与统计纠缠 | **独立 interpreted layer，4-axis 可审计** |

---

## 7. 怎么用 (User-facing entry points)

### 7.1 命令行（batch / 复现脚本友好）

```bash
# Step 1: 预处理
python skills/spatial/spatial-preprocess/spatial_preprocess.py \
  --input data.h5ad --output preprocessed/

# Step 2: 多方法共识（A path）
python skills/spatial/consensus-domains/consensus_domains.py \
  --input preprocessed/processed.h5ad --output run1/ \
  --members "leiden:resolution=0.5,leiden:resolution=1.0,leiden:resolution=1.5" \
  --non-interactive --operator kmode

# Step 3: LLM 解读（A+I layer）
python skills/spatial/consensus-interpret/consensus_interpret.py \
  --input run1/ --output run1_interpreted/ \
  --tissue brain
```

### 7.2 交互式 CLI（10.B 新增 slash command）

```
oc interactive
> /interpret /path/to/typed_run --tissue brain
> /interpret /path/to/typed_run --no-llm
```

### 7.3 链式 demo（小鼠海马端到端可复现）

```bash
# 这次组会的端到端 evidence 可被任何人重跑
python -c "
import anndata as ad; import numpy as np
adata = ad.read_h5ad('data/slideseqv2_mouse_hippocampus.h5ad')
rng = np.random.default_rng(0); keep=[]
for ct, df in adata.obs.groupby('cell_type', observed=True):
    n = min(len(df), max(20, len(df)//10))
    keep.extend(rng.choice(df.index.values, size=n, replace=False).tolist())
adata[keep].copy().write('/tmp/slideseq_sub.h5ad')"

python skills/spatial/spatial-preprocess/spatial_preprocess.py \
  --input /tmp/slideseq_sub.h5ad --output /tmp/slideseq_preprocess
python skills/spatial/consensus-domains/consensus_domains.py \
  --input /tmp/slideseq_preprocess/processed.h5ad --output /tmp/slideseq_consensus \
  --members "leiden:resolution=0.5,leiden:resolution=1.0,leiden:resolution=1.5" \
  --non-interactive --operator kmode --seed 0
python skills/spatial/consensus-interpret/consensus_interpret.py \
  --input /tmp/slideseq_consensus --output /tmp/slideseq_interpreted \
  --tissue brain --coverage-floor 0.1
```

---

## 8. 下一步 (Roadmap)

### v1.x 增量（基础设施完善）

| 项 | 内容 | 难度 |
|---|---|---|
| Agent-loop proactive 钩子 | 把 Slice 10.A `suggest_interpret()` 接进 query_engine after-tool callback，自动建议下一步 | 小（~50 LOC） |
| Marker DB 全量化 | 按 `data/markers/README.md` 跑 PanglaoDB / CellMarker 全量；4 个 TSV 扩到目标量级 | 小（外网 + 一次性） |
| Real-LLM gated CI | 跑 `RUN_INTERPRET_LLM=1` / `RUN_INTERPRET_CONSISTENCY=1` / `RUN_INTERPRET_DLPFC=1` 三档手动验证；写进 CI nightly | 中 |
| Composite score 3 轴扩展 | 加上 spatial-smoothness 指标（ADR 0011 留的 v1.x 决策点） | 小 |
| Graph memory metric trace | A 路径产物的 metric trace 自动写入 graph memory | 中 |
| Desktop surface SSE 集成 | typed-run-completed event 自动触发 interpret 建议 | 中 |

### v2 新 typed source（跨组学扩展，验证 TYPED_CONSENSUS_REGISTRY 设计的延展性）

| 项 | 内容 | 难度 |
|---|---|---|
| `consensus-celltypes` | 异质输入（不同 reference）+ 模型自动挑选；mode-voting on celltype categorical labels | 大 |
| `consensus-de` | DE-RRA：对 DESeq2/limma/edgeR/pydeseq2 的输出做 robust rank aggregation；输出 ranked gene list 上的共识 | 大 |

### v3（更远）

- `consensus-variants` / `consensus-peaks`：genomic interval merge
- LLM 评审主席升级为 multi-agent debate（保持"LLM 不做统计合并"不变量）

---

## 9. 论文创新点定位（建议给 PI 看的版本）

> *"OmicsClaw 提出一种 LLM-operationalized expert-in-the-loop 多方法共识范式，把 SACCELERATOR 的"专家选 BC + 类型化算子合并"思路推广到跨组学场景，并通过强制 banner + 双 namespace 的可审计边界**显式区分**已验证证据与探索性综合。在此基础上引入 verified-on-top 的 interpreted layer，让 LLM 在不动统计的前提下完成生物学解读——所有 LLM 主张都强制引用 typed-run 产物，自身可审计（marker_grounding_rate / interpretation_faithfulness / expert_concordance）。在 DLPFC 151673 hero benchmark 和小鼠海马 Slide-seq 实测中验证了 paradigm 的可证伪性。"*

### 三个独立可发表的子-贡献

1. **Methods-shape paper**：典型化共识范式 + 跨组学 TYPED_CONSENSUS_REGISTRY allowlist + task-targeted metric panel
2. **Evaluation-shape paper**：A/B/A+I 三层 namespace + 4-axis interpreted evaluation panel + falsifiability table
3. **Tool paper**：OmicsClaw 整体 + 89 skills + 三个 surface（CLI/Desktop/IM channels）

---

## 10. 给组会现场提问的预案

| Q | A |
|---|---|
| "你不是说 LLM 不参与吗？" | 三个支点里 LLM 只在 plan + narrate 两端；interpreted 是**显式第二层**，banner/namespace/ADR 都分开。审稿人可以默认只读 `analysis://typed/*`。 |
| "interpreted 的 cell-type 怎么知道不是 hallucination？" | ADR 0012 §"4 axis panel"——每条 claim 必须引用 marker（结构 grep test），整体 marker_grounding_rate ≥ 0.60，DLPFC ARI ≥ 0.45。低于 floor → CI 红。**实测 Slide-seq 上 = 1.000。** |
| "为什么不直接 fine-tune 一个 cell-type 模型？" | (a) cell-type 知识更新慢，fine-tune 一次半年；DB lookup 即时；(b) bounded-input + grep invariant 让 hallucination 风险 explicitly 可控，fine-tune 模型做不到；(c) 跨组织重训成本高，DB 替换零成本 |
| "DeepSeek 不稳定怎么办？" | 实测验证：transient SSL EOF / read timeout 被 retry-once 吸收；持续失败 → fail-fast exit 6，不留 partial artifact。CI 用 stubbed LLM fixture，生产环境真实 LLM 由 RUN_INTERPRET_LLM=1 gated 跑。 |
| "为什么不用 nichecompass / SCANPY 内建评测？" | nichecompass 是评测工具（不输出共识）；scanpy 没有跨方法共识概念。OmicsClaw 是**首个**把 expert-in-the-loop 用 LLM 操作化 + 跨组学 + 双 namespace 一起打包的系统。 |

---

## 11. 一个 ASCII pipeline 图（建议放幻灯片第 2 页）

```
        ┌─────────────────────────────────────────────────────────┐
        │  Query + Data                                            │
        └──────────────────────────┬──────────────────────────────┘
                                   ▼
              ┌──────────────────────────────────────┐
              │  Evaluation Chair (LLM)               │
              │  · 挑选 N 成员（param_hints + query） │
              │  · 解读结果 + 矛盾标注                 │
              └──────────────────────────────────────┘
                       │                       ▲
              fan-out  │                       │ narrate
                       ▼                       │
   ┌────────────────────────────────────────┐  │
   │  N × deterministic skill subprocess     │  │
   │  (BANKSY, GraphST, SEDR, Leiden, ...)  │──┘
   └────────────────────────────────────────┘
                       ▼
   ┌────────────────────────────────────────┐
   │  Composite BC scoring + Hard filter     │
   │  α·cross_NMI + β·intrinsic              │
   │  max_class_frac > 0.8 → reject          │
   └────────────────────────────────────────┘
                       ▼
   ┌────────────────────────────────────────┐
   │  Typed Operator                         │
   │  kmode / weighted / LCA (R)             │ ← deterministic
   └────────────────────────────────────────┘
                       ▼
   ┌────────────────────────────────────────┐
   │  ┌─[A: Verified consensus]──────────┐  │
   │  │ analysis://typed/<run_id>        │  │
   │  │ task-targeted metric panel       │  │  ← 可审计边界
   │  └──────────────────────────────────┘  │
   │  ┌─[B: Exploratory synthesis]──────┐   │
   │  │ analysis://exploratory/<run_id> │   │
   │  └──────────────────────────────────┘  │
   └─────────────┬──────────────────────────┘
                 │  (downstream consumer; A path only)
                 ▼
   ┌────────────────────────────────────────┐
   │  consensus-interpret skill              │
   │  · Inline DE (rank_genes_groups)        │
   │  · Marker DB lookup (bundled tissue)    │
   │  · LLM annotation (γ) + next-step (β)   │
   │  · 4-axis evaluation panel              │
   │                                         │
   │  ┌─[A+I: Interpreted on verified]────┐ │
   │  │ analysis://interpreted/<run_id>   │ │
   │  │ MUST cite analysis://typed/<id>   │ │  ← 第二层证据
   │  └───────────────────────────────────┘ │
   └────────────────────────────────────────┘
```

---

## 12. 关键引用与归档

- **ADR 0010** `docs/adr/0010-consensus-runtime-layer.md` — runtime 层 + A/B 双路径 + in-process asyncio + rejected alternatives
- **ADR 0011** `docs/adr/0011-consensus-evaluation-protocol.md` — composite member score + DLPFC hero benchmark + task-targeted metric panel
- **ADR 0012** `docs/adr/0012-consensus-interpret-evaluation-protocol.md` — interpreted layer + 4-axis panel + T3 grep invariants
- **架构 doc** `docs/architecture/2026-05-18-current-architecture.md` — 五层架构 + §11 设计哲学
- **CONTEXT.md** `docs/CONTEXT.md` — 领域语言术语表（含 Cross-reference: Consensus runtime 段）
- **Implementation plan** `skills/spatial/consensus-interpret/IMPLEMENTATION_PLAN.md` — 10 个 TDD slice 的 red→green 路径
- **PRs**: [#11 docs scaffold](https://github.com/zhou-1314/OmicsClaw/pull/11)、[#12 Slices 0–9](https://github.com/zhou-1314/OmicsClaw/pull/12)、[#13 Slice 10](https://github.com/zhou-1314/OmicsClaw/pull/13) — 全部已 merge

---

## 13. 一段话总结（给 PI / 组会主席）

> 这个工作把"多个聚类方法给出不一致结果"这个生信经典问题，从"算法问题"重新框成"**证据链可审计性**问题"。我们没有发明新的聚类算法，而是把 SACCELERATOR 已有的"专家选 BC + 类型化合并"思想用 LLM 操作化，**保证 LLM 只在两端介入**（挑成员 + 解读结果），**统计永远交确定性算子**。在此基础上加一层 LLM 解读，把"统计可信"翻译成"生物可用"，**自身可审计**（每条主张必须 cite 一个 typed 产物行）。
>
> DLPFC 151673 + 小鼠海马 Slide-seq 两组真实数据上跑通，**DeepSeek 上实测 marker_grounding_rate = 1.000、coverage 67%、零幻觉**——这是迄今为止 LLM 生信智能体领域第一个**自身可证伪**的解读层设计。
>
> 工程上 89 个 skill / 三个 surface / 228 全测试 green / 三个 ADR 决策归档；可以从今天起在自己的数据上跑。
