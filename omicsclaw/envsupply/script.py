"""The local first-use install script, its disclosure, and its runner.

C3's ``build_env_install_script`` (remote) is the vocabulary this module
mirrors: ``set -e``; locate uv; ``uv python install``; ``uv venv``;
``uv pip install`` the base + stack; verify by importing ipykernel at
the end. Idempotent by construction the same way — every uv step is a
no-op when its output exists, so a retried install heals a
half-finished one instead of fighting it.

The local differences are the plan's, not inventions:

* **the uv ladder has no curl rung.** The App ships the uv binary
  (Tier 0), so resolution is: the ``OMICSCLAW_UV_BIN`` environment
  variable (what the App points at its shipped installer), then
  ``$OMICSCLAW_UV_INSTALLERS_DIR/uv``, then ``command -v uv`` — and a
  machine with none of the three is *reported* (exit 127, manual
  commands in the error), never fixed over the network unprompted;
* **mirrors are defaults, not opts-in.** ``UV_INDEX_URL`` prefixes the
  pip line and ``UV_PYTHON_INSTALL_MIRROR`` the interpreter line with
  the CN-friendly defaults from :mod:`.pins`, overridable per
  deployment — the mirror URLs always come from configuration, never
  from anything a model sent;
* **step markers** (``===OMICSCLAW_ENV_STEP:<name>===``) echo before
  each phase so the jobs plane can stream honest phase-level progress
  without parsing uv's output format.

POSIX sh, like the remote script: the desktop backend's install paths
run where ``sh`` exists. A Windows-first-run bootstrap is the App's
business (it drives the shipped ``uv.exe`` directly), not this
script's.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Callable, Sequence

from .pins import (
    CORE_STACK_PACKAGES,
    DEFAULT_ENV_NAME,
    DEFAULT_UV_INDEX_URL,
    DEFAULT_UV_PYTHON_MIRROR,
    ENV_ROOT,
    KERNEL_BASE_PACKAGES,
    PYTHON_VERSION,
)

__all__ = [
    "DEFAULT_ENV_DIR",
    "ENV_INSTALL_TIMEOUT_S",
    "ENV_STEP_MARKER_PREFIX",
    "EnvSupplyError",
    "UV_NOT_FOUND_EXIT",
    "build_env_install_script",
    "env_step_line",
    "manual_env_commands",
    "parse_env_step",
    "run_install_script",
]

DEFAULT_ENV_DIR = ENV_ROOT.rstrip("/") + "/" + DEFAULT_ENV_NAME
"""``~/.omicsclaw/envs/main`` — the Tier-1 environment the App
bootstraps and the backend defaults to."""

ENV_INSTALL_TIMEOUT_S = 1800.0
"""The remote plane's half-hour budget, copied: the scanpy stack is
hundreds of megabytes and a lab uplink is not a datacenter's."""

ENV_STEP_MARKER_PREFIX = "===OMICSCLAW_ENV_STEP:"
"""Every step marker starts with this; the name follows, ``===`` closes."""

UV_NOT_FOUND_EXIT = 127
"""The shell's own "command not found" code, reused for the no-uv
refusal so the failure reads as what it is."""

_CLOSED_ALPHABET = re.compile(r"[A-Za-z0-9_./-]+")


def _path_literal(path: str) -> str:
    """A path as POSIX sh should receive it.

    The remote plane's rule, restated for the local script: a leading
    ``~`` means $HOME and single-quoting would make it a literal tilde
    (the ``mkdir '~'`` trap), so it is spliced as ``"$HOME"/tail`` with
    the tail unquoted — safe because every path this package builds is
    its own closed alphabet (``[A-Za-z0-9_./-]``). Anything else is
    single-quoted whole via :func:`shlex.quote`.
    """
    if path.startswith("~/"):
        tail = path[2:]
        if _CLOSED_ALPHABET.fullmatch(tail or ""):
            return '"$HOME"/' + tail
    return shlex.quote(path)


def env_step_line(name: str) -> str:
    """The marker line a runner reads as "phase *name* started"."""
    return f"{ENV_STEP_MARKER_PREFIX}{name}==="


def parse_env_step(line: str) -> str | None:
    """The step name a line announces, or ``None`` if it is no marker.

    Tolerant of the newline and of uv output interleaving: markers are
    matched on the stripped line, and a line that merely contains a
    marker amid other text is not one (the script echoes them alone)."""
    stripped = line.strip()
    if not stripped.startswith(ENV_STEP_MARKER_PREFIX):
        return None
    if not stripped.endswith("==="):
        return None
    name = stripped[len(ENV_STEP_MARKER_PREFIX):-3]
    return name or None


class EnvSupplyError(RuntimeError):
    """A local environment could not be provisioned.

    Carries the manual commands — the remote plane's disclosure rule:
    the failure mode the plan prefers to a silent fallback is the person
    being told exactly what to run by hand on the machine that refused
    automation."""

    def __init__(self, message: str, manual_commands: Sequence[str] = ()) -> None:
        self.manual_commands = tuple(manual_commands)
        text = message
        if self.manual_commands:
            text += " Manual setup:\n  " + "\n  ".join(self.manual_commands)
        super().__init__(text)


def build_env_install_script(
    env_dir: str,
    *,
    packages: Sequence[str] = (),
    python_version: str = PYTHON_VERSION,
    uv_index_url: str | None = DEFAULT_UV_INDEX_URL,
    uv_python_mirror: str | None = DEFAULT_UV_PYTHON_MIRROR,
) -> str:
    """The first-use install script, POSIX sh. Pure.

    Same step order as the remote script (interpreter, venv, packages,
    verify-at-the-end) with the local ladder and mirror injection. The
    package list is the base + core stacks **plus** *packages* — extras
    add on top, they never replace the core (a Tier-2 domain overlay
    that dropped scanpy would quietly break every env it touched), and
    re-listing what is already installed costs uv one "already
    satisfied" line, which is the idempotence working. Every name is
    shell-quoted, because the callers are tools and jobs whose
    arguments arrived as model text.
    """
    resolved = (
        tuple(KERNEL_BASE_PACKAGES) + CORE_STACK_PACKAGES + tuple(packages)
    )
    quoted_packages = " ".join(shlex.quote(name) for name in resolved)
    version = shlex.quote(python_version)
    env = _path_literal(env_dir)
    env_python = _path_literal(env_dir.rstrip("/") + "/bin/python")
    index_prefix = f"UV_INDEX_URL={shlex.quote(uv_index_url)} " if uv_index_url else ""
    mirror_prefix = (
        f"UV_PYTHON_INSTALL_MIRROR={shlex.quote(uv_python_mirror)} "
        if uv_python_mirror
        else ""
    )
    lines = [
        "set -e",
        # The local uv ladder: the App's shipped binary (named by either
        # environment variable), then PATH. No curl rung — a machine with
        # none of these is reported, not fixed over the network.
        'UV="${OMICSCLAW_UV_BIN:-}"',
        (
            'if [ -z "$UV" ] && [ -n "${OMICSCLAW_UV_INSTALLERS_DIR:-}" ]'
            ' && [ -x "${OMICSCLAW_UV_INSTALLERS_DIR}/uv" ]; then'
            ' UV="${OMICSCLAW_UV_INSTALLERS_DIR}/uv"; fi'
        ),
        'if [ -z "$UV" ]; then UV="$(command -v uv 2>/dev/null || true)"; fi',
        (
            'if [ -z "$UV" ]; then echo "omicsclaw: no uv executable found'
            ' (set OMICSCLAW_UV_BIN or put uv on PATH); the manual commands'
            ' are in the error that follows" >&2; exit '
            f"{UV_NOT_FOUND_EXIT}; fi"
        ),
        env_step_line("python"),
        f'{mirror_prefix}"$UV" python install {version}',
        env_step_line("venv"),
        f'"$UV" venv {env} --python {version}',
        env_step_line("packages"),
        f'{index_prefix}"$UV" pip install --python {env_python}'
        f" {quoted_packages}",
        env_step_line("verify"),
        f"{env_python} -c 'import ipykernel'",
    ]
    return "\n".join(lines)


def manual_env_commands(
    env_dir: str,
    *,
    python_version: str = PYTHON_VERSION,
    uv_index_url: str | None = DEFAULT_UV_INDEX_URL,
    uv_python_mirror: str | None = DEFAULT_UV_PYTHON_MIRROR,
) -> tuple[str, ...]:
    """The by-hand equivalent of the install, for the error card.

    Mirrors the remote plane's disclosure: plain commands a person can
    paste, mirrors inlined where the script would inject them, uv
    assumed present (its absence is the thing being disclosed around).
    """
    packages = " ".join(KERNEL_BASE_PACKAGES + CORE_STACK_PACKAGES)
    pip = f"{env_dir}/bin/python"
    commands = [
        f"uv python install {python_version}"
        + (
            f"   # with UV_PYTHON_INSTALL_MIRROR={uv_python_mirror}"
            if uv_python_mirror
            else ""
        ),
        f"uv venv {env_dir} --python {python_version}",
        f"uv pip install --python {pip} {packages}"
        + (f"   # with UV_INDEX_URL={uv_index_url}" if uv_index_url else ""),
        f"{pip} -c 'import ipykernel'",
    ]
    return tuple(commands)


# ---- the runner ----------------------------------------------------------------


InstallRunner = Callable[..., int]
"""``runner(argv, on_line=..., timeout=...)`` runs one command, hands
each output line to *on_line* as it arrives (from a worker thread), and
answers the exit code. Injectable so tests and the App-side driver
alike never need a real shell."""


def default_install_runner(
    argv: Sequence[str],
    *,
    on_line: Callable[[str], None] | None = None,
    timeout: float = ENV_INSTALL_TIMEOUT_S,
) -> int:
    """Run the script under ``sh``, streaming merged output line by line.

    stdout and stderr share one pipe on purpose: uv's useful progress
    goes to stderr, and a caller streaming phases wants both in order.
    The timeout kills the process (uv installs are resumable — the
    script's idempotence is what makes a timeout safe).
    """
    process = subprocess.Popen(
        list(argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert process.stdout is not None  # PIPE was asked for above
        for line in process.stdout:
            if on_line is not None:
                on_line(line.rstrip("\n"))
        return process.wait(timeout=max(0.0, timeout))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def run_install_script(
    script: str,
    *,
    runner: InstallRunner | None = None,
    on_line: Callable[[str], None] | None = None,
    timeout: float = ENV_INSTALL_TIMEOUT_S,
) -> int:
    """Execute one built script through the (injectable) runner.

    The mechanism half only: it answers the exit code and stays silent
    about policy — who approved the install is the caller's question.
    """
    spawn = runner or default_install_runner
    return spawn(["sh", "-c", script], on_line=on_line, timeout=timeout)
