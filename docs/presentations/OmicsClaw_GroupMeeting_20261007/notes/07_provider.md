provider 把模型端点的差异收在适配器里。engine 面向 LLMProvider，只需要传入消息和工具；普通生成、流式生成和配置变体分别通过 generate、generate_stream 和 bind 表达。当前主要适配 OpenAI-compatible 和 Anthropic Messages 两类协议。

模型名称、温度和输出上限属于 provider 实例。需要变体时通过 bind 处理，engine 不需要知道某个厂商怎样命名这些参数。适配层还负责把流式事件、工具调用和错误转换成框架统一的形式。

统一协议可以降低替换模型的成本，却不能保证不同模型有相同的工具选择能力、推理质量或长上下文表现。因此，接口测试和真实模型评估仍然是两类证据。源码可从 omicsclaw/provider/base.py 和 factory.py 开始阅读。