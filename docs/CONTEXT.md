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
_Avoid_: "channel" (the bare word now denotes the Surface, not its adapters), "gateway" (the reference implementation's word, not ours), "backend" (clashes with LLM/storage/queue backend).

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

### Analysis routing

**Analysis Router**: The analysis-intent decision boundary that classifies a user request as non-analysis chat, an Exact skill match, a Partial skill match, or a No skill match.
_Avoid_: "orchestrator" (reserved for orchestration skills), "planner", "gateway"

**Deterministic route, assisted parameterization**: The execution rule for an Exact skill match. The Analysis Router fixes *which* skill runs (deterministic); the outer LLM then recommends *how* — the method and key parameters — grounded in two deterministically-supplied inputs: the matched skill's SKILL.md (its method menu, defaults, parameters, preconditions) and an `inspect_data` schema of the input. The recommendation (chosen method, rationale, near-tied alternative skills) is always shown. The LLM proceeds without blocking when the choice is safe — stating its assumptions — and asks exactly one focused question only on consequential ambiguity, per the **assisted-parameterization rule**: (1) a method named in the request is used as-is; (2) a safe/clear choice proceeds with stated assumptions; (3) a materially different, query-unresolved choice asks one focused question via the structured preflight channel; (4) a missing precondition (e.g. absent `obsm["X_pca"]`) blocks with remediation instead of running. Recommendation scope stays *within* the chosen skill; it never reselects the skill.
_Avoid_: "run the skill's default method silently", "LLM decides the skill", "fully bypass the LLM"

**Autonomous Analysis Path**: The fallback analysis route for Partial / No skill matches. It delegates uncovered work to the Autonomous Code Mini-Agent while preserving skill-first routing and outer-loop judgment.
_Avoid_: "reference path", "free-code mode", "fallback script", "replacement for skills"

**Autonomous Code Mini-Agent**: The bounded fallback executor introduced by ADR 0032. It owns a tactical inspect → plan → code → execute → feedback → self-check loop inside one autonomous run, using a persistent kernel plus curated skill handles; it does not own final acceptance of the result.
_Avoid_: "second chat engine", "fully autonomous scientist", "workflow author"

**Autonomous Code Runner**: The package / execution boundary that hosts the Autonomous Code Mini-Agent, allocates the run workspace, enforces permissions, records provenance, and returns output-shape-compatible artifacts.
_Avoid_: "custom_analysis_execute", "hidden skill", "generic notebook"

**Legacy custom analysis adapter** (removed): `custom_analysis_execute` was the one-shot notebook compatibility adapter; it was removed in the ADR 0032 single-engine consolidation. `autonomous_analysis_execute` (the Autonomous Code Mini-Agent) is now the sole generated-code fallback path.
_Avoid_: "primary fallback", "new autonomous path"

**Autonomous Code Loop**: The mini-agent's bounded tactical loop. It may reason over execution feedback, choose the next generated cell, call curated skill handles, and self-check artifacts before `ReturnAnswer`, but it remains scoped to one fallback run.
_Avoid_: "replace run_query_engine", "chat loop", "workflow runtime", "reusable orchestration"

**Skill-handle facade**: The injected `oc` / `skills` object available inside the autonomous kernel. v1 handles call the existing skill runner as subprocesses, write nested output directories, record ordered skill-call provenance, and reload declared artifacts back into the kernel; they are not arbitrary imports of skill scripts.
_Avoid_: "direct skill import", "raw subprocess access", "tool monkeypatch"

**Persistent autonomous kernel session**: The per-run Jupyter kernel used by the Autonomous Code Mini-Agent so data handles, variables, figures, and intermediate tables survive across generated-code steps.
_Avoid_: "global notebook", "shared user kernel", "session memory"

**Autonomous Kernel Safety Envelope**: The process / OS boundary around the persistent autonomous kernel: no network by default, stripped secrets, reads limited to explicit inputs/upstream artifacts/workspace; the host write-surface is the run workspace (the deliverable) plus ephemeral kernel scratch — never the host repo/inputs/system; and resource/time limits where supported. AST checks are lint inside this envelope, not the security boundary.
_Avoid_: "AST sandbox", "prompt safety", "best-effort guard"

**Kernel scratch home**: The throwaway `$HOME` for the autonomous kernel's tool dotfiles (matplotlib / numba / ipython caches). In the sandbox it lives inside the ephemeral `/tmp` tmpfs; without a sandbox it is a host temp dir removed on shutdown. It is machinery, never deliverable, so it never lands in the **Autonomous run workspace**.
_Avoid_: "kernel cache dir", "the .cache folder", "HOME in the workspace"

**Replay artifact**: The deterministic reproduction package emitted on `ReturnAnswer`: accepted cells in execution order (`analysis.py`, re-runnable — a manual re-run writes into a `rerun/` sibling, never over the original artifacts), the run manifest, the completion report, and the replay status. The validation re-run executes in throwaway scratch (a **Kernel scratch home**), so its re-run outputs never clutter the deliverable — only the pass/fail status is surfaced.
_Avoid_: "notebook transcript", "cell dump", "audit note", "replay subdirectory"

**Evidence-bound repair**: The mini-agent retry rule: failed generated steps may be revised from captured execution feedback, schema, variable/artifact diffs, and prior accepted steps. Whether the whole run satisfies the user's request remains **Autonomous result validation** in the outer loop.
_Avoid_: "keep trying", "self-debug until it works", "the runner has final judgment"

**Data-grounded autonomous planning**: The outer pre-handoff rule for a **No skill match** or **Partial skill match** that carries a trusted input file: the harness deterministically runs `inspect_data`, injects the real schema, and resolves consequential ambiguity before the mini-agent starts. The approved goal/schema/plan are passed to the mini-agent as run context.
_Avoid_: "skill preflight" (that fills skill parameters — a different step), "auto-inspect", "guess from the filename", "mid-kernel user interview"

**Autonomous result validation**: The outer-loop rule that, after the mini-agent returns, the outer LLM judges the replay-validated artifacts against the plan and intent and triggers a bounded re-delegation when they do not satisfy it — rather than trusting `ReturnAnswer` or exit code 0.
_Avoid_: "exit-code success", "trust the runner", "the runner self-validates"

**Outer autonomous seams**: The two judgment seams that stay outside the mini-agent after ADR 0032: one focused preflight question before handoff on consequential ambiguity, and final result validation before accepting `ReturnAnswer`.
_Avoid_: "human-in-the-loop everywhere", "runner asks whenever it wants", "full judgment handoff"

**Code Runner Permission Tier**: The risk class assigned to an Autonomous Code Runner action before execution.
_Avoid_: "safety flag", "mode"

**`read_only_probe`**: A Code Runner Permission Tier for commands that inspect files, schemas, package versions, or directory structure without writing analysis outputs.
_Avoid_: "dry run" (that implies a skipped execution)

**`analysis_write`**: A Code Runner Permission Tier for generated Python or R code that writes only inside the run workspace's approved output folders.
_Avoid_: "safe write" (too broad)

**`system_mutation`**: A Code Runner Permission Tier for package installation, network download, service startup, workspace-external writes, broad deletion, or unknown binary execution.
_Avoid_: "advanced mode", "admin mode"

**Output-shape parity**: The contract that Autonomous Code Runner runs produce the same navigable artifact shape as skill runs while preserving a distinct source label.
_Avoid_: "fake skill output", "separate output format"

**Autonomous run workspace**: The isolated output directory created for one Autonomous Code Runner execution under `autonomous-code__<timestamp>__<id>`. It is the *instance* (`AutonomousWorkspace`); its shape is the **Run layout**.
_Avoid_: "project root", "scratch dir", "temp dir"

**Run layout**: The single declaration (`run_layout`) of every path in an Autonomous run workspace — each path's name, lifecycle (eager vs lazy), and role (deliverable / provenance / sentinel / rerun). The eager-create set, the completion-report artifact contract, and the typed path accessors all derive from it, so the workspace layer and the contract cannot drift apart. The *schema* to the run workspace's *instance*.
_Avoid_: "dir constants", "the subdirs list", "WORKSPACE_SUBDIRS" (the scattered form it replaced)

**Autonomous run lifecycle**: The job-shaped lifecycle of an Autonomous Code Runner execution, including status, logs, cancellation, retry, artifacts, and terminal outcome.
_Avoid_: "plain tool result", "one-shot shell output"

**Lifecycle-shape compatibility**: The rule that Autonomous run lifecycle records should align with existing job fields and artifact/log conventions without requiring callers to go through the remote jobs router.
_Avoid_: "reuse the remote router", "separate job model"

**Upstream artifact reference**: A manifest entry that points to a prior skill output or user input without copying the underlying data into the Autonomous run workspace.
_Avoid_: "artifact copy", "imported output"

**Exact skill match**: A capability decision where one built-in skill fully covers the user's requested analysis and should run through the shared skill runner.
_Avoid_: "direct match", "normal route"

**Partial skill match**: A capability decision where a built-in skill covers the nearest core analysis but the request also needs generated code for post-processing, visualization, reporting, or an extra analytic step.
_Avoid_: "almost match", "hybrid match"

**Skill-first composition**: The execution rule for a Partial skill match: run the nearest built-in skill first, then let the Autonomous Analysis Path consume the skill artifacts for the uncovered work.
_Avoid_: "rewrite the skill", "replace the skill"

**No skill match**: A capability decision where no built-in skill covers the requested analysis well enough to execute safely as a skill.
_Avoid_: "unknown request", "miss"

### Run & output organization

**Run**: One execution of a skill (or the Autonomous Analysis Path) that produces a single output directory and one `analysis://` memory record. Surfaces and the desktop frontend already model this unit (`OutputRun`, `run_meta`, `run_id`). The Project-scoped layout below is decided in ADR 0035.
_Avoid_: "job" (the remote-router lifecycle shape), "task", "analysis" (a Run *is* an `analysis://` record, but the word also names the routing intent)

**Project output directory**: The on-disk projection of a Project, holding every Run of that Project. Its directory **name** is a readable `<name-slug>__<short-id>` (or the literal `default`) — *not* the raw `project_id`, because a Bench `project_id` is an opaque `uuid.uuid4().hex` and opaque project folders would re-introduce the very confusion this layer removes. The `short-id` is deterministic (`hash(project_id)[:10]`), and the name is **frozen at creation**: a later thread rename updates `project_meta.json` + memory only, never the folder (moving it would dangle every stored `output_path`). The canonical `project_id` (the **same** id as the Bench investigation thread `project://<project_id>`, ADR 0018) is recorded inside the directory's `project_meta.json`, which is the authoritative `project_id`↔directory mapping — never parse the folder name for it. One Project, two projections (a memory subtree and an output directory), not two concepts; `project_id` already reaches the runtime as `thread_id` for memory scoping, and this extends it into output-path resolution.
_Avoid_: "output project", "workspace project" (would re-fork the single Project concept), "results root"

**`default` project**: The reserved fallback Project for Runs with no explicit thread — today every CLI, stateless `/chat`, and channel-bot Run. Its output directory is the literal `<output root>/default/`; it is the filesystem analogue of memory's no-thread `analysis://typed/*` convention. A `default`-project Run can later be reassigned to a real Project (reassignment policy — move vs re-tag — is an open question, see below).
_Avoid_: "misc", "scratch", "uncategorized", "inbox"

**Run directory**: A Run's on-disk folder at `<project output directory>/<skill>[__<method>]__<YYYYMMDD_HHMMSS>__<dataset-slug>-<uid8>/`. The leaf is the Run's `run_id` and must match the frontend run-dir pattern (`^[^/]*__\d{8}_\d{6}__[^/]+$`, `run-link.ts`). The trailing token pairs the input **dataset** slug (readable at a glance) with a mandatory `uid8` (keeps `run_id` **globally unique** — the desktop `run_meta` keys on it). The directory is reserved atomically (`mkdir(exist_ok=False)`), and the resolver picks the final name up front — there is no post-run rename (ADR 0035 constraints).
_Avoid_: "run folder"; a pure-dataset leaf with no uid (it collides on `run_id`)

**project_meta.json**: The per-Project file at the Project output directory root recording the canonical `project_id`, display name, and creation metadata — the durable `project_id`↔directory binding (mirrors cellclaw's `project_meta.json`).
_Avoid_: "project config", "manifest" (manifest is per-Run)

**Run index**: A rebuildable `index.jsonl` at each Project output directory root, one line per Run (`schema_version`, `project_id`, `run_id`, skill, method, dataset, status, `manifest_mtime`, `path_rel`), appended by the single path resolver via a lock-protected / `O_APPEND` single-line write. It is a **derived cache** for fast listing — the Run directories and their `manifest.json` are the only source of truth, so a reader that hits a missing file, a half-written/corrupt line, or an `mtime` mismatch rebuilds by walking (`oc outputs reindex`, or lazily in `/outputs/latest`). The memory graph (`analysis://`) and the desktop SQLite `run_meta` are **reconciled views**: they may cache an `output_path`, but `manifest.json` alone defines where a Run lives, and a reindex repairs stale cached paths.
_Avoid_: "run database", "registry" (overstates it — it is a rebuildable cache)

### Prompt Prefix & Caching

The vocabulary for maximizing the LLM provider's automatic prefix cache (DeepSeek's `prompt_cache_hit_tokens`, OpenAI's `prompt_tokens_details.cached_tokens`). The mechanism is byte-exact: the provider caches the longest request prefix that is byte-identical to a recent request, so cache hit rate is governed entirely by **how stable the front of the request is across turns of one session**. OmicsClaw targets automatic prefix caching, **not** Anthropic `cache_control` breakpoints (decided 2026-05-30). Inspired by `DeepSeek-Reasonix`'s 99.82%-hit design, adapted to OmicsClaw's multi-surface, multi-user, layered assembler.

**Prompt prefix**: The leading span of the request — serialized `tools` + the `system` message — that the provider keys its cache on. Everything the provider sees before the first turn-varying byte.
_Avoid_: "system prompt" (only one part of the prefix; tools come first), "context".

**Stable prefix invariant**: The rule that, within one session, the Prompt prefix must be **byte-identical across consecutive requests**. The provider's prefix cache fails from the first differing byte onward, so a single mid-prefix change discards the cache for that change point *and the entire conversation history that follows it*. The whole caching design is the enforcement of this one invariant.
_Avoid_: "prefix should be similar", "mostly stable".

**Volatile context**: The per-turn content that changes with the current query — query-gated rule layers, matched-skill / capability / knowledge-guidance / plan context, **query-ranked `scoped_memory_context`**, **volatile session work-state `project_state_context`** (current dataset / recent analyses / insights — Decision-2), **query/skill/domain-matched `knowhow_constraints`**, and the Analysis Router's route / autonomous-understanding / assisted-parameterization context. To preserve the Stable prefix invariant it is rendered into the **latest user message** (`placement="message"` for layers; prepended as `user_turn_context` for route context), landing at the append-only tail of history, never in the `system` prefix. The classifier is *volatility*, not semantic role: a layer goes here iff its content varies within a session, regardless of whether it reads as an "instruction".
_Avoid_: "dynamic prompt", "context injection" (too broad — covers stable layers too).

**Session prefix snapshot**: The session-stable `system` tier (base persona, surface voice, output style, extension packs, mcp instructions, workspace context, and **session-scoped `memory_context`** — **durable identity only** since Decision-2: user preferences + project context, which change only on a memory *write* to those types, not per query; the volatile work-state slice — current dataset / recent analyses / insights — moved to the message tail as `project_state_context`) plus the Frozen tool list. v1 keeps it byte-stable **by construction** — deterministic re-assembly of session-constant inputs each turn — rather than a literal cached freeze object (that freeze is a deferred perf optimization). It changes only at a deliberate, logged **cache re-warm**: a model switch, a **durable-memory** write (preferences/project, refreshing `memory_context`), a context collapse, or a session-start/resume hook injecting content. The analogue of Reasonix's `addTool`/`replaceSystem` invalidation.
_Avoid_: "cache warmup", "prompt cache" (the cache is the provider's; this is the stable input tier).

**Frozen tool list**: The session's tool payload, filtered once by `surface` (a session constant) and then frozen in static registration order. Per-turn query-keyword gating (the former tool-list-compression) is dropped: once tools live in a cached prefix, hit-token pricing (~10% of miss) makes compression a net loss. Unlike Reasonix, OmicsClaw needs **no** locale-independent re-sort — its tools are statically registered, so order is already deterministic; the only requirement is to stop varying the subset per request.
_Avoid_: "lazy-load tools", "tool compression" (the retired per-turn behavior).

**Cache-hit diagnostics**: The per-turn observability that reads `hit`/`miss` tokens from provider usage, computes a hit ratio, and — when a miss is unexpected — infers the reason by hashing prefix segments (tools / stable-system) and comparing against the prior turn (`tool-list-changed`, `system-changed`, `cold-start`, …). The Reasonix feature that turns the Stable prefix invariant from a hope into a measured property.
_Avoid_: "cache metrics" (too vague), "token accounting" (that is billing, a superset).

## Relationships

- A **Surface** holds one **MemoryClient** per request context.
- A **MemoryClient** holds one **MemoryEngine** reference and one **Namespace** string.
- A **MemoryClient** routes each `remember()` call to either a **Versioned upsert** or an **Overwrite upsert** based on the **Memory URI**'s domain.
- A **MemoryEngine** writes a `(domain, path)` row partitioned by **Namespace**; **Read fallback** to `__shared__` happens at query time, not at write time.
- A **ReviewLog** reads the same database as **MemoryEngine** but exposes only **Cold path** verbs.
- A **Memory URI** with domain `core` and path starting with `agent` / `kh` / `my_user_default` is **routed to** `__shared__` by `namespace_policy` whenever something writes there. Both `core://agent` and `core://kh/*` are now wired: every memory-init path (Compat bot, MemoryClient legacy db_url, app/server.py chat lifespan, memory/server.py lifespan) calls `seed_knowhows()` after `init_db()`, mirroring the on-disk KH corpus into `__shared__` under `core://kh/<doc_id>`. `core://my_user_default` remains a reserved prefix awaiting a writer. Everything outside those three prefixes lives in the caller's current **Namespace**.
- A **Versioned upsert**'s `migrated_to` chain is the only structure where **ReviewLog.rollback_to** can operate.
- The **Analysis Router** classifies analysis-intent requests before execution; non-analysis chat stays on the normal conversational path.
- An **Exact skill match** uses **Deterministic route, assisted parameterization**.
- A **Partial skill match** uses **Skill-first composition**, then the **Autonomous Code Mini-Agent** handles the uncovered work through the **Autonomous Code Runner** boundary.
- A **No skill match** enters the **Autonomous Analysis Path** and uses the **Autonomous Code Mini-Agent** directly.
- The **Analysis Router** submits deterministic analysis routes as planned tool calls through the existing tool policy, approval, callback, result-store, and transcript pipeline.
- The **Autonomous Code Runner** is composed by the **Analysis Router**; under ADR 0032 its **Skill-handle facade** wraps the existing skill runner instead of importing skill scripts directly.
- The **Legacy custom analysis adapter** (`custom_analysis_execute`) was removed in the ADR 0032 single-engine consolidation; the **Autonomous Code Mini-Agent** (`autonomous_analysis_execute`) is the only generated-code route.
- The **Autonomous Code Runner** hosts the **Autonomous Code Loop**; the outer chat engine can invoke it but is not replaced by it.
- The **Autonomous Code Loop** uses **Evidence-bound repair** inside bounded step / failure / budget limits.
- The **Skill-handle facade** is the only approved way for generated autonomous code to invoke OmicsClaw skills; raw subprocess access from generated cells remains blocked.
- The **Persistent autonomous kernel session** lives only for one autonomous run and must run inside the **Autonomous Kernel Safety Envelope**.
- A successful autonomous run emits a **Replay artifact** and validates it in a fresh isolated process before the outer loop accepts the run as successful.
- Every Autonomous Code Runner command is assigned a **Code Runner Permission Tier**; **`system_mutation`** is blocked unless the user explicitly approves it.
- The **Autonomous Code Runner** writes by default only inside its **Autonomous run workspace**.
- The **Autonomous Code Runner** exposes an **Autonomous run lifecycle**, not just a synchronous tool-result string.
- **Lifecycle-shape compatibility** keeps Autonomous run records compatible with existing job UI expectations while avoiding a hard dependency on the remote jobs router.
- A **Partial skill match** passes prior skill outputs to the **Autonomous Code Runner** as **Upstream artifact references** by default, not by copying large artifacts.
- **Output-shape parity** lets CLI, Desktop, memory, and review tooling read Autonomous Code Runner outputs through the same manifest and completion-report conventions used for skill outputs.
- A **No skill match** or **Partial skill match** that carries a trusted input file passes through **Data-grounded autonomous planning** — deterministic `inspect_data` + schema injection + consequential-ambiguity resolution — before the **Autonomous Code Mini-Agent** is invoked.
- **Outer autonomous seams** mean the mini-agent gets full handoff of execution, not full handoff of judgment.
- **Autonomous result validation** (outer loop), not **Evidence-bound repair** (mini-agent), decides whether a run satisfied the user request; on failure it triggers a bounded re-delegation to the **Autonomous Code Runner**.
- The **Prompt prefix** is the **Frozen tool list** (serialized first) followed by the stable `system` layers of the **Session prefix snapshot**; the **Stable prefix invariant** keeps it byte-identical across a session's turns.
- The **Frozen tool list** is filtered once by the **Surface** (`surface` is a session constant), so per-Surface tool sets never break the **Stable prefix invariant** — different Surfaces are different sessions, not different turns.
- **Volatile context** — per-turn `memory` recall, volatile session work-state (`project_state_context`: dataset/analysis/insight — Decision-2), `capability`/`skill` hints, query-gated rule layers, and the **Analysis Router**'s route context — is rendered into the latest user message (`placement="message"`), landing on the append-only tail; it never enters the `system` prefix, so it cannot break the **Stable prefix invariant**.
- A **Session prefix snapshot** is invalidated only by a deliberate, logged **cache re-warm**: a model switch, a **durable-memory** write to preferences/project (which refreshes the snapshotted session-scoped `memory_context`; volatile dataset/analysis/insight writes ride the message tail as `project_state_context` and do not re-warm — Decision-2), or a context collapse. Between re-warms, history is append-only.
- Context collapse (the existing `CONTEXT_COLLAPSE` / `AUTO_COMPACT`) is the sole overflow handler: it folds old messages into a frozen `system` summary (one **cache re-warm**) so history stays append-only between collapses — replacing the former per-turn `trim_history_to_budget` sliding window, which shifted the history prefix every turn and discarded all history caching for sessions past the window.
- **Cache-hit diagnostics** hash the **Frozen tool list** and the stable `system` prefix each turn: an unexpected provider miss with unchanged hashes is reported `history-shifted`, a changed tool hash `tool-list-changed`, a changed system hash `system-changed` — turning the **Stable prefix invariant** into a CI-assertable regression guard.
- A **Run** is written into a **Project output directory**; its `project_id` is the same id as the Bench thread `project://<project_id>` (ADR 0018), and a Run with no active thread lands in the **`default` project**.
- All four Surfaces (CLI, Desktop `/chat`, Channel, the agent loop) resolve a **Run directory** through one shared path resolver, which also writes **project_meta.json** and appends the **Run index**. Today these paths diverge (`omicsclaw.py`, `skill/runner.py`, `skill/chain.py`, `runtime/tools/builders/agent_executors.py`); converging them is the core engineering change.
- The **Run index**, the memory graph's `analysis://<project_id>/typed/<run_id>` records, and the desktop **`run_meta`** rows are all **reconciled views** keyed by `run_id` over the same Run directories + `manifest.json`; the directories are the single source of truth and every view is rebuildable from a walk. Views may cache an `output_path` (memory's `_auto_capture_analysis` does today), but only the manifest/index defines where a Run lives, and a reindex repairs stale paths.

## Example dialogue

> **Dev:** "If a Telegram user updates their `qc_threshold` preference, does the old value disappear?"
> **Architect:** "No — `preference://*` is in `VERSIONED_PREFIXES`, so `MemoryClient.remember()` routes to a **Versioned upsert**. The old `Memory` row stays with `deprecated=True`; **ReviewLog.list_version_chain** can find it; the user can roll back via the Desktop review UI."
>
> **Dev:** "Same user processes `pbmc.h5ad` in two different workspaces — what happens?"
> **Architect:** "Two different **Namespaces**, two independent `paths` rows. The same file produces two `dataset://pbmc.h5ad` entries. **Read fallback** doesn't connect them because `dataset://*` is per-Namespace, not shared."
>
> **Dev:** "I bind the keyword 'TIL' to a shared OmicsClaw concept node — do other users see it?"
> **Architect:** "Only if you call `add_glossary_shared('TIL', node)`. Plain `add_glossary` writes the binding under your current **Namespace**. **Read fallback** surfaces shared bindings to everyone, but your private one stays private."
>
> **Dev:** "The user asks for a built-in clustering plus a custom publication figure."
> **Architect:** "That is a **Partial skill match** using **Skill-first composition**: the clustering runs through the shared skill runner, then the figure is produced through the **Autonomous Analysis Path**."
>
> **Dev:** "The user typed a file path this turn, so the file tools appeared in the request. Next turn they didn't — does that hurt?"
> **Architect:** "It used to: per-turn query-keyword gating changed the **Frozen tool list**, breaking the **Stable prefix invariant** at the tools segment and discarding the cache for the whole request. We dropped that gating — the tool list is now frozen per session, so the path-mentioning turn and the next turn send a byte-identical **Prompt prefix**. The file-path *rule layer* still appears adaptively, but as **Volatile context** in the user message, where it costs nothing in cache."
>
> **Dev:** "A 60-message session shows hit_ratio 0.95 on turn 30, then 0.0 on turn 31. What broke?"
> **Architect:** "Read the **Cache-hit diagnostics** miss reason. `system-changed` means a `memory` write re-warmed the **Session prefix snapshot** — expected, one turn. `history-shifted` with unchanged hashes would mean a context collapse folded old messages (also expected). `tool-list-changed` would be a real regression — something re-introduced per-turn tool variation."

## Resolved-by-default decisions

Decisions that the refactor PRs (#125–#132) chose by sensible default rather than explicit RFC:

- **One database, many Namespaces** — `OMICSCLAW_MEMORY_DB_URL` selects a single SQLite/Postgres database; Namespace columns partition the data inside it. Different desktop launches with different `OMICSCLAW_DESKTOP_LAUNCH_ID` values share the same DB but get distinct Namespaces. Dropping a per-launch DB option keeps cross-launch read-fallback (`__shared__`) working with no extra cross-DB plumbing.
- **`oc interactive` from `~/`** — uses the absolute home path as Namespace. No special-case handling; `~` is a valid string id like any other directory.
- **Read fallback policy is asymmetric** — `recall` and `search` fall back to `__shared__`; `list_children` and `get_subtree` do not. The asymmetry is deliberate: per-row fallbacks give per-user contexts visibility into globally-shared content, but per-listing fallbacks would pollute a user's own inventory with shared structure they didn't author.
- **No legacy output migration** — the pre-existing flat `output/<skill>__…__<uuid8>/` directories are disposable test data (owner's call, 2026-06-24); the Project layout is a clean cut-over with no migration step. The listing walk may still tolerate a root-level run dir (treating it as `default`) for safety, but no data is moved or preserved by contract.

## Open questions

Tracked but not yet resolved in code:

- **`_auto_capture_dataset` policy** — When the bot detects a dataset filename, should it write under the user's Namespace or `__shared__`? Today it goes user-scoped (per CompatMemoryStore default), which means the same `pbmc.h5ad` mentioned by two users gets two `dataset://` rows. Whether to deduplicate at `__shared__` is a UX question.
- **`default`→real-Project reassignment** — when a Run created in the **`default` project** is later attributed to a real Project, does the resolver *move* the **Run directory** (changing its absolute path — the frontend survives because **`run_meta`** keys on `run_id` and re-fetches paths, but memory `AnalysisMemory.output_path` would dangle) or only *re-tag* it in the **Run index** + memory (leaving the directory under `default`, so the disk projection and the logical Project diverge)? Unresolved; v1 may simply not support reassignment.

## Resolved (kept here for tombstone)

- ~~**`core://kh/*` seed bootstrap**~~ — **Resolved (PR #172, 2026-05-11)**: every `init_db()` caller invokes `seed_knowhows()`, which iterates `KnowHowInjector.iter_entries()` and writes each `(uri, content)` via the idempotent `MemoryEngine.seed_shared`. Same-content reseeds are no-ops; failures downgrade to a warning log and don't block startup.

## Cross-reference: Consensus runtime (forward-declared)

ADR 0010 (2026-05-18) introduces a new subsystem `omicsclaw/runtime/consensus/` that adds its own domain vocabulary. ADR 0011 amends the evaluation protocol; ADR 0012 adds the **interpreted layer** as a downstream skill (`consensus-interpret`) without weakening the A/B binary. The canonical definitions live in those ADRs' "Vocabulary" sections — listed here so cross-subsystem readers can recognise the terms when they appear in skill code, ADRs 0010/0011/0012, and reports. **These terms migrate to `omicsclaw/runtime/consensus/CONTEXT.md` once that directory exists**; until then, the ADRs are the source of truth.

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
- **Interpreted consensus** — a *downstream* skill (`consensus-interpret`) that reads a verified typed consensus run and produces a biologically grounded interpretation. Lives under `analysis://interpreted/<typed_run_id>` and MUST cite the typed run as evidence base. Banner: `[A+I: Interpreted on verified consensus]`. **Not a third path** — strictly a consumer of A-path output; the A/B binary established by ADR 0010 is preserved. Defined in ADR 0012. _Avoid_: "consensus annotation" (collides with cell-type annotation skills), "consensus narrative" (reserved for B path).
- **Verified consensus run** — synonym for a typed consensus output directory (`analysis://typed/<run_id>`). Term used by `consensus-interpret` and any downstream consumer to refer to "the thing we are interpreting". _Avoid_: "consensus result" (overloaded).
- **Interpretation faithfulness** — fraction of LLM-generated claim sentences in an interpreted report that contain at least one verbatim citation of a typed-run artifact (cluster id, NMI value, marker name, p-value, etc.). Floor: 100% as a structural invariant (grep-tested per ADR 0012 §"T3 invariants"); also tracked as a soft regression metric. _Avoid_: "interpretation accuracy" (different — not the same as biological correctness).
- **Marker grounding** — invariant that every cell-type claim in an interpreted report must cite ≥1 marker drawn from inline per-cluster DE intersected with the bundled marker DB. Quantified by `marker_grounding_rate` (Jaccard of LLM-claimed top-K markers vs DE-derived top-K markers per cluster). Floor: 0.60. _Avoid_: "marker validation" (vaguer; covers many distinct activities).
- **Backward proof-driven recommendation** — `consensus-interpret` β output: top-3 next-step skill suggestions, each MUST cite ≥1 specific typed-run artifact row (`evidence_refs`). Distinct from forward routing (`orchestrator` skill: `query → skill`); backward direction is `result_artifacts → (skill, evidence)`. Capped at top-3 by `priority` (specificity-to-evidence). _Avoid_: "next step" (too vague), "recommendation" (collides with generic chat agent verb).

### Relationship to Memory System

A consensus run writes to graph memory under either `analysis://typed/<run_id>` or `analysis://exploratory/<run_id>` per the **A path vs B path** distinction above. The `analysis://*` Domain is already covered by [**Versioned upsert**](#write-modes) semantics — consensus runs do not introduce a new write mode. The `typed/` vs `exploratory/` sub-prefix is a structural URI convention enforced by `runtime/consensus/dispatch.py`, not a new Domain.

## Flagged ambiguities

- **"user"** is used three ways: (1) the human researcher (`core://my_user`), (2) the chat-side participant (`Session.user_id`), (3) the Linux process owner. In this doc "user" means (1) or (2) — context determines which; never (3).
- **"workspace"** appears in both the **Surface** layer (the directory the user picked, used as the Namespace string) and the `ScopedMemory` layer (a filesystem root). Same physical directory, different concepts; will collapse only if ScopedMemory is integrated into MemoryEngine.
- **GraphService is retired.** Production code uses `MemoryEngine` / `ReviewLog` / `MemoryClient` exclusively. The legacy path-based admin operations (`/api/browse/*` write endpoints) live in a private `omicsclaw/memory/api/_browse_helpers.BrowseHelpers` class consumed only by the `oc memory-server` admin UI — do not import from outside `omicsclaw/memory/api/`. A future rewrite can port the admin UI against `MemoryEngine` and delete this module entirely.
- **"namespace"** vs **"domain"**: domain is the URI prefix (`dataset`, `core`, …); namespace is the user/workspace isolation key. They are orthogonal columns; never use one as a synonym for the other.
