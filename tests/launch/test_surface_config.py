"""What each entry point is allowed to run a configuration as.

``surface_config`` is the one place a resolved configuration is changed
because of which entry point is about to run it. Its first rule is about
``ask_user``: an entry point that cannot show a question to a person and
read the answer back runs with it off, whatever the flag or the
environment said.

This is the only defence. :class:`~omicsclaw.entry.turn.TurnRunner` binds
the question channel whenever the configuration says so and knows nothing
of surfaces, so an app assembled directly with ``ask_user=True`` and no
one answering really would wait. The tests below therefore assert on the
configuration each ``start_*`` hands to the code that assembles the app,
which is the last point at which the switch can still be turned.

The INFO record is unconditional when the value changes. The shell cannot
tell a deliberate ``--ask-user true`` from a default, and may not read the
flag or the environment a second time to find out.

Entry points that ask are listed in ``_ASKING_SURFACES``. While it is
empty every row of the table below is off, the REPL included: the REPL
cannot read an answer until the CLI's question card exists, and a mounted
tool with nobody reading would hang the first exchange that used it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import get_args

import pytest

from omicsclaw.entry.config import AppConfig
from omicsclaw.launch import _surfaces
from omicsclaw.launch._surfaces import ReplOptions, SurfaceName, surface_config

SURFACES = get_args(SurfaceName)

CANNOT_ASK = tuple(name for name in SURFACES if name not in _surfaces._ASKING_SURFACES)


def _records(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "omicsclaw.launch" and "ask_user" in record.getMessage()
    ]


# ---- the function ---------------------------------------------------------------


def test_the_entry_points_are_the_five_the_shell_starts():
    assert SURFACES == ("repl", "once", "piped", "desktop", "channel")
    assert _surfaces._ASKING_SURFACES <= set(SURFACES)


@pytest.mark.parametrize("surface", CANNOT_ASK)
def test_an_entry_point_that_cannot_ask_runs_with_ask_user_off_and_says_so(
    surface, tmp_path, caplog
):
    """Mutation: let ``piped`` through and a question would read the next
    line of the script on standard input as the person's decision."""
    config = AppConfig(workspace=tmp_path, ask_user=True, max_turns=7)

    with caplog.at_level(logging.INFO, logger="omicsclaw.launch"):
        run_as = surface_config(config, surface)

    assert run_as.ask_user is False
    assert dataclasses.replace(run_as, ask_user=True) == config, "nothing else moved"
    (line,) = _records(caplog)
    assert surface in line
    assert not line.startswith("--")


@pytest.mark.parametrize("surface", SURFACES)
def test_a_configuration_already_off_is_returned_as_it_is_and_nothing_is_logged(
    surface, tmp_path, caplog
):
    config = AppConfig(workspace=tmp_path, ask_user=False)

    with caplog.at_level(logging.INFO, logger="omicsclaw.launch"):
        run_as = surface_config(config, surface)

    assert run_as is config
    assert _records(caplog) == []


@pytest.mark.parametrize("surface", sorted(_surfaces._ASKING_SURFACES))
def test_an_entry_point_that_asks_keeps_what_the_configuration_says(
    surface, tmp_path, caplog
):
    config = AppConfig(workspace=tmp_path, ask_user=True)

    with caplog.at_level(logging.INFO, logger="omicsclaw.launch"):
        assert surface_config(config, surface) is config

    assert _records(caplog) == []


def test_a_name_that_is_not_an_entry_point_is_refused(tmp_path):
    """The annotation only constrains a type checker."""
    with pytest.raises(ValueError, match="unknown surface 'tui'"):
        surface_config(AppConfig(workspace=tmp_path), "tui")  # type: ignore[arg-type]


# ---- oc cli ---------------------------------------------------------------------


def _started_cli(monkeypatch, tmp_path, surface_argv, *, interactive: bool):
    """Run ``start_cli`` with ``--ask-user true`` and report what it started."""
    started: list[tuple[AppConfig, dict]] = []
    asked: list[bool] = []

    async def run_cli(config, options, **kwargs):
        started.append((config, kwargs))
        return 0

    def is_interactive() -> bool:
        asked.append(interactive)
        return interactive

    monkeypatch.setattr(_surfaces, "_run_cli", run_cli)
    monkeypatch.setattr(_surfaces, "is_interactive", is_interactive)
    deployment = ["--ask-user", "true", "--workspace", str(tmp_path)]

    assert _surfaces.start_cli(deployment, surface_argv, {"LLM_API_KEY": "k"}) == 0
    ((config, kwargs),) = started
    return config, kwargs, asked


@pytest.mark.parametrize(
    ("surface", "surface_argv", "interactive"),
    [
        ("once", ["--prompt", "summarise the run"], True),
        ("once", ["--prompt", "summarise the run"], False),
        ("piped", [], False),
        ("repl", [], True),
    ],
)
def test_oc_cli_hands_on_the_configuration_of_the_entry_point_it_is(
    monkeypatch, tmp_path, caplog, surface, surface_argv, interactive
):
    """One exchange has no next line to read an answer from, and a pipe's
    next line is the script's, so neither may ask. The REPL at a terminal
    is the entry point that can, once it is listed as one."""
    with caplog.at_level(logging.INFO, logger="omicsclaw.launch"):
        config, _kwargs, _asked = _started_cli(
            monkeypatch, tmp_path, surface_argv, interactive=interactive
        )

    assert config.ask_user is (surface in _surfaces._ASKING_SURFACES)
    assert config.workspace == tmp_path.resolve()
    assert len(_records(caplog)) == (0 if config.ask_user else 1)
    if not config.ask_user:
        assert f"the {surface} entry point" in _records(caplog)[0]


@pytest.mark.parametrize("interactive", [True, False])
def test_standard_input_is_tested_once_and_the_repl_is_told_the_answer(
    monkeypatch, tmp_path, interactive
):
    """Two tests of standard input could disagree, and then the tool table
    would be chosen for a terminal while the prompt source read a pipe.

    Mutation: stop passing ``interactive=`` to ``_run_cli`` and the keyword
    below is missing.
    """
    _config, kwargs, asked = _started_cli(
        monkeypatch, tmp_path, [], interactive=interactive
    )

    assert asked == [interactive]
    assert kwargs["interactive"] is interactive


class _Source:
    def close(self) -> None:
        pass


class _App:
    async def aclose(self) -> None:
        pass


class _Repl:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def welcome(self) -> None:
        pass

    async def run(self) -> None:
        return None


@pytest.mark.parametrize("interactive", [True, False, None])
def test_the_repl_s_prompt_source_is_opened_with_what_the_shell_decided(
    monkeypatch, interactive
):
    opened: list[dict] = []

    async def open_app(_config):
        return _App()

    def open_prompt_source(**kwargs):
        opened.append(kwargs)
        return _Source()

    monkeypatch.setattr(_surfaces, "open_app", open_app)
    monkeypatch.setattr(_surfaces, "attach_sessions", lambda given: given)
    monkeypatch.setattr(_surfaces, "open_prompt_source", open_prompt_source)
    monkeypatch.setattr(_surfaces, "Screen", lambda *a, **k: None)
    monkeypatch.setattr(_surfaces, "Repl", _Repl)

    code = asyncio.run(
        _surfaces._run_cli(object(), ReplOptions(), interactive=interactive)
    )

    assert code == 0
    assert opened == [{"interactive": interactive}]


# ---- oc desktop and oc channel ----------------------------------------------------


def test_oc_desktop_serves_with_ask_user_off(monkeypatch, tmp_path, caplog):
    """The Desktop routes have none an answer could arrive on, and the
    wire contract has no frame that would show the question."""
    served: list[AppConfig] = []

    async def serve(config, options, token, uvicorn, settings=None):
        served.append(config)
        return 0

    monkeypatch.setattr(_surfaces, "_asgi_server_module", lambda: object())
    monkeypatch.setattr(_surfaces, "_serve_desktop", serve)

    with caplog.at_level(logging.INFO, logger="omicsclaw.launch"):
        code = _surfaces.start_desktop(
            ["--ask-user", "true", "--workspace", str(tmp_path)], [], {}
        )

    assert code == 0
    assert [config.ask_user for config in served] == [False]
    assert "the desktop entry point" in _records(caplog)[0]


def test_oc_channel_serves_with_ask_user_off_and_its_deadline_untouched(
    monkeypatch, tmp_path, caplog
):
    """A chat has no way to answer yet. The approval deadline is whatever
    the configuration says: ``surface_config`` changes one field."""
    served: list[AppConfig] = []

    async def serve(config, _options, _env):
        served.append(config)
        return 0

    monkeypatch.setattr(_surfaces, "_serve_channels", serve)

    with caplog.at_level(logging.INFO, logger="omicsclaw.launch"):
        asking = ["--ask-user", "true", "--workspace", str(tmp_path)]
        channels = ["--channels", "telegram"]
        timed = _surfaces.start_channel(
            [*asking, "--approval-timeout", "5"], channels, {}
        )
        untimed = _surfaces.start_channel(asking, channels, {})

    assert (timed, untimed) == (0, 0)
    assert [config.ask_user for config in served] == [False, False]
    assert [config.approval_timeout_s for config in served] == [5.0, None]
    assert len(_records(caplog)) == 2
    assert "the channel entry point" in _records(caplog)[0]
