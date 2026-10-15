---
name: metabolomics-peak-detection
description: Load when running per-sample peak picking on a feature × intensity table via
  `scipy.signal.find_peaks` — emits per-(sample, feature) peaks with prominence and width. Skip
  when working with mz / RT raw scans (use metabolomics-xcms-preprocessing); only normalising
  (use metabolomics-quantification).
trigger: peak detection, feature detection, XCMS, MZmine, MS-DIAL, peak picking
tags:
- metabolomics
- peak-detection
- find-peaks
- xcms
- mzmine
---

# Chromatographic Peak Detection & Feature Filtering (`metabolomics-peak-detection`)

## 1. Method Decision Matrix & When to Use

Select the peak-picking engine according to whether raw centroided `mzML` spectra or pre-extracted chromatogram/feature tables are analyzed:

| Input Data Format | Recommended Algorithm | Upstream Package | Key Parameters |
|---|---|---|---|
| **Raw centroided LC-MS `mzML` files** | **CentWave** (continuous wavelet transform on ROI EICs) | R `xcms` (`CentWaveParam`) | `ppm = 5–15`, `peakwidth = c(5, 30)` s (UPLC) or `c(10, 60)` s (HPLC), `snthresh = 10` |
| **Raw `mzML` in Python / C++** | **FeatureFinderMetabo** (mass trace + isotope model fitting) | `pyopenms` (`MassTraceDetection` + `ElutionPeakDetection`) | `mass_error_ppm = 10.0`, `chrom_fwhm = 10.0`, `chrom_peak_snr = 5.0` |
| **High-throughput GUI / batch CLI** | **ADAP Chromatogram Builder + Local Minimum Resolver** | `MZmine 3 / 4` | `min_highest_intensity`, `peak_duration`, MS2 pairing |
| **Pre-tabulated `mz` / `rt` intensity profile** (this skill) | **1D Local Maxima & Prominence Filtering** (`scipy.signal.find_peaks`) | `scipy.signal.find_peaks` | `prominence = 10000.0`, `height`, `distance` (row index separation along RT-sorted profile) |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: `pyopenms` Raw `mzML` Mass Trace & Elution Peak Detection

```python
import pyopenms as oms

# 1. Load centroided LC-MS mzML file
exp = oms.MSExperiment()
oms.MzMLFile().load("sample1.mzML", exp)

# 2. Detect mass traces (extracted ion chromatograms within 10 ppm)
mtd = oms.MassTraceDetection()
mtd_params = mtd.getDefaults()
mtd_params.setValue("mass_error_ppm", 10.0)
mtd_params.setValue("noise_threshold_int", 1000.0)
mtd.setParameters(mtd_params)
mass_traces = []
mtd.run(exp, mass_traces, 0)

# 3. Deconvolve chromatographic elution peaks along each mass trace
epd = oms.ElutionPeakDetection()
split_traces = []
epd.detectPeaks(mass_traces, split_traces)
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("metabolomics-peak-detection")`)

```python
# Calling use_skill("metabolomics-peak-detection") auto-injects kernel.py helpers into the python kernel:
peaks_df = detect_peaks(profile_df, prominence=10000.0, distance=5)
summary = peak_summary(peaks_df)
```

## 3. Output Format & Verification Contract

- **Detected Peaks Table** (`tables/detected_peaks.csv`):
  - Sorted by retention time (`rt`), preserving `mz`, `rt`, sample intensity columns, `mean_intensity`, and `prominence` for local apex rows passing `scipy.signal.find_peaks`.
- **Verification Invariants**:
  - Input table must include `mz`, `rt`, and at least one numeric sample/intensity column.
  - Empty peak results preserve the exact input column schema plus `mean_intensity` and `prominence`.

## 4. Gotchas & Troubleshooting

1. **Profile-Mode vs. Centroided `mzML` Spectra**:
   - `CentWave` (`XCMS`), `FeatureFinderMetabo` (`pyopenms`), and `ADAP` (`MZmine`) strictly require **centroided** MS1 spectra. Running them on raw profile-mode Gaussian data splits a single mass peak into dozens of adjacent $m/z$ traces and exhausts RAM. Always centroid upstream (`msconvert --filter "peakPicking true 1-"`).
2. **Matching `peakwidth` to UPLC vs. Standard HPLC Chromatography**:
   - Sub-2-$\mu$m UPLC columns produce narrow 3–15 s peaks, whereas standard 5-$\mu$m HPLC columns or HILIC methods produce 15–60 s peaks. Setting `peakwidth` too wide merges closely eluting isomers (e.g., leucine and isoleucine); setting it too narrow splits tailing peaks into multiple fragments.
3. **Row-Index `distance` vs. Physical Seconds in 1D Table Peak Finding**:
   - In `detect_peaks` on tabular profiles, `distance` specifies the minimum separation in **RT-sorted row positions** (`scipy.signal.find_peaks`), not physical seconds or minutes. Scale `distance` according to your MS1 scan acquisition rate (e.g., 2–5 Hz).

## Dependencies

`numpy`, `pandas`, `scipy`, `matplotlib`
