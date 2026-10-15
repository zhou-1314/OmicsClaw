"""Lightweight kernel helpers for bulkrna-coexpression (soft thresholding, modules, kME hubs)."""
from __future__ import annotations
__all__ = ['select_soft_threshold', 'detect_coexpression_modules', 'compute_kme_hubs']

def select_soft_threshold(expr: pd.DataFrame, *, powers: list[int] | range=(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12), r2_cutoff: float=0.8) -> tuple[pd.DataFrame, int]:
    """Evaluate signed scale-free topology R^2 across candidate soft-threshold powers."""
    import numpy as np, pandas as pd
    cor = np.corrcoef(expr.to_numpy(dtype=float))
    sim = np.clip((1.0 + np.nan_to_num(cor, nan=0.0)) / 2.0, 0.0, 1.0)
    np.fill_diagonal(sim, 0.0)
    rows = []
    for p in powers:
        k = np.power(sim, p).sum(axis=1)
        pos = k[k > 0]
        r2 = 0.0
        if len(pos) >= 5:
            hist, edges = np.histogram(np.log10(pos), bins=10)
            m = hist > 0
            if m.sum() >= 3:
                r = np.corrcoef(0.5 * (edges[:-1][m] + edges[1:][m]), np.log10(hist[m] / hist.sum()))[0, 1]
                r2 = float(r ** 2) if not np.isnan(r) and r < 0 else 0.0
        rows.append({'power': int(p), 'r_squared': r2, 'mean_connectivity': float(k.mean())})
    fit_df = pd.DataFrame(rows)
    passing = fit_df[fit_df['r_squared'] >= r2_cutoff]
    best = int(passing['power'].iloc[0]) if not passing.empty else int(fit_df.loc[fit_df['r_squared'].idxmax(), 'power'])
    return (fit_df, best)

def detect_coexpression_modules(expr: pd.DataFrame, *, power: int=6, min_module_size: int=10) -> pd.DataFrame:
    """Cluster genes into signed co-expression modules via soft-thresholded adjacency."""
    import numpy as np, pandas as pd
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import squareform
    cor = np.corrcoef(expr.to_numpy(dtype=float))
    dist = np.clip(1.0 - np.power(np.clip((1.0 + np.nan_to_num(cor, nan=0.0)) / 2.0, 0.0, 1.0), power), 0.0, 1.0)
    np.fill_diagonal(dist, 0.0)
    labels = fcluster(linkage(squareform(dist, checks=False), method='average'), t=0.75, criterion='distance')
    counts = pd.Series(labels).value_counts()
    palette = ['turquoise', 'blue', 'brown', 'yellow', 'green', 'red', 'black', 'pink', 'magenta', 'purple']
    valid = [cid for cid, cnt in counts.items() if cnt >= min_module_size]
    cmap = {cid: palette[i % len(palette)] for i, cid in enumerate(valid)}
    return pd.DataFrame({'gene': expr.index, 'module': [cmap.get(c, 'grey') for c in labels]}).set_index('gene')

def compute_kme_hubs(expr: pd.DataFrame, modules_df: pd.DataFrame, *, top_n: int=10) -> pd.DataFrame:
    """Rank intramodular hub genes by absolute correlation (kME) with each module eigengene."""
    import numpy as np, pandas as pd
    records = []
    for mod, grp in modules_df.groupby('module'):
        if mod == 'grey' or len(grp) < 2:
            continue
        sub = expr.loc[grp.index].to_numpy(dtype=float)
        sub_z = (sub - sub.mean(axis=1, keepdims=True)) / np.maximum(sub.std(axis=1, keepdims=True), 1e-08)
        _, _, vt = np.linalg.svd(sub_z, full_matrices=False)
        kme = sub_z @ vt[0] / sub_z.shape[1]
        df_m = pd.DataFrame({'gene': grp.index, 'module': mod, 'kME': kme})
        records.append(df_m.reindex(df_m['kME'].abs().sort_values(ascending=False).index).head(top_n))
    return pd.concat(records, ignore_index=True) if records else pd.DataFrame(columns=['gene', 'module', 'kME'])
