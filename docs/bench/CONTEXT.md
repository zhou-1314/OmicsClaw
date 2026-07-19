# Bench — Research Assistant Workspace

The desktop "research assistant" page (working codename **Bench / 研究台**) added to
OmicsClaw-App: a *study-scoped* workspace that carries one research project across the
Read → Ideate → Analyze → Write lifecycle, over the existing agent loop. This context
names the vocabulary specific to that surface. Decisions:
[ADR 0017](../adr/0017-bench-research-continuity-workspace.md),
[0018](../adr/0018-investigation-thread-equals-project.md),
[0019](../adr/0019-kg-first-class-dependency-for-bench.md),
[0053](../adr/0053-make-control-plane-state-authoritative-for-project-conversation-and-turn.md),
[0055](../adr/0055-model-project-lifecycle-as-reversible-archive-and-restore.md),
[0056](../adr/0056-keep-unassigned-runs-outside-project-lifecycle-and-freeze-run-scope.md),
[0057](../adr/0057-persist-minimal-run-lifecycle-receipts-in-control-plane-state.md),
[0058](../adr/0058-bind-retried-run-submissions-to-one-fenced-execution-assignment.md), and
[0059](../adr/0059-store-accepted-inbound-attachments-as-immutable-per-turn-records.md).

> **Scope.** Bench is a **page on the existing Desktop Surface**, not a fourth Surface.
> It reuses `core.llm_tool_loop`, the Desktop SSE/permission plumbing, the Analysis
> Router, the Workflow runtime, and the graph Memory System. Terms for those live in
> [docs/CONTEXT.md](../CONTEXT.md) and
> [runtime/CONTEXT.md](../../omicsclaw/runtime/CONTEXT.md).

## Language

**Bench**:
The research-assistant page/workspace on the Desktop Surface — a study-scoped surface for
the research lifecycle, distinct from the stateless `/chat` task console.
_Avoid_: "Assistant Surface" / "Bot" (Surface is reserved for Channel/Desktop/CLI; Bench
is a page *within* Desktop), "companion" (the persona-companion framing was explicitly
rejected — ADR 0017). The name is a working codename, not final.

**Investigation thread**:
A Bench presentation of exactly one durable **Project** (课题). Its opaque Project ID and
lifecycle come from the authoritative Control Plane State Project Record; its
`project://<id>` Memory subtree stores associated research knowledge. The Project, not
the UI thread row or Memory subtree, is the unit of research continuity.
_Avoid_: "workspace" (already overloaded — Surface dir-as-namespace vs ScopedMemory root,
see docs/CONTEXT.md flagged ambiguities), "session" (the domain term is Conversation),
a separate thread identity, treating `project://` as the Project registry.

**Manuscript**:
A write-target child object of an investigation thread; a thread has 0..n. The Write stage
drafts one named manuscript from the thread's evidence.
_Avoid_: "paper" (ambiguous with an ingested KG **Source** page — a manuscript is
*authored*, a Source is *read*), "document".

**Stage**:
One of the four lifecycle phases of a thread — **Read**, **Ideate**, **Analyze**,
**Write**. Each stage is the same agent loop conditioned by a stage-specific
system-prompt fragment + tool subset + UI panel; it is **not** a separate engine.
_Avoid_: "mode" (the existing ask/plan/autoresearch `ModeIndicator` is a different axis a
stage may reuse), "step" (reserved for skill/pipeline steps).

**Read stage** _(v1)_: ingests a paper (PDF/DOI/PubMed/GEO) via the `literature` skill +
KG ingest and answers questions with KG-cited provenance.

**Ideate stage** _(v1.5)_: turns read material into KG **Hypothesis** pages via KG
`ideate`, each offering a "test this hypothesis" handoff.

**Analyze stage** _(v1)_: runs OmicsClaw skills bound to the thread through the existing
Analysis Router / Workflow runtime. Each admitted Run freezes
`ProjectScope(project_id)` from the investigation thread's active Project and receives a
caller-generated opaque Run Submission ID, a control-generated Run ID, minimal Run
Receipt and at most one Assignment-ID-fenced executor start. Re-delivery of the same
Bench action returns that Run rather than starting another; its scientific Manifest and
artifacts roll up to `project://<id>` and write back to KG via `record_result`. Bench
never substitutes its UI row, display name, output directory or remote Job ID for
Project, submission, Run or Assignment identity.

**Write stage** _(v2)_: drafts a **Manuscript** from the thread's `analysis://` lineage +
KG evidence subgraph via a new `writing` skill domain. Methods text is generated from the
recorded skill names + assisted-parameterization params (reproducible by construction).

**Research-stance persona**:
A thin, additive system-prompt fragment expressing Bench's research tone/stance, layered
over `core://agent` (SOUL.md). It shapes tone only; it cannot override SOUL.md's
non-negotiable safety rules.
_Avoid_: "soul" (that's the existing SOUL.md / `core://agent`), "personality file" (there
is no parallel markdown store — ADR 0017).

**Thread-scoped recall**:
The default memory behaviour inside a thread: `recall`/`search` prefer the active thread's
`project://<project_id>` grouping, but may surface cross-Project results when useful
(cross-Project method transfer is a feature, not a leak). A Project is a soft Memory
grouping referenced by its canonical Project ID, never a Memory Namespace; legacy
`app/<user_id>` or transport-derived Namespace values are implementation drift, not the
accepted state-ownership model.
_Avoid_: "thread isolation" (implies a hard namespace boundary, which we rejected — ADR 0018).

**Control/knowledge boundary**:
The rule that authoritative **Control Plane State** owns Project, Conversation, Turn and
Run Receipt identity/lifecycle; Run storage owns scientific Manifests/artifacts; the graph
**Memory System** owns agent/Project knowledge (`core://`,
`project://`, `analysis://`, `insight://`, `preference://`); and **OmicsClaw-KG** owns
cross-study scientific reading knowledge (Source/Entity/Concept/Method/Hypothesis pages).
These are distinct logical owners even if a future deployment shares physical storage.
_Avoid_: calling Memory the Project registry, using "the knowledge base" as a synonym for
all three owners, letting a KG or Memory write create a Project.

## Relationships

- A **Bench** presents many **Investigation threads**; the Owner works on one at a time.
- An **Investigation thread** presents exactly one existing Control Plane State **Project
  Record**. Its `project://<id>` subtree, KG **Source** and **Hypothesis** pages,
  `analysis://` Runs and `insight://` notes are associated knowledge or execution records,
  not alternative Project registries.
- An archived **Investigation thread** still presents the same Project and remains
  inspectable and restorable, but its Read/Ideate/Analyze/Write actions cannot create new
  scientific work until the Project returns to `active`.
- Analyze-stage Run activity is determined by authoritative **Run Receipts**, not by
  directory mtimes, Remote Job rows or `analysis://` projections; an accepted
  non-terminal Project Run makes archive return `project_busy`.
- A Bench Analyze action creates one Run Submission ID at its user-visible operation
  boundary. UI or transport retry reuses it and returns the existing Run; observing a
  Run never assigns or starts it.
- An **Investigation thread** has 0..n **Manuscripts**; a **Manuscript** belongs to
  exactly one thread.
- A **Stage** is the shared agent loop conditioned by a fragment + tool subset; switching
  stage does not switch engine.
- **Read** and **Ideate** depend on **OmicsClaw-KG** (first-class dependency, ADR 0019);
  **Analyze** runs without it.
- **Bench** is the same engine as `/chat` at a different zoom level (study vs. task); the
  two bridge bidirectionally.

## Example dialogue

> **Dev:** "The Owner reads two papers on glioma, then runs sc-de on their own data. One thread
> or two?"
> **Architect:** "One **Project**, presented as one **Investigation thread** (the glioma 课题). Both papers become KG
> **Source** pages under it; the sc-de run is an `analysis://` node rolled up to the same
> `project://<id>`. If the glioma study later splits into two journal submissions, that's
> still one thread with two **Manuscripts**."
>
> **Dev:** "The Owner removes that thread from the active Bench list. Should we delete its
> analyses and start a new Project if they reopen it?"
> **Architect:** "No. That action is Project archive, not deletion. Archived Bench views
> retain the same Project ID, Conversations and evidence; restore reopens that exact
> Project."

## Scope sequencing (roadmap, not an ADR)

- **v1**: Project binding (opaque Project ID plumbed frontend→backend and validated against
  Control Plane State) + **Read** + **Analyze**
  + a light, skippable first-run onboarding that seeds `core://my_user` (domains, organism,
  platforms, target venue).
- **v1.5**: **Ideate** (hypothesis cards + "test this hypothesis" handoff).
- **v2**: **Write** (`writing` skill domain), proactive read-only heartbeat, episodic daily
  memory promotion.

### Read-stage data flow (v1)

"Reading a paper" produces two complementary outputs from two non-overlapping tools:
- **KG `ingest`** (LLM extraction) → durable **Source/Entity/Method/Concept** pages — the
  *interpretation* substrate; every Read-stage answer cites these, never hallucination.
- **`literature` skill** (regex/heuristic, no LLM) → GEO accessions + dataset metadata, with
  an optional permission-gated download that registers the dataset as a `dataset://` under
  the thread — the *data-acquisition* bridge into Analyze.
KG ingest always runs; the dataset download is offered, not automatic (network + disk).

### Ideate-stage data flow (v1.5) — see [ADR 0021](../adr/0021-bench-ideate-v1_5-design.md)

The KG ideation engine is **thread-blind + batch** (drafts from every Question page in the
workspace). Ideate therefore:
- **Two origins**: (a) auto-from-questions (needs corpus density: a concept cited by ≥2 sources —
  empty on a fresh thread), and (b) a **net-new formalize** path (free-text hunch → grounded
  Hypothesis against the thread's Source pages via the closed-list validator).
- **Thread-filter post-hoc**: only drafts whose `supported_by` cites ≥1 of the thread's Source
  pages surface; cross-thread-citing drafts get a **cross-study (跨课题) badge**, not hidden.
- **Soft grounding gate**: an ungrounded formalize hypothesis is allowed but flagged
  "纯推测" with `supported_by=[]` — never a fabricated citation. Skill names are catalog-checked
  at draft time.
- **"Test this hypothesis"** = a single-skill free-standing handoff; the **run-time Analysis
  Router is authoritative** (`recommended_skills` is only a seed). Verdict on `record_result` is
  **suggested**, and `status` flips only on human confirm (a weak run never auto-refutes).
Multi-step experiment DAGs and the proactive heartbeat are v2.

### Write-stage manuscript object model (v2) — see [ADR 0022](../adr/0022-bench-write-manuscript-object-model.md)

A **Manuscript** is `manuscript://<id>` under `project://<thread_id>` (memory-canonical; disk holds
derived exports only) — an ordered array of typed sections `{kind, content, provenance_refs, status}`,
one `writing` skill per grounded kind. The grounding contract is a **machine-checkable graph
invariant** (`enforce_manuscript_invariants`, pre-write + CI double-lock): grounded kinds
(methods/results/figure-legends/citations) need **resolvable** provenance refs; interpretive kinds
(discussion/intro/outline) are exempt. `provenance_refs` is a tagged union with **opposite freshness**:
*bibliographic* (→ KG Source, live-linked, becomes References) vs *reproducibility* (→ `analysis://`
run/figure, version-pinned, grounds Methods/Results inline). A per-section `status` machine
(`empty→generated→edited` + `stale`) pins the source-run version, so a re-run flags sections stale and
prompts "regenerate or keep?" — never a silent overwrite. **Prerequisite captured in v1**: the
provenance index (`AN-PROV-CAPTURE-13`), because the assisted-parameterization decision is ephemeral.

### Heartbeat + episodic memory (v2) — see [ADR 0034](../adr/0034-bench-heartbeat-episodic-memory.md)

The proactive layer is minimal and read-only (continuity, not companion). The **heartbeat** is an
*on-open check* (no scheduler): on thread-open, a `heartbeat` pseudo-stage (read-only tool subset + one
timestamp write) diffs five on-disk sources and returns `{notable, proposals[]}` — silent when nothing
notable, else a briefing of QuickAction proposals; per-thread with opportunistic cross-thread hints.
**Episodic daily memory** lives at `project://<thread_id>/daily/<date>` (graph, `episodic`-marked,
overwrite): a per-event mechanical *spine* (piggybacks existing write hooks), reasoning via explicit
"remember this", narrative generated at read time. A read-time 30-day-half-life decay down-weights only
`episodic` rows (never `insight://`); **promotion** to durable `insight://` is suggested at read time but
written only on human confirm, as an edge (insight → source) + `promoted_date`. Bench is the first writer
of `insight://` and `project://<thread>/daily/*`.

## Flagged ambiguities

- "workspace" — do NOT use for an investigation thread; it collides with the existing
  Surface/ScopedMemory senses. Use "investigation thread" / "thread".
- "delete thread" previously meant hiding a legacy `ThreadMemory` row while retaining
  content. Use **archive Project** in product language; permanent data purge is outside
  Bench v1.
- "Bench" is a working codename; final naming is an open question.
