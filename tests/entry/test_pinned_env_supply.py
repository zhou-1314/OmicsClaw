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


def test_offline_wheel_cache_missing_package_never_publishes_ready(tmp_path):
    from omicsclaw.entry.env_supply import PinnedEnvironmentSupply

    cache = tmp_path / "owned-wheel-cache"
    cache.mkdir(mode=0o700)
    lock = tmp_path / "science.lock"
    lock.write_text("omicsclaw-absent-fixture==0.0.0 --hash=sha256:" + "a" * 64 + "\n")
    supplier = PinnedEnvironmentSupply(
        tmp_path / "owned-envs", wheelhouse=cache, timeout_s=30
    )
    with pytest.raises(ValueError, match="install failed"):
        asyncio.run(
            supplier.supply(manifest(lock), lock=lock, interpreter=Path(sys.executable))
        )
    assert not list(supplier.root.rglob("ready.json"))


def test_supplied_wheel_cache_symlink_is_refused_before_install(tmp_path):
    from omicsclaw.entry.env_supply import PinnedEnvironmentSupply

    actual = tmp_path / "actual-cache"
    actual.mkdir()
    alias = tmp_path / "symlink-cache"
    alias.symlink_to(actual, target_is_directory=True)
    lock = tmp_path / "science.lock"
    lock.write_text("")
    with pytest.raises(ValueError, match="wheelhouse"):
        supplier = PinnedEnvironmentSupply(tmp_path / "owned-envs", wheelhouse=alias)
        asyncio.run(
            supplier.supply(manifest(lock), lock=lock, interpreter=Path(sys.executable))
        )


def test_environment_verification_imports_write_cache_inside_the_owned_prefix(
    tmp_path, monkeypatch
):
    import json
    import subprocess

    from omicsclaw.entry.env_supply import PinnedEnvironmentSupply

    private = tmp_path / "owned-profile"
    python = private / "venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("owned verification fixture")
    outside = tmp_path / "simulated-user-home"
    lock = tmp_path / "science.lock"
    lock.write_text("")
    declared = manifest(lock)

    def probe(argv, *, env, **kwargs):
        home = Path(env.get("HOME", str(outside)))
        home.mkdir(parents=True, exist_ok=True)
        (home / "scientific-cache").write_text("created by verification import")
        facts = {
            key: declared[key]
            for key in ("pythonVersion", "platform", "machine", "abi")
        }
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(facts), stderr="")

    monkeypatch.setattr(subprocess, "run", probe)
    PinnedEnvironmentSupply._verify(python, declared)
    assert not outside.exists() and (private / "home/scientific-cache").is_file()
