"""Real-process Linux native sandbox integration tests (``bwrap`` / ``netns`` + ``EgressProxy``)."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest

from omicsclaw.sandbox import (
    EgressMode,
    SandboxConfig,
    SandboxManager,
    probe_sandbox_backend,
)

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not probe_sandbox_backend().available,
    reason="Linux native sandbox (bwrap or unshare netns) required",
)


def test_real_linux_sandbox_executes_commands_and_bridges_egress(tmp_path: Path):
    async def run() -> None:
        # Start a local HTTP server on host 127.0.0.1 to act as an approved endpoint
        async def handle_http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            _ = await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 15\r\nConnection: close\r\n\r\nsandbox-bridged"
            )
            await writer.drain()
            writer.close()

        srv = await asyncio.start_server(handle_http, "127.0.0.1", 0)
        srv_port = srv.sockets[0].getsockname()[1]

        ws = tmp_path / "ws"
        ws.mkdir()
        cfg = SandboxConfig(
            egress=EgressMode.PROXY,
            egress_endpoints=(f"127.0.0.1:{srv_port}",),
        )
        manager = SandboxManager(
            ws,
            config=cfg,
            env_provider=lambda: {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": str(ws),
                "NCBI_API_KEY": "ncbi-kept-123",
            },
        )
        await manager.start()
        try:
            status = manager.status()
            assert status.active
            assert status.egress == EgressMode.PROXY

            # 1. Basic command execution and environment variable preservation
            res = await manager.run_bash(
                f'echo "key=$NCBI_API_KEY"; {sys.executable} -c "print(6*7)"',
                cwd=str(ws),
                timeout=15.0,
            )
            assert res.exit_code == 0, res.output
            assert "key=ncbi-kept-123" in res.output
            assert "42" in res.output

            # 2. Direct arbitrary external socket inside isolated netns fails (Network is unreachable)
            direct = await manager.run_bash(
                f"{sys.executable} -c \""
                "import socket, sys\n"
                "s = socket.socket()\n"
                "s.settimeout(2.0)\n"
                "try:\n"
                "    s.connect(('93.184.216.34', 80))\n"
                "    sys.exit(0)\n"
                "except OSError as e:\n"
                "    print('direct-blocked:', e)\n"
                "    sys.exit(7)\n"
                "\"",
                cwd=str(ws),
                timeout=15.0,
            )
            assert direct.exit_code == 7
            assert "direct-blocked:" in direct.output

            # 3. Unapproved domain through HTTP_PROXY is blocked with 403 and hint is appended
            blocked = await manager.run_bash(
                f"{sys.executable} -c \""
                "import urllib.request, sys\n"
                "try:\n"
                "    urllib.request.urlopen('http://unapproved.example.com/', timeout=5)\n"
                "except Exception as e:\n"
                "    print('proxy-err:', e)\n"
                "    sys.exit(3)\n"
                "\"",
                cwd=str(ws),
                timeout=15.0,
            )
            assert blocked.exit_code == 3
            assert "[Sandbox Egress Blocked]" in blocked.output
            assert "unapproved.example.com" in blocked.output

            # 4. Approved endpoint through HTTP_PROXY succeeds across the UDS bridge
            allowed = await manager.run_bash(
                f"{sys.executable} -c \""
                "import os, urllib.request\n"
                "os.environ.pop('NO_PROXY', None)\n"
                "os.environ.pop('no_proxy', None)\n"
                "proxy = os.environ['HTTP_PROXY']\n"
                "opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http': proxy}))\n"
                f"resp = opener.open('http://127.0.0.1:{srv_port}/hello', timeout=5)\n"
                "print(resp.read().decode())\n"
                "\"",
                cwd=str(ws),
                timeout=15.0,
            )
            assert allowed.exit_code == 0, allowed.output
            assert "sandbox-bridged" in allowed.output

            # 5. Timeout kills the sandboxed process group cleanly
            timed = await manager.run_bash("sleep 30", cwd=str(ws), timeout=0.5)
            assert timed.timed_out
        finally:
            await manager.aclose()
            srv.close()
            await srv.wait_closed()

    asyncio.run(asyncio.wait_for(run(), timeout=30.0))



def test_real_linux_sandbox_python_and_r_session_kernels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import os
    import threading
    from omicsclaw.kernel.session import SessionKernel, _descendant_pids, _r_spec_manager
    from omicsclaw.tools.builtin.bash import without_control_credentials

    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-must-not-leak")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-secret-must-not-leak")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-agent.sock")
    monkeypatch.setenv("NCBI_API_KEY", "ncbi-allowed-key")

    async def run() -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        cfg = SandboxConfig(egress=EgressMode.PROXY)
        manager = SandboxManager(
            ws,
            config=cfg,
            env_provider=without_control_credentials,
        )
        await manager.start()
        try:
            wrapper = manager.spawn_wrapper()
            assert wrapper is not None
            assert wrapper.kernel_transport == "ipc"

            # --- Python SessionKernel ---
            py_kernel = SessionKernel(
                "py-sbx",
                workspace=ws,
                language="python",
                spawn_wrapper=wrapper,
                env_scrubber=without_control_credentials,
                startup_timeout=30.0,
            )
            py_kernel.start()
            tree_pids = _descendant_pids(py_kernel.pid)
            try:
                cell = py_kernel.execute(
                    "import os, socket\n"
                    "for k in ('OPENAI_API_KEY', 'TELEGRAM_BOT_TOKEN', 'SSH_AUTH_SOCK', 'NCBI_API_KEY'):\n"
                    "    print(f'{k}={os.environ.get(k)}')\n"
                    "s = socket.socket()\n"
                    "s.settimeout(1.5)\n"
                    "try:\n"
                    "    s.connect(('93.184.216.34', 80))\n"
                    "    print('NET=OPEN')\n"
                    "except OSError:\n"
                    "    print('NET=BLOCKED')\n",
                    timeout_s=15.0,
                )
                assert cell.status == "ok", cell.error
                assert "OPENAI_API_KEY=None" in cell.stdout
                assert "TELEGRAM_BOT_TOKEN=None" in cell.stdout
                assert "SSH_AUTH_SOCK=None" in cell.stdout
                assert "NCBI_API_KEY=ncbi-allowed-key" in cell.stdout
                assert "NET=BLOCKED" in cell.stdout
                assert (py_kernel.peak_rss_kb() or 0) > 0
                assert (py_kernel.cpu_seconds() or 0.0) >= 0.0

                timer = threading.Timer(1.0, py_kernel.interrupt)
                timer.start()
                interrupted = py_kernel.execute("import time; time.sleep(30)", timeout_s=15.0)
                timer.cancel()
                assert interrupted.status == "interrupted"

                after = py_kernel.execute("print('RECOVERED_PY')", timeout_s=10.0)
                assert after.status == "ok"
                assert "RECOVERED_PY" in after.stdout
            finally:
                py_kernel.shutdown()

            alive_py = [p for p in tree_pids if Path(f"/proc/{p}").exists()]
            assert not alive_py, f"Orphan Python kernel processes remained: {alive_py}"

            # --- R SessionKernel (if IRkernel spec is installed) ---
            try:
                has_r = "ir" in _r_spec_manager().find_kernel_specs()
            except Exception:
                has_r = False

            if has_r:
                r_kernel = SessionKernel(
                    "r-sbx",
                    workspace=ws,
                    language="r",
                    spawn_wrapper=wrapper,
                    env_scrubber=without_control_credentials,
                    startup_timeout=30.0,
                )
                r_kernel.start()
                r_pids = _descendant_pids(r_kernel.pid)
                try:
                    rcell = r_kernel.execute(
                        "cat('OPENAI=', Sys.getenv('OPENAI_API_KEY'), '\\n', sep='')\n"
                        "cat('NCBI=', Sys.getenv('NCBI_API_KEY'), '\\n', sep='')\n",
                        timeout_s=15.0,
                    )
                    assert rcell.status == "ok", rcell.error
                    assert "OPENAI=\n" in rcell.stdout
                    assert "NCBI=ncbi-allowed-key" in rcell.stdout
                    assert (r_kernel.peak_rss_kb() or 0) > 0

                    rtimer = threading.Timer(1.0, r_kernel.interrupt)
                    rtimer.start()
                    r_int = r_kernel.execute("Sys.sleep(30)", timeout_s=15.0)
                    rtimer.cancel()
                    assert r_int.status == "interrupted"
                finally:
                    r_kernel.shutdown()

                alive_r = [p for p in r_pids if Path(f"/proc/{p}").exists()]
                assert not alive_r, f"Orphan R kernel processes remained: {alive_r}"
        finally:
            await manager.aclose()

    asyncio.run(asyncio.wait_for(run(), timeout=60.0))
