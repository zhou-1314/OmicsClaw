---
name: bulkrna-qc
description: Load when checking a bulk RNA-seq count matrix for library-size outliers, gene detection
  rates, and sample-sample correlation before DE. Skip when data is raw FASTQ (use bulkrna-read-qc); aligner
  logs (use bulkrna-read-alignment); single-cell counts (use sc-qc).
trigger: bulk QC, library size, count matrix, sample quality, gene detection, RNA-seq quality, count QC
tags:
- bulkrna
- QC
- count-matrix
- library-size
- gene-detection
- sample-correlation
- CPM
---

# Bulk RNA-seq Count Matrix Quality Control (`bulkrna-qc`)

## 1. Method Decision Matrix & When to Use

Select the count-matrix quality control and normalization diagnostic prior to downstream differential expression or clustering:

| QC Check / Diagnostic | Metric / Transformation | Standard Threshold | Why |
|---|---|---|---|
| **Sequencing depth & library failure** | Total mapped counts per sample ($\sum_g c_{gi}$) | $> 5\times 10^6$ reads (mRNA-seq); flag libraries $< 10\%$ of cohort median | Detects failed library prep or severe under-sequencing before size-factor estimation |
| **Transcriptome complexity / saturation** | Detected genes ($c_{gi} > 0$ and $c_{gi} \ge 10$) & top-gene dominance | $> 10,000$ detected protein-coding genes (human/mouse); top-5 genes $< 25\%$ of reads | Identifies PCR jackpotting, rRNA/globin contamination, or degraded FFPE RNA |
| **Sample-sample concordance & swaps** | Pairwise Pearson/Spearman correlation on $\log_2(\text{CPM} + 1)$ | Within-condition $r \ge 0.90$; flag samples with $|z_r| > 2.5$ below cohort mean | Catches mislabeled samples, batch outliers, or degraded biological replicates |
| **Exploratory visualization matrix** | Counts Per Million ($\text{CPM}_{gi} = \frac{c_{gi}}{\sum_g c_{gi}} \times 10^6$) | Used for density/PCA plots only | Normalizes library depth for visual QC while keeping raw integer counts untouched for `pydeseq2` |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Idiomatic `pandas` / `scipy` Count Matrix QC & Outlier Detection

```python
import numpy as np
import pandas as pd

# 1. Load gene-by-sample raw integer count matrix
counts = pd.read_csv("data/counts.csv", index_col=0)
if (counts.values < 0).any():
    raise ValueError("Count matrix contains negative values.")

# 2. Compute per-sample library size, gene detection, and CPM normalization
lib_size = counts.sum(axis=0)
detected_gt0 = (counts > 0).sum(axis=0)
detected_ge10 = (counts >= 10).sum(axis=0)
cpm = counts.div(lib_size.replace(0, np.nan), axis=1) * 1e6
log2_cpm = np.log2(cpm + 1.0)

# 3. Evaluate sample-sample Pearson correlation and flag low-correlation outliers
corr_mat = log2_cpm.corr(method="pearson")
mean_corr = (corr_mat.sum(axis=1) - 1.0) / max(len(corr_mat) - 1, 1)
z_corr = (mean_corr - mean_corr.mean()) / max(mean_corr.std(ddof=1), 1e-8)
is_outlier = (z_corr < -2.0) | (lib_size < 0.2 * lib_size.median())

qc_table = pd.DataFrame({
    "total_counts": lib_size,
    "detected_genes": detected_gt0,
    "expressed_genes_ge10": detected_ge10,
    "mean_sample_corr": mean_corr,
    "is_outlier": is_outlier,
})
qc_table.to_csv("tables/sample_stats.csv")
```

### Pattern B: Lightweight Micro-Helper via `use_skill("bulkrna-qc")`

```python
# Calling use_skill("bulkrna-qc") auto-injects kernel.py helpers into the python kernel:
sample_stats = assess_count_matrix(counts)
cpm_df = cpm_normalize(counts)
```

## 3. Output Format & Verification Contract

- **Sample QC Summary Table** (`tables/sample_stats.csv`):
  - Indexed by `sample` with columns `total_counts`, `detected_genes`, `expressed_genes_ge10`, `mean_sample_corr`, `is_outlier`.
- **Normalized Visualization Matrix** (`tables/cpm_normalized.csv`):
  - Gene-by-sample CPM matrix where each column sums to $10^6$ (`np.allclose(cpm_df.sum(axis=0), 1e6)`).
- **Verification Invariants**:
  - Every sample column in `counts` appears in `tables/sample_stats.csv`.
  - Zero-count libraries raise an explicit `ValueError` rather than producing `NaN` or `Inf` in `tables/cpm_normalized.csv`.

## 4. Gotchas & Troubleshooting

1. **Never Pass `cpm_normalized.csv` into `DESeq2` / `pydeseq2`**:
   - `tables/cpm_normalized.csv` is generated strictly for QC density plots, heatmaps, and PCA. `pydeseq2` requires raw unmodified integer counts so its Negative Binomial dispersion model can weight Poisson shot noise accurately.
2. **Correlation Outliers in Heterogeneous Designs**:
   - Cohort-wide mean correlation (`mean_sample_corr`) can flag a genuinely distinct biological condition if that group has far fewer replicates than the reference group. Always inspect the full `sample_correlation` heatmap alongside condition metadata before dropping a sample.
3. **High Mitochondrial / rRNA / Globin Gene Dominance**:
   - If a sample has normal `total_counts` ($> 20\text{M}$) but low `detected_genes` ($< 6,000$), check what fraction of reads map to `MT-*` genes or hemoglobin (`HBA1`, `HBB`). A few hyper-abundant transcripts can consume $> 50\%$ of sequencing depth and starve the rest of the transcriptome.

## Dependencies

`matplotlib`, `numpy`, `pandas`, `scipy`
