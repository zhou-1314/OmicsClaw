"""Frozen-source reader for Run-manifest projections (ADR 0064).

Bridges the projector's ``SourceReader`` contract to the Run Store: for a
``source_store='run'`` Intent whose ``source_ref`` is a Manifest reference, it
re-reads the immutable Manifest and re-derives the canonical analysis-lineage
bytes so the applicator can verify them against the frozen digest.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping

from omicsclaw.control.projection_payload import analysis_lineage_bytes

logger = logging.getLogger(__name__)

__all__ = ["RUN_SOURCE_STORE", "RunManifestSourceReader"]

RUN_SOURCE_STORE = "run"


class RunManifestSourceReader:
    """Re-derive an analysis-lineage projection from a Run Manifest.

    Structurally a ``omicsclaw.memory.projection.SourceReader``. For the ``run``
    store it reads the Manifest via the injected reader and returns the canonical
    analysis-lineage bytes.

    A read error PROPAGATES (so the driver defers the Intent and retries) rather
    than being reported as ``None`` / source loss. The Run Store collapses a
    genuinely-missing Manifest and a transient read fault into the same error, so
    the reader cannot tell them apart; a transient fault must not permanently
    fail the Intent, and Manifests are not deleted in v1, so deferral is the safe
    choice for both. Genuine tampering surfaces instead as a digest mismatch in
    the applicator. Any non-``run`` store yields ``None`` — this reader only owns
    the ``run`` store, and the only Producer today emits ``run`` Intents; a
    multi-store dispatcher is a future extension.
    """

    def __init__(self, read_manifest: Callable[[str], Mapping[str, Any]]):
        self._read_manifest = read_manifest

    def __call__(self, *, source_store: str, source_ref: str) -> bytes | None:
        if source_store != RUN_SOURCE_STORE:
            logger.warning(
                "RunManifestSourceReader cannot read source_store=%r", source_store
            )
            return None
        manifest = self._read_manifest(source_ref)  # read errors propagate -> deferred
        if not isinstance(manifest, Mapping):
            return None
        return analysis_lineage_bytes(manifest)
