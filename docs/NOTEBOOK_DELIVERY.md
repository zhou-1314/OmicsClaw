# OmicsClaw 交互式分析闭环交付

本轮完成聊天 Agent 与 Notebook 的共享分析上下文，以及带信任边界的 live-app 状态回灌。实现、独立评审、验收测试和固定源码回归均留有证据。交付保存在 miracle 容器：

`/workspace/dataset/private/zhouwg_data/OmicsClaw-deliveries/notebook-closed-loop-20261013`

## 已交付的行为

聊天与 Notebook 通过显式、一对一绑定共享本地 Python 内核。获批的 Agent 调用和用户 Run 进入同一执行记录、所有权租约和 durable stream；拒绝审批不会创建执行。取消覆盖排队、admission、内核启动和执行阶段，并通过执行标识避免中断后续任务。断线只重放事件，不重跑代码。

Notebook 的近期结果与显式发布的 note、selection、view 状态，会在每次主 Agent 模型请求前加入有界的 `Role.USER` 参考数据。服务端生成信任标签与来源；CAS、事件幂等和所属 execution 校验阻止旧状态回滚或跨 Notebook 注入。该注入路径不把参考块直接写入 system、聊天历史或长期记忆，也不授予审批权限。模型后续回复若引用这些数据，仍可能进入现有的历史或记忆流程。状态回执表示已准备加入请求，不表示模型已理解或采用。

UI 支持 Chat ↔ Notebook 导航、编辑、运行、取消、输出/图表、断线恢复、`.ipynb` 代码单元导入导出、状态发布与 revision 回执。运行目标变化时会阻止旧目标的命令。导入不执行保存的代码或输出。

本轮回归还修复了未绑定聊天的 augmentor 兼容问题、快速执行完成后的 admission 竞态、重复取消及取消信号归属、headless 父进程导致图表丢失、durable stream 初始化吞掉存储异常，以及 App standalone 缺依赖和开发提交钩子的测试隔离问题。

## 固定版本与回归

| 验收分组 | 最终结果 |
| --- | --- |
| 后端 fast + scripted eval | 9641 passed，25 skipped，3 xfailed，3 xpassed |
| 同一后端 sandbox 串行分区 | 22 passed |
| 后端科学互补分区 | 258 passed，309 skipped |
| Extended 科学环境 | 1 passed |
| CI 额外单细胞路径 | 163 passed |
| App 全量单测 | 2298 passed，4 skipped，0 failed |
| App Web / Electron 类型检查、生产构建、Electron bundler | 通过 |
| App lint | 0 errors，55 项既有 warnings |
| 旧版与当前 App 的实际 Desktop 代理 | hello / approval / abort 共 6 项通过 |
| 固定产物 + 最终后端的真实浏览器闭环 | 2 passed（10.8 秒），独立 reviewer 核验通过 |
| 修复后的实际 Git 提交钩子 | 2298 passed，4 skipped，0 failed；实际 commit 成功 |

后端有效分区合计 **10085 passed，0 普通失败/错误**；334 skipped、3 xfailed、3 xpassed 单列，没有计入 passed。快速分区和科学互补共同覆盖 `make test-all` 的 `not eval` 全集，另加默认测试路径未包含的 163 项 CI 测试。sandbox 参数中有进程 PID，因而单独串行执行；没有跳过该文件。

334 个跳过中，296 项缺历史 parity/旧 CLI 基线，11 项未配置独立基础解释器；其余是明确的环境、平台、网络或可选依赖条件。3 个预期失败来自既有 CI 已知失败清单（provider 协议、二次 compaction、EMERGENCY/FULL 预期），本轮没有修改或新增这些标记。精确 nodeid 与理由见 `evidence/backend-fast` 和 `evidence/backend-science`。

科学测试按 CI 的依赖分组使用隔离环境，包括 POT 0.9.4、harmonypy 0.0.10 和专用 pertpy CPU 环境；没有修改共享 Conda 包，也未完整重建每个 hosted CI 依赖环境；验收采用已记录的分组依赖与版本。extended 首次默认全路径收集因专用环境缺 Desktop 依赖失败，原日志保留。随后按 CI 精确文件重跑；全集 collect-only 证明 extended 集合只有该 1 项，未漏测。

新增后端文件的 lint/format 通过；修改的生产代码新增行没有 lint 诊断。原文件既有 lint/格式问题未作无关清理。App 旧钩子缺测试隔离，以及 Git 仓库环境变量污染测试临时仓库的失败尝试均保留，最终结果以成功的规范钩子日志为准。

## 独立审核与真实链路证据

共享执行、live-state 信任边界、取消与所有权分别经过独立 reviewer；UI 和 standalone 运行边界另有独立 reviewer。最后补充的 headless 图表修复经 27 项独立测试、stream 初始化修复经 228 项独立测试验证。评审发现的问题已修复并复测。

浏览器验收使用实际 Chat/API、实际持久 Python 内核、实际审批和生产 Web 产物。脚本化 provider 记录实际传入它的请求消息，用于验证 Agent → Notebook → Agent 共享变量、显式状态进入 user-role untrusted 上下文、拒绝执行不落库。核心 API 没有被 mock。它不代表外部商业模型的理解能力或所有模型的提示注入鲁棒性。

## 源码、提交和产物

- 后端本轮代码提交：`54abeaad`；完整已测集成快照：`4fb35640871f2a3d02b88607a700fb3fe44fc578`。
- 后端固定清单：2172 文件，SHA256 `1cf7eb11ea311333aecbe807341f8eed9cee1ce2fcf7642a34e2419922c29bd8`。
- App 功能提交：`46ec54addcc0eeee07782935aaf28f3a03fab298`；钩子提交：`2c671e9998739eb151c74e9b22e4246acf01a860`；最终源码快照：`51418616f258ed1e85c2387a8a16b7c98fdd5e2a`。
- App 产品构建清单：1134 文件，SHA256 `77ea851783dbc0ee5094068c0c8489b803bf8cf1e1b6a1a54dc236ca04d75636`。最终开发源码仅增加钩子修复，生产代码字节不变；最终清单 SHA256 为 `648f1bc383003ef86ab89908ca3b7d70689b2d4509c2d42eb6f3a1266b86dd58`，见 `sources/`。
- 后端 wheel：`artifacts/omicsclaw-0.1.2-py3-none-any.whl`，SHA256 `f8e5c1d76a86b486bf4a5a9d887974eb4e6faf4cf8ec46424fa707b83135858b`。304 个 Python 模块逐文件与已测源码一致，独立安装目录的绑定/状态/上下文 smoke 通过。
- Linux x64 / Node 24 Web standalone：`artifacts/omicsclaw-app-standalone-linux-node24.tar.gz`，SHA256 `9d0e3eed16119540d5bfeb6626fc3172d290e192beec857ff1381f7beae7235f`。已在源码目录之外实际启动，依赖不会从上级源码目录补足。

`evidence/` 保存独立审核报告、完整日志、JUnit、实际依赖版本、原失败尝试和浏览器证据；`reproduction/` 保存固定输入、哈希和隔离测试依赖。总文件清单见 `SHA256SUMS`。本轮只做本地提交和容器内交付，没有推送、发布或替换用户运行中的服务。

原仓库已有其他未提交的 context/Run 改动，已保留。功能提交只收录本轮范围；完整测试快照包含当时的集成工作树，因此复现请用交付的源码快照/本地 tested tag，不要将功能提交误当成整个已测工作树。

## 使用与范围

启动匹配版本的 Backend 和 App，创建聊天并发送首条消息，在 Notebook 页面显式绑定该聊天。随后可交替让 Agent 执行 Python 和手动运行 cell；在已完成的 execution 上发布状态，再回到聊天继续分析。Web 产物独立启动方式见 `artifacts/STANDALONE_README.md`。

进程重启保留 Notebook 记录和绑定，不保留 Python 变量，也不会自动重跑代码。`.ipynb` 只保证代码单元及支持的输出互换，不是任意 Jupyter metadata/widgets 的无损往返。当前 live-app 接口接收 note/selection/view 的结构化状态，未实现任意 HTML/JS 应用托管或通用 RPC。

这次交付包含 Linux Web 运行产物；没有制作、签名或验收 macOS/Windows Electron 安装包。390px 视口验收覆盖 Notebook，窄屏 Chat 的既有 File Tree 布局限制没有在本轮修复。真实商业模型 eval、真实外部部署和性能基准不在本轮验收结果中。
