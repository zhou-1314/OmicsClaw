"""Tests for the ADR-0074 Evaluation Protocol schema + digest."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omicsclaw.skill.evaluation_protocol import protocol_digest
from omicsclaw.skill.schema import EvaluationProtocol, Validation


# ---- schema -----------------------------------------------------------------


def test_evaluation_protocol_parses_and_defaults():
    p = EvaluationProtocol(id="pbmc3k-fixture-v1", kind="fixture",
                           entry="tests/test_pbmc3k_fixture.py")
    assert p.repeats == 1
    assert p.dataset_ref is None
    assert p.runner == "pytest"
    assert p.pass_rule == "all_runs"


def test_protocol_runner_is_kind_bound_and_benchmark_is_single_case_for_now():
    demo = EvaluationProtocol(id="demo-v1", kind="demo", entry="skill.py")
    assert demo.runner == "shared_runner"
    with pytest.raises(ValidationError, match="shared_runner"):
        EvaluationProtocol(
            id="demo-v1", kind="demo", entry="skill.py", runner="pytest"
        )
    with pytest.raises(ValidationError, match="runner=command"):
        EvaluationProtocol(
            id="bench-v1",
            kind="benchmark",
            entry="tests/bench.py",
            suite_id="omicbench",
            case_ids=["A03_hvg"],
            dataset_ref={
                "store": "repository",
                "path": "data/benchmarks/case",
                "content_sha256": "sha256:" + "a" * 64,
            },
        )
    with pytest.raises(ValidationError, match="exactly one"):
        EvaluationProtocol(
            id="bench-v1",
            kind="benchmark",
            entry="tests/bench.py",
            runner="command",
            suite_id="omicbench",
            case_ids=["A03_hvg", "A04_pca"],
            dataset_ref={
                "store": "repository",
                "path": "data/benchmarks/case",
                "content_sha256": "sha256:" + "a" * 64,
            },
        )


def test_validation_carries_protocols_and_defaults_empty():
    assert Validation().protocols == []
    v = Validation(
        level="fixture-validated",
        protocols=[{"id": "p1", "kind": "fixture", "entry": "tests/t.py"}],
    )
    assert v.protocols[0].id == "p1"


def test_evaluation_protocol_rejects_unknown_field_and_bad_kind():
    with pytest.raises(ValidationError):
        EvaluationProtocol(id="p", kind="fixture", entry="t.py", bogus=1)
    with pytest.raises(ValidationError):
        EvaluationProtocol(id="p", kind="not-a-kind", entry="t.py")


def test_evaluation_protocol_bounds_repeats():
    with pytest.raises(ValidationError):
        EvaluationProtocol(id="p", kind="stability", entry="t.py", repeats=0)
    with pytest.raises(ValidationError):
        EvaluationProtocol(id="p", kind="stability", entry="t.py", repeats=1000)


def test_benchmark_protocol_requires_content_bound_suite_cases_and_dataset():
    digest = "sha256:" + "a" * 64
    protocol = EvaluationProtocol(
        id="omicbench-a03-v1",
        kind="benchmark",
        entry="tests/omicbench_a03.py",
        runner="command",
        suite_id="omicbench",
        case_ids=["A03_hvg"],
        dataset_ref={
            "store": "repository",
            "path": "data/benchmarks/omicbench/omicbench-A03_hvg",
            "content_sha256": digest,
        },
        metrics=["score"],
    )

    assert protocol.dataset_ref is not None
    assert protocol.dataset_ref.content_sha256 == digest
    assert protocol.case_ids == ["A03_hvg"]

    for missing in ("suite_id", "case_ids", "dataset_ref"):
        payload = protocol.model_dump()
        payload.pop(missing)
        with pytest.raises(ValidationError):
            EvaluationProtocol.model_validate(payload)


def test_dataset_reference_can_bind_a_non_overlapping_member_bundle():
    protocol = EvaluationProtocol(
        id="scagent-paga-v1",
        kind="benchmark",
        entry="tests/scagent_paga.py",
        runner="command",
        suite_id="scagent-bench",
        case_ids=["paga"],
        dataset_ref={
            "store": "repository",
            "path": "data/benchmarks/scagent-bench",
            "members": [
                "datasets/groundtruth/paga.csv",
                "datasets/inputs/paga",
                "graders/consistency.py",
            ],
            "content_sha256": "sha256:" + "a" * 64,
        },
    )
    assert protocol.dataset_ref is not None
    assert protocol.dataset_ref.members == [
        "datasets/groundtruth/paga.csv",
        "datasets/inputs/paga",
        "graders/consistency.py",
    ]

    payload = protocol.model_dump()
    payload["dataset_ref"]["members"] = ["datasets", "datasets/inputs/paga"]
    with pytest.raises(ValidationError, match="overlapping"):
        EvaluationProtocol.model_validate(payload)


def test_protocol_paths_and_ids_are_bounded_and_protocol_ids_are_unique():
    digest = "sha256:" + "b" * 64
    with pytest.raises(ValidationError):
        EvaluationProtocol(id="p", kind="fixture", entry="../escape.py")
    with pytest.raises(ValidationError):
        EvaluationProtocol(
            id="p",
            kind="benchmark",
            entry="tests/p.py",
            runner="command",
            suite_id="omicbench",
            case_ids=["A03_hvg"],
            dataset_ref={
                "store": "repository",
                "path": "/absolute/task",
                "content_sha256": digest,
            },
        )
    with pytest.raises(ValidationError):
        Validation(
            protocols=[
                {"id": "same", "kind": "fixture", "entry": "tests/a.py"},
                {"id": "same", "kind": "fixture", "entry": "tests/b.py"},
            ]
        )

    canonical = EvaluationProtocol(
        id="canonical-v1", kind="fixture", entry="tests/./nested//test.py"
    )
    assert canonical.entry == "tests/nested/test.py"


# ---- digest -----------------------------------------------------------------

_SPEC = {
    "id": "p1",
    "kind": "benchmark",
    "entry": "tests/t.py",
    "runner": "command",
    "suite_id": "omicbench",
    "case_ids": ["A03_hvg"],
    "dataset_ref": {
        "store": "repository",
        "path": "data/benchmarks/omicbench/omicbench-A03_hvg",
        "content_sha256": "sha256:" + "c" * 64,
    },
    "pass_rule": "all_runs",
    "repeats": 3,
}


def test_protocol_digest_shape_and_determinism():
    d1 = protocol_digest(protocol=_SPEC, entry_bytes=b"code",
                         dependency_versions={"scanpy": "1.10.0"})
    d2 = protocol_digest(protocol=dict(_SPEC), entry_bytes=b"code",
                         dependency_versions={"scanpy": "1.10.0"})
    assert d1 == d2
    assert d1.startswith("sha256:") and len(d1) == len("sha256:") + 64


def test_protocol_digest_is_dependency_order_independent():
    a = protocol_digest(protocol=_SPEC, entry_bytes=b"c",
                        dependency_versions={"a": "1", "b": "2"})
    b = protocol_digest(protocol=_SPEC, entry_bytes=b"c",
                        dependency_versions={"b": "2", "a": "1"})
    assert a == b


def test_protocol_digest_changes_on_entry_spec_or_dep_change():
    base = protocol_digest(protocol=_SPEC, entry_bytes=b"c",
                           dependency_versions={"scanpy": "1.10.0"})
    entry = protocol_digest(protocol=_SPEC, entry_bytes=b"CHANGED",
                            dependency_versions={"scanpy": "1.10.0"})
    spec = protocol_digest(protocol={**_SPEC, "repeats": 5}, entry_bytes=b"c",
                           dependency_versions={"scanpy": "1.10.0"})
    dep = protocol_digest(protocol=_SPEC, entry_bytes=b"c",
                          dependency_versions={"scanpy": "1.11.0"})
    assert len({base, entry, spec, dep}) == 4  # each change is distinct


# ---- AUD-10: metrics allowlist ----------------------------------------------


def test_metrics_allowlist_normalized_and_bounded():
    p = EvaluationProtocol(id="p", kind="stability", entry="t.py",
                           metrics=["silhouette", "ari", "silhouette", " runtime "])
    assert p.metrics == ["ari", "runtime", "silhouette"]  # sorted, deduped, trimmed
    with pytest.raises(ValidationError):
        EvaluationProtocol(id="p", kind="stability", entry="t.py",
                           metrics=[f"m{i}" for i in range(33)])
    with pytest.raises(ValidationError):
        EvaluationProtocol(id="p", kind="stability", entry="t.py", metrics=["x" * 65])


def test_protocol_digest_changes_on_metrics_allowlist():
    base = protocol_digest(protocol=_SPEC, entry_bytes=b"c")
    with_ari = protocol_digest(protocol={**_SPEC, "metrics": ["ari"]}, entry_bytes=b"c")
    with_sil = protocol_digest(protocol={**_SPEC, "metrics": ["silhouette"]}, entry_bytes=b"c")
    assert len({base, with_ari, with_sil}) == 3


def test_protocol_digest_changes_on_suite_case_or_dataset_identity():
    base = protocol_digest(protocol=_SPEC, entry_bytes=b"c")
    suite = protocol_digest(protocol={**_SPEC, "suite_id": "other"}, entry_bytes=b"c")
    cases = protocol_digest(protocol={**_SPEC, "case_ids": ["A04_pca"]}, entry_bytes=b"c")
    dataset_ref = {
        **_SPEC["dataset_ref"],
        "content_sha256": "sha256:" + "d" * 64,
    }
    dataset = protocol_digest(
        protocol={**_SPEC, "dataset_ref": dataset_ref},
        entry_bytes=b"c",
    )
    assert len({base, suite, cases, dataset}) == 4

    member_bundle = {
        **_SPEC["dataset_ref"],
        "members": ["inputs/paga", "graders/paga.py"],
    }
    selected = protocol_digest(
        protocol={**_SPEC, "dataset_ref": member_bundle},
        entry_bytes=b"c",
    )
    assert selected not in {base, suite, cases, dataset}


def test_protocol_digest_changes_on_runner_pass_rule_or_timeout():
    base = protocol_digest(protocol=_SPEC, entry_bytes=b"c")
    runner = protocol_digest(
        protocol={**_SPEC, "runner": "pytest"}, entry_bytes=b"c"
    )
    pass_rule = protocol_digest(
        protocol={**_SPEC, "pass_rule": "other"}, entry_bytes=b"c"
    )
    timeout = protocol_digest(
        protocol={**_SPEC, "timeout_seconds": 601}, entry_bytes=b"c"
    )
    assert len({base, runner, pass_rule, timeout}) == 4
