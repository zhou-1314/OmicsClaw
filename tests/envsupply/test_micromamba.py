"""The micromamba groundwork: resolution ladder, probe, version parse.

Stretch scope by design — these pin the seam a later R-provisioning
phase builds on, not an installer that exists yet.
"""

from __future__ import annotations

from omicsclaw.envsupply.micromamba import (
    MicromambaLocation,
    build_probe_argv,
    locate_micromamba,
    parse_micromamba_version,
    probe_micromamba,
)


class _Env(dict):
    """env_get over a plain dict."""

    def __call__(self, name: str) -> str | None:
        return self.get(name)


def test_the_env_var_rung_wins_without_touching_the_fs():
    found = locate_micromamba(
        env_get=_Env(OMICSCLAW_MICROMAMBA_BIN="/opt/tools/micromamba"),
        file_exists=lambda path: (_ for _ in ()).throw(AssertionError(path)),
        which=lambda name: (_ for _ in ()).throw(AssertionError(name)),
    )
    assert found == MicromambaLocation("/opt/tools/micromamba", "env")


def test_the_installers_dir_rung_uses_the_platform_binary_name():
    env = _Env(OMICSCLAW_INSTALLERS_DIR="/Applications/OmicsClaw.app/Contents/Resources/installers/darwin-arm64")
    seen: list[str] = []

    def exists(candidate: str) -> bool:
        seen.append(candidate)
        return candidate.endswith("/micromamba")

    found = locate_micromamba(
        env_get=env, file_exists=exists, which=lambda name: None
    )
    assert found.from_ == "installers"
    assert found.path is not None and found.path.endswith("/micromamba")
    assert seen == [env["OMICSCLAW_INSTALLERS_DIR"] + "/micromamba"]

    win = locate_micromamba(
        env_get=_Env(OMICSCLAW_INSTALLERS_DIR="C:\\app\\resources\\installers\\win32-x64"),
        file_exists=lambda candidate: candidate.endswith("micromamba.exe"),
        which=lambda name: None,
        platform="win32",
    )
    assert win.path is not None and win.path.endswith("micromamba.exe")


def test_path_is_the_last_rung_and_none_is_the_honest_floor():
    on_path = locate_micromamba(
        env_get=_Env(), file_exists=lambda path: False, which=lambda name: "/usr/bin/micromamba"
    )
    assert on_path == MicromambaLocation("/usr/bin/micromamba", "path")
    nowhere = locate_micromamba(
        env_get=_Env(), file_exists=lambda path: False, which=lambda name: None
    )
    assert nowhere == MicromambaLocation(None, "none")


def test_the_probe_is_one_version_call():
    assert build_probe_argv("/opt/micromamba") == [
        "/opt/micromamba",
        "--version",
    ]


def test_the_version_parses_tolerantly():
    assert (
        parse_micromamba_version("micromamba 2.0.5\r\n")
        == "2.0.5"
    )
    assert parse_micromamba_version("banner line\nmicromamba 1.5.10") == "1.5.10"
    assert parse_micromamba_version("Segmentation fault") is None


def test_probe_answers_the_version_or_nothing():
    ok = probe_micromamba(
        MicromambaLocation("/opt/micromamba", "env"),
        runner=lambda argv: (0, "micromamba 2.0.5\n"),
    )
    assert ok == "2.0.5"
    crashed = probe_micromamba(
        MicromambaLocation("/opt/micromamba", "env"),
        runner=lambda argv: (139, ""),
    )
    assert crashed is None
    missing = probe_micromamba(MicromambaLocation(None, "none"))
    assert missing is None
