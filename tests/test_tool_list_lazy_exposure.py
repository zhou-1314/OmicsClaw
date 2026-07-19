"""Phase 1 (T1.7) RED tests for the per-tool predicate mapping.

Verifies that each lazy-load tool is exposed iff its expected predicate
fires for the given request. The always-on tools (predicate=None) must appear
in every scenario regardless of query.

Coverage map (mirrors plan section "Wire 33 lazy-load tools to predicates"):

| Predicate                          | Tools                                     |
|------------------------------------|-------------------------------------------|
| (none — always-on)                 | omicsclaw, candidate_plan_execute,        |
|                                    | resolve_capability,                       |
|                                    | consult_knowledge, inspect_data,          |
|                                    | list_directory, glob_files, file_read,    |
|                                    | read_knowhow, ask_user, kg_search,        |
|                                    | kg_get_page, kg_list_pages,               |
|                                    | kg_graph_neighbors, kg_status,            |
|                                    | kg_recent_log, kg_communities             |
| anndata_or_file_path_in_query      | save_file, write_file, inspect_file,      |
|                                    | make_directory, move_file, remove_file,   |
|                                    | get_file_size, file_write, file_edit,     |
|                                    | grep_files, tool_search, create_json_file,|
|                                    | create_csv_file                           |
| pdf_or_paper_intent                | parse_literature, fetch_geo_metadata      |
| memory_in_use                      | remember, recall, forget                  |
| implementation_intent              | autonomous_analysis_execute               |
| workspace_active                   | todo_write, task_create, task_get,        |
|                                    | task_list, task_update                    |
| non_trivial_no_capability          | list_skills_in_domain                     |
| plot_intent                        | replot_skill                              |
| web_or_url_intent                  | web_method_search, web_fetch, web_search, |
|                                    | download_file                             |
| skill_creation_intent              | create_omics_skill                        |
| (custom: "mcp" keyword)            | mcp_list                                  |
| (custom: audio keywords)           | generate_audio                            |
"""

from __future__ import annotations

import pytest

from omicsclaw.runtime.tools.builders.agent import BotToolContext, build_bot_tool_specs
from omicsclaw.runtime.context.layers import ContextAssemblyRequest
from omicsclaw.runtime.tools.registry import select_tool_specs


def _all_specs():
    ctx = BotToolContext(skill_names=("sc-de",), domain_briefing="(test)")
    return build_bot_tool_specs(ctx)


def _selected_names(query: str = "", **kwargs) -> set[str]:
    req = ContextAssemblyRequest(surface="bot", query=query, **kwargs)
    return {s.name for s in select_tool_specs(_all_specs(), request=req)}


ALWAYS_ON = (
    "omicsclaw",
    "candidate_plan_execute",
    "resolve_capability",
    "consult_knowledge",
    "inspect_data",
    "list_directory",
    "glob_files",
    "file_read",
    "read_knowhow",
    # ``ask_user`` (interactive choice tool) is always-on: predicate=None,
    # so it is exposed every turn regardless of query.
    "ask_user",
    # OmicsClaw-KG read tools (Bench Phase 3.1, ADR 0019) are predicate-less,
    # hence always-on like ``consult_knowledge`` — exposed every turn (and
    # Read-stage allow-listed). They soft-fail when the optional package is absent.
    "kg_search",
    "kg_get_page",
    "kg_list_pages",
    "kg_graph_neighbors",
    "kg_status",
    "kg_recent_log",
    "kg_communities",
    # kg_ingest (Bench Phase 3.3c) is also predicate-less always-on — the KG
    # citation-substrate writer, AUTO-approved (ADR 0019).
    "kg_ingest",
    # kg_build_packet / kg_record_result (hypothesis handoff, ADR 0021) are likewise
    # predicate-less always-on; they are write tools, so still Read-stage withheld.
    "kg_build_packet",
    "kg_record_result",
)


# --- Always-on tools never get filtered out ----------------------------------


@pytest.mark.parametrize("tool", ALWAYS_ON)
def test_always_on_tool_visible_with_empty_query(tool: str) -> None:
    assert tool in _selected_names(query="")


@pytest.mark.parametrize("tool", ALWAYS_ON)
def test_always_on_tool_visible_with_unrelated_query(tool: str) -> None:
    assert tool in _selected_names(query="hello there")


# --- Lazy-load mapping: file/path triggers -----------------------------------


FILE_PATH_TOOLS = (
    "save_file",
    "write_file",
    "inspect_file",
    "make_directory",
    "move_file",
    "remove_file",
    "get_file_size",
    "file_write",
    "file_edit",
    "grep_files",
    "tool_search",
)


@pytest.mark.parametrize("tool", FILE_PATH_TOOLS)
def test_file_path_tool_appears_when_h5ad_in_query(tool: str) -> None:
    assert tool in _selected_names(query="run sc-de on /tmp/x.h5ad")


@pytest.mark.parametrize("tool", FILE_PATH_TOOLS)
def test_file_path_tool_hidden_when_no_path(tool: str) -> None:
    assert tool not in _selected_names(query="explain UMAP")


# --- Lazy-load mapping: PDF / paper ------------------------------------------


@pytest.mark.parametrize("tool", ("parse_literature", "fetch_geo_metadata"))
def test_pdf_paper_tool_appears_on_pdf_query(tool: str) -> None:
    assert tool in _selected_names(query="extract from /tmp/paper.pdf")


@pytest.mark.parametrize("tool", ("parse_literature", "fetch_geo_metadata"))
def test_pdf_paper_tool_hidden_on_unrelated_query(tool: str) -> None:
    assert tool not in _selected_names(query="run sc-de")


# --- Lazy-load mapping: memory -----------------------------------------------


@pytest.mark.parametrize("tool", ("remember", "recall", "forget"))
def test_memory_tool_appears_on_memory_query(tool: str) -> None:
    assert tool in _selected_names(query="please remember I prefer DESeq2")


@pytest.mark.parametrize("tool", ("remember", "recall", "forget"))
def test_memory_tool_hidden_on_unrelated_query(tool: str) -> None:
    assert tool not in _selected_names(query="run sc-de")


# Regression: desktop-app users state preferences declaratively
# ("以后请用中文回答") without saying 记住/remember. Before, none of the
# 3 memory tools were exposed, so LLM-initiated persistence silently
# failed. Now ``remember`` co-gates on ``preference_statement_intent``,
# but ``recall`` / ``forget`` stay strict — those are explicit user
# actions, not LLM-initiated.

PREFERENCE_STATEMENT_QUERIES = (
    "以后请用中文回答",
    "from now on use DESeq2 for DE",
    "总是用 harmony 做整合",
    "I prefer to skip the doublet step",
    "默认用 leiden 聚类",
)


@pytest.mark.parametrize("query", PREFERENCE_STATEMENT_QUERIES)
def test_remember_appears_on_preference_statement(query: str) -> None:
    assert "remember" in _selected_names(query=query), (
        f"remember tool must be exposed on declarative preference "
        f"statement {query!r} so the LLM can persist it without the "
        f"user uttering 记住/remember explicitly."
    )


@pytest.mark.parametrize("query", PREFERENCE_STATEMENT_QUERIES)
def test_recall_forget_hidden_on_preference_statement(query: str) -> None:
    selected = _selected_names(query=query)
    assert "recall" not in selected, (
        f"recall is an explicit-user-action tool; should NOT be exposed "
        f"merely because the user stated a preference: {query!r}"
    )
    assert "forget" not in selected, (
        f"forget is an explicit-user-action tool; should NOT be exposed "
        f"merely because the user stated a preference: {query!r}"
    )


# --- Lazy-load mapping: implementation intent --------------------------------


@pytest.mark.parametrize(
    "tool", ("autonomous_analysis_execute",)
)
def test_custom_analysis_tools_appear_on_implement_query(tool: str) -> None:
    assert tool in _selected_names(query="implement a new sc-de variant")


@pytest.mark.parametrize(
    "tool", ("autonomous_analysis_execute",)
)
def test_custom_analysis_tools_hidden_on_plain_query(tool: str) -> None:
    assert tool not in _selected_names(query="explain UMAP")


# --- Lazy-load mapping: workspace_active -------------------------------------


WORKSPACE_TOOLS = ("todo_write", "task_create", "task_get", "task_list", "task_update")


@pytest.mark.parametrize("tool", WORKSPACE_TOOLS)
def test_workspace_tool_appears_when_workspace_set(tool: str) -> None:
    assert tool in _selected_names(query="show plan", workspace="/tmp/run42")


@pytest.mark.parametrize("tool", WORKSPACE_TOOLS)
def test_workspace_tool_hidden_when_no_workspace(tool: str) -> None:
    assert tool not in _selected_names(query="show plan")


# --- Lazy-load mapping: non_trivial_no_capability ----------------------------


def test_list_skills_in_domain_appears_on_substantive_no_capability() -> None:
    assert "list_skills_in_domain" in _selected_names(
        query="do differential expression on single-cell data",
        capability_context="",
    )


def test_list_skills_in_domain_hidden_when_capability_present() -> None:
    assert "list_skills_in_domain" not in _selected_names(
        query="do differential expression on single-cell data",
        capability_context="## Deterministic Capability Assessment\n- coverage: exact_skill",
    )


# --- Lazy-load mapping: plot_intent ------------------------------------------


def test_replot_skill_appears_on_plot_query() -> None:
    assert "replot_skill" in _selected_names(query="enhance the violin plot")


def test_replot_skill_hidden_on_non_plot_query() -> None:
    assert "replot_skill" not in _selected_names(query="run sc-de")


# --- Lazy-load mapping: web_or_url_intent ------------------------------------


WEB_TOOLS = ("web_method_search", "web_fetch", "web_search")


@pytest.mark.parametrize("tool", WEB_TOOLS)
def test_web_tool_appears_on_url_query(tool: str) -> None:
    assert tool in _selected_names(query="search the web for spatial deconv methods")


@pytest.mark.parametrize("tool", WEB_TOOLS)
def test_web_tool_hidden_on_unrelated_query(tool: str) -> None:
    assert tool not in _selected_names(query="run sc-de on /tmp/x.h5ad")


# --- Lazy-load mapping: skill_creation_intent --------------------------------


def test_create_omics_skill_appears_on_create_query() -> None:
    assert "create_omics_skill" in _selected_names(
        query="create a new skill for batch correction"
    )


def test_create_omics_skill_hidden_on_run_query() -> None:
    assert "create_omics_skill" not in _selected_names(query="run sc-de")


# --- Lazy-load mapping: niche tools ------------------------------------------


def test_generate_audio_hidden_by_default() -> None:
    """``generate_audio`` is niche — gate it on explicit audio keywords."""
    assert "generate_audio" not in _selected_names(query="run sc-de")
    assert "generate_audio" not in _selected_names(query="explain UMAP")


def test_generate_audio_appears_on_audio_query() -> None:
    assert "generate_audio" in _selected_names(query="generate a podcast summary")
    assert "generate_audio" in _selected_names(query="generate audio for this report")


# Phase 2: ``mcp_list``, ``download_file``, ``create_json_file``,
# ``create_csv_file`` were deleted as confirmed dead code (0 audit-log
# calls, no production callers, functional overlap with ``web_fetch`` /
# ``write_file``).


# --- Aggregate count assertions ---------------------------------------------


def test_baseline_query_only_shows_always_on_set() -> None:
    """Baseline (empty query, no workspace, no capability) should show
    exactly the always-on set and nothing else."""
    selected = _selected_names(query="")
    extras = selected - set(ALWAYS_ON)
    assert extras == set(), f"unexpected non-always-on tools fired: {extras}"
    missing = set(ALWAYS_ON) - selected
    assert missing == set(), f"always-on tools missing: {missing}"


def test_realistic_scde_turn_count_well_under_full_set() -> None:
    """Realistic sc-de query exposes always-on + file-path tools, but
    keeps memory / pdf / web / plot tools hidden."""
    selected = _selected_names(query="run sc-de on /tmp/x.h5ad")
    assert len(selected) <= 33, (
        f"realistic sc-de turn exposed {len(selected)} tools; expected <= 33 "
        f"(always-on 17 + file-path ~13 + maybe non_trivial fallback): {sorted(selected)}"
    )
    assert len(selected) < 47, "ALL 47 tools shown — predicate gating not applied"
    assert "remember" not in selected
    assert "parse_literature" not in selected
    assert "web_search" not in selected
    assert "replot_skill" not in selected
