---
# AUTO-GENERATED header from skill.yaml — do not edit by hand.
# Edit skill.yaml, then run: python scripts/generate_skill_md.py <skill_dir>
name: proteomics-de
description: Load when computing two-group differential protein abundance (group2 vs group1, log2FC +
  p-value + BH-adjusted FDR) via Welch t-test, equal-variance t-test, or Mann-Whitney on a wide protein
  × sample CSV. Skip when you need multi-condition DE (run pairwise contrasts manually); label-based TMT
  linear-mixed models.
version: 0.5.0
author: OmicsClaw
license: MIT
emoji: ⚖️
tags:
- proteomics
- differential-expression
- ttest
- welch
- mann-whitney
- bh-fdr
requires:
- numpy
- pandas
- scipy
---

# proteomics-de

## When to use

The user has a wide protein × sample CSV (rows = proteins as
index, columns = samples) and wants two-group differential
abundance. Three backends:

- `ttest` (default) — Student's two-sample t-test (equal variance).
- `welch` — Welch's t-test (unequal variance).
- `mann_whitney` — non-parametric Mann-Whitney U.

All return per-protein `log2fc` (group2 vs group1), `pvalue`, and
BH-adjusted `padj`. `--alpha` controls significance threshold for
the `tables/significant.csv` shortlist; `--log2fc-threshold`
optionally adds an absolute log2FC filter.

For multi-condition DE, run pairwise contrasts manually. For
label-based TMT linear-mixed models, use MSstats / limma in R.

## Inputs & Outputs

<!-- AUTO-GENERATED from skill.yaml (interface) — do not edit by hand. Regenerate: python scripts/generate_skill_md.py <skill_dir> -->

**Inputs**

- File types: `.csv`

**Outputs**

- `tables/differential_abundance.csv`
- `tables/significant.csv`
- `report.md`
- `result.json`

## Flow

1. Load CSV with `pd.read_csv(args.input_path, index_col=0)` (`proteomics_de.py:289`); split columns at midpoint — first half = group1, second half = group2 (`:290-292`). NO CLI flag for prefix/suffix.
2. Dispatch on `--method` (`proteomics_de.py:295`); per-protein test → `log2fc` (mean(log2(g2)) − mean(log2(g1))) + raw `pvalue`.
3. Apply BH FDR adjustment (`proteomics_de.py:137` / `:178`) → `padj` column.
4. Filter `padj < args.alpha` (and `|log2fc| ≥ args.log2fc_threshold` if > 0) → `tables/significant.csv`.
5. Write `tables/differential_abundance.csv` (`proteomics_de.py:299`) + `tables/significant.csv` (`:306`) + `report.md` + `result.json` (`:322`).

## Gotchas

- **Group assignment is by COLUMN POSITION — first half / second half.** `proteomics_de.py:290-292` splits `data.columns[:mid]` vs `data.columns[mid:]`. There is NO CLI flag for control / treatment prefixes; if your CSV columns are interleaved, pre-sort them. Demo uses `control_1..N` then `treatment_1..N` (`:204-205`).
- **Index column 0 is treated as the protein ID.** `pd.read_csv(args.input_path, index_col=0)` (`proteomics_de.py:289`) is unconditional — make sure your protein-ID column is the FIRST column in the CSV.
- **Unknown `--method` raises `ValueError`.** `proteomics_de.py:192` rejects values outside `("ttest", "welch", "mann_whitney")` — argparse `choices=` enforces this at parse time too.
- **`--input` REQUIRED unless `--demo`.** `proteomics_de.py:288` raises `ValueError("--input required")`.
- **log2FC direction: group2 minus group1.** Positive `log2fc` means group2 > group1. If your "control" is in the second half of columns, you'll get inverted signs — the script does NOT auto-detect direction.
- **NaN handling differs per backend.** `ttest` / `welch` (`proteomics_de.py:116-118`) drop rows where either group's mean is non-finite (`np.isfinite` filter). `mann_whitney` (`:150-151`) additionally drops `0` values (`g1 > 0`, `g2 > 0`) — small placeholder intensities silently disappear from Mann-Whitney runs but stay in t-test runs. Pre-impute zeros if you need consistent behaviour.

## Key CLI

```bash
# Demo
python omicsclaw.py run proteomics-de --demo --output /tmp/de_demo

# Real CSV (first half = group1, second half = group2)
python omicsclaw.py run proteomics-de \
  --input protein_abundance.csv --output results/ \
  --method welch --alpha 0.05 --log2fc-threshold 1.0

# Mann-Whitney (non-parametric)
python omicsclaw.py run proteomics-de \
  --input protein_abundance.csv --output results/ \
  --method mann_whitney --alpha 0.01
```

## See also

- `references/parameters.md` — every CLI flag
- `references/methodology.md` — t-test / Welch / Mann-Whitney trade-offs, BH FDR
- `references/output_contract.md` — `tables/differential_abundance.csv` schema
- Adjacent skills: `proteomics-quantification` (upstream — produces protein abundance), `proteomics-data-import` (upstream — schema normalisation), `proteomics-enrichment` (downstream — pathway enrichment on significant proteins), `proteomics-ptm` (parallel — PTM site analysis)
