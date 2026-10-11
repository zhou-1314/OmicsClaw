"""The ``search_skills`` tool."""

from __future__ import annotations

import asyncio
import json

import pytest

from omicsclaw.tools import ApprovalMode, RiskLevel, Tool, ToolRegistry
from omicsclaw.tools.function_tool import ToolArgumentError
from omicsclaw.schema import ToolCall

from omicsclaw.skills import (
    SEARCH_SKILL_TOOL_NAME,
    SkillIndex,
    load_skills,
    search_skill_tool,
)

from .test_loader import write_skill


def run(tool, **arguments) -> str:
    """Execute *tool* the way the registry does, with a raw JSON payload."""
    return asyncio.run(tool.execute(json.dumps(arguments)))


def build(tmp_path):
    """A three-skill index with one trigger keyword worth hiding behind."""
    write_skill(
        tmp_path / "spatial" / "spatial-de",
        "spatial-de",
        "spatial variable features",
        trigger="DE, differentially expressed",
    )
    write_skill(tmp_path / "spatial" / "spatial-domains", "spatial-domains", "tissue domains")
    write_skill(tmp_path / "genomics" / "genomics-qc", "genomics-qc", "alignment metrics")
    return load_skills(tmp_path)


def test_the_tool_is_called_search_skills():
    tool = search_skill_tool(SkillIndex())

    assert tool.name == SEARCH_SKILL_TOOL_NAME == "search_skills"
    assert tool.definition().name == "search_skills"


def test_the_tool_satisfies_the_tool_protocol():
    assert isinstance(search_skill_tool(SkillIndex()), Tool)


def test_the_definition_asks_for_one_required_string():
    schema = search_skill_tool(SkillIndex()).definition().input_schema

    assert schema["required"] == ["query"]
    assert schema["properties"]["query"]["type"] == "string"
    assert schema["properties"]["domain"]["type"] == "string"
    assert schema["additionalProperties"] is False


def test_the_definition_says_nothing_about_policy():
    """Policy is what this machine permits; a model must not read it."""
    rendered = json.dumps(
        {
            "description": search_skill_tool(SkillIndex()).definition().description,
            "schema": search_skill_tool(SkillIndex()).definition().input_schema,
        }
    )

    for leak in ("risk_level", "approval_mode", "read_only", "concurrency_safe"):
        assert leak not in rendered


def test_searching_is_automatic_and_read_only():
    """Left to the guarded defaults, every search would need approval."""
    policy = search_skill_tool(SkillIndex()).policy

    assert policy.approval_mode is ApprovalMode.AUTO
    assert policy.risk_level is RiskLevel.LOW
    assert policy.read_only
    assert policy.concurrency_safe


def test_a_hit_names_its_domain_and_says_how_to_load_it(tmp_path):
    tool = search_skill_tool(build(tmp_path))

    output = run(tool, query="domains")

    assert "- spatial-domains (spatial): tissue domains" in output
    assert "load with use_skill" in output


def test_a_trigger_keyword_finds_a_name_that_does_not_contain_it(tmp_path):
    """The acceptance case from the plan: `spatial-de` via `DE`, not via its name."""
    tool = search_skill_tool(build(tmp_path))

    output = run(tool, query="DE")

    assert "spatial-de" in output


def test_an_empty_query_lists_the_whole_catalogue(tmp_path):
    """`/skills` and the model's tool share one code path, blank meaning all."""
    tool = search_skill_tool(build(tmp_path))

    output = run(tool, query="")

    for name in ("spatial-de", "spatial-domains", "genomics-qc"):
        assert name in output


def test_a_domain_filters_the_hits(tmp_path):
    tool = search_skill_tool(build(tmp_path))

    output = run(tool, query="", domain="genomics")

    assert "genomics-qc" in output
    assert "spatial-de" not in output


def test_a_domain_that_names_nothing_is_a_miss(tmp_path):
    tool = search_skill_tool(build(tmp_path))

    output = run(tool, query="spatial", domain="proteomics")

    assert "No skills matched" in output
    assert "proteomics" in output


def test_a_miss_offers_close_names_and_better_terms(tmp_path):
    tool = search_skill_tool(build(tmp_path))

    output = run(tool, query="spatial-dee")

    assert "No skills matched" in output
    assert "Did you mean: spatial-de" in output
    assert "domain term" in output


def test_an_empty_index_says_so_rather_than_matching_everything():
    output = run(search_skill_tool(SkillIndex()), query="")

    assert output == "The skill index is empty."


def test_a_missing_argument_is_refused():
    tool = search_skill_tool(SkillIndex())

    with pytest.raises(ToolArgumentError):
        run(tool)


def test_a_wrongly_typed_argument_is_refused():
    tool = search_skill_tool(SkillIndex())

    with pytest.raises(ToolArgumentError):
        run(tool, query=7)


def test_the_registry_runs_a_search_without_an_approval_channel(tmp_path):
    """Approval fails closed everywhere, so AUTO is what makes this work."""
    registry = ToolRegistry()
    registry.register(search_skill_tool(build(tmp_path)))

    result = asyncio.run(
        registry.execute(
            ToolCall(id="1", name="search_skills", arguments='{"query": "domains"}')
        )
    )

    assert not result.is_error
    assert "spatial-domains" in result.output
