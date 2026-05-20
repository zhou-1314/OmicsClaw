"""Consensus member — one ``(skill, params)`` fan-out target.

A member is a *deterministic skill subprocess* — NOT an LLM sub-agent.
The fan-out runtime (``team.py``) invokes ``skill.runner.run_skill``
once per member; cancel_event propagates via the existing ADR 0009 chain
(threading.Event → killpg).

How to read a member's outputs is the job of a ``MemberArtifactReader``
in ``source_registry.py`` — never the member's own concern. This keeps
member specs (what to run) decoupled from artifact schemas (what came out).
"""

from __future__ import annotations

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
    """

    name: str
    skill_name: str
    params: Mapping[str, str] = field(default_factory=dict)

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
