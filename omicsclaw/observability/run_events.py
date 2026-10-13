"""Export committed Run events through the configured telemetry backend."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from .attributes import ATTR_RUN_ID, METRIC_RUN_EVENTS, SPAN_RUN_EVENT
from .contract import AttributeValue, Meter, Tracer
from .scope import current_parent

_log = logging.getLogger(__name__)
_KINDS = {"run", "job", "kernel", "artifact", "verification"}
_EVENTS = {"created", "updated", "started", "finished"}
_IDS = {
    "run_id": ATTR_RUN_ID,
    "session_id": "session.id",
    "plan_id": "plan.id",
    "step_id": "plan.step.id",
    "job_id": "job.id",
    "kernel_id": "kernel.id",
    "artifact_id": "artifact.id",
    "verification_id": "verification.id",
}
_STATUSES = {
    "created",
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "interrupted",
    "lost",
    "unknown",
    "ready",
    "idle",
    "busy",
    "dead",
    "stopped",
}
_CHECKS = {
    "checksum",
    "file_exists",
    "json_schema",
    "csv_schema",
    "h5ad_schema",
    "exit_code",
    "reviewer",
}
_VERDICTS = {"pass", "warn", "fail", "inconclusive"}


class RunEventObserver:
    """Record identifiers and verdicts without exporting result content."""

    def __init__(self, tracer: Tracer, meter: Meter) -> None:
        self._tracer = tracer
        self._meter = meter

    def __call__(self, event: Mapping[str, Any]) -> None:
        kind, action = event.get("kind"), event.get("event")
        if not isinstance(kind, str) or not isinstance(action, str):
            return
        if kind not in _KINDS or action not in _EVENTS:
            return
        payload = event.get("payload", {})
        if not isinstance(payload, Mapping):
            return
        name = f"{kind}.{action}"
        status = payload.get("status")
        if kind == "verification" and action in {"created", "updated"}:
            name = "verification.finished"
        elif kind == "job" and payload.get("status") == "queued":
            name = "job.submitted"
        elif (
            kind == "kernel" and isinstance(status, str) and status in {"ready", "idle"}
        ):
            name = "kernel.ready"
        attributes: dict[str, AttributeValue] = {
            "event.name": name,
            "object.kind": kind,
        }
        for field, key in _IDS.items():
            value = payload.get(field) or event.get(field)
            if isinstance(value, str) and value:
                attributes[key] = value
        version = payload.get("artifact_version", payload.get("version"))
        if type(version) is int:
            attributes["artifact.version"] = version
        if type(event.get("seq")) is int:
            attributes["event.sequence"] = event["seq"]
        for field, allowed in (
            ("status", _STATUSES),
            ("check", _CHECKS),
            ("verdict", _VERDICTS),
        ):
            value = payload.get(field)
            if isinstance(value, str) and value in allowed:
                attributes[f"{kind}.{field}"] = value
        try:
            span = self._tracer.start_span(
                SPAN_RUN_EVENT,
                parent=current_parent(),
                attributes=attributes,
            )
            span.end()
            self._meter.count(
                METRIC_RUN_EVENTS, 1, {"event.name": name, "object.kind": kind}
            )
        except Exception:  # noqa: BLE001 - optional telemetry must not interrupt execution
            _log.warning("run event telemetry failed", exc_info=True)
