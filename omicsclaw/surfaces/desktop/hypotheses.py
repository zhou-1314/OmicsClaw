"""Thread-scoped Ideate listing + formalize for the Bench surface (ADR 0021).

KG is thread-blind (ADR 0021 §1): true per-thread filtering and the cross-study
(跨课题) badge need a thread<->Source association that does not exist yet (the
0019 gap — ``kg_ingest`` records no thread, ``ThreadMemory`` has no source list).
So this v1.5 slice is **workspace-wide**: it lists every Hypothesis page in the
workspace KG and grounds formalize against every workspace Source page. The
``thread_id`` is carried through by the caller and the returned shape keeps a
``cross_study`` field (always False for now) so per-thread scoping can be layered
in later without changing the frontend contract.

All KG access is in-process via ``omicsclaw_kg`` (the same package the desktop
server mounts at ``/kg``), keyed by the resolved KG ``home`` string.
"""

from __future__ import annotations

import asyncio
from typing import Any

from omicsclaw.analysis_router.router import route_analysis_request


def to_frontend_hypothesis(raw: dict[str, Any]) -> dict[str, Any]:
    """Map a KG hypothesis (frontmatter dict or a formalize result) to the shape
    the frontend HypothesisCard renders. Both sources use the same KG field names
    (slug/title/proposed_claim/supported_by/...), so one mapper serves both.
    """
    supported = [str(s) for s in (raw.get("supported_by") or [])]
    return {
        "id": str(raw.get("slug") or raw.get("id") or ""),
        "title": str(raw.get("title") or ""),
        "claim": str(raw.get("proposed_claim") or raw.get("claim") or ""),
        "supported_by": supported,
        "ungrounded": len(supported) == 0,
        "candidate_datasets": [str(d) for d in (raw.get("candidate_datasets") or [])],
        "recommended_skills": [str(s) for s in (raw.get("recommended_skills") or [])],
        # Deferred to 0019 (needs thread<->source association); always False here.
        "cross_study": False,
        "status": str(raw.get("status") or "draft"),
        # ADR 0021 §6: a verdict suggested by record_result, pending human confirm
        # (null when nothing is pending).
        "suggested_verdict": raw.get("suggested_verdict") or None,
    }


def list_workspace_source_slugs(home: str, *, limit: int = 1000) -> list[str]:
    """Every Source page slug in the workspace KG — the allow-list for formalize."""
    from omicsclaw_kg.mcp_server import tools

    listing = tools.kg_list_pages(page_type="sources", limit=limit, home=home)
    if "error" in listing:
        raise RuntimeError(listing["error"])
    return [str(p["slug"]) for p in listing.get("pages", []) if p.get("slug")]


def list_workspace_hypotheses(home: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """List workspace hypotheses (newest-first) mapped to the frontend shape."""
    from omicsclaw_kg.mcp_server import tools

    listing = tools.kg_list_pages(page_type="hypotheses", limit=limit, home=home)
    if "error" in listing:
        raise RuntimeError(listing["error"])

    out: list[dict[str, Any]] = []
    for page in listing.get("pages", []):
        slug = page.get("slug")
        if not slug:
            continue
        detail = tools.kg_get_page(page_type="hypotheses", slug=slug, home=home)
        if "error" in detail:
            continue
        out.append(to_frontend_hypothesis(detail.get("frontmatter") or {}))
    return out


def formalize_thread_hypothesis(home: str, hunch: str, llm: Any) -> dict[str, Any]:
    """Ground a free-text hunch against every workspace Source, returning the new
    hypothesis in the frontend shape. ``llm`` is a KG ``LLMClient`` (injected so
    tests can pass a stub). May raise ``HypothesisIdeationError`` on a bad draft.
    """
    from omicsclaw_kg import config as kg_config
    from omicsclaw_kg.ideation.formalize import formalize_hypothesis

    cfg = kg_config.resolve(home)
    source_slugs = list_workspace_source_slugs(home)
    result = formalize_hypothesis(cfg, hunch, source_slugs, llm)
    return to_frontend_hypothesis(result)


async def _resolve_thread_dataset_path(memory_client: Any, thread_id: str) -> str:
    """Best-effort ``file_path`` of a dataset bound to this thread, else ``""``.

    Datasets register at ``dataset://<thread_id>/<basename>`` (ADR 0018) inside the
    desktop user's namespace — the same namespace ``memory_client`` (a ``MemoryClient``)
    already reads. We list that subtree and return the first leaf's ``file_path`` (a
    thread usually has one primary dataset; the Router result is a preview the user
    confirms regardless). Any failure — memory off, no dataset, unparseable content —
    yields ``""`` so the Router still runs claim-only. ``MemoryClient`` has no
    ``get_memories`` (that lives on the session-keyed ``CompatMemoryStore``); the
    namespace-direct ``get_subtree``/``recall`` pair is the endpoint-read path.
    """
    if not thread_id:
        return ""
    try:
        from omicsclaw.memory.compat import _content_to_memory

        refs = await memory_client.get_subtree(f"dataset://{thread_id}/", limit=20)
        for ref in refs or []:
            uri = getattr(ref, "uri", None)
            if not uri:
                continue
            rec = await memory_client.recall(uri)
            content = getattr(rec, "content", None) if rec is not None else None
            if not content:
                continue  # container node / empty leaf
            mem = _content_to_memory(content, "dataset")
            file_path = str(getattr(mem, "file_path", "") or "") if mem else ""
            if file_path:
                return file_path
    except Exception:
        return ""
    return ""


async def route_preview(
    memory_client: Any, thread_id: str, slug: str, claim: str
) -> dict[str, Any]:
    """Preview the Analysis Router's recommendation for testing a hypothesis (ADR 0021 §4).

    Resolves the thread's bound dataset (best-effort) and runs the Router on the claim so
    the Ideate panel can SHOW the recommended skill + route kind + dataset before the user
    commits — Analyze never authors a new path. The Router runs even with no dataset.
    """
    dataset_path = await _resolve_thread_dataset_path(memory_client, thread_id)
    route = await asyncio.to_thread(route_analysis_request, claim, dataset_path)
    return {
        "thread_id": thread_id,
        "slug": slug,
        "kind": route.kind.value,
        "chosen_skill": route.chosen_skill,
        "confidence": route.confidence,
        "should_search_web": route.should_search_web,
        "missing_params": list(route.missing_params),
        "dataset_path": dataset_path or None,
        "reasoning": list(route.capability_decision.reasoning),
    }


def confirm_thread_hypothesis_verdict(home: str, slug: str, verdict: str) -> dict[str, Any]:
    """Confirm a suggested verdict (ADR 0021 §6): flip the hypothesis status and
    clear the suggestion. May raise ValueError (unknown verdict) / FileNotFoundError
    (missing page) from the KG layer.
    """
    from omicsclaw_kg import config as kg_config
    from omicsclaw_kg.handoff.feedback import confirm_hypothesis_verdict

    cfg = kg_config.resolve(home)
    update = confirm_hypothesis_verdict(cfg, slug, verdict)
    return {
        "slug": update.slug,
        "old_status": update.old_status,
        "new_status": update.new_status,
    }
