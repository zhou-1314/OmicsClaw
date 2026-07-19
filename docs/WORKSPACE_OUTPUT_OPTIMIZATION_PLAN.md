# Workspace & Output Optimization Plan

> **Historical plan — Desktop Workspace switching is superseded.** The Desktop
> Backend now freezes one Active Workspace at composition-root startup. Request
> headers, chat turns, and in-loop tools cannot change that root, and no
> production path mutates `OMICSCLAW_WORKSPACE` per request. Selecting another
> Desktop Workspace requires an explicit Backend restart; see ADR 0054/0061 and
> the current contract in `docs/CONTEXT.md`. The output-organization proposals
> below remain historical design input and must be revalidated against current
> ADRs before implementation.

> Goal: (1) finish the deferred **multi-workspace KG isolation** so each project's
> knowledge graph is its own store, and (2) make backend **outputs genuinely
> browsable / un-piled**. Both unify under one spine: **"a workspace = the project
> directory you select"** — KG home `‹ws›/.omicsclaw/knowledge`, outputs
> `‹ws›/output/‹project=thread›/‹run›`.

## Decisions (confirmed with user, 2026-06-28)
- **KG isolation: FULL per-workspace** — App injects `X-OmicsClaw-Workspace` on **both** `/kg/*` and `/thread/*`; in-loop KG tools honor it; CLI/Channel/headless keep the env fallback.
- **Output pains to fix:** thread-less runs pile into `default/`; App can't browse by project; autonomous runs are inconsistent/scattered.
- **NOT doing:** a separate `‹root›/‹workspace›/‹project›/` directory tier — the desktop already roots outputs at `‹workspace›/output`, so each workspace owns its own output tree.

## What already exists (do not rebuild)
- **ADR 0035** project-scoped outputs: `run_paths.resolve_run_dir` → `‹output_root›/‹project_dir›/‹skill›__‹ts›__‹dataset›-‹uid8›/`; `project_id == thread_id` (ADR 0018); per-project `project_meta.json` + `index.jsonl`; `/outputs/latest` already supports `?project=`.
- Backend embedded KG router **already reads** `X-OmicsClaw-Workspace` (`server.py:_embedded_kg_config` ~176) and coerces a workspace root → `‹ws›/.omicsclaw/knowledge` (`_coerce_kg_home` ~130). The **receive half is done**; the App **send half** is missing.

## Hard constraints (must not break)
- ADR 0035: run-leaf name FINAL at creation, must keep matching `_RUN_DIR_RE` (mirrored by App `run-link.ts` RUN_DIR_PATTERN); project dir immutable (rename only updates `project_meta.json`); `run_id` globally unique; resolve via index/one-level walk; **no migration of legacy flat runs** (`iter_run_dirs`/`find_run_dir` must keep tolerating them).
- ADR 0018/0019/0025: thread ≠ memory namespace (stays `app/‹user_id›`); KG Source pages stay **cross-study-shared within a workspace** (per-thread grounding remains the memory `thread_source://` links from batch 7); KG scoping grain is **workspace, not thread**.
- ADR 0005: all surfaces share the loop — fixes must degrade for CLI/Channel (env fallback), not be desktop-only.
- Remote mode: the injected workspace path must be valid on the **backend** filesystem (`default_project_dir` is already remote-aware).

---

## Phase 1 — Multi-workspace KG isolation (the deferred item)

**Outcome:** an App project's KG reads/writes hit `‹its workspace›/.omicsclaw/knowledge`; a different project hits a different home; no-header callers keep the global fallback.

| # | Change | File:seam |
|---|---|---|
| 1.1 | Stamp `X-OmicsClaw-Workspace: ‹default_project_dir›` on all KG/thread proxy helpers (server-side `getSetting('default_project_dir')`) | App `src/lib/bench-proxy.ts` `proxyGet`/`proxyJson`/`proxyEmptyPost`/`proxyDelete` (~20/32/56/68) |
| 1.2 | Teach the Bench **write** path to honor the header (today `_resolve_shared_kg_home` is env-only) — add a request-aware resolver used by the `/thread/*` handlers | backend `server.py` `_resolve_shared_kg_home` (~732) + call sites 3293/3321/3354/3395/3442/3467/3514 |
| 1.3 | **Superseded:** do not thread or mutate Workspace per turn. In-loop tools must consume the lifespan-frozen Active Workspace through their owning Runtime/Adapter boundary. | ADR 0054/0061 Active Workspace composition root |
| 1.4 | Preserve the no-header fallback chain (`OMICSCLAW_KG_HOME` → `OMICSCLAW_WORKSPACE` → `DATA_DIR`) | (no change — assert in tests) |

**Tests:** backend — `/thread/sources` (or formalize) with header=A vs B writes into A's vs B's `.omicsclaw/knowledge`; no-header → global fallback. App (node:test) — bench-proxy forwards the header with value = `default_project_dir`; remote-mode value is the backend path.

**Note (half-fix avoided):** the App's Bench UI writes via `/thread/*`, NOT `/kg/*`. A `/kg/*`-only header would isolate only the Explorer. 1.2 is what makes the isolation real.

---

## Phase 2 — Output organization

**Outcome:** thread-less runs are grouped (not one flat `default/`); autonomous runs use the same readable scheme; projects are listable.

| # | Change | File:seam |
|---|---|---|
| 2.1 | Thread-less runs → a **per-day ad-hoc project** `adhoc-‹YYYYMMDD›` instead of literal `default` (keeps the single project level → `iter_run_dirs`/`find_run_dir` unchanged; ADR-0035-safe). Plain `/chat`, Channel, `oc run` w/o `--project` all bucket by day. | `run_paths.resolve_project_dir` (~296-349, empty-id branch ~310-321); callers `agent_executors.py:392-400`, `skill/runner.py:191-202`, `outbox.py:209` pass the derived id |
| 2.2 | Unify autonomous leaf naming to the dataset-bearing scheme (`autonomous__‹ts›__‹dataset›-‹uid8›`), still matching `_RUN_DIR_RE` | `autonomous/workspace.py` `create_workspace` (~40-65), `run_layout.py` |
| 2.3 | New `GET /projects` endpoint wrapping the existing (unexposed) lister → `[{project_id, project_name, run_count, latest_ts}]` | backend `server.py` (new route) + `run_paths.list_projects` (~659) |

**Tests:** thread-less run lands in `adhoc-‹date›` (not flat); two days → two buckets; autonomous leaf matches `_RUN_DIR_RE` + has a dataset token; `GET /projects` lists projects with counts.

---

## Phase 3 — App browse-by-project

**Outcome:** the App can filter outputs by project/thread (not just 本对话/全部).

| # | Change | File:seam |
|---|---|---|
| 3.1 | New `/api/projects` route proxying backend `GET /projects` | App `src/app/api/projects/route.ts` (new) |
| 3.2 | OutputPanel: add a **project** scope (picker from `/api/projects`); forward `?project=` (route already forwards it) | App `OutputPanel.tsx` Scope type (~599), fetch param (~621), tabs (~701-709) |
| 3.3 | Extend the run sidecar to stamp `thread_id`/`project_id` (not just `session_id`) → first-class thread scope + robust chat↔run linkage | backend `_write_session_sidecar`/`_read_session_sidecar` (~5871/5890), call site 1773; App `applyBackendSessionLinks` reads it |

**Tests:** App — project scope sends `?project=`; `/api/projects` proxies + shapes. Backend — sidecar payload includes `thread_id`; `/outputs/latest` row carries it.

---

## Sequencing & method
Phase 1 and 2 are independent; Phase 3 depends on 2.3 (`/projects`). Each phase: **TDD-first → implement → codex (gpt-5.5 xhigh) review → fix → re-review to merge-ready**, same as the audit batches. No commits without explicit instruction.

## Open sub-choices (sensible defaults taken; flag to change)
- Thread-less grain = **per-day** (`adhoc-‹YYYYMMDD›`). Alternative: per chat-session (finer, more dirs). Default chosen: per-day.
- Autonomous prefix = `autonomous` (was `autonomous-code`). Kept short; `_RUN_DIR_RE` must accept it (verify).
- `--output` explicit runs stay un-indexed (out of scope) unless you want them folded in.
