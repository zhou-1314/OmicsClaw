"""Unit tests for the ADR 0035 project-scoped output resolver."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


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


def test_iter_run_dirs_covers_nested_and_legacy(tmp_path: Path):
    res = rp.resolve_run_dir(output_root=tmp_path, skill="sc-de", project_id="t1",
                             input_path="/d/pbmc.h5ad", timestamp=TS)
    legacy = tmp_path / "old__20250101_000000__ds-legacy01"
    legacy.mkdir()
    pairs = list(rp.iter_run_dirs(tmp_path))
    run_dirs = {r for _, r in pairs}
    assert res.run_dir in run_dirs
    assert legacy in run_dirs


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
