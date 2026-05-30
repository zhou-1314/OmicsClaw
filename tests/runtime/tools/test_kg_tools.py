"""Tests for the OmicsClaw-KG in-loop read tools (Bench Phase 3.1, ADR 0019).

Covers:
  (a) soft-fail when the optional ``omicsclaw_kg`` package is unavailable,
  (b) shared KG-home resolution precedence,
  (c) the dict->text formatters + error/exception handling via fake KG functions,
  (d) a real end-to-end ``kg_status`` against an empty tmp KG home, and
  (e) registry wiring: the tools are registered as specs + executors and are
      Read-stage allow-listed on the bot surface.
"""

from __future__ import annotations

import types

import pytest

from omicsclaw.runtime.tools import kg_tools


def _fake_kg(**fns) -> types.SimpleNamespace:
    """A stand-in for ``omicsclaw_kg.mcp_server.tools`` with selected functions."""
    return types.SimpleNamespace(**fns)


# Every executor with a minimal set of valid args.
_EXECUTORS_WITH_ARGS = [
    (kg_tools.execute_kg_search, {"query": "tp53"}),
    (kg_tools.execute_kg_get_page, {"page_type": "sources", "slug": "x"}),
    (kg_tools.execute_kg_list_pages, {"page_type": "hypotheses"}),
    (kg_tools.execute_kg_graph_neighbors, {"node_id": "tp53"}),
    (kg_tools.execute_kg_status, {}),
    (kg_tools.execute_kg_recent_log, {}),
    (kg_tools.execute_kg_communities, {}),
]
_IDS = [executor.__name__ for executor, _ in _EXECUTORS_WITH_ARGS]


# ---- (a) soft-fail when KG unavailable -------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("executor,args", _EXECUTORS_WITH_ARGS, ids=_IDS)
async def test_executors_soft_fail_when_kg_unavailable(executor, args, monkeypatch):
    """Each executor returns the friendly notice (not an exception) when the
    optional package cannot be imported."""
    monkeypatch.setattr(kg_tools, "_import_kg", lambda: None)
    out = await executor(args)
    assert out == kg_tools._KG_UNAVAILABLE_HINT
    assert "not installed" in out


def test_import_kg_swallows_import_error(monkeypatch):
    """``_import_kg`` returns None on ImportError rather than propagating it."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("omicsclaw_kg"):
            raise ImportError("simulated missing package")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert kg_tools._import_kg() is None


# ---- (b) shared KG-home resolution -----------------------------------------


def test_resolve_kg_home_prefers_explicit_kg_home(monkeypatch):
    monkeypatch.setenv("OMICSCLAW_KG_HOME", "/explicit/kg")
    monkeypatch.setenv("OMICSCLAW_WORKSPACE", "/some/ws")
    assert kg_tools._resolve_kg_home() == "/explicit/kg"


def test_resolve_kg_home_coerces_workspace(monkeypatch):
    monkeypatch.delenv("OMICSCLAW_KG_HOME", raising=False)
    monkeypatch.setenv("OMICSCLAW_WORKSPACE", "/some/ws")
    assert kg_tools._resolve_kg_home() == "/some/ws/.omicsclaw/knowledge"


def test_resolve_kg_home_passthrough_for_knowledge_path(monkeypatch):
    monkeypatch.delenv("OMICSCLAW_KG_HOME", raising=False)
    monkeypatch.setenv("OMICSCLAW_WORKSPACE", "/some/ws/.omicsclaw/knowledge")
    assert kg_tools._resolve_kg_home() == "/some/ws/.omicsclaw/knowledge"


def test_resolve_kg_home_none_when_unset(monkeypatch):
    monkeypatch.delenv("OMICSCLAW_KG_HOME", raising=False)
    monkeypatch.delenv("OMICSCLAW_WORKSPACE", raising=False)
    assert kg_tools._resolve_kg_home() is None


# ---- (c) formatters + error/exception handling -----------------------------


@pytest.mark.asyncio
async def test_kg_search_formats_hits_and_pagination(monkeypatch):
    def fake_search(**kwargs):
        return {
            "hits": [
                {
                    "page_type": "sources",
                    "slug": "smith2020",
                    "title": "TP53 in cancer",
                    "score": 4.2,
                    "matched_terms": ["tp53"],
                    "matched_fields": ["title"],
                }
            ],
            "returned": 1,
            "total": 3,
        }

    monkeypatch.setattr(kg_tools, "_import_kg", lambda: _fake_kg(kg_search=fake_search))
    out = await kg_tools.execute_kg_search({"query": "tp53"})
    assert "1 of 3 matches" in out
    assert "[sources/smith2020] TP53 in cancer" in out
    assert "score=4.2" in out
    assert "2 more" in out  # pagination hint when total > returned


@pytest.mark.asyncio
async def test_kg_search_surfaces_kg_error_verbatim(monkeypatch):
    monkeypatch.setattr(
        kg_tools, "_import_kg", lambda: _fake_kg(kg_search=lambda **k: {"error": "boom"})
    )
    out = await kg_tools.execute_kg_search({"query": "x"})
    assert out == "Knowledge graph search error: boom"


@pytest.mark.asyncio
async def test_kg_search_requires_non_empty_query(monkeypatch):
    monkeypatch.setattr(kg_tools, "_import_kg", lambda: _fake_kg(kg_search=lambda **k: {}))
    out = await kg_tools.execute_kg_search({"query": "   "})
    assert "required" in out


@pytest.mark.asyncio
async def test_kg_search_passes_resolved_home_and_coerced_limit(monkeypatch):
    captured: dict = {}

    def fake_search(**kwargs):
        captured.update(kwargs)
        return {"hits": [], "returned": 0, "total": 0}

    monkeypatch.setenv("OMICSCLAW_KG_HOME", "/explicit/kg")
    monkeypatch.setattr(kg_tools, "_import_kg", lambda: _fake_kg(kg_search=fake_search))
    await kg_tools.execute_kg_search({"query": "x", "limit": "7"})
    assert captured["home"] == "/explicit/kg"
    assert captured["limit"] == 7  # string coerced to int


@pytest.mark.asyncio
async def test_executor_catches_underlying_exception(monkeypatch):
    def boom(**k):
        raise RuntimeError("graph corrupt")

    monkeypatch.setattr(kg_tools, "_import_kg", lambda: _fake_kg(kg_status=boom))
    out = await kg_tools.execute_kg_status({})
    assert out.startswith("Error reading knowledge-graph status:")
    assert "graph corrupt" in out


@pytest.mark.asyncio
async def test_kg_graph_neighbors_formats_nodes_and_edges(monkeypatch):
    def fake(**k):
        return {
            "node": {"id": "entity:tp53", "node_type": "entity", "label": "TP53"},
            "neighbors": [
                {"id": "entity:mdm2", "node_type": "entity", "label": "MDM2"}
            ],
            "edges": [
                {
                    "source": "entity:tp53",
                    "target": "entity:mdm2",
                    "edge_types": ["regulates"],
                }
            ],
            "depth": 1,
        }

    monkeypatch.setattr(
        kg_tools, "_import_kg", lambda: _fake_kg(kg_graph_neighbors=fake)
    )
    out = await kg_tools.execute_kg_graph_neighbors({"node_id": "tp53"})
    assert "entity:tp53" in out and "TP53" in out
    assert "MDM2" in out
    assert "entity:tp53 -[regulates]-> entity:mdm2" in out


@pytest.mark.asyncio
async def test_kg_get_page_requires_page_type_and_slug(monkeypatch):
    monkeypatch.setattr(kg_tools, "_import_kg", lambda: _fake_kg(kg_get_page=lambda **k: {}))
    out = await kg_tools.execute_kg_get_page({"page_type": "sources"})
    assert "required" in out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "executor,fn_name,args",
    [
        (kg_tools.execute_kg_list_pages, "kg_list_pages", {}),
        (kg_tools.execute_kg_graph_neighbors, "kg_graph_neighbors", {}),
    ],
    ids=["kg_list_pages_missing_page_type", "kg_graph_neighbors_missing_node_id"],
)
async def test_executor_required_arg_guard(executor, fn_name, args, monkeypatch):
    """A missing required arg returns a friendly error before forwarding upstream."""
    forwarded = {"called": False}

    def fake(**k):
        forwarded["called"] = True
        return {}

    monkeypatch.setattr(kg_tools, "_import_kg", lambda: _fake_kg(**{fn_name: fake}))
    out = await executor(args)
    assert "required" in out
    assert forwarded["called"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_slug",
    ["../../../etc/passwd", "../secret", "a/b", "..", "foo/../bar", "a\\b", "/abs"],
)
async def test_kg_get_page_rejects_traversal_slug(bad_slug, monkeypatch):
    """A path-traversal slug is rejected at the OmicsClaw boundary BEFORE it
    reaches the (unsanitized) upstream kg_get_page (security blocker fix)."""
    forwarded = {"called": False}

    def fake_get_page(**k):
        forwarded["called"] = True
        return {"page_type": "sources", "slug": "x", "frontmatter": {}, "body": "", "path": "x"}

    monkeypatch.setattr(kg_tools, "_import_kg", lambda: _fake_kg(kg_get_page=fake_get_page))
    out = await kg_tools.execute_kg_get_page({"page_type": "sources", "slug": bad_slug})
    assert "invalid slug" in out
    assert forwarded["called"] is False  # rejected before forwarding upstream


@pytest.mark.asyncio
async def test_kg_get_page_forwards_safe_slug_and_formats(monkeypatch):
    """A normal slug is forwarded; covers the _fmt_page happy path."""
    forwarded: dict = {}

    def fake_get_page(**k):
        forwarded.update(k)
        return {
            "page_type": "sources",
            "slug": k["slug"],
            "frontmatter": {"title": "TP53 review"},
            "body": "Body text.",
            "path": "sources/smith-2020.md",
        }

    monkeypatch.setattr(kg_tools, "_import_kg", lambda: _fake_kg(kg_get_page=fake_get_page))
    out = await kg_tools.execute_kg_get_page({"page_type": "sources", "slug": "smith-2020"})
    assert forwarded["slug"] == "smith-2020"
    assert "# sources/smith-2020  (sources/smith-2020.md)" in out
    assert "title: TP53 review" in out
    assert "Body text." in out


@pytest.mark.asyncio
async def test_kg_list_pages_formats(monkeypatch):
    def fake(**k):
        return {
            "pages": [
                {
                    "slug": "h1",
                    "title": "Hypothesis 1",
                    "state": "draft",
                    "status": "testing",
                    "knowledge_state": None,
                }
            ],
            "returned": 1,
            "total": 2,
        }

    monkeypatch.setattr(kg_tools, "_import_kg", lambda: _fake_kg(kg_list_pages=fake))
    out = await kg_tools.execute_kg_list_pages({"page_type": "hypotheses"})
    assert "1 of 2" in out
    assert "h1: Hypothesis 1" in out
    assert "state=draft" in out and "status=testing" in out
    assert "1 more" in out


@pytest.mark.asyncio
async def test_kg_recent_log_formats(monkeypatch):
    def fake(**k):
        return {
            "entries": [
                {
                    "timestamp": "2026-05-30T00:00:00Z",
                    "event_type": "ingest",
                    "subject": "paper-x",
                    "fields": {"n": 3},
                }
            ],
            "total": 1,
        }

    monkeypatch.setattr(kg_tools, "_import_kg", lambda: _fake_kg(kg_recent_log=fake))
    out = await kg_tools.execute_kg_recent_log({})
    assert "1 entries" in out
    assert "[ingest] paper-x" in out
    assert "n=3" in out


@pytest.mark.asyncio
async def test_kg_communities_formats(monkeypatch):
    def fake(**k):
        return {
            "algorithm": "louvain",
            "modularity": 0.42,
            "n_communities": 1,
            "n_nodes_total": 5,
            "communities": [{"id": 0, "size": 3, "key_nodes": ["tp53", "mdm2"]}],
        }

    monkeypatch.setattr(kg_tools, "_import_kg", lambda: _fake_kg(kg_communities=fake))
    out = await kg_tools.execute_kg_communities({})
    assert "louvain" in out
    assert "modularity=0.42" in out
    assert "community 0 (size=3): tp53, mdm2" in out


@pytest.mark.asyncio
async def test_kg_search_coerces_bad_limit_to_default(monkeypatch):
    """_as_int fallback: a non-numeric limit degrades to the default, not a crash."""
    captured: dict = {}

    def fake_search(**k):
        captured.update(k)
        return {"hits": [], "returned": 0, "total": 0}

    monkeypatch.setattr(kg_tools, "_import_kg", lambda: _fake_kg(kg_search=fake_search))
    out = await kg_tools.execute_kg_search({"query": "x", "limit": "all"})
    assert captured["limit"] == 10  # bad input coerced to default
    assert "No matching" in out


# ---- (d) real end-to-end through the genuine KG package --------------------


@pytest.mark.asyncio
async def test_kg_status_real_end_to_end_on_empty_home(monkeypatch, tmp_path):
    """Drive ``execute_kg_status`` through the REAL ``omicsclaw_kg`` package
    against an empty home: it must return readable zero-count text, no crash."""
    pytest.importorskip("omicsclaw_kg")
    monkeypatch.setenv("OMICSCLAW_KG_HOME", str(tmp_path))
    monkeypatch.delenv("OMICSCLAW_WORKSPACE", raising=False)
    out = await kg_tools.execute_kg_status({})
    # The resolved tmp home must actually flow through (not just the literal label).
    assert str(tmp_path.resolve()) in out
    assert "Wiki pages: 0 total" in out
    assert "Graph: 0 nodes, 0 edges" in out


# ---- (e) registry wiring ----------------------------------------------------


def test_kg_executors_registered_in_runtime_dispatch():
    # Load state first (production import order) to avoid the pre-existing
    # state <-> agent_executors import cycle, mirroring tests/bot/test_tool_executors.py.
    import omicsclaw.runtime.agent.state  # noqa: F401
    import omicsclaw.runtime.tools.builders.agent_executors as agent_executors

    table = agent_executors._available_tool_executors()
    for name in kg_tools.KG_TOOL_EXECUTORS:
        assert name in table, f"{name} missing from dispatch table"


def test_kg_specs_are_bot_surface_read_only_and_read_stage_allowed():
    from omicsclaw.runtime.context.layers import ContextAssemblyRequest
    from omicsclaw.runtime.tools.builders.agent import (
        BotToolContext,
        build_bot_tool_specs,
    )
    from omicsclaw.runtime.tools.registry import select_tool_specs

    specs = build_bot_tool_specs(
        BotToolContext(skill_names=("sc-de",), domain_briefing="(test)")
    )
    by_name = {s.name: s for s in specs}
    req = ContextAssemblyRequest(surface="bot")
    read = {
        s.name
        for s in select_tool_specs(specs, request=req, surface_only=True, stage="read")
    }

    for name in kg_tools.KG_TOOL_EXECUTORS:
        assert name in by_name, f"{name} not registered as a ToolSpec"
        assert by_name[name].read_only is True
        assert "bot" in by_name[name].surfaces
        assert name in read, f"{name} should be allowed in the Read stage"
