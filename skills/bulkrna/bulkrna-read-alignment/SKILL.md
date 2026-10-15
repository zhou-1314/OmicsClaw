---
name: bulkrna-read-alignment
description: Load when summarising STAR / HISAT2 / Salmon alignment-rate logs in bulk RNA-seq. Skip when
  data is raw FASTQ (use bulkrna-read-qc); already counted (use bulkrna-qc); genome-DNA alignment (use
  genomics-alignment).
trigger: RNA-seq alignment, STAR, HISAT2, Salmon, mapping rate, read alignment, alignment QC
tags:
- bulkrna
- alignment
- STAR
- HISAT2
- Salmon
- mapping-rate
- strandedness
---

# Bulk RNA-seq Read Alignment & Log QC (`bulkrna-read-alignment`)

## 1. Method Decision Matrix & When to Use

Choose the aligner or pseudo-aligner according to downstream requirements (splice-junction discovery vs. rapid transcript quantification) and parse its summary log:

| Upstream Aligner | Log File Format | Primary Use Case | Key Metrics Extracted |
|---|---|---|---|
| **STAR** (`STAR --runMode alignReads`) | `Log.final.out` (key-value text) | Gold-standard splice-aware genome alignment for DE (`featureCounts`/`HTSeq`) and splicing (`rMATS`) | `total_reads`, `uniquely_mapped`, `multi_mapped`, `unmapped`, `unique_rate` |
| **HISAT2** (`hisat2 --new-summary`) | Standard error alignment summary text | Low-memory FM-index splice-aware genome alignment | Concordant unique pairs, concordant multi-mapped pairs, overall alignment rate |
| **Salmon** (`salmon quant --validateMappings`) | `aux_info/meta_info.json` (JSON) | Fast selective-alignment transcript quantification (`tximport` $\rightarrow$ `DESeq2` / `SUPPA2`) | `num_processed`, `num_mapped`, `percent_mapped` (total transcriptome mapping rate) |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Idiomatic CLI Execution & Python Log Parsing for STAR / Salmon

```bash
# 1A. Splice-aware genome alignment with STAR (2-pass mode for novel junctions)
STAR --genomeDir /ref/star_grch38 \
     --readFilesIn sample_R1.fastq.gz sample_R2.fastq.gz \
     --readFilesCommand zcat \
     --twopassMode Basic \
     --outSAMtype BAM SortedByCoordinate \
     --quantMode GeneCounts \
     --runThreadN 8 \
     --outFileNamePrefix star_out/sample_

# 1B. Selective alignment transcript quantification with Salmon
salmon quant -i /ref/salmon_gencode_v44 -l A \
             -1 sample_R1.fastq.gz -2 sample_R2.fastq.gz \
             --validateMappings --gcBias -p 8 -o salmon_out/sample
```

### Pattern B: Parse `STAR` / `HISAT2` / `Salmon` Logs via `use_skill("bulkrna-read-alignment")`

```python
from pathlib import Path
# Calling use_skill("bulkrna-read-alignment") auto-injects kernel.py helpers into the python kernel:
log_text = Path("star_out/sample_Log.final.out").read_text()
stats_df = parse_alignment_log(log_text, method="star")
qc_verdict = evaluate_mapping_qc(stats_df)
```

## 3. Output Format & Verification Contract

- **Alignment Summary Table** (`tables/alignment_stats.csv`):
  - Columns: `aligner`, `total_reads`, `uniquely_mapped`, `multi_mapped`, `unmapped`, `unique_rate`, `mapped_rate`, `qc_status`.
- **Diagnostic Figures**:
  - `figures/mapping_summary.png` and `figures/alignment_composition.png`.
- **Verification Invariants**:
  - Read conservation: `uniquely_mapped + multi_mapped + unmapped == total_reads` (within rounding of reported log counts).
  - Rate bounds: $0.0 \le \text{unique\_rate} \le \text{mapped\_rate} \le 100.0$.

## 4. Gotchas & Troubleshooting

1. **Salmon `percent_mapped` vs. STAR `Uniquely mapped reads %`**:
   - Salmon quantifies isoform expectations via an EM algorithm over multi-mapping equivalence classes, so `meta_info.json` reports **total mapped reads** (`num_mapped`), not uniquely mapped reads. Never compare Salmon's `mapped_rate` directly to STAR's `unique_rate` without accounting for multi-isoform reads.
2. **Low Unique Mapping Rate ($< 60\%$) Diagnostics**:
   - In STAR, if `Uniquely mapped reads %` is low while `% of reads mapped to multiple loci` is high ($> 20\%$), suspect ribosomal RNA (rRNA) depletion failure. If `% of reads unmapped: too short` dominates, check for adapter dimers, over-trimming, or reference genome species mismatch.
3. **Library Strandedness Verification (`ReadsPerGene.out.tab`)**:
   - STAR's `--quantMode GeneCounts` outputs 4 columns: `gene_id`, `unstranded`, `stranded_first`, `stranded_second`. For standard Illumina TruSeq dUTP / Watchmaker reverse-stranded kits (`ISR`), column 4 (`stranded_second`) should have $\sim 5\text{--}10\times$ more counts than column 3. Selecting the wrong column discards $> 85\%$ of valid reads.
4. **Gene-Body $5'\rightarrow 3'$ Coverage Bias Requires BAM Inspection**:
   - Alignment summary logs (`Log.final.out` / `meta_info.json`) do not contain $5'\rightarrow 3'$ transcript coverage profiles. Run `RSeQC` (`geneBody_coverage.py`) on the coordinate-sorted BAM to detect $3'$ poly(A)-capture degradation.

## Dependencies

`matplotlib`, `numpy`, `pandas`
