"""Lightweight kernel helpers for genomics-variant-annotation (VEP/SnpEff impact & CADD prioritization)."""
from __future__ import annotations
__all__ = ['read_records', 'analyze', 'impact_distribution']

def read_records(path: str | Path) -> pd.DataFrame:
    """Read annotated variant records from CSV or TSV."""
    import pandas as pd
    from pathlib import Path
    p = Path(path)
    sep = '\t' if p.suffix.lower() in {'.tsv', '.txt'} else ','
    return pd.read_csv(p, sep=sep)

def impact_distribution(data: pd.DataFrame) -> pd.DataFrame:
    """Compute counts and percentages across HIGH, MODERATE, LOW, and MODIFIER tiers."""
    import pandas as pd
    vc = data['impact'].value_counts() if 'impact' in data.columns else pd.Series(dtype=int)
    total = max(int(vc.sum()), 1)
    tiers = ['HIGH', 'MODERATE', 'LOW', 'MODIFIER']
    rows = [{'impact': t, 'count': int(vc.get(t, 0)), 'pct': float(vc.get(t, 0) / total * 100.0)} for t in tiers if t in vc or len(vc) == 0]
    for extra in vc.index.difference(tiers):
        rows.append({'impact': str(extra), 'count': int(vc[extra]), 'pct': float(vc[extra] / total * 100.0)})
    return pd.DataFrame(rows)

def analyze(data: pd.DataFrame) -> pd.DataFrame:
    """Validate annotated variants and summarize HIGH/MODERATE impacts and CADD scores."""
    import pandas as pd
    df = data.copy()
    req = {'impact', 'consequence', 'gene', 'cadd_phred'}
    if not req.issubset(df.columns):
        raise ValueError(f'Missing required annotation columns: {req - set(df.columns)}')
    df['cadd_phred'] = pd.to_numeric(df['cadd_phred'], errors='coerce').fillna(0.0)
    df.attrs['run_info'] = {'n_variants': int(len(df)), 'n_high_impact': int((df['impact'] == 'HIGH').sum()), 'n_moderate_impact': int((df['impact'] == 'MODERATE').sum()), 'n_cadd_ge20': int((df['cadd_phred'] >= 20.0).sum()), 'mean_cadd_phred': float(df['cadd_phred'].mean()) if len(df) else 0.0, 'n_genes': int(df['gene'].nunique())}
    return df
