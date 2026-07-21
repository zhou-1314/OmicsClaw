"""Deterministic KG handoff-queue executor — close the idea→analysis→verdict loop.

Named `handoff_executor` rather than `outbox` per ADR 0060, so that "Outbox"
unambiguously means the Outbound Delivery Outbox in control-plane architecture.
This module is a scientific handoff queue/executor: it schedules skill
execution, whereas Delivery Outbox Items may only transmit already-computed
terminal content. It must never be reused as the delivery authority.

The upstream `omicsclaw_kg` on-disk directory and the `/thread/{id}/outbox`
HTTP route keep their names: both are external contracts this rename does not
own.

Audit E (idea→analysis→verdict has no deterministic closure): a HandoffPacket
written to the KG outbox today has **no consumer**, and a result is recorded
back to the KG only if the LLM voluntarily calls ``kg_record_result``. This
module consumes a packet server-side: it resolves the packet's target skill and
the thread's dataset, runs the skill to completion via the skill runner, and
records a ``HandoffResult`` back to the KG (archiving the packet, updating the
graph, advancing any experiment step) — independent of any LLM tool call.

What stays non-deterministic by design: the scientific *verdict*
(validated / refuted) is a judgement, not a mechanical output of a skill run.
The caller supplies it; absent one we record the honest ``inconclusive`` (the
analysis ran; interpretation is pending the user's confirm-verdict step). The
*closure itself* — run + record + archive + graph update — is deterministic.

``omicsclaw_kg`` is imported lazily (it is added to ``sys.path`` only when the
desktop KG router mounts), so this module imports cleanly without it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from omicsclaw.common.report import load_result_json
from omicsclaw.runtime.tools.kg_tools import _import_kg_handoff, _is_safe_slug
from omicsclaw.services.path_validation import validate_input_path
from omicsclaw.skill.runner import arun_skill
from omicsclaw.surfaces.desktop.hypotheses import _resolve_thread_dataset_path

logger = logging.getLogger("omicsclaw.surfaces.desktop.handoff_executor")

# A skill run yields success + artifacts, not a scientific truth value. A
# deterministic executor may only carry an explicitly-supplied verdict or the
# honest "inconclusive"; "refined" is excluded because it requires a concrete
# refined hypothesis (slug + claim) that cannot be produced mechanically.
_ALLOWED_VERDICTS = {"validated", "refuted", "inconclusive"}


def _resolve_skill_name(packet: Any) -> str | None:
    """The skill to run: the resolved omicsclaw target, else the first
    recommended skill, else None (an unresolved ``file_drop`` packet)."""
    target = getattr(packet, "target", None)
    if (
        target is not None
        and getattr(target, "kind", "") == "omicsclaw_skill"
        and getattr(target, "skill_name", None)
    ):
        return str(target.skill_name)
    for rec in getattr(packet, "recommended_skills", None) or []:
        if rec:
            return str(rec)
    return None


def _flatten_summary(skill: str, result: Any, result_json: dict | None) -> str:
    """A one-line, non-empty summary for ``HandoffResult.summary`` (min_length 1).

    Prefer the skill's self-reported ``result.json['summary']`` (a dict) flattened
    to ``k=v`` pairs; fall back to an artifact count.
    """
    summary_obj = (result_json or {}).get("summary")
    if isinstance(summary_obj, dict) and summary_obj:
        flat = ", ".join(f"{k}={v}" for k, v in summary_obj.items())
        return f"{skill}: {flat}"[:480]
    if isinstance(summary_obj, str) and summary_obj.strip():
        return f"{skill}: {summary_obj.strip()}"[:480]
    n = len(getattr(result, "files", None) or [])
    return f"{skill} completed: {n} artifact(s)"[:480]


def _load_packet(cfg: Any, packet_id: str) -> Any:
    """Load a pending packet from the KG outbox. Raises FileNotFoundError when
    it is not pending (already archived runs are not re-run without intent)."""
    kg = _import_kg_handoff()
    if kg is None:
        raise RuntimeError("OmicsClaw-KG is not available")
    path = kg.paths.handoff_outbox_dir(cfg) / f"{packet_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"no pending packet {packet_id!r} in the outbox")
    import json

    return kg.HandoffPacket.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _record_packet_result(
    cfg: Any,
    packet_id: str,
    *,
    verdict: str,
    summary: str,
    artifact_paths: list[str],
) -> dict[str, Any]:
    """Stage a ``HandoffResult`` and record it (mirrors the proven recipe in
    ``kg_tools.execute_kg_record_result``). Synchronous — call via
    ``asyncio.to_thread`` from the async request path. Returns
    ``record_result``'s dict."""
    kg = _import_kg_handoff()
    if kg is None:
        raise RuntimeError("OmicsClaw-KG is not available")
    result = kg.HandoffResult(
        packet_id=packet_id,
        completed=datetime.now(timezone.utc),
        verdict=verdict,  # type: ignore[arg-type]
        summary=summary,
        artifact_paths=artifact_paths,
    )
    staging = kg.paths.cache_dir(cfg) / "result_staging"
    staging.mkdir(parents=True, exist_ok=True)
    rf = staging / f"{packet_id}.json"
    kg.atomic_write_text(rf, result.model_dump_json(indent=2))
    return kg.record_result(cfg, packet_id, rf)


def list_outbox_packets(cfg: Any) -> list[dict[str, Any]]:
    """Enumerate pending packets in the KG outbox (no public lister exists)."""
    kg = _import_kg_handoff()
    if kg is None:
        return []
    import json

    out: list[dict[str, Any]] = []
    outbox = kg.paths.handoff_outbox_dir(cfg)
    if not outbox.is_dir():
        return out
    for f in sorted(outbox.glob("*.json")):
        try:
            pkt = kg.HandoffPacket.model_validate(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue  # a malformed packet must not break the listing
        out.append(
            {
                "packet_id": pkt.packet_id,
                "skill_name": _resolve_skill_name(pkt),
                "hypothesis_slug": getattr(getattr(pkt, "hypothesis", None), "slug", ""),
                "experiment_slug": getattr(pkt, "experiment_slug", None),
                "step_id": getattr(pkt, "step_id", None),
                "question": getattr(pkt, "question", ""),
            }
        )
    return out


async def run_packet(
    memory_client: Any,
    cfg: Any,
    thread_id: str,
    packet_id: str,
    *,
    input_path: str | None = None,
    verdict: str = "inconclusive",
) -> dict[str, Any]:
    """Run one outbox packet to completion and record its result.

    Returns a status dict:
      - ``{"status": "recorded", ...}``   — skill ran and result recorded
      - ``{"status": "run_failed", ...}`` — skill ran but failed (NOT recorded;
        the packet stays in the outbox for retry)
      - ``{"status": "error", ...}``      — could not start (bad verdict / unsafe
        packet_id / no skill / no input)
    """
    if verdict not in _ALLOWED_VERDICTS:
        return {
            "status": "error",
            "error": (
                f"unsupported verdict {verdict!r}; use one of {sorted(_ALLOWED_VERDICTS)}. "
                "A 'refined' verdict needs an explicit refined hypothesis and is recorded "
                "via the Ideate flow, not the deterministic executor."
            ),
        }
    # packet_id is concatenated into outbox/staging filesystem paths below, and at
    # the HTTP boundary it is attacker-influenced — gate it to a single safe
    # filename segment (no ``/`` / ``..``) before it touches the filesystem.
    if not _is_safe_slug(packet_id):
        return {"status": "error", "error": f"invalid packet_id {packet_id!r}"}

    try:
        packet = _load_packet(cfg, packet_id)
    except FileNotFoundError as exc:
        return {"status": "error", "error": str(exc)}
    except Exception as exc:  # pragma: no cover - defensive (malformed packet / KG absent)
        logger.error("run_packet: failed to load packet %s: %s", packet_id, exc, exc_info=True)
        return {"status": "error", "error": f"could not load packet {packet_id!r}: {exc}"}

    skill = _resolve_skill_name(packet)
    if not skill:
        kind = getattr(getattr(packet, "target", None), "kind", None)
        return {
            "status": "error",
            "error": f"packet {packet_id!r} has no resolvable target skill (target.kind={kind!r})",
        }

    raw_input = input_path
    if not raw_input and memory_client is not None and thread_id:
        raw_input = await _resolve_thread_dataset_path(memory_client, thread_id)
    if not raw_input:
        return {
            "status": "error",
            "error": "no input dataset for this thread; bind a dataset to the thread or pass input_path",
        }
    resolved = validate_input_path(str(raw_input), allow_dir=True)
    if resolved is None:
        return {"status": "error", "error": f"input path is not in a trusted directory: {raw_input}"}

    try:
        result = await arun_skill(skill, input_path=str(resolved), project_id=thread_id or "")
    except Exception as exc:  # the skill runner itself blew up (not a clean failure)
        logger.error("run_packet: skill %s raised: %s", skill, exc, exc_info=True)
        return {"status": "run_failed", "packet_id": packet_id, "skill": skill, "error": str(exc)}
    if not getattr(result, "success", False):
        return {
            "status": "run_failed",
            "packet_id": packet_id,
            "skill": skill,
            "exit_code": getattr(result, "exit_code", None),
            "output_dir": getattr(result, "output_dir", None),
            "stderr": (getattr(result, "stderr", "") or "")[-2000:],
        }

    output_dir = getattr(result, "output_dir", None)
    result_json = load_result_json(output_dir) if output_dir else None
    summary = _flatten_summary(skill, result, result_json)
    # SkillRunResult.files are basenames; the run directory is the reliable,
    # absolute artifact reference for the KG (the consumer can browse it).
    artifacts = [str(output_dir)] if output_dir else []

    try:
        record = await asyncio.to_thread(
            _record_packet_result,
            cfg,
            packet_id,
            verdict=verdict,
            summary=summary,
            artifact_paths=artifacts,
        )
    except Exception as exc:
        logger.error("run_packet: record_result failed for %s: %s", packet_id, exc, exc_info=True)
        return {
            "status": "error",
            "error": f"analysis ran but recording the result failed: {exc}",
            "packet_id": packet_id,
            "skill": skill,
            "output_dir": output_dir,
        }

    # E-(2→3): also record the run as a thread-scoped AnalysisMemory so it surfaces
    # in the Analyze panel (the App's useThreadArtifacts lists analysis://<thread_id>/*).
    # The KG feedback above closes the verdict loop; this makes the deterministic run
    # visible as a thread artifact. Best-effort — never fail the run over a memory hiccup.
    if memory_client is not None and thread_id:
        try:
            from omicsclaw.memory.compat import (
                AnalysisMemory,
                _memory_to_content,
                _memory_to_uri_path,
            )

            method = ""
            if isinstance(result_json, dict):
                data = result_json.get("data") or {}
                method = str((data.get("method") if isinstance(data, dict) else "") or "") or str(
                    result_json.get("method") or ""
                )
            mem = AnalysisMemory(
                source_dataset_id="",
                skill=skill,
                method=method or "default",
                output_path=str(output_dir) if output_dir else "",
                status="completed",
                thread_id=thread_id,
                artifacts=artifacts,
            )
            await memory_client.remember(
                f"analysis://{_memory_to_uri_path(mem)}",
                _memory_to_content(mem),
                disclosure=f"Packet run: {skill}",
            )
        except Exception as exc:  # noqa: BLE001 — capture is best-effort
            logger.warning("run_packet: thread analysis capture failed (non-fatal): %s", exc)

    return {
        "status": "recorded",
        "packet_id": packet_id,
        "skill": skill,
        "output_dir": output_dir,
        "verdict": verdict,
        "summary": summary,
        "record": record,
    }
