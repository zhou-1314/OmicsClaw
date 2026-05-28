"""Deterministic execution planning for first-class analysis routes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from omicsclaw.services.path_validation import validate_input_path

from .models import AnalysisRoute, AnalysisRouteKind

_PATH_TOKEN_RE = re.compile(r"(?P<path>(?:~|/|\./|\.\./)[^\s,;]+)")
_OUTPUT_DIR_RE = re.compile(r"Output dir:\s*(?P<path>\S+)")
_SKILL_OUTPUT_HINT_RE = re.compile(r"completed\.\s*Output:\s*(?P<path>\S+)")
_NOTEBOOK_HINT_RE = re.compile(
    r"Reproducibility notebook available:\s*(?P<path>\S+?\.ipynb)"
)
_BACKTICK_PATH_RE = re.compile(r"`(?P<path>/[^`]+)`")
_DEMO_HINTS = (" demo", "--demo", "示例", "演示")


@dataclass(frozen=True, slots=True)
class DeterministicToolCallPlan:
    """Tool call sequence chosen before entering the LLM engine."""

    route: AnalysisRoute
    calls: tuple[tuple[str, dict], ...]
    final_message: str = ""

    @property
    def should_execute(self) -> bool:
        return bool(self.calls)


def _strip_path_punctuation(value: str) -> str:
    return value.strip().strip("`'\"()[]{}<>.,;:")


def extract_valid_input_paths(text: str) -> list[str]:
    """Extract trusted input paths from user text.

    Autonomous generated code only receives references to paths that already
    pass OmicsClaw's trusted input validation.
    """
    paths: list[str] = []
    seen: set[str] = set()
    for match in _PATH_TOKEN_RE.finditer(str(text or "")):
        raw_path = _strip_path_punctuation(match.group("path"))
        if not raw_path:
            continue
        resolved = validate_input_path(raw_path, allow_dir=True)
        if resolved is None:
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        paths.append(key)
    return paths


def extract_output_paths(text: str) -> list[str]:
    """Extract output directories from existing tool result text."""
    paths: list[str] = []
    seen: set[str] = set()

    def add_path(raw_path: str) -> None:
        raw_path = _strip_path_punctuation(raw_path)
        if not raw_path:
            return
        path = Path(raw_path).expanduser()
        if not path.exists():
            return
        if path.is_file():
            for parent in path.parents:
                if (parent / "manifest.json").exists() or (
                    parent / "completion_report.json"
                ).exists():
                    path = parent
                    break
            else:
                if path.parent.name == "reproducibility":
                    path = path.parent.parent
                else:
                    path = path.parent
        key = str(path.resolve())
        if key in seen:
            return
        seen.add(key)
        paths.append(key)

    for pattern in (
        _OUTPUT_DIR_RE,
        _SKILL_OUTPUT_HINT_RE,
        _NOTEBOOK_HINT_RE,
        _BACKTICK_PATH_RE,
    ):
        for match in pattern.finditer(str(text or "")):
            add_path(match.group("path"))
    return paths


def _requests_demo(text: str) -> bool:
    lowered = f" {str(text or '').lower()} "
    return any(hint in lowered for hint in _DEMO_HINTS)


def build_analysis_tool_plan(
    route: AnalysisRoute,
    *,
    user_text: str,
    language: str = "python",
    max_repair_attempts: int = 2,
) -> DeterministicToolCallPlan | None:
    """Map a route to one deterministic tool call.

    ``partial_skill`` intentionally returns the skill-first call only. The
    autonomous continuation is appended after the skill result has produced
    concrete upstream evidence.
    """
    if route.kind is AnalysisRouteKind.CHAT:
        return None

    input_paths = extract_valid_input_paths(user_text)
    demo_requested = _requests_demo(user_text)

    if route.kind is AnalysisRouteKind.EXACT_SKILL:
        if not route.chosen_skill:
            return None
        if demo_requested:
            return DeterministicToolCallPlan(
                route=route,
                calls=(
                    (
                        "omicsclaw",
                        {
                            "skill": route.chosen_skill,
                            "mode": "demo",
                            "query": user_text,
                        },
                    ),
                ),
            )
        if not input_paths:
            return DeterministicToolCallPlan(
                route=route,
                calls=(
                    (
                        "omicsclaw",
                        {
                            "skill": route.chosen_skill,
                            "mode": "path",
                            "query": user_text,
                        },
                    ),
                ),
            )
        return DeterministicToolCallPlan(
            route=route,
            calls=(
                (
                    "omicsclaw",
                    {
                        "skill": route.chosen_skill,
                        "mode": "path",
                        "file_path": input_paths[0],
                        "query": user_text,
                    },
                ),
            ),
        )

    if route.kind is AnalysisRouteKind.PARTIAL_SKILL:
        if not route.chosen_skill:
            return None
        if demo_requested:
            return DeterministicToolCallPlan(
                route=route,
                calls=(
                    (
                        "omicsclaw",
                        {
                            "skill": route.chosen_skill,
                            "mode": "demo",
                            "query": user_text,
                        },
                    ),
                ),
            )
        if not input_paths:
            return DeterministicToolCallPlan(
                route=route,
                calls=(
                    (
                        "omicsclaw",
                        {
                            "skill": route.chosen_skill,
                            "mode": "path",
                            "query": user_text,
                        },
                    ),
                ),
            )
        return DeterministicToolCallPlan(
            route=route,
            calls=(
                (
                    "omicsclaw",
                    {
                        "skill": route.chosen_skill,
                        "mode": "path",
                        "file_path": input_paths[0],
                        "query": user_text,
                    },
                ),
            ),
        )

    if route.kind is AnalysisRouteKind.NO_SKILL:
        return DeterministicToolCallPlan(
            route=route,
            calls=(
                (
                    "autonomous_analysis_execute",
                    {
                        "goal": user_text,
                        "input_paths": input_paths,
                        "language": language,
                        "max_repair_attempts": max(0, min(int(max_repair_attempts), 2)),
                    },
                ),
            ),
        )

    return None


def build_partial_autonomous_continuation(
    route: AnalysisRoute,
    *,
    user_text: str,
    skill_output: str,
    language: str = "python",
    max_repair_attempts: int = 2,
) -> tuple[str, dict] | None:
    """Build the autonomous follow-up call after a partial skill run."""
    if route.kind is not AnalysisRouteKind.PARTIAL_SKILL:
        return None
    upstream_paths = extract_output_paths(skill_output)
    if not upstream_paths:
        return None
    input_paths = extract_valid_input_paths(user_text)
    context = (
        "Route kind: partial_skill\n"
        f"Matched built-in skill: {route.chosen_skill}\n"
        "Use the upstream OmicsClaw skill output as evidence. Do not rerun or "
        "rewrite the matched skill; add only the requested supplement, "
        "post-processing, plotting, or report integration.\n\n"
        "Skill result summary:\n"
        f"{skill_output[:4000]}"
    )
    return (
        "autonomous_analysis_execute",
        {
            "goal": user_text,
            "context": context,
            "input_paths": input_paths,
            "upstream_paths": upstream_paths,
            "language": language,
            "max_repair_attempts": max(0, min(int(max_repair_attempts), 2)),
        },
    )


__all__ = [
    "DeterministicToolCallPlan",
    "build_analysis_tool_plan",
    "build_partial_autonomous_continuation",
    "extract_output_paths",
    "extract_valid_input_paths",
]
