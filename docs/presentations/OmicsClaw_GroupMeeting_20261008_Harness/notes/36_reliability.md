近期可靠性修复集中在两类具体失败边界。第一类是模型请求的总 deadline：它覆盖 SDK 重试、首响应等待和流式读取，预算耗尽后 engine 不再额外重试；流清理另有有界时间额度，避免清理过程无限拖延。

这个机制仍有异步执行的限制：如果同步代码阻塞事件循环，异步 deadline 无法及时打断它。第二类是审查材料绑定：审查前后比较文件哈希，框架原样保存 reviewer 回复，accept 再核对 receipt 与 replay。它能发现材料与旧审查不一致，但 receipt 没有签名，不能抵抗同时改写材料和回执的写入者。

目前也没有实现中断交换的自动恢复，已完成的工具副作用不会回滚。详细修复和验证范围保存在 docs/FRAMEWORK-REBUILD.md 的 runtime reliability follow-up 部分。