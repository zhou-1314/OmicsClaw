"""Model-capability gate for the Autonomous Code Mini-Agent (ADR 0032 §8).

The mini-agent is far more demanding than tool-calling or one-shot codegen: a
weak local model can burn the whole budget emitting malformed turns. ADR §8
requires that, when the active model cannot reliably follow the markdown/code
contract, the mini-agent **refuses the route** — with a clear diagnostic —
rather than grinding. (Single-engine consolidation removed the old ``degrade``
fallback: there is no simpler engine to drop down to.)

This replaces the purely heuristic posture (envelope fail-closed + in-loop
format-error budget) with an explicit, *behavioural* pre-flight probe: one or
two cheap completions that test whether the model actually produces a valid
``Purpose/Reasoning/Next Goal/Code`` turn. The in-loop ``WARMUP_STEPS`` backstop
in :mod:`mini_agent` is the second line of defence for models that pass the
probe but then flail.

Pure logic over a ``complete(prompt)`` client; no kernel dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import os
from typing import Protocol

from .protocol import TurnFormatError, parse_turn
from .validation import validate_generated_code


class CapabilityVerdict(StrEnum):
    CAPABLE = "capable"
    INCAPABLE = "incapable"


class _Completer(Protocol):
    def complete(self, prompt: str, *, temperature: float = 0.0) -> str | None: ...


@dataclass(slots=True)
class CapabilityProbe:
    """Outcome of probing whether a model can drive the mini-agent contract."""

    verdict: CapabilityVerdict
    attempts_used: int
    reason: str
    last_excerpt: str = ""

    @property
    def capable(self) -> bool:
        return self.verdict == CapabilityVerdict.CAPABLE

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "attempts_used": self.attempts_used,
            "reason": self.reason,
        }


@dataclass(slots=True)
class GateDecision:
    """What the dispatch should do for one mini-agent request."""

    action: str  # "run" | "refuse"
    probe: CapabilityProbe | None

    @property
    def diagnostic(self) -> str:
        if self.probe is None:
            return ""
        return (
            "The active model could not follow the Autonomous Code Mini-Agent contract "
            f"({self.probe.reason}). Refusing the generated-code route; use a stronger "
            "model, or run the analysis through a built-in skill."
        )


_PROBE_PROMPT = (
    "Capability check for the OmicsClaw Autonomous Code Mini-Agent. Reply with EXACTLY "
    "these four markdown sections and nothing else:\n"
    "**Purpose**: <one line>\n"
    "**Reasoning**: <one line>\n"
    "**Next Goal**: <one line>\n"
    "**Code**:\n"
    "```python\n"
    "print(2 + 2)\n"
    "```\n"
    "Do not import subprocess, os, socket, or requests. Produce exactly one step now."
)


def probe_model_capability(llm: _Completer, *, attempts: int = 2) -> CapabilityProbe:
    """Behaviourally test whether *llm* can emit a valid mini-agent turn."""
    attempts = max(1, int(attempts))
    prompt = _PROBE_PROMPT
    last = ""
    for index in range(attempts):
        raw = ""
        try:
            raw = llm.complete(prompt, temperature=0.0) or ""
        except Exception as exc:  # provider error — treat as a failed attempt
            last = f"<provider error: {exc}>"
            prompt = _PROBE_PROMPT
            continue
        last = raw
        if not raw.strip():
            prompt = _PROBE_PROMPT + "\n\n(Your previous reply was empty.)"
            continue
        try:
            turn = parse_turn(raw)
        except TurnFormatError as exc:
            prompt = (
                _PROBE_PROMPT
                + f"\n\n(Your previous reply was rejected: {'; '.join(exc.problems)}.)"
            )
            continue
        if validate_generated_code(turn.code, language="python"):
            prompt = _PROBE_PROMPT + "\n\n(Your code used a blocked construct; keep it trivial and safe.)"
            continue
        return CapabilityProbe(
            verdict=CapabilityVerdict.CAPABLE,
            attempts_used=index + 1,
            reason="produced a valid Purpose/Reasoning/Next Goal/Code turn",
        )
    return CapabilityProbe(
        verdict=CapabilityVerdict.INCAPABLE,
        attempts_used=attempts,
        reason=f"no valid mini-agent turn in {attempts} attempt(s)",
        last_excerpt=last[:300],
    )


def mini_agent_gate(
    llm: _Completer,
    *,
    probe_enabled: bool | None = None,
    attempts: int = 2,
) -> GateDecision:
    """Decide run / refuse for a mini-agent request.

    With probing disabled the decision is ``run`` (trust the caller). With
    probing enabled, a capable model runs and an incapable one is refused with a
    clear diagnostic (there is only one engine, so there is nothing to degrade to).
    """
    if probe_enabled is None:
        probe_enabled = probe_enabled_default()
    if not probe_enabled:
        return GateDecision(action="run", probe=None)
    probe = probe_model_capability(llm, attempts=attempts)
    if probe.capable:
        return GateDecision(action="run", probe=probe)
    # One engine (ADR 0032): there is no simpler engine to degrade to, so an
    # incapable model is refused with a clear diagnostic.
    return GateDecision(action="refuse", probe=probe)


def probe_enabled_default() -> bool:
    return os.getenv("OMICSCLAW_MINI_AGENT_PROBE", "1").strip().lower() not in {"0", "false", "no", "off"}


__all__ = [
    "CapabilityProbe",
    "CapabilityVerdict",
    "GateDecision",
    "mini_agent_gate",
    "probe_enabled_default",
    "probe_model_capability",
]
