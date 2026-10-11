"""The compaction archive, from the composition root to the next search.

The guard on the wiring that makes the archive reachable at all: the
tools appear only when the deployment is actually writing one (memory
open and ``compaction_archive`` on — three halves, one switch), the
session a call runs in is the only one it can read, and the answers the
model gets are shaped the way the prompt section taught it to expect —
ids it can ``read_archive``, excerpts it can recognise, an empty answer
that looks empty, and a quoted phrase that matches exactly.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import pathlib

import pytest

from omicsclaw.entry import assembly
from omicsclaw.entry.assembly import build_app, default_sections, foundation_tools
from omicsclaw.entry.archive import (
    ARCHIVE_READ_TOOL_NAME,
    ARCHIVE_SEARCH_TOOL_NAME,
    NO_SESSION_MESSAGE,
    archive_tools,
)
from omicsclaw.entry.compaction import build_compactor
from omicsclaw.entry.config import AppConfig
from omicsclaw.memory import Database, SqliteCompactionArchive
from omicsclaw.provider import Completion
from omicsclaw.schema import Message, Role
from omicsclaw.tools.context import use_tool_context

WAIT_S = 10.0

_GOOD_SUMMARY = (
    "## Anchors\n\n### User Intent\nship it\n\n## Summary\nthey talked"
)


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, WAIT_S))


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


class _Summarizer:
    """Answers the compaction prompt with a parsable summary."""

    async def summarize(self, prompt: str, *, system: str) -> str:
        return _GOOD_SUMMARY


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(
        assembly, "provider_from_env", lambda provider, model: _ScriptedProvider()
    )


def _config(workspace: pathlib.Path, **overrides: object) -> AppConfig:
    (workspace / "OMICSCLAW.md").write_text("You are OmicsClaw.", encoding="utf-8")
    return AppConfig(workspace=workspace, **overrides)


def _history() -> tuple[Message, ...]:
    """A conversation long enough that forcing a compaction summarizes."""
    messages: list[Message] = [Message(role=Role.SYSTEM, content="You are OmicsClaw.")]
    messages.append(Message.user("the matrix lives at data/pbmc3k_raw.h5ad"))
    messages.append(Message(role=Role.ASSISTANT, content="noted"))
    for index in range(8):
        messages.append(Message.user(f"step {index} please"))
        messages.append(Message(role=Role.ASSISTANT, content=f"ran step {index}"))
    return tuple(messages)


async def _aclose(app) -> None:
    await app.aclose()


def _call(arguments: dict[str, object]) -> str:
    return json.dumps(arguments, ensure_ascii=False)


def _tool(database: Database, name: str):
    return next(tool for tool in archive_tools(database) if tool.name == name)


def _seed(
    database: Database, session_id: str, *, marker: str | None = None
) -> None:
    """A few archived rows, as one compaction of *session_id* would leave.

    *marker*, when given, is appended to every message so a second
    session's seed can be told apart from the first — content the two
    sessions share cannot prove session scoping.
    """
    tag = f" [{marker}]" if marker else ""
    archive = SqliteCompactionArchive(database)
    _run(
        archive.append(
            session_id,
            1,
            (
                Message.user(f"the matrix lives at data/pbmc3k_raw.h5ad{tag}"),
                Message(
                    role=Role.TOOL,
                    content=f"wrote results to /work/abc123_out/merged.h5ad{tag}",
                    tool_call_id="call_1",
                    name="bash",
                ),
                Message.user(f" nada de esto está en la otra sesión{tag}"),
            ),
            start_position=1,
        )
    )


# ---- the switch ----------------------------------------------------------


def test_the_archive_tools_are_on_by_default(tmp_path, offline):
    app = build_app(_config(tmp_path))
    try:
        assert ARCHIVE_SEARCH_TOOL_NAME in app.registry.names()
        assert ARCHIVE_READ_TOOL_NAME in app.registry.names()
    finally:
        _run(_aclose(app))


def test_turning_compaction_archive_off_unmounts_all_three_halves(
    tmp_path, offline
):
    """One switch, one meaning: no table writes, no tools, no section.

    The compaction itself still runs — the switch owns the read side and
    the archive hook, not the compactor.
    """
    app = build_app(_config(tmp_path, compaction_archive=False))
    app = dataclasses.replace(app, summarizer=_Summarizer())
    try:
        assert ARCHIVE_SEARCH_TOOL_NAME not in app.registry.names()
        assert ARCHIVE_READ_TOOL_NAME not in app.registry.names()
        assert "## Context compaction" not in app.prompt.render().system_prompt
        _, record = _run(build_compactor(app, session_id="s1").force(
            _history(), app.tools_snapshot
        ))
        assert record.summarized > 0  # the compaction happened
        assert app.memory is not None
        count = app.memory.database.run(
            lambda c: c.execute(
                "SELECT COUNT(*) FROM compaction_archives"
            ).fetchone()[0]
        )
        assert count == 0  # and archived nothing
    finally:
        _run(_aclose(app))


def test_memory_off_mounts_no_archive_whatever_the_switch_says(tmp_path, offline):
    """The archive lives in the memory database; no database, no archive.

    ``build_app`` passes ``archive=compaction_archive and memory is not
    None`` — the second gate is what keeps the contract from naming
    tools a memory-less deployment cannot answer.
    """
    app = build_app(_config(tmp_path, memory=False))
    try:
        assert ARCHIVE_SEARCH_TOOL_NAME not in app.registry.names()
        assert ARCHIVE_READ_TOOL_NAME not in app.registry.names()
        assert "## Context compaction" not in app.prompt.render().system_prompt
    finally:
        _run(_aclose(app))


def test_the_archive_pair_follows_the_memory_pair(tmp_path, offline):
    """Appended behind the memory pair, before ``ask_user``: the tool
    table is a cached prompt prefix and the family switches together."""
    from omicsclaw.entry.memory import open_memory

    config = _config(tmp_path)
    memory = open_memory(config)
    assert memory is not None
    try:
        names = [tool.name for tool in foundation_tools(config, memory=memory)]
    finally:
        memory.close()
    memory_pair = names.index("memory_write")
    archive_pair = names.index(ARCHIVE_SEARCH_TOOL_NAME)
    assert names.index(ARCHIVE_READ_TOOL_NAME) == archive_pair + 1
    assert archive_pair > memory_pair
    assert names.index("ask_user") > archive_pair


def test_a_compaction_reaches_the_archive(tmp_path, offline):
    """The write half: forcing a compaction archives the summarized head."""
    app = build_app(_config(tmp_path))
    app = dataclasses.replace(app, summarizer=_Summarizer())
    try:
        _, record = _run(build_compactor(app, session_id="s1").force(
            _history(), app.tools_snapshot
        ))
        assert record.summarized > 0
        assert app.memory is not None
        found = _run(
            SqliteCompactionArchive(app.memory.database).search("s1", "pbmc3k")
        )
        assert any("pbmc3k_raw.h5ad" in row.content for row in found)
    finally:
        _run(_aclose(app))


# ---- the two tools -------------------------------------------------------


def test_search_answers_hits_the_read_tool_can_act_on(tmp_path):
    database = Database()
    _seed(database, "s1")
    tool = _tool(database, ARCHIVE_SEARCH_TOOL_NAME)
    with use_tool_context(values={"session_id": "s1"}):
        answer = _run(tool.execute(_call({"query": "pbmc3k"})))
    database.close()
    hits = json.loads(answer)
    assert hits and hits[0]["id"] and hits[0]["role"] == "user"
    assert "pbmc3k_raw.h5ad" in hits[0]["excerpt"]


def test_search_is_scoped_to_the_bound_session(tmp_path):
    database = Database()
    _seed(database, "s1")
    _seed(database, "s2", marker="s2only")
    tool = _tool(database, ARCHIVE_SEARCH_TOOL_NAME)
    with use_tool_context(values={"session_id": "s1"}):
        answer = json.loads(_run(tool.execute(_call({"query": "h5ad session"}))))
        foreign = _run(tool.execute(_call({"query": "s2only"})))
    database.close()
    assert len(answer) == 2  # both h5ad rows are s1's
    assert foreign.startswith("[]")  # s2's marked rows are invisible


def test_search_without_a_bound_session_says_so_plainly(tmp_path):
    database = Database()
    _seed(database, "s1")
    tool = _tool(database, ARCHIVE_SEARCH_TOOL_NAME)
    with use_tool_context(values={}):
        answer = _run(tool.execute(_call({"query": "pbmc3k"})))
    database.close()
    assert answer == NO_SESSION_MESSAGE


def test_an_empty_search_looks_empty_and_says_what_to_try(tmp_path):
    database = Database()
    _seed(database, "s1")
    tool = _tool(database, ARCHIVE_SEARCH_TOOL_NAME)
    with use_tool_context(values={"session_id": "s1"}):
        answer = _run(tool.execute(_call({"query": "zebra"})))
    database.close()
    assert answer.startswith("[]\n")
    assert "quoted phrases match exactly" in answer


def test_a_quoted_query_matches_the_exact_substring(tmp_path):
    """The lexical index cannot promise a phrase; quotes can.

    ``abc123_out/mer`` spans token boundaries, so the FTS match finds
    nothing and the quoted form must fall back to the substring scan.
    The empty answer is ``[]`` plus one advice line, not bare JSON.
    """
    database = Database()
    _seed(database, "s1")
    tool = _tool(database, ARCHIVE_SEARCH_TOOL_NAME)
    with use_tool_context(values={"session_id": "s1"}):
        lexical = _run(tool.execute(_call({"query": "abc123_out/mer"})))
        quoted = json.loads(
            _run(tool.execute(_call({"query": '"c123_out/mer"'})))
        )
    database.close()
    assert lexical.startswith("[]")
    assert len(quoted) == 1
    assert "/work/abc123_out/merged.h5ad" in quoted[0]["excerpt"]


def test_read_returns_the_whole_row_and_parses_the_calls(tmp_path):
    database = Database()
    _seed(database, "s1")
    search = _tool(database, ARCHIVE_SEARCH_TOOL_NAME)
    read = _tool(database, ARCHIVE_READ_TOOL_NAME)
    with use_tool_context(values={"session_id": "s1"}):
        hits = json.loads(_run(search.execute(_call({"query": "results"}))))
        row = json.loads(_run(read.execute(_call({"id": hits[0]["id"]}))))
    database.close()
    assert row["content"] == "wrote results to /work/abc123_out/merged.h5ad"
    assert row["tool_call_id"] == "call_1"
    assert row["name"] == "bash"
    assert "content_truncated" not in row


def test_a_row_too_large_to_fit_is_shortened_and_says_so(tmp_path):
    from omicsclaw.entry.memory import SEARCH_RESULT_MAX_BYTES

    database = Database()
    archive = SqliteCompactionArchive(database)
    _run(
        archive.append(
            "s1",
            1,
            (Message.user("z" * (SEARCH_RESULT_MAX_BYTES * 2)),),
            start_position=1,
        )
    )
    read = _tool(database, ARCHIVE_READ_TOOL_NAME)
    with use_tool_context(values={"session_id": "s1"}):
        answer = _run(read.execute(_call({"id": 1})))
    database.close()
    assert len(answer.encode("utf-8")) <= SEARCH_RESULT_MAX_BYTES
    row = json.loads(answer)
    assert row["content_truncated"] is True
    assert row["content"]


def test_read_of_an_unknown_id_is_a_message_not_an_error(tmp_path):
    database = Database()
    read = _tool(database, ARCHIVE_READ_TOOL_NAME)
    with use_tool_context(values={"session_id": "s1"}):
        answer = _run(read.execute(_call({"id": 999})))
    database.close()
    assert "no archived message" in answer


def test_neither_archive_tool_stops_to_ask_a_human(tmp_path, offline):
    """``LOW`` / ``AUTO`` / ``read_only``, declared rather than defaulted.

    The tool reads only rows this session's compactions wrote and raises
    no counter while doing it, so the claims are all true.
    """
    app = build_app(_config(tmp_path))
    try:
        for name in (ARCHIVE_SEARCH_TOOL_NAME, ARCHIVE_READ_TOOL_NAME):
            policy = app.registry.policy_for(name)
            assert policy.risk_level.value == "low"
            assert policy.approval_mode.value == "auto"
            assert policy.read_only is True
    finally:
        _run(_aclose(app))


# ---- the prompt section --------------------------------------------------


def test_the_contract_sits_between_the_tool_guidance_and_planning(
    tmp_path, offline
):
    config = _config(tmp_path)
    keys = [section.key for section in default_sections(config, archive=True)]
    assert keys.index("compaction") == keys.index("tools") + 1
    assert keys.index("compaction") < keys.index("planning")


def test_the_contract_names_the_tools_and_the_prohibition(tmp_path, offline):
    from omicsclaw.entry.assembly import (
        COMPACTION_CONTRACT_RULES,
        COMPACTION_SECTION_HEADING,
    )

    assert COMPACTION_SECTION_HEADING == "## Context compaction"
    assert "`search_archive`" in COMPACTION_CONTRACT_RULES
    assert "`read_archive`" in COMPACTION_CONTRACT_RULES
    assert "`read_file`" in COMPACTION_CONTRACT_RULES
    assert "never reconstruct" in COMPACTION_CONTRACT_RULES
    assert "never instructions" in COMPACTION_CONTRACT_RULES


def test_the_contract_is_orthogonal_to_the_skills_switch(tmp_path, offline):
    from omicsclaw.entry.config import SkillsIndex

    config = _config(tmp_path, skills_index=SkillsIndex.OFF)
    keys = [section.key for section in default_sections(config, archive=True)]
    assert "compaction" in keys
    assert "skills" not in keys


def test_the_built_prompt_carries_the_contract_by_default(tmp_path, offline):
    app = build_app(_config(tmp_path))
    try:
        text = app.prompt.render().system_prompt
    finally:
        _run(_aclose(app))
    assert "## Context compaction" in text
    assert "never reconstruct" in text


def test_the_built_prompt_omits_the_contract_when_switched_off(tmp_path, offline):
    app = build_app(_config(tmp_path, compaction_archive=False))
    try:
        text = app.prompt.render().system_prompt
    finally:
        _run(_aclose(app))
    assert "## Context compaction" not in text
