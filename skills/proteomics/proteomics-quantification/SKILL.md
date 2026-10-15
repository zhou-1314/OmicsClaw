---
name: proteomics-quantification
description: Load when computing per-protein abundance from a peptide / PSM table via LFQ (intensity
  summation), iBAQ (intensity / tryptic peptides), or spectral counting. Skip when the input is
  already protein-level (use proteomics-ms-qc); label-based TMT / iTRAQ workflows (search
  upstream first).
trigger: protein quantification, LFQ, TMT, DIA, DIA-NN, Skyline
tags:
- proteomics
- quantification
- lfq
- ibaq
- spectral-counting
---

# Peptide-to-Protein Rollup Quantification (LFQ, iBAQ, Spectral Count) (`proteomics-quantification`)

## 1. Method Decision Matrix & When to Use

Select the peptide-to-protein rollup algorithm according to whether relative inter-sample comparison or absolute intra-sample stoichiometry is needed:

| Quantification Goal | Method (`method=`) | Upstream Algorithm / Formula | Why |
|---|---|---|---|
| **Relative abundance across samples** (DDA / DIA) | **MaxLFQ** / Intensity Rollup (`method="lfq"`) | `iq` (R) / `directLFQ` (Python) / intensity summation | Delayed normalization of pairwise peptide ratios preserves inter-sample fold changes even with missing peptides |
| **Absolute stoichiometry / molar ranking within a sample** | **iBAQ** (`method="ibaq"`) | $\text{iBAQ}_p = \frac{\sum_{i \in p} I_i}{N_{\text{theoretical tryptic peptides}}(6 \le L \le 30)}$ | Normalizes summed intensity by protein size/detectability so 200-kDa and 15-kDa subunits can be compared on a molar scale |
| **Low-precision / AP-MS pull-down screening** | **Spectral Counting** (`method="spectral_count"`) | $\text{PSM count per protein}$ (or length-normalized `NSAF`) | Simple non-parametric count of MS2 identifications when precursor XIC peak integration is unavailable |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: `directLFQ` / `iBAQ` Tryptic Peptide Normalization in Python

```python
import re
import pandas as pd

# 1. Count theoretical observable tryptic peptides (cleave after K/R not followed by P, length 6..30 aa)
def count_tryptic_peptides(seq: str, min_len: int = 6, max_len: int = 30) -> int:
    frags = re.split(r"(?<=[KR])(?!P)", str(seq).upper())
    return sum(1 for f in frags if min_len <= len(f) <= max_len)

# 2. Compute iBAQ = summed precursor intensity / theoretical tryptic peptides
peptides = pd.read_csv("data/peptides.csv")
summed = peptides.groupby("protein", as_index=False)["intensity"].sum()
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("proteomics-quantification")`)

```python
# Calling use_skill("proteomics-quantification") auto-injects kernel.py helpers into the python kernel:
lfq_df = quantify(peptides, method="lfq")
ibaq_df = quantify(peptides, method="ibaq")
```

## 3. Output Format & Verification Contract

- **Protein Abundance Table** (`tables/protein_abundance.csv`):
  - Contains `protein`, `n_peptides` (or PSM count), `abundance`, and `log10_abundance`, sorted descending by `abundance`.
- **Verification Invariants**:
  - `method` is one of `{"lfq", "ibaq", "spectral_count"}`.
  - For `method="ibaq"`, every protein must have a positive theoretical tryptic peptide count (`theoretical_peptides > 0` or computable from `protein_sequence`).

## 4. Gotchas & Troubleshooting

1. **Comparing Raw `LFQ` Intensities Across Different Proteins Within One Sample**:
   - A 250-kDa protein yields 10$\times$ more tryptic peptides than a 20-kDa protein at the exact same molar concentration. `LFQ` is designed strictly for comparing **the same protein across different samples**; use **iBAQ** (or `TPA` / histone proteomic ruler) when comparing the molar abundance of Protein A vs. Protein B within the same sample.
2. **Shared (Razor) Peptides Distort Homologous Isoform Quantification**:
   - Summing shared peptides into multiple paralogs double-counts MS1 precursor signal. Assign shared peptides via parsimony (razor protein with the largest number of unique peptides) or quantify exclusively on unique peptides when resolving paralog-specific regulation.
3. **Keil Rule (`[KR]` followed by `P`) in In-Silico Tryptic Digestion**:
   - Trypsin cleavage is sterically blocked when Lys (`K`) or Arg (`R`) is immediately followed by Proline (`P`). Use regex `(?<=[KR])(?!P)` and restrict to observable mass-spec peptide lengths ($6 \le L \le 30$ aa) when calculating iBAQ denominators.

## Dependencies

`numpy`, `pandas`, `matplotlib`
