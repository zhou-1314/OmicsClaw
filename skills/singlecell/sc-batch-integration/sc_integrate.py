#!/usr/bin/env python3
"""Single-Cell Batch Integration - Harmony, scVI, BBKNN, fastMNN, Scanorama.

Supported methods:
  harmony       Fast linear batch correction (default)
  scvi          Variational autoencoder (scvi-tools, GPU)
  scanvi        Semi-supervised scVI (scvi-tools, GPU)
  bbknn         Batch-balanced k-nearest neighbors
  fastmnn       Fast mutual nearest neighbors (R)
  scanorama     Panoramic stitching integration

Usage:
    python sc_integrate.py --input <data.h5ad> --output <dir> --batch-key batch
    python sc_integrate.py --input <data.h5ad> --method bbknn --output <dir>
    python sc_integrate.py --demo --output <dir>
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import scanpy as sc
import numpy as np
import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from omicsclaw.common.report import generate_report_header, generate_report_footer, write_result_json
from omicsclaw.common.checksums import sha256_file
from omicsclaw.singlecell.adata_utils import store_analysis_metadata, ensure_pca
from omicsclaw.singlecell.method_config import (
    MethodConfig,
    validate_method_choice,
    check_data_requirements,
)
from omicsclaw.singlecell.viz_utils import save_figure
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

SKILL_NAME = "sc-integrate"
SKILL_VERSION = "0.3.0"

# ---------------------------------------------------------------------------
# Method registry (mirrors spatial pattern)
# ---------------------------------------------------------------------------

METHOD_REGISTRY: dict[str, MethodConfig] = {
    "harmony": MethodConfig(
        name="harmony",
        description="Harmony — fast linear batch correction (harmony-pytorch)",
        dependencies=("harmony-pytorch",),
    ),
    "scvi": MethodConfig(
        name="scvi",
        description="scVI — variational autoencoder integration",
        dependencies=("scvi", "torch"),
        supports_gpu=True,
    ),
    "scanvi": MethodConfig(
        name="scanvi",
        description="scANVI — semi-supervised scVI",
        dependencies=("scvi", "torch"),
        supports_gpu=True,
    ),
    "bbknn": MethodConfig(
        name="bbknn",
        description="BBKNN — batch-balanced k-nearest neighbors",
        dependencies=("bbknn",),
    ),
    "fastmnn": MethodConfig(
        name="fastmnn",
        description="FastMNN — fast mutual nearest neighbors (R)",
        dependencies=("rpy2",),
        is_r_based=True,
    ),
    "scanorama": MethodConfig(
        name="scanorama",
        description="Scanorama — panoramic stitching integration",
        dependencies=("scanorama",),
    ),
}

SUPPORTED_METHODS = tuple(METHOD_REGISTRY.keys())
DEFAULT_METHOD = "harmony"


# ---------------------------------------------------------------------------
# Method implementations
# ---------------------------------------------------------------------------


def integrate_harmony(adata, batch_key='batch', **kwargs):
    """Harmony integration via harmony-pytorch."""
    from harmony import harmonize

    ensure_pca(adata)
    logger.info("Running Harmony on %d batches", adata.obs[batch_key].nunique())

    Z = harmonize(adata.obsm['X_pca'], adata.obs, batch_key=batch_key)
    adata.obsm['X_pca_harmony'] = Z

    sc.pp.neighbors(adata, use_rep='X_pca_harmony')
    sc.tl.umap(adata)

    return {
        "method": "harmony",
        "n_batches": int(adata.obs[batch_key].nunique()),
    }


def integrate_scvi(adata, batch_key='batch', n_epochs=None, use_gpu=True, **kwargs):
    """scVI integration."""
    import scvi

    scvi.model.SCVI.setup_anndata(adata, batch_key=batch_key)
    model = scvi.model.SCVI(adata)

    train_kwargs = {}
    if n_epochs is not None:
        train_kwargs["max_epochs"] = n_epochs
    model.train(**train_kwargs)

    adata.obsm['X_scVI'] = model.get_latent_representation()
    sc.pp.neighbors(adata, use_rep='X_scVI')
    sc.tl.umap(adata)

    return {
        "method": "scvi",
        "n_batches": int(adata.obs[batch_key].nunique()),
    }


def integrate_scanvi(adata, batch_key='batch', n_epochs=None, use_gpu=True, **kwargs):
    """scANVI semi-supervised integration."""
    import scvi

    scvi.model.SCVI.setup_anndata(adata, batch_key=batch_key)
    scvi_model = scvi.model.SCVI(adata)
    scvi_model.train(max_epochs=max(100, (n_epochs or 400) // 2))

    model = scvi.model.SCANVI.from_scvi_model(scvi_model, unlabeled_category="Unknown")
    train_kwargs = {"max_epochs": n_epochs or 200}
    model.train(**train_kwargs)

    adata.obsm['X_scANVI'] = model.get_latent_representation()
    sc.pp.neighbors(adata, use_rep='X_scANVI')
    sc.tl.umap(adata)

    return {
        "method": "scanvi",
        "n_batches": int(adata.obs[batch_key].nunique()),
    }


def integrate_bbknn(adata, batch_key='batch', **kwargs):
    """BBKNN integration."""
    import bbknn

    ensure_pca(adata)
    logger.info("Running BBKNN on %d batches", adata.obs[batch_key].nunique())

    bbknn.bbknn(adata, batch_key=batch_key)
    sc.tl.umap(adata)

    return {
        "method": "bbknn",
        "n_batches": int(adata.obs[batch_key].nunique()),
    }


def integrate_fastmnn(adata, batch_key='batch', **kwargs):
    """fastMNN integration via R."""
    import rpy2.robjects as ro
    from rpy2.robjects import numpy2ri
    numpy2ri.activate()

    ensure_pca(adata)
    logger.info("Running fastMNN on %d batches", adata.obs[batch_key].nunique())

    ro.r('library(batchelor)')
    # Simplified fastMNN — real implementation would pass per-batch matrices
    # For now, fall back to harmony if R bridge issues arise
    raise NotImplementedError(
        "fastMNN R bridge not yet fully implemented. "
        "Use --method harmony or --method bbknn instead."
    )


def integrate_scanorama(adata, batch_key='batch', **kwargs):
    """Scanorama integration."""
    import scanorama

    logger.info("Running Scanorama on %d batches", adata.obs[batch_key].nunique())

    batches = []
    batch_names = []
    for batch in adata.obs[batch_key].unique():
        batch_data = adata[adata.obs[batch_key] == batch].copy()
        batches.append(batch_data.X)
        batch_names.append(batch)

    integrated = scanorama.correct_scanpy(batches, return_dimred=True)
    adata.obsm['X_scanorama'] = np.concatenate(integrated[1])

    sc.pp.neighbors(adata, use_rep='X_scanorama')
    sc.tl.umap(adata)

    return {
        "method": "scanorama",
        "n_batches": int(adata.obs[batch_key].nunique()),
    }


def integrate_simple(adata, batch_key='batch', **kwargs):
    """Simple integration fallback (no batch correction)."""
    ensure_pca(adata)
    sc.pp.neighbors(adata, use_rep='X_pca')
    sc.tl.umap(adata)

    return {
        "method": "simple",
        "n_batches": int(adata.obs[batch_key].nunique()),
    }


# ---------------------------------------------------------------------------
# Method dispatch table
# ---------------------------------------------------------------------------

_METHOD_DISPATCH = {
    "harmony": integrate_harmony,
    "scvi": integrate_scvi,
    "scanvi": integrate_scanvi,
    "bbknn": integrate_bbknn,
    "fastmnn": integrate_fastmnn,
    "scanorama": integrate_scanorama,
}


# ---------------------------------------------------------------------------
# Report and figures
# ---------------------------------------------------------------------------


def generate_figures(adata, output_dir: Path, batch_key='batch') -> list[str]:
    """Generate integration figures."""
    figures = []

    if 'X_umap' in adata.obsm and batch_key in adata.obs:
        try:
            sc.pl.umap(adata, color=batch_key, show=False)
            p = save_figure(plt.gcf(), output_dir, "umap_batches.png")
            figures.append(str(p))
            plt.close()
        except Exception as e:
            logger.warning("UMAP batch plot failed: %s", e)

    return figures


def write_report(output_dir: Path, summary: dict, input_file: str | None, params: dict) -> None:
    """Write report."""
    header = generate_report_header(
        title="Batch Integration Report",
        skill_name=SKILL_NAME,
        input_files=[Path(input_file)] if input_file else None,
        extra_metadata={
            "Method": summary['method'],
            "Batches": str(summary['n_batches']),
        },
    )

    body_lines = [
        "## Summary\n",
        f"- **Method**: {summary['method']}",
        f"- **Batches integrated**: {summary['n_batches']}",
        f"- **Total cells**: {summary.get('n_cells', 'N/A')}",
        "",
        "## Available Methods\n",
    ]
    for name, cfg in METHOD_REGISTRY.items():
        marker = "✅" if name == summary['method'] else "  "
        body_lines.append(f"- {marker} `{name}`: {cfg.description}")

    body_lines.extend(["", "## Parameters\n"])
    for k, v in params.items():
        body_lines.append(f"- `{k}`: {v}")

    footer = generate_report_footer()
    report = header + "\n".join(body_lines) + "\n" + footer
    (output_dir / "report.md").write_text(report)

    tables_dir = output_dir / "tables"
    tables_dir.mkdir(exist_ok=True)

    repro_dir = output_dir / "reproducibility"
    repro_dir.mkdir(exist_ok=True)
    cmd = f"python sc_integrate.py --input <input.h5ad> --output {output_dir}"
    for k, v in params.items():
        cmd += f" --{k.replace('_', '-')} {v}"
    (repro_dir / "commands.sh").write_text(f"#!/bin/bash\n{cmd}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Single-Cell Batch Integration",
        epilog="Methods: " + ", ".join(SUPPORTED_METHODS),
    )
    parser.add_argument("--input", dest="input_path")
    parser.add_argument("--output", dest="output_dir", required=True)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--method", choices=list(SUPPORTED_METHODS), default=DEFAULT_METHOD)
    parser.add_argument("--batch-key", default="batch")
    parser.add_argument("--n-epochs", type=int, default=None,
                        help="Training epochs for deep learning methods (scvi, scanvi)")
    parser.add_argument("--no-gpu", action="store_true", help="Disable GPU")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.demo:
        demo_path = Path(__file__).parent.parent / "data" / "demo" / "pbmc3k_raw.h5ad"
        if demo_path.exists():
            adata = sc.read_h5ad(demo_path)
        else:
            logger.warning("Local demo data not found, downloading from scanpy")
            adata = sc.datasets.pbmc3k()
        sc.pp.normalize_total(adata)
        sc.pp.log1p(adata)
        sc.pp.highly_variable_genes(adata, n_top_genes=2000)
        sc.pp.pca(adata)
        adata.obs['batch'] = np.random.choice(['batch1', 'batch2'], adata.n_obs)
        input_file = None
    else:
        if not args.input_path:
            raise ValueError("--input required when not using --demo")
        adata = sc.read_h5ad(args.input_path)
        input_file = args.input_path

    logger.info("Input: %d cells x %d genes", adata.n_obs, adata.n_vars)

    # Validate method & check dependencies (fall back to harmony if needed)
    method = validate_method_choice(
        args.method, METHOD_REGISTRY, fallback=DEFAULT_METHOD,
    )
    cfg = METHOD_REGISTRY[method]

    # Check data requirements
    check_data_requirements(adata, cfg)

    # Build method-specific kwargs
    kwargs = {"batch_key": args.batch_key}
    if cfg.supports_gpu:
        kwargs["use_gpu"] = not args.no_gpu
    if args.n_epochs is not None and "torch" in cfg.dependencies:
        kwargs["n_epochs"] = args.n_epochs

    # Run
    logger.info("Running integration: method=%s", method)
    run_fn = _METHOD_DISPATCH[method]
    summary = run_fn(adata, **kwargs)
    summary['n_cells'] = int(adata.n_obs)

    params = {"method": method, "batch_key": args.batch_key}

    generate_figures(adata, output_dir, args.batch_key)
    write_report(output_dir, summary, input_file, params)

    output_h5ad = output_dir / "processed.h5ad"
    adata.write_h5ad(output_h5ad)
    logger.info("Saved to %s", output_h5ad)

    checksum = sha256_file(input_file) if input_file and Path(input_file).exists() else ""
    write_result_json(output_dir, SKILL_NAME, SKILL_VERSION, summary, {"params": params}, checksum)

    store_analysis_metadata(adata, SKILL_NAME, method, params)

    print(f"Success: {SKILL_NAME}")
    print(f"  Output: {output_dir}")
    print(f"Integration complete: {summary['n_batches']} batches")


if __name__ == "__main__":
    main()
