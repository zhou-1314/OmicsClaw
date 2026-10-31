如果主循环里为每个工具写一个分支，新增方法就会不断修改 engine。教程第五讲用 Registry 解决这个耦合：先 register 挂载工具对象，再通过 available_tools 暴露名称、描述和参数定义，模型返回调用后由 execute 根据名称路由执行，结果统一成为 ToolResult。

OmicsClaw 的工具对象同时提供定义、策略与执行行为，registry 保持定义的注册顺序。模型看到的定义与本机策略分开，风险级别、审批方式和并发声明不需要塞进工具参数 Schema。具体部署负责决定这一轮有哪些工具可见。

registry 通过方法形状满足 engine 的 Protocol，无需反向导入 engine。这样可以用简单替身测试循环，也可以独立替换工具实现。实现见 omicsclaw/tools/registry.py、base.py，以及 engine/executor.py。