<!--
  自动生成 + 交叉验证:OmicsClaw 三仓库「统一平台」审计 TODO
  生成:Claude Code 多 agent workflow(omicsclaw-tri-repo-audit)— 15 finder × per-finding 对抗式 verify
  验证:codex gpt-5.5 xhigh(read-only,三仓库)逐条核对 + Claude 二次核验
  日期:2026-06-27 ｜ 覆盖:OmicsClaw(backend) + OmicsClaw-App(frontend) + OmicsClaw-KG
  规模:77 个 subagent;61 原始发现 → 57 通过对抗验证 → codex 复核(A–E 全 CONFIRMED;A-1/A-3 PARTIAL 已校正;5 项 E 升 High;0 误报;0 高置信遗漏)
  状态:POST-codex-cross-validation(已 reconcile;校正与结论见文末「codex 交叉验证结论」)
-->

# OmicsClaw 统一平台跨仓库审计 TODO

## 现状评估

平台的四个阶段(literature → idea → analysis → report)在各仓库都已有实现,但它们目前是**四段彼此脱节的管线,而非一条连通的流水线**。后端聊天 LLM 走 OpenAI 兼容的 `LLM_API_KEY`/`LLM_BASE_URL`,而 KG 的全部 LLM 能力(ingest 抽取 + ideation/formalize/experiment-design)硬编码 `AnthropicLLMClient` + `ANTHROPIC_API_KEY`,导致大多数用户的 idea 生成与知识抽取直接 500;自主分析引擎(平台的主执行路径)写出的 `completion_report.json` 与 App 读取的 `result.json` 契约不一致,使每个成功的自主分析都被误报为 running→failed 且图表/会话不回链;literature 技能因正则 tuple bug 只产出 metadata、从不向分析交付数据,且与 KG ingest 是两套互不打通的抽取实现;KG 的 handoff outbox 与 record-result 写回路径没有任何 App 代理或后端消费者,idea→analysis→verdict 闭环只能靠 LLM "自觉"调用工具。换言之,核心骨架已具雏形,但**贯通一条流程所需的关键缝合点(统一 LLM provider、自主结果回报、literature→KG 桥接、packet 执行器、thread 级作用域、report 阶段)基本缺失或断线**。

## 如何使用本清单

- **严重度图例**:🔴 Critical(核心流程被阻断) · 🟠 High · 🟡 Medium · 🟢 Low
- **[uncertain]** 标记表示仍需人工二次确认的项(本批次 57 项均为 verified=confirmed,故暂无 uncertain;若后续 codex 交叉验证翻案请补标)。
- 本文档已通过 **codex (gpt-5.5 xhigh) 交叉验证 + Claude 二次核验**(三方:workflow 对抗验证 + codex + 人工)。codex 对 A–E 全部逐条核对引用代码,结论见文末「codex 交叉验证结论」:绝大多数 CONFIRMED,无误报需删,未发现高置信遗漏;A-1/A-3 为 PARTIAL(已就地校正),5 项 E 上调为 High,执行顺序微调。
- 条目已**跨区去重**(尤其 contract-drift 与 per-repo 重复),合并项保留最强证据与全部文件引用。

## 实现进度 (2026-06-27)

A 节 4 项致命阻断已按 **TDD** 实现并经 **codex (gpt-5.5 xhigh) 复核**:A-1 首轮判 INCOMPLETE → 修复 2 项 must-fix 后复审 **COMPLETE**;A-2/A-3/A-4 首轮即 **LGTM**;0 回归。

| 项 | 修复 | 关键改动 | 测试 |
|---|---|---|---|
| A-1 | KG LLM provider 统一 | 新增 `OpenAICompatibleLLMClient` + `default_llm_client()`(读 `LLM_API_KEY`/`LLM_BASE_URL`/`OMICSCLAW_MODEL`,Anthropic 兜底,缺 provider 抛可读错误并在 HTTP 翻译为 503);改写 KG `_llm_for` 及 `cmd_ingest`/`cmd_ideate`/`cmd_experiment`/`cmd_watch`;backend `thread_formalize` 改用 live 平台 client `_build_kg_extractor()` | `OmicsClaw-KG/tests/unit/test_llm_client_provider.py` (12) |
| A-2 | 自主 run 写 `result.json` 契约 | `autonomous/runner.py` 末尾(完成标记)写 `result.json`(status/summary/output_dir/completed_at/error);`server._collect_key_files` 收录 `result_summary.md`/`completion_report.json` | `test_autonomous_code_runner.py`(+2)、`test_autonomous_desktop_linkage.py` |
| A-3 | 图表内联 + 会话回链 | `agent_executors._register_autonomous_media()` 把 figures + summary 经 `pending_media` 侧通道交给 `on_tool_result`;精简 digest 不变 | `test_autonomous_desktop_linkage.py` (4) |
| A-4 | GEO 下载 tuple bug | downloader 正则扩展组改为非捕获组 | `test_geo_downloader.py` (2) |

**验证**:KG `639 passed`;backend 触及面套件全绿。唯一红 `test_kg_specs...kg_build_packet` 经 `git stash` 验证为 HEAD 既有失败、与本次无关(见 [[known-preexisting-test-failures]])。

> 分支:backend 在 `fix/autonomous-verification-storm-and-token-economics`,KG 在新分支 `fix/kg-openai-compatible-llm`。改动**尚未 commit**(待确认)。下一批建议:A 节剩余 + B-1(`discover_file` 信任绕过)+ E 节 outbox executor / record-result 闭环。

### 第二批 (2026-06-27):B-1 安全 + E 确定性闭环

经 **TDD + codex (gpt-5.5 xhigh) 复核**(codex 首轮提 3 项 must-fix → 全部修复 → 复审 **B-1 COMPLETE / E COMPLETE·merge-ready,0 回归**)。

| 项 | 修复 | 关键改动 | 测试 |
|---|---|---|---|
| B-1 | `discover_file` 绕过 trusted-dir(任意文件读) | absolute + relative/glob 两分支都对每个命中过 `_is_trusted_root`(解析后再 `relative_to`,拦截 `..` 遍历与符号链接逃逸);集中修复,两处 agent 调用点自动受益 | `tests/test_discover_file_trust.py` (4:absolute/relative/symlink/missing) |
| E | idea→analysis→verdict **确定性闭环** | 新增 `omicsclaw/surfaces/desktop/outbox.py`:`run_packet()` 加载 outbox packet→解析 skill(`target.skill_name`/`recommended_skills`)+thread dataset→`arun_skill` 跑→`asyncio.to_thread` 调 `record_result` 写回(归档 packet、更新图、推进 experiment step);`list_outbox_packets()`;`_import_kg_handoff` 加 `HandoffPacket`。端点 `POST /thread/{id}/run-packet`、`GET /thread/{id}/outbox`。packet_id 经 `_is_safe_slug` 防注入;verdict 默认诚实 `inconclusive`,拒绝 `refined`;run 失败不写回 | `tests/test_outbox_executor.py` (10) |

闭合:outbox 自此有确定性消费者,结果回写不再依赖 LLM 自觉;experiment-step packet 经 `record_result` 自动分派(后端消费者已就位)。

**本批遗留(follow-up,非阻断)**:
- (2→3)/C 节:App 侧 `KGExperimentDAG` submit→`/run-packet`、`/outbox` 列表、`/record-result` 代理 UI 仍需接线(前端)。
- dataset 解析:packet 不携带真实路径,执行器用 thread 绑定的单一 dataset(per-step 不同 dataset 尚不支持;ADR 0021 v1.5 deferral 同源)。
- experiment step `running` 中间态仍无人写(record_result 直接置终态;非阻断)。

---

### 第三批 (2026-06-27):App 端 KG 接线(run-packet / Eval 真正可用)

经 **TDD + codex (gpt-5.5 xhigh) 复核**(codex 提 2 项 must-fix → 修复 → 复审 **VERIFIED·merge-ready,0 回归**)。打通了 KG Explorer 上 submit→run→Eval 的 UI 闭环(此前 submit 写 packet 但无人运行 → step 永远 `planned` → Eval 必 400)。

| 项 | 修复 | 关键改动 |
|---|---|---|
| C Eval 死路 / (2→3) | UI 能运行 packet → step 转终态 → Eval 不再 400 | 新增代理 `app/api/thread/[id]/run-packet`、`.../outbox`(经 `bench-proxy` 转后端);client `runPacket`/`threadOutbox` + 类型;`KGExperimentDAG` 加 `threadId`,从 outbox 取本实验待跑 packet,"Run (N)" 按钮逐个跑(verdict `inconclusive`)、记录数/失败数反馈、跑完清 pending;`KGExplorer` 经 `getLastThreadId()` 注入 thread |
| (codex must-fix) | 后端 `{detail}` 错误不再被压成 "HTTP 400" | `bench-api-client.request` 兼读 `error`/`detail` |
| (codex must-fix) | 运行上下文不再静默 | UI 显式展示 `Runs against thread {id}` + 可选 dataset 覆盖输入;无 thread 时显式提示而非隐藏按钮 |

测试:App `npm test` = **1259 passed**(client +runPacket/threadOutbox/`{detail}`;component +Run-flow +no-thread);eslint 干净;typecheck 干净。分支 `OmicsClaw-App` 新建 `feat/kg-run-packet-ui`(未 commit)。

**遗留**:KG Explorer 在 mount 时快照 last-thread,其他视图切换 thread 不会即时联动(已显式展示运行 thread,不致静默误跑);per-step 不同 dataset、experiment↔thread 正式绑定仍是后续项。

---

### 第四批 (2026-06-27):D-1 literature → KG ingest 收敛(打通文献→知识→idea grounding)

经 **理解 workflow(4 reader→design)+ TDD + codex (gpt-5.5 xhigh) 复核**(codex 提 1 项 must-fix〔inline ingest 阻塞用户工具〕→ 改后台+超时 → 复审 **merge-ready,0 回归**)。此前 3 套分叉正则抽取,literature 产出从不进 KG → 跑 literature 建零图 → ideation 无从 grounding。

| 项 | 修复 | 关键改动 |
|---|---|---|
| D-1 (literature 路径) | literature 产出进 KG → 成为可 grounding 的 Source | `kg_tools.ingest_source_into_kg(source)` 单一 in-process 桥(复用 `_build_kg_extractor`/`_resolve_kg_home`/`cmd_ingest.ingest`,`to_thread`,缺 KG/LLM 软失败);`literature_parse.py` 把解析后的文本持久化为 `source.txt`(所有输入类型)+ result.json 加 `source_text_path`/`source`;`agent_executors._ingest_literature_into_kg` 读 `source.txt` 触发 KG ingest,**后台 spawn + 120s 超时**,绝不阻塞/中断 literature 工具 |

**单一真源决策**:KG ingest(`omicsclaw_kg cmd_ingest`)是唯一建图者。literature 正则保留(GEO 下载/报告),不再建图;改持久化 `source.txt` 供后端 ingest。选 `source.txt`(服务端可控、统一、无 SSRF/重抓)而非重抓 URL/重解析 PDF。
**grounding 闭合**(codex 证实):`source.txt`→KG router 走 NOTE → `cmd_ingest` 写 `wiki/sources/<slug>.md` + concept/claim 图节点 → `formalize` 经 `list_workspace_source_slugs` 立即可引用;`draft_questions`/`hypotheses` 在 concept 累积 ≥2 source 后浮现。

测试 `tests/test_literature_kg_ingest.py`(10):桥软失败/分派、后端触发/无源 noop/非致命/**超时有界**、skill 持久化 source.txt。无回归(唯一红仍是既有 `kg_build_packet` spec)。

**遗留(D-1 余项)**:`omicsclaw/agents/intake.py`(第三套正则,自主 intake 管线)未收敛——设计已就绪(pipeline `prepare_intake` 后触发 `ingest_source_into_kg` + `IntakeResult.kg_source`,正则降级为离线 fallback),作为后续项;per-thread source 作用域(ADR 0019/0021)仍为已知缺口。

---

### 第五批 (2026-06-27):B 节剩余 High-priority bugs(B2/B3/B4)

经 **TDD + codex (gpt-5.5 xhigh) 复核**(codex 三轮:B3 提 must-fix〔新 `status:"failed"` 结果未被 callers 处理〕→ 传播到 `_fmt_ingest`/CLI printer/batch counter/watch + literature hook 日志 + batch 摘要 → 复审 **VERIFIED merge-ready,0 回归**;B2/B4 首轮即 merge-ready)。

| 项 | 修复 | 关键改动 |
|---|---|---|
| B2 (backend) | `oc run literature` DOI/URL/text 不再被 mangle | `_prepare_skill_run` 仅对**真实存在的本地文件/目录**才 `resolve()`;DOI/URL/text 原样透传给 `--input`,skill 自动检测类型;真实文件仍解析(子进程换 cwd) |
| B3 (KG) | 抽取 JSON 容错 + 单源 ingest 不崩 | `_loads_extraction`(去 fence→直解→首个平衡 `{...}`,字符串/转义感知→typed `ExtractionError`)用于两个 client;`_do_file_ingest` **和 webclip** 捕获 → 记 failed result;failed 结果**全链路传播**(`_fmt_ingest` 单/批、CLI printer、batch failures 计数、watch、literature hook 日志) |
| B4 (frontend) | 远程模式 file 变更路由不再误写本地 fs | `write`/`mkdir`/`rename`/`open` 套用 `delete` 的 `isRemoteMode()` 409 `remote_unsupported` 守卫 |

测试:backend `test_literature_cli_input.py`(3)、`test_literature_kg_ingest.py`(11,+failed 传播);KG `test_extraction_json_tolerance.py`(9,含 webclip + batch failed 计数);App `files-mutation-remote-guard.test.ts`(2)。三仓库套件全绿(唯一红:既有 flaky-DNS webclip 测试 + 既有 `kg_build_packet` spec,均与本次无关,见 [[known-preexisting-test-failures]])。

---

### 第六批 (2026-06-27):E 节 SSE cancel_event + Bench Write 报告阶段

经 **TDD + codex (gpt-5.5 xhigh) 复核**(两项首轮即 **merge-ready,无 must-fix**;codex 两条 nice-to-have〔Write slug 跨 thread 冲突、写前刷新 verdict〕已折入)。

| 项 | 修复 | 关键改动 |
|---|---|---|
| E SSE 断连 cancel_event (codex↑High) | 关标签页中断长 run 不再遗孤 skill 子进程 | 抽 `server._abort_active_session(session_id)`:**先 set `cancel_event` 再 `task.cancel()`**(ADR 0009,经 subprocess `_cancel_watcher` 真杀子进程);`/chat/abort` 与 SSE `event_generator` 的 `except CancelledError` 共用 |
| E Bench "Write" 报告阶段 (codex↑High) | 终端报告阶段从 placeholder → 真实功能,闭合 (4)→KG | 新增纯工具 `lib/bench/synthesis.ts`(slug/report/SynthesisFM)+ `WritePanel`:拉取 thread 假设+verdict → 预览综述 → 客户端 `kgPutPage('syntheses', …)` 写回 KG(schema-valid:id==slug/type/graph_node_id;claims/supported_by 聚合);slug 按 thread 作用域防跨 thread 覆盖;写前刷新 verdict;`StageRail` 渲染 WritePanel |

测试:backend `test_chat_abort_cancel_event.py`(3:set-before-cancel 顺序/清理/无 session);App `bench-synthesis.test.ts`(5:slug/report/empty/frontmatter/thread 作用域)。backend SSE 测试绿;App `npm test` = **1266 passed**,eslint+typecheck 干净。

> **E 节遗留(未做,大改)**:~~`(1) thread↔source 作用域`~~ **✅ 第七批完成**(下)。其余 E:literature Next Steps 多 domain、Results 分页、Ideate→Analyze 一键 为 M 级(批8/9)。`X-OmicsClaw-Workspace` 多工作区按用户决定**暂不顺带**,留作独立批次。

---

### 第七批 (2026-06-27):E-(1) thread↔source 作用域(per-thread grounding,跨三仓库架构批次)

经 **understand workflow(6 reader)+ 3-lens 对抗式 design critique(翻 A→B + 4 must-fix)+ TDD + codex (gpt-5.5 xhigh) 复核(VERDICT merge-ready,0 must-fix,2 nits〔陈旧 docstring 已修 / total 计数语义 non-blocking〕)**。打通"按项目 grounding":知识 ingest 记 thread,formalize 只用本 thread 的 source 作 allow-list,ReadPanel 列本 thread sources,cross_study(跨课题)徽章变真。**0 回归**。

设计要点(critique 后):thread↔source 链是 *study state* → 存图 Memory System(DB,per-user namespace),**KG Source 页仍全 workspace 共享(ADR 0019 不破)**;链以独立 overwrite 节点 `thread_source://<tid>/<slug>`(非 versioned)存,**消除** A 方案(ThreadMemory 加 list 字段)与 `update_thread`/`set_thread_preference` 的跨抽象 read-modify-write 丢更新竞态。KG 侧 `formalize_hypothesis(thread_source_slugs)` 早已就绪 → **无需改 KG formalize**。

| 仓库 | 修复 | 关键改动 |
|---|---|---|
| KG | cache-hit ingest 不返回 slug → 跨 thread 复用同论文时新 thread 关联丢失 | `wiki/writer.py::find_source_slug_by_ingest_hash`(扫 `wiki/sources/*.md` frontmatter 的 ingest_hash —— `resolve_unique_source_slug` 对已消歧 slug **非幂等**,不能重派生);`cmd_ingest._do_file_ingest` + `ingest/webclip.ingest_url` 两 cache-hit 分支回填 `slug` |
| backend | 落 thread 标记 + 写/读桥 | `memory/compat.py::ThreadSourceMemory`(+`_TYPE_TO_DOMAIN`/`_TYPE_CLASSES`/`_memory_to_uri_path`,overwrite-mode);`orchestration._capture_thread_source`(best-effort,守 memory-off/空参,吞 LookupError);`thread.list_thread_source_slugs`(`list_children` 无尾斜杠) |
| backend | ingest 联动写链 | `agent_executors._ingest_literature_into_kg/_spawn_literature_kg_ingest`(thread_id/session_id 透传,spawn 时绑定;ingested+skipped 都带 slug 则记);`kg_tools.execute_kg_ingest`(+`thread_id`,`_fmt_ingest` 前 capture slug);`agent.py` kg_ingest ToolSpec `context_params`+`thread_id` |
| backend | grounding 收敛(收 C 节 formalize 双契约)+ 新端点 | `hypotheses.formalize_thread_hypothesis(thread_source_slugs=None)`(None→workspace;list→与 workspace 交集作 allow-list;[]→ungrounded 非 400);`to_frontend_hypothesis(thread_slugs=None)` cross_study 守 None;`list_thread_sources`(富化+剔陈旧);`server.py` `thread_hypotheses`/`thread_formalize` **sync→async**(await slugs + `asyncio.to_thread` 卸载阻塞 KG/LLM);新 `GET /thread/{id}/sources` |
| App | Read 面板按 thread 列 sources | `types/bench.ts::ThreadSource[Response]`;`bench-api-client.threadSources`;`api/thread/[id]/sources/route.ts`;`hooks/useThreadSources`;`ReadPanel`(threadId prop + 内层 `ReadSources` 列表/loading/empty);`StageRail` 透传;i18n en/zh(`bench.read.empty` 改 per-thread + `bench.read.loading`) |

测试(全先红后绿):KG `test_cache_hit_slug_backfill.py`(5:find/cache-hit/disambiguated/webclip);backend `test_thread_source_index.py`(7:模型 round-trip/URI/非 versioned/capture+list 收敛/守卫/缺 thread)、`test_literature_kg_ingest.py`(+6:ingested/cache-hit/无 thread 联动 + kg_ingest 工具)、`test_bench_ideate_wiring.py`(+9:thread-scoped allow-list/空 thread ungrounded/None workspace/cross_study/list_thread_sources/3 端点 wiring)。App `bench-read-panel.test.tsx`(4:KG-dark 不 fetch/空/列表/错误降级)。
**结果**:KG 5✓+unit 全绿(1 既有 DNS-flaky webclip 无关);backend 新增 35✓ + memory 279✓ + tools/registry 62✓,0 新回归(2 项 HEAD 既红:`test_chat_stream_emits_protocol_events_and_usage`、`test_kg_specs...kg_build_packet`,`git stash` 验证);App `npm test` **1270 passed** + typecheck/eslint 干净。

> **遗留**:并发同 thread 多 ingest 经独立节点已无丢更新;本批前 ingest 的 workspace source 无 thread 关联(新 thread 从零积累,无 backfill,可接受)。IM/无 thread/memory-off 一律退回 workspace-wide。

---

### 第八批 (2026-06-28):C/D 剩余漂移/分散项收敛(用户定向:/thread 为 canonical + 交叉链接,非拆除)

经 **understand(直接读码)+ TDD + codex (gpt-5.5 xhigh) 复核(VERDICT merge-ready-with-must-fix → 1 must-fix〔D-3 build_packet 与 route-preview 的 dataset 解析不一致会漂移 skill〕已修 → 2 nits 折入/记录)**。**0 新回归**(2 项 HEAD 既红仍既红)。决策见 **ADR 0036**。

| 项 | 修复 | 关键改动 |
|---|---|---|
| §4.2 / D-1 intake 收敛 | 自主 pipeline 的 intake 喂 KG(此前只 regex,建零图) | `agents/intake.py`:`IntakeResult` 加 `source_text_path`/`kg_source`;`prepare_intake` 持久化全文到 `paper/source.txt`;新 `async ingest_intake_paper`(经 `kg_tools.ingest_source_into_kg`,best-effort never-raises,Mode C/resume→"");`agents/pipeline.py` `run` 调用。regex 留作 research_request 元数据 |
| C-1 双契约 | 定单一写表面 | ADR 0036;`/thread/*` canonical(App 只调);KG `routes.py` `post_ideate_formalize`/`post_confirm_hypothesis_verdict` docstring 标 headless-only(不注销) |
| C-3 formalize 漂移 | 见第七批(`thread_formalize` 已 thread-scoped) | — |
| C-5 ideate 选项 | UI 透传被丢的 KG 选项 | `bench-api-client.kgIdeate` 按 kind 透传 `llm`/`min_size`/`for_concept`;`KGIdeationWizard` topics LLM 开关 + syntheses for_concept;i18n |
| D-2 双 idea→analysis 表面 | 交叉链接 + ADR | `KGExplorer` 加 "Open in Bench →"(→`/bench/<threadId>`);ADR 0036 标孤立流 deprecated(不拆)。**复审补测**:`kg-explorer.test.tsx` +2 例断言交叉链接 href(无 thread→`/bench`、有 thread→`/bench/<id>`) |
| D-3 skill 选择双机制 | Router 权威 | `kg_tools._router_skill_for_hypothesis(slug,home,dataset_path)` + `_thread_dataset_path_for_loop`;`execute_kg_build_packet`(+`session_id`/`thread_id` context_params)未显式 target_skill 时用 Router 对 (claim, **thread dataset_path**) 推 skill;dataset 解析经**共享** `compat.resolve_thread_dataset_path`(从 `hypotheses` 提取,route-preview 同源 → codex must-fix)。显式优先,不确信则退回 |

测试(先红后绿):backend `test_intake_kg_convergence.py`(8)、`test_kg_tools.py`(+6 D-3 含 dataset 一致性)、`test_bench_ideate_wiring.py`(route_preview 经共享 resolver 仍绿);App `bench-api-client.test.ts`(+3 kgIdeate)。**结果**:backend 触达模块 97✓(仅既有 `kg_build_packet` read-stage spec red)+ intake/ideate/memory 全绿;App `npm test` **1273 passed** + typecheck/eslint 干净;KG doc-only(imports 干净)。

> **遗留/记录**:C-4 `X-OmicsClaw-Workspace` 多工作区按用户决定**暂不做**(独立批次)。codex nit:`intake.kg_source` 在 resume 经 `from_workspace` 丢失(无害,字段仅记录 side-effect)。
> **顺带修复的真实 bug**:`_resolve_thread_dataset_path`(已提取为共享 `compat.resolve_thread_dataset_path`)原用**尾斜杠** `get_subtree("dataset://<tid>/")`,真实 MemoryClient 返回空 → thread 绑定的 dataset **从不到达 Router**(route-preview/run-packet/build_packet 全静默退化为 claim-only)。改无尾斜杠后三处同时生效;加真实-client 回归测试 `test_resolve_thread_dataset_path_real_client`(假 client 因无视 slash 而漏过此 bug)。

---

### 第九批 (2026-06-28):E 节中级三项(沿 literature→idea→analysis→report 补齐统一流程)

经 **TDD + codex (gpt-5.5 xhigh) 复核**(E#1 codex 2 must-fix〔LC-MS 误判、短 alias 子串误匹配〕已修;E#2 首轮 merge-ready;E#3 codex 提 2 must-fix〔slug 未过 `_is_safe_slug`、run 不落 AnalysisMemory → Analyze 面板看不到〕已修+回归测试,复审中)。**0 新回归**(2 项 HEAD 既红 + 1 项 App 多文件上传聚合 flaky〔隔离跑过〕)。

| 项 | 修复 | 关键改动 |
|---|---|---|
| E-(1) literature Next Steps 多 domain | 6 domain domain-aware 路由 | `extractor.extract_technology` 扩 6 domain(spatial/sc 优先;bulk 需显式 anchor;**`\b…s?\b` word-boundary+plural** 防 wes∈western/支持 metabolomic→metabolomics;metabolomics 先于 proteomics 解 LC-MS 歧义)+ `infer_domain`/`DOMAIN_ENTRY`(skill 名盘核验);`generate_report` 按 domain 出入口 skill |
| E-(4) Results 分页 | 破除静默封顶 10 | 后端 `outputs_latest` limit cap `le=50→200`;`/api/outputs` 代理透传 `limit`+`project`(session 仍前端过滤);`OutputPanel` 请求 100 + "加载更多"(↑200) |
| E-(2→3) Ideate→Analyze 一键 | 程序化 run(augments chat) | 新 `POST /thread/{id}/run-hypothesis`(`_is_safe_slug` 守 → build_packet〔Router 权威 skill〕→ outbox.run_packet);**`run_packet` 成功后落 thread-scoped `AnalysisMemory`**(codex must-fix,run-packet 亦受益)→ 经 useThreadArtifacts 入 Analyze 面板;RouterRecommendationCard "Run now"(仅有 skill);IdeatePanel `runRoute` |

测试:backend `test_literature_next_steps`(10)、`test_run_hypothesis_endpoint`(4 含 unsafe-slug 400)、`test_outbox_executor`(+2 AnalysisMemory 捕获)、`test_thread_source_index`(+1 real-client dataset);App `bench-api-client`(+3 kgIdeate)、`outputs-route`(+1 转发)、`bench-ideate-panel`(+1 Run now)。**结果**:backend 触达模块全绿(仅既有 `kg_build_packet` spec red);App `npm test` 1274/1275(1 既有 flaky 上传测试,隔离跑 4/4)+ typecheck/eslint 干净。

> **遗留**:E-(4) 后端仅 limit 无 offset,>200 真分页留作后续。C-4 多工作区 header 用户暂缓。

---

### 第十批 (2026-06-28):F 节 KG/backend 小修(correctness/contract)

经 **TDD + codex (gpt-5.5 xhigh) 复核**(codex 提 1 must-fix〔refined-slug 校验只在 verdict='refined' 分支,但 feedback 无条件写 wiki 链 → 非 refined 的坏 slug 仍悬挂〕→ 改为任意非空 slug 都校验格式 + 非 refined verdict 拒绝 refined 字段 → 复审 merge-ready)。**0 回归**(KG 377 passed)。

| 项 | 修复 |
|---|---|
| `parse_doi` lstrip 腐蚀 | `'10.' + doi.removeprefix('10.')`(lstrip 删字符集 {1,0,.} 把 1038→38) |
| `/log/recent` total 截断 | 切片前算 `total`(post-filter)+ 加 `returned`,对齐 kg_search/list_pages |
| Experiment.status 死转换 | 记录首个 step 即 `planned→running`(原 `any(status=="running")` 永假;eval 仍无条件 `completed`) |
| `refined_hypothesis_slug` 无格式校验 | `HandoffResult` 对**任意非空** slug 校验 `^[a-z][a-z0-9-]*$`≤80;非 refined verdict 拒绝 refined 字段(codex must-fix) |
| `build_packet` 忽略 recommended_skills | 无显式 target_skill 时按序 `resolve_skill(fm.recommended_skills)` 再退 file_drop;**显式 target_skill(含 D-3 Router)权威,不被覆盖** |

测试:backend `test_literature_parse_doi`(3);KG `test_mcp_server`(+2)/`test_exp_eval_flow`(+1)/`test_record_result_flow`(+4)/`test_handoff_flow`(+1)。

> **F 节剩余(~12 项,后续批 — 批10/11/12 见下)**:KG HTTP 无 auth;backend 🟢(per-session policy 清理、多文件附件、流中错误 transcript、死 approval 参数);frontend/Electron(~11)。

---

### 第十一批 (2026-06-28):F 节安全/完整性(KG ingest)

经 **TDD + codex (gpt-5.5 xhigh) 复核**(codex 提 3 must-fix:① refill 用 `resolve_unique_source_slug` 给同名源造新 slug → 改复用 `existing_slug`;② `load_graph` 对**损坏**(非删除)graph.json 仍抛 → backup+warn+空图;③ DNS rebinding TOCTOU〔pre-existing,非本项范围〕→ docstring 标 KNOWN RESIDUAL,defer。①② 修+回归 → 复审 **merge-ready**)。**0 回归**(KG 672 passed)。

| 项 | 修复 |
|---|---|
| webclip SSRF 只校验初始 URL | `_ValidatingRedirectHandler` 对**每跳** `validate_url`(`build_opener` 替换默认 redirect handler,跳数 ≤10);`_fetch` 用该 opener |
| content-hash 缓存短路图写入 | `graph_store.has_source(cfg,slug)`;cache-hit **仅当** cached+wiki 页+图节点全在才 skip,否则**复用缓存抽取**重跑 upsert/merge(免 LLM)、**复用原 slug**;file+webclip 两路;`load_graph` 容损坏 |

测试:KG `test_webclip_ssrf_redirect`(4)、`test_cache_hit_slug_backfill`(+4:删图/损图/消歧 slug refill、图在则 skip)。

---

### 第十二批 (2026-06-28):F 节 backend 🟡(billing 准确性 + rollback 隔离)

经 **TDD + codex (gpt-5.5 xhigh) 复核**(codex **merge-ready**,2 should-fix nits:① `memory/api/review.py` 硬编码 `__shared__`,namespace 现被强制后会 fail-close → 加 `resolve_memory_namespace` 取节点实际 partition〔已修〕;② Anthropic 原生 shape 计价〔运行时 OpenAI-compat,非 blocker〕)。**0 回归**(337 passed)。

| 项 | 修复 |
|---|---|
| cost_usd 按全价计 cache 命中(90% hit→~5x) | 两计价路径:`billing.py` 加 `cached_input_tokens` + `CACHE_READ_DISCOUNT=0.1`(env override,clamp);`get_usage_snapshot` cost=fresh*inp+cached*inp*DISC+out;`server._build_token_usage` 同(fresh=input−cache_read,与 payload `fresh_input_tokens` 一致);state 重导出 `_cache_read_discount` |
| memory rollback 忽略 namespace | `rollback_to` 写前校验目标 node 经 `Memory.node_uuid→Edge.child_uuid→Path.namespace` 在给定 ns 可达,否则 raise(挡跨 ns 改 version chain);desktop 已传 `desktop_namespace()`;standalone review 经 `resolve_memory_namespace` 取实际 ns |

测试:backend `test_billing`(+2)、`test_token_economics_slimming`(+1 server cost)、`test_review_log`(+2:跨 ns 拒绝、resolve_memory_namespace)。

---

### 第十三批 (2026-06-28):F 节 backend 🟢 cluster

经 **scope workflow(4 并行 reader)→ TDD + codex (gpt-5.5 xhigh) 复核**(codex 提 2 must-fix:① 多文件 bare key 跨轮粘在首个文件 → 加 `_reset_session_attachments` 每轮换新批;② autonomous 工具 ToolSpec 漏 `approval_mode=ASK` → 我的 docstring 谎称外层 gate;补 ASK〔RISK_HIGH 任意代码,与 sibling 一致〕+ 删死 context_param。再 2 nit:LRU 非真正最近、stale doc。全部修 → **复审 merge-ready**)。**0 回归**(touched 210 passed)。

| 项 | 修复 |
|---|---|
| per-session policy/profile 字典永不清理 | `_MAX_TRACKED_SESSIONS=512` + `_evict_stale_session_state()`(插入序淘汰最旧);**不在 finally pop**(会丢跨轮 "Allow for session" 审批);`_set_session_permission_profile` 先算 next_state(保留跨轮 approved_tool_names)再 pop+重插 → LRU-by-turn |
| 多文件附件仅最后一个注册 | `_register_attachment_for_session`:首个文件存 bare `session_id`、其余存 `session_id::path`(全局扫描 reader 全见,值仍 dict 不动 5 个 reader);`_build_multimodal_content` 每个有文件的轮次先 `_reset_session_attachments`(换新批,免 stale) |
| 流中非可重试错误 partial 不入 transcript | `_materialize_message_from_stream` try/except 把 partial(仅 content+reasoning,**不含半截 tool_calls**)挂到 exc;loop error 分支在 on_llm_error 前 append partial + `_STREAM_INTERRUPTED_NOTE`(guard partial.content → pre-stream 失败不伪造) |
| `request_tool_approval` 死参数 | 从 `run_autonomous_code_loop[_async]` + caller + ToolSpec context_params 删除;**真正 gate**:autonomous 工具补 `approval_mode=ASK`(外层 ADR 0008 L2);docstring/架构 doc 校正 |

测试:`test_app_attachments`(+3)、`test_desktop_session_state_eviction`(2)、`test_query_engine`(+2)、`test_mini_agent_runner`(+1)、`test_engineering_tools`(+1 approval-gated);`baseline_bot.json` snapshot 重生(仅含批8 kg_ingest 描述变更)。

> **F 节剩余(~11 项)**:frontend/Electron(git/autofiles 远程守卫、pathology SSE 丢弃、卡住的 tool spinner、compress-retry race、preflight SSE、respawn storm、python-probe 阻塞、spawn-env 调和、review 非 uuid 行、skill-install SSE 解析)。

---

### 第十四批 (2026-06-28):F 节 KG HTTP 写路由 auth

经 **TDD + codex (gpt-5.5 xhigh) 复核**(codex 提 1 must-fix:`_is_local_bind_host("")` 返 True 但 uvicorn 把 `host=""` 映射成 wildcard → `--host ""` 绕过守卫 → 改空 host 视为非 local;3 nit:仅 exempt `/_health`〔`/health` 是 KG 读路由〕、测试用真实写路由 `/kg/handoff`、`compare_digest` 改 bytes 防非 ASCII 500。全部修 → **复审 merge-ready**)。**0 回归**(KG 676 passed)。

| 项 | 修复 |
|---|---|
| 独立 KG HTTP server 写路由无 auth | `build_app` 加 `@app.middleware("http")` 可选 bearer(`OMICSCLAW_KG_API_TOKEN`:unset=开放〔本地默认〕,set 时除 `/_health` 全路由需 `Bearer`,`compare_digest` bytes);CORS 后加=外层,preflight 不受影响;`serve_http` fail-closed:非 local/空 host 无 token → SystemExit(`_validate_server_security`/`_is_local_bind_host`,镜像 memory-server);embedded `build_router` 路径不受影响(宿主自管 auth);CLI help 文档化 |

测试:KG `test_http_api_auth`(4:unset 开放、bearer 必需〔wrong/Basic/`/kg/health` 401〕、真实写路由 `/kg/handoff` gated、空/远程 host fail-closed)。

---

### 第十五~十七批 (2026-06-28):F 节 frontend/Electron(OmicsClaw-App)

经 **11-reader scope workflow → TDD + codex (gpt-5.5 xhigh) per-cluster 复核**。三簇全 **0 回归**(App typecheck clean + unit suite 1311 passed)。

**批15 SSE/transcript(codex merge-ready,1 must-fix〔多文件 bare key 跨轮 stale〕+ nits 已处理,见批16)**
| 项 | 修复 |
|---|---|
| 卡住的 tool spinner | `chat-stream-transcript.buildPersistedAssistantMessageContent` 为停止轮次遗留的无 result `tool_use` 合成 `is_error` 占位 result(免重载永久 spinner + history 合规) |
| skill-install SSE 跨 chunk 丢事件 | 抽 `lib/skills/install-progress-stream.consumeInstallProgressStream`,event-type 累加器**提到 read loop 外**(跨 TCP 拆分持久),组件改用之 |
| pathology_detected SSE 丢弃 | `useSSEStream` 加 `PathologyDetectedInfo`+callback+case+proxy;types union;stream-session 派发 `CustomEvent`(markActive);ChatView warning toast;i18n `chat.pathology.notice`(en+zh) |
| preflight_pending SSE 脆弱文本解析 | 抽 `preflightFromPayload`(与文本扫描 DRY);`useSSEStream` 消费结构化事件 keyed by tool_use_id 写权威 `preflightByToolId`,文本扫描降为 fallback |
| compress-retry race | 抽 `lib/chat/compress-and-retry.runCompressAndRetry`:先订阅再发 `/compact`,等 `completed` 事件且 `phase==='completed'` 才重发(+watchdog),消除 retry abort 掉 /compact 的竞态 |

**批16 remote/review(codex merge-ready,nits 已处理)**
| 项 | 修复 |
|---|---|
| git 代理无 remote 守卫 | 新 `api/git/_remote-guard.gitRemoteGuard()`(remote→409 `remote_unsupported`),插入全部 10 个 git 路由首句(写前,挡误改本地 repo) |
| autoagent/files 无 remote 守卫 | remote 时 `proxyOptimizeFiles` 转换代理后端 `/files/tree`(browse depth1 / scan depth3,`path.posix` cwd-relative,ext 过滤,越界 403,404/throw 透传) |
| review tab 丢无 uuid 行 | `groupByNode` 用永不空 `groupKey`(`uuid`/合成 `table:id`),nodes/paths 行不再丢,渲染总数=后端 count |

**批17 Electron supervisor(codex merge-ready,1 must-fix〔源码混入 NUL 字节→git 视为二进制,改 `.join('\0')`〕+ 2 nit 已处理)**
| 项 | 修复 |
|---|---|
| respawn 风暴(error 态每 tick 重生) | 纯 `decideReconcile`+`planSignatureOf`;`lastFailedSignature` 抑制同签名失败计划重生(失败经 `getStatus()==='error'` 检测,因 start() 吞 health-timeout);force/签名变才重生 |
| spawn-env 调和忽略 proxy | `getLastEnv()` 入 RuntimeProcess+python-manager;签名并入 proxy-only `spawnEnvSignature`(8 键)→ Settings 改 proxy 自动重生、PATH 等不抖动 |
| python-probe 阻塞主线程 | 抽 `resolvePathInterpreter`(memoize+注入式 async probe);main.ts 改 promisified `execFile`+进程级 cache,免每 tick `execFileSync` 冻结 |

测试(App node:test):`review-audit-grouping`/`git-routes-remote-guard`/`optimize-files-remote`/`chat-stream-transcript`/`install-progress-stream`/`sse-stream-pathology`/`sse-stream-preflight`/`compress-and-retry`/`runtime-supervisor`/`python-runtime`。

### 第十八批 (2026-06-28):F 节 refined-hypothesis 图血缘(KG)

经 **TDD + codex (gpt-5.5 xhigh) 复核**。**0 回归**(KG 677 passed)。

| 项 | 修复 |
|---|---|
| verdict='refined' 的 hypothesis 页从不接入图 | `feedback._update_graph` refined 分支:创建 refined hypothesis **节点** + `original --REFINED_AFTER--> refined_hyp` 血缘边(原误指 `original→AnalysisResult`;hyp→result 关系本就由 TESTED_BY 覆盖);`graph/rebuild.py` hypotheses 分支读 `refines` 并重建同向 `REFINED_AFTER` 边(全量 rebuild 不再丢血缘) |

测试:KG `test_record_result_flow::test_refined_verdict_creates_page_and_edge`(改为断言 refined 节点+original→refined 边、且 original→result 非 REFINED_AFTER)、`test_graph_rebuild`(+1:rebuild 重建 refines 血缘)。

---

> **F 节剩余(1 项)**:`X-OmicsClaw-Workspace` 多工作区隔离(**用户明确 DEFER,先聚焦 thread 作用域**)。

---

## A. 🔴 Critical / blockers(核心流程被阻断)

- [x] ✅ **KG 全部 LLM 能力锁死 Anthropic,与平台 OpenAI 兼容 provider 隔离** — kg+backend/fragmentation/🔴 · ingest 抽取与 ideation/formalize/experiment-design 都 `AnthropicLLMClient()`+`ANTHROPIC_API_KEY`,任何配 DeepSeek/OpenAI/Ollama 等且无 Anthropic key 的用户在步骤(1)知识抽取与(2)idea 生成处直接 500,阻断 literature→idea 主链 · 证据: `OmicsClaw-KG/omicsclaw_kg/llm/client.py:39,50,57`, `omicsclaw_kg/http_api/routes.py:198-199,385`, `omicsclaw_kg/cli/cmd_ingest.py:321`, `OmicsClaw/omicsclaw/surfaces/desktop/server.py:3226,3229`, `omicsclaw/surfaces/desktop/hypotheses.py:77-88` · 修复: 在 `omicsclaw_kg.llm` 增加 OpenAI 兼容 client,由 KGConfig+`LLM_API_KEY`/`LLM_BASE_URL` 注入(model 可覆盖),让 ingest 与 ideation 共用平台 provider;过渡期至少把缺 key 转成可读 400/503(合并 be-desktop-api-1 / kg-http-mcp-2 / kg-ingest-graph-1 / x-e2e-flow-6)
  - 🔎 **codex 校正(范围收窄,严重度不变)**:后端 chat-agent 的 `kg_ingest` 工具已通过 `_build_kg_extractor()`(`omicsclaw/runtime/tools/kg_tools.py:462`)把平台 OpenAI client 注入 KG ingest,故「经聊天上传文献」的抽取不受影响。真正 Anthropic-locked 的是:① KG-native CLI ingest(`cmd_ingest.py:321`)② 全部 KG HTTP ideation/formalize/syntheses/experiment-design(`routes.py:198` `_llm_for`)③ desktop thread formalize(`server.py:3228`)。idea 生成(步骤 2)整条仍被阻断 → **维持 Critical**;修复应在 KG 包内为上述路径注入同一 OpenAI 适配器(已确认 `kg_tools.py:462` 可复用)。
- [x] ✅ **自主分析写 `completion_report.json`,但 App outputs 读 `result.json` → 每个自主 run 被误报 running→failed** — backend/bug/🔴 · 主执行引擎成功的 run 在 `/outputs/latest` 显示 running 30 分钟后变 failed,且交付物 `result_summary.md` 不进 key files,步骤(4)结果回报对主引擎彻底失效 · 证据: `omicsclaw/autonomous/runner.py:60,118`, `omicsclaw/runtime/policy/verification.py:40`, `omicsclaw/surfaces/desktop/server.py:5419,5455,5463` · 修复: 在 `write_run_records` 把 `AutonomousRunStatus` 映射成 `result.json`(status/summary/error/completed_at),或让 `_read_result_json`/`_collect_key_files` 回退到 `completion_report.json`+`result_summary.md`
- [x] ✅ **自主工具返回纯文本 digest,无路径/无 media → 图表不渲染、run 不回链会话** — backend/bug/🔴 · verification-storm 修复把结构化路径换成 Markdown,`_extract_media`/`_resolve_session_run_dir` 只解析 JSON,自主 run 的图永不内联、也写不出 `.omicsclaw_session.json`,"本对话"看不到产物 · 证据: `omicsclaw/runtime/tools/builders/agent_executors.py:2088,2209,513`, `omicsclaw/surfaces/desktop/server.py:1441,1686,5532` · 修复: 让自主 executor 返回含 `output_dir`/`run_dir`/figure 路径的紧凑 JSON,或注册 `pending_media[session_id]`
  - 🔎 **codex 校正(措辞收窄)**:digest 内其实**含输出路径**(纯文本 prose 形式,`agent_executors.py:2128`),只是无 JSON/media 字段;而 `_extract_media`/`_resolve_session_run_dir` 仅解析 JSON/dict(`server.py:1441,5574`)。故「图不内联、run 不回链 session」结论成立,仅「无路径」应改为「路径不可机读」。
- [x] ✅ **GEO 补充文件下载因正则 tuple bug 静默失效 → literature 从不向分析交付数据** — backend/bug/🔴 · `re.findall` 用了两个捕获组返回 tuple,`output_dir/filename` 抛 TypeError 被外层 except 吞掉,只剩 metadata.json,而 `_register_literature_datasets` 跳过 metadata.json → 注册 0 个数据集,paper→GEO→分析 handoff 产出为空 · 证据: `skills/literature/core/downloader.py:96-141`(111-115), `omicsclaw/runtime/tools/builders/agent_executors.py:1018-1024` · 修复: 改非捕获组 `(?:h5ad|mtx|csv|tsv|txt|gz|tar)` 或取 `m[0]`,加测试断言能产出至少一个非 metadata 路径

---

## B. 🟠 High-priority bugs

- [x] ✅ **`discover_file()` 返回未受信绝对路径,agent 调用方未复验 → 绕过 trusted-dir 闸门** — backend/risk/🟠 · `validate_input_path` 拒绝的越界绝对路径被 `discover_file` 复活并直接读取,配合 prompt 注入/远程 job 输入构成任意文件读取,破坏"基因数据不出本机"边界(remote routers 已挂载放大为远程任意读) · 证据: `omicsclaw/services/path_validation.py:182-186`, `omicsclaw/runtime/tools/builders/agent_executors.py:250-254,2013-2019`(对照 `omicsclaw/analysis_router/dispatcher.py:69-73` 已复验) · 修复: 两处 agent 调用点对每个 `discover_file` 命中都过 `validate_input_path`,或让 absolute 分支自身强制 `_is_trusted_root()`
- [x] ✅ **`oc run literature` 把 DOI/URL/text 输入 resolve 成文件路径,文档化的 CLI literature 入口对最常见输入不可用** — backend/bug/🟠 · `_prepare_skill_run` 无条件 `Path(...).resolve()`,`10.1038/...`/`https://...` 被 mangle 后落到 'text' 解析、无 GEO、退出(agent 路径绕过 runner 仍可用 → surface 不一致) · 证据: `omicsclaw/skill/runner.py:153-154`, `omicsclaw/skill/execution/argv_builder.py:92-93`, `skills/literature/SKILL.md:74-86` · 修复: 对非文件型 skill(SKILL.md flag 或 url/doi/text 形状检测)跳过 resolve,或对 literature 别名原样转发 `--input`+`--input-type`
- [x] ✅ **LLM 抽取结果 `json.loads` 无容错 → 单源 ingest 遇畸形 JSON 崩溃** — kg/bug/🟠 · 模型返回 prose/空串/截断 JSON 即抛 JSONDecodeError,`_do_file_ingest`/webclip 单文件路径无 try/except(仅 batch 有),步骤(1)单文档抽取直接抛栈给桌面 · 证据: `OmicsClaw-KG/omicsclaw_kg/llm/client.py:88-98`, `omicsclaw_kg/ingest/extractor.py:58`, `omicsclaw_kg/cli/cmd_ingest.py:126` · 修复: 容错解析(抽首个 `{...}`、尝试修复),失败抛 typed `ExtractionError` 记为 failed result;加 "return ONLY valid JSON" 限次重试
- [x] ✅ **远程 SSH 模式下 file write/mkdir/rename/open 缺少 delete 已有的 remote guard,静默写本地 Next.js fs** — frontend/risk/🟠 · 文件树列的是远程路径,但这些 mutation 路由无 `isRemoteMode()` 检查,在错误机器写文件并可覆盖同名本地文件,损坏远程 GPU 执行半侧 · 证据: `OmicsClaw-App/src/app/api/files/write/route.ts:18`, `.../mkdir/route.ts`, `.../rename/route.ts`, 对照 `.../delete/route.ts:18-28`(已 409 remote_unsupported) · 修复: 对 write/mkdir/rename/open 套用 delete 的 `isRemoteMode()` 409/501 守卫,或待后端有 file-mutation 端点后做真实远程代理

---

## C. 🔗 跨仓库接口漂移(App↔Backend↔KG contract drift)

- [x] ✅ **KG 写路由 (/ideate/formalize、/handoff、/record-result、/hypothesis/{slug}/confirm-verdict) App 无代理,后端以 /thread/* 重复实现 → 双契约漂移** — cross/fragmentation/🟠 · **第八批(ADR 0036)**:定 `/thread/*` 为 canonical desktop 写表面(App 只调它),KG-native `/kg/ideate/formalize`+`/hypothesis/confirm-verdict` 标 headless-only(routes.py docstring;不注销以保 headless KG);`/handoff`/`/record-result` 非纯重复(KG Explorer 经 C-2 代理在用)保留 first-class · 同一能力两套 HTTP 表面,KG 原生写表面对前端是死路,两边请求模型/grounding/错误映射已经分叉(KG `FormalizeRequest{hunch,thread_source_slugs,stub}` vs 后端 `ThreadFormalizeRequest{hunch}`) · 证据: `OmicsClaw-KG/omicsclaw_kg/http_api/routes.py:336,359,675,690`, `OmicsClaw-App/src/lib/bench-api-client.ts:118,133`, `OmicsClaw/omicsclaw/surfaces/desktop/server.py:3209`, `omicsclaw/surfaces/desktop/hypotheses.py:83` · 修复: 选定单一 HTTP 表面——要么 App 直连 `/kg/*` 写路由删后端 thread 重复,要么把 KG 写路由降级为 library-only 不再 `build_kg_router` 注册(合并 kg-http-mcp-1 / fe-kg-formalize-confirm-unreachable)
- [x] ✅ **KG experiment "Eval" 按钮死路:无 record-result App 代理,step 永远非 terminal → eval 必 400** — cross/gap/🟠 · DAG 只有 submit/eval,新建 step 默认 `planned`,仅 `record_result` 能推进到 terminal,而 App 无 `/api/kg/record-result` 路由也无 `kgRecordResult` client,KG Explorer 上 (3)→(4) 闭环无法在 UI 完成 · 证据: `OmicsClaw-App/src/components/kg/KGExperimentDAG.tsx:120,135`, `src/lib/bench-api-client.ts:257`, `OmicsClaw-KG/omicsclaw_kg/http_api/routes.py:336,359`, `omicsclaw_kg/handoff/exp_eval.py:78`, `omicsclaw_kg/handoff/feedback.py:400`, `omicsclaw_kg/schema/frontmatter.py:203` · 修复: 加 `/kg/record-result` 代理+client 与 KG-Explorer "记录结果"动作,或 run 完成后由后端自动 `record_result`(合并 fe-kg-experiment-loop-deadend / x-contract-drift-1)
- [x] ✅ **"formalize hypothesis" 两套实现分叉,App 用的那套忽略 thread 作用域** — cross/fragmentation/🟡 · `formalizeHypothesis` 只发 `{hunch}` 到 `/thread/{id}/formalize`,后端对**整个 workspace 的 Source** grounding,而支持 `thread_source_slugs` 的 `/kg/ideate/formalize` 从 App 不可达 → UI 标称 "thread-grounded" 实为 workspace-wide(ADR 0021 已知 v1.5 deferral) · 证据: `OmicsClaw-App/src/lib/bench-api-client.ts:118`, `OmicsClaw/omicsclaw/surfaces/desktop/server.py:3096,3209`, `omicsclaw/surfaces/desktop/hypotheses.py:77`, `OmicsClaw-KG/omicsclaw_kg/http_api/routes.py:180,675` · 修复: **第七批解决(就此收敛方案)** — 后端 `thread_formalize` 现解析 thread 绑定的 source slugs(`_thread_source_slugs`)透传给 KG `formalize_hypothesis(thread_source_slugs)`,`/thread/{id}/formalize` 成 canonical desktop 表面;KG-native `/kg/ideate/formalize`(body 驱动)降为 headless-only(已在 `thread_formalize` docstring 注明)
- [ ] **X-OmicsClaw-Workspace 多工作区隔离形同虚设:无任何 client 发该 header** — cross/gap/🟡 · KG 为 per-request 多 workspace 设计,但 App 代理只转 path+body+Authorization,所有 KG 调用落到单一默认 `OMICSCLAW_KG_HOME`,多项目知识图无法按项目隔离 · 证据: `OmicsClaw-KG/omicsclaw_kg/http_api/workspace.py:24`, `OmicsClaw-App/src/lib/bench-proxy.ts:23`, `src/lib/backend-fetch.ts:99`, `OmicsClaw/omicsclaw/surfaces/desktop/server.py:175` · 修复: App KG 代理按当前 thread/session 注入 `X-OmicsClaw-Workspace`,或明确单 workspace 并移除该 header 路径
- [x] ✅ **App ideate client 丢弃 llm/min_size/for_concept → KG topic LLM 增强与 concept 定向 synthesis 从 UI 不可达** — cross/gap/🟢 · **第八批**:`kgIdeate` 透传 `llm`/`min_size`/`for_concept`(按 kind);`KGIdeationWizard` topics 加 LLM 开关、syntheses 加 for_concept 输入 · `kgIdeate` 只发 `{limit,stub?}`,UI topic ideation 永远 structural-only,concept 定向 synthesis 完全无法触发 · 证据: `OmicsClaw-App/src/lib/bench-api-client.ts:305`, `src/components/kg/KGIdeationWizard.tsx:66`, `OmicsClaw-KG/omicsclaw_kg/http_api/routes.py:80,88` · 修复: 扩展 `kgIdeate` 选项与向导表单透传 `llm`/`min_size`/`for_concept`(代理已 verbatim 转发)

---

## D. 🧩 功能分散 / 重复实现(需收敛到单一真源)

- [x] ✅(literature) **三套互不互通的 literature 抽取实现,literature 技能输出从不喂给 idea 或 KG** — backend+kg/fragmentation/🟠 · (a) `skills/literature` 正则(GEO `\b(GSE\d{3,})\b`)、(b) `agents/intake` 分叉正则(`GSE\d{4,8}`)、(c) `omicsclaw_kg` LLM ingest,三者抽取质量分叉且无共享结构化产物,跑 literature 技能建出零知识图 · 证据: `skills/literature/core/extractor.py:7-100`, `skills/literature/literature_parse.py:117-147`, `omicsclaw/agents/intake.py:406-460,1011-1031`, `omicsclaw/runtime/tools/kg_tools.py:539-589`, `OmicsClaw-KG/omicsclaw_kg/ingest/extractor.py` · 修复: **以 `omicsclaw_kg` ingest 为单一真源**,让 literature 技能与 intake stage 消费同一抽取模块并触发 KG ingest,使抽取的 metadata 成为可被 ideation grounding 的知识对象(合并 be-skills-knowledge-lit-3 / x-e2e-flow-4)。**literature 第四批完成,intake 第八批 §4.2 完成**(`intake.prepare_intake` 持久化 `paper/source.txt` + `ingest_intake_paper` 经 `kg_tools.ingest_source_into_kg` 喂 KG;regex 降为 research_request 元数据补充;pipeline `run` best-effort 调用,never breaks)
- [x] ✅ **两个竞争的 idea→analysis 表面(Bench vs KG Explorer)无交叉链接,只有 Bench 接入 agent** — frontend/fragmentation/🟡 · **第八批(ADR 0036)**:Bench/chat-agent 定为 canonical idea→analysis 真源;KGExplorer 加 "Open in Bench →" 交叉链接(→ `/bench/<threadId>`),ADR 标其孤立 experiment 流为 deprecated(不拆除) · Bench `IdeatePanel` 经 `fill-message-input` 接 Analyze agent(可用),KG Explorer 是独立 ideation+experiment 流且生命周期死路,两者共享 KG 数据但无导航/handoff,KG 里发现的 idea 进不了同一分析路径 · 证据: `OmicsClaw-App/src/lib/primary-nav.ts:22`, `src/components/bench/IdeatePanel.tsx:116`, `src/components/kg/KGExperimentDAG.tsx:51`, `src/components/kg/KGIdeationWizard.tsx` · 修复: **以 Bench/chat-agent 为单一 idea→analysis 真源**,把 KG Explorer 的 experiment-run 路由到同一 composer/Analyze dispatch,或加显式交叉链接并写 ADR 废弃孤立路径
- [x] ✅ **测试某假设的 skill 选择有两套互不协调机制(KG catalog vs 后端 Router)** — cross/fragmentation/🟡 · **第八批**:`execute_kg_build_packet` 在 agent 未显式指定 `target_skill` 时,经新 `_router_skill_for_hypothesis` 用 Router 对 (claim, thread dataset_path) 推 chosen_skill 作 target(`resolve_skill` 仅校验);dataset 解析经共享 `compat.resolve_thread_dataset_path` 与 route-preview 同源(codex must-fix:claim-only 会与 preview 漂移)。显式 target_skill 优先;Router 不确信则退回 KG recommended_skills/file_drop · `build_packet` 用 `bridge.resolve_skill(target_skill)` 烘入 `packet.target.skill_name`,而 `route_preview` 用 `route_analysis_request(claim)` 从 claim 文本推 `chosen_skill`,二者可不一致且无冲突标记 · 证据: `OmicsClaw-KG/omicsclaw_kg/handoff/builder.py:92`, `omicsclaw_kg/handoff/bridge.py:174`, `OmicsClaw/omicsclaw/surfaces/desktop/hypotheses.py:126` · 修复: 让一方权威——Router 的 `chosen_skill` 喂入 `build_packet` 作 `target_skill`(`resolve_skill` 仅校验),UI 与 packet 同源呈现单一推荐 skill

---

## E. 🧪 达成统一流程缺失的能力(沿 literature→idea→analysis→report 排序)

- [x] ✅ **(1) 无 thread↔source 关联:ingest 知识全 workspace 级,无法按项目作用域** — cross/gap/🟠 · `kg_ingest` 不记 thread,`ThreadMemory` 无 source list,`thread_hypotheses`/formalize 全 workspace-wide、`cross_study` 恒 False,Read 面板即使 KG 在线也列不出项目 sources(ADR 0021 已知 v1.5 deferral) · 证据: `omicsclaw/surfaces/desktop/hypotheses.py:1-13`, `omicsclaw/surfaces/desktop/server.py:3189-3206`, `OmicsClaw-App/src/components/bench/ReadPanel.tsx:8-14` · 修复: **第七批完成** — ingest 经 `_capture_thread_source` 记 `thread_source://<tid>/<slug>` 独立节点;`GET /thread/{id}/sources` + `formalize` 按 thread allow-list;ReadPanel 列本 thread sources;cross_study 变真。KG Source 页仍 workspace 共享(ADR 0019)
- [x] ✅ **(1) literature 报告 "Next Steps" 只路由 spatial/single-cell,无视其余 4 个 domain** — backend/gap/🟡 · 已检测 technology 却未映射到 genomics/proteomics/metabolomics/bulkrna 入口 skill,bulk/蛋白/代谢论文得到错误路由 · 证据: `skills/literature/literature_parse.py:187-190`, `skills/literature/core/extractor.py:67-86` · 修复: **第九批** — `extract_technology` 扩 6 domain(spatial/sc 优先;bulk 需显式 anchor;**word-boundary+plural** 匹配防 wes∈western;metabolomics 先于 proteomics 解 LC-MS 歧义)+ `infer_domain`/`DOMAIN_ENTRY`(skill 名经盘核验);`generate_report` 按 domain 产出入口 skill,unknown 列通用起点。codex 2 must-fix(LC-MS 误判 proteomics、短 alias 子串误匹配)已修
- [x] ✅ **(2) HandoffPacket 写入 outbox 后无任何消费者 → target/skill/dataset 载荷只写不读** — cross/gap/🟠(codex↑High) · `write_packet` 是前向箭头终点,后端 analysis_router/autonomous/execution 全无 outbox reader,packet 丰富契约惰性,是否/哪个分析跑全凭 LLM 读提示串 · 证据: `OmicsClaw-KG/omicsclaw_kg/handoff/writer.py:13`, `omicsclaw_kg/handoff/builder.py:177`, `omicsclaw_kg/handoff/feedback.py:288`, `OmicsClaw/omicsclaw/runtime/tools/kg_tools.py:626` · 修复: 加 outbox executor(watcher 或 `POST /thread/{id}/run-packet`)加载 packet→解析 `target.skill_name`+dataset→经 execution 层跑→写回 `HandoffResult`(与下条共用)
- [x] ✅ **(2→3) KG experiment submit 的 packet 无后端消费者 → 设计好的 experiment 永不启动** — cross/gap/🟠(codex↑High) · `/kg/experiment/{slug}/submit` 每 step `write_packet`,UI 只显示 "submitted N packets",无 watcher/poller 读 outbox 跑 skill · 证据: `OmicsClaw-KG/omicsclaw_kg/http_api/routes.py:616-650`, `omicsclaw_kg/handoff/packet.py:1-7`, `OmicsClaw-App/src/components/kg/KGExperimentDAG.tsx:122-135`, `OmicsClaw/omicsclaw/runtime/tools/kg_tools.py:644-660` · 修复: 同上 outbox executor,并把 `KGExperimentDAG` submit 接到该端点(合并 x-e2e-flow-1)
- [x] ✅ **(2→3) Bench Ideate→Analyze 仅 chat-text 预填,无程序化分析启动** — frontend/gap/🟡 · `acceptRoute` 只 `fill-message-input`+切 Analyze,需用户手动按发送(ADR 0023 §6 有意设计,但仍非"一键") · 证据: `OmicsClaw-App/src/components/bench/IdeatePanel.tsx:116-127`, `src/components/bench/AnalyzePanel.tsx:106-111`, `OmicsClaw/omicsclaw/surfaces/desktop/hypotheses.py:124-148` · 修复: **第九批** — 新 `POST /thread/{id}/run-hypothesis`(build_packet〔Router 权威 skill,D-3〕→ outbox.run_packet),结果落为 thread analysis(经 useThreadArtifacts 入 Analyze);RouterRecommendationCard 加 "Run now"(仅有 skill 时),IdeatePanel `runRoute` 跑完切 Analyze。**augments** chat 路径(ADR 0023 §6 的 acceptRoute 保留)
- [x] ✅ **(3) 客户端断连只 `task.cancel()` 不 set cancel_event → 遗孤 skill 子进程** — backend/risk/🟠(codex↑High) · `chat_abort` 会先 `cancel_event.set()` 再 cancel,而 SSE 断连处理只 cancel,长 run 中关标签页泄漏 detached 子进程 · 证据: `omicsclaw/surfaces/desktop/server.py:2142-2145`(对照 2180-2184) · 修复: 在 `event_generator` 的 `except CancelledError` 分支镜像 `chat_abort`,先 `_active_envelopes.get(session_id).cancel_event.set()`
- [x] ✅ **(4) Results 面板静默封顶 10 个 run,无 limit/分页** — frontend/gap/🟡 · 代理不转 `limit`/`project`,后端默认 `limit=10`,旧产物在统一 dashboard 永久不可达 · 证据: `OmicsClaw-App/src/app/api/outputs/route.ts:195`, `src/components/layout/OutputPanel.tsx:615`, `OmicsClaw/omicsclaw/surfaces/desktop/server.py:5620` · 修复: **第九批** — 后端 `outputs_latest` limit cap `le=50→200`;`/api/outputs` 代理透传 `limit`+`project`(`session` 仍前端过滤);`OutputPanel` 请求 `limit=100` + "加载更多"(↑200)。遗留:后端仅 limit 无 offset,>200 的真分页留作后续
- [x] ✅ **(4→2) 结果写回 KG 完全依赖 LLM 自觉调用 `kg_record_result`,无确定性闭环** — cross/gap/🟠(codex↑High) · `record_result` 只经 agent 工具 + 无 App 代理的 `/record-result` 可达,loop 仅 prompt nudge,agent 跳过则 verdict/evidence 永不写回 · 证据: `OmicsClaw/omicsclaw/runtime/tools/kg_tools.py:662-718`, `omicsclaw/runtime/tools/builders/agent.py:1069`, `OmicsClaw-KG/omicsclaw_kg/handoff/feedback.py:213-362`, `omicsclaw/engine/loop.py:148` · 修复: 由 hypothesis/packet 启动的 run 完成后,服务端确定性调用 `record_result` 带 run artifacts(绑定 outbox executor)
- [x] ✅ **(4) Bench "Write" 报告阶段未构建 → Bench 流程无结果→workspace/KG 回报终端** — frontend/gap/🟠(codex↑High) · write 阶段走 `StagePanelPlaceholder` "coming soon"(有意 v2 scope-out),管线无已建成的终端阶段 · 证据: `OmicsClaw-App/src/components/bench/StageRail.tsx:20-24,74-97` · 修复: 构建 Write 面板,汇总 run artifacts+verdicts 成报告,经 `kgPutPage` 写回 KG Synthesis/wiki 页与/或 workspace 报告文件,闭合 (4)→KG

---

## F. 🟡 Medium / 🟢 Low backlog(精简)

- [x] ✅(批12) **cost_usd 把 cache 命中输入按全价计 → 与同 commit 输出的 cache_hit_ratio 自相矛盾** — backend/bug/🟡 · 90% 命中时报价约真实 5x · 证据: `omicsclaw/surfaces/desktop/server.py:922-935,949-957`, `omicsclaw/services/billing.py:139-143` · 修复: `cost = fresh_input*price + cached_input*price*DISCOUNT + output*price`,价表加 cached-read 费率
- [x] ✅(批12) **memory rollback 忽略 namespace 参数,文档化的 partition 隔离是死代码** — backend/bug/🟡 · 任意 namespace 的 memory_id 都可 rollback,共享 DB 多 namespace 下桌面 Review 可越区改 version chain · 证据: `omicsclaw/memory/review_log.py:164-211`, `omicsclaw/surfaces/desktop/server.py:3974-3999` · 修复: rollback 前 join Memory→Edge→Path 校验 namespace 可达,否则报错;或删参数并改正 docstring
- [x] ✅(批16) **所有 git 代理本地执行,无 remote-mode 处理** — frontend/gap/🟡 · 远程模式下 git surface 全部失败/误指本地 fs · 证据: `OmicsClaw-App/src/app/api/git/status/route.ts:11`, `src/lib/git/service.ts:1,21` · 修复: `isRemoteMode()` 守卫返回明确不支持,或经后端 git 端点远程执行
- [x] ✅(批16) **autoagent/files(optimize 选择器)无 remote 守卫扫本地 fs** — frontend/gap/🟡 · 远程模式选不了输入数据集(对照 autoagent/commit 已守卫) · 证据: `OmicsClaw-App/src/app/api/autoagent/files/route.ts`, `.../autoagent/commit/route.ts:38-50` · 修复: remote 时代理到后端 `/files/browse|/files/tree`
- [x] ✅(批15) **后端 `pathology_detected` SSE 事件被前端静默丢弃** — frontend/gap/🟡 · agent 自检的卡死/verification-storm 警告到不了用户 · 证据: `OmicsClaw-App/src/hooks/useSSEStream.ts:120-321`, `src/types/index.ts:740-755`, `OmicsClaw/omicsclaw/surfaces/desktop/server.py:2049-2060` · 修复: `handleSSEEvent` 加 case + 警告 chip,补 `SSEEventType`
- [x] ✅(批15) **被停止/超时的轮次持久化 tool_use 无 tool_result → 重载后永久 spinner** — frontend/bug/🟡 · 历史已完成消息显示假"运行中" · 证据: `OmicsClaw-App/src/lib/chat-stream-transcript.ts:164-194`, `src/components/chat/MessageItem.tsx:185-233`, `src/components/ai-elements/tool-actions-group.tsx:203-206` · 修复: 非完成终态为缺结果的 tool_use 合成 `(interrupted)` 占位 result,或 getStatus 对历史消息无结果判为 error
- [x] ✅(批15) **"compress and retry" 恢复竞态:retry abort 掉它依赖的 /compact 流** — frontend/bug/🟡 · 用户被弹回 overflow,平台自带恢复失效 · 证据: `OmicsClaw-App/src/components/chat/ChatView.tsx:494-496,572-578`, `src/lib/stream-session-manager.ts:259-318` · 修复: retry 链到 /compact 实际完成事件后再发,而非同步连发
- [x] ✅(批15) **后端 `preflight_pending` 结构化 SSE 未被消费,前端靠脆弱文本标记解析** — frontend/fragmentation/🟢 · 文本格式一变 preflight 引导卡片即静默失效 · 证据: `OmicsClaw/omicsclaw/surfaces/desktop/server.py:1716-1730`, `OmicsClaw-App/src/hooks/useSSEStream.ts:120-321`, `src/lib/stream-session-manager.ts:485-513` · 修复: 消费该事件按 `tool_use_id` 入 `preflightByToolId` 作真源,文本扫描降为 legacy fallback
- [x] ✅(批17) **本地后端 respawn 风暴:reconcile 对 error 态每 ~30s 无退避重启,绕过 MAX_RESTART_RETRIES** — frontend/bug/🟡 · 坏后端无限重生、淹没真实诊断 · 证据: `OmicsClaw-App/electron/runtime-supervisor.ts:88-94`, `electron/main.ts:585,607,763-765`, `electron/python-manager.ts:394-399` · 修复: 把 error 视为非 force 不自动重试态,跟踪失败计数/上次失败 plan 签名,plan 变化或用户强制才重试
- [x] ✅(批17) **每 3s poll tick 同步 `execFileSync` 探测 python 版本,阻塞 Electron 主线程** — frontend/risk/🟡 · `source==='path'` 时每 tick 最多 3000ms/候选阻塞致 UI jank · 证据: `OmicsClaw-App/electron/main.ts:616,656-670`, `electron/runtime-supervisor.ts:85` · 修复: 解释器路径解析一次性缓存,或 no-op 短路前置于探测之前,或改 async `execFile`
- [x] ✅(批17) **reconcile no-op 守卫忽略 spawn env → proxy/shell-env 改动需手动重启才生效** — frontend/gap/🟢 · 用户在 Settings 修好"连接错误"却无反应,阻 (2) ideation · 证据: `OmicsClaw-App/electron/runtime-supervisor.ts:90-94`, `electron/main.ts:696-713`, `src/app/api/settings/app/route.ts:20-37` · 修复: 变化检测纳入 spawn env 签名(至少 proxy vars),或 proxy 键 PUT 时触发 force reconcile
- [x] ✅(批16) **memory review tab 静默丢弃无 node-style uuid 的变更行(如 nodes-table 行)** — frontend/bug/🟡 · 审阅者看不到 Approve/Clear 将作用的全部变更,总数发散 · 证据: `OmicsClaw-App/src/app/memory/ReviewAuditTab.tsx:41`, `OmicsClaw/omicsclaw/memory/snapshot.py:213`, `omicsclaw/memory/models.py:109` · 修复: grouping 回退到 `record.uuid`(edge 用 source/target),或把无归属行归入合成分组
- [x] ✅(批15) **skill install/uninstall SSE 解析每 chunk 重置 event type → 跨 TCP 读拆分的 data 行丢失** — frontend/risk/🟢 · 终端 done/error 被吞,对话框卡 running · 证据: `OmicsClaw-App/src/components/skills/InstallProgressDialog.tsx:87`, `src/app/api/skills/marketplace/install/route.ts:36` · 修复: 把 `currentEvent` 提到 while 循环外持久化(对齐 notebook/execute 行缓冲解析器)
- [x] ✅(批13) **per-session permission/policy 状态字典永不清理** — backend/risk/🟢 · 长跑后端缓慢内存增长 · 证据: `omicsclaw/surfaces/desktop/server.py:354-355,2104-2118` · 修复: `_run_loop` finally 中 pop 两个字典,或 LRU 限界
- [x] ✅(批13) **多文件聊天附件:仅最后保存的文件注册供工具自动拾取** — backend/bug/🟢 · 一次拖多个数据集时 `received_files` 只见最后一个 · 证据: `omicsclaw/surfaces/desktop/server.py:1006-1010`, `omicsclaw/surfaces/desktop/_attachments.py:234-243` · 修复: 每 session 存 list 而非单 dict(显式路径 inline 引用已覆盖)
- [x] ✅(批13) **流式中途非可重试 LLM 错误:部分内容已流给用户但 assistant 轮次未入 transcript** — backend/risk/🟢 · transcript/UI 分歧,下轮历史缺用户已见内容 · 证据: `omicsclaw/runtime/agent/query_engine.py:481-532,1366-1389,1405-1418` · 修复: 中途失败后把 partial content(或截断+error 标记)入 transcript 再走 on_llm_error
- [x] ✅(批13) **`request_tool_approval` 透传到自主 loop 但被静默忽略;非 bwrap 桌面靠 best-effort monkeypatch** — backend/risk/🟢 · 宣传的审批钩子无效(顶层 loop 仍可 gate;有 fail-closed strict mode) · 证据: `omicsclaw/runtime/tools/builders/agent_executors.py:2200`, `omicsclaw/autonomous/code_loop.py:287`, `omicsclaw/autonomous/mini_agent_runner.py:80`, `omicsclaw/autonomous/runtime_guard.py:30` · 修复: 删除误导性死参数,或在非沙箱档真正 gate 首个可执行 cell 并向用户暴露 isolation 档位
- [x] ✅(批10) **`parse_doi` 用 `lstrip('10.')` 腐蚀畸形 DOI** — backend/bug/🟢 · `1038/foo`→`10.38/foo`(错 registrant) · 证据: `skills/literature/core/parser.py:84-90` · 修复: 改 `removeprefix` 或正则,或非 `10.` 前缀时原样透传让解析显式失败
- [x] ✅(批10) **/log/recent 返回截断后的 total,破坏其他 KG 端点遵守的分页契约** — kg/bug/🟢 · UI 无法判断是否有更多条目 · 证据: `OmicsClaw-KG/omicsclaw_kg/mcp_server/tools.py:94,297,308` · 修复: 切片前计算 total(对齐 `kg_search`/`kg_list_pages`)
- [x] ✅(批14) **独立 KG HTTP server 暴露写路由无任何 auth,唯一缓解是 --read-only** — kg/risk/🟢 · `--host 0.0.0.0` 即无认证公开写 API · 证据: `OmicsClaw-KG/omicsclaw_kg/cli/cmd_http.py:18`, `omicsclaw_kg/http_api/server.py:42`, `omicsclaw_kg/http_api/routes.py:8` · 修复: `build_app` 加可选 bearer 中间件(`OMICSCLAW_KG_API_TOKEN`,unset 时开放=本地默认,set 时除 `/_health` 全路由需 `Bearer`,`secrets.compare_digest`);`serve_http` fail-closed:非 local bind 无 token 即 SystemExit(`_validate_server_security`/`_is_local_bind_host`,镜像 memory-server);CLI help 文档化。测试 `test_http_api_auth.py`(4)
- [x] ✅(批18) **verdict='refined' 创建的 hypothesis 页从不接入图** — kg/bug/🟡 · `REFINED_AFTER` 边指向 AnalysisResult 而非 refined hypothesis,精炼血缘对图导航不可见(rebuild 也忽略 `refines`) · 证据: `OmicsClaw-KG/omicsclaw_kg/ideation/refine.py:84`, `omicsclaw_kg/handoff/feedback.py:181,316` · 修复: `record_result` 中加 refined 节点与 original→refined `REFINED_AFTER`/`SUPERSEDES` 边并 `save_graph`
- [x] ✅(批10) **`refined_hypothesis_slug` 未做格式校验 → 坏 slug 产生悬挂 wiki 链接且无页面** — kg/risk/🟢 · 静默死链(仅 wiki 链悬挂,图边不悬挂) · 证据: `OmicsClaw-KG/omicsclaw_kg/handoff/result.py:51`, `omicsclaw_kg/ideation/refine.py:40`, `omicsclaw_kg/handoff/feedback.py:303` · 修复: `HandoffResult` 按 `_SLUG_RE` 校验,或 `create_refined_hypothesis` 返回 None 时 raise
- [x] ✅(批10) **`build_packet` 忽略 hypothesis 自带 recommended_skills 来选 handoff target** — kg/gap/🟢 · target_skill 空时落 file_drop 不命名 skill(experiment 路径已用 recommended_skills,影响有限) · 证据: `OmicsClaw-KG/omicsclaw_kg/handoff/builder.py:91,129` · 修复: target_skill 缺省时按序 `resolve_skill(fm.recommended_skills)` 再退 file_drop
- [x] ✅(批10) **Experiment.status 永不进 'running' — 死转换分支** — kg/bug/🟢 · UI 状态卡 'planned' 直到 eval 翻 'completed',进度误导 · 证据: `OmicsClaw-KG/omicsclaw_kg/handoff/feedback.py:422`, `omicsclaw_kg/handoff/builder.py:162`, `omicsclaw_kg/cli/cmd_experiment.py:168` · 修复: submit 时把 step 标 running,或首个记录 step 即把 experiment 标 running
- [x] ✅(批11) **content-hash 缓存短路图/wiki 写入 → 丢失的图永不重填** — kg/bug/🟡 · 缓存仅按 SHA256,与图状态解耦,删 graph.json 后重 ingest 报 cache hit 不重 merge · 证据: `OmicsClaw-KG/omicsclaw_kg/cli/cmd_ingest.py:86`, `omicsclaw_kg/ingest/webclip.py:100`, `omicsclaw_kg/cache.py:49` · 修复: cache hit 时校验图节点+wiki 页存在,缺失则从缓存抽取重跑 merge/upsert,或 skip 以图成员判定
- [x] ✅(批11) **webclip SSRF 守卫只校验初始 URL,urllib 静默跟随重定向到内网** — kg/risk/🟡 · 攻击页 302→169.254.169.254/127.0.0.1 被抓取摄入 · 证据: `OmicsClaw-KG/omicsclaw_kg/ingest/webclip.py:40,79`, `omicsclaw_kg/ingest/_security.py:28` · 修复: 自定义 opener 禁自动重定向(或每跳 `validate_url` 并限跳数),逐跳重解析 IP

---

## 建议执行顺序(前 8 步解锁统一流程)

1. **统一 KG LLM provider**(A-1):在 `omicsclaw_kg.llm` 加 OpenAI 兼容 client 并由 `LLM_API_KEY`/`LLM_BASE_URL` 注入,一次打通步骤(1)ingest 与(2)idea 生成——这是阻断面最广的单点。
2. **修复自主结果回报**(A-2 + A-3):自主 finalizer 产出 `result.json` 契约 + 返回含路径/media 的结构化 digest,让步骤(4)对主执行引擎恢复可见与回链。
3. **修复 GEO 下载 tuple bug**(A-4):恢复 literature→分析的数据交付,这是 (1)→(3) 数据流的硬前提。
4. **堵住 `discover_file` trust 绕过**(B-1):在打开远程 routers 前补上安全边界,避免后续放大为远程任意读。
5. **加 outbox executor + record-result 回写**(E:HandoffPacket/submit/record-result 三项合并):实现 idea→analysis→verdict 的确定性闭环,替代"LLM 自觉调用",同步补 App `/api/kg/record-result` 代理让 KG Explorer Eval 不再 400。
6. **收敛 literature 抽取到 KG ingest 单一真源**(D-1):让 literature 技能/intake 复用同一抽取并触发 KG ingest,消除三套分叉。
7. **收敛 formalize 双契约 + 加 thread↔source 作用域**(C-formalize + E-(1) thread scope):定一套 HTTP 表面并让 idea grounding 真正按 thread 的 literature。
8. **构建 Bench "Write" 报告阶段**(E-(4) Write):接上终端阶段,把 run artifacts+verdicts 写回 KG/workspace,让 (1)→(2)→(3)→(4) 首次端到端连通。

---

## 附:codex (gpt-5.5 xhigh) 交叉验证结论

**方法**:codex 以 read-only 沙箱独立打开三个仓库,对 A–E 全部条目逐条核对所引用的 `file:line`(F 抽检)。

**总体结论:与审计高度一致** —— A–E 绝大多数 **CONFIRMED**;**无需删除的误报**;**未发现高置信度遗漏项**(独立佐证审计覆盖面)。仅 2 项 PARTIAL(真实但范围被夸大,已就地校正),5 项 E 上调严重度,执行顺序 1 处微调。

### PARTIAL(真实但已就地校正)
- **A-1 KG LLM 锁 Anthropic** → 范围收窄:chat-agent ingest 已用平台 OpenAI 适配器(`kg_tools.py:462`);仍 Anthropic-locked 的是 KG CLI ingest + 全部 KG HTTP ideation/formalize/experiment-design + desktop formalize。idea 生成整条仍断 → **维持 Critical**。
- **A-3 自主 digest 无路径/media** → digest 含纯文本路径但非 JSON/无 media;媒体与 session 回写仅解析 JSON,故「图不内联、不回链」成立。

### 严重度上调(🟡→🟠 High,已就地标 `codex↑High`)
- **outbox executor 无消费者 / experiment submit 不启动 / record-result 回写**——缺它们就没有确定性的 idea→analysis→verdict 闭环。
- **Bench「Write」报告阶段**——报告阶段(步骤 4)目前是 placeholder,对统一平台目标属 High。
- **SSE 断连未 set cancel_event**——长分析下关闭客户端会遗留子进程,High 运维风险。

### 执行顺序微调(codex)
把 **D-1(收敛 literature 抽取到 KG ingest)提前,与 A-4(GEO 修复)并行**——只修 GEO 能恢复文件下载,但在 KG ingest 统一前 literature 仍无法 ground ideation。
> codex 建议序:**A-1 → A-2/A-3 → A-4 + D-1 → B-1 → outbox executor + record-result → formalize/thread-source 作用域 → Bench/KG 表面收敛 → Write 阶段**。

### Claude 二次核验(对 codex 结论)
- 独立复核 4 个 Critical:全部 CONFIRMED(唯一细节:A-4 首个 TypeError 在 `downloader.py:114` 而非 115)。
- 复核 A-1 争议点:已读 `kg_tools.py:462`,确认后端 chat-agent ingest 走 OpenAI 适配器(codex 收窄成立),但 ideation/formalize 路径确为 Anthropic-locked → Critical 不变。
- 三方对 A–E 结论一致,可据此开工。

> OmicsClaw is a research and educational tool for multi-omics analysis. It is not a medical device and does not provide clinical diagnoses. Consult a domain expert before making decisions based on these results.