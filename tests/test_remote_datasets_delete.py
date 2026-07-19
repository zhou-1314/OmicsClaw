"""``DELETE /datasets/{dataset_id}`` — remove a registered dataset.

Semantics:
- ``upload``-type datasets: the file lives inside the dataset dir, so
  fd-relative removal deletes both the file and ``meta.json``.
- ``import-remote``-type datasets: ``storage_uri`` points to a path
  outside the workspace; the dataset dir only holds ``meta.json``. The
  same command just unregisters it — **the source file MUST NOT be
  touched**, since deleting a user-provided path is a data-loss risk
  the backend cannot recover from.

Also verifies path-sandboxing on ``dataset_id`` and standard 404/204
HTTP semantics.
"""

from __future__ import annotations

import asyncio
import io
import os
from pathlib import Path
import threading

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from omicsclaw.control.run_runtime import RunRuntime  # noqa: E402
from omicsclaw.remote.app_integration import register_remote_routers  # noqa: E402
from omicsclaw.remote.auth import capture_remote_bearer_authority  # noqa: E402
from omicsclaw.remote.routers import datasets as datasets_module  # noqa: E402
from omicsclaw.remote.runtime_binding import (  # noqa: E402
    bind_remote_run_runtime,
    unbind_remote_run_runtime,
)


@pytest.fixture()
def client(monkeypatch, tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # Remote Dataset adapters consume the Backend-lifespan binding.  An
    # environment-only workspace must never become a request-time fallback.
    monkeypatch.delenv("OMICSCLAW_WORKSPACE", raising=False)
    monkeypatch.delenv("OMICSCLAW_REMOTE_AUTH_TOKEN", raising=False)
    runtime = object.__new__(RunRuntime)
    unbind_remote_run_runtime()
    bind_remote_run_runtime(runtime, workspace=workspace)
    app = FastAPI()
    capture_remote_bearer_authority(app, {})
    register_remote_routers(app)
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        unbind_remote_run_runtime(runtime)


def _upload_demo(
    client: TestClient, *, name: str = "demo.h5ad", payload: bytes | None = None
) -> str:
    if payload is None:
        payload = name.encode("utf-8") * 16  # vary content per name to dodge
                                             # checksum dedup from _upload_dataset
    response = client.post(
        "/datasets/upload",
        files={"file": (name, io.BytesIO(payload), "application/octet-stream")},
        data={"execution_target": "local"},
    )
    assert response.status_code == 200, response.text
    return response.json()["dataset_id"]


def _import_remote(client: TestClient, src: Path) -> str:
    response = client.post(
        "/datasets/import-remote",
        json={
            "remote_path": str(src),
            "execution_target": "remote:profile-a",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["dataset_id"]


def test_delete_upload_dataset_removes_file_and_meta(
    client: TestClient, tmp_path: Path
) -> None:
    dataset_id = _upload_demo(client)
    workspace = tmp_path / "workspace"
    dataset_dir = (
        workspace / ".omicsclaw" / "remote" / "datasets" / dataset_id
    )
    assert dataset_dir.is_dir()

    response = client.delete(f"/datasets/{dataset_id}")
    assert response.status_code == 204

    assert not dataset_dir.exists()
    listing = client.get("/datasets").json()
    assert all(d["dataset_id"] != dataset_id for d in listing["datasets"])


def test_delete_import_remote_dataset_preserves_source_file(
    client: TestClient, tmp_path: Path
) -> None:
    """CRITICAL: unregistering an imported path must not delete the
    user's source file. Tests put the source OUTSIDE the workspace so a
    naive ``shutil.rmtree`` on storage_uri would be caught."""
    source = tmp_path / "outside" / "big-cohort.h5ad"
    source.parent.mkdir()
    source.write_bytes(b"precious" * 128)

    dataset_id = _import_remote(client, source)

    response = client.delete(f"/datasets/{dataset_id}")
    assert response.status_code == 204

    # Source must still exist — removing the meta dir is fine, deleting
    # the referenced source is a data-loss bug.
    assert source.is_file(), (
        "DELETE /datasets/:id removed the user's source file — data loss"
    )
    assert source.read_bytes() == b"precious" * 128


def test_delete_unknown_dataset_returns_404(client: TestClient) -> None:
    response = client.delete("/datasets/not-a-real-id-xyz")
    assert response.status_code == 404


def test_delete_rejects_unsafe_dataset_id(client: TestClient) -> None:
    """Path-traversal attempts and absolute paths must never reach
    ``shutil.rmtree``.

    ``.`` / ``..`` get canonicalized by httpx into ``/datasets`` or
    ``/datasets/``, which the router resolves to the GET endpoint →
    405 Method Not Allowed. That's a correct rejection at the routing
    layer; anything in {400, 404, 405} means the dangerous id never hit
    the handler.
    """
    for bad in ("../etc/passwd", "foo/bar", "..", "."):
        response = client.delete(f"/datasets/{bad}")
        assert response.status_code in (400, 404, 405), (
            f"unsafe id {bad!r} accepted as {response.status_code}"
        )


def test_delete_is_idempotent_second_call_is_404(client: TestClient) -> None:
    dataset_id = _upload_demo(client, name="once.h5ad")
    first = client.delete(f"/datasets/{dataset_id}")
    assert first.status_code == 204
    second = client.delete(f"/datasets/{dataset_id}")
    assert second.status_code == 404


def test_delete_does_not_disturb_siblings(
    client: TestClient, tmp_path: Path
) -> None:
    keep_id = _upload_demo(client, name="keep.h5ad")
    drop_id = _upload_demo(client, name="drop.h5ad")

    response = client.delete(f"/datasets/{drop_id}")
    assert response.status_code == 204

    listing = client.get("/datasets").json()
    remaining_ids = {d["dataset_id"] for d in listing["datasets"]}
    assert keep_id in remaining_ids
    assert drop_id not in remaining_ids


@pytest.mark.parametrize("filename", ["meta.json", "meta.json.tmp", "META.JSON"])
def test_upload_rejects_metadata_filename_collisions_before_storage_creation(
    client: TestClient,
    tmp_path: Path,
    filename: str,
) -> None:
    response = client.post(
        "/datasets/upload",
        files={"file": (filename, io.BytesIO(b"payload"), "application/octet-stream")},
        data={"execution_target": "local"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "reserved_dataset_filename"
    assert not (tmp_path / "workspace" / ".omicsclaw").exists()


def test_dedup_verifies_full_content_after_matching_head_and_size(
    client: TestClient,
) -> None:
    prefix = b"A" * (64 * 1024)
    first_id = _upload_demo(
        client,
        name="first.bin",
        payload=prefix + b"X" * 128,
    )
    second_id = _upload_demo(
        client,
        name="second.bin",
        payload=prefix + b"Y" * 128,
    )

    assert first_id != second_id
    listing = client.get("/datasets")
    assert listing.status_code == 200
    assert {row["dataset_id"] for row in listing.json()["datasets"]} == {
        first_id,
        second_id,
    }


def test_list_projects_changed_imported_source_as_stale(
    client: TestClient,
    tmp_path: Path,
) -> None:
    source = tmp_path / "external" / "cohort.h5ad"
    source.parent.mkdir()
    source.write_bytes(b"original" * 32)
    dataset_id = _import_remote(client, source)
    source.write_bytes(b"replacement" * 128)

    response = client.get("/datasets")

    assert response.status_code == 200
    row = next(
        item for item in response.json()["datasets"] if item["dataset_id"] == dataset_id
    )
    assert row["status"] == "stale"


def test_dataset_root_symlink_is_rejected_without_touching_outside(
    client: TestClient,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside-datasets"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("do-not-touch", encoding="utf-8")
    remote = workspace / ".omicsclaw" / "remote"
    remote.mkdir(parents=True)
    try:
        (remote / "datasets").symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    response = client.get("/datasets")

    assert response.status_code == 409
    assert response.json()["detail"] == "unsafe_dataset_storage"
    assert secret.read_text(encoding="utf-8") == "do-not-touch"


def test_delete_remains_anchored_when_dataset_root_path_is_replaced(
    client: TestClient,
    tmp_path: Path,
    monkeypatch,
) -> None:
    dataset_id = _upload_demo(client, name="owned.bin", payload=b"owned")
    workspace = tmp_path / "workspace"
    datasets_path = workspace / ".omicsclaw" / "remote" / "datasets"
    detached = datasets_path.with_name("datasets-detached")
    outside = tmp_path / "outside-datasets"
    outside_victim = outside / dataset_id
    outside_victim.mkdir(parents=True)
    secret = outside_victim / "precious.txt"
    secret.write_text("must-survive", encoding="utf-8")
    real_remove = datasets_module._quarantine_and_remove_dataset

    def replace_path_then_remove(root_fd: int, candidate_id: str) -> None:
        datasets_path.rename(detached)
        datasets_path.symlink_to(outside, target_is_directory=True)
        real_remove(root_fd, candidate_id)

    monkeypatch.setattr(
        datasets_module,
        "_quarantine_and_remove_dataset",
        replace_path_then_remove,
    )

    response = client.delete(f"/datasets/{dataset_id}")

    assert response.status_code == 204
    assert secret.read_text(encoding="utf-8") == "must-survive"
    assert not (detached / dataset_id).exists()


def test_upload_rolls_back_when_lexical_dataset_root_is_replaced(
    client: TestClient,
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    datasets_path = workspace / ".omicsclaw" / "remote" / "datasets"
    detached = datasets_path.with_name("datasets-detached")
    real_write_meta = datasets_module._write_meta_at

    def replace_root_after_meta(dataset_fd: int, ref) -> None:
        real_write_meta(dataset_fd, ref)
        datasets_path.rename(detached)
        datasets_path.mkdir()

    monkeypatch.setattr(datasets_module, "_write_meta_at", replace_root_after_meta)

    response = client.post(
        "/datasets/upload",
        files={"file": ("cohort.bin", io.BytesIO(b"cohort"), "application/octet-stream")},
        data={"execution_target": "local"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "unsafe_dataset_storage"
    assert list(datasets_path.iterdir()) == []
    assert list(detached.iterdir()) == []


def test_import_rolls_back_if_source_path_is_atomically_replaced(
    client: TestClient,
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "external" / "source.h5ad"
    source.parent.mkdir()
    source.write_bytes(b"original" * 32)
    replacement = source.with_name("replacement.h5ad")
    replacement.write_bytes(b"replacement" * 512)
    real_write_meta = datasets_module._write_meta_at

    def replace_source_after_meta(dataset_fd: int, ref) -> None:
        real_write_meta(dataset_fd, ref)
        os.replace(replacement, source)

    monkeypatch.setattr(datasets_module, "_write_meta_at", replace_source_after_meta)

    response = client.post(
        "/datasets/import-remote",
        json={
            "remote_path": str(source),
            "execution_target": "remote:profile-a",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "unsafe_dataset_storage"
    assert source.read_bytes() == b"replacement" * 512
    listing = client.get("/datasets")
    assert listing.status_code == 200
    assert listing.json()["total"] == 0


def test_oversized_display_name_is_rejected_before_storage_creation(
    client: TestClient,
    tmp_path: Path,
) -> None:
    response = client.post(
        "/datasets/upload",
        files={"file": ("cohort.bin", io.BytesIO(b"cohort"), "application/octet-stream")},
        data={
            "execution_target": "local",
            "display_name": "x" * 1025,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "dataset_display_name_too_long"
    assert not (tmp_path / "workspace" / ".omicsclaw").exists()


def test_import_fifo_is_rejected_without_blocking(
    client: TestClient,
    tmp_path: Path,
) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO unavailable on this platform")
    fifo = tmp_path / "external.pipe"
    os.mkfifo(fifo)

    response = client.post(
        "/datasets/import-remote",
        json={
            "remote_path": str(fifo),
            "execution_target": "remote:profile-a",
        },
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_canceled_blocking_worker_keeps_an_exclusive_duplicate_fd(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original.bin"
    replacement = tmp_path / "replacement.bin"
    original.write_bytes(b"original")
    replacement.write_bytes(b"replacement")
    original_fd = os.open(original, os.O_RDONLY)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    observed: list[bytes] = []

    def delayed_read(worker_fd: int) -> None:
        started.set()
        assert release.wait(timeout=5)
        os.lseek(worker_fd, 0, os.SEEK_SET)
        observed.append(os.read(worker_fd, 64))
        finished.set()

    task = asyncio.create_task(
        datasets_module._run_with_duplicated_fd(delayed_read, original_fd)
    )
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    os.close(original_fd)

    replacement_fd = os.open(replacement, os.O_RDONLY)
    try:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        assert observed == [b"original"]
    finally:
        os.close(replacement_fd)
