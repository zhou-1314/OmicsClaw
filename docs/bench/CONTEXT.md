# Bench — Research Assistant Workspace

The desktop "research assistant" page (working codename **Bench / 研究台**) added to
OmicsClaw-App: a *study-scoped* workspace that carries one research project across the
Read → Ideate → Analyze → Write lifecycle, over the existing agent loop. This context
names the vocabulary specific to that surface. Decisions:
[ADR 0017](../adr/0017-bench-research-continuity-workspace.md),
[0018](../adr/0018-investigation-thread-equals-project.md),
[0019](../adr/0019-kg-first-class-dependency-for-bench.md).

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
A durable Bench scope for exactly one research project (课题), backed by a `project://<id>`
memory subtree. The unit of research continuity.
_Avoid_: "workspace" (already overloaded — Surface dir-as-namespace vs ScopedMemory root,
see docs/CONTEXT.md flagged ambiguities), "session" (`Session` is the chat-turn
participant), bare "project" (that's the `project://` memory **domain** the thread is
*backed by*, not the thread itself).

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
Analysis Router / Workflow runtime; results roll up to `project://<id>` and write back to
KG via `record_result`.

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
`project://<thread_id>` grouping, but may surface cross-thread results when useful
(cross-project method transfer is a feature, not a leak). A thread is a soft grouping in the
stable `app/<user_id>` namespace, never its own namespace.
_Avoid_: "thread isolation" (implies a hard namespace boundary, which we rejected — ADR 0018).

**Two-store boundary**:
The rule that the graph **Memory System** owns agent/study *state* (`core://`,
`project://`, `analysis://`, `insight://`, `preference://`, `session://`), while
**OmicsClaw-KG** owns cross-study scientific *reading knowledge*
(Source/Entity/Concept/Method/Hypothesis pages). Not merged.
_Avoid_: using "the knowledge base" as a synonym for memory — they are two distinct stores.

## Relationships

- A **Bench** holds many **Investigation threads**; the user works one thread at a time.
- An **Investigation thread** is backed by exactly one `project://<id>` subtree and rolls
  up its KG **Source** pages, **Hypothesis** pages, `analysis://` runs and `insight://`
  notes.
- An **Investigation thread** has 0..n **Manuscripts**; a **Manuscript** belongs to
  exactly one thread.
- A **Stage** is the shared agent loop conditioned by a fragment + tool subset; switching
  stage does not switch engine.
- **Read** and **Ideate** depend on **OmicsClaw-KG** (first-class dependency, ADR 0019);
  **Analyze** runs without it.
- **Bench** is the same engine as `/chat` at a different zoom level (study vs. task); the
  two bridge bidirectionally.

## Example dialogue

> **Dev:** "User reads two papers on glioma, then runs sc-de on their own data. One thread
> or two?"
> **Architect:** "One **Investigation thread** (the glioma 课题). Both papers become KG
> **Source** pages under it; the sc-de run is an `analysis://` node rolled up to the same
> `project://<id>`. If the glioma study later splits into two journal submissions, that's
> still one thread with two **Manuscripts**."

## Scope sequencing (roadmap, not an ADR)

- **v1**: Thread binding (`project://<id>` plumbed frontend→backend) + **Read** + **Analyze**
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
- "Bench" is a working codename; final naming is an open question.
