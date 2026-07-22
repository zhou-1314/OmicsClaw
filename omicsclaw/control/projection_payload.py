"""Canonical analysis-lineage projection payload from a Run Manifest (ADR 0064).

A completed Run's immutable Manifest is the frozen *source* of an
``analysis_lineage`` Project projection. The Producer (at terminalization) and
the projector's source-reader (later, possibly after archive) both derive the
projection bytes from that Manifest through the SAME pure functions here, so the
``content_sha256`` frozen in the Projection Intent equals what the reader
recomputes. The Manifest is immutable once completed, so this derivation is
stable across reads — and because both sides call one function, an imperfect
field guess degrades identically on both sides and never breaks the digest.

This lives in the control layer because the Run Manifest is a control/run-store
concept; the Memory layer imports it (Memory already depends on control).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

__all__ = [
    "ANALYSIS_LINEAGE_KIND",
    "canonical_projection_bytes",
    "analysis_lineage_payload",
    "analysis_lineage_bytes",
    "analysis_lineage_digest",
]

ANALYSIS_LINEAGE_KIND = "analysis_lineage"


def canonical_projection_bytes(payload: Mapping[str, Any]) -> bytes:
    """Deterministic UTF-8 JSON — sorted keys (recursively) + compact separators."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def analysis_lineage_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the stable analysis-lineage record from a completed Run Manifest.

    Pure and defensive: unknown/missing fields degrade to empty instead of
    raising. Correctness of the digest does not depend on getting every field
    right — Producer and reader run this same function on the same immutable
    Manifest — only the record's informativeness does.
    """
    header = _as_dict(manifest.get("header"))
    inputs = _as_dict(header.get("inputs"))
    scope = _as_dict(header.get("scope"))
    completion = _as_dict(manifest.get("completion"))
    raw_artifacts = completion.get("artifacts")
    artifact_paths = sorted(
        str(item.get("path", ""))
        for item in (raw_artifacts if isinstance(raw_artifacts, list) else [])
        if isinstance(item, Mapping)
    )
    return {
        "kind": ANALYSIS_LINEAGE_KIND,
        "schema_version": 1,
        "run_id": str(header.get("run_id", "")),
        "skill_id": str(inputs.get("skill_id", "")),
        "skill_revision": {
            str(k): str(v)
            for k, v in _as_dict(header.get("skill_revision")).items()
        },
        "project_id": str(scope.get("project_id") or ""),
        "parameters": _as_dict(header.get("parameters")),
        "terminal_status": str(completion.get("kind", "")),
        "result_envelope_sha256": str(completion.get("result_envelope_sha256", "")),
        "artifacts": artifact_paths,
    }


def analysis_lineage_bytes(manifest: Mapping[str, Any]) -> bytes:
    return canonical_projection_bytes(analysis_lineage_payload(manifest))


def analysis_lineage_digest(manifest: Mapping[str, Any]) -> str:
    return hashlib.sha256(analysis_lineage_bytes(manifest)).hexdigest()
