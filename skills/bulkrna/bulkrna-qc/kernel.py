"""Lightweight kernel helpers for bulkrna-qc (count matrix QC metrics & CPM normalization)."""
from __future__ import annotations
__all__ = ['assess_count_matrix', 'cpm_normalize']

def cpm_normalize(counts: pd.DataFrame) -> pd.DataFrame:
    """Compute Counts Per Million (CPM) across a gene-by-sample count matrix."""
    if (counts.to_numpy(dtype=float) < 0).any():
        raise ValueError('Count matrix must be non-negative.')
    lib_sizes = counts.sum(axis=0)
    if (lib_sizes <= 0).any():
        raise ValueError(f'Zero-count sample libraries detected: {lib_sizes[lib_sizes <= 0].index.tolist()}')
    return counts.div(lib_sizes, axis=1) * 1000000.0

def assess_count_matrix(counts: pd.DataFrame, *, corr_z_cutoff: float=-2.0) -> pd.DataFrame:
    """Compute library size, gene detection rates, and correlation outlier flags per sample."""
    import numpy as np, pandas as pd
    cpm = cpm_normalize(counts)
    log_cpm = np.log2(cpm + 1.0)
    lib_size = counts.sum(axis=0)
    detected = (counts > 0).sum(axis=0)
    expressed_ge10 = (counts >= 10).sum(axis=0)
    n_samples = counts.shape[1]
    if n_samples > 1:
        corr = log_cpm.corr(method='pearson')
        mean_corr = (corr.sum(axis=1) - 1.0) / (n_samples - 1)
        std_c = float(mean_corr.std(ddof=1))
        z_corr = (mean_corr - mean_corr.mean()) / std_c if std_c > 1e-08 else pd.Series(0.0, index=counts.columns)
        outliers = (z_corr < corr_z_cutoff) | (lib_size < 0.2 * float(lib_size.median()))
    else:
        mean_corr = pd.Series(1.0, index=counts.columns)
        outliers = pd.Series(False, index=counts.columns)
    out = pd.DataFrame({'sample': counts.columns, 'total_counts': lib_size.values.astype(int), 'detected_genes': detected.values.astype(int), 'expressed_genes_ge10': expressed_ge10.values.astype(int), 'mean_sample_corr': mean_corr.values.astype(float), 'is_outlier': outliers.values.astype(bool)}).set_index('sample')
    out.attrs['outlier_samples'] = out.index[out['is_outlier']].tolist()
    return out
