"""Lightweight kernel helpers for sc-in-silico-perturbation (virtual knockout screening)."""
from __future__ import annotations
__all__ = ['knockout_correlation', 'top_perturbed_genes']

def knockout_correlation(adata, *, ko_gene: str, n_top_genes: int=2000) -> pd.DataFrame:
    """Compute descriptive correlation edge-removal scores (dr_score, wt_ko_corr) for ko_gene."""
    import numpy as np, pandas as pd
    from scipy import sparse
    if ko_gene not in adata.var_names:
        raise ValueError(f"Target ko_gene '{ko_gene}' not found in adata.var_names.")
    mat = adata.layers['counts'] if 'counts' in adata.layers else adata.X
    dense = mat.toarray().astype(float) if sparse.issparse(mat) else np.asarray(mat, dtype=float)
    variances = np.var(dense, axis=0)
    k = min(max(2, int(n_top_genes)), adata.n_vars)
    top_idx = list(np.argsort(variances)[::-1][:k])
    ko_idx = int(adata.var_names.get_loc(ko_gene))
    if ko_idx not in top_idx:
        top_idx[-1] = ko_idx
    sub_genes = np.asarray(adata.var_names[top_idx], dtype=str)
    sub_mat = dense[:, top_idx]
    with np.errstate(invalid='ignore', divide='ignore'):
        wt_net = np.nan_to_num(np.corrcoef(sub_mat, rowvar=False), nan=0.0)
    np.fill_diagonal(wt_net, 0.0)
    ko_net = wt_net.copy()
    ko_pos = int(np.where(sub_genes == ko_gene)[0][0])
    wt_ko_corr = wt_net[:, ko_pos].copy()
    ko_net[ko_pos, :] = 0.0
    ko_net[:, ko_pos] = 0.0
    dr_score = np.abs(wt_net - ko_net).mean(axis=1)
    df = pd.DataFrame({'gene': sub_genes, 'dr_score': dr_score, 'wt_ko_corr': wt_ko_corr})
    return df.sort_values('dr_score', ascending=False, ignore_index=True)

def top_perturbed_genes(table: pd.DataFrame, *, n_top: int=15) -> pd.DataFrame:
    """Return top-N perturbed genes ranked by p.adj (scTenifoldKnk) or dr_score (grn_ko)."""
    if 'p.adj' in table.columns:
        return table.sort_values(['p.adj', 'FC'], ascending=[True, False], ignore_index=True).head(n_top)
    return table.sort_values('dr_score', ascending=False, ignore_index=True).head(n_top)
