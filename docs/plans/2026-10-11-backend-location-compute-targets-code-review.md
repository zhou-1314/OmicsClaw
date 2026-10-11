# Backend 运行位置与远程计算目标：实现审查

审查对象：当前工作树中 `2026-10-11-backend-location-compute-targets.md` 对应的后端改动，以及 App 工作树的同步状态。审查基于两仓库的 `CLAUDE.md`、`SPEC.md`、`AGENTS.md`、后端 `SERVED_PATHS`/Desktop v3 实现和现有 C1/C2/C3 代码。

## 结论：NEEDS_REVISION

当前改动保留了 `RemoteJobsBridge`、`remote_jobs`、`runtime="remote:<alias>"` 和 C3 kernel，未删除 SSH 隧道或高级自托管 Backend；这部分符合计划边界。但新增的文件树/预览路由存在可触发的错误处理和路径越权问题，Range/8 MiB 契约没有实现，目标快照没有持久化，SSH `Include` 解析也不符合常见配置语义。Desktop 契约和测试/App 代理尚未同步，因此不能进入发布或灰度。

## 阻断项

1. **文件路由的异常映射当前不可用。** `omicsclaw/entry/desktop/remote_hosts.py` 使用了 `RemotePathRefused`，却没有导入它。树路由在工作目录越界时会在异常匹配阶段抛 `NameError` 并返回 500；serve 路由先捕获其父类 `ValueError`，所以越界会错误地返回 413 `preview_too_large`，而不是 409 `path_outside_workdir`。修订应补齐导入，并把 `RemotePathRefused` 放在 `ValueError` 之前；为树/serve 的 workdir、path 越界和未知 alias 增加 HTTP 测试。

2. **只做了词法前缀检查，符号链接可越出 scratch/workdir。** `_inside_remote_dir()` 只规范化字符串；`find` 把 symlink 列出来，`read_bytes()` 使用 shell 重定向读取路径，会跟随指向 scratch 外部的 symlink。攻击者或共享主机上的任务可借此读取任意可读文件。后端必须在远端以 realpath/`readlink -f`（或等价的、不跟随链接的文件句柄流程）重新确认 root 和目标均在允许目录内，拒绝 symlink、非普通文件和 root symlink，并覆盖 workdir/path symlink 测试。

3. **计划约定的 Range 与 8 MiB 响应契约没有实现。** `/remote/hosts/{alias}/files/serve` 没有读取 `Range`，始终返回 200 全文件，也没有 `Accept-Ranges`、206、416 `range_not_satisfiable` 或 `Content-Range`。`read_bytes()` 先 `wc -c` 再完整 `base64`，竞态下文件可在检查后变大，解码结果没有二次上限，因而不能证明响应始终不超过 8 MiB。应实现受限的远端区间读取/尺寸检查，固定错误码和响应头，并测试 0/1/8 MiB 边界、超限与 Range。

4. **目标快照没有落到持久化契约。** 新增的 `target_alias` 只是从 `runtime`/`host_alias` 派生的 property；`jobs`、`remote_jobs`、`RemoteJobHandle` JSON 和数据库 schema 没有 `target_workdir`、能力集合或 C1/C2/C3 使用快照，也没有计划要求的两边 `migration_state`/迁移命令。重启后只能重新解释当前 runtime，无法保证运行中修改目标配置不影响已有任务。应先冻结快照 JSON/SQL，增加一次性幂等迁移、创建/读取/重启路径以及历史记录和失败状态测试。

5. **SSH `Include` 的相对路径解析错误。** `list_ssh_aliases()` 将 `Include conf.d/*` 直接交给进程当前工作目录的 `glob`，没有相对于包含它的配置文件目录解析；嵌套 include 也继承此错误。因此常见的 `~/.ssh/config` + `Include conf.d/*` 不会出现在补全中。应按每个包含文件的父目录解析相对 glob，保持去重/循环检测和 OpenSSH 的文件顺序，并测试嵌套、相对和绝对 include。

6. **Desktop 契约、测试和 App 尚未同步。** `wire_contract.SERVED_PATHS` 已新增三条路由，但现有 `tests/entry/test_desktop_wire_contract.py::test_served_paths_names_the_routes_the_app_mounts` 仍断言旧 tuple；没有 aliases/tree/serve 的后端路由测试。`docs/core-features/surfaces.md` 没有新增 schema/认证/错误码/Range 定义，App 工作树只有旧的 `/api/remote-hosts` 代理，尚未代理别名、文件树/serve，也没有读取新的文件能力字段，仍有本地 SSH 配置解析路径。发布前必须同步两仓库契约、代理、能力类型和测试，且确认所有 App 请求仍经过活动 Backend。

## 非阻断但应在同一轮处理

- `/health` 新增的 `remote_execution_aliases` 未在计划和 App 类型中定义；应决定删除或记录为正式能力。`remote_execution_files` 当前仅按 plane 是否存在置真，需与实际 SFTP 路由能力保持一致。
- `find -printf` 不是 POSIX `find` 接口，而远端契约只保证 POSIX shell；没有能力探测或可移植回退时，部分主机会无法使用树路由。
- `REMOTE_TREE_MAX_ENTRIES` 截断结果却没有在响应中告知客户端；应在 schema 中标明截断或分页。
- 未探测 host 的 scratch 回退值是字面量 `~`，随后被 `shell_quote()` 包成 `'~'`，远端 shell 不会展开，文件浏览会得到误导性的路径错误。应要求绝对 home/scratch 或返回明确的“未探测”错误。
- serve 响应只设置 MIME、长度和 `nosniff`；应明确 Content-Disposition/缓存策略，并覆盖 HTML/SVG 等类型的预览行为，避免客户端把远端内容当作活动页面。

## 已执行与建议的验证命令

已执行：

```text
python -m compileall -q omicsclaw/entry/desktop omicsclaw/remote tests/remote tests/entry
```

结果：通过；这只能发现语法错误，不能覆盖路由、权限或远端路径语义。

尝试执行的风险匹配测试命令：

```text
python -m pytest -q tests/remote/test_desktop_remote_hosts.py tests/remote/test_plane.py tests/remote/test_bridge.py tests/remote/test_store.py tests/remote/test_server_remote_binding.py tests/entry/test_desktop_wire_contract.py tests/entry/test_desktop_http.py tests/entry/test_desktop_jobs.py tests/entry/test_desktop_files.py
```

当前审查环境的 `/opt/conda/bin/python` 没有安装 `pytest`（`No module named pytest`），因此测试未运行。具备项目测试环境后，必须先修复现有 `SERVED_PATHS` 断言，再执行上述集合，并补充 aliases/Include、symlink 越权、Range/8 MiB、目标快照迁移和未探测 host 的用例；之后按后端 SPEC 的 Desktop 契约最高验证等级运行 App typecheck/lint、真实后端 smoke 和远端 E2E。
