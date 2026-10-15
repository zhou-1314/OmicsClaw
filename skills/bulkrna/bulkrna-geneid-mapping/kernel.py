"""Lightweight kernel helpers for bulkrna-geneid-mapping (Ensembl version stripping & ID aggregation)."""
from __future__ import annotations
__all__ = ['strip_ensembl_version', 'map_and_aggregate_counts']

def strip_ensembl_version(ids: list[str] | pd.Index) -> list[str]:
    """Strip trailing version suffixes (e.g. .15) from Ensembl gene identifiers."""
    import re
    _VERSION_RE = re.compile('^(ENS[A-Z]*G\\d+)\\.\\d+(?:_PAR_[XY])?$')
    return [_VERSION_RE.sub('\\1', str(x).strip()) for x in ids]

def map_and_aggregate_counts(counts: pd.DataFrame, *, mapping: dict[str, str] | pd.Series, on_duplicate: str='sum', retain_unmapped: bool=True) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Map gene row index using a dictionary/Series and resolve target symbol collisions."""
    import pandas as pd
    map_dict = dict(mapping)
    orig_ids = [str(i) for i in counts.index]
    stripped = strip_ensembl_version(orig_ids)
    mapped_ids, was_mapped = ([], [])
    for orig, clean in zip(orig_ids, stripped):
        target = map_dict.get(clean) or map_dict.get(orig)
        if target is not None and str(target).strip() != '' and pd.notna(target):
            mapped_ids.append(str(target).strip())
            was_mapped.append(True)
        else:
            mapped_ids.append(clean if retain_unmapped else '')
            was_mapped.append(False)
    audit_df = pd.DataFrame({'original_id': orig_ids, 'stripped_id': stripped, 'mapped_id': mapped_ids, 'was_mapped': was_mapped})
    out = counts.copy()
    out.index = mapped_ids
    if not retain_unmapped:
        out = out.loc[[m != '' for m in out.index]]
    if on_duplicate == 'sum':
        out = out.groupby(level=0, sort=False).sum()
    elif on_duplicate == 'first':
        out = out.loc[~out.index.duplicated(keep='first')]
    elif on_duplicate == 'drop':
        out = out.loc[~out.index.duplicated(keep=False)]
    else:
        raise ValueError(f'Unsupported on_duplicate policy: {on_duplicate}')
    return (out, audit_df)
