---
name: genomics-epigenomics
description: Load when summarising a peak file (BED / narrowPeak) from ATAC-seq / ChIP-seq / CUT&Tag —
  peak count, width distribution, per-chromosome counts, score statistics. Skip when calling peaks from
  BAM (run MACS / Genrich externally first); working with single-cell ATAC (use scatac-preprocessing).
trigger: epigenomics, ATAC-seq, ChIP-seq, peak calling, MACS, motif, chromatin
tags:
- genomics
- epigenomics
- atac-seq
- chip-seq
- cut-tag
- peaks
- macs
- bed
---

# Epigenomic Peak & Chromatin Accessibility Analysis (`genomics-epigenomics`)

## 1. Method Decision Matrix & When to Use

Select the peak caller and interval analysis workflow according to chromatin assay (`ChIP-seq`, `ATAC-seq`, `CUT&Tag`):

| Chromatin Assay | Signal Topology | Recommended Upstream Caller | Key Parameters & Why |
|---|---|---|---|
| **TF ChIP-seq / Punctate Histone** (`H3K4me3`, `H3K27ac`) | Sharp narrow peaks (150–500 bp) | **MACS3** (`macs3 callpeak`) | `-f BAMPE -g hs -q 0.01 --keep-dup all` with matched IgG/Input control (`-c`) |
| **Broad Histone Marks** (`H3K27me3`, `H3K36me3`, `H3K9me3`) | Broad domains (1–100 kb) | **MACS3** (`--broad --broad-cutoff 0.1`) or **SICER2** | Merges nearby enriched islands across broad heterochromatic or transcribed domains |
| **ATAC-seq** (open chromatin, Tn5 transposase) | Nucleosome-free regions (NFR) + cutting sites | **MACS3** (`--nomodel --shift -100 --extsize 200`) | Centers windows directly on 5' Tn5 insertion sites after `+4 bp / -5 bp` shift |
| **CUT&Tag / CUT&RUN** (low background, pA-Tn5) | High SNR, ultra-low background | **SEACR** (stringent/relaxed) or **MACS3** (`-f BAMPE`) | Poisson background models in standard ChIP callers over-call false peaks when background is near zero |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: `MACS3` Peak Calling + `pyranges` / `pandas` narrowPeak Inspection

```python
import pandas as pd

# 1. Call narrow peaks with MACS3 (CLI):
# macs3 callpeak -t treat.bam -c input.bam -f BAMPE -g hs -n sample1 -q 0.01 --outdir macs3_out/

# 2. Load ENCODE 10-column narrowPeak BED (0-based half-open coordinates)
cols = ["chrom", "start", "end", "name", "score", "strand", "signal_value", "p_value", "q_value", "peak"]
peaks = pd.read_csv("macs3_out/sample1_peaks.narrowPeak", sep="\t", header=None, names=cols)
peaks["width"] = peaks["end"] - peaks["start"]
high_conf = peaks[peaks["q_value"] >= 2.0]  # -log10(q) >= 2.0 corresponds to FDR <= 0.01
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("genomics-epigenomics")`)

```python
# Calling use_skill("genomics-epigenomics") auto-injects kernel.py helpers into the python kernel:
peaks = read_records("data/peaks.narrowPeak")
res_df = analyze(peaks, assay="atac-seq")
summary = peak_summary(res_df)
```

## 3. Output Format & Verification Contract

- **Peak Summary Table** (`tables/peaks_summary.csv`):
  - Contains `chrom`, `start`, `end`, `width` (`end - start`), `signal_value`, `q_value` ($-\log_{10} q$), and `assay`.
- **Per-Chromosome Peak Distribution** (`tables/peaks_per_chromosome.csv`):
  - Contains `chrom`, `n_peaks`, `total_bp`, and `mean_width`.
- **Verification Invariants**:
  - All intervals satisfy BED 0-based half-open semantics (`start >= 0`, `end > start`, `width == end - start`).
  - Total genome coverage equals `peaks["width"].sum()` when peaks are disjoint/merged.

## 4. Gotchas & Troubleshooting

1. **BED / `narrowPeak` 0-Based Half-Open Coordinates vs. 1-Based GFF/VCF**:
   - BED intervals are `[start, end)` (0-based start, 1-based end), so peak width is strictly `end - start` (never `end - start + 1`). Adding `+1` off-by-one shifts summit coordinates and motif extraction windows.
2. **ENCODE Blacklist Regions Cause Massive False-Positive Peaks**:
   - Centromeric/pericentromeric satellites (`chrM`, ENCODE v2 blacklist) accumulate collapses of multi-copy repeats with apparent $-\log_{10}(q) > 1000$. Always filter against the ENCODE blacklist (`bedtools intersect -v`) and remove `chrM` reads before computing FRiP (Fraction of Reads in Peaks).
3. **Tn5 Offset (`+4 bp` / `-5 bp`) in ATAC-seq Footprinting**:
   - The Tn5 homodimer binds as a 9-bp staggered dimer. For TF footprinting (`TOBIAS`, `HINT-ATAC`), shift `+` strand reads by `+4 bp` and `-` strand reads by `-5 bp` to center on the exact transposase cleavage midpoint.
4. **Effective Genome Size (`-g`) Must Match Assembly and Read Mappability**:
   - Using `-g hs` ($2.7 	imes 10^9$) on mouse (`-g mm`, $1.87 	imes 10^9$) or Drosophila (`-g dm`, $1.2 	imes 10^8$) skews the MACS3 global lambda background and invalidates $q$-values.

## Dependencies

`numpy`, `pandas`, `matplotlib`
