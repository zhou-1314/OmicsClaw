"""P5's environment half: the local supply commands and their probe.

The remote twin's pins (``tests/remote/test_kernel_env.py``), asserted
for the local variant — no network, no real interpreter beyond the
probes' fake runners and (for one smoke) this process's own python.
What these pin:

* the script's shape: uv located three ways with **no curl rung**
  (``OMICSCLAW_UV_BIN``, the installers dir, PATH), the same step
  order as the remote script (interpreter, venv, packages,
  verify-at-the-end), and one mirror prefix per download line;
* the pins are shared, not forked: the remote module's constants and
  the local package's answer the same objects;
* the probe's truth table — one subprocess, sentinel-guarded JSON,
  honest degradation on anything it cannot parse;
* the manual-command disclosure names every step a person would need.
"""

from __future__ import annotations

import json

from omicsclaw import envsupply
from omicsclaw.envsupply import (
    CORE_STACK_PACKAGES,
    DEFAULT_ENV_DIR,
    DEFAULT_UV_INDEX_URL,
    KERNEL_BASE_PACKAGES,
    PROBE_SENTINEL,
    build_env_install_script,
    build_probe_argv,
    env_step_line,
    interpreter_in_env,
    manual_env_commands,
    parse_env_step,
    probe_interpreter,
)
from omicsclaw.remote import kernel as remote_kernel

# ---- the pins are shared, not forked -------------------------------------------


def test_the_remote_and_local_planes_share_the_pins():
    assert remote_kernel.CORE_STACK_PACKAGES is CORE_STACK_PACKAGES
    assert remote_kernel.KERNEL_BASE_PACKAGES is KERNEL_BASE_PACKAGES
    assert remote_kernel.REMOTE_PYTHON_VERSION == envsupply.PYTHON_VERSION
    assert remote_kernel.UV_INSTALL_URL == envsupply.UV_INSTALL_URL


def test_the_default_env_dir_is_the_tier_one_directory():
    assert DEFAULT_ENV_DIR == "~/.omicsclaw/envs/main"
    assert interpreter_in_env(DEFAULT_ENV_DIR).endswith("/bin/python")
    assert interpreter_in_env("C:/envs/main", platform="win32").endswith(
        "\\Scripts\\python.exe"
    )


# ---- the script builder ---------------------------------------------------------


def test_the_script_locates_uv_three_ways_and_never_curls():
    script = build_env_install_script(DEFAULT_ENV_DIR)
    ladder_start = script.index('UV="${OMICSCLAW_UV_BIN:-}"')
    installers_rung = script.index("OMICSCLAW_UV_INSTALLERS_DIR")
    path_rung = script.index("command -v uv")
    assert ladder_start < installers_rung < path_rung
    # the local contract: no network self-repair — a machine without uv
    # is reported with the shell's own not-found code, never curled at
    assert "curl" not in script
    assert "exit 127" in script


def test_the_script_follows_the_remote_step_order_and_verifies_at_the_end():
    script = build_env_install_script(DEFAULT_ENV_DIR)
    steps = [parse_env_step(line) for line in script.splitlines()]
    steps = [name for name in steps if name]
    assert steps == ["python", "venv", "packages", "verify"]
    assert script.index('"$UV" python install') < script.index('"$UV" venv')
    assert script.index('"$UV" venv') < script.index('"$UV" pip install')
    lines = script.splitlines()
    assert lines[-1].endswith("-c 'import ipykernel'")
    assert DEFAULT_ENV_DIR.replace("~", '"$HOME"') in lines[-1]


def test_the_idempotent_no_ops_are_the_same_commands_a_retry_sends():
    # pure builder: same arguments, same script — the retry heals rather
    # than diverges, because every uv step is a no-op over existing output
    assert build_env_install_script(DEFAULT_ENV_DIR) == build_env_install_script(
        DEFAULT_ENV_DIR
    )


def test_the_stack_and_extras_ride_one_quoted_pip_line():
    script = build_env_install_script(DEFAULT_ENV_DIR, packages=("squidpy",))
    pip_line = next(line for line in script.splitlines() if '"$UV" pip install' in line)
    for package in (*KERNEL_BASE_PACKAGES, *CORE_STACK_PACKAGES, "squidpy"):
        assert package in pip_line
    # bare names need no quotes, but a dotted name still lands whole
    assert "squidpy" in pip_line and pip_line.count("pip install") == 1


def test_the_mirrors_prefix_their_own_download_lines_once():
    script = build_env_install_script(DEFAULT_ENV_DIR)
    assert script.count("UV_INDEX_URL=") == 1
    assert script.count("UV_PYTHON_INSTALL_MIRROR=") == 1
    python_line = next(
        line for line in script.splitlines() if '"$UV" python install' in line
    )
    pip_line = next(line for line in script.splitlines() if '"$UV" pip install' in line)
    assert "UV_PYTHON_INSTALL_MIRROR=" in python_line
    assert "UV_INDEX_URL=" in pip_line
    assert DEFAULT_UV_INDEX_URL in pip_line


def test_the_mirrors_are_overridable_and_droppable():
    script = build_env_install_script(
        DEFAULT_ENV_DIR,
        uv_index_url="https://pypi.example/simple",
        uv_python_mirror="https://py.example/download",
    )
    assert "https://pypi.example/simple" in script
    assert "https://py.example/download" in script
    assert DEFAULT_UV_INDEX_URL not in script
    bare = build_env_install_script(
        DEFAULT_ENV_DIR, uv_index_url=None, uv_python_mirror=None
    )
    assert "UV_INDEX_URL=" not in bare
    assert "UV_PYTHON_INSTALL_MIRROR=" not in bare


def test_the_env_dir_tilde_is_home_expanded_not_literal():
    script = build_env_install_script(DEFAULT_ENV_DIR)
    assert '"$HOME"/.omicsclaw/envs/main' in script
    assert "'~/.omicsclaw" not in script


def test_step_markers_round_trip_and_reject_lookalikes():
    assert parse_env_step(env_step_line("packages") + "\n") == "packages"
    assert parse_env_step("===OMICSCLAW_ENV_STEP:packages=== partial") is None
    assert parse_env_step("Resolved 42 packages") is None
    assert parse_env_step("") is None


def test_manual_commands_mirror_the_script_steps():
    commands = manual_env_commands(DEFAULT_ENV_DIR)
    joined = "\n".join(commands)
    assert "uv python install" in joined
    assert "uv venv" in joined
    assert "uv pip install" in joined
    assert "ipykernel" in joined and "scanpy" in joined
    assert DEFAULT_UV_INDEX_URL in joined  # the mirrors travel with them


def test_manual_commands_are_runnable_as_pasted():
    # review m5: the mirrors ride as VAR=value prefixes on the exact
    # commands the script would run — pasting the card hits the same
    # mirrors, not the official CDN behind a comment
    python_line, venv_line, pip_line, verify = manual_env_commands(DEFAULT_ENV_DIR)
    assert (
        python_line.startswith("UV_PYTHON_INSTALL_MIRROR=")
        and "uv python install" in python_line
    )
    assert venv_line.startswith("uv venv")
    assert pip_line.startswith("UV_INDEX_URL=") and "uv pip install" in pip_line
    assert "# with UV_INDEX_URL" not in pip_line
    assert verify.endswith("-c 'import ipykernel'")
    bare = manual_env_commands(
        DEFAULT_ENV_DIR, uv_index_url=None, uv_python_mirror=None
    )
    assert bare[0].startswith("uv python install")
    assert bare[2].startswith("uv pip install")


def test_a_stalled_install_expires_at_its_deadline():
    # review m4: silence must cost the same as output — a network stall
    # that prints nothing still dies at the deadline, disclosing itself
    from omicsclaw.envsupply.script import (
        INSTALL_TIMEOUT_EXIT,
        default_install_runner,
    )

    seen: list[str] = []
    code = default_install_runner(
        ["sh", "-c", "echo one; sleep 30"],
        on_line=seen.append,
        timeout=0.5,
    )
    assert code == INSTALL_TIMEOUT_EXIT
    assert "one" in seen
    assert any("deadline" in line for line in seen)


def test_a_fast_install_streams_and_answers_zero():
    from omicsclaw.envsupply.script import default_install_runner

    seen: list[str] = []
    code = default_install_runner(
        ["sh", "-c", "echo step-a; echo step-b"],
        on_line=seen.append,
        timeout=10.0,
    )
    assert code == 0
    assert seen == ["step-a", "step-b"]


# ---- the probe ------------------------------------------------------------------


class _FakeProbeRunner:
    """Answers argv with a canned outcome; records what was asked."""

    def __init__(
        self, stdout: str = "", exit_code: int = 0, started: bool = True
    ) -> None:
        from omicsclaw.envsupply import ProbeOutcome

        self._outcome = ProbeOutcome(exit_code, stdout, started=started)
        self.argvs: list[list[str]] = []

    def __call__(self, argv):
        self.argvs.append(list(argv))
        return self._outcome


def _probe_stdout(found: dict[str, bool]) -> str:
    return (
        "some startup warning on stderr-ish line\n"
        + PROBE_SENTINEL
        + "\n"
        + json.dumps(found)
        + "\n"
    )


def test_a_ready_interpreter_is_one_subprocess_and_reports_no_gaps():
    runner = _FakeProbeRunner(stdout=_probe_stdout({"ipykernel": True, "scanpy": True}))
    probe = probe_interpreter(
        "~/.omicsclaw/envs/main/bin/python",
        packages=("ipykernel", "scanpy"),
        runner=runner,
    )
    assert len(runner.argvs) == 1
    assert probe.exists and probe.ready
    assert probe.missing == ()
    # the probe source runs under -c with the names as argv, never in a
    # shell — an interpreter path with spaces is one argv element
    assert runner.argvs[0][0] == "~/.omicsclaw/envs/main/bin/python"
    assert runner.argvs[0][1] == "-c"
    assert runner.argvs[0][3:]  # the names follow the -c source


def test_a_gap_report_lists_only_the_missing_in_order():
    runner = _FakeProbeRunner(
        stdout=_probe_stdout(
            {"ipykernel": True, "jupyter_client": False, "scanpy": False}
        )
    )
    probe = probe_interpreter(
        "/opt/bin/python",
        packages=("ipykernel", "jupyter_client", "scanpy"),
        runner=runner,
    )
    assert probe.ready is True
    assert probe.missing == ("jupyter_client", "scanpy")


def test_a_missing_ipykernel_is_not_ready_even_with_the_stack():
    runner = _FakeProbeRunner(
        stdout=_probe_stdout({"ipykernel": False, "scanpy": True})
    )
    probe = probe_interpreter("/opt/bin/python", runner=runner)
    assert probe.exists and not probe.ready
    assert "ipykernel" in probe.missing


def test_an_interpreter_that_cannot_start_reports_absent():
    runner = _FakeProbeRunner(started=False, exit_code=127)
    probe = probe_interpreter("/no/such/python", runner=runner)
    assert probe.exists is False
    assert probe.ready is False
    assert probe.missing  # everything it was asked about


def test_garbage_output_degrades_to_cannot_vouch():
    runner = _FakeProbeRunner(stdout="Segmentation fault", exit_code=139)
    probe = probe_interpreter("/opt/bin/python", runner=runner)
    assert probe.exists is True  # it ran; it just answered nothing usable
    assert probe.ready is False
    assert probe.missing


def test_the_probe_argv_carries_the_names_not_a_shell_line():
    argv = build_probe_argv("/opt/bin/python", ("ipykernel", "scanpy"))
    assert argv[0] == "/opt/bin/python"
    assert argv[1] == "-c"
    assert argv[3:] == ["ipykernel", "scanpy"]
    source = argv[2]
    assert ";" not in source and "&&" not in source and "os.system" not in source


def test_a_real_interpreter_answers_the_structured_probe():
    # one smoke over this process's own interpreter: the sentinel JSON
    # contract is what the desktop backend will actually consume
    import sys

    probe = probe_interpreter(sys.executable, packages=("ipykernel", "json"))
    assert probe.exists is True
    assert "json" not in probe.missing


def test_install_timeout_stops_its_owned_descendant_before_late_write(tmp_path):
    import shlex
    import sys
    import time

    from omicsclaw.envsupply.script import INSTALL_TIMEOUT_EXIT, default_install_runner

    marker = tmp_path / "late-owned-write"
    source = (
        "import time;from pathlib import Path;time.sleep(.4);Path("
        + repr(str(marker))
        + ").write_text('late')"
    )
    script = shlex.quote(sys.executable) + " -c " + shlex.quote(source) + " & wait"
    code = default_install_runner(["sh", "-c", script], timeout=0.1)
    time.sleep(0.6)
    assert code == INSTALL_TIMEOUT_EXIT and not marker.exists()


def test_install_output_is_bounded_even_without_newlines():
    import sys

    from omicsclaw.envsupply.script import default_install_runner

    widths = []
    code = default_install_runner(
        [sys.executable, "-c", "import os;os.write(1,b'x'*(4*1024*1024+1))"],
        on_line=lambda line: widths.append(len(line.encode())),
        timeout=5,
    )
    assert code == 125 and max(widths, default=0) <= 65536


def test_install_utf8_character_split_at_fragment_boundary_remains_intact():
    import sys

    from omicsclaw.envsupply.script import default_install_runner

    expected = "x" * 16383 + "汉🙂tail"
    seen = []
    code = default_install_runner(
        [
            sys.executable,
            "-c",
            "import os;os.write(1," + repr((expected + "\n").encode()) + ")",
        ],
        on_line=seen.append,
    )
    assert code == 0 and "".join(seen) == expected


def test_install_callback_failure_stops_the_owned_session(tmp_path):
    import shlex
    import sys
    import time

    import pytest

    from omicsclaw.envsupply.script import default_install_runner

    marker = tmp_path / "callback-late-write"
    source = (
        "import time;from pathlib import Path;time.sleep(.4);Path("
        + repr(str(marker))
        + ").write_text('late')"
    )
    script = (
        shlex.quote(sys.executable)
        + " -c "
        + shlex.quote(source)
        + " & echo ready; wait"
    )

    def broken(_):
        raise ValueError("owned callback failure")

    with pytest.raises(ValueError, match="owned callback failure"):
        default_install_runner(["sh", "-c", script], on_line=broken)
    time.sleep(0.6)
    assert not marker.exists()
