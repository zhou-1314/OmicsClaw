---
# AUTO-GENERATED header from skill.yaml — do not edit by hand.
# Edit skill.yaml, then run: python scripts/generate_skill_md.py <skill_dir>
name: spatial-statistics
description: Load when running spatial autocorrelation / hotspot / co-occurrence / neighbourhood-enrichment
  / Ripley K stats on a clustered spatial AnnData via squidpy. Skip when ranking spatially variable genes
  (use spatial-genes); tissue domain detection (use spatial-domains).
version: 0.5.0
author: OmicsClaw
license: MIT
emoji: 📊
tags:
- spatial
- statistics
- moran
- geary
- ripley
- co-occurrence
- nhood-enrichment
- getis-ord
- squidpy
requires:
- anndata
- esda
- libpysal
- matplotlib
- networkx
- numpy
- pandas
- scanpy
- scipy
- seaborn
- squidpy
---

# spatial-statistics

## When to use

The user has a clustered spatial AnnData (`obs[--cluster-key]` for
cluster-aware analyses; `obsm["spatial"]` populated) and wants a
specific spatial-statistics analysis. Pick `--analysis-type` from
`VALID_ANALYSIS_TYPES`:

- `moran` / `geary` — global spatial autocorrelation per gene.
- `local_moran` — per-spot LISA + GeoDa quadrants (`--local-moran-geoda-quads`).
- `getis_ord` — per-spot hotspot Z-scores.
- `bivariate_moran` — exactly two genes (`--genes geneA,geneB`).
- `neighborhood_enrichment` — squidpy NES between cluster pairs.
- `ripley` — Ripley K / L / G / F (`--ripley-mode`, `--ripley-metric`).
- `co_occurrence` — pairwise label co-occurrence at distance bins
  (`--coocc-interval`, `--coocc-n-splits`).
- `spatial_centrality` — graph-centrality per spot.

For per-gene SVG ranking use `spatial-genes`; for tissue-domain
detection use `spatial-domains`.

## Inputs & Outputs

<!-- AUTO-GENERATED from skill.yaml (interface) — do not edit by hand. Regenerate: python scripts/generate_skill_md.py <skill_dir> -->

**Inputs**

- File types: `.h5ad`
- Requires a preprocessed AnnData (`X` normalised, PCA/neighbours present)
- Expects `obsm`: `spatial`

**Outputs**

- `tables/analysis_results.csv`
- `tables/analysis_summary.csv`
- `tables/bivariate_moran_summary.csv`
- `tables/centrality_scores.csv`
- `tables/cluster_summary.csv`
- `tables/cooccurrence_curves.csv`
- `tables/cooccurrence_pairs.csv`
- `tables/neighborhood_counts.csv`
- `tables/neighborhood_pairs.csv`
- `tables/neighborhood_zscore.csv`
- `tables/network_per_cluster.csv`
- `tables/network_summary.csv`
- `tables/pair_summary.csv`
- `tables/per_cluster_metrics.csv`
- `tables/ripley_cluster_summary.csv`
- `tables/ripley_curves.csv`
- `tables/spot_statistics.csv`
- `tables/top_results.csv`
- `figures/bivariate_moran_scatter.png`
- `figures/bivariate_moran_spatial.png`
- `figures/centrality_scores.png`
- `figures/centrality_scores_barplot.png`
- `figures/co_occurrence_curves.png`
- `figures/co_occurrence_distribution.png`
- `figures/co_occurrence_top_pairs.png`
- `figures/geary_pvalue_distribution.png`
- `figures/geary_ranking.png`
- `figures/geary_score_vs_significance.png`
- `figures/moran_pvalue_distribution.png`
- `figures/moran_ranking.png`
- `figures/moran_score_vs_significance.png`
- `figures/neighborhood_enrichment_heatmap.png`
- `figures/neighborhood_top_pairs.png`
- `figures/neighborhood_zscore_distribution.png`
- `figures/network_degree_histogram.png`
- `figures/network_per_cluster_degree.png`
- `figures/ripley_cluster_max_stat.png`
- `figures/ripley_curves.png`
- `figures/ripley_stat_distribution.png`
- `processed.h5ad`
- `report.md`
- `result.json`
- Processed AnnData (`saves_h5ad`) — adds `obs`: `local_moran_<gene>`, `local_moran_pval_<gene>`, `local_moran_q_<gene>`, `getis_ord_<gene>`, `getis_ord_pval_<gene>`

## Flow

1. Load AnnData (`--input`) or chain through `spatial-preprocess --demo` via subprocess (`spatial_statistics.py:1499-1515`).
2. `parser.error` validates `--analysis-type` ∈ `VALID_ANALYSIS_TYPES`; per-analysis numeric ranges (lines `:1558-1601`).
3. Auto-resolve `--cluster-key` if unset; auto-leiden if no cluster column exists (with size guard at `:1546`).
4. Dispatch to analysis-type runner; squidpy graph build uses `--stats-n-neighs` / `--stats-n-rings` / `--stats-n-perms`.
5. Build standardised result tables; collect per-spot metrics.
6. Save tables, figures, `processed.h5ad`, `report.md`, `result.json`.

## Gotchas

- **All input + parameter validation goes through `parser.error` (exit code 2).** `spatial_statistics.py:1530` for missing `--input`; `:1546` for too-small dataset to auto-leiden; `:1558` for invalid `--analysis-type`; `:1560-1601` for numeric / multi-value flag validation. Wrappers expecting `ValueError` need to catch exit-2.
- **`bivariate_moran` requires exactly TWO genes.** `spatial_statistics.py:1601` raises `parser.error("--analysis-type bivariate_moran requires exactly two genes via --genes geneA,geneB")`. Pass them comma-separated, no spaces.
- **Demo chains through `spatial-preprocess` via subprocess.** `spatial_statistics.py:1499` raises `FileNotFoundError(f"spatial-preprocess not found at {preprocess_script}")` if the sibling skill is missing; `:1510` raises `RuntimeError("spatial-preprocess --demo failed (exit ...)")` on chained failure; `:1515` raises `FileNotFoundError(f"Expected {processed}")` when the demo output isn't where expected.
- **`--cluster-key` auto-leiden has a size guard.** `spatial_statistics.py:1546` raises `parser.error("Dataset is too small to auto-compute `leiden` clusters.")` when the auto-fallback can't run. Pass `--cluster-key <existing-column>` for small datasets.
- **`local_moran` writes `n_significant_spots`; `getis_ord` writes `n_hotspots`.** `spatial_statistics.py:555` documents the value-column divergence. Downstream tools reading "spatially significant cell count" need to branch on `--analysis-type`.
- **`neighborhood_enrichment` consumes `<cluster_key>_nhood_enrichment` from `uns`.** Computed lazily within squidpy; if you re-run with a different `--cluster-key`, the previous `uns` key remains and won't be reused for the new cluster column. Clean `adata.uns` between runs or expect stale keys.
- **`spatial_centrality` is graph-only.** It produces per-spot centrality without any cluster-key dependency — useful when `--cluster-key` is unavailable.

## Key CLI

```bash
# Demo (chained from spatial-preprocess --demo)
python omicsclaw.py run spatial-statistics --demo --analysis-type moran --output /tmp/spatial_stats_demo

# Global Moran's I on a clustered AnnData
python omicsclaw.py run spatial-statistics \
  --input clustered.h5ad --output results/ \
  --analysis-type moran --cluster-key spatial_domain --stats-n-perms 100

# Local Moran with GeoDa quadrants
python omicsclaw.py run spatial-statistics \
  --input clustered.h5ad --output results/ \
  --analysis-type local_moran --local-moran-geoda-quads --n-top-genes 20

# Neighbourhood enrichment between clusters
python omicsclaw.py run spatial-statistics \
  --input clustered.h5ad --output results/ \
  --analysis-type neighborhood_enrichment --cluster-key spatial_domain

# Ripley K on a labelled object
python omicsclaw.py run spatial-statistics \
  --input clustered.h5ad --output results/ \
  --analysis-type ripley --ripley-mode K --ripley-n-simulations 100 --ripley-n-steps 50

# Co-occurrence at increasing distance bins
python omicsclaw.py run spatial-statistics \
  --input clustered.h5ad --output results/ \
  --analysis-type co_occurrence --cluster-key cell_type --coocc-interval 30 --coocc-n-splits 5

# Bivariate Moran between two genes
python omicsclaw.py run spatial-statistics \
  --input clustered.h5ad --output results/ \
  --analysis-type bivariate_moran --genes EGFR,BRCA1
```

## See also

- `references/parameters.md` — every CLI flag, per-analysis numeric ranges
- `references/methodology.md` — when each analysis-type wins; squidpy mapping
- `references/output_contract.md` — per-analysis table / `obs` / `uns` schema
- Adjacent skills: `spatial-preprocess` (upstream — produces `obsm["spatial"]` + cluster column), `spatial-domains` / `spatial-annotate` (upstream — produce `obs["spatial_domain"]` / cell-type labels for `--cluster-key`), `spatial-genes` (parallel — per-gene SVG ranking, NOT statistics on labels), `spatial-de` (downstream — DE between clusters identified by neighbourhood-enrichment hotspots)
