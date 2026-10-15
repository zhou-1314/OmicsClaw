# 执行沙箱（Native OS Sandbox v3.0）

`omicsclaw/sandbox/` 与 `omicsclaw/entry/sandbox.py` 为 OmicsClaw 的本地执行平面（`bash` 工具、`python`/`r` Jupyter Kernel 会话、`skillenv` 依赖探测与安装）提供**零守护进程、跨平台原生 OS 沙箱**与**生信白名单网络出站代理（Egress Proxy）**。

设计基准见原生沙箱重构方案（v3.0）：完全移除旧版对重型 Docker / Podman daemon 与预构建镜像的依赖，直接使用宿主机已有的 Conda / `uv` / R 生信环境，以毫秒级启动开销实现文件系统写边界隔离、敏感凭据屏蔽、进程树强回收与防数据外泄网络过滤。

---

## 1. 架构总览与跨平台后端矩阵

OmicsClaw 在启动时通过 `probe_sandbox_backend()` 自动探测当前操作系统的原生隔离原语，并计算有效安全等级（`Level`）：

| 平台 | 首选后端 (`BackendName`) | 备选后端 | 文件系统隔离 | 网络隔离与过滤 | 进程组与孤儿回收 | 有效等级 (`Level`) |
|---|---|---|---|---|---|---|
| **Linux** | `bwrap` (`bubblewrap`) | — | `--ro-bind / /`，仅放行 `<workspace>` 与 `.omicsclaw/sandbox/` 可写；遮蔽 `~/.ssh`、`.env`、`.git/hooks` 等 | `--unshare-net` + UNIX socket 桥接宿主机 `EgressProxy` | `--unshare-pid --die-with-parent` + `_bridge.py` 信号转发 | `isolated` |
| **Linux（无 bwrap）** | — | `netns` (`unshare`) | 无 OS 级挂载隔离（回退至路径守卫 + 环境变量凭据清洗） | `unshare --user --map-root-user --net --pid --fork` + `EgressProxy` | `--kill-child=SIGKILL` + 进程组回收 | `net_only` |
| **macOS** | `seatbelt` (`sandbox-exec`) | — | SBPL `(deny file-write*)` 默认拒绝写，精细放行 `<workspace>` 与 `.omicsclaw/sandbox/`；`(deny file-read*)` 遮蔽敏感目录与 `.env` | SBPL `(deny network-outbound)`，仅放行 `remote ip "localhost:<proxy_port>"` 与本地 IPC | `start_new_session=True` + `os.killpg` | `isolated` |
| **Windows 10/11** (`>=17763`) | `appcontainer` (Win32 LowBox Token) | — | LowBox Token 默认无权读写用户目录；在 `<workspace>` 显式授予 `(OI)(CI)(M)` 并对 `.env`、`.git/hooks`、`.omicsclaw` 施加 `/deny` ACE（优先于父目录继承） | 不授予 `internetClient` capability（内核级拦截公网直连），仅授予 `privateNetworkClientServer` 连接宿主机 `127.0.0.1:<proxy_port>` | `CREATE_SUSPENDED` + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` + `taskkill /F /T` | `isolated` |
| **降级模式** | `none` | — | 无 OS 隔离 | 无网络隔离 | 进程组 / `taskkill` | `degraded` |

---

## 2. 全执行面覆盖（`bash` + `python`/`r` Kernel + `skillenv`）

旧版沙箱仅包裹 `bash` 工具，而智能体常用的 `notebook_run_cell`（`python`/`r` Kernel）与 `install_skill_deps`（`skillenv`）直接在宿主机裸跑。v3.0 通过 `SpawnWrapper` 协议统一覆盖全部本地执行面：

1. **`bash` 工具**：`SandboxManager.run_bash(command, cwd, timeout)` 将命令包裹在平台后端（`bwrap` / `unshare` / `sandbox-exec` / `_win_appcontainer_exec`）内执行。
2. **`python` 与 `r` Jupyter Kernel（`SessionKernel`）**：
   - `SandboxManager.spawn_wrapper()` 提供 `KernelSpawnAdapter`，注入 `WorkspaceKernelBinding` 与 `SessionKernel`。
   - **ZMQ 通道穿越**：
     - Linux (`bwrap` / `netns`)：由于 `--unshare-net` 隔离了网络命名空间（含抽象与回环 TCP 端口），`SessionKernel` 自动切换为 `transport="ipc"`（挂载在共享文件系统的 UNIX Domain Socket，路径长度超過 84 字节时自动回退至短路径临时目录避免 `AF_UNIX` 108 字节截断）。
     - Linux PID 命名空间兼容：在 `unshare --pid` / `bwrap --unshare-pid` 下，沙箱内 `_bridge.py` 为 PID 1，`SessionKernel.start()` 自动清理 `JPY_PARENT_PID` 并以 `independent=True` 启动 `KernelManager`，防止 `ipykernel` 的 `ParentPollerUnix` 因宿主 PID 与命名空间 PID 1 不匹配而误自杀；进程回收由 `--die-with-parent` / `--kill-child=SIGKILL` 与 `SessionKernel._kill_manager()`（递归遍历 `/proc/<pid>/task/*/children`）双重保障。
     - macOS (`seatbelt`) 与 Windows (`appcontainer`)：保持 `transport="tcp"`（127.0.0.1 回环端口），分别通过 SBPL `(allow network* (local ip "localhost:*"))` 与 AppContainer Loopback + `_win_shims.py`（修复 LowBox 下 `os.devnull` 与 `\.\pipe\LOCAL\` 命名管道限制）通信。
3. **`skillenv` 依赖安装与探测**：`OverlayRunner` 通过同一 `SpawnWrapper` 在沙箱内运行 `pip install` 与模块探测，并将 `TMPDIR` / `PIP_CACHE_DIR` 重定向至 `.omicsclaw/sandbox/cache`。
4. **控制面凭据强制清洗（第 0 道防线）**：无论沙箱是否开启，`without_control_credentials()` 都会从 `bash`、`SessionKernel`、`skillenv`、`envsupply` 的子进程环境变量中剥离 20 项 LLM API Key、Channel 密钥、`OMICSCLAW_REMOTE_AUTH_TOKEN` 与 `SSH_AUTH_SOCK`。

---

## 3. 文件系统写边界与工作区防持久化（`profile.py`）

`build_analysis_profile(workspace, ...)` 生成统一的 `SandboxProfile`：

- **可写路径**：
  - `<workspace>`（分析工作区）
  - `<workspace>/.omicsclaw/sandbox/{tmp,home,cache,py_pkgs,r_libs}`（沙箱专用暂存、缓存与包目录）
  - `<workspace>/.omicsclaw/kernel/`（Jupyter 连接文件与 IPC 套接字目录）
- **工作区内只读保护（防沙箱逃逸与权限篡改）**：
  - `<workspace>/.git/hooks`、`<workspace>/.git/config`（防止注入恶意 Git hook 在宿主机触发执行）
  - `<workspace>/.omicsclaw`（保护权限规则 `settings.json` 与记忆库不被篡改；其下的 `sandbox/` 与 `kernel/` 子目录单独重挂载为可写）
  - `<workspace>/.mcp.json`、`<workspace>/.bashrc`、`<workspace>/.zshrc`、`<workspace>/.profile`、`<workspace>/.envrc`
- **敏感凭据遮蔽（禁止读取）**：
  - `<workspace>/.env`、`.env.local`、`.env.production`、`.netrc`、`.npmrc`、`.pypirc`
  - 宿主 `$HOME` 下的 `~/.ssh`、`~/.aws`、`~/.config/gcloud`、`~/.azure`、`~/.kube`、`~/.docker`、`~/.gnupg`、`~/.omicsclaw/auth` 等敏感目录。

---

## 4. 生信白名单出站代理（`EgressProxy`）与防数据外泄

当 `OMICSCLAW_SANDBOX_EGRESS=proxy`（默认）时，宿主机在 `127.0.0.1:<随机端口>` 启动纯 Python `asyncio` 实现的 HTTP / HTTPS `CONNECT` 代理 `EgressProxy`，并向沙箱子进程注入 `HTTP_PROXY` / `HTTPS_PROXY`：

### 4.1 严格五步判定顺序（`evaluate_egress`）
1. **`blocked-by-ip-literal`**：目标 host 为字面量 IP 时，若命中私有网段（RFC1918 `10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16`）、回环（`127.0.0.0/8`、`::1`）、链路本地与云元数据（`169.254.0.0/16`）且未显式列入 `OMICSCLAW_SANDBOX_EGRESS_ENDPOINTS`，立即拒绝。
2. **`blocked-by-resolved-ip`（防 DNS Rebinding）**：由宿主机代理解析目标域名并在建立上游连接时**直接连接已校验的 IP**；若任一解析出的 IP 属于私有/回环/元数据网段（含 `localhost`），且未列入 `egress_endpoints`，立即拒绝。
3. **`blocked-by-denylist`（不可覆盖防外泄黑名单）**：内置拦截 `pastebin.com`、`transfer.sh`、`0x0.st`、`file.io`、`catbox.moe`、`webhook.site`、`*.ngrok.io`、`*.ngrok-free.app`、`*.trycloudflare.com`、`*.pipedream.net`、`*.oastify.com`、`*.burpcollaborator.net` 等公网外泄通道，任何用户配置或运行时授权均不可覆盖。
4. **`allowed`**：匹配当前启用的生信预设组（`EGRESS_PRESET_GROUPS`：PyPI、Conda/Bioconda、CRAN/Bioconductor、GitHub/GitLab、NCBI Entrez/GEO/SRA、Ensembl/UCSC、UniProt/EBI、Zenodo/Figshare）或用户额外授权域名。
5. **`blocked-by-default`**：未命中白名单的域名一律返回 `HTTP/1.1 403 Forbidden (X-OmicsClaw-Sandbox: blocked-by-default)`。

### 4.2 动态网络授权闭环（`request_network_access`）
当沙箱内的命令或代码因访问未授权科研域名被拦截（HTTP 403）时：
- `EgressProxy` 记录拦截事件，`bash` 输出末尾自动追加提示行：`[SANDBOX EGRESS BLOCKED] Access to '<host>' was denied by the sandbox network policy...`
- 智能体可调用内置工具 `request_network_access(domain, reason, scope="session"|"always")`（强制 `ApprovalMode.ASK`，必须经人类审批）：
  - `scope="session"`：当前会话内动态加入 `EgressProxy` 白名单；
  - `scope="always"`：同时持久化至 `<workspace>/.omicsclaw/sandbox_egress_grants.json`。
- 用户也可在 CLI REPL 中直接执行 `/sandbox allow <domain> [--always]` 或 `/sandbox status` 查看与管理。

---

## 5. 自动审批安全不变量（`can_auto_approve`）

`OMICSCLAW_SANDBOX_AUTO_APPROVE=true` 仅在以下两个条件**同时满足**时才会将 `bash` 与 `python` 工具降为 `ApprovalMode.AUTO`：
1. `binding.effective_level == Level.ISOLATED`（文件系统写边界与敏感路径遮蔽已生效）；
2. `binding.effective_egress in (EgressMode.PROXY, EgressMode.NONE)`（网络出站受控）。

若当前处于 `Level.NET_ONLY`（如缺少 `bwrap` 的 Linux 机器）、`Level.DEGRADED` 或 `egress=open`，`can_auto_approve` 恒为 `False`，系统打印 Warning 并保持逐条审批（`ApprovalMode.ASK`）。

---

## 6. 配置与可观测性

### 6.1 环境变量与 CLI 参数

| 环境变量 | CLI 参数 | 默认值 | 说明 |
|---|---|---|---|
| `OMICSCLAW_SANDBOX` | `--sandbox` | `off`（可设 `auto`/`on`/`off`） | 沙箱总开关（旧值 `docker`/`podman` 自动映射为 `auto` 并告警） |
| `OMICSCLAW_SANDBOX_BACKEND` | `--sandbox-backend` | `auto` | `auto` \| `bwrap` \| `netns` \| `seatbelt` \| `appcontainer` |
| `OMICSCLAW_SANDBOX_EGRESS` | `--sandbox-egress` | `proxy` | `proxy` \| `none` \| `open` |
| `OMICSCLAW_SANDBOX_EGRESS_GROUPS` | — | 全部 9 个生信预设组 | 逗号分隔的预设组名 |
| `OMICSCLAW_SANDBOX_EGRESS_ALLOW` | `--sandbox-egress-allow` | 空 | 额外放行的域名列表（逗号分隔） |
| `OMICSCLAW_SANDBOX_EGRESS_ENDPOINTS` | — | 空 | 允许访问的内网私有源 `IP:port`（如 `10.0.1.50:8081`） |
| `OMICSCLAW_SANDBOX_KERNELS` | `--sandbox-kernels` | `true` | 是否将 `python`/`r` Kernel 与 `skillenv` 纳入沙箱 |
| `OMICSCLAW_SANDBOX_REQUIRED` | `--sandbox-required` | `false` | 沙箱不可用时是否拒绝启动 |
| `OMICSCLAW_SANDBOX_MOUNTS` | `--sandbox-mounts` | 空 | 额外只读挂载路径（`os.pathsep` 分隔） |
| `OMICSCLAW_SANDBOX_AUTO_APPROVE` | `--sandbox-auto-approve` | `false` | 满足 `isolated` + 受控网络时自动批准 `bash`/`python` |

### 6.2 Doctor 诊断、CLI `/sandbox` 与 Desktop `SandboxBar`
- **Doctor 检查**：`oc desktop` 的 `/api/v1/doctor` 与桌面端诊断面板包含 `sandbox` 检查项，输出当前后端、安全等级、出站模式及缺失依赖的修复命令（`fix_hint`，如 `sudo apt install bubblewrap`）。
- **CLI `/sandbox` 命令**：支持 `/sandbox status` 查看实时状态与最近拦截记录，`/sandbox allow <domain> [--always]` 授权域名。
- **Desktop `SandboxBar`**：顶部状态栏通过四态徽章（🟢 `Isolated` / 🔵 `Restricted (Net+PID)` / 🟡 `Unisolated (Degraded)` / ⚪ `Sandbox Off`）展示 `SandboxStatus` 快照。

---

## 7. 测试

```bash
/opt/conda/envs/OmicsClaw/bin/pytest tests/sandbox/ tests/entry/test_sandbox.py tests/entry/test_sandbox_config.py -q
```

| 测试文件 | 覆盖内容 |
|---|---|
| `tests/sandbox/test_config.py` | `SandboxConfig`、`SandboxMode`、`BackendName`、`Level`、`EgressMode` 解析与校验 |
| `tests/sandbox/test_egress_proxy.py` | 五步出站判定、防 DNS Rebinding、黑名单不可覆盖、真实 `EgressProxy` HTTP/CONNECT 拦截与放行 |
| `tests/sandbox/test_backends.py` | `bwrap`、`netns`、`seatbelt`、`appcontainer` 参数生成、敏感路径遮蔽、Windows LowBox 补丁与 Job Object |
| `tests/sandbox/test_linux_integration.py` | Linux 真实 `netns`/`bwrap` 进程隔离、网络拦截、进程组超时强杀、真实 Python & R `SessionKernel` 沙箱执行与信号中断 |
| `tests/sandbox/test_sandbox_is_a_leaf_layer.py` | AST 架构守卫：确保 `sandbox` 与 `envsupply` 保持纯叶子层，不反向依赖上层包 |
| `tests/entry/test_sandbox.py` | `open_sandbox`、`can_auto_approve` 不变量、`RequestNetworkAccessTool`、`/sandbox` REPL 与 Doctor 集成 |
