"""Lightweight kernel helpers for sc-metacell (metacell partitioning and aggregation)."""
from __future__ import annotations
__all__ = ['metacells', 'aggregate_metacells', 'metacell_summary']

def aggregate_metacells(adata, labels: pd.Series | None=None, *, celltype_key: str='leiden'):
    """Aggregate cells by metacell label into a new mean-expression AnnData."""
    import anndata as ad, numpy as np, pandas as pd
    from scipy import sparse
    lab = pd.Series(labels, index=adata.obs_names) if labels is not None else adata.obs['metacell']
    if lab.isna().any() or len(lab) != adata.n_obs:
        raise ValueError('Metacell labels must cover every cell without NaNs.')
    mat = adata.layers['counts'] if 'counts' in adata.layers else adata.X
    label_col = celltype_key if celltype_key in adata.obs.columns else adata.obs.columns[0] if len(adata.obs.columns) else None
    rows, meta = ([], [])
    for mc, idx in adata.obs.groupby(lab.astype(str), observed=True).groups.items():
        pos = adata.obs.index.get_indexer(idx)
        sub = mat[pos]
        mean_vec = np.asarray(sub.mean(axis=0)).ravel().astype(float) if sparse.issparse(sub) else np.asarray(sub.mean(axis=0), dtype=float).ravel()
        dom = str(adata.obs.loc[idx, label_col].mode().iloc[0]) if label_col else 'unknown'
        rows.append(mean_vec)
        meta.append({'metacell': str(mc), 'n_cells': int(len(idx)), 'dominant_label': dom})
    obs_df = pd.DataFrame(meta).set_index('metacell')
    X_agg = np.vstack(rows)
    madata = ad.AnnData(X=X_agg.copy(), obs=obs_df, var=adata.var.copy())
    madata.layers['mean_expression'] = X_agg.copy()
    return madata

def metacells(adata, *, method: str='kmeans', use_rep: str='X_pca', n_metacells: int=30, random_state: int=0, celltype_key: str='leiden'):
    """Assign cells to metacells in adata.obs['metacell'] and return aggregated metacell AnnData."""
    import numpy as np, pandas as pd
    from sklearn.cluster import KMeans
    if use_rep not in adata.obsm:
        raise ValueError(f"Representation '{use_rep}' not found in adata.obsm.")
    if n_metacells < 2 or n_metacells >= adata.n_obs:
        raise ValueError('n_metacells must satisfy 2 <= n_metacells < adata.n_obs.')
    km = KMeans(n_clusters=int(n_metacells), random_state=int(random_state), n_init=10)
    preds = km.fit_predict(np.asarray(adata.obsm[use_rep], dtype=float))
    adata.obs['metacell'] = pd.Categorical([f'MC_{i}' for i in preds])
    return aggregate_metacells(adata, celltype_key=celltype_key)

def metacell_summary(madata) -> pd.DataFrame:
    """Return metacell summary DataFrame with n_cells and dominant_label."""
    return madata.obs[['n_cells', 'dominant_label']].copy().reset_index()
