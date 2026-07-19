"""Execution-time verification for declared Skill output guarantees."""

from __future__ import annotations

import builtins
import json
import os
from pathlib import Path
import sys

import pytest

from omicsclaw.skill.execution_contract import (
    describe_skill_security,
    primary_anndata_output_path,
    verify_skill_run_outputs,
)


def _valid_envelope(**extra: object) -> dict[str, object]:
    return {
        "skill": "demo-skill",
        "version": "1.0.0",
        "completed_at": "2026-07-15T00:00:00+00:00",
        "input_checksum": "",
        "summary": {"method": "default"},
        "data": {},
        **extra,
    }


def _write_result(output_dir: Path, payload: object | None = None) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "result.json").write_text(
        json.dumps(_valid_envelope() if payload is None else payload),
        encoding="utf-8",
    )


def _info(outputs: dict[str, object]) -> dict[str, object]:
    return {"source": "v2", "output_contract": outputs}


def test_declared_result_json_is_required(tmp_path: Path) -> None:
    report = verify_skill_run_outputs(
        _info({"files": ["report.md", "result.json"]}),
        tmp_path,
    )

    assert report.ok is False
    assert report.codes == ("result_json_missing",)


def test_result_json_must_be_parseable_and_match_the_shared_envelope(tmp_path: Path) -> None:
    (tmp_path / "result.json").write_text("{broken", encoding="utf-8")
    malformed = verify_skill_run_outputs(
        _info({"files": ["result.json"]}),
        tmp_path,
    )
    assert malformed.codes == ("result_json_invalid",)

    _write_result(tmp_path, {"summary": {}, "data": {}})
    incomplete = verify_skill_run_outputs(
        _info({"files": ["result.json"]}),
        tmp_path,
    )
    assert incomplete.ok is False
    assert incomplete.codes == ("result_envelope_invalid",)
    assert "skill must be a non-empty string" in incomplete.violations[0].message


def test_required_result_keys_are_enforced_at_runtime(tmp_path: Path) -> None:
    _write_result(tmp_path)

    report = verify_skill_run_outputs(
        _info(
            {
                "files": ["result.json"],
                "result_json": {"required_keys": ["status", "lineage"]},
            }
        ),
        tmp_path,
    )

    assert report.codes == (
        "result_required_key_missing",
        "result_required_key_missing",
    )
    assert {violation.subject for violation in report.violations} == {
        "status",
        "lineage",
    }


def test_global_file_inventory_is_not_treated_as_an_unconditional_guarantee(
    tmp_path: Path,
) -> None:
    _write_result(tmp_path, _valid_envelope(summary={}))

    report = verify_skill_run_outputs(
        _info(
            {
                "files": [
                    "result.json",
                    "figures/optional-method-only.png",
                    "tables/optional-branch.csv",
                ]
            }
        ),
        tmp_path,
    )

    assert report.ok is True


def test_semantic_artifacts_are_unconditional_guarantees(tmp_path: Path) -> None:
    _write_result(tmp_path)

    report = verify_skill_run_outputs(
        _info(
            {
                "files": ["result.json", "tables/results.csv"],
                "artifacts": [
                    {
                        "kind": "demo.results",
                        "path": "tables/results.csv",
                        "format": "csv",
                    }
                ],
            }
        ),
        tmp_path,
    )

    assert report.codes == ("artifact_missing",)
    assert report.violations[0].subject == "tables/results.csv"


def test_directory_cannot_satisfy_a_declared_file_artifact(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    (output_dir / "tables" / "summary.csv").mkdir(parents=True)
    report = verify_skill_run_outputs(
        {
            "output_contract": {
                "files": ["tables/summary.csv"],
                "artifacts": [
                    {
                        "kind": "table.summary",
                        "path": "tables/summary.csv",
                        "format": "csv",
                    }
                ],
            }
        },
        output_dir,
    )

    assert report.codes == ("artifact_missing",)


def test_in_tree_symlink_cannot_satisfy_a_declared_artifact(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    tables = output_dir / "tables"
    tables.mkdir(parents=True)
    real_table = tables / "real.csv"
    real_table.write_text("gene,value\nA,1\n", encoding="utf-8")
    (tables / "summary.csv").symlink_to(real_table.name)

    report = verify_skill_run_outputs(
        _info(
            {
                "files": ["tables/summary.csv"],
                "artifacts": [
                    {
                        "kind": "table.summary",
                        "path": "tables/summary.csv",
                        "format": "csv",
                    }
                ],
            }
        ),
        output_dir,
    )

    assert report.codes == ("artifact_missing",)


def test_internal_output_claim_cannot_satisfy_an_artifact(tmp_path: Path) -> None:
    from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    (output_dir / OUTPUT_CLAIM_FILENAME).write_text("{}\n", encoding="utf-8")
    report = verify_skill_run_outputs(
        {
            "output_contract": {
                "files": [OUTPUT_CLAIM_FILENAME],
                "artifacts": [
                    {
                        "kind": "internal.claim",
                        "path": OUTPUT_CLAIM_FILENAME,
                        "format": "json",
                    }
                ],
            }
        },
        output_dir,
    )

    assert report.codes == ("artifact_missing",)
    assert report.violations[0].subject == OUTPUT_CLAIM_FILENAME


def test_hardlink_to_internal_claim_cannot_satisfy_an_artifact(tmp_path: Path) -> None:
    from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    claim = output_dir / OUTPUT_CLAIM_FILENAME
    claim.write_text("{}\n", encoding="utf-8")
    (output_dir / "artifact.json").hardlink_to(claim)
    report = verify_skill_run_outputs(
        {
            "output_contract": {
                "files": ["artifact.json"],
                "artifacts": [
                    {
                        "kind": "demo.artifact",
                        "path": "artifact.json",
                        "format": "json",
                    }
                ],
            }
        },
        output_dir,
    )

    assert report.codes == ("artifact_missing",)


def test_hardlink_to_internal_claim_cannot_satisfy_result_json(tmp_path: Path) -> None:
    from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    claim = output_dir / OUTPUT_CLAIM_FILENAME
    claim.write_text(json.dumps(_valid_envelope()), encoding="utf-8")
    (output_dir / "result.json").hardlink_to(claim)

    report = verify_skill_run_outputs(
        _info({"files": ["result.json"]}),
        output_dir,
    )

    assert report.codes == ("result_json_missing",)


def test_in_tree_symlink_cannot_satisfy_result_json(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    payload = output_dir / "payload.json"
    payload.write_text(json.dumps(_valid_envelope()), encoding="utf-8")
    (output_dir / "result.json").symlink_to(payload.name)

    report = verify_skill_run_outputs(
        _info({"files": ["result.json"]}),
        output_dir,
    )

    assert report.codes == ("result_json_missing",)


def test_saves_h5ad_requires_a_readable_primary_anndata(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    _write_result(output_dir)
    info = _info(
        {
            "files": ["result.json", "processed.h5ad"],
            "anndata": {"saves_h5ad": True},
        }
    )

    missing = verify_skill_run_outputs(info, output_dir)
    assert missing.codes == ("anndata_missing",)

    (output_dir / "processed.h5ad").write_text("not hdf5", encoding="utf-8")
    invalid = verify_skill_run_outputs(info, output_dir)
    assert invalid.codes == ("anndata_invalid",)

    (output_dir / "processed.h5ad").unlink()
    anndata = __import__("anndata")
    numpy = __import__("numpy")
    anndata.AnnData(X=numpy.zeros((2, 1), dtype=float)).write_h5ad(
        output_dir / "processed.h5ad"
    )
    valid = verify_skill_run_outputs(info, output_dir)
    assert valid.ok is True


def test_saves_h5ad_probe_uses_the_skill_runtime_not_backend_imports(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output_dir = tmp_path / "out"
    _write_result(output_dir)
    anndata = __import__("anndata")
    numpy = __import__("numpy")
    anndata.AnnData(X=numpy.zeros((1, 1), dtype=float)).write_h5ad(
        output_dir / "processed.h5ad"
    )
    real_import = builtins.__import__

    def _backend_without_anndata(name, *args, **kwargs):
        if name == "anndata" or name.startswith("anndata."):
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _backend_without_anndata)

    report = verify_skill_run_outputs(
        _info(
            {
                "files": ["result.json", "processed.h5ad"],
                "anndata": {"saves_h5ad": True},
            }
        ),
        output_dir,
        runtime_python=sys.executable,
        runtime_env=os.environ.copy(),
    )

    assert report.ok is True


def test_saves_h5ad_probe_preserves_runtime_pythonpath(
    tmp_path: Path,
) -> None:
    """The verifier must use the producer environment, including PYTHONPATH."""

    output_dir = tmp_path / "out"
    _write_result(output_dir)
    (output_dir / "processed.h5ad").write_bytes(b"fake runtime-owned container")
    runtime_modules = tmp_path / "runtime-modules"
    runtime_modules.mkdir()
    runtime_trace = tmp_path / "runtime-import.json"
    (runtime_modules / "anndata.py").write_text(
        "import json\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['ANNDATA_RUNTIME_TRACE']).write_text(\n"
        "    json.dumps({\n"
        "        'sys_path': sys.path,\n"
        "        'python_no_user_site': os.environ.get('PYTHONNOUSERSITE'),\n"
        "        'control_keys': sorted(\n"
        "            key for key in os.environ\n"
        "            if key in {\n"
        "                'OMICSCLAW_REMOTE_AUTH_TOKEN',\n"
        "                'OMICSCLAW_SKILL_EVOLUTION_TOKEN',\n"
        "                'OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD',\n"
        "            }\n"
        "        ),\n"
        "    }),\n"
        "    encoding='utf-8',\n"
        ")\n"
        "class _File:\n"
        "    def close(self):\n"
        "        pass\n"
        "class _Backed:\n"
        "    file = _File()\n"
        "def read_h5ad(path, backed=None):\n"
        "    assert backed == 'r'\n"
        "    return _Backed()\n",
        encoding="utf-8",
    )
    runtime_env = os.environ.copy()
    runtime_env["PYTHONPATH"] = str(runtime_modules)
    runtime_env["PYTHONNOUSERSITE"] = "0"
    runtime_env["ANNDATA_RUNTIME_TRACE"] = str(runtime_trace)
    runtime_env["OMICSCLAW_REMOTE_AUTH_TOKEN"] = "must-not-reach-validator"
    runtime_env["OMICSCLAW_SKILL_EVOLUTION_TOKEN"] = "must-not-reach-validator"
    runtime_env["OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD"] = "3"

    report = verify_skill_run_outputs(
        _info(
            {
                "files": ["result.json", "processed.h5ad"],
                "anndata": {"saves_h5ad": True},
            }
        ),
        output_dir,
        runtime_python=sys.executable,
        runtime_env=runtime_env,
    )

    assert report.ok is True
    observed_runtime = json.loads(runtime_trace.read_text(encoding="utf-8"))
    assert str(runtime_modules) in observed_runtime["sys_path"]
    assert observed_runtime["python_no_user_site"] == "0"
    assert observed_runtime["control_keys"] == []


def test_saves_h5ad_probe_reports_missing_runtime_validator_separately(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "out"
    _write_result(output_dir)
    (output_dir / "processed.h5ad").write_bytes(b"not inspected by fake runtime")
    runtime = tmp_path / "runtime-without-anndata"
    runtime.write_text("#!/bin/sh\nexit 70\n", encoding="utf-8")
    runtime.chmod(0o755)

    with pytest.raises(RuntimeError, match="runtime validator unavailable"):
        verify_skill_run_outputs(
            _info(
                {
                    "files": ["result.json", "processed.h5ad"],
                    "anndata": {"saves_h5ad": True},
                }
            ),
            output_dir,
            runtime_python=str(runtime),
        )


def test_saves_h5ad_rejects_cross_run_hardlink(tmp_path: Path) -> None:
    from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME

    output_dir = tmp_path / "trial-1"
    sibling = tmp_path / "trial-0"
    output_dir.mkdir()
    sibling.mkdir()
    _write_result(output_dir)
    sibling_claim = sibling / OUTPUT_CLAIM_FILENAME
    sibling_claim.write_text("claim", encoding="utf-8")
    (output_dir / "processed.h5ad").hardlink_to(sibling_claim)

    report = verify_skill_run_outputs(
        _info(
            {
                "files": ["result.json", "processed.h5ad"],
                "anndata": {"saves_h5ad": True},
            }
        ),
        output_dir,
    )

    assert report.codes == ("anndata_missing",)


def test_saves_h5ad_rejects_in_tree_symlink_to_readable_anndata(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "out"
    _write_result(output_dir)
    anndata = __import__("anndata")
    numpy = __import__("numpy")
    real_output = output_dir / "real.h5ad"
    anndata.AnnData(X=numpy.zeros((1, 1), dtype=float)).write_h5ad(real_output)
    (output_dir / "processed.h5ad").symlink_to(real_output.name)

    report = verify_skill_run_outputs(
        _info(
            {
                "files": ["result.json", "processed.h5ad"],
                "anndata": {"saves_h5ad": True},
            }
        ),
        output_dir,
    )

    assert report.codes == ("anndata_missing",)


def test_saves_h5ad_supports_one_explicit_nonstandard_inventory_path(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "out"
    _write_result(output_dir)
    anndata = __import__("anndata")
    numpy = __import__("numpy")
    anndata.AnnData(X=numpy.zeros((1, 1), dtype=float)).write_h5ad(
        output_dir / "raw_counts.h5ad"
    )

    report = verify_skill_run_outputs(
        _info(
            {
                "files": ["result.json", "raw_counts.h5ad"],
                "anndata": {"saves_h5ad": True},
            }
        ),
        output_dir,
    )

    assert report.ok is True


def test_primary_anndata_prefers_processed_h5ad_and_rejects_ambiguity(
    tmp_path: Path,
) -> None:
    preferred = {
        "files": ["result.json", "raw_counts.h5ad", "processed.h5ad"],
        "anndata": {"saves_h5ad": True},
    }
    assert primary_anndata_output_path(preferred) == "processed.h5ad"

    ambiguous = {
        "files": ["result.json", "first.h5ad", "second.h5ad"],
        "anndata": {"saves_h5ad": True},
    }
    assert primary_anndata_output_path(ambiguous) is None
    report = verify_skill_run_outputs(_info(ambiguous), tmp_path)
    assert report.codes == ("result_json_missing", "anndata_contract_invalid")


def test_every_registered_saves_h5ad_skill_has_one_primary_inventory_path() -> None:
    from omicsclaw.skill.registry import ensure_registry_loaded

    snapshot = ensure_registry_loaded().snapshot()
    unresolved = []
    for skill_name in snapshot.canonical_aliases:
        info = snapshot.skills[skill_name]
        if not bool(info.get("saves_h5ad")):
            continue
        if primary_anndata_output_path(info.get("output_contract") or {}) is None:
            unresolved.append(skill_name)

    assert unresolved == []


def test_method_scoped_outputs_are_required_only_for_the_selected_method(
    tmp_path: Path,
) -> None:
    # No actual method is recorded, so the requested canonical method is the
    # contract selector.  If the payload records an actual fallback, that value
    # correctly wins instead.
    _write_result(tmp_path, _valid_envelope(summary={}))
    info = _info(
        {
            "files": ["result.json", "tables/dynamical.csv"],
            "method_scopes": [
                {"methods": ["dynamical"], "files": ["tables/dynamical.csv"]}
            ],
        }
    )

    default = verify_skill_run_outputs(info, tmp_path, requested_method="default")
    dynamical = verify_skill_run_outputs(
        info,
        tmp_path,
        requested_method="dynamical",
    )

    assert default.ok is True
    assert dynamical.codes == ("method_output_missing",)


def test_guarantee_paths_cannot_escape_the_output_directory(tmp_path: Path) -> None:
    _write_result(tmp_path)

    report = verify_skill_run_outputs(
        _info(
            {
                "artifacts": [
                    {
                        "kind": "demo.results",
                        "path": "../outside.csv",
                        "format": "csv",
                    }
                ]
            }
        ),
        tmp_path,
    )

    assert report.codes == ("contract_path_invalid",)


def test_guarantee_symlinks_cannot_escape_the_output_directory(
    tmp_path: Path,
) -> None:
    _write_result(tmp_path)
    outside = tmp_path.parent / "outside.csv"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "escaped.csv").symlink_to(outside)

    report = verify_skill_run_outputs(
        _info(
            {
                "artifacts": [
                    {
                        "kind": "demo.results",
                        "path": "escaped.csv",
                        "format": "csv",
                    }
                ]
            }
        ),
        tmp_path,
    )

    assert report.codes == ("contract_path_invalid",)


def test_legacy_or_uncontracted_skill_is_not_retroactively_rejected(
    tmp_path: Path,
) -> None:
    assert verify_skill_run_outputs({}, tmp_path).ok is True
    assert verify_skill_run_outputs({"source": "v1"}, tmp_path).ok is True


def test_output_dir_prefix_is_normalized_for_migrated_contracts(tmp_path: Path) -> None:
    _write_result(tmp_path)
    (tmp_path / "metadata.json").write_text("{}", encoding="utf-8")

    report = verify_skill_run_outputs(
        _info(
            {
                "files": ["output_dir/result.json", "output_dir/metadata.json"],
                "artifacts": [
                    {
                        "kind": "demo.metadata",
                        "path": "output_dir/metadata.json",
                        "format": "json",
                    }
                ],
            }
        ),
        tmp_path,
    )

    assert report.ok is True


def test_security_status_distinguishes_unreviewed_from_declarative_review() -> None:
    unreviewed = describe_skill_security({"security_contract": {}})
    reviewed = describe_skill_security(
        {
            "security_contract": {
                "data_egress": "optional",
                "network": "optional",
                "writes": "output_dir_only",
            },
            "security_reviewed": True,
        }
    )

    assert unreviewed.reviewed is False
    assert unreviewed.enforcement == "undeclared"
    assert reviewed.reviewed is True
    assert reviewed.enforcement == "declarative"
    assert reviewed.network == "optional"
