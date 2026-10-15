---
name: genomics-alignment
description: Load when computing alignment QC metrics (mapping rate, MAPQ, insert size, duplicates,
  proper pairs) from a text SAM file (BWA / Bowtie2 / Minimap2); convert a BAM with samtools
  view -h first. Skip when running the alignment itself; only FASTQ-level QC is needed (use
  genomics-qc).
trigger: alignment, BWA, Bowtie2, Minimap2, map reads
tags:
- genomics
- alignment
- bam
- sam
- bwa
- bowtie2
- minimap2
---

# Short- & Long-Read Alignment QC (`genomics-alignment`)

## 1. Method Decision Matrix & When to Use

Select the read mapper and BAM/SAM post-processing pipeline based on sequencing platform and read length:

| Sequencing Read Type | Recommended Aligner | Upstream CLI / Library | Key Parameters & Why |
|---|---|---|---|
| **Illumina short paired-end reads** (75–300 bp) | **BWA-MEM2** (`bwa-mem2 mem`) | `bwa-mem2` + `samtools sort/markdup` + `pysam` | `-M -t 8 -R "@RG\tID:..."` for GATK-compatible secondary alignment flags and read groups |
| **PacBio HiFi long reads** (10–25 kb, Q20+) | **minimap2** (`-ax map-hifi`) | `minimap2` + `samtools` + `pysam` | Splice-free long-read preset optimized for high-accuracy CCS reads and SV/indel continuity |
| **Oxford Nanopore (ONT) long reads** | **minimap2** (`-ax map-ont`) | `minimap2` + `samtools` + `pysam` | Tolerates higher indel rates and multi-kilobase chimeric split alignments |
| **Post-alignment BAM/SAM inspection** | **SAM flag & MAPQ audit** | `pysam.AlignmentFile` / `samtools flagstat` / `samtools stats` | Quantifies primary mapping rate, properly paired rate (`flag & 0x2`), PCR duplicates (`0x400`), and insert size (`TLEN`) |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: `bwa-mem2` + `samtools` + `pysam` Alignment & BAM Audit

```python
import subprocess
import pysam
import pandas as pd

# 1. Align paired-end FASTQ with bwa-mem2 and coordinate-sort via samtools
cmd = (
    'bwa-mem2 mem -t 8 -R "@RG\\tID:S1\\tSM:Sample1\\tPL:ILLUMINA" '
    'ref/genome.fa data/R1.fq.gz data/R2.fq.gz '
    '| samtools fixmate -m - - | samtools sort -@ 4 - | samtools markdup - sorted.bam && '
    'samtools index sorted.bam'
)
# subprocess.run(cmd, shell=True, check=True)

# 2. Inspect alignment records with pysam (exclude secondary 0x100 & supplementary 0x800 for primary stats)
rows = []
with pysam.AlignmentFile("sorted.bam", "rb") as bam:
    for r in bam.fetch(until_eof=True):
        if r.flag & 0x900:  # Skip secondary (0x100) and supplementary (0x800)
            continue
        rows.append({
            "qname": r.query_name,
            "flag": r.flag,
            "rname": r.reference_name or "*",
            "pos": r.reference_start + 1,  # Convert 0-based pysam to 1-based SAM
            "mapq": r.mapping_quality,
            "tlen": r.template_length,
        })
records_df = pd.DataFrame(rows)
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("genomics-alignment")`)

```python
# Calling use_skill("genomics-alignment") auto-injects kernel.py helpers into the python kernel:
records = read_records("data/alignments.sam")
stats_df = analyze(records)
summary = alignment_summary(records)
```

## 3. Output Format & Verification Contract

- **Alignment Summary Table** (`tables/alignment_stats.csv`):
  - Contains `total_reads`, `mapped_reads`, `unmapped_reads`, `mapping_rate_pct`, `properly_paired_pct`, `duplicate_rate_pct`, `mean_mapq`, `q20_mapq_pct`, `mean_insert_size`, and `median_insert_size`.
- **Processed Records** (`intermediate/`):
  - DataFrame with `qname`, `flag`, `rname`, `pos`, `mapq`, `tlen`, and bitwise flag decoded fields (`is_mapped`, `is_paired`, `is_proper_pair`, `is_duplicate`).
- **Verification Invariants**:
  - `mapped_reads + unmapped_reads == total_reads` across primary records (`(flag & 0x900) == 0`).
  - `mapping_rate_pct` is bounded in $[0, 100]$, and `mean_insert_size` is computed exclusively over inward-facing properly paired reads (`tlen > 0`).

## 4. Gotchas & Troubleshooting

1. **Secondary (`0x100`) and Supplementary (`0x800`) Alignments Inflate Read Counts**:
   - Chimeric and multi-mapping reads emit multiple BAM records per template. Always filter `(flag & 0x900) == 0` before computing per-read mapping rates, or mapping percentages can exceed 100% of sequenced FASTQ reads.
2. **Coordinate System Mismatch (`pysam` 0-based vs. SAM 1-based)**:
   - `pysam.AlignedSegment.reference_start` is **0-based half-open** (like BED), whereas raw SAM text `POS` and `samtools view` regions are **1-based inclusive**. Add `+1` when exporting `reference_start` to SAM/VCF coordinate tables.
3. **Signed Insert Size (`TLEN`) Averages to Zero if Both Mates Are Included**:
   - In paired-end SAM/BAM files, the leftmost mate has `+TLEN` and the rightmost mate has `-TLEN`. Summing or averaging `TLEN` across all reads yields `0.0`; always filter to `tlen > 0` (and exclude inter-chromosomal chimeras with `abs(tlen) < 100000`) before computing insert-size distributions.
4. **Missing `@RG` (Read Group) Tags Break Downstream Variant Callers**:
   - `GATK HaplotypeCaller`, `Mutect2`, and `bcftools` require valid `@RG` headers (`ID`, `SM`, `PL`). Always inject `-R "@RG\tID:...\tSM:..."` during `bwa-mem2` / `minimap2` alignment rather than patching BAM headers post-hoc.

## Dependencies

`numpy`, `pandas`, `matplotlib`
