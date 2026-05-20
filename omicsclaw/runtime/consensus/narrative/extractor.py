"""Per-member structured extraction (B-path step 1)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "extract.tmpl"

Confidence = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class MemberExtraction:
    member_name: str
    skill_name: str
    key_findings: tuple[str, ...]
    key_numbers: dict[str, Any]
    confidence: Confidence
    caveats: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "member_name": self.member_name,
            "skill_name": self.skill_name,
            "key_findings": list(self.key_findings),
            "key_numbers": self.key_numbers,
            "confidence": self.confidence,
            "caveats": list(self.caveats),
        }


def render_extract_prompt(*, member_name: str, skill_name: str, report_text: str) -> str:
    template = _PROMPT_PATH.read_text()
    return template.format(
        member_name=member_name,
        skill_name=skill_name,
        report_text=report_text.strip(),
    )


def _coerce_extraction(
    payload: Any, *, member_name: str, skill_name: str
) -> MemberExtraction:
    if not isinstance(payload, dict):
        raise ValueError("extraction payload must be a JSON object")
    findings = payload.get("key_findings") or []
    numbers = payload.get("key_numbers") or {}
    confidence = str(payload.get("confidence", "low")).lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "low"
    caveats = payload.get("caveats") or []
    return MemberExtraction(
        member_name=member_name,
        skill_name=skill_name,
        key_findings=tuple(str(f) for f in findings if str(f).strip()),
        key_numbers={str(k): v for k, v in (numbers or {}).items()},
        confidence=confidence,  # type: ignore[arg-type]
        caveats=tuple(str(c) for c in caveats if str(c).strip()),
    )


def _default_llm_call(prompt: str, timeout: float = 30.0) -> str | None:
    """Best-effort extractor LLM call; ``None`` on failure so callers fall back."""
    from omicsclaw.providers.chat_completion import call_chat_completion

    return call_chat_completion(prompt, timeout=timeout)


def extract_member_findings(
    *,
    member_name: str,
    skill_name: str,
    report_path: Path,
    llm: Any = None,
    offline_extraction: MemberExtraction | None = None,
) -> MemberExtraction:
    """Build a ``MemberExtraction`` for one member's report.

    When the LLM is unavailable AND ``offline_extraction`` is None, returns
    a minimal extraction with the report's first 5 markdown bullet/numbered
    lines as ``key_findings`` and ``confidence='low'`` — enough for the
    synthesiser to still produce a report.
    """
    if not report_path.exists():
        raise FileNotFoundError(f"report not found: {report_path}")
    report_text = report_path.read_text()
    prompt = render_extract_prompt(
        member_name=member_name, skill_name=skill_name, report_text=report_text
    )

    if llm is None:
        llm = _default_llm_call
    raw = llm(prompt)
    if raw:
        try:
            payload = json.loads(raw)
            return _coerce_extraction(payload, member_name=member_name, skill_name=skill_name)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("extract: invalid LLM JSON (%s); falling back to heuristic", exc)

    if offline_extraction is not None:
        return offline_extraction
    return _heuristic_offline_extraction(
        member_name=member_name, skill_name=skill_name, report_text=report_text
    )


def _heuristic_offline_extraction(
    *, member_name: str, skill_name: str, report_text: str
) -> MemberExtraction:
    bullets: list[str] = []
    for line in report_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ", "+ ")):
            bullets.append(stripped[2:].strip())
        elif stripped[:2].isdigit() and stripped[2:3] in (".", ")"):
            bullets.append(stripped[3:].strip())
        if len(bullets) >= 5:
            break
    return MemberExtraction(
        member_name=member_name,
        skill_name=skill_name,
        key_findings=tuple(bullets),
        key_numbers={},
        confidence="low",
        caveats=("offline heuristic extraction — LLM unavailable",),
    )


def to_extractions_payload(items: Iterable[MemberExtraction]) -> str:
    return json.dumps([item.to_dict() for item in items], indent=2)
