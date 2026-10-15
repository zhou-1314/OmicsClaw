"""Lightweight kernel helpers for sc-multi-count (merging multi-sample count matrices)."""
from __future__ import annotations
__all__ = ['merge_samples', 'per_sample_summary', 'barcode_metrics']

def merge_samples(adatas: list, *, sample_ids: list[str] | None=None, sample_key: str='sample_id', join: str='outer'):
    """Concatenate multiple single-sample count AnnData objects with sample-prefixed barcodes."""
    import anndata as ad
    if len(adatas) < 2:
        raise ValueError('merge_samples requires at least two AnnData objects.')
    labels = sample_ids or [f'sample_{i + 1}' for i in range(len(adatas))]
    prepared = []
    for obj, sid in zip(adatas, labels):
        copy = obj.copy()
        copy.var_names_make_unique()
        copy.obs[sample_key] = str(sid)
        copy.obs_names = [f'{sid}_{bc}' for bc in copy.obs_names]
        prepared.append(copy)
    merged = ad.concat(prepared, join=join, fill_value=0)
    merged.layers['counts'] = merged.X.copy()
    merged.raw = merged.copy()
    return merged

def per_sample_summary(adata, *, sample_key: str='sample_id') -> pd.DataFrame:
    """Summarize cell counts, median UMIs, and median detected genes per sample."""
    import numpy as np, pandas as pd
    mat = adata.layers['counts'] if 'counts' in adata.layers else adata.X
    df = pd.DataFrame({sample_key: adata.obs[sample_key].astype(str).values, 'total_counts': np.asarray(mat.sum(axis=1)).ravel(), 'n_genes': np.asarray((mat > 0).sum(axis=1)).ravel()})
    return df.groupby(sample_key, as_index=False).agg(n_cells=('total_counts', 'count'), median_counts=('total_counts', 'median'), median_genes=('n_genes', 'median'), total_umis=('total_counts', 'sum'))

def barcode_metrics(adata, *, sample_key: str='sample_id') -> pd.DataFrame:
    """Return per-barcode UMI counts and detected features sorted by total counts."""
    import numpy as np, pandas as pd
    mat = adata.layers['counts'] if 'counts' in adata.layers else adata.X
    out = pd.DataFrame({'barcode': adata.obs_names, sample_key: adata.obs[sample_key].astype(str).values if sample_key in adata.obs else 'sample_1', 'total_counts': np.asarray(mat.sum(axis=1)).ravel(), 'n_genes_by_counts': np.asarray((mat > 0).sum(axis=1)).ravel()})
    return out.sort_values('total_counts', ascending=False).reset_index(drop=True)
