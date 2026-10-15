"""Lightweight kernel helpers for sc-perturb-prep (barcode-to-sgRNA mapping & attachment)."""
from __future__ import annotations
__all__ = ['standardize_mapping', 'collapse_assignments', 'attach_assignments']

def standardize_mapping(table: pd.DataFrame, *, barcode_column: str | None=None, sgrna_column: str | None=None, target_column: str | None=None) -> pd.DataFrame:
    """Standardize a barcode-to-sgRNA DataFrame into (barcode, sgRNA, target_gene) columns."""
    import pandas as pd
    cols = {c.lower(): c for c in table.columns}
    b_col = barcode_column or cols.get('barcode') or cols.get('cell_barcode') or cols.get('cell') or table.columns[0]
    s_col = sgrna_column or cols.get('sgrna') or cols.get('guide') or cols.get('feature_call') or table.columns[1]
    t_col = target_column or cols.get('target_gene') or cols.get('target') or cols.get('gene')
    out = pd.DataFrame({'barcode': table[b_col].astype(str).str.strip(), 'sgRNA': table[s_col].astype(str).str.strip()})
    out['target_gene'] = table[t_col].astype(str).str.strip() if t_col and t_col in table.columns else ''
    out = out[(out['barcode'] != '') & (out['sgRNA'] != '') & ~out['barcode'].isin(['nan', 'None']) & ~out['sgRNA'].isin(['nan', 'None'])]
    return out.drop_duplicates(subset=['barcode', 'sgRNA'], ignore_index=True)

def collapse_assignments(mapping: pd.DataFrame, *, delimiter: str='_', gene_position: int=0, control_patterns: tuple[str, ...]=('NT', 'NTC', 'NON-TARGET', 'NON_TARGET', 'NEGATIVE_CONTROL', 'NEG_CTRL'), control_label: str='NT', drop_multi_guide: bool=True) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Collapse standardized mapping to one row per barcode; return (assigned_df, dropped_df)."""
    import re, pandas as pd
    ctrl_set = {p.upper() for p in control_patterns}
    assigned_rows, dropped_rows = ([], [])
    for bc, grp in mapping.groupby('barcode', sort=False):
        guides = grp['sgRNA'].tolist()
        if len(guides) > 1 and drop_multi_guide:
            dropped_rows.append({'barcode': bc, 'sgRNA': ';'.join(guides), 'n_guides': len(guides)})
            continue
        guide_str = ';'.join(guides)
        tgt_raw = str(grp['target_gene'].iloc[0]) if grp['target_gene'].iloc[0] else guides[0].split(delimiter)[gene_position]
        tokens = [t.upper() for t in re.split('[^A-Za-z0-9]+', f'{guides[0]}_{tgt_raw}') if t]
        is_ctrl = any((t in ctrl_set for t in tokens))
        tgt = control_label if is_ctrl else tgt_raw if len(guides) == 1 else 'multi_guide'
        status = 'control' if is_ctrl else 'assigned' if len(guides) == 1 else 'multi_guide'
        assigned_rows.append({'barcode': bc, 'sgRNA': guide_str, 'target_gene': tgt, 'perturbation': tgt, 'assignment_status': status})
    return (pd.DataFrame(assigned_rows), pd.DataFrame(dropped_rows))

def attach_assignments(adata, assignments: pd.DataFrame, *, pert_key: str='perturbation', sgrna_key: str='sgRNA', target_key: str='target_gene'):
    """Filter to Gene Expression features and attach collapsed guide assignments onto matching cells."""
    ad = adata[:, adata.var['feature_types'] == 'Gene Expression'].copy() if 'feature_types' in adata.var.columns else adata.copy()
    indexed = assignments.set_index('barcode') if 'barcode' in assignments.columns else assignments.copy()
    common = [bc for bc in ad.obs_names if bc in indexed.index]
    if not common:
        raise ValueError('No overlapping cell barcodes between adata.obs_names and assignments.')
    ad = ad[common].copy()
    sub = indexed.loc[ad.obs_names]
    ad.obs[sgrna_key] = sub['sgRNA'].to_numpy()
    ad.obs[target_key] = sub['target_gene'].to_numpy()
    ad.obs[pert_key] = sub['perturbation'].to_numpy()
    ad.obs['assignment_status'] = sub['assignment_status'].to_numpy()
    return ad
