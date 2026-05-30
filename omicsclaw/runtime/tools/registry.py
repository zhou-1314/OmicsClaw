from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..tools.executor import ToolCallable, build_executor_map
from ..tools.spec import ToolSpec

_LOGGER = logging.getLogger("omicsclaw.runtime.tools.registry")


# Bench (ADR 0020) — per-stage default tool subsets. ``STAGE_TO_TOOL_SUBSETS`` is
# the single source of truth for per-stage tool *subsets* (the per-stage prompt
# *stance* lives separately in ``_STAGE_FRAGMENTS``, engine/loop.py — the two maps
# are deliberately NOT key-aligned: a stage may carry a stance fragment without a
# tool subset). A stage NOT present here (``analyze``, ``""``/unset, or any unknown
# string) is UNFILTERED — the full surface tool list — which keeps the legacy /
# non-Bench path byte-identical and cache-stable (ADR 0024). Stages are permissive,
# not jails: the Read subset is a default-deny allow-list (heavyweight analysis /
# file-writing / network-download / media tools are withheld), and the frontend
# proposes a one-click switch to Analyze when a withheld tool is wanted.
_READ_STAGE_TOOLS: frozenset[str] = frozenset(
    {
        # literature + knowledge + web reading (metadata / parse / read-only).
        # parse_literature is now IN Read (Phase 3.3b): its download is permission-
        # gated (approval_mode=ASK, ADR 0021) — a proposal, never automatic — and a
        # downloaded dataset registers under dataset://<thread_id>/*. fetch_geo_metadata
        # stays ungated as the metadata reader (download defaults False); its rarely-used
        # download=True branch is itself ungated — a pre-existing bypass to gate in a
        # follow-up (ADR 0021 applies to it too).
        "fetch_geo_metadata", "parse_literature", "consult_knowledge", "read_knowhow",
        "web_search", "web_fetch", "web_method_search",
        # OmicsClaw-KG read tools (Bench Phase 3.1, ADR 0019): read-only retrieval
        # over the cross-research knowledge base — the core of the Read stage.
        # Soft-fail when the optional package is absent (kg_tools.py).
        "kg_search", "kg_get_page", "kg_list_pages", "kg_graph_neighbors",
        "kg_status", "kg_recent_log", "kg_communities",
        # KG ingest (Bench Phase 3.3c, RD-INGEST-9): the citation-substrate writer —
        # ingesting a dropped paper is the user's evident intent (ADR 0019, AUTO).
        "kg_ingest",
        # lightweight memory notes + read-only data / file inspection. remember /
        # forget kept: note-taking only — no compute, no workspace write (recall is
        # the pure-read counterpart).
        "recall", "remember", "forget",
        "inspect_data", "inspect_file", "file_read", "glob_files", "grep_files",
        "list_directory", "get_file_size",
        # interaction / discovery / planning. task_* / todo_write are writes_config
        # (session task store, drives the desktop 待办) — read-safe: no workspace,
        # network, or compute.
        "ask_user", "tool_search", "list_skills_in_domain", "resolve_capability",
        "task_create", "task_get", "task_list", "task_update", "todo_write",
    }
)

STAGE_TO_TOOL_SUBSETS: dict[str, frozenset[str]] = {
    "read": _READ_STAGE_TOOLS,
    # "ideate": Read tools + KG ideate tools — added in v1.5 (Phase 6) when they exist.
    # "write": writing-domain tools — added in v2 (Phase 7) when they exist.
    # "analyze" and "" are intentionally absent → full (unfiltered) tool list.
}


def select_tool_specs(
    specs: tuple[ToolSpec, ...] | list[ToolSpec],
    *,
    request: Any,
    surface_only: bool = False,
    stage: str = "",
) -> tuple[ToolSpec, ...]:
    """Filter ``specs`` to those that should be visible for the given request.

    Filtering rules (in order):
      1. ``request.surface`` must be in ``spec.surfaces`` — surface gating
         remains a prerequisite.
      2. If ``surface_only`` is True, the tool is included (predicate skipped).
      3. Else if ``spec.predicate is None``, the tool is included (always-on).
      4. Otherwise ``spec.predicate(request)`` is called under try/except.
         A raising predicate is fail-closed: the tool is suppressed and a
         WARNING is logged. A return value of False suppresses the tool.

    ``surface_only=True`` (ADR 0024) yields the **Frozen tool list**: every
    surface-eligible tool, independent of the per-turn query. Because ``surface``
    is a session constant and ``specs`` order is static, the result is
    byte-identical across a session's turns — the **Stable prefix invariant**
    for the tool segment. The per-turn query-keyword gating (the former
    tool-list-compression) is bypassed: once tools live in a cached prefix,
    hit-token pricing (~10% of miss) makes compressing them a net loss.

    Order is preserved.

    On predicate evaluation, ``EVENT_PREDICATE_HIT`` /
    ``EVENT_PREDICATE_MISS`` events are emitted through the shared
    predicate-event sink registered via
    ``omicsclaw.runtime.context_layers.register_predicate_event_sink`` —
    this gives the Phase 4 predicate hook a production producer beyond
    context-layer telemetry.
    """
    surface = str(getattr(request, "surface", "") or "").strip()
    selected: list[ToolSpec] = []

    from ..context.layers import _emit_predicate_event  # type: ignore[attr-defined]
    from . import hooks as _events_mod

    for spec in specs:
        if spec.surfaces and surface and surface not in spec.surfaces:
            continue
        if surface_only or spec.predicate is None:
            selected.append(spec)
            continue
        try:
            decision = bool(spec.predicate(request))
        except Exception as exc:
            _LOGGER.warning(
                "Predicate for tool %r raised %s: %s; suppressing tool",
                spec.name,
                exc.__class__.__name__,
                exc,
            )
            continue
        try:
            _emit_predicate_event(
                _events_mod.EVENT_PREDICATE_HIT
                if decision
                else _events_mod.EVENT_PREDICATE_MISS,
                predicate=spec.name,
                surface=surface,
                source="tool_registry.predicate",
                kind="tool",
            )
        except Exception:  # pragma: no cover - never break selection on telemetry failure
            pass
        if decision:
            selected.append(spec)

    # Bench (ADR 0020): when the active lifecycle stage defines a default tool
    # subset, keep only those tools. A stage with no defined subset (analyze, "",
    # unknown) is unfiltered — the permissive default — so the legacy path and the
    # cache-stable surface list are untouched.
    allowed = STAGE_TO_TOOL_SUBSETS.get(stage)
    if allowed is not None:
        selected = [spec for spec in selected if spec.name in allowed]

    return tuple(selected)


@dataclass(frozen=True, slots=True)
class ToolRuntime:
    specs: tuple[ToolSpec, ...]
    specs_by_name: dict[str, ToolSpec]
    openai_tools: tuple[dict[str, Any], ...]
    executors: dict[str, ToolCallable]


class ToolRegistry:
    """Ordered registry for tool specs shared across entry surfaces."""

    def __init__(self, specs: list[ToolSpec] | tuple[ToolSpec, ...]):
        names: set[str] = set()
        ordered: list[ToolSpec] = []
        for spec in specs:
            if spec.name in names:
                raise ValueError(f"Duplicate tool name: {spec.name}")
            names.add(spec.name)
            ordered.append(spec)
        self._specs = tuple(ordered)

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return self._specs

    def for_surface(self, surface: str) -> "ToolRegistry":
        return ToolRegistry([spec for spec in self._specs if surface in spec.surfaces])

    def to_openai_tools(self) -> list[dict[str, Any]]:
        return [spec.to_openai_tool() for spec in self._specs]

    def to_openai_tools_for_request(
        self, request: Any, *, surface_only: bool = False, stage: str = ""
    ) -> list[dict[str, Any]]:
        """Per-request filtered openai-tool payload.

        ``to_openai_tools()`` (no request) still returns the full list —
        callers that haven't migrated keep the legacy behavior. With
        ``surface_only=True`` (ADR 0024) the per-turn query predicates are
        skipped, yielding the session-stable **Frozen tool list**.

        ``stage`` (Bench, ADR 0020) sub-filters that list to the active
        lifecycle stage's default subset; an empty / ``analyze`` / unknown stage
        is unfiltered, preserving the cache-stable frozen list.
        """
        return [
            spec.to_openai_tool()
            for spec in select_tool_specs(
                self._specs, request=request, surface_only=surface_only, stage=stage
            )
        ]

    def build_runtime(self, available_executors: dict[str, ToolCallable]) -> ToolRuntime:
        return ToolRuntime(
            specs=self._specs,
            specs_by_name={spec.name: spec for spec in self._specs},
            openai_tools=tuple(self.to_openai_tools()),
            executors=build_executor_map(self._specs, available_executors),
        )
