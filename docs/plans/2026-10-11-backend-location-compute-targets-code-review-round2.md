# Backend 运行位置与远程计算目标：实现审查（第二轮）

审查对象：两仓库当前工作树中与 `2026-10-11-backend-location-compute-targets.md` 对应的改动。

审查范围：后端 Desktop v3 路由、远程文件树/预览、SSH alias 解析、远程 job 快照、App 代理与 capability 声明，以及 C1/C2/C3 现有实现是否仍在。

## 结论：NEEDS_REVISION

第一轮中关于 `RemotePathRefused` 导入/异常顺序、Range/8 MiB 实现和 SSH Include 相对路径的代码缺陷已得到修订；C1、C2、C3 的现有模块也仍在。但本轮仍有两个发布阻断：目标快照只补在 `remote_jobs`/handle，`jobs` 的 `runtime` 仍是唯一来源且没有迁移状态/幂等迁移报告；Desktop v3/App 契约同步不完整，已有 health 精确断言会因新增 capability 直接失败，新路由没有完整 HTTP 测试，aliases 代理也没有按 capability gate。未解决前不能进入发布或灰度。

## 第一轮六项阻断复核

| 项目 | 本轮状态 | 证据与剩余工作 |
|---|---|---|
| 1. `RemotePathRefused` 异常映射 | 基本已修复 | `remote_hosts.py` 已导入异常，树/serve 路由在 `ValueError` 前捕获它，越界路径应返回 409。仍缺树/serve 的 workdir、path 越界和未知 alias HTTP 测试，不能只凭静态顺序放行。 |
| 2. symlink 越界 | 实现方向已修复，验证未完成 | `plane.py` 使用 `readlink -f`、边界 realpath、`[ ! -L ]`、`find -P`，读取前再次检查边界；这比第一轮词法前缀安全。仍没有覆盖 root symlink、目标 symlink、父目录 symlink、竞态和远端命令不可用的测试；需确认实现环境支持 `readlink -f`。 |
| 3. Range 与 8 MiB | 实现已补，测试仍不足 | serve 路由读取 Range，支持 200/206/416、`Content-Range` 和 `Accept-Ranges`；无 Range 时拒绝超过 8 MiB，并在读取后再次检查解码长度。需补 0/1/8 MiB 边界、suffix/open-ended、超界和读取竞态测试，并固定错误码/响应头契约。 |
| 4. 目标快照/迁移 | **未修复，阻断** | `remote_jobs`、`RemoteJobHandle` 增加了 `target_alias`、`target_workdir`、`capabilities`，但 `jobs` 仍仅持久化 `runtime`，`JobRecord.target_alias` 只是动态解析 `runtime` 的 property；没有 Backend/App 两边的 `migration_state`、dry-run/apply、unresolved 报告或重启后快照契约。目标被修改/删除后，历史 jobs 仍可能重新解释当前 runtime。 |
| 5. SSH `Include` | 基本已修复 | `list_ssh_aliases()` 按包含文件父目录展开相对 glob，支持嵌套 Include、去重和循环；新增测试覆盖这些形状。仍需测试绝对 Include、无权限文件、引号/多余空白和真实 OpenSSH 配置顺序。 |
| 6. Desktop 契约/App 同步 | **未修复，阻断** | `SERVED_PATHS`、后端 aliases/tree/serve 路由和 App 新代理已加入，但 `tests/entry/test_desktop_http.py::test_health_advertises_optional_capabilities_without_changing_v3` 仍精确断言旧 capability 字典，而 health 现在无条件多出 `remote_execution_aliases`/`remote_execution_files`，该测试必然失败。没有新路由的完整 FastAPI HTTP 测试；App aliases route 未读取/校验 `remote_execution_aliases` capability，树/serve 虽使用 `remote_execution_files` 但缺代理单测。 |

## C1/C2/C3 保留情况

1. C1 的 `RemotePlaneBinding`、`/remote/hosts` REST 面和 `remote_hosts` store 仍在。
2. C2 的 `RemoteJobsBridge`、`remote_jobs` 表和 `runtime="remote:<alias>"` 路径仍在。
3. C3 的 `remote/kernel.py`、`RemoteKernelEnv` 和 `RemoteKernelRuntime` 未被删除。
4. 当前改动没有证明 C3 环境/会话目录快照与新 target snapshot 的关系；必须在修复第 4 项时加回归测试，确保 C3 仍使用原有目录注入方式。
5. SSH 隧道和显式自托管 Backend 高级入口没有被本轮改动删除，符合保留边界。

## 必须修复的实现问题

### A. 固定目标快照与迁移契约

1. 在本地 `jobs` 记录中持久化独立的 `target_alias`、`target_workdir`、能力集合（以及需要时的 Backend/profile 标识），不能只从 `runtime` 动态推导。
2. 创建 job 时生成不可变快照；更新/删除 remote host 后，运行中和历史 job 必须继续读取快照。
3. 明确 `runtime="remote:<alias>"` 与 `target_alias` 的兼容映射，历史记录只在一次迁移时回填，之后不得每次读取都重新解释。
4. 为 App SQLite 和 Backend workspace `.omicsclaw/remote.db` 分别增加迁移版本/状态；两库不能使用跨库外键或假设一个事务。
5. 提供 dry-run/apply、重复执行不重复写入、unresolved 记录和失败重试语义。
6. 为旧 jobs/runtime、remote_jobs、C3 目录和重启恢复增加单元/集成测试。
7. 迁移失败必须保留原数据库副本、版本标记和恢复顺序；不得以隐式 fallback 继续运行新契约。

### B. 完成 v3 契约与 App 代理同步

1. 在 `docs/core-features/surfaces.md` 明确 aliases/tree/serve 的请求、响应、认证、错误码、Range、8 MiB 上限和 capability 字段。
2. 更新 health 精确断言，或改为只断言已知字段；不能留下必然失败的旧精确字典。
3. 为后端 aliases、tree、serve 增加 FastAPI HTTP 测试：未授权、disabled、unknown alias、invalid path、symlink、Range、过大文件和正常响应。
4. App aliases route 应与其他新能力一致：先读取活动 Backend health，并在 `remote_execution_aliases` 不可用时返回稳定 409/503。
5. 为 App aliases/tree/serve 代理增加单元测试，确认 bearer、活动 Backend、Range 和响应头原样透传。
6. health capability 不应只以 `remote is not None` 粗略宣称文件能力；应与实际路由/探测能力一致，或在文档中明确这是同一能力集合。
7. 确认新路径仍全部经过活动 Backend resolver，不读取本机 SSH 配置或旧 Stage-0 URL。

### C. 远端文件实现的验证与可移植性

1. 为 `readlink -f`、`find -printf` 不可用的主机定义能力探测或明确拒绝错误；不能把 POSIX shell 契约默认为 GNU 扩展。
2. 不要把未探测 scratch 的字面量 `~` 当作绝对根；返回明确的 `workdir_unavailable` 或要求已探测绝对目录。
3. 远端树响应若达到 `REMOTE_TREE_MAX_ENTRIES`，必须在 schema 中说明截断或提供分页字段。
4. serve 响应补齐 `Content-Disposition`/缓存策略，并测试 SVG/HTML 等可执行内容按下载或安全预览处理。
5. 使用真实远端 fixture 或命令替身覆盖 root/path symlink 和文件变化竞态。

## 已运行的检查

1. 后端：`python -m compileall -q omicsclaw/entry/desktop omicsclaw/remote tests/remote tests/entry`，通过；仅证明语法可编译。
2. App：`npm run typecheck`，通过（web 与 Electron 两个 TypeScript 项目均通过）。
3. 两仓库 `git diff --check`，通过。
4. 当前环境未运行 pytest；该环境此前缺少 `pytest` 模块，故不能据此宣称 FastAPI/数据库测试通过。

## 明确未验证项

1. **FastAPI/真实 HTTP：未验证。** 尚未在具备 pytest/FastAPI 测试依赖的环境执行新增 aliases/tree/serve 路由、health capability、Range、错误映射和迁移测试。
2. **真实 SSH：未验证。** 未连接真实 SSH 主机，未验证 OpenSSH `Include` 解析后的 alias 探测、`readlink -f`/`find -printf` 可用性、SFTP/远端文件竞态、权限拒绝或隧道断开恢复。
3. **端到端 App：未验证。** 尚未运行 App 的 lint、浏览器 smoke、真实 Backend remote E2E，也未验证活动 Backend key 随切换和 C1/C2/C3 的完整用户流程。

## 放行条件

只有在 A、B 两组阻断全部修复，并在项目测试环境中通过后端 FastAPI/迁移测试、App typecheck/lint/目标单测、真实 Backend smoke 和可用的远端 SSH E2E 后，结论才可改为 PASS。
