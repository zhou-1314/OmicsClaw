# 教程与 OmicsClaw 的对应关系

讲解参考是 Tony Bai《从0开始构建 Agent Harness》的本地第 01 至 20 讲，原文件位于 `/workspace/dataset/private/zhouwg_data/Agent Harness搭建教程`。使用未带重复导出时间后缀的正文版本，跳过评论、音频、图片和同目录下的 ppt-master 工具仓库；文件路径和 SHA-256 见 `tutorial_manifest.json`。讲解采用问题、机制、实现和验证的顺序，以下是按当前 OmicsClaw 源码作出的对应，不代表项目逐项照搬教程。

| 教程 | 讲解问题 | PPT 页 | 当前实现与取舍 |
|---|---|---|---|
| 01 架构演进 | 模型怎样持续与环境交互 | 2、4、5 | 组件按职责分层，entry 负责组装；Agent 动态决策与科学分析模块的显式依赖并存，不把所有 DAG 都描述成错误设计。 |
| 02 Main Loop | 工具结果怎样进入下一次决策 | 6、8 | schema 统一消息契约；run/run_stream 复用 _kernel；区分正常收敛、轮数耗尽和输出截断。 |
| 03 Thinking | 是否在每轮行动前强制增加一次无工具规划 | 9 | 当前 _kernel 没有教程的强制双阶段请求。原生推理配置和 reasoning_content 不等同于额外一次模型调用。 |
| 04 Provider | 不同厂商协议怎样统一 | 7、9 | OpenAI-compatible 与 Anthropic 适配为统一消息、流事件与错误；配置保留在实例，bind 创建变体。 |
| 05 Registry | 增加工具是否必须改循环 | 10 | register、available_tools、execute 分别负责挂载、定义和路由；策略与模型可见定义分开。 |
| 06 基础工具 | 如何避免把每个科学方法都注册为专用工具 | 11、23 | read_file/write_file/edit_file/bash 配合 use_skill；保留权限和沙箱配置，不把教程的 YOLO 讨论当默认无审批。 |
| 07 Edit | 格式误差如何容错且不误改 | 12 | 四级匹配，每级唯一；拒绝空锚点；审批后在锁内复读；fuzzy 命中后重读确认。 |
| 08 并发 | 同轮多调用是否足以证明独立 | 13 | concurrency_safe 声明驱动安全批次，非安全调用独占；结果保序。没有采用无条件全部并发。 |
| 09 飞书 | 引擎怎样独立于交互渠道 | 20、21 | EngineEvent 与 entry 入口适配，三入口共用 AgentApp；同会话排队，跨会话并发。 |
| 10 Prompt / Skills | 如何组织运行规范与方法说明 | 14、15 | OMICSCLAW.md 是分析 Agent 运行契约；PromptAssembler 按序组装，技能先目录后正文。动态 system 块仍影响前缀缓存。 |
| 11 Session | 如何防止会话互相覆盖、历史无限增长 | 17、20 | SessionRegistry 隔离会话执行，SQLite 保存会话；token 预算与渐进压缩约束模型请求，不是简单截取最近 N 条。 |
| 12 Compaction | 怎样减少载荷并保留工具调用关系 | 16 | 卸载、局部总结、全面总结、紧急截断逐级处理，保持调用与结果配对；阈值收益还需真实流量验证。 |
| 13 Memory / Plan | 压缩后任务目标如何存续 | 17、18 | SQLite/FTS5/MEMORY.md 和 PlanBook 分工；不是仅靠模型自觉读取 PLAN.md/TODO.md。 |
| 14 Error Recovery | 失败应交给哪层处理 | 19、36 | 工具错误成为观察，provider 暂时故障有限重试，总 deadline 不再重试；未实现教程同款统一提示模板仓库。 |
| 15 Reminders | 提醒怎样在下一次判断时仍可见 | 18 | PlanInjector 在 compaction 后追加至 sent，不污染 history；默认连续只读且无计划八轮提醒一次；不是通用重复调用检测器。 |
| 16 Middleware / Approval | 如何在执行前等待人类决定 | 22、23 | PermissionGate 决策，entry 提供审批通道；等待暂停工具计时；hooks 在权限检查内侧。 |
| 17 Subagent | 探索上下文怎样不占满主任务 | 25、32 | task 运行独立上下文与受限工具集，entry 组装子运行；module-reviewer 仅按用户当前请求触发。 |
| 18 Cost | 如何记录资源消耗 | 26 | Usage、provider 包装及六类 instruments；没有把 token 直接标成固定货币成本。 |
| 19 Tracing | 如何定位具体失败或耗时节点 | 26 | interaction/turn/llm_request/tool 跨度树，消费既有 EngineEvent；OTEL 可选，默认不采集正文。 |
| 20 Benchmark | 如何检验动作和副作用 | 27、37 | Case 走生产 build_app，检查工具轨迹、审批和工作区差异；ScriptedProvider 与真实模型路由评估分开。 |

## 本轮项目状态更新

代码版本为 `90a3bec3e6549bc33dddbf67e4b23869736c49d1`。本次运行技能加载器得到 88 个技能、0 skipped；递归枚举得到 83 个 `_api.py`，分域为单细胞 27、空间 16、bulk RNA-seq 14、基因组 10、蛋白组 8、代谢组 7、文献 1。五个 CLI-only 技能在第 38 页逐项列出，XCMS 的真实 raw-MS 拒绝边界也单独说明。

当前审查默认关闭。验证后先展示报告，用户请求才调用独立审查；Desktop 的请求只授权当前新回合。审查不授权自动修复或重跑。用户明确接受时使用当前审查；只有明确接受且明确跳过审查，才可使用 `--skip-review`。

第 37 和 45 页的 7,175、7,206 和 41 个示例回放来自计划 0076 已保存的验收记录。本次只重新核对目录、接口和演示文件，没有重跑全套科学实验，也没有请求真实模型进行新的性能测试。
