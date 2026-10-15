"""Lightweight kernel helpers for sc-fastq-qc (FASTQ Phred quality, GC content, and Q30 summaries)."""
from __future__ import annotations
__all__ = ['summarize_fastq', 'per_base_quality_table', 'fastq_qc_summary']

def per_base_quality_table(fastq_path: str, *, max_reads: int=20000) -> pd.DataFrame:
    """Compute per-position mean, median, and IQR Phred quality scores across sampled reads."""
    import gzip, numpy as np, pandas as pd

    def _iter_fastq(path: str, max_reads: int):
        opener = gzip.open if str(path).endswith('.gz') else open
        with opener(path, 'rt', encoding='utf-8', errors='replace') as fh:
            for i in range(max_reads):
                header = fh.readline()
                if not header:
                    break
                seq = fh.readline().rstrip('\r\n')
                fh.readline()
                qual = fh.readline().rstrip('\r\n')
                if seq and qual:
                    yield (seq, qual)
    quals = [np.frombuffer(q.encode('ascii'), dtype=np.uint8) - 33 for _, q in _iter_fastq(fastq_path, max_reads)]
    if not quals:
        return pd.DataFrame(columns=['position', 'mean_phred', 'q25_phred', 'median_phred', 'q75_phred'])
    max_len = max((len(q) for q in quals))
    mat = np.full((len(quals), max_len), np.nan, dtype=np.float32)
    for idx, q in enumerate(quals):
        mat[idx, :len(q)] = q
    return pd.DataFrame({'position': np.arange(1, max_len + 1), 'mean_phred': np.nanmean(mat, axis=0), 'q25_phred': np.nanpercentile(mat, 25, axis=0), 'median_phred': np.nanmedian(mat, axis=0), 'q75_phred': np.nanpercentile(mat, 75, axis=0)})

def summarize_fastq(fastq_path: str, *, max_reads: int=20000) -> dict:
    """Return read count, mean read length, GC%, and Q30 base percentage for a FASTQ file."""
    import gzip, pathlib, numpy as np

    def _iter_fastq(path: str, max_reads: int):
        opener = gzip.open if str(path).endswith('.gz') else open
        with opener(path, 'rt', encoding='utf-8', errors='replace') as fh:
            for i in range(max_reads):
                header = fh.readline()
                if not header:
                    break
                seq = fh.readline().rstrip('\r\n')
                fh.readline()
                qual = fh.readline().rstrip('\r\n')
                if seq and qual:
                    yield (seq, qual)
    n_reads, total_bases, gc_bases, q30_bases = (0, 0, 0, 0)
    for seq, qual in _iter_fastq(fastq_path, max_reads):
        n_reads += 1
        total_bases += len(seq)
        gc_bases += seq.upper().count('G') + seq.upper().count('C')
        q30_bases += int((np.frombuffer(qual.encode('ascii'), dtype=np.uint8) - 33 >= 30).sum())
    denom = max(total_bases, 1)
    return {'file': pathlib.Path(fastq_path).name, 'sampled_reads': n_reads, 'mean_length': float(total_bases / max(n_reads, 1)), 'gc_pct': float(100.0 * gc_bases / denom), 'q30_pct': float(100.0 * q30_bases / denom)}

def fastq_qc_summary(summaries: list[dict]) -> pd.DataFrame:
    """Combine per-file FASTQ quality dictionaries into a summary DataFrame."""
    import pandas as pd
    return pd.DataFrame(summaries)
