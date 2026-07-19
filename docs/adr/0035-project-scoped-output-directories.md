# Project-scoped output directories with a rebuildable run index

**Status:** accepted with required implementation constraints (2026-06-24). Reviewed by
Codex (gpt-5.5, 2026-06-24); the constraints in §"Required implementation constraints"
below incorporate that review — they are part of the decision, not optional polish.

**Project-authority refinement (2026-07-14):**
[ADR 0053](0053-make-control-plane-state-authoritative-for-project-conversation-and-turn.md)
keeps the Project-scoped output layout and `project_meta.json` as the durable
Project-to-directory mapping inside the output subsystem. The file may mirror
display metadata but no longer determines Project existence, current name or
lifecycle; those facts belong to Control Plane State. The literal `default`
directory remains a Run grouping, while whether it should have a Project Record
is explicitly unresolved by ADR 0053; the older "default project" wording below
does not decide that domain status.

**Run-scope refinement (2026-07-14):**
[ADR 0056](0056-keep-unassigned-runs-outside-project-lifecycle-and-freeze-run-scope.md)
resolves that status. `default/` is the non-Project **Unassigned Run Grouping**,
not a Project ID or Project Record. Every new Run freezes either
`ProjectScope(project_id)` or `UnassignedScope` at admission, and v1 does not
move, retag, copy or symlink an existing Run into another scope. Historical
"default project" and mutable `project_id` wording below is superseded.

**Run-identity and lifecycle refinement (2026-07-14):**
[ADR 0057](0057-persist-minimal-run-lifecycle-receipts-in-control-plane-state.md)
supersedes this ADR's use of the Run-directory leaf as canonical Run ID and its
claim that directories/Manifests are the sole truth for all Run facts. The
control plane now generates an opaque Run ID and persists the minimal Run
Receipt for acceptance, Scope and operational lifecycle; the readable directory
leaf is a storage name, while the Manifest remains scientific-provenance truth
and `index.jsonl` remains a rebuildable projection. Historical layout details
below are retained as implementation history.

## Context

OmicsClaw writes every analysis Run into a flat `output/<skill>__<method>__<ts>__<uuid8>/`
pile under one output root, and four Surfaces resolve that path slightly differently:
CLI (`omicsclaw.py` / `omicsclaw/skill/runner.py`), the agent loop
(`omicsclaw/runtime/tools/builders/agent_executors.py`), Channel
(`omicsclaw/skill/chain.py`), and Desktop (`omicsclaw/surfaces/desktop/server.py`).
Once a researcher has run dozens of analyses the directory is unnavigable: the
`uuid8` suffix is opaque, nothing groups runs by study or dataset, and the only
registries that *do* group runs — the graph-memory `analysis://<thread_id>/typed/<run_id>`
records (ADR 0010 / 0018) and the desktop SQLite `run_meta` — are opt-in (memory) or
desktop-only (`run_meta`) and never reshape the filesystem.

The reference project **cellclaw** groups outputs by `workspace/<project_id>/results/`
plus a database run registry. Its filesystem grouping-by-project is worth borrowing;
its `results/` *naming* (each skill invents its own subdir, no convention) is the very
confusion we already have and is not. We already own the registry half (memory +
`run_meta`).

ADR 0018 already established **Project = Bench investigation thread**, persisted at
`project://<thread_id>`, rolling up a study's `analysis://` runs; ADR 0023 (decision 3)
already threads `thread_id` from the Desktop request through `runtime/agent/loop.py`
into `agent_executors.py`. Today that `thread_id` scopes *memory* but not the *output
path* — that gap is the whole bug.

## Decision

Project the existing Project concept onto the filesystem. A Run is written to:

```
<output root>/<project dir>/<skill>[__<method>]__<YYYYMMDD_HHMMSS>__<dataset-slug>-<uid8>/
```

- **Grouping axis = Project.** The on-disk Project is the **same identity** as
  `project://<project_id>` (ADR 0018) — one Project, two projections — with the
  canonical `project_id` recorded in a per-project `project_meta.json`. The Project
  **directory name** is a readable `<name-slug>__<short-id>` (not the opaque
  `uuid.uuid4().hex` `project_id`), so the project layer itself stays human-navigable.
  The `short-id` is **deterministic** — `hash(project_id)[:10]`, not a fresh random id —
  so a Project's directory is reproducible from its `project_id`; the authoritative
  `project_id`↔directory mapping comes from scanning `project_meta.json`, never from
  parsing the directory name. The directory name is **frozen at creation** (a later
  `ThreadMemory` rename updates only `project_meta.json` + memory, never the folder — see
  constraints).
- **Non-Bench fallback = a literal `default` project.** CLI, stateless `/chat`, and
  Channel Runs with no bound thread land in `<output root>/default/`. This reuses the
  `thread_id` already on the wire; `""` → `default`. The filesystem analogue of memory's
  no-thread `analysis://typed/*` convention.
- **Within a Project: flat Runs, no second directory level.** Dataset / date / method /
  session become manifest fields and filters, not directory tiers (cellclaw's lesson: let
  the registry slice, not the tree). The **Run directory** leaf keeps the frontend run-dir
  pattern (`^[^/]*__\d{8}_\d{6}__[^/]+$`, `run-link.ts`), and its trailing token is
  `<dataset-slug>-<uid8>`: the **input dataset slug** makes the Run identifiable from the
  folder name alone, while the retained `uid8` keeps the `run_id` **globally unique**. That
  uniqueness is load-bearing — the desktop `run_meta` table keys on `run_id` and the file
  API resolves a bare `run_id`, so a pure-dataset token (two runs of the same skill on the
  same dataset in the same second) would silently collide. `run_id` stays globally unique,
  so no `run_key` migration is needed.
- **Source of truth = the Run directories + their `manifest.json`,** extended with
  `project_id` / `run_id` / `dataset` / `status` (reusing `omicsclaw/common/manifest.py`,
  which already writes a per-Run `manifest.json`). A per-project **`index.jsonl`** is a
  *rebuildable cache* for fast listing; the memory graph and `run_meta` are **reconciled
  views** keyed by `run_id`, not competing registries — any of them can be rebuilt by
  walking (constraint 8).
- **One resolver.** All four Surfaces converge on a single `resolve_run_dir(...)` that
  resolves the Project directory, builds the Run name, dedups, writes `project_meta.json`,
  and appends `index.jsonl`. Consolidating here also lets us finally implement CLAUDE.md's
  "warn before overwriting" rule in one place.

This keeps output-shape parity with Autonomous Code Runner runs (ADR 0013 / 0032), which
already emit `manifest.json` + `completion_report.json` under
`autonomous-code__<ts>__<id>`; autonomous runs become one more Run kind nested under a
Project rather than a parallel scheme.

## Required implementation constraints

The flat-`uuid8` layout silently provides four properties — global `run_id` uniqueness,
race-free directory creation, a stable post-run path, and trivial `run_id`→path lookup.
The Project layout must preserve all four explicitly or it regresses correctness while
improving readability. These constraints are part of the decision:

1. **`run_id` stays globally unique.** The Run leaf is `…__<ts>__<dataset-slug>-<uid8>`;
   the `uid8` is mandatory. The desktop `run_meta` table keys on `run_id`
   (`OmicsClaw-App/src/lib/db.ts`) and the file API takes a bare `run_id`, so dropping the
   uid would collide and overwrite. No `run_key` is introduced.
2. **Atomic Run-directory creation.** The resolver reserves the directory with
   `mkdir(exist_ok=False)` and, on `FileExistsError`, regenerates the uid / appends `_N` and
   retries — never the current `exists()`-then-`mkdir` check (`output_finalize.py`
   `deduplicate_path`), which is a TOCTOU under agent fan-out.
3. **The resolver names the final directory up front; no post-run rename.** The current
   runner renames the output dir post-hoc once the method is known
   (`output_finalize.py`); that invalidates any `manifest.json` / `index.jsonl` entry the
   resolver already wrote. The resolver picks the final name before execution (requested
   method, else `method-unknown`); the actual method is recorded in `manifest.json` only.
4. **`run_id`→path resolution goes through the index, never path concatenation.**
   `/outputs/{run_id}/files` currently does `output_dir / run_id`
   (`surfaces/desktop/server.py`) and `/outputs/latest` iterates top-level dirs; both must
   descend one Project level and look the `run_id` up in the index (or a one-level walk).
5. **`index.jsonl` is a lock-protected, rebuildable cache.** Each line is a single
   `O_APPEND`/locked write carrying `schema_version`, `project_id`, `run_id`,
   `manifest_mtime`, `path_rel`, `status`. A reader that sees a missing file, a corrupt /
   half-written line, or an `mtime` mismatch rebuilds from a Project walk (`oc outputs
   reindex`, or lazily inside `/outputs/latest`). The Run directories + `manifest.json` are
   the only source of truth.
6. **Project directory is immutable after creation.** A `ThreadMemory` rename updates
   `project_meta.json.display_name` + memory only; it never moves the folder (moving would
   dangle `AnalysisMemory.output_path`, frontend caches, and any user bookmarks). The
   creation-time `name-slug` may therefore go stale on disk — acceptable, because the
   authoritative display name lives in `project_meta.json`, not the folder name.
7. **Dataset-slug resolution has an explicit fallback ladder.** `slugify_output_token`
   keeps only `[a-z0-9]` and collapses anything else to `default` (`common/report.py`), so
   a pipeline input (an upstream *output*, not the raw dataset), a multi-input or
   parameter-only skill, a demo, or a non-ASCII name needs a defined token:
   manifest-lineage root dataset → explicit input basename → upstream-run dataset →
   `multi-<hash>` → `demo` → `params-<hash>` → `unknown`. The slug is length-capped (≈48);
   the full dataset name lives in `manifest.json`.
8. **`manifest.json` is the sole write-truth; memory / `run_meta` are reconciled views.**
   `_auto_capture_analysis` today writes an absolute `output_path` into memory
   (`skill/orchestration.py`). Views may cache `output_path`, but they store `project_id` /
   `run_id` as the durable key and a `reindex` repairs any stale cached path; nothing but
   the manifest/index defines where a Run lives.
9. **Autonomous runs nest, nested skill-calls do not.** A top-level Autonomous run becomes
   one Run in the Project index; the skill-facade sub-calls it writes under its workspace
   (`autonomous/workspace.py`) are recorded as provenance with a `parent_run_id`, **not** as
   top-level Runs, to avoid double-counting.
10. **Path-safety is asserted, not assumed.** The resolver joins exactly one slugified
    segment per level, then `resolve()`s and asserts the result is under the output root,
    and refuses a pre-existing symlinked Project/Run directory. (Slugifying already strips
    `/` and `.`, so `../` traversal is blocked; the assert + symlink check are defence in
    depth, and the backend keeps its own containment check rather than trusting the
    frontend allowlist.)

## Considered options

- **Group by session / conversation** — matches `run_meta.session_id`, but sessions are
  ephemeral and ADR 0017 makes one study span many sessions; rejected.
- **Group by dataset** — intuitive ("everything done to sample_A"), but pipeline inputs
  are usually upstream *outputs*, not the raw dataset, so the axis blurs; kept as a
  manifest field and the Run-name token instead of a directory level.
- **Index-only, no filesystem restructure** — lowest risk, but leaves CLI / filesystem
  users facing the same flat pile; rejected as not solving the stated pain.
- **Memory graph as the sole source of truth** — reuses existing plumbing but requires
  memory enabled + a DB and is not disk-browsable; rejected in favor of disk-as-truth with
  memory as a view.
- **Project dir named by the raw `project_id` hex** — cleanest id↔dir mapping but
  re-introduces opaque folders one level up; rejected for the readable
  `name-slug__short-id`, with the id kept in `project_meta.json`.

## Consequences

- The `thread_id`→output-path wiring is small (the value is already in
  `agent_executors.py`); converging the four Surfaces on one resolver is the bulk of the
  work and the main risk surface.
- Backend `/outputs/latest` must walk one level deeper (`<root>/<project>/<run>/`) and
  gains `project_id` / `project_name` per run plus a `?project=` filter; **and
  `/outputs/{run_id}/files` must stop concatenating `output_dir / run_id`** (verified
  `surfaces/desktop/server.py`) and resolve via the index instead (constraint 4). The flat
  `runs[]` shape and the `run_id` regex are **unchanged**, so the desktop frontend keeps
  working and gains Project as an additive grouping dimension. `run_meta` (keyed by the
  still-globally-unique `run_id`) and the file-serve allowlist (keyed by the output root)
  are unaffected.
- **No migration.** The pre-existing flat `output/*` directories are disposable test data
  (owner's call, 2026-06-24); this is a clean cut-over. The listing walk may tolerate a
  root-level run dir as `default` for safety, but nothing is moved by contract.
- **Open:** Project *rename* is settled (constraint 6: never move the folder), but
  *reassigning* a `default` Run to a real Project later is not. Moving its directory dangles
  `AnalysisMemory.output_path`; re-tagging it in the index leaves the disk projection under
  `default` while the logical Project differs. v1 may not support reassignment. Tracked in
  `docs/CONTEXT.md` "Open questions".

Vocabulary for this decision (Run, Project output directory, `default` project, Run
directory, project_meta.json, Run index) is defined in `docs/CONTEXT.md` §"Run & output
organization".
