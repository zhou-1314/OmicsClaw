"""Typed (A path) consensus driver — one-shot orchestration.

``run_typed_consensus`` is the single seam through which every thin skill
(consensus-domains, sc-consensus-clustering, future consensus-celltypes /
consensus-de) executes an A-path run. Each thin skill is reduced to
``argparse → plan members → run_typed_consensus → format_typed_report``.

The driver owns:

1. ``plan.json`` audit dump (before fan-out, so audit survives early failures)
2. parallel skill-subprocess fan-out via ``fan_out`` (ADR 0010)
3. label + intrinsic gather through the source's ``MemberArtifactReader``
4. composite scoring + cross-method NMI matrix
5. BC selection (via injected ``bc_selector`` callable)
6. operator dispatch (kmode / weighted / lca) + ``LCAUnavailableError``
   surfacing
7. canonical artifact writes (``consensus_labels.tsv``, ``member_scores.csv``,
   ``cross_method_nmi.csv`` — schema stable across thin skills)

The driver does NOT write ``report.md`` — markdown rendering lives in
``report.format_typed_report`` so thin skills can pass a per-skill title.
Banner enforcement still happens in ``report.format_typed_report``; no
caller can produce a verified report without the ``[A: Verified consensus]``
prefix.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence

import numpy as np
import pandas as pd

from omicsclaw.runtime.consensus.member import ConsensusMember
from omicsclaw.runtime.consensus.operators.categorical import (
    ConsensusResult,
    kmode_consensus,
    weighted_consensus,
)
from omicsclaw.runtime.consensus.scoring import (
    ALPHA_DEFAULT,
    BETA_DEFAULT,
    MAX_CLASS_FRAC_CAP_DEFAULT,
    MemberScore,
    score_all_members,
)
from omicsclaw.runtime.consensus.source_registry import ConsensusSource
from omicsclaw.runtime.workflow import (
    DEFAULT_TIMEOUT_SECONDS,
    FanOutResult,
    InsufficientSurvivorsError,
    StepRunResult,
    fan_out,
)

OperatorName = Literal["kmode", "weighted", "lca"]
BCSelectorFn = Callable[[list[MemberScore], int], list[str]]

# Consensus needs at least two members to merge. This survivor minimum is L2's
# policy, supplied explicitly to the neutral L1 fan-out so a sub-threshold run
# raises InsufficientSurvivorsError with the full per-member failure summary
# (crash/timeout details) — diagnostics the readable-label gate alone can't give.
MIN_CONSENSUS_MEMBERS = 2


@dataclass(frozen=True)
class ScoreConfig:
    """ADR 0011 composite-score weights and class-imbalance cap."""

    alpha: float = ALPHA_DEFAULT
    beta: float = BETA_DEFAULT
    max_class_frac_cap: float = MAX_CLASS_FRAC_CAP_DEFAULT


@dataclass(frozen=True)
class TypedConsensusRun:
    """Complete result of one ``run_typed_consensus`` invocation.

    Every downstream consumer (report rendering, graph-memory writer, CI
    benchmark) programs against this dataclass rather than against the
    output filesystem.
    """

    run_id: str
    operator: str
    members: tuple[ConsensusMember, ...]
    team_result: FanOutResult
    labels_df: pd.DataFrame
    intrinsic_map: dict[str, float]
    scores: tuple[MemberScore, ...]
    nmi_matrix: pd.DataFrame
    selected_bcs: tuple[str, ...]
    consensus: ConsensusResult
    output_dir: Path
    artifacts_written: tuple[Path, ...]
    missing_label_members: tuple[str, ...] = field(default_factory=tuple)


class InsufficientBCsError(RuntimeError):
    """Raised when the BC selector returns fewer than 2 base clusterings.

    ADR 0010 forbids silent fallback — A path failure is loud by design.
    """


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _gather_labels(
    survivors: Sequence[StepRunResult],
    source: ConsensusSource,
) -> tuple[pd.DataFrame, dict[str, float], list[str]]:
    """Pull labels + intrinsic quality through the source reader."""
    columns: dict[str, pd.Series] = {}
    intrinsic: dict[str, float] = {}
    missing: list[str] = []
    for r in survivors:
        output_root = r.output_dir.parent
        labels = source.reader.read_labels(r.step, output_root)
        if labels is None:
            missing.append(r.step.name)
            continue
        columns[r.step.name] = labels
        intrinsic[r.step.name] = source.reader.read_intrinsic_quality(r.step, output_root)
    if not columns:
        return pd.DataFrame(), {}, missing
    labels_df = pd.concat(columns, axis=1).dropna(axis=0, how="any")
    return labels_df, intrinsic, missing


def _cross_method_nmi_matrix(labels_df: pd.DataFrame) -> pd.DataFrame:
    from sklearn.metrics import normalized_mutual_info_score

    cols = list(labels_df.columns)
    matrix = np.zeros((len(cols), len(cols)), dtype=float)
    for i, a in enumerate(cols):
        for j, b in enumerate(cols):
            matrix[i, j] = 1.0 if i == j else float(
                normalized_mutual_info_score(labels_df[a], labels_df[b])
            )
    return pd.DataFrame(matrix, index=cols, columns=cols)


def _run_operator(
    operator: OperatorName,
    labels_df: pd.DataFrame,
    *,
    seed: int,
    score_lookup: Mapping[str, float],
) -> ConsensusResult:
    if operator == "kmode":
        return kmode_consensus(labels_df, seed=seed)
    if operator == "weighted":
        weights = {c: max(float(score_lookup.get(c, 0.0)), 1e-6) for c in labels_df.columns}
        return weighted_consensus(labels_df, weights=weights, seed=seed)
    if operator == "lca":
        # Late import is intentional — the R subprocess wrapper raises
        # ``LCAUnavailableError`` if Rscript / diceR is missing; we want
        # that error to propagate so the thin skill can render a printable
        # message + exit non-zero, but we don't want the kmode/weighted
        # paths to drag the import.
        from omicsclaw.runtime.consensus.operators.lca_r import lca_consensus

        return lca_consensus(labels_df, seed=seed)
    raise ValueError(f"unknown operator: {operator!r}")


# --------------------------------------------------------------------------- #
# Entry                                                                       #
# --------------------------------------------------------------------------- #

async def run_typed_consensus(
    *,
    members: Sequence[ConsensusMember],
    source: ConsensusSource,
    input_path: str,
    output_dir: Path | str,
    operator: OperatorName,
    bc_selector: BCSelectorFn,
    top_k_default: int = 4,
    score_config: ScoreConfig = ScoreConfig(),
    seed: int = 0,
    plan_audit: Mapping[str, Any] | None = None,
    cancel_event: threading.Event | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_parallel: int | None = None,
    runner: Any = None,
) -> TypedConsensusRun:
    """Orchestrate one typed-consensus run end-to-end.

    Raises
    ------
    InsufficientSurvivorsError
        Fewer than 2 members survived fan-out.
    InsufficientBCsError
        ``bc_selector`` returned fewer than 2 base clusterings.
    LCAUnavailableError
        ``operator='lca'`` but the R subprocess can't run.
    """
    output_dir_p = Path(output_dir)
    output_dir_p.mkdir(parents=True, exist_ok=True)
    artifacts: list[Path] = []

    # 1. plan.json audit BEFORE fan-out
    if plan_audit is not None:
        plan_path = output_dir_p / "plan.json"
        # Slice 0 precondition for consensus-interpret (ADR 0012): the
        # absolute, resolved adata path is the canonical handoff for
        # downstream interpreted runs. Driver is authoritative — overwrite
        # any caller-supplied (possibly relative) value.
        audit = dict(plan_audit)
        audit["input_path"] = str(Path(input_path).resolve())
        plan_path.write_text(json.dumps(audit, indent=2))
        artifacts.append(plan_path)

    run_id = str((plan_audit or {}).get("run_id") or output_dir_p.name)

    # 2. fan-out
    #
    # Pass the consensus survivor minimum explicitly: L1 is domain-neutral and
    # sets no threshold, but L2 requires >=2 surviving subprocesses. Doing it
    # here means a sub-threshold run fails loudly with fan_out's full per-member
    # failure summary (crash/timeout statuses), not a bare label-count message.
    team: FanOutResult = await fan_out(
        members,
        input_path=input_path,
        output_root=output_dir_p,
        cancel_event=cancel_event,
        timeout_seconds=timeout_seconds,
        max_parallel=max_parallel,
        required_survivors=MIN_CONSENSUS_MEMBERS,
        runner=runner,
    )

    # 3. gather labels through the source reader
    #
    # fan_out already guaranteed >= MIN_CONSENSUS_MEMBERS surviving subprocesses.
    # Surviving is necessary but not sufficient — a member can exit 0 yet emit
    # unreadable labels — so this gate additionally requires two readable label
    # columns.
    labels_df, intrinsic_map, missing = _gather_labels(team.survived, source)
    if labels_df.shape[1] < MIN_CONSENSUS_MEMBERS:
        raise InsufficientSurvivorsError(
            f"Only {labels_df.shape[1]} member(s) produced readable labels "
            f"(< {MIN_CONSENSUS_MEMBERS} required). Missing artifacts: {missing}"
        )

    # 4. score + persist score table
    labels_arrays = {col: labels_df[col].to_numpy() for col in labels_df.columns}
    scores = score_all_members(
        labels_arrays,
        intrinsic_map,
        alpha=score_config.alpha,
        beta=score_config.beta,
        max_class_frac_cap=score_config.max_class_frac_cap,
    )
    scores_path = output_dir_p / "member_scores.csv"
    pd.DataFrame([asdict(s) for s in scores]).to_csv(scores_path, index=False)
    artifacts.append(scores_path)

    # 5. cross-method NMI + persist
    nmi_df = _cross_method_nmi_matrix(labels_df)
    nmi_path = output_dir_p / "cross_method_nmi.csv"
    nmi_df.to_csv(nmi_path)
    artifacts.append(nmi_path)

    # 6. BC selection via injected callable
    selected = bc_selector(scores, top_k_default)
    if len(selected) < 2:
        raise InsufficientBCsError(
            f"BC selector returned {len(selected)} member(s) (< 2 required). "
            f"Selected: {selected}"
        )

    # 7. operator
    score_lookup = {s.member: s.composite for s in scores}
    consensus = _run_operator(
        operator, labels_df[selected], seed=seed, score_lookup=score_lookup
    )

    # 8. consensus labels TSV
    consensus_path = output_dir_p / "consensus_labels.tsv"
    pd.DataFrame(
        {
            "observation": consensus.labels.index,
            f"consensus_{operator}": consensus.labels.values,
        }
    ).to_csv(consensus_path, sep="\t", index=False)
    artifacts.append(consensus_path)

    return TypedConsensusRun(
        run_id=run_id,
        operator=operator,
        members=tuple(members),
        team_result=team,
        labels_df=labels_df,
        intrinsic_map=intrinsic_map,
        scores=tuple(scores),
        nmi_matrix=nmi_df,
        selected_bcs=tuple(selected),
        consensus=consensus,
        output_dir=output_dir_p,
        artifacts_written=tuple(artifacts),
        missing_label_members=tuple(missing),
    )
