"""Tests for Stage 3 auto-routing helpers in ``omicsclaw.runtime.agent.state``.

Covers the two output-shaping functions that were added to make the ``auto``
path interact with the LLM safely:

* ``_format_auto_disambiguation`` — returns a candidate list when the top two
  scores are within ``_AUTO_DISAMBIGUATE_GAP``.
* ``_format_auto_route_banner`` — returns a short ``📍 Auto-routed …`` banner
  that gets prepended to successful tool output.

We do not exercise the full ``execute_omicsclaw`` subprocess pipeline here —
that would require a live subprocess and demo data. Instead we build the
``CapabilityDecision`` shape directly and assert on the formatted text.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omicsclaw.skill.capability_resolver import (
    CapabilityCandidate,
    CapabilityDecision,
)


def _decision(
    chosen: str,
    confidence: float,
    candidates: list[tuple[str, float, list[str]]],
    coverage: str = "exact_skill",
) -> CapabilityDecision:
    """Build a minimal CapabilityDecision matching the resolver's shape."""
    return CapabilityDecision(
        query="dummy",
        domain="",
        coverage=coverage,
        confidence=confidence,
        chosen_skill=chosen,
        skill_candidates=[
            CapabilityCandidate(
                skill=name, domain="", score=score, reasons=list(reasons)
            )
            for name, score, reasons in candidates
        ],
    )


# ---------------------------------------------------------------------------
# _format_auto_route_banner
# ---------------------------------------------------------------------------


def test_auto_route_banner_shows_chosen_and_confidence():
    from omicsclaw.runtime.agent.state import _format_auto_route_banner

    dec = _decision(
        chosen="sc-de",
        confidence=0.73,
        candidates=[
            ("sc-de", 10.5, ["trigger keyword match: differential expression"]),
            ("sc-markers", 7.1, ["trigger keyword match: marker genes"]),
        ],
    )
    banner = _format_auto_route_banner(dec)

    assert banner.startswith("📍 Auto-routed to `sc-de`")
    assert "confidence 0.73" in banner
    # A close alternative should be disclosed so the LLM can pivot if wrong.
    assert "sc-markers" in banner
    # The banner terminates with a markdown divider so the real tool output
    # stays visually separated.
    assert banner.rstrip().endswith("---")


def test_auto_route_banner_without_alternatives():
    from omicsclaw.runtime.agent.state import _format_auto_route_banner

    dec = _decision(
        chosen="spatial-preprocess",
        confidence=0.95,
        candidates=[("spatial-preprocess", 13.0, ["alias hit"])],
    )
    banner = _format_auto_route_banner(dec)

    assert "spatial-preprocess" in banner
    assert "confidence 0.95" in banner
    # No alternatives in the candidate list → no "Close alternatives" phrase.
    assert "Close alternatives" not in banner


@pytest.mark.asyncio
async def test_auto_route_refuses_execution_when_preconditions_are_not_ready(
    tmp_path,
    monkeypatch,
):
    import omicsclaw.runtime.agent.state  # noqa: F401 - bootstrap executor exports
    from omicsclaw.runtime.tools.builders import agent_executors
    import omicsclaw.skill.capability_resolver as resolver_module

    input_path = tmp_path / "raw.h5ad"
    input_path.touch()
    decision = _decision(
        chosen="sc-clustering",
        confidence=0.95,
        candidates=[("sc-clustering", 13.0, ["clustering intent"])],
    )
    decision.precondition_status = "needs_preparation"
    decision.precondition_evaluated = True
    decision.execution_ready = False
    decision.missing_preconditions = ["preprocessed", "obsm.X_pca"]
    decision.precondition_reasons = ["required obsm key 'X_pca' is not available"]
    decision.recommended_preparation = ["sc-preprocessing"]

    monkeypatch.setattr(
        agent_executors,
        "validate_input_path",
        lambda *_args, **_kwargs: input_path,
    )
    monkeypatch.setattr(agent_executors, "audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        resolver_module,
        "resolve_capability",
        lambda *_args, **_kwargs: decision,
    )

    async def fail_if_executed(**_kwargs):
        raise AssertionError("skill runner must not execute behind a failed precondition gate")

    monkeypatch.setattr(
        agent_executors,
        "_run_skill_via_shared_runner",
        fail_if_executed,
    )

    result = await agent_executors.execute_omicsclaw(
        {
            "skill": "auto",
            "mode": "path",
            "query": "cluster my scRNA-seq cells",
            "file_path": str(input_path),
        }
    )

    assert "not execution-ready" in result
    assert "preprocessed" in result
    assert "obsm.X_pca" in result
    assert "sc-preprocessing" in result


@pytest.mark.asyncio
async def test_auto_route_never_executes_an_unreadable_h5ad(tmp_path, monkeypatch):
    import omicsclaw.runtime.agent.state  # noqa: F401 - bootstrap executor exports
    from omicsclaw.runtime.tools.builders import agent_executors

    input_path = tmp_path / "corrupt.h5ad"
    input_path.write_bytes(b"not an hdf5 file")

    monkeypatch.setattr(
        agent_executors,
        "validate_input_path",
        lambda *_args, **_kwargs: input_path,
    )
    monkeypatch.setattr(agent_executors, "audit", lambda *_args, **_kwargs: None)

    async def fail_if_executed(**_kwargs):
        raise AssertionError("corrupt input must not reach the shared runner")

    monkeypatch.setattr(
        agent_executors,
        "_run_skill_via_shared_runner",
        fail_if_executed,
    )

    result = await agent_executors.execute_omicsclaw(
        {
            "skill": "auto",
            "mode": "path",
            "query": "cluster my scRNA-seq cells with Leiden",
            "file_path": str(input_path),
        }
    )

    assert "not execution-ready" in result
    assert "inspection" in result


@pytest.mark.asyncio
async def test_file_mode_profiles_the_current_sessions_upload(tmp_path, monkeypatch):
    import omicsclaw.runtime.agent.state  # noqa: F401 - bootstrap executor exports
    from omicsclaw.runtime.tools.builders import agent_executors
    from omicsclaw.skill import capability_resolver as resolver_module
    from omicsclaw.skill import preconditions as preconditions_module
    from omicsclaw.skill.preconditions import InputProfile

    other_path = tmp_path / "other-session.h5ad"
    current_path = tmp_path / "current-session.h5ad"
    other_path.touch()
    current_path.touch()
    monkeypatch.setattr(
        agent_executors,
        "received_files",
        {
            "other": {"path": str(other_path)},
            "current": {"path": str(current_path)},
        },
    )
    monkeypatch.setattr(agent_executors, "audit", lambda *_args, **_kwargs: None)

    captured: dict[str, object] = {}

    def fake_probe(path):
        captured["probed_path"] = Path(path)
        return InputProfile(
            file_type="h5ad",
            modality="scrna",
            preprocessed=False,
            obsm=set(),
        )

    def fake_resolve(*_args, **kwargs):
        captured["resolver_file_path"] = kwargs["file_path"]
        captured["profile"] = kwargs["input_profile"]
        decision = _decision(
            chosen="sc-clustering",
            confidence=0.95,
            candidates=[("sc-clustering", 13.0, ["clustering intent"])],
        )
        decision.precondition_status = "needs_preparation"
        decision.precondition_evaluated = True
        decision.execution_ready = False
        decision.missing_preconditions = ["preprocessed", "obsm.X_pca"]
        return decision

    monkeypatch.setattr(preconditions_module, "probe_input_profile", fake_probe)
    monkeypatch.setattr(resolver_module, "resolve_capability", fake_resolve)

    async def fail_if_executed(**_kwargs):
        raise AssertionError("failed preconditions must not reach the shared runner")

    monkeypatch.setattr(
        agent_executors,
        "_run_skill_via_shared_runner",
        fail_if_executed,
    )

    result = await agent_executors.execute_omicsclaw(
        {
            "skill": "auto",
            "mode": "file",
            "query": "cluster my scRNA-seq cells",
        },
        session_id="current",
    )

    assert "not execution-ready" in result
    assert captured["probed_path"] == current_path
    assert captured["resolver_file_path"] == str(current_path)


@pytest.mark.asyncio
async def test_file_mode_fails_closed_when_the_session_upload_disappears(
    tmp_path,
    monkeypatch,
):
    import omicsclaw.runtime.agent.state  # noqa: F401 - bootstrap executor exports
    from omicsclaw.runtime.tools.builders import agent_executors

    missing_path = tmp_path / "disappeared.h5ad"
    monkeypatch.setattr(
        agent_executors,
        "received_files",
        {"current": {"path": str(missing_path)}},
    )

    async def fail_if_executed(**_kwargs):
        raise AssertionError("a missing session upload must not reach the shared runner")

    monkeypatch.setattr(
        agent_executors,
        "_run_skill_via_shared_runner",
        fail_if_executed,
    )

    result = await agent_executors.execute_omicsclaw(
        {
            "skill": "auto",
            "mode": "file",
            "query": "cluster my scRNA-seq cells with Leiden",
        },
        session_id="current",
    )

    assert "not execution-ready" in result
    assert "inspection" in result


@pytest.mark.asyncio
async def test_resolve_tool_prefers_a_trusted_file_probe_to_caller_claims(
    tmp_path,
    monkeypatch,
):
    import omicsclaw.runtime.agent.state  # noqa: F401 - bootstrap executor exports
    from omicsclaw.runtime.tools.builders import agent_executors
    from omicsclaw.skill import capability_resolver as resolver_module
    from omicsclaw.skill import preconditions as preconditions_module
    from omicsclaw.skill.preconditions import InputProfile

    input_path = tmp_path / "trusted.h5ad"
    input_path.touch()
    observed = InputProfile(
        file_type="h5ad",
        modality="scrna",
        preprocessed=False,
        obsm=set(),
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        agent_executors,
        "validate_input_path",
        lambda *_args, **_kwargs: input_path,
    )
    monkeypatch.setattr(
        preconditions_module,
        "probe_input_profile",
        lambda path: observed,
    )

    def fake_resolve(*_args, **kwargs):
        captured["profile"] = kwargs["input_profile"]
        return _decision(
            chosen="sc-clustering",
            confidence=0.95,
            candidates=[("sc-clustering", 13.0, ["clustering intent"])],
        )

    monkeypatch.setattr(resolver_module, "resolve_capability", fake_resolve)

    await agent_executors.execute_resolve_capability(
        {
            "query": "cluster my scRNA-seq cells",
            "file_path": str(input_path),
            "input_profile": {
                "file_type": "h5ad",
                "modality": "scrna",
                "preprocessed": True,
                "obsm": ["X_pca"],
            },
        }
    )

    assert captured["profile"] is observed


# ---------------------------------------------------------------------------
# _format_auto_disambiguation
# ---------------------------------------------------------------------------


def test_disambiguation_lists_top_three_candidates():
    from omicsclaw.runtime.agent.state import _format_auto_disambiguation

    dec = _decision(
        chosen="bulkrna-de",
        confidence=0.62,
        candidates=[
            ("bulkrna-de", 8.0, ["description token overlap: differential"]),
            ("spatial-de", 7.3, ["trigger keyword match: differential expression"]),
            ("sc-de", 7.0, ["trigger keyword match: de"]),
        ],
    )
    block = _format_auto_disambiguation(dec, query_text="find differentially expressed genes")

    # Human-readable header prevents the LLM from silently re-running the wrong skill.
    assert "Auto-routing found multiple close candidates" in block
    # All three candidate aliases must be surfaced for LLM selection.
    for alias in ("bulkrna-de", "spatial-de", "sc-de"):
        assert f"`{alias}`" in block
    # Quantitative scores give the LLM a concrete tie-breaker signal.
    assert "8.00" in block and "7.30" in block
    # Explicit instruction to re-invoke with a specific skill.
    assert "re-invoke" in block.lower()
    assert "skill=" in block


def test_disambiguation_handles_empty_candidate_list():
    from omicsclaw.runtime.agent.state import _format_auto_disambiguation

    dec = _decision(chosen="", confidence=0.0, candidates=[])
    block = _format_auto_disambiguation(dec, query_text="nothing matches")

    # No candidates → the helper should produce no output rather than a
    # malformed "Top candidates" block. This protects callers that iterate
    # output lines.
    assert block == ""


def test_disambiguation_truncates_long_description():
    from omicsclaw.runtime.agent.state import _format_auto_disambiguation
    import omicsclaw.runtime.agent.state as bc

    # Monkey-patch the registry lookup so this test doesn't depend on the
    # live SKILL.md content (which changes as the repo grows).
    class _FakeRegistry:
        skills = {
            "fake-skill": {
                "description": "x" * 300,  # deliberately long
            },
            "other-skill": {"description": "short"},
        }

    original = bc._skill_registry
    bc._skill_registry = lambda: _FakeRegistry()  # type: ignore[assignment]
    try:
        dec = _decision(
            chosen="fake-skill",
            confidence=0.4,
            candidates=[
                ("fake-skill", 5.0, []),
                ("other-skill", 4.2, []),
            ],
        )
        block = _format_auto_disambiguation(dec, query_text="q")
    finally:
        bc._skill_registry = original  # type: ignore[assignment]

    # Truncation marker appears; the raw 300-char payload must not be inlined.
    assert "…" in block
    assert "x" * 300 not in block


# ---------------------------------------------------------------------------
# _AUTO_DISAMBIGUATE_GAP
# ---------------------------------------------------------------------------


def test_disambiguate_gap_is_calibrated():
    """Sanity-check the threshold: too low and we never disambiguate, too
    high and we refuse to execute on most queries. The scorer typically
    gives 0.85 per trigger-keyword hit and ~10 for alias hits, so 2.0 is a
    reasonable dead-zone between "one extra keyword" and "clearly better"."""
    from omicsclaw.runtime.agent.state import _AUTO_DISAMBIGUATE_GAP

    assert 1.0 <= _AUTO_DISAMBIGUATE_GAP <= 4.0
