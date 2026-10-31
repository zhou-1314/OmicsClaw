教程第九讲用 Reporter 把终端输出与飞书发送解耦。在 OmicsClaw 中，对应的是 engine 发出事件，由 entry 下不同入口适配；CLI、Desktop 和 Channel 都共用 AgentApp，避免每个入口复制一套执行循环。

CLI 提供 REPL、审批和会话恢复；Desktop 提供 HTTP、SSE、审批与文件服务；Channel 支持七种平台，并要求 owner allowlist。当前后端具备审批和中止端点，独立模块审查由当前请求显式开启，不能因为前一轮批准过审查就长期保持开启。

本仓库负责后端政策、执行、持久化和 HTTP 契约，单独的 OmicsClaw-App 仓库负责 Electron、Next.js 和 SSH 隧道。本次没有重新检查 App 源码或运行端到端客户端，因此后端接口核对不等于本次客户端体验验收。