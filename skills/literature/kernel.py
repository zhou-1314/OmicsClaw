"""Lightweight kernel helper for literature metadata parsing."""
from __future__ import annotations
__all__ = ['extract_accessions', 'extract_dois_pmids', 'extract_method_parameters']

def extract_accessions(text: str) -> list[str]:
    """Extract unique GEO/SRA/ArrayExpress accession IDs in appearance order."""
    import re
    _ACCESSION_RE = re.compile('\\b(GSE\\d+|GSM\\d+|GPL\\d+|PRJNA\\d+|SRP\\d+|SRR\\d+|E-MTAB-\\d+)\\b')
    return list(dict.fromkeys(_ACCESSION_RE.findall(text)))

def extract_dois_pmids(text: str) -> dict[str, list[str]]:
    """Extract unique DOIs (with trailing punctuation stripped) and PubMed IDs from text."""
    import re
    _DOI_RE = re.compile('\\b(10\\.\\d{4,9}/[-._;()/:A-Za-z0-9]+)\\b')
    _PMID_RE = re.compile('\\bPMID[:\\s]*(\\d{6,9})\\b', re.IGNORECASE)
    dois = list(dict.fromkeys((d.rstrip('.,;)') for d in _DOI_RE.findall(text))))
    pmids = list(dict.fromkeys(_PMID_RE.findall(text)))
    return {'dois': dois, 'pmids': pmids}

def extract_method_parameters(text: str) -> pd.DataFrame:
    """Extract stated numeric method thresholds with exact character spans."""
    import re, pandas as pd
    _PARAM_RE = re.compile('\\b(padj|FDR|p-value|pvalue|log2FC|log2FoldChange|min_count|resolution)\\s*(<=|>=|<|>|=)\\s*([0-9]+(?:\\.[0-9]+)?(?:e-?\\d+)?)', re.IGNORECASE)
    rows = [{'param': m.group(1), 'operator': m.group(2), 'value': float(m.group(3)), 'quote': m.group(0), 'start': m.start(), 'end': m.end()} for m in _PARAM_RE.finditer(text)]
    return pd.DataFrame(rows, columns=['param', 'operator', 'value', 'quote', 'start', 'end'])
