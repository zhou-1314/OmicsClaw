"""Pre-CAS authority contracts for accepted AutoAgent commits.

These tests exercise the public ``HarnessWorkspace.commit_accepted_patch``
boundary with real Git repositories.  A rejected candidate must not change the
durable accepted ref, its in-process cache, or accepted evidence.
"""

from __future__ import annotations

import json
import locale
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import omicsclaw.autoagent.harness_workspace as harness_workspace_module
import omicsclaw.common.output_claim as output_claim_module
from omicsclaw.autoagent.edit_surface import EditSurface
from omicsclaw.autoagent.harness_workspace import AcceptedPatchRecord, HarnessWorkspace
from omicsclaw.autoagent.patch_engine import FileDiff, Hunk, PatchPlan


ANALYSIS_FILE = "skills/test/analysis.py"
HELPER_FILE = "skills/test/helper.py"


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
    gitignore: str | None = None,
    gitattributes: str | None = None,
    helper_code: str = "scale = 10\n",
    analysis_bytes: bytes | None = None,
) -> tuple[HarnessWorkspace, Path, Path]:
    source_root = tmp_path / "project"
    analysis_file = source_root / ANALYSIS_FILE
    helper_file = source_root / HELPER_FILE
    analysis_file.parent.mkdir(parents=True)
    if analysis_bytes is None:
        analysis_file.write_text("threshold = 1\n", encoding="utf-8")
    else:
        analysis_file.write_bytes(analysis_bytes)
    helper_file.write_text(helper_code, encoding="utf-8")
    if gitignore is not None:
        (source_root / ".gitignore").write_text(gitignore, encoding="utf-8")
    if gitattributes is not None:
        (source_root / ".gitattributes").write_text(
            gitattributes,
            encoding="utf-8",
        )
    os.chmod(analysis_file, 0o644)
    os.chmod(helper_file, 0o644)
    _init_source_repository(source_root)

    workspace = HarnessWorkspace(source_root, tmp_path / "run")
    workspace.create()
    return workspace, analysis_file, helper_file


def _surface(workspace: HarnessWorkspace) -> EditSurface:
    return EditSurface(
        max_level=2,
        project_root=workspace.source_project_root,
        explicit_files=[ANALYSIS_FILE, HELPER_FILE],
    )


def _plan(
    *,
    old_code: str = "threshold = 1",
    new_code: str = "threshold = 2",
    target_files: list[str] | None = None,
    diff_file: str = ANALYSIS_FILE,
) -> PatchPlan:
    return PatchPlan(
        target_files=(
            list(target_files) if target_files is not None else [diff_file]
        ),
        description="Raise the governed threshold",
        expected_improvements=["More robust governed analysis"],
        rollback_conditions=["Hard-gate regression"],
        diffs=[
            FileDiff(
                file=diff_file,
                hunks=[Hunk(old_code=old_code, new_code=new_code)],
            )
        ],
        reasoning="The accepted trial passed every hard gate.",
    )


def _git(workspace: HarnessWorkspace, *args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd or workspace.repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _accepted_head(workspace: HarnessWorkspace) -> str:
    return _git(
        workspace,
        "rev-parse",
        f"refs/heads/{workspace.accepted_branch}^{{commit}}",
    )


def _artifact_state(workspace: HarnessWorkspace) -> dict[str, bytes]:
    root = workspace.accepted_artifacts_root
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.iterdir())
        if path.is_file()
    }


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


def _assert_commit_rejected_without_authority_change(
    workspace: HarnessWorkspace,
    operation: Callable[[], Any],
) -> None:
    previous_head = _accepted_head(workspace)
    previous_cache = workspace.accepted_commit
    previous_artifacts = _artifact_state(workspace)

    failure: Exception | None = None
    returned: Any = None
    try:
        returned = operation()
    except Exception as exc:  # the public contract is fail-closed, not error-type specific
        failure = exc

    assert _accepted_head(workspace) == previous_head
    assert workspace.accepted_commit == previous_cache
    assert _artifact_state(workspace) == previous_artifacts
    assert not workspace.promotion_journal_path.exists()
    assert failure is not None, (
        "commit_accepted_patch accepted an unauthenticated candidate: "
        f"{returned!r}"
    )


def test_exact_accepted_commit_is_durable_and_reauthenticates(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)
    patch = _plan()

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        (trial_root / ANALYSIS_FILE).write_text(
            "threshold = 2\n",
            encoding="utf-8",
        )
        record = _commit_accepted(workspace,
            iteration=1,
            worktree=trial_root,
            patch=patch,
            modified_files=[ANALYSIS_FILE],
        )

    durable_record = workspace.durable_accepted_head_record()
    manifest = json.loads(Path(record.manifest_path).read_text(encoding="utf-8"))
    reloaded = HarnessWorkspace(
        workspace.source_project_root,
        workspace.output_root,
    )
    reloaded.open_existing()
    reloaded_record = reloaded.durable_accepted_head_record()

    assert _accepted_head(workspace) == record.commit_hash
    assert workspace.accepted_commit == record.commit_hash
    assert durable_record.to_dict() == record.to_dict()
    assert reloaded_record.to_dict() == record.to_dict()
    assert manifest["patch_plan"] == patch.to_dict()
    assert not workspace.promotion_journal_path.exists()


def test_commit_requires_explicit_pre_execution_witness(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        (trial_root / ANALYSIS_FILE).write_text(
            "threshold = 2\n",
            encoding="utf-8",
        )
        _assert_commit_rejected_without_authority_change(
            workspace,
            lambda: workspace.commit_accepted_patch(
                iteration=1,
                worktree=trial_root,
                patch=_plan(),
                modified_files=[ANALYSIS_FILE],
            ),
        )


def test_rehydrated_workspace_can_promote_durable_accepted_state(
    tmp_path: Path,
) -> None:
    workspace, source_analysis, _helper_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        (trial_root / ANALYSIS_FILE).write_text(
            "threshold = 2\n",
            encoding="utf-8",
        )
        record = _commit_accepted(
            workspace,
            iteration=1,
            worktree=trial_root,
            patch=_plan(),
            modified_files=[ANALYSIS_FILE],
        )

    reloaded = HarnessWorkspace(
        workspace.source_project_root,
        workspace.output_root,
    )
    reloaded.open_existing()
    durable_record = reloaded.durable_accepted_head_record()
    result = reloaded.promote_accepted_state(accepted_patch=durable_record)

    assert result.status == "applied"
    assert durable_record.to_dict() == record.to_dict()
    source_stat = os.lstat(workspace.source_project_root)
    assert record.source_project_root == str(workspace.source_project_root)
    assert record.source_project_identity == (
        source_stat.st_dev,
        source_stat.st_ino,
    )
    assert source_analysis.read_text(encoding="utf-8") == "threshold = 2\n"


def test_rehydrated_workspace_rejects_durable_state_from_foreign_source_root(
    tmp_path: Path,
) -> None:
    workspace, source_analysis, _helper_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        (trial_root / ANALYSIS_FILE).write_text(
            "threshold = 2\n",
            encoding="utf-8",
        )
        record = _commit_accepted(
            workspace,
            iteration=1,
            worktree=trial_root,
            patch=_plan(),
            modified_files=[ANALYSIS_FILE],
        )

    foreign_root = tmp_path / "foreign-project"
    foreign_analysis = foreign_root / ANALYSIS_FILE
    foreign_analysis.parent.mkdir(parents=True)
    foreign_analysis.write_text("threshold = 1\n", encoding="utf-8")
    (foreign_analysis.parent / "helper.py").write_text(
        "scale = 10\n",
        encoding="utf-8",
    )
    foreign = HarnessWorkspace(foreign_root, workspace.output_root)
    # Adversarial private-state bypass: durable operations must still reject.
    foreign._created = True

    with pytest.raises(ValueError, match="source project root|source project identity"):
        foreign.durable_accepted_head_record()
    with pytest.raises(ValueError, match="source project root|source project identity"):
        foreign.promote_accepted_state(accepted_patch=record)

    assert source_analysis.read_text(encoding="utf-8") == "threshold = 1\n"
    assert foreign_analysis.read_text(encoding="utf-8") == "threshold = 1\n"
    assert not workspace.promotion_journal_path.exists()


def test_rehydrated_workspace_rejects_replaced_source_root_inode(
    tmp_path: Path,
) -> None:
    workspace, _source_analysis, _helper_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        (trial_root / ANALYSIS_FILE).write_text(
            "threshold = 2\n",
            encoding="utf-8",
        )
        record = _commit_accepted(
            workspace,
            iteration=1,
            worktree=trial_root,
            patch=_plan(),
            modified_files=[ANALYSIS_FILE],
        )

    original_root = workspace.source_project_root.with_name("project-original")
    workspace.source_project_root.rename(original_root)
    replacement_analysis = workspace.source_project_root / ANALYSIS_FILE
    replacement_analysis.parent.mkdir(parents=True)
    replacement_analysis.write_text("threshold = 1\n", encoding="utf-8")
    (replacement_analysis.parent / "helper.py").write_text(
        "scale = 10\n",
        encoding="utf-8",
    )
    reloaded = HarnessWorkspace(
        workspace.source_project_root,
        workspace.output_root,
    )
    # Adversarial private-state bypass: durable operations must still reject.
    reloaded._created = True

    with pytest.raises(ValueError, match="source project identity"):
        reloaded.durable_accepted_head_record()
    with pytest.raises(ValueError, match="source project identity"):
        reloaded.promote_accepted_state(accepted_patch=record)

    assert (original_root / ANALYSIS_FILE).read_text(encoding="utf-8") == (
        "threshold = 1\n"
    )
    assert replacement_analysis.read_text(encoding="utf-8") == "threshold = 1\n"
    assert not workspace.promotion_journal_path.exists()


def test_commit_rejects_target_mutation_after_pre_execution_witness(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)
    patch = _plan()

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        target = trial_root / ANALYSIS_FILE
        target.write_text("threshold = 2\n", encoding="utf-8")
        workspace.freeze_candidate_patch(
            iteration=1,
            worktree=trial_root,
            patch=patch,
            modified_files=[ANALYSIS_FILE],
        )
        target.write_text("threshold = 3\n", encoding="utf-8")

        _assert_commit_rejected_without_authority_change(
            workspace,
            lambda: workspace.commit_accepted_patch(
                iteration=1,
                worktree=trial_root,
                patch=patch,
                modified_files=[ANALYSIS_FILE],
            ),
        )


def test_commit_rejects_duplicate_modified_files_before_cas(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        (trial_root / ANALYSIS_FILE).write_text("threshold = 2\n", encoding="utf-8")
        _assert_commit_rejected_without_authority_change(
            workspace,
            lambda: _commit_accepted(workspace,
                iteration=1,
                worktree=trial_root,
                patch=_plan(),
                modified_files=[ANALYSIS_FILE, ANALYSIS_FILE],
            ),
        )


@pytest.mark.parametrize("case", ["wrong", "superset"])
def test_commit_rejects_wrong_or_superset_modified_files_before_cas(
    tmp_path: Path,
    case: str,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        if case == "wrong":
            (trial_root / HELPER_FILE).write_text("scale = 20\n", encoding="utf-8")
            modified_files = [HELPER_FILE]
        else:
            (trial_root / ANALYSIS_FILE).write_text(
                "threshold = 2\n",
                encoding="utf-8",
            )
            modified_files = [ANALYSIS_FILE, HELPER_FILE]

        _assert_commit_rejected_without_authority_change(
            workspace,
            lambda: _commit_accepted(workspace,
                iteration=1,
                worktree=trial_root,
                patch=_plan(),
                modified_files=modified_files,
            ),
        )


def test_commit_rejects_pre_staged_extra_file_before_cas(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)

    with pytest.raises(RuntimeError, match="worktree cleanup"):
        with workspace.trial_worktree(1, _surface(workspace)) as (
            trial_root,
            _trial_surface,
        ):
            (trial_root / ANALYSIS_FILE).write_text(
                "threshold = 2\n",
                encoding="utf-8",
            )
            (trial_root / HELPER_FILE).write_text("scale = 20\n", encoding="utf-8")
            _git(workspace, "add", "--", HELPER_FILE, cwd=trial_root)

            _assert_commit_rejected_without_authority_change(
                workspace,
                lambda: _commit_accepted(workspace,
                    iteration=1,
                    worktree=trial_root,
                    patch=_plan(),
                    modified_files=[ANALYSIS_FILE],
                ),
            )

    assert workspace.git_control_compromise_path.is_file()


def test_commit_rejects_unstaged_tracked_extra_file_before_cas(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        (trial_root / ANALYSIS_FILE).write_text("threshold = 2\n", encoding="utf-8")
        (trial_root / HELPER_FILE).write_text("scale = 20\n", encoding="utf-8")

        _assert_commit_rejected_without_authority_change(
            workspace,
            lambda: _commit_accepted(workspace,
                iteration=1,
                worktree=trial_root,
                patch=_plan(),
                modified_files=[ANALYSIS_FILE],
            ),
        )


def test_commit_rejects_nonignored_untracked_file_before_cas(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        (trial_root / ANALYSIS_FILE).write_text("threshold = 2\n", encoding="utf-8")
        (trial_root / "unexpected-trial-state.txt").write_text(
            "not part of the accepted patch\n",
            encoding="utf-8",
        )

        _assert_commit_rejected_without_authority_change(
            workspace,
            lambda: _commit_accepted(workspace,
                iteration=1,
                worktree=trial_root,
                patch=_plan(),
                modified_files=[ANALYSIS_FILE],
            ),
        )


def test_commit_rejects_gitignored_untracked_file_before_cas(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(
        tmp_path,
        gitignore="runtime.cache\n",
    )

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        (trial_root / ANALYSIS_FILE).write_text("threshold = 2\n", encoding="utf-8")
        (trial_root / "runtime.cache").write_text(
            "runtime-owned candidate state\n",
            encoding="utf-8",
        )

        _assert_commit_rejected_without_authority_change(
            workspace,
            lambda: _commit_accepted(workspace,
                iteration=1,
                worktree=trial_root,
                patch=_plan(),
                modified_files=[ANALYSIS_FILE],
            ),
        )


def test_commit_rejects_nested_gitignored_directory_before_cas(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(
        tmp_path,
        gitignore="runtime-cache/\n",
    )

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        (trial_root / ANALYSIS_FILE).write_text("threshold = 2\n", encoding="utf-8")
        ignored_file = trial_root / "runtime-cache" / "nested" / "state.bin"
        ignored_file.parent.mkdir(parents=True)
        ignored_file.write_bytes(b"runtime-owned candidate state\n")

        _assert_commit_rejected_without_authority_change(
            workspace,
            lambda: _commit_accepted(workspace,
                iteration=1,
                worktree=trial_root,
                patch=_plan(),
                modified_files=[ANALYSIS_FILE],
            ),
        )
        assert ignored_file.read_bytes() == b"runtime-owned candidate state\n"


def test_commit_rejects_gitignored_file_created_during_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(
        tmp_path,
        gitignore="runtime.cache\n",
    )
    real_git = workspace._git

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        ignored_file = trial_root / "runtime.cache"

        def add_then_create_ignored_state(
            args: list[str],
            *,
            cwd: Path,
            check: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            result = real_git(args, cwd=cwd, check=check)
            if args[:2] == ["add", "--"]:
                ignored_file.write_text(
                    "runtime-owned candidate state\n",
                    encoding="utf-8",
                )
            return result

        monkeypatch.setattr(workspace, "_git", add_then_create_ignored_state)
        (trial_root / ANALYSIS_FILE).write_text("threshold = 2\n", encoding="utf-8")

        _assert_commit_rejected_without_authority_change(
            workspace,
            lambda: _commit_accepted(workspace,
                iteration=1,
                worktree=trial_root,
                patch=_plan(),
                modified_files=[ANALYSIS_FILE],
            ),
        )
        assert ignored_file.read_text(encoding="utf-8") == (
            "runtime-owned candidate state\n"
        )


@pytest.mark.parametrize("index_flag", ["assume-unchanged", "skip-worktree"])
def test_commit_rejects_hidden_tracked_extra_file_before_cas(
    tmp_path: Path,
    index_flag: str,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)

    with pytest.raises(RuntimeError, match="worktree cleanup"):
        with workspace.trial_worktree(1, _surface(workspace)) as (
            trial_root,
            _trial_surface,
        ):
            (trial_root / ANALYSIS_FILE).write_text(
                "threshold = 2\n",
                encoding="utf-8",
            )
            _git(
                workspace,
                "update-index",
                f"--{index_flag}",
                "--",
                HELPER_FILE,
                cwd=trial_root,
            )
            (trial_root / HELPER_FILE).write_text("scale = 20\n", encoding="utf-8")

            _assert_commit_rejected_without_authority_change(
                workspace,
                lambda: _commit_accepted(workspace,
                    iteration=1,
                    worktree=trial_root,
                    patch=_plan(),
                    modified_files=[ANALYSIS_FILE],
                ),
            )

    assert workspace.git_control_compromise_path.is_file()


def test_commit_rejects_tracked_extra_hidden_by_local_fsmonitor(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)

    with pytest.raises(RuntimeError, match="worktree cleanup"):
        with workspace.trial_worktree(1, _surface(workspace)) as (
            trial_root,
            _trial_surface,
        ):
            (trial_root / ANALYSIS_FILE).write_text(
                "threshold = 2\n",
                encoding="utf-8",
            )
            _git(
                workspace,
                "config",
                "core.fsmonitor",
                "/bin/sh -c 'printf token\\0'",
                cwd=trial_root,
            )
            _git(
                workspace,
                "status",
                "--porcelain=v1",
                "-z",
                cwd=trial_root,
            )
            (trial_root / HELPER_FILE).write_text("scale = 20\n", encoding="utf-8")

            _assert_commit_rejected_without_authority_change(
                workspace,
                lambda: _commit_accepted(workspace,
                    iteration=1,
                    worktree=trial_root,
                    patch=_plan(),
                    modified_files=[ANALYSIS_FILE],
                ),
            )

    assert workspace.git_control_compromise_path.is_file()


def test_commit_never_executes_candidate_controlled_fsmonitor_hook(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)
    sentinel = tmp_path / "fsmonitor-was-executed"
    hook = tmp_path / "candidate-fsmonitor.sh"
    hook.write_text(
        "#!/bin/sh\n"
        f": > {sentinel}\n"
        "printf 'token\\0'\n",
        encoding="utf-8",
    )
    os.chmod(hook, 0o755)

    with pytest.raises(RuntimeError, match="worktree cleanup"):
        with workspace.trial_worktree(1, _surface(workspace)) as (
            trial_root,
            _trial_surface,
        ):
            (trial_root / ANALYSIS_FILE).write_text(
                "threshold = 2\n",
                encoding="utf-8",
            )
            (trial_root / HELPER_FILE).write_text("scale = 20\n", encoding="utf-8")
            _git(
                workspace,
                "config",
                "core.fsmonitor",
                str(hook),
                cwd=trial_root,
            )

            _assert_commit_rejected_without_authority_change(
                workspace,
                lambda: _commit_accepted(workspace,
                    iteration=1,
                    worktree=trial_root,
                    patch=_plan(),
                    modified_files=[ANALYSIS_FILE],
                ),
            )
            assert not sentinel.exists()

    assert workspace.git_control_compromise_path.is_file()
    assert not sentinel.exists()


def test_commit_rejects_raw_tracked_bytes_hidden_by_clean_filter(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(
        tmp_path,
        gitattributes=f"{HELPER_FILE} text eol=lf\n",
    )

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        (trial_root / ANALYSIS_FILE).write_text("threshold = 2\n", encoding="utf-8")
        (trial_root / HELPER_FILE).write_bytes(b"scale = 10\r\n")

        _assert_commit_rejected_without_authority_change(
            workspace,
            lambda: _commit_accepted(workspace,
                iteration=1,
                worktree=trial_root,
                patch=_plan(),
                modified_files=[ANALYSIS_FILE],
            ),
        )


def test_commit_rejects_target_raw_crlf_normalized_by_clean_filter(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(
        tmp_path,
        gitattributes=f"{ANALYSIS_FILE} text eol=lf\n",
    )

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        (trial_root / ANALYSIS_FILE).write_bytes(b"threshold = 2\r\n")

        _assert_commit_rejected_without_authority_change(
            workspace,
            lambda: _commit_accepted(
                workspace,
                iteration=1,
                worktree=trial_root,
                patch=_plan(),
                modified_files=[ANALYSIS_FILE],
            ),
        )


def test_commit_preserves_non_target_raw_bytes_despite_crlf_attribute(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(
        tmp_path,
        gitattributes=f"{HELPER_FILE} text eol=crlf\n",
    )

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        assert (trial_root / HELPER_FILE).read_bytes() == b"scale = 10\n"
        (trial_root / ANALYSIS_FILE).write_text(
            "threshold = 2\n",
            encoding="utf-8",
        )
        record = _commit_accepted(
            workspace,
            iteration=1,
            worktree=trial_root,
            patch=_plan(),
            modified_files=[ANALYSIS_FILE],
        )

    assert workspace.durable_accepted_head_record().commit_hash == record.commit_hash


def test_commit_accepts_native_crlf_target_normalized_by_patch_engine(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(
        tmp_path,
        analysis_bytes=b"threshold = 1\r\n",
    )

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        assert (trial_root / ANALYSIS_FILE).read_bytes() == b"threshold = 1\r\n"
        (trial_root / ANALYSIS_FILE).write_bytes(b"threshold = 2\n")
        record = _commit_accepted(
            workspace,
            iteration=1,
            worktree=trial_root,
            patch=_plan(),
            modified_files=[ANALYSIS_FILE],
        )

    assert workspace.durable_accepted_head_record().commit_hash == record.commit_hash


def test_git_info_attributes_drift_rejects_without_invoking_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)
    git_calls: list[str] = []

    with pytest.raises(RuntimeError, match="worktree cleanup"):
        with workspace.trial_worktree(1, _surface(workspace)) as (
            trial_root,
            _trial_surface,
        ):
            (trial_root / ANALYSIS_FILE).write_text(
                "threshold = 2\n",
                encoding="utf-8",
            )
            (workspace.repo_root / ".git" / "info" / "attributes").write_text(
                f"{ANALYSIS_FILE} text eol=lf\n",
                encoding="utf-8",
            )

            def record_git_call(*args: Any, **kwargs: Any) -> Any:
                git_calls.append(repr((args, kwargs)))
                raise AssertionError("Git must not run before control witness rejection")

            for name in (
                "_git",
                "_git_output",
                "_git_output_bytes",
                "_git_output_with_input",
            ):
                monkeypatch.setattr(workspace, name, record_git_call)

            _assert_commit_rejected_without_authority_change(
                workspace,
                lambda: _commit_accepted(
                    workspace,
                    iteration=1,
                    worktree=trial_root,
                    patch=_plan(),
                    modified_files=[ANALYSIS_FILE],
                ),
            )
            assert git_calls == []

    assert git_calls == []
    assert workspace.git_control_compromise_path.is_file()


def test_git_control_drift_compromises_all_later_workspace_authority(
    tmp_path: Path,
) -> None:
    workspace, source_analysis, _helper_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface(workspace)) as (
        first_root,
        _first_surface,
    ):
        (first_root / ANALYSIS_FILE).write_text(
            "threshold = 2\n",
            encoding="utf-8",
        )
        accepted = _commit_accepted(
            workspace,
            iteration=1,
            worktree=first_root,
            patch=_plan(),
            modified_files=[ANALYSIS_FILE],
        )

    second_patch = _plan(
        old_code="threshold = 2",
        new_code="threshold = 3",
    )
    with pytest.raises(RuntimeError, match="worktree cleanup"):
        with workspace.trial_worktree(2, _surface(workspace)) as (
            second_root,
            _second_surface,
        ):
            (second_root / ANALYSIS_FILE).write_text(
                "threshold = 3\n",
                encoding="utf-8",
            )
            (workspace.repo_root / ".git" / "info" / "attributes").write_text(
                f"{ANALYSIS_FILE} text eol=lf\n",
                encoding="utf-8",
            )
            _assert_commit_rejected_without_authority_change(
                workspace,
                lambda: _commit_accepted(
                    workspace,
                    iteration=2,
                    worktree=second_root,
                    patch=second_patch,
                    modified_files=[ANALYSIS_FILE],
                ),
            )

    with pytest.raises(ValueError, match="control authority is compromised"):
        workspace.create_worktree(
            3,
            editable_files=[ANALYSIS_FILE, HELPER_FILE],
        )
    with pytest.raises(ValueError, match="control authority is compromised"):
        workspace.durable_accepted_head_record()
    with pytest.raises(ValueError, match="control authority is compromised"):
        workspace.promote_accepted_state(accepted_patch=accepted)
    assert workspace.git_control_compromise_path.is_file()

    reloaded = HarnessWorkspace(
        workspace.source_project_root,
        workspace.output_root,
    )
    # Adversarial private-state bypass: the durable latch remains authoritative.
    reloaded._created = True
    with pytest.raises(ValueError, match="control authority is compromised"):
        reloaded.durable_accepted_head_record()
    with pytest.raises(ValueError, match="control authority is compromised"):
        reloaded.promote_accepted_state(accepted_patch=accepted)

    assert _accepted_head(workspace) == accepted.commit_hash
    assert source_analysis.read_text(encoding="utf-8") == "threshold = 1\n"


def test_marker_write_failure_leaves_open_authority_and_restart_rejects_before_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _source_analysis, _helper_file = _workspace(tmp_path)
    real_writer = harness_workspace_module.atomic_write_owned_output_text

    def fail_only_compromise_marker(path: Path, **kwargs: Any) -> Path:
        if Path(path) == workspace.git_control_compromise_path:
            raise OSError("injected compromise-marker persistence failure")
        return real_writer(path, **kwargs)

    monkeypatch.setattr(
        harness_workspace_module,
        "atomic_write_owned_output_text",
        fail_only_compromise_marker,
    )
    with pytest.raises(RuntimeError, match="worktree cleanup|compromise marker"):
        with workspace.trial_worktree(1, _surface(workspace)):
            (workspace.repo_root / ".git" / "info" / "attributes").write_text(
                f"{ANALYSIS_FILE} text eol=lf\n",
                encoding="utf-8",
            )

    assert workspace._git_control_compromised is True
    assert not workspace.git_control_compromise_path.exists()
    state = json.loads(
        workspace.git_control_state_path.read_text(encoding="utf-8")
    )
    assert state["status"] == "trial_open"

    reloaded = HarnessWorkspace(
        workspace.source_project_root,
        workspace.output_root,
    )

    def forbid_git(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("rehydration must reject durable open state before Git")

    monkeypatch.setattr(harness_workspace_module.subprocess, "run", forbid_git)
    with pytest.raises(ValueError, match="not clean|trial.*open"):
        reloaded.open_existing()


@pytest.mark.parametrize(
    "mutation",
    ["missing", "corrupt", "symlink", "hardlink"],
)
def test_open_existing_rejects_invalid_control_state_before_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    workspace, _source_analysis, _helper_file = _workspace(tmp_path)
    state = workspace.git_control_state_path
    if mutation == "missing":
        state.unlink()
    elif mutation == "corrupt":
        state.write_text("{not-json}\n", encoding="utf-8")
    elif mutation == "symlink":
        outside = tmp_path / "outside-state"
        outside.write_text("{}\n", encoding="utf-8")
        state.unlink()
        state.symlink_to(outside)
    else:
        os.link(state, tmp_path / "state-hardlink")

    reloaded = HarnessWorkspace(
        workspace.source_project_root,
        workspace.output_root,
    )

    def forbid_git(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("invalid durable control must fail before Git")

    monkeypatch.setattr(harness_workspace_module.subprocess, "run", forbid_git)
    with pytest.raises(ValueError, match="Persisted Git control"):
        reloaded.open_existing()
    # Adversarial private-state bypass cannot skip durable state validation.
    reloaded._created = True
    with pytest.raises(ValueError, match="Persisted Git control"):
        reloaded.durable_accepted_head_record()


def test_open_existing_rejects_clean_common_git_drift_before_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _source_analysis, _helper_file = _workspace(tmp_path)
    attributes = workspace.repo_root / ".git" / "info" / "attributes"
    attributes.write_text(
        attributes.read_text(encoding="utf-8")
        + f"{ANALYSIS_FILE} text eol=lf\n",
        encoding="utf-8",
    )
    reloaded = HarnessWorkspace(
        workspace.source_project_root,
        workspace.output_root,
    )

    def forbid_git(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Git-control drift must fail before Git")

    monkeypatch.setattr(harness_workspace_module.subprocess, "run", forbid_git)
    with pytest.raises(ValueError, match="clean Git control authority changed"):
        reloaded.open_existing()


def test_unopened_workspace_public_git_read_is_rejected_before_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _source_analysis, _helper_file = _workspace(tmp_path)
    attributes = workspace.repo_root / ".git" / "info" / "attributes"
    attributes.write_text(
        attributes.read_text(encoding="utf-8")
        + f"{ANALYSIS_FILE} text eol=lf\n",
        encoding="utf-8",
    )
    reloaded = HarnessWorkspace(
        workspace.source_project_root,
        workspace.output_root,
    )

    def forbid_git(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unopened workspace must fail before Git")

    monkeypatch.setattr(harness_workspace_module.subprocess, "run", forbid_git)
    with pytest.raises(RuntimeError, match=r"create\(\)|open_existing\(\)"):
        reloaded.read_file_from_commit(
            workspace.baseline_commit,
            ANALYSIS_FILE,
        )


def test_open_existing_rejects_linked_worktree_registration_before_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _source_analysis, _helper_file = _workspace(tmp_path)
    registration = workspace.repo_root / ".git" / "worktrees" / "stale"
    registration.mkdir(parents=True)
    (registration / "gitdir").write_text(
        str(tmp_path / "unowned" / ".git") + "\n",
        encoding="utf-8",
    )
    reloaded = HarnessWorkspace(
        workspace.source_project_root,
        workspace.output_root,
    )

    def forbid_git(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("linked-worktree residue must fail before Git")

    monkeypatch.setattr(harness_workspace_module.subprocess, "run", forbid_git)
    with pytest.raises(ValueError, match="linked worktree|lock residue"):
        reloaded.open_existing()


def test_cleanup_checkpoint_write_failure_blocks_promotion_and_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, source_analysis, _helper_file = _workspace(tmp_path)
    real_writer = harness_workspace_module.atomic_write_owned_output_text
    accepted: AcceptedPatchRecord | None = None

    def fail_clean_checkpoint(path: Path, **kwargs: Any) -> Path:
        if (
            Path(path) == workspace.git_control_state_path
            and '"status":"clean"' in str(kwargs.get("text", ""))
        ):
            raise OSError("injected clean checkpoint persistence failure")
        return real_writer(path, **kwargs)

    monkeypatch.setattr(
        harness_workspace_module,
        "atomic_write_owned_output_text",
        fail_clean_checkpoint,
    )
    with pytest.raises(RuntimeError, match="clean Git authority"):
        with workspace.trial_worktree(1, _surface(workspace)) as (
            trial_root,
            _trial_surface,
        ):
            (trial_root / ANALYSIS_FILE).write_text(
                "threshold = 2\n",
                encoding="utf-8",
            )
            accepted = _commit_accepted(
                workspace,
                iteration=1,
                worktree=trial_root,
                patch=_plan(),
                modified_files=[ANALYSIS_FILE],
            )

    assert accepted is not None
    state = json.loads(
        workspace.git_control_state_path.read_text(encoding="utf-8")
    )
    assert state["status"] == "trial_open"
    assert workspace.git_control_compromise_path.is_file()
    with pytest.raises(ValueError, match="control authority is compromised"):
        workspace.promote_accepted_state(accepted_patch=accepted)

    reloaded = HarnessWorkspace(
        workspace.source_project_root,
        workspace.output_root,
    )
    with pytest.raises(ValueError, match="control authority is compromised"):
        reloaded.open_existing()
    assert source_analysis.read_text(encoding="utf-8") == "threshold = 1\n"


def test_clean_is_not_published_before_checkpoint_verification_and_marker_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _source_analysis, _helper_file = _workspace(tmp_path)
    trial_root = workspace.create_worktree(
        1,
        editable_files=[ANALYSIS_FILE, HELPER_FILE],
    )
    real_snapshot = workspace._snapshot_persistable_git_control
    real_writer = harness_workspace_module.atomic_write_owned_output_text
    snapshot_calls = 0

    def fail_second_checkpoint_snapshot() -> Any:
        nonlocal snapshot_calls
        snapshot_calls += 1
        if snapshot_calls == 2:
            raise OSError("injected checkpoint verification failure")
        return real_snapshot()

    def fail_compromise_marker(path: Path, **kwargs: Any) -> Path:
        if Path(path) == workspace.git_control_compromise_path:
            raise OSError("injected compromise-marker failure")
        return real_writer(path, **kwargs)

    monkeypatch.setattr(
        workspace,
        "_snapshot_persistable_git_control",
        fail_second_checkpoint_snapshot,
    )
    monkeypatch.setattr(
        harness_workspace_module,
        "atomic_write_owned_output_text",
        fail_compromise_marker,
    )

    with pytest.raises(RuntimeError, match="clean Git authority|compromise marker"):
        workspace.cleanup_worktree(trial_root)

    state = json.loads(
        workspace.git_control_state_path.read_text(encoding="utf-8")
    )
    assert state["status"] == "trial_open"
    assert not workspace.git_control_compromise_path.exists()
    reloaded = HarnessWorkspace(
        workspace.source_project_root,
        workspace.output_root,
    )
    with pytest.raises(ValueError, match="not clean|trial.*open"):
        reloaded.open_existing()


def test_post_replace_clean_checkpoint_error_authenticates_visible_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _source_analysis, _helper_file = _workspace(tmp_path)
    trial_root = workspace.create_worktree(
        1,
        editable_files=[ANALYSIS_FILE, HELPER_FILE],
    )
    real_writer = harness_workspace_module.atomic_write_owned_output_text
    real_fsync_directory = output_claim_module._fsync_directory
    clean_write_in_progress = False
    fsync_failures = 0
    marker_attempts = 0

    def fail_clean_directory_fsync(path: Path) -> None:
        nonlocal fsync_failures
        if clean_write_in_progress:
            fsync_failures += 1
            raise OSError("injected post-replace directory fsync failure")
        real_fsync_directory(path)

    def fail_marker_if_checkpoint_is_misclassified(
        path: Path,
        **kwargs: Any,
    ) -> Path:
        nonlocal clean_write_in_progress, marker_attempts
        candidate = Path(path)
        if candidate == workspace.git_control_compromise_path:
            marker_attempts += 1
            raise OSError("injected compromise-marker failure")
        if (
            candidate == workspace.git_control_state_path
            and '"status":"clean"' in str(kwargs.get("text", ""))
        ):
            clean_write_in_progress = True
            try:
                return real_writer(path, **kwargs)
            finally:
                clean_write_in_progress = False
        return real_writer(path, **kwargs)

    monkeypatch.setattr(
        output_claim_module,
        "_fsync_directory",
        fail_clean_directory_fsync,
    )
    monkeypatch.setattr(
        harness_workspace_module,
        "atomic_write_owned_output_text",
        fail_marker_if_checkpoint_is_misclassified,
    )

    workspace.cleanup_worktree(trial_root)

    state = json.loads(
        workspace.git_control_state_path.read_text(encoding="utf-8")
    )
    assert state["status"] == "clean"
    assert fsync_failures == 1
    assert marker_attempts == 0
    assert not workspace.git_control_compromise_path.exists()
    reloaded = HarnessWorkspace(
        workspace.source_project_root,
        workspace.output_root,
    )
    reloaded.open_existing()


def test_post_replace_checkpoint_error_rejects_different_visible_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _source_analysis, _helper_file = _workspace(tmp_path)
    trial_root = workspace.create_worktree(
        1,
        editable_files=[ANALYSIS_FILE, HELPER_FILE],
    )
    prior_open_state = workspace.git_control_state_path.read_bytes()
    real_writer = harness_workspace_module.atomic_write_owned_output_text
    real_fsync_directory = output_claim_module._fsync_directory
    clean_write_in_progress = False
    fsync_failures = 0

    def replace_visible_clean_with_different_state_and_fail(path: Path) -> None:
        nonlocal fsync_failures
        if clean_write_in_progress:
            workspace.git_control_state_path.write_bytes(prior_open_state)
            fsync_failures += 1
            raise OSError("injected post-replace directory fsync failure")
        real_fsync_directory(path)

    def identify_clean_checkpoint(path: Path, **kwargs: Any) -> Path:
        nonlocal clean_write_in_progress
        if (
            Path(path) == workspace.git_control_state_path
            and '"status":"clean"' in str(kwargs.get("text", ""))
        ):
            clean_write_in_progress = True
            try:
                return real_writer(path, **kwargs)
            finally:
                clean_write_in_progress = False
        return real_writer(path, **kwargs)

    monkeypatch.setattr(
        output_claim_module,
        "_fsync_directory",
        replace_visible_clean_with_different_state_and_fail,
    )
    monkeypatch.setattr(
        harness_workspace_module,
        "atomic_write_owned_output_text",
        identify_clean_checkpoint,
    )

    with pytest.raises(RuntimeError, match="clean Git authority"):
        workspace.cleanup_worktree(trial_root)

    assert fsync_failures == 1
    assert workspace.git_control_state_path.read_bytes() == prior_open_state
    assert workspace.git_control_compromise_path.is_file()
    reloaded = HarnessWorkspace(
        workspace.source_project_root,
        workspace.output_root,
    )
    with pytest.raises(ValueError, match="control authority is compromised"):
        reloaded.open_existing()


def test_full_workspace_rebuild_is_the_only_compromise_latch_reset(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)

    with pytest.raises(RuntimeError, match="worktree cleanup"):
        with workspace.trial_worktree(1, _surface(workspace)) as (
            trial_root,
            _trial_surface,
        ):
            (trial_root / ANALYSIS_FILE).write_text(
                "threshold = 2\n",
                encoding="utf-8",
            )
            (workspace.repo_root / ".git" / "info" / "attributes").write_text(
                f"{ANALYSIS_FILE} text eol=lf\n",
                encoding="utf-8",
            )
            _assert_commit_rejected_without_authority_change(
                workspace,
                lambda: _commit_accepted(
                    workspace,
                    iteration=1,
                    worktree=trial_root,
                    patch=_plan(),
                    modified_files=[ANALYSIS_FILE],
                ),
            )

    assert workspace.git_control_compromise_path.is_file()
    workspace.create()
    assert not workspace.git_control_compromise_path.exists()

    with workspace.trial_worktree(1, _surface(workspace)) as (
        clean_root,
        _clean_surface,
    ):
        assert (clean_root / ANALYSIS_FILE).read_text(encoding="utf-8") == (
            "threshold = 1\n"
        )


def test_rebuild_keeps_compromise_latch_until_clean_checkpoint_is_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)
    with pytest.raises(RuntimeError, match="worktree cleanup"):
        with workspace.trial_worktree(1, _surface(workspace)):
            (workspace.repo_root / ".git" / "info" / "attributes").write_text(
                f"{ANALYSIS_FILE} text eol=lf\n",
                encoding="utf-8",
            )
    assert workspace.git_control_compromise_path.is_file()
    real_writer = harness_workspace_module.atomic_write_owned_output_text

    def fail_clean_checkpoint(path: Path, **kwargs: Any) -> Path:
        if Path(path) == workspace.git_control_state_path:
            raise OSError("injected rebuild checkpoint persistence failure")
        return real_writer(path, **kwargs)

    monkeypatch.setattr(
        harness_workspace_module,
        "atomic_write_owned_output_text",
        fail_clean_checkpoint,
    )
    with pytest.raises(OSError, match="rebuild checkpoint"):
        workspace.create()

    assert workspace._created is False
    assert workspace.git_control_compromise_path.is_file()
    reloaded = HarnessWorkspace(
        workspace.source_project_root,
        workspace.output_root,
    )
    with pytest.raises(ValueError, match="control authority is compromised"):
        reloaded.open_existing()


def test_missing_trial_root_cannot_bypass_control_witness_cleanup(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)

    with pytest.raises(RuntimeError, match="worktree cleanup"):
        with workspace.trial_worktree(1, _surface(workspace)) as (
            trial_root,
            _trial_surface,
        ):
            (workspace.repo_root / ".git" / "info" / "attributes").write_text(
                f"{ANALYSIS_FILE} text eol=lf\n",
                encoding="utf-8",
            )
            shutil.rmtree(trial_root)

    assert workspace.git_control_compromise_path.is_file()
    with pytest.raises(ValueError, match="control authority is compromised"):
        workspace.create_worktree(
            2,
            editable_files=[ANALYSIS_FILE, HELPER_FILE],
        )


def test_commit_rejects_new_empty_directory_before_cas(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        (trial_root / ANALYSIS_FILE).write_text("threshold = 2\n", encoding="utf-8")
        (trial_root / "skills" / "test" / "runtime-empty").mkdir()

        _assert_commit_rejected_without_authority_change(
            workspace,
            lambda: _commit_accepted(workspace,
                iteration=1,
                worktree=trial_root,
                patch=_plan(),
                modified_files=[ANALYSIS_FILE],
            ),
        )


def test_commit_rejects_existing_directory_mode_change_before_cas(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        (trial_root / ANALYSIS_FILE).write_text("threshold = 2\n", encoding="utf-8")
        os.chmod(trial_root / "skills" / "test", 0o700)

        _assert_commit_rejected_without_authority_change(
            workspace,
            lambda: _commit_accepted(workspace,
                iteration=1,
                worktree=trial_root,
                patch=_plan(),
                modified_files=[ANALYSIS_FILE],
            ),
        )


def test_commit_accepts_tracked_target_matching_git_exclude_rule(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(
        tmp_path,
        gitignore=f"/{ANALYSIS_FILE}\n",
    )

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        (trial_root / ANALYSIS_FILE).write_text("threshold = 2\n", encoding="utf-8")
        record = _commit_accepted(workspace,
            iteration=1,
            worktree=trial_root,
            patch=_plan(),
            modified_files=[ANALYSIS_FILE],
        )

    assert workspace.durable_accepted_head_record().commit_hash == record.commit_hash


def test_commit_rejects_orphan_worktree_head_before_cas(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)

    with pytest.raises(RuntimeError, match="worktree cleanup"):
        with workspace.trial_worktree(1, _surface(workspace)) as (
            trial_root,
            _trial_surface,
        ):
            _git(
                workspace,
                "checkout",
                "--orphan",
                "divergent-candidate",
                cwd=trial_root,
            )
            (trial_root / ANALYSIS_FILE).write_text(
                "threshold = 2\n",
                encoding="utf-8",
            )

            _assert_commit_rejected_without_authority_change(
                workspace,
                lambda: _commit_accepted(workspace,
                    iteration=1,
                    worktree=trial_root,
                    patch=_plan(),
                    modified_files=[ANALYSIS_FILE],
                ),
            )

    assert workspace.git_control_compromise_path.is_file()


@pytest.mark.parametrize("mismatch", ["target_files", "diff_file"])
def test_commit_rejects_patch_plan_file_set_mismatch_before_cas(
    tmp_path: Path,
    mismatch: str,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        (trial_root / ANALYSIS_FILE).write_text("threshold = 2\n", encoding="utf-8")
        patch = (
            _plan(target_files=[HELPER_FILE])
            if mismatch == "target_files"
            else _plan(
                old_code="scale = 10",
                new_code="scale = 20",
                target_files=[ANALYSIS_FILE],
                diff_file=HELPER_FILE,
            )
        )

        _assert_commit_rejected_without_authority_change(
            workspace,
            lambda: _commit_accepted(workspace,
                iteration=1,
                worktree=trial_root,
                patch=patch,
                modified_files=[ANALYSIS_FILE],
            ),
        )


def test_commit_rejects_hunks_that_do_not_produce_candidate_bytes(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        (trial_root / ANALYSIS_FILE).write_text("threshold = 2\n", encoding="utf-8")
        contradictory_patch = _plan(new_code="threshold = 999")

        _assert_commit_rejected_without_authority_change(
            workspace,
            lambda: _commit_accepted(workspace,
                iteration=1,
                worktree=trial_root,
                patch=contradictory_patch,
                modified_files=[ANALYSIS_FILE],
            ),
        )


def test_commit_rejects_file_mode_change_before_cas(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        trial_file = trial_root / ANALYSIS_FILE
        trial_file.write_text("threshold = 2\n", encoding="utf-8")
        os.chmod(trial_file, 0o755)

        _assert_commit_rejected_without_authority_change(
            workspace,
            lambda: _commit_accepted(workspace,
                iteration=1,
                worktree=trial_root,
                patch=_plan(),
                modified_files=[ANALYSIS_FILE],
            ),
        )


def test_commit_refuses_to_extend_chain_with_missing_upstream_manifest(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface(workspace)) as (
        first_root,
        _first_surface,
    ):
        (first_root / ANALYSIS_FILE).write_text("threshold = 2\n", encoding="utf-8")
        first_record = _commit_accepted(workspace,
            iteration=1,
            worktree=first_root,
            patch=_plan(),
            modified_files=[ANALYSIS_FILE],
        )

    Path(first_record.manifest_path).unlink()
    second_patch = _plan(
        old_code="threshold = 2",
        new_code="threshold = 3",
    )

    with workspace.trial_worktree(2, _surface(workspace)) as (
        second_root,
        _second_surface,
    ):
        (second_root / ANALYSIS_FILE).write_text("threshold = 3\n", encoding="utf-8")
        _assert_commit_rejected_without_authority_change(
            workspace,
            lambda: _commit_accepted(workspace,
                iteration=2,
                worktree=second_root,
                patch=second_patch,
                modified_files=[ANALYSIS_FILE],
            ),
        )


@pytest.mark.parametrize("iteration", [0, -1, True])
def test_commit_rejects_non_positive_or_boolean_iteration_before_cas(
    tmp_path: Path,
    iteration: int,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        (trial_root / ANALYSIS_FILE).write_text("threshold = 2\n", encoding="utf-8")
        _assert_commit_rejected_without_authority_change(
            workspace,
            lambda: _commit_accepted(workspace,
                iteration=iteration,
                worktree=trial_root,
                patch=_plan(),
                modified_files=[ANALYSIS_FILE],
            ),
        )


def test_commit_rejects_foreign_registered_worktree_before_cas(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)
    foreign_worktree = tmp_path / "foreign-worktree"
    _git(
        workspace,
        "worktree",
        "add",
        "--detach",
        str(foreign_worktree),
        workspace.accepted_commit,
    )
    try:
        (foreign_worktree / ANALYSIS_FILE).write_text(
            "threshold = 2\n",
            encoding="utf-8",
        )
        _assert_commit_rejected_without_authority_change(
            workspace,
            lambda: _commit_accepted(workspace,
                iteration=1,
                worktree=foreign_worktree,
                patch=_plan(),
                modified_files=[ANALYSIS_FILE],
            ),
        )
    finally:
        _git(
            workspace,
            "worktree",
            "remove",
            "--force",
            str(foreign_worktree),
        )


def test_commit_rolls_back_ref_when_post_cas_reauthentication_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)
    real_authenticate = workspace._authenticate_accepted_chain
    calls = 0

    def fail_second_authentication(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("injected post-CAS authentication failure")
        return real_authenticate(*args, **kwargs)

    monkeypatch.setattr(
        workspace,
        "_authenticate_accepted_chain",
        fail_second_authentication,
    )
    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        (trial_root / ANALYSIS_FILE).write_text("threshold = 2\n", encoding="utf-8")
        _assert_commit_rejected_without_authority_change(
            workspace,
            lambda: _commit_accepted(workspace,
                iteration=1,
                worktree=trial_root,
                patch=_plan(),
                modified_files=[ANALYSIS_FILE],
            ),
        )


def test_durable_reauth_rejects_consistent_patch_plan_metadata_tamper(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        (trial_root / ANALYSIS_FILE).write_text("threshold = 2\n", encoding="utf-8")
        record = _commit_accepted(workspace,
            iteration=1,
            worktree=trial_root,
            patch=_plan(),
            modified_files=[ANALYSIS_FILE],
        )

    manifest_path = Path(record.manifest_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["reasoning"] = "forged but internally duplicated reasoning"
    payload["patch_plan"]["reasoning"] = payload["reasoning"]
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    with pytest.raises(ValueError, match="manifest|PatchPlan"):
        workspace.durable_accepted_head_record()


def test_commit_rejects_consistent_manifest_metadata_tamper_before_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)
    real_writer = harness_workspace_module.atomic_write_owned_output_text

    def write_tampered_manifest(*args: Any, **kwargs: Any) -> Path:
        if kwargs.get("label") == "accepted patch manifest":
            payload = json.loads(kwargs["text"])
            payload["reasoning"] = "forged but internally duplicated reasoning"
            payload["patch_plan"]["reasoning"] = payload["reasoning"]
            kwargs["text"] = json.dumps(payload, indent=2)
        return real_writer(*args, **kwargs)

    monkeypatch.setattr(
        harness_workspace_module,
        "atomic_write_owned_output_text",
        write_tampered_manifest,
    )
    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        (trial_root / ANALYSIS_FILE).write_text("threshold = 2\n", encoding="utf-8")
        _assert_commit_rejected_without_authority_change(
            workspace,
            lambda: _commit_accepted(workspace,
                iteration=1,
                worktree=trial_root,
                patch=_plan(),
                modified_files=[ANALYSIS_FILE],
            ),
        )


def test_commit_rejects_sandbox_git_directory_alias_before_any_cas(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)

    with pytest.raises(RuntimeError, match="worktree cleanup"):
        with workspace.trial_worktree(1, _surface(workspace)) as (
            trial_root,
            _trial_surface,
        ):
            (trial_root / ANALYSIS_FILE).write_text(
                "threshold = 2\n",
                encoding="utf-8",
            )
            previous_head = _accepted_head(workspace)
            external_git = tmp_path / "external-git-authority"
            sandbox_git = workspace.repo_root / ".git"
            sandbox_git.rename(external_git)
            os.symlink(external_git, sandbox_git, target_is_directory=True)
            try:
                with pytest.raises(
                    ValueError,
                    match="Git control|Git authority|alias",
                ):
                    _commit_accepted(workspace,
                        iteration=1,
                        worktree=trial_root,
                        patch=_plan(),
                        modified_files=[ANALYSIS_FILE],
                    )
                assert (
                    subprocess.run(
                        [
                            "git",
                            f"--git-dir={external_git}",
                            "rev-parse",
                            f"refs/heads/{workspace.accepted_branch}^{{commit}}",
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout.strip()
                    == previous_head
                )
                assert _artifact_state(workspace) == {}
            finally:
                sandbox_git.unlink()
                external_git.rename(sandbox_git)

    assert workspace.git_control_compromise_path.is_file()


def test_durable_reauth_rejects_timestamp_and_source_identity_tamper(
    tmp_path: Path,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        (trial_root / ANALYSIS_FILE).write_text("threshold = 2\n", encoding="utf-8")
        record = _commit_accepted(workspace,
            iteration=1,
            worktree=trial_root,
            patch=_plan(),
            modified_files=[ANALYSIS_FILE],
        )

    manifest_path = Path(record.manifest_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["timestamp"] = "2099-01-01T00:00:00+00:00"
    payload["source_project_commit"] = "f" * 40
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    with pytest.raises(ValueError, match="manifest|evidence|commit"):
        workspace.durable_accepted_head_record()


def test_failed_writer_retains_candidate_evidence_reachable_from_new_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)
    real_authenticate = workspace._authenticate_accepted_chain
    candidate_hash = ""

    def advance_to_descendant_then_fail(*args: Any, **kwargs: Any) -> Any:
        nonlocal candidate_hash
        candidate_hash = kwargs["accepted_commit"]
        candidate_tree = _git(
            workspace,
            "rev-parse",
            f"{candidate_hash}^{{tree}}",
        )
        descendant = _git(
            workspace,
            "commit-tree",
            candidate_tree,
            "-p",
            candidate_hash,
            "-m",
            "concurrent accepted descendant",
        )
        _git(
            workspace,
            "update-ref",
            f"refs/heads/{workspace.accepted_branch}",
            descendant,
            workspace.accepted_commit,
        )
        raise ValueError("injected concurrent writer after evidence publication")

    monkeypatch.setattr(
        workspace,
        "_authenticate_accepted_chain",
        advance_to_descendant_then_fail,
    )
    with pytest.raises(RuntimeError, match="clean Git authority"):
        with workspace.trial_worktree(1, _surface(workspace)) as (
            trial_root,
            _trial_surface,
        ):
            (trial_root / ANALYSIS_FILE).write_text(
                "threshold = 2\n",
                encoding="utf-8",
            )
            with pytest.raises(RuntimeError, match="referenced|retained"):
                _commit_accepted(
                    workspace,
                    iteration=1,
                    worktree=trial_root,
                    patch=_plan(),
                    modified_files=[ANALYSIS_FILE],
                )

    monkeypatch.setattr(workspace, "_authenticate_accepted_chain", real_authenticate)
    assert candidate_hash
    state = json.loads(
        workspace.git_control_state_path.read_text(encoding="utf-8")
    )
    assert state["status"] == "trial_open"
    assert workspace.git_control_compromise_path.is_file()
    reloaded = HarnessWorkspace(
        workspace.source_project_root,
        workspace.output_root,
    )
    with pytest.raises(ValueError, match="control authority is compromised"):
        reloaded.open_existing()
    evidence = list(workspace.accepted_artifacts_root.glob(f"*_{candidate_hash[:12]}.*"))
    assert sorted(path.suffix for path in evidence) == [".json", ".patch"]


def test_failure_cleanup_locks_ref_across_reachability_check_and_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)
    previous_head = workspace.accepted_commit
    previous_artifacts = _artifact_state(workspace)
    real_git = workspace._git
    real_remove = workspace._remove_owned_metadata_file
    candidate_hash = ""
    race_returncode: int | None = None

    def fail_candidate_cas(
        args: list[str],
        *,
        cwd: Path,
        check: bool = True,
    ) -> Any:
        nonlocal candidate_hash
        if (
            len(args) == 4
            and args[0] == "update-ref"
            and args[1] == f"refs/heads/{workspace.accepted_branch}"
            and args[3] == previous_head
        ):
            candidate_hash = args[2]
            raise RuntimeError("injected candidate CAS failure")
        return real_git(args, cwd=cwd, check=check)

    def race_ref_before_remove(path: Path) -> None:
        nonlocal race_returncode
        if race_returncode is None:
            race = subprocess.run(
                [
                    "git",
                    "-C",
                    str(workspace.repo_root),
                    "update-ref",
                    f"refs/heads/{workspace.accepted_branch}",
                    candidate_hash,
                    previous_head,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            race_returncode = race.returncode
        real_remove(path)

    monkeypatch.setattr(workspace, "_git", fail_candidate_cas)
    monkeypatch.setattr(workspace, "_remove_owned_metadata_file", race_ref_before_remove)
    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        (trial_root / ANALYSIS_FILE).write_text("threshold = 2\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="injected candidate CAS failure"):
            _commit_accepted(workspace,
                iteration=1,
                worktree=trial_root,
                patch=_plan(),
                modified_files=[ANALYSIS_FILE],
            )

    assert candidate_hash
    assert race_returncode not in {None, 0}
    assert _accepted_head(workspace) == previous_head
    assert _artifact_state(workspace) == previous_artifacts


def test_commit_uses_utf8_independently_of_process_locale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)
    monkeypatch.setattr(locale, "getencoding", lambda: "ascii")
    patch = _plan()
    patch.description = "提高受管阈值"
    patch.reasoning = "所有硬门均已通过"

    with workspace.trial_worktree(1, _surface(workspace)) as (
        trial_root,
        _trial_surface,
    ):
        (trial_root / ANALYSIS_FILE).write_text("threshold = 2\n", encoding="utf-8")
        record = _commit_accepted(workspace,
            iteration=1,
            worktree=trial_root,
            patch=patch,
            modified_files=[ANALYSIS_FILE],
        )

    assert workspace.durable_accepted_head_record().commit_hash == record.commit_hash


@pytest.mark.parametrize("next_iteration", [2, 1])
def test_commit_requires_iteration_to_advance_past_accepted_parent(
    tmp_path: Path,
    next_iteration: int,
) -> None:
    workspace, _analysis_file, _helper_file = _workspace(tmp_path)

    with workspace.trial_worktree(2, _surface(workspace)) as (
        first_root,
        _first_surface,
    ):
        (first_root / ANALYSIS_FILE).write_text("threshold = 2\n", encoding="utf-8")
        _commit_accepted(workspace,
            iteration=2,
            worktree=first_root,
            patch=_plan(),
            modified_files=[ANALYSIS_FILE],
        )

    second_patch = _plan(
        old_code="threshold = 2",
        new_code="threshold = 3",
    )
    with workspace.trial_worktree(next_iteration, _surface(workspace)) as (
        second_root,
        _second_surface,
    ):
        (second_root / ANALYSIS_FILE).write_text("threshold = 3\n", encoding="utf-8")
        _assert_commit_rejected_without_authority_change(
            workspace,
            lambda: _commit_accepted(workspace,
                iteration=next_iteration,
                worktree=second_root,
                patch=second_patch,
                modified_files=[ANALYSIS_FILE],
            ),
        )
