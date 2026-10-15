"""Lightweight kernel helpers for metabolomics-annotation (ESI adduct exact-mass ppm matching)."""
from __future__ import annotations
__all__ = ['annotate', 'annotation_summary']

def annotate(data: pd.DataFrame, *, database: str='hmdb', ppm: float=10.0, adducts: list[str] | None=None, reference: pd.DataFrame | None=None) -> pd.DataFrame:
    """Match feature m/z values against a reference neutral_mass table across ESI adducts within ppm."""
    import numpy as np, pandas as pd
    _ADDUCTS = {'[M+H]+': 1.007276, '[M+Na]+': 22.989218, '[M+NH4]+': 18.033823, '[M+K]+': 38.963158, '[M-H]-': -1.007276, '[M+Cl]-': 34.969402, '[M+FA-H]-': 44.998201}
    if reference is None or reference.empty:
        raise ValueError('A non-empty reference DataFrame (name, neutral_mass, database_id, formula) is required.')
    if ppm < 0:
        raise ValueError('ppm tolerance must be non-negative.')
    req_ref = {'name', 'neutral_mass', 'database_id', 'formula'}
    if not req_ref.issubset(reference.columns) or 'mz' not in data.columns:
        raise ValueError("data requires 'mz' and reference requires name, neutral_mass, database_id, formula.")
    used_adducts = adducts or ['[M+H]+', '[M-H]-']
    ref_masses = reference['neutral_mass'].to_numpy(dtype=float)
    rows = []
    for idx, f_row in data.iterrows():
        fid = f_row.get('feature_id', idx)
        q_mz = float(f_row['mz'])
        hit_any = False
        for add in used_adducts:
            shift = _ADDUCTS[add]
            theo = ref_masses + shift
            errs = np.abs(q_mz - theo) / np.maximum(theo, 1e-09) * 1000000.0
            for r_idx in np.where(errs <= ppm)[0]:
                hit_any = True
                r = reference.iloc[int(r_idx)]
                err = float(errs[r_idx])
                rows.append({'feature_id': fid, 'query_mz': q_mz, 'matched_name': r['name'], 'database_id': r['database_id'], 'formula': r['formula'], 'adduct': add, 'theoretical_mz': float(theo[r_idx]), 'ppm_error': err, 'confidence': 'high' if err <= 3.0 else 'medium'})
        if not hit_any:
            rows.append({'feature_id': fid, 'query_mz': q_mz, 'matched_name': 'Unknown', 'database_id': '', 'formula': '', 'adduct': '', 'theoretical_mz': np.nan, 'ppm_error': np.nan, 'confidence': 'unmatched'})
    out = pd.DataFrame(rows)
    out.attrs['run_info'] = {'database': database, 'ppm': float(ppm), 'n_queries': int(len(data)), 'n_matched_rows': int((out['matched_name'] != 'Unknown').sum())}
    return out

def annotation_summary(table: pd.DataFrame) -> dict:
    """Return dictionary summary of annotated vs. unmatched metabolomics features."""
    return table.attrs.get('run_info') or {'n_rows': int(len(table))}
