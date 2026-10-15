"""Lightweight kernel helpers for metabolomics-peak-detection (RT-sorted prominence peak picking)."""
from __future__ import annotations
__all__ = ['detect_peaks', 'peak_summary']

def detect_peaks(data: pd.DataFrame, *, sample_cols: list[str] | None=None, prominence: float=10000.0, height: float | None=None, distance: int=5) -> pd.DataFrame:
    """Detect chromatographic peaks along RT-sorted mean sample intensity using scipy.signal.find_peaks."""
    import numpy as np
    from scipy import signal
    _META_NAMES = {'feature_id', 'id', 'name', 'mz', 'rt', 'retention_time'}
    if not {'mz', 'rt'}.issubset(data.columns):
        raise ValueError("Input DataFrame must contain 'mz' and 'rt' columns.")
    df = data.sort_values('rt').reset_index(drop=True)
    if sample_cols is None:
        num_cols = [c for c in df.select_dtypes(include=[np.number]).columns if c.lower() not in _META_NAMES]
        pref = [c for c in num_cols if c.lower().startswith(('sample', 'intensity', 'ctrl', 'treat'))]
        cols = pref or num_cols
    else:
        cols = list(sample_cols)
    if not cols:
        raise ValueError('No numeric sample/intensity columns found.')
    mean_sig = df[cols].astype(float).mean(axis=1).to_numpy()
    idx, props = signal.find_peaks(mean_sig, prominence=prominence, height=height, distance=max(int(distance), 1))
    out = df.iloc[idx].copy().reset_index(drop=True)
    out['mean_intensity'] = mean_sig[idx]
    out['prominence'] = props.get('prominences', np.zeros(len(idx), dtype=float))
    out.attrs['run_info'] = {'n_input_rows': int(len(df)), 'n_peaks': int(len(out)), 'sample_columns': cols, 'prominence': float(prominence), 'distance': int(distance)}
    return out

def peak_summary(peaks: pd.DataFrame) -> dict:
    """Return dictionary summary of detected chromatographic peaks."""
    return peaks.attrs.get('run_info') or {'n_peaks': int(len(peaks))}
