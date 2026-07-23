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


# ---- digest -----------------------------------------------------------------

_SPEC = {"id": "p1", "kind": "fixture", "entry": "tests/t.py",
         "dataset_ref": "fixture://pbmc3k", "repeats": 3}


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
