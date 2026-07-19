from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
import threading

import pytest


_GOVERNED_REFERENCE_TYPE = "linux-user-systemd-bwrap-v1"
_GOVERNED_REFERENCE = f"omicsclaw-run-{'a' * 24}.scope"


def _patch_governed_start(monkeypatch, api, owner_type) -> None:
    def resolve_provider(request):
        return {
            "provider": request.provider_id or "ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": request.llm_model or "local-test-model",
            "api_key": "governed-test-key",
        }

    monkeypatch.setattr(api, "governed_worker_available", lambda: True)
    monkeypatch.setattr(
        api,
        "_resolve_governed_worker_provider",
        resolve_provider,
    )
    monkeypatch.setattr(
        api,
        "new_governed_worker_reference",
        lambda: (_GOVERNED_REFERENCE_TYPE, _GOVERNED_REFERENCE),
    )
    monkeypatch.setattr(api, "GovernedAutoAgentWorker", owner_type)


def _governed_success_result(request: dict[str, object]) -> dict[str, object]:
    return {
        "success": True,
        "mode": "harness_evolution",
        "skill": request["skill_name"],
        "method": request["method"],
        "evolution_goal": request["evolution_goal"],
        "output_dir": request["output_dir"],
        "promotion": {"status": "skipped"},
        "best_params": {"alpha": 1.0},
        "best_score": 0.75,
    }


@pytest.fixture
def bound_autoagent_repository(tmp_path: Path):
    """Mirror the production Desktop lifespan's Control binding."""

    from omicsclaw.autoagent import api
    from omicsclaw.control import ControlStateRepository

    repository = ControlStateRepository(tmp_path / "autoagent-control")
    api._sessions.clear()
    api._start_timestamps.clear()
    api.bind_autoagent_repository(repository)
    try:
        yield repository
    finally:
        api._sessions.clear()
        api.unbind_autoagent_repository(repository)
        repository.close()


def _write_trial_authority_skill(
    project_root: Path,
    *,
    primary_anndata: str = "sandbox_primary.h5ad",
) -> None:
    """Create one manifest-backed sandbox Skill for real authority capture."""

    (project_root / "omicsclaw.py").write_text("# intercepted\n", encoding="utf-8")
    skill_dir = project_root / "skills" / "spatial" / "sandbox-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "run.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text(
        "\n".join(
            [
                "schema_version: 2",
                "id: sandbox-skill",
                "name: sandbox-skill",
                "domain: spatial",
                "version: 2.3.4",
                "summary:",
                "  load_when: testing per-trial sandbox authority",
                "  aliases: [sandbox-skill-legacy]",
                "interface:",
                "  outputs:",
                f"    files: [{primary_anndata}, result.json]",
                "    anndata:",
                "      saves_h5ad: true",
                "runtime:",
                "  language: python",
                "  entry: run.py",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _trial_authority(
    skill_name: str = "test-skill",
    *,
    primary_anndata: str | None = None,
):
    from omicsclaw.autoagent.authority import TrialSkillAuthority

    revision = "sha256:" + "a" * 64
    return TrialSkillAuthority(
        requested_skill_name=skill_name,
        canonical_skill_id=skill_name,
        skill_version="1.0.0",
        manifest_hash=revision,
        source_hash=revision,
        primary_anndata_path=primary_anndata,
        skills_root="/test/skills",
    )


@pytest.mark.parametrize(
    "primary_anndata",
    [
        "..\\escape.h5ad",
        "nested\\primary.h5ad",
        "C:\\escape.h5ad",
        "C:/escape.h5ad",
    ],
)
def test_trial_authority_rejects_cross_platform_escaping_primary_anndata(
    primary_anndata,
):
    from omicsclaw.autoagent.authority import TrialSkillAuthority

    revision = "sha256:" + "a" * 64
    with pytest.raises(ValueError, match="primary AnnData path"):
        TrialSkillAuthority(
            requested_skill_name="test-skill",
            canonical_skill_id="test-skill",
            skill_version="1.0.0",
            manifest_hash=revision,
            source_hash=revision,
            primary_anndata_path=primary_anndata,
            skills_root="/test/skills",
        )


def _write_run_claim(
    output_dir: Path,
    authority,
    *,
    skill_name: str | None = None,
    claim_id: str = "a" * 32,
    claimed_at: str = "2026-07-17T00:00:00+00:00",
) -> None:
    from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME

    (output_dir / OUTPUT_CLAIM_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "claim_id": claim_id,
                "owner": f"skill:{skill_name or authority.canonical_skill_id}",
                "claimed_at": claimed_at,
                "audit_identity": {
                    "skill_id": authority.canonical_skill_id,
                    "skill_version": authority.skill_version,
                    "skill_hash": authority.manifest_hash,
                    "source_hash": authority.source_hash,
                    "environment_id": "env:" + "a" * 20,
                },
                "runtime_source": "base",
            }
        ),
        encoding="utf-8",
    )


def _capture_trial_authority(project_root: Path, requested_skill_name: str):
    from omicsclaw.autoagent.authority import capture_trial_skill_authority

    return capture_trial_skill_authority(project_root, requested_skill_name)


def _write_valid_run_result(
    output_dir: Path,
    skill_name: str = "sandbox-skill",
    version: str = "2.3.4",
    **data,
) -> None:
    (output_dir / "result.json").write_text(
        json.dumps(
            {
                "skill": skill_name,
                "version": version,
                "completed_at": "2026-07-17T00:00:00+00:00",
                "input_checksum": "",
                "summary": {},
                "data": data,
                "status": "ok",
            }
        ),
        encoding="utf-8",
    )


def test_parent_trace_uses_frozen_sandbox_authority_not_global_registry(
    monkeypatch,
    tmp_path,
):
    """The parent must inspect the artifact declared by the executed sandbox."""

    from types import SimpleNamespace

    from omicsclaw.autoagent.runner import execute_trial
    from omicsclaw.autoagent.search_space import SearchSpace
    from omicsclaw.autoagent.trace import TraceCollector

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    _write_trial_authority_skill(sandbox)
    authority = _capture_trial_authority(sandbox, "sandbox-skill-legacy")
    output = tmp_path / "trial"

    class CompletedProc:
        returncode = 0

        def __init__(self, args, **_kwargs):
            self.args = args

        def communicate(self, timeout=None):
            output.mkdir(parents=True, exist_ok=True)
            _write_valid_run_result(output)
            _write_run_claim(output, authority)
            (output / "sandbox_primary.h5ad").write_text(
                "sandbox", encoding="utf-8"
            )
            (output / "global_primary.h5ad").write_text(
                "global", encoding="utf-8"
            )
            return "", ""

    monkeypatch.setattr(
        "omicsclaw.autoagent.runner.subprocess.Popen",
        CompletedProc,
    )
    execution = execute_trial(
        skill_name="sandbox-skill-legacy",
        input_path="",
        output_dir=output,
        params={},
        search_space=SearchSpace(
            skill_name="sandbox-skill",
            method="default",
            tunable=[],
        ),
        project_root=sandbox,
        demo=True,
    )

    # Reproduce the A/B split: the process-global Registry advertises a
    # different primary artifact than the sandbox the child just executed.
    monkeypatch.setattr(
        "omicsclaw.skill.registry.ensure_registry_loaded",
        lambda: SimpleNamespace(
            skills={
                "sandbox-skill-legacy": {
                    "alias": "global-skill",
                    "saves_h5ad": True,
                    "output_contract": {
                        "files": ["global_primary.h5ad", "result.json"],
                        "anndata": {"saves_h5ad": True},
                    },
                }
            }
        ),
    )
    observed: list[Path] = []

    def _read(path):
        observed.append(Path(path))
        return {"n_obs": 7, "n_vars": 3}

    monkeypatch.setattr("omicsclaw.autoagent.trace._read_adata_shape", _read)

    trace = TraceCollector.collect(
        trial_id=1,
        skill_name="sandbox-skill-legacy",
        method="default",
        execution=execution,
        output_dir=output,
    )

    assert execution.success is True
    assert execution.authority is not None
    assert execution.authority.requested_skill_name == "sandbox-skill-legacy"
    assert execution.authority.canonical_skill_id == "sandbox-skill"
    assert execution.authority.skill_version == "2.3.4"
    assert execution.authority.manifest_hash.startswith("sha256:")
    assert execution.authority.source_hash.startswith("sha256:")
    assert observed == [output / "sandbox_primary.h5ad"]
    assert trace.data_shape.n_obs_after == 7
    assert trace.authority == execution.authority


def test_execute_trial_rejects_sandbox_source_revision_drift(monkeypatch, tmp_path):
    """A child-time source edit invalidates the trial before parent scoring."""

    from omicsclaw.autoagent.runner import execute_trial
    from omicsclaw.autoagent.search_space import SearchSpace

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    _write_trial_authority_skill(sandbox)
    output = tmp_path / "trial"
    runtime_entry = sandbox / "skills" / "spatial" / "sandbox-skill" / "run.py"

    class DriftingProc:
        returncode = 0

        def __init__(self, args, **_kwargs):
            self.args = args

        def communicate(self, timeout=None):
            output.mkdir(parents=True, exist_ok=True)
            (output / "result.json").write_text("{}", encoding="utf-8")
            runtime_entry.write_text("print('changed')\n", encoding="utf-8")
            return "", ""

    monkeypatch.setattr(
        "omicsclaw.autoagent.runner.subprocess.Popen",
        DriftingProc,
    )

    execution = execute_trial(
        skill_name="sandbox-skill",
        input_path="",
        output_dir=output,
        params={},
        search_space=SearchSpace(
            skill_name="sandbox-skill",
            method="default",
            tunable=[],
        ),
        project_root=sandbox,
        demo=True,
    )

    assert execution.success is False
    assert execution.authority is None
    assert execution.exit_code == -1
    assert "post-verified" in execution.authority_error


def test_execute_trial_without_provable_sandbox_authority_fails_before_spawn(
    monkeypatch,
    tmp_path,
):
    from omicsclaw.autoagent.runner import execute_trial
    from omicsclaw.autoagent.search_space import SearchSpace

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "omicsclaw.py").write_text("# no skills\n", encoding="utf-8")
    monkeypatch.setattr(
        "omicsclaw.autoagent.runner.subprocess.Popen",
        lambda *_args, **_kwargs: pytest.fail(
            "an unbound sandbox must fail before child spawn"
        ),
    )

    execution = execute_trial(
        skill_name="unknown-skill",
        input_path="",
        output_dir=tmp_path / "trial",
        params={},
        search_space=SearchSpace(
            skill_name="unknown-skill",
            method="default",
            tunable=[],
        ),
        project_root=sandbox,
        demo=True,
    )

    assert execution.success is False
    assert execution.authority is None
    assert execution.exit_code == -1
    assert "could not be established" in execution.authority_error


def test_evaluator_uses_trial_authority_primary_anndata(monkeypatch, tmp_path):
    """Scoring must consume the same frozen primary path as execution."""

    from omicsclaw.autoagent.authority import capture_trial_skill_authority
    from omicsclaw.autoagent.evaluator import Evaluator
    from omicsclaw.autoagent.metrics_registry import MetricDef

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    _write_trial_authority_skill(sandbox)
    authority = capture_trial_skill_authority(
        sandbox,
        "sandbox-skill-legacy",
    )
    output = tmp_path / "trial"
    output.mkdir()
    _write_valid_run_result(output)
    _write_run_claim(output, authority)
    declared = output / "sandbox_primary.h5ad"
    declared.write_text("sandbox", encoding="utf-8")
    (output / "processed.h5ad").write_text("wrong fallback", encoding="utf-8")
    observed: list[Path] = []

    def _compute(path, **_kwargs):
        observed.append(Path(path))
        return {"score": 0.9}

    monkeypatch.setattr(
        "omicsclaw.autoagent.metrics_compute.compute_metrics_from_adata",
        _compute,
    )
    evaluator = Evaluator(
        {
            "score": MetricDef(
                source="result.json:summary.score",
                direction="maximize",
                weight=1.0,
            )
        },
        skill_name="sandbox-skill-legacy",
    )

    result = evaluator.evaluate(output, authority=authority)

    assert result.raw_metrics == {"score": 0.9}
    assert observed == [declared]


def test_execute_trial_binds_relative_output_to_one_absolute_child_path(
    monkeypatch,
    tmp_path,
):
    """A sandbox child must not create a second tree behind stale evidence."""

    from omicsclaw.autoagent.runner import execute_trial
    from omicsclaw.autoagent.search_space import SearchSpace

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    _write_trial_authority_skill(sandbox)
    authority = _capture_trial_authority(sandbox, "sandbox-skill")
    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.chdir(caller)
    requested = Path("outputs") / "trial_0000"
    destination = caller / requested
    observed: dict[str, object] = {}

    class ChildProc:
        returncode = 0

        def __init__(self, args, **kwargs):
            self.args = args
            raw_output = args[args.index("--output") + 1]
            child_output = Path(raw_output)
            if not child_output.is_absolute():
                child_output = Path(kwargs["cwd"]) / child_output
            child_output.mkdir(parents=True, exist_ok=True)
            _write_valid_run_result(child_output, state="fresh-child")
            _write_run_claim(child_output, authority)
            observed["raw_output"] = raw_output
            observed["child_output"] = child_output

        def communicate(self, timeout=None):
            return "", ""

    monkeypatch.setattr(
        "omicsclaw.autoagent.runner.subprocess.Popen",
        ChildProc,
    )

    execution = execute_trial(
        skill_name="sandbox-skill",
        input_path="",
        output_dir=requested,
        params={},
        search_space=SearchSpace(
            skill_name="sandbox-skill",
            method="default",
            tunable=[],
        ),
        project_root=sandbox,
        demo=True,
    )

    canonical = destination.resolve()
    assert execution.success is True
    assert execution.output_dir == str(canonical)
    assert observed == {
        "raw_output": str(canonical),
        "child_output": canonical,
    }
    assert json.loads((canonical / "result.json").read_text())["data"] == {
        "state": "fresh-child"
    }
    assert not (sandbox / requested).exists()


def test_execute_trial_rejects_preexisting_trial_leaf_before_spawn(
    monkeypatch,
    tmp_path,
):
    from omicsclaw.autoagent.runner import execute_trial
    from omicsclaw.autoagent.search_space import SearchSpace

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    _write_trial_authority_skill(sandbox)
    trial_output = tmp_path / "trial_0000"
    trial_output.mkdir()
    (trial_output / "result.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "omicsclaw.autoagent.runner.subprocess.Popen",
        lambda *_args, **_kwargs: pytest.fail(
            "a pre-existing trial leaf must fail before child spawn"
        ),
    )

    execution = execute_trial(
        skill_name="sandbox-skill",
        input_path="",
        output_dir=trial_output,
        params={},
        search_space=SearchSpace(
            skill_name="sandbox-skill",
            method="default",
            tunable=[],
        ),
        project_root=sandbox,
        demo=True,
    )

    assert execution.success is False
    assert execution.exit_code == -1
    assert "already exists" in execution.stderr


def test_execute_trial_requires_child_to_claim_exact_output(monkeypatch, tmp_path):
    from omicsclaw.autoagent.runner import execute_trial
    from omicsclaw.autoagent.search_space import SearchSpace

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    _write_trial_authority_skill(sandbox)
    trial_output = tmp_path / "trial_0000"

    class OutputlessProc:
        returncode = 0

        def __init__(self, args, **_kwargs):
            self.args = args

        def communicate(self, timeout=None):
            return "", ""

    monkeypatch.setattr(
        "omicsclaw.autoagent.runner.subprocess.Popen",
        OutputlessProc,
    )

    execution = execute_trial(
        skill_name="sandbox-skill",
        input_path="",
        output_dir=trial_output,
        params={},
        search_space=SearchSpace(
            skill_name="sandbox-skill",
            method="default",
            tunable=[],
        ),
        project_root=sandbox,
        demo=True,
    )

    assert execution.success is False
    assert execution.exit_code == -1
    assert "owned result.json and matching run claim" in execution.stderr
    assert not trial_output.exists()


@pytest.mark.parametrize(
    ("write_result", "write_claim"),
    [(False, False), (True, False), (False, True)],
)
def test_execute_trial_requires_owned_result_and_matching_claim(
    monkeypatch,
    tmp_path,
    write_result,
    write_claim,
):
    from omicsclaw.autoagent.runner import execute_trial
    from omicsclaw.autoagent.search_space import SearchSpace

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    _write_trial_authority_skill(sandbox)
    authority = _capture_trial_authority(sandbox, "sandbox-skill")
    trial_output = tmp_path / "trial_0000"

    class IncompleteOutputProc:
        returncode = 0

        def __init__(self, args, **_kwargs):
            self.args = args

        def communicate(self, timeout=None):
            trial_output.mkdir()
            if write_result:
                _write_valid_run_result(trial_output)
            if write_claim:
                _write_run_claim(trial_output, authority)
            return "", ""

    monkeypatch.setattr(
        "omicsclaw.autoagent.runner.subprocess.Popen",
        IncompleteOutputProc,
    )

    execution = execute_trial(
        skill_name="sandbox-skill",
        input_path="",
        output_dir=trial_output,
        params={},
        search_space=SearchSpace(
            skill_name="sandbox-skill",
            method="default",
            tunable=[],
        ),
        project_root=sandbox,
        demo=True,
    )

    assert execution.success is False
    assert execution.exit_code == -1
    assert "owned result.json and matching run claim" in execution.stderr


@pytest.mark.parametrize(
    "receipt_fault",
    [
        "invalid-envelope",
        "wrong-owner",
        "wrong-skill",
        "wrong-version",
        "bad-claim-id",
        "bad-claimed-at",
    ],
)
def test_execute_trial_rejects_forged_child_receipt(
    monkeypatch,
    tmp_path,
    receipt_fault,
):
    from omicsclaw.autoagent.runner import execute_trial
    from omicsclaw.autoagent.search_space import SearchSpace

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    _write_trial_authority_skill(sandbox)
    authority = _capture_trial_authority(sandbox, "sandbox-skill")
    trial_output = tmp_path / "trial_0000"

    class ForgedReceiptProc:
        returncode = 0

        def __init__(self, args, **_kwargs):
            self.args = args

        def communicate(self, timeout=None):
            trial_output.mkdir()
            if receipt_fault == "invalid-envelope":
                (trial_output / "result.json").write_text("{}", encoding="utf-8")
                _write_run_claim(trial_output, authority)
            elif receipt_fault == "wrong-owner":
                _write_valid_run_result(trial_output)
                _write_run_claim(
                    trial_output,
                    authority,
                    skill_name="other-skill",
                )
            elif receipt_fault == "wrong-skill":
                _write_valid_run_result(trial_output, skill_name="other-skill")
                _write_run_claim(trial_output, authority)
            elif receipt_fault == "wrong-version":
                _write_valid_run_result(trial_output, version="9.9.9")
                _write_run_claim(trial_output, authority)
            elif receipt_fault == "bad-claim-id":
                _write_valid_run_result(trial_output)
                _write_run_claim(
                    trial_output,
                    authority,
                    claim_id="NOT-A-UUID",
                )
            else:
                _write_valid_run_result(trial_output)
                _write_run_claim(
                    trial_output,
                    authority,
                    claimed_at="yesterday",
                )
            return "", ""

    monkeypatch.setattr(
        "omicsclaw.autoagent.runner.subprocess.Popen",
        ForgedReceiptProc,
    )

    execution = execute_trial(
        skill_name="sandbox-skill",
        input_path="",
        output_dir=trial_output,
        params={},
        search_space=SearchSpace(
            skill_name="sandbox-skill",
            method="default",
            tunable=[],
        ),
        project_root=sandbox,
        demo=True,
    )

    assert execution.success is False
    assert execution.exit_code == -1
    assert "owned result.json and matching run claim" in execution.stderr


def test_execute_trial_cancellation_terminates_active_process(monkeypatch, tmp_path):
    from omicsclaw.autoagent.errors import OptimizationCancelled
    from omicsclaw.autoagent import runner
    from omicsclaw.autoagent.runner import execute_trial
    from omicsclaw.autoagent.search_space import ParameterDef, SearchSpace

    cancel_event = threading.Event()
    killed_signals: list[int] = []

    class FakeProc:
        def __init__(self, *args, **kwargs):
            self.args = args[0]
            self.pid = 4321
            self.returncode: int | None = None
            self.communicate_calls = 0

        def communicate(self, timeout=None):
            if self.returncode is not None:
                return ("", "")
            self.communicate_calls += 1
            cancel_event.set()
            raise subprocess.TimeoutExpired(self.args, timeout)

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = -15
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    fake_proc = FakeProc(["python", "omicsclaw.py"])

    monkeypatch.setattr(
        "omicsclaw.autoagent.runner.subprocess.Popen",
        lambda *args, **kwargs: fake_proc,
    )

    if hasattr(runner.os, "killpg"):
        monkeypatch.setattr(
            "omicsclaw.autoagent.runner.os.killpg",
            lambda _pid, sig: killed_signals.append(sig) or setattr(fake_proc, "returncode", -sig),
        )

    backend_root = tmp_path / "backend"
    backend_root.mkdir()
    _write_trial_authority_skill(backend_root)
    search_space = SearchSpace(
        skill_name="sandbox-skill",
        method="method",
        tunable=[
            ParameterDef(
                name="alpha",
                param_type="float",
                default=1.0,
                low=0.1,
                high=5.0,
                cli_flag="--alpha",
            )
        ],
    )

    with pytest.raises(OptimizationCancelled, match="cancelled"):
        execute_trial(
            skill_name="sandbox-skill",
            input_path="",
            output_dir=tmp_path / "trial_0000",
            params={"alpha": 2.0},
            search_space=search_space,
            project_root=backend_root,
            cancel_event=cancel_event,
        )

    assert fake_proc.communicate_calls >= 1
    if killed_signals:
        assert killed_signals[0] in {15, 9}
    else:
        assert fake_proc.returncode in {-15, -9}


def test_execute_trial_pins_import_path_and_disables_candidate_bytecode(
    monkeypatch,
    tmp_path,
    request,
):
    from omicsclaw.autoagent.runner import execute_trial
    from omicsclaw.autoagent.search_space import SearchSpace

    backend_root = tmp_path / "backend"
    backend_root.mkdir()
    _write_trial_authority_skill(backend_root)
    (backend_root / "bytecode_probe.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    authority = _capture_trial_authority(backend_root, "sandbox-skill")
    runtime_modules = tmp_path / "runtime-modules"
    runtime_modules.mkdir()
    pythonpath_loop = tmp_path / "pythonpath-loop"
    pythonpath_loop.symlink_to(pythonpath_loop)
    request.addfinalizer(lambda: pythonpath_loop.unlink(missing_ok=True))
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            [
                str(backend_root),
                ".",
                "",
                str(pythonpath_loop),
                str(runtime_modules),
                str(backend_root),
            ]
        ),
    )
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "0")
    observed: dict[str, object] = {}
    real_popen = subprocess.Popen

    class CompletedProc:
        returncode = 0

        def __init__(self, args, **kwargs):
            self.args = args
            self.output_dir = Path(args[args.index("--output") + 1])
            observed["env"] = kwargs["env"]
            probe = real_popen(
                [args[0], "-c", "import bytecode_probe"],
                cwd=kwargs["cwd"],
                env=kwargs["env"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            probe.communicate()
            observed["probe_returncode"] = probe.returncode
            observed["pycache_exists"] = (backend_root / "__pycache__").exists()

        def communicate(self, timeout=None):
            self.output_dir.mkdir(parents=True)
            _write_valid_run_result(self.output_dir)
            _write_run_claim(self.output_dir, authority)
            return "", ""

    monkeypatch.setattr(
        "omicsclaw.autoagent.runner.subprocess.Popen",
        CompletedProc,
    )
    search_space = SearchSpace(
        skill_name="sandbox-skill",
        method="method",
        tunable=[],
    )

    result = execute_trial(
        skill_name="sandbox-skill",
        input_path="",
        output_dir=tmp_path / "trial_0000",
        params={},
        search_space=search_space,
        project_root=backend_root,
        demo=True,
    )

    assert result.success is True
    env = observed["env"]
    assert isinstance(env, dict)
    assert env["PYTHONPATH"] == str(backend_root)
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert observed["probe_returncode"] == 0
    assert observed["pycache_exists"] is False


def test_params_to_cli_args_ignores_unknown_params(caplog):
    from omicsclaw.autoagent.runner import _params_to_cli_args
    from omicsclaw.autoagent.search_space import ParameterDef, SearchSpace

    search_space = SearchSpace(
        skill_name="test-skill",
        method="method",
        tunable=[
            ParameterDef(
                name="alpha",
                param_type="float",
                default=1.0,
                low=0.1,
                high=5.0,
                cli_flag="--alpha",
            )
        ],
    )

    with caplog.at_level(logging.WARNING):
        args = _params_to_cli_args(
            {"alpha": 2.0, "hallucinated_knob": 9},
            search_space,
        )

    assert args == ["--alpha", "2.0"]
    assert "Ignoring unknown trial param hallucinated_knob" in caplog.text


def test_build_reproduce_command_quotes_values_and_omits_false_boolean_flags():
    from omicsclaw.autoagent.reproduce import build_reproduce_command

    command = build_reproduce_command(
        skill_name="sc-batch-integration",
        method="harmony",
        input_path="/tmp/demo data/input file.h5ad",
        params={
            "no_gpu": False,
            "refine": True,
            "batch_key": "sample group",
            "reference_cat": ["T cell", "B cell"],
        },
        fixed_params={
            "n_epochs": 10,
            "use_gpu": False,
        },
    )

    assert "--refine" in command
    assert "--no-gpu" not in command
    assert "--use-gpu" not in command
    assert "'/tmp/demo data/input file.h5ad'" in command
    assert "--batch-key 'sample group'" in command
    assert "--reference-cat 'T cell' 'B cell'" in command
    assert "--n-epochs 10" in command


def test_ask_llm_returns_real_error_message(monkeypatch, tmp_path):
    from omicsclaw.autoagent.evaluator import Evaluator
    from omicsclaw.autoagent.metrics_registry import MetricDef
    from omicsclaw.autoagent.optimization_loop import OptimizationLoop
    from omicsclaw.autoagent.search_space import ParameterDef, SearchSpace

    metrics = {
        "score": MetricDef(
            source="result.json:summary.score",
            direction="maximize",
        )
    }
    search_space = SearchSpace(
        skill_name="test-skill",
        method="method",
        tunable=[
            ParameterDef(
                name="alpha",
                param_type="float",
                default=1.0,
                low=0.1,
                high=5.0,
                cli_flag="--alpha",
            )
        ],
    )
    loop = OptimizationLoop(
        skill_name="test-skill",
        method="method",
        input_path="",
        output_root=tmp_path / "optimize-llm-error",
        search_space=search_space,
        evaluator=Evaluator(metrics),
        metrics=metrics,
        max_trials=2,
    )

    def raise_key_error(_directive: str) -> str:
        raise RuntimeError("No LLM API key found")

    monkeypatch.setattr(loop, "_call_llm", raise_key_error)

    suggestion, error = loop._ask_llm("test directive")

    assert suggestion is None
    assert error == "LLM call failed: No LLM API key found"


def test_optimization_trial_without_verified_authority_cannot_be_candidate(
    monkeypatch,
    tmp_path,
):
    from omicsclaw.autoagent.evaluator import Evaluator
    from omicsclaw.autoagent.metrics_registry import MetricDef
    from omicsclaw.autoagent.optimization_loop import OptimizationLoop
    from omicsclaw.autoagent.runner import TrialExecution
    from omicsclaw.autoagent.search_space import SearchSpace

    metrics = {
        "score": MetricDef(
            source="result.json:summary.score",
            direction="maximize",
        )
    }
    loop = OptimizationLoop(
        skill_name="test-skill",
        method="method",
        input_path="",
        output_root=tmp_path / "optimize-unbound",
        search_space=SearchSpace(
            skill_name="test-skill",
            method="method",
            tunable=[],
        ),
        evaluator=Evaluator(metrics, skill_name="test-skill"),
        metrics=metrics,
    )
    monkeypatch.setattr(
        "omicsclaw.autoagent.optimization_loop.execute_trial",
        lambda **_kwargs: TrialExecution(
            success=True,
            output_dir=str(tmp_path / "trial_0000"),
            duration_seconds=1.0,
        ),
    )
    monkeypatch.setattr(
        loop.evaluator,
        "evaluate",
        lambda *_args, **_kwargs: pytest.fail(
            "unbound execution must not reach scoring"
        ),
    )

    trial = loop._run_trial(trial_id=0, params={})

    assert trial.status == "crash"
    assert trial.composite_score == float("-inf")
    assert "authority" in trial.error_output.lower()


def test_optimization_failed_execution_does_not_precreate_trial_leaf(
    monkeypatch,
    tmp_path,
):
    from omicsclaw.autoagent.evaluator import Evaluator
    from omicsclaw.autoagent.metrics_registry import MetricDef
    from omicsclaw.autoagent.optimization_loop import OptimizationLoop
    from omicsclaw.autoagent.runner import TrialExecution
    from omicsclaw.autoagent.search_space import SearchSpace

    metrics = {
        "score": MetricDef(
            source="result.json:summary.score",
            direction="maximize",
        )
    }
    loop = OptimizationLoop(
        skill_name="test-skill",
        method="method",
        input_path="",
        output_root=tmp_path / "optimize-failed",
        search_space=SearchSpace(
            skill_name="test-skill",
            method="method",
            tunable=[],
        ),
        evaluator=Evaluator(metrics, skill_name="test-skill"),
        metrics=metrics,
    )
    trial_leaf = loop.output_root / "trial_0000"
    authority = _trial_authority()
    monkeypatch.setattr(
        "omicsclaw.autoagent.optimization_loop.execute_trial",
        lambda **_kwargs: TrialExecution(
            success=False,
            output_dir=str(trial_leaf),
            duration_seconds=1.0,
            exit_code=1,
            stderr="child failed",
            authority=authority,
        ),
    )

    record = loop._run_trial(trial_id=0, params={})

    assert record.status == "crash"
    assert record.error_output == "child failed"
    assert record.authority == authority
    assert not trial_leaf.exists()


def test_optimization_session_root_cannot_be_claimed_twice(tmp_path):
    from omicsclaw.autoagent.evaluator import Evaluator
    from omicsclaw.autoagent.metrics_registry import MetricDef
    from omicsclaw.autoagent.optimization_loop import OptimizationLoop
    from omicsclaw.autoagent.search_space import SearchSpace

    metrics = {
        "score": MetricDef(
            source="result.json:summary.score",
            direction="maximize",
        )
    }
    kwargs = {
        "skill_name": "test-skill",
        "method": "method",
        "input_path": "",
        "output_root": tmp_path / "one-session",
        "search_space": SearchSpace(
            skill_name="test-skill",
            method="method",
            tunable=[],
        ),
        "evaluator": Evaluator(metrics, skill_name="test-skill"),
        "metrics": metrics,
    }

    first = OptimizationLoop(**kwargs)

    assert first.output_root.is_absolute()
    with pytest.raises(ValueError, match="already exists"):
        OptimizationLoop(**kwargs)


def test_run_trial_preserves_evaluator_diagnostics(monkeypatch, tmp_path):
    from omicsclaw.autoagent.evaluator import EvaluationResult, Evaluator
    from omicsclaw.autoagent.metrics_registry import MetricDef
    from omicsclaw.autoagent.optimization_loop import OptimizationLoop
    from omicsclaw.autoagent.runner import TrialExecution
    from omicsclaw.autoagent.search_space import ParameterDef, SearchSpace

    metrics = {
        "score": MetricDef(
            source="result.json:summary.score",
            direction="maximize",
        ),
        "batch_asw": MetricDef(
            source="result.json:summary.batch_asw",
            direction="maximize",
        ),
    }
    search_space = SearchSpace(
        skill_name="test-skill",
        method="method",
        tunable=[
            ParameterDef(
                name="alpha",
                param_type="float",
                default=1.0,
                low=0.1,
                high=5.0,
                cli_flag="--alpha",
            )
        ],
    )
    loop = OptimizationLoop(
        skill_name="test-skill",
        method="method",
        input_path="",
        output_root=tmp_path / "optimize-eval-diagnostics",
        search_space=search_space,
        evaluator=Evaluator(metrics),
        metrics=metrics,
        max_trials=2,
    )

    monkeypatch.setattr(
        "omicsclaw.autoagent.optimization_loop.execute_trial",
        lambda **_kwargs: TrialExecution(
            success=True,
            output_dir=str(tmp_path / "trial_0000"),
            duration_seconds=1.25,
            authority=_trial_authority(),
        ),
    )
    monkeypatch.setattr(
        loop.evaluator,
        "evaluate",
        lambda *_args, **_kwargs: EvaluationResult(
            composite_score=float("-inf"),
            raw_metrics={},
            success=False,
            missing_metrics=["score", "batch_asw"],
        ),
    )

    trial = loop._run_trial(
        trial_id=0,
        params={"alpha": 1.0},
        description="baseline",
    )

    assert trial.status == "pending"
    assert trial.composite_score == float("-inf")
    assert trial.evaluation_success is False
    assert trial.missing_metrics == ["score", "batch_asw"]
    assert trial.authority == _trial_authority()


def test_call_llm_reuses_active_provider_runtime(monkeypatch, tmp_path):
    from omicsclaw.autoagent.evaluator import Evaluator
    from omicsclaw.autoagent.metrics_registry import MetricDef
    from omicsclaw.autoagent.optimization_loop import OptimizationLoop
    from omicsclaw.autoagent.search_space import ParameterDef, SearchSpace
    from omicsclaw.providers.runtime import (
        clear_active_provider_runtime,
        set_active_provider_runtime,
    )

    metrics = {
        "score": MetricDef(
            source="result.json:summary.score",
            direction="maximize",
        )
    }
    search_space = SearchSpace(
        skill_name="test-skill",
        method="method",
        tunable=[
            ParameterDef(
                name="alpha",
                param_type="float",
                default=1.0,
                low=0.1,
                high=5.0,
                cli_flag="--alpha",
            )
        ],
    )
    loop = OptimizationLoop(
        skill_name="test-skill",
        method="method",
        input_path="",
        output_root=tmp_path / "optimize-active-runtime",
        search_space=search_space,
        evaluator=Evaluator(metrics),
        metrics=metrics,
        max_trials=2,
    )

    captured: dict[str, str | None] = {}

    class FakeOpenAI:
        def __init__(self, *, api_key: str, base_url: str | None = None, **_kwargs):
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            self.chat = self
            self.completions = self

        def create(self, **kwargs):
            captured["model"] = kwargs["model"]
            return type(
                "FakeResponse",
                (),
                {
                    "choices": [
                        type(
                            "FakeChoice",
                            (),
                            {
                                "message": type(
                                    "FakeMessage",
                                    (),
                                    {"content": '{"alpha": 2.0}'},
                                )()
                            },
                        )()
                    ]
                },
            )()

    monkeypatch.setattr("omicsclaw.autoagent.llm_client.OpenAI", FakeOpenAI)
    set_active_provider_runtime(
        provider="deepseek",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        api_key="runtime-secret",
    )

    try:
        response = loop._call_llm("Suggest params")
    finally:
        clear_active_provider_runtime()

    assert response == '{"alpha": 2.0}'
    assert captured == {
        "api_key": "runtime-secret",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
    }


def test_run_trial_records_stderr_for_crashed_trials(monkeypatch, tmp_path):
    from omicsclaw.autoagent.evaluator import Evaluator
    from omicsclaw.autoagent.metrics_registry import MetricDef
    from omicsclaw.autoagent.optimization_loop import OptimizationLoop
    from omicsclaw.autoagent.runner import TrialExecution
    from omicsclaw.autoagent.search_space import ParameterDef, SearchSpace

    metrics = {
        "score": MetricDef(
            source="result.json:summary.score",
            direction="maximize",
        )
    }
    search_space = SearchSpace(
        skill_name="test-skill",
        method="method",
        tunable=[
            ParameterDef(
                name="alpha",
                param_type="float",
                default=1.0,
                low=0.1,
                high=5.0,
                cli_flag="--alpha",
            )
        ],
    )
    loop = OptimizationLoop(
        skill_name="test-skill",
        method="method",
        input_path="",
        output_root=tmp_path / "optimize-stderr",
        search_space=search_space,
        evaluator=Evaluator(metrics),
        metrics=metrics,
        max_trials=2,
    )

    monkeypatch.setattr(
        "omicsclaw.autoagent.optimization_loop.execute_trial",
        lambda **kwargs: TrialExecution(
            success=False,
            output_dir=str(tmp_path / "trial_0001"),
            duration_seconds=0.12,
            exit_code=2,
            stdout="stdout text\n",
            stderr="ValueError: bad param\n",
        ),
    )

    trial = loop._run_trial(
        trial_id=1,
        params={"alpha": 9.0},
        description="boom",
    )

    assert trial.status == "crash"
    assert trial.error_output == "ValueError: bad param"


def test_validate_and_clamp_params_parses_bool_strings_rejects_invalid_categories_and_drops_unknown_params(
    tmp_path,
    caplog,
):
    from omicsclaw.autoagent.evaluator import Evaluator
    from omicsclaw.autoagent.metrics_registry import MetricDef
    from omicsclaw.autoagent.optimization_loop import OptimizationLoop
    from omicsclaw.autoagent.search_space import ParameterDef, SearchSpace

    metrics = {
        "score": MetricDef(
            source="result.json:summary.score",
            direction="maximize",
        )
    }
    search_space = SearchSpace(
        skill_name="test-skill",
        method="method",
        tunable=[
            ParameterDef(
                name="use_gpu",
                param_type="bool",
                default=True,
                choices=[True, False],
                cli_flag="--use-gpu",
            ),
            ParameterDef(
                name="mode",
                param_type="categorical",
                default="safe",
                choices=["safe", "fast"],
                cli_flag="--mode",
            ),
        ],
    )
    loop = OptimizationLoop(
        skill_name="test-skill",
        method="method",
        input_path="",
        output_root=tmp_path / "optimize-params",
        search_space=search_space,
        evaluator=Evaluator(metrics),
        metrics=metrics,
        max_trials=2,
    )

    with caplog.at_level(logging.WARNING):
        params = loop._validate_and_clamp_params({
            "use_gpu": "false",
            "mode": "turbo",
            "hallucinated_knob": 123,
        })

    assert params == {"use_gpu": False, "mode": "safe"}
    assert "Discarding unknown LLM-suggested params" in caplog.text
    assert "hallucinated_knob" in caplog.text


def test_optimization_loop_fails_when_llm_suggests_only_unknown_params(monkeypatch, tmp_path):
    from omicsclaw.autoagent.evaluator import Evaluator
    from omicsclaw.autoagent.experiment_ledger import TrialRecord
    from omicsclaw.autoagent.metrics_registry import MetricDef
    from omicsclaw.autoagent.optimization_loop import OptimizationLoop
    from omicsclaw.autoagent.search_space import ParameterDef, SearchSpace

    metrics = {
        "score": MetricDef(
            source="result.json:summary.score",
            direction="maximize",
        )
    }
    search_space = SearchSpace(
        skill_name="test-skill",
        method="method",
        tunable=[
            ParameterDef(
                name="alpha",
                param_type="float",
                default=1.0,
                low=0.1,
                high=5.0,
                cli_flag="--alpha",
            )
        ],
    )
    loop = OptimizationLoop(
        skill_name="test-skill",
        method="method",
        input_path="",
        output_root=tmp_path / "optimize-unknown-only",
        search_space=search_space,
        evaluator=Evaluator(metrics),
        metrics=metrics,
        max_trials=2,
    )

    def fake_run_trial(
        trial_id: int,
        params: dict[str, object],
        description: str = "",
        on_event=None,
    ) -> TrialRecord:
        assert trial_id == 0
        return TrialRecord(
            trial_id=trial_id,
            params=params,
            composite_score=1.0,
            raw_metrics={"score": 1.0},
            status="pending",
            reasoning=description,
        )

    monkeypatch.setattr(loop, "_run_trial", fake_run_trial)
    monkeypatch.setattr(
        loop,
        "_ask_llm",
        lambda directive: {
            "params": {"hallucinated_knob": 9},
            "reasoning": "try a made-up parameter",
        },
    )

    events: list[tuple[str, dict[str, object]]] = []
    result = loop.run(on_event=lambda event_type, data: events.append((event_type, data)))

    assert result.success is False
    assert (
        result.error_message
        == "LLM suggestion contained no valid tunable parameters for test-skill/method. "
        "Allowed params: alpha."
    )
    assert [event_type for event_type, _data in events].count("trial_start") == 0
    assert len(result.ledger.all_trials()) == 1
    assert result.ledger.all_trials()[0].params == {"alpha": 1.0}


@pytest.mark.asyncio
async def test_optimize_runtime_emit_is_safe_from_worker_threads():
    from omicsclaw.autoagent.api import OptimizeSessionRuntime

    loop = asyncio.get_running_loop()
    runtime = OptimizeSessionRuntime(session_id="a" * 32, loop=loop)
    producer_errors: list[Exception] = []

    def producer() -> None:
        try:
            runtime.emit("trial_start", {"trial_id": 1, "params": {"alpha": 2.0}})
            runtime.emit("progress", {"completed": 1, "total": 3})
            runtime.mark_done({"success": True, "best_params": {"alpha": 2.0}})
        except Exception as exc:  # pragma: no cover - defensive assertion aid
            producer_errors.append(exc)

    thread = threading.Thread(target=producer, name="optimize-test-producer")
    thread.start()

    events: list[dict[str, object]] = []
    while True:
        event = await asyncio.wait_for(runtime.queue.get(), timeout=1)
        events.append(event)
        if event["type"] == "_finished":
            break

    thread.join(timeout=1)

    assert producer_errors == []
    assert [event["type"] for event in events] == [
        "trial_start",
        "progress",
        "done",
        "_finished",
    ]
    assert runtime.snapshot() == ("done", {"success": True, "best_params": {"alpha": 2.0}}, None)


@pytest.mark.asyncio
async def test_optimize_abort_preserves_session_and_reports_cancelled(
    monkeypatch,
    bound_autoagent_repository,
    tmp_path,
):
    pytest.importorskip("fastapi")

    from fastapi import HTTPException
    from omicsclaw.autoagent import api
    from omicsclaw.autoagent.process_owner import GovernedWorkerOutcome

    api._sessions.clear()
    session_id = "b" * 32
    started = asyncio.Event()
    cancellation_seen = asyncio.Event()

    class CancelledOwner:
        def __init__(self, **kwargs):
            self.execution_reference_type = kwargs["execution_reference_type"]
            self.execution_reference = kwargs["execution_reference"]
            self.ipc_root = kwargs["ipc_root"]
            self.process_tree_confirmed_empty = False

        def request_cancel(self) -> None:
            cancellation_seen.set()

        async def run(self, *, on_event=None):
            started.set()
            await cancellation_seen.wait()
            self.process_tree_confirmed_empty = True
            return GovernedWorkerOutcome("cancelled", error_code="cancelled")

    _patch_governed_start(monkeypatch, api, CancelledOwner)

    response = await api.optimize_start(
        api.OptimizeRequest(
            session_id=session_id,
            skill="sc-batch-integration",
            method="harmony",
            cwd=str(tmp_path),
        )
    )

    body_iterator = response.body_iterator
    try:
        first_chunk = await anext(body_iterator)
        if isinstance(first_chunk, bytes):
            first_chunk = first_chunk.decode("utf-8")
        assert session_id in str(first_chunk)

        await asyncio.wait_for(started.wait(), timeout=1)

        running_status = await api.optimize_status(session_id)
        assert running_status.status == "running"

        abort_payload = await api.optimize_abort(session_id)
        assert abort_payload == {"status": "cancelling", "session_id": session_id}

        cancelled_status = None
        for _ in range(100):
            candidate = await api.optimize_status(session_id)
            if candidate.status == "cancelled":
                cancelled_status = candidate
                break
            await asyncio.sleep(0.01)

        assert cancelled_status is not None
        assert cancelled_status.status == "cancelled"
        assert cancelled_status.error == "Optimization cancelled"
        assert api._sessions[session_id].cancel_event.is_set() is True

        with pytest.raises(HTTPException) as excinfo:
            await api.optimize_results(session_id)
        assert excinfo.value.status_code == 409
    finally:
        await body_iterator.aclose()
        api._sessions.clear()


@pytest.mark.asyncio
async def test_optimize_status_and_results_reap_only_expired_cache(
    monkeypatch,
    bound_autoagent_repository,
    tmp_path,
):
    pytest.importorskip("fastapi")

    from omicsclaw.autoagent import api

    loop = asyncio.get_running_loop()
    api._sessions.clear()
    session_id = "c" * 32
    output_dir = str(tmp_path / "durable-result")
    result = {
        "success": True,
        "mode": "harness_evolution",
        "skill": "sc-batch-integration",
        "method": "harmony",
        "evolution_goal": "",
        "output_dir": output_dir,
        "promotion": {"status": "skipped"},
    }

    runtime = api.OptimizeSessionRuntime(session_id=session_id, loop=loop)
    runtime.status = "done"
    runtime.result = result
    runtime.finished_at = 1.0
    api._sessions[session_id] = runtime
    bound_autoagent_repository.accept_autoagent_session(
        session_id=session_id,
        cwd=str(tmp_path),
        output_dir=output_dir,
        skill="sc-batch-integration",
        method="harmony",
        evolution_goal="",
        creation_receipt_sha256=None,
        execution_reference_type=_GOVERNED_REFERENCE_TYPE,
        execution_reference=_GOVERNED_REFERENCE,
    )
    bound_autoagent_repository.confirm_autoagent_owner_stopped(session_id)
    bound_autoagent_repository.complete_autoagent_session_success(
        session_id,
        result,
    )

    monkeypatch.setattr(
        api.time,
        "monotonic",
        lambda: 1.0 + api._SESSION_TTL_SECONDS + 10,
    )

    try:
        status = await api.optimize_status(session_id)
        assert status.status == "done"
        assert status.result == result
        assert session_id not in api._sessions
        assert await api.optimize_results(session_id) == result
    finally:
        api._sessions.clear()


@pytest.mark.asyncio
async def test_optimize_start_streams_governed_worker_events(
    monkeypatch,
    bound_autoagent_repository,
    tmp_path,
):
    pytest.importorskip("fastapi")

    from omicsclaw.autoagent import api
    from omicsclaw.autoagent.process_owner import GovernedWorkerOutcome

    api._sessions.clear()
    session_id = "d" * 32
    captured: dict[str, object] = {}

    class SuccessfulOwner:
        def __init__(self, **kwargs):
            captured.update(kwargs["request"])
            self.execution_reference_type = kwargs["execution_reference_type"]
            self.execution_reference = kwargs["execution_reference"]
            self.ipc_root = kwargs["ipc_root"]
            self.process_tree_confirmed_empty = False

        def request_cancel(self) -> None:
            return None

        async def run(self, *, on_event=None):
            assert on_event is not None
            on_event("trial_start", {"trial_id": 0, "params": {"alpha": 1.0}})
            on_event(
                "progress",
                {"completed": 1, "total": 2, "best_score": 0.75},
            )
            # A producer terminal is provisional and must not reach the wire.
            on_event("done", {"unverified": True})
            self.process_tree_confirmed_empty = True
            return GovernedWorkerOutcome(
                "done",
                result=_governed_success_result(captured),
            )

    _patch_governed_start(monkeypatch, api, SuccessfulOwner)
    output_dir = tmp_path / "output" / "governed-stream"

    response = await api.optimize_start(
        api.OptimizeRequest(
            session_id=session_id,
            skill="sc-batch-integration",
            method="harmony",
            cwd=str(tmp_path),
            output_dir=str(output_dir),
            provider_id="deepseek",
            llm_model="deepseek-chat",
        )
    )

    body_iterator = response.body_iterator
    chunks: list[str] = []
    try:
        async for chunk in body_iterator:
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8")
            chunks.append(str(chunk))
            if "event: done" in str(chunk):
                break

        event_types: list[str] = []
        event_payloads: dict[str, dict[str, object]] = {}
        for raw_chunk in chunks:
            event_type: str | None = None
            data_line: str | None = None
            for line in raw_chunk.splitlines():
                if line.startswith("event: "):
                    event_type = line[7:].strip()
                elif line.startswith("data: "):
                    data_line = line[6:]
            if not event_type or data_line is None:
                continue
            payload = json.loads(data_line) if data_line else {}
            event_types.append(event_type)
            if isinstance(payload, dict):
                event_payloads[event_type] = payload

        assert event_types == ["status", "trial_start", "progress", "done"]
        assert event_payloads["trial_start"]["trial_id"] == 0
        assert event_payloads["progress"]["best_score"] == 0.75
        assert event_payloads["done"] == {
            "session_id": session_id,
            "status": "done",
        }
        assert captured["cwd"] == str(tmp_path)
        assert captured["output_dir"] == str(output_dir)
        assert captured["llm_provider"] == "deepseek"
        assert captured["llm_model"] == "deepseek-chat"

        final_status = None
        for _ in range(100):
            candidate = await api.optimize_status(session_id)
            if candidate.status == "done":
                final_status = candidate
                break
            await asyncio.sleep(0.01)

        assert final_status is not None
        assert final_status.result is not None
        assert final_status.result["best_params"] == {"alpha": 1.0}
    finally:
        await body_iterator.aclose()
        api._sessions.clear()


def test_optimization_loop_reports_llm_failure_without_done(monkeypatch, tmp_path):
    from omicsclaw.autoagent.evaluator import Evaluator
    from omicsclaw.autoagent.experiment_ledger import TrialRecord
    from omicsclaw.autoagent.metrics_registry import MetricDef
    from omicsclaw.autoagent.optimization_loop import OptimizationLoop
    from omicsclaw.autoagent.search_space import ParameterDef, SearchSpace

    metrics = {
        "score": MetricDef(
            source="result.json:summary.score",
            direction="maximize",
        )
    }
    search_space = SearchSpace(
        skill_name="test-skill",
        method="method",
        tunable=[
            ParameterDef(
                name="alpha",
                param_type="float",
                default=1.0,
                low=0.1,
                high=5.0,
                cli_flag="--alpha",
            )
        ],
    )
    loop = OptimizationLoop(
        skill_name="test-skill",
        method="method",
        input_path="",
        output_root=tmp_path / "optimize-no-suggestion",
        search_space=search_space,
        evaluator=Evaluator(metrics),
        metrics=metrics,
        max_trials=4,
    )

    def fake_run_trial(
        trial_id: int,
        params: dict[str, object],
        description: str = "",
        on_event=None,
    ) -> TrialRecord:
        assert trial_id == 0
        return TrialRecord(
            trial_id=trial_id,
            params=params,
            composite_score=1.0,
            raw_metrics={"score": 1.0},
            status="pending",
            reasoning=description,
        )

    monkeypatch.setattr(loop, "_run_trial", fake_run_trial)
    monkeypatch.setattr(loop, "_ask_llm", lambda directive: None)

    event_types: list[str] = []
    result = loop.run(on_event=lambda event_type, _data: event_types.append(event_type))

    assert result.success is False
    assert result.error_message == "LLM returned no suggestion"
    assert result.total_trials == 1
    assert "done" not in event_types


def test_optimization_loop_emits_missing_metrics_in_trial_complete_and_done(monkeypatch, tmp_path):
    from omicsclaw.autoagent.evaluator import Evaluator
    from omicsclaw.autoagent.experiment_ledger import TrialRecord
    from omicsclaw.autoagent.metrics_registry import MetricDef
    from omicsclaw.autoagent.optimization_loop import OptimizationLoop
    from omicsclaw.autoagent.search_space import ParameterDef, SearchSpace

    metrics = {
        "score": MetricDef(
            source="result.json:summary.score",
            direction="maximize",
        )
    }
    search_space = SearchSpace(
        skill_name="test-skill",
        method="method",
        tunable=[
            ParameterDef(
                name="alpha",
                param_type="float",
                default=1.0,
                low=0.1,
                high=5.0,
                cli_flag="--alpha",
            )
        ],
    )
    loop = OptimizationLoop(
        skill_name="test-skill",
        method="method",
        input_path="",
        output_root=tmp_path / "optimize-missing-metrics-events",
        search_space=search_space,
        evaluator=Evaluator(metrics),
        metrics=metrics,
        max_trials=1,
    )

    def fake_run_trial(
        trial_id: int,
        params: dict[str, object],
        description: str = "",
        on_event=None,
    ) -> TrialRecord:
        assert trial_id == 0
        return TrialRecord(
            trial_id=trial_id,
            params=params,
            composite_score=float("-inf"),
            raw_metrics={},
            status="pending",
            reasoning=description,
            evaluation_success=False,
            missing_metrics=["score"],
        )

    monkeypatch.setattr(loop, "_run_trial", fake_run_trial)

    events: list[tuple[str, dict[str, object]]] = []
    result = loop.run(on_event=lambda event_type, data: events.append((event_type, data)))

    # A baseline with -inf score means metrics extraction failed entirely.
    # The loop now stops immediately with success=False rather than
    # continuing with meaningless comparisons.
    assert result.success is False
    assert "non-finite baseline" in (result.error_message or "")

    trial_complete_payload = next(
        data for event_type, data in events if event_type == "trial_complete"
    )
    assert trial_complete_payload["evaluation_success"] is False
    assert trial_complete_payload["missing_metrics"] == ["score"]

    # An error event is emitted instead of a done event.
    error_payload = next(data for event_type, data in events if event_type == "error")
    assert "non-finite baseline" in error_payload["message"]


def test_optimization_loop_stops_after_three_consecutive_crashes_without_done(monkeypatch, tmp_path):
    from omicsclaw.autoagent.evaluator import Evaluator
    from omicsclaw.autoagent.experiment_ledger import TrialRecord
    from omicsclaw.autoagent.metrics_registry import MetricDef
    from omicsclaw.autoagent.optimization_loop import OptimizationLoop
    from omicsclaw.autoagent.search_space import ParameterDef, SearchSpace

    metrics = {
        "score": MetricDef(
            source="result.json:summary.score",
            direction="maximize",
        )
    }
    search_space = SearchSpace(
        skill_name="test-skill",
        method="method",
        tunable=[
            ParameterDef(
                name="alpha",
                param_type="float",
                default=1.0,
                low=0.1,
                high=5.0,
                cli_flag="--alpha",
            )
        ],
    )
    loop = OptimizationLoop(
        skill_name="test-skill",
        method="method",
        input_path="",
        output_root=tmp_path / "optimize-crashes",
        search_space=search_space,
        evaluator=Evaluator(metrics),
        metrics=metrics,
        max_trials=5,
    )

    suggestions = iter([
        {"params": {"alpha": 1.1}, "reasoning": "first crash"},
        {"params": {"alpha": 1.2}, "reasoning": "second crash"},
        {"params": {"alpha": 1.3}, "reasoning": "third crash"},
    ])

    def fake_run_trial(
        trial_id: int,
        params: dict[str, object],
        description: str = "",
        on_event=None,
    ) -> TrialRecord:
        if trial_id == 0:
            return TrialRecord(
                trial_id=trial_id,
                params=params,
                composite_score=1.0,
                raw_metrics={"score": 1.0},
                status="pending",
                reasoning="baseline",
            )
        return TrialRecord(
            trial_id=trial_id,
            params=params,
            composite_score=float("-inf"),
            raw_metrics={},
            status="crash",
            reasoning=description,
        )

    monkeypatch.setattr(loop, "_run_trial", fake_run_trial)
    monkeypatch.setattr(loop, "_ask_llm", lambda directive: next(suggestions))

    events: list[tuple[str, dict[str, object]]] = []
    result = loop.run(on_event=lambda event_type, data: events.append((event_type, data)))

    assert result.success is False
    assert result.error_message == "3 consecutive crashes — stopping."
    assert result.total_trials == 4
    assert [event_type for event_type, _data in events].count("done") == 0
    crash_statuses = [
        str(data["status"])
        for event_type, data in events
        if event_type == "trial_complete" and int(data["trial_id"]) > 0
    ]
    assert crash_statuses == ["crash", "crash", "crash"]


def test_optimization_loop_progress_tracks_completed_trials(monkeypatch, tmp_path):
    from omicsclaw.autoagent.evaluator import Evaluator
    from omicsclaw.autoagent.experiment_ledger import TrialRecord
    from omicsclaw.autoagent.metrics_registry import MetricDef
    from omicsclaw.autoagent.optimization_loop import OptimizationLoop
    from omicsclaw.autoagent.search_space import ParameterDef, SearchSpace

    metrics = {
        "score": MetricDef(
            source="result.json:summary.score",
            direction="maximize",
        )
    }
    search_space = SearchSpace(
        skill_name="test-skill",
        method="method",
        tunable=[
            ParameterDef(
                name="alpha",
                param_type="float",
                default=1.0,
                low=0.1,
                high=5.0,
                cli_flag="--alpha",
            )
        ],
    )
    loop = OptimizationLoop(
        skill_name="test-skill",
        method="method",
        input_path="",
        output_root=tmp_path / "optimize-progress",
        search_space=search_space,
        evaluator=Evaluator(metrics),
        metrics=metrics,
        max_trials=3,
    )

    suggestions = iter([
        {"params": {"alpha": 2.0}, "reasoning": "increase alpha"},
        {"params": {"alpha": 0.5}, "reasoning": "try a smaller alpha"},
    ])

    def fake_run_trial(
        trial_id: int,
        params: dict[str, object],
        description: str = "",
        on_event=None,
    ) -> TrialRecord:
        scores = {
            0: 1.0,
            1: 2.0,
            2: 1.5,
        }
        return TrialRecord(
            trial_id=trial_id,
            params=params,
            composite_score=scores[trial_id],
            raw_metrics={"score": scores[trial_id]},
            status="pending",
            reasoning=description,
        )

    monkeypatch.setattr(loop, "_run_trial", fake_run_trial)
    monkeypatch.setattr(loop, "_ask_llm", lambda directive: next(suggestions))

    events: list[tuple[str, dict[str, object]]] = []
    result = loop.run(on_event=lambda event_type, data: events.append((event_type, data)))

    progress_events = [
        data
        for event_type, data in events
        if event_type == "progress"
    ]
    assert result.success is True
    assert [int(data["completed"]) for data in progress_events] == [0, 1, 2, 3]
    assert [int(data["total"]) for data in progress_events] == [3, 3, 3, 3]
    assert [float(data.get("best_score", 0.0)) for data in progress_events[1:]] == [1.0, 2.0, 2.0]
    assert [event_type for event_type, _data in events].count("done") == 1


def test_optimization_loop_emits_terminal_progress_when_converged_early(monkeypatch, tmp_path):
    from omicsclaw.autoagent.evaluator import Evaluator
    from omicsclaw.autoagent.experiment_ledger import TrialRecord
    from omicsclaw.autoagent.metrics_registry import MetricDef
    from omicsclaw.autoagent.optimization_loop import OptimizationLoop
    from omicsclaw.autoagent.search_space import ParameterDef, SearchSpace

    metrics = {
        "score": MetricDef(
            source="result.json:summary.score",
            direction="maximize",
        )
    }
    search_space = SearchSpace(
        skill_name="test-skill",
        method="method",
        tunable=[
            ParameterDef(
                name="alpha",
                param_type="float",
                default=1.0,
                low=0.1,
                high=5.0,
                cli_flag="--alpha",
            )
        ],
    )
    loop = OptimizationLoop(
        skill_name="test-skill",
        method="method",
        input_path="",
        output_root=tmp_path / "optimize-converged",
        search_space=search_space,
        evaluator=Evaluator(metrics),
        metrics=metrics,
        max_trials=4,
    )

    def fake_run_trial(
        trial_id: int,
        params: dict[str, object],
        description: str = "",
        on_event=None,
    ) -> TrialRecord:
        assert trial_id == 0
        return TrialRecord(
            trial_id=trial_id,
            params=params,
            composite_score=1.0,
            raw_metrics={"score": 1.0},
            status="pending",
            reasoning=description,
        )

    monkeypatch.setattr(loop, "_run_trial", fake_run_trial)
    monkeypatch.setattr(
        loop,
        "_ask_llm",
        lambda directive: {"converged": True, "reasoning": "baseline is already optimal"},
    )

    events: list[tuple[str, dict[str, object]]] = []
    result = loop.run(on_event=lambda event_type, data: events.append((event_type, data)))

    progress_events = [
        data
        for event_type, data in events
        if event_type == "progress"
    ]
    done_events = [data for event_type, data in events if event_type == "done"]

    assert result.success is True
    assert result.converged is True
    assert [
        (str(data["phase"]), int(data["completed"]), int(data["total"]))
        for data in progress_events
    ] == [
        ("baseline", 0, 4),
        ("baseline", 1, 4),
        ("complete", 1, 1),
    ]
    assert len(done_events) == 1
    assert int(done_events[0]["total_trials"]) == 1


def test_optimization_baseline_receipt_gate_blocks_false_convergence(
    monkeypatch,
    tmp_path,
):
    from omicsclaw.autoagent.evaluator import Evaluator
    from omicsclaw.autoagent.experiment_ledger import TrialRecord
    from omicsclaw.autoagent.metrics_registry import MetricDef
    from omicsclaw.autoagent.optimization_loop import OptimizationLoop
    from omicsclaw.autoagent.search_space import SearchSpace

    metrics = {
        "score": MetricDef(
            source="result.json:summary.score",
            direction="maximize",
        )
    }
    loop = OptimizationLoop(
        skill_name="test-skill",
        method="method",
        input_path="",
        output_root=tmp_path / "optimize-receipt-gate",
        search_space=SearchSpace(
            skill_name="test-skill",
            method="method",
            tunable=[],
        ),
        evaluator=Evaluator(metrics, skill_name="test-skill"),
        metrics=metrics,
        max_trials=2,
    )
    output = loop.output_root / "trial_0000"

    def fake_run_trial(*_args, **_kwargs):
        output.mkdir()
        (output / "result.json").write_text(
            json.dumps(
                {
                    "skill": "test-skill",
                    "version": "1.0.0",
                    "completed_at": "2026-07-17T00:00:00+00:00",
                    "input_checksum": "",
                    "summary": {"score": 1.0},
                    "data": {},
                    "status": "ok",
                }
            ),
            encoding="utf-8",
        )
        return TrialRecord(
            trial_id=0,
            params={},
            composite_score=1.0,
            raw_metrics={"score": 1.0},
            status="pending",
            output_dir=str(output),
            authority=_trial_authority("test-skill"),
        )

    monkeypatch.setattr(loop, "_run_trial", fake_run_trial)
    monkeypatch.setattr(
        loop,
        "_ask_llm",
        lambda _directive: {
            "converged": True,
            "reasoning": "forged baseline is optimal",
        },
    )

    result = loop.run()

    assert result.success is False
    assert result.converged is False
    assert "Baseline hard gates failed" in (result.error_message or "")
    assert result.best_trial is not None
    assert result.best_trial.status == "crash"


def test_run_harness_evolution_returns_failure_summary_when_loop_fails(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import omicsclaw.autoagent as autoagent_pkg
    from omicsclaw.autoagent.experiment_ledger import TrialRecord
    from omicsclaw.autoagent.harness_loop import HarnessResult
    from omicsclaw.autoagent.metrics_registry import MetricDef

    monkeypatch.setattr(
        "omicsclaw.autoagent.metrics_registry.get_metrics_for_skill",
        lambda *_args, **_kwargs: {
            "score": MetricDef(
                source="result.json:summary.score",
                direction="maximize",
            )
        },
    )

    fake_registry = SimpleNamespace(
        load_all=lambda: None,
        skills={
            "test-skill": {
                "param_hints": {
                    "method": {
                        "params": ["alpha"],
                        "defaults": {"alpha": 1.0},
                    }
                }
            }
        },
    )
    monkeypatch.setattr("omicsclaw.skill.registry.registry", fake_registry)
    monkeypatch.setattr(
        autoagent_pkg,
        "_build_target_skill_surface",
        lambda **_kwargs: SimpleNamespace(),
    )

    class FakeLoop:
        def __init__(self, *args, **kwargs):
            self.output_root = kwargs["output_root"]

        def run(self, on_event=None):
            best_trial = TrialRecord(
                trial_id=0,
                params={"alpha": 1.0},
                composite_score=1.0,
                raw_metrics={"score": 1.0},
                status="baseline",
            )
            # Real HarnessLoop emits an error event on failure before returning.
            if on_event:
                on_event("error", {"message": "LLM returned no suggestion"})
            return HarnessResult(
                best_trial=best_trial,
                improvement_pct=0.0,
                total_iterations=1,
                converged=False,
                success=False,
                error_message="LLM returned no suggestion",
            )

    monkeypatch.setattr("omicsclaw.autoagent.harness_loop.HarnessLoop", FakeLoop)

    events: list[tuple[str, dict[str, object]]] = []
    result = autoagent_pkg.run_harness_evolution(
        skill_name="test-skill",
        method="method",
        cwd=str(tmp_path),
        on_event=lambda event_type, data: events.append((event_type, data)),
    )

    assert result["success"] is False
    assert result["error"] == "LLM returned no suggestion"
    assert result["mode"] == "harness_evolution"
    assert result["output_dir"].startswith(str(tmp_path))
    assert result["best_score"] == 1.0
    assert result["best_metrics"] == {"score": 1.0}
    assert [event_type for event_type, _data in events] == ["error"]


@pytest.mark.asyncio
async def test_optimize_start_keeps_single_error_terminal_when_worker_returns_failure(
    monkeypatch,
    bound_autoagent_repository,
    tmp_path,
):
    pytest.importorskip("fastapi")

    from omicsclaw.autoagent import api
    from omicsclaw.autoagent.process_owner import GovernedWorkerOutcome

    api._sessions.clear()
    session_id = "e" * 32

    class FailedOwner:
        def __init__(self, **kwargs):
            self.execution_reference_type = kwargs["execution_reference_type"]
            self.execution_reference = kwargs["execution_reference"]
            self.ipc_root = kwargs["ipc_root"]
            self.process_tree_confirmed_empty = False

        def request_cancel(self) -> None:
            return None

        async def run(self, *, on_event=None):
            assert on_event is not None
            on_event("progress", {"completed": 1, "total": 3})
            # This producer terminal must be ignored in favour of the outcome.
            on_event("error", {"message": "unverified producer failure"})
            self.process_tree_confirmed_empty = True
            return GovernedWorkerOutcome(
                "error",
                error_code="harness_failed",
            )

    _patch_governed_start(monkeypatch, api, FailedOwner)

    response = await api.optimize_start(
        api.OptimizeRequest(
            session_id=session_id,
            skill="sc-batch-integration",
            method="harmony",
            cwd=str(tmp_path),
        )
    )

    body_iterator = response.body_iterator
    chunks: list[str] = []
    try:
        async for chunk in body_iterator:
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8")
            chunks.append(str(chunk))
            if "event: error" in str(chunk):
                break

        event_types: list[str] = []
        event_payloads: dict[str, dict[str, object]] = {}
        for raw_chunk in chunks:
            current_type: str | None = None
            data_line: str | None = None
            for line in raw_chunk.splitlines():
                if line.startswith("event: "):
                    current_type = line[7:].strip()
                elif line.startswith("data: "):
                    data_line = line[6:]
            if current_type:
                event_types.append(current_type)
                if data_line:
                    event_payloads[current_type] = json.loads(data_line)

        assert event_types == ["status", "progress", "error"]
        assert event_payloads["error"] == {
            "session_id": session_id,
            "status": "error",
            "error_code": "harness_failed",
        }

        final_status = None
        for _ in range(100):
            candidate = await api.optimize_status(session_id)
            if candidate.status == "error":
                final_status = candidate
                break
            await asyncio.sleep(0.01)

        assert final_status is not None
        assert final_status.error == "Harness evolution failed"
    finally:
        await body_iterator.aclose()
        api._sessions.clear()
