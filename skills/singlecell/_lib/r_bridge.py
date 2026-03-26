"""Shared R bridge helpers for single-cell skills.

These helpers call standalone R scripts via subprocess for process isolation.
Data is exchanged via temporary h5ad/CSV files on the shared filesystem.

R crashes never bring down the Python process.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import sparse

from omicsclaw.core.dependency_manager import validate_r_environment
from omicsclaw.core.r_script_runner import RScriptRunner
from omicsclaw.core.r_utils import read_r_embedding_csv, read_r_result_csv

logger = logging.getLogger(__name__)

# R scripts directory
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "omicsclaw" / "r_scripts"


# ---------------------------------------------------------------------------
# Pure-Python helpers (unchanged from original)
# ---------------------------------------------------------------------------


def _align_matrix(matrix: np.ndarray, n_obs: int, n_vars: int) -> sparse.csr_matrix:
    arr = np.asarray(matrix)
    if arr.shape == (n_obs, n_vars):
        return sparse.csr_matrix(arr)
    if arr.shape == (n_vars, n_obs):
        return sparse.csr_matrix(arr.T)
    raise ValueError(
        f"Unexpected matrix shape {arr.shape}; expected {(n_obs, n_vars)} or {(n_vars, n_obs)}"
    )


def _subset_adata(adata, cells: list[str], genes: list[str]):
    cells = pd.Index([str(x) for x in cells])
    genes = pd.Index([str(x) for x in genes])
    common_cells = [c for c in cells if c in adata.obs_names]
    common_genes = [g for g in genes if g in adata.var_names]
    if not common_genes:
        reverse_gene_map = {str(g).replace("_", "-"): str(g) for g in adata.var_names}
        mapped = [reverse_gene_map[g] for g in genes if g in reverse_gene_map]
        common_genes = mapped
    if not common_cells:
        raise ValueError("R bridge returned no cells matching the AnnData input")
    if not common_genes:
        raise ValueError("R bridge returned no genes matching the AnnData input")
    return adata[common_cells, common_genes].copy()


def _sync_obs(adata, obs_df: pd.DataFrame) -> None:
    obs_df = obs_df.copy()
    obs_df.index = obs_df.index.astype(str)
    obs_df = obs_df.reindex(adata.obs_names)
    for col in obs_df.columns:
        adata.obs[col] = obs_df[col].values


def _sync_embedding(adata, key: str, values: np.ndarray, expected_rows: int) -> None:
    arr = np.asarray(values)
    if arr.ndim != 2:
        raise ValueError(f"Embedding '{key}' must be 2D, got {arr.shape}")
    if arr.shape[0] != expected_rows and arr.shape[1] == expected_rows:
        arr = arr.T
    if arr.shape[0] != expected_rows:
        raise ValueError(f"Embedding '{key}' rows {arr.shape[0]} do not match {expected_rows}")
    adata.obsm[key] = arr


# ---------------------------------------------------------------------------
# R-backed methods (subprocess-based)
# ---------------------------------------------------------------------------


def run_seurat_preprocessing(
    adata,
    *,
    workflow: str = "seurat",
    min_genes: int = 200,
    min_cells: int = 3,
    max_mt_pct: float = 20.0,
    n_top_hvg: int = 2000,
    n_pcs: int = 50,
    n_neighbors: int = 15,
    leiden_resolution: float = 1.0,
):
    """Run Seurat-based preprocessing and return an updated AnnData object."""
    validate_r_environment(["Seurat", "SingleCellExperiment", "zellkonverter"])
    runner = RScriptRunner(scripts_dir=_SCRIPTS_DIR)

    with tempfile.TemporaryDirectory(prefix="omicsclaw_seurat_") as tmpdir:
        tmpdir = Path(tmpdir)
        input_path = tmpdir / "input.h5ad"
        adata.write_h5ad(input_path)

        output_dir = tmpdir / "output"
        output_dir.mkdir()

        runner.run_script(
            "sc_seurat_preprocess.R",
            args=[
                str(input_path), str(output_dir), workflow,
                str(min_genes), str(min_cells), str(max_mt_pct),
                str(n_top_hvg), str(n_pcs), str(n_neighbors),
                str(leiden_resolution),
            ],
            expected_outputs=["obs.csv", "pca.csv", "umap.csv", "hvg.csv"],
            output_dir=output_dir,
        )

        # Read results
        obs_df = pd.read_csv(output_dir / "obs.csv", index_col=0)
        pca = read_r_embedding_csv(output_dir / "pca.csv")
        umap = read_r_embedding_csv(output_dir / "umap.csv")
        hvg_df = pd.read_csv(output_dir / "hvg.csv")
        hvg_list = hvg_df["gene"].tolist()

        # Read normalized expression if available
        norm_path = output_dir / "X_norm.csv"
        info_path = output_dir / "info.json"

        cells = list(obs_df.index.astype(str))
        genes = list(adata.var_names)  # Use original gene set for subsetting

        # If we have the norm matrix, read the gene names from it
        if norm_path.exists():
            norm_df = pd.read_csv(norm_path, index_col=0, nrows=0)
            genes = list(norm_df.columns)

        updated = _subset_adata(adata, cells, genes)
        updated.layers["counts"] = updated.X.copy()

        if norm_path.exists():
            norm_df = pd.read_csv(norm_path, index_col=0)
            norm_arr = norm_df.values.T  # R writes genes x cells, we need cells x genes
            updated.X = _align_matrix(norm_arr, updated.n_obs, updated.n_vars)

        _sync_obs(updated, obs_df)
        if "nFeature_RNA" in updated.obs and "n_genes_by_counts" not in updated.obs:
            updated.obs["n_genes_by_counts"] = updated.obs["nFeature_RNA"].values
        if "nCount_RNA" in updated.obs and "total_counts" not in updated.obs:
            updated.obs["total_counts"] = updated.obs["nCount_RNA"].values
        if "percent.mt" in updated.obs and "pct_counts_mt" not in updated.obs:
            updated.obs["pct_counts_mt"] = updated.obs["percent.mt"].values

        updated.var["highly_variable"] = updated.var_names.isin(hvg_list)

        _sync_embedding(updated, "X_pca", pca, updated.n_obs)
        _sync_embedding(updated, "X_umap", umap, updated.n_obs)

        updated.obs["leiden"] = updated.obs["seurat_clusters"].astype(str)

        assay_name = workflow
        if info_path.exists():
            with open(info_path) as f:
                info = json.load(f)
                assay_name = info.get("default_assay", workflow)

        updated.uns["preprocessing"] = {"method": workflow, "default_assay": assay_name}
        return updated


def run_singler_annotation(adata, *, reference: str = "HPCA") -> pd.DataFrame:
    """Run SingleR annotation on an AnnData object."""
    validate_r_environment(["SingleR", "celldex", "SingleCellExperiment", "zellkonverter"])
    runner = RScriptRunner(scripts_dir=_SCRIPTS_DIR)

    with tempfile.TemporaryDirectory(prefix="omicsclaw_singler_") as tmpdir:
        tmpdir = Path(tmpdir)
        input_path = tmpdir / "input.h5ad"
        adata.write_h5ad(input_path)

        output_dir = tmpdir / "output"
        output_dir.mkdir()

        runner.run_script(
            "sc_singler_annotate.R",
            args=[str(input_path), str(output_dir), reference],
            expected_outputs=["singler_results.csv"],
            output_dir=output_dir,
        )
        return read_r_result_csv(output_dir / "singler_results.csv")


def run_doubletfinder(adata, *, expected_doublet_rate: float = 0.06) -> pd.DataFrame:
    """Run DoubletFinder on AnnData-derived Seurat object."""
    validate_r_environment(["Seurat", "DoubletFinder", "SingleCellExperiment", "zellkonverter"])
    runner = RScriptRunner(scripts_dir=_SCRIPTS_DIR)

    with tempfile.TemporaryDirectory(prefix="omicsclaw_df_") as tmpdir:
        tmpdir = Path(tmpdir)
        input_path = tmpdir / "input.h5ad"
        adata.write_h5ad(input_path)

        output_dir = tmpdir / "output"
        output_dir.mkdir()

        runner.run_script(
            "sc_doubletfinder.R",
            args=[str(input_path), str(output_dir), str(expected_doublet_rate)],
            expected_outputs=["doubletfinder_results.csv"],
            output_dir=output_dir,
        )
        return read_r_result_csv(output_dir / "doubletfinder_results.csv")


def run_scdblfinder(adata, *, expected_doublet_rate: float = 0.06) -> pd.DataFrame:
    """Run scDblFinder on AnnData-derived SingleCellExperiment."""
    validate_r_environment(["scDblFinder", "SingleCellExperiment", "zellkonverter"])
    runner = RScriptRunner(scripts_dir=_SCRIPTS_DIR)

    with tempfile.TemporaryDirectory(prefix="omicsclaw_scdbf_") as tmpdir:
        tmpdir = Path(tmpdir)
        input_path = tmpdir / "input.h5ad"
        adata.write_h5ad(input_path)

        output_dir = tmpdir / "output"
        output_dir.mkdir()

        runner.run_script(
            "sc_scdblfinder.R",
            args=[str(input_path), str(output_dir), str(expected_doublet_rate)],
            expected_outputs=["scdblfinder_results.csv"],
            output_dir=output_dir,
        )
        return read_r_result_csv(output_dir / "scdblfinder_results.csv")


def run_seurat_integration(
    adata,
    *,
    method: str,
    batch_key: str,
    n_features: int = 2000,
    n_pcs: int = 30,
):
    """Run an R-backed integration workflow and return embeddings."""
    if method not in {"seurat_cca", "seurat_rpca", "fastmnn"}:
        raise ValueError(f"Unsupported R integration method: {method}")

    if method == "fastmnn":
        required = ["batchelor", "SingleCellExperiment", "zellkonverter"]
    else:
        required = ["Seurat", "SingleCellExperiment", "zellkonverter"]
    validate_r_environment(required)

    runner = RScriptRunner(scripts_dir=_SCRIPTS_DIR)

    with tempfile.TemporaryDirectory(prefix="omicsclaw_integrate_") as tmpdir:
        tmpdir = Path(tmpdir)
        input_path = tmpdir / "input.h5ad"
        adata.write_h5ad(input_path)

        output_dir = tmpdir / "output"
        output_dir.mkdir()

        runner.run_script(
            "sc_seurat_integrate.R",
            args=[
                str(input_path), str(output_dir), method, batch_key,
                str(n_features), str(n_pcs),
            ],
            expected_outputs=["embedding.csv", "obs.csv"],
            output_dir=output_dir,
        )

        embedding = read_r_embedding_csv(output_dir / "embedding.csv")
        obs_df = pd.read_csv(output_dir / "obs.csv", index_col=0)

        cells = list(obs_df.index.astype(str))
        updated = _subset_adata(adata, cells, list(adata.var_names))
        _sync_embedding(updated, f"X_{method}", embedding, updated.n_obs)

        if not obs_df.empty:
            _sync_obs(updated, obs_df)

        umap_path = output_dir / "umap.csv"
        if umap_path.exists():
            umap = read_r_embedding_csv(umap_path)
            _sync_embedding(updated, "X_umap", umap, updated.n_obs)

        return updated


def run_soupx(
    *,
    raw_matrix_dir: str,
    filtered_matrix_dir: str,
) -> tuple[sparse.csr_matrix, list[str], list[str], float]:
    """Run SoupX on 10x raw/filtered matrices and return corrected counts."""
    validate_r_environment(["Seurat", "SoupX"])
    runner = RScriptRunner(scripts_dir=_SCRIPTS_DIR)

    with tempfile.TemporaryDirectory(prefix="omicsclaw_soupx_") as tmpdir:
        tmpdir = Path(tmpdir)
        output_dir = tmpdir / "output"
        output_dir.mkdir()

        runner.run_script(
            "sc_soupx.R",
            args=[str(raw_matrix_dir), str(filtered_matrix_dir), str(output_dir)],
            expected_outputs=["corrected_counts.csv", "contamination.json"],
            output_dir=output_dir,
        )

        counts_df = pd.read_csv(output_dir / "corrected_counts.csv", index_col=0)
        genes = list(counts_df.index.astype(str))
        cells = list(counts_df.columns.astype(str))
        matrix = _align_matrix(counts_df.values.T, len(cells), len(genes))

        with open(output_dir / "contamination.json") as f:
            contamination = json.load(f)["contamination"]

        return matrix, cells, genes, float(contamination)


def run_pseudobulk_deseq2(
    adata,
    *,
    condition_key: str,
    case_label: str,
    reference_label: str,
    sample_key: str,
    celltype_key: str,
) -> pd.DataFrame:
    """Run DESeq2 pseudobulk differential expression in R."""
    validate_r_environment(["DESeq2", "muscat", "SingleCellExperiment", "zellkonverter"])
    runner = RScriptRunner(scripts_dir=_SCRIPTS_DIR)

    with tempfile.TemporaryDirectory(prefix="omicsclaw_deseq2_") as tmpdir:
        tmpdir = Path(tmpdir)
        input_path = tmpdir / "input.h5ad"
        adata.write_h5ad(input_path)

        output_dir = tmpdir / "output"
        output_dir.mkdir()

        runner.run_script(
            "sc_pseudobulk_deseq2.R",
            args=[
                str(input_path), str(output_dir),
                condition_key, case_label, reference_label,
                sample_key, celltype_key,
            ],
            expected_outputs=["deseq2_results.csv"],
            output_dir=output_dir,
        )
        return read_r_result_csv(output_dir / "deseq2_results.csv", index_col=None)


def run_cellchat(
    adata,
    *,
    cell_type_key: str = "cell_type",
    species: str = "human",
) -> pd.DataFrame:
    """Run CellChat and return the inferred ligand-receptor table."""
    validate_r_environment(["CellChat", "SingleCellExperiment", "zellkonverter"])

    if getattr(adata, "raw", None) is not None:
        adata_r = adata.raw.to_adata()
        adata_r.obs = adata.obs.copy()
    else:
        adata_r = adata

    runner = RScriptRunner(scripts_dir=_SCRIPTS_DIR)

    with tempfile.TemporaryDirectory(prefix="omicsclaw_cellchat_") as tmpdir:
        tmpdir = Path(tmpdir)
        input_path = tmpdir / "input.h5ad"
        adata_r.write_h5ad(input_path)

        output_dir = tmpdir / "output"
        output_dir.mkdir()

        runner.run_script(
            "sc_cellchat.R",
            args=[str(input_path), str(output_dir), cell_type_key, species],
            expected_outputs=["cellchat_results.csv"],
            output_dir=output_dir,
        )
        return read_r_result_csv(output_dir / "cellchat_results.csv", index_col=None)
