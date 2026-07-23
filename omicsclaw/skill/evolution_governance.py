"""Human-gated, evidence-bound governance for skill evolution.

The public Interface intentionally exposes only refresh, snapshot, approve,
and reject.  Product callers never provide a target path, patch function, or
validator callbacks; those details remain inside this Module.

EVO-G1 implements earned ``smoke-only -> demo-validated`` promotion. EVO-06
adds the reverse transition only for a reproduced explicit-demo Skill defect,
plus evidence-bound deprecation to one exact, routable replacement. Every
writeback passes the same fixed representation, execution, and retrieval
validators before the proposal becomes approved.
"""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Mapping, Protocol
import unicodedata
from uuid import uuid4

import yaml

from .evolution import (
    EvolutionApplyError,
    EvolutionApprovalReceipt,
    EvolutionProposal,
    EvolutionProposalStore,
    EvolutionRecoveryRequiredError,
    SkillHealthLedger,
    SkillRunEvent,
    _AtomicWriteConflict,
    _atomic_write,
    _capture_planned_skill_execution_identity,
    _exclusive_file_lock,
    _fsync_directory,
    _guarded_swap_path,
    _remove_guarded_swap,
    capture_skill_execution_identity,
    compute_execution_source_hash,
    default_evolution_proposal_store,
    default_skill_health_ledger,
)
from .capability_resolver import _tokenize
from .registry import GOVERNED_REPLACEMENT_VALIDATION_LEVELS, OmicsRegistry
from .schema import SkillManifest, load_skill_yaml, parse_skill_manifest
from .evaluation_protocol import protocol_digest
from .evaluation_run import (
    EvaluationResultStore,
    default_evaluation_result_store,
    run_protocol_evaluations,
)
from .skill_audit import (
    VALIDATION_LADDER,
    CachedRevisionResolver,
    SkillAuditRuntime,
    SkillExperienceView,
    SkillIdentityInput,
)
from .skill_md import append_gotcha_entry, render_skill_md

logger = logging.getLogger(__name__)


def _run_protocol_entry(skill_dir: Path, entry: str) -> str:
    """Run a test-backed protocol's entry in a bounded pytest subprocess.

    Returns ``"succeeded"`` on exit 0, else ``"failed"``. Control credentials are
    scrubbed from the child environment, stdout/stderr are discarded, and a
    timeout is a failure. This is the phased shared-runner-adjacent executor
    (ADR 0074 §10 "Deferred"): the RunRuntime governed queue — resource
    scheduling, cancellation and full AuditOperation observability — is a
    follow-up.
    """
    import subprocess

    from .execution.environment import scrub_internal_control_credentials
    from .execution.python_runtime import get_skill_runner_python

    entry_path = skill_dir / entry
    if not entry_path.is_file():
        return "failed"
    try:
        proc = subprocess.run(
            [get_skill_runner_python(), "-m", "pytest", "-q", str(entry_path)],
            cwd=str(skill_dir),
            env=scrub_internal_control_credentials(os.environ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=600,
        )
    except (subprocess.TimeoutExpired, OSError):
        return "failed"
    return "succeeded" if proc.returncode == 0 else "failed"


# Split a requirement string ("scanpy>=1.10", "leidenalg[extra]") to its bare
# distribution name — the same normalization the environment probe uses.
_DEP_NAME_SPLIT = re.compile(r"[<>=!~;\s\[]")


@lru_cache(maxsize=4096)
def _installed_dependency_version(package: str) -> str:
    """Installed version of one distribution, or ``"missing"`` (memoized).

    Resolved in-process, so it reflects the interpreter that runs Skills by
    default (``get_skill_runner_python()`` is ``sys.executable`` unless
    ``OMICSCLAW_RUN_PYTHON`` overrides it). Installed versions do not change
    within a process, so memoizing keeps the per-resolve digest computation
    cheap; a fresh Backend process re-resolves and picks up an upgrade.
    """
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "missing"
    except Exception:  # a broken distribution must not break audit reads
        return "missing"


def _manifest_dependency_versions(manifest: SkillManifest) -> dict[str, str]:
    """Resolved versions of a Skill's declared Python dependencies (ADR 0074 §6.4).

    Binds the protocol digest to the runtime env's key dependency versions: when
    a declared dependency's installed version changes, the digest changes and
    evidence produced under the old version stops applying to the current one.
    A Skill that declares no ``deps.python`` binds nothing (empty map). CLI / R
    tool versions are a follow-up; the declared Python deps are the tractable,
    author-controlled signal today.
    """
    packages: set[str] = set()
    for requirement in manifest.deps.python:
        name = _DEP_NAME_SPLIT.split(requirement.strip(), maxsplit=1)[0].strip().casefold()
        if name:
            packages.add(name)
    return {name: _installed_dependency_version(name) for name in sorted(packages)}


def _manifest_protocol_digests(manifest: SkillManifest, skill_dir: Path) -> dict[str, str]:
    """Current digest of each declared Evaluation Protocol (ADR 0074 §6.1).

    Recomputed on every resolve (cheap: a few small test entries + memoized
    dependency-version lookups) so the digest always reflects the current
    protocol bytes and the current key dependency versions; a stored evaluation
    result earns a level only while its protocol_digest still matches one of
    these.
    """
    dependency_versions = _manifest_dependency_versions(manifest)
    digests: dict[str, str] = {}
    for proto in manifest.validation.protocols:
        entry_path = skill_dir / proto.entry
        try:
            entry_bytes = entry_path.read_bytes()
        except OSError:
            entry_bytes = b""
        digests[proto.id] = protocol_digest(
            protocol={
                "id": proto.id,
                "kind": proto.kind,
                "entry": proto.entry,
                "dataset_ref": proto.dataset_ref,
                "repeats": proto.repeats,
                "metrics": proto.metrics,
            },
            entry_bytes=entry_bytes,
            dependency_versions=dependency_versions,
        )
    return digests

# ADR 0074 additive Desktop snapshot contract (see docs/design §9.2). These
# fields are added to GET /skill-evolution without removing the existing
# ``proposals``/``health`` shape, so an old App keeps working.
_AUDIT_SNAPSHOT_SCHEMA_VERSION = 1
_AUDIT_CAPABILITIES: tuple[str, ...] = (
    "experience_view",
    "effective_validation",
    "audit_summary",
)

# Bounded per-Skill Experience View pagination (ADR 0074 §9.3).
_DEFAULT_EXPERIENCE_PAGE = 50
_MAX_EXPERIENCE_PAGE = 100


def _encode_experience_cursor(skill_id: str) -> str:
    """Opaque, fixed-shape resume token for a Skill-id-ordered page."""
    return base64.urlsafe_b64encode(skill_id.encode("utf-8")).decode("ascii")


def _decode_experience_cursor(cursor: str) -> str:
    try:
        return base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise ValueError(f"invalid cursor: {cursor!r}") from exc


def _identity_mtime_signature(*paths: Path) -> str:
    """Opaque token that changes when any of ``paths`` is modified.

    Caches a Skill's expensive ``(manifest_hash, source_hash)`` identity keyed
    by its own manifest + entry mtimes. A shared-library change that leaves those
    untouched is picked up only on the next explicit resolver invalidate (the
    refresh path) — a documented ADR-0074 first-slice bound, acceptable because
    a Backend restart also mints a fresh authority epoch and cold cache.
    """
    parts: list[str] = []
    for path in paths:
        try:
            parts.append(str(os.stat(path).st_mtime_ns))
        except OSError:
            parts.append("-")
    return ":".join(parts)


def _build_registry_revision_resolver(skills_root: Path) -> CachedRevisionResolver:
    """A read-only CachedRevisionResolver over the on-disk canonical Skill tree.

    It parses each ``skill.yaml`` for the cheap identity inputs and computes the
    ADR-0069 ``(manifest_hash, source_hash)`` closure only on a cache miss. A
    manifest that fails to parse is skipped; one whose identity cannot be
    computed is surfaced with an ``unknown`` hash rather than breaking the whole
    snapshot.
    """
    entry_by_dir: dict[str, Path] = {}

    def enumerate_skills() -> Iterable[SkillIdentityInput]:
        entry_by_dir.clear()
        for manifest_path in sorted(skills_root.rglob("skill.yaml")):
            try:
                manifest = parse_skill_manifest(load_skill_yaml(manifest_path))
            except Exception:
                continue
            skill_dir = manifest_path.parent
            entry_path = skill_dir / manifest.runtime.entry
            entry_by_dir[str(skill_dir)] = entry_path
            yield SkillIdentityInput(
                skill_id=manifest.id,
                version=manifest.version,
                declared_validation_level=manifest.validation.level,
                cache_key=str(skill_dir),
                mtime_signature=_identity_mtime_signature(manifest_path, entry_path),
                protocol_digests=_manifest_protocol_digests(manifest, skill_dir),
            )

    def compute_identity(cache_key: str) -> tuple[str, str]:
        entry_path = entry_by_dir.get(cache_key)
        if entry_path is None:
            return "unknown", "unknown"
        try:
            return capture_skill_execution_identity(
                entry_path, skills_root=skills_root, skill_dir=Path(cache_key)
            )
        except Exception:
            logger.warning(
                "Skill identity computation failed for %s", cache_key, exc_info=True
            )
            return "unknown", "unknown"

    return CachedRevisionResolver(enumerate_skills, compute_identity)


_SKILL_DEFECT_KINDS = frozenset({"script_defect", "contract_failure"})

# ADR 0074 §8.2 stage-one merge pre-filter. A bounded, deterministic
# description-similarity gate (Jaccard over two Skills' capability fingerprints)
# calibrated well above the real catalog's maximum observed same-domain overlap
# (~0.29 Jaccard) so it fires only on genuine near-duplicates a maintainer
# should review, never on the current healthy catalog. It is advisory only: a
# ``merge_candidate`` never merges, never concatenates code/methodology, and
# being a non-approvable draft cannot mutate anything — retiring a Skill still
# requires the ADR-0068 replacement-backed deprecation (stage two).
_MERGE_SIMILARITY_THRESHOLD = 0.5
_MERGE_MIN_SHARED_TOKENS = 6
_MERGE_MAX_SHARED_TOKENS_SHOWN = 12

# ADR 0074 §8.1 protocol-revision coverage gap. A declared level is earned from
# evidence only through a matching Evaluation Protocol kind; ``demo-validated``
# is earned from demo execution evidence (no protocol needed) and ``smoke-only``
# needs nothing, so those never gap. ``production`` is human-approved but still
# needs benchmarked-level protocol substance beneath it.
_PROTOCOL_KIND_EARNS = {
    "demo": "demo-validated",
    "fixture": "fixture-validated",
    "benchmark": "benchmarked",
}
_LEVEL_NEEDS_PROTOCOL = {
    "fixture-validated": "fixture-validated",
    "benchmarked": "benchmarked",
    "production": "benchmarked",
}
_SUPPORTED_FROM_LEVEL = "smoke-only"
_SUPPORTED_TO_LEVEL = "demo-validated"
_DEMOTION_FROM_LEVEL = "demo-validated"
_DEMOTION_TO_LEVEL = "smoke-only"
_ROUTABLE_LIFECYCLES = frozenset({"mvp", "stable"})
_UNSAFE_GOTCHA_TEXT_CHARS = frozenset("\r\n`*<>[]!#\\{}|~")
_UNSAFE_GOTCHA_TEXT_PATTERNS = (
    re.compile(r"(?i)[A-Z][A-Z0-9+.-]*://\S+"),
    re.compile(
        r"(?i)(?<![A-Za-z0-9_.~-])/{1,2}[A-Za-z0-9_.~-]+"
        r"(?:/[A-Za-z0-9_.~-]+)*"
    ),
    re.compile(r"(?i)[A-Z]:[\\/]\S+"),
    re.compile(r"(?<![A-Za-z0-9])_(?=\S)|(?<=\S)_(?![A-Za-z0-9])"),
)
_CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    r"(?P<key>[^:=\r\n]{1,128})[ \t]*[:=][ \t]*\S+"
)
_CREDENTIAL_KEY_FAMILIES = frozenset(
    {
        "accesskey",
        "accesskeyid",
        "accesstoken",
        "apikey",
        "authorization",
        "credential",
        "credentials",
        "password",
        "passwd",
        "privatekey",
        "refreshtoken",
        "secret",
        "secretaccesskey",
        "secretkey",
        "token",
    }
)


def _has_credential_assignment(value: str) -> bool:
    # Scan a compatibility-normalized shadow, but preserve the original
    # narrative bytes for scientific text. This closes fullwidth/confusable
    # separators without rewriting accepted content.
    security_view = unicodedata.normalize("NFKC", value)
    for match in _CREDENTIAL_ASSIGNMENT_PATTERN.finditer(security_view):
        normalized_key = re.sub(
            r"[^A-Za-z0-9]+", "", match.group("key")
        ).casefold()
        if any(
            normalized_key == family or normalized_key.endswith(family)
            for family in _CREDENTIAL_KEY_FAMILIES
        ):
            return True
    return False


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _skill_capability_tokens(manifest: SkillManifest) -> frozenset[str]:
    """Deterministic capability fingerprint for the merge pre-filter (ADR 0074 §8.2).

    Reuses the router's tokenizer so a merge advisory reflects the same
    description overlap the resolver would see (name + load-when + trigger
    keywords + tags), keeping the signal consistent with routing.
    """
    parts = [manifest.name, manifest.summary.load_when]
    parts.extend(manifest.summary.trigger_keywords)
    parts.extend(manifest.summary.tags)
    return frozenset(_tokenize(" ".join(parts)))


def _level_rank(level: str) -> int:
    """Rank a validation level; an unknown level ranks at the floor."""
    try:
        return VALIDATION_LADDER.index(level)
    except ValueError:
        return 0


@dataclass(frozen=True, slots=True)
class _CurrentGotchaSnapshot:
    skill_version: str
    manifest_hash: str
    source_hash: str
    target_content_hash: str
    target_path_hash: str
    current_state: str


class EvolutionRevalidationError(RuntimeError):
    """Approval could not prove that the proposed transition remains safe."""


class EvolutionExecutionAdapter(Protocol):
    """Seam for the mandatory real-demo execution validator."""

    def validate_demo(self, skill_id: str) -> None: ...

    def validate_demo_defect(self, skill_id: str) -> None: ...


class EvolutionProjectionAdapter(Protocol):
    """Seam for registry and generated catalog/DAG refresh."""

    def refresh(self, skills_root: Path, skill_id: str) -> None: ...

    def rebuild(self, skills_root: Path) -> None: ...


class EvolutionRecoveryJournal:
    """Durable single-flight intent for one in-progress approval."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def prepare(
        self,
        *,
        proposal: EvolutionProposal,
        target_relative_path: str,
        swap_relative_path: str = "",
        before: bytes,
        after: bytes,
        mode: int,
        approver: str,
        reason: str,
    ) -> None:
        if not swap_relative_path:
            swap_relative_path = _guarded_swap_path(
                Path(target_relative_path),
                proposal.proposal_id,
            ).as_posix()
        payload = {
            "schema_version": 2,
            "proposal_id": proposal.proposal_id,
            "target_skill": proposal.target_skill,
            "target_relative_path": target_relative_path,
            "swap_relative_path": swap_relative_path,
            "before": base64.b64encode(before).decode("ascii"),
            "after": base64.b64encode(after).decode("ascii"),
            "before_hash": _sha256(before),
            "after_hash": _sha256(after),
            "mode": mode,
            "approver": approver,
            "reason": reason,
        }
        with _exclusive_file_lock(self._lock_path):
            if self.path.exists():
                raise EvolutionRecoveryRequiredError(
                    "an interrupted approval requires reconciliation"
                )
            _atomic_write(
                self.path,
                (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"),
                mode=0o600,
            )

    def read(self) -> dict[str, Any] | None:
        with _exclusive_file_lock(self._lock_path):
            if not self.path.exists():
                return None
            value = json.loads(self.path.read_text(encoding="utf-8"))
        common = {
            "schema_version",
            "proposal_id",
            "target_skill",
            "target_relative_path",
            "before",
            "after",
            "before_hash",
            "after_hash",
            "mode",
            "approver",
            "reason",
        }
        if not isinstance(value, dict):
            raise EvolutionRevalidationError("invalid evolution recovery journal")
        schema_version = value.get("schema_version")
        required = common | (
            {"swap_relative_path"} if schema_version == 2 else set()
        )
        if (
            not isinstance(schema_version, int)
            or schema_version not in {1, 2}
            or set(value) != required
            or not isinstance(value["mode"], int)
        ):
            raise EvolutionRevalidationError("invalid evolution recovery journal")
        try:
            before = base64.b64decode(value["before"], validate=True)
            after = base64.b64decode(value["after"], validate=True)
        except Exception as exc:
            raise EvolutionRevalidationError("invalid evolution recovery journal") from exc
        if (
            _sha256(before) != value["before_hash"]
            or _sha256(after) != value["after_hash"]
        ):
            raise EvolutionRevalidationError("invalid evolution recovery journal hashes")
        return {
            **value,
            "swap_relative_path": str(value.get("swap_relative_path") or ""),
            "before_bytes": before,
            "after_bytes": after,
        }

    def clear(self, proposal_id: str) -> None:
        with _exclusive_file_lock(self._lock_path):
            if not self.path.exists():
                return
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if value.get("proposal_id") != proposal_id:
                raise EvolutionRevalidationError(
                    "recovery journal belongs to another proposal"
                )
            self.path.unlink()
            _fsync_directory(self.path.parent)


class SharedRunnerEvolutionExecutionAdapter:
    """Run the approved skill through the unified execution gate."""

    @staticmethod
    def _run_demo(skill_id: str):
        from .runner import run_skill

        with tempfile.TemporaryDirectory(prefix="omicsclaw-evolution-demo-") as tmp:
            return run_skill(
                skill_id,
                demo=True,
                output_dir=str(Path(tmp) / "output"),
            )

    def validate_demo(self, skill_id: str) -> None:
        result = self._run_demo(skill_id)
        if not result.success:
            detail = result.error_kind or "unknown"
            raise EvolutionRevalidationError(
                f"demo revalidation failed for {skill_id}: {detail}"
            )

    def validate_demo_defect(self, skill_id: str) -> None:
        result = self._run_demo(skill_id)
        if result.success:
            raise EvolutionRevalidationError(
                f"demo defect did not reproduce for {skill_id}"
            )
        if result.error_kind not in _SKILL_DEFECT_KINDS:
            raise EvolutionRevalidationError(
                f"demo revalidation for {skill_id} failed outside the Skill: "
                f"{result.error_kind or 'unknown'}"
            )


def _atomic_projection_write(path: Path, payload: bytes) -> None:
    mode = path.stat().st_mode if path.exists() else 0o644
    _atomic_write(path, payload, mode=mode)


class RegistryProjectionAdapter:
    """Refresh the runtime registry and the two committed skill projections."""

    def refresh(self, skills_root: Path, skill_id: str) -> None:
        self.rebuild(skills_root)

        probe = OmicsRegistry()
        probe.load_all(skills_root)
        info = probe.skills.get(skill_id)
        if info is None:
            raise EvolutionRevalidationError(
                f"retrieval revalidation did not observe {skill_id}"
            )

    def rebuild(self, skills_root: Path) -> None:
        from scripts.generate_catalog import generate_catalog
        from scripts.generate_skill_dag import generate_skill_dag

        catalog = generate_catalog(skills_root)
        graph = generate_skill_dag(skills_root)
        _atomic_projection_write(
            skills_root / "catalog.json",
            (json.dumps(catalog, indent=2) + "\n").encode("utf-8"),
        )
        _atomic_projection_write(
            skills_root / "skill_dag.json",
            (json.dumps(graph, indent=2) + "\n").encode("utf-8"),
        )

        # Long-running product Surfaces use the singleton registry.  Reload it
        # only when this governance instance owns that same skill tree.
        from .registry import SKILLS_DIR, registry

        if skills_root.resolve() == SKILLS_DIR.resolve():
            registry.reload(skills_root)


class SkillEvolutionGovernance:
    """Deep Module that owns evolution policy, validation, and writeback."""

    def __init__(
        self,
        *,
        skills_root: str | Path,
        ledger: SkillHealthLedger,
        proposals: EvolutionProposalStore,
        execution_adapter: EvolutionExecutionAdapter | None = None,
        projection_adapter: EvolutionProjectionAdapter | None = None,
        recovery_journal: EvolutionRecoveryJournal | None = None,
        minimum_demo_executions: int = 1,
        minimum_demo_failures: int = 1,
        minimum_deprecation_defects: int = 3,
        minimum_gotcha_defects: int = 3,
        minimum_gotcha_counterexamples: int = 1,
        audit_runtime: SkillAuditRuntime | None = None,
        evaluation_store: EvaluationResultStore | None = None,
    ) -> None:
        if minimum_demo_executions < 1:
            raise ValueError("minimum_demo_executions must be at least 1")
        if minimum_demo_failures < 1:
            raise ValueError("minimum_demo_failures must be at least 1")
        if minimum_deprecation_defects < 1:
            raise ValueError("minimum_deprecation_defects must be at least 1")
        if minimum_gotcha_defects < 1:
            raise ValueError("minimum_gotcha_defects must be at least 1")
        if minimum_gotcha_counterexamples < 1:
            raise ValueError("minimum_gotcha_counterexamples must be at least 1")
        self.skills_root = Path(skills_root).resolve()
        self.ledger = ledger
        self.proposals = proposals
        self.execution_adapter = (
            execution_adapter or SharedRunnerEvolutionExecutionAdapter()
        )
        self.projection_adapter = projection_adapter or RegistryProjectionAdapter()
        self.recovery_journal = recovery_journal or EvolutionRecoveryJournal(
            proposals.path.with_suffix(proposals.path.suffix + ".inflight.json")
        )
        self.minimum_demo_executions = minimum_demo_executions
        self.minimum_demo_failures = minimum_demo_failures
        self.minimum_deprecation_defects = minimum_deprecation_defects
        self.minimum_gotcha_defects = minimum_gotcha_defects
        self.minimum_gotcha_counterexamples = minimum_gotcha_counterexamples
        # ADR 0074 additive audit read models. The revision resolver caches the
        # expensive per-Skill identity and is invalidated on the explicit refresh
        # path, so snapshot() stays cheap — it reads the last-refreshed summary
        # and never recomputes source hashes on a GET. A fresh process mints a
        # new authority epoch; snapshot_revision is monotonic within that epoch.
        self._authority_epoch = uuid4().hex
        self._snapshot_revision = 0
        self._evaluation_store = evaluation_store or default_evaluation_result_store()
        if audit_runtime is not None:
            self._revision_resolver: CachedRevisionResolver | None = None
            self._audit_runtime = audit_runtime
        else:
            self._revision_resolver = _build_registry_revision_resolver(self.skills_root)
            self._audit_runtime = SkillAuditRuntime(
                self.ledger,
                self._revision_resolver,
                protocol_results=self._evaluation_store.results_for,
            )
        self._audit_views: tuple[SkillExperienceView, ...] = ()
        self._audit_summary: dict[str, Any] = self._audit_runtime.summary([])

    def _recompute_audit_readmodels(self) -> None:
        """Refresh the cached audit read models on the explicit refresh path.

        Recomputes the (cached) revision identities, the per-Skill Experience
        Views and their summary, bumping ``snapshot_revision`` only when the
        read models actually change. Defensive: an audit-read failure keeps the
        prior read models rather than breaking proposal synthesis — ADR 0074
        treats an audit failure as a framework incident, not a governance break.
        """
        try:
            if self._revision_resolver is not None:
                self._revision_resolver.invalidate()
            views = tuple(self._audit_runtime.experience_views())
            summary = self._audit_runtime.summary(views)
        except Exception:
            logger.warning(
                "Audit read-model refresh failed; keeping prior read models",
                exc_info=True,
            )
            return
        if views != self._audit_views or summary != self._audit_summary:
            self._snapshot_revision += 1
        self._audit_views = views
        self._audit_summary = summary

    def experience_view(self, skill_id: str) -> dict[str, Any] | None:
        """The last-refreshed Skill Experience View for one Skill id, or None."""
        for view in self._audit_views:
            if view.skill_revision.skill_id == skill_id:
                return view.to_dict()
        return None

    def experience_page(
        self,
        cursor: str = "",
        limit: int = _DEFAULT_EXPERIENCE_PAGE,
        state: str = "",
    ) -> dict[str, Any]:
        """A bounded, opaque-cursor page of Skill Experience Views (ADR 0074 §9.3).

        Views come from the last refresh (a cheap read; no per-request source
        hashing). ``state`` optionally filters by validation state; the cursor
        resumes after a Skill id in the id-sorted list; ``limit`` is clamped to a
        fixed maximum. A malformed cursor raises ``ValueError`` (the route maps
        it to 422). Never returns raw audit payloads — only the projected views.
        """
        views = self._audit_views
        if state:
            views = tuple(v for v in views if v.validation_state == state)
        start = 0
        if cursor:
            after = _decode_experience_cursor(cursor)
            start = next(
                (
                    index
                    for index, view in enumerate(views)
                    if view.skill_revision.skill_id > after
                ),
                len(views),
            )
        bounded = max(1, min(int(limit or _DEFAULT_EXPERIENCE_PAGE), _MAX_EXPERIENCE_PAGE))
        page = views[start : start + bounded]
        has_more = (start + bounded) < len(views)
        next_cursor = (
            _encode_experience_cursor(page[-1].skill_revision.skill_id)
            if has_more and page
            else None
        )
        return {
            "skills": [view.to_dict() for view in page],
            "next_cursor": next_cursor,
        }

    def evaluate(self, skill_id: str, *, run_one=None) -> list:
        """Run a Skill's declared Evaluation Protocols and store the results.

        ADR 0074 M-C (phased shared-runner path): each declared protocol runs via
        ``run_one`` (injectable for tests; the default runs ``demo`` protocols
        through the shared runner and a test-backed protocol's entry in a bounded
        subprocess), the digest-bound results are appended to the evaluation
        store, and the audit read models are refreshed so effective validation
        reflects them. Returns the produced ``ProtocolEvaluationResult`` list.

        Read-only for Skill files: this never mutates ``skill.yaml`` / ``SKILL.md``
        — a validation-level change still requires a Backend proposal + human
        approval (``SkillEvolutionGovernance`` remains the sole mutation authority).
        """
        if self._revision_resolver is None:
            raise RuntimeError("evaluate() requires the registry-backed resolver")
        self._revision_resolver.invalidate()
        current = next(
            (cr for cr in self._revision_resolver() if cr.revision.skill_id == skill_id),
            None,
        )
        if current is None:
            raise KeyError(f"unknown skill: {skill_id}")
        manifest_path, manifest, _hash = self._find_manifest(skill_id)
        skill_dir = manifest_path.parent
        protocols = [
            (
                {
                    "id": proto.id,
                    "kind": proto.kind,
                    "entry": proto.entry,
                    "dataset_ref": proto.dataset_ref,
                    "repeats": proto.repeats,
                    "metrics": proto.metrics,
                },
                current.protocol_digests.get(proto.id, ""),
            )
            for proto in manifest.validation.protocols
        ]
        runner = run_one or self._make_default_protocol_runner(skill_id, skill_dir)
        results = run_protocol_evaluations(current.revision, protocols, runner)
        for result in results:
            self._evaluation_store.append(current.revision, result)
        self._recompute_audit_readmodels()
        return results

    def _make_default_protocol_runner(self, skill_id: str, skill_dir: Path):
        def run_one(spec: Mapping[str, Any]) -> str:
            if str(spec.get("kind")) == "demo":
                from .runner import run_skill

                with tempfile.TemporaryDirectory(prefix="omicsclaw-eval-") as tmp:
                    result = run_skill(
                        skill_id, demo=True, output_dir=str(Path(tmp) / "output")
                    )
                return "succeeded" if getattr(result, "success", False) else "failed"
            return _run_protocol_entry(skill_dir, str(spec.get("entry", "")))

        return run_one

    def refresh(self) -> list[EvolutionProposal]:
        """Synthesize earned promotion and explicit-demo demotion candidates."""
        latest_proposals = self.proposals.list_latest()
        manifest_snapshots = self._manifest_snapshots()
        current_gotcha_snapshots = self._current_gotcha_snapshots(
            manifest_snapshots
        )
        self._stale_obsolete_gotcha_candidates(
            latest_proposals,
            current_gotcha_snapshots,
        )
        latest_proposals = self.proposals.list_latest()
        materialized_gotcha_clusters = {
            identity
            for proposal in latest_proposals
            if proposal.kind == "gotcha"
            and proposal.status in {"pending", "approved"}
            if (identity := self._gotcha_cluster_identity(proposal)) is not None
        }
        grouped: dict[tuple[str, str, str], list[SkillRunEvent]] = {}
        for event in self.ledger.events():
            grouped.setdefault(
                (event.skill_id, event.skill_version, event.skill_hash), []
            ).append(event)

        created: list[EvolutionProposal] = []
        for review in self._gotcha_review_candidates(
            latest_proposals,
            current_gotcha_snapshots,
        ):
            if self.proposals.submit_if_absent(review):
                created.append(review)
        for path, manifest, manifest_hash in manifest_snapshots:
            if manifest.lifecycle.status not in _ROUTABLE_LIFECYCLES:
                continue
            events = grouped.get((manifest.id, manifest.version, manifest_hash), [])
            if manifest.type != "consensus":
                for candidate in self._gotcha_evidence_candidates(
                    path,
                    manifest,
                    manifest_hash,
                    events,
                ):
                    if (
                        self._gotcha_cluster_identity(candidate)
                        in materialized_gotcha_clusters
                    ):
                        continue
                    if self.proposals.submit_if_absent(candidate):
                        created.append(candidate)
            if manifest.validation.level == _SUPPORTED_FROM_LEVEL:
                if any(event.error_kind in _SKILL_DEFECT_KINDS for event in events):
                    continue

                evidence = self._distinct_demo_successes(events)
                if len(evidence) < self.minimum_demo_executions:
                    continue
                support = [
                    event.event_id
                    for event in evidence[: self.minimum_demo_executions]
                ]
                proposal = EvolutionProposal(
                    proposal_id=self._promotion_proposal_id(
                        manifest.id,
                        manifest.version,
                        manifest_hash,
                    ),
                    created_at=datetime.now(timezone.utc).isoformat(),
                    target_skill=manifest.id,
                    skill_version=manifest.version,
                    skill_hash=manifest_hash,
                    kind="validation_promotion",
                    status="pending",
                    rationale=(
                        f"{len(support)} distinct explicit demo execution(s) passed "
                        "for the exact skill version and manifest hash"
                    ),
                    support_event_ids=support,
                    counterexample_event_ids=[],
                    proposed_change={
                        "field": "validation.level",
                        "from": _SUPPORTED_FROM_LEVEL,
                        "to": _SUPPORTED_TO_LEVEL,
                        "evidence_event_ids": support,
                    },
                    target_path_hash=_sha256(
                        path.relative_to(self.skills_root).as_posix().encode("utf-8")
                    ),
                )
            elif manifest.validation.level == _DEMOTION_FROM_LEVEL:
                evidence = self._distinct_demo_defects(events)
                if len(evidence) < self.minimum_demo_failures:
                    continue
                support = [
                    event.event_id
                    for event in evidence[: self.minimum_demo_failures]
                ]
                counterexamples = [
                    event.event_id
                    for event in self._distinct_demo_successes(events)
                ]
                proposal = EvolutionProposal(
                    proposal_id=self._demotion_proposal_id(
                        manifest.id,
                        manifest.version,
                        manifest_hash,
                    ),
                    created_at=datetime.now(timezone.utc).isoformat(),
                    target_skill=manifest.id,
                    skill_version=manifest.version,
                    skill_hash=manifest_hash,
                    kind="validation_demotion",
                    status="pending",
                    rationale=(
                        f"{len(support)} distinct explicit demo skill defect(s) "
                        "were observed for the exact skill version and manifest hash"
                    ),
                    support_event_ids=support,
                    counterexample_event_ids=counterexamples,
                    proposed_change={
                        "field": "validation.level",
                        "from": _DEMOTION_FROM_LEVEL,
                        "to": _DEMOTION_TO_LEVEL,
                        "evidence_event_ids": support,
                    },
                    target_path_hash=_sha256(
                        path.relative_to(self.skills_root).as_posix().encode("utf-8")
                    ),
                )
            else:
                continue
            if self.proposals.submit_if_absent(proposal):
                created.append(proposal)
        for merge in self._merge_candidate_candidates(manifest_snapshots):
            if self.proposals.submit_if_absent(merge):
                created.append(merge)
        for revision in self._protocol_revision_candidates(manifest_snapshots):
            if self.proposals.submit_if_absent(revision):
                created.append(revision)
        self._recompute_audit_readmodels()
        return created

    def _merge_candidate_candidates(
        self,
        manifest_snapshots: list[tuple[Path, SkillManifest, str]],
    ) -> list[EvolutionProposal]:
        """Stage-one merge advisories from bounded description overlap (ADR 0074 §8.2).

        A deterministic capability-fingerprint Jaccard over each same-domain,
        routable, non-consensus Skill pair. A pair above the calibrated threshold
        yields ONE non-approvable ``merge_candidate`` draft that only describes the
        overlap and points at the two-stage replacement + deprecation resolution.
        It never merges or concatenates code/methodology; being a draft it can
        never be approved, so the sole retirement path stays the ADR-0068
        replacement-backed deprecation. Same-domain only: the eight domains are
        disjoint capability spaces, so cross-domain text overlap is not a merge
        signal.
        """
        from itertools import combinations

        by_domain: dict[str, list[tuple[str, str, str, frozenset[str], Path]]] = {}
        for path, manifest, manifest_hash in manifest_snapshots:
            if (
                manifest.lifecycle.status not in _ROUTABLE_LIFECYCLES
                or manifest.type == "consensus"
            ):
                continue
            tokens = _skill_capability_tokens(manifest)
            if len(tokens) < _MERGE_MIN_SHARED_TOKENS:
                continue
            by_domain.setdefault(manifest.domain, []).append(
                (manifest.id, manifest.version, manifest_hash, tokens, path)
            )

        candidates: list[EvolutionProposal] = []
        for domain, skills in by_domain.items():
            for left, right in combinations(
                sorted(skills, key=lambda item: item[0]), 2
            ):
                shared = left[3] & right[3]
                if len(shared) < _MERGE_MIN_SHARED_TOKENS:
                    continue
                jaccard = len(shared) / len(left[3] | right[3])
                if jaccard < _MERGE_SIMILARITY_THRESHOLD:
                    continue
                target_id, target_version, target_hash, _tokens, target_path = left
                other_id, other_version, other_hash = right[0], right[1], right[2]
                candidates.append(
                    EvolutionProposal(
                        proposal_id=self._merge_candidate_proposal_id(
                            target_id, other_id
                        ),
                        created_at=datetime.now(timezone.utc).isoformat(),
                        target_skill=target_id,
                        skill_version=target_version,
                        skill_hash=target_hash,
                        kind="merge_candidate",
                        status="draft",
                        rationale=(
                            f"{target_id} and {other_id} share {len(shared)} "
                            f"capability tokens (jaccard={jaccard:.2f}) in domain "
                            f"{domain}; a maintainer should review whether one "
                            "should replace the other"
                        ),
                        support_event_ids=[],
                        counterexample_event_ids=[],
                        proposed_change={
                            "field": "lifecycle.merge_review",
                            "action": "review_capability_overlap",
                            "advisory_only": True,
                            "domain": domain,
                            "overlap_skills": [target_id, other_id],
                            "overlap_versions": [target_version, other_version],
                            "overlap_manifest_hashes": [target_hash, other_hash],
                            "shared_capability_tokens": sorted(shared)[
                                :_MERGE_MAX_SHARED_TOKENS_SHOWN
                            ],
                            "shared_token_count": len(shared),
                            "similarity": round(jaccard, 3),
                            "resolution_path": (
                                "acquire or select one replacement Skill, revalidate "
                                "it to demo-validated or higher and pass routing "
                                "regression, then file a replacement-backed "
                                "deprecation (ADR 0068) for the other; never "
                                "concatenate two Skills' code or methodology"
                            ),
                        },
                        target_path_hash=_sha256(
                            target_path.relative_to(self.skills_root)
                            .as_posix()
                            .encode("utf-8")
                        ),
                    )
                )
        return candidates

    def _protocol_revision_candidates(
        self,
        manifest_snapshots: list[tuple[Path, SkillManifest, str]],
    ) -> list[EvolutionProposal]:
        """Non-approvable protocol-revision advisories (ADR 0074 §8.1 / §11.3).

        Two deterministic, manifest-derived signals per routable, non-consensus
        Skill:

        - ``coverage_gap``: the declared validation level can only be earned
          through a matching Evaluation Protocol kind (fixture/benchmark), but
          the Skill declares none that earns it — so effective validation can
          never reach the declared level from evidence. One advisory per Skill.
        - ``protocol_invalid``: a declared protocol whose ``entry`` cannot be
          loaded (§11.3). One advisory per Skill, listing the broken protocols.

        Each is a non-approvable ``draft`` that only describes the gap and its
        remediation (declare/fix a protocol); it never rewrites ``skill.yaml``.
        The real fix flows through a maintainer/AutoAgent remediation brief and
        the existing governed writeback, not this advisory.
        """
        candidates: list[EvolutionProposal] = []
        for path, manifest, manifest_hash in manifest_snapshots:
            if (
                manifest.lifecycle.status not in _ROUTABLE_LIFECYCLES
                or manifest.type == "consensus"
            ):
                continue
            skill_dir = path.parent
            path_hash = _sha256(
                path.relative_to(self.skills_root).as_posix().encode("utf-8")
            )
            protocols = manifest.validation.protocols

            needed = _LEVEL_NEEDS_PROTOCOL.get(manifest.validation.level)
            if needed is not None:
                best = max(
                    (
                        _level_rank(_PROTOCOL_KIND_EARNS.get(proto.kind, "smoke-only"))
                        for proto in protocols
                    ),
                    default=0,
                )
                if best < _level_rank(needed):
                    candidates.append(
                        self._protocol_revision_proposal(
                            manifest,
                            manifest_hash,
                            path_hash,
                            problem_kind="coverage_gap",
                            proposed_change={
                                "field": "validation.protocols",
                                "action": "declare_evaluation_protocol",
                                "advisory_only": True,
                                "problem_kind": "coverage_gap",
                                "declared_level": manifest.validation.level,
                                "required_protocol_kinds": sorted(
                                    kind
                                    for kind, earns in _PROTOCOL_KIND_EARNS.items()
                                    if _level_rank(earns) >= _level_rank(needed)
                                ),
                                "resolution_path": (
                                    "declare (and pass) an Evaluation Protocol whose "
                                    f"kind earns {needed!r} so the declared level "
                                    f"{manifest.validation.level!r} can be earned from "
                                    "evidence; the effective level stays capped until "
                                    "then"
                                ),
                            },
                        )
                    )

            invalid = [
                {"id": proto.id, "kind": proto.kind, "entry": proto.entry}
                for proto in protocols
                if not (skill_dir / proto.entry).is_file()
            ]
            if invalid:
                candidates.append(
                    self._protocol_revision_proposal(
                        manifest,
                        manifest_hash,
                        path_hash,
                        problem_kind="protocol_invalid",
                        proposed_change={
                            "field": "validation.protocols",
                            "action": "repair_evaluation_protocol",
                            "advisory_only": True,
                            "problem_kind": "protocol_invalid",
                            "invalid_protocols": invalid,
                            "resolution_path": (
                                "each declared protocol's entry must be a loadable "
                                "file; repair or remove the broken entries — a "
                                "protocol whose entry cannot load earns nothing and "
                                "is classified protocol_invalid, never a demotion"
                            ),
                        },
                    )
                )
        return candidates

    def _protocol_revision_proposal(
        self,
        manifest: SkillManifest,
        manifest_hash: str,
        path_hash: str,
        *,
        problem_kind: str,
        proposed_change: dict[str, Any],
    ) -> EvolutionProposal:
        return EvolutionProposal(
            proposal_id=self._protocol_revision_proposal_id(manifest.id, problem_kind),
            created_at=datetime.now(timezone.utc).isoformat(),
            target_skill=manifest.id,
            skill_version=manifest.version,
            skill_hash=manifest_hash,
            kind="protocol_revision",
            status="draft",
            rationale=(
                f"{manifest.id} has a protocol {problem_kind.replace('_', ' ')}; a "
                "maintainer should declare or repair an Evaluation Protocol"
            ),
            support_event_ids=[],
            counterexample_event_ids=[],
            proposed_change=proposed_change,
            target_path_hash=path_hash,
        )

    def _gotcha_evidence_candidates(
        self,
        manifest_path: Path,
        manifest: SkillManifest,
        manifest_hash: str,
        events: list[SkillRunEvent],
    ) -> list[EvolutionProposal]:
        """Cluster privacy-safe ordinary defects into non-approvable drafts."""
        skill_md_path = manifest_path.with_name("SKILL.md")
        source_path = (manifest_path.parent / manifest.runtime.entry).resolve()
        try:
            source_path.relative_to(manifest_path.parent.resolve())
        except ValueError:
            return []
        if not skill_md_path.is_file() or not source_path.is_file():
            return []
        source_hash = compute_execution_source_hash(
            source_path,
            skills_root=self.skills_root,
            skill_dir=manifest_path.parent,
        )
        clusters: dict[tuple[str, str, str], list[SkillRunEvent]] = {}
        for event in events:
            if (
                event.source_hash != source_hash
                or event.evidence_kind != "ordinary"
                or event.outcome != "failed"
                or event.error_kind not in _SKILL_DEFECT_KINDS
            ):
                continue
            for anchor in self._safe_gotcha_event_anchors(event, source_path):
                clusters.setdefault(
                    (event.error_kind, event.environment_id, anchor),
                    [],
                ).append(event)

        candidates: list[EvolutionProposal] = []
        seen_support: set[tuple[str, ...]] = set()
        ordered_clusters = sorted(
            clusters.items(),
            key=lambda item: (
                item[0][0],
                item[0][1],
                0 if ".py:" in item[0][2] else 1,
                item[0][2],
            ),
        )
        target_content_hash = _sha256(skill_md_path.read_bytes())
        target_path_hash = _sha256(
            skill_md_path.relative_to(self.skills_root)
            .as_posix()
            .encode("utf-8")
        )
        for (error_kind, environment_id, anchor), clustered in ordered_clusters:
            defects = self._distinct_skill_defects(clustered)
            if len(defects) < self.minimum_gotcha_defects:
                continue
            support = tuple(
                event.event_id
                for event in defects[: self.minimum_gotcha_defects]
            )
            if support in seen_support:
                continue
            counterexamples = self._distinct_ordinary_successes(
                event
                for event in events
                if event.source_hash == source_hash
                and event.environment_id == environment_id
            )
            if len(counterexamples) < self.minimum_gotcha_counterexamples:
                continue
            if self._execution_identities(defects).intersection(
                self._execution_identities(counterexamples)
            ):
                continue
            seen_support.add(support)
            proposal = EvolutionProposal(
                proposal_id=self._gotcha_evidence_proposal_id(
                    manifest.id,
                    manifest.version,
                    manifest_hash,
                    source_hash,
                    target_path_hash,
                    target_content_hash,
                    error_kind,
                    environment_id,
                    anchor,
                ),
                created_at=datetime.now(timezone.utc).isoformat(),
                target_skill=manifest.id,
                skill_version=manifest.version,
                skill_hash=manifest_hash,
                kind="gotcha_evidence",
                status="draft",
                rationale=(
                    f"{len(support)} distinct ordinary Skill defects share "
                    "one exact-source structural signature; human narrative required"
                ),
                support_event_ids=list(support),
                counterexample_event_ids=[
                    event.event_id for event in counterexamples[:20]
                ],
                proposed_change={
                    "field": "SKILL.md.Gotchas",
                    "action": "request_structured_entry",
                    "evidence_error_kind": error_kind,
                    "evidence_environment_id": environment_id,
                    "evidence_anchor": anchor,
                    "evidence_event_ids": list(support),
                },
                target_path_hash=target_path_hash,
                source_hash=source_hash,
                target_content_hash=target_content_hash,
                proposed_by="system",
                proposal_reason="automatic privacy-safe evidence cluster",
            )
            candidates.append(proposal)
        return candidates

    def snapshot(self) -> dict[str, Any]:
        """Return privacy-minimal proposal and aggregate health state.

        ADR 0074 adds the ``schema_version`` / ``authority_epoch`` /
        ``snapshot_revision`` / ``generated_at`` / ``capabilities`` / ``summary``
        fields additively: the existing ``proposals`` and ``health`` keys and
        their shapes are unchanged, so an old App keeps working while a new App
        can negotiate the extra capabilities. ``summary`` is the last-refreshed
        audit read model (cheap: no per-GET source-hash recomputation).
        """
        return {
            "proposals": [
                proposal.to_dict() for proposal in self.proposals.list_latest()
            ],
            "health": [asdict(bucket) for bucket in self.ledger.summarize()],
            "schema_version": _AUDIT_SNAPSHOT_SCHEMA_VERSION,
            "authority_epoch": self._authority_epoch,
            "snapshot_revision": self._snapshot_revision,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "capabilities": list(_AUDIT_CAPABILITIES),
            "summary": dict(self._audit_summary),
        }

    def _current_gotcha_snapshots(
        self,
        manifests: Iterable[tuple[Path, SkillManifest, str]],
    ) -> dict[str, _CurrentGotchaSnapshot]:
        current: dict[str, _CurrentGotchaSnapshot] = {}
        for manifest_path, manifest, manifest_hash in manifests:
            states: list[str] = []
            if manifest.lifecycle.status not in _ROUTABLE_LIFECYCLES:
                states.append(
                    f"non_routable_lifecycle:{manifest.lifecycle.status}"
                )
            if manifest.type == "consensus":
                states.append("unsupported_consensus")
            skill_md_path = manifest_path.with_name("SKILL.md")
            target_path_hash = _sha256(
                skill_md_path.relative_to(self.skills_root)
                .as_posix()
                .encode("utf-8")
            )
            if skill_md_path.is_file():
                target_content_hash = _sha256(skill_md_path.read_bytes())
            else:
                target_content_hash = ""
                states.append("missing_target")
            source_path = (manifest_path.parent / manifest.runtime.entry).resolve()
            try:
                source_path.relative_to(manifest_path.parent.resolve())
            except ValueError:
                source_hash = ""
                states.append("source_outside_skill")
            else:
                if source_path.is_file():
                    source_hash = compute_execution_source_hash(
                        source_path,
                        skills_root=self.skills_root,
                        skill_dir=manifest_path.parent,
                    )
                else:
                    source_hash = ""
                    states.append("missing_source")
            current[manifest.id] = _CurrentGotchaSnapshot(
                skill_version=manifest.version,
                manifest_hash=manifest_hash,
                source_hash=source_hash,
                target_content_hash=target_content_hash,
                target_path_hash=target_path_hash,
                current_state="|".join(states) if states else "current",
            )
        return current

    @staticmethod
    def _missing_gotcha_snapshot() -> _CurrentGotchaSnapshot:
        return _CurrentGotchaSnapshot(
            skill_version="",
            manifest_hash="",
            source_hash="",
            target_content_hash="",
            target_path_hash="",
            current_state="missing_manifest",
        )

    def _stale_obsolete_gotcha_candidates(
        self,
        proposals: Iterable[EvolutionProposal],
        current: Mapping[str, _CurrentGotchaSnapshot],
    ) -> None:
        for proposal in proposals:
            reason = ""
            snapshot = current.get(proposal.target_skill)
            if proposal.kind == "gotcha_evidence" and proposal.status == "draft":
                if (
                    snapshot is None
                    or snapshot.current_state != "current"
                    or proposal.skill_version != snapshot.skill_version
                    or proposal.skill_hash != snapshot.manifest_hash
                    or proposal.source_hash != snapshot.source_hash
                    or proposal.target_content_hash != snapshot.target_content_hash
                    or proposal.target_path_hash != snapshot.target_path_hash
                ):
                    reason = (
                        "Gotcha evidence source, manifest, or target is no longer current"
                    )
            elif proposal.kind == "gotcha" and proposal.status == "pending":
                if (
                    snapshot is None
                    or snapshot.current_state != "current"
                    or proposal.skill_version != snapshot.skill_version
                    or proposal.skill_hash != snapshot.manifest_hash
                    or proposal.source_hash != snapshot.source_hash
                    or proposal.target_content_hash != snapshot.target_content_hash
                    or proposal.target_path_hash != snapshot.target_path_hash
                ):
                    reason = "pending Gotcha source, manifest, or target is no longer current"
            elif proposal.kind == "gotcha_review" and proposal.status == "draft":
                snapshot = snapshot or self._missing_gotcha_snapshot()
                approved_manifest_hash = str(
                    proposal.proposed_change.get("approved_manifest_hash") or ""
                )
                approved_source_hash = str(
                    proposal.proposed_change.get("approved_source_hash") or ""
                )
                approved_target_path_hash = str(
                    proposal.proposed_change.get("approved_target_path_hash") or ""
                )
                approved_target_content_hash = str(
                    proposal.proposed_change.get("approved_target_content_hash")
                    or ""
                )
                source_proposal_id = str(
                    proposal.proposed_change.get("source_proposal_id") or ""
                )
                if (
                    proposal.skill_version != snapshot.skill_version
                    or proposal.skill_hash != snapshot.manifest_hash
                    or proposal.source_hash != snapshot.source_hash
                    or proposal.target_path_hash != snapshot.target_path_hash
                    or proposal.target_content_hash != snapshot.target_content_hash
                    or proposal.proposed_change.get("current_state")
                    != snapshot.current_state
                    or proposal.proposal_id
                    != self._gotcha_review_proposal_id(
                        source_proposal_id,
                        snapshot.current_state,
                        snapshot.manifest_hash,
                        snapshot.source_hash,
                        snapshot.target_path_hash,
                        snapshot.target_content_hash,
                    )
                    or (
                        snapshot.current_state == "current"
                        and approved_manifest_hash == snapshot.manifest_hash
                        and approved_source_hash == snapshot.source_hash
                        and approved_target_path_hash == snapshot.target_path_hash
                        and approved_target_content_hash
                        == snapshot.target_content_hash
                    )
                ):
                    reason = "Gotcha review no longer describes the current provenance"
            if reason:
                try:
                    self.proposals.mark_stale(
                        proposal.proposal_id,
                        reason=reason,
                    )
                except ValueError:
                    # Another serialized human decision won the race after the
                    # refresh snapshot.  Its newer terminal state is authoritative.
                    continue

    def _gotcha_review_candidates(
        self,
        proposals: Iterable[EvolutionProposal],
        current: Mapping[str, _CurrentGotchaSnapshot],
    ) -> list[EvolutionProposal]:
        candidates: list[EvolutionProposal] = []
        for approved in proposals:
            if approved.kind != "gotcha" or approved.status != "approved":
                continue
            snapshot = current.get(
                approved.target_skill,
                self._missing_gotcha_snapshot(),
            )
            if (
                snapshot.current_state == "current"
                and approved.skill_hash == snapshot.manifest_hash
                and approved.source_hash == snapshot.source_hash
                and approved.target_path_hash == snapshot.target_path_hash
                and approved.after_hash == snapshot.target_content_hash
            ):
                continue
            proposed_change = {
                "field": "SKILL.md.Gotchas",
                "action": "review_after_source_drift",
                "source_proposal_id": approved.proposal_id,
                "approved_manifest_hash": approved.skill_hash,
                "approved_source_hash": approved.source_hash,
                "approved_target_path_hash": approved.target_path_hash,
                "approved_target_content_hash": approved.after_hash,
                "current_state": snapshot.current_state,
                "current_manifest_hash": snapshot.manifest_hash,
                "current_source_hash": snapshot.source_hash,
                "current_target_path_hash": snapshot.target_path_hash,
                "current_target_content_hash": snapshot.target_content_hash,
                "rendered_bullet": str(
                    approved.proposed_change.get("rendered_bullet") or ""
                ),
            }
            candidates.append(
                EvolutionProposal(
                    proposal_id=self._gotcha_review_proposal_id(
                        approved.proposal_id,
                        snapshot.current_state,
                        snapshot.manifest_hash,
                        snapshot.source_hash,
                        snapshot.target_path_hash,
                        snapshot.target_content_hash,
                    ),
                    created_at=datetime.now(timezone.utc).isoformat(),
                    target_skill=approved.target_skill,
                    skill_version=snapshot.skill_version,
                    skill_hash=snapshot.manifest_hash,
                    kind="gotcha_review",
                    status="draft",
                    rationale=(
                        "an approved Gotcha is bound to an older execution source; "
                        "human review is required before treating it as current"
                    ),
                    support_event_ids=list(approved.support_event_ids),
                    counterexample_event_ids=list(
                        approved.counterexample_event_ids
                    ),
                    proposed_change=proposed_change,
                    target_path_hash=snapshot.target_path_hash,
                    source_hash=snapshot.source_hash,
                    target_content_hash=snapshot.target_content_hash,
                    proposed_by="system",
                    proposal_reason="approved Gotcha provenance drift detected",
                )
            )
        return candidates

    def propose_gotcha(
        self,
        *,
        target_skill: str,
        proposer: str,
        reason: str,
        support_event_ids: Iterable[str],
        entry: Mapping[str, Any],
    ) -> EvolutionProposal:
        """Persist one structured, evidence-bound Gotcha without mutating files."""
        target_skill = target_skill.strip()
        proposer = proposer.strip()
        reason = reason.strip()
        if not target_skill or not proposer or not reason:
            raise ValueError("Gotcha proposal requires target, proposer, and reason")
        if len(target_skill) > 128 or len(proposer) > 128 or len(reason) > 1000:
            raise ValueError("Gotcha proposal field exceeds audit limits")
        if self.recovery_journal.read() is not None:
            raise EvolutionRevalidationError(
                "an interrupted approval requires reconciliation"
            )

        manifest_path, manifest, manifest_hash = self._find_manifest(target_skill)
        if manifest.lifecycle.status not in _ROUTABLE_LIFECYCLES:
            raise EvolutionRevalidationError(
                "only a currently routable skill can receive a Gotcha"
            )
        if manifest.type == "consensus":
            raise EvolutionRevalidationError(
                "consensus Skill does not support governed demo revalidation"
            )
        skill_md_path = manifest_path.with_name("SKILL.md")
        if not skill_md_path.is_file():
            raise EvolutionRevalidationError("canonical SKILL.md is missing")
        source_path = (manifest_path.parent / manifest.runtime.entry).resolve()
        try:
            source_path.relative_to(manifest_path.parent.resolve())
        except ValueError as exc:
            raise EvolutionRevalidationError(
                "runtime entry escapes the canonical Skill directory"
            ) from exc
        if not source_path.is_file():
            raise EvolutionRevalidationError("canonical runtime entry is missing")

        source_hash = compute_execution_source_hash(
            source_path,
            skills_root=self.skills_root,
            skill_dir=manifest_path.parent,
        )
        target_content_hash = _sha256(skill_md_path.read_bytes())
        target_path_hash = _sha256(
            skill_md_path.relative_to(self.skills_root)
            .as_posix()
            .encode("utf-8")
        )
        normalized_entry = self._normalize_gotcha_entry(entry)
        self._validate_gotcha_anchors_against_source(
            source_path,
            normalized_entry["anchors"],
        )

        event_ids = list(dict.fromkeys(str(value).strip() for value in support_event_ids))
        event_ids = [value for value in event_ids if value]
        if len(event_ids) > 100 or any(len(value) > 128 for value in event_ids):
            raise ValueError("supporting event ids exceed audit field limits")
        events = self.ledger.events()
        by_id = {event.event_id: event for event in events}
        selected: list[SkillRunEvent] = []
        for event_id in event_ids:
            event = by_id.get(event_id)
            if event is None:
                raise EvolutionRevalidationError(
                    f"supporting event is missing: {event_id}"
                )
            if (
                event.skill_id != manifest.id
                or event.skill_version != manifest.version
                or event.skill_hash != manifest_hash
                or event.source_hash != source_hash
                or event.evidence_kind != "ordinary"
                or event.outcome != "failed"
                or event.error_kind not in _SKILL_DEFECT_KINDS
            ):
                raise EvolutionRevalidationError(
                    f"supporting event is not an exact ordinary Skill defect: {event_id}"
                )
            selected.append(event)
        defects = self._distinct_skill_defects(selected)
        if len(defects) < self.minimum_gotcha_defects:
            raise EvolutionRevalidationError(
                "Gotcha proposal lacks distinct exact-source Skill defects"
            )
        selected_kinds = {event.error_kind for event in defects}
        selected_environments = {event.environment_id for event in defects}
        if len(selected_kinds) != 1 or len(selected_environments) != 1:
            raise EvolutionRevalidationError(
                "Gotcha support must share one error kind and environment"
            )
        anchors = list(normalized_entry["anchors"])
        if any(
            not all(self._event_supports_gotcha_anchor(event, anchor) for event in defects)
            for anchor in anchors
        ):
            raise EvolutionRevalidationError(
                "every Gotcha anchor must resolve in every supporting event"
            )
        support = [
            event.event_id for event in defects[: self.minimum_gotcha_defects]
        ]
        environment_id = next(iter(selected_environments))
        counterexamples = self._distinct_ordinary_successes(
            event
            for event in events
            if event.skill_id == manifest.id
            and event.skill_version == manifest.version
            and event.skill_hash == manifest_hash
            and event.source_hash == source_hash
            and event.environment_id == environment_id
        )
        if len(counterexamples) < self.minimum_gotcha_counterexamples:
            raise EvolutionRevalidationError(
                "Gotcha proposal requires an exact-source success counterexample"
            )
        if self._execution_identities(defects).intersection(
            self._execution_identities(counterexamples)
        ):
            raise EvolutionRevalidationError(
                "Gotcha evidence has a conflicting execution identity"
            )
        counterexample_ids = [event.event_id for event in counterexamples[:20]]
        rendered_bullet = self._render_gotcha_bullet(normalized_entry)
        entry_digest = _sha256(
            json.dumps(
                normalized_entry,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        proposed_change = {
            "field": "SKILL.md.Gotchas",
            "action": "append_canonical_entry",
            "entry": normalized_entry,
            "rendered_bullet": rendered_bullet,
            "entry_digest": entry_digest,
            "evidence_error_kind": next(iter(selected_kinds)),
            "evidence_environment_id": environment_id,
            "evidence_event_ids": support,
            "source_candidate_id": self._gotcha_evidence_proposal_id(
                manifest.id,
                manifest.version,
                manifest_hash,
                source_hash,
                target_path_hash,
                target_content_hash,
                next(iter(selected_kinds)),
                environment_id,
                anchors[0],
            ),
        }
        proposal_id = self._gotcha_proposal_id(
            str(proposed_change["source_candidate_id"]),
            entry_digest,
        )
        proposal = EvolutionProposal(
            proposal_id=proposal_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            target_skill=manifest.id,
            skill_version=manifest.version,
            skill_hash=manifest_hash,
            kind="gotcha",
            status="pending",
            rationale=(
                f"{len(support)} distinct exact-source ordinary Skill defects "
                "share one structural signature and have a success counterexample"
            ),
            support_event_ids=support,
            counterexample_event_ids=counterexample_ids,
            proposed_change=proposed_change,
            target_path_hash=target_path_hash,
            source_hash=source_hash,
            target_content_hash=target_content_hash,
            proposed_by=proposer,
            proposal_reason=reason,
        )
        with _exclusive_file_lock(self.proposals._lock_path):
            candidate_id = str(proposed_change["source_candidate_id"])
            candidate = self.proposals._get_unlocked(
                candidate_id,
                missing_ok=True,
            )
            if candidate is not None:
                if (
                    candidate.kind != "gotcha_evidence"
                    or candidate.status != "draft"
                    or candidate.target_skill != proposal.target_skill
                    or candidate.skill_version != proposal.skill_version
                    or candidate.skill_hash != proposal.skill_hash
                    or candidate.source_hash != proposal.source_hash
                    or candidate.target_content_hash
                    != proposal.target_content_hash
                    or candidate.target_path_hash != proposal.target_path_hash
                    or candidate.support_event_ids != proposal.support_event_ids
                    or not set(candidate.counterexample_event_ids).issubset(
                        proposal.counterexample_event_ids
                    )
                ):
                    raise EvolutionRevalidationError(
                        "automatic Gotcha evidence candidate no longer matches proposal"
                    )

            latest: dict[str, EvolutionProposal] = {}
            if self.proposals.path.exists():
                for line in self.proposals.path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    stored = EvolutionProposal.from_dict(json.loads(line))
                    latest[stored.proposal_id] = stored
            if candidate is None and any(
                stored.kind == "gotcha_evidence"
                and stored.target_skill == proposal.target_skill
                and stored.skill_version == proposal.skill_version
                and stored.skill_hash == proposal.skill_hash
                and stored.source_hash == proposal.source_hash
                and stored.support_event_ids == proposal.support_event_ids
                and stored.proposed_change.get("evidence_error_kind")
                == proposal.proposed_change["evidence_error_kind"]
                and stored.proposed_change.get("evidence_environment_id")
                == proposal.proposed_change["evidence_environment_id"]
                and stored.proposed_change.get("evidence_anchor")
                == proposal.proposed_change["entry"]["anchors"][0]
                for stored in latest.values()
            ):
                raise EvolutionRevalidationError(
                    "automatic Gotcha evidence candidate no longer matches proposal"
                )
            related = [
                stored
                for stored in latest.values()
                if stored.kind == "gotcha"
                and stored.proposed_change.get("source_candidate_id") == candidate_id
            ]
            current = latest.get(proposal.proposal_id)
            if current is not None:
                if (
                    current.proposed_change == proposal.proposed_change
                    and current.proposed_by == proposal.proposed_by
                    and current.proposal_reason == proposal.proposal_reason
                ):
                    return current
                raise EvolutionRevalidationError(
                    "Gotcha proposal identifier collides with different content"
                )
            if any(stored.status == "pending" for stored in related):
                raise EvolutionRevalidationError(
                    "another Gotcha narrative revision is already pending"
                )
            self.proposals._append_unlocked(proposal)
            return proposal

    def propose_deprecation(
        self,
        *,
        target_skill: str,
        replacement_skill: str,
        proposer: str,
        reason: str,
        support_event_ids: Iterable[str],
    ) -> EvolutionProposal:
        """Persist one evidence-bound lifecycle candidate without mutating Skills."""
        target_skill = target_skill.strip()
        replacement_skill = replacement_skill.strip()
        proposer = proposer.strip()
        reason = reason.strip()
        if not target_skill or not replacement_skill or not proposer or not reason:
            raise ValueError(
                "deprecation proposal requires target, replacement, proposer, and reason"
            )
        if (
            len(target_skill) > 128
            or len(replacement_skill) > 128
            or len(proposer) > 128
            or len(reason) > 1000
        ):
            raise ValueError("deprecation proposal field exceeds audit limits")
        if target_skill == replacement_skill:
            raise EvolutionRevalidationError(
                "a deprecated skill cannot supersede itself"
            )
        if self.recovery_journal.read() is not None:
            raise EvolutionRevalidationError(
                "an interrupted approval requires reconciliation"
            )

        target_path, target_manifest, target_hash = self._find_manifest(target_skill)
        _replacement_path, replacement_manifest, replacement_hash = self._find_manifest(
            replacement_skill
        )
        target_revision = self._capture_execution_revision(
            target_path,
            target_manifest,
        )
        if target_revision[0] != target_hash:
            raise EvolutionRevalidationError(
                "target Skill manifest changed during deprecation proposal"
            )
        target_source_hash = target_revision[1]
        if target_manifest.lifecycle.status not in _ROUTABLE_LIFECYCLES:
            raise EvolutionRevalidationError(
                "only a currently routable skill can be deprecated"
            )
        if replacement_manifest.lifecycle.status not in _ROUTABLE_LIFECYCLES:
            raise EvolutionRevalidationError(
                "replacement skill must be currently routable"
            )
        if (
            replacement_manifest.validation.level
            not in GOVERNED_REPLACEMENT_VALIDATION_LEVELS
        ):
            raise EvolutionRevalidationError(
                "replacement skill must already be demo-validated or higher"
            )

        event_ids = list(dict.fromkeys(str(value).strip() for value in support_event_ids))
        event_ids = [value for value in event_ids if value]
        if len(event_ids) > 100 or any(len(value) > 128 for value in event_ids):
            raise ValueError("supporting event ids exceed audit field limits")
        events = self.ledger.events()
        by_id = {event.event_id: event for event in events}
        selected: list[SkillRunEvent] = []
        for event_id in event_ids:
            event = by_id.get(event_id)
            if event is None:
                raise EvolutionRevalidationError(
                    f"supporting event is missing: {event_id}"
                )
            if (
                event.skill_id != target_manifest.id
                or event.skill_version != target_manifest.version
                or event.skill_hash != target_hash
                or event.source_hash != target_source_hash
                or event.outcome != "failed"
                or event.error_kind not in _SKILL_DEFECT_KINDS
            ):
                raise EvolutionRevalidationError(
                    "supporting event is not an exact source Skill defect: "
                    f"{event_id}"
                )
            selected.append(event)
        defects = self._distinct_skill_defects(selected)
        if len(defects) < self.minimum_deprecation_defects:
            raise EvolutionRevalidationError(
                "deprecation proposal lacks distinct exact-source Skill defects"
            )
        support = [
            event.event_id for event in defects[: self.minimum_deprecation_defects]
        ]
        counterexamples = sorted(
            event.event_id
            for event in events
            if event.skill_id == target_manifest.id
            and event.skill_version == target_manifest.version
            and event.skill_hash == target_hash
            and event.source_hash == target_source_hash
            and event.outcome == "succeeded"
        )
        proposal = EvolutionProposal(
            proposal_id=self._deprecation_proposal_id(
                target_manifest.id,
                target_manifest.version,
                target_hash,
                target_source_hash,
                replacement_manifest.id,
                replacement_manifest.version,
                replacement_hash,
            ),
            created_at=datetime.now(timezone.utc).isoformat(),
            target_skill=target_manifest.id,
            skill_version=target_manifest.version,
            skill_hash=target_hash,
            kind="skill_deprecation",
            status="pending",
            rationale=(
                f"{len(support)} distinct exact-source Skill defects support "
                f"replacement by {replacement_manifest.id}"
            ),
            support_event_ids=support,
            counterexample_event_ids=counterexamples,
            proposed_change={
                "field": "lifecycle",
                "from": target_manifest.lifecycle.status,
                "to": "deprecated",
                "superseded_by": replacement_manifest.id,
                "replacement_version": replacement_manifest.version,
                "replacement_hash": replacement_hash,
                "evidence_event_ids": support,
            },
            target_path_hash=_sha256(
                target_path.relative_to(self.skills_root).as_posix().encode("utf-8")
            ),
            source_hash=target_source_hash,
            proposed_by=proposer,
            proposal_reason=reason,
        )
        self.proposals.submit_if_absent(proposal)
        persisted = self.proposals.get(proposal.proposal_id)
        assert persisted is not None
        return persisted

    def approve(
        self,
        proposal_id: str,
        *,
        approver: str,
        reason: str = "",
    ) -> EvolutionApprovalReceipt:
        """Approve one supported transition through fixed Backend validators."""
        if not approver.strip() or not reason.strip():
            raise ValueError("approval requires a human approver and review reason")
        if len(approver.strip()) > 128 or len(reason.strip()) > 1000:
            raise ValueError("approver or review reason exceeds audit field limits")
        if self.recovery_journal.read() is not None:
            raise EvolutionRevalidationError(
                "an interrupted approval requires reconciliation"
            )
        proposal = self.proposals.get(proposal_id)
        assert proposal is not None
        self._validate_supported_proposal(proposal)
        manifest_path, manifest, _manifest_hash = self._find_manifest(
            proposal.target_skill
        )
        if proposal.kind == "gotcha":
            try:
                self._validate_gotcha_source_snapshot(
                    manifest_path,
                    manifest,
                    proposal,
                )
            except EvolutionRevalidationError as exc:
                self.proposals.mark_stale(
                    proposal_id,
                    reason=str(exc),
                )
                raise
            target_path = manifest_path.with_name("SKILL.md")
            target_hash = proposal.target_content_hash
        else:
            target_path = manifest_path
            target_hash = proposal.skill_hash
        swap_path = _guarded_swap_path(target_path, proposal.proposal_id)
        if (
            _sha256(
                target_path.relative_to(self.skills_root)
                .as_posix()
                .encode("utf-8")
            )
            != proposal.target_path_hash
        ):
            raise EvolutionRevalidationError(
                "proposal target path no longer matches the canonical artifact"
            )
        if not target_path.is_file() or _sha256(target_path.read_bytes()) != target_hash:
            self.proposals.mark_stale(
                proposal_id,
                reason="target content hash changed after proposal synthesis",
            )
            raise EvolutionRevalidationError(
                f"stale proposal {proposal_id}: target content hash changed"
            )
        if manifest.version != proposal.skill_version:
            self.proposals.mark_stale(
                proposal_id,
                reason="skill version changed after proposal synthesis",
            )
            raise EvolutionRevalidationError(
                f"stale proposal {proposal_id}: skill version changed"
            )
        target_before = target_path.read_bytes()
        self._validate_supporting_events(proposal)
        if (
            proposal.kind == "validation_promotion"
            and self._has_disqualifying_defect(proposal)
        ):
            self.proposals.mark_stale(
                proposal_id,
                reason="disqualifying skill defect arrived after proposal synthesis",
            )
            raise EvolutionRevalidationError(
                f"stale proposal {proposal_id}: disqualifying skill defect evidence"
            )

        projection_snapshot: dict[str, bytes | None] | None = None
        validated_target_revision: tuple[str, str] | None = None
        expected_target_revision: tuple[str, str] | None = None
        replacement_snapshot: tuple[Path, SkillManifest] | None = None
        validated_replacement_revision: tuple[str, str] | None = None
        expected_replacement_revision: tuple[str, str] | None = None

        def apply_change(before: bytes, current: EvolutionProposal) -> bytes:
            if _sha256(before) != self._proposal_target_content_hash(current):
                raise EvolutionRevalidationError(
                    f"stale proposal {current.proposal_id}: target content hash changed"
                )
            return self._changed_target_bytes(before, current)

        def representation(staged_path: Path) -> None:
            if proposal.kind == "gotcha":
                staged_text = staged_path.read_text(encoding="utf-8")
                self._validate_changed_skill_md(
                    target_before,
                    staged_text.encode("utf-8"),
                    manifest,
                    proposal,
                )
                from scripts.skill_lint import lint_skill

                lint_errors = lint_skill(
                    manifest_path.parent,
                    skill_md_text=staged_text,
                )
                if lint_errors:
                    raise EvolutionRevalidationError(
                        "targeted Skill lint rejected staged governed Gotcha: "
                        + "; ".join(lint_errors)
                    )
                return
            parsed = load_skill_yaml(staged_path)
            self._validate_changed_manifest(parsed, proposal)

        def execution(_staged_path: Path) -> None:
            nonlocal replacement_snapshot
            nonlocal validated_replacement_revision
            nonlocal validated_target_revision
            if proposal.kind != "gotcha":
                validated_target_revision = self._capture_execution_revision(
                    manifest_path,
                    manifest,
                )
                if validated_target_revision[0] != proposal.skill_hash:
                    raise EvolutionRevalidationError(
                        "target Skill manifest changed before demo validation"
                    )
                if (
                    proposal.kind == "skill_deprecation"
                    and validated_target_revision[1] != proposal.source_hash
                ):
                    raise EvolutionRevalidationError(
                        "deprecated target source no longer matches its evidence"
                    )

            if proposal.kind == "validation_promotion":
                self.execution_adapter.validate_demo(proposal.target_skill)
            elif proposal.kind == "validation_demotion":
                self.execution_adapter.validate_demo_defect(proposal.target_skill)
            elif proposal.kind == "skill_deprecation":
                replacement_skill = str(
                    proposal.proposed_change["superseded_by"]
                )
                self._validate_replacement_snapshot(proposal)
                replacement_path, replacement, _replacement_hash = self._find_manifest(
                    replacement_skill
                )
                replacement_snapshot = (replacement_path, replacement)
                validated_replacement_revision = self._capture_execution_revision(
                    replacement_path,
                    replacement,
                )
                if (
                    validated_replacement_revision[0]
                    != proposal.proposed_change["replacement_hash"]
                ):
                    raise EvolutionRevalidationError(
                        "replacement Skill changed before demo validation"
                    )
                self.execution_adapter.validate_demo(replacement_skill)
                self._validate_replacement_snapshot(proposal)
                if (
                    self._capture_execution_revision(replacement_path, replacement)
                    != validated_replacement_revision
                ):
                    raise EvolutionRevalidationError(
                        "replacement Skill execution source changed during demo validation"
                    )
            elif proposal.kind == "gotcha":
                self._validate_gotcha_source_snapshot(
                    manifest_path,
                    manifest,
                    proposal,
                )
                self.execution_adapter.validate_demo(proposal.target_skill)
                self._validate_gotcha_source_snapshot(
                    manifest_path,
                    manifest,
                    proposal,
                )
            else:  # guarded by _validate_supported_proposal
                raise EvolutionRevalidationError(
                    f"unsupported evolution proposal kind: {proposal.kind}"
                )
            if validated_target_revision is not None:
                current_target_revision = self._capture_execution_revision(
                    manifest_path,
                    manifest,
                )
                # EvolutionProposalStore owns the target-manifest CAS. Let its
                # exact-byte check classify a manifest edit as ``stale`` and
                # preserve those external bytes. Source-only drift is a failed
                # validator and therefore follows the normal rollback record.
                if (
                    current_target_revision[0] == validated_target_revision[0]
                    and current_target_revision != validated_target_revision
                ):
                    raise EvolutionRevalidationError(
                        "target Skill execution source changed during demo validation"
                    )
            if (
                proposal.kind == "validation_promotion"
                and self._has_disqualifying_defect(proposal)
            ):
                raise EvolutionRevalidationError(
                    "disqualifying skill defect evidence arrived during approval"
                )

        def retrieval(live_path: Path) -> None:
            nonlocal projection_snapshot
            if proposal.kind == "gotcha":
                self._validate_gotcha_source_snapshot(
                    manifest_path,
                    manifest,
                    proposal,
                )
                self._validate_changed_skill_md(
                    target_before,
                    live_path.read_bytes(),
                    manifest,
                    proposal,
                )
            else:
                parsed = load_skill_yaml(live_path)
                self._validate_changed_manifest(parsed, proposal)
            if proposal.kind == "skill_deprecation":
                self._validate_replacement_snapshot(proposal)
            if (
                proposal.kind == "validation_promotion"
                and self._has_disqualifying_defect(proposal)
            ):
                raise EvolutionRevalidationError(
                    "disqualifying skill defect evidence arrived before retrieval"
                )
            # Retrieval runs inside EvolutionProposalStore's exclusive
            # approval transaction. Capture projections here so a later
            # failed approval cannot restore bytes from before another
            # serialized approval successfully refreshed them.
            if proposal.kind == "gotcha":
                # Narrative Gotchas are consumed directly from SKILL.md by the
                # registry. They do not project into catalog.json or the DAG,
                # so touching those unrelated artifacts would widen this
                # approval transaction and create stale rollback hazards.
                self._reload_runtime_registry_if_owned()
            else:
                projection_snapshot = self._snapshot_projection_files()
                self.projection_adapter.refresh(
                    self.skills_root,
                    proposal.target_skill,
                )
            self._validate_projected_state(proposal)
            # Refresh may take observable time and may itself record a
            # contract failure. Recheck after it completes so evidence that
            # arrives in that final window rolls back this whole transaction
            # instead of approving a now-disqualified manifest.
            if (
                proposal.kind == "validation_promotion"
                and self._has_disqualifying_defect(proposal)
            ):
                raise EvolutionRevalidationError(
                    "disqualifying skill defect evidence arrived during projection refresh"
                )

        def rollback_projections() -> None:
            if projection_snapshot is not None:
                self._restore_projection_files(projection_snapshot)
            self._reload_runtime_registry_if_owned()

        def commit_approved(approved: EvolutionProposal) -> None:
            # The last defect recheck and durable approved record share the
            # ledger lock. A ledger-governed defect append therefore happens
            # entirely before this check or entirely after the approval state
            # is durable; it cannot land in a final check/append gap.
            with self.ledger.locked_events() as events:
                self._validate_supporting_events(proposal, events=events)
                if proposal.kind != "gotcha":
                    if expected_target_revision is None:
                        raise EvolutionRevalidationError(
                            "target execution authority was not prepared"
                        )
                    if (
                        self._capture_execution_revision(manifest_path, manifest)
                        != expected_target_revision
                    ):
                        raise EvolutionRevalidationError(
                            "target Skill execution authority changed before approval commit"
                        )
                if proposal.kind == "skill_deprecation":
                    self._validate_replacement_snapshot(proposal)
                    if (
                        replacement_snapshot is None
                        or expected_replacement_revision is None
                    ):
                        raise EvolutionRevalidationError(
                            "replacement execution authority was not prepared"
                        )
                    if (
                        self._capture_execution_revision(*replacement_snapshot)
                        != expected_replacement_revision
                    ):
                        raise EvolutionRevalidationError(
                            "replacement Skill execution authority changed before approval commit"
                        )
                elif proposal.kind == "gotcha":
                    self._validate_gotcha_source_snapshot(
                        manifest_path,
                        manifest,
                        proposal,
                    )
                if (
                    proposal.kind == "validation_promotion"
                    and self._has_disqualifying_defect(proposal, events=events)
                ):
                    raise EvolutionRevalidationError(
                        "disqualifying skill defect evidence arrived before approval commit"
                    )
                self.proposals._append_unlocked(approved)

        def prepare_recovery(
            before: bytes,
            after: bytes,
            current: EvolutionProposal,
        ) -> None:
            nonlocal expected_replacement_revision
            nonlocal expected_target_revision
            if proposal.kind != "gotcha":
                if validated_target_revision is None:
                    raise EvolutionRevalidationError(
                        "target execution authority was not validated"
                    )
                if (
                    self._capture_execution_revision(manifest_path, manifest)
                    != validated_target_revision
                ):
                    raise EvolutionRevalidationError(
                        "target Skill execution authority changed before manifest commit"
                    )
                expected_target_revision = self._capture_planned_execution_revision(
                    manifest_path,
                    manifest,
                    transition_path=target_path,
                    before=before,
                    after=after,
                )
                if proposal.kind == "skill_deprecation":
                    if (
                        replacement_snapshot is None
                        or validated_replacement_revision is None
                    ):
                        raise EvolutionRevalidationError(
                            "replacement execution authority was not validated"
                        )
                    if (
                        self._capture_execution_revision(*replacement_snapshot)
                        != validated_replacement_revision
                    ):
                        raise EvolutionRevalidationError(
                            "replacement Skill execution authority changed before manifest commit"
                        )
                    expected_replacement_revision = (
                        self._capture_planned_execution_revision(
                            *replacement_snapshot,
                            transition_path=target_path,
                            before=before,
                            after=after,
                        )
                    )
            self.recovery_journal.prepare(
                proposal=current,
                target_relative_path=target_path.relative_to(
                    self.skills_root
                ).as_posix(),
                swap_relative_path=swap_path.relative_to(
                    self.skills_root
                ).as_posix(),
                before=before,
                after=after,
                mode=target_path.stat().st_mode,
                approver=approver.strip(),
                reason=reason.strip(),
            )

        def ensure_no_inflight_recovery() -> None:
            if self.recovery_journal.read() is not None:
                raise EvolutionRecoveryRequiredError(
                    "an interrupted approval requires reconciliation"
                )

        def clear_recovery() -> None:
            try:
                if swap_path.exists():
                    raise EvolutionRecoveryRequiredError(
                        "guarded swap witness requires reconciliation"
                    )
                _fsync_directory(swap_path.parent)
                self.recovery_journal.clear(proposal.proposal_id)
            except Exception:
                # The durable proposal state is authoritative once appended.
                # A leftover journal is safe and can be cleared by reconcile().
                pass

        try:
            return self.proposals._approve_and_apply(
                proposal_id,
                approver=approver,
                reason=reason,
                target_path=target_path,
                apply_change=apply_change,
                validators={
                    "representation": representation,
                    "execution": execution,
                    "retrieval": retrieval,
                },
                on_rollback=rollback_projections,
                approval_committer=commit_approved,
                approval_guard=ensure_no_inflight_recovery,
                before_commit=prepare_recovery,
                on_state_persisted=clear_recovery,
                guarded_swap_path=swap_path,
            )
        except EvolutionRecoveryRequiredError as exc:
            raise EvolutionRevalidationError(str(exc)) from exc
        except EvolutionRevalidationError:
            latest = self.proposals.get(proposal_id)
            if latest is not None and latest.status == "pending":
                self.proposals.mark_stale(
                    proposal_id,
                    reason="governed target changed during approval",
                )
            raise
        except EvolutionApplyError as exc:
            latest = self.proposals.get(proposal_id)
            message = str(exc)
            if latest is not None and latest.status == "stale":
                raise EvolutionRevalidationError(message) from exc
            raise EvolutionRevalidationError(message) from exc
        except Exception as exc:
            raise EvolutionRevalidationError(
                f"approval could not be committed: {exc}"
            ) from exc

    def reconcile(self, *, operator: str, reason: str) -> dict[str, str]:
        """Recover one interrupted approval without inferring approval."""
        if not operator.strip() or not reason.strip():
            raise ValueError("reconciliation requires an operator and reason")
        if len(operator.strip()) > 128 or len(reason.strip()) > 1000:
            raise ValueError("operator or reconciliation reason exceeds audit limits")
        with _exclusive_file_lock(self.proposals._lock_path):
            record = self.recovery_journal.read()
            if record is None:
                return {"status": "clean", "proposal_id": "", "action": "none"}
            proposal_id = str(record["proposal_id"])
            proposal = self.proposals._get_unlocked(proposal_id)
            assert proposal is not None
            if (
                record["target_skill"] != proposal.target_skill
                or _sha256(
                    str(record["target_relative_path"]).encode("utf-8")
                )
                != proposal.target_path_hash
                or record["before_hash"]
                != self._proposal_target_content_hash(proposal)
                or record["after_bytes"]
                != self._changed_target_bytes(record["before_bytes"], proposal)
            ):
                raise EvolutionRevalidationError(
                    "recovery journal target does not match proposal"
                )
            relative = Path(str(record["target_relative_path"]))
            target = (self.skills_root / relative).resolve()
            try:
                target.relative_to(self.skills_root)
            except ValueError as exc:
                raise EvolutionRevalidationError(
                    "recovery journal target escapes the skills root"
                ) from exc
            expected_swap = _guarded_swap_path(target, proposal_id)
            legacy_journal = int(record["schema_version"]) == 1
            if legacy_journal:
                swap = expected_swap
            else:
                swap_relative = Path(str(record["swap_relative_path"]))
                swap = (self.skills_root / swap_relative).resolve()
                try:
                    swap.relative_to(self.skills_root)
                except ValueError as exc:
                    raise EvolutionRevalidationError(
                        "recovery swap witness escapes the skills root"
                    ) from exc
                if swap != expected_swap.resolve():
                    raise EvolutionRevalidationError(
                        "recovery swap witness does not match proposal"
                    )
            before = record["before_bytes"]
            after = record["after_bytes"]
            if not target.is_file():
                return {
                    "status": "conflict",
                    "proposal_id": proposal_id,
                    "action": "manual_recovery_required",
                }
            current = target.read_bytes()
            witness: bytes | None = None
            if swap.exists():
                try:
                    witness = swap.read_bytes()
                except OSError:
                    return {
                        "status": "conflict",
                        "proposal_id": proposal_id,
                        "action": "manual_recovery_required",
                    }
            if proposal.status == "approved":
                if witness is not None:
                    # A durable approval can only follow a fully verified
                    # exchange. The sole recoverable leftover is its exact
                    # predecessor after an interrupted witness cleanup.
                    if current != after or witness != before:
                        return {
                            "status": "conflict",
                            "proposal_id": proposal_id,
                            "action": "manual_recovery_required",
                        }
                    try:
                        _remove_guarded_swap(swap)
                    except Exception:
                        return {
                            "status": "conflict",
                            "proposal_id": proposal_id,
                            "action": "manual_recovery_required",
                        }
                    current = target.read_bytes()
                if current == after:
                    try:
                        _remove_guarded_swap(expected_swap)
                    except Exception:
                        return {
                            "status": "conflict",
                            "proposal_id": proposal_id,
                            "action": "manual_recovery_required",
                        }
                    self._refresh_reconciled_state(proposal)
                    self.recovery_journal.clear(proposal_id)
                    return {
                        "status": "approved",
                        "proposal_id": proposal_id,
                        "action": "finalized_committed_approval",
                    }
                # The durable human decision remains authoritative, but its
                # promoted bytes are no longer live. Recovery must neither
                # demote that decision nor overwrite the external state.
                return {
                    "status": "conflict",
                    "proposal_id": proposal_id,
                    "action": "manual_recovery_required",
                }
            if proposal.status not in {"pending", "rolled_back", "stale"}:
                raise EvolutionRevalidationError(
                    f"cannot reconcile proposal state: {proposal.status}"
                )
            if legacy_journal and current == after:
                # Schema v1 used an untracked random exchange path. Exact
                # after bytes therefore cannot prove that an exchanged-out
                # external edit was not stranded at process termination.
                return {
                    "status": "conflict",
                    "proposal_id": proposal_id,
                    "action": "manual_recovery_required",
                }
            if witness is not None:
                safe_pre_exchange = witness == after and current == before
                safe_verified_exchange = witness == before and current == after
                if not (safe_pre_exchange or safe_verified_exchange):
                    # In particular, a third byte sequence at the witness is
                    # the non-cooperating edit swapped out before a crash.
                    # Keep both the live path and witness for manual recovery.
                    return {
                        "status": "conflict",
                        "proposal_id": proposal_id,
                        "action": "manual_recovery_required",
                    }
                try:
                    _remove_guarded_swap(swap)
                except Exception:
                    return {
                        "status": "conflict",
                        "proposal_id": proposal_id,
                        "action": "manual_recovery_required",
                    }
                current = target.read_bytes()
            if current == after:
                try:
                    _atomic_write(
                        target,
                        before,
                        mode=int(record["mode"]),
                        expected=after,
                        swap_path=expected_swap,
                    )
                except _AtomicWriteConflict:
                    return {
                        "status": "conflict",
                        "proposal_id": proposal_id,
                        "action": "manual_recovery_required",
                    }
            elif current != before:
                return {
                    "status": "conflict",
                    "proposal_id": proposal_id,
                    "action": "manual_recovery_required",
                }
            self._refresh_reconciled_state(proposal)
            if proposal.status == "pending":
                recovered = replace(
                    proposal,
                    status="rolled_back",
                    approved_by=str(record["approver"]),
                    approval_reason=str(record["reason"]),
                    before_hash=str(record["before_hash"]),
                    after_hash=str(record["after_hash"]),
                    validation_error="interrupted_approval_reconciled",
                    reconciled_by=operator.strip(),
                    reconciliation_reason=reason.strip(),
                )
                self.proposals._append_unlocked(recovered)
            try:
                if expected_swap.exists():
                    return {
                        "status": "conflict",
                        "proposal_id": proposal_id,
                        "action": "manual_recovery_required",
                    }
                _remove_guarded_swap(expected_swap)
            except Exception:
                return {
                    "status": "conflict",
                    "proposal_id": proposal_id,
                    "action": "manual_recovery_required",
                }
            self.recovery_journal.clear(proposal_id)
            return {
                "status": "rolled_back",
                "proposal_id": proposal_id,
                "action": "restored_interrupted_approval",
            }

    def reject(
        self,
        proposal_id: str,
        *,
        approver: str,
        reason: str,
    ) -> EvolutionProposal:
        if len(approver.strip()) > 128 or len(reason.strip()) > 1000:
            raise ValueError("approver or review reason exceeds audit field limits")
        if self.recovery_journal.read() is not None:
            raise EvolutionRevalidationError(
                "an interrupted approval requires reconciliation"
            )

        def ensure_no_inflight_recovery() -> None:
            if self.recovery_journal.read() is not None:
                raise EvolutionRecoveryRequiredError(
                    "an interrupted approval requires reconciliation"
                )

        try:
            return self.proposals.reject(
                proposal_id,
                approver=approver,
                reason=reason,
                decision_guard=ensure_no_inflight_recovery,
            )
        except EvolutionRecoveryRequiredError as exc:
            raise EvolutionRevalidationError(str(exc)) from exc

    def _manifest_snapshots(self) -> list[tuple[Path, SkillManifest, str]]:
        snapshots: list[tuple[Path, SkillManifest, str]] = []
        for path in sorted(self.skills_root.rglob("skill.yaml")):
            relative = path.relative_to(self.skills_root)
            if any(part.startswith((".", "__", "_")) for part in relative.parts[:-1]):
                continue
            payload = path.read_bytes()
            raw = yaml.safe_load(payload.decode("utf-8"))
            if not isinstance(raw, dict):
                raise EvolutionRevalidationError(
                    f"canonical manifest must be a mapping: {path}"
                )
            snapshots.append((path, parse_skill_manifest(raw), _sha256(payload)))
        return snapshots

    def _find_manifest(self, skill_id: str) -> tuple[Path, SkillManifest, str]:
        matches = [
            (path, manifest, manifest_hash)
            for path, manifest, manifest_hash in self._manifest_snapshots()
            if manifest.id == skill_id
        ]
        if len(matches) != 1:
            raise EvolutionRevalidationError(
                f"expected exactly one manifest for {skill_id}, found {len(matches)}"
            )
        return matches[0]

    @staticmethod
    def _distinct_demo_successes(events: list[SkillRunEvent]) -> list[SkillRunEvent]:
        distinct: dict[str, SkillRunEvent] = {}
        for event in sorted(events, key=lambda item: (item.occurred_at, item.event_id)):
            if (
                event.outcome != "succeeded"
                or event.error_kind != "none"
                or event.evidence_kind != "demo"
            ):
                continue
            identity = event.run_id.strip() or event.execution_fingerprint.strip()
            if not identity:
                continue
            distinct.setdefault(identity, event)
        return list(distinct.values())

    @staticmethod
    def _event_identity(event: SkillRunEvent) -> str:
        return event.run_id.strip() or event.execution_fingerprint.strip()

    @classmethod
    def _execution_identities(
        cls,
        events: Iterable[SkillRunEvent],
    ) -> set[str]:
        return {
            identity
            for event in events
            if (identity := cls._event_identity(event))
        }

    @staticmethod
    def _distinct_demo_defects(events: list[SkillRunEvent]) -> list[SkillRunEvent]:
        distinct: dict[str, SkillRunEvent] = {}
        for event in sorted(events, key=lambda item: (item.occurred_at, item.event_id)):
            if (
                event.outcome != "failed"
                or event.evidence_kind != "demo"
                or event.error_kind not in _SKILL_DEFECT_KINDS
            ):
                continue
            identity = event.run_id.strip() or event.execution_fingerprint.strip()
            if not identity:
                continue
            distinct.setdefault(identity, event)
        return list(distinct.values())

    @staticmethod
    def _distinct_skill_defects(events: list[SkillRunEvent]) -> list[SkillRunEvent]:
        distinct: dict[str, SkillRunEvent] = {}
        for event in sorted(events, key=lambda item: (item.occurred_at, item.event_id)):
            if event.outcome != "failed" or event.error_kind not in _SKILL_DEFECT_KINDS:
                continue
            identity = event.run_id.strip() or event.execution_fingerprint.strip()
            if not identity:
                continue
            distinct.setdefault(identity, event)
        return list(distinct.values())

    @staticmethod
    def _distinct_ordinary_successes(
        events: Iterable[SkillRunEvent],
    ) -> list[SkillRunEvent]:
        distinct: dict[str, SkillRunEvent] = {}
        for event in sorted(events, key=lambda item: (item.occurred_at, item.event_id)):
            if (
                event.outcome != "succeeded"
                or event.error_kind != "none"
                or event.evidence_kind != "ordinary"
            ):
                continue
            identity = event.run_id.strip() or event.execution_fingerprint.strip()
            if not identity:
                continue
            distinct.setdefault(identity, event)
        return list(distinct.values())

    @staticmethod
    def _normalize_gotcha_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
        expected_keys = {"lead", "condition", "guidance", "anchors"}
        if set(entry) != expected_keys:
            raise ValueError("Gotcha entry must contain lead, condition, guidance, and anchors")
        values: dict[str, Any] = {}
        limits = {"lead": 160, "condition": 500, "guidance": 500}
        for field, limit in limits.items():
            raw = entry.get(field)
            if not isinstance(raw, str):
                raise ValueError(f"Gotcha {field} must be text")
            value = raw.strip()
            if field == "lead":
                value = value.rstrip(".").strip()
            security_view = unicodedata.normalize("NFKC", value)
            has_unsafe_unicode = any(
                unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"}
                or (char.isspace() and char != " ")
                for char in value
            )
            if (
                not value
                or len(value) > limit
                or any(char in _UNSAFE_GOTCHA_TEXT_CHARS for char in security_view)
                or has_unsafe_unicode
                or any(
                    pattern.search(security_view)
                    for pattern in _UNSAFE_GOTCHA_TEXT_PATTERNS
                )
                or _has_credential_assignment(security_view)
            ):
                raise ValueError(f"Gotcha {field} is empty, unsafe, or exceeds audit limits")
            values[field] = value
        raw_anchors = entry.get("anchors")
        if not isinstance(raw_anchors, list):
            raise ValueError("Gotcha anchors must be a list")
        anchors = list(dict.fromkeys(str(value).strip() for value in raw_anchors))
        anchors = [value for value in anchors if value]
        if not 1 <= len(anchors) <= 8:
            raise ValueError("Gotcha entry requires between one and eight anchors")
        file_anchor = re.compile(r"[A-Za-z0-9_.-]+\.py:[1-9][0-9]*")
        if any(
            len(anchor) > 128
            or not file_anchor.fullmatch(anchor)
            for anchor in anchors
        ):
            raise ValueError("Gotcha anchor must be a safe file:line")
        anchors.sort()
        values["anchors"] = anchors
        return values

    @staticmethod
    def _event_supports_gotcha_anchor(event: SkillRunEvent, anchor: str) -> bool:
        return bool(
            re.fullmatch(r"[A-Za-z0-9_.-]+\.py:[1-9][0-9]*", anchor)
            and f"trace:{anchor}" in event.evidence_refs
        )

    @classmethod
    def _safe_gotcha_event_anchors(
        cls,
        event: SkillRunEvent,
        source_path: Path,
    ) -> list[str]:
        candidates: set[str] = set()
        for ref in event.evidence_refs:
            if ref.startswith("trace:"):
                candidates.add(ref.removeprefix("trace:"))
        resolved: list[str] = []
        for anchor in sorted(candidates):
            try:
                cls._validate_gotcha_anchors_against_source(source_path, [anchor])
            except (EvolutionRevalidationError, OSError, UnicodeError):
                continue
            resolved.append(anchor)
        return resolved

    @staticmethod
    def _render_gotcha_bullet(entry: Mapping[str, Any]) -> str:
        anchors = ", ".join(f"`{anchor}`" for anchor in entry["anchors"])
        lead = str(entry["lead"]).rstrip(".")
        return (
            f"- **{lead}.** {entry['condition']} {entry['guidance']} "
            f"Evidence: {anchors}."
        )

    @staticmethod
    def _merge_candidate_proposal_id(skill_a: str, skill_b: str) -> str:
        # Stable per unordered pair (ADR 0074 §8.2): re-running refresh is
        # idempotent, and one advisory covers the pair regardless of which side
        # is the sorted target anchor.
        basis = json.dumps(
            ["merge_candidate", *sorted((skill_a, skill_b))],
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(basis).hexdigest()[:24]

    @staticmethod
    def _protocol_revision_proposal_id(skill_id: str, problem_kind: str) -> str:
        # Stable per (skill, problem kind) so re-running refresh is idempotent
        # and one advisory tracks each open protocol gap for a Skill.
        basis = json.dumps(
            ["protocol_revision", skill_id, problem_kind],
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(basis).hexdigest()[:24]

    @staticmethod
    def _promotion_proposal_id(skill_id: str, version: str, skill_hash: str) -> str:
        basis = json.dumps(
            [
                skill_id,
                version,
                skill_hash,
                _SUPPORTED_FROM_LEVEL,
                _SUPPORTED_TO_LEVEL,
            ],
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(basis).hexdigest()[:24]

    @staticmethod
    def _demotion_proposal_id(skill_id: str, version: str, skill_hash: str) -> str:
        basis = json.dumps(
            [
                skill_id,
                version,
                skill_hash,
                _DEMOTION_FROM_LEVEL,
                _DEMOTION_TO_LEVEL,
            ],
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(basis).hexdigest()[:24]

    @staticmethod
    def _deprecation_proposal_id(
        skill_id: str,
        version: str,
        skill_hash: str,
        source_hash: str,
        replacement_skill: str,
        replacement_version: str,
        replacement_hash: str,
    ) -> str:
        basis = json.dumps(
            [
                skill_id,
                version,
                skill_hash,
                source_hash,
                "deprecated",
                replacement_skill,
                replacement_version,
                replacement_hash,
            ],
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(basis).hexdigest()[:24]

    @staticmethod
    def _gotcha_evidence_proposal_id(
        skill_id: str,
        version: str,
        skill_hash: str,
        source_hash: str,
        target_path_hash: str,
        target_content_hash: str,
        error_kind: str,
        environment_id: str,
        anchor: str,
    ) -> str:
        basis = json.dumps(
            [
                skill_id,
                version,
                skill_hash,
                source_hash,
                target_path_hash,
                target_content_hash,
                "gotcha_evidence",
                error_kind,
                environment_id,
                anchor,
            ],
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(basis).hexdigest()[:24]

    @staticmethod
    def _gotcha_cluster_identity(
        proposal: EvolutionProposal,
    ) -> tuple[str, str, str, str, str, str, str] | None:
        """Identify one exact-source failure cluster independent of its target.

        Evidence candidate revisions bind the current ``SKILL.md`` path/content,
        so target drift deliberately gives them a new proposal id.  Once a
        maintainer has materialized that same exact-source cluster, however,
        the approval's own Gotcha write must not immediately nominate it again.
        Source, manifest, environment, error kind, or trace-anchor drift still
        yields a different identity and therefore remains reviewable.
        """
        if proposal.kind not in {"gotcha_evidence", "gotcha"}:
            return None
        change = proposal.proposed_change
        anchor = str(change.get("evidence_anchor") or "").strip()
        if proposal.kind == "gotcha":
            entry = change.get("entry")
            anchors = entry.get("anchors") if isinstance(entry, Mapping) else None
            if isinstance(anchors, list) and anchors:
                anchor = str(anchors[0]).strip()
        identity = (
            proposal.target_skill.strip(),
            proposal.skill_version.strip(),
            proposal.skill_hash.strip(),
            proposal.source_hash.strip(),
            str(change.get("evidence_error_kind") or "").strip(),
            str(change.get("evidence_environment_id") or "").strip(),
            anchor,
        )
        return identity if all(identity) else None

    @staticmethod
    def _gotcha_proposal_id(candidate_id: str, entry_digest: str) -> str:
        basis = json.dumps(
            [candidate_id, "gotcha", entry_digest],
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(basis).hexdigest()[:24]

    @staticmethod
    def _gotcha_review_proposal_id(
        source_proposal_id: str,
        current_state: str,
        current_manifest_hash: str,
        current_source_hash: str,
        current_target_path_hash: str,
        current_target_content_hash: str,
    ) -> str:
        basis = json.dumps(
            [
                source_proposal_id,
                "gotcha_review",
                current_state,
                current_manifest_hash,
                current_source_hash,
                current_target_path_hash,
                current_target_content_hash,
            ],
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(basis).hexdigest()[:24]

    def _validate_supported_proposal(self, proposal: EvolutionProposal) -> None:
        if proposal.status != "pending":
            raise EvolutionRevalidationError(
                f"proposal is not pending: {proposal.status}"
            )
        transitions = {
            "validation_promotion": (_SUPPORTED_FROM_LEVEL, _SUPPORTED_TO_LEVEL),
            "validation_demotion": (_DEMOTION_FROM_LEVEL, _DEMOTION_TO_LEVEL),
        }
        transition = transitions.get(proposal.kind)
        expected: dict[str, Any] | None = (
            {
                "field": "validation.level",
                "from": transition[0],
                "to": transition[1],
                "evidence_event_ids": proposal.support_event_ids,
            }
            if transition is not None
            else None
        )
        if proposal.kind == "skill_deprecation":
            replacement = str(proposal.proposed_change.get("superseded_by") or "")
            source_status = str(proposal.proposed_change.get("from") or "")
            expected = {
                "field": "lifecycle",
                "from": source_status,
                "to": "deprecated",
                "superseded_by": replacement,
                "replacement_version": str(
                    proposal.proposed_change.get("replacement_version") or ""
                ),
                "replacement_hash": str(
                    proposal.proposed_change.get("replacement_hash") or ""
                ),
                "evidence_event_ids": proposal.support_event_ids,
            }
            if (
                source_status not in _ROUTABLE_LIFECYCLES
                or not replacement
                or replacement == proposal.target_skill
                or not re.fullmatch(
                    r"sha256:[0-9a-f]{64}",
                    expected["replacement_hash"],
                )
                or not re.fullmatch(
                    r"sha256:[0-9a-f]{64}",
                    proposal.source_hash,
                )
                or proposal.proposal_id
                != self._deprecation_proposal_id(
                    proposal.target_skill,
                    proposal.skill_version,
                    proposal.skill_hash,
                    proposal.source_hash,
                    replacement,
                    expected["replacement_version"],
                    expected["replacement_hash"],
                )
                or not expected["replacement_version"]
                or not proposal.proposed_by.strip()
                or not proposal.proposal_reason.strip()
                or len(proposal.proposed_by.strip()) > 128
                or len(proposal.proposal_reason.strip()) > 1000
            ):
                expected = None
        if proposal.kind == "gotcha":
            evidence_error_kind = ""
            environment_id = ""
            expected_candidate_id = ""
            entry_digest = ""
            try:
                normalized_entry = self._normalize_gotcha_entry(
                    proposal.proposed_change.get("entry", {})
                )
                entry_digest = _sha256(
                    json.dumps(
                        normalized_entry,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                evidence_error_kind = str(
                    proposal.proposed_change.get("evidence_error_kind") or ""
                )
                environment_id = str(
                    proposal.proposed_change.get("evidence_environment_id") or ""
                )
                expected = {
                    "field": "SKILL.md.Gotchas",
                    "action": "append_canonical_entry",
                    "entry": normalized_entry,
                    "rendered_bullet": self._render_gotcha_bullet(normalized_entry),
                    "entry_digest": entry_digest,
                    "evidence_error_kind": evidence_error_kind,
                    "evidence_environment_id": environment_id,
                    "evidence_event_ids": proposal.support_event_ids,
                    "source_candidate_id": str(
                        proposal.proposed_change.get("source_candidate_id") or ""
                    ),
                }
                expected_candidate_id = self._gotcha_evidence_proposal_id(
                    proposal.target_skill,
                    proposal.skill_version,
                    proposal.skill_hash,
                    proposal.source_hash,
                    proposal.target_path_hash,
                    proposal.target_content_hash,
                    evidence_error_kind,
                    environment_id,
                    normalized_entry["anchors"][0],
                )
            except (TypeError, ValueError):
                expected = None
            if (
                expected is None
                or evidence_error_kind not in _SKILL_DEFECT_KINDS
                or not environment_id
                or not re.fullmatch(
                    r"[0-9a-f]{24}",
                    expected["source_candidate_id"] if expected is not None else "",
                )
                or (
                    expected is not None
                    and expected["source_candidate_id"] != expected_candidate_id
                )
                or proposal.proposal_id
                != self._gotcha_proposal_id(expected_candidate_id, entry_digest)
                or len(environment_id) > 256
                or not proposal.counterexample_event_ids
                or not proposal.proposed_by.strip()
                or not proposal.proposal_reason.strip()
                or len(proposal.proposed_by.strip()) > 128
                or len(proposal.proposal_reason.strip()) > 1000
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", proposal.source_hash)
                or not re.fullmatch(
                    r"sha256:[0-9a-f]{64}",
                    proposal.target_content_hash,
                )
            ):
                expected = None
        if expected is None or proposal.proposed_change != expected:
            raise EvolutionRevalidationError(
                "proposal is not a supported governed evolution transition"
            )
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", proposal.target_path_hash):
            raise EvolutionRevalidationError("proposal has an invalid target path")

    def _validate_supporting_events(
        self,
        proposal: EvolutionProposal,
        *,
        events: Iterable[SkillRunEvent] | None = None,
    ) -> None:
        available = self.ledger.events() if events is None else events
        by_id = {event.event_id: event for event in available}
        selected: list[SkillRunEvent] = []
        for event_id in proposal.support_event_ids:
            event = by_id.get(event_id)
            if event is None:
                raise EvolutionRevalidationError(
                    f"supporting event is missing: {event_id}"
                )
            if (
                event.skill_id != proposal.target_skill
                or event.skill_version != proposal.skill_version
                or event.skill_hash != proposal.skill_hash
            ):
                raise EvolutionRevalidationError(
                    f"supporting event no longer matches proposal: {event_id}"
                )
            selected.append(event)
        if proposal.kind == "validation_promotion":
            distinct = self._distinct_demo_successes(selected)
            required = self.minimum_demo_executions
            label = "distinct explicit demo success"
        elif proposal.kind == "validation_demotion":
            distinct = self._distinct_demo_defects(selected)
            required = self.minimum_demo_failures
            label = "distinct explicit demo Skill defect"
        elif proposal.kind == "skill_deprecation":
            if any(event.source_hash != proposal.source_hash for event in selected):
                raise EvolutionRevalidationError(
                    "deprecation evidence no longer matches its exact source"
                )
            if len(set(proposal.counterexample_event_ids)) != len(
                proposal.counterexample_event_ids
            ):
                raise EvolutionRevalidationError(
                    "deprecation counterexample ids must be unique"
                )
            for event_id in proposal.counterexample_event_ids:
                event = by_id.get(event_id)
                if event is None:
                    raise EvolutionRevalidationError(
                        f"deprecation counterexample event is missing: {event_id}"
                    )
                if (
                    event.skill_id != proposal.target_skill
                    or event.skill_version != proposal.skill_version
                    or event.skill_hash != proposal.skill_hash
                    or event.source_hash != proposal.source_hash
                    or event.outcome != "succeeded"
                ):
                    raise EvolutionRevalidationError(
                        "deprecation counterexample no longer matches its exact "
                        f"source: {event_id}"
                    )
            distinct = self._distinct_skill_defects(selected)
            required = self.minimum_deprecation_defects
            label = "distinct exact-source Skill defect"
        elif proposal.kind == "gotcha":
            if any(
                event.source_hash != proposal.source_hash
                or event.evidence_kind != "ordinary"
                for event in selected
            ):
                raise EvolutionRevalidationError(
                    "Gotcha evidence no longer matches its exact source"
                )
            distinct = self._distinct_skill_defects(selected)
            required = self.minimum_gotcha_defects
            label = "distinct exact-source ordinary Skill defect"
            kinds = {event.error_kind for event in distinct}
            environments = {event.environment_id for event in distinct}
            if (
                len(kinds) != 1
                or len(environments) != 1
                or next(iter(kinds), "")
                != proposal.proposed_change["evidence_error_kind"]
                or next(iter(environments), "")
                != proposal.proposed_change["evidence_environment_id"]
            ):
                raise EvolutionRevalidationError(
                    "Gotcha evidence signature no longer matches proposal"
                )
            anchors = proposal.proposed_change["entry"]["anchors"]
            if any(
                not all(
                    self._event_supports_gotcha_anchor(event, anchor)
                    for event in distinct
                )
                for anchor in anchors
            ):
                raise EvolutionRevalidationError(
                    "Gotcha structural evidence no longer resolves"
                )
            counterexamples: list[SkillRunEvent] = []
            for event_id in proposal.counterexample_event_ids:
                event = by_id.get(event_id)
                if event is None:
                    raise EvolutionRevalidationError(
                        f"counterexample event is missing: {event_id}"
                    )
                if (
                    event.skill_id != proposal.target_skill
                    or event.skill_version != proposal.skill_version
                    or event.skill_hash != proposal.skill_hash
                    or event.source_hash != proposal.source_hash
                    or event.environment_id
                    != proposal.proposed_change["evidence_environment_id"]
                ):
                    raise EvolutionRevalidationError(
                        f"counterexample no longer matches proposal: {event_id}"
                    )
                counterexamples.append(event)
            if (
                len(self._distinct_ordinary_successes(counterexamples))
                < self.minimum_gotcha_counterexamples
            ):
                raise EvolutionRevalidationError(
                    "Gotcha proposal lacks an exact-source success counterexample"
                )
            if self._execution_identities(distinct).intersection(
                self._execution_identities(counterexamples)
            ):
                raise EvolutionRevalidationError(
                    "Gotcha evidence has a conflicting execution identity"
                )
        else:
            raise EvolutionRevalidationError(
                f"unsupported evidence policy for {proposal.kind}"
            )
        if len(distinct) < required:
            raise EvolutionRevalidationError(
                f"proposal lacks {label} evidence"
            )

    def _has_disqualifying_defect(
        self,
        proposal: EvolutionProposal,
        *,
        events: Iterable[SkillRunEvent] | None = None,
    ) -> bool:
        return any(
            event.skill_id == proposal.target_skill
            and event.skill_version == proposal.skill_version
            and event.skill_hash == proposal.skill_hash
            and event.error_kind in _SKILL_DEFECT_KINDS
            for event in (self.ledger.events() if events is None else events)
        )

    @staticmethod
    def _promoted_manifest_bytes(
        before: bytes,
        proposal: EvolutionProposal,
    ) -> bytes:
        raw = yaml.safe_load(before.decode("utf-8"))
        if not isinstance(raw, dict):
            raise EvolutionRevalidationError("skill.yaml must be a mapping")
        validation = raw.setdefault("validation", {})
        if not isinstance(validation, dict):
            raise EvolutionRevalidationError("validation must be a mapping")
        current = str(validation.get("level") or _SUPPORTED_FROM_LEVEL)
        if current != _SUPPORTED_FROM_LEVEL:
            raise EvolutionRevalidationError(
                f"expected {_SUPPORTED_FROM_LEVEL}, found {current}"
            )
        evidence = validation.setdefault("evidence", [])
        if not isinstance(evidence, list):
            raise EvolutionRevalidationError("validation.evidence must be a list")
        evidence_ref = (
            f"evolution:{proposal.proposal_id}:events="
            + ",".join(proposal.support_event_ids)
        )
        validation["level"] = _SUPPORTED_TO_LEVEL
        validation["evidence"] = [
            *[str(value) for value in evidence if str(value).strip()],
            evidence_ref,
        ]
        parse_skill_manifest(raw)
        return yaml.safe_dump(
            raw,
            sort_keys=False,
            allow_unicode=True,
            width=100,
        ).encode("utf-8")

    @staticmethod
    def _demoted_manifest_bytes(
        before: bytes,
        proposal: EvolutionProposal,
    ) -> bytes:
        raw = yaml.safe_load(before.decode("utf-8"))
        if not isinstance(raw, dict):
            raise EvolutionRevalidationError("skill.yaml must be a mapping")
        validation = raw.setdefault("validation", {})
        if not isinstance(validation, dict):
            raise EvolutionRevalidationError("validation must be a mapping")
        current = str(validation.get("level") or _SUPPORTED_FROM_LEVEL)
        if current != _DEMOTION_FROM_LEVEL:
            raise EvolutionRevalidationError(
                f"expected {_DEMOTION_FROM_LEVEL}, found {current}"
            )
        evidence = validation.setdefault("evidence", [])
        if not isinstance(evidence, list):
            raise EvolutionRevalidationError("validation.evidence must be a list")
        evidence_ref = (
            f"evolution:{proposal.proposal_id}:demotion-events="
            + ",".join(proposal.support_event_ids)
        )
        validation["level"] = _DEMOTION_TO_LEVEL
        validation["evidence"] = [
            *[str(value) for value in evidence if str(value).strip()],
            evidence_ref,
        ]
        parse_skill_manifest(raw)
        return yaml.safe_dump(
            raw,
            sort_keys=False,
            allow_unicode=True,
            width=100,
        ).encode("utf-8")

    @staticmethod
    def _deprecated_manifest_bytes(
        before: bytes,
        proposal: EvolutionProposal,
    ) -> bytes:
        raw = yaml.safe_load(before.decode("utf-8"))
        if not isinstance(raw, dict):
            raise EvolutionRevalidationError("skill.yaml must be a mapping")
        lifecycle = raw.setdefault("lifecycle", {})
        if not isinstance(lifecycle, dict):
            raise EvolutionRevalidationError("lifecycle must be a mapping")
        current = str(lifecycle.get("status") or "mvp")
        expected = str(proposal.proposed_change["from"])
        if current != expected:
            raise EvolutionRevalidationError(f"expected {expected}, found {current}")
        lifecycle["status"] = "deprecated"
        lifecycle["superseded_by"] = str(
            proposal.proposed_change["superseded_by"]
        )
        parse_skill_manifest(raw)
        return yaml.safe_dump(
            raw,
            sort_keys=False,
            allow_unicode=True,
            width=100,
        ).encode("utf-8")

    @classmethod
    def _changed_manifest_bytes(
        cls,
        before: bytes,
        proposal: EvolutionProposal,
    ) -> bytes:
        if proposal.kind == "validation_promotion":
            return cls._promoted_manifest_bytes(before, proposal)
        if proposal.kind == "validation_demotion":
            return cls._demoted_manifest_bytes(before, proposal)
        if proposal.kind == "skill_deprecation":
            return cls._deprecated_manifest_bytes(before, proposal)
        raise EvolutionRevalidationError(
            f"unsupported evolution proposal kind: {proposal.kind}"
        )

    @staticmethod
    def _proposal_target_content_hash(proposal: EvolutionProposal) -> str:
        return (
            proposal.target_content_hash
            if proposal.kind == "gotcha"
            else proposal.skill_hash
        )

    @classmethod
    def _changed_target_bytes(
        cls,
        before: bytes,
        proposal: EvolutionProposal,
    ) -> bytes:
        if proposal.kind == "gotcha":
            try:
                text = before.decode("utf-8")
                changed = append_gotcha_entry(
                    text,
                    str(proposal.proposed_change["rendered_bullet"]),
                )
            except (UnicodeDecodeError, ValueError) as exc:
                raise EvolutionRevalidationError(str(exc)) from exc
            return changed.encode("utf-8")
        return cls._changed_manifest_bytes(before, proposal)

    @classmethod
    def _validate_changed_skill_md(
        cls,
        before: bytes,
        after: bytes,
        manifest: SkillManifest,
        proposal: EvolutionProposal,
    ) -> None:
        expected = cls._changed_target_bytes(before, proposal)
        if after != expected:
            raise EvolutionRevalidationError(
                "representation changed bytes outside the governed Gotchas edit"
            )
        try:
            text = after.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EvolutionRevalidationError("SKILL.md must remain UTF-8") from exc
        if render_skill_md(manifest, text) != text:
            raise EvolutionRevalidationError(
                "SKILL.md generated representation is not idempotent"
            )
        bullet = str(proposal.proposed_change["rendered_bullet"])
        if text.splitlines().count(bullet) != 1:
            raise EvolutionRevalidationError(
                "representation did not append exactly one Gotcha"
            )

    @staticmethod
    def _validate_gotcha_anchors_against_source(
        source_path: Path,
        anchors: Iterable[str],
    ) -> None:
        source_text = source_path.read_text(encoding="utf-8")
        line_count = len(source_text.splitlines())
        for anchor in anchors:
            file_match = re.fullmatch(
                r"([A-Za-z0-9_.-]+\.py):([1-9][0-9]*)",
                anchor,
            )
            if file_match is not None:
                if (
                    file_match.group(1) != source_path.name
                    or int(file_match.group(2)) > line_count
                ):
                    raise EvolutionRevalidationError(
                        f"Gotcha source anchor does not resolve: {anchor}"
                    )
                continue
            raise EvolutionRevalidationError(
                f"Gotcha source anchor does not resolve: {anchor}"
            )

    def _validate_gotcha_source_snapshot(
        self,
        manifest_path: Path,
        _manifest: SkillManifest,
        proposal: EvolutionProposal,
    ) -> None:
        if not manifest_path.is_file():
            raise EvolutionRevalidationError(
                "Gotcha evidence manifest changed after proposal synthesis"
            )
        payload = manifest_path.read_bytes()
        if _sha256(payload) != proposal.skill_hash:
            raise EvolutionRevalidationError(
                "Gotcha evidence manifest changed after proposal synthesis"
            )
        raw = yaml.safe_load(payload.decode("utf-8"))
        if not isinstance(raw, dict):
            raise EvolutionRevalidationError("canonical manifest must be a mapping")
        current = parse_skill_manifest(raw)
        if current.id != proposal.target_skill or current.version != proposal.skill_version:
            raise EvolutionRevalidationError(
                "Gotcha evidence identity changed after proposal synthesis"
            )
        if current.lifecycle.status not in _ROUTABLE_LIFECYCLES:
            raise EvolutionRevalidationError(
                "Gotcha target is no longer routable"
            )
        source_path = (manifest_path.parent / current.runtime.entry).resolve()
        try:
            source_path.relative_to(manifest_path.parent.resolve())
        except ValueError as exc:
            raise EvolutionRevalidationError(
                "runtime entry escapes the canonical Skill directory"
            ) from exc
        if (
            not source_path.is_file()
            or compute_execution_source_hash(
                source_path,
                skills_root=self.skills_root,
                skill_dir=manifest_path.parent,
            )
            != proposal.source_hash
        ):
            raise EvolutionRevalidationError(
                "Gotcha evidence source changed after proposal synthesis"
            )
        self._validate_gotcha_anchors_against_source(
            source_path,
            proposal.proposed_change["entry"]["anchors"],
        )

    @staticmethod
    def _validate_promoted_manifest(
        manifest: SkillManifest,
        proposal: EvolutionProposal,
    ) -> None:
        expected_ref = (
            f"evolution:{proposal.proposal_id}:events="
            + ",".join(proposal.support_event_ids)
        )
        if manifest.id != proposal.target_skill:
            raise EvolutionRevalidationError("representation changed skill identity")
        if manifest.version != proposal.skill_version:
            raise EvolutionRevalidationError("representation changed skill version")
        if manifest.validation.level != _SUPPORTED_TO_LEVEL:
            raise EvolutionRevalidationError("representation did not promote one level")
        if expected_ref not in manifest.validation.evidence:
            raise EvolutionRevalidationError("representation omitted evidence reference")

    @staticmethod
    def _validate_demoted_manifest(
        manifest: SkillManifest,
        proposal: EvolutionProposal,
    ) -> None:
        expected_ref = (
            f"evolution:{proposal.proposal_id}:demotion-events="
            + ",".join(proposal.support_event_ids)
        )
        if manifest.id != proposal.target_skill:
            raise EvolutionRevalidationError("representation changed skill identity")
        if manifest.version != proposal.skill_version:
            raise EvolutionRevalidationError("representation changed skill version")
        if manifest.validation.level != _DEMOTION_TO_LEVEL:
            raise EvolutionRevalidationError("representation did not demote one level")
        if expected_ref not in manifest.validation.evidence:
            raise EvolutionRevalidationError("representation omitted evidence reference")

    @staticmethod
    def _validate_deprecated_manifest(
        manifest: SkillManifest,
        proposal: EvolutionProposal,
    ) -> None:
        if manifest.id != proposal.target_skill:
            raise EvolutionRevalidationError("representation changed skill identity")
        if manifest.version != proposal.skill_version:
            raise EvolutionRevalidationError("representation changed skill version")
        if manifest.lifecycle.status != "deprecated":
            raise EvolutionRevalidationError("representation did not deprecate skill")
        if manifest.lifecycle.superseded_by != proposal.proposed_change["superseded_by"]:
            raise EvolutionRevalidationError(
                "representation did not bind the governed replacement"
            )

    @classmethod
    def _validate_changed_manifest(
        cls,
        manifest: SkillManifest,
        proposal: EvolutionProposal,
    ) -> None:
        if proposal.kind == "validation_promotion":
            cls._validate_promoted_manifest(manifest, proposal)
            return
        if proposal.kind == "validation_demotion":
            cls._validate_demoted_manifest(manifest, proposal)
            return
        if proposal.kind == "skill_deprecation":
            cls._validate_deprecated_manifest(manifest, proposal)
            return
        raise EvolutionRevalidationError(
            f"unsupported evolution proposal kind: {proposal.kind}"
        )

    def _validate_projected_state(self, proposal: EvolutionProposal) -> None:
        probe = OmicsRegistry()
        probe.load_all(self.skills_root)
        info = probe.skills.get(proposal.target_skill)
        if info is None:
            raise EvolutionRevalidationError(
                f"retrieval revalidation did not observe {proposal.target_skill}"
            )
        if proposal.kind == "gotcha":
            expected_lead = (
                str(proposal.proposed_change["entry"]["lead"]).rstrip(".") + "."
            )
            expected_detail = str(
                proposal.proposed_change["rendered_bullet"]
            ).removeprefix("- ")
            if expected_lead not in (info.get("gotchas") or []):
                raise EvolutionRevalidationError(
                    "retrieval revalidation did not observe the governed Gotcha"
                )
            if expected_detail not in (info.get("gotcha_details") or []):
                raise EvolutionRevalidationError(
                    "retrieval revalidation omitted Gotcha condition or guidance"
                )
            from omicsclaw.runtime.context.layers import load_skill_context

            runtime_context = load_skill_context(
                skill=proposal.target_skill,
                _registry_skills=probe.skills,
            )
            if expected_detail not in runtime_context:
                raise EvolutionRevalidationError(
                    "runtime context did not consume the governed Gotcha"
                )
            return
        if proposal.kind == "skill_deprecation":
            if (
                info.get("lifecycle_status") != "deprecated"
                or info.get("superseded_by")
                != proposal.proposed_change["superseded_by"]
            ):
                raise EvolutionRevalidationError(
                    "retrieval revalidation did not observe deprecated replacement state"
                )
            return
        expected_level = str(proposal.proposed_change["to"])
        if info.get("validation_level") != expected_level:
            raise EvolutionRevalidationError(
                f"retrieval revalidation did not observe {proposal.target_skill} "
                f"at {expected_level}"
            )

    def _validate_replacement_snapshot(self, proposal: EvolutionProposal) -> None:
        replacement_skill = str(proposal.proposed_change["superseded_by"])
        _replacement_path, replacement, replacement_hash = self._find_manifest(
            replacement_skill
        )
        if replacement.lifecycle.status not in _ROUTABLE_LIFECYCLES:
            raise EvolutionRevalidationError(
                "replacement skill is no longer routable"
            )
        if (
            replacement.validation.level
            not in GOVERNED_REPLACEMENT_VALIDATION_LEVELS
        ):
            raise EvolutionRevalidationError(
                "replacement skill is no longer demo-validated"
            )
        if (
            replacement.version != proposal.proposed_change["replacement_version"]
            or replacement_hash != proposal.proposed_change["replacement_hash"]
        ):
            raise EvolutionRevalidationError(
                "replacement skill changed after proposal synthesis"
            )

    def _capture_execution_revision(
        self,
        manifest_path: Path,
        manifest: SkillManifest,
    ) -> tuple[str, str]:
        source_path = self._runtime_entry_path(manifest_path, manifest)
        try:
            return capture_skill_execution_identity(
                source_path,
                skills_root=self.skills_root,
                skill_dir=manifest_path.parent,
            )
        except (OSError, ValueError) as exc:
            raise EvolutionRevalidationError(
                "could not capture a stable Skill execution revision"
            ) from exc

    def _capture_planned_execution_revision(
        self,
        manifest_path: Path,
        manifest: SkillManifest,
        *,
        transition_path: Path,
        before: bytes,
        after: bytes,
    ) -> tuple[str, str]:
        source_path = self._runtime_entry_path(manifest_path, manifest)
        try:
            return _capture_planned_skill_execution_identity(
                source_path,
                skills_root=self.skills_root,
                skill_dir=manifest_path.parent,
                transition_path=transition_path,
                expected_transition_payload=before,
                planned_transition_payload=after,
            )
        except (OSError, ValueError) as exc:
            raise EvolutionRevalidationError(
                "could not capture planned Skill execution authority"
            ) from exc

    @staticmethod
    def _runtime_entry_path(
        manifest_path: Path,
        manifest: SkillManifest,
    ) -> Path:
        entry = Path(manifest.runtime.entry)
        if entry.is_absolute():
            raise EvolutionRevalidationError(
                "runtime entry escapes the canonical Skill directory"
            )
        source_path = Path(os.path.abspath(manifest_path.parent / entry))
        try:
            source_path.relative_to(manifest_path.parent.resolve())
        except ValueError as exc:
            raise EvolutionRevalidationError(
                "runtime entry escapes the canonical Skill directory"
            ) from exc
        return source_path

    def _snapshot_projection_files(self) -> dict[str, bytes | None]:
        return {
            name: (
                (self.skills_root / name).read_bytes()
                if (self.skills_root / name).exists()
                else None
            )
            for name in ("catalog.json", "skill_dag.json")
        }

    def _refresh_reconciled_state(self, proposal: EvolutionProposal) -> None:
        if proposal.kind == "gotcha":
            self._reload_runtime_registry_if_owned()
            return
        self.projection_adapter.rebuild(self.skills_root)

    def _restore_projection_files(self, snapshot: dict[str, bytes | None]) -> None:
        deleted_parents: set[Path] = set()
        for name, payload in snapshot.items():
            path = self.skills_root / name
            if payload is None:
                if path.exists():
                    path.unlink()
                    deleted_parents.add(path.parent)
            else:
                _atomic_projection_write(path, payload)
        for parent in sorted(deleted_parents):
            _fsync_directory(parent)

    def _reload_runtime_registry_if_owned(self) -> None:
        from .registry import SKILLS_DIR, registry

        if self.skills_root.resolve() == SKILLS_DIR.resolve():
            registry.reload(self.skills_root)


def default_skill_evolution_governance(
    skills_root: str | Path | None = None,
) -> SkillEvolutionGovernance:
    from .registry import SKILLS_DIR

    return SkillEvolutionGovernance(
        skills_root=skills_root or SKILLS_DIR,
        ledger=default_skill_health_ledger(),
        proposals=default_evolution_proposal_store(),
    )


__all__ = [
    "EvolutionExecutionAdapter",
    "EvolutionProjectionAdapter",
    "EvolutionRecoveryJournal",
    "EvolutionRevalidationError",
    "RegistryProjectionAdapter",
    "SharedRunnerEvolutionExecutionAdapter",
    "SkillEvolutionGovernance",
    "default_skill_evolution_governance",
]
