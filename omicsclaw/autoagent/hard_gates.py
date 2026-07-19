"""Hard gates — must-pass checks before composite scoring.

The acceptance function for harness-level evolution is:

    accept = hard_gates_pass AND composite_score_improved

Hard gates catch catastrophic regressions that a weighted score might
mask.  They run before the soft metric comparison and produce a clear
pass/fail signal with diagnostics.

Gate categories:
1. **authority_bound** — trace evidence is bound to the executed trial tree.
2. **receipt_bound** — child claim/result evidence matches frozen authority.
3. **no_crash** — exit code must be 0.
4. **artifacts_present** — required output files must exist.
5. **cell_retention** — cell count must not collapse below threshold.
6. **fallback_recorded** — if a fallback occurred it must be logged.
7. **no_empty_output** — declared primary AnnData must not be empty.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from omicsclaw.autoagent.authority import TrialSkillAuthority
from omicsclaw.autoagent.output_ownership import (
    VerifiedChildTrialReceipt,
    verify_child_trial_receipt,
)
from omicsclaw.autoagent.trace import RunTrace
from omicsclaw.common.output_claim import (
    collect_output_claim_identities,
    is_scientific_output_file,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gate result
# ---------------------------------------------------------------------------


@dataclass
class GateResult:
    """Result of a single hard gate check."""

    name: str
    passed: bool
    message: str
    value: Any = None
    threshold: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HardGateVerdict:
    """Aggregated result of all hard gates for one trial."""

    all_passed: bool
    results: list[GateResult] = field(default_factory=list)
    receipt: dict[str, Any] = field(default_factory=dict)

    @property
    def failed_gates(self) -> list[GateResult]:
        return [r for r in self.results if not r.passed]

    def summary(self) -> str:
        """One-line pass/fail summary for logging."""
        if self.all_passed:
            return f"All {len(self.results)} hard gates passed."
        failed = self.failed_gates
        names = ", ".join(g.name for g in failed)
        return f"{len(failed)}/{len(self.results)} hard gate(s) FAILED: {names}"

    def to_diagnostic(self) -> str:
        """Multi-line diagnostic for Meta-Agent context."""
        lines: list[str] = []
        for r in self.results:
            status = "PASS" if r.passed else "FAIL"
            line = f"  [{status}] {r.name}: {r.message}"
            if r.value is not None:
                line += f" (value={r.value}"
                if r.threshold is not None:
                    line += f", threshold={r.threshold}"
                line += ")"
            lines.append(line)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "all_passed": self.all_passed,
            "results": [r.to_dict() for r in self.results],
            "receipt": dict(self.receipt),
        }


# ---------------------------------------------------------------------------
# Gate definitions
# ---------------------------------------------------------------------------


def gate_authority_bound(trace: RunTrace) -> GateResult:
    """The trace must carry the post-verified authority from its execution."""

    authority = trace.authority
    passed = (
        isinstance(authority, TrialSkillAuthority)
        and authority.matches_skill_name(trace.skill_name)
    )
    return GateResult(
        name="authority_bound",
        passed=passed,
        message=(
            "Trial trace is bound to its frozen sandbox authority."
            if passed
            else "Trial trace has no matching post-verified sandbox authority."
        ),
    )


def gate_receipt_bound(
    trace: RunTrace,
    output_dir: Path,
) -> GateResult:
    """The exact child claim/result receipt must match frozen authority."""

    authority = trace.authority
    if not isinstance(authority, TrialSkillAuthority):
        return GateResult(
            name="receipt_bound",
            passed=False,
            message="Child receipt cannot be verified without trial authority.",
        )

    try:
        receipt = verify_child_trial_receipt(
            output_dir,
            canonical_skill_id=authority.canonical_skill_id,
            skill_version=authority.skill_version,
            manifest_hash=authority.manifest_hash,
            source_hash=authority.source_hash,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return _receipt_gate_result(trace, None, str(exc))
    return _receipt_gate_result(trace, receipt, "")


def _receipt_gate_result(
    trace: RunTrace,
    receipt: VerifiedChildTrialReceipt | None,
    verification_error: str,
) -> GateResult:
    """Build one receipt gate result from a verifier observation."""

    authority = trace.authority
    passed = (
        isinstance(authority, TrialSkillAuthority)
        and receipt is not None
        and receipt.canonical_skill_id == authority.canonical_skill_id
        and receipt.skill_version == authority.skill_version
    )
    return GateResult(
        name="receipt_bound",
        passed=passed,
        message=(
            "Child run claim and result envelope match frozen trial authority."
            if passed
            else "Child receipt is not bound to frozen trial authority: "
            + (verification_error or "receipt mismatch")
        ),
    )


def gate_no_crash(trace: RunTrace) -> GateResult:
    """Trial must complete without crashing (exit_code == 0)."""
    passed = trace.execution.exit_code == 0
    return GateResult(
        name="no_crash",
        passed=passed,
        message="Process exited cleanly." if passed else (
            f"Process crashed with exit code {trace.execution.exit_code}."
        ),
        value=trace.execution.exit_code,
        threshold=0,
    )


def gate_artifacts_present(
    trace: RunTrace,
    output_dir: Path,
    required_files: list[str] | None = None,
    *,
    verified_receipt: VerifiedChildTrialReceipt | None = None,
) -> GateResult:
    """Required output files must exist in the output directory.

    Default required files: ``result.json``. For AnnData-producing Skills, the
    trial-authority primary ``.h5ad`` inventory path is also required.
    """
    if required_files is None:
        if not gate_authority_bound(trace).passed:
            return GateResult(
                name="artifacts_present",
                passed=False,
                message="Required artifacts cannot be derived without trial authority.",
            )
        required_files = ["result.json"]
        primary_anndata = trace.authority.primary_anndata_path
        if primary_anndata:
            required_files.append(primary_anndata)

    output_dir = Path(output_dir)
    claim_identities = (
        frozenset({verified_receipt.claim_identity})
        if verified_receipt is not None
        else collect_output_claim_identities(output_dir)
    )
    missing = [
        relative
        for relative in required_files
        if not is_scientific_output_file(
            output_dir / relative,
            output_root=output_dir,
            claim_identities=claim_identities,
        )
    ]

    passed = len(missing) == 0
    if passed:
        msg = f"All {len(required_files)} required artifact(s) present."
    else:
        msg = f"Missing artifacts: {', '.join(missing)}"

    return GateResult(
        name="artifacts_present",
        passed=passed,
        message=msg,
        value=required_files if passed else missing,
    )


def gate_cell_retention(
    trace: RunTrace,
    min_retention: float = 0.05,
) -> GateResult:
    """Cell retention rate must not collapse below threshold.

    A retention rate of 0.05 means at least 5% of cells must survive
    filtering.  This catches catastrophic over-filtering.
    """
    rate = trace.data_shape.cell_retention_rate
    n_before = trace.data_shape.n_obs_before
    n_after = trace.data_shape.n_obs_after

    # Skip if the pre-filter count was never recorded (unknown).
    # If it was explicitly recorded as 0, the gate should fail.
    if n_before == 0 and not trace.data_shape.n_obs_before_known:
        return GateResult(
            name="cell_retention",
            passed=True,
            message="Skipped: no pre-filter cell count available.",
        )
    if n_before == 0 and trace.data_shape.n_obs_before_known:
        return GateResult(
            name="cell_retention",
            passed=False,
            message="Input data has 0 cells (n_obs_before=0).",
            value=0.0,
            threshold=min_retention,
        )

    passed = rate >= min_retention
    if passed:
        msg = (
            f"Retained {n_after}/{n_before} cells "
            f"({rate:.1%} >= {min_retention:.0%})."
        )
    else:
        msg = (
            f"Cell retention collapsed: {n_after}/{n_before} cells "
            f"({rate:.1%} < {min_retention:.0%} threshold)."
        )

    return GateResult(
        name="cell_retention",
        passed=passed,
        message=msg,
        value=round(rate, 4),
        threshold=min_retention,
    )


def gate_no_empty_output(trace: RunTrace) -> GateResult:
    """Processed output must have >0 cells and >0 genes."""
    n_obs = trace.data_shape.n_obs_after
    n_vars = trace.data_shape.n_vars_after

    shape_known = (
        trace.data_shape.n_obs_after_known
        or trace.data_shape.n_vars_after_known
    )

    # A missing observation is different from a measured zero-sized output.
    if (
        not shape_known
        and n_obs == 0
        and n_vars == 0
        and trace.execution.exit_code == 0
    ):
        return GateResult(
            name="no_empty_output",
            passed=True,
            message="Skipped: data shape not available in trace.",
        )

    passed = n_obs > 0 and n_vars > 0
    if passed:
        msg = f"Output has {n_obs} cells x {n_vars} genes."
    else:
        msg = f"Empty output: {n_obs} cells x {n_vars} genes."

    return GateResult(
        name="no_empty_output",
        passed=passed,
        message=msg,
        value={"n_obs": n_obs, "n_vars": n_vars},
    )


def gate_fallback_recorded(trace: RunTrace) -> GateResult:
    """If a method fallback occurred, it must be recorded with a reason.

    Silent fallbacks (switching methods without logging why) undermine
    reproducibility and make diagnosis impossible.
    """
    mt = trace.method_trace

    # No fallback → pass
    if not mt.fallback_used:
        return GateResult(
            name="fallback_recorded",
            passed=True,
            message="No fallback occurred.",
        )

    # Fallback with reason → pass
    if mt.fallback_reason:
        return GateResult(
            name="fallback_recorded",
            passed=True,
            message=(
                f"Fallback recorded: {mt.requested_method} -> "
                f"{mt.executed_method} ({mt.fallback_reason})"
            ),
        )

    # Fallback without reason → fail
    return GateResult(
        name="fallback_recorded",
        passed=False,
        message=(
            f"Silent fallback: {mt.requested_method} -> "
            f"{mt.executed_method} with no recorded reason."
        ),
    )


# ---------------------------------------------------------------------------
# Run all gates
# ---------------------------------------------------------------------------

# Default gate list in execution order
DEFAULT_GATES = [
    "authority_bound",
    "receipt_bound",
    "no_crash",
    "artifacts_present",
    "cell_retention",
    "no_empty_output",
    "fallback_recorded",
]


def run_hard_gates(
    trace: RunTrace,
    output_dir: Path,
    *,
    gates: list[str] | None = None,
    min_cell_retention: float = 0.05,
    required_artifacts: list[str] | None = None,
) -> HardGateVerdict:
    """Run all hard gates against a trial trace.

    Parameters
    ----------
    trace:
        The RunTrace from the completed trial.
    output_dir:
        Path to the trial output directory.
    gates:
        Quality gate names to run. Defaults to :data:`DEFAULT_GATES`;
        ``receipt_bound`` is always added as an admission precondition.
    min_cell_retention:
        Minimum cell retention rate (0.0-1.0).
    required_artifacts:
        Override default required artifact list.

    Returns
    -------
    HardGateVerdict
        Aggregated pass/fail with per-gate details.
    """
    output_dir = Path(output_dir)
    configured_gates = list(DEFAULT_GATES if gates is None else gates)
    # Receipt binding is an admission precondition, not an optional quality
    # check.  A custom gate subset therefore cannot bypass it.
    active_gates = list(configured_gates)
    if "receipt_bound" not in active_gates:
        active_gates.insert(0, "receipt_bound")

    verified_receipt: VerifiedChildTrialReceipt | None = None
    receipt_error = ""
    authority = trace.authority
    if isinstance(authority, TrialSkillAuthority):
        try:
            verified_receipt = verify_child_trial_receipt(
                output_dir,
                canonical_skill_id=authority.canonical_skill_id,
                skill_version=authority.skill_version,
                manifest_hash=authority.manifest_hash,
                source_hash=authority.source_hash,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            receipt_error = str(exc)
    else:
        receipt_error = "trial authority is missing"

    gate_dispatch = {
        "authority_bound": lambda: gate_authority_bound(trace),
        "receipt_bound": lambda: _receipt_gate_result(
            trace,
            verified_receipt,
            receipt_error,
        ),
        "no_crash": lambda: gate_no_crash(trace),
        "artifacts_present": lambda: gate_artifacts_present(
            trace,
            output_dir,
            required_artifacts,
            verified_receipt=verified_receipt,
        ),
        "cell_retention": lambda: gate_cell_retention(
            trace, min_cell_retention,
        ),
        "no_empty_output": lambda: gate_no_empty_output(trace),
        "fallback_recorded": lambda: gate_fallback_recorded(trace),
    }

    results: list[GateResult] = []
    for gate_name in active_gates:
        gate_fn = gate_dispatch.get(gate_name)
        if gate_fn is None:
            results.append(
                GateResult(
                    name=gate_name,
                    passed=False,
                    message=f"Unknown hard gate configuration: {gate_name}",
                )
            )
            continue
        try:
            result = gate_fn()
        except Exception as exc:
            result = GateResult(
                name=gate_name,
                passed=False,
                message=f"Gate raised exception: {exc}",
            )
        results.append(result)

    all_passed = all(r.passed for r in results)
    return HardGateVerdict(
        all_passed=all_passed,
        results=results,
        receipt=(
            verified_receipt.to_audit_dict()
            if verified_receipt is not None
            else {}
        ),
    )
