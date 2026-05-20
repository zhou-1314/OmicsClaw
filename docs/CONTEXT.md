# OmicsClaw Memory System

The graph-backed agent memory at `omicsclaw/memory/`. It records what each user (or workspace) has told OmicsClaw — sessions, preferences, dataset / analysis / insight lineage — and replays that context across runs and surfaces (CLI, Desktop, Telegram, Feishu) so the agent acts with continuity.

> **Scope.** This file documents the Memory System and the Ingress / Surface layer (the three user-facing entry points that all dispatch into `core.llm_tool_loop`). If the Skills system or other subsystems acquire enough domain vocabulary to warrant their own CONTEXT, split via a top-level `CONTEXT-MAP.md` per [CONTEXT-FORMAT.md §"Single vs multi-context repos"].

## Language

### Identity & Addressing

**Memory URI**: A `domain://path` string that names a memory's logical location, independent of any row id.
_Avoid_: "memory key", "memory id" (the latter is `Memory.id` — a row id of one content version)

**Domain**: The top-level segment of a Memory URI — one of `core`, `dataset`, `analysis`, `insight`, `preference`, `project`, `session`.
_Avoid_: "namespace" (orthogonal concept, see below), "category"

**Namespace**: The isolation dimension stored as a column on `paths`, `search_documents`, and `glossary_keywords`. Surfaces inject it: CLI/Desktop = workspace path, Bot = `f"{platform}/{user_id}"`, system = `__shared__`.
_Avoid_: "tenant", "scope" (the `ScopedMemory` filesystem layer is a different concept)

**`__shared__`**: The reserved Namespace whose rows are visible to every other Namespace via Read fallback. Holds `core://agent`, `core://kh/*`, and the system glossary.
_Avoid_: "global", "default", "public"

**Read fallback**: The rule that `recall` and `search` automatically include `__shared__` results when the current Namespace doesn't match. `list_children` deliberately does NOT fall back, to prevent cross-user leakage during subtree traversal.
_Avoid_: "merge", "auto-join"

**Display label**: The human-readable string shown for a Memory in desktop tree and listing UIs. For `dataset`, `preference`, `core`, `session` the label equals the URI's last path segment. For `analysis://*` the URI's last segment is a UUID hex (load-bearing for write-collision avoidance, since `analysis://*` is overwrite-mode), so the label is **derived from `Memory.content`** at the API boundary — `<dataset_basename> · <hh:mm or yyyy-mm-dd hh:mm> · <status>`. The Memory URI remains the canonical identity; the label is purely presentation.
_Avoid_: "title", "name" (overloaded — `name` is also the response field that carries this label)

### Layers & Modules

**Hot path**: The high-frequency operations triggered by every chat turn, skill run, or auto-capture — `upsert`, `recall`, `search`, `list_children`, `get_subtree`.
_Avoid_: "agent path", "fast path"

**Cold path**: The low-frequency, human-triggered operations — orphan inspection, version chain audit, rollback, cascade delete, changeset approval.
_Avoid_: "admin path", "slow path"

**MemoryEngine**: The Hot path engine. Single SQLAlchemy-backed module exposing 7 verbs against `(uri, namespace)` pairs; owns transactions and search reindexing.
_Avoid_: "GraphService" (the legacy 1584-line class this replaces), "MemoryStore", "MemoryService"

**ReviewLog**: The Cold path engine. Reads the same DB but is invoked exclusively by the Desktop app's `/memory/review/*` endpoints and bot cleanup paths.
_Avoid_: "audit log", "history"

**MemoryClient**: The strategy layer between Surfaces and `MemoryEngine`. Decides Namespace via `resolve_namespace()` and version policy via `should_version()`; the only handle Surfaces should hold.
_Avoid_: "MemoryAPI", "MemoryFacade"

**ScopedMemory**: The filesystem-backed memory layer at `.omicsclaw/scoped_memory/` (markdown + frontmatter). Holds workspace-local hints. Live consumers: the `/memory` slash command in `omicsclaw/surfaces/cli/` (CLI/TUI) and `omicsclaw/diagnostics.py`. ScopedMemory now coexists with graph memory on the CLI/TUI Surface — markdown notes (`scope`/`list`/`add`/`prune`) stay on disk while `remember`/`recall`/`search` route to `MemoryEngine`.
_Avoid_: "workspace memory" (would clash with `Namespace=workspace`)

### Write Modes

**Versioned upsert**: A write that appends a new `Memory` row and marks the previous as `deprecated=True` with `migrated_to` pointing to the successor. The only write mode `ReviewLog.rollback_to` operates on.
_Avoid_: "history write", "audit write"

**Overwrite upsert**: A write that updates content in-place on a single active `Memory` row, no deprecation chain. The high-volume default.
_Avoid_: "replace", "update"

**Shared write**: The explicit `MemoryClient.remember_shared(uri, content)` call that pins `namespace='__shared__'` regardless of the caller's current Namespace. Used by the system to seed `core://agent`, KH guards, and shared glossary.
_Avoid_: "global write", "broadcast"

### Surfaces

**Surface**: A user-facing entry point. Three Surfaces today: **Channel Surface**, **Desktop Surface**, **CLI Surface**. All Surfaces dispatch into the same engine entry `core.llm_tool_loop` and host one `MemoryClient` per request context.
_Avoid_: "entry" (overloaded with engine entry-point); "front-end" (overloaded with the Vue project under `frontend/`).

**Channel Surface**: The Surface that fans out to all IM platforms. Holds N **Channel Adapter** instances and is lifecycle-managed by `omicsclaw/surfaces/channels/manager.py:ChannelManager`. Today the wired adapters are Telegram, Feishu, Slack, Discord, WeChat, WeCom, DingTalk, iMessage, Email, QQ.
_Avoid_: "Bot Surface" (overloaded with the OmicsBot persona in `SOUL.md`), "IM Surface" (only some of the 10 adapters are IM in the strict sense — Email isn't).

**Channel Adapter**: The per-platform implementation that lives inside the Channel Surface. One file per platform under `omicsclaw/surfaces/channels/<name>.py`. Each adapter today calls `core.llm_tool_loop` directly (per ADR 0003).
_Avoid_: "channel" (the bare word now denotes the Surface, not its adapters), "gateway" (cellclaw's word, not ours), "backend" (clashes with LLM/storage/queue backend).

**Desktop Surface**: The Surface served by `omicsclaw/surfaces/desktop/server.py` (FastAPI). Today streams intermediate events via SSE plus an `asyncio.Queue` callback bridge; supports cancel via `pending_preflight_requests`.

**CLI Surface**: The Surface served by `omicsclaw/surfaces/cli/interactive.py` and `tui.py` (`oc interactive` — graph memory wired 2026-05 via `build_graph_memory_command_view`). Dispatches in-process: `asyncio.create_task(core.llm_tool_loop(...))` and prints/streams to the terminal.

### Surface namespace defaults

The wired Surfaces derive their Namespace string consistently:

| Surface | Helper | Namespace |
|---|---|---|
| CLI Surface | `cli_namespace_from_workspace(workspace_dir)` | absolute workspace path (cwd if unset) |
| Desktop Surface | `desktop_namespace()` | `app/<OMICSCLAW_DESKTOP_LAUNCH_ID>` or `app/desktop_user` |
| Channel Surface (per Channel Adapter) | `CompatMemoryStore` per-session | `f"{platform}/{user_id}"` where `platform` is the adapter name (e.g. `telegram`, `feishu`, `slack`, ...) |
| System / boot scripts | constant | `__shared__` |

The `app/`, `<platform>/` prefixes are structural — they prevent any cross-surface collision with absolute filesystem paths used by the CLI Surface.

## Relationships

- A **Surface** holds one **MemoryClient** per request context.
- A **MemoryClient** holds one **MemoryEngine** reference and one **Namespace** string.
- A **MemoryClient** routes each `remember()` call to either a **Versioned upsert** or an **Overwrite upsert** based on the **Memory URI**'s domain.
- A **MemoryEngine** writes a `(domain, path)` row partitioned by **Namespace**; **Read fallback** to `__shared__` happens at query time, not at write time.
- A **ReviewLog** reads the same database as **MemoryEngine** but exposes only **Cold path** verbs.
- A **Memory URI** with domain `core` and path starting with `agent` / `kh` / `my_user_default` is **routed to** `__shared__` by `namespace_policy` whenever something writes there. Both `core://agent` and `core://kh/*` are now wired: every memory-init path (Compat bot, MemoryClient legacy db_url, app/server.py chat lifespan, memory/server.py lifespan) calls `seed_knowhows()` after `init_db()`, mirroring the on-disk KH corpus into `__shared__` under `core://kh/<doc_id>`. `core://my_user_default` remains a reserved prefix awaiting a writer. Everything outside those three prefixes lives in the caller's current **Namespace**.
- A **Versioned upsert**'s `migrated_to` chain is the only structure where **ReviewLog.rollback_to** can operate.

## Example dialogue

> **Dev:** "If a Telegram user updates their `qc_threshold` preference, does the old value disappear?"
> **Architect:** "No — `preference://*` is in `VERSIONED_PREFIXES`, so `MemoryClient.remember()` routes to a **Versioned upsert**. The old `Memory` row stays with `deprecated=True`; **ReviewLog.list_version_chain** can find it; the user can roll back via the Desktop review UI."
>
> **Dev:** "Same user processes `pbmc.h5ad` in two different workspaces — what happens?"
> **Architect:** "Two different **Namespaces**, two independent `paths` rows. The same file produces two `dataset://pbmc.h5ad` entries. **Read fallback** doesn't connect them because `dataset://*` is per-Namespace, not shared."
>
> **Dev:** "I bind the keyword 'TIL' to a shared OmicsClaw concept node — do other users see it?"
> **Architect:** "Only if you call `add_glossary_shared('TIL', node)`. Plain `add_glossary` writes the binding under your current **Namespace**. **Read fallback** surfaces shared bindings to everyone, but your private one stays private."

## Resolved-by-default decisions

Decisions that the refactor PRs (#125–#132) chose by sensible default rather than explicit RFC:

- **One database, many Namespaces** — `OMICSCLAW_MEMORY_DB_URL` selects a single SQLite/Postgres database; Namespace columns partition the data inside it. Different desktop launches with different `OMICSCLAW_DESKTOP_LAUNCH_ID` values share the same DB but get distinct Namespaces. Dropping a per-launch DB option keeps cross-launch read-fallback (`__shared__`) working with no extra cross-DB plumbing.
- **`oc interactive` from `~/`** — uses the absolute home path as Namespace. No special-case handling; `~` is a valid string id like any other directory.
- **Read fallback policy is asymmetric** — `recall` and `search` fall back to `__shared__`; `list_children` and `get_subtree` do not. The asymmetry is deliberate: per-row fallbacks give per-user contexts visibility into globally-shared content, but per-listing fallbacks would pollute a user's own inventory with shared structure they didn't author.

## Open questions

Tracked but not yet resolved in code:

- **`_auto_capture_dataset` policy** — When the bot detects a dataset filename, should it write under the user's Namespace or `__shared__`? Today it goes user-scoped (per CompatMemoryStore default), which means the same `pbmc.h5ad` mentioned by two users gets two `dataset://` rows. Whether to deduplicate at `__shared__` is a UX question.

## Resolved (kept here for tombstone)

- ~~**`core://kh/*` seed bootstrap**~~ — **Resolved (PR #172, 2026-05-11)**: every `init_db()` caller invokes `seed_knowhows()`, which iterates `KnowHowInjector.iter_entries()` and writes each `(uri, content)` via the idempotent `MemoryEngine.seed_shared`. Same-content reseeds are no-ops; failures downgrade to a warning log and don't block startup.

## Cross-reference: Consensus runtime (forward-declared)

ADR 0010 (2026-05-18) introduces a new subsystem `omicsclaw/runtime/consensus/` that adds its own domain vocabulary. The canonical definitions live in ADR 0010's "Vocabulary" section — listed here so cross-subsystem readers can recognise the terms when they appear in skill code, ADRs 0010/0011, and reports. **These terms migrate to `omicsclaw/runtime/consensus/CONTEXT.md` once that directory exists**; until then, ADR 0010 is the source of truth.

- **Typed consensus (A path)** — statistical consensus via a categorical operator (kmode / LCA / weighted). Output is marked "verified". _Avoid_: "strict consensus", "hard consensus".
- **Narrative consensus (B path)** — LLM-mediated synthesis with explicit contradiction annotation. Output is marked "exploratory" and lives under a separate `analysis://exploratory/*` namespace. _Avoid_: "LLM consensus" (the LLM is in both paths).
- **Consensus member** — a `(name, skill_name, params)` triple that names one fan-out target; runs as a deterministic skill subprocess, **not** an LLM sub-agent. Reading the member's outputs is the job of a **MemberArtifactReader**, not of the member itself. _Avoid_: "sub-agent".
- **MemberArtifactReader** — per-source-skill adapter that knows where the member's labels and intrinsic-quality value live on disk. One singleton per registered source skill (e.g. `SpatialDomainsArtifactReader`, `ScClusteringArtifactReader`). Driver / graph-memory writer / test harness program against `(read_labels, read_intrinsic_quality)`; they do not know file paths or column names. _Avoid_: "loader", "ingester".
- **TypedConsensusSource** — the value type of `TYPED_CONSENSUS_REGISTRY` (a frozen dataclass). v1 holds one field (`reader`); v1.x may add a `planner` / `report_template` / etc. without changing the registry's shape. _Avoid_: "skill spec", "registry entry".
- **TypedConsensusRun** — frozen result object returned by `run_typed_consensus`. Carries everything one A-path execution produced: members, team_result, labels_df, intrinsic_map, scores, nmi_matrix, selected_bcs, consensus, output_dir, artifacts_written. Downstream report rendering, graph-memory writes, and CI assertions all program against `TypedConsensusRun`. _Avoid_: "result", "report" (overloaded with the markdown).
- **Evaluation chair** — the LLM role that picks members and narrates results; has no statistical synthesis authority. SACCELERATOR's "expert-in-the-loop" with the LLM as the expert. _Avoid_: "judge", "synthesizer", "orchestrator" (the latter is taken by the routing skill).
- **Base clusterings (BC)** — the subset of members the user selects (CLI) or top-K-by-score picks (Desktop/Channel) to feed into the typed operator. Direct analogue of SACCELERATOR `02_BC_ranking`. _Avoid_: "selected methods", "chosen clusterings".
- **`TYPED_CONSENSUS_REGISTRY`** — the explicit allowlist of skills with a typed operator. A skill not in the set auto-routes to the B path; new skills must register explicitly. _Avoid_: "consensus-eligible flag".
- **`analysis://typed/*` vs `analysis://exploratory/*`** — graph-memory namespace split. Future meta-analysis defaults to reading only `typed/*`. _Avoid_: collapsing the two; bare `analysis://`.
- **Member score** — composite `α · cross_method_NMI + β · intrinsic_quality` with class-imbalance hard filter at `max_class_frac > 0.8`; defaults `α=0.6, β=0.4`. Defined in ADR 0011. _Avoid_: "quality score" (ambiguous — could mean intrinsic alone).

### Relationship to Memory System

A consensus run writes to graph memory under either `analysis://typed/<run_id>` or `analysis://exploratory/<run_id>` per the **A path vs B path** distinction above. The `analysis://*` Domain is already covered by [**Versioned upsert**](#write-modes) semantics — consensus runs do not introduce a new write mode. The `typed/` vs `exploratory/` sub-prefix is a structural URI convention enforced by `runtime/consensus/dispatch.py`, not a new Domain.

## Flagged ambiguities

- **"user"** is used three ways: (1) the human researcher (`core://my_user`), (2) the chat-side participant (`Session.user_id`), (3) the Linux process owner. In this doc "user" means (1) or (2) — context determines which; never (3).
- **"workspace"** appears in both the **Surface** layer (the directory the user picked, used as the Namespace string) and the `ScopedMemory` layer (a filesystem root). Same physical directory, different concepts; will collapse only if ScopedMemory is integrated into MemoryEngine.
- **GraphService is retired.** Production code uses `MemoryEngine` / `ReviewLog` / `MemoryClient` exclusively. The legacy path-based admin operations (`/api/browse/*` write endpoints) live in a private `omicsclaw/memory/api/_browse_helpers.BrowseHelpers` class consumed only by the `oc memory-server` admin UI — do not import from outside `omicsclaw/memory/api/`. A future rewrite can port the admin UI against `MemoryEngine` and delete this module entirely.
- **"namespace"** vs **"domain"**: domain is the URI prefix (`dataset`, `core`, …); namespace is the user/workspace isolation key. They are orthogonal columns; never use one as a synonym for the other.
