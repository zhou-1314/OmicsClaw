"""Contracts for exact-skill assisted parameterization (ADR 0015).

An Exact skill match injects the matched skill's SKILL.md method menu plus
(when a trusted file is present) its inspect_data schema, and a
directive that has the outer LLM recommend the method/parameters *within* that
skill. ``route_analysis_request`` is monkeypatched so these contracts assert the
context-building behavior without depending on capability-scoring thresholds.
"""

from __future__ import annotations

import asyncio

from omicsclaw.analysis_router.models import AnalysisRoute, AnalysisRouteKind
from omicsclaw.runtime.agent import loop
from omicsclaw.skill.capability_resolver import CapabilityDecision


def _route(kind: AnalysisRouteKind, *, chosen_skill: str = "") -> AnalysisRoute:
    return AnalysisRoute(
        kind=kind,
        capability_decision=CapabilityDecision(
            query="test",
            coverage=kind.value,
            chosen_skill=chosen_skill,
            confidence=0.9,
        ),
    )


def _patch_route(monkeypatch, route: AnalysisRoute) -> None:
    monkeypatch.setattr(loop, "route_analysis_request", lambda *_a, **_k: route)


def test_assist_exact_skill_injects_menu_and_directive(monkeypatch) -> None:
    _patch_route(monkeypatch, _route(AnalysisRouteKind.EXACT_SKILL, chosen_skill="spatial-domains"))
    ctx = asyncio.run(
        loop._build_exact_skill_assisted_param_context("identify spatial domains")
    )
    assert "Assisted Parameterization" in ctx          # the directive
    assert "method menu (SKILL.md)" in ctx             # the injected menu header
    assert "cellcharter" in ctx.lower()                # real SKILL.md method content


def test_assist_context_empty_for_non_exact_routes(monkeypatch) -> None:
    for kind in (AnalysisRouteKind.CHAT, AnalysisRouteKind.NO_SKILL, AnalysisRouteKind.PARTIAL_SKILL):
        _patch_route(monkeypatch, _route(kind, chosen_skill="" if kind is AnalysisRouteKind.CHAT else "sc-qc"))
        out = asyncio.run(loop._build_exact_skill_assisted_param_context("hello"))
        assert out == "", f"{kind} should not inject assisted-param context"


def test_assist_context_empty_when_skill_has_no_menu(monkeypatch) -> None:
    # Unknown skill -> no SKILL.md -> no menu; with no input file there is no
    # schema either, so the builder injects nothing rather than a bare directive.
    _patch_route(monkeypatch, _route(AnalysisRouteKind.EXACT_SKILL, chosen_skill="no-such-skill-xyz"))
    assert asyncio.run(loop._build_exact_skill_assisted_param_context("do it")) == ""


def test_format_block_includes_directive_menu_and_optional_schema() -> None:
    full = loop._format_exact_skill_assisted_param_block("MENU-TEXT", "## Data Inspection\nfoo")
    assert "Assisted Parameterization" in full
    assert "MENU-TEXT" in full
    assert "## Data Inspection" in full
    # No schema present -> directive + menu still emitted.
    menu_only = loop._format_exact_skill_assisted_param_block("MENU-TEXT", "")
    assert "Assisted Parameterization" in menu_only
    assert "MENU-TEXT" in menu_only
    assert "## Data Inspection" not in menu_only


def test_load_skill_md_returns_menu_and_handles_missing() -> None:
    from omicsclaw.skill.orchestration import load_skill_md

    md = load_skill_md("spatial-domains")
    assert "cellcharter" in md.lower() and "leiden" in md.lower()
    assert load_skill_md("no-such-skill-xyz") == ""
