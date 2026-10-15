"""Lightweight kernel helpers for genomics-phasing (VCF GT/PS parsing & phase block N50)."""
from __future__ import annotations
__all__ = ['read_records', 'analyze', 'phase_blocks']

def read_records(path: str | Path) -> pd.DataFrame:
    """Parse VCF or CSV records into chrom, pos, gt, is_het, is_phased, and phase_set."""
    import pandas as pd
    from pathlib import Path
    p = Path(path)
    if p.suffix.lower() == '.csv':
        return pd.read_csv(p)
    rows = []
    with open(p, 'r', encoding='utf-8') as fh:
        for line in fh:
            if not line.strip() or line.startswith('#'):
                continue
            parts = line.rstrip('\n').split('\t')
            if len(parts) < 10:
                continue
            fmt = parts[8].split(':')
            smp = parts[9].split(':')
            kv = dict(zip(fmt, smp))
            gt = kv.get('GT', './.')
            alleles = gt.replace('|', '/').split('/')
            is_het = len(alleles) == 2 and alleles[0] != alleles[1] and ('.' not in alleles)
            is_phased = '|' in gt and is_het
            ps = kv.get('PS', parts[1] if is_phased else '.')
            rows.append({'chrom': parts[0], 'pos': int(parts[1]), 'gt': gt, 'is_het': is_het, 'is_phased': is_phased, 'phase_set': str(ps)})
    return pd.DataFrame(rows)

def phase_blocks(data: pd.DataFrame) -> pd.DataFrame:
    """Aggregate phased heterozygous variants into contiguous phase blocks."""
    import pandas as pd
    phased = data[data['is_phased'] & data['is_het']] if len(data) else data.iloc[0:0]
    if phased.empty:
        return pd.DataFrame(columns=['chrom', 'phase_set', 'n_variants', 'start_pos', 'end_pos', 'span_bp'])
    grp = phased.groupby(['chrom', 'phase_set'], as_index=False).agg(n_variants=('pos', 'count'), start_pos=('pos', 'min'), end_pos=('pos', 'max'))
    grp['span_bp'] = grp['end_pos'] - grp['start_pos']
    return grp.sort_values('span_bp', ascending=False).reset_index(drop=True)

def analyze(data: pd.DataFrame) -> pd.DataFrame:
    """Annotate phased variants and compute heterozygous phasing rate and block N50."""
    import numpy as np
    df = data.copy()
    blocks = phase_blocks(df)
    spans = blocks['span_bp'].to_numpy(dtype=np.int64)
    csum = np.cumsum(spans)
    n50 = int(spans[int(np.searchsorted(csum, csum[-1] * 0.5))]) if len(spans) and csum[-1] > 0 else 0
    n_het = int(df['is_het'].sum()) if len(df) else 0
    n_phased = int((df['is_het'] & df['is_phased']).sum()) if len(df) else 0
    df.attrs['run_info'] = {'n_variants': int(len(df)), 'n_het': n_het, 'n_phased_het': n_phased, 'phasing_rate_pct': float(n_phased / max(n_het, 1) * 100.0) if n_het else 0.0, 'n_phase_blocks': int(len(blocks)), 'block_n50_bp': n50}
    return df
