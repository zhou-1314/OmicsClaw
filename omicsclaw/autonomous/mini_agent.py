"""The Autonomous Code Mini-Agent tactical loop (ADR 0032 §2, §8).

Given an approved goal + schema + plan from the outer loop, drive a persistent
sandboxed kernel through ``Purpose/Reasoning/Next Goal/Code`` steps until the
model calls ``ReturnAnswer`` or a budget trips. Each cell is AST-linted before
execution; skill execution goes through the injected ``oc`` facade only.

The loop owns *tactics* (write -> run -> observe -> revise); the outer loop keeps
the two ADR 0014 judgment seams (pre-handoff preflight, post-run validation).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Protocol

from .budget import BudgetLedger, MiniAgentBudget, MiniAgentStep, TerminationReason
from .kernel_session import KernelSession
from .protocol import TurnFormatError, parse_turn
from .runtime_guard import build_kernel_guard_code
from .skill_facade import SKILL_CALLS_LOG
from .validation import validate_generated_code

ANSWER_FILE = "_oc_answer.txt"

# In-loop capability backstop: if the model never produces a single parseable,
# lint-clean turn within this many opening steps, it cannot drive the contract
# (distinct from a capable model hitting a hard runtime problem). ADR 0032 §8.
WARMUP_STEPS = 3


class MiniAgentLLM(Protocol):
    """Single-prompt completion client (matches ProviderChatClient)."""

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str | None: ...


@dataclass(slots=True)
class MiniAgentOutcome:
    """Result of one mini-agent loop."""

    answer: str
    termination: TerminationReason
    steps: list[MiniAgentStep] = field(default_factory=list)
    accepted_cells: list[str] = field(default_factory=list)
    ledger: dict | None = None

    @property
    def succeeded(self) -> bool:
        return self.termination == TerminationReason.RETURNED_ANSWER


def _sync_skill_calls(ledger: BudgetLedger, workspace: Path) -> None:
    """Mirror nested skill calls into the host-side ledger.

    The ``oc`` facade runs inside the kernel and appends one line per call to
    ``skill_calls.jsonl``; the host loop cannot see its in-process counter, so we
    recount the log after each cell to keep ``skill_calls_used`` (provenance and
    the budget report) accurate. The facade itself remains the hard per-call
    enforcer. Idempotent: only the delta is recorded.
    """
    log = workspace / SKILL_CALLS_LOG
    if not log.exists():
        return
    total = sum(1 for line in log.read_text(encoding="utf-8").splitlines() if line.strip())
    while ledger.skill_calls_used < total:
        ledger.record_skill_call()


def run_mini_agent(
    *,
    session: KernelSession,
    llm: MiniAgentLLM,
    goal: str,
    workspace_root: str | Path,
    input_paths: list[str] | None = None,
    data_schema: str = "",
    analysis_plan: str = "",
    budget: MiniAgentBudget | None = None,
    process_guard: bool = False,
) -> MiniAgentOutcome:
    """Run the tactical loop against an already-started kernel session.

    ``process_guard=True`` (the non-bwrap tier) prepends an in-kernel guard cell
    that blocks network egress and confines writes to the workspace.
    """
    budget = (budget or MiniAgentBudget()).clamped()
    workspace = Path(workspace_root)
    answer_path = workspace / ANSWER_FILE

    init = session.execute(
        build_init_code(workspace, input_paths or [], budget, process_guard=process_guard),
        timeout=180,
    )
    if not init.ok:
        return MiniAgentOutcome(
            answer="",
            termination=TerminationReason.ENGINE_ERROR,
            steps=[MiniAgentStep(index=0, purpose="kernel init", code="<init>", error=init.error_summary or init.stderr)],
        )

    system_prompt = build_system_prompt(goal, data_schema, analysis_plan)
    ledger = BudgetLedger(budget=budget)
    transcript: list[str] = []
    steps: list[MiniAgentStep] = []
    accepted_cells: list[str] = []
    started = time.monotonic()
    answer = ""
    termination = TerminationReason.STEP_BUDGET
    produced_usable_turn = False
    warmup_steps = max(1, min(WARMUP_STEPS, budget.max_steps))

    while True:
        # Capability backstop FIRST: a model that has not produced one parseable,
        # lint-clean turn by the end of the warmup window is not driving the
        # contract. Checked before the budget so it is reported as MODEL_INCAPABLE
        # rather than the coincident CONSECUTIVE_FAILURES / STEP_BUDGET.
        if not produced_usable_turn and ledger.steps_used >= warmup_steps:
            termination = TerminationReason.MODEL_INCAPABLE
            break

        reason = ledger.exhausted_reason(elapsed_seconds=time.monotonic() - started)
        if reason is not None:
            termination = reason
            break

        prompt = system_prompt + "\n\n" + "\n\n".join(transcript) + "\n\nProduce the next step."
        raw = llm.complete(prompt, temperature=0.0)
        index = ledger.steps_used + 1
        tokens = _estimate_tokens(prompt) + _estimate_tokens(raw or "")

        if not raw:
            steps.append(MiniAgentStep(index=index, error="LLM returned no content."))
            ledger.record_step(accepted=False, tokens=tokens)
            transcript.append(f"[step {index}] engine error: empty LLM response. Try again.")
            continue

        try:
            turn = parse_turn(raw)
        except TurnFormatError as exc:
            problems = "; ".join(exc.problems)
            steps.append(MiniAgentStep(index=index, error=f"format: {problems}", tokens=tokens))
            ledger.record_step(accepted=False, tokens=tokens)
            transcript.append(
                f"[step {index}] your response was rejected: {problems}. "
                "Respond again with the required **Purpose**/**Reasoning**/**Next Goal**/**Code** sections."
            )
            continue

        issues = validate_generated_code(turn.code, language="python")
        if issues:
            joined = "; ".join(issues)
            steps.append(
                MiniAgentStep(
                    index=index,
                    purpose=turn.purpose,
                    code=turn.code,
                    error=f"blocked: {joined}",
                    tokens=tokens,
                )
            )
            ledger.record_step(accepted=False, tokens=tokens)
            transcript.append(
                f"[step {index}] code rejected by the safety lint: {joined}. "
                "Use the `oc` facade for skills; do not import subprocess/os/network."
            )
            continue

        # The model produced a parseable, lint-clean step — it can drive the
        # contract, so the capability backstop no longer applies.
        produced_usable_turn = True
        before = set(session.introspect())
        timeout = budget.skill_call_timeout_seconds if _references_oc(turn.code) else budget.raw_cell_timeout_seconds
        cell = session.execute(turn.code, timeout=timeout)
        if cell.timed_out:
            new_vars = {}
        else:
            after = session.introspect()
            new_vars = {k: _fmt_var(v) for k, v in after.items() if k not in before}

        step = MiniAgentStep(
            index=index,
            purpose=turn.purpose,
            reasoning=turn.reasoning,
            next_goal=turn.next_goal,
            code=turn.code,
            stdout=cell.stdout,
            stderr=cell.stderr,
            error=cell.error_summary,
            new_variables=new_vars,
            duration_seconds=cell.duration_seconds,
            accepted=cell.ok,
            tokens=tokens,
        )
        steps.append(step)
        ledger.record_step(accepted=cell.ok, tokens=tokens)
        _sync_skill_calls(ledger, workspace)

        if cell.ok:
            accepted_cells.append(turn.code)

        transcript.append(_feedback(step))

        if cell.timed_out:
            termination = TerminationReason.ENGINE_ERROR
            break

        if turn.calls_return_answer and cell.ok:
            if _answer_written(answer_path):
                answer = _read_answer(answer_path)
                termination = TerminationReason.RETURNED_ANSWER
                break
            transcript.append(
                f"[step {index}] ReturnAnswer appeared in code, but no answer file was written. "
                "Call ReturnAnswer(...) in executed top-level code before finishing."
            )

    return MiniAgentOutcome(
        answer=answer,
        termination=termination,
        steps=steps,
        accepted_cells=accepted_cells,
        ledger=ledger.to_dict(),
    )


# --------------------------------------------------------------------------- #
# namespace + prompt construction
# --------------------------------------------------------------------------- #


def build_init_code(
    workspace: Path,
    input_paths: list[str],
    budget: MiniAgentBudget,
    *,
    process_guard: bool = False,
) -> str:
    """Kernel bootstrap: bind ``oc`` / ``ReturnAnswer`` / ``show`` / ``adata``.

    With ``process_guard`` the non-bwrap in-kernel guard (no-network,
    workspace-confined writes) is prepended as the first thing the kernel runs.
    """
    fig_dir = workspace / "figures"
    answer_file = workspace / ANSWER_FILE
    h5ad_inputs = [str(p) for p in input_paths if str(p).endswith(".h5ad")]
    # Appended AFTER the imports so library caches (matplotlib font cache, etc.)
    # warm up before the destructive-os-op block applies.
    guard = (
        "\n" + build_kernel_guard_code(workspace_root=workspace, read_roots=list(input_paths))
        if process_guard
        else ""
    )
    return f"""
import sys as _sys
if {str(_repo_root())!r} not in _sys.path:
    _sys.path.insert(0, {str(_repo_root())!r})
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as _plt
from pathlib import Path as _Path
_Path({str(fig_dir)!r}).mkdir(parents=True, exist_ok=True)
from omicsclaw.autonomous.skill_facade import build_facade as _build_facade
oc = _build_facade(
    {str(workspace)!r},
    max_skill_calls={budget.max_skill_calls},
    skill_timeout_seconds={budget.skill_call_timeout_seconds},
)
_oc_fig = [0]
def show(*_a, **_k):
    for _num in _plt.get_fignums():
        _oc_fig[0] += 1
        _plt.figure(_num).savefig(
            {str(fig_dir)!r} + "/fig_%02d.png" % _oc_fig[0], dpi=120, bbox_inches="tight"
        )
    _plt.close("all")
def ReturnAnswer(text=""):
    with open({str(answer_file)!r}, "w", encoding="utf-8") as _f:
        _f.write(str(text))
    print("__OC_RETURN_ANSWER__")
adata = None
for _p in {h5ad_inputs!r}:
    try:
        import anndata as _ad
        adata = _ad.read_h5ad(_p)
        print("loaded adata from", _p, "shape", getattr(adata, "shape", None))
        break
    except Exception as _e:
        print("could not load", _p, ":", _e)
print("[mini-agent kernel ready]")
""" + guard


def build_system_prompt(goal: str, data_schema: str, analysis_plan: str) -> str:
    """Instruction prefix shared across steps."""
    parts = [
        "You are the OmicsClaw Autonomous Code Mini-Agent. You solve one bioinformatics",
        "analysis by writing small Python cells that run in a persistent, network-isolated",
        "kernel. Variables persist across steps.",
        "",
        "EVERY response MUST have exactly these four markdown sections:",
        "**Purpose**: one line on what this step does.",
        "**Reasoning**: why, given prior results.",
        "**Next Goal**: what you will do after.",
        "**Code**:",
        "```python",
        "# one focused step",
        "```",
        "",
        "Available in the kernel namespace:",
        "- `adata`: the input AnnData (already loaded if an .h5ad was provided).",
        "- `oc`: the vetted skill facade. Run heavy/standard steps as skills, e.g.",
        "  `res = oc.run('spatial-preprocess', adata, method='scanpy'); adata = res.adata`.",
        "  `res.adata` is the reloaded result; `res.tables` / `res.figures` list artifacts.",
        "  ALWAYS prefer a skill over hand-rolling QC/normalisation/clustering/DE parameters.",
        "- `show(...)`: save current matplotlib figures into the run's figures/ folder.",
        "- `ReturnAnswer(text)`: call this once, when done, with your final summary.",
        "",
        "Rules: do NOT import subprocess/os.system/socket/requests or install packages.",
        "Write only inside the run workspace. Use `oc` for all skill execution.",
        "Inspect before you commit to parameters. Finish by calling ReturnAnswer(...).",
        "",
        f"GOAL: {goal}",
    ]
    if data_schema.strip():
        parts += ["", "INPUT DATA SCHEMA (ground truth — read real keys/columns from here):", data_schema.strip()]
    if analysis_plan.strip():
        parts += ["", "APPROVED PLAN:", analysis_plan.strip()]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _references_oc(code: str) -> bool:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return "oc." in code
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "oc":
            return True
    return False


def _fmt_var(info: dict) -> str:
    shape = info.get("shape")
    return f"{info.get('type', '?')}{f' shape={shape}' if shape else ''}"


def _read_answer(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _answer_written(path: Path) -> bool:
    return path.exists()


def _estimate_tokens(text: str) -> int:
    # Cheap deterministic budget accounting; intentionally approximate.
    return max(1, (len(text or "") + 3) // 4)


def _feedback(step: MiniAgentStep) -> str:
    lines = [f"[step {step.index}] purpose: {step.purpose}"]
    if step.stdout.strip():
        lines.append("stdout:\n" + step.stdout.strip()[-1500:])
    if step.error:
        lines.append(f"ERROR: {step.error}")
        if step.stderr.strip():
            lines.append("stderr:\n" + step.stderr.strip()[-1200:])
    if step.new_variables:
        lines.append("new variables: " + ", ".join(f"{k} ({v})" for k, v in step.new_variables.items()))
    if not step.accepted and not step.error:
        lines.append("(no output)")
    return "\n".join(lines)


__all__ = [
    "ANSWER_FILE",
    "MiniAgentLLM",
    "MiniAgentOutcome",
    "build_init_code",
    "build_system_prompt",
    "run_mini_agent",
]
