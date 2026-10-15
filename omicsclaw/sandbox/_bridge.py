"""Loopback-to-UDS bridge and network-namespace init wrapper for Linux sandboxes.

Inside a Linux ``--unshare-net`` network namespace, the loopback interface
``lo`` starts down and host ``127.0.0.1`` is unreachable over TCP. This module:
1. Brings up ``lo`` inside the new network namespace via ``ioctl(SIOCSIFFLAGS)``.
2. Optionally binds ``127.0.0.1:<listen_port>`` inside the namespace and
   forwards all TCP streams to the host :class:`EgressProxy` Unix domain
   socket.
3. Spawns the target command and exits with its exact status as soon as the
   target command finishes.

Pure standard-library module: no logging, no dynamic imports, and no direct
reads of the process environment or ``sys`` argument vector.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import socket
import struct
from collections.abc import Sequence
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "BRIDGE_SCRIPT_PATH",
    "LoopbackUnixBridge",
    "bring_up_loopback",
    "build_bridge_argv",
    "main",
]

BRIDGE_SCRIPT_PATH = Path(__file__).resolve()
_SIOCGIFFLAGS = 0x8913
_SIOCSIFFLAGS = 0x8914
_IFF_UP = 0x1
_CHUNK = 65536


def bring_up_loopback() -> bool:
    """Bring up the ``lo`` interface in the current network namespace."""
    if fcntl is None:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            ifr = struct.pack("16sH14s", b"lo", 0, b"")
            res = fcntl.ioctl(sock.fileno(), _SIOCGIFFLAGS, ifr)
            flags = struct.unpack("16sH14s", res)[1]
            ifr = struct.pack("16sH14s", b"lo", flags | _IFF_UP, b"")
            fcntl.ioctl(sock.fileno(), _SIOCSIFFLAGS, ifr)
        return True
    except OSError:
        return False


def build_bridge_argv(
    python_executable: str,
    child_argv: Sequence[str],
    *,
    unix_socket: Path | str | None = None,
    listen_port: int | None = None,
) -> list[str]:
    """Construct the command line that runs *child_argv* inside the netns bridge."""
    cmd = [python_executable, "-B", str(BRIDGE_SCRIPT_PATH)]
    if unix_socket is not None and listen_port is not None and listen_port > 0:
        cmd.extend(
            [
                "--unix-socket",
                str(unix_socket),
                "--listen-port",
                str(int(listen_port)),
            ]
        )
    cmd.append("--")
    cmd.extend(child_argv)
    return cmd


class LoopbackUnixBridge:
    """Forwards ``127.0.0.1:<listen_port>`` TCP connections to a Unix domain socket."""

    def __init__(self, unix_socket: Path | str, listen_port: int = 0) -> None:
        self._unix_socket = str(unix_socket)
        self._requested_port = int(listen_port)
        self._server: asyncio.AbstractServer | None = None
        self._port: int = 0

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle_client,
            host="127.0.0.1",
            port=self._requested_port,
        )
        sockets = self._server.sockets or ()
        if sockets:
            self._port = int(sockets[0].getsockname()[1])

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
            self._port = 0

    async def _handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        uds_writer: asyncio.StreamWriter | None = None
        try:
            uds_reader, uds_writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self._unix_socket),
                timeout=10.0,
            )
            t1 = asyncio.create_task(_copy_stream(client_reader, uds_writer))
            t2 = asyncio.create_task(_copy_stream(uds_reader, client_writer))
            try:
                await asyncio.gather(t1, t2)
            finally:
                for task in (t1, t2):
                    if not task.done():
                        task.cancel()
        except OSError:
            pass
        finally:
            if uds_writer is not None:
                with contextlib.suppress(Exception):
                    uds_writer.close()
            with contextlib.suppress(Exception):
                client_writer.close()


async def _copy_stream(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        while True:
            data = await reader.read(_CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (OSError, asyncio.CancelledError):
        pass
    finally:
        with contextlib.suppress(Exception):
            if writer.can_write_eof():
                writer.write_eof()


async def _run_main(
    unix_socket: str | None,
    listen_port: int,
    command: Sequence[str],
) -> int:
    bring_up_loopback()
    bridge: LoopbackUnixBridge | None = None
    if unix_socket and listen_port > 0:
        bridge = LoopbackUnixBridge(unix_socket, listen_port)
        await bridge.start()

    try:
        if not command:
            # Standalone bridge server mode until interrupted
            stop_event = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(NotImplementedError, RuntimeError):
                    loop.add_signal_handler(sig, stop_event.set)
            await stop_event.wait()
            return 0

        proc = await asyncio.create_subprocess_exec(*command)
        loop = asyncio.get_running_loop()

        def _forward_signal(sig: int) -> None:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    proc.send_signal(sig)

        # Ignore SIGINT in the bridge itself: callers signal the whole process
        # group via os.killpg(pgid, SIGINT), so the child process already
        # receives SIGINT directly. Forwarding a second SIGINT would interrupt
        # IPython mid-traceback.
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(signal.SIGINT, lambda: None)
        for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", signal.SIGTERM)):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, _forward_signal, sig)

        code = await proc.wait()
        if code < 0:
            return 128 - code
        return code
    finally:
        if bridge is not None:
            await bridge.stop()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for ``python -B /path/to/_bridge.py``."""
    parser = argparse.ArgumentParser(prog="omicsclaw-sandbox-bridge")
    parser.add_argument("--unix-socket", default=None)
    parser.add_argument("--listen-port", type=int, default=0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    ns = parser.parse_args(argv)
    cmd = list(ns.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    return asyncio.run(_run_main(ns.unix_socket, ns.listen_port, cmd))


if __package__ is None:  # pragma: no cover
    raise SystemExit(main())
