"""Snapshot + invariant tests for the assembled bot tool list.

ADR 0024 (Prompt Prefix Caching) retired per-turn tool-list-compression: the
production path now calls ``to_openai_tools_for_request(..., surface_only=True)``,
yielding the **Frozen tool list** — every surface-eligible tool, byte-identical
across a session's turns regardless of the query. (The old per-turn predicate
gating, and the char-budget assertions that drove it, are gone; once tools live
in a cached prefix, hit-token pricing makes compressing them a net loss.)

This file pins the frozen payload per surface (drift detection — it is what
catches a stray new always-on tool) and asserts its two invariants:
query-independence and full-surface coverage.

Usage:
    pytest tests/test_tool_list_snapshots.py
    UPDATE_SNAPSHOTS=1 pytest tests/test_tool_list_snapshots.py  # regenerate
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from omicsclaw.runtime.context.layers import ContextAssemblyRequest
from omicsclaw.runtime.tools.builders.agent import BotToolContext, build_bot_tool_specs
from omicsclaw.runtime.tools.registry import select_tool_specs

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "tool_list"
UPDATE = os.environ.get("UPDATE_SNAPSHOTS", "").strip() not in ("", "0", "false", "False")


@dataclass(frozen=True)
class Scenario:
    name: str
    surface: str = "bot"
    query: str = ""
    workspace: str = ""


# One snapshot per surface pins the frozen payload for drift detection.
SNAPSHOT_SCENARIOS: tuple[Scenario, ...] = (
    Scenario(name="baseline_bot", surface="bot"),
    Scenario(name="baseline_interactive", surface="interactive"),
)


# Diverse bot queries that, pre-ADR-0024, gated different tools (file path, PDF,
# web, plot, memory). Post-ADR-0024 they must all yield the IDENTICAL frozen
# list — that is the tool-segment half of the Stable prefix invariant.
DIVERSE_BOT_QUERIES: tuple[str, ...] = (
    "",
    "do differential expression on /tmp/sample.h5ad",
    "extract GEO accession from /tmp/paper.pdf",
    "search the web for spatial deconvolution comparison",
    "enhance the umap plot with bigger fonts",
    "请记住我喜欢 DESeq2",
    "implement and save a sc-de variant under output/",
)


def _frozen_specs(*, surface: str = "bot", query: str = "", workspace: str = ""):
    """The per-session Frozen tool list — what production actually sends."""
    ctx = BotToolContext(skill_names=("sc-de",), domain_briefing="(test)")
    all_specs = build_bot_tool_specs(ctx)
    request = ContextAssemblyRequest(surface=surface, query=query, workspace=workspace)
    return select_tool_specs(all_specs, request=request, surface_only=True)


def _serialize(scenario: Scenario) -> dict[str, Any]:
    specs = _frozen_specs(surface=scenario.surface, query=scenario.query)
    tools = [spec.to_openai_tool() for spec in specs]
    total_chars = sum(
        len(spec.description) + len(json.dumps(spec.parameters)) for spec in specs
    )
    return {
        "request": {"surface": scenario.surface, "query": scenario.query},
        "total_chars": total_chars,
        "tool_count": len(specs),
        "tool_names": sorted(spec.name for spec in specs),
        "tools": tools,
    }


def _fixture_path(scenario: Scenario) -> Path:
    return FIXTURE_DIR / f"{scenario.name}.json"


@pytest.mark.parametrize("scenario", SNAPSHOT_SCENARIOS, ids=lambda s: s.name)
def test_frozen_tool_list_matches_snapshot(scenario: Scenario) -> None:
    actual = _serialize(scenario)
    fixture = _fixture_path(scenario)
    if UPDATE or not fixture.exists():
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        fixture.write_text(
            json.dumps(actual, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        if not UPDATE:
            pytest.skip(f"Snapshot created at {fixture}; rerun without UPDATE_SNAPSHOTS")
        return
    expected = json.loads(fixture.read_text(encoding="utf-8"))
    assert actual == expected, (
        f"\nSnapshot drift: {scenario.name}\n"
        f"  fixture: {fixture}\n"
        f"  actual tool_count: {actual['tool_count']} vs expected {expected.get('tool_count')}\n"
        f"  A new always-on tool or a changed description? If intended, rerun with "
        f"UPDATE_SNAPSHOTS=1 and review the diff before committing."
    )


def test_all_scenarios_have_unique_names() -> None:
    names = [s.name for s in SNAPSHOT_SCENARIOS]
    assert len(names) == len(set(names))


def test_frozen_list_is_query_independent_for_bot() -> None:
    """ADR 0024 core invariant: the frozen bot tool payload is byte-identical
    across queries that previously gated different tools."""
    baseline = [spec.to_openai_tool() for spec in _frozen_specs(surface="bot")]
    for query in DIVERSE_BOT_QUERIES:
        payload = [spec.to_openai_tool() for spec in _frozen_specs(surface="bot", query=query)]
        assert payload == baseline, (
            f"frozen tool list changed for query {query!r}; the tool segment of "
            f"the prefix must be query-independent (else cache diagnostics will "
            f"flip to 'tool-list-changed')."
        )


def test_frozen_list_is_full_bot_surface_set() -> None:
    """The frozen list includes every bot-surface tool — no per-turn gating."""
    ctx = BotToolContext(skill_names=("sc-de",), domain_briefing="(test)")
    all_specs = build_bot_tool_specs(ctx)
    bot_eligible = {
        spec.name for spec in all_specs if (not spec.surfaces) or "bot" in spec.surfaces
    }
    frozen = {spec.name for spec in _frozen_specs(surface="bot")}
    assert frozen == bot_eligible, (
        f"frozen list must equal the full bot-surface set\n"
        f"  missing: {bot_eligible - frozen}\n"
        f"  unexpected: {frozen - bot_eligible}"
    )


# Tools the Read stage MUST withhold (ADR 0020 safety boundary): heavyweight
# compute / skill creation, network-download, file/dir mutation, and media gen.
# This is the security boundary as an executable contract — see the test below.
_READ_WITHHELD_TOOLS: frozenset[str] = frozenset(
    {
        # heavyweight compute / skill creation
        "omicsclaw", "autonomous_analysis_execute",
        "create_omics_skill", "replot_skill",
        # NOTE: parse_literature is NO LONGER withheld from Read (Phase 3.3b): it is
        # allowed in Read with its download permission-gated (approval_mode=ASK).
        # file / directory mutation
        "file_edit", "file_write", "save_file", "write_file",
        "move_file", "remove_file", "make_directory",
        # media generation
        "generate_audio",
    }
)


def test_read_stage_withholds_exactly_the_unsafe_tools_from_real_bot_set() -> None:
    """Security boundary as an executable contract (Bench Phase 2, ADR 0020).

    Against the REAL assembled bot tool set, ``stage='read'`` must withhold
    EXACTLY the heavyweight / file-writing / network-download / media / skill-
    creation tools — nothing more, nothing less. This catches the class of bug a
    full review caught manually: an unsafe tool slipping into the Read allow-list
    (it would vanish from the withheld set), or a new always-on write tool added
    to the registry but never classified read-safe (it would appear here).
    """
    ctx = BotToolContext(skill_names=("sc-de",), domain_briefing="(test)")
    all_specs = build_bot_tool_specs(ctx)
    req = ContextAssemblyRequest(surface="bot")

    frozen = {s.name for s in select_tool_specs(all_specs, request=req, surface_only=True)}
    read = {
        s.name
        for s in select_tool_specs(all_specs, request=req, surface_only=True, stage="read")
    }

    assert frozen - read == _READ_WITHHELD_TOOLS, (
        "Read-stage tool boundary drifted:\n"
        f"  unsafe tool LEAKED into Read: {_READ_WITHHELD_TOOLS - (frozen - read)}\n"
        f"  read-safe tool wrongly WITHHELD: {(frozen - read) - _READ_WITHHELD_TOOLS}"
    )
    # Read only removes tools, never adds — a strict subset of the frozen list.
    assert read < frozen
    # Empty stage is byte-equal to no stage (cache-stable / legacy invariant).
    empty = {
        s.name
        for s in select_tool_specs(all_specs, request=req, surface_only=True, stage="")
    }
    assert empty == frozen
