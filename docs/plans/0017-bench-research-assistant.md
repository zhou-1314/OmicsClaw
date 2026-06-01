# Plan 0017 — Bench: the desktop research assistant

**Status:** design complete, ready to build · 2026-05-30
**Decisions:** [ADR 0017](../adr/0017-bench-research-continuity-workspace.md) ·
[0018](../adr/0018-investigation-thread-equals-project.md) ·
[0019](../adr/0019-kg-first-class-dependency-for-bench.md) ·
[0020](../adr/0020-stage-is-backend-aware-permissive.md) ·
**Vocabulary:** [docs/bench/CONTEXT.md](../bench/CONTEXT.md)

## 1. What we're building

**Bench** (working codename, 研究台) is a new page on the OmicsClaw **Desktop Surface**: a
*study-scoped research-continuity workspace* that carries one research project across the
**Read → Ideate → Analyze → Write** lifecycle, over the **same** agent loop that `/chat`
already drives. It is not a chat box and not a CodePilot-style persona companion; its
differentiator is the durable **investigation thread**. Three repos:

- **Backend** `/data/beifen/zhouwg_data/project/OmicsClaw` — the agent loop, Desktop Surface,
  memory, skills, Analysis Router.
- **Frontend** `/home/weige/project/OmicsClaw-App` — Electron + Next.js 16; gets the `/bench` page.
- **Knowledge base** `/home/weige/project/OmicsClaw-KG` — Python wiki+graph KB; already mounted
  under `/kg`.

## 2. Locked decisions (the plan must honor these)

| # | Decision | ADR |
|---|---|---|
| 1 | Bench = research-continuity workspace, **not** a companion. One engine, two zoom levels (`/chat`=task, Bench=study), bridged bidirectionally. A page on the Desktop Surface, not a 4th Surface. | 0017 |
| 2 | Investigation thread = one research **project** (`project://<id>`). Manuscript = child write-target (0..n per thread). | 0018 |
| 3 | Thread binding = **soft grouping** in the stable `app/<user_id>` namespace. Cross-thread recall **deliberately allowed** (method transfer is a feature). Not a per-thread namespace. | 0018 |
| 4 | OmicsClaw-KG = **first-class bundled dependency** + graceful soft-fail. Reads via MCP tools; write-loop via HTTP `/kg`. Two-store boundary: Memory owns *state*, KG owns *reading knowledge*. | 0019 |
| 5 | Stage = **backend-aware, permissive**. `stage` request field + a single stage→tool-subset map. Defaults, not jails. | 0020 |
| 6 | Scope: **v1** = thread binding + Read + Analyze + light skippable onboarding. **v1.5** = Ideate. **v2** = Write + read-only heartbeat + episodic daily memory. | — |
| 7 | Read data flow: KG ingest **always** (citation substrate) + `literature` skill (GEO/metadata) + permission-gated download → `dataset://` under the thread. | — |
| 8 | Research-stance persona = thin additive fragment over `core://agent`; tone only; cannot override SOUL.md. No parallel markdown store. | 0017/0020 |

## 3. Cross-repo contracts (the seams)

- **Chat request (FE→BE).** `POST /chat/stream` body (`ChatRequest`, `server.py:560`) gains two
  optional fields next to the existing `system_prompt_append`: `thread_id: str = ""`
  (backend-generated `project://` UUID; the frontend echoes the selected thread's id) and
  `stage: str = ""` (`read|analyze|ideate|write`; empty = legacy full-tool behavior). The
  frontend route `src/app/api/chat/route.ts` **must explicitly add both** to the forwarded body
  (it currently only spreads `system_prompt_append`).
- **Envelope (in-process).** `MessageEnvelope` (`envelope.py:20`, frozen) gains `thread_id` and
  `stage` in the defaulted block (anywhere after `content`; all fields from line 24 already
  default). **They follow the exact threading template of the existing `scoped_memory_scope`
  field (`envelope.py:29`)** — request→envelope→`dispatcher.py:113`→`loop.py:1037`→
  `engine/loop.py:85`→`QueryEngineContext` (`engine/loop.py:200`).
- **Thread CRUD (FE→BE), NEW REST.** `POST /thread/create {name,description?,domains?,organism?,platforms?,venue?}`→`{thread_id,...}`;
  `GET /thread/list`; `GET/PUT/DELETE /thread/{id}` (soft-delete); `GET/PUT /thread/{id}/preference`.
  Authoritative data lives in `project://<thread_id>`. Backend enforces ownership via
  session/namespace — never trust a raw client `thread_id`.
- **Onboarding (FE→BE).** `POST /onboard/user {domains,organism,platforms,venue}` → versioned
  `core://my_user`; Skip → `preference://bench/onboarded=true`.
- **Memory URI conventions (two-store boundary).** Memory owns *state*: `project://<thread_id>`
  (add `('project','')` to `VERSIONED_PREFIXES`, `namespace_policy.py:27`),
  `analysis://<thread_id>/typed|exploratory/<run_id>` (thread-scoped via `consensus_namespace`),
  `dataset://<basename>` (overwrite-only), `insight://`, `preference://bench/*`, `core://my_user`,
  `core://agent/research_stance`. KG owns *reading knowledge*: Source/Entity/Concept/Method/Hypothesis pages.
- **KG reads (BE→KG, MCP).** `kg_tools.py` wraps the read tools from
  `OmicsClaw-KG/omicsclaw_kg/mcp_server/tools.py` (`kg_search`, `kg_get_page`, `kg_list_pages`,
  `kg_graph_neighbors`, `kg_status`, `kg_recent_log`, `kg_communities`) as OpenAI functions.
- **KG writes (BE→KG, HTTP).** Already-mounted `/kg` router (`server.py:181`,
  `build_kg_router(enable_writes=True)`, gated `server.py:519`). `kg_write_client.py` is a thin
  HTTP wrapper over `POST /kg/handoff`, `POST /kg/record-result`, `POST /kg/ideate/*`; **soft-fails
  to `{error:...}`, never raises into the loop**; validates `PACKET_SCHEMA_VERSION`.
- **KG availability (BE→FE).** A `_KG_AVAILABLE` flag (extend `_register_optional_kg_router`)
  surfaced via `GET /kg/status|/health`; frontend `useKGStatus` polls it; dark → Read/Ideate UI
  shows one-click install prompt, KG tools skipped, Analyze unaffected.
- **Stage→tool-subset map (single source of truth).** Lives ONCE in the backend tool runtime;
  consumed where `request_tools` are built; the **frontend never gates tools**, only sends the
  string. `read` = read/recall/search/ask_user/literature/kg-ingest/kg-read; `analyze` = full;
  `ideate` = read + ideate; `write` = write subset.
- **System-prompt composition order.** SOUL.md (immutable) → `core://agent` (base) →
  `core://agent/research_stance` (tone) → stage fragment → `system_prompt_append` (user). No
  layer overrides SOUL.md (ADR 0020 "Authority vs. order").

## 4. Verified ground truth (the plan rests on these reads)

- `ChatRequest.system_prompt_append` at `server.py:559`; forwarded `server.py:1895`.
- `MessageEnvelope` frozen; **`scoped_memory_scope: str = ""` already exists** (`envelope.py:29`) —
  the proven plumbing template for `thread_id`/`stage`.
- `desktop_namespace()` (`memory/__init__.py:200`) = `app/<user_id>`, **stable across launches**
  (ignores `OMICSCLAW_DESKTOP_LAUNCH_ID`). MemoryClient bound once at `server.py:432`.
- `core://agent`, `core://my_user` ∈ `VERSIONED_PREFIXES` (`namespace_policy.py`), boot-loaded
  `memory_client.py:525`.
- KG router mounted `server.py:181`, gated `server.py:519`.
- Skill execution = `omicsclaw/execution/executors/default.py:96 SkillRunnerExecutor` →
  `skill_runner.arun_skill` (`:118`). Consensus driver = `runtime/consensus/driver.run_typed_consensus`;
  pipeline = `pipeline_runner.run_pipeline`.
- `consensus_namespace(run_id, mode)` = `runtime/consensus/dispatch.py:56` — **no production caller
  found in grep** → see Spike S1.
- Frontend: existing `/chat` at `src/app/chat/page.tsx`; nav `src/lib/primary-nav.ts`; icons/groups
  `src/components/layout/ChatListPanel.tsx` (`workspace`/`intelligence`); SSE `src/hooks/useSSEStream.ts`;
  proxy `src/app/api/chat/route.ts`; reusable `src/components/chat/*`; i18n `src/i18n/{en,zh}.ts`.
  Frontend sends `systemPromptAppend` but **no scope/thread binding**.

## 5. Required spikes (do these FIRST — the critic's blockers)

- **S1 — consensus/skill thread-scoping (BLOCKER for thread-scoped lineage).** Find the production
  caller of `consensus_namespace` and how `SkillRunnerExecutor`/`run_typed_consensus`/`run_pipeline`
  are invoked from the loop. Decide where `thread_id` reaches the run so outputs land under
  `analysis://<thread_id>/...`. *Until S1 lands, Phase 0 must NOT change `consensus_namespace`.*
- **S2 — stage→tool reach.** Confirm how the tool list is built for a request and where `stage`
  can intercept it (the tool builder takes a `request`, not a `stage`). Decide: add `stage` to the
  context the builder sees, or filter post-build. Owns task `BE-STAGE-REQUEST-THREADING`.
- **S3 — multi-layer system prompt.** `_merge_system_prompt_additions` (`loop.py:1172`) appends a
  single addition today. Decide refactor vs. new `_compose_system_prompt()` for the 5-layer order.
  Owns `BE-PERSONA-MULTI-LAYER`.

## 6. Phase plan

Phases 0–5 = **v1**; Phase 6 = **v1.5**; Phase 7 = **v2**. Each task: `ID (repo) — what · Files · Done when`.

### Phase 0 — Walking skeleton: pure plumbing pass-through (rescoped per critic)
Prove REQUEST→ENVELOPE→DISPATCHER→LOOP for two new optional fields with **zero behavior change**
when empty. **No** consensus changes, **no** stage filtering, **no** persona — those are later.

- `BE-REQ-1` (be) — add `thread_id`,`stage` to `ChatRequest`; invalid stage tolerated, not 400 (permissive). · `server.py:560` · empty values byte-identical to today.
- `BE-ENV-2` (be) — add both to `MessageEnvelope` defaulted block; mirror `scoped_memory_scope`. · `envelope.py` · frozen contract valid; `server.py:1880` constructs with both.
- `BE-LOOP-3` (be) — accept+pass-through (do not use yet) through `dispatcher.py:113` → `loop.py:1037` → `engine/loop.py:85`; store `thread_id` on `QueryEngineContext` (`engine/loop.py:200`). · those files · no type errors.
- `BE-SKEL-TEST` (be) — test: request with `thread_id`/`stage` set carries through to context; empty request identical to baseline; existing tests pass. · `tests/` · green.

**Acceptance:** an empty-field turn is unchanged; a set-field turn reaches `QueryEngineContext` with the values observable but inert.

### Phase 1A — Thread-scoped lineage (depends on S1)
- `AN-CONSENSUS-INTEGRATION` (be) — thread `thread_id` into the run path so `consensus_namespace(run_id, mode, thread_id="")` yields `analysis://<thread_id>/typed/<run_id>` (empty=unchanged). Add `('project','')` to `VERSIONED_PREFIXES`. · `consensus/dispatch.py`, `consensus/driver.py`, `execution/executors/default.py`, `namespace_policy.py:27` · a thread-bound sc-de lands under `analysis://<thread_id>/...`; empty `thread_id` unchanged.

### Phase 1 — Thread lifecycle + frontend rail
- `BE-THREAD-CRUD-2` (be) — `/thread/create|list|{id}` CRUD writing `project://<uuid>` metadata `{thread_id,name,created_at,description,domains,organism,platforms,venue}`; soft-delete. **`chat_sessions` gains a `thread_id` column** stamped at lazy session creation; the backend resolves `thread_id = request.thread_id ?? session.thread_id` (ADR 0023 decision 3), so thread binding survives turns that omit the field (e.g. a plain /chat turn on a thread-bound session). Thread-bound sessions are hidden from the /chat default list. · `server.py` (+ a `thread` router module), session model · CRUD round-trips; a thread-bound session's runs land under `analysis://<thread_id>` even without the request field.
- `BE-RECALL-6` (be) — `recall`/`search` accept optional `thread_id`; default scopes to `project://<thread_id>/*` + `analysis://<thread_id>/*`; **cross-thread fallback allowed** (see §7). · `memory_client.py`, `agent_executors.py` · thread-scoped by default; tool can widen.
- `BE-CTX-7` (be) — `thread_id` from envelope into context assembly so completions register under the thread lineage. · `engine/loop.py` · skill/consensus outputs thread-prefixed.
**Frontend three-zone architecture: [ADR 0023](../adr/0023-bench-threezone-frontend.md)** (shell
auto-collapse to 64px; one-thread-one-conversation with stage-as-lens + per-message stage badge;
composer stage switcher + hidden `mode`; bench-owned exclusive `StageRail`; structured in-page
`onCardAction` callback; route `/bench/[threadId]?stage=`).

- `FE-NAV-1` (fe) — register `/bench` in `primary-nav.ts` (intelligence group) + icon in `ChatListPanel.tsx` + i18n keys; auto-collapse the nav to 64px on `/bench` (`toggleChatListCollapsed`). · those files, `i18n/{en,zh}.ts` · nav item renders; nav collapses on /bench.
- `FE-PAGE-2` (fe) — three-zone `/bench/[threadId]` page (thread rail | center chat | bench-owned `StageRail`); `StageRail` swaps a per-stage panel exclusively (FilePreview embedded by prop). · `src/app/bench/[threadId]/page.tsx`, `src/components/bench/StageRail.tsx` · layout renders; stage swaps the right panel.
- `FE-THREADRAIL-3` (fe) — thread list + New Thread dialog (create+select). · `src/components/bench/*` · creating a thread selects it.
- `FE-CHATEMBED-5` (fe) — `BenchChatArea` wraps existing `chat/*` verbatim, bound to active thread, injecting `thread_id`+`stage` into the POST; adds a composer **stage switcher** (Radix Tabs beside `ModeIndicator`) and **hides `mode`**; per-message **stage badge**; a typed `onCardAction(...)` from `StageRail` into `sendMessage(...)` (prefill + switch stage, no auto-send). · `src/components/bench/*`, `src/app/api/chat/route.ts` (forward `thread_id`+`stage`) · turns carry both; cards inject structurally.
- `FE-STAGE-PROPOSE` (fe) — render the backend permissive "switch to Analyze?" as a **one-click proposal card** (AskUserCard/TodoPlanCard precedent), NOT silent `onModeChanged`; Accept re-runs the triggering message under the new stage. · `src/components/bench/*` · inferred-intent proposes; explicit card-click is consent.
- `FE-THREADID-URL-BINDING` (fe) — route **`/bench/[threadId]?stage=`** (path = durable thread, query = ephemeral stage; matches `/chat/[sessionId]`); bidirectional sync (URL↔rail↔chat↔stage); localStorage = bare-`/bench` last-thread fallback only. · bench page/hooks · reload restores thread+stage; single-user deep-link.
- `FE-ARTIFACTS-9` (fe) — `useThreadArtifacts` fetches `analysis://<thread_id>/*` for the right rail (depends on 1A). · bench hooks · artifacts list per thread.

**Acceptance:** create a thread, send a turn (fields on the wire), see thread-scoped artifacts; cross-thread isolation by default; `core://my_user`+`preference://bench/*` shared across threads.

### Phase 2 — Stage mechanism (depends on S2, S3)
- `BE-STAGEMAP-LOCATION` + `BE-STAGEMAP-4` (be) — define `STAGE_TO_TOOL_SUBSETS` once in the tool runtime (decide registry per S2); respect existing skill-enabled gating. · `runtime/tools/registry.py` · single source of truth.
- `BE-STAGE-REQUEST-THREADING` + `BE-STAGEFILTER-4b` (be) — make `stage` reach the tool builder; filter only when non-empty (empty/unknown=full set). · per S2 · `stage=read`→read subset; `stage=''`→all.
- `BE-STAGEPROMPT-5` + `BE-PERSONA-MULTI-LAYER` (be) — `STAGE_SYSTEM_PROMPTS` + 5-layer composer (per S3), order per §3. · `loop.py` · stage text in final prompt; SOUL.md never shadowed.
- `BE-STAGE-TEST` (be) — subsets + composition + permissive nudge. · `tests/` · green.

**Acceptance:** per-stage tool subset + stage fragment apply; permissive (Read proposes "switch to Analyze"); frontend gates nothing.

### Phase 3 — Read stage (depends on Phase 2)
- `KG-TOOLS-2` + `KG-IMPORT-GUARD` (be) — `kg_tools.py` wraps MCP read tools as OpenAI functions; lazy/guarded import (absent KG must not break load). · `runtime/tools/kg_tools.py` · tools callable; absent KG = clean skip.
- `KG-SOFTFAIL-10` (be) — `_KG_AVAILABLE` flag via `/kg/status|/health`; Read/Ideate tools skipped when dark; Analyze unaffected. · `server.py:519` · dark KG degrades cleanly.
- `RD-INGEST-9` (be) — on paper drop, KG ingest **always** runs (citation substrate). · Read handler · Source pages created; answers cite them.
- `RD-LIT-8` + `RD-DATASET-14` (be) — `literature` skill extracts GEO/metadata; permission-gated download → `dataset://<basename>` under thread (overwrite-only). · `skills/literature/*`, memory · downloaded dataset visible to Analyze in same thread.
- `FE-READPANEL-4` + `FE-KGSTATUS-7` (fe) — ReadPanel renders Source pages / one-click install prompt; `useKGStatus` polls. · bench components · prompt auto-dismisses when KG up.

**Acceptance:** read a paper → cited Q&A + offered dataset; KG-dark path degrades; Analyze still runs.

### Phase 4 — Analyze stage + persona (depends on 1A, 3)
- `AN-ROUTER-10` (be) — Analyze routes through the **unchanged** Analysis Router; `thread_id` is a plumbing param on the consensus/skill/autonomous runners so results land thread-scoped. · `analysis_router/*`, `execution/executors/default.py` · GSE-download+sc-de stores under `analysis://<thread_id>/...`.
- `AN-CTXRECALL-11` (be) — context injects thread-scoped recall (dataset from Read, KG Source pages, prior runs). · `engine/loop.py` · agent can reference "the paper we read".
- `AN-PROV-CAPTURE-13` (be) — **provenance index for future Write** ([ADR 0022](../adr/0022-bench-write-manuscript-object-model.md) decision 0; v1 because the assisted-param decision is ephemeral). Enrich `_auto_capture_analysis` to read `result.json`/`manifest.json` post-run (`{run_id, skill, method, effective_params, artifact paths, version/checksum}`); **instrument the agent loop's tool-result callback to record the assisted-parameterization recommendation + accept/reject**; persist `TypedConsensusRun` under `analysis://<thread_id>/typed/<run_id>`. Bulky DataFrames stay on disk by path. · `omicsclaw/skill/orchestration.py`, `runtime/agent/loop.py`, `runtime/consensus/driver.py`, `memory/compat.py` · every thread run has a memory-resident, queryable provenance record incl. the method-choice decision.
- `BE-PERSONA-7` + `BE-PERSONA-BOOT-9` (be) — seed `core://agent/research_stance` (thin JSON tone), boot-load alongside `core://agent`, inject at persona layer; empty default = no-op. · `memory_client.py:534`, `loop.py` · composition order test passes.
- `FE-ANALYZEPANEL-4b` + `FE-HANDOFF-8` (fe) — Analyze artifact cards (skill/params/results, View/Rerun); "Run in Chat" / "Save to Bench" round-trip. · bench components, `bench-handoff.ts` · bidirectional bridge works; empty-thread Analyze == today.

**Acceptance:** thread-bound analysis via existing machinery; persona stack correct; `/chat` un-regressed.

### Phase 5 — Onboarding + persona seed + E2E (closes v1)
- `BE-ONBOARD-8` + `BE-PREF-7` (be) — `POST /onboard/user` → versioned `core://my_user`; Skip → `preference://bench/onboarded`; `preference://bench/cross_thread_recall` (default off). · `server.py` · onboarding persists across launches; missing values default gracefully.
- `FE-ONBOARD-6` (fe) — first `/bench` visit with empty `core://my_user` → `/bench/onboard`; ≤5 questions; Skip. · `src/app/bench/onboard/*` · no re-prompt after.
- `BE-WIRING-TEST-8` + `FE-E2E-13` — full backend wiring test + frontend E2E (create→turn→artifacts→soft-fail→bridge→reload→no `/chat` regression). · `tests/` · green.

### Phase 6 — v1.5 Ideate (design: [ADR 0021](../adr/0021-bench-ideate-v1_5-design.md))
Touches **all three repos**. The KG ideation engine is thread-blind + batch; Ideate filters
post-hoc and adds a net-new formalize path.

- `KG-WRITECLIENT-4` (be) — `kg_write_client.py` HTTP wrapper over `/kg` write endpoints; soft-fail to `{error}`; schema_version. · new file · never raises into loop.
- `KG-IDEATE-5` (be) — auto-from-questions tool via HTTP gateway; **thread-filter post-hoc** (`supported_by` ∩ thread Source slugs ≥1; cross-study badge when it also cites other threads); idempotent by question slug; cache in `project://<thread_id>/ideate_cache`. · `kg_tools.py` · only thread-relevant drafts surface.
- `KG-FORMALIZE-NEW` (kg) — **net-new** KG endpoint + drafter: free-text hunch → grounded Hypothesis via the existing closed-list validator over the thread's Source slugs. **Soft gate**: no support → `supported_by=[]` + "ungrounded/纯推测" flag, never a fabricated citation. · `OmicsClaw-KG/omicsclaw_kg/ideation/`, `http_api/routes.py` · ungrounded hunch writes a flagged draft; cannot cite an unread paper.
- `KG-DRAFT-VALIDATE` (be) — at draft, render `candidate_datasets`/`recommended_skills` as "unverified" chips; **validate skill names against the live OmicsClaw catalog** (catch `resolve_skill`'s silent `file_drop`). · `kg_tools.py`, catalog resolver · unknown skill flagged at draft, not at run.
- `KG-HANDOFF-6` (be) — `handoff_hypothesis_to_analysis(slug, notes)`: `recommended_skills` seeds the query but the **run-time Analysis Router is authoritative** (assisted parameterization, shows recommendation). Dataset binding: thread `dataset://` first → permission-gated real-GEO fetch → ask; never auto-fetch a fabricated accession. Requires `thread_id`. · handoff path, Analysis Router · stale skill name recovers via `resolve_capability(claim)` or falls to autonomous, user is told.
- `KG-RECORD-FREESTANDING` (kg+be) — close the loop in v1.5 for the free-standing path: `record_result` appends evidence + **suggests** a verdict; **`hypothesis.status` flips only on explicit human confirm** (change `_record_hypothesis_result`'s current auto-flip to match KG ADR-0003). · `OmicsClaw-KG/omicsclaw_kg/handoff/feedback.py`, FE confirm action · a weak run never auto-refutes; user confirms.
- `KG-IDEATE-TEST-12` + `FE-IDEATEPANEL-4c` (be/fe) — hypothesis cards (claim, grounding/unverified flags, "test this hypothesis", verdict-confirm); Ideate hidden when KG dark. · tests, bench · empty-state copy when auto path has no corpus.

### Phase 7 — v2 Write + record_result + heartbeat + episodic memory
Manuscript object model: [ADR 0022](../adr/0022-bench-write-manuscript-object-model.md). Depends on the
v1 provenance index (`AN-PROV-CAPTURE-13`).

- `WR-MANUSCRIPT-CRUD` (be) — `manuscript://<id>` memory object under `project://<thread_id>` + CRUD routes (`/thread/{id}/manuscript/*`); ordered typed sections `{kind, content, provenance_refs, status}`; provenance as typed graph **edges** to `analysis://` runs / KG Sources; disk = derived exports only. · `memory/`, `server.py` · survives launch-ID change; provenance is edges, not strings.
- `WR-SKILL-DOMAIN` (be) — new `writing` skill domain, **one skill per grounded kind** (outline/methods/results/figure-legends/citations) via the normal skill registry (inherits permission tiers, replot, provenance, disclaimer). Methods generated FROM the `AN-PROV-CAPTURE-13` index (skill + assisted-param decision = reproducible by construction); Results from real artifact values verbatim. · `skills/writing/*` · per-kind generators with distinct evidence sources.
- `WR-INVARIANT` (be) — `enforce_manuscript_invariants` (port of `enforce_interpreted_invariants`): **pre-write + CI grep double-lock**; refs must **resolve** (typed edge to a real run/Source), not just be non-empty; **per-kind** (grounded kinds enforced, discussion/intro/outline exempt). · `skills/writing/_invariants.py`, CI · ungrounded grounded-kind section fails fast; dangling ref rejected.
- `WR-STATUS-STALE` (be+fe) — per-section status machine `{empty→generated→edited}` + orthogonal `stale` bit pinned to the run `version/checksum`; upstream change → `stale` + "regenerate or keep?" prompt; `edited` never silently overwritten; export stamps a "contains stale citations" marker (not blocked). · `memory/`, Write panel · weak-run re-analysis never silently rewrites edited prose nor ships stale numbers.
- `WR-CITATION-2TYPE` (be) — `provenance_refs` tagged union: **bibliographic** (→ KG Source; References; live-linked, re-resolved at export) vs **reproducibility** (→ `analysis://`/figure; inline; version-pinned). · `memory/`, Write panel · opposite freshness; `analysis://` never leaks into References.
- `WR-WRITEPANEL-FE` (fe) — **purpose-built** Write panel bound to the typed manuscript: section list, per-section status badges (incl. `stale`), clickable provenance links, per-section regenerate/keep. NOT the notebook editor. · `src/components/bench/*` · renders the object model natively.
- `WR-EXPORT` (be) — **fast-follow**: BibTeX/CSL/DOCX/LaTeX codegen over stored `SourceFM` fields + a supplementary reproducibility `.ipynb` via `notebook_export.py`. `analysis://` refs must not leak into `.bib`. · `skills/writing/export*` · depends on nothing upstream; build last.
- `KG-RECORD-EXPERIMENT` (be) — the **experiment-DAG** record path (per-step packets + step status). The free-standing `record_result` + verdict-confirm already shipped in v1.5 (`KG-RECORD-FREESTANDING`); v2 adds the multi-step variant with the Workflow runtime as consumer. KG-gated (local `analysis://` persists regardless). · handoff path.
**Heartbeat + episodic memory: [ADR 0024](../adr/0024-bench-heartbeat-episodic-memory.md)** (no
scheduler exists; `insight://`/`project://` are first-written here).

- `V2-HEARTBEAT` (be+fe) — **on-open check, no scheduler**: per-thread `last-heartbeat-date` stamp; on thread-open if date differs, run a `heartbeat` pseudo-stage (read-only tool subset + the one timestamp write, ADR 0020 mechanism) that diffs five on-disk sources (provenance index / open Hypotheses / stale sections / unfinished runs / recent episodic) and returns structured `{notable, proposals[]}`. `notable:false` → silent (stamp only); `true` → briefing with QuickAction proposals; opportunistic lean cross-thread hint. · `memory/`, `runtime/tools/registry.py` (heartbeat subset), bench UI · weak day = no bubble; tick cannot execute a skill.
- `V2-EPISODIC` (be) — episodic daily memory at `project://<thread_id>/daily/<date>`, `episodic`/`volatile`-marked (excluded from normal FTS, decayed in recall); **overwrite** (refine versioning policy: `project://<thread>` versioned, `daily/*` overwrite). **Per-event mechanical spine** piggybacks existing write hooks (`_auto_capture_analysis`); reasoning via explicit "remember this"; narrative generated read-time. · `memory/`, `skill/orchestration.py` · spine is crash-safe; narrative not persisted.
- `V2-DECAY` (be) — read-time 30-day-half-life age multiplier (tunable) applied to `episodic`-marked rows only in recall/search; no GC; `insight://`/`analysis://`/Hypotheses untouched. **First temporal-ranking signal in the engine** — scope strictly to episodic rows. · `memory/search.py`, `memory_engine.py` · a 90-day note ≈ 12.5% weight; global search unchanged.
- `V2-PROMOTE` (be+fe) — promotion to `insight://` is **suggested at read-time** (heartbeat QuickAction or explicit user mark) and **written on human confirm** only; creates an **edge** (insight → source daily/run) + `promoted_date`; never auto. · `memory/`, bench UI · tick never writes `insight://`; a wrong insight is retractable by source.

## 7. Resolved tensions

- **Cross-thread recall (soft grouping vs. default-scoped).** *Passive context injection* is
  thread-scoped by default (only the active thread's lineage auto-enters context). The
  `recall`/`search` **tool** carries a `cross_thread: bool=false` arg the agent can flip when it
  judges a cross-project reference is useful ("did I do this in another study?"). The
  `preference://bench/cross_thread_recall` toggle only controls whether *passive* injection also
  widens — the tool can always widen on demand. This honors ADR 0018 (cross-thread allowed)
  without polluting every prompt.
- **Persona/stage order.** Resolved in ADR 0020 "Authority vs. order": SOUL.md → persona → stage →
  user-append; "under" = lower authority, not earlier text.

## 8. Critical path

`BE-REQ-1 → BE-ENV-2 → BE-LOOP-3 → BE-SKEL-TEST` **‖ S1** `→ AN-CONSENSUS-INTEGRATION →
BE-THREAD-CRUD-2 → BE-RECALL-6 → BE-CTX-7` **‖ S2,S3** `→ BE-STAGEMAP-4 → BE-STAGEFILTER-4b →
BE-STAGEPROMPT-5 → KG-TOOLS-2 → KG-SOFTFAIL-10 → RD-INGEST-9 → AN-ROUTER-10 → AN-CTXRECALL-11 →
BE-PERSONA-7 → BE-ONBOARD-8 → BE-WIRING-TEST-8 → FE-E2E-13`. Frontend (`FE-NAV-1 … FE-ARTIFACTS-9`)
parallelizes against the backend once the chat-request contract (§3) is frozen.

## 9. Open questions

- **Final name** for Bench (研究台 / Workbench / Studio / …) — affects nav label + i18n keys only.
- **Default home**: keep `/chat` as the landing page in v1 (Bench is a peer), decide promotion from
  usage later. (Pre-decided "not aggressive"; confirm at ship.)
