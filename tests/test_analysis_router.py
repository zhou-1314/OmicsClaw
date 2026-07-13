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
    precondition_status: str = "eligible",
    precondition_evaluated: bool = False,
    execution_ready: bool = True,
    missing_preconditions: list[str] | None = None,
) -> CapabilityDecision:
    return CapabilityDecision(
        query=query,
        coverage=coverage,
        chosen_skill=chosen_skill,
        confidence=confidence,
        should_search_web=should_search_web,
        reasoning=reasoning or [],
        precondition_status=precondition_status,
        precondition_evaluated=precondition_evaluated,
        execution_ready=execution_ready,
        missing_preconditions=missing_preconditions or [],
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


def test_router_requires_preflight_when_selected_skill_needs_preparation() -> None:
    router = AnalysisRouter(
        resolver=lambda query, **_: _decision(
            query=query,
            coverage="exact_skill",
            chosen_skill="sc-clustering",
            precondition_status="needs_preparation",
            precondition_evaluated=True,
            execution_ready=False,
            missing_preconditions=["preprocessed", "obsm.X_pca"],
        )
    )

    route = router.route("cluster my cells")

    assert route.kind is AnalysisRouteKind.EXACT_SKILL
    assert route.preflight_required is True
    assert route.missing_params == ["preprocessed", "obsm.X_pca"]


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


def test_loop_formats_failed_precondition_as_a_do_not_execute_rule() -> None:
    from omicsclaw.runtime.agent.loop import _format_analysis_route_context

    route = AnalysisRouter(
        resolver=lambda query, **_: _decision(
            query=query,
            coverage="exact_skill",
            chosen_skill="sc-clustering",
            precondition_status="needs_preparation",
            precondition_evaluated=True,
            execution_ready=False,
            missing_preconditions=["preprocessed", "obsm.X_pca"],
        )
    ).route("cluster my cells")

    context = _format_analysis_route_context(route)

    assert "preflight_required: true" in context
    assert "precondition_status: needs_preparation" in context
    assert "missing_preconditions: preprocessed; obsm.X_pca" in context
    assert "do not execute" in context.lower()


def test_route_context_probes_a_trusted_h5ad_before_declaring_execution_ready(
    tmp_path,
    monkeypatch,
) -> None:
    import anndata as ad
    import numpy as np
    import omicsclaw.runtime.agent.loop as loop

    input_path = tmp_path / "raw.h5ad"
    ad.AnnData(np.ones((3, 2))).write_h5ad(input_path)
    monkeypatch.setattr(
        loop,
        "extract_valid_input_paths",
        lambda _text: [str(input_path)],
    )

    context = loop._build_analysis_route_context(
        f"cluster my scRNA-seq cells with Leiden from {input_path}"
    )

    assert "chosen_skill: sc-clustering" in context
    assert "preflight_required: true" in context
    assert "obsm.X_pca" in context


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


def test_understanding_preflight_injects_schema_for_no_skill_file(monkeypatch) -> None:
    """ADR 0014: a no_skill/partial route with a trusted input file gets a
    deterministic inspect_data schema plus the plan/validate/interpret directive."""
    import asyncio

    import omicsclaw.runtime.agent.loop as loop
    import omicsclaw.runtime.tools.builders.agent_executors as execs

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
    file; chat / exact-skill / no-path are all silent no-ops."""
    import asyncio

    import omicsclaw.runtime.agent.loop as loop
    import omicsclaw.runtime.tools.builders.agent_executors as execs


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


def test_understanding_preflight_skips_non_h5ad_input(monkeypatch) -> None:
    """A non-.h5ad path makes inspect_data return an error string, so the
    preflight emits nothing and the base route context is left untouched."""
    import asyncio

    import omicsclaw.runtime.agent.loop as loop
    import omicsclaw.runtime.tools.builders.agent_executors as execs

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
