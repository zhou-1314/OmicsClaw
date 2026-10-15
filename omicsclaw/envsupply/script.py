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

import codecs
import os
import re
import selectors
import shlex
import signal
import subprocess
import time
from collections.abc import Callable, Sequence

from .pins import (
    CORE_STACK_PACKAGES,
    DEFAULT_ENV_NAME,
    DEFAULT_UV_INDEX_URL,
    DEFAULT_UV_PYTHON_MIRROR,
    ENV_ROOT,
    KERNEL_BASE_PACKAGES,
    PYTHON_VERSION,
    without_control_credentials,
)

__all__ = [
    "DEFAULT_ENV_DIR",
    "ENV_INSTALL_TIMEOUT_S",
    "ENV_STEP_MARKER_PREFIX",
    "INSTALL_OUTPUT_LIMIT_EXIT",
    "INSTALL_TIMEOUT_EXIT",
    "MAX_INSTALL_OUTPUT_BYTES",
    "UV_NOT_FOUND_EXIT",
    "EnvSupplyError",
    "build_env_install_script",
    "default_install_runner",
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
    name = stripped[len(ENV_STEP_MARKER_PREFIX) : -3]
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
    resolved = tuple(KERNEL_BASE_PACKAGES) + CORE_STACK_PACKAGES + tuple(packages)
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
            " (set OMICSCLAW_UV_BIN or put uv on PATH); the manual commands"
            ' are in the error that follows" >&2; exit '
            f"{UV_NOT_FOUND_EXIT}; fi"
        ),
        env_step_line("python"),
        f'{mirror_prefix}"$UV" python install {version}',
        env_step_line("venv"),
        f'"$UV" venv {env} --python {version}',
        env_step_line("packages"),
        (f'{index_prefix}"$UV" pip install --python {env_python} {quoted_packages}'),
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
    paste **and run as pasted** — the mirrors ride as ``VAR=value``
    prefixes exactly where the script injects them (review m5), not as
    comments someone has to reconstruct, and uv is assumed present (its
    absence is the thing being disclosed around).
    """
    packages = " ".join(KERNEL_BASE_PACKAGES + CORE_STACK_PACKAGES)
    pip = f"{env_dir}/bin/python"
    mirror_prefix = (
        f"UV_PYTHON_INSTALL_MIRROR={shlex.quote(uv_python_mirror)} "
        if uv_python_mirror
        else ""
    )
    index_prefix = f"UV_INDEX_URL={shlex.quote(uv_index_url)} " if uv_index_url else ""
    commands = [
        f"{mirror_prefix}uv python install {python_version}",
        f"uv venv {env_dir} --python {python_version}",
        f"{index_prefix}uv pip install --python {pip} {packages}",
        f"{pip} -c 'import ipykernel'",
    ]
    return tuple(commands)


# ---- the runner ----------------------------------------------------------------


InstallRunner = Callable[..., int]
"""``runner(argv, on_line=..., timeout=...)`` runs one command, hands
each output line to *on_line* as it arrives (from a worker thread), and
answers the exit code. Injectable so tests and the App-side driver
alike never need a real shell."""

INSTALL_TIMEOUT_EXIT = 124
"""The exit code answered when the install exceeds its deadline —
GNU ``timeout(1)``'s convention, so an operator reading "exit 124"
already knows what it means."""

INSTALL_OUTPUT_LIMIT_EXIT = 125
MAX_INSTALL_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_INSTALL_FRAGMENT_BYTES = 16 * 1024


def default_install_runner(
    argv: Sequence[str],
    *,
    on_line: Callable[[str], None] | None = None,
    timeout: float = ENV_INSTALL_TIMEOUT_S,
    env_provider: Callable[[], dict[str, str]] | None = None,
) -> int:
    """Run a POSIX installer in its own session with bounded output.

    The deadline includes silent children and EOF followed by a lingering
    process. Timeout, output overflow and callback failure stop the owned
    process group before returning. A selector reads fixed-size byte chunks;
    incomplete lines cannot create an unbounded queue or allocation.
    """
    scrubber = env_provider or without_control_credentials
    if os.name != "posix":
        raise EnvSupplyError("install runner requires a POSIX process group")
    process = subprocess.Popen(
        list(argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=scrubber(),
        start_new_session=True,
        bufsize=0,
    )
    deadline = time.monotonic() + max(0.0, timeout)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = bytearray()
    total = 0

    def stop_owned_group() -> None:
        for action in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, action)
            except ProcessLookupError:
                # The session has already exited; no other process is selected.
                break
            if action == signal.SIGTERM:
                try:
                    process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    pass  # The next step kills this same owned session.
        process.wait(timeout=2)

    def refuse(code: int, reason: str) -> int:
        stop_owned_group()
        if on_line is not None:
            on_line(
                "omicsclaw: install "
                + reason
                + "; owned process group stopped; retry is resumable"
            )
        return code

    def emit(fragment: bytes, *, final: bool = False) -> None:
        text = decoder.decode(fragment, final=final)
        if on_line is not None and text:
            on_line(text.rstrip("\n"))

    stream = process.stdout
    try:
        if stream is None:
            raise EnvSupplyError("install output pipe unavailable")
        with selectors.DefaultSelector() as selector:
            selector.register(stream, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    return refuse(
                        INSTALL_TIMEOUT_EXIT,
                        f"exceeded its {max(0.0, timeout):g}s deadline",
                    )
                chunk = os.read(stream.fileno(), 4096)
                if not chunk:
                    emit(bytes(pending), final=True)
                    break
                total += len(chunk)
                if total > MAX_INSTALL_OUTPUT_BYTES:
                    return refuse(
                        INSTALL_OUTPUT_LIMIT_EXIT, "exceeded its output byte limit"
                    )
                pending.extend(chunk)
                while pending:
                    newline = pending.find(b"\n")
                    if 0 <= newline < MAX_INSTALL_FRAGMENT_BYTES:
                        size = newline + 1
                    elif len(pending) >= MAX_INSTALL_FRAGMENT_BYTES:
                        size = MAX_INSTALL_FRAGMENT_BYTES
                    else:
                        break
                    emit(bytes(pending[:size]))
                    del pending[:size]
            try:
                return process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                return refuse(INSTALL_TIMEOUT_EXIT, "exceeded its deadline after EOF")
    except BaseException:
        stop_owned_group()
        raise
    finally:
        if stream is not None:
            stream.close()


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
