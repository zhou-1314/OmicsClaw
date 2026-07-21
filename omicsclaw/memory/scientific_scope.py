"""Explicit scientific-Memory domain scopes (ADR 0064).

ADR 0064 rejects transport-derived Memory partitioning: Owner Identity, Source
Namespace, Channel sender ID, Desktop launch ID, and the legacy Memory
Namespace never decide who owns a scientific Memory row. Instead every target
Memory write resolves one of six explicit *domain scopes* before mutation:

  - OWNER        — preferences, persona, Owner-wide non-scientific settings
  - PROJECT      — hypotheses, insights, analysis lineage, and references that
                   make scientific content part of one Project's continuity
  - WORKSPACE    — a catalog of observations about authorized pre-existing
                   local files
  - CONVERSATION — facts whose domain owner is one Conversation
  - RUN          — facts whose domain owner is one Run
  - SYSTEM       — seeded knowledge-base facts owned by the system

This module is intentionally pure: it owns the scope *vocabulary*, the
memory-type -> scope classification, and the canonical dataset-observation
version identity. Wiring the scopes into the Memory write path, the
Project-archive admission gate, and the projection applicator are separate
ADR-0064 concerns (later slices). ``Namespace`` remains a
migration/implementation field until the Memory schema represents the explicit
owner directly — this module does not remove it.

Note the deliberate distinction from ``omicsclaw.memory.scoped_memory``: that
module is a separate workspace-local Markdown store for heuristics, unrelated
to these graph-Memory scientific scopes.
"""

from __future__ import annotations

import hashlib
import posixpath
from enum import Enum

__all__ = [
    "MemoryScope",
    "DatasetPathError",
    "scope_for_memory_type",
    "is_project_fenced",
    "normalize_relative_path",
    "dataset_observation_identity",
    "provisional_dataset_observation_identity",
]


class MemoryScope(str, Enum):
    """The explicit domain owner of a scientific Memory row (ADR 0064)."""

    OWNER = "owner"
    PROJECT = "project"
    WORKSPACE = "workspace"
    CONVERSATION = "conversation"
    RUN = "run"
    SYSTEM = "system"


# Classification of the existing compat/graph Memory *types* (see
# omicsclaw/memory/compat.py) onto ADR 0064 scopes, keyed by ``memory_type``.
#
# ``dataset`` classifies as WORKSPACE: a dataset row is fundamentally an
# *observation* of a pre-existing local file (ADR 0064 §"Canonical dataset
# observations belong to Workspace or Attachment identity"). A Project's *use*
# of that dataset is a distinct Project-scoped Dataset Reference — the reference,
# not the observation, carries PROJECT scope, and it is a later ADR-0064 slice.
_TYPE_SCOPES: dict[str, MemoryScope] = {
    # Owner-wide, non-scientific.
    "preference": MemoryScope.OWNER,
    # Project scientific continuity: hypotheses / insights / lineage / context.
    "insight": MemoryScope.PROJECT,
    "analysis": MemoryScope.PROJECT,
    "autonomous_run": MemoryScope.PROJECT,
    "project_context": MemoryScope.PROJECT,
    "thread": MemoryScope.PROJECT,
    "thread_source": MemoryScope.PROJECT,
    # Observation of an authorized pre-existing local file.
    "dataset": MemoryScope.WORKSPACE,
}


def scope_for_memory_type(memory_type: str) -> MemoryScope | None:
    """Resolve the ADR-0064 scope for a compat/graph Memory ``memory_type``.

    Returns ``None`` for an unknown type so a caller fails closed rather than
    silently defaulting a novel scientific type to the wrong owner.
    """
    return _TYPE_SCOPES.get(str(memory_type or "").strip())


def is_project_fenced(scope: MemoryScope) -> bool:
    """Whether *novel* mutation of ``scope`` is fenced behind an active Project.

    Only PROJECT scope carries scientific continuity that Project archive
    closes. A WORKSPACE observation may still be updated by an unassigned Run,
    and OWNER / CONVERSATION / RUN / SYSTEM are not Project-owned — so the
    archive gate (a later slice) applies to PROJECT scope alone.
    """
    return scope is MemoryScope.PROJECT


class DatasetPathError(ValueError):
    """A dataset path was absolute or escaped its Workspace root."""


def normalize_relative_path(path: str) -> str:
    """Normalize a Workspace-relative dataset path for identity (ADR 0064 §2).

    The version identity of a Workspace dataset observation is
    ``(workspace_id, normalized_relative_path, observed_content_sha256)``. Two
    spellings of one relative path (``a/b.h5ad`` vs ``./a//b.h5ad``) must
    normalize to a single key; an absolute path or a ``..`` escape is rejected,
    because a dataset observation always lives *inside* its Workspace.
    """
    raw = str(path or "").strip()
    if not raw:
        raise DatasetPathError("dataset relative path cannot be empty")
    # Reject POSIX absolute, Windows UNC / backslash-absolute, and drive-letter
    # paths up front — a Workspace observation is never keyed by an absolute path.
    if raw.startswith(("/", "\\")) or (len(raw) >= 2 and raw[1] == ":"):
        raise DatasetPathError(f"dataset path must be Workspace-relative: {path!r}")
    unified = raw.replace("\\", "/")
    normalized = posixpath.normpath(unified)
    # ``normpath`` collapses ``.`` / ``//`` but keeps a leading ``..``; an empty
    # or ``.`` result means the path pointed at the Workspace root itself.
    if normalized in {"", "."} or normalized == ".." or normalized.startswith("../"):
        raise DatasetPathError(f"dataset path escapes its Workspace: {path!r}")
    return normalized


def _identity_digest(*parts: str) -> str:
    """Collision-resistant digest of length-prefixed parts.

    Length-prefixing each part means a delimiter appearing inside one component
    (e.g. a path containing ``:``) can never forge a different tuple's digest.
    """
    hasher = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        hasher.update(str(len(encoded)).encode("ascii"))
        hasher.update(b":")
        hasher.update(encoded)
        hasher.update(b"|")
    return hasher.hexdigest()


def _require_sha256(content_sha256: str) -> str:
    digest = str(content_sha256 or "").strip().lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("content_sha256 must be a 64-hex SHA-256 digest")
    return digest


def _require_workspace(workspace_id: str) -> str:
    workspace = str(workspace_id or "").strip()
    if not workspace:
        raise ValueError("workspace_id is required for a dataset observation")
    return workspace


def dataset_observation_identity(
    *, workspace_id: str, relative_path: str, content_sha256: str
) -> str:
    """Canonical version identity of a *settled* Workspace dataset observation.

    ``(workspace_id, normalized_relative_path, observed_content_sha256)`` — the
    digest is authoritative, so the same bytes at the same Workspace path are
    one scientific object regardless of the Surface that mentioned them, and a
    display filename alone never dedups (ADR 0064 §2). The returned key carries
    a ``dataset-obs:`` prefix so it can never collide with a provisional one.
    """
    workspace = _require_workspace(workspace_id)
    digest = _require_sha256(content_sha256)
    rel = normalize_relative_path(relative_path)
    return f"dataset-obs:{_identity_digest(workspace, rel, digest)}"


def provisional_dataset_observation_identity(
    *,
    workspace_id: str,
    relative_path: str,
    observed_size: int,
    observed_mtime_ns: int,
) -> str:
    """Provisional identity before a content digest is established (ADR 0064 §2).

    ``(observed_size, observed_mtime_ns)`` may *identify* a provisional
    observation at one Workspace path, but it MUST NOT merge with another path
    or claim byte equality. This keys on the path too, and the distinct
    ``dataset-prov:`` prefix guarantees a provisional row can never be mistaken
    for — or dedup against — a settled digest-bound observation.
    """
    workspace = _require_workspace(workspace_id)
    rel = normalize_relative_path(relative_path)
    size = int(observed_size)
    mtime_ns = int(observed_mtime_ns)
    if size < 0 or mtime_ns < 0:
        raise ValueError("observed_size and observed_mtime_ns must be non-negative")
    return f"dataset-prov:{_identity_digest(workspace, rel, str(size), str(mtime_ns))}"
