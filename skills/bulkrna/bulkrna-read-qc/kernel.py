"""Lightweight kernel helpers for bulkrna-read-qc (FASTQ Phred+33, Q20/Q30, GC & adapter metrics)."""
from __future__ import annotations
__all__ = ['read_fastq_sample', 'compute_fastq_qc']

def read_fastq_sample(path: str | Path, *, max_reads: int=100000) -> pd.DataFrame:
    """Read up to max_reads 4-line FASTQ records (.fastq or .fastq.gz) into a DataFrame."""
    import gzip, pandas as pd
    from pathlib import Path
    p = Path(path)
    opener = gzip.open if p.suffix == '.gz' else open
    seqs, quals = ([], [])
    with opener(p, 'rt', encoding='utf-8') as fh:
        while len(seqs) < max_reads:
            header = fh.readline()
            if not header:
                break
            seq = fh.readline().rstrip('\r\n')
            plus = fh.readline()
            qual = fh.readline().rstrip('\r\n')
            if not header.startswith('@') or not plus.startswith('+') or len(seq) == 0 or (len(seq) != len(qual)):
                raise ValueError('Malformed or truncated FASTQ record encountered.')
            seqs.append(seq.upper())
            quals.append(qual)
    if not seqs:
        raise ValueError('FASTQ input contains no reads.')
    return pd.DataFrame({'sequence': seqs, 'quality': quals})

def compute_fastq_qc(reads: pd.DataFrame) -> pd.DataFrame:
    """Compute Phred+33 mean quality, Q20/Q30 rates, GC%, and adapter hit rate."""
    import numpy as np, pandas as pd
    _ADAPTERS = ('AGATCGGAAGAGC', 'CTGTCTCTTATACACATCT', 'TGGAATTCTCGG')
    if reads.empty:
        raise ValueError('Reads DataFrame is empty.')
    seqs = reads['sequence'].astype(str).tolist()
    quals = reads['quality'].astype(str).tolist()
    all_q = np.concatenate([np.frombuffer(q.encode('ascii'), dtype=np.uint8).astype(int) - 33 for q in quals])
    if (all_q < 0).any() or (all_q > 93).any():
        raise ValueError('Invalid Phred+33 quality scores outside [0, 93].')
    total_bases = len(all_q)
    joined_seq = ''.join(seqs)
    gc_pct = 100.0 * (joined_seq.count('G') + joined_seq.count('C')) / max(len(joined_seq), 1)
    adapter_hits = sum((sum((s.count(ad) for ad in _ADAPTERS)) for s in seqs))
    return pd.DataFrame([{'total_reads': len(seqs), 'mean_length': float(np.mean([len(s) for s in seqs])), 'mean_quality': float(np.mean(all_q)), 'q20_rate': float(100.0 * np.sum(all_q >= 20) / total_bases), 'q30_rate': float(100.0 * np.sum(all_q >= 30) / total_bases), 'gc_content': float(gc_pct), 'adapter_rate': float(100.0 * adapter_hits / len(seqs))}])
