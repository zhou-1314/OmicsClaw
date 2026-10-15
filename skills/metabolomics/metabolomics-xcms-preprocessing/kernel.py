"""Lightweight kernel helpers for metabolomics-xcms-preprocessing (synthetic XCMS peak table generator)."""
from __future__ import annotations
__all__ = ['simulate_peak_table', 'peak_table_summary']

def simulate_peak_table(*, ppm: float=15.0, peakwidth_min: float=5.0, peakwidth_max: float=30.0, n_samples: int=5, seed: int=42, input_path: str | None=None) -> pd.DataFrame:
    """Generate a seeded synthetic XCMS-style LC-MS peak table (rejects real mzML inputs)."""
    import numpy as np, pandas as pd
    if input_path is not None:
        raise ValueError('metabolomics-xcms-preprocessing is a synthetic demo generator; run XCMS/pyopenms externally for real mzML files.')
    if ppm <= 0 or peakwidth_min <= 0 or peakwidth_max <= peakwidth_min:
        raise ValueError('Invalid ppm or peakwidth bounds: require ppm > 0 and 0 < peakwidth_min < peakwidth_max.')
    rng = np.random.default_rng(seed)
    n_peaks = max(50, int(round(80.0 * ppm * ((peakwidth_max - peakwidth_min) / 25.0))))
    mz = np.sort(rng.uniform(70.0, 950.0, size=n_peaks))
    mz_half = mz * (ppm * 1e-06)
    rt = rng.uniform(30.0, 1200.0, size=n_peaks)
    pw_half = rng.uniform(peakwidth_min, peakwidth_max, size=n_peaks) * 0.5
    base_into = rng.lognormal(mean=11.0, sigma=1.2, size=n_peaks)
    df = pd.DataFrame({'feature_id': [f'FT{i + 1:04d}' for i in range(n_peaks)], 'mz': mz, 'mzmin': mz - mz_half, 'mzmax': mz + mz_half, 'rt': rt, 'rtmin': np.clip(rt - pw_half, 0.0, None), 'rtmax': rt + pw_half, 'into': base_into, 'maxo': base_into / np.maximum(pw_half, 1.0)})
    for s in range(1, n_samples + 1):
        df[f'sample_{s}'] = base_into * rng.lognormal(mean=0.0, sigma=0.25, size=n_peaks)
    df.attrs['run_info'] = {'synthetic_demo': True, 'n_samples': int(n_samples), 'n_peaks': int(n_peaks), 'mz_min': float(mz.min()), 'mz_max': float(mz.max()), 'rt_min': float(rt.min()), 'rt_max': float(rt.max())}
    return df

def peak_table_summary(table: pd.DataFrame) -> dict:
    """Return dictionary summary of synthetic XCMS peak table bounds and dimensions."""
    return table.attrs.get('run_info') or {'n_peaks': int(len(table))}
