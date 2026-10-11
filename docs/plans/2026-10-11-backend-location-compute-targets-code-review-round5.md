# Backend location / compute targets 第五轮独立代码审查

日期：2026-10-11
审查范围：当前 OmicsClaw 后端与 OmicsClaw-App 工作树
规范基线：`docs/plans/2026-10-11-backend-location-compute-targets.md`、后端 `CLAUDE.md`、App `AGENTS.md`/`SPEC.md`

## 结论

**NEEDS_REVISION**。本轮发布阻塞已明显收敛，但 C2/C3 的 target snapshot 仍未成为唯一执行来源，且真实 Backend/SSH/SFTP 验收尚未完成。

## 本轮确认完成

- RemoteExecutor 的 tree/stat/read 已改为只读 SFTP batch，health 与 surfaces 标记为 `sftp-readonly`。
- SFTP 读取在发起 `get` 前执行 stat；超过 8 MiB 的文件直接拒绝，避免 Range 请求整文件下载。
- aliases/tree/serve capability gate、非 2xx health 透传、路径边界、symlink 拒绝和 tree 截断测试通过。
- App profile migration 支持 profile scope、dry-run/apply、真实数据库 WAL/SHM 备份和失败状态；远端 runtime activation 在提交前调用 startup migration runner。
- Backend workspace backup failure 返回结构化 failed report/sidecar。
- 普通 onboarding 不再提供远端全栈选择；旧 `connection_mode=remote` 不再自动进入 `RemoteServerStep`；Runtimes 文案已重定位为 Backend 运行位置，并指向 Remote Hosts 计算目标。

## 剩余发布阻断

### 1. C2/C3 target snapshot 未完全驱动执行

`JobsManager.create_job` 目前只从输入可选字段写入 `target_workdir`。`RemotePlaneBinding.submit` 在 probe 后重新生成 scratch workdir，只写入 `remote_jobs`，没有将最终 workdir 回写或统一写入本地 `jobs` 快照。RemoteJobsBridge 主要使用 `target_alias`，C3 仍保留 `record.inputs.location` fallback，RemoteKernelRuntime 也没有使用快照 workdir。

需要由同一个 target resolver 在创建阶段产出最终 alias、workdir、capabilities，并同时写入两张 job 表；dispatch、恢复、审批和重启路径只读取不可变快照。必须补充目标变更、Backend 重启恢复、C2 scratch、C3 kernel 和两 store 一致性测试。

### 2. 真实环境验收未完成

尚未在真实 FastAPI/uvicorn、SSH/SFTP 目标上验证认证、错误码、Range、8 MiB 限制、权限、symlink、非 GNU 目标和大目录截断，也未完成 C1/C2/C3 任务、双计算目标隔离、重启恢复及跨 profile key/provider/session E2E。当前定向测试通过不能替代这些发布前验收。

## 当前验证记录

- 后端定向套件：**302 passed, 13 skipped**。
- App 相关单测：**43 passed**；TypeScript typecheck 通过；目标 ESLint 无错误（`OnboardingShell.tsx` 保留既有 max-lines 警告）。
- 后端 `compileall` 与两仓库 `git diff --check` 通过。

在完成上述两个阻断并执行真实环境验收前，不提升为 PASS。
