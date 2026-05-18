"""B-path step 2 — synthesise N member extractions into one narrative report."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from omicsclaw.runtime.consensus.dispatch import output_banner
from omicsclaw.runtime.consensus.narrative.extractor import (
    MemberExtraction,
    to_extractions_payload,
)

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "synthesize.tmpl"


@dataclass(frozen=True)
class NarrativeReport:
    markdown: str
    used_llm: bool
    n_members: int


def render_synthesis_prompt(
    *, query: str, skill_name: str, extractions: Iterable[MemberExtraction]
) -> str:
    template = _PROMPT_PATH.read_text()
    return template.format(
        query=query or "(none)",
        skill_name=skill_name,
        extractions_json=to_extractions_payload(extractions),
    )


def _default_llm_call(prompt: str, timeout: float = 60.0) -> str | None:
    api_key = os.getenv("LLM_API_KEY") or ""
    if not api_key:
        return None
    try:
        import requests

        from omicsclaw.routing.llm_router import _resolve_llm_config

        _, base_url, model = _resolve_llm_config()
        url = f"{base_url.rstrip('/')}/chat/completions"
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            },
            timeout=timeout,
        )
        if response.status_code != 200:
            logger.warning("synthesise: LLM HTTP %s: %s", response.status_code, response.text[:200])
            return None
        return str(response.json()["choices"][0]["message"]["content"]).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("synthesise: LLM call failed: %s", exc)
        return None


def _ensure_banner(markdown: str) -> str:
    """Banner is non-configurable per ADR 0010. Prepend if missing."""
    banner = output_banner("narrative")
    if markdown.lstrip().startswith(banner):
        return markdown
    return f"{banner}\n\n{markdown}"


def synthesize_narrative(
    *,
    query: str,
    skill_name: str,
    extractions: list[MemberExtraction],
    llm: Any = None,
) -> NarrativeReport:
    """Synthesise N ``MemberExtraction`` into one markdown narrative report.

    When the LLM is unavailable, falls back to a templated markdown
    (Agreement / Contradictions / Per-member confidence / Open questions)
    so downstream consumers always get a usable file. Falls back loudly via
    ``confidence='low'`` markers and an "OFFLINE" caveat.
    """
    if not extractions:
        raise ValueError("synthesize_narrative requires at least 1 extraction")

    if llm is None:
        llm = _default_llm_call
    prompt = render_synthesis_prompt(
        query=query, skill_name=skill_name, extractions=extractions
    )
    raw = llm(prompt)
    if raw:
        return NarrativeReport(
            markdown=_ensure_banner(raw),
            used_llm=True,
            n_members=len(extractions),
        )

    return NarrativeReport(
        markdown=_templated_offline_synthesis(query, skill_name, extractions),
        used_llm=False,
        n_members=len(extractions),
    )


def _templated_offline_synthesis(
    query: str, skill_name: str, extractions: list[MemberExtraction]
) -> str:
    lines = [output_banner("narrative"), ""]
    lines.append(f"# Narrative consensus — {skill_name}")
    lines.append("")
    lines.append(f"_Query_: {query or '(none)'}")
    lines.append(
        "_Note_: LLM unavailable — using OFFLINE templated synthesis. "
        "Treat conclusions with extra skepticism."
    )
    lines.append("")

    lines.append("## Agreement")
    finding_counts: dict[str, list[str]] = {}
    for e in extractions:
        for f in e.key_findings:
            finding_counts.setdefault(f, []).append(e.member_name)
    shared = sorted(
        ((k, v) for k, v in finding_counts.items() if len(v) >= 2),
        key=lambda kv: (-len(kv[1]), kv[0]),
    )
    if shared:
        for finding, members in shared:
            lines.append(f"- *{', '.join(sorted(members))}*: {finding}")
    else:
        lines.append("- No finding was reported by ≥2 members.")
    lines.append("")

    lines.append("## Contradictions")
    lines.append(
        "- Offline synthesis cannot reliably detect semantic contradictions; "
        "review members manually for disagreement."
    )
    lines.append("")

    lines.append("## Per-member confidence")
    for e in extractions:
        caveat = f" — {'; '.join(e.caveats)}" if e.caveats else ""
        lines.append(f"- **{e.member_name}** ({e.skill_name}): {e.confidence}{caveat}")
    lines.append("")

    lines.append("## Open questions")
    lines.append(
        "- Run with `LLM_API_KEY` set to obtain a model-narrated synthesis "
        "with explicit contradiction annotation."
    )
    lines.append("")
    return "\n".join(lines)
