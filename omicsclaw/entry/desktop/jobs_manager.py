"""The Desktop jobs plane: a manager, a store, and runner plugins.

P1 of the front-back plan. A *job* is one unit of work the desktop client
started on purpose — today ``kind="skill_run"``, a direct call into a
skill's ``_api.py`` entry function with no model in the loop. The manager
owns each job's asyncio task, its lifecycle events and its approval gate;
the routes in :mod:`~omicsclaw.entry.desktop.jobs` are a thin adapter over
this module, which like the rest of ``entry.desktop`` keeps no web
framework in its signatures.

**The event vocabulary** (the ``type`` of every ``/jobs/{id}/events``
frame, shared with the chat stream where both exist)::

    job.created -> job.started ->
      llm_chunk | tool_started{tool, human_description} |
      tool_output_chunk | progress{label, percent} | heartbeat{state} |
      tool_approval_request{call_id, risk, summary} | usage{tokens} |
      artifact.created{artifact_id, kind, title, path} ->
    job.done{status} | job.failed{error, phase}

``human_description`` is the one-line plain-language summary of what a
tool call does ("run spatial_domains clustering (n=12)"), generated from
the tool name and its arguments by :func:`human_tool_description`.
``artifact.created`` is P2's: the output-contract scanner
(:mod:`~omicsclaw.entry.desktop.artifacts_manager`) emits one frame per
artifact it registers for a finished job, before the terminal event.

Every event is persisted to the ``job_events`` ring (most recent
:data:`JOB_EVENTS_RETAINED` per job) *before* it is broadcast, so a
``Last-Event-ID`` reconnect replays from SQLite and then continues live
with no window in which an event can be lost or duplicated.

**The approval gate reuses the tool layer's channel.** A job runs its
runner inside :func:`~omicsclaw.tools.context.use_tool_context` with a
per-job :class:`JobApprovalGate` as the approval channel and a progress
sink that maps :class:`~omicsclaw.tools.context.ProgressUpdate` onto
``progress`` events — the same seams ``bash`` and every other tool
already speak, so anything that calls
:func:`~omicsclaw.tools.context.require_approval` inside a job is gated
without this module knowing about it.

Not thread-safe as a whole: routes, runners and the gate all run on the
server's event loop. The SQLite store is reached synchronously (it is a
local file and the writes are single-row), which the memory layer's
:class:`~omicsclaw.memory.database.Database` serialises under its own
lock anyway.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import logging
import re
import secrets
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from omicsclaw.memory.artifacts import ArtifactStore
from omicsclaw.memory.database import Database
from omicsclaw.skills.frontmatter import parse_frontmatter
from omicsclaw.tools.context import (
    ApprovalDecision,
    ApprovalRequest,
    ProgressUpdate,
    use_tool_context,
)

__all__ = [
    "DEFAULT_JOB_TIMEOUT_S",
    "JOB_EVENTS_RETAINED",
    "HEARTBEAT_INTERVAL_S",
    "CodeRunRunner",
    "JobError",
    "JobExecutionContext",
    "JobFailure",
    "JobRecord",
    "JobStore",
    "JobsManager",
    "MAX_CONCURRENT_JOBS",
    "TERMINAL_JOB_STATUSES",
    "human_tool_description",
]

_log = logging.getLogger(__name__)

JOB_EVENTS_RETAINED: Final = 5000
"""Events kept per job in the ring. Older ``seq`` numbers are pruned; a
reconnect whose ``Last-Event-ID`` falls behind them is answered from
whatever the ring still holds."""

DEFAULT_JOB_TIMEOUT_S: Final = 3600.0
"""The job ceiling. Deliberately far above the engine per-tool budget:
a job is a thing a person started knowing it takes long, not a model
call that has to be bounded."""

MAX_CONCURRENT_JOBS: Final = 2
TERMINAL_JOB_STATUSES: Final = frozenset(
    {"succeeded", "failed", "canceled", "interrupted"}
)
TERMINAL_EVENT_TYPES: Final = frozenset({"job.done", "job.failed"})
HEARTBEAT_INTERVAL_S: Final = 25.0

_DATA_PARAMETER_NAMES: Final = ("adata", "data", "dataset", "input")
_DATA_INPUT_KEYS: Final = ("adata", "data", "dataset", "input")
_RESULT_SUMMARY_CHARS: Final = 4000
_HUMAN_DESCRIPTION_CHARS: Final = 160


class JobError(ValueError):
    """A refused job request. ``code`` is the wire detail, ``status`` its HTTP status."""

    def __init__(self, code: str, status_code: int = 422) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class JobFailure(Exception):
    """A runner's own report that the job failed, with a phase for the wire.

    ``terminal_status`` lets a runner end the job somewhere other than
    ``failed``: an interrupted cell reports ``interrupted`` (a terminal
    state the wire already knows), which lands as ``job.done`` rather
    than ``job.failed``.
    """

    def __init__(
        self, error: str, phase: str = "run", *, terminal_status: str = "failed"
    ) -> None:
        super().__init__(error)
        self.error = error
        self.phase = phase
        self.terminal_status = terminal_status


@dataclass(frozen=True, slots=True)
class JobRecord:
    """One row of the ``jobs`` table, as the manager and routes pass it around."""

    id: str
    session_id: str = ""
    kind: str = "skill_run"
    skill: str = ""
    inputs: Mapping[str, Any] = field(default_factory=dict)
    status: str = "queued"
    error: str = ""
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "session_id": self.session_id,
            "kind": self.kind,
            "skill": self.skill,
            "inputs": dict(self.inputs),
            "status": self.status,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class JobStore:
    """The ``jobs`` and ``job_events`` tables over one SQLite connection.

    The schema lives in :mod:`omicsclaw.memory.database` so every
    ``Database`` — the memory layer's and this one — creates it, which is
    this codebase's migration mechanism: ``CREATE TABLE IF NOT EXISTS``
    on open.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    @property
    def database(self) -> Database:
        """The connection this store runs on, so another store (P2's
        artifacts) can share one file and one lock with it."""
        return self._db

    def insert_job(self, record: JobRecord) -> None:
        self._db.run(
            lambda conn: conn.execute(
                "INSERT INTO jobs (id, session_id, kind, skill, inputs_json,"
                " status, error, created_at, started_at, finished_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.id,
                    record.session_id,
                    record.kind,
                    record.skill,
                    json.dumps(dict(record.inputs), ensure_ascii=False, default=str),
                    record.status,
                    record.error,
                    record.created_at,
                    record.started_at,
                    record.finished_at,
                ),
            )
        )

    def update_job(self, job_id: str, **changes: Any) -> None:
        allowed = ("status", "error", "started_at", "finished_at", "session_id")
        columns = [name for name in changes if name in allowed]
        if not columns:
            return
        self._db.run(
            lambda conn: conn.execute(
                "UPDATE jobs SET " + ", ".join(c + " = ?" for c in columns)
                + " WHERE id = ?",
                (*(changes[c] for c in columns), job_id),
            )
        )

    def get_job(self, job_id: str) -> JobRecord | None:
        rows = self._db.run(
            lambda conn: conn.execute(
                "SELECT id, session_id, kind, skill, inputs_json, status, error,"
                " created_at, started_at, finished_at FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchall()
        )
        return _row_to_record(rows[0]) if rows else None

    def list_jobs(
        self,
        *,
        session_id: str = "",
        status: str = "",
        limit: int = 100,
    ) -> list[JobRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(limit, 500)))
        rows = self._db.run(
            lambda conn: conn.execute(
                "SELECT id, session_id, kind, skill, inputs_json, status, error,"
                " created_at, started_at, finished_at FROM jobs " + where
                + " ORDER BY created_at DESC, id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        )
        return [_row_to_record(row) for row in rows]

    def interrupt_orphans(self) -> int:
        """Mark every non-terminal job ``interrupted``; called at start-up.

        A job is an asyncio task of this process; nothing survives a
        restart, and a row still saying ``running`` would be a lie the
        event ring cannot even support, because the ring is in the same
        file as the row.
        """
        return self._db.run(
            lambda conn: conn.execute(
                "UPDATE jobs SET status = 'interrupted',"
                " error = 'backend restarted before this job finished',"
                " finished_at = ? WHERE status IN ('queued', 'running',"
                " 'cancel_requested')",
                (time.time(),),
            ).rowcount
        )

    def append_event(
        self, job_id: str, seq: int, event_type: str, payload: Mapping[str, Any]
    ) -> None:
        def _write(conn: Any) -> None:
            conn.execute(
                "INSERT INTO job_events (job_id, seq, type, payload_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    job_id,
                    seq,
                    event_type,
                    json.dumps(dict(payload), ensure_ascii=False, default=str),
                    time.time(),
                ),
            )
            conn.execute(
                "DELETE FROM job_events WHERE job_id = ? AND seq <= ?",
                (job_id, seq - JOB_EVENTS_RETAINED),
            )

        self._db.run(_write)

    def events_after(
        self, job_id: str, after_seq: int, limit: int = 500
    ) -> list[tuple[int, str, dict[str, Any]]]:
        rows = self._db.run(
            lambda conn: conn.execute(
                "SELECT seq, type, payload_json FROM job_events"
                " WHERE job_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?",
                (job_id, after_seq, max(1, limit)),
            ).fetchall()
        )
        events: list[tuple[int, str, dict[str, Any]]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            events.append((int(row["seq"]), str(row["type"]), payload))
        return events

    def latest_seq(self, job_id: str) -> int:
        rows = self._db.run(
            lambda conn: conn.execute(
                "SELECT MAX(seq) AS seq FROM job_events WHERE job_id = ?", (job_id,)
            ).fetchall()
        )
        return int(rows[0]["seq"] or 0) if rows else 0


def _row_to_record(row: Any) -> JobRecord:
    try:
        inputs = json.loads(row["inputs_json"]) if row["inputs_json"] else {}
    except (TypeError, ValueError):
        inputs = {}
    if not isinstance(inputs, dict):
        inputs = {}
    return JobRecord(
        id=str(row["id"]),
        session_id=str(row["session_id"] or ""),
        kind=str(row["kind"] or ""),
        skill=str(row["skill"] or ""),
        inputs=inputs,
        status=str(row["status"] or "queued"),
        error=str(row["error"] or ""),
        created_at=float(row["created_at"] or 0.0),
        started_at=float(row["started_at"]) if row["started_at"] else None,
        finished_at=float(row["finished_at"]) if row["finished_at"] else None,
    )


# ---- the vocabulary helpers ------------------------------------------------


def human_tool_description(tool: str, arguments: str = "") -> str:
    """One plain-language line for a tool call, from its name and arguments.

    The cheap half of the P1 experience win: a card that says "run
    spatial_domains clustering (n=12)" instead of a JSON blob. Never
    raises — an unparseable payload degrades to the tool's name.
    """
    label = tool.rsplit("/", 1)[-1].replace("_", " ").strip() or tool
    try:
        decoded = json.loads(arguments) if arguments else {}
    except (TypeError, ValueError):
        decoded = {}
    if not isinstance(decoded, dict):
        decoded = {}
    facts: list[str] = []
    command = decoded.get("command")
    if isinstance(command, str) and command.strip():
        facts.append(command.strip().splitlines()[0].strip())
    for key in ("method", "name", "skill", "file", "path"):
        value = decoded.get(key)
        if isinstance(value, str) and value.strip():
            facts.append(value.strip())
    for key in ("n", "n_domains", "count", "resolution", "epochs"):
        value = decoded.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            facts.append(key + "=" + str(value))
    summary = " ".join(facts)[:_HUMAN_DESCRIPTION_CHARS]
    verb = "run"
    for candidate in ("delete", "remove", "write", "read"):
        if candidate in tool.lower():
            verb = candidate
            break
    return ("{verb} {label} {summary}".format(verb=verb, label=label, summary=summary)).strip()


def validate_inputs_against_schema(inputs: Any, schema: Mapping[str, Any]) -> str:
    """Validate *inputs* against the JSON Schema subset skills declare.

    Supports ``type``, ``properties``, ``required``, ``enum``,
    ``minimum``, ``maximum``, ``items`` and ``additionalProperties`` —
    enough for a generated form, deliberately not a schema library.
    Returns ``""`` when the inputs conform, else the first problem found,
    phrased for a person who has to fix the form.
    """
    if not isinstance(schema, Mapping):
        return ""
    if not isinstance(inputs, Mapping):
        return "inputs must be a JSON object"
    return _schema_problems(inputs, schema, "$")


def _schema_problems(value: Any, schema: Mapping[str, Any], path: str) -> str:
    expected = schema.get("type")
    if isinstance(expected, str) and not _json_type_matches(value, expected):
        return path + " must be of type " + expected
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return path + " must be one of: " + ", ".join(str(o) for o in enum)
    minimum, maximum = schema.get("minimum"), schema.get("maximum")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(minimum, (int, float)) and value < minimum:
            return path + " must be >= " + str(minimum)
        if isinstance(maximum, (int, float)) and value > maximum:
            return path + " must be <= " + str(maximum)
    if isinstance(value, dict):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, Mapping) else {}
        required = schema.get("required")
        for name in required if isinstance(required, list) else []:
            if name not in value:
                return path + "." + str(name) + " is required"
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    return path + "." + str(name) + " is not an allowed property"
        for name, child in value.items():
            child_schema = properties.get(name)
            if isinstance(child_schema, Mapping):
                problem = _schema_problems(child, child_schema, path + "." + str(name))
                if problem:
                    return problem
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, child in enumerate(value):
                problem = _schema_problems(child, items, path + "[" + str(index) + "]")
                if problem:
                    return problem
    return ""


def _json_type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def skill_inputs_declaration(
    skill_directory: Path,
) -> tuple[str | None, dict[str, Any] | None]:
    """Read a SKILL.md frontmatter optional ``entry`` and ``inputs_schema``.

    ``inputs_schema`` is declared as a block scalar holding JSON, because
    the frontmatter parser this repo ships parses scalars and sequences
    only — a nested mapping would be dropped, while ``inputs_schema: |``
    followed by JSON text round-trips exactly. A value that does not
    parse as JSON is ignored rather than refused: a malformed schema must
    not make a skill unrunnable from the chat surface it already serves.
    """
    try:
        content = (skill_directory / "SKILL.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, None
    frontmatter = parse_frontmatter(content)
    entry = frontmatter.text("entry").strip() or None
    raw_schema = frontmatter.text("inputs_schema").strip()
    if not raw_schema:
        return entry, None
    try:
        schema = json.loads(raw_schema)
    except ValueError:
        return entry, None
    return entry, schema if isinstance(schema, dict) else None


# ---- the approval gate -----------------------------------------------------


class JobApprovalGate:
    """The job's :data:`~omicsclaw.tools.ApprovalChannel`.

    Same semantics as the chat surface's
    :class:`~omicsclaw.entry.approval.ApprovalBroker` — ask, wait, deny on
    deadline — minus the ``TurnStream`` the chat one is welded to, which a
    job has none of. A pending question is an ``asyncio.Future`` keyed by
    ``call_id``; :meth:`settle` is the synchronous half the HTTP route
    calls from a click, and a decision after the fact is the no-op the
    chat broker documents, not an error.

    The id is ``<job_id>.<n>`` rather than the chat broker's
    ``<turn_id>#<n>``: a jobs call id rides a URL path segment
    (``POST /jobs/{id}/approval/{call_id}``), and ``#`` would be read as
    a fragment by every HTTP client. ``.`` cannot appear in a hex job id,
    so the split stays unambiguous.
    """

    def __init__(
        self,
        job_id: str,
        emit: Callable[[str, dict[str, Any]], int],
        *,
        timeout_s: float | None = None,
    ) -> None:
        self._job_id = job_id
        self._emit = emit
        self._timeout_s = timeout_s
        self._pending: dict[str, asyncio.Future[ApprovalDecision]] = {}
        self._counter = 0

    async def __call__(self, request: ApprovalRequest) -> ApprovalDecision:
        self._counter += 1
        call_id = self._job_id + "." + str(self._counter)
        summary = request.reason or human_tool_description(
            request.tool_name, request.arguments
        )
        self._emit(
            "tool_approval_request",
            {
                "call_id": call_id,
                "tool": request.tool_name,
                "risk": str(getattr(request.risk_level, "value", request.risk_level)),
                "summary": summary[:_HUMAN_DESCRIPTION_CHARS],
                "arguments": request.arguments,
            },
        )
        future: asyncio.Future[ApprovalDecision] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[call_id] = future
        try:
            if self._timeout_s is None:
                return await future
            return await asyncio.wait_for(future, self._timeout_s)
        except TimeoutError:
            _log.info("job approval expired: job=%s call=%s", self._job_id, call_id)
            return ApprovalDecision(
                approved=False, reason="no answer before the approval deadline"
            )
        finally:
            self._pending.pop(call_id, None)

    def settle(self, call_id: str, decision: ApprovalDecision) -> bool:
        future = self._pending.get(call_id)
        if future is None or future.done():
            return False
        future.set_result(decision)
        return True

    def abandon(self, reason: str = "the job ended before this was answered") -> None:
        """Deny everything outstanding. Fail closed, like the chat broker."""
        for future in list(self._pending.values()):
            if not future.done():
                future.set_result(ApprovalDecision(approved=False, reason=reason))
        self._pending.clear()


# ---- the runner seam --------------------------------------------------------


class JobExecutionContext:
    """What a runner may do beyond its own return value: speak the vocabulary.

    The typed helpers keep frames shaped, and :meth:`emit` exists so a
    test double (or a future runner with richer events) is not forced
    through them. All methods are synchronous and safe from a worker
    thread, because the skill runner calls them from
    :func:`asyncio.to_thread`.
    """

    def __init__(
        self,
        record: JobRecord,
        emit: Callable[[str, dict[str, Any]], int],
        workspace: Path,
    ) -> None:
        self._record = record
        self._emit_raw = emit
        self.workspace = workspace

    @property
    def job_id(self) -> str:
        return self._record.id

    @property
    def inputs(self) -> Mapping[str, Any]:
        return self._record.inputs

    def emit(self, event_type: str, payload: Mapping[str, Any] | None = None) -> int:
        return self._emit_raw(event_type, dict(payload or {}))

    def progress(self, label: str, percent: float | None = None) -> None:
        payload: dict[str, Any] = {"label": label}
        if percent is not None:
            payload["percent"] = max(0.0, min(100.0, float(percent)))
        self._emit_raw("progress", payload)

    def tool_started(
        self, tool: str, arguments: str = "", human_description: str | None = None
    ) -> None:
        self._emit_raw(
            "tool_started",
            {
                "tool": tool,
                "human_description": (
                    human_description
                    or human_tool_description(tool, arguments)
                )[:_HUMAN_DESCRIPTION_CHARS],
            },
        )

    def tool_output(self, text: str) -> None:
        self._emit_raw(
            "tool_output_chunk", {"text": str(text)[:_RESULT_SUMMARY_CHARS]}
        )


JobRunner = Callable[[JobRecord, JobExecutionContext], Any]
"""``await runner(record, ctx)`` runs one job. Any exception it raises
becomes ``job.failed``; :class:`JobFailure` carries the wire ``phase``."""


class SkillApiRunner:
    """``kind="skill_run"``: call a skill ``_api.py`` entry function directly.

    The entry function is the frontmatter ``entry:`` when declared, else
    the first name of the module ``__all__``, else the first public
    function the module defines — which for the curated skills is the
    function their SKILL.md documents first.

    Arguments: ``inputs`` is passed as keyword arguments. When the entry
    function first positional parameter is not among them and ``inputs``
    carries one of ``adata``/``data``/``dataset``/``input`` as a path
    string, that file is loaded (``.h5ad`` via anndata, ``.csv`` via
    pandas) and handed over — the step-runner shape every
    ``examples/example_step.py`` already uses. Anything the runner cannot
    bind is a ``job.failed`` with ``phase="setup"``, phrased for a person
    staring at a generated form.
    """

    def __init__(self, app: Any, *, artifacts: ArtifactStore | None = None) -> None:
        self._app = app
        self._artifacts = artifacts
        self._modules: dict[str, Any] = {}

    def validate(self, skill: str, inputs: Any) -> None:
        """Refuse a job the runner cannot even start, before any row is written.

        :raises JobError: the skill is unknown, or ``inputs`` does not
            match the schema its frontmatter declares.
        """
        if not isinstance(skill, str) or not skill.strip():
            raise JobError("skill_required")
        name = skill.strip().rsplit("/", 1)[-1]
        index = self._app.skills
        found = index.get(name) if index is not None else None
        if found is None:
            raise JobError("skill_not_found", status_code=404)
        declared = skill.strip()
        declared_domain = declared.rsplit("/", 1)[0] if "/" in declared else ""
        if declared_domain and declared_domain != (found.domain or "general"):
            raise JobError("skill_not_found", status_code=404)
        _, schema = skill_inputs_declaration(found.directory)
        if schema is not None:
            problem = validate_inputs_against_schema(inputs, schema)
            if problem:
                raise JobError("inputs_do_not_match_schema: " + problem)

    async def __call__(self, record: JobRecord, ctx: JobExecutionContext) -> Any:
        skill = record.skill.strip()
        name = skill.rsplit("/", 1)[-1]
        found = self._app.skills.get(name)
        if found is None:
            raise JobFailure("skill " + repr(skill) + " is no longer loaded", phase="setup")
        entry_name, _ = skill_inputs_declaration(found.directory)
        module = await asyncio.to_thread(self._import_module, found.directory, name)
        function = self._entry_function(module, entry_name)
        arguments = await asyncio.to_thread(
            self._bind_arguments, function, record.inputs, ctx.workspace
        )
        tool = "skill:" + skill + "#" + str(getattr(function, "__name__", "entry"))
        ctx.tool_started(
            tool,
            json.dumps(dict(record.inputs), ensure_ascii=False, default=str),
        )
        started = time.time()
        result = await asyncio.to_thread(self._call_entry, function, arguments)
        ctx.progress(skill + " finished in " + f"{time.time() - started:.1f}s", 100.0)
        ctx.tool_output(_summarize_result(result))
        await self._register_artifacts(record, ctx, found.directory)
        return result

    async def _register_artifacts(
        self, record: JobRecord, ctx: JobExecutionContext, skill_directory: Path
    ) -> None:
        """P2 capture channel ②: scan the skill's output contract.

        Runs only on the good path — a job that raised does not get its
        half-written files promoted to the results tray — and a scan that
        itself fails is logged and swallowed: artifacts are a view of a
        finished job, never a reason a finished job reports failure.
        Off the event loop (``to_thread``): the walk and the sha256
        streams are exactly the blocking kind. Idempotent by the store's
        ``(job_id, path)`` check, however many times a scan lands.
        """
        if self._artifacts is None:
            return
        try:
            from .artifacts_manager import register_job_artifacts

            await asyncio.to_thread(
                register_job_artifacts,
                self._artifacts,
                job_id=record.id,
                session_id=record.session_id,
                skill_directory=skill_directory,
                workspace=Path(ctx.workspace),
                emit=lambda event_type, payload: ctx.emit(event_type, payload),
                since=record.created_at,
            )
        except Exception:  # noqa: BLE001 - never fail a done job over the tray
            _log.exception("artifact scan failed for job %s", record.id)

    def _import_module(self, skill_directory: Path, name: str) -> Any:
        """Import ``_api.py`` by path, with the skills root parent importable.

        The curated skills import ``skills.<domain>._lib`` absolutely, so
        the repo root (the directory above the scanned skills root) has
        to be on ``sys.path`` for the load to succeed. Cached per path: a
        second job over the same skill must not re-execute module scope.
        """
        cached = self._modules.get(str(skill_directory))
        if cached is not None:
            return cached
        root = skill_directory.parent.parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        module_name = "omicsclaw_jobs_." + re.sub(
            r"[^0-9a-zA-Z_]", "_", str(skill_directory.relative_to(root))
        )
        spec = importlib.util.spec_from_file_location(
            module_name, skill_directory / "_api.py"
        )
        if spec is None or spec.loader is None:
            raise JobFailure("skill " + repr(name) + " has no importable _api.py", phase="setup")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - a skill import error is job data
            sys.modules.pop(module_name, None)
            raise JobFailure(
                "importing " + repr(name) + " failed: " + str(exc), phase="setup"
            ) from exc
        self._modules[str(skill_directory)] = module
        return module

    def _entry_function(self, module: Any, entry_name: str | None) -> Any:
        if entry_name:
            function = getattr(module, entry_name, None)
            if not callable(function):
                raise JobFailure(
                    "entry " + repr(entry_name) + " is not callable in _api.py",
                    phase="setup",
                )
            return function
        exported = getattr(module, "__all__", None)
        if exported:
            for name in exported:
                function = getattr(module, name, None)
                if callable(function):
                    return function
        defined = sorted(
            (
                (
                    getattr(function, "__code__", None) is not None
                    and function.__code__.co_firstlineno
                    or 0,
                    name,
                    function,
                )
                for name, function in vars(module).items()
                if not name.startswith("_")
                and callable(function)
                and getattr(function, "__module__", "")
                == getattr(module, "__name__", "")
            ),
        )
        for _, name, function in defined:
            return function
        raise JobFailure("_api.py exposes no callable entry function", phase="setup")

    def _bind_arguments(
        self, function: Any, inputs: Mapping[str, Any], workspace: Path
    ) -> dict[str, Any]:
        try:
            parameters = list(inspect.signature(function).parameters.values())
        except (TypeError, ValueError):
            return dict(inputs)
        kwargs = dict(inputs)
        positional = [
            p
            for p in parameters
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        first = positional[0] if positional else None
        if first is not None and first.name not in kwargs:
            for key in _DATA_INPUT_KEYS:
                candidate = kwargs.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    kwargs[first.name] = self._load_data(candidate, workspace)
                    kwargs.pop(key)
                    break
        has_var_keyword = any(p.kind == p.VAR_KEYWORD for p in parameters)
        if not has_var_keyword:
            known = {p.name for p in parameters}
            unknown = sorted(key for key in kwargs if key not in known)
            if unknown:
                raise JobFailure(
                    "unknown input parameters: " + ", ".join(unknown), phase="setup"
                )
        if (
            first is not None
            and first.name not in kwargs
            and first.default is inspect.Parameter.empty
        ):
            raise JobFailure(
                "input "
                + repr(first.name)
                + " is required; pass it, or one of "
                + ", ".join(_DATA_INPUT_KEYS)
                + " as a data file path",
                phase="setup",
            )
        return kwargs

    def _load_data(self, declared: str, workspace: Path) -> Any:
        path = Path(declared).expanduser()
        if not path.is_absolute():
            path = workspace / path
        path = path.resolve()
        if not path.is_file():
            raise JobFailure("input data file not found: " + str(path), phase="setup")
        suffix = path.suffix.lower()
        try:
            if suffix == ".h5ad":
                import anndata  # noqa: PLC0415 - deliberately lazy: heavy import

                return anndata.read_h5ad(path)
            if suffix in (".csv", ".txt", ".tsv"):
                import pandas  # noqa: PLC0415

                return pandas.read_csv(path, sep=None, engine="python")
        except ImportError as exc:
            raise JobFailure(
                "reading " + suffix + " needs a library this backend lacks: " + str(exc),
                phase="setup",
            ) from exc
        raise JobFailure("unsupported input data format: " + suffix, phase="setup")

    def _call_entry(self, function: Any, arguments: dict[str, Any]) -> Any:
        try:
            return function(**arguments)
        except JobFailure:
            raise
        except TypeError as exc:
            raise JobFailure(
                "the entry function rejected the inputs: " + str(exc), phase="setup"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - a skill failure is the job failure
            raise JobFailure(type(exc).__name__ + ": " + str(exc), phase="run") from exc


class CodeRunRunner:
    """``kind="code_run"``: Python on the session's persistent kernel.

    P4's job face. The payload is ``inputs.code`` (folded from the top
    level by the route) plus an optional ``inputs.adata_path`` naming an
    h5ad on the workspace — the input the handle bridge exists for:
    :meth:`~omicsclaw.kernel.PersistentKernelManager.execute` runs it
    through the revision-gated ``sync_in`` and binds the resident object
    to ``adata`` in the cell's namespace, so a second job over an
    unchanged file is a zero-copy skip rather than another full read.

    The session is the request's ``session_id`` (the same id the chat
    face's ``python`` tool keys on — one kernel per session, whichever
    surface asked), or ``job-<id>`` when none came: a job without a
    session still gets a kernel, just a private one.

    Events are the P1 vocabulary, no new types: ``tool_started`` with
    ``tool="python"``, stdout as ``tool_output_chunk`` frames as iopub
    streams them, ``cell_idle_notice`` when a running cell goes output-
    silent, ``usage`` with the per-cell wall/cpu/peak_rss the manager
    probes, and one ``artifact.created`` per kernel-captured figure
    (registered inline with ``capture="kernel"`` — the chat face inserts
    the same rows with no event, having no job stream to speak on).

    ``skill_run`` and ``code_run`` share this whole lifecycle framework —
    the semaphore, the timeout, the approval gate, the ring — because a
    job is a job; only the runner differs.
    """

    def __init__(self, kernels: Any, *, artifact_store: Any = None) -> None:
        self._binding = kernels
        self._artifact_store = artifact_store

    def validate(self, skill: str, inputs: Any) -> None:
        code = inputs.get("code") if isinstance(inputs, dict) else None
        if not isinstance(code, str) or not code.strip():
            raise JobError("code_required")
        adata_path = inputs.get("adata_path")
        if adata_path is not None and not isinstance(adata_path, str):
            raise JobError("invalid_adata_path")
        language = inputs.get("language", "python")
        if language not in ("python", "r"):
            raise JobError("invalid_language")
        if adata_path and language != "python":
            # The h5ad bridge injects python (sc.read_h5ad); an R cell
            # cannot consume it — refuse at the door, not mid-flight.
            raise JobError("adata_bridge_is_python_only")

    async def __call__(self, record: JobRecord, ctx: JobExecutionContext) -> Any:
        from omicsclaw.kernel import KernelCallbacks, format_cell_summary

        manager = self._binding.manager
        code = str(record.inputs["code"])
        adata_path = record.inputs.get("adata_path")
        adata_path = adata_path.strip() if isinstance(adata_path, str) else None
        language = str(record.inputs.get("language", "python"))
        session_id = record.session_id or ("job-" + record.id[:12])
        ctx.tool_started(
            "python" if language == "python" else "r",
            json.dumps({"code": code[:400]}, ensure_ascii=False),
        )
        self._protect(manager, session_id, record, language=language)

        def _on_stream(name: str, text: str) -> None:
            if name == "stdout" and text:
                ctx.tool_output(text)

        def _on_idle(seconds_idle: float, last_label: str) -> None:
            ctx.emit(
                "cell_idle_notice",
                {"seconds_idle": int(seconds_idle), "last_label": last_label},
            )

        def _on_usage(usage: dict[str, Any]) -> None:
            ctx.emit("usage", dict(usage))

        try:
            result = await manager.execute(
                session_id,
                code,
                origin="agent",
                language=language,
                callbacks=KernelCallbacks(
                    on_stream=_on_stream,
                    on_idle=_on_idle,
                    on_usage=_on_usage,
                ),
                adata_in=adata_path or None,
                job_id=record.id,
            )
        finally:
            self._unprotect(manager, session_id, record, language=language)
        for ref in result.figures:
            if ref.artifact_id:
                ctx.emit(
                    "artifact.created",
                    {
                        "artifact_id": ref.artifact_id,
                        "kind": "figure",
                        "title": Path(ref.path).stem,
                        "path": ref.path,
                    },
                )
        summary = format_cell_summary(result)
        if result.status == "interrupted":
            raise JobFailure(
                "the cell was interrupted", phase="run", terminal_status="interrupted"
            )
        if result.status == "error" and result.error is not None:
            raise JobFailure(
                str(result.error.get("ename", "Error"))
                + ": "
                + str(result.error.get("evalue", "")),
                phase="run",
            )
        if result.status == "timeout":
            raise JobFailure("the cell exceeded its wall-clock limit", phase="run")
        if result.status == "dead":
            raise JobFailure("the kernel died mid-cell; retry to cold-start", phase="run")
        ctx.tool_output(summary)
        await self._scan_artifacts(record, ctx)
        return summary

    def _protect(
        self, manager: Any, session_id: str, record: JobRecord, *, language: str = "python"
    ) -> None:
        """Keep the reaper away while this job owns the session's kernel."""
        try:
            manager.protect(session_id, "job:" + record.id, language=language)
        except Exception:  # noqa: BLE001 - protection is best-effort
            _log.exception("kernel protection failed for job %s", record.id)

    def _unprotect(
        self, manager: Any, session_id: str, record: JobRecord, *, language: str = "python"
    ) -> None:
        try:
            manager.unprotect(session_id, "job:" + record.id, language=language)
        except Exception:  # noqa: BLE001
            pass

    async def _scan_artifacts(
        self, record: JobRecord, ctx: JobExecutionContext
    ) -> None:
        """P2's capture channel ②, reused: the fallback scan after a good end.

        A ``code_run`` has no output contract, so the scanner's safety net
        is the whole of it — the conventional ``figures/``/``tables/``
        directories, attributed by mtime from this job's creation. Figures
        the kernel captured inline are already rows (``capture="kernel"``)
        and the store's ``(job_id, path)`` check keeps this scan from
        double-registering them; what it adds is everything else the cell
        wrote by hand.
        """
        if self._artifact_store is None:
            return
        try:
            from .artifacts_manager import register_job_artifacts

            await asyncio.to_thread(
                register_job_artifacts,
                self._artifact_store,
                job_id=record.id,
                session_id=record.session_id,
                skill_directory=Path(ctx.workspace) / ".omicsclaw" / "no-skill",
                workspace=Path(ctx.workspace),
                emit=lambda event_type, payload: ctx.emit(event_type, payload),
                since=record.created_at,
            )
        except Exception:  # noqa: BLE001 - never fail a done job over the tray
            _log.exception("artifact scan failed for job %s", record.id)

    def bind_artifact_store(self, store: Any) -> None:
        """Give the runner the jobs plane's shared store, for the scan."""
        self._artifact_store = store





def _summarize_result(result: Any) -> str:
    if result is None:
        return "done"
    try:
        import anndata  # noqa: PLC0415

        if isinstance(result, anndata.AnnData):
            return "AnnData: " + str(result.n_obs) + " obs x " + str(result.n_vars) + " vars"
    except Exception:  # noqa: BLE001 - summarising must never fail a finished job
        pass
    return repr(result)[:_RESULT_SUMMARY_CHARS]


# ---- the manager ------------------------------------------------------------


@dataclass(slots=True)
class _LiveJob:
    task: asyncio.Task[Any] | None
    gate: JobApprovalGate
    subscribers: list[asyncio.Queue[tuple[int, str, dict[str, Any]]]] = field(
        default_factory=list
    )
    cancelled: bool = False


class JobsManager:
    """Owns the job tasks, their events and their approval gates.

    One instance per desktop app. Constructing it sweeps the store: jobs
    a previous process left non-terminal are marked ``interrupted``,
    because the tasks that were running them are gone.
    """

    def __init__(
        self,
        app: Any,
        *,
        store: JobStore | None = None,
        artifact_store: ArtifactStore | None = None,
        default_timeout_s: float = DEFAULT_JOB_TIMEOUT_S,
        max_concurrent: int = MAX_CONCURRENT_JOBS,
        kernels: Any = None,
    ) -> None:
        self._app = app
        if store is None:
            workspace = Path(app.config.workspace)
            store = JobStore(Database(workspace / ".omicsclaw" / "jobs.db"))
        self.store = store
        self.artifacts = artifact_store or ArtifactStore(store.database)
        self.default_timeout_s = float(default_timeout_s)
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent))
        self._runners: dict[str, Any] = {}
        self._live: dict[str, _LiveJob] = {}
        self._seq: dict[str, int] = {}
        self._approval_timeout_s = getattr(app.config, "approval_timeout_s", None)
        try:
            interrupted = self.store.interrupt_orphans()
        except Exception:  # noqa: BLE001 - a fresh store must not kill the server
            interrupted = 0
        if interrupted:
            _log.info("marked %d orphaned job(s) interrupted at start-up", interrupted)
        self.register_runner(
            "skill_run", SkillApiRunner(app, artifacts=self.artifacts)
        )
        # P4's code_run rides the same lifecycle on the deployment's
        # persistent kernels: the binding build_app mounted behind the
        # ``python`` tool when there is one (one manager, shared session
        # kernels between the faces), else a private one for this manager.
        if kernels is None:
            kernels = getattr(app, "kernels", None)
        if kernels is None:
            from ..assembly import WorkspaceKernelBinding

            kernels = WorkspaceKernelBinding(app.config.workspace)
        self.kernels = kernels
        self.register_runner(
            "code_run",
            CodeRunRunner(kernels, artifact_store=self.artifacts),
        )

    # ---- construction and lookup ----

    def register_runner(self, kind: str, runner: Any) -> None:
        """Bind one ``kind`` to one runner. The injection point for tests."""
        self._runners[kind] = runner

    def runner_kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._runners))

    def get_job(self, job_id: str) -> JobRecord | None:
        return self.store.get_job(job_id)

    def list_jobs(
        self, *, session_id: str = "", status: str = "", limit: int = 100
    ) -> list[JobRecord]:
        return self.store.list_jobs(
            session_id=session_id, status=status, limit=limit
        )

    # ---- lifecycle ----

    async def create_job(
        self,
        *,
        kind: str = "skill_run",
        skill: str = "",
        inputs: Any = None,
        session_id: str = "",
        workspace: str = "",
    ) -> JobRecord:
        """Validate, persist and start one job. Returns its record.

        :raises JobError: the kind is unknown, or the runner ``validate``
            refused the skill or inputs.
        """
        runner = self._runners.get(kind)
        if runner is None:
            raise JobError("unknown_kind")
        if inputs is None:
            inputs = {}
        if not isinstance(inputs, dict):
            raise JobError("inputs_must_be_object")
        declared_workspace = str(getattr(self._app.config, "workspace", ""))
        if workspace and workspace.strip():
            try:
                same = Path(workspace.strip()).expanduser().resolve() == Path(
                    declared_workspace
                ).expanduser().resolve()
            except (ValueError, OSError, RuntimeError):
                same = False
            if not same:
                raise JobError(
                    "workspace_does_not_match_backend_runtime", status_code=409
                )
        validate = getattr(runner, "validate", None)
        if callable(validate):
            validate(skill, inputs)
        record = JobRecord(
            id=secrets.token_hex(16),
            session_id=str(session_id or ""),
            kind=kind,
            skill=str(skill or ""),
            inputs=inputs,
            status="queued",
            created_at=time.time(),
        )
        self.store.insert_job(record)
        self._emit(
            record.id,
            "job.created",
            {"job_id": record.id, "kind": kind, "skill": record.skill},
        )
        live = _LiveJob(task=None, gate=self._make_gate(record.id))
        self._live[record.id] = live
        live.task = asyncio.create_task(
            self._run(record.id), name="omicsclaw-job-" + record.id
        )
        return record

    def cancel(self, job_id: str) -> str:
        """Cancel one job. Answers ``"canceled"``, ``"unknown"`` or ``"finished"``.

        Cancellation is the asyncio kind: the runner task is cancelled at
        its next ``await``. A skill executing a long numpy call in a
        worker thread cannot be interrupted mid-call — the thread runs to
        its end, orphaned, while the job is already reported canceled.
        That is the documented bound of in-process skill calls.
        """
        record = self.store.get_job(job_id)
        if record is None:
            return "unknown"
        live = self._live.get(job_id)
        if live is None or record.status in TERMINAL_JOB_STATUSES:
            return "finished"
        live.cancelled = True
        self.store.update_job(job_id, status="cancel_requested")
        if live.task is not None and not live.task.done():
            live.task.cancel()
        return "canceled"

    def settle_approval(
        self, job_id: str, call_id: str, decision: ApprovalDecision
    ) -> bool:
        live = self._live.get(job_id)
        if live is None:
            return False
        return live.gate.settle(call_id, decision)

    # ---- events ----

    def subscribe(self, job_id: str) -> asyncio.Queue[tuple[int, str, dict[str, Any]]]:
        """Register one live event queue for the SSE route."""
        live = self._live.get(job_id)
        if live is None:
            live = _LiveJob(task=None, gate=self._make_gate(job_id))
            self._live[job_id] = live
        queue: asyncio.Queue[tuple[int, str, dict[str, Any]]] = asyncio.Queue(
            maxsize=1000
        )
        live.subscribers.append(queue)
        return queue

    def unsubscribe(
        self, job_id: str, queue: asyncio.Queue[tuple[int, str, dict[str, Any]]]
    ) -> None:
        live = self._live.get(job_id)
        if live is None:
            return
        try:
            live.subscribers.remove(queue)
        except ValueError:
            pass

    def replay_events(
        self, job_id: str, after_seq: int
    ) -> list[tuple[int, str, dict[str, Any]]]:
        return self.store.events_after(job_id, after_seq)

    def _emit(self, job_id: str, event_type: str, payload: dict[str, Any]) -> int:
        seq = self._seq.get(job_id, self.store.latest_seq(job_id)) + 1
        self._seq[job_id] = seq
        self.store.append_event(job_id, seq, event_type, payload)
        live = self._live.get(job_id)
        if live is not None:
            frame = (seq, event_type, payload)
            for queue in list(live.subscribers):
                try:
                    queue.put_nowait(frame)
                except asyncio.QueueFull:
                    _log.warning(
                        "job event subscriber dropped a frame: job=%s seq=%s",
                        job_id,
                        seq,
                    )
        return seq

    def _make_gate(self, job_id: str) -> JobApprovalGate:
        return JobApprovalGate(
            job_id,
            lambda event_type, payload: self._emit(job_id, event_type, payload),
            timeout_s=self._approval_timeout_s,
        )

    async def _run(self, job_id: str) -> None:
        record = self.store.get_job(job_id)
        live = self._live.get(job_id)
        if record is None or live is None:
            return
        runner = self._runners.get(record.kind)
        gate = live.gate
        ctx = JobExecutionContext(
            record,
            lambda t, p: self._emit(job_id, t, p),
            Path(self._app.config.workspace),
        )
        progress_sink = self._progress_sink(job_id)
        terminal: tuple[str, dict[str, Any]] | None = None
        try:
            async with self._semaphore:
                self.store.update_job(job_id, status="running", started_at=time.time())
                self._emit(job_id, "job.started", {"job_id": job_id})
                with use_tool_context(
                    approval=gate,
                    progress=progress_sink,
                    values={
                        "session_id": record.session_id,
                        "job_id": job_id,
                        "surface": "desktop-jobs",
                    },
                ):
                    async with asyncio.timeout(self.default_timeout_s):
                        if runner is None:
                            raise JobFailure("no runner for kind " + record.kind, phase="setup")
                        await runner(record, ctx)
                terminal = ("job.done", {"status": "succeeded"})
                self.store.update_job(
                    job_id, status="succeeded", finished_at=time.time()
                )
        except TimeoutError:
            terminal = (
                "job.failed",
                {
                    "error": "job exceeded its "
                    + str(int(self.default_timeout_s))
                    + "s limit",
                    "phase": "run",
                },
            )
            self.store.update_job(
                job_id, status="failed", error="timeout", finished_at=time.time()
            )
        except asyncio.CancelledError:
            terminal = ("job.done", {"status": "canceled"})
            self.store.update_job(job_id, status="canceled", finished_at=time.time())
        except JobFailure as exc:
            if exc.terminal_status == "interrupted":
                terminal = ("job.done", {"status": "interrupted"})
                self.store.update_job(
                    job_id, status="interrupted", error=exc.error, finished_at=time.time()
                )
            else:
                terminal = ("job.failed", {"error": exc.error, "phase": exc.phase})
                self.store.update_job(
                    job_id, status="failed", error=exc.error, finished_at=time.time()
                )
        except Exception as exc:  # noqa: BLE001 - one job must never take the plane down
            _log.exception("job %s crashed the runner", job_id)
            named = type(exc).__name__ + ": " + str(exc)
            terminal = ("job.failed", {"error": named, "phase": "run"})
            self.store.update_job(
                job_id, status="failed", error=named, finished_at=time.time()
            )
        finally:
            gate.abandon()
        if terminal is not None:
            self._emit(job_id, terminal[0], terminal[1])
            live = self._live.get(job_id)
            if live is not None and not live.subscribers:
                self._live.pop(job_id, None)

    def _progress_sink(self, job_id: str) -> Callable[[ProgressUpdate], None]:
        def sink(update: ProgressUpdate) -> None:
            label = update.message or update.tool_name
            if not label:
                return
            payload: dict[str, Any] = {"label": label}
            if update.fraction is not None:
                payload["percent"] = max(
                    0.0, min(100.0, update.fraction * 100.0)
                )
            self._emit(job_id, "progress", payload)

        return sink
