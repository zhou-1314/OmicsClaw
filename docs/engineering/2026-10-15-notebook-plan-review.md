# SSH controller Notebook 方案独立审查

日期：2026-10-15。审查目标：在 OmicsClaw-App 的 SSH controller 路径中提供 Claude Science 式 Notebook，使分析记录、内核状态和用户操作能支持连续组学分析。本报告保留访谈前的审查结论；方案链接已更新为现行章节导航，问题描述仍针对访谈前版本。后续用户确认的要求以原方案第 12 节为准。

审查结论：原方案可以保留为现状调查和工作清单，但还不足以直接作为完整实现规格。SSH 关闭点、表达式输出、helper 恢复和导出等诊断方向成立。实现前应补充环境接入、生命周期、用户介入后的状态校验和组学数据契约，并调整阶段依赖。单独完成 M0/M1 只能证明 Python 接入可用。

本次没有修改产品代码，也没有重新运行原报告中的 65 项后端测试和 27 项 App 测试。以下“已验证”指本次独立阅读当前源码所得；未启动原版 Claude Science，未连接用户实际服务器，也未运行真实 App。两个仓库 HEAD 仍分别为 `b04fd5099ecb1af732bd725235e5a9bdb9c7d6fe` 和 `48a226f5303f270135a0afe40f06178a30ab7790`。审查保留原方案和已有工作区文件。

审查中的 P1 表示会影响目标交付的设计缺口，P2 表示需要补齐的验收或规格细节，不表示已发现相同等级的安全漏洞。断线保留全部内存、Slurm 全覆盖、任意环境热切换均未被用户逐项指定，不能视为首版必选项。

## 1. P1：科学环境的实际接入入口缺失，默认启用条件仍无法落地

方案位置：[现行方案第 182 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:182)、[现行方案第 255 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:255)、[现行方案第 398 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:398)。

已验证：controller 的科学部署不是一个任意 Python 路径。`verified_science_deployment` 要求带哈希的环境目录、`ready.json`、manifest、requirements lock 和技能 payload；Python 固定为 `environment_root/venv/bin/python`。App 的 `science` 是可选部署项，configure 仅在该项存在时传入。当前 App 运行时准备脚本检查和安装的控制依赖不含 `jupyter_client`；后端 `full.in` 才声明它。科学环境有 ipykernel 不能证明控制环境有 Notebook client。

证据：[科学部署验证](/workspace/dataset/private/zhouwg_data/OmicsClaw/omicsclaw/entry/ssh/science_deployment.py:62)、[App configure](/workspace/dataset/private/zhouwg_data/OmicsClaw-App/src/lib/controller/connection.ts:74)、[控制运行时准备](/workspace/dataset/private/zhouwg_data/OmicsClaw-App/src/lib/controller/provision.ts:170)、[Notebook 控制依赖](/workspace/dataset/private/zhouwg_data/OmicsClaw/omicsclaw/entry/ssh/profiles/full.in:1)。

失败方式：开发者把 `science.python` 接到 kernel manager 后，用户已有 conda 分析环境仍不能通过部署验证；或者 science 可用、控制 client 缺失，第一次执行才失败。用户继续看到“未启用”，只多了一个准确错误。

建议修改：在 M0/M1 前增加可执行的部署契约，明确首版接受现有受管 science publication，还是支持登记用户现有 conda/venv。后者须单独定义环境证据和更新行为，不能跳过现有验证。列清 App 配置入口、控制 profile 的安装与验证、science 的 ipykernel/核心包预检、失败后的修复操作、重启后的配置保存。自动安装全部科学环境可以后置，但必须提供一条用户可完成的预配置路径。

待决策：首版环境来源；是否允许现有可变 conda；变更包后是拒绝执行、更新指纹还是建立新环境版本。无需先实现通用环境管理器。

## 2. P1：M1 依赖的内核代次、环境身份和断线契约被排在后续

方案位置：[现行方案第 234 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:234)、[现行方案第 295 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:295)、[现行方案第 398 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:398)。

已验证：controller EOF 调用 `app.aclose()`；Notebook close 取消其任务，kernel manager close 关闭所有内核。Notebook 的持久 owner 是带 30 秒租约的 controller 内服务，恢复逻辑会将 owner 丢失的执行改为 interrupted。当前 kernel state 的键没有 environment identity，snapshot 也没有稳定 kernel generation。Notebook 内核另有 30 分钟空闲回收，以及年龄和内存相关回收规则。

证据：[controller 关闭](/workspace/dataset/private/zhouwg_data/OmicsClaw/omicsclaw/entry/ssh/controller.py:914)、[Notebook 关闭](/workspace/dataset/private/zhouwg_data/OmicsClaw/omicsclaw/entry/desktop/notebooks_manager.py:743)、[owner 恢复](/workspace/dataset/private/zhouwg_data/OmicsClaw/omicsclaw/entry/desktop/notebooks_manager.py:245)、[kernel state](/workspace/dataset/private/zhouwg_data/OmicsClaw/omicsclaw/kernel/manager.py:169)、[回收策略](/workspace/dataset/private/zhouwg_data/OmicsClaw/omicsclaw/kernel/reaper.py:37)。

失败方式：M1 宣称取消、重连、重试正确时，还没有 M4 的 generation 和 M6 的环境身份。后续选择断线保活，必须重做 owner、取消、恢复和执行终态，而不能只把内核进程留在后台。即使 SSH 正常，用户看图讨论超过空闲期限也可能失去昂贵对象；只讨论 EOF 会漏掉这条路径。

建议修改：把不可变环境身份、kernel generation、执行命令身份和生命周期决策前移到 M1 之前。至少写出“App 断开、SSH EOF、controller SIGKILL、内核死亡、用户 reset、空闲回收”各自对任务、变量、历史、审批和下一次执行的影响。持久记录要区分执行已终止、仅失去观察和结果待核对。

推荐契约：先允许 controller 有限生命周期，提供准确的丢失提示、经过验证的显式 checkpoint 恢复和明确的空闲回收设置。该版本可交付受限的连续分析体验。若目标包含断线后昂贵交互状态继续存活，再增加可重新附着的受管内核所有者和租约，单独验收；SSH 保活或 tmux 不能替代它。Slurm 不在这一前置决策内。

## 3. P1：用户修改通知没有阻止 Agent 基于旧状态继续执行

方案位置：[现行方案第 109 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:109)、[现行方案第 180 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:180)、[现行方案第 329 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:329)。

已验证：现有 Notebook 通过 `_idle` 禁止同时执行，cell CAS 校验源代码修订；这些机制没有校验模型所见的内核状态水位。用户可以在模型推理或审批等待期间执行另一单元，之后模型生成的代码仍可进入同一 generation。Claude Science terminal 除了预期内核身份，还等待 Agent bracket 释放；Agent 会收到用户执行、停止和清空通知。

证据：[Notebook admission](/workspace/dataset/private/zhouwg_data/OmicsClaw/omicsclaw/entry/desktop/notebooks_manager.py:369)、[Agent cell 提交](/workspace/dataset/private/zhouwg_data/OmicsClaw/omicsclaw/entry/desktop/notebooks_manager.py:157)、[Claude Science 用户执行协调](/workspace/dataset/private/zhouwg_data/逆向工程-bioAgent/work/claude-science/src-recovered/claude-science.pretty.js:373868)、[用户操作通知](/workspace/dataset/private/zhouwg_data/逆向工程-bioAgent/work/claude-science/src-recovered/claude-science.pretty.js:243460)。

失败方式：Agent 准备在某批细胞上做 DE，用户先替换 `adata`、改变筛选或 layer；下一段代码仍可能成功，却分析了另一份对象。只在下一次模型请求刷新提示，不能覆盖模型输出至 admission 之间的变化。

建议修改：增加服务端执行水位和用户变更水位，并规定在哪个时刻捕获、何时比较。可选最小方案是在 Agent admission 前比较它所依据的 user-state watermark；变化时拒绝本次旧状态执行，要求模型刷新上下文。另一方案是短期的分析执行 bracket，明确用户提交排队和取消规则。不要锁住整轮聊天或无限等待审批。运行中断后可能已改变的对象也要标记“需重新检查”，不以同一 generation 推断状态未变。

验收补充：在模型推理中、等待审批中和两个 Agent cell 之间分别插入用户修改；在 reset 后提交旧 Run；保证旧代码不会悄悄作用于新对象。

## 4. P1：组学数据与选择状态缺少稳定身份，科学分析可错误地“成功”

方案位置：[现行方案第 380 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:380)、[现行方案第 329 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:329)、[现行方案第 443 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:443)。

已验证：当前 Live state 校验 app revision 与 execution，却只给选择项提供通用字符串 ID，并限制至 100 项。现有 AnnData handle 的缓存 revision 是 `(mtime_ns, size)`，服务于 `code_run` 的 h5ad 同步；它不等于内存对象每次原地变更的版本，也没有构成 Notebook 的通用数据契约。现行 `sc-de` 明确区分 donor/sample 统计单位、counts 和 log-normalized 矩阵，说明这些信息已经影响方法选择。

证据：[selection 限制和来源校验](/workspace/dataset/private/zhouwg_data/OmicsClaw/omicsclaw/entry/notebook_context.py:90)、[AnnData handle](/workspace/dataset/private/zhouwg_data/OmicsClaw/omicsclaw/kernel/handles.py:95)、[DE 方法和输入要求](/workspace/dataset/private/zhouwg_data/OmicsClaw/skills/singlecell/scrna/sc-de/SKILL.md:19)。

失败方式：用户在旧 UMAP 上选中细胞后，Agent 已筛选或重排对象；同一 app revision 不足以说明选中 ID 仍指向当前数据。一次圈选数百细胞会超出现有接口。shape 相同的两个对象也可能来自不同样本、物种或处理阶段，通用变量摘要无法区分。

建议修改：为首批分析对象增加轻量、显式登记的 dataset reference，至少包括来源、版本、obs/var 身份、使用的矩阵或 assay/layer、样本与条件字段、相关处理状态和生成 execution。选择状态携带 dataset version、object revision、obs 身份、数量及服务端 `selection_id`；数千或数万细胞的集合保存为有界文件或服务端记录，模型仅接收统计摘要和有界样本。对象变更后旧选择应失效或要求显式映射。无需放宽现有 JSON 数组上限。

这不要求分析任意 Python 的全部副作用。采用技能帮助函数或显式登记动作；无法判断时写“未知”，再检查关键字段。首先支持 AnnData 和表格/RDS 的文件引用即可，不必建立覆盖全部组学格式的对象系统。

验收补充：重排细胞、重复 barcode 跨样本、切 counts/log layer、缺失 donor、旧图圈选、大集合选择。测试既检查结果也检查拒绝或提示行为。大对象路径明确禁止默认 densify、全量 `repr` 或每轮重复 h5ad 写回。

## 5. P1：长任务与 R 的交付边界太晚确定，无法保证代表性组学请求闭环

方案位置：[现行方案第 190 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:190)、[现行方案第 398 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:398)、[现行方案第 398 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:398)。

已验证：当前 `code_run` 使用聊天 session id 调用 manager，Notebook 常使用 `notebook:<id>`。controller 的 JobsManager 关闭默认 runners，实际注册的是科学 `skill_run`，因此“引导到 code_run”不是现有 SSH 下已具备的替代方案。IRkernel 当前从默认 kernelspec 目录寻找 `ir`，并不使用 science deployment 的 Rscript 及 library 配置。

证据：[code_run](/workspace/dataset/private/zhouwg_data/OmicsClaw/omicsclaw/entry/desktop/jobs_manager.py:1234)、[controller runner 装配](/workspace/dataset/private/zhouwg_data/OmicsClaw/omicsclaw/entry/ssh/controller.py:345)、[IRkernel 选择](/workspace/dataset/private/zhouwg_data/OmicsClaw/omicsclaw/kernel/session.py:249)、[科学 R 配置](/workspace/dataset/private/zhouwg_data/OmicsClaw/omicsclaw/entry/ssh/science_deployment.py:135)。

失败方式：Python 交互可用后，耗时步骤仍离开 Notebook，得到的 Job 没有对象状态交接；下一步读不到结果或错误地使用旧 `adata`。仅把 R 暴露到 Notebook UI，可能从另一份 R 安装和 library 启动，已有科学部署证据无法解释运行结果。

建议修改：把“长任务如何接回分析”前移到完整交付边界。定义两个明确模式：共享同一内核的长 cell；隔离进程的有限 Job。后者通过 immutable input checkpoint、job id、结果 manifest 和显式加载回到 Notebook，不能声称共享变量。分别定义观察超时、执行限时、取消和断线的行为。现有 Jobs 继续拥有作业状态，Notebook 只引用和投影。

R 应先声明支持等级：首版可选择 Python Notebook 加已配置 Rscript 作业回接；若承诺 Claude Science 式 Python/R 交互，再做 IRkernel、环境指纹和独立 namespace 的薄实现。两者均无需现在引入 `kernel.R` sidecar 体系。多目标 HPC、Slurm、任意环境热切换可后置，但其缺失要在能力面明确。

待决策：用户常用的首批请求是否必须使用 R 交互；最长交互执行预算；哪类任务必须脱离 controller 生命周期。不能仅凭“底层有 R”认定支持，也不应据此要求首版支持所有 R 工作流。

## 6. P1：按执行顺序导出没有定义跨代次和失败执行的重放规则

方案位置：[现行方案第 380 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:380)、[现行方案第 398 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:398)、[现行方案第 420 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:420)。

已验证：当前执行 provenance 保留实际代码，具备生成历史导出的基础。Claude Science bundle 按 frame 的 segments 导出，每段记录 kernel id、language 和 environment，分别写 Notebook 和环境文件。原方案只要求按实际顺序和恢复 helper，没有规定这种分段及恢复依赖。

证据：[实际执行代码](/workspace/dataset/private/zhouwg_data/OmicsClaw/omicsclaw/entry/desktop/notebooks_manager.py:435)、[Claude Science 分段 bundle](/workspace/dataset/private/zhouwg_data/逆向工程-bioAgent/work/claude-science/src-recovered/claude-science.pretty.js:342943)。

失败方式：先执行 Python，reset 后重建变量，再执行 R 或从 checkpoint 载入；把所有历史 cell 依次拼接，会把几个独立命名空间混在一起。失败 cell 可能已修改对象或写文件；全部保留会中断重放，全部跳过又会丢失实际状态。helper 哈希正确仍不能保证分析能重现。

建议修改：把“执行审计记录”和“可执行复现 recipe”进一步分开。历史按 kernel generation、language 和 environment 分段；每段标明初始化、helper 版本、输入和 checkpoint。失败、取消、结果不明、有外部副作用的执行保留在审计中，进入可执行 recipe 前须明确选择或补救。导出固定依赖与随机种子、算法参数、输入版本和输出契约；缺失信息标记为未验证。不要自动运行 bundle。

验收补充：除了 helper 最小示例，至少复跑一条真实组学流程；先验证输入和输出身份，再按所用方法规定数值容差或不变量。跨代次导出、源码在导出前被编辑、helper 源码更新和缺失大文件都应有明确结果。无需承诺跨平台数值逐位一致。

## 7. P2：分页和数据通道不能等到 M7 才落地

方案位置：[现行方案第 255 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:255)、[现行方案第 398 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:398)、[现行方案第 420 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:420)。

已验证：SSH JSON frame 限制为 1 MiB，当前 Notebook snapshot 加载所有 cell、execution 和 feedback。每个 cell 源码允许 256 KiB，执行 provenance 又保留代码。全量 snapshot 可以在远小于 1,000 次执行时超限；增加富输出后更容易发生。

证据：[协议限额](/workspace/dataset/private/zhouwg_data/OmicsClaw/omicsclaw/entry/ssh/protocol.py:15)、[snapshot](/workspace/dataset/private/zhouwg_data/OmicsClaw/omicsclaw/entry/desktop/notebooks_manager.py:313)、[源码限制](/workspace/dataset/private/zhouwg_data/OmicsClaw/omicsclaw/entry/desktop/notebooks_manager.py:346)。

建议修改：M1 先实现按字节有界的分页、轻量 snapshot、独立 artifact 引用、有限并发与取消后释放订阅。图像、导出和大输出必须有可验证的读取边界与传输路径。M7 再做虚拟列表、长期留存和大规模性能优化。保持 control/science RSS 分开测量，不要把大对象搬入 controller 来生成摘要。

验收补充：构造接近和超过 1 MiB 的元数据、图像与导出；慢消费者同时运行聊天和取消；事件 gap 后通过读取恢复，不能重跑代码。只用执行数量不足以覆盖这些边界。

## 8. P2：SSH 操作身份和用户执行授权仍停留在概念字段

方案位置：[现行方案第 234 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:234)、[现行方案第 255 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:255)、[现行方案第 255 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:255)。

已验证：普通 Notebook 用户 Run 是经过 HTTP 身份检查的显式操作，直接调用 NotebookAnalysis；Agent Run 走工具审批，两条入口的授权依据不同。App 现有 Notebook target 由 HTTP backend context 派生，controller 有独立 authority、workspace、epoch 和方法白名单。Notebook events 保存在 `notebooks.db`，controller 原有聊天事件来自 `memory.db`。

证据：[用户 Run 路由](/workspace/dataset/private/zhouwg_data/OmicsClaw/omicsclaw/entry/desktop/notebooks.py:35)、[Notebook target](/workspace/dataset/private/zhouwg_data/OmicsClaw-App/src/lib/notebook-target.ts:5)、[controller 方法白名单](/workspace/dataset/private/zhouwg_data/OmicsClaw-App/src/lib/controller/connection.ts:19)、[Notebook 数据库](/workspace/dataset/private/zhouwg_data/OmicsClaw/omicsclaw/entry/assembly.py:769)。

风险属于设计缺项，不表示现有 SSH Notebook 已有绕过漏洞，因为该路径尚未开放。新 adapter 若只复制 HTTP 路由名，可能丢掉目标切换校验、误用聊天事件 cursor，或把所有执行一律当成用户显式请求。

建议修改：为每项写操作列明服务端校验的 workspace/authority、session binding、generation、source revision 和 command digest；单独说明 Agent 审批、用户 Run、Live App state 的权限依据。定义命令 hash 是否包含代码、环境、预算和 generation，同一 command 携带不同语义必须冲突。给 Notebook 事件明确 stream namespace 与可恢复 cursor，不把 controller epoch 当作历史事件身份。Jupyter connection key/运行目录权限、原生 sandbox 和凭据清理沿用既有后端边界，补 SSH 环境下的集成验证。

不需要新增第二套权限系统、独立 scheduler 或独立 Notebook 数据库产品；这些是现有共享服务的适配规格。

## 9. P2：验收覆盖面不错，但无法判定“贴合组学”的完成状态

方案位置：[现行方案第 420 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:420)、[现行方案第 420 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:420)、[现行方案第 443 行](/workspace/dataset/private/zhouwg_data/OmicsClaw/docs/engineering/2026-10-15-notebook-comparison-and-roadmap.md:443)。

已验证：现有闭环测试使用 scripted provider，直接调用共享服务和真实 Python kernel，能证明共享变量和审批，却没有经过 App、SSH controller 与科学部署选择。原方案已明确这个限制，应保留。

证据：[现有闭环测试](/workspace/dataset/private/zhouwg_data/OmicsClaw/tests/entry/test_notebook_closed_loop.py:21)。

建议修改：把“固定小型单细胞数据”写成一条可以失败的验收脚本：数据版本及样本设计、技能、步骤、检查点、用户改动、预期产物、科学不变量、环境配置和故障注入时点均固定。再补一个跨 Job 或 R 的代表性流程，按首版承诺的支持等级选择。至少有一次真实 App 经实际 OpenSSH 连接的记录；scripted provider 和真实模型结果分开报告，模型质量不由管线测试代替。

“外部副作用只发生一次”应限定为同一已持久化 command 的重试不再次执行。若进程在写文件后、写终态前崩溃，只能证明不自动重跑，并记录结果待核对；无法对任意 Python/R 副作用承诺端到端 exactly-once。验收应验证文件副作用、执行 ledger 和未知状态分别如何呈现。

## 推荐目标定义

SSH controller 负责把用户与 Agent 的分析操作交给同一套后端业务服务。Notebook 是当前分析会话的执行记录与交互入口；每次执行能定位到确定的数据、环境和内核代次。用户可以查看结果、修改代码或选择分析对象，Agent 在继续之前知道这些变化。长任务的输入输出有明确交接，导出包含足以验证代表性分析的代码、环境、技能来源和数据引用。

这里“Claude Science 式”对应执行日志、共享内核、用户干预反馈、持续同步和可核对的复现材料。保留 Jupyter client、现有 NotebookAnalysis 和 Jobs 状态机即可，不需要复制它的 worker 协议、host SDK 或全部产品功能。

## 首个可交付版本的范围

建议把首个可交付目标限定为：一个远端 Linux SSH target、一个确定的 science 环境、一条完整 Python 组学分析流程，以及一个按实际需求选取的 R 或隔离长 Job 回接流程。只验证这些范围，不声称 88 个技能都可用。

交付必须同时包括：

1. App 上可配置并验证 science 环境；控制环境能启动 Notebook client，首次分析按需建立绑定，普通聊天不预启动内核。
2. Python Agent 与用户共用同一明确代次的内核；表达式、表格、图像和异常可见；持续执行能自动发现和回放。
3. 用户修改和 reset 不会让 Agent 悄悄执行旧状态代码；审批、目标切换、取消、重复提交和断线状态有明确契约。
4. 首批数据对象、矩阵语义、样本身份、选择状态和产物有可追踪引用；昂贵步骤可通过显式 checkpoint 恢复。
5. 长任务有受支持的时间预算或隔离 Job 路径，回接文件不会被误认为共享内存。R 的支持等级写明。
6. 分段导出在新环境复跑代表性流程，记录无法复现的部分；真实 App 加 SSH 验收通过。

EOF 关闭内核可以作为首版降级，但须明确告诉用户变量丢失，提供 checkpoint 恢复操作，并验证不会自动重跑。若用户要求断线后保留交互内存，这个降级版本不满足该要求。Slurm、多主机、多环境热切换、任意 HTML 内核 handler、通用对象序列化和完整 Jupyter 编辑器不在这个首版范围内。

## 推荐阶段顺序

| 阶段 | 产出 | 通过条件 |
| --- | --- | --- |
| S0 部署与契约 | science 来源、control profile、生命周期表、执行身份、首批科学流程 | 环境可真实配置；所有状态与故障有明确处理；缺失能力可解释 |
| S1 SSH 执行基础 | 共享服务接入、generation、environment identity、命令 digest、有界读取、权限与取消 | App 经 SSH 首次执行；重复提交、旧目标、取消和 EOF 行为符合 S0 |
| S2 连续交互 | 默认绑定、发现订阅、完整基础输出、用户变更水位、kernel 状态 | Agent、用户、Agent 连续工作；模型推理期间的用户修改不会导致静默错用状态 |
| S3 组学闭环 | 数据引用、样本/矩阵摘要、选择引用、checkpoint、预算与 Job 回接；选定的 R 支持 | 一条真实组学流程和一个跨执行模式流程通过，故障后能明确恢复 |
| S4 复现与来源 | 技能加载记录、固定源码、环境与数据 manifest、分段 recipe | 新内核或新环境复跑代表性分析，未知与不支持项明确 |
| S5 匹配版本交付 | App/backend 兼容、真实 SSH 故障验收、性能与安装检查 | 可供当前用户安装和重复完成核心流程；记录实测版本和验证范围 |

S0/S1 中的纯能力错误展示可以先交付，但不能替代 Notebook 完成。输出修复可并行进行。技能来源记录应在 S1 创建执行记录时留好扩展点，S4 再实现完整导出。环境身份和 generation 不能等到复现阶段才加入。发布完成后再按真实需求扩展可附着内核、更多 R 交互、多目标 HPC 和大规模历史管理。

## 建议的首批验收材料

科学验收分三类，选择少量现有技能即可：一类验证 AnnData 的 QC、聚类和选择后的再分析；一类验证多样本差异分析的 counts、sample/condition 对齐与统计单位，按已声明能力使用 Python 或 R；一类验证耗时步骤的 checkpoint、隔离 Job、结果回接与继续分析。三类可以复用同一小型数据集，不需要为全部技能建立验收工程。

| 场景 | 需要留下的证据 |
| --- | --- |
| 单细胞或空间分析 | 固定数据 checksum；counts/layer 和 sample 身份；加载、QC、聚类、绘图、用户选择及后续分析的执行记录和产物 |
| 用户干预 | 在模型推理或审批期间改变对象、参数或选择；记录旧状态执行被拒绝或重新规划 |
| 长 Job 或 R | input checkpoint、environment identity、job id、结果 manifest、Notebook 回接执行及数据身份核对 |
| 连接与进程故障 | App 关闭、SSH EOF、controller SIGKILL、kernel 死亡、owner 过期分别测试；结果待核对与已确认终态不混淆 |
| 幂等与取消 | 提交响应丢失重试；副作用后进程死亡；旧 execution 取消不能中断新 generation |
| 恢复与导出 | 原内核丢失后显式载入 checkpoint；独立环境运行分段 recipe；缺失数据或环境变更得到可操作错误 |
| 资源与传输 | 大于 frame 上限的输出与导出；慢消费者；数据对象常驻 science、controller RSS 有界；首个结果和重连时间 |

验证仍按仓库 `SPEC.md` 的风险级别执行。涉及 SSH 协议、权限、取消和匹配发布的实现属于 level 4；文档审查本身为 level 1。不要把原方案的历史测试通过数当作后续实现验收结果。


## 主审复核与完整目标的对齐建议

主审复核了环境发布和 App configure、controller 关闭链路、Notebook admission、Live state 选择限制、R kernelspec 与传输限额。上述主要问题成立。原对比方案继续作为现状证据；本审查提出实施规格需要补充的内容，本轮不修改其阶段表或运行代码。

按本轮目标，完整路线建议覆盖 Python/R 交互内核、用户与 Agent 共享分析状态、组学数据与选择身份、长任务回接、checkpoint 恢复，以及可验证的分段导出。Rscript 作业可以先交付，但不能用它替代 R 交互 Notebook 的完成验收。上面的首个可交付版本用于分阶段验证，不代表完整目标已经缩减到 Python 单链路。

实施时将 S3 拆成两个可分别评审的交付：组学数据、选择与长任务闭环；固定 R 环境的交互内核与独立命名空间。两项都进入完整目标，任意环境热切换和全量 HPC 调度继续后置。S1 即记录技能加载来源与固定源码引用，S4 补全导出格式和复跑验证，避免早期执行事后无法还原。

断线后的推荐基线是：历史可恢复、未知执行不自动重跑、昂贵分析有显式 checkpoint、隔离 Job 可以重新观察。交互内存是否保活在 S0 固定；选择 EOF 关闭时，必须标明变量失效并验证恢复成本。若后续要求断线后直接继续使用原内存对象，再将可重新附着的内核所有者纳入交付，单独处理租约、回收和取消。
