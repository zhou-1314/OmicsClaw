"""Unit tests for the ADR 0035 project-scoped output resolver."""

from __future__ import annotations

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from omicsclaw.common import output_claim
from omicsclaw.common import run_paths as rp

RUN_DIR_RE = re.compile(r"^[^/]*__\d{8}_\d{6}__[^/]+$")  # frontend run-link.ts pattern
TS = "20260624_120000"


# --------------------------------------------------------------------------- slug
def test_slugify_token_empty_and_nonascii():
    assert rp.slugify_token("PBMC 3k!!") == "pbmc-3k"
    assert rp.slugify_token("细胞数据") == ""  # all stripped -> empty, not "default"
    assert len(rp.slugify_token("a" * 100)) <= 48


def test_dataset_slug_ladder():
    assert rp.dataset_slug(dataset_label="My Sample") == "my-sample"
    assert rp.dataset_slug(input_path="/data/pbmc3k.h5ad") == "pbmc3k"
    assert rp.dataset_slug(demo=True) == "demo"
    assert rp.dataset_slug() == "noinput"
    multi = rp.dataset_slug(input_paths=["/a/x.h5ad", "/a/y.h5ad"])
    assert multi.startswith("multi-")
    # CJK basename does not collapse to "default"
    cjk = rp.dataset_slug(input_path="/data/细胞.h5ad")
    assert cjk.startswith("ds-")


def test_short_id_deterministic():
    pid = "a1b2c3d4e5f6"
    assert rp.project_short_id(pid) == rp.project_short_id(pid)
    assert len(rp.project_short_id(pid)) == 10


# --------------------------------------------------------------------------- run name
def test_build_run_dir_name_matches_frontend_regex():
    name = rp.build_run_dir_name("sc-de", TS, "pbmc3k", method="wilcoxon", uid="a1b2c3d4")
    assert name == "sc-de__wilcoxon__20260624_120000__pbmc3k-a1b2c3d4"
    assert RUN_DIR_RE.match(name)
    # method optional
    nom = rp.build_run_dir_name("sc-de", TS, "pbmc3k", uid="a1b2c3d4")
    assert nom == "sc-de__20260624_120000__pbmc3k-a1b2c3d4"
    assert RUN_DIR_RE.match(nom)


# --------------------------------------------------------------------------- project dir
def test_default_project(tmp_path: Path):
    d = rp.resolve_project_dir(tmp_path, "")
    assert d == tmp_path / "default"
    meta = rp.read_project_meta(d)
    assert meta["project_id"] == "default"


def test_project_dir_reused_by_short_id_regardless_of_name(tmp_path: Path):
    pid = "thread123abc"
    first = rp.resolve_project_dir(tmp_path, pid, "Glioma Study")
    assert first.name.startswith("glioma-study__")
    # A later caller that does NOT know the name must resolve the SAME folder.
    second = rp.resolve_project_dir(tmp_path, pid, "")
    assert second == first
    assert rp.read_project_meta(first)["project_id"] == pid


def test_project_resolver_rejects_windows_reparse_output_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    junction = tmp_path / "junction"
    junction.mkdir()
    junction_identity = (junction.lstat().st_dev, junction.lstat().st_ino)
    monkeypatch.setattr(
        output_claim,
        "_is_windows_reparse_point",
        lambda entry_stat: (entry_stat.st_dev, entry_stat.st_ino)
        == junction_identity,
    )

    with pytest.raises(ValueError, match="aliased output root"):
        rp.resolve_project_dir(junction / "output", "project-id", "Project")

    assert list(junction.iterdir()) == []


def test_rename_updates_meta_not_folder(tmp_path: Path):
    pid = "thread123abc"
    d = rp.resolve_project_dir(tmp_path, pid, "Glioma Study")
    d2 = rp.resolve_project_dir(tmp_path, pid, "GBM Atlas")
    assert d2 == d  # folder unchanged (constraint 6)
    assert rp.read_project_meta(d)["display_name"] == "GBM Atlas"


def test_empty_name_does_not_clobber_seeded_display_name(tmp_path: Path):
    """A later caller without the thread name (the agent passes project_name='')
    must keep the readable display name seeded at thread creation, not reset it
    to the opaque project_id."""
    pid = "deadbeefthread"
    rp.resolve_project_dir(tmp_path, pid, "Glioma Study")  # seeded (e.g. thread create)
    d = rp.resolve_project_dir(tmp_path, pid, "")          # agent run, no name
    assert rp.read_project_meta(d)["display_name"] == "Glioma Study"


def test_short_id_lookup_verified_against_meta(tmp_path: Path):
    """A stray directory sharing the short-id suffix but with a different/absent
    project_meta must not be mis-matched (constraint 6)."""
    pid = "realproject1"
    short = rp.project_short_id(pid)
    decoy = tmp_path / f"decoy__{short}"   # same suffix, no/!= project_id
    decoy.mkdir()
    (decoy / "project_meta.json").write_text('{"project_id": "someone-else"}')
    d = rp.resolve_project_dir(tmp_path, pid, "Real Project")
    assert d != decoy
    assert rp.read_project_meta(d)["project_id"] == pid
    # second resolve finds the real dir, not the decoy
    assert rp.resolve_project_dir(tmp_path, pid, "") == d


def test_project_listing_does_not_trust_symlinked_project_metadata(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    project_dir = output_root / "safe-project"
    run_dir = project_dir / "qc__20250101_000000__ds-deadbeef"
    run_dir.mkdir(parents=True)
    forged = tmp_path / "forged-project-meta.json"
    forged.write_text(
        '{"project_id": "attacker", "display_name": "Spoofed Display"}\n',
        encoding="utf-8",
    )
    (project_dir / rp.PROJECT_META_FILENAME).symlink_to(forged)

    assert rp.read_project_meta(project_dir) == {}
    assert rp.list_projects(output_root) == [
        {
            "dir": "safe-project",
            "project_id": "safe-project",
            "display_name": "safe-project",
            "runs": 1,
        }
    ]
    assert rp.resolve_cli_project(output_root, "Spoofed Display") == (
        "spoofed-display",
        "Spoofed Display",
    )


def test_project_listing_does_not_trust_hardlinked_project_metadata(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    project_dir = output_root / "safe-project"
    run_dir = project_dir / "qc__20250101_000000__ds-deadbeef"
    run_dir.mkdir(parents=True)
    forged = tmp_path / "forged-project-meta.json"
    forged.write_text(
        '{"project_id": "attacker", "display_name": "Spoofed Display"}\n',
        encoding="utf-8",
    )
    (project_dir / rp.PROJECT_META_FILENAME).hardlink_to(forged)

    assert rp.read_project_meta(project_dir) == {}
    assert rp.list_projects(output_root)[0]["project_id"] == "safe-project"
    assert rp.list_projects(output_root)[0]["display_name"] == "safe-project"
    assert rp.resolve_cli_project(output_root, "Spoofed Display") == (
        "spoofed-display",
        "Spoofed Display",
    )


def test_project_metadata_json_must_be_an_object(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    for project_name, payload in (("list-meta", "[]\n"), ("scalar-meta", '"spoof"\n')):
        project_dir = output_root / project_name
        (project_dir / "qc__20250101_000000__ds-deadbeef").mkdir(parents=True)
        (project_dir / rp.PROJECT_META_FILENAME).write_text(payload, encoding="utf-8")
        assert rp.read_project_meta(project_dir) == {}

    assert {
        project["project_id"] for project in rp.list_projects(output_root)
    } == {"list-meta", "scalar-meta"}


def test_project_metadata_reader_preserves_symlink_dotdot_evidence(tmp_path: Path) -> None:
    lexical_root = tmp_path / "lexical"
    lexical_root.mkdir()
    external_root = tmp_path / "external"
    project_dir = external_root / "safe-project"
    project_dir.mkdir(parents=True)
    (external_root / "nested").mkdir()
    (project_dir / rp.PROJECT_META_FILENAME).write_text(
        '{"project_id": "attacker", "display_name": "Spoofed Display"}\n',
        encoding="utf-8",
    )
    alias = lexical_root / "alias"
    alias.symlink_to(external_root / "nested", target_is_directory=True)
    raw_project_dir = alias / ".." / "safe-project"

    assert raw_project_dir.resolve(strict=True) == project_dir
    assert rp.read_project_meta(raw_project_dir) == {}


def test_project_metadata_reader_rejects_directory_and_fifo(tmp_path: Path) -> None:
    directory_project = tmp_path / "directory-project"
    (directory_project / rp.PROJECT_META_FILENAME).mkdir(parents=True)
    fifo_project = tmp_path / "fifo-project"
    fifo_project.mkdir()
    os.mkfifo(fifo_project / rp.PROJECT_META_FILENAME)

    assert rp.read_project_meta(directory_project) == {}
    assert rp.read_project_meta(fifo_project) == {}


@pytest.mark.parametrize(
    "payload",
    [
        {"display_name": "Missing ID"},
        {"project_id": ""},
        {"project_id": "   "},
        {"project_id": 42},
    ],
)
def test_project_metadata_reader_requires_typed_nonempty_project_id(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / rp.PROJECT_META_FILENAME).write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    assert rp.read_project_meta(project_dir) == {}


def test_project_metadata_writer_does_not_follow_preplanted_temp_symlink(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "output"
    project_dir = output_root / rp.DEFAULT_PROJECT_ID
    project_dir.mkdir(parents=True)
    victim = tmp_path / "victim.json"
    victim.write_text("KEEP\n", encoding="utf-8")
    poisoned_temp = project_dir / f".{rp.PROJECT_META_FILENAME}.tmp-{os.getpid()}"
    poisoned_temp.symlink_to(victim)

    resolved = rp.resolve_project_dir(output_root)

    assert resolved == project_dir
    assert victim.read_text(encoding="utf-8") == "KEEP\n"
    assert rp.read_project_meta(project_dir)["project_id"] == rp.DEFAULT_PROJECT_ID


def test_project_metadata_writer_rejects_destination_aliases_without_touching_victim(
    tmp_path: Path,
) -> None:
    for alias_kind in ("symlink", "hardlink"):
        output_root = tmp_path / alias_kind / "output"
        project_dir = output_root / rp.DEFAULT_PROJECT_ID
        project_dir.mkdir(parents=True)
        victim = tmp_path / f"{alias_kind}-victim.json"
        original = '{"project_id": "attacker", "display_name": "Forged"}\n'
        victim.write_text(original, encoding="utf-8")
        metadata_path = project_dir / rp.PROJECT_META_FILENAME
        if alias_kind == "symlink":
            metadata_path.symlink_to(victim)
        else:
            metadata_path.hardlink_to(victim)

        try:
            rp.resolve_project_dir(output_root)
        except RuntimeError:
            pass
        else:  # pragma: no cover - assertion branch
            raise AssertionError(f"{alias_kind} metadata destination was accepted")

        assert victim.read_text(encoding="utf-8") == original


def test_project_resolution_lock_rejects_symlink_and_hardlink(tmp_path: Path) -> None:
    for alias_kind in ("symlink", "hardlink"):
        output_root = tmp_path / alias_kind
        output_root.mkdir()
        victim = tmp_path / f"{alias_kind}-lock-victim"
        victim.write_text("KEEP\n", encoding="utf-8")
        lock_path = output_root / ".project-resolve.lock"
        if alias_kind == "symlink":
            lock_path.symlink_to(victim)
        else:
            lock_path.hardlink_to(victim)

        try:
            rp.resolve_project_dir(output_root)
        except RuntimeError:
            pass
        else:  # pragma: no cover - assertion branch
            raise AssertionError(f"{alias_kind} resolution lock was accepted")

        assert victim.read_text(encoding="utf-8") == "KEEP\n"


def test_project_resolution_lock_rejects_fifo_and_directory_without_blocking(
    tmp_path: Path,
) -> None:
    for entry_kind in ("fifo", "directory"):
        output_root = tmp_path / entry_kind
        output_root.mkdir()
        lock_path = output_root / ".project-resolve.lock"
        if entry_kind == "fifo":
            os.mkfifo(lock_path)
        else:
            lock_path.mkdir()

        try:
            rp.resolve_project_dir(output_root)
        except RuntimeError:
            pass
        else:  # pragma: no cover - assertion branch
            raise AssertionError(f"{entry_kind} resolution lock was accepted")


def test_project_dir_creation_is_serialized_by_project_id(tmp_path: Path, monkeypatch):
    """Concurrent first creators with different names must not leave duplicate
    ``__<short-id>`` project dirs for one canonical project_id."""

    pid = "race-project"
    names = ["Zeta Study", "Alpha Study"]
    original_write = rp._write_project_meta

    def slow_write(*args, **kwargs):
        import time

        time.sleep(0.02)
        return original_write(*args, **kwargs)

    monkeypatch.setattr(rp, "_write_project_meta", slow_write)

    with ThreadPoolExecutor(max_workers=2) as pool:
        dirs = list(pool.map(lambda name: rp.resolve_project_dir(tmp_path, pid, name), names))

    short = rp.project_short_id(pid)
    project_dirs = [
        p
        for p in tmp_path.iterdir()
        if p.is_dir() and p.name.endswith(f"__{short}")
    ]
    assert len(project_dirs) == 1
    assert dirs[0] == dirs[1] == project_dirs[0]
    assert rp.read_project_meta(project_dirs[0])["project_id"] == pid


# --------------------------------------------------------------------------- run dir
def test_resolve_run_dir_nested_and_unique(tmp_path: Path):
    res = rp.resolve_run_dir(
        output_root=tmp_path, skill="sc-de", project_id="t1", project_name="Study One",
        input_path="/data/pbmc3k.h5ad", method="wilcoxon", timestamp=TS,
    )
    assert res.run_dir.is_dir()
    assert res.run_dir.parent == res.project_dir
    assert RUN_DIR_RE.match(res.run_id)
    assert "pbmc3k" in res.run_id

    # Same ts + same uid -> atomic _N suffix, never an overwrite (constraint 2).
    a = rp.resolve_run_dir(output_root=tmp_path, skill="x", project_id="t1",
                           input_path="/d/s.h5ad", timestamp=TS, uid="dead")
    b = rp.resolve_run_dir(output_root=tmp_path, skill="x", project_id="t1",
                           input_path="/d/s.h5ad", timestamp=TS, uid="dead")
    assert a.run_dir != b.run_dir
    assert b.run_dir.name.endswith("_1")


def test_resolve_run_dir_default_project(tmp_path: Path):
    res = rp.resolve_run_dir(output_root=tmp_path, skill="qc", demo=True)
    assert res.project_dir == tmp_path / "default"
    assert res.project_id == "default"
    assert "demo" in res.run_id


def test_resolve_project_dir_rejects_raw_alias_before_creating_output_root(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(external, target_is_directory=True)
    requested_root = alias / "new-output-root"

    with pytest.raises((RuntimeError, ValueError), match="alias|symbolic"):
        rp.resolve_project_dir(
            requested_root,
            project_id="project-id",
            project_name="Project",
        )

    assert not (external / "new-output-root").exists()


# --------------------------------------------------------------------------- finalize + index
def test_finalize_writes_manifest_and_index(tmp_path: Path):
    res = rp.resolve_run_dir(output_root=tmp_path, skill="sc-de", project_id="t1",
                             project_name="Study", input_path="/d/pbmc.h5ad", timestamp=TS)
    rp.finalize_run(res.run_dir, skill="sc-de", status="completed",
                    method="wilcoxon", dataset=res.dataset, surface="cli")

    manifest = json.loads((res.run_dir / "manifest.json").read_text())
    run_meta = manifest["metadata"]["run"]
    assert run_meta["project_id"] == "t1"
    assert run_meta["run_id"] == res.run_id
    assert run_meta["status"] == "completed"
    assert run_meta["dataset"] == "pbmc"

    rows = rp.read_index(res.project_dir)
    assert len(rows) == 1
    assert rows[0]["run_id"] == res.run_id
    assert rows[0]["project_id"] == "t1"
    assert rows[0]["manifest_mtime"] > 0
    assert rows[0]["path_rel"] == res.run_id


def test_finalize_does_not_publish_index_when_manifest_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    res = rp.resolve_run_dir(
        output_root=tmp_path,
        skill="sc-de",
        project_id="t1",
        project_name="Study",
        timestamp=TS,
    )

    def _fail_manifest_write(*_args, **_kwargs):
        raise OSError("simulated manifest persistence failure")

    monkeypatch.setattr(
        "omicsclaw.common.manifest.save_manifest",
        _fail_manifest_write,
    )

    with pytest.raises(OSError, match="manifest persistence failure"):
        rp.finalize_run(
            res.run_dir,
            skill="sc-de",
            status="completed",
            dataset=res.dataset,
        )

    assert not (res.run_dir / "manifest.json").exists()
    assert rp.read_index(res.project_dir) == []


def test_finalize_does_not_consume_aliased_project_identity(tmp_path: Path) -> None:
    for alias_kind in ("symlink", "hardlink"):
        project_dir = tmp_path / alias_kind / "safe-project"
        run_dir = project_dir / "qc__20250101_000000__ds-deadbeef"
        run_dir.mkdir(parents=True)
        forged = tmp_path / f"{alias_kind}-forged-project-meta.json"
        forged.write_text(
            '{"project_id": "attacker", "display_name": "Spoofed Display"}\n',
            encoding="utf-8",
        )
        metadata_path = project_dir / rp.PROJECT_META_FILENAME
        if alias_kind == "symlink":
            metadata_path.symlink_to(forged)
        else:
            metadata_path.hardlink_to(forged)

        manifest_path = rp.finalize_run(
            run_dir,
            skill="qc",
            status="completed",
            dataset="ds",
        )

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["metadata"]["run"]["project_id"] == "safe-project"
        assert rp.read_index(project_dir)[0]["project_id"] == "safe-project"


def test_index_append_rejects_symlink_and_hardlink_without_writing_victim(
    tmp_path: Path,
) -> None:
    for alias_kind in ("symlink", "hardlink"):
        project_dir = tmp_path / alias_kind
        project_dir.mkdir()
        victim = tmp_path / f"{alias_kind}-index-victim.jsonl"
        victim.write_text('{"keep": true}\n', encoding="utf-8")
        index_path = project_dir / rp.RUN_INDEX_FILENAME
        if alias_kind == "symlink":
            index_path.symlink_to(victim)
        else:
            index_path.hardlink_to(victim)

        try:
            rp._append_index_line(index_path, {"run_id": "forged"})
        except (OSError, RuntimeError):
            pass
        else:  # pragma: no cover - assertion branch
            raise AssertionError(f"{alias_kind} index destination was accepted")

        assert victim.read_text(encoding="utf-8") == '{"keep": true}\n'


def test_index_append_rejects_fifo_and_directory_without_blocking(tmp_path: Path) -> None:
    for entry_kind in ("fifo", "directory"):
        project_dir = tmp_path / entry_kind
        project_dir.mkdir()
        index_path = project_dir / rp.RUN_INDEX_FILENAME
        if entry_kind == "fifo":
            os.mkfifo(index_path)
        else:
            index_path.mkdir()

        try:
            rp._append_index_line(index_path, {"run_id": "forged"})
        except RuntimeError:
            pass
        else:  # pragma: no cover - assertion branch
            raise AssertionError(f"{entry_kind} index destination was accepted")


def test_index_append_concurrent_first_creation_keeps_complete_lines(tmp_path: Path) -> None:
    index_path = tmp_path / rp.RUN_INDEX_FILENAME
    run_ids = [f"run-{index}" for index in range(24)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda run_id: rp._append_index_line(index_path, {"run_id": run_id}),
                run_ids,
            )
        )

    rows = rp.read_index(tmp_path)
    assert len(rows) == len(run_ids)
    assert {row["run_id"] for row in rows} == set(run_ids)


def test_read_index_rejects_symlink_and_hardlink_metadata(tmp_path: Path) -> None:
    for alias_kind in ("symlink", "hardlink"):
        project_dir = tmp_path / alias_kind
        project_dir.mkdir()
        forged = tmp_path / f"{alias_kind}-forged-index.jsonl"
        forged.write_text('{"run_id": "attacker"}\n', encoding="utf-8")
        index_path = project_dir / rp.RUN_INDEX_FILENAME
        if alias_kind == "symlink":
            index_path.symlink_to(forged)
        else:
            index_path.hardlink_to(forged)

        assert rp.read_index(project_dir) == []


def test_read_index_rejects_fifo_directory_and_parent_alias(tmp_path: Path) -> None:
    fifo_project = tmp_path / "fifo-project"
    fifo_project.mkdir()
    os.mkfifo(fifo_project / rp.RUN_INDEX_FILENAME)
    directory_project = tmp_path / "directory-project"
    (directory_project / rp.RUN_INDEX_FILENAME).mkdir(parents=True)

    external_root = tmp_path / "external"
    (external_root / "nested").mkdir(parents=True)
    external_project = external_root / "project"
    external_project.mkdir()
    (external_project / rp.RUN_INDEX_FILENAME).write_text(
        '{"run_id": "attacker"}\n',
        encoding="utf-8",
    )
    lexical_root = tmp_path / "lexical"
    lexical_root.mkdir()
    alias = lexical_root / "alias"
    alias.symlink_to(external_root / "nested", target_is_directory=True)
    raw_project = alias / ".." / "project"

    assert rp.read_index(fifo_project) == []
    assert rp.read_index(directory_project) == []
    assert raw_project.resolve(strict=True) == external_project
    assert rp.read_index(raw_project) == []


def test_read_index_accepts_only_json_object_lines(tmp_path: Path) -> None:
    (tmp_path / rp.RUN_INDEX_FILENAME).write_text(
        "[]\n\"scalar\"\nnull\n{not-json}\n"
        '{"run_id": "real", "status": "completed"}\n',
        encoding="utf-8",
    )

    assert rp.read_index(tmp_path) == [
        {"run_id": "real", "status": "completed"}
    ]


# --------------------------------------------------------------------------- lookup + rebuild
def test_find_run_dir(tmp_path: Path):
    res = rp.resolve_run_dir(output_root=tmp_path, skill="sc-de", project_id="t1",
                             input_path="/d/pbmc.h5ad", timestamp=TS)
    found = rp.find_run_dir(tmp_path, res.run_id)
    assert found == res.run_dir
    # legacy root-level run dir
    legacy = tmp_path / "old-skill__20250101_000000__ds-legacy01"
    legacy.mkdir()
    assert rp.find_run_dir(tmp_path, legacy.name) == legacy
    # traversal rejected
    assert rp.find_run_dir(tmp_path, "../escape") is None
    assert rp.find_run_dir(tmp_path, "nope") is None


def test_find_run_dir_rejects_symlinked_project_parent(tmp_path: Path):
    storage = tmp_path / "storage" / "nested-project"
    run = storage / "qc__20250101_000000__ds-deadbeef"
    run.mkdir(parents=True)
    (tmp_path / "project-link").symlink_to(storage, target_is_directory=True)

    assert rp.find_run_dir(tmp_path, run.name) is None


def test_iter_run_dirs_covers_nested_and_legacy(tmp_path: Path):
    res = rp.resolve_run_dir(output_root=tmp_path, skill="sc-de", project_id="t1",
                             input_path="/d/pbmc.h5ad", timestamp=TS)
    legacy = tmp_path / "old__20250101_000000__ds-legacy01"
    legacy.mkdir()
    pairs = list(rp.iter_run_dirs(tmp_path))
    run_dirs = {r for _, r in pairs}
    assert res.run_dir in run_dirs
    assert legacy in run_dirs


def test_run_discovery_rejects_symlinked_output_root(tmp_path: Path) -> None:
    external_root = tmp_path / "external-output"
    project_dir = external_root / "real-project"
    run_dir = project_dir / "qc__20250101_000000__ds-deadbeef"
    run_dir.mkdir(parents=True)
    (project_dir / rp.PROJECT_META_FILENAME).write_text(
        '{"project_id": "external", "display_name": "External"}\n',
        encoding="utf-8",
    )
    output_alias = tmp_path / "output-alias"
    output_alias.symlink_to(external_root, target_is_directory=True)

    assert list(rp.iter_run_dirs(output_alias)) == []
    assert rp.find_run_dir(output_alias, run_dir.name) is None
    assert rp.list_projects(output_alias) == []


def test_run_discovery_rejects_windows_reparse_project_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "output"
    junction = output_root / "project-junction"
    run_dir = junction / "qc__20250101_000000__ds-deadbeef"
    run_dir.mkdir(parents=True)
    (junction / rp.PROJECT_META_FILENAME).write_text(
        '{"project_id": "external", "display_name": "External"}\n',
        encoding="utf-8",
    )
    junction_identity = (junction.lstat().st_dev, junction.lstat().st_ino)
    monkeypatch.setattr(
        output_claim,
        "_is_windows_reparse_point",
        lambda entry_stat: (entry_stat.st_dev, entry_stat.st_ino)
        == junction_identity,
    )

    assert list(rp.iter_run_dirs(output_root)) == []
    assert rp.find_run_dir(output_root, run_dir.name) is None
    assert rp.list_projects(output_root) == []


def test_run_discovery_preserves_output_root_symlink_dotdot_evidence(
    tmp_path: Path,
) -> None:
    lexical_root = tmp_path / "lexical"
    lexical_root.mkdir()
    external_root = tmp_path / "external"
    (external_root / "nested").mkdir(parents=True)
    output_root = external_root / "output"
    project_dir = output_root / "real-project"
    run_dir = project_dir / "qc__20250101_000000__ds-deadbeef"
    run_dir.mkdir(parents=True)
    (project_dir / rp.PROJECT_META_FILENAME).write_text(
        '{"project_id": "external", "display_name": "External"}\n',
        encoding="utf-8",
    )
    alias = lexical_root / "alias"
    alias.symlink_to(external_root / "nested", target_is_directory=True)
    raw_output_root = alias / ".." / "output"

    assert raw_output_root.resolve(strict=True) == output_root
    assert list(rp.iter_run_dirs(raw_output_root)) == []
    assert rp.find_run_dir(raw_output_root, run_dir.name) is None
    assert rp.list_projects(raw_output_root) == []


def test_iter_run_dirs_does_not_follow_symlinked_project(tmp_path: Path):
    output_root = tmp_path / "output"
    output_root.mkdir()
    external_project = tmp_path / "external-project"
    external_run = external_project / "qc__20250101_000000__ds-deadbeef"
    external_run.mkdir(parents=True)
    (output_root / "project-link").symlink_to(
        external_project,
        target_is_directory=True,
    )

    assert list(rp.iter_run_dirs(output_root)) == []


def test_iter_run_dirs_does_not_follow_symlinked_run(tmp_path: Path):
    output_root = tmp_path / "output"
    project = output_root / "project"
    project.mkdir(parents=True)
    external_run = tmp_path / "qc__20250101_000000__ds-deadbeef"
    external_run.mkdir()
    (project / external_run.name).symlink_to(
        external_run,
        target_is_directory=True,
    )

    assert list(rp.iter_run_dirs(output_root)) == []


def test_list_projects_does_not_follow_symlinked_project(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    output_root.mkdir()
    external_project = tmp_path / "external-project"
    external_project.mkdir()
    (external_project / rp.PROJECT_META_FILENAME).write_text(
        '{"project_id": "external", "display_name": "External"}\n',
        encoding="utf-8",
    )
    (output_root / "project-link").symlink_to(
        external_project,
        target_is_directory=True,
    )

    assert rp.list_projects(output_root) == []


def test_project_listing_keeps_real_default_and_legacy_projects(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    real = rp.resolve_run_dir(
        output_root=output_root,
        skill="qc",
        project_id="real-project-id",
        project_name="Real Study",
        demo=True,
        timestamp=TS,
    )
    default = rp.resolve_run_dir(
        output_root=output_root,
        skill="qc",
        demo=True,
        timestamp=TS,
    )
    legacy_project = output_root / "legacy-project"
    (legacy_project / "qc__20250101_000000__ds-deadbeef").mkdir(parents=True)

    projects = {project["project_id"]: project for project in rp.list_projects(output_root)}

    assert projects["real-project-id"]["display_name"] == "Real Study"
    assert projects[rp.DEFAULT_PROJECT_ID]["dir"] == default.project_dir.name
    assert projects["legacy-project"]["display_name"] == "legacy-project"
    assert rp.resolve_cli_project(output_root, "Real Study") == (
        "real-project-id",
        "Real Study",
    )
    assert real.run_dir.is_dir()


def test_get_current_project_rejects_symlink_and_hardlink_pointer(tmp_path: Path) -> None:
    for alias_kind in ("symlink", "hardlink"):
        output_root = tmp_path / alias_kind
        output_root.mkdir()
        forged = tmp_path / f"{alias_kind}-forged-current-project.json"
        forged.write_text(
            '{"project_id": "attacker", "display_name": "Forged"}\n',
            encoding="utf-8",
        )
        pointer = output_root / rp.CURRENT_PROJECT_FILENAME
        if alias_kind == "symlink":
            pointer.symlink_to(forged)
        else:
            pointer.hardlink_to(forged)

        assert rp.peek_current_project(output_root) == ("", "")
        assert not (output_root / ".current-project.lock").exists()
        assert rp.get_current_project(output_root) == ("", "")


def test_get_current_project_rejects_special_parent_alias_and_non_mapping(
    tmp_path: Path,
) -> None:
    fifo_root = tmp_path / "fifo-root"
    fifo_root.mkdir()
    os.mkfifo(fifo_root / rp.CURRENT_PROJECT_FILENAME)
    directory_root = tmp_path / "directory-root"
    (directory_root / rp.CURRENT_PROJECT_FILENAME).mkdir(parents=True)
    non_mapping_root = tmp_path / "non-mapping-root"
    non_mapping_root.mkdir()
    (non_mapping_root / rp.CURRENT_PROJECT_FILENAME).write_text(
        '["attacker"]\n',
        encoding="utf-8",
    )

    external_root = tmp_path / "external"
    (external_root / "nested").mkdir(parents=True)
    external_output = external_root / "output"
    external_output.mkdir()
    (external_output / rp.CURRENT_PROJECT_FILENAME).write_text(
        '{"project_id": "attacker", "display_name": "Forged"}\n',
        encoding="utf-8",
    )
    lexical_root = tmp_path / "lexical"
    lexical_root.mkdir()
    alias = lexical_root / "alias"
    alias.symlink_to(external_root / "nested", target_is_directory=True)
    raw_output_root = alias / ".." / "output"

    assert rp.get_current_project(fifo_root) == ("", "")
    assert rp.get_current_project(directory_root) == ("", "")
    assert rp.get_current_project(non_mapping_root) == ("", "")
    assert raw_output_root.resolve(strict=True) == external_output
    assert rp.get_current_project(raw_output_root) == ("", "")


def test_set_current_project_rejects_raw_output_alias_before_mkdir(tmp_path: Path) -> None:
    external_root = tmp_path / "external"
    (external_root / "nested").mkdir(parents=True)
    lexical_root = tmp_path / "lexical"
    lexical_root.mkdir()
    alias = lexical_root / "alias"
    alias.symlink_to(external_root / "nested", target_is_directory=True)
    raw_output_root = alias / ".." / "created-by-alias"
    external_target = external_root / "created-by-alias"

    try:
        rp.set_current_project(raw_output_root, "project-id", "Project")
    except RuntimeError:
        pass
    else:  # pragma: no cover - assertion branch
        raise AssertionError("aliased output root was accepted")

    assert not external_target.exists()


def test_set_current_project_rejects_destination_alias_without_writing_victim(
    tmp_path: Path,
) -> None:
    for alias_kind in ("symlink", "hardlink"):
        output_root = tmp_path / alias_kind
        output_root.mkdir()
        victim = tmp_path / f"{alias_kind}-current-project-victim.json"
        victim.write_text("KEEP\n", encoding="utf-8")
        pointer = output_root / rp.CURRENT_PROJECT_FILENAME
        if alias_kind == "symlink":
            pointer.symlink_to(victim)
        else:
            pointer.hardlink_to(victim)

        try:
            rp.set_current_project(output_root, "project-id", "Project")
        except RuntimeError:
            pass
        else:  # pragma: no cover - assertion branch
            raise AssertionError(f"{alias_kind} current pointer was accepted")

        assert victim.read_text(encoding="utf-8") == "KEEP\n"


def test_clear_current_project_rejects_symlink_and_hardlink_pointer(tmp_path: Path) -> None:
    for alias_kind in ("symlink", "hardlink"):
        output_root = tmp_path / alias_kind
        output_root.mkdir()
        victim = tmp_path / f"{alias_kind}-clear-victim.json"
        victim.write_text("KEEP\n", encoding="utf-8")
        pointer = output_root / rp.CURRENT_PROJECT_FILENAME
        if alias_kind == "symlink":
            pointer.symlink_to(victim)
        else:
            pointer.hardlink_to(victim)

        try:
            rp.clear_current_project(output_root)
        except RuntimeError:
            pass
        else:  # pragma: no cover - assertion branch
            raise AssertionError(f"{alias_kind} current pointer was removed")

        assert os.path.lexists(pointer)
        assert victim.read_text(encoding="utf-8") == "KEEP\n"


def test_clear_current_project_rejects_special_entry_and_parent_alias(
    tmp_path: Path,
) -> None:
    unsafe_pointers: list[Path] = []
    fifo_root = tmp_path / "fifo-root"
    fifo_root.mkdir()
    fifo_pointer = fifo_root / rp.CURRENT_PROJECT_FILENAME
    os.mkfifo(fifo_pointer)
    unsafe_pointers.append(fifo_pointer)
    directory_root = tmp_path / "directory-root"
    directory_pointer = directory_root / rp.CURRENT_PROJECT_FILENAME
    directory_pointer.mkdir(parents=True)
    unsafe_pointers.append(directory_pointer)

    for pointer in unsafe_pointers:
        try:
            rp.clear_current_project(pointer.parent)
        except RuntimeError:
            pass
        else:  # pragma: no cover - assertion branch
            raise AssertionError(f"unsafe pointer was removed: {pointer}")
        assert os.path.lexists(pointer)

    external_root = tmp_path / "external"
    (external_root / "nested").mkdir(parents=True)
    external_output = external_root / "output"
    external_output.mkdir()
    external_pointer = external_output / rp.CURRENT_PROJECT_FILENAME
    external_pointer.write_text("KEEP\n", encoding="utf-8")
    lexical_root = tmp_path / "lexical"
    lexical_root.mkdir()
    alias = lexical_root / "alias"
    alias.symlink_to(external_root / "nested", target_is_directory=True)
    raw_output_root = alias / ".." / "output"

    try:
        rp.clear_current_project(raw_output_root)
    except RuntimeError:
        pass
    else:  # pragma: no cover - assertion branch
        raise AssertionError("current pointer through parent alias was removed")

    assert external_pointer.read_text(encoding="utf-8") == "KEEP\n"


def test_current_project_pointer_normal_lifecycle_and_missing_clear(tmp_path: Path) -> None:
    output_root = tmp_path / "output"

    rp.clear_current_project(output_root)
    rp.set_current_project(output_root, "project-id", "Project Name")
    assert rp.get_current_project(output_root) == ("project-id", "Project Name")

    rp.clear_current_project(output_root)
    assert rp.get_current_project(output_root) == ("", "")
    rp.clear_current_project(output_root)


def test_peek_current_project_is_bounded_pure_navigation(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    output_root.mkdir()
    pointer = output_root / rp.CURRENT_PROJECT_FILENAME
    pointer.write_text(
        json.dumps({"project_id": "a" * 32, "display_name": "Current"}),
        encoding="utf-8",
    )

    assert rp.peek_current_project(output_root) == ("a" * 32, "Current")
    assert not (output_root / ".current-project.lock").exists()

    pointer.write_bytes(b"{" + b"x" * 4096 + b"}")
    assert rp.peek_current_project(output_root) == ("", "")
    assert not (output_root / ".current-project.lock").exists()


def test_concurrent_current_project_clear_is_idempotent(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    rp.set_current_project(output_root, "project-id", "Project Name")

    def _clear_and_capture(_index: int) -> BaseException | None:
        try:
            rp.clear_current_project(output_root)
        except BaseException as exc:  # pragma: no cover - surfaced below
            return exc
        return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        errors = list(pool.map(_clear_and_capture, range(16)))

    assert errors == [None] * 16
    assert rp.get_current_project(output_root) == ("", "")


def test_rebuild_index_from_walk(tmp_path: Path):
    res = rp.resolve_run_dir(output_root=tmp_path, skill="sc-de", project_id="t1",
                             input_path="/d/pbmc.h5ad", timestamp=TS)
    rp.finalize_run(res.run_dir, skill="sc-de", status="completed", dataset=res.dataset)
    # nuke the cache, then rebuild from the manifests
    (res.project_dir / rp.RUN_INDEX_FILENAME).unlink()
    n = rp.rebuild_index(res.project_dir)
    assert n == 1
    rows = rp.read_index(res.project_dir)
    assert rows[0]["run_id"] == res.run_id
    assert rows[0]["skill"] == "sc-de"


def test_read_missing_index_is_read_only(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    assert rp.read_index(project_dir) == []
    assert list(project_dir.iterdir()) == []


def test_rebuild_index_serializes_with_concurrent_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / rp.PROJECT_META_FILENAME).write_text(
        json.dumps({"project_id": "project"}),
        encoding="utf-8",
    )

    rebuild_ready = threading.Event()
    release_rebuild = threading.Event()
    append_done = threading.Event()
    errors: list[BaseException] = []
    real_atomic_write = rp.atomic_write_owned_output_text

    def _blocking_atomic_write(path, **kwargs):
        if Path(path).name == rp.RUN_INDEX_FILENAME:
            rebuild_ready.set()
            assert release_rebuild.wait(timeout=5)
        return real_atomic_write(path, **kwargs)

    monkeypatch.setattr(rp, "atomic_write_owned_output_text", _blocking_atomic_write)

    def _run_rebuild() -> None:
        try:
            rp.rebuild_index(project_dir)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def _run_append() -> None:
        try:
            rp._append_index_line(
                project_dir / rp.RUN_INDEX_FILENAME,
                {"schema_version": 1, "run_id": "concurrent-run"},
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            append_done.set()

    rebuild_thread = threading.Thread(target=_run_rebuild)
    rebuild_thread.start()
    assert rebuild_ready.wait(timeout=5)

    append_thread = threading.Thread(target=_run_append)
    append_thread.start()
    try:
        assert not append_done.wait(timeout=0.2)
    finally:
        release_rebuild.set()

    rebuild_thread.join(timeout=5)
    append_thread.join(timeout=5)
    assert not rebuild_thread.is_alive()
    assert not append_thread.is_alive()
    assert errors == []
    assert rp.read_index(project_dir) == [
        {"schema_version": 1, "run_id": "concurrent-run"}
    ]


def test_rebuild_index_does_not_follow_predictable_temp_symlink(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    victim = tmp_path / "rebuild-victim.jsonl"
    victim.write_text("KEEP\n", encoding="utf-8")
    poisoned_temp = project_dir / f".{rp.RUN_INDEX_FILENAME}.tmp-{os.getpid()}"
    poisoned_temp.symlink_to(victim)

    count = rp.rebuild_index(project_dir)

    index_path = project_dir / rp.RUN_INDEX_FILENAME
    assert count == 0
    assert victim.read_text(encoding="utf-8") == "KEEP\n"
    assert index_path.is_file() and not index_path.is_symlink()
    assert index_path.read_text(encoding="utf-8") == ""


def test_rebuild_index_rejects_destination_aliases_without_writing_victim(
    tmp_path: Path,
) -> None:
    for alias_kind in ("symlink", "hardlink"):
        project_dir = tmp_path / alias_kind
        project_dir.mkdir()
        victim = tmp_path / f"{alias_kind}-rebuild-victim.jsonl"
        victim.write_text("KEEP\n", encoding="utf-8")
        index_path = project_dir / rp.RUN_INDEX_FILENAME
        if alias_kind == "symlink":
            index_path.symlink_to(victim)
        else:
            index_path.hardlink_to(victim)

        try:
            rp.rebuild_index(project_dir)
        except RuntimeError:
            pass
        else:  # pragma: no cover - assertion branch
            raise AssertionError(f"{alias_kind} rebuild destination was accepted")

        assert victim.read_text(encoding="utf-8") == "KEEP\n"


def test_rebuild_index_cleans_random_temp_when_replace_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    def fail_replace(_source, _destination):
        raise OSError("injected replace failure")

    monkeypatch.setattr(rp.os, "replace", fail_replace)

    try:
        rp.rebuild_index(project_dir)
    except OSError as exc:
        assert "injected replace failure" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("injected replace failure was swallowed")

    assert [path.name for path in project_dir.iterdir()] == [".index.lock"]
    assert (project_dir / ".index.lock").is_file()
