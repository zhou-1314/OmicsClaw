---
# AUTO-GENERATED header from skill.yaml — do not edit by hand.
# Edit skill.yaml, then run: python scripts/generate_skill_md.py <skill_dir>
name: spatial-condition
description: Load when comparing two or more experimental conditions (treatment vs control) on a multi-sample
  preprocessed spatial AnnData via PyDESeq2 pseudobulk or Wilcoxon DE — needs `obs[condition_key]`, `obs[sample_key]`,
  and cluster labels. Skip when running per-cluster DE on one condition (use spatial-de); comparing two
  slices without replicates.
version: 0.5.0
author: OmicsClaw
license: MIT
emoji: ⚖️
tags:
- spatial
- condition
- pseudobulk
- pydeseq2
- wilcoxon
- differential-expression
requires:
- anndata
- matplotlib
- numpy
- pandas
- pydeseq2
- scanpy
- scipy
- seaborn
- statsmodels
---

# spatial-condition

## When to use

The user has a preprocessed multi-sample spatial AnnData with
`obs[condition_key]` (e.g. `treatment`/`control`), `obs[sample_key]`
(biological replicate id), and a cluster column (default `leiden`),
and wants per-cluster differential expression between conditions.
Two backends:

- `pydeseq2` (default) — pseudobulk per `(sample, cluster)`,
  PyDESeq2 NB/GLM. Requires raw counts in `layers["counts"]` (or
  `adata.raw` as fallback). Honours replicate structure correctly.
- `wilcoxon` — spot-level Wilcoxon rank-sum
  (`scanpy.tl.rank_genes_groups`). Cheap fallback when no replicate
  structure exists, but ignores pseudoreplication.

For per-cluster DE within a single condition use `spatial-de`. For
spatially variable genes use `spatial-genes`.

## Inputs & Outputs

<!-- AUTO-GENERATED from skill.yaml (interface) — do not edit by hand. Regenerate: python scripts/generate_skill_md.py <skill_dir> -->

**Inputs**

- File types: `.h5ad`
- Requires a preprocessed AnnData (`X` normalised, PCA/neighbours present)
- Expects `obsm`: `spatial`

**Outputs**

- `tables/cluster_de_metrics.csv`
- `tables/condition_run_summary.csv`
- `tables/condition_spatial_points.csv`
- `tables/condition_umap_points.csv`
- `tables/per_cluster_summary.csv`
- `tables/pseudobulk_de.csv`
- `tables/pseudobulk_volcano_points.csv`
- `tables/sample_counts_by_condition.csv`
- `tables/skipped_contrasts.csv`
- `tables/top_de_genes.csv`
- `figures/cluster_de_burden.png`
- `figures/condition_de_barplot.png`
- `figures/condition_effect_burden_spatial.png`
- `figures/condition_effect_burden_umap.png`
- `figures/condition_pvalue_distribution.png`
- `figures/condition_spatial_context.png`
- `figures/pseudobulk_volcano.png`
- `figures/sample_counts_by_condition.png`
- `figures/skipped_contrasts.png`
- `processed.h5ad`
- `report.md`
- `result.json`
- Processed AnnData (`saves_h5ad`)

## Flow

1. Load AnnData (`--input`) or build a 12-sample demo (`--demo`).
2. Validate `obs[condition_key]` + `obs[sample_key]` exist (`_lib/condition.py:91-95` raises `ValueError` if missing); cast `condition_key` + `cluster_key` to Categorical (`spatial_condition.py:84-86`).
3. For `pydeseq2`: aggregate raw counts per `(sample, cluster)` pseudobulk; require `layers["counts"]` or fall back to `adata.raw`.
4. Per cluster: skip the contrast if either condition has < `--min-samples-per-condition` samples; log to `tables/skipped_contrasts.csv`.
5. Fit DE model per surviving (cluster, contrast); apply `--fdr-threshold` + `--log2fc-threshold`.
6. Compute UMAP / spatial summaries; render plots; save tables and `processed.h5ad`.

## Gotchas

- **`pydeseq2` falls back silently when raw counts are missing.** `_lib/condition.py:60-86` (`_get_counts_matrix`) prefers `adata.layers["counts"]`, falls back to `adata.raw`, then to `adata.X` — each fallback only logs a warning. If `adata.X` is log-normalised, pseudobulk sums are statistically invalid (`log(a)+log(b) != log(a+b)`). Always preprocess so `layers["counts"]` is populated. `wilcoxon` skips this codepath entirely — it normalises internally.
- **Single-condition / no-replicate clusters are silently skipped.** `tables/skipped_contrasts.csv` lists clusters with < `--min-samples-per-condition` samples per condition. Always inspect that file — clusters not in `pseudobulk_de.csv` were dropped, not "no DE genes".
- **`--condition-key` and `--sample-key` must be different columns.** `spatial_condition.py:1024-1025` rejects via `parser.error` when they match. A common mistake is using `condition` for both — pseudobulk needs the sample axis distinct from the condition axis.
- **`obs[condition_key]` and `obs[cluster_key]` cast to Categorical in place.** `spatial_condition.py:84-86` overwrites both columns with `pd.Categorical(...)`. Order is sorted-unique unless `--reference-condition` pins the reference level — non-alphabetical custom orderings on input are lost. `obs[sample_key]` is NOT cast.
- **`obsm["X_pca"]` is recomputed inside the script when needed.** `spatial_condition.py:1170` writes `obsm["X_pca"] = adata_hvg.obsm["X_pca"]` for the UMAP / PCA reporting view; this is a diagnostic recompute, not a published embedding.
- **PyDESeq2 needs ≥ 2 samples per condition.** `--min-samples-per-condition` defaults to 2. If your study has one slice per condition, either pool spots into pseudo-replicates upstream or fall back to `--method wilcoxon`.

## Key CLI

```bash
# Demo (synthetic 12-sample data)
python omicsclaw.py run spatial-condition --demo --output /tmp/cond_demo

# PyDESeq2 pseudobulk (default)
python omicsclaw.py run spatial-condition \
  --input preprocessed.h5ad --output results/ \
  --method pydeseq2 \
  --condition-key treatment --sample-key sample_id --cluster-key leiden \
  --reference-condition control \
  --min-samples-per-condition 3 --fdr-threshold 0.05 --log2fc-threshold 1.0

# Wilcoxon spot-level (cheap fallback when no replicates)
python omicsclaw.py run spatial-condition \
  --input preprocessed.h5ad --output results/ \
  --method wilcoxon --condition-key treatment --sample-key sample_id \
  --wilcoxon-alternative two-sided
```

## See also

- `references/parameters.md` — every CLI flag, per-method tunables
- `references/methodology.md` — when each backend wins; replicate-count rules
- `references/output_contract.md` — pseudobulk + skipped-contrast schemas
- Adjacent skills: `spatial-preprocess` (upstream), `spatial-domains` (upstream — provides `obs["leiden"]`), `spatial-de` (parallel — per-cluster DE within one condition), `spatial-integrate` (upstream — required for cross-batch comparisons), `spatial-statistics` (parallel — per-gene Moran's I)
