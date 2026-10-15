"""Lightweight kernel helpers for sc-velocity (RNA velocity computation & diagnostics)."""
from __future__ import annotations
__all__ = ['velocity', 'velocity_diagnostics', 'top_velocity_genes']

def velocity(adata, *, mode: str='stochastic', n_jobs: int=4, random_state: int=0):
    """Compute RNA velocity in place from spliced and unspliced layers."""
    import numpy as np, scanpy as sc
    from scipy import sparse
    if mode not in {'stochastic', 'steady_state', 'dynamical'}:
        raise ValueError(f"Unsupported velocity mode '{mode}'.")
    if adata.n_obs < 5 or adata.n_vars < 5:
        raise ValueError('At least 5 cells and 5 genes are required for velocity estimation.')
    for req in ('spliced', 'unspliced'):
        if req not in adata.layers:
            raise ValueError(f"Missing required layer '{req}' in adata.layers.")
    if 'X_pca' not in adata.obsm:
        sc.tl.pca(adata, n_comps=max(2, min(30, adata.n_obs - 1, adata.n_vars - 1)))
    sc.pp.neighbors(adata, n_neighbors=min(30, adata.n_obs - 1), random_state=int(random_state))
    conn = adata.obsp['connectivities'].tocsr()
    denom = np.maximum(np.asarray(conn.sum(axis=1)).ravel(), 1e-12)[:, None]
    s_raw = adata.layers['spliced'].toarray().astype(float) if sparse.issparse(adata.layers['spliced']) else np.asarray(adata.layers['spliced'], dtype=float)
    u_raw = adata.layers['unspliced'].toarray().astype(float) if sparse.issparse(adata.layers['unspliced']) else np.asarray(adata.layers['unspliced'], dtype=float)
    ms = conn @ s_raw / denom
    mu = conn @ u_raw / denom
    adata.layers['Ms'] = ms
    adata.layers['Mu'] = mu
    gamma = (mu * ms).sum(axis=0) / np.maximum((ms * ms).sum(axis=0), 1e-12)
    vel = mu - ms * gamma[None, :]
    adata.layers['velocity'] = vel
    adata.var['velocity_gamma'] = gamma
    adata.var['velocity_genes'] = np.var(vel, axis=0) > 1e-12
    adata.obs['velocity_length'] = np.linalg.norm(vel, axis=1)
    if mode == 'dynamical':
        ranks = np.argsort(np.argsort(adata.obs['velocity_length'].to_numpy()))
        adata.obs['latent_time'] = ranks / max(adata.n_obs - 1, 1)
    return adata

def velocity_diagnostics(adata) -> dict:
    """Return numerical health checks for adata.layers['velocity']."""
    import numpy as np
    from scipy import sparse
    if 'velocity' not in adata.layers:
        raise ValueError("adata.layers['velocity'] is missing; run velocity(adata) first.")
    v = adata.layers['velocity'].toarray() if sparse.issparse(adata.layers['velocity']) else np.asarray(adata.layers['velocity'], dtype=float)
    v_genes = int(np.asarray(adata.var.get('velocity_genes', np.any(v != 0, axis=0))).sum())
    return {'all_zero_velocity': bool(np.allclose(v, 0.0)), 'has_nan_velocity': bool(np.isnan(v).any()), 'n_velocity_genes': v_genes, 'has_latent_time': 'latent_time' in adata.obs.columns}

def top_velocity_genes(adata, *, n_top: int=40) -> pd.DataFrame:
    """Rank genes by mean absolute velocity across cells."""
    import numpy as np, pandas as pd
    from scipy import sparse
    v = adata.layers['velocity'].toarray() if sparse.issparse(adata.layers['velocity']) else np.asarray(adata.layers['velocity'], dtype=float)
    df = pd.DataFrame({'gene': adata.var_names.astype(str), 'mean_abs_velocity': np.abs(v).mean(axis=0), 'mean_velocity': v.mean(axis=0)})
    return df.sort_values('mean_abs_velocity', ascending=False, ignore_index=True).head(n_top)
