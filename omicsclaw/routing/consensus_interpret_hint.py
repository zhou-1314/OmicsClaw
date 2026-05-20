"""Detection helper — "is this path a verified typed consensus run?".

Slice 10.A per ADR 0012 IMPLEMENTATION_PLAN.md. Used by:

- ``/interpret`` CLI slash command (Slice 10.B) — validates the user's
  argument is actually a typed run before dispatching.
- Future agent-loop after-tool hook — can scan recent tool outputs for
  a freshly produced typed run dir and proactively surface a
  ``consensus-interpret`` suggestion (β backward proof-driven
  recommendation per ADR 0012 §"β-mid → γ-strict").

Pure detection — no LLM, no I/O beyond reading ``plan.json`` for the
``run_id``. Safe to call on any path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


_REQUIRED_TYPED_ARTIFACTS = (
    "plan.json",
    "consensus_labels.tsv",
    "member_scores.csv",
    "cross_method_nmi.csv",
)


@dataclass(frozen=True)
class InterpretSuggestion:
    """Ready-to-surface suggestion for a backward proof-driven recommendation.

    Surfaces in /interpret CLI dispatch and (in the future) in agent-loop
    after-tool hooks.
    """

    typed_run_dir: Path
    typed_run_id: str
    args_hint: str   # equivalent /run argument string


def is_typed_consensus_run(path: Path | str) -> bool:
    """True if ``path`` is a directory containing every canonical typed
    consensus artifact (4 files; see ADR 0010).
    """
    p = Path(path)
    if not p.is_dir():
        return False
    return all((p / a).exists() for a in _REQUIRED_TYPED_ARTIFACTS)


def suggest_interpret(path: Path | str) -> Optional[InterpretSuggestion]:
    """Return an :class:`InterpretSuggestion` if ``path`` is a typed run.

    ``typed_run_id`` resolution order: ``plan.json:run_id`` →
    directory basename. Malformed plan.json falls back gracefully
    (does not raise — this helper must be safe to call speculatively).
    """
    p = Path(path).resolve()
    if not is_typed_consensus_run(p):
        return None

    typed_run_id = _resolve_run_id(p)
    output_dir = p.parent / f"{p.name}_interpreted"
    args_hint = (
        f"consensus-interpret --input {p} --output {output_dir}"
    )

    return InterpretSuggestion(
        typed_run_dir=p,
        typed_run_id=typed_run_id,
        args_hint=args_hint,
    )


def _resolve_run_id(typed_run_dir: Path) -> str:
    """Best-effort: read run_id from plan.json; fall back to dir name."""
    plan_path = typed_run_dir / "plan.json"
    try:
        plan = json.loads(plan_path.read_text())
    except (OSError, json.JSONDecodeError):
        return typed_run_dir.name
    return str(plan.get("run_id") or typed_run_dir.name)
