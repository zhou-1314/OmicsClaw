"""Detached Notebook owner entry; no agent application or HTTP server."""

import argparse
import asyncio
import fcntl
import logging
import os
import signal
import uuid
from pathlib import Path

from omicsclaw.entry.environment_coordination import (
    EnvironmentCoordinator,
    coordinator_directory,
)
from omicsclaw.entry.notebook.contracts import OWNER_PROTOCOL
from omicsclaw.entry.notebook.policy import decode_policy
from omicsclaw.entry.notebook.runtime import OwnerRuntime
from omicsclaw.entry.notebook.security import (
    private_directory,
    private_file,
    process_evidence,
    read_private,
    write_private,
)
from omicsclaw.memory.database import Database
from omicsclaw.memory.turn_events import DurableTurnEventLog
from omicsclaw.tools.builtin.bash import without_control_credentials

from .manifest import verified_source_metadata
from .notebook_ipc import MAX_IPC_BYTES, OwnerServer


class BoundedLog(logging.Handler):
    def __init__(self, path):
        super().__init__(logging.WARNING)
        self.fd = private_file(path)

    def emit(self, record):
        msg = record.getMessage().replace("\n", " ")[:512]
        data = (record.levelname + " " + record.name + ": " + msg + "\n").encode()[:1024]
        if os.fstat(self.fd).st_size + len(data) > 1024 * 1024:
            os.ftruncate(self.fd, 0)
        os.lseek(self.fd, 0, os.SEEK_END)
        os.write(self.fd, data)


async def serve(workspace):
    workspace = workspace.resolve(strict=True)
    directory = private_directory(workspace / ".omicsclaw/notebook-owner")
    fd = private_file(directory / "owner.lock")
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # Bound diagnostics without imposing a file limit on scientific outputs.
        logging.getLogger().addHandler(BoundedLog(directory / "owner.log"))
        startup = read_private(directory / "startup.json")
        if startup["protocol"] != OWNER_PROTOCOL or startup["workspace"] != str(
            workspace
        ):
            raise ValueError("notebook_store_mismatch")
        root = Path(__file__).resolve().parents[3]
        if (root / "controller-manifest.json").exists():
            metadata = verified_source_metadata(root)
            if metadata["sourceFilesHash"] != startup["sourceDigest"]:
                raise ValueError("notebook_store_mismatch")
        elif startup["sourceDigest"] != "development-unverified":
            raise ValueError("notebook_store_mismatch")
        database = Database(workspace / ".omicsclaw/memory.db")
        try:
            identity = DurableTurnEventLog(database).identity
        finally:
            database.close()
        trusted = {
            "authorityId": identity.authority_id,
            "workspaceId": identity.workspace_id,
        }
        if trusted != startup["identity"]:
            raise ValueError("notebook_store_mismatch")
        runtime = OwnerRuntime(
            workspace,
            trusted,
            source_digest=startup["sourceDigest"],
            limits=startup["limits"],
            config=decode_policy(startup["policy"]),
            env=without_control_credentials(),
            environment_coordinator=EnvironmentCoordinator(coordinator_directory()),
        )
        await runtime.start()
        endpoint = "omicsclaw-notebook-" + uuid.uuid4().hex
        handler = OwnerServer(runtime)
        server = await asyncio.start_unix_server(
            handler.handle, path="\0" + endpoint, limit=MAX_IPC_BYTES
        )
        write_private(
            directory / "owner.json",
            {
                "protocol": OWNER_PROTOCOL,
                "identity": trusted,
                "ownerId": runtime.owner_id,
                "ownerEpoch": runtime.epoch,
                "address": endpoint,
                "sourceDigest": runtime.source_digest,
                "process": process_evidence(os.getpid()),
            },
        )
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stopped.set)
        try:
            async with server:
                await stopped.wait()
        finally:
            await runtime.close()
    except Exception as exc:
        logging.getLogger("omicsclaw.notebook_owner").error(
            "%s: %s", type(exc).__name__, getattr(exc, "code", str(exc))
        )
        raise
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(serve(args.workspace))


if __name__ == "__main__":
    main()
