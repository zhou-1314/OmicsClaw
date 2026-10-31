分析模块是可以独立解释和交付的阶段，例如 QC、聚类或注释；技能则是可以跨模块复用的方法单元。两者不能混用，一个模块往往会组合多个技能。

跨模块的问题、数据、计划和决策放在 docs/analysis_strategy/STRATEGY.md。每个 analysis 子目录保存模块 README 与 Python、R 步骤，说明方法、输入、参数选择和验证代码；对应的 results 子目录保存图表、中间文件、日志、报告、notebook、provenance、审查和 baseline。

这样的目录安排让代码和输出有明确归属，也为回放和冻结建立边界。聊天中解释过某个选择，并不等于这个选择已经进入可交付材料；应当把必要的方法说明保留到模块文档里。目录契约和执行器实现见 OMICSCLAW.md 与 skills/_sdk/notebook/。