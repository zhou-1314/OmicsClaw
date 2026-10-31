教程前五讲建立架构、消息与循环、Thinking、Provider 和 Registry，对应本报告的核心执行层。六到十讲讨论基础原语、编辑容错、并发、飞书输出和提示组装，对应物理交互与运行规范。十一到十五讲讨论会话、压缩、记忆计划、恢复与提醒，对应状态和上下文管理。十六到二十讲再用审批、子任务、成本、追踪和 benchmark 约束与度量执行。

这些是讲解关系，不是逐项照搬实现。最需要注意的差别是当前没有强制双阶段 Thinking，默认生产工具调度有安全批次屏障，记忆与计划分别使用 SQLite、摘要和 PlanBook，恢复与提醒也没有被扩写成通用模板仓库或全能死循环检测器。

MCP、skillenv、科学函数库、分析模块和 replay 等内容则按 OmicsClaw 的实际需求补充。逐讲页码、源码和差异保存在 sources/tutorial_alignment.md，原文件路径与哈希见 tutorial_manifest.json，教程作者是 Tony Bai。