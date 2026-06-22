"""Trusted input-path extraction for first-class analysis routes.

The deterministic dispatch planner (``build_analysis_tool_plan`` /
``build_partial_autonomous_continuation`` / ``DeterministicToolCallPlan``) was
removed in the 2026-06-22 control-plane consolidation along with the off/assist/
auto router modes. What remains is the trusted input-path extraction used by the
always-on assist-style route context builders in ``runtime/agent/loop.py``.
"""

from __future__ import annotations

import re

from omicsclaw.services.path_validation import discover_file, validate_input_path

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


__all__ = ["extract_valid_input_paths"]
