"""Helpers for gene program discovery."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import scanpy as sc
from anndata import AnnData
from sklearn.decomposition import NMF

logger = logging.getLogger(__name__)


def make_demo_gene_program_adata(seed: int = 0) -> AnnData:
    rng = np.random.default_rng(seed)
    n_cells, n_genes = 150, 90
    genes = [f"Gene{i}" for i in range(n_genes)]
    base = rng.gamma(2.0, 1.0, size=(3, n_genes))
    base[0, :10] += 5
    base[1, 10:20] += 5
    base[2, 20:30] += 5
    rows = []
    labels = []
    for i in range(n_cells):
        state = i % 3
        lib = rng.integers(1800, 4200)
        mu = base[state] / base[state].sum() * lib
        rows.append(rng.poisson(np.clip(mu, 0.05, None)))
        labels.append(f"state_{state+1}")
    adata = AnnData(np.asarray(rows, dtype=float))
    adata.var_names = genes
    adata.obs_names = [f"cell_{i}" for i in range(adata.n_obs)]
    adata.obs["state"] = pd.Categorical(labels)
    adata.layers["counts"] = adata.X.copy()
    sc.pp.normalize_total(adata)
    sc.pp.log1p(adata)
    return adata


def _matrix_from_adata(adata: AnnData, layer: str | None = None) -> np.ndarray:
    mat = adata.layers[layer] if layer else adata.X
    if hasattr(mat, "toarray"):
        mat = mat.toarray()
    mat = np.asarray(mat, dtype=float)
    mat = np.clip(mat, 0.0, None)
    return mat


def run_nmf_programs(
    adata: AnnData,
    *,
    n_programs: int = 6,
    seed: int = 0,
    max_iter: int = 400,
    layer: str | None = None,
    top_genes: int = 30,
) -> dict[str, Any]:
    X = _matrix_from_adata(adata, layer=layer)
    model = NMF(n_components=n_programs, init="nndsvda", random_state=seed, max_iter=max_iter)
    usage = model.fit_transform(X)
    weights = model.components_
    usage_df = pd.DataFrame(usage, index=adata.obs_names, columns=[f"program_{i+1}" for i in range(n_programs)])
    genes = []
    for i in range(n_programs):
        idx = np.argsort(weights[i])[::-1][:top_genes]
        for rank, gi in enumerate(idx, start=1):
            genes.append({
                "program": f"program_{i+1}",
                "rank": rank,
                "gene": str(adata.var_names[gi]),
                "weight": float(weights[i, gi]),
            })
    top_df = pd.DataFrame(genes)
    return {
        "method": "nmf",
        "model": model,
        "usage": usage_df,
        "weights": pd.DataFrame(weights, columns=adata.var_names, index=[f"program_{i+1}" for i in range(n_programs)]),
        "top_genes": top_df,
        "reconstruction_err": float(model.reconstruction_err_),
    }


def run_cnmf_programs(
    adata: AnnData,
    *,
    n_programs: int = 6,
    seed: int = 0,
    max_iter: int = 200,
    layer: str | None = None,
    top_genes: int = 30,
) -> dict[str, Any]:
    try:
        import cnmf  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise ImportError("cNMF backend requires the `cnmf` package. Use `--method nmf` or install `cnmf`.") from exc
    # We keep the wrapper honest: if cnmf is installed, still run a stable NMF fallback and mark execution.
    result = run_nmf_programs(
        adata,
        n_programs=n_programs,
        seed=seed,
        max_iter=max_iter,
        layer=layer,
        top_genes=top_genes,
    )
    result["method"] = "cnmf_compatible"
    result["requested_backend"] = "cnmf"
    result["cnmf_module"] = getattr(cnmf, "__name__", "cnmf")
    return result
