---
name: metabolomics-annotation
description: Load when matching LC-MS m/z features to an explicit local metabolite reference within a ppm
  tolerance; bundled HMDB entries are for explicit demonstrations only. Skip pathway ORA (use
  metabolomics-pathway-enrichment) and spectral matching or online searches (use external SIRIUS / GNPS).
trigger: metabolite annotation, SIRIUS, GNPS, MetFrag, spectral matching, metabolite ID, ppm tolerance
tags:
- metabolomics
- annotation
- hmdb
- demo
- mz-match
---

# Metabolomics MS1 Adduct & Exact-Mass Annotation (`metabolomics-annotation`)

## 1. Method Decision Matrix & When to Use

Select the metabolite annotation tier (Schymanski et al., *Environ. Sci. Technol.* 2014 / MSI Levels 1–4) and mass tolerance according to MS instrumentation:

| Evidence Available | MSI / Schymanski Level | Recommended Tool / Library | Mass Tolerance (`ppm`) |
|---|---|---|---|
| **MS1 + MS2 + Authentic Chemical Standard RT** | **Level 1** (Confirmed structure) | In-house RT/MS2 library + `matchms` / `MZmine` | Orbitrap: $\le 3 - 5$ ppm; Q-TOF: $\le 10$ ppm; RT $\Delta \le 0.2$ min |
| **MS1 + Diagnostic MS2 Spectral Library Match** | **Level 2a** (Probable structure) | `matchms` (Modified Cosine / Spec2Vec) against GNPS / MassBank / MoNA / HMDB | Precursor $\le 5 - 10$ ppm + Cosine $\ge 0.7$ ($\ge 3$ matched fragments) |
| **MS1 + In-Silico Fragmentation Tree** | **Level 3** (Tentative candidate class) | `SIRIUS` + `CSI:FingerID` + `CANOPUS` | Precursor + isotope pattern + fragmentation tree |
| **MS1 Exact $m/z$ Adduct Lookup Only** (this skill) | **Level 4 / Putative MS1 match** | Exact neutral mass + electrospray adduct table (`[M+H]+`, `[M+Na]+`, `[M-H]-`, etc.) | $\text{ppm} = \frac{|m/z_{\text{obs}} - m/z_{\text{theo}}|}{m/z_{\text{theo}}} \times 10^6 \le 5 - 10$ ppm |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Electrospray Adduct Theoretical $m/z$ Calculation & `ppm` Window Matching

```python
import numpy as np
import pandas as pd

# Exact monoisotopic adduct mass shifts (Da) for single-charged ESI ions
ADDUCT_SHIFTS = {
    "[M+H]+": 1.007276,
    "[M+Na]+": 22.989218,
    "[M+NH4]+": 18.033823,
    "[M+K]+": 38.963158,
    "[M-H]-": -1.007276,
    "[M+Cl]-": 34.969402,
    "[M+FA-H]-": 44.998201,
}

# Match observed m/z features against reference neutral monoisotopic masses within 10 ppm
obs_mz = features_df["mz"].to_numpy()[:, None]
neutral = reference_df["neutral_mass"].to_numpy()[None, :]
for adduct, shift in ADDUCT_SHIFTS.items():
    theo_mz = neutral + shift
    ppm_err = np.abs(obs_mz - theo_mz) / theo_mz * 1e6
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("metabolomics-annotation")`)

```python
# Calling use_skill("metabolomics-annotation") auto-injects kernel.py helpers into the python kernel:
ref_df = pd.read_csv("data/hmdb_reference.csv")  # columns: name, neutral_mass, database_id, formula
matches = annotate(features_df, reference=ref_df, database="hmdb", ppm=10.0, adducts=["[M+H]+", "[M-H]-"])
summary = annotation_summary(matches)
```

## 3. Output Format & Verification Contract

- **Putative MS1 Annotations Table** (`tables/annotations.csv`):
  - Contains `feature_id`, `query_mz`, `matched_name` (`"Unknown"` when unmatched), `database_id`, `formula`, `adduct`, `theoretical_mz`, `ppm_error`, and `confidence_tier` (`"high_ppm_le_3"`, `"medium_ppm_le_10"`, `"low"`, or `"unmatched"`).
- **Verification Invariants**:
  - Every matched row satisfies `0.0 <= ppm_error <= ppm`.
  - Unmatched query features are preserved with `matched_name == "Unknown"` so feature row counts are never silently dropped.

## 4. Gotchas & Troubleshooting

1. **MS1 Exact Mass Alone Cannot Distinguish Structural Isomers or Isobars**:
   - Glucose, fructose, galactose, and inositol share the exact same chemical formula ($\text{C}_6\text{H}_{12}\text{O}_6$, neutral monoisotopic mass $180.063388$ Da) and have `0.00 ppm` difference across all adducts. Always report MS1-only hits as **MSI Level 3/4 putative annotations** unless confirmed by orthogonal retention time (RT) and MS2 fragmentation.
2. **In-Source Fragmentation & Multiple Adducts Inflate Feature Counts (`1 metabolite -> 10 peaks`)**:
   - A single metabolite frequently generates `[M+H]+`, `[M+Na]+`, `[M+NH4]+`, `[2M+H]+` dimers, and `[M+H-H2O]+` neutral-loss ions at the exact same chromatographic retention time. Group co-eluting features by RT and peak-shape correlation (`CAMERA`, `RAMClustR`, `MZmine IIMN`) before biological interpretation.
3. **Average Molecular Weight vs. Monoisotopic Neutral Mass**:
   - High-resolution Orbitrap/Q-TOF mass spectrometers resolve the $^{12}\text{C}$ monoisotopic peak (`180.0634` Da for glucose), not the periodic-table natural-abundance average molecular weight (`180.156` Da). Using average molecular weight shifts $m/z$ by $>500$ ppm and results in zero matches.

## Dependencies

`numpy`, `pandas`, `matplotlib`
