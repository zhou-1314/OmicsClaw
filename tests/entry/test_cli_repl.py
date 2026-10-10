"""The loop, driven without a terminal.

Plan 0031 task D3. Every test here builds a **real**
:class:`~omicsclaw.entry.assembly.AgentApp` — real registry, real engine,
real context assembly — over the scripted provider
``test_turn_runner.py`` already defines, and reads what the surface
printed out of a :class:`io.StringIO`. That is only possible because the
input source and the console are both arguments (``cli.go:36``); a REPL
that reached for :data:`sys.stdin` would need a pty here.

There is no ``pytest-asyncio`` on this machine, so every test drives
:func:`asyncio.run` itself, and **every await is inside an
:func:`asyncio.wait_for`**: a defect in a loop like this one shows up as a
hang, and a hang with no timeout plugin installed is a test run that never
finishes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import logging
import pathlib
import types

import pytest
from rich.text import Text

from omicsclaw.engine import RunResult, StopReason
from omicsclaw.entry.cli import PROMPT, Repl, ScriptedSource, Screen, run_once
from omicsclaw.entry.cli import _repl
from omicsclaw.entry.cli._constants import LOGO_LINES
from omicsclaw.entry.cli._slash_command_support import (
    CLI_SLASH_COMMAND_SPECS,
    REPL_SLASH_COMMAND_SPECS,
    slash_token,
)
from omicsclaw.entry.events import TurnEvent
from omicsclaw.entry.session import attach_sessions
from omicsclaw.schema import Message, Role, ToolCall
from omicsclaw.tools import ApprovalRequest
from tests.entry.test_turn_runner import (  # type: ignore[import-not-found]
    CUT_ARGUMENTS,
    Asking,
    Exploding,
    Finishing,
    Reporting,
    Scripted,
    Sleeping,
    calling,
    make_app,
)

WAIT_S = 10.0
"""Every await in this file is bounded by it."""


def build(tmp_path: pathlib.Path, provider, *, tools=(), **overrides):
    """A real app with a session registry, over a scripted backend."""
    return attach_sessions(make_app(tmp_path, provider, tools=tools, **overrides))


def repl_over(app, lines, **kwargs):
    """A REPL reading *lines*, writing into a buffer this returns with it."""
    buffer = io.StringIO()
    source = ScriptedSource(lines)
    return (
        Repl(app, source=source, screen=Screen.into(buffer), **kwargs),
        source,
        buffer,
    )


def answering(text: str) -> Scripted:
    return Scripted(Message(role=Role.ASSISTANT, content=text))


# ---- the loop ---------------------------------------------------------


def test_one_question_is_answered_and_the_loop_asks_again(tmp_path):
    """The whole surface in one assertion, plus the prompt coming back."""

    async def drive():
        app = build(tmp_path, answering("spatial autocorrelation is Moran's I"))
        repl, source, buffer = repl_over(app, ["what is Moran's I?", "/exit"])
        await asyncio.wait_for(repl.run(), WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return source, buffer.getvalue()

    source, printed = asyncio.run(drive())

    assert "spatial autocorrelation is Moran's I" in printed
    assert source.prompts == [PROMPT, PROMPT]


def test_end_of_input_ends_the_loop(tmp_path):
    """EOF and ``/exit`` are the same exit (``cli.go:36-79``).

    Without this, ``oc cli < questions.txt`` would
    hang on the last line forever rather than finishing.
    """

    async def drive():
        app = build(tmp_path, answering("done"))
        repl, _source, buffer = repl_over(app, ["one question"])
        await asyncio.wait_for(repl.run(), WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return buffer.getvalue()

    assert "done" in asyncio.run(drive())


def test_a_blank_line_is_not_a_question(tmp_path):
    """It costs a model call and answers nothing."""

    async def drive():
        provider = answering("should not be reached")
        app = build(tmp_path, provider)
        repl, _source, _buffer = repl_over(app, ["", "   ", "/exit"])
        await asyncio.wait_for(repl.run(), WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return provider.calls

    assert asyncio.run(drive()) == 0


def test_help_lists_what_this_build_runs_and_a_blocked_command_says_so(tmp_path):
    """Plan 0031 §5.1's boundary, as a user meets it.

    ``/research`` is in the ported catalogue and is not implemented here.
    Sending it to the model as though it were a question — which is what a
    loop that only knew the implemented names would do — would produce a
    confident answer about a command that does not run.
    """

    async def drive():
        provider = answering("unused")
        app = build(tmp_path, provider)
        repl, _source, buffer = repl_over(app, ["/help", "/research idea", "/exit"])
        await asyncio.wait_for(repl.run(), WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return provider.calls, buffer.getvalue()

    calls, printed = asyncio.run(drive())

    assert calls == 0
    assert "/research is not available in this build." in printed
    for spec in REPL_SLASH_COMMAND_SPECS:
        assert spec.name in printed
    assert "/research  Research pipeline" not in printed


@pytest.mark.parametrize("command", ["/run demo", "/doctor", "/context", "/memory"])
def test_the_commands_plan_0037_called_half_done_are_refused(tmp_path, command):
    """Listed in the catalogue is not the same as implemented.

    Plan 0037 §5.3 argues that moving the old top-level subcommands
    in-surface is "already half fixed today", and cites
    ``_slash_command_support.py`` as already listing ``/run`` and
    ``/doctor``. It lists them; this build does not run them —— nine of
    the thirty-eight specs are implemented. The refusal path is what
    keeps that honest for a user, and it is the most likely thing in
    this surface to break silently: nothing else would notice if
    ``/run`` started being forwarded to the model as a sentence, and the
    model would answer it.

    The generic case is
    :func:`test_help_lists_what_this_build_runs_and_a_blocked_command_says_so`;
    these four are named because they are the ones a reader of plan 0037
    and ``AGENTS.md`` will type first.
    """

    async def drive():
        provider = answering("unused")
        app = build(tmp_path, provider)
        repl, _source, buffer = repl_over(app, [command, "/exit"])
        await asyncio.wait_for(repl.run(), WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return provider.calls, buffer.getvalue()

    calls, printed = asyncio.run(drive())
    name = command.split(" ")[0]

    assert calls == 0, f"{name} was sent to the model as a question"
    assert f"{name} is not available in this build." in printed


def test_the_skills_command_reports_the_index_the_prompt_advertised(tmp_path):
    """One scan, one answer.

    ``app.skills`` is what the system prompt listed and what ``use_skill``
    can load; a second scan here could disagree with both.
    """
    skills = tmp_path / "skills" / "spatial-preprocess"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "---\nname: spatial-preprocess\ndescription: QC a Visium slide\n---\nbody",
        encoding="utf-8",
    )

    async def drive():
        app = build(tmp_path, answering("unused"))
        repl, _source, buffer = repl_over(app, ["/skills", "/exit"])
        await asyncio.wait_for(repl.run(), WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return buffer.getvalue()

    assert "spatial-preprocess" in asyncio.run(drive())


# ---- a slash that names no command --------------------------------------


def write_skills(tmp_path: pathlib.Path) -> None:
    """Two skills under a domain, as the real corpus is laid out."""
    for name, description, body in (
        ("spatial-de", "rank spatial markers", "Use Wilcoxon, then filter."),
        ("spatial-domains", "find tissue domains", "Build the graph first."),
    ):
        directory = tmp_path / "skills" / "spatial" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n"
            f"trigger: niche, marker gene\n---\n\n{body}\n",
            encoding="utf-8",
        )


def drive_lines(tmp_path: pathlib.Path, provider, lines) -> str:
    """Run a REPL over *lines* against a real app and return what it printed."""

    async def drive():
        app = build(tmp_path, provider)
        repl, _source, buffer = repl_over(app, lines)
        await asyncio.wait_for(repl.run(), WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return buffer.getvalue()

    return asyncio.run(drive())


@pytest.mark.parametrize(
    "line",
    ["/spatial-de compare tumour and stroma", "/spatial-de", "/SPATIAL-DE"],
)
def test_a_skill_name_is_not_a_command_and_the_reply_says_how_to_ask(
    tmp_path, line
):
    """Skills are picked by the agent; a slash no longer runs one.

    Neither the skill's body nor the line reaches the model, and the
    person is told to describe the task instead of being left to guess
    why a name that used to work does nothing.
    """
    write_skills(tmp_path)
    provider = answering("unused")

    printed = drive_lines(tmp_path, provider, [line, "/exit"])

    assert provider.calls == 0
    token = line.split()[0]
    assert f"No command named {token}." in printed
    assert "describe the task" in printed
    assert "Use Wilcoxon" not in printed


def test_a_command_name_is_not_taken_by_a_skill(tmp_path):
    """A skill called ``help`` changes nothing about ``/help``."""
    directory = tmp_path / "skills" / "help"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: help\ndescription: not the menu\n---\n\nSkill body.\n",
        encoding="utf-8",
    )
    provider = answering("unused")

    printed = drive_lines(tmp_path, provider, ["/help", "/exit"])

    assert provider.calls == 0, "/help was answered by the skill, not the menu"
    assert "Skill body." not in printed
    assert "/skills" in printed


def test_an_unknown_name_is_reported_instead_of_asked(tmp_path):
    """Sending a typo to the model buys a prose answer and no correction."""
    write_skills(tmp_path)
    provider = answering("no such thing")

    printed = drive_lines(tmp_path, provider, ["/spatial-dx", "/exit"])

    assert provider.calls == 0, "an unknown /name was sent to the model"
    assert "No command named /spatial-dx." in printed
    assert "/help lists the commands." in printed
    assert "describe the task" not in printed, "spatial-dx is not a skill"


def test_a_pasted_path_is_a_question_and_not_a_mistyped_name(tmp_path):
    """An omics workspace's lines start with ``/`` for a second reason."""
    write_skills(tmp_path)
    provider = answering("that is an AnnData file")

    printed = drive_lines(tmp_path, provider, ["/data/run7/matrix.h5ad", "/exit"])

    assert provider.calls == 1
    assert provider.seen[0][-1].content == "/data/run7/matrix.h5ad"
    assert "No command named" not in printed


def test_a_catalogue_command_this_build_lacks_keeps_its_own_answer(tmp_path):
    """``/research`` is known and unimplemented, not unknown."""
    write_skills(tmp_path)
    provider = answering("unused")

    printed = drive_lines(tmp_path, provider, ["/research", "/exit"])

    assert provider.calls == 0
    assert "/research is not available in this build." in printed
    assert "No command named" not in printed


@pytest.mark.parametrize(
    "line, expected",
    [
        ("/spatial-de", "spatial-de"),
        ("/spatial-de rank the markers", "spatial-de"),
        ("   /spatial-de   ", "spatial-de"),
        ("/not-a-command", "not-a-command"),
        ("/UPPER", "UPPER"),
    ],
)
def test_a_slash_line_names_its_first_token(line, expected):
    """Parsing only: the name need not be one anything claims."""
    assert slash_token(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "spatial-de",
        "",
        "   ",
        "/",
        "/ spatial-de",
        "//spatial-de",
        "/data/run7/matrix.h5ad",
        "/home/user/counts.csv what is this?",
        "/tmp\\windows\\path",
    ],
)
def test_a_line_that_names_nothing_has_no_token(line):
    """A pasted path is a question, not a name somebody got wrong."""
    assert slash_token(line) is None


def test_the_skills_command_finds_a_skill_by_its_trigger(tmp_path):
    """``/skills`` searches the triggers, which no name or description holds."""
    write_skills(tmp_path)

    printed = drive_lines(tmp_path, answering("unused"), ["/skills niche", "/exit"])

    assert "  spatial-de" in printed
    assert "spatial" in printed
    assert "/spatial-de" not in printed, "a skill is not offered as a command"
    assert "describe the task" in printed


# ---- Ctrl-C -----------------------------------------------------------


def test_an_interrupt_cancels_the_exchange_and_the_prompt_comes_back(tmp_path):
    """Plan 0031's ``Ctrl-C`` contract, all three halves of it.

    The exchange ends ``cancelled``, the loop keeps going, and — trap 3 —
    the conversation is **byte-identical** to what it was before the
    cancelled exchange started. The second question is asked and answered
    afterwards, so the history that is compared is the one a surviving
    loop produced rather than an empty one.
    """

    async def drive():
        sleeping = Sleeping()
        app = build(
            tmp_path,
            Scripted(calling("sleep"), Message(role=Role.ASSISTANT, content="second")),
            tools=(sleeping,),
        )
        repl, source, buffer = repl_over(app, ["hang please", "and now?", "/exit"])
        loop = asyncio.create_task(repl.run())
        await asyncio.wait_for(sleeping.entered.wait(), WAIT_S)

        assert repl.interrupt() is True

        await asyncio.wait_for(loop, WAIT_S)
        session = app.sessions.session(repl.state.session_id)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return session.history, source.prompts, buffer.getvalue()

    history, prompts, printed = asyncio.run(drive())

    assert "Cancelled." in printed
    assert prompts == [PROMPT, PROMPT, PROMPT]
    # Trap 3: the cancelled exchange left nothing behind. Only the second
    # question and its answer are in the conversation.
    assert [message.content for message in history] == ["and now?", "second"]


def test_an_interrupt_with_nothing_running_reports_that(tmp_path):
    """The entry point needs the answer to decide what ``Ctrl-C`` means.

    At an idle prompt there is no exchange to cancel, and a handler that
    could not tell would either kill a running analysis or refuse to quit.
    """

    async def drive():
        app = build(tmp_path, answering("unused"))
        repl, _source, _buffer = repl_over(app, ["/exit"])
        fired = repl.interrupt()
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return fired

    assert asyncio.run(drive()) is False


# ---- approvals --------------------------------------------------------


class Deliberating(ScriptedSource):
    """A person who will not answer the first card until they see the second.

    **The whole of trap 1 is in this class.** A source that answers
    instantly cannot tell a correct loop from a deadlocked one: awaiting
    the human inside the ``async for`` body works perfectly as long as the
    human is a list. The reference harness states the rule as a consumer
    contract — the UI "must keep consuming events while it shows the
    dialog" (``stream.go:51-58``) — and the only way to test a contract
    about *continuing to consume* is a consumer that is asked to stop.

    So the first approval read blocks until a second one has been
    requested. A loop that stopped iterating to ask cannot deliver the
    second request, the first read never returns, and the test fails on
    its :func:`asyncio.wait_for` instead of hanging.
    """

    def __init__(self, lines, *, verdicts: dict[str, str]):
        super().__init__(lines)
        self._verdicts = verdicts
        self._second_card = asyncio.Event()

    async def read(self, prompt: str) -> str:
        if not prompt.startswith("approve"):
            return await super().read(prompt)
        self.prompts.append(prompt)
        asked = [one for one in self.prompts if one.startswith("approve")]
        if len(asked) >= 2:
            self._second_card.set()
        else:
            await self._second_card.wait()
        # Answered by which tool is named rather than by arrival order:
        # the gate makes the *second* card the first one answered, and a
        # test whose expected output depends on that is a test about
        # scheduling.
        for name, verdict in self._verdicts.items():
            if name in prompt:
                return verdict
        return ""


def test_two_concurrent_approvals_are_both_answered(tmp_path):
    """Trap 1, at this surface.

    Two tools in one model message means two approval requests in flight.
    A loop that awaited the human **inside** its ``async for`` would never
    deliver the second request, and the exchange would sit there until the
    deadline — which for a CLI is ``None``, so: forever.
    """

    async def drive():
        app = build(
            tmp_path,
            Scripted(
                calling("ask_a", "ask_b"),
                Message(role=Role.ASSISTANT, content="both settled"),
            ),
            tools=(Asking("ask_a"), Asking("ask_b")),
        )
        buffer = io.StringIO()
        source = Deliberating(
            ["do two things", "/exit"],
            verdicts={"ask_a": "y", "ask_b": "no thanks"},
        )
        repl = Repl(app, source=source, screen=Screen.into(buffer))
        await asyncio.wait_for(repl.run(), WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return source.prompts, buffer.getvalue()

    prompts, printed = asyncio.run(drive())

    assert "approve ask_a [#1]? [y/N/a=always] " in prompts
    assert "approve ask_b [#2]? [y/N/a=always] " in prompts
    assert "both settled" in printed
    assert "<- ask_a ok" in printed
    assert "<- ask_b error" in printed


def test_a_sub_agent_s_approval_names_the_sub_agent_on_the_card_and_at_the_prompt(
    tmp_path,
):
    """The person saw the parent hand a task over and nothing of what the
    sub-agent did next, so a bare ``approve ask`` would read as the parent
    asking. Card and prompt both say whose call it is; the parent's own
    prompt, pinned by the test above, stays as it was.

    Mutation: format the prompt with the tool name alone in ``Repl._ask``
    and the prompt assertion fails.
    """
    delegating = Message(
        role=Role.ASSISTANT,
        tool_calls=(
            ToolCall(
                id="d1",
                name="task",
                arguments=json.dumps(
                    {"subagent_type": "general-purpose", "prompt": "do the thing"}
                ),
            ),
        ),
    )

    async def drive():
        app = build(
            tmp_path,
            Scripted(
                delegating,
                calling("ask"),
                Message(role=Role.ASSISTANT, content="the sub-agent finished"),
                Message(role=Role.ASSISTANT, content="handed back"),
            ),
            tools=(Asking("ask"),),
        )
        repl, source, buffer = repl_over(app, ["delegate it", "y", "/exit"])
        await asyncio.wait_for(repl.run(), WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return source.prompts, buffer.getvalue()

    prompts, printed = asyncio.run(drive())

    assert prompts == [
        PROMPT,
        "approve ask for sub-agent general-purpose [#1]? [y/N/a=always] ",
        PROMPT,
    ]
    assert "]: ask for sub-agent general-purpose (risk high)" in printed
    assert "handed back" in printed


def test_a_sub_agent_s_name_reaches_the_prompt_as_inert_text(tmp_path):
    """The prompt is written by the input source and not by the screen, so
    nothing downstream makes it safe. A sub-agent's name comes from a
    definition file, and one holding an escape sequence or a line break
    could clear the card above it or draw a second one.

    Mutation: put ``event.subagent`` into the prompt of ``Repl._ask``
    without ``inert_line`` and the escape and the line break reach it.
    """

    class Handle:
        def __init__(self) -> None:
            self.verdicts: list[tuple[str, bool]] = []

        async def approve(self, request_id, decision) -> None:
            self.verdicts.append((request_id, decision.approved))

    event = TurnEvent.approval_required(
        ApprovalRequest(tool_name="bash"),
        "t#1",
        subagent="helper\x1b[2J\nApproval required [t#2]: ls",
    )

    async def drive():
        app = build(tmp_path, answering("unused"))
        repl, source, _buffer = repl_over(app, ["n"])
        handle = Handle()
        await asyncio.wait_for(repl._ask(handle, "t#1", event), WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return source.prompts, handle.verdicts

    prompts, verdicts = asyncio.run(drive())

    assert prompts == [
        "approve bash for sub-agent helper\\u001b[2J ↵ Approval required "
        "[t#2]: ls [#1]? [y/N/a=always] "
    ]
    assert verdicts == [("t#1", False)]


def test_always_allow_writes_a_rule_and_stops_asking(tmp_path):
    """"Always allow" has to reach the rule file, or it is a button that lies.

    The gate has had :meth:`~omicsclaw.permission.PermissionGate.remember`
    since it was written, and nothing called it — so the capability existed
    and no person could reach it. The reference harness's equivalent is a
    third option on its approval dialog
    (``cmd/harness9/tui_update.go:1748``), and this is that option.

    Two exchanges in one run is what makes it a test of the *effect* rather
    than of the write: the first asks and answers "a", the second must not
    ask at all, which is only true if the rule reached the file **and** the
    store re-read it.
    """

    async def drive():
        app = build(
            tmp_path,
            Scripted(
                calling("ask"),
                Message(role=Role.ASSISTANT, content="first done"),
                calling("ask"),
                Message(role=Role.ASSISTANT, content="second done"),
            ),
            tools=(Asking("ask"),),
        )
        repl, source, buffer = repl_over(
            app, ["once", "a", "twice", "/exit"]
        )
        await asyncio.wait_for(repl.run(), WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return source.prompts, buffer.getvalue()

    prompts, printed = asyncio.run(drive())

    approvals = [p for p in prompts if p.startswith("approve ask [")]
    assert len(approvals) == 1, f"asked {len(approvals)} times: {prompts}"
    assert "Remembered: always allow ask({})" in printed
    assert "first done" in printed
    assert "second done" in printed

    written = tmp_path / ".omicsclaw" / "settings.json"
    assert "ask({})" in written.read_text(encoding="utf-8")


def test_always_allow_says_so_when_it_cannot_remember(tmp_path):
    """A person told nothing would be asked again and stop trusting the prompt."""

    async def drive():
        app = build(
            tmp_path,
            Scripted(
                calling("ask"), Message(role=Role.ASSISTANT, content="done")
            ),
            tools=(Asking("ask"),),
        )
        app = dataclasses.replace(app, permission=None)
        repl, _source, buffer = repl_over(app, ["once", "always", "/exit"])
        await asyncio.wait_for(repl.run(), WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return buffer.getvalue()

    printed = asyncio.run(drive())

    assert "nowhere to remember that" in printed
    assert "done" in printed, "the approval itself still went through"
    assert not (tmp_path / ".omicsclaw" / "settings.json").exists()


def test_an_empty_answer_denies(tmp_path):
    """Fail closed (plan 0031 Q12).

    Somebody who pressed enter to get their prompt back has not consented
    to anything, and a surface that read that as yes would be the reason a
    ``bash`` call nobody approved ran.
    """

    async def drive():
        app = build(
            tmp_path,
            Scripted(calling("ask_a"), Message(role=Role.ASSISTANT, content="ok")),
            tools=(Asking("ask_a"),),
        )
        repl, _source, buffer = repl_over(app, ["do it", "", "/exit"])
        await asyncio.wait_for(repl.run(), WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return buffer.getvalue()

    printed = asyncio.run(drive())

    assert "Approval denied" in printed
    assert "<- ask_a error" in printed


def test_nobody_at_the_terminal_denies_rather_than_waiting(tmp_path):
    """``run_once`` with no source: a deadline of ``None`` and no human.

    The combination a cron job reaches. Waiting forever is the wrong
    answer and so is approving, so it denies — the same verdict an expired
    deadline gives.
    """

    async def drive():
        app = build(
            tmp_path,
            Scripted(calling("ask_a"), Message(role=Role.ASSISTANT, content="ok")),
            tools=(Asking("ask_a"),),
        )
        buffer = io.StringIO()
        handle = await asyncio.wait_for(
            run_once(app, "do it", screen=Screen.into(buffer)), WAIT_S
        )
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return handle, buffer.getvalue()

    handle, printed = asyncio.run(drive())

    assert handle is not None and handle.terminal == "converged"
    assert "Approval denied" in printed


class Refusing(ScriptedSource):
    """A terminal that cannot put an approval card, the way a real one can't.

    ``prompt_toolkit`` raises ``AssertionError: Application is already
    running.`` when a second prompt overlaps the first, and that is what
    :class:`~omicsclaw.entry.cli._input.PromptToolkitSource` used to let
    through. The exception type is copied rather than invented because
    the escape it took — out of :meth:`Repl._ask`'s two-exception
    ``except`` — is the whole defect.
    """

    async def read(self, prompt: str) -> str:
        if prompt.startswith("approve"):
            self.prompts.append(prompt)
            raise AssertionError("Application is already running.")
        return await super().read(prompt)


def test_an_approval_that_cannot_be_asked_denies_rather_than_hanging(tmp_path):
    """A question that could not be put is still answered.

    :attr:`~omicsclaw.entry.config.AppConfig.approval_timeout_s` is
    ``None`` at this surface, so a request nobody settles is not a slow
    exchange, it is one that never ends — the REPL stops printing, stops
    reading, and the reason sits in a log sink the process will not
    release until it exits. Denying is the only verdict that is both safe
    and terminal.
    """

    async def drive():
        app = build(
            tmp_path,
            Scripted(calling("ask_a"), Message(role=Role.ASSISTANT, content="ok")),
            tools=(Asking("ask_a"),),
        )
        buffer = io.StringIO()
        source = Refusing(["do it", "/exit"])
        repl = Repl(app, source=source, screen=Screen.into(buffer))
        await asyncio.wait_for(repl.run(), WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return buffer.getvalue()

    printed = asyncio.run(drive())

    assert "Could not ask about ask_a" in printed, "the person is told why"
    assert "<- ask_a error" in printed, "the tool was refused, not left waiting"
    assert "ok" in printed, "the exchange ran to its end"


def test_a_failed_approval_task_is_logged_and_not_merely_dropped(
    tmp_path, caplog, monkeypatch
):
    """The last resort behind :meth:`Repl._ask`'s own fail-closed path.

    ``_ask`` answers on every path it can reach, so a Task that arrives
    here with an exception failed somewhere it could not — settling the
    request, say. Retrieving the exception is what keeps the event loop
    from reporting it at garbage-collection time instead, which at this
    surface means into a log sink the REPL holds until the process exits.
    """

    async def boom(self, handle, request_id, event, **_how) -> None:
        raise RuntimeError("could not settle it")

    async def drive():
        app = build(tmp_path, answering("unused"))
        repl, _source, _buffer = repl_over(app, ["/exit"])
        repl._ask_human(None, types.SimpleNamespace(request_id="r1"))
        started = tuple(repl._asking)
        await asyncio.wait_for(
            asyncio.gather(*started, return_exceptions=True), WAIT_S
        )
        await asyncio.sleep(0)  # done callbacks run on the next iteration
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return repl

    monkeypatch.setattr(Repl, "_ask", boom)
    with caplog.at_level(logging.ERROR, logger="omicsclaw.entry.cli._repl"):
        repl = asyncio.run(drive())

    assert not repl._asking, "the finished task was dropped from the set"
    assert "could not settle it" in caplog.text


def test_an_approval_nobody_answered_does_not_outlive_its_exchange(tmp_path):
    """The Task asking a human is reaped when the exchange is cancelled.

    Otherwise the next thing the user types answers a question about a
    tool call that was abandoned minutes ago, and an un-awaited cancelled
    Task prints "Task exception was never retrieved" into the terminal
    they are still reading.
    """

    async def drive():
        app = build(
            tmp_path,
            Scripted(calling("ask_a"), Message(role=Role.ASSISTANT, content="ok")),
            tools=(Asking("ask_a"),),
        )
        repl, source, buffer = repl_over(app, ["do it"])
        loop = asyncio.create_task(repl.run())
        for _ in range(200):
            await asyncio.sleep(0)
            if "approve ask_a [#1]? [y/N/a=always] " in source.prompts:
                break
        assert repl.interrupt() is True
        await asyncio.wait_for(loop, WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return repl, buffer.getvalue()

    repl, printed = asyncio.run(drive())

    assert "Cancelled." in printed
    assert not repl._asking


class LeavesTheCardOpen(ScriptedSource):
    """A person who never answers an approval card, and when its prompt
    was closed."""

    def __init__(self, lines, buffer) -> None:
        super().__init__(lines)
        self._buffer = buffer
        self.on_screen_when_closed: str | None = None

    async def read(self, prompt: str) -> str:
        if not prompt.startswith("approve"):
            return await super().read(prompt)
        self.prompts.append(prompt)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.on_screen_when_closed = self._buffer.getvalue()
            raise


def test_an_approval_card_s_prompt_stays_open_past_its_deadline(tmp_path):
    """What happens today, held so that it changes only on purpose. A
    question's prompt is taken down when its deadline passes; an approval
    card's is not, and stays until the exchange ends. Whether it should
    come down too is a separate decision.

    Mutation: record the approval's Task in ``Repl._replying`` and retract
    on ``APPROVAL_SETTLED`` as well, and the prompt is closed as soon as
    the denial is printed.
    """

    async def drive():
        sleeping = Sleeping()
        app = build(
            tmp_path,
            Scripted(calling("ask_a", "sleep")),
            tools=(Asking("ask_a"), sleeping),
            approval_timeout_s=0.2,
        )
        buffer = io.StringIO()
        source = LeavesTheCardOpen(["do it"], buffer)
        repl = Repl(app, source=source, screen=Screen.into(buffer))
        loop = asyncio.create_task(repl.run())
        for _ in range(int(WAIT_S / 0.005)):
            if "Approval denied [" in buffer.getvalue():
                break
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.05)  # room for a retraction, were there one
        denied = buffer.getvalue()
        still_reading = [task for task in repl._asking if not task.done()]
        closed_before_the_end = source.on_screen_when_closed
        assert repl.interrupt() is True
        await asyncio.wait_for(loop, WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return denied, still_reading, closed_before_the_end, source

    denied, still_reading, closed_before_the_end, source = asyncio.run(drive())

    assert "no answer before the approval deadline" in denied
    assert len(still_reading) == 1, "the card's prompt was taken down"
    assert closed_before_the_end is None
    assert source.on_screen_when_closed is not None, "the exchange's end closes it"
    assert source.prompts.count("approve ask_a [#1]? [y/N/a=always] ") == 1


class InterruptedAtTheCard(ScriptedSource):
    """A person who presses Ctrl-C at every approval card.

    ``prompt_toolkit`` reads with the terminal in raw mode, where Ctrl-C
    is a key rather than a signal: ``prompt_async`` raises
    :exc:`KeyboardInterrupt` in whichever Task is reading, and the SIGINT
    handler the entry point installs never runs. Raising it from
    :meth:`read` is that key press, without a terminal.
    """

    async def read(self, prompt: str) -> str:
        if prompt.startswith("approve"):
            await asyncio.sleep(0)
            self.prompts.append(prompt)
            raise KeyboardInterrupt
        return await super().read(prompt)


class Recording(Repl):
    """A REPL that keeps the handle of every exchange it ran."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.handles = []

    async def ask(self, text):
        handle = await super().ask(text)
        self.handles.append(handle)
        return handle


def test_ctrl_c_at_an_approval_card_cancels_the_exchange_not_the_repl(tmp_path):
    """Ctrl-C at a card does what Ctrl-C while the exchange works does.

    The key press and the signal are two paths. Every other Ctrl-C test
    calls :meth:`Repl.interrupt`, which is where the signal handler lands,
    and they stayed green while the key press — a
    :exc:`KeyboardInterrupt` raised inside the Task answering the card —
    went past every ``except`` there, out of :func:`asyncio.run`, and
    ended ``oc cli`` with exit code 130 in the middle of a conversation.

    Three things are held here: the exchange ends ``cancelled``; the card
    is settled as a denial before that, so the tool is refused on the
    record rather than merely torn down with the exchange; and the loop
    goes back to the prompt and answers the next line. The history check
    is the cancellation contract every other Ctrl-C path keeps: the
    cancelled exchange leaves nothing behind.
    """

    async def drive():
        app = build(
            tmp_path,
            Scripted(calling("ask_a"), Message(role=Role.ASSISTANT, content="second")),
            tools=(Asking("ask_a"),),
        )
        buffer = io.StringIO()
        source = InterruptedAtTheCard(["do it", "and now?", "/exit"])
        repl = Recording(app, source=source, screen=Screen.into(buffer))
        await asyncio.wait_for(repl.run(), WAIT_S)
        session = app.sessions.session(repl.state.session_id)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return repl, source.prompts, session.history, buffer.getvalue()

    try:
        repl, prompts, history, printed = asyncio.run(drive())
    except KeyboardInterrupt:
        pytest.fail("Ctrl-C at the approval card escaped the REPL")

    interrupted, answered = repl.handles
    assert interrupted.terminal == "cancelled"
    assert "Approval denied [" in printed
    assert "interrupted at the terminal" in printed
    assert "Cancelled." in printed
    assert answered.terminal == "converged"
    assert prompts == [PROMPT, "approve ask_a [#1]? [y/N/a=always] ", PROMPT, PROMPT]
    assert [message.content for message in history] == ["and now?", "second"]
    assert not repl._asking


class OneCard(ScriptedSource):
    """A source whose card read raises *outcome*, or never returns."""

    def __init__(self, outcome: BaseException | None) -> None:
        super().__init__(())
        self._outcome = outcome

    async def read(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self._outcome is None:
            await asyncio.Event().wait()
        raise self._outcome


class Interrupts(Repl):
    """A REPL that counts the times it cancelled the running exchange."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.interrupts = 0

    def interrupt(self) -> bool:
        self.interrupts += 1
        return super().interrupt()


def _card_reader(tmp_path, source):
    """A REPL over *source*, a record of how its card was settled, and the
    buffer it printed into."""
    buffer = io.StringIO()
    app = build(tmp_path, answering("unused"))
    repl = Interrupts(app, source=source, screen=Screen.into(buffer))
    settled: list[str] = []

    async def refuse(reason: str) -> None:
        settled.append(reason)

    return app, repl, settled, refuse, buffer


@pytest.mark.parametrize(
    ("raised", "reason", "interrupts"),
    [
        (KeyboardInterrupt(), "interrupted at the terminal", 1),
        (EOFError(), "no operator at the terminal", 0),
        (RuntimeError("no tty"), "the terminal could not ask: no tty", 0),
    ],
    ids=["ctrl-c", "eof", "failure"],
)
def test_a_card_read_without_an_answer_is_settled_exactly_once(
    tmp_path, raised, reason, interrupts
):
    """Every way of not getting an answer settles the card, in one place.

    A terminal deployment sets no deadline on a card, so a path that
    returns without settling it is an exchange that never ends. The paths
    used to be split between the card reader (Ctrl-C) and the approval
    code (the input ending, the Task being cancelled, the source failing),
    so a second kind of card would have had to repeat the latter three and
    could forget one. Held at the reader itself, where a new card inherits
    it: each path settles once, with its own reason; only Ctrl-C cancels
    the exchange; a failure is shown as well as logged.
    """

    async def drive():
        app, repl, settled, refuse, buffer = _card_reader(tmp_path, OneCard(raised))
        answer = await asyncio.wait_for(
            repl._read_card(
                "card> ", subject="the thing", settled_as="Denied", refuse=refuse
            ),
            WAIT_S,
        )
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return answer, settled, repl.interrupts, buffer.getvalue()

    try:
        answer, settled, counted, printed = asyncio.run(drive())
    except KeyboardInterrupt:
        pytest.fail("Ctrl-C at the card escaped the card reader")

    assert answer is None
    assert settled == [reason]
    assert counted == interrupts
    if isinstance(raised, RuntimeError):
        assert "Could not ask about the thing: no tty. Denied." in printed


def test_a_cancelled_card_read_is_settled_and_not_left_open(tmp_path):
    """The Task reading a card is cancelled when its exchange ends first.

    The card is still settled — as nobody at the terminal — and the
    cancellation stops there: the reader returns rather than raising, so
    the Task answering the card finishes instead of failing.
    """

    async def drive():
        source = OneCard(None)
        app, repl, settled, refuse, _buffer = _card_reader(tmp_path, source)
        reading = asyncio.create_task(
            repl._read_card(
                "card> ", subject="the thing", settled_as="Denied", refuse=refuse
            )
        )
        while not source.prompts:
            await asyncio.sleep(0)
        reading.cancel()
        await asyncio.wait({reading}, timeout=WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return reading, settled, repl.interrupts

    reading, settled, interrupts = asyncio.run(drive())

    assert not reading.cancelled()
    assert reading.result() is None
    assert settled == ["no operator at the terminal"]
    assert interrupts == 0


def test_an_answered_card_is_not_settled_by_the_reader(tmp_path):
    """The line goes back to the caller, which is the one to act on it."""

    async def drive():
        app, repl, settled, refuse, _buffer = _card_reader(
            tmp_path, ScriptedSource(["y"])
        )
        answer = await asyncio.wait_for(
            repl._read_card(
                "card> ", subject="the thing", settled_as="Denied", refuse=refuse
            ),
            WAIT_S,
        )
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return answer, settled

    assert asyncio.run(drive()) == ("y", [])


def test_ctrl_c_at_a_card_cancels_the_exchange_even_if_settling_fails(tmp_path):
    """The exchange is cancelled whatever settling the card did.

    A Ctrl-C that left the exchange running because the settlement raised
    would be a key press that did nothing visible.
    """

    async def drive():
        app, repl, _settled, _refuse, _buffer = _card_reader(
            tmp_path, OneCard(KeyboardInterrupt())
        )

        async def refuse(reason: str) -> None:
            raise RuntimeError("could not settle it")

        with pytest.raises(RuntimeError, match="could not settle it"):
            await asyncio.wait_for(
                repl._read_card(
                    "card> ", subject="the thing", settled_as="Denied", refuse=refuse
                ),
                WAIT_S,
            )
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return repl.interrupts

    assert asyncio.run(drive()) == 1


def test_an_approval_card_that_meets_the_end_of_input_is_denied_as_unattended(
    tmp_path,
):
    """The approval card's end-of-input verdict, reason and all.

    ``run_once`` reads from a source with no lines, so the card meets the
    end of input at once. The reason is what the model is told about the
    refusal, and it has to say nobody was there rather than that somebody
    said no.
    """

    async def drive():
        app = build(
            tmp_path,
            Scripted(calling("ask_a"), Message(role=Role.ASSISTANT, content="ok")),
            tools=(Asking("ask_a"),),
        )
        buffer = io.StringIO()
        handle = await asyncio.wait_for(
            run_once(app, "do it", screen=Screen.into(buffer)), WAIT_S
        )
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return handle, buffer.getvalue()

    handle, printed = asyncio.run(drive())

    assert handle is not None and handle.terminal == "converged"
    assert "no operator at the terminal" in printed


# ---- session commands -------------------------------------------------


def test_new_starts_a_conversation_with_no_history(tmp_path):
    """``/new`` abandons the session rather than editing one.

    The registry's session object is what the lane pump saves into;
    reaching in to empty its history is how a surface races an exchange it
    forgot was running.
    """

    async def drive():
        app = build(tmp_path, answering("hello"))
        repl, _source, _buffer = repl_over(app, ["first", "/new", "second", "/exit"])
        first = repl.state.session_id
        await asyncio.wait_for(repl.run(), WAIT_S)
        second = repl.state.session_id
        histories = (
            app.sessions.session(first).history,
            app.sessions.session(second).history,
        )
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return first, second, histories

    first, second, (before, after) = asyncio.run(drive())

    assert first != second
    assert [message.content for message in before] == ["first", "hello"]
    assert [message.content for message in after] == ["second", "hello"]


def test_a_repl_needs_a_registry(tmp_path):
    """``build_app`` alone leaves ``sessions=None``; say so at construction.

    The alternative is an :exc:`AttributeError` on ``None`` at the moment
    the first question is asked, which is the least useful place to find
    out that ``attach_sessions`` was not called.
    """
    app = make_app(tmp_path, answering("unused"))

    with pytest.raises(ValueError, match="attach_sessions"):
        Repl(app, source=ScriptedSource(()))


# ---- invariants the catalogue holds on its own -----------------------
#
# These two outlived ``test_cli_port_fidelity.py``, which compared this
# package against ``omicsclaw/surfaces/cli/`` until that tree was deleted.
# The five comparison tests went with the tree because a comparison needs
# both sides. These two never compared anything: they are properties of
# the data this package ships, and they were the reason the deletion did
# not silently drop coverage.


def test_the_repl_offers_a_subset_of_the_catalogue_and_nothing_else():
    """Every implemented name must also be a catalogue name.

    A name offered by the menu but absent from the catalogue would never
    reach :func:`parse_slash_command`'s lookup, so it would be advertised
    and then answered as an unknown command. The strict ``<`` also pins
    the other half: the catalogue is deliberately the larger set, because
    the names this build refuses are refused **by name** rather than
    falling through to the model.
    """
    catalogue = {spec.name for spec in CLI_SLASH_COMMAND_SPECS}
    offered = {spec.name for spec in REPL_SLASH_COMMAND_SPECS}

    assert offered < catalogue
    assert offered  # a filter that matched nothing would pass vacuously


def test_the_logo_rows_are_all_the_same_width():
    """Each row is 74 characters of box drawing, and must stay that way.

    The rows are written as adjacent string literals split across source
    lines, so a row is easy to break while editing and the damage shows up
    as a ragged banner rather than as an error. Three bytes per character
    means one row is ~200 bytes on one line, which is why it is split.
    """
    assert {len(row) for row in LOGO_LINES} == {74}


# ---- a reply the output limit cut off -----------------------------------
#
# The helper and the two sentences are reached through ``_repl``, so that
# on a tree without them each test fails by itself and the file still
# collects.

OPENS_WITH = "The reply was cut off at the output limit"
"""How both forms of the line begin.

Written out here on purpose. ``oc cli --prompt`` prints the line on
standard output and exits 0, so these words are what a script matches,
and changing them is changing an interface.
"""


def says(text: str) -> Message:
    return Message(role=Role.ASSISTANT, content=text)


def writes(text: str = "", *names: str) -> Message:
    """An assistant message whose calls all carry a cut-off payload."""
    return Message.assistant(
        text,
        tool_calls=tuple(
            ToolCall(id=f"c{index}", name=name, arguments=CUT_ARGUMENTS)
            for index, name in enumerate(names or ("write_file",))
        ),
    )


def outcome_of(stop: StopReason, *messages: Message):
    """What ``_cut_off_notice`` reads, without running an exchange."""
    return types.SimpleNamespace(
        result=RunResult(messages=tuple(messages), stop_reason=stop)
    )


def run_repl(tmp_path, provider, lines, **overrides):
    """Run a REPL over *lines*; return its source and what it printed."""

    async def drive():
        app = build(tmp_path, provider, **overrides)
        repl, source, buffer = repl_over(app, lines)
        await asyncio.wait_for(repl.run(), WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return source, buffer.getvalue()

    return asyncio.run(drive())


def lines_that_open_with_it(printed: str) -> list[str]:
    return [line for line in printed.splitlines() if line.startswith(OPENS_WITH)]


@pytest.mark.parametrize("finish", ["length", "max_tokens"])
def test_a_reply_cut_off_at_the_output_limit_is_reported_under_it(tmp_path, finish):
    """One line, after the text that was cut, before the next prompt.

    ``length`` is the OpenAI dialect's word and ``max_tokens`` Anthropic's.
    """
    provider = Finishing((says("Moran's I measures spatial autocorre"), finish))

    source, printed = run_repl(tmp_path, provider, ["what is Moran's I?", "/exit"])

    assert lines_that_open_with_it(printed) == [_repl._CUT_OFF_NOTICE]
    assert printed.index("spatial autocorre") < printed.index(OPENS_WITH)
    assert printed.index(OPENS_WITH) < printed.index("Goodbye")
    assert source.prompts == [PROMPT, PROMPT]


@pytest.mark.parametrize(
    "text", ["Writing the notes now.", ""], ids=["text", "no-text"]
)
def test_a_cut_off_reply_names_the_tool_calls_that_did_not_run(tmp_path, text):
    """The calls reach the screen no other way: nothing starts them.

    With no text the line is the only thing the exchange prints about
    what the model did.
    """
    provider = Finishing((writes(text, "write_file"), "length"))

    _source, printed = run_repl(tmp_path, provider, ["write the notes", "/exit"])

    assert lines_that_open_with_it(printed) == [
        _repl._CUT_OFF_CALLS_NOTICE.format(names="write_file")
    ]
    assert "-> write_file" not in printed


def test_a_cut_in_a_later_model_call_is_reported_once_at_the_end(tmp_path):
    """The first call's tool ran; the second call is the one cut off."""
    provider = Finishing(
        (calling("report"), "tool_calls"),
        (writes("Read it. Writing the notes now.", "write_file"), "length"),
    )

    _source, printed = run_repl(
        tmp_path, provider, ["read, then write", "/exit"], tools=[Reporting()]
    )

    assert lines_that_open_with_it(printed) == [
        _repl._CUT_OFF_CALLS_NOTICE.format(names="write_file")
    ]
    assert printed.index("<- report ok") < printed.index(OPENS_WITH)


@pytest.mark.parametrize(
    ("provider", "overrides"),
    [
        (Finishing(says("An ordinary answer.")), {}),
        (Finishing((says("An ordinary answer."), "")), {}),
        (
            Finishing((calling("report"), "tool_calls")),
            {"max_turns": 1, "tools": [Reporting()]},
        ),
        (Exploding(), {}),
    ],
    ids=["stop", "no-finish-reason", "turn-ceiling", "failed"],
)
def test_an_exchange_that_was_not_cut_off_prints_no_such_line(
    tmp_path, provider, overrides
):
    """An answer that finished, the turn ceiling and a failure all print none."""
    _source, printed = run_repl(tmp_path, provider, ["go", "/exit"], **overrides)

    assert "cut off" not in printed


def test_the_line_is_not_repeated_by_compact_or_by_the_next_answer(tmp_path):
    """``/compact`` goes through the same ``_drive`` and reports its own way."""
    provider = Finishing(
        (says("Moran's I measures spatial autocorre"), "length"),
        says("Moran's I measures spatial autocorrelation."),
    )

    _source, printed = run_repl(
        tmp_path, provider, ["what is Moran's I?", "/compact", "go on", "/exit"]
    )

    assert printed.count(OPENS_WITH) == 1
    assert printed.index(OPENS_WITH) < printed.index("Nothing to compact")
    assert "spatial autocorrelation." in printed


def test_a_second_cut_off_reply_in_the_same_repl_is_reported_too(tmp_path):
    """Each exchange is judged by itself.

    Two cuts are two lines, each in the form of its own reply, and the
    ordinary answer after them gets none.
    """
    provider = Finishing(
        (says("Moran's I measures spatial autocorre"), "length"),
        (writes("Shorter, and written down.", "write_file"), "length"),
        says("Moran's I measures spatial autocorrelation."),
    )

    _source, printed = run_repl(
        tmp_path,
        provider,
        ["what is Moran's I?", "shorter please", "once more", "/exit"],
    )

    assert lines_that_open_with_it(printed) == [
        _repl._CUT_OFF_NOTICE,
        _repl._CUT_OFF_CALLS_NOTICE.format(names="write_file"),
    ]
    assert printed.rindex(OPENS_WITH) < printed.index("spatial autocorrelation.")


def test_a_single_shot_run_reports_the_cut_and_still_converges(tmp_path):
    """Same line, same screen; the verdict the exit code reads is unchanged."""

    async def drive():
        provider = Finishing((says("Moran's I measures spatial autocorre"), "length"))
        app = build(tmp_path, provider)
        buffer = io.StringIO()
        handle = await asyncio.wait_for(
            run_once(app, "what is Moran's I?", screen=Screen.into(buffer)), WAIT_S
        )
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return handle, buffer.getvalue()

    handle, printed = asyncio.run(drive())

    assert lines_that_open_with_it(printed) == [_repl._CUT_OFF_NOTICE]
    assert handle is not None and handle.terminal == "converged"
    assert handle.outcome.result.stop_reason is StopReason.TRUNCATED


def test_the_line_is_printed_after_open_cards_are_taken_down(tmp_path):
    """A card's prompt can outlive its exchange; the line waits for it to go."""

    class Marking(Repl):
        async def _reap_asking(self) -> None:
            await super()._reap_asking()
            self._screen.print("-- cards taken down --")

    async def drive():
        app = build(tmp_path, Finishing((says("half a sente"), "length")))
        buffer = io.StringIO()
        repl = Marking(
            app, source=ScriptedSource(["go", "/exit"]), screen=Screen.into(buffer)
        )
        await asyncio.wait_for(repl.run(), WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return buffer.getvalue()

    printed = asyncio.run(drive())

    assert printed.index("-- cards taken down --") < printed.index(OPENS_WITH)


def test_a_text_only_cut_after_a_tool_ran_does_not_name_that_tool(tmp_path):
    """Only the reply that was cut is read. ``report`` ran, and is not listed."""
    provider = Finishing(
        (calling("report"), "tool_calls"),
        (says("It reports twelve clu"), "length"),
    )

    _source, printed = run_repl(
        tmp_path, provider, ["read it to me", "/exit"], tools=[Reporting()]
    )

    assert "<- report ok" in printed
    assert lines_that_open_with_it(printed) == [_repl._CUT_OFF_NOTICE]
    assert "were not run" not in printed


def test_a_tool_name_with_square_brackets_reaches_the_screen_as_it_is(tmp_path):
    """The line is printed as text. Read as markup, ``[/]`` raises."""
    provider = Finishing((writes("", "write[/]file", "[red]x"), "length"))

    _source, printed = run_repl(tmp_path, provider, ["write the notes", "/exit"])

    assert lines_that_open_with_it(printed) == [
        _repl._CUT_OFF_CALLS_NOTICE.format(names="write[/]file, [red]x")
    ]


def test_the_line_is_handed_to_the_screen_as_yellow_text(tmp_path):
    """A ``Text`` styled yellow, the colour this surface warns in."""

    class Keeping(Screen):
        def __init__(self, sink) -> None:
            super().__init__(Screen.into(sink).console)
            self.handed: list[object] = []

        def print(self, *args: object, **kwargs: object) -> None:
            self.handed.extend(args)
            super().print(*args, **kwargs)

    async def drive():
        app = build(tmp_path, Finishing((says("half a sente"), "length")))
        screen = Keeping(io.StringIO())
        repl = Repl(app, source=ScriptedSource(["go", "/exit"]), screen=screen)
        await asyncio.wait_for(repl.run(), WAIT_S)
        await asyncio.wait_for(app.aclose(), WAIT_S)
        return screen.handed

    handed = asyncio.run(drive())

    ours = [
        item
        for item in handed
        if isinstance(item, Text) and item.plain.startswith(OPENS_WITH)
    ]
    assert [item.plain for item in ours] == [_repl._CUT_OFF_NOTICE]
    assert str(ours[0].style) == "yellow"
    assert not [item for item in handed if isinstance(item, str) and OPENS_WITH in item]


def test_the_notice_is_empty_for_every_ending_but_a_cut():
    """No outcome is a cancelled or failed exchange."""
    cut = says("half a sente")

    assert _repl._cut_off_notice(None) == ""
    assert _repl._cut_off_notice(outcome_of(StopReason.CONVERGED, cut)) == ""
    assert _repl._cut_off_notice(outcome_of(StopReason.MAX_TURNS, writes())) == ""
    assert (
        _repl._cut_off_notice(outcome_of(StopReason.TRUNCATED, cut))
        == _repl._CUT_OFF_NOTICE
    )
    assert (
        _repl._cut_off_notice(outcome_of(StopReason.TRUNCATED))
        == _repl._CUT_OFF_NOTICE
    )
    after_a_round = outcome_of(
        StopReason.TRUNCATED, Message.user("go"), calling("report"), cut
    )
    assert _repl._cut_off_notice(after_a_round) == _repl._CUT_OFF_NOTICE


def test_the_notice_lists_every_call_of_the_cut_message_in_order():
    """One name per call, the complete ones and the repeats included.

    However many there are: the number of names is the number of calls
    that did not run.
    """
    earlier = calling("report")
    cut = Message.assistant(
        "",
        tool_calls=(
            ToolCall(id="c0", name="read_file", arguments='{"path": "notes.md"}'),
            ToolCall(id="c1", name="write_file", arguments=CUT_ARGUMENTS),
            ToolCall(id="c2", name="write_file", arguments=CUT_ARGUMENTS),
        ),
    )
    many = ["read_file"] * 8 + ["write_file"]

    notice = _repl._cut_off_notice(
        outcome_of(StopReason.TRUNCATED, Message.user("go"), earlier, cut)
    )
    of_many = _repl._cut_off_notice(outcome_of(StopReason.TRUNCATED, writes("", *many)))

    assert notice == _repl._CUT_OFF_CALLS_NOTICE.format(
        names="read_file, write_file, write_file"
    )
    assert "report" not in notice
    assert of_many == _repl._CUT_OFF_CALLS_NOTICE.format(names=", ".join(many))


def test_a_tool_name_cannot_act_on_the_terminal_through_the_notice():
    """The name is the model's text; a blank or missing one is shown as ``?``."""
    hostile = writes("", "write\x1b[2J_file\r\n", "", "   ")

    notice = _repl._cut_off_notice(outcome_of(StopReason.TRUNCATED, hostile))

    assert "\x1b" not in notice and "\r" not in notice and "\n" not in notice
    assert notice.startswith(OPENS_WITH)
    assert ", ?, ?." in notice


def test_the_two_forms_read_as_agreed_and_open_with_the_same_words():
    """The wording is written out once, here; see :data:`OPENS_WITH`."""
    assert _repl._CUT_OFF_NOTICE == (
        "The reply was cut off at the output limit and is incomplete. "
        "Ask for a shorter answer, or for it in parts."
    )
    assert _repl._CUT_OFF_CALLS_NOTICE.format(names="write_file") == (
        "The reply was cut off at the output limit, so the tool calls in it "
        "were not run: write_file. Ask for the work in smaller pieces."
    )
    assert _repl._CUT_OFF_NOTICE.startswith(OPENS_WITH)
    assert _repl._CUT_OFF_CALLS_NOTICE.startswith(OPENS_WITH)
