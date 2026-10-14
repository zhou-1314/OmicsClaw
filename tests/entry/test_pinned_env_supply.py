"""Pinned supply publishes ready only after actual isolated interpreter checks."""

import asyncio
import hashlib
import sys
from pathlib import Path

import pytest


def manifest(lock):
    import platform
    import sysconfig

    return {
        "schemaVersion": 1,
        "role": "science",
        "pythonVersion": platform.python_version(),
        "platform": sys.platform,
        "machine": platform.machine(),
        "abi": sysconfig.get_config_var("SOABI"),
        "lockSha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "imports": [],
    }


def test_pinned_supply_is_atomic_and_concurrent_calls_reuse_one_environment(tmp_path):
    from omicsclaw.entry.env_supply import PinnedEnvironmentSupply

    lock = tmp_path / "science.lock"
    lock.write_text("")
    supplier = PinnedEnvironmentSupply(tmp_path / "owned-envs")
    declared = manifest(lock)

    async def scenario():
        first, second = await asyncio.gather(
            *(
                supplier.supply(declared, lock=lock, interpreter=Path(sys.executable))
                for _ in range(2)
            )
        )
        assert (first["environmentDigest"], first["python"], first["ready"]) == (
            second["environmentDigest"],
            second["python"],
            True,
        )
        assert (Path(first["root"]) / "ready.json").is_file()
        assert first["reused"] != second["reused"]

    asyncio.run(scenario())


def test_wrong_lock_or_abi_never_publishes_ready(tmp_path):
    from omicsclaw.entry.env_supply import PinnedEnvironmentSupply

    lock = tmp_path / "science.lock"
    lock.write_text("")
    supplier = PinnedEnvironmentSupply(tmp_path / "owned-envs")
    declared = manifest(lock)
    declared["abi"] = "unknown-abi"
    with pytest.raises(ValueError, match="ABI"):
        asyncio.run(
            supplier.supply(declared, lock=lock, interpreter=Path(sys.executable))
        )
    assert not list((tmp_path / "owned-envs").rglob("ready.json"))
    declared = manifest(lock)
    lock.write_text("changed-lock")
    with pytest.raises(ValueError, match="lock"):
        asyncio.run(
            supplier.supply(declared, lock=lock, interpreter=Path(sys.executable))
        )


def test_post_move_verification_failure_does_not_leave_ready(tmp_path, monkeypatch):
    from omicsclaw.entry.env_supply import PinnedEnvironmentSupply

    lock = tmp_path / "science.lock"
    lock.write_text("")
    supplier = PinnedEnvironmentSupply(tmp_path / "owned-envs")
    calls = []
    verify = supplier._verify

    def observed(python, declared):
        calls.append(str(python))
        if len(calls) == 2:
            raise ValueError("owned final ABI failure")
        return verify(python, declared)

    monkeypatch.setattr(supplier, "_verify", observed)
    with pytest.raises(ValueError, match="final ABI"):
        asyncio.run(
            supplier.supply(manifest(lock), lock=lock, interpreter=Path(sys.executable))
        )
    assert not list(supplier.root.rglob("ready.json"))
    monkeypatch.setattr(supplier, "_verify", verify)
    restored = asyncio.run(
        supplier.supply(manifest(lock), lock=lock, interpreter=Path(sys.executable))
    )
    assert restored["ready"] is True
