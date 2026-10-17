# SSH Notebook 实施与验收记录

日期：2026-10-15，更新于 2026-10-16。原计划依据 [Notebook 对比及优化方案](2026-10-15-notebook-comparison-and-roadmap.md) 中的 11 项决策按 S0–S6 实施；2026-10-16 用户将当前交付收敛为核心 SSH Notebook。每批实现后由另一 agent 独立审查。

当前核心范围：SSH 连接 → 登记已有科学环境 → 首次获准分析自动建立 Notebook → Python/R 持久执行和结果展示 → 用户编辑、Run、继续 Agent → 保存与显式恢复已登记对象。Notebook 按需启动；普通聊天不创建科学内核。

核心实现：S0–S3、S4a 对象与选择状态、S4b 整组保存已通过各自开发组件验收并合入工作树。S4c 正常 Restore 的实际浏览器、Python PBMC 和 R DESeq2 结果已独立核验。整组 Restore 与 owner 故障后的 Release 修复现已合入两端工作树，37 个路径按覆盖前后哈希核对；合入后后端 36 项、App 56 项及 Web/Electron 类型检查通过。新的实际 SSH owner 故障场景已获独立接纳：原回执未重放，Release 后进程全部退出，原 gate/hold 解除，新代内核成功执行；旧故障现场保留。

低额度 AnnData 保存修复也已合入：四个文件与已审版本逐字相同，另一个文件仅接入已审的内存和超时额度字段；相关检查为 32 passed、1 条件跳过，五个文件前后来源一致。该修复在 Linux 的独立子进程中执行 HDF5 写入，失败后原科学内核保留。跳过项不计为通过，当前检查不代表完整产品交付。

带对象维护、Job 回接、显式 owner 升级以及来源、导出、导入和环境材料的候选按用户要求停止后续开发与合入。已完成的独立审查及固定后端组合 9,437 passed、0 failed 保留为历史证据，不代表当前工作树的全功能验收。Replay、交互圈选扩展、原生安装包发布均延期。Live App 导航 P1 仍未关闭，保留在延期清单中。

正常启动链已修复并合入 App：捆绑当前核心 controller、读取部署缓存时校验版本，以及在独立控制环境中准备 Notebook 客户端依赖。核心组件共 487 个文件、4,494,877 字节，压缩包 1,563,752 字节，均在既定额度内。科学环境仍由用户登记，不在连接时安装或升级分析软件。组件来自隔离的干净本地源码快照 `219e4d0c18fecdf847b37c261296369617c0cf0c`；两个项目的工作树均未提交，尚未发布原生安装包。

2026-10-16 核心收尾验证：

- 当前后端绑定、普通聊天、富输出、helper 和组件检查为 24 passed、3 条件跳过；显式提供已有 R 库后补测这三项，3 passed。
- 供给候选为 44 passed、1 既有条件跳过，Web/Electron 类型与 scoped lint 通过；独立供给审查为 24 passed、无跳过。13 个 App 路径按覆盖前后哈希合入，包含匹配的组件资源。
- 实际 App 供给函数经 OpenSSH 分发上述组件并确认 Notebook 客户端可用；脚本化 Agent 首次执行自动绑定 AnnData 内核，用户 Run 把变量 40 改成 42，继续 Agent 读到 42 和同一 AnnData，SSH 重连保留原内核代次，最后明确 Release。四项断言通过，无清理错误，已获独立结果复核接纳。SQLite 中只有三次 Agent→用户→Agent 执行，均已完成；科学进程在 Release 后退出，空闲 owner 按设计保留。该检查使用实际 SDK/SSH 和固定模型响应，没有调用外部模型服务。
- 首次在线安装控制依赖因测试服务器访问包索引超时而失败，原记录保留。成功场景使用已有精确版本控制依赖离线准备的私有测试环境，再走正常 App 供给链；因此不将它记为联网首次安装通过。没有向科学环境安装包。

本机最终证据为 `core-final-backend-summary.json`、`core-supply-candidate1-independent-review.json`、`core-supply-root-integration.json`、`core-default-supply-v3-evidence.json` 和 `core-default-supply-v3-independent-review.json`，均位于 `build/notebook-implementation-20261015/`。之前各批的科学检查和失败记录继续保留。

## 核心功能的使用顺序

1. 使用匹配本次组件的 App 源码构建，选择 SSH alias 和远端工作目录，配置模型。已安装的旧 App 不会因本地源码修改而更新。已有正在运行的旧 owner 也不会自动换代；首次试用可选一个新的分析工作目录。
2. 在设置的 **Notebook 科学环境** 中点击 **连接并读取环境**，登记远端 Python 的绝对路径；使用 R 时另登记 R 可执行文件和库目录。Python 环境须有 `ipykernel` 及所需分析包，R 库须有 `IRkernel`。预检通过后使用该环境；有多个候选时在 Notebook 中明确选择。
3. 在聊天中提出分析请求，并按界面批准执行。第一次 Python/R 分析会建立 Notebook，后续同语言调用复用其中的变量。可在 Notebook 编辑代码并 Run，完成后点击继续 Agent；SSH 断开后重新连接可观察尚存的原内核。
4. 对需保存的 AnnData、表格或支持的 R 对象，在对象面板登记内存变量，再使用 **保存 Python 对象** 或 **保存 R 对象**。恢复时在 **恢复已保存对象** 中选择保存组并按界面确认。检查点只覆盖已登记且受支持的对象，不能保存整个 Python/R 进程的所有变量。

正常 Python/R 科学流程及浏览器交互已有分批实测。最终原生安装包和用户实际模型服务未在本次收尾中验证。

本机证据索引为 `build/notebook-implementation-20261015/independent-open-blockers-20261016.md`、`s6-reviewed-milestone-index.json` 和 `s5-accepted-integration-index-v1.json`。以下各节保留此前批次的原始结果；旧的“待修”表述应结合本段和后续复审记录阅读。

## 1. 批次状态

| 批次 | 当前状态 | 验收边界 |
| --- | --- | --- |
| S0 环境登记与契约 | 登记批及修复通过 | Python/R 登记、预检、版本接受、能力原因已验证。环境更新影响预览和匹配发布组件尚未交付 |
| S1 独立 owner 与重连 | owner 范围通过 | 真实 SSH 验证断线保活、原代重连、幂等、取消和故障恢复；普通聊天回归已在 S2 中途包修复并实测。目标服务器会话清理策略和发布安装包留待 S6 |
| S2 SSH Python 与默认体验 | 开发组件通过 | 首次分析自动绑定、编辑/Run/Resume、共享对象、固定 helper、富输出与有界传输；原生安装包及并行 App 改动的完整交付验证留待 S6 |
| S3 R 与环境维护 | 开发组件已通过并合入 | R/IRkernel 双语言交互；无对象环境更新、影响确认与失败修复；Python owner 故障。带分析对象的维护留待 S4 |
| S4 组学状态与恢复 | 对象、保存、Restore/Release 与低额度保存修复已合入；其余扩展延期 | 正常 Python/R 科学恢复、浏览器 Restore、新的 SSH 故障释放均已独立接受；带对象维护、Job、Live App 导航不属本次核心交付 |
| S5 来源与复现 | 按用户要求延期；已审候选和未完成 Replay 副本保留 | 不再继续开发或合入，不作为核心验收门槛 |
| S6 交付验证 | 只保留匹配核心组件与正常 App 连接验证；显式运行中升级、原生发布延期 | 当前核心组件源码与压缩包均未超额；正常 SSH 分发与交互已通过，控制依赖经过离线准备 |

后端基线为 `b04fd5099ecb1af732bd725235e5a9bdb9c7d6fe`，App 基线为 `48a226f5303f270135a0afe40f06178a30ab7790`。当前验证包含未提交改动，必须同时核对批次文件清单。另一任务正在修改 App 的界面；这些改动不属于本批。

## 2. S0 首次实现与审查

首次实现增加独立的 registered environment 记录，保留 Python venv 的启动路径，显式指定 R executable/library，并保存探测版本、依赖和环境指纹。App 的 SSH 设置增加登记、重新预检和接受新版本的入口。Notebook 与维护能力继续返回未连接原因。

首次审查未通过：

| 问题 | 影响 | 关闭条件 |
| --- | --- | --- |
| F1 / P1：临时 HOME 改变凭据屏蔽路径 | 科学环境预检弱化原有 sandbox 文件读取边界 | 最终策略仍屏蔽原用户凭据目录；完整 sandbox 的合成凭据实机检查另列为部署验收 |
| F2 / P2：超时后的管道清理无界 | 脱离进程组的后代可使预检超过预算 | 真实进程的超时、取消、持续输出与后代清理测试；区分有无原生进程隔离 |
| F3 / P2：错误码在 SSH peer 中丢失 | 输入和版本冲突被 App 误报为连接失败 | 受限语义错误经过真实编解码，HTTP 状态和恢复提示正确，未知异常仍脱敏 |
| F4 / P2：子进程参数入口未登记 | 两项既有架构检查失败 | 明确进程入口契约并通过检查，不通过混淆代码规避扫描 |

原始报告、复现与逐文件 SHA256 保存在 `build/notebook-implementation-20261015/`；该目录是本机证据，不随 Git 发布。首次失败与修复后的验证分别保留。

首次验证记录：

- 后端 targeted：实现者报告 30 passed；审查者独立运行新增环境测试，10 passed。
- 后端完整 CI unit 选择：8966 passed、2 failed、60 skipped、77 deselected、4 xfailed、1 xpassed。两项失败均为 F4，尚非绿色 CI。
- scripted eval：23 passed、8 skipped；跳过项依赖已退役的旧 skill step runner。
- App 首批相关测试：实现者报告 11 passed、1 条件 SSH 测试跳过；Web/Electron typecheck 通过。
- 实际 App `ControllerConnection` 经隔离 OpenSSH 连接开发组件：Python/R 登记、重连记录保留和旧 epoch 拒绝通过。该检查未覆盖 GUI 或发布安装包；开发 manifest 的 `sourceCommitVerified=false`。
- Desktop 兼容检查：已知 v3 客户端与当前 App 代理的 hello、approval、abort 通过。

后端验证解释器为 `/opt/conda/envs/OmicsClaw/bin/python`，版本 3.11.15。上述命令、源文件清单和完整日志位于同一证据目录。当前机器可运行 netns/PID namespace。另将仓库元数据校验过的 bubblewrap 包解压到本批 build 目录，未安装系统包；实际启动被宿主的 namespace/mount 限制拒绝。因此完整文件系统隔离尚未实机验收，netns 的通过不能替代它。

### 修复批复审与验证

F1–F4 均已关闭。凭据目录屏蔽使用宿主 HOME，并在额外挂载后生效；预检超时后的管道回收有独立上限；两端只传递白名单中的公开错误码；科学 Python 探测进程单独登记在打包和架构合同中。复审报告为 `s0-re-review.md`，证据为 `s0-re-review-evidence.json`。

2026-10-15，修复后的未提交源码验证如下：

- 独立复审：后端 64 passed，App 25 passed，均无跳过；27 个冻结文件在复审前后相同。
- 完整后端 CI：8985 passed、60 skipped、77 deselected、4 xfailed、1 xpassed，533.87 秒，无失败。运行期间 891 个源码/配置文件哈希未变化。日志为 `s0-repair-backend-ci-unit.log`。
- 另用科学 Python 的 pytest runner 加载 control venv 内锁定的 SDK，补测 controller protocol/client lifetime：34 passed、无跳过；未向基础科学环境安装包。
- 实际 App `ControllerConnection` → OpenSSH → 新打包开发组件：不含 ipykernel/scanpy/anndata/numpy 的轻量 control 环境可登记科学 Python/R，重连保留版本；控制端缺依赖与科学端缺 ipykernel 分开显示；四类语义错误跨真实 SSH 保留。
- 由后端基线代码创建的旧库，经本批增量迁移及重新打开后，合成聊天、cell、execution 和持久身份均保留；完整性检查通过。这是单一基线夹具，不是全部发布版本迁移矩阵。
- App Web/Electron typecheck 通过；本批 scoped lint 无错误、8 条既有警告。CI 结束时检查发现，并行 UI 任务向 `en.ts` 新增了 `shell.*` 文案；与冻结副本比较确认 Notebook 文案未变化，不将整个当前 App 树描述为同一冻结版本。

完整后端命令：

```bash
env OPENAI_API_KEY='' ANTHROPIC_API_KEY='' DEEPSEEK_API_KEY='' LLM_API_KEY='' OTEL_ENABLED=false \
  /opt/conda/envs/OmicsClaw/bin/python -m pytest tests \
  -m 'not slow and not demo and not eval and not scripted_eval and not skill_example' \
  -p no:cacheprovider -q -o addopts=''
```

跳过项和既有 xfail/xpass 不作为本批新增功能的通过证据。真实 SSH 检查使用开发 archive，`sourceCommitVerified=false`；尚未覆盖原生 Mac GUI、生产安装包或完整 bwrap 文件隔离。上述门槛允许进入 S1，不代表整个 S0 或完整 Notebook 已交付。

### 浏览器补验发现的跨模块错误

真实 React → Next API → App → SSH 补验发现 F5：共享连接可能由另一路由 bundle 创建，其 `ControllerProtocolError` 与环境路由中的构造器不同。非法路径错误仍带 `invalid_environment_path`，但 `instanceof` 为 false，页面收到 HTTP 503 和“连接失败”。首次单模块测试未覆盖此情况。修复改为读取自身数据属性中的公开白名单 code，不依赖构造器身份，不执行 getter，也不转发任意异常消息。窄范围独立复审 23 passed、0 skipped，4 个冻结文件哈希相同。

该检查使用独立 App 源码副本、数据库和远端 workspace，已核对监听进程的 cwd、环境变量及数据库文件描述符。开发 publication 由夹具配置，native client proof 由测试模拟；不计为原生 Mac 或生产发布验收。最终真实页面检查通过：环境列表、非法路径 HTTP 422 与提示、Python/anndata 登记、R/IRkernel/DESeq2 登记、刷新后两条记录保留；无 pageerror。原 Next 进程及跨 bundle 共享连接在修复前后复用，未通过重启绕过问题。证据为 `s0-browser-evidence.json`、`s0-browser-server.log`、`s0-bundle-error-review.md` 和对应冻结清单；截图保存在 `s0-browser-fixture/`。检查后已按进程出生证据关闭私有 Next 服务。

## 3. 科学验收输入

已在 Notebook 集成实现前固定输入、参数、容差和判定条件，保存在本机 `scientific-fixtures/manifest.json`。该 manifest 的 SHA256 为 `1c1c44d2829746c1a3e2f30d6cc0a32e83bf9227591bff832f118018ecfaef69`。集成测试接入时需将可发布的小型 fixture、生成器及获取记录纳入测试目录；大型数据继续按 checksum 获取。

PBMC3k 从已安装 Scanpy 的 `datasets.pbmc3k` 使用的维护者地址重新获取，与本地 raw 数据的 counts、barcodes、gene names 和 gene IDs 一致。直接访问 10x 原始归档返回 HTTP 403；没有将维护者下载文件的本机 SHA256 宣称为发布方校验。来源说明见 [Scanpy 数据文档](https://scanpy.readthedocs.io/en/stable/generated/scanpy.datasets.pbmc3k.html)。

Python 参考流程得到 2638×2000 的分析对象，完成 QC、归一化、特征选择、PCA、邻域、UMAP、Leiden、101 个细胞的后续 marker 分析及 h5ad 往返。两次独立运行的身份和 counts 一致，聚类 ARI 为 1.0，PCA/UMAP 在预定容差内一致。前 101 个细胞是技术选择 fixture；其 marker 结果不承担具有生物学重复的条件差异推断。

R 输入为固定种子的负二项合成 counts，包含 1201 个基因、每组 4 个样本、平衡 batch 和已知升降方向。参考流程实际运行 R 4.3.3 / DESeq2 1.42.0，检查样本对齐、设计矩阵、反向 contrast、合法 p/padj、允许的 NA 和 RDS 往返。两次结果在预定容差内一致。null 基因误报比例与 FDR 分开表述；统一 batch 倍数仅用于设计和对齐流程，不能证明基因特异的批次校正。

另外以真实 IRkernel 执行四个连续 cell，验证变量跨 cell 保留，再运行相同 DESeq2 recipe。仅调整基准脚本的输入根路径、输出目录和证据标签；基因身份、NA 分布及所有结果字段与直接 Rscript 基准在预定容差内一致。该检查耗时 10.015 秒，记录在 `irkernel-reference/evidence.json` 与 `comparison.json`。

这些是直接客户端运行的参考分析。IRkernel 经 Notebook/SSH 的交互、Notebook 选择身份、用户介入、断线保活、自动 checkpoint、Job 回接和导出复跑仍须经过各自的集成验收。独立审查提出的限制与固定脚本哈希保存在 `scientific-recipe-contract.json`，未依据运行结果调整原通过阈值。

## 4. S1 提前联调

在 S1 实现尚未冻结时，先将后端打包成独立开发组件，使用无科学栈的 control venv、真实 App `ControllerConnection` 和隔离 OpenSSH 做联调。该组件有 348 个文件，源码 3,923,215 字节，archive 1,404,115 字节；`sourceCommitVerified=false`。这是开发链路证据，不是发布验收。

回执丢失测试通过：提交前暂停 SSH stdout，科学 cell 写入入口计数后确认客户端尚未收到回执，再终止原 controller。重连后，在 cell 仍运行时并发重试同一命令，两个调用取得同一 execution；原 owner、内核进程和变量保留，入口副作用只有一次，结果已持久化。清理后核对了真实内核进程退出。证据为 `s1-preflight-lost-ack-evidence.json`。

首次生命周期测试发现 S1-P01：EOF 后静默等待 37 秒，cell 仍可完成并保存结果，但随后故意提交不同源码的同一 command，收到正常 `command_conflict` 拒绝后，下一条合法 cell 被 `notebook_durability_failed` 拒绝。实现把操作任务的所有异常都视为持久化失败。修复后，请求校验和冲突拒绝不再触发该状态；实际 SQLite 故障、输出写入和续租失败仍保留各自的失败处理。原失败证据保存在 `s1-preflight-lifecycle-evidence.json`。

重新打包后，生命周期与回执丢失场景均通过。EOF 后静默 37 秒、controller SIGKILL 和 App transport close 均保留原 owner、内核代次与变量；运行中及完成后的同命令重试没有重复副作用。显式取消后可在同代继续使用变量；旧代取消被拒绝时，新代正在运行的 cell 继续完成。释放和最终清理均核对真实内核进程退出。证据为 `s1-preflight-repair-lifecycle-evidence.json` 与 `s1-preflight-repair-lost-ack-evidence.json`。controller 加 owner 的当前 RSS 样本为 107,642,880 字节，低于 160 MiB 门槛；该样本不代表完整 profile 的峰值或冷启动验收。独立 reviewer 正在审查这个冻结后端包。

验收脚本经过独立静态审查，改用文件屏障确保故障发生时 cell 尚在运行，并在取消后先验证同代继续执行、在新代运行时发送旧取消、核对进程出生信息及真实退出。当前测试的 `app-transport-close` 指连接层关闭；真实 Electron/Next 退出、事件分页和发布资源测量仍单独验证。

同一冻结组件的进程故障联调也通过：在 cell 入口副作用完成、计算仍等待屏障时 SIGKILL owner，新 owner 将原执行保留为 `outcome_unknown`、原内核标为 `unconfirmed`；重试仍返回原 execution，后续迟到的完成标记不被当作已确认结果。显式释放后，新代命名空间为空。另在运行中 SIGKILL 真正的 ipykernel Python 进程，执行变为 `failed`、内核变为 `lost`。六个已记录进程的出生身份均核对退出。证据为 `s1-preflight-process-faults-evidence.json`。独立审查核对了本次数据库和 marker，同时指出驱动两处断言不足：应等待迟到完成标记出现再检查 unknown，并在内核死亡后的重试之后复核副作用。驱动已补强，须在最终包上重跑；本次结果不涵盖任意用户子进程。

提前独立审查发现以下待修问题，成功的生命周期测试不能抵消这些失败：

| 问题 | 已复现影响 | 修复验收 |
| --- | --- | --- |
| owner 身份检查与 IPC 发送之间发生切换 | 旧 owner 的写请求可送入新 owner；共享 client 的并发 attach 也会改变地址或 token | 每次调用固定连接与 owner 身份，owner 收到请求后再次核对；旧请求无副作用 |
| 终态已保存但事件尚未提交时 owner 崩溃 | 恢复后 pending 标记使后续 cell 永久被 `notebook_busy` 拒绝 | 保留已确认终态，明确事件记录不完整并解除遗留占用，不重跑代码 |
| 内核根进程退出后跳过后代清理 | 已记录的 setsid 后代继续存活 | 在根进程身份有效时记录后代出生信息，退出后只清理身份仍匹配的进程 |

审查报告为 `s1-backend-pre-review.md`，对冻结包运行的 16 项既有服务/取消测试通过，348 个包文件哈希一致。另确认 controller 直接创建的 AppConfig 默认 sandbox 为 OFF，owner 却自行采用 AUTO/PROXY；owner 不解析 `OMICSCLAW_SANDBOX_*`。因此环境预检和内核运行的策略不同。修复将传递 controller 的实际策略并记录摘要，已有 owner 的策略变化须明确处理。上述旧包的原生 namespace 实测保留为特定配置证据；默认 OFF 下的修复包需重新验证，不能称为科学进程已隔离。

### S1 修复候选的验证

修复候选的 archive SHA256 为 `5e580ce4181a60c28fb62b5d3297d6631eb23a78968868588081688a438584b6`，包含 349 个文件、3,934,223 字节源码，压缩包为 1,406,925 字节。后端 25 个和 App 16 个本批文件在 `s1-product-freeze-files.json` 中固定，并与后端 archive、独立 App 源码副本逐项匹配。仍为开发组件，`sourceCommitVerified=false`。

该候选按 controller 实际默认 OFF 策略运行，页面显示 `off / open`。真实 SSH 生命周期、回执丢失和进程故障三组均通过，证据为 `s1-review-repair-*-evidence.json`。owner 崩溃后，当前配置的原内核也退出；执行保持 unknown，同命令不重放。故障驱动明确区分“旧内核存活并迟到完成”和“原内核已退出”，本次没有将未发生的迟到完成记为通过。旧 AUTO 配置的迟到完成证据单独保留。

正式独立审查已复验前述三项修复，并继续发现三项问题：旧 generation 的 unknown 执行仍被当作 active；操作等待响应时重连会使按钮一直禁用；持续输出绕过执行时限，1.1 秒预算的 cell 在输出 2.5 秒后返回成功。

修复后的候选 archive SHA256 为 `a6af0c1d065efa916612cae01d39730affd91f46ae9770546914a75801977dd1`，仍含 349 个文件。6 文件增量经独立复审，三项均关闭：后端 21 项、App 10 项通过；原持续输出复现现在约 1.15 秒返回 timeout，忽略 SIGINT 的内核在 5 秒宽限后被终止，并核对真实进程消失。报告为 `s1-independent-re-review.md`。慢 append 测试验证写队列限额及提交顺序，其阻塞点在进入 SQLite 前，不能代替真实磁盘长时间持锁故障。

该候选的真实 SSH 生命周期、回执丢失、owner/内核故障三组均通过；证据为 `s1-final-*-evidence.json` 中对应三份记录。Desktop 已知 v3 与当前 App 的 hello、approval、abort 均通过。科学 Python 加载隔离 control venv 的 SDK 后，controller protocol/client lifetime 补测为 34 passed，无跳过。

完整后端 CI 使用本记录 §2 的相同命令，结果为 **3 failed、9026 passed、60 skipped、77 deselected、4 xfailed、1 xpassed**，563.19 秒。1346 个源码、测试和配置文件前后哈希相同。两项独立 owner 测试首次 bind 返回 `notebook_resource_limit`；共享内核取消测试返回 `dead`，并出现异步 interrupt 未等待警告。日志为 `s1-final-backend-ci.log`，源码核对为 `s1-final-ci-source-check.json`。局部和独立测试的通过不抵消这三项失败。

真实浏览器已验证绑定、输出、关闭 Next/controller 后计算继续、重连保留对象，以及 UI 取消后同代继续执行。第三次驱动在释放成功并确认内核退出后，误将分页首屏当作全部历史而失败；第四次修正分页后又因按钮文案选择器错误结束。两次失败分别保留为 `s1-browser-third-*`、`s1-browser-fourth-*`，没有改写为整次通过。

随后对第四次的同一已释放 Notebook 做短补验：API 返回四条唯一记录、四页后 cursor 耗尽；新页面实际点击三次 Load more 后显示全部历史，选中断线期间完成的执行，读取到原来的 `BROWSER_AFTER_CLOSE 42`。generation 仍为 1、kernel 仍为 released，没有启动新内核或重跑分析。证据为 `s1-final-browser-pagination-evidence.json`，两张重连及释放后历史截图已人工查看。该候选的浏览器结论由故障链与短补验共同支持；完整 CI 的三项失败仍待修复。浏览器夹具模拟 native client proof，不是 Electron 安装包验收。

### S1 最终修复与批次验收

完整 CI 的三项失败已定位并修复。Jupyter 的同步桥将私有事件循环缓存进 ContextVar；在线程切换后复用该上下文会使 interrupt 遇到正在运行的 loop。同步 Jupyter 调用现使用独立上下文并回收其私有循环，原权限、guard 和输出回调仍在原上下文中。两项 owner 测试改用真实独立 controller 进程，避免将大型 pytest 父进程的 RSS 算入待验 controller 的资源额度。产品的 512 MiB 运行额度没有放宽。原失败 CI 没有采集当时的 RSS，后续 600 MiB 压力复现仅用于验证这一机制。

最终开发组件 archive SHA256 为 `fdb78074cf04bdcd079407089b5dc45210e060faca36964ff63e153c0df81087`，包含 349 个文件、3,935,879 字节源码，压缩包为 1,407,401 字节；全部文件与清单匹配，`sourceCommitVerified=false`。验证日期为 2026-10-15，解释器仍为 Python 3.11.15，基线提交及本地改动范围见 §1 和 `s1-ci-repair-source-manifest.json`。

| 检查 | 结果与证据 |
| --- | --- |
| 独立增量复审 | 6 个改动文件和 349 个包文件哈希一致；5 项上下文/取消测试、2 项真实独立 controller 压力测试通过；真实超额请求被拒绝，持续输出和 6 次运行中取消后变量仍在。见 `s1-ci-repair-independent-review.md` |
| 完整后端 CI | 9033 passed、60 skipped、77 deselected、4 xfailed、1 xpassed，569.67 秒，无失败；1348 个源码/测试/配置文件前后哈希不变。见 `s1-ci-repair-backend-ci.log`、`s1-ci-repair-source-check.json` |
| Scripted eval | 23 passed、8 skipped；跳过项依赖已退役的旧执行器。见 `s1-ci-repair-scripted-eval.log` |
| Desktop 兼容 | 已知 v3 与当前 App 的 hello、approval、abort 共 6 项通过。见 `s1-ci-repair-desktop-compat.log` |
| 真实 App/OpenSSH | 生命周期、回执丢失、owner/内核故障三组通过，全部使用上述组件摘要；确认本批内核进程退出。见 `s1-ci-repair-lifecycle-evidence.json`、`s1-ci-repair-lost-ack-evidence.json`、`s1-ci-repair-process-faults-evidence.json` |
| 真实 Next/浏览器 | v5 完成绑定、关闭 App/controller 后计算继续、原代重连、取消后继续使用变量及明确释放；同一 released Notebook 的短补验完成 4 页 API 历史、3 次 UI 翻页和原输出读取。见下述边界 |

v5 完整浏览器驱动仍保留为 failed：它错误地要求每次翻页都增加可见记录数，而轮询已经合并过的执行会被去重。独立审查未发现本次游标循环；补验在同一已释放 Notebook 上读取全部四条唯一历史及 `BROWSER_AFTER_CLOSE 42`，没有新建内核或重跑分析。证据为 `s1-browser-fifth-evidence.json`、`s1-ci-repair-browser-pagination-evidence.json` 和 `s1-browser-v5-pagination-review.md`。组合证据支持本批浏览器功能，不能将完整驱动改记为通过，也不代表动态重复页分支已经重新实测。App 的 16 个本批文件和 Notebook 文案与冻结版本一致；并行任务的其他文案变化单独记录。

S1 在上述范围内验收通过，S2 开始实施。当前 SSH 页面提供历史、状态、取消和释放，默认分析入口及编辑/Run 仍在 S2。浏览器运行于 Linux，并模拟 native client proof；Electron/Mac 安装包、真实目标服务器 PAM/cgroup 生命周期及完整文件隔离仍待 S6。默认 sandbox 如实显示 `off / open`，不作科学进程已隔离的承诺。

### 后续预检发现的普通聊天回归

S2 真实模型验收的前置检查使用上述未修改的 S1 组件和实际 App/OpenSSH，普通聊天在模型请求前返回 `AttributeError`，外部模型调用次数为零。原因是 `nudges._notebook_context` 读取 `app.kernels.notebooks`，而 S1 注入的 `NotebookOwnerClient` 只提供 owner IPC，没有该属性。使用冻结组件直接调用同一上下文入口，复现了 `'NotebookOwnerClient' object has no attribute 'notebooks'`。

证据为 `s2-provider-preflight-evidence.json` 和 `s1-post-acceptance-chat-regression.json`。此前的手工 Notebook、完整 CI 和 Desktop 兼容检查未覆盖这个 SSH 聊天入口。S1 的 owner 功能证据继续保留，但该组件还不能作为聊天可用的交付版本。S2 已将修复和“真实 controller 普通聊天完成、零科学内核”回归列为优先项，修复后重新执行实际 SSH 检查。

## 5. S2 请求状态与默认分析入口

请求上下文核心增量已独立审查通过。审查者以固定 S1 组件叠加 10 个冻结文件运行 274 项定向测试，并补充共享 registry 并发、取消恢复及两种审批预算检查，4 项通过；所有冻结文件和测试副本哈希一致。报告为 `s2a-context-independent-review.md`。

每次模型请求固定自己的 Notebook 状态，工具执行前先校验，再进入审批。重试复用原请求状态；下一轮推理重新读取。这个核心批次只证明请求身份和检查入口正确，owner 接纳时的再次校验、子 agent 独立会话及完整 SSH 交互仍在 S2 组合验收范围内。

普通聊天崩溃的修复已通过真实 Controller 组装配合脚本化 provider 的回归，尚待新开发组件的实际 OpenSSH 重验。S2 允许普通聊天为读取状态启动轻量 owner，但不得因此创建分析绑定或科学内核。

### S2 中途组件的实际 SSH 联调

开发 archive `95a2bb860b8acaa6e18ad5c829dea77c61fb2e580af50b5b1cc80c36481ad754` 含 443 个文件，源码 4,190,476 字节，已逐文件核对并部署。实际 App `ControllerConnection` 经 OpenSSH 调用该组件，普通聊天完成且没有分析绑定或科学内核；首次获准 Python 执行自动创建 Notebook。S1 的普通聊天回归在这份完整依赖组件上已关闭。此包仍为中途开发版本，App 连接模块取自当时工作树，尚不属于整批冻结交付。

脚本化 HTTP 模型通过实际 SDK 完成 5 次请求，外部 `deepseek-v4-flash` 完成 7 次请求，均验证 Agent 创建 AnnData、用户编辑选中细胞与分组、Resume 发起新请求，以及 Agent 在原对象、原内核代次继续计算。选中细胞的 counts 汇总和分组数与固定期望一致。证据为 `s2-scripted-interaction-evidence.json` 和 `s2-live-interaction-evidence.json`。

三项竞争场景也通过实际 SSH：推理期间接管、审批期间接管、同一模型回复中的第二条陈旧 Python 调用。旧调用分别返回 `notebook_user_control` 或 `notebook_request_stale`，新请求成功执行并断言旧副作用变量不存在；恢复回执确认新推理已经准备。证据为 `s2-scripted-races-evidence.json`。这些场景使用脚本化模型响应，不作为实际模型自主分析质量的证据。

独立驱动审查要求精确核对恢复请求的状态和错误归因；对原始 wire 记录的离线补验已通过，审查者复核了 9 个原始文件的 SHA256。报告为 `s2-driver-review.md`，补验为 `s2-interaction-evidence-revalidation.json`。完整输入矩阵、全部细胞/基因 ID 和初始分组的新增断言将用于最终 S2 组件，目前没有将其计入旧运行结果。

首次外部模型运行在用户夹具写入新分类值时失败，原始记录保留为 `s2-live-first-interaction-*`；夹具先将分组列转为字符串后通过。竞争驱动首次误读快照摘要中的 output，修正为读取 execution 接口后通过，原记录保留为 `s2-scripted-races-first-*`。两次均没有修改产品来绕过失败。GUI 默认发现、编辑按钮、富输出、最终冻结审查及完整 CI 仍待完成。

### S2 冻结候选与独立审查

后端候选 archive SHA256 为 `049b51434cf6a8ad13f90c9c0eac64b4bd9190c89cef6035dced3f1163bfe870`，445 个文件、4,212,807 字节源码、1,527,842 字节压缩包，逐文件验证通过。475 项定向测试及 715 项请求/权限测试通过；33 项 controller 测试因缺少可选 anthropic SDK 跳过，真实 Python/R 内核检查实际运行。首次定向运行暴露 IRkernel PNG 的 MIME 换行兼容问题，修复后仍严格拒绝非法 base64 字符。完整 CI 和整批独立审查继续进行。

这份冻结后端通过实际 App/OpenSSH 的默认分析、用户编辑、Resume 及三项竞争检查，证据为 `s2-scripted-races-review-component-evidence.json`。本轮实际执行完整输入矩阵、全部 ID 和初始分组断言。用户接管时，旧审批可被主动拒绝；驱动精确识别该 `ApprovalDenied` 原因或 `notebook_user_control`，没有接受任意错误。此前因错误形式变化而失败的驱动记录保留为 `s2-scripted-races-rich-component-first-*`。

App 第一份候选的独立审查复现了三项 P2：丢弃续接请求后仍可能发送聊天，读取最新 revision 的迟到响应覆盖新草稿，未绑定聊天的 Notebook 入口默选另一聊天的记录。实现者已修复并冻结 candidate 2，106 项组合测试及 Web/Electron 类型检查通过；独立增量复核尚在进行。清单为 `s2-app-candidate2-manifest.json`，原失败报告为 `s2-app-independent-review.md`。

浏览器前两轮均保留为失败：第一轮发送早于工作目录初始化；第二轮已收到模型回复，但开发服务器冷编译使页面渲染超过驱动的 30 秒等待上限。当前以冻结后的前后端重跑，分别等待 App 自身完成初始化和页面渲染；不会将这些失败记为 GUI 已验收。

后端整批独立审查未确认新的阻断项，273 项测试通过、无跳过，37 个冻结文件哈希一致，报告为 `s2-backend-independent-review.md`。真实 SSH 的富输出及 helper 场景通过，完整技能说明和源码已与组件文件逐字补核；证据为 `s2-scripted-rich-review-component-evidence.json`、`s2-fixed-helper-provenance-revalidation.json`。controller 协议和连接生命周期补测 34 项全部通过；scripted eval 为 23 passed、8 个旧执行器跳过，Desktop 兼容 6 项通过。

首次完整 CI 为 1 failed、9065 passed、60 skipped、77 deselected、4 xfailed、1 xpassed，594.80 秒，1363 个验证文件哈希不变。唯一失败沿用 S0 的“Notebook 能力关闭”断言；测试已改为核对能力开放，同时登记环境不创建 owner 数据库、科学绑定或内核。26 项相关检查通过，完整复跑正在进行。原失败保留在 `s2-first-final-backend-ci.log` 与 `s2-first-final-ci-source-check.json`，产品组件未改变。

App candidate 2 的前三项问题经独立复核关闭，随后新增两项 P2：临时 waiter 存在但尚未持久准备续接时，App 过早删除队列，断线后无法恢复；快速新建 cell 并输入时，新草稿继承上一格的 revision，Run 被禁用。后者由真实浏览器发现，并在冻结编辑器的独立 Chromium 夹具中复现，原 revision 5 被写入应为 0 的新 cell。candidate 3 正在修复这两项。证据为 `s2-app-independent-re-review.md`、`s2-browser-fourth-evidence.json`、`s2-new-cell-browser/evidence.json`。

第四轮浏览器已通过自动绑定、Save/Run、刷新保留草稿、实际 Resume、表格/大 PNG/异常与中文截断提示，但在上述新 cell 问题处失败，不能记为整次通过。第三轮的截断断言错误另行保留：单条流输出使用终态 `truncated=true` 和可见的 `[output truncated]`，列表预算耗尽才使用独立的 truncated 类型；驱动已同时核对有效标记与传输大小。

### S2 最终验收

2026-10-15 18:17 UTC，S2 在上述后端 `049b514…` 组件及 App candidate 3 范围内通过。App 清单 SHA256 为 `e6eb64ffdcbde9713949cfbe9e8791abcaf9165b24d8970aae03226da09a5f27`，28 个文件以 candidate 2 为基底，仅覆盖本次 5 个修复文件；并行 UI 改动未混入该冻结副本。

| 检查 | 最终结果 |
| --- | --- |
| 后端完整 CI | 9066 passed、60 skipped、77 deselected、4 xfailed、1 xpassed，595.95 秒，exit 0；1363 个源码/测试/配置文件前后哈希一致。见 `s2-final-backend-ci.log`、`s2-final-ci-source-check.json` |
| 后端独立审查 | 273 passed、无跳过，37 个冻结文件稳定；额外独立验证 S0 旧能力断言的单文件修复。见 `s2-backend-independent-review.md` |
| App 独立复审 | R01–R05 全部关闭；25 项测试、Web 类型检查通过，实际 Chromium 在有/无轮询时各 20 次立即新建输入均通过，28 文件哈希稳定。见 `s2-app-candidate3-independent-review.md` |
| 实际 SSH | 最终组件的默认分析/三类竞争为 14 次脚本化模型请求；富输出/helper 为 12 次。所有场景及进程清理通过。见 `s2-scripted-races-review-component-evidence.json`、`s2-scripted-rich-review-component-evidence.json` |
| 实际 Next/浏览器 | candidate 3 完整流程通过：普通聊天零科学内核、自动侧栏、Save/Run、刷新保留草稿、Continue Agent、原对象和精确汇总、表格/大 PNG/异常/中文截断、GUI 释放及真实进程退出；浏览器错误和清理错误均为零。见 `s2-browser-fifth-evidence.json` |
| 兼容和评测 | controller 协议/生命周期 34 passed、无跳过；scripted eval 23 passed、8 个旧执行器跳过；已知 v3 与当前 Desktop 代理兼容 6 项通过 |

浏览器截图已查看。完成后 Editor 的续接提示尚未随已完成队列清除，功能已完成但提示仍说等待；这是非阻断显示问题，已列入 S3 界面修正。实际外部模型证据仍是前述中途组件的 7 次请求，未冒充最终冻结组件的模型自主分析验证。

当前 App 工作树还包含并行 UI 改动。该组合套件为 111 passed、2 failed：一项方法合同摘要漂移，以及聊天流测试子进程在 200 秒后仍未退出，被核对进程身份后终止。另一个含合同的套件为 82 passed、1 failed；Notebook 行为子集 79 passed。记录见 `s2-app-candidate3-handoff.md`。这些失败未改记为通过，也没有用冻结副本覆盖并行代码；S6 仍需对最终完整 App 源码与匹配安装包验收。

本批仍是 `sourceCommitVerified=false` 的开发 archive，浏览器在 Linux 上模拟 native client proof；不能据此宣称 Electron/Mac 安装包、目标主机 PAM/cgroup 生命周期或完整文件隔离已通过。S3–S6 继续实施。

## 6. S3a 真实 R 内核验收

S3a 在独立后端和 App 副本中实施、冻结和审查，通过后按文件清单合入根树。后端开发 archive 为 `de985a2b8809af0cf58cf8c5c18d2ca2891e47887a6e58ad2ea957623d2fb336`，447 个文件、4,238,589 字节源码、1,533,055 字节压缩包。父任务逐文件核对归档、清单和冻结源码后部署；`sourceCommitVerified=false`，没有改动资源预算。

实际 App `ControllerConnection` → OpenSSH → owner → 登记 IRkernel 已通过七组检查，使用 9 次脚本化模型请求：

- 首次 Python/R 分别绑定，同一个 Notebook 中的同名变量相互隔离；实际 R 版本、R.home、库顺序、IRkernel/jsonlite 版本与包路径匹配登记观察，进程 argv、可执行文件和出生证据一致。
- Agent 创建 R 环境对象，用户检查完整 1201×8 输入后修改 counts、样本选择和分组；Agent 读取原对象，全部 1201 个基因汇总符合固定期望。
- Resume 后的首条对应模型请求只含一份最新 Notebook context，双语言身份、水位、stateSeq 和用户执行正确；持久 receipt 已 prepared，实际 R tool result 对应本次 execution。
- R 表达式、data.frame、PNG、message、warning 和预期异常可经有界输出返回。
- 显式结束 SSH stdin，旧 controller 正常退出、没有依赖 TERM/KILL 升级；重连取得原 owner/R 内核和计算结果。R 取消后仍在原代次使用对象，释放 R 后 Python 仍可执行。

证据为 `s3a-first-real-ssh-evidence.json`，清理错误为零。验收驱动的独立审查先发现三处证据不足：把一般关闭流程叫作纯 EOF、只信环境 ID、只统计 Resume 后请求数。父任务补齐实际退出、R 身份和精确请求/回执关联后才运行；修前报告保留为 `s3-real-driver-independent-review.md`。

同一组件另用真实 `deepseek-v4-flash` 完成 4 次模型请求。模型通过 `r` 工具创建对象，在用户编辑后从原对象计算，并由独立用户 cell 核对全部 1201 个汇总、对象身份和原代次。证据为 `s3a-first-live-r-evidence.json`，清理错误为零。这是固定合成 counts 的交互验收，不包含 DESeq2 拟合或生物学推断。

App candidate 1 的独立审查发现 P2：远端改变同一 cell 的语言后，普通 rebase 把本地 R 草稿改标为 Python，并可提交给错误内核。独立测试捕获了实际请求 body。candidate 2 保留原草稿语言，语言冲突时阻止普通 rebase/执行，异步读取后再次检查语言；98 项测试与 Web 类型检查通过，独立复审已关闭该问题，48 项测试与 Web 类型检查通过，16 个文件哈希稳定。后端独立审查另通过 7 项 R 测试、54 项兼容测试及六组真实 owner/IRkernel 边界验证，19 个自有文件和 447 个归档文件一致，没有新增 P1/P2。两份候选和原失败均保留，根树未被旧副本覆盖。

第一轮浏览器在首个聊天 POST 之前等待超时，未产生 Notebook GUI 验收结果；原因尚未确定，`s3-browser-first-evidence.json` 保留失败和清理记录。第二轮使用真实 Submit 按钮，记录输入及按钮状态后继续定位。第二轮通过首个 R Save 预留和 Run 启动，但脚本化模型在同一次 Resume 中错误切换阶段；修正的是验收 endpoint，原失败保留。第三轮使用 candidate 2 与同一后端组件，九项浏览器检查全部通过，包括 R 草稿保存/恢复、真实 Run、Continue Agent、原对象校验、表格/PNG 渲染，以及释放 R 后 Python 继续执行；浏览器和进程清理错误均为零。证据为 `s3-browser-third-evidence.json`，运行时源码和驱动另有哈希清单。

首轮完整后端 CI 为 10 failed、9064 passed、63 skipped，1364 个文件哈希稳定。其中九项来自父任务复制时漏掉六个公开示例/入口文件，已从未改动的 HEAD 文件补齐；另一个既有 bench 进程测试的 stray 计数为 2，预期为 1，单包复跑 11 项通过。补齐副本后，同一产品源码的完整 CI 为 **9074 passed、63 skipped、77 deselected、4 xfailed、1 xpassed**，604.99 秒，无失败；1370 个源码/测试/配置文件前后不变。使用 Python 3.11.15 和 §2 所列相同 CI 选择，最终证据归档为 `s3a-final-backend-ci.log` 与 `s3a-final-ci-source-check.json`，原失败另存。

后端 19 个冻结文件已合入，1370 项根树文件与通过 CI 的副本逐字一致。App 合入 candidate 2 的 16 项改动，三处并发差异由独立审查处理：每种语言仅补九个 Notebook 键，保留 52 个 UI 新键；方法合同只更新本批三个源码引用的 14 个字段。合入后 109 项 Notebook/协议测试通过、无跳过，lint 无错误、一条既有 warning，16 项自有文件哈希稳定。完整 App 的首轮 Web 类型检查在并行 UI 文件 `src/__tests__/e2e/ui-design-media.spec.ts:187` 报 `Event.pointerId` 类型错误；该任务随后修改文件，重新检查已通过，本批没有编辑该 UI 测试。Electron 类型检查也通过，失败与重验日志分别保留。历史方法合同摘要漂移已关闭。聊天流测试的窄延迟实验确认了测试清理竞态；单文件修复经独立复审后合入，改为等到终态再清理自身 GC。根树 Notebook/协议/聊天流组合测试 138 项全部通过、20.05 秒自然退出，产品会话保留策略未变。原历史挂起因缺资源快照仍不能唯一归因。

本批结论限定为准备好的开发组件。S2 已完成续接却仍显示等待的提示也在第三轮浏览器中确认消失；R 下拉占位与部分阻塞原因文案的改善随维护界面完成。Electron/Mac 原生安装、目标主机会话策略、完整文件隔离及最终组合源码验收仍在 S6。

S3b–e 已在另一个副本开始维护协调器与 App 接口实现。原地更新仍 disabled；checkpoint 屏障、真实安装、新版本与重启尚待验收。

### S3a 补验：实际 SSH 下的 DESeq2 拟合与断线保活

2026-10-15 20:05–20:06 UTC，沿用已验收的 S3a 组件和固定输入，在实际 App/OpenSSH、R 4.3.3 / IRkernel 1.3.2 / DESeq2 1.42.0 中完成三项检查：执行进程与登记环境身份一致；1201 基因、8 样本的原字节 recipe 通过样本对齐、满秩 `~ batch + condition`、反向 contrast、NA 和六个数值列的冻结容差；用户修改真实 `dds` 后，对 SSH stdin 发 EOF，重连仍访问同 owner、同内核代次和同 R 进程。前后的内存环境身份、用户标记及 counts/sample/design/result 摘要一致。

上调和下调植入基因的召回分别为 0.9833、0.9667，非零 null 基因被检出比例为 0.0102；该比例不是 FDR。父任务另核对 1045 个冻结源文件及五个输出文件，结果 CSV 与固定参考逐字节一致。显式 release 后，R 和 owner 的出生身份均已确认退出，清理错误为零。证据为 `s4-deseq2-preflight-evidence-NsqsiJ/evidence.json`、`s4-deseq2-preflight-result.md` 和 `s4-deseq2-preflight-parent-verification.json`。

首次启动遗漏私有 SSH wrapper 的 PATH，在连接阶段退出，未运行科学内核；原日志和证据保留。第二次仅修正启动环境，使用新私有工作区，冻结源未变。本次没有模型请求或依赖安装；原 recipe 的 RDS 写读是手动参考序列化，S4 自动 checkpoint、恢复、Job 和导出接口仍待实现。

### S3b 环境协调与维护界面审查

环境保护首批冻结 21 个文件，开发 archive SHA256 为 `70c5d2067f53426600c51fda369317b5a7ab4c75c210f2dc308e6ca0b44aa3ef`。实现者的 152 项定向测试全部通过，包含真实 Python/R 内核、Job、架构与打包检查。父任务完整 CI 为 9100 passed、60 skipped、77 deselected、4 xfailed、1 xpassed，652.85 秒、exit 0；1378 个源码/测试/配置文件前后哈希不变。日志为 `s3b-guard-backend-ci.log`，源码证据为 `s3b-guard-ci-source-check.json`。登记 SSH Python 以 `-I` 启动，实际运行与登记预检采用一致的导入路径隔离；旧 Desktop 行为保留。维护 RPC 尚未开放，不能将这一批记为包更新已经打通。

独立审查随后用真实进程确认两处缺口，当前候选暂不合入：无 PID 隔离的内核正常 release 后，脱离进程组的后代仍存活，但协调器已关闭占用；control 在 reserve 与补写进程身份之间退出，会留下无法回收的 pending 占用。六文件修复经独立复验关闭这两项，21 项定向检查通过。但复验又发现旧 CLI/Desktop 内核按既有策略换代时，SIGKILL 后立即检查退出会使下一格报错，四次真实运行中三次复现；新代启动前还缺少持久 launch intent。后续候选须一起修复。完整 CI 通过不能抵消这些失败；详见 `s3b-guard-repair-independent-review.md`。

六文件修复会保留无法确认退出的科学进程占用。它关闭了错误清除保护的问题，尚不足以交付环境维护：受支持主机还须提供匹配实际启动链和进程出生身份的退出证明，确认后代全部退出后才允许安装。配置中的隔离标签不作为证明；没有实际证明时保留未确认状态。

六文件修复的完整 CI 为 9103 passed、60 skipped、77 deselected、4 xfailed、1 xpassed，655.25 秒、exit 0；1378 个文件前后哈希相同，见 `s3b-guard-repair-backend-ci.log` 与 `s3b-guard-repair-ci-source-check.json`。这份固定副本仍包含上述独立复现的换代问题，暂未合入根树。

后续实际测试发现，本机 conda R/OpenMP 在共享宿主 `/proc` 的 PID namespace 中会崩溃。因此默认 SSH OFF 路径改用宿主 subreaper 监督进程，正常结束时回收被收养的后代并写入绑定出生身份的退出证明；监督进程异常死亡仍保留未确认占用。维护进程单独使用禁止网络的策略，普通显式 NETNS/BWRAP 策略未被降级。独立最小实验验证了 R/NumPy、脱离进程组的后代、非 SIGCHLD 子进程及监督者异常分支；它不构成文件系统隔离证明。

该生命周期候选冻结 26 个文件，组件为 `e97c7acdf57a57d0f0810c173c1fedeeb08ae0c6ea748952e757471d97d9ed9e`。父任务核对全部 457 个打包文件，源码 4,330,249 字节、archive 1,556,512 字节，均在原预算内。实现者的 28 项集成、63 项兼容及 9 项最终边界检查通过；独立审查的 32 项定向测试通过，但另用真实流程确认正常 Job 取消的占用回收缺口：进程及后代均已退出且证明有效，active 记录仍不能回收，影响后续维护。三文件修复经独立复审关闭：真实取消后，新 Python 进程能读取有效退出证明并回收占用；监督进程被强制终止且后代仍存活时继续保留占用。独立 17 项定向检查通过、无跳过，修复与原冻结文件哈希稳定。完整维护服务仍待接通，候选尚未合入；复审见 `s3-subreaper-repair-independent-review.md`，原证据见 `s3-subreaper-handoff.md`、`s3-subreaper-parent-package-check.json` 和 `s3-subreaper-independent/`。

维护界面 candidate 1 的独立审查发现两项 P2：另一个窗口删除待处理命令后，当前窗口仍一直阻止新操作；旧拒绝状态可能删除后来替换的命令。candidate 2 关闭这两项，但仍会在同一命令重试时误删恢复记录。candidate 3 修复后，独立双页浏览器又确认：较早 POST 结果未知、后一次重试被拒绝时，仅跟踪最近一次尝试仍会丢失后来获接纳的请求；不支持 Web Locks 的只读观察也会丢失已找到的 receipt。父任务另从实际连接入口发现，11 个维护方法尚未加入 `ControllerConnection` 白名单，调用在到达 SSH peer 前即被拒绝。

candidate 4 关闭上述问题，但独立复审发现持续 accepted 回执会覆盖用户选择的历史维护记录。candidate 5 保留用户选择，原失败用例复跑通过；独立 Node 20 测试 34 项、受控 HTTP 下的 Chromium 检查 3 项全部通过，20 个文件在三份副本中哈希一致。App 候选可进入真实 SSH 维护联调；后端审批和安装尚未接通，两端维护改动均未合入根树。

真实维护验收将使用独立私有 Python 环境和 R library 的本地合成包，不修改基础科学环境。成功更新驱动修复了清理门槛、独立磁盘核对、分页预算和失败 cell 身份采集问题，已通过独立复审。App candidate 5 另补精确的 `environment_maintenance_invalid_request → 422` 映射；独立复验原版为 2 passed、1 failed，三文件修复为 3 passed，未知错误仍脱敏。专用 harness 已切到这份修复副本，维护页面驱动同步固定依赖；原冻结版本保留。

失败与修复驱动另经独立审查，要求清理前核对原损伤、用当前 registry revision 挑战 accept，并证明合成包确实进入预期失败。清理和版本检查已通过独立复审，12 个纯控制流分支通过；最终失败事件字段已与冻结后端合同对齐。实际页面驱动也通过 12 个清理分支及 5 个写前文件清单分支的独立检查，但尚未启动 Next/SSH 安装验收。

完整维护后端 candidate 1 的独立审查暂不接纳，报告为 `s3-maintenance-candidate1-independent-review.md`。14 项真实 Python/R、RPC、Job 定向测试通过，但新增复现确认三项问题：旧 owner 退出后，repair 的验证请求仍发送给旧 owner；环境已验证提交后取消，后台任务退出却未写终态；并发 Restart 用旧数组覆盖另一项成功结果。前两项使用真实私有环境安装，第三项控制 mailbox 回执顺序验证持久并发行为。48 项改动及 466 个打包文件均匹配冻结清单。candidate 2 已冻结五文件修复，正在独立复审；原候选和失败证据保留。

candidate 1 的完整 CI 尝试被中断，结果为 7 failed、3809 passed、37 skipped、77 deselected、2 xfailed，1041.85 秒、exit 2，1399 个源码文件前后相同。独立诊断确认：六项失败受整个 pytest 进程超过控制端 512 MiB 配额影响；随后 observer 测试无限等待已被拒绝的执行。另一项测试在全局占用关闭前读取 impact，随后被合法的 `plan_stale` 拒绝；真实线程窗口实验复现了此顺序。父任务在核对本人进程出生身份后中断挂起，保留完整 trace。测试隔离、等待边界及真实占用收束另批修正，产品配额与 CAS 不变。报告为 `s3-maintenance-candidate1-ci-independent-diagnosis.md`；这次中断不能记为全套通过。

维护 App 已与当前根树的并行界面改动做独立整合，尚未写回根树。独立审查核对 20 项增量和全部来源，22 项 Connection、Peer 错误码及接口库存测试通过；两种语言各保留 2479 个原键，只新增 53 个维护键。后续只能在逐文件原始哈希仍匹配时应用增量，不能覆盖整树。报告为 `s3b-app-root-integration-independent-review.md`。真实 App/OpenSSH 的五项只读预检已通过，涵盖能力与精确错误码，未登记科学环境、执行安装或调用模型，不能代替维护验收。

### 维护第三版复审与首轮实际 SSH

candidate 2 关闭前述三项问题后，独立审查又发现显式 Restart 会替换任务索引，使 owner 关闭时遗漏仍在等待的自动重启任务。candidate 3 以完整任务集合跟踪并回收主 worker 与 retry；原真实安装复现及三项并发关闭检查通过。49 项增量及 466 个打包文件匹配冻结清单，组件 SHA256 为 `d05f385704ee4d9918fb299d6a8dda1e9d8c2154351155f9be74c2e0af644a5e`。报告为 `s3-maintenance-candidate3-independent-review.md`。本结论允许进入实际验收，尚不表示完整维护已交付。

六文件测试隔离修复另经独立复验：父测试进程额外持有 600 MiB 时，11 项检查通过；真实子 owner 仍按原 512 MiB 限额拒绝超量，科学进程采样未被替换。修复没有提高产品预算。c3 与该测试增量的完整 CI 在独立副本自然完成：9159 passed、1 failed、60 skipped、77 deselected、4 xfailed、1 xpassed，826.64 秒；1402 个源码与配置文件在检查前后相同。唯一失败是 `tests/launch/test_grammar.py` 的进程入口清单缺少 `skillenv/process_supervisor.py`、`skillenv/maintenance_probe.py` 和 `skillenv/contained_exec.py`。单文件补齐登记后，原失败用例已独立复验通过；扫描器和精确集合断言未变，375 个产品 Python 文件逐字节相同，见 `s3-maintenance-entry-inventory-independent-review.md`。日志中的 `KeyboardInterrupt` 来自既有 hooks 用例捕获的日志，不是本次 runner 被中断。原日志和源码核对见 `s3-maintenance-candidate3-combined-ci.log`、`s3-maintenance-candidate3-combined-ci-source-check.json`。

首轮实际 Python/R SSH 检查均在创建维护计划前停止。驱动错误要求两个新工作区具有相同 `authorityId`，但该身份属于各自工作区数据库；同一账户的跨工作区环境协调使用物理路径及 inode，不依赖该等式。原驱动、失败证据和环境保留，新驱动改为验证活 owner 的同账户、主机出生标识、SSH 目标及共同物理安装根，并继续检查计划影响包含两个 Notebook。

浏览器首轮则在 Plan 返回 `environment_maintenance_unconfirmed`。独立只读复现确认，同一 root 账户的协调库中，其他 pytest 工作区存在保守范围为 `/` 的内核占用，其中部分无法确认退出；计划不能为这些占用找到有效 Notebook owner。当前计划探测本身正常退出，未建立维护记录或执行安装。此结果不支持删除未知占用来放行。后续实际验收改用专用系统账户、独立 home 和私有科学环境，继续使用产品按 UID 和主机定位协调库的原逻辑。原 root 协调库、占用记录和浏览器 owner 均保留。诊断见 `s3-maintenance-run1-independent-diagnosis.md`。

专用账户的第二轮在 controller 握手阶段停止，尚未配置科学环境或执行安装。测试部署复制了 archive 中的 466 个文件，却漏放独立的 `controller-manifest.json`；实际 hello 返回 `manifestVerified=false`，App 正确拒绝连接。原三份失败记录和只读 hello 探测已归档至 `s3-maintenance-account-run2-evidence/`。修复只补齐测试部署清单，产品校验不变；这次失败不计为维护功能通过。

补齐部署清单后，实际 App hello 通过。第三次启动因已有结果文件而在 SSH 前拒绝覆盖；第四轮使用新结果路径，Python/R 各完成四项真实检查：同环境两个工作区的影响范围、已获准维护在 controller EOF 后继续、新包版本与新内核代次、checkpoint 阻止更新及取消后原内存保留。独立复核确认 466 个组件文件未变，四个工作区共 8 次执行最终持久完成，Python 11 项与 R 24 项包目录记录和取消后的清单完全一致，记录的进程均已退出。报告为 `s3-maintenance-success-run4-independent-review.md`。此轮不含 S4 对象保存、恢复或发布安装包。

失败修复轮已实测：Python/R 安装或验证失败后保留损坏 epoch，controller EOF 后 owner 和损坏状态不变，两个受影响 Notebook 均拒绝 Run。随后驱动将 `environments.list` 的 `environments` 字段误写为 `items`，在发起 Repair 前停止；四个 owner 与损坏标记按规则保留，后续从原失败记录显式修复。owner 故障驱动的首次启动因账户内尚有维护损坏而在环境登记阶段被拒，未执行安装或 SIGKILL；浏览器的环境登记也尚未通过。以上失败分别留证，不计为故障恢复通过。

对同一浏览器工作区的实际 SSH 复现确认，登记返回公开错误 `environment_maintenance_frozen`；App 环境路由漏映射该维护错误，将它变成 `503 environment_connection_failed`。登记在预检前已保存一条 `unverified`、revision 0 记录，不能描述为完全没有登记。窄修保留冻结策略，补齐错误投影；证据为 `s3-browser-register-diagnosis.json`，未绑定或运行内核，也未执行安装。

修复续接沿用上述四个工作区、owner 和原失败记录，未重新制造失败。Python/R 各 14 项检查通过，独立只读复核确认：preflight、accept 和取消修复均保留原损坏 epoch；显式 Repair 验证成功后才关闭原保护；两个工作区的新内核均实际导入 0.0.2；另行 Release/Bind 后，过期 Restart 被精确拒绝。最终数据库中的六次执行已持久完成，每语言八个记录进程均已退出，清理错误为空。49 个驱动与依赖、466 个组件文件及两份原失败证据哈希不变。报告为 `s3-maintenance-repair-continuation-independent-review.md`。

owner 故障第四版驱动在实际安装 0.0.5 后、发送 owner SIGKILL 前停止。它误认为 verifier 的直接父进程就是宿主监督进程；实际 netns 路径中还有固定的 bridge/contained_exec 包装。原 owner、维护保护和失败证据保留，未释放测试 barrier。后续只读检查找到匹配出生身份的退出证明，表明该验证进程树已收容；退出码为 -9，不能视为验证成功。需先从原现场显式修复，再用新输入验证修正后的故障驱动，不能重用已安装的夹具或把这次失败计为 owner 故障通过。

该现场已通过公开 Repair 续接恢复：原损伤存在和两边 Run 拒绝、新版本与新内核代次共四项检查通过，两边均实际导入 0.0.2，原损伤保护确认解除后才清理进程，清理错误为空。47 项冻结依赖和原退出证明先经父任务核对；结果的独立复核继续进行。证据为 `s3-maintenance-owner-fault-python-recovery-v1-evidence.json`。这次是恢复被驱动中断的安装现场，仍不构成 owner SIGKILL 通过。

App 错误映射的七文件窄修已独立通过：21 项定向检查通过，另有一项未配置的可选 SSH 测试跳过；1302 个完整源文件和七项增量哈希稳定。报告为 `s3b-app-registry-errors-independent-review.md`。修复后的完整浏览器维护流程仍待实际验收。

修复后的 account-v3 浏览器流程已通过四项实际检查：界面固定计划与受影响 Notebook、批准安装后新代次实际导入、刷新后显式读取维护历史、checkpoint 阻止安装并取消后保留原内存标记 713。没有浏览器或清理错误，模型请求为零，1134 个 App 源文件前后不变。独立复核确认两个执行最终持久完成、三份进程出生记录均已退出，阻止、取消和清理阶段的包目录清单一致；同时确认上一段原故障现场恢复的两个执行已提交、八份出生记录均已退出。报告为 `s3-fault-recovery-browser-independent-review.md`。此处是实际 Next/OpenSSH 的开发组件验证，原生客户端证明由夹具模拟，不构成 Electron 安装包验收。

owner 故障第六版使用新的包与 barrier 路径，进程链和 proof 重读共 14 项验证通过；实际主流程仍在首次登记时停止，未建立维护或安装故障包。专用账户的冻结预检复现 `PermissionError`：新夹具的 `--system-site-packages` 继承了指向私有开发目录的 editable 元数据。原失败证据、初始包及不存在的三个 barrier 文件保留，清理无错误。后续夹具恢复此前通过的独立 site-packages 方式，在启动主流程前先跑完整登记预检；不修改私有目录权限或基础环境。

第七版夹具完整登记预检通过后，实际 owner SIGKILL 场景完成七项检查，独立复核通过。验证进程仍存活时终止 owner，重连取得 epoch 2 的新 owner；两个工作区均拒绝 Run，Repair 精确返回 `environment_maintenance_unconfirmed`。五次活进程样本及出生证明覆盖这个窗口。验证进程树随后退出，退出码 120 仍按未知验证处理，原损伤保留；新的明确 Repair 验证 0.0.2 成功后才解除保护。原维护和修复各仅安装一次，原验证入口计数为一。经明确 Release/Bind，两边新内核实际导入新包，两个执行最终持久完成；13 个记录进程均已退出，清理错误为空。报告为 `s3-owner-fault-v7-independent-review.md/json`；这是 Python 合成包的实际故障验收，不扩大为 R 故障、内存恢复或发布安装包验收。

上述维护后端、六文件测试隔离修复、入口清单修复及 App 维护/错误映射共 78 个最终文件，按原工作树哈希逐项核对后合入两端。合入只搬运已审查文件，保留并行界面改动；原文件备份和逐项结果为 `s3-reviewed-root-integration.json`。整合后后端 48 项、App 134 项定向检查全部通过且无跳过，App 检查包含真实 Python/TypeScript peer；Web/Electron 类型检查通过，定向 lint 无错误、保留一条既有 ref 警告。78 个整合文件在检查后哈希仍匹配；原完整 CI 的失败日志保留。

## 7. S4 对象与恢复合同

对象 App 首份候选按第三版合同实现五项操作：登记、检查、创建选择、显式重映射和清除当前选择。清除允许源选择已失效，但须核对它仍是当前选择；历史保留，不自动执行代码。缺少新 capability 时保留 S3 行为；能力已开启但状态读取失败时保留草稿和 Save，阻止 Run。

独立审查复跑原 21 项 Node 20 测试和实际 Chromium 双页的 4 项检查，全部通过；20 个改动文件及 1292 个完整副本文件的哈希稳定。但新增两例确认同一 P2：当前 Python 选择在 R Run 中被遗漏；尚无 R kernel 时，失效的 Python 选择也未阻止首次 R Run。当前选择属于整本 Notebook 的分析上下文，任何语言的 Run 都要验证并保留其源对象引用，这不代表 Python/R 共享变量。原失败报告为 `s4a-app-independent-review.md`。

candidate 2 的五文件修复已通过独立复审：原两项失败不改断言转绿，29 项定向测试自然退出；新增延迟绑定检查确认，界面随后观察到另一选择时，已发起的 bind→Run 仍固定原分析 revision、对象和选择。第四版合同明确了这项跨语言语义，未增加操作；完整副本与改动文件哈希稳定。报告为 `s4a-app-candidate2-independent-review.md`。未将上一候选的浏览器通过计为新候选重跑，也未声称查明早期测试高 CPU 停滞的原因。App 修复尚未合入，后端已在独立副本开始接线，实际 SSH 的对象和选择验收仍待完成。

checkpoint/恢复预审已完成，报告为 `s4-checkpoint-contract-independent-review.md`。实施前须补齐阶段保存与排队修改的持久顺序、保留内存的停止策略、登记对象覆盖与维护损失确认、持久恢复屏障，以及两个既有存储间的整组发布和引用保留。主方案 §6.7–6.8 已纳入这些要求，后续仍按真实 SSH 科学流程验收。

S4a App 的整合副本已通过独立复审：1322 个完整副本文件、20 项增量及来源哈希匹配，保留维护功能和 8 项并行界面改动；25 项连接/代理/错误检查及原跨语言回归 2 项通过。Python/R 科学检查的七文件子批也已交叉审查并合入独立后端副本，41 项检查通过，另核验稀疏 AnnData、组合身份、真实 R 对象和检查前后数据不变。报告分别为 `s4a-app-integration-independent-review.md`、`s4-scientific-independent-review.md`。后端对象操作、模型工具和真实 SSH 科学通路尚未完成，以上结果不能替代 S4 整批验收。

后端 S4a candidate 1 冻结 29 项改动，包含上述科学子批。冻结副本的 152 项定向检查通过、无跳过；476 项打包源码与 archive 核对一致，实际 SSH hello 的 manifest 校验通过。独立审查另确认两项 P2，因此暂不接纳：无效登记对象会阻止修正 Run，释放并新建内核后仍被旧对象阻挡；选择文件在发布失败后留下未登记产物，额度检查漏算这些文件。后者以三次被拒的 75,034 字节输入复现，残留 225,102 字节但预算仍通过。原证据为 `s4a-independent-recovery-probe.*`、`s4a-independent-orphan-budget-probe.*`；c1 保持冻结，修复另批实施。

恢复入口采用显式取消对象登记：只退出当前登记，内存对象和历史仍保留，用户可修正后重新登记；旧选择仍须明确清除或重映射。新代 Run 只校验该代的活动登记对象。文件修复须处理失败和事务回滚的本次产物，并将未登记文件纳入保守额度；不得通过放行所有失效对象或提高额度掩盖问题。

后端 candidate 2 的九文件修复和完整 31 文件候选已冻结；161 项组合检查通过、无跳过。此前同一组合的 160 passed、1 项新增回执断言失败，以及修正后的子包结果均保留。App 第五版合同接入取消登记与 `registrationState`，并合并已验收的维护错误提示；Node 20 的 45 项、Node 24 的 50 项检查通过，后者另有一项可选跳过，Web/Electron 类型及定向 lint 通过。两端正由另一 agent 独立复审，尚未合入或计为真实 SSH 科学验收通过。

后端窄修已通过独立复审：原两个失败脚本转绿，另有 20 项检查通过，476 个打包成员与两组冻结清单匹配，完整 CI 已启动。App 则确认新的 P2：401 个历史对象、零活动对象时，运行守卫仍要求加载全部历史；界面最多加载 400 个，因而永久无法 Run。实际后端的分页数据和真实 hook 均复现，报告为 `s4a-object-removal-app-independent-review.md`。修复将增加有界、同一分析 revision 的运行上下文读取，包含当前活动对象及当前选择和它的源；历史浏览不再作为运行前置，也不能因此忽略失效选择。原 App 候选暂不接纳。

运行上下文的四文件后端增量已通过独立窄审：13 项定向测试和三项额外边界通过。单事务投影保留同一水位，包含最多 32 个当前活动对象及全局选择的精确来源，返回不超过 32 KiB；403 条历史不阻止新 Run，当前失效对象与跨语言失效选择仍被拒绝。原 Run 校验未放宽，App 原问题须待接线及原失败复现转绿才能关闭。报告为 `s4a-run-context-independent-review.md/json`。

candidate 2 完整 CI 自然结束：9206 passed、30 failed、60 skipped、77 deselected、4 xfailed、1 xpassed，877.33 秒；1421 个源码与配置文件前后一致。29 项失败发生在同进程 owner 的控制 RSS 检查，另一项 Channel 测试在适配器启动后立即发送 TERM，未等到 manager 启动日志。独立诊断已确认两类测试问题：同样五个原用例在冷进程通过，pytest 额外持有 600 MiB 后全部在 RSS 检查失败；Channel 的受控启动屏障证明 adapter marker 早于 manager 就绪。修复仅让四个内联 owner 测试模块使用既有隔离 fixture，并有界等待真实 manager 就绪，保留实际子进程配额负例。报告为 `s4a-candidate2-ci-independent-diagnosis.md/json`；修复及完整 CI 重跑尚未完成，不调整产品配额。日志与源码核对为 `s4a-candidate2-full-ci.log`、`s4a-candidate2-full-ci-source-check.json`。

App 运行上下文候选的 33 项检查及原历史上限两条复现通过，但独立审查新增两个 P2，暂不接纳：历史下一页读取失败会清空有效 Run guard；当前无效对象排在第 401 条历史之后时，运行已被保护阻止，Inspect/Remove 恢复入口却也不可达。真实后端分页按 `entity_id` 排序，独立夹具确认该对象在第 11 页。修复须分离历史错误与当前运行状态，并从最多 32 个当前 ObjectRefs 独立读取详情；详情失败保留引用和重试入口。报告为 `s4a-run-context-app-independent-review.md/json`，1324 个完整源文件及 13 项增量前后不变。

App 五文件 candidate 2 已通过独立复审，R04/R05 均关闭：两个原脚本逐字复跑通过，相关回归与新增边界共 36 passed、0 skipped。额外检查覆盖全部历史读取失败、迟到 rev1 详情不能覆盖 rev2 或 Inspect CAS、32 个当前对象最多四个详情请求在途。1324 个源文件哈希稳定，未扩大为实际浏览器或发布验收。报告为 `s4a-run-context-app-candidate2-independent-review.md/json`。

真实 SSH 科学驱动第二版已完成 PBMC 固定计算及参考对照，Agent 的对象登记也持久提交。随后驱动错误地用 Notebook 存储身份发起 controller 读取，得到 `notebook_target_changed`；产品身份校验按预期拒绝。此次未完成选择和恢复验收，记录的 owner、kernel 均已退出，清理错误为空，原参考不变。`s4a-science-v2-scope-diagnosis.json` 保留诊断；第三版只修请求身份并增加绑定后读取预检，响应仍校验 Notebook 存储身份，科学代码及阈值不变。

第三版实际运行完成了 PBMC 参考比较、Agent 对象登记/检查、101 成员选择和用户下游计算，随后仍因驱动续接条件错误停止。独立复现确认：用户接管本身会令 `continuationReady=false`，驱动却在提交 Resume 前要求它为 true。原库有对象 rev3 失效记录、用户执行 completed、Resume 零记录；原即时 detail 响应未保存，不能断言原失败的具体行。报告为 `s4a-science-v3-independent-failure-diagnosis.md`。下一版保留科学断言，按真实 App 顺序提交显式 Resume，再等待可续聊状态；inspect/remap/EOF 仍待重验。

独立微型 AnnData 实验确认：0.12.11 默认写入在文件创建失败前会将字符串列转成 category；固定关闭该转换后，对照保留 dtype 和值。这说明保存参数需要明确约束，不代表产品 checkpoint 已实现。报告使用冻结生命周期副本和主合同；实施指南在审查期间的并行更新已单独记录。

保存核心的 12 文件窄批已独立审查，暂不接纳。23 项实际 Python/R 序列化、受保护停止与持久 store 检查通过，但新增真实探针发现 P1：调用方 `Task.cancel` 结束等待后提前清除锁与保存保护，实际内核仍运行；后续普通取消可重启它并丢失内存。探针使用原十秒宽限完成复现，原日志和进程清理证据保留，报告为 `s4b-checkpoint-core-independent-review.md/json`。修复须在调用方取消后继续持有并观察真实执行，确认结束后才释放保护。该核心尚未接公开 checkpoint RPC，也未启用 capability；不能把底层测试通过当成完整保存功能。

第四版科学驱动的 Python、真实 IRkernel R 及取消登记恢复三组实际 SSH 场景已完成并通过独立核验，共八项检查、36 次外部模型请求、18 个持久完成的执行。固定 PBMC 的身份、counts 和聚类一致，PCA 最大绝对差约 5.96×10⁻⁸、UMAP 差为零，均满足原容差；DESeq2 CSV 与原参考逐字节一致。101 成员选择的内容及摘要、旧选择失效、显式重新检查与重映射、EOF 后同一 owner/内核代次/对象均已核对。两个陈旧请求被拒后没有回执或执行记录，11 份 owner/内核出生记录及断开的 controller 均已确认退出，清理错误为空。原参考、现场数据库和冻结来源不变，报告为 `s4a-science-v4-independent-results.md/json`。这验证固定科学代码经过真实模型和 SSH 的执行路径，不代表自然语言分析方法选择质量，也不包含 checkpoint、几何选择、Job 或产品导出。

五文件 CI 修复已通过独立复审，产品代码和配额未变。原五个失败用例在额外持有 600 MiB 的真实 pytest 进程中全部通过；实际子 owner 的配额拒绝、科学进程采样及 Channel 等待/清理另有七项通过，均无跳过。1430 个源码和配置文件哈希稳定。当前最终副本的完整 CI 正在运行；scripted eval 为 23 passed、8 项既有跳过，已知 v3 与当前 App 的实际 HTTP hello/approval/abort 六项兼容检查通过。63 个待合入文件已核对无工作树冲突，等待完整 CI 结果。报告为 `s4a-ci-repair-independent-review.md/json`，原 30 项失败的日志继续保留。

最终 S4a 完整 CI 已通过：9243 passed、60 skipped、77 deselected、4 xfailed、1 xpassed，898.93 秒；1430 个源码和配置文件无漂移。保留一条 `test_install_concurrency` 中 subprocess transport 析构时事件循环已关闭的警告，尚未确认其归因。命令与解释器为当前 Eval CI 的 unit 选择和 Python 3.11.15，使用本批真实 R library；完整结果为 `s4a-final-ci-source-check.json`，原失败运行未被覆盖。

整合脚本经独立只读审核后，将后端 37、App 26 项最终文件按覆盖前哈希核对合入两端。备份和记录为 `s4a-reviewed-root-integration.json`。合入后后端 43 项、App 116 项定向检查全部通过且无跳过；Web/Electron 类型通过，lint 无错误、保留一条既有 ref cleanup 警告。63 个文件前后哈希匹配，检查记录为 `s4a-reviewed-root-checks.json`。当前通过的是匹配开发组件的 S4a 对象/选择流程；用户远端旧部署仍须经过 S6 的正常组件升级验证。

保存核心的两文件修复也已独立接纳，原 P1 关闭。新的真实门闩探针确认五次调用方取消后锁、执行身份和保护仍在，普通取消不能重置内核；放行后原对象及权限上下文保留。另九项 ready callback、真实 R 停止和普通取消回归通过、无跳过。原漏洞断言脚本保留，其断言用于证明旧漏洞，不被改写成修复后的通过记录。报告为 `s4b-checkpoint-core-repair-independent-review.md/json`；公开保存、阶段屏障和整组发布尚待完整联调。

### S4b 完整保存候选与验收准备

2026-10-16，保存 candidate 1 已冻结，组合检查为 210 passed、0 skipped、273.56 秒；1443 项源码、34 项增量及 484 个组件成员核对一致。该批连接手工保存、成功阶段的保存屏障、失败重试/明确继续、整组发布及引用保留。新增来源查询曾误用不存在的 `e.started_at` 列，原失败保留；修复把实际 execution ID 与 mutation 同事务记录，并验证不会误关联提前排队的下一 cell。20 项保存集成文件的定向 lint 通过；全部 34 项的 lint 仍有 19 条，均位于逐字沿用的已接受 core/CI 测试文件，不能写成全绿。完整 CI 与独立 owner 审查仍在进行，尚未合入保存功能。

独立 owner 审查的 23 项定向检查通过，另以真实 owner/内核确认新的 P2：自动保存失败已确认停止，但存在被保存屏障挡住的排队单元时，overview 允许 Inspect，实际检查却被旧 idle 判定拒绝为 `notebook_busy`。当前候选暂不接纳；修复须只放行这一情形中的固定检查，保留运行中、停止未知及普通排队限制。原证据为 `s4b-save-candidate1-independent-queue-inspect.py/json/log`，完整 CI 继续保留该冻结版本的结果。

App 保存 candidate 1 的 38 项独立检查通过，覆盖丢回执后查询原操作、保存操作与普通执行区分、旧失败确认失效及停止未知时的限制。实际 SSH 驱动审查发现文件审计漏查 AnnData 的 X/lognorm 和 DDS 拟合结果；第二版独立验证干净 Python/R 文件通过、四类损坏被拒，关闭该验收脚本问题。原 reference、阈值和失败证据保留。第三版已固定当前 owner 组件，因上述 P2 尚未执行；修复后须另建匹配组件的驱动候选。上述结果均不代表实际 SSH 保存、产品 Restore 或浏览器保存操作已通过。

控制组件源码接近固定体积上限，另将六个基础工具模块的长篇迁移说明完整移至[工程记录](runtime-tool-design-notes.md)。独立核对确认其余源码逐字一致、执行 AST 一致，源码减少 45,112 字节；七个文件已按覆盖前哈希核对合入工作树。此整理未改变冻结的保存 candidate 1，新组件需在后续批次重新打包并验证来源。证据为 `runtime-notes-candidate1-independent-review.json` 和 `runtime-notes-root-integration.json`。

candidate 2 的五文件修复已通过独立窄审，关闭 `S4B-SAVE-R01`。五个实际场景覆盖允许固定检查的正例、普通排队、正在执行、停止未知以及真实 IRkernel 活动；另十项回归通过。正例只更新对象 revision，原失败记录、排队状态、对象身份、内核 PID 和数据保留；明确 Continue 后，排队旧代码仍被判为 `reference_stale`。作者冻结组合为 89 passed、0 skipped、147.89 秒，1444 项源码及 484 个组件成员稳定。报告为 `s4b-save-candidate2-independent-review.md/json`；第四版 SSH 驱动已单独固定该组件，尚未执行。

candidate 1 完整 CI 自然结束：13 failed、9284 passed、60 skipped、77 deselected、4 xfailed、1 xpassed，1000.18 秒，1443 项源码无漂移。失败中一项旧 `SimpleNamespace` 夹具缺少 checkpoint 服务，candidate 2 已改为真实 Store/schema，并验证保存屏障确实拒绝清除。candidate 2 完整 CI 为 12 failed、9288 passed，其他分类数量相同，1019.60 秒，1444 项源码无漂移；剩余失败均在 environment guard/helper 两个测试模块。两轮日志和源码检查分别保存在对应的 `s4b-save-candidate*-ci.log` 与 `*-ci-source-check.json`，不能把分次检查合写为全绿。

这 12 项已完成因果对照：同一冻结代码在正常进程内存下全部通过；仅向 pytest 主进程增加 600 MiB 占用后，全部因真实控制端 RSS 超过 512 MiB 被拒，helper 后续代码未执行也由此产生。两文件修复仅让进程内嵌 owner 的行为测试使用既有计量夹具；压力下 12 项通过，两个完整模块及原配额用例共 18 项通过。真实独立 owner 的冷启动约 45.6 MB，分配后约 675 MB 仍被原配额拒绝，科学子进程采样保持真实。该测试修复已冻结，正在独立窄审；产品代码和 SSH 组件未变。

第四版实际 SSH 驱动已分别完成 Python 和真实 IRkernel R 保存链，全部 13 项断言通过，清理错误为空，所记录的四个科学进程与两个 owner 均退出。两边均验证手工保存丢回执后原命令查询与重试、真实 EOF 后原内核继续、阶段自动保存，以及失败后的阻断、重试、旧确认拒绝和明确继续；每语言三个成功组均以独立进程读取文件并核对原科学标准。R 另记录已知公式环境的显式修改与重新检查，没有把修改藏在 serializer 内。持久证据正在独立核验，原科学计算未因此重跑；这些结果不等于产品 Restore 或浏览器验收完成。

实际操作耗时保存在 `s4b-checkpoint-v4-operation-metrics.json`：Python 每组 52,392,846 字节，成功操作约 15 秒；R 每组 800,640 字节，约 39 秒。操作时间包含检查、序列化、验证及发布，不能视为纯写入速度或用户目标机器的延迟保证。浏览器保存和真实目录写入失败后的重试仍待执行。S4c Restore App 的 32 项独立检查已通过，范围限界面与请求合同，后端实际恢复继续实施。

实际保存结果已获独立接受，报告为 `s4b-checkpoint-v4-results-independent-review.md/json`。六组十二个科学文件再次按冻结标准重读通过；SQLite 记录确认每语言三个 ready、两个 failed、准确的来源 execution、唯一命令 receipt、拒绝请求无执行，以及 F1→F2→明确 Continue 的完整关联。两边均保持单一内核 instance/generation，最终状态为 released，记录的原 controller、owner 和科学 PID 均已退出；74 项驱动来源、1330 项 App 源码及 484 个打包/部署源文件稳定。审计期间 SQLite 只读查询生成了协调旁文件；新增前没有预记其 inode，因此保留现场并单列副作用，只声明主数据库和科学产物内容稳定，不声明整个夹具目录未变。该结论不包含 GUI、产品 Restore、S5 或完整 CI。

两文件测试隔离修复也已独立接受：同一 600 MiB 压力下，原代码十二项失败、修复后十二项通过；真实独立 owner 仍按 512 MiB 拒绝超额，科学 PID 的 RSS 采样保持真实。最终完整 CI 已启动。额外 scripted eval 为 23 passed、8 项既有跳过，旧版 v3 和当前 App 的实际 HTTP hello/approval/abort 六项均通过，1444 项后端及 1330 项 App 源码无漂移。浏览器候选仍在审查，实际链路尚未运行。

最终完整 CI 已于 2026-10-16 完成：**9300 passed、60 skipped、77 deselected、4 xfailed、1 xpassed，无失败**，1048.37 秒；1444 个验证文件哈希不变。命令为 `/opt/conda/envs/OmicsClaw/bin/python -m pytest tests -q -p no:cacheprovider -o addopts= -m "not slow and not demo and not eval and not scripted_eval and not skill_example"`，解释器为 Python 3.11.15，基于 `b04fd5099ecb1af732bd725235e5a9bdb9c7d6fe` 加冻结 S4b 候选和已审测试修复。日志与哈希核对保存在 `s4b-final-ci.log`、`s4b-final-ci-source-check.json`。

浏览器驱动 v1 的独立审查确认 `D-S4B-B01/P2`：已取得 HTTP 状态码但正文读取失败或 owner 回执未知时，清理逻辑可能误发 Release。v1 未运行产品操作；v2 将请求是否结算与 HTTP 状态分开，未知时仅查询原命令回执，待独立复审后实际运行。这是验收脚本问题，尚无证据表明产品的未知回执处理存在同样缺陷。保存批次的工作树合入仍等待浏览器实测。

浏览器驱动 v2 的未知回执修复已通过独立 13 项边界检查与 25 项辅助检查，随后实际运行在对象登记阶段停止：UI 创建执行已完成、登记请求已接纳，但脚本立即读取尚未发布的对象，收到 `notebook_analysis_not_found` 后退出。清理看到登记执行仍在运行，按约定保留 owner、内核与内存。稍后的只读数据库核验确认两项执行均完成、对象已 verified、原创建源码仅执行一次。原现场和失败证据保留在 `s4b-browser-checkpoint-v2/checkpoint-evidence.json`，诊断在 `s4b-browser-v2-async-register-diagnosis.json`；同现场续接必须先经公开接口核原回执和终态，不能把这次运行记为保存浏览器验收通过。

浏览器 v3 经独立 9 项边界检查及 31 项辅助检查后，续接原 v2 现场，已完成三项实际检查：手工保存丢响应后刷新查询原回执；退出 Next/controller 后重连原 owner 和内核；专用账户在目录不可写时保存失败、界面禁用 Run/Release，恢复目录权限后明确 Retry 成功。随后原 unsupported cell 已执行，阶段保存已失败；脚本查询编辑器 Save 按钮时却同时匹配到 Python/R checkpoint 的同名按钮，因定位不唯一退出。尚未执行该失败的 Retry/Continue，不能记为整条浏览器流程通过。原科学进程继续保留，v4 将核原回执及终态后从该位置继续，不重放原分析和故障 cell。证据为 `/tmp/omicsclaw-notebook-acceptance-rzzq5n2v/fixture/s4b-browser-checkpoint-v3/checkpoint-evidence.json`，日志为 `s4b-checkpoint-browser-v3-run.log`。

浏览器 v4 的同现场续接脚本通过独立 13 项边界及 37 项辅助检查。实际续接完成原失败的 Retry、对新失败的明确 Continue 和 Inspect，随后修正 cell 的 Run 返回 `409 notebook_analysis_reference_stale`。请求携带 analysisRevision 57、objectRevision 6。随后 47 次公开只读查询确认当前版本为 58/7，原命令没有回执，全部 14 个执行均无该命令或修正源码；结合同步 admission CAS 路径，确认这一次 Run 在接纳前被拒。诊断为 `s4b-browser-v4-stale-diagnosis/public-precheck.json`，原 owner 和科学进程出生不变。驱动在取得该后续诊断前已停止，保留内存，没有自动重跑或 Release。本轮未完成新的验收项，v3 三项既有检查单独保留。原证据为 `/tmp/omicsclaw-notebook-acceptance-rzzq5n2v/fixture/s4b-browser-checkpoint-v4/checkpoint-evidence.json`，SHA 为 `5671e4e33a518b2834ac916d9880779b736dd0b243c55e254a5984fe655c7887`。

浏览器 v5 已在同一现场完成续接：确认原陈旧 Run 在接纳前被拒，页面取得 analysisRevision 58 / objectRevision 7 后，以新命令明确执行一次修正，自动 checkpoint 达到 ready、mutationSeq 为 3。原创建与故障源码各保持一次。随后实际 UI Release 结束原两个科学进程，owner 也按已核出生退出，清理错误为空。新增两项检查与此前三项分开保存，原 v2/v3/v4 失败证据保留。结果为 `s4b-browser-checkpoint-v5/checkpoint-evidence.json`，SHA `9b9337064985e9b8f496889acccfac2720883a6f558916a2c9f290b0cfa5b08a`；实际证据已交独立核验，保存批次尚未合入。

浏览器实际证据链已获独立接纳，报告为 `s4b-checkpoint-browser-v5-results-independent-review.md/json`。18 条持久执行与公开历史一致，三个成功 H5AD 的矩阵、counts、轴及文件摘要均通过独立读取。保存记录、失败记录及 hold 状态一致，原进程的 native closure 包含 ECHILD/childrenEmpty；该闭包记录的 exitCode 为 -9，未宣称科学进程自然退出。审计在写者退出后使用只读 immutable SQLite，43 个现场文件的内容、权限和 inode 前后相同，没有新增 WAL。

2026-10-16，S4b 的 57 项已审文件按覆盖前 SHA 合入两端工作树，其中 52 项写入、5 项已相同，备份及记录为 `s4b-reviewed-root-integration.json`。合入后的后端六模块共 48 passed，App 十二测试文件共 127 passed，均无跳过；Web/Electron 类型通过，lint 无错误并保留一条既有 ref cleanup 警告。57 项来源不变，结果为 `s4b-reviewed-root-checks.json`（SHA `b1ed45860a11515ea0d0e25a0445600b89de8226452aa019afa91eda9a175e31`）。该批完成匹配开发组件的保存流程，Restore 和用户正常连接时的发布组件升级仍单独验收。

### S4c 显式 Restore 候选

2026-10-16，后端 candidate 1 已冻结为 15 项增量、1451 项完整来源。组件包含 487 个文件，源码 4,690,895 字节，归档 1,643,061 字节，均未超过原预算。定向检查已完成 Python AnnData/结果表和真实 R DDS/结果表的整组保存、释放、新代恢复及后续计算；环境关键版本不一致、持久 unknown 不重派、旧选择失效、显式 Remap 和受保护加载的明确 Release 另有边界覆盖。这些结果尚不等于实际 App/OpenSSH Restore 验收通过。

冻结组合为 244 passed、1 failed、426.14 秒，1451 项来源无漂移。唯一失败是旧测试要求 Restore capability 不存在。独立测试修复候选将该断言改为能力存在，并经真实 Controller 接口查询空 Restore overview、两语言的资格与运行状态、空操作分页；作者 27 项定向检查通过，独立复跑该 RPC 用例通过，单测试修复已接纳。产品代码未因该测试修复改变。原失败记录为 `s4c-restore-candidate1-validation.json`，测试候选为 `s4c-capability-test-repair-manifest.json`。

candidate 1 完整 CI 于 2026-10-16 结束：1 failed、9326 passed、60 skipped、77 deselected、4 xfailed、1 xpassed，1185.19 秒，1451 项来源无漂移。唯一失败仍是上述旧能力断言。采用 Python 3.11.15、前文相同的 Eval CI unit 命令和显式 R library；基线为 `b04fd5099ecb1af732bd725235e5a9bdb9c7d6fe` 加冻结 S4c c1 及 manifest 所列既有修复。记录为 `s4c-restore-candidate1-ci-source-check.json`，SHA 为 `7ec556950fa5b968a80d6acb61c4d894818fd3734132a0dec7cb905b4b98caf9`。该结果不包含随后 R01 修复，最终组合仍需完整回归。

独立审查已验证实际整组加载、内存提交完成但 SQL 发布失败时，事务回滚不暴露部分 ObjectRefs，原 gate 和 source hold 保留，同命令查询不重复加载，明确 Release 后才清除相应保护。另一个私有探针发现换代遗漏：失败目标进程结束并被标记 lost 后，公开 Bind 成功创建 generation 3，overview 的 activeOperationId 消失，continuationReady 变为 true，旧 gate/hold 却尚未解除。该候选暂不接纳；修复须让持久阻断跨内核换代继续生效，Bind 在同一事务内检查，并保留针对原目标的明确 Release 路径。首轮探针的后续 Run 因未提供新的 analysis preconditions 被另一个守卫拒绝，因此该证据仅证明 Bind 与状态投影问题，不能声称实际 Run 已越过阻断。原脚本和结果保存在 `s4c-restore-candidate1-independent/`。

后续 `lost-gate-bypass-v2.py/json/log` 在新的私有工作区使用当前 v3 analysis guard，实际 Run 已完成并输出 `NEW_GENERATION_DESPITE_RESTORE_GATE`。旧 operation 仍为 gateActive=true、targetReleased=false，source hold 为 1；对旧目标的明确 Release 返回 `notebook_generation_changed`。独立审查据此确认 `S4C-R01/P2`。25 项相关回归及发布回滚正例通过不能关闭该问题，修复另批进行。

candidate 2 的三个文件已通过独立窄审，R01 关闭。独立探针实际加载两个对象、提交内存后注入 SQL 发布失败，再确认原目标退出并进入 lost；新 Bind 和带当前 guard 的 Run 均被拒，错误代次 Release 也被拒。精确原目标 Release 确认协调记录闭合后，才清除对应 gate/hold，并允许新代实际 Run。同命令回执未重复加载。四项新回归独立复跑通过，包含真实 R 和根进程退出但无闭包证明的 `stop_unconfirmed` 负例；三份自有科学进程出生均确认退出。1452 项完整来源及 487 个组件成员匹配，报告为 `s4c-restore-candidate2-independent-review.md/json`。最终完整 CI 正在运行，App 失联目标的显式释放入口仍单独修复，实际 SSH 科学 Restore 尚未执行。

最终组合完整 CI 于 2026-10-16 通过：9331 passed、60 skipped、77 deselected、4 xfailed、1 xpassed，1240.76 秒，无失败；1452 项来源前后不变。Python 3.11.15 与 Eval CI unit 命令保持一致，验证基线为 `b04fd5099ecb1af732bd725235e5a9bdb9c7d6fe` 加冻结 c2 和其列明的已审测试修复，不含后续维护实现。结果为 `s4c-final-ci-source-check.json`（SHA `ebb2850f4647ab3fd37902e4fd85a66ac3a476554d47219c1ab27c1ced4dba12`），日志为 `s4c-final-ci.log`（SHA `74ca2509f3262dfbf313caa944d581d4e5b70c8b6b0c0de8dd2526da4c792412`）。实际 SSH 科学恢复和 App 入口仍须单独验收。

App 的失联目标释放 candidate 1 已关闭原入口缺失，但补充独审发现 `S4C-APP-R02/P2`：旧回执查询在途时，另一窗口写入新 operation/新代的待定 Release；旧回执返回后，本地 hook 短暂清空待定状态。真实 Surface 测试确认此时 Prepare 被启用、原命令查询入口消失。31 项既有检查及四项新增边界通过，两条独立红测保留；尚未实际发送错误 Bind/Run 或后端释放。候选暂不接纳，作者在新副本中修复跨窗口同步和完整意图匹配。报告为 `s4c-app-release-repair-candidate1-independent-review.md/json`，1337 项来源无漂移。

App candidate 2 的两文件修复已通过独立复审，`S4C-APP-R02` 关闭。原五条 hook 探针与同路径 Surface 正向检查共六项通过，冻结 UI 三十项通过，均无跳过。新 pending 保持可见，Prepare 保持禁用；同 command 但不同完整目标、无 Web Locks 观察也已覆盖。1337 项来源稳定，原负向脚本与红测证据保留。报告为 `s4c-app-release-repair-candidate2-independent-review.md/json`。最终科学和浏览器驱动已固定对应来源并完成本地准备，实际恢复仍待执行。

实际 SSH Restore 驱动 v1 已通过独立审查，但仍禁用，等待接纳后的组件。独立 10 项控制边界及五项科学校验器检查通过；正常 Python/R 对象通过，X、lognorm 与 DDS 系数损坏被拒。R 的计数文件只能证明本场景顺序执行未重放，不声明并发原子独占。该驱动未启动产品 Restore，也未执行模型请求。

第二批历史说明整理也已通过独立核验并合入工作树：30 个模块的初始介绍完整移至[框架设计记录](runtime-framework-design-notes.md)，31 个候选文件逐项核 SHA，可执行后缀逐字一致、执行 AST 一致，源码减少 89,043 字节。该变化会改变模块 `__doc__` 和源码行号。基于 S4c c1 的静态大小投影为 4,601,852 字节，尚未据此构建新组件；冻结 S4b/S4c 组件、完整 CI 副本均未改。证据为 `runtime-notes-candidate2-independent-review.json` 与 `runtime-notes-candidate2-root-integration.json`。

## 8. S5 重建材料预检

独立审计发现原科学环境的安装元数据与实际运行版本有差异：NumPy 实际为 2.0.2，conda 记录仍为 1.26.4；numba、llvmlite、psutil 还存在旧版本元数据。重建材料按实际模块来源选择，不能直接复制环境目录或只导出第一份版本记录。已逐字核对 NumPy wheel 与实际核心文件，以及 llvmlite conda 二进制经前缀重定位后的文件；审计和限制见 `s5-materials-independent-review.md`。

当前已固定 358 个材料归档，共 866,434,274 字节：原闭包的 351 个 conda 包、额外 pkg-config、五个 R 源码包及 NumPy 覆盖 wheel。所有材料已重新核 SHA256。GenomeInfoDbData 1.2.11 从 [GWDG 的 Bioconductor 3.18 源](https://ftp.gwdg.de/pub/misc/bioconductor/packages/3.18/data/annotation/html/GenomeInfoDbData.html) 获取，MD5 与原 conda 固定的 `dataURLs.json` 一致。原下载包装器会在 post-link 内联网，因此保留它作来源证据，新环境改为显式安装同版本 R 源码；清单明确记录这项安装器差异。其余四个 R 源码版本、文件大小与原安装日志一致。

材料清单为 `s5-preflight-material-lock.json`。专用账户的新目录已完成离线预检重建：使用本地精确归档，网络 namespace 验证无路由，八个身份/安装步骤全部退出 0。独立复核确认 351 条 conda 元数据与选定归档一致，70,958 个普通文件没有多硬链接，2,387 个软链接均解析在新前缀内；Python/R 的实际科学动态库也来自新环境。报告为 `s5-rebuild-independent-review.md/json`，结论仅接纳环境预检。

在新目录逐字运行原参考脚本，PBMC 与 Rscript DESeq2 均通过。对照预先固定的标准，细胞/基因身份和 counts 精确一致，聚类 ARI=1，UMAP 最大差为 0，PCA 最大绝对差约 2.91×10⁻¹¹；1201 基因的 DESeq2 CSV 与原参考逐字节一致。证据为 `s5-reference-preflight-result.json` 和 `s5-reference-preflight-comparison.json`。这只是独立环境的可行性预检，尚未完成产品导出包、Notebook/真实 IRkernel 和新 Job 的复跑，不能据此关闭 S5。

随后以实际 UID 999 调用冻结组件的完整 `probe_environment`，保留默认 15 秒限制：Python 3.11.15 返回 86 个包、R 4.3.3 返回 103 个包，两者缺失项均为空。证据为 `s5-rebuild-registered-probe-preflight.json`。该调用验证登记前探测，不会创建 Notebook 或执行科学分析。真实 SSH 科学验收驱动首轮在组件目录布局核对时停止，尚未启动探测、SSH 或模型；原日志保留，部署容器修正另行冻结。

## 9. 完整交付的判定

最终逐项核对主方案 A01–A18 及三条科学主线。自动检查与需要用户在实际远端/Mac 上执行的检查分开列出，记录失败、跳过和未运行项。真实模型的请求处理质量也单独记录，脚本化 provider 通过不能替代它。

匹配开发组件已支持 SSH Python/R 持久交互及无对象环境维护。组学恢复、带对象维护和复现仍在 S4–S6，用户不能仅修改一个开关便获得尚未交付的完整功能。

### S4c 首轮实测停止与 S4d 迟到回执修复

Restore 的首轮实际 Python/R 驱动均在外部模型调用阶段收到 `ProviderError`，尚未执行 Restore。Python 的原科学 execution 已完成，产物和内核保留；R 尚无 tool 或 Notebook execution。原失败证据 `s4c-restore-v2-python-evidence.json`、`s4c-restore-v2-r-evidence.json` 保持不变，后续从原会话续接，不重放已完成的 Python 分析。具体供应商失败原因仍在排查，不能将该错误归因于 Restore。

浏览器已完成 Save、源代 Release 和未执行目标 Prepare，随后因 `Saved group` 精确 label 定位失败停止，原证据中没有 Restore POST。v3 改用恢复面板内唯一 combobox，并在连接后核对原回执、全部执行记录和未使用目标。16 项续接约束检查及真实 Chromium 静态 DOM 定位检查通过；完整 UI 续接须先通过独立驱动审查。它保留原证据、保存组和 owner，禁止重发源分析、Save 或 Prepare。

S4d 核心审查发现全局维护记录停在 unknown、原本地 prepare 随后成功时，迟到回执无法把空 operationId 收敛为原唯一操作。c2 限定该首次收敛的完整身份，并继续拒绝非空替换和状态倒退。独立真实 owner 探针验证原 prepare、intent、hold 各一条，最终 ready/stopped 后只清除本次 hold；10 项定向测试及 6 项非法形状负例通过。报告为 `s4d-checkpoint-core-candidate2-independent-review.md`。该结果只覆盖内部保存协调，公开审批、全部 owner 保存后停止及安装链继续验收。

### S4c 浏览器实际恢复通过，模型 API 连接失败单列

浏览器 v3 在纯读预检中错误地把四字段 binding 状态与两字段 Bind 回执比较，未提交任何新写入。修正后以真实读取投影独立核验，v4 已实际通过 UI 整组 Restore、丢失应用响应后的原回执恢复、真实 Next/controller EOF 后同代重连、AnnData 读取和修改，以及显式 Release。保存文件保持原哈希，两代科学进程与 owner 均确认退出，cleanupErrors 为空。证据为 `s4c-browser-restore-v4/restore-evidence.json`；结果独立审查待完成。v2/v3 原失败及 v3 审查遗漏说明均保留。

Python 科学 v4 续接确认原科学 execution 已完成且内核存活，跳过原 recipe 后完成对象准备。随后的外部模型注册请求连续六次 `UND_ERR_CONNECT_TIMEOUT`，因此整次仍记为失败并保留内核。无凭据的 HEAD 检查显示当前主机直连、环境 HTTP 代理及 SOCKS 远端 DNS 到模型 API 均失败，另两个公共站点可达；这只证明当前主机的 API 连接问题，不代表供应商全面故障。诊断为 `s4c-provider-transport-diagnosis-v1.json`。R 新续接及 owner 故障注入尚未启动。

S4e App 的独立 Chromium 探针发现，iframe 即使设置现有 CSP 和 sandbox，仍能通过自导航向探针自己的 loopback 服务发送请求。候选尚不接纳；修复需验证导航、资源请求和 SDK 通信各自的约束。该探针使用合成标记，未读取或外送用户数据。


### S4c 浏览器结果已独立核验，科学续接改用显式用户入口

`s4c-browser-v4-results-independent-review.md/json` 已接受本轮小型 AnnData 界面恢复。核对覆盖 528 个驱动依赖、1337 个 App 文件和 487 个组件文件；八次执行均持久完成，原源分析仅一次。唯一 Restore 操作最终为 restored，gate、hold 和预留空间均清零；新对象保留原 DatasetRef，三个保存文件哈希不变。独立重读 HDF5 确认 X、counts 和全部行列身份符合原数据。两代四个科学进程、owner、两次 Next 及旧 controller 均已退出。这不替代 PBMC、真实 R、多对象恢复、owner 故障或发布包验收。

科学 v5 驱动沿原 Python/R 会话，通过公开 User Run 和 metadata 继续：Python 的原 recipe 与已完成 prepare 都先核对后跳过；R 必须先证明公开历史为空，才首次运行原冻结代码。全部科学比较、丢回执与 EOF、旧选择阻断、继续编辑和文件不变断言保留。24 项续接约束检查通过，驱动已送独立审查，尚未实跑。模型传输被显式禁止，成功也只计为用户入口 Restore；原模型连接失败证据保持不变。

S4d 公开维护的 43 项组合检查通过，完整 CI 正在运行。S4e App 导航修复 c3 已冻结，作者的 Chromium 和类型检查通过，原 P1 仍须独立复验后才能关闭。


R v5 实际运行已完成七项检查：真实 IRkernel 的固定 DESeq2 分析、整组保存审计、丢回执后 Restore 与 EOF、引用换代及旧选择阻断、继续读取与编辑均通过；模型请求为零，清理错误为空。结果待独立科学复核。Python v5 在纯读预检错误地要求 snapshot 无下一页，未提交任何新命令；产品每页至多一条执行，现有两条历史需要翻页。Python-only v6 保留原两执行总集和全部科学标准，补齐有界公开分页；34 项检查通过，原失败与审查报告保留。

S4e c3 的独立导航审查被自动系统标记为可能涉及网络安全风险而中止，相关审查已停止，已有日志保留，尚无接纳结论。S4d 完整 CI 在约 34% 出现失败，继续等待原进程自然完成和具体 trace；暂不把先前定向通过扩大为全量通过。


Python 分页 v6 在实跑前被独立审查拒绝：公开 Notebook cursor 是带 store/scope/after 的对象，字符串假设不成立。v7 改用完整对象，并用冻结产品分页函数生成的两页记录验证；独立 16 项分页检查、4 项正例及 10 项负例通过后，沿原场景实际完成七项科学恢复检查。Python/R 两组均模型请求为零、原科学来源不变、无清理错误，正在进行合并结果审计。原 v5/v6 失败及两份审查补充说明保留。

### 低额度 AnnData 保存缺口

Job 实现中的负例暴露出旧保存核心问题：在隔离科学子进程中，对稀疏 AnnData（含 layers、UMAP、raw）使用 64、256、512、1024、4096 字节保存额度，五次均以 SIGSEGV 退出，未达到保存失败后继续读取内存的断言。最小复现直接调用原 S4d 冻结副本的 notebook_checkpoint.save，不导入 Job 新模块；结果为 `s4f-hdf5-budget-diagnosis/evidence.json`。现有 Notebook 会话未受影响，但此前成功场景不能证明这一失败边界安全。问题已交独立确认，最终交付前须修复且验证原内存仍可用，不能用更高额度替代。

S4d 完整 CI 最终为 13 failed、9345 passed、60 skipped、77 deselected、4 xfailed、1 xpassed，1356.91 秒；1460 个验证源码文件前后未变。失败均在控制进程 RSS 配额处，作者的额外 600 MiB pytest 压力实验复现原 13 项，冷进程对照和独立复核仍在进行。暂未修改产品配额，也未接纳该批。

### 科学恢复审计与故障边界续验

`s4c-science-v7-python-v5-r-independent-results-review.md/json` 已接纳 Python v7 与 R v5 的显式用户入口恢复，共十四项检查。原 Python recipe 和 prepare 各一次；R 首次通过真实 IRkernel 执行固定 DESeq2 配方。十条 Python 和十三条 R execution 均持久完成，旧选择请求在接纳前被拒绝，明确 Clear 后继续编辑；原保存文件和科学参考保持不变。最后 gate、hold、预留空间均清零，相关科学进程和 owner 已退出。

科学驱动的 controller EOF 发生于目标仍 reserved、尚无科学进程时，只证明 owner 和目标代次延续；活跃内核 EOF 由前述浏览器及 S1 证据覆盖。外部模型连接失败仍单列，显式用户入口通过不能替代模型续接验收。owner 故障驱动 v5 仅更新上述已成功前置证据，经独立复核后开始首次实际运行，尚未据此接纳 S4c 合入。

`s4b-hdf5-budget-independent-review.md/json` 确认 `S4B-HDF5-QUOTA-R01/P1`：新私有子进程在 4096 字节限额下自然以 SIGSEGV 退出，栈位于 HDF5 fileobj 关闭路径，原对象检查未执行到。根树、已接纳 S4b 与冻结 S4d 的序列化源码相同；没有对用户会话注入该故障。具体 C 层失效生命周期仍待定位。修复必须同时保留真实物理额度与原科学进程内存，并补验实际 owner 的同代继续读取、失败记录和显式重试。

S4d 的十三项失败在冷进程全部通过，增加 600 MiB pytest 驻留后全部复现；三份测试文件改用已有控制 RSS 夹具后，同压力检查全部通过。产品源码及配额未改。独立审查和最终完整 CI 尚未完成。

独立复核已接纳上述三文件测试修正：额外 600 MiB 驻留下抽验三项通过，真实独立 owner 超过原 512 MiB 配额仍被拒绝，真实 NumPy kernel 的 RSS 采样未替换。证据为 `s4d-ci-resource-repair-independent-review.md/json`。最终完整 CI 待保存核心修复是否合并后再运行，当前仍不声明该批全量通过。

owner 故障 v5 在观察器启动时停止：所用 conda Python 没有 `os.pidfd_open`，尚未 armed，也未提交 Restore 或发出信号。原科学分析和 Save 已完成，源代已明确释放，owner 与未使用的目标代次保留。只读检查确认系统 Python 的标准库具备所需 pidfd API；v6 固定该解释器及原失败证据，从同一现场核七条完整执行、ready 保存组和 Restore 尚无回执后续接。该候选仍待独立审查，不重复原分析、登记、保存或代次切换。


### 2026-10-16：Restore owner 故障现场续验

原 v9 驱动在真实 Restore 的 `loading` 阶段确认目标进程出生身份后，终止了该私有夹具的原 owner。新 controller 和新 owner 找回原回执；Restore 保留为 `unknown`，阻断仍生效，源 checkpoint hold 未释放，没有发布恢复对象。驱动随后因状态列表漏掉合法的 `unconfirmed` 而停止，原现场保留。独立审查核对了冻结代码和只读数据库，确认这是驱动断言问题；`unconfirmed` 不表示内核已重新附着或可以执行。

v10 经独立审查后续接了原现场：原回执、8 条执行、unknown/gate/hold 均不变；Bind 被 `notebook_checkpoint_blocked` 拒绝，Run 则更早被 `environment_maintenance_unconfirmed` 拒绝。两条命令均没有回执或新执行。驱动原先只接受后一层的 checkpoint 错误码，因此在 Release 前停止。独立只读核验确认 Run 的实际 guard 有效；原内核使用者记录的 `uncertain_exit=1` 先触发环境限制。该 Run 只能证明环境状态不确定时拒绝执行。v11 查询原拒绝结果后继续明确 Release 和新代执行，不重复注入故障、原分析、保存或拒绝请求。

带对象维护的独立检查另确认：环境版本提交成功后、原 prepare 清理前发生重启，会留下阻止 Bind 的本地维护记录和 hold。该 P2 修复独立提交；此前完整 CI 的 13 项资源夹具修正已独立接纳，最终组合仍须重新运行完整 CI。


v11 实际续验完成了原回执、原拒绝命令和保存文件的复核，随后明确 Release 返回 `notebook_checkpoint_stop_unconfirmed`。原监督进程已退出，原 ipykernel 仍活着，新 owner 也保留。系统没有假称整个 Restore 已停止；这次是产品进程回收路径的缺口，不能通过放宽驱动断言关闭。原场景保持原样，等待独立诊断及窄修。已退出监督进程的现场若无法再证明整个进程闭包，必须保留红例，在修复后的新私有夹具验证正确释放，不得补写关闭凭证或清除 gate 来得到通过。
