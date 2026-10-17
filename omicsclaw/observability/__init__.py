"""Expose telemetry construction, run scopes and tracing adapters.

The composition root binds provider tracing and tool hooks, then feeds engine
events to a run scope. Telemetry configuration and export stay in this layer.

Historical design notes: docs/engineering/runtime-framework-design-notes.md.
"""

from __future__ import annotations

from .attributes import DEFAULT_SERVICE_NAME, INSTRUMENTS
from .config import ExporterType, ObservabilityConfig
from .console import ConsoleMeter, ConsoleTracer
from .contract import NOOP_METER, NOOP_TRACER, Meter, NoopMeter, NoopTracer, Span, Tracer
from .hook import TracingHook
from .provider import TracedProvider
from .scope import RunScope, TurnScope, current_parent
from .serialize import MAX_ATTRIBUTE_BYTES, truncate_attr
from .telemetry import Telemetry, build_telemetry

__all__ = [
    "ConsoleMeter",
    "ConsoleTracer",
    "DEFAULT_SERVICE_NAME",
    "ExporterType",
    "INSTRUMENTS",
    "MAX_ATTRIBUTE_BYTES",
    "Meter",
    "NOOP_METER",
    "NOOP_TRACER",
    "NoopMeter",
    "NoopTracer",
    "ObservabilityConfig",
    "RunScope",
    "Span",
    "Telemetry",
    "TracedProvider",
    "Tracer",
    "TracingHook",
    "TurnScope",
    "build_telemetry",
    "current_parent",
    "truncate_attr",
]
