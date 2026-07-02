"""Per-skill dependency resolution — the data layer for adaptive env provisioning.

Phase 0 of ``docs/proposals/adaptive-environment-provisioning.md``. This module is
PURE METADATA RESOLUTION: it never creates virtualenvs, installs packages, probes
importability, or chooses an interpreter. It answers, for a skill:

  * :func:`required_packages` — which canonical packages this skill's scripts import.
  * :func:`import_name_for`   — the *import* name to probe for a package (pip name
    ≠ import name: ``scvi-tools`` → ``scvi``).
  * :func:`pip_spec_for` / :func:`is_pip_eligible` — whether a *missing* package can
    be pip-installed into an overlay venv here, and the exact pip spec to use.
  * :func:`partition_missing` — split a missing set into installable pip specs vs
    deferred (deny-listed / conda-preferred / unknown-kind) packages.
  * :func:`runtime_kind` — a coarse skill-level hint for logging / early skip.

Sources of truth (reconciled 2026-06-29):
  * SKILL.md frontmatter ``requires:`` — the AST-reconciled Python-package surface
    (``scripts/audit_skill_requires.py``), surfaced as ``skill_info["requires"]``.
    Because it is derived from *Python* imports, R/CLI deps never appear here — so
    the resolver is naturally scoped to the Python surface (hybrid Python/R skills
    like ``spatial-deconv`` only expose their Python deps; the R methods stay on
    the existing ``r_dependency_manager`` validators).
  * ``skills/<domain>/_lib/dependency_manager.py`` ``DEPENDENCY_REGISTRY`` — the
    canonical PyPI-name → import-name map plus ``install_cmd`` kind hints. The
    field is ``module_name`` (spatial/singlecell) OR ``import_name``
    (proteomics/metabolomics); both are read.
  * ``pyproject.toml [project.optional-dependencies]`` — the curated pip-only LEAF
    set and version specs.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# Repo root holds pyproject.toml + skills/. This module lives at
# omicsclaw/skill/execution/dep_spec.py, so the root is parents[3].
_REPO_ROOT = Path(__file__).resolve().parents[3]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

# --------------------------------------------------------------------------- #
# Static maps for common libraries whose PyPI name differs from the import     #
# name and which are NOT in any backend registry. (Inverse of the audit        #
# script's COMMON_MODULE_TO_PKG, plus a few extras.)                           #
# --------------------------------------------------------------------------- #
_COMMON_PKG_TO_IMPORT: dict[str, str] = {
    "scikit-learn": "sklearn",
    "scikit-image": "skimage",
    "scikit-misc": "skmisc",
    "opencv-python": "cv2",
    "pyyaml": "yaml",
    "pillow": "PIL",
    "python-igraph": "igraph",
    "umap-learn": "umap",
    "biopython": "Bio",
    "pytables": "tables",
    "tables": "tables",
    "leidenalg": "leidenalg",
}

# Lightweight scientific base that pip-installs cleanly even into a *bare* venv
# (no conda). These are safe to add to an overlay on a machine that never ran
# ``0_setup_env.sh``. Canonical (PyPI) names.
_BASE_PIP_SAFE: frozenset[str] = frozenset({
    "numpy", "pandas", "scipy", "matplotlib", "seaborn", "statsmodels",
    "scikit-learn", "scikit-image", "scikit-misc", "anndata", "scanpy",
    "networkx", "h5py", "numba", "leidenalg", "python-igraph", "igraph",
    "umap-learn", "tqdm", "requests", "pyyaml", "openpyxl", "adjusttext",
    "natsort", "joblib", "patsy", "pyarrow", "tables", "numexpr",
})

# Packages that are pip-HOSTILE: CUDA/native/deep-learning stacks or meta-packages
# that transitively pull ``torch``/``scvi-tools`` and blow up pip's resolver. They
# live in ``environment.yml`` Tier 4 (conda). On a provisioned conda base they are
# already importable (``--system-site-packages`` makes them visible, so they are
# never "missing"); on a bare base env we must NOT launch a doomed pip mega-solve —
# we defer to ``0_setup_env.sh`` with a hint. Over-listing here only means "defer
# instead of pip", the safe direction.
#
# POLICY NOTE (deliberate, Codex-reviewed): packages that were moved to conda for
# the *bulk* resolution problem but pip-install CLEANLY on their own — scanpy,
# anndata, squidpy (base), and harmonypy/gseapy/pydeseq2/liana/esda/libpysal/POT/
# celltypist/scrublet/doubletdetection/louvain — are intentionally LEFT pip-eligible
# (see the snapshot test). Deferring them would force the heavy conda build this
# feature exists to avoid. Their classification is pinned by ``test_dep_spec`` so it
# is an intentional, reviewable decision, not a blind default. (ABI-shadow safety
# when installing over a partial conda base is handled in Phase 2 via constraints.)
_CONDA_PREFERRED: frozenset[str] = frozenset({
    "torch", "torchvision", "torchaudio", "scvi-tools", "cellrank", "scvelo",
    "palantir", "pyscenic", "arboreto", "scanorama", "bbknn",
    # Meta-packages / leaves that hard-depend on torch or scvi-tools:
    "cellbender", "pertpy", "omicverse", "popv", "velovi",
})

# Never pip-install into the shared overlay: these conflict with the base env or
# require a dedicated conda sub-env (``external_env.py`` BANKSY bridge) / are
# proprietary / have no compatible build. Both PyPI and import names are listed
# so a probe on either form is recognised.
_DENY: frozenset[str] = frozenset({
    "pybanksy", "banksy", "cnvkit", "velocyto", "cellranger", "cellranger-atac",
})

# install_cmd prefixes that mark a NON-pip backend (R / system). Such entries
# never reach a skill's frontmatter ``requires:`` (that is Python-only), but we
# classify defensively in case a registry name is probed directly.
_NON_PIP_INSTALL_RE = re.compile(r"^\s*(Rscript|BiocManager|R\s|conda\s|mamba\s|brew\s|apt|sudo)", re.IGNORECASE)


@lru_cache(maxsize=1)
def _load_registries() -> tuple[dict[str, str], dict[str, str]]:
    """Merge every domain ``DEPENDENCY_REGISTRY`` into (pkg→import, pkg→install_cmd).

    Defensive per-domain: a domain whose ``_lib`` fails to import is skipped with
    a warning rather than breaking resolution for the others. Field name varies
    (``module_name`` vs ``import_name``); both are read.
    """
    pkg_to_import: dict[str, str] = {}
    pkg_to_install: dict[str, str] = {}
    for domain in ("spatial", "singlecell", "proteomics", "metabolomics"):
        try:
            module = __import__(
                f"skills.{domain}._lib.dependency_manager",
                fromlist=["DEPENDENCY_REGISTRY"],
            )
            registry = getattr(module, "DEPENDENCY_REGISTRY", {}) or {}
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("dep_spec: could not load %s registry: %s", domain, exc)
            continue
        for key, info in registry.items():
            import_name = getattr(info, "module_name", None) or getattr(info, "import_name", None)
            if import_name and key not in pkg_to_import:
                pkg_to_import[str(key)] = str(import_name)
            install_cmd = getattr(info, "install_cmd", None)
            if install_cmd and key not in pkg_to_install:
                pkg_to_install[str(key)] = str(install_cmd)
    return pkg_to_import, pkg_to_install


@lru_cache(maxsize=1)
def _load_pyproject_specs() -> dict[str, str]:
    """Parse ``[project.optional-dependencies]`` into pkg → versioned pip spec.

    The curated pip-only LEAF set. Maps the canonical (lower-cased) package name
    to the full spec string (e.g. ``infercnvpy`` → ``infercnvpy>=0.4.0``). The
    first occurrence wins; aggregate ``omicsclaw[...]`` self-references are skipped.
    """
    specs: dict[str, str] = {}
    try:
        import tomllib

        with _PYPROJECT.open("rb") as handle:
            data = tomllib.load(handle)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("dep_spec: could not parse pyproject extras: %s", exc)
        return specs

    extras = (data.get("project", {}) or {}).get("optional-dependencies", {}) or {}
    spec_re = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.\-]*)")
    for requirements in extras.values():
        if not isinstance(requirements, list):
            continue
        for raw in requirements:
            text = str(raw).strip()
            if not text or text.startswith("omicsclaw["):  # aggregate self-ref
                continue
            match = spec_re.match(text)
            if not match:
                continue
            name = match.group(1).lower()
            specs.setdefault(name, text)
    return specs


@lru_cache(maxsize=1)
def _import_index() -> dict[str, str]:
    """Case-insensitive package→import index (registry wins over the common map).

    ``requires:`` lists canonical PyPI casing (``PyYAML``, ``POT``), so all
    name matching is done lower-cased to avoid case-mismatch misses.
    """
    pkg_to_import, _ = _load_registries()
    index: dict[str, str] = {}
    for key, import_name in pkg_to_import.items():
        index.setdefault(key.lower(), import_name)
    for key, import_name in _COMMON_PKG_TO_IMPORT.items():
        index.setdefault(key.lower(), import_name)
    return index


@lru_cache(maxsize=1)
def _install_index() -> dict[str, str]:
    """Case-insensitive package→install_cmd index from the domain registries."""
    _, pkg_to_install = _load_registries()
    return {key.lower(): cmd for key, cmd in pkg_to_install.items()}


_DENY_LOWER = frozenset(name.lower() for name in _DENY)
_CONDA_PREFERRED_LOWER = frozenset(name.lower() for name in _CONDA_PREFERRED)
_BASE_PIP_SAFE_LOWER = frozenset(name.lower() for name in _BASE_PIP_SAFE)


def import_name_for(pkg: str) -> str:
    """Return the *import* name to probe for a canonical/PyPI package name."""
    return _import_index().get(pkg.lower()) or pkg.replace("-", "_")


def required_packages(skill_info: dict) -> list[str]:
    """Canonical packages a skill imports, from ``skill_info['requires']``.

    Deny-listed names are dropped (they are handled by the sub-env bridge / are
    never overlay-installable). Order-preserving, de-duplicated.
    """
    seen: set[str] = set()
    out: list[str] = []
    for pkg in skill_info.get("requires", []) or []:
        name = str(pkg).strip()
        if not name or name in seen:
            continue
        if name.lower() in _DENY_LOWER or import_name_for(name).lower() in _DENY_LOWER:
            continue
        seen.add(name)
        out.append(name)
    return out


def kind_of(pkg: str) -> str:
    """Classify a package: ``pip`` | ``conda`` | ``non-pip`` | ``deny``.

    * ``deny``    — never overlay-install (sub-env / proprietary).
    * ``non-pip`` — R/system backend (install_cmd is Rscript/conda/…).
    * ``conda``   — pip-hostile heavy stack; defer to conda on a bare env.
    * ``pip``     — installable into an overlay venv (base-safe, curated leaf,
                    or — since ``requires:`` is Python-only — an unrecognised but
                    by-construction Python package).
    """
    low = pkg.lower()
    if low in _DENY_LOWER or import_name_for(pkg).lower() in _DENY_LOWER:
        return "deny"
    # Known lightweight base libs are pip-safe even on a bare venv — short-circuit
    # before the conda/install_cmd checks.
    if low in _BASE_PIP_SAFE_LOWER:
        return "pip"
    if low in _CONDA_PREFERRED_LOWER:
        return "conda"
    install_cmd = _install_index().get(low, "")
    if install_cmd and _NON_PIP_INSTALL_RE.match(install_cmd):
        return "non-pip"
    return "pip"


def is_pip_eligible(pkg: str) -> bool:
    """True when a *missing* ``pkg`` can be pip-installed into an overlay here."""
    return kind_of(pkg) == "pip"


def pip_spec_for(pkg: str) -> str | None:
    """Versioned pip spec for ``pkg`` (pyproject constraint if any), else bare name.

    Returns ``None`` when ``pkg`` is not pip-eligible (deny / conda / non-pip).
    """
    if not is_pip_eligible(pkg):
        return None
    return _load_pyproject_specs().get(pkg.lower(), pkg)


def partition_missing(missing: list[str]) -> tuple[list[str], list[str]]:
    """Split missing packages into (installable pip specs, deferred names).

    ``deferred`` are packages we deliberately will NOT overlay-install (deny /
    conda-preferred / non-pip). The caller warns and falls back rather than
    launching a doomed install.
    """
    pip_specs: list[str] = []
    deferred: list[str] = []
    for pkg in missing:
        spec = pip_spec_for(pkg)
        if spec is None:
            deferred.append(pkg)
        else:
            pip_specs.append(spec)
    return pip_specs, deferred


def runtime_kind(skill_info: dict) -> str:
    """Coarse skill-level classification for logging / early skip.

    * ``none``         — no declared Python ``requires:`` (base-deps-only skill).
    * ``python``       — every declared package is overlay-installable.
    * ``python-heavy`` — declares at least one conda-preferred / deferred package.

    This is advisory only; the per-package :func:`partition_missing` is the real
    safety net (a heavy package that is already present in the base env never
    triggers provisioning).

    NOTE (Codex asked for a *method*-level classifier): method-level R/CLI gating
    is unnecessary here because ``requires:`` is derived from *Python* imports, so a
    hybrid skill's R/CLI methods (e.g. ``spatial-deconv --method card``) contribute
    NO packages to this set — they stay on the existing ``r_dependency_manager`` /
    CLI validators. The pip resolver is therefore scoped to the Python surface by
    construction, and a per-method Python split would only ever *shrink* an already
    non-fatal probe set. Kept skill-level deliberately.
    """
    packages = required_packages(skill_info)
    if not packages:
        return "none"
    if any(kind_of(pkg) != "pip" for pkg in packages):
        return "python-heavy"
    return "python"


__all__ = [
    "import_name_for",
    "required_packages",
    "kind_of",
    "is_pip_eligible",
    "pip_spec_for",
    "partition_missing",
    "runtime_kind",
]
