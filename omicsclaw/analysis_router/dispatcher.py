"""Deterministic execution planning for first-class analysis routes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from omicsclaw.services.path_validation import discover_file, validate_input_path

from .models import AnalysisRoute, AnalysisRouteKind

_PATH_TOKEN_RE = re.compile(r"(?P<path>(?:~|/|\./|\.\./)[^\s,;]+)")
# Bare data filenames with no path prefix, e.g. "slideseqv2_mouse_hippocampus.h5ad".
# ASCII-only character classes (not ``\w``) so CJK/other non-ASCII text acts as a
# boundary — desktop users routinely write the filename flush against Chinese
# text ("对xxx.h5ad执行..."). The leading negative lookbehind skips tokens that
# are already part of a slash/~/.-prefixed path (those are handled by
# ``_PATH_TOKEN_RE``). Resolution stays gated by ``validate_input_path`` against
# the trusted data directories, so matching a token here never widens trust.
_DATA_FILE_EXTENSIONS = (
    "h5ad", "h5", "loom", "zarr", "mtx",
    "mzml", "mzxml",
    "fastq", "fq", "fasta", "fa",
    "bam", "sam", "cram", "vcf", "bcf",
    "gtf", "gff", "bed", "csv", "tsv", "rds",
)
_BARE_DATA_FILE_RE = re.compile(
    r"(?<![A-Za-z0-9_./~\\-])"
    r"([A-Za-z0-9_][A-Za-z0-9_.+\-]*\.(?:"
    + "|".join(_DATA_FILE_EXTENSIONS)
    + r")(?:\.gz|\.bz2)?)"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
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
    text = str(text or "")

    def _add(raw_token: str, *, allow_discovery: bool = False) -> None:
        raw_token = _strip_path_punctuation(raw_token)
        if not raw_token:
            return
        resolved = validate_input_path(raw_token, allow_dir=True)
        if resolved is None and allow_discovery:
            # ``validate_input_path`` only checks the top level of each trusted
            # dir, but the skill executor resolves bare names with
            # ``discover_file`` (recursive rglob within the trusted dirs). A
            # file one level down — e.g. ``<workspace>/data/foo.h5ad`` when the
            # Desktop app trusts ``<workspace>`` — is therefore visible to the
            # executor but not to this path extraction, so the deterministic
            # router built a path-less plan and the run reported 'No input file
            # available'. Mirror the executor: fall back to ``discover_file``,
            # re-validating each hit so trust is never widened. ``discover_file``
            # sorts newest-first, so the first trusted match is the best guess.
            for found in discover_file(raw_token):
                resolved = validate_input_path(str(found), allow_dir=True)
                if resolved is not None:
                    break
        if resolved is None:
            return
        key = str(resolved)
        if key in seen:
            return
        seen.add(key)
        paths.append(key)

    # 1. Explicit path tokens (~, /, ./, ../) — preserved first so an explicit
    #    path keeps priority in the returned order. No recursive discovery:
    #    an explicit path that fails trust validation is rejected, not widened.
    for match in _PATH_TOKEN_RE.finditer(text):
        _add(match.group("path"))
    # 2. Bare data filenames resolved against the trusted data directories,
    #    including files nested in their subdirectories (recursive discovery).
    for match in _BARE_DATA_FILE_RE.finditer(text):
        _add(match.group(1), allow_discovery=True)
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


def _extract_named_method(user_text: str, skill_key: str) -> str:
    """Return a valid method explicitly named in ``user_text`` for ``skill_key``.

    The Run-as-typed (``auto``) route honors a method the user named outright
    (e.g. "…用 CellCharter…") instead of silently dropping it. Valid methods are
    the skill's ``param_hints`` keys (method-keyed in SKILL.md); matching is
    whole-token and case-insensitive. Typos are *not* corrected — fuzzy intent is
    the assist-mode LLM's job, not the deterministic literal route. Returns ``""``
    when no valid method token is present.
    """
    text = str(user_text or "").lower()
    if not text or not skill_key:
        return ""
    try:
        from omicsclaw.skill.orchestration import _lookup_skill_info

        info = _lookup_skill_info(skill_key, force_reload=False) or {}
        methods = [
            str(m).strip().lower()
            for m in (info.get("param_hints") or {})
            if str(m).strip()
        ]
    except Exception:
        return ""
    # Longest method name first so a specific token wins over a substring.
    for method in sorted(set(methods), key=len, reverse=True):
        if re.search(r"(?<![a-z0-9])" + re.escape(method) + r"(?![a-z0-9])", text):
            return method
    return ""


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
        # Run-as-typed: honor an explicitly named method instead of dropping it,
        # so `auto` runs e.g. CellCharter when the user said so. A path-less plan
        # is still emitted when no input resolved (the executor reports the
        # missing input); the named method rides along either way.
        call_args: dict = {
            "skill": route.chosen_skill,
            "mode": "path",
            "query": user_text,
        }
        if input_paths:
            call_args["file_path"] = input_paths[0]
        named_method = _extract_named_method(user_text, route.chosen_skill)
        if named_method:
            call_args["method"] = named_method
        return DeterministicToolCallPlan(
            route=route,
            calls=(("omicsclaw", call_args),),
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
