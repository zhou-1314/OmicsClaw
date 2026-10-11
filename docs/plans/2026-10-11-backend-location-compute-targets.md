# Backend 运行位置与远程计算目标收敛计划

状态：待独立审核。本文是跨 OmicsClaw 后端与 OmicsClaw-App 的实施计划；实现前以当前代码符号和 `docs/core-features/surfaces.md` §8 重新核对路径。

## 1. 目标与不变量

1. App 在任一时刻只有一个活动 Backend。
2. Backend 负责会话、模型供应商、聊天流、审批、Desktop 契约和计算目标编排。
3. 一个活动 Backend 可以关联零个、一个或多个远程计算目标。
4. 计算目标表示分析任务的执行位置，不再被当作第二个 Backend。
5. `connection_profiles` 保存 Backend 连接身份；`remote_hosts` 保存计算目标。
6. 两类记录使用独立主键、生命周期和权限校验。删除目标不删除 Backend，删除 Backend 连接也不隐式删除目标。
7. 所有聊天、审批、停止和 SSE 请求绑定到活动 Backend。计算任务另外绑定目标快照。
8. Key、供应商配置、installation identity 和 session authority 跟随活动 Backend；计算目标不持有 App 全局 API Key。
9. 远端全栈 Backend 逐步退出主路径，保留明确标注的“自托管 Backend”高级入口。
10. 不为这次收敛新增无实际需求的 feature flag、长期双栈或包装式兼容层。已有公开契约和已有用户数据需要迁移的部分，采用一次性迁移和明确版本变更。

## 2. 现有能力边界

### 2.1 C1、C2、C3 的代码事实

- C1 是 `omicsclaw/remote/plane.py` 提供的远程工具平面，包括探测、知识库和远程执行工具。
- C2 是 `omicsclaw/remote/bridge.py` 与 `omicsclaw/entry/desktop/jobs.py` 的远程作业路由，当前通过 `runtime="remote:<alias>"` 进入 `/jobs`。
- C3 是 `omicsclaw/remote/kernel.py` 的远端持久内核，包含现有环境缓存和内核生命周期。
- 这三个名称表示能力层，不表示 Backend 类型，也不能映射成“本地 C1、SSH C2、内核 C3”。一个计算目标可以同时提供 C1、C2、C3 能力。
- C3 保持当前形态，不增加新的 HTTP 或 agent 面。
- 目标快照记录目标 ID、能力集合、workdir 和任务所需的 C1/C2/C3 能力。目标配置后来发生变化时，运行中的任务仍使用提交时的快照。
- 目标不可用时返回结构化错误，不自动切换另一个目标；用户重新选择目标后重试。

### 2.2 Backend 与计算目标职责

- Backend 负责聊天上下文、模型调用、工具审批、事件流、目标发现和任务派发。
- 计算目标负责远程主机、工作目录和 SFTP 数据通道。
- App 只向活动 Backend 发起 Desktop v3 请求，不直接把聊天或 bearer 发给计算主机。
- Backend 不可用时不从计算目标推导另一个 Backend；计算目标不可用时不把 Backend 标记为不可用。

## 3. 数据模型与迁移

### 3.1 Backend 连接

盘点并整理现有 `connection_profiles`。每条记录表示一个 Backend，包含显示名、连接类型、URL 或隧道元数据、凭据引用、SSH 信息、健康状态和时间戳。不要在该表增加计算工作目录、SFTP 根目录或作业队列字段。

`active_backend_id` 是 App 本地设置中的唯一活动 profile 指针，不是 Backend 数据库的外键。缺失时遵循当前本地默认初始化规则，指向已删除记录时在 App 启动和请求阶段 fail-closed。旧 Stage-0 URL/token 只能作为一次性迁移输入，不继续作为运行时 fallback。

### 3.2 计算目标

`remote_hosts` 属于当前 Backend 的 workspace，由后端的 `remote_hosts` store 管理。它至少保留现有 alias、probe、notes 和时间字段，并按实际 C1/C2/C3 需要增加 workdir、SFTP 根目录、能力集合和审批策略。它不保存 App 的 profile ID，也不把 App 的 `ssh_alias` 或 `ssh_host` 自动解释成计算目标。

- 当前 App profile 通过代理连接哪个 Backend，由 App 决定；Backend 只管理自己 workspace 中的计算目标。
- 凭据保存引用，不把私钥、密码或 bearer token 写入普通业务表。
- workdir 必须在后端认可的根路径内，由服务器重新规范化。
- capabilities 和 approval policy 使用后端认可的枚举。
- 只有活动 Backend 返回的 host store 才能供当前聊天选择；切换 Backend 后重新读取目标列表。

### 3.3 runtime 与 jobs

先盘点 App SQLite 中的 `connection_profiles`、活动 profile 设置，以及 Backend workspace 中的 `remote_hosts`、`remote_jobs` 和 jobs runtime 字段。两套数据库由不同进程和仓库所有，不能使用跨库外键或一个事务。

- App 迁移只负责 profile 字段、活动 profile 和 tunnel 配置；Backend 迁移只负责自己的 host store、remote jobs 和任务字段。
- 现有 profile 的 `ssh_alias`、`ssh_host` 描述 Backend 连接，不自动拆成计算目标。无法从现有数据确定目标时生成 unresolved 报告，由用户在 Remote Hosts 页面重新探测。
- 已有 `runtime="remote:<alias>"`、`RemoteJobsBridge`、`remote_jobs` 和 jobs runtime 字段在高级自托管 Backend 可用期间继续保留。新目标契约没有覆盖其全部调用者前，不删除或重命名它们。
- 新任务可以增加目标快照字段，但必须继续满足现有 C2 bridge 和 C3 kernel 的调用约定。历史任务保留 ID、状态、输出尾部和时间戳，不重新派发。
- 两边分别使用可重复的迁移标记、备份和 unresolved 清单。先完成 App 迁移，再对每个 Backend workspace 执行后端迁移。

迁移前分别备份 App SQLite 和每个 Backend workspace 的 `.omicsclaw/remote.db`。任一迁移失败时只回滚对应数据库和配置，不尝试恢复已经在远端运行的任务。不要以长期兼容层掩盖迁移失败。

## 4. 后端实施

### 4.1 Desktop v3 契约

实现前阅读后端 `CLAUDE.md`、`SPEC.md`、`CHANGELOG.md` 与 `docs/core-features/surfaces.md` §8，以 `omicsclaw/entry/desktop/wire_contract.py` 的 `SERVED_PATHS` 为真实路由清单。

- 现有请求与 SSE 继续使用契约 v3。
- Backend profile CRUD 和活动 profile 属于 App 本地职责，不加入 Backend `SERVED_PATHS`。Backend 只提供计算目标和执行能力。
- 新的计算目标、别名建议、探测和只读远端文件能力必须同步更新 `SERVED_PATHS`、契约文档、schema 和契约测试。
- 若增加目标字段可以保持 v3 语义，则按 v3 的明确变更完成；若字段删除或含义破坏性改变，则先提升契约版本并同步 App，不能偷偷接受旧版本并转换。
- 不恢复已退役的旧页面路由，也不为旧页面新建代理。

### 4.2 服务边界

在 `omicsclaw/entry/desktop/` 和 `omicsclaw/remote/` 的现有抽象内整理 remote host store、compute target resolver、目标能力检查器、工作目录和 SFTP 路径检查器。Backend 不建立 App profile repository，也不读取 App SQLite。沿用现有持久化和依赖注入方式，不引入新的数据库框架，也不在路由中散落 SQL。

健康检查分别报告 Backend 本身和目标状态。活动 Backend 选择后，单次请求固定 URL、凭据和 profile ID。连接解析沿用现有优先级，解析失败不得读取凭据，未解析目标不得建立 SFTP 连接。

### 4.3 路由和任务派发

实现前先在 `wire_contract.py` 和 `surfaces.md` §8 中固定以下路由的请求、响应、认证、能力声明、错误码和测试。它们属于 Desktop HTTP 管理契约，沿用当前 wire contract v3 的版本声明，但不属于聊天请求或 SSE 事件；每个路由的 schema 单独记录，不能用“chat v3”笼统代替。

| 所有者 | 路由 | 用途 |
|---|---|---|
| App 本地 | Electron/Next 的 profile API | profile CRUD、活动 profile、隧道生命周期，不经过 Backend |
| Backend 现有 | `GET/POST/DELETE /remote/hosts` | host store 列表、探测、删除，沿用 `remote_execution` 能力门控 |
| Backend 新增 | `GET /remote/hosts/aliases` | 无 body；返回 `{aliases: string[]}`，只返回具体 alias；401 为认证失败，503 为 `remote_execution_disabled` |
| Backend 新增 | `GET /remote/hosts/{alias}/files/tree?path=<relative>&depth=<n>` | 返回 `{alias, root, entries:[{path,name,type,size,mtime}]}`；path 只接受 workdir 相对路径，超出根目录返回 409 `path_outside_workdir` |
| Backend 新增 | `GET /remote/hosts/{alias}/files/serve?path=<relative>` | 只读字节响应；文本、图片和 PDF 预览上限 8 MiB，Range 超过上限返回 416 `range_not_satisfiable`，文件过大返回 413 `preview_too_large` |
| Backend 现有 | `/jobs*` 与 C2 bridge | 高级自托管 Backend 继续使用 `runtime="remote:<alias>"`，直到另行完成替换 |

上述新增路由都要求当前 Backend 的认证头，并在 `/health` 的 `capabilities.remote_execution` 开启时提供；文件路由另外返回 `capabilities.remote_execution_files`，表示只读 SFTP 路由是否可用。未知 alias 返回 404 `remote_host_not_found`，无效 alias/path 返回 422，SSH 不可达返回 400 `unreachable`。App 代理逐字透传状态、错误码和响应头，并为每条路由增加契约测试。

当前 host store 的 alias 是稳定目标键。新建任务时生成目标快照，至少包含 `target_alias`、能力集合、workdir 和 C1/C2/C3 使用情况。C2 请求继续发送 `runtime="remote:<target_alias>"`，并在 jobs/remote_jobs 的新增快照字段中保存 `target_alias`、`target_workdir` 和能力集合；App 不另造一个无法映射到 alias 的 profile ID。C1 通过 `remote/plane.py` 使用相同 alias，C3 通过 `remote/kernel.py` 使用相同 alias，已有 C2 bridge 和 C3 kernel 的入口字段保持不变。

工作目录只统一任务数据目录，不改变 C3 的环境和会话目录。继续使用当前 C1 的 `<scratch>/omicsclaw/<stamp>-<rand>` 作为新任务数据目录格式；C2 复用同一 helper，C3 只把该目录作为数据和产物目录。C3 的 `~/.omicsclaw/kernel-env` 与 `~/.omicsclaw/kernel-sessions` 保持原样。

目标能力不足、SFTP 失败和路径不合法都返回稳定错误码并关联 Backend、目标和 job。后端不自动切换目标。审批卡片显示 Backend、目标、工作目录和操作类型。`ls`、`cat`、`head` 等只读操作可以纳入低摩擦审批策略；写入、删除、重命名和写命令继续要求明确能力检查和审批。高级自托管 Backend 仍必须能走现有 C2 bridge 和 C3 kernel。

## 5. App 实施

### 5.1 Electron 与连接状态

在 `electron/` 中整理 Backend manager、connection profile store、SSH tunnel 和启动生命周期，使主进程只管理一个活动 Backend。切换 Backend 时冻结当前请求、停止旧活动连接并验证新 Backend `/health`，不自动删除 `remote_hosts`。

Key、provider state、installation ID 和 session authority 跟随活动 Backend。这里的“本机”指活动 Backend 所在机器；如果用户选择远端工作站作为 Backend，Key 位于该工作站，计算目标不会收到 App 全局 Key。失效 profile 不得使用缓存凭据，计算主机不得直接接收 App bearer。隧道代码保留，作为自托管 Backend 高级入口的部署方式。

### 5.2 Next API 与客户端

盘点 `src/app/api/` 的 Backend 代理路由，统一通过活动 Backend resolver 获取目标。聊天、审批、Stop、health、providers、workspace 和 files 代理都绑定活动 Backend。新增的计算目标 API 必须对应后端 `SERVED_PATHS`，App 不伪造后端能力。

Remote Hosts 的别名补全应由活动 Backend 提供。这样本地 Backend 使用本机 SSH 配置时与探测一致，远端自托管 Backend 则使用其所在机器的配置。通配符模式不能作为可探测候选。目标文件浏览保持只读，SFTP 请求仍通过 Backend 权限和 workdir 检查。

### 5.3 导航、文案和 onboarding

- `Runtimes` 的可见文案改为“Backend 运行位置”。
- `Remote Hosts` 改为“计算目标”或“远端计算目标”，保留独立页面和独立数据表。
- 页面显示当前活动 Backend，以及“由哪台 Backend 执行探测”的上下文。
- onboarding 主流程先配置一个 Backend，再添加计算目标；远端全栈 profile 不再作为普通执行位置展示。
- 在设置的高级区域保留“连接自托管 Backend”，说明部署、版本和可达性由用户负责。
- 继续保留 C3 当前入口和行为。
- 同步 `src/i18n/en.ts`、`src/i18n/zh.ts`、README、远程文档和 `docs/PROJECT_MEMORY.md`，删除会把远端全栈和计算目标混称的文案。

## 6. 远端全栈的退出顺序

1. 先冻结新功能投资，保留现有 connection profile、tunnel、`remote_execution`、C2 bridge、`remote_jobs` 和 C3 kernel。
2. 先改变用户可见语义和 onboarding，使计算目标成为远程计算的主入口。
3. 新的 C1/C2/C3 目标入口先与高级自托管 Backend 并存，不能让新 UI 假设 Backend 一定是本地的。
4. 迁移只处理真实存在且能确定映射的 App profile 或 Backend workspace 记录；不从隧道字段推断计算目标。
5. `RemoteJobsBridge`、`remote_jobs` 和 `runtime` 字段不在本计划中删除。只有在后续计划完成目标快照契约、覆盖所有调用者并通过符号级依赖检查后，才可单独提出清理。
6. 保留 `/runtimes` 作为高级 Backend 部署页面，并继续支持现有用户的 profile/tunnel 连接。
7. 计划 0066 标记为维护模式，后续新增需求只进入 Backend 部署或计算目标对应的边界。

## 7. 分阶段交付

### 阶段 0：事实锁定

读取两仓库根文档、契约文档和计划 0066，建立文件、路由、表、字段、文案和测试写集。确认 C1/C2/C3 的真实定义、v3 请求/SSE 字段和迁移样本。阶段结束后冻结错误码和数据映射。

### 阶段 1：两边的数据与内部服务

先分别完成 App profile 状态盘点和 Backend workspace host store 盘点。App 只迁移自己的 profile/active-profile 数据，Backend 只迁移自己的 `remote_hosts`、`remote_jobs` 和 jobs 字段；两边都写迁移版本和 unresolved 报告。此阶段不改 onboarding。运行后端迁移、存储、权限和目标单元测试。

### 阶段 2：后端 Desktop v3 路由

增加 target、alias、probe 和只读文件所需的 Backend 路由，更新 `SERVED_PATHS`、`surfaces.md` 和契约测试。profile CRUD 仍由 App 本地 API 负责。保持聊天、SSE、审批和 Stop 语义；保留 `/jobs*`、C2 bridge、`remote_jobs` 和 C3 kernel。确认后端路由和真实后端冒烟通过后，App 才依赖新能力。

### 阶段 3：App 状态和代理

更新 Electron 活动 Backend 生命周期、Next API resolver、jobs/runtime 读取写入、计算目标客户端和目标快照展示。验证 `npm run dev` 和 `npm run electron:dev` 两条路径。

### 阶段 4：UI 与 onboarding

把 Backend 配置放入主 onboarding，把计算目标设为后续可选步骤，隐藏远端全栈主入口，保留高级自托管入口，更新中英文文案和页面状态。覆盖新用户、已有用户、跳过配置和恢复配置。

### 阶段 5：C1/C2/C3 体验补齐

增加只读 SFTP 文件树和预览；统一 C1 作业与 C3 内核的远端 workdir 约定，并用后端契约测试验证；落实只读命令低摩擦审批和写操作审批。C3 保持当前形态，不增加新的 HTTP 或 agent 层。

### 阶段 6：迁移、灰度与清理

分别在 App SQLite 副本和 Backend workspace `.omicsclaw/remote.db` 副本上执行迁移，导入旧 runtime/jobs 样本，运行 C1/C2/C3 和 v3 契约验收，再对内部用户灰度。确认迁移、目标失败、SFTP 和审批日志正常后，只删除已确认未引用的 UI 主路径和文案；不删除 `RemoteJobsBridge`、`remote_jobs`、`runtime` 或高级 Backend 路由。

App 的单一 SQLite 中为每个 profile 保存独立的 `migration_state`，Backend 的每个 workspace 在自己的 `.omicsclaw/remote.db` 保存独立状态。状态包含 `version`、`status`（`pending`、`complete`、`unresolved`、`failed`）、开始/完成时间、错误码和 unresolved 条目数。Backend 迁移工具固定为 `python -m omicsclaw.entry.desktop.migrate_remote_targets --workspace <path> --dry-run|--apply`，App 迁移沿用启动迁移 runner，并提供等价的 `--profile-id <id> --dry-run|--apply` 调用入口。工具必须幂等，`--dry-run` 不写库，`--apply` 每次只处理未完成条目。

某个 workspace 迁移失败时，App 保持该 profile 可连接，继续提供现有聊天、C2 bridge 和 C3；新计算目标管理显示迁移状态与 unresolved 报告，等待该 workspace 重试。内部灰度按真实 profile/workspace 发布批次执行，不增加运行时 feature flag。降级只能先恢复对应数据库备份，再使用上一版本 App/Backend；不能让旧版本直接读取已变更但未回滚的数据库。

## 8. 测试与验收

后端覆盖：`remote_hosts` CRUD、单 workspace host store、多目标、删除隔离、失效 Backend fail-closed、SSH 解析优先级、凭据引用、alias 路由、目标文件树/预览、v3 schema/SSE、health、C1 `remote/plane.py`、C2 `remote/bridge.py` 和 `/jobs`、C3 `remote/kernel.py`、目标快照、无自动切换、SFTP 根路径、workdir、只读和写入审批、历史 jobs/runtime、迁移重复执行与回滚。目标测试至少覆盖 `tests/remote/test_desktop_remote_hosts.py`、`tests/remote/test_server_remote_binding.py`、`tests/remote/test_bridge.py`、`tests/remote/test_store.py`、`tests/entry/test_desktop_wire_contract.py`、`tests/entry/test_desktop_http.py`、`tests/entry/test_desktop_jobs.py`、`tests/entry/test_desktop_files.py` 和 `tests/entry/test_approval.py`，具体命令由阶段 0 根据当前 import 和 fixture 冻结。

App 覆盖：`npm run typecheck`、`npm run lint`、活动 Backend 切换、目标列表与选择、chat/approval/Stop/SSE 绑定、历史 jobs、迁移启动、onboarding 跳过与恢复、高级自托管入口、双语文案、browser-only、Electron、远端只读文件和审批卡片。目标单测覆盖 `src/__tests__/unit/remote-hosts-route.test.ts`、`src/__tests__/unit/runtimes-api.test.ts`、`src/__tests__/unit/runtime-activation-route.test.ts`、`src/__tests__/unit/onboarding-remote-server-step.test.tsx`、`src/__tests__/unit/settings-remote-execution.test.tsx`、`src/__tests__/unit/jobs-routes-remote.test.ts`、`src/__tests__/unit/file-tree-remote.test.tsx` 和 `src/__tests__/unit/files-serve-remote.test.ts`。

端到端至少验证：一个 Backend 添加两个目标；分别运行 C1、C2、C3；运行中修改目标不影响已有任务；目标断开不影响聊天；删除目标不影响另一个目标和历史任务；切换 Backend 时 Key、provider、session 和请求目标一起切换；失效 profile 不回退旧 URL 或 localhost；高级自托管入口仍能连接既有 Backend；迁移后历史记录可读。

## 9. 发布、回滚和完成标准

这是 Desktop 契约、权限和跨仓库数据迁移变更，按后端 SPEC 的最高验证等级执行。后端契约和各 workspace 迁移工具先发布，App 在新路由可用后再开放新 UI。App SQLite 为每个 profile 保存迁移版本标记，Backend workspace 各自保存备份文件、unresolved 报告和失败日志。任何 v3 契约失败、密钥错绑、目标越权或路径逃逸都停止灰度；恢复对应数据库备份后，再使用上一版本 App 与后端组合回滚。旧版本不能直接读取已变更但未回滚的数据库。回滚不恢复已经运行的远端任务，也不重新引入远端全栈主 onboarding。

完成时应满足：只有一个活动 Backend；一个 Backend 可管理 N 个计算目标；`connection_profiles` 与 `remote_hosts` 分离；C1、C2、C3 可用且 C3 形态未变；远端全栈退出主路径但高级自托管入口仍可用；所有聊天、SSE、审批和 Stop 请求走活动 Backend；Key 与 provider 跟随活动 Backend；SFTP、只读审批和 workdir 约束由后端执行；历史 runtime/jobs 可读；迁移可重复、可回滚；v3 文档、App 双语文案、README、项目记忆和发布说明同步；没有新增需求之外的 feature flag、长期双栈或兼容层。

## 10. 明确不做

- 不拆除 SSH 隧道、connection profile、`remote_execution`、C2 bridge、`remote_jobs` 或 `runtime` 字段，它们服务于高级 Backend 路径和已有用户。
- 不重启或扩展 R1/R2 远端全栈供给能力。
- 不在远端建立 artifact 或 memory 副本。
- 不把 C3 改成第二个 Backend。
- 不让计算目标承载聊天、模型供应商、会话或审批数据库。
- 不引入自动故障转移、隐式目标切换或额外的长期兼容层。
