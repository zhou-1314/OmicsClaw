---
name: proteomics-structural
description: Load when summarising cross-linking MS (XL-MS) results — intra/inter-protein link split,
  optional FDR filtering, distance-constraint validation against a per-crosslinker max distance.
  Skip when raw spectra are the input (run XlinkX / pLink / xiSEARCH); no XL-MS experiment ran.
trigger: structural proteomics, cross-linking MS, XL-MS, XlinkX, pLink, xiSEARCH
tags:
- proteomics
- structural
- xl-ms
- crosslinking
- dss
- bs3
- dsso
- dsbu
- edc
---

# Cross-Linking Mass Spectrometry (XL-MS) & Distance Constraint Validation (`proteomics-structural`)

## 1. Method Decision Matrix & When to Use

Select the crosslinker spacer arm ceiling ($C_\alpha - C_\alpha$ Euclidean distance limit) and search engine according to experimental chemistry:

| Crosslinker Chemistry | Reactive Residues | Spacer Arm Length | Max $C_\alpha - C_\alpha$ Distance Cutoff | Upstream XL-MS Search Engine |
|---|---|---|---|---|
| **DSS / BS3** (non-cleavable NHS-ester) | Lys–Lys (`K-K`), Ser/Thr/Tyr, N-term | $11.4\text{ \AA}$ | **$30.0\text{ \AA}$** (spacer + $2 \times$ Lys side chains + backbone flexibility) | `pLink 2`, `xiSEARCH` + `xiFDR`, `OpenPepXL` |
| **DSSO / DSBU** (MS-cleavable sulfoxide/urea) | Lys–Lys (`K-K`), Ser/Thr/Tyr | $10.3 - 12.5\text{ \AA}$ | **$30.0\text{ \AA}$** | `XlinkX` (Proteome Discoverer), `MeroX`, `Scout` |
| **EDC** (zero-length carbodiimide) | Lys (`K`) to Asp/Glu (`D/E`) salt bridges | $0.0\text{ \AA}$ (direct amide bond) | **$20.0\text{ \AA}$** | `pLink 2`, `xiSEARCH` |
| **Photo-leucine / SDA** (diazirine photo-XL) | Any residue (non-specific) | $3.9\text{ \AA}$ (SDA) | **$20.0 - 25.0\text{ \AA}$** | `xiSEARCH` + `xiFDR` |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Separate Intra/Inter-Protein FDR Filtering & PDB/AlphaFold $C_\alpha - C_\alpha$ Validation

```python
import numpy as np
import pandas as pd

# 1. Load residue-pair (CSM/URP) table and classify intra- vs. inter-protein crosslinks
xl = pd.read_csv("crosslinks.csv")
xl["link_type"] = np.where(xl["protein_a"] == xl["protein_b"], "intra-protein", "inter-protein")

# 2. Validate 3D C-alpha Euclidean distances against DSS/BS3 30.0 Angstrom constraint
max_dist = 30.0  # 20.0 for zero-length EDC
xl["within_constraint"] = xl["distance_angstrom"] <= max_dist
violation_rate = 1.0 - xl["within_constraint"].mean()
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("proteomics-structural")`)

```python
# Calling use_skill("proteomics-structural") auto-injects kernel.py helpers into the python kernel:
res_df = analyse_crosslinks(xl, fdr_threshold=0.05, crosslinker="DSS")
inter_df = inter_protein_links(res_df)
```

## 3. Output Format & Verification Contract

- **Classified Crosslinks Table** (`tables/crosslinks.csv`):
  - Contains `protein_a`, `protein_b`, `link_type` (`"intra-protein"` or `"inter-protein"`), optional `fdr`, `distance_angstrom`, and `constraint_status` (`"satisfied"`, `"violated"`, or `"unchecked"`).
- **Inter-Protein Subset** (`tables/inter_protein_crosslinks.csv`):
  - Subset of heteromeric/inter-protein crosslinks (`link_type == "inter-protein"`).
- **Verification Invariants**:
  - Crosslinker max distance ceilings are `30.0` Å for `DSS`, `BS3`, `DSSO`, `DSBU` and `20.0` Å for `EDC`.
  - When `fdr` is present, all retained rows satisfy `fdr <= fdr_threshold`.

## 4. Gotchas & Troubleshooting

1. **Inter-Protein vs. Intra-Protein Crosslinks Require Separate Target-Decoy FDR Estimation (`xiFDR`)**:
   - The search space for inter-protein pairs scales quadratically ($O(N_{\text{proteins}}^2)$), whereas intra-protein links scale linearly ($O(N_{\text{proteins}})$). Pooling intra- and inter-protein links into a single global 5% FDR list results in **20–50% actual false positives among the inter-protein (PPI) links**. Always compute FDR separately for self-links vs. heteromeric links (`xiFDR`).
2. **Over-LengthViolations ($>30\text{ \AA}$ for DSS/BS3) Can Reflect Homodimer Symmetry or Conformational Dynamics**:
   - When mapping crosslinks onto a monomeric PDB/AlphaFold structure, an apparent $45\text{ \AA}$ "intra-protein" violation (`protein_a == protein_b`) frequently spans across the symmetric interface of a homodimer or reflects an open/closed conformational transition rather than a false identification.
3. **Ambiguous Peptide-Level vs. Residue-Pair (URP) Reporting**:
   - Ensure FDR is controlled at the **Unique Residue Pair (URP)** level rather than raw cross-linked spectrum matches (CSMs) before counting structural restraints for `HADDOCK` / `AlphaLink2` integrative modeling.

## Dependencies

`numpy`, `pandas`, `matplotlib`
