"""Lightweight kernel helpers for proteomics-structural (XL-MS intra/inter split & distance validation)."""
from __future__ import annotations
__all__ = ['analyse_crosslinks', 'inter_protein_links', 'crosslink_summary']

def analyse_crosslinks(data: pd.DataFrame, *, fdr_threshold: float=0.05, crosslinker: str='DSS') -> pd.DataFrame:
    """Filter XL-MS crosslinks by FDR, classify intra/inter-protein links, and validate distance limits."""
    import numpy as np, pandas as pd
    _MAX_DIST = {'DSS': 30.0, 'BS3': 30.0, 'DSSO': 30.0, 'DSBU': 30.0, 'EDC': 20.0}
    req = {'protein_a', 'protein_b'}
    if not req.issubset(data.columns):
        raise ValueError(f'Missing required columns: {req - set(data.columns)}')
    xl_key = crosslinker.upper()
    if xl_key not in _MAX_DIST:
        raise ValueError(f"Unsupported crosslinker '{crosslinker}'. Expected one of {sorted(_MAX_DIST)}.")
    max_d = _MAX_DIST[xl_key]
    df = data.copy()
    if 'fdr' in df.columns:
        df = df[pd.to_numeric(df['fdr'], errors='coerce') <= fdr_threshold].reset_index(drop=True)
    df['link_type'] = np.where(df['protein_a'].astype(str) == df['protein_b'].astype(str), 'intra-protein', 'inter-protein')
    if 'distance_angstrom' in df.columns:
        dist = pd.to_numeric(df['distance_angstrom'], errors='coerce')
        df['within_constraint'] = dist <= max_d
        df['constraint_status'] = np.where(dist.isna(), 'unchecked', np.where(dist <= max_d, 'satisfied', 'violated'))
    else:
        df['constraint_status'] = 'unchecked'
    df.attrs['run_info'] = {'crosslinker': xl_key, 'max_distance_angstrom': max_d, 'fdr_threshold': float(fdr_threshold), 'n_crosslinks': int(len(df)), 'n_intra': int((df['link_type'] == 'intra-protein').sum()), 'n_inter': int((df['link_type'] == 'inter-protein').sum()), 'n_satisfied': int((df['constraint_status'] == 'satisfied').sum()), 'n_violated': int((df['constraint_status'] == 'violated').sum())}
    return df

def inter_protein_links(table: pd.DataFrame) -> pd.DataFrame:
    """Return subset of inter-protein crosslinks."""
    return table[table['link_type'] == 'inter-protein'].reset_index(drop=True)

def crosslink_summary(table: pd.DataFrame) -> dict:
    """Return dictionary summary of XL-MS intra/inter counts and distance constraint satisfaction."""
    return table.attrs.get('run_info') or {'n_crosslinks': int(len(table))}
