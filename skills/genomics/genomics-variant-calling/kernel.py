"""Lightweight kernel helpers for genomics-variant-calling (VCF parsing, SNP/INDEL classification & Ti/Tv)."""
from __future__ import annotations
__all__ = ['read_records', 'analyze', 'variant_summary']

def read_records(path: str | Path) -> pd.DataFrame:
    """Parse VCF or CSV records, splitting multi-allelic ALT fields into separate rows."""
    import pandas as pd
    from pathlib import Path

    def _var_type(ref: str, alt: str) -> str:
        if len(ref) == 1 and len(alt) == 1:
            return 'SNP'
        return 'MNP' if len(ref) == len(alt) else 'INDEL'
    p = Path(path)
    if p.suffix.lower() == '.csv':
        return pd.read_csv(p)
    rows = []
    with open(p, 'r', encoding='utf-8') as fh:
        for line in fh:
            if not line.strip() or line.startswith('#'):
                continue
            parts = line.rstrip('\n').split('\t')
            ref = parts[3].upper()
            qual = float(parts[5]) if parts[5] != '.' else 0.0
            for alt in parts[4].upper().split(','):
                rows.append({'chrom': parts[0], 'pos': int(parts[1]), 'ref': ref, 'alt': alt, 'qual': qual, 'filter': parts[6], 'type': _var_type(ref, alt)})
    return pd.DataFrame(rows)

def analyze(data: pd.DataFrame) -> pd.DataFrame:
    """Classify SNPs/INDELs and compute PASS rate and transition/transversion (Ti/Tv) ratio."""
    import pandas as pd
    _TRANSITIONS = {('A', 'G'), ('G', 'A'), ('C', 'T'), ('T', 'C')}

    def _var_type(ref: str, alt: str) -> str:
        if len(ref) == 1 and len(alt) == 1:
            return 'SNP'
        return 'MNP' if len(ref) == len(alt) else 'INDEL'
    df = data.copy()
    if 'type' not in df.columns:
        df['type'] = [_var_type(str(r).upper(), str(a).upper()) for r, a in zip(df['ref'], df['alt'])]
    snps = df[df['type'] == 'SNP']
    pairs = list(zip(snps['ref'].str.upper(), snps['alt'].str.upper()))
    n_ti = sum((1 for p in pairs if p in _TRANSITIONS))
    n_tv = len(pairs) - n_ti
    df.attrs['run_info'] = {'n_variants': int(len(df)), 'n_snps': int((df['type'] == 'SNP').sum()), 'n_indels': int((df['type'] == 'INDEL').sum()), 'n_pass': int(df['filter'].isin(['PASS', '.']).sum()) if 'filter' in df.columns else int(len(df)), 'ti_tv_ratio': float(n_ti / max(n_tv, 1)) if len(pairs) else 0.0, 'mean_qual': float(pd.to_numeric(df['qual'], errors='coerce').mean()) if len(df) else 0.0}
    return df

def variant_summary(data: pd.DataFrame) -> dict:
    """Return dictionary summary of SNP/indel counts and Ti/Tv ratio."""
    return analyze(data).attrs['run_info']
