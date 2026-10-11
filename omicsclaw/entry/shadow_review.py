"""Checkpoint-triggered shadow review (P6 item 4).

A small automatic reviewer that watches the P2 artifact plane and, at a
checkpoint, makes one bounded model call whose verdict — ``pass``,
``warn`` or ``fail`` — is injected back into the main loop. The two
triggers:

* **count** — :data:`SHADOW_REVIEW_ARTIFACT_THRESHOLD` (3) or more new
  artifact records since the last checkpoint, and
* **structured block** — one new artifact of a structured kind
  (:data:`STRUCTURED_ARTIFACT_KINDS` — ``report`` and ``table``, the two
  kinds the results tray renders as a structured block rather than a
  media card) arrives.

The detector is a *checker*, not a listener: at each turn submission the
desktop surface asks the shared :class:`~omicsclaw.memory.artifacts.
ArtifactStore` for the newest records of the session and feeds them to a
per-session :class:`ShadowReviewCheckpoint` watermark, so no insert path
is touched and every writer (the ``save_artifact`` sink and the job
scanner) is covered by one query.

The review itself is fire-and-forget: it is scheduled as a background
task, and a failure or timeout is logged and skipped — it **never blocks
the main loop**. A ``warn``/``fail`` verdict lands in two additive
places:

* a one-shot context note attached to the **next** turn's request
  (:class:`ShadowReviewNudge`, mounted by
  :func:`~omicsclaw.entry.nudges.build_augmentor`), and
* a ``shadow_review`` frame published into the running exchange's stream
  when one is live (the desktop surface's publisher), following the
  additive frame precedent of the R1 ``suggestions`` frame.

``pass`` is quiet in both places.

The whole mechanism is off unless :attr:`~omicsclaw.entry.config.
AppConfig.shadow_review` is set: a deployment that has not asked for it
keeps byte-identical turns.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Sequence

from omicsclaw.memory.artifacts import ArtifactRecord, ArtifactStore
from omicsclaw.schema import Message, Role

if TYPE_CHECKING:  # pragma: no cover - a type-only import
    from omicsclaw.entry.assembly import AgentApp

__all__ = [
    "MAX_REVIEW_NOTE_CHARS",
    "SHADOW_REVIEW_ARTIFACT_THRESHOLD",
    "STRUCTURED_ARTIFACT_KINDS",
    "ShadowReview",
    "ShadowReviewCheckpoint",
    "ShadowReviewNudge",
    "ShadowReviewVerdict",
    "parse_verdict",
]

_log = logging.getLogger(__name__)

SHADOW_REVIEW_ARTIFACT_THRESHOLD = 3
"""New artifacts of one session that trigger a review checkpoint."""

STRUCTURED_ARTIFACT_KINDS: frozenset[str] = frozenset({"report", "table"})
"""The artifact kinds that read as a structured block.

The P2 vocabulary (``omicsclaw/schema/artifacts.py``) has seven words;
these two are the ones whose *rendering* is a structured block — a
markdown report or a data table — as opposed to a media card (``figure``,
``pdf``) or a raw data file (``h5ad``, ``rds``, ``other``). One new
artifact of either kind is a checkpoint on its own, because a block of
structure is what the reviewer is for."""

MAX_REVIEW_NOTE_CHARS = 1000
"""Longest note a verdict may inject into the next turn's request.

Capped so a reviewer's essay cannot crowd the conversation it reviews."""

_REVIEW_LOOKBACK = 25
"""Newest artifact records one checkpoint evaluation reads per session."""

_MAX_ARTIFACT_LINE_CHARS = 200
_MAX_HISTORY_EXCERPT_CHARS = 4000
_MAX_TOKENS = 256

_VERDICT_LINE = re.compile(r"\AVERDICT:\s*(PASS|WARN|FAIL)\b")


@dataclass(frozen=True, slots=True)
class ShadowReviewVerdict:
    """One reviewer answer: a verdict word and a bounded note.

    ``verdict`` is ``"pass"``, ``"warn"`` or ``"fail"``; anything the
    reviewer wrote that is not recognisably one of those is read as
    ``"pass"`` — a garbled review is not a reason to interrupt anyone.
    """

    verdict: str
    note: str = ""


def parse_verdict(text: str) -> ShadowReviewVerdict:
    """Read a reviewer answer as a verdict, quietly.

    The first line must start with ``VERDICT: PASS|WARN|FAIL`` (the same
    ``VERDICT:`` convention the module-review archive uses); the rest of
    the answer is the note, capped at :data:`MAX_REVIEW_NOTE_CHARS`.
    """
    match = _VERDICT_LINE.match(str(text or "").lstrip())
    if match is None:
        return ShadowReviewVerdict("pass")
    note = str(text).split("\n", 1)[1].strip() if "\n" in str(text) else ""
    return ShadowReviewVerdict(match[1].lower(), note[:MAX_REVIEW_NOTE_CHARS])


@dataclass(slots=True)
class ShadowReviewCheckpoint:
    """One session's window of new artifacts since its last review.

    A watermark rather than a counter over everything: the first
    evaluation after the checkpoint exists records the newest artifact
    and triggers nothing, so a session's pre-existing results tray is
    never reviewed on sight. Every later evaluation counts only records
    newer than the watermark, accumulates them into a batch, and fires —
    returning and resetting the batch — when the batch holds
    :data:`SHADOW_REVIEW_ARTIFACT_THRESHOLD` records or when any one of
    them has a structured kind.
    """

    session_id: str
    _watermark: ArtifactRecord | None = None
    _batch: list[ArtifactRecord] = field(default_factory=list)

    def evaluate(
        self, records: Sequence[ArtifactRecord]
    ) -> list[ArtifactRecord]:
        """Fold *records* (newest first, as the store returns them) into
        the window and answer the batch a trigger fires, if any.

        :param records: The store's newest rows for this session.
        :returns: The accumulated new records when this call crossed a
            trigger — the batch the reviewer should be shown, cleared by
            this call — or ``[]``.
        """
        if not records:
            return []
        newest = records[0]
        if self._watermark is None:
            self._watermark = newest
            return []
        new = [r for r in records if _newer(r, self._watermark)]
        self._watermark = newest
        if not new:
            return []
        # Prepend so the batch stays newest-first, matching the store's
        # own ordering — the reviewer prompt reads it top-down.
        self._batch[0:0] = new
        structured = any(
            record.kind in STRUCTURED_ARTIFACT_KINDS for record in new
        )
        if structured or len(self._batch) >= SHADOW_REVIEW_ARTIFACT_THRESHOLD:
            fired = self._batch
            self._batch = []
            return fired
        return []


def _newer(record: ArtifactRecord, watermark: ArtifactRecord) -> bool:
    """Whether *record* came after *watermark*.

    ``created_at`` is a wall-clock float and ties are real — two rows in
    the same instant — so the row id breaks them the way the store's own
    ``ORDER BY created_at DESC, id DESC`` ordering does.
    """
    return (record.created_at, record.id) > (
        watermark.created_at,
        watermark.id,
    )


class ShadowReviewNudge:
    """The one-shot context note a verdict left for a session's next turn.

    Satisfies the engine's ``TurnAugmentor`` protocol structurally, like
    :class:`~omicsclaw.context.MemoryNudge`: :meth:`augment` pops the
    pending note for its session exactly once, so the model sees the
    verdict with the next request and never again.
    """

    __slots__ = ("_notes", "_session_id")

    def __init__(self, notes: "dict[str, str]", session_id: str) -> None:
        self._notes = notes
        self._session_id = session_id

    async def augment(
        self,
        history: Sequence[Message],
        tools: Sequence[Any] = (),
    ) -> tuple[Message, ...]:
        note = self._notes.pop(self._session_id, None)
        if not note:
            return ()
        return (Message(role=Role.USER, content=note),)


_ReviewCall = Callable[
    [str, Sequence[ArtifactRecord], Sequence[Message]],
    Awaitable[ShadowReviewVerdict],
]


@dataclass(slots=True)
class ShadowReview:
    """The checkpoint detector and reviewer, one instance per deployment.

    Built only when :attr:`~omicsclaw.entry.config.AppConfig.shadow_review`
    is on; ``None`` on the app otherwise, which is the off state every
    existing path already handles.

    The artifact store is not known at build time — :func:`build_app`
    serves every surface and only the desktop constructs the
    :class:`~omicsclaw.entry.desktop.jobs_manager.JobsManager` — so the
    desktop server calls :meth:`bind_store` once it has one. Until then,
    and on every other surface, :meth:`evaluate` sees no store and
    answers ``[]``: nothing fires, nothing changes.
    """

    _app: "AgentApp"
    _model: str
    _timeout_s: float
    _review_call: _ReviewCall | None = None
    _store: ArtifactStore | None = None
    _checkpoints: dict[str, ShadowReviewCheckpoint] = field(default_factory=dict)
    _notes: dict[str, str] = field(default_factory=dict)

    def __init__(
        self,
        app: "AgentApp",
        *,
        model: str = "",
        timeout_s: float = 30.0,
        review_call: _ReviewCall | None = None,
    ) -> None:
        self._app = app
        self._model = model
        self._timeout_s = timeout_s
        # A test seam: the caller supplies the whole reviewer call, which
        # is how verdict handling is tested without a model on the wire.
        self._review_call = review_call
        self._store: ArtifactStore | None = None
        self._checkpoints: dict[str, ShadowReviewCheckpoint] = {}
        self._notes: dict[str, str] = {}

    def bind_store(self, store: ArtifactStore) -> None:
        """Point the detector at the desktop's artifact store."""
        self._store = store

    def evaluate(self, session_id: str) -> list[ArtifactRecord]:
        """The triggering batch of new artifacts for *session_id*, if any.

        Reads the store directly and never raises: a checkpoint that
        cannot be read is logged and answered ``[]``, because a review
        is a luxury and a broken one must not fail the turn that is
        about to start.
        """
        if self._store is None:
            return []
        try:
            records = self._store.list_artifacts(
                session_id=session_id, limit=_REVIEW_LOOKBACK
            )
        except Exception as error:  # noqa: BLE001 - see the docstring
            _log.info(
                "shadow review: checkpoint read failed for session %s (%s)",
                session_id,
                type(error).__name__,
            )
            return []
        checkpoint = self._checkpoints.get(session_id)
        if checkpoint is None:
            checkpoint = ShadowReviewCheckpoint(session_id)
            self._checkpoints[session_id] = checkpoint
        return checkpoint.evaluate(records)

    def fire(
        self,
        session_id: str,
        records: Sequence[ArtifactRecord],
        *,
        history: Sequence[Message] = (),
        publisher: Callable[[ShadowReviewVerdict], None] | None = None,
    ) -> "asyncio.Task[None]":
        """Schedule one review for *records* and return its task.

        Never awaited by the caller: the verdict lands whenever it lands
        and the next turn's nudge is the delivery vehicle. A caller that
        wants the outcome — a test, or a shutdown path — awaits the
        returned task.
        """
        return asyncio.create_task(
            self._review(session_id, records, history, publisher)
        )

    async def _review(
        self,
        session_id: str,
        records: Sequence[ArtifactRecord],
        history: Sequence[Message],
        publisher: Callable[[ShadowReviewVerdict], None] | None,
    ) -> None:
        try:
            verdict = await self._run_review_call(session_id, records, history)
        except Exception as error:  # noqa: BLE001 - never blocks the loop
            _log.info(
                "shadow review: reviewer call failed for session %s (%s)",
                session_id,
                type(error).__name__,
            )
            return
        if verdict.verdict == "pass":
            return
        self._notes[session_id] = _verdict_note(verdict)
        if publisher is not None:
            try:
                publisher(verdict)
            except Exception as error:  # noqa: BLE001 - the note is delivered
                _log.info(
                    "shadow review: notice publish failed for session %s (%s)",
                    session_id,
                    type(error).__name__,
                )
        _log.info(
            "shadow review: %s for session %s after %d artifact(s)",
            verdict.verdict,
            session_id,
            len(records),
        )

    async def _run_review_call(
        self,
        session_id: str,
        records: Sequence[ArtifactRecord],
        history: Sequence[Message],
    ) -> ShadowReviewVerdict:
        if self._review_call is not None:
            return await self._review_call(session_id, records, history)
        from omicsclaw.provider.factory import provider_from_env

        config = self._app.config
        model = self._model or config.model
        provider = provider_from_env(
            config.provider, model=model, max_tokens=_MAX_TOKENS
        )
        request = Message(
            role=Role.USER,
            content=_review_prompt(session_id, records, history),
        )
        async with asyncio.timeout(self._timeout_s):
            completion = await provider.generate([request], None)
        return parse_verdict(completion.message.content)

    def nudge_for(self, session_id: str) -> ShadowReviewNudge:
        """The augmentor that delivers this session's pending verdict note."""
        return ShadowReviewNudge(self._notes, session_id)


def _verdict_note(verdict: ShadowReviewVerdict) -> str:
    """The one-shot note a ``warn``/``fail`` verdict leaves for the model."""
    body = verdict.note or "no details were given"
    return (
        f"An automatic shadow review of this session's recent artifacts "
        f"returned a {verdict.verdict} verdict: {body} "
        f"Address the points it raises in your next work."
    )


def _review_prompt(
    session_id: str,
    records: Sequence[ArtifactRecord],
    history: Sequence[Message],
) -> str:
    """The bounded prompt the reviewer model is sent.

    Only the artifact ledger and a short excerpt of the last assistant
    reply go in — tool output and files stay out, for the same reason
    the App's module-review prompt keeps them out.
    """
    lines = [
        "You are a shadow reviewer for an omics data-analysis session.",
        "Review only the session's recent artifacts (below) for obvious "
        "problems: internally inconsistent results, missing provenance, "
        "a claim the listed artifacts cannot support.",
        "Answer with a first line of exactly 'VERDICT: PASS', "
        "'VERDICT: WARN' or 'VERDICT: FAIL', then one short paragraph "
        "naming what you saw. PASS means nothing needs attention.",
        f"session_id: {session_id}",
        "artifacts:",
    ]
    for record in records:
        lines.append(
            f"- kind={record.kind} title={record.title[:80]!r} "
            f"path={record.path.split('/')[-1]!r} size={record.size}"
        )
        if len(lines[-1]) > _MAX_ARTIFACT_LINE_CHARS:
            lines[-1] = lines[-1][:_MAX_ARTIFACT_LINE_CHARS]
    for message in reversed(history):
        if message.role is Role.ASSISTANT and message.content:
            excerpt = message.content[:_MAX_HISTORY_EXCERPT_CHARS]
            lines.append("last assistant reply (excerpt):")
            lines.append(excerpt)
            break
    return "\n".join(lines)
