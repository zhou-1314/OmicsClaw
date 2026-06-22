"""Replay validation gate for the Autonomous Code Mini-Agent (ADR 0032 §5).

A persistent kernel hides state: out-of-order or overwritten cells mean the live
session reaching ``ReturnAnswer`` does NOT prove the result reproduces. So the
runner concatenates the *accepted* cells (in execution order) into a standalone
``analysis.py`` and re-runs it in a **fresh, isolated** kernel against the
recorded inputs. The run is only accepted if this replay also completes and
reaches ``ReturnAnswer``.

By default replay re-runs nested skills (faithful reproducibility); reusing
recorded nested outputs is an ADR open question deferred to a later version.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .budget import MiniAgentBudget
from .kernel_session import KernelSession, KernelStartError
from .mini_agent import ANSWER_FILE, build_init_code

REPLAY_SCRIPT = "analysis.py"


@dataclass(slots=True)
class ReplayResult:
    """Outcome of re-running the emitted analysis script."""

    ok: bool
    script_path: str
    error: str = ""
    answer: str = ""
    stdout: str = ""


def emit_replay_script(
    workspace: Path,
    accepted_cells: list[str],
    input_paths: list[str],
    budget: MiniAgentBudget,
    *,
    replay_workspace: Path,
    process_guard: bool = False,
) -> Path:
    """Write the consolidated, re-runnable ``analysis.py`` and return its path."""
    header = (
        "# OmicsClaw Autonomous Code Mini-Agent — replay script (ADR 0032).\n"
        "# Re-runs the accepted cells from the input to reproduce the result.\n"
        "# Generated; do not edit by hand.\n"
    )
    init = build_init_code(replay_workspace, input_paths, budget, process_guard=process_guard)
    body = "\n\n".join(
        f"# === accepted step {i + 1} ===\n{cell}" for i, cell in enumerate(accepted_cells)
    )
    script = f"{header}{init}\n\n{body}\n"
    path = workspace / REPLAY_SCRIPT
    path.write_text(script, encoding="utf-8")
    return path


def validate_replay(
    *,
    workspace: Path,
    accepted_cells: list[str],
    input_paths: list[str],
    budget: MiniAgentBudget,
    sandbox: bool = True,
    process_guard: bool = False,
    repo_root: Path | None = None,
) -> ReplayResult:
    """Re-run the accepted cells in a fresh kernel; gate run acceptance on it."""
    workspace = Path(workspace)
    replay_ws = workspace / "replay"
    replay_ws.mkdir(parents=True, exist_ok=True)
    script_path = emit_replay_script(
        workspace,
        accepted_cells,
        input_paths,
        budget,
        replay_workspace=replay_ws,
        process_guard=process_guard,
    )

    if not accepted_cells:
        return ReplayResult(ok=False, script_path=str(script_path), error="no accepted cells to replay")

    kwargs = {} if repo_root is None else {"repo_root": repo_root}
    session = KernelSession(
        workspace_root=replay_ws,
        read_roots=list(input_paths),
        sandbox=sandbox,
        startup_timeout=90,
        **kwargs,
    )
    try:
        session.start()
    except KernelStartError as exc:
        return ReplayResult(ok=False, script_path=str(script_path), error=f"replay kernel start failed: {exc}")

    try:
        cell = session.execute(script_path.read_text(encoding="utf-8"), timeout=budget.wall_clock_seconds)
    finally:
        session.shutdown()

    answer = _read(replay_ws / ANSWER_FILE)
    if not cell.ok:
        return ReplayResult(
            ok=False,
            script_path=str(script_path),
            error=cell.error_summary or (cell.stderr or "")[-1000:] or "replay produced no result",
            stdout=(cell.stdout or "")[-2000:],
        )
    if not (replay_ws / ANSWER_FILE).exists():
        return ReplayResult(
            ok=False,
            script_path=str(script_path),
            error="replay completed without calling ReturnAnswer(...)",
            stdout=(cell.stdout or "")[-2000:],
        )
    return ReplayResult(
        ok=True,
        script_path=str(script_path),
        answer=answer,
        stdout=(cell.stdout or "")[-2000:],
    )


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


__all__ = ["REPLAY_SCRIPT", "ReplayResult", "emit_replay_script", "validate_replay"]
