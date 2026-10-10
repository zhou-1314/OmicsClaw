"""``ask_user``: one question for the person, answered before the tool returns.

The tool reaches the person only through
:func:`~omicsclaw.tools.context.ask_question`, so how a question is shown
and how a reply is read belong to whoever bound the question channel. The
result is JSON that repeats the question beside its answer.

:func:`option_numbers` is shared with those surfaces: it decides which
labels are refused here and which replies a surface reads as option
numbers, so that no label can be mistaken for a number.

**Leaf-adjacent.** ``omicsclaw.schema``, ``omicsclaw.tools.base``,
``omicsclaw.tools.context``, ``omicsclaw.tools.function_tool`` and the
standard library.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from omicsclaw.schema import ToolDefinition

from ..base import ApprovalMode, RiskLevel, ToolPolicy
from ..context import (
    AnswerStatus,
    QuestionAnswer,
    QuestionOption,
    QuestionRequest,
    QuestionUnavailable,
    ask_question,
)
from ..function_tool import ToolArgumentError, decode_arguments, validate_arguments

TOOL_NAME = "ask_user"

MAX_QUESTION_CHARS = 2000
"""Characters a question may have."""

MIN_OPTIONS = 2
"""Fewest options a question that offers any may offer."""

MAX_OPTIONS = 6
"""Most options a question may offer."""

MAX_LABEL_CHARS = 80
"""Characters an option's label may have."""

MAX_DESCRIPTION_CHARS = 300
"""Characters an option's description may have."""

ASK_USER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["question"],
    "additionalProperties": False,
    "properties": {
        "question": {
            "type": "string",
            "description": (
                "The whole question, self-contained: the person may read it "
                "away from this conversation, on a phone."
            ),
        },
        "options": {
            "type": "array",
            "description": (
                "2 to 6 answers to offer when the choice is among known "
                "alternatives. Omit for an open question."
            ),
            "items": {
                "type": "object",
                "required": ["label"],
                "additionalProperties": False,
                "properties": {
                    "label": {
                        "type": "string",
                        "description": (
                            "Short answer text, at most 80 characters, not a "
                            "bare number."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "What choosing it means, if the label does not say."
                        ),
                    },
                },
            },
        },
        "multi_select": {
            "type": "boolean",
            "description": "True when more than one option may be chosen.",
        },
    },
}
"""The arguments the model is shown. The bounds the descriptions state are
checked by :meth:`AskUserTool.execute`, since the shared validator does not
check lengths or item counts."""

DESCRIPTION = "\n\n".join(
    (
        "Ask the person you are working for one question, and wait for the "
        "answer.",
        "Use it only when you cannot do the task well without something only "
        "they know or decide: which group is the control, which organism or "
        "genome build, whether to overwrite an existing report, which of two "
        "defensible analyses they want. Look first: a question the data, the "
        "files, the metadata or a SKILL.md can answer is not a question for "
        "the person.",
        "Do not use it to ask whether you may run a tool (tools that need "
        "consent ask for it themselves), to ask whether to continue, or to "
        "confirm a plan you could simply carry out. Do not ask about "
        "parameters or thresholds a skill's methodology already fixes. Never "
        "ask for a password, API key, token or other secret.",
        "Offer 2 to 6 options when the choice is among known alternatives, "
        "with a short label each and a description where the label is not "
        "enough. Put the option you recommend first and say so in its "
        "description. Set multi_select when more than one may apply. The "
        "person can always answer in their own words instead.",
        "One question per call. Calls placed after this one in the same "
        "message wait for the answer but were decided before it; put anything "
        "that depends on the answer in a later message.",
        "The result is JSON with status answered, declined or no_answer, and "
        "repeats the question. After declined or no_answer, do not ask the "
        "same question again in this request.",
    )
)
"""What the model is told about the tool, one paragraph to an entry. Every
rule about when to ask is here; the system prompt says nothing about this
tool."""

DECLINED_NOTE = (
    "The person chose not to answer. Do not ask this again in this request; "
    "proceed on your best judgement and say what you assumed."
)
"""What the result tells the model after :attr:`AnswerStatus.DECLINED`."""

NO_ANSWER_NOTE = (
    "Nobody answered. They may reply later in a new message. Finish what does "
    "not depend on the answer, then end your reply by restating the question."
)
"""What the result tells the model after :attr:`AnswerStatus.NO_ANSWER`."""

_POLICY = ToolPolicy(
    risk_level=RiskLevel.LOW,
    approval_mode=ApprovalMode.AUTO,
    read_only=True,
    concurrency_safe=False,
    allowed_in_background=False,
)
"""Asking changes nothing, so the tool is ``LOW``, ``AUTO`` and
``read_only``: it runs in read-only mode and shows no approval card of its
own. ``concurrency_safe=False`` makes each call run alone, so one question
is on screen at a time and never beside an approval card."""

_NUMBER_LIST = re.compile(r"[0-9]+(?:(?:\s*,\s*|\s+)[0-9]+)*")

_LONGEST_NUMBER = 10
"""Digits of a number that are read. Anything longer is past every option
whatever its remaining digits are, and :func:`int` refuses very long ones."""


def option_numbers(text: str) -> tuple[int, ...] | None:
    """The option numbers *text* spells, or ``None`` when it spells anything else.

    A list of numbers is whole numbers in ASCII digits, separated by commas
    or white space, with only white space around them: ``"2"``, ``"1, 3"``,
    ``" 1 3 "``. Leading zeros are ignored.

    Args:
        text: A reply, or an option's label.

    Returns:
        The numbers in the order written, repeats included; a number of
        more than ten digits comes back as at least ``10**9``. ``None`` for
        text that is not such a list, the empty string included.
    """
    stripped = text.strip()
    if not _NUMBER_LIST.fullmatch(stripped):
        return None
    return tuple(
        int((token.lstrip("0") or "0")[:_LONGEST_NUMBER])
        for token in re.split(r"[\s,]+", stripped)
    )


class AskUserTool:
    """Ask the person one question and return their answer as JSON.

    Satisfies :class:`~omicsclaw.tools.base.Tool` structurally. It needs no
    workspace and no configuration: the question goes to whatever
    :attr:`~omicsclaw.tools.context.ToolContext.question` is bound when it
    runs.
    """

    policy = _POLICY

    def __init__(self) -> None:
        self._definition = ToolDefinition(
            name=TOOL_NAME,
            description=DESCRIPTION,
            input_schema=copy.deepcopy(ASK_USER_SCHEMA),
        )

    @property
    def name(self) -> str:
        return TOOL_NAME

    def definition(self) -> ToolDefinition:
        """The tool definition, built once so the prompt prefix stays stable."""
        return self._definition

    async def execute(self, arguments: str) -> str:
        """Ask the question in ``arguments`` and return how it ended.

        Returns:
            One JSON object, non-ASCII text kept as written. It always has
            ``status`` and the ``question`` as asked, then ``selected`` (the
            labels chosen) and ``reply`` (what was typed) when answered, a
            ``note`` when declined, or a ``reason`` and a ``note`` when
            nobody answered.

        Raises:
            ToolArgumentError: The payload does not match the schema; the
                question is blank or over :data:`MAX_QUESTION_CHARS`; there
                are options but fewer than :data:`MIN_OPTIONS` or more than
                :data:`MAX_OPTIONS`; a label is blank, over
                :data:`MAX_LABEL_CHARS`, a list of numbers, or the same as
                another apart from case; a description is over
                :data:`MAX_DESCRIPTION_CHARS`; or ``multi_select`` is set
                without options.
            QuestionUnavailable: No question channel is bound. The message
                tells the model not to call the tool again.
        """
        request = _request(arguments)
        try:
            answer = await ask_question(request)
        except QuestionUnavailable as unavailable:
            raise QuestionUnavailable(
                f"{unavailable}; do not call {TOOL_NAME} again, proceed on your "
                "best judgement and state your assumptions"
            ) from None
        return _result(request, answer)


def _request(arguments: str) -> QuestionRequest:
    """The question the raw payload asks, checked against every bound.

    Labels and descriptions are stored without the white space around them;
    the question is stored as written.

    Raises:
        ToolArgumentError: As :meth:`AskUserTool.execute` lists.
    """
    decoded = decode_arguments(arguments)
    issues = validate_arguments(decoded, ASK_USER_SCHEMA)
    if issues:
        listed = "\n".join(f"  - {issue}" for issue in issues)
        raise ToolArgumentError(
            "the arguments do not match this tool's schema:\n"
            f"{listed}\nRe-send the call with all of these corrected."
        )

    question = decoded["question"]
    if not question.strip():
        raise ToolArgumentError("input.question is empty; write the whole question")
    if len(question) > MAX_QUESTION_CHARS:
        raise ToolArgumentError(
            f"input.question is {len(question)} characters and the limit is "
            f"{MAX_QUESTION_CHARS}; ask it in fewer words"
        )

    raw_options = decoded.get("options", [])
    if raw_options and not MIN_OPTIONS <= len(raw_options) <= MAX_OPTIONS:
        raise ToolArgumentError(
            f"input.options has {len(raw_options)} entries; offer "
            f"{MIN_OPTIONS} to {MAX_OPTIONS}, or leave options out for an "
            "open question"
        )
    options = tuple(_option(index, raw) for index, raw in enumerate(raw_options))
    seen: dict[str, int] = {}
    for index, option in enumerate(options):
        folded = option.label.casefold()
        if folded in seen:
            raise ToolArgumentError(
                f"input.options[{index}].label repeats input.options"
                f"[{seen[folded]}].label; every label must be different, "
                "whatever its case"
            )
        seen[folded] = index

    multi_select = decoded.get("multi_select", False)
    if multi_select and not options:
        raise ToolArgumentError(
            "input.multi_select is true and there are no options to choose "
            f"among; add {MIN_OPTIONS} to {MAX_OPTIONS} options or leave "
            "multi_select out"
        )
    return QuestionRequest(
        question=question, options=options, multi_select=multi_select
    )


def _option(index: int, raw: dict[str, Any]) -> QuestionOption:
    """One option of the payload, its label and description checked.

    Raises:
        ToolArgumentError: The label is blank, too long or a list of
            numbers, or the description is too long.
    """
    where = f"input.options[{index}]"
    label = raw["label"].strip()
    if not label:
        raise ToolArgumentError(f"{where}.label is empty")
    if len(label) > MAX_LABEL_CHARS:
        raise ToolArgumentError(
            f"{where}.label is {len(label)} characters and the limit is "
            f"{MAX_LABEL_CHARS}; keep the label short and put the rest in "
            "description"
        )
    if option_numbers(label) is not None:
        raise ToolArgumentError(
            f"{where}.label is {label!r}, which reads as an option number; "
            "the person answers by number, so write the label in words"
        )
    description = raw.get("description", "").strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise ToolArgumentError(
            f"{where}.description is {len(description)} characters and the "
            f"limit is {MAX_DESCRIPTION_CHARS}"
        )
    return QuestionOption(label=label, description=description)


def _result(request: QuestionRequest, answer: QuestionAnswer) -> str:
    """The JSON the model reads for *answer* to *request*."""
    payload: dict[str, Any] = {
        "status": answer.status.value,
        "question": request.question,
    }
    if answer.status is AnswerStatus.ANSWERED:
        payload["selected"] = list(answer.selected)
        payload["reply"] = answer.reply
    elif answer.status is AnswerStatus.DECLINED:
        payload["note"] = DECLINED_NOTE
    else:
        payload["reason"] = answer.reason
        payload["note"] = NO_ANSWER_NOTE
    return json.dumps(payload, ensure_ascii=False)


__all__ = [
    "ASK_USER_SCHEMA",
    "DECLINED_NOTE",
    "DESCRIPTION",
    "MAX_DESCRIPTION_CHARS",
    "MAX_LABEL_CHARS",
    "MAX_OPTIONS",
    "MAX_QUESTION_CHARS",
    "MIN_OPTIONS",
    "NO_ANSWER_NOTE",
    "TOOL_NAME",
    "AskUserTool",
    "option_numbers",
]
