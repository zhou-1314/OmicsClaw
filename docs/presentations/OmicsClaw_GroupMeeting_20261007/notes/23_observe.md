工具执行链中，permission 先决定调用能否发生，hooks 位于其内侧，负责拦截和 JSONL 审计，最后才执行实际工具。因此，被权限拒绝的调用不会进入内侧 hook；审计覆盖范围要按这一顺序理解。

observability 从 interaction、turn 到 llm_request、tool 建立运行跨度，并记录六类度量：模型耗时、输入 token、输出 token、工具次数、工具耗时和轮次数。它消费已有 EngineEvent，OpenTelemetry 是可选输出，避免为了观测而让核心循环承担额外日志职责。

默认不采集对话和工具正文载荷，保留时延与调用结构，同时限制内容采集。需要调试正文时应当明确配置和数据范围，不能把度量已开启理解成全部内容都已保存。实现见 omicsclaw/hooks/ 与 observability/。