---
name: genomics-qc
description: Load when running pre-alignment FASTQ quality control — Phred scores, Q20/Q30 rates, GC /
  N content, read-length distribution, adapter detection. Skip when working with already-aligned
  BAMs (use genomics-alignment); peak or variant files are the input (use the relevant
  downstream skill).
trigger: sequencing QC, FastQC, read quality, adapter trimming, fastp
tags:
- genomics
- qc
- fastq
- phred
- adapter
- fastqc
---

# Raw Sequencing Read FASTQ Quality Control (`genomics-qc`)

## 1. Method Decision Matrix & When to Use

Select the read QC profiler and adapter/quality trimmer according to sequencing technology:

| Sequencing Platform | QC & Trimming Tool | Key Thresholds | Why |
|---|---|---|---|
| **Illumina short reads** (WGS, WES, RNA-seq) | **fastp** (`fastp -i R1.fq.gz -I R2.fq.gz`) or **FastQC + MultiQC** | Phred $Q \ge 20$ ($>90\%$), $Q \ge 30$ ($>85\%$), auto-adapter detection | All-in-one C++ preprocessor performing per-cycle Phred QC, polyG tail trimming (NovaSeq/NextSeq 2-color), and JSON/HTML reporting |
| **PacBio HiFi / ONT long reads** | **NanoPlot** / **chopper** / **filtlong** | Mean read $Q \ge 12$ (ONT R10.4) or $Q \ge 20$ (HiFi), min length $\ge 1000$ bp | Evaluates read length N50 vs. mean Phred quality in log-length space |
| **Direct FASTQ stream inspection** | **Phred+33 ASCII decoding** (`ord(c) - 33`) | Error probability $P = 10^{-Q/10}$ | Computes per-read mean Phred quality, Q20/Q30 base fractions, GC content, and ambiguous `N` base rates |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: `fastp` Automated QC & Trimming + JSON Audit

```python
import json
import subprocess

# 1. Run fastp for adapter trimming, NovaSeq polyG removal (-g), and JSON report generation
cmd = [
    "fastp", "-i", "data/R1.fq.gz", "-I", "data/R2.fq.gz",
    "-o", "clean_R1.fq.gz", "-O", "clean_R2.fq.gz",
    "--qualified_quality_phred", "20", "--length_required", "50",
    "--trim_poly_g", "--json", "fastp_qc.json", "--html", "fastp_qc.html",
]
# subprocess.run(cmd, check=True)

# 2. Parse fastp JSON summary metrics
# with open("fastp_qc.json") as f:
#     summary = json.load(f)["summary"]["after_filtering"]
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("genomics-qc")`)

```python
# Calling use_skill("genomics-qc") auto-injects kernel.py helpers into the python kernel:
reads = read_records("data/reads.fastq.gz", max_reads=500000)
metrics_df = analyze(reads, max_reads=500000)
summary = qc_summary(reads)
```

## 3. Output Format & Verification Contract

- **Read-Level & Aggregate QC Table** (`tables/qc_metrics.csv`):
  - Contains per-read `length`, `mean_quality`, `q20_pct`, `q30_pct`, `gc_pct`, and `n_count`, with dataset-wide summary statistics in `attrs["run_info"]`.
- **Per-Base Quality & Length Tables** (`tables/per_base_quality.csv`, `tables/read_length_distribution.csv`):
  - Cycle-wise Phred score quantiles and read length histogram bins.
- **Verification Invariants**:
  - Every FASTQ record satisfies `len(sequence) == len(quality) > 0`.
  - Phred+33 quality scores satisfy $0 \le Q \le 93$ and `q20_pct >= q30_pct`.

## 4. Gotchas & Troubleshooting

1. **Averaging Phred Scores Linearly vs. Error-Probability Space**:
   - Phred scores are logarithmic ($Q = -10 \log_{10} P_{\text{err}}$). A read with half $Q40$ ($P=10^{-4}$) and half $Q10$ ($P=0.1$) has a true mean error probability of $\approx 0.05$ ($Q \approx 13$), whereas a naive arithmetic average gives $Q25$. Be aware of whether downstream tools report arithmetic mean $Q$ or probability-scale mean $Q$.
2. **2-Color Chemistry Poly-G Artifacts (NovaSeq / NextSeq)**:
   - On Illumina 2-channel instruments, "no signal" (dark cycles after short inserts) is called as high-confidence `G`, artificially inflating `gc_pct` and `q30_pct` at read 3' ends. Always enable `--trim_poly_g` in `fastp`.
3. **Legacy Phred+64 vs. Standard Sanger Phred+33 Encoding**:
   - Modern sequencers emit Phred+33 (ASCII `33` = `!` = $Q0$, ASCII `73` = `I` = $Q40$). If minimum ASCII values exceed `64` (`@`) and reach `104` (`h`), the file is legacy Illumina 1.3–1.7 Phred+64 and must be converted before QC.

## Dependencies

`numpy`, `pandas`, `matplotlib`
