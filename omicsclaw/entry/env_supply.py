"""The composition root's binding over the environment-supply mechanism.

The ``WorkspaceKernelBinding`` pattern, applied to P5: the
``manage_environments`` tool speaks the
:class:`~omicsclaw.tools.builtin.manage_env.EnvironmentSupply` Protocol
and this binding answers it on the ``envsupply`` package — probe one
interpreter, build and run the idempotent install script, list what
exists under the env root. One binding per deployment, shared by the
tool (chat face) and the ``env_install`` jobs runner, so both faces
provision the same directories with the same mirrors.

The blocking halves run under :func:`asyncio.to_thread` — a probe is a
subprocess and an install is minutes of one — and install output lines
reach the caller's ``on_output`` from that worker thread, which is the
contract the jobs runner's event emitters already accept.

Policy stays out: the mirrors, env root and timeouts arrive as
configuration (:func:`build_env_supply` reads them from
:class:`~omicsclaw.entry.config.AppConfig`), the approval happens in
the tool's gate, and a job is pre-consented by the person who posted
it. Nothing here asks anyone anything.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from omicsclaw.envsupply import (
    CORE_STACK_PACKAGES,
    DEFAULT_UV_INDEX_URL,
    DEFAULT_UV_PYTHON_MIRROR,
    ENV_INSTALL_TIMEOUT_S,
    KERNEL_BASE_PACKAGES,
    EnvSupplyError,
    build_env_install_script,
    interpreter_in_env,
    manual_env_commands,
    probe_interpreter,
    run_install_script,
)

__all__ = ["EnvSupplyBinding", "build_env_supply"]

DEFAULT_ENV_ROOT = "~/.omicsclaw/envs"


class EnvSupplyBinding:
    """The EnvironmentSupply Protocol over one deployment's configuration.

    *env_root* is a ``~``-relative or absolute directory; named
    environments are ``<root>/<name>`` and the Tier-1 default is
    ``main`` (``~/.omicsclaw/envs/main``). The probe and install
    runners are injectable seams so tests drive the whole binding
    without a process.
    """

    def __init__(
        self,
        *,
        env_root: str = DEFAULT_ENV_ROOT,
        index_url: str | None = DEFAULT_UV_INDEX_URL,
        python_mirror: str | None = DEFAULT_UV_PYTHON_MIRROR,
        install_timeout_s: float = ENV_INSTALL_TIMEOUT_S,
        probe_runner: Any = None,
        install_runner: Any = None,
    ) -> None:
        root = str(env_root).rstrip("/") or DEFAULT_ENV_ROOT
        # Expanded once, here: a "~"-relative root (the default) must
        # reach the filesystem as the real home for every probe, verify
        # and install. Only list_environments used to expand it, which
        # turned a healthy default-config install into "cannot import
        # ipykernel" (review B1). The install script's own "$HOME"
        # splice is independent of this and unchanged.
        self._env_root = str(Path(root).expanduser())
        self._index_url = index_url
        self._python_mirror = python_mirror
        self._install_timeout_s = float(install_timeout_s)
        self._probe_runner = probe_runner
        self._install_runner = install_runner

    # ---- the Protocol ----

    def env_dir(self, env: str) -> str:
        """The ``~``-relative directory of one named environment."""
        return self._env_root + "/" + env

    async def list_environments(self) -> list[dict[str, Any]]:
        """The named environments under the root, each probed.

        Directory presence decides *listing*; one probe per listed env
        decides *readiness* — the same ``exists``/``ready``/``missing``
        shape :meth:`probe_environment` answers, so the tool's rendering
        and the model's reading see one vocabulary (review n6). Probes
        run sequentially under ``to_thread``: a ``list`` is an explicit
        action, not a hot path, and an unprobed "ready" would be the
        lie the probe layer exists to prevent."""
        root = Path(self._env_root)
        found: list[dict[str, Any]] = []
        if not root.is_dir():
            return found
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            python = Path(interpreter_in_env(entry))
            present = python.exists()
            record: dict[str, Any] = {
                "name": entry.name,
                "env_dir": str(entry),
                "interpreter": str(python),
                "exists": present,
                "ready": False,
                "missing": [],
            }
            if present:
                probe = await asyncio.to_thread(
                    probe_interpreter, str(python), runner=self._probe_runner
                )
                record["ready"] = probe.ready
                record["missing"] = list(probe.missing)
            found.append(record)
        return found

    async def probe_environment(
        self, *, env: str = "main", interpreter: str | None = None
    ) -> dict[str, Any]:
        """Probe a named environment or an explicit interpreter.

        An explicit interpreter is inspected and never installed into —
        the same trust statement the remote plane's ``python=`` pin
        makes about a machine somebody already curates."""
        target = interpreter or interpreter_in_env(self.env_dir(env))
        probe = await asyncio.to_thread(
            probe_interpreter, target, runner=self._probe_runner
        )
        return {
            "env": None if interpreter else env,
            "interpreter": probe.interpreter,
            "exists": probe.exists,
            "ready": probe.ready,
            "missing": list(probe.missing),
        }

    async def install_environment(
        self,
        *,
        env: str = "main",
        packages: Sequence[str] = (),
        on_output: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Install (or finish installing) one named environment.

        Idempotent by the script's construction; answers the summary the
        faces render, or raises :exc:`EnvSupplyError` carrying the
        manual commands and the installer's tail — the disclosure rule
        the remote plane set."""
        env_dir = self.env_dir(env)
        resolved = (
            tuple(KERNEL_BASE_PACKAGES) + CORE_STACK_PACKAGES + tuple(packages)
        )
        script = build_env_install_script(
            env_dir,
            packages=tuple(packages),
            uv_index_url=self._index_url,
            uv_python_mirror=self._python_mirror,
        )
        lines: list[str] = []

        def _collect(line: str) -> None:
            lines.append(line)
            if on_output is not None:
                on_output(line)

        exit_code = await asyncio.to_thread(
            run_install_script,
            script,
            runner=self._install_runner,
            on_line=_collect,
            timeout=self._install_timeout_s,
        )
        if exit_code != 0:
            raise EnvSupplyError(
                "installing the environment at "
                f"{env_dir} failed (exit {exit_code}): "
                f"{''.join(line + chr(10) for line in lines[-12:])}",
                manual_env_commands(
                    env_dir,
                    uv_index_url=self._index_url,
                    uv_python_mirror=self._python_mirror,
                ),
            )
        python = interpreter_in_env(env_dir)
        verify = await asyncio.to_thread(
            probe_interpreter, python, runner=self._probe_runner
        )
        if not verify.ready:
            raise EnvSupplyError(
                f"the environment at {env_dir} installed but its python"
                " cannot import ipykernel",
                manual_env_commands(
                    env_dir,
                    uv_index_url=self._index_url,
                    uv_python_mirror=self._python_mirror,
                ),
            )
        return {
            "env": env,
            "env_dir": env_dir,
            "interpreter": python,
            "verified": True,
            "packages": list(resolved),
        }


def build_env_supply(config: Any) -> EnvSupplyBinding:
    """One binding from a resolved :class:`AppConfig`.

    The single place the mirror URLs enter the system: configuration
    read at start-up, never an argument a model sent. ``OMICSCLAW_UV_BIN``
    and ``OMICSCLAW_UV_INSTALLERS_DIR`` are deliberately *not* read here
    — the script itself resolves them at run time so the App can point
    at its shipped installer without reconfiguring the backend.
    """
    return EnvSupplyBinding(
        env_root=str(
            getattr(config, "env_supply_env_root", None) or DEFAULT_ENV_ROOT
        ),
        index_url=getattr(config, "env_supply_index_url", None)
        or DEFAULT_UV_INDEX_URL,
        python_mirror=getattr(config, "env_supply_python_mirror", None)
        or DEFAULT_UV_PYTHON_MIRROR,
        install_timeout_s=float(
            getattr(config, "env_supply_install_timeout_s", None)
            or ENV_INSTALL_TIMEOUT_S
        ),
    )
