"""Thread-scoped Ideate listing + formalize for the Bench surface (ADR 0021 / 批7).

Per-thread grounding (批7) closed the 0019 gap: KG ingest now records a
thread<->Source link (``thread_source://<thread_id>/<slug>`` nodes in the graph
Memory System; see ``orchestration._capture_thread_source`` /
``thread.list_thread_source_slugs``). So formalize grounds against the THREAD's
Sources (its citation allow-list) when the caller passes ``thread_source_slugs``,
and ``to_frontend_hypothesis`` computes the real cross-study (跨课题) badge from
the thread's slug set. Backward-compatible: a ``None`` slug set (no thread /
memory off) keeps the legacy **workspace-wide** grounding and a False badge. The
Hypothesis LISTING stays workspace-wide (a thread can see hypotheses formed
elsewhere); only the badge is thread-relative. The KG Source pages themselves
remain shared across threads (ADR 0019) — only the per-thread *link* is scoped.

All KG access is in-process via ``omicsclaw_kg`` (the same package the desktop
server mounts at ``/kg``), keyed by the resolved KG ``home`` string.
"""

from __future__ import annotations

import asyncio
from typing import Any

from omicsclaw.analysis_router.router import route_analysis_request


def to_frontend_hypothesis(
    raw: dict[str, Any], thread_slugs: set[str] | None = None
) -> dict[str, Any]:
    """Map a KG hypothesis (frontmatter dict or a formalize result) to the shape
    the frontend HypothesisCard renders. Both sources use the same KG field names
    (slug/title/proposed_claim/supported_by/...), so one mapper serves both.

    ``thread_slugs`` (批7) is the set of Source slugs ingested in the current
    thread. When provided, ``cross_study`` (跨课题 badge) is True iff the
    hypothesis cites at least one Source OUTSIDE the thread. ``None`` (no thread
    context) keeps the legacy always-False behavior — guard the membership test
    against None so ``set(...) <= None`` never raises.
    """
    supported = [str(s) for s in (raw.get("supported_by") or [])]
    cross_study = bool(
        thread_slugs is not None and supported and not set(supported) <= set(thread_slugs)
    )
    return {
        "id": str(raw.get("slug") or raw.get("id") or ""),
        "title": str(raw.get("title") or ""),
        "claim": str(raw.get("proposed_claim") or raw.get("claim") or ""),
        "supported_by": supported,
        "ungrounded": len(supported) == 0,
        "candidate_datasets": [str(d) for d in (raw.get("candidate_datasets") or [])],
        "recommended_skills": [str(s) for s in (raw.get("recommended_skills") or [])],
        "cross_study": cross_study,
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


def list_workspace_hypotheses(
    home: str, *, limit: int = 50, thread_slugs: set[str] | None = None
) -> list[dict[str, Any]]:
    """List workspace hypotheses (newest-first) mapped to the frontend shape.

    ``thread_slugs`` (批7) scopes the cross-study badge: when provided, each
    hypothesis is flagged ``cross_study`` if it cites a Source outside the current
    thread. The LISTING stays workspace-wide (a thread can see hypotheses formed
    elsewhere); only the badge is thread-relative. ``None`` → no badge.
    """
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
        out.append(to_frontend_hypothesis(detail.get("frontmatter") or {}, thread_slugs))
    return out


def list_thread_sources(home: str, slugs: list[str]) -> list[dict[str, Any]]:
    """Enrich a thread's Source slugs (批7) with title/state from the workspace KG.

    ``slugs`` is the thread's recorded source set (from ``thread.list_thread_source_slugs``).
    We intersect it with the live workspace Source listing — preserving the thread's
    order — so stale/deleted slugs are dropped and each entry carries the page's
    title + state for the Read panel. One workspace listing, no per-slug KG fetch.
    """
    from omicsclaw_kg.mcp_server import tools

    listing = tools.kg_list_pages(page_type="sources", limit=1000, home=home)
    if "error" in listing:
        raise RuntimeError(listing["error"])
    by_slug = {str(p["slug"]): p for p in listing.get("pages", []) if p.get("slug")}
    out: list[dict[str, Any]] = []
    for s in slugs:
        p = by_slug.get(s)
        if not p:
            continue  # stale/deleted — not a live workspace Source
        out.append(
            {
                "slug": s,
                "title": str(p.get("title") or s),
                "state": p.get("state"),
                "knowledge_state": p.get("knowledge_state"),
                "status": p.get("status"),
            }
        )
    return out


def formalize_thread_hypothesis(
    home: str, hunch: str, llm: Any, thread_source_slugs: list[str] | None = None
) -> dict[str, Any]:
    """Ground a free-text hunch into a hypothesis, returning the frontend shape.

    ``thread_source_slugs`` (批7) is the per-thread citation allow-list:
      - ``None`` → ground against EVERY workspace Source (legacy / no-thread /
        memory-disabled callers — backward compatible).
      - a list (possibly empty) → ground ONLY against those of the thread's
        sources that still exist as workspace Sources. An empty allow-list yields
        an UNGROUNDED hypothesis (KG ``require_support=False``), not an error.

    ``llm`` is a KG ``LLMClient`` (injected so tests pass a stub). May raise
    ``HypothesisIdeationError`` when the draft cites a Source outside the allow-list.
    """
    from omicsclaw_kg import config as kg_config
    from omicsclaw_kg.ideation.formalize import formalize_hypothesis

    cfg = kg_config.resolve(home)
    if thread_source_slugs is None:
        source_slugs = list_workspace_source_slugs(home)
    else:
        # Keep only slugs that still exist as workspace Sources (drop stale/deleted).
        workspace = set(list_workspace_source_slugs(home))
        source_slugs = [s for s in thread_source_slugs if s in workspace]
    result = formalize_hypothesis(cfg, hunch, source_slugs, llm)
    # A formalize result cites only within its allow-list, so cross_study is
    # always False for the fresh hypothesis — pass None (no badge).
    return to_frontend_hypothesis(result)


async def _resolve_thread_dataset_path(memory_client: Any, thread_id: str) -> str:
    """The thread's bound dataset path (批8/D-3: shared with kg_build_packet so the
    route-preview and the built packet route on the same dataset). Delegates to the
    neutral ``memory.compat`` resolver."""
    from omicsclaw.memory.compat import resolve_thread_dataset_path

    return await resolve_thread_dataset_path(memory_client, thread_id)


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
