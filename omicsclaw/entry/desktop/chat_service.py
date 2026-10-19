"""Shared Desktop chat admission and observation, independent of transport."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omicsclaw.entry.assembly import AgentApp
from omicsclaw.entry.events import TurnEvent, TurnEventType
from omicsclaw.entry.ingress import SenderPolicy
from omicsclaw.entry.session import SessionRegistry
from omicsclaw.entry.session_options import SessionOptionsState
from omicsclaw.tools.context import current_context
from omicsclaw.entry.stream import ObserverCapacityError, TurnObservation
from omicsclaw.entry.turn import TurnHandle

from .doctor import effective_model
from .interactions import DesktopInteractions
from .turn_observation import KEEPALIVE_INTERVAL_S, DesktopChatSSEBody
from .turn_submission import DesktopIngressError, decode_chat_stream_request

_log = logging.getLogger(__name__)
COMPACT_COMMAND = "/compact"


@dataclass(frozen=True, slots=True)
class ChatStream:
    """An admitted request: which exchange it resolved to, and its frames."""

    turn_id: str
    session_id: str
    body: DesktopChatSSEBody
    resumed: bool
    """Whether this request resolved to an exchange already started, by
    ``resume`` or by redelivering its ``source_request_id``, rather than
    starting one."""


async def open_chat_stream(
    app: AgentApp,
    document: Mapping[str, Any],
    *,
    policy: SenderPolicy | None = None,
    after_seq: int = 0,
    keepalive_s: float | None = KEEPALIVE_INTERVAL_S,
    interactions: DesktopInteractions | None = None,
    epoch: int | None = None,
) -> ChatStream:
    """Admit one parsed request and return the stream of its frames.

    The whole of ``/chat/stream`` above the HTTP boundary, with no
    framework in its signature.

    *after_seq* is the resume cursor, the body's ``after_seq`` as the HTTP
    route reads it; it defaults to "everything the ring still holds". A
    redelivered ``source_request_id`` on the same session resolves to the
    same exchange, and a new observation over it is opened at that
    cursor.

    With ``resume: true`` in the body, the exchange is found by
    ``(session_id, source_request_id)`` alone and observed from
    *after_seq*; ``content`` and ``permission_profile`` are not read, and
    nothing is started or queued, so a restarted backend or an exchange no
    longer retained can never turn a reconnect into a second run of the
    message.

    *interactions* is the server's
    :class:`~omicsclaw.entry.desktop.interactions.DesktopInteractions`:
    the request is recorded in it so ``/chat/abort`` can find the
    exchange, a ``permission_profile`` in the body is applied to the
    session in it before the exchange starts, and the body consults it for
    session grants and ``full_access``. ``None`` uses a fresh one that
    nothing else shares.

    Content equal to :data:`COMPACT_COMMAND` queues
    :meth:`~omicsclaw.entry.SessionRegistry.compact` for the session
    rather than a message; a redelivered ``source_request_id`` resolves
    to the same compaction while it is retained.

    :raises DesktopIngressError: the request was refused. No exchange is
        created on any refusal path, including the sender policy's. A
        resume that finds no retained exchange is 409
        ``exchange_not_retained``; an exchange that already has as many
        observers as it allows is 429 ``too_many_observers``.
    :raises RuntimeError: the app has no session registry, which means
        :func:`~omicsclaw.entry.attach_sessions` was never called.
    """
    settled = await admit_chat(app, document, policy=policy, interactions=interactions)
    return ChatStream(
        turn_id=settled.handle.turn_id,
        session_id=settled.handle.session_id,
        resumed=not settled.newly_admitted,
        body=_observed_body(
            app, settled.handle, after_seq, keepalive_s, settled.interactions, epoch
        ),
    )


def _maybe_fire_shadow_review(
    app: AgentApp,
    registry: SessionRegistry,
    session_id: str,
    *,
    values: Mapping[str, object] | None = None,
) -> Any:
    """Evaluate the shadow-review checkpoint and fire it, if crossed.

    Runs after the exchange exists, so the verdict's notice frame has a
    live stream to land in; the review itself is a background task and
    can never hold up the turn that just started.
    """
    active_values = values if values is not None else current_context().values
    options = SessionOptionsState.from_mapping(active_values)
    if options.verifier_mode is False:
        return None
    reviewer = app.shadow_review
    if reviewer is None and options.verifier_mode is True:
        reviewer = getattr(app, "verifier_review", None)
    if reviewer is None:
        return None
    records = reviewer.evaluate(session_id)
    if not records:
        return None
    session = registry.session(session_id)
    history = session.history if session is not None else ()

    def publish(verdict: Any) -> None:
        event = TurnEvent.shadow_review_verdict(
            verdict.verdict, verdict.note, session_id=session_id
        )
        for handle in registry.running():
            if handle.session_id == session_id:
                # The stream seals at EXCHANGE_END and ignores late
                # frames, so a verdict that arrives after the exchange
                # is a silent no-op here — the note still rides the
                # next turn through the nudge.
                handle.stream.publish(event)

    return reviewer.fire(
        session_id,
        records,
        history=history,
        publisher=publish,
        reviewer_model=options.reviewer_model,
    )


def _retained_exchange(registry: SessionRegistry, turn_id: str | None) -> TurnHandle:
    """The exchange a resume names, if the registry still retains it.

    :raises DesktopIngressError: 409 ``exchange_not_retained`` otherwise.
    """
    if turn_id is not None:
        try:
            return registry.handle(turn_id)
        except KeyError:
            pass
    raise DesktopIngressError("exchange_not_retained", status_code=409)


def _observed_body(
    app: AgentApp,
    handle: TurnHandle,
    after_seq: int,
    keepalive_s: float | None,
    interactions: DesktopInteractions,
    epoch: int | None = None,
) -> DesktopChatSSEBody:
    """A new SSE body over *handle*, observed from *after_seq*.

    A compaction exchange is handed to the body so that it reports its
    outcome, unless the ``COMPACTION`` event that already reported it is
    at or before the cursor.

    :raises DesktopIngressError: 429 ``too_many_observers``.
    """
    observation = _observe(handle, after_seq)
    compaction = handle if handle.compaction_only else None
    if compaction is not None and any(
        event.type is TurnEventType.COMPACTION and event.seq <= after_seq
        for event in handle.stream.retained()
    ):
        compaction = None
    return DesktopChatSSEBody(
        observation,
        keepalive_s=keepalive_s,
        after_seq=after_seq,
        approvals=handle.approvals,
        questions=handle.questions,
        interactions=interactions,
        provider=app.provider.name,
        model=effective_model(app),
        compaction=compaction,
        epoch=epoch,
    )


def _observe(handle: TurnHandle, after_seq: int) -> TurnObservation:
    try:
        return handle.observe(after_seq=after_seq)
    except ObserverCapacityError as exc:
        raise DesktopIngressError("too_many_observers", status_code=429) from exc


async def _compaction(
    registry: SessionRegistry, session_id: str, known: str | None
) -> TurnHandle:
    """The compaction *known* names if it is still retained, else a new one."""
    if known is not None:
        try:
            handle: TurnHandle = registry.handle(known)
        except KeyError:
            pass
        else:
            if handle.compaction_only:
                return handle
    return await registry.compact(session_id)


def workspace_payload(app: AgentApp) -> dict[str, Any]:
    """What ``GET /workspace`` answers: ``{workspace, trusted_dirs}``.

    ``trusted_dirs`` is always ``[]``.
    """
    return {"workspace": str(app.config.workspace), "trusted_dirs": []}


def change_workspace(app: AgentApp, document: Mapping[str, Any]) -> dict[str, Any]:
    """``PUT /workspace``: accept ``{workspace}`` naming the current one.

    Two spellings of one directory (``~``, ``..``, a trailing slash, a
    symlink) are the same workspace. Returns :func:`workspace_payload`.

    :raises DesktopIngressError: 422 ``workspace_required`` when
        ``workspace`` is missing or empty; 422 ``invalid_workspace`` when
        it is not a resolvable path; 409
        ``workspace_change_requires_restart`` when it names another
        directory.
    """
    declared = document.get("workspace")
    if not isinstance(declared, str) or not declared.strip():
        raise DesktopIngressError("workspace_required")
    if not _same_workspace(declared, app.config.workspace):
        raise DesktopIngressError("workspace_change_requires_restart", status_code=409)
    return workspace_payload(app)


def _same_workspace(declared: str, workspace: Path | str) -> bool:
    """Whether the client's *declared* path names the served *workspace*.

    :raises DesktopIngressError: 422 ``invalid_workspace`` when *declared*
        cannot be resolved to a path at all: an embedded NUL, a ``~user``
        naming no user, or a symlink loop on Python versions whose
        :meth:`~pathlib.Path.resolve` raises for one.
    """
    try:
        resolved = Path(declared.strip()).expanduser().resolve()
    except (ValueError, OSError, RuntimeError) as exc:
        raise DesktopIngressError("invalid_workspace") from exc
    return resolved == Path(workspace).expanduser().resolve()


@dataclass(frozen=True, slots=True)
class ChatAdmission:
    handle: TurnHandle
    newly_admitted: bool
    interactions: DesktopInteractions


async def admit_chat(
    app: AgentApp,
    document: Mapping[str, Any],
    *,
    policy: SenderPolicy | None = None,
    interactions: DesktopInteractions | None = None,
    deployment_values: Mapping[str, object] | None = None,
) -> ChatAdmission:
    """Use the shared receipt/lane once, applying entry policy only on admission."""
    request, inbound = prepare_chat_intent(
        app, document, deployment_values=deployment_values
    )
    if policy is not None and not policy.admits(inbound):
        raise DesktopIngressError("sender_not_allowed", status_code=403)
    registry = app.sessions
    if registry is None:
        raise RuntimeError("attach_sessions required for Desktop admission")
    interactions = (
        interactions if interactions is not None else DesktopInteractions(app)
    )
    if request.resume:
        handle = _retained_exchange(
            registry,
            interactions.turn_for(request.session_id, request.source_request_id),
        )
        return ChatAdmission(handle, False, interactions)
    handle, newly_admitted = await registry.admit(
        inbound, compaction_only=request.content.strip() == COMPACT_COMMAND
    )
    if newly_admitted and request.permission_profile:
        interactions.set_permission_profile(
            request.session_id, request.permission_profile
        )
    interactions.remember_request(
        handle.session_id, request.source_request_id, handle.turn_id
    )
    if newly_admitted:
        _maybe_fire_shadow_review(app, registry, handle.session_id, values=inbound.values)
    return ChatAdmission(handle, newly_admitted, interactions)


def prepare_chat_intent(app, document, *, deployment_values=None):
    """Decode the effective intent shared by admission and read-only validation."""
    request = decode_chat_stream_request(document)
    if request.workspace.strip() and not _same_workspace(
        request.workspace, app.config.workspace
    ):
        raise DesktopIngressError(
            "workspace_does_not_match_backend_runtime", status_code=409
        )
    inbound = request.to_inbound()
    inbound = dataclasses.replace(
        inbound,
        values={
            **inbound.values,
            **(deployment_values or {}),
            "__desktop_permission_profile__": request.permission_profile,
        },
    )
    return request, inbound
