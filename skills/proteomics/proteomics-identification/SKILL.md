---
name: proteomics-identification
description: Load when summarising peptide identifications (PSM counts, unique peptides, score / charge
  distributions) of a peptide-level CSV from MaxQuant / FragPipe / DIA-NN. Skip when raw spectra
  are the input (run the search engine); protein-quantification tables (use proteomics-ms-qc).
trigger: peptide identification, database search, MaxQuant, MS-GF+, Comet, Mascot
tags:
- proteomics
- identification
- peptides
- psm
- maxquant
- msgf
---

# Peptide & PSM Identification Confidence Filtering (`proteomics-identification`)

## 1. Method Decision Matrix & When to Use

Select the peptide-spectrum match (PSM) rescoring and FDR control workflow according to the search engine:

| Search Engine & Acquisition | Confidence Columns | Recommended Rescoring / FDR Control | Standard Threshold |
|---|---|---|---|
| **MaxQuant (`evidence.txt` / `peptides.txt`)** | `PEP` (Posterior Error Prob.) & target-decoy `q-value` | Target-decoy competition + MaxQuant Andromeda score & `PEP` | Global `q-value <= 0.01` (1% FDR) + remove `Reverse == "+"` |
| **FragPipe (`MSFragger` + `Philosopher` / `Percolator`)** | `PeptideProphet Probability` / `q-value` | `MSBooster` deep-learning RT/MS2 prediction + `Percolator` SVM | `q-value <= 0.01` at both PSM, peptide, and protein levels |
| **DIA-NN (`report.tsv`)** | `Q.Value` (precursor), `Global.Q.Value`, `PG.Q.Value` | Neural network target-decoy classifier | `Q.Value <= 0.01` and `PG.Q.Value <= 0.01` |
| **OpenMS / `pyopenms` (`mzIdentML` / `idXML`)** | `Posterior Error Probability_score` / `q-value` | `PercolatorAdapter` + `FalseDiscoveryRate` | 1% global peptide & protein FDR |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Target-Decoy 1% Global FDR Filtering on PSM / Peptide Tables

```python
import pandas as pd

# 1. Load peptide/PSM table (e.g. FragPipe psm.tsv, DIA-NN report.tsv, or MaxQuant peptides.txt)
df = pd.read_csv("psm.tsv", sep="\t")

# 2. Exclude decoy and contaminant hits before summarizing target identifications
if "Is Decoy" in df.columns:
    df = df[~df["Is Decoy"].astype(bool)]

# 3. Filter at 1% global FDR (q-value <= 0.01) and summarize PSMs, stripped peptides, and proteins
conf = df[df["q-value"] <= 0.01]
n_psms = len(conf)
n_peptides = conf["Peptide"].nunique()
n_proteins = conf["Protein"].nunique()
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("proteomics-identification")`)

```python
# Calling use_skill("proteomics-identification") auto-injects kernel.py helpers into the python kernel:
df = read_table("data/peptides.csv")
filtered = filter_identifications(df, fdr_threshold=0.01)
summary = identification_summary(filtered)
```

## 3. Output Format & Verification Contract

- **Filtered Peptide / PSM Table** (`tables/peptides.csv`):
  - Contains confidence-filtered PSM/peptide rows (`qvalue <= fdr_threshold`) with standardized `sequence`, `protein`, `charge`, `score`, and confidence columns.
- **Summary Metrics** (`df.attrs["run_info"]`):
  - Reports `confidence_column`, `fdr_threshold`, `n_input_psms`, `n_retained_psms`, `n_unique_peptides`, `n_unique_proteins`, and (when `n_spectra` is supplied) `id_rate_pct`.
- **Verification Invariants**:
  - All rows in `tables/peptides.csv` satisfy `confidence <= fdr_threshold`.
  - `n_unique_proteins <= n_unique_peptides <= n_retained_psms`.

## 4. Gotchas & Troubleshooting

1. **Posterior Error Probability (`PEP`) vs. Global `q-value` (FDR)**:
   - `PEP` is the **local** probability that a *single* individual PSM is incorrect (conditional on its score), whereas `q-value` is the **expected fraction of false positives across the entire accepted set** up to that threshold. Filtering at `PEP <= 0.01` is vastly more conservative than `q-value <= 0.01` (typically a set filtered at `q-value <= 0.01` includes edge PSMs with `PEP` up to `0.05–0.10`). Always prefer `q-value` over `PEP` for global FDR control.
2. **PSM-Level 1% FDR Inflates to 5–15% FDR at the Protein Level**:
   - True positive PSMs cluster onto the same true abundant proteins, whereas false positive decoy hits scatter randomly across the database. Consequently, 1% PSM FDR accumulates to a much higher error rate at the unique-peptide and protein-group levels unless protein-level FDR (`Picked-FDR` / `PG.Q.Value <= 0.01`) is enforced independently.
3. **Modified Sequence (`_M(Oxidation)_`) vs. Stripped Sequence When Counting Unique Peptides**:
   - Counting unique strings on modified peptide notation counts oxidized methionine or carbamidomethyl variants as distinct peptides. Strip modification brackets (`re.sub(r"[\(\[].*?[\)\]]", "", seq)`) when reporting unique stripped sequences.

## Dependencies

`numpy`, `pandas`, `matplotlib`
