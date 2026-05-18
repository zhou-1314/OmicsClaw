"""Consensus member — one ``(skill, params)`` fan-out target.

A member is a *deterministic skill subprocess* — NOT an LLM sub-agent.
The fan-out runtime (``team.py``) invokes ``skill.runner.run_skill``
once per member; cancel_event propagates via the existing ADR 0009 chain
(threading.Event → killpg).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class ConsensusMember:
    """One unit of consensus fan-out.

    Attributes
    ----------
    name :
        Stable identifier — used as output subdirectory name and reporting
        label. Must be unique within a team and filesystem-safe.
    skill_name :
        Skill alias accepted by ``omicsclaw.skill.runner.run_skill``
        (e.g. ``"spatial-domains"``, ``"sc-clustering"``).
    params :
        Mapping of CLI flag (without leading ``--``) to string value. For
        flags with no value, set the value to the empty string. Flags are
        serialised into ``--flag value`` pairs by ``to_extra_args``.
    intrinsic_quality_path :
        Dotted accessor into the member's output ``summary.json`` (or other
        canonical artifact) yielding a single float. Example:
        ``"summary.mean_local_purity"``. Optional — if absent the scoring
        layer uses ``0.0`` and records a warning.
    artifact_relpath :
        Path (relative to the member's output dir) to the labels TSV/CSV the
        operator should read. The file is expected to have columns
        ``observation`` and one label column whose name is given by
        ``label_column``.
    label_column :
        Name of the per-observation label column inside ``artifact_relpath``.
    """

    name: str
    skill_name: str
    params: Mapping[str, str] = field(default_factory=dict)
    intrinsic_quality_path: str = ""
    artifact_relpath: str = "figure_data/spatial_full.csv"
    label_column: str = "spatial_domain"

    def to_extra_args(self) -> list[str]:
        """Flatten ``params`` into a ``--flag value`` list for ``run_skill``."""
        out: list[str] = []
        for flag, value in self.params.items():
            out.append(f"--{flag}")
            if value != "":
                out.append(str(value))
        return out

    def member_output_dir(self, output_root: Path) -> Path:
        return Path(output_root) / self.name

    def artifact_path(self, output_root: Path) -> Path:
        return self.member_output_dir(output_root) / self.artifact_relpath


def read_intrinsic_quality(
    summary_path: Path, dotted_path: str
) -> float:
    """Resolve ``"a.b.c"`` against a JSON file. Returns 0.0 on any miss."""
    if not dotted_path:
        return 0.0
    try:
        data = json.loads(summary_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return 0.0
    cursor: object = data
    for key in dotted_path.split("."):
        if isinstance(cursor, dict) and key in cursor:
            cursor = cursor[key]
        else:
            return 0.0
    try:
        return float(cursor)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
