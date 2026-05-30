"""Typed-consensus source registry — per-source-skill artifact readers.

A **MemberArtifactReader** knows where a consensus member's labels and
intrinsic-quality value live on disk, given the member spec and the team's
output root. One singleton per source skill; the driver / graph-memory writer
/ test harness all program against ``(read_labels, read_intrinsic_quality)``
and never touch file paths or column names directly.

This module owns the artifact readers + the ``ConsensusSource`` contract
(ADR 0016 L3). The flavour registry lives in ``sources.CONSENSUS_SOURCES``
(keyed by flavour name); ``sources`` also derives the member_skill-keyed
``TYPED_CONSENSUS_REGISTRY`` that ``dispatch`` consults for typed/narrative
routing — a single source of truth, no hand-maintained second copy.

Adding a new typed-consensus flavour: implement a ``MemberArtifactReader``
(~30 lines) here, then add one ``ConsensusSource`` row to
``sources.CONSENSUS_SOURCES``. See ADR 0010/0011 for the evaluation contract
and ADR 0016 for this structure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

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


@runtime_checkable
class MemberPlanner(Protocol):
    """Strategy that turns CLI args into the members to fan out (ADR 0016 L3).

    Implementations live in ``planners.py`` (``ChairLLMPlanner`` and
    ``SweepPlanner``, sharing the ``_explicit_members`` helper for the
    ``--members`` branch). This is the one piece of genuine per-flavour logic;
    the rest of a ``ConsensusSource`` is data.
    """

    def propose(self, args: Any, *, source: "ConsensusSource") -> list[ConsensusMember]:
        ...


@dataclass(frozen=True)
class ConsensusSource:
    """Declarative contract for one consensus flavour (ADR 0016 L3).

    ``reader`` is the only required field — reader-only construction
    (``ConsensusSource(reader=...)``) stays valid. The rest carry the two-axis
    (template × source) contract: ``CONSENSUS_SOURCES`` rows populate them all,
    while the dispatch-derived view needs only ``member_skill`` + ``template``.
    """

    reader: MemberArtifactReader
    name: str = ""
    template: str = "categorical"
    member_skill: str = ""
    planner: "MemberPlanner | None" = None
    domain: str = ""
    report_title: str = ""
    param_hints_path: Path | None = None


# --------------------------------------------------------------------------- #
# Adapters                                                                    #
# --------------------------------------------------------------------------- #

class SpatialDomainsArtifactReader:
    """spatial-domains writes per-observation labels under ``figure_data/``
    (production: ``domain_spatial_points.csv``; legacy/mock: ``spatial_*.csv``)
    with columns ``(observation, spatial_domain[, ...])``. Intrinsic quality
    is ``summary.mean_local_purity`` in either ``summary.json`` (legacy/mock)
    or ``result.json`` (production)."""

    _LABEL_COLUMN = "spatial_domain"
    _OBS_COLUMN = "observation"
    # Production filename first, legacy/mock next; glob as last-resort fallback.
    _LABEL_RELPATH_CANDIDATES = (
        "figure_data/domain_spatial_points.csv",
        "figure_data/spatial_full.csv",
    )
    _LABEL_FALLBACK_GLOBS = (
        "figure_data/domain_*.csv",
        "figure_data/spatial_*.csv",
    )
    # Production writes result.json; legacy/mock writes summary.json. Same dotted path.
    _SUMMARY_RELPATH_CANDIDATES = ("result.json", "summary.json")
    _INTRINSIC_DOTTED = "summary.mean_local_purity"

    def _resolve_label_csv(self, member_dir: Path) -> Path | None:
        for relpath in self._LABEL_RELPATH_CANDIDATES:
            candidate = member_dir / relpath
            if candidate.exists():
                return candidate
        for pattern in self._LABEL_FALLBACK_GLOBS:
            matches = sorted(member_dir.glob(pattern))
            if matches:
                return matches[0]
        return None

    def read_labels(
        self, member: ConsensusMember, output_root: Path
    ) -> pd.Series | None:
        member_dir = member.member_output_dir(output_root)
        csv_path = self._resolve_label_csv(member_dir)
        if csv_path is None:
            return None
        df = pd.read_csv(csv_path)
        if self._OBS_COLUMN not in df.columns or self._LABEL_COLUMN not in df.columns:
            return None
        return df.set_index(self._OBS_COLUMN)[self._LABEL_COLUMN].astype(str)

    def read_intrinsic_quality(
        self, member: ConsensusMember, output_root: Path
    ) -> float:
        import json

        member_dir = member.member_output_dir(output_root)
        for relpath in self._SUMMARY_RELPATH_CANDIDATES:
            summary_path = member_dir / relpath
            if not summary_path.exists():
                continue
            try:
                data: object = json.loads(summary_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            cursor: object = data
            ok = True
            for key in self._INTRINSIC_DOTTED.split("."):
                if isinstance(cursor, dict) and key in cursor:
                    cursor = cursor[key]
                else:
                    ok = False
                    break
            if not ok:
                continue
            try:
                return float(cursor)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
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


# The flavour registry (CONSENSUS_SOURCES) and its derived member_skill-keyed
# view (TYPED_CONSENSUS_REGISTRY) live in ``sources.py`` — single source of truth.
