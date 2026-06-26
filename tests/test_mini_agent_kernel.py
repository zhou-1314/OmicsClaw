"""Integration tests for the ADR 0032 persistent kernel session.

These actually launch a kernel (sandboxed via bubblewrap when available, else
un-isolated), so they are a few seconds each.
"""

from __future__ import annotations

from pathlib import Path
import queue

import pytest

from omicsclaw.autonomous.kernel_envelope import envelope_available
from omicsclaw.autonomous.kernel_session import (
    REPO_ROOT,
    CellResult,
    KernelSession,
    kernel_ipc_available,
)

SANDBOX = envelope_available()
IPC_AVAILABLE = kernel_ipc_available()


@pytest.fixture()
def session(tmp_path: Path):
    if not IPC_AVAILABLE:
        pytest.skip("ZMQ IPC sockets are unavailable in this test sandbox")
    ks = KernelSession(workspace_root=tmp_path, sandbox=SANDBOX, startup_timeout=60)
    ks.start()
    try:
        yield ks
    finally:
        ks.shutdown()


def test_state_persists_across_cells(session: KernelSession):
    r1 = session.execute("a = 41\nprint('set')", timeout=30)
    assert r1.ok and "set" in r1.stdout
    r2 = session.execute("print(a + 1)", timeout=30)
    assert r2.ok
    assert r2.stdout.strip() == "42"


def test_error_is_captured(session: KernelSession):
    r = session.execute("1 / 0", timeout=30)
    assert r.ok is False
    assert r.ename == "ZeroDivisionError"
    assert "ZeroDivisionError" in r.error_summary


def test_introspect_reports_shape(session: KernelSession):
    session.execute("import numpy as np\narr = np.zeros((3, 4))\nlabel = 'hi'", timeout=60)
    variables = session.introspect()
    assert "arr" in variables
    assert variables["arr"]["shape"] == "(3, 4)"
    assert variables["label"]["type"] == "str"


def test_timeout_is_reported(session: KernelSession):
    r = session.execute("while True:\n    pass", timeout=3)
    assert isinstance(r, CellResult)
    assert r.timed_out is True
    assert r.ok is False


def test_timeout_marks_session_unusable_and_terminates(tmp_path: Path):
    class _Client:
        def execute(self, _code, store_history=True):
            return "msg-1"

        def get_iopub_msg(self, timeout):
            raise queue.Empty

        def stop_channels(self):
            pass

    class _Proc:
        terminated = False

        def poll(self):
            return None

        def send_signal(self, _signal):
            pass

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            pass

    proc = _Proc()
    ks = KernelSession(workspace_root=tmp_path, sandbox=False)
    ks._client = _Client()
    ks._proc = proc
    ks._alive = True
    restarted = False

    def _fake_start():
        nonlocal restarted
        restarted = True
        raise RuntimeError("restart failed in fake test")

    ks.start = _fake_start  # type: ignore[method-assign]

    result = ks.execute("while True:\n    pass", timeout=0.01)

    assert result.timed_out is True
    assert restarted is True
    assert ks.alive is False
    assert proc.terminated is True


def test_cleanup_scratch_removes_conn_and_home_dirs(tmp_path: Path):
    """Shutdown, restart, AND a failed start must remove the ipc dir and the
    throwaway HOME so a never-ready kernel leaks nothing under /tmp."""
    ks = KernelSession(workspace_root=tmp_path, sandbox=False)
    conn = tmp_path / "conn"
    conn.mkdir()
    (conn / "kernel.json").write_text("{}")
    home = tmp_path / "home"
    (home / ".cache").mkdir(parents=True)
    ks._conn_dir = conn
    ks._conn_file = conn / "kernel.json"
    ks._home_dir = home

    ks._cleanup_scratch()

    assert not conn.exists()
    assert not home.exists()
    assert ks._conn_dir is None and ks._home_dir is None and ks._conn_file is None


def test_returnanswer_creates_missing_output_root(tmp_path: Path):
    """A re-run of analysis.py whose output root (e.g. a lazy rerun/ sibling) does
    not exist yet must still write the answer — ReturnAnswer mkdirs its parent."""
    if not IPC_AVAILABLE:
        pytest.skip("ZMQ IPC sockets are unavailable in this test sandbox")
    from omicsclaw.autonomous.budget import MiniAgentBudget
    from omicsclaw.autonomous.mini_agent import ANSWER_FILE, build_init_code

    out_root = tmp_path / "rerun"  # deliberately NOT pre-created
    ks = KernelSession(workspace_root=tmp_path, sandbox=SANDBOX, startup_timeout=60)
    ks.start()
    try:
        assert ks.execute(build_init_code(out_root, [], MiniAgentBudget()), timeout=60).ok
        # ReturnAnswer only — no show()/oc.run() to lazily create the dir first.
        assert ks.execute("ReturnAnswer('ok')", timeout=30).ok
    finally:
        ks.shutdown()
    assert (out_root / ANSWER_FILE).read_text(encoding="utf-8").strip() == "ok"


@pytest.mark.skipif(not SANDBOX, reason="bubblewrap not available; cannot assert network isolation")
def test_sandbox_blocks_network(session: KernelSession):
    code = (
        "import socket\n"
        "try:\n"
        "    s = socket.socket(); s.settimeout(3); s.connect(('1.1.1.1', 80)); print('OPEN')\n"
        "except Exception as e:\n"
        "    print('BLOCKED', type(e).__name__)\n"
    )
    r = session.execute(code, timeout=30)
    assert "BLOCKED" in r.stdout


@pytest.mark.skipif(not SANDBOX, reason="bubblewrap not available; cannot assert fs isolation")
def test_sandbox_cannot_write_into_readonly_host_path(session: KernelSession):
    # repo_root is mounted read-only; a write into a real host path must fail.
    # (Writes to the sandbox's ephemeral tmpfs root like /etc succeed but never
    # touch the host, so we probe a path that is genuinely bind-mounted ro.)
    probe = REPO_ROOT / "_oc_sandbox_escape_probe.tmp"
    r = session.execute(
        f"try:\n"
        f"    open({str(probe)!r}, 'w').write('x'); print('WROTE')\n"
        f"except Exception as e:\n"
        f"    print('BLOCKED', type(e).__name__)\n",
        timeout=30,
    )
    assert "BLOCKED" in r.stdout
    # Defensive: the host file must not exist regardless of the sandbox result.
    assert not probe.exists()


def test_in_kernel_guard_blocks_network_and_destructive_os(tmp_path: Path):
    """Non-bwrap tier: the in-kernel guard reliably blocks network + destructive os ops."""
    if not IPC_AVAILABLE:
        pytest.skip("ZMQ IPC sockets are unavailable in this test sandbox")
    from omicsclaw.autonomous.runtime_guard import build_kernel_guard_code

    ks = KernelSession(workspace_root=tmp_path, sandbox=False, startup_timeout=60)
    ks.start()
    try:
        assert ks.execute(build_kernel_guard_code(workspace_root=tmp_path), timeout=30).ok
        net = ks.execute(
            "import socket\n"
            "try:\n"
            "    socket.socket(); print('OPEN')\n"
            "except Exception as e:\n"
            "    print('BLOCKED', type(e).__name__)\n",
            timeout=30,
        )
        assert "BLOCKED" in net.stdout
        rm = ks.execute(
            "import os\n"
            "try:\n"
            "    os.remove('/tmp/oc_guard_no_such_file'); print('REMOVED')\n"
            "except RuntimeError:\n"
            "    print('BLOCKED')\n"
            "except Exception as e:\n"
            "    print('OTHER', type(e).__name__)\n",
            timeout=30,
        )
        assert "BLOCKED" in rm.stdout
    finally:
        ks.shutdown()
