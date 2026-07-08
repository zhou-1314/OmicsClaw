# skill.yaml v3「飞轮就绪」设计草案 —— G1 组合/能力签名 + G2 类型化参数

> 状态：草案 v0.2（**已纳入 Codex/gpt-5.5 审核：裁决 SOUND-WITH-FIXES，5 条 must-fix 全部应用**；见文末 §7）
> 承接：[`skill-representation-flywheel-readiness.md`](../reviews/skill-representation-flywheel-readiness.md) §5「v3 设计 pass」的 Must-decide 1–2；只覆盖 **G1 + G2** 两个承重缺口。
> 参照实测：`omicsclaw/skill/schema.py`（`SkillManifest`，ADR 0037）、`pipelines/spatial-pipeline.yaml`、`omicsclaw/runtime/consensus/sources.py`、`omicsclaw/skill/execution/flag_introspection.py`（ADR 0041）、`skills/**/skill.yaml`（95 个真实样本）。
> 铁律：加法式、v2 文件仍合法可读、**先定形状再增量实现**。

---

## 0. 目标与边界

**目标**：把两个"飞轮引擎会撞上"的表示缺口补成 schema：
- **G2 类型化参数**：解锁 P2 参数提升、P5 `source_ref` 铁律、未来 MCP input_schema 派生。
- **G1 组合/能力签名**：让 skill→skill 的**组合边**与**可链接能力**进表示层，收编当前三处脑裂（manifest 无边 / pipeline 靠文件名 / consensus 成员在 runtime），支撑 P4「相似分析」匹配与 §3.4「and-then」检索。

**三条原则（不可违反）**
1. **纯加法**：所有新块 optional、默认空；**一个 v2 `skill.yaml` 不加任何字段就是一份合法 v3 文档**。
2. **不替换资产**：`Summary`/路由 C、治理三槽（Lifecycle/Validation/Provenance）、`Security`、`Deps` 通道模型、单一真源单向生成、`hints`——**一律不动**（review §5「明确不要动」）。
3. **消费者先行**：只加"已有真实消费者或本轮就接线"的字段（`deps.cli` 那种"留了没人用"的教训不再犯）。

**唯一的非纯加法点**：`schema.py:_is_v2`（`:320-325`）硬拒 `schema_version != 2`。v3 必须显式放宽为**双读 {2,3}**——见 §1。这是 Codex 在 readiness 复核里点名的前置（"否则不是纯加法"）。

**本轮明确不做**（留给后续 v3 增量或 v4，见 §5）：G3 全域类型化输出、G4 完整扩展纪律/接口 semver、G5 能力↔实现解耦、G6 多入口、G7 错误分类、MCP 导出上关键路径。

---

## 1. v3 派发机制（前置）

```python
SUPPORTED_SCHEMA_VERSIONS = frozenset({2, 3})   # was: SCHEMA_VERSION = 2

@field_validator("schema_version")
def _check_schema_version(cls, v: int) -> int:
    if v not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(f"schema_version must be one of {sorted(SUPPORTED_SCHEMA_VERSIONS)}, got {v!r}")
    return v
```

- **向后兼容**：新块（`params`/`capability`/`composition`）全 `Optional`/默认空 → v2 文件校验路径不变。
- **向前兼容的边界**：一份 v3 文件**能被当前(知 v3 的)schema.py 读**；但**旧 checkout 的 schema.py（只知 v2）读不了 v3 文件**——这与 ADR 0037 v1→v2 完全同构，用同一条"schema.py 先落 v3 支持、再有任何 v3 文件进 live tree"的双轨纪律处理。
- **`extra='forbid'` 保持**：核心仍拒未知键（响亮失败）。**`ext:` 保留命名空间本轮不加**（Codex Q5：G1/G2 已够大；且不加 ext 就更要砍掉无消费者的字段/枚举——见 §4 过度设计反推）。`ext:` 留到 G4 专项。
- **不 bump 未迁移文件**：迁移是**逐域、可选**的；`schema_version:2` 与 `:3` 长期共存，不搞一次性全量 bump。

---

## 2. G2 —— 类型化参数 `interface.parameters.params`

### 2.1 现状（实测）
`Parameters`（`schema.py:139-156`）= `allowed_extra_flags: list[str]`（ADR 0041 已改为从 argparse **派生**，仅 4 consensus 保留覆盖）+ `hints: dict`（**完全自由**）。实测 `spatial-domains.hints` 是**按 backend 分组**：`{leiden|louvain|spagcn|stagate: {priority, params:[名字], defaults:{名:值}, requires, tips}}`。类型/范围/choices **只存在于脚本 argparse**（`add_argument(type=int, default=0, choices=[...], nargs='+')`），`flag_introspection.extract_argparse_flags` 当前**只抽 flag 名、丢弃类型**。

### 2.2 新增字段（并排 `hints`，不替换）

```yaml
interface:
  parameters:
    allowed_extra_flags: [...]        # 不动（ADR 0041 派生 / consensus 覆盖）
    hints: {...}                      # 不动（叙事：per-backend priority/tips/defaults）
    params:                           # ★ v3 新增；optional，缺省=v2 兼容
      - name: min_genes               # 规范 snake_case 参数名
        flag: --min-genes             # kebab CLI flag（必须 ∈ 派生 allowed_flags）
        type: int                     # int|float|str|bool|enum|path
        default: 0                    # 类型化字面量；可 null
        min: 0                        # 可选（int/float）
        max: null                     # 可选
        choices: null                 # 可选（type=enum 时为列表）
        required: false
        repeatable: false             # 表达列表参（如 --resolutions nargs='+'）
        backends: [scanpy_standard]   # 归属哪个 hints backend；[] = 跨 backend 共享
        help: "min genes per spot QC threshold"
        source_ref: null              # ★ G2↔P5 挂点：{quote,doc,char_span} 或 {todo:true}
```

对应 pydantic（新模型，`extra='forbid'`）：
```python
ParamType = Literal["int", "float", "str", "bool", "enum", "path"]
class SourceRef(_Strict):
    quote: Optional[str] = None; doc: Optional[str] = None
    char_span: Optional[list[int]] = None; todo: bool = False
class TypedParam(_Strict):
    name: str = Field(min_length=1); flag: str = Field(min_length=1)
    type: ParamType; default: object = None
    min: Optional[float] = None; max: Optional[float] = None
    choices: Optional[list] = None; required: bool = False
    repeatable: bool = False; backends: list[str] = Field(default_factory=list)
    help: Optional[str] = None; source_ref: Optional[SourceRef] = None
# Parameters 增: params: list[TypedParam] = Field(default_factory=list)
```

### 2.3 seeding（迁移几乎机械）
扩 `flag_introspection`：新增 `extract_argparse_params(script_text) -> dict[flag, {type,default,choices,repeatable,required}]`。**⚠️ Codex SF：不要只复用现有的正则/平衡括号扫描**（`flag_introspection.py:40` 现在只抽 flag 字符串）——必须用 **AST** 正确处理 `action=store_true/store_false`（→`type: bool`）、`action=append`/`nargs`（→`repeatable`）、`choices=`（→`type: enum`+choices）、负数默认值、`required=`。迁移脚本用它 + `hints[*].defaults`/`params`/`advanced_params`/`tips` 合并出 `params` 列表；`min/max/help/source_ref` 由人/LLM 增补。**纯 AST 可得的部分零手工**。

### 2.4 lint（新规则，扩 ADR 0041 的 "hints⊆allowed" 守卫）
1. `params[].flag ⊆ effective_allowed_flags`（派生 allow-list 减 blocked）。
2. **parity（Codex MF2 修正）**：每个 `hints[b].params` **∪ `hints[b].advanced_params`** 里的名字都有一条 `params` 记录且 `b ∈ backends`（防 hints 与 params 漂移）。⚠️ 真实 hints backend 有 **`params` 和 `advanced_params` 两个列表**（`sc-clustering/skill.yaml` 即是；`parameters_md.py:81` + `skill_lint.py:339` 都消费两者），只覆盖 `params` 会留漂移口。
3. `type(default)` 与 `params[].type` 相符；`choices` 仅在 `type=enum`；`min/max` 仅在 `int/float`。
4. **铁律 lint（P5，硬门推迟——Codex MF1 修正）**：`source_ref` 字段**本轮就加**（结构就位），但**硬门 lint 推迟**：现 `Origin = Literal["human","scaffolded","promoted","migrated"]`（`schema.py:75`）**没有 `corpus`**，所以"`origin=='corpus'` 强制 `source_ref`"的硬门**当前不可实现**。触发条件 = §5 里 `Origin += 'corpus'`（P5/G4）落地。本轮所有 origin 值都只对"带默认值却无 source_ref"发**软告警**，不阻断。

### 2.5 生成 / 消费
- `parameters.md`：有 `params` 时生成**类型化表**（名/flag/type/default/range/choices/help），无则回落现 `hints` 叙事。**现消费者**（`parameters_md.py:67`、`plan.py:104`、`argv_builder.py:114` 只用 flag 名）**全部不变**。
- **新消费者**（本轮不接线、但数据就位）：MCP `input_schema` 可从 `params` 派生真 JSON schema；P2 参数提升有了类型化落点；P5 `source_ref` 有了结构化位置。

---

## 3. G1 —— 组合 + 能力签名

拆两块：**能力签名**（让"什么能接在 A 后"可机械推导）+ **组合块**（给 pipeline/consensus 一个 canonical 表示）。

### 3.1 能力签名 `capability`（现有 interface 的规范化投影）

**关键设计**：不新造平行类型系统，而是把已有 `interface.inputs`/`interface.outputs` **投影**成一个跨域的 artifact-kind 词表 —— 补上"AnnData 偏置"（G3 的可链接子集），只类型化**可链接**产物。

```yaml
capability:                          # ★ v3 新增 top-level；optional
  requires:                          # 消费的产物（← interface.inputs 投影）
    - kind: anndata                  # 最小核心词表(见下) + 输入文件扩展派生
      state: {obsm: [spatial]}       # 数据状态键（← preconditions.data_shape）
      optional: false
  provides:                          # 产出的产物（← interface.outputs 投影）
    - kind: anndata
      state: {obs: [leiden], obsm: [X_pca, X_umap], var: [highly_variable], layers: [counts]}
      path: processed.h5ad           # 可选具体产物名（也用作 pipeline output selector, 见 §3.2）
```

**artifact-kind 词表——最小核心（Codex Q3 修正，别一次锁死跨域）**：本轮只固定 **`anndata | table | figure | report`** + **输入文件扩展名派生**（vcf/bam/mzml/fasta/bed… 由 `interface.inputs.file_types` 直接映射，不预先写死一张"跨域类型系统已成熟"的大词表）。非 AnnData 域的类型化**随真实链接需求增量补**。`role: primary|aux` 本轮先不落（见 §4 过度设计反推），用 `provides[].path` 作可链接主产物的锚点即可。

- **可派生**：缺 `capability` 时，一个 derivation shim 从 interface 计算它（`requires` ← `file_types`+`data_shape`+`requires_preprocessed`；`provides` ← `outputs.anndata.*` + `outputs.files` 按扩展名归 kind）。→ 检索/链式**对未迁移 skill 也能用**，`capability` 存在时作**权威增补**（如标 `role: primary`、补非 AnnData 域的 kind）。
- **可链接判定**：`B.requires ⊑ A.provides`（kind 相符 ∧ state 键子集）——这正是 P4「相似分析」与 §3.4「and-then」需要的边。
- **加法**：optional；`DataShape` 已 `extra='allow'`，不与之冲突。

### 3.2 组合块 `composition`（收编 split-brain）

给**保留的 `type: workflow`** 与**已存在的 `type: consensus`** 一个 manifest 内的声明式组合表示。**两模式，不建全 DAG**（review 明确）：

```yaml
# 模式 A —— pipeline（激活 workflow 保留类型，替代 pipelines/spatial-pipeline.yaml 的文件名接力）
type: workflow
composition:
  pattern: pipeline
  steps:
    - {id: preprocess, skill: spatial-preprocess, bind: {input: {source: $input}}}
    # 显式 output selector（Codex MF3）：绑到某 step 的具名产物，由该 step skill 的
    # capability.provides[].path 解析；缺省取 role/primary 主产物。非全局 chain_output_basename。
    - {id: domains,    skill: spatial-domains, bind: {input: {step: preprocess, output: processed.h5ad}}}
    - {id: de,         skill: spatial-de,     bind: {input: {step: preprocess, output: processed.h5ad}}}
```
```yaml
# 模式 B —— consensus（把 CONSENSUS_SOURCES 的"声明半"收回 manifest）
type: consensus
composition:
  pattern: consensus
  member_skill: spatial-domains
  template: categorical            # categorical | continuous
```

对应 pydantic：
```python
class InputSource(_Strict):                 # 显式 output selector (Codex MF3)
    source: Optional[str] = None            # "$input" (pipeline 外部输入)
    step: Optional[str] = None              # 绑到某 step
    output: Optional[str] = None            # 该 step 的具名产物; 缺省=primary
class StepBind(_Strict): input: InputSource
class Step(_Strict):
    id: str = Field(min_length=1); skill: str = Field(min_length=1)
    bind: StepBind
class Composition(_Strict):
    pattern: Literal["pipeline", "consensus"]
    steps: list[Step] = Field(default_factory=list)              # pattern=pipeline
    member_skill: Optional[str] = None                          # pattern=consensus
    template: Optional[Literal["categorical", "continuous"]] = None
# SkillManifest 增: composition: Optional[Composition] = None
```

- **减少漂移（Codex MF4 修正：这是"lint-gated mirror"，不是"脑裂治愈"）**：
  - **pipeline**：`spatial-pipeline` 成为一份 `type: workflow` 的 skill.yaml（或 `pipelines/*.yaml` 改遵从 `Composition` 子 schema）。`bind` 按 **§3.2 显式 output selector** 解析（经 capability 定位产物），取代 `chain_output_basename` 的全局文件名接力（`pipeline_runner.py:103,131`）。`pipeline_runner.py` 加 compat shim：读 `composition.steps` 优先、回落 legacy YAML —— **现行为不破**。
  - **⚠️ workflow-as-skill 的注册路径待定（Codex SF）**：`registry.py:235` 要求能解析入口脚本，否则**跳过该 skill**——一份纯 `composition`、无 entry 脚本的 `type: workflow` 会被 registry 跳过。本轮须给 workflow 定 registry/dispatch 方案（如"composition-only skill 免 entry 脚本、由 pipeline_runner 派发"），否则 pipeline 迁进来会不可路由。
  - **consensus**：4 个 consensus skill.yaml 增 `composition.pattern=consensus + member_skill + template`。**但 runtime 仍直接读 `CONSENSUS_SOURCES`**（`run.py:234`）**且 dispatch 由 `TYPED_CONSENSUS_REGISTRY`/`source.template` 决定**（`dispatch.py:47`）——所以本轮只是把 `member_skill/template` **镜像**进 manifest 并用 lint 断言与 `CONSENSUS_SOURCES` 一致（**lint-gated mirror，减少漂移**）。**真·脑裂治愈**要 runtime **改读 manifest 声明**（或反向：从 manifest 生成 `CONSENSUS_SOURCES`）——列为**后续**（本轮不宣称已治愈）。
- **不做**：分支/条件/并行 DAG、跨 pipeline 依赖解析 —— 线性 steps + 单 member fan 足以解锁获取（review §5.2）。

### 3.3 lint（组合/能力）
1. `composition.steps[].skill` / `member_skill` 都解析到真实 registry skill。
2. **（Codex MF5 修正：必须版本门控）** `type ∈ {workflow,consensus} ⟺ composition != None` **仅对 `schema_version == 3` 强制**。现有 **4 个 v2 consensus manifest 没有 composition**（`consensus-domains/skill.yaml` 等），全局强制会**打破"v2 零行为变化"**。二选一：(a) 规则版本门控（v2 consensus 不受影响）；(b) 同 PR 把 4 consensus + 1 pipeline 迁到 v3 并加 composition。
3. `bind.input.step` 必须指向**更早**的 step（无环、线性）；`bind.input` 恰含 `source:$input` 或 `{step,output}` 之一。
4. consensus（**lint-gated mirror**）：manifest `member_skill/template` == `CONSENSUS_SOURCES[name]`——断言镜像一致，**非** canonical 源（见 §3.2 MF4）。
5. capability 存在时：`provides/requires` 的 kind/state 不与 `interface` 的投影矛盾（增补可、冲突不可）。

---

## 4. 实现清单（供 Codex 核可行性）

| 面 | 改动 | 消费者本轮接线？ |
|---|---|---|
| `schema.py` | `_is_v2`→`_check_schema_version{2,3}`；加 `TypedParam/SourceRef/Capability/Composition/Step`；`Parameters.params`、top-level `capability`/`composition` | — |
| `flag_introspection.py` | 加 `extract_argparse_params`（type/default/choices/nargs），供 seeding | 迁移期 |
| lint (`skill_lint`) | §2.4 + §3.3 规则；派发按 `runtime.language`（R/bash 不跑 argparse-parity） | ✅ |
| 生成 | `parameters.md` 类型化表（有 params 时）；catalog 暴露 capability 边供检索；SKILL.md I&O 显示 typed params | ✅ |
| 迁移 | 扩 `migrate_to_skill_yaml`：seed `params`（机械）+ 派生 `capability`（机械）+ 手写 1 pipeline / 4 consensus 的 `composition`（面极小）；逐域 Codex-review + `--check` 门（同 ADR 0037 runbook） | ✅ |
| runtime | `pipeline_runner` compat shim（读 composition.steps 优先，回落 legacy YAML）；workflow-as-skill 的 registry 免-entry 派发方案（`registry.py:235`） | ✅ |
| **测试** | 同步改 `test_skill_schema.py:93`（现断言 "must be 2"→接受 {2,3}）；补 v3 carrier / params / capability / composition 的 schema+lint 测试 | ✅ |

**过度设计反推（Codex Q6——本轮先砍到有消费者再加）**：
- **砍**：`min/max`、`role: primary|aux`、宽 artifact-kind 词表——暂无消费者，最像 `deps.cli` 式装饰。capability 先只留 `kind/state/path/optional`（用 `path` 而非 `role` 锚主产物）。
- **留**：`repeatable`（仅当 argparse `append`/`nargs` 或 MCP 有明确消费者时填）；`template`（现有 consensus runtime 已消费，`dispatch.py:47`）。
- **不加**：`ext:` 保留命名空间（§1）。

**风险**：(a) capability 派生 shim 与显式 `capability` 双源——用"显式增补、冲突即 lint fail"消歧；(b) pipeline 迁移不能破 `pipeline_runner`——compat shim + 保留 legacy YAML 直到全迁；(c) `params` 与 `hints`(**含 advanced_params**) 双写漂移——parity lint 兜底；(d) v3 新块会被现生成器/lint 卡（它们先 `validate_skill_yaml`）——**故 schema.py 的 v3 支持 + 生成器/lint 兼容必须与首个 v3 文件同 PR**（Codex §6-1）。

---

## 5. 明确推迟（边界）+ 交付顺序
**推迟**：`Origin += 'corpus'`（**它是 §2.4 `source_ref` 硬门的触发条件**）、G3 全域类型化输出（只类型化可链接产物）、G4 完整扩展纪律 + 接口 semver + `ext:` 命名空间、G5 能力↔实现解耦、G6 多入口、G7 错误分类、MCP 导出上关键路径、dense 检索。理由：readiness §5 优先级 4–7 + Codex over-engineering 反推。

**交付顺序（Codex Q7——拆 PR，取代"能否同一个 PR"）**：
1. **PR-A：v3 carrier + G2 typed params**。`schema.py` 版本双读 {2,3} + `params`/`SourceRef` 模型 + `extract_argparse_params`(AST) + lint(parity 覆盖 params∪advanced_params) + `parameters_md` 生成兼容 + schema 测试。风险小、P2/P5 更急。
2. **PR-B：G1 capability + composition**，**与 pipeline/consensus 的 runtime 迁移同批**（`pipeline_runner` selector、workflow registry 派发、consensus lint-gated mirror）。不与 PR-A 混，避免一个 PR 同时改 schema/lint/pipeline 执行/consensus dispatch。

---

## 6. 交给 Codex 的审核问题
1. **加法性/兼容**：`_check_schema_version{2,3}` + 全 optional 新块，是否**真**保证 v2 文件零行为变化？有无我漏掉的 `extra='forbid'`/生成/消费路径会因新块存在而 v2 侧炸？
2. **G2**：`params` **并排** `hints`（不替换）是否正确？parity lint 是否足以防双写漂移，还是应让 `hints` 从 `params` **生成**（更激进、破坏"不动 hints"原则）？`source_ref` 铁律 lint 只对 `origin=corpus` 硬门、其余软告警——门槛合理吗？
3. **G1 能力签名**：把 `capability` 设成"interface 的可派生投影 + 可选权威增补"是否是对的加法姿态？artifact-kind 词表（anndata|vcf|table|mzml|…）够不够、是否过早给非 AnnData 域造类型？"只类型化可链接产物"的收敛对不对？
4. **G1 组合**：`composition` 只收**声明半**、runtime `sources.py` 留 behavior —— 这条"manifest 权威 + lint 门控 = 治愈脑裂"是否成立，还是必须连 reader/planner 一起迁才算收编？pipeline `bind` 按 step-id（非文件名）+ compat shim 是否足够不破 `pipeline_runner`？
5. **扩展命名空间 `ext:`**：本轮该不该顺手落 G4 的最小前哨（`ext` 内 `extra='allow'`），还是严格只做 G1/G2、`ext` 留到 G4 专项？
6. **过度设计反推**：`repeatable`/`min/max`/`role:primary|aux`/`template` 这些字段里，哪些是"没有当下消费者的 deps.cli 式装饰"、该砍到有消费者再加？
7. **顺序**：G2 与 G1 能否同一个 v3 schema PR 落地（都是纯加法），还是应 G2 先行（P2/P5 更急）、G1 随 pipeline/consensus 迁移单独走？

---

## 7. Codex 复核修订记录（v0.2）

Codex/gpt-5.5 对本草案做了 read-only、逐条对照真实代码的审核（运行了 manifest 校验：`validated 95 bad 0`，未改任何文件）。**裁决：SOUND-WITH-FIXES**——加法方向成立、G2 并排 typed params / G1 capability+composition 与现代码兼容，但若干"纯加法/治愈脑裂/source_ref 铁律"表述过强，须修正。

**Must-fix（5，均已应用 ✅）**
1. ✅ **`origin=corpus` 硬门当前不可实现**：`Origin` 枚举无 `corpus`（`schema.py:75`）→ §2.4 rule 4 降级为"字段本轮就加、硬门推迟到 `Origin += corpus`(P5)"；§5 标明 corpus 是触发条件。
2. ✅ **parity 漏了 `advanced_params`**：真实 hints backend 有 `params` + `advanced_params` 两列表（`sc-clustering/skill.yaml`；消费者 `parameters_md.py:81`、`skill_lint.py:339`）→ §2.4 rule 2 改为覆盖两者之并。
3. ✅ **`bind: preprocess.output` 不可执行**：runner 只按全局 `chain_output_basename` 找文件（`pipeline_runner.py:103,131`）→ §3.2 改为显式 output selector `{step,output}`，由 `capability.provides[].path` 解析。
4. ✅ **"脑裂治愈"过强**：runtime 仍直读 `CONSENSUS_SOURCES`（`run.py:234`）+ dispatch 由 `TYPED_CONSENSUS_REGISTRY`（`dispatch.py:47`）→ 改称 **lint-gated mirror**；真治愈=runtime 改读 manifest，列为后续。
5. ✅ **`type⟺composition` lint 破 v2 兼容**：4 个 v2 consensus 无 composition（`consensus-domains/skill.yaml`）→ §3.3 rule 2 改为**仅 `schema_version==3` 强制**（或同 PR 迁 4+1）。

**Should-fix（已并入 ✅）**：`extract_argparse_params` 用 AST 非 regex（store_true/append/nargs/choices/负默认，§2.3）；workflow-as-skill 需 registry 免-entry 派发方案（`registry.py:235`，§3.2/§4）；`composition.steps` 缺 params/method bind（记为后续）；capability 词表收敛到最小核心（§3.1）；同步改 `test_skill_schema.py:93`（§4）。

**过度设计反推（已采纳，§4）**：砍 `min/max`、`role:primary|aux`、宽 artifact-kind 词表；`repeatable` 待消费者；`template` 保留（consensus runtime 已消费）；`ext:` 不加。

**交付顺序**：拆 PR-A（v3 carrier + G2）/ PR-B（G1 + pipeline/consensus runtime 迁移），见 §5。

---

*OmicsClaw 是多组学研究与教育工具，非医疗器械，不提供临床诊断。基于本草案的工程决策请经领域专家复核。*
