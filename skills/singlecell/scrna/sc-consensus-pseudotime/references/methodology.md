# sc-consensus-pseudotime — methodology

Continuous (rank-gauge) consensus over pseudotime methods (ADR 0031). The
`continuous` Workflow template, bound by `CONSENSUS_SOURCES["sc-consensus-pseudotime"]`.

## Member axis

One member per pseudotime method (`dpt` / `palantir` / `via`), each emitting a
**single global pseudotime**, all sharing one user-specified root
(`--root-cluster` / `--root-cell`). The shared root pins direction so the residual
gauge is monotone-only. Multi-lineage methods (slingshot/monocle3/cellrank) are
deferred — they re-introduce branching topology (out of scope).

## Consensus math (rank-gauge)

A pseudotime is defined only up to a monotone reparameterisation and a direction
flip, so members are made comparable before aggregation:

1. **Rank-normalise** each member's pseudotime to `(avg_rank_1based − 1)/(n − 1) ∈ [0,1]`
   (ties → average rank). Monotone-invariant.
2. **Direction safeguard:** anchor = the member with the highest mean pairwise `|ρ|`
   (deterministic tie-break by name); flip any member with `ρ < 0` vs the anchor.
   Degenerate members (constant / <2 unique / non-finite) are dropped whole.
3. **Agreement:** the symmetric pairwise Spearman matrix; a member's score is its row
   mean (mean ρ vs the others). v1 is agreement-only (α=1, β=0).
4. **Operator:** per-cell `median` (default) or agreement-`weighted` mean (non-negative
   weights; all-zero → median fallback), then **re-ranked** to `[0, 1]`.
5. **Per-cell dispersion:** `pseudotime_mad = clip(2·MAD, 0, 1)` (majority support) plus
   the per-cell `range` (full disagreement companion).
6. **Weak-agreement guard:** if the voters' cohort mean Spearman < 0.5, report + warn
   (the data may have no single shared trajectory); report-only, no members dropped.
