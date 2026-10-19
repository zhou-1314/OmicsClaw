"""End-to-end Desktop ask_user (question_request) and session options tests."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from omicsclaw.entry import attach_sessions
from omicsclaw.entry.desktop import (
    DesktopIngressError,
    DesktopInteractions,
    answer_question,
    decode_chat_stream_request,
    open_chat_stream,
)
from omicsclaw.entry.events import TurnEventType
from omicsclaw.entry.render import to_wire
from omicsclaw.entry.stream import event_from_durable
from omicsclaw.schema import Message, Role, ToolCall
from tests.entry.test_turn_runner import Scripted, make_app


def _says(text: str) -> Message:
    return Message(role=Role.ASSISTANT, content=text)


def _calls(*calls: tuple[str, dict[str, Any]]) -> Message:
    return Message(
        role=Role.ASSISTANT,
        tool_calls=tuple(
            ToolCall(id=f"c{index}", name=name, arguments=json.dumps(arguments))
            for index, (name, arguments) in enumerate(calls)
        ),
    )


def test_ask_user_emits_question_request_sse_and_settles_via_answer_question(tmp_path):
    """Full round-trip: AskUserTool -> QUESTION_ASKED -> question_request SSE -> answer_question."""
    provider = Scripted(
        _calls(
            (
                "ask_user",
                {
                    "question": "Which PBMC dataset should we analyze?",
                    "options": [
                        {"label": "pbmc3k_raw.h5ad", "description": "Raw counts full pipeline"},
                        {"label": "pbmc3k_processed.h5ad", "description": "Preprocessed with PCA/UMAP"},
                    ],
                },
            )
        ),
        _says("Proceeding with pbmc3k_raw.h5ad."),
    )

    async def drive():
        app = attach_sessions(make_app(tmp_path, provider, ask_user=True))
        interactions = DesktopInteractions(app)
        stream = await open_chat_stream(
            app,
            {
                "ingress_schema_version": 3,
                "session_id": "sess_ask_1",
                "source_request_id": "a" * 32,
                "content": "Analyze PBMC",
            },
            interactions=interactions,
        )
        question_frames: list[dict[str, object]] = []
        resolution_result: dict[str, object] = {}

        async with stream.body as body:
            async for raw_frame in body:
                if "data: " not in raw_frame:
                    continue
                envelope = json.loads(raw_frame.split("data: ", 1)[1])
                if envelope.get("type") == "question_request":
                    data = json.loads(envelope["data"])
                    question_frames.append(data)
                    resolution_result = await answer_question(
                        app,
                        interactions,
                        {
                            "request_id": data["request_id"],
                            "answer": {
                                "status": "answered",
                                "selected": ["pbmc3k_raw.h5ad"],
                                "reply": "pbmc3k_raw.h5ad",
                            },
                        },
                    )
                elif envelope.get("type") == "done":
                    break

        outcome = await app.sessions.handle(stream.turn_id).wait()
        await app.aclose()
        return question_frames, resolution_result, outcome

    question_frames, resolution_result, outcome = asyncio.run(
        asyncio.wait_for(drive(), timeout=5.0)
    )
    assert len(question_frames) == 1
    qframe = question_frames[0]
    assert qframe["question"] == "Which PBMC dataset should we analyze?"
    assert len(qframe["options"]) == 2
    assert qframe["options"][0]["label"] == "pbmc3k_raw.h5ad"
    assert resolution_result["ok"] is True
    assert resolution_result["status"] == "answered"
    assert resolution_result["selected"] == ["pbmc3k_raw.h5ad"]
    assert outcome is not None


def test_event_from_durable_rehydrates_question_asked():
    """Durable replay preserves question_request fields across reconnects."""
    from omicsclaw.entry.events import TurnEvent
    from omicsclaw.tools.context import QuestionOption, QuestionRequest

    original = TurnEvent.question_asked(
        QuestionRequest(
            question="Select normalization",
            options=(QuestionOption("LogNormalize", "Standard"), QuestionOption("SCT", "SCTransform")),
            multi_select=True,
        ),
        "b" * 32 + "#1",
        seq=5,
        session_id="s1",
        turn_id="b" * 32,
    )
    wire = to_wire(original)
    row = SimpleNamespace(
        type=TurnEventType.QUESTION_ASKED.value,
        seq=5,
        session_id="s1",
        turn_id="b" * 32,
        payload=wire,
    )
    rehydrated = event_from_durable(row)
    assert rehydrated is not None
    assert rehydrated.type is TurnEventType.QUESTION_ASKED
    assert rehydrated.request_id == "b" * 32 + "#1"
    assert rehydrated.question is not None
    assert rehydrated.question.question == "Select normalization"
    assert rehydrated.question.multi_select is True
    assert [o.label for o in rehydrated.question.options] == ["LogNormalize", "SCT"]


def test_decode_chat_stream_request_accepts_reasoning_effort_and_session_options():
    req = decode_chat_stream_request(
        {
            "ingress_schema_version": 3,
            "session_id": "s1",
            "source_request_id": "c" * 32,
            "content": "Run cluster analysis",
            "reasoning_effort": "max",
            "ultra_mode": True,
            "verifier_mode": False,
            "memory_mode": True,
            "reviewer_model": "claude-opus-4-7",
            "specialist": "scrna-specialist",
        }
    )
    assert req.values["reasoning_effort"] == "max"
    assert req.values["ultra_mode"] is True
    assert req.values["verifier_mode"] is False
    assert req.values["memory_mode"] is True
    assert req.values["reviewer_model"] == "claude-opus-4-7"
    assert req.values["specialist"] == "scrna-specialist"

    with pytest.raises(DesktopIngressError, match="invalid_reasoning_effort"):
        decode_chat_stream_request(
            {
                "ingress_schema_version": 3,
                "session_id": "s1",
                "source_request_id": "c" * 32,
                "content": "Run cluster analysis",
                "reasoning_effort": "extreme",
            }
        )
