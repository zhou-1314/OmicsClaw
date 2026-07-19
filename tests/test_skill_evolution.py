from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import sys
import threading

import pytest

import omicsclaw.skill.evolution as evolution_module
from omicsclaw.skill.evolution import (
    EvolutionApplyError,
    EvolutionProposalStore,
    SkillHealthLedger,
    SkillRunEvent,
    generate_evolution_proposals,
    record_skill_run_result,
)
from omicsclaw.skill.outcomes import SkillErrorKind, classify_skill_error
from omicsclaw.skill.result import build_skill_run_result


def _hold_evolution_lock(path: str, acquired, release) -> None:
    with evolution_module._exclusive_file_lock(Path(path)):
        acquired.set()
        release.wait(timeout=5)


def test_runtime_evidence_probe_scrubs_supplied_backend_control_credentials(
    tmp_path: Path,
):
    control_keys = (
        "OMICSCLAW_REMOTE_AUTH_TOKEN",
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN",
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD",
    )
    runtime_env = os.environ.copy()
    runtime_env.update({key: "must-not-reach-runtime-probe" for key in control_keys})
    runtime_env["OMICSCLAW_EVOLUTION_PROBE_TEST_KEEP"] = "ordinary-value"
    code = (
        "import json, os;"
        f"print(json.dumps({{k: os.environ.get(k) for k in {control_keys!r}}}));"
        "print(os.environ.get('OMICSCLAW_EVOLUTION_PROBE_TEST_KEEP', ''))"
    )

    completed = evolution_module._run_bounded_runtime_probe(
        [sys.executable, "-c", code],
        runtime_env=runtime_env,
        runtime_cwd=tmp_path,
    )

    lines = completed.stdout.decode("utf-8").splitlines()
    assert json.loads(lines[0]) == {key: None for key in control_keys}
    assert lines[1] == "ordinary-value"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"success": True}, SkillErrorKind.NONE),
        ({"success": False, "stderr": "ModuleNotFoundError: No module named scanpy"}, SkillErrorKind.MISSING_DEPENDENCY),
        ({"success": False, "stderr": "invalid input file"}, SkillErrorKind.BAD_INPUT),
        ({"success": False, "stderr": "operation timed out"}, SkillErrorKind.TIMEOUT),
        ({"success": False, "exit_code": 137, "stderr": "Killed"}, SkillErrorKind.RESOURCE_EXHAUSTED),
        ({"success": False, "cancelled": True}, SkillErrorKind.CANCELLED),
        ({"success": False, "contract_failure": True}, SkillErrorKind.CONTRACT_FAILURE),
        ({"success": False, "stderr": "Traceback: AssertionError"}, SkillErrorKind.SCRIPT_DEFECT),
        ({"success": False, "stderr": "unclassified failure"}, SkillErrorKind.UNKNOWN),
    ],
)
def test_error_classifier_has_typed_positive_negative_and_unknown_cases(kwargs, expected):
    assert classify_skill_error(**kwargs) is expected


def test_evolution_lock_serializes_independent_processes(tmp_path: Path):
    context = multiprocessing.get_context("spawn")
    lock_path = str(tmp_path / "evolution.lock")
    first_acquired = context.Event()
    first_release = context.Event()
    second_acquired = context.Event()
    second_release = context.Event()
    first = context.Process(
        target=_hold_evolution_lock,
        args=(lock_path, first_acquired, first_release),
    )
    second = context.Process(
        target=_hold_evolution_lock,
        args=(lock_path, second_acquired, second_release),
    )

    first.start()
    assert first_acquired.wait(timeout=3)
    second.start()
    assert not second_acquired.wait(timeout=0.2)
    first_release.set()
    assert second_acquired.wait(timeout=3)
    second_release.set()
    first.join(timeout=3)
    second.join(timeout=3)

    assert first.exitcode == 0
    assert second.exitcode == 0


def test_evolution_lock_fails_closed_without_an_os_process_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(evolution_module, "fcntl", None)
    monkeypatch.setattr(evolution_module, "msvcrt", None)

    with pytest.raises(RuntimeError, match="cross-process"):
        with evolution_module._exclusive_file_lock(tmp_path / "unsupported.lock"):
            pass


def test_final_ledger_fence_blocks_governed_event_append(tmp_path: Path):
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    ledger.append(_event("before-fence"))
    append_started = threading.Event()
    append_finished = threading.Event()

    def append_during_fence() -> None:
        append_started.set()
        ledger.append(_event("after-fence"))
        append_finished.set()

    writer = threading.Thread(target=append_during_fence)
    with ledger.locked_events() as events:
        writer.start()
        assert append_started.wait(timeout=1)
        assert not append_finished.wait(timeout=0.2)
        assert [event.event_id for event in events] == ["before-fence"]

    writer.join(timeout=2)
    assert not writer.is_alive()
    assert append_finished.is_set()
    assert [event.event_id for event in ledger.events()] == [
        "before-fence",
        "after-fence",
    ]


def test_guarded_manifest_write_fails_closed_without_atomic_exchange(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    target = tmp_path / "skill.yaml"
    expected = b"validation: smoke-only\n"
    external_edit = b"validation: smoke-only\nexternal: preserved\n"
    target.write_bytes(external_edit)
    monkeypatch.setattr(evolution_module, "_rename_exchange", lambda _left, _right: False)

    with pytest.raises(evolution_module._AtomicWriteConflict, match="atomic compare-and-swap"):
        evolution_module._atomic_write(
            target,
            b"validation: demo-validated\n",
            mode=target.stat().st_mode,
            expected=expected,
        )

    assert target.read_bytes() == external_edit


def test_manifest_identity_includes_change_and_permission_metadata(tmp_path: Path):
    target = tmp_path / "skill.yaml"
    target.write_bytes(b"validation: smoke-only\n")
    observed = evolution_module._file_identity(target)
    stat = target.stat()

    assert observed == (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        stat.st_mode,
        stat.st_uid,
        stat.st_gid,
        stat.st_nlink,
    )


def test_guarded_manifest_write_keeps_witness_when_directory_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    target = tmp_path / "skill.yaml"
    before = b"validation: smoke-only\n"
    target.write_bytes(before)
    original_fsync_directory = evolution_module._fsync_directory
    fsync_count = 0

    def fail_publish_directory_fsync(path: Path) -> None:
        nonlocal fsync_count
        fsync_count += 1
        if fsync_count == 2:
            raise OSError("simulated directory fsync failure")
        original_fsync_directory(path)

    monkeypatch.setattr(
        evolution_module,
        "_fsync_directory",
        fail_publish_directory_fsync,
    )

    promoted = b"validation: demo-validated\n"
    with pytest.raises(
        evolution_module._AtomicWriteDurabilityError,
        match="durability",
    ):
        evolution_module._atomic_write(
            target,
            promoted,
            mode=target.stat().st_mode,
            expected=before,
        )

    witness = evolution_module._guarded_swap_path(target)
    assert target.read_bytes() == promoted
    assert witness.read_bytes() == before


def test_guarded_manifest_conflict_retains_external_predecessor_witness(
    tmp_path: Path,
):
    target = tmp_path / "skill.yaml"
    expected = b"validation: smoke-only\n"
    external = b"validation: smoke-only\nexternal: retained\n"
    promoted = b"validation: demo-validated\n"
    target.write_bytes(external)

    with pytest.raises(
        evolution_module._AtomicWriteDurabilityError,
        match="predecessor mismatch",
    ):
        evolution_module._atomic_write(
            target,
            promoted,
            mode=target.stat().st_mode,
            expected=expected,
        )

    witness = evolution_module._guarded_swap_path(target)
    assert target.read_bytes() == promoted
    assert witness.read_bytes() == external


def test_guarded_manifest_mismatch_never_auto_rolls_back_over_later_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    target = tmp_path / "skill.yaml"
    expected = b"validation: smoke-only\n"
    external_before_exchange = b"external: before-exchange\n"
    external_before_rollback = b"external: after-check-before-rollback\n"
    promoted = b"validation: demo-validated\n"
    target.write_bytes(external_before_exchange)
    real_exchange = evolution_module._rename_exchange
    exchange_calls = 0

    def inject_if_destructive_rollback(left: Path, right: Path) -> bool:
        nonlocal exchange_calls
        exchange_calls += 1
        if exchange_calls == 2:
            Path(right).write_bytes(external_before_rollback)
        return real_exchange(Path(left), Path(right))

    monkeypatch.setattr(
        evolution_module,
        "_rename_exchange",
        inject_if_destructive_rollback,
    )

    with pytest.raises(
        evolution_module._AtomicWriteDurabilityError,
        match="predecessor",
    ):
        evolution_module._atomic_write(
            target,
            promoted,
            mode=target.stat().st_mode,
            expected=expected,
        )

    witness = evolution_module._guarded_swap_path(target)
    assert exchange_calls == 1
    assert target.read_bytes() == promoted
    assert witness.read_bytes() == external_before_exchange


def _event(
    event_id: str,
    *,
    environment_id: str = "env-a",
    outcome: str = "failed",
    error_kind: SkillErrorKind = SkillErrorKind.SCRIPT_DEFECT,
    source_hash: str = "sha256:source-a",
) -> SkillRunEvent:
    return SkillRunEvent(
        event_id=event_id,
        occurred_at="2026-07-14T00:00:00+00:00",
        run_id=f"run-{event_id}",
        skill_id="sc-test",
        skill_version="1.2.3",
        skill_hash="sha256:skill",
        environment_id=environment_id,
        outcome=outcome,
        error_kind=error_kind.value,
        exit_code=0 if outcome == "succeeded" else 1,
        duration_seconds=1.0,
        evidence_refs=[f"stderr:sha256:{event_id}"],
        source_hash=source_hash,
    )


def test_health_ledger_buckets_by_skill_version_hash_and_environment(tmp_path: Path):
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    for event in (
        _event("defect"),
        _event("dependency", error_kind=SkillErrorKind.MISSING_DEPENDENCY),
        _event("cancel", error_kind=SkillErrorKind.CANCELLED),
        _event("other-env", environment_id="env-b"),
    ):
        ledger.append(event)

    buckets = ledger.summarize()

    assert len(buckets) == 2
    env_a = next(bucket for bucket in buckets if bucket.environment_id == "env-a")
    assert env_a.skill_defect_count == 1
    assert env_a.environment_failure_count == 1
    assert env_a.cancelled_count == 1
    assert env_a.failures_by_kind["script_defect"] == 1


def test_health_ledger_separates_runtime_entry_source_revisions(tmp_path: Path):
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    ledger.append(_event("source-a", source_hash="sha256:source-a"))
    ledger.append(_event("source-b", source_hash="sha256:source-b"))

    buckets = ledger.summarize()

    assert len(buckets) == 2
    assert {bucket.source_hash for bucket in buckets} == {
        "sha256:source-a",
        "sha256:source-b",
    }


def test_framework_validator_failure_is_not_counted_as_a_skill_defect(tmp_path: Path):
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    ledger.append(
        _event(
            "validator",
            error_kind=SkillErrorKind.CONTRACT_VALIDATOR_FAILED,
        )
    )

    bucket = ledger.summarize()[0]

    assert bucket.framework_failure_count == 1
    assert bucket.skill_defect_count == 0
    assert bucket.environment_failure_count == 0


def test_legacy_candidate_generator_never_bypasses_gotcha_governance():
    events = [_event(f"failure-{index}") for index in range(3)]
    events.append(_event("counterexample", outcome="succeeded", error_kind=SkillErrorKind.NONE))

    proposals = generate_evolution_proposals(events, repeated_threshold=3)

    assert proposals == []

    environment_only = [
        _event(f"env-{index}", error_kind=SkillErrorKind.MISSING_DEPENDENCY)
        for index in range(3)
    ]
    assert generate_evolution_proposals(environment_only, repeated_threshold=3) == []


def test_pending_proposal_cannot_write_and_approved_change_is_revalidated(tmp_path: Path):
    target = tmp_path / "skill.yaml"
    target.write_text("validation: smoke-only\n", encoding="utf-8")
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    proposal = generate_evolution_proposals(
        [
            _event(
                f"success-{index}",
                outcome="succeeded",
                error_kind=SkillErrorKind.NONE,
            )
            for index in range(3)
        ],
        repeated_threshold=3,
    )[0]
    store.submit(proposal)

    assert target.read_text(encoding="utf-8") == "validation: smoke-only\n"

    receipt = store._approve_and_apply(
        proposal.proposal_id,
        approver="human-reviewer",
        target_path=target,
        apply_change=lambda before, _proposal: before.replace(
            b"smoke-only", b"fixture-validated"
        ),
        validators={
            "representation": lambda path: "fixture-validated" in path.read_text(encoding="utf-8") or (_ for _ in ()).throw(AssertionError("not validated")),
            "execution": lambda _path: None,
            "retrieval": lambda _path: None,
        },
    )

    assert receipt.status == "approved"
    assert receipt.before_hash != receipt.after_hash
    assert target.read_text(encoding="utf-8") == "validation: fixture-validated\n"


def test_failed_revalidation_rolls_back_exact_bytes(tmp_path: Path):
    target = tmp_path / "skill.yaml"
    original = b"validation: smoke-only\n"
    target.write_bytes(original)
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    proposal = generate_evolution_proposals(
        [
            _event(
                f"success-{index}",
                outcome="succeeded",
                error_kind=SkillErrorKind.NONE,
            )
            for index in range(3)
        ],
        repeated_threshold=3,
    )[0]
    store.submit(proposal)

    def reject(_path: Path) -> None:
        raise AssertionError("routing regression")

    with pytest.raises(EvolutionApplyError, match="routing regression"):
        store._approve_and_apply(
            proposal.proposal_id,
            approver="human-reviewer",
            target_path=target,
            apply_change=lambda before, _proposal: before + b"changed: true\n",
            validators={
                "representation": lambda _path: None,
                "execution": lambda _path: None,
                "retrieval": reject,
            },
        )

    assert target.read_bytes() == original
    assert store.get(proposal.proposal_id).status == "rolled_back"


def test_run_result_exposes_typed_error_without_changing_legacy_shape():
    result = build_skill_run_result(
        skill="sc-test",
        success=False,
        exit_code=1,
        output_dir=None,
        stderr="No module named scanpy",
    )

    assert result.error_kind == "missing_dependency"
    assert "error_kind" not in result.to_legacy_dict()


def test_run_event_fingerprints_error_evidence_without_storing_paths_or_secrets():
    result = build_skill_run_result(
        skill="sc-test",
        success=False,
        exit_code=1,
        output_dir=None,
        stderr="failed at /private/patient/data.h5ad token=super-secret",
    )

    event = SkillRunEvent.from_result(result)
    serialized = str(event.to_dict())

    assert event.evidence_refs[0].startswith("stderr:sha256:")
    assert event.run_id == ""
    assert event.evidence_kind == "ordinary"
    assert "/private/patient" not in serialized
    assert "super-secret" not in serialized


def test_run_event_never_uses_unresolved_caller_text_as_skill_identity():
    result = build_skill_run_result(
        skill="/home/alice/patient_A.h5ad token=super-secret",
        success=False,
        exit_code=-1,
        output_dir=None,
        stderr="unknown skill",
    )

    event = SkillRunEvent.from_result(result)
    serialized = str(event.to_dict())

    assert event.skill_id.startswith("unresolved-")
    assert "/home/alice" not in serialized
    assert "super-secret" not in serialized


def test_recorded_run_event_binds_manifest_and_runtime_entry_hashes(tmp_path: Path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    script = skill_dir / "entry.py"
    manifest = skill_dir / "skill.yaml"
    script.write_text("print('ok')\n", encoding="utf-8")
    manifest.write_text("id: source-bound\n", encoding="utf-8")
    result = build_skill_run_result(
        skill="source-bound",
        success=True,
        exit_code=0,
        output_dir=None,
        stderr="",
    )

    event = record_skill_run_result(
        result,
        skill_info={"script": script, "version": "1.0.0"},
        skills_root=tmp_path,
        ledger=SkillHealthLedger(tmp_path / "events.jsonl"),
    )

    assert event.skill_hash == evolution_module._sha256(manifest.read_bytes())
    assert event.source_hash == evolution_module.compute_execution_source_hash(
        script,
        skills_root=tmp_path,
    )


def test_recorded_run_event_refuses_to_guess_an_execution_source_root(
    tmp_path: Path,
):
    skill_dir = tmp_path / "skills" / "singlecell" / "source-bound"
    skill_dir.mkdir(parents=True)
    script = skill_dir / "entry.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text("id: source-bound\n", encoding="utf-8")
    result = build_skill_run_result(
        skill="source-bound",
        success=True,
        exit_code=0,
        output_dir=None,
    )

    with pytest.raises(ValueError, match="canonical skills_root"):
        record_skill_run_result(
            result,
            skill_info={"script": script, "version": "1.0.0"},
            ledger=SkillHealthLedger(tmp_path / "events.jsonl"),
        )

    assert not (tmp_path / "events.jsonl").exists()


def test_recorded_run_event_never_persists_result_json_key_names(tmp_path: Path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    script = skill_dir / "entry.py"
    script.write_text("raise RuntimeError('failed')\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text(
        "id: source-bound\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "result.json").write_text(
        '{"/private/patient_A": 1, "api_key=super-secret": 2}',
        encoding="utf-8",
    )
    result = build_skill_run_result(
        skill="source-bound",
        success=False,
        exit_code=1,
        output_dir=output_dir,
        stderr="RuntimeError: failed",
    )

    event = record_skill_run_result(
        result,
        skill_info={"script": script, "version": "1.0.0"},
        skills_root=tmp_path,
        ledger=SkillHealthLedger(tmp_path / "events.jsonl"),
    )
    serialized = str(event.to_dict())

    assert all(not ref.startswith("result_keys:") for ref in event.evidence_refs)
    assert "/private/patient_A" not in serialized
    assert "super-secret" not in serialized


def test_recorded_traceback_refs_only_include_the_canonical_runtime_entry(
    tmp_path: Path,
):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    script = skill_dir / "entry.py"
    script.write_text("raise RuntimeError('failed')\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text(
        "id: source-bound\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    result = build_skill_run_result(
        skill="source-bound",
        success=False,
        exit_code=1,
        output_dir=None,
        stderr=(
            '  File "/private/patient_A.py", line 7, in <module>\n'
            '  File "/private/entry.py", line 11, in decoy\n'
            f'  File "{script}", line 13, in main\n'
            "RuntimeError: failed"
        ),
    )

    event = record_skill_run_result(
        result,
        skill_info={"script": script, "version": "1.0.0"},
        skills_root=tmp_path,
        ledger=SkillHealthLedger(tmp_path / "events.jsonl"),
    )
    serialized = str(event.to_dict())

    assert "trace:entry.py:13" in event.evidence_refs
    assert "trace:entry.py:11" not in event.evidence_refs
    assert all("patient_A.py" not in ref for ref in event.evidence_refs)
    assert "patient_A.py" not in serialized


def test_health_event_preserves_runner_producer_environment_identity(
    tmp_path: Path,
):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    script = skill_dir / "entry.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text("id: source-bound\n", encoding="utf-8")
    result = build_skill_run_result(
        skill="source-bound",
        success=True,
        exit_code=0,
        output_dir=None,
    )
    producer_environment_id = "env:" + "a" * 20

    event = record_skill_run_result(
        result,
        skill_info={"script": script, "version": "1.0.0", "requires": ["scanpy"]},
        skills_root=tmp_path,
        environment_id=producer_environment_id,
        ledger=SkillHealthLedger(tmp_path / "events.jsonl"),
    )

    assert event.environment_id == producer_environment_id


def test_execution_source_hash_covers_skill_python_tree_and_domain_library(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    domain_dir = skills_root / "singlecell"
    skill_dir = domain_dir / "source-bound"
    lib_dir = domain_dir / "_lib"
    nested_dir = skill_dir / "helpers"
    nested_dir.mkdir(parents=True)
    lib_dir.mkdir(parents=True)
    script = skill_dir / "entry.py"
    helper = nested_dir / "helper.py"
    shared = lib_dir / "shared.py"
    notes = skill_dir / "notes.txt"
    script.write_text("print('entry')\n", encoding="utf-8")
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    shared.write_text("SHARED = 1\n", encoding="utf-8")
    notes.write_text("not executable source\n", encoding="utf-8")

    original = evolution_module.compute_execution_source_hash(
        script,
        skills_root=skills_root,
    )
    assert original == evolution_module.compute_execution_source_hash(
        script,
        skills_root=skills_root,
    )

    helper.write_text("VALUE = 2\n", encoding="utf-8")
    helper_revision = evolution_module.compute_execution_source_hash(
        script,
        skills_root=skills_root,
    )
    assert helper_revision != original

    shared.write_text("SHARED = 2\n", encoding="utf-8")
    shared_revision = evolution_module.compute_execution_source_hash(
        script,
        skills_root=skills_root,
    )
    assert shared_revision != helper_revision

    notes.write_text("documentation changed\n", encoding="utf-8")
    assert (
        evolution_module.compute_execution_source_hash(
            script,
            skills_root=skills_root,
        )
        == shared_revision
    )


def test_execution_source_hash_always_covers_bash_runtime_entry(tmp_path: Path):
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "genomics" / "bash-source-bound"
    skill_dir.mkdir(parents=True)
    entry = skill_dir / "run.sh"
    entry.write_text("#!/usr/bin/env bash\necho one\n", encoding="utf-8")

    original = evolution_module.compute_execution_source_hash(
        entry,
        skills_root=skills_root,
    )
    entry.write_text("#!/usr/bin/env bash\necho two\n", encoding="utf-8")

    assert (
        evolution_module.compute_execution_source_hash(
            entry,
            skills_root=skills_root,
        )
        != original
    )


def test_execution_source_hash_covers_conservative_skill_runtime_assets(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "singlecell" / "asset-source-bound"
    prompt = skill_dir / "prompts" / "analysis.tmpl"
    marker = skill_dir / "markers" / "cell_types.tsv"
    large_runtime_asset = skill_dir / "markers" / "large.json"
    cache_asset = skill_dir / "__pycache__" / "cached.json"
    output_asset = skill_dir / "output" / "result.yaml"
    test_source = skill_dir / "tests" / "test_entry.py"
    reference_asset = skill_dir / "references" / "example.tmpl"
    large_demo_asset = skill_dir / "data" / "large-demo.h5ad"
    for path in (
        prompt,
        marker,
        large_runtime_asset,
        cache_asset,
        output_asset,
        test_source,
        reference_asset,
        large_demo_asset,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
    entry = skill_dir / "entry.py"
    entry.write_text("print('entry')\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text(
        "id: asset-source-bound\n",
        encoding="utf-8",
    )
    prompt.write_text("prompt revision one\n", encoding="utf-8")
    marker.write_text("gene\tcell_type\nCD3D\tT cell\n", encoding="utf-8")
    large_runtime_asset.write_bytes(b"x" * (9 * 1024 * 1024))
    cache_asset.write_text('{"revision": 1}\n', encoding="utf-8")
    output_asset.write_text("revision: 1\n", encoding="utf-8")
    test_source.write_text("REVISION = 1\n", encoding="utf-8")
    reference_asset.write_text("reference revision one\n", encoding="utf-8")
    large_demo_asset.write_bytes(b"x" * (9 * 1024 * 1024))

    previous = evolution_module.compute_execution_source_hash(
        entry,
        skills_root=skills_root,
    )
    for asset, revision in (
        (prompt, "prompt revision two\n"),
        (marker, "gene\tcell_type\nMS4A1\tB cell\n"),
    ):
        asset.write_text(revision, encoding="utf-8")
        current = evolution_module.compute_execution_source_hash(
            entry,
            skills_root=skills_root,
        )
        assert current != previous
        previous = current

    large_runtime_asset.write_bytes(b"y" * (9 * 1024 * 1024))
    current = evolution_module.compute_execution_source_hash(
        entry,
        skills_root=skills_root,
    )
    assert current != previous
    previous = current

    cache_asset.write_text('{"revision": 2}\n', encoding="utf-8")
    output_asset.write_text("revision: 2\n", encoding="utf-8")
    test_source.write_text("REVISION = 2\n", encoding="utf-8")
    reference_asset.write_text("reference revision two\n", encoding="utf-8")
    large_demo_asset.write_bytes(b"y" * (9 * 1024 * 1024))
    assert (
        evolution_module.compute_execution_source_hash(
            entry,
            skills_root=skills_root,
        )
        == previous
    )


def test_execution_source_hash_covers_project_runtime_and_script_assets(
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    skills_root = workspace / "skills"
    skill_dir = skills_root / "singlecell" / "runtime-prompt-bound"
    prompt = (
        workspace
        / "omicsclaw"
        / "runtime"
        / "consensus"
        / "narrative"
        / "prompts"
        / "system.tmpl"
    )
    script_asset = workspace / "scripts" / "prompts" / "demo.yaml"
    skill_dir.mkdir(parents=True)
    prompt.parent.mkdir(parents=True)
    script_asset.parent.mkdir(parents=True)
    entry = skill_dir / "entry.py"
    entry.write_text("print('entry')\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text(
        "id: runtime-prompt-bound\n",
        encoding="utf-8",
    )
    prompt.write_text("prompt revision one\n", encoding="utf-8")
    script_asset.write_text("revision: one\n", encoding="utf-8")

    previous = evolution_module.compute_execution_source_hash(
        entry,
        skills_root=skills_root,
        skill_dir=skill_dir,
    )
    for asset, revision in (
        (prompt, "prompt revision two\n"),
        (script_asset, "revision: two\n"),
    ):
        asset.write_text(revision, encoding="utf-8")
        current = evolution_module.compute_execution_source_hash(
            entry,
            skills_root=skills_root,
            skill_dir=skill_dir,
        )
        assert current != previous
        previous = current


@pytest.mark.parametrize("subdomain", ["scrna", "scatac"])
def test_execution_source_hash_covers_nested_singlecell_skill_and_domain_library(
    tmp_path: Path,
    subdomain: str,
):
    skills_root = tmp_path / "skills"
    domain_dir = skills_root / "singlecell"
    skill_dir = domain_dir / subdomain / "sc-source-bound"
    lib_dir = domain_dir / "_lib"
    helper_dir = skill_dir / "helpers"
    helper_dir.mkdir(parents=True)
    lib_dir.mkdir(parents=True)
    script = skill_dir / "entry.py"
    helper = helper_dir / "helper.py"
    shared = lib_dir / "shared.py"
    (skill_dir / "skill.yaml").write_text(
        "schema_version: '2.0'\nid: sc-source-bound\n",
        encoding="utf-8",
    )
    script.write_text("print('entry')\n", encoding="utf-8")
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    shared.write_text("SHARED = 1\n", encoding="utf-8")

    original = evolution_module.compute_execution_source_hash(
        script,
        skills_root=skills_root,
    )

    helper.write_text("VALUE = 2\n", encoding="utf-8")
    skill_revision = evolution_module.compute_execution_source_hash(
        script,
        skills_root=skills_root,
    )
    assert skill_revision != original

    shared.write_text("SHARED = 2\n", encoding="utf-8")
    assert (
        evolution_module.compute_execution_source_hash(
            script,
            skills_root=skills_root,
        )
        != skill_revision
    )


def test_execution_source_hash_uses_only_canonical_root_libraries_without_shadowing(
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    skills_root = workspace / "skills"
    domain_dir = skills_root / "singlecell"
    subdomain_dir = domain_dir / "scrna"
    skill_dir = subdomain_dir / "sc-source-bound"
    domain_lib = domain_dir / "_lib"
    subdomain_lib = subdomain_dir / "_lib"
    outer_lib = workspace / "_lib"
    for path in (skill_dir, domain_lib, subdomain_lib, outer_lib):
        path.mkdir(parents=True, exist_ok=True)
    script = skill_dir / "entry.py"
    script.write_text("print('entry')\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text(
        "schema_version: '2.0'\nid: sc-source-bound\n",
        encoding="utf-8",
    )
    domain_shared = domain_lib / "shared.py"
    subdomain_shared = subdomain_lib / "shared.py"
    outer_shared = outer_lib / "shared.py"
    domain_shared.write_text("DOMAIN = 1\n", encoding="utf-8")
    subdomain_shared.write_text("SUBDOMAIN = 1\n", encoding="utf-8")
    outer_shared.write_text("OUTER = 1\n", encoding="utf-8")

    original = evolution_module.compute_execution_source_hash(
        script,
        skills_root=skills_root,
    )

    outer_shared.write_text("OUTER = 2\n", encoding="utf-8")
    assert (
        evolution_module.compute_execution_source_hash(
            script,
            skills_root=skills_root,
        )
        == original
    )

    domain_shared.write_text("DOMAIN = 2\n", encoding="utf-8")
    domain_revision = evolution_module.compute_execution_source_hash(
        script,
        skills_root=skills_root,
    )
    assert domain_revision != original

    subdomain_shared.write_text("SUBDOMAIN = 2\n", encoding="utf-8")
    assert (
        evolution_module.compute_execution_source_hash(
            script,
            skills_root=skills_root,
        )
        != domain_revision
    )


def test_execution_source_hash_covers_root_bounded_r_source_closure(
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    skills_root = workspace / "skills"
    domain_dir = skills_root / "singlecell"
    skill_dir = domain_dir / "scrna" / "sc-source-bound"
    domain_lib = domain_dir / "_lib"
    project_r_scripts = workspace / "omicsclaw" / "r_scripts"
    unrelated_dir = workspace / "unrelated"
    for path in (skill_dir, domain_lib, project_r_scripts, unrelated_dir):
        path.mkdir(parents=True, exist_ok=True)

    script = skill_dir / "entry.py"
    skill_r_source = skill_dir / "helper.R"
    domain_r_source = domain_lib / "shared.r"
    project_r_source = project_r_scripts / "shared.R"
    unrelated_r_source = unrelated_dir / "outside.R"
    script.write_text("print('entry')\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text(
        "schema_version: '2.0'\nid: sc-source-bound\n",
        encoding="utf-8",
    )
    skill_r_source.write_text("SKILL_REVISION <- 1\n", encoding="utf-8")
    domain_r_source.write_text("DOMAIN_REVISION <- 1\n", encoding="utf-8")
    project_r_source.write_text("PROJECT_REVISION <- 1\n", encoding="utf-8")
    unrelated_r_source.write_text("OUTSIDE_REVISION <- 1\n", encoding="utf-8")

    original = evolution_module.compute_execution_source_hash(
        script,
        skills_root=skills_root,
    )

    skill_r_source.write_text("SKILL_REVISION <- 2\n", encoding="utf-8")
    skill_revision = evolution_module.compute_execution_source_hash(
        script,
        skills_root=skills_root,
    )
    assert skill_revision != original

    domain_r_source.write_text("DOMAIN_REVISION <- 2\n", encoding="utf-8")
    domain_revision = evolution_module.compute_execution_source_hash(
        script,
        skills_root=skills_root,
    )
    assert domain_revision != skill_revision

    project_r_source.write_text("PROJECT_REVISION <- 2\n", encoding="utf-8")
    project_revision = evolution_module.compute_execution_source_hash(
        script,
        skills_root=skills_root,
    )
    assert project_revision != domain_revision

    unrelated_r_source.write_text("OUTSIDE_REVISION <- 2\n", encoding="utf-8")
    assert (
        evolution_module.compute_execution_source_hash(
            script,
            skills_root=skills_root,
        )
        == project_revision
    )


def test_execution_source_hash_covers_only_the_canonical_project_runtime(
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    skills_root = workspace / "skills"
    skill_dir = skills_root / "singlecell" / "scrna" / "sc-source-bound"
    project_runtime = workspace / "omicsclaw"
    unrelated_runtime = tmp_path / "unrelated" / "omicsclaw"
    skill_dir.mkdir(parents=True)

    script = skill_dir / "entry.py"
    script.write_text("print('entry')\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text(
        "schema_version: '2.0'\nid: sc-source-bound\n",
        encoding="utf-8",
    )
    runtime_sources = [
        project_runtime / "common" / "report.py",
        project_runtime / "core" / "r_script_runner.py",
        project_runtime / "providers" / "chat_completion.py",
    ]
    for index, source in enumerate(runtime_sources, start=1):
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"REVISION = {index}\n", encoding="utf-8")
    unrelated_source = unrelated_runtime / "common" / "report.py"
    unrelated_source.parent.mkdir(parents=True)
    unrelated_source.write_text("REVISION = 1\n", encoding="utf-8")

    previous = evolution_module.compute_execution_source_hash(
        script,
        skills_root=skills_root,
    )
    for index, source in enumerate(runtime_sources, start=11):
        source.write_text(f"REVISION = {index}\n", encoding="utf-8")
        current = evolution_module.compute_execution_source_hash(
            script,
            skills_root=skills_root,
        )
        assert current != previous
        previous = current

    unrelated_source.write_text("REVISION = 2\n", encoding="utf-8")
    assert (
        evolution_module.compute_execution_source_hash(
            script,
            skills_root=skills_root,
        )
        == previous
    )


def test_execution_source_hash_covers_fixed_project_execution_roots(
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    skills_root = workspace / "skills"
    skill_dir = skills_root / "spatial" / "spatial-enrichment"
    sibling_skill_dir = skills_root / "spatial" / "spatial-preprocess"
    project_scripts = workspace / "scripts"
    unrelated_project = tmp_path / "unrelated"
    for path in (skill_dir, sibling_skill_dir, project_scripts, unrelated_project):
        path.mkdir(parents=True, exist_ok=True)

    script = skill_dir / "spatial_enrichment.py"
    sibling_source = sibling_skill_dir / "spatial_preprocess.py"
    sibling_manifest = sibling_skill_dir / "skill.yaml"
    sibling_parameters = sibling_skill_dir / "parameters.yaml"
    project_script = project_scripts / "generate_demo_data.py"
    project_entry = workspace / "omicsclaw.py"
    unrelated_source = unrelated_project / "generate_demo_data.py"
    script.write_text("print('enrichment')\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text(
        "schema_version: '2.0'\nid: spatial-enrichment\n",
        encoding="utf-8",
    )
    sibling_source.write_text("REVISION = 1\n", encoding="utf-8")
    sibling_manifest.write_text(
        "schema_version: '2.0'\nid: spatial-preprocess\nversion: '1'\n",
        encoding="utf-8",
    )
    sibling_parameters.write_text("resolution: 0.5\n", encoding="utf-8")
    project_script.write_text("REVISION = 1\n", encoding="utf-8")
    project_entry.write_text("REVISION = 1\n", encoding="utf-8")
    unrelated_source.write_text("REVISION = 1\n", encoding="utf-8")

    previous = evolution_module.compute_execution_source_hash(
        script,
        skills_root=skills_root,
    )
    revisions = (
        (
            sibling_manifest,
            "schema_version: '2.0'\nid: spatial-preprocess\nversion: '2'\n",
        ),
        (sibling_source, "REVISION = 2\n"),
        (project_script, "REVISION = 2\n"),
        (project_entry, "REVISION = 2\n"),
    )
    for source, revision in revisions:
        source.write_text(revision, encoding="utf-8")
        current = evolution_module.compute_execution_source_hash(
            script,
            skills_root=skills_root,
        )
        assert current != previous
        previous = current

    sibling_parameters.write_text("resolution: 1.0\n", encoding="utf-8")
    assert (
        evolution_module.compute_execution_source_hash(
            script,
            skills_root=skills_root,
        )
        == previous
    )

    unrelated_source.write_text("REVISION = 2\n", encoding="utf-8")
    assert (
        evolution_module.compute_execution_source_hash(
            script,
            skills_root=skills_root,
        )
        == previous
    )


@pytest.mark.parametrize(
    "symlink_kind",
    ["file", "manifest", "directory", "broken"],
)
def test_execution_source_hash_rejects_symlinked_project_sources(
    tmp_path: Path,
    symlink_kind: str,
):
    workspace = tmp_path / "workspace"
    skills_root = workspace / "skills"
    skill_dir = skills_root / "spatial" / "spatial-enrichment"
    external_dir = tmp_path / "external-sources"
    skill_dir.mkdir(parents=True)
    external_dir.mkdir()
    script = skill_dir / "spatial_enrichment.py"
    script.write_text("print('enrichment')\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text(
        "schema_version: '2.0'\nid: spatial-enrichment\n",
        encoding="utf-8",
    )

    if symlink_kind == "file":
        target = external_dir / "shared.py"
        target.write_text("REVISION = 1\n", encoding="utf-8")
        link = skill_dir / "shared.py"
        target_is_directory = False
    elif symlink_kind == "manifest":
        target = external_dir / "skill.yaml"
        target.write_text("id: external-skill\n", encoding="utf-8")
        link = skills_root / "spatial" / "spatial-preprocess" / "skill.yaml"
        link.parent.mkdir(parents=True)
        target_is_directory = False
    elif symlink_kind == "directory":
        target = external_dir / "shared"
        target.mkdir()
        (target / "shared.py").write_text("REVISION = 1\n", encoding="utf-8")
        link = skills_root / "spatial" / "spatial-preprocess"
        target_is_directory = True
    else:
        target = external_dir / "missing.py"
        link = skill_dir / "missing.py"
        target_is_directory = False
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as exc:  # pragma: no cover - host policy may forbid symlinks
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(ValueError, match="symbolic link"):
        evolution_module.compute_execution_source_hash(
            script,
            skills_root=skills_root,
        )


@pytest.mark.parametrize(
    "mutation_kind",
    ["content", "manifest_content", "inventory"],
)
def test_execution_source_hash_rejects_concurrent_source_tree_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation_kind: str,
):
    skills_root = tmp_path / "workspace" / "skills"
    skill_dir = skills_root / "spatial" / "spatial-enrichment"
    skill_dir.mkdir(parents=True)
    script = skill_dir / "spatial_enrichment.py"
    script.write_text("REVISION = 1\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text(
        "schema_version: '2.0'\nid: spatial-enrichment\n",
        encoding="utf-8",
    )

    original_read = evolution_module.os.read
    manifest = skill_dir / "skill.yaml"
    script_stat = script.stat()
    script_identity = (script_stat.st_dev, script_stat.st_ino)
    manifest_stat = manifest.stat()
    manifest_identity = (manifest_stat.st_dev, manifest_stat.st_ino)
    mutated = False

    def mutate_during_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        payload = original_read(descriptor, size)
        if payload and not mutated:
            descriptor_stat = evolution_module.os.fstat(descriptor)
            descriptor_identity = (
                descriptor_stat.st_dev,
                descriptor_stat.st_ino,
            )
            expected_identity = {
                "content": script_identity,
                "manifest_content": manifest_identity,
            }.get(mutation_kind)
            if expected_identity is not None and descriptor_identity != expected_identity:
                return payload
            mutated = True
            if mutation_kind == "content":
                script.write_text("REVISION = 2\n", encoding="utf-8")
            elif mutation_kind == "manifest_content":
                manifest.write_text(
                    "schema_version: '2.0'\nid: spatial-enrichment\nversion: '2'\n",
                    encoding="utf-8",
                )
            else:
                (skill_dir / "late_source.py").write_text(
                    "LATE = True\n",
                    encoding="utf-8",
                )
        return payload

    monkeypatch.setattr(evolution_module.os, "read", mutate_during_read)

    with pytest.raises(ValueError, match="changed during hashing"):
        evolution_module.compute_execution_source_hash(
            script,
            skills_root=skills_root,
        )
    assert mutated is True


def test_capture_execution_identity_rejects_manifest_change_before_source_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "spatial" / "spatial-enrichment"
    skill_dir.mkdir(parents=True)
    script = skill_dir / "spatial_enrichment.py"
    manifest = skill_dir / "skill.yaml"
    script.write_text("print('enrichment')\n", encoding="utf-8")
    manifest.write_text(
        "schema_version: '2.0'\nid: spatial-enrichment\nversion: '1'\n",
        encoding="utf-8",
    )

    original_compute = evolution_module.compute_execution_source_hash
    mutated = False

    def mutate_before_source_scan(*args, **kwargs):
        nonlocal mutated
        mutated = True
        manifest.write_text(
            "schema_version: '2.0'\nid: spatial-enrichment\nversion: '2'\n",
            encoding="utf-8",
        )
        return original_compute(*args, **kwargs)

    monkeypatch.setattr(
        evolution_module,
        "compute_execution_source_hash",
        mutate_before_source_scan,
    )

    with pytest.raises(ValueError, match="manifest changed during identity capture"):
        evolution_module.capture_skill_execution_identity(
            script,
            skills_root=skills_root,
            skill_dir=skill_dir,
        )
    assert mutated is True


def test_capture_execution_identity_rejects_broken_bound_manifest_symlink(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "spatial" / "spatial-enrichment"
    skill_dir.mkdir(parents=True)
    script = skill_dir / "spatial_enrichment.py"
    script.write_text("print('enrichment')\n", encoding="utf-8")
    manifest = skill_dir / "skill.yaml"
    try:
        manifest.symlink_to(tmp_path / "missing-skill.yaml")
    except OSError as exc:  # pragma: no cover - host policy may forbid symlinks
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(ValueError, match="symbolic link"):
        evolution_module.capture_skill_execution_identity(
            script,
            skills_root=skills_root,
            skill_dir=skill_dir,
        )


def test_execution_identity_uses_outer_skill_manifest_for_nested_runtime_entry(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "singlecell" / "nested-runtime"
    runtime_dir = skill_dir / "runtime"
    runtime_dir.mkdir(parents=True)
    script = runtime_dir / "entry.py"
    sibling_helper = skill_dir / "helper.py"
    canonical_manifest = skill_dir / "skill.yaml"
    decoy_manifest = runtime_dir / "skill.yaml"
    script.write_text("print('entry')\n", encoding="utf-8")
    sibling_helper.write_text("HELPER = 1\n", encoding="utf-8")
    canonical_manifest.write_text("id: nested-runtime\n", encoding="utf-8")
    decoy_manifest.write_text("id: decoy\n", encoding="utf-8")

    manifest_hash, original_source_hash = (
        evolution_module.capture_skill_execution_identity(
            script,
            skills_root=skills_root,
            skill_dir=skill_dir,
        )
    )

    assert manifest_hash == evolution_module._sha256(canonical_manifest.read_bytes())
    sibling_helper.write_text("HELPER = 2\n", encoding="utf-8")
    assert (
        evolution_module.compute_execution_source_hash(
            script,
            skills_root=skills_root,
            skill_dir=skill_dir,
        )
        != original_source_hash
    )


def test_execution_identity_uses_registry_directory_when_domain_is_also_a_skill(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    domain_skill = skills_root / "orchestrator"
    nested_skill = domain_skill / "omics-skill-builder"
    nested_skill.mkdir(parents=True)
    (domain_skill / "skill.yaml").write_text(
        "id: orchestrator\n",
        encoding="utf-8",
    )
    domain_entry = domain_skill / "omics_orchestrator.py"
    domain_entry.write_text("ROOT = 1\n", encoding="utf-8")
    nested_manifest = nested_skill / "skill.yaml"
    nested_manifest.write_text("id: omics-skill-builder\n", encoding="utf-8")
    nested_entry = nested_skill / "omics_skill_builder.py"
    nested_entry.write_text("print('builder')\n", encoding="utf-8")

    manifest_hash, original_source_hash = (
        evolution_module.capture_skill_execution_identity(
            nested_entry,
            skills_root=skills_root,
            directory_name="omics-skill-builder",
        )
    )

    assert manifest_hash == evolution_module._sha256(nested_manifest.read_bytes())
    domain_entry.write_text("ROOT = 2\n", encoding="utf-8")
    assert (
        evolution_module.compute_execution_source_hash(
            nested_entry,
            skills_root=skills_root,
            skill_dir=nested_skill,
        )
        != original_source_hash
    )


def test_canonical_domain_library_keeps_the_historical_hash_namespace(
    tmp_path: Path,
):
    import hashlib

    skills_root = tmp_path / "skills"
    domain_dir = skills_root / "singlecell"
    skill_dir = domain_dir / "source-bound"
    domain_lib = domain_dir / "_lib"
    skill_dir.mkdir(parents=True)
    domain_lib.mkdir(parents=True)
    script = skill_dir / "entry.py"
    script.write_text("print('entry')\n", encoding="utf-8")
    (skill_dir / "helper.py").write_text("HELPER = 1\n", encoding="utf-8")
    (domain_lib / "shared.py").write_text("SHARED = 1\n", encoding="utf-8")

    digest = hashlib.sha256()
    legacy_sources = []
    for namespace, root in (("skill", skill_dir), ("domain_lib", domain_lib)):
        legacy_sources.extend(
            (namespace, path.relative_to(root), path)
            for path in root.rglob("*.py")
            if path.is_file()
        )
    for namespace, relative_path, path in sorted(
        legacy_sources,
        key=lambda item: (item[0], item[1].as_posix()),
    ):
        identity = f"{namespace}/{relative_path.as_posix()}".encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(identity).to_bytes(8, "big"))
        digest.update(identity)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)

    assert evolution_module.compute_execution_source_hash(
        script,
        skills_root=skills_root,
    ) == "sha256:" + digest.hexdigest()


def test_old_run_event_rows_remain_readable_but_do_not_become_demo_evidence():
    original = _event("legacy").to_dict()
    original.pop("evidence_kind")
    original.pop("execution_fingerprint")
    original.pop("source_hash")

    restored = SkillRunEvent.from_dict(original)

    assert restored.evidence_kind == "ordinary"
    assert restored.execution_fingerprint == ""
    assert restored.source_hash == ""


def test_approval_refuses_writeback_without_revalidation(tmp_path: Path):
    target = tmp_path / "skill.yaml"
    original = b"validation: smoke-only\n"
    target.write_bytes(original)
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    proposal = generate_evolution_proposals(
        [
            _event(
                f"success-{index}",
                outcome="succeeded",
                error_kind=SkillErrorKind.NONE,
            )
            for index in range(3)
        ],
        repeated_threshold=3,
    )[0]
    store.submit(proposal)

    with pytest.raises(ValueError, match="validator"):
        store._approve_and_apply(
            proposal.proposal_id,
            approver="human-reviewer",
            target_path=target,
            apply_change=lambda before, _proposal: before + b"changed: true\n",
            validators={},
        )

    assert target.read_bytes() == original


def test_approval_refuses_unexpected_validation_stage(tmp_path: Path):
    target = tmp_path / "skill.yaml"
    original = b"validation: smoke-only\n"
    target.write_bytes(original)
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    proposal = generate_evolution_proposals(
        [
            _event(
                f"success-{index}",
                outcome="succeeded",
                error_kind=SkillErrorKind.NONE,
            )
            for index in range(3)
        ],
        repeated_threshold=3,
    )[0]
    store.submit(proposal)

    with pytest.raises(ValueError, match="unexpected"):
        store._approve_and_apply(
            proposal.proposal_id,
            approver="human-reviewer",
            target_path=target,
            apply_change=lambda before, _proposal: before + b"changed: true\n",
            validators={
                "representation": lambda _path: None,
                "execution": lambda _path: None,
                "retrieval": lambda _path: None,
                "bonus": lambda _path: None,
            },
        )

    assert target.read_bytes() == original


def test_shared_runner_error_seam_records_typed_health_event(tmp_path: Path, monkeypatch):
    from omicsclaw.skill.runner import _err

    ledger_path = tmp_path / "runner-events.jsonl"
    monkeypatch.setenv("OMICSCLAW_SKILL_HEALTH_LEDGER", str(ledger_path))

    result = _err(
        "sc-test",
        "No module named scanpy",
        skill_info={"alias": "sc-test"},
    )
    events = SkillHealthLedger(ledger_path).events()

    assert result.error_kind == "missing_dependency"
    assert len(events) == 1
    assert events[0].skill_id == "sc-test"
    assert events[0].error_kind == "missing_dependency"
    assert events[0].evidence_refs[0].startswith("stderr:sha256:")
