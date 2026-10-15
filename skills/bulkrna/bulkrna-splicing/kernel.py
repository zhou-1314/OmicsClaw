"""Lightweight kernel helpers for bulkrna-splicing (rMATS / SUPPA2 delta-PSI filtering & summary)."""
from __future__ import annotations
__all__ = ['filter_splicing_events', 'summarize_event_types']

def filter_splicing_events(events: pd.DataFrame, *, dpsi_cutoff: float=0.1, padj_cutoff: float=0.05) -> pd.DataFrame:
    """Validate splicing event columns and filter significant differential splicing events."""
    import numpy as np
    _REQUIRED_COLS = {'gene', 'event_type', 'delta_psi', 'padj'}
    missing = _REQUIRED_COLS - set(events.columns)
    if missing:
        raise ValueError(f'Missing required splicing columns: {sorted(missing)}')
    df = events.dropna(subset=['delta_psi', 'padj']).copy()
    dpsi = df['delta_psi'].to_numpy(dtype=float)
    padj = df['padj'].to_numpy(dtype=float)
    if (np.abs(dpsi) > 1.0 + 1e-06).any() or ((padj < 0.0) | (padj > 1.0 + 1e-06)).any():
        raise ValueError('delta_psi must lie in [-1, 1] and padj in [0, 1].')
    mask = (np.abs(dpsi) > dpsi_cutoff) & (padj < padj_cutoff)
    return df.loc[mask].sort_values(['padj', 'delta_psi'], ascending=[True, False]).reset_index(drop=True)

def summarize_event_types(events: pd.DataFrame) -> pd.DataFrame:
    """Count total, inclusion (delta_psi > 0), and skipping (delta_psi < 0) events per event_type."""
    import numpy as np, pandas as pd
    if events.empty:
        return pd.DataFrame(columns=['event_type', 'n_events', 'n_inclusion', 'n_skipping', 'mean_abs_dpsi'])
    rows = []
    for ev_type, grp in events.groupby('event_type', sort=True):
        dpsi = grp['delta_psi'].to_numpy(dtype=float)
        rows.append({'event_type': str(ev_type), 'n_events': int(len(grp)), 'n_inclusion': int((dpsi > 0).sum()), 'n_skipping': int((dpsi < 0).sum()), 'mean_abs_dpsi': float(np.mean(np.abs(dpsi)))})
    return pd.DataFrame(rows).sort_values('n_events', ascending=False).reset_index(drop=True)
