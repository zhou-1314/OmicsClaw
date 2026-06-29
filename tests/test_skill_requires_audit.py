"""Tests for scripts/audit_skill_requires.py (the per-skill `requires:` auditor).

Locks in the contract that motivated the tool so the analyzer cannot silently
regress: transitive `_lib` backends are detected, the shared `viz/` re-export
does not inflate unrelated skills, names canonicalise once, and `--write` never
drops a declared dependency.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "audit_skill_requires", _ROOT / "scripts" / "audit_skill_requires.py"
)
audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit)


@pytest.fixture(scope="module")
def report():
    return {r["skill"]: r for r in audit.build_report()}


def test_trajectory_includes_gated_and_external_backends(report):
    """cellrank (gated `import`) and palantir (via scanpy.external, registry-
    backed param_hints) must both surface for spatial-trajectory."""
    rec = report["spatial/spatial-trajectory"]["recommended"]
    assert "cellrank" in rec
    assert "palantir" in rec


def test_de_does_not_inherit_unused_viz_backends(report):
    """spatial-de imports plot_expression/plot_features, NOT plot_trajectory, so
    it must not inherit cellrank/scvelo from the eager viz re-export."""
    rec = report["spatial/spatial-de"]["recommended"]
    assert "cellrank" not in rec
    assert "scvelo" not in rec


def test_spatialde_canonicalises_once(report):
    """The registry key is `spatialde` but PyPI is `SpatialDE`; emit one name."""
    rec = report["spatial/spatial-genes"]["recommended"]
    assert rec.count("SpatialDE") == 1
    assert "spatialde" not in rec


def test_soft_fallback_classified_as_optional(report):
    """statsmodels is imported behind a guard, so it is optional (not core)."""
    r = report["spatial/spatial-trajectory"]
    assert "statsmodels" in r["optional"]
    assert "statsmodels" not in r["core"]


def test_local_lib_module_not_treated_as_package(report):
    """`import cnv` (the sibling _lib/cnv module) must not become a dependency;
    the real CNV backend is infercnvpy."""
    rec = report["spatial/spatial-cnv"]["recommended"]
    assert "cnv" not in rec
    assert "infercnvpy" in rec


def test_write_is_union_only(report):
    """`final` (what --write emits) must be a superset of declared — delegating
    skills (consensus/orchestrator) hide deps behind omicsclaw.* and must keep
    their hand-authored requires."""
    for r in report.values():
        declared_norm = {audit.pkg_name(x) for x in r["declared"]}
        assert declared_norm <= set(r["final"]), r["skill"]


def test_package_form_lib_import_resolves_submodules(report):
    """`from skills.<d>._lib import trajectory` must follow trajectory.py (the
    named child module), not stop at _lib/__init__.py. Regression for the
    package-import traversal bug: sc-pseudotime's GRN/trajectory backends live
    in _lib/trajectory.py reached only via this form."""
    libs = report["singlecell/scrna/sc-pseudotime"]["lib_modules"]
    assert any(m.endswith("singlecell/_lib/trajectory.py") for m in libs), libs


def test_grn_detects_pyscenic_stack(report):
    """sc-grn imports pyscenic/dask transitively through _lib/grn.py (package
    form) — they must surface, proving submodule traversal works end-to-end."""
    rec = report["singlecell/scrna/sc-grn"]["recommended"]
    assert "pyscenic" in rec


def test_registry_parses_single_quoted_install_cmds():
    """Entries whose install_cmd uses single quotes (e.g. `.[extra]`) must still
    be registered (regression guard for the quote-style parser)."""
    for name in ("celltypist", "pyVIA", "cellrank", "palantir", "scvi-tools"):
        assert name in audit.REGISTRY_CANON
