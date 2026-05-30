<a id="top"></a>

<div align="center">

<a href="https://github.com/TianGzlab/OmicsClaw">
  <img src="docs/images/OmicsClaw_banner.jpeg" alt="OmicsClaw — 面向多组学工作流的本地优先 AI" width="100%"/>
</a>

<h3>面向多组学分析的本地优先 AI 研究助手</h3>

<p>用对话驱动工作流 · 运行可复现技能 · 数据留在本地 · 用记忆延续上下文</p>

<p>
  <a href="README.md"><b>English</b></a> ·
  <b>简体中文</b> ·
  <a href="#-最新动态"><b>最新动态</b></a> ·
  <a href="#-快速开始"><b>快速开始</b></a> ·
  <a href="#-架构"><b>架构</b></a> ·
  <a href="#-领域"><b>领域</b></a> ·
  <a href="https://TianGzlab.github.io/OmicsClaw/"><b>文档站</b></a>
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

> **OmicsClaw 把本地多组学工具变成 AI 可调用的技能。** LLM 负责规划与编排；Python、R、CLI 工具在你的本地或远程运行时里实际处理数据 —— 原始矩阵永不离开你的机器。一个 agent loop 同时驱动终端 CLI、桌面 App 和九个聊天平台，并由图记忆托底，让分析得以续接而非重来。

## 📢 最新动态

- **🤝 共识运行时** — 多方法共识现在是一个声明式工作流运行时。并行 fan-out N 个空间聚类或单细胞方法，再用经过验证的类型化算子或探索性 LLM 综合来合并。由 `consensus-domains` 与 `sc-consensus-clustering` 两个技能触发。
- **🧠 自主分析路径** — Analysis Router 可以基于你的数据为精确匹配的技能补全参数，或运行生成式代码分析，并对工作区写入做审批门控、对 LLM 修复做有界约束。
- **⚡ Prompt 前缀缓存** — 跨轮自动命中 provider 缓存，降低延迟与 token 开销。
- **🖥️ 桌面端升级** — 带规划引导的实时待办列表、交互式 `ask_user` 选择工具，以及 LLM 生成的会话标题。

<details>
<summary><b>更早的更新</b></summary>

- **Provider** — 实时发现 Ollama 模型并标注工具能力，新增 DashScope 上的 `qwen3.7-max`。
- **Surfaces 雨伞** — CLI、Desktop、Channels 统一到同一 dispatch + 类型化事件流。
- **循环健康** — ping-pong / 重复失败的 pathology 检测 + 软自纠。

</details>

## 🖥️ App 工作区

<p align="center">
  <img src="docs/images/omicsclaw-app-overview.png" alt="OmicsClaw App：连接后端、AutoAgent、数据集、技能、记忆、远程桥与多组学分析卡片" width="94%"/>
</p>

<p align="center">
  <b>一个工作区统一对话、数据集、技能、执行、记忆与分析产出。</b>
</p>

<p align="center">
  <a href="https://github.com/TianGzlab/OmicsClaw/releases/latest"><b>📥 下载 OmicsClaw 桌面应用</b></a>
  &nbsp;·&nbsp;
  <a href="https://github.com/TianGzlab/OmicsClaw/releases"><b>所有版本</b></a>
  &nbsp;·&nbsp;
  <a href="https://github.com/TianGzlab/OmicsClaw/releases/latest/download/SHA256SUMS.txt"><b>SHA256SUMS</b></a>
</p>

**[Releases](https://github.com/TianGzlab/OmicsClaw/releases)** 页提供预编译的桌面安装包——内置与 CLI 同源的 `oc desktop-server`，外层是开箱即用的 Electron 对话界面。按平台直接下载：

| 平台 | 安装包 |
|---|---|
| <picture><source media="(prefers-color-scheme: dark)" srcset="https://api.iconify.design/simple-icons:apple.svg?color=%23ffffff"><img alt="" width="14" height="14" src="https://api.iconify.design/simple-icons:apple.svg?color=%23000000"></picture> **macOS — Apple Silicon** (M1 / M2 / M3 / M4) | [`OmicsClaw-<ver>-arm64.dmg`](https://github.com/TianGzlab/OmicsClaw/releases/latest) |
| <picture><source media="(prefers-color-scheme: dark)" srcset="https://api.iconify.design/simple-icons:apple.svg?color=%23ffffff"><img alt="" width="14" height="14" src="https://api.iconify.design/simple-icons:apple.svg?color=%23000000"></picture> **macOS — Intel** | [`OmicsClaw-<ver>-x64.dmg`](https://github.com/TianGzlab/OmicsClaw/releases/latest) |
| <picture><source media="(prefers-color-scheme: dark)" srcset="https://api.iconify.design/simple-icons:windows.svg?color=%23ffffff"><img alt="" width="14" height="14" src="https://api.iconify.design/simple-icons:windows.svg?color=%230078D4"></picture> **Windows — x64 / ARM64** | [`OmicsClaw.Setup.<ver>-x64.exe`](https://github.com/TianGzlab/OmicsClaw/releases/latest) · [`OmicsClaw.Setup.<ver>-arm64.exe`](https://github.com/TianGzlab/OmicsClaw/releases/latest) |
| <picture><source media="(prefers-color-scheme: dark)" srcset="https://api.iconify.design/simple-icons:linux.svg?color=%23ffffff"><img alt="" width="14" height="14" src="https://api.iconify.design/simple-icons:linux.svg?color=%23000000"></picture> **Linux — x64** | [`.AppImage`](https://github.com/TianGzlab/OmicsClaw/releases/latest) · [`.deb`](https://github.com/TianGzlab/OmicsClaw/releases/latest) · [`.rpm`](https://github.com/TianGzlab/OmicsClaw/releases/latest) |
| <picture><source media="(prefers-color-scheme: dark)" srcset="https://api.iconify.design/simple-icons:linux.svg?color=%23ffffff"><img alt="" width="14" height="14" src="https://api.iconify.design/simple-icons:linux.svg?color=%23000000"></picture> **Linux — ARM64** | [`.AppImage`](https://github.com/TianGzlab/OmicsClaw/releases/latest) |

> 下载后用同 release 里的 `SHA256SUMS.txt` 校验完整性。桌面端与 CLI 共用同一后端，分析、记忆、远程运行时在两端之间无缝迁移。

## 💡 为什么选择 OmicsClaw？

| 常见痛点 | OmicsClaw 的回应 |
|---|---|
| 分析每次都从零开始 | 持久化的工作区、会话与图记忆 |
| Python、R、CLI 工具散落各处 | 统一的技能运行器 + 自然语言路由 |
| 大数据放在服务器上 | 本地 UI + 通过 SSH 远程在 Linux 上执行 |
| 报告、产物、参数互相漂移 | 标准化技能输出契约 + 可复现 demo |

## ✨ 能力

| | | | |
|---|---|---|---|
| 🧠 **记忆**<br/>会话、偏好、血缘 | 🔒 **本地优先**<br/>原始数据留在你的运行时 | 🧰 **89 个技能**<br/>自动生成目录 + demo | 🧭 **智能路由**<br/>自然语言映射到工具 |
| 💬 **CLI Surface**<br/>`oc interactive`、`oc tui` | 🌐 **Desktop Surface**<br/>给桌面/Web 前端用的 FastAPI | 📨 **Channel Surface**<br/>9 个 IM 适配器（Telegram、飞书 …） | 📡 **远程模式**<br/>SSH 隧道到 Linux 服务器 |
| 🤝 **共识**<br/>多方法合并 | 🤖 **自主路径**<br/>Router + 参数辅助 | 🔌 **任意 LLM**<br/>OpenAI 兼容 provider | 📊 **可复现**<br/>图 + 数据 + 报告 |

<details>
<summary><b>自主分析路径 —— 路由模式怎么工作</b></summary>

OmicsClaw 优先使用匹配的内置技能，但为其余情况内置了一等的自主路径。行为由 `OMICSCLAW_ANALYSIS_ROUTER_MODE=off|assist|auto` 控制（缺省 `assist`）：

- **`assist`** —— 精确匹配的技能获得**数据驱动的参数辅助**：技能选择保持确定性，由外层 LLM 在该技能*内部*推荐方法与参数 —— 以匹配技能的 `SKILL.md` 方法菜单和 `inspect_data` schema 为依据 —— 仅在关键歧义处问一个聚焦问题。
- **`auto`** —— **照写即跑**的字面路径：把 exact / no / partial 分析路由提交进既有的工具策略、审批、transcript 与完成结果流水线（无外层 LLM），并尊重请求中显式指定的方法。
- **`off`** —— 完全关闭 router。

旧的 `OMICSCLAW_ANALYSIS_ROUTER_ENABLED=true` 仍被当作 `auto` 接受。生成式代码分析在独立的 `omicsclaw/autonomous/` 运行器里执行，写入工作区需审批门控、LLM 修复有界、产出 skill 风格的 manifest / 完成报告。

</details>

## 🏗️ 架构

三个 Surface，**一个 agent loop**。每个入口都构造 `MessageEnvelope` 并 dispatch 进同一条类型化事件流 —— 任何一个 surface 故障都不会污染其他 surface，而技能、记忆、远程运行时由所有 surface 共享。

```mermaid
flowchart TD
    U["🧑‍🔬 你 — 对话 · 命令 · 数据"]

    subgraph Surfaces["🧭 Surfaces"]
        CLI["💬 CLI<br/>oc interactive · oc tui"]
        DESK["🌐 Desktop<br/>oc desktop-server · FastAPI/SSE"]
        CHAN["📨 Channels<br/>9 个 IM 适配器"]
    end

    DISPATCH["⚙️ dispatch envelope → 类型化事件流"]
    LOOP["🔁 Agent loop<br/>规划 → 工具调用 → 结果 → 循环<br/>pathology 守护 · 审批门控"]

    subgraph Capabilities["🧰 能力层"]
        SKILLS["🧪 技能运行器<br/>89 个技能 · 8 个领域"]
        MEMORY["🧠 图记忆<br/>会话 · 数据集 · 血缘"]
        PROV["🔌 Providers<br/>任意 OpenAI 兼容 LLM"]
        REMOTE["📡 远程<br/>SSH 到 Linux 服务器"]
    end

    OUT["📊 可复现产物<br/>figures · figure_data · report.md"]

    U --> CLI & DESK & CHAN
    CLI --> DISPATCH
    DESK --> DISPATCH
    CHAN --> DISPATCH
    DISPATCH --> LOOP
    LOOP --> SKILLS & MEMORY & PROV
    SKILLS --> REMOTE
    SKILLS --> OUT
    MEMORY -. 跨运行续接 .-> LOOP
```

在单次对话之外，还有两个独立子系统跑长任务：**多 agent 研究流水线**（`omicsclaw/agents/`，intake → plan → research → execute → analyze → write → review）与 **AutoAgent** 实验/优化循环。完整拆解见 [`docs/architecture/`](docs/architecture/)。

## ⚡ 快速开始

```bash
git clone https://github.com/TianGzlab/OmicsClaw.git
cd OmicsClaw
bash 0_setup_env.sh
conda activate OmicsClaw
oc list
oc run spatial-preprocess --demo --output /tmp/omicsclaw_demo
```

配置对话与运行时：

```bash
oc onboard
oc interactive
```

如果 `oc` 不在 `PATH` 中，用 `python omicsclaw.py <command>` 替代。

<p align="center">
  <img src="docs/images/OmicsClaw_configure_fast.png" alt="OmicsClaw 配置向导" width="82%"/>
</p>

## 🧭 接入方式

选择适合你工作流的入口 —— 它们最终都到达同一后端。

| Surface | 命令 | 用途 |
|---|---|---|
| 💬 **CLI Surface** | `oc interactive` / `oc tui` | 终端里的自然语言工作流（REPL + 全屏 TUI） |
| 🌐 **Desktop Surface** | `oc desktop-server` | 给 OmicsClaw-App 与浏览器前端用的 FastAPI 后端 |
| 📨 **Channel Surface** | `python -m omicsclaw.surfaces.channels --channels <names>` | Telegram、飞书、Slack、Discord、微信（含企微）、钉钉、iMessage、邮件、QQ |
| 🧪 技能运行器（非 Surface） | `oc run <skill> --demo` | 一次性可复现分析 |
| 🔌 MCP（非 Surface） | `oc mcp add ...` | 外部工具接入 |
| 📡 远程模式 | SSH 上跑 `oc desktop-server` | 服务端数据与任务 |

远程模式使用 `127.0.0.1` + SSH 隧道 + `OMICSCLAW_REMOTE_AUTH_TOKEN`。详见 [remote execution](docs/engineering/remote-execution.mdx) 与 [legacy remote guide](docs/_legacy/remote-connection-guide.md)。

## 📦 安装

| 路径 | 适用 | 命令 |
|---|---|---|
| 🥇 **完整 conda** | 用 Python + R + 生信 CLI 的真实分析 | `bash 0_setup_env.sh` |
| 🪶 **轻量 venv** | 对话、路由、开发、纯 Python 技能 | `pip install -e ".[interactive]"` |
| 🖥️ **桌面/Web 后端** | OmicsClaw-App 或浏览器前端 | `oc desktop-server --host 127.0.0.1 --port 8765` |
| 🧠 **记忆 API** | 通过 HTTP 检视图记忆 | `pip install -e ".[memory]"` 然后 `oc memory-server` |

📖 详细见 [安装指南](docs/_legacy/INSTALLATION.md) 与 [快速上手](docs/introduction/quickstart.mdx)。依赖分别由 [`pyproject.toml`](pyproject.toml)、[`environment.yml`](environment.yml)、[`0_setup_env.sh`](0_setup_env.sh) 管理。

## 🧬 领域

`oc list` 与 `skills/catalog.json` 当前一致维护 **89 个已注册技能**，分布在 **8 个领域**。

| 领域 | 技能数 | 示例技能 | 文档 |
|---|---|---|---|
| 🧫 空间转录组 | 17 | QC、domain、注释、解卷积、CNV、轨迹 | [spatial](docs/domains/spatial.mdx) |
| 🔬 单细胞组学 | 30 | QC、聚类、注释、doublet、velocity、GRN | [singlecell](docs/domains/singlecell.mdx) |
| 🧬 基因组学 | 10 | QC、比对、变异、CNV、组装、表观 | [genomics](docs/domains/genomics.mdx) |
| 🧪 蛋白组学 | 8 | DIA/DDA、PTM、网络、biomarker | [proteomics](docs/domains/proteomics.mdx) |
| ⚗️ 代谢组学 | 8 | 峰、归一化、注释、通路 | [metabolomics](docs/domains/metabolomics.mdx) |
| 📈 Bulk RNA-seq | 13 | DE、富集、共表达、解卷积、生存 | [bulkrna](docs/domains/bulkrna.mdx) |
| 🧠 编排 | 2 | 路由、规划、文献支持 | [orchestrator](docs/domains/orchestrator.mdx) |
| 📚 文献 | 1 | PDF/DOI/PubMed/GEO 解析与数据集交接 | — |

完整 CLI 技能列表运行 `oc list` 查看。

## 🧠 记忆

`omicsclaw/memory/` 下的图记忆把会话、数据集、分析、偏好、洞察跨运行串起来 —— 重开任意入口都能找回对话历史与血缘。每个入口相互隔离，状态不会在用户或工作区之间泄漏。

| 入口 | 记忆作用域 |
|---|---|
| CLI / TUI | 按工作区路径 |
| 桌面 App | 按启动（或登录用户） |
| Telegram / 飞书 Bot | 按平台用户 |

保留的 `__shared__` 池（核心 agent 身份、术语表）是所有入口都会自动回读的部分。完整术语与架构详见 [`docs/CONTEXT.md`](docs/CONTEXT.md)。

## 📚 文档

| 主题 | 位置 |
|---|---|
| 🚀 快速上手与配置 | [introduction/quickstart](docs/introduction/quickstart.mdx) |
| 🏗️ 架构 | [`docs/architecture/`](docs/architecture/) |
| 🧬 领域指南 | [spatial](docs/domains/spatial.mdx) · [singlecell](docs/domains/singlecell.mdx) · [genomics](docs/domains/genomics.mdx) · [proteomics](docs/domains/proteomics.mdx) · [metabolomics](docs/domains/metabolomics.mdx) · [bulkrna](docs/domains/bulkrna.mdx) |
| 🧠 领域语言与记忆 | [`docs/CONTEXT.md`](docs/CONTEXT.md) |
| 📡 远程执行 | [engineering/remote-execution](docs/engineering/remote-execution.mdx) |
| 🔒 安全与数据隐私 | [数据隐私](docs/safety/data-privacy.mdx) · [规则与免责声明](docs/safety/rules-and-disclaimer.mdx) |
| 🛠️ 构建技能 | [CONTRIBUTING.md](CONTRIBUTING.md) · [`templates/skill/`](templates/skill/) |
| 🤖 仓库 / agent 契约 | [AGENTS.md](AGENTS.md) |

托管文档站：**<https://TianGzlab.github.io/OmicsClaw/>**

## ❓ FAQ

<details>
<summary><b>OmicsClaw 会上传我的原始数据吗？</b></summary>

不会。技能在你配置的本地或远程运行时里执行；LLM 调用收到的是上下文和工具结果，不包含原始组学矩阵。

</details>

<details>
<summary><b>我应该选哪种安装方式？</b></summary>

真实分析用 `bash 0_setup_env.sh`。轻量 venv 仅用于对话、路由、开发、纯 Python 技能。

</details>

<details>
<summary><b>桌面 App 能在服务器上跑任务吗？</b></summary>

可以。在远程 Linux 上运行 `oc desktop-server`，绑定 `127.0.0.1`，再通过 App 的 SSH 隧道运行时连接过来。

</details>

## ⚠️ 安全

| 规则 | 含义 |
|---|---|
| 🔒 本地优先 | 原始数据处理发生在你的本地或远程运行时 |
| 🧪 仅供研究 | 不是医疗器械，不提供临床诊断 |
| 👩‍🔬 专家复核 | 在做决策前由领域专家验证科学产出 |
| 🔐 远程谨慎 | 使用 localhost 绑定、SSH 隧道与 token |

> OmicsClaw 是一个用于多组学分析的研究与教育工具。它不是医疗器械，也不提供临床诊断。在基于这些结果做决策前，请咨询领域专家。

详见 [数据隐私](docs/safety/data-privacy.mdx) 与 [使用规则与免责声明](docs/safety/rules-and-disclaimer.mdx)。

## 👥 社区

维护者：Luyi Tian（首席研究员）、Weige Zhou（主导开发）、Liying Chen（开发）、Pengfei Yin（开发）。

🐛 [Issues](https://github.com/TianGzlab/OmicsClaw/issues) · 💬 [Discussions](https://github.com/TianGzlab/OmicsClaw/discussions) · 📖 [文档站](https://TianGzlab.github.io/OmicsClaw/)

<table>
  <tr>
    <td align="center" width="30%">
      <img src="docs/images/IMG_3729.JPG" alt="OmicsClaw 微信交流群" width="180"/>
      <br/>
      <b>微信交流群</b>
      <br/>
      <sub>扫码加入</sub>
    </td>
    <td valign="middle" width="70%">
      欢迎扫码加入微信群，分享分析经验、反馈问题、与社区交流多组学 AI 工作流。
    </td>
  </tr>
</table>

<a href="https://github.com/TianGzlab/OmicsClaw/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=TianGzlab/OmicsClaw" alt="OmicsClaw 贡献者"/>
</a>

## 🙏 致谢

OmicsClaw 的架构、技能设计和本地优先理念深受 **[ClawBio](https://github.com/ClawBio/ClawBio)**（生物信息学场景下较早的原生 AI agent 技能库）启发。记忆与会话续接模式参考了 [Nocturne Memory](https://github.com/Dataojitori/nocturne_memory)。

## 🛠️ 贡献

- **新增技能**：参考 [CONTRIBUTING.md](CONTRIBUTING.md) 与 [`templates/skill/`](templates/skill/) 的 v2 脚手架。
- **仓库 / agent 开发**：参考 [AGENTS.md](AGENTS.md) —— 包含 contract 测试、provider 契约、技能运行器、架构文档索引。

## 📜 许可证

Apache-2.0，详见 [LICENSE](LICENSE)。

## 📝 引用

```bibtex
@software{omicsclaw2026,
  title = {OmicsClaw: A Memory-Enabled AI Agent for Multi-Omics Analysis},
  author = {Zhou, Weige and Chen, Liying and Yin, Pengfei and Tian, Luyi},
  year = {2026},
  url = {https://github.com/TianGzlab/OmicsClaw}
}
```

[⬆ 返回顶部](#top)
