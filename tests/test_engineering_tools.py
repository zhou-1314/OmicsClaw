import asyncio
import json
from pathlib import Path

from omicsclaw.runtime.tools.builders.agent import BotToolContext, build_bot_tool_specs
from omicsclaw.runtime.tools.builders.engineering import (
    build_engineering_tool_executors,
    build_engineering_tool_specs,
)


def _build_executors(tmp_path: Path):
    repo_root = tmp_path / "repo"
    (repo_root / "data").mkdir(parents=True, exist_ok=True)
    (repo_root / "examples").mkdir(parents=True, exist_ok=True)
    (repo_root / "output").mkdir(parents=True, exist_ok=True)
    return build_engineering_tool_executors(
        omicsclaw_dir=repo_root,
        state_root=tmp_path / "state",
        tool_specs_supplier=build_engineering_tool_specs,
    )


def _build_executors_with_data(tmp_path: Path):
    """Build executors over a repo with three real data files under data/."""
    repo_root = tmp_path / "repo"
    (repo_root / "data").mkdir(parents=True, exist_ok=True)
    (repo_root / "examples").mkdir(parents=True, exist_ok=True)
    (repo_root / "output").mkdir(parents=True, exist_ok=True)
    (repo_root / "data" / "pbmc3k_raw.h5ad").write_bytes(b"")
    (repo_root / "data" / "pbmc3k_processed.h5ad").write_bytes(b"")
    (repo_root / "data" / "slideseqv2_mouse_hippocampus.h5ad").write_bytes(b"")
    executors = build_engineering_tool_executors(
        omicsclaw_dir=repo_root,
        state_root=tmp_path / "state",
        tool_specs_supplier=build_engineering_tool_specs,
    )
    return repo_root, executors


def test_glob_files_data_star_star_matches_files_not_just_dir(tmp_path: Path):
    """Regression for the data/** -> 0 matches bug.

    pathlib.Path.glob("data/**") on Python 3.11 returns only the
    ``data`` directory itself (not its files), and the executor then
    drops directories via ``is_dir()`` — yielding an empty match list
    even though the directory contains real files. The user-visible
    bug: ``data/*`` finds the files but ``data/**`` claims the
    directory is empty.
    """
    repo_root, executors = _build_executors_with_data(tmp_path)
    payload = asyncio.run(
        executors["glob_files"](
            {"pattern": "data/**", "root": str(repo_root)},
            surface="interactive",
        )
    )
    result = json.loads(payload)
    assert result["count"] == 3, (
        f"data/** should find 3 .h5ad files but returned {result['count']}: "
        f"{result['matches']}"
    )
    names = sorted(Path(m).name for m in result["matches"])
    assert names == [
        "pbmc3k_processed.h5ad",
        "pbmc3k_raw.h5ad",
        "slideseqv2_mouse_hippocampus.h5ad",
    ]


def test_glob_files_bare_double_star_matches_files_recursively(tmp_path: Path):
    """A pattern that is exactly ``**`` should match every file under
    the root recursively, not just enumerate directories."""
    repo_root, executors = _build_executors_with_data(tmp_path)
    payload = asyncio.run(
        executors["glob_files"](
            {"pattern": "**", "root": str(repo_root)},
            surface="interactive",
        )
    )
    result = json.loads(payload)
    assert result["count"] >= 3
    names = {Path(m).name for m in result["matches"]}
    assert "pbmc3k_raw.h5ad" in names


def test_glob_files_data_star_unchanged_by_normalization(tmp_path: Path):
    """The normalization for trailing ``**`` must not affect other
    patterns. ``data/*`` (already correct) must keep returning the
    same 3 files."""
    repo_root, executors = _build_executors_with_data(tmp_path)
    payload = asyncio.run(
        executors["glob_files"](
            {"pattern": "data/*", "root": str(repo_root)},
            surface="interactive",
        )
    )
    result = json.loads(payload)
    assert result["count"] == 3


def test_glob_files_data_star_star_star_unchanged_by_normalization(tmp_path: Path):
    """``data/**/*`` already enumerates files correctly under pathlib;
    the normalization must not corrupt patterns that already end with
    a non-``**`` segment."""
    repo_root, executors = _build_executors_with_data(tmp_path)
    payload = asyncio.run(
        executors["glob_files"](
            {"pattern": "data/**/*", "root": str(repo_root)},
            surface="interactive",
        )
    )
    result = json.loads(payload)
    assert result["count"] == 3


def test_build_bot_tool_specs_includes_curated_engineering_tools():
    specs = build_bot_tool_specs(
        BotToolContext(
            skill_names=("auto",),
            skill_desc_text="auto (auto route)",
        )
    )
    names = {spec.name for spec in specs}

    assert "tool_search" in names
    assert "file_read" in names
    assert "file_write" in names
    assert "file_edit" in names
    assert "glob_files" in names
    assert "grep_files" in names
    assert "task_create" in names
    assert "task_list" in names
    assert "todo_write" in names
    assert "web_fetch" in names
    assert "web_search" in names
    # ``mcp_list`` was removed in the tool-list-compression refactor as
    # confirmed dead code (0 audit-log calls, no production refs).
    # ``mcp_list_servers`` HTTP endpoint in app/server.py is unaffected.


def test_omicsclaw_tool_description_mentions_sc_batch_auto_prepare():
    specs = build_bot_tool_specs(
        BotToolContext(
            skill_names=("auto", "sc-batch-integration"),
            skill_desc_text="auto (auto route), sc-batch-integration",
        )
    )

    omics_spec = next(spec for spec in specs if spec.name == "omicsclaw")

    # ``auto_prepare=true`` reference lives in the top-level description
    # so the LLM sees the recovery path on every turn.
    assert "auto_prepare=true" in omics_spec.description
    # ``sc-standardize-input`` / ``sc-preprocessing`` are the concrete
    # upstream steps that ``auto_prepare=true`` runs; they live in the
    # ``auto_prepare`` parameter description (model sees both during
    # tool-use). Pin them there.
    auto_prepare_desc = (
        omics_spec.parameters["properties"]["auto_prepare"]["description"]
    )
    assert "sc-standardize-input" in auto_prepare_desc
    assert "sc-preprocessing" in auto_prepare_desc


def test_tool_search_reports_engineering_and_existing_tools(tmp_path: Path):
    executors = _build_executors(tmp_path)

    result = asyncio.run(
        executors["tool_search"](
            {
                "query": "task planning",
                "include_schema": True,
            }
        )
    )
    payload = json.loads(result)
    tool_names = {item["name"] for item in payload["tools"]}

    assert "task_create" in tool_names
    assert "task_list" in tool_names
    assert "todo_write" in tool_names
    assert any("parameters" in item for item in payload["tools"])


def test_file_tools_follow_workspace_and_safe_surface_defaults(tmp_path: Path):
    executors = _build_executors(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    write_result = asyncio.run(
        executors["file_write"](
            {"path": "notes/plan.txt", "content": "alpha\nbeta\n"},
            surface="interactive",
            workspace=str(workspace),
        )
    )
    assert str(workspace / "notes" / "plan.txt") in write_result

    read_result = asyncio.run(
        executors["file_read"](
            {"path": "notes/plan.txt", "start_line": 2, "end_line": 2},
            surface="interactive",
            workspace=str(workspace),
        )
    )
    assert "2: beta" in read_result

    edit_result = asyncio.run(
        executors["file_edit"](
            {
                "path": "notes/plan.txt",
                "old_text": "beta",
                "new_text": "gamma",
            },
            surface="interactive",
            workspace=str(workspace),
        )
    )
    assert "replaced 1 occurrence" in edit_result
    assert (workspace / "notes" / "plan.txt").read_text(encoding="utf-8") == "alpha\ngamma\n"

    safe_default_result = asyncio.run(
        executors["file_write"](
            {"path": "remote.txt", "content": "safe"},
            surface="telegram",
        )
    )
    assert str(tmp_path / "repo" / "output" / "engineering" / "remote.txt") in safe_default_result


def test_task_tools_persist_state_for_session(tmp_path: Path):
    executors = _build_executors(tmp_path)

    create_payload = json.loads(
        asyncio.run(
            executors["task_create"](
                {
                    "title": "Draft integration plan",
                    "description": "Capture the curated tool rollout.",
                },
                session_id="interactive:user:chat-1",
                chat_id="chat-1",
                surface="interactive",
                workspace=str(tmp_path / "workspace"),
            )
        )
    )
    task_id = create_payload["task"]["id"]

    update_payload = json.loads(
        asyncio.run(
            executors["task_update"](
                {
                    "task_id": task_id,
                    "status": "in_progress",
                    "summary": "Shared runtime module drafted.",
                    "artifact_ref": "omicsclaw/runtime/engineering_tools.py",
                },
                session_id="interactive:user:chat-1",
                chat_id="chat-1",
                surface="interactive",
                workspace=str(tmp_path / "workspace"),
            )
        )
    )
    assert update_payload["task"]["status"] == "in_progress"
    assert update_payload["task"]["metadata"]["summary"] == "Shared runtime module drafted."

    list_payload = json.loads(
        asyncio.run(
            executors["task_list"](
                {"status": "in_progress"},
                session_id="interactive:user:chat-1",
                chat_id="chat-1",
                surface="interactive",
                workspace=str(tmp_path / "workspace"),
            )
        )
    )
    assert [task["id"] for task in list_payload["tasks"]] == [task_id]
    assert list_payload["metadata"]["workspace"] == str(tmp_path / "workspace")

    todo_payload = json.loads(
        asyncio.run(
            executors["todo_write"](
                {
                    "items": [
                        {"title": "Inspect registry"},
                        {"title": "Wire executors", "status": "completed"},
                    ]
                },
                session_id="interactive:user:chat-1",
                chat_id="chat-1",
                surface="interactive",
            )
        )
    )
    assert todo_payload["task_count"] == 2
    assert {task["status"] for task in todo_payload["tasks"]} == {"pending", "completed"}


def test_web_fetch_uses_shared_fetcher_and_truncates(monkeypatch, tmp_path: Path):
    executors = _build_executors(tmp_path)
    import omicsclaw.research.web_search as web_search

    async def fake_fetch(url: str, timeout: float = 10.0) -> str:
        assert url == "https://example.org"
        return "0123456789" * 10

    monkeypatch.setattr(web_search, "_fetch_webpage", fake_fetch)

    result = asyncio.run(
        executors["web_fetch"](
            {"url": "https://example.org", "max_chars": 25}
        )
    )
    assert result.startswith("URL: https://example.org")
    assert "... [truncated]" in result


# ``test_mcp_list_sanitizes_environment_values`` removed: the
# ``mcp_list`` bot ToolSpec + executor were deleted in the tool-list-
# compression refactor (0 audit-log calls, no production callers).
# The ``mcp_list_servers`` HTTP endpoint in ``omicsclaw/app/server.py``
# is a separate function and remains tested via the desktop-server suite.


def test_autonomous_analysis_execute_is_approval_gated():
    # F (codex must-fix): autonomous_analysis_execute runs arbitrary generated code
    # (RISK_HIGH). It must be outer-loop approval-gated (ADR 0008 L2), else removing
    # the dead per-cell approval hook would leave it ungated. And the now-unused
    # request_tool_approval context_param must be gone.
    from omicsclaw.runtime.tools.spec import APPROVAL_MODE_ASK

    specs = build_bot_tool_specs(BotToolContext(skill_names=("sc-de",), domain_briefing="(test)"))
    spec = next(s for s in specs if s.name == "autonomous_analysis_execute")
    assert spec.approval_mode == APPROVAL_MODE_ASK
    assert "request_tool_approval" not in (spec.context_params or ())
