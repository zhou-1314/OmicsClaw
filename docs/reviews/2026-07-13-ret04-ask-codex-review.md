# RET-04 Ask Codex 独立审查记录

日期：2026-07-13
范围：当前未提交的 RET-04 输入前置条件安全纵切
审查器：Ask Codex CLI，`gpt-5.6-terra`，`xhigh`，只读/临时会话
结论：**PASS — auto-route safety 范围内无 Blocker/High**

本轮按维护者要求仅使用 Ask Codex 独立审查，没有使用 Claude Code。审查器不能编辑、提交或
推送，并明确忽略了无关的未跟踪目录 `docs/agents/`。所有发现均由主实现方以本地回归测试
复现后再整改，未直接照单接受。

## 审查过程

### 第一轮：FAIL，2 High

1. 损坏或未完成探测的 `.h5ad` 可能被标记为 `eligible`，从而穿过 auto-run 门。
2. `mode=file` 探测全局首个上传文件，但执行使用当前 `session_id`，存在跨会话错配。

整改：

- `inspection_error` 必定进入 `blocked/execution_ready=false`；缺少必需 file type/modality 证据
  进入 `needs_preparation`，不再伪装 eligible。
- 上传文件的探测与执行统一绑定同一个 session record。
- dotted/compressed 文件名按实际 omics 类型规范化，例如 `patient.v1.h5ad -> h5ad`、
  `reads.fastq.gz -> fastq`。
- 可读路径的真实探针优先于 `resolve_capability` caller-supplied profile；后者只作规划提示。
- oracle 的 precondition 命中必须同时满足 `precondition_evaluated=true`。

### 第二轮：FAIL，1 High

Ask Codex 进一步复现：session 已绑定、但上传文件在探测前消失时，`is_file()` 判断会跳过
profile，保留 `execution_ready=true` 并到达 shared runner。

整改：只要 auto-route 已选择输入路径，就必须调用 `probe_input_profile()`；不存在的 `.h5ad`
产生 inspection error 并在 runner 前硬停止。回归测试用一个已消失的 session 上传路径证明
runner 不会被调用。

同轮 Medium 整改：

- 真实路径优先规则下沉到 `resolve_capability()`，覆盖直接 API/AnalysisRouter caller。
- 配置 `precondition_accuracy` 时，oracle 必须同时含 `eligible / needs_preparation / blocked`
  三态案例，避免空语料得到 1.0。
- 两个相关 CI job 均固定 `anndata==0.11.4`，避免 solver 漂移；该版本与仓库声明的
  `anndata>=0.11` 合同一致。
- 缓存文档收紧为 path + mtime_ns + size **签名变化**时失效，不再声称检测所有内容变化。

### 第三轮：PASS

Ask Codex 最终确认：

- 当前 session 上传文件被同源探测和执行；即使文件已消失也会 fail closed。
- 本地真实路径覆盖 caller assertion。
- oracle 强制三态语料，且 precondition case 必须真实执行 evaluator。
- 两个 CI job 的 AnnData 版本均已固定。
- RET-04 auto-route safety 范围内无剩余 Blocker/High。

## 本地复验证据

- RET-04/resolver/router/oracle 定向回归：**106 passed**。
- PR CI acquisition + routing 同构集合：**247 passed / 3 failed**；3 个失败均为本机已有
  `anndata 0.10.6 + pandas 2.3.3` 在 acquisition 子进程重写 ArrowStringArray 时的
  `IORegistryError`。scaffolder 逻辑未被本纵切修改；CI 已固定到 `anndata==0.11.4`。
- Routing oracle：29 cases / 8 domains；precision@1、top-3 recall、domain、decision、
  precondition accuracy 均为 **1.000**，hallucinated alias rate **0.000**。
- 95/95 `skill.yaml` 有效；catalog、parameters、SKILL.md、version、routing table、domain index、
  description drift、requires audit、skill lint 均通过。
- Routing budget：all-tools JSON **43,259 / 45,000**；`git diff --check` 通过。

## 明确边界

本次 PASS 只覆盖 RET-04 的 auto-route safety 纵切，不表示完整 skill 审计闭环完成：

- 显式指定 skill 的执行入口尚未统一接入硬门。
- 非 `.h5ad` 输入目前只做文件名/类型探测，尚无结构化内容探针。
- 尚未实现 candidate-wide precondition penalty、compatibility DAG、复合 topo chain。
- `sc_batch` 仍未收敛为通用 evaluator 的消费者。
