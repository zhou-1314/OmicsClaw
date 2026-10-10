"""``omicsclaw/context`` — message lists in, message lists out.

Plan 0030 task C. Pure transforms over a conversation: no clock, no
I/O, no model. Everything here is ``(messages, budget) -> messages``,
which is what makes the whole compaction story testable without
inventing half a session to test it in.

**Every return value is a tuple, and none of them is the caller's
list.** The reference harness has a scar here: issue #117 was an
aliasing bug, and ``progressive_compactor.go:354-360`` still carries the
defensive copy and the explanation. Python looks safer than it is —
:class:`~omicsclaw.schema.Message` is frozen, so no single entry can be
edited behind a caller's back — but a returned *slice* of the caller's
list is the same bug in a different spelling: one ``append`` and two
conversations are one conversation.

**Nothing here truncates a message.** Capacity is solved at the
granularity of whole messages — :func:`fit_to_budget` drops the oldest
ones first, :func:`emergency_fit` skips the ones that do not fit — never
by shortening one. In particular
:attr:`~omicsclaw.schema.ToolCall.arguments` is never rewritten: ADR
0077 requires it to stay the unparsed JSON text the model emitted, byte
for byte, because prompt-prefix caching and replay evidence both read
those bytes. A truncated argument payload is also an *invalid* one,
which the model will be handed and will retry against forever.

:func:`drop_unanswered_calls` is the one function here that edits a
message. It takes whole tool calls off a turn when no tool result
answers them, and it leaves the text and the arguments of what it keeps
as they were. A tool result with no call, and an answered call whose
arguments do not parse, are still there afterwards.
"""

from __future__ import annotations

from typing import Sequence

from omicsclaw.schema import Message, Role, ToolCall

from .budget import ContextBudget
from .summary import is_summary_message
from .tokens import (
    TokenCounter,
    estimate_message_tokens,
    estimate_messages_tokens,
)

__all__ = [
    "MISSING_TOOL_RESULT",
    "drop_unanswered_calls",
    "emergency_fit",
    "fit_to_budget",
    "render_for_summary",
    "repair_tool_pairs",
    "split_head_tail",
]

MISSING_TOOL_RESULT = "[tool result unavailable: the context was compacted]"
"""Stands in for a tool result that compaction removed.

Worded rather than copied: the harness's string is Chinese prose about
its own compactor, and this one has to read as an explanation to the
model of why an answer it is looking at is not there. Callers who want
different words pass ``placeholder=``.
"""


def repair_tool_pairs(
    messages: Sequence[Message],
    *,
    placeholder: str = MISSING_TOOL_RESULT,
) -> tuple[Message, ...]:
    """Make every tool call and tool result find its partner, **in place**.

    Two repairs, in both directions, ported from ``compaction.go:
    195-239``: a tool result whose call is gone is dropped, and a tool
    call whose result is gone gets a placeholder result inserted
    directly after it. The Anthropic Messages API refuses a conversation
    where either is unpaired, so this is not tidiness — it is the
    difference between a compacted history and a 400.

    **Pairing is adjacency, not membership.** The Go tests two sets of
    ids and so did the first version of this function, and both are
    weaker than the rule they are defending. Anthropic requires every
    ``tool_use`` block of an assistant turn to be answered by a
    ``tool_result`` block in the *immediately following* user turn, and
    ``anthropic_provider.py:271-291`` flushes its batch of pending
    results the moment any other message interrupts them. A membership
    test says "the answer is somewhere in this list" and that is not the
    same statement. It goes wrong on parallel tool calls: an assistant
    turn asking for ``c0`` and ``c1`` whose ``c0`` result survives and
    whose ``c1`` result ends up behind a compaction summary encodes as
    ``assistant[tool_use c0, tool_use c1]``, ``user[tool_result c0]``,
    ``user[text]``, ``user[tool_result c1]`` — ``c1`` unanswered where it
    has to be answered, which is the 400 this function exists to
    prevent. So the answers an assistant turn may keep are exactly the
    contiguous run of ``Role.TOOL`` messages behind it; anything else
    wearing that role is unpaired wherever it happens to sit, and every
    call the run does not answer gets a placeholder.

    **Two lines of the Go cannot be taken literally.**

    *Which message is a tool result.* The harness asks
    ``m.ToolCallID != ""``, because its schema has three roles and folds
    observations into ``user``. This repository has a fourth role and
    the Anthropic adapter dispatches on it — ``Role.TOOL`` at
    ``anthropic_provider.py:276`` — so the test here is the role. A
    repair that used a different criterion than the adapter would be
    repairing something the adapter cannot see.

    *What the placeholder is.* The harness builds
    ``Message{Role: RoleUser, ToolCallID: tc.ID}`` for the same reason.
    Translated literally that becomes ``Message(role=USER,
    tool_call_id=...)``, and ``anthropic_provider.py:271-291`` branches
    on role alone: the message would encode as an ordinary text turn,
    ``tool_call_id`` would be dropped on the floor, and the unanswered
    ``tool_use`` block would still be unanswered — the exact API 400
    this function exists to prevent, now with the appearance of a fix.
    So the placeholder is a real ``Role.TOOL`` message.

    ``is_error`` stays ``False``: nothing failed. Marking it an error
    would teach the model to retry a call that already succeeded.

    **The role test is ``==``, not ``is``.** Decision Q11 spells it
    ``message.role == Role.TOOL`` and both adapters are written that way
    (``anthropic_provider.py:272,276,281``; ``openai_provider.py:140``
    converts with ``str()`` first). :class:`~omicsclaw.schema.Message` is
    a dataclass with no validation, so a history deserialized from a
    session row arrives with plain ``str`` roles, and
    :class:`~omicsclaw.schema.Role` is a ``StrEnum`` precisely so that
    keeps working. Under ``is`` it does not: every ``str`` role misses
    every branch, this function answers one ``tool_use`` with two
    ``tool_result`` messages, and the failure is silent.
    """
    repaired: list[Message] = []
    index = 0
    total = len(messages)
    while index < total:
        message = messages[index]
        index += 1
        if message.role == Role.TOOL:
            # Reached without an assistant turn in front of it: whatever
            # it answered is not where the API will look for it.
            continue
        repaired.append(message)
        if message.role != Role.ASSISTANT or not message.tool_calls:
            continue
        run_end = index
        while run_end < total and messages[run_end].role == Role.TOOL:
            run_end += 1
        answers: dict[str, Message] = {}
        for answer in messages[index:run_end]:
            answers.setdefault(answer.tool_call_id, answer)
        for call in message.tool_calls:
            found = answers.pop(call.id, None)
            if found is None:
                found = Message.tool(tool_call_id=call.id, content=placeholder)
            repaired.append(found)
        index = run_end
    return tuple(repaired)


def drop_unanswered_calls(messages: Sequence[Message]) -> tuple[Message, ...]:
    """Remove every tool call that no tool result answers.

    The results that can answer a turn's calls are the ``Role.TOOL``
    messages directly behind it, the same adjacency
    :func:`repair_tool_pairs` pairs by. One result answers one call:
    each call, in the order the turn lists them, takes one result that
    carries its id, and a call left without one is unanswered. Ids are
    compared as they are, the empty id included, and a result marked as
    an error answers its call like any other.

    An unanswered call is taken off its turn. The turn keeps its text,
    its reasoning and its answered calls, unedited. If that leaves the
    turn with no call and no text other than whitespace (what
    ``str.strip`` removes), the whole turn is removed. A turn that lost
    no call is never touched, whatever it holds.

    No result is written in a removed call's place.
    :func:`repair_tool_pairs` would write a placeholder that says the
    result was compacted away, which is false of a call that never ran.
    The call's arguments may also be cut off inside the JSON, and the
    Anthropic adapter refuses to encode those.

    A run that the output ceiling cuts off inside a tool call ends on
    such a turn: the engine records the turn and runs none of its calls.
    DeepSeek answers a request that carries one with a 400, and the
    Anthropic Messages API documents the same requirement.

    The result is a new tuple. With nothing to remove it holds the same
    message objects in the same order. A tool result whose call is
    missing is left for :func:`repair_tool_pairs`. Roles are compared
    with ``==`` for the reason given there.
    """
    kept: list[Message] = []
    index = 0
    total = len(messages)
    while index < total:
        message = messages[index]
        index += 1
        if message.role != Role.ASSISTANT or not message.tool_calls:
            kept.append(message)
            continue
        run_end = index
        while run_end < total and messages[run_end].role == Role.TOOL:
            run_end += 1
        unclaimed = [answer.tool_call_id for answer in messages[index:run_end]]
        calls: list[ToolCall] = []
        for call in message.tool_calls:
            if call.id in unclaimed:
                unclaimed.remove(call.id)
                calls.append(call)
        if len(calls) == len(message.tool_calls):
            kept.append(message)
        elif calls or message.content.strip():
            kept.append(message.replace(tool_calls=calls))
    return tuple(kept)


def split_head_tail(
    messages: Sequence[Message],
    *,
    pinned: int,
    min_tail: int,
) -> tuple[tuple[Message, ...], tuple[Message, ...], tuple[Message, ...]]:
    """Cut a conversation into ``(pinned, head, tail)``.

    *pinned* messages at the front are kept whatever happens, *min_tail*
    at the back are kept whatever happens, and the head between them is
    what a compaction is allowed to touch.

    **Nobody here guesses that message zero is a system prompt.** Every
    compactor in the harness requires it and returns the input untouched
    when it is missing (``compaction.go:103-105``,
    ``progressive_compactor.go:235-237``), which is safe there because
    its engine injects one at position 0 on every load
    (``history.go:62-64``). This project's engine injects nothing, so
    copying that precondition would turn the compactor into a silent
    no-op for any conversation assembled differently — a thing that
    resolves, is never read, and never goes red. The caller says how
    many messages are pinned instead: ``pinned=1`` is the harness's
    behaviour, ``pinned=0`` says there is no protected prefix.

    Ported from ``progressive_compactor.go:230-245``, including its one
    structural guard: when the messages after the pinned prefix number
    ``min_tail`` or fewer there is **no head**, and the caller skips the
    tier rather than compacting something that is already minimal.
    """
    kept = tuple(messages[:pinned])
    rest = tuple(messages[pinned:])
    if len(rest) <= min_tail:
        return kept, (), rest
    head_end = len(rest) - min_tail
    return kept, rest[:head_end], rest[head_end:]


def fit_to_budget(
    messages: Sequence[Message],
    budget: ContextBudget,
    *,
    pinned: int = 0,
    min_tail: int,
    counter: TokenCounter | None = None,
    target_tokens: int | None = None,
) -> tuple[Message, ...]:
    """Drop the oldest messages of the head until the rest fits, then repair.

    :func:`split_head_tail` cuts the conversation into the pinned
    prefix, the head and the tail. The prefix and the tail are never
    dropped. The head loses its oldest droppable message, the result is
    measured, and this repeats until it fits, so what is left is the
    newest end of the conversation and as much of it as the target
    allows.

    **The input comes back as it is**, neither rebuilt nor repaired,
    when it is empty, already fits, has no head, or has a head in which
    nothing can be dropped. A function that rebuilt the conversation on
    every call would churn the prompt prefix for nothing.

    **Never dropped**, besides the prefix and the tail:

    * the system messages the head opens on, however many there are.
      They are in the head when ``pinned`` does not cover them, as with
      ``pinned=0``.
    * every compaction summary, which is a user message that
      :func:`~omicsclaw.context.summary.is_summary_message` accepts.
    * the last user message in the head, unless the tail itself opens on
      a user message. It is the request in flight when the tail holds
      no request, and otherwise the request whose replies the tail
      opens on.
    * the tool round the tail finishes. When the tail opens on tool
      results and one of them answers the call the head ends on, that
      call stays together with the results it already has in the head,
      so the results in the tail keep the call they answer. The call is
      the last message of the head that is not a tool result, and only
      the unbroken run of tool results the tail opens on is read.

    **An earlier request goes with its last assistant reply.** It stays
    while an assistant message of its exchange is left, and it is
    dropped in the same step as the last one. A tool result does not
    keep it: once the call is dropped its results are orphans and the
    repair deletes them. A request with no assistant reply behind it is
    dropped when its turn comes. A system message in the middle of the
    conversation counts as a reply.

    **The result can be over the target.** When everything droppable is
    gone the result is the prefix, the messages listed above and the
    tail, whatever they add up to. The target is also checked before
    :func:`repair_tool_pairs` runs: an orphaned result the repair is
    about to delete is counted, and a placeholder it is about to add is
    not.

    **A cut in the head makes no placeholder.** Dropping from the oldest
    end can leave tool results whose call is gone, and the repair
    deletes them. It cannot leave a call whose results are gone. A
    placeholder in the result stands for a call the input itself left
    unanswered, except in the first case below.

    **A pinned prefix that reaches into the conversation** has two cases
    this function does not guard:

    * A prefix that stops inside a tool round, on the call or on some of
      its results, leaves the rest of that round's results as the oldest
      messages of the head. Every truncation drops them first and the
      repair answers the call with placeholders. A placeholder can be
      larger than the small result it replaces, so the result can be
      larger than the input. A prefix that stops between rounds does not
      meet this.
    * A prefix that ends on a request keeps the request, and its replies
      in the head are dropped like any others. The request can then
      stand in the result with no reply to it. Such a prefix already
      stops between rounds, so stopping between rounds does not avoid
      this.

    A prefix that covers only the system prompt meets neither. The
    entry layer calls with ``pinned=1`` over one system message.

    **``target_tokens`` is what "fits" means**, and it defaults to
    ``budget.usable_tokens``. The harness has the same two numbers and
    they are deliberately *different*: ``NewTokenBudgetCompactor``
    trims to ``contextWindow * 80/100`` (``compaction.go:86-92``) while
    the tiers that call it trigger from 60% of the same window
    (``progressive_compactor.go:129-133, 215``), so a fallback from
    ``TierFull`` genuinely removes messages. Collapse the two onto one
    number and the fallback becomes a provable no-op: every tier below
    ``EMERGENCY`` sits under 95% of ``usable_tokens`` by definition, so
    ``estimate <= usable`` holds on arrival and the first branch below
    returns the input byte for byte.
    :func:`~omicsclaw.context.compaction.compact` therefore passes a
    target drawn from :class:`~omicsclaw.context.budget.ContextBudget`'s
    own declared tier thresholds rather than from a fresh literal.
    """
    usable = budget.usable_tokens if target_tokens is None else target_tokens
    if not messages:
        return tuple(messages)
    if estimate_messages_tokens(messages, counter=counter) <= usable:
        return tuple(messages)

    kept, head, tail = split_head_tail(messages, pinned=pinned, min_tail=min_tail)
    if not head:
        return tuple(messages)

    held = _held(head, tail)
    if len(held) == len(head):
        return tuple(messages)
    candidate: tuple[Message, ...] = ()
    for cut in range(1, len(head) + 1):
        if cut - 1 in held:
            continue
        staying = held | _request_of_oldest_reply(head, held, cut)
        survivors = (*(head[i] for i in range(cut) if i in staying), *head[cut:])
        candidate = (*kept, *survivors, *tail)
        if estimate_messages_tokens(candidate, counter=counter) <= usable:
            break
    return repair_tool_pairs(candidate)


def _held(head: tuple[Message, ...], tail: tuple[Message, ...]) -> frozenset[int]:
    """Positions in *head* that a truncation never drops.

    The system messages the head opens on, every compaction summary, and
    the last user message in the head unless the tail itself opens on a
    user message. That last one is the request in flight when the tail
    holds no request, and otherwise the request whose replies the tail
    opens on. The tool round the tail finishes is held too, so the
    results the tail opens on keep the call they answer.
    """
    held: set[int] = set()
    for index, message in enumerate(head):
        if message.role != Role.SYSTEM:
            break
        held.add(index)
    held.update(
        index
        for index, message in enumerate(head)
        if message.role == Role.USER and is_summary_message(message)
    )
    if not (tail and tail[0].role == Role.USER):
        for index in range(len(head) - 1, -1, -1):
            if head[index].role == Role.USER:
                held.add(index)
                break
    held.update(_round_the_tail_finishes(head, tail))
    return frozenset(held)


def _round_the_tail_finishes(
    head: tuple[Message, ...], tail: tuple[Message, ...]
) -> range:
    """Positions in *head* of a tool round whose results run on into *tail*.

    The call is the assistant message in front of the tool results the
    head ends on, and the round counts when one of the tool results the
    tail opens on answers it. Pairing is by adjacency, the way
    :func:`repair_tool_pairs` reads it. A call that sits in the pinned
    prefix is not looked for.
    """
    start = len(head)
    while start and head[start - 1].role == Role.TOOL:
        start -= 1
    if start == 0:
        return range(0)
    caller = head[start - 1]
    if caller.role != Role.ASSISTANT or not caller.tool_calls:
        return range(0)
    asked = {call.id for call in caller.tool_calls}
    for message in tail:
        if message.role != Role.TOOL:
            break
        if message.tool_call_id in asked:
            return range(start - 1, len(head))
    return range(0)


def _request_of_oldest_reply(
    head: tuple[Message, ...], held: frozenset[int], cut: int
) -> frozenset[int]:
    """The request that stays because a reply to it does.

    Everything from *cut* on survives. Tool results are passed over:
    the ones standing at the cut have lost their call to it and the
    repair is about to delete them, so they keep no request. When the
    oldest survivor that is neither held nor a tool result is a reply,
    the nearest user message before it is its request, and the two stay
    together.
    """
    for index in range(cut, len(head)):
        if index in held or head[index].role == Role.TOOL:
            continue
        if head[index].role == Role.USER:
            return frozenset()
        for owner in range(index - 1, -1, -1):
            if head[owner].role == Role.USER:
                return frozenset({owner})
        return frozenset()
    return frozenset()


def emergency_fit(
    messages: Sequence[Message],
    budget: ContextBudget,
    *,
    pinned: int = 0,
    counter: TokenCounter | None = None,
) -> tuple[Message, ...]:
    """Lossy truncation that keeps the task in view.

    Ported from ``compaction.go:134-179`` together with the reason its
    comment gives, which is measured rather than argued:

    * **The first message after the pinned prefix is kept
      unconditionally.** It carries the task. An emergency truncation
      that throws it away leaves the model with no goal and it spins:
      the harness recorded 143 consecutive emergency turns and a failed
      task before this line was added (issue #117).
    * **Newest first, and a message that does not fit is skipped, not
      shortened.** One enormous tool result would otherwise fill the
      emergency view by itself and push the task back out of it. Older
      and smaller messages still get their chance.

    *pinned* is on this function for the reason it is on
    :func:`split_head_tail`: the harness hardcodes "message zero is the
    system prompt" and this layer refuses to guess. Plan 0030 §3.2 omits
    the parameter here, which would have left
    :func:`~omicsclaw.context.compaction.compact` unable to honour its
    own ``pinned`` on the one path that discards the most.
    """
    if not messages:
        return tuple(messages)
    kept = tuple(messages[:pinned])
    rest = tuple(messages[pinned:])
    if not rest:
        return tuple(messages)
    survivors = _emergency_survivors(kept, rest, budget, counter)
    return repair_tool_pairs((*kept, *survivors))


def _emergency_survivors(
    kept: tuple[Message, ...],
    rest: tuple[Message, ...],
    budget: ContextBudget,
    counter: TokenCounter | None,
    target_tokens: int | None = None,
) -> tuple[Message, ...]:
    """The task anchor plus whatever else fits, oldest-to-newest order.

    *target_tokens* is what the survivors must fit in; ``None`` means
    ``budget.usable_tokens``.

    Separate from :func:`emergency_fit` so that
    :func:`~omicsclaw.context.compaction.plan_compaction` can put the
    survivors in a plan and let one repair pass run over the assembled
    result, instead of repairing twice.
    """
    task = rest[0]
    later = rest[1:]
    target = budget.usable_tokens if target_tokens is None else target_tokens
    remaining = target - estimate_messages_tokens((*kept, task), counter=counter)
    keep = [False] * len(later)
    for index in range(len(later) - 1, -1, -1):
        cost = estimate_message_tokens(later[index], counter=counter)
        if cost <= remaining:
            remaining -= cost
            keep[index] = True
    return (task, *(m for index, m in enumerate(later) if keep[index]))


def render_for_summary(messages: Sequence[Message]) -> str:
    """Flatten messages into the text handed to a summarizing model.

    Ported line for line from ``progressive_compactor.go:391-404``::

        [tool_result <id>]: <content>
        [<role>]: <content>
        [tool_call <name>(<id>)]: <arguments>

    joined with newlines. A tool result contributes its one line and
    nothing else; any other message contributes its content when it has
    some, then one line per tool call it requested.

    **Nothing is truncated, at any length.** The Go does not truncate
    either, and the layer being replaced — which did, at 70/30 — has had
    every one of the three paths that called it deleted. Size is handled
    by :func:`fit_to_budget` dropping whole messages before this is
    reached, never by a renderer quietly deciding how much of a tool
    result matters.

    **This output goes to the summarizer and comes back as a summary. It
    never becomes a** :attr:`~omicsclaw.schema.Message.content` **the
    agent will read.** That is the only reason it can be plain prose.
    The replaced layer rendered historical tool calls as
    ``<prior-tool-calls names="…"/>`` because prose in the *model's own*
    view triggers few-shot imitation — the model starts narrating tool
    calls instead of making them — while a structured tag reads as
    metadata and is not repeated. That finding still holds; it simply
    has nothing to apply to while this is the only renderer here and its
    output only ever travels outward. **Anyone adding a second renderer,
    or routing this one's output back into the conversation, has to come
    back and read this paragraph first.**
    """
    lines: list[str] = []
    for message in messages:
        if message.role == Role.TOOL:
            lines.append(f"[tool_result {message.tool_call_id}]: {message.content}")
            continue
        if message.content:
            # ``str`` and not ``.value``: a ``StrEnum`` prints as its
            # value, and a history deserialized from a session row
            # carries plain ``str`` roles that have no ``.value``.
            lines.append(f"[{message.role!s}]: {message.content}")
        for call in message.tool_calls:
            lines.append(f"[tool_call {call.name}({call.id})]: {call.arguments}")
    return "\n".join(lines)
