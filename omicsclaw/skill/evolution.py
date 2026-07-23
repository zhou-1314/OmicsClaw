"""Evidence-bound skill health aggregation and human-gated evolution proposals.

Run events deliberately store fingerprints and identifiers rather than raw
stderr, input paths, or data.  Automated logic can only submit candidates.
The low-level proposal store is persistence Implementation; the product
writeback Interface is owned by ``SkillEvolutionGovernance``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager
import ctypes
from dataclasses import MISSING, asdict, dataclass, field, replace
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import stat as stat_module
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, TYPE_CHECKING
import uuid

from .outcomes import SkillErrorKind
from .execution.environment import scrub_internal_control_credentials

try:  # pragma: no cover - Windows fallback keeps process-local locking
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

try:  # pragma: no cover - imported only on Windows
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from .result import SkillRunResult


_SKILL_DEFECT_KINDS = frozenset(
    {SkillErrorKind.SCRIPT_DEFECT.value, SkillErrorKind.CONTRACT_FAILURE.value}
)
_ENVIRONMENT_FAILURE_KINDS = frozenset(
    {
        SkillErrorKind.MISSING_DEPENDENCY.value,
        SkillErrorKind.TIMEOUT.value,
        SkillErrorKind.RESOURCE_EXHAUSTED.value,
    }
)
_FRAMEWORK_FAILURE_KINDS = frozenset(
    {SkillErrorKind.CONTRACT_VALIDATOR_FAILED.value}
)
_EXECUTION_SOURCE_SUFFIXES = frozenset({".py", ".r", ".sh", ".bash"})
_SKILL_RUNTIME_ASSET_SUFFIXES = frozenset(
    {".json", ".yaml", ".yml", ".tsv", ".tmpl", ".jinja", ".j2"}
)
_IGNORED_EXECUTION_SOURCE_DIRECTORIES = frozenset(
    {
        ".cache",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "output",
        "outputs",
        "references",
        "test",
        "tests",
    }
)
_LEDGER_LOCK = threading.RLock()
_RUNTIME_PROBE_TIMEOUT_SECONDS = 15.0
_RUNTIME_PROBE_MAX_STREAM_BYTES = 64 * 1024
_RUNTIME_EXECUTABLE_MAX_BYTES = 512 * 1024 * 1024
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PYTHON_RUNTIME_PROBE = """\
import hashlib
import importlib.metadata
import json
import os
import platform
import sys

packages = json.loads(sys.argv[1])
dependencies = {}
for package in packages:
    try:
        version = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        version = "missing"
    dependencies[package] = version

def private_path_id(value):
    if not value:
        return "none"
    canonical = os.path.realpath(value)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

payload = {
    "schema_version": 1,
    "strategy": "python-runtime-probe-v1",
    "os": {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
    },
    "python": {
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
        "cache_tag": str(getattr(sys.implementation, "cache_tag", "")),
    },
    "environment": {
        "prefix_id": private_path_id(sys.prefix),
        "base_prefix_id": private_path_id(getattr(sys, "base_prefix", sys.prefix)),
        "virtual_env_id": private_path_id(os.environ.get("VIRTUAL_ENV", "")),
    },
    "dependencies": dependencies,
}
print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
"""


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _unresolved_skill_id(value: str) -> str:
    """Return a privacy-safe identity for a pre-registry caller value.

    A failed lookup may contain an input path, credential fragment, or other
    user-controlled text.  The audit ledger must retain neither that value nor
    a reversible encoding of it.
    """
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:24]
    return f"unresolved-{digest}"


def _is_symbolic_link(path: Path) -> bool:
    """Inspect one directory entry without following its target."""
    try:
        return stat_module.S_ISLNK(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def _source_stat_identity(value: os.stat_result) -> tuple[int, ...]:
    """Return metadata that changes when a source or directory is replaced."""
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
    )


def _inventory_execution_source_root(
    root: Path,
    *,
    include_skill_manifests: bool = False,
    include_runtime_assets: bool = False,
) -> tuple[
    tuple[tuple[str, str, tuple[int, ...]], ...],
    list[tuple[Path, Path, tuple[int, ...]]],
]:
    """Inventory one source root without following symbolic links.

    Directory metadata is part of the witness so a source appearing or
    disappearing while the hash is being captured cannot produce a mixed-time
    identity. Every symbolic link encountered in the governed inventory is
    rejected: even a currently benign link can be retargeted between inventory
    and read, while a directory link can silently expand the closure outside
    the canonical project root. Explicitly excluded cache/output/test/reference
    subtrees are not traversed and are not execution authority.
    """

    inventory: list[tuple[str, str, tuple[int, ...]]] = []
    sources: list[tuple[Path, Path, tuple[int, ...]]] = []

    def is_hashed_file(path: Path, *, is_root: bool) -> bool:
        suffix = path.suffix.casefold()
        return (
            is_root
            or suffix in _EXECUTION_SOURCE_SUFFIXES
            or (include_skill_manifests and path.name == "skill.yaml")
            or (
                include_runtime_assets
                and path.name != "skill.yaml"
                and suffix in _SKILL_RUNTIME_ASSET_SUFFIXES
            )
        )

    def inspect(path: Path, relative_path: Path, *, is_root: bool = False) -> None:
        try:
            path_stat = os.lstat(path)
        except OSError as exc:
            raise ValueError(
                f"execution source tree changed during hashing: {path}"
            ) from exc
        if stat_module.S_ISLNK(path_stat.st_mode):
            raise ValueError(
                f"execution source tree contains a symbolic link: {path}"
            )

        identity = _source_stat_identity(path_stat)
        relative_name = relative_path.as_posix()
        if stat_module.S_ISREG(path_stat.st_mode):
            if is_hashed_file(path, is_root=is_root):
                inventory.append(("source", relative_name, identity))
                sources.append((relative_path, path, identity))
            return
        if not stat_module.S_ISDIR(path_stat.st_mode):
            if is_hashed_file(path, is_root=is_root):
                raise ValueError(
                    f"execution source path is not a regular file: {path}"
                )
            return
        if not is_root and path.name in _IGNORED_EXECUTION_SOURCE_DIRECTORIES:
            return

        inventory.append(("directory", relative_name, identity))
        try:
            with os.scandir(path) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
        except OSError as exc:
            raise ValueError(
                f"execution source tree changed during hashing: {path}"
            ) from exc
        for child in children:
            child_path = path / child.name
            child_relative = relative_path / child.name
            # ``inspect`` uses lstat again rather than DirEntry.is_dir(), so a
            # link is never followed and a replacement after scandir is seen.
            inspect(child_path, child_relative)

    if root.is_absolute():
        absolute_root = root
    else:  # callers normally pass resolved roots; keep direct use deterministic
        absolute_root = root.absolute()
    try:
        root_stat = os.lstat(absolute_root)
    except OSError as exc:
        raise ValueError(
            f"execution source tree changed during hashing: {absolute_root}"
        ) from exc
    if stat_module.S_ISREG(root_stat.st_mode):
        inspect(absolute_root, Path(absolute_root.name), is_root=True)
    else:
        inspect(absolute_root, Path("."), is_root=True)
    return tuple(inventory), sources


def _read_stable_execution_source(
    path: Path,
    expected_identity: tuple[int, ...],
) -> bytes:
    """Read a regular source through a no-follow descriptor and verify it."""

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"execution source changed during hashing: {path}") from exc
    try:
        before = os.fstat(descriptor)
        before_identity = _source_stat_identity(before)
        if (
            not stat_module.S_ISREG(before.st_mode)
            or before_identity != expected_identity
        ):
            raise ValueError(f"execution source changed during hashing: {path}")

        def read_all() -> bytes:
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)

        payload = read_all()
        middle_identity = _source_stat_identity(os.fstat(descriptor))
        os.lseek(descriptor, 0, os.SEEK_SET)
        verification_payload = read_all()
        after_identity = _source_stat_identity(os.fstat(descriptor))
        if (
            middle_identity != before_identity
            or after_identity != before_identity
            or verification_payload != payload
        ):
            raise ValueError(f"execution source changed during hashing: {path}")
    finally:
        os.close(descriptor)

    try:
        path_identity = _source_stat_identity(os.lstat(path))
    except OSError as exc:
        raise ValueError(f"execution source changed during hashing: {path}") from exc
    if path_identity != expected_identity:
        raise ValueError(f"execution source changed during hashing: {path}")
    return payload


def _read_stable_manifest(path: Path) -> tuple[bytes, tuple[int, ...]]:
    """Capture one manifest without following links or accepting a torn read."""

    inventory, sources = _inventory_execution_source_root(path)
    if len(sources) != 1:
        raise ValueError(f"canonical Skill manifest is not a regular file: {path}")
    _relative_path, source_path, source_identity = sources[0]
    payload = _read_stable_execution_source(source_path, source_identity)
    observed_inventory, _observed_sources = _inventory_execution_source_root(path)
    if observed_inventory != inventory:
        raise ValueError(
            f"canonical Skill manifest changed during identity capture: {path}"
        )
    return payload, source_identity


def _execution_source_roots(
    script_path: str | Path,
    *,
    skills_root: str | Path,
    skill_dir: str | Path | None = None,
    directory_name: str = "",
) -> tuple[Path, list[tuple[str, Path]]]:
    """Resolve one Skill tree and its canonical shared-library ancestors."""
    entry_input = Path(script_path).expanduser()
    root_input = Path(skills_root).expanduser()
    if _is_symbolic_link(entry_input):
        raise ValueError(f"runtime entry is a symbolic link: {entry_input}")
    if _is_symbolic_link(root_input):
        raise ValueError(f"canonical skills root is a symbolic link: {root_input}")
    entry = entry_input.resolve()
    root = root_input.resolve()
    if not entry.is_file():
        raise FileNotFoundError(f"runtime entry is not a file: {entry}")
    if not root.is_dir():
        raise NotADirectoryError(f"canonical skills root is not a directory: {root}")
    try:
        relative_entry = entry.relative_to(root)
    except ValueError as exc:
        raise ValueError("runtime entry is outside the canonical skills root") from exc
    if len(relative_entry.parts) < 2:
        raise ValueError("runtime entry must be nested below a canonical domain")

    domain_dir = root / relative_entry.parts[0]
    resolved_skill_dir: Path | None = None
    if skill_dir is not None:
        skill_dir_input = Path(skill_dir).expanduser()
        if _is_symbolic_link(skill_dir_input):
            raise ValueError(
                f"canonical Skill directory is a symbolic link: {skill_dir_input}"
            )
        resolved_skill_dir = skill_dir_input.resolve()
        if not resolved_skill_dir.is_dir():
            raise NotADirectoryError(
                f"canonical Skill directory is not a directory: {resolved_skill_dir}"
            )
        try:
            entry.relative_to(resolved_skill_dir)
            resolved_skill_dir.relative_to(domain_dir)
        except ValueError as exc:
            raise ValueError(
                "canonical Skill directory does not contain the runtime entry"
            ) from exc
    else:
        expected_name = str(directory_name).strip()
        for ancestor in (entry.parent, *entry.parent.parents):
            if ancestor == root:
                break
            try:
                ancestor.relative_to(domain_dir)
            except ValueError:
                break
            if expected_name:
                if ancestor.name == expected_name and (ancestor / "skill.yaml").is_file():
                    resolved_skill_dir = ancestor
                    break
            elif (ancestor / "skill.yaml").is_file():
                resolved_skill_dir = ancestor
                break
        if expected_name and resolved_skill_dir is None:
            raise ValueError(
                "runtime entry does not resolve to the Registry Skill directory"
            )
    if resolved_skill_dir is None:
        # Legacy/test fixtures may not have a manifest.  Keep their closure
        # bounded to the entry directory; production callers pass the Registry
        # directory identity or the governance manifest parent explicitly.
        resolved_skill_dir = entry.parent

    manifest_path = resolved_skill_dir / "skill.yaml"
    has_manifest = manifest_path.is_file()
    roots: list[tuple[str, Path]] = [("skill", resolved_skill_dir)]
    if has_manifest:
        # Keep the target's own manifest identity stable across a governed
        # directory relocation. The project Skills scan will physically
        # de-duplicate this path while sibling manifests retain their
        # project-relative identity.
        roots.append(("skill_manifest", manifest_path))
    shared_parents: list[Path] = []
    ancestor = resolved_skill_dir.parent
    while ancestor != root:
        try:
            ancestor.relative_to(domain_dir)
        except ValueError:
            break
        shared_parents.append(ancestor)
        if ancestor == domain_dir:
            break
        ancestor = ancestor.parent
    if resolved_skill_dir == domain_dir:
        shared_parents.append(domain_dir)

    for shared_parent in reversed(shared_parents):
        shared_lib = shared_parent / "_lib"
        if not shared_lib.is_dir():
            continue
        # A root Skill (for example ``skills/literature``) already hashes its
        # complete tree, so do not include an internal ``_lib`` twice.
        try:
            shared_lib.relative_to(resolved_skill_dir)
        except ValueError:
            pass
        else:
            continue
        if shared_parent == domain_dir:
            namespace = "domain_lib"
        else:
            namespace = (
                "ancestor_lib/" + shared_parent.relative_to(root).as_posix()
            )
        roots.append((namespace, shared_lib))

    # Project-wide revisions are attached only to a manifest-backed Registry
    # Skill. Legacy direct callers without ``skill.yaml`` retain their bounded
    # per-directory closure; treating an arbitrary compatibility collection as
    # a complete project would pull unrelated sibling trees into the identity.
    if has_manifest:
        project_runtime = root.parent / "omicsclaw"
        roots.append(("project_skills", root))
        if _is_symbolic_link(project_runtime):
            raise ValueError(f"project runtime is a symbolic link: {project_runtime}")
        if project_runtime.is_dir():
            roots.append(("project_runtime", project_runtime))
        project_scripts = root.parent / "scripts"
        if _is_symbolic_link(project_scripts):
            raise ValueError(
                f"project scripts root is a symbolic link: {project_scripts}"
            )
        if project_scripts.is_dir():
            roots.append(("project_scripts", project_scripts))
        project_entry = root.parent / "omicsclaw.py"
        if _is_symbolic_link(project_entry):
            raise ValueError(f"project entry is a symbolic link: {project_entry}")
        if project_entry.is_file():
            roots.append(("project_entry", project_entry))
    # The resolved runtime entry is authority regardless of language or suffix.
    # Source-language allowlists cover helpers, but can never make the manifest's
    # own dispatch target decorative.
    roots.append(("runtime_entry", entry))
    return entry, roots


def compute_execution_source_hash(
    script_path: str | Path,
    *,
    skills_root: str | Path,
    skill_dir: str | Path | None = None,
    _source_transition: tuple[Path, bytes, bytes] | None = None,
) -> str:
    """Hash the root-bounded executable source closure for one Skill execution.

    The runtime entry alone is not a sufficient execution identity: Skills
    commonly import Python or R helpers from their own directory, from the
    canonical ``skills_root/<domain>/_lib`` package, and from the fixed project
    execution roots: the complete Skills tree, sibling ``omicsclaw`` runtime,
    sibling ``scripts`` tree, and top-level ``omicsclaw.py`` entry.  A Skill may
    sit directly below the domain or below a subdomain such as
    ``singlecell/scrna``.  The domain is always the first path component below
    the explicit root, so an unrelated outer ``_lib`` cannot enter the hash and
    a subdomain ``_lib`` cannot shadow the domain library. Existing subdomain
    libraries are included conservatively in addition to the domain library.

    Production callers bind ``skill_dir`` from the same Registry snapshot or
    governance manifest that supplied the entry.  If omitted, the nearest
    manifest-bearing ancestor is used only as a compatibility convenience for
    direct callers.  An explicitly bound nested ``runtime.entry`` therefore
    still covers sibling helpers and cannot narrow the closure with a decoy
    inner manifest. Python, R, and shell sources are hashed in stable
    relative-path order, while the resolved runtime entry is always included
    regardless of suffix. The exact ``skill.yaml`` basename is also hashed below
    the project Skills root because Registry-driven execution can read sibling
    manifests at runtime. A conservative Skill-local runtime-asset allowlist
    also covers prompt templates and JSON/YAML/TSV marker/config assets without
    a size bypass; cache, output, tests, references, and unrecognised binary demo
    data stay outside this partial closure. An explicit ``runtime.assets``
    manifest contract is still required to close the remaining open world.
    """
    _entry, roots = _execution_source_roots(
        script_path,
        skills_root=skills_root,
        skill_dir=skill_dir,
    )
    transition_path = (
        Path(os.path.abspath(_source_transition[0]))
        if _source_transition is not None
        else None
    )
    transition_applied = False
    sources: list[tuple[str, Path, Path, tuple[int, ...]]] = []
    inventories: list[
        tuple[Path, bool, bool, tuple[tuple[str, str, tuple[int, ...]], ...]]
    ] = []
    seen_sources: set[Path] = set()
    manifest_backed = any(namespace == "skill_manifest" for namespace, _ in roots)
    for namespace, root in roots:
        include_skill_manifests = namespace == "project_skills"
        include_runtime_assets = manifest_backed and namespace in {
            "skill",
            "project_runtime",
            "project_scripts",
        }
        inventory, candidates = _inventory_execution_source_root(
            root,
            include_skill_manifests=include_skill_manifests,
            include_runtime_assets=include_runtime_assets,
        )
        inventories.append(
            (root, include_skill_manifests, include_runtime_assets, inventory)
        )
        for relative_path, path, source_stat_identity in candidates:
            # Roots overlap deliberately: per-Skill and shared-library
            # namespaces retain precedence, while project-wide roots add only
            # previously unseen sibling/runtime sources.
            source_identity = path
            if source_identity in seen_sources:
                continue
            seen_sources.add(source_identity)
            sources.append(
                (namespace, relative_path, path, source_stat_identity)
            )

    digest = hashlib.sha256()
    for namespace, relative_path, path, source_stat_identity in sorted(
        sources,
        key=lambda item: (item[0], item[1].as_posix()),
    ):
        identity = f"{namespace}/{relative_path.as_posix()}".encode("utf-8")
        payload = _read_stable_execution_source(path, source_stat_identity)
        digest_payload = payload
        if transition_path is not None and path == transition_path:
            expected_payload = _source_transition[1]
            if payload != expected_payload:
                raise ValueError(
                    f"execution source changed before planned transition: {path}"
                )
            digest_payload = _source_transition[2]
            transition_applied = True
        digest.update(len(identity).to_bytes(8, "big"))
        digest.update(identity)
        digest.update(len(digest_payload).to_bytes(8, "big"))
        digest.update(digest_payload)

    # Re-enumerate after all reads. This closes the window in which a source
    # could appear/disappear between the initial inventory and digest return.
    for (
        root,
        include_skill_manifests,
        include_runtime_assets,
        expected_inventory,
    ) in inventories:
        observed_inventory, _sources = _inventory_execution_source_root(
            root,
            include_skill_manifests=include_skill_manifests,
            include_runtime_assets=include_runtime_assets,
        )
        if observed_inventory != expected_inventory:
            raise ValueError(
                f"execution source tree changed during hashing: {root}"
            )
    if transition_path is not None and not transition_applied:
        raise ValueError(
            "planned transition path is outside the execution source closure: "
            f"{transition_path}"
        )
    return "sha256:" + digest.hexdigest()


def _capture_planned_skill_execution_identity(
    script_path: str | Path,
    *,
    skills_root: str | Path,
    skill_dir: str | Path,
    transition_path: str | Path,
    expected_transition_payload: bytes,
    planned_transition_payload: bytes,
) -> tuple[str, str]:
    """Compute the exact post-transition identity from one stable live tree.

    The governed target still contains ``expected_transition_payload`` while
    this function runs.  The source digest substitutes only that verified file
    with the planned bytes; every other source is read through the same
    no-follow, stable-inventory path as ordinary execution identity capture.
    A final ordinary capture after publication can therefore compare against
    this expected pair without confusing the governed manifest edit with
    unrelated source drift.
    """

    entry, roots = _execution_source_roots(
        script_path,
        skills_root=skills_root,
        skill_dir=skill_dir,
    )
    resolved_skill_dir = roots[0][1]
    manifest_path = resolved_skill_dir / "skill.yaml"
    transition = Path(os.path.abspath(Path(transition_path).expanduser()))

    manifest_payload, manifest_identity = _read_stable_manifest(manifest_path)
    transition_payload, transition_identity = _read_stable_manifest(transition)
    if transition_payload != expected_transition_payload:
        raise ValueError(
            f"planned transition source changed before identity capture: {transition}"
        )
    source_hash = compute_execution_source_hash(
        entry,
        skills_root=skills_root,
        skill_dir=resolved_skill_dir,
        _source_transition=(
            transition,
            expected_transition_payload,
            planned_transition_payload,
        ),
    )
    observed_manifest, observed_manifest_identity = _read_stable_manifest(
        manifest_path
    )
    observed_transition, observed_transition_identity = _read_stable_manifest(
        transition
    )
    if (
        observed_manifest != manifest_payload
        or observed_manifest_identity != manifest_identity
        or observed_transition != transition_payload
        or observed_transition_identity != transition_identity
    ):
        raise ValueError(
            "execution authority changed during planned identity capture"
        )
    planned_manifest_payload = (
        planned_transition_payload
        if manifest_path == transition
        else manifest_payload
    )
    return _sha256(planned_manifest_payload), source_hash


def capture_skill_execution_identity(
    script_path: str | Path,
    *,
    skills_root: str | Path,
    skill_dir: str | Path | None = None,
    directory_name: str = "",
) -> tuple[str, str]:
    """Capture one mutually consistent manifest and execution-source identity."""
    requires_manifest = skill_dir is not None or bool(str(directory_name).strip())
    entry, roots = _execution_source_roots(
        script_path,
        skills_root=skills_root,
        skill_dir=skill_dir,
        directory_name=directory_name,
    )
    resolved_skill_dir = roots[0][1]
    manifest_path = resolved_skill_dir / "skill.yaml"
    try:
        os.lstat(manifest_path)
    except FileNotFoundError:
        if requires_manifest:
            raise ValueError(
                f"canonical Skill manifest is missing: {manifest_path}"
            ) from None
        return "unknown", compute_execution_source_hash(
            entry,
            skills_root=skills_root,
            skill_dir=resolved_skill_dir,
        )

    manifest_payload, manifest_identity = _read_stable_manifest(manifest_path)
    source_hash = compute_execution_source_hash(
        entry,
        skills_root=skills_root,
        skill_dir=resolved_skill_dir,
    )
    observed_payload, observed_identity = _read_stable_manifest(manifest_path)
    if (
        observed_payload != manifest_payload
        or observed_identity != manifest_identity
    ):
        raise ValueError(
            "canonical Skill manifest changed during identity capture: "
            f"{manifest_path}"
        )
    return _sha256(manifest_payload), source_hash


def compute_environment_id(
    skill_info: Mapping[str, Any] | None,
    *,
    runtime_source: str,
    runtime_executable: str,
    runtime_env: Mapping[str, str],
    runtime_cwd: str | Path,
    runtime_language: str = "python",
) -> str:
    """Fingerprint the actual selected producer without persisting raw paths.

    Python runtimes must report their implementation/version and declared
    dependency versions from the exact executable, environment and cwd that
    will serve the Skill.  The parent validates a bounded canonical JSON probe
    and combines it with a digest of the selected executable.  No parent
    package metadata is accepted as a fallback.

    Bash/R/other declared runtimes use the deliberately narrower
    ``non-python-executable-v1`` strategy: bind the executable selected through
    the actual PATH (or mark it unavailable so the normal missing-runtime path
    remains actionable), while explicitly recording that dependency-version
    evidence is unsupported for that runtime family.
    """

    source = _bounded_control_free_text(runtime_source, label="runtime source")
    language = _bounded_control_free_text(
        str(runtime_language).strip().casefold(),
        label="runtime language",
    )
    environment = scrub_internal_control_credentials(
        {str(key): str(value) for key, value in runtime_env.items()}
    )
    cwd = Path(runtime_cwd).expanduser()
    try:
        cwd = cwd.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError("runtime evidence cwd is unavailable") from exc
    if not cwd.is_dir():
        raise RuntimeError("runtime evidence cwd is unavailable")

    executable_evidence = _runtime_executable_evidence(
        runtime_executable,
        runtime_env=environment,
        runtime_cwd=cwd,
    )
    packages = _declared_dependency_packages(skill_info)
    if language == "python":
        if not executable_evidence["available"]:
            raise RuntimeError("selected Python runtime is unavailable")
        probe = _probe_python_runtime(
            runtime_executable,
            runtime_env=environment,
            runtime_cwd=cwd,
            packages=packages,
        )
        basis: dict[str, Any] = {
            "schema_version": 1,
            "strategy": "python-producer-environment-v1",
            "runtime_source": source,
            "runtime_language": language,
            "executable": executable_evidence,
            "probe": probe,
        }
    else:
        basis = {
            "schema_version": 1,
            "strategy": "non-python-executable-v1",
            "runtime_source": source,
            "runtime_language": language,
            "executable": executable_evidence,
            "declared_dependencies": packages,
            "dependency_version_evidence": "unsupported",
            "host": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
            },
        }
    canonical = json.dumps(
        basis,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "env:" + hashlib.sha256(canonical).hexdigest()[:20]


def _unbound_environment_id(*, runtime_source: str) -> str:
    """Identify a pre-execution failure without claiming producer evidence."""

    source = str(runtime_source or "unbound")[:128]
    canonical = json.dumps(
        {
            "schema_version": 1,
            "strategy": "unbound-pre-execution-v1",
            "runtime_source": source,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "env:" + hashlib.sha256(canonical).hexdigest()[:20]


def _declared_dependency_packages(
    skill_info: Mapping[str, Any] | None,
) -> list[str]:
    packages: set[str] = set()
    for requirement in map(str, (skill_info or {}).get("requires") or []):
        package = re.split(
            r"[<>=!~;\s\[]",
            requirement.strip(),
            maxsplit=1,
        )[0].strip().casefold()
        if package:
            packages.add(package)
    return sorted(packages)


def _bounded_control_free_text(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise RuntimeError(f"{label} is invalid")
    return value


def _runtime_executable_evidence(
    runtime_executable: str,
    *,
    runtime_env: Mapping[str, str],
    runtime_cwd: Path,
) -> dict[str, Any]:
    """Return path-private evidence for the executable selected by real env."""

    command = str(runtime_executable)
    command_path = Path(command)
    selected: Path | None
    if command_path.is_absolute() or command_path.parent != Path("."):
        selected = command_path if command_path.is_absolute() else runtime_cwd / command_path
    else:
        found = shutil.which(command, path=runtime_env.get("PATH", os.defpath))
        selected = Path(found) if found else None
    if selected is None:
        return {
            "available": False,
            "command_id": _sha256(command.encode("utf-8")),
        }
    try:
        lexical = Path(os.path.abspath(selected))
        resolved = lexical.resolve(strict=True)
        entry_stat = resolved.stat()
    except (OSError, RuntimeError):
        return {
            "available": False,
            "command_id": _sha256(command.encode("utf-8")),
        }
    if (
        not stat_module.S_ISREG(entry_stat.st_mode)
        or entry_stat.st_size > _RUNTIME_EXECUTABLE_MAX_BYTES
        or not os.access(resolved, os.X_OK)
    ):
        return {
            "available": False,
            "command_id": _sha256(command.encode("utf-8")),
        }
    digest = hashlib.sha256()
    total_bytes = 0
    try:
        with resolved.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (
                entry_stat.st_dev,
                entry_stat.st_ino,
            ):
                raise RuntimeError("selected runtime executable changed while opening")
            while chunk := handle.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > _RUNTIME_EXECUTABLE_MAX_BYTES:
                    raise RuntimeError("selected runtime executable exceeds size limit")
                digest.update(chunk)
    except OSError as exc:
        raise RuntimeError("selected runtime executable cannot be fingerprinted") from exc
    try:
        observed = resolved.stat()
    except OSError as exc:
        raise RuntimeError("selected runtime executable changed while hashing") from exc
    if (
        total_bytes != entry_stat.st_size
        or (observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns)
        != (
            entry_stat.st_dev,
            entry_stat.st_ino,
            entry_stat.st_size,
            entry_stat.st_mtime_ns,
        )
    ):
        raise RuntimeError("selected runtime executable changed while hashing")
    selection_basis = (
        str(lexical) + "\0" + str(resolved)
    ).encode("utf-8")
    return {
        "available": True,
        "content_sha256": "sha256:" + digest.hexdigest(),
        "selection_id": _sha256(selection_basis),
        "size_bytes": entry_stat.st_size,
    }


def _probe_python_runtime(
    runtime_executable: str,
    *,
    runtime_env: Mapping[str, str],
    runtime_cwd: Path,
    packages: list[str],
) -> dict[str, Any]:
    completed = _run_bounded_runtime_probe(
        [
            str(runtime_executable),
            "-P",
            "-c",
            _PYTHON_RUNTIME_PROBE,
            json.dumps(packages, separators=(",", ":")),
        ],
        runtime_env=runtime_env,
        runtime_cwd=runtime_cwd,
    )
    if completed.returncode != 0:
        raise RuntimeError("selected Python runtime evidence probe failed")
    try:
        decoded = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("selected Python runtime evidence probe is invalid") from exc
    return _validate_python_runtime_probe(decoded, packages=packages)


@dataclass(frozen=True, slots=True)
class _BoundedProbeResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def _run_bounded_runtime_probe(
    argv: list[str],
    *,
    runtime_env: Mapping[str, str],
    runtime_cwd: Path,
) -> _BoundedProbeResult:
    """Run one producer probe with bounded time and retained output memory."""

    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": scrub_internal_control_credentials(runtime_env),
        "cwd": str(runtime_cwd),
    }
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(argv, **kwargs)
    except (OSError, ValueError) as exc:
        raise RuntimeError("selected Python runtime evidence probe could not start") from exc
    streams: dict[str, bytes] = {}
    errors: list[BaseException] = []

    def drain(name: str, stream: Any) -> None:
        retained = bytearray()
        overflow = False
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                available = _RUNTIME_PROBE_MAX_STREAM_BYTES - len(retained)
                if available > 0:
                    retained.extend(chunk[:available])
                if len(chunk) > available:
                    overflow = True
        except BaseException as exc:  # pragma: no cover - OS pipe failure
            errors.append(exc)
        finally:
            streams[name] = bytes(retained)
            if overflow:
                errors.append(RuntimeError(f"runtime probe {name} exceeded limit"))

    assert process.stdout is not None and process.stderr is not None
    readers = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        returncode = process.wait(timeout=_RUNTIME_PROBE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        _terminate_runtime_probe(process)
        raise RuntimeError("selected Python runtime evidence probe timed out") from exc
    finally:
        for reader in readers:
            reader.join(timeout=2.0)
        if any(reader.is_alive() for reader in readers):
            _terminate_runtime_probe(process)
            for reader in readers:
                reader.join(timeout=2.0)
        process.stdout.close()
        process.stderr.close()
    if any(reader.is_alive() for reader in readers) or errors:
        raise RuntimeError("selected Python runtime evidence probe output is invalid")
    return _BoundedProbeResult(
        returncode=returncode,
        stdout=streams.get("stdout", b""),
        stderr=streams.get("stderr", b""),
    )


def _terminate_runtime_probe(process: subprocess.Popen[Any]) -> None:
    try:
        if os.name != "nt" and process.pid > 0:
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover - Windows fallback
            process.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=2.0)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _validate_python_runtime_probe(
    value: object,
    *,
    packages: list[str],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "strategy",
        "os",
        "python",
        "environment",
        "dependencies",
    }:
        raise RuntimeError("selected Python runtime evidence probe has wrong schema")
    if value.get("schema_version") != 1 or value.get("strategy") != (
        "python-runtime-probe-v1"
    ):
        raise RuntimeError("selected Python runtime evidence probe has wrong version")
    os_payload = value.get("os")
    python_payload = value.get("python")
    environment_payload = value.get("environment")
    dependencies = value.get("dependencies")
    if (
        not isinstance(os_payload, dict)
        or set(os_payload) != {"system", "release", "machine"}
        or not isinstance(python_payload, dict)
        or set(python_payload) != {"implementation", "version", "cache_tag"}
        or not isinstance(environment_payload, dict)
        or set(environment_payload)
        != {"prefix_id", "base_prefix_id", "virtual_env_id"}
        or not isinstance(dependencies, dict)
        or set(dependencies) != set(packages)
    ):
        raise RuntimeError("selected Python runtime evidence probe has wrong fields")
    for field_value in (*os_payload.values(), *python_payload.values()):
        _bounded_control_free_text(field_value, label="runtime probe field")
    for field_value in environment_payload.values():
        if field_value != "none" and (
            not isinstance(field_value, str) or not _SHA256_RE.fullmatch(field_value)
        ):
            raise RuntimeError("selected Python runtime path identity is invalid")
    for package, version in dependencies.items():
        _bounded_control_free_text(package, label="runtime dependency name")
        _bounded_control_free_text(version, label="runtime dependency version")
    return {
        "schema_version": 1,
        "strategy": "python-runtime-probe-v1",
        "os": dict(os_payload),
        "python": dict(python_payload),
        "environment": dict(environment_payload),
        "dependencies": dict(sorted(dependencies.items())),
    }


@contextmanager
def _exclusive_file_lock(path: Path):
    """Serialize JSONL reads/state transitions across threads and processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LEDGER_LOCK:
        with path.open("a+b") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            elif msvcrt is not None:  # pragma: no cover - exercised on Windows
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if exc.errno not in {errno.EACCES, errno.EDEADLK}:
                            raise
                        time.sleep(0.05)
            else:  # pragma: no cover - unsupported Python platform
                raise RuntimeError("cross-process file locking is unavailable")
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover - exercised on Windows
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@dataclass(frozen=True, slots=True)
class SkillRunEvent:
    event_id: str
    occurred_at: str
    run_id: str
    skill_id: str
    skill_version: str
    skill_hash: str
    environment_id: str
    outcome: str
    error_kind: str
    exit_code: int
    duration_seconds: float
    # ``run_id`` is reserved for the authoritative Project Run identity.  A
    # privacy-safe fingerprint is kept separately for evidence deduplication.
    evidence_kind: str = "ordinary"
    execution_fingerprint: str = ""
    # Conservative project execution revision: the bound runtime entry,
    # target/domain sources, project Skill/runtime/script sources and manifests,
    # plus the bounded runtime-asset suffix set. ``skill_hash`` remains the
    # target manifest identity. This revision is intentionally broader than a
    # static import closure and still does not prove undeclared open-world assets.
    source_hash: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    source: str = "skill-runner"
    thread_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SkillRunEvent":
        # Old append-only ledger rows predate the two evidence fields.  Their
        # defaults keep history readable without retroactively treating an
        # ordinary success as explicit demo evidence.
        payload: dict[str, Any] = {}
        for name, definition in cls.__dataclass_fields__.items():
            if name in value:
                payload[name] = value[name]
            elif definition.default is not MISSING:
                payload[name] = definition.default
            elif definition.default_factory is not MISSING:
                payload[name] = definition.default_factory()
            else:
                raise KeyError(name)
        return cls(**payload)

    @classmethod
    def from_result(
        cls,
        result: "SkillRunResult",
        *,
        skill_id: str = "",
        run_id: str = "",
        skill_version: str = "",
        skill_hash: str = "",
        environment_id: str = "",
        source: str = "skill-runner",
        thread_id: str = "",
        evidence_kind: str = "ordinary",
        execution_fingerprint: str = "",
        source_hash: str = "",
        extra_evidence_refs: Iterable[str] = (),
    ) -> "SkillRunEvent":
        error_text = result.error_text(default="") if not result.success else ""
        refs = ["stderr:" + _sha256(error_text.encode("utf-8"))] if error_text else []
        refs.extend(str(ref) for ref in extra_evidence_refs if str(ref).strip())
        kind = str(result.error_kind or SkillErrorKind.UNKNOWN.value)
        outcome = (
            "succeeded"
            if result.success
            else "cancelled"
            if kind == SkillErrorKind.CANCELLED.value
            else "failed"
        )
        return cls(
            event_id=uuid.uuid4().hex,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            run_id=run_id.strip(),
            skill_id=skill_id.strip() or _unresolved_skill_id(result.skill),
            skill_version=skill_version or "unknown",
            skill_hash=skill_hash or "unknown",
            environment_id=environment_id or result.runtime_source or "unknown",
            outcome=outcome,
            error_kind=kind,
            exit_code=result.exit_code,
            duration_seconds=result.duration_seconds,
            evidence_kind=evidence_kind.strip() or "ordinary",
            execution_fingerprint=execution_fingerprint.strip(),
            source_hash=source_hash.strip(),
            evidence_refs=refs,
            source=source,
            thread_id=thread_id,
        )


@dataclass(frozen=True, slots=True)
class SkillHealthBucket:
    skill_id: str
    skill_version: str
    skill_hash: str
    source_hash: str
    environment_id: str
    total_count: int
    success_count: int
    failures_by_kind: dict[str, int]
    skill_defect_count: int
    environment_failure_count: int
    framework_failure_count: int
    cancelled_count: int


@dataclass(frozen=True, slots=True)
class SkillHealthSummary:
    """Per-skill run-health aggregate across all versions/hashes/environments.

    ``other_count`` collects every failure whose ``error_kind`` is outside the
    skill/environment/framework/cancelled attribution sets (e.g. ``bad_input``,
    ``upstream_failed``, ``unknown``, ``none``) so the breakdown is fully closed:
    ``success + skill_defect + environment + framework + cancelled + other
    == total`` always holds. Without this, a UI summing only the four
    attribution buckets would silently under-100%.
    """

    skill_id: str
    total_count: int
    success_count: int
    skill_defect_count: int
    environment_failure_count: int
    framework_failure_count: int
    cancelled_count: int
    other_count: int

    @property
    def completion_rate(self) -> float:
        return (self.success_count / self.total_count) if self.total_count else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total_count,
            "success": self.success_count,
            "skill_defect": self.skill_defect_count,
            "environment": self.environment_failure_count,
            "framework": self.framework_failure_count,
            "cancelled": self.cancelled_count,
            "other": self.other_count,
            "completion_rate": round(self.completion_rate, 4),
        }


class SkillHealthLedger:
    """Append-only JSONL event ledger with deterministic health aggregation."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, event: SkillRunEvent) -> None:
        payload = json.dumps(event.to_dict(), sort_keys=True, ensure_ascii=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_file_lock(self.path.with_suffix(self.path.suffix + ".lock")):
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(payload + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def _events_unlocked(self) -> list[SkillRunEvent]:
        if not self.path.exists():
            return []
        result: list[SkillRunEvent] = []
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            try:
                result.append(SkillRunEvent.from_dict(json.loads(line)))
            except Exception as exc:
                raise ValueError(
                    f"invalid skill health event at {self.path}:{line_number}: {exc}"
                ) from exc
        return result

    @contextmanager
    def locked_events(self) -> Iterator[list[SkillRunEvent]]:
        """Expose one ledger snapshot while preventing new ledger appends.

        Governance uses this narrow primitive only for its final approval
        revalidation. The caller must consume the returned snapshot before
        leaving the context; it is not a mutable ledger transaction.
        """
        with _exclusive_file_lock(self.path.with_suffix(self.path.suffix + ".lock")):
            yield self._events_unlocked()

    def events(self) -> list[SkillRunEvent]:
        # Ordinary reads need the same serialization, but they deliberately
        # do not use the approval-fence seam. This keeps locked_events()
        # specific to the final check/commit critical section and makes that
        # behavior directly testable without counting incidental reads.
        with _exclusive_file_lock(self.path.with_suffix(self.path.suffix + ".lock")):
            return self._events_unlocked()

    def summarize(self) -> list[SkillHealthBucket]:
        groups: dict[tuple[str, str, str, str, str], list[SkillRunEvent]] = defaultdict(list)
        for event in self.events():
            groups[
                (
                    event.skill_id,
                    event.skill_version,
                    event.skill_hash,
                    event.source_hash,
                    event.environment_id,
                )
            ].append(event)

        buckets: list[SkillHealthBucket] = []
        for key, events in sorted(groups.items()):
            failures = Counter(
                event.error_kind
                for event in events
                if event.outcome != "succeeded"
            )
            buckets.append(
                SkillHealthBucket(
                    skill_id=key[0],
                    skill_version=key[1],
                    skill_hash=key[2],
                    source_hash=key[3],
                    environment_id=key[4],
                    total_count=len(events),
                    success_count=sum(event.outcome == "succeeded" for event in events),
                    failures_by_kind=dict(sorted(failures.items())),
                    skill_defect_count=sum(failures[kind] for kind in _SKILL_DEFECT_KINDS),
                    environment_failure_count=sum(
                        failures[kind] for kind in _ENVIRONMENT_FAILURE_KINDS
                    ),
                    framework_failure_count=sum(
                        failures[kind] for kind in _FRAMEWORK_FAILURE_KINDS
                    ),
                    cancelled_count=failures[SkillErrorKind.CANCELLED.value],
                )
            )
        return buckets


def default_skill_health_ledger() -> SkillHealthLedger:
    configured = os.environ.get("OMICSCLAW_SKILL_HEALTH_LEDGER", "").strip()
    path = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".config" / "omicsclaw" / "audit" / "skill-runs.jsonl"
    )
    return SkillHealthLedger(path)


def aggregate_skill_health(
    ledger: SkillHealthLedger | None = None,
) -> dict[str, SkillHealthSummary]:
    """Aggregate the run-event ledger into one closed per-skill health summary.

    Sums every :class:`SkillHealthBucket` for a skill across versions, hashes,
    and environments, then derives ``other_count`` so the breakdown is fully
    closed (see :class:`SkillHealthSummary`). This is a read-only projection of
    the append-only ledger — safe to expose from unauthenticated catalog reads,
    unlike the governance snapshot which mutates canonical repo state.

    Returns a mapping keyed by ``skill_id``; skills with no recorded runs are
    absent (callers treat "missing" as "no runs yet").
    """
    source = ledger if ledger is not None else default_skill_health_ledger()
    totals: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "total": 0,
            "success": 0,
            "skill_defect": 0,
            "environment": 0,
            "framework": 0,
            "cancelled": 0,
        }
    )
    for bucket in source.summarize():
        agg = totals[bucket.skill_id]
        agg["total"] += bucket.total_count
        agg["success"] += bucket.success_count
        agg["skill_defect"] += bucket.skill_defect_count
        agg["environment"] += bucket.environment_failure_count
        agg["framework"] += bucket.framework_failure_count
        agg["cancelled"] += bucket.cancelled_count

    summaries: dict[str, SkillHealthSummary] = {}
    for skill_id, agg in totals.items():
        attributed = (
            agg["success"]
            + agg["skill_defect"]
            + agg["environment"]
            + agg["framework"]
            + agg["cancelled"]
        )
        summaries[skill_id] = SkillHealthSummary(
            skill_id=skill_id,
            total_count=agg["total"],
            success_count=agg["success"],
            skill_defect_count=agg["skill_defect"],
            environment_failure_count=agg["environment"],
            framework_failure_count=agg["framework"],
            cancelled_count=agg["cancelled"],
            other_count=max(agg["total"] - attributed, 0),
        )
    return summaries


def record_skill_run_result(
    result: "SkillRunResult",
    *,
    skill_info: Mapping[str, Any] | None = None,
    skill_hash: str | None = None,
    source_hash: str | None = None,
    skills_root: str | Path | None = None,
    runtime_entry_path: str | Path | None = None,
    environment_id: str | None = None,
    source: str = "skill-runner",
    thread_id: str = "",
    run_id: str = "",
    evidence_kind: str = "ordinary",
    ledger: SkillHealthLedger | None = None,
) -> SkillRunEvent:
    """Record one privacy-minimal event with stable skill/environment identity.

    Callers that did not freeze both hashes before execution must supply the
    canonical ``skills_root``; fallback capture never guesses by walking above
    the runtime entry.
    """
    info = skill_info or {}
    canonical_skill_id = str(
        info.get("alias") or info.get("canonical_name") or ""
    ).strip()
    if not canonical_skill_id:
        canonical_skill_id = _unresolved_skill_id(result.skill)
    script = info.get("script")
    script_path = Path(script) if script else None
    if skill_hash is None or source_hash is None:
        if script_path is not None and script_path.is_file():
            if skills_root is None:
                raise ValueError(
                    "canonical skills_root is required to infer execution identity"
                )
            inferred_skill_hash, inferred_source_hash = (
                capture_skill_execution_identity(
                    script_path,
                    skills_root=skills_root,
                    directory_name=str(info.get("directory_name") or ""),
                )
            )
        else:
            inferred_skill_hash, inferred_source_hash = ("unknown", "unknown")
        if skill_hash is None:
            skill_hash = inferred_skill_hash
        if source_hash is None:
            source_hash = inferred_source_hash
    evidence_refs: list[str] = []
    execution_fingerprint = ""
    if result.output_dir:
        execution_fingerprint = "output:" + hashlib.sha256(
            str(Path(result.output_dir).expanduser().resolve()).encode("utf-8")
        ).hexdigest()[:24]
    # ``result.json`` belongs to the Skill and may use arbitrary user-derived
    # field names.  Even top-level keys are therefore not privacy-safe audit
    # evidence.  Structural output validation is recorded through the typed
    # outcome/error kind instead of copying result-envelope content.
    canonical_path_value = runtime_entry_path or script_path
    canonical_path = (
        Path(canonical_path_value).expanduser().resolve()
        if canonical_path_value is not None
        else None
    )
    canonical_entry = canonical_path.name if canonical_path is not None else ""
    traceback_text = result.error_text(default="") if not result.success else ""
    for filename, line in re.findall(
        r'File ["\']([^"\']+\.py)["\'], line (\d+)',
        traceback_text,
    ):
        if canonical_path is None:
            continue
        trace_path = Path(filename).expanduser()
        if not trace_path.is_absolute():
            # The shared runner launches Skills with the entrypoint directory
            # as cwd, so relative traceback paths are resolved at that exact
            # boundary.  Only persist the basename after the full path matches.
            trace_path = canonical_path.parent / trace_path
        try:
            matches_entry = trace_path.resolve() == canonical_path
        except OSError:
            matches_entry = False
        if matches_entry:
            ref = f"trace:{canonical_entry}:{line}"
            if ref not in evidence_refs:
                evidence_refs.append(ref)

    event = SkillRunEvent.from_result(
        result,
        skill_id=canonical_skill_id,
        run_id=run_id,
        skill_version=str(info.get("version") or "unknown"),
        skill_hash=skill_hash,
        environment_id=environment_id
        or _unbound_environment_id(runtime_source=result.runtime_source),
        source=source,
        thread_id=thread_id,
        evidence_kind=evidence_kind,
        execution_fingerprint=execution_fingerprint,
        source_hash=source_hash,
        extra_evidence_refs=evidence_refs,
    )
    (ledger or default_skill_health_ledger()).append(event)
    return event


@dataclass(frozen=True, slots=True)
class EvolutionProposal:
    proposal_id: str
    created_at: str
    target_skill: str
    skill_version: str
    skill_hash: str
    kind: str
    status: str
    rationale: str
    support_event_ids: list[str]
    counterexample_event_ids: list[str]
    proposed_change: dict[str, Any]
    target_path_hash: str = ""
    source_hash: str = ""
    target_content_hash: str = ""
    approved_by: str = ""
    approval_reason: str = ""
    before_hash: str = ""
    after_hash: str = ""
    validation_error: str = ""
    reconciled_by: str = ""
    reconciliation_reason: str = ""
    proposed_by: str = ""
    proposal_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvolutionProposal":
        payload: dict[str, Any] = {}
        for name, definition in cls.__dataclass_fields__.items():
            if name in value:
                payload[name] = value[name]
            elif definition.default is not MISSING:
                payload[name] = definition.default
            elif definition.default_factory is not MISSING:
                payload[name] = definition.default_factory()
            else:
                raise KeyError(name)
        return cls(**payload)


def _proposal_id(
    skill: str,
    version: str,
    skill_hash: str,
    kind: str,
    support: list[str],
) -> str:
    basis = json.dumps(
        [skill, version, skill_hash, kind, support],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(basis).hexdigest()[:24]


def generate_evolution_proposals(
    events: Iterable[SkillRunEvent],
    *,
    repeated_threshold: int = 3,
) -> list[EvolutionProposal]:
    """Generate legacy success previews; governed Gotchas use the Backend SSOT.

    This compatibility helper intentionally never synthesizes failure-driven
    Gotchas.  Such candidates require exact source identity, structural
    anchors, counterexamples, a structured human narrative, and the fixed
    governance approval transaction implemented by ``SkillEvolutionGovernance``.
    """
    groups: dict[tuple[str, str, str], list[SkillRunEvent]] = defaultdict(list)
    for event in events:
        groups[(event.skill_id, event.skill_version, event.skill_hash)].append(event)

    proposals: list[EvolutionProposal] = []
    for (skill, version, skill_hash), grouped in sorted(groups.items()):
        successes = sorted(
            (
                event
                for event in grouped
                if event.outcome == "succeeded"
                and event.error_kind == SkillErrorKind.NONE.value
            ),
            key=lambda event: event.event_id,
        )
        if len(successes) >= repeated_threshold:
            support = [event.event_id for event in successes[:repeated_threshold]]
            proposals.append(
                EvolutionProposal(
                    proposal_id=_proposal_id(
                        skill,
                        version,
                        skill_hash,
                        "validation_promotion",
                        support,
                    ),
                    created_at=datetime.now(timezone.utc).isoformat(),
                    target_skill=skill,
                    skill_version=version,
                    skill_hash=skill_hash,
                    kind="validation_promotion",
                    status="pending",
                    rationale=f"{len(successes)} repeated successful executions",
                    support_event_ids=support,
                    counterexample_event_ids=[],
                    proposed_change={
                        "field": "validation.level",
                        "action": "review_next_level",
                    },
                )
            )
    return proposals


class EvolutionApplyError(RuntimeError):
    """Raised after a failed validator has restored the exact previous bytes."""


class EvolutionRecoveryRequiredError(EvolutionApplyError):
    """An existing recovery witness must be reconciled before approval."""


@dataclass(frozen=True, slots=True)
class EvolutionApprovalReceipt:
    proposal_id: str
    status: str
    approved_by: str
    before_hash: str
    after_hash: str


ApplyChange = Callable[[bytes, EvolutionProposal], bytes]
Validator = Callable[[Path], None]
_REQUIRED_VALIDATION_STAGES = frozenset({"representation", "execution", "retrieval"})


class _AtomicWriteConflict(RuntimeError):
    """The target no longer contains the bytes expected by a guarded write."""


class _AtomicWriteDurabilityError(_AtomicWriteConflict):
    """The guarded write could not confirm publication or rollback durability."""


def _rename_exchange(left: Path, right: Path) -> bool:
    """Atomically exchange two paths where the host OS exposes that primitive."""
    libc = ctypes.CDLL(None, use_errno=True)
    if os.name == "posix" and hasattr(libc, "renameat2"):
        renameat2 = libc.renameat2
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            -100,
            os.fsencode(left),
            -100,
            os.fsencode(right),
            2,
        )
        if result == 0:
            return True
        error = ctypes.get_errno()
        if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.EXDEV}:
            return False
        if error == errno.ENOENT:
            raise _AtomicWriteConflict(f"target disappeared before atomic commit: {right}")
        raise OSError(error, os.strerror(error), str(right))
    if os.name == "posix" and hasattr(libc, "renamex_np"):
        renamex_np = libc.renamex_np
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        result = renamex_np(os.fsencode(left), os.fsencode(right), 0x00000002)
        if result == 0:
            return True
        error = ctypes.get_errno()
        if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.EXDEV}:
            return False
        if error == errno.ENOENT:
            raise _AtomicWriteConflict(f"target disappeared before atomic commit: {right}")
        raise OSError(error, os.strerror(error), str(right))
    return False


def _file_identity(path: Path) -> tuple[int, ...]:
    stat = path.stat()
    # Content alone cannot reveal an in-place rewrite to identical bytes.
    # Include the kernel-maintained change time and relevant metadata so a
    # chmod/chown/link or same-byte rewrite in the exchange/rollback window
    # prevents us from restoring stale bytes over that newer observation.
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        stat.st_mode,
        stat.st_uid,
        stat.st_gid,
        stat.st_nlink,
    )


def _fsync_directory(path: Path) -> None:
    """Durably publish a directory entry change on POSIX filesystems."""
    if os.name != "posix":  # pragma: no cover - Windows has no directory fsync
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _guarded_swap_path(path: Path, transaction_id: str = "guarded-write") -> Path:
    """Return a stable same-directory witness path for guarded exchanges."""
    token = hashlib.sha256(transaction_id.encode("utf-8")).hexdigest()[:24]
    return path.with_name(f".{path.name}.omicsclaw-{token}.swap")


def _remove_guarded_swap(path: Path) -> None:
    """Remove a swap witness and durably publish that removal."""
    if path.exists():
        path.unlink()
    # Also issue the barrier when the name is already absent. A previous
    # unlink may have succeeded while its directory fsync failed; recovery
    # must confirm that absence before it can clear the journal.
    _fsync_directory(path.parent)


def _atomic_write(
    path: Path,
    payload: bytes,
    *,
    mode: int,
    expected: bytes | None = None,
    swap_path: str | Path | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if expected is not None:
        witness = (
            Path(swap_path)
            if swap_path is not None
            else _guarded_swap_path(path)
        )
        if witness == path or witness.parent.resolve() != path.parent.resolve():
            raise ValueError("guarded swap witness must share the target directory")
        try:
            with witness.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                if hasattr(os, "fchmod"):
                    os.fchmod(handle.fileno(), mode)
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise _AtomicWriteDurabilityError(
                f"guarded recovery witness already exists: {witness}"
            ) from exc
        except Exception:
            try:
                _remove_guarded_swap(witness)
            except Exception as cleanup_exc:
                raise _AtomicWriteDurabilityError(
                    f"guarded recovery witness cleanup failed: {witness}"
                ) from cleanup_exc
            raise
        try:
            _fsync_directory(path.parent)
        except Exception as exc:
            try:
                _remove_guarded_swap(witness)
            except Exception as cleanup_exc:
                raise _AtomicWriteDurabilityError(
                    f"guarded recovery witness durability is unknown: {witness}"
                ) from cleanup_exc
            raise _AtomicWriteConflict(
                f"guarded recovery witness could not be published: {witness}"
            ) from exc

        # From this point until the witness is durably removed, any abrupt
        # termination deliberately leaves a stable path that recovery can
        # inspect. Never put witness cleanup in a finally block.
        try:
            exchanged = _rename_exchange(witness, path)
        except _AtomicWriteConflict:
            _remove_guarded_swap(witness)
            raise
        except Exception as exc:
            raise _AtomicWriteDurabilityError(
                f"guarded exchange outcome is unknown; witness retained: {witness}"
            ) from exc
        if not exchanged:
            try:
                _remove_guarded_swap(witness)
            except Exception as cleanup_exc:
                raise _AtomicWriteDurabilityError(
                    f"atomic exchange unavailable and witness cleanup failed: {witness}"
                ) from cleanup_exc
            raise _AtomicWriteConflict(
                f"atomic compare-and-swap is unavailable for target: {path}"
            )

        try:
            committed_identity = _file_identity(path)
            previous = witness.read_bytes()
        except Exception as exc:
            raise _AtomicWriteDurabilityError(
                f"guarded exchange could not be inspected; witness retained: {witness}"
            ) from exc
        if previous != expected:
            # Never auto-exchange the predecessor back. A non-cooperating
            # writer can modify the live path after any userspace check but
            # before that second exchange, which would move the newer edit to
            # the witness and make a later cleanup destroy it. Preserve both
            # endpoints and require journal-led reconciliation instead.
            raise _AtomicWriteDurabilityError(
                f"guarded predecessor mismatch; witness retained: {witness}"
            )

        try:
            _fsync_directory(path.parent)
        except Exception as exc:
            raise _AtomicWriteDurabilityError(
                f"target durability could not be confirmed; witness retained: {witness}"
            ) from exc
        if _file_identity(path) != committed_identity or path.read_bytes() != payload:
            raise _AtomicWriteDurabilityError(
                f"target changed after guarded exchange; witness retained: {witness}"
            )
        try:
            _remove_guarded_swap(witness)
        except Exception as exc:
            raise _AtomicWriteDurabilityError(
                f"guarded witness cleanup durability failed: {witness}"
            ) from exc
        return

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class EvolutionProposalStore:
    """Append-only proposal state; writeback exists only behind explicit approval."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @staticmethod
    def _is_submittable_candidate(proposal: EvolutionProposal) -> bool:
        # A merge candidate (ADR 0074 §8.2 stage one) is a non-approvable static
        # overlap advisory: its evidence is the manifest overlap carried in
        # ``proposed_change``, not ledger events, so it is submittable as a draft
        # with no supporting event ids. Every other kind still requires them.
        if proposal.kind == "merge_candidate":
            return proposal.status == "draft"
        if not proposal.support_event_ids:
            return False
        return proposal.status == "pending" or (
            proposal.kind in {"gotcha_evidence", "gotcha_review"}
            and proposal.status == "draft"
        )

    def _append_unlocked(self, proposal: EvolutionProposal) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        payload = json.dumps(proposal.to_dict(), sort_keys=True, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(payload + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if not existed:
            _fsync_directory(self.path.parent)

    def _append(self, proposal: EvolutionProposal) -> None:
        with _exclusive_file_lock(self._lock_path):
            self._append_unlocked(proposal)

    def submit(self, proposal: EvolutionProposal) -> None:
        if not self._is_submittable_candidate(proposal):
            raise ValueError(
                "evolution proposals require a reviewable status and supporting events"
            )
        with _exclusive_file_lock(self._lock_path):
            if self._get_unlocked(proposal.proposal_id, missing_ok=True) is not None:
                raise ValueError(f"proposal already exists: {proposal.proposal_id}")
            self._append_unlocked(proposal)

    def submit_if_absent(self, proposal: EvolutionProposal) -> bool:
        """Submit a deterministic proposal once; return whether it was new."""
        if not self._is_submittable_candidate(proposal):
            raise ValueError(
                "evolution proposals require a reviewable status and supporting events"
            )
        with _exclusive_file_lock(self._lock_path):
            if self._get_unlocked(proposal.proposal_id, missing_ok=True) is not None:
                return False
            self._append_unlocked(proposal)
            return True

    def list_latest(self) -> list[EvolutionProposal]:
        """Return the latest state for every proposal in deterministic order."""
        with _exclusive_file_lock(self._lock_path):
            latest: dict[str, EvolutionProposal] = {}
            if self.path.exists():
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    proposal = EvolutionProposal.from_dict(json.loads(line))
                    latest[proposal.proposal_id] = proposal
            return [latest[key] for key in sorted(latest)]

    def mark_stale(self, proposal_id: str, *, reason: str) -> EvolutionProposal:
        with _exclusive_file_lock(self._lock_path):
            proposal = self._get_unlocked(proposal_id)
            assert proposal is not None
            if proposal.status not in {"draft", "pending", "rolled_back"}:
                raise ValueError(f"proposal cannot become stale: {proposal.status}")
            stale = replace(proposal, status="stale", validation_error=reason.strip())
            self._append_unlocked(stale)
            return stale

    def reject(
        self,
        proposal_id: str,
        *,
        approver: str,
        reason: str,
        decision_guard: Callable[[], None] | None = None,
    ) -> EvolutionProposal:
        if not approver.strip() or not reason.strip():
            raise ValueError("rejection requires an approver and reason")
        with _exclusive_file_lock(self._lock_path):
            proposal = self._get_unlocked(proposal_id)
            assert proposal is not None
            if proposal.status != "pending":
                raise ValueError(f"proposal is not pending: {proposal.status}")
            if decision_guard is not None:
                decision_guard()
            rejected = replace(
                proposal,
                status="rejected",
                approved_by=approver.strip(),
                approval_reason=reason.strip(),
            )
            self._append_unlocked(rejected)
            return rejected

    def get(
        self,
        proposal_id: str,
        *,
        missing_ok: bool = False,
    ) -> EvolutionProposal | None:
        with _exclusive_file_lock(self._lock_path):
            return self._get_unlocked(proposal_id, missing_ok=missing_ok)

    def _get_unlocked(
        self,
        proposal_id: str,
        *,
        missing_ok: bool = False,
    ) -> EvolutionProposal | None:
        latest = None
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                value = json.loads(line)
                if value.get("proposal_id") == proposal_id:
                    latest = EvolutionProposal.from_dict(value)
        if latest is None and not missing_ok:
            raise KeyError(f"unknown evolution proposal: {proposal_id}")
        return latest

    def _approve_and_apply(
        self,
        proposal_id: str,
        *,
        approver: str,
        reason: str = "",
        target_path: str | Path,
        apply_change: ApplyChange,
        validators: Mapping[str, Validator],
        on_rollback: Callable[[], None] | None = None,
        approval_committer: Callable[[EvolutionProposal], None] | None = None,
        approval_guard: Callable[[], None] | None = None,
        before_commit: Callable[[bytes, bytes, EvolutionProposal], None] | None = None,
        on_state_persisted: Callable[[], None] | None = None,
        guarded_swap_path: str | Path | None = None,
    ) -> EvolutionApprovalReceipt:
        validator_names = set(validators)
        missing_validators = _REQUIRED_VALIDATION_STAGES - validator_names
        unexpected_validators = validator_names - _REQUIRED_VALIDATION_STAGES
        if missing_validators or unexpected_validators:
            details = []
            if missing_validators:
                details.append("missing: " + ", ".join(sorted(missing_validators)))
            if unexpected_validators:
                details.append("unexpected: " + ", ".join(sorted(unexpected_validators)))
            raise ValueError(
                "approval requires exactly representation, execution, and retrieval validators; "
                + "; ".join(details)
            )
        if not approver.strip():
            raise ValueError("an explicit human approver is required")
        with _exclusive_file_lock(self._lock_path):
            proposal = self._get_unlocked(proposal_id)
            assert proposal is not None
            if proposal.status != "pending":
                raise ValueError(f"proposal is not pending: {proposal.status}")
            if approval_guard is not None:
                # This check runs under the same proposal lock as the whole
                # approval transaction. A caller that passed an earlier fast
                # precheck cannot race an interrupted predecessor's journal.
                approval_guard()
            target = Path(target_path)
            swap_path = (
                Path(guarded_swap_path)
                if guarded_swap_path is not None
                else _guarded_swap_path(target, proposal.proposal_id)
            )
            before = target.read_bytes()
            before_hash = _sha256(before)
            after = apply_change(before, proposal)
            if not isinstance(after, bytes):
                raise TypeError("apply_change must return bytes")
            mode = target.stat().st_mode
            after_hash = _sha256(after)
            committed = False
            validation_stage = "representation"
            try:
                # Validate representation and execution on staged bytes.  A
                # malformed manifest or failed demo therefore never becomes
                # visible in the live skill tree.
                with tempfile.TemporaryDirectory(prefix="omicsclaw-evolution-") as tmp:
                    staged = Path(tmp) / target.name
                    staged.write_bytes(after)
                    validators["representation"](staged)
                    validation_stage = "execution"
                    validators["execution"](staged)

                # Execution validation may be long-running.  Recheck the live
                # bytes before commit so a concurrent edit is never clobbered.
                if target.read_bytes() != before:
                    stale = replace(
                        proposal,
                        status="stale",
                        approved_by=approver.strip(),
                        approval_reason=reason.strip(),
                        before_hash=before_hash,
                        validation_error="target changed during approval revalidation",
                    )
                    self._append_unlocked(stale)
                    if on_state_persisted is not None:
                        on_state_persisted()
                    raise EvolutionApplyError(
                        "proposal became stale during approval revalidation"
                    )

                if before_commit is not None:
                    before_commit(before, after, proposal)
                try:
                    _atomic_write(
                        target,
                        after,
                        mode=mode,
                        expected=before,
                        swap_path=swap_path,
                    )
                except _AtomicWriteDurabilityError:
                    # The recovery journal is the only durable witness when
                    # neither publication nor rollback durability is known.
                    # Keep the proposal pending and preserve that journal for
                    # explicit reconciliation after storage is healthy.
                    raise
                except _AtomicWriteConflict as exc:
                    stale = replace(
                        proposal,
                        status="stale",
                        approved_by=approver.strip(),
                        approval_reason=reason.strip(),
                        before_hash=before_hash,
                        validation_error="target changed during atomic approval commit",
                    )
                    self._append_unlocked(stale)
                    if on_state_persisted is not None:
                        on_state_persisted()
                    raise EvolutionApplyError(
                        "proposal became stale during atomic approval commit"
                    ) from exc
                committed = True
                # Retrieval observes the committed tree and owns registry and
                # generated-projection refresh.  Failure still rolls back the
                # exact manifest bytes below.
                validation_stage = "retrieval"
                validators["retrieval"](target)
                validation_stage = "proposal_state"
                approved = replace(
                    proposal,
                    status="approved",
                    approved_by=approver.strip(),
                    approval_reason=reason.strip(),
                    before_hash=before_hash,
                    after_hash=after_hash,
                )
                if approval_committer is None:
                    self._append_unlocked(approved)
                else:
                    approval_committer(approved)
                if on_state_persisted is not None:
                    on_state_persisted()
            except (EvolutionRecoveryRequiredError, _AtomicWriteDurabilityError):
                raise
            except Exception as exc:
                latest = self._get_unlocked(proposal_id)
                if latest is not None and latest.status == "stale":
                    raise
                rollback_error: Exception | None = None
                if committed:
                    try:
                        _atomic_write(
                            target,
                            before,
                            mode=mode,
                            expected=after,
                            swap_path=swap_path,
                        )
                    except _AtomicWriteDurabilityError:
                        # Keep the pending proposal and journal when rollback
                        # durability is unknown. Reconciliation can inspect the
                        # exact before/after endpoints after storage recovers.
                        raise
                    except Exception as rollback_exc:
                        rollback_error = rollback_exc
                if on_rollback is not None:
                    try:
                        # Generated projections and the live manifest are one
                        # approval transaction. Roll them back before releasing
                        # the proposal lock so another approval cannot race a
                        # stale projection restore.
                        on_rollback()
                    except Exception as rollback_exc:  # pragma: no cover - disk failure
                        if rollback_error is None:
                            rollback_error = rollback_exc
                rolled_back = replace(
                    proposal,
                    status="rolled_back",
                    approved_by=approver.strip(),
                    approval_reason=reason.strip(),
                    before_hash=before_hash,
                    after_hash=after_hash,
                    validation_error=(
                        f"{validation_stage}_validation_failed:"
                        f"{type(exc).__name__}"
                    ),
                )
                self._append_unlocked(rolled_back)
                if rollback_error is None and on_state_persisted is not None:
                    on_state_persisted()
                if rollback_error is not None:
                    raise EvolutionApplyError(
                        "proposal validation failed; projection rollback also failed: "
                        f"{type(rollback_error).__name__}"
                    ) from exc
                raise EvolutionApplyError(
                    f"proposal validation failed and was rolled back: {exc}"
                ) from exc
        return EvolutionApprovalReceipt(
            proposal_id=proposal_id,
            status="approved",
            approved_by=approver.strip(),
            before_hash=before_hash,
            after_hash=after_hash,
        )


def default_evolution_proposal_store() -> EvolutionProposalStore:
    configured = os.environ.get("OMICSCLAW_EVOLUTION_PROPOSALS", "").strip()
    path = (
        Path(configured).expanduser()
        if configured
        else Path.home()
        / ".config"
        / "omicsclaw"
        / "audit"
        / "evolution-proposals.jsonl"
    )
    return EvolutionProposalStore(path)


__all__ = [
    "EvolutionApplyError",
    "EvolutionApprovalReceipt",
    "EvolutionProposal",
    "EvolutionProposalStore",
    "EvolutionRecoveryRequiredError",
    "SkillHealthBucket",
    "SkillHealthLedger",
    "SkillRunEvent",
    "capture_skill_execution_identity",
    "compute_environment_id",
    "compute_execution_source_hash",
    "default_evolution_proposal_store",
    "default_skill_health_ledger",
    "generate_evolution_proposals",
    "record_skill_run_result",
]
