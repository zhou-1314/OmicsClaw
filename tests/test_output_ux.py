"""Tests for human-friendly output directory UX."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import nbformat

from omicsclaw.common.report import (
    build_output_dir_name,
    extract_method_name,
    write_output_readme,
)


ROOT = Path(__file__).resolve().parent.parent


def _load_omicsclaw_script():
    spec = importlib.util.spec_from_file_location("omicsclaw_main_test", ROOT / "omicsclaw.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_extract_method_name_prefers_summary_method():
    payload = {
        "summary": {"method": "cellcharter"},
        "data": {"params": {"method": "leiden"}},
    }
    assert extract_method_name(payload) == "cellcharter"


def test_write_output_readme_surfaces_method_params_and_entrypoints(tmp_path):
    payload = {
        "skill": "spatial-domains",
        "completed_at": "2026-03-29T06:26:34+00:00",
        "summary": {
            "method": "cellcharter",
            "n_domains": 2,
            "domain_counts": {"0": 10, "1": 8},
        },
        "data": {
            "params": {
                "method": "cellcharter",
                "resolution": 1.0,
                "auto_k": True,
            }
        },
    }
    (tmp_path / "report.md").write_text("# report\n", encoding="utf-8")
    (tmp_path / "figures").mkdir()
    (tmp_path / "reproducibility").mkdir()
    notebook_path = tmp_path / "reproducibility" / "analysis_notebook.ipynb"
    notebook_path.write_text("{}", encoding="utf-8")
    (tmp_path / "result.json").write_text(json.dumps(payload), encoding="utf-8")

    readme_path = write_output_readme(
        tmp_path,
        skill_alias="spatial-domain-identification",
        description="Identify tissue domains",
        result_payload=payload,
        notebook_path=notebook_path,
    )

    text = readme_path.read_text(encoding="utf-8")
    assert "spatial-domain-identification" in text
    assert "`cellcharter`" in text
    assert "`resolution`: 1" in text
    assert "Open `report.md`" in text
    assert "analysis_notebook.ipynb" in text
    assert "`figures/`" in text
    assert "Identify tissue domains" in text


def test_write_output_readme_does_not_advertise_hardlinked_report(tmp_path):
    source = tmp_path / "source.md"
    source.write_text("# unowned report\n", encoding="utf-8")
    (tmp_path / "report.md").hardlink_to(source)

    readme_path = write_output_readme(
        tmp_path,
        skill_alias="spatial-domain-identification",
    )

    text = readme_path.read_text(encoding="utf-8")
    assert "This run did not generate `report.md`" in text
    assert "Open `report.md`" not in text


def test_write_output_readme_does_not_inventory_contained_directory_symlink(
    tmp_path: Path,
) -> None:
    real_dir = tmp_path / "figures"
    real_dir.mkdir()
    (tmp_path / "figures-alias").symlink_to(
        real_dir.name,
        target_is_directory=True,
    )

    readme_path = write_output_readme(tmp_path, skill_alias="demo")

    text = readme_path.read_text(encoding="utf-8")
    assert "`figures/`" in text
    assert "figures-alias" not in text


def test_build_output_dir_name_includes_method_when_available():
    name = build_output_dir_name("spatial-domain-identification", "20260329_063000", method="CellCharter")
    assert name == "spatial-domain-identification__cellcharter__20260329_063000"


def test_run_skill_generates_readme_and_human_readable_dir(monkeypatch, tmp_path):
    oc = _load_omicsclaw_script()
    from omicsclaw.skill import runner as skill_runner

    fake_script = tmp_path / "demo" / "fake-skill" / "fake_skill.py"
    fake_script.parent.mkdir(parents=True)
    fake_script.write_text("print('fake')\n", encoding="utf-8")

    monkeypatch.setattr(skill_runner, "DEFAULT_OUTPUT_ROOT", tmp_path)

    from omicsclaw.skill.registry import OmicsRegistry, registry

    fake_registry = OmicsRegistry()
    fake_registry.skills = {
        "fake-skill": {
            "alias": "fake-skill",
            "script": fake_script,
            "domain": "demo",
            "demo_args": ["--demo"],
            "allowed_extra_flags": {"--method"},
            "description": "Synthetic test skill",
        }
    }
    fake_registry.canonical_aliases = ["fake-skill"]
    fake_registry.domains = {"demo": {"name": "Demo"}}
    fake_registry._loaded_dir = tmp_path.resolve()
    fake_registry._loaded = True
    monkeypatch.setattr(registry, "_state", fake_registry.snapshot()._state)
    monkeypatch.setattr(skill_runner, "ensure_registry_loaded", lambda: registry)

    def fake_drive_subprocess(cmd, *, out_dir, **_kwargs):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.md").write_text("# Fake report\n", encoding="utf-8")
        payload = {
            "skill": "fake-skill-internal",
            "completed_at": "2026-03-29T06:26:34+00:00",
            "summary": {"method": "cellcharter", "score": 0.98},
            "data": {"params": {"method": "cellcharter", "resolution": 1.0}},
        }
        (out_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")
        (out_dir / "claim-alias.json").hardlink_to(
            out_dir / ".omicsclaw-run-claim.json"
        )
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(skill_runner, "drive_subprocess", fake_drive_subprocess)
    monkeypatch.setattr(skill_runner.time, "sleep", lambda _seconds: None)

    result = oc.run_skill("fake-skill", demo=True, extra_args=["--method", "cellcharter"])

    assert result.success is True
    assert result.method == "cellcharter"
    assert "__cellcharter__" in Path(result.output_dir).name
    assert Path(result.readme_path).exists()
    assert Path(result.notebook_path).exists()
    assert "README.md" in result.files
    assert "analysis_notebook.ipynb" in result.files
    assert ".omicsclaw-run-claim.json" not in result.files
    assert "claim-alias.json" not in result.files
    readme_text = Path(result.readme_path).read_text(encoding="utf-8")
    assert "Synthetic test skill" in readme_text
    assert "cellcharter" in readme_text
    assert "analysis_notebook.ipynb" in readme_text
    assert ".omicsclaw-run-claim.json" not in readme_text
    assert "claim-alias.json" not in readme_text

    notebook = nbformat.read(result.notebook_path, as_version=4)
    assert notebook.metadata["omicsclaw"]["skill"] == "fake-skill"
    sources = "\n".join(cell.source for cell in notebook.cells)
    assert "load_skill" in sources
    assert "ACTUAL_RUN_COMMAND" in sources
    assert "preview_function" in sources


def test_pipeline_readme_lists_step_methods(tmp_path):
    from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME
    from omicsclaw.skill.runner import _write_pipeline_readme

    claim = tmp_path / OUTPUT_CLAIM_FILENAME
    claim.write_text("{}\n", encoding="utf-8")
    (tmp_path / "claim-alias.json").hardlink_to(claim)
    readme_path = _write_pipeline_readme(
        tmp_path,
        pipeline_name="spatial-pipeline",
        completed_at="2026-03-29T06:30:00+00:00",
        results={
            "preprocess": {
                "success": True,
                "method": "scanpy",
                "output_dir": str(tmp_path / "preprocess"),
                "notebook_path": str(tmp_path / "preprocess" / "reproducibility" / "analysis_notebook.ipynb"),
            },
            "domains": {
                "success": True,
                "method": "cellcharter",
                "output_dir": str(tmp_path / "domains"),
                "notebook_path": str(tmp_path / "domains" / "reproducibility" / "analysis_notebook.ipynb"),
            },
        },
    )

    text = readme_path.read_text(encoding="utf-8")
    assert "spatial-pipeline" in text
    assert "scanpy" in text
    assert "cellcharter" in text
    assert "`preprocess`" in text
    assert "analysis_notebook.ipynb" in text
    assert OUTPUT_CLAIM_FILENAME not in text
    assert "claim-alias.json" not in text


def test_pipeline_readme_does_not_inventory_contained_directory_symlink(
    tmp_path: Path,
) -> None:
    from omicsclaw.skill.runner import _write_pipeline_readme

    step_dir = tmp_path / "step-one"
    step_dir.mkdir()
    (tmp_path / "step-alias").symlink_to(
        step_dir.name,
        target_is_directory=True,
    )

    readme_path = _write_pipeline_readme(
        tmp_path,
        pipeline_name="demo-pipeline",
        completed_at="2026-07-17T00:00:00+00:00",
        results={},
    )

    text = readme_path.read_text(encoding="utf-8")
    assert "`step-one/`" in text
    assert "step-alias" not in text


def test_analysis_notebook_rejects_claim_aliases(tmp_path):
    from omicsclaw.common.notebook_export import write_analysis_notebook
    from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME

    output_dir = tmp_path / "out"
    (output_dir / "figures").mkdir(parents=True)
    (output_dir / "tables").mkdir()
    claim = output_dir / OUTPUT_CLAIM_FILENAME
    claim.write_text("{}\n", encoding="utf-8")
    (output_dir / "processed.h5ad").hardlink_to(claim)
    (output_dir / "figures" / "claim.png").hardlink_to(claim)
    (output_dir / "tables" / "claim.csv").hardlink_to(claim)
    (output_dir / "figures" / "plot.png").write_bytes(b"png")
    (output_dir / "tables" / "table.csv").write_text("a\n1\n", encoding="utf-8")

    notebook_path = write_analysis_notebook(
        output_dir,
        skill_alias="demo-skill",
        result_payload={"summary": {}, "data": {}},
    )
    notebook = nbformat.read(notebook_path, as_version=4)
    rendered = "\n".join(cell.source for cell in notebook.cells)

    assert "processed.h5ad" not in rendered
    assert "claim.png" not in rendered
    assert "claim.csv" not in rendered
    assert "plot.png" in rendered
    assert "table.csv" in rendered


def test_spatial_genes_help_does_not_require_scanpy_runtime():
    script = ROOT / "skills" / "spatial" / "spatial-genes" / "spatial_genes.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )

    assert result.returncode == 0
    assert "--morans-coord-type" in result.stdout
    assert "--sparkx-option" in result.stdout
    assert "--flashs-bandwidth" in result.stdout
