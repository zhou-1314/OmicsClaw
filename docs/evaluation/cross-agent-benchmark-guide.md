# 跨 Agent Benchmark 运行指南（OmicsClaw vs Claude Code vs Codex）

> 状态：可行性判定 + 运行指南，2026-08-03。
>
> 判定：**可行**，但**不能直接复用现有 benchmark 代码**。现有 `benchmark_campaign.py`
> 在 schema 层结构性禁止跨 agent 比较，且全项目没有 agent 执行 harness。
> 需要先做 3 项前置改动（约 2–3 天），之后 P1 阶段可产出第一批可发表的对比数据。
>
> 与既有文档的关系：
> [三套 suite 汇总](muse-three-suite-skill-lifecycle-benchmark.md) 是 **Per-Skill 证据面**，
> 本文是 **Agent 证据面**。两者的分数不可互换、不可合并。

---

## 1. 先回答：Skill lifecycle 完善吗

**架构完整，闭环未合拢。** 这不是本次 benchmark 的阻塞项，但决定了你能声称什么。

依据 [Skill 系统蓝图](../design/skill-system-blueprint.md) §2 的实测状态（2026-08-02）：

| MUSE 阶段 | 设计强度 | 实测 | 对本次 benchmark 的影响 |
| --- | --- | --- | --- |
| Creation | ≥ MUSE | ❌ 正式库 **0/95** 来自创建路径 | `self_created_skill` 条件目前拿不出真实数据 |
| Memory（Experience View） | 强于 MUSE | 🟡 3 字段无生产者，usage 计数硬编码 | 不影响 agent 面对比 |
| Management | 强于 MUSE | 🟡 仅 stage-one advisory | 不影响 |
| Evaluation | 强于 MUSE | 🟡 **4/95** Skills 声明协议 | 不影响（agent 面走 suite 原生 grader） |
| Refinement | 刻意弱于 MUSE（人工门） | 🟡 remediation → AutoAgent 未接线 | `refined_skill` 条件目前拿不出数据 |
| Context | 弱于 MUSE | ⚠️ 已知债 | **会影响**：长任务上下文压缩质量直接进入分数 |

四个结构缺口 S1–S4 未修，P1 验收标准（≥1 个技能走完 创建→发布→评测→挣得等级）未达成。

**结论**：Skill 系统的**证据与治理机制**是可用的（r7 已在 4 个真实外部 case 上端到端跑通），
但**技能自创与自改进的闭环还没转起来**。因此本次 benchmark 只能测
"OmicsClaw 作为一个装备了 95 个人工技能的 agent" 对比 "通用 coding agent"，
**不能**测 MUSE 式的 `self_created_skill` lift。这是必须写进论文限制章节的事实。

---

## 2. 为什么可行：三条已验证的地基

### 2.1 三套 suite 的评分对 agent 是中立的

| Suite | 任务格式 | 评分入口 | agent 中立性 |
| --- | --- | --- | --- |
| OmicBench（44） | Harbor：`instruction.md` 原文投喂 coding agent | `bash tests/test.sh <final.h5ad>` | ✅ 完全中立——只看最终 AnnData 的 obs/var/obsm/uns |
| BiomniBench-DA（公开 50） | Harbor：`instruction.md` + 要求写 `trace.md` | `tests/test.sh` + LLM judge + rubric | ✅ 中立，但需 judge 凭据 |
| scAgentBench main（50） | prompt + 人工 ground-truth 代码/输出 | `bioagent-benchmark/evaluation/` 多维指标 | 🟡 中立但需自建对接 |

`instruction.md` 里 OmicBench 明确写着：*"The prompt below is delivered verbatim to the
coding agent... deliberately names no library or API"*。这正是跨 agent 比较需要的形状。

**本机实证**（2026-08-03）：OmicBench verifier 可脱离 Docker 独立运行。

```
$ bash tests/test.sh tests/oracle/A02_normalize_oracle.h5ad
task        : A02_normalize_log
passed      : True
score       : 1.000
checks: [PASS] counts_layer / x_is_normalized / per_cell_sum / counts_int
```

verifier 依赖已在 `OmicsClaw` conda env 中满足（scanpy 1.11.5 / anndata 0.12.11 /
scipy 1.16.3 / scikit-learn 1.8.0 / numpy 2.0.2）。任务 Dockerfile 只装
`anndata scanpy mudata numpy pandas scipy scikit-learn`，**因此 OmicBench 不需要 Docker**。

### 2.2 三个 agent 都有 headless 单发入口，且本机已装

| Agent | 版本 | 单发入口 | 轨迹/用量 |
| --- | --- | --- | --- |
| OmicsClaw | 0.1.2 | `oc interactive -p "<prompt>" --workspace <dir> -m run -n <cell>` | ✅ stdout 打印 `[Usage: N in · M out \| $X]` |
| Claude Code | 2.1.220 | `claude -p "<prompt>" --output-format stream-json` | ✅ stream-json 全轨迹 |
| Codex | 0.146.0 | `codex exec -C <dir> --json -o last.txt "<prompt>"` | ✅ `--json` JSONL 事件流 |

**本机实证**：`oc interactive -p` 已跑通——它调用 `file_write` 写出文件、返回 `done`、
并打印 `[Usage: 8,088 in · 222 out | $0.0014]`。这就是 Campaign record 需要的
`input_tokens` / `output_tokens` / `cost_usd`。

OmicsClaw 的 agent 工具集是通用的（`file_read/file_write/file_edit/glob_files/grep_files/
list_directory/inspect_data` + `autonomous_analysis_execute` 代码循环），
**不是只能调固定技能**，因此与通用 coding agent 同类可比。

### 2.3 有现成的外部对照基线

- `data/benchmarks/biomnibench-da/OmicOS-BiomniBench/` —— omicverse 官方 harness，
  **7 模型 × 50 任务**已发布结果 + 6 维能力雷达 + 成本分析。其
  `src/omicos_biomnibench/{matrix,runner,client,grader}.py` 是本文 harness 的**参考设计**
  （注意：PolyForm Noncommercial 1.0.0，衍生代码需注意许可）。
  已公布 headline：gpt-5.5 80.7% / ds4-pro 73.9% / gpt-5.4 68.0% / gpt-5.4-mini 44.2%。
- `data/benchmarks/scagent-bench/supplementary_json_results.zip` —— scAgentBench 已发表
  raw metrics（ReAct/LangGraph/AutoGen × 8 LLM）。

---

## 3. 阻塞项：必须先做的 3 件事

### B1 · Campaign schema 结构性禁止跨 agent（必改）

`omicsclaw/skill/benchmark_campaign.py` 把 agent 身份作为**冻结常量**而非对比维度：

```python
# BenchmarkCampaignSpec：单一标量
agent_revision: str; model_id: str; runtime_id: str

# analyze_campaign()：任何不一致直接 raise
if record.agent_revision != spec.agent_revision:      # :489
    raise ValueError("record agent revision mismatch")
if record.model_id != spec.model_id:                  # :495
    raise ValueError("record model identity mismatch")
```

且 `_EXPERIMENT_CONTRACTS` 只允许 4 种 `experiment_kind`，`conditions` 必须**精确等于**
预定元组；没有 `agent_comparison`，也没有 `claude_code` / `codex` 这类 condition。
现状下你无法把跨 agent 结果写进任何合法 record。

**改法（纯加法，勿改既有语义）**：
1. `BenchmarkCondition` 增加 `omicsclaw_agent` / `claude_code` / `codex_cli`（按需扩展）。
2. `BenchmarkExperiment` 增加 `agent_comparison`，其 `_EXPERIMENT_CONTRACTS` 条目的
   baseline 与 anchor 显式声明。
3. 新增 `condition_agent_identities: dict[BenchmarkCondition, AgentIdentity]`，
   `AgentIdentity` 绑定 `agent_revision / model_id / runtime_id / tool_policy_id`。
   `analyze_campaign` 的漂移检查从"对全局 spec 标量"改为"对本 condition 的身份"——
   **condition 内部仍然零漂移**，只是不同 condition 之间允许不同。
4. `agent_comparison` 下豁免 `no_skill records cannot bind Skills` 一类的
   MUSE 专属规则（它们只对 `main_skill_effect` 成立）。

规模：约 120 行 + 回归测试。既有 4 种 experiment_kind 的行为必须逐条保持不变
（`tests/test_benchmark_campaign.py` 是现成的回归网）。

### B2 · 没有执行 harness（必写）

项目自己已经写明这一点：`analyze_campaign()` 返回
`analysis_assurance: "matrix-integrity-only"`、
`causal_claim_status: "not-established-without-execution-harness-provenance"`。

现有 `scripts/run_three_suite_skill_lifecycle_benchmark.py` **不是** agent harness——
它调 `governance.evaluate(skill_id)`，即"技能对着 fixture 跑适配器脚本"，
不是"agent 读 instruction.md 自己干活"。二者不可互换。

**需要新写** `scripts/run_cross_agent_campaign.py`，契约：

| 阶段 | 职责 |
| --- | --- |
| stage | 每 cell 建独立工作区，**只**拷 `environment/data/`，绝不拷 `tests/` |
| prompt | `instruction.md` 原文 + **三方完全相同**的 harness 前言（输入路径 / 输出路径 / 禁止访问项） |
| drive | 按 condition 调对应 agent adapter，施加统一 wallclock + turn 预算 |
| capture | 落 `trajectory.jsonl` / `stdout` / `stderr` / usage，进 `EvaluationArtifactStore` |
| grade | 调 suite 原生 grader，落 `grade.json` |
| record | 产出 `BenchmarkRunRecord` JSONL，交 `analyze_campaign()` |

失败必须落成 record（`outcome=timeout/setup_failure/infra_failure`，score=0），
不能静默跳过——严格分母是这套 schema 最有价值的地方。

### B3 · 模型混淆（最严重的效度问题，必须显式处理）

当前 `.env` 是 `deepseek-v4-flash`；Claude Code 跑 Anthropic 模型；Codex 跑 GPT。
直接比较得到的是 **agent × model 的合成效应**，无法归因给 agent 架构或 skill 系统。

务实做法（不要试图强行统一）：

- **臂 A（as-shipped）**：三方各用各自默认/推荐配置。这是"产品对比"，可发表，
  但结论只能写成"配置 X 的 OmicsClaw vs 配置 Y 的 Claude Code"。
- **臂 B（model-controlled，可选）**：把能对齐的对齐。OmicsClaw 支持
  `--provider/--model`；Codex 支持 `-c model_provider=...` 指向 OpenAI 兼容端点；
  Claude Code 认 `ANTHROPIC_BASE_URL`。三方完全同模型需要一个兼容代理，成本不低。
- 无论哪条臂，`model_id` 必须写进 spec 并**单独成列报告**，
  且结论章节明确写"未做因果归因"。

---

## 4. 公平性与污染：会毁掉结果的 5 个坑

1. **Oracle 泄漏（最致命）**。任务目录下 `tests/oracle/*.h5ad` 就是标准答案。
   cell 工作区**只能**含 `environment/data/`。任何把任务根目录暴露给 agent 的做法
   （`claude --add-dir <task_root>`、`codex -C <task_root>`）都会直接作废整轮结果。
2. **OmicsClaw 已经"见过题"**。以下 case 已写进 `skill.yaml` 的 validation protocol
   和 `skills/*/tests/` 适配器脚本，对 OmicsClaw 属于训练/调优过的题：
   - OmicBench `A02_normalize_log`、`A03_hvg`（`sc-preprocessing`）
   - scAgentBench main PAGA（`sc-pseudotime`）
   - BiomniBench-DA `da-12-2`（`bulkrna-enrichment`）
   **必须从跨 agent 分母中剔除，或单列为 seen-set 分开报告。**
   软提示：OmicBench `F03_paga_pseudotime` 与 scAgentBench PAGA 是同方法不同 case，
   建议一并标注。
3. **训练污染 canary**。OmicBench 带 canary，任何情况下不得把其内容写入训练语料
   （见 `data/benchmarks/README.md`）。
4. **预算不对称**。OmicBench 每个 `task.toml` 自带 `max_turns` / `timeout_sec`
   （A02 = 10 turns / 300 s，B01 = 30 turns / 1500 s），必须**统一施加到三方**：
   - OmicsClaw：`OMICSCLAW_MAX_TOOL_ITERATIONS=<n>`（默认 20，
     见 `omicsclaw/engine/loop.py:55`）——注意默认 20 会**卡住** B01 这类 30 turns 的任务。
   - Claude Code：本版本（2.1.220）**没有 `--max-turns`**，只能用 wallclock
     `timeout <sec>` 约束，并在报告中披露这一不对称。
   - Codex：用 wallclock 约束。
5. **网络与工具权限**。OmicBench 不需要网络；BiomniBench `task.toml` 是
   `allow_internet = true`。同一 campaign 内三方策略必须一致：
   - Claude Code：`--disallowedTools WebSearch WebFetch`
   - Codex：`-s workspace-write`（默认无网络）
   - OmicsClaw：`web_search` / `web_fetch` / `parse_literature` 的禁用开关**待确认**，
     这是 B2 实现前需要验证的一项。

---

## 5. 运行指南

### 前置

```bash
cd /work/zhouweige_data/project/OmicsClaw
conda activate OmicsClaw
export PYTHONDONTWRITEBYTECODE=1

# benchmark cell 根目录（勿放进 repo；/work 余量 408G）
export BENCH_ROOT=/work/zhouweige_data/bench-runs
mkdir -p "$BENCH_ROOT"

# OmicsClaw 需要把 cell 目录加进受信数据目录，否则读 h5ad 会被 path validation 拦
export OMICSCLAW_DATA_DIRS="$BENCH_ROOT,$OMICSCLAW_DATA_DIRS"
```

机器余量：128 核 / 2 TB 内存 / `/work` 408 G 可用 / Docker 26.1.3 / bubblewrap 0.4.0。

### P0 · 单 cell 手工冒烟（**今天就能跑，零新代码**）

目的：在写 harness 之前，先证明"同一道题、三个 agent、同一个 grader"这条链路成立。
选一道 easy 且**不在 seen-set** 的题，例如 `A01_qc_filter`（medium / 6 checks）。

```bash
TASK=data/benchmarks/omicbench/omicbench-A01_qc_filter
TASK_ABS="$PWD/$TASK"

stage_cell () {   # $1 = agent 标签
  CELL="$BENCH_ROOT/p0/A01_qc_filter/$1"
  rm -rf "$CELL"; mkdir -p "$CELL/data" "$CELL/output"
  cp "$TASK_ABS"/environment/data/* "$CELL/data/"     # 只拷 data，绝不拷 tests
  echo "$CELL"
}

# 三方共用的、逐字相同的 prompt
build_prompt () {   # $1 = cell 路径
  cat "$TASK_ABS/instruction.md"
  printf '\n\n---\nHARNESS NOTES (identical for every agent)\n'
  printf -- '- Input fixture directory: %s/data/\n' "$1"
  printf -- '- Write the final AnnData/MuData object to exactly: %s/output/final.h5ad\n' "$1"
  printf -- '- Work only inside %s. Do not search the web.\n' "$1"
}
```

**OmicsClaw**

```bash
CELL=$(stage_cell omicsclaw)
OMICSCLAW_MAX_TOOL_ITERATIONS=$(grep -oP 'max_turns = \K[0-9]+' "$TASK_ABS/task.toml") \
timeout "$(grep -oP 'timeout_sec = \K[0-9]+' "$TASK_ABS/task.toml")" \
  oc interactive -p "$(build_prompt "$CELL")" \
     --workspace "$CELL" -m run -n a01-omicsclaw \
     2>&1 | tee "$CELL/agent.log"
```

**Claude Code**

```bash
CELL=$(stage_cell claude-code)
timeout "$(grep -oP 'timeout_sec = \K[0-9]+' "$TASK_ABS/task.toml")" \
  claude -p "$(build_prompt "$CELL")" \
     --output-format stream-json --verbose \
     --add-dir "$CELL" \
     --disallowedTools WebSearch WebFetch \
     --permission-mode acceptEdits \
     > "$CELL/trajectory.jsonl" 2> "$CELL/agent.err"
```

**Codex**

```bash
CELL=$(stage_cell codex)
timeout "$(grep -oP 'timeout_sec = \K[0-9]+' "$TASK_ABS/task.toml")" \
  codex exec -C "$CELL" -s workspace-write --skip-git-repo-check \
     --json -o "$CELL/last_message.txt" \
     "$(build_prompt "$CELL")" \
     > "$CELL/trajectory.jsonl" 2> "$CELL/agent.err"
```

**统一评分**（三方逐字相同的一条命令）

```bash
for A in omicsclaw claude-code codex; do
  CELL="$BENCH_ROOT/p0/A01_qc_filter/$A"
  printf '\n=== %s ===\n' "$A"
  if [ -f "$CELL/output/final.h5ad" ]; then
    bash "$TASK_ABS/tests/test.sh" "$CELL/output/final.h5ad" || true
  else
    echo "no final.h5ad -> score 0 (invalid_output)"
  fi
done
```

**P0 验收**：三方都产出了可被同一 verifier 打分的结果（分数高低不重要），
且没有任何一方接触过 `tests/`。

### P1 · OmicBench 跨 agent（主实验，建议首发）

选 OmicBench 打头阵的理由：**确定性 grader、无需 judge 凭据、无需 Docker、单位成本最低**。

预注册（执行前冻结，之后不许改）：

- 分母：44 − 2（seen-set A02/A03）= **42**，`subset_kind: preregistered_subset`，
  `selection_rationale` 写明剔除理由。若只跑分层子集，同样预先冻结 case 列表并写进
  `coverage_manifest_digest`。
- conditions：`omicsclaw_agent` / `claude_code` / `codex_cli`（B1 落地后）。
- repeats：**≥3**（agent 是随机的；OmicOS 记录过同题 5 次 `[48,48,52,75,87]` 的双峰散布，
  单次结果不可信）。
- `pass_threshold`：OmicBench 原生定义 pass = 全部 check 通过 ⇒ **1.0**；
  score = 通过比例。不要事后调阈值。
- 每 cell 预算取该 `task.toml` 的 `max_turns` / `timeout_sec`。

规模：42 × 3 agents × 3 repeats = **378 cells**。128 核可并行 8–16 cell
（每 cell 计算不重，瓶颈是 LLM 往返）。

```bash
# B2 落地后
python scripts/run_cross_agent_campaign.py \
  --suite omicbench \
  --spec configs/campaigns/omicbench-agent-comparison-v1.json \
  --output "$BENCH_ROOT/omicbench-agentcmp-r1" \
  --parallel 12

python scripts/analyze_benchmark_campaign.py \
  --spec configs/campaigns/omicbench-agent-comparison-v1.json \
  --records "$BENCH_ROOT/omicbench-agentcmp-r1/records.jsonl" \
  --markdown "$BENCH_ROOT/omicbench-agentcmp-r1/report.md"
```

### P2 · BiomniBench-DA（加 process-level 维度）

额外前置：`GEMINI_API_KEY` 或 `GOOGLE_API_KEY`（`task.toml` 的
`[verifier.env]` 声明 `GEMINI_API_KEY` + `MODEL_NAME=gemini-3.5-flash`）。
没有凭据就只能像现有 pilot 那样退到 deterministic preflight，**不能报官方分数**。

价值：这是唯一能和 **7 个已发布模型基线**直接对齐的 suite，
且能给出 6 维能力雷达（data handling / method selection / statistical rigor /
biological interpretation / scientific reasoning / source reliability）。

注意：`OmicOS-BiomniBench/docs/failure-cases/` 已认定 4 道题 benchmark 本身有缺陷
（`da-12-4` / `da-18-7` / `da-20-1` / `da-6-2`）。要么沿用它们的
"capability mean 剔除 + all-50 mean 并报"的双口径，要么全留——但必须**执行前**决定。

### P3 · scAgentBench（可选，成本最高）

需要对接 `bioagent-benchmark/evaluation/` 的多维指标，且原始 conda 环境包（14 GiB，
Zenodo 17455069）未下载。除非要和其 8-LLM 基线对齐，否则优先级低于 P1/P2。

---

## 6. 报告纪律

- **三套 suite 的分数不得合并**——它们量的不是同一个东西
  （end-state 确定性 vs. process rubric vs. 多维指标）。
- 严格分母：missing / timeout / setup / infra / invalid_output 一律计 0，
  不得从分母剔除。`analyze_campaign()` 已经强制这一点，别绕过它。
- seen-set 单列。
- `model_id` 单列，结论不做跨 model 因果归因。
- 披露预算不对称（Claude Code 无 turn cap）。
- 每个 cell 绑定 content-addressed artifact bundle；`covered` 的 record
  必须有 `trial_id` / `isolation_id` / `artifact_bundle_ref`。

---

## 7. 工作量估计

| 项 | 规模 | 阻塞 P1？ |
| --- | --- | --- |
| B1 Campaign schema `agent_comparison` | ~120 行 + 回归测试 | ✅ 是 |
| B2 `run_cross_agent_campaign.py` + 3 个 adapter | ~600–800 行 | ✅ 是 |
| 验证 OmicsClaw web 工具禁用开关 | 半天 | ✅ 是 |
| B3 model-controlled 臂（可选） | 需兼容代理 | ❌ 否（臂 A 可先发） |
| P2 judge 凭据 | 申请 | ❌ 否 |

P0 今天可跑。B1+B2 完成后 P1 即可执行。

---

OmicsClaw 是多组学研究与教育工具，非医疗器械，不提供临床诊断。
基准分数不能替代领域专家对方法学和科学结果的复核。
