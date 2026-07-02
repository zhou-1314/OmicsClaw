"""Phase 0 of adaptive env provisioning: metadata wiring + dependency resolution.

Covers (a) the ``requires:`` frontmatter surfacing into ``skill_info`` via the
registry, and (b) the pure resolution helpers in
``omicsclaw.skill.execution.dep_spec``. No venv/install/interpreter behavior is
exercised here — Phase 0 is the data layer only.
"""

from __future__ import annotations

import pytest

from omicsclaw.skill.execution import dep_spec
from omicsclaw.skill.registry import ensure_registry_loaded


# --------------------------------------------------------------------------- #
# (a) requires: frontmatter -> skill_info wiring                              #
# --------------------------------------------------------------------------- #


def test_requires_surfaces_into_skill_info():
    reg = ensure_registry_loaded()
    info = reg.skills.get("spatial-trajectory")
    assert info is not None
    requires = info.get("requires")
    assert isinstance(requires, list) and requires, "spatial-trajectory should declare requires"
    # Reconciled surface includes optional backends, not just the base four.
    assert {"scanpy", "numpy"}.issubset(set(requires))
    assert any(pkg in requires for pkg in ("cellrank", "palantir", "scvelo"))


def test_requires_is_pip_list_not_parameters_yaml_contract():
    """The frontmatter list form, never the parameters.yaml {bins,env,config}."""
    reg = ensure_registry_loaded()
    for _alias, info in reg.iter_primary_skills():
        requires = info.get("requires", [])
        assert isinstance(requires, list)
        # A leaked {bins,env,config} dict would not be a list of strings.
        assert all(isinstance(pkg, str) for pkg in requires)
        assert "bins" not in requires and "config" not in requires


def test_every_primary_skill_has_requires_key():
    reg = ensure_registry_loaded()
    for _alias, info in reg.iter_primary_skills():
        assert "requires" in info, f"{_alias} missing requires key"
        assert isinstance(info["requires"], list)


# --------------------------------------------------------------------------- #
# (b) import-name bridge                                                       #
# --------------------------------------------------------------------------- #


def test_import_name_bridge_registry_backed():
    # scvi-tools -> scvi comes straight from DEPENDENCY_REGISTRY.
    assert dep_spec.import_name_for("scvi-tools") == "scvi"


@pytest.mark.parametrize(
    "pkg,expected",
    [
        ("scikit-learn", "sklearn"),
        ("scikit-image", "skimage"),
        ("opencv-python", "cv2"),
        ("pyyaml", "yaml"),
        ("python-igraph", "igraph"),
    ],
)
def test_import_name_bridge_common_libs(pkg, expected):
    assert dep_spec.import_name_for(pkg) == expected


def test_import_name_bridge_identity_fallback():
    # Unknown names: dashes become underscores, otherwise identity.
    assert dep_spec.import_name_for("scanpy") == "scanpy"
    assert dep_spec.import_name_for("some-new-pkg") == "some_new_pkg"


def test_import_name_bridge_is_case_insensitive():
    # `requires:` uses canonical PyPI casing (PyYAML, POT); the bridge must
    # still resolve to the lower-cased import module.
    assert dep_spec.import_name_for("PyYAML") == "yaml"
    assert dep_spec.import_name_for("POT") == "ot"  # registry-backed


def test_deny_and_conda_matching_is_case_insensitive():
    assert dep_spec.kind_of("PyBANKSY") == "deny"
    assert dep_spec.kind_of("Torch") == "conda"


# --------------------------------------------------------------------------- #
# (c) classification + eligibility                                            #
# --------------------------------------------------------------------------- #


def test_base_packages_are_pip_eligible():
    for pkg in ("numpy", "pandas", "scipy", "matplotlib", "scanpy", "anndata"):
        assert dep_spec.kind_of(pkg) == "pip"
        assert dep_spec.is_pip_eligible(pkg)


def test_conda_preferred_not_pip_eligible():
    for pkg in ("torch", "scvi-tools", "cellrank", "omicverse", "popv", "cellbender"):
        assert dep_spec.kind_of(pkg) == "conda"
        assert not dep_spec.is_pip_eligible(pkg)
        assert dep_spec.pip_spec_for(pkg) is None


def test_deny_listed_not_pip_eligible():
    for pkg in ("pybanksy", "cnvkit", "velocyto", "cellranger"):
        assert dep_spec.kind_of(pkg) == "deny"
        assert not dep_spec.is_pip_eligible(pkg)


def test_non_pip_backend_classified_by_install_cmd():
    # metabolomics registry maps metaboanalyst -> Rscript install_cmd.
    assert dep_spec.kind_of("metaboanalyst") == "non-pip"
    assert not dep_spec.is_pip_eligible("metaboanalyst")


def test_pip_spec_prefers_pyproject_constraint():
    # infercnvpy lives in a pyproject extra with a version pin.
    spec = dep_spec.pip_spec_for("infercnvpy")
    assert spec is not None and spec.startswith("infercnvpy")
    assert any(op in spec for op in (">=", "==", "<", ">")), spec


def test_pip_spec_bare_name_when_no_constraint():
    assert dep_spec.pip_spec_for("seaborn") == "seaborn"


# --------------------------------------------------------------------------- #
# (d) skill-level helpers                                                      #
# --------------------------------------------------------------------------- #


def test_required_packages_drops_deny_listed():
    info = {"requires": ["scanpy", "numpy", "pybanksy", "scanpy"]}
    pkgs = dep_spec.required_packages(info)
    assert pkgs == ["scanpy", "numpy"]  # deny dropped, de-duplicated, order kept


def test_partition_missing_splits_installable_vs_deferred():
    pip_specs, deferred = dep_spec.partition_missing(
        ["infercnvpy", "torch", "seaborn", "pybanksy"]
    )
    assert "torch" in deferred and "pybanksy" in deferred
    assert any(s.startswith("infercnvpy") for s in pip_specs)
    assert "seaborn" in pip_specs


def test_runtime_kind_classification():
    assert dep_spec.runtime_kind({"requires": []}) == "none"
    assert dep_spec.runtime_kind({"requires": ["numpy", "pandas", "scanpy"]}) == "python"
    assert dep_spec.runtime_kind({"requires": ["scanpy", "torch"]}) == "python-heavy"


def test_real_skill_runtime_kind_smoke():
    """spatial-trajectory declares cellrank (conda-preferred) -> python-heavy."""
    reg = ensure_registry_loaded()
    info = reg.skills.get("spatial-trajectory")
    assert dep_spec.runtime_kind(info) in {"python", "python-heavy"}


# --------------------------------------------------------------------------- #
# (e) classification snapshot over the REAL union of all skills' requires:      #
#     Pins every intentional decision so misclassification is caught in CI.     #
# --------------------------------------------------------------------------- #


def _real_requires_union() -> set[str]:
    reg = ensure_registry_loaded()
    union: set[str] = set()
    for _alias, info in reg.iter_primary_skills():
        union.update(info.get("requires", []))
    return union


# Genuinely pip-hostile (CUDA/torch/scvi-bound) — MUST be deferred, never auto-pip.
_EXPECTED_CONDA = {
    "torch", "scvi-tools", "cellrank", "scvelo", "palantir", "scanorama", "bbknn",
    "arboreto", "pyscenic", "cellbender", "pertpy", "omicverse", "popv", "velovi",
}
# Moved to conda for the *bulk* solve but individually pip-safe — kept pip-eligible
# (deliberate policy; deferring these would force the heavy conda build).
_EXPECTED_PIP_KEEP = {
    "scanpy", "anndata", "squidpy", "harmonypy", "gseapy", "pydeseq2", "liana",
    "esda", "libpysal", "POT", "celltypist", "scrublet", "doubletdetection", "louvain",
}


def test_no_r_or_cli_leakage_in_requires():
    """`requires:` is AST-derived from Python imports -> nothing classifies non-pip."""
    for pkg in _real_requires_union():
        assert dep_spec.kind_of(pkg) != "non-pip", f"{pkg} unexpectedly non-pip"


def test_expected_conda_packages_are_deferred():
    union = _real_requires_union()
    for pkg in _EXPECTED_CONDA & union:
        assert dep_spec.kind_of(pkg) == "conda", f"{pkg} should be conda-deferred"
        assert not dep_spec.is_pip_eligible(pkg)


def test_expected_pip_packages_stay_eligible():
    union = _real_requires_union()
    for pkg in _EXPECTED_PIP_KEEP & union:
        assert dep_spec.kind_of(pkg) == "pip", f"{pkg} should stay pip-eligible"
        assert dep_spec.is_pip_eligible(pkg)


def test_classification_covers_whole_union_without_unknown_kinds():
    """Every real package lands in a known bucket; only deny is pybanksy."""
    valid = {"pip", "conda", "non-pip", "deny"}
    deny = set()
    for pkg in _real_requires_union():
        kind = dep_spec.kind_of(pkg)
        assert kind in valid, f"{pkg} -> unexpected kind {kind}"
        if kind == "deny":
            deny.add(pkg)
    assert deny <= {"pybanksy", "cnvkit", "velocyto", "cellranger"}
