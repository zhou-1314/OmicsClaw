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
import hashlib
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

from omicsclaw.entry.render import HUMAN_DESCRIPTION_CHARS, human_tool_description
from omicsclaw.envsupply import parse_env_step
from omicsclaw.memory.artifacts import ArtifactStore
from omicsclaw.memory.database import Database
from omicsclaw.remote import RemoteJobsBridge, refresh_in_flight
from omicsclaw.runs.manifest import build_manifest
from omicsclaw.runs.models import Activity, Kernel, Run, Verification
from omicsclaw.runs.models import Job as RunJob
from omicsclaw.runs.projections import (
    belongs_to_run,
    bind_kernel,
    ensure_session_run,
    observe_kernel,
    plan_snapshot,
)
from omicsclaw.runs.store import RunStore
from omicsclaw.runs.verification import verify_artifact, verify_exit_code, verify_file
from omicsclaw.schema.env_names import ENV_NAME_PATTERN, PACKAGE_NAME_PATTERN
from omicsclaw.skills.frontmatter import parse_frontmatter
from omicsclaw.tools.builtin.remote import valid_host
from omicsclaw.tools.context import (
    ApprovalDecision,
    ApprovalRequest,
    ProgressUpdate,
    use_tool_context,
)

__all__ = [
    "DEFAULT_JOB_TIMEOUT_S",
    "HEARTBEAT_INTERVAL_S",
    "JOB_EVENTS_RETAINED",
    "MAX_CONCURRENT_JOBS",
    "TERMINAL_JOB_STATUSES",
    "CodeRunRunner",
    "EnvInstallRunner",
    "JobError",
    "JobExecutionContext",
    "JobFailure",
    "JobRecord",
    "JobStore",
    "JobsManager",
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
_PROJECTION_BYTES: Final = 512 * 1024
_JOB_COLUMNS = (
    "id,session_id,kind,skill,inputs_json,runtime,target_alias,target_workdir,capabilities_json,"
    "status,error,created_at,started_at,finished_at,lifetime,source_request_id,payload_hash,"
    "length(CAST(projection_json AS BLOB)) AS projection_bytes,"
    f"CASE WHEN length(CAST(projection_json AS BLOB)) <= {_PROJECTION_BYTES} "
    "THEN projection_json ELSE NULL END AS projection_json"
)


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
    runtime: str = "local"
    """``local``, or ``remote:<alias>`` — where this job's work runs.

    Persisted (the ``jobs`` column of the same name) because it decides
    which runner a *restarted* backend would need too: a remote job is
    not this process's asyncio task, so the start-up sweep that marks
    orphans interrupted leaves it alone and the reconciler settles it
    from the handle row instead."""
    status: str = "queued"
    error: str = ""
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    target_alias: str = ""
    target_workdir: str = ""
    capabilities: tuple[str, ...] = ()
    lifetime: str = ""
    source_request_id: str = ""
    payload_hash: str = ""
    projection: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.target_alias and self.runtime.startswith("remote:"):
            object.__setattr__(self, "target_alias", self.runtime[len("remote:") :])
        if not self.target_workdir and isinstance(self.inputs, Mapping):
            candidate = self.inputs.get("target_workdir")
            if isinstance(candidate, str):
                object.__setattr__(self, "target_workdir", candidate)
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        if not self.lifetime:
            object.__setattr__(
                self,
                "lifetime",
                "in_process" if self.runtime == "local" else "external",
            )
        if self.lifetime not in {"in_process", "external"}:
            raise ValueError("invalid_job_lifetime")

    def as_payload(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "session_id": self.session_id,
            "kind": self.kind,
            "skill": self.skill,
            "inputs": dict(self.inputs),
            "runtime": self.runtime,
            "lifetime": self.lifetime,
            "source_request_id": self.source_request_id,
            "target_alias": self.target_alias or None,
            "target_workdir": self.target_workdir or None,
            "capabilities": list(self.capabilities),
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
        self._db.run(lambda conn: self._insert_job(conn, record))

    @staticmethod
    def _insert_job(conn, record):
        projection_json = json.dumps(dict(record.projection), ensure_ascii=False)
        if len(projection_json.encode()) > _PROJECTION_BYTES:
            raise JobError("job_projection_too_large")
        return conn.execute(
            "INSERT INTO jobs (id, session_id, kind, skill, inputs_json,"
            " runtime, target_alias, target_workdir, capabilities_json,"
            " status, error, created_at, started_at, finished_at,lifetime,source_request_id,payload_hash,projection_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.session_id,
                record.kind,
                record.skill,
                json.dumps(dict(record.inputs), ensure_ascii=False, default=str),
                record.runtime,
                record.target_alias,
                record.target_workdir,
                json.dumps(list(record.capabilities), ensure_ascii=False),
                record.status,
                record.error,
                record.created_at,
                record.started_at,
                record.finished_at,
                record.lifetime,
                record.source_request_id,
                record.payload_hash,
                projection_json,
            ),
        )

    def by_request(self, session_id: str, request_id: str) -> JobRecord | None:
        row = self._db.run(
            lambda conn: conn.execute(
                "SELECT "
                + _JOB_COLUMNS
                + " FROM jobs WHERE session_id=? AND source_request_id=?",
                (session_id, request_id),
            ).fetchone()
        )
        return _row_to_record(row) if row is not None else None

    def claim_job(self, record: JobRecord) -> tuple[JobRecord, bool]:
        """Atomically admit a canonical job in the original jobs table."""
        if not record.source_request_id:
            self.insert_job(record)
            return record, True

        def write(conn):
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT "
                + _JOB_COLUMNS
                + " FROM jobs WHERE session_id=? AND source_request_id=?",
                (record.session_id, record.source_request_id),
            ).fetchone()
            if row is not None:
                original = _row_to_record(row)
                if original.payload_hash != record.payload_hash:
                    raise JobError("job_payload_conflict", status_code=409)
                return original, False
            self._insert_job(conn, record)
            return record, True

        return self._db.run(write)

    def update_job(self, job_id: str, **changes: Any) -> None:
        immutable = {
            "target_alias",
            "target_workdir",
            "capabilities_json",
            "lifetime",
            "source_request_id",
            "payload_hash",
            "projection_json",
        }
        if immutable.intersection(changes):
            raise ValueError("target_snapshot_immutable")
        allowed = (
            "status",
            "error",
            "started_at",
            "finished_at",
            "session_id",
            "runtime",
        )
        columns = [name for name in changes if name in allowed]
        if not columns:
            return
        self._db.run(
            lambda conn: conn.execute(
                "UPDATE jobs SET "
                + ", ".join(c + " = ?" for c in columns)
                + " WHERE id = ?",
                (*(changes[c] for c in columns), job_id),
            )
        )

    def get_job(self, job_id: str) -> JobRecord | None:
        rows = self._db.run(
            lambda conn: conn.execute(
                "SELECT " + _JOB_COLUMNS + " FROM jobs WHERE id = ?",
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
                "SELECT "
                + _JOB_COLUMNS
                + " FROM jobs "
                + where
                + " ORDER BY created_at DESC, id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        )
        return [_row_to_record(row) for row in rows]

    def interrupt_orphans(self) -> int:
        """Mark every non-terminal **local** job ``interrupted``; start-up.

        A local job is an asyncio task of this process; nothing survives
        a restart, and a row still saying ``running`` would be a lie the
        event ring cannot even support, because the ring is in the same
        file as the row. A **remote** job (``runtime`` ``remote:<alias>``)
        is not this process's task — it is the host's or the scheduler's —
        so it is left non-terminal here for
        :meth:`JobsManager.reconcile_remote_jobs` to settle from its
        persisted handle, which is the whole reason that method exists.
        """
        return self._db.run(
            lambda conn: (
                conn.execute(
                    "UPDATE jobs SET status = 'interrupted',"
                    " error = 'backend restarted before this job finished',"
                    " finished_at = ? WHERE status IN ('queued', 'running',"
                    " 'cancel_requested') AND runtime = 'local' "
                    "AND lifetime IN ('','in_process')",
                    (time.time(),),
                ).rowcount
            )
        )

    def append_event(
        self, job_id: str, seq: int, event_type: str, payload: Mapping[str, Any]
    ) -> None:
        def _write(conn: Any) -> None:
            source_id = str(payload.get("source_event_id", ""))
            encoded = json.dumps(dict(payload), ensure_ascii=False, default=str)
            if source_id:
                count, size = conn.execute(
                    "SELECT COUNT(*),COALESCE(SUM(length(CAST(payload_json AS BLOB))),0) "
                    "FROM job_events WHERE job_id=? AND source_event_id != ''",
                    (job_id,),
                ).fetchone()
                if count >= 20_000 or size + len(encoded.encode()) > 4 * 1024 * 1024:
                    raise JobError("job_source_event_budget")
            conn.execute(
                "INSERT INTO job_events (job_id, seq, type, payload_json, created_at,source_event_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    seq,
                    event_type,
                    encoded,
                    time.time(),
                    source_id,
                ),
            )
            conn.execute(
                "DELETE FROM job_events WHERE job_id = ? AND seq <= ? AND source_event_id = ''",
                (job_id, seq - JOB_EVENTS_RETAINED),
            )

        self._db.run(_write)

    def source_event(self, job_id: str, source_id: str):
        return self._db.run(
            lambda conn: conn.execute(
                "SELECT seq,type,payload_json FROM job_events WHERE job_id=? AND source_event_id=?",
                (job_id, source_id),
            ).fetchone()
        )

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

    def earliest_seq(self, job_id: str) -> int:
        rows = self._db.run(
            lambda conn: conn.execute(
                "SELECT MIN(seq) AS seq FROM job_events WHERE job_id = ?", (job_id,)
            ).fetchall()
        )
        return int(rows[0]["seq"] or 0) if rows else 0


def _decode_capabilities(value: Any) -> tuple[str, ...]:
    try:
        decoded = json.loads(value or "[]")
    except (TypeError, ValueError):
        return ()
    if not isinstance(decoded, list):
        return ()
    return tuple(str(item) for item in decoded if isinstance(item, str) and item)


def _row_to_record(row: Any) -> JobRecord:
    if row["projection_bytes"] > _PROJECTION_BYTES:
        raise JobError("job_projection_too_large")
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
        # A row written before the column existed answers NULL, which
        # means the same thing "local" does — that job ran in the
        # process that wrote it.
        runtime=str(row["runtime"] or "local"),
        lifetime=str(row["lifetime"] or ""),
        source_request_id=str(row["source_request_id"] or ""),
        payload_hash=str(row["payload_hash"] or ""),
        projection=json.loads(row["projection_json"] or "{}"),
        target_alias=str(row["target_alias"] or ""),
        target_workdir=str(row["target_workdir"] or ""),
        capabilities=_decode_capabilities(row["capabilities_json"]),
        status=str(row["status"] or "queued"),
        error=str(row["error"] or ""),
        created_at=float(row["created_at"] or 0.0),
        started_at=float(row["started_at"]) if row["started_at"] else None,
        finished_at=float(row["finished_at"]) if row["finished_at"] else None,
    )


# ---- the vocabulary helpers ------------------------------------------------

# human_tool_description moved up to omicsclaw.entry.render (P6 item 1
# needed it on the chat approval frame too); imported above and kept in
# this module's __all__ so existing importers are unaffected.


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
                "summary": summary[:HUMAN_DESCRIPTION_CHARS],
                "arguments": request.arguments,
            },
        )
        future: asyncio.Future[ApprovalDecision] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[call_id] = future
        outcome = "abandoned"
        try:
            decision = (
                await future
                if self._timeout_s is None
                else await asyncio.wait_for(future, self._timeout_s)
            )
            outcome = "approve" if decision.approved else "deny"
            return decision
        except TimeoutError:
            outcome = "expired"
            return ApprovalDecision(
                approved=False, reason="no answer before the approval deadline"
            )
        finally:
            self._emit(
                "tool_approval_resolved", {"call_id": call_id, "decision": outcome}
            )
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
        observe: Callable[[str, Any], None] | None = None,
    ) -> None:
        self._record = record
        self._emit_raw = emit
        self.workspace = workspace
        self._observe = observe

    def observe_execution(self, reference: str, report: Any) -> None:
        if self._observe is not None:
            self._observe(reference, report)

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
                    human_description or human_tool_description(tool, arguments)
                )[:HUMAN_DESCRIPTION_CHARS],
            },
        )

    def tool_output(self, text: str) -> None:
        self._emit_raw("tool_output_chunk", {"text": str(text)[:_RESULT_SUMMARY_CHARS]})


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
            raise JobFailure(
                "skill " + repr(skill) + " is no longer loaded", phase="setup"
            )
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
            raise JobFailure(
                "skill " + repr(name) + " has no importable _api.py", phase="setup"
            )
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
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
                import anndata

                return anndata.read_h5ad(path)
            if suffix in (".csv", ".txt", ".tsv"):
                import pandas

                return pandas.read_csv(path, sep=None, engine="python")
        except ImportError as exc:
            raise JobFailure(
                "reading "
                + suffix
                + " needs a library this backend lacks: "
                + str(exc),
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
        except Exception as exc:
            raise JobFailure(type(exc).__name__ + ": " + str(exc), phase="run") from exc


class CodeRunRunner:
    """``kind="code_run"``: Python on the session's persistent kernel.

    P4's job face, C3's location riding beside P4's language. The
    payload is ``inputs.code`` (folded from the top level by the route)
    plus an optional ``inputs.adata_path`` naming an
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
        location = inputs.get("location", "local")
        if location is None:
            location = "local"
        if not isinstance(location, str):
            raise JobError("invalid_location")
        if location != "local" and not (
            location.startswith("remote:") and valid_host(location[len("remote:") :])
        ):
            raise JobError("invalid_location")
        if location != "local" and language != "python":
            # Remote kernels are python this round; the language seam
            # carries an IRkernel remote when that lands.
            raise JobError("remote_kernels_are_python_only")
        if adata_path and language != "python":
            # The h5ad bridge injects python (sc.read_h5ad); an R cell
            # cannot consume it — refuse at the door, not mid-flight.
            raise JobError("adata_bridge_is_python_only")
        if adata_path and location != "local":
            # The bridge reads a file on this machine into the kernel;
            # with the kernel remote the file is not where it reads.
            raise JobError("adata_bridge_is_local_only")

    async def __call__(self, record: JobRecord, ctx: JobExecutionContext) -> Any:
        from omicsclaw.kernel import KernelCallbacks, format_cell_summary

        manager = self._binding.manager
        code = str(record.inputs["code"])
        adata_path = record.inputs.get("adata_path")
        adata_path = adata_path.strip() if isinstance(adata_path, str) else None
        language = str(record.inputs.get("language", "python"))
        # The request's location is only used to create the immutable target
        # snapshot. Resumed jobs must follow the stored alias even if the
        # original input payload or host configuration has changed.
        if record.target_alias and "remote_kernel" in record.capabilities:
            location = "remote:" + record.target_alias
        else:
            # A pre-snapshot remote row must never be silently reinterpreted
            # as local execution after a restart.  The input is inspected
            # only to identify that legacy state; it never selects a target.
            requested_location = record.inputs.get("location")
            if isinstance(requested_location, str) and requested_location.startswith(
                "remote:"
            ):
                raise JobFailure(
                    "remote code job has no immutable target snapshot",
                    phase="setup",
                )
            location = "local"
        if location != "local" and getattr(manager, "remote_runtime", None) is None:
            # The door-check the chat face gets from its schema flag: a
            # job that asked for a remote kernel on a deployment without
            # the runtime fails at setup with the reason, not mid-flight
            # with a manager ValueError the wire cannot phrase.
            raise JobFailure(
                "remote kernel locations are not enabled on this"
                " deployment (no remote kernel runtime bound)",
                phase="setup",
            )
        session_id = record.session_id or ("job-" + record.id[:12])
        ctx.tool_started(
            "python" if language == "python" else "r",
            json.dumps({"code": code[:400], "location": location}, ensure_ascii=False),
        )
        self._protect(manager, session_id, record, language=language, location=location)

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
                location=location,
                callbacks=KernelCallbacks(
                    on_stream=_on_stream,
                    on_idle=_on_idle,
                    on_usage=_on_usage,
                ),
                adata_in=adata_path or None,
                job_id=record.id,
                target_workdir=record.target_workdir,
            )
        finally:
            self._unprotect(
                manager, session_id, record, language=language, location=location
            )
        state = manager.state_for(session_id, language=language, location=location)
        observed = await asyncio.to_thread(observe_kernel, state)
        if observed is not None:
            from dataclasses import asdict

            ctx.emit("kernel.observed", asdict(observed))
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
            raise JobFailure(
                "the kernel died mid-cell; retry to cold-start", phase="run"
            )
        ctx.tool_output(summary)
        await self._scan_artifacts(record, ctx)
        return summary

    def _protect(
        self,
        manager: Any,
        session_id: str,
        record: JobRecord,
        *,
        language: str = "python",
        location: str = "local",
    ) -> None:
        """Keep the reaper away while this job owns the session's kernel."""
        try:
            manager.protect(
                session_id, "job:" + record.id, language=language, location=location
            )
        except Exception:  # noqa: BLE001 - protection is best-effort
            _log.exception("kernel protection failed for job %s", record.id)

    def _unprotect(
        self,
        manager: Any,
        session_id: str,
        record: JobRecord,
        *,
        language: str = "python",
        location: str = "local",
    ) -> None:
        try:
            manager.unprotect(
                session_id, "job:" + record.id, language=language, location=location
            )
        except Exception:  # noqa: BLE001
            _log.debug(
                "could not release kernel protection for job %s",
                record.id,
                exc_info=True,
            )

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


class EnvInstallRunner:
    """``kind="env_install"``: provision one local Python environment.

    P5's job face, riding the CodeRunRunner shape: an injected seam
    (whatever answers the ``manage_environments`` Protocol — the
    deployment's :class:`~omicsclaw.entry.env_supply.EnvSupplyBinding`),
    a door-check ``validate``, and a ``__call__`` that speaks the P1
    vocabulary with no new event types. ``tool_started`` names the
    ``manage_environments`` surface it shares; the installer's step
    markers (``===OMICSCLAW_ENV_STEP:<name>===``) map onto ``progress``
    frames with honest phase-level percents — uv's own line format is
    nobody's contract — and its raw output rides ``tool_output_chunk``
    for the person watching the job panel.

    Pre-consented by construction: a job is a thing a person started on
    purpose, so the install runs without an approval card (the chat
    face's ``manage_environments`` asks; this face's approval was the
    POST). A runner with no binding behind it — the switch is off —
    answers ``env_install_unavailable`` (409), the
    ``remote_runtime_unavailable`` refusal copied for the local plane.
    """

    _STEP_PERCENT: Final = {
        "python": 20.0,
        "venv": 40.0,
        "packages": 75.0,
        "verify": 95.0,
    }
    """Phase-level percents; the phases between markers are uv's, not ours."""

    def __init__(self, env_supply: Any) -> None:
        self._supply = env_supply

    def validate(self, skill: str, inputs: Any) -> None:
        if self._supply is None:
            raise JobError("env_install_unavailable", status_code=409)
        env = inputs.get("env", "main") if isinstance(inputs, dict) else "main"
        if env is None:
            env = "main"
        if not isinstance(env, str) or not ENV_NAME_PATTERN.fullmatch(env):
            raise JobError("invalid_env")
        packages = inputs.get("packages", []) if isinstance(inputs, dict) else []
        if packages is None:
            packages = []
        if not isinstance(packages, list) or not all(
            isinstance(item, str) for item in packages
        ):
            raise JobError("invalid_packages")
        for item in packages:
            # the same closed alphabet the chat face's tool holds: one
            # implementation of the rule, shared through the schema
            # package so the two validators cannot drift (review n7)
            if item and not PACKAGE_NAME_PATTERN.fullmatch(item.strip()):
                raise JobError("invalid_packages")

    async def __call__(self, record: JobRecord, ctx: JobExecutionContext) -> Any:
        env = str(record.inputs.get("env") or "main")
        packages = [
            str(item).strip()
            for item in record.inputs.get("packages", [])
            if str(item).strip()
        ]
        ctx.tool_started(
            "manage_environments",
            json.dumps({"env": env, "packages": packages}, ensure_ascii=False),
            human_description=(
                "set up the "
                + env
                + " environment"
                + (" (+" + ", ".join(packages) + ")" if packages else "")
            ),
        )
        ctx.progress("provisioning the " + env + " environment", 5.0)

        def on_output(line: str) -> None:
            step = parse_env_step(line)
            if step is not None:
                ctx.progress(
                    "environment " + env + ": " + step,
                    self._STEP_PERCENT.get(step, 50.0),
                )
                return
            if line.strip():
                ctx.tool_output(line)

        try:
            report = await self._supply.install_environment(
                env=env, packages=tuple(packages), on_output=on_output
            )
        except Exception as exc:
            # The binding's EnvSupplyError carries the manual commands
            # in its message; letting it through to the manager's own
            # failure handling puts them on the ``job.failed`` frame the
            # person reads, which is the disclosure the remote plane set.
            raise JobFailure(str(exc), phase="run") from exc
        ctx.progress("environment " + env + " ready", 100.0)
        interpreter = str(report.get("interpreter", ""))
        ctx.tool_output(
            "environment ready: "
            + interpreter
            + "; installed: "
            + ", ".join(str(name) for name in report.get("packages", ()))
        )
        return {
            "env": env,
            "interpreter": interpreter,
            "packages": list(report.get("packages", ())),
        }


def _summarize_result(result: Any) -> str:
    if result is None:
        return "done"
    try:
        import anndata

        if isinstance(result, anndata.AnnData):
            return (
                "AnnData: "
                + str(result.n_obs)
                + " obs x "
                + str(result.n_vars)
                + " vars"
            )
    except Exception:  # noqa: BLE001 - summarising must never fail a finished job
        _log.debug("AnnData summary unavailable", exc_info=True)
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
    because the tasks that were running them are gone — except remote
    jobs, whose work never was a task of this process; those wait for
    :meth:`reconcile_remote_jobs`, and a manager constructed with a
    *remote_runtime* bridge is the manager that has one.
    """

    def __init__(
        self,
        app: Any,
        *,
        store: JobStore | None = None,
        artifact_store: ArtifactStore | None = None,
        default_timeout_s: float = DEFAULT_JOB_TIMEOUT_S,
        max_concurrent: int = MAX_CONCURRENT_JOBS,
        remote_runtime: RemoteJobsBridge | None = None,
        kernels: Any = None,
        env_supply: Any = None,
        run_event_sink: Any = None,
        register_default_runners: bool = True,
    ) -> None:
        self._app = app
        if store is None:
            workspace = Path(app.config.workspace)
            store = JobStore(Database(workspace / ".omicsclaw" / "jobs.db"))
        self.store = store
        run_db = Path(app.config.workspace) / ".omicsclaw" / "runs.db"
        run_db.parent.mkdir(parents=True, exist_ok=True)
        telemetry = getattr(app, "telemetry", None)
        sink_factory = getattr(telemetry, "run_event_sink", None)
        sink = (
            run_event_sink
            if run_event_sink is not None
            else (sink_factory() if callable(sink_factory) else None)
        )
        self.run_store = RunStore(str(run_db), on_event=sink)
        self._run_ids: dict[str, str] = {}
        self._run_job_ids: dict[str, str] = {}
        # An observation authorizes only this process's current projection.
        # It is never restored from cached events or supplied by event payloads.
        self._external_observations: dict[str, tuple[str, str, int | None]] = {}
        for persisted in self.run_store.list("job"):
            if (
                persisted.execution_ref
                and persisted.run_id
                and persisted.execution_ref not in self._run_ids
            ):
                self._run_ids[persisted.execution_ref] = persisted.run_id
                self._run_job_ids[persisted.execution_ref] = persisted.job_id
        self.artifacts = artifact_store or ArtifactStore(store.database)
        self.default_timeout_s = float(default_timeout_s)
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent))
        self._runners: dict[str, Any] = {}
        self._live: dict[str, _LiveJob] = {}
        self._cancel_tasks: set[asyncio.Task] = set()
        self._seq: dict[str, int] = {}
        self._remote_runtime = remote_runtime
        self._reconcile_lock = asyncio.Lock()
        self._approval_timeout_s = getattr(app.config, "approval_timeout_s", None)
        try:
            interrupted = self.store.interrupt_orphans()
        except Exception:  # noqa: BLE001 - a fresh store must not kill the server
            interrupted = 0
        if interrupted:
            _log.info("marked %d orphaned job(s) interrupted at start-up", interrupted)
        for record in self.store.list_jobs(limit=10000):
            if record.runtime == "local" and record.status in TERMINAL_JOB_STATUSES:
                self._sync_run_status(record.id, record.status)
        if register_default_runners:
            self.register_runner(
                "skill_run", SkillApiRunner(app, artifacts=self.artifacts)
            )
        # P4's code_run rides the same lifecycle on the deployment's
        # persistent kernels: the binding build_app mounted behind the
        # ``python`` tool when there is one (one manager, shared session
        # kernels between the faces), else a private one for this manager.
        if kernels is None:
            kernels = getattr(app, "kernels", None)
        if kernels is None and register_default_runners:
            from ..assembly import WorkspaceKernelBinding

            kernels = WorkspaceKernelBinding(app.config.workspace)
        self.kernels = kernels
        if register_default_runners:
            self.register_runner(
                "code_run",
                CodeRunRunner(kernels, artifact_store=self.artifacts),
            )
        # P5's env_install rides the same lifecycle over the deployment's
        # environment supply: the binding build_app mounted behind
        # ``manage_environments`` when the switch is on, else None — and
        # a None binding still registers, so the kind is refused with
        # its own code (409) rather than a generic unknown_kind that
        # would read as "this backend is old".
        if env_supply is None:
            env_supply = getattr(app, "env_supply", None)
        if register_default_runners:
            self.register_runner("env_install", EnvInstallRunner(env_supply))

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
        return self.store.list_jobs(session_id=session_id, status=status, limit=limit)

    # ---- lifecycle ----

    async def refresh_projections(self) -> None:
        await self.reconcile_remote_jobs()
        sessions = getattr(self._app, "sessions", None)
        plans = getattr(self._app, "plans", None)
        if sessions is not None:
            for session in await sessions.list_sessions(limit=500):
                ensure_session_run(self.run_store, session.session_id, plans=plans)
        for run in self.run_store.list("run"):
            owner_id = next(
                (
                    job_id
                    for job_id, run_id in self._run_ids.items()
                    if run_id == run.run_id
                ),
                None,
            )
            owner = self.store.get_job(owner_id) if owner_id else None
            if owner is not None and owner.source_request_id:
                continue
            plan_id, steps, active = plan_snapshot(plans, run.session_id)
            run.plan_id, run.plan_steps = plan_id, steps
            if run.origin == "legacy":
                run.step_id = active
            self.run_store.save(run)
        manager = getattr(self.kernels, "manager", None)
        observed = set()
        observations = {}
        if manager is not None and hasattr(manager, "state_for"):
            for record in self.store.list_jobs(limit=10000):
                if record.kind != "code_run" or record.id not in self._run_ids:
                    continue
                session_id = record.session_id or ("job-" + record.id[:12])
                language = str(record.inputs.get("language", "python"))
                location = (
                    "remote:" + record.target_alias if record.target_alias else "local"
                )
                state = manager.state_for(
                    session_id, language=language, location=location
                )
                key = (session_id, language, location)
                if key not in observations:
                    observations[key] = await asyncio.to_thread(observe_kernel, state)
                kernel = observations[key]
                if kernel is not None:
                    projected_job = self.run_store.load(
                        self._run_job_ids.get(record.id, "")
                    )
                    if projected_job is None or (
                        projected_job.kernel_id
                        and projected_job.kernel_id != kernel.kernel_id
                    ):
                        continue
                    if not projected_job.kernel_id and record.id not in self._live:
                        continue
                    bind_kernel(
                        self.run_store, kernel, self._run_ids[record.id], record.id
                    )
                    observed.add(kernel.kernel_id)
        for kernel in self.run_store.list("kernel"):
            if kernel.kernel_id not in observed and kernel.status != "lost":
                # Lost describes our control handle, not proof the process died.
                kernel.status = "lost"
                self.run_store.save(kernel)
        resolved = {
            a.payload.get("call_id")
            for a in self.run_store.list("activity")
            if a.event_type == "tool_approval_resolved"
        }
        for activity in self.run_store.list("activity"):
            if (
                activity.event_type != "tool_approval_request"
                or activity.payload.get("call_id") in resolved
            ):
                continue
            live = self._live.get(activity.job_id)
            if live is None or live.task is None or live.task.done():
                self._emit(
                    activity.job_id,
                    "tool_approval_resolved",
                    {
                        "call_id": activity.payload.get("call_id"),
                        "decision": "unavailable",
                        "reason": "backend no longer owns this approval",
                    },
                )

    def _save_verification(self, result: Verification) -> None:
        """Record a supplied reviewer verdict; this is not a machine check."""
        self.run_store.save(result, event="finished")

    def _run_verification(self, check, verify, **refs):
        """Persist the attempt before invoking the verifier, even if it crashes."""
        started = Verification(check=check, **refs)
        self.run_store.save(started, event="started")
        try:
            result = verify()
        except Exception as exc:  # noqa: BLE001 - verifier failure is persisted evidence
            _log.exception("verification failed to execute: check=%s", check)
            result = Verification(check=check, verdict="inconclusive", details=str(exc))
        result.verification_id = started.verification_id
        result.created_at = started.created_at
        for key, value in refs.items():
            setattr(result, key, value)
        self.run_store.save(result, event="finished")
        return result

    async def create_job(
        self,
        *,
        kind: str = "skill_run",
        skill: str = "",
        inputs: Any = None,
        session_id: str = "",
        workspace: str = "",
        runtime: str = "local",
        source_request_id: str = "",
        lifetime: str = "",
    ) -> JobRecord:
        """Validate, persist and start one job. Returns its record.

        *runtime* is C2's routing field: ``"local"`` (the default, and
        the only form before the field existed) runs the kind's runner;
        ``"remote:<alias>"`` runs the remote bridge, which submits
        ``inputs.command`` on that SSH alias and streams its log back as
        vocabulary events. A remote runtime with no bridge bound is
        ``remote_runtime_unavailable`` (409) rather than a silent local
        fallback — a client that asked for a GPU node and got this
        process would be worse than a client that got an error.

        :raises JobError: the kind is unknown, the runtime is malformed,
        no remote bridge is bound, or the runner ``validate`` refused the
        skill or inputs.
        """
        if inputs is None:
            inputs = {}
        if not isinstance(inputs, dict):
            raise JobError("inputs_must_be_object")
        configured_runner = self._runners.get(kind)
        canonical_inputs = getattr(configured_runner, "canonical_inputs", None)
        if callable(canonical_inputs):
            inputs = await asyncio.to_thread(canonical_inputs, skill, inputs)
        lifetime = lifetime or (
            getattr(configured_runner, "lifetime", "in_process")
            if runtime == "local"
            else "external"
        )
        if not isinstance(lifetime, str) or lifetime not in {"in_process", "external"}:
            raise JobError("invalid_job_lifetime")
        if (
            not isinstance(source_request_id, str)
            or source_request_id
            and not re.fullmatch(r"[A-Za-z0-9:._-]{1,256}", source_request_id)
        ):
            raise JobError("invalid_job_request_id")
        declared_workspace = str(getattr(self._app.config, "workspace", ""))
        if workspace and workspace.strip():
            try:
                same = (
                    Path(workspace.strip()).expanduser().resolve()
                    == Path(declared_workspace).expanduser().resolve()
                )
            except (ValueError, OSError, RuntimeError):
                same = False
            if not same:
                raise JobError(
                    "workspace_does_not_match_backend_runtime", status_code=409
                )
        payload_hash = ""
        if source_request_id:
            try:
                payload = json.dumps(
                    {
                        "kind": kind,
                        "skill": str(skill or ""),
                        "inputs": inputs,
                        "session_id": str(session_id or ""),
                        "runtime": runtime,
                        "lifetime": lifetime,
                        "workspace": str(Path(self._app.config.workspace).resolve()),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode()
            except (ValueError, TypeError) as exc:
                raise JobError("invalid_job_payload") from exc
            if len(payload) > 2 * 1024 * 1024:
                raise JobError("job_payload_too_large")
            payload_hash = hashlib.sha256(payload).hexdigest()
            original = self.store.by_request(str(session_id or ""), source_request_id)
            if original is not None:
                if original.payload_hash != payload_hash:
                    raise JobError("job_payload_conflict", status_code=409)
                self._ensure_job_projection(original)
                return original
        runner = self._runners.get(kind)
        if runner is None:
            raise JobError("unknown_kind")
        if runtime != "local":
            alias = runtime[len("remote:") :] if runtime.startswith("remote:") else ""
            if not alias or not valid_host(alias):
                raise JobError("invalid_runtime")
            if self._remote_runtime is None:
                raise JobError("remote_runtime_unavailable", status_code=409)
        validate = (
            self._remote_runtime.validate
            if runtime != "local"
            and self._remote_runtime is not None
            and not getattr(runner, "binds_scientific_inputs", False)
            else getattr(runner, "validate", None)
        )
        if callable(validate):
            try:
                validate(skill, inputs)
            except JobError:
                raise
            except ValueError as exc:
                # The remote bridge validates with plain ValueErrors —
                # it cannot import this module's error type without a
                # dependency pointing the wrong way — so the boundary
                # where the wire's vocabulary lives is where they become
                # JobErrors, at 422 like every other refused request.
                raise JobError(str(exc)) from exc
        # Resolve the target before inserting the row.  The resolver probes
        # the selected host once and returns the exact scratch/work directory
        # and capability set that both the local job row and the remote handle
        # must carry for the rest of the job's life.
        target_runtime = runtime
        if target_runtime == "local" and kind == "code_run":
            location = inputs.get("location")
            if isinstance(location, str) and location.startswith("remote:"):
                target_runtime = location
        is_remote_target = target_runtime.startswith("remote:")
        target_caps = (
            ("remote_kernel",)
            if kind == "code_run" and is_remote_target
            else ("remote_exec", "remote_submit")
            if is_remote_target
            else ()
        )
        target_snapshot = None
        if is_remote_target:
            if self._remote_runtime is None:
                # Keep the existing setup-phase error for deployments with
                # C3 disabled; no row can be created without a target
                # snapshot, so there is nothing to resume or execute.
                if kind == "code_run":
                    raise JobError("remote_runtime_unavailable", status_code=409)
                raise JobError("remote_runtime_unavailable", status_code=409)
            try:
                target_snapshot = await self._remote_runtime.resolve_target(
                    target_runtime[len("remote:") :], capabilities=target_caps
                )
            except JobError:
                raise
            except Exception as exc:
                _log.debug("target resolution failed", exc_info=True)
                raise JobError("target_resolution_failed", status_code=409) from exc
        elif lifetime == "external" and getattr(runner, "target_alias", ""):
            target_snapshot = await runner.resolve_target(runner.target_alias)
        if runtime != "local":
            # The query-time half of C2's re-estimation: before adding a
            # job to the in-flight set, ask the ones already in it what
            # they are doing, so a backend that was away while jobs ran
            # catches up on the same request that proves it has a working
            # remote runtime. Guarded because reconciliation is
            # housekeeping, not a precondition — a host that will not
            # answer must not block a submission to a different one.
            try:
                await self.reconcile_remote_jobs()
            except Exception:  # noqa: BLE001 - reconcile is best-effort here
                _log.debug("pre-create remote reconcile failed", exc_info=True)
        job_id = secrets.token_hex(16)
        plan_id, plan_steps, step_id = plan_snapshot(
            getattr(self._app, "plans", None), str(session_id or "")
        )
        run = Run(
            run_id="run_job_" + job_id,
            session_id=str(session_id or ""),
            plan_id=plan_id,
            plan_steps=plan_steps,
            step_id=step_id,
            execution_location=target_runtime if kind == "code_run" else runtime,
        )
        projection = {
            "run_id": run.run_id,
            "plan_id": run.plan_id,
            "plan_steps": run.plan_steps,
            "step_id": run.step_id,
            "created_at": run.created_at,
            "execution_location": run.execution_location,
        }
        projection_bytes = json.dumps(
            projection,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        if len(projection_bytes) > _PROJECTION_BYTES - 128:
            raise JobError("job_projection_too_large")
        projection = {
            **projection,
            "schema_version": 1,
            "sha256": hashlib.sha256(projection_bytes).hexdigest(),
        }
        record = JobRecord(
            id=job_id,
            session_id=str(session_id or ""),
            kind=kind,
            skill=str(skill or ""),
            inputs=inputs,
            runtime=runtime,
            lifetime=lifetime,
            source_request_id=source_request_id,
            payload_hash=payload_hash,
            projection=projection,
            target_alias=target_snapshot.alias if target_snapshot is not None else "",
            target_workdir=(
                target_snapshot.workdir if target_snapshot is not None else ""
            ),
            capabilities=(
                target_snapshot.capabilities if target_snapshot is not None else ()
            ),
            status="queued",
            created_at=time.time(),
        )
        record, newly_claimed = self.store.claim_job(record)
        self._ensure_job_projection(record)
        if not newly_claimed:
            return record
        live = _LiveJob(task=None, gate=self._make_gate(record.id))
        self._live[record.id] = live
        live.task = asyncio.create_task(
            self._run(record.id), name="omicsclaw-job-" + record.id
        )
        return record

    def _ensure_job_projection(self, record: JobRecord) -> None:
        """Repair the original job-to-run links without admitting execution."""
        snapshot = dict(record.projection)
        if record.source_request_id and not snapshot:
            raise JobError("job_projection_conflict", status_code=409)
        if snapshot:
            version = snapshot.pop("schema_version", None)
            digest = snapshot.pop("sha256", None)
            raw = json.dumps(
                snapshot,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
            if version != 1 or digest != hashlib.sha256(raw).hexdigest():
                raise JobError("job_projection_conflict", status_code=409)
        run_id = (
            self._run_ids.get(record.id)
            or snapshot.get("run_id")
            or "run_job_" + record.id
        )
        run = self.run_store.load(run_id)
        if (
            run is not None
            and snapshot
            and (
                not isinstance(run, Run)
                or run.run_id != snapshot["run_id"]
                or run.session_id != record.session_id
                or any(
                    getattr(run, name) != snapshot[name]
                    for name in (
                        "plan_id",
                        "plan_steps",
                        "step_id",
                        "created_at",
                        "execution_location",
                    )
                )
            )
        ):
            raise JobError("job_projection_conflict", status_code=409)
        if run is None:
            allowed = {
                name: snapshot[name]
                for name in (
                    "plan_id",
                    "plan_steps",
                    "step_id",
                    "created_at",
                    "execution_location",
                )
                if name in snapshot
            }
            run = Run(run_id=run_id, session_id=record.session_id, **allowed)
            if not snapshot:
                run.execution_location = record.runtime
            self.run_store.save(run, if_absent=True)
        self._run_ids[record.id] = run_id
        projection_id = (
            self._run_job_ids.get(record.id) or "job_projection_" + record.id
        )
        existing_job = self.run_store.load(projection_id)
        if (
            existing_job is not None
            and snapshot
            and (
                not isinstance(existing_job, RunJob)
                or existing_job.run_id != run_id
                or existing_job.execution_ref != record.id
                or existing_job.host_id != record.target_alias
                or existing_job.scheduler != record.runtime
                or existing_job.command_digest != record.payload_hash
                or existing_job.step_id != run.step_id
            )
        ):
            raise JobError("job_projection_conflict", status_code=409)
        if existing_job is None:
            self.run_store.save(
                RunJob(
                    job_id=projection_id,
                    run_id=run_id,
                    host_id=record.target_alias,
                    scheduler=record.runtime,
                    execution_ref=record.id,
                    step_id=run.step_id,
                    command_digest=record.payload_hash,
                ),
                if_absent=True,
            )
        self._run_job_ids[record.id] = projection_id
        if self.store.latest_seq(record.id) == 0:
            self._emit(
                record.id,
                "job.created",
                {
                    "job_id": record.id,
                    "kind": record.kind,
                    "skill": record.skill,
                    "runtime": record.runtime,
                },
            )

    async def reconcile_remote_jobs(self) -> dict[str, int]:
        """Re-estimate every in-flight remote handle; move local rows with it.

        The start-up/query-time half of C2. The remote rows are asked
        through the plane (unreachable hosts answer ``unknown`` and keep
        their rows); a job the desktop started gets its local row settled
        too, which is what makes "the backend restarted mid-job" mean
        "the status caught up" instead of "interrupted, and the remote
        job's eventual finish is never recorded".

        A no-op answer, not an error, when no bridge is bound: there are
        no remote rows to ask about.
        """
        if self._remote_runtime is None:
            return {"checked": 0, "still_in_flight": 0, "terminal": 0, "unknown": 0}
        from omicsclaw.remote.jobs import has_identity_proof, verified_state

        def on_status(local_job_id: str, alias: str, state: str) -> None:
            record = self.store.get_job(local_job_id)
            if record is None:
                return
            remote = self._remote_runtime.plane.job_store().by_local_job(local_job_id)
            if remote is None or not has_identity_proof(remote.handle()):
                state = "unknown"
            status = {"done": "succeeded", "pending": "queued"}.get(state, state)
            if record.status in TERMINAL_JOB_STATUSES:
                return
            if (
                record.status == "cancel_requested"
                and status not in TERMINAL_JOB_STATUSES
            ):
                return
            if record.status != status:
                changes: dict[str, Any] = {"status": status}
                if status in TERMINAL_JOB_STATUSES:
                    changes["finished_at"] = time.time()
                self.store.update_job(local_job_id, **changes)
            self._sync_run_status(local_job_id, status)

        async with self._reconcile_lock:
            report = await refresh_in_flight(
                self._remote_runtime.plane, on_remote_status=on_status
            )
            # A previous process may have persisted the remote result before
            # updating the desktop projection. Repair that window on restart.
            remote_store = self._remote_runtime.plane.job_store()
            for record in self.store.list_jobs(limit=10000):
                if record.lifetime == "external" or record.runtime.startswith(
                    "remote:"
                ):
                    row = remote_store.by_local_job(record.id)
                    if row is not None:
                        self._external_observations.pop(record.id, None)
                        if not has_identity_proof(row.handle()):
                            if record.status not in TERMINAL_JOB_STATUSES:
                                on_status(record.id, row.host_alias, "unknown")
                            continue
                        try:
                            self._external_observations.pop(record.id, None)
                            observation = await self._remote_runtime.plane.executor_for(
                                row.host_alias
                            ).status(row.handle())
                        except OSError:
                            observation = None
                        state = verified_state(row.handle(), observation)
                        self._observe_external_fact(record.id, str(row.id), observation)
                        on_status(record.id, row.host_alias, state)
                        live = self._live.get(record.id)
                        if state in {"done", "failed", "canceled"} and (
                            live is None or live.task is None or live.task.done()
                        ):
                            await self._recover_remote_evidence(
                                record, row, observation
                            )
                            self._emit_terminal_once(
                                record.id,
                                {"done": "succeeded"}.get(state, state),
                            )
                        self._external_observations.pop(record.id, None)
            return report

    async def _recover_remote_evidence(self, record, row, observation):
        from omicsclaw.remote.bridge import remote_output_payloads
        from omicsclaw.remote.jobs import verified_state

        if verified_state(row.handle(), observation) not in {
            "done",
            "failed",
            "canceled",
        }:
            return
        self._observe_external_fact(record.id, str(row.id), observation)
        verification = self.run_store.load("verification:exit:" + record.id)
        if verification is None or verification.verdict == "inconclusive":
            code = observation.exit_code
            if verification is None or code is not None:
                self._emit(
                    record.id,
                    "execution.finished",
                    {
                        "exit_code": code,
                        "evidence_ref": "remote-job:" + str(row.id),
                        "identity_digest": observation.identity_digest,
                    },
                )
        if observation.state == "done":
            runner = self._runners.get(record.kind)
            recover = getattr(runner, "recover_evidence", None)
            if callable(recover):
                await recover(
                    record,
                    row,
                    JobExecutionContext(
                        record,
                        lambda kind, value: self._emit(record.id, kind, value),
                        Path(self._app.config.workspace),
                    ),
                )
                return
            for payload in remote_output_payloads(
                row.host_alias, row.workdir, record.inputs.get("outputs", [])
            ):
                self._emit(record.id, "artifact.created", payload)

    def _sync_run_status(self, job_id: str, status: str) -> None:
        run_id = self._run_ids.get(job_id)
        if not run_id:
            return
        status = {"canceled": "cancelled", "cancel_requested": "running"}.get(
            status, status
        )
        ids = [self._run_job_ids.get(job_id)]

        ids.append(run_id)
        for ident in ids:
            obj = self.run_store.load(ident) if ident else None
            if obj is None or obj.status == status:
                continue
            # Recovery can observe a terminal result without seeing running.
            if obj.status in {
                "created",
                "queued",
                "unknown",
                "lost",
                "interrupted",
            } and status in {
                "succeeded",
                "failed",
                "cancelled",
                "interrupted",
                "unknown",
            }:
                obj.status = "running"
                self.run_store.save(obj)
            if obj.status in {"succeeded", "failed", "cancelled"}:
                continue
            obj.status = status
            self.run_store.save(obj)

    async def cancel_job(self, job_id: str) -> str:
        """Cancel a live task or a remote handle recovered after restart."""
        record = self.store.get_job(job_id)
        if record is None or record.lifetime != "external":
            return self.cancel(job_id)
        if record.status in TERMINAL_JOB_STATUSES:
            return "finished"
        if self._remote_runtime is None:
            raise JobError("remote_runtime_unavailable", status_code=409)
        async with self._reconcile_lock:
            self._external_observations.pop(job_id, None)
            plane = self._remote_runtime.plane
            row = plane.job_store().by_local_job(job_id)
            if row is None:
                self.store.update_job(job_id, status="cancel_requested")
                return "cancel_requested"
            from omicsclaw.remote.jobs import has_identity_proof, verified_state

            if not has_identity_proof(row.handle()):
                self.store.update_job(job_id, status="cancel_requested")
                return "cancel_requested"
            self._external_observations.pop(job_id, None)
            report = await plane.cancel(str(row.id))
            self._observe_external_fact(job_id, str(row.id), report)
            if verified_state(row.handle(), report) in {"canceled", "done", "failed"}:
                status = {"done": "succeeded"}.get(report.state, report.state)
                self._emit(
                    job_id,
                    "execution.finished",
                    {
                        "exit_code": report.exit_code,
                        "evidence_ref": "remote-job:" + str(row.id),
                        "identity_digest": report.identity_digest,
                    },
                )
                self.store.update_job(job_id, status=status, finished_at=time.time())
                self._sync_run_status(job_id, status)
                self._emit_terminal_once(job_id, status)
                self._external_observations.pop(job_id, None)
                return "canceled" if status == "canceled" else "finished"
            self.store.update_job(job_id, status="cancel_requested")
            self._external_observations.pop(job_id, None)
            return "cancel_requested"

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
        if record.lifetime == "external" and record.status not in TERMINAL_JOB_STATUSES:
            self.store.update_job(job_id, status="cancel_requested")
            task = asyncio.create_task(
                self.cancel_job(job_id), name="omicsclaw-explicit-cancel-" + job_id
            )
            self._cancel_tasks.add(task)
            task.add_done_callback(self._cancel_finished)
            return "cancel_requested"
        live = self._live.get(job_id)
        if live is None or record.status in TERMINAL_JOB_STATUSES:
            return "finished"
        live.cancelled = True
        self.store.update_job(job_id, status="cancel_requested")
        if live.task is not None and not live.task.done():
            live.task.cancel()
        return "canceled"

    async def shutdown_observations(self) -> None:
        """Detach controller-owned observers without cancelling external work."""
        tasks = [
            live.task
            for live in self._live.values()
            if live.task is not None and not live.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=3.0)
            if pending:
                raise RuntimeError("job_observation_shutdown_unconfirmed")
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._cancel_tasks:
            _, pending = await asyncio.wait(self._cancel_tasks, timeout=3.0)
            if pending:
                raise RuntimeError("job_explicit_cancel_shutdown_unconfirmed")

    def _cancel_finished(self, task: asyncio.Task) -> None:
        self._cancel_tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            _log.warning("explicit job cancel unconfirmed: %s", type(error).__name__)

    def _emit_terminal_once(self, job_id: str, status: str, payload=None) -> None:
        found = self.store.database.run(
            lambda conn: conn.execute(
                "SELECT 1 FROM job_events WHERE job_id=? AND type IN ('job.done','job.failed') LIMIT 1",
                (job_id,),
            ).fetchone()
        )
        if found is None:
            self._emit(
                job_id,
                "job.failed" if status == "failed" else "job.done",
                payload or {"status": status},
            )

    def _detach_external_observation(self, job_id: str, reason: str) -> None:
        current = self.store.get_job(job_id)
        if current is None or current.status in TERMINAL_JOB_STATUSES:
            return
        status = (
            "cancel_requested" if current.status == "cancel_requested" else "unknown"
        )
        self.store.update_job(job_id, status=status, error=reason)
        self._emit(
            job_id, "job.observation_stopped", {"reason": reason, "status": status}
        )

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
        owner = self.store.get_job(job_id)
        if (
            owner is not None
            and owner.lifetime == "external"
            and event_type
            in {"execution.finished", "artifact.created", "job.done", "job.failed"}
        ):
            from omicsclaw.remote.jobs import has_identity_proof

            row = (
                self._remote_runtime.plane.job_store().by_local_job(job_id)
                if self._remote_runtime
                else None
            )
            observed = self._external_observations.get(job_id)
            if (
                row is None
                or not has_identity_proof(row.handle())
                or observed is None
                or observed[0] != row.handle().proof_digest()
                or observed[1] not in {"done", "failed", "canceled"}
                or type(observed[2]) is not int
            ):
                raise JobError("external_observation_unconfirmed")
            if event_type == "execution.finished" and (
                payload.get("identity_digest") != observed[0]
                or type(payload.get("exit_code")) is not int
                or payload["exit_code"] != observed[2]
            ):
                raise JobError("external_exit_unconfirmed")
            if event_type == "artifact.created" and (
                observed[1] != "done" or observed[2] != 0
            ):
                raise JobError("external_artifact_unconfirmed")
        source_id = payload.get("source_event_id", "")
        if source_id:
            if not isinstance(source_id, str) or not re.fullmatch(
                r"[A-Za-z0-9:._-]{1,256}", source_id
            ):
                raise JobError("invalid_job_source_event")
            previous = self.store.source_event(job_id, source_id)
            if previous is not None:
                if (
                    previous["type"] != event_type
                    or json.loads(previous["payload_json"]) != payload
                ):
                    raise JobError("job_source_event_conflict", status_code=409)
                return int(previous["seq"])
        run_id = self._run_ids.get(job_id)
        if run_id and event_type == "kernel.observed":
            bind_kernel(self.run_store, Kernel(**payload), run_id, job_id)
        if run_id and event_type == "execution.finished":
            previous = self.run_store.load("verification:exit:" + job_id)
            if previous is not None:
                expected = str(payload.get("exit_code"))
                if previous.evidence_ref == expected:
                    return self.store.latest_seq(job_id)
                if previous.verdict != "inconclusive":
                    raise JobError("job_exit_evidence_conflict", status_code=409)
            self._run_verification(
                "exit_code",
                lambda: verify_exit_code(payload.get("exit_code")),
                verification_id="verification:exit:" + job_id,
                run_id=run_id,
                job_id=job_id,
                details=str(payload.get("evidence_ref", "")),
            )
        if run_id and event_type == "artifact.created":
            path = str(payload.get("path") or payload.get("remote_ref") or "")
            if any(
                a.job_id == job_id and a.uri == path
                for a in self.run_store.list("artifact")
            ):
                return self.store.latest_seq(job_id)
            record = self.store.get_job(job_id)
            inputs = dict(record.inputs) if record else {}
            code = str(inputs.get("code") or inputs.get("command") or "")
            import hashlib

            code_ref = str(
                payload.get("code_ref")
                or (
                    "sha256:" + hashlib.sha256(code.encode()).hexdigest()
                    if code
                    else ""
                )
            )
            host_id = str(
                payload.get("host_id") or (record.target_alias if record else "")
            )
            kernels = [
                k for k in self.run_store.list("kernel") if belongs_to_run(k, run_id)
            ]
            kernel_id = str(
                payload.get("kernel_id") or (kernels[0].kernel_id if kernels else "")
            )
            artifact_id = str(payload.get("artifact_id", "")) or None
            logical_output = str(payload.get("logical_output") or path)
            scope = (
                record.session_id
                if record and record.session_id
                else json.dumps(
                    [
                        record.kind if record else "",
                        code_ref,
                        str(self._app.config.workspace),
                        inputs.get("inputs", []),
                    ],
                    sort_keys=True,
                )
            )
            lineage_key = hashlib.sha256(
                json.dumps(
                    [scope, host_id, logical_output], separators=(",", ":")
                ).encode()
            ).hexdigest()
            prior = [
                a
                for a in self.run_store.list("artifact")
                if a.lineage_key == lineage_key
            ]
            prior.sort(key=lambda a: a.version)
            version = max((a.version for a in prior), default=0) + 1
            parent_id = prior[-1].artifact_id if prior else None
            versioned_id = (
                (artifact_id if version == 1 else f"{artifact_id}:v{version}")
                if artifact_id
                else None
            )
            artifact = build_manifest(
                path,
                run_id=run_id,
                artifact_id=versioned_id,
                version=version,
                parent_artifact_id=parent_id,
                job_id=job_id,
                host_id=host_id,
                kernel_id=kernel_id,
                code_ref=code_ref,
                environment_ref=str(
                    payload.get("environment_ref")
                    or inputs.get("environment_ref")
                    or inputs.get("env")
                    or (kernels[0].environment if kernels else "")
                ),
                input_artifact_ids=payload.get("input_artifact_ids")
                or inputs.get("input_artifact_ids", []),
            )
            artifact.lineage_key = lineage_key
            if path.startswith("remote://") and payload.get("verified_sha256"):
                artifact.checksum = str(payload["verified_sha256"])
                artifact.size = int(payload["verified_bytes"])
            artifact.execution_ref = job_id
            self.run_store.save(artifact)
            if path:
                refs = {
                    "artifact_id": artifact.artifact_id,
                    "artifact_version": artifact.version,
                    "run_id": run_id,
                    "job_id": job_id,
                }
                self._run_verification(
                    "checksum" if artifact.checksum else "file_exists",
                    lambda: verify_file(path, artifact.checksum or None),
                    **refs,
                )
                if Path(path).suffix.lower() in {".json", ".csv", ".h5ad"}:
                    schema = payload.get("schema") or inputs.get("output_schema", {})
                    self._run_verification(
                        Path(path).suffix.lower()[1:] + "_schema",
                        lambda: verify_artifact(path, schema=schema),
                        **refs,
                    )
        seq = self._seq.get(job_id, self.store.latest_seq(job_id)) + 1
        self._seq[job_id] = seq
        self.store.append_event(job_id, seq, event_type, payload)
        if run_id:
            self.run_store.save(
                Activity(
                    activity_id=f"activity:{job_id}:{seq}",
                    run_id=run_id,
                    job_id=job_id,
                    event_type=event_type,
                    payload=dict(payload),
                )
            )
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

    def _observe_external_fact(self, job_id: str, reference: str, report: Any) -> None:
        from omicsclaw.remote.jobs import has_identity_proof, verified_state

        row = (
            self._remote_runtime.plane.job_store().get(reference)
            if self._remote_runtime
            else None
        )
        record = self.store.get_job(job_id)
        self._external_observations.pop(job_id, None)
        if (
            row is None
            or record is None
            or row.local_job_id != job_id
            or not has_identity_proof(row.handle())
        ):
            raise JobError("external_observation_unconfirmed")
        if record.target_alias and record.target_alias != row.host_alias:
            raise JobError("external_observation_target_conflict")
        if verified_state(row.handle(), report) == "unknown":
            return
        self._external_observations[job_id] = (
            report.identity_digest,
            report.state,
            report.exit_code,
        )
        if (
            report.state == "running"
            and record.status not in TERMINAL_JOB_STATUSES
            and record.status != "cancel_requested"
        ):
            self.store.update_job(
                job_id, status="running", started_at=record.started_at or time.time()
            )
            self._sync_run_status(job_id, "running")
            if not any(
                kind == "job.started"
                for _, kind, _ in self.store.events_after(job_id, 0)
            ):
                self._emit(
                    job_id, "job.started", {"job_id": job_id, "evidence_ref": reference}
                )

    async def _run(self, job_id: str) -> None:
        record = self.store.get_job(job_id)
        live = self._live.get(job_id)
        if record is None or live is None:
            return
        # C2's dispatch: a remote job runs the bridge whatever its kind
        # says, because "where" outranks "what" — the same body carries
        # either. Everything after the choice is shared: the semaphore,
        # the timeout, the vocabulary, the gate.
        runner: Any = self._remote_runtime if record.runtime != "local" else None
        configured = self._runners.get(record.kind)
        if getattr(configured, "binds_scientific_inputs", False):
            runner = configured
        if runner is None:
            runner = self._runners.get(record.kind)
        gate = live.gate
        ctx = JobExecutionContext(
            record,
            lambda t, p: self._emit(job_id, t, p),
            Path(self._app.config.workspace),
            observe=lambda reference, report: self._observe_external_fact(
                job_id, reference, report
            ),
        )
        progress_sink = self._progress_sink(job_id)
        terminal: tuple[str, dict[str, Any]] | None = None
        try:
            async with self._semaphore:
                if record.lifetime != "external":
                    self.store.update_job(
                        job_id, status="running", started_at=time.time()
                    )
                    self._sync_run_status(job_id, "running")
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
                            raise JobFailure(
                                "no runner for kind " + record.kind, phase="setup"
                            )
                        outcome = await runner(record, ctx)
                status = (
                    "canceled"
                    if isinstance(outcome, Mapping)
                    and outcome.get("status") == "canceled"
                    else "succeeded"
                )
                if record.lifetime == "external":
                    observed = self._external_observations.get(job_id)
                    if (
                        observed is None
                        or observed[1] not in {"done", "failed", "canceled"}
                        or type(observed[2]) is not int
                    ):
                        raise JobError("external_terminal_unconfirmed")
                    status = {"done": "succeeded"}.get(observed[1], observed[1])
                    if status == "failed":
                        raise JobFailure("external job failed", phase="run")
                terminal = ("job.done", {"status": status})
                self.store.update_job(job_id, status=status, finished_at=time.time())
        except TimeoutError:
            if record.lifetime == "external":
                self._detach_external_observation(job_id, "observation_timeout")
            else:
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
            if record.lifetime == "external":
                self._detach_external_observation(job_id, "observation_detached")
            else:
                terminal = ("job.done", {"status": "canceled"})
                self.store.update_job(
                    job_id, status="canceled", finished_at=time.time()
                )
        except JobFailure as exc:
            observed = self._external_observations.get(job_id)
            if record.lifetime == "external" and (
                observed is None
                or observed[1] != "failed"
                or type(observed[2]) is not int
            ):
                self._detach_external_observation(
                    job_id, "observation_unconfirmed:" + type(exc).__name__
                )
            elif exc.terminal_status == "interrupted":
                terminal = ("job.done", {"status": "interrupted"})
                self.store.update_job(
                    job_id,
                    status="interrupted",
                    error=exc.error,
                    finished_at=time.time(),
                )
            else:
                terminal = ("job.failed", {"error": exc.error, "phase": exc.phase})
                self.store.update_job(
                    job_id, status="failed", error=exc.error, finished_at=time.time()
                )
        except Exception as exc:  # noqa: BLE001 - one job must never take the plane down
            _log.exception("job %s crashed the runner", job_id)
            named = type(exc).__name__ + ": " + str(exc)
            observed = self._external_observations.get(job_id)
            if record.lifetime == "external" and (
                observed is None
                or observed[1] != "failed"
                or type(observed[2]) is not int
            ):
                self._detach_external_observation(
                    job_id, "observation_unconfirmed:" + type(exc).__name__
                )
            else:
                terminal = ("job.failed", {"error": named, "phase": "run"})
                self.store.update_job(
                    job_id, status="failed", error=named, finished_at=time.time()
                )
        finally:
            gate.abandon()
            final = self.store.get_job(job_id)
            if final:
                self._sync_run_status(job_id, final.status)
                if (
                    record.lifetime != "external"
                    and self.run_store.load("verification:exit:" + job_id) is None
                ):
                    self._emit(
                        job_id,
                        "execution.finished",
                        {
                            "exit_code": None,
                            "evidence_ref": "runner did not report a process exit code",
                        },
                    )
        if terminal is not None:
            if record.lifetime == "external":
                final = self.store.get_job(job_id)
                self._emit_terminal_once(job_id, final.status, terminal[1])
            else:
                self._emit(job_id, terminal[0], terminal[1])
            live = self._live.get(job_id)
            if live is not None and not live.subscribers:
                self._live.pop(job_id, None)
        self._external_observations.pop(job_id, None)

    def _progress_sink(self, job_id: str) -> Callable[[ProgressUpdate], None]:
        def sink(update: ProgressUpdate) -> None:
            label = update.message or update.tool_name
            if not label:
                return
            payload: dict[str, Any] = {"label": label}
            if update.fraction is not None:
                payload["percent"] = max(0.0, min(100.0, update.fraction * 100.0))
            self._emit(job_id, "progress", payload)

        return sink
