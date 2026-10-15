"""Lightweight kernel helpers for sc-drug-response (drug-target expression scoring)."""
from __future__ import annotations
__all__ = ['builtin_drug_targets', 'score_drug_targets', 'top_drugs']

def builtin_drug_targets() -> dict[str, list[str]]:
    """Return a copy of the built-in illustrative drug-associated target gene sets."""
    _BUILTIN_TARGETS = {'Imatinib': ['ABL1', 'KIT', 'PDGFRA', 'PDGFRB'], 'Dasatinib': ['ABL1', 'SRC', 'LCK', 'YES1', 'FYN', 'KIT'], 'Venetoclax': ['BCL2', 'BCL2L1', 'MCL1', 'BAX'], 'Osimertinib': ['EGFR', 'ERBB2', 'MET'], 'Gefitinib': ['EGFR', 'ERBB2', 'ERBB3'], 'Trametinib': ['MAP2K1', 'MAP2K2', 'KRAS', 'BRAF'], 'Dabrafenib': ['BRAF', 'RAF1', 'MAP2K1'], 'Olaparib': ['PARP1', 'PARP2', 'BRCA1', 'BRCA2'], 'Palbociclib': ['CDK4', 'CDK6', 'CCND1', 'RB1'], 'Ruxolitinib': ['JAK1', 'JAK2', 'STAT3', 'STAT5A']}
    return {k: list(v) for k, v in _BUILTIN_TARGETS.items()}

def score_drug_targets(adata, *, cluster_key: str | None=None, drug_targets: dict[str, list[str]] | None=None) -> pd.DataFrame:
    """Return mean_target_expression per (Cluster, Drug) across available target genes."""
    import pandas as pd
    from scipy import sparse
    targets = drug_targets if drug_targets is not None else builtin_drug_targets()
    if cluster_key is not None and cluster_key not in adata.obs.columns:
        raise ValueError(f"cluster_key '{cluster_key}' not found in adata.obs.")
    groups = adata.obs.groupby(cluster_key, observed=True).groups.items() if cluster_key else [('all', adata.obs.index)]
    var_set = set(adata.var_names)
    rows = []
    for grp, idx in groups:
        sub = adata[idx]
        for drug, genes in targets.items():
            matched = [g for g in genes if g in var_set]
            if not matched:
                continue
            mat = sub[:, matched].X
            val = float(mat.mean()) if not sparse.issparse(mat) else float(mat.mean())
            rows.append({'Cluster': str(grp), 'Drug': str(drug), 'mean_target_expression': round(val, 4), 'TargetGenes': len(matched), 'TotalTargets': len(genes), 'OverlapPct': round(100.0 * len(matched) / max(len(genes), 1), 1), 'MatchedGenes': ','.join(matched)})
    df = pd.DataFrame(rows)
    return df.sort_values(['Cluster', 'mean_target_expression'], ascending=[True, False], ignore_index=True) if len(df) else df

def top_drugs(table: pd.DataFrame, *, n_top: int=10) -> pd.DataFrame:
    """Rank drugs across clusters by decreasing mean score/expression."""
    score_col = 'Score' if 'Score' in table.columns else 'mean_target_expression'
    return table.groupby('Drug', as_index=False)[score_col].mean().sort_values(score_col, ascending=False, ignore_index=True).head(n_top)
