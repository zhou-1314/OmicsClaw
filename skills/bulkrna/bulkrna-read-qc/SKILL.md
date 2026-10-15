---
name: bulkrna-read-qc
description: Load when checking raw FASTQ quality (Phred / GC / adapter / Q20-Q30) before alignment in
  bulk RNA-seq. Skip when reads are already aligned (use bulkrna-read-alignment); counted (use bulkrna-qc);
  single-cell FASTQ (use sc-fastq-qc).
trigger: FASTQ QC, read quality, Phred, FastQC, adapter, GC content, Q20, Q30
tags:
- bulkrna
- FASTQ
- QC
- Phred
- GC-content
- adapter
- read-quality
---

# Bulk RNA-seq Raw FASTQ Read Quality Control (`bulkrna-read-qc`)

## 1. Method Decision Matrix & When to Use

Select the raw FASTQ quality control and preprocessing tool before read alignment (`STAR` / `HISAT2` / `Salmon`):

| Stage / Task | Recommended Tool | Output | Why |
|---|---|---|---|
| **All-in-one QC + adapter/polyG trimming** (Production standard) | `fastp` (`fastp -i R1.fq.gz -I R2.fq.gz --detect_adapter_for_pe`) | Trimmed FASTQ + `fastp.json` + `fastp.html` | Ultra-fast C++ streaming pass that auto-detects Illumina adapters, trims NovaSeq polyG tails, and outputs machine-readable JSON QC |
| **Standalone diagnostic visual reporting** across many samples | `FastQC` + `MultiQC` | `*_fastqc.html` + `multiqc_report.html` | Aggregates per-base Phred quality, GC distribution, duplication levels, and overrepresented sequences across cohorts |
| **Lightweight Python FASTQ sampling & Phred+33 verification** | `bulkrna-read-qc` kernel (`read_fastq_sample` + `compute_fastq_qc`) | `tables/qc_summary.csv` | Fast in-memory inspection of the leading $N$ reads without external binary dependencies |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Idiomatic `fastp` / `FastQC` CLI Workflow

```bash
# 1. Run fastp for paired-end QC, automatic adapter removal, and NovaSeq polyG trimming
fastp -i raw/sample_R1.fastq.gz -I raw/sample_R2.fastq.gz \
      -o clean/sample_R1.trimmed.fq.gz -O clean/sample_R2.trimmed.fq.gz \
      --detect_adapter_for_pe --trim_poly_g \
      --qualified_quality_phred 20 --length_required 36 \
      --json qc/sample_fastp.json --html qc/sample_fastp.html --thread 8

# 2. Aggregate cohort-wide QC reports with MultiQC
multiqc qc/ -o qc/multiqc_report
```

### Pattern B: Python FASTQ Sampling & Phred+33 Metrics via `use_skill("bulkrna-read-qc")`

```python
# Calling use_skill("bulkrna-read-qc") auto-injects kernel.py helpers into the python kernel:
reads_df = read_fastq_sample("raw/sample_R1.fastq.gz", max_reads=100_000)
qc_summary = compute_fastq_qc(reads_df)
```

## 3. Output Format & Verification Contract

- **FASTQ Quality Summary Table** (`tables/qc_summary.csv`):
  - Columns: `total_reads`, `mean_length`, `mean_quality`, `q20_rate`, `q30_rate`, `gc_content`, `adapter_rate`.
- **Diagnostic Figures**:
  - `figures/per_base_quality.png`, `figures/gc_content.png`, `figures/read_length_distribution.png`, `figures/quality_score_distribution.png`.
- **Verification Invariants**:
  - Every FASTQ record satisfies `len(sequence) == len(quality)`.
  - Phred+33 ASCII scores lie in `[33, 126]` (Phred $Q \in [0, 93]$); `q30_rate <= q20_rate <= 100.0`.

## 4. Gotchas & Troubleshooting

1. **NovaSeq / NextSeq 2-Color Chemistry Poly-G Artifacts**:
   - Illumina 2-color sequencers (NovaSeq 6000/X, NextSeq) encode "no fluorescence" as `G` with high Phred confidence when clusters reach the end of short inserts. This creates artificial `GGGGGGGGGG` tails and skews `gc_content` upward. Always enable `--trim_poly_g` in `fastp`.
2. **Bimodal or Shifted GC Content Distribution**:
   - Human/mouse mRNA-seq typically exhibits a unimodal GC distribution centered around $48\text{--}52\%$. A sharp secondary peak at $> 60\%$ or $< 35\%$ indicates rRNA contamination, adapter dimers, or bacterial/mycoplasma contamination.
3. **5' Random Hexamer Priming Bias vs. Adapter Read-Through**:
   - In standard dUTP stranded RNA-seq, the first $10\text{--}12\text{ bp}$ at the $5'$ end always show skewed per-base sequence content due to random hexamer priming; **do not** hard-clip the $5'$ end, as this is technical priming preference rather than foreign sequence. Trim $3'$ Illumina adapters (`AGATCGGAAGAGC`) instead.
4. **Phred+33 vs. Legacy Phred+64 Encoding**:
   - Modern Illumina (CASAVA $\ge 1.8$) uses Phred+33 (`ord(char) - 33`, ASCII `!` to `I`). Legacy Solexa/Illumina 1.3–1.7 FASTQs from older GEO submissions use Phred+64 (`ord(char) - 64`, ASCII `@` to `h`), which will inflate computed `mean_quality` above 50 if decoded as Phred+33.

## Dependencies

`matplotlib`, `numpy`, `pandas`
