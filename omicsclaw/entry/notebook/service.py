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
from omicsclaw.memory.notebook import ExecutionRecord, NotebookCell, NotebookStore
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
            if cell is None and source is not None:
                cell = NotebookCell(
                    nid,
                    cid,
                    source=source,
                    provenance={"source": "agent_run"},
                    language=semantic["language"] if semantic else "python",
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
                "language": semantic["language"] if semantic else cell.language,
                "source_revision": expected,
                "code": cell.source,
                "session_id": kernel_session,
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
                "execution_count": result.execution_count,
                "usage": result.usage,
            }
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

    @staticmethod
    def _feedback(row):
        return {
            **dict(row),
            "payload": json.loads(row["payload"]),
            "trust_label": "live_app_untrusted",
            "provenance": {"source": "live_app", "execution_id": row["execution_id"]},
        }

    async def feedback(self, nid, document):
        record = await self.execution(nid, document.get("execution_id"))
        kind, payload = document.get("kind"), document.get("payload")
        if kind not in ("selection", "note") or not isinstance(payload, dict):
            raise NotebookError("invalid_feedback")
        encoded = json.dumps(payload, allow_nan=False)
        if len(encoded.encode()) > 16384:
            raise NotebookError("feedback_too_large", 413)
        fid = uuid.uuid4().hex

        def work(c):
            c.execute(
                "INSERT INTO notebook_feedback VALUES(?,?,?,?,?,?)",
                (fid, nid, record.execution_id, kind, encoded, time.time()),
            )
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
