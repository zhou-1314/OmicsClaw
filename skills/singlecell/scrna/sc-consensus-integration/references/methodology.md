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
   - `ilisi_norm` (**weight 1.0** — the single scored axis) — iLISI
     batch-neighbourhood diversity, `log(iLISI)/log(n_batches)`; higher = better
     mixing. The log map (vs linear) keeps real-world iLISI (which sits near 1)
     from compressing into the bottom of `[0,1]` (B3).
   - `knn_preservation_norm`, `batch_asw_norm`, `cluster_asw_norm` — **weight-0
     diagnostics**, reported but not scored.
   On panc8 (ground-truth cell types) `ilisi` correlated with cell-type recovery
   (`r=+0.99`) while `knn_preservation` **anti**-correlated (`r=-0.74`), so the
   panel scores on `ilisi` alone and reports `knn_preservation` to flag
   over-integration (B1, ADR 0029 Amendment). **The weight is experimental**
   (recorded in `plan.json`), validated on one real dataset, not yet calibrated
   across several.
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
