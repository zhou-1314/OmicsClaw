"""Capture channel ② of the artifacts plane: the output-contract scanner.

The curated skills already declare what they write in
``references/output_contract.md`` — free-form prose a person reads, built
from backticked relative paths (``​`tables/program_usage.csv`​``) and,
in the auto-generated contracts, a fenced ``output_directory/`` tree.
That file is the raw material the plan named: after a ``skill_run`` job
reaches a good end, this module parses the contract of the skill that
ran, walks the workspace for files it declares, and registers what it
finds as artifacts — **zero LLM cost**, which is why it is the first
capture channel.

The parser is deliberately a tokenizer, not a markdown parser. Three
shapes carry the declarations this repository's 88 contracts use:

* backtick spans anywhere in the text — ``​`figures/mean_program_usage.png`​``;
* fenced code blocks holding a directory tree, whose ``├──`` /
  ``└──`` lines name one file each;
* bare path-shaped words — list items (``- tables/Summary.csv: counts``)
  and prose mentions (``writes processed.h5ad, report.md, result.json``).
  The curated contracts are not uniform: a bare ``-`` list with no
  backticks is a real style, and the auto-generated ones name their root
  outputs in plain sentences.

A token survives only when it *looks like a file path*: no spaces or
brackets, a short alphanumeric suffix, relative, not a directory, no
``..``. Everything else — ``run_info``, ``obsm["X_gene_programs"]``,
``--r-enhanced``, ``figures/r_enhanced/`` — is prose about the outputs,
not a declaration, and is dropped. A declared path still registers only
when the file exists in the workspace and passes the attribution rules
below, so a prose mention of a file the run never wrote is inert.

**The fallback.** A contract that is missing, unparseable (no token
survived) or that matched nothing in the workspace turns the scan over
to the safety net: the conventional output directories the skills'
``write_output`` SDK actually creates — ``figures/``, ``tables/``,
``figure_data/``, ``results/`` — anywhere in the workspace, filtered by
extension. Artifacts registered that way carry
``meta["capture"] = "fallback"`` so a review can tell a declared output
from an inferred one. (A contract that parsed but matched nothing is
treated the same way: from the scanner's seat, "declared and absent" and
"undeclared" are both "the declaration told me nothing", and the
convention scan is what answers.)

**Idempotent by construction.** Registration goes through
:meth:`~omicsclaw.memory.artifacts.ArtifactStore.insert_artifact`, whose
``(job_id, path)`` uniqueness check makes a second scan of the same job
register nothing — the scanner itself also keeps per-scan bookkeeping so
one declaration matching two files (``figures/a.png`` declared, run wrote
``run1/figures/a.png`` and ``run2/figures/a.png``) registers each once,
in a stable order.
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Callable, Final

from omicsclaw.memory.artifacts import (
    ArtifactRecord,
    ArtifactStore,
    hash_and_size,
    kind_and_mime_for,
    new_artifact_id,
)

__all__ = [
    "CONTRACT_FILENAME",
    "FALLBACK_DIRECTORIES",
    "MAX_SCAN_FILES",
    "OUTPUT_CONTRACT_SUFFIXES",
    "parse_output_contract",
    "register_job_artifacts",
    "scan_workspace_files",
]

_log = logging.getLogger(__name__)

CONTRACT_FILENAME: Final = "output_contract.md"
"""Where a skill declares its outputs, relative to the skill directory."""

FALLBACK_DIRECTORIES: Final = frozenset(
    {"figures", "tables", "figure_data", "results"}
)
"""Conventional output directory names the safety-net scan accepts.

Confirmed against the curated skills: ``write_output`` writes under
``figures/``/``tables/``/``intermediate/``/``logs/`` inside a module's
results, and the CLI contracts speak of ``figures/``, ``tables/`` and
``figure_data/``. ``intermediate/`` and ``logs/`` are deliberately
**not** artifact material — they are plumbing a person does not browse
— and ``results`` covers the module root the SDK lays out."""

OUTPUT_CONTRACT_SUFFIXES: Final = frozenset(
    {
        ".png", ".svg", ".jpg", ".jpeg", ".csv", ".tsv", ".h5ad", ".rds",
        ".html", ".htm", ".md", ".pdf", ".json", ".txt",
        ".parquet", ".xlsx", ".bam", ".h5", ".npz", ".rdata", ".gz",
    }
)
"""Extensions a declared token may carry. Deliberately wider than the
kind map: a contract may declare a ``.bam`` or ``.txt`` the run really
writes, and refusing it would make the contract a lie; it registers as
kind ``other`` instead."""

MAX_SCAN_FILES: Final = 20_000
"""Ceiling on files one workspace walk collects. A workspace is also a
person's data directory; the walk must stay bounded even when it is
huge. Exceeding the ceiling logs and registers what was collected."""

MAX_SCAN_DEPTH: Final = 12
"""Directory depth ceiling for the same reason."""

_TREE_ENTRY: Final = re.compile(
    r"^(?P<prefix>[│\s]*)(?P<marker>[├└])──\s*(?P<name>.+?)\s*$"
)
_BACKTICK: Final = re.compile(r"`([^`\n]+)`")
_BARE_WORD: Final = re.compile(r"[^\s`(){}\[\]<>\"'`;,:?!]+")
"""One punctuation-delimited word of the contract text. Colons and commas
delimit on purpose: ``- tables/x.csv: counts`` yields ``tables/x.csv``,
prose like ``writes a.h5ad, b.md,`` yields both files clean. Leading and
trailing ``.``/``-`` are stripped before the shape rules judge the word,
so ``commands.sh.`` becomes ``commands.sh`` and a list marker never
survives into the token."""
_BAD_CHARS: Final = re.compile(r"[\s(){}\[\]<>\"'|*?:$#]")
_SUFFIX: Final = re.compile(r"\.[A-Za-z0-9]{1,8}$")

Emit = Callable[[str, dict], object]
"""The job event channel ``(event_type, payload) -> seq`` the scanner is
handed — ``JobsManager._emit`` bound to one job id."""


def parse_output_contract(text: str) -> list[str]:
    """The declared relative paths of one contract's text, in order.

    Backtick spans from the prose, file lines from fenced trees, and bare
    path-shaped words (list items and prose mentions), kept only when
    they look like a relative file path (see the module docstring). Tree
    nesting is followed, so ``└── tables/`` with an indented
    ``├── Summary.csv`` beneath it declares ``tables/Summary.csv``.
    Deduplicated, order-preserving, never raising — a contract is data
    about a skill, and malformed data parses to fewer declarations, which
    the caller turns into the fallback scan.
    """
    declared: list[str] = []
    seen: set[str] = set()

    def _consider(token: str) -> None:
        cleaned = token.strip().strip("`")
        if not cleaned or cleaned in seen:
            return
        if not _looks_like_file_path(cleaned):
            return
        seen.add(cleaned)
        declared.append(cleaned)

    stripped = text or ""
    for token in _BACKTICK.findall(stripped):
        _consider(token)
    for block in re.findall(r"```[^\n]*\n(.*?)```", stripped, flags=re.DOTALL):
        _parse_tree(block, _consider)
    for word in _BARE_WORD.findall(_mask_tree_blocks(stripped)):
        _consider(word.strip(".-"))
    return declared


def _mask_tree_blocks(text: str) -> str:
    """Blank out fenced blocks that carry directory-tree markers.

    A tree's file lines are claimed by :func:`_parse_tree` with their
    directory prefixes reconstructed; letting the bare-word pass see the
    same lines would declare ``Summary.csv`` a second time without its
    ``tables/`` prefix. A fenced block without tree markers is left in —
    a plain fenced list of paths is prose-shaped and fair game.
    """
    def _mask(match: re.Match[str]) -> str:
        block = match.group(0)
        if "├" in block or "└" in block:
            return " " * len(block)
        return block

    return re.sub(r"```.*?```", _mask, text, flags=re.DOTALL)


def _parse_tree(block: str, consider) -> None:
    """Walk one fenced directory tree, declaring its files with their
    directory prefixes reconstructed from the indentation.

    A misindented or unreadable line is skipped rather than guessed at:
    the sc-count-style contracts that use trees also list their files in
    backticks in the prose, so the tree is a second reading of the same
    declaration, not the only one.
    """
    stack: list[str] = []
    for line in block.splitlines():
        match = _TREE_ENTRY.match(line)
        if match is None:
            continue
        prefix = match.group("prefix")
        name = match.group("name")
        depth = len(prefix) // 4
        del stack[depth:]
        if not name:
            continue
        if name.endswith("/"):
            stack.append(name.rstrip("/"))
            continue
        consider("/".join([*stack, name]))


def _looks_like_file_path(token: str) -> bool:
    if _BAD_CHARS.search(token):
        return False
    if token.startswith(("/", "~", "\\")) or token.endswith("/"):
        return False
    if ".." in Path(token).parts:
        return False
    name = token.rsplit("/", 1)[-1]
    suffix = Path(name).suffix.lower()
    if not _SUFFIX.search(name) or suffix not in OUTPUT_CONTRACT_SUFFIXES:
        return False
    return True


def scan_workspace_files(workspace: Path) -> dict[str, Path]:
    """Every visible file under *workspace*, as ``{relative posix: path}``.

    Hidden directories (``.omicsclaw``, ``.git``) are pruned, which keeps
    the store's own database and any repository plumbing out of the
    artifact space — and matches what ``/files/serve`` is willing to
    serve, so a registered artifact is always a servable one. A file
    symlink whose target resolves outside the workspace is skipped for
    the same reason: the row would describe a file the serve route would
    refuse. Never raises: a walk that trips on one unreadable directory
    keeps going.
    """
    found: dict[str, Path] = {}
    root = workspace.resolve()
    counted = 0
    for current, directories, files in _walk_bounded(root):
        directories[:] = sorted(d for d in directories if not d.startswith("."))
        for name in sorted(files):
            if name.startswith("."):
                continue
            path = Path(current) / name
            if path.is_symlink():
                try:
                    if not path.resolve().is_relative_to(root):
                        continue
                except OSError:
                    continue
            found[path.relative_to(root).as_posix()] = path
            counted += 1
            if counted >= MAX_SCAN_FILES:
                _log.warning(
                    "artifact scan hit the %d-file ceiling under %s",
                    MAX_SCAN_FILES,
                    root,
                )
                return found
    return found


def _walk_bounded(root: Path):
    """``os.walk`` with the depth ceiling applied.

    A separate named helper because the ceiling is easier to see as a
    ``relative_to`` count than as arithmetic on ``dirpath`` strings.
    """
    base_depth = len(root.parts)
    for current, directories, files in os.walk(root):
        if len(Path(current).parts) - base_depth >= MAX_SCAN_DEPTH:
            directories[:] = []
        yield current, directories, files


def register_job_artifacts(
    store: ArtifactStore,
    *,
    job_id: str,
    session_id: str,
    skill_directory: Path,
    workspace: Path,
    emit: Emit | None = None,
    since: float | None = None,
) -> list[ArtifactRecord]:
    """Scan once for one finished job and register what it produced.

    *emit* is the job's event channel (``JobsManager._emit`` shaped);
    every artifact that becomes a row sends one
    ``artifact.created{artifact_id, kind, title, path}`` frame on it, so
    the tray fills from the same stream the progress bar does. A
    job-level scan runs inside ``asyncio.to_thread`` with the store's own
    lock serializing writes, and the ``(job_id, path)`` check makes the
    whole operation idempotent however many times it runs.

    *since* is the attribution floor — the job's ``created_at``. The
    scan walks the whole workspace (a job does not declare its output
    root), and without the floor the second run of a skill would claim
    the first run's files too, because the contract declares both. A
    file is this job's when the contract names it **and** its mtime is
    at or after the floor (one second of slack for the clock's sake);
    ``None`` disables the floor for callers that have no job clock, such
    as a repair re-scan.

    :returns: only the artifacts newly registered by this call.
    """
    files = scan_workspace_files(workspace)
    if since is not None:
        floor = since - 1.0
        files = {
            relative: path
            for relative, path in files.items()
            if _mtime(path) >= floor
        }
    declared = _read_contract(skill_directory)
    matches = _match_declarations(declared, files)
    if matches:
        capture = "contract"
    else:
        capture = "fallback"
        matches = [
            (None, path)
            for relative, path in sorted(files.items())
            if _is_fallback_output(relative)
        ]
    registered: list[ArtifactRecord] = []
    for declared_path, path in matches:
        if _belongs_to_an_earlier_job(store, job_id, path):
            continue
        record = _register_one(
            store,
            job_id=job_id,
            session_id=session_id,
            path=path,
            declared=declared_path,
            capture=capture,
        )
        if record is None:
            continue
        registered.append(record)
        if emit is not None:
            try:
                emit(
                    "artifact.created",
                    {
                        "artifact_id": record.id,
                        "kind": record.kind,
                        "title": record.title,
                        "path": record.path,
                    },
                )
            except Exception:  # noqa: BLE001 - the event must not undo the row
                _log.exception("artifact.created emit failed for %s", record.id)
    return registered


def _belongs_to_an_earlier_job(
    store: ArtifactStore, job_id: str, path: Path
) -> bool:
    """Whether *path* was already claimed by another job and not changed
    since — the ledger half of attribution.

    The mtime floor alone cannot tell two jobs that ran a second apart
    apart (its slack has to absorb the clock), but the row a previous
    scan wrote remembers when it looked. A file unchanged since that row
    stays that job's artifact; a file rewritten afterwards (mtime newer
    than the row) is free for this job to claim, which is why a re-run
    of a skill still produces this run's artifacts.
    """
    existing = store.latest_for_path(str(path))
    if existing is None or existing.job_id == job_id:
        return False
    return _mtime(path) <= existing.created_at + 1.0


def _mtime(path: Path) -> float:
    """The mtime, or ``0.0`` when it cannot be read (a file the walk saw
    and the stat then lost is excluded by the floor, which is the safe
    direction: an unattributable file is not promoted to the tray)."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _read_contract(skill_directory: Path) -> list[str]:
    try:
        text = (skill_directory / "references" / CONTRACT_FILENAME).read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeDecodeError):
        return []
    return parse_output_contract(text)


def _match_declarations(
    declared: list[str], files: dict[str, Path]
) -> list[tuple[str, Path]]:
    """Declared paths that exist, matching by exact or suffix position.

    The contract's paths are relative to the skill's output root, which a
    job does not fix (the API entry writes wherever it writes), so
    ``tables/summary.csv`` matches ``run1/tables/summary.csv`` as well as
    a literal ``tables/summary.csv`` — a declaration is answered by the
    file it names, wherever that file sits under the workspace.
    """
    matches: list[tuple[str, Path]] = []
    used: set[str] = set()
    for declaration in declared:
        suffix = "/" + declaration
        for relative, path in sorted(files.items()):
            if relative in used:
                continue
            if relative == declaration or relative.endswith(suffix):
                used.add(relative)
                matches.append((declaration, path))
    return matches


def _is_fallback_output(relative: str) -> bool:
    parts = relative.split("/")
    if not any(part in FALLBACK_DIRECTORIES for part in parts[:-1]):
        return False
    kind, _ = kind_and_mime_for(relative)
    return kind != "other" or Path(relative).suffix.lower() in {
        ".json", ".txt", ".parquet", ".xlsx",
    }


def _register_one(
    store: ArtifactStore,
    *,
    job_id: str,
    session_id: str,
    path: Path,
    declared: str | None,
    capture: str,
) -> ArtifactRecord | None:
    try:
        if not path.is_file():
            return None
        kind, mime = kind_and_mime_for(path.name)
        sha256, size = hash_and_size(path)
    except OSError as exc:
        _log.warning("artifact scan could not read %s: %s", path, exc)
        return None
    meta: dict[str, str] = {"capture": capture}
    if declared:
        meta["declared"] = declared
    if not sha256:
        meta["sha256_skipped"] = "oversize"
    record = ArtifactRecord(
        id=new_artifact_id(),
        job_id=job_id,
        session_id=session_id,
        kind=kind,
        path=str(path),
        title=path.stem,
        mime=mime,
        sha256=sha256,
        size=size,
        produced_by="output_contract" if capture == "contract" else "fallback_scan",
        meta=meta,
        created_at=time.time(),
    )
    return store.insert_artifact(record)
