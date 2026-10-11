"""The session scratch: from a written-back compaction into the database.

The read side — the augmentor block that restates the dead ends while no
compaction message carries the anchors — is pinned by
``tests/entry/test_nudge_wiring.py``. Here the write side: a compaction
that is written back merges its ``tried_solutions`` anchor into the
scratch, one row per line, and a compaction that is not written back
merges nothing.
"""

from __future__ import annotations

import asyncio
import dataclasses
import pathlib

from omicsclaw.entry.assembly import build_app
from omicsclaw.entry.compaction import build_compactor
from omicsclaw.entry.config import AppConfig
from omicsclaw.memory import SCRATCH_KIND_TRIED_SOLUTIONS, SessionScratch
from omicsclaw.provider import Completion
from omicsclaw.schema import Message, Role

WAIT_S = 10.0


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, WAIT_S))


def _config(workspace: pathlib.Path) -> AppConfig:
    (workspace / "OMICSCLAW.md").write_text("You are OmicsClaw.", encoding="utf-8")
    return AppConfig(workspace=workspace)


def _history() -> tuple[Message, ...]:
    messages: list[Message] = [Message(role=Role.SYSTEM, content="You are OmicsClaw.")]
    messages.append(Message.user("the matrix lives at data/pbmc3k_raw.h5ad"))
    messages.append(Message(role=Role.ASSISTANT, content="noted"))
    for index in range(8):
        messages.append(Message.user(f"step {index} please"))
        messages.append(Message(role=Role.ASSISTANT, content=f"ran step {index}"))
    return tuple(messages)


class _Summarizer:
    """Answers the compaction prompt with tried_solutions, or fails."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def summarize(self, prompt: str, *, system: str) -> str:
        if self.fail:
            raise RuntimeError("summary model unavailable")
        return (
            "## Anchors\n\n### User Intent\nship it\n\n"
            "### Tried Solutions\n- louvain: too slow\n"
            "- leiden res=2: noisy\n\n## Summary\nthey talked"
        )


class _ScriptedProvider:
    """A backend that is never called."""

    @property
    def name(self) -> str:
        return "scripted"

    async def generate(self, messages, tools=None):
        return Completion(message=Message(role=Role.ASSISTANT, content="done"))

    def generate_stream(self, messages, tools=None):
        raise NotImplementedError

    def bind(self, **overrides):
        return self


def test_a_written_back_compaction_merges_its_dead_ends(tmp_path, monkeypatch):
    from omicsclaw.entry import assembly

    monkeypatch.setattr(assembly, "provider_from_env", lambda p, m: _ScriptedProvider())
    app = build_app(_config(tmp_path))
    app = dataclasses.replace(app, summarizer=_Summarizer())
    try:
        _, record = _run(build_compactor(app, session_id="s1").force(
            _history(), app.tools_snapshot
        ))
        assert app.memory is not None
        text = _run(
            SessionScratch(app.memory.database).read(
                "s1", SCRATCH_KIND_TRIED_SOLUTIONS
            )
        )
    finally:
        _run(app.aclose())

    assert record.written_back
    assert text == "- louvain: too slow\n- leiden res=2: noisy"


def test_a_compaction_that_was_not_kept_merges_nothing(tmp_path, monkeypatch):
    from omicsclaw.entry import assembly

    monkeypatch.setattr(assembly, "provider_from_env", lambda p, m: _ScriptedProvider())
    app = build_app(_config(tmp_path))
    app = dataclasses.replace(app, summarizer=_Summarizer(fail=True))
    try:
        _, record = _run(build_compactor(app, session_id="s1").force(
            _history(), app.tools_snapshot
        ))
        assert app.memory is not None
        text = _run(
            SessionScratch(app.memory.database).read(
                "s1", SCRATCH_KIND_TRIED_SOLUTIONS
            )
        )
    finally:
        _run(app.aclose())

    assert not record.written_back
    assert text == ""
