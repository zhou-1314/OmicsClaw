"""Tests for ``omicsclaw.skill.execution.async_subprocess_driver`` (OMI-12 audit P1 #4).

The async driver replaces the ``asyncio.to_thread(run_skill)`` wrap that
``SkillRunnerExecutor`` used to do — pin the four corners of its
contract so a future "back to threads" change can't sneak in:

1. Happy-path captures stdout + stderr separately and returns exit_code=0.
2. ``-9 + result.json exists → 0`` legacy heuristic still fires (so
   skills that don't yet emit ``status: ok`` keep behaving the same).
3. ``status: ok`` overrides a non-zero exit code (the new P1 #2 contract
   carries over).
4. ``asyncio.CancelledError`` flows through and SIGTERMs the process
   group — even when the child ignores SIGTERM, the grace-period
   SIGKILL escalation closes the run promptly.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from omicsclaw.skill.execution import async_subprocess_driver as driver_module
from omicsclaw.skill.execution.async_subprocess_driver import (
    adrive_subprocess,
    governed_process_tree_supported,
    new_governed_process_tree_reference,
    reconcile_governed_process_tree,
)


def _write_script(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake_skill.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return script


def test_adrive_subprocess_captures_stdout_stderr_separately(tmp_path: Path):
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    script = _write_script(
        tmp_path,
        """
        import sys
        print("hello on stdout")
        print("warning on stderr", file=sys.stderr)
        """,
    )
    proc = asyncio.run(
        adrive_subprocess(
            [sys.executable, str(script)],
            cwd=tmp_path,
            env={},
            out_dir=out_dir,
        )
    )
    assert proc.returncode == 0
    assert "hello on stdout" in proc.stdout
    # stderr is captured separately — the executor merges them later when
    # writing ``ctx.stdout_log``.
    assert "warning on stderr" in proc.stderr


def test_adrive_subprocess_devnull_discards_large_untrusted_output(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """IPC-only workers must never aggregate arbitrary diagnostic output."""

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    canary = "autoagent-stdio-secret-canary"
    script = _write_script(
        tmp_path,
        f"""
        import os
        chunk = ({canary!r} + "x" * (1024 * 1024)).encode()
        for _ in range(16):
            os.write(1, chunk)
            os.write(2, chunk)
        """,
    )

    completed = asyncio.run(
        adrive_subprocess(
            [sys.executable, str(script)],
            cwd=tmp_path,
            env={},
            out_dir=out_dir,
            stdio="devnull",
        )
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert canary not in repr(completed)
    assert canary not in caplog.text


def test_async_driver_scrubs_supplied_backend_control_credentials(tmp_path: Path):
    control_keys = (
        "OMICSCLAW_REMOTE_AUTH_TOKEN",
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN",
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD",
    )
    child_env = os.environ.copy()
    child_env.update({key: "must-not-reach-skill" for key in control_keys})
    child_env["OMICSCLAW_ASYNC_DRIVER_TEST_KEEP"] = "ordinary-value"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    script = _write_script(
        tmp_path,
        f"""
        import json, os
        keys = {control_keys!r}
        print(json.dumps({{key: os.environ.get(key) for key in keys}}))
        print(os.environ.get("OMICSCLAW_ASYNC_DRIVER_TEST_KEEP", ""))
        """,
    )

    completed = asyncio.run(
        adrive_subprocess(
            [sys.executable, str(script)],
            cwd=tmp_path,
            env=child_env,
            out_dir=out_dir,
        )
    )

    lines = completed.stdout.splitlines()
    assert json.loads(lines[0]) == {key: None for key in control_keys}
    assert lines[1] == "ordinary-value"


def test_adrive_subprocess_keeps_minus_9_to_zero_heuristic(tmp_path: Path):
    """When a skill exits via SIGKILL but already produced ``result.json``,
    the driver classifies it as success — preserving the legacy heuristic
    so the 95 skills that don't yet emit a status field don't regress."""
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    # The script writes result.json then SIGKILLs itself so the runner
    # observes exit code -9 with an existing envelope (no ``status``).
    script = _write_script(
        tmp_path,
        f"""
        import json, os, pathlib, signal
        out = pathlib.Path({str(out_dir)!r})
        (out / "result.json").write_text(json.dumps({{"skill": "stub"}}))
        os.kill(os.getpid(), signal.SIGKILL)
        """,
    )
    proc = asyncio.run(
        adrive_subprocess(
            [sys.executable, str(script)],
            cwd=tmp_path,
            env={},
            out_dir=out_dir,
        )
    )
    assert (
        proc.returncode == 0
    ), f"SIGKILL with existing result.json must map to success; got {proc.returncode}"


def test_adrive_subprocess_honours_status_ok_over_non_zero_exit(tmp_path: Path):
    """``status: ok`` short-circuits exit-code analysis — same semantics
    the sync driver already exposes (OMI-12 P1 #2 carrying over)."""
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    script = _write_script(
        tmp_path,
        f"""
        import json, pathlib, sys
        out = pathlib.Path({str(out_dir)!r})
        (out / "result.json").write_text(
            json.dumps({{"skill": "stub", "status": "ok"}})
        )
        sys.exit(7)  # arbitrary non-zero
        """,
    )
    proc = asyncio.run(
        adrive_subprocess(
            [sys.executable, str(script)],
            cwd=tmp_path,
            env={},
            out_dir=out_dir,
        )
    )
    assert proc.returncode == 0


def test_adrive_subprocess_terminates_process_group_on_cancel(tmp_path: Path):
    """``asyncio.CancelledError`` must propagate AND the child's process
    group must die — including children that ignore SIGTERM (the
    canonical "skill forked workers" case)."""
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Ignore SIGTERM so the driver has to escalate to the grace-period
    # SIGKILL. Without process-group kill semantics this script would
    # leak.
    script = _write_script(
        tmp_path,
        """
        import signal, time
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        # Sleep long enough that any successful cancel must be due to a
        # forcible kill, not the script exiting on its own.
        for _ in range(60):
            time.sleep(0.5)
        """,
    )

    async def driver() -> tuple[bool, float]:
        cancelled = False
        started = time.time()
        task = asyncio.create_task(
            adrive_subprocess(
                [sys.executable, str(script)],
                cwd=tmp_path,
                env={},
                out_dir=out_dir,
            )
        )
        await asyncio.sleep(0.4)  # let the child install its SIGTERM handler
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True
        return cancelled, time.time() - started

    cancelled, elapsed = asyncio.run(driver())
    assert cancelled, "adrive_subprocess must re-raise CancelledError"
    # ``_CANCEL_GRACE_SECONDS`` is 5.0 — the child should be dead within
    # roughly that grace + a bit of slack. Without SIGKILL escalation the
    # script would sleep for 30s.
    assert elapsed < 10.0, (
        f"cancel did not kill the child within the grace period — "
        f"elapsed {elapsed:.1f}s (expected < 10s)"
    )


@pytest.mark.skipif(
    not governed_process_tree_supported(),
    reason="no cgroup-backed governed process-tree Adapter",
)
def test_strict_driver_kills_setsid_descendant_before_accepting_status_ok(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    escaped_marker = out_dir / "escaped-alive"
    reference_type, reference = new_governed_process_tree_reference()
    script = _write_script(
        tmp_path,
        f"""
        import json, os, pathlib, time
        child = os.fork()
        if child:
            pathlib.Path({str(out_dir / 'result.json')!r}).write_text(
                json.dumps({{"skill": "stub", "status": "ok"}})
            )
            os._exit(0)
        os.setsid()
        os.close(1)
        os.close(2)
        time.sleep(0.2)
        pathlib.Path({str(escaped_marker)!r}).write_text("escaped")
        time.sleep(30)
        """,
    )

    proc = asyncio.run(
        adrive_subprocess(
            [sys.executable, str(script)],
            cwd=tmp_path,
            env=os.environ.copy(),
            out_dir=out_dir,
            require_process_tree_proof=True,
            governed_execution_reference=reference,
        )
    )

    assert proc.returncode == 0
    assert not escaped_marker.exists()
    assert (
        asyncio.run(reconcile_governed_process_tree(reference_type, reference)) is False
    )


@pytest.mark.skipif(
    not governed_process_tree_supported(),
    reason="no cgroup-backed governed process-tree Adapter",
)
def test_strict_driver_blocks_nested_user_systemd_scope(tmp_path: Path) -> None:
    out_dir = tmp_path / "nested-out"
    out_dir.mkdir()
    nested_marker = out_dir / "nested-scope-alive"
    nested_result = out_dir / "nested-result.json"
    nested_scope = f"omicsclaw-nested-{secrets.token_hex(8)}"
    reference_type, reference = new_governed_process_tree_reference()
    script = _write_script(
        tmp_path,
        f"""
        import json, pathlib, subprocess, sys
        outcome = subprocess.run(
            [
                "systemd-run", "--user", "--scope", "--quiet",
                "--unit={nested_scope}", "--", sys.executable, "-c",
                "import pathlib, time; "
                "pathlib.Path({str(nested_marker)!r}).write_text('escaped'); "
                "time.sleep(30)",
            ],
            capture_output=True,
            timeout=5,
        )
        pathlib.Path({str(nested_result)!r}).write_text(
            json.dumps({{"returncode": outcome.returncode}})
        )
        pathlib.Path({str(out_dir / 'result.json')!r}).write_text(
            json.dumps({{"skill": "stub", "status": "ok"}})
        )
        """,
    )

    try:
        proc = asyncio.run(
            adrive_subprocess(
                [sys.executable, str(script)],
                cwd=tmp_path,
                env=os.environ.copy(),
                out_dir=out_dir,
                require_process_tree_proof=True,
                governed_execution_reference=reference,
            )
        )
        assert proc.returncode == 0
        assert json.loads(nested_result.read_text(encoding="utf-8"))["returncode"] != 0
        assert not nested_marker.exists()
        assert (
            asyncio.run(reconcile_governed_process_tree(reference_type, reference))
            is False
        )
    finally:
        subprocess.run(
            [
                "systemctl",
                "--user",
                "kill",
                "--kill-who=all",
                "--signal=SIGKILL",
                nested_scope + ".scope",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


@pytest.mark.skipif(
    not governed_process_tree_supported(),
    reason="no cgroup-backed governed process-tree Adapter",
)
def test_strict_driver_cancel_during_spawn_handle_window_cleans_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_dir = tmp_path / "spawn-window-out"
    out_dir.mkdir()
    started = out_dir / "spawned"
    reference_type, reference = new_governed_process_tree_reference()
    script = _write_script(
        tmp_path,
        f"""
        import pathlib, time
        pathlib.Path({str(started)!r}).write_text("started")
        time.sleep(30)
        """,
    )

    async def scenario() -> None:
        real_spawn = driver_module.asyncio.create_subprocess_exec
        handle_withheld = asyncio.Event()
        release_handle = asyncio.Event()
        intercepted = False

        async def delayed_handle(*args, **kwargs):
            nonlocal intercepted
            proc = await real_spawn(*args, **kwargs)
            if not intercepted and any(
                str(value).endswith("governed_launcher.py") for value in args
            ):
                intercepted = True
                handle_withheld.set()
                await release_handle.wait()
            return proc

        monkeypatch.setattr(
            driver_module.asyncio,
            "create_subprocess_exec",
            delayed_handle,
        )
        task = asyncio.create_task(
            adrive_subprocess(
                [sys.executable, str(script)],
                cwd=tmp_path,
                env=os.environ.copy(),
                out_dir=out_dir,
                require_process_tree_proof=True,
                governed_execution_reference=reference,
            )
        )
        await asyncio.wait_for(handle_withheld.wait(), timeout=3)
        for _ in range(100):
            if started.exists():
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release_handle.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        assert await reconcile_governed_process_tree(reference_type, reference) is False

    asyncio.run(scenario())


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux PR_SET_PDEATHSIG contract",
)
def test_governed_launcher_prevents_late_child_after_backend_death(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "launcher-ready"
    late_marker = tmp_path / "late-child"
    target = tmp_path / "launcher-target.py"
    target.write_text(
        textwrap.dedent(
            f"""
            import os, pathlib, time
            pathlib.Path({str(ready)!r}).write_text(str(os.getpid()))
            time.sleep(0.5)
            pathlib.Path({str(late_marker)!r}).write_text("late")
            time.sleep(30)
            """
        ),
        encoding="utf-8",
    )
    launcher = Path(driver_module.__file__).with_name("governed_launcher.py")
    parent = tmp_path / "launcher-parent.py"
    parent.write_text(
        textwrap.dedent(
            f"""
            import os, pathlib, signal, subprocess, sys, time
            subprocess.Popen([
                sys.executable,
                {str(launcher)!r},
                str(os.getpid()),
                "--",
                sys.executable,
                {str(target)!r},
            ])
            ready = pathlib.Path({str(ready)!r})
            for _ in range(200):
                if ready.exists():
                    break
                time.sleep(0.01)
            os.kill(os.getpid(), signal.SIGKILL)
            """
        ),
        encoding="utf-8",
    )

    wrapper = subprocess.Popen([sys.executable, str(parent)])
    wrapper.wait(timeout=5)
    assert wrapper.returncode == -signal.SIGKILL
    for _ in range(100):
        if ready.exists():
            break
        time.sleep(0.01)
    assert ready.exists()
    target_pid = int(ready.read_text(encoding="utf-8"))
    try:
        time.sleep(0.7)
        assert not late_marker.exists()
    finally:
        try:
            os.kill(target_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.parametrize(("populated", "expected_empty"), [("1", False), ("0", True)])
def test_terminal_systemd_state_requires_cgroup_occupancy_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    populated: str,
    expected_empty: bool,
) -> None:
    control_group = "user.slice/omicsclaw-test.scope"
    events = tmp_path / control_group / "cgroup.events"
    events.parent.mkdir(parents=True)
    events.write_text(f"populated {populated}\nfrozen 0\n", encoding="utf-8")

    async def failed_state(_unit):
        return "failed"

    async def observed_control_group(_unit, property_name):
        assert property_name == "ControlGroup"
        return "/" + control_group

    monkeypatch.setattr(driver_module, "_CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(driver_module, "_systemd_scope_state", failed_state)
    monkeypatch.setattr(
        driver_module,
        "_systemd_scope_property",
        observed_control_group,
    )

    assert (
        asyncio.run(
            driver_module._systemd_scope_empty("omicsclaw-run-" + "a" * 24 + ".scope")
        )
        is expected_empty
    )


def test_process_tree_helpers_scrub_backend_control_credentials(monkeypatch) -> None:
    class FakeHelper:
        returncode = 0

        async def communicate(self):
            return b"inactive\n", b""

        async def wait(self):
            return 0

        def kill(self):
            self.returncode = -9

    control_keys = {
        "OMICSCLAW_REMOTE_AUTH_TOKEN",
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN",
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD",
    }
    for key in control_keys:
        monkeypatch.setenv(key, "must-not-reach-process-tree-helper")
    monkeypatch.setenv("OMICSCLAW_PROCESS_TREE_TEST_KEEP", "ordinary-value")
    monkeypatch.setattr(
        driver_module.shutil,
        "which",
        lambda name: "/usr/bin/systemctl" if name == "systemctl" else None,
    )
    observed: list[dict[str, str]] = []

    async def fake_spawn(*_args, **kwargs):
        observed.append(kwargs["env"])
        return FakeHelper()

    monkeypatch.setattr(
        driver_module.asyncio,
        "create_subprocess_exec",
        fake_spawn,
    )
    unit = "omicsclaw-run-" + "a" * 24 + ".scope"

    async def scenario() -> None:
        assert await driver_module._systemd_scope_property(unit, "ActiveState") == (
            "inactive"
        )
        await driver_module._signal_systemd_scope(unit, signal_name="SIGTERM")
        assert await driver_module._taskkill_windows_tree(1234, force=True) == 0

    asyncio.run(scenario())

    assert len(observed) == 3
    for child_env in observed:
        assert child_env["OMICSCLAW_PROCESS_TREE_TEST_KEEP"] == "ordinary-value"
        assert not control_keys.intersection(child_env)


@pytest.mark.skipif(
    not governed_process_tree_supported(),
    reason="no cgroup-backed governed process-tree Adapter",
)
def test_strict_driver_repeated_cancel_waits_for_scope_to_be_empty(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    started = out_dir / "started"
    escaped_marker = out_dir / "escaped-after-cancel"
    reference_type, reference = new_governed_process_tree_reference()
    script = _write_script(
        tmp_path,
        f"""
        import os, pathlib, time
        child = os.fork()
        if child:
            pathlib.Path({str(started)!r}).write_text("started")
            time.sleep(30)
        else:
            os.setsid()
            os.close(1)
            os.close(2)
            time.sleep(0.5)
            pathlib.Path({str(escaped_marker)!r}).write_text("escaped")
            time.sleep(30)
        """,
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            adrive_subprocess(
                [sys.executable, str(script)],
                cwd=tmp_path,
                env=os.environ.copy(),
                out_dir=out_dir,
                require_process_tree_proof=True,
                governed_execution_reference=reference,
            )
        )
        for _ in range(100):
            if started.exists():
                break
            await asyncio.sleep(0.01)
        assert started.exists()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    time.sleep(0.6)
    assert not escaped_marker.exists()
    assert (
        asyncio.run(reconcile_governed_process_tree(reference_type, reference)) is False
    )
