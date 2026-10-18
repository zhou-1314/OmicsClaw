"""Authenticated local transport to the independently running Notebook owner."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

from omicsclaw.entry.notebook.contracts import OWNER_PROTOCOL, bounded, encoded
from omicsclaw.entry.notebook.policy import encode_policy, policy_digest
from omicsclaw.entry.notebook.security import (
    private_directory,
    private_file,
    process_evidence,
    read_private,
    same_process,
    write_private,
)
from omicsclaw.entry.notebook.service import NotebookError
from omicsclaw.tools.builtin.bash import without_control_credentials

from .manifest import verified_source_metadata
from .protocol import PUBLIC_ERROR_CODES

MAX_IPC_BYTES = 512 * 1024
LEASE_SECONDS = 30


def controller_evidence(workspace, pid, claim):
    """SO_PEERCRED, current flock holder, nonce and process birth must all agree."""
    path = workspace / ".omicsclaw/controller.owner"
    record = read_private(path)
    birth = process_evidence(pid)
    if (
        birth is None
        or birth["uid"] != os.getuid()
        or record.get("pid") != pid
        or record.get("startTime") != birth["boot"] + ":" + birth["start"]
        or not isinstance(claim, dict)
        or set(claim) != {"instanceId", "nonce", "epoch"}
        or record.get("instanceId") != claim["instanceId"]
        or record.get("nonce") != claim["nonce"]
        or not isinstance(claim["epoch"], str)
        or len(claim["epoch"]) != 32
    ):
        raise NotebookError("notebook_controller_changed", 409)
    fd = private_file(path)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return birth
        raise NotebookError("notebook_controller_changed", 409)
    finally:
        os.close(fd)


class OwnerServer:
    def __init__(self, runtime):
        self.runtime = runtime
        self.lease = None
        self.active = 0

    def authorize(self, pid, claim, token):
        birth = controller_evidence(self.runtime.workspace, pid, claim)
        lease = self.lease
        if (
            lease is None
            or lease["claim"] != claim
            or lease["pid"] != pid
            or lease["token"] != token
            or time.monotonic() > lease["until"]
        ):
            raise NotebookError("notebook_controller_changed", 409)
        lease["until"] = time.monotonic() + LEASE_SECONDS
        self.runtime.controller_process = birth

    async def handle(self, reader, writer):
        self.active += 1
        try:
            if self.active > 16:
                raise NotebookError("notebook_resource_limit", 409)
            peer = writer.get_extra_info("socket")
            pid, uid, _ = struct.unpack(
                "3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            if uid != os.getuid():
                raise NotebookError("notebook_controller_changed", 409)
            line = await asyncio.wait_for(reader.readline(), 5)
            if not line.endswith(b"\n") or len(line) > MAX_IPC_BYTES:
                raise NotebookError("invalid_notebook_command")
            request = json.loads(line)
            if (
                not isinstance(request, dict)
                or set(request)
                != {"protocol", "claim", "token", "owner", "method", "params"}
                or request["protocol"] != OWNER_PROTOCOL
            ):
                raise NotebookError("invalid_notebook_command")
            claim, method, params = (
                request["claim"],
                request["method"],
                request["params"],
            )
            if not isinstance(params, dict):
                raise NotebookError("invalid_notebook_command")
            birth = controller_evidence(self.runtime.workspace, pid, claim)
            if method == "attach":
                if (
                    set(params) != {"identity", "policyDigest"}
                    or params["identity"] != self.runtime.identity
                ):
                    raise NotebookError("notebook_store_mismatch", 409)
                if params["policyDigest"] != self.runtime.policy_digest:
                    raise NotebookError("notebook_policy_changed", 409)
                if (
                    self.lease is None
                    or self.lease["claim"] != claim
                    or self.lease["pid"] != pid
                ):
                    self.lease = {
                        "claim": claim,
                        "pid": pid,
                        "token": secrets.token_hex(32),
                        "until": 0,
                    }
                self.lease["until"] = time.monotonic() + LEASE_SECONDS
                self.runtime.controller_process = birth
                result = {**self.runtime.health(), "leaseToken": self.lease["token"]}
            else:
                if request["owner"] != {
                    "ownerId": self.runtime.owner_id,
                    "ownerEpoch": self.runtime.epoch,
                }:
                    raise NotebookError("notebook_owner_changed", 409)
                authorize = lambda: self.authorize(pid, claim, request["token"])
                authorize()
                if method in {"bind", "admit", "admit_agent", "cancel", "release", "context", "check_request", "select_environment", "save", "resume", "register_helper", "maintenance", "analysis_command", "analysis_agent", "check_analysis_request"}:
                    result = await self.runtime.mutate(method, params, authorize)
                elif method in {"analysis_state", "analysis_page"}:
                    result = await asyncio.to_thread(getattr(self.runtime.analysis.store, "state_response" if method == "analysis_state" else "page"), params)
                elif method in {"binding", "receipt", "changes", "cell"}:
                    result = getattr(self.runtime.actions, method)(params)
                elif method in {"artifact_info", "artifact_chunk"}:
                    result = await asyncio.to_thread(self.runtime.artifact_reader.read, params, chunk=method == "artifact_chunk")
                elif method == "health":
                    result = self.runtime.health()
                elif method == "list":
                    result = self.runtime.views.listing(
                        params.get("cursor"), params.get("limit", 20)
                    )
                elif method == "snapshot":
                    result = self.runtime.views.snapshot(
                        params["notebookId"],
                        cell_cursor=params.get("cellCursor"),
                        execution_cursor=params.get("executionCursor"),
                        limit=params.get("limit", 20),
                    )
                elif method == "execution":
                    result = self.runtime.views.execution(
                        params["notebookId"], params["executionId"]
                    )
                elif method == "events":
                    result = self.runtime.views.events(
                        params["notebookId"],
                        params["executionId"],
                        params.get("cursor"),
                        params.get("limit", 20),
                    )
                else:
                    raise NotebookError("invalid_notebook_command")
            response = {"result": bounded(result)}
        except (NotebookError, ValueError) as error:
            code = getattr(error, "code", str(error))
            response = {
                "error": {
                    "code": code if code in PUBLIC_ERROR_CODES else "request_refused"
                }
            }
        except Exception:  # noqa: BLE001 - redact unknown failures at the IPC boundary
            response = {"error": {"code": "notebook_owner_unavailable"}}
        finally:
            self.active -= 1
        try:
            writer.write(encoded(response) + b"\n")
            await asyncio.wait_for(writer.drain(), 5)
        except (ConnectionError, TimeoutError):
            pass
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except (ConnectionError, TimeoutError):
                pass


class NotebookOwnerClient:
    """Closing a controller drops this proxy, never the owner's scientific tasks."""

    def __init__(
        self, workspace, identity, instance, epoch, *, limits=None, sandbox_config=None
    ):
        self.workspace = Path(workspace)
        self.identity = identity
        self.instance, self.epoch = instance, epoch
        self.directory = private_directory(self.workspace / ".omicsclaw/notebook-owner")
        self.lock = asyncio.Lock()
        self.token = None
        self.address = None
        self.health = None
        self.closed = False
        self.limits = limits or {}
        self.policy = encode_policy(sandbox_config)
        self.policy_digest = policy_digest(sandbox_config)
        self._semaphore = asyncio.Semaphore(16)

    def claim(self):
        record = read_private(self.workspace / ".omicsclaw/controller.owner")
        if record["instanceId"] != self.instance or record["pid"] != os.getpid():
            raise NotebookError("notebook_controller_changed", 409)
        return {
            "instanceId": self.instance,
            "nonce": record["nonce"],
            "epoch": self.epoch,
        }

    async def _request(self, method, params, *, address, token, owner):
        # Capture the complete connection before awaiting a concurrency slot.
        # Another attach must never replace an already validated write fence.
        claim = self.claim()
        async with self._semaphore:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(address, limit=MAX_IPC_BYTES), 5
            )
            try:
                payload = {
                    "protocol": OWNER_PROTOCOL,
                    "claim": claim,
                    "token": token,
                    "owner": owner,
                    "method": method,
                    "params": params,
                }
                writer.write(encoded(payload) + b"\n")
                await asyncio.wait_for(writer.drain(), 5)
                raw = await asyncio.wait_for(reader.readline(), 45)
                if len(raw) > MAX_IPC_BYTES or not raw.endswith(b"\n"):
                    raise NotebookError("notebook_owner_unavailable", 503)
                response = json.loads(raw)
                if "error" in response:
                    code = response["error"].get("code")
                    raise NotebookError(
                        code if code in PUBLIC_ERROR_CODES else "request_refused"
                    )
                return bounded(response["result"])
            finally:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), 1)
                except (ConnectionError, TimeoutError):
                    pass

    @staticmethod
    def _expected_source_digest() -> str:
        root = Path(__file__).resolve().parents[3]
        metadata = (
            verified_source_metadata(root)
            if (root / "controller-manifest.json").exists()
            else {}
        )
        return metadata.get("sourceFilesHash", "development-unverified")

    def _launch(self):
        root = Path(__file__).resolve().parents[3]
        startup = {
            "identity": self.identity,
            "sourceDigest": self._expected_source_digest(),
            "limits": self.limits,
            "workspace": str(self.workspace),
            "protocol": OWNER_PROTOCOL,
            "policy": self.policy,
        }
        write_private(self.directory / "startup.json", startup)
        log = private_file(self.directory / "owner.log")
        try:
            os.ftruncate(log, 0)
            child_env = without_control_credentials()
            child_env["PYTHONPATH"] = str(root)
            # Descriptor closure and a new session detach SSH stdio. Whether a
            # host also clears its login cgroup remains a deployment check.
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "omicsclaw.entry.ssh.notebook_owner",
                    "--workspace",
                    str(self.workspace),
                ],
                cwd=root,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            os.close(log)

    async def attach(self):
        health, _ = await self._attach_session()
        return health

    def _owner_is_idle(self) -> bool:
        import sqlite3

        db_path = self.workspace / ".omicsclaw/notebooks.db"
        if not db_path.is_file():
            return True
        try:
            with sqlite3.connect(
                f"file:{db_path}?mode=ro", uri=True, timeout=2.0
            ) as conn:
                active_kernel = conn.execute(
                    "SELECT 1 FROM notebook_runtime_kernels WHERE state NOT IN ('released','lost') LIMIT 1"
                ).fetchone()
                if active_kernel is not None:
                    return False
                active_exec = conn.execute(
                    "SELECT 1 FROM notebook_executions WHERE status IN ('queued','running') LIMIT 1"
                ).fetchone()
                return active_exec is None
        except sqlite3.Error:
            return False

    async def _spawn_and_wait(self, state_path: Path, previous_owner_id: str | None = None):
        await asyncio.to_thread(self._launch)
        for _ in range(200):
            try:
                state = read_private(state_path)
                if same_process(state.get("process")) and (
                    previous_owner_id is None or state.get("ownerId") != previous_owner_id
                ):
                    return state
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            await asyncio.sleep(0.05)
        raise NotebookError("notebook_owner_unavailable", 503)

    async def _attach_session(self):
        if self.closed:
            raise NotebookError("notebook_owner_unavailable", 503)
        async with self.lock:
            state_path = self.directory / "owner.json"
            try:
                state = read_private(state_path)
            except FileNotFoundError:
                state = None
            expected_digest = self._expected_source_digest()
            if (
                state is not None
                and same_process(state.get("process"))
                and state.get("sourceDigest") not in {None, expected_digest}
            ):
                proc = state.get("process")
                old_owner_id = state.get("ownerId")
                if same_process(proc):
                    import signal

                    try:
                        os.kill(proc["pid"], signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    for _ in range(40):
                        if not same_process(proc):
                            break
                        await asyncio.sleep(0.05)
                    if same_process(proc):
                        try:
                            os.kill(proc["pid"], signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                state = await self._spawn_and_wait(
                    state_path, previous_owner_id=old_owner_id
                )
            elif state is None or not same_process(state.get("process")):
                state = await self._spawn_and_wait(state_path)
            if (
                state.get("protocol") != OWNER_PROTOCOL
                or state.get("identity") != self.identity
            ):
                raise NotebookError("notebook_store_mismatch", 409)
            address = state.get("address")
            if (
                not isinstance(address, str)
                or not address.startswith("omicsclaw-notebook-")
                or len(address) > 100
            ):
                raise NotebookError("notebook_unsafe_state", 409)
            self.address = "\0" + address
            try:
                result = await self._request(
                    "attach",
                    {"identity": self.identity, "policyDigest": self.policy_digest},
                    address=self.address,
                    token=None,
                    owner=None,
                )
            except NotebookError as error:
                if error.code != "notebook_policy_changed" or not self._owner_is_idle():
                    raise
                proc = state.get("process")
                old_owner_id = state.get("ownerId")
                if same_process(proc):
                    import signal

                    try:
                        os.kill(proc["pid"], signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    for _ in range(40):
                        if not same_process(proc):
                            break
                        await asyncio.sleep(0.05)
                state = await self._spawn_and_wait(state_path, previous_owner_id=old_owner_id)
                address = state.get("address")
                if (
                    not isinstance(address, str)
                    or not address.startswith("omicsclaw-notebook-")
                    or len(address) > 100
                ):
                    raise NotebookError("notebook_unsafe_state", 409)
                self.address = "\0" + address
                result = await self._request(
                    "attach",
                    {"identity": self.identity, "policyDigest": self.policy_digest},
                    address=self.address,
                    token=None,
                    owner=None,
                )
            self.token = result.pop("leaseToken")
            self.health = result
            return result, {
                "address": self.address,
                "token": self.token,
                "owner": {k: result["owner"][k] for k in ("ownerId", "ownerEpoch")},
            }

    async def call(self, method, params, *, expected_owner):
        # Refresh authentication for every request; generation stays unchanged.
        _, connection = await self._attach_session()
        if connection["owner"] != expected_owner:
            raise NotebookError("notebook_owner_changed", 409)
        return await self._request(method, params, **connection)

    async def aclose(self):
        self.closed = True
        self.token = None
