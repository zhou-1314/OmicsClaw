"""Lightweight kernel helpers for sc-perturb (Perturb-seq Mixscape classification)."""
from __future__ import annotations
__all__ = ['mixscape', 'class_counts', 'global_class_counts']

def mixscape(adata, *, pert_key: str='perturbation', control: str='NT', split_by: str | None=None, logfc_threshold: float=0.25, pval_cutoff: float=0.05, perturbation_type: str='KO'):
    """Return an AnnData copy with mixscape_class, mixscape_class_global, and posterior probabilities."""
    import numpy as np, pandas as pd
    from scipy import sparse
    from scipy.stats import mannwhitneyu
    if pert_key not in adata.obs.columns:
        raise ValueError(f"pert_key '{pert_key}' not found in adata.obs.")
    if split_by is not None and split_by not in adata.obs.columns:
        raise ValueError(f"split_by '{split_by}' not found in adata.obs.")
    labels = adata.obs[pert_key].astype(str)
    if control not in set(labels):
        raise ValueError(f"Control label '{control}' not found in adata.obs['{pert_key}'].")
    ad = adata.copy()
    mat = ad.X.toarray().astype(float) if sparse.issparse(ad.X) else np.asarray(ad.X, dtype=float)
    ctrl_mask = (labels == control).to_numpy()
    ctrl_mean = mat[ctrl_mask].mean(axis=0, keepdims=True)
    ad.layers['X_pert'] = mat - ctrl_mean
    cls, gcls, probs = ([], [], np.zeros(ad.n_obs, dtype=float))
    for pert, idx in ad.obs.groupby(labels, observed=True).groups.items():
        pos = ad.obs.index.get_indexer(idx)
        if pert == control:
            for p in pos:
                cls.append((p, control))
                gcls.append((p, control))
            continue
        diff = np.abs(mat[pos].mean(axis=0) - ctrl_mean.ravel())
        top_g = int(np.argmax(diff))
        _, pval = mannwhitneyu(mat[pos, top_g], mat[ctrl_mask, top_g], alternative='two-sided')
        if diff[top_g] < logfc_threshold or pval > pval_cutoff:
            for p in pos:
                cls.append((p, f'{pert} NP'))
                gcls.append((p, 'NP'))
        else:
            dev = np.linalg.norm(ad.layers['X_pert'][pos], axis=1)
            ctrl_dev = float(np.median(np.linalg.norm(ad.layers['X_pert'][ctrl_mask], axis=1)))
            p_ko = 1.0 / (1.0 + np.exp(-(dev - ctrl_dev)))
            probs[pos] = p_ko
            for p, pr in zip(pos, p_ko):
                is_resp = pr >= 0.5
                cls.append((p, f'{pert} {perturbation_type}' if is_resp else f'{pert} NP'))
                gcls.append((p, perturbation_type if is_resp else 'NP'))
    ad.obs['mixscape_class'] = pd.Series(dict(cls)).reindex(range(ad.n_obs)).to_numpy()
    ad.obs['mixscape_class_global'] = pd.Series(dict(gcls)).reindex(range(ad.n_obs)).to_numpy()
    ad.obs[f'mixscape_class_p_{perturbation_type.lower()}'] = probs
    return ad

def class_counts(adata) -> pd.DataFrame:
    """Return cell counts per target-specific mixscape_class."""
    return adata.obs['mixscape_class'].value_counts().rename_axis('class').reset_index(name='n_cells')

def global_class_counts(adata) -> pd.DataFrame:
    """Return cell counts per mixscape_class_global (e.g. KO/OE, NP, NT)."""
    return adata.obs['mixscape_class_global'].value_counts().rename_axis('global_class').reset_index(name='n_cells')
