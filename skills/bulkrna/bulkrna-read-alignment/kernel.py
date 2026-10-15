"""Lightweight kernel helpers for bulkrna-read-alignment (STAR/HISAT2/Salmon log parser)."""
from __future__ import annotations
__all__ = ['parse_alignment_log', 'evaluate_mapping_qc']

def parse_alignment_log(text: str, *, method: str='star') -> pd.DataFrame:
    """Parse STAR Log.final.out, HISAT2 summary, or Salmon meta_info.json into a standard row."""
    import json, re, pandas as pd
    m = method.lower().strip()
    if m == 'star':

        def _grab(pat: str) -> int:
            hit = re.search(pat, text)
            if not hit:
                raise ValueError(f'Missing STAR log field matching: {pat}')
            return int(hit.group(1))
        total = _grab('Number of input reads\\s*\\|\\s*(\\d+)')
        uniq = _grab('Uniquely mapped reads number\\s*\\|\\s*(\\d+)')
        multi = _grab('Number of reads mapped to multiple loci\\s*\\|\\s*(\\d+)')
        many = re.search('Number of reads mapped to too many loci\\s*\\|\\s*(\\d+)', text)
        multi += int(many.group(1)) if many else 0
        unmapped = max(total - uniq - multi, 0)
    elif m == 'hisat2':
        tot_m = re.search('(\\d+)\\s+reads;\\s+of these:', text)
        uniq_m = re.search('(\\d+)\\s+\\([\\d.]+%\\)\\s+aligned concordantly exactly 1 time', text)
        mult_m = re.search('(\\d+)\\s+\\([\\d.]+%\\)\\s+aligned concordantly >1 times', text)
        if not (tot_m and uniq_m and mult_m):
            raise ValueError('Incomplete HISAT2 paired-end summary log.')
        total, uniq, multi = (int(tot_m.group(1)), int(uniq_m.group(1)), int(mult_m.group(1)))
        unmapped = max(total - uniq - multi, 0)
    elif m == 'salmon':
        meta = json.loads(text)
        total, uniq, multi = (int(meta['num_processed']), int(meta['num_mapped']), 0)
        unmapped = max(total - uniq, 0)
    else:
        raise ValueError(f'Unsupported alignment log method: {method}')
    if total <= 0:
        raise ValueError('Total reads must be positive.')
    return pd.DataFrame([{'aligner': m, 'total_reads': total, 'uniquely_mapped': uniq, 'multi_mapped': multi, 'unmapped': unmapped, 'unique_rate': round(100.0 * uniq / total, 2), 'mapped_rate': round(100.0 * (uniq + multi) / total, 2)}])

def evaluate_mapping_qc(stats_df: pd.DataFrame, *, min_mapped_rate: float=70.0) -> dict[str, object]:
    """Return QC status flag and summary metrics from a parsed alignment table."""
    row = stats_df.iloc[0]
    return {'aligner': str(row['aligner']), 'mapped_rate': float(row['mapped_rate']), 'unique_rate': float(row['unique_rate']), 'qc_status': 'PASS' if float(row['mapped_rate']) >= min_mapped_rate else 'WARN'}
