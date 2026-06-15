---
name: sc-integrate-cluster
description: A consensus member skill that integrates (Harmony/Scanorama/scVI or unintegrated baseline) and clusters in one self-contained unit, emitting the sc-clustering artifact schema. Used as a fan-out member of sc-consensus-integration; not normally called directly.
version: 0.1.0
author: OmicsClaw
license: MIT
tags:
- singlecell
- scrna
- integration
- clustering
- consensus
---

# sc-integrate-cluster

**Domain:** Single-Cell Omics (scRNA) · **Role:** consensus member skill

One self-contained *integrate + cluster* unit, used as a fan-out member of
`sc-consensus-integration` (ADR 0016 / 0029). It produces a batch-correction
representation **and** clusters on it at a fixed resolution, emitting the
standard `sc-clustering` artifact schema so the consensus
`ScClusteringArtifactReader` reads it unchanged.

This is the single-cell analog of `spatial-domains --method <m>`: where the
spatial consensus fans out genuinely different domain algorithms
(spagcn/graphst/cellcharter), the integration consensus fans out genuinely
different batch-correction representations.

## When to use

- As a member of `sc-consensus-integration` (you normally do not call this
  directly — the consensus planner fans it out across methods).
- Input must be a preprocessed AnnData with an `obs` batch key (≥2 batches for
  any real integration method) and ideally an `X_pca` baseline (computed if
  absent).

## Methods (`--method`)

| method | backend | device | obsm key | notes |
|---|---|---|---|---|
| `none` | unintegrated baseline | CPU | `X_pca` | reveals batch-artifact clusters |
| `harmony` | Harmony | CPU | `X_harmony` | fast, deterministic |
| `scanorama` | Scanorama | CPU | `X_scanorama` | needs shared genes across batches |
| `scvi` | scVI VAE | GPU | `X_scvi` | **stochastic** (reproducible within tolerance, not bit-identical); opt-in, serialise GPU members |

## CLI

```bash
python skills/singlecell/scrna/sc-integrate-cluster/sc_integrate_cluster.py \
  --input <preprocessed.h5ad> --output <member_dir> \
  --method harmony --batch-key batch --resolution 1.0 --cluster-method leiden --seed 0
```

## Outputs

- `figure_data/embedding_points.csv` — `cell_id`, `embedding_key`, `coord1`,
  `coord2`, `<cluster-method>` (the labels the consensus reads), `batch`.
- `figure_data/clustering_summary.csv` — `method`, `representation_used`,
  `cluster_method`, `n_cells`, `n_clusters`, `resolution`, `batch_key`,
  `n_batches`.
- `processed.h5ad` — carries `obsm[representation_used]` + `obsm['X_pca']` +
  the cluster labels and batch key; the consensus driver reads this member's
  embedding to compute the integration intrinsic panel.
- `result.json` — standardised result envelope.

## Disclaimer

OmicsClaw is a research and educational tool for multi-omics analysis. It is
not a medical device and does not provide clinical diagnoses. Consult a domain
expert before making decisions based on these results.
