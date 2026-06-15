---
name: sc-consensus-integration
description: 'Load when you want a multi-sample single-cell (scRNA) clustering robust to the choice of integration method — fanning out Harmony/Scanorama/scVI + an unintegrated baseline, scoring each by a batch-mixing intrinsic panel, and voting a consensus. Skip when single-batch (use sc-consensus-clustering) or one integration method is fixed.'
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

## When to use

Verified consensus over **batch-correction representations**. For multi-sample
single-cell data the dominant axis of variation is not clustering resolution but
how batch effect is removed: clustering uncorrected PCA of multi-sample data
clusters *batches*, not cell types, and different integration methods
(Harmony / Scanorama / scVI / …) yield different embeddings and so different
clusterings. Use this when you have a preprocessed multi-sample AnnData with a
batch key in `obs` (≥2 batches) and want a clustering that is **not an artifact
of one integration method**, with per-cell confidence and batch-artifact flags.

It mirrors `consensus-domains`: members fan out `sc-integrate-cluster --method <m>`
— each a self-contained *integrate + cluster* unit — at a **fixed** resolution
(so member cluster counts stay comparable for the operator), scored by the
integration intrinsic panel (ADR 0029) before voting a consensus.

## Inputs & Outputs

| Input | Format | Required |
|---|---|---|
| Preprocessed multi-sample AnnData | `--input <preprocessed.h5ad>` with `obsm["X_pca"]` + a batch key in `obs` (≥2 batches) | yes |
| Output directory | `--output <dir>` | yes |
| Batch key | `--batch-key batch` | no (default `batch`) |
| Integration methods | `--integration-methods none,harmony,scanorama` | no (default set) |
| Include scVI member | `--include-scvi` | no (GPU/stochastic; serialise with `--max-parallel 1`) |
| Fixed resolution | `--resolution 1.0` | no |
| Operator | `--operator {kmode,weighted,lca}` | no (default `kmode`) |
| Non-interactive | `--non-interactive` | no |
| Seed | `--seed 0` | no |

| Output | Path | Notes |
|---|---|---|
| Verified consensus labels | `consensus_labels.tsv` | per-cell `consensus_<operator>`, `support`, `entropy` |
| Composite member scores | `member_scores.csv` | incl. per-member `n_clusters` |
| Intrinsic panel breakdown | `member_intrinsic_panel.csv` | iLISI / kNN-preservation per member |
| Cross-method NMI | `cross_method_nmi.csv` | square per member |
| Audit + panel weights | `plan.json` | members, operator, experimental panel weights |
| Markdown report | `report.md` | `[A: Verified consensus]` banner + k-divergence section |

## Flow

1. **Plan members** — the `--integration-methods` set (`none` baseline + harmony
   + scanorama by default; `scvi` via `--include-scvi`).
2. **Fan out** — run `sc-integrate-cluster --method <m>` per member at the fixed
   `--resolution` (member cluster counts stay comparable for the operator).
3. **Score** — the driver computes the **batch-mixing intrinsic panel** (ADR 0029)
   on each member's embedding + batch key: `ilisi_norm` (0.5, iLISI diversity
   `(iLISI−1)/(n_batches−1)`) and `knn_preservation_norm` (0.5, within-batch
   `X_pca` neighbour retention); `batch_asw_norm` / `cluster_asw_norm` reported at
   weight 0.
4. **Consensus** — vote `kmode` / `weighted` / `lca` over the surviving members.
5. **Report** — banner + per-cell support/entropy + a k-divergence section.

## Gotchas

- **`none` is the unintegrated `X_pca` baseline** — it deliberately exposes
  batch-artifact clusters; clusters that survive only on `none` are the artifacts
  the consensus is meant to flag.
- **scVI is GPU/stochastic** — reproducible within tolerance, not bit-identical;
  add it with `--include-scvi` and serialise GPU members with `--max-parallel 1`.
- **The intrinsic panel is unsupervised batch-mixing-vs-structure** — it is NOT
  validated against curated cell types; treat the score as a relative ranking and
  the panel weights in `plan.json` as experimental (ADR 0029), not calibrated.
- **Fixed `--resolution` is intentional** — members must produce comparable
  cluster counts for the operator; do not sweep resolution here (use
  `sc-consensus-clustering` for the resolution-robustness question).

## Key CLI

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

## See also

- `references/methodology.md` — integration-consensus + intrinsic-panel rationale
- `references/output_contract.md` — `consensus_labels.tsv` / `member_scores.csv` / `plan.json` schema
- `references/parameters.md` — every CLI flag (generated from `parameters.yaml`)
- Adjacent skills: `sc-preprocessing` (upstream), `sc-integrate-cluster` (the per-member integrate+cluster unit this wraps), `sc-consensus-clustering` (parallel — resolution-robustness instead of integration-robustness), `consensus-domains` (parallel — the spatial analogue)
- ADR 0011/0016/0029 — scoring protocol, workflow runtime, integration intrinsic panel
