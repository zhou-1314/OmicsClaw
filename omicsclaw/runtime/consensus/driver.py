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
import logging
import threading
from dataclasses import asdict, dataclass, field, replace
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
from omicsclaw.runtime.consensus.explain import (
    per_spot_confidence,
    render_nmi_heatmap,
)
from omicsclaw.runtime.consensus.spatial_panel import (
    PANEL_METRICS as SPATIAL_PANEL_METRICS,
    intrinsic_spatial_panel,
)
from omicsclaw.runtime.consensus.integration_panel import (
    PANEL_METRICS as INTEGRATION_PANEL_METRICS,
    intrinsic_integration_panel,
)
from omicsclaw.runtime.workflow import (
    DEFAULT_TIMEOUT_SECONDS,
    FanOutResult,
    InsufficientSurvivorsError,
    StepRunResult,
    fan_out,
)

logger = logging.getLogger(__name__)

OperatorName = Literal["kmode", "weighted", "lca"]
BCSelectorFn = Callable[[list[MemberScore], int], list[str]]

# Consensus needs at least two members to merge. This survivor minimum is L2's
# policy, supplied explicitly to the neutral L1 fan-out so a sub-threshold run
# raises InsufficientSurvivorsError with the full per-member failure summary
# (crash/timeout details) — diagnostics the readable-label gate alone can't give.
MIN_CONSENSUS_MEMBERS = 2

# kmode/weighted Hungarian alignment + majority vote is only well-posed when
# member cluster counts are comparable. When the largest member k exceeds the
# smallest by more than this ratio the driver warns and records it (ADR 0029):
# disagreement may then be operator-induced (a fine partition folded into a
# coarse one) rather than biological. v1 reports + warns; it does not downweight.
K_DIVERGENCE_RATIO = 2.0


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
    #: Per-member raw spatial-panel metrics (chaos/pas/mlami) when the spatial
    #: intrinsic panel ran; empty for non-spatial runs or when disabled.
    intrinsic_panel_raw: dict[str, dict[str, float]] = field(default_factory=dict)
    #: ADR 0011 scoring config (alpha/beta/max-class cap) actually used.
    score_config: "ScoreConfig" = field(default_factory=ScoreConfig)
    #: Top-K used for BC selection.
    top_k: int = 4
    #: Per-observation consensus confidence (support/entropy/n_members).
    confidence: pd.DataFrame = field(default_factory=pd.DataFrame)
    #: Path to the cross-method NMI heatmap PNG, if it was rendered.
    nmi_heatmap_path: Path | None = None
    #: Per-member cluster counts + spread (k-divergence guard, ADR 0029):
    #: ``{"k_by_member": {...}, "k_min", "k_max", "k_cv", "diverged": bool}``.
    k_stats: dict[str, Any] = field(default_factory=dict)


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


def _load_spatial_coords(
    input_path: str, obs_index: pd.Index
) -> np.ndarray | None:
    """Spatial coordinates for ``obs_index`` from the shared input AnnData.

    Reads ``obsm['spatial']`` from ``input_path`` and reorders it to match the
    gathered ``labels_df`` index (observation ids). Returns ``None`` — so the
    caller falls back to the reader's single intrinsic signal — when the input
    is unreadable, carries no spatial coordinates, or its observations don't
    cover ``obs_index``. A non-spatial flavour's AnnData (e.g. sc-clustering)
    has no ``obsm['spatial']``, so this coords-presence check is what scopes the
    spatial panel to spatial flavours without the driver knowing about domains.
    """
    if not input_path:
        return None
    try:
        import anndata

        adata = anndata.read_h5ad(input_path)
    except Exception:  # noqa: BLE001 — missing/unreadable input -> fall back
        return None
    if "spatial" not in getattr(adata, "obsm", {}):
        return None
    coords_all = np.asarray(adata.obsm["spatial"])
    if coords_all.ndim != 2 or coords_all.shape[0] != adata.n_obs:
        return None
    position = {str(name): i for i, name in enumerate(adata.obs_names)}
    rows: list[int] = []
    for obs in obs_index:
        i = position.get(str(obs))
        if i is None:
            # The input HAS spatial coordinates but its observation ids don't
            # cover the gathered labels — a real misconfiguration, not a
            # non-spatial flavour. Warn (the run still proceeds on the reader's
            # single intrinsic) rather than silently dropping the panel.
            logger.warning(
                "spatial coordinates present but observation id %r is missing "
                "from the input AnnData; skipping the spatial intrinsic panel "
                "and using the reader's single intrinsic signal.",
                str(obs),
            )
            return None
        rows.append(i)
    return coords_all[rows][:, :2]


def _reindex_rows(values: np.ndarray, obs_names: Sequence[Any], obs_index: pd.Index) -> np.ndarray | None:
    """Reorder ``values`` (row i ↔ obs_names[i]) to ``obs_index``; ``None`` if any id is missing."""
    position = {str(name): i for i, name in enumerate(obs_names)}
    rows: list[int] = []
    for obs in obs_index:
        i = position.get(str(obs))
        if i is None:
            return None
        rows.append(i)
    return np.asarray(values)[rows]


def _load_integration_panel_inputs(
    member_dirs: Mapping[str, Path], batch_key: str, obs_index: pd.Index
) -> tuple[dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]], int]:
    """Per-member ``(embedding, X_pca, batch_labels)`` from each member's output.

    Integration happens *inside* each member subprocess, so the representation
    (``X_harmony``/``X_scvi``/...) lives only in that member's ``processed.h5ad``,
    not the shared input. Each member's ``result.json`` records the
    ``representation_used`` obsm key it clustered on. Members whose artifacts are
    missing/unreadable, or whose observation ids don't cover ``obs_index``, are
    skipped fail-soft (they keep the reader's intrinsic). Returns the per-member
    inputs plus the max batch count seen (0 if none loaded).
    """
    import json

    import anndata

    out: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    max_batches = 0
    for member, member_dir in member_dirs.items():
        try:
            summary = json.loads((Path(member_dir) / "result.json").read_text()).get("summary", {})
            rep_key = str(summary.get("representation_used") or "X_pca")
            adata = anndata.read_h5ad(Path(member_dir) / "processed.h5ad")
            if rep_key not in adata.obsm or "X_pca" not in adata.obsm:
                continue
            if batch_key not in adata.obs.columns:
                continue
            embedding = _reindex_rows(np.asarray(adata.obsm[rep_key]), adata.obs_names, obs_index)
            x_pca = _reindex_rows(np.asarray(adata.obsm["X_pca"]), adata.obs_names, obs_index)
            batch = _reindex_rows(
                adata.obs[batch_key].astype(str).to_numpy(), adata.obs_names, obs_index
            )
            if embedding is None or x_pca is None or batch is None:
                continue
            out[member] = (embedding, x_pca, batch)
            max_batches = max(max_batches, int(np.unique(batch).size))
        except Exception:  # noqa: BLE001 — fail-soft: member keeps reader intrinsic
            logger.warning("integration panel: could not load embedding for member %r", member)
            continue
    return out, max_batches


def _write_intrinsic_panel_csv(
    path: Path,
    members: Sequence[str],
    panel_raw: Mapping[str, Mapping[str, float]],
    panel_scalar: Mapping[str, float],
    metric_cols: Sequence[str],
) -> None:
    """Write ``member_intrinsic_panel.csv`` (one row per member, stable metric columns)."""
    pd.DataFrame(
        [
            {
                "member": m,
                **{mc: panel_raw.get(m, {}).get(mc, float("nan")) for mc in metric_cols},
                "intrinsic_panel": panel_scalar[m],
            }
            for m in members
            if m in panel_scalar
        ]
    ).to_csv(path, index=False)


def _compute_k_stats(labels_df: pd.DataFrame) -> dict[str, Any]:
    """Per-member cluster counts + spread for the k-divergence guard (ADR 0029)."""
    k_by_member = {col: int(pd.Series(labels_df[col]).nunique()) for col in labels_df.columns}
    ks = np.asarray(list(k_by_member.values()), dtype=float)
    k_min, k_max = int(ks.min()), int(ks.max())
    k_mean = float(ks.mean())
    k_cv = float(ks.std() / k_mean) if k_mean > 0 else 0.0
    diverged = k_min > 0 and (k_max / k_min) > K_DIVERGENCE_RATIO
    return {
        "k_by_member": k_by_member,
        "k_min": k_min,
        "k_max": k_max,
        "k_cv": k_cv,
        "diverged": diverged,
    }


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


def _annotate_selection(
    scores: Sequence[MemberScore], selected: Sequence[str]
) -> tuple[MemberScore, ...]:
    """Stamp each score with whether/why it entered consensus (explainability).

    ``selection_reason`` is one of: ``"passed"`` (entered consensus),
    ``"below_top_k"`` (scored, not picked), or ``"filtered (max_class_fraction
    > cap)"`` (hard-filtered before selection).
    """
    selected_set = set(selected)
    annotated: list[MemberScore] = []
    for s in scores:
        if s.filtered:
            sel, reason = False, "filtered (max_class_fraction > cap)"
        elif s.member in selected_set:
            sel, reason = True, "passed"
        else:
            sel, reason = False, "below_top_k"
        annotated.append(replace(s, selected=sel, selection_reason=reason))
    return tuple(annotated)


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
    use_spatial_panel: bool = True,
    panel_weights: Mapping[str, float] | None = None,
    batch_key: str = "batch",
    runner: Any = None,
) -> TypedConsensusRun:
    """Orchestrate one typed-consensus run end-to-end.

    When ``use_spatial_panel`` is set (default) and the input AnnData carries
    spatial coordinates, the member intrinsic-quality signal fed to BC scoring
    is a normalised multi-metric spatial panel (chaos/pas/mlami) instead of the
    reader's single value; non-spatial inputs fall back to the reader signal.

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
        # Record the scoring thresholds actually used (authoritative from the
        # driver, never hidden) so plan.json + report stay truthful — the
        # max-class-fraction hard filter in particular (ADR 0011).
        audit["operator"] = operator
        audit["alpha"] = score_config.alpha
        audit["beta"] = score_config.beta
        audit["max_class_fraction_cap"] = score_config.max_class_frac_cap
        audit["top_k"] = top_k_default
        # Panel-based rankings depend on the intrinsic-panel family + its weights
        # (ADR 0028/0029); record the EFFECTIVE values so the run is reproducible
        # from plan.json — especially for API callers passing non-default
        # ``panel_weights``. ``"none"`` / ``{}`` when no panel is active.
        _panel_kind = getattr(source, "intrinsic_panel", "")
        if use_spatial_panel and _panel_kind in ("spatial", "integration"):
            if _panel_kind == "spatial":
                from omicsclaw.runtime.consensus.spatial_panel import (
                    DEFAULT_PANEL_WEIGHTS as _panel_default_weights,
                )
            else:
                from omicsclaw.runtime.consensus.integration_panel import (
                    DEFAULT_PANEL_WEIGHTS as _panel_default_weights,
                )
            audit["intrinsic_panel"] = _panel_kind
            audit["panel_weights"] = dict(panel_weights or _panel_default_weights)
        else:
            audit["intrinsic_panel"] = "none"
            audit["panel_weights"] = {}
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

    # 3b. driver-computed intrinsic panel (optional)
    #
    # Replace the reader's single intrinsic signal with a normalised multi-metric
    # panel so member quality is judged from several angles. The panel family is
    # dispatched on the *flavour's declared* ``intrinsic_panel`` — not a domain or
    # coords-presence check — so adding a family is one ConsensusSource field:
    #   - "spatial"     chaos/pas/mlami on shared-input spatial coords (ADR 0028)
    #   - "integration" iLISI / within-batch kNN preservation on each member's own
    #                   embedding + the batch key (ADR 0029)
    intrinsic_panel_raw: dict[str, dict[str, float]] = {}
    panel_kind = getattr(source, "intrinsic_panel", "")
    if use_spatial_panel and panel_kind == "spatial":
        coords = _load_spatial_coords(input_path, labels_df.index)
        if coords is not None:
            panel_intrinsic: dict[str, float] = {}
            for col in labels_df.columns:
                scalar, raw = intrinsic_spatial_panel(
                    labels_df[col].to_numpy(), coords,
                    weights=panel_weights, seed=seed,
                )
                panel_intrinsic[col] = scalar
                intrinsic_panel_raw[col] = raw
            intrinsic_map = panel_intrinsic
            panel_path = output_dir_p / "member_intrinsic_panel.csv"
            _write_intrinsic_panel_csv(
                panel_path, list(labels_df.columns), intrinsic_panel_raw,
                panel_intrinsic, list(SPATIAL_PANEL_METRICS),
            )
            artifacts.append(panel_path)
    elif use_spatial_panel and panel_kind == "integration":
        member_dirs = {r.step.name: r.output_dir for r in team.survived}
        panel_inputs, n_batches = _load_integration_panel_inputs(
            member_dirs, batch_key, labels_df.index
        )
        if n_batches < 2:
            logger.warning(
                "integration panel: input has %d batch(es) on obs[%r] — batch-mixing "
                "is undefined; scoring on within-batch structure only.",
                n_batches, batch_key,
            )
        panel_intrinsic = {}
        for col in labels_df.columns:
            pi = panel_inputs.get(col)
            if pi is None:
                continue  # member keeps the reader's intrinsic (fail-soft)
            embedding, x_pca, batch_labels = pi
            scalar, raw = intrinsic_integration_panel(
                labels_df[col].to_numpy(), embedding, batch_labels, x_pca,
                weights=panel_weights, seed=seed,
            )
            panel_intrinsic[col] = scalar
            intrinsic_panel_raw[col] = raw
        if panel_intrinsic:
            # Override only the members we computed; any skipped member retains
            # its reader intrinsic so a partial panel never zeroes a member.
            intrinsic_map = {**intrinsic_map, **panel_intrinsic}
            panel_path = output_dir_p / "member_intrinsic_panel.csv"
            _write_intrinsic_panel_csv(
                panel_path, list(labels_df.columns), intrinsic_panel_raw,
                panel_intrinsic, list(INTEGRATION_PANEL_METRICS),
            )
            artifacts.append(panel_path)

    # k-divergence guard: record per-member cluster counts + spread; warn loudly
    # when k diverges (operator-induced disagreement risk, ADR 0029).
    k_stats = _compute_k_stats(labels_df)
    if k_stats["diverged"]:
        logger.warning(
            "member cluster counts diverge (k_min=%d, k_max=%d, ratio>%.1f): kmode/"
            "weighted alignment may induce disagreement; treat per-spot support with "
            "care. k_by_member=%s",
            k_stats["k_min"], k_stats["k_max"], K_DIVERGENCE_RATIO, k_stats["k_by_member"],
        )

    # 4. score
    labels_arrays = {col: labels_df[col].to_numpy() for col in labels_df.columns}
    scores = score_all_members(
        labels_arrays,
        intrinsic_map,
        alpha=score_config.alpha,
        beta=score_config.beta,
        max_class_frac_cap=score_config.max_class_frac_cap,
    )

    # 5. cross-method NMI + persist (+ optional heatmap figure)
    nmi_df = _cross_method_nmi_matrix(labels_df)
    nmi_path = output_dir_p / "cross_method_nmi.csv"
    nmi_df.to_csv(nmi_path)
    artifacts.append(nmi_path)
    nmi_heatmap_path = render_nmi_heatmap(nmi_df, output_dir_p / "cross_method_nmi.png")
    if nmi_heatmap_path is not None:
        artifacts.append(nmi_heatmap_path)

    # 6. BC selection; stamp every member with selected/why, then persist the
    #    score table. Written before the gate so a failed selection is still
    #    auditable — every member carries its selection reason.
    selected = bc_selector(scores, top_k_default)
    scores = _annotate_selection(scores, selected)
    scores_path = output_dir_p / "member_scores.csv"
    pd.DataFrame([asdict(s) for s in scores]).to_csv(scores_path, index=False)
    artifacts.append(scores_path)
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

    # 8. per-spot confidence + consensus labels TSV (label + agreement columns)
    confidence = per_spot_confidence(consensus.aligned_labels)
    consensus_path = output_dir_p / "consensus_labels.tsv"
    pd.DataFrame(
        {
            "observation": consensus.labels.index,
            f"consensus_{operator}": consensus.labels.values,
            "support": confidence["support"].to_numpy(),
            "entropy": confidence["entropy"].to_numpy(),
            "n_members": confidence["n_members"].to_numpy(),
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
        intrinsic_panel_raw=intrinsic_panel_raw,
        score_config=score_config,
        top_k=top_k_default,
        confidence=confidence,
        nmi_heatmap_path=nmi_heatmap_path,
        k_stats=k_stats,
    )
