"""Lightweight kernel helpers for genomics-qc (FASTQ Phred+33 quality, Q20/Q30, and GC% QC)."""
from __future__ import annotations
__all__ = ['read_records', 'analyze', 'qc_summary']

def read_records(path: str | Path, *, max_reads: int=500000) -> pd.DataFrame:
    """Read plain or gzipped FASTQ (or CSV) into sequence and quality columns."""
    import gzip, pandas as pd
    from pathlib import Path
    p = Path(path)
    if p.suffix.lower() == '.csv':
        return pd.read_csv(p, nrows=max_reads)
    opener = gzip.open if p.suffix.lower() == '.gz' else open
    rows = []
    with opener(p, 'rt', encoding='utf-8') as fh:
        while len(rows) < max_reads:
            hdr = fh.readline()
            if not hdr:
                break
            seq = fh.readline().rstrip('\n')
            plus = fh.readline()
            qual = fh.readline().rstrip('\n')
            if not plus or len(seq) != len(qual) or (not seq):
                raise ValueError('Malformed FASTQ record or mismatched sequence/quality length.')
            rows.append({'read_id': hdr[1:].split()[0], 'sequence': seq.upper(), 'quality': qual})
    return pd.DataFrame(rows)

def analyze(data: pd.DataFrame, *, max_reads: int=500000) -> pd.DataFrame:
    """Compute per-read Phred+33 mean quality, Q20/Q30 base rates, length, and GC%."""
    import numpy as np
    df = data.iloc[:max_reads].copy()
    seqs = df['sequence'].astype(str).str.upper()
    quals = df['quality'].astype(str)
    q_arrs = [np.frombuffer(q.encode('ascii'), dtype=np.uint8).astype(float) - 33.0 for q in quals]
    df['length'] = seqs.str.len()
    df['mean_quality'] = [float(a.mean()) if len(a) else 0.0 for a in q_arrs]
    df['q20_pct'] = [float((a >= 20).mean() * 100.0) if len(a) else 0.0 for a in q_arrs]
    df['q30_pct'] = [float((a >= 30).mean() * 100.0) if len(a) else 0.0 for a in q_arrs]
    df['gc_pct'] = [float((s.count('G') + s.count('C')) / max(len(s) - s.count('N'), 1) * 100.0) for s in seqs]
    df.attrs['run_info'] = {'n_reads': int(len(df)), 'total_bases': int(df['length'].sum()), 'mean_length': float(df['length'].mean()) if len(df) else 0.0, 'mean_quality': float(df['mean_quality'].mean()) if len(df) else 0.0, 'q20_pct': float(df['q20_pct'].mean()) if len(df) else 0.0, 'q30_pct': float(df['q30_pct'].mean()) if len(df) else 0.0, 'gc_pct': float(df['gc_pct'].mean()) if len(df) else 0.0}
    return df

def qc_summary(data: pd.DataFrame, *, max_reads: int=500000) -> dict:
    """Return dictionary summary of FASTQ read count, Q20/Q30, and GC content."""
    return analyze(data, max_reads=max_reads).attrs['run_info']
