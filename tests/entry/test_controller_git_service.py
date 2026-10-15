import asyncio
import subprocess
import pytest


def test_stdio_git_adapter_returns_the_remote_git_result(tmp_path):
    from types import SimpleNamespace
    from omicsclaw.entry.desktop.service import DesktopService

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    service = DesktopService(
        SimpleNamespace(config=SimpleNamespace(workspace=tmp_path)), None, None
    )
    result = asyncio.run(
        service.dispatch(
            {
                "method": "POST",
                "path": "/git/execute",
                "document": {
                    "args": ["status", "--porcelain=v2"],
                    "cwd": str(tmp_path),
                    "timeoutMs": 5000,
                },
            }
        )
    )
    assert result == {
        "status": 200,
        "payload": {"exitCode": 0, "stdout": "", "stderr": ""},
    }


def test_git_transport_executes_in_remote_workspace_and_refuses_escape(tmp_path):
    from omicsclaw.entry.desktop.git_service import execute_git

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)

    async def run():
        result = await execute_git(
            tmp_path,
            {
                "args": ["status", "--porcelain=v2"],
                "cwd": str(tmp_path),
                "timeoutMs": 5000,
            },
        )
        assert result == {"exitCode": 0, "stdout": "", "stderr": ""}
        with pytest.raises(Exception):
            await execute_git(
                tmp_path,
                {"args": ["status"], "cwd": str(tmp_path.parent), "timeoutMs": 5000},
            )
        with pytest.raises(Exception):
            await execute_git(
                tmp_path,
                {
                    "args": ["-c", "alias.x=!touch unsafe", "x"],
                    "cwd": str(tmp_path),
                    "timeoutMs": 5000,
                },
            )
        with pytest.raises(Exception):
            await execute_git(
                tmp_path,
                {
                    "args": [
                        "worktree",
                        "add",
                        "-b",
                        "new",
                        str(tmp_path.parent / "escape"),
                    ],
                    "cwd": str(tmp_path),
                    "timeoutMs": 5000,
                },
            )

    asyncio.run(run())
