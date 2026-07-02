---
# AUTO-GENERATED header from skill.yaml — do not edit by hand.
# Edit skill.yaml, then run: python scripts/generate_skill_md.py <skill_dir>
name: spatial-integrate
description: Load when removing batch effects across multiple spatial samples on a multi-batch spatial
  AnnData via Harmony, BBKNN, or Scanorama before downstream analysis. Skip when aligning physical slice
  coordinates (use spatial-register); single-batch data (no integration needed) (use spatial-domains).
version: 0.4.0
author: OmicsClaw
license: MIT
emoji: 🔗
tags:
- spatial
- integration
- batch-correction
- harmony
- bbknn
- scanorama
- multi-sample
requires:
- anndata
- bbknn
- harmonypy
- matplotlib
- numpy
- pandas
- scanpy
- scipy
- seaborn
---

# spatial-integrate

## When to use

The user has a multi-sample spatial AnnData (sample / donor labels in
`obs["batch"]` or another `--batch-key`) and wants batch effects in
the **gene-expression embedding** removed before downstream domain /
cluster / DE analysis. Three methods:

- `harmony` (default) — soft k-means in PCA space; produces
  `obsm["X_pca_harmony"]`. Tunable via `--harmony-theta` /
  `--harmony-lambda` / `--harmony-max-iter`. Requires `harmonypy`.
- `bbknn` — batch-balanced neighbour graph; produces a fused
  `obsp["distances"]` ready for UMAP / clustering. Tunable via
  `--bbknn-neighbors-within-batch` / `--bbknn-n-pcs` / `--bbknn-trim`.
  Requires `bbknn`.
- `scanorama` — corrected expression matrix in `obsm["X_scanorama"]`.
  Tunable via `--scanorama-knn` / `--scanorama-sigma` /
  `--scanorama-alpha` / `--scanorama-batch-size`. Requires
  `scanorama`.

For **physical** slice-coordinate alignment use `spatial-register`. For
single-batch data skip this skill and go to `spatial-domains` /
`spatial-de` directly.

## Inputs & Outputs

<!-- AUTO-GENERATED from skill.yaml (interface) — do not edit by hand. Regenerate: python scripts/generate_skill_md.py <skill_dir> -->

**Inputs**

- File types: `.h5ad`
- Requires a preprocessed AnnData (`X` normalised, PCA/neighbours present)
- Expects `obsm`: `X_pca`

**Outputs**

- `tables/batch_sizes.csv`
- `tables/corrected_embedding_points.csv`
- `tables/integration_metrics.csv`
- `tables/integration_observations.csv`
- `tables/umap_after_points.csv`
- `tables/umap_before_points.csv`
- `figures/batch_entropy_after_umap.png`
- `figures/batch_entropy_distribution.png`
- `figures/batch_highlight.png`
- `figures/batch_mixing.png`
- `figures/batch_sizes.png`
- `figures/umap_before_by_batch.png`
- `figures/umap_by_batch.png`
- `figures/umap_by_cluster.png`
- `processed.h5ad`
- `report.md`
- `result.json`
- Processed AnnData (`saves_h5ad`) — adds `obsm`: `X_pca_harmony`, `X_scanorama`

## Flow

1. Load AnnData (`--input`) or build a 3-batch demo via the bundled `spatial-preprocess --demo` (`spatial_integrate.py:803-815` chains via subprocess).
2. `parser.error` validates per-method numeric ranges (`--harmony-theta` ≥ 0; `--harmony-lambda` > 0 or -1; `--harmony-max-iter` ≥ 1; `--bbknn-*` ≥ 1; `--scanorama-*` per-flag bounds).
3. Dispatch to method:
   - `harmony` → write `obsm["X_pca_harmony"]`.
   - `bbknn` → rebuild `obsp["distances"]` + `obsp["connectivities"]`.
   - `scanorama` → write `obsm["X_scanorama"]`.
4. Compute integration metrics (e.g., LISI / silhouette scores when supported).
5. Save `processed.h5ad`, tables, figures, `report.md`, `result.json`.

## Gotchas

- **All numeric flag validation goes through `parser.error` (exit code 2).** `spatial_integrate.py:837-857` covers the harmony / bbknn / scanorama numeric ranges. Wrappers expecting `ValueError` need to catch exit-2 separately.
- **Demo chains through `spatial-preprocess` via subprocess.** `spatial_integrate.py:803` raises `FileNotFoundError(f"OmicsClaw runner not found at {main_runner}")` when `omicsclaw.py` is missing; `:814` raises `RuntimeError("spatial-preprocess --demo failed (exit ...)")` when the chained run fails; `:819` raises `FileNotFoundError(f"Expected {processed}")` when the demo output isn't where expected. Real runs skip this chain.
- **Each method writes a DIFFERENT `obsm` / `obsp` key.** harmony → `obsm["X_pca_harmony"]`, scanorama → `obsm["X_scanorama"]`, bbknn → modifies `obsp["distances"]` / `obsp["connectivities"]` in place (no new `obsm` key). Downstream skills that consume the integrated embedding via `--use-rep` must branch on method.
- **`obsm["X_pca"]` is required as input** for harmony / bbknn (used as starting embedding). If the input AnnData skipped `spatial-preprocess`, harmony / bbknn fail at runtime. Run `spatial-preprocess` first, or check for `X_pca` presence.
- **`--harmony-lambda` accepts `-1` to enable auto-lambda estimation.** `spatial_integrate.py:839` documents this special case in the flag check. Other harmony numerics must be strictly positive.
- **UMAP snapshot key validation.** `spatial_integrate.py:67` raises `KeyError(f"UMAP snapshot '{umap_key}' not found in adata.obsm")` when a "before" UMAP comparison is requested but the key is missing. Affects the integration-metrics figure only.

## Key CLI

```bash
# Demo (synthetic 3-batch chained from spatial-preprocess --demo)
python omicsclaw.py run spatial-integrate --demo --output /tmp/spatial_int_demo

# Default Harmony on real multi-sample data
python omicsclaw.py run spatial-integrate \
  --input multi_sample.h5ad --output results/ \
  --method harmony --batch-key sample

# BBKNN with custom neighbour budget
python omicsclaw.py run spatial-integrate \
  --input multi_sample.h5ad --output results/ \
  --method bbknn --batch-key donor \
  --bbknn-neighbors-within-batch 5 --bbknn-n-pcs 30

# Scanorama with strong correction
python omicsclaw.py run spatial-integrate \
  --input multi_sample.h5ad --output results/ \
  --method scanorama --batch-key library_id \
  --scanorama-sigma 30 --scanorama-alpha 0.05
```

## See also

- `references/parameters.md` — every CLI flag, per-method tunables
- `references/methodology.md` — when Harmony / BBKNN / Scanorama wins; LISI metric
- `references/output_contract.md` — `obsm["X_pca_harmony"]` / `obsm["X_scanorama"]` / `obsp["distances"]` semantics
- Adjacent skills: `spatial-preprocess` (upstream — produces `obsm["X_pca"]` required input), `spatial-register` (parallel — aligns physical coordinates, NOT expression), `spatial-domains` (downstream — pass `--use-rep X_pca_harmony` / `X_scanorama` for batch-aware domain detection), `spatial-de` (downstream — use the integrated embedding for clustering before DE)
