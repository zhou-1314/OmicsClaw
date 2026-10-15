"""Lightweight kernel helpers for bulkrna-batch-correction (ComBat & batch R2 diagnostics)."""
from __future__ import annotations
__all__ = ['combat_empirical_bayes', 'batch_variance_metrics']

def combat_empirical_bayes(expr: pd.DataFrame, *, batch: pd.Series | list[str], condition: pd.Series | None=None) -> pd.DataFrame:
    """Parametric empirical Bayes location-scale batch adjustment across genes x samples."""
    import numpy as np, pandas as pd
    b_ser = pd.Series(batch, index=expr.columns).astype(str)
    batches = b_ser.unique()
    if len(batches) < 2:
        raise ValueError('At least two batches are required for batch correction.')
    X = expr.to_numpy(dtype=float, copy=True)
    grand_mean = X.mean(axis=1, keepdims=True)
    var_pooled = np.where(X.var(axis=1, ddof=1, keepdims=True) < 1e-08, 1.0, X.var(axis=1, ddof=1, keepdims=True))
    s_data = (X - grand_mean) / np.sqrt(var_pooled)
    gamma_hat = np.zeros((X.shape[0], len(batches)))
    delta_hat = np.ones((X.shape[0], len(batches)))
    for i, b in enumerate(batches):
        sub = s_data[:, (b_ser == b).to_numpy()]
        gamma_hat[:, i] = sub.mean(axis=1)
        delta_hat[:, i] = np.maximum(sub.var(axis=1, ddof=max(1, sub.shape[1] - 1)), 1e-06)
    gamma_bar, tau2 = (gamma_hat.mean(axis=0), np.maximum(gamma_hat.var(axis=0, ddof=1), 1e-06))
    adjusted = np.empty_like(s_data)
    for i, b in enumerate(batches):
        mask = (b_ser == b).to_numpy()
        n_b = mask.sum()
        g_star = (n_b * tau2[i] * gamma_hat[:, i] + delta_hat[:, i] * gamma_bar[i]) / (n_b * tau2[i] + delta_hat[:, i])
        d_star = 0.5 * delta_hat[:, i] + 0.5 * delta_hat[:, i].mean()
        adjusted[:, mask] = (s_data[:, mask] - g_star[:, None]) / np.sqrt(d_star[:, None])
    return pd.DataFrame(adjusted * np.sqrt(var_pooled) + grand_mean, index=expr.index, columns=expr.columns)

def batch_variance_metrics(raw_expr: pd.DataFrame, corrected_expr: pd.DataFrame, *, batch: pd.Series | list[str]) -> dict[str, float]:
    """Compute fraction of variance explained by batch on PC1 before and after correction."""
    import numpy as np, pandas as pd
    b_ser = pd.Series(batch, index=raw_expr.columns).astype(str)

    def _pc1_r2(mat: pd.DataFrame) -> float:
        centered = mat.to_numpy(dtype=float).T - mat.to_numpy(dtype=float).T.mean(axis=0, keepdims=True)
        u, s, _ = np.linalg.svd(centered, full_matrices=False)
        pc1 = u[:, 0] * s[0]
        ss_tot = float(np.sum((pc1 - pc1.mean()) ** 2))
        if ss_tot <= 1e-12:
            return 0.0
        ss_btw = sum(((b_ser == b).sum() * (pc1[b_ser == b].mean() - pc1.mean()) ** 2 for b in b_ser.unique()))
        return float(ss_btw / ss_tot)
    return {'pc1_batch_r2_before': _pc1_r2(raw_expr), 'pc1_batch_r2_after': _pc1_r2(corrected_expr)}
