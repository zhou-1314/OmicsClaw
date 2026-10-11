"""The remote-execution host routes: ``/remote/hosts*``, C1's REST face.

Mounted into :func:`~omicsclaw.entry.desktop.server.create_desktop_app`
by :func:`mount_remote_host_routes`, in the style the jobs routes set:
every route checks the bearer token itself, refuses a body that is not
exactly one ``application/json``, reads it under a running cap, and
answers a refusal as ``{"detail": <code>}`` with the code's status. The
knowledge itself lives in the plane's
:class:`~omicsclaw.remote.store.RemoteHostStore`; this module is the
adapter, and imports FastAPI inside the mounting function for the same
reason ``server.py`` does.

Three routes over the host knowledge base:

* ``GET /remote/hosts`` — every stored host as a card payload
  (:func:`card_payload`); an empty knowledge base answers ``[]``.
* ``POST /remote/hosts`` ``{alias}`` — probe (or re-probe) one alias
  with real SSH under :data:`HOSTS_PROBE_BUDGET_S`, store the fresh
  card, and answer it as ``201``. A transport that cannot reach the
  host answers ``400 {"error": "unreachable", "detail": <sanitized>}``
  rather than a stack trace; an alias that fails
  :func:`~omicsclaw.tools.builtin.remote.valid_host` is ``422``
  ``invalid_host`` — the same closed-alphabet rule every ``host``
  argument obeys, because the alias reaches an ``ssh`` argv unchanged.
* ``DELETE /remote/hosts/{alias}`` — forget one host; ``204``, or
  ``404`` when nothing was stored under that alias.

A deployment with ``remote_execution`` off has no plane bound, and all
three routes answer ``503 remote_execution_disabled``: the routes exist
(the wire contract lists them) but the knowledge base is not open, and
answering ``[]`` would tell a client "no hosts" when the truth is "no
feature". ``GET /health``'s ``capabilities.remote_execution`` is how a
client knows not to ask.
"""

# This module deliberately does NOT enable `from __future__ import
# annotations`: the routes are defined inside mount_remote_host_routes, where
# Request is a local name; with postponed evaluation FastAPI would see the
# string "Request", fail to resolve it in this module's globals and treat the
# parameter as a required query parameter, so every route would answer 422 —
# the same trap server.py documents for itself.

import asyncio
import glob
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Callable, Final

from omicsclaw.remote.ssh import (
    RemoteHostRefused,
    RemoteHostUnreachable,
    RemotePathRefused,
    RemoteTransferError,
)
from omicsclaw.tools.builtin.remote import HostCard, valid_host
from omicsclaw.remote.plane import REMOTE_FILE_PREVIEW_MAX_BYTES, REMOTE_TREE_MAX_ENTRIES

from .turn_submission import DesktopIngressError

__all__ = [
    "HOSTS_MAX_REQUEST_BYTES",
    "HOSTS_PROBE_BUDGET_S",
    "card_payload",
    "list_ssh_aliases",
    "mount_remote_host_routes",
    "sanitize_probe_detail",
]

HOSTS_PROBE_BUDGET_S: Final = 30.0
"""How long one ``POST /remote/hosts`` waits for the probe to answer.

The route's own budget, distinct from the plane's per-command
``PROBE_TIMEOUT_S``: a client of a desktop UI should not have a request
open for a minute, and ``asyncio.wait_for`` closes the outer wait even
when the inner command is still inside its own, larger budget."""

HOSTS_MAX_REQUEST_BYTES: Final = 16 * 1024
"""Largest body ``POST /remote/hosts`` reads: one alias and nothing else."""

_DETAIL_MAX_CHARS: Final = 300
_MAX_TREE_DEPTH: Final = 4


def list_ssh_aliases(home: str | Path | None = None) -> list[str]:
    """Return concrete ``Host`` aliases from the Backend's SSH config.

    Suggestions must come from the machine that will run the probe. Globs,
    negated patterns and the catch-all ``Host *`` are matching rules, not
    destinations, so they are intentionally omitted.
    """
    root = Path(home).expanduser() if home is not None else Path.home()
    aliases: list[str] = []
    seen: set[str] = set()
    visited: set[Path] = set()

    def read(path: Path) -> None:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            return
        if resolved in visited:
            return
        visited.add(resolved)
        try:
            lines = resolved.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return
        for raw in lines:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            match = re.match(r"(?i)^include\s+(.+)$", line)
            if match:
                for pattern in match.group(1).split():
                    expanded = os.path.expanduser(pattern)
                    if not os.path.isabs(expanded):
                        expanded = str(resolved.parent / expanded)
                    for included in sorted(Path(p) for p in glob.glob(expanded)):
                        read(included)
                continue
            match = re.match(r"(?i)^host(?:\s+|=)(.+)$", line)
            if not match:
                continue
            for candidate in match.group(1).split():
                if any(token in candidate for token in "*?!"):
                    continue
                if valid_host(candidate) and candidate not in seen:
                    seen.add(candidate)
                    aliases.append(candidate)

    read(root / ".ssh" / "config")
    return aliases


def card_payload(card: HostCard) -> dict[str, Any]:
    """One :class:`HostCard` as the REST face answers it. Pure.

    The shape is the tool card's — alias, the probe's facts
    (``hostname``/``platform``/``nproc``/``memory_mb``/``gpus``/
    ``commands``/``slurm_partitions``/``scratch_root``), the
    ``notes`` ``ask_about_host`` collected, and ``parse_error`` for a
    host that answered but not in the probe's grammar — plus the two
    bookkeeping fields a list view wants: ``probed`` (whether the
    knowledge base holds a probe result at all — parsed facts or a
    parse error; a row only ``ask_about_host`` created answers false)
    and ``last_probed_at``.
    """
    return {
        "alias": card.alias,
        "probed": bool(card.hostname) or bool(card.parse_error),
        "hostname": card.hostname,
        "platform": card.platform,
        "nproc": card.nproc,
        "memory_mb": card.memory_mb,
        "gpus": list(card.gpus),
        "commands": dict(card.commands),
        "slurm_partitions": list(card.slurm_partitions),
        "scratch_root": card.scratch_root,
        "last_probed_at": card.last_probed_at,
        "notes": [{**note} for note in card.notes],
        "parse_error": card.parse_error,
    }


def sanitize_probe_detail(text: str) -> str:
    """Make a transport failure safe to hand to a desktop client.

    The probe's failures are ``ssh``'s own merged output, which is
    diagnostic gold ("Connection timed out", "Permission denied
    (publickey)") but can carry this user's home path and arbitrarily
    many lines of banner noise. Both are trimmed here: whitespace
    collapses, the local home directory becomes ``~``, and the tail past
    :data:`_DETAIL_MAX_CHARS` is dropped — enough to act on, never
    enough to hide a wall of text behind a 4xx badge.
    """
    home = str(Path.home())
    if home and home != "/":
        text = text.replace(home, "~")
    collapsed = " ".join(text.split())
    if len(collapsed) > _DETAIL_MAX_CHARS:
        collapsed = collapsed[:_DETAIL_MAX_CHARS].rstrip() + "…"
    return collapsed


def mount_remote_host_routes(
    api: Any,
    plane: Any,
    *,
    authorized: Callable[[Any], bool],
) -> None:
    """Define the ``/remote/hosts*`` routes on *api*.

    *plane* is the app's remote binding — ``None`` on a deployment with
    ``remote_execution`` off, which every route answers with
    ``503 remote_execution_disabled``. *authorized* is the mounting
    application's bearer check, so these routes answer 401 exactly like
    the chat routes do.
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse, Response

    from .server import is_json_media_type

    def _unauthorized() -> Any:
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    def _refused(exc: DesktopIngressError) -> Any:
        return JSONResponse({"detail": exc.code}, status_code=exc.status_code)

    def _disabled() -> Any:
        return JSONResponse(
            {"detail": "remote_execution_disabled"}, status_code=503
        )

    async def _read_json(request: Request) -> dict[str, Any]:
        declared = request.headers.getlist("content-type")
        if len(declared) != 1 or not is_json_media_type(declared[0]):
            raise DesktopIngressError("unsupported_media_type", status_code=415)
        declared_length = request.headers.getlist("content-length")
        if declared_length:
            try:
                length = int(declared_length[0])
            except ValueError as exc:
                raise DesktopIngressError(
                    "invalid_content_length", status_code=400
                ) from exc
            if length < 0 or length > HOSTS_MAX_REQUEST_BYTES:
                raise DesktopIngressError(
                    "request_document_too_large", status_code=413
                )
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > HOSTS_MAX_REQUEST_BYTES:
                raise DesktopIngressError(
                    "request_document_too_large", status_code=413
                )
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise DesktopIngressError("invalid_json", status_code=400) from exc
        if not isinstance(document, dict):
            raise DesktopIngressError("invalid_json_body", status_code=400)
        return document

    @api.get("/remote/hosts")
    async def list_hosts(request: Request) -> Any:
        if not authorized(request):
            return _unauthorized()
        if plane is None:
            return _disabled()
        return [card_payload(card) for card in plane.list_host_cards()]

    @api.get("/remote/hosts/aliases")
    async def list_aliases(request: Request) -> Any:
        if not authorized(request):
            return _unauthorized()
        if plane is None:
            return _disabled()
        return {"aliases": list_ssh_aliases()}

    @api.post("/remote/hosts")
    async def add_host(request: Request) -> Any:
        if not authorized(request):
            return _unauthorized()
        if plane is None:
            return _disabled()
        try:
            document = await _read_json(request)
        except DesktopIngressError as exc:
            return _refused(exc)
        alias = document.get("alias")
        if not isinstance(alias, str) or not valid_host(alias):
            return JSONResponse({"detail": "invalid_host"}, status_code=422)
        try:
            card = await asyncio.wait_for(
                plane.probe_card(alias), timeout=HOSTS_PROBE_BUDGET_S
            )
        except (
            RemoteHostUnreachable,
            RemoteTransferError,
            TimeoutError,
            OSError,
        ) as exc:
            return JSONResponse(
                {"error": "unreachable", "detail": sanitize_probe_detail(str(exc))},
                status_code=400,
            )
        except RemoteHostRefused as exc:
            return JSONResponse(
                {"detail": f"invalid_host: {sanitize_probe_detail(str(exc))}"},
                status_code=422,
            )
        return JSONResponse(card_payload(card), status_code=201)

    @api.delete("/remote/hosts/{alias}")
    async def delete_host(request: Request, alias: str) -> Any:
        if not authorized(request):
            return _unauthorized()
        if plane is None:
            return _disabled()
        if not valid_host(alias):
            return JSONResponse({"detail": "invalid_host"}, status_code=422)
        if not plane.host_store().delete(alias):
            return JSONResponse({"detail": "not_found"}, status_code=404)
        return Response(status_code=204)

    @api.get("/remote/hosts/{alias}/files/tree")
    async def list_target_files(request: Request, alias: str) -> Any:
        if not authorized(request):
            return _unauthorized()
        if plane is None:
            return _disabled()
        if not valid_host(alias):
            return JSONResponse({"detail": "invalid_host"}, status_code=422)
        path = request.query_params.get("path", "")
        workdir = request.query_params.get("workdir", "")
        try:
            depth = int(request.query_params.get("depth", "2"))
        except ValueError:
            return JSONResponse({"detail": "invalid_depth"}, status_code=422)
        if depth < 1 or depth > _MAX_TREE_DEPTH:
            return JSONResponse({"detail": "invalid_depth"}, status_code=422)
        if path.startswith("/") or any(part == ".." for part in path.split("/")):
            return JSONResponse({"detail": "path_outside_workdir"}, status_code=409)
        try:
            root, entries, has_more = await plane.list_target_files(
                alias, workdir=workdir, path=path, depth=depth
            )
        except KeyError:
            return JSONResponse({"detail": "remote_host_not_found"}, status_code=404)
        except RemoteHostRefused:
            return JSONResponse({"detail": "invalid_host"}, status_code=422)
        except RemotePathRefused as exc:
            if str(exc).startswith("workdir_unavailable"):
                detail = "workdir_unavailable"
            elif str(exc).startswith("remote_files_unsupported"):
                detail = "remote_files_unsupported"
            else:
                detail = "path_outside_workdir"
            return JSONResponse({"detail": detail}, status_code=409)
        except (RemoteHostUnreachable, RemoteTransferError, OSError) as exc:
            return JSONResponse(
                {"error": "unreachable", "detail": sanitize_probe_detail(str(exc))},
                status_code=400,
            )
        return {"alias": alias, "root": root, "entries": entries, "truncated": has_more, "has_more": has_more, "max_entries": REMOTE_TREE_MAX_ENTRIES}

    @api.get("/remote/hosts/{alias}/files/serve")
    async def serve_target_file(request: Request, alias: str) -> Any:
        if not authorized(request):
            return _unauthorized()
        if plane is None:
            return _disabled()
        if not valid_host(alias):
            return JSONResponse({"detail": "invalid_host"}, status_code=422)
        path = request.query_params.get("path", "")
        workdir = request.query_params.get("workdir", "")
        if not path or path.startswith("/") or any(part == ".." for part in path.split("/")):
            return JSONResponse({"detail": "path_outside_workdir"}, status_code=409)
        try:
            _, total_size = await plane.stat_target_file(alias, workdir=workdir, path=path)
        except KeyError:
            return JSONResponse({"detail": "remote_host_not_found"}, status_code=404)
        except RemoteHostRefused:
            return JSONResponse({"detail": "invalid_host"}, status_code=422)
        except RemotePathRefused as exc:
            if str(exc).startswith("workdir_unavailable"):
                detail = "workdir_unavailable"
            elif str(exc).startswith("remote_files_unsupported"):
                detail = "remote_files_unsupported"
            else:
                detail = "path_outside_workdir"
            return JSONResponse({"detail": detail}, status_code=409)
        except (RemoteHostUnreachable, RemoteTransferError, OSError) as exc:
            return JSONResponse(
                {"error": "unreachable", "detail": sanitize_probe_detail(str(exc))},
                status_code=400,
            )

        range_header = request.headers.get("range", "").strip()
        start = 0
        end = total_size - 1
        status_code = 200
        content_range = ""
        if range_header:
            match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", range_header)
            if not match or (not match.group(1) and not match.group(2)):
                return Response(status_code=416, headers={"Content-Range": f"bytes */{total_size}"})
            first, last = match.groups()
            if first:
                start = int(first)
                end = int(last) if last else total_size - 1
            else:
                suffix = int(last)
                start = max(total_size - suffix, 0) if suffix else total_size
                end = total_size - 1
            if start >= total_size or end < start or end >= total_size:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{total_size}"})
            if end - start + 1 > REMOTE_FILE_PREVIEW_MAX_BYTES:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{total_size}"})
            status_code = 206
            content_range = f"bytes {start}-{end}/{total_size}"
        elif total_size > REMOTE_FILE_PREVIEW_MAX_BYTES:
            return JSONResponse({"detail": "preview_too_large"}, status_code=413)

        try:
            _, content, _ = await plane.read_target_file(
                alias,
                workdir=workdir,
                path=path,
                start=start,
                count=end - start + 1,
            )
        except RemotePathRefused as exc:
            if str(exc).startswith("workdir_unavailable"):
                detail = "workdir_unavailable"
            elif str(exc).startswith("remote_files_unsupported"):
                detail = "remote_files_unsupported"
            else:
                detail = "path_outside_workdir"
            return JSONResponse({"detail": detail}, status_code=409)
        except ValueError:
            return JSONResponse({"detail": "preview_too_large"}, status_code=413)
        except (RemoteHostUnreachable, RemoteTransferError, OSError) as exc:
            return JSONResponse(
                {"error": "unreachable", "detail": sanitize_probe_detail(str(exc))},
                status_code=400,
            )
        media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", path.rsplit("/", 1)[-1]) or "download"
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(len(content)),
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
        if content_range:
            headers["Content-Range"] = content_range
        return Response(
            content=content,
            media_type=media_type,
            status_code=status_code,
            headers=headers,
        )
