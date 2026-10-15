"""Lightweight kernel helpers for proteomics-identification (PSM/peptide FDR filtering & summary)."""
from __future__ import annotations
__all__ = ['read_table', 'filter_identifications', 'identification_summary']

def read_table(path: str | Path) -> pd.DataFrame:
    """Read peptide/PSM CSV or TSV and normalize common MaxQuant/FragPipe column names."""
    import pandas as pd
    from pathlib import Path
    p = Path(path)
    sep = '\t' if p.suffix.lower() in {'.tsv', '.txt'} else ','
    df = pd.read_csv(p, sep=sep, low_memory=False)
    col_map = {'Sequence': 'sequence', 'Peptide': 'sequence', 'Stripped.Sequence': 'sequence', 'Proteins': 'protein', 'Protein': 'protein', 'Protein.Group': 'protein', 'Charge': 'charge', 'Score': 'score'}
    for src, dst in col_map.items():
        if src in df.columns and dst not in df.columns:
            df[dst] = df[src]
    return df

def filter_identifications(data: pd.DataFrame, *, fdr_threshold: float=0.01, n_spectra: int | None=None) -> pd.DataFrame:
    """Filter peptide/PSM table by the first available q-value/PEP/FDR column."""
    import pandas as pd
    _CONF_COLS = ('qvalue', 'q-value', 'q_value', 'Q.Value', 'PEP', 'pep', 'fdr')
    df = data.copy()
    conf_col = next((c for c in _CONF_COLS if c in df.columns), None)
    if conf_col is not None:
        vals = pd.to_numeric(df[conf_col], errors='coerce')
        out = df[vals <= fdr_threshold].reset_index(drop=True)
    else:
        out = df.reset_index(drop=True)
    n_pep = int(out['sequence'].nunique()) if 'sequence' in out.columns else int(len(out))
    n_prot = int(out['protein'].astype(str).str.split(';').str[0].nunique()) if 'protein' in out.columns else 0
    out.attrs['run_info'] = {'confidence_column': conf_col or 'none', 'fdr_threshold': float(fdr_threshold), 'n_input_psms': int(len(df)), 'n_retained_psms': int(len(out)), 'n_unique_peptides': n_pep, 'n_unique_proteins': n_prot, 'id_rate_pct': float(len(out) / max(n_spectra, 1) * 100.0) if n_spectra else 100.0}
    return out

def identification_summary(table: pd.DataFrame) -> dict:
    """Return dictionary summary of retained PSMs, unique peptides, and unique proteins."""
    return table.attrs.get('run_info') or {'n_retained_psms': int(len(table))}
