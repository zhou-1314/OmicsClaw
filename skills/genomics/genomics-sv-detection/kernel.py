"""Lightweight kernel helpers for genomics-sv-detection (SV VCF parsing & size-class stratification)."""
from __future__ import annotations
__all__ = ['read_records', 'analyze', 'sv_summary']

def read_records(path: str | Path) -> pd.DataFrame:
    """Parse SV VCF or CSV into chrom, pos, sv_type, sv_len, filter, and genotype."""
    import pandas as pd
    from pathlib import Path

    def _size_class(sv_type: str, sv_len: int) -> str:
        if sv_type == 'BND' or sv_len <= 0:
            return 'translocation'
        if sv_len < 1000:
            return 'small_50_1kb'
        return 'medium_1kb_10kb' if sv_len < 10000 else 'large_10kb_plus'
    p = Path(path)
    if p.suffix.lower() == '.csv':
        return pd.read_csv(p)
    rows = []
    with open(p, 'r', encoding='utf-8') as fh:
        for line in fh:
            if not line.strip() or line.startswith('#'):
                continue
            parts = line.rstrip('\n').split('\t')
            info = dict((item.split('=', 1) for item in parts[7].split(';') if '=' in item))
            sv_type = info.get('SVTYPE', 'UNK')
            raw_len = info.get('SVLEN', '0').split(',')[0]
            sv_len = abs(int(raw_len)) if raw_len.lstrip('-').isdigit() else max(0, int(info.get('END', parts[1])) - int(parts[1]))
            gt = parts[9].split(':')[0] if len(parts) > 9 else './.'
            rows.append({'chrom': parts[0], 'pos': int(parts[1]), 'sv_type': sv_type, 'sv_len': sv_len, 'filter': parts[6], 'genotype': gt, 'size_class': _size_class(sv_type, sv_len)})
    return pd.DataFrame(rows)

def analyze(data: pd.DataFrame) -> pd.DataFrame:
    """Standardize SV lengths, assign size classes, and compute SV type breakdown."""
    import pandas as pd

    def _size_class(sv_type: str, sv_len: int) -> str:
        if sv_type == 'BND' or sv_len <= 0:
            return 'translocation'
        if sv_len < 1000:
            return 'small_50_1kb'
        return 'medium_1kb_10kb' if sv_len < 10000 else 'large_10kb_plus'
    df = data.copy()
    df['sv_len'] = df['sv_len'].astype(int).abs()
    if 'size_class' not in df.columns:
        df['size_class'] = [_size_class(t, l) for t, l in zip(df['sv_type'], df['sv_len'])]
    pass_mask = df['filter'].isin(['PASS', '.']) if 'filter' in df.columns else pd.Series(True, index=df.index)
    df.attrs['run_info'] = {'n_total': int(len(df)), 'n_pass': int(pass_mask.sum()), 'by_type': {str(k): int(v) for k, v in df['sv_type'].value_counts().items()}, 'median_sv_len': float(df.loc[df['sv_len'] > 0, 'sv_len'].median()) if (df['sv_len'] > 0).any() else 0.0}
    return df

def sv_summary(data: pd.DataFrame) -> dict:
    """Return dictionary summary of SV counts by type and median size."""
    return analyze(data).attrs['run_info']
