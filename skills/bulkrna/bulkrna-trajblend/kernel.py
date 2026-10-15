"""Lightweight kernel helpers for bulkrna-trajblend (joint PCA/kNN pseudotime + NNLS fractions)."""
from __future__ import annotations
__all__ = ['project_bulk_pseudotime', 'deconvolve_reference_fractions']

def deconvolve_reference_fractions(bulk: pd.DataFrame, *, reference: pd.DataFrame, cell_types: pd.Series) -> pd.DataFrame:
    """Estimate sample-by-cell-type proportions from a cell-by-gene reference via NNLS."""
    import numpy as np, pandas as pd
    from scipy.optimize import nnls
    shared = bulk.columns.intersection(reference.columns)
    if len(shared) < 10:
        raise ValueError(f'Insufficient shared genes ({len(shared)}) between bulk and reference.')
    sig_df = reference[shared].groupby(cell_types.loc[reference.index].astype(str)).mean().T
    S, B = (sig_df.to_numpy(dtype=float), bulk[shared].to_numpy(dtype=float))
    props = np.zeros((B.shape[0], S.shape[1]), dtype=float)
    for i in range(B.shape[0]):
        w, _ = nnls(S, B[i])
        props[i] = w / float(w.sum()) if w.sum() > 0 else np.full(S.shape[1], 1.0 / S.shape[1])
    return pd.DataFrame(props, index=bulk.index, columns=sig_df.columns)

def project_bulk_pseudotime(bulk: pd.DataFrame, *, reference: pd.DataFrame, cell_types: pd.Series, pseudotime: pd.Series, k: int=15) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Project sample-by-gene bulk profiles onto a cell-by-gene reference pseudotime axis."""
    import numpy as np, pandas as pd
    shared = bulk.columns.intersection(reference.columns)
    if len(shared) < 10:
        raise ValueError(f'Insufficient shared genes ({len(shared)}) for trajectory projection.')
    fractions = deconvolve_reference_fractions(bulk, reference=reference, cell_types=cell_types)
    R, B = (np.log1p(reference[shared].to_numpy(dtype=float)), np.log1p(bulk[shared].to_numpy(dtype=float)))
    R_z = (R - R.mean(axis=0)) / np.maximum(R.std(axis=0), 1e-08)
    B_z = (B - B.mean(axis=0)) / np.maximum(B.std(axis=0), 1e-08)
    _, _, vt = np.linalg.svd(R_z, full_matrices=False)
    basis = vt[:min(10, R_z.shape[0] - 1, R_z.shape[1])].T
    R_pcs, B_pcs = (R_z @ basis, B_z @ basis)
    pt_vec, k_eff = (pseudotime.loc[reference.index].to_numpy(dtype=float), min(k, len(reference)))
    rows = []
    for i, sample in enumerate(bulk.index):
        dists = np.linalg.norm(R_pcs - B_pcs[i], axis=1)
        nn = np.argsort(dists)[:k_eff]
        w = 1.0 / np.maximum(dists[nn], 1e-06)
        w /= w.sum()
        pt_mean = float(np.sum(w * pt_vec[nn]))
        rows.append({'sample': sample, 'pseudotime_mean': pt_mean, 'pseudotime_std': float(np.sqrt(np.sum(w * (pt_vec[nn] - pt_mean) ** 2))), 'dominant_cell_type': str(fractions.loc[sample].idxmax())})
    return (pd.DataFrame(rows).set_index('sample'), fractions)
