---
name: proteomics-ptm
description: Load when summarising PTM sites (phosphorylation, acetylation, ubiquitination) from a
  per-site CSV — site-class assignment (Olsen I/II/III `localization_probability`), per-PTM-type
  counts. Skip when raw spectra are the input; only protein-level abundance is needed (use
  proteomics-quantification).
trigger: PTM, phosphorylation, acetylation, ubiquitination, modification, motif
tags:
- proteomics
- ptm
- phosphorylation
- acetylation
- ubiquitination
- site-localization
---

# Post-Translational Modification (PTM) Site Localization & Analysis (`proteomics-ptm`)

## 1. Method Decision Matrix & When to Use

Select the PTM localization algorithm and confidence tier according to the PTM enrichment workflow:

| PTM Workflow | Upstream Localization Engine | Localization Confidence Metric | Standard Cutoff |
|---|---|---|---|
| **MaxQuant (`Phospho (STY)Sites.txt`, `Acetyl (K)Sites.txt`)** | **Andromeda PTM Score** (Olsen et al., *Cell* 2006) | `Localization prob` ($P \in [0, 1]$) | **Class I**: $P \ge 0.75$; **Class II**: $0.50 \le P < 0.75$; **Class III**: $P < 0.25 - 0.50$ |
| **FragPipe (`MSFragger` + `PTM-Shepherd` / `luCIpher`)** | **PTMProphet** / **Ascore** | Site localization probability ($P \ge 0.75$) or $\text{Ascore} \ge 13$ ($p \le 0.05$) | Retain localized Class I sites ($P \ge 0.75$) for motif/kinase analysis |
| **DIA-NN / Spectronaut PTM** | `PTM.Site.Confidence` (`--var-mod UniMod:21,79.966331,STY`) | Peptidoform site confidence score | `PTM.Site.Confidence >= 0.75` and `PTM.Q.Value <= 0.01` |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Olsen Class I ($P \ge 0.75$) Site Filtering & Protein-Abundance Normalization

```python
import numpy as np
import pandas as pd

# 1. Load site table and assign Olsen et al. localization classes
sites = pd.read_csv("Phospho_STY_Sites.csv")
sites["site_class"] = np.where(
    sites["localization_probability"] >= 0.75, "Class I",
    np.where(sites["localization_probability"] >= 0.50, "Class II", "Class III"),
)
class1 = sites[sites["site_class"] == "Class I"].copy()

# 2. Normalize log2 PTM site changes by matched global protein log2 changes (avoid protein-level confounding)
# net_ptm_log2fc = ptm_log2fc - global_protein_log2fc
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("proteomics-ptm")`)

```python
# Calling use_skill("proteomics-ptm") auto-injects kernel.py helpers into the python kernel:
classified = classify_sites(sites, loc_threshold=0.75)
class1_sites = class_i_sites(classified)
```

## 3. Output Format & Verification Contract

- **Classified PTM Sites Table** (`tables/ptm_sites.csv`):
  - Contains all input PTM site columns (`protein`, `ptm_type`, optional `residue`, `position`, `localization_probability`) plus `site_class` (`"Class I"`, `"Class II"`, `"Class III"`, or `"Unknown"` when `localization_probability` is absent).
- **High-Confidence Class I Subset** (`tables/ptm_class_I_sites.csv`):
  - Subset of rows with `site_class == "Class I"` (`localization_probability >= loc_threshold`).
- **Verification Invariants**:
  - `0.50 <= loc_threshold <= 1.0`.
  - Every site with `localization_probability >= loc_threshold` is labeled `"Class I"`.

## 4. Gotchas & Troubleshooting

1. **Confounding PTM Stoichiometry Changes with Host Protein Expression Changes**:
   - A 4-fold increase in a phosphopeptide (`S473`) may simply reflect a 4-fold transcriptional induction of the total protein rather than kinase activation. Whenever a matched global proteome dataset exists, subtract the host protein's $\log_2\text{FC}$ ($\Delta\log_2\text{PTM}_{\text{net}} = \log_2\text{FC}_{\text{PTM}} - \log_2\text{FC}_{\text{protein}}$) or include total protein abundance as a covariate (`MSstatsPTM`).
2. **Adjacent Ser/Thr/Tyr Ambiguity Without Site-Determining Fragment Ions**:
   - A peptide containing `...SSY...` with one phosphate (+79.9663 Da) requires $b/y$ fragment ions cleaving *between* the candidate residues to localize the modification. Never feed Class II/III (`localization_probability < 0.75`) sites into sequence-logo motif finders (`motif-x`) or Kinase-Substrate Enrichment Analysis (`KSEA`).
3. **MaxQuant Site Multiplicity (`___1`, `___2`, `___3`) Columns**:
   - MaxQuant splits site intensities by the number of modifications on the supporting peptide (`Intensity sample___1`, `___2`, `___3`). Ensure multiplicity columns are flattened or analyzed consistently rather than dropped.

## Dependencies

`numpy`, `pandas`, `matplotlib`
