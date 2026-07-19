"""Negative + edge-case loader tests for ``OmicsRegistry`` discovery.

These cover behaviors that don't show up in the snapshot tests:

- ``_looks_like_skill_dir`` heuristic boundaries (positive *and* negative).
- ``skills_dir`` cache key — re-loading from a different directory must
  invalidate the previous snapshot instead of silently returning it.
- ``invalidate`` / ``reload`` semantics.
- Hot-loading a synthetic fixture skill so we exercise the same paths the
  bundled 95 skills use, without depending on those snapshots.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from types import MappingProxyType

import pytest

from omicsclaw.skill.registry import OmicsRegistry, ensure_registry_loaded


def _write_skill(
    skill_dir: Path,
    *,
    skill_name: str,
    description: str = "Test skill",
    domain_in_yaml: str | None = None,
    legacy_aliases: tuple[str, ...] = (),
) -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = [
        "---",
        f"name: {skill_name}",
        f"description: {description}",
        "---",
        "",
        f"# {skill_name}",
    ]
    (skill_dir / "SKILL.md").write_text("\n".join(frontmatter), encoding="utf-8")
    sidecar_lines = [f"script: {skill_name.replace('-', '_')}.py"]
    if domain_in_yaml is not None:
        sidecar_lines.append(f"domain: {domain_in_yaml}")
    if legacy_aliases:
        sidecar_lines.append("legacy_aliases:")
        sidecar_lines.extend(f"  - {alias}" for alias in legacy_aliases)
    (skill_dir / "parameters.yaml").write_text("\n".join(sidecar_lines), encoding="utf-8")
    (skill_dir / f"{skill_name.replace('-', '_')}.py").write_text(
        "if __name__ == '__main__':\n    pass\n",
        encoding="utf-8",
    )


def _write_v2_skill(
    skill_dir: Path,
    *,
    skill_name: str,
    version: str = "1.0.0",
    memory_mib: int = 1024,
) -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    entry = f"{skill_name.replace('-', '_')}.py"
    (skill_dir / entry).write_text("if __name__ == '__main__':\n    pass\n")
    (skill_dir / "skill.yaml").write_text(
        "\n".join(
            [
                "schema_version: 2",
                f"id: {skill_name}",
                f"name: {skill_name}",
                "domain: spatial",
                f"version: {version}",
                "summary:",
                "  load_when: exercising frozen Registry authority",
                "runtime:",
                f"  entry: {entry}",
                "resources:",
                "  compute:",
                "    cpu_cores: 1",
                f"    memory_mib: {memory_mib}",
                "    gpu_devices: 0",
                "    threads: 1",
                "    temporary_disk_mib: 1024",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_looks_like_skill_dir_rejects_empty_directory(tmp_path):
    empty = tmp_path / "empty-container"
    empty.mkdir()
    assert OmicsRegistry._looks_like_skill_dir(empty) is False


def test_looks_like_skill_dir_rejects_multi_script_subdomain(tmp_path):
    """A subdomain container with multiple top-level scripts (but no SKILL.md
    and no matching ``<dir>.py``) must NOT be classified as a skill.
    """
    container = tmp_path / "scrna"
    container.mkdir()
    (container / "helper_a.py").write_text("x = 1\n", encoding="utf-8")
    (container / "helper_b.py").write_text("y = 2\n", encoding="utf-8")
    assert OmicsRegistry._looks_like_skill_dir(container) is False


def test_looks_like_skill_dir_accepts_skill_md(tmp_path):
    skill = tmp_path / "fake-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    assert OmicsRegistry._looks_like_skill_dir(skill) is True


def test_looks_like_skill_dir_accepts_single_top_level_script(tmp_path):
    """One top-level .py with no SKILL.md is still a valid layout (legacy)."""
    skill = tmp_path / "lone-script"
    skill.mkdir()
    (skill / "lone_script.py").write_text("x = 1\n", encoding="utf-8")
    assert OmicsRegistry._looks_like_skill_dir(skill) is True


def test_load_all_uses_fixture_skills_dir(tmp_path):
    """``load_all`` must respect a custom ``skills_dir`` and not fall back to
    the repo default. Verifies the fixture isolation contract."""
    fixtures = tmp_path / "skills"
    _write_skill(fixtures / "spatial" / "fake-foo", skill_name="fake-foo")

    registry = OmicsRegistry()
    registry.load_all(fixtures)

    assert "fake-foo" in registry.skills
    # No real skills leaked in.
    assert "spatial-preprocess" not in registry.skills


def test_full_load_reparses_after_an_explicit_lightweight_snapshot(tmp_path):
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "spatial" / "fake-foo"
    _write_skill(
        skill_dir,
        skill_name="fake-foo",
        description="Original description",
    )
    registry = OmicsRegistry()
    registry.load_lightweight(skills_root)
    stale_lazy = registry.lazy_skills["fake-foo"]
    assert stale_lazy.description == "Original description"

    _write_skill(
        skill_dir,
        skill_name="fake-foo",
        description="Fresh description",
    )
    registry.load_all(skills_root)

    assert registry.lazy_skills["fake-foo"] is not stale_lazy
    assert registry.skills["fake-foo"]["description"] == "Fresh description"


def test_loaded_registry_rejects_cross_root_lightweight_mix(tmp_path):
    first_root = tmp_path / "first-skills"
    second_root = tmp_path / "second-skills"
    _write_skill(
        first_root / "spatial" / "first-skill",
        skill_name="first-skill",
    )
    _write_skill(
        second_root / "genomics" / "second-skill",
        skill_name="second-skill",
    )
    registry = OmicsRegistry()
    registry.load_all(first_root)
    first_state = registry._state

    with pytest.raises(RuntimeError, match="use reload"):
        registry.load_lightweight(second_root)

    assert registry._state is first_state
    assert set(registry.skills) == {"first-skill"}
    assert set(registry.lazy_skills) == {"first-skill"}
    assert registry._loaded_dir == first_root.resolve()


def test_published_registry_is_deeply_immutable(tmp_path):
    """A frozen publication must reject every public in-place mutation.

    ``RegistrySnapshot`` used to share ordinary nested dict/list/set objects
    with ``registry.skills``.  A same-process caller could therefore weaken an
    execution contract after a plan was confirmed without changing the bound
    manifest/source identity.  The publication itself, not only the snapshot
    dataclass, must be recursively read-only.
    """
    skills_root = tmp_path / "skills"
    _write_skill(
        skills_root / "spatial" / "frozen-skill",
        skill_name="frozen-skill",
    )
    registry = OmicsRegistry()
    registry.load_all(skills_root)
    snapshot = registry.snapshot()

    live_info = registry.skills["frozen-skill"]
    frozen_info = snapshot.skills["frozen-skill"]
    assert live_info is frozen_info

    with pytest.raises(TypeError):
        registry.skills["injected"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        live_info["output_contract"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        live_info["param_hints"]["profile"] = {}  # type: ignore[index]
    with pytest.raises(AttributeError):
        live_info["trigger_keywords"].append("injected")
    with pytest.raises(AttributeError):
        live_info["allowed_extra_flags"].add("--injected")
    with pytest.raises(TypeError):
        registry.domains["spatial"]["skill_count"] = 999  # type: ignore[index]

    assert "injected" not in registry.skills
    assert frozen_info["output_contract"] == {}
    assert frozen_info["param_hints"] == {}
    assert frozen_info["trigger_keywords"] == ()
    assert frozen_info["allowed_extra_flags"] == set()
    assert registry.domains["spatial"]["skill_count"] == 1


def test_unloaded_and_invalidated_registry_cannot_poison_domain_baseline(tmp_path):
    """Even the empty publication must not expose hardcoded nested lists."""
    registry = OmicsRegistry()
    expected = tuple(registry.domains["spatial"]["representative_skills"])

    with pytest.raises(TypeError):
        registry.domains["spatial"]["name"] = "poisoned"  # type: ignore[index]
    with pytest.raises(AttributeError):
        registry.domains["spatial"]["representative_skills"].append("poisoned")

    skills_root = tmp_path / "skills"
    _write_skill(
        skills_root / "spatial" / "baseline-skill",
        skill_name="baseline-skill",
    )
    registry.load_all(skills_root)
    registry.invalidate()

    assert tuple(registry.domains["spatial"]["representative_skills"]) == expected
    with pytest.raises(AttributeError):
        registry.domains["spatial"]["representative_skills"].append("poisoned")


def test_snapshot_does_not_trust_shallow_read_only_wrappers(tmp_path):
    """Top-level proxies are not proof that nested authority is frozen."""
    registry = OmicsRegistry()
    shallow_state = replace(
        registry._state,
        skills=MappingProxyType(
            {
                "shallow-skill": {
                    "alias": "shallow-skill",
                    "script": tmp_path / "entry.py",
                    "param_hints": {"profile": {"params": []}},
                }
            }
        ),
        canonical_aliases=("shallow-skill",),
        domains=MappingProxyType({"demo": {"name": "Demo"}}),
        lazy_skills=MappingProxyType({}),
        lazy_skills_by_path=MappingProxyType({}),
        skill_manifest_revisions=MappingProxyType({}),
        loaded=True,
        loaded_dir=tmp_path,
    )
    registry._state = shallow_state

    snapshot = registry.snapshot()

    assert snapshot._state is not shallow_state
    with pytest.raises(TypeError):
        snapshot.skills["shallow-skill"]["param_hints"]["profile"] = {}  # type: ignore[index]
    with pytest.raises(AttributeError):
        snapshot.skills["shallow-skill"]["param_hints"]["profile"][
            "params"
        ].append("poisoned")


def test_snapshot_cannot_rebind_fields_away_from_its_published_state(tmp_path):
    """A copied snapshot must not retain authority with substituted fields."""
    skills_root = tmp_path / "skills"
    _write_skill(
        skills_root / "spatial" / "bound-skill",
        skill_name="bound-skill",
    )
    registry = OmicsRegistry()
    registry.load_all(skills_root)
    snapshot = registry.snapshot()

    forged_skills = MappingProxyType(
        {
            "forged-skill": MappingProxyType(
                {
                    "alias": "forged-skill",
                    "script": tmp_path / "forged.py",
                }
            )
        }
    )
    with pytest.raises(ValueError, match="published Registry state"):
        replace(snapshot, skills=forged_skills)

    equal_but_distinct_root = Path(str(snapshot.loaded_dir))
    assert equal_but_distinct_root == snapshot.loaded_dir
    assert equal_but_distinct_root is not snapshot.loaded_dir
    with pytest.raises(ValueError, match="published Registry state"):
        replace(snapshot, loaded_dir=equal_but_distinct_root)

def test_load_all_with_different_skills_dir_invalidates_previous_snapshot(tmp_path):
    """Re-calling ``load_all`` with a different directory must rescan, not
    silently return the previous snapshot."""
    fixtures_a = tmp_path / "skills_a"
    fixtures_b = tmp_path / "skills_b"
    _write_skill(fixtures_a / "spatial" / "fake-a", skill_name="fake-a")
    _write_skill(fixtures_b / "spatial" / "fake-b", skill_name="fake-b")

    registry = OmicsRegistry()
    registry.load_all(fixtures_a)
    assert "fake-a" in registry.skills
    assert "fake-b" not in registry.skills

    registry.load_all(fixtures_b)
    assert "fake-b" in registry.skills
    assert "fake-a" not in registry.skills


def test_invalidate_and_reload_round_trip(tmp_path):
    fixtures = tmp_path / "skills"
    _write_skill(fixtures / "spatial" / "fake-x", skill_name="fake-x")

    registry = OmicsRegistry()
    registry.load_all(fixtures)
    assert "fake-x" in registry.skills

    # Add a second skill on disk; without invalidate the cache is sticky.
    _write_skill(fixtures / "spatial" / "fake-y", skill_name="fake-y")
    registry.load_all(fixtures)
    assert "fake-y" not in registry.skills, "snapshot should be sticky until invalidated"

    registry.reload(fixtures)
    assert "fake-y" in registry.skills
    assert "fake-x" in registry.skills


@pytest.mark.parametrize("changed_field", ["version", "resources"])
def test_candidate_plan_rejects_manifest_changed_after_registry_load(
    tmp_path: Path,
    changed_field: str,
) -> None:
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "spatial" / "bound-skill"
    _write_v2_skill(skill_dir, skill_name="bound-skill")
    registry = OmicsRegistry()
    registry.load_all(skills_root)
    assert registry.skills["bound-skill"]["version"] == "1.0.0"
    assert registry.skills["bound-skill"]["compute_resources"]["memory_mib"] == 1024

    _write_v2_skill(
        skill_dir,
        skill_name="bound-skill",
        version="2.0.0" if changed_field == "version" else "1.0.0",
        memory_mib=2048 if changed_field == "resources" else 1024,
    )

    with pytest.raises(RuntimeError, match="reload"):
        registry.build_candidate_skill_chain(["bound-skill"])

    registry.reload(skills_root)
    plan = registry.build_candidate_skill_chain(["bound-skill"])
    assert plan["skill_revisions"]["bound-skill"]["skill_version"] == (
        "2.0.0" if changed_field == "version" else "1.0.0"
    )
    assert plan["resource_requests"]["bound-skill"]["memory_mib"] == (
        2048 if changed_field == "resources" else 1024
    )


def test_initial_load_keeps_empty_snapshot_visible_until_candidate_is_validated(
    tmp_path,
    monkeypatch,
):
    """First use publishes nothing until the complete candidate is valid."""
    skills_root = tmp_path / "skills"
    _write_skill(
        skills_root / "spatial" / "first-skill",
        skill_name="first-skill",
        legacy_aliases=("first-legacy",),
    )
    _write_skill(
        skills_root / "spatial" / "second-skill",
        skill_name="second-skill",
    )

    registry = OmicsRegistry()
    empty_state = registry._state
    validation_entered = Event()
    allow_validation = Event()
    original_validate = OmicsRegistry._validate_lifecycle_links

    def blocking_validate(candidate: OmicsRegistry) -> None:
        validation_entered.set()
        if not allow_validation.wait(timeout=5):
            raise TimeoutError("test did not release initial-load validation barrier")
        original_validate(candidate)

    monkeypatch.setattr(
        OmicsRegistry,
        "_validate_lifecycle_links",
        blocking_validate,
    )

    load_errors: list[BaseException] = []

    def run_initial_load() -> None:
        try:
            registry.load_all(skills_root)
        except BaseException as exc:  # pragma: no cover - asserted below
            load_errors.append(exc)

    worker = Thread(target=run_initial_load, name="registry-initial-load-test")
    worker.start()
    assert validation_entered.wait(timeout=5), "initial load never reached validation"
    try:
        assert registry._state is empty_state
        assert registry.skills == {}
        assert registry.canonical_aliases == ()
        assert registry.lazy_skills == {}
        assert registry._loaded is False
        assert registry._loaded_dir is None
    finally:
        allow_validation.set()
        worker.join(timeout=5)

    assert not worker.is_alive(), "initial load did not finish after validation release"
    assert load_errors == []
    assert registry._state is not empty_state
    assert set(registry.skills) == {
        "first-skill",
        "first-legacy",
        "second-skill",
    }
    assert registry.canonical_aliases == ("first-skill", "second-skill")
    assert registry._loaded is True
    assert registry._loaded_dir == skills_root.resolve()


def test_initial_load_validation_failure_preserves_the_empty_state_object(
    tmp_path,
):
    skills_root = tmp_path / "skills"
    _write_skill(
        skills_root / "spatial" / "first-dir",
        skill_name="shared-skill",
    )
    _write_skill(
        skills_root / "genomics" / "second-dir",
        skill_name="shared-skill",
    )
    registry = OmicsRegistry()
    empty_state = registry._state

    with pytest.raises(ValueError, match="duplicate registry identity 'shared-skill'"):
        registry.load_all(skills_root)

    assert registry._state is empty_state
    assert registry.skills == {}
    assert registry.lazy_skills == {}
    assert registry._loaded is False


def test_two_concurrent_first_load_calls_publish_one_complete_snapshot(
    tmp_path,
    monkeypatch,
):
    skills_root = tmp_path / "skills"
    _write_skill(
        skills_root / "spatial" / "first-skill",
        skill_name="first-skill",
    )
    _write_skill(
        skills_root / "spatial" / "second-skill",
        skill_name="second-skill",
    )

    registry = OmicsRegistry()
    import omicsclaw.skill.registry as registry_module

    monkeypatch.setattr(registry_module, "registry", registry)
    validation_entered = Event()
    allow_validation = Event()
    second_started = Event()
    second_finished = Event()
    validation_calls: list[OmicsRegistry] = []
    returned_registries: list[OmicsRegistry] = []
    errors: list[BaseException] = []
    original_validate = OmicsRegistry._validate_lifecycle_links

    def blocking_first_validation(candidate: OmicsRegistry) -> None:
        validation_calls.append(candidate)
        if len(validation_calls) == 1:
            validation_entered.set()
            if not allow_validation.wait(timeout=5):
                raise TimeoutError("test did not release concurrent-load barrier")
        original_validate(candidate)

    monkeypatch.setattr(
        OmicsRegistry,
        "_validate_lifecycle_links",
        blocking_first_validation,
    )

    def load_registry(*, second: bool = False) -> None:
        if second:
            second_started.set()
        try:
            returned_registries.append(ensure_registry_loaded(skills_root))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            if second:
                second_finished.set()

    first = Thread(target=load_registry, name="registry-first-caller")
    first.start()
    assert validation_entered.wait(timeout=5), "first caller never reached validation"

    second = Thread(
        target=lambda: load_registry(second=True),
        name="registry-second-caller",
    )
    second.start()
    assert second_started.wait(timeout=5), "second caller did not start"
    # While the first private candidate is blocked, a competing first caller
    # must wait rather than discover/register into the same live state.
    assert not second_finished.wait(timeout=0.1)

    allow_validation.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert len(validation_calls) == 1
    assert returned_registries == [registry, registry]
    assert registry.canonical_aliases == ("first-skill", "second-skill")
    assert set(registry.skills) == {"first-skill", "second-skill"}
    assert registry._loaded is True
    assert registry._loaded_dir == skills_root.resolve()


def test_canonical_alias_reader_uses_one_snapshot_during_reload(tmp_path, monkeypatch):
    old_root = tmp_path / "old-skills"
    new_root = tmp_path / "new-skills"
    _write_skill(old_root / "spatial" / "old-a", skill_name="old-a")
    _write_skill(old_root / "spatial" / "old-b", skill_name="old-b")
    _write_skill(new_root / "genomics" / "new-a", skill_name="new-a")
    registry = OmicsRegistry()
    registry.load_all(old_root)

    iteration_entered = Event()
    allow_iteration = Event()

    import omicsclaw.skill.registry as registry_module

    original_routable = registry_module.is_skill_automatically_routable

    def blocking_routable(info):
        if not iteration_entered.is_set():
            iteration_entered.set()
            if not allow_iteration.wait(timeout=5):
                raise TimeoutError("test did not release alias-reader barrier")
        return original_routable(info)

    monkeypatch.setattr(
        registry_module,
        "is_skill_automatically_routable",
        blocking_routable,
    )
    observed: list[list[str]] = []
    errors: list[BaseException] = []

    def read_aliases() -> None:
        try:
            observed.append(registry.canonical_skill_aliases())
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    reader = Thread(target=read_aliases, name="registry-alias-reader")
    reader.start()
    assert iteration_entered.wait(timeout=5), "alias reader never reached barrier"
    try:
        registry.reload(new_root)
    finally:
        allow_iteration.set()
        reader.join(timeout=5)

    assert errors == []
    assert not reader.is_alive()
    assert observed == [["old-a", "old-b"]]
    assert registry.canonical_aliases == ("new-a",)


def test_reload_keeps_old_snapshot_visible_until_new_snapshot_is_validated(
    tmp_path,
    monkeypatch,
):
    """A live registry publishes a complete reload with one atomic state swap."""
    old_root = tmp_path / "old-skills"
    new_root = tmp_path / "new-skills"
    _write_skill(
        old_root / "spatial" / "old-skill",
        skill_name="old-skill",
        legacy_aliases=("old-legacy",),
    )
    _write_skill(
        new_root / "genomics" / "new-a",
        skill_name="new-a",
        legacy_aliases=("new-a-legacy",),
    )
    _write_skill(
        new_root / "genomics" / "new-b",
        skill_name="new-b",
    )

    registry = OmicsRegistry()
    registry.load_all(old_root)
    registry._skill_dag_cache = {"snapshot": "old"}
    registry_identity = id(registry)
    old_skills_view = registry.skills

    validation_entered = Event()
    allow_validation = Event()
    original_validate = OmicsRegistry._validate_lifecycle_links

    def blocking_validate(candidate: OmicsRegistry) -> None:
        validation_entered.set()
        if not allow_validation.wait(timeout=5):
            raise TimeoutError("test did not release registry validation barrier")
        original_validate(candidate)

    monkeypatch.setattr(
        OmicsRegistry,
        "_validate_lifecycle_links",
        blocking_validate,
    )

    reload_errors: list[BaseException] = []

    def run_reload() -> None:
        try:
            registry.reload(new_root)
        except BaseException as exc:  # pragma: no cover - asserted below
            reload_errors.append(exc)

    worker = Thread(target=run_reload, name="registry-reload-test")
    worker.start()
    assert validation_entered.wait(timeout=5), "reload never reached validation barrier"
    try:
        during_reload = {
            "identity": id(registry),
            "skills_view": registry.skills,
            "skill_keys": set(registry.skills),
            "canonical_aliases": list(registry.canonical_aliases),
            "lazy_keys": set(registry.lazy_skills),
            "spatial_count": registry.domains["spatial"].get("skill_count", 0),
            "genomics_count": registry.domains["genomics"].get("skill_count", 0),
            "loaded": registry._loaded,
            "loaded_dir": registry._loaded_dir,
            "dag_cache": registry._skill_dag_cache,
        }
    finally:
        allow_validation.set()
        worker.join(timeout=5)

    assert not worker.is_alive(), "reload did not finish after validation release"
    assert reload_errors == []
    assert during_reload == {
        "identity": registry_identity,
        "skills_view": old_skills_view,
        "skill_keys": {"old-skill", "old-legacy"},
        "canonical_aliases": ["old-skill"],
        "lazy_keys": {"old-skill"},
        "spatial_count": 1,
        "genomics_count": 0,
        "loaded": True,
        "loaded_dir": old_root.resolve(),
        "dag_cache": {"snapshot": "old"},
    }

    assert id(registry) == registry_identity
    assert set(registry.skills) == {"new-a", "new-a-legacy", "new-b"}
    assert registry.canonical_aliases == ("new-a", "new-b")
    assert set(registry.lazy_skills) == {"new-a", "new-b"}
    assert registry.domains["spatial"]["skill_count"] == 0
    assert registry.domains["genomics"]["skill_count"] == 2
    assert registry._loaded is True
    assert registry._loaded_dir == new_root.resolve()
    assert registry._skill_dag_cache is None


def test_reload_cannot_publish_while_compatibility_dag_cache_is_building(
    tmp_path,
    monkeypatch,
):
    import omicsclaw.skill.skill_dag as skill_dag_module

    old_root = tmp_path / "old-skills"
    new_root = tmp_path / "new-skills"
    _write_skill(old_root / "spatial" / "old-skill", skill_name="old-skill")
    _write_skill(new_root / "genomics" / "new-skill", skill_name="new-skill")
    registry = OmicsRegistry()
    registry.load_all(old_root)

    dag_build_entered = Event()
    allow_dag_build = Event()
    reload_started = Event()
    reload_finished = Event()
    errors: list[BaseException] = []
    dag_results: list[dict[str, object]] = []

    def blocking_build(subject: OmicsRegistry, **_kwargs):
        aliases = tuple(subject.canonical_aliases)
        dag_build_entered.set()
        if not allow_dag_build.wait(timeout=5):
            raise TimeoutError("test did not release DAG-build barrier")
        return {"aliases": aliases}

    monkeypatch.setattr(skill_dag_module, "build_skill_dag", blocking_build)

    def build_dag() -> None:
        try:
            dag_results.append(registry.build_compatibility_dag())
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def reload_registry() -> None:
        reload_started.set()
        try:
            registry.reload(new_root)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            reload_finished.set()

    dag_worker = Thread(target=build_dag, name="registry-dag-build")
    dag_worker.start()
    assert dag_build_entered.wait(timeout=5), "DAG build never reached barrier"

    reload_worker = Thread(target=reload_registry, name="registry-dag-reload")
    reload_worker.start()
    assert reload_started.wait(timeout=5), "reload caller did not start"
    reloaded_while_dag_blocked = reload_finished.wait(timeout=0.1)
    allow_dag_build.set()
    dag_worker.join(timeout=5)
    reload_worker.join(timeout=5)

    assert not reloaded_while_dag_blocked
    assert errors == []
    assert not dag_worker.is_alive()
    assert not reload_worker.is_alive()
    assert dag_results == [{"aliases": ("old-skill",)}]
    assert registry.canonical_aliases == ("new-skill",)
    assert registry._skill_dag_cache is None
    assert registry.build_compatibility_dag() == {"aliases": ("new-skill",)}


@pytest.mark.parametrize(
    "query_name",
    ["upstream", "downstream", "topological", "candidate"],
)
def test_composite_graph_queries_bind_one_registry_publication(
    tmp_path,
    monkeypatch,
    query_name,
):
    """Reload cannot split graph, alias, and param reads across snapshots."""
    old_root = tmp_path / "old-skills"
    new_root = tmp_path / "new-skills"
    _write_skill(old_root / "spatial" / "shared-skill", skill_name="shared-skill")
    _write_skill(new_root / "genomics" / "shared-skill", skill_name="shared-skill")
    registry = OmicsRegistry()
    registry.load_all(old_root)

    canonical_entered = Event()
    allow_canonical = Event()
    reload_finished = Event()
    errors: list[BaseException] = []
    original_canonical = OmicsRegistry._canonical_graph_skill

    def blocking_canonical(subject: OmicsRegistry, skill: str) -> str:
        canonical = original_canonical(subject, skill)
        canonical_entered.set()
        if not allow_canonical.wait(timeout=5):
            raise TimeoutError("test did not release canonicalization barrier")
        return canonical

    monkeypatch.setattr(
        OmicsRegistry,
        "_canonical_graph_skill",
        blocking_canonical,
    )

    def run_query() -> None:
        try:
            if query_name == "upstream":
                registry.get_upstream_skills("shared-skill")
            elif query_name == "downstream":
                registry.get_downstream_skills("shared-skill")
            elif query_name == "topological":
                registry.topological_skill_order(["shared-skill"])
            else:
                registry.build_candidate_skill_chain(["shared-skill"])
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def run_reload() -> None:
        try:
            registry.reload(new_root)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            reload_finished.set()

    query_worker = Thread(target=run_query, name=f"registry-{query_name}-query")
    query_worker.start()
    assert canonical_entered.wait(timeout=5), "query never reached canonicalization"

    reload_worker = Thread(target=run_reload, name=f"registry-{query_name}-reload")
    reload_worker.start()
    published_mid_query = reload_finished.wait(timeout=0.1)
    allow_canonical.set()
    query_worker.join(timeout=5)
    reload_worker.join(timeout=5)

    assert not published_mid_query
    assert errors == []
    assert not query_worker.is_alive()
    assert not reload_worker.is_alive()
    assert registry._loaded_dir == new_root.resolve()


def test_keyword_map_binds_lazy_metadata_and_aliases_to_one_publication(
    tmp_path,
    monkeypatch,
):
    old_root = tmp_path / "old-skills"
    new_root = tmp_path / "new-skills"
    _write_skill(old_root / "spatial" / "old-skill", skill_name="old-skill")
    _write_skill(new_root / "genomics" / "new-skill", skill_name="new-skill")
    registry = OmicsRegistry()
    registry.load_all(old_root)

    class LegacyLazy:
        name = ""
        domain = "spatial"
        trigger_keywords = ["needle"]

    registry._state = replace(
        registry._state,
        lazy_skills=MappingProxyType({"old-skill": LegacyLazy()}),
    )
    alias_entered = Event()
    allow_alias = Event()
    reload_finished = Event()
    errors: list[BaseException] = []
    original_resolve = OmicsRegistry._resolve_alias

    def blocking_resolve(subject: OmicsRegistry, key: str) -> str:
        resolved = original_resolve(subject, key)
        alias_entered.set()
        if not allow_alias.wait(timeout=5):
            raise TimeoutError("test did not release keyword alias barrier")
        return resolved

    monkeypatch.setattr(OmicsRegistry, "_resolve_alias", blocking_resolve)

    def build_keywords() -> None:
        try:
            registry.build_keyword_map()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def reload_registry() -> None:
        try:
            registry.reload(new_root)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            reload_finished.set()

    query_worker = Thread(target=build_keywords, name="registry-keyword-query")
    query_worker.start()
    assert alias_entered.wait(timeout=5), "keyword query never resolved an alias"
    reload_worker = Thread(target=reload_registry, name="registry-keyword-reload")
    reload_worker.start()
    published_mid_query = reload_finished.wait(timeout=0.1)
    allow_alias.set()
    query_worker.join(timeout=5)
    reload_worker.join(timeout=5)

    assert not published_mid_query
    assert errors == []
    assert not query_worker.is_alive()
    assert not reload_worker.is_alive()


def test_first_dag_build_caches_on_the_loaded_snapshot(tmp_path, monkeypatch):
    import omicsclaw.skill.registry as registry_module

    skills_root = tmp_path / "skills"
    _write_skill(
        skills_root / "spatial" / "first-skill",
        skill_name="first-skill",
    )
    monkeypatch.setattr(registry_module, "SKILLS_DIR", skills_root)
    registry = OmicsRegistry()

    graph = registry.build_compatibility_dag()

    assert registry._loaded is True
    assert registry._loaded_dir == skills_root.resolve()
    assert registry._skill_dag_cache == graph
    assert [node["skill"] for node in graph["nodes"]] == ["first-skill"]


def test_dag_cache_rejects_review_authority_change_during_build(
    tmp_path,
    monkeypatch,
):
    import omicsclaw.skill.skill_dag as skill_dag_module

    skills_root = tmp_path / "skills"
    _write_skill(
        skills_root / "spatial" / "first-skill",
        skill_name="first-skill",
    )
    registry = OmicsRegistry()
    registry.load_all(skills_root)
    review_path = skills_root / "skill_dag_reviews.yaml"
    original_build = skill_dag_module.build_skill_dag

    def mutate_review_during_build(subject, **kwargs):
        review_path.write_text(
            "schema_version: 2\nreviews: []\n",
            encoding="utf-8",
        )
        return original_build(subject, **kwargs)

    monkeypatch.setattr(
        skill_dag_module,
        "build_skill_dag",
        mutate_review_during_build,
    )

    with pytest.raises(ValueError, match="changed while building"):
        registry.build_compatibility_dag()

    assert registry._skill_dag_cache is None


def test_reload_validation_failure_keeps_the_published_snapshot(tmp_path, monkeypatch):
    old_root = tmp_path / "old-skills"
    invalid_root = tmp_path / "invalid-skills"
    _write_skill(old_root / "spatial" / "old-skill", skill_name="old-skill")
    _write_skill(invalid_root / "genomics" / "invalid", skill_name="invalid")

    registry = OmicsRegistry()
    registry.load_all(old_root)
    old_skills = registry.skills
    old_aliases = registry.canonical_aliases
    old_lazy = registry.lazy_skills
    old_domains = registry.domains

    def reject_candidate(_candidate: OmicsRegistry) -> None:
        raise ValueError("candidate registry is invalid")

    monkeypatch.setattr(
        OmicsRegistry,
        "_validate_lifecycle_links",
        reject_candidate,
    )

    with pytest.raises(ValueError, match="candidate registry is invalid"):
        registry.reload(invalid_root)

    assert registry.skills is old_skills
    assert registry.canonical_aliases is old_aliases
    assert registry.lazy_skills is old_lazy
    assert registry.domains is old_domains
    assert set(registry.skills) == {"old-skill"}
    assert registry._loaded is True
    assert registry._loaded_dir == old_root.resolve()


def test_reload_missing_root_keeps_the_published_state_object(tmp_path):
    old_root = tmp_path / "old-skills"
    _write_skill(old_root / "spatial" / "old-skill", skill_name="old-skill")

    registry = OmicsRegistry()
    registry.load_all(old_root)
    old_state = registry._state

    with pytest.raises(FileNotFoundError, match="skills root does not exist"):
        registry.reload(tmp_path / "missing-skills")

    assert registry._state is old_state
    assert set(registry.skills) == {"old-skill"}


def test_reload_empty_root_keeps_the_published_state_object(tmp_path):
    old_root = tmp_path / "old-skills"
    empty_root = tmp_path / "empty-skills"
    empty_root.mkdir()
    _write_skill(old_root / "spatial" / "old-skill", skill_name="old-skill")

    registry = OmicsRegistry()
    registry.load_all(old_root)
    old_state = registry._state

    with pytest.raises(ValueError, match="skills inventory is empty"):
        registry.reload(empty_root)

    assert registry._state is old_state
    assert set(registry.skills) == {"old-skill"}


def test_reload_missing_unrelated_runtime_keeps_the_published_state_object(tmp_path):
    old_root = tmp_path / "old-skills"
    candidate_root = tmp_path / "candidate-skills"
    _write_skill(old_root / "spatial" / "old-skill", skill_name="old-skill")
    _write_skill(candidate_root / "spatial" / "target-skill", skill_name="target-skill")
    unrelated = candidate_root / "genomics" / "unrelated-skill"
    _write_skill(unrelated, skill_name="unrelated-skill")
    (unrelated / "unrelated_skill.py").unlink()
    (unrelated / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")

    registry = OmicsRegistry()
    registry.load_all(old_root)
    old_state = registry._state

    with pytest.raises(ValueError, match="has no runnable Python entry"):
        registry.reload(candidate_root)

    assert registry._state is old_state
    assert set(registry.skills) == {"old-skill"}


def test_reload_incomplete_manifest_inventory_keeps_the_published_state_object(tmp_path):
    old_root = tmp_path / "old-skills"
    candidate_root = tmp_path / "candidate-skills"
    _write_skill(old_root / "spatial" / "old-skill", skill_name="old-skill")
    _write_skill(candidate_root / "spatial" / "target-skill", skill_name="target-skill")
    _write_skill(
        candidate_root / "spatial" / "nested" / "too-deep" / "missed-skill",
        skill_name="missed-skill",
    )

    registry = OmicsRegistry()
    registry.load_all(old_root)
    old_state = registry._state

    with pytest.raises(ValueError, match="manifest inventory is incomplete"):
        registry.reload(candidate_root)

    assert registry._state is old_state
    assert set(registry.skills) == {"old-skill"}


def test_reload_duplicate_canonical_identity_keeps_the_published_state_object(tmp_path):
    old_root = tmp_path / "old-skills"
    candidate_root = tmp_path / "candidate-skills"
    _write_skill(old_root / "spatial" / "old-skill", skill_name="old-skill")
    _write_skill(candidate_root / "spatial" / "first-dir", skill_name="shared-skill")
    _write_skill(candidate_root / "genomics" / "second-dir", skill_name="shared-skill")

    registry = OmicsRegistry()
    registry.load_all(old_root)
    old_state = registry._state

    with pytest.raises(ValueError, match="duplicate registry identity 'shared-skill'"):
        registry.reload(candidate_root)

    assert registry._state is old_state
    assert set(registry.skills) == {"old-skill"}


def test_reload_duplicate_lookup_alias_keeps_the_published_state_object(tmp_path):
    old_root = tmp_path / "old-skills"
    candidate_root = tmp_path / "candidate-skills"
    _write_skill(old_root / "spatial" / "old-skill", skill_name="old-skill")
    _write_skill(
        candidate_root / "spatial" / "first-skill",
        skill_name="first-skill",
        legacy_aliases=("shared-alias",),
    )
    _write_skill(
        candidate_root / "genomics" / "second-skill",
        skill_name="second-skill",
        legacy_aliases=("shared-alias",),
    )

    registry = OmicsRegistry()
    registry.load_all(old_root)
    old_state = registry._state

    with pytest.raises(ValueError, match="duplicate registry alias 'shared-alias'"):
        registry.reload(candidate_root)

    assert registry._state is old_state
    assert set(registry.skills) == {"old-skill"}


def test_discovery_order_is_stable_when_filesystem_enumeration_is_reversed(
    tmp_path,
    monkeypatch,
):
    skills_root = tmp_path / "skills"
    _write_skill(
        skills_root / "spatial" / "zz-spatial",
        skill_name="zz-spatial",
    )
    _write_skill(
        skills_root / "spatial" / "aa-spatial",
        skill_name="aa-spatial",
    )
    _write_skill(
        skills_root / "genomics" / "zz-genomics",
        skill_name="zz-genomics",
    )
    _write_skill(
        skills_root / "genomics" / "aa-genomics",
        skill_name="aa-genomics",
    )
    _write_skill(
        skills_root / "singlecell" / "scrna" / "zz-rna",
        skill_name="zz-rna",
    )
    _write_skill(
        skills_root / "singlecell" / "scrna" / "aa-rna",
        skill_name="aa-rna",
    )
    _write_skill(
        skills_root / "singlecell" / "scatac" / "zz-atac",
        skill_name="zz-atac",
    )
    _write_skill(
        skills_root / "singlecell" / "scatac" / "aa-atac",
        skill_name="aa-atac",
    )

    original_iterdir = Path.iterdir

    def reversed_iterdir(path: Path):
        return iter(
            sorted(
                original_iterdir(path),
                key=lambda candidate: candidate.name,
                reverse=True,
            )
        )

    monkeypatch.setattr(Path, "iterdir", reversed_iterdir)

    registry = OmicsRegistry()
    registry.load_all(skills_root)

    expected = [
        "aa-genomics",
        "zz-genomics",
        "aa-atac",
        "zz-atac",
        "aa-rna",
        "zz-rna",
        "aa-spatial",
        "zz-spatial",
    ]
    assert registry.canonical_aliases == tuple(expected)
    assert list(registry.lazy_skills) == expected


def test_parameters_yaml_domain_overrides_directory_parent(tmp_path):
    """If a skill's parameters.yaml declares a ``domain:`` it wins over the
    parent directory name. This locks the current precedence so changing it
    requires an explicit test update."""
    fixtures = tmp_path / "skills"
    _write_skill(
        fixtures / "orchestrator" / "stray-spatial",
        skill_name="stray-spatial",
        domain_in_yaml="spatial",
    )

    registry = OmicsRegistry()
    registry.load_all(fixtures)
    info = registry.skills["stray-spatial"]
    assert info["domain"] == "spatial"


def test_lazy_metadata_keeps_same_basename_skills_bound_to_their_own_paths(
    tmp_path,
):
    skills_root = tmp_path / "skills"
    spatial_dir = skills_root / "spatial" / "shared"
    genomics_dir = skills_root / "genomics" / "shared"
    _write_skill(spatial_dir, skill_name="spatial-shared")
    _write_skill(genomics_dir, skill_name="genomics-shared")

    registry = OmicsRegistry()
    registry.load_all(skills_root)

    assert set(registry.canonical_aliases) == {
        "spatial-shared",
        "genomics-shared",
    }
    assert registry.skills["spatial-shared"]["script"].parent == spatial_dir
    assert registry.skills["genomics-shared"]["script"].parent == genomics_dir
    assert set(registry.lazy_skills) == {"spatial/shared", "genomics/shared"}


def test_skill_count_refreshed_from_disk(tmp_path):
    """Domain ``skill_count`` must be derived from the loaded skills, not from
    a stale literal in ``_HARDCODED_DOMAINS``."""
    fixtures = tmp_path / "skills"
    _write_skill(fixtures / "spatial" / "fake-1", skill_name="fake-1")
    _write_skill(fixtures / "spatial" / "fake-2", skill_name="fake-2")

    registry = OmicsRegistry()
    registry.load_all(fixtures)
    assert registry.domains["spatial"]["skill_count"] == 2


def test_invalidate_resets_state(tmp_path):
    fixtures = tmp_path / "skills"
    _write_skill(fixtures / "spatial" / "fake-z", skill_name="fake-z")

    registry = OmicsRegistry()
    registry.load_all(fixtures)
    assert registry.skills

    registry.invalidate()
    assert registry.skills == {}
    assert registry.lazy_skills == {}
    assert registry._loaded is False
