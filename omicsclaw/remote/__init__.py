"""Provide remote execution services.

Historical design notes: docs/engineering/runtime-additional-design-notes.md.
"""

from __future__ import annotations

from .bridge import RemoteJobsBridge
from .jobs import RemoteJobHandle
from .kernel import (
    RemoteKernelEnv,
    RemoteKernelEnvError,
    RemoteKernelRuntime,
    RemoteSessionKernel,
)
from .plane import RemoteExecutor, RemotePlaneBinding, TargetSnapshot
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
    "RemoteKernelEnv",
    "RemoteKernelEnvError",
    "RemoteKernelRuntime",
    "RemotePlaneBinding",
    "TargetSnapshot",
    "RemoteSessionKernel",
    "SshSpawner",
    "SystemSshSpawner",
    "parse_probe_output",
    "refresh_in_flight",
]
