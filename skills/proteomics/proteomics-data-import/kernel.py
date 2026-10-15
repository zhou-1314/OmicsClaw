"""Lightweight kernel helpers for proteomics-data-import (MaxQuant/FragPipe/DIA-NN standardization)."""
from __future__ import annotations
__all__ = ['read_table', 'standardize', 'import_summary']

def read_table(path: str | Path) -> pd.DataFrame:
    """Read CSV or TSV proteomics search-engine table by inspecting header delimiter."""
    import pandas as pd
    from pathlib import Path
    p = Path(path)
    with open(p, 'r', encoding='utf-8', errors='ignore') as fh:
        first_line = fh.readline()
    sep = '\t' if '\t' in first_line and p.suffix.lower() != '.csv' else ','
    return pd.read_csv(p, sep=sep, low_memory=False)

def standardize(data: pd.DataFrame, *, format: str='maxquant') -> pd.DataFrame:
    """Filter reverse/contaminant/site-only flags and standardize protein_id and intensity columns."""
    _FLAG_COLS = ('Reverse', 'Potential contaminant', 'Only identified by site', 'Contaminant')
    df = data.copy()
    n_in = len(df)
    for col in _FLAG_COLS:
        if col in df.columns:
            df = df[~df[col].astype(str).str.strip().isin(['+', 'True', 'true', '1'])]
    id_map = {'maxquant': 'Majority protein IDs', 'fragpipe': 'Protein ID', 'diann': 'Protein.Group'}
    src_id = id_map.get(format.lower(), 'protein_id')
    if src_id in df.columns and 'protein_id' not in df.columns:
        df['protein_id'] = df[src_id].astype(str).str.split(';').str[0]
    elif 'protein_id' not in df.columns:
        df['protein_id'] = df.iloc[:, 0].astype(str).str.split(';').str[0]
    mask_rev = df['protein_id'].astype(str).str.startswith(('REV__', 'rev_', 'CON__', 'contam_'))
    df = df[~mask_rev].reset_index(drop=True)
    df.attrs['run_info'] = {'format': format.lower(), 'n_input': int(n_in), 'n_retained': int(len(df)), 'n_filtered': int(n_in - len(df))}
    return df

def import_summary(table: pd.DataFrame) -> dict:
    """Return dictionary summary of imported and filtered protein groups."""
    return table.attrs.get('run_info') or {'n_retained': int(len(table))}
