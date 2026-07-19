"""Trusted child entrypoint for one governed AutoAgent harness execution."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sys
import threading
from typing import Any, BinaryIO, Mapping

# The Backend may be launched from an arbitrary active Workspace without an
# editable install. This file is invoked by one strict absolute path; bind
# imports to that same checked-out source tree rather than ambient PYTHONPATH.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from omicsclaw.autoagent.process_owner import (
    GovernedWorkerProtocolError,
    WORKER_REQUEST_KEYS,
    decode_worker_frame,
    encode_worker_frame,
)


_HEADER_BYTES = 4
_MAX_FRAME_BYTES = 4 * 1024 * 1024 + 64 * 1024
_MAX_REQUEST_BYTES = 1024 * 1024


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise GovernedWorkerProtocolError("worker IPC closed mid-frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_frame(stream: BinaryIO, *, max_bytes: int) -> dict[str, Any]:
    header = _read_exact(stream, _HEADER_BYTES)
    size = int.from_bytes(header, byteorder="big", signed=False)
    if size <= 0 or size > max_bytes:
        raise GovernedWorkerProtocolError("worker frame exceeds its byte limit")
    return decode_worker_frame(_read_exact(stream, size), max_bytes=max_bytes)


def _write_frame(
    stream: BinaryIO,
    payload: Mapping[str, Any],
    *,
    max_bytes: int = _MAX_FRAME_BYTES,
) -> None:
    stream.write(encode_worker_frame(payload, max_bytes=max_bytes))
    stream.flush()


def _validated_request(frame: Mapping[str, Any]) -> dict[str, Any]:
    if set(frame) != {"version", "kind", "payload"}:
        raise GovernedWorkerProtocolError("worker request frame is invalid")
    if frame.get("version") != 1 or frame.get("kind") != "request":
        raise GovernedWorkerProtocolError("worker request frame is invalid")
    payload = frame.get("payload")
    if not isinstance(payload, dict) or set(payload) != WORKER_REQUEST_KEYS:
        raise GovernedWorkerProtocolError("worker request payload is invalid")
    if payload.get("auto_promote") is not False:
        raise GovernedWorkerProtocolError("worker request must disable auto promotion")
    # Round-trip before running so no non-finite/non-JSON value reaches the
    # harness through a looser in-process call path.
    json.dumps(payload, allow_nan=False)
    return dict(payload)


def _run(stream: BinaryIO) -> int:
    _write_frame(
        stream,
        {"version": 1, "kind": "hello"},
        max_bytes=4096,
    )
    challenge = _read_frame(stream, max_bytes=4096)
    nonce = challenge.get("nonce")
    if (
        set(challenge) != {"version", "kind", "nonce"}
        or challenge.get("version") != 1
        or challenge.get("kind") != "challenge"
        or not isinstance(nonce, str)
        or len(nonce) != 64
        or any(character not in "0123456789abcdef" for character in nonce)
    ):
        raise GovernedWorkerProtocolError("worker IPC challenge is invalid")
    _write_frame(
        stream,
        {
            "version": 1,
            "kind": "challenge_response",
            "nonce": nonce,
        },
        max_bytes=4096,
    )
    request = _validated_request(_read_frame(stream, max_bytes=_MAX_REQUEST_BYTES))

    def emit(event_type: str, data: dict[str, Any]) -> None:
        if event_type in {"done", "error"}:
            return
        try:
            _write_frame(
                stream,
                {
                    "version": 1,
                    "kind": "event",
                    "event_type": event_type,
                    "data": data,
                },
                max_bytes=256 * 1024,
            )
        except GovernedWorkerProtocolError:
            # Oversized/non-JSON progress is non-authoritative.  The final
            # result remains mandatory and independently bounded.
            return

    try:
        from omicsclaw.autoagent import run_harness_evolution

        result = run_harness_evolution(
            **request,
            on_event=emit,
            cancel_event=threading.Event(),
        )
    except BaseException:
        _write_frame(
            stream,
            {
                "version": 1,
                "kind": "terminal",
                "status": "error",
                "error_code": "worker_crashed",
            },
        )
        return 1
    if not isinstance(result, dict) or result.get("success") is not True:
        _write_frame(
            stream,
            {
                "version": 1,
                "kind": "terminal",
                "status": "error",
                "error_code": "harness_failed",
            },
        )
        return 0
    try:
        _write_frame(
            stream,
            {
                "version": 1,
                "kind": "terminal",
                "status": "done",
                "result": result,
            },
        )
    except GovernedWorkerProtocolError:
        _write_frame(
            stream,
            {
                "version": 1,
                "kind": "terminal",
                "status": "error",
                "error_code": "invalid_terminal_result",
            },
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--ipc-address", required=True)
    try:
        args = parser.parse_args(argv)
        if not args.ipc_address.startswith("@") or len(args.ipc_address) > 100:
            return 125
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(10.0)
        connection.connect("\0" + args.ipc_address[1:])
        connection.settimeout(None)
        with connection, connection.makefile("rwb", buffering=0) as stream:
            # Harness/subprocess diagnostics are not IPC and must not create an
            # unbounded parent capture.  Authoritative progress uses frames.
            with open(os.devnull, "w", encoding="utf-8") as sink:
                sys.stdout = sink
                sys.stderr = sink
                return _run(stream)
    except (GovernedWorkerProtocolError, OSError, ValueError):
        return 125


if __name__ == "__main__":  # pragma: no cover - subprocess entrypoint
    raise SystemExit(main())
