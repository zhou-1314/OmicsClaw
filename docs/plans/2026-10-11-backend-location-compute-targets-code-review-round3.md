# Backend 运行位置与远程计算目标：第三轮独立实现审查

审查对象：OmicsClaw 后端与 OmicsClaw-App 当前工作树中对应批准计划 `2026-10-11-backend-location-compute-targets.md` 的改动。

审查依据：批准计划、两仓库 `CLAUDE.md`、`AGENTS.md`、`SPEC.md`、Desktop v3 `SERVED_PATHS`/`surfaces.md`，以及现有 C1/C2/C3 实现边界。

## 结论：NEEDS_REVISION

本轮确认第一轮提出的异常映射、Range/8 MiB、SSH Include、health capability 精确断言和 App 新代理测试等部分已经补进；C1 `remote/plane.py`、C2 `RemoteJobsBridge`/`remote_jobs`、C3 `remote/kernel.py` 以及高级自托管 Backend 入口仍在。

仍有发布阻断：App 侧按批准计划要求的 profile migration runner、备份和 `migration_state` 没有实现；Backend 迁移工具的 dry-run 在新 workspace 会创建数据库、没有备份和失败状态处理；jobs target snapshot 仍可被普通更新改写，C3 `location=remote:<alias>` 也没有写入同一快照；远端全栈仍然以 onboarding 普通步骤出现，没有真正降级为显式高级入口；新增 FastAPI 路由在当前 `.venv-dev` 因缺少 FastAPI 被跳过，现有新增 HTTP 测试本身还遗漏了先保存 host card 的前置步骤；新文件路由使用远端 shell 的 GNU 命令而非计划要求的只读 SFTP 数据通道，tree 截断标志也有边界误报。

## 已修复或基本修复的项目

1. `omicsclaw/entry/desktop/remote_hosts.py` 已导入 `RemotePathRefused`，树/serve 路由在 `ValueError` 前捕获它，workdir/path 越界不再走 500 或错误的 413。
2. 远端文件 serve 已实现单段 Range、206/416、`Content-Range`、`Accept-Ranges`、无 Range 时的 8 MiB 上限，并在 base64 解码后再次校验大小。
3. 远端 tree/stat/read 命令已使用 `readlink -f`、边界 realpath 和 `[ ! -L ]` 检查，较第一轮只做词法前缀检查安全性明显提高。
4. `list_ssh_aliases()` 已按包含文件父目录解析相对 Include，并带嵌套 Include、去重和循环检测。
5. health 的 `remote_execution_aliases`/`remote_execution_files` 字段、`SERVED_PATHS` 三条新路由、App tree/serve capability gate 以及 aliases gate 已加入。
6. App 新增 `remote-target-files-route.test.ts` 的 4 个目标测试通过；App typecheck 和变更文件 lint 通过。

## 发布阻断

### 1. 跨仓库迁移没有达到批准计划的 dry-run/apply/backup 语义

1. Backend 新增 `migrate_remote_targets.py`，但 `migrate_workspace()` 直接打开 `jobs.db` 和 `remote.db`。
2. `Database` 构造函数会创建目录、数据库文件和 schema，因此对不存在的 workspace 执行 `--dry-run` 会写入 `.omicsclaw/jobs.db` 与 `.omicsclaw/remote.db`；这违反批准计划中“`--dry-run` 不写库”。
3. 实测命令：

   ```text
   python -m omicsclaw.entry.desktop.migrate_remote_targets --workspace <新目录> --dry-run
   ```

   输出 `complete`，但目录中已经出现两个数据库文件。
4. 工具没有在 apply 前创建数据库备份，也没有处理 WAL/SHM 一致性，更没有输出备份路径或恢复步骤。
5. `MigrationReport.error_code` 虽然存在，但实现没有捕获 apply 异常、写入 `status=failed` 或保存失败日志；异常直接向 CLI 冒泡。
6. App 的 `connection_profiles` 迁移完全没有实现：没有每个 profile 的 `migration_state`、profile-id dry-run/apply runner、备份、unresolved 记录或启动迁移入口。
7. 当前后端测试只覆盖单个已有 jobs DB 的 dry-run/apply；没有新 workspace dry-run 不写入、备份产生、WAL 恢复、失败状态和 App profile 迁移测试。
8. 这不是可选清理，而是批准计划的回滚和数据安全前置条件；在实现前不能灰度。

### 2. target snapshot 还不是不可变且没有完整覆盖 C3

1. `JobRecord` 增加了 `target_alias`、`target_workdir`、`capabilities` 并写入 `jobs`，`RemoteJobHandle`/`remote_jobs` 也保存了这些字段，这部分结构已存在。
2. 但 `JobStore.update_job()` 的 `allowed` 列表仍允许更新 `target_alias`、`target_workdir` 和 `capabilities_json`。普通 job 更新路径因此可以改写提交时快照，违反批准计划的 immutable snapshot 不变量。
3. 新建 C2 job 的 capabilities 固定为 `("remote_exec", "remote_submit")`，没有根据任务实际所需的 C1/C2/C3 能力生成；提交的 `target_workdir` 只是从用户 `inputs` 复制，未从已探测目标或后端认可的 workdir 快照确定。
4. C2 bridge 提交后生成的实际 scratch job workdir 写进 `remote_jobs` handle，但本地 `jobs.target_workdir` 仍可能为空或只是输入字段，两条记录的目标快照不一致。
5. C3 的 `code_run` 以 `inputs.location="remote:<alias>"` 进入 `CodeRunRunner`，而 `JobsManager.create_job()` 只有 `runtime="remote:<alias>"` 时才写 target snapshot；C3 远端任务因此没有同一套 `target_alias`/workdir/capabilities 快照。
6. 需要禁止普通 update 改写 snapshot，只允许创建时写入；迁移工具应是唯一的历史回填者，并增加 C1、C2、C3、重启恢复和修改/删除目标后的快照测试。

### 3. 远端全栈仍在 onboarding 普通主路径

1. `RemoteServerStep` 仍展示远程 profile 选择、继续按钮和“添加后端位置”链接；`onboarding.execution.remote` 仍是普通执行位置选项。
2. 新增的 `advancedNote` 只是文案，没有把 self-hosted Backend 移到显式高级入口，也没有阻止主 onboarding 将远端全栈当成普通运行位置。
3. 批准计划要求“远端全栈退出主路径、保留显式自托管 Backend 高级入口”。当前实现只改了标签并新增 Compute Targets 链接，产品行为仍未满足这一不变量。
4. 需要让主 onboarding 先完成唯一 Backend，计算目标成为后续可选步骤；自托管 Backend profile/tunnel 仍可从设置的高级入口进入，并保留 C2/C3。

### 4. Desktop v3 HTTP 契约还缺真实可运行的后端验证

1. `.venv-dev` 没有安装 FastAPI；以下后端命令收集到 37 个测试、2 个 skipped，两个 skipped 正是 `test_desktop_remote_hosts.py` 和 `test_desktop_http.py` 的 FastAPI 依赖：

   ```text
   ./.venv-dev/bin/pytest -q \
     tests/entry/test_migrate_remote_targets.py \
     tests/remote/test_desktop_remote_hosts.py \
     tests/remote/test_store.py \
     tests/entry/test_desktop_wire_contract.py \
     tests/entry/test_desktop_http.py
   ```

2. 其余 backend 目标集（jobs、bridge、store、remote switch）为 41 passed、1 skipped；`compileall` 和两仓库 `git diff --check` 通过。
3. 新增 `test_target_file_routes_enforce_range_and_path_boundary()` 没有先调用 `POST /remote/hosts` 或向 host store 写入 `hpc1`，而 `target_root()` 要求已存在并已探测的 host card；在 FastAPI 可用时，首个 tree 请求会得到 404，不能作为通过的 HTTP 回归测试。
4. 仍缺未授权、disabled、unknown alias、workdir unavailable、root/path symlink、非普通文件、413/416 边界、Content-Disposition 和超大 tree 的 FastAPI 测试。
5. `test_desktop_wire_contract.py` 和 health 精确断言已经同步字段，但没有证明真实路由挂载与响应形状同步。

### 5. 文件能力实现不符合批准计划的只读 SFTP/可移植性要求

1. 批准计划明确新文件树/预览是“只读 SFTP 路由”和 SFTP 数据通道。
2. 当前 `RemoteExecutor.list_tree()`、`stat_file()`、`read_bytes()` 通过 SSH shell 执行 `readlink -f`、`find -printf`、`wc`、`dd | base64`，不是 SFTP 目录列表/文件读取。
3. `find -printf` 是 GNU find 扩展，`readlink -f` 也不是所有目标环境保证提供；当前没有能力探测、可移植回退或稳定的“不支持”错误。
4. 需要明确采用 SFTP 实现，或更新契约并在目标能力探测中拒绝不支持主机；不能在文档写 SFTP、实现却默认为 GNU shell。

### 6. tree 截断语义和远端资源上限仍有边界问题

1. `list_tree()` 对完整 SSH 输出执行 `splitlines()[:REMOTE_TREE_MAX_ENTRIES]`，远端 `find` 没有先限制输出；主机上有大量条目时，Backend 仍会接收和保存在内存中，才在 Python 侧截断。
2. 路由用 `len(entries) >= 2000` 作为 `truncated`，恰好有 2000 项但没有被截断时也会返回 `true`；必须读取第 2001 项或由远端命令显式报告是否还有更多。
3. paths 含 tab/newline 时，`-printf` 的制表分隔解析没有转义协议；需要采用安全的 SFTP/长度前缀或明确拒绝不可编码条目。

### 7. aliases App proxy 的错误状态没有完全遵循契约

1. `src/app/api/remote-hosts/aliases/route.ts` 对 Backend `/health` 的任意非 2xx 都映射成 503 `backend_health_unavailable`。
2. 批准计划要求 App 代理逐字透传状态、错误码和响应头；Backend health 401、409 或其他结构化失败在 aliases 代理中没有保留。
3. 新增 aliases 单测只覆盖 health 200 且 capability true/false，没有覆盖 health 401、invalid body 和 backend route 错误。

## C1/C2/C3 与高级自托管 Backend

1. `omicsclaw/remote/plane.py` 和 `/remote/hosts` 仍提供 C1 远程工具平面。
2. `omicsclaw/remote/bridge.py`、`omicsclaw/entry/desktop/jobs.py`、`remote_jobs` 和 `runtime="remote:<alias>"` 仍提供 C2。
3. `omicsclaw/remote/kernel.py` 的 `RemoteKernelEnv`/`RemoteKernelRuntime` 仍提供 C3；本轮没有删除其环境目录和会话目录。
4. App 的 SSH tunnel、connection profile 和 `/runtimes` 自托管 Backend 页面仍在。
5. 但第 2 项未解决前，不能声称一个目标快照同时覆盖 C1/C2/C3；特别是 C3 `location` 目前没有写进 jobs snapshot。

## 验证记录

已运行并通过：

- `./.venv-dev/bin/pytest -q tests/entry/test_migrate_remote_targets.py tests/remote/test_store.py tests/entry/test_desktop_wire_contract.py`：37 passed（同一批命令中的 2 个 FastAPI 测试 skipped）。
- `./.venv-dev/bin/pytest -q tests/entry/test_desktop_jobs.py tests/remote/test_bridge.py tests/remote/test_store.py tests/remote/test_remote_switch.py`：41 passed、1 skipped。
- `python -m compileall -q omicsclaw/entry/desktop omicsclaw/remote tests/entry tests/remote`：通过。
- App `npm run typecheck`：web 与 Electron 均通过。
- App 目标单测：

  ```text
  npx tsx --import ./src/__tests__/setup/jsdom-setup.ts --test \
    src/__tests__/unit/remote-target-files-route.test.ts
  ```

  4 passed。
- App 新增代理相关文件的 eslint：通过。
- 两仓库 `git diff --check`：通过。

未完成或未验证：

- 后端 FastAPI HTTP 测试未执行，原因是 `.venv-dev` 没有 FastAPI；现有新增 HTTP 测试还有 host card 前置缺陷。
- 真实 SSH/SFTP 未执行；未验证 OpenSSH Include 的真实解析顺序、远端 GNU 命令可用性、symlink/race、权限拒绝、SFTP 文件树和 Range。
- App 完整 `npm run test:unit` 未完成；本轮只运行新增目标单测，完整命令因耗时被中断，不能据此声称全套通过。
- 未运行 browser smoke、真实 Desktop Backend smoke、remote E2E 或 C3 真实内核流程。

## 放行条件

只有在迁移备份和 App profile runner、不可变且覆盖 C3 的 target snapshot、onboarding 高级入口、可运行的 FastAPI HTTP/契约测试、计划要求的 SFTP/可移植性、tree 截断边界和 aliases 错误透传全部修复，并重新完成后端目标测试、App typecheck/lint/目标单测、真实 Backend smoke 和可用的 SSH/SFTP E2E 后，结论才可改为 PASS。
