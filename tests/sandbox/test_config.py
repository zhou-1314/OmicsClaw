"""``SandboxConfig``: native OS sandbox defaults, domain groups, and validation."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from omicsclaw.sandbox import (
    BackendName,
    DEFAULT_ALLOWED_DOMAINS,
    DEFAULT_DENY_READ_PATHS,
    DEFAULT_DENY_WRITE_GLOBS,
    EGRESS_DENYLIST,
    EGRESS_GROUPS,
    EgressMode,
    Level,
    Mount,
    SandboxConfig,
    SandboxConfigError,
    SandboxMode,
    host_user,
)


def test_defaults_are_calibrated_for_native_os_sandbox():
    config = SandboxConfig()

    assert config.mode == SandboxMode.AUTO
    assert config.backend == BackendName.AUTO
    assert config.egress == EgressMode.PROXY
    assert config.isolates_network
    assert config.sandbox_kernels is True
    assert config.egress_groups == ("pkg",)
    assert config.pids_limit == 65536
    assert config.tmpfs_size == "64g"
    assert config.shm_size == "128g"
    assert config.nofile == 65536
    assert config.bootstrap == ""
    assert config.bootstrap_timeout_s == 600.0
    assert config.user == host_user()


def test_mode_syncs_concrete_backend():
    assert SandboxConfig(mode=SandboxMode.BWRAP).backend == BackendName.BWRAP
    assert SandboxConfig(mode=SandboxMode.NETNS).backend == BackendName.NETNS
    assert SandboxConfig(mode=SandboxMode.SEATBELT).backend == BackendName.SEATBELT
    assert SandboxConfig(mode=SandboxMode.APPCONTAINER).backend == BackendName.APPCONTAINER
    assert SandboxConfig(mode="docker").mode == SandboxMode.AUTO
    assert SandboxConfig(mode="local").mode == SandboxMode.AUTO


def test_egress_modes_and_network_isolation():
    assert SandboxConfig(egress=EgressMode.PROXY).isolates_network
    assert SandboxConfig(egress=EgressMode.NONE).isolates_network
    assert not SandboxConfig(egress=EgressMode.OPEN).isolates_network
    assert not SandboxConfig(network="bridge").isolates_network


def test_domain_groups_and_denylist_are_populated():
    assert {"pkg", "bio", "git", "registries", "default"} <= set(EGRESS_GROUPS)
    assert "pypi.org" in DEFAULT_ALLOWED_DOMAINS
    assert "eutils.ncbi.nlm.nih.gov" in EGRESS_GROUPS["bio"]
    assert "pastebin.com" in EGRESS_DENYLIST
    assert "*.ngrok-free.app" in EGRESS_DENYLIST
    assert "~/.ssh" in DEFAULT_DENY_READ_PATHS
    assert ".git/hooks" in DEFAULT_DENY_WRITE_GLOBS
    assert ".omicsclaw" in DEFAULT_DENY_WRITE_GLOBS


@pytest.mark.parametrize(
    "overrides",
    [
        {"mode": "invalid-mode"},
        {"backend": "invalid-backend"},
        {"egress": "invalid-egress"},
        {"image": "--privileged"},
        {"image": "a b"},
        {"network": "-host"},
        {"memory": "--oom-kill-disable"},
        {"cpus": "1 --privileged"},
        {"user": ""},
        {"runtime": " "},
        {"pids_limit": 0},
        {"start_timeout_s": 0},
        {"bootstrap_timeout_s": -1},
        {"stop_grace_s": -1},
        {"read_only_mounts": (Path("relative/data"),)},
        {"read_only_mounts": (Path("/data/a:b"),)},
    ],
)
def test_invalid_values_raise_sandbox_config_error(overrides):
    with pytest.raises(SandboxConfigError):
        SandboxConfig(**overrides)


def test_mounts_and_extra_paths_are_normalised_and_frozen():
    m_ro = Mount(source=Path("/ref/genome"), writable=False)
    m_rw = Mount(source=Path("/scratch/work"), writable=True)
    config = SandboxConfig(
        read_only_mounts=("/ref/hg38",),  # type: ignore[arg-type]
        mounts=(m_ro, m_rw),
        egress_allow=("api.custom.org",),
    )

    assert Path("/ref/hg38") in config.extra_read_paths
    assert Path("/ref/genome") in config.extra_read_paths
    assert Path("/scratch/work") in config.extra_write_paths
    assert "api.custom.org" in config.effective_allowed_domains
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.egress = EgressMode.OPEN  # type: ignore[misc]


def test_the_error_is_a_value_error():
    assert issubclass(SandboxConfigError, ValueError)
    assert Level.ISOLATED == Level.FULL
    assert Level.NET_ONLY == Level.PARTIAL
