"""Lightweight kernel helpers for genomics-alignment (SAM/BAM flag & MAPQ QC)."""
from __future__ import annotations
__all__ = ['read_records', 'analyze', 'alignment_summary']

def read_records(path: str | Path) -> pd.DataFrame:
    """Parse text SAM or tabular alignment records with flag, mapq, and tlen."""
    import pandas as pd
    from pathlib import Path
    p = Path(path)
    if p.suffix.lower() == '.csv':
        return pd.read_csv(p)
    rows = []
    with open(p, 'r', encoding='utf-8') as fh:
        for line in fh:
            if not line.strip() or line.startswith('@'):
                continue
            parts = line.rstrip('\n').split('\t')
            if len(parts) >= 9:
                rows.append({'qname': parts[0], 'flag': int(parts[1]), 'rname': parts[2], 'pos': int(parts[3]), 'mapq': int(parts[4]), 'cigar': parts[5], 'tlen': int(parts[8])})
    return pd.DataFrame(rows)

def analyze(data: pd.DataFrame) -> pd.DataFrame:
    """Compute primary alignment rate, proper pairing, duplicate rate, and insert size."""
    import pandas as pd
    df = data.copy()
    flags = df['flag'].astype(int)
    primary = df[flags & 2304 == 0]
    p_flags = primary['flag'].astype(int)
    mapped = p_flags & 4 == 0
    proper = p_flags & 2 != 0
    dup = p_flags & 1024 != 0
    pos_tlen = primary.loc[mapped & (primary['tlen'] > 0), 'tlen']
    mapped_mapq = primary.loc[mapped, 'mapq']
    n_total = max(len(primary), 1)
    summary = {'total_reads': int(len(primary)), 'mapped_reads': int(mapped.sum()), 'unmapped_reads': int((~mapped).sum()), 'mapping_rate_pct': float(mapped.mean() * 100.0) if len(primary) else 0.0, 'properly_paired_pct': float(proper.sum() / n_total * 100.0), 'duplicate_rate_pct': float(dup.sum() / n_total * 100.0), 'mean_mapq': float(mapped_mapq.mean()) if len(mapped_mapq) else 0.0, 'q20_mapq_pct': float((mapped_mapq >= 20).mean() * 100.0) if len(mapped_mapq) else 0.0, 'mean_insert_size': float(pos_tlen.mean()) if len(pos_tlen) else 0.0, 'median_insert_size': float(pos_tlen.median()) if len(pos_tlen) else 0.0}
    out = pd.DataFrame([summary])
    out.attrs['run_info'] = summary
    return out

def alignment_summary(data: pd.DataFrame) -> dict:
    """Return dictionary summary of primary alignment QC metrics."""
    return analyze(data).iloc[0].to_dict()
