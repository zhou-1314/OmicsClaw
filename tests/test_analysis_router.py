"""Tests for first-class analysis route contracts."""

from omicsclaw.analysis_router import AnalysisRouter, AnalysisRouteKind
from omicsclaw.skill.capability_resolver import CapabilityDecision


def _decision(
    *,
    query: str = "run something",
    coverage: str = "no_skill",
    chosen_skill: str = "",
    confidence: float = 0.0,
    should_search_web: bool = False,
    reasoning: list[str] | None = None,
) -> CapabilityDecision:
    return CapabilityDecision(
        query=query,
        coverage=coverage,
        chosen_skill=chosen_skill,
        confidence=confidence,
        should_search_web=should_search_web,
        reasoning=reasoning or [],
    )


def test_router_routes_help_request_to_chat() -> None:
    router = AnalysisRouter(
        resolver=lambda query, **_: _decision(
            query=query,
            coverage="no_skill",
            reasoning=["request does not look like an omics analysis task"],
        )
    )

    route = router.route("help")

    assert route.kind is AnalysisRouteKind.CHAT
    assert route.capability_decision.reasoning == [
        "request does not look like an omics analysis task"
    ]
    assert route.preflight_required is False
    assert route.missing_params == []


def test_router_routes_empty_non_analysis_decision_to_chat() -> None:
    router = AnalysisRouter(
        resolver=lambda query, **_: _decision(
            query=query,
            coverage="no_skill",
            reasoning=["empty request"],
        )
    )

    route = router.route("")

    assert route.kind is AnalysisRouteKind.CHAT
    assert route.is_chat is True


def test_router_maps_exact_skill_decision() -> None:
    router = AnalysisRouter(
        resolver=lambda query, **_: _decision(
            query=query,
            coverage="exact_skill",
            chosen_skill="spatial-preprocess",
            confidence=0.86,
        )
    )

    route = router.route("run spatial preprocessing")

    assert route.kind is AnalysisRouteKind.EXACT_SKILL
    assert route.chosen_skill == "spatial-preprocess"
    assert route.confidence == 0.86


def test_router_maps_partial_skill_decision() -> None:
    router = AnalysisRouter(
        resolver=lambda query, **_: _decision(
            query=query,
            coverage="partial_skill",
            chosen_skill="spatial-preprocess",
            should_search_web=True,
            reasoning=["query requests web/literature lookups"],
        )
    )

    route = router.route("run preprocessing and then check latest docs")

    assert route.kind is AnalysisRouteKind.PARTIAL_SKILL
    assert route.chosen_skill == "spatial-preprocess"
    assert route.should_search_web is True


def test_router_maps_analysis_no_skill_decision() -> None:
    router = AnalysisRouter(
        resolver=lambda query, **_: _decision(
            query=query,
            coverage="no_skill",
            confidence=0.2,
            reasoning=["no skill achieved a meaningful semantic match"],
        )
    )

    route = router.route("compute a brand new multi-omics score")

    assert route.kind is AnalysisRouteKind.NO_SKILL
    assert route.is_chat is False
    assert route.chosen_skill == ""
    assert route.confidence == 0.2


def test_router_passes_file_path_and_domain_hint_to_resolver() -> None:
    calls: dict[str, str] = {}

    def resolver(query: str, *, file_path: str = "", domain_hint: str = ""):
        calls["query"] = query
        calls["file_path"] = file_path
        calls["domain_hint"] = domain_hint
        return _decision(
            query=query,
            coverage="exact_skill",
            chosen_skill="bulkrna-de",
        )

    router = AnalysisRouter(resolver=resolver)

    route = router.route(
        "run differential expression",
        file_path="/tmp/counts.csv",
        domain_hint="bulkrna",
    )

    assert route.kind is AnalysisRouteKind.EXACT_SKILL
    assert calls == {
        "query": "run differential expression",
        "file_path": "/tmp/counts.csv",
        "domain_hint": "bulkrna",
    }


def test_real_resolver_help_request_routes_to_chat() -> None:
    route = AnalysisRouter().route("What is OmicsClaw and how do I install it?")

    assert route.kind is AnalysisRouteKind.CHAT
    assert route.chosen_skill == ""


def test_loop_formats_non_chat_route_context() -> None:
    from omicsclaw.runtime.agent.loop import _format_analysis_route_context

    route = AnalysisRouter(
        resolver=lambda query, **_: _decision(
            query=query,
            coverage="partial_skill",
            chosen_skill="spatial-preprocess",
            confidence=0.75,
            should_search_web=True,
            reasoning=["query requests web/literature lookups"],
        )
    ).route("run preprocessing and a custom figure")

    context = _format_analysis_route_context(route)

    assert "## Analysis Router" in context
    assert "route_kind: partial_skill" in context
    assert "chosen_skill: spatial-preprocess" in context
    assert "skill-first composition" in context


def test_loop_omits_chat_route_context() -> None:
    from omicsclaw.runtime.agent.loop import _format_analysis_route_context

    route = AnalysisRouter(
        resolver=lambda query, **_: _decision(
            query=query,
            coverage="no_skill",
            reasoning=["request does not look like an omics analysis task"],
        )
    ).route("help")

    assert _format_analysis_route_context(route) == ""


def test_loop_analysis_router_defaults_to_assist_mode(monkeypatch) -> None:
    from omicsclaw.runtime.agent.loop import (
        _analysis_router_enabled,
        _analysis_router_auto_execute_enabled,
        _normalize_analysis_router_mode,
        _build_analysis_route_context,
    )

    monkeypatch.delenv("OMICSCLAW_ANALYSIS_ROUTER_ENABLED", raising=False)
    monkeypatch.delenv("OMICSCLAW_ANALYSIS_ROUTER_MODE", raising=False)

    assert _normalize_analysis_router_mode() == "assist"
    assert _analysis_router_enabled() is True
    assert _analysis_router_auto_execute_enabled() is False
    assert "## Analysis Router" in _build_analysis_route_context(
        "run spatial preprocessing"
    )


def test_loop_analysis_router_mode_off_disables_context_and_auto(monkeypatch) -> None:
    from omicsclaw.runtime.agent.loop import (
        _analysis_router_auto_execute_enabled,
        _analysis_router_enabled,
        _build_analysis_route_context,
    )

    monkeypatch.setenv("OMICSCLAW_ANALYSIS_ROUTER_MODE", "off")

    assert _analysis_router_enabled() is False
    assert _analysis_router_auto_execute_enabled() is False
    assert _build_analysis_route_context("run spatial preprocessing") == ""


def test_loop_analysis_router_mode_auto_enables_deterministic_dispatch(monkeypatch) -> None:
    from omicsclaw.runtime.agent.loop import (
        _analysis_router_auto_execute_enabled,
        _analysis_router_enabled,
        _normalize_analysis_router_mode,
    )

    monkeypatch.setenv("OMICSCLAW_ANALYSIS_ROUTER_MODE", "auto")

    assert _normalize_analysis_router_mode() == "auto"
    assert _analysis_router_enabled() is True
    assert _analysis_router_auto_execute_enabled() is True


def test_loop_analysis_router_legacy_boolean_maps_to_auto(monkeypatch) -> None:
    from omicsclaw.runtime.agent.loop import _normalize_analysis_router_mode

    monkeypatch.delenv("OMICSCLAW_ANALYSIS_ROUTER_MODE", raising=False)
    monkeypatch.setenv("OMICSCLAW_ANALYSIS_ROUTER_ENABLED", "true")

    assert _normalize_analysis_router_mode() == "auto"


def test_understanding_preflight_injects_schema_for_no_skill_file(monkeypatch) -> None:
    """ADR 0014: a no_skill/partial route with a trusted input file gets a
    deterministic inspect_data schema plus the plan/validate/interpret directive."""
    import asyncio

    import omicsclaw.runtime.agent.loop as loop
    import omicsclaw.runtime.tools.builders.agent_executors as execs

    monkeypatch.delenv("OMICSCLAW_ANALYSIS_ROUTER_MODE", raising=False)
    monkeypatch.setattr(loop, "extract_valid_input_paths", lambda text: ["/trusted/x.h5ad"])

    async def _fake_inspect(args):
        assert args["file_path"] == "/trusted/x.h5ad"
        return "## Data Inspection: `x.h5ad`\n\n| Platform | Spatial transcriptomics |"

    monkeypatch.setattr(execs, "execute_inspect_data", _fake_inspect)

    ctx = asyncio.run(
        loop._build_autonomous_understanding_context(
            "compute a custom novel graph autocorrelation metric not in omicsclaw"
        )
    )
    assert "Autonomous Understanding Preflight" in ctx
    assert "## Data Inspection" in ctx
    assert "`data_schema`" in ctx and "`analysis_plan`" in ctx


def test_understanding_preflight_is_noop_for_chat_exact_and_no_file(monkeypatch) -> None:
    """The preflight only fires for no_skill/partial routes that carry a trusted
    file; chat / exact-skill / no-path / off-mode are all silent no-ops."""
    import asyncio

    import omicsclaw.runtime.agent.loop as loop
    import omicsclaw.runtime.tools.builders.agent_executors as execs

    monkeypatch.delenv("OMICSCLAW_ANALYSIS_ROUTER_MODE", raising=False)

    inspect_calls = {"n": 0}

    async def _fake_inspect(args):
        inspect_calls["n"] += 1
        return "## Data Inspection: `x.h5ad`"

    monkeypatch.setattr(execs, "execute_inspect_data", _fake_inspect)

    def _run(query):
        return asyncio.run(loop._build_autonomous_understanding_context(query))

    monkeypatch.setattr(loop, "extract_valid_input_paths", lambda text: ["/trusted/x.h5ad"])
    assert _run("hello how are you") == ""  # chat route
    assert _run("run spatial preprocessing") == ""  # exact_skill route

    # no_skill route but no trusted path -> no inspection attempted
    monkeypatch.setattr(loop, "extract_valid_input_paths", lambda text: [])
    before = inspect_calls["n"]
    assert _run("compute a custom novel graph autocorrelation metric not in omicsclaw") == ""
    assert inspect_calls["n"] == before

    # router disabled -> no-op even with a file
    monkeypatch.setattr(loop, "extract_valid_input_paths", lambda text: ["/trusted/x.h5ad"])
    monkeypatch.setenv("OMICSCLAW_ANALYSIS_ROUTER_MODE", "off")
    assert _run("compute a custom novel graph autocorrelation metric not in omicsclaw") == ""


def test_understanding_preflight_skips_non_h5ad_input(monkeypatch) -> None:
    """A non-.h5ad path makes inspect_data return an error string, so the
    preflight emits nothing and the base route context is left untouched."""
    import asyncio

    import omicsclaw.runtime.agent.loop as loop
    import omicsclaw.runtime.tools.builders.agent_executors as execs

    monkeypatch.delenv("OMICSCLAW_ANALYSIS_ROUTER_MODE", raising=False)
    monkeypatch.setattr(loop, "extract_valid_input_paths", lambda text: ["/trusted/x.csv"])

    async def _fake_inspect(args):
        return "inspect_data only supports .h5ad files. Got: .csv"

    monkeypatch.setattr(execs, "execute_inspect_data", _fake_inspect)

    ctx = asyncio.run(
        loop._build_autonomous_understanding_context(
            "compute a custom novel graph autocorrelation metric not in omicsclaw"
        )
    )
    assert ctx == ""
