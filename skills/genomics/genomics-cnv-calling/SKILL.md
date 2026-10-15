---
name: genomics-cnv-calling
description: Load when calling CNV segments via CBS-style segmentation on a bin-level log2-ratio CSV
  from exome / WGS coverage — emits 5-class CN state (`amplification` / `gain` / `neutral` /
  `loss`) and genome-fraction summary. Skip when working with single-cell / spatial CNV (use
  spatial-cnv).
trigger: CNV, copy number, amplification, deletion, CNVkit
tags:
- genomics
- cnv
- copy-number
- cbs
- segmentation
- cnvkit
- gatk-gcnv
---

# Copy Number Variation Segmentation & Calling (`genomics-cnv-calling`)

## 1. Method Decision Matrix & When to Use

Select the CNV segmentation algorithm and copy-ratio framework according to sequencing assay and tumor/normal pairing:

| Sequencing Design | Recommended Tool | Segmentation Algorithm | Why |
|---|---|---|---|
| **Hybrid-capture WES / targeted panel** (tumor-normal or panel of normals) | **CNVkit** (`cnvkit.py batch`) | Circular Binary Segmentation (**CBS**; `DNAcopy`) or HMM | Corrects GC content, target edge effects, and off-target background reads simultaneously |
| **Matched Tumor-Normal WGS / WES** (allele-specific purity/ploidy) | **GATK ModelSegments** / **ASCAT** / **FACETS** | Joint log2 copy-ratio + B-allele frequency (BAF) segmentation | Resolves copy-neutral LOH, subclonal gains/losses, and absolute integer copy number |
| **Low-pass WGS** ($0.1\times - 1\times$ cfDNA / single-cell) | **ichorCNA** / **QDNAseq** | HMM on GC/mappability-normalized fixed genomic bins | Robust on ultra-low depth uniform genomic windows |
| **Pre-binned `log2_ratio` table** | **CBS change-point segmentation** | Recursive binary splitting (`method="cbs"`) | Groups adjacent genomic bins into contiguous segments and assigns diploid-relative CN states |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: `CNVkit` CLI + Python Segment Inspection

```python
import numpy as np
import pandas as pd

# 1. Run CNVkit batch pipeline on tumor/normal BAMs (CLI):
# cnvkit.py batch tumor.bam --normal normal.bam --targets targets.bed --fasta ref.fa --output-dir cnv_out/

# 2. Load bin-level log2 copy ratios (.cnr / CSV) and convert segment log2_ratio to integer CN
bins = pd.read_csv("data/cnv_bins.csv")  # columns: chrom, start, end, log2_ratio
# Diploid baseline: CN = 2 * 2^(log2_ratio)
bins["estimated_cn"] = np.clip(np.round(2.0 * (2.0 ** bins["log2_ratio"])), 0, None).astype(int)
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("genomics-cnv-calling")`)

```python
# Calling use_skill("genomics-cnv-calling") auto-injects kernel.py helpers into the python kernel:
bins = read_bins("data/cnv_bins.csv")
segments = analyze(bins, method="cbs", alpha=0.01)
summary = cnv_summary(segments)
```

## 3. Output Format & Verification Contract

- **Segmented Copy Number Table** (`tables/cnv_segments.csv`):
  - Contains `chrom`, `start`, `end`, `n_bins`, `log2_ratio` (segment mean), `estimated_cn` ($	ext{round}(2 \cdot 2^{	ext{log2\_ratio}})$), and `cn_state` (`"deletion"` for $	ext{log2\_ratio} < -0.3$, `"duplication"` for $	ext{log2\_ratio} > 0.3$, `"neutral"` otherwise).
- **Per-Chromosome Summary** (`tables/cnv_per_chromosome.csv`):
  - Aggregated segment counts, altered base pairs, and mean copy ratio per chromosome.
- **Verification Invariants**:
  - Segment coordinates are non-overlapping and strictly ordered per chromosome (`start < end`).
  - Sum of `n_bins` across all segments equals total input bins (`segments["n_bins"].sum() == len(bins)`).

## 4. Gotchas & Troubleshooting

1. **Never Segment Across Chromosome Boundaries**:
   - Running change-point detection on the full genome array without grouping by `chrom` merges the q-arm telomere of one chromosome with the p-arm of the next. Always group by `chrom` and sort by `(start, end)` before segmentation.
2. **GC-Content and Mappability Wave Artifacts Mimic Large CNVs**:
   - Uncorrected read depth exhibits kilobase-to-megabase GC waves that trigger false CBS splits. Always normalize bin depth against GC content and a matched Panel of Normals (PoN) before segmentation.
3. **Tumor Purity Attenuates `log2_ratio` Thresholds**:
   - In a pure diploid sample, a single-copy hemizygous deletion ($CN=1$) yields $\log_2(1/2) = -1.0$, and a single-copy gain ($CN=3$) yields $\log_2(3/2) pprox +0.585$. At 40% tumor purity, those signals shrink to $-0.32$ and $+0.26$; adjust thresholds (`[-0.3, +0.3]`) or jointly model BAF and purity.
4. **Sex Chromosomes (`chrX`, `chrY`) Require Ploidy-Aware Baselines**:
   - Comparing a male sample (`XY`) against a mixed-sex diploid reference panel makes `chrX` appear as a whole-chromosome deletion ($	ext{log2\_ratio} pprox -1.0$). Use sex-matched references or ploidy-aware calling (`cnvkit.py call -y`).

## Dependencies

`numpy`, `pandas`, `matplotlib`
