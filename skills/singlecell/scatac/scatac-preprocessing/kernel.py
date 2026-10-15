"""Lightweight kernel helpers for scatac-preprocessing (Signac-style TF-IDF + LSI + Leiden)."""
from __future__ import annotations
__all__ = ['preprocess_atac', 'tfidf_normalize', 'atac_summary']

def tfidf_normalize(X, *, scale_factor: float=10000.0):
    """Compute Signac-style log(1 + TF * IDF * scale_factor) on a cell x peak matrix."""
    import numpy as np, scipy.sparse as sp
    mat = X.tocsr().astype(np.float32) if sp.issparse(X) else sp.csr_matrix(X, dtype=np.float32)
    cell_sums = np.asarray(mat.sum(axis=1)).ravel()
    cell_sums[cell_sums == 0] = 1.0
    tf = sp.diags(1.0 / cell_sums) @ mat
    peak_counts = np.asarray((mat > 0).sum(axis=0)).ravel()
    idf = np.log1p(mat.shape[0] / np.maximum(peak_counts, 1.0))
    tfidf = tf @ sp.diags(idf) * scale_factor
    tfidf.data = np.log1p(tfidf.data)
    return tfidf

def preprocess_atac(adata, *, min_peaks: int=200, min_cells: int=5, n_top_peaks: int=10000, n_lsi: int=30, n_neighbors: int=15, resolution: float=0.8, random_state: int=0):
    """Filter cell x peak matrix, run TF-IDF + LSI (dropping component 1), UMAP, and Leiden."""
    import numpy as np, scanpy as sc
    from sklearn.decomposition import TruncatedSVD
    ad = adata.copy()
    sc.pp.filter_cells(ad, min_genes=min_peaks)
    sc.pp.filter_genes(ad, min_cells=min_cells)
    peak_totals = np.asarray(ad.X.sum(axis=0)).ravel()
    if ad.n_vars > n_top_peaks:
        top_idx = np.argsort(peak_totals)[::-1][:n_top_peaks]
        ad = ad[:, np.sort(top_idx)].copy()
    ad.layers['counts'] = ad.X.copy()
    ad.X = tfidf_normalize(ad.X)
    k = min(n_lsi, ad.n_obs - 1, ad.n_vars - 1)
    svd = TruncatedSVD(n_components=k, random_state=random_state)
    lsi = svd.fit_transform(ad.X)
    ad.obsm['X_lsi'] = lsi
    ad.uns['lsi_variance_ratio'] = svd.explained_variance_ratio_
    ad.obsm['X_lsi_retained'] = lsi[:, 1:] if k > 1 else lsi
    sc.pp.neighbors(ad, use_rep='X_lsi_retained', n_neighbors=n_neighbors, random_state=random_state)
    sc.tl.umap(ad, random_state=random_state)
    sc.tl.leiden(ad, resolution=resolution, key_added='leiden', flavor='igraph', n_iterations=2, random_state=random_state)
    return ad

def atac_summary(adata, *, cluster_key: str='leiden') -> dict:
    """Summarize retained cells, peaks, and cluster counts."""
    clusters = int(adata.obs[cluster_key].nunique()) if cluster_key in adata.obs else 0
    return {'n_cells': int(adata.n_obs), 'n_peaks': int(adata.n_vars), 'n_clusters': clusters}
