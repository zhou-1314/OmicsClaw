<a id="top"></a>

<div align="center">

<a href="https://github.com/TianGzlab/OmicsClaw">
  <img src="docs/images/OmicsClaw_banner.jpeg" alt="OmicsClaw — Local-first AI for Multi-Omics Workflows" width="100%"/>
</a>

<h3>Local-first AI research partner for multi-omics analysis</h3>

<p>Chat with your workflows · run reproducible skills · keep data local · resume with memory</p>

<p>
  <b>English</b> ·
  <a href="README_zh-CN.md"><b>简体中文</b></a> ·
  <a href="#-whats-new"><b>What's New</b></a> ·
  <a href="#-quick-start"><b>Quick Start</b></a> ·
  <a href="#-architecture"><b>Architecture</b></a> ·
  <a href="#-domains"><b>Domains</b></a> ·
  <a href="https://TianGzlab.github.io/OmicsClaw/"><b>Docs Site</b></a>
</p>

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![CI](https://github.com/TianGzlab/OmicsClaw/actions/workflows/pr-ci.yml/badge.svg)](https://github.com/TianGzlab/OmicsClaw/actions/workflows/pr-ci.yml)
[![Website](https://img.shields.io/badge/Website-Live-brightgreen.svg)](https://TianGzlab.github.io/OmicsClaw/)
[![Desktop App](https://img.shields.io/github/v/tag/TianGzlab/OmicsClaw?sort=semver&filter=v*&label=desktop%20app&color=blue&cacheSeconds=600)](https://github.com/TianGzlab/OmicsClaw/releases/latest)
[![Installer Downloads](https://img.shields.io/github/downloads/TianGzlab/OmicsClaw/total?label=installer%20downloads&color=brightgreen&cacheSeconds=600)](https://github.com/TianGzlab/OmicsClaw/releases)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-lightgrey)](https://github.com/TianGzlab/OmicsClaw/releases/latest)

</div>

> **OmicsClaw turns local multi-omics tools into AI-callable skills.** The LLM plans and operates; Python, R, and CLI tools process your data in a local or remote runtime — raw matrices never leave your machine. One agent loop powers a terminal CLI, a desktop app, and nine chat platforms, all backed by graph memory so your analyses resume instead of restarting.

## 📢 What's New

- **🤝 Consensus runtime** — multi-method consensus is now a declarative workflow runtime. Fan out N spatial-clustering or single-cell methods, then merge them with verified typed operators or an exploratory LLM synthesis. Triggered by the `consensus-domains` and `sc-consensus-clustering` skills.
- **🧠 Autonomous Analysis Path** — an Analysis Router can parameterize an exact skill from your data, or run a generated-code analysis with approval-gated workspace writes and bounded LLM repair.
- **⚡ Prompt-prefix caching** — automatic provider cache hits across turns to cut latency and token spend.
- **🖥️ Desktop upgrades** — a live to-do task list with planning guidance, an interactive `ask_user` choice tool, and LLM-generated session titles.

<details>
<summary><b>Earlier highlights</b></summary>

- **Providers** — live Ollama model discovery with tool-capability tagging, plus `qwen3.7-max` on DashScope.
- **Surfaces umbrella** — CLI, Desktop, and Channels unified behind one dispatch + typed event stream.
- **Loop health** — ping-pong / repeated-failure pathology detection with soft self-correction.

</details>

## 🖥️ App Workspace

<p align="center">
  <img src="docs/images/omicsclaw-app-overview.png" alt="OmicsClaw App showing connected backend, AutoAgent, datasets, skills, memory, remote bridge, and multi-omics analysis cards" width="94%"/>
</p>

<p align="center">
  <b>One workspace for chat, datasets, skills, execution, memory, and analysis outputs.</b>
</p>

<p align="center">
  <a href="https://github.com/TianGzlab/OmicsClaw/releases/latest"><b>📥 Download the OmicsClaw Desktop App</b></a>
  &nbsp;·&nbsp;
  <a href="https://github.com/TianGzlab/OmicsClaw/releases"><b>All releases</b></a>
  &nbsp;·&nbsp;
  <a href="https://github.com/TianGzlab/OmicsClaw/releases/latest/download/SHA256SUMS.txt"><b>SHA256SUMS</b></a>
</p>

The **[Releases](https://github.com/TianGzlab/OmicsClaw/releases)** tab hosts the prebuilt desktop installers — the same `oc desktop-server` the CLI ships, wrapped in a chat-ready Electron UI. Pick the asset for your platform:

| Platform | Installer |
|---|---|
| <picture><source media="(prefers-color-scheme: dark)" srcset="https://api.iconify.design/simple-icons:apple.svg?color=%23ffffff"><img alt="" width="14" height="14" src="https://api.iconify.design/simple-icons:apple.svg?color=%23000000"></picture> **macOS — Apple Silicon** (M1 / M2 / M3 / M4) | [`OmicsClaw-<ver>-arm64.dmg`](https://github.com/TianGzlab/OmicsClaw/releases/latest) |
| <picture><source media="(prefers-color-scheme: dark)" srcset="https://api.iconify.design/simple-icons:apple.svg?color=%23ffffff"><img alt="" width="14" height="14" src="https://api.iconify.design/simple-icons:apple.svg?color=%23000000"></picture> **macOS — Intel** | [`OmicsClaw-<ver>-x64.dmg`](https://github.com/TianGzlab/OmicsClaw/releases/latest) |
| <picture><source media="(prefers-color-scheme: dark)" srcset="https://api.iconify.design/simple-icons:windows.svg?color=%23ffffff"><img alt="" width="14" height="14" src="https://api.iconify.design/simple-icons:windows.svg?color=%230078D4"></picture> **Windows — x64 / ARM64** | [`OmicsClaw.Setup.<ver>-x64.exe`](https://github.com/TianGzlab/OmicsClaw/releases/latest) · [`OmicsClaw.Setup.<ver>-arm64.exe`](https://github.com/TianGzlab/OmicsClaw/releases/latest) |
| <picture><source media="(prefers-color-scheme: dark)" srcset="https://api.iconify.design/simple-icons:linux.svg?color=%23ffffff"><img alt="" width="14" height="14" src="https://api.iconify.design/simple-icons:linux.svg?color=%23000000"></picture> **Linux — x64** | [`.AppImage`](https://github.com/TianGzlab/OmicsClaw/releases/latest) · [`.deb`](https://github.com/TianGzlab/OmicsClaw/releases/latest) · [`.rpm`](https://github.com/TianGzlab/OmicsClaw/releases/latest) |
| <picture><source media="(prefers-color-scheme: dark)" srcset="https://api.iconify.design/simple-icons:linux.svg?color=%23ffffff"><img alt="" width="14" height="14" src="https://api.iconify.design/simple-icons:linux.svg?color=%23000000"></picture> **Linux — ARM64** | [`.AppImage`](https://github.com/TianGzlab/OmicsClaw/releases/latest) |

> Verify each download against `SHA256SUMS.txt` published alongside the installers. The desktop client and the CLI talk to the same backend — analyses, memory, and remote runtimes stay portable across both.

## 💡 Why OmicsClaw?

| Common pain | OmicsClaw answer |
|---|---|
| Analyses restart from zero | Persistent workspace, sessions, and graph memory |
| Python, R, and CLI tools are scattered | Unified skill runner plus natural-language routing |
| Large data lives on servers | Local UI with remote Linux execution over SSH |
| Reports, artifacts, and parameters drift | Standard skill output contracts and reproducible demos |

## ✨ Capabilities

| | | | |
|---|---|---|---|
| 🧠 **Memory**<br/>Sessions, preferences, lineage | 🔒 **Local-first**<br/>Raw data stays in your runtime | 🧰 **95 skills**<br/>Generated catalog + demos | 🧭 **Smart routing**<br/>Natural language to tools |
| 💬 **CLI Surface**<br/>`oc interactive`, `oc tui` | 🌐 **Desktop Surface**<br/>FastAPI for desktop/web | 📨 **Channel Surface**<br/>9 IM adapters (Telegram, Feishu, …) | 📡 **Remote mode**<br/>SSH tunnel to Linux servers |
| 🤝 **Consensus**<br/>Multi-method merge | 🤖 **Autonomous path**<br/>Router + assisted params | 🔌 **Any LLM**<br/>OpenAI-compatible providers | 📊 **Reproducible**<br/>Figures + data + report |

<details>
<summary><b>Autonomous Analysis Path — how routing works</b></summary>

OmicsClaw prefers a matching built-in skill, but ships a first-class autonomous path for everything else. Routing is **always on and assistive** — there is no mode switch:

- **Exact skill match** gets **data-grounded assisted parameterization**: the skill choice stays deterministic while the outer LLM recommends the method and parameters *within* it — grounded in the matched `SKILL.md` method menu and an `inspect_data` schema — asking a focused question only on consequential ambiguity.
- **Partial / No skill match** is delegated to the autonomous code path.

Generated-code analysis runs in the single autonomous engine — the **Autonomous Code Mini-Agent** (`omicsclaw/autonomous/`): a bounded, tiered-isolation Jupyter-kernel agent that drives vetted skills through a curated `oc` handle and gates acceptance on a replay rerun.

Design note: [ADR 0032](docs/adr/0032-autonomous-code-mini-agent.md) defines this fallback's architecture — a bounded autonomous code mini-agent with curated skill handles, a persistent Jupyter kernel under **tiered isolation** (bubblewrap OS envelope when available, in-kernel guard otherwise), and replay validation. As of the 2026-06-22 single-engine consolidation it is the **only** autonomous engine — always on, no flag, no legacy one-shot runner. The earlier `off`/`assist`/`auto` router-mode selector (`OMICSCLAW_ANALYSIS_ROUTER_MODE`) was removed in the same consolidation.

</details>

## 🏗️ Architecture

Three Surfaces, **one agent loop**. Every entry point builds a `MessageEnvelope` and dispatches it into the same typed event stream — so a fault in one surface never leaks into another, and skills, memory, and remote runtimes are shared by all.

```mermaid
flowchart TD
    U["🧑‍🔬 You — chat · commands · data"]

    subgraph Surfaces["🧭 Surfaces"]
        CLI["💬 CLI<br/>oc interactive · oc tui"]
        DESK["🌐 Desktop<br/>oc desktop-server · FastAPI/SSE"]
        CHAN["📨 Channels<br/>9 IM adapters"]
    end

    DISPATCH["⚙️ dispatch envelope → typed event stream"]
    LOOP["🔁 Agent loop<br/>plan → tool calls → results → repeat<br/>pathology guard · approval gates"]

    subgraph Capabilities["🧰 Capabilities"]
        SKILLS["🧪 Skill runner<br/>95 skills · 8 domains"]
        MEMORY["🧠 Graph memory<br/>sessions · datasets · lineage"]
        PROV["🔌 Providers<br/>any OpenAI-compatible LLM"]
        REMOTE["📡 Remote<br/>SSH to Linux servers"]
    end

    OUT["📊 Reproducible outputs<br/>figures · figure_data · report.md"]

    U --> CLI & DESK & CHAN
    CLI --> DISPATCH
    DESK --> DISPATCH
    CHAN --> DISPATCH
    DISPATCH --> LOOP
    LOOP --> SKILLS & MEMORY & PROV
    SKILLS --> REMOTE
    SKILLS --> OUT
    MEMORY -. resumes across runs .-> LOOP
```

Beyond the single chat turn, two independent subsystems run longer jobs: a **multi-agent research pipeline** (`omicsclaw/agents/`, intake → plan → research → execute → analyze → write → review) and an **AutoAgent** experiment/optimization loop. Full breakdown in [`docs/architecture/`](docs/architecture/).

## ⚡ Quick Start

```bash
git clone https://github.com/TianGzlab/OmicsClaw.git
cd OmicsClaw
bash 0_setup_env.sh
conda activate OmicsClaw
oc list
oc run spatial-preprocess --demo --output /tmp/omicsclaw_demo
```

Configure chat and runtime settings:

```bash
oc onboard
oc interactive
```

If `oc` is not on `PATH`, use `python omicsclaw.py <command>`.

<p align="center">
  <img src="docs/images/OmicsClaw_configure_fast.png" alt="OmicsClaw setup wizard" width="82%"/>
</p>

## 🧭 Interfaces

Pick the entry point that fits your workflow — they all reach the same backend.

| Surface | Entry point | Use it for |
|---|---|---|
| 💬 **CLI Surface** | `oc interactive` / `oc tui` | Natural-language workflows in the terminal (REPL + full-screen TUI) |
| 🌐 **Desktop Surface** | `oc desktop-server` | FastAPI backend consumed by OmicsClaw-App and browser frontends |
| 📨 **Channel Surface** | `python -m omicsclaw.surfaces.channels --channels <names>` | Telegram, Feishu, Slack, Discord, WeChat (incl. WeCom), DingTalk, iMessage, Email, QQ |
| 🧪 Skill runner (non-Surface) | `oc run <skill> --demo` | Reproducible one-shot analysis |
| 🔌 MCP (non-Surface) | `oc mcp add ...` | External tool integration |
| 📡 Remote mode | `oc desktop-server` over SSH | Server-side data and jobs |

Remote mode uses `127.0.0.1`, SSH tunneling, and `OMICSCLAW_REMOTE_AUTH_TOKEN`. See [remote execution](docs/engineering/remote-execution.mdx) and the [legacy remote guide](docs/_legacy/remote-connection-guide.md).

## 📦 Installation

| Path | Best for | Command |
|---|---|---|
| 🥇 **Full conda** | Real analysis with Python + R + bioinformatics CLIs | `bash 0_setup_env.sh` |
| 🪶 **Lightweight venv** | Chat, routing, dev, Python-only skills | `pip install -e ".[interactive]"` |
| 🖥️ **Desktop/web backend** | OmicsClaw-App or browser frontends | `oc desktop-server --host 127.0.0.1 --port 8765` |
| 🧠 **Memory API** | Inspect graph memory over HTTP | `pip install -e ".[memory]"` then `oc memory-server` |

📖 Details: [installation guide](docs/_legacy/INSTALLATION.md), [quickstart](docs/introduction/quickstart.mdx). Dependencies live in [`pyproject.toml`](pyproject.toml), [`environment.yml`](environment.yml), and [`0_setup_env.sh`](0_setup_env.sh).

## 🧬 Domains

`oc list` and `skills/catalog.json` currently agree on **95 registered skills** across **8 domains**.

| Domain | Skills | Examples | Docs |
|---|---|---|---|
| 🧫 Spatial transcriptomics | 19 | QC, domains, annotation, deconvolution, CNV, trajectory | [spatial](docs/domains/spatial.mdx) |
| 🔬 Single-cell omics | 34 | QC, clustering, annotation, doublets, velocity, GRN | [singlecell](docs/domains/singlecell.mdx) |
| 🧬 Genomics | 10 | QC, alignment, variants, CNV, assembly, epigenomics | [genomics](docs/domains/genomics.mdx) |
| 🧪 Proteomics | 8 | DIA/DDA, PTM, networks, biomarkers | [proteomics](docs/domains/proteomics.mdx) |
| ⚗️ Metabolomics | 8 | Peaks, normalization, annotation, pathways | [metabolomics](docs/domains/metabolomics.mdx) |
| 📈 Bulk RNA-seq | 13 | DE, enrichment, co-expression, deconvolution, survival | [bulkrna](docs/domains/bulkrna.mdx) |
| 🧠 Orchestration | 2 | Routing, planning, literature support | [orchestrator](docs/domains/orchestrator.mdx) |
| 📚 Literature | 1 | PDF/DOI/PubMed/GEO parsing and dataset handoff | — |

Run `oc list` for the current CLI catalog.

## 🧠 Memory

Graph-backed memory at `omicsclaw/memory/` carries your sessions, datasets, analyses, preferences, and insights across runs — chat history and lineage come back when you reopen any surface. Each surface stays isolated so state never leaks across users or workspaces.

| Surface | Memory scope |
|---|---|
| CLI / TUI | Per workspace path |
| Desktop app | Per launch (or per signed-in user) |
| Telegram / Feishu bot | Per platform user |

A reserved `__shared__` pool (core agent identity, knowledge handbook guards, glossary) is the one thing every surface reads back automatically. Full vocabulary and architecture in [`docs/CONTEXT.md`](docs/CONTEXT.md).

## 📚 Documentation

| Topic | Where |
|---|---|
| 🚀 Quickstart & onboarding | [introduction/quickstart](docs/introduction/quickstart.mdx) |
| 🏗️ Architecture | [`docs/architecture/`](docs/architecture/) |
| 🧬 Domain guides | [spatial](docs/domains/spatial.mdx) · [singlecell](docs/domains/singlecell.mdx) · [genomics](docs/domains/genomics.mdx) · [proteomics](docs/domains/proteomics.mdx) · [metabolomics](docs/domains/metabolomics.mdx) · [bulkrna](docs/domains/bulkrna.mdx) |
| 🧠 Domain language & memory | [`docs/CONTEXT.md`](docs/CONTEXT.md) |
| 📡 Remote execution | [engineering/remote-execution](docs/engineering/remote-execution.mdx) |
| 🔒 Safety & data privacy | [data privacy](docs/safety/data-privacy.mdx) · [rules & disclaimer](docs/safety/rules-and-disclaimer.mdx) |
| 🛠️ Building skills | [CONTRIBUTING.md](CONTRIBUTING.md) · [`templates/skill/`](templates/skill/) |
| 🤖 Repo / agent contracts | [AGENTS.md](AGENTS.md) |

Hosted docs site: **<https://TianGzlab.github.io/OmicsClaw/>**

## ❓ FAQ

<details>
<summary><b>Does OmicsClaw upload my raw data?</b></summary>

No. Skills run in the configured local or remote runtime; LLM calls should receive context and tool results, not raw omics matrices.

</details>

<details>
<summary><b>Which installation path should I use?</b></summary>

Use `bash 0_setup_env.sh` for real analysis. Use the lightweight venv only for chat, routing, development, or Python-only skills.

</details>

<details>
<summary><b>Can the desktop App run jobs on a server?</b></summary>

Yes. Run `oc desktop-server` on the remote Linux host, keep it bound to `127.0.0.1`, and connect through the App's SSH tunnel runtime.

</details>

## ⚠️ Safety

| Rule | Meaning |
|---|---|
| 🔒 Local-first | Raw data processing happens in your local or remote runtime |
| 🧪 Research use only | Not a medical device; no clinical diagnosis |
| 👩‍🔬 Expert review | Validate scientific outputs before decisions |
| 🔐 Remote caution | Use localhost binding, SSH tunnels, and tokens |

> OmicsClaw is a research and educational tool for multi-omics analysis. It is not a medical device and does not provide clinical diagnoses. Consult a domain expert before making decisions based on these results.

See [data privacy](docs/safety/data-privacy.mdx) and [rules/disclaimer](docs/safety/rules-and-disclaimer.mdx).

## 👥 Community

Maintainers: Luyi Tian (Principal Investigator), Weige Zhou (Lead Developer), Liying Chen (Developer), and Pengfei Yin (Developer).

🐛 [Issues](https://github.com/TianGzlab/OmicsClaw/issues) · 💬 [Discussions](https://github.com/TianGzlab/OmicsClaw/discussions) · 📖 [Docs](https://TianGzlab.github.io/OmicsClaw/)

<table>
  <tr>
    <td align="center" width="30%">
      <img src="docs/images/IMG_3729.JPG" alt="OmicsClaw WeChat group" width="180"/>
      <br/>
      <b>WeChat group</b>
      <br/>
      <sub>Scan to join</sub>
    </td>
    <td valign="middle" width="70%">
      Scan to join our WeChat group to share analysis tips, report issues, and discuss multi-omics AI workflows.
    </td>
  </tr>
</table>

<a href="https://github.com/TianGzlab/OmicsClaw/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=TianGzlab/OmicsClaw" alt="OmicsClaw contributors"/>
</a>

## 🙏 Acknowledgments

Architecture, skill design, and local-first philosophy are inspired by **[ClawBio](https://github.com/ClawBio/ClawBio)**, an early bioinformatics-native AI agent skill library. Memory and session-continuity patterns are inspired by [Nocturne Memory](https://github.com/Dataojitori/nocturne_memory).

## 🛠️ Contributing

- **New skills**: see [CONTRIBUTING.md](CONTRIBUTING.md) and the v2 scaffold under [`templates/skill/`](templates/skill/).
- **Repository / agent work**: see [AGENTS.md](AGENTS.md) — covers contract tests, provider contracts, skill runner, and architecture references.

## 📜 License

Apache-2.0. See [LICENSE](LICENSE).

## 📝 Citation

```bibtex
@software{omicsclaw2026,
  title = {OmicsClaw: A Memory-Enabled AI Agent for Multi-Omics Analysis},
  author = {Zhou, Weige and Chen, Liying and Yin, Pengfei and Tian, Luyi},
  year = {2026},
  url = {https://github.com/TianGzlab/OmicsClaw}
}
```

[⬆ Back to top](#top)

## 项目进展（Dream 自动维护）
<!-- DREAM:START -->
### 当前状态
- OmicsClaw 的 autonomous analysis 已收敛为**单一引擎**（Autonomous Code Mini-Agent）
- ADR 0032 (Autonomous Code Mini-Agent) 于 2026-06-21 接受，2026-06-22 完成单引擎合并（移除 flag 与 legacy 一次性 runner）
- 现实现（`omicsclaw/autonomous/`）**永远开、无 flag**：有 bwrap 走 OS envelope、无 bwrap 走进程内 guard 的分层隔离；mini-agent 全套 + autonomous workspace + bot 路由共 78 passed
- 分析输出已**按研究课题（project）分组**（ADR 0035）：从平铺 `output/<skill>__ts__uuid8/` 改为 `output/<project>/<skill>__ts__<dataset>-<uid8>/`，每课题一个 `project_meta.json` + 可重建 `index.jsonl`；四 surface 收敛到单一 resolver `omicsclaw/common/run_paths.py`
- Skill 审计系统已有稳定的 v2 表示底座；按 [2026-07-13 M0–M3 验收基线](docs/reviews/2026-07-13-skill-audit-system-design-assessment.md)，现已关闭 promoted quarantine、正式 v2 fail-closed，并落地 trace-provable workflow 的 facade-free acquisition 泛化、8 域 routing oracle、RET-04 `.h5ad` 探针/auto-route 三态门和 RET-04b 显式 skill 统一执行门；下一主线是 RET-05 compatibility DAG/topo chain，剩余长期重点还有复杂 Python/artifact 泛化与人工门控演化

### 最近进展（近7天）
- 2026-07-13: 完成 [RET-04b 显式 skill 统一执行门与 Ask Codex 独立复审](docs/reviews/2026-07-13-ret04b-ask-codex-review.md) — shared runner、sync/async、agent 与 pipeline 在输出分配和 subprocess 前共享 `preflight_skill_execution()`；`path_kinds=file|directory|freeform` 对称约束并生成到 SKILL.md；全库审计校准 12 个目录消费者及公开 file types，routing/execution 共享 `InputProfile.path_kind`，正确区分 `.zarr/` 与普通点号目录；空 session/缺失输入 fail closed；169 项定向回归通过，四轮只读复审最终 PASS（无 Blocker/High）
- 2026-07-13: 完成 [RET-04 precondition 安全纵切与 Ask Codex 独立复审](docs/reviews/2026-07-13-ret04-ask-codex-review.md) — `interface.inputs` 全量进入 registry；新增 cached backed-mode `.h5ad` profile 与 `eligible/needs_preparation/blocked` evaluator；resolver/AnalysisRouter/route context/`skill=auto` 共享 `execution_ready` 门禁，探测失败与未验证身份均 fail closed，上传文件探测和执行绑定同一 session；修正 `spatial-domains` 可自动 PCA 却声明 `X_pca` 硬前置的合同错误；routing oracle 扩为 29 cases 并新增 precondition accuracy=1.000 CI 门；三轮只读复审最终 PASS（无 Blocker/High）
- 2026-07-13: 完成 acquisition 泛化与全域 routing oracle — promotion 读取 `skill_calls.jsonl` + manifest steps，对可证明 lineage 的 workflow 生成显式 `run_skill` 脚本并持久化 abstraction/fallback evidence；以 2 inputs × 2 parameter sets 验收无 facade 复用；新增 26-case/8-domain oracle，全局及逐域 top1/top3/domain/decision 均 1.000、alias hallucination 0.000，并接入 PR CI
- 2026-07-13: 完成 [Claude Code 交叉审查整改](docs/reviews/2026-07-13-acquisition-routing-oracle-claude-cross-validation.md) — PR CI 显式运行 acquisition/resolver/oracle/analysis-router 回归；补齐 two-call `step:1` 真执行、6 类 AST/trace fail-closed、oracle 防刷分/partial/幻觉 alias/exit-code 负向测试；移除 benchmark-near triggers 后以通用 scRNA/scATAC 模态与 workflow-stage 信号保持 8 域指标 1.000；进一步收紧 meta-routing，避免 `route/choose` 科学分析语句误进 orchestrator，并把两条 hard negative 纳入 26-case oracle；CI 同构集合 228 passed，新增 bot 接线 32 passed
- 2026-07-13: 完成 skill 审计首轮修复 — 无 sandbox/依赖受限的 promoted code 转入 `skills/.quarantine/` 且 registry 不可见；禁用全局 latest 晋升；正式 v2 fail-closed；resolver 接入 lifecycle、validation 和 structured `skip_when`（含否定极性/跨域 redirect）；routing budget 与全量 skill lint 接入 PR CI；`OmicsClaw` 环境扩大回归 425 passed / 2 skipped
- 2026-07-13: 建立 skill 表示→获取→检索→演化的统一验收规格，并完成代码级诊断：95/95 manifest 静态门通过、定向套件 285 passed / 3 skipped / 5 xfailed；确认 resolver 未消费 `skip_when`、无 bwrap 的 promoted draft 仍可正式注册、evolution 尚只有重复成功提示原型
- 2026-06-24: ADR 0035 接受并实现 — project 作用域输出目录 + 可重建 run 索引；单一 resolver（`run_paths.py`）接入 CLI/agent/channel/autonomous，新增 `oc project list|new|use|current|clear|reindex` 与 `oc run --project`，后端 `/outputs/latest` 下钻一层 + `/outputs/{run_id}/files` 经索引定位，前端 OutputPanel 增 project 维度；经 Codex（gpt-5.5 xhigh）审查补 10 条硬约束（run_id 全局唯一、原子建目录、禁后置改名、改名不动目录等），后端新增 13 个 run_paths 单测 + 嵌套-project 端点测试，134 passed
- 2026-06-22: 单引擎合并 — autonomous 收敛为唯一的 mini-agent 引擎、永远开、无 flag；隔离改为分层（bwrap OS envelope / 进程内 guard）；删除 legacy executor/permissions/policy 与一次性 runner
- 2026-06-21: ADR 0032 接受 — Autonomous Code Runner 升级为 bounded mini-agent（持久化 Jupyter kernel + bubblewrap 隔离 + replay 验证）
- 2026-06-21: ADR 0032 实现完成（`omicsclaw/autonomous/` 8 个新模块），经过架构评审和准确性评审两轮 Codex review
- 2026-06-21: 实现准确性评审发现并修复 7 个问题（kernel 超时重启、ReturnAnswer 检测、token 预算计费、skill call 限制等）
- 测试状态：mini-agent 全套 + autonomous workspace + bot 路由 78 passed（kernel 集成在无 IPC 环境优雅 skip），ruff 干净；legacy 一次性 runner 已删除

### 待办 / 开放问题
- ADR 0032 Open Questions 待解决：primary artifact mapping、nested skill-call replay 策略、mid-loop user input 协议、模型能力阈值基准
- 弱模型（本地 Ollama gemma ~12B）可能无法胜任 multi-step mini-agent loop，需 capability gate
<!-- DREAM:END -->
