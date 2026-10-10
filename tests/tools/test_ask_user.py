"""``ask_user``: one question to the person, through the question channel.

The tool is tested here with a fake channel and no ``omicsclaw.entry``:
what a question looks like on a screen and how a typed reply becomes an
answer are a surface's business, and ``tests/entry`` covers them. What this
file pins is the tool's own contract: which payloads it refuses, what the
channel is handed, what the model reads back for each ending, and that the
person's thinking time is outside the tool's timeout.

Design choices recorded here because the code only shows their result:

- Every result repeats the question. A summary or a memory extract may keep
  the tool result and drop the assistant message that held the call, and
  ``{"reply": "1"}`` alone answers nothing.
- With no channel the tool is an error that tells the model to stop
  calling it. Returning an empty answer would read as "the person said
  nothing", and the model would proceed as if it had asked.
- A label may not read as a number. The person answers by option number,
  so with labels ``3, 2, 1`` the reply ``1`` would be ambiguous.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import contextmanager
from typing import Any

import pytest

from omicsclaw.schema import ToolCall, ToolResult
from omicsclaw.tools import (
    AnswerStatus,
    ApprovalMode,
    AskUserTool,
    QuestionAnswer,
    QuestionOption,
    QuestionRequest,
    QuestionUnavailable,
    RiskLevel,
    ToolArgumentError,
    ToolPolicy,
    ToolRegistry,
    use_timeout_pause,
    use_tool_context,
)
from omicsclaw.tools.builtin.ask_user import (
    DECLINED_NOTE,
    MAX_DESCRIPTION_CHARS,
    MAX_LABEL_CHARS,
    MAX_QUESTION_CHARS,
    NO_ANSWER_NOTE,
    TOOL_NAME,
    option_numbers,
)

WAIT_S = 5.0

QUESTION = "对照组是哪一组？ Which group is the control?"

TWO = [{"label": "Leiden"}, {"label": "Louvain", "description": "the older method"}]


def _payload(**fields: Any) -> str:
    return json.dumps({"question": QUESTION, **fields})


class _Channel:
    """A question channel that records what it was asked and answers from a script."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.asked: list[QuestionRequest] = []

    async def __call__(self, request: QuestionRequest) -> Any:
        self.asked.append(request)
        return self.answer


def _ask(arguments: str, channel: Any = None) -> str:
    """The tool's own return value, with *channel* bound."""

    async def main() -> str:
        with use_tool_context(question=channel):
            return await AskUserTool().execute(arguments)

    return asyncio.run(asyncio.wait_for(main(), WAIT_S))


def _observe(arguments: str, channel: Any = None) -> ToolResult:
    """What the model is shown: the call run through a registry."""

    async def main() -> ToolResult:
        registry = ToolRegistry([AskUserTool()])
        with use_tool_context(question=channel):
            return await registry.execute(
                ToolCall(id="c1", name=TOOL_NAME, arguments=arguments)
            )

    return asyncio.run(asyncio.wait_for(main(), WAIT_S))


# ---- what the tool refuses ------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "complaint"),
    [
        pytest.param({"question": ""}, "input.question is empty", id="empty question"),
        pytest.param(
            {"question": " \n\t"}, "input.question is empty", id="blank question"
        ),
        pytest.param(
            {"question": "x" * (MAX_QUESTION_CHARS + 1)},
            f"the limit is {MAX_QUESTION_CHARS}",
            id="question too long",
        ),
        pytest.param(
            {"options": [{"label": "only"}]}, "has 1 entries", id="one option"
        ),
        pytest.param(
            {"options": [{"label": f"option {n}"} for n in "abcdefg"]},
            "has 7 entries",
            id="seven options",
        ),
        pytest.param(
            {"options": [{"label": "Leiden"}, {"label": " LEIDEN "}]},
            "repeats input.options[0].label",
            id="labels that differ only in case",
        ),
        pytest.param(
            {"options": [{"label": "3"}, {"label": "four"}]},
            "reads as an option number",
            id="a label that is a number",
        ),
        pytest.param(
            {"options": [{"label": "one"}, {"label": "1, 2"}]},
            "reads as an option number",
            id="a label that is a list of numbers",
        ),
        pytest.param(
            {"options": [{"label": "one"}, {"label": "  "}]},
            "input.options[1].label is empty",
            id="blank label",
        ),
        pytest.param(
            {"options": [{"label": "one"}, {"label": "x" * (MAX_LABEL_CHARS + 1)}]},
            f"the limit is {MAX_LABEL_CHARS}",
            id="label too long",
        ),
        pytest.param(
            {
                "options": [
                    {"label": "one"},
                    {"label": "two", "description": "x" * (MAX_DESCRIPTION_CHARS + 1)},
                ]
            },
            f"limit is {MAX_DESCRIPTION_CHARS}",
            id="description too long",
        ),
        pytest.param(
            {"multi_select": True},
            "no options to choose among",
            id="multi_select without options",
        ),
        pytest.param(
            {"multi_select": True, "options": []},
            "no options to choose among",
            id="multi_select with an empty list",
        ),
        pytest.param({"extra": 1}, "input.extra is not allowed", id="unknown key"),
        pytest.param(
            {"options": [{"label": "a", "value": 1}, {"label": "b"}]},
            "input.options[0].value is not allowed",
            id="unknown option key",
        ),
        pytest.param(
            {"options": "Leiden"}, "must be an array", id="options not a list"
        ),
    ],
)
def test_a_payload_outside_the_bounds_is_refused_before_anybody_is_asked(
    fields, complaint
):
    """The shared validator does not check lengths or item counts, so the
    tool does; and it does so before the channel is touched, because a
    question the model has to re-send must not reach the person first.

    Mutation: delete the option-count check in ``_request`` and the "one
    option" and "seven options" cases reach the channel.
    """
    channel = _Channel(QuestionAnswer(AnswerStatus.ANSWERED, reply="x"))

    with pytest.raises(ToolArgumentError) as refused:
        _ask(_payload(**fields), channel)

    assert complaint in str(refused.value)
    assert channel.asked == []


def test_a_missing_question_and_unreadable_json_are_refused_too():
    with pytest.raises(ToolArgumentError, match="input.question is required"):
        _ask("{}", _Channel(None))
    with pytest.raises(ToolArgumentError, match="not valid JSON"):
        _ask('{"question": "half', _Channel(None))


def test_the_bounds_themselves_are_accepted():
    """2,000 characters, two options and six options are inside the bounds."""
    channel = _Channel(QuestionAnswer(AnswerStatus.DECLINED))

    _ask(json.dumps({"question": "x" * MAX_QUESTION_CHARS}), channel)
    _ask(_payload(options=TWO), channel)
    _ask(_payload(options=[{"label": f"option {n}"} for n in "abcdef"]), channel)
    _ask(
        _payload(
            options=[
                {"label": "x" * MAX_LABEL_CHARS},
                {"label": "b", "description": "y" * MAX_DESCRIPTION_CHARS},
            ]
        ),
        channel,
    )

    assert [len(request.options) for request in channel.asked] == [0, 2, 6, 2]


# ---- what the channel is handed ---------------------------------------------


def test_the_channel_is_handed_the_question_as_written_and_tidy_options():
    channel = _Channel(QuestionAnswer(AnswerStatus.DECLINED))
    options = [
        {"label": "  Leiden "},
        {"label": "Louvain", "description": " the older method\n"},
    ]

    _ask(json.dumps({"question": f"  {QUESTION}\n", "options": options}), channel)
    _ask(_payload(options=TWO, multi_select=True), channel)
    _ask(_payload(options=[]), channel)

    first, second, third = channel.asked
    assert first == QuestionRequest(
        question=f"  {QUESTION}\n",
        options=(
            QuestionOption("Leiden"),
            QuestionOption("Louvain", "the older method"),
        ),
        multi_select=False,
    )
    assert second.multi_select is True
    assert third == QuestionRequest(question=QUESTION)


# ---- what the model reads back ----------------------------------------------


def test_an_answer_comes_back_with_the_question_the_reply_and_what_it_chose():
    answer = QuestionAnswer(AnswerStatus.ANSWERED, reply="1", selected=("Leiden",))

    result = _observe(_payload(options=TWO), _Channel(answer))

    assert result.is_error is False
    assert json.loads(result.output) == {
        "status": "answered",
        "question": QUESTION,
        "selected": ["Leiden"],
        "reply": "1",
    }
    assert "对照组" in result.output, "non-ASCII text is kept as written"


def test_a_declined_question_tells_the_model_not_to_ask_again():
    result = _observe(_payload(), _Channel(QuestionAnswer(AnswerStatus.DECLINED)))

    assert result.is_error is False
    assert json.loads(result.output) == {
        "status": "declined",
        "question": QUESTION,
        "note": DECLINED_NOTE,
    }
    assert "Do not ask this again in this request" in DECLINED_NOTE


def test_an_unanswered_question_carries_the_reason_and_what_to_do_next():
    answer = QuestionAnswer(
        AnswerStatus.NO_ANSWER, reason="no answer before the question deadline"
    )

    result = _observe(_payload(), _Channel(answer))

    assert result.is_error is False
    assert json.loads(result.output) == {
        "status": "no_answer",
        "question": QUESTION,
        "reason": "no answer before the question deadline",
        "note": NO_ANSWER_NOTE,
    }
    assert "restating the question" in NO_ANSWER_NOTE


@pytest.mark.parametrize(
    "answer",
    [
        QuestionAnswer(AnswerStatus.ANSWERED, reply="in my own words"),
        QuestionAnswer(AnswerStatus.DECLINED),
        QuestionAnswer(AnswerStatus.NO_ANSWER, reason="nobody"),
    ],
    ids=lambda answer: answer.status.value,
)
def test_every_ending_repeats_the_question(answer):
    """Mutation: leave ``question`` out of ``_result`` and all three fail."""
    assert json.loads(_ask(_payload(), _Channel(answer)))["question"] == QUESTION


def test_a_channel_that_returns_nothing_is_read_as_no_answer():
    """A channel that forgot to return its answer has not answered.

    Mutation: read ``None`` as ``ANSWERED`` in ``ask_question`` and the
    status below is ``answered`` with an empty reply.
    """
    decoded = json.loads(_ask(_payload(), _Channel(None)))

    assert decoded["status"] == "no_answer"
    assert decoded["reason"] == "the question channel returned no answer"


def test_a_channel_that_returns_something_else_is_a_type_error():
    with pytest.raises(TypeError, match="QuestionAnswer or None; got str"):
        _ask(_payload(), _Channel("Leiden"))

    reported = _observe(_payload(), _Channel(True))
    assert reported.is_error is True
    assert "TypeError" in reported.output


def test_a_synchronous_channel_is_accepted():
    answer = QuestionAnswer(AnswerStatus.ANSWERED, reply="yes")

    assert json.loads(_ask(_payload(), lambda request: answer))["reply"] == "yes"


def test_with_nobody_to_ask_the_model_is_told_to_stop_calling_the_tool():
    """Mutation: return ``ANSWERED("")`` when no channel is bound and this
    is no longer an error, and the model reads an empty reply as an answer."""
    with pytest.raises(QuestionUnavailable):
        _ask(_payload())

    result = _observe(_payload())

    assert result.is_error is True
    assert "nobody can be asked in this session" in result.output
    assert "do not call ask_user again" in result.output


# ---- the wait is the person's, not the tool's ---------------------------------


def test_the_wait_for_the_answer_is_inside_the_timeout_pause():
    """The engine charges a tool for the time between the pause's exit and
    its entry only, so the question has to be put, and waited on, inside it.

    Mutation: call the channel outside ``pause_tool_timeout()`` in
    ``ask_question`` and the channel sees ``paused == []``.
    """
    paused: list[str] = []
    seen_by_channel: list[list[str]] = []

    @contextmanager
    def pause():
        paused.append("in")
        try:
            yield
        finally:
            paused.append("out")

    async def channel(request: QuestionRequest) -> QuestionAnswer:
        await asyncio.sleep(0)
        seen_by_channel.append(list(paused))
        return QuestionAnswer(AnswerStatus.ANSWERED, reply="ok")

    async def main() -> str:
        with use_tool_context(question=channel), use_timeout_pause(pause):
            return await AskUserTool().execute(_payload())

    asyncio.run(asyncio.wait_for(main(), WAIT_S))

    assert seen_by_channel == [["in"]]
    assert paused == ["in", "out"]


# ---- policy and description ------------------------------------------------------


def test_the_policy_lets_it_run_unasked_alone_and_in_read_only_mode():
    """Mutation: ``concurrency_safe=True`` and two questions in one model
    message would be on screen together, beside any approval card."""
    policy = AskUserTool.policy

    assert policy == ToolPolicy(
        risk_level=RiskLevel.LOW,
        approval_mode=ApprovalMode.AUTO,
        read_only=True,
        concurrency_safe=False,
        allowed_in_background=False,
    )
    registry = ToolRegistry([AskUserTool()])
    assert registry.policy_for(TOOL_NAME) == policy
    assert registry.is_concurrency_safe(TOOL_NAME) is False


_POLICY_WORDS = (
    "gate",
    "gated",
    "Gated",
    "GatedTool",
    "permission",
    "permission gate",
    "permission mode",
    "PermissionMode",
    "auto-approve",
    "auto_approve",
    "approval_mode",
    "prompts_for_itself",
)
"""Words that must not reach the model from this tool, matched whole.

The mounted table is checked against the entry layer's own list by
``tests/entry/test_permission_wiring.py``; this one adds a bare
``permission``, because the description tells the model that asking whether
it may run a tool is not what the tool is for, and naming the machinery
there would invite exactly that question."""


def test_the_description_states_the_rules_and_names_no_policy_machinery():
    definition = AskUserTool().definition()
    shown = json.dumps(
        {
            "name": definition.name,
            "description": definition.description,
            "input_schema": definition.input_schema,
        }
    )

    assert definition.name == TOOL_NAME == "ask_user"
    for phrase in ("Look first", "One question per call", "password"):
        assert phrase in definition.description
    assert [
        word for word in _POLICY_WORDS if re.search(rf"\b{re.escape(word)}\b", shown)
    ] == []


def test_the_schema_asks_for_one_question_and_allows_nothing_unnamed():
    schema = AskUserTool().definition().input_schema

    assert schema["required"] == ["question"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"question", "options", "multi_select"}
    option = schema["properties"]["options"]["items"]
    assert option["required"] == ["label"]
    assert option["additionalProperties"] is False


def test_the_definition_is_the_same_object_on_every_call():
    tool = AskUserTool()

    assert tool.definition() is tool.definition()


# ---- the grammar a label is checked against -----------------------------------


@pytest.mark.parametrize(
    ("text", "numbers"),
    [
        ("2", (2,)),
        (" 2 ", (2,)),
        ("1, 3", (1, 3)),
        ("1,3", (1, 3)),
        ("1 3", (1, 3)),
        ("3 , 1 , 3", (3, 1, 3)),
        ("007", (7,)),
        ("0", (0,)),
    ],
)
def test_a_list_of_numbers_is_read_as_option_numbers(text, numbers):
    assert option_numbers(text) == numbers


@pytest.mark.parametrize(
    "text",
    ["", "  ", "one", "1.", "1,", ",1", "1,,2", "1-3", "#1", "1 and 2", "2nd", "-1"],
)
def test_anything_else_is_not_option_numbers(text):
    assert option_numbers(text) is None


def test_a_number_too_long_to_convert_is_still_past_every_option():
    """``int`` refuses a string of more than 4,300 digits, and a reply is
    whatever a person pasted."""
    (number,) = option_numbers("9" * 5000)

    assert number >= 10**9
