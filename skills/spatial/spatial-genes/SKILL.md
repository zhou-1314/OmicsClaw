---
# AUTO-GENERATED header from skill.yaml — do not edit by hand.
# Edit skill.yaml, then run: python scripts/generate_skill_md.py <skill_dir>
name: spatial-genes
description: Load when ranking spatially variable genes (SVGs) on a preprocessed spatial AnnData via Moran's
  I, SpatialDE, SPARK-X, or FlashS. Skip when detecting tissue domains (use spatial-domains); differential
  expression between groups (use spatial-de).
version: 0.5.0
author: OmicsClaw
license: MIT
emoji: 🧭
tags:
- spatial
- svg
- spatially-variable-genes
- morans-i
- spatialde
- sparkx
- flashs
requires:
- anndata
- matplotlib
- numpy
- pandas
- scanpy
- scipy
- seaborn
- SpatialDE
- squidpy
- statsmodels
---

# spatial-genes

## When to use

The user has a preprocessed spatial AnnData (`obsm["spatial"]`
populated; ideally `layers["counts"]` for count-based methods) and
wants per-gene spatial-variability scores. Four methods:

- `morans` (default) — Moran's I via `squidpy.gr.spatial_autocorr`
  (`--morans-n-neighs`, `--morans-n-perms`). Fast.
- `spatialde` — SpatialDE Gaussian-process model (`--spatialde-min-counts`,
  `--spatialde-aeh-patterns` / `--spatialde-aeh-lengthscale`,
  `--spatialde-no-aeh`). Most rigorous, slower.
- `sparkx` — SPARK-X non-parametric covariance (`--sparkx-num-cores`,
  `--sparkx-max-genes`). Scales to large slides.
- `flashs` — FLASH-S random-Fourier-feature approximation
  (`--flashs-n-rand-features`, `--flashs-bandwidth`). Fastest.

For tissue domain detection use `spatial-domains`; for between-group
DE use `spatial-de`.

## Inputs & Outputs

<!-- AUTO-GENERATED from skill.yaml (interface) — do not edit by hand. Regenerate: python scripts/generate_skill_md.py <skill_dir> -->

**Inputs**

- File types: `.h5ad`
- Requires a preprocessed AnnData (`X` normalised, PCA/neighbours present)
- Expects `obsm`: `spatial`

**Outputs**

- `tables/coords.csv`
- `tables/counts.csv`
- `tables/significant_svgs.csv`
- `tables/sparkx_results.csv`
- `tables/svg_observation_metrics.csv`
- `tables/svg_results.csv`
- `tables/svg_run_summary.csv`
- `tables/top_svg_scores.csv`
- `tables/top_svg_spatial_points.csv`
- `tables/top_svg_umap_points.csv`
- `figures/moran_ranking.png`
- `figures/svg_score_vs_significance.png`
- `figures/svg_significance_distribution.png`
- `figures/top_svg_scores.png`
- `figures/top_svg_spatial.png`
- `figures/top_svg_umap.png`
- `processed.h5ad`
- `report.md`
- `result.json`
- Processed AnnData (`saves_h5ad`) — adds `uns`: `moranI`

## Flow

1. Load AnnData (`--input`) or chain through `spatial-preprocess --demo` via subprocess (`spatial_genes.py:1043-1050`).
2. `parser.error` validates per-method numeric ranges (`--morans-n-neighs` ≥ 1, etc.) at lines `:1062-1078`.
3. Validate input matrix: count-based methods (`spatialde` / `sparkx`) expect `layers["counts"]`; if missing the script logs a warning and falls back to `adata.X` — results may be suboptimal.
4. Dispatch to chosen method; method-specific kwargs flow from `_collect_run_configuration(args)`.
5. Build standardised SVG result table with score / pvalue / padj columns; rank by score.
6. Detect significance at `--fdr-threshold`; build top-N table.
7. Save tables, figures, `processed.h5ad`, `report.md`, `result.json`.

## Gotchas

- **`--input` missing → bare `print` + `sys.exit(1)`, NOT `parser.error`.** `spatial_genes.py:1240` does `print("ERROR: Provide --input or --demo", file=sys.stderr); sys.exit(1)`. Different from sibling skills' `parser.error` (exit 2).
- **`spatialde` / `sparkx` silently fall back to `.X` when `layers["counts"]` is missing.** `spatial_genes.py:1244-1257` logs a warning and continues — `result.json` does NOT record the fallback as a separate flag. Always add `adata.layers["counts"] = adata.X.copy()` upstream when running these count-based methods on preprocessed data.
- **Demo mode chains through `spatial-preprocess --demo` via subprocess.** `spatial_genes.py:1050` raises `RuntimeError(f"spatial-preprocess --demo failed: {result.stderr}")` if the chained run fails. Real runs skip this chain.
- **Per-method numeric flag validation goes through `parser.error` (exit 2).** `spatial_genes.py:1062-1078` covers Moran's-I, SpatialDE, SPARK-X, FlashS numeric ranges. Out-of-range values exit 2 before the method dispatches.
- **No method writes a `var` column — scores live in `adata.uns` (morans only) and `tables/svg_results.csv`.** `spatial_genes.py:377` checks `summary["method"] == "morans" and "moranI" in adata.uns`; `_lib/genes.py:163` consumes from `adata.uns["moranI"]` only. There is no `var["moranI"]` mirror. SpatialDE / SPARK-X / FlashS write to `tables/svg_results.csv` exclusively.
- **Result-table column varies by method.** `tables/svg_results.csv` always has `gene` + `score` + `pvalue` + `padj`, but the `score` column's *semantic* differs: Moran's I (in [-1, 1], higher = more spatial), SpatialDE LL difference, SPARK-X test stat, FLASH-S coefficient. Compare scores within method only.

## Key CLI

```bash
# Demo (chained from spatial-preprocess --demo)
python omicsclaw.py run spatial-genes --demo --output /tmp/spatial_genes_demo

# Default Moran's I
python omicsclaw.py run spatial-genes \
  --input preprocessed.h5ad --output results/ \
  --method morans --morans-n-neighs 6 --morans-n-perms 100

# SpatialDE on raw counts
python omicsclaw.py run spatial-genes \
  --input preprocessed.h5ad --output results/ \
  --method spatialde --spatialde-min-counts 3 --spatialde-aeh-patterns 5

# SPARK-X for large slides
python omicsclaw.py run spatial-genes \
  --input preprocessed.h5ad --output results/ \
  --method sparkx --sparkx-num-cores 8 --sparkx-max-genes 5000

# FLASH-S fast approximation
python omicsclaw.py run spatial-genes \
  --input preprocessed.h5ad --output results/ \
  --method flashs --flashs-n-rand-features 200 --flashs-bandwidth 1.0
```

## See also

- `references/parameters.md` — every CLI flag, per-method tunables
- `references/methodology.md` — Moran's I vs SpatialDE vs SPARK-X vs FlashS speed/rigor trade-offs
- `references/output_contract.md` — `tables/svg_results.csv` column schema; per-method semantics
- Adjacent skills: `spatial-preprocess` (upstream — produces `obsm["spatial"]` and `layers["counts"]`), `spatial-domains` (parallel — domain detection, NOT per-gene scoring), `spatial-statistics` (parallel — autocorrelation / co-occurrence stats over labels, NOT genes), `spatial-de` (downstream — DE between domain pairs after picking interesting domains)
