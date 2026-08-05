"""Tests for the ADR-0074 M-C evaluation result store + orchestration."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile

import pytest

import omicsclaw.skill.evaluation_run as evaluation_run_module
from omicsclaw.skill.evaluation_run import (
    EvaluationResultConflictError,
    EvaluationResultStore,
    EvaluationStoreCorruptError,
    run_protocol_evaluations,
)
from omicsclaw.skill.skill_audit import ProtocolEvaluationResult, SkillRevision
from omicsclaw.skill.evolution_governance import _run_protocol_entry

SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64

REV = SkillRevision("sc-de", "1.0.0", SHA_A, SHA_B)
OTHER = SkillRevision("sc-de", "1.0.0", SHA_C, SHA_D)


def _result(protocol_id="p1", kind="fixture", digest=SHA_C, outcome="succeeded",
            occurred_at="2026-07-23T00:00:00Z"):
    return ProtocolEvaluationResult(
        protocol_id,
        kind,
        digest,
        outcome,
        occurred_at,
        environment_id=SHA_D,
        dataset_digest=SHA_A,
        reason_code="none" if outcome == "succeeded" else "protocol_failed",
    )


# ---- store ------------------------------------------------------------------


def test_store_append_and_results_round_trip(tmp_path):
    store = EvaluationResultStore(tmp_path / "evals.jsonl")
    r = _result()
    store.append(REV, r)
    assert store.results_for(REV) == [r]


def test_store_filters_by_exact_revision(tmp_path):
    store = EvaluationResultStore(tmp_path / "evals.jsonl")
    store.append(REV, _result(protocol_id="a"))
    store.append(OTHER, _result(protocol_id="b"))
    got = store.results_for(REV)
    assert [x.protocol_id for x in got] == ["a"]


def test_store_missing_file_is_empty(tmp_path):
    assert EvaluationResultStore(tmp_path / "nope.jsonl").results_for(REV) == []


def test_store_preserves_append_order(tmp_path):
    store = EvaluationResultStore(tmp_path / "evals.jsonl")
    for i in range(3):
        store.append(REV, _result(protocol_id=f"p{i}", occurred_at=f"t{i}"))
    assert [r.protocol_id for r in store.results_for(REV)] == ["p0", "p1", "p2"]


def test_store_corrupt_row_fails_closed(tmp_path):
    path = tmp_path / "evals.jsonl"
    store = EvaluationResultStore(path)
    store.append(REV, _result())
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    with pytest.raises(EvaluationStoreCorruptError):
        store.results_for(REV)


def test_store_reads_v1_conservatively_without_reconstructing_a_batch(tmp_path):
    path = tmp_path / "evals.jsonl"
    legacy = {
        "schema_version": 1,
        "revision": REV.to_dict(),
        "result": {
            "protocol_id": "legacy",
            "kind": "fixture",
            "protocol_digest": "digest",
            "outcome": "succeeded",
            "occurred_at": "t",
            "run_index": 0,
            "repeats": 2,
            "metrics": {},
        },
    }
    path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

    [result] = EvaluationResultStore(path).results_for(REV)
    assert len(result.result_id) == 32
    assert result.evaluation_id == result.result_id
    assert result.environment_id == ""
    assert result.dataset_digest == ""


def test_store_unknown_schema_fails_closed(tmp_path):
    path = tmp_path / "evals.jsonl"
    path.write_text(
        json.dumps({"schema_version": 99, "revision": {}, "result": {}}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(EvaluationStoreCorruptError):
        EvaluationResultStore(path).results_for(REV)


def test_store_result_id_is_idempotent_and_conflicting_reuse_fails_closed(tmp_path):
    store = EvaluationResultStore(tmp_path / "evals.jsonl")
    result = ProtocolEvaluationResult(
        "p1",
        "fixture",
        SHA_C,
        "succeeded",
        "t",
        result_id="1" * 32,
        evaluation_id="2" * 32,
        environment_id=SHA_D,
        dataset_digest=SHA_A,
    )
    store.append(REV, result)
    store.append(REV, result)
    assert store.results_for(REV) == [result]

    conflicting = ProtocolEvaluationResult(
        "p1",
        "fixture",
        SHA_C,
        "failed",
        "t",
        result_id="1" * 32,
        evaluation_id="2" * 32,
        environment_id=SHA_D,
        dataset_digest=SHA_A,
        reason_code="protocol_failed",
    )
    with pytest.raises(EvaluationResultConflictError):
        store.append(REV, conflicting)


def test_store_requires_strict_ids_and_closed_classifications(tmp_path):
    store = EvaluationResultStore(tmp_path / "evals.jsonl")
    result = _result()
    with pytest.raises(ValueError, match="invalid evaluation result"):
        store.append(REV, replace(result, result_id=""))
    with pytest.raises(ValueError, match="invalid evaluation result"):
        store.append(REV, replace(result, reason_code="free-form-error"))


@pytest.mark.parametrize(
    ("change", "value"),
    [
        ("protocol_digest", "not-a-digest"),
        ("environment_id", ""),
        ("dataset_digest", "not-a-digest"),
    ],
)
def test_store_rejects_unbound_v2_evidence(tmp_path, change, value):
    store = EvaluationResultStore(tmp_path / "evals.jsonl")
    with pytest.raises(ValueError, match="invalid evaluation result"):
        store.append(REV, replace(_result(), **{change: value}))


def test_store_requires_benchmark_dataset_identity(tmp_path):
    store = EvaluationResultStore(tmp_path / "evals.jsonl")
    with pytest.raises(ValueError, match="invalid evaluation result"):
        store.append(REV, replace(_result(kind="benchmark"), dataset_digest=""))


@pytest.mark.parametrize(
    "result",
    [
        replace(_result(), reason_code="protocol_failed"),
        replace(_result(outcome="failed"), reason_code="none"),
    ],
)
def test_store_rejects_outcome_reason_contradictions(tmp_path, result):
    store = EvaluationResultStore(tmp_path / "evals.jsonl")
    with pytest.raises(ValueError, match="invalid evaluation result"):
        store.append(REV, result)


def test_store_requires_content_addressed_skill_revision(tmp_path):
    store = EvaluationResultStore(tmp_path / "evals.jsonl")
    with pytest.raises(ValueError, match="invalid evaluation result"):
        store.append(replace(REV, source_hash="not-a-digest"), _result())


def test_artifact_store_is_content_addressed_and_detects_corruption(tmp_path):
    store = evaluation_run_module.EvaluationArtifactStore(tmp_path / "artifacts")
    payload = b"immutable evaluator evidence\n"

    first = store.put_bytes(payload)
    second = store.put_bytes(payload)

    assert first == second
    assert store.read_bytes(first) == payload
    digest = first.rsplit(":", 1)[1]
    object_path = store.root / "objects" / digest[:2] / digest[2:]
    object_path.write_bytes(b"corrupt")
    with pytest.raises(
        evaluation_run_module.EvaluationArtifactStoreError,
        match="integrity verification",
    ):
        store.read_bytes(first)


def test_artifact_store_rejects_oversized_corrupt_object_before_read(tmp_path):
    store = evaluation_run_module.EvaluationArtifactStore(tmp_path / "artifacts")
    reference = store.put_bytes(b"small")
    digest = reference.rsplit(":", 1)[1]
    object_path = store.root / "objects" / digest[:2] / digest[2:]
    with object_path.open("wb") as handle:
        handle.truncate(evaluation_run_module._MAX_ARTIFACT_BYTES + 1)

    with pytest.raises(
        evaluation_run_module.EvaluationArtifactStoreError,
        match="exceeds size limit",
    ):
        store.read_bytes(reference)


def test_default_artifact_store_is_derived_from_result_store(tmp_path, monkeypatch):
    monkeypatch.delenv("OMICSCLAW_EVALUATION_ARTIFACT_STORE", raising=False)
    result_store = EvaluationResultStore(tmp_path / "audit" / "evals.jsonl")

    artifact_store = evaluation_run_module.default_evaluation_artifact_store(
        result_store
    )

    assert artifact_store.root == tmp_path / "audit" / "evals.jsonl.artifacts"


# ---- orchestration ----------------------------------------------------------


def test_run_protocol_evaluations_builds_bound_results():
    protocols = [
        ({"id": "p1", "kind": "fixture"}, "digest-1"),
        ({"id": "p2", "kind": "benchmark"}, "digest-2"),
    ]
    outcomes = {"p1": "succeeded", "p2": "failed"}
    results = run_protocol_evaluations(
        REV, protocols,
        run_one=lambda spec: outcomes[spec["id"]],
        now=lambda: "2026-07-23T12:00:00Z",
    )
    assert [(r.protocol_id, r.kind, r.protocol_digest, r.outcome) for r in results] == [
        ("p1", "fixture", "digest-1", "succeeded"),
        ("p2", "benchmark", "digest-2", "failed"),
    ]
    assert all(r.occurred_at == "2026-07-23T12:00:00Z" for r in results)
    assert len({r.evaluation_id for r in results}) == 1
    assert all(r.evaluation_id for r in results)
    assert len({r.result_id for r in results}) == len(results)
    assert all(r.result_id for r in results)


def test_run_then_store_then_derive_end_to_end(tmp_path):
    # The results a run produces, once stored, are exactly what results_for returns.
    from omicsclaw.skill.skill_audit import derive_experience_view

    store = EvaluationResultStore(tmp_path / "evals.jsonl")
    results = run_protocol_evaluations(
        REV,
        [(
            {
                "id": "p1",
                "kind": "fixture",
                "environment_id": SHA_D,
                "dataset_digest": SHA_A,
            },
            SHA_C,
        )],
        run_one=lambda spec: "succeeded",
        now=lambda: "2026-07-23T12:00:00Z",
    )
    for r in results:
        store.append(REV, r)

    fresh = store.results_for(REV)
    view = derive_experience_view(
        REV, "fixture-validated", [],
        protocol_results=fresh, current_protocol_digests={"p1": SHA_C},
    )
    assert view.effective_validation_level == "fixture-validated"
    assert view.validation_state == "current"


# ---- AUD-10: repeats + metric allowlist -------------------------------------


def test_repeats_runs_protocol_n_times():
    protocols = [({"id": "s1", "kind": "stability", "repeats": 3}, "d1")]
    results = run_protocol_evaluations(
        REV, protocols, run_one=lambda spec: "succeeded", now=lambda: "t")
    assert len(results) == 3
    assert [r.run_index for r in results] == [0, 1, 2]
    assert all(r.repeats == 3 for r in results)


def test_metrics_are_allowlist_filtered_and_numeric():
    protocols = [({"id": "s1", "kind": "stability", "repeats": 1,
                   "metrics": ["silhouette"]}, "d1")]
    results = run_protocol_evaluations(
        REV, protocols,
        run_one=lambda spec: ("succeeded", {"silhouette": 0.8, "denied": 1.0,
                                            "bad": float("nan"), "notnum": "x"}),
        now=lambda: "t")
    assert results[0].metrics == {"silhouette": 0.8}


def test_string_runner_yields_no_metrics_and_round_trips(tmp_path):
    store = EvaluationResultStore(tmp_path / "ev.jsonl")
    [r] = run_protocol_evaluations(
        REV,
        [(
            {
                "id": "s1",
                "kind": "stability",
                "repeats": 1,
                "metrics": ["m"],
                "environment_id": SHA_D,
            },
            SHA_C,
        )],
        run_one=lambda spec: "failed", now=lambda: "t")
    assert r.metrics == {}
    assert r.reason_code == "protocol_failed"
    store.append(REV, r)
    assert store.results_for(REV) == [r]  # run_index/repeats/metrics survive round-trip


def test_command_protocol_entry_receives_dataset_and_returns_structured_result(
    tmp_path, monkeypatch
):
    skill_dir = tmp_path / "skill"
    tests_dir = skill_dir / "tests"
    tests_dir.mkdir(parents=True)
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "fixture.txt").write_text("ok", encoding="utf-8")
    output = tmp_path / "protocol-output"
    entry = tests_dir / "benchmark_entry.py"
    entry.write_text(
        """
import json
import os
from pathlib import Path

dataset = Path(os.environ["OMICSCLAW_EVALUATION_DATASET"])
result = Path(os.environ["OMICSCLAW_EVALUATION_RESULT"])
assert (dataset / "fixture.txt").read_text() == "ok"
assert "OMICSCLAW_SKILL_EVOLUTION_TOKEN" not in os.environ
assert os.environ["OMICSCLAW_SKIP_ADAPTIVE_ENV"] == "1"
assert os.environ["OMICSCLAW_ADAPTIVE_ENV"] == "off"
result.write_text(json.dumps({
    "schema_version": 1,
    "outcome": "succeeded",
    "reason_code": "none",
    "metrics": {"score": 1.0, "ignored": 9.0},
}))
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMICSCLAW_SKILL_EVOLUTION_TOKEN", "must-not-leak")

    outcome = _run_protocol_entry(
        skill_dir,
        {
            "entry": "tests/benchmark_entry.py",
            "runner": "command",
            "timeout_seconds": 30,
        },
        dataset_path=dataset,
        output_dir=output,
    )

    assert outcome.outcome == "succeeded"
    assert outcome.reason_code == "none"
    assert outcome.metrics == {"score": 1.0, "ignored": 9.0}


def test_command_protocol_import_does_not_write_bytecode_into_dataset(tmp_path):
    skill_dir = tmp_path / "skill"
    entry = skill_dir / "tests" / "entry.py"
    entry.parent.mkdir(parents=True)
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "grader_helper.py").write_text("SCORE = 1.0\n", encoding="utf-8")
    entry.write_text(
        """
import json
import os
import sys
from pathlib import Path

dataset = Path(os.environ["OMICSCLAW_EVALUATION_DATASET"])
sys.path.insert(0, str(dataset))
from grader_helper import SCORE

Path(os.environ["OMICSCLAW_EVALUATION_RESULT"]).write_text(json.dumps({
    "schema_version": 1,
    "outcome": "succeeded",
    "reason_code": "none",
    "metrics": {"score": SCORE},
}))
""".strip()
        + "\n",
        encoding="utf-8",
    )

    outcome = _run_protocol_entry(
        skill_dir,
        {"entry": "tests/entry.py", "runner": "command"},
        dataset_path=dataset,
        output_dir=tmp_path / "output",
    )

    assert outcome.outcome == "succeeded"
    assert not list(dataset.rglob("*.pyc"))
    assert not (dataset / "__pycache__").exists()


def test_command_protocol_mounts_dataset_read_only_when_bubblewrap_is_available(
    tmp_path,
):
    from omicsclaw.skill.evolution_governance import _bubblewrap_available

    if not _bubblewrap_available():
        pytest.skip("bubblewrap read-only mount is unavailable on this host")
    skill_dir = tmp_path / "skill"
    entry = skill_dir / "tests" / "entry.py"
    entry.parent.mkdir(parents=True)
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    protected = dataset / "protected.txt"
    protected.write_text("original\n", encoding="utf-8")
    entry.write_text(
        """
import json
import os
from pathlib import Path

dataset = Path(os.environ["OMICSCLAW_EVALUATION_DATASET"])
try:
    (dataset / "protected.txt").write_text("modified\\n")
except OSError:
    outcome, reason = "succeeded", "none"
else:
    outcome, reason = "failed", "dataset_integrity_failed"
Path(os.environ["OMICSCLAW_EVALUATION_RESULT"]).write_text(json.dumps({
    "schema_version": 1,
    "outcome": outcome,
    "reason_code": reason,
    "metrics": {},
}))
""".strip()
        + "\n",
        encoding="utf-8",
    )

    outcome = _run_protocol_entry(
        skill_dir,
        {"entry": "tests/entry.py", "runner": "command"},
        dataset_path=dataset,
        output_dir=tmp_path / "output",
    )

    assert outcome.outcome == "succeeded"
    assert protected.read_text(encoding="utf-8") == "original\n"


def test_command_protocol_receives_governance_owned_framework_import_root(
    tmp_path, monkeypatch
):
    framework_root = tmp_path / "repository"
    package = framework_root / "framework_package"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 7\n", encoding="utf-8")
    skill_dir = framework_root / "skills" / "domain" / "skill"
    entry = skill_dir / "tests" / "entry.py"
    entry.parent.mkdir(parents=True)
    entry.write_text(
        """
import json
import os
from pathlib import Path
from framework_package import VALUE

Path(os.environ["OMICSCLAW_EVALUATION_RESULT"]).write_text(json.dumps({
    "schema_version": 1,
    "outcome": "succeeded",
    "reason_code": "none",
    "metrics": {"value": VALUE},
}))
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("PYTHONPATH", raising=False)

    outcome = _run_protocol_entry(
        skill_dir,
        {"entry": "tests/entry.py", "runner": "command"},
        output_dir=tmp_path / "output",
        framework_root=framework_root,
    )

    assert outcome.outcome == "succeeded"
    assert outcome.metrics == {"value": 7}


def test_command_protocol_entry_rejects_duplicate_json_keys(tmp_path):
    skill_dir = tmp_path / "skill"
    entry = skill_dir / "tests" / "entry.py"
    entry.parent.mkdir(parents=True)
    entry.write_text(
        """
import os
from pathlib import Path

Path(os.environ["OMICSCLAW_EVALUATION_RESULT"]).write_text(
    '{"schema_version":1,"outcome":"succeeded","outcome":"failed",'
    '"reason_code":"none","metrics":{}}'
)
""".strip()
        + "\n",
        encoding="utf-8",
    )

    outcome = _run_protocol_entry(
        skill_dir,
        {"entry": "tests/entry.py", "runner": "command"},
        output_dir=tmp_path / "output",
    )
    assert outcome.outcome == "failed"
    assert outcome.reason_code == "result_invalid"


def test_benchmark_command_requires_exact_skill_revision(tmp_path):
    skill_dir = tmp_path / "skill"
    entry = skill_dir / "tests" / "entry.py"
    entry.parent.mkdir(parents=True)
    entry.write_text(
        """
import json
import os
from pathlib import Path

Path(os.environ["OMICSCLAW_EVALUATION_RESULT"]).write_text(json.dumps({
    "schema_version": 1,
    "outcome": "succeeded",
    "reason_code": "none",
    "metrics": {"score": 1.0},
    "skill_revision": {
        "skill_id": "other",
        "version": "1.0.0",
        "manifest_hash": "m",
        "source_hash": "s",
    },
}))
""".strip()
        + "\n",
        encoding="utf-8",
    )
    expected = REV.to_dict()
    outcome = _run_protocol_entry(
        skill_dir,
        {
            "entry": "tests/entry.py",
            "runner": "command",
            "kind": "benchmark",
            "skill_revision": expected,
        },
        output_dir=tmp_path / "output",
    )
    assert outcome.outcome == "failed"
    assert outcome.reason_code == "revision_unverified"


def test_command_protocol_artifacts_survive_temporary_output_cleanup(tmp_path):
    skill_dir = tmp_path / "skill"
    entry = skill_dir / "tests" / "entry.py"
    entry.parent.mkdir(parents=True)
    trace_bytes = b"bounded evaluator trace\n"
    trace_digest = "sha256:" + hashlib.sha256(trace_bytes).hexdigest()
    entry.write_text(
        f"""
import json
import os
import sys
from pathlib import Path

output = Path(os.environ["OMICSCLAW_EVALUATION_OUTPUT"])
(output / "trace.md").write_bytes({trace_bytes!r})
print("protocol stdout")
print("protocol stderr", file=sys.stderr)
Path(os.environ["OMICSCLAW_EVALUATION_RESULT"]).write_text(json.dumps({{
    "schema_version": 1,
    "outcome": "succeeded",
    "reason_code": "none",
    "metrics": {{}},
    "evidence_refs": [{trace_digest!r}],
}}))
""".strip()
        + "\n",
        encoding="utf-8",
    )
    artifact_store = evaluation_run_module.EvaluationArtifactStore(
        tmp_path / "evaluation-artifacts"
    )

    with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
        output_dir = Path(temporary) / "protocol"
        outcome = _run_protocol_entry(
            skill_dir,
            {"id": "fixture", "entry": "tests/entry.py", "runner": "command"},
            output_dir=output_dir,
            artifact_store=artifact_store,
        )
        assert output_dir.is_dir()

    bundle_refs = [
        ref
        for ref in outcome.evidence_refs
        if ref.startswith("evaluation-artifact:sha256:")
    ]
    assert len(bundle_refs) == 1
    bundle = artifact_store.read_json(bundle_refs[0])
    by_role = {item["role"]: item for item in bundle["artifacts"]}
    assert artifact_store.read_bytes(by_role["stdout"]["ref"]) == b"protocol stdout\n"
    assert artifact_store.read_bytes(by_role["stderr"]["ref"]) == b"protocol stderr\n"
    assert artifact_store.read_bytes(by_role["evidence"]["ref"]) == trace_bytes


def test_command_protocol_failure_persists_diagnostics_before_cleanup(tmp_path):
    skill_dir = tmp_path / "skill"
    entry = skill_dir / "tests" / "entry.py"
    entry.parent.mkdir(parents=True)
    entry.write_text(
        "print('diagnostic before invalid result')\n",
        encoding="utf-8",
    )
    artifact_store = evaluation_run_module.EvaluationArtifactStore(
        tmp_path / "evaluation-artifacts"
    )

    with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
        outcome = _run_protocol_entry(
            skill_dir,
            {"id": "fixture", "entry": "tests/entry.py", "runner": "command"},
            output_dir=Path(temporary) / "protocol",
            artifact_store=artifact_store,
        )

    assert outcome.outcome == "failed"
    assert outcome.reason_code == "result_invalid"
    [bundle_ref] = [
        ref
        for ref in outcome.evidence_refs
        if ref.startswith("evaluation-artifact:sha256:")
    ]
    bundle = artifact_store.read_json(bundle_ref)
    stdout = next(item for item in bundle["artifacts"] if item["role"] == "stdout")
    assert artifact_store.read_bytes(stdout["ref"]) == (
        b"diagnostic before invalid result\n"
    )


def test_command_protocol_nonzero_exit_without_result_is_execution_error(tmp_path):
    skill_dir = tmp_path / "skill"
    entry = skill_dir / "tests" / "entry.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("raise RuntimeError('boom')\n", encoding="utf-8")

    outcome = _run_protocol_entry(
        skill_dir,
        {"entry": "tests/entry.py", "runner": "command"},
        output_dir=tmp_path / "output",
    )

    assert outcome.outcome == "failed"
    assert outcome.reason_code == "execution_error"


def test_command_protocol_keyboard_interrupt_stops_process_group(tmp_path, monkeypatch):
    skill_dir = tmp_path / "skill"
    entry = skill_dir / "tests" / "entry.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")

    real_popen = subprocess.Popen
    spawned: list[subprocess.Popen] = []

    class InterruptOnce:
        def __init__(self, process: subprocess.Popen) -> None:
            self.process = process
            self.interrupted = False

        @property
        def pid(self) -> int:
            return self.process.pid

        def poll(self):
            return self.process.poll()

        def wait(self, timeout=None):
            if not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt
            return self.process.wait(timeout=timeout)

    def interrupting_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return InterruptOnce(process)

    monkeypatch.setattr(subprocess, "Popen", interrupting_popen)
    leaked_before_fallback_cleanup = []
    try:
        with pytest.raises(KeyboardInterrupt):
            _run_protocol_entry(
                skill_dir,
                {"entry": "tests/entry.py", "runner": "command"},
                output_dir=tmp_path / "output",
            )
    finally:
        for process in spawned:
            if process.poll() is None:
                leaked_before_fallback_cleanup.append(process.pid)
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)

    assert len(spawned) == 1
    assert leaked_before_fallback_cleanup == []
    assert spawned[0].poll() is not None
