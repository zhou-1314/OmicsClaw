import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from omicsclaw.entry.desktop.notebooks_manager import NotebookAnalysis, NotebookError
from omicsclaw.entry.desktop.server import create_desktop_app
from omicsclaw.entry.notebook_context import NotebookContext
from omicsclaw.memory.database import Database
from omicsclaw.memory.notebook import ExecutionRecord
from omicsclaw.tools.context import use_tool_context
from tests.entry.test_turn_runner import Scripted, make_app


@pytest.fixture
def analysis(tmp_path):
    db = Database(tmp_path / "apps.db")
    service = NotebookAnalysis(db, None)
    service.bind("n", "chat")
    asyncio.run(
        service.store.record_execution(
            ExecutionRecord(
                execution_id="e",
                notebook_id="n",
                cell_id="c",
                status="completed",
                output={"stdout": "42", "event_stream_pending": False},
            )
        )
    )
    yield service
    asyncio.run(service.close())
    db.close()


def document(**changes):
    return {
        "title": "Scatter",
        "html": "<button>Pick</button>",
        "execution_id": "e",
        "expected_revision": 0,
        **changes,
    }


def state(**changes):
    return {
        "app_revision": 1,
        "client_event_id": "event",
        "expected_revision": 0,
        "kind": "view",
        "payload": {"view_id": "scatter", "parameters": {"threshold": 0.5}},
        **changes,
    }


def test_app_persists_and_snapshot_only_exposes_metadata(analysis, tmp_path):
    async def scenario():
        saved = await analysis.apps.publish("n", document())
        assert saved["revision"] == 1
        assert saved["trust_label"] == "live_app_untrusted"
        assert "html" not in (await analysis.snapshot("n"))["live_app"]
        db = Database(tmp_path / "apps.db")
        restored = NotebookAnalysis(db, None)
        try:
            assert await restored.apps.read("n") == saved
        finally:
            await restored.close()
            db.close()

    asyncio.run(scenario())


def test_app_cas_and_immutable_revision(analysis):
    async def scenario():
        await analysis.apps.publish("n", document())
        with pytest.raises(NotebookError, match="app_revision_conflict"):
            await analysis.apps.publish("n", document(title="stale"))
        updated = await analysis.apps.publish(
            "n", document(title="Updated", expected_revision=1)
        )
        assert updated["revision"] == 2
        assert (
            await analysis.db.arun(
                lambda c: c.execute("SELECT count(*) FROM notebook_apps").fetchone()[0]
            )
            == 2
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes",
    [
        {"html": "x" * (256 * 1024 + 1)},
        {"html": " "},
        {"title": "x" * 121},
        {"expected_revision": True},
        {"trust_label": "trusted"},
    ],
)
def test_invalid_app_does_not_publish(analysis, changes):
    async def scenario():
        with pytest.raises(NotebookError):
            await analysis.apps.publish("n", document(**changes))
        assert await analysis.apps.read("n") is None

    asyncio.run(scenario())


def test_app_requires_own_completed_execution_and_binding(analysis):
    async def scenario():
        for status in ("running", "failed", "interrupted"):
            await analysis.store.record_execution(
                ExecutionRecord(
                    execution_id=status, notebook_id="n", cell_id="c", status=status
                )
            )
            with pytest.raises(NotebookError, match="execution_not_completed"):
                await analysis.apps.publish("n", document(execution_id=status))
        with pytest.raises(NotebookError, match="execution_not_found"):
            await analysis.apps.publish("n", document(execution_id="missing"))
        with pytest.raises(NotebookError, match="notebook_not_bound"):
            await analysis.apps.publish("other", document())

    asyncio.run(scenario())


def test_hosted_state_has_server_provenance_and_exact_replay(analysis):
    async def scenario():
        await analysis.apps.publish("n", document())
        saved = await analysis.apps.publish_state("n", state())
        assert saved["execution_id"] == "e"
        assert saved["provenance"]["app_revision"] == 1
        assert saved["trust_label"] == "live_app_untrusted"
        assert await analysis.apps.publish_state("n", state()) == saved
        with pytest.raises(NotebookError, match="state_revision_conflict"):
            await analysis.apps.publish_state("n", state(client_event_id="next"))
        with pytest.raises(NotebookError):
            await analysis.apps.publish_state("n", state(execution_id="foreign"))
        await analysis.apps.publish("n", document(expected_revision=1))
        with pytest.raises(NotebookError, match="app_revision_conflict"):
            await analysis.apps.publish_state("n", state())
        assert (await analysis.context.snapshot("n"))["live_state"] == saved

    asyncio.run(scenario())


def test_context_carries_app_metadata_but_not_html(analysis):
    async def scenario():
        await analysis.apps.publish(
            "n", document(html="<script>secret_html_marker</script>")
        )
        messages = await NotebookContext(analysis, "chat").augment(())
        assert '"live_app"' in messages[0].content
        assert "secret_html_marker" not in messages[0].content

    asyncio.run(scenario())


def test_http_hosted_app_routes_are_authenticated(analysis, tmp_path):
    app = make_app(tmp_path, Scripted())
    api = create_desktop_app(app, bearer_token="secret", notebook_analysis=analysis)
    with TestClient(api) as client:
        headers = {"Authorization": "Bearer secret"}
        assert client.get("/notebooks/n/live-app").status_code == 401
        assert client.put("/notebooks/n/live-app", json=document()).status_code == 401
        assert (
            client.post("/notebooks/n/live-app/state", json=state()).status_code == 401
        )
        assert (
            client.put(
                "/notebooks/n/live-app", json=document(), headers=headers
            ).status_code
            == 200
        )
        assert (
            client.get("/notebooks/n/live-app", headers=headers).json()["live_app"][
                "html"
            ]
            == document()["html"]
        )
        assert (
            client.post(
                "/notebooks/n/live-app/state", json=state(), headers=headers
            ).status_code
            == 200
        )


def test_agent_publish_approval_and_session_binding(tmp_path):
    async def scenario():
        app = make_app(tmp_path, Scripted())
        service = app.kernels.notebooks
        service.bind("n", "chat")
        await service.store.record_execution(
            ExecutionRecord(
                execution_id="e", notebook_id="n", cell_id="c", status="completed"
            )
        )
        tool = app.registry.get("live_app")
        approvals = []

        async def approve(request):
            approvals.append(request.tool_name)
            return True

        try:
            with use_tool_context(approval=approve, values={"session_id": "chat"}):
                result = json.loads(
                    await tool.execute(json.dumps({"action": "publish", **document()}))
                )
                assert result["live_app"]["revision"] == 1
                read = json.loads(await tool.execute('{"action":"read"}'))
                assert read["live_app"]["html"] == document()["html"]
                assert approvals == ["live_app"]
            with (
                use_tool_context(
                    approval=lambda _: False, values={"session_id": "chat"}
                ),
                pytest.raises(Exception, match="[Dd]enied|[Rr]ejected|[Nn]ot approved"),
            ):
                await tool.execute(
                    json.dumps({"action": "publish", **document(expected_revision=1)})
                )
            with (
                use_tool_context(approval=approve, values={"session_id": "other"}),
                pytest.raises(NotebookError, match="notebook_not_bound"),
            ):
                await tool.execute('{"action":"read"}')
            assert (await service.apps.read("n"))["revision"] == 1
        finally:
            await app.aclose()

    asyncio.run(scenario())
