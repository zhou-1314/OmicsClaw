"""Bound same-host transport for the original external-job executor."""

from __future__ import annotations

import asyncio
import os
import shlex
import stat
import tempfile
from pathlib import Path

from omicsclaw.tools.builtin.remote import ExecOutcome

from .ssh import (
    RemoteHostRefused,
    RemotePathRefused,
    RemoteTransferError,
    SystemSshSpawner,
    build_sftp_argv,
    build_ssh_argv,
    shell_quote,
    validate_host,
)


class LocalTargetSpawner:
    """Use native finite processes and owned files; never invoke ssh or sftp."""

    def __init__(self, root: Path, *, alias: str = "controller", process_spawner=None):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise RemotePathRefused("local_target_root_not_directory")
        self.alias = validate_host(alias)
        self._identity = self._root_identity()
        self._processes = process_spawner or SystemSshSpawner()
        self.staging_root = self._path(
            str(self.root / ".omicsclaw" / "control-staging")
        )
        self.staging_root.mkdir(parents=True, mode=0o700, exist_ok=True)

    async def spawn(
        self, argv: list[str], *, timeout: float, stdin_text: str | None = None
    ) -> ExecOutcome:
        if self._root_identity() != self._identity:
            raise RemotePathRefused("local_target_root_identity_changed")
        if argv and argv == build_ssh_argv(self.alias, argv[-1]):
            command = f"cd -- {shell_quote(str(self.root))} && " + argv[-1]
            return await self._processes.spawn(
                [
                    "env",
                    "-i",
                    "PATH=/usr/bin:/bin",
                    "LANG=C.UTF-8",
                    "/bin/bash",
                    "--noprofile",
                    "--norc",
                    "-c",
                    command,
                ],
                timeout=timeout,
                stdin_text=stdin_text,
            )
        if argv == build_sftp_argv(self.alias):
            async with asyncio.timeout(timeout):
                return await asyncio.to_thread(self._transfer, stdin_text or "")
        raise RemoteHostRefused("local_target_binding_mismatch")

    def _root_identity(self):
        observed = self.root.stat()
        return observed.st_dev, observed.st_ino, observed.st_uid

    def validate_workdir(self, value: str) -> None:
        """Refuse an unbound directory before the executor creates anything."""
        if self._root_identity() != self._identity:
            raise RemotePathRefused("local_target_root_identity_changed")
        self._path(value)

    def _path(self, value: str) -> Path:
        raw = Path(value)
        candidate = raw if raw.is_absolute() else self.root / raw
        resolved = candidate.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise RemotePathRefused("local_target_path_outside_root")
        current = candidate
        while current != self.root and current != current.parent:
            if current.is_symlink():
                raise RemotePathRefused("local_target_symlink_refused")
            current = current.parent
        return resolved

    def _transfer(self, batch: str) -> ExecOutcome:
        total = 0
        for line in batch.splitlines():
            tokens = shlex.split(line)
            if not tokens or tokens == ["exit"]:
                continue
            if len(tokens) != 4 or tokens[0] not in {"put", "get"} or tokens[1] != "--":
                raise RemotePathRefused("local_target_transfer_command_refused")
            source, destination = self._path(tokens[2]), self._path(tokens[3])
            if not source.is_file() or not destination.parent.is_dir():
                raise RemoteTransferError("local_target_transfer_path_missing")
            descriptor, temporary = tempfile.mkstemp(
                prefix=".omicsclaw-transfer-", dir=destination.parent
            )
            try:
                with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
                    before = os.fstat(reader.fileno())
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or before.st_size > 1024 * 1024 * 1024
                    ):
                        raise RemoteTransferError(
                            "local_target_transfer_budget_exceeded"
                        )
                    copied = 0
                    for chunk in iter(lambda: reader.read(64 * 1024), b""):
                        copied += len(chunk)
                        total += len(chunk)
                        if (
                            copied > 1024 * 1024 * 1024
                            or total > 2 * 1024 * 1024 * 1024
                        ):
                            raise RemoteTransferError(
                                "local_target_transfer_budget_exceeded"
                            )
                        writer.write(chunk)
                    after = os.fstat(reader.fileno())
                    fields = (
                        "st_dev",
                        "st_ino",
                        "st_size",
                        "st_mtime_ns",
                        "st_ctime_ns",
                    )
                    if copied != before.st_size or tuple(
                        getattr(before, key) for key in fields
                    ) != tuple(getattr(after, key) for key in fields):
                        raise RemoteTransferError(
                            "local_target_transfer_source_changed"
                        )
                    writer.flush()
                    os.fsync(writer.fileno())
                if (
                    self._path(str(destination)) != destination
                    or self._root_identity() != self._identity
                ):
                    raise RemotePathRefused("local_target_transfer_identity_changed")
                os.replace(temporary, destination)
            finally:
                Path(temporary).unlink(missing_ok=True)
        return ExecOutcome(output="", exit_code=0, host=self.alias)
