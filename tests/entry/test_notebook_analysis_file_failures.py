"""Failed publications cannot escape the owner disk budget or leave live references."""

import asyncio

import pytest

from omicsclaw.entry.notebook.service import NotebookError
from omicsclaw.skillenv import notebook_identity as codec
from tests.entry.test_notebook_r import create

pytestmark = pytest.mark.usefixtures("notebook_control_rss")


def source(owner, count=5000):
    path = owner.analysis.files.staging / "input.ids"
    with path.open("wb") as stream:
        codec.write_rows(stream, ["id"], [[f"cell-{i:05d}"] for i in range(count)])
    return path


def artifact_count(owner):
    return owner.artifact_db.run(
        lambda c: c.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    )


def test_repeated_quota_refusal_removes_only_attempt_files_and_charges_staging(
    tmp_path,
):
    async def run():
        owner = await create(tmp_path)
        try:
            files = owner.analysis.files
            path = source(owner)
            owner.limits["diskBytes"] = files.disk_usage() + 128
            for _ in range(3):
                with pytest.raises(NotebookError, match="notebook_analysis_limit"):
                    files.transaction(
                        lambda c: files.copy_input(
                            c, "nb", "execution", path, ["id"], 5000
                        )
                    )
            assert list(files.owned.iterdir()) == [] and artifact_count(owner) == 0
            assert path.stat().st_size == 75034
            owner.limits["diskBytes"] = files.disk_usage() - 1
            with pytest.raises(NotebookError, match="notebook_resource_limit"):
                owner._budget()
        finally:
            await owner.close()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["copy", "members"])
def test_publication_followed_by_entity_rollback_cleans_new_rows_and_files(
    tmp_path, mode
):
    async def run():
        owner = await create(tmp_path)
        try:
            files = owner.analysis.files
            path = source(owner, 2)

            def publish(c):
                if mode == "copy":
                    return files.copy_input(c, "nb", "execution", path, ["id"], 2)
                view = {"viewId": "view", "viewRevision": 1, "axis": {"keys": ["id"]}}
                c.execute(
                    "INSERT OR IGNORE INTO notebook_analysis_view_rows VALUES('nb','view',1,0,?,?)",
                    (codec.tuple_bytes(["cell-00000"]), "{}"),
                )
                return files.members(c, "nb", view, values=[["cell-00000"]])[
                    "memberArtifact"
                ]

            kept = files.transaction(publish)

            def fail(c):
                publish(c)
                assert artifact_count(owner) == 2
                owner.analysis.store.put(
                    c, "nb", "dataset", {"datasetId": "rollback", "datasetRevision": 1}
                )
                raise ValueError("entity write failed")

            with pytest.raises(ValueError, match="entity write failed"):
                files.transaction(fail)
            assert artifact_count(owner) == 1
            assert [p.name for p in files.owned.iterdir()] == [
                kept["artifactId"] + ".ids"
            ]
            assert (
                owner.db.run(
                    lambda c: c.execute(
                        "SELECT count(*) FROM notebook_analysis_entities WHERE entity_id='rollback'"
                    ).fetchone()[0]
                )
                == 0
            )
        finally:
            await owner.close()

    asyncio.run(run())


def test_unregistered_owned_and_staging_files_remain_charged_after_owner_reopen(
    tmp_path,
):
    async def run():
        owner = await create(tmp_path)
        files = owner.analysis.files
        orphan = files.owned / "orphan.ids"
        orphan.write_bytes(b"x" * 100000)
        staging = source(owner)
        before = files.disk_usage()
        await owner.close()
        owner = await create(tmp_path)
        try:
            files = owner.analysis.files
            assert orphan.exists() and staging.exists() and artifact_count(owner) == 0
            dbbytes = sum(
                p.stat().st_size
                for p in (tmp_path / ".omicsclaw").glob("notebook*.db*")
                if p.is_file()
            )
            assert files.disk_usage() >= dbbytes + 175034
            owner.limits["diskBytes"] = dbbytes + 175033
            with pytest.raises(NotebookError, match="notebook_resource_limit"):
                owner._budget()
            assert before >= 175034
        finally:
            await owner.close()

    asyncio.run(run())


def test_process_death_between_artifact_and_entity_commit_retains_charged_evidence(
    tmp_path,
):
    import os
    import subprocess
    import sys

    script = """
import asyncio, os, sys
from pathlib import Path
from tests.entry.test_notebook_r import create
from omicsclaw.skillenv import notebook_identity as codec
async def main():
    owner = await create(Path(sys.argv[1]))
    path = owner.analysis.files.staging / "input.ids"
    with path.open("wb") as stream:
        codec.write_rows(stream, ["id"], [["a"]])
    def crash(c):
        result = owner.analysis.files.copy_input(c, "nb", "execution", path, ["id"], 1)
        owner.analysis.store.put(c, "nb", "selectionInput", result)
        os._exit(91)
    owner.analysis.files.transaction(crash)
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        env={**os.environ},
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 91, result.stderr.decode()

    async def verify():
        owner = await create(tmp_path)
        try:
            files = owner.analysis.files
            assert len(list(files.owned.iterdir())) == artifact_count(owner) == 1
            assert (
                owner.db.run(
                    lambda c: c.execute(
                        "SELECT COUNT(*) FROM notebook_analysis_entities WHERE kind='selectionInput'"
                    ).fetchone()[0]
                )
                == 0
            )
            assert files.disk_usage() >= sum(
                p.stat().st_size for p in files.directory.rglob("*") if p.is_file()
            )
            owner.limits["diskBytes"] = files.disk_usage() - 1
            with pytest.raises(NotebookError, match="notebook_resource_limit"):
                owner._budget()
        finally:
            await owner.close()

    asyncio.run(verify())
