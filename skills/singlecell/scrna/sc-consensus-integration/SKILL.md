---
name: sc-consensus-integration
description: Verified consensus over batch-correction representations — fans out integration methods (Harmony/Scanorama/scVI + unintegrated baseline), clusters each, and votes a consensus scored by a batch-mixing intrinsic panel. Use for robust multi-sample single-cell clustering that is not an artifact of one integration method.
version: 0.1.0
author: OmicsClaw
license: MIT
tags:
- singlecell
- scrna
- consensus
- integration
- batch-correction
---

# sc-consensus-integration

**Domain:** Single-Cell Omics (scRNA) · **Role:** verified-consensus flavour (A path)

Verified consensus over **batch-correction representations**. For multi-sample
single-cell data, the dominant axis of variation is not the clustering
resolution but how batch effect is removed: clustering uncorrected PCA of
multi-sample data clusters *batches*, not cell types, and different integration
methods (Harmony / Scanorama / scVI / …) yield different embeddings and so
different clusterings. This flavour asks the scientifically meaningful question:
**which cell populations are stable across integration methods, and which are
batch artifacts?**

It mirrors `consensus-domains` (which fans out genuinely different spatial
algorithms): here members fan out `sc-integrate-cluster --method <m>` — each a
self-contained *integrate + cluster* unit — at a **fixed** resolution (so member
cluster counts stay comparable for the operator), and the runtime scores them
with the **integration intrinsic panel** (ADR 0029) before voting a consensus.

## When to use

- A preprocessed multi-sample AnnData with a batch key in `obs` (≥2 batches).
- You want a robust clustering that is not an artifact of one integration
  method, with per-cell confidence and a flag for batch-artifact clusters.

## CLI

```bash
# default members: unintegrated (X_pca baseline) + harmony + scanorama
python omicsclaw.py run sc-consensus-integration \
  --input <preprocessed.h5ad> --output <dir> \
  --batch-key batch --resolution 1.0 --operator kmode --seed 0 --non-interactive

# add the GPU/stochastic scVI member (serialise GPU members)
python omicsclaw.py run sc-consensus-integration --input <h5ad> --output <dir> \
  --include-scvi --max-parallel 1 --non-interactive

# explicit method set
python omicsclaw.py run sc-consensus-integration --input <h5ad> --output <dir> \
  --integration-methods harmony,scanorama,scvi --non-interactive
```

## Members (`--integration-methods`)

`none` (unintegrated `X_pca` baseline — exposes batch-artifact clusters),
`harmony`, `scanorama` (default set); `scvi` via `--include-scvi`. Each member
runs the `sc-integrate-cluster` skill.

## Intrinsic panel (ADR 0029)

The reader's per-member intrinsic is replaced by a driver-computed batch-mixing
panel, scored on each member's own embedding + the batch key (no ground-truth
cell types needed):

- **`ilisi_norm`** (weight 0.5) — iLISI batch-neighbourhood diversity,
  `(iLISI−1)/(n_batches−1)`. Higher = better mixing.
- **`knn_preservation_norm`** (weight 0.5) — fraction of each cell's *within-batch*
  `X_pca` neighbours retained in the integrated embedding. Higher = better
  structure preservation; a direct over-integration probe.
- Diagnostics (reported, weight 0): `batch_asw_norm`, `cluster_asw_norm`.

Weights are **experimental** (recorded in `plan.json`), not empirically
calibrated. Batch mixing balanced against within-batch structure penalises both
over- and under-integration.

## Outputs

Standard verified-consensus artifacts (`consensus_labels.tsv` with per-cell
`support`/`entropy`, `member_scores.csv` incl. per-member `n_clusters`,
`member_intrinsic_panel.csv`, `cross_method_nmi.csv`, `report.md` with the
`[A: Verified consensus]` banner, a **k-divergence** section, and the fixed
interpretation caveats).

## Caveats

- The consensus is a *reproducible computational estimate, not experimental
  ground truth*. scVI is stochastic (reproducible within tolerance, not
  bit-identical).
- The intrinsic panel is unsupervised batch-mixing-vs-structure; it is **not**
  validated against curated cell types — treat the score as a relative ranking.

## Disclaimer

OmicsClaw is a research and educational tool for multi-omics analysis. It is not
a medical device and does not provide clinical diagnoses. Consult a domain
expert before making decisions based on these results.
