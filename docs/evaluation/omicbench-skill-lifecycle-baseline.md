# OmicBench Skill Lifecycle 基线

> 状态：2026-08-02 实测。范围是 OmicBench 44 个任务中的 A02、A03 两个 case，
> 不是全套通过声明。

## 1. 目标与边界

本基线用真实 OmicBench case 验证这条链路：

```text
skill.yaml protocol
  -> content-bound dataset resolution
  -> exact Skill revision + environment
  -> Shared Runner
  -> Skill-owned adapter
  -> OmicBench deterministic grader
  -> schema-v2 evaluation result
  -> Experience View
```

OmicBench 当前包含 44 个 task、164 个 grader check，数据约 10 GiB。本轮只选择与
`sc-preprocessing` Interface 直接匹配的 A02/A03；每个 protocol 只绑定一个 case，原始
fixture、oracle、rubric 和 grader 均不修改。

## 2. Diagnose 反馈环

| 阶段 | 信号 | 根因与处理 |
| --- | --- | --- |
| A03 首次运行 | categorical `fillna("")` 抛错 | gene-name 候选列先转 nullable string，再填空值；增加 categorical 回归测试 |
| A03 第二次运行 | AnnData 写出时 index/column 名冲突 | 新建 gene index 时不继承 `feature_name` 名称；增加写出回归测试 |
| 原生 rubric | `3/3 PASS`，但输出少 8 genes | rubric 没覆盖“不子集”；adapter 增加完整 obs/var identity 自检，先转红 |
| strict adapter | shape 相同但 4,658 个 var identity 被替换 | 新增显式 `--preserve-var-names`；QC 名称解析后恢复原 feature 轴，默认标准化行为不变 |
| A02 交叉验证 | `4/4 PASS`，轴 identity 全保留 | 证明修复不是只针对 A03 grader 的特例 |
| 共享 demo 回归 | Scanpy 1.11.5 sparse normalization 返回未赋值变量 | 稀疏、非高表达排除路径使用等价的 SciPy row scaling；counts 与 target-sum 回归测试锁定语义 |

Native benchmark score 不是唯一通过条件。Skill-owned adapter 可以补上任务文字中明确、
但 grader 未覆盖的不变量；它不能放宽或改写 OmicBench rubric。

## 3. Lifecycle 契约改进

- `EvaluationDatasetRef` 绑定 repository-relative path 与 `sha256:` 内容身份；执行前后都
  重算 tree digest，拒绝 symlink 和运行中漂移。`__pycache__/*.pyc` 被明确视为生成缓存，
  不参与科学内容摘要。
- benchmark protocol 必须声明 `runner=command`、suite、恰好一个 case、dataset ref、
  `pass_rule=all_runs`、timeout 和 metric allowlist；demo protocol 规范化为
  `runner=shared_runner`。
- protocol digest 绑定完整声明、entry bytes 和 Skill runner 中的依赖版本；current
  contract 另绑定实际 runner interpreter identity。
- `ProtocolEvaluationResult` 使用共同 batch `evaluation_id` 与逐 run 唯一 `result_id`。
  schema v2 严格拒绝未知字段、重复 JSON key、NaN/Inf、自由 reason 和冲突 ID；v1 只做
  保守只读迁移，不能重建 repeat batch，也不能挣得 benchmarked。
- Experience View 按 protocol digest、current contract、dataset、environment 和完整
  run-index 集合聚合。缺一项、混合两个 batch 或任一 run 失败都不能挣等级。
- `evidence_supported_validation_level` 单独展示证据已经支持的等级；
  `effective_validation_level=min(declared, supported)`，仍由人工审批声明等级。
- command entry 使用 fresh scratch、控制凭据清理、Ctrl-C/timeout 的进程组 cleanup 和 exact
  Skill revision 回传。本机还通过 bubblewrap 把所选 dataset 只读挂载并使用 PID namespace；
  无 bubblewrap 平台回退到执行前后 digest guard。这仍不是完整 OS sandbox；迁移到
  RunRuntime/AuditOperation 继续延期。

## 4. 实测结果

两项结果来自同一次 `governance.evaluate("sc-preprocessing")`：

| 字段 | A02 normalize/log | A03 HVG |
| --- | --- | --- |
| OmicBench | 4/4，score 1.0 | 3/3，score 1.0 |
| adapter 自检 | 7710 x 20938，obs/var identity 保留 | 7710 x 20938，obs/var identity 保留 |
| protocol ID | `omicbench-a02-normalize-log-v1` | `omicbench-a03-hvg-v1` |
| dataset digest | `sha256:beddcd538b9584af7dac0be911ad265382b50ed8dcce89ca2456261297d4635e` | `sha256:acac3a34caf59663b62fe85d78dfdbc1e25e831e46b81f6f3c948f801b756b68` |
| protocol digest | `sha256:ffe009cd1f5da0feb01d69ffbdafeeb7aa35b2f094da53d041588ddb82260601` | `sha256:e577c7dbbfc4fa9a45fc11a83c4b4618088da1424c712154265ca18859b611d0` |
| result ID | `b448bbccae97485896a7b54617c9d3b5` | `a9087e0bc40747f3b06ef5e6d16ea696` |
| artifact bundle | `evaluation-artifact:sha256:9a12e8fe...705feb` | `evaluation-artifact:sha256:12b07d6f...38d655` |

共同身份：

- evaluation ID：`9512275bfe134a899a933420a63f8660`
- environment ID：`sha256:a9155407f4b5022dc029e258649aa150835ecfffac76a1c64e6ac15fe9af95e6`
- exact Skill revision：manifest
  `sha256:cad704629fcc61d21bdf0e928a475d45118b494efbd3cafa2ffad873b2b65299`，
  source `sha256:ab46fe754c91eef975029cc7b5ff562cc64bf3a056eceec54207184329f3c2a6`
- Experience View：`evidence_supported_validation_level=benchmarked`，
  `effective_validation_level=smoke-only`，`validation_state=current`

原始 byte-tree 清单在运行前后保持：

- A02：`d6d816b8c7a011ec9bc0b24206a4205f81887017a92a47de292ea4f4fbb80460`
- A03：`9936e725bed89b71cd03b481b506ba17b8559c2eb4a1be27df40bbe5ecce8674`

## 5. Suite 覆盖与下一步

当前可信正向覆盖是 **2/44**。下一项建议是 B08 `sc-pathway-scoring --method
aucell_py`，但必须由 adapter 验证 method provenance、至少三个有限且非恒定 score，并处理
现有 `enrich__*` 与 OmicBench `aucell_*` 命名契约的差异。

C01 暂不接入：它需要 Visium 预处理、至少两个 spatial-domain 方法、无真值选择后再加载
truth；native rubric 没检查“两种方法”这一任务要求，单次 `spatial-domains` 通过会高估能力。

明确能力缺口包括 D01/D02、E02、G01-G05、L01-L04；E05 也缺任务要求的真实
PLS-DA/OPLS-DA。它们应记录为 unsupported/tool gap，不能映射到名字相近的 Skill 来挣
`benchmarked`。

本批 r7 结果已经在清理 scratch 前保存有界 stdout/stderr、result envelope 与
hash-matched verifier evidence；两个 bundle 均可解析且没有 unresolved reference。三套 suite
的本地 ignored evidence snapshot 位于
`output/skill-lifecycle-benchmark-20260802-governed-r7/`，其 33-file manifest SHA-256 为
`4fad3edcd0409104ac17d7ddeedf691d46a8abdc468e0c546516c424993d6cfd`。RunRuntime-backed
AuditOperation、retention/GC、资源调度和完整 OS 隔离仍未完成。

OmicsClaw is a research and educational tool for multi-omics analysis. It is
not a medical device and does not provide clinical diagnoses. Consult a domain
expert before making decisions based on these results.
