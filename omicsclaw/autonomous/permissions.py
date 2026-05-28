"""Conservative permission classification for autonomous runner commands."""

from __future__ import annotations

from pathlib import Path
import shlex

from .contracts import PermissionTier


_READ_ONLY_COMMANDS = {"ls", "head", "grep", "sed"}
_WRITE_MARKERS = {">", ">>", "2>", "2>>", "&>", "|", "tee"}
_SYSTEM_MUTATION_COMMANDS = {
    "apt",
    "apt-get",
    "brew",
    "conda",
    "curl",
    "dnf",
    "docker",
    "git",
    "pip",
    "pip3",
    "service",
    "sudo",
    "systemctl",
    "wget",
    "yum",
}
_PYTHON_INLINE_MUTATION_MARKERS = (
    "__import__",
    "chmod",
    "chown",
    "eval(",
    "exec(",
    "httpx",
    "mkdir",
    "open(",
    "os.remove",
    "os.rename",
    "os.replace",
    "os.rmdir",
    "os.system",
    "pathlib",
    "pip",
    "remove(",
    "rename(",
    "replace(",
    "requests",
    "rmdir",
    "shutil",
    "socket",
    "subprocess",
    "to_csv",
    "to_excel",
    "unlink",
    "wget",
    "write(",
    "write_text",
)


def classify_command(command: str | list[str], *, workspace_root: str | Path | None = None) -> PermissionTier:
    """Classify a command into a coarse autonomous permission tier.

    The classifier intentionally prefers false positives for ``system_mutation``
    over accidentally allowing commands with broad system effects.
    """
    argv = _coerce_argv(command)
    if not argv:
        return PermissionTier.SYSTEM_MUTATION

    lowered = [item.lower() for item in argv]
    executable = Path(lowered[0]).name
    command_text = " ".join(lowered)

    if _has_system_mutation_marker(argv, lowered, executable, command_text):
        return PermissionTier.SYSTEM_MUTATION

    if _writes_outside_workspace(argv, workspace_root):
        return PermissionTier.SYSTEM_MUTATION

    if _python_inline_requires_system_mutation(argv, executable):
        return PermissionTier.SYSTEM_MUTATION

    if _is_python_probe(argv, executable):
        return PermissionTier.READ_ONLY_PROBE

    if (
        executable in _READ_ONLY_COMMANDS
        and not any(token in _WRITE_MARKERS for token in argv)
        and _read_paths_are_inside_workspace(argv, workspace_root)
    ):
        return PermissionTier.READ_ONLY_PROBE

    if _is_analysis_interpreter(executable):
        if _script_argument_is_outside_workspace(argv, workspace_root):
            return PermissionTier.SYSTEM_MUTATION
        return PermissionTier.ANALYSIS_WRITE

    return PermissionTier.SYSTEM_MUTATION


def _coerce_argv(command: str | list[str]) -> list[str]:
    if isinstance(command, str):
        try:
            return shlex.split(command)
        except ValueError:
            return []
    return [str(item) for item in command]


def _has_system_mutation_marker(
    argv: list[str],
    lowered: list[str],
    executable: str,
    command_text: str,
) -> bool:
    if executable in _SYSTEM_MUTATION_COMMANDS:
        if executable == "git" and len(lowered) >= 2 and lowered[1] != "clone":
            return True
        return True
    if "git clone" in command_text or "rm -rf" in command_text or "rm -fr" in command_text:
        return True
    if executable == "sed" and "-i" in argv:
        return True
    return any(token in _WRITE_MARKERS for token in argv)


def _is_python_probe(argv: list[str], executable: str) -> bool:
    if not (_is_python_executable(executable) and len(argv) >= 2 and argv[1] == "-c"):
        return False
    code = argv[2].lower() if len(argv) >= 3 else ""
    return not any(marker in code for marker in _PYTHON_INLINE_MUTATION_MARKERS)


def _python_inline_requires_system_mutation(argv: list[str], executable: str) -> bool:
    if not (_is_python_executable(executable) and len(argv) >= 2 and argv[1] == "-c"):
        return False
    code = argv[2].lower() if len(argv) >= 3 else ""
    return any(marker in code for marker in _PYTHON_INLINE_MUTATION_MARKERS)


def _is_analysis_interpreter(executable: str) -> bool:
    return _is_python_executable(executable) or executable == "rscript"


def _is_python_executable(executable: str) -> bool:
    return executable == "python" or executable.startswith("python3")


def _script_argument_is_outside_workspace(
    argv: list[str],
    workspace_root: str | Path | None,
) -> bool:
    if workspace_root is None or len(argv) < 2:
        return False
    script = argv[1]
    if script.startswith("-"):
        return False
    return not _path_is_inside(script, Path(workspace_root).resolve())


def _writes_outside_workspace(
    argv: list[str],
    workspace_root: str | Path | None,
) -> bool:
    if workspace_root is None:
        return False

    root = Path(workspace_root).resolve()
    for index, token in enumerate(argv):
        if token in {"-o", "--output", "--output-dir", "--out", "--outdir"} and index + 1 < len(argv):
            if not _path_is_inside(argv[index + 1], root):
                return True
        if token.startswith("--output=") and not _path_is_inside(token.split("=", 1)[1], root):
            return True
    return False


def _path_is_inside(value: str, root: Path) -> bool:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    try:
        path.resolve().relative_to(root)
    except ValueError:
        return False
    return True


def _read_paths_are_inside_workspace(
    argv: list[str],
    workspace_root: str | Path | None,
) -> bool:
    if workspace_root is None:
        return False
    root = Path(workspace_root).resolve()
    for token in argv[1:]:
        if not token or token.startswith("-"):
            continue
        # sed expressions and grep patterns are not paths.
        if "/" not in token and "\\" not in token and not token.startswith("."):
            continue
        if not _path_is_inside(token, root):
            return False
    return True
