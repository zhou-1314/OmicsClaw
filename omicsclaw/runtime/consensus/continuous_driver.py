"""Continuous (rank-gauge) consensus driver (ADR 0031) — A-path, one-shot.

The continuous analog of ``driver.run_typed_consensus``: fan out pseudotime
members, gather each member's canonical per-cell ``pseudotime`` vector,
rank-normalise + direction-align them, score by mean pairwise Spearman, BC-select,
aggregate via ``median`` / ``weighted``, re-rank the consensus to ``[0, 1]``, and
write the banner'd artifacts. It is its **own** driver (not folded into
``run_typed_consensus``) bound to the ``continuous`` template; it reuses the
domain-neutral L1/L2 seams (``fan_out`` + ``MIN_CONSENSUS_MEMBERS``,
``top_k_by_score``) but its scoring/operators/artifacts are continuous-specific.

Like ``driver.py`` it must **not** import ``dispatch`` (``templates`` imports this
module, and ``dispatch`` imports ``templates`` — a cycle). Banner enforcement
lives in ``continuous_report.format_continuous_report``, never here.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd

from omicsclaw.runtime.consensus.continuous_scoring import (
    ContinuousMemberScore,
    score_continuous_members,
)
from omicsclaw.runtime.consensus.driver import (
    InsufficientBCsError,
    MIN_CONSENSUS_MEMBERS,
    ScoreConfig,
)
from omicsclaw.runtime.consensus.member import ConsensusMember
from omicsclaw.runtime.consensus.operators.continuous import (
    ContinuousConsensusResult,
    align_directions,
    is_degenerate,
    median_consensus,
    pairwise_spearman,
    rank_normalize,
    weak_agreement_stats,
    weighted_consensus,
)
from omicsclaw.runtime.consensus.scoring import top_k_by_score
from omicsclaw.runtime.consensus.source_registry import ConsensusSource
from omicsclaw.runtime.workflow import (
    DEFAULT_TIMEOUT_SECONDS,
    FanOutResult,
    InsufficientSurvivorsError,
    StepRunResult,
    fan_out,
)

logger = logging.getLogger(__name__)

BCSelectorFn = Callable[[Sequence[ContinuousMemberScore], int], list[str]]


@dataclass(frozen=True)
class ContinuousConsensusRun:
    """Complete result of one ``run_continuous_consensus`` invocation."""

    run_id: str
    operator: str
    members: tuple[ConsensusMember, ...]
    team_result: FanOutResult
    pseudotime_df: pd.DataFrame        # raw per-member pseudotime (non-degenerate)
    aligned_df: pd.DataFrame           # rank-normalised + direction-aligned
    scores: tuple[ContinuousMemberScore, ...]
    agreement_matrix: pd.DataFrame     # pairwise Spearman over aligned members
    anchor: str
    flipped_members: tuple[str, ...]
    dropped_degenerate: tuple[str, ...]
    selected_bcs: tuple[str, ...]
    consensus: ContinuousConsensusResult
    weak_agreement: dict[str, Any]
    output_dir: Path
    artifacts_written: tuple[Path, ...]
    missing_members: tuple[str, ...] = field(default_factory=tuple)
    score_config: ScoreConfig = field(default_factory=ScoreConfig)
    top_k: int = 4


def _gather_pseudotime(
    survivors: Sequence[StepRunResult], source: ConsensusSource
) -> tuple[pd.DataFrame, list[str]]:
    """Pull each member's canonical per-cell ``pseudotime`` vector through the reader."""
    columns: dict[str, pd.Series] = {}
    missing: list[str] = []
    for r in survivors:
        output_root = r.output_dir.parent
        series = source.reader.read_labels(r.step, output_root)
        if series is None:
            missing.append(r.step.name)
            continue
        columns[r.step.name] = pd.to_numeric(series, errors="coerce")
    if not columns:
        return pd.DataFrame(), missing
    df = pd.concat(columns, axis=1).dropna(axis=0, how="any")
    return df, missing


def _annotate_selection(
    scores: Sequence[ContinuousMemberScore], voting: Sequence[str]
) -> tuple[ContinuousMemberScore, ...]:
    voting_set = set(voting)
    out: list[ContinuousMemberScore] = []
    for s in scores:
        if s.filtered:
            sel, reason = False, "filtered"
        elif s.member in voting_set:
            sel, reason = True, "passed"
        else:
            sel, reason = False, "below_top_k"
        out.append(replace(s, selected=sel, selection_reason=reason))
    return tuple(out)


async def run_continuous_consensus(
    *,
    members: Sequence[ConsensusMember],
    source: ConsensusSource,
    input_path: str,
    output_dir: Path | str,
    operator: str,
    bc_selector: BCSelectorFn,
    top_k_default: int = 4,
    score_config: ScoreConfig = ScoreConfig(),
    seed: int = 0,
    plan_audit: Mapping[str, Any] | None = None,
    cancel_event: threading.Event | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_parallel: int | None = None,
    # Accepted for driver-call symmetry with run_typed_consensus; not used by the
    # continuous template (no spatial/integration panel, no diagnostic baseline).
    use_spatial_panel: bool = True,  # noqa: ARG001
    panel_weights: Mapping[str, float] | None = None,  # noqa: ARG001
    batch_key: str = "batch",  # noqa: ARG001
    non_voting_members: Sequence[str] = (),  # noqa: ARG001
    runner: Any = None,
) -> ContinuousConsensusRun:
    """Orchestrate one continuous (rank-gauge) consensus run end-to-end.

    v1 is agreement-only: the composite is mean pairwise Spearman; α/β are forced
    to ``1.0 / 0.0`` regardless of the passed ``score_config`` (recorded in
    ``plan.json``). ``operator`` must be ``"median"`` or ``"weighted"``.

    Raises ``InsufficientSurvivorsError`` (< 2 members with a usable pseudotime,
    incl. after dropping degenerate ones) or ``InsufficientBCsError`` (< 2 voters).
    """
    if operator not in ("median", "weighted"):
        raise ValueError(f"continuous operator must be 'median' or 'weighted', got {operator!r}")
    output_dir_p = Path(output_dir)
    output_dir_p.mkdir(parents=True, exist_ok=True)
    artifacts: list[Path] = []
    # v1 is agreement-only: α=1, β=0 regardless of the passed score_config
    # (intrinsic_panel="" skips any panel, but BETA_DEFAULT is 0.4 — force it).
    eff_config = ScoreConfig(alpha=1.0, beta=0.0, max_class_frac_cap=score_config.max_class_frac_cap)

    # 1. plan.json audit BEFORE fan-out (survives early failure).
    if plan_audit is not None:
        audit = dict(plan_audit)
        audit["input_path"] = str(Path(input_path).resolve())
        audit["operator"] = operator
        audit["alpha"] = eff_config.alpha
        audit["beta"] = eff_config.beta
        audit["top_k"] = top_k_default
        audit["intrinsic_panel"] = "none"
        audit["template"] = "continuous"
        plan_path = output_dir_p / "plan.json"
        plan_path.write_text(json.dumps(audit, indent=2))
        artifacts.append(plan_path)

    run_id = str((plan_audit or {}).get("run_id") or output_dir_p.name)

    # 2. fan-out (L2 supplies the survivor minimum to the neutral L1).
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

    # 3. gather per-member pseudotime; drop degenerate members; fail loud if < 2.
    raw_df, missing = _gather_pseudotime(team.survived, source)
    if raw_df.shape[1] < MIN_CONSENSUS_MEMBERS:
        raise InsufficientSurvivorsError(
            f"Only {raw_df.shape[1]} member(s) produced a readable pseudotime "
            f"(< {MIN_CONSENSUS_MEMBERS} required). Missing artifacts: {missing}"
        )
    good: list[str] = []
    dropped: list[str] = []
    for col in raw_df.columns:
        (dropped if is_degenerate(raw_df[col].to_numpy(dtype=float)) else good).append(col)
    if dropped:
        logger.warning(
            "dropping degenerate pseudotime member(s) %s (constant / <2 unique / "
            "non-finite — Spearman is undefined there).", dropped,
        )
    if len(good) < MIN_CONSENSUS_MEMBERS:
        raise InsufficientSurvivorsError(
            f"Only {len(good)} member(s) had a non-degenerate pseudotime "
            f"(< {MIN_CONSENSUS_MEMBERS} required); dropped {dropped}, missing {missing}."
        )
    pseudotime_df = raw_df[good]

    # 4. rank-normalise + direction safeguard + pairwise Spearman.
    rank_by_member = {m: rank_normalize(pseudotime_df[m].to_numpy()) for m in pseudotime_df.columns}
    aligned, anchor, flipped = align_directions(rank_by_member)
    aligned_df = pd.DataFrame(aligned, index=pseudotime_df.index)
    if flipped:
        logger.warning(
            "direction safeguard flipped member(s) %s (anti-correlated with anchor %r).",
            flipped, anchor,
        )
    agreement = pairwise_spearman(aligned)
    agreement_path = output_dir_p / "member_agreement_spearman.csv"
    agreement.round(6).to_csv(agreement_path)
    artifacts.append(agreement_path)

    # 5. score (mean pairwise Spearman; α=1, β=0).
    scores = score_continuous_members(agreement)

    # 6. BC selection (top-K by agreement) → voters; stamp + persist.
    selected = bc_selector(scores, top_k_default)
    voting = [m for m in selected if m in aligned_df.columns]
    scores = _annotate_selection(scores, voting)
    scores_path = output_dir_p / "member_scores.csv"
    pd.DataFrame([asdict(s) for s in scores]).to_csv(scores_path, index=False)
    artifacts.append(scores_path)
    audit_path = output_dir_p / "selection_audit.json"
    audit_path.write_text(json.dumps({
        "top_k_candidates": list(selected),
        "voting_bcs": list(voting),
        "anchor": anchor,
        "flipped_members": list(flipped),
        "dropped_degenerate": list(dropped),
    }, indent=2))
    artifacts.append(audit_path)
    if len(voting) < MIN_CONSENSUS_MEMBERS:
        raise InsufficientBCsError(
            f"BC selector returned {len(voting)} voting member(s) (< {MIN_CONSENSUS_MEMBERS} "
            f"required). Selected: {selected}."
        )

    # 7. weak-agreement guard over the VOTERS (report-only, ADR 0031 §9).
    weak = weak_agreement_stats(agreement.loc[voting, voting])
    if weak["diverged"]:
        logger.warning(
            "weak agreement: voters' mean pairwise Spearman %.3f < %.2f — methods "
            "disagree on the ordering; a single consensus pseudotime may be ill-posed "
            "(no shared trajectory / multiple lineages). worst pair %s ρ=%.3f.",
            weak["cohort_mean_spearman"], weak["threshold"],
            weak["min_pair"], weak["min_pairwise_spearman"],
        )

    # 8. operator (median / weighted) over the voters' aligned ranks.
    if operator == "weighted":
        score_lookup = {s.member: s.composite for s in scores}
        consensus = weighted_consensus(
            aligned_df[voting], weights={m: score_lookup.get(m, 0.0) for m in voting}, seed=seed,
        )
    else:
        consensus = median_consensus(aligned_df[voting], seed=seed)

    # 9. consensus pseudotime + per-cell dispersion artifact.
    consensus_path = output_dir_p / "consensus_pseudotime.tsv"
    pd.DataFrame({
        "observation": consensus.pseudotime.index,
        "consensus_pseudotime": consensus.pseudotime.to_numpy(),
        "pseudotime_mad": consensus.pseudotime_mad.to_numpy(),
        "range": consensus.value_range.to_numpy(),
    }).to_csv(consensus_path, sep="\t", index=False)
    artifacts.append(consensus_path)

    return ContinuousConsensusRun(
        run_id=run_id,
        operator=operator,
        members=tuple(members),
        team_result=team,
        pseudotime_df=pseudotime_df,
        aligned_df=aligned_df,
        scores=tuple(scores),
        agreement_matrix=agreement,
        anchor=anchor,
        flipped_members=tuple(flipped),
        dropped_degenerate=tuple(dropped),
        selected_bcs=tuple(voting),
        consensus=consensus,
        weak_agreement=weak,
        output_dir=output_dir_p,
        artifacts_written=tuple(artifacts),
        missing_members=tuple(missing),
        score_config=eff_config,
        top_k=top_k_default,
    )
