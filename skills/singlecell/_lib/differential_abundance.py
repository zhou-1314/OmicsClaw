"""Helpers for single-cell differential abundance analysis."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import scanpy as sc
from anndata import AnnData

logger = logging.getLogger(__name__)


def make_demo_da_adata(seed: int = 0) -> AnnData:
    """Create a tiny synthetic dataset with sample/condition/cell-type metadata."""
    rng = np.random.default_rng(seed)
    n_cells = 180
    n_genes = 120
    genes = [f"Gene{i}" for i in range(n_genes)]
    cell_types = np.array(["T", "B", "Mono"])
    samples = []
    conditions = []
    labels = []
    counts = []
    sample_defs = [
        ("ctrl_1", "control", {"T": 0.45, "B": 0.35, "Mono": 0.20}),
        ("ctrl_2", "control", {"T": 0.42, "B": 0.38, "Mono": 0.20}),
        ("stim_1", "stim", {"T": 0.25, "B": 0.25, "Mono": 0.50}),
        ("stim_2", "stim", {"T": 0.28, "B": 0.24, "Mono": 0.48}),
    ]
    per_sample = n_cells // len(sample_defs)
    base_profiles = {
        "T": rng.gamma(2.5, 1.2, size=n_genes),
        "B": rng.gamma(2.0, 1.0, size=n_genes),
        "Mono": rng.gamma(3.0, 1.1, size=n_genes),
    }
    # make a few marker-like genes more distinct
    base_profiles["T"][:10] += 4
    base_profiles["B"][10:20] += 4
    base_profiles["Mono"][20:30] += 4

    for sample, condition, probs in sample_defs:
        chosen = rng.choice(cell_types, size=per_sample, p=[probs[k] for k in cell_types])
        for ct in chosen:
            lib = rng.integers(1800, 4200)
            mu = base_profiles[ct] / base_profiles[ct].sum() * lib
            counts.append(rng.poisson(np.clip(mu, 0.05, None)))
            samples.append(sample)
            conditions.append(condition)
            labels.append(ct)

    adata = AnnData(np.asarray(counts, dtype=float))
    adata.var_names = genes
    adata.obs_names = [f"cell_{i}" for i in range(adata.n_obs)]
    adata.obs["sample"] = pd.Categorical(samples)
    adata.obs["condition"] = pd.Categorical(conditions)
    adata.obs["cell_type"] = pd.Categorical(labels)
    adata.layers["counts"] = adata.X.copy()
    sc.pp.normalize_total(adata)
    sc.pp.log1p(adata)
    sc.pp.pca(adata, n_comps=20)
    sc.pp.neighbors(adata, n_neighbors=12, n_pcs=20)
    sc.tl.umap(adata)
    return adata


def _normalize_counts_table(df: pd.DataFrame) -> pd.DataFrame:
    return df.div(df.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)


def build_sample_celltype_table(
    adata: AnnData,
    *,
    sample_key: str,
    celltype_key: str,
) -> pd.DataFrame:
    obs = adata.obs[[sample_key, celltype_key]].copy()
    table = pd.crosstab(obs[sample_key], obs[celltype_key]).sort_index()
    table.index.name = sample_key
    return table


def build_composition_summary(
    adata: AnnData,
    *,
    sample_key: str,
    condition_key: str,
    celltype_key: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    counts = build_sample_celltype_table(adata, sample_key=sample_key, celltype_key=celltype_key)
    props = _normalize_counts_table(counts)
    meta = (
        adata.obs[[sample_key, condition_key]]
        .drop_duplicates()
        .set_index(sample_key)
        .reindex(counts.index)
    )
    grouped = props.join(meta)
    mean_props = grouped.groupby(condition_key, observed=False).mean(numeric_only=True)
    return counts, props, mean_props


def run_simple_da(
    adata: AnnData,
    *,
    sample_key: str,
    condition_key: str,
    celltype_key: str,
    contrast: str | None = None,
    fdr: float = 0.05,
) -> pd.DataFrame:
    """Exploratory sample-aware DA summary using per-sample cell-type proportions."""
    from scipy.stats import mannwhitneyu
    from statsmodels.stats.multitest import multipletests

    counts, props, _ = build_composition_summary(
        adata,
        sample_key=sample_key,
        condition_key=condition_key,
        celltype_key=celltype_key,
    )
    sample_meta = (
        adata.obs[[sample_key, condition_key]]
        .drop_duplicates()
        .set_index(sample_key)
        .reindex(props.index)
    )
    groups = list(sample_meta[condition_key].astype(str).unique())
    if contrast:
        try:
            group_a, group_b = [x.strip() for x in contrast.split("vs")]
        except ValueError as exc:
            raise ValueError("contrast must look like 'group1 vs group2'") from exc
    elif len(groups) == 2:
        group_a, group_b = groups
    else:
        raise ValueError("Provide --contrast when more than 2 condition levels are present")

    res = []
    for ct in props.columns:
        a = props.loc[sample_meta[condition_key].astype(str) == group_a, ct]
        b = props.loc[sample_meta[condition_key].astype(str) == group_b, ct]
        if len(a) == 0 or len(b) == 0:
            continue
        stat, pval = mannwhitneyu(a, b, alternative="two-sided")
        log2fc = float(np.log2((b.mean() + 1e-6) / (a.mean() + 1e-6)))
        res.append(
            {
                "cell_type": ct,
                "group_a": group_a,
                "group_b": group_b,
                "mean_prop_group_a": float(a.mean()),
                "mean_prop_group_b": float(b.mean()),
                "log2fc_group_b_over_a": log2fc,
                "u_statistic": float(stat),
                "pvalue": float(pval),
            }
        )
    df = pd.DataFrame(res)
    if df.empty:
        return df
    df["padj"] = multipletests(df["pvalue"], method="fdr_bh")[1]
    df["significant"] = df["padj"] <= fdr
    return df.sort_values(["padj", "pvalue", "cell_type"]).reset_index(drop=True)


def run_milo_da(
    adata: AnnData,
    *,
    sample_key: str,
    condition_key: str,
    celltype_key: str,
    prop: float = 0.1,
    n_neighbors: int = 30,
    contrast: str | None = None,
) -> tuple[Any, pd.DataFrame]:
    """Run Milo differential abundance via the local Milo wrapper."""
    if sample_key not in adata.obs:
        raise ValueError(f"Missing sample key: {sample_key}")
    if condition_key not in adata.obs:
        raise ValueError(f"Missing condition key: {condition_key}")
    if celltype_key not in adata.obs:
        raise ValueError(f"Missing cell type key: {celltype_key}")
    if "neighbors" not in adata.uns:
        sc.pp.neighbors(adata, n_neighbors=n_neighbors)

    try:
        from skills.singlecell._lib._milo_dev import Milo
    except Exception as exc:  # pragma: no cover - exercised via smoke tests instead
        logger.warning("Falling back to internal Milo-like neighborhood DA because the Milo wrapper is unavailable: %s", exc)
        return _run_internal_milo_like_da(
            adata,
            sample_key=sample_key,
            condition_key=condition_key,
            celltype_key=celltype_key,
            prop=prop,
            n_neighbors=n_neighbors,
            contrast=contrast,
        )

    milo = Milo()
    mdata = milo.load(adata)
    milo.make_nhoods(mdata["rna"], prop=prop)
    mdata = milo.count_nhoods(mdata, sample_col=sample_key)
    milo.add_covariate_to_nhoods_var(mdata, [condition_key], feature_key="rna")
    if contrast:
        parts = [x.strip() for x in contrast.split("vs")]
        model_contrasts = f"{condition_key}[{parts[1]}] - {condition_key}[{parts[0]}]"
    else:
        model_contrasts = None
    milo.da_nhoods(mdata, design=f"~ {condition_key}", model_contrasts=model_contrasts)
    milo.annotate_nhoods(mdata, anno_col=celltype_key)
    milo.build_nhood_graph(mdata)
    nhood = mdata["milo"].var.copy()
    nhood.index.name = "nhood"
    if hasattr(mdata["milo"], "uns"):
        mdata["milo"].uns["backend"] = "milo"
    return mdata, nhood.reset_index()


def run_sccoda_da(
    adata: AnnData,
    *,
    sample_key: str,
    condition_key: str,
    celltype_key: str,
    reference_cell_type: str = "automatic",
    fdr: float = 0.05,
) -> tuple[Any, pd.DataFrame]:
    """Run scCODA through pertpy when available."""
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")

    try:
        import pertpy as pt
    except ImportError:
        return _run_sccoda_da_direct(
            adata,
            sample_key=sample_key,
            condition_key=condition_key,
            celltype_key=celltype_key,
            reference_cell_type=reference_cell_type,
        )

    model = pt.tl.Sccoda()
    mdata = model.load(
        adata,
        type="cell_level",
        generate_sample_level=True,
        cell_type_identifier=celltype_key,
        sample_identifier=sample_key,
        covariate_obs=[condition_key],
    )
    modality_key = "coda"
    model.prepare(mdata, modality_key=modality_key, formula=condition_key, reference_cell_type=reference_cell_type)
    model.run_nuts(mdata, modality_key=modality_key)
    model.set_fdr(mdata, modality_key=modality_key, est_fdr=fdr)
    effect_df = model.get_effect_df(mdata, modality_key=modality_key).reset_index()
    if hasattr(mdata, "uns"):
        mdata.uns["backend"] = "sccoda_pertpy"
    return mdata, effect_df


def _resolve_condition_groups(sample_meta: pd.DataFrame, condition_key: str, contrast: str | None = None) -> tuple[str, str]:
    groups = list(sample_meta[condition_key].astype(str).unique())
    if contrast:
        try:
            group_a, group_b = [x.strip() for x in contrast.split("vs")]
        except ValueError as exc:
            raise ValueError("contrast must look like 'group1 vs group2'") from exc
        return group_a, group_b
    if len(groups) == 2:
        return groups[0], groups[1]
    raise ValueError("Provide --contrast when more than 2 condition levels are present")


def _run_internal_milo_like_da(
    adata: AnnData,
    *,
    sample_key: str,
    condition_key: str,
    celltype_key: str,
    prop: float,
    n_neighbors: int,
    contrast: str | None,
) -> tuple[dict[str, Any], pd.DataFrame]:
    from scipy.stats import mannwhitneyu
    from statsmodels.stats.multitest import multipletests

    sample_meta = (
        adata.obs[[sample_key, condition_key]]
        .drop_duplicates()
        .assign(**{sample_key: lambda df: df[sample_key].astype(str), condition_key: lambda df: df[condition_key].astype(str)})
        .set_index(sample_key)
        .sort_index()
    )
    group_a, group_b = _resolve_condition_groups(sample_meta, condition_key, contrast)
    sample_totals = adata.obs[sample_key].astype(str).value_counts().reindex(sample_meta.index).fillna(0.0)

    graph = adata.obsp["distances"] if "distances" in adata.obsp else adata.obsp["connectivities"]
    graph = graph.tocsr()
    target_size = max(5, min(n_neighbors, adata.n_obs))
    seed_count = max(8, min(adata.n_obs, int(np.ceil(adata.n_obs * prop))))
    seed_indices = np.linspace(0, adata.n_obs - 1, num=seed_count, dtype=int)

    rows: list[dict[str, Any]] = []
    for hood_idx, seed in enumerate(np.unique(seed_indices)):
        row = graph.getrow(int(seed))
        neighbors = row.indices
        if neighbors.size:
            weights = row.data
            if "distances" in adata.obsp:
                order = np.argsort(weights)
            else:
                order = np.argsort(weights)[::-1]
            neighbors = neighbors[order][: max(0, target_size - 1)]
        cells = np.unique(np.concatenate(([int(seed)], neighbors)))
        hood_obs = adata.obs.iloc[cells]
        hood_counts = hood_obs[sample_key].astype(str).value_counts().reindex(sample_meta.index).fillna(0.0)
        hood_fracs = hood_counts.div(sample_totals.replace(0, np.nan)).fillna(0.0)
        frac_a = hood_fracs[sample_meta[condition_key] == group_a]
        frac_b = hood_fracs[sample_meta[condition_key] == group_b]
        stat, pvalue = mannwhitneyu(frac_a, frac_b, alternative="two-sided")
        annotation = hood_obs[celltype_key].astype(str).mode().iloc[0]
        rows.append(
            {
                "nhood": f"nhood_{hood_idx:03d}",
                "seed_cell": str(adata.obs_names[int(seed)]),
                "nhood_size": int(len(cells)),
                "nhood_annotation": annotation,
                "mean_frac_group_a": float(frac_a.mean()),
                "mean_frac_group_b": float(frac_b.mean()),
                "logFC": float(np.log2((frac_b.mean() + 1e-6) / (frac_a.mean() + 1e-6))),
                "U_statistic": float(stat),
                "pvalue": float(pvalue),
            }
        )

    nhood = pd.DataFrame(rows)
    nhood["SpatialFDR"] = multipletests(nhood["pvalue"], method="fdr_bh")[1]
    nhood["significant"] = nhood["SpatialFDR"] <= 0.05
    nhood = nhood.sort_values(["SpatialFDR", "pvalue", "nhood"]).reset_index(drop=True)
    return {"backend": "milo_like"}, nhood


def _run_sccoda_da_direct(
    adata: AnnData,
    *,
    sample_key: str,
    condition_key: str,
    celltype_key: str,
    reference_cell_type: str,
) -> tuple[AnnData, pd.DataFrame]:
    from sccoda.util.comp_ana import CompositionalAnalysis

    counts = build_sample_celltype_table(adata, sample_key=sample_key, celltype_key=celltype_key)
    meta = (
        adata.obs[[sample_key, condition_key]]
        .drop_duplicates()
        .assign(**{sample_key: lambda df: df[sample_key].astype(str), condition_key: lambda df: df[condition_key].astype(str)})
        .set_index(sample_key)
        .reindex(counts.index.astype(str))
    )
    mdata = AnnData(counts.to_numpy(dtype=float))
    mdata.obs = meta.copy()
    mdata.obs_names = meta.index.astype(str)
    mdata.var_names = counts.columns.astype(str)
    model = CompositionalAnalysis(mdata, formula=condition_key, reference_cell_type=reference_cell_type)
    result = model.sample_hmc(num_results=200, num_burnin=100)
    effect_df = result.effect_df.reset_index()
    mdata.uns["backend"] = "sccoda_direct"
    sampling_stats = getattr(result, "sampling_stats", None)
    if isinstance(sampling_stats, dict) and "acc_rate" in sampling_stats:
        mdata.uns["sccoda_acceptance_rate"] = float(sampling_stats["acc_rate"])
    return mdata, effect_df


def save_heatmap(df: pd.DataFrame, output_path: str | Path, title: str) -> Path | None:
    import matplotlib.pyplot as plt
    import seaborn as sns

    if df.empty:
        return None
    output_path = Path(output_path)
    fig, ax = plt.subplots(figsize=(max(6, 0.6 * df.shape[1]), max(4, 0.4 * df.shape[0])))
    sns.heatmap(df, cmap="viridis", ax=ax)
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(output_path, dpi=200)
    plt.close(fig)
    return output_path
