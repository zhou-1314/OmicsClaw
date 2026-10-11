# Backend location / compute targets 第四轮独立代码审查

日期：2026-10-11
审查范围：OmicsClaw 后端与 OmicsClaw-App 当前工作树（仅审查，不修改实现）
规范基线：`docs/plans/2026-10-11-backend-location-compute-targets.md`、后端 `CLAUDE.md`、App `AGENTS.md`/`SPEC.md`、Desktop v3 surfaces 与已批准的前三轮整改项

## 结论

**NEEDS_REVISION**。当前实现已覆盖单一活动 Backend、多计算目标数据模型、C1/C2/C3 的大部分持久化字段和 Desktop v3 新路由，定向测试也基本通过；但仍有发布前阻断：

1. 只读目标文件 API 明确采用 `posix-shell-readonly`，而批准计划与验收要求是通过 Backend 权限/workdir 检查的只读 SFTP 路由。命名 capability 不能替代传输实现，当前实现也没有针对真实 SSH/SFTP 的可执行验证。
2. App 的 profile migration 只有 HTTP POST、无计划要求的 `--profile-id <id> --dry-run|--apply` 入口，也没有启动迁移 runner；备份失败发生在 `try` 外，无法落入 `failed` 状态。现有单测没有覆盖真实文件备份、失败状态、每 profile 隔离和恢复。
3. Backend apply 的数据库迁移失败可以写入 `failed` 状态，但 workspace 层的备份失败在 `migrate_workspace()` 的迁移状态写入之前直接抛出；因此备份异常没有结构化报告或可重试状态。现有测试只验证成功备份。
4. C3 创建任务时已保存 `target_alias`/能力集合，但 `target_workdir` 只从输入可选字段读取，C2 实际生成的 scratch workdir 不一定回写本地 jobs；C3 执行仍从 `record.inputs.location` 选择目标，快照字段没有成为恢复/执行的唯一来源。没有测试证明修改目标后重启任务仍使用提交时的 alias/workdir/capability。
5. onboarding 仍在普通 `ExecutionLocationStep` 中展示并可选择 remote 全栈 Backend，`RemoteServerStep` 仍是主流程步骤，只是允许跳过并附注“advanced”。计划要求远端全栈退出普通执行位置，普通 onboarding 先配置一个 Backend，再由高级入口配置自托管 Backend。

上述任一项都足以阻止发布，结论保持 NEEDS_REVISION。

## 已确认通过的部分

### Backend 与目标快照

- `connection_profiles`（App）与 Backend workspace 的 `remote_hosts`/`remote_jobs` 仍是两个存储域，Backend 不读取 App SQLite。
- `JobRecord` 和 `RemoteJobHandle` 增加 `target_alias`、`target_workdir`、能力集合；`JobStore.update_job()` 拒绝修改快照字段并返回 `target_snapshot_immutable`。
- C2 的 `runtime="remote:<alias>"`、remote bridge、`remote_jobs` 仍保留；C3 的 `code_run`/persistent kernel 入口仍保留，没有新增第二个 Backend 或新的 C3 HTTP 面。
- C3 远端 code run 会在创建记录时推导 `remote:<alias>` 并保存 `remote_kernel` 能力；C2 保存 `remote_exec`/`remote_submit`。这证明能力分类和记录字段已接上，但还不足以证明运行时使用不可变快照（见阻断 4）。
- `update_job` 的快照不可变保护和现有 remote store 状态更新没有被定向测试充分覆盖，尤其缺少 C3 重启/目标变更场景。

### Backend migration

- `migrate_remote_targets.py` 的 dry-run 对缺失 workspace 不创建 `.omicsclaw`、不创建数据库；本轮实际执行命令验证两个 scope 均 `status=complete`、`db_files=0`。
- apply 对已存在 DB 会创建带时间戳的 `*.target-migration.*.bak`，并复制 `-wal`/`-shm` sidecar；迁移状态包含 `pending`、`complete`/`unresolved` 和 `failed` 字段。
- `migrate_database()` 的 SQL 写入异常会尝试保存 `target_migration_state.status='failed'`、错误类型和 unresolved 计数，重复 apply 对已经补齐的行是幂等的。
- 缺失数据库的 dry-run、成功备份和重复 apply 已有测试；没有测试备份拷贝失败、状态表损坏、apply 中途异常后恢复以及真实备份内容的一致性，workspace 层备份异常仍在 `migrate_workspace()` 的状态管理之外（见阻断 3）。

### Desktop v3/HTTP 与文件树

- `SERVED_PATHS`、`surfaces.md`、health capabilities 和 App backend health 类型已加入 aliases/tree/serve 路由；App 代理使用活动 Backend 的 profile，不自行拼接目标连接。
- tree 返回 `entries`、`has_more`/`truncated`、`max_entries`，后端以 `REMOTE_TREE_MAX_ENTRIES + 1` 判断截断；输出上限和此前 exact-2000 误报问题已修复。
- tree/serve 做了 alias/path/workdir 边界检查，拒绝绝对路径、`..` 片段、越界 realpath、未知 alias 和不可用 workdir；serve 保留 Range、416、413 及 `Content-Range` 语义。
- App target route 单测验证了 aliases capability gate、aliases/tree proxy、Range/header 透传；后端 remote-host fixture 已先建立 host 再调用目标路由，避免 fixture 缺失。
- App aliases proxy 在 health 非 2xx 时统一转换为 `503 backend_health_unavailable`，不会逐字保留后端健康响应的状态/错误体；若契约要求错误状态和错误码原样透传，这仍需补齐专门测试和实现。
- 真实 FastAPI/uvicorn 端到端本轮未执行：`.venv-dev` 没有 `uvicorn`，相关 HTTP 测试被跳过；因此不能把 mock/纯函数代理测试当作真实 v3 HTTP 验收。

### Onboarding 与 App

- App migration dry-run 单测确认不写 `profile_migration_state`，apply 可重复执行且 unresolved 会被记录。
- remote-host aliases/tree/serve 代理单测 4 项通过；onboarding RemoteServerStep 单测 2 项通过；`npm run typecheck`（web + electron）通过；本轮变更文件的 eslint 通过。
- RemoteServerStep 现在可以在没有 profile 时跳过，并提供 remote-host 高级入口；这降低了阻塞性，但没有把远端全栈从普通执行位置删除（见阻断 5）。

## 阻断项详述与修复要求

### 1. SFTP 传输与契约不一致（发布阻断）

批准计划在文件树、预览、workdir 和验收章节明确要求只读 SFTP 数据通道。当前 `RemoteExecutor.list_tree()`、`stat_file()`、`read_bytes()` 实际拼接 `ssh` shell 命令，使用 `readlink -f`、GNU `find -printf`、`wc`、`dd|base64`；health 和 surfaces 主动把能力标成 `remote_execution_files_transport: "posix-shell-readonly"`。Backend 的 `_sftp()` 只服务已有上传/下载批处理，不是新 tree/serve 路由的读取通道。

这不是文案差异：shell 工具集、路径解析和输出转义与 SFTP 的 `realpath/stat/open/read` 语义不同，`find -printf`/`readlink -f` 也不是所有目标系统保证提供的 POSIX 接口。若产品要接受 shell 传输，必须先修改批准计划、v3 契约、能力命名和验收标准；在当前批准规范下，应实现只读 SFTP tree/stat/range read，或在目标 probe 中明确能力探测并对不支持平台返回稳定错误。两种选择都要增加真实 SSH/SFTP fixture 和越界、symlink、Range、超限测试。

### 2. App profile migration runner、备份失败与 per-profile 状态（发布阻断）

`src/lib/profile-migration.ts` 提供函数和 `/api/connections/migrate` POST，但没有批准计划要求的 `--profile-id <id> --dry-run|--apply` 等价调用入口，也没有在启动/连接建立前执行的 migration runner。HTTP body 只有全局 `dry_run`，每次检查所有 profile，无法按活动 Backend 或单 profile 重试。

apply 的 `backupAppDatabase()` 在 `try` 之前执行。SQLite 文件不存在时跳过备份；文件存在但复制失败时直接抛异常，既不返回 `status='failed'`，也不写 `profile_migration_state`，调用方拿不到结构化错误。现有测试传入内存 DB 和一个不存在路径，只验证 dry-run/重复 apply，未覆盖真实 sqlite 文件、WAL/SHM、copy failure、transaction failure、failed 状态或 backup path 内容。

修复要求：增加明确的 profile-id runner（CLI 或与启动 runner 等价的内部入口），按 profile 写入 migration state/unresolved；将备份与 apply 的异常统一转换为带 error_code/backup_path 的失败报告；失败后保留 profile 可连接并能重试；增加真实临时数据库、WAL、备份失败、事务失败和重复执行测试。

### 3. Backend workspace 备份失败未形成失败状态（发布阻断）

`migrate_database()` 内部有失败状态处理，但 `migrate_workspace()` 在调用它之前直接执行 `_backup_database(path)`。`shutil.copy2` 或 sidecar copy 出错时函数会直接抛出，既不会产生 `MigrationReport(status='failed')`，也不会在目标 workspace 的 migration state 中留下错误。这样 rollback/重试依赖未记录的异常，不符合计划“备份文件、unresolved 报告和失败日志”的要求。

修复要求：将备份纳入 per-scope try/except；失败时返回结构化 failed report，写入可用的失败状态/错误码，并保证不打开或半迁移目标 DB；补充只读文件、权限错误、损坏数据库和 sidecar 不可复制的测试。

### 4. 快照字段记录了但未完全驱动执行（发布阻断）

`JobStore` 保护了 alias/workdir/capabilities 字段，但 C3 runner 仍通过 `record.inputs.location` 取目标，C2 创建记录时只从请求中可选的 `target_workdir` 取值；实际 scratch workdir 是 remote plane submit 时生成并保存到 `remote_jobs`，不保证回写到 local `jobs.target_workdir`。因此“创建时快照”可能是空 workdir，重启后执行路径仍来自输入而不是快照列。

修复要求：在创建时由同一个 target resolver 解析 alias、能力和最终 workdir，写入 local jobs 与 remote_jobs；C2/C3 dispatch、恢复和审批均只使用快照，输入里的 location 仅作为创建请求；补充修改 host/workdir 后重启、C3 location、C2 scratch、快照字段不可更新和两个 store 一致性的测试。

### 5. 远端全栈仍在普通 onboarding 主路径（发布阻断）

`ExecutionLocationStep` 仍将 `remote` 与 `local` 并排渲染、可作为普通执行位置选择；`RemoteServerStep` 仍在 7 步 onboarding 中展示 profile 下拉框。虽然可跳过且文案写了 advanced，用户仍可从普通流程进入远端全栈，而计划要求远端全栈退出普通执行位置，保留设置中的显式高级自托管 Backend 入口。

修复要求：普通 onboarding 固定活动 Backend（本地或已选 Backend）并把计算目标作为后续可选项；从 `ExecutionLocationStep` 移除远端全栈卡片；将 profile/tunnel activation 移到设置的高级 self-hosted Backend 页面；保留现有 activateRemoteRuntime、C2 bridge、remote_jobs 和 C3 kernel 的深链接/回归测试。

## C1/C2/C3 与 key 绑定检查

- C1 remote plane、host store、probe、remote file 路由仍存在；新增路由通过活动 Backend 的 plane 解析目标。
- C2 `runtime="remote:<alias>"`、bridge、`remote_jobs`、状态重估和 stop/approval 入口仍存在；没有发现把 C2 重命名为 Backend 或删除历史 runtime 的实现。
- C3 persistent kernel、kernel env/session 目录和 `code_run` 入口仍存在，remote kernel 能力在记录创建时保存；但执行未完全改为读取快照（阻断 4）。
- App/Backend profile 与 host alias 仍分离；key/provider/session 的活动 Backend 绑定未在本轮定向测试中做跨 profile 真实请求验收，需在最终 E2E 中补测。

## 本轮验证记录

### Backend

- `./.venv-dev/bin/pytest -q tests/entry/test_migrate_remote_targets.py tests/remote/test_desktop_remote_hosts.py tests/remote/test_store.py tests/entry/test_desktop_wire_contract.py tests/entry/test_desktop_http.py`：**202 passed, 3 skipped**。
- `./.venv-dev/bin/pytest -q tests/entry/test_desktop_jobs.py tests/remote/test_bridge.py tests/remote/test_store.py tests/remote/test_remote_switch.py`：**55 passed, 10 skipped**；跳过项为 `code_run needs jupyter_client`。
- HTTP 子集 `pytest -q -rs tests/entry/test_desktop_jobs.py tests/remote/test_desktop_remote_hosts.py tests/entry/test_desktop_http.py`：**178 passed, 12 skipped**；2 个 HTTP 测试因 `.venv-dev` 缺少 `uvicorn` 跳过。
- `./.venv-dev/bin/python -m omicsclaw.entry.desktop.migrate_remote_targets --workspace <new-temp-dir> --dry-run`：两个 scope 均 `complete`，没有创建 `.omicsclaw` 或数据库文件。
- `python -m compileall -q omicsclaw/entry/desktop omicsclaw/remote tests/entry tests/remote`：通过。
- `git diff --check`：后端通过。

### App

- `npx tsx --import ./src/__tests__/setup/jsdom-setup.ts --test src/__tests__/unit/profile-migration.test.ts`：**1 passed**。
- 同命令执行 `remote-target-files-route.test.ts`：**4 passed**。
- 同命令执行 `onboarding-remote-server-step.test.tsx`：**2 passed**。
- `npm run typecheck`（web + electron）：通过。
- 针对本轮变更文件执行 `npx eslint ...`：通过。
- App target tests 只覆盖 mock/proxy 层；没有真实 Backend v3/SSH/SFTP、Electron 启动迁移或跨 profile key/provider E2E。

## 未验证项与发布前验收

以下项目不能因本轮定向测试通过而标记完成：

1. 安装 `uvicorn`/FastAPI 依赖后运行真实 Desktop ASGI route、401/403/409/413/416、health capability gate、SERVED_PATHS 和响应头契约。
2. 使用真实 SSH 目标验证 SFTP tree/stat/range read、symlink/realpath、不可达、工作目录缺失、非 GNU 工具和大目录 truncation。
3. 运行 C1、C2、C3 各一条真实任务；修改目标配置、重启 Backend、恢复 job，确认 immutable snapshot 的 alias/workdir/capability 仍一致。
4. 用临时文件数据库验证 App/Backend migration 的 backup 内容、WAL/SHM、失败状态、unresolved 报告、重复执行和 rollback。
5. 验证普通 onboarding 不再出现远端全栈执行位置，高级 self-hosted Backend 深链仍能连接已有 Backend。
6. 验证一个活动 Backend 管理至少两个 compute targets，聊天/SSE/approval/Stop/Key/provider/session 都只走该 Backend，切换 Backend 后不会回退旧 URL 或 localhost。

在上述阻断修复并完成真实 FastAPI/SSH/SFTP 验证前，本轮不能提升为 PASS。
