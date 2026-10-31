"""User-driven notebook execution over the shared persistent kernel."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import asdict, replace
from typing import Any

from omicsclaw.entry.events import TurnEvent, TurnEventType
from omicsclaw.entry.stream import TurnStream, _stream_key
from omicsclaw.kernel import KernelCallbacks
from omicsclaw.memory.database import Database
from omicsclaw.memory.notebook import ExecutionRecord, NotebookCell, NotebookStore, _cell, _execution
from omicsclaw.memory.notebook_io import cells_to_ipynb
from omicsclaw.memory.turn_events import DurableTurnEventLog
from omicsclaw.tools.context import ProgressUpdate

_log = logging.getLogger(__name__)

TERMINAL = frozenset({"completed", "failed", "interrupted"})
ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class NotebookError(ValueError):
    def __init__(self, code: str, status: int = 422):
        super().__init__(code)
        self.code, self.status = code, status


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise NotebookError("invalid_identifier")
    return value


def revision(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise NotebookError("invalid_revision")
    return value


class NotebookAnalysis:
    """One workspace's cells; authenticated Run is an explicit user command.

    Execution claims and cell CAS writes share a SQLite transaction. A process
    never resumes an abandoned command automatically: its code may already have
    produced side effects before the process stopped.
    """

    def __init__(self, database: Database, kernel: Any, *, coordination=None):
        self.db = database
        self.coordination = coordination
        self.store = NotebookStore(database)
        self.kernel = kernel
        self.events = DurableTurnEventLog(database)
        self.tasks: dict[str, asyncio.Task] = {}
        self.output_sinks: dict[str, Any] = {}
        from omicsclaw.entry.notebook_context import NotebookLiveState

        self.context = NotebookLiveState(self)
        from omicsclaw.entry.notebook_apps import NotebookApps

        self.apps = NotebookApps(self)
        self.closed = False
        self.admissions: set[asyncio.Task] = set()
        self.cancelling: set[str] = set()
        self.owner_id = uuid.uuid4().hex
        self.heartbeat: asyncio.Task | None = None
        self.db.run(
            lambda c: c.execute(
                """CREATE TABLE IF NOT EXISTS notebook_owners (
            notebook_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, lease_until REAL NOT NULL)"""
            )
        )
        self.db.run(
            lambda c: c.execute("""CREATE TABLE IF NOT EXISTS notebook_bindings (
            notebook_id TEXT PRIMARY KEY, session_id TEXT UNIQUE NOT NULL,
            kernel_session_id TEXT NOT NULL, created_at REAL NOT NULL)""")
        )
        self.db.run(
            lambda c: c.execute(
                """CREATE TABLE IF NOT EXISTS notebook_feedback (
            id TEXT PRIMARY KEY, notebook_id TEXT NOT NULL, execution_id TEXT NOT NULL,
            kind TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL)"""
            )
        )

    def session(self, nid: str) -> str:
        binding = self.binding(notebook_id=nid)
        return binding["kernel_session_id"] if binding else "notebook:" + nid

    def binding(self, *, notebook_id=None, session_id=None):
        field, value = (
            ("notebook_id", notebook_id)
            if notebook_id is not None
            else ("session_id", session_id)
        )

        def read(c):
            row = c.execute(
                f"SELECT * FROM notebook_bindings WHERE {field}=?", (value,)
            ).fetchone()
            return dict(row) if row else None

        return self.db.run(read)

    def bind(self, nid, sid):
        identifier(nid)
        identifier(sid)

        def work(c):
            rows = c.execute(
                "SELECT * FROM notebook_bindings WHERE notebook_id=? OR session_id=?",
                (nid, sid),
            ).fetchall()
            if rows:
                if (
                    len(rows) == 1
                    and rows[0]["notebook_id"] == nid
                    and rows[0]["session_id"] == sid
                ):
                    return dict(rows[0])
                raise NotebookError("binding_conflict", 409)
            self._idle(c, nid)
            states = self.kernel.snapshot() if self.kernel else []
            chat_live = any(
                x.get("session_id") == sid
                and x.get("alive")
                and x.get("language") == "python"
                and x.get("location") == "local"
                for x in states
            )
            has_executions = c.execute(
                "SELECT 1 FROM notebook_executions WHERE notebook_id=? LIMIT 1", (nid,)
            ).fetchone()
            if chat_live and has_executions:
                raise NotebookError("kernel_state_conflict", 409)
            key = sid if chat_live else "notebook:" + nid
            c.execute(
                "INSERT INTO notebook_bindings VALUES(?,?,?,?)",
                (nid, sid, key, time.time()),
            )
            return dict(
                c.execute(
                    "SELECT * FROM notebook_bindings WHERE notebook_id=?", (nid,)
                ).fetchone()
            )

        # Caller verifies the session is idle immediately before this synchronous transaction.
        return self._transaction(work)

    async def run_agent(
        self,
        session_id,
        code,
        command,
        *,
        on_output=None,
        timeout_s=None,
        location="local",
        language="python",
        description="",
    ):
        binding = self.binding(session_id=session_id)
        if binding is None:
            raise NotebookError("notebook_not_bound", 409)
        if location != "local":
            raise NotebookError("bound_notebook_location_mismatch", 409)
        nid = binding["notebook_id"]
        cid = "agent-" + command[:58]
        eid = hashlib.sha256(json.dumps([nid, command]).encode()).hexdigest()
        admission = asyncio.create_task(
            self.execute(
                nid,
                cid,
                command,
                0,
                source=code,
                origin="agent",
                on_output=on_output,
                timeout_s=timeout_s or 45,
            )
        )
        try:
            await asyncio.shield(admission)
            task = self.tasks.get(eid)
            if task:
                await asyncio.shield(task)
            else:
                # A fast execution can finish before the admission caller resumes.
                # The durable result, rather than that earlier receipt, decides readiness.
                settled = await self.execution(nid, eid)
                if settled.status not in TERMINAL or (settled.output or {}).get(
                    "event_stream_pending"
                ):
                    raise NotebookError("execution_owner_unavailable", 409)
        except asyncio.CancelledError:
            # Admission is durable even if this caller disconnects. Mark the
            # command before waiting so it cannot start code after cancellation.
            self.cancelling.add(eid)

            async def settle_cancel():
                try:
                    try:
                        await admission
                    except Exception:  # noqa: BLE001 - preserve cancellation and log admission failure
                        _log.exception("Cancelled notebook admission failed: %s", eid)
                        return
                    await self.cancel(nid, eid)
                finally:
                    self.cancelling.discard(eid)

            cleanup = asyncio.create_task(settle_cancel())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # Repeated caller cancellation must not cancel cleanup or
                    # clear the command marker while admission still owns it.
                    continue
            cleanup.result()
            raise
        completed = await self.execution(nid, eid)
        return json.dumps(
            {
                "notebook_id": nid,
                "cell_id": cid,
                "execution_id": eid,
                "status": completed.status,
                "output": completed.output,
            },
            ensure_ascii=True,
        )

    def _transaction(self, work):
        def locked(c):
            c.execute("BEGIN IMMEDIATE")
            return work(c)

        return self.db.run(locked)

    def _recover(self, c, nid):
        if self.coordination is not None:
            return  # Recovery belongs to the independently fenced owner.
        owner = c.execute(
            "SELECT lease_until FROM notebook_owners WHERE notebook_id=?", (nid,)
        ).fetchone()
        if owner and owner["lease_until"] >= time.time():
            return
        c.execute("DELETE FROM notebook_owners WHERE notebook_id=?", (nid,))
        output = json.dumps(
            {
                "error": {
                    "ename": "OwnerLost",
                    "evalue": "Execution owner stopped; code was not rerun",
                },
                "durable_error": "owner_lost",
            }
        )
        c.execute(
            "UPDATE notebook_executions SET status='interrupted', output_json=?, finished_at=? WHERE notebook_id=? AND status IN ('queued','running')",
            (output, time.time(), nid),
        )
        c.execute(
            "UPDATE notebook_cells SET status='interrupted', output_json=?, revision=revision+1 WHERE notebook_id=? AND status IN ('queued','running')",
            (output, nid),
        )
        for row in c.execute(
            "SELECT execution_id, output_json FROM notebook_executions WHERE notebook_id=? AND status IN ('completed','failed','interrupted')",
            (nid,),
        ).fetchall():
            value = json.loads(row["output_json"] or "null")
            if isinstance(value, dict) and value.get("event_stream_pending"):
                value.update(event_stream_pending=False, durable_error="owner_lost")
                encoded = json.dumps(value)
                c.execute(
                    "UPDATE notebook_executions SET output_json=? WHERE execution_id=?",
                    (encoded, row["execution_id"]),
                )
                c.execute(
                    "UPDATE notebook_cells SET output_json=?, revision=revision+1 WHERE notebook_id=? AND execution_id=?",
                    (encoded, nid, row["execution_id"]),
                )

    def _idle(self, c, nid):
        if self.coordination is not None:
            self.coordination.idle(c, nid)
            return
        self._recover(c, nid)
        if c.execute(
            "SELECT 1 FROM notebook_executions WHERE notebook_id=? AND "
            "(status IN ('queued','running','outcome_unknown') OR "
            "json_extract(output_json,'$.event_stream_pending')=1) LIMIT 1",
            (nid,),
        ).fetchone():
            raise NotebookError("notebook_busy", 409)

    async def listing(self):
        rows = await self.db.arun(
            lambda c: [
                dict(row)
                for row in c.execute(
                    "SELECT notebook_id FROM notebook_cells UNION SELECT notebook_id FROM notebook_bindings ORDER BY notebook_id"
                ).fetchall()
            ]
        )

        return [
            {**row, "binding": self.binding(notebook_id=row["notebook_id"])}
            for row in rows
        ]

    async def snapshot(self, nid):
        await asyncio.to_thread(self._transaction, lambda c: self._recover(c, nid))
        cells = await self.store.list_cells(nid)
        executions = await self.store.list_executions(nid)
        feedback = await self.db.arun(
            lambda c: [
                self._feedback(row)
                for row in c.execute(
                    "SELECT * FROM notebook_feedback WHERE notebook_id=? ORDER BY created_at",
                    (nid,),
                ).fetchall()
            ]
        )
        kernel = (
            [
                s
                for s in self.kernel.snapshot()
                if s.get("session_id") == self.session(nid)
            ]
            if self.kernel
            else []
        )
        return {
            "notebook_id": nid,
            "binding": self.binding(notebook_id=nid),
            "cells": [asdict(x) for x in cells],
            "executions": [asdict(x) for x in executions],
            "kernel": kernel,
            "feedback": feedback,
            "live_app": await self.apps.read(nid, metadata=True),
            **(await self.context.snapshot(nid)),
        }

    async def save(self, nid, cid, source, expected):
        if not isinstance(source, str) or len(source.encode("utf-8")) > 256 * 1024:
            raise NotebookError("invalid_source")
        expected = revision(expected)

        def work(c):
            self._idle(c, nid)
            old = self.store._get_cell(c, nid, cid)
            cell = (
                replace(old, source=source, status="idle", output=None)
                if old
                else NotebookCell(
                    nid,
                    cid,
                    source=source,
                    provenance={"source": "user_edit"},
                    trust_label="user_code",
                )
            )
            return self.store._put_cell(c, cell, expected)

        return asdict(await asyncio.to_thread(self._transaction, work))

    async def execute(
        self,
        nid,
        cid,
        command,
        expected,
        *,
        source=None,
        origin="user",
        on_output=None,
        timeout_s=1800,
        semantic=None,
        language=None,
        description="",
    ):
        identifier(command)
        if source is not None and (
            not isinstance(source, str) or len(source.encode()) > 256 * 1024
        ):
            raise NotebookError("invalid_source")
        expected = revision(expected)
        if self.closed or self.kernel is None:
            raise NotebookError("kernel_unavailable", 503)
        eid = hashlib.sha256(json.dumps([nid, command]).encode()).hexdigest()
        kernel_session = self.session(nid)

        def claim(c):
            nonlocal kernel_session
            old = c.execute(
                "SELECT * FROM notebook_executions WHERE execution_id=?", (eid,)
            ).fetchone()
            if old:
                if self.coordination is not None:
                    self.coordination.check_retry(c, eid, semantic)
                provenance = json.loads(old["provenance_json"])
                if (
                    old["cell_id"] != cid
                    or provenance["source_revision"] != expected
                    or (source is not None and provenance["code"] != source)
                ):
                    raise NotebookError("command_conflict", 409)
                return False
            if self.coordination is not None:
                self.coordination.claim(c, nid, eid, semantic)
                self.coordination.admission_idle(c, nid, semantic)
                kernel_session, _ = self.coordination.execution_target(c, eid)
            else:
                self._idle(c, nid)
            owner = c.execute(
                "SELECT owner_id, lease_until FROM notebook_owners WHERE notebook_id=?",
                (nid,),
            ).fetchone()
            if (
                owner
                and owner["owner_id"] != self.owner_id
                and owner["lease_until"] >= time.time()
            ):
                raise NotebookError("kernel_owner_elsewhere", 409)
            cell = self.store._get_cell(c, nid, cid)
            desc = (semantic.get("description") if isinstance(semantic, dict) else "") or description
            if cell is None and source is not None:
                cell = NotebookCell(
                    nid,
                    cid,
                    source=source,
                    provenance={"source": "agent_run", **({"description": desc} if desc else {})},
                    language=semantic["language"] if semantic else (language or "python"),
                    trust_label="agent_code",
                )
            if cell is None:
                raise NotebookError("cell_not_found", 404)
            if cell.revision != expected:
                raise NotebookError("revision_conflict", 409)
            if not cell.source.strip():
                raise NotebookError("code_required")
            c.execute(
                "INSERT INTO notebook_owners VALUES(?,?,?) ON CONFLICT(notebook_id) DO UPDATE SET owner_id=excluded.owner_id, lease_until=excluded.lease_until",
                (nid, self.owner_id, time.time() + 30),
            )
            provenance = {
                "source": "agent_run" if origin == "agent" else "user_run",
                "origin": origin,
                "timeout_s": timeout_s,
                "language": semantic["language"] if semantic else (language or cell.language),
                "source_revision": expected,
                "code": cell.source,
                "session_id": kernel_session,
                **({"description": desc} if desc else {}),
            }
            if self.coordination is not None:
                from .contracts import public_semantic

                provenance["runtime"] = public_semantic(semantic)
            self.store._record_execution(
                c,
                ExecutionRecord(
                    eid,
                    nid,
                    cid,
                    client_command_id=command,
                    provenance=provenance,
                    trust_label="kernel_output_data",
                ),
            )
            self.store._put_cell(
                c,
                replace(
                    cell,
                    status="queued",
                    execution_id=eid,
                    client_command_id=command,
                    output=None,
                ),
                expected,
            )
            return True

        # Shield admission so an HTTP disconnect cannot leave a committed claim
        # without scheduling its task. Only this task owns the create decision.
        async def admit():
            fresh = await asyncio.to_thread(self._transaction, claim)
            if fresh:
                if on_output is not None:
                    self.output_sinks[eid] = on_output
                if self.coordination is None and self.heartbeat is None:
                    self.heartbeat = asyncio.create_task(self._heartbeat())
                if self.coordination is None:
                    self.dispatch(nid, eid)
                else:
                    committed = getattr(self.coordination, "admission_committed", None)
                    if committed is not None:
                        await asyncio.to_thread(committed, eid)
                    self.coordination.wake()
            return asdict(await self.store.get_execution(eid))

        admission = asyncio.create_task(admit())
        self.admissions.add(admission)
        admission.add_done_callback(self.admissions.discard)
        return await asyncio.shield(admission)

    def dispatch(self, nid, eid):
        """Schedule an already committed execution in the service owner."""
        if eid in self.tasks:
            return
        task = asyncio.create_task(self._run(nid, eid))
        self.tasks[eid] = task

        def finished(done):
            self.tasks.pop(eid, None)
            if self.coordination is not None:
                self.coordination.execution_finished(nid, eid)
            if (
                self.coordination is not None
                and not done.cancelled()
                and done.exception() is not None
            ):
                self.coordination.durability_failed = True

        task.add_done_callback(finished)

    async def _heartbeat(self):
        while not self.closed or self.tasks or self.admissions:
            await self.db.arun(
                lambda c: c.execute(
                    "UPDATE notebook_owners SET lease_until=? WHERE owner_id=? AND lease_until>=?",
                    (time.time() + 30, self.owner_id, time.time()),
                )
            )
            await asyncio.sleep(5)

    async def _state(self, nid, eid, status, output=None):
        def work(c):
            owner = c.execute(
                "SELECT owner_id, lease_until FROM notebook_owners WHERE notebook_id=?",
                (nid,),
            ).fetchone()
            if (
                owner is None
                or owner["owner_id"] != self.owner_id
                or owner["lease_until"] < time.time()
            ):
                raise NotebookError("execution_owner_lost", 409)
            row = c.execute(
                "SELECT cell_id, provenance_json FROM notebook_executions WHERE execution_id=?",
                (eid,),
            ).fetchone()
            cell = self.store._get_cell(c, nid, row["cell_id"])
            if cell is None or (cell.execution_id != eid and self.coordination is None):
                raise NotebookError("execution_owner_lost", 409)
            self.store._update_execution(
                c,
                eid,
                status,
                output,
                json.loads(row["provenance_json"]),
                "kernel_output_data",
                time.time() if status in TERMINAL else None,
            )
            if cell.execution_id == eid:
                self.store._put_cell(
                    c, replace(cell, status=status, output=output), cell.revision
                )
            if self.coordination is not None:
                self.coordination.state_changed(c, nid, eid, status, output)

        await asyncio.to_thread(self._transaction, work)

    async def _run(self, nid, eid):
        stream = None
        output: dict[str, Any] = {}
        status = "failed"
        kernel_session, language = (
            self.db.run(lambda c: self.coordination.execution_target(c, eid))
            if self.coordination
            else (self.session(nid), "python")
        )
        try:
            record = await self.store.get_execution(eid)
            if self.coordination is None:
                language = record.provenance.get("language", "python")
            stream = (
                self.coordination.stream(kernel_session, eid, self.events)
                if self.coordination is not None
                else TurnStream(kernel_session, eid, durable_log=self.events)
            )
            stream.publish(
                TurnEvent.exchange_start(session_id=kernel_session, turn_id=eid)
            )
            if not await stream.flush_durable():
                raise RuntimeError("event_log_unavailable")
            await self._state(nid, eid, "running")

            def on_stream(name, text):
                if name == "stdout" and eid in self.output_sinks:
                    self.output_sinks[eid](text)
                if self.coordination is not None:
                    return
                stream.publish_threadsafe(
                    TurnEvent.progress_update(
                        ProgressUpdate(tool_name=name, message=text[:8000]),
                        session_id=kernel_session,
                        turn_id=eid,
                    )
                )

            def on_output(item):
                if self.coordination is not None:
                    stream.publish_threadsafe(
                        TurnEvent(
                            type=TurnEventType.PROGRESS,
                            seq=0,
                            session_id=kernel_session,
                            turn_id=eid,
                            wire_payload={"notebookOutput": item, "outputVersion": 1},
                        )
                    )

            if eid in self.cancelling:
                raise asyncio.CancelledError()
            if self.coordination is not None:
                await self.coordination.before_dispatch(nid, eid)
            checkpoint = getattr(self.coordination, "checkpoints", None)
            protected = checkpoint.execution_options(nid, eid) if checkpoint is not None else {}
            result = await self.kernel.execute(
                kernel_session,
                record.provenance["code"],
                origin=record.provenance.get("origin", "user"),
                language=language,
                callbacks=KernelCallbacks(on_stream=on_stream, on_output=on_output, on_timeout_pending=(lambda elapsed: checkpoint.timeout(nid, eid)) if protected else None),
                timeout_s=record.provenance.get("timeout_s", 1800),
                retry_on_death=False,
                execution_token=eid,
                cancelled=lambda: eid in self.cancelling,
                **protected,
            )
            output = {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "error": result.error,
                "figures": [asdict(f) for f in result.figures],
                "files_written": getattr(result, "files_written", []),
                "execution_count": result.execution_count,
                "execution_id": getattr(result, "execution_id", "") or eid,
                "usage": result.usage,
                "provenance": {
                    **dict(record.provenance or {}),
                    "code": record.provenance.get("code", ""),
                    "execution_id": getattr(result, "execution_id", "") or eid,
                },
            }
            if getattr(result, "files_read", None):
                output["files_read"] = list(result.files_read)
            if getattr(result, "skill_calls", None):
                output["skill_calls"] = list(result.skill_calls)
            if getattr(result, "skills_loaded", None):
                output["skills_loaded"] = list(result.skills_loaded)
            if getattr(result, "object_versions", None):
                output["object_versions"] = list(result.object_versions)
            if self.coordination is not None:
                output.update({"outputVersion": 1, "outputs": result.outputs})
            if self.coordination is not None and result.status == "timeout":
                output["termination_reason"] = "execution_timeout"
            status = {
                "ok": "completed",
                "interrupted": "interrupted",
                "timeout": "interrupted",
            }.get(result.status, "failed")
        except asyncio.CancelledError:
            status = "interrupted"
            output = {
                "error": {"ename": "Interrupted", "evalue": "Execution interrupted"}
            }
        except Exception as exc:  # noqa: BLE001 - persist the execution failure
            output = {"error": {"ename": type(exc).__name__, "evalue": str(exc)}}
        finally:
            finishing = asyncio.create_task(
                self._finish(nid, eid, stream, status, output)
            )
            try:
                await asyncio.shield(finishing)
            except asyncio.CancelledError:
                await finishing
            finally:
                self.output_sinks.pop(eid, None)

    async def _finish(self, nid, eid, stream, status, output):
        kernel_session = (
            self.db.run(lambda c: self.coordination.execution_target(c, eid))[0]
            if self.coordination
            else self.session(nid)
        )
        try:
            await asyncio.sleep(0)
            analysis = getattr(self.coordination, "analysis", None)
            if analysis is not None:

                status, output = await analysis.finish(nid, eid, status, output)
            if self.coordination is not None:
                output = self.coordination.prepare_output(output)
            output["event_stream_pending"] = True
            # Fence and commit the outcome before publishing its terminal event.
            # SSE waits for the second write, which certifies the event flush.
            await self._state(nid, eid, status, output)
            if stream:
                terminal = "cancelled" if status == "interrupted" else status
                stream.publish(
                    TurnEvent.exchange_end(
                        terminal, session_id=kernel_session, turn_id=eid
                    )
                )
                if not await stream.flush_durable():
                    output["durable_error"] = "event_log_unavailable"
            output["event_stream_pending"] = False
            await self._state(nid, eid, status, output)
            committed = getattr(self.coordination, "execution_committed", None)
            if committed is not None:
                await asyncio.to_thread(committed, eid)
        except NotebookError as exc:
            if exc.code != "execution_owner_lost":
                raise
            # Recovery already owns the record. A stale writer cannot publish
            # or overwrite the verdict made by its replacement.
        finally:
            if stream and self.coordination is not None:
                await self.coordination.close_stream(eid, stream)
            if stream and stream._durable_owner_epoch is not None:
                await asyncio.to_thread(
                    self.events.release_owner,
                    _stream_key(kernel_session, eid),
                    epoch=stream._durable_owner_epoch,
                )

    async def execution(self, nid, eid):
        identifier(eid)
        await asyncio.to_thread(self._transaction, lambda c: self._recover(c, nid))
        record = await self.store.get_execution(eid)
        if record is None or record.notebook_id != nid:
            raise NotebookError("execution_not_found", 404)
        return record

    async def cancel(self, nid, eid):
        record = await self.execution(nid, eid)
        if record.status in TERMINAL:
            task = self.tasks.get(eid)
            if task:
                await asyncio.shield(task)
            elif isinstance(record.output, dict) and record.output.get(
                "event_stream_pending"
            ):
                raise NotebookError("execution_owner_unavailable", 409)
            return {"status": (await self.execution(nid, eid)).status}
        if eid not in self.tasks:
            raise NotebookError("execution_owner_unavailable", 409)
        # Interrupt the kernel before task cancellation: cancelling a to_thread
        # waiter alone would leave Python code running in the background.
        self.cancelling.add(eid)
        kernel_session, language = (
            self.db.run(lambda c: self.coordination.execution_target(c, eid))
            if self.coordination
            else (self.session(nid), "python")
        )
        await self.kernel.cancel(
            kernel_session, execution_token=eid, language=language, location="local"
        )
        task = self.tasks.get(eid)
        if task:
            await asyncio.shield(task)
        self.cancelling.discard(eid)
        return {"status": (await self.execution(nid, eid)).status}

    async def export(self, nid):
        return cells_to_ipynb(await self.store.list_cells(nid))

    async def replay(
        self,
        nid: str,
        workspace: Any,
        *,
        artifact_store: Any = None,
        module: str | None = None,
        python: str | None = None,
        timeout_s: float = 180.0,
        registered_inputs: list[str] | None = None,
        replay: bool = True,
        slice_dead_cells: bool = False,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            export_and_replay,
            nid,
            workspace,
            db=self.db,
            artifact_store=artifact_store,
            module=module,
            python=python,
            timeout_s=timeout_s,
            registered_inputs=registered_inputs,
            replay=replay,
            slice_dead_cells=slice_dead_cells,
        )

    async def trace(
        self,
        nid: str,
        workspace: Any,
        *,
        artifact_store: Any = None,
        module: str | None = None,
        registered_inputs: list[str] | None = None,
        slice_dead_cells: bool = False,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            export_and_trace,
            nid,
            workspace,
            db=self.db,
            artifact_store=artifact_store,
            module=module,
            registered_inputs=registered_inputs,
            slice_dead_cells=slice_dead_cells,
        )

    async def import_document(self, nid, document):
        if (
            not isinstance(document, dict)
            or type(document.get("nbformat")) is not int
            or document["nbformat"] != 4
            or not isinstance(document.get("cells"), list)
        ):
            raise NotebookError("invalid_ipynb")
        cells, seen = [], set()
        for index, raw in enumerate(document["cells"]):
            if not isinstance(raw, dict) or raw.get("cell_type") not in (
                "code",
                "markdown",
                "raw",
            ):
                raise NotebookError("invalid_ipynb_cell")
            if raw["cell_type"] != "code":
                continue
            cid = identifier(raw.get("id") or f"cell-{index}")
            if cid in seen:
                raise NotebookError("duplicate_cell_id")
            seen.add(cid)
            source = raw.get("source", "")
            if isinstance(source, list) and all(isinstance(x, str) for x in source):
                source = "".join(source)
            if not isinstance(source, str) or len(source.encode()) > 256 * 1024:
                raise NotebookError("invalid_source")
            cells.append(
                NotebookCell(
                    nid,
                    cid,
                    source=source,
                    provenance={"source": "ipynb_import"},
                    trust_label="imported_untrusted",
                )
            )

        def work(c):
            if c.execute(
                "SELECT 1 FROM notebook_cells WHERE notebook_id=?", (nid,)
            ).fetchone():
                raise NotebookError("notebook_not_empty", 409)
            return [asdict(self.store._put_cell(c, cell, 0)) for cell in cells]

        return await asyncio.to_thread(self._transaction, work)

    async def slice_for_artifact(
        self, nid: str, target_path: str = ""
    ) -> dict[str, Any]:
        identifier(nid)
        await asyncio.to_thread(self._transaction, lambda c: self._recover(c, nid))
        cells = await self.store.list_cells(nid)
        executions = await self.store.list_executions(nid)
        cells_map = {cell.cell_id: cell for cell in cells}
        latest_by_cell: dict[str, ExecutionRecord] = {}
        for rec in executions:
            latest_by_cell[rec.cell_id] = rec
        for cell in cells:
            if cell.source.strip() and cell.cell_id not in latest_by_cell:
                latest_by_cell[cell.cell_id] = ExecutionRecord(
                    execution_id=cell.execution_id or cell.cell_id,
                    notebook_id=cell.notebook_id,
                    cell_id=cell.cell_id,
                    status=cell.status,
                    output=cell.output,
                    provenance={
                        **(cell.provenance or {}),
                        "code": cell.source,
                        "language": cell.language,
                    },
                )
        steps: list[dict[str, Any]] = []
        for idx, rec in enumerate(latest_by_cell.values(), 1):
            prov = rec.provenance or {}
            cell_obj = cells_map.get(rec.cell_id)
            code = str(
                prov.get("code")
                or (cell_obj.source if cell_obj is not None else "")
                or ""
            ).strip()
            if not code:
                continue
            out = rec.output if isinstance(rec.output, dict) else {}
            code_sha = hashlib.sha256(code.encode("utf-8")).hexdigest()
            steps.append(
                {
                    "index": idx,
                    "cell_id": rec.cell_id,
                    "execution_id": rec.execution_id,
                    "language": str(
                        prov.get("language")
                        or (cell_obj.language if cell_obj else "python")
                        or "python"
                    ),
                    "code_sha256": code_sha,
                    "files_read": list(
                        out.get("files_read") or prov.get("files_read") or []
                    ),
                    "files_written": list(
                        out.get("files_written") or prov.get("files_written") or []
                    ),
                    "skill_calls": list(
                        out.get("skill_calls") or prov.get("skill_calls") or []
                    ),
                    "object_versions": list(
                        out.get("object_versions") or prov.get("object_versions") or []
                    ),
                    "code_preview": code[:1200],
                    "code": code,
                }
            )
        return compute_backward_artifact_slice(
            steps, target_artifact=target_path or None
        )

    @staticmethod
    def _feedback(row):
        return {
            **dict(row),
            "payload": json.loads(row["payload"]),
            "trust_label": "live_app_untrusted",
            "provenance": {"source": "live_app", "execution_id": row["execution_id"]},
        }

    async def feedback(self, nid, document):
        kind, payload = document.get("kind"), document.get("payload")
        if kind not in ("selection", "note", "finding_addressed") or not isinstance(
            payload, dict
        ):
            raise NotebookError("invalid_feedback")
        raw_eid = document.get("execution_id")
        if kind == "finding_addressed" and not raw_eid:
            identifier(nid)
            execs = await self.store.list_executions(nid)
            eid = execs[-1].execution_id if execs else "finding-resolution"
        else:
            record = await self.execution(nid, raw_eid)
            eid = record.execution_id
        encoded = json.dumps(payload, allow_nan=False)
        if len(encoded.encode()) > 16384:
            raise NotebookError("feedback_too_large", 413)
        fid = uuid.uuid4().hex

        def work(c):
            c.execute(
                "INSERT INTO notebook_feedback VALUES(?,?,?,?,?,?)",
                (fid, nid, eid, kind, encoded, time.time()),
            )
            if kind == "finding_addressed":
                finding_id = str(
                    payload.get("finding_id") or payload.get("id") or ""
                ).strip()
                resolution_note = str(payload.get("resolution_note") or "").strip()
                if finding_id:
                    for table, id_col in (
                        ("notebook_executions", "execution_id"),
                        ("notebook_cells", "cell_id"),
                    ):
                        rows = c.execute(
                            f"SELECT {id_col}, output_json, provenance_json FROM {table} WHERE notebook_id=?",
                            (nid,),
                        ).fetchall()
                        for r in rows:
                            for col_name in ("output_json", "provenance_json"):
                                raw_json = r[col_name]
                                if not raw_json:
                                    continue
                                try:
                                    obj = json.loads(raw_json)
                                except Exception:
                                    continue
                                if isinstance(obj, dict) and isinstance(
                                    obj.get("findings"), list
                                ):
                                    changed = False
                                    for item in obj["findings"]:
                                        if (
                                            isinstance(item, dict)
                                            and str(item.get("id") or "") == finding_id
                                        ):
                                            item["status"] = "addressed"
                                            if resolution_note:
                                                item["resolution_note"] = (
                                                    resolution_note
                                                )
                                            changed = True
                                    if changed:
                                        c.execute(
                                            f"UPDATE {table} SET {col_name}=? WHERE notebook_id=? AND {id_col}=?",
                                            (json.dumps(obj), nid, r[id_col]),
                                        )
                    candidate_roots: list[Path] = []
                    ws_raw = payload.get("workspace")
                    if isinstance(ws_raw, str) and ws_raw.strip():
                        candidate_roots.append(Path(ws_raw))
                    db_path = getattr(self.db, "path", None)
                    if db_path and str(db_path) != ":memory:":
                        try:
                            resolved_db = Path(str(db_path)).resolve()
                            for p in (resolved_db.parent, resolved_db.parent.parent):
                                if p not in candidate_roots:
                                    candidate_roots.append(p)
                        except OSError:
                            pass
                    now_ts = time.time()
                    for root_dir in candidate_roots:
                        for rel_fp in (
                            ".omicsclaw/provenance/findings.json",
                            "provenance/findings.json",
                        ):
                            fp = root_dir / rel_fp
                            if not fp.is_file():
                                continue
                            try:
                                raw_f = json.loads(fp.read_text(encoding="utf-8"))
                                items_list = (
                                    raw_f.get("findings")
                                    if isinstance(raw_f, dict)
                                    else raw_f
                                    if isinstance(raw_f, list)
                                    else None
                                )
                                if isinstance(items_list, list):
                                    updated_disk = False
                                    for item in items_list:
                                        if (
                                            isinstance(item, dict)
                                            and str(item.get("id") or "") == finding_id
                                        ):
                                            item["status"] = "addressed"
                                            item["resolution_note"] = resolution_note
                                            item["addressed_note"] = resolution_note
                                            item["addressed_at"] = now_ts
                                            updated_disk = True
                                    if updated_disk:
                                        fp.write_text(
                                            json.dumps(raw_f, indent=2),
                                            encoding="utf-8",
                                        )
                            except Exception:
                                pass
            return self._feedback(
                c.execute(
                    "SELECT * FROM notebook_feedback WHERE id=?", (fid,)
                ).fetchone()
            )

        return await self.db.arun(work)

    async def close(self):
        self.closed = True
        if self.admissions:
            await asyncio.gather(*tuple(self.admissions))
        for eid in list(self.tasks):
            record = await self.store.get_execution(eid)
            await self.cancel(record.notebook_id, eid)
        if self.heartbeat:
            self.heartbeat.cancel()
            await asyncio.gather(self.heartbeat, return_exceptions=True)
        await self.db.arun(
            lambda c: c.execute(
                "DELETE FROM notebook_owners WHERE owner_id=?", (self.owner_id,)
            )
        )


import csv
from pathlib import Path
from omicsclaw.runs.verification import (
    resolve_code_artifact_markers,
    run_clean_room_replay,
    sha256_file,
)

_REPLAY_BOOTSTRAP_HEADER = """# Auto-generated clean-room replay script by OmicsClaw
try:
    from omicsclaw.skills.sidecar import build_sidecar_bootstrap_code as _oc_bootstrap
    exec(_oc_bootstrap(), globals())
except Exception:
    pass
"""


def _read_kernel_ledger(workspace: Path) -> list[dict[str, Any]]:
    candidates = [
        workspace / ".omicsclaw" / "provenance" / "kernel_ledger.jsonl",
        workspace / ".omicsclaw" / "provenance" / "session_ledger.jsonl",
        workspace / "provenance" / "kernel_ledger.jsonl",
        workspace / "provenance" / "session_ledger.jsonl",
    ]
    entries: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for ledger_path in candidates:
        if not ledger_path.is_file():
            continue
        try:
            for line in ledger_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        key = (
                            str(item.get("execution_id") or ""),
                            str(item.get("execution_count") or ""),
                            str(item.get("code_sha256") or ""),
                        )
                        if key == ("", "", "") or key not in seen_keys:
                            if key != ("", "", ""):
                                seen_keys.add(key)
                            entries.append(item)
                except ValueError:
                    continue
        except OSError:
            continue
    return entries


def _resolve_skill_kernel_path(
    ws: Path, kpath_str: str = "", skill_name: str = ""
) -> Path | None:
    from omicsclaw.skills.sidecar import DEFAULT_SKILLS_ROOT

    kpath_str = str(kpath_str or "").strip()
    skill_name = str(skill_name or "").strip()
    if kpath_str and not kpath_str.startswith("<"):
        kp = Path(kpath_str)
        if kp.is_absolute() and kp.is_file():
            return kp.resolve()
        if not kp.is_absolute():
            for base in (ws, DEFAULT_SKILLS_ROOT.parent, DEFAULT_SKILLS_ROOT):
                cand = base / kp
                if cand.is_file():
                    return cand.resolve()
    if skill_name:
        for cand in (
            ws / "provenance" / "sidecars" / skill_name / "kernel.py",
            ws / "output" / "sidecars" / skill_name / "kernel.py",
            ws / "skills" / skill_name / "kernel.py",
        ):
            if cand.is_file():
                return cand.resolve()
        for root_cand in (ws / "skills", DEFAULT_SKILLS_ROOT):
            if root_cand.is_dir():
                for found in sorted(root_cand.rglob("kernel.py")):
                    if found.parent.name == skill_name:
                        return found.resolve()
    if kpath_str and not kpath_str.startswith("<"):
        kp = Path(kpath_str)
        if kp.is_file():
            return kp.resolve()
    return None



def _summarize_table_for_brief(path: Path, max_rows: int = 5) -> str:
    delimiter = "\t" if path.suffix.lower() in {".tsv", ".txt"} else ","
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh, delimiter=delimiter))
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        return f"- `{path.name}`: unreadable ({exc})"
    if not rows:
        return f"- `{path.name}`: empty file (0 rows)"
    header, body = rows[0], rows[1:]
    preview = [", ".join(r[:10]) for r in body[:max_rows]]
    lines = [
        f"- `{path.name}`: {len(body)} rows x {len(header)} cols; columns={header[:12]}",
    ]
    for idx, p in enumerate(preview, 1):
        lines.append(f"  - row {idx}: `{p[:200]}`")
    return "\n".join(lines)


def _summarize_input_metadata(workspace: Path, input_paths: list[str]) -> list[str]:
    notes: list[str] = []
    candidates = list(input_paths)
    data_dir = workspace / "data"
    if data_dir.is_dir():
        for f in sorted(data_dir.rglob("*")):
            if f.is_file():
                rel = f.relative_to(workspace).as_posix()
                if rel not in candidates:
                    candidates.append(rel)
    for rel in candidates[:12]:
        target = workspace / rel
        if not target.is_file():
            continue
        suffix = target.suffix.lower()
        if suffix in {".csv", ".tsv"}:
            notes.append(_summarize_table_for_brief(target, max_rows=3))
        elif suffix == ".h5ad":
            try:
                import anndata
                ad = anndata.read_h5ad(target, backed="r")
                obs_cols = list(ad.obs.columns)[:20]
                var_cols = list(ad.var.columns)[:12]
                shape = tuple(int(x) for x in ad.shape)
                if getattr(ad, "file", None) is not None:
                    ad.file.close()
                notes.append(
                    f"- `{rel}` (h5ad): shape={shape}, obs_columns={obs_cols}, var_columns={var_cols}"
                )
            except Exception as exc:  # noqa: BLE001
                notes.append(f"- `{rel}` (h5ad): size={target.stat().st_size} bytes ({exc})")
        else:
            notes.append(f"- `{rel}`: size={target.stat().st_size} bytes")
    return notes


_READONLY_METHODS = frozenset({
    "head", "tail", "describe", "info", "copy", "to_csv", "to_parquet", "to_json",
    "to_dict", "to_numpy", "to_list", "tolist", "write_h5ad", "write", "write_csvs",
    "mean", "sum", "std", "var", "min", "max", "median", "quantile", "value_counts",
    "nunique", "isna", "isnull", "notna", "notnull", "select_dtypes", "get", "keys",
    "values", "items", "iterrows", "itertuples", "groupby", "loc", "iloc",
})


def _extract_cell_def_use(code: str, object_versions: list[dict[str, Any]] | None = None) -> tuple[set[str], set[str]]:
    import ast as _ast

    defs: set[str] = set()
    uses: set[str] = set()

    def _base_name(node: _ast.AST | None) -> str | None:
        cur = node
        while isinstance(cur, (_ast.Attribute, _ast.Subscript)):
            cur = cur.value
        if isinstance(cur, _ast.Name):
            return cur.id
        return None

    try:
        tree = _ast.parse(code)
    except SyntaxError:
        return defs, uses

    for node in _ast.walk(tree):
        if isinstance(node, _ast.Name):
            if isinstance(node.ctx, _ast.Store):
                defs.add(node.id)
            elif isinstance(node.ctx, _ast.Load):
                uses.add(node.id)
        elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
            defs.add(node.name)
        elif isinstance(node, (_ast.Import, _ast.ImportFrom)):
            for alias in node.names:
                defs.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, (_ast.Assign, _ast.AnnAssign, _ast.AugAssign)):
            targets = node.targets if isinstance(node, _ast.Assign) else [node.target]
            for tgt in targets:
                if isinstance(tgt, (_ast.Attribute, _ast.Subscript)):
                    bname = _base_name(tgt)
                    if bname:
                        defs.add(bname)
                        uses.add(bname)
        elif isinstance(node, _ast.Call):
            has_inplace = any(
                kw.arg == "inplace" and isinstance(kw.value, _ast.Constant) and bool(kw.value.value)
                for kw in node.keywords
            )
            if isinstance(node.func, _ast.Attribute):
                attr_name = node.func.attr
                bname = _base_name(node.func.value)
                if bname and (has_inplace or attr_name.startswith(("add_", "set_", "update", "append", "extend", "insert", "pop", "remove", "clear", "sort"))):
                    defs.add(bname)
                    uses.add(bname)
                func_unparsed = ""
                try:
                    func_unparsed = _ast.unparse(node.func)
                except Exception:
                    func_unparsed = attr_name
                if any(
                    func_unparsed.startswith(pref)
                    for pref in ("sc.pp.", "sc.tl.", "scanpy.pp.", "scanpy.tl.", "sq.gr.", "squidpy.gr.", "mu.pp.", "mu.tl.")
                ):
                    copy_false = not any(
                        kw.arg == "copy" and isinstance(kw.value, _ast.Constant) and bool(kw.value.value)
                        for kw in node.keywords
                    )
                    if copy_false and node.args and isinstance(node.args[0], _ast.Name):
                        defs.add(node.args[0].id)
                        uses.add(node.args[0].id)

    for ov in object_versions or ():
        if isinstance(ov, dict) and ov.get("name"):
            vname = str(ov["name"])
            prev_v = ov.get("previous_version") if ov.get("previous_version") is not None else ov.get("prev_version")
            ver = int(ov.get("version", 0))
            if prev_v is not None or ver >= 1:
                defs.add(vname)
            if (prev_v is not None and int(prev_v) > 0) or ver > 1:
                uses.add(vname)

    return defs, uses


def _load_steps_from_workspace(ws: Path) -> list[dict[str, Any]]:
    for rc_cand in (
        ws / "provenance" / "replay_receipt.json",
        ws / ".omicsclaw" / "provenance" / "replay_receipt.json",
    ):
        if rc_cand.is_file():
            try:
                data = json.loads(rc_cand.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("steps"), list) and data["steps"]:
                    return list(data["steps"])
            except Exception:  # noqa: BLE001
                pass
    ledger_entries = _read_kernel_ledger(ws)
    steps: list[dict[str, Any]] = []
    idx = 0
    for l_entry in ledger_entries:
        if l_entry.get("status", "ok") not in ("ok", "completed"):
            continue
        code = str(l_entry.get("code") or "").strip()
        if not code and not (
            l_entry.get("files_read")
            or l_entry.get("files_written")
            or l_entry.get("skill_calls")
            or l_entry.get("object_versions")
        ):
            continue
        idx += 1
        steps.append(
            {
                "index": idx,
                "cell_id": str(l_entry.get("cell_id") or f"cell-{idx}"),
                "execution_id": str(l_entry.get("execution_id") or f"exec-{idx}"),
                "code_sha256": str(l_entry.get("code_sha256") or ""),
                "files_read": list(l_entry.get("files_read") or []),
                "files_written": list(l_entry.get("files_written") or []),
                "skill_calls": list(l_entry.get("skill_calls") or []),
                "object_versions": list(l_entry.get("object_versions") or []),
                "code": code,
                "code_preview": code[:1200],
            }
        )
    return steps


def compute_backward_artifact_slice(
    steps_or_workspace: list[dict[str, Any]] | dict[str, Any] | str | Path,
    target_artifact: str | None = None,
) -> dict[str, Any]:
    """Compute a deterministic backward program slice over notebook steps for ``target_artifact``.

    Combines static AST Def-Use + in-place container mutation analysis + runtime
    ``object_versions`` + audit-hook ``files_read``/``files_written`` dependency edges.
    """
    if isinstance(steps_or_workspace, (str, Path)):
        steps = _load_steps_from_workspace(Path(steps_or_workspace).resolve())
    elif isinstance(steps_or_workspace, dict):
        steps = list(steps_or_workspace.get("steps") or [])
    else:
        steps = list(steps_or_workspace)

    norm_target = Path(target_artifact).as_posix().lstrip("./") if target_artifact else None
    needed_files: set[str] = set()
    if norm_target:
        needed_files.add(norm_target)
    else:
        for st in steps:
            for w in (st.get("files_written") or st.get("writes") or ()):
                wp = w.get("path") if isinstance(w, dict) else str(w)
                if wp:
                    needed_files.add(Path(str(wp)).as_posix().lstrip("./"))

    needed_vars: set[str] = set()
    kept_rev: list[dict[str, Any]] = []
    pruned_rev: list[dict[str, Any]] = []

    def _file_matches(written_set: set[str], needed_set: set[str]) -> bool:
        if written_set & needed_set:
            return True
        for wf in written_set:
            for nf in needed_set:
                if wf.endswith(f"/{nf}") or nf.endswith(f"/{wf}") or Path(wf).name == nf:
                    return True
        return False

    for st in reversed(steps):
        code = str(st.get("code") or st.get("code_preview") or st.get("source") or "")
        ovs = st.get("object_versions") if isinstance(st.get("object_versions"), list) else []
        defs, uses = _extract_cell_def_use(code, ovs)
        raw_writes = st.get("files_written") if st.get("files_written") is not None else (st.get("writes") or ())
        written = {
            Path(str(w.get("path") if isinstance(w, dict) else w)).as_posix().lstrip("./")
            for w in raw_writes
            if (w.get("path") if isinstance(w, dict) else w)
        }
        if not written and code:
            for m in re.finditer(
                r"(?:to_csv|to_parquet|to_tsv|savefig|write_h5ad|write_csv|write_text)\(\s*['\"]([^'\"]+)['\"]",
                code,
            ):
                if m.group(1):
                    written.add(Path(m.group(1)).as_posix().lstrip("./"))
        raw_reads = st.get("files_read") if st.get("files_read") is not None else (st.get("reads") or ())
        read = {
            Path(str(r.get("path") if isinstance(r, dict) else r)).as_posix().lstrip("./")
            for r in raw_reads
            if (r.get("path") if isinstance(r, dict) else r)
        }

        produces_needed_file = _file_matches(written, needed_files)
        produces_needed_var = bool(defs & needed_vars)

        if produces_needed_file or produces_needed_var:
            kept_rev.append(st)
            needed_files.update(read)
            needed_vars.update(uses)
        else:
            pruned_rev.append(st)

    def _step_idx(s: dict[str, Any]) -> int:
        if s.get("index") is not None:
            return int(s["index"])
        if s.get("cell_index") is not None:
            return int(s["cell_index"])
        return 0

    kept_steps = list(reversed(kept_rev))
    pruned_steps = list(reversed(pruned_rev))
    synthesized_blocks = [
        f"# --- Cell {_step_idx(s)}: {s.get('cell_id', '')} (sha256={str(s.get('code_sha256', ''))[:12]}) ---\n{str(s.get('code') or s.get('code_preview') or s.get('source') or '').strip()}\n"
        for s in kept_steps
        if str(s.get("code") or s.get("code_preview") or s.get("source") or "").strip()
    ]
    kept_indices = [_step_idx(s) for s in kept_steps]
    pruned_indices = [_step_idx(s) for s in pruned_steps]
    synth = "\n".join(synthesized_blocks)
    return {
        "target_artifact": norm_target,
        "target_path": norm_target or "",
        "kept_indices": kept_indices,
        "kept_cells": kept_indices,
        "kept_cell_ids": [str(s.get("cell_id", "")) for s in kept_steps],
        "pruned_indices": pruned_indices,
        "pruned_cells": pruned_indices,
        "pruned_cell_ids": [str(s.get("cell_id", "")) for s in pruned_steps],
        "steps": kept_steps,
        "synthesized_code": synth,
        "script": synth,
    }


def slice_for_artifact(
    receipt_or_steps: dict[str, Any] | list[dict[str, Any]] | str | Path,
    artifact_path: str,
    *,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    """Return the backward program slice for ``artifact_path`` from a receipt, workspace, or step list."""
    target_src = workspace if workspace is not None else receipt_or_steps
    return compute_backward_artifact_slice(target_src, target_artifact=artifact_path)


NotebookService = NotebookAnalysis


def _build_review_brief(workspace: Path, receipt: dict[str, Any]) -> str:
    v_mode = receipt.get("verification_mode", "replay")
    lines = [
        "# OmicsClaw Provenance & Replay Review Brief",
        "",
        "## 1. Replay & Integrity Verdict",
        f"- **Verification Mode**: `{v_mode}` (Trace-first provenance verification)",
        f"- **Status**: `{receipt.get('status', 'unknown')}` (exit_code={receipt.get('exit_code')})",
        f"- **Replay Script**: `{receipt.get('script', 'output/reproduce.py')}` (sha256=`{str(receipt.get('script_sha256', ''))[:16]}`)",
        f"- **Verified Outputs**: {len(receipt.get('verified_outputs', []))}",
        f"- **Changed Outputs**: {receipt.get('changed_outputs', [])}",
        f"- **Missing Outputs**: {receipt.get('missing_outputs', [])}",
        f"- **Orphan Outputs**: {receipt.get('orphan_outputs', [])}",
        f"- **Stale Inputs**: {receipt.get('stale_inputs', [])}",
        f"- **Unregistered Bypass Reads**: {receipt.get('unregistered_reads', [])}",
        "",
        "## 2. Executed Cells & Skill Calls",
    ]
    steps = receipt.get("steps") or []
    if not steps:
        lines.append("- No notebook cells recorded; standalone script replay used.")
    for step in steps:
        lines.append(
            f"### Step {step.get('index')} (`{step.get('cell_id')}`, sha256=`{str(step.get('code_sha256', ''))[:12]}`)"
        )
        if step.get("object_versions"):
            ov_parts = []
            for ov in step["object_versions"]:
                prev_v = ov.get("previous_version") if ov.get("previous_version") is not None else ov.get("prev_version")
                transition = f"{ov.get('name')}@v{prev_v}->v{ov.get('version')}" if prev_v else f"{ov.get('name')}@v{ov.get('version')}"
                deltas = []
                for k in ("added_obs", "added_uns", "added_obsm", "added_layers", "added_columns"):
                    if ov.get(k):
                        deltas.append(f"{k}={ov[k]}")
                delta_str = f" ({', '.join(deltas)})" if deltas else ""
                ov_parts.append(f"`{transition}` {ov.get('type')}{list(ov.get('shape', ()))}{delta_str}")
            lines.append("- **Object Versions**: " + "; ".join(ov_parts))
        if step.get("skill_calls"):
            for sc in step["skill_calls"]:
                assump = sc.get("assumptions") or {}
                assump_str = f" assumptions={json.dumps(assump, sort_keys=True)}" if assump else ""
                lines.append(
                    f"- **Skill Call**: `{sc.get('qualified') or sc.get('function')}` args=`{sc.get('args_summary', '')}`{assump_str}"
                )
        if step.get("files_read"):
            lines.append(
                "- **Files Read**: "
                + ", ".join(f"`{r.get('path')}` ({str(r.get('sha256', ''))[:10]})" for r in step["files_read"])
            )
        if step.get("files_written"):
            lines.append(
                "- **Files Written**: "
                + ", ".join(f"`{w.get('path')}` ({str(w.get('sha256', ''))[:10]})" for w in step["files_written"])
            )
        snippet = str(step.get("code_preview") or "").strip()
        if snippet:
            lines.append("```python")
            lines.append(snippet[:1200])
            lines.append("```")

    if receipt.get("assumption_checks"):
        lines.extend(["", "## 2b. Statistical Assumption Contracts"])
        for ac in receipt["assumption_checks"]:
            lines.append(
                f"- `{ac.get('qualified') or ac.get('function')}`: `{json.dumps(ac.get('assumptions', {}), sort_keys=True)}`"
            )

    if receipt.get("artifact_lineage"):
        lines.extend(["", "## 2c. Per-Artifact Backward Program Slices"])
        for art_p, sl_info in sorted(receipt["artifact_lineage"].items()):
            lines.append(
                f"- `{art_p}`: kept_cells={sl_info.get('kept_cell_ids', [])}, pruned_cells={sl_info.get('pruned_cell_ids', [])}"
            )

    lines.extend(["", "## 3. Input Data & Metadata Structure"])
    in_paths = [i["path"] for i in receipt.get("inputs", []) if isinstance(i, dict) and i.get("path")]
    meta_notes = _summarize_input_metadata(workspace, in_paths)
    lines.extend(meta_notes or ["- No input tables discovered."])

    lines.extend(["", "## 4. Output Table & JSON Snapshots"])
    snap_notes: list[str] = []
    for out in receipt.get("verified_outputs", []):
        if not isinstance(out, dict) or not out.get("path"):
            continue
        p = workspace / out["path"]
        if p.suffix.lower() in {".csv", ".tsv"} and p.is_file():
            snap_notes.append(_summarize_table_for_brief(p, max_rows=5))
        elif p.suffix.lower() == ".json" and p.is_file():
            try:
                txt = p.read_text(encoding="utf-8")[:800]
                snap_notes.append(f"- `{out['path']}` (json): `{txt}`")
            except OSError:
                pass
    lines.extend(snap_notes or ["- No verified tabular/JSON outputs."])
    return "\n".join(lines) + "\n"


def export_and_trace(
    notebook_id_or_workspace: str | Path,
    workspace: str | Path | None = None,
    *,
    db: Database | None = None,
    db_path: str | Path | None = None,
    artifact_store: Any = None,
    module: str | None = None,
    registered_inputs: list[str] | None = None,
    slice_dead_cells: bool = False,
) -> dict[str, Any]:
    """Claude Science lightweight 'Trace, don't recompute' provenance verification.

    Synthesizes the clean reproduction script and verifies all inputs, skill sidecars,
    object versions, and output artifact SHA-256 digests directly against the kernel
    audit ledger without spawning a second clean-room recomputation subprocess.
    """
    return export_and_replay(
        notebook_id_or_workspace,
        workspace,
        db=db,
        db_path=db_path,
        artifact_store=artifact_store,
        module=module,
        registered_inputs=registered_inputs,
        replay=False,
        slice_dead_cells=slice_dead_cells,
    )


def export_and_replay(
    notebook_id_or_workspace: str | Path,
    workspace: str | Path | None = None,
    *,
    db: Database | None = None,
    db_path: str | Path | None = None,
    artifact_store: Any = None,
    module: str | None = None,
    python: str | None = None,
    timeout_s: float = 180.0,
    registered_inputs: list[str] | None = None,
    replay: bool = True,
    slice_dead_cells: bool = False,
) -> dict[str, Any]:
    if workspace is None:
        ws = Path(notebook_id_or_workspace).resolve()
        notebook_id = "default"
    else:
        notebook_id = str(notebook_id_or_workspace)
        ws = Path(workspace).resolve()
    own_db = False
    if db is None:
        for cand_db in (
            Path(db_path) if db_path is not None else None,
            ws / ".omicsclaw" / "notebooks.db",
            ws / ".omicsclaw" / "jobs.db",
        ):
            if cand_db is not None and cand_db.is_file():
                db = Database(cand_db)
                own_db = True
                break

    own_jobs_db: Database | None = None
    if artifact_store is None:
        jobs_db_file = ws / ".omicsclaw" / "jobs.db"
        if jobs_db_file.is_file():
            try:
                from omicsclaw.memory.artifacts import ArtifactStore

                own_jobs_db = Database(jobs_db_file)
                artifact_store = ArtifactStore(own_jobs_db)
            except Exception:  # noqa: BLE001
                own_jobs_db = None
                artifact_store = None

    steps: list[dict[str, Any]] = []
    code_blocks: list[str] = []
    language = "python"
    ledger_entries = _read_kernel_ledger(ws)
    ledger_by_eid: dict[str, list[dict[str, Any]]] = {}
    ledger_by_sha: dict[str, list[dict[str, Any]]] = {}
    for entry in ledger_entries:
        eid = str(entry.get("execution_id") or "")
        if eid:
            ledger_by_eid.setdefault(eid, []).append(entry)
        csha = str(entry.get("code_sha256") or "")
        if csha:
            ledger_by_sha.setdefault(csha, []).append(entry)
        raw_entry_code = str(entry.get("code") or "").strip()
        if raw_entry_code:
            norm_csha = hashlib.sha256(raw_entry_code.encode("utf-8")).hexdigest()
            if entry not in ledger_by_sha.get(norm_csha, []):
                ledger_by_sha.setdefault(norm_csha, []).append(entry)

    try:
        if db is not None:
            executions = db.run(
                lambda c: [
                    _execution(r)
                    for r in c.execute(
                        "SELECT * FROM notebook_executions WHERE notebook_id=? ORDER BY created_at ASC, rowid ASC",
                        (notebook_id,),
                    ).fetchall()
                ]
            )
            if not executions:
                executions = db.run(
                    lambda c: [
                        _execution(r)
                        for r in c.execute(
                            "SELECT * FROM notebook_executions WHERE status='completed' ORDER BY created_at ASC, rowid ASC"
                        ).fetchall()
                    ]
                )
            completed_execs = [e for e in executions if e.status == "completed"]
            latest_by_cell: dict[str, ExecutionRecord] = {}
            for ex in completed_execs:
                if ex.cell_id in latest_by_cell:
                    del latest_by_cell[ex.cell_id]
                latest_by_cell[ex.cell_id] = ex

            cells_rows = db.run(
                lambda c: [
                    _cell(r)
                    for r in c.execute(
                        "SELECT * FROM notebook_cells WHERE notebook_id=? ORDER BY cell_index ASC, created_at ASC, rowid ASC",
                        (notebook_id,),
                    ).fetchall()
                ]
            )
            if not cells_rows:
                cells_rows = db.run(
                    lambda c: [
                        _cell(r)
                        for r in c.execute(
                            "SELECT * FROM notebook_cells ORDER BY cell_index ASC, created_at ASC, rowid ASC"
                        ).fetchall()
                    ]
                )
            cells_map = {cell.cell_id: cell for cell in cells_rows}

            for cell in cells_map.values():
                if cell.status == "completed" and cell.source.strip():
                    existing = latest_by_cell.get(cell.cell_id)
                    existing_code = str((existing.provenance or {}).get("code") or "").strip() if existing else ""
                    if (
                        existing is None
                        or (cell.execution_id and cell.execution_id != existing.execution_id)
                        or cell.source.strip() != existing_code
                    ):
                        latest_by_cell[cell.cell_id] = ExecutionRecord(
                            execution_id=cell.execution_id or (existing.execution_id if existing else cell.cell_id),
                            notebook_id=cell.notebook_id,
                            cell_id=cell.cell_id,
                            status="completed",
                            output=cell.output if cell.output is not None else (existing.output if existing else None),
                            provenance={
                                **(existing.provenance if existing and existing.provenance else {}),
                                **(cell.provenance or {}),
                                "code": cell.source,
                                "language": cell.language,
                            },
                        )

            ordered_records = list(latest_by_cell.values())

            for idx, rec in enumerate(ordered_records, 1):
                prov = rec.provenance or {}
                cell_obj = cells_map.get(rec.cell_id)
                fallback_source = (
                    cell_obj.source
                    if (cell_obj is not None and cell_obj.status == "completed")
                    else ""
                )
                code = str(prov.get("code") or fallback_source or "").strip()
                if not code:
                    continue
                code_sha = hashlib.sha256(code.strip().encode("utf-8")).hexdigest()
                language = str(prov.get("language") or (cell_obj.language if cell_obj else "python") or "python")
                out = rec.output if isinstance(rec.output, dict) else {}
                f_read = list(out.get("files_read") or [])
                f_written = list(out.get("files_written") or [])
                s_calls = list(out.get("skill_calls") or [])
                obj_vers = list(out.get("object_versions") or [])
                matched_ledger = list(ledger_by_eid.get(rec.execution_id, []))
                if not matched_ledger:
                    matched_ledger = list(ledger_by_sha.get(code_sha, []))
                for l_entry in matched_ledger:
                    for item in l_entry.get("files_read") or []:
                        if item not in f_read:
                            f_read.append(item)
                    for item in l_entry.get("files_written") or []:
                        if item not in f_written:
                            f_written.append(item)
                    for item in l_entry.get("skill_calls") or []:
                        if item not in s_calls:
                            s_calls.append(item)
                    for item in l_entry.get("object_versions") or []:
                        if item not in obj_vers:
                            obj_vers.append(item)

                norm_written: list[dict[str, Any]] = []
                for w in f_written:
                    if isinstance(w, str):
                        wp = ws / w
                        norm_written.append(
                            {
                                "path": w,
                                "sha256": sha256_file(wp) if wp.is_file() else "",
                            }
                        )
                    elif isinstance(w, dict) and w.get("path"):
                        wp = ws / str(w["path"])
                        w_copy = dict(w)
                        if not w_copy.get("sha256") and wp.is_file():
                            w_copy["sha256"] = sha256_file(wp)
                        norm_written.append(w_copy)

                norm_read: list[dict[str, Any]] = []
                for r in f_read:
                    if isinstance(r, str):
                        rp = ws / r
                        norm_read.append(
                            {
                                "path": r,
                                "sha256": sha256_file(rp) if rp.is_file() else "",
                            }
                        )
                    elif isinstance(r, dict) and r.get("path"):
                        rp = ws / str(r["path"])
                        r_copy = dict(r)
                        if not r_copy.get("sha256") and rp.is_file():
                            r_copy["sha256"] = sha256_file(rp)
                        norm_read.append(r_copy)

                steps.append(
                    {
                        "index": idx,
                        "cell_id": rec.cell_id,
                        "execution_id": rec.execution_id,
                        "language": language,
                        "code_sha256": code_sha,
                        "files_read": norm_read,
                        "files_written": norm_written,
                        "skill_calls": s_calls,
                        "object_versions": obj_vers,
                        "code_preview": code[:1200],
                        "code": code,
                    }
                )
                code_blocks.append(f"# --- Cell {idx}: {rec.cell_id} (sha256={code_sha[:12]}) ---\n{code}\n")
    finally:
        if own_db and db is not None:
            db.close()

    try:
        if not steps and ledger_entries:
            step_idx = 0
            for l_entry in ledger_entries:
                if l_entry.get("status", "ok") not in ("ok", "completed"):
                    continue
                code = str(l_entry.get("code") or "").strip()
                if not code and not (
                    l_entry.get("files_read")
                    or l_entry.get("files_written")
                    or l_entry.get("skill_calls")
                ):
                    continue
                step_idx += 1
                code_sha = hashlib.sha256(code.encode("utf-8")).hexdigest()
                steps.append(
                    {
                        "index": step_idx,
                        "cell_id": str(l_entry.get("cell_id") or f"cell-{step_idx}"),
                        "execution_id": str(l_entry.get("execution_id") or f"exec-{step_idx}"),
                        "language": "python",
                        "code_sha256": code_sha,
                        "files_read": list(l_entry.get("files_read") or []),
                        "files_written": list(l_entry.get("files_written") or []),
                        "skill_calls": list(l_entry.get("skill_calls") or []),
                        "object_versions": list(l_entry.get("object_versions") or []),
                        "code_preview": code[:1200],
                        "code": code,
                    }
                )
                if code:
                    code_blocks.append(f"# --- Cell {step_idx} ---\n{code}\n")

        prov_dir = ws / "provenance"
        prov_dir.mkdir(parents=True, exist_ok=True)
        hidden_prov_dir = ws / ".omicsclaw" / "provenance"
        hidden_prov_dir.mkdir(parents=True, exist_ok=True)
        out_dir = ws / "output"
        out_dir.mkdir(parents=True, exist_ok=True)

        hidden_ledger = hidden_prov_dir / "kernel_ledger.jsonl"
        public_ledger = prov_dir / "kernel_ledger.jsonl"
        if not hidden_ledger.is_file() and steps:
            synth_lines = []
            for step in steps:
                synth_lines.append(
                    json.dumps(
                        {
                            "session_id": notebook_id,
                            "cell_id": step.get("cell_id"),
                            "execution_id": step.get("execution_id"),
                            "status": "ok",
                            "code_sha256": step.get("code_sha256"),
                            "code": step.get("code_preview", ""),
                            "files_read": step.get("files_read", []),
                            "files_written": step.get("files_written", []),
                            "skill_calls": step.get("skill_calls", []),
                            "object_versions": step.get("object_versions", []),
                        },
                        sort_keys=True,
                    )
                )
            hidden_ledger.write_text("\n".join(synth_lines) + "\n", encoding="utf-8")
        if hidden_ledger.is_file():
            ledger_txt = hidden_ledger.read_text(encoding="utf-8")
            public_ledger.write_text(ledger_txt, encoding="utf-8")
            (hidden_prov_dir / "session_ledger.jsonl").write_text(ledger_txt, encoding="utf-8")
            (prov_dir / "session_ledger.jsonl").write_text(ledger_txt, encoding="utf-8")

        inputs_by_path: dict[str, dict[str, Any]] = {}
        outputs_by_path: dict[str, dict[str, Any]] = {}
        skill_files_by_path: dict[str, dict[str, Any]] = {}
        all_skill_calls: list[dict[str, Any]] = []
        all_object_versions: list[dict[str, Any]] = []
        assumption_checks: list[dict[str, Any]] = []
        stale_inputs: list[str] = []
        discovered_skills: dict[str, str] = {}

        for mf in (
            prov_dir / "skills_loaded.json",
            hidden_prov_dir / "skills_loaded.json",
        ):
            if mf.is_file():
                try:
                    raw_loaded = json.loads(mf.read_text(encoding="utf-8"))
                    if isinstance(raw_loaded, list):
                        for item in raw_loaded:
                            if isinstance(item, dict) and item.get("skill"):
                                s_n = str(item["skill"])
                                k_p = str(item.get("kernel_path") or item.get("staged_path") or "")
                                discovered_skills.setdefault(s_n, k_p)
                except Exception:  # noqa: BLE001
                    pass

        for sidecar_root in (prov_dir / "sidecars", out_dir / "sidecars"):
            if sidecar_root.is_dir():
                for kp_cand in sorted(sidecar_root.glob("*/kernel.py")):
                    discovered_skills.setdefault(kp_cand.parent.name, str(kp_cand.resolve()))

        for l_entry in ledger_entries:
            for sl in l_entry.get("skills_loaded") or []:
                if isinstance(sl, dict) and sl.get("skill"):
                    discovered_skills.setdefault(
                        str(sl["skill"]),
                        str(sl.get("kernel_path") or ""),
                    )
            for sc in l_entry.get("skill_calls") or []:
                if isinstance(sc, dict) and sc.get("skill"):
                    discovered_skills.setdefault(
                        str(sc["skill"]),
                        str(sc.get("kernel_path") or ""),
                    )

        for step in steps:
            for ov in step.get("object_versions") or []:
                if isinstance(ov, dict):
                    all_object_versions.append({**ov, "cell_id": step.get("cell_id"), "step_index": step.get("index")})
            for w in step.get("files_written") or []:
                p = str(w.get("path") or "")
                if p and not p.startswith((".omicsclaw/", "provenance/")):
                    outputs_by_path[p] = w
            for r in step.get("files_read") or []:
                p = str(r.get("path") or "")
                if not p or p in outputs_by_path or p.startswith((".omicsclaw/", "provenance/")):
                    continue
                rp = ws / p
                cur_sha = sha256_file(rp) if rp.is_file() else ""
                rec_sha = str(r.get("sha256") or "")
                if rec_sha and cur_sha and rec_sha != cur_sha:
                    stale_inputs.append(p)
                inputs_by_path.setdefault(
                    p,
                    {
                        "path": p,
                        "sha256": rec_sha or cur_sha,
                        "current_sha256": cur_sha,
                    },
                )
            for sc in step.get("skill_calls") or []:
                if isinstance(sc, dict):
                    all_skill_calls.append(sc)
                    if sc.get("assumptions"):
                        assumption_checks.append(
                            {
                                "skill": sc.get("skill"),
                                "function": sc.get("function"),
                                "qualified": sc.get("qualified"),
                                "cell_id": step.get("cell_id"),
                                "assumptions": sc.get("assumptions"),
                            }
                        )
                    s_name = str(sc.get("skill") or "")
                    kpath = str(sc.get("kernel_path") or "")
                    if s_name:
                        discovered_skills.setdefault(s_name, kpath)
                    if kpath or s_name:
                        resolved_kp = _resolve_skill_kernel_path(ws, kpath, s_name)
                        cur_ksha = sha256_file(resolved_kp) if (resolved_kp is not None and resolved_kp.is_file()) else ""
                        rec_ksha = str(sc.get("kernel_sha256") or cur_ksha)
                        key_path = kpath or (str(resolved_kp) if resolved_kp is not None else s_name)
                        if rec_ksha and cur_ksha and rec_ksha != cur_ksha:
                            stale_inputs.append(key_path)
                        skill_files_by_path[key_path] = {
                            "path": key_path,
                            "sha256": rec_ksha,
                            "current_sha256": cur_ksha,
                        }

        artifact_lineage: dict[str, dict[str, Any]] = {}
        for out_rel in outputs_by_path:
            sl = compute_backward_artifact_slice(steps, target_artifact=out_rel)
            artifact_lineage[out_rel] = {
                "target_artifact": out_rel,
                "target_path": out_rel,
                "kept_indices": sl["kept_indices"],
                "kept_cells": sl["kept_indices"],
                "kept_cell_ids": sl["kept_cell_ids"],
                "pruned_indices": sl["pruned_indices"],
                "pruned_cells": sl["pruned_indices"],
                "pruned_cell_ids": sl["pruned_cell_ids"],
                "synthesized_code": sl["synthesized_code"],
                "script": sl["script"],
            }

        if slice_dead_cells and steps:
            global_slice = compute_backward_artifact_slice(steps, target_artifact=None)
            if global_slice["steps"]:
                code_blocks = [
                    f"# --- Cell {s.get('index')}: {s.get('cell_id')} (sha256={str(s.get('code_sha256', ''))[:12]}) ---\n{str(s.get('code') or s.get('code_preview') or '').strip()}\n"
                    for s in global_slice["steps"]
                ]

        bootstrap_sidecars: list[tuple[str, str]] = []
        for s_name, raw_kpath in discovered_skills.items():
            resolved_kp = _resolve_skill_kernel_path(ws, raw_kpath, s_name)
            if resolved_kp is not None and resolved_kp.is_file():
                try:
                    k_src = resolved_kp.read_text(encoding="utf-8")
                    for stage_base in (prov_dir / "sidecars" / s_name, out_dir / "sidecars" / s_name):
                        stage_base.mkdir(parents=True, exist_ok=True)
                        (stage_base / "kernel.py").write_text(k_src, encoding="utf-8")
                except OSError:
                    pass
                bootstrap_sidecars.append((s_name, str(resolved_kp)))
            else:
                bootstrap_sidecars.append((s_name, raw_kpath))

        ext = ".R" if language.lower() == "r" else ".py"
        user_reproduce = out_dir / f"reproduce{ext}"
        prov_reproduce = prov_dir / f"reproduce{ext}"

        if code_blocks:
            if ext == ".py":
                from omicsclaw.skills.sidecar import build_sidecar_bootstrap_code

                inline_bootstrap = build_sidecar_bootstrap_code(bootstrap_sidecars, workspace=ws)
                staged_args = ", ".join(
                    f"({s_name!r}, str(_oc_Path(__file__).resolve().parent / 'sidecars' / {s_name!r} / 'kernel.py'))"
                    for s_name, _ in bootstrap_sidecars
                )
                dynamic_call = (
                    f"exec(_oc_bootstrap([{staged_args}]), globals())"
                    if staged_args
                    else "exec(_oc_bootstrap(), globals())"
                )
                host_shim = r"""
if "host" not in globals():
    class _ReplayFindingsResult(list):
        def __getitem__(self, key):
            if isinstance(key, str):
                if key == "findings":
                    return list(self)
                if key == "count":
                    return len(self)
                raise KeyError(key)
            return super().__getitem__(key)
        def get(self, key, default=None):
            if key == "findings":
                return list(self)
            if key == "count":
                return len(self)
            return default
    class _ReplayFindings:
        def __call__(self, status="unresolved", limit=50):
            import json as _j, pathlib as _p
            ws_r = _p.Path.cwd()
            items = []
            for rel in (".omicsclaw/provenance/findings.json", "provenance/findings.json"):
                fp = ws_r / rel
                if fp.is_file():
                    try:
                        raw = _j.loads(fp.read_text(encoding="utf-8"))
                        items = raw.get("findings", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
                        break
                    except Exception:
                        pass
            if status != "all":
                if status in ("unresolved", "open"):
                    items = [f for f in items if f.get("status", "unresolved") in ("unresolved", "open", "unaddressed")]
                elif status in ("addressed", "claimed"):
                    items = [f for f in items if f.get("status", "unresolved") in ("addressed", "claimed", "resolved")]
            return _ReplayFindingsResult(items[:limit])
        def mark_addressed(self, ids, note=""):
            if isinstance(ids, str):
                ids = [ids]
            return {"addressed": list(ids), "count": len(ids)}
    class _ReplayHost:
        def __init__(self):
            self.findings = _ReplayFindings()
        def artifacts(self, filename=None, exact=False, kind=None, limit=200):
            return []
        def artifact_path(self, vid):
            import pathlib as _p
            from omicsclaw.runs.verification import resolve_artifact_reference as _rar
            res = _rar(str(vid), _p.Path.cwd())
            if res is not None:
                return str(res)
            raise FileNotFoundError(f"host.artifact_path: unknown artifact {vid!r}")
        def artifact_marker(self, vid, escaped=False):
            raw = str(vid or "").strip()
            prefix = "{{!artifact:" if escaped else "{{artifact:"
            return f"{prefix}{raw}}}}}"
    host = _ReplayHost()
"""
                header = (
                    "# Auto-generated clean-room replay script by OmicsClaw\n"
                    f"{inline_bootstrap}\n"
                    f"{host_shim}\n"
                    "try:\n"
                    "    from pathlib import Path as _oc_Path\n"
                    "    from omicsclaw.skills.sidecar import build_sidecar_bootstrap_code as _oc_bootstrap\n"
                    f"    {dynamic_call}\n"
                    "except Exception:\n"
                    "    pass\n"
                )
            else:
                header = "# Auto-generated R replay script\n"
            resolved_blocks = [
                resolve_code_artifact_markers(b, ws, artifact_store=artifact_store)
                for b in code_blocks
            ]
            synthesized = header + "\n".join(resolved_blocks)
            prov_reproduce.write_text(synthesized, encoding="utf-8")
            user_reproduce.write_text(synthesized, encoding="utf-8")
            target_script = user_reproduce
        else:
            target_script = user_reproduce if user_reproduce.is_file() else (ws / f"reproduce{ext}")

        if artifact_store is not None:
            try:
                for art in artifact_store.list_artifacts():
                    ap = Path(art.path)
                    try:
                        rel = ap.resolve().relative_to(ws).as_posix()
                    except ValueError:
                        continue
                    if rel.startswith((".omicsclaw/", "provenance/", "output/sidecars/")):
                        continue
                    outputs_by_path.setdefault(rel, {"path": rel, "sha256": art.sha256})
            except Exception:  # noqa: BLE001
                pass

        unregistered_reads: list[str] = []
        if registered_inputs is not None:
            allowed = {Path(x).as_posix().lstrip("./") for x in registered_inputs}
            for p in inputs_by_path:
                norm_p = Path(p).as_posix().lstrip("./")
                if norm_p not in allowed:
                    unregistered_reads.append(norm_p)

        tracked_out_list = list(outputs_by_path.keys()) if outputs_by_path else None
        tracked_in_list = list(inputs_by_path.keys())

        if replay:
            replay_res = run_clean_room_replay(
                target_script,
                ws,
                python=python,
                timeout_s=timeout_s,
                tracked_outputs=tracked_out_list,
                tracked_inputs=tracked_in_list,
            )
            verification_mode = "replay"
        else:
            verification_mode = "trace"
            verified_outputs: list[dict[str, Any]] = []
            changed_outputs: list[str] = []
            missing_outputs: list[str] = []
            orphan_outputs: list[str] = []
            comparison_details: dict[str, Any] = {}

            for rel_p, out_meta in sorted(outputs_by_path.items()):
                fp = ws / rel_p
                if not fp.is_file():
                    missing_outputs.append(rel_p)
                    comparison_details[rel_p] = {"mode": "trace_sha256", "passed": False, "reason": "missing_on_disk"}
                    continue
                cur_sha = sha256_file(fp)
                rec_sha = str(out_meta.get("sha256") or "")
                if rec_sha and cur_sha != rec_sha:
                    changed_outputs.append(rel_p)
                    comparison_details[rel_p] = {
                        "mode": "trace_sha256",
                        "passed": False,
                        "recorded_sha256": rec_sha,
                        "current_sha256": cur_sha,
                    }
                else:
                    verified_outputs.append(
                        {
                            "path": rel_p,
                            "sha256": cur_sha,
                            "size_bytes": fp.stat().st_size,
                            "comparison_mode": "trace_sha256",
                        }
                    )
                    comparison_details[rel_p] = {
                        "mode": "trace_sha256",
                        "passed": True,
                        "sha256": cur_sha,
                    }

            known_out = {Path(k).as_posix().lstrip("./") for k in outputs_by_path}
            for scan_dir_name in ("output", "tables", "figures"):
                scan_dir = ws / scan_dir_name
                if not scan_dir.is_dir():
                    continue
                for cand_f in sorted(scan_dir.rglob("*")):
                    if not cand_f.is_file():
                        continue
                    rel_c = cand_f.relative_to(ws).as_posix()
                    if rel_c in ("output/reproduce.py", "output/reproduce.R", "output/REPORT.md"):
                        continue
                    if rel_c.startswith(("output/sidecars/", "provenance/", ".omicsclaw/")):
                        continue
                    if rel_c not in known_out:
                        orphan_outputs.append(rel_c)

            script_rel = target_script.relative_to(ws).as_posix() if target_script.is_file() and target_script.is_relative_to(ws) else f"output/reproduce{ext}"
            script_sha = sha256_file(target_script) if target_script.is_file() else ""
            trace_ok = (
                bool(steps or verified_outputs)
                and not changed_outputs
                and not missing_outputs
                and not orphan_outputs
            )
            replay_res = {
                "status": "ok" if trace_ok else "failed",
                "exit_code": 0 if trace_ok else 1,
                "script": script_rel,
                "script_sha256": script_sha,
                "verified_outputs": verified_outputs,
                "changed_outputs": changed_outputs,
                "missing_outputs": missing_outputs,
                "orphan_outputs": orphan_outputs,
                "comparison_details": comparison_details,
            }

        report_path = ""
        report_sha256 = ""
        for candidate_rep in ("output/REPORT.md", "REPORT.md"):
            rp = ws / candidate_rep
            if rp.is_file():
                report_path = candidate_rep
                report_sha256 = sha256_file(rp)
                break

        serialized_steps = [{k: v for k, v in st.items() if k != "code"} for st in steps]

        overall_ok = (
            replay_res.get("status") == "ok"
            and not stale_inputs
            and not unregistered_reads
        )
        receipt: dict[str, Any] = {
            "version": 1,
            "notebook_id": notebook_id,
            "workspace": str(ws),
            "created_at": time.time(),
            "verification_mode": verification_mode,
            "mode": verification_mode,
            "recomputed": bool(replay),
            "status": "ok" if overall_ok else "failed",
            "replay_verified": bool(overall_ok),
            "trace_verified": bool(overall_ok),
            "exit_code": replay_res.get("exit_code", -1),
            "returncode": replay_res.get("exit_code", -1),
            "script": replay_res.get("script", f"output/reproduce{ext}"),
            "script_sha256": replay_res.get("script_sha256", ""),
            "steps": serialized_steps,
            "inputs": list(inputs_by_path.values()),
            "skill_files": list(skill_files_by_path.values()),
            "skill_calls": all_skill_calls,
            "object_versions": all_object_versions,
            "assumption_checks": assumption_checks,
            "artifact_lineage": artifact_lineage,
            "verified_outputs": replay_res.get("verified_outputs", []),
            "changed_outputs": replay_res.get("changed_outputs", []),
            "missing_outputs": replay_res.get("missing_outputs", []),
            "orphan_outputs": replay_res.get("orphan_outputs", []),
            "stale_inputs": sorted(set(stale_inputs)),
            "unregistered_reads": sorted(set(unregistered_reads)),
            "comparison_details": replay_res.get("comparison_details", {}),
            "report_path": report_path,
            "report_sha256": report_sha256,
        }

        brief_md = _build_review_brief(ws, receipt)
        (prov_dir / "replay_receipt.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8"
        )
        (prov_dir / "review_brief.md").write_text(brief_md, encoding="utf-8")

        target_modules: list[str] = []
        if module:
            target_modules.append(module)
        results_root = ws / "results"
        if results_root.is_dir():
            for child in sorted(results_root.iterdir()):
                if child.is_dir() and child.name not in target_modules:
                    target_modules.append(child.name)

        for mod_name in target_modules:
            mod_prov = ws / "results" / mod_name / "provenance"
            mod_prov.mkdir(parents=True, exist_ok=True)
            (mod_prov / "replay_receipt.json").write_text(
                json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8"
            )
            (mod_prov / "review_brief.md").write_text(brief_md, encoding="utf-8")
            if public_ledger.is_file():
                ledger_content = public_ledger.read_text(encoding="utf-8")
                (mod_prov / "kernel_ledger.jsonl").write_text(
                    ledger_content, encoding="utf-8"
                )
                (mod_prov / "session_ledger.jsonl").write_text(
                    ledger_content, encoding="utf-8"
                )

        return receipt
    finally:
        if own_jobs_db is not None:
            own_jobs_db.close()
