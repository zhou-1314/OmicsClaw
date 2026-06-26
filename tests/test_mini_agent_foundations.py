"""Unit tests for the ADR 0032 mini-agent foundation modules.

Covers the dependency-free building blocks: the per-step response protocol,
the budget/ledger accounting, and the bubblewrap kernel safety envelope argv.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omicsclaw.autonomous.budget import (
    BudgetLedger,
    MiniAgentBudget,
    MiniAgentStep,
    SkillCallTrace,
    TerminationReason,
)
from omicsclaw.autonomous.kernel_envelope import (
    EnvelopeConfig,
    build_bwrap_argv,
    build_launch_env,
    envelope_available,
    scrub_env,
)
from omicsclaw.autonomous.protocol import (
    TurnFormatError,
    code_calls_return_answer,
    extract_return_answer_literal,
    parse_turn,
    strip_thinking,
)
from omicsclaw.autonomous.validation import validate_generated_code


# --------------------------------------------------------------------------- #
# validation (safety lint)
# --------------------------------------------------------------------------- #


def test_validate_generated_code_allows_facade_run_call():
    """Regression (2026-06-24): the mini-agent safety lint must NOT block
    ``oc.run(...)`` — the documented public API of the injected skill facade
    (``skill_facade.run``). ``build_system_prompt`` tells the model to call exactly
    ``oc.run('skill', adata, ...)``; while the bare attr-name ``run`` sat in
    ``_BLOCKED_PYTHON_ATTRS`` every skill-driven step failed the lint, so the
    mini-agent could never compose a skill once its data finally loaded."""
    assert validate_generated_code("res = oc.run('spatial-preprocess', adata)") == []
    assert (
        validate_generated_code("adata = oc.run('sc-de', adata, method='wilcoxon').adata")
        == []
    )


def test_validate_generated_code_still_blocks_destructive_and_network():
    """The owner-aware guard keeps the dangerous forms blocked: ``os`` is an allowed
    import so its destructive ops stay blocked, ``Path('x').unlink()`` is a non-name
    (``object``) owner, and network/process modules stay blocked by import."""
    for snippet in (
        "import os\nos.system('rm -rf /')",
        "import os\nos.remove('/data/x')",
        "import os\nos.rename('a', 'b')",
        "from pathlib import Path\nPath('x').unlink()",
        "import subprocess",
        "import requests",
        "import socket",
    ):
        assert validate_generated_code(snippet), f"must stay blocked: {snippet!r}"


def test_validate_generated_code_allows_data_methods_on_variables():
    """Owner-aware lint (Codex finding 4): destructive-NAMED methods on plain user
    variables are not filesystem ops and must pass — these are everyday QC/cleaning
    calls that the bare-attr-name ban used to reject."""
    for snippet in (
        "df2 = df.rename(columns={'a': 'b'})",
        "df2 = df.replace(0, 1)",
        "adata.obs = adata.obs.rename(columns={'x': 'y'})",
        "res = oc.run('spatial-preprocess', adata)",
        "items.remove(0)",
        "layer.call(x)",  # .call/.run on a user variable (non-risky root) is fine
        "pipeline.run(data)",
    ):
        assert validate_generated_code(snippet) == [], f"should pass: {snippet!r}"


def test_validate_generated_code_blocks_dynamic_import_subprocess_bypass():
    """Codex finding 3 regression + re-review hardening: keeping ``run`` owner-aware
    must not reopen a shell. The dynamic-import / FFI / file-exec modules are
    import-blocked, and ALL subprocess execution methods (run/Popen/call/
    check_call/check_output) stay blocked on a risky root — whether reached
    directly or via ``sys.modules[...]``."""
    for snippet in (
        "import importlib",
        "from importlib import import_module",
        "import runpy",
        "import ctypes",
        "subprocess.run(['x'])",
        "subprocess.Popen(['x'])",
        "sys.modules['subprocess'].run(['x'])",
        "sys.modules['subprocess'].Popen(['x'])",
        "sys.modules['subprocess'].check_output(['x'])",
        "import os\nos.popen('x')",
    ):
        assert validate_generated_code(snippet), f"must stay blocked: {snippet!r}"


# --------------------------------------------------------------------------- #
# protocol
# --------------------------------------------------------------------------- #

VALID_TURN = """
**Purpose**: Load the data and inspect QC.
**Reasoning**: Need cell/gene counts before filtering.
**Next Goal**: Decide min_genes from the distribution.
**Code**:
```python
import scanpy as sc
print(adata.shape)
```
"""


def test_parse_turn_happy_path():
    turn = parse_turn(VALID_TURN)
    assert turn.purpose.startswith("Load the data")
    assert turn.reasoning.startswith("Need cell")
    assert turn.next_goal.startswith("Decide min_genes")
    assert "import scanpy as sc" in turn.code
    assert turn.calls_return_answer is False


def test_parse_turn_strips_thinking():
    text = "<think>secret chain of thought</think>\n" + VALID_TURN
    turn = parse_turn(text)
    assert "secret chain of thought" not in turn.raw
    assert turn.purpose


@pytest.mark.parametrize(
    "missing_block, expected_token",
    [
        ("**Purpose**: only purpose here", "Reasoning"),
        ("", "Purpose"),
    ],
)
def test_parse_turn_reports_missing_sections(missing_block, expected_token):
    with pytest.raises(TurnFormatError) as exc:
        parse_turn(missing_block)
    assert any(expected_token in problem for problem in exc.value.problems)


def test_parse_turn_rejects_syntax_error():
    bad = VALID_TURN.replace("print(adata.shape)", "print(adata.shape")  # unbalanced
    with pytest.raises(TurnFormatError) as exc:
        parse_turn(bad)
    assert any("syntax error" in p for p in exc.value.problems)


def test_return_answer_detection_and_literal():
    code = 'x = 1\nReturnAnswer("Found 5 niches across the slide.")'
    assert code_calls_return_answer(code) is True
    assert extract_return_answer_literal(code) == "Found 5 niches across the slide."


def test_return_answer_computed_arg_has_no_literal():
    code = "ReturnAnswer(f'Found {n} niches')"
    assert code_calls_return_answer(code) is True
    # f-strings are not plain constants -> must be read from the kernel sentinel.
    assert extract_return_answer_literal(code) is None


def test_return_answer_attribute_form():
    assert code_calls_return_answer("oc.ReturnAnswer('done')") is True


def test_strip_thinking_is_idempotent_without_tags():
    assert strip_thinking("plain text") == "plain text"


# --------------------------------------------------------------------------- #
# budget
# --------------------------------------------------------------------------- #


def test_budget_defaults_match_adr():
    b = MiniAgentBudget()
    assert b.max_steps == 8
    assert b.max_consecutive_failures == 3
    assert b.raw_cell_timeout_seconds == 120
    assert b.skill_call_timeout_seconds == 1800


def test_budget_clamp_enforces_step_ceiling():
    b = MiniAgentBudget(max_steps=999).clamped()
    assert b.max_steps == MiniAgentBudget.STEP_CEILING == 15
    assert MiniAgentBudget(max_steps=0).clamped().max_steps == 1


def test_budget_with_overrides_reclamps():
    b = MiniAgentBudget().with_overrides(max_steps=12, max_skill_calls=5)
    assert b.max_steps == 12
    assert b.max_skill_calls == 5


def test_ledger_consecutive_failures_reset_on_accept():
    ledger = BudgetLedger(budget=MiniAgentBudget())
    ledger.record_step(accepted=False)
    ledger.record_step(accepted=False)
    assert ledger.consecutive_failures == 2
    ledger.record_step(accepted=True)
    assert ledger.consecutive_failures == 0


def test_ledger_exhaustion_reasons():
    ledger = BudgetLedger(budget=MiniAgentBudget(max_steps=2, max_consecutive_failures=2))
    assert ledger.exhausted_reason(elapsed_seconds=0) is None
    ledger.record_step(accepted=True)
    ledger.record_step(accepted=True)
    assert ledger.exhausted_reason(elapsed_seconds=0) is TerminationReason.STEP_BUDGET

    failing = BudgetLedger(budget=MiniAgentBudget(max_steps=10, max_consecutive_failures=2))
    failing.record_step(accepted=False)
    failing.record_step(accepted=False)
    assert failing.exhausted_reason(elapsed_seconds=0) is TerminationReason.CONSECUTIVE_FAILURES


def test_ledger_wall_clock_and_tokens():
    ledger = BudgetLedger(budget=MiniAgentBudget(wall_clock_seconds=60, max_total_tokens=100))
    assert ledger.exhausted_reason(elapsed_seconds=120) is TerminationReason.WALL_CLOCK
    ledger.tokens_used = 100
    assert ledger.exhausted_reason(elapsed_seconds=0) is TerminationReason.TOKEN_BUDGET


def test_step_and_skillcall_serialise():
    step = MiniAgentStep(index=0, purpose="p", code="x=1", accepted=True)
    step.skill_calls.append(
        SkillCallTrace(
            skill="spatial-preprocess",
            params={"method": "scanpy"},
            input_artifact="in.h5ad",
            output_dir="out",
            primary_artifact="out/processed.h5ad",
            status="succeeded",
        )
    )
    payload = step.to_dict()
    assert payload["accepted"] is True
    assert payload["skill_calls"][0]["skill"] == "spatial-preprocess"


# --------------------------------------------------------------------------- #
# kernel envelope
# --------------------------------------------------------------------------- #


def test_envelope_available_returns_bool():
    assert isinstance(envelope_available(), bool)


def test_scrub_env_drops_secrets_keeps_runtime():
    scrubbed = scrub_env(
        {
            "PATH": "/usr/bin",
            "OPENAI_API_KEY": "sk-secret",
            "LLM_API_KEY": "secret",
            "HF_TOKEN": "hf_secret",
            "AWS_SECRET_ACCESS_KEY": "x",
            "LANG": "en_US.UTF-8",
        },
        workspace_root=Path("/tmp/ws"),
    )
    assert scrubbed["PATH"] == "/usr/bin"
    assert scrubbed["LANG"] == "en_US.UTF-8"
    assert scrubbed["PYTHONNOUSERSITE"] == "1"
    assert scrubbed["HOME"] == "/tmp/ws"
    for leaked in ("OPENAI_API_KEY", "LLM_API_KEY", "HF_TOKEN", "AWS_SECRET_ACCESS_KEY"):
        assert leaked not in scrubbed


def test_build_bwrap_argv_isolates_network_and_filesystem(tmp_path: Path):
    workspace = tmp_path / "ws"
    ipc = tmp_path / "ipc"
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    for d in (workspace, ipc, repo, data):
        d.mkdir()
    input_file = data / "sample.h5ad"
    input_file.write_text("x")

    config = EnvelopeConfig(
        workspace_root=workspace,
        ipc_dir=ipc,
        repo_root=repo,
        read_roots=[input_file],
    )
    argv = build_bwrap_argv(config, ["python", "-m", "ipykernel_launcher", "-f", "c.json"])

    assert argv[0] == "bwrap"
    assert "--unshare-net" in argv
    # env is controlled by the launcher (Popen env=build_launch_env), not argv,
    # so bubblewrap 0.4.0 (no --clearenv) works and values stay out of `ps`.
    assert "--clearenv" not in argv
    assert "--setenv" not in argv
    # workspace is writable; the input is read-only.
    assert _has_pair(argv, "--bind", str(workspace.resolve()))
    assert _has_pair(argv, "--ro-bind", str(input_file.resolve()))
    # inner command sits after the terminating '--'.
    sep = argv.index("--")
    assert argv[sep + 1 :] == ["python", "-m", "ipykernel_launcher", "-f", "c.json"]


def test_build_launch_env_is_secret_free(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("PATH", "/usr/bin")
    config = EnvelopeConfig(
        workspace_root=tmp_path / "ws",
        ipc_dir=tmp_path / "ipc",
        repo_root=tmp_path / "repo",
        extra_env={"OMICSCLAW_RUN_PYTHON": "/x/py", "SNEAKY_TOKEN": "leak"},
    )
    env = build_launch_env(config)
    assert env["PATH"] == "/usr/bin"
    assert env["OMICSCLAW_RUN_PYTHON"] == "/x/py"  # benign extra merged
    assert "OPENAI_API_KEY" not in env  # host secret dropped
    assert "SNEAKY_TOKEN" not in env  # secret-shaped extra rejected


def test_build_bwrap_argv_can_allow_network(tmp_path: Path):
    workspace = tmp_path / "ws"
    ipc = tmp_path / "ipc"
    repo = tmp_path / "repo"
    for d in (workspace, ipc, repo):
        d.mkdir()
    config = EnvelopeConfig(
        workspace_root=workspace, ipc_dir=ipc, repo_root=repo, allow_network=True
    )
    argv = build_bwrap_argv(config, ["true"])
    assert "--unshare-net" not in argv


def test_build_bwrap_argv_does_not_double_bind_ipc_inside_workspace(tmp_path: Path):
    workspace = tmp_path / "ws"
    ipc = workspace / "ipc"
    repo = tmp_path / "repo"
    ipc.mkdir(parents=True)
    repo.mkdir()
    config = EnvelopeConfig(workspace_root=workspace, ipc_dir=ipc, repo_root=repo)
    argv = build_bwrap_argv(config, ["true"])

    assert _count_pair(argv, "--bind", str(workspace.resolve())) == 1
    assert not _has_pair(argv, "--bind", str(ipc.resolve()))


def test_kernel_scratch_home_is_a_tmpfs_dir_not_a_host_bind(tmp_path: Path):
    """The kernel's scratch HOME is created in the sandbox tmpfs via --dir and
    pointed at by $HOME — never a host writable bind (Q1: deliverable-only run)."""
    home = Path("/tmp/oc-kernel-home")
    config = EnvelopeConfig(
        workspace_root=tmp_path / "ws",
        ipc_dir=tmp_path / "ipc",
        repo_root=tmp_path / "repo",
        home_dir=home,
    )
    argv = build_bwrap_argv(config, ["true"])
    # --dir is a single-value bwrap flag that creates an empty dir in the tmpfs.
    assert any(
        argv[i] == "--dir" and argv[i + 1] == str(home) for i in range(len(argv) - 1)
    )
    assert not _has_pair(argv, "--bind", str(home))  # NOT a host writable bind
    assert build_launch_env(config)["HOME"] == str(home)  # kernel HOME points there


def test_scrub_env_home_points_at_scratch_home_when_given():
    scrubbed = scrub_env(
        {"PATH": "/usr/bin"},
        workspace_root=Path("/tmp/ws"),
        home_dir=Path("/tmp/oc-kernel-home"),
    )
    assert scrubbed["HOME"] == "/tmp/oc-kernel-home"  # scratch home wins over workspace


def _has_pair(argv: list[str], flag: str, value: str) -> bool:
    for i, tok in enumerate(argv):
        if tok == flag and i + 2 < len(argv) and argv[i + 1] == value and argv[i + 2] == value:
            return True
    return False


def _count_pair(argv: list[str], flag: str, value: str) -> int:
    count = 0
    for i, tok in enumerate(argv):
        if tok == flag and i + 2 < len(argv) and argv[i + 1] == value and argv[i + 2] == value:
            count += 1
    return count
