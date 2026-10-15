"""Lightweight kernel helpers for genomics-epigenomics (BED/narrowPeak interval analysis)."""
from __future__ import annotations
__all__ = ['read_records', 'analyze', 'peak_summary']

def read_records(path: str | Path) -> pd.DataFrame:
    """Read CSV or BED/narrowPeak interval files into a standardized DataFrame."""
    import pandas as pd
    from pathlib import Path
    _NARROWPEAK_COLS = ['chrom', 'start', 'end', 'name', 'score', 'strand', 'signal_value', 'p_value', 'q_value', 'peak']
    p = Path(path)
    if p.suffix.lower() == '.csv':
        return pd.read_csv(p)
    df = pd.read_csv(p, sep='\t', comment='#', header=None)
    df.columns = _NARROWPEAK_COLS[:len(df.columns)]
    return df

def analyze(data: pd.DataFrame, *, assay: str='chip-seq') -> pd.DataFrame:
    """Compute BED 0-based peak widths (end - start) and assay-level summary metrics."""
    import pandas as pd
    df = data.copy()
    df['start'] = df['start'].astype(int)
    df['end'] = df['end'].astype(int)
    df['width'] = df['end'] - df['start']
    if (df['width'] <= 0).any():
        raise ValueError('Invalid BED interval: end must be strictly greater than start.')
    info = {'assay': assay, 'n_peaks': int(len(df)), 'total_bp': int(df['width'].sum()), 'mean_width': float(df['width'].mean()) if len(df) else 0.0, 'median_width': float(df['width'].median()) if len(df) else 0.0, 'n_chromosomes': int(df['chrom'].nunique()) if len(df) else 0}
    if 'signal_value' in df.columns and len(df):
        info['mean_signal'] = float(pd.to_numeric(df['signal_value'], errors='coerce').mean())
    df.attrs['run_info'] = info
    return df

def peak_summary(data: pd.DataFrame, *, assay: str='chip-seq') -> dict:
    """Return dictionary of peak count, width distribution, and total genomic footprint."""
    res = analyze(data, assay=assay)
    return res.attrs['run_info']
