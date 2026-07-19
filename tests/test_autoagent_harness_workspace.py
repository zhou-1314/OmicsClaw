"""Accepted-state and promotion contracts for the AutoAgent workspace."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import omicsclaw.autoagent.harness_workspace as harness_workspace_module
from omicsclaw.autoagent.harness_workspace import HarnessWorkspace
from omicsclaw.autoagent.patch_engine import FileDiff, Hunk, PatchPlan


ANALYSIS_FILE = "skills/test/analysis.py"
HELPER_FILE = "skills/test/helper.py"


def test_git_environment_scrubs_backend_control_credentials(monkeypatch) -> None:
    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "must-not-leak")
    monkeypatch.setenv("OMICSCLAW_SKILL_EVOLUTION_TOKEN", "must-not-leak")
    monkeypatch.setenv("OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD", "3")
    monkeypatch.setenv("OMICSCLAW_TEST_PRESERVED", "yes")

    environment = HarnessWorkspace._git_environment()

    assert environment["OMICSCLAW_TEST_PRESERVED"] == "yes"
    assert "OMICSCLAW_REMOTE_AUTH_TOKEN" not in environment
    assert "OMICSCLAW_SKILL_EVOLUTION_TOKEN" not in environment
    assert "OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD" not in environment


def test_protected_branch_git_probes_scrub_backend_control_credentials(
    monkeypatch,
    tmp_path,
) -> None:
    from types import SimpleNamespace

    import omicsclaw.autoagent as autoagent

    (tmp_path / ".git").mkdir()
    observed_envs: list[dict[str, str]] = []

    def fake_run(command, **kwargs):
        observed_envs.append(dict(kwargs["env"]))
        stdout = "feature/safe\n" if "rev-parse" in command else ""
        return SimpleNamespace(stdout=stdout)

    monkeypatch.setenv("OMICSCLAW_REMOTE_AUTH_TOKEN", "must-not-leak")
    monkeypatch.setenv("OMICSCLAW_SKILL_EVOLUTION_TOKEN", "must-not-leak")
    monkeypatch.setenv("OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD", "3")
    monkeypatch.setattr(autoagent.subprocess, "run", fake_run)

    assert autoagent._check_protected_branch(tmp_path) is None
    assert len(observed_envs) == 2
    for child_env in observed_envs:
        assert "OMICSCLAW_REMOTE_AUTH_TOKEN" not in child_env
        assert "OMICSCLAW_SKILL_EVOLUTION_TOKEN" not in child_env
        assert "OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD" not in child_env


def _init_source_repository(source_root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=source_root, check=True)
    subprocess.run(
        ["git", "config", "user.name", "OmicsClaw Test"],
        cwd=source_root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "omicsclaw-test@local"],
        cwd=source_root,
        check=True,
    )
    subprocess.run(["git", "add", "-f", "-A"], cwd=source_root, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "test source baseline"],
        cwd=source_root,
        check=True,
    )


def _workspace(
    tmp_path: Path,
    *,
    source_mode: int | None = None,
) -> tuple[HarnessWorkspace, Path]:
    source_root = tmp_path / "project"
    source_file = source_root / "skills" / "test" / "analysis.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("threshold = 1\n", encoding="utf-8")
    if source_mode is not None:
        source_file.chmod(source_mode)
    (source_file.parent / "helper.py").write_text(
        "scale = 10\n",
        encoding="utf-8",
    )
    _init_source_repository(source_root)

    workspace = HarnessWorkspace(source_root, tmp_path / "run")
    workspace.create()
    return workspace, source_file


def test_baseline_materializes_only_source_stage_zero_tracked_inventory(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "project"
    tracked = source_root / "tracked.py"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("value = 'committed'\n", encoding="utf-8")
    _init_source_repository(source_root)
    tracked.write_text("value = 'dirty tracked'\n", encoding="utf-8")

    ordinary = source_root / "ordinary-untracked.py"
    ordinary.write_text("raise RuntimeError('must not run')\n", encoding="utf-8")
    info_ignored = source_root / "info-ignored.py"
    info_ignored.write_text("raise RuntimeError('must not run')\n", encoding="utf-8")
    info_exclude = source_root / ".git" / "info" / "exclude"
    info_exclude.write_text(
        info_exclude.read_text(encoding="utf-8") + "info-ignored.py\n",
        encoding="utf-8",
    )
    global_ignored = source_root / "global-ignored.py"
    global_ignored.write_text("raise RuntimeError('must not run')\n", encoding="utf-8")
    global_excludes = tmp_path / "global-excludes"
    global_excludes.write_text("global-ignored.py\n", encoding="utf-8")
    subprocess.run(
        ["git", "config", "core.excludesFile", str(global_excludes)],
        cwd=source_root,
        check=True,
    )

    workspace = HarnessWorkspace(source_root, tmp_path / "run")
    workspace.create()

    tree = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", workspace.baseline_commit],
        cwd=workspace.repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert tree == ["tracked.py"]
    assert workspace.read_file_from_commit(
        workspace.baseline_commit,
        "tracked.py",
    ) == b"value = 'dirty tracked'\n"
    for path in (ordinary, info_ignored, global_ignored):
        assert not (workspace.repo_root / path.name).exists()


def test_detached_worktree_raw_bytes_ignore_source_working_tree_encoding(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "project"
    source_root.mkdir()
    (source_root / ".gitattributes").write_text(
        "tracked.txt working-tree-encoding=UTF-16\n",
        encoding="utf-8",
    )
    tracked = source_root / "tracked.txt"
    tracked.write_bytes("committed\n".encode("utf-16"))
    _init_source_repository(source_root)
    dirty_bytes = b"dirty-as-utf8\n"
    tracked.write_bytes(dirty_bytes)

    workspace = HarnessWorkspace(source_root, tmp_path / "run")
    workspace.create()
    assert workspace.read_file_from_commit(
        workspace.baseline_commit,
        "tracked.txt",
    ) == dirty_bytes

    trial_root = workspace.create_worktree(0, editable_files=[])
    try:
        assert (trial_root / "tracked.txt").read_bytes() == dirty_bytes
    finally:
        workspace.cleanup_worktree(trial_root)


def test_create_requires_source_git_top_level_without_copy_all_fallback(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "project"
    source_root.mkdir()
    (source_root / "untracked.py").write_text("value = 1\n", encoding="utf-8")
    workspace = HarnessWorkspace(source_root, tmp_path / "run")

    with pytest.raises(ValueError, match="Git top-level|committed HEAD"):
        workspace.create()

    assert workspace._created is False
    assert not workspace.repo_root.exists()


def test_create_rejects_tracked_symlink_in_source_inventory(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "project"
    source_root.mkdir()
    (source_root / "target.py").write_text("value = 1\n", encoding="utf-8")
    (source_root / "alias.py").symlink_to("target.py")
    _init_source_repository(source_root)
    workspace = HarnessWorkspace(source_root, tmp_path / "run")

    with pytest.raises(ValueError, match="unsupported entry"):
        workspace.create()

    assert workspace._created is False
    assert not workspace.repo_root.exists()


def test_create_rejects_source_index_entry_with_missing_blob(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "project"
    source_root.mkdir()
    (source_root / "base.py").write_text("value = 1\n", encoding="utf-8")
    _init_source_repository(source_root)
    (source_root / "ghost.py").write_text("value = 2\n", encoding="utf-8")
    missing_oid = "1" * 40
    subprocess.run(
        [
            "git",
            "update-index",
            "--add",
            "--info-only",
            "--cacheinfo",
            f"100644,{missing_oid},ghost.py",
        ],
        cwd=source_root,
        check=True,
    )
    workspace = HarnessWorkspace(source_root, tmp_path / "run")

    with pytest.raises(ValueError, match="index object|Git blob"):
        workspace.create()

    assert workspace._created is False
    assert not workspace.repo_root.exists()


def test_untracked_edit_target_is_rejected_before_open_state_or_worktree(
    tmp_path: Path,
) -> None:
    workspace, _source_file = _workspace(tmp_path)
    untracked = workspace.source_project_root / "skills" / "test" / "new.py"
    untracked.write_text("value = 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="governed tracked baseline|track it"):
        workspace.create_worktree(
            1,
            editable_files=["skills/test/new.py"],
        )

    state = json.loads(
        workspace.git_control_state_path.read_text(encoding="utf-8")
    )
    assert state["status"] == "clean"
    assert not (workspace.worktrees_root / "iter_0001").exists()
    assert not (workspace.repo_root / ".git" / "worktrees" / "iter_0001").exists()


def _threshold_patch(
    *,
    before: int = 1,
    after: int = 2,
    description: str = "accepted threshold",
) -> PatchPlan:
    return PatchPlan(
        target_files=[ANALYSIS_FILE],
        description=description,
        diffs=[
            FileDiff(
                file=ANALYSIS_FILE,
                hunks=[
                    Hunk(
                        old_code=f"threshold = {before}",
                        new_code=f"threshold = {after}",
                    )
                ],
            )
        ],
    )


def _two_file_patch() -> PatchPlan:
    return PatchPlan(
        target_files=[ANALYSIS_FILE, HELPER_FILE],
        description="accepted two-file patch",
        diffs=[
            FileDiff(
                file=ANALYSIS_FILE,
                hunks=[Hunk(old_code="threshold = 1", new_code="threshold = 2")],
            ),
            FileDiff(
                file=HELPER_FILE,
                hunks=[Hunk(old_code="scale = 10", new_code="scale = 20")],
            ),
        ],
    )


def _accepted_head(workspace: HarnessWorkspace) -> str:
    return subprocess.run(
        ["git", "rev-parse", workspace.accepted_branch],
        cwd=workspace.repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _unreferenced_descendant(
    workspace: HarnessWorkspace,
    parent_commit: str,
) -> str:
    tree = subprocess.run(
        ["git", "rev-parse", f"{parent_commit}^{{tree}}"],
        cwd=workspace.repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return subprocess.run(
        [
            "git",
            "commit-tree",
            tree,
            "-p",
            parent_commit,
            "-m",
            "unreferenced rejected candidate",
        ],
        cwd=workspace.repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit_accepted(
    workspace: HarnessWorkspace,
    *,
    iteration: int,
    worktree: Path,
    patch: PatchPlan,
    modified_files: list[str],
):
    workspace.freeze_candidate_patch(
        iteration=iteration,
        worktree=worktree,
        patch=patch,
        modified_files=modified_files,
    )
    return workspace.commit_accepted_patch(
        iteration=iteration,
        worktree=worktree,
        patch=patch,
        modified_files=modified_files,
    )


def test_trial_cleanup_removes_worktree_and_git_registration(
    tmp_path: Path,
) -> None:
    workspace, _source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        registration = (
            workspace.repo_root / ".git" / "worktrees" / trial_root.name
        )
        assert trial_root.is_dir()
        assert registration.is_dir()

    assert not trial_root.exists()
    assert not registration.exists()


def test_trial_cleanup_rejects_false_success_with_residual_git_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _source_file = _workspace(tmp_path)
    real_run = subprocess.run
    trial_root: Path | None = None

    def leave_worktree_registered(command, *args, **kwargs):
        if (
            command[:4] == ["git", "worktree", "remove", "--force"]
            and Path(command[4]).name == "iter_0001"
        ):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="",
                stderr="",
            )
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(
        harness_workspace_module.subprocess,
        "run",
        leave_worktree_registered,
    )
    with pytest.raises(RuntimeError, match="cleanup failed verification"):
        with workspace.trial_worktree(1, _surface_for(workspace)) as (
            active_root,
            _trial_surface,
        ):
            trial_root = active_root

    assert trial_root is not None
    registration = workspace.repo_root / ".git" / "worktrees" / trial_root.name
    assert trial_root.is_dir()
    assert registration.is_dir()
    compromise = json.loads(
        workspace.git_control_compromise_path.read_text(encoding="utf-8")
    )
    assert compromise["reason"] == "worktree_remove_incomplete"


def test_trial_cleanup_rejects_externally_deleted_accepted_worktree_authority(
    tmp_path: Path,
) -> None:
    workspace, _source_file = _workspace(tmp_path)

    with pytest.raises(RuntimeError, match="worktree cleanup"):
        with workspace.trial_worktree(1, _surface_for(workspace)) as (
            trial_root,
            _trial_surface,
        ):
            (trial_root / ANALYSIS_FILE).write_text(
                "threshold = 2\n",
                encoding="utf-8",
            )
            _commit_accepted(
                workspace,
                iteration=1,
                worktree=trial_root,
                patch=_threshold_patch(),
                modified_files=[ANALYSIS_FILE],
            )
            registration = (
                workspace.repo_root / ".git" / "worktrees" / trial_root.name
            )
            shutil.rmtree(trial_root)
            shutil.rmtree(registration)

    compromise = json.loads(
        workspace.git_control_compromise_path.read_text(encoding="utf-8")
    )
    assert compromise["reason"] == "worktree_path_missing_before_remove"
    with pytest.raises(ValueError, match="control authority is compromised"):
        workspace.durable_accepted_head_record()


def test_commit_does_not_advance_accepted_head_when_artifact_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _source_file = _workspace(tmp_path)
    previous_commit = workspace.accepted_commit

    real_writer = harness_workspace_module.atomic_write_owned_output_text

    def fail_metadata_write(*args: object, **kwargs: object) -> Path:
        if kwargs.get("label") == "accepted patch artifact":
            raise OSError("injected artifact persistence failure")
        return real_writer(*args, **kwargs)

    monkeypatch.setattr(
        harness_workspace_module,
        "atomic_write_owned_output_text",
        fail_metadata_write,
        raising=False,
    )

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        trial_file = trial_root / "skills" / "test" / "analysis.py"
        trial_file.write_text("threshold = 2\n", encoding="utf-8")
        with pytest.raises(OSError, match="artifact persistence failure"):
            _commit_accepted(workspace,
                iteration=1,
                worktree=trial_root,
                patch=_threshold_patch(description="raise threshold"),
                modified_files=["skills/test/analysis.py"],
            )

    assert workspace.accepted_commit == previous_commit
    assert _accepted_head(workspace) == previous_commit


def test_commit_cleans_partial_metadata_and_keeps_head_when_manifest_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _source_file = _workspace(tmp_path)
    previous_commit = workspace.accepted_commit
    real_writer = harness_workspace_module.atomic_write_owned_output_text

    def fail_manifest_write(*args: object, **kwargs: object) -> Path:
        if kwargs.get("label") == "accepted patch manifest":
            raise OSError("injected manifest persistence failure")
        return real_writer(*args, **kwargs)

    monkeypatch.setattr(
        harness_workspace_module,
        "atomic_write_owned_output_text",
        fail_manifest_write,
    )

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        trial_file = trial_root / "skills" / "test" / "analysis.py"
        trial_file.write_text("threshold = 2\n", encoding="utf-8")
        with pytest.raises(OSError, match="manifest persistence failure"):
            _commit_accepted(workspace,
                iteration=1,
                worktree=trial_root,
                patch=_threshold_patch(description="raise threshold"),
                modified_files=["skills/test/analysis.py"],
            )

    assert workspace.accepted_commit == previous_commit
    assert _accepted_head(workspace) == previous_commit
    assert list(workspace.accepted_artifacts_root.iterdir()) == []


def test_promotion_uses_last_successful_record_not_mutable_commit_cache(
    tmp_path: Path,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )

    # A mutable cache/member must not become promotion authority.  Only the
    # successful AcceptedPatchRecord passed by the harness is authoritative.
    workspace.accepted_commit = "f" * 40
    result = workspace.promote_accepted_state(
        accepted_patch=accepted_record,
    )

    assert result.status == "applied"
    assert source_file.read_text(encoding="utf-8") == "threshold = 2\n"


def test_promotion_rejects_record_for_commit_outside_accepted_branch(
    tmp_path: Path,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )
        # Create the unreferenced object while the owned trial transaction is
        # still open, so the subsequent clean checkpoint accounts for it.
        rejected_commit = _unreferenced_descendant(
            workspace,
            accepted_record.commit_hash,
        )

    forged_record = replace(accepted_record, commit_hash=rejected_commit)
    with pytest.raises(ValueError, match="accepted branch"):
        workspace.promote_accepted_state(
            accepted_patch=forged_record,
        )

    assert source_file.read_text(encoding="utf-8") == "threshold = 1\n"
    assert not workspace.promotion_journal_path.exists()


@pytest.mark.parametrize("missing_name", ["manifest_path", "artifact_path"])
def test_promotion_requires_each_durable_final_record_artifact(
    tmp_path: Path,
    missing_name: str,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )

    Path(getattr(accepted_record, missing_name)).unlink()

    with pytest.raises(ValueError, match="[Dd]urable accepted"):
        workspace.promote_accepted_state(
            accepted_patch=accepted_record,
        )

    assert source_file.read_text(encoding="utf-8") == "threshold = 1\n"
    assert not workspace.promotion_journal_path.exists()


@pytest.mark.parametrize("changed_name", ["manifest_path", "artifact_path"])
def test_promotion_rejects_changed_durable_final_record_artifact(
    tmp_path: Path,
    changed_name: str,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )

    changed_path = Path(getattr(accepted_record, changed_name))
    changed_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="[Dd]urable accepted"):
        workspace.promote_accepted_state(
            accepted_patch=accepted_record,
        )

    assert source_file.read_text(encoding="utf-8") == "threshold = 1\n"
    assert not workspace.promotion_journal_path.exists()


def test_promotion_rejects_mutated_record_with_accepted_head_commit(
    tmp_path: Path,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )

    changed_record = replace(
        accepted_record,
        description="different in-memory description",
    )
    with pytest.raises(ValueError, match="exact durable accepted record"):
        workspace.promote_accepted_state(accepted_patch=changed_record)

    assert source_file.read_text(encoding="utf-8") == "threshold = 1\n"
    assert not workspace.promotion_journal_path.exists()


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_promotion_rejects_aliased_durable_manifest(
    tmp_path: Path,
    alias_kind: str,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )

    manifest_path = Path(accepted_record.manifest_path)
    if alias_kind == "symlink":
        relocated = tmp_path / "relocated-manifest.json"
        manifest_path.replace(relocated)
        manifest_path.symlink_to(relocated)
    else:
        (manifest_path.parent / "manifest-hardlink.json").hardlink_to(manifest_path)

    with pytest.raises(ValueError, match="[Dd]urable accepted manifest"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert source_file.read_text(encoding="utf-8") == "threshold = 1\n"
    assert not workspace.promotion_journal_path.exists()


def test_promotion_derives_files_from_durable_git_state(
    tmp_path: Path,
) -> None:
    workspace, source_file = _workspace(tmp_path)
    helper_file = source_file.with_name("helper.py")

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )

    result = workspace.promote_accepted_state(
        accepted_patch=accepted_record,
    )

    assert result.status == "applied"
    assert result.promoted_files == ["skills/test/analysis.py"]
    assert source_file.read_text(encoding="utf-8") == "threshold = 2\n"
    assert helper_file.read_text(encoding="utf-8") == "scale = 10\n"


def test_promotion_requires_durable_artifacts_for_entire_accepted_chain(
    tmp_path: Path,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        first_root,
        _first_surface,
    ):
        first_file = first_root / "skills" / "test" / "analysis.py"
        first_file.write_text("threshold = 2\n", encoding="utf-8")
        first_record = _commit_accepted(workspace,
            iteration=1,
            worktree=first_root,
            patch=_threshold_patch(description="first accepted threshold"),
            modified_files=["skills/test/analysis.py"],
        )

    with workspace.trial_worktree(2, _surface_for(workspace)) as (
        second_root,
        _second_surface,
    ):
        second_file = second_root / "skills" / "test" / "analysis.py"
        second_file.write_text("threshold = 3\n", encoding="utf-8")
        second_record = _commit_accepted(workspace,
            iteration=2,
            worktree=second_root,
            patch=_threshold_patch(before=2, after=3, description="second accepted threshold"),
            modified_files=["skills/test/analysis.py"],
        )

    Path(first_record.manifest_path).unlink()

    with pytest.raises(ValueError, match="[Dd]urable accepted"):
        workspace.promote_accepted_state(
            accepted_patch=second_record,
        )

    assert source_file.read_text(encoding="utf-8") == "threshold = 1\n"
    assert not workspace.promotion_journal_path.exists()


def test_promotion_cas_preserves_external_change_after_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )

    real_read = workspace.read_file_from_commit
    changed = False

    def change_source_after_preflight(commit_hash: str, rel_path: str) -> bytes:
        nonlocal changed
        content = real_read(commit_hash, rel_path)
        if commit_hash == accepted_record.commit_hash and not changed:
            source_file.write_text("threshold = external\n", encoding="utf-8")
            changed = True
        return content

    monkeypatch.setattr(workspace, "read_file_from_commit", change_source_after_preflight)
    result = workspace.promote_accepted_state(
        accepted_patch=accepted_record,
    )

    assert result.status == "blocked"
    assert result.blocked_files == ["skills/test/analysis.py"]
    assert source_file.read_text(encoding="utf-8") == "threshold = external\n"


def test_promotion_blocks_source_mode_drift_from_authenticated_baseline(
    tmp_path: Path,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / ANALYSIS_FILE
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=[ANALYSIS_FILE],
        )

    baseline_mode = source_file.stat().st_mode & 0o7777
    drifted_mode = baseline_mode ^ 0o100
    source_file.chmod(drifted_mode)

    result = workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert result.status == "blocked"
    assert result.blocked_files == [ANALYSIS_FILE]
    assert source_file.read_text(encoding="utf-8") == "threshold = 1\n"
    assert source_file.stat().st_mode & 0o7777 == drifted_mode
    assert not list(source_file.parent.glob(".*.omicsclaw-*"))


def test_promotion_rejects_non_target_tracked_drift_before_source_mutation(
    tmp_path: Path,
) -> None:
    workspace, source_file = _workspace(tmp_path)
    helper_file = source_file.with_name("helper.py")

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        (accepted_root / ANALYSIS_FILE).write_text(
            "threshold = 2\n",
            encoding="utf-8",
        )
        accepted_record = _commit_accepted(
            workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=[ANALYSIS_FILE],
        )

    helper_file.write_text("scale = 999\n", encoding="utf-8")
    with pytest.raises(ValueError, match="tracked state changed"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert source_file.read_text(encoding="utf-8") == "threshold = 1\n"
    assert helper_file.read_text(encoding="utf-8") == "scale = 999\n"
    assert not workspace.promotion_journal_path.exists()


def test_promotion_allows_non_target_mode_drift_within_git_exec_class(
    tmp_path: Path,
) -> None:
    workspace, source_file = _workspace(tmp_path)
    helper_file = source_file.with_name("helper.py")

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        (accepted_root / ANALYSIS_FILE).write_text(
            "threshold = 2\n",
            encoding="utf-8",
        )
        accepted_record = _commit_accepted(
            workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=[ANALYSIS_FILE],
        )

    helper_file.chmod(0o600)
    result = workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert result.status == "applied"
    assert source_file.read_text(encoding="utf-8") == "threshold = 2\n"
    assert helper_file.read_text(encoding="utf-8") == "scale = 10\n"
    assert helper_file.stat().st_mode & 0o7777 == 0o600


def test_promotion_rechecks_non_target_tracked_state_before_first_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)
    helper_file = source_file.with_name("helper.py")

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        (accepted_root / ANALYSIS_FILE).write_text(
            "threshold = 2\n",
            encoding="utf-8",
        )
        accepted_record = _commit_accepted(
            workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=[ANALYSIS_FILE],
        )

    real_check = workspace._require_source_tracked_state_matches_baseline
    check_count = 0

    def drift_after_initial_check(**kwargs: object) -> None:
        nonlocal check_count
        check_count += 1
        real_check(**kwargs)
        if check_count == 1:
            helper_file.write_text("scale = 999\n", encoding="utf-8")

    monkeypatch.setattr(
        workspace,
        "_require_source_tracked_state_matches_baseline",
        drift_after_initial_check,
    )
    result = workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert check_count >= 2
    assert result.status == "blocked"
    assert source_file.read_text(encoding="utf-8") == "threshold = 1\n"
    assert helper_file.read_text(encoding="utf-8") == "scale = 999\n"


def test_promotion_rolls_back_when_non_target_drifts_after_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)
    helper_file = source_file.with_name("helper.py")

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        (accepted_root / ANALYSIS_FILE).write_text(
            "threshold = 2\n",
            encoding="utf-8",
        )
        accepted_record = _commit_accepted(
            workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=[ANALYSIS_FILE],
        )

    real_install = workspace._install_entry_cas

    def install_then_drift(entry: object) -> None:
        real_install(entry)
        helper_file.write_text("scale = 999\n", encoding="utf-8")

    monkeypatch.setattr(workspace, "_install_entry_cas", install_then_drift)
    result = workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert result.status == "rolled_back"
    assert source_file.read_text(encoding="utf-8") == "threshold = 1\n"
    assert helper_file.read_text(encoding="utf-8") == "scale = 999\n"


@pytest.mark.parametrize("source_mode", [0o600, 0o700])
def test_promotion_preserves_private_mode_in_matching_git_exec_class(
    tmp_path: Path,
    source_mode: int,
) -> None:
    workspace, source_file = _workspace(tmp_path, source_mode=source_mode)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / ANALYSIS_FILE
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=[ANALYSIS_FILE],
        )

    result = workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert result.status == "applied"
    assert source_file.read_text(encoding="utf-8") == "threshold = 2\n"
    assert source_file.stat().st_mode & 0o7777 == source_mode


def test_promotion_rejects_windows_reparse_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )

    parent_identity = (
        source_file.parent.lstat().st_dev,
        source_file.parent.lstat().st_ino,
    )
    monkeypatch.setattr(
        harness_workspace_module,
        "stat_is_filesystem_alias",
        lambda entry_stat: (entry_stat.st_dev, entry_stat.st_ino)
        == parent_identity,
        raising=False,
    )

    with pytest.raises(ValueError, match="plain directory"):
        workspace.promote_accepted_state(
            accepted_patch=accepted_record,
        )

    assert source_file.read_text(encoding="utf-8") == "threshold = 1\n"


def test_multi_file_promotion_rolls_back_when_second_install_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)
    helper_file = source_file.with_name("helper.py")

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_analysis = accepted_root / "skills" / "test" / "analysis.py"
        accepted_helper = accepted_analysis.with_name("helper.py")
        accepted_analysis.write_text("threshold = 2\n", encoding="utf-8")
        accepted_helper.write_text("scale = 20\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_two_file_patch(),
            modified_files=[
                "skills/test/analysis.py",
                "skills/test/helper.py",
            ],
        )

    real_link = harness_workspace_module.os.link
    link_calls = 0

    def fail_second_install(*args: object, **kwargs: object) -> None:
        nonlocal link_calls
        link_calls += 1
        if link_calls == 2:
            raise OSError("injected second-file install failure")
        real_link(*args, **kwargs)

    monkeypatch.setattr(harness_workspace_module.os, "link", fail_second_install)
    result = workspace.promote_accepted_state(
        accepted_patch=accepted_record,
    )

    assert result.status == "rolled_back"
    assert result.promoted_files == []
    assert source_file.read_text(encoding="utf-8") == "threshold = 1\n"
    assert helper_file.read_text(encoding="utf-8") == "scale = 10\n"
    journal = json.loads(workspace.promotion_journal_path.read_text(encoding="utf-8"))
    assert journal["status"] == "rolled_back"
    assert journal["applied_files"] == []
    assert not list(source_file.parent.glob(".*.omicsclaw-*"))


def test_workspace_rejects_alias_output_root(tmp_path: Path) -> None:
    source_root = tmp_path / "project"
    source_root.mkdir()
    real_output = tmp_path / "real-run"
    real_output.mkdir()
    alias_output = tmp_path / "run-alias"
    alias_output.symlink_to(real_output, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link"):
        HarnessWorkspace(source_root, alias_output)


def test_retry_recovers_interrupted_multi_file_promotion_from_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)
    helper_file = source_file.with_name("helper.py")

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_analysis = accepted_root / "skills" / "test" / "analysis.py"
        accepted_helper = accepted_analysis.with_name("helper.py")
        accepted_analysis.write_text("threshold = 2\n", encoding="utf-8")
        accepted_helper.write_text("scale = 20\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_two_file_patch(),
            modified_files=[
                "skills/test/analysis.py",
                "skills/test/helper.py",
            ],
        )

    real_write_journal = workspace._write_promotion_journal
    interrupted = False

    def interrupt_after_first_install(payload: dict[str, object]) -> None:
        nonlocal interrupted
        if (
            not interrupted
            and payload.get("status") == "applying"
            and payload.get("applied_files")
            == ["skills/test/analysis.py"]
        ):
            interrupted = True
            raise SystemExit("injected process interruption")
        real_write_journal(payload)

    monkeypatch.setattr(
        workspace,
        "_write_promotion_journal",
        interrupt_after_first_install,
    )
    with pytest.raises(SystemExit, match="process interruption"):
        workspace.promote_accepted_state(
            accepted_patch=accepted_record,
        )

    monkeypatch.setattr(workspace, "_write_promotion_journal", real_write_journal)
    result = workspace.promote_accepted_state(
        accepted_patch=accepted_record,
    )

    assert result.status == "applied"
    assert source_file.read_text(encoding="utf-8") == "threshold = 2\n"
    assert helper_file.read_text(encoding="utf-8") == "scale = 20\n"
    assert not list(source_file.parent.glob(".*.omicsclaw-*"))


def test_retry_recovers_linked_target_when_interrupted_before_stage_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )

    real_link = harness_workspace_module.os.link

    def interrupt_after_link(*args: object, **kwargs: object) -> None:
        real_link(*args, **kwargs)
        raise SystemExit("injected interruption after install link")

    monkeypatch.setattr(harness_workspace_module.os, "link", interrupt_after_link)
    with pytest.raises(SystemExit, match="after install link"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    journal = json.loads(
        workspace.promotion_journal_path.read_text(encoding="utf-8")
    )
    stage = source_file.with_name(journal["entries"][0]["stage_name"])
    assert source_file.stat().st_ino == stage.stat().st_ino
    assert source_file.stat().st_nlink == 2

    monkeypatch.setattr(harness_workspace_module.os, "link", real_link)
    result = workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert result.status == "applied"
    assert source_file.read_text(encoding="utf-8") == "threshold = 2\n"
    assert not list(source_file.parent.glob(".*.omicsclaw-*"))


def test_interrupted_recovery_blocks_non_target_tracked_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)
    helper_file = source_file.with_name("helper.py")

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        (accepted_root / ANALYSIS_FILE).write_text(
            "threshold = 2\n",
            encoding="utf-8",
        )
        accepted_record = _commit_accepted(
            workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=[ANALYSIS_FILE],
        )

    real_link = harness_workspace_module.os.link

    def interrupt_after_link(*args: object, **kwargs: object) -> None:
        real_link(*args, **kwargs)
        raise SystemExit("injected interruption after install link")

    monkeypatch.setattr(harness_workspace_module.os, "link", interrupt_after_link)
    with pytest.raises(SystemExit, match="after install link"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    helper_file.write_text("scale = 999\n", encoding="utf-8")
    monkeypatch.setattr(harness_workspace_module.os, "link", real_link)
    result = workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert result.status == "recovery_required"
    assert source_file.read_text(encoding="utf-8") == "threshold = 2\n"
    assert helper_file.read_text(encoding="utf-8") == "scale = 999\n"
    journal = json.loads(
        workspace.promotion_journal_path.read_text(encoding="utf-8")
    )
    assert journal["status"] == "recovery_required"
    stage = source_file.with_name(journal["entries"][0]["stage_name"])
    assert stage.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "schema",
        "source_project_root",
        "sandbox_repo",
        "baseline_commit",
        "accepted_commit",
        "files",
        "entry_path",
        "stage_name",
        "expected_digest",
        "accepted_digest",
        "parent_chain_identities",
    ],
)
def test_interrupted_recovery_rejects_mutable_journal_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )

    real_link = harness_workspace_module.os.link

    def interrupt_after_link(*args: object, **kwargs: object) -> None:
        real_link(*args, **kwargs)
        raise SystemExit("injected interruption after install link")

    monkeypatch.setattr(harness_workspace_module.os, "link", interrupt_after_link)
    with pytest.raises(SystemExit, match="after install link"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)
    monkeypatch.setattr(harness_workspace_module.os, "link", real_link)

    journal = json.loads(
        workspace.promotion_journal_path.read_text(encoding="utf-8")
    )
    stage = source_file.with_name(journal["entries"][0]["stage_name"])
    backup = source_file.with_name(journal["entries"][0]["backup_name"])
    before_identities = {
        path: (path.stat().st_dev, path.stat().st_ino, path.stat().st_nlink)
        for path in (source_file, stage, backup)
    }
    if mutation == "schema":
        journal["unexpected"] = True
    elif mutation == "source_project_root":
        journal["source_project_root"] = str(tmp_path / "other-project")
    elif mutation == "sandbox_repo":
        journal["sandbox_repo"] = str(tmp_path / "other-sandbox")
    elif mutation == "baseline_commit":
        journal["baseline_commit"] = "0" * 40
    elif mutation == "accepted_commit":
        journal["accepted_commit"] = "0" * 40
    elif mutation == "files":
        journal["files"] = []
    elif mutation == "entry_path":
        journal["entries"][0]["path"] = "skills/test/helper.py"
    elif mutation == "stage_name":
        journal["entries"][0]["stage_name"] = "unbound.new"
    elif mutation == "expected_digest":
        journal["entries"][0]["expected_digest"] = "0" * 64
    elif mutation == "parent_chain_identities":
        journal["entries"][0]["parent_chain_identities"] = []
    else:
        journal["entries"][0]["accepted_digest"] = "0" * 64
    workspace.promotion_journal_path.write_text(
        json.dumps(journal),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Interrupted promotion"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert {
        path: (path.stat().st_dev, path.stat().st_ino, path.stat().st_nlink)
        for path in (source_file, stage, backup)
    } == before_identities
    assert source_file.read_text(encoding="utf-8") == "threshold = 2\n"
    assert stage.read_text(encoding="utf-8") == "threshold = 2\n"
    assert backup.read_text(encoding="utf-8") == "threshold = 1\n"


def test_interrupted_recovery_rejects_mode_outside_git_baseline_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / ANALYSIS_FILE
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=[ANALYSIS_FILE],
        )

    real_write_journal = workspace._write_promotion_journal

    def interrupt_after_prepared_journal(payload: dict[str, object]) -> None:
        real_write_journal(payload)
        if payload.get("status") == "prepared":
            raise SystemExit("injected interruption after prepared journal")

    monkeypatch.setattr(
        workspace,
        "_write_promotion_journal",
        interrupt_after_prepared_journal,
    )
    with pytest.raises(SystemExit, match="after prepared journal"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)
    monkeypatch.setattr(workspace, "_write_promotion_journal", real_write_journal)

    journal = json.loads(
        workspace.promotion_journal_path.read_text(encoding="utf-8")
    )
    stage = source_file.with_name(journal["entries"][0]["stage_name"])
    drifted_mode = journal["entries"][0]["expected_mode"] ^ 0o100
    source_file.chmod(drifted_mode)
    stage.chmod(drifted_mode)
    journal["entries"][0]["expected_mode"] = drifted_mode
    workspace.promotion_journal_path.write_text(
        json.dumps(journal),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Interrupted promotion entry"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert stage.exists()
    assert source_file.read_text(encoding="utf-8") == "threshold = 1\n"


def test_interrupted_recovery_requires_exact_durable_accepted_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )

    real_link = harness_workspace_module.os.link

    def interrupt_after_link(*args: object, **kwargs: object) -> None:
        real_link(*args, **kwargs)
        raise SystemExit("injected interruption after install link")

    monkeypatch.setattr(harness_workspace_module.os, "link", interrupt_after_link)
    with pytest.raises(SystemExit, match="after install link"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)
    monkeypatch.setattr(harness_workspace_module.os, "link", real_link)
    changed_record = replace(accepted_record, description="mutable retry record")
    before_identity = (source_file.stat().st_dev, source_file.stat().st_ino)

    with pytest.raises(ValueError, match="exact durable accepted record"):
        workspace.promote_accepted_state(accepted_patch=changed_record)

    assert (source_file.stat().st_dev, source_file.stat().st_ino) == before_identity
    assert source_file.read_text(encoding="utf-8") == "threshold = 2\n"


def test_interrupted_recovery_rejects_a_third_hard_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )

    real_link = harness_workspace_module.os.link

    def interrupt_after_link(*args: object, **kwargs: object) -> None:
        real_link(*args, **kwargs)
        raise SystemExit("injected interruption after install link")

    monkeypatch.setattr(harness_workspace_module.os, "link", interrupt_after_link)
    with pytest.raises(SystemExit, match="after install link"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)
    monkeypatch.setattr(harness_workspace_module.os, "link", real_link)

    journal = json.loads(
        workspace.promotion_journal_path.read_text(encoding="utf-8")
    )
    stage = source_file.with_name(journal["entries"][0]["stage_name"])
    third_link = source_file.with_name("third-link.py")
    third_link.hardlink_to(source_file)

    result = workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert result.status == "recovery_required"
    assert source_file.stat().st_nlink == 3
    assert source_file.stat().st_ino == stage.stat().st_ino
    assert source_file.stat().st_ino == third_link.stat().st_ino


def test_link_unlink_recovery_rejects_same_digest_replacement_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / ANALYSIS_FILE
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=[ANALYSIS_FILE],
        )

    real_unlink = Path.unlink

    def interrupt_after_stage_unlink(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        real_unlink(path, *args, **kwargs)
        if path.name.endswith(".new"):
            raise SystemExit("injected interruption after stage unlink")

    monkeypatch.setattr(Path, "unlink", interrupt_after_stage_unlink)
    with pytest.raises(SystemExit, match="after stage unlink"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)
    monkeypatch.setattr(Path, "unlink", real_unlink)

    journal = json.loads(
        workspace.promotion_journal_path.read_text(encoding="utf-8")
    )
    raw_entry = journal["entries"][0]
    assert raw_entry["stage_identity"] is not None
    assert raw_entry["installed_identity"] is None
    backup = source_file.with_name(raw_entry["backup_name"])
    assert backup.read_text(encoding="utf-8") == "threshold = 1\n"

    replacement = source_file.with_name("same-digest-replacement.py")
    replacement.write_text("threshold = 2\n", encoding="utf-8")
    replacement.chmod(raw_entry["expected_mode"])
    replacement.replace(source_file)

    result = workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert result.status == "recovery_required"
    assert source_file.read_text(encoding="utf-8") == "threshold = 2\n"
    assert backup.read_text(encoding="utf-8") == "threshold = 1\n"


def test_retry_of_durable_applied_promotion_is_idempotent(
    tmp_path: Path,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )

    first = workspace.promote_accepted_state(accepted_patch=accepted_record)
    first_identity = (source_file.stat().st_dev, source_file.stat().st_ino)
    second = workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert first.status == "applied"
    assert second.status == "applied"
    assert second.promoted_files == ["skills/test/analysis.py"]
    assert (source_file.stat().st_dev, source_file.stat().st_ino) == first_identity
    assert source_file.read_text(encoding="utf-8") == "threshold = 2\n"


def test_retry_of_applied_journal_validates_and_cleans_durable_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )

    real_cleanup = workspace._cleanup_promotion_entries

    def interrupt_before_cleanup(entries: object) -> None:
        raise SystemExit("injected interruption before applied cleanup")

    monkeypatch.setattr(
        workspace,
        "_cleanup_promotion_entries",
        interrupt_before_cleanup,
    )
    with pytest.raises(SystemExit, match="before applied cleanup"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    journal = json.loads(
        workspace.promotion_journal_path.read_text(encoding="utf-8")
    )
    backup = source_file.with_name(journal["entries"][0]["backup_name"])
    assert journal["status"] == "applied"
    assert backup.read_text(encoding="utf-8") == "threshold = 1\n"

    monkeypatch.setattr(workspace, "_cleanup_promotion_entries", real_cleanup)
    result = workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert result.status == "applied"
    assert result.promoted_files == ["skills/test/analysis.py"]
    assert source_file.read_text(encoding="utf-8") == "threshold = 2\n"
    assert not list(source_file.parent.glob(".*.omicsclaw-*"))


def test_retry_of_applied_journal_blocks_non_target_drift_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)
    helper_file = source_file.with_name("helper.py")

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        (accepted_root / ANALYSIS_FILE).write_text(
            "threshold = 2\n",
            encoding="utf-8",
        )
        accepted_record = _commit_accepted(
            workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=[ANALYSIS_FILE],
        )

    real_cleanup = workspace._cleanup_promotion_entries

    def interrupt_before_cleanup(entries: object) -> None:
        raise SystemExit("injected interruption before applied cleanup")

    monkeypatch.setattr(
        workspace,
        "_cleanup_promotion_entries",
        interrupt_before_cleanup,
    )
    with pytest.raises(SystemExit, match="before applied cleanup"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    journal = json.loads(
        workspace.promotion_journal_path.read_text(encoding="utf-8")
    )
    backup = source_file.with_name(journal["entries"][0]["backup_name"])
    helper_file.write_text("scale = 999\n", encoding="utf-8")
    monkeypatch.setattr(workspace, "_cleanup_promotion_entries", real_cleanup)
    result = workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert result.status == "recovery_required"
    assert source_file.read_text(encoding="utf-8") == "threshold = 2\n"
    assert helper_file.read_text(encoding="utf-8") == "scale = 999\n"
    assert backup.read_text(encoding="utf-8") == "threshold = 1\n"
    journal = json.loads(
        workspace.promotion_journal_path.read_text(encoding="utf-8")
    )
    assert journal["status"] == "recovery_required"


def test_retry_of_applied_journal_preserves_drifted_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )

    real_cleanup = workspace._cleanup_promotion_entries

    def interrupt_before_cleanup(entries: object) -> None:
        raise SystemExit("injected interruption before applied cleanup")

    monkeypatch.setattr(
        workspace,
        "_cleanup_promotion_entries",
        interrupt_before_cleanup,
    )
    with pytest.raises(SystemExit, match="before applied cleanup"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    journal = json.loads(
        workspace.promotion_journal_path.read_text(encoding="utf-8")
    )
    backup = source_file.with_name(journal["entries"][0]["backup_name"])
    backup.write_text("threshold = external-backup\n", encoding="utf-8")
    monkeypatch.setattr(workspace, "_cleanup_promotion_entries", real_cleanup)

    with pytest.raises(RuntimeError, match="backup state drifted"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert source_file.read_text(encoding="utf-8") == "threshold = 2\n"
    assert backup.read_text(encoding="utf-8") == "threshold = external-backup\n"


def test_retry_of_applied_cleanup_preserves_backup_when_target_identity_drifts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / ANALYSIS_FILE
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=[ANALYSIS_FILE],
        )

    real_cleanup = workspace._cleanup_promotion_entries

    def interrupt_before_cleanup(entries: object) -> None:
        raise SystemExit("injected interruption before applied cleanup")

    monkeypatch.setattr(
        workspace,
        "_cleanup_promotion_entries",
        interrupt_before_cleanup,
    )
    with pytest.raises(SystemExit, match="before applied cleanup"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    journal = json.loads(
        workspace.promotion_journal_path.read_text(encoding="utf-8")
    )
    raw_entry = journal["entries"][0]
    backup = source_file.with_name(raw_entry["backup_name"])
    replacement = source_file.with_name("same-content-retry-target.py")
    replacement.write_text("threshold = 2\n", encoding="utf-8")
    replacement.chmod(raw_entry["expected_mode"])
    replacement.replace(source_file)

    monkeypatch.setattr(workspace, "_cleanup_promotion_entries", real_cleanup)
    with pytest.raises(RuntimeError, match="source state drifted"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert source_file.read_text(encoding="utf-8") == "threshold = 2\n"
    assert backup.read_text(encoding="utf-8") == "threshold = 1\n"


def test_applied_cleanup_preserves_replaced_backup_and_requires_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )

    real_write_journal = workspace._write_promotion_journal
    replaced = False

    def replace_backup_after_applied(payload: dict[str, object]) -> None:
        nonlocal replaced
        real_write_journal(payload)
        if payload.get("status") == "applied" and not replaced:
            replaced = True
            entries = payload["entries"]
            assert isinstance(entries, list)
            backup = source_file.with_name(entries[0]["backup_name"])
            replacement = source_file.with_name("replacement-backup.py")
            replacement.write_text("threshold = 1\n", encoding="utf-8")
            replacement.replace(backup)

    monkeypatch.setattr(
        workspace,
        "_write_promotion_journal",
        replace_backup_after_applied,
    )
    result = workspace.promote_accepted_state(accepted_patch=accepted_record)

    journal = json.loads(
        workspace.promotion_journal_path.read_text(encoding="utf-8")
    )
    backup = source_file.with_name(journal["entries"][0]["backup_name"])
    assert result.status == "recovery_required"
    assert journal["status"] == "recovery_required"
    assert source_file.read_text(encoding="utf-8") == "threshold = 2\n"
    assert backup.read_text(encoding="utf-8") == "threshold = 1\n"


def test_first_applied_cleanup_preserves_backup_when_target_identity_drifts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / ANALYSIS_FILE
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=[ANALYSIS_FILE],
        )

    real_write_journal = workspace._write_promotion_journal
    replaced = False

    def replace_target_after_applied(payload: dict[str, object]) -> None:
        nonlocal replaced
        real_write_journal(payload)
        if payload.get("status") == "applied" and not replaced:
            replaced = True
            replacement = source_file.with_name("replacement-target.py")
            replacement.write_text("threshold = 2\n", encoding="utf-8")
            replacement.replace(source_file)

    monkeypatch.setattr(
        workspace,
        "_write_promotion_journal",
        replace_target_after_applied,
    )
    result = workspace.promote_accepted_state(accepted_patch=accepted_record)

    journal = json.loads(
        workspace.promotion_journal_path.read_text(encoding="utf-8")
    )
    backup = source_file.with_name(journal["entries"][0]["backup_name"])
    assert result.status == "recovery_required"
    assert journal["status"] == "recovery_required"
    assert source_file.read_text(encoding="utf-8") == "threshold = 2\n"
    assert backup.read_text(encoding="utf-8") == "threshold = 1\n"


def test_retry_of_applied_journal_requires_exact_durable_record(
    tmp_path: Path,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )

    workspace.promote_accepted_state(accepted_patch=accepted_record)
    changed_record = replace(accepted_record, description="mutable retry record")

    with pytest.raises(ValueError, match="exact durable accepted record"):
        workspace.promote_accepted_state(accepted_patch=changed_record)

    assert source_file.read_text(encoding="utf-8") == "threshold = 2\n"


def test_retry_of_applied_journal_cleans_only_exact_linked_stage(
    tmp_path: Path,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )

    workspace.promote_accepted_state(accepted_patch=accepted_record)
    journal = json.loads(
        workspace.promotion_journal_path.read_text(encoding="utf-8")
    )
    stage = source_file.with_name(journal["entries"][0]["stage_name"])
    stage.hardlink_to(source_file)
    assert source_file.stat().st_nlink == 2

    result = workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert result.status == "applied"
    assert source_file.stat().st_nlink == 1
    assert not stage.exists()


def test_retry_of_applied_journal_preserves_unrelated_stage(
    tmp_path: Path,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )

    workspace.promote_accepted_state(accepted_patch=accepted_record)
    journal = json.loads(
        workspace.promotion_journal_path.read_text(encoding="utf-8")
    )
    stage = source_file.with_name(journal["entries"][0]["stage_name"])
    stage.write_text("threshold = 2\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="exact journal-owned hard-link pair"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert source_file.read_text(encoding="utf-8") == "threshold = 2\n"
    assert stage.read_text(encoding="utf-8") == "threshold = 2\n"


def test_retry_of_applied_promotion_rejects_source_drift(
    tmp_path: Path,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )

    workspace.promote_accepted_state(accepted_patch=accepted_record)
    source_file.write_text("threshold = external\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="source state drifted"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert source_file.read_text(encoding="utf-8") == "threshold = external\n"


def test_retry_of_applied_promotion_rejects_installed_identity_drift(
    tmp_path: Path,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )

    workspace.promote_accepted_state(accepted_patch=accepted_record)
    installed_identity = (source_file.stat().st_dev, source_file.stat().st_ino)
    replacement = source_file.with_name("replacement.py")
    replacement.write_text("threshold = 2\n", encoding="utf-8")
    replacement.replace(source_file)
    assert (source_file.stat().st_dev, source_file.stat().st_ino) != (
        installed_identity
    )

    with pytest.raises(RuntimeError, match="source state drifted"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert source_file.read_text(encoding="utf-8") == "threshold = 2\n"


def test_retry_of_applied_promotion_rejects_mode_drift(
    tmp_path: Path,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / ANALYSIS_FILE
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=[ANALYSIS_FILE],
        )

    workspace.promote_accepted_state(accepted_patch=accepted_record)
    journal = json.loads(
        workspace.promotion_journal_path.read_text(encoding="utf-8")
    )
    drifted_mode = journal["entries"][0]["expected_mode"] ^ 0o100
    source_file.chmod(drifted_mode)

    with pytest.raises(RuntimeError, match="source state drifted"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert source_file.stat().st_mode & 0o7777 == drifted_mode


def test_retry_of_applied_promotion_requires_stage_installed_identity_binding(
    tmp_path: Path,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / ANALYSIS_FILE
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=[ANALYSIS_FILE],
        )

    result = workspace.promote_accepted_state(accepted_patch=accepted_record)
    assert result.status == "applied"
    journal = json.loads(
        workspace.promotion_journal_path.read_text(encoding="utf-8")
    )
    stage = source_file.with_name(journal["entries"][0]["stage_name"])
    assert not stage.exists()
    assert journal["entries"][0]["stage_identity"] == journal["entries"][0][
        "installed_identity"
    ]
    journal["entries"][0]["stage_identity"] = [0, 0]
    workspace.promotion_journal_path.write_text(
        json.dumps(journal),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Applied promotion journal entry"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert source_file.read_text(encoding="utf-8") == "threshold = 2\n"


def test_retry_of_applied_promotion_rejects_mode_outside_git_baseline(
    tmp_path: Path,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / ANALYSIS_FILE
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=[ANALYSIS_FILE],
        )

    result = workspace.promote_accepted_state(accepted_patch=accepted_record)
    assert result.status == "applied"
    journal = json.loads(
        workspace.promotion_journal_path.read_text(encoding="utf-8")
    )
    drifted_mode = journal["entries"][0]["expected_mode"] ^ 0o100
    source_file.chmod(drifted_mode)
    journal["entries"][0]["expected_mode"] = drifted_mode
    workspace.promotion_journal_path.write_text(
        json.dumps(journal),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Applied promotion journal entry"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert source_file.stat().st_mode & 0o7777 == drifted_mode


@pytest.mark.parametrize(
    "mutation",
    [
        "schema",
        "source_project_root",
        "sandbox_repo",
        "baseline_commit",
        "accepted_commit",
        "files",
        "entries",
        "accepted_digest",
        "parent_chain_identities",
    ],
)
def test_retry_of_applied_promotion_rejects_journal_authority_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )

    workspace.promote_accepted_state(accepted_patch=accepted_record)
    journal = json.loads(
        workspace.promotion_journal_path.read_text(encoding="utf-8")
    )
    if mutation == "schema":
        journal["schema_version"] = 999
    elif mutation == "source_project_root":
        journal["source_project_root"] = str(tmp_path / "other-project")
    elif mutation == "sandbox_repo":
        journal["sandbox_repo"] = str(tmp_path / "other-sandbox")
    elif mutation == "baseline_commit":
        journal["baseline_commit"] = "0" * 40
    elif mutation == "accepted_commit":
        journal["accepted_commit"] = "0" * 40
    elif mutation == "files":
        journal["files"] = []
    elif mutation == "entries":
        journal["entries"] = []
    elif mutation == "parent_chain_identities":
        journal["entries"][0]["parent_chain_identities"] = []
    else:
        journal["entries"][0]["accepted_digest"] = "0" * 64
    workspace.promotion_journal_path.write_text(
        json.dumps(journal),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Applied promotion journal"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert source_file.read_text(encoding="utf-8") == "threshold = 2\n"


def test_retry_preserves_stage_without_durable_stage_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )

    real_stage_writer = workspace._write_exclusive_stage

    def interrupt_after_stage(
        entry: object,
        content: bytes,
    ) -> None:
        real_stage_writer(entry, content)
        raise SystemExit("injected interruption after stage creation")

    monkeypatch.setattr(workspace, "_write_exclusive_stage", interrupt_after_stage)
    with pytest.raises(SystemExit, match="after stage creation"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    journal = json.loads(
        workspace.promotion_journal_path.read_text(encoding="utf-8")
    )
    stages = list(source_file.parent.glob(".*.omicsclaw-*.new"))
    assert len(stages) == 1
    assert len(journal["entries"]) == 1
    assert journal["entries"][0]["stage_name"] == stages[0].name
    assert journal["entries"][0]["stage_identity"] is None

    monkeypatch.setattr(workspace, "_write_exclusive_stage", real_stage_writer)
    result = workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert result.status == "recovery_required"
    assert source_file.read_text(encoding="utf-8") == "threshold = 1\n"
    assert stages[0].read_text(encoding="utf-8") == "threshold = 2\n"


def test_pre_stage_same_digest_placeholder_is_not_cleanup_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=["skills/test/analysis.py"],
        )

    real_stage_writer = workspace._write_exclusive_stage
    occupied_stage: Path | None = None

    def occupy_before_stage(entry: object, content: bytes) -> None:
        nonlocal occupied_stage
        stage = entry.stage
        stage.write_bytes(content)
        occupied_stage = stage
        real_stage_writer(entry, content)

    monkeypatch.setattr(workspace, "_write_exclusive_stage", occupy_before_stage)
    with pytest.raises(FileExistsError):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert occupied_stage is not None
    assert occupied_stage.read_text(encoding="utf-8") == "threshold = 2\n"
    journal = json.loads(
        workspace.promotion_journal_path.read_text(encoding="utf-8")
    )
    assert journal["entries"][0].get("stage_identity") is None


def test_stage_creation_rejects_preflight_parent_symlink_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / ANALYSIS_FILE
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=[ANALYSIS_FILE],
        )

    real_stage_writer = workspace._write_exclusive_stage
    source_parent = source_file.parent
    original_parent = source_parent.with_name("test-original")
    external_parent = tmp_path / "external-parent"
    external_parent.mkdir()
    external_stage: Path | None = None

    def replace_parent_before_stage(entry: object, content: bytes) -> None:
        nonlocal external_stage
        source_parent.rename(original_parent)
        source_parent.symlink_to(external_parent, target_is_directory=True)
        external_stage = external_parent / entry.stage.name
        real_stage_writer(entry, content)
        raise SystemExit("stage writer reached replaced parent")

    monkeypatch.setattr(
        workspace,
        "_write_exclusive_stage",
        replace_parent_before_stage,
    )
    with pytest.raises(RuntimeError, match="parent chain"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert external_stage is not None
    assert not external_stage.exists()
    assert (original_parent / source_file.name).read_text(
        encoding="utf-8"
    ) == "threshold = 1\n"


def test_parent_drift_cleanup_preserves_external_canonical_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / ANALYSIS_FILE
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=[ANALYSIS_FILE],
        )

    real_stage_writer = workspace._write_exclusive_stage
    source_parent = source_file.parent
    original_parent = source_parent.with_name("test-original")
    external_parent = tmp_path / "external-parent"
    external_parent.mkdir()
    external_stage: Path | None = None

    def occupy_replaced_parent_stage(entry: object, content: bytes) -> None:
        nonlocal external_stage
        source_parent.rename(original_parent)
        source_parent.symlink_to(external_parent, target_is_directory=True)
        external_stage = external_parent / entry.stage.name
        external_stage.write_text("external sentinel\n", encoding="utf-8")
        real_stage_writer(entry, content)

    monkeypatch.setattr(
        workspace,
        "_write_exclusive_stage",
        occupy_replaced_parent_stage,
    )
    with pytest.raises(RuntimeError, match="parent chain"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert external_stage is not None
    assert external_stage.read_text(encoding="utf-8") == "external sentinel\n"
    assert (original_parent / source_file.name).read_text(
        encoding="utf-8"
    ) == "threshold = 1\n"


def test_parent_drift_after_durable_stage_preserves_recovery_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / ANALYSIS_FILE
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=[ANALYSIS_FILE],
        )

    real_write_journal = workspace._write_promotion_journal
    source_parent = source_file.parent
    original_parent = source_parent.with_name("test-original")
    external_stage: Path | None = None
    original_stage: Path | None = None
    swapped = False

    def drift_after_prepared_journal(payload: dict[str, object]) -> None:
        nonlocal external_stage, original_stage, swapped
        real_write_journal(payload)
        if payload.get("status") == "prepared" and not swapped:
            swapped = True
            entries = payload["entries"]
            assert isinstance(entries, list)
            stage_name = entries[0]["stage_name"]
            source_parent.rename(original_parent)
            source_parent.mkdir()
            original_stage = original_parent / stage_name
            external_stage = source_parent / stage_name
            external_stage.write_text("external sentinel\n", encoding="utf-8")
            raise RuntimeError("injected parent drift after prepared journal")

    monkeypatch.setattr(
        workspace,
        "_write_promotion_journal",
        drift_after_prepared_journal,
    )
    with pytest.raises(RuntimeError, match="injected parent drift"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert external_stage is not None
    assert original_stage is not None
    assert external_stage.read_text(encoding="utf-8") == "external sentinel\n"
    assert original_stage.read_text(encoding="utf-8") == "threshold = 2\n"
    assert (original_parent / source_file.name).read_text(
        encoding="utf-8"
    ) == "threshold = 1\n"

    result = workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert result.status == "recovery_required"
    assert external_stage.read_text(encoding="utf-8") == "external sentinel\n"
    assert original_stage.read_text(encoding="utf-8") == "threshold = 2\n"
    assert (original_parent / source_file.name).read_text(
        encoding="utf-8"
    ) == "threshold = 1\n"


def test_install_revalidates_parent_chain_after_stage_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / ANALYSIS_FILE
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=[ANALYSIS_FILE],
        )

    real_install = workspace._install_entry_cas
    real_snapshot = workspace._snapshot_regular_file
    real_replace = harness_workspace_module.os.replace
    source_parent = source_file.parent
    original_parent = source_parent.with_name("test-original")
    external_parent = tmp_path / "external-parent"
    external_parent.mkdir()
    external_target = external_parent / source_file.name
    external_mutations: list[tuple[Path, Path]] = []
    install_started = False
    swapped = False

    def track_external_replace(source: object, target: object) -> None:
        source_path = Path(source)
        target_path = Path(target)
        if (
            source_path.parent == external_parent
            or target_path.parent == external_parent
        ):
            external_mutations.append((source_path, target_path))
        real_replace(source, target)

    def swap_after_stage_snapshot(
        path: Path,
        *,
        expected_nlink: int = 1,
    ) -> object:
        nonlocal swapped
        snapshot = real_snapshot(path, expected_nlink=expected_nlink)
        if install_started and not swapped and path.name.endswith(".new"):
            swapped = True
            source_parent.rename(original_parent)
            source_parent.symlink_to(external_parent, target_is_directory=True)
            external_target.write_text("external target\n", encoding="utf-8")
            (external_parent / path.name).write_text(
                "external stage\n",
                encoding="utf-8",
            )
        return snapshot

    def install_with_swap(entry: object) -> None:
        nonlocal install_started
        install_started = True
        real_install(entry)

    monkeypatch.setattr(
        harness_workspace_module.os,
        "replace",
        track_external_replace,
    )
    monkeypatch.setattr(
        workspace,
        "_snapshot_regular_file",
        swap_after_stage_snapshot,
    )
    monkeypatch.setattr(workspace, "_install_entry_cas", install_with_swap)

    result = workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert result.status == "recovery_required"
    assert external_mutations == []
    assert external_target.read_text(encoding="utf-8") == "external target\n"
    assert not list(external_parent.glob("*.bak"))
    assert (original_parent / source_file.name).read_text(
        encoding="utf-8"
    ) == "threshold = 1\n"


def test_stage_creation_rejects_preflight_plain_parent_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / ANALYSIS_FILE
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_threshold_patch(),
            modified_files=[ANALYSIS_FILE],
        )

    real_stage_writer = workspace._write_exclusive_stage
    source_parent = source_file.parent
    original_parent = source_parent.with_name("test-original")
    replacement_stage: Path | None = None

    def replace_parent_before_stage(entry: object, content: bytes) -> None:
        nonlocal replacement_stage
        source_parent.rename(original_parent)
        source_parent.mkdir()
        replacement_stage = source_parent / entry.stage.name
        real_stage_writer(entry, content)
        raise SystemExit("stage writer reached replaced parent")

    monkeypatch.setattr(
        workspace,
        "_write_exclusive_stage",
        replace_parent_before_stage,
    )
    with pytest.raises(RuntimeError, match="parent chain"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert replacement_stage is not None
    assert not replacement_stage.exists()
    assert (original_parent / source_file.name).read_text(
        encoding="utf-8"
    ) == "threshold = 1\n"


def test_preparing_declares_all_entries_before_first_stage_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_file = _workspace(tmp_path)
    helper_file = source_file.with_name("helper.py")

    with workspace.trial_worktree(1, _surface_for(workspace)) as (
        accepted_root,
        _accepted_surface,
    ):
        accepted_file = accepted_root / "skills" / "test" / "analysis.py"
        accepted_helper = accepted_file.with_name("helper.py")
        accepted_file.write_text("threshold = 2\n", encoding="utf-8")
        accepted_helper.write_text("scale = 20\n", encoding="utf-8")
        accepted_record = _commit_accepted(workspace,
            iteration=1,
            worktree=accepted_root,
            patch=_two_file_patch(),
            modified_files=[
                "skills/test/analysis.py",
                "skills/test/helper.py",
            ],
        )

    real_stage_writer = workspace._write_exclusive_stage

    def interrupt_during_first_stage(
        entry: object,
        content: bytes,
    ) -> None:
        journal = json.loads(
            workspace.promotion_journal_path.read_text(encoding="utf-8")
        )
        assert [entry["path"] for entry in journal["entries"]] == [
            "skills/test/analysis.py",
            "skills/test/helper.py",
        ]
        assert not list(source_file.parent.glob(".*.omicsclaw-*"))
        real_stage_writer(entry, content)
        raise SystemExit("injected first-stage interruption")

    monkeypatch.setattr(
        workspace,
        "_write_exclusive_stage",
        interrupt_during_first_stage,
    )
    with pytest.raises(SystemExit, match="first-stage interruption"):
        workspace.promote_accepted_state(accepted_patch=accepted_record)

    monkeypatch.setattr(workspace, "_write_exclusive_stage", real_stage_writer)
    result = workspace.promote_accepted_state(accepted_patch=accepted_record)

    assert result.status == "recovery_required"
    assert source_file.read_text(encoding="utf-8") == "threshold = 1\n"
    assert helper_file.read_text(encoding="utf-8") == "scale = 10\n"
    assert list(source_file.parent.glob(".*.omicsclaw-*.new"))


def _surface_for(workspace: HarnessWorkspace):
    from omicsclaw.autoagent.edit_surface import EditSurface

    return EditSurface(
        max_level=2,
        project_root=workspace.source_project_root,
        explicit_files=[
            "skills/test/analysis.py",
            "skills/test/helper.py",
        ],
    )
