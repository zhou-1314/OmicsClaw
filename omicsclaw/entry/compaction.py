"""The compactor one exchange runs with, and where its files go.

:func:`build_compactor` joins the context layer's
:class:`~omicsclaw.context.ProgressiveCompactor` to this deployment: the
budget and summarizer of the app, a file store for offloaded tool results
inside the workspace (so ``read_file`` can read them back), and a JSONL
log of every compaction. Both live under ``<workspace>/.omicsclaw/``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Sequence

from omicsclaw.context import (
    BudgetReport,
    CompactionArchiver,
    CompactionRecord,
    CompactionState,
    Offloader,
    ProgressiveCompactor,
)
from omicsclaw.memory import (
    FileOffloadStore,
    JsonlCompactionLog,
    SCRATCH_KIND_TRIED_SOLUTIONS,
    SessionScratch,
    SqliteCompactionArchive,
)
from omicsclaw.schema import Message

from .config import STATE_DIRNAME, AppConfig
from .memory import build_memory_extractor

if TYPE_CHECKING:  # pragma: no cover - a type-only import
    from .assembly import AgentApp

__all__ = [
    "PINNED_SYSTEM_MESSAGES",
    "RECORDS_DIRNAME",
    "STATE_DIRNAME",
    "TOOL_RESULTS_DIRNAME",
    "build_compactor",
    "compaction_log",
    "offload_store",
]

_log = logging.getLogger(__name__)

PINNED_SYSTEM_MESSAGES = 1
"""Messages at the head of a composed conversation that compaction keeps:
the one system message :func:`~omicsclaw.context.assemble` puts at index 0.
The history carried between exchanges drops the same number."""

TOOL_RESULTS_DIRNAME = "tool_results"
"""Offloaded tool results, one subdirectory per session."""

RECORDS_DIRNAME = "compaction_records"
"""Compaction logs, one ``<session>.jsonl`` per session."""


def _state_dir(config: AppConfig) -> Path:
    return config.state_dir()


def offload_store(config: AppConfig, session_id: str = "") -> FileOffloadStore:
    """Where *session_id*'s offloaded tool results are kept.

    References are workspace-relative, which is what ``read_file`` takes.
    """
    return FileOffloadStore(
        _state_dir(config) / TOOL_RESULTS_DIRNAME,
        session_id,
        reference_base=config.workspace,
    )


def compaction_log(config: AppConfig) -> JsonlCompactionLog:
    """The log every compaction in this deployment is appended to."""
    return JsonlCompactionLog(_state_dir(config) / RECORDS_DIRNAME)


class _SessionArchiver:
    """One session's :class:`CompactionArchiver` over the memory database.

    Adapts :class:`~omicsclaw.memory.SqliteCompactionArchive.append` to
    the protocol the context layer calls: it keeps the session's
    compaction number itself, seeded from the rows that are already
    there and raised by one per archived batch, so two compactions of
    one exchange never share a number even though ``compact()`` has no
    state to hand one down. ``seq_hint`` — where in the history the
    removed run began — is forwarded as the batch's start position,
    which is what it is a hint of.
    """

    def __init__(self, store: SqliteCompactionArchive, session_id: str) -> None:
        self._store = store
        self._session_id = session_id
        self._next: int | None = None

    async def archive(
        self, messages: Sequence[Message], *, seq_hint: int
    ) -> object:
        if self._next is None:
            self._next = await self._store.next_seq(self._session_id)
        seq = self._next
        self._next += 1
        written = await self._store.append(
            self._session_id, seq, messages, start_position=seq_hint
        )
        _log.info(
            "compaction.archived session=%s seq=%d rows=%d",
            self._session_id or "-",
            seq,
            written,
        )
        return written


def _compaction_archiver(app: "AgentApp", session_id: str) -> CompactionArchiver | None:
    """The archiver one exchange's compactions write through, or ``None``.

    ``None`` when :attr:`AppConfig.compaction_archive` is off or the
    deployment keeps no memory database — the switch owns the table
    writes, the tools and the prompt section together, so a deployment
    that turns it off sees none of the three.
    """
    if not app.config.compaction_archive or app.memory is None:
        return None
    return _SessionArchiver(
        SqliteCompactionArchive(app.memory.database), session_id
    )


def build_compactor(
    app: "AgentApp",
    *,
    session_id: str = "",
    state: CompactionState | None = None,
    on_measure: Callable[[BudgetReport], None] | None = None,
    on_compact: Callable[[CompactionRecord], None] | None = None,
) -> ProgressiveCompactor:
    """A compactor for one exchange of *session_id*.

    Compacts from :attr:`AppConfig.compact_at` upwards, keeps the system
    message, offloads into :func:`offload_store`, hands summarized-away
    messages to an extractor when there is one, archives what each
    compaction removes into the memory database when
    :attr:`AppConfig.compaction_archive` is on, merges a written-back
    compaction's ``tried_solutions`` anchor into the session scratch,
    and logs every compaction that changed the conversation or failed
    to :func:`compaction_log`. A listener that raises or a log that
    cannot be written costs a log line, never the exchange or the other.

    **The extractor is built here, from the app, on every call**, rather
    than held on the app. It needs a model and a store, and the model is
    :attr:`AgentApp.summarizer` — so building it once at assembly would
    freeze a summarizer the app can be given a different one of later,
    and extraction would quietly keep calling the old one. Reading both
    fields in the same breath is what keeps them the same decision.
    :attr:`AgentApp.memory_extractor` overrides this when it is set, for
    a deployment that wants extraction through something else.

    :param state: Summary and anchors from the session's earlier exchanges.
    :param on_measure: Called with every pre-call measurement.
    :param on_compact: Called with every compaction record, before it is
        logged.
    """
    config = app.config
    log = compaction_log(config)
    extractor = app.memory_extractor
    if extractor is None:
        extractor = build_memory_extractor(app.memory, app.summarizer)
    archiver = _compaction_archiver(app, session_id)
    scratch = (
        SessionScratch(app.memory.database) if app.memory is not None else None
    )

    async def record(entry: CompactionRecord) -> None:
        if on_compact is not None:
            try:
                on_compact(entry)
            except Exception:
                _log.exception("a compaction listener failed")
        _log.info(
            "compacted at %s: %d→%d tokens, %d→%d messages, %d offloaded%s%s",
            entry.pressure,
            entry.tokens_before,
            entry.tokens_after,
            entry.msgs_before,
            entry.msgs_after,
            len(entry.offloaded),
            "" if entry.written_back else " (not kept)",
            f" (degraded: {entry.degraded})" if entry.degraded else "",
        )
        if scratch is not None:
            tried = entry.anchors.tried_solutions
            if entry.written_back and tried not in ("", "N/A"):
                try:
                    await scratch.merge(
                        session_id, SCRATCH_KIND_TRIED_SOLUTIONS, tried
                    )
                except Exception as error:  # noqa: BLE001 - like the log below
                    _log.warning(
                        "could not merge tried_solutions into the scratch "
                        "of session %s: %s",
                        session_id or "-",
                        error,
                    )
        try:
            await log.append(session_id, entry)
        except OSError as error:
            _log.warning(
                "could not log a compaction of session %s: %s",
                session_id or "-",
                error,
            )

    return ProgressiveCompactor(
        app.budget,
        summarizer=app.summarizer,
        offloader=Offloader(offload_store(config, session_id)),
        extractor=extractor,
        archiver=archiver,
        state=state,
        pinned=PINNED_SYSTEM_MESSAGES,
        trigger=config.compact_at,
        on_measure=on_measure,
        on_compact=record,
    )
