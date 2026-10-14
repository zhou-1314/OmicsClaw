"""Execute bounded Git commands for the existing Desktop TypeScript service."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

from omicsclaw.mcp.stdio import child_environment

from .turn_submission import DesktopIngressError

COMMANDS = frozenset(
    {
        "rev-parse",
        "rev-list",
        "status",
        "branch",
        "checkout",
        "log",
        "add",
        "commit",
        "push",
        "show",
        "worktree",
    }
)


async def execute_git(workspace, document):
    if not isinstance(document, dict) or set(document) != {"args", "cwd", "timeoutMs"}:
        raise DesktopIngressError("invalid_git_request")
    args, cwd, timeout = document["args"], document["cwd"], document["timeoutMs"]
    if (
        not isinstance(args, list)
        or not 1 <= len(args) <= 128
        or any(not isinstance(arg, str) or "\0" in arg for arg in args)
        or sum(len(arg.encode()) for arg in args) > 65536
        or args[0] not in COMMANDS
    ):
        raise DesktopIngressError("invalid_git_arguments")
    if (
        not isinstance(cwd, str)
        or not Path(cwd).is_absolute()
        or type(timeout) is not int
        or not 1 <= timeout <= 30000
    ):
        raise DesktopIngressError("invalid_git_scope")
    root, target = Path(workspace).resolve(), Path(cwd).resolve()
    if not target.is_relative_to(root):
        raise DesktopIngressError("git_workspace_mismatch", status_code=409)
    # Worktree destinations are separately validated; Git would otherwise accept
    # an absolute path outside the controller's workspace.
    if args[:2] == ["worktree", "add"]:
        destination = Path(args[-1])
        if not destination.is_absolute() or not destination.resolve().is_relative_to(
            root
        ):
            raise DesktopIngressError(
                "git_worktree_requires_target_publication", status_code=409
            )
    env = child_environment({})
    env["GIT_TERMINAL_PROMPT"] = "0"
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=target,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    size = 0

    async def read(stream):
        nonlocal size
        parts = []
        while chunk := await stream.read(16384):
            size += len(chunk)
            if size > 524288:
                raise DesktopIngressError(
                    "git_result_requires_data_channel", status_code=413
                )
            parts.append(chunk)
        return b"".join(parts).decode("utf-8", errors="replace")

    try:
        async with asyncio.timeout(timeout / 1000):
            stdout, stderr = await asyncio.gather(
                read(process.stdout), read(process.stderr)
            )
            code = await process.wait()
        return {"exitCode": code, "stdout": stdout, "stderr": stderr}
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
