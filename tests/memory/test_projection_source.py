"""Unit tests for the ADR-0064 Run-manifest projection source reader."""

import hashlib

import pytest

from omicsclaw.control.projection_payload import (
    analysis_lineage_bytes,
    analysis_lineage_digest,
)
from omicsclaw.memory.projection_source import RunManifestSourceReader


def _manifest():
    return {
        "header": {
            "run_id": "a" * 32,
            "inputs": {"skill_id": "sc-de"},
            "scope": {"project_id": "proj1"},
            "parameters": {"resolution": 1.0},
            "skill_revision": {"skill_content_sha256": "c" * 64},
        },
        "completion": {
            "kind": "succeeded",
            "result_envelope_sha256": "e" * 64,
            "artifacts": [{"path": "result.json"}],
        },
    }


def test_reader_returns_canonical_bytes_for_run_store():
    manifest = _manifest()
    reader = RunManifestSourceReader(lambda ref: manifest)
    content = reader(source_store="run", source_ref="run-store://manifest/1")
    assert content == analysis_lineage_bytes(manifest)


def test_reader_roundtrips_the_frozen_digest():
    # The Producer freezes analysis_lineage_digest(manifest); the reader must
    # return bytes whose SHA-256 equals it — proving Producer/reader agree.
    manifest = _manifest()
    frozen_digest = analysis_lineage_digest(manifest)
    reader = RunManifestSourceReader(lambda ref: manifest)
    content = reader(source_store="run", source_ref="ref")
    assert hashlib.sha256(content).hexdigest() == frozen_digest


def test_reader_ignores_non_run_store():
    reader = RunManifestSourceReader(lambda ref: _manifest())
    assert reader(source_store="transcript", source_ref="x") is None


def test_reader_propagates_read_error_for_deferral():
    # A read error propagates so the driver DEFERS/retries (transient-safe)
    # rather than permanently failing the Intent as source_missing. The Run
    # Store cannot distinguish missing from transient, and v1 never deletes
    # Manifests, so deferral is the safe choice for both.
    def _raise(ref):
        raise RuntimeError("transient read fault")

    reader = RunManifestSourceReader(_raise)
    with pytest.raises(RuntimeError):
        reader(source_store="run", source_ref="x")


def test_reader_returns_none_on_non_mapping_manifest():
    reader = RunManifestSourceReader(lambda ref: "not-a-manifest")
    assert reader(source_store="run", source_ref="x") is None
