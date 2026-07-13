from __future__ import annotations

import importlib
import inspect
import json
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest


def _install_fake_skills(monkeypatch, skills: dict, domains: dict | None = None) -> None:
    """Inject fake skills/domains into the live registry for the duration of a test.

    The runner no longer caches ``SKILLS`` / ``DOMAINS`` at module-import time
    (so that ``registry.reload()`` actually takes effect for downstream
    callers). Tests therefore patch ``registry.skills`` / ``registry.domains``
    directly and freeze ``_loaded`` so that any internal
    ``ensure_registry_loaded()`` calls become a no-op rather than rescanning
    disk and clobbering the fake.
    """
    from omicsclaw.skill.registry import SKILLS_DIR, registry

    monkeypatch.setattr(registry, "skills", skills, raising=False)
    monkeypatch.setattr(
        registry, "domains", domains if domains is not None else {"demo": {"name": "Demo"}},
        raising=False,
    )
    monkeypatch.setattr(registry, "_loaded", True, raising=False)
    monkeypatch.setattr(registry, "_loaded_dir", SKILLS_DIR.resolve(), raising=False)


def test_skill_runner_module_exposes_run_skill_contract():
    module = importlib.import_module("omicsclaw.skill.runner")

    assert hasattr(module, "run_skill")
    signature = inspect.signature(module.run_skill)
    assert list(signature.parameters) == [
        "skill_name",
        "input_path",
        "input_paths",
        "output_dir",
        "demo",
        "session_path",
        "extra_args",
        "project_id",  # ADR 0035: project-scoped output
        "project_name",
        "stdout_callback",
        "stderr_callback",
        "cancel_event",
        "status_callback",  # adaptive-env provisioning progress sink
    ]


def test_run_skill_returns_skill_run_result_natively():
    """OMI-12 P1.6: ``run_skill`` returns the typed model — not a dict.

    The unknown-skill error path is the cheapest exit; pin the native
    return type here so a future "convenience dict-wrap" cannot silently
    regress the contract and reintroduce the dict↔model round-trip.
    """
    from omicsclaw.skill.result import SkillRunResult
    from omicsclaw.skill.runner import run_skill

    result = run_skill("__definitely_not_a_real_skill__", demo=True)
    assert isinstance(result, SkillRunResult)
    assert result.success is False
    assert "Unknown skill" in result.stderr

    # The legacy dict shape is still reachable for callers that need it.
    legacy = result.to_legacy_dict()
    assert isinstance(legacy, dict)
    assert legacy["success"] is False
    assert "Unknown skill" in legacy["stderr"]


def test_root_omicsclaw_reexports_shared_run_skill():
    root = importlib.import_module("omicsclaw")
    runner = importlib.import_module("omicsclaw.skill.runner")

    assert root.run_skill is runner.run_skill


def test_explicit_skill_rejects_uninspectable_local_input_before_execution(
    tmp_path,
    monkeypatch,
):
    """RET-04b: named skills share the same fail-closed execution gate.

    A corrupt local ``.h5ad`` must be rejected before the runner creates its
    output directory or starts a subprocess.  This public runner contract is
    intentionally independent of the Analysis Router's auto-route preflight.
    """
    skill_runner = importlib.import_module("omicsclaw.skill.runner")

    fake_script = tmp_path / "must_not_run.py"
    fake_script.write_text("raise SystemExit('must not execute')\n", encoding="utf-8")
    corrupt_input = tmp_path / "corrupt.h5ad"
    corrupt_input.write_text("not an h5ad file\n", encoding="utf-8")
    output_dir = tmp_path / "out"

    _install_fake_skills(
        monkeypatch,
        {
            "guarded-skill": {
                "script": fake_script,
                "domain": "singlecell",
                "demo_args": ["--demo"],
                "allowed_extra_flags": set(),
                "description": "Explicit execution gate test skill",
                "input_contract": {
                    "modalities": ["scrna"],
                    "file_types": ["h5ad"],
                    "preconditions": {
                        "data_shape": {
                            "requires_preprocessed": True,
                            "obsm": ["X_pca"],
                        }
                    },
                },
            }
        },
    )

    subprocess_calls: list[list[str]] = []

    def _must_not_execute(cmd, **_kwargs):
        subprocess_calls.append(cmd)
        raise AssertionError("subprocess must not start after preflight failure")

    monkeypatch.setattr(skill_runner, "drive_subprocess", _must_not_execute)

    result = skill_runner.run_skill(
        "guarded-skill",
        input_path=str(corrupt_input),
        output_dir=str(output_dir),
    )

    assert result.success is False
    assert result.exit_code == -1
    assert "USER_GUIDANCE_JSON:" in result.stderr
    assert "precondition" in result.stderr.lower()
    assert "inspection" in result.stderr.lower()
    assert subprocess_calls == []
    assert not output_dir.exists()


def test_explicit_skill_gate_allows_a_verified_local_input(tmp_path, monkeypatch):
    import anndata as ad
    import numpy as np

    skill_runner = importlib.import_module("omicsclaw.skill.runner")
    fake_script = tmp_path / "verified.py"
    fake_script.write_text("raise SystemExit('driver is stubbed')\n", encoding="utf-8")
    input_path = tmp_path / "verified.h5ad"
    adata = ad.AnnData(np.ones((3, 2)))
    adata.obsm["X_pca"] = np.ones((3, 2))
    adata.uns["omicsclaw_input_contract"] = {
        "domain": "singlecell",
        "modality": "scrna",
        "preprocessed": True,
    }
    adata.write_h5ad(input_path)

    _install_fake_skills(
        monkeypatch,
        {
            "guarded-skill": {
                "script": fake_script,
                "domain": "singlecell",
                "demo_args": ["--demo"],
                "allowed_extra_flags": set(),
                "input_contract": {
                    "modalities": ["scrna"],
                    "file_types": ["h5ad"],
                    "preconditions": {
                        "data_shape": {
                            "requires_preprocessed": True,
                            "obsm": ["X_pca"],
                        }
                    },
                },
            }
        },
    )
    calls: list[list[str]] = []

    def _completed(cmd, **_kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(skill_runner, "drive_subprocess", _completed)

    result = skill_runner.run_skill(
        "guarded-skill",
        input_path=str(input_path),
        output_dir=str(tmp_path / "out"),
    )

    assert result.success is True, result.stderr
    assert len(calls) == 1


def test_explicit_gate_allows_external_h5ad_without_omicsclaw_modality_tag(
    tmp_path,
    monkeypatch,
):
    """Explicit selection supplies intent; unknown identity is not a conflict."""
    import anndata as ad
    import numpy as np

    skill_runner = importlib.import_module("omicsclaw.skill.runner")
    fake_script = tmp_path / "external.py"
    fake_script.write_text("raise SystemExit('driver is stubbed')\n", encoding="utf-8")
    input_path = tmp_path / "external.h5ad"
    adata = ad.AnnData(np.ones((3, 2)))
    adata.obsm["X_pca"] = np.ones((3, 2))
    adata.write_h5ad(input_path)
    _install_fake_skills(
        monkeypatch,
        {
            "guarded-skill": {
                "script": fake_script,
                "domain": "singlecell",
                "demo_args": ["--demo"],
                "allowed_extra_flags": set(),
                "input_contract": {
                    "modalities": ["scrna"],
                    "file_types": ["h5ad"],
                    "preconditions": {
                        "data_shape": {
                            "requires_preprocessed": True,
                            "obsm": ["X_pca"],
                        }
                    },
                },
            }
        },
    )
    calls: list[list[str]] = []

    def _completed(cmd, **_kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(skill_runner, "drive_subprocess", _completed)

    result = skill_runner.run_skill(
        "guarded-skill",
        input_path=str(input_path),
        output_dir=str(tmp_path / "external-out"),
    )

    assert result.success is True, result.stderr
    assert len(calls) == 1


def test_explicit_gate_still_rejects_an_observed_modality_conflict(
    tmp_path,
    monkeypatch,
):
    import anndata as ad
    import numpy as np

    skill_runner = importlib.import_module("omicsclaw.skill.runner")
    fake_script = tmp_path / "conflict.py"
    fake_script.write_text("raise SystemExit('must not execute')\n", encoding="utf-8")
    input_path = tmp_path / "genomics.h5ad"
    adata = ad.AnnData(np.ones((2, 2)))
    adata.uns["omicsclaw_input_contract"] = {"modality": "genomics"}
    adata.write_h5ad(input_path)
    _install_fake_skills(
        monkeypatch,
        {
            "scrna-skill": {
                "script": fake_script,
                "domain": "singlecell",
                "demo_args": ["--demo"],
                "allowed_extra_flags": set(),
                "input_contract": {
                    "modalities": ["scrna"],
                    "file_types": ["h5ad"],
                    "preconditions": {},
                },
            }
        },
    )
    calls: list[list[str]] = []

    def _must_not_execute(cmd, **_kwargs):
        calls.append(cmd)
        raise AssertionError("modality conflict must not reach subprocess")

    monkeypatch.setattr(skill_runner, "drive_subprocess", _must_not_execute)

    result = skill_runner.run_skill(
        "scrna-skill",
        input_path=str(input_path),
        output_dir=str(tmp_path / "conflict-out"),
    )

    assert result.success is False
    assert "modality 'genomics' is incompatible" in result.stderr
    assert calls == []


def test_explicit_gate_preserves_existing_directory_inputs(tmp_path, monkeypatch):
    skill_runner = importlib.import_module("omicsclaw.skill.runner")
    fake_script = tmp_path / "directory.py"
    fake_script.write_text("raise SystemExit('driver is stubbed')\n", encoding="utf-8")
    input_dir = tmp_path / "tenx_matrix.v1"
    input_dir.mkdir()
    (input_dir / "matrix.mtx").write_text("%%MatrixMarket\n", encoding="utf-8")
    _install_fake_skills(
        monkeypatch,
        {
            "standardize-skill": {
                "script": fake_script,
                "domain": "singlecell",
                "demo_args": ["--demo"],
                "allowed_extra_flags": set(),
                "input_contract": {
                    "modalities": ["scrna"],
                    "file_types": ["h5ad", "h5", "loom"],
                    "path_kinds": ["file", "directory"],
                    "preconditions": {},
                },
            }
        },
    )
    calls: list[list[str]] = []

    def _completed(cmd, **_kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(skill_runner, "drive_subprocess", _completed)

    result = skill_runner.run_skill(
        "standardize-skill",
        input_path=str(input_dir),
        output_dir=str(tmp_path / "directory-out"),
    )

    assert result.success is True, result.stderr
    assert len(calls) == 1


def test_explicit_gate_rejects_directory_for_a_file_only_skill(tmp_path, monkeypatch):
    skill_runner = importlib.import_module("omicsclaw.skill.runner")
    fake_script = tmp_path / "file_only.py"
    fake_script.write_text("raise SystemExit('must not execute')\n", encoding="utf-8")
    input_dir = tmp_path / "not_an_h5ad"
    input_dir.mkdir()
    _install_fake_skills(
        monkeypatch,
        {
            "file-only-skill": {
                "script": fake_script,
                "domain": "singlecell",
                "demo_args": ["--demo"],
                "allowed_extra_flags": set(),
                "input_contract": {
                    "modalities": ["scrna"],
                    "file_types": ["h5ad"],
                    "path_kinds": ["file"],
                    "preconditions": {},
                },
            }
        },
    )

    result = skill_runner.run_skill(
        "file-only-skill",
        input_path=str(input_dir),
        output_dir=str(tmp_path / "file-only-out"),
    )

    assert result.success is False
    assert "directory input is incompatible" in result.stderr


def test_explicit_gate_rejects_file_for_a_directory_only_skill(tmp_path, monkeypatch):
    skill_runner = importlib.import_module("omicsclaw.skill.runner")
    fake_script = tmp_path / "directory_only.py"
    fake_script.write_text("raise SystemExit('must not execute')\n", encoding="utf-8")
    input_file = tmp_path / "audit.json"
    input_file.write_text("{}\n", encoding="utf-8")
    _install_fake_skills(
        monkeypatch,
        {
            "directory-only-skill": {
                "script": fake_script,
                "domain": "spatial",
                "demo_args": ["--demo"],
                "allowed_extra_flags": set(),
                "input_contract": {
                    "modalities": [],
                    "file_types": ["json"],
                    "path_kinds": ["directory"],
                    "preconditions": {},
                },
            }
        },
    )

    result = skill_runner.run_skill(
        "directory-only-skill",
        input_path=str(input_file),
        output_dir=str(tmp_path / "directory-only-out"),
    )

    assert result.success is False
    assert "file input is incompatible" in result.stderr


@pytest.mark.parametrize(
    "freeform_input",
    [
        "plain raw text",
        "10.1038/s41586-024-00000-0",
        "https://example.org/paper",
    ],
)
def test_explicit_gate_rejects_freeform_for_a_file_only_skill(
    tmp_path,
    monkeypatch,
    freeform_input,
):
    skill_runner = importlib.import_module("omicsclaw.skill.runner")
    fake_script = tmp_path / "file_only.py"
    fake_script.write_text("raise SystemExit('must not execute')\n", encoding="utf-8")
    _install_fake_skills(
        monkeypatch,
        {
            "file-only-skill": {
                "script": fake_script,
                "domain": "singlecell",
                "demo_args": ["--demo"],
                "allowed_extra_flags": set(),
                "input_contract": {
                    "modalities": ["scrna"],
                    "file_types": ["h5ad"],
                    "path_kinds": ["file"],
                    "preconditions": {},
                },
            }
        },
    )

    result = skill_runner.run_skill(
        "file-only-skill",
        input_path=freeform_input,
        output_dir=str(tmp_path / "freeform-out"),
    )

    assert result.success is False
    assert "freeform input is incompatible" in result.stderr


def test_explicit_skill_gate_does_not_reclassify_demo_or_free_form_inputs(
    tmp_path,
    monkeypatch,
):
    skill_runner = importlib.import_module("omicsclaw.skill.runner")
    fake_script = tmp_path / "flexible.py"
    fake_script.write_text("raise SystemExit('driver is stubbed')\n", encoding="utf-8")
    _install_fake_skills(
        monkeypatch,
        {
            "flexible-skill": {
                "script": fake_script,
                "domain": "literature",
                "demo_args": ["--demo"],
                "allowed_extra_flags": set(),
                "input_contract": {
                    "modalities": [],
                    "file_types": ["pdf"],
                    "path_kinds": ["file", "freeform"],
                    "preconditions": {},
                },
            }
        },
    )
    calls: list[list[str]] = []

    def _completed(cmd, **_kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(skill_runner, "drive_subprocess", _completed)

    demo_result = skill_runner.run_skill(
        "flexible-skill",
        input_path=str(tmp_path / "corrupt.pdf"),
        output_dir=str(tmp_path / "demo-out"),
        demo=True,
    )
    doi_result = skill_runner.run_skill(
        "flexible-skill",
        input_path="10.1038/s41586-024-00000-0",
        output_dir=str(tmp_path / "doi-out"),
    )

    assert demo_result.success is True, demo_result.stderr
    assert doi_result.success is True, doi_result.stderr
    assert len(calls) == 2


def test_explicit_gate_preserves_matching_non_h5ad_inputs_until_they_are_inspectable(
    tmp_path,
    monkeypatch,
):
    """RET-04b must not invent missing structure for formats it cannot probe."""
    skill_runner = importlib.import_module("omicsclaw.skill.runner")
    fake_script = tmp_path / "csv_skill.py"
    fake_script.write_text("raise SystemExit('driver is stubbed')\n", encoding="utf-8")
    input_path = tmp_path / "proteins.csv"
    input_path.write_text("protein_id,intensity\nP1,1.0\n", encoding="utf-8")
    _install_fake_skills(
        monkeypatch,
        {
            "csv-skill": {
                "script": fake_script,
                "domain": "proteomics",
                "demo_args": ["--demo"],
                "allowed_extra_flags": set(),
                "input_contract": {
                    "modalities": ["proteomics"],
                    "file_types": ["csv"],
                    "preconditions": {
                        "data_shape": {
                            "obs": ["sample_id"],
                        }
                    },
                },
            }
        },
    )
    calls: list[list[str]] = []

    def _completed(cmd, **_kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(skill_runner, "drive_subprocess", _completed)

    result = skill_runner.run_skill(
        "csv-skill",
        input_path=str(input_path),
        output_dir=str(tmp_path / "csv-out"),
    )

    assert result.success is True, result.stderr
    assert len(calls) == 1


@pytest.mark.parametrize("filename", ["missing.csv", "missing.vcf"])
def test_explicit_gate_rejects_a_missing_non_h5ad_path(
    tmp_path,
    monkeypatch,
    filename,
):
    """File existence is observable even when content structure is not."""
    skill_runner = importlib.import_module("omicsclaw.skill.runner")
    fake_script = tmp_path / "csv_skill.py"
    fake_script.write_text("raise SystemExit('must not execute')\n", encoding="utf-8")
    missing_input = tmp_path / filename
    output_dir = tmp_path / "missing-out"
    _install_fake_skills(
        monkeypatch,
        {
            "csv-skill": {
                "script": fake_script,
                "domain": "proteomics",
                "demo_args": ["--demo"],
                "allowed_extra_flags": set(),
                "input_contract": {
                    "modalities": ["proteomics"],
                    "file_types": ["csv"],
                    "preconditions": {},
                },
            }
        },
    )
    calls: list[list[str]] = []

    def _must_not_execute(cmd, **_kwargs):
        calls.append(cmd)
        raise AssertionError("missing input must not reach subprocess")

    monkeypatch.setattr(skill_runner, "drive_subprocess", _must_not_execute)

    result = skill_runner.run_skill(
        "csv-skill",
        input_path=str(missing_input),
        output_dir=str(output_dir),
    )

    assert result.success is False
    assert "does not exist" in result.stderr
    assert calls == []
    assert not output_dir.exists()


def test_explicit_gate_returns_a_stable_result_for_an_invalid_session(
    tmp_path,
    monkeypatch,
):
    skill_runner = importlib.import_module("omicsclaw.skill.runner")
    fake_script = tmp_path / "session_skill.py"
    fake_script.write_text("raise SystemExit('must not execute')\n", encoding="utf-8")
    bad_session = tmp_path / "session.json"
    bad_session.write_text("{broken json", encoding="utf-8")
    output_dir = tmp_path / "session-out"
    _install_fake_skills(
        monkeypatch,
        {
            "session-skill": {
                "script": fake_script,
                "domain": "singlecell",
                "demo_args": ["--demo"],
                "allowed_extra_flags": set(),
                "input_contract": {
                    "modalities": ["scrna"],
                    "file_types": ["h5ad"],
                    "preconditions": {},
                },
            }
        },
    )

    result = skill_runner.run_skill(
        "session-skill",
        session_path=str(bad_session),
        output_dir=str(output_dir),
    )

    assert result.success is False
    assert result.exit_code == -1
    assert "USER_GUIDANCE_JSON:" in result.stderr
    assert "session input inspection failed" in result.stderr
    assert not output_dir.exists()


def test_explicit_runner_rejects_missing_input_before_creating_output(
    tmp_path,
    monkeypatch,
):
    skill_runner = importlib.import_module("omicsclaw.skill.runner")
    fake_script = tmp_path / "input_required.py"
    fake_script.write_text("raise SystemExit('must not execute')\n", encoding="utf-8")
    output_dir = tmp_path / "missing-input-out"
    _install_fake_skills(
        monkeypatch,
        {
            "input-required-skill": {
                "script": fake_script,
                "domain": "singlecell",
                "demo_args": ["--demo"],
                "allowed_extra_flags": set(),
                "input_contract": {
                    "modalities": ["scrna"],
                    "file_types": ["h5ad"],
                    "path_kinds": ["file"],
                    "preconditions": {},
                },
            }
        },
    )

    result = skill_runner.run_skill(
        "input-required-skill",
        output_dir=str(output_dir),
    )

    assert result.success is False
    assert "No --input, --demo, or --session provided." in result.stderr
    assert not output_dir.exists()


@pytest.mark.parametrize(
    "session_payload",
    [
        {},
        {"primary_data_path": ""},
        {"primary_data_path": "missing-relative-input"},
    ],
)
def test_explicit_gate_rejects_a_session_without_a_usable_local_input(
    tmp_path,
    monkeypatch,
    session_payload,
):
    skill_runner = importlib.import_module("omicsclaw.skill.runner")
    fake_script = tmp_path / "session_skill.py"
    fake_script.write_text("raise SystemExit('must not execute')\n", encoding="utf-8")
    session_path = tmp_path / "session.json"
    session_path.write_text(json.dumps(session_payload), encoding="utf-8")
    output_dir = tmp_path / "session-out"
    _install_fake_skills(
        monkeypatch,
        {
            "session-skill": {
                "script": fake_script,
                "domain": "singlecell",
                "demo_args": ["--demo"],
                "allowed_extra_flags": set(),
                "input_contract": {
                    "modalities": ["scrna"],
                    "file_types": ["h5ad"],
                    "path_kinds": ["file"],
                    "preconditions": {},
                },
            }
        },
    )

    result = skill_runner.run_skill(
        "session-skill",
        session_path=str(session_path),
        output_dir=str(output_dir),
    )

    assert result.success is False
    assert "USER_GUIDANCE_JSON:" in result.stderr
    assert "session" in result.stderr.lower()
    assert not output_dir.exists()


@pytest.mark.asyncio
async def test_async_explicit_skill_uses_the_same_precondition_gate(
    tmp_path, monkeypatch
):
    skill_runner = importlib.import_module("omicsclaw.skill.runner")
    fake_script = tmp_path / "must_not_run_async.py"
    fake_script.write_text("raise SystemExit('must not execute')\n", encoding="utf-8")
    corrupt_input = tmp_path / "corrupt.h5ad"
    corrupt_input.write_text("not an h5ad file\n", encoding="utf-8")
    output_dir = tmp_path / "async-out"
    _install_fake_skills(
        monkeypatch,
        {
            "guarded-skill": {
                "script": fake_script,
                "domain": "singlecell",
                "demo_args": ["--demo"],
                "allowed_extra_flags": set(),
                "input_contract": {
                    "modalities": ["scrna"],
                    "file_types": ["h5ad"],
                    "preconditions": {},
                },
            }
        },
    )
    calls: list[list[str]] = []

    async def _must_not_execute(cmd, **_kwargs):
        calls.append(cmd)
        raise AssertionError("async subprocess must not start")

    monkeypatch.setattr(skill_runner, "adrive_subprocess", _must_not_execute)

    result = await skill_runner.arun_skill(
        "guarded-skill",
        input_path=str(corrupt_input),
        output_dir=str(output_dir),
    )

    assert result.success is False
    assert "precondition" in result.stderr.lower()
    assert calls == []
    assert not output_dir.exists()


def test_run_skill_streams_stdout_and_stderr_lines_via_callbacks(tmp_path, monkeypatch):
    """The runner must surface skill output line-by-line in real time so that
    long-running deep-learning skills produce visible logs to the bot/operator
    instead of staying silent until completion."""
    skill_runner = importlib.import_module("omicsclaw.skill.runner")

    fake_script = tmp_path / "fake_streamer.py"
    fake_script.write_text(textwrap.dedent("""\
        import argparse, json, sys, time
        from pathlib import Path

        ap = argparse.ArgumentParser()
        ap.add_argument("--demo", action="store_true")
        ap.add_argument("--output", required=True)
        args = ap.parse_args()

        for i in range(3):
            print(f"epoch {i}/3", flush=True)
            time.sleep(0.02)
        print("warning: synthetic stderr", file=sys.stderr, flush=True)
        print("done", flush=True)

        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.json").write_text(json.dumps({"summary": {"method": "fake"}}), encoding="utf-8")
    """), encoding="utf-8")

    monkeypatch.setattr(skill_runner, "DEFAULT_OUTPUT_ROOT", tmp_path)
    _install_fake_skills(monkeypatch, {
        "fake-streamer": {
            "script": fake_script,
            "domain": "demo",
            "demo_args": ["--demo"],
            "allowed_extra_flags": set(),
            "description": "Streaming test skill",
        }
    })

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    result = skill_runner.run_skill(
        "fake-streamer",
        demo=True,
        output_dir=str(tmp_path / "out"),
        stdout_callback=stdout_lines.append,
        stderr_callback=stderr_lines.append,
    )

    assert result.success is True, result.stderr
    assert stdout_lines == ["epoch 0/3", "epoch 1/3", "epoch 2/3", "done"]
    assert stderr_lines == ["warning: synthetic stderr"]
    # Aggregated stdout/stderr fields must still contain the same content.
    for line in stdout_lines:
        assert line in result.stdout
    assert "warning: synthetic stderr" in result.stderr


def test_run_skill_callback_exception_does_not_break_run(tmp_path, monkeypatch):
    """A buggy stdout/stderr callback must not abort the skill — the runner
    swallows callback errors so the underlying analysis still completes."""
    skill_runner = importlib.import_module("omicsclaw.skill.runner")

    fake_script = tmp_path / "fake_one_line.py"
    fake_script.write_text(textwrap.dedent("""\
        import argparse, json
        from pathlib import Path

        ap = argparse.ArgumentParser()
        ap.add_argument("--demo", action="store_true")
        ap.add_argument("--output", required=True)
        args = ap.parse_args()

        print("hello")
        Path(args.output).mkdir(parents=True, exist_ok=True)
        (Path(args.output) / "result.json").write_text(json.dumps({"summary": {}}), encoding="utf-8")
    """), encoding="utf-8")

    monkeypatch.setattr(skill_runner, "DEFAULT_OUTPUT_ROOT", tmp_path)
    _install_fake_skills(monkeypatch, {
        "fake-one-line": {
            "script": fake_script,
            "domain": "demo",
            "demo_args": ["--demo"],
            "allowed_extra_flags": set(),
            "description": "One-line test skill",
        }
    })

    def boom(_line: str) -> None:
        raise RuntimeError("callback exploded")

    result = skill_runner.run_skill(
        "fake-one-line",
        demo=True,
        output_dir=str(tmp_path / "out"),
        stdout_callback=boom,
    )
    assert result.success is True
    assert "hello" in result.stdout


def test_run_skill_cancel_event_kills_long_running_subprocess(tmp_path, monkeypatch):
    """Setting ``cancel_event`` while a skill is running must terminate the
    child subprocess (and its process group) and return promptly.

    Pre-fix the runner's ``popen.wait()`` would not wake up for asyncio
    cancellation, so jobs cancelled by the FastAPI router would leak
    children that kept consuming CPU/GPU until they finished naturally.
    """
    skill_runner = importlib.import_module("omicsclaw.skill.runner")

    fake_script = tmp_path / "fake_long.py"
    fake_script.write_text(textwrap.dedent("""\
        import argparse, time
        ap = argparse.ArgumentParser()
        ap.add_argument("--demo", action="store_true")
        ap.add_argument("--output", required=True)
        args = ap.parse_args()
        for i in range(60):
            print(f"working {i}", flush=True)
            time.sleep(0.5)
    """), encoding="utf-8")

    monkeypatch.setattr(skill_runner, "DEFAULT_OUTPUT_ROOT", tmp_path)
    _install_fake_skills(monkeypatch, {
        "fake-long": {
            "script": fake_script,
            "domain": "demo",
            "demo_args": ["--demo"],
            "allowed_extra_flags": set(),
            "description": "Long-running test skill",
        }
    })

    cancel_event = threading.Event()

    def _trigger_cancel_after_startup() -> None:
        # Wait for the child to actually start producing output before cancelling
        # so we exercise the real "kill while busy" path, not "kill before start".
        time.sleep(1.0)
        cancel_event.set()

    threading.Thread(target=_trigger_cancel_after_startup, daemon=True).start()

    started_at = time.time()
    result = skill_runner.run_skill(
        "fake-long",
        demo=True,
        output_dir=str(tmp_path / "long_out"),
        cancel_event=cancel_event,
    )
    elapsed = time.time() - started_at

    # Without cancellation the fake script would run for ~30s. Cancellation
    # must interrupt within a few seconds (1s pre-cancel + grace + cleanup).
    assert elapsed < 10, f"cancel did not interrupt; ran for {elapsed:.1f}s"
    assert result.success is False
    assert result.exit_code != 0


def test_run_skill_cancellation_with_partial_result_json_is_not_reported_as_success(
    tmp_path, monkeypatch
):
    """A skill that wrote ``result.json`` early then was SIGKILL'd via
    ``cancel_event`` must NOT be silently reclassified as success.

    Pre-fix, the runner mapped any ``returncode == -9`` to ``0`` whenever
    ``result.json`` existed (originally a workaround for the orphan reaper's
    SIGKILL race). After we wired ``cancel_event`` through SIGTERM/SIGKILL,
    ``-9`` also became the *normal* outcome of cancellation — so cancelled
    runs that happened to leave a partial ``result.json`` were silently
    reported as ``success=True``.
    """
    skill_runner = importlib.import_module("omicsclaw.skill.runner")

    fake_script = tmp_path / "fake_partial_then_sleep.py"
    fake_script.write_text(textwrap.dedent("""\
        import argparse, json, signal, time
        from pathlib import Path
        # Ignore SIGTERM so the runner has to escalate to SIGKILL (-9), which is
        # exactly the path that used to trip the "-9 + result.json → success"
        # heuristic.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        ap = argparse.ArgumentParser()
        ap.add_argument("--demo", action="store_true")
        ap.add_argument("--output", required=True)
        args = ap.parse_args()
        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.json").write_text(
            json.dumps({"summary": {"method": "fake", "partial": True}}),
            encoding="utf-8",
        )
        print("partial-result-written", flush=True)
        for _ in range(60):
            time.sleep(0.5)
    """), encoding="utf-8")

    monkeypatch.setattr(skill_runner, "DEFAULT_OUTPUT_ROOT", tmp_path)
    _install_fake_skills(monkeypatch, {
        "fake-partial": {
            "script": fake_script,
            "domain": "demo",
            "demo_args": ["--demo"],
            "allowed_extra_flags": set(),
            "description": "Partial-then-sleep test skill",
        }
    })

    cancel_event = threading.Event()

    def _trigger_cancel_after_partial_write() -> None:
        # Give the child time to write result.json before cancelling.
        time.sleep(1.0)
        cancel_event.set()

    threading.Thread(target=_trigger_cancel_after_partial_write, daemon=True).start()

    result = skill_runner.run_skill(
        "fake-partial",
        demo=True,
        output_dir=str(tmp_path / "partial_out"),
        cancel_event=cancel_event,
    )

    # The partial result.json was on disk when SIGKILL fired, which used to
    # trip the -9 → 0 heuristic. Cancellation must override the heuristic.
    assert result.success is False, (
        "cancelled run with partial result.json must NOT be reported as success"
    )
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Skill-subprocess interpreter + environment isolation (desktop-server
# "missing deps" regression — see project memory
# project_desktop_server_wrong_interpreter_usersite).
#
# Two failure modes, both exercised at the ``_prepare_skill_run`` seam where
# the subprocess argv + env are actually built:
#   1. ``OMICSCLAW_RUN_PYTHON`` was ignored on the main run path (runner.py
#      hardcoded ``sys.executable``), so an app server running in a lighter
#      env could not redirect skills to the analysis env.
#   2. Skill subprocesses inherited ``~/.local`` user-site, letting a broken
#      package there shadow the analysis env's deps.
# ---------------------------------------------------------------------------


def _install_fake_demo_skill(monkeypatch, tmp_path):
    """Register a trivial demo skill and return the runner module."""
    skill_runner = importlib.import_module("omicsclaw.skill.runner")
    fake_script = tmp_path / "fake_prep.py"
    fake_script.write_text("print('noop')\n", encoding="utf-8")
    monkeypatch.setattr(skill_runner, "DEFAULT_OUTPUT_ROOT", tmp_path)
    _install_fake_skills(monkeypatch, {
        "fake-prep": {
            "script": fake_script,
            "domain": "demo",
            "demo_args": ["--demo"],
            "allowed_extra_flags": set(),
            "description": "Prep test skill",
        }
    })
    return skill_runner


def _prepare(skill_runner, tmp_path):
    prepared = skill_runner._prepare_skill_run(
        "fake-prep",
        input_path=None,
        input_paths=None,
        output_dir=str(tmp_path / "out"),
        demo=True,
        session_path=None,
        extra_args=None,
        log_banner=False,
    )
    assert not isinstance(prepared, skill_runner.SkillRunResult), getattr(prepared, "stderr", prepared)
    return prepared


def test_prepare_skill_run_honours_omicsclaw_run_python(tmp_path, monkeypatch):
    """The skill-subprocess interpreter must follow ``OMICSCLAW_RUN_PYTHON``.

    Regression: ``runner.py`` used to hardcode ``PYTHON = sys.executable`` and
    pass it to ``build_skill_argv``, so the documented override silently did
    nothing — skills always ran under whatever interpreter launched the server
    (e.g. base anaconda3 instead of the activated analysis env).
    """
    skill_runner = _install_fake_demo_skill(monkeypatch, tmp_path)

    # An existing absolute path is returned resolved by get_skill_runner_python.
    fake_py = tmp_path / "analysis_env" / "bin" / "python"
    fake_py.parent.mkdir(parents=True)
    fake_py.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("OMICSCLAW_RUN_PYTHON", str(fake_py))

    prepared = _prepare(skill_runner, tmp_path)
    assert prepared.cmd[0] == str(fake_py.resolve())


def test_prepare_skill_run_defaults_to_sys_executable_without_override(tmp_path, monkeypatch):
    skill_runner = _install_fake_demo_skill(monkeypatch, tmp_path)
    monkeypatch.delenv("OMICSCLAW_RUN_PYTHON", raising=False)

    prepared = _prepare(skill_runner, tmp_path)
    assert prepared.cmd[0] == sys.executable


def test_prepare_skill_run_isolates_user_site_by_default(tmp_path, monkeypatch):
    """Skill subprocesses must not inherit ``~/.local`` user-site packages.

    Regression: a broken ABI-mismatched ``~/.local`` torch shadowed the
    analysis env's torch, so CellCharter failed to import even under the
    correct interpreter.
    """
    skill_runner = _install_fake_demo_skill(monkeypatch, tmp_path)
    monkeypatch.delenv("PYTHONNOUSERSITE", raising=False)

    prepared = _prepare(skill_runner, tmp_path)
    assert prepared.env["PYTHONNOUSERSITE"] == "1"


def test_prepare_skill_run_respects_explicit_pythonnousersite_optout(tmp_path, monkeypatch):
    skill_runner = _install_fake_demo_skill(monkeypatch, tmp_path)
    monkeypatch.setenv("PYTHONNOUSERSITE", "0")

    prepared = _prepare(skill_runner, tmp_path)
    assert prepared.env["PYTHONNOUSERSITE"] == "0"
