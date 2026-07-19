"""Credential-boundary tests for the shared R subprocess runner."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from omicsclaw.core import r_script_runner as runner_module
from omicsclaw.core.r_script_runner import RScriptRunner


def test_r_runner_scrubs_base_override_and_probe_environments(monkeypatch, tmp_path):
    control_keys = {
        "OMICSCLAW_REMOTE_AUTH_TOKEN",
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN",
        "OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD",
    }
    for key in control_keys:
        monkeypatch.setenv(key, "must-not-reach-rscript")
    monkeypatch.setenv("OMICSCLAW_R_RUNNER_TEST_KEEP", "ordinary-value")
    monkeypatch.delenv("R_LIBS_USER", raising=False)

    rscript = tmp_path / "bin" / "Rscript"
    rscript.parent.mkdir()
    rscript.write_text("", encoding="utf-8")
    script = tmp_path / "analysis.R"
    script.write_text("", encoding="utf-8")
    observed: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(cmd, **kwargs):
        observed.append((list(cmd), kwargs["env"]))
        if "path.expand" in " ".join(str(part) for part in cmd):
            return SimpleNamespace(
                returncode=0,
                stdout=str(tmp_path / "r-user-library"),
                stderr="",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    runner = RScriptRunner(r_executable=str(rscript), verbose=False)

    built_env = runner._build_r_env()
    assert built_env["OMICSCLAW_R_RUNNER_TEST_KEEP"] == "ordinary-value"
    assert not control_keys.intersection(built_env)

    caller_env = {key: "caller-must-not-reinject" for key in control_keys}
    caller_env["OMICSCLAW_R_CALLER_TEST_KEEP"] = "caller-value"
    result = runner.run_script(script, env=caller_env)

    assert result.success is True
    assert len(observed) >= 3
    for _cmd, child_env in observed:
        assert not control_keys.intersection(child_env)
    main_env = observed[-1][1]
    assert main_env["OMICSCLAW_R_RUNNER_TEST_KEEP"] == "ordinary-value"
    assert main_env["OMICSCLAW_R_CALLER_TEST_KEEP"] == "caller-value"
