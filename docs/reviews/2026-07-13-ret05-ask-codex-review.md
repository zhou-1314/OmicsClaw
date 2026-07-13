# RET-05 AnnData compatibility graph / candidate plan — Ask Codex 复审记录

日期：2026-07-13
审查方式：独立 Ask Codex，只读 live tree 与对抗测试；未使用 Claude Code
最终结论：**PASS，无 Blocker / High**

## 验收边界

本里程碑只验收 singlecell/spatial 的 **AnnData compatibility 纵切**：机器可读 output contract、
候选 producer→consumer 图、选定计划的 topo/provenance、复合 intent 保真和执行前确认门。
它不等于 8 域 workflow graph，也不包含真正的 topo executor、失败级联或 evolution loop。

## 首轮 FAIL 与整改

首轮审查拒绝验收，原因是：确认仅存在于 prompt；`processed.h5ad` 文件名被过度解释为预处理；
全局候选关系被强制 DAG 化并产生反向/漏边；复合 resolver 静默丢弃 disconnected intents。

整改后：

- `outputs.anndata.processing_state = raw | standardized | preprocessed` 成为显式 schema；generic
  preprocessing 边还要求 producer/consumer modality 已知且相交，path/file/domain 合同兼容。
- 全库 compatibility graph 与 selected-plan DAG 分离。派生边默认 `alternative + reviewed=false`；
  `skill_dag_reviews.yaml` 用完整 edge identity 治理 kind/scope/reviewer，stale identity fail closed。
- 无边 intent 保留为 unresolved/parallel，connected selection 才输出 topo order；普通科学描述、
  comparison、explanation、how-to 不会因 `and` 生成执行计划。
- plan digest 绑定 chat-scoped pending state。严格 standalone 确认前，execution hook 在 executor
  前阻断 `omicsclaw` 与 `autonomous_analysis_execute`；通用 approval 不会重试 `hook_blocked`；
  确认跨普通 CHAT turn 保留，新 analysis/cancel 替换或清除，确认后仍拒绝计划外 skill。

第二/三轮审查继续发现并关闭了 no-`omicsclaw_dir` fail-open、autonomous fallback、确认状态过早
删除、单字符 `y` 子串误确认、跨 turn 返回强制降回 false、计划外 skill 以及 advisory plain-and
误判。最终对抗复审未再发现可执行旁路。

## 最终证据

- 图生成物：95 nodes / 52 edges；32 exact / 20 generic；6 reviewed；`has_cycle=false`。
- 域覆盖：singlecell 28 edges、spatial 24 edges；其他 6 域 0（刻意保留在有限声明中）。
- RET-05/registry/router/engine/hook 定向回归：**202 passed**。
- 独立审查另跑：154 项 RET-05 + 96 项 RET-04b/runner/precondition 回归通过。
- routing oracle：precision@1、top-3 recall、domain accuracy、decision accuracy、
  precondition accuracy 全部 1.000；hallucinated alias rate 0.000。
- `generate_skill_dag --check`、`generate_catalog --check`、`generate_skill_md --all --check`、
  95 个 `skill.yaml` validation 与 `git diff --check` 通过。
- `tests/test_tool_execution_hooks.py` 已加入 PR CI，物理验证未确认时 executor 调用数为 0。

扩大到 385 项 acquisition/routing/registry/engine/runner CI-like 用例时得到 **382 passed、3 failed**；
3 个失败都位于 skill scaffolder 的临时 AnnData 写入，根因是当前本地 pandas `ArrowStringArray`
与 anndata/h5py writer 组合不兼容，不经过 RET-05 graph、resolver 或执行门路径。该环境型失败保留为
已知验证边界，不计作本里程碑 green，也没有为通过测试而改动无关 scaffolder 代码。

全仓 pytest 曾启动，但在与 RET-05 无关的既有/环境型用例（consensus 指标依赖、KG stage、
provider/auth、context compaction、未跟踪工程文档断言等）出现多处失败后于约 46% 主动停止；
因此本文只声明上述可复现的里程碑定向门，不虚报全仓 green。

## 非阻断后续项

- review overlay 尚不能以 `accepted/rejected` 显式拒绝误派生边；46 条 unreviewed alternative
  仍只能作为候选证据使用。
- 标准 spatial 五步 pipeline 的 induced edge 仍为空；现有 smoke 只证明无反向冲突，后续需
  非空锚点或明确并行阶段合同。
- `condition_scope` 尚无 method-scoped 实例；其他 6 域需各自 artifact/interface 语义，不能套用
  AnnData 规则。
- pending plan 尚无 TTL，key 仍沿用 chat id；长期应使用专用不可覆盖 gate action。
- topo 顺序执行、失败级联、unresolved plan 执行策略、candidate-wide penalty 与 M3 演化闭环
  不计入本次 PASS。

OmicsClaw 是多组学研究与教育工具，非医疗器械，不提供临床诊断；图合同验收不能替代领域专家
对具体方法链与科学结果的复核。
