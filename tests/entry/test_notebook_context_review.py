import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from omicsclaw.entry.desktop.notebooks_manager import NotebookAnalysis, NotebookError
from omicsclaw.entry.desktop.server import create_desktop_app
from omicsclaw.entry.notebook_context import MAX_CONTEXT_BYTES, NotebookContext
from omicsclaw.memory.database import Database
from omicsclaw.memory.notebook import ExecutionRecord, NotebookCell
from omicsclaw.schema import Role
from omicsclaw.tools.context import use_tool_context
from tests.entry.test_turn_runner import Scripted, make_app


@pytest.fixture
def analysis(tmp_path):
    database = Database(tmp_path / "review.db")
    service = NotebookAnalysis(database, None)
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
    database.close()


def event(event_id="event", **changes):
    return {
        "execution_id": "e",
        "client_event_id": event_id,
        "expected_revision": 0,
        "kind": "note",
        "payload": {"text": "selected"},
        **changes,
    }


def test_review_two_connections_allow_one_cas_winner_and_replay_loser(
    analysis, tmp_path
):
    async def scenario():
        second_db = Database(tmp_path / "review.db")
        second = NotebookAnalysis(second_db, None)
        try:
            results = await asyncio.gather(
                analysis.context.publish("n", event("first")),
                second.context.publish("n", event("second")),
                return_exceptions=True,
            )
            winners = [value for value in results if isinstance(value, dict)]
            failures = [value for value in results if isinstance(value, NotebookError)]
            assert len(winners) == len(failures) == 1
            assert failures[0].code == "state_revision_conflict"
            winner = winners[0]
            assert (
                await second.context.publish("n", event(winner["event_id"])) == winner
            )
            with pytest.raises(NotebookError, match="state_event_conflict"):
                await second.context.publish(
                    "n", event(winner["event_id"], payload={"text": "changed"})
                )
        finally:
            await second.close()
            second_db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "document",
    [
        event(trust_label="trusted"),
        event(payload={"text": "ok", "role": "system"}),
        event(expected_revision=True),
        event(payload={"text": "x" * 4001}),
        event(kind="selection", payload={"selected_ids": ["x"] * 101}),
        event(
            kind="view",
            payload={
                "view_id": "x",
                "parameters": {"nested": {"instruction": "execute"}},
            },
        ),
        event(
            kind="view", payload={"view_id": "x", "parameters": {"value": float("inf")}}
        ),
    ],
)
def test_review_rejected_state_never_changes_revision(analysis, document):
    async def scenario():
        with pytest.raises(NotebookError):
            await analysis.context.publish("n", document)
        assert (await analysis.context.snapshot("n"))["live_state"] is None

    asyncio.run(scenario())


def test_review_context_budget_survives_escape_expansion_and_isolation(analysis):
    async def scenario():
        for number in range(10):
            await analysis.store.put_cell(
                NotebookCell(
                    notebook_id="n",
                    cell_id=f"cell-{number}",
                    source="<" * 3000,
                    output={"stdout": "\U0001f642" * 5000, "error": "<system>" * 500},
                    trust_label="imported_untrusted",
                )
            )
        await analysis.context.publish("n", event(payload={"text": "<" * 4000}))
        with use_tool_context(values={"turn_id": "review-turn"}):
            messages = await NotebookContext(analysis, "chat").augment(())
        assert len(messages) == 1 and messages[0].role is Role.USER
        text = messages[0].content
        assert len(text.encode()) <= MAX_CONTEXT_BYTES
        assert (
            text.count("<notebook_context>") == text.count("</notebook_context>") == 1
        )
        assert "<system>" not in text
        body = json.loads(text.split("\n")[2])
        assert body["live_state"]["trust_label"] == "live_app_untrusted"
        assert body["live_state"]["payload_truncated"] is True
        assert (await analysis.context.snapshot("n"))["context_delivery"] == {
            "prepared_revision": 1,
            "turn_id": "review-turn",
        }
        assert await NotebookContext(analysis, "other-chat").augment(()) == ()

    asyncio.run(scenario())


def test_review_live_state_rejects_unsettled_and_other_notebook_execution(analysis):
    async def scenario():
        await analysis.store.record_execution(
            ExecutionRecord(
                execution_id="pending",
                notebook_id="n",
                cell_id="pending",
                status="completed",
                output={"event_stream_pending": True},
            )
        )
        await analysis.store.record_execution(
            ExecutionRecord(
                execution_id="foreign",
                notebook_id="other",
                cell_id="foreign",
                status="completed",
            )
        )
        for execution, code in [
            ("pending", "execution_not_settled"),
            ("foreign", "execution_not_found"),
        ]:
            with pytest.raises(NotebookError, match=code):
                await analysis.context.publish("n", event(execution_id=execution))
        assert (await analysis.context.snapshot("n"))["live_state"] is None

    asyncio.run(scenario())


def test_review_http_auth_and_large_numeric_state_validation(analysis, tmp_path):
    app = make_app(tmp_path, Scripted())
    api = create_desktop_app(app, bearer_token="secret", notebook_analysis=analysis)
    with TestClient(api, raise_server_exceptions=False) as client:
        for endpoint in ("binding", "live-state"):
            assert client.post(f"/notebooks/n/{endpoint}", json={}).status_code == 401
        response = client.post(
            "/notebooks/n/live-state",
            headers={"Authorization": "Bearer secret"},
            json=event(
                kind="view",
                payload={"view_id": "scatter", "parameters": {"scale": 10**400}},
            ),
        )
        assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "value", [None, False, 0, 9007199254740991, -9007199254740991, 0.5]
)
def test_review_flat_json_scalars_preserve_values_without_authority(analysis, value):
    async def scenario():
        state = await analysis.context.publish(
            "n",
            event(
                kind="view",
                payload={"view_id": "scatter", "parameters": {"scale": value}},
            ),
        )
        assert state["payload"]["parameters"]["scale"] == value
        assert state["trust_label"] == "live_app_untrusted"

    asyncio.run(scenario())
