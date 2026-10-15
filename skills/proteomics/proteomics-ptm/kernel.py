"""Lightweight kernel helpers for proteomics-ptm (Olsen Class I/II/III PTM site localization)."""
from __future__ import annotations
__all__ = ['classify_sites', 'class_i_sites', 'ptm_summary']

def classify_sites(data: pd.DataFrame, *, loc_threshold: float=0.75) -> pd.DataFrame:
    """Assign Olsen et al. Class I / II / III localization confidence tiers to PTM sites."""
    import numpy as np, pandas as pd
    if not 0.5 <= loc_threshold <= 1.0:
        raise ValueError('loc_threshold must lie in [0.5, 1.0].')
    req = {'protein', 'ptm_type'}
    if not req.issubset(data.columns):
        raise ValueError(f'Missing required columns: {req - set(data.columns)}')
    df = data.copy()
    if 'localization_probability' in df.columns:
        prob = pd.to_numeric(df['localization_probability'], errors='coerce')
        df['site_class'] = np.where(prob.isna(), 'Unknown', np.where(prob >= loc_threshold, 'Class I', np.where(prob >= 0.5, 'Class II', 'Class III')))
    else:
        df['site_class'] = 'Unknown'
    vc = df['site_class'].value_counts()
    df.attrs['run_info'] = {'loc_threshold': float(loc_threshold), 'n_sites': int(len(df)), 'n_class_i': int(vc.get('Class I', 0)), 'n_class_ii': int(vc.get('Class II', 0)), 'n_class_iii': int(vc.get('Class III', 0)), 'n_proteins': int(df['protein'].nunique()), 'ptm_types': {str(k): int(v) for k, v in df['ptm_type'].value_counts().items()}}
    return df

def class_i_sites(table: pd.DataFrame) -> pd.DataFrame:
    """Return subset of high-confidence Class I PTM sites."""
    return table[table['site_class'] == 'Class I'].reset_index(drop=True)

def ptm_summary(table: pd.DataFrame) -> dict:
    """Return dictionary summary of PTM site classes and PTM type counts."""
    return table.attrs.get('run_info') or {'n_sites': int(len(table))}
