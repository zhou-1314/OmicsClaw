# 逐页依据

01. OmicsClaw：`README.md`
02. 模型能决定下一步，执行更需要支撑：`sources/background_reference.md`
03. 分析助手为什么需要运行时支撑：`OMICSCLAW.md`
04. 系统地图：交互、决策、执行与证据：`omicsclaw/entry/assembly.py`
05. engine：一个内核维持 ReAct 循环：`omicsclaw/engine/loop.py`
06. schema：先统一组件之间的语言：`omicsclaw/schema/__init__.py`
07. provider：把模型差异收在适配层：`omicsclaw/provider/base.py`
08. ToolRegistry：注册、描述、执行集中在一处：`omicsclaw/tools/registry.py`
09. PromptAssembler：把规则按职责组装：`omicsclaw/context/prompt.py`
10. skills：先看目录，再加载方法：`omicsclaw/skills/index.py`
11. context：把有限窗口留给当前判断：`omicsclaw/context/progressive.py`
12. memory 与 planning 保存不同信息：`omicsclaw/memory/__init__.py`
13. entry 把组件接成可管理的会话：`omicsclaw/entry/session.py`
14. 失败之后，谁负责恢复？：`omicsclaw/engine/retry.py`
15. permission：策略决定，入口负责询问：`omicsclaw/permission/gate.py`
16. sandbox 与 local-first 的边界：`omicsclaw/sandbox/__init__.py`
17. skillenv：补依赖时保留基础环境：`omicsclaw/skillenv/overlay.py`
18. MCP 与 subagent 扩展两类能力：`omicsclaw/subagent/task_tool.py`
19. hooks 与 observability 观察已有执行：`omicsclaw/observability/__init__.py`
20. 评估要检查动作与副作用：`omicsclaw/bench/run.py`
21. 框架与科学代码通过文件契约协作：`tests/sdk/test_boundary.py`
22. 同一算法，函数库与 CLI 各负其责：`skills/_sdk/notebook/__init__.py`
23. 分析模块把代码与输出放在明确位置：`OMICSCLAW.md`
24. run、status、replay 分别回答不同问题：`skills/_sdk/notebook/run.py`
25. 先交付验证结果，审查与接受由用户触发：`omicsclaw/entry/review.py`
26. 四模块案例：检查应落在实际输出上：`docs/plans/0074-singlecell-skill-migration-delivery.md`
27. 下一阶段优先验证什么？：`README.md`
