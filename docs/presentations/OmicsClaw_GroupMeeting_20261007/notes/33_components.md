这页按职责提供代码定位。决策与执行包括 schema、provider、engine、tools、permission 和 sandbox；知识与任务状态包括 context、skills 加载器、memory 和 planning；组装与扩展包括 entry、launch、MCP、subagent、hooks、observability 和 evals。

科学方法侧，skillenv 负责依赖探测与 overlay，skills/_sdk 提供机械辅助和步骤执行器，domain/_lib 放域内共享实现，_api.py 暴露可组合的方法函数。evals 则让框架行为测试和真实模型评估有独立位置，避免与科学算法测试混成一个数字。

还需要留意历史文档：common、attachments 仍在当前快照中，而 remote、routing、surfaces、ensemble、runtime 已不在当前代码树。不要根据旧目录说明去使用已删除入口。本页依据实际目录枚举和 sources/repository_snapshot.json。