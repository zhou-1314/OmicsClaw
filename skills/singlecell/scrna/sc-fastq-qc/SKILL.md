---
name: sc-fastq-qc
description: Load when checking raw single-cell FASTQ read quality (Phred / GC / adapter / length) before
  counting. Skip when reads are already counted (use sc-qc); bulk FASTQ (use bulkrna-read-qc).
trigger: scRNA FASTQ QC, FastQC single-cell, MultiQC single-cell, raw read quality, read-level QC
tags:
- singlecell
- scrna
- fastq
- qc
- read-quality
---

# Single-Cell Raw FASTQ Read Quality Control (`sc-fastq-qc`)

## 1. Method Decision Matrix & When to Use

Run read-level quality assessment on raw single-cell sequencing FASTQ files (`R1` barcode/UMI + `R2` cDNA transcript) **before** launching compute-intensive alignment (`sc-count`).

| Inspection Layer | Tool / Library | Metrics Computed | Typical Target Threshold | Best Used When |
|---|---|---|---|---|
| **Fast Prefix / Stream Profiling** | Python `gzip` + `numpy` (`kernel.py`) | Per-position Phred quartiles (`mean`, `Q25`, `median`, `Q75`), `%Q30`, `%GC`, read length distribution | `R1` (CB+UMI) `%Q30 > 90%`; `R2` (cDNA) `%Q30 > 75%` | Instant pre-flight check on sampled reads (`max_reads=20000..100000`) without external Java/CLI dependencies |
| **Full-File Per-Read Diagnostics** | `fastqc` + `multiqc` (CLI) | Adapter contamination, overrepresented sequences, per-tile quality, duplication levels | `< 5%` adapter read-through (`PolyA` / Illumina TruSeq) | Comprehensive full-depth sequencing lane validation across multi-sample cohorts |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Paired R1/R2 Read Structure & Phred Quality Profiling in Python

```python
import gzip
import numpy as np
import pandas as pd

def profile_fastq(path: str, max_reads: int = 50000) -> tuple[dict, pd.DataFrame]:
    opener = gzip.open if str(path).endswith(".gz") else open
    quals, gc_bases, total_bases = [], 0, 0
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
        for _ in range(max_reads):
            if not fh.readline():
                break
            seq = fh.readline().rstrip("\r\n")
            fh.readline()
            qual = fh.readline().rstrip("\r\n")
            if seq and qual:
                q_arr = np.frombuffer(qual.encode("ascii"), dtype=np.uint8) - 33
                quals.append(q_arr)
                total_bases += len(seq)
                gc_bases += seq.upper().count("G") + seq.upper().count("C")

    max_len = max(len(q) for q in quals)
    mat = np.full((len(quals), max_len), np.nan, dtype=np.float32)
    for i, q in enumerate(quals):
        mat[i, :len(q)] = q

    summary = {
        "sampled_reads": len(quals),
        "mean_length": float(total_bases / max(len(quals), 1)),
        "gc_pct": float(100.0 * gc_bases / max(total_bases, 1)),
        "q30_pct": float(100.0 * np.nanmean(mat >= 30)),
    }
    per_base = pd.DataFrame({
        "position": np.arange(1, max_len + 1),
        "mean_phred": np.nanmean(mat, axis=0),
        "q25_phred": np.nanpercentile(mat, 25, axis=0),
        "median_phred": np.nanmedian(mat, axis=0),
        "q75_phred": np.nanpercentile(mat, 75, axis=0),
    })
    return summary, per_base
```

### Pattern B: CLI Batch Execution with `FastQC` + `MultiQC`

```bash
mkdir -p artifacts/fastqc artifacts/multiqc
fastqc --threads 4 --outdir artifacts/fastqc sample_R1.fastq.gz sample_R2.fastq.gz
multiqc artifacts/fastqc --outdir artifacts/multiqc
```

### Optional Micro-Helper (`kernel.py`)

Calling `use_skill("sc-fastq-qc")` automatically loads `kernel.py` helpers into the persistent Python kernel:
- `summarize_fastq(fastq_path, max_reads=20000)`
- `per_base_quality_table(fastq_path, max_reads=20000)`
- `fastq_qc_summary(summaries)`

## 3. Output Format & Verification Contract

- **Summary Tables** (`tables/fastq_per_file_summary.csv`, `tables/fastq_per_sample_summary.csv`, `tables/fastq_per_base_quality.csv`):
  - Per-file columns: `file`, `sampled_reads`, `mean_length`, `gc_pct`, `q30_pct`.
  - Per-base columns: `position` ($1 \dots L$), `mean_phred`, `q25_phred`, `median_phred`, `q75_phred`.
- **Verification Invariants**:
  - Confirm `sampled_reads > 0` and `0.0 <= q30_pct <= 100.0`.
  - Confirm Read 1 length matches expected 10x barcode+UMI geometry ($26$ bp for `10xv2`, $28$ bp for `10xv3`, or full paired read length if untrimmed).

## 4. Gotchas & Troubleshooting

1. **Read 1 (Barcode + UMI) vs. Read 2 (cDNA) Asymmetry in 10x Libraries**:
   - In 10x Chromium 3' and 5' libraries, **Read 1** encodes the synthetic cell barcode + UMI ($26\text{--}28$ bp) and naturally exhibits high sequence duplication and non-uniform per-base nucleotide composition—`FastQC` will flag Read 1 with "Per base sequence content" and "Sequence Duplication Levels" warnings, which are **normal and expected** for barcode reads.
2. **Poly(A) / TSO Tails Lower 3' Quality in Read 2**:
   - Read 2 sequences the biological cDNA transcript ($90\text{--}150$ bp) and frequently reads into poly(A) tails or template-switching oligos (TSO), causing a dip in Phred quality near bases $75\text{--}150$. Splice-aware aligners (`STARsolo` / `Cell Ranger`) soft-clip these tails automatically; do **not** aggressively length-trim Read 1 or you will destroy the fixed-offset UMI!
3. **Read Pair Count Conservation (`n_reads(R1) == n_reads(R2)`)**:
   - Paired-end single-cell counters (`cellranger`, `STARsolo`, `kb-python`) require exact 1-to-1 synchronization between `R1` and `R2` FASTQ files. Never run independent read-dropping filters on `R1` and `R2` separately.

## Dependencies

Python packages this skill's script needs. They are not installed for you — check before a long run.

`anndata`, `matplotlib`, `numpy`, `pandas`, `scanpy`, `scipy`, `seaborn`
