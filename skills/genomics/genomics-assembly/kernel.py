"""Lightweight kernel helpers for genomics-assembly (FASTA contiguity: N50, L50, NG50, GC%)."""
from __future__ import annotations
__all__ = ['read_records', 'analyze', 'assembly_summary']

def read_records(path: str | Path) -> pd.DataFrame:
    """Read FASTA or CSV contig records into a DataFrame with contig and sequence."""
    import pandas as pd
    from pathlib import Path
    p = Path(path)
    if p.suffix.lower() == '.csv':
        df = pd.read_csv(p)
        df['sequence'] = df['sequence'].astype(str).str.upper()
        return df
    rows, name, seq = ([], None, [])
    with open(p, 'r', encoding='utf-8') as fh:
        for line in fh:
            s = line.strip()
            if s.startswith('>'):
                if name is not None:
                    rows.append({'contig': name, 'sequence': ''.join(seq).upper()})
                name, seq = (s[1:].split()[0], [])
            elif s:
                seq.append(s)
    if name is not None:
        rows.append({'contig': name, 'sequence': ''.join(seq).upper()})
    return pd.DataFrame(rows)

def analyze(data: pd.DataFrame, *, genome_size: int=0) -> pd.DataFrame:
    """Compute per-contig lengths/GC and assembly-wide N50, N90, L50, L90, and NG50."""
    import numpy as np, pandas as pd
    seqs = data['sequence'].astype(str).str.upper()
    lengths = seqs.str.len()
    gc = seqs.apply(lambda s: (s.count('G') + s.count('C')) / max(len(s) - s.count('N'), 1) * 100.0)
    out = pd.DataFrame({'contig': data['contig'], 'length': lengths, 'gc_pct': gc, 'n_count': seqs.str.count('N')})
    out = out.sort_values('length', ascending=False).reset_index(drop=True)
    arr = out['length'].to_numpy(dtype=np.int64)
    total = int(arr.sum())
    csum = np.cumsum(arr)
    idx50 = int(np.searchsorted(csum, total * 0.5)) if total > 0 else 0
    idx90 = int(np.searchsorted(csum, total * 0.9)) if total > 0 else 0
    info = {'n_contigs': int(len(arr)), 'total_length': total, 'largest_contig': int(arr[0]) if len(arr) else 0, 'n50': int(arr[idx50]) if len(arr) else 0, 'n90': int(arr[idx90]) if len(arr) else 0, 'l50': idx50 + 1 if len(arr) else 0, 'l90': idx90 + 1 if len(arr) else 0, 'gc_pct': float(out['gc_pct'].mean()) if len(out) else 0.0}
    if genome_size > 0 and len(arr):
        ng_idx = int(np.searchsorted(csum, genome_size * 0.5))
        info['ng50'] = int(arr[ng_idx]) if ng_idx < len(arr) else 0
        info['completeness_pct'] = float(total / genome_size * 100.0)
    out.attrs['run_info'] = info
    return out

def assembly_summary(data: pd.DataFrame, *, genome_size: int=0) -> dict:
    """Return dictionary of N50/L50/GC assembly summary metrics."""
    return analyze(data, genome_size=genome_size).attrs['run_info']
