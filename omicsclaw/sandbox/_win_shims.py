"""Windows AppContainer shims: PATH command guards, LowBox NUL/pipe shims, and CPython >=3.12.4 mkdtemp DACL patch.

Provides:
- Command shims at the front of ``PATH`` that refuse destructive or mutating
  arguments targeting ``.git/hooks``, ``.git/config``, ``.bashrc``, ``.Rprofile``,
  ``.omicsclaw``, etc. (defense-in-depth alongside NTFS ACLs).
- LowBox ``os.devnull`` redirect (``NUL`` -> ``<scratch>/.sink``) and Windows
  ``AF_PIPE`` namespace adjustment (``\\\\.\\pipe\\LOCAL\\``) inside LowBox tokens.
- CPython ``>= 3.12.4`` ``tempfile.mkdtemp(mode=0o700)`` restrictive DACL patch
  injected via ``sitecustomize.py`` inside ``PYTHONPATH`` so AppContainer child
  processes do not break ``pip`` / ``pytest`` / ``scipy`` / ``biopython``.

Pure standard-library module: no logging, no dynamic imports, and no direct
reads of the process environment or ``sys`` argument vector.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import DEFAULT_DENY_WRITE_GLOBS

__all__ = [
    "SHIMMED_COMMANDS",
    "apply_lowbox_devnull_and_pipe_patch",
    "apply_mkdtemp_dacl_patch",
    "build_win32_sitecustomize_source",
    "check_shim_invocation",
    "ensure_win_shim_dir",
]

SHIMMED_COMMANDS: tuple[str, ...] = (
    "chmod",
    "cp",
    "del",
    "erase",
    "git",
    "ln",
    "move",
    "mv",
    "rd",
    "ren",
    "rename",
    "rm",
    "rmdir",
)


def _normalize_rel(path_str: str, workspace: Path) -> str:
    try:
        candidate = Path(path_str)
        if not candidate.is_absolute():
            candidate = workspace / candidate
        rel = candidate.resolve().relative_to(workspace.resolve())
        return rel.as_posix().lower()
    except (OSError, ValueError):
        return path_str.replace("\\", "/").strip("/").lower()


def check_shim_invocation(
    command: str,
    args: Sequence[str],
    *,
    workspace: Path,
    protected_targets: Sequence[str] = DEFAULT_DENY_WRITE_GLOBS,
) -> tuple[bool, str]:
    """Check whether *command* with *args* attempts to mutate a protected path.

    Returns ``(allowed, reason_if_blocked)``.
    """
    cmd = command.lower().removesuffix(".exe").removesuffix(".cmd").removesuffix(".bat")
    protected_norm = tuple(
        item.replace("\\", "/").strip("/").lower()
        for item in protected_targets
        if item.strip()
    )

    if cmd == "git":
        lowered = [a.lower() for a in args]
        if "config" in lowered:
            for token in lowered:
                if "hookspath" in token or token in ("--local", "--replace-all", "--unset", "--add"):
                    return (
                        False,
                        "[Sandbox Protected Path] Modifying '.git/config' or git hooks via git config is blocked.",
                    )
            cfg_idx = lowered.index("config")
            non_flag = [a for a in lowered[cfg_idx + 1 :] if not a.startswith("-")]
            if len(non_flag) >= 2:
                return (
                    False,
                    "[Sandbox Protected Path] Writing to '.git/config' is blocked by sandbox policy.",
                )
        return True, ""

    for arg in args:
        if arg.startswith("-"):
            continue
        rel = _normalize_rel(arg, workspace)
        for prot in protected_norm:
            if rel == prot or rel.startswith(prot + "/"):
                return (
                    False,
                    f"[Sandbox Protected Path] Modification of '{prot}' is blocked by sandbox policy.",
                )
    return True, ""


def apply_lowbox_devnull_and_pipe_patch(sink_file: Path) -> dict[str, str]:
    """Configure LowBox ``os.devnull`` fallback and named-pipe ``AF_PIPE`` prefix.

    Inside a Windows AppContainer (LowBox) token, opening ``\\\\.\\NUL`` can fail
    with ``ERROR_ACCESS_DENIED`` on older builds, and named pipes must live
    under ``\\\\.\\pipe\\LOCAL\\``.
    """
    import multiprocessing.connection as _mp_conn

    sink_file.parent.mkdir(parents=True, exist_ok=True)
    if not sink_file.exists():
        sink_file.write_bytes(b"")
    sink_str = str(sink_file.resolve())
    if sys.platform == "win32":
        try:
            with open(os.devnull, "wb"):
                pass
        except OSError:
            os.devnull = sink_str

    orig_arb = getattr(_mp_conn, "_oc_orig_arbitrary_address", _mp_conn.arbitrary_address)
    setattr(_mp_conn, "_oc_orig_arbitrary_address", orig_arb)

    def _local_pipe_arbitrary_address(family: str) -> str:
        addr = orig_arb(family)
        if family == "AF_PIPE" and isinstance(addr, str):
            prefix = r"\\.\pipe\\"
            local_prefix = r"\\.\pipe\LOCAL\\"
            if addr.startswith(prefix) and not addr.startswith(local_prefix):
                return local_prefix + addr[len(prefix) :]
        return addr

    _mp_conn.arbitrary_address = _local_pipe_arbitrary_address
    return {
        "devnull": sink_str,
        "pipe_prefix": r"\\.\pipe\LOCAL\\",
    }


def apply_mkdtemp_dacl_patch(sid_string: str) -> None:
    """Patch ``tempfile.mkdtemp`` in-process so CPython >= 3.12.4 0o700 DACL grants *sid_string*."""
    import tempfile as _tempfile

    orig_mkdtemp = getattr(_tempfile, "_oc_orig_mkdtemp", _tempfile.mkdtemp)
    setattr(_tempfile, "_oc_orig_mkdtemp", orig_mkdtemp)

    def _patched_mkdtemp(
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | os.PathLike[str] | None = None,
    ) -> str:
        created = orig_mkdtemp(suffix=suffix, prefix=prefix, dir=dir)
        if sys.platform == "win32" and sid_string:
            try:
                subprocess.run(
                    ["icacls", created, "/grant", f"*{sid_string}:(OI)(CI)(F)", "/Q"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5.0,
                    check=False,
                )
            except Exception:  # noqa: BLE001
                pass
        return created

    _tempfile.mkdtemp = _patched_mkdtemp


def build_win32_sitecustomize_source(
    *,
    sid_string: str,
    sink_file: Path,
) -> str:
    """Render ``sitecustomize.py`` source for Windows AppContainer child Python processes."""
    sink_str = str(sink_file.resolve())
    return (
        "# Auto-generated by omicsclaw.sandbox._win_shims for Windows AppContainer.\n"
        "import multiprocessing.connection as _mp_conn\n"
        "import os, subprocess, sys, tempfile\n"
        f"_SINK = {sink_str!r}\n"
        f"_SID = {sid_string!r}\n"
        "if sys.platform == 'win32':\n"
        "    try:\n"
        "        with open(os.devnull, 'wb'):\n"
        "            pass\n"
        "    except OSError:\n"
        "        os.devnull = _SINK\n"
        "    _orig_arb = getattr(_mp_conn, '_oc_orig_arbitrary_address', _mp_conn.arbitrary_address)\n"
        "    _mp_conn._oc_orig_arbitrary_address = _orig_arb\n"
        "    def _oc_arbitrary_address(family):\n"
        "        addr = _orig_arb(family)\n"
        "        if family == 'AF_PIPE' and isinstance(addr, str):\n"
        "            if addr.startswith(r'\\\\.\\pipe\\') and not addr.startswith(r'\\\\.\\pipe\\LOCAL\\'):\n"
        "                return r'\\\\.\\pipe\\LOCAL\\' + addr[len(r'\\\\.\\pipe\\'):]\n"
        "        return addr\n"
        "    _mp_conn.arbitrary_address = _oc_arbitrary_address\n"
        "    _orig_mkdtemp = getattr(tempfile, '_oc_orig_mkdtemp', tempfile.mkdtemp)\n"
        "    tempfile._oc_orig_mkdtemp = _orig_mkdtemp\n"
        "    def _oc_mkdtemp(suffix=None, prefix=None, dir=None):\n"
        "        d = _orig_mkdtemp(suffix=suffix, prefix=prefix, dir=dir)\n"
        "        if _SID:\n"
        "            try:\n"
        "                subprocess.run(\n"
        "                    ['icacls', d, '/grant', f'*{_SID}:(OI)(CI)(F)', '/Q'],\n"
        "                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,\n"
        "                )\n"
        "            except Exception:\n"
        "                pass\n"
        "        return d\n"
        "    tempfile.mkdtemp = _oc_mkdtemp\n"
    )


def ensure_win_shim_dir(
    shim_dir: Path,
    *,
    workspace: Path,
    protected_targets: Sequence[str] = DEFAULT_DENY_WRITE_GLOBS,
    python_executable: str | None = None,
    sid_string: str = "S-1-15-2-1",
) -> Path:
    """Create ``.cmd`` wrappers and ``sitecustomize.py`` in *shim_dir*."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    py_exe = python_executable or sys.executable
    ws_str = str(workspace.resolve())
    prot_list = list(protected_targets)

    sink_file = shim_dir / ".sink"
    if not sink_file.exists():
        sink_file.write_bytes(b"")

    sitecustomize = shim_dir / "sitecustomize.py"
    sitecustomize.write_text(
        build_win32_sitecustomize_source(sid_string=sid_string, sink_file=sink_file),
        encoding="utf-8",
    )

    guard_script = shim_dir / "_omicsclaw_shim_guard.py"
    guard_script.write_text(
        "import argparse, os, shutil, subprocess, sys\n"
        "from pathlib import Path\n"
        f"WORKSPACE = Path({ws_str!r})\n"
        f"PROTECTED = {prot_list!r}\n"
        f"SHIM_DIR = str(Path({str(shim_dir.resolve())!r}))\n"
        "_p = argparse.ArgumentParser(add_help=False)\n"
        "_p.add_argument('cmd')\n"
        "_p.add_argument('args', nargs=argparse.REMAINDER)\n"
        "_ns = _p.parse_args()\n"
        "cmd, args = _ns.cmd, _ns.args\n"
        "for a in args:\n"
        "    if a.startswith('-'):\n"
        "        continue\n"
        "    try:\n"
        "        p = Path(a)\n"
        "        if not p.is_absolute():\n"
        "            p = WORKSPACE / p\n"
        "        rel = p.resolve().relative_to(WORKSPACE.resolve()).as_posix().lower()\n"
        "    except Exception:\n"
        "        rel = a.replace('\\\\', '/').strip('/').lower()\n"
        "    for prot in PROTECTED:\n"
        "        pn = prot.replace('\\\\', '/').strip('/').lower()\n"
        "        if rel == pn or rel.startswith(pn + '/'):\n"
        "            sys.stderr.write(f\"[Sandbox Protected Path] Modification of '{prot}' is blocked.\\n\")\n"
        "            raise SystemExit(126)\n"
        "real = shutil.which(cmd)\n"
        "if not real:\n"
        "    raise SystemExit(127)\n"
        "raise SystemExit(subprocess.call([real, *args]))\n",
        encoding="utf-8",
    )

    for cmd in SHIMMED_COMMANDS:
        cmd_file = shim_dir / f"{cmd}.cmd"
        cmd_file.write_text(
            f'@echo off\r\n"{py_exe}" -B "{guard_script}" {cmd} %*\r\n',
            encoding="utf-8",
        )
    return shim_dir
