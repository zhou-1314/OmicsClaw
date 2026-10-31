CLI、Desktop 和 Channel 共用 AgentApp。CLI 提供终端 REPL、审批与会话恢复；Desktop 提供 HTTP、SSE、审批和文件服务；Channel 接入七种聊天平台，启动要求 owner allowlist。它们的交互方式不同，但后端执行政策应当一致。

本次源码检查确认 Desktop 已经提供 /chat/permission 和 /chat/abort。旧文档曾把它们列为未移植缺口，本报告以快照中的实际代码为准。后端接口存在仍不等于客户端端到端流程已经验收。

仓库分工也需要明确：OmicsClaw 负责政策、执行、持久化和 HTTP 契约；单独的 OmicsClaw-App 负责 Electron、Next.js 界面与 SSH 隧道等客户端能力。本次没有读取 App 仓库，因此不对前端完整体验作验证结论。入口代码在 omicsclaw/entry/ 下。