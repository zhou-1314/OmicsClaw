# AGENTS.md — OmicsClaw Guide for AI Coding Agents

This guide is for AI coding agents working on the OmicsClaw codebase.

## Repository Working Contract

Before any complex repository maintenance, feature, or refactor task, read
`README.md` first for project context and prior decisions. Then read this
`AGENTS.md`, root `SPEC.md`, and the directly relevant code/docs.

Core rules:

- Reply in the user's language, usually Chinese or English.
- Stay concise, practical, and execution-focused.
- For non-trivial changes, work from a concise plan, keep edits scoped, and
  verify claims with concrete commands or file inspections before reporting
  completion.
- When you make an important decision or complete a meaningful milestone,
  update `README.md` while preserving its existing structure.

## Project Overview

OmicsClaw is a multi-omics analysis platform supporting 95 registered skills
across 8 domains: spatial transcriptomics, single-cell omics, genomics,
proteomics, metabolomics, Bulk RNA-seq, orchestration, and literature. Each
skill is a self-contained module that performs a specific analysis task via CLI
or Python API. All processing is local-first. Design is inspired by
[ClawBio](https://github.com/ClawBio/ClawBio).

**Note**: OmicsClaw evolved from SpatialClaw and now uses a unified `omicsclaw.py` entrypoint.

## Setup

```bash
cd /path/to/OmicsClaw

# Recommended: full conda-primary install (R + CLIs + Python in one shot)
bash 0_setup_env.sh
conda activate OmicsClaw

# Lightweight alternative (Python-only skills, no R or external CLIs):
# pip install -e .
# pip install -e ".[interactive]" / ".[tui]" / ".[memory]" / ".[full]"

python omicsclaw.py list   # or: oc list
python omicsclaw.py run spatial-preprocess --demo
```

> **`oc` short alias**: After installing OmicsClaw (either path), both
> `omicsclaw` and `oc` commands are available system-wide via the
> `[project.scripts]` entry in `pyproject.toml`.
>
> **Dependency source of truth**:
> - **Python deps** live in `pyproject.toml` (used by both install paths).
> - **R packages, bioinformatics CLIs, build toolchain** live in
>   `environment.yml` (conda path only).
> - **GitHub-only R packages** are installed inline by `0_setup_env.sh`
>   Tier 3 (`devtools::install_github` for spacexr, CARD, CellChat, numbat,
>   SPARK, DoubletFinder).
> - **Optional analysis backends** (cellrank, palantir, scvelo, tangram-sc,
>   …) are catalogued per domain in `skills/<domain>/_lib/dependency_manager.py`
>   `DEPENDENCY_REGISTRY` (canonical PyPI name → module + install_cmd). This is
>   the SSOT for backend name mapping; new algorithms register here.
> - **Per-skill `requires:` frontmatter** is generated/checked from the real
>   import surface by `scripts/audit_skill_requires.py` (`--check` in CI,
>   `--write` to regenerate). Never hand-edit it to "fix" a missing backend —
>   register the backend and run `--write`. See CONTRIBUTING.md.
>
> The repository does not use a root `requirements.txt` as a primary
> install entrypoint.
>
> **Known `pip check` warning**: the full conda environment keeps
> `jinja2>=3.1.5` for FastAPI/nbconvert even though upstream
> `pygpcca==1.0.4` still pins `jinja2==3.0.3`. Treat that single warning as
> metadata noise when `oc doctor` and targeted import checks pass.

## Commands

> Both `python omicsclaw.py <cmd>` and the short alias `oc <cmd>` work identically
> after `pip install -e .` (or `make install-oc`).

| Command | Purpose |
|---------|---------|
| `oc list` | List all 95 skills across 8 domains |
| `oc run <skill> --demo` | Run a skill with demo data |
| `oc run <skill> --input <file> --output <dir>` | Run with user data |
| `oc interactive` | **Start interactive terminal chat (CLI mode)** |
| `oc interactive --ui tui` | **Start full-screen Textual TUI** |
| `oc interactive -p "<prompt>"` | **Single-shot mode (non-interactive)** |
| `oc interactive --session <id>` | **Resume a previous session** |
| `oc tui` | Alias for `interactive --ui tui` |
| `oc desktop-server` | Start the FastAPI backend used by OmicsClaw-App / web frontends |
| `oc mcp list` | List configured MCP servers |
| `oc mcp add <name> <cmd> [args]` | Add an MCP server |
| `oc mcp remove <name>` | Remove an MCP server |
| `oc mcp config` | Show MCP config file path |
| `oc onboard` | Run interactive setup wizard for LLM, runtime, memory, and channels |
| `python -m pytest -v` | Run all tests |
| `make test` | Alias for pytest |
| `make demo` | Run preprocess demo |
| `make install-oc` | (Re)install package + activate `oc` alias |
| `make oc-link` | Quick wrapper script in `~/.local/bin/oc` (no pip) |
| `make bot-telegram` | Start Telegram bot |
| `make bot-feishu` | Start Feishu bot |

## Project Structure

Boundary-led layout (see ADRs 0004 + 0005 for the design history). Each
top-level sub-package of `omicsclaw/` corresponds to one architectural
boundary; new code lands in the sub-package whose name matches its
concern.

```
OmicsClaw/
├── omicsclaw.py                # Main CLI script (SKILLS dict, DOMAINS registry, `oc list`/`oc run`/`oc desktop-server`)
├── omicsclaw/                  # The single top-level Python package
│   ├── surfaces/               # ← Ingress layer (ADR 0005). The three user-facing entry points.
│   │   ├── channels/           #   Channel Surface — 10 IM adapters + ChannelManager + `python -m`
│   │   ├── desktop/            #   Desktop Surface — FastAPI server for Electron / Next.js frontends
│   │   └── cli/                #   CLI Surface — prompt_toolkit REPL, Textual TUI, setup wizard, `oc` launcher
│   ├── runtime/                # Agent loop, context assembly, tool registry, policy, transcript storage (ADR 0004 P3)
│   │   ├── agent/, context/, tools/, policy/, storage/
│   ├── skill/                  # Skill registry, runner, lookup, subprocess execution (ADR 0004 P2)
│   ├── providers/              # LLM provider registry + OpenAI/ccproxy adapters (ADR 0004 P1)
│   ├── memory/                 # Graph memory system (MemoryEngine, MemoryClient, ReviewLog)
│   ├── services/               # Cross-cutting: audit, billing, rate_limit, path_validation
│   ├── core/                   # Thin base layer: dependency_manager, external_env, r_* helpers
│   ├── loaders/                # File-extension → domain detection
│   ├── routing/                # Multi-agent routing
│   ├── agents/                 # Agent definitions
│   ├── remote/                 # Remote runtime (jobs, workspace resolution)
│   ├── knowledge/              # Knowhow / semantic index
│   ├── extensions/             # MCP integration etc.
│   ├── engine/                 # Shared engine primitives
│   ├── execution/              # Execution helpers
│   ├── interactive/ ⛔          # MOVED → surfaces/cli/ (ADR 0005)
│   ├── channels/    ⛔          # MOVED → surfaces/channels/ (ADR 0005)
│   ├── app/         ⛔          # MOVED → surfaces/desktop/ (ADR 0005)
│   └── __main__.py             # `python -m omicsclaw` entry hook (stays at package root)
├── skills/                     # Domain-organized skills + shared utilities
│   ├── spatial/                # 19 spatial transcriptomics skills (+ _lib/)
│   ├── singlecell/             # 34 single-cell omics skills (+ _lib/)
│   ├── genomics/               # 10 genomics skills (+ _lib/)
│   ├── proteomics/             # 8 proteomics skills (+ _lib/)
│   ├── metabolomics/           # 8 metabolomics skills (+ _lib/)
│   ├── bulkrna/                # 13 bulk RNA skills (+ _lib/)
│   ├── orchestrator/           # 2 orchestration skills
│   └── literature/             # 1 literature skill
├── docs/                       # Project docs (CONTEXT.md vocabulary, adr/ history)
│   ├── CONTEXT.md              # Authoritative glossary for Surfaces, Memory, Namespaces, etc.
│   └── adr/                    # Architecture Decision Records (0001..0005)
├── tests/                      # Test suite (mirrors omicsclaw/ structure)
├── SOUL.md                     # OmicsBot persona used by the Channel Surface
├── SPEC.md                     # Repository maintenance + AI development contract
├── templates/skill/            # v2 scaffold for new skills (copy whole dir)
├── examples/                   # Shared demo data
├── CLAUDE.md                   # Agent routing instructions (Claude Code entry)
└── AGENTS.md                   # This file
```

> **Import convention**: domain-specific skill utilities live in
> `skills/<domain>/_lib/` and are imported via
> `from skills.<domain>._lib.<module> import <name>`. The `_lib/` directories
> are internal shared packages and are not registered as skills (the registry
> ignores directories starting with `_`). The `omicsclaw/` package contains
> only domain-agnostic framework code; ingress lives in `surfaces/`, agent
> machinery in `runtime/`, skill machinery in `skill/`.

## Skill Architecture

Every skill has a `SKILL.md` with YAML frontmatter + methodology, a Python script accepting `--input`, `--output`, `--demo`, and optionally `tests/` and `data/`.

Skills are registered in `omicsclaw/core/registry.py` and dynamically discovered from `skills/`.

### Skill Metadata Rules

- SKILL.md frontmatter (`metadata.omicsclaw`) is the single source of truth for skill metadata — canonical name, legacy aliases, allowed flags, `saves_h5ad`, and so on.
- All primary skill scripts must expose a lightweight direct `--help` path.
- Skill scripts write native artifacts; the shared runner writes the top-level `README.md` and `reproducibility/analysis_notebook.ipynb`.
- Bot skill execution uses the same shared runner contract as CLI, interactive, agent tools, app, and remote jobs.
- Shared result construction and adapter coercion live in `omicsclaw/core/skill_result.py`; new execution surfaces should reuse that model instead of rebuilding legacy result dictionaries.

## How to Add a New Skill

1. `mkdir skills/<your-skill-name>`
2. `cp -r templates/skill skills/<domain>/<your-skill-name>` (then rename + fill placeholders)
3. Fill in SKILL.md
4. Add Python script accepting `--input`, `--output`, `--demo`
5. Add tests in `tests/`
6. Register stable aliases in `omicsclaw/core/registry.py` (or rely on dynamic discovery)
7. Add test path to `pytest.ini`
8. Regenerate catalog: `python scripts/generate_catalog.py`

## Development Workflow

For repository development work, start with a short plan when the task spans
multiple files, debug from root cause before editing, and verify the affected
behavior before committing, pushing, or opening a PR.

After creating or materially updating any PR, always run
`cursor-team-kit:make-pr-easy-to-review` before handing it off. The PR should
have a reviewer-oriented description with a TL;DR, recommended review order,
diff buckets, generated/mechanical file notes, risk notes, and verification
evidence.

### Contract Tests

Framework optimization guardrails are enforced by targeted contract tests:
`tests/test_documentation_facts.py`, `tests/test_skill_runner_contract.py`, `tests/test_skill_metadata_contract.py`, `tests/test_skill_help_contract.py`, `tests/test_registry_alias_contract.py`, and `tests/test_output_ownership_contract.py`.

### Architecture Contracts

- [domain input contracts](docs/engineering/domain-input-contracts.md)

## Graph Memory System

OmicsClaw uses a centralized graph-based memory system to persist context across sessions, agents, and tool invocations. The core system is located in `omicsclaw/memory/`. Vocabulary, decisions, and architectural diagrams live in [`docs/CONTEXT.md`](docs/CONTEXT.md).

### Architecture

Three layers over a SQLite/PostgreSQL graph database (SQLAlchemy):

| Layer | Module | Role |
|---|---|---|
| Strategy | `MemoryClient(engine, namespace=...)` | Decides which Namespace a write lands in and whether it's versioned vs. overwrite. |
| Hot path | `MemoryEngine` (`omicsclaw/memory/engine.py`) | 7 verbs over `(uri, namespace)`: `upsert`, `upsert_versioned`, `patch_edge_metadata`, `recall`, `search`, `list_children`, `get_subtree`. |
| Cold path | `ReviewLog` (`omicsclaw/memory/review_log.py`) | Version-chain inspection, rollback, orphan/GC, browse_shared, changeset approve/discard — for the desktop Review & Audit pane. |

- **Nodes & Edges**: Every entity (session, dataset, user preference) is a node connected via edges to a central root (`ROOT_NODE_UUID`). Nodes are addressed by URIs (e.g., `core://agent`, `dataset://pbmc.h5ad`).
- **Namespace partition**: `paths`, `search_documents`, and `glossary_keywords` carry a `namespace` column; surfaces inject the value (CLI = workspace path, Desktop = `app/<launch_id>`, Bot = `<platform>/<user_id>`, system = `__shared__`).
- **Read fallback is asymmetric**: `recall` and `search` see `__shared__` content automatically; `list_children` and `get_subtree` are strict so private inventories don't get polluted by shared structure.
- **Compat Layer**: `omicsclaw/memory/compat.py` (`CompatMemoryStore`) is the bot's drop-in replacement for the legacy `bot.memory.MemoryStore`; it derives a per-session namespace from `(platform, user_id)`.
- **REST API**: A FastAPI backend provides management capabilities (browse, search, review, rollback, orphan inspection), accessible via `oc memory-server`. The desktop's `/memory/review/*` routes go through `ReviewLog`.

#### Surface helpers

```python
from omicsclaw.memory import (
    cli_namespace_from_workspace,  # absolute workspace path (cwd if None)
    desktop_namespace,             # app/<OMICSCLAW_DESKTOP_LAUNCH_ID> or app/desktop_user
    get_memory_client,             # factory: MemoryClient bound to a namespace
    get_memory_engine,             # singleton MemoryEngine
    get_review_log,                # singleton ReviewLog
)
```

> **Migration note.** `MemoryEngine` and `ReviewLog` are the canonical hot- and cold-path layers; all production endpoints route through them. The legacy `GraphService` class has been retired (`graph.py` deleted). Its path-based admin operations now live in a private `omicsclaw/memory/api/_browse_helpers.BrowseHelpers` class used only by the `oc memory-server` admin UI (`/api/browse/*`). New code MUST use `MemoryEngine` / `ReviewLog` / `MemoryClient`; do not import `_browse_helpers` from outside `omicsclaw/memory/api/`.

> **KH bootstrap.** Every memory-init path (`CompatMemoryStore.initialize`, `MemoryClient.initialize` with `database_url`, `app/server.py` chat lifespan, `memory/server.py` lifespan) calls `seed_knowhows()` after `init_db()`. The function reads `KnowHowInjector.iter_entries()` and writes each entry to `__shared__` under `core://kh/<doc_id>` via the idempotent `MemoryEngine.seed_shared`. Failures downgrade to a log line; missing `knowledge_base/` does not block startup.

### Running the Dashboard API

You can spin up the backend API to inspect and manage memories. **Note**: `fastapi` and `uvicorn` are optional dependencies, you must install them first:

```bash
# Install memory API dependencies
pip install fastapi uvicorn
# OR: pip install -e ".[memory]"

# Starts the FastAPI server on port 8766
oc memory-server
```

The memory API binds to `127.0.0.1:8766` by default. If you bind it to a non-local interface, set `OMICSCLAW_MEMORY_API_TOKEN` as well.

## Surfaces (Ingress Layer)

OmicsClaw exposes three user-facing **Surfaces** — Channel, Desktop, CLI —
that all live under `omicsclaw/surfaces/` and dispatch into the same
agent entry, `omicsclaw.runtime.agent.state.llm_tool_loop`. Authoritative
vocabulary is in [`docs/CONTEXT.md`](docs/CONTEXT.md) §"Surfaces"; the
restructure history is in [ADR 0005](docs/adr/0005-surfaces-umbrella-for-ingress.md).

| Surface | Location | Primary entry |
|---|---|---|
| **Channel Surface** | `omicsclaw/surfaces/channels/` | `python -m omicsclaw.surfaces.channels --channels <names>` |
| **Desktop Surface** | `omicsclaw/surfaces/desktop/` | `oc desktop-server --host 127.0.0.1 --port 8765` |
| **CLI Surface** | `omicsclaw/surfaces/cli/` | `oc interactive` (REPL) / `oc tui` (TUI) |

### Desktop Surface — FastAPI backend for desktop / web frontends

```bash
pip install -e ".[desktop]"
oc desktop-server --host 127.0.0.1 --port 8765
```

Binds `127.0.0.1:8765` by default and serves chat streaming, skills,
providers, MCP, outputs, bridge control, and memory proxy endpoints
for the OmicsClaw-App Electron/Next.js frontend.

**Environment variables**:
- `OMICSCLAW_MEMORY_DB_URL` — SQLAlchemy connection URL (e.g. `sqlite+aiosqlite:///~/.omicsclaw/memory.db`).
- `OMICSCLAW_MEMORY_API_TOKEN` — Bearer token required when exposing the API beyond localhost.

**Provider backend contract** (must hold across provider changes):
- `/providers` reports the active provider, model, and endpoint.
- `/providers/test` performs a short live LLM connectivity probe.
- `/chat/stream` reinitializes the provider runtime when a request changes model, even if the provider id is unchanged.

### Channel Surface — 10 IM platform adapters + `ChannelManager`

```bash
pip install -e ".[channels]"     # platform SDKs are extras
python -m omicsclaw.surfaces.channels --channels telegram
python -m omicsclaw.surfaces.channels --channels telegram,feishu,slack
python -m omicsclaw.surfaces.channels --list
make bot-telegram                # Makefile alias
make bot-feishu
```

Wired adapters: Telegram, Feishu, Slack, Discord, WeChat, WeCom,
DingTalk, iMessage, Email, QQ. Lifecycle is managed by
`omicsclaw/surfaces/channels/manager.py:ChannelManager`; each adapter
calls `core.llm_tool_loop` directly (per ADR 0003 — there is no
middleware pipeline). Cross-cutting concerns (rate limit, dedup,
audit) live in `omicsclaw/services/`.

The OmicsBot persona used across all Channel adapters is in `SOUL.md`.
Configuration goes in `.env` at the project root — see
`omicsclaw/surfaces/channels/README.md` for the per-platform variables.

### CLI Surface — prompt_toolkit REPL + Textual TUI

```
omicsclaw.py interactive
    └── omicsclaw/surfaces/cli/interactive.py   # prompt_toolkit REPL
           ├── omicsclaw/runtime/agent/state.py # LLM tool loop (shared with other Surfaces)
           ├── _session.py                     # SQLite session persistence
           ├── _mcp.py                         # MCP server management
           └── _constants.py                   # Banner, slash commands

omicsclaw.py tui   (or --ui tui)
    └── omicsclaw/surfaces/cli/tui.py           # Textual full-screen TUI
```

The CLI Surface also hosts the one-shot interactive setup wizard
(`omicsclaw/surfaces/cli/setup_wizard.py`) reached via `oc onboard`,
and the `oc` console-script launcher (`omicsclaw/surfaces/cli/launcher.py`)
that loads the repo-root `omicsclaw.py` skill runner.

### Interactive Mode Commands

```bash
# Enter interactive CLI (default, uses prompt_toolkit REPL)
oc interactive

# Enter full-screen TUI (requires: pip install textual)
oc tui
oc interactive --ui tui

# Single-shot (non-interactive)
oc interactive -p "run spatial-preprocessing demo"

# Resume a previous session
oc interactive --session <session-id>

# Override model/provider
oc interactive --provider deepseek --model deepseek-chat

# Set working directory
oc interactive --workspace /path/to/workdir

# Daemon mode (persistent workspace, default behavior)
oc interactive --mode daemon

# Run mode (isolated per-session workspace)
oc interactive --mode run

# Run mode with a named workspace
oc interactive --mode run --name my-analysis
```

### Slash Commands (inside interactive session)

| Command | Description |
|---------|-------------|
| `/skills [domain]` | List all skills (optionally filter by domain) |
| `/run <skill> [--demo] [--input <path>]` | Run a skill directly |
| `/sessions` | List recent sessions |
| `/resume [id]` | Resume a session (interactive picker if no ID) |
| `/delete <id>` | Delete a saved session |
| `/current` | Show current session info |
| `/new` | Start a new session |
| `/clear` | Clear conversation history |
| `/mcp list` | List MCP servers |
| `/mcp add <name> <cmd> [args]` | Add MCP server |
| `/mcp remove <name>` | Remove MCP server |
| `/config list` | View configuration |
| `/config set <key> <val>` | Update configuration |
| `/help` | Show all commands |
| `/exit` | Quit OmicsClaw |

### MCP Server Management

```bash
# Add an MCP server (stdio transport)
oc mcp add sequential-thinking npx -- -y @modelcontextprotocol/server-sequential-thinking

# Add an HTTP-based MCP server
oc mcp add my-server http://localhost:8080

# List all configured MCP servers
oc mcp list

# Remove an MCP server
oc mcp remove sequential-thinking

# Show config file location
oc mcp config
# → ~/.config/omicsclaw/mcp.yaml
```

MCP tools are loaded from `~/.config/omicsclaw/mcp.yaml` at session start.
Requires `langchain-mcp-adapters` for actual tool execution:
```bash
pip install langchain-mcp-adapters
```

### Session Persistence

Sessions are saved to `~/.config/omicsclaw/sessions.db` (SQLite).
Conversation history is preserved across restarts and can be resumed by ID.

### Dependencies

```bash
# Minimal (CLI mode)
pip install prompt-toolkit rich questionary pyyaml aiosqlite

# Or via pyproject.toml extras
pip install -e ".[interactive]"

# Full TUI support
pip install -e ".[tui]"
# then: pip install textual>=0.80
```

### Provider Runtime Contract

Interactive CLI provider changes share the runtime resolution path with the app backend: `LLM_PROVIDER=custom` must honor `LLM_BASE_URL`, `OMICSCLAW_MODEL`, and `LLM_API_KEY`; explicit CLI `--provider` / `--model` overrides win over environment defaults; malformed custom endpoints should return actionable diagnostics instead of `(no response)`.

### TUI Implementation Notes

TUI helpers under `omicsclaw/surfaces/cli/_tui_support.py` stay dependency-light so support tests can run without optional memory or Textual installs. When adding Textual containers, mount the parent widget into the live tree before mounting child widgets.

## Safety Boundaries

1. **Local-first**: No data upload
2. **Disclaimer required**: Every report must include the OmicsClaw disclaimer
3. **No hallucinated science**: All parameters trace to SKILL.md or cited tools
4. **Security filtering**: `omicsclaw.py` enforces `allowed_extra_flags` whitelists
