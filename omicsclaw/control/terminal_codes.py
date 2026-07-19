"""Closed, non-secret terminal-code vocabulary for control receipts.

Terminal codes are persisted authority, not diagnostic text.  Keeping the
runtime vocabulary finite prevents Workers and executor reporters from using a
syntactically valid string as a side channel for credentials or exception
detail.  Adding a code therefore requires an explicit schema migration.
Historical migrations must use their own versioned literal snapshots rather
than importing these live maps.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Literal, Mapping, TypeAlias


TurnTerminalCode: TypeAlias = Literal[
    "attachment_finalize_failed",
    "canceled",
    "canceled_before_start",
    "canceled_by_owner",
    "control_plane_restarted",
    "dispatch_enqueue_failed",
    "invalid_worker_outcome",
    "worker_failed",
    "worker_task_interrupted",
]

RunTerminalCode: TypeAlias = Literal[
    "canceled",
    "canceled_before_assignment",
    "canceled_by_owner",
    "completion_commit_failed",
    "control_plane_restarted",
    "execution_interrupted",
    "executor_failed",
    "spawn_failed",
    "submission_failed",
    "timed_out",
    "validation_failed",
]


TURN_TERMINAL_CODES_BY_STATUS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "succeeded": frozenset(),
        "failed": frozenset(
            {
                "attachment_finalize_failed",
                "dispatch_enqueue_failed",
                "invalid_worker_outcome",
                "worker_failed",
            }
        ),
        "canceled": frozenset(
            {"canceled", "canceled_before_start", "canceled_by_owner"}
        ),
        "interrupted": frozenset(
            {"control_plane_restarted", "worker_task_interrupted"}
        ),
    }
)

RUN_TERMINAL_CODES_BY_STATUS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "succeeded": frozenset(),
        "failed": frozenset(
            {
                "completion_commit_failed",
                "executor_failed",
                "spawn_failed",
                "submission_failed",
                "timed_out",
                "validation_failed",
            }
        ),
        "canceled": frozenset(
            {"canceled", "canceled_before_assignment", "canceled_by_owner"}
        ),
        "interrupted": frozenset({"control_plane_restarted", "execution_interrupted"}),
    }
)


def is_allowed_turn_terminal_code(status: str, code: object) -> bool:
    """Return whether ``code`` is an allowlisted Turn code for ``status``."""

    return isinstance(code, str) and code in TURN_TERMINAL_CODES_BY_STATUS.get(
        status, ()
    )


def is_allowed_run_terminal_code(status: str, code: object) -> bool:
    """Return whether ``code`` is an allowlisted Run code for ``status``."""

    return isinstance(code, str) and code in RUN_TERMINAL_CODES_BY_STATUS.get(
        status, ()
    )


__all__ = [
    "RUN_TERMINAL_CODES_BY_STATUS",
    "TURN_TERMINAL_CODES_BY_STATUS",
    "RunTerminalCode",
    "TurnTerminalCode",
    "is_allowed_run_terminal_code",
    "is_allowed_turn_terminal_code",
]
