# Backend 运行位置与远程计算目标收敛计划审核

第一轮结论：NEEDS_REVISION。

计划方向正确，但以下问题阻断实现：

1. C1/C2 定义与代码不符，清理步骤会误删核心能力。实际代码中，`omicsclaw/remote/plane.py` 是 C1 远程工具平面，`remote/bridge.py` 是 C2 的 `/jobs` 远程作业路由，`remote/kernel.py` 才是 C3。计划应补充代码事实映射，并保留 `RemoteJobsBridge`、`remote_jobs` 和 `runtime` 字段，直到新的目标快照契约与迁移完成。
2. App 的 `connection_profiles` 与活动 profile 在 App SQLite；Backend 的 `remote_hosts`、`remote_jobs` 在每个 workspace 的 `.omicsclaw/remote.db`。两边不能使用跨库外键或单一事务。App profile 的 `ssh_host` 和 `ssh_alias` 描述 Backend 隧道，不等于计算目标。迁移应按仓库分两阶段执行，带迁移标记和 unresolved 报告。
3. Backend profile CRUD 属于 App 本地职责。Backend 当前已有 `/remote/hosts`、`/jobs*` 和 workspace `/files/*`，没有别名建议或远端 SFTP 文件路由。计划需要列出新增路由的归属、请求响应、错误码、认证、能力声明和测试，并区分 App profile 管理与 Desktop v3 执行契约。
4. 远端 profile 仍是合法的活动 Backend。退出范围应限制在主 onboarding、主导航和文案；保留 profile、tunnel、`remote_execution`、C2 bridge 和 C3。清理只能在目标契约覆盖全部调用者并通过符号级依赖检查后另行提出。Key 位于活动 Backend 所在机器，计算目标不接收 App 全局 Key。
5. 这是 Desktop 契约、权限和跨仓库数据库迁移，按最高验证等级执行。计划需要给出具体测试文件、迁移失败门槛、备份产物、版本标记、恢复顺序和旧版本回滚约束。

本轮审核未修改计划正文、生产代码或现有用户改动。

第二轮结论：NEEDS_REVISION。C1/C2/C3 映射、两仓库 ownership、保留高级 Backend/C2 bridge/C3、验证等级已经补齐，但新增 aliases、远端文件树和预览路由仍缺完整 wire shape；target snapshot 还需固定 alias 到 `runtime="remote:<alias>"` 的关系，并区分 C1 数据目录与 C3 环境/会话目录；跨 workspace 迁移还需明确状态、重试和旧版本降级规则。

第三轮结论：PASS。计划已补充管理契约的路由归属、请求/响应形状、认证、能力声明、错误码、8 MiB 预览和 Range 规则；固定 `target_alias` 到 C2 runtime 的映射，保留 C1/C2/C3 的实际模块和 C3 环境/会话目录；补充 App 单一 SQLite 与 Backend workspace 的迁移状态、dry-run/apply、unresolved、灰度和恢复顺序。实现时应在阶段 0 将 C3 数据目录注入方式和目标快照的 SQL/JSON 形状写入契约测试。
