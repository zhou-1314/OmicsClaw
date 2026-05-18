"""sc-consensus-clustering — multi-resolution typed consensus over sc-clustering."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from omicsclaw.runtime.consensus.dispatch import output_banner
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
    score_all_members,
    top_k_by_score,
)
from omicsclaw.runtime.consensus.team import (
    DEFAULT_TIMEOUT_SECONDS,
    InsufficientSurvivorsError,
    MemberRunResult,
    TeamRunResult,
    run_team,
)

logger = logging.getLogger("sc-consensus-clustering")

ARTIFACT_RELPATH = "figure_data/embedding_points.csv"
DEFAULT_RESOLUTIONS = "0.5,0.8,1.0,1.4,2.0"
DEFAULT_METHODS = "leiden"


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Multi-resolution consensus over sc-clustering (ADR 0010 typed A path)."
    )
    parser.add_argument("--input", required=False, help="Preprocessed scRNA AnnData (.h5ad)")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument(
        "--members",
        default=None,
        help="Explicit member list, e.g. leiden:resolution=0.5,louvain:resolution=1.0",
    )
    parser.add_argument(
        "--resolutions",
        default=DEFAULT_RESOLUTIONS,
        help="Comma-separated resolution values for the sweep (default 0.5,0.8,1.0,1.4,2.0).",
    )
    parser.add_argument(
        "--cluster-methods",
        default=DEFAULT_METHODS,
        help="Comma-separated cluster methods to sweep (default 'leiden').",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Sweep BOTH leiden and louvain at every default resolution (10 members).",
    )
    parser.add_argument("--n-clusters", type=int, default=None)
    parser.add_argument("--confirm-plan", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--alpha", type=float, default=ALPHA_DEFAULT)
    parser.add_argument("--beta", type=float, default=BETA_DEFAULT)
    parser.add_argument("--max-class-frac", type=float, default=MAX_CLASS_FRAC_CAP_DEFAULT)
    parser.add_argument("--llm-judge", action="store_true")
    parser.add_argument(
        "--operator", choices=["kmode", "weighted", "lca"], default="kmode"
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-parallel", type=int, default=None)
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument("--run-id", default=None)
    return parser


# --------------------------------------------------------------------------- #
# Member planning                                                              #
# --------------------------------------------------------------------------- #

def _members_from_explicit(spec: str) -> list[ConsensusMember]:
    out: list[ConsensusMember] = []
    seen: set[str] = set()
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if ":" not in token:
            raise SystemExit(f"Invalid member '{token}'; expected '<method>:resolution=<float>'")
        method, params_spec = token.split(":", 1)
        params: dict[str, str] = {"cluster-method": method}
        for kv in params_spec.split(";"):
            if "=" not in kv:
                continue
            k, v = kv.split("=", 1)
            params[k.strip()] = v.strip()
        suffix = "_".join(f"{k}-{v}" for k, v in sorted(params.items()) if k != "cluster-method")
        name = f"{method}_{suffix}" if suffix else method
        if name in seen:
            raise SystemExit(f"duplicate member name '{name}'")
        seen.add(name)
        out.append(
            ConsensusMember(
                name=name,
                skill_name="sc-clustering",
                params=params,
                intrinsic_quality_path="",  # we read CSV separately for sc
                artifact_relpath=ARTIFACT_RELPATH,
                label_column=method,
            )
        )
    return out


def _members_from_sweep(methods: list[str], resolutions: list[float]) -> list[ConsensusMember]:
    out: list[ConsensusMember] = []
    for method in methods:
        for r in resolutions:
            r_str = str(r)
            name = f"{method}_resolution-{r_str}"
            out.append(
                ConsensusMember(
                    name=name,
                    skill_name="sc-clustering",
                    params={"cluster-method": method, "resolution": r_str},
                    intrinsic_quality_path="",
                    artifact_relpath=ARTIFACT_RELPATH,
                    label_column=method,
                )
            )
    return out


def _plan_members(args: argparse.Namespace) -> list[ConsensusMember]:
    if args.members:
        return _members_from_explicit(args.members)
    if args.all:
        methods = ["leiden", "louvain"]
        resolutions = [float(r) for r in DEFAULT_RESOLUTIONS.split(",")]
    else:
        methods = [m.strip() for m in args.cluster_methods.split(",") if m.strip()]
        resolutions = [float(r) for r in args.resolutions.split(",")]
    return _members_from_sweep(methods, resolutions)


# --------------------------------------------------------------------------- #
# Post fan-out                                                                 #
# --------------------------------------------------------------------------- #

def _load_member_labels(result: MemberRunResult) -> pd.Series | None:
    csv_path = result.output_dir / ARTIFACT_RELPATH
    if not csv_path.exists():
        candidates = list(result.output_dir.glob("figure_data/embedding_points*.csv"))
        if not candidates:
            return None
        csv_path = candidates[0]
    df = pd.read_csv(csv_path)
    if "cell_id" not in df.columns:
        return None
    label_col = result.member.label_column
    if label_col not in df.columns:
        # sc-clustering may name the column after the cluster_method (default
        # "leiden") even when the member intended another label. Fall back to
        # the rightmost non-coordinate column.
        candidate_cols = [c for c in df.columns if c not in {"cell_id", "embedding_key", "coord1", "coord2"}]
        if not candidate_cols:
            return None
        label_col = candidate_cols[-1]
    return df.set_index("cell_id")[label_col].astype(str)


def _load_intrinsic_quality(result: MemberRunResult) -> float:
    """Read silhouette from sc-clustering's ``clustering_summary.csv``."""
    csv_path = result.output_dir / "figure_data" / "clustering_summary.csv"
    if not csv_path.exists():
        return 0.0
    try:
        df = pd.read_csv(csv_path)
    except Exception:  # noqa: BLE001
        return 0.0
    if "metric" not in df.columns or "value" not in df.columns:
        return 0.0
    row = df.loc[df["metric"] == "silhouette_score"]
    if row.empty:
        return 0.0
    try:
        return float(row.iloc[0]["value"])
    except (TypeError, ValueError):
        return 0.0


def _gather_labels(survivors) -> tuple[pd.DataFrame, dict[str, float], list[str]]:
    columns: dict[str, pd.Series] = {}
    intrinsic: dict[str, float] = {}
    missing: list[str] = []
    for r in survivors:
        labels = _load_member_labels(r)
        if labels is None:
            missing.append(r.member.name)
            continue
        columns[r.member.name] = labels
        intrinsic[r.member.name] = _load_intrinsic_quality(r)
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


def _pick_bcs(scores, *, non_interactive: bool, top_k: int) -> list[str]:
    default_pick = top_k_by_score(scores, k=top_k)
    if non_interactive or not sys.stdin.isatty():
        return default_pick
    print(
        f"[expert] base clusterings (default = top-{top_k} by score: "
        f"{','.join(default_pick) or '(none)'})"
    )
    raw = input("       enter comma list, 'all', or press Enter for default: ").strip()
    if not raw:
        return default_pick
    if raw.lower() == "all":
        return [s.member for s in scores if not s.filtered]
    valid = {s.member for s in scores if not s.filtered}
    requested = [r.strip() for r in raw.split(",") if r.strip()]
    return [m for m in requested if m in valid] or default_pick


def _run_operator(operator: str, labels_df: pd.DataFrame, seed: int, scores_lookup: dict[str, float]) -> ConsensusResult:
    if operator == "kmode":
        return kmode_consensus(labels_df, seed=seed)
    if operator == "weighted":
        weights = {c: max(scores_lookup.get(c, 0.0), 1e-6) for c in labels_df.columns}
        return weighted_consensus(labels_df, weights=weights, seed=seed)
    if operator == "lca":
        from omicsclaw.runtime.consensus.operators.lca_r import lca_consensus

        return lca_consensus(labels_df, seed=seed)
    raise ValueError(f"unknown operator: {operator}")


def _write_report(
    output_dir: Path,
    *,
    operator: str,
    consensus: ConsensusResult,
    scores,
    selected: list[str],
    n_failed: int,
    n_total: int,
    nmi_df: pd.DataFrame,
) -> Path:
    banner = output_banner("typed")
    lines = [
        banner,
        "",
        f"# Verified consensus — sc clustering ({operator})",
        "",
        f"- members fanned out: **{n_total}**",
        f"- members surviving: **{n_total - n_failed}**",
        f"- members entering consensus (BC): **{len(selected)}**",
        f"- consensus clusters returned: **{consensus.n_clusters_returned}**",
        f"- operator: `{operator}` (seed={consensus.seed})",
        "",
        "## Base clusterings",
        "",
        "| member | composite | cross_NMI | intrinsic (silhouette) | max_class_frac | filtered |",
        "|---|---|---|---|---|---|",
    ]
    for s in scores:
        chk = "✓" if (s.member in selected) else ""
        lines.append(
            f"| {s.member} {chk} | {s.composite:.4f} | {s.cross_nmi_mean:.4f} | "
            f"{s.intrinsic:.4f} | {s.max_class_frac:.3f} | "
            f"{'yes (' + (s.filter_reason or '') + ')' if s.filtered else 'no'} |"
        )
    lines += [
        "",
        "## Cross-method NMI matrix",
        "",
        "```",
        nmi_df.round(3).to_string(),
        "```",
        "",
        "_Audit_: A path; namespace `analysis://typed/<run_id>`. Banner is enforced by ADR 0010.",
    ]
    p = output_dir / "report.md"
    p.write_text("\n".join(lines))
    return p


# --------------------------------------------------------------------------- #
# Entry                                                                        #
# --------------------------------------------------------------------------- #

async def _main_async(args: argparse.Namespace) -> int:
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    members = _plan_members(args)
    if not members:
        print("[sc-consensus-clustering] no members planned.")
        return 2

    plan_audit = {
        "run_id": args.run_id or output_dir.name,
        "operator": args.operator,
        "members": [{"name": m.name, "params": dict(m.params)} for m in members],
        "alpha": args.alpha,
        "beta": args.beta,
        "max_class_frac": args.max_class_frac,
    }
    (output_dir / "plan.json").write_text(json.dumps(plan_audit, indent=2))

    try:
        team: TeamRunResult = await run_team(
            members,
            input_path=args.input or "",
            output_root=output_dir,
            timeout_seconds=args.timeout,
            max_parallel=args.max_parallel,
        )
    except InsufficientSurvivorsError as exc:
        print(f"[sc-consensus-clustering] FATAL: {exc}", file=sys.stderr)
        return 3

    labels_df, intrinsic_map, missing = _gather_labels(team.survived)
    if labels_df.shape[1] < 2:
        print(
            f"[sc-consensus-clustering] FATAL: insufficient label artifacts (missing: {missing}).",
            file=sys.stderr,
        )
        return 4

    labels_arrays = {col: labels_df[col].to_numpy() for col in labels_df.columns}
    scores = score_all_members(
        labels_arrays,
        intrinsic_map,
        alpha=args.alpha,
        beta=args.beta,
        max_class_frac_cap=args.max_class_frac,
    )
    pd.DataFrame([asdict(s) for s in scores]).to_csv(output_dir / "member_scores.csv", index=False)
    nmi_df = _cross_method_nmi_matrix(labels_df)
    nmi_df.to_csv(output_dir / "cross_method_nmi.csv")

    selected = _pick_bcs(scores, non_interactive=args.non_interactive, top_k=args.top_k)
    if len(selected) < 2:
        print(
            f"[sc-consensus-clustering] FATAL: <2 base clusterings selected (got {selected}).",
            file=sys.stderr,
        )
        return 5

    scores_lookup = {s.member: s.composite for s in scores}
    try:
        consensus = _run_operator(args.operator, labels_df[selected], args.seed, scores_lookup)
    except Exception as exc:  # noqa: BLE001
        from omicsclaw.runtime.consensus.operators.lca_r import LCAUnavailableError

        if isinstance(exc, LCAUnavailableError):
            print(
                f"[sc-consensus-clustering] FATAL: LCA operator unavailable: {exc}\n"
                "Pass --operator kmode or --operator weighted to bypass.",
                file=sys.stderr,
            )
            return 6
        print(
            f"[sc-consensus-clustering] FATAL: {args.operator} operator failed: {exc}",
            file=sys.stderr,
        )
        return 7

    pd.DataFrame(
        {
            "cell_id": consensus.labels.index,
            f"consensus_{args.operator}": consensus.labels.values,
        }
    ).to_csv(output_dir / "consensus_labels.tsv", sep="\t", index=False)

    _write_report(
        output_dir,
        operator=args.operator,
        consensus=consensus,
        scores=scores,
        selected=selected,
        n_failed=team.n_failed,
        n_total=team.total,
        nmi_df=nmi_df,
    )
    print(
        f"[sc-consensus-clustering] OK: {team.n_survived}/{team.total} members survived; "
        f"{len(selected)} entered consensus; operator={args.operator}; "
        f"clusters_returned={consensus.n_clusters_returned}."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=os.environ.get("OMICSCLAW_LOG_LEVEL", "INFO"))
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
