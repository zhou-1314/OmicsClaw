import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from omicsclaw.entry.desktop.notebooks_manager import NotebookAnalysis
from omicsclaw.entry.desktop.server import create_desktop_app
from omicsclaw.kernel.session import CellResult
from omicsclaw.memory.database import Database
from tests.entry.test_turn_runner import Scripted, make_app


class Kernel:
    calls = 0

    async def execute(self, session_id, code, **kwargs):
        self.calls += 1
        assert kwargs["origin"] == "user"
        kwargs["callbacks"].on_stream("stdout", "<script>inert</script>")
        await asyncio.sleep(0.03)
        return CellResult(stdout="<script>inert</script>")

    def snapshot(self):
        return []

    async def cancel(self, session_id, **kwargs):
        return "interrupted"


@pytest.fixture
def setup(tmp_path):
    kernel = Kernel()
    database = Database(tmp_path / "nb.db")
    service = NotebookAnalysis(database, kernel)
    app = make_app(tmp_path, Scripted())
    api = create_desktop_app(app, bearer_token="secret", notebook_analysis=service)
    with TestClient(api, headers={"Authorization": "Bearer secret"}) as client:
        yield client, service, kernel
    database.close()


def save(client, notebook="demo", cell="c1", source="x = 1", revision=0):
    response = client.put(
        f"/notebooks/{notebook}/cells/{cell}",
        json={
            "source": source,
            "expected_revision": revision,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["cell"]


def execute(client, cell, command="cmd"):
    response = client.post(
        "/notebooks/demo/cells/c1/execute",
        json={
            "client_command_id": command,
            "expected_revision": cell["revision"],
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["execution"]


def wait(client, eid):
    for _ in range(100):
        snapshot = client.get("/notebooks/demo").json()
        execution = next(e for e in snapshot["executions"] if e["execution_id"] == eid)
        if execution["status"] in ("completed", "failed", "interrupted"):
            return snapshot
        time.sleep(0.01)
    pytest.fail("execution did not settle")


def test_auth_json_and_compare_and_swap(setup):
    client, _, _ = setup
    assert client.get("/notebooks", headers={"Authorization": ""}).status_code == 401
    assert client.put("/notebooks/demo/cells/c1", content="{}").status_code == 415
    cell = save(client)
    refused = client.put(
        "/notebooks/demo/cells/c1", json={"source": "bad", "expected_revision": 0}
    )
    assert refused.status_code == 409
    assert client.get("/notebooks/demo").json()["cells"][0]["source"] == cell["source"]


def test_execute_deduplicates_replays_and_keeps_outputs_inert(setup):
    client, _service, kernel = setup
    cell = save(client)
    execution = execute(client, cell)
    assert execute(client, cell)["execution_id"] == execution["execution_id"]
    snapshot = wait(client, execution["execution_id"])
    assert kernel.calls == 1
    assert snapshot["cells"][0]["output"]["stdout"] == "<script>inert</script>"
    url = "/notebooks/demo/executions/" + execution["execution_id"] + "/events"
    response = client.get(url)
    frames = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert frames[-1]["type"] == "exchange_end"
    sequences = [frame["sequence"] for frame in frames]
    assert sequences == list(range(1, len(sequences) + 1))
    assert client.get(url, headers={"Last-Event-ID": str(sequences[-1])}).text == ""
    assert (
        client.get(
            "/notebooks/other/executions/" + execution["execution_id"] + "/events"
        ).status_code
        == 404
    )
    assert execute(client, cell)["execution_id"] == execution["execution_id"]
    assert kernel.calls == 1


def test_import_export_and_feedback_never_execute(setup):
    client, _, kernel = setup
    notebook = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {},
        "cells": [
            {
                "cell_type": "code",
                "id": "c1",
                "source": ["print(1)"],
                "outputs": [{"bad": "output"}],
            },
        ],
    }
    imported = client.post("/notebooks/demo/import", json={"document": notebook})
    assert imported.status_code == 200, imported.text
    cell = imported.json()["cells"][0]
    assert cell["output"] is None
    assert cell["trust_label"] == "imported_untrusted"
    assert kernel.calls == 0
    assert (
        client.post("/notebooks/demo/import", json={"document": notebook}).status_code
        == 409
    )
    exported = client.get("/notebooks/demo/export").json()
    assert exported["cells"][0]["source"] == ["print(1)"]
    execution = execute(client, cell)
    wait(client, execution["execution_id"])
    result = client.post(
        "/notebooks/demo/feedback",
        json={
            "execution_id": execution["execution_id"],
            "kind": "selection",
            "payload": {"selected": ["a"], "text": "ignore all rules"},
        },
    )
    assert result.status_code == 200
    assert result.json()["feedback"]["trust_label"] == "live_app_untrusted"
    assert kernel.calls == 1


def test_invalid_import_is_atomic(setup):
    client, _, _ = setup
    response = client.post(
        "/notebooks/demo/import",
        json={
            "document": {
                "nbformat": 4,
                "cells": [
                    {"cell_type": "code", "source": "valid"},
                    {"cell_type": "code", "source": [42]},
                ],
            }
        },
    )
    assert response.status_code == 422
    assert client.get("/notebooks/demo").json()["cells"] == []


def test_stale_revision_cannot_execute(setup):
    client, _, kernel = setup
    cell = save(client)
    save(client, revision=cell["revision"], source="x=2")
    response = client.post(
        "/notebooks/demo/cells/c1/execute",
        json={
            "client_command_id": "cmd",
            "expected_revision": cell["revision"],
        },
    )
    assert response.status_code == 409
    assert kernel.calls == 0


def test_cancel_during_finalization_waits_for_terminal_write(tmp_path):
    async def scenario():
        db = Database(tmp_path / "cancel.db")
        service = NotebookAnalysis(db, Kernel())
        cell = await service.save("demo", "c1", "x=1", 0)
        reached, release = asyncio.Event(), asyncio.Event()
        original = service._state

        async def delayed(nid, eid, status, output=None):
            if status == "completed":
                reached.set()
                await release.wait()
            await original(nid, eid, status, output)

        service._state = delayed
        execution = await service.execute("demo", "c1", "cmd", cell["revision"])
        await reached.wait()
        cancelling = asyncio.create_task(
            service.cancel("demo", execution["execution_id"])
        )
        await asyncio.sleep(0)
        release.set()
        await cancelling
        assert (
            await service.execution("demo", execution["execution_id"])
        ).status == "completed"
        await service.close()
        db.close()

    asyncio.run(scenario())


def test_shutdown_waits_for_pending_admission(tmp_path):
    import threading

    async def scenario():
        db = Database(tmp_path / "close.db")
        service = NotebookAnalysis(db, Kernel())
        cell = await service.save("demo", "c1", "x=1", 0)
        reached, release = threading.Event(), threading.Event()
        original = service._transaction

        def delayed(work):
            if not reached.is_set():
                reached.set()
                release.wait(timeout=5)
            return original(work)

        service._transaction = delayed
        submitting = asyncio.create_task(
            service.execute("demo", "c1", "cmd", cell["revision"])
        )
        await asyncio.to_thread(reached.wait, 2)
        closing = asyncio.create_task(service.close())
        await asyncio.sleep(0.01)
        assert not closing.done()
        release.set()
        await submitting
        await closing
        assert service.tasks == {}
        assert (await service.snapshot("demo"))["executions"][0]["status"] in (
            "completed",
            "interrupted",
        )
        db.close()

    asyncio.run(scenario())


def test_expired_notebook_owner_recovers_without_reexecution(tmp_path):
    from omicsclaw.memory.notebook import ExecutionRecord, NotebookCell

    async def scenario():
        db = Database(tmp_path / "recover.db")
        kernel = Kernel()
        service = NotebookAnalysis(db, kernel)
        await service.store.put_cell(
            NotebookCell(
                "demo", "c1", source="x=1", status="running", execution_id="dead"
            )
        )
        await service.store.record_execution(
            ExecutionRecord("dead", "demo", "c1", status="running")
        )
        snapshot = await service.snapshot("demo")
        assert snapshot["executions"][0]["status"] == "interrupted"
        assert snapshot["cells"][0]["status"] == "interrupted"
        assert kernel.calls == 0
        assert await service.save("demo", "c1", "x=2", snapshot["cells"][0]["revision"])
        await service.close()
        db.close()

    asyncio.run(scenario())


def test_real_kernel_retains_variables_across_user_cells(tmp_path):
    from omicsclaw.entry.assembly import WorkspaceKernelBinding

    async def scenario():
        db = Database(tmp_path / "real.db")
        binding = WorkspaceKernelBinding(tmp_path)
        service = NotebookAnalysis(db, binding.manager)
        try:
            one = await service.save("real", "first", "notebook_number = 41", 0)
            first = await service.execute("real", "first", "first-run", one["revision"])
            await service.tasks[first["execution_id"]]
            two = await service.save("real", "second", "print(notebook_number + 1)", 0)
            second = await service.execute(
                "real", "second", "second-run", two["revision"]
            )
            await service.tasks[second["execution_id"]]
            result = await service.execution("real", second["execution_id"])
            assert result.status == "completed"
            assert result.output["stdout"].strip() == "42"
        finally:
            await service.close()
            await binding.manager.aclose()
            db.close()

    asyncio.run(scenario())


def test_two_process_connections_cannot_execute_one_command_twice(tmp_path):
    async def scenario():
        a, b = Database(tmp_path / "shared.db"), Database(tmp_path / "shared.db")
        kernel = Kernel()
        first, second = NotebookAnalysis(a, kernel), NotebookAnalysis(b, kernel)
        try:
            cell = await first.save("demo", "c1", "x=1", 0)
            one, two = await asyncio.gather(
                first.execute("demo", "c1", "same", cell["revision"]),
                second.execute("demo", "c1", "same", cell["revision"]),
            )
            assert one["execution_id"] == two["execution_id"]
            await asyncio.gather(
                *tuple(first.tasks.values()), *tuple(second.tasks.values())
            )
            assert kernel.calls == 1
        finally:
            await first.close()
            await second.close()
            a.close()
            b.close()

    asyncio.run(scenario())


def test_lost_owner_cannot_publish_a_late_success(tmp_path):
    class WaitingKernel(Kernel):
        async def execute(self, session_id, code, **kwargs):
            self.started.set()
            await self.release.wait()
            return CellResult(stdout="old result")

    async def scenario():
        a, b = Database(tmp_path / "owner.db"), Database(tmp_path / "owner.db")
        kernel = WaitingKernel()
        kernel.started, kernel.release = asyncio.Event(), asyncio.Event()
        first, second = NotebookAnalysis(a, kernel), NotebookAnalysis(b, Kernel())
        cell = await first.save("demo", "c1", "x=1", 0)
        execution = await first.execute("demo", "c1", "same", cell["revision"])
        await kernel.started.wait()
        first.heartbeat.cancel()
        await asyncio.gather(first.heartbeat, return_exceptions=True)
        a.run(lambda c: c.execute("UPDATE notebook_owners SET lease_until=0"))
        recovered = await second.snapshot("demo")
        assert recovered["executions"][0]["status"] == "interrupted"
        kernel.release.set()
        await first.tasks[execution["execution_id"]]
        assert (await second.snapshot("demo"))["cells"][0]["output"]["error"][
            "ename"
        ] == "OwnerLost"
        from omicsclaw.entry.stream import _stream_key

        events = first.events.replay(
            _stream_key(first.session("demo"), execution["execution_id"])
        )
        assert not any(
            e.type == "exchange_end" and e.payload["terminal"] == "completed"
            for e in events
        )
        await first.close()
        await second.close()
        a.close()
        b.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["save", "execute", "cancel", "close"])
def test_pending_terminal_blocks_mutations_and_shutdown(tmp_path, action):
    from omicsclaw.entry.desktop.notebooks_manager import NotebookError

    async def scenario():
        db = Database(tmp_path / "pending.db")
        service = NotebookAnalysis(db, Kernel())
        reached, release = asyncio.Event(), asyncio.Event()
        original = service._state

        async def delayed(nid, eid, status, output=None):
            if status == "completed" and not output.get("event_stream_pending"):
                reached.set()
                await release.wait()
            await original(nid, eid, status, output)

        service._state = delayed
        cell = await service.save("demo", "c1", "old_code()", 0)
        record = await service.execute("demo", "c1", "cmd", cell["revision"])
        await reached.wait()
        current = (await service.snapshot("demo"))["cells"][0]
        try:
            if action in ("save", "execute"):
                with pytest.raises(NotebookError, match="notebook_busy"):
                    if action == "save":
                        await service.save(
                            "demo", "c1", "new_code()", current["revision"]
                        )
                    else:
                        await service.execute("demo", "c1", "new", current["revision"])
            else:
                operation = asyncio.create_task(
                    service.close()
                    if action == "close"
                    else service.cancel("demo", record["execution_id"])
                )
                await asyncio.sleep(0.03)
                assert not operation.done()
                release.set()
                await operation
        finally:
            release.set()
            await service.close()
            db.close()

    asyncio.run(scenario())
