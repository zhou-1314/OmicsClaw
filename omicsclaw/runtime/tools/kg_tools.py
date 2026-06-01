"""In-loop OpenAI tool wrappers for the OmicsClaw-KG read surface (Bench Phase 3.1).

These ``execute_kg_*`` coroutines expose the read-only knowledge-graph tools
(``omicsclaw_kg.mcp_server.tools``) to the agent loop so a researcher in the
**Read** lifecycle stage (ADR 0020) can search and traverse the cross-research
knowledge base that prior literature ingestion has built.

Design (ADR 0019 — KG is a *first-class but soft* dependency):

* **Lazy, soft-failing import.** ``omicsclaw_kg`` is optional. Each executor
  imports it at call time through ``_import_kg``; when the package is absent the
  executor returns a friendly notice (``_KG_UNAVAILABLE_HINT``) instead of
  raising. The tools are therefore *always registered* (a cache-stable tool
  surface, ADR 0024) and degrade gracefully when the package is missing.
* **Single shared KG home.** The KG stores cross-research reading knowledge, so
  v1 uses one shared home (not thread-isolated). ``_resolve_kg_home`` mirrors the
  desktop ``/kg`` mount's resolution (``OMICSCLAW_KG_HOME`` → ``OMICSCLAW_WORKSPACE``)
  without importing the surfaces layer; ``None`` lets KG's own ``resolve()`` walk
  up / fall back.
* **Readable, faithful output.** The underlying KG functions return
  JSON-serializable dicts (and ``{"error": ...}`` on user-correctable conditions,
  never exceptions). Each executor formats that dict into compact text for the
  LLM, surfacing the KG ``error`` verbatim — no fabricated fields (SOUL.md).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger("omicsclaw.runtime.tools.kg_tools")

_KG_UNAVAILABLE_HINT = (
    "OmicsClaw-KG is not installed, so the knowledge graph cannot be searched. "
    "This is an optional dependency — install the `omicsclaw_kg` package (or set "
    "OMICSCLAW_KG_SOURCE_DIR to its checkout) to enable Read-stage knowledge "
    "retrieval. Continue without it; KG tools will keep returning this notice."
)


# ---------------------------------------------------------------------------
# KG home resolution + soft import
# ---------------------------------------------------------------------------


def _coerce_kg_home(workspace: str) -> str:
    """Map a workspace root to its KG home (`<ws>/.omicsclaw/knowledge`).

    Mirrors ``surfaces/desktop/server.py:_coerce_kg_home`` so the in-loop tools
    and the HTTP ``/kg`` mount resolve the same directory. If ``workspace``
    already *is* a ``.omicsclaw/knowledge`` path it is used as-is.
    """
    path = Path(workspace).expanduser()
    if path.name == "knowledge" and path.parent.name == ".omicsclaw":
        return str(path)
    return str(path / ".omicsclaw" / "knowledge")


def _resolve_kg_home() -> str | None:
    """Resolve the single shared KG home for in-loop read tools.

    Precedence (mirrors the desktop ``/kg`` mount, minus the request header):
      1. ``OMICSCLAW_KG_HOME`` — explicit override.
      2. ``OMICSCLAW_WORKSPACE`` — the active desktop workspace, coerced to its
         ``.omicsclaw/knowledge`` home.
      3. ``None`` — defer to ``omicsclaw_kg.config.resolve()`` (walk-up / default).
    """
    explicit = str(os.getenv("OMICSCLAW_KG_HOME", "") or "").strip()
    if explicit:
        return explicit
    workspace = str(os.getenv("OMICSCLAW_WORKSPACE", "") or "").strip()
    if workspace:
        return _coerce_kg_home(workspace)
    return None


def _import_kg():
    """Lazily import the optional KG read-tool module; ``None`` if unavailable.

    The single seam tests monkeypatch to exercise the soft-fail path. Only
    ``ImportError`` is swallowed — a genuinely broken (but importable) KG is left
    to surface through each executor's call-site ``try/except``.
    """
    try:
        from omicsclaw_kg.mcp_server import tools as kg

        return kg
    except ImportError:
        return None


def _as_int(value: Any, default: int) -> int:
    """Coerce an LLM-supplied argument to int, falling back on bad input."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _is_safe_slug(slug: str) -> bool:
    """Reject slugs that aren't a single safe filename segment (defense-in-depth).

    ``kg_get_page`` builds ``<wiki_subdir>/<slug>.md`` and the upstream KG does
    NOT sanitize the slug, so an LLM-supplied ``slug`` like ``../../secret`` would
    traverse out of the wiki dir and read an arbitrary ``.md`` file. These tools
    are offered to all bot users (incl. untrusted IM) and the slug is fully
    attacker-controllable via the tool call, so the slug is gated *here* before
    it reaches the upstream path build: a single segment of ``[A-Za-z0-9._-]``
    with no ``..``. Bars path separators (``/`` ``\\``) and parent refs.
    """
    return bool(_SAFE_SLUG_RE.match(slug)) and ".." not in slug


# ---------------------------------------------------------------------------
# Result formatters (dict -> readable text). Each surfaces a KG ``error`` verbatim.
# ---------------------------------------------------------------------------


def _fmt_search(r: dict[str, Any]) -> str:
    if "error" in r:
        return f"Knowledge graph search error: {r['error']}"
    hits = r.get("hits") or []
    returned = r.get("returned", len(hits))
    total = r.get("total", returned)
    if not hits:
        return f"No matching knowledge-graph pages (0 of {total})."
    lines = [f"Knowledge graph search — {returned} of {total} matches:"]
    for h in hits:
        terms = ", ".join(h.get("matched_terms") or [])
        lines.append(
            f"- [{h.get('page_type')}/{h.get('slug')}] {h.get('title')} "
            f"(score={h.get('score')}; matched: {terms})"
        )
    if total > returned:
        lines.append(f"... {total - returned} more (raise `limit` to page further).")
    return "\n".join(lines)


def _fmt_page(r: dict[str, Any]) -> str:
    if "error" in r:
        return f"Knowledge graph page error: {r['error']}"
    parts = [f"# {r.get('page_type')}/{r.get('slug')}  ({r.get('path')})"]
    fm = r.get("frontmatter") or {}
    if fm:
        parts.append("Frontmatter:\n" + "\n".join(f"  {k}: {v}" for k, v in fm.items()))
    parts.append("Body:\n" + (r.get("body") or "").strip())
    if "notes" in r:
        parts.append("Notes:\n" + (r.get("notes") or "").strip())
    return "\n\n".join(parts)


def _fmt_list(r: dict[str, Any]) -> str:
    if "error" in r:
        return f"Knowledge graph list error: {r['error']}"
    pages = r.get("pages") or []
    returned = r.get("returned", len(pages))
    total = r.get("total", returned)
    if not pages:
        return f"No knowledge-graph pages found (0 of {total})."
    lines = [f"Knowledge graph pages — {returned} of {total}:"]
    for p in pages:
        extra = []
        if p.get("state"):
            extra.append(f"state={p['state']}")
        if p.get("status"):
            extra.append(f"status={p['status']}")
        if p.get("knowledge_state"):
            extra.append(f"knowledge_state={p['knowledge_state']}")
        suffix = f" ({'; '.join(extra)})" if extra else ""
        lines.append(f"- {p.get('slug')}: {p.get('title')}{suffix}")
    if total > returned:
        lines.append(f"... {total - returned} more (raise `limit`).")
    return "\n".join(lines)


def _fmt_neighbors(r: dict[str, Any]) -> str:
    if "error" in r:
        return f"Knowledge graph neighbors error: {r['error']}"
    node = r.get("node") or {}
    nbrs = r.get("neighbors") or []
    edges = r.get("edges") or []
    lines = [
        f"Node {node.get('id')} ({node.get('node_type')}): {node.get('label')} — "
        f"{len(nbrs)} neighbor(s) within depth {r.get('depth')}:"
    ]
    for n in nbrs:
        lines.append(f"- {n.get('id')} ({n.get('node_type')}): {n.get('label')}")
    if edges:
        lines.append(f"Edges ({len(edges)}):")
        for e in edges:
            ets = ", ".join(e.get("edge_types") or [])
            lines.append(f"  {e.get('source')} -[{ets}]-> {e.get('target')}")
    return "\n".join(lines)


def _fmt_status(r: dict[str, Any]) -> str:
    if "error" in r:
        return f"Knowledge graph status error: {r['error']}"
    wc = r.get("wiki_counts") or {}
    wc_line = ", ".join(f"{k}={v}" for k, v in wc.items())
    lines = [
        f"KG home: {r.get('kg_home')}",
        f"Wiki pages: {r.get('wiki_total')} total"
        + (f" ({wc_line})" if wc_line else ""),
        f"Graph: {r.get('graph_nodes')} nodes, {r.get('graph_edges')} edges",
    ]
    nbt = r.get("graph_nodes_by_type") or {}
    if nbt:
        lines.append("Nodes by type: " + ", ".join(f"{k}={v}" for k, v in nbt.items()))
    ebc = r.get("graph_edges_by_confidence") or {}
    if ebc:
        lines.append(
            "Edges by confidence: " + ", ".join(f"{k}={v}" for k, v in ebc.items())
        )
    return "\n".join(lines)


def _fmt_log(r: dict[str, Any]) -> str:
    if "error" in r:
        return f"Knowledge graph log error: {r['error']}"
    entries = r.get("entries") or []
    if not entries:
        return "No knowledge-graph log entries."
    lines = [f"Recent KG log ({len(entries)} entries, newest first):"]
    for e in entries:
        fields = e.get("fields") or {}
        fstr = " ".join(f"{k}={v}" for k, v in fields.items())
        lines.append(
            f"- {e.get('timestamp')} [{e.get('event_type')}] {e.get('subject')} {fstr}".rstrip()
        )
    return "\n".join(lines)


def _fmt_communities(r: dict[str, Any]) -> str:
    if "error" in r:
        return f"Knowledge graph communities error: {r['error']}"
    comms = r.get("communities") or []
    lines = [
        f"Knowledge communities ({r.get('algorithm')}): {r.get('n_communities')} "
        f"cluster(s) over {r.get('n_nodes_total')} nodes "
        f"(modularity={r.get('modularity')}):"
    ]
    for c in comms:
        keys = ", ".join(c.get("key_nodes") or [])
        lines.append(f"- community {c.get('id')} (size={c.get('size')}): {keys}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Executors. Signature ``(args, **kwargs) -> str``: KG read tools need no
# context_params (shared home from env), so **kwargs is accepted but unused.
# ---------------------------------------------------------------------------


async def execute_kg_search(args: dict, **kwargs) -> str:
    """BM25 search across every knowledge-graph wiki page."""
    kg = _import_kg()
    if kg is None:
        return _KG_UNAVAILABLE_HINT
    query = str(args.get("query", "") or "").strip()
    if not query:
        return "Error: 'query' parameter is required."
    try:
        result = kg.kg_search(
            query=query,
            page_type=args.get("page_type") or None,
            state=args.get("state") or None,
            status=args.get("status") or None,
            field=args.get("field") or None,
            limit=_as_int(args.get("limit", 10), 10),
            home=_resolve_kg_home(),
        )
    except Exception as e:
        logger.error("kg_search failed: %s", e, exc_info=True)
        return f"Error searching knowledge graph: {e}"
    return _fmt_search(result)


async def execute_kg_get_page(args: dict, **kwargs) -> str:
    """Fetch a single knowledge-graph wiki page (frontmatter + body)."""
    kg = _import_kg()
    if kg is None:
        return _KG_UNAVAILABLE_HINT
    page_type = str(args.get("page_type", "") or "").strip()
    slug = str(args.get("slug", "") or "").strip()
    if not page_type or not slug:
        return "Error: 'page_type' and 'slug' parameters are required."
    if not _is_safe_slug(slug):
        return (
            "Error: invalid slug. A slug must be a single page name "
            "(letters, digits, '.', '_', '-') — no path separators or '..'."
        )
    try:
        result = kg.kg_get_page(
            page_type=page_type,
            slug=slug,
            include_notes=bool(args.get("include_notes", False)),
            home=_resolve_kg_home(),
        )
    except Exception as e:
        logger.error("kg_get_page failed: %s", e, exc_info=True)
        return f"Error reading knowledge-graph page: {e}"
    return _fmt_page(result)


async def execute_kg_list_pages(args: dict, **kwargs) -> str:
    """List knowledge-graph pages of a given type, optionally filtered."""
    kg = _import_kg()
    if kg is None:
        return _KG_UNAVAILABLE_HINT
    page_type = str(args.get("page_type", "") or "").strip()
    if not page_type:
        return "Error: 'page_type' parameter is required."
    try:
        result = kg.kg_list_pages(
            page_type=page_type,
            state=args.get("state") or None,
            status=args.get("status") or None,
            limit=_as_int(args.get("limit", 50), 50),
            home=_resolve_kg_home(),
        )
    except Exception as e:
        logger.error("kg_list_pages failed: %s", e, exc_info=True)
        return f"Error listing knowledge-graph pages: {e}"
    return _fmt_list(result)


async def execute_kg_graph_neighbors(args: dict, **kwargs) -> str:
    """Return the graph neighborhood of a knowledge-graph node."""
    kg = _import_kg()
    if kg is None:
        return _KG_UNAVAILABLE_HINT
    node_id = str(args.get("node_id", "") or "").strip()
    if not node_id:
        return "Error: 'node_id' parameter is required."
    try:
        result = kg.kg_graph_neighbors(
            node_id=node_id,
            depth=_as_int(args.get("depth", 1), 1),
            home=_resolve_kg_home(),
        )
    except Exception as e:
        logger.error("kg_graph_neighbors failed: %s", e, exc_info=True)
        return f"Error traversing knowledge graph: {e}"
    return _fmt_neighbors(result)


async def execute_kg_status(args: dict, **kwargs) -> str:
    """Report wiki + graph counts for the knowledge graph."""
    kg = _import_kg()
    if kg is None:
        return _KG_UNAVAILABLE_HINT
    try:
        result = kg.kg_status(home=_resolve_kg_home())
    except Exception as e:
        logger.error("kg_status failed: %s", e, exc_info=True)
        return f"Error reading knowledge-graph status: {e}"
    return _fmt_status(result)


async def execute_kg_recent_log(args: dict, **kwargs) -> str:
    """Return recent knowledge-graph activity log entries (newest first)."""
    kg = _import_kg()
    if kg is None:
        return _KG_UNAVAILABLE_HINT
    try:
        result = kg.kg_recent_log(
            limit=_as_int(args.get("limit", 20), 20),
            event_type=args.get("event_type") or None,
            home=_resolve_kg_home(),
        )
    except Exception as e:
        logger.error("kg_recent_log failed: %s", e, exc_info=True)
        return f"Error reading knowledge-graph log: {e}"
    return _fmt_log(result)


async def execute_kg_communities(args: dict, **kwargs) -> str:
    """Detect knowledge clusters in the graph and return the top N."""
    kg = _import_kg()
    if kg is None:
        return _KG_UNAVAILABLE_HINT
    try:
        result = kg.kg_communities(
            limit=_as_int(args.get("limit", 10), 10),
            algorithm=str(args.get("algorithm", "louvain") or "louvain"),
            home=_resolve_kg_home(),
        )
    except Exception as e:
        logger.error("kg_communities failed: %s", e, exc_info=True)
        return f"Error detecting knowledge communities: {e}"
    return _fmt_communities(result)


# ---------------------------------------------------------------------------
# KG ingest (Bench Phase 3.3c, RD-INGEST-9) — build the citation substrate.
# ---------------------------------------------------------------------------


def _import_kg_ingest():
    """Lazy soft-import of the KG ingest entrypoint + config. ``None`` if absent."""
    try:
        from omicsclaw_kg import config as kg_config
        from omicsclaw_kg.cli import cmd_ingest

        return kg_config, cmd_ingest
    except ImportError:
        return None


class _OmicsClawKGExtractor:
    """Adapt OmicsClaw's OpenAI-compatible LLM to the KG ingest ``LLMClient``.

    KG's ingest pipeline calls ``call_extractor(text, prompt)`` *synchronously*,
    but OmicsClaw's runtime client (``_core.llm``) is ``AsyncOpenAI``. So this
    holds a *sync* ``openai.OpenAI`` built from the same credentials and mirrors
    KG ``AnthropicLLMClient.call_extractor``'s request + JSON-fence handling —
    avoiding any dependency on ``ANTHROPIC_API_KEY``.
    """

    def __init__(self, client: Any, model: str) -> None:
        self._client = client
        self._model = model

    def call_extractor(self, text: str, prompt: str) -> dict[str, Any]:
        max_chars = 60_000
        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n[... truncated by ingest pipeline ...]"
        resp = self._client.chat.completions.create(
            model=self._model,
            max_tokens=4096,  # bound output cost (mirrors KG AnthropicLLMClient)
            messages=[
                {
                    "role": "user",
                    "content": (
                        prompt
                        + "\n\n---\n\nSOURCE TEXT:\n\n"
                        + text
                        + "\n\nReturn ONLY valid JSON, no markdown fences."
                    ),
                }
            ],
        )
        body = (resp.choices[0].message.content or "").strip()
        if body.startswith("```"):
            body = body.split("\n", 1)[1] if "\n" in body else body
            if body.endswith("```"):
                body = body.rsplit("```", 1)[0]
        return json.loads(body)


def _build_kg_extractor() -> Any | None:
    """Construct the ingest LLM adapter from OmicsClaw's configured client.

    Returns ``None`` when no LLM is configured (ingest then cannot extract).
    This is the test seam: monkeypatch to return a ``StubLLMClient``.
    """
    from omicsclaw.runtime.agent import state as _core

    client = getattr(_core, "llm", None)
    model = str(getattr(_core, "OMICSCLAW_MODEL", "") or "")
    if client is None:
        return None
    try:
        from openai import OpenAI

        sync_client = OpenAI(
            api_key=getattr(client, "api_key", None),
            base_url=str(getattr(client, "base_url", "") or "") or None,
        )
    except Exception:  # pragma: no cover - construction failure degrades to "no LLM"
        return None
    return _OmicsClawKGExtractor(sync_client, model)


def _resolve_ingest_source(args: dict, session_id: str | None) -> tuple[str, bool]:
    """Resolve the ingest source.

    Returns ``(source, is_explicit)``. ``is_explicit`` is True for an
    LLM-supplied ``args['source']`` (which the caller MUST path-guard before
    ingesting); False for a freshly dropped PDF (a server-controlled, trusted
    location). Returns ``("", False)`` when neither is available.
    """
    source = str(args.get("source", "") or "").strip()
    if source:
        return source, True
    try:
        from omicsclaw.runtime.agent import state as _core

        for _cid, info in (getattr(_core, "received_files", None) or {}).items():
            fp = info.get("path", "") if isinstance(info, dict) else ""
            if fp and Path(fp).suffix.lower() == ".pdf":
                return fp, False
    except Exception:
        pass
    return "", False


def _fmt_ingest(r: Any, source: str) -> str:
    if not isinstance(r, dict):
        return f"Knowledge graph ingest completed for {source}."
    if "error" in r:
        return f"Knowledge graph ingest error: {r['error']}"
    status = r.get("status")
    if status == "skipped":
        return f"Ingest skipped {source}: {r.get('reason', 'already ingested')}."
    if status == "ingested":
        counts = r.get("counts") or {}
        cstr = ", ".join(f"{k}={v}" for k, v in counts.items())
        return (
            f"Ingested into the knowledge graph: {r.get('slug', source)} "
            f"(source page: {r.get('source_page', '?')}"
            + (f"; {cstr}" if cstr else "")
            + ")."
        )
    # Batch (directory) ingest — summarize counts, don't dump the raw envelope.
    if "dir" in r or "results" in r or status in ("batch_complete", "complete"):
        results = r.get("results") or []
        ingested = sum(1 for x in results if isinstance(x, dict) and x.get("status") == "ingested")
        return (
            f"Batch ingest of {r.get('dir', source)}: "
            f"{ingested}/{len(results)} sources ingested."
        )
    # Unknown shape — a compact, bounded summary (never dump a huge raw envelope).
    keys = ", ".join(sorted(r.keys()))
    return f"Knowledge graph ingest completed for {source} (status={status}; fields: {keys})."


async def execute_kg_ingest(args: dict, session_id: str | None = None, **kwargs) -> str:
    """Ingest a paper/source into the knowledge graph (citation substrate).

    Bench Phase 3.3c (RD-INGEST-9): creates a Source page (+ extracted entities/
    concepts/methods) the agent can cite. The KG is shared reading knowledge
    (ADR 0019) so this is global / not thread-scoped and ungated (the gated
    action is the dataset download in ``parse_literature``). Soft-fails when KG
    or the LLM is unavailable; never raises into the loop. Heavy work runs off
    the event loop via ``asyncio.to_thread``.
    """
    imported = _import_kg_ingest()
    if imported is None:
        return _KG_UNAVAILABLE_HINT
    kg_config, cmd_ingest = imported

    source, is_explicit = _resolve_ingest_source(args, session_id)
    if not source:
        return (
            "Error: no source to ingest. Provide a 'source' (a file path or URL), "
            "or drop a PDF into the chat."
        )

    # Security (Phase 3.3c): kg_ingest is AUTO-approved and reachable by untrusted
    # IM users, so an LLM-supplied LOCAL path must be confined to a trusted data
    # directory — otherwise it would read+exfiltrate any host file to the LLM and
    # persist it into the shared KG. http(s) URLs are left to KG's validate_url
    # (SSRF guard); a dropped PDF is already in a server-controlled location.
    if is_explicit and not source.lower().startswith(("http://", "https://")):
        from omicsclaw.services.path_validation import validate_input_path

        if validate_input_path(source, allow_dir=True) is None:
            return (
                "Access denied: the ingest source is not inside a trusted data "
                "directory. Drop the file into the chat, place it under the "
                "workspace/data directory, or pass an http(s) URL."
            )

    extractor = _build_kg_extractor()
    if extractor is None:
        return (
            "Knowledge-graph ingest needs an LLM to extract entities, but no LLM "
            "client is configured. Configure LLM_API_KEY / LLM_BASE_URL to enable it."
        )

    try:
        cfg = kg_config.resolve(_resolve_kg_home())
        result = await asyncio.to_thread(cmd_ingest.ingest, source, cfg, extractor)
    except Exception as e:
        logger.error("kg_ingest failed: %s", e, exc_info=True)
        return f"Error ingesting into the knowledge graph: {e}"
    return _fmt_ingest(result, source)


# ---------------------------------------------------------------------------
# KG handoff (Bench Ideate→Analyze, ADR 0021 §4/§5/§6) — close the verdict loop.
# These two write tools let the agent, while testing a hypothesis in the Analyze
# stage, link the hypothesis to its analysis (build a packet) and record the
# outcome (which SUGGESTS a verdict the user confirms in Ideate). They are write
# tools, so they are NOT in _READ_STAGE_TOOLS (excluded from Read/Ideate); Analyze
# is unfiltered, so they are available there.
# ---------------------------------------------------------------------------


def _import_kg_handoff():
    """Lazy soft-import of the KG handoff entrypoints. ``None`` if KG is absent."""
    try:
        from omicsclaw_kg import config as kg_config
        from omicsclaw_kg import paths as kg_paths
        from omicsclaw_kg.fs_utils import atomic_write_text
        from omicsclaw_kg.handoff import build_packet, write_packet
        from omicsclaw_kg.handoff.feedback import RecordResultError, record_result
        from omicsclaw_kg.handoff.result import HandoffResult

        return SimpleNamespace(
            config=kg_config,
            paths=kg_paths,
            atomic_write_text=atomic_write_text,
            build_packet=build_packet,
            write_packet=write_packet,
            record_result=record_result,
            RecordResultError=RecordResultError,
            HandoffResult=HandoffResult,
        )
    except ImportError:
        return None


async def execute_kg_build_packet(args: dict, **kwargs) -> str:
    """Build a handoff packet for a hypothesis (ADR 0021 §5) — one hypothesis → one
    packet → the Analysis Router. Returns the ``packet_id`` to record a result
    against. Soft-fails when KG is unavailable; never raises into the loop.
    """
    kg = _import_kg_handoff()
    if kg is None:
        return _KG_UNAVAILABLE_HINT

    slug = str(args.get("hypothesis_slug", "") or "").strip()
    if not slug:
        return "Error: 'hypothesis_slug' is required."
    if not _is_safe_slug(slug):
        return f"Error: invalid hypothesis_slug {slug!r}."
    target_skill = str(args.get("target_skill", "") or "").strip() or None
    notes = str(args.get("notes", "") or "").strip() or None

    try:
        cfg = kg.config.resolve(_resolve_kg_home())
        packet = await asyncio.to_thread(kg.build_packet, cfg, slug, target_skill, notes)
        await asyncio.to_thread(kg.write_packet, cfg, packet)
    except (FileNotFoundError, ValueError) as e:
        return f"Error building handoff packet: {e}"
    except Exception as e:  # pragma: no cover - defensive
        logger.error("kg_build_packet failed: %s", e, exc_info=True)
        return f"Error building handoff packet: {e}"

    skill = packet.target.skill_name or "(unresolved — file_drop)"
    return (
        f"Built handoff packet `{packet.packet_id}` for hypothesis `{slug}` "
        f"(target skill: {skill}, kind: {packet.target.kind}). After running the "
        f"analysis, record the outcome with kg_record_result(packet_id="
        f"'{packet.packet_id}', verdict=<validated|refuted|refined|inconclusive>, summary=...)."
    )


async def execute_kg_record_result(args: dict, **kwargs) -> str:
    """Record an analysis outcome against a handoff packet (ADR 0021 §6).

    This SUGGESTS a verdict on the hypothesis (it does NOT flip its status); the
    user confirms in the Ideate stage. Soft-fails when KG is unavailable.
    """
    kg = _import_kg_handoff()
    if kg is None:
        return _KG_UNAVAILABLE_HINT

    packet_id = str(args.get("packet_id", "") or "").strip()
    if not packet_id:
        return "Error: 'packet_id' is required (from kg_build_packet)."
    if not _is_safe_slug(packet_id):
        return f"Error: invalid packet_id {packet_id!r}."
    summary = str(args.get("summary", "") or "").strip()
    if not summary:
        return "Error: 'summary' is required (a one-line statement of the finding)."
    verdict = str(args.get("verdict", "") or "").strip()
    raw_artifacts = args.get("artifact_paths")
    # Guard against an LLM passing a bare string (which would char-split into a
    # list of single characters and still satisfy the list[str] schema).
    artifacts = [str(a) for a in raw_artifacts] if isinstance(raw_artifacts, list) else []
    refined = str(args.get("refined_hypothesis_slug", "") or "").strip() or None
    notes = str(args.get("notes", "") or "").strip() or None

    try:
        result = kg.HandoffResult(
            packet_id=packet_id,
            completed=datetime.now(timezone.utc),
            verdict=verdict,  # type: ignore[arg-type]
            summary=summary,
            artifact_paths=artifacts,
            refined_hypothesis_slug=refined,
            notes=notes,
        )
    except Exception as e:  # pydantic validation (bad verdict / refined-without-slug)
        return f"Error: invalid result — {e}"

    try:
        cfg = kg.config.resolve(_resolve_kg_home())
        staging = kg.paths.cache_dir(cfg) / "result_staging"
        staging.mkdir(parents=True, exist_ok=True)
        rf = staging / f"{packet_id}.json"
        kg.atomic_write_text(rf, result.model_dump_json(indent=2))
        out = await asyncio.to_thread(kg.record_result, cfg, packet_id, rf)
    except kg.RecordResultError as e:
        return f"Error recording result: {e}"
    except Exception as e:  # pragma: no cover - defensive
        logger.error("kg_record_result failed: %s", e, exc_info=True)
        return f"Error recording result: {e}"

    return (
        f"Recorded result for packet `{packet_id}` (hypothesis `{out.get('hypothesis_slug')}`): "
        f"suggested verdict **{out.get('suggested_verdict')}**. The hypothesis status stays "
        f"'{out.get('hypothesis_status')}' until the user confirms it in the Ideate stage."
    )


KG_TOOL_EXECUTORS: dict[str, Any] = {
    "kg_search": execute_kg_search,
    "kg_get_page": execute_kg_get_page,
    "kg_list_pages": execute_kg_list_pages,
    "kg_graph_neighbors": execute_kg_graph_neighbors,
    "kg_status": execute_kg_status,
    "kg_recent_log": execute_kg_recent_log,
    "kg_communities": execute_kg_communities,
    "kg_ingest": execute_kg_ingest,
    "kg_build_packet": execute_kg_build_packet,
    "kg_record_result": execute_kg_record_result,
}
