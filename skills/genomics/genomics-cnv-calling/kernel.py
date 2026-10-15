"""Lightweight kernel helpers for genomics-cnv-calling (CBS segmentation & CN state assignment)."""
from __future__ import annotations
__all__ = ['read_bins', 'analyze', 'cnv_summary']

def read_bins(path: str | Path) -> pd.DataFrame:
    """Load genomic copy-ratio bins (chrom, start, end, log2_ratio)."""
    import pandas as pd
    df = pd.read_csv(path)
    req = {'chrom', 'start', 'end', 'log2_ratio'}
    if not req.issubset(df.columns):
        raise ValueError(f'Missing required columns: {req - set(df.columns)}')
    return df.sort_values(['chrom', 'start', 'end']).reset_index(drop=True)

def analyze(data: pd.DataFrame, *, method: str='cbs', alpha: float=0.01) -> pd.DataFrame:
    """Segment per-chromosome log2 copy ratios and classify deletions/duplications."""
    import numpy as np, pandas as pd

    def _segment_chrom(sub: pd.DataFrame, method: str, alpha: float) -> list[dict]:
        vals = sub['log2_ratio'].to_numpy(dtype=float)
        if method == 'none' or len(vals) < 4:
            splits = [[i] for i in range(len(vals))] if method == 'none' else [list(range(len(vals)))]
        else:
            thresh = 0.35 if alpha <= 0.01 else 0.25
            diffs = np.abs(np.diff(vals))
            cuts = [0] + [i + 1 for i, d in enumerate(diffs) if d >= thresh] + [len(vals)]
            splits = [list(range(cuts[j], cuts[j + 1])) for j in range(len(cuts) - 1)]
        rows = []
        for idx in splits:
            chunk = sub.iloc[idx]
            mean_l2r = float(chunk['log2_ratio'].mean())
            cn = int(max(0, round(2.0 * 2.0 ** mean_l2r)))
            state = 'deletion' if mean_l2r < -0.3 else 'duplication' if mean_l2r > 0.3 else 'neutral'
            rows.append({'chrom': str(chunk['chrom'].iloc[0]), 'start': int(chunk['start'].iloc[0]), 'end': int(chunk['end'].iloc[-1]), 'n_bins': int(len(chunk)), 'log2_ratio': mean_l2r, 'estimated_cn': cn, 'cn_state': state})
        return rows
    segs = []
    for _, sub in data.groupby('chrom', sort=False):
        segs.extend(_segment_chrom(sub.sort_values('start'), method, alpha))
    out = pd.DataFrame(segs)
    out.attrs['run_info'] = {'n_segments': int(len(out)), 'n_deletions': int((out['cn_state'] == 'deletion').sum()) if len(out) else 0, 'n_duplications': int((out['cn_state'] == 'duplication').sum()) if len(out) else 0}
    return out

def cnv_summary(segments: pd.DataFrame) -> dict:
    """Summarize total segments, deletions, and duplications."""
    return segments.attrs.get('run_info') or {'n_segments': int(len(segments))}
