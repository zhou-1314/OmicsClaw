"""Lightweight kernel helpers for genomics-vcf-operations (VCF parsing, QUAL/DP filtering & summary)."""
from __future__ import annotations
__all__ = ['read_records', 'analyze', 'vcf_summary']

def read_records(path: str | Path) -> pd.DataFrame:
    """Parse VCF or CSV records into chrom, pos, ref, alt, qual, filter, dp, and type."""
    import pandas as pd
    from pathlib import Path

    def _parse_dp(info_str: str) -> int:
        for item in str(info_str).split(';'):
            if item.startswith('DP='):
                val = item.split('=', 1)[1].split(',')[0]
                return int(val) if val.isdigit() else 0
        return 0
    p = Path(path)
    if p.suffix.lower() == '.csv':
        return pd.read_csv(p)
    headers, rows = ([], [])
    with open(p, 'r', encoding='utf-8') as fh:
        for line in fh:
            if line.startswith('#'):
                headers.append(line.rstrip('\n'))
                continue
            if not line.strip():
                continue
            parts = line.rstrip('\n').split('\t')
            ref = parts[3].upper()
            qual = float(parts[5]) if parts[5] != '.' else 0.0
            dp = _parse_dp(parts[7])
            for alt in parts[4].upper().split(','):
                vtype = 'SNP' if len(ref) == 1 and len(alt) == 1 else 'MNP' if len(ref) == len(alt) else 'INDEL'
                rows.append({'chrom': parts[0], 'pos': int(parts[1]), 'id': parts[2], 'ref': ref, 'alt': alt, 'qual': qual, 'filter': parts[6], 'dp': dp, 'type': vtype})
    df = pd.DataFrame(rows)
    df.attrs['vcf_headers'] = headers
    return df

def analyze(data: pd.DataFrame, *, min_qual: float=0.0, min_dp: int=0) -> pd.DataFrame:
    """Filter VCF records by minimum QUAL and INFO/DP and compute summary statistics."""
    import pandas as pd
    df = data.copy()
    df['qual'] = pd.to_numeric(df['qual'], errors='coerce').fillna(0.0)
    df['dp'] = pd.to_numeric(df['dp'], errors='coerce').fillna(0).astype(int)
    out = df[(df['qual'] >= min_qual) & (df['dp'] >= min_dp)].reset_index(drop=True)
    out.attrs['vcf_headers'] = data.attrs.get('vcf_headers', [])
    out.attrs['run_info'] = {'n_input': int(len(df)), 'n_retained': int(len(out)), 'min_qual': float(min_qual), 'min_dp': int(min_dp), 'mean_qual': float(out['qual'].mean()) if len(out) else 0.0, 'mean_dp': float(out['dp'].mean()) if len(out) else 0.0}
    return out

def vcf_summary(data: pd.DataFrame, *, min_qual: float=0.0, min_dp: int=0) -> dict:
    """Return dictionary summary of input vs. retained VCF variants."""
    return analyze(data, min_qual=min_qual, min_dp=min_dp).attrs['run_info']
