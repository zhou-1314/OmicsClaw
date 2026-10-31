# 逐页依据

01. OmicsClaw：`README.md`
02. 模型能决定下一步，执行更需要支撑：`sources/background_reference.md`
03. OmicsClaw 需要把每一步判断接稳：`OMICSCLAW.md`
04. 系统地图：交互、决策、执行与证据：`omicsclaw/entry/assembly.py`
05. engine：一个内核维持 ReAct 循环：`omicsclaw/engine/loop.py`
06. schema：先统一组件之间的语言：`omicsclaw/schema/__init__.py`
07. provider：把接口差异收在适配层：`omicsclaw/provider/base.py`
08. 通用工具连接模型与科学方法：`omicsclaw/tools/registry.py`
09. PromptAssembler：把信息汇到一次请求：`omicsclaw/context/prompt.py`
10. skills：先看目录，再加载方法：`omicsclaw/skills/index.py`
11. 压缩历史时，保留下一步需要的依据：`omicsclaw/context/progressive.py`
12. memory 记事实，planning 记进度：`omicsclaw/memory/__init__.py`
13. entry 把组件接成可管理的会话：`omicsclaw/entry/session.py`
14. 让错误进入判断，而不是重复同一动作：`omicsclaw/engine/retry.py`
15. permission：先决定能否执行：`omicsclaw/permission/gate.py`
16. sandbox 与 local-first 的边界：`omicsclaw/sandbox/__init__.py`
17. skillenv：补依赖时保留基础环境：`omicsclaw/skillenv/overlay.py`
18. MCP 扩展工具，subagent 分担子任务：`omicsclaw/subagent/task_tool.py`
19. observability：让过程可以定位：`omicsclaw/observability/__init__.py`
20. 用任务结果判断是否完成：`omicsclaw/bench/run.py`
21. 框架与科学代码通过文件契约协作：`tests/sdk/test_boundary.py`
22. 同一算法，函数库与 CLI 各负其责：`skills/_sdk/notebook/__init__.py`
23. 分析模块把代码与输出放在明确位置：`OMICSCLAW.md`
24. run、status、replay 分别回答不同问题：`skills/_sdk/notebook/run.py`
25. 先交付验证结果，审查与接受由用户触发：`omicsclaw/entry/review.py`
26. 四模块案例：检查应落在实际输出上：`docs/plans/0074-singlecell-skill-migration-delivery.md`
27. 科学分析需要连续判断，而现有评测常只看局部：`sources/genebench_pro_reference.md`
28. GeneBench-Pro：把连续判断变成可检验任务：`sources/genebench_pro_reference.md`
29. 发现异常之后，分析决策是否随之改变？：`sources/genebench_pro_reference.md`
30. OmicsClaw：让诊断证据进入后续决策：`sources/genebench_pro_reference.md`
31. 沿着一次分析，研究三类决策机制：`sources/research_direction_conversation.md`
32. 改变实验设计，Agent 会改变决策吗？：`sources/research_direction_conversation.md`
33. 先证明任务有效，再验证方法增益：`sources/research_direction_conversation.md`
