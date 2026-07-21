"""Process-global resource admission shared by every scientific executor.

The scheduler accounts declared reservations; it is deliberately not an OS
quota or a predictor of data-size-dependent peak usage. One live event loop
owns the process scheduler; a second concurrent loop fails closed instead of
creating an independent capacity pool.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any, Mapping


_REQUEST_FIELDS = frozenset(
    {
        "cpu_cores",
        "memory_mib",
        "gpu_devices",
        "threads",
        "temporary_disk_mib",
    }
)
_GPU_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_.:/-]+$")
_PROCESS_SCHEDULER: ExecutionResourceScheduler | None = None
_PROCESS_SCHEDULER_LOOP: asyncio.AbstractEventLoop | None = None


class ResourceConfigurationError(ValueError):
    """Raised when a request or process budget is internally invalid."""


class NestedResourceAcquisitionError(ResourceConfigurationError):
    """Raised when a Run that already holds a global Lease acquires another.

    ADR 0062: a governed Run must not submit a second global resource ticket
    while it still holds part of its envelope. Enqueueing the reentrant ticket
    would let the child wait for capacity the parent still holds while the
    parent waits for the child — a strict-FIFO deadlock that cancellation
    cannot safely break. The scheduler fails such an acquisition closed and
    immediately, before it enqueues, instead of deadlocking the FIFO.
    """

    def __init__(self, run_id: str) -> None:
        super().__init__(
            f"Run {run_id!r} already holds a global Resource Lease; a nested "
            "global acquisition would deadlock the strict-FIFO scheduler"
        )
        self.run_id = run_id


@dataclass(frozen=True, slots=True)
class ExecutionResourceRequest:
    cpu_cores: int
    memory_mib: int
    gpu_devices: int
    threads: int
    temporary_disk_mib: int

    def __post_init__(self) -> None:
        positive = {
            "cpu_cores": self.cpu_cores,
            "memory_mib": self.memory_mib,
            "threads": self.threads,
        }
        for name, value in positive.items():
            _require_integer(name, value, minimum=1)
        _require_integer("gpu_devices", self.gpu_devices, minimum=0)
        _require_integer(
            "temporary_disk_mib",
            self.temporary_disk_mib,
            minimum=0,
        )
        if self.threads > self.cpu_cores:
            raise ResourceConfigurationError("threads cannot exceed reserved cpu_cores")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ExecutionResourceRequest:
        if set(value) != _REQUEST_FIELDS:
            missing = sorted(_REQUEST_FIELDS - set(value))
            unknown = sorted(set(value) - _REQUEST_FIELDS)
            raise ResourceConfigurationError(
                "resource request must contain exactly the governed fields; "
                f"missing={missing}, unknown={unknown}"
            )
        return cls(**{field: value[field] for field in _REQUEST_FIELDS})

    def to_dict(self) -> dict[str, int]:
        return {
            "cpu_cores": self.cpu_cores,
            "memory_mib": self.memory_mib,
            "gpu_devices": self.gpu_devices,
            "threads": self.threads,
            "temporary_disk_mib": self.temporary_disk_mib,
        }


@dataclass(frozen=True, slots=True)
class ExecutionResourceBudget:
    cpu_cores: int
    memory_mib: int
    gpu_device_ids: tuple[str, ...]
    threads: int
    temporary_disk_mib: int
    max_processes: int = 4

    def __post_init__(self) -> None:
        for name in (
            "cpu_cores",
            "memory_mib",
            "threads",
            "max_processes",
        ):
            _require_integer(name, getattr(self, name), minimum=1)
        _require_integer(
            "temporary_disk_mib",
            self.temporary_disk_mib,
            minimum=0,
        )
        normalized = tuple(str(value).strip() for value in self.gpu_device_ids)
        if any(not _GPU_DEVICE_ID_RE.fullmatch(value) for value in normalized) or len(
            set(normalized)
        ) != len(normalized):
            raise ResourceConfigurationError(
                "GPU device identifiers must be unique and safe for "
                "CUDA_VISIBLE_DEVICES"
            )
        object.__setattr__(self, "gpu_device_ids", normalized)
        if self.threads > self.cpu_cores:
            raise ResourceConfigurationError("budget threads cannot exceed cpu_cores")

    def accommodates(self, request: ExecutionResourceRequest) -> bool:
        return all(
            (
                request.cpu_cores <= self.cpu_cores,
                request.memory_mib <= self.memory_mib,
                request.gpu_devices <= len(self.gpu_device_ids),
                request.threads <= self.threads,
                request.temporary_disk_mib <= self.temporary_disk_mib,
            )
        )

    def to_public_dict(self) -> dict[str, int]:
        return {
            "cpu_cores": self.cpu_cores,
            "memory_mib": self.memory_mib,
            "gpu_devices": len(self.gpu_device_ids),
            "threads": self.threads,
            "temporary_disk_mib": self.temporary_disk_mib,
            "max_processes": self.max_processes,
        }


@dataclass(frozen=True, slots=True)
class ResourceTicket:
    """One correlated request in the global strict-FIFO resource order."""

    request: ExecutionResourceRequest
    run_id: str | None = None
    step_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request, ExecutionResourceRequest):
            raise ResourceConfigurationError("Resource Ticket request is invalid")
        for name, value in (("run_id", self.run_id), ("step_id", self.step_id)):
            if value is not None and (
                not isinstance(value, str)
                or not value
                or len(value) > 128
                or any(
                    ord(character) < 33 or ord(character) == 127 for character in value
                )
            ):
                raise ResourceConfigurationError(
                    f"Resource Ticket {name} must be bounded control-free text"
                )


@dataclass(frozen=True, slots=True)
class ResourceLease:
    request: ExecutionResourceRequest
    gpu_device_ids: tuple[str, ...]
    wait_seconds: float
    run_id: str | None = None
    step_id: str | None = None

    @property
    def environment(self) -> dict[str, str]:
        threads = str(self.request.threads)
        return {
            "CUDA_VISIBLE_DEVICES": ",".join(self.gpu_device_ids),
            "OMP_NUM_THREADS": threads,
            "OPENBLAS_NUM_THREADS": threads,
            "MKL_NUM_THREADS": threads,
            "NUMEXPR_NUM_THREADS": threads,
        }


@dataclass(slots=True, eq=False)
class _Ticket:
    """One queued waiter, identified by object identity and never by value.

    Two waiters carrying the same request, Run ID and Step ID are distinct
    queue entries. With the dataclass default ``eq=True`` they would compare
    equal, and the FIFO's wait predicate matches on ``is`` — see ``_acquire``.
    """

    value: ResourceTicket


class ExecutionResourceScheduler:
    """FIFO, multidimensional, atomic admission for one runtime event loop."""

    def __init__(self, budget: ExecutionResourceBudget):
        self.budget = budget
        self._condition = asyncio.Condition()
        self._queue: list[_Ticket] = []
        self._active_processes = 0
        self._cpu_cores = 0
        self._memory_mib = 0
        self._threads = 0
        self._temporary_disk_mib = 0
        self._gpu_device_ids: set[str] = set()
        self._quarantined = False
        # Keyed by `id()`, but holding the Lease itself: storing bare ids would
        # let CPython recycle a collected Lease's address onto a later Lease,
        # which would then be treated as retained and never released.
        self._retained_leases: dict[int, ResourceLease] = {}
        # ADR 0062: Run IDs that currently hold a global Lease. A second global
        # acquisition under the same Run ID is a nested acquisition, rejected
        # before it enqueues (see `_acquire`) rather than deadlocking the FIFO.
        # Untagged (`run_id is None`) Leases — fixed Workflow / Candidate / chain
        # steps that hold no parent Lease — are never tracked here.
        self._active_lease_run_ids: set[str] = set()

    @property
    def ready(self) -> bool:
        return not self._quarantined

    @property
    def quiescent(self) -> bool:
        """Whether a closed-loop owner may safely hand authority to a new loop."""

        return (
            not self._quarantined
            and not self._queue
            and self._active_processes == 0
            and self._cpu_cores == 0
            and self._memory_mib == 0
            and self._threads == 0
            and self._temporary_disk_mib == 0
            and not self._gpu_device_ids
            and not self._retained_leases
        )

    def _fits_available(self, request: ExecutionResourceRequest) -> bool:
        return all(
            (
                self._active_processes < self.budget.max_processes,
                self._cpu_cores + request.cpu_cores <= self.budget.cpu_cores,
                self._memory_mib + request.memory_mib <= self.budget.memory_mib,
                self._threads + request.threads <= self.budget.threads,
                self._temporary_disk_mib + request.temporary_disk_mib
                <= self.budget.temporary_disk_mib,
                len(self._gpu_device_ids) + request.gpu_devices
                <= len(self.budget.gpu_device_ids),
            )
        )

    def _allocate_gpu_ids(self, count: int) -> tuple[str, ...]:
        available = [
            value
            for value in self.budget.gpu_device_ids
            if value not in self._gpu_device_ids
        ]
        return tuple(available[:count])

    async def _acquire(self, value: ResourceTicket) -> ResourceLease:
        request = value.request
        if not self.budget.accommodates(request):
            raise ResourceConfigurationError(
                "resource request exceeds the scheduler budget"
            )
        started = time.monotonic()
        ticket = _Ticket(value)
        async with self._condition:
            if self._quarantined:
                raise ResourceConfigurationError(
                    "the process resource scheduler is quarantined"
                )
            if (
                value.run_id is not None
                and value.run_id in self._active_lease_run_ids
            ):
                # Reject before enqueue: a queued reentrant ticket would sit
                # behind capacity its own Run still holds and wedge the FIFO.
                raise NestedResourceAcquisitionError(value.run_id)
            self._queue.append(ticket)
            try:
                while not (self._queue[0] is ticket and self._fits_available(request)):
                    await self._condition.wait()
                    if self._quarantined:
                        raise ResourceConfigurationError(
                            "the process resource scheduler is quarantined"
                        )
            except BaseException:
                # Remove by identity. `list.remove()` deletes the first *equal*
                # entry: for the identical resource requests a multi-Step plan
                # normally declares, cancelling a later waiter would evict an
                # earlier one and orphan this ticket at the head. Since the
                # wait predicate above matches on `is`, that head would never
                # pop and the process-global FIFO would wedge permanently.
                for index, queued in enumerate(self._queue):
                    if queued is ticket:
                        del self._queue[index]
                        self._condition.notify_all()
                        break
                raise

            self._queue.pop(0)
            gpu_ids = self._allocate_gpu_ids(request.gpu_devices)
            self._active_processes += 1
            self._cpu_cores += request.cpu_cores
            self._memory_mib += request.memory_mib
            self._threads += request.threads
            self._temporary_disk_mib += request.temporary_disk_mib
            self._gpu_device_ids.update(gpu_ids)
            if value.run_id is not None:
                self._active_lease_run_ids.add(value.run_id)
            self._condition.notify_all()
        return ResourceLease(
            request=request,
            gpu_device_ids=gpu_ids,
            wait_seconds=max(0.0, time.monotonic() - started),
            run_id=value.run_id,
            step_id=value.step_id,
        )

    async def _release(self, lease: ResourceLease) -> None:
        request = lease.request
        async with self._condition:
            self._active_processes -= 1
            self._cpu_cores -= request.cpu_cores
            self._memory_mib -= request.memory_mib
            self._threads -= request.threads
            self._temporary_disk_mib -= request.temporary_disk_mib
            self._gpu_device_ids.difference_update(lease.gpu_device_ids)
            if lease.run_id is not None:
                self._active_lease_run_ids.discard(lease.run_id)
            self._condition.notify_all()

    async def quarantine(self, lease: ResourceLease) -> None:
        """Retain one uncertain Lease and reject every future acquisition."""

        async with self._condition:
            self._quarantined = True
            self._retained_leases[id(lease)] = lease
            self._condition.notify_all()

    async def quarantine_unknown_owner(self) -> None:
        """Reject admission when restart cannot reconstruct a prior Lease."""

        async with self._condition:
            self._quarantined = True
            self._condition.notify_all()

    @asynccontextmanager
    async def reserve(self, request: ExecutionResourceRequest | ResourceTicket):
        ticket = (
            request
            if isinstance(request, ResourceTicket)
            else ResourceTicket(request=request)
        )
        lease = await self._acquire(ticket)
        try:
            yield lease
        finally:
            if id(lease) not in self._retained_leases:
                release = asyncio.create_task(self._release(lease))
                caller_canceled = False
                while True:
                    try:
                        await asyncio.shield(release)
                        break
                    except asyncio.CancelledError:
                        if release.done():
                            release.result()
                            break
                        caller_canceled = True
                if caller_canceled:
                    raise asyncio.CancelledError


def _require_integer(name: str, value: object, *, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ResourceConfigurationError(
            f"{name} must be an integer greater than or equal to {minimum}"
        )


def _environment_integer(
    environ: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
) -> int:
    raw = str(environ.get(name, "") or "").strip()
    if not raw:
        return default
    if not raw.isdigit() or int(raw) < minimum:
        raise ResourceConfigurationError(
            f"{name} must be an integer greater than or equal to {minimum}"
        )
    return int(raw)


def _available_cpu_cores() -> int:
    try:
        affinity = os.sched_getaffinity(0)
        if affinity:
            return len(affinity)
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    return max(1, int(os.cpu_count() or 1))


def _available_memory_mib() -> int:
    try:
        pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        return max(1, pages * page_size // (1024 * 1024))
    except (AttributeError, OSError, TypeError, ValueError):
        return 4096


def _existing_disk_path(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _gpu_ids(environ: Mapping[str, str]) -> tuple[str, ...]:
    explicit = environ.get("OMICSCLAW_PLAN_GPU_DEVICE_IDS")
    raw = str(
        explicit if explicit is not None else environ.get("CUDA_VISIBLE_DEVICES", "")
    ).strip()
    if raw.lower() in {"", "-1", "none", "void"}:
        return ()
    values = tuple(value.strip() for value in raw.split(",") if value.strip())
    if len(set(values)) != len(values):
        raise ResourceConfigurationError("GPU device identifiers must be unique")
    return values


def detect_execution_resource_budget(
    output_root: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> ExecutionResourceBudget:
    """Build the process admission budget from host capacity + operator overrides."""
    source = os.environ if environ is None else environ
    detected_cpu = _available_cpu_cores()
    cpu_cores = _environment_integer(
        source,
        "OMICSCLAW_PLAN_CPU_CORES",
        detected_cpu,
        minimum=1,
    )
    memory_default = max(1, int(_available_memory_mib() * 0.8))
    memory_mib = _environment_integer(
        source,
        "OMICSCLAW_PLAN_MEMORY_MIB",
        memory_default,
        minimum=1,
    )
    disk_free = shutil.disk_usage(_existing_disk_path(Path(output_root))).free
    disk_default = max(0, int((disk_free // (1024 * 1024)) * 0.8))
    temporary_disk_mib = _environment_integer(
        source,
        "OMICSCLAW_PLAN_TEMPORARY_DISK_MIB",
        disk_default,
        minimum=0,
    )
    threads = _environment_integer(
        source,
        "OMICSCLAW_PLAN_THREADS",
        cpu_cores,
        minimum=1,
    )
    max_processes = _environment_integer(
        source,
        "OMICSCLAW_PLAN_MAX_PROCESSES",
        min(4, cpu_cores),
        minimum=1,
    )
    return ExecutionResourceBudget(
        cpu_cores=cpu_cores,
        memory_mib=memory_mib,
        gpu_device_ids=_gpu_ids(source),
        threads=threads,
        temporary_disk_mib=temporary_disk_mib,
        max_processes=max_processes,
    )


def get_process_resource_scheduler(
    output_root: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
    budget: ExecutionResourceBudget | None = None,
) -> ExecutionResourceScheduler:
    """Return the scheduler shared by every scientific executor on this loop."""
    global _PROCESS_SCHEDULER, _PROCESS_SCHEDULER_LOOP

    loop = asyncio.get_running_loop()
    if _PROCESS_SCHEDULER is None:
        _PROCESS_SCHEDULER = ExecutionResourceScheduler(
            budget
            if budget is not None
            else detect_execution_resource_budget(output_root, environ=environ)
        )
        _PROCESS_SCHEDULER_LOOP = loop
    elif _PROCESS_SCHEDULER_LOOP is not loop:
        if (
            _PROCESS_SCHEDULER_LOOP is not None
            and not _PROCESS_SCHEDULER_LOOP.is_closed()
        ):
            raise ResourceConfigurationError(
                "the process resource scheduler belongs to another live event loop"
            )
        if not _PROCESS_SCHEDULER.quiescent:
            raise ResourceConfigurationError(
                "the previous process resource scheduler still owns capacity"
            )
        _PROCESS_SCHEDULER = ExecutionResourceScheduler(
            budget
            if budget is not None
            else detect_execution_resource_budget(output_root, environ=environ)
        )
        _PROCESS_SCHEDULER_LOOP = loop
    elif budget is not None and _PROCESS_SCHEDULER.budget != budget:
        raise ResourceConfigurationError(
            "the process resource scheduler already owns a different budget"
        )
    return _PROCESS_SCHEDULER


__all__ = [
    "ExecutionResourceBudget",
    "ExecutionResourceRequest",
    "ExecutionResourceScheduler",
    "ResourceLease",
    "ResourceTicket",
    "ResourceConfigurationError",
    "NestedResourceAcquisitionError",
    "detect_execution_resource_budget",
    "get_process_resource_scheduler",
]
