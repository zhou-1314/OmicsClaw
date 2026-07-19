# OmicsClaw Control Plane, Ingress, and Memory Context

The graph-backed agent memory at `omicsclaw/memory/` and the control-plane identity context around it. Memory records what the Owner has told OmicsClaw — preferences, dataset / analysis / insight lineage and Project knowledge — while Control Plane State determines which Projects, Conversations and Turns exist across CLI, Desktop and Channel Surfaces.

> **Scope.** This file documents the Memory System and the Ingress / Surface layer (the three user-facing entry points that normalize conversational input before `dispatch()`). If the Skills system or other subsystems acquire enough domain vocabulary to warrant their own CONTEXT, split via a top-level `CONTEXT-MAP.md` per [CONTEXT-FORMAT.md §"Single vs multi-context repos"].

## Language

### Identity & Addressing

**Owner**: The sole human served by one OmicsClaw backend instance and the owner of all non-system state in that instance.
_Avoid_: "User" (implies a multi-user registry), "tenant", "account", "Linux owner", "sender"

**Owner Identity**: A Surface- or Channel Adapter-issued subject identifier explicitly configured to authorize the Owner. Multiple Owner Identities may exist, but all represent the same Owner. It is used at ingress and never owns or partitions durable domain state.
_Avoid_: "User ID", display name, an inferred identity match, a second User, an Authentication Session, a Memory Namespace

**Source Attribution**: Minimal non-secret metadata identifying the Surface, Channel Adapter, Owner Identity reference, and provider message involved in an accepted inbound message. It supports audit and reply diagnostics but never determines state ownership or authorization by itself.
_Avoid_: authentication credentials, provider token, storage partition, Conversation ID, Reply Target

**Source Request ID**: A stable opaque identity assigned by the provider or local client to one retryable inbound submission before the control plane accepts it.
_Avoid_: Turn ID, content hash, arrival timestamp, Conversation ID, display text

**Ingress Idempotency Key**: The Surface-scoped `(Surface, Source Namespace, Source Request ID)` identity used to recognize repeated delivery of one inbound submission.
_Avoid_: Turn ID, Source Request ID without a namespace, message-content digest, Conversation ID

**Ingress Idempotency Binding**: The restart-resilient control-plane relation from one Ingress Idempotency Key to its canonical Turn ID and versioned request fingerprint.
_Avoid_: TTL dedup cache, persistent task, replay record, content-equality index

**Control Plane State**: The sole logical durable authority for Project, Conversation, active-binding, Turn-receipt, Run-receipt, ingress-idempotency, Run-submission-binding, Execution-Assignment and Outbound-Delivery identity and lifecycle facts, persisted by the Control Database.
_Avoid_: Control Database file, Memory graph, Transcript content, Run artifacts, Surface process globals

**Control Database**: The Backend-exclusive, always-on local SQLite `control.db` that physically persists Control Plane State for exactly one single-process control plane.
_Avoid_: graph `memory.db`, `transcripts.db`, CLI `sessions.db`, Desktop App `omicsclaw.db`, all-purpose monolithic database, Worker database

**Canonical Transcript Store**: The Backend-owned independent SQLite `transcripts.db` that owns immutable, Turn-attributed provider-message and terminal entries plus one replaceable active view per canonical Conversation. It owns content, not Conversation or Turn existence; Control binds it by an opaque immutable Transcript Store identity rather than treating a filesystem path as authority.
_Avoid_: Control Database, legacy write-through chat mirror, CLI `sessions.db`, Desktop App message cache, Memory graph, second Conversation authority

**Terminal Transcript Candidate**: An immutable terminal entry staged in the Canonical Transcript Store before the authoritative terminal Turn transaction. It may become committed only after the same Turn Receipt atomically records its entry ID and content digest, or become abandoned when no such terminal authority exists.
_Avoid_: terminal Event, unverified assistant message, durable Turn status, mutable draft, Delivery Item

**Turn Terminal Transcript Reference**: The immutable `(entry_id, content_sha256)` reference committed in Control Plane State with a terminal Turn Receipt. Startup and observation verify it against the bound Canonical Transcript Store before treating terminal content as available.
_Avoid_: Transcript body in `control.db`, path, latest-message lookup, optional cache hint, provider message ID

**Legacy Identity Map**: The auditable migration relation from a bounded legacy Project, Session or Surface key to its canonical control-plane identity.
_Avoid_: runtime fallback, External Identity mapping, Owner Identity, parseable canonical ID, permanent second registry

**Transcript Migration Profile**: An Owner-reviewed, versioned offline mapping from every inventoried legacy Backend Transcript stream to one explicit Surface, Reply Target and active-selection decision. It is input to manifest-bound `plan/apply/verify`; it is never inferred at runtime or used as a fallback lookup.
_Avoid_: timestamp merge, guessed address, automatic Session resume, live compatibility registry, dual-write configuration

**Raw Inbound**: A transport-shaped, not-yet-accepted conversational input emitted by a Surface after provider authenticity checks and basic decoding. It may expose an external subject, Reply Target, content, side-effect-free Source Attachment Descriptors, and transport metadata, but it is not safe for Agent or durable-state use.
_Avoid_: Inbound Envelope, MessageEnvelope, accepted message, Transcript entry

**Source Attachment Descriptor**: A side-effect-free, serializable, Surface/source-scoped description of one attachment declared by Raw Inbound. It contributes to the request fingerprint before any download, copy, or durable write and contains no credential, expiring URL, local path, or attachment bytes.
_Avoid_: Attachment ID, Attachment Reference, provider credential, signed URL, temporary path, Base64 payload

**Attachment ID**: A globally unique opaque identity generated by the control plane for one proposed and then accepted inbound attachment occurrence. It carries no digest, path, provider-key, Turn, Conversation, Project, or Owner Identity semantics.
_Avoid_: blob digest, provider file key, filename, Turn ID, parseable composite key

**Attachment Record**: The immutable Attachment Store record for one accepted attachment occurrence, owned by exactly one Turn and therefore exactly one Conversation. It records the Attachment ID, declared metadata, verified full-content digest, blob reference, ordinal, size, media type, and integrity state without embedding bytes or an executable provider locator.
_Avoid_: Attachment Blob, Transcript message, latest-Session file, mutable upload row, provider object

**Attachment Blob**: Immutable bytes addressed by their verified full SHA-256 digest in the Attachment Store. Multiple Attachment Records may share one Blob when distinct message occurrences contain identical bytes.
_Avoid_: Attachment Record, filename, Workspace path, provider URL, semantic deduplication identity

**Attachment Reference**: A versioned structured reference to one accepted Attachment ID carried by Inbound Envelope, Transcript, prompt rendering, or tool input. Resolution is authorized through the owning Turn and Conversation; the reference never exposes bytes, a provider handle, signed URL, temporary path, or mutable registry entry.
_Avoid_: filesystem path, Base64 content, Attachment Record copy, latest received file, provider file key

**Attachment Store**: The Backend-owned specialized content store authoritative for Attachment Records, Attachment Blobs, staging, integrity verification, provisional publication, acceptance finalization, and reference-aware garbage collection. It cannot establish that a Turn exists; only a committed Turn Receipt in Control Plane State can do that.
_Avoid_: Control Database, Workspace upload folder, global received-files registry, provider CDN, Transcript database

**File Reference**: An authorized structured reference to a pre-existing local Workspace file selected without uploading it as part of the inbound submission. It follows Workspace authorization and mutation semantics rather than Attachment Record retention semantics and must not be silently converted into an Attachment Reference.
_Avoid_: Attachment Reference, absolute path in prompt text, provider attachment, durable copy by implication

**Ingress Normalizer**: The single in-process control-plane Module that owns Owner admission, Conversation and Project resolution, normalized content, Source Attachment Descriptor fingerprinting, Attachment Store coordination, File Reference validation, Source Attribution, and Inbound Envelope construction. Prompt-toolkit/single-shot CLI, the Desktop text and multipart-image paths, and Owner-only Telegram text/single-photo path currently use its supported subset through Control Runtime; Textual TUI remains a legacy migration path and non-Telegram Channel Adapters are disabled pending cutover. The accepted target routes every conversational Surface through it.
_Avoid_: Channel Adapter, HTTP gateway, queue, Agent Loop, Surface-specific helper

**Inbound Envelope**: Pure, immutable, versioned, JSON-compatible domain data produced only by the Ingress Normalizer. It records the normalized facts, ordered Attachment References, File References, and requested options of one accepted conversational Turn and is accepted by `dispatch()` only alongside a fresh Dispatch Context.
_Avoid_: Raw Inbound, live callback, effective policy, authorization capability, `chat_id`, SDK event, arbitrary `llm_tool_loop` kwargs

**Dispatch Context**: The process-local, per-invocation capability container passed beside Inbound Envelope to `dispatch()`. It holds live cancellation, approval, usage, effective-policy, tracing, Response Sink, or similar runtime ports and is never serialized, persisted, hashed into the Envelope, or reconstructed from inbound data.
_Avoid_: message metadata, Transcript state, replay record, serializable policy request, durable context

**MessageEnvelope**: The incomplete implementation DTO consumed by the legacy Agent dispatcher. It mirrors `llm_tool_loop` kwargs and mixes request data with live `usage_accumulator`, approval, policy, and cancellation objects. Prompt-toolkit/single-shot CLI, Desktop text/multipart-image and Telegram text/single-photo Turns now construct it only inside the Control Runtime's Agent Worker Adapter after Turn activation; Textual TUI and disabled legacy Channel implementations may still construct it at their Surface entry points. It is migration input to the Inbound Envelope plus Dispatch Context design, not the canonical domain contract.
_Avoid_: canonical domain contract, accepted Surface input, durable Envelope

**Run Request**: A typed request from an already-authenticated control-plane caller for an explicitly non-conversational deterministic execution through the Run Executor facade. It carries explicit inputs and one admission-resolved immutable **Run Scope**—a validated active Project or explicit Unassigned—creates no Conversation, writes no Transcript, and produces no chat reply.
_Avoid_: user chat, fake Conversation, Agent-loop shortcut, generic background task

**Project**: A durable Owner research-continuity aggregate with one opaque Project ID that groups related Conversations, Memory knowledge and Runs across Surfaces.
_Avoid_: Bench UI thread, `project://` Memory subtree, Project output directory, Workspace, Unassigned Run Grouping

**Project Record**: The minimal authoritative Control Plane State record that establishes one Project's identity, current display metadata and current `active` or `archived` lifecycle state.
_Avoid_: `project://<id>`, `project_meta.json`, output folder, Run index, display-name cache

**Project Archive**: The reversible Control Plane State transition that retains one Project and all of its references and content while closing it to new scientific work until restored.
_Avoid_: delete, soft-delete, trash, purge, hiding a Bench thread

**Project Projection Intent**: A content-free Control Plane State fence created while a Project is active that authorizes one exact, digest-bound, idempotent scientific Memory projection from an already accepted Turn or Run, including after archive.
_Avoid_: scientific content, unrestricted post-archive write, durable task payload, Memory row, projection cache

**Workspace dataset observation**: A versioned scientific observation of one authorized pre-existing Workspace file, identified by Workspace, normalized relative path, and verified content digest when available.
_Avoid_: filename-only dataset, Attachment Record, Project-owned copy, Owner Identity Namespace

**Project Dataset Reference**: Project-scoped scientific Memory that cites a Workspace dataset observation or immutable Attachment Record without moving, copying, or retagging the canonical source identity.
_Avoid_: canonical dataset ownership, Run Scope mutation, symlink membership, duplicate dataset row

**Conversation**: An ordered message stream and continuity context for the Owner at exactly one immutable Conversation Address, such as a Desktop chat, CLI interaction, direct chat, or Channel thread.
_Avoid_: `chat_id` (an overloaded implementation field), "Session" (a legacy implementation term), "reply target"

**Turn**: One accepted Inbound Envelope's ordered conversational execution within exactly one Conversation, from activation before its first Transcript access through its terminal Event.
_Avoid_: message, Run, task, Conversation, persistent job

**Turn ID**: A stable opaque identifier generated by the control plane for exactly one accepted Turn, carrying no Conversation, Surface, Reply Target, Project, Owner Identity, transport-message, or Run semantics.
_Avoid_: Conversation ID, Run ID, Source Message ID, client request ID, parseable composite key

**Turn Receipt**: The minimal durable, content-free lifecycle record for one Turn, containing identity, status, timestamps, an optional status-specific code from the closed non-secret vocabulary, and optional retry provenance but no executable payload or process capability.
_Avoid_: persistent queue item, Transcript message, Run record, serialized Inbound Envelope, task

**Turn Execution**: The process-local execution capabilities and scheduler entry for one Turn, keyed by Turn ID and never reconstructed from its Turn Receipt.
_Avoid_: Turn Receipt, durable job, replay record, Conversation state

**Turn Sequencer**: The bounded process-local FIFO single-writer gate that permits at most one active Turn per Conversation while allowing different Conversations to execute concurrently.
_Avoid_: global queue, persistent task center, Run Executor, Transcript write lock, Worker queue

**Control Runtime**: The deep Backend composition Module whose Interface starts Attachment, canonical Transcript and control reconciliation, accepts Raw Inbound with fresh process-local ports, either returns after durable acceptance or waits for the authoritative terminal Turn Receipt, exposes a verified read-only Turn snapshot plus atomically opened replay/live observation, cancels by Turn ID, and closes the lifetime Control Database, Attachment Store and Canonical Transcript Store owners. It hides the Control Repository, Ingress Normalizer, Turn Sequencer, bounded Turn Event Hub, Execution Coordinator, terminal candidate protocol and current Agent Worker Adapter from a cut-over Surface. Its Channel composition additionally owns the Delivery Pump and committed-Transcript content resolver. Today prompt-toolkit/single-shot CLI, Desktop text/multipart-image and Telegram text/single-photo input use it; Textual TUI and other Channel Adapters do not.
_Avoid_: Surface dispatcher helper, persistent task queue, second Control Plane State owner, legacy MessageEnvelope builder at a Surface

**Conversation ID**: A stable opaque identifier generated by the control plane that carries no Owner Identity, Surface, Reply Target, thread, or Project semantics.
_Avoid_: `platform:user_id:chat_id`, a parseable composite key, a transport message id

**Reply Target**: The normalized, serializable, stable logical destination used to deliver responses within a Surface. Channel targets include the Adapter/provider-account namespace, destination id, and thread/topic components needed for uniqueness. It is never a live SSE connection, terminal handle, SDK client, or callback.
_Avoid_: "Conversation id", a bare `chat_id`, Owner Identity, Response Sink, "Session id" (legacy implementation name)

**Conversation Address**: The immutable `(Surface, Reply Target)` pair stored by a Conversation. It constrains where that Transcript may continue without being part of or encoded into Conversation ID.
_Avoid_: Conversation ID, Active Conversation Binding, physical connection, movable route

**Response Sink**: A process-local observer attachment that renders one Turn's live Events or ephemeral progress and may detach or be replaced without owning Turn lifecycle or canonical terminal Channel delivery.
_Avoid_: Reply Target, Conversation Address, Outbound Delivery, serialized callback, durable state owner, cancellation trigger

**Turn Event Hub**: The process-local per-Turn EventFrame buffer and observer registry owned by Control Runtime. It binds producer authority to the Turn's opening event loop; bounds Turn count, retained frame count, live observers per Turn and each observer queue; assigns one-based monotonic sequence numbers and emission timestamps; and decides replay/gap state plus live registration in one lock domain. It replays only complete retained history, reports a structured gap for an evicted cursor, wakes concurrent close, detaches slow or disconnected observers, and never becomes lifecycle or restart-replay authority. A strict retained-byte quota remains target work.
_Avoid_: durable event log, persistent chat queue, Outbound Delivery Outbox, Response Sink, Turn Receipt, cancellation owner

**Outbound Delivery**: The durable intent and operational lifecycle for making one accepted Channel Turn's frozen terminal reply or terminal notice visible at its immutable Reply Target.
_Avoid_: Turn, Response Sink, provider webhook acknowledgement, Desktop SSE replay, scientific retry, KG HandoffPacket

**Delivery ID**: A globally unique opaque identity generated by the control plane for one Outbound Delivery and carrying no Turn, Conversation, Surface, provider, target, ordinal or resend semantics.
_Avoid_: Turn ID, provider message ID, Reply Target, parseable composite key

**Delivery Target Sequence**: The monotonic integer allocated transactionally to each Outbound Delivery at one `(Surface, Reply Target)`, used as the durable target-local causal order for canonical replies and explicit resends.
_Avoid_: creation timestamp, Delivery ID ordering, Conversation FIFO, global Channel sequence

**Delivery Item**: One immutable ordered provider-send unit inside an Outbound Delivery, such as a bounded text chunk or one durable outbound media reference.
_Avoid_: Inbound Attachment, live Event, whole Turn, provider response, mutable filesystem path

**Suppressed Delivery Item**: A terminal Delivery Item that was intentionally not sent because an earlier Item in the same ordered Delivery became failed or acceptance-unknown.
_Avoid_: retryable Item, provider rejection, delivered Item, silently dropped suffix

**Delivery Attempt**: One recorded invocation of a Delivery Adapter for one Delivery Item with a classified acceptance outcome and bounded non-secret provider evidence.
_Avoid_: Turn retry, hidden Adapter retry loop, scientific execution attempt, provider message identity

**Delivery Adapter**: A Surface/provider-specific single-attempt port that sends one frozen Delivery Item to its Reply Target and classifies the provider result without owning retry policy or execution authority.
_Avoid_: Channel ingress Adapter, Response Sink, Delivery Pump, Agent, Worker

**Delivery Pump**: The restart-resilient in-process control-plane scheduler that claims due Delivery Items from the Outbound Delivery Outbox, invokes Delivery Adapters, and records outcomes without entering conversational or scientific execution.
_Avoid_: Turn Sequencer, Run Executor, cross-process Worker, external broker, KG handoff executor

**Outbound Delivery Outbox**: The bounded logical set of non-terminal Delivery Items durably represented in Control Plane State and processed by the Delivery Pump.
_Avoid_: persistent Turn queue, Event log, Desktop KG handoff outbox, `pending_media`, external message broker

**Active Conversation Binding**: The restart-resilient control-plane pointer from one Conversation Address to the Conversation currently selected there. It is navigation state: one per Reply Target, while older Conversations at that same address remain durable and addressable.
_Avoid_: Conversation ID, Transcript owner, Project binding, running task, Owner Identity mapping

**Conversation Resolver**: The Ingress Normalizer component that validates an explicit Conversation ID or follows/creates the Active Conversation Binding, then returns the opaque Conversation ID placed in Inbound Envelope.
_Avoid_: SessionManager composite-key lookup, Reply Target parser, Transcript loader

**Memory URI**: A `domain://path` string that names a memory's logical location, independent of any row id.
_Avoid_: "memory key", "memory id" (the latter is `Memory.id` — a row id of one content version)

**Domain**: The top-level segment of a Memory URI — one of `core`, `dataset`, `analysis`, `insight`, `preference`, `project`, `session`.
_Avoid_: "namespace" (orthogonal concept, see below), "category"

**Namespace**: A legacy Memory implementation partition stored as a column on `paths`, `search_documents`, and `glossary_keywords`. Current Surfaces inject workspace-, launch-, or Owner-Identity-derived strings. It is not a canonical Owner or Conversation identity. New architecture must organize state by explicit Owner, Conversation, Project, Workspace, Run, or system ownership and must never derive ownership from Owner Identity.
_Avoid_: "tenant", "User", Owner Identity, Conversation ID, "scope" (the `ScopedMemory` filesystem layer is a different concept)

**`__shared__`**: The reserved Namespace whose rows are visible to every other Namespace via Read fallback. Holds `core://agent`, `core://kh/*`, and the system glossary.
_Avoid_: "global", "default", "public"

**Read fallback**: The rule that `recall` and `search` automatically include `__shared__` results when the current Namespace doesn't match. `list_children` deliberately does NOT fall back, to prevent one context's inventory from leaking into another during subtree traversal.
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

**MemoryClient**: The strategy layer between a control-plane request context and `MemoryEngine`. It decides legacy Namespace via `resolve_namespace()` and version policy via `should_version()`. Current Surface paths configure it indirectly; the accepted target keeps it behind ingress/runtime state resolution rather than exposing it as a Surface-owned handle.
_Avoid_: "MemoryAPI", "MemoryFacade", Surface storage client

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

**Surface**: A user-facing entry point. Three Surfaces today: **Channel Surface**, **Desktop Surface**, **CLI Surface**. Prompt-toolkit/single-shot CLI conversational input, Desktop text/multipart-image and Owner-only Telegram text/single-photo input currently emit Raw Inbound to the shared Ingress Normalizer and render typed Events; the accepted target extends this to Textual TUI and the remaining Channel Adapters. Explicit non-chat Run Requests do not pass through conversational ingress.
_Avoid_: "entry" (overloaded with engine entry-point); "front-end" (overloaded with the Vue project under `frontend/`).

**Channel Surface**: The Surface that fans out to all IM platforms. Holds N **Channel Adapter** instances and is lifecycle-managed by `omicsclaw/surfaces/channels/manager.py:ChannelManager`. Today the wired adapters are Telegram, Feishu, Slack, Discord, WeChat, WeCom, DingTalk, iMessage, Email, QQ.
_Avoid_: "Bot Surface" (overloaded with the OmicsBot persona in `SOUL.md`), "IM Surface" (only some of the 10 adapters are IM in the strict sense — Email isn't).

**Channel Adapter**: The per-platform implementation that lives inside the Channel Surface. One file per platform under `omicsclaw/surfaces/channels/<name>.py`. Telegram now emits Raw Inbound for text and one ordinary photo; disabled legacy Adapters retain shared or open-coded MessageEnvelope paths. The accepted target is for every Adapter to emit Raw Inbound and never own normalization or Agent dispatch.
_Avoid_: "channel" (the bare word now denotes the Surface, not its adapters), "gateway" (the reference implementation's word, not ours), "backend" (clashes with LLM/storage/queue backend).

**Desktop Surface**: The Surface served by `omicsclaw/surfaces/desktop/server.py` (FastAPI). Its production compatibility text path requires an explicit Source Request ID and uses Control Runtime for durable acceptance, canonical Transcript terminalization and bounded Event observation. Its versioned `POST /v1/turns` multipart Adapter accepts 1–8 digest-declared images, returns after durable acceptance, and shares `/v1/turns/{turn_id}` receipt, Event and cancel Interfaces with their unversioned compatibility aliases. Matching retries return the original Turn before the upload source is opened. The transport Adapter owns counted bytes, strict UTF-8/depth, a finite read deadline, complete-boundary proof, provisional spool cleanup and bounded concurrency; configured Bearer policy is conditional in default loopback mode. Its Remote compatibility `POST /jobs` exact-demo Adapter directly submits a canonical non-chat `UnassignedScope` Run through the same Backend `RunRuntime`; it is an HTTP Adapter within this Surface, not a fourth Surface. Desktop File Reference, legacy JSON `files`, non-image input, requested options/Project commands, conversational `/chat/stream` legacy `job_id` binding and per-Turn provider credentials remain rejected; OmicsClaw-App has not yet adopted the new multipart or canonical Job Interfaces.

**CLI Surface**: The Surface served by `omicsclaw/surfaces/cli/interactive.py`, `tui.py`, and the root `omicsclaw.py` command Adapter (`oc interactive` / `oc run`). The prompt-toolkit REPL and single-shot conversational path emit Raw Inbound through Control Runtime and consume typed Events through an ephemeral Response Sink; `/new` moves the stable CLI Reply Target's Active Conversation Binding and cancellation targets Turn ID. Exact prompt-toolkit `/run <canonical-skill> --demo` and the root exact-demo Scope command family are typed non-chat Run Request Adapters through `RunRuntime`, not Conversations, Turns or Transcripts. The REPL always uses `UnassignedScope`. Root accepts exactly omitted Scope, fixed-order `--demo --project <32-lower-hex-id>`, or `--demo --no-project`: only omission may read the legacy current-Project navigation hint; explicit Project and explicit Unassigned bypass it. Novel explicit Project admission requires an existing active Control Project and never downgrades to Unassigned. Every root demo-shaped request is owned by the canonical boundary; aliases and all other option-bearing/malformed forms fail closed without legacy fallback. Root non-demo and unsupported option-bearing forms, Textual TUI `/run`, `/interpret`, and non-demo/option-bearing prompt-toolkit forms remain outside those Adapters; Textual TUI still constructs MessageEnvelope directly for conversation input.

### Current Surface Namespace derivation (legacy)

The wired Surfaces currently derive Namespace strings as follows. This table is
implementation evidence, not the accepted target ownership model:

| Surface | Helper | Namespace |
|---|---|---|
| CLI Surface | `cli_namespace_from_workspace(workspace_dir)` | absolute workspace path (cwd if unset) |
| Desktop Surface | `desktop_namespace()` | `app/<OMICSCLAW_DESKTOP_LAUNCH_ID>` or `app/desktop_user` |
| Channel Surface (per Channel Adapter) | `CompatMemoryStore` per-Conversation | `f"{platform}/{user_id}"` where `platform` is the adapter name (e.g. `telegram`, `feishu`, `slack`, ...) |
| System / boot scripts | constant | `__shared__` |

The `app/`, `<platform>/` prefixes prevent string collisions with absolute
filesystem paths, but the Channel row violates ADR 0045 by partitioning the
same Owner through transport identity. New code must not depend on this shape.

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

**Semantic artifact contract**: A non-AnnData producer/consumer interface identified by exact semantic `kind`, accepted/produced format, and a declared relative output path. It permits auditable table/VCF handoff without inferring meaning from filenames.
_Avoid_: "same filename", "generic file edge", "artifact guess"

**Content precondition contract**: A format-specific structural requirement under `interface.inputs.preconditions.content` that can be proven by the bounded Input Profile probe before routing or execution. Current facts cover tabular headers, VCF metadata headers, FASTQ record/mate layout, and governed Directory signatures; absent declarations remain uninspected semantics rather than guessed failures.
_Avoid_: "extension validation", "load the dataset", "filename heuristic"

**Directory signature**: A privacy-minimal semantic label emitted by the bounded directory probe for a recognised on-disk layout, such as `paired-fastq`, `tenx-matrix`, `cellranger-output`, or `starsolo-velocity`. The Input Profile never exposes an arbitrary directory inventory to routing.
_Avoid_: "directory listing", "path glob contract", "folder name guess"

**Method-scoped output guarantee**: An additional file, AnnData-field, or Semantic artifact guarantee that holds only when a skill runs with one of the canonical `--method` values declared by that scope. The methods must exist in `interface.parameters.hints`; a compatibility edge derived from the guarantee carries immutable `condition_scope.source_methods` evidence.
_Avoid_: "optional output", "method hint", "review-selected condition"

**Skill execution contract**: The post-subprocess verification Interface owned by the shared Skill runner. Exit code zero becomes success only after a declared result envelope, required top-level result keys, unconditional Semantic artifacts, and the matching Method-scoped output guarantees pass. `outputs.files` remains an inventory; reviewed security metadata remains declarative rather than OS enforcement.
_Avoid_: "exit-zero success", "all files are mandatory", "security sandbox"

**Skill compatibility graph**: The generated, auditable producer→consumer relation derived from canonical `interface.outputs` and `interface.inputs.preconditions`, including AnnData facts and Semantic artifact contracts. It is candidate evidence and may contain cycles; generated edges stay unreviewed alternatives until the governed overlay explicitly accepts or rejects them. `summary.skip_when` never enters this graph.
_Avoid_: "execution DAG", "all-domain workflow graph", "proven dependency"

**Compatibility review overlay**: The governed `accepted|rejected` decision layer over a derived edge identity. It cannot invent an edge, erase or replace a derived method condition, and a stale identity or condition fails closed. Only accepted reviewed dependencies are executable.
_Avoid_: "manual graph", "edge override", "approval means required"

**Candidate skill plan**: A user-requested set of resolved skills induced from the Skill compatibility graph. A cycle-free connected selection carries producer-before-consumer order and edge provenance; a Method-scoped output guarantee participates only when the producer has a matching method binding. Disconnected or unbound intents remain explicit unresolved/parallel candidates instead of receiving invented order. It is not executable authority until its complete digest, including method bindings, is explicitly confirmed.
_Avoid_: "automatic pipeline", "confirmed workflow", "LLM-generated dependency"

**Candidate plan executor**: The dedicated one-shot action that verifies the confirmed plan digest, independently revalidates method conditions, consumes its authority, selects only governed dependencies, propagates declared artifacts, runs topological phases with bounded concurrency, and cascades failures only to descendants. Cancellation propagates to the async skill runner and its process group. Ordinary skill calls cannot substitute for this whole-plan Interface.
_Avoid_: "loop over skills", "pipeline alias", "confirmation hook"

**Compute resource reservation**: The Skill representation's static per-process declaration at `resources.compute` (`cpu_cores`, `memory_mib`, `gpu_devices`, `threads`, and `temporary_disk_mib`). It becomes an Execution Resource Request when admitted through a Run. It is part of the Candidate skill plan digest and must be complete before execution. It is neither an OS quota nor a prediction of data-size-dependent peak usage.
_Avoid_: "resource limit", "guaranteed peak", "automatic default"

**Execution Resource Request**: A complete immutable integer multidimensional request for one scientific process unit, with the five canonical `resources.compute` dimensions and one implicit process slot, correlated to its owning Run and optional Run Step. A simple Skill has one request; a Workflow or Candidate plan has a per-Step plan.
_Avoid_: optimistic default, current free capacity, Resource Lease, Run Assignment

**Governed Resource Envelope**: The complete static aggregate capacity contract for one dynamic Run, including process slots and the five compute dimensions plus a per-Step maximum, child-parallelism bound, and ready-Step window. The global scheduler leases the aggregate; the Run cannot expand it.
_Avoid_: per-Step request, optimistic maximum, private capacity pool, current free capacity

**Execution resource budget**: Runtime capacity owned by the one Backend execution environment and narrowed by operator overrides. It bounds scientific process count, CPU, memory, GPU identifiers, threads and temporary disk. Physical GPU identifiers and current availability are runtime-only state and never enter a Run Fingerprint, Candidate plan digest or public audit result.
_Avoid_: "skill metadata", "cluster capacity", "plan resource request"

**Execution Resource Scheduler**: The one process-local global-capacity Module used by every scientific Run Executor. Its Interface atomically admits complete Execution Resource Requests or Governed Resource Envelopes in strict FIFO order, assigns unique GPU identifiers, and releases Resource Leases only after every process covered by the reservation stops. It provides admission accounting and governed environment propagation, not Run ownership, OS quota enforcement or distributed scheduling. The current Implementation is shared by Candidate plans and the canonical Simple Skill Runtime reached by Desktop `POST /v1/runs`, exact prompt-toolkit `/run <canonical-skill> --demo`, exact root `oc run <canonical-skill> --demo`, and Remote compatibility exact-demo `POST /jobs`; remaining Run kinds and legacy callers must still converge.
_Avoid_: "CPU semaphore", "hard quota", "distributed scheduler"

**Run-local allocation**: A child capacity grant suballocated inside one globally leased Governed Resource Envelope to a live dynamic kernel or child Step. It cannot exceed or escape the envelope and is not another global Resource Lease or capacity authority.
_Avoid_: nested global scheduler ticket, Resource Lease, Run Assignment, private unaccounted capacity

**Skill run event**: A privacy-minimal structured execution outcome keyed by skill id, version/hash, environment identity, outcome, typed error kind and explicit evidence kind. `run_id` is present only when an authoritative Run ID exists; a privacy-safe `execution_fingerprint` may deduplicate evidence but is never a Run identity. Raw input paths, stderr, and secrets are not event payloads; hashes and safe structural references are evidence.
_Avoid_: "raw log", "analytics dump", "result.json copy"

**Skill health ledger**: The append-only collection and aggregation of Skill run events, bucketed by skill id + version/hash + environment so dependency/resource failures and framework-validator failures are not counted as script defects.
_Avoid_: "failure count", "leaderboard", "global skill score"

**Evolution proposal**: An append-only, evidence-bound candidate or decision for a Gotcha, validation change, promotion, deprecation, or replacement. It cannot write a formal Skill until a human approves it through Skill evolution governance and representation/execution/retrieval revalidation succeeds; failure rolls back exact governed bytes and any affected projections. Implemented writebacks are earned `smoke-only -> demo-validated`, reproduced-demo `demo-validated -> smoke-only`, exact-replacement `mvp|stable -> deprecated`, and exact-source conditional Gotcha narrative append to canonical `SKILL.md`. Automatic Gotcha evidence and post-approval source-drift review remain non-approvable `draft` states until a maintainer supplies or re-reviews structured wording. Parameter revision remains unimplemented.
_Avoid_: "auto-fix", "self-edit", "automatic promotion"

**Skill evolution governance**: The Backend-owned Module whose small Interface synthesizes evidence-bound proposals, accepts evidence-bound deprecation candidates, exposes privacy-minimal snapshots, records human decisions, derives supported manifest changes, reconciles interrupted approvals, and owns the fixed representation/execution/retrieval validation sequence. The separate OmicsClaw-App repository may present this Interface but must not reproduce its policy or write Backend manifests.
_Avoid_: "frontend promotion logic", "generic patch endpoint", "self-evolution UI"

**Partial skill match**: A capability decision where a built-in skill covers the nearest core analysis but the request also needs generated code for post-processing, visualization, reporting, or an extra analytic step.
_Avoid_: "almost match", "hybrid match"

**Skill-first composition**: The execution rule for a Partial skill match: run the nearest built-in skill first, then let the Autonomous Analysis Path consume the skill artifacts for the uncovered work.
_Avoid_: "rewrite the skill", "replace the skill"

**No skill match**: A capability decision where no built-in skill covers the requested analysis well enough to execute safely as a skill.
_Avoid_: "unknown request", "miss"

### Run & output organization

**Run**: One top-level Skill, Workflow or Autonomous Analysis execution accepted through the Run Executor facade with one immutable Run ID, Run Kind and Run Scope.
_Avoid_: generic job, task, chat stream, Turn, nested tool call, output directory

**Run ID**: A globally unique opaque identity generated by the control plane for exactly one accepted Run and shared unchanged by every executor and projection.
_Avoid_: directory leaf, Remote Job ID, PID, Slurm Job ID, Worker ID, Turn ID, client-generated key

**Remote canonical Job projection**: The authenticated HTTP compatibility view whose `job_id` is exactly `run-<run_id>` for one already canonical `UnassignedScope` Simple Skill Run. The prefix creates no new identity, execution record or path authority; the response also carries the canonical Run ID, while the request's exact 32-hex `Idempotency-Key` is the Run Submission ID.
_Avoid_: top-level Job domain object, independent Job UUID, Execution Reference, output path, retry identity inferred from `run-<run_id>`

**Remote Workspace Binding**: The process-local existing absolute Workspace root resolved once when the Desktop Backend starts and atomically frozen with its sole `ControlRuntime` and `RunRuntime` for that lifespan. Every Remote compatibility Adapter that requires Workspace state consumes this binding instead of resolving mutable environment state per request; the retired Session-resume tombstone intentionally requires no binding. The authenticated Workspace command is bounded and can only confirm the same root or request an explicit Backend restart for another absolute root. Remote Linux compatibility-state mutation remains anchored to held no-follow directory handles, while explicit imported Dataset sources may live elsewhere. The binding therefore anchors compatibility-state storage and Runtime composition but is not filesystem confinement and establishes no Project, Conversation, Run, artifact or resumable-Session identity.
_Avoid_: request-supplied Workspace authority, per-request environment re-resolution, hot Runtime retargeting, Session registry, Job JSON, Run Scope, artifact path authority, filesystem sandbox

**Run Submission ID**: A globally unique opaque retry identity generated by an authenticated control-plane caller before one logical top-level Run submission is accepted.
_Avoid_: Run ID, content hash, tool-call ID, output path, timestamp, reusable command ID

**Run Request Fingerprint**: A versioned canonical digest of caller-declared Run semantics used only to detect conflicting reuse of one Run Submission ID.
_Avoid_: canonical Run identity, executable payload, secret store, mutable executor/resource selection, content-equality deduplication

**Run Submission Binding**: The durable Control Plane State relation from one Run Submission ID to exactly one canonical Run ID and its recorded Run Request Fingerprint.
_Avoid_: TTL cache, persistent executable queue, Run Receipt, retry history inferred from equal inputs

**Run Dispatcher**: The one bounded process-local control-plane scheduler for every accepted top-level Run. It preserves strict FIFO admission order, bounds active Run orchestrators, removes queued cancellations and coordinates the sole Execution Assignment transition. It neither allocates compute resources nor persists or reconstructs executable work.
_Avoid_: durable job queue, Resource Scheduler, Worker broker, per-Surface queue, Run Receipt

**Run Receipt**: The minimal durable Control Plane State record for one Run's accepted identity, immutable Scope and operational lifecycle, containing no executable or scientific payload.
_Avoid_: Run Manifest, persistent queue item, serialized Run Request, logs, artifact inventory, remote Job

**Run Manifest**: The Run-storage record of one Run's scientific provenance, including resolved inputs, parameters, methods, environment, Run Step lineage, artifact inventory and completion evidence.
_Avoid_: Run Receipt, lifecycle registry, UI cache, output directory alone

**Run Integrity Incident**: An append-only, content-free Control record that one closed Assignment, Receipt, Manifest or Process Tree Owner invariant failed; v1 stores only closed codes, opaque Run/Assignment IDs, Receipt revision, evidence version/digest and time, and never repairs or replays the Run.
_Avoid_: exception log, traceback, path, Manifest body, Execution Reference, credential hash, incident acknowledgement, automatic retry

**Run Step**: A nested Skill or method execution performed inside one admitted Workflow or Autonomous Run and recorded as provenance of that parent rather than as another top-level Run.
_Avoid_: independent Run, child job without parent provenance, every tool call

**Execution Assignment**: The single process-bound Control Plane State grant authorizing exactly one executor invocation to begin one accepted Run.
_Avoid_: Run, retry, queue item, Execution Reference, Resource Lease, renewable cross-process lease

**Assignment ID**: The opaque fencing value for one Execution Assignment, carried with executor reports so stale or duplicate ownership evidence cannot rewrite Run lifecycle.
_Avoid_: Run ID, Worker ID, PID, authentication credential, user-facing job link

**Execution Reference**: An executor-specific replaceable identifier such as a subprocess token, PID, remote Job UUID, Slurm job ID or Worker assignment used to observe or control one concrete execution assignment.
_Avoid_: Run ID, scientific identity, stable user-facing link, proof of safe reattachment

**Process Tree Owner**: The canonical local executor's typed, unique, write-once Execution Reference bound atomically with its Assignment before launch. The Linux v1 Adapter uses one user-systemd scope, a parent-death-bound launcher and a bubblewrap PID/cgroup namespace; absence of the unit or `cgroup.events populated=0` is its stop proof. It is lifecycle ownership evidence, not a resource quota, Run identity or permission to replay.
_Avoid_: PID alone, Resource Lease, Execution Lease, mutable scope name, inferred cgroup

**Resource Lease**: A process-local reservation granted by the Execution Resource Scheduler for one complete Execution Resource Request or Governed Resource Envelope. It covers all real process lifetime accounted by that reservation, does not authorize Run execution, fence callbacks or survive Backend restart, and is never released merely because cancellation was requested.
_Avoid_: Execution Assignment, Assignment ID, execution ownership, renewable Execution Lease, bare "lease"

**Run Scope**: The immutable admission-time union `ProjectScope(project_id) | UnassignedScope` recorded by one Run. Project Scope requires an existing active Project with a control-generated opaque ID; Unassigned Scope contains no Project ID. A chat-triggered Run derives it from the Conversation's immutable Project binding, while an explicit Run Request chooses a validated Project or explicit Unassigned. Project selection affects only future Runs.
_Avoid_: mutable `project_id` tag, output directory, current Project pointer, Session/Chat/Conversation ID, `"default"` sentinel, post-run reassignment

**Project output directory**: The on-disk Run-output projection of one Control Plane State Project. Its directory **name** is a readable `<name-slug>__<short-id>` — *not* the raw opaque Project ID. The `short-id` is deterministic (`hash(project_id)[:10]`), and the name is frozen at creation: a later Project rename updates the Project Record and mirrored display metadata but never moves the folder, which would dangle stored output paths. `project_meta.json` records the canonical Project ID for path-local scanning; parsing the directory name never recovers identity.
_Avoid_: "output project", "workspace project" (would re-fork the single Project concept), "results root"

**Unassigned Run Grouping**: The non-Project filesystem grouping for Runs admitted with `UnassignedScope`. Its compatibility path remains the literal `<output root>/default/`, but `default` is a directory name—not a Project ID. It has no Project Record, lifecycle, Conversation binding, `project://` knowledge or steady-state `project_meta.json`, appears separately from Project lists, and is governed by Run retention rather than Project archive/restore.
_Avoid_: "default Project", reserved Project, `project_id="default"`, "unscoped", "misc", "scratch", "uncategorized", "inbox"

**Run directory**: A Run's on-disk storage folder under either a **Project output directory** or the **Unassigned Run Grouping**, using a readable leaf that may include a collision-resistant display token derived from Run ID but is never canonical identity.
_Avoid_: Run ID, Run Receipt, lifecycle authority, path constructed from a bare Run ID, post-run scope move

**project_meta.json**: The output subsystem's durable path-local projection from one Project ID to its frozen Project output directory, optionally mirroring display metadata from the Project Record. It exists only for a real Project output directory; a legacy copy under `default/` is not Project evidence.
_Avoid_: Project Record, Project registry, lifecycle authority, Unassigned metadata, "project config", Run manifest

**Run index**: A rebuildable `index.jsonl` projection that maps canonical Run ID to Run-store location and selected Receipt/Manifest display fields for fast listing.
_Avoid_: Run Receipt store, Run Manifest, lifecycle authority, canonical ID generator, independent registry

### Prompt Prefix & Caching

The vocabulary for maximizing the LLM provider's automatic prefix cache (DeepSeek's `prompt_cache_hit_tokens`, OpenAI's `prompt_tokens_details.cached_tokens`). The mechanism is byte-exact: the provider caches the longest request prefix that is byte-identical to a recent request, so cache hit rate is governed entirely by **how stable the front of the request is across turns of one Conversation**. OmicsClaw targets automatic prefix caching, **not** Anthropic `cache_control` breakpoints (decided 2026-05-30). Inspired by `DeepSeek-Reasonix`'s 99.82%-hit design, adapted to OmicsClaw's multi-surface, layered assembler.

**Prompt prefix**: The leading span of the request — serialized `tools` + the `system` message — that the provider keys its cache on. Everything the provider sees before the first turn-varying byte.
_Avoid_: "system prompt" (only one part of the prefix; tools come first), "context".

**Stable prefix invariant**: The rule that, within one Conversation, the Prompt prefix must be **byte-identical across consecutive requests**. The provider's prefix cache fails from the first differing byte onward, so a single mid-prefix change discards the cache for that change point *and the entire conversation history that follows it*. The whole caching design is the enforcement of this one invariant.
_Avoid_: "prefix should be similar", "mostly stable".

**Volatile context**: The per-turn content that changes with the current query — query-gated rule layers, matched-skill / capability / knowledge-guidance / plan context, **query-ranked `scoped_memory_context`**, **volatile Conversation work-state `project_state_context`** (current dataset / recent analyses / insights — Decision-2), **query/skill/domain-matched `knowhow_constraints`**, and the Analysis Router's route / autonomous-understanding / assisted-parameterization context. To preserve the Stable prefix invariant it is rendered into the **latest user message** (`placement="message"` for layers; prepended as `user_turn_context` for route context), landing at the append-only tail of history, never in the `system` prefix. The classifier is *volatility*, not semantic role: a layer goes here iff its content varies within a Conversation, regardless of whether it reads as an "instruction".
_Avoid_: "dynamic prompt", "context injection" (too broad — covers stable layers too).

**Conversation prefix snapshot**: The Conversation-stable `system` tier (base persona, surface voice, output style, extension packs, mcp instructions, workspace context, and Conversation-scoped `memory_context` — durable identity only since Decision-2: Owner preferences + Project context, which change only on a Memory write to those types, not per query; the volatile work-state slice — current dataset / recent analyses / insights — moved to the message tail as `project_state_context`) plus the Frozen tool list. v1 keeps it byte-stable by deterministic re-assembly of Conversation-constant inputs each turn rather than a literal cached freeze object. It changes only at a deliberate, logged **cache re-warm**: a model switch, a durable-Memory write, a context collapse, or a Conversation start/resume hook injecting content.
_Avoid_: "Session prefix snapshot" (legacy term), "cached system prompt" (the snapshot includes more than one prompt string), "cache warmup", "prompt cache" (the cache is the provider's; this is the stable input tier)

**Frozen tool list**: The Conversation's tool payload, filtered once by `surface` (a Conversation constant) and then frozen in static registration order. Per-turn query-keyword gating (the former tool-list-compression) is dropped: once tools live in a cached prefix, hit-token pricing (~10% of miss) makes compression a net loss. Unlike Reasonix, OmicsClaw needs **no** locale-independent re-sort — its tools are statically registered, so order is already deterministic; the only requirement is to stop varying the subset per request.
_Avoid_: "lazy-load tools", "tool compression" (the retired per-turn behavior).

**Cache-hit diagnostics**: The per-turn observability that reads `hit`/`miss` tokens from provider usage, computes a hit ratio, and — when a miss is unexpected — infers the reason by hashing prefix segments (tools / stable-system) and comparing against the prior turn (`tool-list-changed`, `system-changed`, `cold-start`, …). The Reasonix feature that turns the Stable prefix invariant from a hope into a measured property.
_Avoid_: "cache metrics" (too vague), "token accounting" (that is billing, a superset).

> **Forward-declared — ADR 0039 / 0040 (Proposed, 2026-07-03).** Two refactors of
> this subsystem are decided but not yet in code, so the terms below are named
> here only so cross-subsystem readers recognise them; canonical definitions live
> in the ADRs until implemented. **ADR 0039** collapses the compaction *budget*
> from chars onto a single **token budget** (one unit for budget → status →
> compaction; the 256000-char cost cap becomes an ~85k-token *latency backstop*;
> the LLM condensed collapse summary becomes the default output). **ADR 0040**
> gives the raw **transcript** restart durability via a write-through **derived-state
> (P-state) mirror** into a dedicated `transcripts.db`, rehydrated **once on
> cold-start miss** (never per-turn). Both preserve every prefix-cache invariant
> above unchanged; the only vocabulary shift is that the compaction budget is
> counted in tokens rather than chars.

## Relationships

- One backend instance serves exactly one **Owner**.
- The **Owner** has one or more configured **Owner Identities**; every accepted identity authorizes the same Owner rather than creating another User.
- **Control Plane State** is the only authority that establishes whether a **Project**, **Conversation**, accepted **Turn** or accepted **Run** exists; a Surface, Memory row, Transcript key, Remote Job or output directory cannot create those identities implicitly.
- Exactly one Backend control-plane process owns one **Control Database** and holds its lifetime operating-system lock; a second process fails closed rather than becoming another writer.
- Only the Backend control-state Repository accesses the **Control Database**. Surfaces, the Desktop App, content stores and compute Workers use typed Interfaces and never open it directly.
- The **Control Database** contains control identity and lifecycle records, including minimal Turn and Run Receipts, content-free Run Integrity Incidents, Outbound Delivery/Item/Attempt control metadata, plus schema migration and Legacy Identity Map bookkeeping; Transcript bodies, Memory, Event, Attachment Record, Attachment Blob, ToolResult, reply/media bodies, credential, executable Run payload, Manifest and artifact content remain outside it.
- The **Canonical Transcript Store** has its own immutable opaque Store identity, and the Control Database binds exactly one such identity. A matching path with another identity, or a missing Store after conversational control state exists, fails startup closed instead of creating an empty replacement.
- Live terminalization stages one **Terminal Transcript Candidate**, atomically commits the terminal **Turn Receipt** plus its **Turn Terminal Transcript Reference**, promotes the candidate into the active view, then publishes the terminal Event. A durable terminal Receipt without the exact committed referenced entry is an integrity failure.
- Every real **Project** has exactly one **Project Record**. Bench threads, `project://` Memory, Project output directories and Runs reference its opaque Project ID rather than acting as alternative Project registries.
- Every newly created **Project** is `active`; its only other v1 lifecycle state is `archived`. Restore is the transition back to `active`, not a third state.
- A **Project Archive** retains the Project ID, immutable Conversation bindings, Active Conversation Bindings, Transcripts, Memory, Runs and files. It permits explicit lookup, export, restore and administrative display-metadata correction but rejects new Conversation binding, novel Turns, new Runs and Project-scoped scientific mutation.
- Project archive/restore and Project-aware Turn/Run admission share one Project-scoped lifecycle gate. Archive queries Run Receipts and returns `project_busy` rather than canceling when any accepted Turn or Project-associated Run is non-terminal.
- A Conversation's optional immutable Project binding must reference an existing **Project Record**; neither ingress nor a projection may bind it to an inferred or orphaned Project ID.
- A `project://<project_id>` subtree owns durable Project knowledge and lineage, while **project_meta.json** owns only the output subsystem's Project-to-directory projection; neither determines the Project's current name or lifecycle.
- **Owner Identity** is consumed by ingress admission and may be copied into **Source Attribution**; it never keys Memory, Transcript, attachments, Outbound Delivery, Project, Workspace, or Run state.
- **Source Attribution** describes where accepted input came from but does not own the resulting state and contains no provider credentials.
- In the implemented Schemes 1–4, prompt-toolkit/single-shot CLI conversational input, Desktop text/multipart-image and Owner-only Telegram text/single-photo input emit **Raw Inbound** to exactly one **Ingress Normalizer**; only its **Inbound Envelope** paired with a fresh **Dispatch Context** enters their production `dispatch()` path. Desktop `POST /v1/runs`, exact prompt-toolkit `/run <canonical-skill> --demo`, exact root `oc run <canonical-skill> --demo`, and Remote compatibility exact-demo `POST /jobs` instead enter the canonical Run boundary directly as typed non-chat **Run Requests**. Textual TUI and non-Telegram Channel Adapters remain explicit cutover work.
- The **Ingress Normalizer** applies Owner admission before durable attachment staging or any Conversation, Project, Transcript, Memory, Agent, tool, or reply side effect.
- After Owner admission, the **Ingress Normalizer** forms the **Ingress Idempotency Key** and request fingerprint without side effects and checks the durable **Ingress Idempotency Binding** before attachment staging, Conversation resolution or Active Conversation Binding mutation.
- Ordered **Source Attachment Descriptors** contribute to that versioned request fingerprint before the Backend downloads, copies, or durably writes attachment bytes. A duplicate key with the same fingerprint returns the original Turn and Attachment Records without restaging; the same key with different descriptors is an idempotency conflict.
- An **Ingress Idempotency Binding** maps exactly one key to one canonical Turn ID. The same key and fingerprint returns that Turn in any lifecycle state; the same key with another fingerprint is rejected; equal content under different keys remains distinct intent.
- Project lifecycle is checked only after that duplicate lookup. A novel input targeting an archived Project is rejected as `project_archived` before Conversation creation/binding, active-pointer mutation, FIFO reservation or Turn acceptance; a duplicate still returns its original Turn.
- The **Ingress Normalizer** resolves the opaque Conversation, immutable optional Project binding, normalized content, ordered accepted Attachment References, validated File References, Reply Target, Source Attribution, and runtime turn options.
- When an explicit Conversation ID is supplied, the **Conversation Resolver** requires its stored **Conversation Address** to match the current Surface and Reply Target and rejects a mismatch. Only when no explicit ID is supplied does it follow the durable **Active Conversation Binding**, creating a new opaque Conversation and binding when none exists.
- Each normalized `(Surface, Reply Target)` has at most one **Active Conversation Binding** but may retain multiple historical Conversations. Owner Identity and Project never enter the binding key.
- Selecting a same-address Conversation, invoking `/new`, or switching from one bound Project to another atomically updates the **Active Conversation Binding**; the previously active Conversation and Transcript remain durable.
- A Conversation is never moved or implicitly resumed at another Reply Target. Related work there creates a new Conversation, normally under the same Project; Transcripts remain separate.
- Selecting a Project for an unbound Conversation binds it once in place; selecting its existing Project does not create another Conversation.
- Inbound Envelope receives the resolved opaque Conversation ID. A later active-binding change never retargets an in-flight dispatch.
- For a novel idempotency key, after Conversation resolution and successful FIFO capacity reservation, the control plane generates an opaque **Turn ID** and atomically commits its queued **Turn Receipt** with the **Ingress Idempotency Binding** before enqueueing the process-local Turn Execution.
- Before that control-state commit, the **Attachment Store** stages and verifies the entire declared attachment batch, publishes immutable Blobs plus provisional Records under the proposed Turn ID, and proves them readable. The batch is all-or-nothing: partial attachment acceptance and silent attachment drop are forbidden.
- Attachment publication and control acceptance use publish-before-control reconciliation rather than a distributed transaction. A provisional publication without a committed Turn Receipt becomes garbage-collectable after a grace period; a committed Turn whose Records are not yet finalized causes them to be promoted during reconciliation; missing or corrupt content for an accepted Record is an integrity incident, never a reason to rerun the Turn.
- A rejected or backpressured input is not a **Turn** and has no durable **Turn Receipt** or Ingress Idempotency Binding; a committed binding is retained with its receipt rather than expiring by TTL.
- Each **Conversation** has one bounded process-local **Turn Sequencer**: at most one Turn is active, and waiting Turns begin in FIFO admission order.
- The **Turn Sequencer** acquires before the first Transcript read or mutation and releases only after terminal Transcript state is committed, a Channel Turn's canonical Outbound Delivery is atomically accepted with the terminal Turn Receipt, and the terminal Event is published to the live Turn event boundary; provider delivery completion is not awaited and Response Sink loss does not cancel, fail or retain the Turn.
- A waiting Turn causes no Transcript, Agent, tool, prompt-state, or Run side effect; its fresh **Dispatch Context** is created only when that Turn becomes active.
- The **Turn Sequencer** is not restart-resilient or cross-process. A full FIFO produces explicit backpressure, never silent drop, merge, automatic cancellation, or unbounded buffering.
- Turns from different **Conversations** may execute concurrently; Run execution concurrency remains an independent execution-plane concern.
- **Turn Receipt** status follows `queued -> running -> succeeded | failed | canceled`; explicit cancellation may also move `queued -> canceled`, while control-plane startup reconciles every prior `queued` or `running` receipt to `interrupted` without replay.
- Terminal **Turn Receipt** status is immutable; explicit retry uses a new Source Request ID and creates a new Turn ID with optional `retry_of_turn_id` provenance.
- Every accepted Channel Turn creates at most one canonical terminal **Outbound Delivery**, enforced by `(turn_id, purpose=terminal)`. Desktop and CLI observe Transcript, Receipt and Event state and do not create a Delivery merely to replay output.
- Terminal reply/notice content and durable outbound artifact references are verified before one `control.db` transaction terminalizes the Turn and inserts its queued Delivery Items. Delivery outcome can never change, reopen or retry the terminal Turn.
- One **Outbound Delivery** freezes the originating Conversation, Turn, Surface and immutable Reply Target and contains one or more ordered immutable **Delivery Items**. It never follows a later Active Conversation Binding.
- The **Outbound Delivery Outbox** is bounded. Each accepted non-terminal Channel Turn consumes one future-delivery capacity unit; novel ingress that cannot reserve capacity returns `delivery_backpressure` before Turn acceptance, while duplicate lookup still returns the original Turn first.
- The in-process **Delivery Pump** survives restart through Control Plane State and invokes a **Delivery Adapter** once per Attempt. It never invokes `dispatch()`, the Agent, a tool, a Run Executor or the KG handoff executor.
- A Delivery Item follows `queued -> sending -> delivered | retry_wait | failed | unknown`; `retry_wait` may return to `queued`. A crash in `sending` becomes `unknown` unless provider idempotency or reconciliation proves acceptance or safe non-acceptance.
- Safe automatic retry reuses the same Delivery and Item identities only when non-acceptance is known or provider idempotency makes replay safe. An explicit Owner resend after an unknown or delivered outcome creates a new Delivery ID with `resend_of_delivery_id` and never creates a Turn.
- A repeated inbound provider message creates neither a second Turn nor a second Delivery. It returns the original Turn while that Turn's original Delivery independently remains queued, delivered, failed or unknown.
- Typing, token streaming, progress placeholders and live approval capabilities remain ephemeral **Response Sink** behavior. Terminal Channel delivery never depends on editing a placeholder, and progress is not restored from the Outbox.
- The current production Channel slice is Owner-only Telegram text plus one ordinary photo with an optional caption: stable `chat_id:message_id` ingress, duplicate-first lazy byte retrieval, immutable per-Turn Attachment Records, deterministic Transcript-backed Items and one-call Telegram Attempts are implemented. Pending provider updates are preserved across restart. Media groups, documents, audio/video, outbound media Items, explicit resend/repair and every non-Telegram Adapter remain disabled/unimplemented rather than falling back to legacy staging or direct terminal send.
- **Turn Execution** holds the live FIFO slot, cancellation, approval, policy, usage, bounded Event buffer and Response Sink observer attachments in process memory; none are persisted or recovered from Turn Receipt.
- The **Turn Event Hub** owns that bounded Event buffer. Capacity, observer backpressure, a slow renderer, or disconnect may create an observation gap or detach a sink, but cannot change an accepted Turn Receipt or release/revoke its execution authority.
- Core cancellation targets **Turn ID**, not Conversation ID, legacy Session ID, Reply Target, or Run ID.
- Canceling a Turn requests cancellation of its non-terminal child Runs, but each Run transitions through its own **Run ID** and remains `cancel_requested` until its executor confirms termination.
- Typed Events and chat-triggered Runs carry Turn correlation; explicit non-chat Run Requests have no fabricated parent Turn.
- A Desktop submission retry reuses its Source Request ID and receives the existing Turn ID; SSE reconnection names that Turn ID plus its last Event sequence, always begins with an unnumbered verified snapshot, and never submits another Turn. Retained frames use one-based IDs and stable typed names; an evicted cursor receives a structured gap before the same atomically opened observer follows newly published frames.
- Losing a **Response Sink** only detaches that observer. The Turn continues until a scientific/conversational terminal outcome or explicit `cancel(Turn ID)`; transport acknowledgement, Outbound Delivery and Delivery retry are separate concerns.
- Transcript mutations retain storage-side Turn attribution without changing provider-visible message payloads or the Stable prefix invariant.
- **Inbound Envelope** contains only versioned JSON-compatible facts and requested options; it never contains effective authority or live process objects.
- **Dispatch Context** controls how one accepted turn executes and carries its live **Response Sink**, but cannot change its Conversation, Project, content, Source Attribution, or Reply Target.
- Every **Conversation** stores one immutable **Conversation Address**; its opaque Conversation ID contains no encoded address semantics.
- **Reply Target** is stable logical data in Inbound Envelope, **Response Sink** is an ephemeral process capability for live observation/progress, and canonical terminal Channel output is an **Outbound Delivery** processed independently after terminal intent is durable.
- Replay revalidates the **Inbound Envelope** and creates a new **Dispatch Context** from current policy; a prior approval or authorization capability is never replayed.
- A **Surface** may verify transport authenticity, decode SDK events, and render Events, but it never constructs canonical domain keys or calls the Agent Loop directly.
- A **Run Request** may bypass conversational ingress only for an explicitly non-chat deterministic action; a chat-triggered Run always begins as Raw Inbound and follows the normal Agent and policy path.
- A Channel message from an identity that is not a configured **Owner Identity** is ignored before Conversation or Project resolution. It creates no Conversation, Transcript entry, attachment state, Agent execution, or reply; missing Owner Identity configuration fails closed rather than authorizing everyone.
- A Channel group or thread may be a **Reply Target**, but non-Owner senders are not participants in the Conversation.
- The **Owner** owns zero or more **Conversations** and **Projects**.
- A **Conversation** owns one Transcript and zero or more accepted Attachment Records through its Turns; each Attachment Record belongs to exactly one Turn and one Conversation.
- A **Conversation** belongs to exactly one immutable **Conversation Address** for its entire lifetime.
- A **Conversation ID** is independent of Owner Identity, Surface origin, Reply Target, platform thread id, and Project binding; those facts are stored explicitly and never inferred by parsing the id.
- A **Conversation** is either unbound or bound to exactly one **Project**. The first binding is immutable; using another Project creates another Conversation.
- A **Project** may contain multiple Conversations from multiple Surfaces and is the unit of cross-Surface research continuity.
- Project creation, archive and restore begin in **Control Plane State**; Memory and filesystem projections follow or are reconciled to the authoritative **Project Record**.
- Archiving never clears or retargets an **Active Conversation Binding**. Novel work fails closed until restoration, after which the same Project-bound Conversation continues without rebinding.
- A **Legacy Identity Map** may make an explicit import idempotent, but after cutover a missing control record is a migration or recovery error; ordinary runtime never falls back to a legacy Session, Memory node, App row, output directory or transport key to recreate identity.
- The **Owner** may reuse Memory across Conversations, but Transcripts are never merged automatically across Conversations.
- Owner-wide preferences and personal Memory use the singleton Owner scope; no synthetic Owner ID is required in v1.
- Transcript and Conversation prompt state are keyed by opaque **Conversation ID**; Attachment Records are keyed by opaque Attachment ID and explicitly carry their owning Turn and Conversation; research continuity is keyed by **Project**; filesystem-local state is keyed by Workspace; scientific execution state is keyed by opaque **Run ID** and carries explicit Run Scope.
- Inbound Envelope and Transcript retain only ordered **Attachment References**. Prompt rendering may derive bounded inline text or image input ephemerally, and tools receive explicit references; neither uses an absolute path, provider locator, Base64 payload, or mutable "latest received files" registry as the durable contract.
- Two different inbound submissions with identical bytes create distinct Attachment Records but may share one content-addressed Attachment Blob. Blob equality never merges Turns or Attachment identities.
- An accepted Attachment Record is immutable and has no ordinary per-attachment delete operation in v1. `/new`, cancellation, disconnect, compaction, Conversation switching, and Project Archive retain it; Blob garbage collection requires zero Attachment Records, Run inputs, or other durable references.
- A **Run Manifest** records the immutable Attachment ID and verified digest for every consumed attachment input so later Blob storage optimization cannot rewrite scientific provenance.
- A **File Reference** remains distinct from an Attachment Reference: it names an authorized pre-existing Workspace file, may observe later Workspace mutation according to its own contract, and gains durable copied-byte semantics only through an explicit import or snapshot operation.
- A **Reply Target** may carry multiple consecutive Conversations; changing Conversation or Project does not require changing the transport destination.
- Every Channel **Outbound Delivery** at one Reply Target receives a unique monotonic **Delivery Target Sequence**. The Delivery Pump starts at most one provider call at that target and never advances to a later sequence until the earlier Delivery has a terminal summary; different Reply Targets remain concurrent.
- When a Delivery Item becomes failed or acceptance-unknown, every higher unattempted Item in that Delivery becomes a **Suppressed Delivery Item**. The target-local barrier may then advance, while provider-visible order remains explicitly ambiguous after `unknown`.
- A normalized control-plane request context holds the **MemoryClient** needed by the runtime; a **Surface** does not choose its Namespace or construct it from transport identity.
- Target scientific Memory ownership is explicit: preferences/persona use Owner scope; Project hypotheses, insights, lineage and Dataset References use Project scope; authorized local-file observations use Workspace scope; accepted upload bytes retain Attachment identity. Owner Identity and Source Namespace choose none of these.
- A novel Project-scoped scientific Memory mutation requires an active Project. A pre-existing **Project Projection Intent** may apply exactly its frozen, digest-verified projection after archive; it cannot authorize derived or broadened work.
- A **Workspace dataset observation** deduplicates only by Workspace, normalized relative path and verified content version. Filename equality or sender/launch Namespace never proves one dataset. Projects reuse it through **Project Dataset References**.
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
- The **Prompt prefix** is the **Frozen tool list** (serialized first) followed by the stable `system` layers of the **Conversation prefix snapshot**; the **Stable prefix invariant** keeps it byte-identical across a Conversation's turns.
- The **Frozen tool list** is filtered once by the **Surface** (`surface` is a Conversation constant), so per-Surface tool sets never break the **Stable prefix invariant** — different Surfaces are different Conversations, not different turns.
- **Volatile context** — per-turn `memory` recall, volatile Conversation work-state (`project_state_context`: dataset/analysis/insight — Decision-2), `capability`/`skill` hints, query-gated rule layers, and the **Analysis Router**'s route context — is rendered into the latest user message (`placement="message"`), landing on the append-only tail; it never enters the `system` prefix, so it cannot break the **Stable prefix invariant**.
- A **Conversation prefix snapshot** is invalidated only by a deliberate, logged **cache re-warm**: a model switch, a durable-Memory write to preferences/Project (which refreshes the snapshotted Conversation-scoped `memory_context`; volatile dataset/analysis/insight writes ride the message tail as `project_state_context` and do not re-warm — Decision-2), or a context collapse. Between re-warms, history is append-only.
- Context collapse (the existing `CONTEXT_COLLAPSE` / `AUTO_COMPACT`) is the sole overflow handler: it folds old messages into a frozen `system` summary (one **cache re-warm**) so history stays append-only between collapses — replacing the former per-turn `trim_history_to_budget` sliding window, which shifted the history prefix every turn and discarded all history caching for long Conversations.
- **Cache-hit diagnostics** hash the **Frozen tool list** and the stable `system` prefix each turn: an unexpected provider miss with unchanged hashes is reported `history-shifted`, a changed tool hash `tool-list-changed`, a changed system hash `system-changed` — turning the **Stable prefix invariant** into a CI-assertable regression guard.
- After caller authentication, the control plane checks the **Run Submission Binding** before current Project lifecycle and capacity gates. The same Run Submission ID plus Fingerprint returns the existing Run in any state; conflicting reuse fails with `run_idempotency_conflict`; a different ID is distinct Owner intent even when content is equal.
- The root exact-demo Adapter classifies raw tokens before argparse and any legacy Project/output resolution. It accepts only omitted Scope, fixed-order `--demo --project <32-lower-hex-id>`, or `--demo --no-project`; every other demo-shaped or command-shifted form fails closed. Only omission reads the bounded, lock-free, side-effect-free current-Project navigation snapshot. Explicit Project freezes `ProjectScope` and novel admission returns `project_not_found` or `project_archived` without downgrade; explicit Unassigned freezes `UnassignedScope` without reading navigation. Ctrl-C explicitly requests cancel, observes terminal closure, and then closes Run before Control; unconfirmed owner stop keeps Control ownership and outranks an ordinary interrupt projection.
- The Remote exact-demo Adapter admits only a canonical Skill, `inputs.demo=true`, empty parameters, explicit Unassigned Scope and one complete simple resource request. Novel submission returns `202`; a matching Binding returns `200`; `run-<run_id>` is only the **Remote canonical Job projection** and never a second identity or executable `job.json`.
- Every Remote compatibility Adapter that requires Workspace state uses the one **Remote Workspace Binding** frozen at Backend startup; the retired Session-resume tombstone performs no Workspace work. Bearer-policy-gated Workspace observation reports the binding; same-root update is an idempotent no-op, while a different root fails closed as restart-required and cannot mutate environment, trusted directories, output roots or live Runtimes. The binding anchors storage and composition rather than confining filesystem access.
- Remote Job detail and SQL-keyset list reads are bounded Receipt projections. Job SSE is snapshot-first and waits only for a greater Receipt revision; opening or resuming SSE observation, or disconnecting an observer, cannot enqueue, acquire a Resource Lease, create an Assignment, recover, replay or cancel work.
- Remote canonical cancel resolves the projection to **Run ID** and delegates only to `RunRuntime.cancel()`, preserving `cancel_requested` until stop proof. Canonical retry fails closed instead of cloning payload; a future explicit scientific retry must create a fresh Submission ID and linked Run under the normal Run contract.
- Remote canonical artifact list/download verify Receipt, Assignment, completed Manifest and the complete immutable inventory through typed Runtime/Run Store Interfaces. Download uses the same verified file descriptor and does not turn Job ID or a path into artifact authority. Historical terminal Job records remain read-only, while historical active scientific Jobs close at startup as `interrupted/legacy_execution_unrecoverable` without replay or guessed import.
- Legacy `POST /sessions/{session_id}/resume` is a fixed retired compatibility response: `resumed=false`, `reason=legacy_session_resume_retired`, and no active IDs. It reads no Workspace or Job store and invokes no Runtime; a Session can neither discover nor resume scientific execution.
- For a novel submission, the control plane first validates a complete static **Execution Resource Request**, per-Step plan or **Governed Resource Envelope** against the configured hard budget, then reserves bounded process-local Run-buffer capacity and atomically validates Project lifecycle/resolves Scope, generates the opaque **Run ID**, and commits both the `queued` **Run Receipt** and **Run Submission Binding**. Only after commit does it enqueue the executable payload in memory; rejection before commit is not a Run, while a post-commit ownership gap reconciles to `failed` or `interrupted` without replay.
- Every accepted top-level Run enters the one bounded process-local **Run Dispatcher**. It preserves strict FIFO admission order, bounds active Run orchestrators and owns the transition opportunity into one Execution Assignment; queue position and wait reason are live projections, not durable Run statuses.
- Before Assignment, the Dispatcher obtains a provisional **Resource Lease** for the first fixed-plan execution unit or the dynamic Run's complete **Governed Resource Envelope** from the shared **Execution Resource Scheduler**. Only then may the Repository atomically commit `queued + no assignment -> running + Assignment ID`; the canonical local tracer includes its pre-generated **Process Tree Owner** in that same transaction. If cancellation or another invalidating transition wins, the Lease is released and no executor starts.
- The Execution Resource Scheduler is the only global capacity authority for all scientific processes across Skill, Workflow, Candidate-plan and Autonomous paths. Its strict FIFO atomically accounts process count, CPU, memory, GPU, threads and temporary disk. A bounded per-Run ready-Step window may limit pending tickets but is not another global process-concurrency authority.
- A dynamic Run holding a Governed Resource Envelope MUST NOT submit a nested global ticket. Its kernel and child Steps use bounded **Run-local allocations** inside the aggregate; the Resource Lease is released only after every covered process stops. Fixed plans continue to acquire one global Lease per scientific process.
- Missing resource semantics reject novel admission with `resource_contract_missing`; a request that can never fit the configured budget returns `resource_unsupported`; temporary contention waits without becoming a scientific failure. Static resource semantics or their versioned digest enter the Run Request Fingerprint and Run Manifest, while availability, wait, physical GPU IDs and scheduling order do not.
- Every **Run** receives one immutable **Run Scope** at admission. A Project-scoped Run is written into that Project's **Project output directory** and references the same opaque Project ID used by its Project Record and `project://<project_id>` knowledge; an Unassigned Run lands in the **Unassigned Run Grouping** without fabricating a Project Record or `project_id="default"`.
- A chat-triggered Run derives Run Scope from its Conversation's immutable Project binding and records `parent_turn_id`; an explicit non-chat **Run Request** chooses a validated active Project or explicit Unassigned and has no fabricated Turn. One Turn may produce zero, one or several Runs.
- Run Scope never changes after admission. Moving, retagging, copying or symlinking an existing Run into another Project is not supported in v1; future cross-Project reuse requires a separate Project-to-Run reference that does not mutate original provenance.
- A top-level Skill, Workflow or Autonomous execution is one **Run**. Nested Workflow/Autonomous Skill calls are **Run Steps** in the parent Manifest; independently admitted Skill executions are separate Runs.
- **Run Receipt** owns acceptance, immutable Scope and operational lifecycle; **Run Manifest** owns scientific inputs, parameters, methods, environment, step lineage, completion evidence and artifact meaning. A mismatch appends a content-free **Run Integrity Incident** without rewriting either authority; it is never permission for silent inference.
- Each Run may acquire at most one **Execution Assignment** through an atomic `queued + no assignment -> running + Assignment ID` transition. The canonical local tracer also persists its write-once Process Tree Owner in that transaction. The executor starts scientific side effects only after the transition is committed and acknowledged; a second claimant must not start.
- Executor start acknowledgements, Execution Reference updates, terminal reports and cancellation confirmations carry both Run ID and **Assignment ID**. Compatible duplicates are idempotent, mismatched assignments are rejected, terminal state never reopens, and conflicting terminal evidence is atomically persisted as a content-free incident rather than resolved by last-write-wins. Incident observation is read-only and cannot inspect a Manifest, acquire capacity, assign or replay a Run.
- Queued cancellation races Assignment creation through the same state gate. A queued cancel that commits first prevents start; an Assignment that commits first moves cancellation through `cancel_requested` until that executor confirms stop.
- Resource Leases are acquired immediately before process startup and released only after every covered process is confirmed stopped. Waiting cancellation removes its resource request; dependency, approval and user-input waits hold no Lease only when no process remains alive. A paused live governed kernel keeps its aggregate Lease; `cancel_requested` alone never releases resources still in use.
- Run Receipt starts `queued`; it may move to `running`, `canceled`, `failed` or `interrupted`. `running` may move to `succeeded`, `failed`, `cancel_requested` or `interrupted`, and `cancel_requested` may move to `canceled`, `succeeded`, `failed` or `interrupted`. Terminal statuses never reopen.
- `succeeded` requires verified durable Manifest/completion evidence and required artifacts; exit code zero, directory presence, UI status or a terminal Event is insufficient by itself.
- v1 never automatically replays or reassigns non-terminal Runs after restart. An unassigned `queued` Receipt reconciles to `interrupted`. An assigned canonical local Run first proves its persisted Process Tree Owner empty, then prefers verified immutable completion evidence and otherwise commits fenced `interrupted` stop evidence. Missing/unobservable ownership, invalid completion evidence or an unapplied terminal Control transaction preserves the nonterminal Receipt and quarantines novel scientific admission. Explicit retry is a fresh idempotent submission with a new Run Submission ID and Run ID, records `retry_of_run_id`, preserves Run Scope and leaves the original history immutable.
- v1 has no renewable Execution Lease, heartbeat stealing or timeout-based second Assignment. A **Resource Lease** is only process-local resource accounting and cannot prove execution ownership. Future safe external-Worker reattachment must retain the same Assignment and requires a separate protocol decision.
- v1 uses strict FIFO independently in the Run Dispatcher and Execution Resource Scheduler, with no Surface/Project/Run-kind priority, bypass, aging, preemption or deadlines. Head-of-line blocking is an explicit deterministic single-Owner trade-off until measurements justify another policy.
- Run submission and observation are separate: status, logs and SSE may observe an existing Run, but `GET` never admits, assigns, resumes or retries scientific work.
- Core Run cancellation targets **Run ID** and uses `cancel_requested` until executor termination is confirmed. PID, Remote Job UUID, Slurm job ID, SSH command ID and Worker assignment are replaceable **Execution References**, not Run identity.
- All execution paths resolve a **Run directory** through one shared Run-store interface. The readable directory leaf is not Run ID; the **Run index**, Memory `analysis://` records and Desktop `run_meta` rows are rebuildable projections keyed by the canonical Run ID over Receipt and Manifest facts.

## Example dialogue

> **Dev:** "If the Owner updates their `qc_threshold` preference from Telegram, does the old value disappear?"
> **Architect:** "No — `preference://*` is in `VERSIONED_PREFIXES`, so `MemoryClient.remember()` routes to a **Versioned upsert**. The old `Memory` row stays with `deprecated=True`; **ReviewLog.list_version_chain** can find it; the Owner can roll back via the Desktop review UI."
>
> **Dev:** "The Owner processes `pbmc.h5ad` in two different workspaces — what happens?"
> **Architect:** "Two different **Namespaces**, two independent `paths` rows. The same file produces two `dataset://pbmc.h5ad` entries. **Read fallback** doesn't connect them because `dataset://*` is per-Namespace, not shared."
>
> **Dev:** "I bind the keyword 'TIL' to a shared OmicsClaw concept node — does it appear from another Namespace?"
> **Architect:** "Only if you call `add_glossary_shared('TIL', node)`. Plain `add_glossary` writes the binding under the current **Namespace**. **Read fallback** surfaces shared bindings to every Namespace in this Owner's backend, while the ordinary binding remains local to its Namespace."
>
> **Dev:** "The user asks for a built-in clustering plus a custom publication figure."
> **Architect:** "That is a **Partial skill match** using **Skill-first composition**: the clustering runs through the shared skill runner, then the figure is produced through the **Autonomous Analysis Path**."
>
> **Dev:** "The user typed a file path this turn, so the file tools appeared in the request. Next turn they didn't — does that hurt?"
> **Architect:** "It used to: per-turn query-keyword gating changed the **Frozen tool list**, breaking the **Stable prefix invariant** at the tools segment and discarding the cache for the whole request. We dropped that gating — the tool list is now frozen per Conversation, so the path-mentioning turn and the next turn send a byte-identical **Prompt prefix**. The file-path *rule layer* still appears adaptively, but as **Volatile context** in the user message, where it costs nothing in cache."
>
> **Dev:** "A 60-message Conversation shows hit_ratio 0.95 on turn 30, then 0.0 on turn 31. What broke?"
> **Architect:** "Read the **Cache-hit diagnostics** miss reason. `system-changed` means a Memory write re-warmed the **Conversation prefix snapshot** — expected, one turn. `history-shifted` with unchanged hashes would mean a context collapse folded old messages (also expected). `tool-list-changed` would be a real regression — something re-introduced per-turn tool variation."
>
> **Dev:** "The Owner sends a second message while the first one is still running a Skill. Do both Agent Loops read the same Transcript?"
> **Architect:** "No. The Conversation's **Turn Sequencer** keeps the second Turn in its bounded FIFO until the first terminal Transcript state is committed and its terminal Event is published. A disconnected Response Sink does not cancel the first Turn; a different Conversation may still run concurrently."
>
> **Dev:** "The backend restarts while a Turn is waiting or running. Should its Turn Receipt cause the work to resume?"
> **Architect:** "No. Startup marks the old receipt `interrupted`. The **Turn Receipt** explains the outcome but contains no executable payload; an explicit retry creates a new **Turn ID** and fresh Dispatch Context."
>
> **Dev:** "Telegram redelivers one provider message after the backend restarts. Should the scientific tool run again?"
> **Architect:** "No. Its **Ingress Idempotency Key** resolves through the durable **Ingress Idempotency Binding** to the original **Turn ID**, even if that receipt is already `interrupted`; only an explicit Owner retry with a new Source Request ID creates another Turn."
>
> **Dev:** "Desktop loses SSE while that Turn is running. Does reconnecting resubmit the prompt?"
> **Architect:** "No. Submission already returned the **Turn ID**. Reconnection observes that Turn after its last Event sequence; losing the Response Sink is not `cancel(Turn ID)`."
>
> **Dev:** "The Telegram Turn succeeded, but sending its reply timed out. Should the provider's inbound redelivery run the Agent again?"
> **Architect:** "No. The original **Turn Receipt** remains succeeded and its one **Outbound Delivery** remains independently queued, failed or unknown. Inbound redelivery returns the original Turn; only the **Delivery Pump** may retry a proven-safe send."
>
> **Dev:** "The Backend crashed after a Channel provider may have accepted chunk two but before we saved the provider message ID. Should startup send it again?"
> **Architect:** "Only when provider idempotency or reconciliation proves that safe. Otherwise the **Delivery Item** becomes `unknown`; an explicit Owner resend creates a new linked **Delivery ID** so a possible duplicate is intentional and auditable."
>
> **Dev:** "Should Desktop reconnect receive an old final response through the Outbox?"
> **Architect:** "No. Desktop SSE is observation, not Outbound Delivery. It reads the existing Transcript and Turn state; the persistent Outbox is only for canonical terminal replies pushed to Channel providers."
>
> **Dev:** "Feishu already showed a temporary 'analyzing' placeholder. Can the terminal Delivery rely on editing it?"
> **Architect:** "No. The placeholder is ephemeral Response Sink progress. The canonical terminal Delivery is independent; editing or deleting the placeholder is best effort and never determines whether the Turn or Delivery succeeded."
>
> **Dev:** "Desktop timed out after submitting a message with two files and retries the same Source Request ID. Should it upload the files again?"
> **Architect:** "No. The matching descriptors and request fingerprint return the original **Turn ID** and its two **Attachment Records**. Duplicate detection happens before attachment staging, so the retry neither downloads nor republishes bytes."
>
> **Dev:** "The Owner sends the same PDF again as a new message. Is it the old attachment?"
> **Architect:** "No. A different Source Request ID is distinct intent and receives a new **Attachment Record**. The two Records may reference the same content-addressed **Attachment Blob**, but they remain different message occurrences owned by different Turns."
>
> **Dev:** "Should `/new`, Turn cancellation, or Project Archive delete the uploaded inputs?"
> **Architect:** "No. Accepted Attachment Records are immutable evidence owned by their Turns. Those operations retain them; Blob collection is allowed only after no durable Record, Run input, or other durable reference remains."
>
> **Dev:** "Feishu gave the Adapter a temporary download path. Can that path be put in the prompt or Transcript?"
> **Architect:** "No. The Adapter emits a Source Attachment Descriptor; the Attachment Store verifies and publishes the bytes, and durable consumers receive an **Attachment Reference**. Provider handles and temporary paths never become the domain contract."
>
> **Dev:** "I found a `project_meta.json` directory and `project://` Memory, but there is no Project Record. Does that prove the Project exists?"
> **Architect:** "No. Those are legacy or orphaned projections that require migration or reconciliation. Only **Control Plane State** establishes the Project; repair may attach valid content to a Project Record but ordinary runtime lookup never invents one from a projection."
>
> **Dev:** "The active Desktop Conversation belongs to an archived Project. Should its next message create an unbound Conversation instead?"
> **Architect:** "No. The **Project Archive** preserves both the immutable Project binding and the **Active Conversation Binding**. A duplicate returns its original Turn; a novel message gets `project_archived` until the Owner restores that same Project."
>
> **Dev:** "Desktop `omicsclaw.db` still has an old chat row, but the Control Database has no Conversation Record. Should the Backend reopen it automatically?"
> **Architect:** "No. An explicit migration may create a new opaque Conversation and record the old row in the **Legacy Identity Map**. After cutover the App row is only a UI cache; runtime fallback would recreate a second authority."
>
> **Dev:** "Can a remote Run Worker open `control.db` to validate its Project?"
> **Architect:** "No. The single-process control plane validates the Project first and sends a typed Run Request. The Worker executes that request but never owns control identity."
>
> **Dev:** "The remote process exited zero, but its completion report was never committed. Is the Run succeeded?"
> **Architect:** "No. Exit code is only executor evidence. The **Run Receipt** becomes `succeeded` only after the **Run Manifest**, completion evidence and required artifacts are durably verified."
>
> **Dev:** "The Backend restarted while a local Run was active. Should we mark it failed or submit it again?"
> **Architect:** "Neither. Its Receipt becomes `interrupted`, which says execution ownership was lost rather than claiming scientific failure. An explicit retry creates a new **Run ID** linked by `retry_of_run_id`; v1 never replays it automatically."
>
> **Dev:** "Desktop lost the response to `POST /runs` and sends the same action again. Do we create another Run?"
> **Architect:** "No. It reuses the original **Run Submission ID**. The matching Binding returns the same Run ID and current Receipt; it neither revalidates current navigation nor creates another Assignment."
>
> **Dev:** "A delayed remote callback reports success with an old Assignment ID. Can it win over the current state?"
> **Architect:** "No. Every executor report is fenced by both Run ID and **Assignment ID**. A mismatch is rejected, and no terminal Run can be reopened."
>
> **Dev:** "The Resource Lease expired. May another Worker run the same Run?"
> **Architect:** "No. Resource Lease is accounting, not execution ownership. v1 permits only one process-bound **Execution Assignment** and never authorizes same-Run reassignment from a timeout."
>
> **Dev:** "I ran a quick analysis before selecting a Project. Can I move that Run into a Project afterward?"
> **Architect:** "No. It keeps `UnassignedScope` because Run Scope is execution provenance. Selecting the Project changes future Runs only. A future Project-to-Run evidence reference may cite the old Run without moving, copying or retagging it."

## Resolved-by-default decisions

Decisions that the refactor PRs (#125–#132) chose by sensible default rather than explicit RFC:

- **One Memory database, many Namespaces** — `OMICSCLAW_MEMORY_DB_URL` selects a single SQLite/Postgres Memory database; Namespace columns partition the data inside it. Different desktop launches with different `OMICSCLAW_DESKTOP_LAUNCH_ID` values share the same DB but get distinct Namespaces. Dropping a per-launch DB option keeps cross-launch read-fallback (`__shared__`) working with no extra cross-DB plumbing. This says nothing about the separate Control Database.
- **`oc interactive` from `~/`** — uses the absolute home path as Namespace. No special-case handling; `~` is a valid string id like any other directory.
- **Read fallback policy is asymmetric** — `recall` and `search` fall back to `__shared__`; `list_children` and `get_subtree` do not. The asymmetry is deliberate: per-row fallbacks give per-Namespace contexts visibility into system-shared content, but per-listing fallbacks would pollute the current Namespace's inventory with shared structure.
- **No legacy output migration** — the pre-existing flat `output/<skill>__…__<uuid8>/` directories are disposable test data (owner's call, 2026-06-24); the Project layout is a clean cut-over with no migration step. The listing walk may still tolerate a root-level run dir (treating it as `default`) for safety, but no data is moved or preserved by contract.

## Open questions

Tracked but not yet resolved in code:

- **Permanent Project data purge** — ADR 0055 deliberately excludes purge from v1. If permanent erasure is required, a separate decision must define archived/idle preconditions, dry-run inventory, explicit Owner confirmation, durable cross-store progress, retry/recovery, content-specific deletion and the minimum surviving Project tombstone; it must not be introduced as another ordinary lifecycle enum value.
- **Cross-process Worker execution protocol** — ADR 0058 deliberately keeps v1 Assignments process-bound and non-renewable. A future independent Worker fleet or durable scheduler needs a separate decision for executable-payload durability, authenticated dispatch, heartbeat/fencing generations and proof of safe same-Assignment reattachment; lease expiry alone may never authorize same-Run reassignment.

## Resolved (kept here for tombstone)

- ~~**Bare "Session" as conversation continuity**~~ — **Resolved (2026-07-14)**: the domain term is **Conversation**. Existing `Session` models, `session://` Memory URIs, and `session_id` fields are legacy implementation names to migrate deliberately. If account login is introduced later, its state must be named **Authentication Session** in full and remains distinct from Conversation.
- ~~**Multi-user User / Participant model**~~ — **Retracted (2026-07-14)** by ADR 0044: one backend serves exactly one **Owner**. Surface identities are **Owner Identities** used for ingress admission, and non-Owner Channel senders never enter a Conversation.
- ~~**Owner Identity as a Memory or storage partition**~~ — **Rejected (2026-07-14)** by ADR 0045: it is used only for ingress admission and non-secret Source Attribution. Durable state is owned by the singleton Owner or an explicit Conversation, Project, Workspace, Run, or system scope.
- ~~**Physical or movable Home Reply Target**~~ — **Rejected (2026-07-14)** by ADR 0049: Conversation stores an immutable logical **Conversation Address**, while the live **Response Sink** is recreated per dispatch. Continuing at another logical target creates another Conversation under the same Project rather than moving the Transcript.
- ~~**Ingress idempotency mapping**~~ — **Resolved (2026-07-14)** by ADR 0052: one durable **Ingress Idempotency Binding** maps `(Surface, Source Namespace, Source Request ID)` plus a request fingerprint to the canonical Turn ID; duplicates never replay a terminal or interrupted Turn, while SSE reconnects observe that Turn directly by Turn ID and Event sequence.
- ~~**Project authority split among `project://`, `project_meta.json`, and Surface thread IDs**~~ — **Resolved (2026-07-14)** by ADR 0053: the **Project Record** in **Control Plane State** is authoritative for Project identity, display metadata and lifecycle; Memory knowledge and output-directory metadata retain specialized associated-content and projection roles.
- ~~**Physical Control Plane State store**~~ — **Resolved (2026-07-14)** by ADR 0054: one Backend-exclusive local SQLite **Control Database** persists all authoritative control records under a lifetime single-process lock; Transcript, Memory, App and Run stores remain separate, and legacy import never becomes a runtime fallback.
- ~~**Project archive/restore/delete lifecycle**~~ — **Resolved for v1 (2026-07-14)** by ADR 0055: a Project is `active` or `archived`; restore returns the same aggregate to `active`, existing references and content remain intact, and permanent cross-store purge is a separate future workflow rather than a Project state.
- ~~**`default` Run grouping lifecycle and Run reassignment**~~ — **Resolved for v1 (2026-07-14)** by ADR 0056: `default/` is the non-Project **Unassigned Run Grouping**; every Run freezes `ProjectScope(project_id)` or `UnassignedScope` at admission, and no move, retag, copy or symlink operation can reassign it afterward.
- ~~**Run identity, record ownership and lifecycle**~~ — **Resolved for v1 (2026-07-14)** by ADR 0057: the control plane generates an opaque Run ID and persists a minimal non-replayable Run Receipt; Run storage retains the scientific Manifest and artifacts, executor-specific identifiers are Execution References, restart interruption never implies failure or automatic replay, and retry creates a new linked Run.
- ~~**Run submission idempotency and execution assignment**~~ — **Resolved for v1 (2026-07-14)** by ADR 0058: a caller-generated opaque Run Submission ID durably binds one logical submission to one Run ID and Fingerprint; each accepted Run receives at most one process-bound Assignment-ID-fenced start grant, while v1 deliberately has no renewable Execution Lease, heartbeat reassignment or automatic replay.
- ~~**Inbound attachment identity, ownership, staging and retention**~~ — **Resolved for v1 (2026-07-14)** by ADR 0059: every accepted attachment occurrence has one opaque Attachment ID and immutable per-Turn Attachment Record in the specialized Attachment Store; duplicate detection precedes staging, byte-equal novel submissions remain distinct Records that may share a Blob, publish-before-control reconciliation closes the cross-store crash window, and accepted attachments have no ordinary delete operation.
- ~~**Terminal Channel reply failure and retry**~~ — **Resolved for v1 (2026-07-14)** by ADR 0060: one canonical opaque Outbound Delivery is created atomically with each Channel Turn's terminal Receipt; the bounded in-process Delivery Pump persists safe retry across restart without replaying the Turn, Desktop/CLI observation and live progress remain outside the Outbox, and provider-acceptance ambiguity becomes explicit `unknown` rather than a blind duplicate send.
- ~~**Run dispatch order and compute-resource ownership**~~ — **Resolved for v1 (2026-07-14)** by ADR 0061: every accepted top-level Run enters one bounded process-local strict-FIFO Run Dispatcher, which obtains first-unit resource readiness before the sole Assignment transition; one independent strict-FIFO Execution Resource Scheduler atomically leases multidimensional capacity to all scientific processes. Neither mechanism is a durable executable queue, Resource Lease is not execution ownership, and v1 accepts deterministic head-of-line blocking instead of priority, bypass or preemption.
- ~~**Dynamic Run nested resource acquisition**~~ — **Resolved for v1 (2026-07-15)** by ADR 0062: a fixed plan obtains one global Resource Lease per process, while a dynamic Run obtains one aggregate Governed Resource Envelope and uses Run-local allocations. A live parent may never submit a nested global ticket, eliminating the strict-FIFO parent/child deadlock.
- ~~**Cross-Delivery ordering at one Reply Target**~~ — **Resolved for v1 (2026-07-15)** by ADR 0063: Deliveries receive a target-local sequence, at most one provider call runs per Reply Target, and a failed/unknown prefix suppresses its unattempted suffix before the next Delivery may proceed.
- ~~**`_auto_capture_dataset` ownership and Project archive projection race**~~ — **Resolved for v1 (2026-07-15)** by ADR 0064: pre-existing local-file observations are Workspace-scoped, uploads retain Attachment identity, Projects cite them through Dataset References, and only a frozen Project Projection Intent may complete accepted scientific Memory projection after archive.
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

- **"user"** previously meant the human researcher, a chat sender, or the Linux process owner. Resolved: **Owner** is the sole human served by the backend; `user_id` is a legacy transport or storage field, and neither a non-Owner sender nor the Linux process owner is an Owner in this context.
- **"workspace"** appears in both the **Surface** layer (the directory the user picked, used as the Namespace string) and the `ScopedMemory` layer (a filesystem root). Same physical directory, different concepts; will collapse only if ScopedMemory is integrated into MemoryEngine.
- **GraphService is retired.** Production code uses `MemoryEngine` / `ReviewLog` / `MemoryClient` exclusively. The legacy path-based admin operations (`/api/browse/*` write endpoints) live in a private `omicsclaw/memory/api/_browse_helpers.BrowseHelpers` class consumed only by the `oc memory-server` admin UI — do not import from outside `omicsclaw/memory/api/`. A future rewrite can port the admin UI against `MemoryEngine` and delete this module entirely.
- **"namespace"** vs **"domain"**: domain is the URI prefix (`dataset`, `core`, …); Namespace is a legacy Memory partition column whose current values mix workspace, launch, and transport identity. They are orthogonal; do not use either as a synonym for Owner, Conversation, or the future explicit state-ownership model.
- **"deduplication"** previously meant both a short-lived Adapter cache and semantic content equality. Resolved: conversational redelivery uses the durable **Ingress Idempotency Binding**; equal content alone never identifies the same Turn.
- **"attachment/file/upload/path"** previously blurred provider objects, copied bytes, prompt text, Workspace files and a mutable Session registry. Resolved: a **Source Attachment Descriptor** fingerprints declared inbound intent, an **Attachment Record** identifies one accepted occurrence, an **Attachment Blob** stores verified immutable bytes, an **Attachment Reference** is the durable consumer contract, and a **File Reference** names a separately authorized pre-existing Workspace file. Absolute and temporary paths are never attachment identity.
- **"outbox/retry/resend"** previously blurred the Desktop KG HandoffPacket execution queue, provider SDK retry and re-execution of a Turn. Resolved: **Outbound Delivery Outbox** means only persisted terminal Channel Delivery Items; safe **Delivery Attempt** retry never enters execution, while explicit resend creates a new linked Delivery ID. The KG mechanism is a handoff queue/executor and must be named separately.
- **"project"** previously referred interchangeably to a Bench thread, `project://` Memory node, output directory and research continuity object. Resolved: **Project** is the control-plane aggregate; the others are UI, knowledge or filesystem projections of its opaque Project ID.
- **"delete Project"** previously meant setting legacy `ThreadMemory.is_deleted` and hiding the Bench thread while all content remained. Resolved: that operation is **Project Archive**; permanent data purge, if ever accepted, is a distinct cross-store workflow and must not use archive or ordinary DELETE semantics.
- **"database"** previously referred interchangeably to graph Memory, Transcript, CLI Session, Desktop App and future control persistence. Resolved: **Control Database** means only Backend `control.db`; every other database is named by its owning subsystem.
- **"Run record"** previously combined output directory, Manifest, remote Job state, Desktop cache and lifecycle. Resolved: **Run Receipt** owns accepted identity/Scope/operational lifecycle, **Run Manifest** owns scientific provenance, and indexes/UI/Memory are projections.
- **"run_id"** previously named a readable directory leaf, an autonomous short token, a remote Job UUID and a Desktop process-local counter. Resolved: **Run ID** means only the opaque control-generated identity; all legacy values migrate as storage names, aliases or Execution References.
- **"job/request/task ID"** previously blurred client retry intent, canonical scientific identity, executor ownership and transport observation. Resolved: **Run Submission ID** identifies pre-acceptance retry intent, **Run ID** identifies the accepted scientific execution, **Assignment ID** fences its one executor start grant, and **Execution Reference** identifies replaceable executor machinery. Remote `run-<run_id>` is only a compatibility projection; a legacy Job UUID remains historical storage identity, and Job SSE is an observer rather than an execution-queue consumer.
- **"lease"** previously risked conflating resource accounting with execution ownership. Resolved for v1: **Resource Lease** means only a process-local resource reservation; Run ownership is a non-renewable **Execution Assignment**, and no Execution Lease exists in the accepted v1 protocol.
- **"queue/concurrency/scheduler"** previously blurred accepted Run buffering, active orchestration, Candidate-plan semaphores, compute admission and Remote Job persistence. Resolved: the **Run Dispatcher** owns bounded top-level FIFO and Assignment eligibility, the **Execution Resource Scheduler** alone owns global scientific-process capacity, and a per-Run ready-Step window only limits how many tickets one Run may expose.
