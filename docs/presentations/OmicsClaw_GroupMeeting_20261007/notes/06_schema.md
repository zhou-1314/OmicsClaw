schema 首先统一组件之间交换的语言，包括 Message、ToolCall、ToolResult、Usage 和 StreamChunk。provider、engine、tools 都依赖这些类型，而 schema 本身不依赖其他项目层，只需要标准库即可导入。这样底层协议不会因为科学依赖或供应商 SDK 的加载问题而失效。

这个边界减少了循环导入，也把供应商特有字段的解释留在适配层。工具参数保留原始 JSON 文本，避免框架提前解析再序列化时无意改变原始协议证据。消息契约还保留 reasoning_content 和 tool 角色，以支持需要历史推理字段的端点。

这里的设计收益是组件可以围绕稳定契约分别测试和替换；代价是跨层变化必须先说明契约如何变化，不能让某个供应商的临时字段自然扩散到整个系统。源码入口是 omicsclaw/schema/。