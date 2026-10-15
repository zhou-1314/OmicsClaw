"""Tests for the canonical AST dependency registry (omicsclaw.skillenv.deps_registry)."""

from __future__ import annotations

import ast
import re

from omicsclaw.skillenv import deps_registry
from tests.skillenv.conftest import REAL_REGISTRY, REPO

REQUIRED_FIELDS = {"module", "kind", "install", "description"}
OPTIONAL_FIELDS = {"also", "alt_env"}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _literal():
    tree = ast.parse(REAL_REGISTRY.read_text(encoding="utf-8"))
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else (
            node.targets[0] if isinstance(node, ast.Assign) else None)
        if isinstance(target, ast.Name) and target.id == "DEPENDENCIES":
            assert isinstance(node, ast.AnnAssign), "DEPENDENCIES must be an annotated assignment"
            return ast.literal_eval(node.value)
    raise AssertionError("no DEPENDENCIES assignment")


def test_the_registry_is_a_literal_equal_to_the_import():
    assert _literal() == deps_registry.DEPENDENCIES


def test_size_keys_and_uniqueness():
    table = deps_registry.DEPENDENCIES
    assert len(table) == 70
    normalised = [_norm(k) for k in table]
    assert len(set(normalised)) == len(normalised)
    modules = [v["module"] for v in table.values()]
    assert len(set(modules)) == len(modules)
    module_owner = {v["module"]: k for k, v in table.items()}
    key_of_norm = {_norm(k): k for k in table}
    ambiguous = {
        m: (module_owner[m], key_of_norm[_norm(m)])
        for m in module_owner
        if _norm(m) in key_of_norm and key_of_norm[_norm(m)] != module_owner[m]
    }
    assert ambiguous == {}
    assert "SpatialDE" in table and "spatialde" not in table
    assert "mzmine" not in table and "pymzml" in table


def test_every_entry_follows_the_field_contract():
    for key, value in deps_registry.DEPENDENCIES.items():
        assert isinstance(value, dict), key
        assert REQUIRED_FIELDS <= set(value), key
        assert set(value) - REQUIRED_FIELDS <= OPTIONAL_FIELDS, key
        assert value["kind"] in {"pip", "git", "r"}, key
        assert all(isinstance(value[f], str) and value[f] for f in REQUIRED_FIELDS), key
        if "also" in value:
            assert isinstance(value["also"], list) and all(isinstance(x, str) for x in value["also"])
        if value["kind"] == "pip":
            assert value["install"] == "pip install " + " ".join([key, *value.get("also", [])]), key
        assert '-e ".[' not in value["install"], key


def test_the_special_entries_are_exactly_these():
    table = deps_registry.DEPENDENCIES
    assert {k for k, v in table.items() if v["kind"] == "git"} == {"STAGATE-pyG", "pybanksy", "STalign"}
    assert {k for k, v in table.items() if v["kind"] == "r"} == {"xcms", "metaboanalyst", "Matrix", "numbat", "CellChat", "DESeq2", "sva", "survival", "WGCNA"}
    assert {k for k, v in table.items() if "also" in v} == {"singler"}
    assert table["singler"]["also"] == ["singlecellexperiment"]
    assert {k for k, v in table.items() if "alt_env" in v} == {"pybanksy"}
    assert table["pybanksy"]["alt_env"] == "omicsclaw_banksy"


def test_the_four_domain_managers_are_gone():
    for domain in ("spatial", "singlecell", "proteomics", "metabolomics"):
        assert not (REPO / "skills" / domain / "_lib" / "dependency_manager.py").exists()
