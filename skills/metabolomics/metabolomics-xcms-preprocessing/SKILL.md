---
name: metabolomics-xcms-preprocessing
description: Load when exercising the CLI and replay pipeline with a synthetic LC-MS peak table. Skip
  real mzML preprocessing (run XCMS externally); table peak picking (use
  metabolomics-peak-detection).
trigger: xcms demo, synthetic metabolomics peaks
tags:
- metabolomics
- demo
- cli-only
---

# LC-MS XCMS Preprocessing & Synthetic Peak Table Generation (`metabolomics-xcms-preprocessing`)

## 1. Method Decision Matrix & When to Use

Distinguish between **real raw `mzML` LC-MS preprocessing** (run externally in R `xcms` / `pyopenms`) and **synthetic demo peak-table generation** (this skill's Python helper):

| Workflow Stage | Upstream Production Tool (R `xcms` v4 / `pyopenms`) | Key Algorithm & Parameters | Role of This Skill (`metabolomics-xcms-preprocessing`) |
|---|---|---|---|
| **1. Chromatographic Peak Picking** | `xcms::findChromPeaks(raw, CentWaveParam(ppm=10, peakwidth=c(5, 30), snthresh=10))` | Continuous Wavelet Transform (CWT) on mass-trace ROIs | Simulates parameter-responsive synthetic peak tables (`--demo`) for pipeline replay; **rejects real `mzML` input** |
| **2. Retention Time Alignment** | `xcms::adjustRtime(xdata, ObiwarpParam(binSize=0.6))` or `PeakGroupsParam` | Non-linear dynamic programming warping of RT drift across runs | Outputs synthetic `rt`, `rtmin`, `rtmax` columns |
| **3. Peak Correspondence (Grouping)** | `xcms::groupChromPeaks(xdata, PeakDensityParam(sampleGroups=..., bw=5, minFraction=0.5))` | Kernel density estimation across aligned retention times | Outputs `mz`, `mzmin`, `mzmax`, `into`, `maxo`, and `sample_1..sample_5` |
| **4. Missing-Peak Gap Filling** | `xcms::fillChromPeaks(xdata, ChromPeakAreaParam())` | Integrates raw EIC signal in target `[mzmin, mzmax] x [rtmin, rtmax]` boxes | Hands off `tables/peak_table.csv` to `metabolomics-quantification` |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Production R `xcms` v4 (`CentWave` -> `Obiwarp` -> `PeakDensity` -> `fillChromPeaks`)

```r
library(xcms)
library(Spectra)

# 1. Read centroided mzML files and run CentWave chromatographic peak detection
raw_data <- readMsExperiment(mzml_files, sampleData = sample_meta)
cwp <- CentWaveParam(ppm = 10, peakwidth = c(5, 30), snthresh = 10, prefilter = c(3, 1000))
xdata <- findChromPeaks(raw_data, param = cwp)

# 2. Align retention times with Obiwarp, group features across samples, and gap-fill missing peaks
xdata <- adjustRtime(xdata, param = ObiwarpParam(binSize = 0.6))
pdp <- PeakDensityParam(sampleGroups = sample_meta$group, bw = 5, minFraction = 0.5, binSize = 0.025)
xdata <- groupChromPeaks(xdata, param = pdp)
xdata <- fillChromPeaks(xdata, param = ChromPeakAreaParam())

# 3. Export feature definitions + integrated peak areas (into) to CSV for downstream Python skills
feat_vals <- featureValues(xdata, value = "into", method = "medret")
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("metabolomics-xcms-preprocessing")`)

```python
# Calling use_skill("metabolomics-xcms-preprocessing") auto-injects kernel.py helpers into the python kernel:
peak_table = simulate_peak_table(ppm=15.0, peakwidth_min=5.0, peakwidth_max=30.0, n_samples=5, seed=42)
summary = peak_table_summary(peak_table)
```

## 3. Output Format & Verification Contract

- **Synthetic XCMS-Style Peak Table** (`tables/peak_table.csv`):
  - Contains `feature_id` (`FT0001`...), `mz`, `mzmin`, `mzmax`, `rt`, `rtmin`, `rtmax`, `into` (integrated peak area), `maxo` (maximum peak height), and `sample_1` ... `sample_5` intensity columns.
- **Summary Envelope** (`result.json` / `df.attrs["run_info"]`):
  - Records `n_samples`, `n_peaks`, `mz_min`, `mz_max`, `rt_min`, `rt_max`, and `"synthetic_demo": True`.
- **Verification Invariants**:
  - Real `mzML` / `mzXML` / `.raw` paths passed to this skill raise a `ValueError` instructing the caller to run `xcms` or `pyopenms` externally.
  - All generated features satisfy `0 < mzmin <= mz <= mzmax` and `0 <= rtmin <= rt <= rtmax`.

## 4. Gotchas & Troubleshooting

1. **`metabolomics-xcms-preprocessing` Does Not Parse Raw `mzML` Spectra**:
   - This skill is a **deterministic synthetic peak-table generator** (`seed=42`) used to exercise downstream normalization, quantification, and DE pipelines without multi-gigabyte `mzML` files. Never report numbers from `--demo` as real experimental LC-MS measurements.
2. **Real `XCMS` `featureValues(xdata)` Contains `NA` Even After `fillChromPeaks`**:
   - If a sample has zero raw ions inside the target `[mzmin, mzmax] x [rtmin, rtmax]` bounding box, `fillChromPeaks` still returns `NA`. Pass the exported CSV to `metabolomics-quantification` (`impute="min"` or `"knn"`) before running PCA or differential expression.
3. **`into` (Integrated Peak Area) vs. `maxo` (Apex Height)**:
   - In `XCMS`, `into` integrates the chromatographic peak area over time and is more robust for quantification of broad peaks, whereas `maxo` (apex intensity) is less sensitive to baseline integration boundary jitter on low-abundance peaks.

## Dependencies

`numpy`, `pandas`
