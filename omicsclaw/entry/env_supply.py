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
from collections import deque
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

__all__ = ["EnvSupplyBinding", "PinnedEnvironmentSupply", "build_env_supply"]

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
        resolved = tuple(KERNEL_BASE_PACKAGES) + CORE_STACK_PACKAGES + tuple(packages)
        script = build_env_install_script(
            env_dir,
            packages=tuple(packages),
            uv_index_url=self._index_url,
            uv_python_mirror=self._python_mirror,
        )
        lines: deque[str] = deque(maxlen=12)

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
                f"{''.join(line + chr(10) for line in lines)}",
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
        env_root=str(getattr(config, "env_supply_env_root", None) or DEFAULT_ENV_ROOT),
        index_url=getattr(config, "env_supply_index_url", None) or DEFAULT_UV_INDEX_URL,
        python_mirror=getattr(config, "env_supply_python_mirror", None)
        or DEFAULT_UV_PYTHON_MIRROR,
        install_timeout_s=float(
            getattr(config, "env_supply_install_timeout_s", None)
            or ENV_INSTALL_TIMEOUT_S
        ),
    )


class PinnedEnvironmentSupply:
    """Original supply runner with owned locks and verified atomic publication."""

    def __init__(
        self,
        root: Path,
        *,
        uv: Path | None = None,
        wheelhouse: Path | None = None,
        timeout_s: float = ENV_INSTALL_TIMEOUT_S,
    ):
        self.root = Path(root).absolute()
        self.uv = Path(uv).absolute() if uv else None
        self.wheelhouse = Path(wheelhouse).absolute() if wheelhouse else None
        self.timeout_s = float(timeout_s)

    async def supply(self, manifest, *, lock: Path, interpreter: Path):
        return await asyncio.to_thread(
            self._supply, manifest, Path(lock), Path(interpreter)
        )

    def _supply(self, manifest, lock, interpreter):
        import fcntl
        import hashlib
        import json
        import os
        import re
        import shlex
        import shutil
        import tempfile
        import time

        allowed = {
            "schemaVersion",
            "role",
            "pythonVersion",
            "platform",
            "machine",
            "abi",
            "lockSha256",
            "imports",
        }
        if (
            not isinstance(manifest, dict)
            or set(manifest) != allowed
            or manifest["schemaVersion"] != 1
            or manifest["role"] not in {"control", "science"}
        ):
            raise ValueError("invalid pinned environment manifest")
        if (
            not isinstance(manifest["imports"], list)
            or len(manifest["imports"]) > 200
            or any(
                not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,127}", name)
                for name in manifest["imports"]
            )
        ):
            raise ValueError("invalid environment imports")
        if manifest["role"] == "control" and set(manifest["imports"]) - {
            "openai",
            "anthropic",
            "httpx",
            "yaml",
            "jupyter_client",
            "zmq",
            "tornado",
        }:
            raise ValueError("scientific import in control manifest")
        if not interpreter.is_absolute() or not interpreter.is_file():
            raise ValueError("selected interpreter unavailable")
        offline = ""
        if self.wheelhouse is not None:
            import stat

            cache = self.wheelhouse
            if not cache.is_dir() or any(
                part.is_symlink() for part in (cache, *cache.parents)
            ):
                raise ValueError("wheelhouse directory unavailable or symlinked")
            observed = cache.stat()
            if observed.st_uid != os.getuid() or observed.st_mode & 0o022:
                raise ValueError("wheelhouse ownership mismatch")
            wheels = list(cache.glob("*.whl"))
            if len(wheels) > 100:
                raise ValueError("wheelhouse file budget")
            total = 0
            for wheel in wheels:
                item = wheel.lstat()
                if (
                    not stat.S_ISREG(item.st_mode)
                    or item.st_uid != os.getuid()
                    or item.st_size > 256 * 1024 * 1024
                ):
                    raise ValueError("wheelhouse archive boundary")
                total += item.st_size
            if total > 1024 * 1024 * 1024:
                raise ValueError("wheelhouse archive budget")
            offline = " --no-index --find-links " + shlex.quote(str(cache))
        with lock.open("rb") as source:
            lock_bytes = source.read(1024 * 1024 + 1)
        if (
            len(lock_bytes) > 1024 * 1024
            or hashlib.sha256(lock_bytes).hexdigest() != manifest["lockSha256"]
        ):
            raise ValueError("environment lock mismatch")
        raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        if len(raw) > 64 * 1024:
            raise ValueError("environment manifest budget")
        digest = hashlib.sha256(raw).hexdigest()
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        root_stat = self.root.stat()
        if (
            self.root.is_symlink()
            or root_stat.st_uid != os.getuid()
            or root_stat.st_mode & 0o022
        ):
            raise ValueError("environment root ownership mismatch")
        final = self.root / digest
        identity = (root_stat.st_dev, root_stat.st_ino, root_stat.st_uid)
        descriptor = os.open(
            self.root / (digest + ".lock"),
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            deadline = time.monotonic() + min(self.timeout_s, 60)
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("environment supply lock deadline")
                    time.sleep(0.05)
            expected = {"environmentDigest": digest, "manifest": manifest}
            if final.exists():
                ready_file = final / "ready.json"
                if final.is_symlink() or final.stat().st_uid != os.getuid():
                    raise ValueError("environment ready evidence conflict")
                if not ready_file.exists():
                    publication = final / "manifest.json"
                    if (
                        publication.is_symlink()
                        or not publication.is_file()
                        or publication.stat().st_size > 64 * 1024
                        or publication.read_bytes() != raw
                    ):
                        raise ValueError("environment incomplete evidence conflict")
                    self._verify(final / "venv/bin/python", manifest)
                    ready_temporary = final / "ready.json.tmp"
                    with ready_temporary.open("x") as sink:
                        sink.write(json.dumps(expected, sort_keys=True))
                        sink.flush()
                        os.fsync(sink.fileno())
                    os.replace(ready_temporary, ready_file)
                if (
                    not (final / "ready.json").is_file()
                    or (final / "ready.json").is_symlink()
                    or (final / "ready.json").stat().st_size > 64 * 1024
                    or json.loads((final / "ready.json").read_text()) != expected
                ):
                    raise ValueError("environment ready evidence conflict")
                self._verify(final / "venv/bin/python", manifest)
                return {
                    "root": str(final),
                    "python": str(final / "venv/bin/python"),
                    "environmentDigest": digest,
                    "ready": True,
                    "reused": True,
                }
            temporary = Path(tempfile.mkdtemp(prefix=".environment-", dir=self.root))
            try:
                venv = temporary / "venv"
                staged_lock = temporary / "requirements.lock"
                staged_lock.write_bytes(lock_bytes)
                script = f"set -eu\numask 077\n{shlex.quote(str(interpreter))} -I -m venv --without-pip {shlex.quote(str(venv))}\n"
                if lock_bytes.strip():
                    if self.uv is not None:
                        if not self.uv.is_file():
                            raise ValueError("pinned environment installer unavailable")
                        script += f"{shlex.quote(str(self.uv))} pip install --python {shlex.quote(str(venv / 'bin/python'))} --require-hashes --no-cache --link-mode copy{offline} -r {shlex.quote(str(staged_lock))}\n"
                    else:
                        script += f"{shlex.quote(str(interpreter))} -I -m pip --isolated --python {shlex.quote(str(venv))} install --require-hashes --no-compile --no-cache-dir --disable-pip-version-check --only-binary=:all:{offline} -r {shlex.quote(str(staged_lock))}\n"
                (temporary / "home").mkdir()
                (temporary / "tmp").mkdir()
                script = (
                    "exec env -i PATH=/usr/bin:/bin "
                    + shlex.quote("HOME=" + str(temporary / "home"))
                    + " "
                    + shlex.quote("TMPDIR=" + str(temporary / "tmp"))
                    + " /bin/sh -c "
                    + shlex.quote(script)
                )
                code = run_install_script(script, timeout=self.timeout_s)
                if code:
                    raise ValueError(f"pinned environment install failed: {code}")
                self._verify(venv / "bin/python", manifest)
                # Published command scripts must not point at the temporary venv.
                for script_file in (venv / "bin").iterdir():
                    if (
                        script_file.is_symlink()
                        or not script_file.is_file()
                        or script_file.stat().st_size > 1024 * 1024
                    ):
                        continue
                    with script_file.open("rb") as reader:
                        first = reader.readline(4096)
                    if first.startswith(b"#!") and str(venv).encode() in first:
                        content = script_file.read_bytes().replace(
                            str(venv).encode(), str(final / "venv").encode()
                        )
                        script_file.write_bytes(content)
                (temporary / "manifest.json").write_bytes(raw)
                observed = self.root.stat()
                if (observed.st_dev, observed.st_ino, observed.st_uid) != identity:
                    raise ValueError("environment root identity changed")
                os.rename(temporary, final)
                self._verify(final / "venv/bin/python", manifest)
                ready = final / "ready.json.tmp"
                ready.write_text(json.dumps(expected, sort_keys=True))
                os.replace(ready, final / "ready.json")
                return {
                    "root": str(final),
                    "python": str(final / "venv/bin/python"),
                    "environmentDigest": digest,
                    "ready": True,
                    "reused": False,
                }
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        finally:
            os.close(descriptor)

    @staticmethod
    def _verify(python, manifest):
        import json
        import subprocess

        probe = (
            "import json,platform,sys,sysconfig,importlib; names="
            + repr(manifest["imports"])
            + "; [importlib.import_module(n) for n in names]; print(json.dumps({'pythonVersion':platform.python_version(),'platform':sys.platform,'machine':platform.machine(),'abi':sysconfig.get_config_var('SOABI')}))"
        )
        import os

        private = python.parent.parent.parent
        if (
            not python.is_absolute()
            or python.parent.name != "bin"
            or python.parent.parent.name != "venv"
        ):
            raise ValueError("environment verification prefix mismatch")
        for name in ("home", "tmp"):
            directory = private / name
            if directory.is_symlink():
                raise ValueError("environment verification directory boundary")
            directory.mkdir(mode=0o700, exist_ok=True)
            if (
                not directory.is_dir()
                or directory.stat().st_uid != os.getuid()
                or directory.stat().st_mode & 0o022
            ):
                raise ValueError("environment verification directory ownership")
        result = subprocess.run(
            [str(python), "-I", "-B", "-c", probe],
            env={
                "PATH": "/usr/bin:/bin",
                "PYTHONNOUSERSITE": "1",
                "HOME": str(private / "home"),
                "TMPDIR": str(private / "tmp"),
                "MPLCONFIGDIR": str(private / "home/matplotlib"),
            },
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode or len(result.stdout.encode()) > 4096:
            raise ValueError("environment ABI/import verification failed")
        facts = json.loads(result.stdout)
        if any(
            facts.get(key) != manifest[key]
            for key in ("pythonVersion", "platform", "machine", "abi")
        ):
            raise ValueError("environment ABI mismatch")
