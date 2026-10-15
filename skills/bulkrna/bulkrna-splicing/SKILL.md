---
name: bulkrna-splicing
description: Load when summarising rMATS / SUPPA2 alternative-splicing output and identifying significant
  differential splicing events. Skip when you only have count-level DE (use bulkrna-de); splicing in single-cell;
  spatial data (currently unsupported).
trigger: alternative splicing, splicing analysis, PSI, rMATS, SUPPA2, exon skipping, differential splicing
tags:
- bulkrna
- splicing
- alternative-splicing
- PSI
- rMATS
- SUPPA2
---

# Bulk RNA-seq Alternative Splicing Analysis (`bulkrna-splicing`)

## 1. Method Decision Matrix & When to Use

Select the upstream differential splicing caller according to input data (`BAM` alignments vs. isoform `TPM` quantifications) and event resolution:

| Input Data & Analytical Goal | Upstream Caller | Event Types Quantified | Why |
|---|---|---|---|
| **Coordinate-sorted STAR BAMs** with replicate groups; classical exon-centric events | **rMATS-turbo** (`rmats.py`) | `SE` (Skipped Exon), `A5SS`, `A3SS`, `MXE` (Mutually Exclusive), `RI` (Retained Intron) | Likelihood-ratio test on junction (`JC`) + exon-body (`JCEC`) counts directly modeling biological replicate variance |
| **Fast transcript-level TPMs** from `Salmon` / `kallisto` | **SUPPA2** (`suppa.py diffSplice`) | `SE`, `A5`, `A3`, `MX`, `RI`, `AF` (Alt First), `AL` (Alt Last) | Translates isoform TPMs into event $\Psi$ (Percent Spliced In) in seconds without reading massive BAM files |
| **Annotation-free *de novo* intron excision clusters** (cryptic splicing) | **LeafCutter** (`leafcutter_ds.R`) | Intron excision clusters (`chr:start:end:clu_N`) | Discovers unannotated cryptic splice sites and disease-specific intron retention without relying on GTF exon definitions |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Idiomatic `rMATS-turbo` CLI & Event Table Standardization

```bash
# 1. Run rMATS-turbo on replicate STAR BAM lists (b1.txt vs b2.txt)
rmats.py --b1 ctrl_bams.txt --b2 treat_bams.txt \
         --gtf gencode.v44.annotation.gtf \
         -t paired --readLength 150 --variable-read-length \
         --nthread 8 --od rmats_out/ --tmp rmats_tmp/
```

```python
import pandas as pd

# 2. Load rMATS JunctionCounts (JC) tables across all 5 canonical event types
event_files = ["SE", "A5SS", "A3SS", "MXE", "RI"]
frames = []
for ev in event_files:
    df = pd.read_csv(f"rmats_out/{ev}.MATS.JC.txt", sep="\t")
    # Filter low-coverage events (< 10 average total junction reads across samples)
    df["event_type"] = ev
    df = df.rename(columns={"geneSymbol": "gene", "IncLevelDifference": "delta_psi", "FDR": "padj", "PValue": "pvalue"})
    frames.append(df[["ID", "gene", "event_type", "delta_psi", "pvalue", "padj"]])

all_events = pd.concat(frames, ignore_index=True)
sig_events = all_events[(all_events["padj"] < 0.05) & (all_events["delta_psi"].abs() > 0.10)]
```

### Pattern B: Filter & Summarize Differential Splicing via `use_skill("bulkrna-splicing")`

```python
# Calling use_skill("bulkrna-splicing") auto-injects kernel.py helpers into the python kernel:
sig_df = filter_splicing_events(all_events, dpsi_cutoff=0.10, padj_cutoff=0.05)
by_type_df = summarize_event_types(sig_df)
```

## 3. Output Format & Verification Contract

- **Standardized Splicing Events** (`tables/splicing_events.csv`):
  - Columns: `gene`, `event_type` (`SE`, `A5SS`, `A3SS`, `MXE`, `RI`), `delta_psi` ($\Delta\Psi \in [-1, 1]$), `padj` ($\in [0, 1]$).
- **Significant Differential Splicing Events** (`tables/significant_events.csv`):
  - Subset satisfying strict $|\Delta\Psi| > 0.10$ and `padj < 0.05`.
- **Verification Invariants**:
  - Every `delta_psi` value lies strictly in $[-1.0, 1.0]$ and `padj` lies in $[0.0, 1.0]$.
  - `len(significant_events) <= len(splicing_events)`.

## 4. Gotchas & Troubleshooting

1. **Always Enforce a Minimum Junction Read Coverage Filter ($\ge 10\text{--}20$ Reads)**:
   - Lowly expressed genes with only 2–3 junction-spanning reads frequently produce extreme $\Delta\Psi = \pm 1.0$ estimates by chance. Sum `IJC_SAMPLE_*` + `SJC_SAMPLE_*` in rMATS and require a mean total junction count $\ge 10$ per condition before ranking hits.
2. **Sign Convention of $\Delta\Psi$ (`IncLevelDifference` in rMATS vs. `dPSI` in SUPPA2)**:
   - In rMATS, `IncLevelDifference` is computed as $\overline{\Psi}_{\text{b1}} - \overline{\Psi}_{\text{b2}}$ (Group 1 minus Group 2), whereas SUPPA2 computes $\overline{\Psi}_{\text{cond2}} - \overline{\Psi}_{\text{cond1}}$ (Condition 2 minus Condition 1). Verify which group is control vs. treatment before reporting exon inclusion (`delta_psi > 0`) vs. skipping (`delta_psi < 0`).
3. **Pooling `SE`, `RI`, `A5SS`, `A3SS`, and `MXE` Tables**:
   - rMATS adjusts `FDR` separately within each of the 5 event classes (`*.MATS.JC.txt`). Keep `event_type` attached to every row when merging tables so isoform coordinates (`exonStart_0base`, `riExonStart_0base`) are not conflated.

## Dependencies

`matplotlib`, `numpy`, `pandas`, `scipy`
