"""Locate and probe the shipped micromamba — Tier 0's second installer.

Groundwork for the R / conda-forge provisioning a later phase plans
(the App ships micromamba beside uv, so the bundle carries neither a
Python nor an R). This module answers the two questions every future
consumer asks and nothing more:

* **which binary** — the same resolution ladder uv's install script
  uses: the ``OMICSCLAW_MICROMAMBA_BIN`` environment variable, then the
  App's shipped installers directory (``OMICSCLAW_INSTALLERS_DIR``),
  then ``PATH``;
* **does it run** — one ``--version`` invocation, whose first line
  carries the version.

No environment creation, no channel choices, no network: those are a
later phase's mechanism, and they will be built on this seam rather
than around it.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass

__all__ = [
    "MicromambaLocation",
    "build_probe_argv",
    "locate_micromamba",
    "parse_micromamba_version",
]

_VERSION_LINE = re.compile(r"micromamba\s+(\S+)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class MicromambaLocation:
    """One resolution's answer: the binary (or ``None``) and its rung."""

    path: str | None
    from_: str
    """``env`` | ``installers`` | ``path`` | ``none`` — which rung won."""


def locate_micromamba(
    *,
    env_get: Callable[[str], str | None] = os.environ.get,
    file_exists: Callable[[str], bool] = os.path.exists,
    which: Callable[[str], str | None] = shutil.which,
    platform: str = "",
) -> MicromambaLocation:
    """Resolve the micromamba binary. Pure; every effect is injected.

    *platform* only decides the binary's name on Windows (``micromamba.exe``);
    the caller passes ``sys.platform`` in production and a literal in tests.
    """
    binary = "micromamba.exe" if platform.startswith("win") else "micromamba"
    override = (env_get("OMICSCLAW_MICROMAMBA_BIN") or "").strip()
    if override:
        return MicromambaLocation(override, "env")
    installers = (env_get("OMICSCLAW_INSTALLERS_DIR") or "").strip()
    if installers:
        candidate = installers.rstrip("/\\") + "/" + binary
        if file_exists(candidate):
            return MicromambaLocation(candidate, "installers")
    found = which("micromamba")
    if found:
        return MicromambaLocation(found, "path")
    return MicromambaLocation(None, "none")


def build_probe_argv(binary: str) -> list[str]:
    """``<micromamba> --version`` — the one-command liveness probe."""
    return [binary, "--version"]


def parse_micromamba_version(output: str) -> str | None:
    """The version from a probe's output, or ``None`` when absent.

    Tolerant of a banner line before the version and of CRLF, because
    the probe may run through a shell the caller owns."""
    for line in output.splitlines():
        match = _VERSION_LINE.search(line.strip())
        if match:
            return match.group(1)
    return None


def probe_micromamba(
    location: MicromambaLocation,
    *,
    runner: Callable[[Sequence[str]], tuple[int, str]] | None = None,
    env_provider: Callable[[], dict[str, str]] | None = None,
) -> str | None:
    """Probe the located binary and answer its version, or ``None``.

    A missing binary is ``None`` (there is nothing to ask); a binary
    that answers garbage is ``None`` too — the honest degradation the
    package's uv probe set. The runner injects the subprocess."""
    if location.path is None:
        return None
    if runner is None:
        import subprocess
        from .pins import without_control_credentials

        scrubber = env_provider or without_control_credentials

        def runner(argv: Sequence[str]) -> tuple[int, str]:  # noqa: F811
            finished = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                timeout=30.0,
                env=scrubber(),
            )
            return finished.returncode, finished.stdout

    code, output = runner(build_probe_argv(location.path))
    if code != 0:
        return None
    return parse_micromamba_version(output)
