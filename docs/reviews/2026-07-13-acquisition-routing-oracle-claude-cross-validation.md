# Acquisition 泛化与全域 Routing Oracle：Claude Code 交叉验证

日期：2026-07-13

审查器：Claude Code 2.1.207（只读 plan mode）

范围：本工作树中的 acquisition structured abstraction、facade-free 生成脚本、capability
resolver、8 域 oracle/evaluator/CI 与对应测试。

## 裁决

Claude 第一轮未发现 acquisition/oracle 核心正确性或安全缺陷，但指出一个阻断级流程缺口
与四类覆盖/泛化风险。整改后第二轮确认 F1–F5 均关闭，并额外发现 meta-routing 的真实
false positive；该问题也已修复并写入版本化 oracle。第三轮窄范围复审裁决 **PASS**，
确认前述 High finding 已关闭，未发现 regex 边界的新高严重度 false positive/negative。

## 发现与处置

| ID | Claude 发现 | 处置与可复验证据 | 状态 |
| --- | --- | --- | --- |
| F1 | 新增 regression tests 未由 PR CI 执行 | `.github/workflows/pr-ci.yml` 显式运行 13 个 acquisition/routing/analysis-router 模块；oracle 测试反向断言 CI 中必须出现关键路径 | closed |
| F2 | PTM/scRNA trigger 与评估措辞过近 | 删除 4 个 benchmark-near trigger；改用通用 scRNA/scATAC 模态和显式 workflow-stage 信号；golden + oracle 仍通过 | closed |
| F3 | oracle 只有 happy path，可能被多 coverage/重复 query 刷分 | 强制单一 coverage 与 decision/coverage 对齐、query 规范化去重；覆盖 malformed corpus、partial、幻觉 alias、CLI exit 1/2 | closed |
| F4 | `step:N` 两步组合只有实现、无真执行测试 | 动态加载生成脚本，证明 step 2 实际读取 step 1 的 `.h5ad`，并核对两步参数、内容链和最终 artifact | closed |
| F5 | AST fail-closed 分支只靠人工审查 | 6 类失败状态、trace mismatch、模糊 lineage、非法参数、call 数不一致、控制流均直接测试 `reusable=False/facade_free=False` | closed |
| F6 | `route/choose + analysis/pipeline` 宽松共现硬切 orchestrator | 元意图收紧为 `which/choose ... skill`、`route this query/request` 或显式 `orchestrate`；3 条负向单测，2 条写入 26-case oracle | closed |

## 独立复验

- PR CI 同构 acquisition/routing/analysis-router 集合：**228 passed**。
- 新增 bot executor + dynamic skill listing 接线：**32 passed**。
- Claude 第三轮独立重跑 resolver + golden + oracle：**62 passed**。
- Routing oracle：**26 cases / 8 domains**；全局及逐域 precision@1、top-3 recall、
  domain accuracy、decision accuracy 均为 **1.000**；hallucinated alias rate **0.000**。
- `skill_lint.py --all`、routing budget、95 个 skill.yaml、catalog/SKILL.md/parameters/
  8 个 INDEX/CLAUDE routing/description drift 均通过。

## 边界

此结论仅覆盖 trace-provable、线性、以 `.h5ad` 为跨步 artifact 的 call composition，不能
外推到任意 Python 科学后处理、复杂分支/DAG、非 `.h5ad` artifact 或真实流量 100% routing
准确率。完整四阶段审计闭环仍缺 RET-04/05 的 precondition/DAG 和 M3 人工门控 evolution。

OmicsClaw 是多组学研究与教育工具，非医疗器械，不提供临床诊断；工程测试不能替代领域
专家对方法学与科学结果的复核。
