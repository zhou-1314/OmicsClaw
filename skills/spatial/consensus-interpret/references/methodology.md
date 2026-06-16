# consensus-interpret — Methodology

A strictly **downstream consumer** (leaf skill) of a verified typed consensus
run. It does NOT refine the consensus; it interprets it (ADR 0012 γ + β). The
verified-vs-interpreted boundary from ADR 0010 is preserved: output lands under
`analysis://interpreted/<run_id>`, never overwriting `analysis://typed/<run_id>`.

## The two grounded LLM tasks

- **γ (naming)** — for each consensus cluster, name the likely cell type, with
  **mandatory marker citations**: `evidence.markers[]` must be non-empty and each
  entry must carry `{gene, db_source, db_celltype}`. Ungrounded output is rejected
  (exit 7), not kept.
- **β (recommendation)** — recommend the top-3 next-step skills, each with
  **mandatory `evidence_refs[]`** pointing at specific typed-run evidence
  (e.g. a `cross_method_nmi.csv` cell). No evidence → exit 7.

## Why DE is inline (not a subprocess)

Per-cluster differential expression is computed inline with
`scanpy.tl.rank_genes_groups`; `spatial-de` is deliberately NOT invoked as a
subprocess (ADR 0012 rejected alternatives §4) — interpretation must be
self-contained and deterministic up to the LLM calls.

## Grounding pipeline (deterministic before the LLM)

1. Preflight (T1) — validate the typed run, locate adata, resolve the marker DB.
2. Per-cluster DE (deterministic) → top-K markers.
3. Marker → cell-type candidates (deterministic) — rank DB entries by
   `db.weight × 1/de_rank` over the cluster's top-K markers.
4. LLM γ per cluster + one β synthesis call, each constrained to the
   `interpreted_assignments.json` schema.
5. Invariant enforcement (T3) — marker grounding + evidence refs + banner.
6. Coverage check (T2) — escalate if interpretable fraction < `--coverage-floor`.

## Degrade modes

- `--no-llm` → structural-only; banner `[I-noLLM: ...]`; no cell-type claims.
- `--markers <tsv>` → bypass the bundled tissue DB with a custom one.

See `references/parameters.md` for every flag, `references/output_contract.md` for
the artifact schema, and ADR 0012 for the 4-axis evaluation + T3 invariants.
