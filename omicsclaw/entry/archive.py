"""The compaction archive, bound to one deployment as two tools.

``search_archive`` and ``read_archive`` are the read-back half of
:mod:`omicsclaw.entry.compaction`'s archiver: the messages a compaction
removed live on in ``compaction_archives``, and these are how the model
reaches them after the summary that stood in for them has omitted the
exact values — a path, an id, a parameter, an error's own words.

Session-scoped by construction: both tools resolve the session from the
tool context at call time, and the store answers only inside it.
"""

from __future__ import annotations

import json
import logging

from omicsclaw.memory import Database, SqliteCompactionArchive, truncate_utf8
from omicsclaw.memory.archive import ArchivedMessage
from omicsclaw.tools import (
    ApprovalMode,
    FunctionTool,
    RiskLevel,
    Tool,
    ToolPolicy,
)
from omicsclaw.tools.context import context_value

from .memory import (
    CONTENT_TRUNCATED_KEY,
    DEFAULT_SEARCH_LIMIT,
    MAX_SEARCH_LIMIT,
    SEARCH_RESULT_MAX_BYTES,
)

__all__ = [
    "ARCHIVE_READ_SCHEMA",
    "ARCHIVE_READ_TOOL_NAME",
    "ARCHIVE_SEARCH_SCHEMA",
    "ARCHIVE_SEARCH_TOOL_NAME",
    "EXCERPT_MAX_CHARS",
    "NO_SESSION_MESSAGE",
    "archive_tools",
]

_log = logging.getLogger(__name__)

ARCHIVE_SEARCH_TOOL_NAME = "search_archive"
ARCHIVE_READ_TOOL_NAME = "read_archive"

EXCERPT_MAX_CHARS = 200
"""Characters of content one search hit carries.

An excerpt is for recognising the row, not reading it — the id is what
``read_archive`` takes, and the full text is one call away."""

NO_SESSION_MESSAGE = "no session is bound to this exchange"

_EMPTY_HINT = (
    "try a shorter exact identifier (a path fragment, a gene id); "
    "quoted phrases match exactly"
)

_SEARCH_DESCRIPTION = (
    "Look through the messages compaction removed from this "
    "conversation. Compaction summaries deliberately omit exact values, "
    "so before writing a path, identifier, parameter or error text that "
    "earlier conversation contained, search for it here and read the "
    "original with `read_archive`. A query wrapped in double quotes "
    "matches that phrase exactly anywhere in a message."
)

ARCHIVE_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Words to look for in the archived messages. Any word may "
                "match. Wrap the query in double quotes to find that "
                "exact phrase as a substring."
            ),
        },
        "limit": {
            "type": "integer",
            "description": (
                f"Greatest number of rows to return, up to "
                f"{MAX_SEARCH_LIMIT}. Defaults to {DEFAULT_SEARCH_LIMIT}."
            ),
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

_READ_DESCRIPTION = (
    "Read one archived message in full, by the `id` a `search_archive` "
    "result reported. Offloaded tool results are not here — read those "
    "back with `read_file` from the reference in the placeholder."
)

ARCHIVE_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {
            "type": "integer",
            "description": (
                "The row id `search_archive` reported for the message."
            ),
        },
    },
    "required": ["id"],
    "additionalProperties": False,
}

_POLICY = ToolPolicy(
    risk_level=RiskLevel.LOW,
    approval_mode=ApprovalMode.AUTO,
    read_only=True,
    concurrency_safe=True,
    allowed_in_background=True,
    tags=frozenset({"memory", "inspection"}),
)
"""Declared rather than defaulted, for the reason ``use_skill``'s is.

:class:`~omicsclaw.tools.base.ToolPolicy` defaults to ``HIGH`` + ``ASK``
and approval fails closed when no channel is bound, so omitting a policy
would put a prompt in front of every archive lookup. The tool reads only
rows this session's compactions wrote, and reads nothing the caller
names outside the archive — unlike ``memory_search`` it does not even
raise a use count, which is why it may claim ``read_only`` where that
one may not.
"""


def archive_tools(database: Database) -> tuple[Tool, ...]:
    """``search_archive`` and ``read_archive``, both over *database*.

    :param database: The memory database the archiver wrote to.
    :returns: The pair, search first.
    """
    return (search_archive_tool(database), read_archive_tool(database))


def search_archive_tool(database: Database) -> FunctionTool:
    """Build the ``search_archive`` tool over *database*.

    No ``policy`` parameter: a deployment that disagrees with the
    default passes its own to :meth:`~omicsclaw.tools.ToolRegistry.
    register`.

    :param database: The memory database the archiver wrote to.
    :returns: The tool, ready to mount.
    """
    archive = SqliteCompactionArchive(database)

    async def run(query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> str:
        return await _search(archive, query, limit)

    return FunctionTool(
        ARCHIVE_SEARCH_TOOL_NAME,
        _SEARCH_DESCRIPTION,
        run,
        parameters=ARCHIVE_SEARCH_SCHEMA,
        policy=_POLICY,
    )


def read_archive_tool(database: Database) -> FunctionTool:
    """Build the ``read_archive`` tool over *database*.

    No ``policy`` parameter, for the reason
    :func:`search_archive_tool` gives.

    :param database: The memory database the archiver wrote to.
    :returns: The tool, ready to mount.
    """
    archive = SqliteCompactionArchive(database)

    async def run(id: int) -> str:  # noqa: A002 - the schema property name
        return await _read(archive, id)

    return FunctionTool(
        ARCHIVE_READ_TOOL_NAME,
        _READ_DESCRIPTION,
        run,
        parameters=ARCHIVE_READ_SCHEMA,
        policy=_POLICY,
    )


def _bound_session() -> str:
    """The session this call runs in, or ``""`` when none is bound."""
    return str(context_value("session_id", ""))


def _quoted(query: str) -> str | None:
    """The phrase a double-quoted *query* asks for, or ``None``.

    Quotes on both ends, with something between them. A query that only
    starts or only ends with a quote is a plain query — half a quote is
    not a phrase anyone meant.
    """
    text = query.strip()
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        phrase = text[1:-1]
        if phrase:
            return phrase
    return None


async def _search(
    archive: SqliteCompactionArchive, query: str, limit: int
) -> str:
    """Answer one ``search_archive`` call as JSON.

    JSON for the reason ``memory_search`` is JSON: every hit carries its
    ``id``, and the id is what ``read_archive`` takes. A quoted query
    goes to the substring index — the lexical one cannot promise a
    phrase — and any empty answer carries the hint that a shorter exact
    identifier, quoted, is the next thing to try.

    :param archive: The store to search.
    :param query: Free text, or a double-quoted phrase.
    :param limit: Requested number of rows; clamped to
        ``1..MAX_SEARCH_LIMIT``.
    :returns: A JSON array of hits, each with an excerpt; ``[]`` plus one
        advice line when nothing matched.
    """
    session = _bound_session()
    if not session:
        return NO_SESSION_MESSAGE
    wanted = min(max(int(limit), 1), MAX_SEARCH_LIMIT)
    phrase = _quoted(query)
    if phrase is not None:
        found = await archive.search_exact(session, phrase, wanted)
    else:
        found = await archive.search(session, query.strip(), wanted)
    _log.info(
        "archive.search session=%s quoted=%s hits=%d",
        session,
        phrase is not None,
        len(found),
    )
    hits = [
        {
            "id": row.id,
            "seq": row.seq,
            "position": row.position,
            "role": row.role.value,
            "excerpt": row.content[:EXCERPT_MAX_CHARS],
        }
        for row in found
    ]
    if not hits:
        return f"[]\n{_EMPTY_HINT}"
    return json.dumps(hits, ensure_ascii=False)


async def _read(archive: SqliteCompactionArchive, row_id: int) -> str:
    """Answer one ``read_archive`` call.

    The whole row, with ``tool_calls`` parsed for reading — the stored
    bytes are untouched, this only renders them. An answer that would
    pass :data:`SEARCH_RESULT_MAX_BYTES` has its content halved until it
    fits and says so with :data:`CONTENT_TRUNCATED_KEY`, the convention
    ``memory_search`` established.

    :param archive: The store to read from.
    :param row_id: The id a search result reported.
    :returns: The row as JSON, or one line saying there is no such row.
    """
    session = _bound_session()
    if not session:
        return NO_SESSION_MESSAGE
    row = await archive.read(session, row_id)
    if row is None:
        return (
            f"no archived message with id {row_id} in this session; "
            f"search_archive reports the id of every row it finds"
        )
    _log.info("archive.read session=%s id=%d", session, row_id)
    answer = _as_dict(row)
    while not _fits(answer) and answer["content"]:
        body = str(answer["content"])
        answer = {
            **answer,
            "content": truncate_utf8(body, max(len(body.encode("utf-8")) // 2, 1)),
            CONTENT_TRUNCATED_KEY: True,
        }
    return json.dumps(answer, ensure_ascii=False)


def _as_dict(row: ArchivedMessage) -> dict[str, object]:
    """One archived row, in the fields the model can act on."""
    calls: object = []
    if row.tool_calls:
        try:
            calls = json.loads(row.tool_calls)
        except ValueError:
            # The stored bytes are the authority; show them rather than
            # fail over text this tool did not write.
            calls = row.tool_calls
    return {
        "id": row.id,
        "seq": row.seq,
        "position": row.position,
        "role": row.role.value,
        "content": row.content,
        "tool_calls": calls,
        "tool_call_id": row.tool_call_id,
        "name": row.name,
    }


def _fits(answer: dict[str, object]) -> bool:
    """Whether *answer* renders inside :data:`SEARCH_RESULT_MAX_BYTES`."""
    return (
        len(json.dumps(answer, ensure_ascii=False).encode("utf-8"))
        <= SEARCH_RESULT_MAX_BYTES
    )
