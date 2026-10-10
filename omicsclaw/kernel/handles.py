"""AnnData handles across the host/kernel boundary, with revision gating.

The sandwich the plan took from OmicOS ``session_store.py`` +
``adata_kernel_runtime.py``: the caller (a job, a tool result) only ever
sees an ``adata_id`` and a one-line shape summary; the object itself
crosses as an h5ad snapshot, and the kernel holds it under
``OV_KERNEL_ADATA[adata_id]`` — a plain dict in the kernel namespace,
populated by :meth:`HandleRegistry.sync_in`.

**Revision gating is the half OmicOS got wrong** (its agent face does a
full h5ad round-trip every turn and its own comment expects that to fall
over at 170K features). A handle records the host-side revision —
``(st_mtime_ns, st_size)`` of the h5ad the kernel actually read — and a
``sync_in`` whose revision matches the resident one is a **zero-copy
skip**: no read, no write, the kernel keeps the object it already has.
The host file's mtime is the version, which is honest for the inputs the
job face declares (``inputs.adata_path`` names a file on the workspace)
and requires nothing of the caller but a stat.

**Cross-session access is refused, not routed.** A handle belongs to the
session whose kernel holds it; a second session asking for the same id
gets :exc:`HandleError`, because silently handing a second kernel the
same mutable object through a fresh file read is how two analyses
diverge while both believe they are looking at one thing.

**This round's scope.** The job face ``code_run`` uses ``sync_in`` (the
job declares ``adata_path``, the kernel gets ``OV_KERNEL_ADATA`` and the
convenience binding ``adata``); ``sync_out`` writes ``*.out.h5ad`` back
to the host for callers that ask. The chat face never crosses this
bridge — its variables live in the session kernel's namespace already,
which is the entire point of a persistent kernel there. R cells would
cross the same seam when an R kernel lands (via IRkernel, same
jupyter_client channel); today R stays on the step-runner subprocess.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["HandleError", "HandleRegistry", "KernelHandle", "OV_KERNEL_VAR"]

OV_KERNEL_VAR: str = "OV_KERNEL_ADATA"
"""The dict in the kernel namespace where resident handles live."""


class HandleError(ValueError):
    """A refused handle operation: unknown, cross-session, or unreadable."""


@dataclass(slots=True)
class KernelHandle:
    """One resident object: who holds it, at which host revision."""

    adata_id: str
    session_id: str
    revision: tuple[int, int]
    summary: str = ""
    synced_at: float = field(default_factory=time.time)

    def as_payload(self) -> dict[str, Any]:
        return {
            "adata_id": self.adata_id,
            "revision": list(self.revision),
            "summary": self.summary,
        }


def _revision(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_size)


class HandleRegistry:
    """The table of resident AnnData handles, per session and revision.

    Entries are dropped when their kernel is recycled
    (:meth:`drop_session`) — a registry row is a claim that a live kernel
    holds the object, and a recycled kernel holds nothing.
    """

    def __init__(self) -> None:
        self._handles: dict[str, KernelHandle] = {}

    # ---- reads ----

    def get(self, adata_id: str) -> KernelHandle | None:
        return self._handles.get(adata_id)

    def for_session(self, session_id: str) -> list[KernelHandle]:
        return [h for h in self._handles.values() if h.session_id == session_id]

    def __len__(self) -> int:
        return len(self._handles)

    # ---- the bridge ----

    def sync_in(
        self,
        adata_id: str,
        source: Any,
        *,
        session_id: str,
        kernel: Any,
    ) -> dict[str, Any]:
        """Make ``OV_KERNEL_ADATA[adata_id]`` resident in *kernel*.

        *source* is an h5ad path (str or Path) or an in-memory object
        with ``write_h5ad`` (an AnnData); the latter is snapshotted to
        ``<workspace>/.omicsclaw/kernel_sync/<id>.sync.h5ad`` first, which
        is where the ``*.sync.h5ad`` naming of the plan lives. A path
        source is read where it lies — the file *is* the snapshot, and
        copying it would only double the IO the gate exists to avoid.

        Returns ``{adata_id, variable, skipped, revision, summary}``;
        ``skipped=True`` means the revision matched and nothing was read.
        *kernel* is anything with ``execute(code, timeout_s=...) ->
        CellResult`` — the manager always passes the live
        :class:`~omicsclaw.kernel.session.SessionKernel`. The h5ad read
        itself happens inside the kernel (the registry only ships code
        and snapshots); tests exercise the gating with utime, not by
        injecting a reader.
        """
        existing = self._handles.get(adata_id)
        if existing is not None and existing.session_id != session_id:
            raise HandleError(
                f"adata handle {adata_id!r} belongs to session "
                f"{existing.session_id!r}; cross-session access is refused"
            )
        path, snapshot = self._materialise(source, adata_id, kernel)
        revision = _revision(path)
        if existing is not None and existing.revision == revision:
            return {
                "adata_id": adata_id,
                "variable": f"{OV_KERNEL_VAR}[{adata_id!r}]",
                "skipped": True,
                "revision": list(revision),
                "summary": existing.summary,
            }
        code = (
            "import anndata as _omicsclaw_anndata\n"
            f"{OV_KERNEL_VAR} = globals().setdefault({OV_KERNEL_VAR!r}, dict())\n"
            f"{OV_KERNEL_VAR}[{adata_id!r}] = _omicsclaw_anndata.read_h5ad("
            f"{str(path)!r})\n"
            f"print({OV_KERNEL_VAR}[{adata_id!r}])\n"
        )
        outcome = kernel.execute(code, timeout_s=600.0)
        summary = outcome.stdout.strip().splitlines()[-1] if outcome.stdout.strip() else ""
        if outcome.status in ("error", "timeout", "dead"):
            reason = (outcome.error or {}).get("evalue") or outcome.status
            raise HandleError(f"syncing {adata_id!r} into the kernel failed: {reason}")
        handle = KernelHandle(
            adata_id=adata_id,
            session_id=session_id,
            revision=revision,
            summary=summary[:200],
        )
        self._handles[adata_id] = handle
        return {
            "adata_id": adata_id,
            "variable": f"{OV_KERNEL_VAR}[{adata_id!r}]",
            "skipped": False,
            "revision": list(revision),
            "summary": handle.summary,
        }

    def sync_out(
        self,
        adata_id: str,
        destination: Any,
        *,
        session_id: str,
        kernel: Any,
        workspace: Path,
    ) -> Path:
        """Write the resident object back to the host as ``*.out.h5ad``.

        *destination* names where (a directory, or a path without the
        ``.out.h5ad`` suffix); the actual file always ends in
        ``.out.h5ad`` so a sync-out can never be confused with its input.
        """
        handle = self._handles.get(adata_id)
        if handle is None:
            raise HandleError(f"adata handle {adata_id!r} is not resident")
        if handle.session_id != session_id:
            raise HandleError(
                f"adata handle {adata_id!r} belongs to session "
                f"{handle.session_id!r}; cross-session access is refused"
            )
        target = Path(destination).expanduser()
        if not target.is_absolute():
            target = workspace / target
        if target.suffix == "":
            target = target / adata_id
        target = target.with_name(target.name + ".out.h5ad")
        target.parent.mkdir(parents=True, exist_ok=True)
        code = (
            f"{OV_KERNEL_VAR}[{adata_id!r}].write_h5ad({str(target)!r})\n"
            f"print('wrote', {str(target)!r})\n"
        )
        outcome = kernel.execute(code, timeout_s=600.0)
        if outcome.status in ("error", "timeout", "dead"):
            reason = (outcome.error or {}).get("evalue") or outcome.status
            raise HandleError(f"syncing {adata_id!r} out failed: {reason}")
        try:
            handle.revision = _revision(target)
        except OSError:
            pass  # the kernel reported success; the stat is best-effort bookkeeping
        return target

    # ---- maintenance ----

    def drop_session(self, session_id: str) -> list[str]:
        """Forget every handle of *session_id*; answers the dropped ids.

        Called when a kernel is recycled: the new kernel's namespace is
        empty, and a registry row claiming otherwise would turn the next
        ``sync_in`` into a zero-copy skip onto an object that is gone.
        """
        dropped = [
            adata_id
            for adata_id, handle in self._handles.items()
            if handle.session_id == session_id
        ]
        for adata_id in dropped:
            self._handles.pop(adata_id, None)
        return dropped

    # ---- internals ----

    def _materialise(
        self, source: Any, adata_id: str, kernel: Any
    ) -> tuple[Path, bool]:
        """Resolve *source* to an h5ad path, snapshotting memory objects."""
        if isinstance(source, (str, Path)):
            path = Path(source).expanduser()
            if not path.is_file():
                raise HandleError(f"adata source file not found: {path}")
            return path, False
        workspace = Path(getattr(kernel, "workspace", "."))
        directory = workspace / ".omicsclaw" / "kernel_sync"
        directory.mkdir(parents=True, exist_ok=True)
        snapshot = directory / f"{adata_id}.sync.h5ad"
        try:
            source.write_h5ad(snapshot)
        except AttributeError as exc:
            raise HandleError(
                "adata source must be an h5ad path or an object with write_h5ad"
            ) from exc
        return snapshot, True
