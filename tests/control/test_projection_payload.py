"""Unit tests for the ADR-0064 canonical analysis-lineage projection payload."""

import hashlib

from omicsclaw.control.projection_payload import (
    ANALYSIS_LINEAGE_KIND,
    analysis_lineage_bytes,
    analysis_lineage_digest,
    analysis_lineage_payload,
    canonical_projection_bytes,
)


def _manifest(*, project_id="proj1", run_id="a" * 32, skill_id="sc-de", artifacts=("result.json", "umap.png")):
    return {
        "schema_version": 1,
        "header": {
            "run_id": run_id,
            "run_submission_id": "b" * 32,
            "run_kind": "skill",
            "scope": {"kind": "project", "project_id": project_id},
            "inputs": {"skill_id": skill_id},
            "parameters": {"method": "leiden", "resolution": 1.0},
            "skill_revision": {"skill_content_sha256": "c" * 64},
        },
        "acceptance": {"state": "accepted"},
        "completion": {
            "kind": "succeeded",
            "committed_at_ms": 123,
            "assignment_id": "d" * 32,
            "result_envelope_sha256": "e" * 64,
            "artifacts": [{"path": p, "sha256": "f" * 64} for p in artifacts],
        },
    }


def test_payload_extracts_expected_fields():
    payload = analysis_lineage_payload(_manifest())
    assert payload["kind"] == ANALYSIS_LINEAGE_KIND
    assert payload["run_id"] == "a" * 32
    assert payload["skill_id"] == "sc-de"
    assert payload["project_id"] == "proj1"
    assert payload["terminal_status"] == "succeeded"
    assert payload["result_envelope_sha256"] == "e" * 64
    assert payload["artifacts"] == ["result.json", "umap.png"]
    assert payload["parameters"] == {"method": "leiden", "resolution": 1.0}


def test_bytes_are_deterministic():
    manifest = _manifest()
    assert analysis_lineage_bytes(manifest) == analysis_lineage_bytes(manifest)


def test_parameter_key_order_does_not_change_bytes():
    a = _manifest()
    a["header"]["parameters"] = {"a": 1, "b": 2, "nested": {"y": 1, "x": 2}}
    b = _manifest()
    b["header"]["parameters"] = {"b": 2, "nested": {"x": 2, "y": 1}, "a": 1}
    assert analysis_lineage_bytes(a) == analysis_lineage_bytes(b)


def test_artifacts_are_sorted_and_only_dicts_with_path():
    manifest = _manifest(artifacts=())
    manifest["completion"]["artifacts"] = [
        {"path": "z.png", "sha256": "1"},
        "not-a-dict",
        {"path": "a.csv"},
        {"no_path": True},
    ]
    payload = analysis_lineage_payload(manifest)
    assert payload["artifacts"] == ["", "a.csv", "z.png"]


def test_missing_sections_degrade_without_raising():
    payload = analysis_lineage_payload({})
    assert payload["run_id"] == ""
    assert payload["skill_id"] == ""
    assert payload["project_id"] == ""
    assert payload["terminal_status"] == ""
    assert payload["artifacts"] == []
    assert payload["parameters"] == {}


def test_digest_matches_manual_sha256():
    manifest = _manifest()
    expected = hashlib.sha256(analysis_lineage_bytes(manifest)).hexdigest()
    assert analysis_lineage_digest(manifest) == expected


def test_canonical_bytes_sorts_keys_compactly():
    assert canonical_projection_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
