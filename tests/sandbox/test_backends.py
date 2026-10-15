"""Unit tests for native OS sandbox backends (bwrap, netns, seatbelt, appcontainer, bridge)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from omicsclaw.sandbox import (
    EgressMode,
    SandboxConfig,
    build_analysis_profile,
    build_appcontainer_policy,
    build_bwrap_argv,
    build_netns_argv,
    build_seatbelt_argv,
    render_seatbelt_profile,
)
from omicsclaw.sandbox._bridge import LoopbackUnixBridge, build_bridge_argv
from omicsclaw.sandbox._win_shims import (
    SHIMMED_COMMANDS,
    check_shim_invocation,
    ensure_win_shim_dir,
)


def test_bwrap_argv_masks_credentials_and_protects_workspace(tmp_path: Path):
    fake_home = tmp_path / "home"
    (fake_home / ".ssh").mkdir(parents=True)
    (fake_home / ".ssh" / "id_rsa").write_text("PRIVATE-KEY", encoding="utf-8")
    (fake_home / ".netrc").write_text("machine github.com login x", encoding="utf-8")

    ws = tmp_path / "ws"
    (ws / ".git" / "hooks").mkdir(parents=True)
    (ws / ".omicsclaw").mkdir(parents=True)
    (ws / ".bashrc").write_text("echo pwned", encoding="utf-8")

    sock_dir = tmp_path / "sock"
    sock_dir.mkdir()
    uds = sock_dir / "egress.sock"

    cfg = SandboxConfig(egress=EgressMode.PROXY)
    profile = build_analysis_profile(ws, cfg, home=fake_home)
    argv = build_bwrap_argv(
        ["bash", "-c", "echo ok"],
        workspace=ws,
        cwd=ws,
        config=cfg,
        bwrap_bin="/usr/bin/bwrap",
        unix_socket_path=uds,
        proxy_port=31280,
        home=fake_home,
        profile=profile,
    )

    assert argv[0] == "/usr/bin/bwrap"
    assert "--unshare-net" in argv
    assert "--unshare-pid" in argv
    assert "--die-with-parent" in argv
    assert "--new-session" in argv
    # ~/.ssh directory is masked with --tmpfs
    ssh_idx = argv.index(str((fake_home / ".ssh").resolve()))
    assert argv[ssh_idx - 1] == "--tmpfs"
    # ~/.netrc file is masked with /dev/null
    netrc_idx = argv.index(str((fake_home / ".netrc").resolve()))
    assert argv[netrc_idx - 2 : netrc_idx] == ["--ro-bind", "/dev/null"]
    # .git/hooks and .omicsclaw inside workspace are remounted read-only AFTER --bind ws ws
    ws_bind_idx = argv.index("--bind")
    hooks_idx = argv.index(str((ws / ".git" / "hooks").resolve()))
    assert hooks_idx > ws_bind_idx
    assert argv[hooks_idx - 1] == "--ro-bind"
    # Bridge wraps the inner command
    assert "--unix-socket" in argv
    assert "--listen-port" in argv


def test_netns_argv_uses_unshare_and_bridge(tmp_path: Path):
    uds = tmp_path / "egress.sock"
    cfg = SandboxConfig(egress=EgressMode.PROXY)
    argv = build_netns_argv(
        ["bash", "-c", "id"],
        config=cfg,
        unshare_bin="/usr/bin/unshare",
        unix_socket_path=uds,
        proxy_port=45678,
    )

    assert argv[:6] == [
        "/usr/bin/unshare",
        "--user",
        "--map-root-user",
        "--pid",
        "--fork",
        "--kill-child=SIGKILL",
    ]
    assert "--net" in argv
    assert "--unix-socket" in argv
    assert "45678" in argv


def test_seatbelt_profile_enforces_credential_and_workspace_protections(tmp_path: Path):
    fake_home = tmp_path / "home"
    (fake_home / ".ssh").mkdir(parents=True)
    ws = tmp_path / "ws"
    (ws / ".git" / "hooks").mkdir(parents=True)
    (ws / ".omicsclaw").mkdir(parents=True)

    cfg = SandboxConfig(egress=EgressMode.PROXY)
    sbpl = render_seatbelt_profile(
        ws,
        egress=cfg.egress,
        proxy_port=51234,
        home=fake_home,
    )

    assert "(version 1)" in sbpl
    assert '(deny file-read* (subpath "' in sbpl
    assert ".ssh" in sbpl
    # Workspace allow must appear BEFORE .git/hooks and .omicsclaw deny-write (last-match-wins)
    allow_ws_pos = sbpl.index(f'(allow file-write* (subpath "{ws.resolve()}"))')
    deny_hooks_pos = sbpl.index(".git/hooks")
    deny_oc_pos = sbpl.index(".omicsclaw")
    assert allow_ws_pos < deny_hooks_pos
    assert allow_ws_pos < deny_oc_pos
    assert '(allow network-outbound (remote tcp "localhost:51234"))' in sbpl

    argv = build_seatbelt_argv(
        ["bash", "-c", "true"],
        workspace=ws,
        config=cfg,
        proxy_port=51234,
        home=fake_home,
    )
    assert argv[0].endswith("sandbox-exec")
    assert argv[1] == "-p"


def test_appcontainer_policy_and_windows_shims(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / ".git" / "hooks").mkdir(parents=True)
    (ws / ".omicsclaw").mkdir(parents=True)

    cfg = SandboxConfig()
    policy = build_appcontainer_policy(ws, config=cfg, sid_string="S-1-15-2-999")
    assert policy.capability_count == 0
    assert policy.profile_name.startswith("omicsclaw.sandbox.")
    assert any("/grant" in cmd and str(ws.resolve()) in cmd for cmd in policy.icacls_commands)
    assert any("/grant:r" in cmd and ".git/hooks" in " ".join(cmd) for cmd in policy.icacls_commands)

    # Check Windows PATH command shim validation
    ok, _ = check_shim_invocation("rm", ["-rf", "results/out.txt"], workspace=ws)
    assert ok
    ok_hooks, reason = check_shim_invocation("rm", ["-rf", ".git/hooks/pre-commit"], workspace=ws)
    assert not ok_hooks and "Protected Path" in reason
    ok_git, reason_git = check_shim_invocation(
        "git", ["config", "core.hooksPath", ".evil"], workspace=ws
    )
    assert not ok_git and "Protected Path" in reason_git

    shim_dir = ensure_win_shim_dir(tmp_path / "shims", workspace=ws)
    assert (shim_dir / "sitecustomize.py").is_file()
    for cmd_name in SHIMMED_COMMANDS:
        assert (shim_dir / f"{cmd_name}.cmd").is_file()


def test_loopback_unix_bridge_streams_bidirectionally(tmp_path: Path):
    async def run() -> None:
        uds_path = tmp_path / "bridge.sock"

        async def handle_uds(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            data = await reader.readline()
            writer.write(b"echo:" + data)
            await writer.drain()
            writer.close()

        uds_server = await asyncio.start_unix_server(handle_uds, path=str(uds_path))
        bridge = LoopbackUnixBridge(uds_path, listen_port=0)
        await bridge.start()
        try:
            assert bridge.port > 0
            reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
            writer.write(b"ping-over-bridge\n")
            await writer.drain()
            reply = await reader.readline()
            writer.close()
            assert reply == b"echo:ping-over-bridge\n"
        finally:
            await bridge.stop()
            uds_server.close()
            await uds_server.wait_closed()

    asyncio.run(asyncio.wait_for(run(), timeout=10.0))
    argv = build_bridge_argv("/usr/bin/python3", ["bash", "-c", "id"], unix_socket=tmp_path / "s", listen_port=9999)
    assert "--unix-socket" in argv and "--listen-port" in argv



def test_bwrap_argv_without_explicit_profile_still_masks_dotenv_and_writable_subdirs(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / ".omicsclaw" / "sandbox" / "tmp").mkdir(parents=True)
    (ws / ".omicsclaw" / "kernel").mkdir(parents=True)
    (ws / ".env").write_text("OPENAI_API_KEY=secret", encoding="utf-8")

    cfg = SandboxConfig(egress=EgressMode.NONE, gpus="all")
    argv = build_bwrap_argv(
        ["bash", "-c", "id"],
        workspace=ws,
        cwd=ws,
        config=cfg,
        bwrap_bin="/usr/bin/bwrap",
        profile=None,
    )
    dotenv_str = str((ws / ".env").resolve())
    assert dotenv_str in argv
    idx = argv.index(dotenv_str)
    assert argv[idx - 2 : idx] == ["--ro-bind", "/dev/null"]
    assert "--unsetenv" in argv
    unset_idx = argv.index("JPY_PARENT_PID")
    assert argv[unset_idx - 1] == "--unsetenv"
    # .omicsclaw is ro-bind, then .omicsclaw/sandbox and .omicsclaw/kernel are writable --bind after it
    oc_ro_idx = argv.index(str((ws / ".omicsclaw").resolve()))
    assert argv[oc_ro_idx - 1] == "--ro-bind"
    sbx_rw_idx = argv.index(str((ws / ".omicsclaw" / "sandbox").resolve()))
    assert sbx_rw_idx > oc_ro_idx and argv[sbx_rw_idx - 1] == "--bind"


def test_appcontainer_denies_dotenv_and_win_shims_patch_af_pipe(tmp_path: Path):
    from omicsclaw.sandbox._win_shims import build_win32_sitecustomize_source

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".env").write_text("LLM_API_KEY=secret", encoding="utf-8")
    cfg = SandboxConfig()
    policy = build_appcontainer_policy(ws, config=cfg, sid_string="S-1-15-2-999")
    assert any(
        "/deny" in cmd and str((ws / ".env").resolve()) in cmd
        for cmd in policy.icacls_commands
    )
    src = build_win32_sitecustomize_source(sid_string="S-1-15-2-999", sink_file=tmp_path / "null.sink")
    assert "pipe" in src and "LOCAL" in src
    assert "arbitrary_address" in src
