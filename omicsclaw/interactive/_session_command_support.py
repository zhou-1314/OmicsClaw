"""Shared session command views for interactive surfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from rich.markup import escape

from omicsclaw.runtime.transcript_store import build_transcript_summary

from ._plan_mode_support import (
    build_interactive_plan_summary_lines,
    normalize_interactive_plan_metadata,
)
from ._pipeline_support import (
    build_pipeline_display_from_snapshot,
    load_pipeline_workspace_snapshot,
    resolve_pipeline_workspace,
)
from ._session import (
    delete_session,
    export_conversation_to_markdown,
    format_relative_time,
    list_sessions,
    load_session,
)


@dataclass(slots=True)
class SessionListEntry:
    session_id: str
    preview: str = ""
    message_count: int = 0
    compacted_tool_result_count: int = 0
    plan_reference_count: int = 0
    advisory_event_count: int = 0
    model: str = ""
    updated_at: str = ""
    updated_label: str = ""


@dataclass(slots=True)
class SessionListView:
    entries: list[SessionListEntry] = field(default_factory=list)
    empty_text: str = "No saved sessions."
    hint_text: str = "/resume to continue a session  /delete <id> to remove  /new to start fresh"


@dataclass(slots=True)
class SessionCommandView:
    output_text: str
    success: bool = True
    render_as_markup: bool = False
    session_id: str = ""
    workspace_dir: str = ""
    session_metadata: dict[str, Any] = field(default_factory=dict)
    replace_session_metadata: bool = False
    messages: list[dict[str, Any]] = field(default_factory=list)
    replace_messages: bool = False
    clear_messages: bool = False
    clear_pipeline_workspace: bool = False
    reset_session_runtime: bool = False
    export_path: str = ""


def normalize_session_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    normalized = normalize_interactive_plan_metadata(metadata)
    pipeline_workspace = str(normalized.get("pipeline_workspace", "") or "").strip()
    if pipeline_workspace:
        normalized["pipeline_workspace"] = pipeline_workspace
    else:
        normalized.pop("pipeline_workspace", None)
    return normalized


def build_session_metadata(
    metadata: dict[str, Any] | None,
    *,
    pipeline_workspace: str | None = None,
) -> dict[str, Any]:
    normalized = normalize_session_metadata(metadata)
    value = str(pipeline_workspace or "").strip()
    if value:
        normalized["pipeline_workspace"] = value
    else:
        normalized.pop("pipeline_workspace", None)
    return normalized


def resolve_active_pipeline_workspace(
    pipeline_workspace: str | None,
    metadata: dict[str, Any] | None,
) -> str | None:
    current = str(pipeline_workspace or "").strip()
    if current:
        return current
    metadata_value = str(
        normalize_session_metadata(metadata).get("pipeline_workspace", "") or ""
    ).strip()
    return metadata_value or None


async def build_session_list_view(limit: int = 20) -> SessionListView:
    sessions = await list_sessions(limit=limit)
    return SessionListView(
        entries=[
            SessionListEntry(
                session_id=str(item.get("session_id", "") or ""),
                preview=str(item.get("preview", "") or ""),
                message_count=int(item.get("message_count", 0) or 0),
                compacted_tool_result_count=int(
                    item.get("compacted_tool_result_count", 0) or 0
                ),
                plan_reference_count=int(item.get("plan_reference_count", 0) or 0),
                advisory_event_count=int(item.get("advisory_event_count", 0) or 0),
                model=str(item.get("model", "") or ""),
                updated_at=str(item.get("updated_at", "") or ""),
                updated_label=format_relative_time(item.get("updated_at")),
            )
            for item in sessions
        ]
    )


def format_session_list_plain(
    view: SessionListView,
    *,
    header: str = "Recent sessions (newest first):",
    hint_text: str | None = None,
) -> str:
    if not view.entries:
        return view.empty_text

    lines = [header]
    for entry in view.entries:
        line = f"  [{entry.session_id}]  {entry.preview[:40]}  ({entry.message_count} msgs)"
        if entry.compacted_tool_result_count > 0:
            line += f" · {entry.compacted_tool_result_count} compacted"
        if entry.plan_reference_count > 0:
            line += f" · {entry.plan_reference_count} plan"
        if entry.advisory_event_count > 0:
            line += f" · {entry.advisory_event_count} advisory"
        lines.append(line)
    final_hint = view.hint_text if hint_text is None else hint_text
    if final_hint:
        lines.append("")
        lines.append(final_hint)
    return "\n".join(lines)


def build_new_session_command_view(session_id: str) -> SessionCommandView:
    return SessionCommandView(
        output_text=f"New session: {session_id}",
        session_id=session_id,
        session_metadata={},
        replace_session_metadata=True,
        clear_messages=True,
        clear_pipeline_workspace=True,
        reset_session_runtime=True,
    )


def build_clear_conversation_command_view() -> SessionCommandView:
    return SessionCommandView(
        output_text="Conversation history cleared.",
        clear_messages=True,
    )


def build_export_session_command_view(
    session_id: str,
    messages: list[dict],
    *,
    workspace_dir: str | Path,
) -> SessionCommandView:
    export_dir = Path(workspace_dir) / "exports"
    export_path = export_dir / f"omicsclaw_session_{session_id}.md"
    try:
        export_conversation_to_markdown(session_id, messages, export_path)
    except Exception as exc:
        return SessionCommandView(
            output_text=f"Export failed: {exc}",
            success=False,
        )

    return SessionCommandView(
        output_text=f"Session exported to: {export_path}",
        export_path=str(export_path),
    )


def _flatten_message_content(content: Any) -> str:
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return " ".join(str(part) for part in parts if str(part).strip()).strip()
    return str(content or "").strip()


def _conversation_preview_lines(
    messages: Iterable[dict[str, Any]],
    *,
    limit: int = 6,
    max_len: int = 160,
) -> list[str]:
    preview_lines: list[str] = []
    visible = [message for message in messages if message.get("role") in ("user", "assistant")]
    for message in visible[-limit:]:
        role = str(message.get("role", "") or "")
        content = _flatten_message_content(message.get("content", ""))
        if not content:
            continue
        preview = escape(content[:max_len])
        if role == "user":
            preview_lines.append(f"[bold cyan]❯ [/bold cyan]{preview}")
        elif role == "assistant":
            preview_lines.append(f"[dim]{preview}[/dim]")
    return preview_lines


def _session_transcript_from_data(data: dict[str, Any]) -> list[dict[str, Any]]:
    transcript = data.get("transcript")
    if isinstance(transcript, list):
        return [message for message in transcript if isinstance(message, dict)]
    messages = data.get("messages")
    if isinstance(messages, list):
        return [message for message in messages if isinstance(message, dict)]
    return []


def _session_transcript_summary_from_data(
    data: dict[str, Any],
    *,
    workspace_dir: str = "",
    session_metadata: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    summary = data.get("transcript_summary")
    if isinstance(summary, dict):
        compacted = summary.get("compacted_tool_results")
        plans = summary.get("plan_references")
        advisories = summary.get("advisory_events")
        if (
            isinstance(compacted, list)
            and isinstance(plans, list)
            and isinstance(advisories, list)
        ):
            return {
                "compacted_tool_results": [item for item in compacted if isinstance(item, dict)],
                "plan_references": [item for item in plans if isinstance(item, dict)],
                "advisory_events": [item for item in advisories if isinstance(item, dict)],
            }

    built = build_transcript_summary(
        _session_transcript_from_data(data),
        metadata=session_metadata or data.get("metadata"),
        workspace=workspace_dir or str(data.get("workspace", "") or ""),
    ).to_dict()
    return {
        key: [item for item in built.get(key, []) if isinstance(item, dict)]
        for key in ("compacted_tool_results", "plan_references", "advisory_events")
    }


def build_resume_session_command_view_from_data(data: dict[str, Any]) -> SessionCommandView:
    session_id = str(data.get("session_id", "") or "")
    workspace_dir = str(data.get("workspace", "") or "").strip()
    messages = _session_transcript_from_data(data)
    session_metadata = normalize_session_metadata(data.get("metadata"))
    pipeline_workspace = resolve_active_pipeline_workspace(None, session_metadata)
    transcript_summary = _session_transcript_summary_from_data(
        data,
        workspace_dir=workspace_dir,
        session_metadata=session_metadata,
    )
    compacted_tool_results = transcript_summary["compacted_tool_results"]
    plan_references = transcript_summary["plan_references"]
    advisory_events = transcript_summary["advisory_events"]

    lines = [f"[green]Resumed session:[/green] [yellow]{escape(session_id)}[/yellow]"]
    if workspace_dir:
        lines.append(f"[dim]Workspace:[/dim] [cyan]{escape(workspace_dir)}[/cyan]")
    if pipeline_workspace:
        lines.append(
            f"[dim]Pipeline Workspace:[/dim] [cyan]{escape(pipeline_workspace)}[/cyan]"
        )
    for line in build_interactive_plan_summary_lines(session_metadata):
        label, _, value = line.partition(": ")
        lines.append(
            f"[dim]{escape(label)}:[/dim] {escape(value)}"
        )
    if compacted_tool_results:
        lines.append(
            f"[dim]Compacted Results:[/dim] {len(compacted_tool_results)} saved artifact(s)"
        )
    if plan_references:
        lines.append(
            f"[dim]Plan References:[/dim] {len(plan_references)} linked plan artifact(s)"
        )
    if advisory_events:
        lines.append(
            f"[dim]Advisory Events:[/dim] {len(advisory_events)} recorded hint(s)"
        )

    preview_lines = _conversation_preview_lines(messages)
    if preview_lines:
        lines.append("")
        lines.append("[dim]── Conversation history ──[/dim]")
        lines.extend(preview_lines)
        lines.append("[dim]── End of history ──[/dim]")

    return SessionCommandView(
        output_text="\n".join(lines),
        render_as_markup=True,
        session_id=session_id,
        workspace_dir=workspace_dir,
        session_metadata=session_metadata,
        replace_session_metadata=True,
        messages=messages,
        replace_messages=True,
    )


async def build_resume_session_command_view(target_id: str) -> SessionCommandView:
    data = await load_session(target_id)
    if not data:
        return SessionCommandView(
            output_text=f"Session '{target_id}' not found.",
            success=False,
        )
    return build_resume_session_command_view_from_data(data)


def build_current_session_command_view(
    *,
    session_id: str,
    workspace_dir: str,
    model: str,
    provider: str,
    messages: list[dict[str, Any]],
    session_metadata: dict[str, Any] | None = None,
    pipeline_workspace: str | None = None,
) -> SessionCommandView:
    active_pipeline_workspace = resolve_active_pipeline_workspace(
        pipeline_workspace,
        session_metadata,
    )
    transcript_summary = build_transcript_summary(
        messages,
        metadata={"pipeline_workspace": active_pipeline_workspace} if active_pipeline_workspace else session_metadata,
        workspace=workspace_dir,
    ).to_dict()

    lines = [
        f"[dim]Session:[/dim]   [yellow]{escape(session_id)}[/yellow]",
        f"[dim]Workspace:[/dim] [cyan]{escape(workspace_dir)}[/cyan]",
    ]
    if active_pipeline_workspace:
        lines.append(
            f"[dim]Pipeline WS:[/dim] [cyan]{escape(active_pipeline_workspace)}[/cyan]"
        )
    for line in build_interactive_plan_summary_lines(session_metadata):
        label, _, value = line.partition(": ")
        lines.append(
            f"[dim]{escape(label)}:[/dim] {escape(value)}"
        )
    lines.extend(
        (
            f"[dim]Model:[/dim]     [magenta]{escape(model)}[/magenta]",
            f"[dim]Provider:[/dim]  [magenta]{escape(provider)}[/magenta]",
            f"[dim]Messages:[/dim]  {len([m for m in messages if m.get('role') in ('user', 'assistant')])}",
        )
    )
    if transcript_summary["compacted_tool_results"]:
        lines.append(
            f"[dim]Compacted:[/dim] {len(transcript_summary['compacted_tool_results'])} tool result artifact(s)"
        )
    if transcript_summary["plan_references"]:
        lines.append(
            f"[dim]Plan Refs:[/dim] {len(transcript_summary['plan_references'])} linked plan artifact(s)"
        )
    if transcript_summary["advisory_events"]:
        lines.append(
            f"[dim]Advisories:[/dim] {len(transcript_summary['advisory_events'])} recorded hint(s)"
        )

    snapshot = load_pipeline_workspace_snapshot(
        resolve_pipeline_workspace(
            None,
            active_pipeline_workspace or workspace_dir,
        )
    )
    if snapshot.has_pipeline_state:
        snapshot_view = build_pipeline_display_from_snapshot(snapshot)
        lines.append(
            f"[dim]Pipeline Stage:[/dim] [cyan]{escape(snapshot_view.current_stage or 'idle')}[/cyan]"
        )
        if snapshot_view.plan.status:
            lines.append(
                f"[dim]Plan Status:[/dim] [cyan]{escape(snapshot_view.plan.status)}[/cyan]"
            )

    return SessionCommandView(
        output_text="\n".join(lines),
        render_as_markup=True,
    )


async def build_delete_session_command_view(
    target_id: str,
    *,
    current_session_id: str,
) -> SessionCommandView:
    if not target_id:
        return SessionCommandView(
            output_text="Usage: /delete <session-id>",
            success=False,
        )
    if target_id == current_session_id:
        return SessionCommandView(
            output_text="Cannot delete the current active session.",
            success=False,
        )

    data = await load_session(target_id)
    if not data:
        return SessionCommandView(
            output_text=f"Session '{target_id}' not found.",
            success=False,
        )

    deleted = await delete_session(str(data.get("session_id", "") or ""))
    if deleted:
        return SessionCommandView(
            output_text=f"Deleted session {data['session_id']}.",
        )
    return SessionCommandView(
        output_text="Failed to delete session.",
        success=False,
    )
