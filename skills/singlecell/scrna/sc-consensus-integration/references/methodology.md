# sc-consensus-integration — Methodology

This skill is a **workflow shim** (ADR 0016/0030): `sc_consensus_integration.py`
binds the flavour name and delegates to the shared consensus runtime
`omicsclaw.runtime.consensus.run` (`--source sc-consensus-integration`).

## Why consensus over integration representations

For multi-sample scRNA, the dominant axis of variation is not clustering
resolution but **how batch effect is removed**. Clustering uncorrected PCA of
multi-sample data clusters *batches*, not cell types; different integration
methods (Harmony / Scanorama / scVI) yield different embeddings and so different
clusterings. The scientifically meaningful question is: *which cell populations
are stable across integration methods, and which are batch artifacts?* This
flavour answers it by fanning out integration **representations** as members.

## The pipeline (delegated to the runtime)

1. **Plan members** — `--integration-methods` set: `none` (unintegrated `X_pca`
   baseline, exposes batch artifacts) + `harmony` + `scanorama` by default;
   `scvi` via `--include-scvi`.
2. **Fan out** — each member runs `sc-integrate-cluster --method <m>` — a
   self-contained *integrate + cluster* unit — at a **fixed** `--resolution` so
   member cluster counts stay comparable for the operator.
3. **Score — integration intrinsic panel (ADR 0029)** — the driver replaces the
   reader's per-member intrinsic with a batch-mixing panel computed on each
   member's own embedding + the batch key (no ground-truth cell types needed):
   - `ilisi_norm` (weight 0.5) — iLISI batch-neighbourhood diversity,
     `(iLISI−1)/(n_batches−1)`; higher = better mixing.
   - `knn_preservation_norm` (weight 0.5) — fraction of each cell's within-batch
     `X_pca` neighbours retained in the integrated embedding; a direct
     over-integration probe.
   - `batch_asw_norm` / `cluster_asw_norm` — diagnostics, reported at weight 0.
   Balancing mixing against within-batch structure penalises both over- and
   under-integration. **Weights are experimental** (recorded in `plan.json`),
   not empirically calibrated.
4. **Consensus** — `kmode` / `weighted` / `lca` over the surviving members.
5. **Report** — `[A: Verified consensus]` banner + per-cell support/entropy + a
   k-divergence section.

## Caveats

- The consensus is a *reproducible computational estimate, not experimental
  ground truth*; scVI is stochastic (reproducible within tolerance).
- The panel is unsupervised batch-mixing-vs-structure — NOT validated against
  curated cell types; treat the score as a relative ranking.

See `references/parameters.md` for every flag and `references/output_contract.md`
for the artifact schema. Scoring is ADR 0011; the panel is ADR 0029; the workflow
runtime is ADR 0016.
