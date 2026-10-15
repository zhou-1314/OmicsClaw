"""Lightweight kernel helpers for sc-gene-programs (NMF gene program discovery)."""
from __future__ import annotations
__all__ = ['find_programs', 'program_weights', 'top_program_genes']

def find_programs(adata, *, method: str='nmf', n_programs: int=6, n_iter: int=400, layer: str | None=None, top_genes: int=30, random_state: int=0):
    """Factorize non-negative expression into K gene programs stored in obsm['X_gene_programs']."""
    import numpy as np
    from scipy import sparse
    from sklearn.decomposition import NMF
    if n_programs < 1 or adata.n_obs < n_programs:
        raise ValueError('Invalid n_programs relative to adata.n_obs.')
    ad = adata.copy()
    mat = ad.layers[layer] if layer is not None else ad.X
    if sparse.issparse(mat):
        mat = mat.tocsr().astype(float)
        mat.data = np.maximum(mat.data, 0.0)
    else:
        mat = np.maximum(np.asarray(mat, dtype=float), 0.0)
    model = NMF(n_components=n_programs, init='nndsvda', max_iter=n_iter, random_state=random_state)
    usage = model.fit_transform(mat)
    prog_names = [f'Program_{i + 1}' for i in range(n_programs)]
    ad.obsm['X_gene_programs'] = usage
    ad.uns['gene_programs_names'] = prog_names
    ad.uns['gene_programs_weights'] = model.components_.copy()
    ad.uns['gene_programs_top_n'] = int(top_genes)
    return ad

def program_weights(adata) -> pd.DataFrame:
    """Return (n_programs, n_genes) DataFrame of NMF gene weights."""
    import pandas as pd
    if 'gene_programs_weights' not in adata.uns:
        raise ValueError('Run find_programs(adata) before calling program_weights.')
    return pd.DataFrame(adata.uns['gene_programs_weights'], index=list(adata.uns['gene_programs_names']), columns=adata.var_names)

def top_program_genes(adata, *, n: int | None=None) -> pd.DataFrame:
    """Return top-N highest-weight genes per program as (program, rank, gene, weight)."""
    import pandas as pd
    w_df = program_weights(adata)
    limit = n if n is not None else int(adata.uns.get('gene_programs_top_n', 30))
    rows = []
    for prog in w_df.index:
        top = w_df.loc[prog].nlargest(limit)
        for rank, (gene, weight) in enumerate(top.items(), 1):
            rows.append({'program': str(prog), 'rank': int(rank), 'gene': str(gene), 'weight': float(weight)})
    return pd.DataFrame(rows)
