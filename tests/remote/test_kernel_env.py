"""C3's environment half: the supply commands, and their idempotence.

Everything here runs over the fake spawner (``tests/remote/_fakes.py``
style) or over pure builders — no network, no host. What these pin:

* the probe is the whole fast path — an environment that can already
  import ipykernel costs one ssh command and never an install;
* the install script's shape: uv located three ways (PATH, ``~/.local``,
  curl), ``uv python install`` for the interpreter, the venv, the pip
  line with the core stack, and the mirror injection when one was
  configured;
* a pinned interpreter (``python=``) is probed and trusted, never
  installed into;
* the one-call composition refuses to install without a consent hook
  (fail closed) and honours a denial by raising;
* the runtime's per-host cache: the second ``ensure_env`` issues no
  ssh at all.
"""

from __future__ import annotations

import pytest

from omicsclaw.remote.kernel import (
    CORE_STACK_PACKAGES,
    DEFAULT_ENV_DIR,
    RemoteKernelEnv,
    RemoteKernelEnvError,
    RemoteKernelRuntime,
    build_env_install_script,
    build_env_probe_command,
    ensure_remote_kernel_env,
)
from omicsclaw.remote.ssh import RemoteHostRefused
from omicsclaw.tools.builtin.remote import ExecOutcome
from tests.remote._fakes import FakeSpawner, run

ENV_PYTHON = DEFAULT_ENV_DIR + "/bin/python"


class _GrowingEnvSpawner(FakeSpawner):
    """The probe's answers, in call order: the env starts missing.

    The install script travels base64-encoded (the probe-script
    convention), so no needle can see inside it; this double instead
    answers successive ``import ipykernel`` probes from a list — the
    first says "missing", the rest say "there" — and delegates
    everything else to the canned rules, which the test aims at the
    ``base64`` marker the encoded command always carries."""

    def __init__(self, probe_exits: list[int]) -> None:
        super().__init__()
        self._probe_exits = list(probe_exits)

    async def spawn(self, argv, *, timeout, stdin_text=None):
        command = argv[-1] if argv else ""
        if "import ipykernel" in command:
            self.calls.append((list(argv), stdin_text))
            code = (
                self._probe_exits.pop(0)
                if self._probe_exits
                else self._probe_exits_default
            )
            return ExecOutcome(exit_code=code)
        return await super().spawn(argv, timeout=timeout, stdin_text=stdin_text)

    _probe_exits_default = 0


# ---- the pure builders ------------------------------------------------------


def test_the_probe_is_one_import_check_of_the_env_python():
    command = build_env_probe_command(ENV_PYTHON)
    # the ~ is the remote $HOME: single quotes would keep it literal,
    # so the path arrives as an expanded "$HOME" splice
    assert command == (
        '"$HOME"/.omicsclaw/kernel-env/bin/python -c \'import ipykernel\''
    )


def test_the_install_script_locates_uv_three_ways_and_installs_the_stack():
    script = build_env_install_script(DEFAULT_ENV_DIR, uv_index_url=None)
    assert 'command -v uv' in script
    assert '"$HOME/.local/bin/uv"' in script
    assert "curl -LsSf https://astral.sh/uv/install.sh | sh" in script
    assert '"$UV" python install' in script
    assert '"$UV" venv' in script and "'3.11'" in script
    assert '"$UV" pip install' in script
    for package in ("ipykernel", "jupyter_client", *CORE_STACK_PACKAGES):
        assert package in script
    assert "UV_INDEX_URL" not in script  # no mirror configured


def test_the_mirror_is_injected_on_the_pip_line_only():
    script = build_env_install_script(
        DEFAULT_ENV_DIR, uv_index_url="https://mirrors.tuna.example/pypi/simple"
    )
    assert "UV_INDEX_URL=https://mirrors.tuna.example/pypi/simple" in script
    assert script.count("UV_INDEX_URL=") == 1
    assert '"$UV" pip install' in script


# ---- the one-call composition over the fake spawner -------------------------


def test_a_ready_environment_is_reused_without_installing():
    spawner = FakeSpawner().on("import ipykernel", ExecOutcome(exit_code=0))
    env = run(
        ensure_remote_kernel_env(spawner, "lab1", approve=_explode_if_called)
    )
    assert env == RemoteKernelEnv(host="lab1", python=ENV_PYTHON)
    assert len(spawner.ssh_commands()) == 1
    assert "uv" not in spawner.ssh_commands()[0]


def test_a_missing_environment_installs_then_verifies():
    spawner = _GrowingEnvSpawner([1, 0]).on(
        "base64", ExecOutcome(output="installed", exit_code=0)
    )
    env = run(ensure_remote_kernel_env(spawner, "lab1", approve=_approve))
    assert env.provisioned is True
    commands = spawner.ssh_commands()
    assert len(commands) == 3
    assert "import ipykernel" in commands[0]
    assert "base64" in commands[1]  # the install script, encoded like the probe
    assert "import ipykernel" in commands[2]


def test_a_failed_install_carries_the_manual_commands():
    spawner = _GrowingEnvSpawner([1]).on(
        "base64", ExecOutcome(output="curl: network unreachable", exit_code=3)
    )
    with pytest.raises(RemoteKernelEnvError) as caught:
        run(ensure_remote_kernel_env(spawner, "lab1", approve=_approve))
    message = str(caught.value)
    assert "exit 3" in message and "network unreachable" in message
    # the manual way out rides in the message, not a doc page
    assert "uv venv ~/.omicsclaw/kernel-env" in message
    assert "uv pip install" in message and "ipykernel" in message


def test_an_install_that_verifies_nothing_is_an_error_not_a_lie():
    # the install "succeeds" but every probe still answers non-zero:
    # reporting a provisioned env whose python cannot import would be
    # the silent lie this error exists to prevent
    spawner = _GrowingEnvSpawner([1, 1]).on(
        "base64", ExecOutcome(exit_code=0)
    )
    with pytest.raises(RemoteKernelEnvError):
        run(ensure_remote_kernel_env(spawner, "lab1", approve=_approve))


def test_no_consent_hook_means_no_install_fail_closed():
    spawner = _GrowingEnvSpawner([1])
    with pytest.raises(RemoteKernelEnvError) as caught:
        run(ensure_remote_kernel_env(spawner, "lab1"))
    assert "no approval channel" in str(caught.value)
    # and nothing ran beyond the probe
    assert len(spawner.ssh_commands()) == 1


def test_a_denied_consent_stops_before_any_install():
    spawner = _GrowingEnvSpawner([1])
    denied = False

    async def refuse(host: str) -> None:
        nonlocal denied
        denied = True
        raise PermissionError("the person said no")
    with pytest.raises(PermissionError):
        run(ensure_remote_kernel_env(spawner, "lab1", approve=refuse))
    assert denied is True
    assert len(spawner.ssh_commands()) == 1  # the probe, nothing else


def test_a_pinned_interpreter_is_probed_and_never_installed_into():
    spawner = FakeSpawner().on("import ipykernel", ExecOutcome(exit_code=0))
    env = run(
        ensure_remote_kernel_env(
            spawner, "lab1", python="/opt/conda/envs/bio/bin/python"
        )
    )
    assert env.python == "/opt/conda/envs/bio/bin/python"
    assert env.provisioned is False
    assert "conda/envs/bio" in spawner.ssh_commands()[0]
    assert not any("uv" in command for command in spawner.ssh_commands())


def test_a_pinned_interpreter_that_cannot_import_is_an_error():
    spawner = FakeSpawner().on("import ipykernel", ExecOutcome(exit_code=1))
    with pytest.raises(RemoteKernelEnvError) as caught:
        run(ensure_remote_kernel_env(spawner, "lab1", python="/bin/false"))
    assert "/bin/false" in str(caught.value)
    assert "uv" not in str(caught.value)  # a pin never triggers an install


def test_a_bad_host_is_refused_before_any_transport():
    spawner = FakeSpawner()
    with pytest.raises(RemoteHostRefused):
        run(ensure_remote_kernel_env(spawner, "lab 1; rm -rf"))
    assert spawner.calls == []


# ---- the runtime's cache ------------------------------------------------------


def test_the_runtime_caches_per_host_second_call_is_silent():
    spawner = FakeSpawner().on("import ipykernel", ExecOutcome(exit_code=0))
    runtime = RemoteKernelRuntime(spawner)
    first = run(runtime.ensure_env("lab1"))
    second = run(runtime.ensure_env("lab1"))
    assert first is second
    assert len(spawner.ssh_commands()) == 1  # one probe for both calls


def test_the_runtime_pins_a_python_for_its_whole_life():
    spawner = FakeSpawner().on("import ipykernel", ExecOutcome(exit_code=0))
    runtime = RemoteKernelRuntime(spawner, python="/opt/shared/bin/python")
    env = run(runtime.ensure_env("lab1"))
    assert env.python == "/opt/shared/bin/python"
    assert not any("uv" in c for c in spawner.ssh_commands())


# ---- helpers -------------------------------------------------------------------


async def _approve(host: str) -> None:
    return None


def _explode_if_called(host: str):
    raise AssertionError("the approve hook must not run on a ready env")
