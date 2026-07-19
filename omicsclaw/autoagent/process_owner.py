"""Backend-owned, confirmably stoppable process owner for AutoAgent workers.

The production Adapter is intentionally Linux-only for now.  It reuses the
canonical user-systemd + bubblewrap process-tree owner used by strict Runs.
Hosts without that Adapter reject before spawning; there is no thread or
plain-process fallback.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
import struct
import sys
import threading
from typing import Any, Literal

from omicsclaw.autoagent.constants import SUBPROCESS_ENV_WHITELIST
from omicsclaw.skill.execution.async_subprocess_driver import (
    ProcessTreeStopUnconfirmed,
    SYSTEMD_USER_SCOPE_REFERENCE_TYPE,
    adrive_subprocess,
    governed_process_tree_supported,
    new_governed_process_tree_reference,
    reconcile_governed_process_tree,
)


LINUX_SYSTEMD_OWNER_REFERENCE_TYPE = SYSTEMD_USER_SCOPE_REFERENCE_TYPE
OWNER_STOP_EVIDENCE_CODE = "process_tree_absent_v1"

_PROTOCOL_VERSION = 1
_FRAME_HEADER = struct.Struct("!I")
_MAX_FRAME_BYTES = 4 * 1024 * 1024 + 64 * 1024
_MAX_REQUEST_BYTES = 1024 * 1024
_MAX_EVENT_BYTES = 256 * 1024
_MAX_EVENT_COUNT = 8192
_MAX_TOTAL_EVENT_BYTES = 16 * 1024 * 1024
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 100_000
_CONNECT_TIMEOUT_SECONDS = 10.0
_IPC_CLOSE_TIMEOUT_SECONDS = 2.0
_EVENT_TYPE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SESSION_ID_RE = re.compile(r"[0-9a-f]{32}\Z")

WorkerEventCallback = Callable[[str, dict[str, Any]], None]
WORKER_REQUEST_KEYS = frozenset(
    {
        "skill_name",
        "method",
        "input_path",
        "cwd",
        "output_dir",
        "output_claim_id",
        "max_iterations",
        "fixed_params",
        "evolution_goal",
        "surface_level",
        "explicit_files",
        "auto_promote",
        "llm_provider",
        "llm_model",
        "llm_provider_config",
        "demo",
    }
)


class GovernedWorkerUnavailable(RuntimeError):
    """This host has no AutoAgent owner with an exact stopped proof."""


class GovernedWorkerProtocolError(RuntimeError):
    """The bounded child IPC contract was violated."""


class GovernedWorkerStopUnconfirmed(RuntimeError):
    """The Backend cannot prove that the worker process tree is empty."""


@dataclass(frozen=True, slots=True)
class GovernedWorkerOutcome:
    """Terminal child evidence released only after process-tree stop proof."""

    status: Literal["done", "error", "cancelled"]
    result: dict[str, Any] | None = None
    error_code: str | None = None


def governed_worker_available() -> bool:
    """Return whether the production AutoAgent process owner is available."""

    return governed_process_tree_supported()


def new_governed_worker_reference() -> tuple[str, str]:
    """Pre-generate the durable owner reference before session acceptance."""

    if not governed_worker_available():
        raise GovernedWorkerUnavailable(
            "governed AutoAgent process ownership is unavailable"
        )
    return new_governed_process_tree_reference()


async def reconcile_governed_worker(
    execution_reference_type: str,
    execution_reference: str,
    *,
    ipc_root: Path,
    session_id: str,
) -> str:
    """Stop/reconcile one durable owner and return closed absent evidence."""

    try:
        await reconcile_governed_process_tree(
            execution_reference_type,
            execution_reference,
        )
    except ProcessTreeStopUnconfirmed as exc:
        raise GovernedWorkerStopUnconfirmed(
            "AutoAgent process-tree stop could not be confirmed"
        ) from exc
    cleanup_governed_worker_ipc(ipc_root, session_id)
    return OWNER_STOP_EVIDENCE_CODE


def _validated_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or _SESSION_ID_RE.fullmatch(session_id) is None:
        raise ValueError("governed AutoAgent session_id must be 32 lowercase hex")
    return session_id


def prepare_governed_worker_ipc_root(state_root: Path) -> Path:
    """Create/verify the Backend-private deterministic IPC parent."""

    root = Path(state_root).resolve(strict=True) / "autoagent-ipc"
    try:
        root.mkdir(mode=0o700, exist_ok=True)
        root_stat = os.lstat(root)
    except OSError as exc:
        raise GovernedWorkerUnavailable(
            "AutoAgent IPC authority directory is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or stat.S_ISLNK(root_stat.st_mode)
        or root_stat.st_uid != os.geteuid()
        or stat.S_IMODE(root_stat.st_mode) & 0o077
    ):
        raise GovernedWorkerUnavailable(
            "AutoAgent IPC authority directory is not Backend-private"
        )
    return root


def governed_worker_ipc_directory(ipc_root: Path, session_id: str) -> Path:
    """Derive one session IPC directory without persisted random state."""

    bound_session_id = _validated_session_id(session_id)
    root = Path(ipc_root).resolve(strict=True)
    return root / bound_session_id


def _governed_worker_ipc_address(ipc_root: Path, session_id: str) -> str:
    """Derive one short Linux abstract-socket address."""

    bound_session_id = _validated_session_id(session_id)
    authority = hashlib.sha256(
        os.fsencode(Path(ipc_root).resolve(strict=True))
    ).hexdigest()[:16]
    return f"\0omicsclaw-aa-{authority}-{bound_session_id}"


def _governed_worker_environment(ipc_dir: Path) -> dict[str, str]:
    """Build a bounded, credential-free launch environment for the child.

    Provider credentials are intentionally absent here.  The parent resolves
    them into the already-bounded request and sends that request only after
    the peer-credential-checked IPC handshake.
    """

    source = os.environ
    env = {key: value for key in SUBPROCESS_ENV_WHITELIST if (value := source.get(key))}
    # The outer ``systemd-run --user`` helper needs the user-bus coordinates.
    # The governed driver removes both with bwrap ``--unsetenv`` before the
    # trusted worker entrypoint executes.
    for key in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"):
        if value := source.get(key):
            env[key] = value
    scratch = str(ipc_dir)
    env.update(
        {
            "HOME": scratch,
            "TMPDIR": scratch,
            "PYTHONNOUSERSITE": "1",
            "PYTHONUNBUFFERED": "1",
            "MPLBACKEND": "Agg",
            "OMICSCLAW_AUTOAGENT_WORKER": "1",
        }
    )
    return env


def cleanup_governed_worker_ipc(ipc_root: Path, session_id: str) -> None:
    """Remove deterministic IPC state only after the caller proved owner empty."""

    directory = governed_worker_ipc_directory(ipc_root, session_id)
    try:
        directory_stat = os.lstat(directory)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise GovernedWorkerStopUnconfirmed(
            "AutoAgent IPC state could not be inspected"
        ) from exc
    if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
        raise GovernedWorkerStopUnconfirmed("AutoAgent IPC state is not a directory")
    if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        raise GovernedWorkerStopUnconfirmed(
            "AutoAgent IPC cleanup lacks descriptor-relative safety"
        )
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        raise GovernedWorkerStopUnconfirmed(
            "AutoAgent IPC state could not be removed"
        ) from exc


def _json_object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GovernedWorkerProtocolError("worker frame contains duplicate keys")
        result[key] = value
    return result


def _validate_json_shape(value: Any) -> None:
    pending: list[tuple[Any, int]] = [(value, 1)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise GovernedWorkerProtocolError("worker frame JSON is too complex")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise GovernedWorkerProtocolError(
                        "worker frame object keys must be strings"
                    )
                pending.append((child, depth + 1))
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise GovernedWorkerProtocolError(
                    "worker frame contains a non-finite number"
                )
        elif item is None or isinstance(item, (str, int, bool)):
            continue
        else:  # pragma: no cover - json.loads/json.dumps exclude this
            raise GovernedWorkerProtocolError("worker frame has a non-JSON value")


def encode_worker_frame(
    payload: Mapping[str, Any],
    *,
    max_bytes: int = _MAX_FRAME_BYTES,
) -> bytes:
    """Encode one finite, bounded, length-prefixed JSON object."""

    try:
        body = json.dumps(
            dict(payload),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError) as exc:
        raise GovernedWorkerProtocolError("worker frame is not finite JSON") from exc
    if not body or len(body) > max_bytes:
        raise GovernedWorkerProtocolError("worker frame exceeds its byte limit")
    return _FRAME_HEADER.pack(len(body)) + body


def decode_worker_frame(
    body: bytes,
    *,
    max_bytes: int = _MAX_FRAME_BYTES,
) -> dict[str, Any]:
    """Decode one already-delimited bounded JSON object."""

    if not body or len(body) > max_bytes:
        raise GovernedWorkerProtocolError("worker frame exceeds its byte limit")
    try:
        payload = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_json_object_without_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                GovernedWorkerProtocolError("worker frame contains a non-finite number")
            ),
        )
    except GovernedWorkerProtocolError:
        raise
    except (RecursionError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GovernedWorkerProtocolError("worker frame is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise GovernedWorkerProtocolError("worker frame must be a JSON object")
    _validate_json_shape(payload)
    return payload


async def write_worker_frame(
    writer: asyncio.StreamWriter,
    payload: Mapping[str, Any],
    *,
    max_bytes: int = _MAX_FRAME_BYTES,
) -> None:
    writer.write(encode_worker_frame(payload, max_bytes=max_bytes))
    await writer.drain()


async def read_worker_frame(
    reader: asyncio.StreamReader,
    *,
    max_bytes: int = _MAX_FRAME_BYTES,
) -> dict[str, Any]:
    try:
        header = await reader.readexactly(_FRAME_HEADER.size)
        (size,) = _FRAME_HEADER.unpack(header)
        if size <= 0 or size > max_bytes:
            raise GovernedWorkerProtocolError("worker frame exceeds its byte limit")
        body = await reader.readexactly(size)
    except asyncio.IncompleteReadError as exc:
        raise GovernedWorkerProtocolError("worker IPC closed mid-frame") from exc
    return decode_worker_frame(body, max_bytes=max_bytes)


def _validate_terminal_frame(frame: Mapping[str, Any]) -> GovernedWorkerOutcome:
    status = frame.get("status")
    if status == "done":
        if set(frame) != {"version", "kind", "status", "result"}:
            raise GovernedWorkerProtocolError("successful terminal frame is invalid")
        result = frame.get("result")
        if not isinstance(result, dict) or result.get("success") is not True:
            raise GovernedWorkerProtocolError(
                "successful terminal frame requires exact success=true"
            )
        return GovernedWorkerOutcome("done", result=dict(result))
    if status == "error":
        if set(frame) != {"version", "kind", "status", "error_code"}:
            raise GovernedWorkerProtocolError("error terminal frame is invalid")
        error_code = frame.get("error_code")
        if error_code not in {
            "harness_failed",
            "invalid_terminal_result",
            "worker_crashed",
        }:
            raise GovernedWorkerProtocolError("worker terminal error code is invalid")
        return GovernedWorkerOutcome("error", error_code=str(error_code))
    raise GovernedWorkerProtocolError("worker terminal status is invalid")


class GovernedAutoAgentWorker:
    """One production AutoAgent child bound to a durable execution reference."""

    def __init__(
        self,
        *,
        session_id: str,
        execution_reference_type: str,
        execution_reference: str,
        cwd: Path,
        writable_output_root: Path,
        ipc_root: Path,
        request: Mapping[str, Any],
    ) -> None:
        _validated_session_id(session_id)
        if set(request) != WORKER_REQUEST_KEYS:
            raise ValueError("governed AutoAgent worker request keys are invalid")
        if request.get("auto_promote") is not False:
            raise ValueError("governed AutoAgent workers require auto_promote=false")
        if request.get("output_claim_id") != session_id:
            raise ValueError("AutoAgent output claim must equal its session authority")
        request_output = request.get("output_dir")
        if (
            not isinstance(request_output, str)
            or not Path(request_output).is_absolute()
        ):
            raise ValueError("governed AutoAgent output_dir must be absolute")
        request_frame = {
            "version": _PROTOCOL_VERSION,
            "kind": "request",
            "payload": dict(request),
        }
        # Validate before durable acceptance/spawn when callers construct the
        # worker before committing its session authority.
        self._encoded_request = encode_worker_frame(
            request_frame,
            max_bytes=_MAX_REQUEST_BYTES,
        )
        self.session_id = session_id
        self.execution_reference_type = execution_reference_type
        self.execution_reference = execution_reference
        self.cwd = Path(cwd)
        self.writable_output_root = Path(writable_output_root)
        self.ipc_root = Path(ipc_root)
        self.expected_result_identity = {
            "skill": request["skill_name"],
            "method": request["method"],
            "evolution_goal": request["evolution_goal"],
            "output_dir": request["output_dir"],
        }
        self.process_tree_confirmed_empty = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cancel_event: asyncio.Event | None = None
        self._cancel_requested = False
        self._state_lock = threading.Lock()

    def request_cancel(self) -> None:
        """Request cancellation; completion still waits for exact stop proof."""

        with self._state_lock:
            self._cancel_requested = True
            loop = self._loop
            event = self._cancel_event
        if loop is not None and event is not None:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass

    async def _stop_driver_with_proof(self, task: asyncio.Task[Any]) -> None:
        if not task.done():
            task.cancel()
        try:
            while True:
                try:
                    await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    if task.done():
                        try:
                            task.result()
                        except asyncio.CancelledError:
                            pass
                        break
        except ProcessTreeStopUnconfirmed as exc:
            raise GovernedWorkerStopUnconfirmed(
                "AutoAgent process-tree stop could not be confirmed"
            ) from exc
        except BaseException:
            # A spawn/helper failure can happen after the durable scope name is
            # bound.  Re-observe that exact owner before allowing closure.
            await reconcile_governed_worker(
                self.execution_reference_type,
                self.execution_reference,
                ipc_root=self.ipc_root,
                session_id=self.session_id,
            )
        self.process_tree_confirmed_empty = True

    async def _exchange_frames(
        self,
        connection: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]],
        *,
        on_event: WorkerEventCallback | None,
    ) -> GovernedWorkerOutcome:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.shield(connection),
                timeout=_CONNECT_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise GovernedWorkerProtocolError(
                "worker did not open bounded IPC"
            ) from exc
        try:
            hello = await read_worker_frame(reader, max_bytes=4096)
            if hello != {"version": _PROTOCOL_VERSION, "kind": "hello"}:
                raise GovernedWorkerProtocolError("worker IPC hello is invalid")
            nonce = secrets.token_hex(32)
            await write_worker_frame(
                writer,
                {
                    "version": _PROTOCOL_VERSION,
                    "kind": "challenge",
                    "nonce": nonce,
                },
                max_bytes=4096,
            )
            response = await read_worker_frame(reader, max_bytes=4096)
            if response != {
                "version": _PROTOCOL_VERSION,
                "kind": "challenge_response",
                "nonce": nonce,
            }:
                raise GovernedWorkerProtocolError(
                    "worker IPC challenge response is invalid"
                )
            writer.write(self._encoded_request)
            await writer.drain()
            # Provider credentials have crossed their only authorized boundary;
            # do not retain the serialized request for the Runtime TTL.
            self._encoded_request = b""

            event_count = 0
            event_bytes = 0
            terminal: GovernedWorkerOutcome | None = None
            while True:
                try:
                    frame = await read_worker_frame(reader)
                except GovernedWorkerProtocolError as exc:
                    if (
                        terminal is not None
                        and exc.__cause__ is not None
                        and isinstance(exc.__cause__, asyncio.IncompleteReadError)
                    ):
                        return terminal
                    raise
                if frame.get("version") != _PROTOCOL_VERSION:
                    raise GovernedWorkerProtocolError("worker frame version is invalid")
                kind = frame.get("kind")
                if terminal is not None:
                    raise GovernedWorkerProtocolError(
                        "worker emitted data after terminal evidence"
                    )
                if kind == "event":
                    if set(frame) != {"version", "kind", "event_type", "data"}:
                        raise GovernedWorkerProtocolError(
                            "worker event frame is invalid"
                        )
                    event_type = frame.get("event_type")
                    data = frame.get("data")
                    if (
                        not isinstance(event_type, str)
                        or _EVENT_TYPE_RE.fullmatch(event_type) is None
                        or event_type in {"done", "error"}
                        or not isinstance(data, dict)
                    ):
                        raise GovernedWorkerProtocolError("worker event is invalid")
                    event_count += 1
                    if event_count > _MAX_EVENT_COUNT:
                        raise GovernedWorkerProtocolError(
                            "worker emitted too many event frames"
                        )
                    encoded_event_size = (
                        len(encode_worker_frame(frame, max_bytes=_MAX_EVENT_BYTES))
                        - _FRAME_HEADER.size
                    )
                    event_bytes += encoded_event_size
                    if event_bytes > _MAX_TOTAL_EVENT_BYTES:
                        raise GovernedWorkerProtocolError(
                            "worker event byte budget is exhausted"
                        )
                    if on_event is not None:
                        try:
                            on_event(event_type, dict(data))
                        except Exception:
                            # Progress is non-authoritative.  A broken observer
                            # cannot acquire or disrupt worker ownership.
                            pass
                    continue
                if kind == "terminal":
                    terminal = _validate_terminal_frame(frame)
                    if terminal.status == "done":
                        assert terminal.result is not None
                        if any(
                            terminal.result.get(key) != value
                            for key, value in self.expected_result_identity.items()
                        ):
                            raise GovernedWorkerProtocolError(
                                "worker result does not match start authority"
                            )
                    continue
                raise GovernedWorkerProtocolError("worker frame kind is invalid")
        finally:
            writer.close()
            try:
                await asyncio.wait_for(
                    writer.wait_closed(),
                    timeout=_IPC_CLOSE_TIMEOUT_SECONDS,
                )
            except (ConnectionError, TimeoutError):
                pass

    async def run(
        self,
        *,
        on_event: WorkerEventCallback | None = None,
    ) -> GovernedWorkerOutcome:
        """Run the child and release terminal evidence only after tree absence."""

        if not governed_worker_available():
            raise GovernedWorkerUnavailable(
                "governed AutoAgent process ownership is unavailable"
            )
        if self.execution_reference_type != LINUX_SYSTEMD_OWNER_REFERENCE_TYPE:
            raise GovernedWorkerUnavailable(
                "AutoAgent execution reference type is unavailable on this host"
            )
        loop = asyncio.get_running_loop()
        cancel_event = asyncio.Event()
        with self._state_lock:
            if self._loop is not None:
                raise RuntimeError("governed AutoAgent worker is single-use")
            self._loop = loop
            self._cancel_event = cancel_event
            if self._cancel_requested:
                cancel_event.set()
        server: asyncio.AbstractServer | None = None
        driver_task: asyncio.Task[Any] | None = None
        protocol_task: asyncio.Task[GovernedWorkerOutcome] | None = None
        cancel_task: asyncio.Task[bool] | None = None
        protocol_outcome: GovernedWorkerOutcome | None = None
        protocol_failed = False

        async def confirm_setup_owner_stopped() -> None:
            if driver_task is not None:
                await self._stop_driver_with_proof(driver_task)
                return
            await reconcile_governed_worker(
                self.execution_reference_type,
                self.execution_reference,
                ipc_root=self.ipc_root,
                session_id=self.session_id,
            )
            self.process_tree_confirmed_empty = True

        try:
            cwd = self.cwd.resolve(strict=True)
            output_root = self.writable_output_root.resolve(strict=True)
            if not cwd.is_dir() or not output_root.is_dir():
                raise ValueError(
                    "AutoAgent worker directories must be existing directories"
                )
            if (
                Path(str(self.expected_result_identity["output_dir"])).resolve(
                    strict=True
                )
                != output_root
            ):
                raise ValueError("AutoAgent worker output authority changed")

            ipc_root = prepare_governed_worker_ipc_root(self.ipc_root.parent)
            if ipc_root != self.ipc_root.resolve(strict=True):
                raise GovernedWorkerUnavailable("AutoAgent IPC root authority changed")
            ipc_dir = governed_worker_ipc_directory(ipc_root, self.session_id)
            try:
                ipc_dir.mkdir(mode=0o700)
            except OSError as exc:
                raise GovernedWorkerUnavailable(
                    "AutoAgent session IPC directory is unavailable"
                ) from exc
            connection: asyncio.Future[
                tuple[asyncio.StreamReader, asyncio.StreamWriter]
            ] = loop.create_future()

            def connected(
                reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter,
            ) -> None:
                transport_socket = writer.get_extra_info("socket")
                try:
                    credentials = transport_socket.getsockopt(
                        socket.SOL_SOCKET,
                        socket.SO_PEERCRED,
                        struct.calcsize("3i"),
                    )
                    _pid, peer_uid, _peer_gid = struct.unpack("3i", credentials)
                except (AttributeError, OSError, struct.error):
                    writer.close()
                    return
                if peer_uid != os.geteuid() or connection.done():
                    writer.close()
                    return
                connection.set_result((reader, writer))

            ipc_address = _governed_worker_ipc_address(ipc_root, self.session_id)
            server = await asyncio.start_unix_server(connected, path=ipc_address)
            env = _governed_worker_environment(ipc_dir)
            command = [
                sys.executable,
                str(Path(__file__).with_name("worker_process.py").resolve(strict=True)),
                "--ipc-address",
                "@" + ipc_address[1:],
            ]
            driver_task = asyncio.create_task(
                adrive_subprocess(
                    command,
                    cwd=cwd,
                    env=env,
                    out_dir=output_root,
                    require_process_tree_proof=True,
                    governed_execution_reference=self.execution_reference,
                    stdio="devnull",
                )
            )
            protocol_task = asyncio.create_task(
                self._exchange_frames(connection, on_event=on_event)
            )
            cancel_task = asyncio.create_task(cancel_event.wait())
            while True:
                waiters: set[asyncio.Task[Any]] = {driver_task, cancel_task}
                if protocol_outcome is None and not protocol_failed:
                    waiters.add(protocol_task)
                done, _pending = await asyncio.wait(
                    waiters,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_task in done and cancel_task.result():
                    await self._stop_driver_with_proof(driver_task)
                    if not protocol_task.done():
                        protocol_task.cancel()
                    await asyncio.gather(protocol_task, return_exceptions=True)
                    return GovernedWorkerOutcome("cancelled", error_code="cancelled")
                if protocol_task in done and protocol_outcome is None:
                    try:
                        protocol_outcome = protocol_task.result()
                    except GovernedWorkerProtocolError:
                        protocol_failed = True
                        await self._stop_driver_with_proof(driver_task)
                    if protocol_failed:
                        return GovernedWorkerOutcome(
                            "error",
                            error_code="worker_crashed",
                        )
                if driver_task in done:
                    try:
                        completed = driver_task.result()
                    except ProcessTreeStopUnconfirmed as exc:
                        raise GovernedWorkerStopUnconfirmed(
                            "AutoAgent process-tree stop could not be confirmed"
                        ) from exc
                    except BaseException:
                        await reconcile_governed_worker(
                            self.execution_reference_type,
                            self.execution_reference,
                            ipc_root=self.ipc_root,
                            session_id=self.session_id,
                        )
                        self.process_tree_confirmed_empty = True
                        if not protocol_task.done():
                            protocol_task.cancel()
                        await asyncio.gather(protocol_task, return_exceptions=True)
                        return GovernedWorkerOutcome(
                            "error",
                            error_code="worker_start_failed",
                        )
                    self.process_tree_confirmed_empty = True
                    if not protocol_task.done():
                        try:
                            protocol_outcome = await asyncio.wait_for(
                                asyncio.shield(protocol_task),
                                timeout=_IPC_CLOSE_TIMEOUT_SECONDS,
                            )
                        except (GovernedWorkerProtocolError, TimeoutError):
                            protocol_outcome = None
                    if completed.returncode != 0 or protocol_outcome is None:
                        return GovernedWorkerOutcome(
                            "error",
                            error_code="worker_crashed",
                        )
                    return protocol_outcome
        except asyncio.CancelledError:
            await confirm_setup_owner_stopped()
            raise
        except GovernedWorkerStopUnconfirmed:
            raise
        except BaseException:
            await confirm_setup_owner_stopped()
            return GovernedWorkerOutcome(
                "error",
                error_code="worker_start_failed",
            )
        finally:
            self._encoded_request = b""
            if cancel_task is not None:
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)
            if server is not None:
                server.close()
                await server.wait_closed()
            if protocol_task is not None and not protocol_task.done():
                protocol_task.cancel()
                await asyncio.gather(protocol_task, return_exceptions=True)
            if self.process_tree_confirmed_empty:
                cleanup_governed_worker_ipc(self.ipc_root, self.session_id)


__all__ = [
    "GovernedAutoAgentWorker",
    "GovernedWorkerOutcome",
    "GovernedWorkerProtocolError",
    "GovernedWorkerStopUnconfirmed",
    "GovernedWorkerUnavailable",
    "LINUX_SYSTEMD_OWNER_REFERENCE_TYPE",
    "OWNER_STOP_EVIDENCE_CODE",
    "WORKER_REQUEST_KEYS",
    "cleanup_governed_worker_ipc",
    "decode_worker_frame",
    "encode_worker_frame",
    "governed_worker_available",
    "governed_worker_ipc_directory",
    "new_governed_worker_reference",
    "read_worker_frame",
    "reconcile_governed_worker",
    "prepare_governed_worker_ipc_root",
    "write_worker_frame",
]
