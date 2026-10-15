"""Lightweight kernel helpers for sc-cytotrace (complexity-based potency scoring)."""
from __future__ import annotations
__all__ = ['cytotrace', 'potency_table', 'potency_composition']

def cytotrace(adata, *, n_neighbors: int=30, layer: str | None=None):
    """Annotate CytoTRACE-simple potency score, gene count, and potency category in place."""
    import numpy as np, pandas as pd, scanpy as sc
    from scipy import sparse
    from scipy.stats import rankdata
    _BINS = ['Differentiated', 'Unipotent', 'Oligopotent', 'Multipotent', 'Pluripotent', 'Totipotent']
    if adata.n_obs == 0 or adata.n_vars == 0 or n_neighbors < 1:
        raise ValueError('Invalid AnnData dimensions or n_neighbors < 1.')
    if layer is not None and layer not in adata.layers:
        raise ValueError(f"Layer '{layer}' not found in adata.layers.")
    mat = adata.layers[layer] if layer is not None else adata.X
    gene_counts = np.asarray((mat > 0).sum(axis=1)).ravel().astype(float) if sparse.issparse(mat) else np.asarray((mat > 0).sum(axis=1), dtype=float).ravel()
    if 'connectivities' not in adata.obsp:
        if 'X_pca' not in adata.obsm:
            sc.tl.pca(adata, n_comps=max(1, min(30, adata.n_obs - 1, adata.n_vars - 1)))
        sc.pp.neighbors(adata, n_neighbors=min(n_neighbors, max(1, adata.n_obs - 1)))
    ranks = (rankdata(gene_counts, method='average') - 1.0) / max(adata.n_obs - 1, 1)
    conn = adata.obsp['connectivities'].tocsr()
    denom = np.maximum(np.asarray(conn.sum(axis=1)).ravel(), 1e-12)
    smoothed = np.asarray(conn.dot(ranks)).ravel() / denom + 0.5 * ranks
    scores = (rankdata(smoothed, method='average') - 1.0) / max(adata.n_obs - 1, 1)
    adata.obs['cytotrace_gene_count'] = gene_counts.astype(int)
    adata.obs['cytotrace_score'] = scores
    adata.obs['cytotrace_potency'] = pd.Categorical(pd.cut(scores, bins=np.linspace(0.0, 1.0, 7), labels=_BINS, include_lowest=True), categories=_BINS, ordered=True)
    return adata

def potency_table(adata) -> pd.DataFrame:
    """Return per-cell cytotrace_score, cytotrace_potency, and cytotrace_gene_count."""
    cols = ['cytotrace_score', 'cytotrace_potency', 'cytotrace_gene_count']
    return adata.obs[cols].copy()

def potency_composition(adata) -> pd.DataFrame:
    """Return cell counts and fractions across the six relative potency bins."""
    vc = adata.obs['cytotrace_potency'].value_counts(sort=False)
    df = vc.rename_axis('potency_bin').reset_index(name='n_cells')
    df['fraction'] = df['n_cells'] / max(len(adata), 1)
    return df
