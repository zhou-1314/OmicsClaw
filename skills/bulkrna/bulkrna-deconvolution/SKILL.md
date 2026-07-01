---
# AUTO-GENERATED header from skill.yaml — do not edit by hand.
# Edit skill.yaml, then run: python scripts/generate_skill_md.py <skill_dir>
name: bulkrna-deconvolution
description: Load when estimating cell-type proportions in bulk RNA-seq samples from a single-cell or
  signature-matrix reference. Skip when the data is already single-cell (no deconvolution needed); spatial
  deconvolution (use spatial-deconv).
version: 0.3.0
author: OmicsClaw
license: MIT
emoji: 🧩
tags:
- bulkrna
- deconvolution
- NNLS
- cell-type-proportion
requires:
- matplotlib
- numpy
- pandas
- scipy
---

# bulkrna-deconvolution

## When to use

Run on a bulk RNA-seq cohort when you have a reference (single-cell
profile or signature matrix) and want per-sample cell-type
proportions.  Built-in NNLS solver is the only backend currently
implemented; the wrapper does not call CIBERSORTx or MuSiC.

## Inputs & Outputs

<!-- AUTO-GENERATED from skill.yaml (interface) — do not edit by hand. Regenerate: python scripts/generate_skill_md.py <skill_dir> -->

**Inputs**

- File types: `.csv`

**Outputs**

- `tables/dominant_types.csv`
- `tables/proportions.csv`
- `figures/mean_proportions_pie.png`
- `figures/proportions_heatmap.png`
- `figures/proportions_stacked.png`
- `report.md`
- `result.json`

## Flow

1. Load bulk matrix (`bulkrna_deconvolution.py:368` raises `ValueError` if `--input` missing without `--demo`).
2. Load reference (`:370` raises `ValueError` if `--reference` missing without `--demo`; `:76` raises `FileNotFoundError` if path doesn't exist).
3. Align gene namespaces between bulk and reference (`:131` raises `ValueError` if no overlap).
4. Run `scipy.optimize.nnls` per sample to estimate per-cell-type weights, then row-normalise to proportions.
5. Render stacked-bar + heatmap; emit `tables/proportions.csv` + `tables/dominant_types.csv`.

## Gotchas

- **`--reference` is REQUIRED for non-demo runs.**  Unlike most bulkrna skills, this one needs two inputs.  `bulkrna_deconvolution.py:370` raises with `"--reference is required when not using --demo"` if you forget; no silent fallback.
- **Gene-namespace mismatch is fatal.**  `:131` raises `ValueError` when bulk and reference share zero gene IDs (typical cause: bulk uses Ensembl, reference uses HGNC symbols).  Pre-run `bulkrna-geneid-mapping` to harmonise.
- **NNLS is the only backend; there is no `--method` flag.**  Despite the skill catalog historically advertising CIBERSORTx and MuSiC bridges, the script (`bulkrna_deconvolution.py:347-358` argparser) accepts only `--input`, `--output`, `--demo`, `--reference`.  The summary dict (`:163-172`) records `n_genes_shared`, `n_samples`, `n_cell_types`, `cell_types`, `proportions_df`, `dominant_types`, `mean_proportions`, `residuals` — no method field, because there is no choice.
- **Negative residuals are not surfaced as a warning.**  NNLS by definition produces non-negative weights, but the per-sample reconstruction `residuals` (saved in `result.json["residuals"]`) measure how well the linear combination explains the bulk profile.  Sanity-check that residuals are small relative to library size; large residuals indicate the reference is missing a major cell type from the bulk.

## Key CLI

```bash
python omicsclaw.py run bulkrna-deconvolution --demo
python omicsclaw.py run bulkrna-deconvolution \
  --input counts.csv --reference signature.csv --output results/
```

## See also

- `references/parameters.md` — every CLI flag and tuning hint
- `references/methodology.md` — NNLS solver, gene-overlap requirement, residual interpretation
- `references/output_contract.md` — exact output directory layout
- Adjacent skills: `bulkrna-geneid-mapping` (run upstream to harmonise gene IDs), `bulkrna-trajblend` (parallel: per-sample pseudotime placement using the same NNLS proportions plus nearest-neighbour mapping), `spatial-deconv` (spatial-side sibling: spot-level proportions with multiple methods)
