"""Lightweight kernel helpers for proteomics-quantification (LFQ, iBAQ, and spectral counting)."""
from __future__ import annotations
__all__ = ['quantify', 'count_tryptic_peptides', 'quantification_summary']

def count_tryptic_peptides(sequence: str, *, min_len: int=6, max_len: int=30) -> int:
    """Count theoretical observable tryptic peptides (cleave after K/R not before P, 6..30 aa)."""
    import re
    parts = re.split('(?<=[KR])(?!P)', str(sequence).strip().upper())
    return sum((1 for p in parts if min_len <= len(p) <= max_len))

def quantify(peptides: pd.DataFrame, *, method: str='lfq') -> pd.DataFrame:
    """Aggregate PSM/peptide rows into protein-level abundance via lfq, ibaq, or spectral_count."""
    import numpy as np
    if 'protein' not in peptides.columns:
        raise ValueError("Input table must include a 'protein' column.")
    m = method.lower()
    if m == 'spectral_count':
        out = peptides.groupby('protein', as_index=False).size().rename(columns={'size': 'abundance'})
        out['n_psms'] = out['abundance']
    elif m in {'lfq', 'ibaq'}:
        if 'intensity' not in peptides.columns:
            raise ValueError(f"Method '{m}' requires an 'intensity' column.")
        grp = peptides.groupby('protein', as_index=False).agg(abundance=('intensity', 'sum'), n_psms=('intensity', 'count'))
        if m == 'ibaq':
            if 'theoretical_peptides' in peptides.columns:
                theo = peptides.groupby('protein')['theoretical_peptides'].first().reindex(grp['protein']).to_numpy(dtype=float)
            elif 'protein_sequence' in peptides.columns:
                seqs = peptides.groupby('protein')['protein_sequence'].first().reindex(grp['protein'])
                theo = np.array([count_tryptic_peptides(s) for s in seqs], dtype=float)
            else:
                raise ValueError("iBAQ requires 'theoretical_peptides' or 'protein_sequence'.")
            if (theo <= 0).any() or np.isnan(theo).any():
                raise ValueError('Theoretical tryptic peptide counts must be strictly positive.')
            grp['abundance'] = grp['abundance'] / theo
        out = grp
    else:
        raise ValueError(f'Unknown quantification method: {method}')
    out['log10_abundance'] = np.log10(np.clip(out['abundance'].to_numpy(dtype=float), 1e-12, None))
    out = out.sort_values('abundance', ascending=False).reset_index(drop=True)
    out.attrs['run_info'] = {'method': m, 'n_proteins': int(len(out)), 'n_input_psms': int(len(peptides))}
    return out

def quantification_summary(table: pd.DataFrame) -> dict:
    """Return dictionary summary of protein quantification results."""
    return table.attrs.get('run_info') or {'n_proteins': int(len(table))}
