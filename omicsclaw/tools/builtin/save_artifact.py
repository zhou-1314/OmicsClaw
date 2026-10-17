"""Register an existing workspace file as an artifact with checksum and lineage.

The tool resolves the path and rejects escaped, sensitive and hidden paths.
Composition supplies the registry sink. Registration does not create the
source file, and approval receives the original call payload.

Historical design notes: docs/engineering/runtime-framework-design-notes.md.
"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from omicsclaw.schema import ToolDefinition
from omicsclaw.schema.artifacts import ARTIFACT_KINDS, kind_and_mime_for

from .._workspace import Workspace
from ..base import ApprovalMode, RiskLevel, ToolPolicy
from ..context import context_value, require_approval
from ..function_tool import ToolArgumentError, decode_arguments, validate_arguments
from .read import resolve_workspace

TOOL_NAME = "save_artifact"

_DESCRIPTION = (
    "Register a file you produced as a named analysis artifact, so it "
    "appears in the desktop results tray with a type icon or thumbnail "
    "and stays traceable to this session. The file must already exist "
    "inside the workspace — this tool does not create or modify "
    "anything, it records the file (path, kind, size, sha256) in the "
    "artifact registry. `path` is relative to the workspace root. "
    "`kind` is one of figure | table | h5ad | rds | report | pdf | other "
    "and defaults to the extension's natural kind. `title` defaults to "
    "the file name. Use it for results a person will want to look at, "
    "not for intermediates."
)

SAVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "File to register, relative to the workspace root, e.g. "
                "'figures/umap.png'. Required; it must already exist."
            ),
        },
        "kind": {
            "type": "string",
            "enum": sorted(ARTIFACT_KINDS),
            "description": (
                "Artifact kind; defaults to the extension's kind "
                "(png/svg/jpg -> figure, csv/tsv -> table, h5ad -> h5ad, "
                "rds -> rds, html/md -> report, pdf -> pdf, else other)."
            ),
        },
        "title": {
            "type": "string",
            "description": "Human-facing title; defaults to the file name.",
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}

_POLICY = ToolPolicy(
    risk_level=RiskLevel.HIGH,
    approval_mode=ApprovalMode.ASK,
    prompts_for_itself=True,
    read_only=False,
    writes_workspace=False,
    concurrency_safe=True,
    allowed_in_background=True,
    tags=frozenset({"workspace", "artifacts", "registration"}),
)
"""``HIGH``/``ASK``, declared: the write category, per the P2 plan.

``writes_workspace=False`` and ``concurrency_safe=True`` are the honest
claims — the bytes on disk are untouched, and two concurrent calls on
one path resolve to the same session-level row via the store's
idempotency check rather than racing."""


@runtime_checkable
class ArtifactSink(Protocol):
    """Where a registration actually lands. Injected, never imported.

    The tools layer is a leaf (``omicsclaw.schema`` and the standard
    library only), and the registry lives in the memory layer, so the
    tool speaks this one-method seam instead: the composition root binds
    an adapter over the workspace's :class:`~omicsclaw.memory.artifacts.ArtifactStore`.
    Like :class:`~omicsclaw.tools.builtin.write.FileWriteEnvironment`,
    the Protocol is structural, so a test double stubs one method rather
    than a store.
    """

    def save(
        self, *, path: str, kind: str, title: str, session_id: str
    ) -> str:
        """Register one file. Returns the human-facing result line
        (``"Saved artifact ..."`` / ``"Already saved ..."``); raises when
        the file cannot be registered."""
        ...


class SaveArtifactTool:
    """Register one existing workspace file as an artifact.

    Satisfies :class:`~omicsclaw.tools.base.Tool` structurally, exactly
    as :class:`~omicsclaw.tools.builtin.write.WriteTool` does. ``sink``
    is the injected registrar (see :class:`ArtifactSink`); omitting it
    constructs a tool that refuses at execution time, because the leaf
    rule means this module cannot open a database for itself.
    """

    policy = _POLICY

    def __init__(
        self,
        workspace: Workspace | None = None,
        *,
        sink: ArtifactSink | None = None,
    ) -> None:
        self._workspace = workspace
        self._sink = sink
        self._definition = ToolDefinition(
            name=TOOL_NAME,
            description=_DESCRIPTION,
            input_schema=copy.deepcopy(SAVE_SCHEMA),
        )

    @property
    def name(self) -> str:
        return TOOL_NAME

    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, arguments: str) -> str:
        """Decode, resolve, **ask**, then register. Nothing before the
        approval survives anywhere but memory."""
        decoded = decode_arguments(arguments)
        issues = validate_arguments(decoded, SAVE_SCHEMA)
        if issues:
            listed = "\n".join("  - " + issue for issue in issues)
            raise ToolArgumentError(
                "the arguments do not match this tool's schema:\n"
                f"{listed}\nRe-send the call with all of these corrected."
            )

        target = str(decoded["path"]).strip()
        if not target:
            raise ToolArgumentError(
                "input.path is required and must name a file inside the workspace"
            )
        declared_kind = decoded.get("kind")
        declared_title = decoded.get("title")
        if isinstance(declared_title, str) and not declared_title.strip():
            declared_title = None

        workspace = resolve_workspace(self._workspace)
        # Hidden segments are refused here even though ``Workspace.resolve``
        # allows some of them: ``/files/serve`` refuses every dot-leading
        # segment, and a registered artifact must be servable, not a dead
        # tray entry that 403s when clicked.
        if any(part.startswith(".") for part in Path(target).parts):
            raise ToolArgumentError(
                f"{target!r} names a hidden path; save_artifact registers "
                "files the results tray can serve, and hidden paths are "
                "not servable"
            )
        resolved = workspace.resolve(target)

        if not resolved.is_file():
            raise ToolArgumentError(
                f"{target!r} does not name an existing file inside the "
                "workspace; save_artifact registers a file that is already "
                "written — write it first, then save it"
            )
        kind = declared_kind if isinstance(declared_kind, str) else None
        kind = kind or kind_and_mime_for(resolved.name)[0]
        title = declared_title or resolved.stem

        await require_approval(
            self.name,
            arguments,
            policy=self.policy,
            reason=(
                f"register {resolved} as a {kind} artifact of this session "
                f"({resolved.stat().st_size} bytes; it becomes visible in "
                "the results tray)"
            ),
        )

        if self._sink is None:
            raise RuntimeError(
                "save_artifact has no artifact sink bound; the surface "
                "assembling the registry must provide one"
            )
        # ``sink.save`` reads and hashes the file before writing its row —
        # up to half a gigabyte of disk IO that must not pause the event
        # loop serving every other client. The job-plane scanner hashes
        # through ``asyncio.to_thread`` for the same reason.
        return await asyncio.to_thread(
            self._sink.save,
            path=str(resolved),
            kind=kind,
            title=title,
            session_id=str(context_value("session_id", "") or ""),
        )


__all__ = [
    "ArtifactSink",
    "SAVE_SCHEMA",
    "TOOL_NAME",
    "SaveArtifactTool",
]
