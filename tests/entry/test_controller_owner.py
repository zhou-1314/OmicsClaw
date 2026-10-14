"""Owner metadata explains the kernel fence without treating a reused PID as ownership."""

import json
import os

import pytest
from omicsclaw.entry.ssh.controller import WorkspaceOwner


def test_owner_identity_and_kernel_fence_survive_stale_metadata(tmp_path):
    owner = WorkspaceOwner(tmp_path, "first")
    try:
        data = json.loads((tmp_path / ".omicsclaw/controller.owner").read_text())
        assert data["pid"] == os.getpid()
        assert data["host"] and data["user"] and data["startTime"] and data["nonce"]
        with pytest.raises(BlockingIOError):
            WorkspaceOwner(tmp_path, "second")
    finally:
        owner.close()
    next_owner = WorkspaceOwner(tmp_path, "second")
    try:
        current = json.loads((tmp_path / ".omicsclaw/controller.owner").read_text())
        assert current["nonce"] != data["nonce"]
        assert current["startTime"] == data["startTime"]
    finally:
        next_owner.close()


def test_owner_refuses_unsafe_existing_lock_inode(tmp_path):
    state = tmp_path / ".omicsclaw"
    state.mkdir()
    other = tmp_path / "other"
    other.write_text("")
    os.link(other, state / "controller.owner")
    with pytest.raises(ValueError, match="unsafe controller owner"):
        WorkspaceOwner(tmp_path, "first")
