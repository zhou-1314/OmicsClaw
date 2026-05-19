"""Typed-consensus source registry — per-source-skill artifact readers.

A **MemberArtifactReader** knows where a consensus member's labels and
intrinsic-quality value live on disk, given the member spec and the team's
output root. One singleton per source skill; the driver / graph-memory writer
/ test harness all program against ``(read_labels, read_intrinsic_quality)``
and never touch file paths or column names directly.

The registry replaces the old ``set[str]`` marker in ``dispatch.py``:
``TYPED_CONSENSUS_REGISTRY: dict[str, TypedConsensusSource]`` carries both
the membership signal (is this skill on the A path?) and the behaviour
needed to read the member's outputs.

Adding a new typed-consensus source skill requires three steps:

1. Implement a ``MemberArtifactReader`` adapter (~30 lines).
2. Register it: ``TYPED_CONSENSUS_REGISTRY["<skill-name>"] = TypedConsensusSource(reader=...)``.
3. Add a thin CLI wrapper that calls ``run_typed_consensus`` with the source.

See ADR 0010 for the architectural decision, ADR 0011 for the evaluation
contract that consumes these adapters' outputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import pandas as pd

from omicsclaw.runtime.consensus.member import ConsensusMember


@runtime_checkable
class MemberArtifactReader(Protocol):
    """Reads one consensus member's outputs into the canonical typed-consensus shape.

    Per-skill singleton. The per-call ``member`` argument lets a single
    adapter serve members with different ``params`` (e.g. sc-clustering's
    leiden vs louvain label column).
    """

    def read_labels(
        self, member: ConsensusMember, output_root: Path
    ) -> pd.Series | None:
        """Per-observation label vector, indexed by observation id.

        Returns ``None`` when the artifact is missing or malformed so the
        caller can record the member as "failed at gather time" without
        the entire run blowing up.
        """

    def read_intrinsic_quality(
        self, member: ConsensusMember, output_root: Path
    ) -> float:
        """Intrinsic-quality scalar for scoring (mean_local_purity / silhouette / ...).

        Returns ``0.0`` on any missing or unparseable artifact; the
        scoring layer logs a warning but treats the run as proceeding.
        """


@dataclass(frozen=True)
class TypedConsensusSource:
    """Value type of ``TYPED_CONSENSUS_REGISTRY``.

    v1 holds one field (``reader``). v1.x may add a ``planner`` /
    ``report_template`` / etc. without breaking the registry shape.
    """

    reader: MemberArtifactReader


# --------------------------------------------------------------------------- #
# Adapters                                                                    #
# --------------------------------------------------------------------------- #

class SpatialDomainsArtifactReader:
    """spatial-domains writes ``figure_data/spatial_*.csv`` with columns
    ``(observation, spatial_domain)`` (and optionally a purity column).
    Intrinsic quality is ``summary.mean_local_purity`` in ``summary.json``."""

    _LABEL_COLUMN = "spatial_domain"
    _OBS_COLUMN = "observation"
    _PRIMARY_RELPATH = "figure_data/spatial_full.csv"
    _FALLBACK_GLOB = "figure_data/spatial_*.csv"
    _SUMMARY_RELPATH = "summary.json"
    _INTRINSIC_DOTTED = "summary.mean_local_purity"

    def read_labels(
        self, member: ConsensusMember, output_root: Path
    ) -> pd.Series | None:
        member_dir = member.member_output_dir(output_root)
        csv_path = member_dir / self._PRIMARY_RELPATH
        if not csv_path.exists():
            candidates = sorted(member_dir.glob(self._FALLBACK_GLOB))
            if not candidates:
                return None
            csv_path = candidates[0]
        df = pd.read_csv(csv_path)
        if self._OBS_COLUMN not in df.columns or self._LABEL_COLUMN not in df.columns:
            return None
        return df.set_index(self._OBS_COLUMN)[self._LABEL_COLUMN].astype(str)

    def read_intrinsic_quality(
        self, member: ConsensusMember, output_root: Path
    ) -> float:
        import json

        summary_path = member.member_output_dir(output_root) / self._SUMMARY_RELPATH
        if not summary_path.exists():
            return 0.0
        try:
            data: object = json.loads(summary_path.read_text())
        except (OSError, json.JSONDecodeError):
            return 0.0
        cursor: object = data
        for key in self._INTRINSIC_DOTTED.split("."):
            if isinstance(cursor, dict) and key in cursor:
                cursor = cursor[key]
            else:
                return 0.0
        try:
            return float(cursor)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0


class ScClusteringArtifactReader:
    """sc-clustering writes ``figure_data/embedding_points.csv`` with columns
    ``(cell_id, embedding_key, coord1, coord2, <cluster_method>)``. The
    label-column name is the value of ``member.params['cluster-method']``
    (default ``leiden`` if absent). Intrinsic quality is the
    ``silhouette_score`` row in ``figure_data/clustering_summary.csv``."""

    _OBS_COLUMN = "cell_id"
    _LABEL_COLUMN_KEY = "cluster-method"
    _LABEL_COLUMN_DEFAULT = "leiden"
    _LABELS_RELPATH = "figure_data/embedding_points.csv"
    _SUMMARY_RELPATH = "figure_data/clustering_summary.csv"
    _SILHOUETTE_METRIC = "silhouette_score"

    def _label_column(self, member: ConsensusMember) -> str:
        return str(member.params.get(self._LABEL_COLUMN_KEY, self._LABEL_COLUMN_DEFAULT))

    def read_labels(
        self, member: ConsensusMember, output_root: Path
    ) -> pd.Series | None:
        csv_path = member.member_output_dir(output_root) / self._LABELS_RELPATH
        if not csv_path.exists():
            return None
        df = pd.read_csv(csv_path)
        if self._OBS_COLUMN not in df.columns:
            return None
        label_col = self._label_column(member)
        if label_col not in df.columns:
            # Fallback: rightmost non-coordinate column.
            non_coord = [
                c for c in df.columns
                if c not in {self._OBS_COLUMN, "embedding_key", "coord1", "coord2"}
            ]
            if not non_coord:
                return None
            label_col = non_coord[-1]
        return df.set_index(self._OBS_COLUMN)[label_col].astype(str)

    def read_intrinsic_quality(
        self, member: ConsensusMember, output_root: Path
    ) -> float:
        csv_path = member.member_output_dir(output_root) / self._SUMMARY_RELPATH
        if not csv_path.exists():
            return 0.0
        try:
            df = pd.read_csv(csv_path)
        except (OSError, pd.errors.ParserError):
            return 0.0
        if "metric" not in df.columns or "value" not in df.columns:
            return 0.0
        row = df.loc[df["metric"] == self._SILHOUETTE_METRIC]
        if row.empty:
            return 0.0
        try:
            return float(row.iloc[0]["value"])
        except (TypeError, ValueError):
            return 0.0


# --------------------------------------------------------------------------- #
# Registry                                                                    #
# --------------------------------------------------------------------------- #

#: Single audit surface for the verified/exploratory boundary.
#: A skill in this dict is on the A (typed) path; anything else is B (narrative).
TYPED_CONSENSUS_REGISTRY: dict[str, TypedConsensusSource] = {
    "spatial-domains": TypedConsensusSource(reader=SpatialDomainsArtifactReader()),
    "sc-clustering": TypedConsensusSource(reader=ScClusteringArtifactReader()),
}
