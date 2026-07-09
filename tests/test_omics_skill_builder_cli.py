"""CLI wiring for P5's --from-paper / --from-tool-docs / --doc-ref flags.

`omics_skill_builder.py` is a thin argparse wrapper around
`create_skill_scaffold` (already covered end-to-end by
tests/test_scaffolder_corpus_derived.py). These tests verify the CLI layer's
own logic — flag parsing, the --from-paper/--from-tool-docs mutual-exclusivity
guard, and correct kwarg threading — by monkeypatching create_skill_scaffold
so nothing touches the real skills/ directory.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "skills" / "orchestrator" / "omics-skill-builder" / "omics_skill_builder.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("omics_skill_builder_under_test", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def builder(monkeypatch):
    module = _load_module()

    calls = []

    def fake_create_skill_scaffold(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            skill_name=kwargs.get("skill_name") or "fake-skill",
            domain=kwargs.get("domain"),
            skill_dir="/fake/skill/dir",
            registry_refreshed=False,
            to_dict=lambda: {"skill_name": "fake-skill"},
        )

    monkeypatch.setattr(module, "create_skill_scaffold", fake_create_skill_scaffold)
    module._calls = calls
    return module


def _run(module, argv, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["omics_skill_builder.py", *argv, "--output", str(tmp_path / "out")])
    module.main()
    return module._calls[-1]


def test_from_paper_threads_into_create_skill_scaffold(builder, tmp_path, monkeypatch):
    kwargs = _run(
        builder,
        [
            "--request", "Cluster spots", "--domain", "spatial",
            "--from-paper", "tests/fixtures/demo_paper.txt", "--doc-ref", "10.1038/xyz",
        ],
        tmp_path, monkeypatch,
    )
    assert kwargs["from_corpus"] == "tests/fixtures/demo_paper.txt"
    assert kwargs["corpus_source_kind"] == "paper"
    assert kwargs["doc_ref"] == "10.1038/xyz"


def test_from_tool_docs_threads_correct_source_kind(builder, tmp_path, monkeypatch):
    kwargs = _run(
        builder,
        [
            "--request", "Cluster spots", "--domain", "spatial",
            "--from-tool-docs", "tests/fixtures/demo_paper.txt",
        ],
        tmp_path, monkeypatch,
    )
    assert kwargs["from_corpus"] == "tests/fixtures/demo_paper.txt"
    assert kwargs["corpus_source_kind"] == "tool_docs"


def test_neither_from_flag_passes_empty_string(builder, tmp_path, monkeypatch):
    kwargs = _run(
        builder,
        ["--request", "Cluster spots", "--domain", "spatial"],
        tmp_path, monkeypatch,
    )
    assert kwargs["from_corpus"] == ""


def test_from_paper_and_from_tool_docs_together_raises(builder, tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys, "argv",
        [
            "omics_skill_builder.py",
            "--request", "x", "--domain", "spatial",
            "--from-paper", "a.txt", "--from-tool-docs", "b.txt",
            "--output", str(tmp_path / "out"),
        ],
    )
    with pytest.raises(SystemExit, match="mutually exclusive"):
        builder.main()
