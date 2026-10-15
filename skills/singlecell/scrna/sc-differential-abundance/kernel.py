"""Lightweight kernel helpers for sc-differential-abundance (compositional & neighborhood DA)."""
from __future__ import annotations
__all__ = ['composition', 'condition_proportions', 'test_abundance']

def composition(adata, *, sample_key: str='sample', celltype_key: str='cell_type', condition_key: str='condition') -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (counts_df, proportions_df) of shape (n_samples, n_cell_types)."""
    import numpy as np, pandas as pd

    def _validate_sample_condition(adata, sample_key: str, condition_key: str) -> pd.Series:
        for col in (sample_key, condition_key):
            if col not in adata.obs.columns:
                raise ValueError(f"Missing required obs column '{col}'.")
        n_cond = adata.obs.groupby(sample_key, observed=True)[condition_key].nunique()
        if (n_cond > 1).any():
            raise ValueError('Each sample must belong to exactly one condition.')
        return adata.obs.groupby(sample_key, observed=True)[condition_key].first()
    _validate_sample_condition(adata, sample_key, condition_key)
    counts = pd.crosstab(adata.obs[sample_key], adata.obs[celltype_key])
    proportions = counts.div(counts.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
    return (counts, proportions)

def condition_proportions(adata, *, sample_key: str='sample', celltype_key: str='cell_type', condition_key: str='condition') -> pd.DataFrame:
    """Return mean cell-type proportions per condition, weighting samples equally."""
    import pandas as pd

    def _validate_sample_condition(adata, sample_key: str, condition_key: str) -> pd.Series:
        for col in (sample_key, condition_key):
            if col not in adata.obs.columns:
                raise ValueError(f"Missing required obs column '{col}'.")
        n_cond = adata.obs.groupby(sample_key, observed=True)[condition_key].nunique()
        if (n_cond > 1).any():
            raise ValueError('Each sample must belong to exactly one condition.')
        return adata.obs.groupby(sample_key, observed=True)[condition_key].first()
    sample_cond = _validate_sample_condition(adata, sample_key, condition_key)
    _, props = composition(adata, sample_key=sample_key, celltype_key=celltype_key, condition_key=condition_key)
    return props.groupby(sample_cond.loc[props.index], observed=True).mean()

def test_abundance(adata, *, method: str='simple', sample_key: str='sample', condition_key: str='condition', celltype_key: str='cell_type', fdr: float=0.05) -> pd.DataFrame:
    """Test per-cell-type proportion differences across samples via two-sided Mann-Whitney U + BH."""
    import numpy as np, pandas as pd
    from scipy.stats import mannwhitneyu
    from statsmodels.stats.multitest import multipletests

    def _validate_sample_condition(adata, sample_key: str, condition_key: str) -> pd.Series:
        for col in (sample_key, condition_key):
            if col not in adata.obs.columns:
                raise ValueError(f"Missing required obs column '{col}'.")
        n_cond = adata.obs.groupby(sample_key, observed=True)[condition_key].nunique()
        if (n_cond > 1).any():
            raise ValueError('Each sample must belong to exactly one condition.')
        return adata.obs.groupby(sample_key, observed=True)[condition_key].first()
    sample_cond = _validate_sample_condition(adata, sample_key, condition_key)
    _, props = composition(adata, sample_key=sample_key, celltype_key=celltype_key, condition_key=condition_key)
    conds = sorted(sample_cond.unique(), key=str)
    if len(conds) < 2:
        raise ValueError('At least two conditions are required for differential abundance testing.')
    mask_a = (sample_cond.loc[props.index] == conds[0]).to_numpy()
    mask_b = (sample_cond.loc[props.index] == conds[1]).to_numpy()
    rows = []
    for ct in props.columns:
        va, vb = (props.loc[mask_a, ct].to_numpy(), props.loc[mask_b, ct].to_numpy())
        _, pval = mannwhitneyu(vb, va, alternative='two-sided') if len(va) > 0 and len(vb) > 0 else (np.nan, 1.0)
        l2fc = float(np.log2((vb.mean() + 1e-06) / (va.mean() + 1e-06)))
        rows.append({'cell_type': ct, 'mean_prop_a': float(va.mean()), 'mean_prop_b': float(vb.mean()), 'log2fc_group_b_over_a': l2fc, 'pval': float(pval)})
    df = pd.DataFrame(rows)
    df['padj'] = multipletests(df['pval'], method='fdr_bh')[1] if len(df) else []
    df['significant'] = df['padj'] < fdr
    return df
