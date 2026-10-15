"""Probe one local Python environment: exists, kernel-ready, gaps.

The local twin of the remote plane's one-command probe
(:func:`omicsclaw.remote.kernel.build_env_probe_command`), grown the one
dimension the plan's ``manage_environments`` tool needs: not just "can
this interpreter host a kernel" but "which of the declared packages is
it missing". Still **one subprocess**: the interpreter is asked to
``find_spec`` every name at once and answer between markers, because a
probe that costs one process per package turns a ``list`` action into a
process storm on a machine with several envs.

Pure mechanism: the runner that spawns the interpreter is injectable
(the binding passes a real one, a test a fake), and nothing here decides
who may install what — that is the caller's policy.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .pins import CORE_STACK_PACKAGES, KERNEL_BASE_PACKAGES, without_control_credentials

__all__ = [
    "DEFAULT_PROBE_PACKAGES",
    "EnvProbe",
    "ProbeOutcome",
    "PROBE_SENTINEL",
    "build_probe_argv",
    "default_probe_runner",
    "interpreter_in_env",
    "probe_interpreter",
]

PROBE_SENTINEL = "===OMICSCLAW_ENV_PROBE==="
"""The stdout line that says the interpreter ran our code (and not, say,
a shell that swallowed it); the JSON answer follows it."""

DEFAULT_PROBE_TIMEOUT_S = 60.0
"""Import time on a cold network home is real; the remote probe allows
itself the same minute."""

DEFAULT_PROBE_PACKAGES: tuple[str, ...] = (
    KERNEL_BASE_PACKAGES + CORE_STACK_PACKAGES
)

_PROBE_SOURCE = (
    "import importlib.util, json, sys\n"
    "found = {}\n"
    "for name in sys.argv[1:]:\n"
    "    try:\n"
    "        found[name] = importlib.util.find_spec(name) is not None\n"
    "    except Exception:\n"
    "        found[name] = False\n"
    'print("' + PROBE_SENTINEL + '")\n'
    "print(json.dumps(found))\n"
)


@dataclass(frozen=True, slots=True)
class EnvProbe:
    """What one probe learned about one interpreter.

    ``missing`` reports on the names the probe was asked about — the
    base + core stacks by default. A distribution whose import name
    differs from its PyPI name (scikit-learn/sklearn) would false-positive
    here, which is why extra packages are installed idempotently rather
    than gated on this list.
    """

    interpreter: str
    exists: bool
    """Whether the interpreter ran at all (a missing file answers False)."""

    ready: bool
    """The kernel fast path: exists and ``ipykernel`` imports."""

    missing: tuple[str, ...]
    """The asked-about packages it cannot import, in the order asked."""


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """One probe subprocess's answer: its exit code and stdout.

    ``started=False`` is the ``FileNotFoundError`` family — the
    interpreter (or its parent) does not exist as a process at all."""

    exit_code: int
    stdout: str
    started: bool = True


ProbeRunner = Callable[[Sequence[str]], ProbeOutcome | None]
"""Spawns argv, waits, answers the outcome; ``None`` = could not run."""


def default_probe_runner(
    argv: Sequence[str],
    *,
    env_provider: Callable[[], dict[str, str]] | None = None,
) -> ProbeOutcome | None:
    scrubber = env_provider or without_control_credentials
    try:
        finished = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=DEFAULT_PROBE_TIMEOUT_S,
            env=scrubber(),
        )
    except (FileNotFoundError, PermissionError, OSError):
        return None
    return ProbeOutcome(finished.returncode, finished.stdout)


def interpreter_in_env(env_dir: str | Path, *, platform: str | None = None) -> str:
    """The interpreter path of a venv-layout environment directory.

    POSIX venvs keep it in ``bin/``, Windows ones in ``Scripts/`` — uv
    follows the same layout, so one rule covers both. The path is
    returned joined but not expanded: a ``~``-relative env dir stays
    ``~``-relative for the caller (a script, an sh line) that runs where
    the tilde means something.
    """
    root = str(env_dir).rstrip("/\\")
    if (platform or sys.platform).startswith("win"):
        return root + "\\Scripts\\python.exe"
    return root + "/bin/python"


def build_probe_argv(interpreter: str, packages: Sequence[str]) -> list[str]:
    """``<interpreter> -c <probe source> <names...>``. Pure."""
    return [interpreter, "-c", _PROBE_SOURCE, *packages]


def probe_interpreter(
    interpreter: str,
    *,
    packages: Sequence[str] = DEFAULT_PROBE_PACKAGES,
    runner: ProbeRunner | None = None,
) -> EnvProbe:
    """Probe one interpreter and answer the structured result.

    An interpreter that runs but answers nothing parseable (a broken
    stdlib, a hijacked ``sitecustomize``) counts as existing and not
    ready, with every asked-about package reported missing: the honest
    degradation is "cannot vouch", never "looks fine".
    """
    names = tuple(packages) or DEFAULT_PROBE_PACKAGES
    spawn = runner or default_probe_runner
    outcome = spawn(build_probe_argv(interpreter, names))
    if outcome is None or not outcome.started:
        return EnvProbe(interpreter, exists=False, ready=False, missing=names)
    found = _parse_probe_stdout(outcome.stdout)
    if found is None:
        return EnvProbe(interpreter, exists=True, ready=False, missing=names)
    missing = tuple(name for name in names if not found.get(name))
    return EnvProbe(
        interpreter,
        exists=True,
        ready=bool(found.get("ipykernel")),
        missing=missing,
    )


def _parse_probe_stdout(stdout: str) -> dict[str, bool] | None:
    """The JSON answer after the sentinel line, or ``None`` if absent.

    Only the region after the sentinel is parsed: an interpreter that
    prints warnings on startup (a conda activation banner, a
    ``UserWarning``) must not make the probe look unparseable.
    """
    marker = stdout.find(PROBE_SENTINEL)
    if marker < 0:
        return None
    tail = stdout[marker + len(PROBE_SENTINEL):].strip()
    if not tail:
        return None
    first, _, rest = tail.partition("\n")
    candidate = rest if rest.strip() else first
    try:
        parsed = json.loads(candidate)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    return {str(key): bool(value) for key, value in parsed.items()}
