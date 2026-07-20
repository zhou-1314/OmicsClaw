"""Verify pyproject's pip layer is thin: heavy hubs must live in environment.yml."""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]

# Packages that MUST be installed via conda/mamba, NOT listed in pyproject.
# Pip-only packages (no conda recipe) stay in pyproject: SpaGCN, GraphST,
# cellcharter, paste-bio, flashdeconv, fastccc, pyVIA, pybanksy, ccproxy-api,
# tangram-sc, deepagents, opendataloader-pdf, torch_geometric, phate,
# python-multipart, langchain*, langgraph*, tavily-python, markdownify, pypdf,
# textual. Pydantic is the deliberate exception among conda-available packages:
# ADR 0037 requires it in pip core so a lightweight install can parse skill.yaml.
# Post-merge follow-up audit (2026-05-02) confirmed cell2location, cellphonedb,
# infercnvpy, and SpatialDE return HTTP 404 on both bioconda and conda-forge —
# they are genuinely pip-only and stay in pyproject.
CONDA_OWNED = {
    "scanpy",
    "anndata",
    "squidpy",
    "numpy",
    "pandas",
    "scipy",
    "scikit-learn",
    "matplotlib",
    "seaborn",
    "pillow",
    "scikit-misc",
    "igraph",
    "python-igraph",
    "leidenalg",
    "louvain",
    "umap-learn",
    "nbformat",
    "jupyter-client",
    "ipykernel",
    "rich",
    "greenlet",
    "prompt-toolkit",
    "questionary",
    "pyyaml",
    "aiosqlite",
    "sqlalchemy",
    "fastapi",
    "uvicorn",
    "cryptography",
    "requests",
    "openai",
    "python-dotenv",
    "httpx",
    "torch",
    "pytorch",
    "pytorch-cpu",
    "jinja2",
    "nbconvert",
    "beautifulsoup4",
    "scvi-tools",
    "scvelo",
    "cellrank",
    "harmonypy",
    "bbknn",
    "scanorama",
    "celltypist",
    "liana",
    "gseapy",
    "pydeseq2",
    "scrublet",
    "doubletdetection",
    "arboreto",
    "palantir",
    "multiqc",
    "kb-python",
    "esda",
    "libpysal",
    "pysal",
    "pot",
    "coloredlogs",
    "humanfriendly",
}


def test_pyproject_thin_pip_layer_excludes_conda_owned_packages():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    seen: set[str] = set()
    for dep in pyproject["project"].get("dependencies", []):
        seen.add(Requirement(dep).name.lower())
    for extra, deps in pyproject["project"]["optional-dependencies"].items():
        for dep in deps:
            req = Requirement(dep)
            if req.name == "omicsclaw":
                continue  # self-extras references are fine
            seen.add(req.name.lower())
    leaked = seen & CONDA_OWNED
    assert not leaked, (
        f"these packages must be installed via mamba (environment.yml) only, "
        f"but still appear in pyproject: {sorted(leaked)}"
    )


def test_pydantic_verified_floor_matches_pip_and_conda_sources():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pip_requirement = next(
        Requirement(value)
        for value in pyproject["project"]["dependencies"]
        if Requirement(value).name.lower() == "pydantic"
    )
    environment_lines = (
        (ROOT / "environment.yml").read_text(encoding="utf-8").splitlines()
    )
    conda_requirement = Requirement(
        next(
            line.strip()[2:]
            for line in environment_lines
            if line.strip().startswith("- pydantic")
        )
    )

    for requirement in (pip_requirement, conda_requirement):
        assert requirement.specifier.contains("2.1.0")
        assert not requirement.specifier.contains("2.0.9")
        assert not requirement.specifier.contains("3.0.0")


def test_channels_extra_installs_authoritative_channel_sdks():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    optional_dependencies = pyproject["project"]["optional-dependencies"]

    assert "channels" in optional_dependencies
    assert "python-telegram-bot>=21.0" in optional_dependencies["channels"]
    assert "lark-oapi>=1.3.0" in optional_dependencies["channels"]
