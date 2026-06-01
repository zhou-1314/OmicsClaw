# Bench Write (v2): the manuscript object model

**Status:** accepted (2026-05-30)

The Write stage drafts a **Manuscript** (a child of an investigation thread, ADR 0018) from the
thread's analysis lineage + KG reading knowledge. Seven decisions define the object model; together
they make the grounding contract — *Methods reproducible by construction; Results verbatim; every
grounded claim resolves to a KG Source or an `analysis://` run* — a **machine-checkable graph
invariant** rather than a prompt aspiration.

**0. Provenance substrate must be captured in v1, not v2.** The substrate "Methods by construction"
needs does not exist today: `_auto_capture_analysis` records only `{"input": input_path}`, never
`effective_params`, the **assisted-parameterization decision** (recommended method + accepted/rejected —
which lives only in the agent loop's turn context and is lost at turn end), figure lineage, or the
in-memory `TypedConsensusRun` (`driver.py` never upserts it). We add a **memory-resident provenance
index** per run under `project://<thread_id>` — `{run_id, skill, method, effective_params, artifact
paths, assisted-param decision, version/checksum}` — captured at run time (enrich
`_auto_capture_analysis` + instrument the agent loop's tool-result callback + persist
`TypedConsensusRun`); bulky tables (scores/NMI DataFrames) stay on disk, addressed by path. This
capture ships in **v1's Analyze stage** because the assisted-param decision is ephemeral and
unrecoverable post-hoc. (Rejected: enrich-everything = schema bloat; thin-capture + per-draft disk
walk = fragile, no durable staleness key.)

**1. Memory-canonical, hybrid.** The manuscript is `manuscript://<id>` under `project://<thread_id>`
(authored state → Memory side, ADR 0019). Memory is the single source of truth; disk holds only
derived, regenerable export snapshots (never authoritative; no Word round-trip). Memory-resident lets
provenance be real **graph edges** (machine-checkable) and inherits the versioned Node chain +
thread-scoped recall; a launch-ID change cannot orphan it (`desktop_namespace()` is stable).
(Rejected: file-on-disk = violates ADR 0019, orphaning, loose-string refs.)

**2. Typed section array, one writing skill per kind.** Manuscript = ordered
`{kind, content, provenance_refs, status}` records; `kind ∈ {outline, methods, results,
figure_legends, citations, discussion, …}`; each grounded kind maps to its own `writing` skill with
its own generator + evidence source + invariant (Methods/Results/figure-legends/citations have
genuinely different, asymmetric grounding rules). Mirrors the consensus-interpret
`ClusterAnnotation`/`NextStep` atomic-claim-with-evidence pattern. (Rejected: monolithic write skill =
no per-section regen/status, collapses grounding rules; cells = no atomic section identity.)

**3. Per-section hard grounding invariant; refs must resolve; per-kind.** Port
`enforce_interpreted_invariants` → `enforce_manuscript_invariants`, enforced **pre-write AND
CI grep-locked** (the redundant double-lock is load-bearing — catches degrade/agent/post-hoc paths).
Two hardenings over the ADR 0012 precedent: (a) provenance refs must **resolve** to a real
run/Source node, not merely be non-empty (the precedent's string refs pass on a typo pointing at a
deleted file / wrong number — F1's typed edges fix this); (b) the invariant is **per-kind** — grounded
kinds (methods/results/figure-legends/citations) require resolvable non-empty refs; interpretive kinds
(discussion/intro/outline) are exempt. Sentence-level faithfulness stays a soft metric. (Rejected:
per-sentence hard binding = over-engineers beyond ADR 0012, hostile editing; soft-only = abandons the
contract.)

**4. Per-section status machine; version-pinned; stale-on-change.** `status ∈ {empty → generated →
edited}` plus an orthogonal `stale` bit. A generated section pins the `input_checksum`/version of the
run it was built from (from decision 0's index). When that run changes, the section flips `stale` and
the UI prompts "regenerate or keep?"; an `edited` section is **never silently overwritten** —
regeneration is always confirmed. The only policy satisfying both SOUL.md (no silent edits) and
verbatim reproducibility (staleness surfaced, not hidden). Export with unresolved staleness is allowed
but stamped a visible "contains stale citations" marker (not blocked). (Rejected: living-doc = silent
edit destruction; snapshot = undetected stale numbers.)

**5. Two ref types with opposite freshness.** `provenance_refs` is a tagged union: a **bibliographic**
ref (→ KG Source page; renders in References; **live-linked, re-resolved at export** because Source
pages are mutable) vs a **reproducibility** ref (→ `analysis://` run / figure; grounds Methods/Results
inline; **version-pinned** per decision 4 so the cited number never drifts; never in the bibliography).
A claim may carry both. Only a two-type model can express the opposite freshness policies. (Rejected:
one type inferred from URI = infer-mode-from-URI fragility; bibliography-only = hides reproducibility
provenance.)

**6. Purpose-built Write panel; notebook is an export target only.** A dedicated Bench Write panel
renders the typed sections, per-section status badges (incl. `stale`), clickable provenance links, and
per-section regenerate/keep actions, against new `manuscript://` CRUD routes. The `/notebook` editor's
competencies (live code execution, cell dirty-tracking, .ipynb-on-disk) are a category mismatch for
grounded prose and fight decisions 1/2; `notebook_export.py` is reused only to *generate* a
supplementary reproducibility .ipynb from a finished manuscript. (Rejected: reuse notebook editor =
substrate-vs-surface error; hybrid embed = v2.5+, not first.)

## Consequences

- **v1 gains a provenance-capture task** (Phase 4) even though Write is v2 — the assisted-param
  decision is unrecoverable if not captured at run time.
- Export codegen (BibTeX/CSL/DOCX/LaTeX) is a v2 fast-follow over already-stored SourceFM fields; it
  depends on nothing upstream. BibTeX export must never leak `analysis://` reproducibility refs into
  the `.bib`.
- The grounding invariant is only as strong as ref *resolvability*; the typed-edge model (decision 1)
  is what upgrades it from the precedent's "non-empty string" theatre to a real check.
