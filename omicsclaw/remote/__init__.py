"""``omicsclaw.remote`` — the remote execution plane (C1/C2).

The remote runtime plan's main route: orchestration stays local, the
remote host is asked for ``sshd`` and a POSIX shell, and every remote
fact that outlives a call is a durable handle in SQLite. Four layers,
each importable without the one above it::

    ssh        argv construction + process spawning (the transport)
    probe      the read-only probe script + its tolerant parser
    jobs       job scripts, handles, the status state machine (pure)
    store      remote_hosts / remote_jobs over the memory Database
    plane      RemotePlaneBinding: the tool layer's injected plane
    bridge     the P1 jobs-plane runner for runtime="remote:<alias>"
    reconcile  re-estimate in-flight handles after a restart

The tool family itself lives in the leaf layer
(:mod:`omicsclaw.tools.builtin.remote`) with this package injected as
its :class:`~omicsclaw.tools.builtin.remote.RemotePlane`; the
composition root binds :class:`RemotePlaneBinding` when
:attr:`~omicsclaw.entry.config.AppConfig.remote_execution` is on, and
nothing remote exists in a deployment that leaves it off — no tool, no
prompt section, no socket opened on the plane's behalf.

**Standard library only**, like everything this package spawns: the
SSH client is the system's ``ssh``/``sftp`` (zero pip dependencies, the
user's config as the contract, host keys the system's business).
"""

from __future__ import annotations

from .bridge import RemoteJobsBridge
from .jobs import RemoteJobHandle
from .plane import RemoteExecutor, RemotePlaneBinding
from .probe import HostProbe, parse_probe_output
from .reconcile import refresh_in_flight
from .ssh import (
    RemoteHostRefused,
    RemoteHostUnreachable,
    SshSpawner,
    SystemSshSpawner,
)
from .store import RemoteHostStore, RemoteJobRow, RemoteJobStore

__all__ = [
    "HostProbe",
    "RemoteExecutor",
    "RemoteHostRefused",
    "RemoteHostStore",
    "RemoteHostUnreachable",
    "RemoteJobHandle",
    "RemoteJobRow",
    "RemoteJobStore",
    "RemoteJobsBridge",
    "RemotePlaneBinding",
    "SshSpawner",
    "SystemSshSpawner",
    "parse_probe_output",
    "refresh_in_flight",
]
