"""OmicsClaw Skill Registry.

Centralises skill definition, discovery, and loading across all omics domains.
"""

from __future__ import annotations

import copy
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Any, Mapping
from weakref import WeakSet

from .lazy_metadata import LazySkillMetadata

logger = logging.getLogger(__name__)

# One process-local publication lock serializes first load, explicit reload,
# lightweight refresh, invalidation, and composite reads. Simple state readers
# remain lock-free: they observe either the prior state with deeply immutable
# execution-authority metadata or the fully validated replacement assigned by
# one atomic swap. ``LazySkillMetadata`` cache objects are discovery helpers,
# not execution authority, and are intentionally kept outside snapshots.
_REGISTRY_PUBLICATION_LOCK = RLock()


def _deep_freeze_registry_value(value: Any) -> Any:
    """Clone nested authority metadata into standard read-only containers."""
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                key: _deep_freeze_registry_value(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, list):
        return tuple(_deep_freeze_registry_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_deep_freeze_registry_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_deep_freeze_registry_value(item) for item in value)
    return value


def _resolve_omicsclaw_dir() -> Path:
    override = str(os.getenv("OMICSCLAW_DIR", "") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


# Base directories
OMICSCLAW_DIR = _resolve_omicsclaw_dir()
SKILLS_DIR = OMICSCLAW_DIR / "skills"
ROUTABLE_LIFECYCLE_STATUSES = frozenset({"mvp", "stable"})
GOVERNED_REPLACEMENT_VALIDATION_LEVELS = frozenset(
    {"demo-validated", "fixture-validated", "benchmarked", "production"}
)


def is_skill_automatically_routable(skill_info: Mapping[str, Any]) -> bool:
    """Return whether governance permits automatic selection of this Skill."""
    return (
        str(skill_info.get("lifecycle_status") or "mvp")
        in ROUTABLE_LIFECYCLE_STATUSES
    )


def is_skill_governed_replacement(skill_info: Mapping[str, Any]) -> bool:
    """Return whether a Skill is mature enough to replace a deprecated one."""
    return is_skill_automatically_routable(skill_info) and str(
        skill_info.get("validation_level") or "smoke-only"
    ) in GOVERNED_REPLACEMENT_VALIDATION_LEVELS


def governed_skill_replacement(
    registry: "OmicsRegistry | RegistrySnapshot",
    skill_info: Mapping[str, Any],
) -> tuple[str, Mapping[str, Any]] | None:
    """Resolve a deprecated Skill's canonical, currently routable replacement."""
    if str(skill_info.get("lifecycle_status") or "mvp") != "deprecated":
        return None
    replacement = str(skill_info.get("superseded_by") or "").strip()
    target = registry.skills.get(replacement)
    if not replacement or target is None or not is_skill_governed_replacement(target):
        return None
    canonical = str(target.get("alias") or replacement)
    if canonical == str(skill_info.get("alias") or ""):
        return None
    canonical_info = registry.skills.get(canonical)
    if canonical_info is None or not is_skill_governed_replacement(canonical_info):
        return None
    return canonical, canonical_info


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class _RegistryState:
    """One publishable in-memory registry snapshot."""

    skills: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    canonical_aliases: list[str] | tuple[str, ...] = field(default_factory=list)
    domains: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    loaded: bool = False
    loaded_dir: Path | None = None
    lazy_skills: Mapping[str, LazySkillMetadata] = field(default_factory=dict)
    lazy_skills_by_path: Mapping[Path, LazySkillMetadata] = field(default_factory=dict)
    skill_manifest_revisions: Mapping[str, str] = field(default_factory=dict)


# A publication is trusted only when the exact state object was produced by
# ``_freeze_registry_state``.  Identity registration keeps the hot snapshot
# check O(1), while a dataclasses.replace() copy is deliberately unregistered
# and therefore cannot carry publication authority into mutable nested values.
_PUBLISHED_REGISTRY_STATES: WeakSet[_RegistryState] = WeakSet()


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    """Read view bound to one atomically published Registry state.

    The contained mappings are recursively immutable just like the published
    state they reference. Holding this value prevents both in-place contract
    mutation and following ``OmicsRegistry._state`` across a concurrent reload.
    """

    skills: Mapping[str, Mapping[str, Any]]
    canonical_aliases: tuple[str, ...]
    domains: Mapping[str, Mapping[str, Any]]
    loaded_dir: Path | None
    skill_manifest_revisions: Mapping[str, str]
    _state: _RegistryState = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        """Reject snapshots whose public view was rebound from its publication."""
        state = self._state
        if (
            not _registry_state_is_frozen(state)
            or self.skills is not state.skills
            or self.canonical_aliases is not state.canonical_aliases
            or self.domains is not state.domains
            or self.loaded_dir is not state.loaded_dir
            or self.skill_manifest_revisions is not state.skill_manifest_revisions
        ):
            raise ValueError(
                "RegistrySnapshot fields must remain bound to one published Registry state"
            )

    def canonical_skill_aliases(self) -> list[str]:
        """Return routable canonical aliases from this exact snapshot."""
        return [
            alias
            for alias in self.canonical_aliases
            if alias in self.skills
            and is_skill_automatically_routable(self.skills[alias])
        ]

    def iter_primary_skills(
        self,
        domain: str | None = None,
    ) -> list[tuple[str, Mapping[str, Any]]]:
        """Return canonical entries from this exact snapshot."""
        return [
            (alias, self.skills[alias])
            for alias in self.canonical_aliases
            if alias in self.skills
            and (domain is None or self.skills[alias].get("domain") == domain)
        ]

    def skill_revision(self, skill: str) -> dict[str, str]:
        """Capture the manifest/source revision for one canonical Skill."""
        info = self.skills.get(skill)
        if info is None:
            raise KeyError(f"unknown skill: {skill}")
        canonical = str(info.get("alias") or skill)
        if canonical != skill:
            raise ValueError(f"skill revision requires canonical id: {skill!r}")
        if self.loaded_dir is None:
            raise RuntimeError("loaded Skill Registry has no canonical skills root")
        script_path = Path(info["script"]).expanduser()
        loaded_manifest_hash = self.skill_manifest_revisions.get(canonical)
        requires_bound_manifest = loaded_manifest_hash not in (None, "unknown")
        directory_name = str(info.get("directory_name") or "").strip()
        skill_dir: Path | None = None
        if requires_bound_manifest and directory_name:
            root = self.loaded_dir.resolve()
            for ancestor in (script_path.parent, *script_path.parent.parents):
                if ancestor == root:
                    break
                try:
                    ancestor.relative_to(root)
                except ValueError:
                    break
                if ancestor.name == directory_name:
                    skill_dir = ancestor
                    break
            if skill_dir is None:
                raise ValueError(
                    "runtime entry does not resolve to the frozen Registry Skill "
                    "directory"
                )
        from .evolution import capture_skill_execution_identity

        manifest_hash, source_hash = capture_skill_execution_identity(
            script_path,
            skills_root=self.loaded_dir,
            skill_dir=skill_dir,
        )
        if (
            loaded_manifest_hash is not None
            and manifest_hash != loaded_manifest_hash
        ):
            raise RuntimeError(
                f"skill manifest changed after Registry load for {canonical!r}; "
                "reload the Registry before planning or execution"
            )
        return {
            "skill_id": canonical,
            "skill_version": str(info.get("version") or "unknown"),
            "manifest_hash": manifest_hash,
            "source_hash": source_hash,
        }

    def skill_revisions(self, skills: list[str]) -> dict[str, dict[str, str]]:
        """Capture deterministic revisions for selected canonical Skills."""
        return {
            skill: self.skill_revision(skill)
            for skill in sorted(dict.fromkeys(skills))
        }

    def graph_revision(
        self,
        skills: list[str],
        *,
        method_bindings: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Capture selected compatibility/review authority for a plan."""
        if self.loaded_dir is None:
            raise RuntimeError("loaded Skill Registry has no canonical skills root")
        from .skill_dag import candidate_graph_revision

        return candidate_graph_revision(
            self,
            skills_root=self.loaded_dir,
            skills=skills,
            method_bindings=method_bindings,
        )


def _empty_registry_state() -> _RegistryState:
    return _freeze_registry_state(_empty_registry_builder_state())


def _empty_registry_builder_state() -> _RegistryState:
    """Return one private mutable state used only during candidate assembly."""
    return _RegistryState(domains=copy.deepcopy(_HARDCODED_DOMAINS))


def _registry_value_is_frozen(value: Any) -> bool:
    """Prove recursive container immutability for a published value."""
    if isinstance(value, MappingProxyType):
        return all(
            _registry_value_is_frozen(key) and _registry_value_is_frozen(item)
            for key, item in value.items()
        )
    if isinstance(value, tuple):
        return all(_registry_value_is_frozen(item) for item in value)
    if isinstance(value, frozenset):
        return all(_registry_value_is_frozen(item) for item in value)
    return not isinstance(value, (dict, list, set))


def _registry_state_is_frozen(state: _RegistryState) -> bool:
    with _REGISTRY_PUBLICATION_LOCK:
        return state in _PUBLISHED_REGISTRY_STATES


def _freeze_registry_state(state: _RegistryState) -> _RegistryState:
    """Return one deeply immutable publication without mutating the builder."""
    with _REGISTRY_PUBLICATION_LOCK:
        if state in _PUBLISHED_REGISTRY_STATES:
            return state
        published = replace(
            state,
            skills=_deep_freeze_registry_value(state.skills),
            canonical_aliases=_deep_freeze_registry_value(state.canonical_aliases),
            domains=_deep_freeze_registry_value(state.domains),
            lazy_skills=_deep_freeze_registry_value(state.lazy_skills),
            lazy_skills_by_path=_deep_freeze_registry_value(
                state.lazy_skills_by_path
            ),
            skill_manifest_revisions=_deep_freeze_registry_value(
                state.skill_manifest_revisions
            ),
        )
        if not (
            _registry_value_is_frozen(published.skills)
            and _registry_value_is_frozen(published.canonical_aliases)
            and _registry_value_is_frozen(published.domains)
            and isinstance(published.lazy_skills, MappingProxyType)
            and isinstance(published.lazy_skills_by_path, MappingProxyType)
            and _registry_value_is_frozen(published.skill_manifest_revisions)
        ):
            raise AssertionError("Registry publication is not deeply immutable")
        _PUBLISHED_REGISTRY_STATES.add(published)
        return published


class OmicsRegistry:
    """Manages skill definitions and dynamic discovery."""

    def __init__(self):
        self._state = _empty_registry_state()
        self._dag_cache_state: _RegistryState | None = None
        self._dag_cache_value: dict[str, Any] | None = None
        self._dag_cache_reviews_hash: str | None = None

    def snapshot(self, skills_dir: Path | None = None) -> RegistrySnapshot:
        """Return one coherent loaded state for a complete caller operation."""
        with _REGISTRY_PUBLICATION_LOCK:
            if skills_dir is None and self._loaded:
                # Bind the registry's currently published root.  In particular,
                # injected/test registries must not be silently replaced by the
                # process-default inventory merely because the caller asks for
                # a snapshot without selecting a different root.
                pass
            else:
                target_dir = (skills_dir or SKILLS_DIR).resolve()
                if not self._loaded or self._loaded_dir != target_dir:
                    self.load_all(target_dir)
            state = self._state
            if state.loaded and not _registry_state_is_frozen(state):
                # Test/embedding registries may assemble a private state
                # directly. The first authority snapshot is their publication
                # boundary and must gain the same deep immutability as load_all.
                state = _freeze_registry_state(state)
                self._state = state
            return RegistrySnapshot(
                skills=state.skills,
                canonical_aliases=state.canonical_aliases,
                domains=state.domains,
                loaded_dir=state.loaded_dir,
                skill_manifest_revisions=state.skill_manifest_revisions,
                _state=state,
            )

    @property
    def skills(self) -> Mapping[str, Mapping[str, Any]]:
        return self._state.skills

    @skills.setter
    def skills(self, value: dict[str, dict[str, Any]]) -> None:
        if self._state.loaded:
            raise TypeError("cannot replace skills on a published Registry")
        self._state = replace(self._state, skills=value)

    @property
    def canonical_aliases(self) -> list[str] | tuple[str, ...]:
        # Canonical skill aliases in registration order (excludes legacy aliases
        # and directory-name lookup keys). Used to keep the ``omicsclaw`` tool's
        # ``skill`` enum compact — legacy aliases still resolve via ``self.skills``
        # but need not bloat the schema sent to the LLM every turn.
        return self._state.canonical_aliases

    @canonical_aliases.setter
    def canonical_aliases(self, value: list[str]) -> None:
        if self._state.loaded:
            raise TypeError("cannot replace aliases on a published Registry")
        self._state = replace(self._state, canonical_aliases=value)

    @property
    def domains(self) -> Mapping[str, Mapping[str, Any]]:
        return self._state.domains

    @domains.setter
    def domains(self, value: dict[str, dict[str, Any]]) -> None:
        if self._state.loaded:
            raise TypeError("cannot replace domains on a published Registry")
        self._state = replace(self._state, domains=value)

    @property
    def _loaded(self) -> bool:
        return self._state.loaded

    @_loaded.setter
    def _loaded(self, value: bool) -> None:
        self._state = replace(self._state, loaded=value)

    @property
    def _loaded_dir(self) -> Path | None:
        return self._state.loaded_dir

    @_loaded_dir.setter
    def _loaded_dir(self, value: Path | None) -> None:
        self._state = replace(self._state, loaded_dir=value)

    @property
    def lazy_skills(self) -> Mapping[str, LazySkillMetadata]:
        return self._state.lazy_skills

    @lazy_skills.setter
    def lazy_skills(self, value: dict[str, LazySkillMetadata]) -> None:
        if self._state.loaded:
            raise TypeError("cannot replace lazy metadata on a published Registry")
        self._state = replace(self._state, lazy_skills=value)

    @property
    def _lazy_skills_by_path(self) -> Mapping[Path, LazySkillMetadata]:
        return self._state.lazy_skills_by_path

    @_lazy_skills_by_path.setter
    def _lazy_skills_by_path(
        self,
        value: dict[Path, LazySkillMetadata],
    ) -> None:
        if self._state.loaded:
            raise TypeError("cannot replace lazy metadata on a published Registry")
        self._state = replace(self._state, lazy_skills_by_path=value)

    @property
    def _skill_dag_cache(self) -> dict[str, Any] | None:
        if self._dag_cache_state is not self._state:
            return None
        return copy.deepcopy(self._dag_cache_value)

    @_skill_dag_cache.setter
    def _skill_dag_cache(self, value: dict[str, Any] | None) -> None:
        if value is None:
            self._dag_cache_state = None
            self._dag_cache_value = None
            self._dag_cache_reviews_hash = None
            return
        self._dag_cache_state = self._state
        self._dag_cache_value = copy.deepcopy(value)
        # This compatibility setter cannot prove which review authority built
        # the injected graph, so public queries must rebuild before using it.
        self._dag_cache_reviews_hash = None

    def canonical_skill_aliases(self) -> list[str]:
        """Routable canonical aliases (no legacy aliases), in registration order.

        Legacy aliases remain resolvable via ``self.skills`` — they are simply
        omitted from the LLM-facing ``omicsclaw`` tool enum, which is sent on
        every turn, to keep that schema compact (ADR 0024 frozen tool list).
        Draft and deprecated entries remain inspectable through the registry
        and catalog but are not offered to automatic agent execution.
        """
        state = self._state
        return [
            alias
            for alias in state.canonical_aliases
            if alias in state.skills
            and is_skill_automatically_routable(state.skills[alias])
        ]

    @staticmethod
    def _path_name_sort_key(path: Path) -> tuple[str, str]:
        """Return one cross-platform deterministic directory-order key."""
        return path.name.casefold(), path.name

    @staticmethod
    def _relative_path_sort_key(path: Path, root: Path) -> tuple[str, str]:
        """Return one deterministic recursive-discovery order key."""
        relative = path.relative_to(root).as_posix()
        return relative.casefold(), relative

    @classmethod
    def _sorted_children(cls, parent: Path) -> list[Path]:
        """Return filesystem children independent of enumeration order."""
        return sorted(parent.iterdir(), key=cls._path_name_sort_key)

    @classmethod
    def _top_level_python_files(cls, skill_path: Path) -> list[Path]:
        """Return runnable top-level Python files in a skill directory."""
        return sorted(
            (
                path
                for path in skill_path.glob("*.py")
                if path.name != "__init__.py" and not path.name.startswith("test_")
            ),
            key=cls._path_name_sort_key,
        )

    @classmethod
    def _looks_like_skill_dir(cls, skill_path: Path) -> bool:
        """Heuristically decide whether a directory is a skill directory."""
        if (skill_path / "SKILL.md").exists():
            return True

        # v2 skills carry a skill.yaml machine contract (ADR 0037).
        if (skill_path / "skill.yaml").exists():
            return True

        expected = skill_path / f"{skill_path.name.replace('-', '_')}.py"
        if expected.exists():
            return True

        return len(cls._top_level_python_files(skill_path)) == 1

    @classmethod
    def _is_enabled_skill_dir(cls, skill_path: Path) -> bool:
        try:
            from omicsclaw.extensions import load_extension_state

            return load_extension_state(skill_path).enabled
        except Exception:
            return True

    @classmethod
    def _iter_skill_dirs(cls, domain_path: Path, disabled_sink: list | None = None):
        """Yield skill directories, handling optional subdomain nesting.

        Supports both flat layouts (spatial/spatial-preprocess/) and nested
        layouts with a subdomain tier (singlecell/scrna/sc-qc/).  A child
        directory is treated as a skill if it contains a matching
        ``<dir_name>.py`` script or a ``SKILL.md``.  Otherwise it is assumed
        to be a subdomain container and scanned one level deeper.

        ``disabled_sink``, when given, collects skill-shaped directories that
        were skipped only because they are disabled, so a caller can tell
        "found skills, all disabled" apart from "nothing skill-shaped here".
        """
        for child in cls._sorted_children(domain_path):
            if not child.is_dir() or child.name.startswith(('.', '__', '_')):
                continue
            if domain_path.name != "orchestrator" and child.name == "orchestrator":
                continue
            if not cls._is_enabled_skill_dir(child):
                if disabled_sink is not None and cls._looks_like_skill_dir(child):
                    disabled_sink.append(child)
                continue

            if cls._looks_like_skill_dir(child):
                yield child
            else:
                # Subdomain container (e.g., scrna/, scatac/, multiome/)
                for grandchild in cls._sorted_children(child):
                    if not grandchild.is_dir() or grandchild.name.startswith(('.', '__', '_')):
                        continue
                    if domain_path.name != "orchestrator" and grandchild.name == "orchestrator":
                        continue
                    if not cls._is_enabled_skill_dir(grandchild):
                        if disabled_sink is not None and cls._looks_like_skill_dir(grandchild):
                            disabled_sink.append(grandchild)
                        continue
                    if cls._looks_like_skill_dir(grandchild):
                        yield grandchild

    @classmethod
    def _resolve_script_path(
        cls,
        skill_path: Path,
        lazy: LazySkillMetadata | None = None,
    ) -> Path | None:
        """Resolve the runnable script for a skill directory."""
        if lazy and lazy.script:
            declared = skill_path / lazy.script
            return declared if declared.is_file() else None

        expected = skill_path / f"{skill_path.name.replace('-', '_')}.py"
        if expected.is_file():
            return expected

        py_files = cls._top_level_python_files(skill_path)
        if len(py_files) == 1:
            return py_files[0]

        return None

    @staticmethod
    def _unique_strings(values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            text = str(value).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result

    @classmethod
    def _declared_manifest_skill_dirs(cls, skills_dir: Path) -> set[Path]:
        """Return every enabled, non-internal directory declaring a Skill."""
        declared: set[Path] = set()
        for filename in ("skill.yaml", "SKILL.md"):
            manifest_paths = sorted(
                skills_dir.rglob(filename),
                key=lambda path: cls._relative_path_sort_key(path, skills_dir),
            )
            for manifest_path in manifest_paths:
                skill_path = manifest_path.parent
                relative_parts = skill_path.relative_to(skills_dir).parts
                if any(part.startswith((".", "__", "_")) for part in relative_parts):
                    continue
                if (
                    relative_parts
                    and relative_parts[0] != "orchestrator"
                    and "orchestrator" in relative_parts[1:]
                ):
                    continue
                if cls._is_enabled_skill_dir(skill_path):
                    declared.add(skill_path.resolve())
        return declared

    def _register_skill_entry(
        self,
        canonical_alias: str,
        info: dict[str, Any],
        *,
        skill_lookup_key: str,
    ) -> None:
        """Register a primary skill entry plus all supported lookup aliases.

        Each alias key gets its own deep-copied snapshot of ``info`` so that a
        future caller mutating one alias view (e.g. ``info["allowed_extra_flags"]
        .add(...)``) does not silently corrupt the canonical or sibling views.
        """
        if canonical_alias in self.skills:
            existing = self.skills[canonical_alias]
            raise ValueError(
                f"duplicate registry identity {canonical_alias!r}: "
                f"{existing.get('script')} and {info.get('script')}"
            )
        self.skills[canonical_alias] = copy.deepcopy(info)
        if canonical_alias not in self.canonical_aliases:
            self.canonical_aliases.append(canonical_alias)

        lookup_keys: list[str] = list(info.get("legacy_aliases", []))
        # The directory name is also a valid lookup key (e.g. ``oc run
        # sc-preprocessing`` resolves to the SKILL.md at skills/.../sc-preprocessing).
        lookup_keys.append(skill_lookup_key)

        for key in self._unique_strings(lookup_keys):
            if key == canonical_alias:
                continue
            if key in self.skills:
                existing = self.skills[key]
                existing_alias = str(existing.get("alias") or "")
                if (
                    str(info.get("lifecycle_status") or "") == "deprecated"
                    and str(info.get("superseded_by") or "") == existing_alias
                ):
                    # A deprecated Skill may hand its old lookup spelling to
                    # its governed replacement. Keep the live target.
                    continue
                if (
                    str(existing.get("lifecycle_status") or "") == "deprecated"
                    and str(existing.get("superseded_by") or "") == canonical_alias
                ):
                    self.skills[key] = copy.deepcopy(info)
                    continue
                raise ValueError(
                    f"duplicate registry alias {key!r}: "
                    f"{existing_alias} and {canonical_alias}"
                )
            self.skills[key] = copy.deepcopy(info)

    def _validate_lifecycle_links(self) -> None:
        """Fail closed when a deprecated entry lacks one canonical live target."""
        for alias in self.canonical_aliases:
            info = self.skills[alias]
            if str(info.get("lifecycle_status") or "mvp") != "deprecated":
                continue
            replacement = str(info.get("superseded_by") or "").strip()
            target = self.skills.get(replacement)
            if (
                target is None
                or str(target.get("alias") or replacement) != replacement
                or not is_skill_governed_replacement(target)
            ):
                raise ValueError(
                    f"deprecated skill '{alias}' requires canonical demo-validated "
                    f"and routable "
                    f"replacement '{replacement}'"
                )

    def _validate_candidate_inventory(
        self,
        target_dir: Path,
        registered_manifest_dirs: set[Path],
        *,
        found_disabled_skills: bool = False,
    ) -> None:
        """Validate one private candidate before it can be published."""
        # An empty result is only suspicious when nothing skill-shaped was
        # ever discovered. A directory whose only skill(s) were intentionally
        # disabled via extension state is a legitimate empty registry, not a
        # broken scan.
        if not self.canonical_aliases and not found_disabled_skills:
            raise ValueError(f"skills inventory is empty: {target_dir}")

        declared_manifest_dirs = self._declared_manifest_skill_dirs(target_dir)
        missing_manifest_dirs = declared_manifest_dirs - registered_manifest_dirs
        stale_manifest_dirs = registered_manifest_dirs - declared_manifest_dirs
        if missing_manifest_dirs or stale_manifest_dirs:
            missing = ", ".join(
                str(path.relative_to(target_dir))
                for path in sorted(missing_manifest_dirs)
            )
            stale = ", ".join(
                str(path.relative_to(target_dir))
                for path in sorted(stale_manifest_dirs)
            )
            details = "; ".join(
                part
                for part in (
                    f"missing={missing}" if missing else "",
                    f"stale={stale}" if stale else "",
                )
                if part
            )
            raise ValueError(f"manifest inventory is incomplete: {details}")

        unresolved = [
            alias
            for alias in self.canonical_aliases
            if not Path(self.skills[alias]["script"]).is_file()
        ]
        if unresolved:
            raise ValueError(
                "skill inventory has unresolved runnable entries: "
                + ", ".join(sorted(unresolved))
            )
        self._validate_lifecycle_links()

    def load_all(self, skills_dir: Path | None = None) -> None:
        """Atomically publish a fully validated filesystem registry snapshot.

        Each skill directory is expected to contain a ``skill.yaml`` (ADR 0037)
        — the single machine-contract source of truth for the skill's metadata
        — plus a generated ``SKILL.md`` card. Metadata is read via the dual-track
        ``LazySkillMetadata`` (v2 ``skill.yaml`` preferred; legacy
        ``metadata.omicsclaw`` frontmatter / ``parameters.yaml`` still accepted).
        A skill directory without a readable ``SKILL.md`` description gets a
        minimal dynamic entry whose name comes from the directory; all per-skill
        metadata (aliases, allowed flags, saves_h5ad, etc.) lives in ``skill.yaml``.

        ``skills_dir`` is part of the cache key — re-calling with a
        different directory triggers a fresh scan instead of silently
        returning the previous snapshot. Pass ``None`` to use the
        repo-default ``SKILLS_DIR``. Initial load and root changes use the same
        private candidate builder as :meth:`reload`; no discovered entry is
        exposed through this instance before validation succeeds.
        """
        target_dir = (skills_dir or SKILLS_DIR).resolve()
        with _REGISTRY_PUBLICATION_LOCK:
            if self._loaded and self._loaded_dir == target_dir:
                return
            self._state = self._build_candidate_state(target_dir)

    def _build_candidate_state(self, target_dir: Path) -> _RegistryState:
        """Build one private complete snapshot without publishing it."""
        candidate = type(self)()
        candidate._state = _empty_registry_builder_state()
        candidate._load_all_candidate(target_dir)
        return _freeze_registry_state(candidate._state)

    def _load_all_candidate(self, target_dir: Path) -> None:
        """Populate and validate a private, freshly constructed registry."""

        if not target_dir.exists():
            raise FileNotFoundError(f"skills root does not exist: {target_dir}")
        if not target_dir.is_dir():
            raise NotADirectoryError(f"skills root is not a directory: {target_dir}")

        # Always parse the candidate's own fresh metadata. A caller may have
        # explicitly used ``load_lightweight`` earlier, but that snapshot must
        # not make a subsequent full load reuse stale on-disk metadata.
        lazy_by_path = self._discover_lazy_skills(target_dir)
        self._lazy_skills_by_path = lazy_by_path
        self.lazy_skills = self._build_public_lazy_index(target_dir, lazy_by_path)

        registered_manifest_dirs: set[Path] = set()
        disabled_skill_dirs: list[Path] = []

        # Scan domain directories
        for domain_path in self._sorted_children(target_dir):
            if not domain_path.is_dir() or domain_path.name.startswith(('.', '__', '_')):
                continue

            domain_name = domain_path.name

            candidate_skill_dirs = []
            if self._looks_like_skill_dir(domain_path):
                candidate_skill_dirs.append(domain_path)
            candidate_skill_dirs.extend(
                self._iter_skill_dirs(domain_path, disabled_sink=disabled_skill_dirs)
            )
            candidate_skill_dirs.sort(
                key=lambda path: self._relative_path_sort_key(path, domain_path)
            )

            # Scan skill directories (handles subdomain nesting)
            for skill_path in candidate_skill_dirs:
                skill_dir_name = skill_path.name
                lazy = self._lazy_skills_by_path.get(skill_path.resolve())

                script_path_candidate = self._resolve_script_path(skill_path, lazy=lazy)
                if script_path_candidate is None:
                    raise ValueError(
                        f"skill inventory entry {skill_path} has no runnable Python "
                        "entry or declared non-Python runtime entry"
                    )

                canonical_alias = (
                    (lazy.name if lazy and lazy.name else "")
                    or skill_dir_name
                )

                # Build skill_info from SKILL.md metadata (single source of truth)
                if lazy and lazy.description:
                    md_info: dict[str, Any] = {
                        "domain": lazy.domain or domain_name,
                        "alias": canonical_alias,
                        "canonical_name": canonical_alias,
                        "directory_name": skill_dir_name,
                        "script": script_path_candidate,
                        "runtime_language": lazy.runtime_language,
                        "source": lazy.source,  # "v2" (skill.yaml) | "v1" (ADR 0037)
                        "type": lazy.type,
                        "version": lazy.version,
                        "validation_level": lazy.validation_level,
                        "origin": lazy.origin,
                        "lifecycle_status": lazy.lifecycle_status,
                        "superseded_by": lazy.superseded_by,
                        "skip_when": lazy.skip_when,
                        # Consensus shims forward to the shared run parser, which
                        # has no `--demo`; declare no demo so `oc run <cs> --demo`
                        # is refused rather than aborting in argparse (ADR 0016/0030).
                        "demo_args": [] if lazy.type == "consensus" else ["--demo"],
                        "description": lazy.description,
                        # Reconciled Python-package surface (frontmatter
                        # `requires:`) for the adaptive env resolver.
                        "requires": lazy.requires or [],
                        "trigger_keywords": lazy.trigger_keywords or [],
                        "allowed_extra_flags": lazy.allowed_extra_flags or set(),
                        "legacy_aliases": self._unique_strings(list(lazy.legacy_aliases or [])),
                        "saves_h5ad": lazy.saves_h5ad,
                        "requires_preprocessed": lazy.requires_preprocessed,
                        "input_contract": lazy.input_contract,
                        "output_contract": lazy.output_contract,
                        "param_hints": lazy.param_hints,
                        "compute_resources": lazy.compute_resources,
                        "security_contract": lazy.security_contract,
                        "security_reviewed": lazy.security_reviewed,
                        "gotchas": lazy.gotchas,
                        "gotcha_details": lazy.gotcha_details,
                    }
                else:
                    # SKILL.md missing or has no description — minimal dynamic entry.
                    # All metadata (legacy aliases, flags, saves_h5ad) defaults to
                    # empty; supply a SKILL.md to enrich.
                    md_info = {
                        "domain": domain_name,
                        "alias": canonical_alias,
                        "canonical_name": canonical_alias,
                        "directory_name": skill_dir_name,
                        "script": script_path_candidate,
                        "runtime_language": lazy.runtime_language if lazy else "python",
                        "source": lazy.source if lazy else "v1",
                        "type": "leaf",
                        "version": "",
                        "validation_level": "smoke-only",
                        "origin": "human",
                        "lifecycle_status": "mvp",
                        "superseded_by": "",
                        "skip_when": [],
                        "demo_args": ["--demo"],
                        "description": f"Dynamically loaded {canonical_alias} skill",
                        "requires": [],
                        "trigger_keywords": [],
                        "allowed_extra_flags": set(),
                        "legacy_aliases": [],
                        "saves_h5ad": False,
                        "requires_preprocessed": False,
                        "input_contract": {},
                        "output_contract": {},
                        "param_hints": {},
                        "compute_resources": {},
                        "security_contract": {},
                        "security_reviewed": False,
                        "gotchas": [],
                        "gotcha_details": [],
                    }

                self._register_skill_entry(
                    canonical_alias,
                    md_info,
                    skill_lookup_key=self._lazy_public_key(
                        target_dir,
                        skill_path.resolve(),
                        self._lazy_skills_by_path,
                    ),
                )
                self._state.skill_manifest_revisions[canonical_alias] = (
                    lazy.manifest_revision if lazy is not None else "unknown"
                )
                if (skill_path / "skill.yaml").exists() or (skill_path / "SKILL.md").exists():
                    registered_manifest_dirs.add(skill_path.resolve())

        self._validate_candidate_inventory(
            target_dir,
            registered_manifest_dirs,
            found_disabled_skills=bool(disabled_skill_dirs),
        )
        self._refresh_domain_skill_counts()
        self._loaded = True
        self._loaded_dir = target_dir

    @classmethod
    def _discover_lazy_skills(
        cls,
        target_dir: Path,
    ) -> dict[Path, LazySkillMetadata]:
        """Build a fresh index keyed by the exact root-relative Skill path."""
        # The shipped 95/95 v2 tree is authoritative and must fail closed on
        # an invalid present manifest. Alternate roots retain the dual-track
        # compatibility behavior for external/legacy skill collections.
        strict_v2 = target_dir == SKILLS_DIR.resolve()
        discovered: dict[Path, LazySkillMetadata] = {}

        for domain_path in cls._sorted_children(target_dir):
            if not domain_path.is_dir() or domain_path.name.startswith(('.', '__', '_')):
                continue

            candidate_skill_dirs = []
            if cls._looks_like_skill_dir(domain_path):
                candidate_skill_dirs.append(domain_path)
            candidate_skill_dirs.extend(cls._iter_skill_dirs(domain_path))
            candidate_skill_dirs.sort(
                key=lambda path: cls._relative_path_sort_key(path, domain_path)
            )

            for skill_path in candidate_skill_dirs:
                # v1 carries metadata in SKILL.md; v2 in skill.yaml (ADR 0037).
                # Accept either so a v2 skill is registered with full metadata.
                if not (skill_path / "SKILL.md").exists() and not (skill_path / "skill.yaml").exists():
                    continue

                lazy = LazySkillMetadata(skill_path, strict_v2=strict_v2)
                discovered[skill_path.resolve()] = lazy
        return discovered

    @classmethod
    def _build_public_lazy_index(
        cls,
        target_dir: Path,
        discovered: Mapping[Path, LazySkillMetadata],
    ) -> dict[str, LazySkillMetadata]:
        """Preserve basename lookup when unique; disambiguate collisions."""
        public: dict[str, LazySkillMetadata] = {}
        for skill_path, lazy in discovered.items():
            key = cls._lazy_public_key(target_dir, skill_path, discovered)
            public[key] = lazy
        return public

    @staticmethod
    def _lazy_public_key(
        target_dir: Path,
        skill_path: Path,
        discovered: Mapping[Path, LazySkillMetadata],
    ) -> str:
        """Return a backward-compatible unique lightweight lookup key."""
        basename_count = sum(path.name == skill_path.name for path in discovered)
        return (
            skill_path.name
            if basename_count == 1
            else skill_path.relative_to(target_dir).as_posix()
        )

    def load_lightweight(self, skills_dir: Path | None = None) -> None:
        """Atomically load basic metadata before the full registry is loaded.

        A fully loaded registry is one coherent snapshot. Replacing only its
        lightweight index (especially from another root) would mix identities;
        use :meth:`reload` to refresh all fields together instead.
        """
        target_dir = (skills_dir or SKILLS_DIR).resolve()
        with _REGISTRY_PUBLICATION_LOCK:
            if self._loaded:
                if self._loaded_dir == target_dir:
                    return
                raise RuntimeError(
                    "cannot mix lightweight metadata from another skills root "
                    "into a loaded registry; use reload"
                )
            if not target_dir.exists():
                return
            lazy_by_path = self._discover_lazy_skills(target_dir)
            self._state = _freeze_registry_state(
                replace(
                    self._state,
                    lazy_skills=self._build_public_lazy_index(target_dir, lazy_by_path),
                    lazy_skills_by_path=lazy_by_path,
                )
            )

    def _resolve_alias(self, skill_dir_name: str) -> str:
        """Map a skill directory name to its registry alias.

        Returns the canonical skill name when the directory or a legacy
        alias is known; otherwise returns ``skill_dir_name`` unchanged.
        """
        if not self._loaded:
            self.load_all()

        info = self.skills.get(skill_dir_name)
        if info:
            return str(info.get("alias", skill_dir_name))
        return skill_dir_name

    def iter_primary_skills(
        self,
        domain: str | None = None,
    ) -> list[tuple[str, Mapping[str, Any]]]:
        """Return the canonical skill entries, excluding alias pointers."""
        if not self._loaded:
            self.load_all()

        items: list[tuple[str, dict[str, Any]]] = []
        for alias, info in self.skills.items():
            if alias != info.get("alias", alias):
                continue
            if domain and info.get("domain") != domain:
                continue
            items.append((alias, info))
        return items

    def build_skill_catalog(self, domain: str | None = None) -> dict[str, str]:
        """Return a canonical skill->description catalog for a domain."""
        return {
            alias: info.get("description", "")
            for alias, info in self.iter_primary_skills(domain=domain)
        }

    def _canonical_graph_skill(self, skill: str) -> str:
        if not self._loaded:
            self.load_all()
        info = self.skills.get(skill)
        if info is None:
            raise KeyError(f"unknown skill: {skill}")
        return str(info.get("alias") or skill)

    def build_compatibility_dag(self) -> dict[str, Any]:
        """Return a DAG bound to one Registry state and exact review revision."""
        with _REGISTRY_PUBLICATION_LOCK:
            if not self._loaded:
                self.load_all()
            state = self._state
            from .skill_dag import (
                build_skill_dag,
                load_skill_dag_reviews_with_revision,
            )

            review_path = (state.loaded_dir or SKILLS_DIR) / "skill_dag_reviews.yaml"
            reviews, reviews_hash = load_skill_dag_reviews_with_revision(
                review_path
            )
            if (
                self._dag_cache_state is not state
                or self._dag_cache_value is None
                or self._dag_cache_reviews_hash != reviews_hash
            ):
                graph = build_skill_dag(
                    self,
                    reviews=reviews,
                )
                _after_reviews, after_hash = load_skill_dag_reviews_with_revision(
                    review_path
                )
                if after_hash != reviews_hash:
                    raise ValueError(
                        "skill DAG review authority changed while building the graph"
                    )
                self._dag_cache_state = state
                self._dag_cache_value = copy.deepcopy(graph)
                self._dag_cache_reviews_hash = reviews_hash
            return copy.deepcopy(self._dag_cache_value)

    def get_upstream_skills(self, skill: str) -> list[str]:
        """Return transitive candidate producers for a canonical or legacy alias."""
        from .skill_dag import upstream_closure

        with _REGISTRY_PUBLICATION_LOCK:
            return upstream_closure(
                self.build_compatibility_dag(),
                self._canonical_graph_skill(skill),
            )

    def get_downstream_skills(self, skill: str) -> list[str]:
        """Return transitive candidate consumers for a canonical or legacy alias."""
        from .skill_dag import downstream_skills

        with _REGISTRY_PUBLICATION_LOCK:
            return downstream_skills(
                self.build_compatibility_dag(),
                self._canonical_graph_skill(skill),
            )

    def topological_skill_order(self, skills: list[str] | None = None) -> list[str]:
        """Return deterministic producer-before-consumer order for selected skills."""
        from .skill_dag import topological_sort

        with _REGISTRY_PUBLICATION_LOCK:
            canonical = (
                [self._canonical_graph_skill(skill) for skill in skills]
                if skills is not None
                else None
            )
            return topological_sort(self.build_compatibility_dag(), skills=canonical)

    def build_candidate_skill_chain(
        self,
        skills: list[str],
        *,
        method_bindings: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Return a topo-ordered selected chain with bound edge provenance."""
        from .skill_dag import (
            build_candidate_chain_with_revision,
            method_binding_is_runtime_accepted,
            supports_unified_method_binding,
        )

        with _REGISTRY_PUBLICATION_LOCK:
            canonical = [self._canonical_graph_skill(skill) for skill in skills]
            canonical_bindings: dict[str, str] = {}
            for skill, method in (method_bindings or {}).items():
                canonical_skill = self._canonical_graph_skill(skill)
                if canonical_skill not in canonical:
                    raise ValueError(
                        f"method binding references an unselected skill: {canonical_skill}"
                    )
                method_name = str(method).strip()
                param_hints = self.skills[canonical_skill].get("param_hints") or {}
                if method_name not in param_hints:
                    raise ValueError(
                        f"method {method_name!r} is not declared in param_hints "
                        f"for {canonical_skill!r}"
                    )
                if not supports_unified_method_binding(
                    self.skills[canonical_skill]
                ):
                    raise ValueError(
                        f"skill {canonical_skill!r} does not expose the unified "
                        "--method flag"
                    )
                if not method_binding_is_runtime_accepted(
                    self.skills[canonical_skill],
                    method_name,
                ):
                    raise ValueError(
                        f"method {method_name!r} is not an accepted --method "
                        f"value for {canonical_skill!r}"
                    )
                canonical_bindings[canonical_skill] = method_name
            snapshot = self.snapshot()
            if snapshot.loaded_dir is None:
                raise RuntimeError("loaded Skill Registry has no canonical skills root")
            plan, graph_revision = build_candidate_chain_with_revision(
                snapshot,
                skills_root=snapshot.loaded_dir,
                skills=canonical,
                method_bindings=canonical_bindings,
            )
            plan["skill_revisions"] = snapshot.skill_revisions(plan["skills"])
            plan["plan_schema_version"] = 2
            plan["graph_revision"] = graph_revision
            return plan

    def build_keyword_map(
        self,
        domain: str | None = None,
        fallback_map: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Build keyword->skill_alias map from SKILL.md trigger_keywords.

        Args:
            domain: If provided, only include skills from this domain.
            fallback_map: Legacy hardcoded map merged underneath
                          (SKILL.md keywords take priority).

        Returns:
            Dict mapping lowercase keyword to skill alias.
        """
        with _REGISTRY_PUBLICATION_LOCK:
            if not self.lazy_skills:
                self.load_lightweight()

            keyword_map: dict[str, str] = {}

            # Start with fallback so SKILL.md keywords override
            if fallback_map:
                keyword_map.update(fallback_map)

            for skill_key, lazy in self.lazy_skills.items():
                if domain and lazy.domain != domain:
                    continue

                skill_alias = lazy.name or self._resolve_alias(skill_key)

                for kw in lazy.trigger_keywords:
                    keyword_map[kw.lower()] = skill_alias

            return keyword_map

    def invalidate(self) -> None:
        """Atomically reset the registry so the next ``load_all`` rescans disk.

        Long-running surfaces (``oc desktop-server``, interactive REPL) can call
        this after editing ``SKILL.md`` / ``parameters.yaml`` to pick up the
        change without restarting the process. Production refresh flows should
        prefer :meth:`reload` so readers retain the old valid snapshot while the
        replacement is being built.
        """
        with _REGISTRY_PUBLICATION_LOCK:
            self._state = _empty_registry_state()

    def reload(self, skills_dir: Path | None = None) -> None:
        """Build privately, validate, then atomically replace the snapshot."""
        target_dir = (skills_dir or SKILLS_DIR).resolve()
        with _REGISTRY_PUBLICATION_LOCK:
            self._state = self._build_candidate_state(target_dir)

    def _refresh_domain_skill_counts(self) -> None:
        """Update domain skill counts from loaded canonical entries."""
        counts: dict[str, int] = {
            domain: 0
            for domain in self.domains
        }
        for alias, info in self.skills.items():
            if alias != info.get("alias", alias):
                continue
            domain = str(info.get("domain", "")).strip()
            if not domain:
                continue
            counts[domain] = counts.get(domain, 0) + 1

        for domain, count in counts.items():
            if domain not in self.domains:
                self.domains[domain] = {"name": domain, "primary_data_types": []}
            self.domains[domain]["skill_count"] = count


def ensure_registry_loaded(skills_dir: Path | None = None) -> OmicsRegistry:
    """Return the shared registry after its atomic first-load barrier completes."""
    registry.load_all(skills_dir)
    return registry


# ---------------------------------------------------------------------------
# Baseline hardcoded definitions for stable legacy mapping
# ---------------------------------------------------------------------------

# ``skill_count`` is intentionally omitted — ``_refresh_domain_skill_counts``
# overwrites it from the live ``skills/`` filesystem after every ``load_all``.
# Hardcoding the count here just rotted (e.g. singlecell drifted from 14 → 30)
# and misled readers of this file.
_HARDCODED_DOMAINS = {
    "spatial": {
        "name": "Spatial Transcriptomics",
        "primary_data_types": ["h5ad", "h5", "zarr", "loom"],
        "summary": (
            "Spatial transcriptomics for Visium/Xenium/MERFISH/Slide-seq: QC, "
            "domain detection, SVG, deconvolution, cell communication, trajectories, CNV."
        ),
        "representative_skills": [
            "spatial-preprocess", "spatial-domains", "spatial-de",
            "spatial-deconv", "spatial-communication",
        ],
    },
    "singlecell": {
        "name": "Single-Cell Omics",
        "primary_data_types": ["h5ad", "h5", "loom", "mtx"],
        "summary": (
            "scRNA-seq + scATAC-seq: FASTQ→counts, QC, filter, doublet removal, "
            "normalize→HVG→PCA→UMAP→cluster, annotation, DE, trajectory, velocity, GRN, CCC."
        ),
        "representative_skills": [
            "sc-preprocessing", "sc-cell-annotation", "sc-de",
            "sc-batch-integration", "sc-pseudotime",
        ],
    },
    "genomics": {
        "name": "Genomics",
        "primary_data_types": ["vcf", "bam", "cram", "fasta", "fastq", "bed"],
        "summary": (
            "Bulk DNA-seq: FASTQ QC, alignment, SNV/indel/SV/CNV calling, VCF ops, "
            "variant annotation, phasing, de novo assembly, ATAC/ChIP peak calling."
        ),
        "representative_skills": [
            "genomics-alignment", "genomics-variant-calling",
            "genomics-variant-annotation", "genomics-sv-detection",
        ],
    },
    "proteomics": {
        "name": "Proteomics",
        "primary_data_types": ["mzml", "mzxml", "csv"],
        "summary": (
            "Mass spec proteomics: raw MS QC, peptide/protein ID, LFQ/TMT/DIA "
            "quantification, differential abundance, PTM, pathway enrichment."
        ),
        "representative_skills": [
            "proteomics-identification", "proteomics-quantification",
            "proteomics-de", "proteomics-enrichment",
        ],
    },
    "metabolomics": {
        "name": "Metabolomics",
        "primary_data_types": ["mzml", "cdf", "csv"],
        "summary": (
            "LC-MS metabolomics: XCMS preprocessing, peak detection, metabolite "
            "annotation (SIRIUS/GNPS), normalization, DE, pathway enrichment."
        ),
        "representative_skills": [
            "metabolomics-peak-detection", "metabolomics-annotation",
            "metabolomics-de", "metabolomics-pathway-enrichment",
        ],
    },
    "bulkrna": {
        "name": "Bulk RNA-seq",
        "primary_data_types": ["csv", "tsv", "fastq", "bam"],
        "summary": (
            "Bulk RNA-seq: FASTQ QC, alignment, count QC, DE (DESeq2), enrichment, "
            "splicing, WGCNA, deconvolution, PPI, survival, TrajBlend bulk-to-sc."
        ),
        "representative_skills": [
            "bulkrna-de", "bulkrna-enrichment", "bulkrna-coexpression",
            "bulkrna-deconvolution", "bulkrna-survival",
        ],
    },
    "orchestrator": {
        "name": "Orchestrator",
        "primary_data_types": ["*"],
        "summary": (
            "Meta tooling: multi-omics query routing and skill scaffolding. "
            "Not an analysis — dispatches to the right domain skill."
        ),
        "representative_skills": ["orchestrator", "omics-skill-builder"],
    },
    "literature": {
        "name": "Literature",
        "primary_data_types": ["pdf", "txt", "doi", "url"],
        "summary": (
            "Scientific literature parsing for PDFs, URLs, DOIs, PubMed IDs, "
            "GEO accession extraction, and dataset metadata handoff."
        ),
        "representative_skills": ["literature"],
    },
}


# Instantiate the global registry
registry = OmicsRegistry()
