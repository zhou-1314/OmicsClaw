"""consensus-domains — multi-method consensus over spatial-domains.

Thin CLI wrapper around ``omicsclaw/runtime/consensus``: plan members,
fan out, score, pick BCs, run typed operator, write banner-prefixed
report. See ``SKILL.md`` and ADR 0010 for the contract.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from omicsclaw.runtime.consensus.dispatch import output_banner
from omicsclaw.runtime.consensus.member import ConsensusMember, read_intrinsic_quality
from omicsclaw.runtime.consensus.operators.categorical import (
    ConsensusResult,
    kmode_consensus,
    weighted_consensus,
)
from omicsclaw.runtime.consensus.plan import PlannedMember, propose_members
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

logger = logging.getLogger("consensus-domains")

ARTIFACT_RELPATH = "figure_data/spatial_full.csv"
LABEL_COLUMN = "spatial_domain"
INTRINSIC_QUALITY_PATH = "summary.mean_local_purity"


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Multi-method consensus over spatial-domains (ADR 0010 typed A path)."
    )
    parser.add_argument("--input", required=False, help="Preprocessed AnnData (.h5ad)")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument(
        "--members",
        default=None,
        help="Comma-separated method names to fan out (e.g. banksy,graphst,sedr,leiden,spagcn).",
    )
    parser.add_argument("--all", action="store_true", help="Fan out every method in param_hints.")
    parser.add_argument("--n-clusters", type=int, default=None, help="Target cluster count.")
    parser.add_argument("--confirm-plan", action="store_true", help="Prompt y/n before fan-out.")
    parser.add_argument("--non-interactive", action="store_true", help="Skip the BC picker prompt.")
    parser.add_argument("--alpha", type=float, default=ALPHA_DEFAULT)
    parser.add_argument("--beta", type=float, default=BETA_DEFAULT)
    parser.add_argument("--max-class-frac", type=float, default=MAX_CLASS_FRAC_CAP_DEFAULT)
    parser.add_argument(
        "--llm-judge",
        action="store_true",
        help="Allow the evaluation-chair LLM to veto/reweight scores within ADR 0011 bounds.",
    )
    parser.add_argument(
        "--operator",
        choices=["kmode", "weighted", "lca"],
        default="kmode",
        help="Consensus operator (default kmode).",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-parallel", type=int, default=None)
    parser.add_argument("--query", default="", help="Optional natural-language query for the chair LLM.")
    parser.add_argument("--top-k", type=int, default=4, help="Non-interactive top-K size (default 4).")
    parser.add_argument(
        "--run-id",
        default=None,
        help="Run id for graph-memory namespace; defaults to a uuid-derived value.",
    )
    return parser


# --------------------------------------------------------------------------- #
# Member construction                                                          #
# --------------------------------------------------------------------------- #

def _spatial_domains_parameters_yaml() -> Path:
    return Path(__file__).resolve().parent.parent / "spatial-domains" / "parameters.yaml"


def _planned_to_member(planned: PlannedMember) -> ConsensusMember:
    return planned.to_consensus_member(
        skill_name="spatial-domains",
        artifact_relpath=ARTIFACT_RELPATH,
        label_column=LABEL_COLUMN,
        intrinsic_quality_path=INTRINSIC_QUALITY_PATH,
    )


def _members_from_explicit_list(spec: str) -> list[ConsensusMember]:
    """Parse ``--members banksy,leiden:resolution=0.5`` into ConsensusMember list."""
    out: list[ConsensusMember] = []
    seen: set[str] = set()
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if ":" in token:
            method, params_spec = token.split(":", 1)
            params: dict[str, str] = {"method": method}
            for kv in params_spec.split(";"):
                if "=" not in kv:
                    continue
                k, v = kv.split("=", 1)
                params[k.strip()] = v.strip()
        else:
            method = token
            params = {"method": method}
        suffix = "_".join(f"{k}-{v}" for k, v in sorted(params.items()) if k != "method")
        name = f"{method}_{suffix}" if suffix else method
        if name in seen:
            raise SystemExit(f"duplicate member name '{name}' in --members; pick distinct params")
        seen.add(name)
        out.append(
            ConsensusMember(
                name=name,
                skill_name="spatial-domains",
                params=params,
                intrinsic_quality_path=INTRINSIC_QUALITY_PATH,
                artifact_relpath=ARTIFACT_RELPATH,
                label_column=LABEL_COLUMN,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Post-fan-out wiring                                                          #
# --------------------------------------------------------------------------- #

def _load_member_labels(result: MemberRunResult) -> pd.Series | None:
    csv_path = result.member.artifact_path(result.output_dir.parent)
    if not csv_path.exists():
        # Some spatial-domains modes write a different filename; try graceful fallbacks.
        candidates = list(result.output_dir.glob("figure_data/spatial_*.csv"))
        if not candidates:
            return None
        csv_path = candidates[0]
    df = pd.read_csv(csv_path)
    if "observation" not in df.columns or LABEL_COLUMN not in df.columns:
        return None
    return df.set_index("observation")[LABEL_COLUMN].astype(str)


def _gather_labels(
    survivors: Iterable[MemberRunResult],
) -> tuple[pd.DataFrame, dict[str, float], list[str]]:
    columns: dict[str, pd.Series] = {}
    intrinsic: dict[str, float] = {}
    missing: list[str] = []
    for r in survivors:
        labels = _load_member_labels(r)
        if labels is None:
            missing.append(r.member.name)
            continue
        columns[r.member.name] = labels
        summary_path = r.output_dir / "summary.json"
        intrinsic[r.member.name] = read_intrinsic_quality(
            summary_path, r.member.intrinsic_quality_path
        )
    if not columns:
        return pd.DataFrame(), {}, missing
    labels_df = pd.concat(columns, axis=1).dropna(axis=0, how="any")
    return labels_df, intrinsic, missing


def _cross_method_nmi_matrix(labels_df: pd.DataFrame) -> pd.DataFrame:
    from sklearn.metrics import normalized_mutual_info_score

    cols = list(labels_df.columns)
    n = len(cols)
    matrix = np.zeros((n, n), dtype=float)
    for i, a in enumerate(cols):
        for j, b in enumerate(cols):
            if i == j:
                matrix[i, j] = 1.0
            else:
                matrix[i, j] = float(normalized_mutual_info_score(labels_df[a], labels_df[b]))
    return pd.DataFrame(matrix, index=cols, columns=cols)


def _median_k(labels_df: pd.DataFrame) -> int:
    ks = [labels_df[c].nunique() for c in labels_df.columns]
    return int(np.median(ks)) if ks else 0


def _pick_bcs(
    scores: list,
    *,
    non_interactive: bool,
    top_k: int,
) -> list[str]:
    default_pick = top_k_by_score(scores, k=top_k)
    if non_interactive or not sys.stdin.isatty():
        return default_pick
    print(f"[expert] base clusterings (default = top-{top_k} by score: {','.join(default_pick) or '(none)'})")
    raw = input("       enter comma list, 'all', or press Enter for default: ").strip()
    if not raw:
        return default_pick
    if raw.lower() == "all":
        return [s.member for s in scores if not s.filtered]
    requested = [r.strip() for r in raw.split(",") if r.strip()]
    valid = {s.member for s in scores if not s.filtered}
    return [m for m in requested if m in valid] or default_pick


def _run_operator(
    operator: str,
    labels_df: pd.DataFrame,
    *,
    seed: int,
    score_lookup: dict[str, float],
) -> ConsensusResult:
    if operator == "kmode":
        return kmode_consensus(labels_df, seed=seed)
    if operator == "weighted":
        weights = {c: max(score_lookup.get(c, 0.0), 1e-6) for c in labels_df.columns}
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
    scores: list,
    selected: list[str],
    n_failed: int,
    n_total: int,
    nmi_df: pd.DataFrame,
) -> Path:
    banner = output_banner("typed")
    lines = [
        banner,
        "",
        f"# Verified consensus — spatial domains ({operator})",
        "",
        f"- members fanned out: **{n_total}**",
        f"- members surviving: **{n_total - n_failed}**",
        f"- members entering consensus (BC): **{len(selected)}**",
        f"- consensus clusters returned: **{consensus.n_clusters_returned}**",
        f"- operator: `{operator}` (seed={consensus.seed})",
        "",
        "## Base clusterings",
        "",
        "| member | composite | cross_NMI | intrinsic | max_class_frac | filtered |",
        "|---|---|---|---|---|---|",
    ]
    for s in scores:
        in_bc = "✓" if (s.member in selected) else ""
        lines.append(
            f"| {s.member} {in_bc} | {s.composite:.4f} | {s.cross_nmi_mean:.4f} | "
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
        f"_Audit_: this report is the A path; namespace `analysis://typed/<run_id>`. "
        f"Do not strip the `{banner}` banner — it is enforced by ADR 0010.",
    ]
    report_path = output_dir / "report.md"
    report_path.write_text("\n".join(lines))
    return report_path


# --------------------------------------------------------------------------- #
# Entry                                                                        #
# --------------------------------------------------------------------------- #

def _plan_members(
    args: argparse.Namespace, parameters_yaml: Path
) -> list[ConsensusMember]:
    if args.members:
        return _members_from_explicit_list(args.members)
    if args.all:
        from omicsclaw.runtime.consensus.plan import load_param_hints

        hints = load_param_hints(parameters_yaml)
        members = [
            ConsensusMember(
                name=method,
                skill_name="spatial-domains",
                params={"method": method},
                intrinsic_quality_path=INTRINSIC_QUALITY_PATH,
                artifact_relpath=ARTIFACT_RELPATH,
                label_column=LABEL_COLUMN,
            )
            for method in sorted(hints.keys())
        ]
        return members
    planned = propose_members(
        query=args.query,
        skill_name="spatial-domains",
        parameters_yaml_path=parameters_yaml,
        n=5,
        domain="spatial",
        allow_offline=True,
    )
    if not planned:
        return []
    return [_planned_to_member(p) for p in planned]


def _maybe_confirm_plan(members: list[ConsensusMember], confirm: bool) -> bool:
    if not confirm:
        return True
    if not sys.stdin.isatty():
        return True
    print("[plan] proposed members:")
    for i, m in enumerate(members, 1):
        params_str = " ".join(f"--{k} {v}" for k, v in m.params.items() if k != "method")
        print(f"  {i}. {m.params.get('method', '?'):<10} {params_str}")
    raw = input("Proceed? [Y/n] ").strip().lower()
    return raw in ("", "y", "yes")


async def _main_async(args: argparse.Namespace) -> int:
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    params_yaml = _spatial_domains_parameters_yaml()
    members = _plan_members(args, params_yaml)
    if not members:
        print("[consensus-domains] no members planned. Check --members or param_hints.")
        return 2

    if not _maybe_confirm_plan(members, args.confirm_plan):
        print("[consensus-domains] aborted by user.")
        return 130

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
        print(f"[consensus-domains] FATAL: {exc}", file=sys.stderr)
        return 3

    labels_df, intrinsic_map, missing = _gather_labels(team.survived)
    if labels_df.shape[1] < 2:
        print(
            f"[consensus-domains] FATAL: insufficient label artifacts (missing: {missing}).",
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
            f"[consensus-domains] FATAL: <2 base clusterings selected after scoring "
            f"(selected={selected}). Use --max-class-frac or different --members.",
            file=sys.stderr,
        )
        return 5

    selected_df = labels_df[selected]
    score_lookup = {s.member: s.composite for s in scores}
    try:
        consensus = _run_operator(
            args.operator, selected_df, seed=args.seed, score_lookup=score_lookup
        )
    except Exception as exc:  # noqa: BLE001
        from omicsclaw.runtime.consensus.operators.lca_r import LCAUnavailableError

        if isinstance(exc, LCAUnavailableError):
            print(
                f"[consensus-domains] FATAL: LCA operator unavailable: {exc}\n"
                "Pass --operator kmode or --operator weighted to bypass.",
                file=sys.stderr,
            )
            return 6
        print(f"[consensus-domains] FATAL: {args.operator} operator failed: {exc}", file=sys.stderr)
        return 7

    consensus_df = pd.DataFrame(
        {
            "observation": consensus.labels.index,
            f"consensus_{args.operator}": consensus.labels.values,
        }
    )
    consensus_df.to_csv(output_dir / "consensus_labels.tsv", sep="\t", index=False)

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
        f"[consensus-domains] OK: {team.n_survived}/{team.total} members survived; "
        f"{len(selected)} entered consensus; operator={args.operator}; "
        f"clusters_returned={consensus.n_clusters_returned}."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("OMICSCLAW_LOG_LEVEL", "INFO"))
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
