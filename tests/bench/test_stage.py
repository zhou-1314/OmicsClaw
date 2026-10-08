"""Staging: what goes into a workspace, and where the run's records are kept."""

from __future__ import annotations

import os

import pytest

from omicsclaw.bench.stage import StageError, set_aside, stage, staged_files

from ._support import Toy


def test_a_workspace_holds_the_public_files_and_nothing_of_the_oracle(tmp_path):
    """The oracle stays under the cases root. A workspace gets a copy of
    ``public/`` and the empty parent directory of each deliverable.
    """
    toy = Toy(tmp_path)
    run = toy.manifest.runs()[0]

    paths = stage(run, toy.campaign)

    files = sorted(
        path.relative_to(paths.workspace).as_posix()
        for path in paths.workspace.rglob("*")
    )
    assert files == ["data", "data/numbers.txt", "output"]
    assert not any("truth" in path.name for path in toy.out.rglob("*"))


def test_meta_is_outside_the_workspace(tmp_path):
    """The prompt and every later record live beside the workspace tree,
    so an agent confined to its workspace cannot rewrite them.
    """
    toy = Toy(tmp_path)
    run = toy.manifest.runs()[0]

    paths = stage(run, toy.campaign)

    assert paths.workspace not in paths.meta.parents
    assert paths.meta not in paths.workspace.parents
    assert paths.meta != paths.workspace
    assert paths.prompt.read_text() == run.case.prompt
    assert not list(paths.workspace.rglob("prompt.md"))
    assert set(staged_files(paths)) == {"data/numbers.txt"}


def test_a_link_in_the_public_files_is_copied_as_a_file(tmp_path):
    """A link would lead back to the cases root, next to the oracle."""
    toy = Toy(tmp_path)
    run = toy.manifest.runs()[0]
    public = toy.campaign.public(run.case)
    os.symlink(public / "data" / "numbers.txt", public / "alias.txt")

    paths = stage(run, toy.campaign)

    copied = paths.workspace / "alias.txt"
    assert copied.is_file() and not copied.is_symlink()


def test_staging_over_an_existing_run_is_refused(tmp_path):
    toy = Toy(tmp_path)
    run = toy.manifest.runs()[0]
    stage(run, toy.campaign)

    with pytest.raises(StageError, match="already exists"):
        stage(run, toy.campaign)


def test_a_case_without_public_files_cannot_be_staged(tmp_path):
    toy = Toy(tmp_path)
    run = toy.manifest.runs()[0]
    public = toy.campaign.public(run.case)
    public.rename(public.with_name("hidden"))

    with pytest.raises(StageError, match="no public directory"):
        stage(run, toy.campaign)


def test_setting_aside_renames_both_directories_alike(tmp_path):
    toy = Toy(tmp_path)
    run = toy.manifest.runs()[0]
    first = stage(run, toy.campaign)
    (first.workspace / "note.txt").write_text("first attempt")

    assert set_aside(first, "infra") == "infra1"
    second = stage(run, toy.campaign)
    assert set_aside(second, "infra") == "infra2"

    assert (first.workspace.with_name("r1.infra1") / "note.txt").read_text() == (
        "first attempt"
    )
    assert first.meta.with_name("r1.infra1").is_dir()
    assert first.meta.with_name("r1.infra2").is_dir()
    assert not first.workspace.exists() and not first.meta.exists()
    assert set_aside(first, "infra") is None
