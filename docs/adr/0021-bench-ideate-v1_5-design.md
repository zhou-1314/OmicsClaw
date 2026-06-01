# Bench Ideate (v1.5): design

**Status:** accepted (2026-05-30)

The Ideate stage turns a thread's reading into testable OmicsClaw-KG **Hypothesis** pages and
offers a "test this hypothesis" handoff into Analyze. Six decisions resolve how it sits on the
OmicsClaw-KG ideation engine, which is **thread-blind, batch-oriented, and corpus-structural**
(no `thread_id` anywhere in OmicsClaw-KG; `POST /kg/ideate/hypotheses` drafts from every Question
page in the workspace).

1. **Discrete stage, thread-filter post-hoc.** Ideate stays a discrete stage (ADR 0020) and
   filters drafts to those whose `supported_by` cites ≥1 Source page ingested in the *active*
   thread. A hypothesis that also cites another thread's papers is shown with a
   **cross-study (跨课题) badge** (consistent with ADR 0018's cross-thread-recall-as-feature),
   not hidden. Drafting is idempotent by question slug. Scoping the engine itself (a KG-side
   allow-list parameter) is deferred to v2.

2. **Two origin paths, including a net-new formalize endpoint.** v1.5 ships (a) the existing,
   HTTP-reachable, fully-grounded **auto-from-questions** path, AND (b) a **net-new `formalize`
   path**: the user types a free-text hunch and the LLM grounds it against the thread's existing
   Source pages via the existing closed-list citation validator. The auto path needs corpus
   density (a concept cited by ≥2 sources) and is empty on a fresh thread, so formalize is what
   makes Ideate useful day-1. Formalize is **new work in the OmicsClaw-KG repo**, not wiring.

3. **Soft grounding gate.** A formalize hypothesis with no support in the thread's reading is
   *allowed* but written with `supported_by=[]` and a visible **"ungrounded / 纯推测"** flag —
   never with a fabricated citation. `candidate_datasets` and `recommended_skills` render as
   "unverified" chips, but **skill names are validated against the live OmicsClaw catalog at
   draft time** (because `resolve_skill` silently degrades an unknown skill to `file_drop`,
   failing later and invisibly).

4. **Run-time Router is authoritative for skill + params.** `recommended_skills` is a hint/seed
   only; at handoff the existing Analysis Router re-derives the route at run time (assisted
   parameterization on the thread's real dataset) and shows its recommendation — Analyze never
   authors a new path. Dataset binding: the thread's existing `dataset://` first, else a
   permission-gated fetch of a real `candidate_dataset`, else ask; **never auto-download a
   fabricated accession**.

5. **Single-skill free-standing handoff.** One hypothesis → one packet → the Router (which itself
   chooses Exact / Partial-composition / autonomous). Multi-step needs fall into the Router's
   existing Partial path (untracked). The KG Experiment-DAG path (per-step packets + status) is
   deferred to v2 with the Workflow runtime as its consumer.

6. **Semi-automatic verdict (both paths).** When a result returns via `record_result`, evidence
   is appended and a verdict is *suggested*, but `hypothesis.status` flips only on explicit human
   confirmation. This **changes the current free-standing KG behavior** (`_record_hypothesis_result`
   auto-flips today) to match the experiment-step path's human-confirm rule (KG ADR-0003) —
   because in omics a single underpowered run must not auto-close a research direction. Closing
   the loop (free-standing `record_result` + verdict confirm) moves **into v1.5**; the
   experiment-DAG record path, heartbeat, and episodic memory stay v2.

## Consequences

- v1.5 Ideate touches **all three repos**: a net-new `formalize` endpoint + a behavior change to
  `_record_hypothesis_result` in **OmicsClaw-KG**; KG tool/HTTP wiring + the handoff in the
  **backend**; the hypothesis-card panel in the **frontend**.
- The closed-list citation validator (`hypotheses.py:89-97`) is the load-bearing safety control
  for both origin paths.
