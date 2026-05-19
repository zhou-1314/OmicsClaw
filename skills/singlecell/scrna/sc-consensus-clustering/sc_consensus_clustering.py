"""sc-consensus-clustering — multi-resolution typed consensus over sc-clustering.

Thin CLI wrapper. Argument parsing + member planning (resolution sweep) +
BC-picker construction; the rest of the orchestration is delegated to
``runtime/consensus/driver``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from omicsclaw.runtime.consensus.driver import (
    BCSelectorFn,
    InsufficientBCsError,
    ScoreConfig,
    run_typed_consensus,
)
from omicsclaw.runtime.consensus.member import ConsensusMember
from omicsclaw.runtime.consensus.operators.lca_r import LCAUnavailableError
from omicsclaw.runtime.consensus.report import format_typed_report
from omicsclaw.runtime.consensus.scoring import (
    ALPHA_DEFAULT,
    BETA_DEFAULT,
    MAX_CLASS_FRAC_CAP_DEFAULT,
    MemberScore,
    top_k_by_score,
)
from omicsclaw.runtime.consensus.source_registry import TYPED_CONSENSUS_REGISTRY
from omicsclaw.runtime.consensus.team import (
    DEFAULT_TIMEOUT_SECONDS,
    InsufficientSurvivorsError,
)

logger = logging.getLogger("sc-consensus-clustering")

SKILL_NAME = "sc-clustering"
DEFAULT_RESOLUTIONS = "0.5,0.8,1.0,1.4,2.0"
DEFAULT_METHODS = "leiden"


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Multi-resolution consensus over sc-clustering (ADR 0010 typed A path)."
    )
    parser.add_argument("--input", required=False, help="Preprocessed scRNA AnnData (.h5ad)")
    parser.add_argument("--output", required=True)
    parser.add_argument("--members", default=None,
                        help="Explicit list, e.g. leiden:resolution=0.5,louvain:resolution=1.0")
    parser.add_argument("--resolutions", default=DEFAULT_RESOLUTIONS)
    parser.add_argument("--cluster-methods", default=DEFAULT_METHODS)
    parser.add_argument("--all", action="store_true",
                        help="Sweep BOTH leiden and louvain at every default resolution")
    parser.add_argument("--n-clusters", type=int, default=None)
    parser.add_argument("--confirm-plan", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--alpha", type=float, default=ALPHA_DEFAULT)
    parser.add_argument("--beta", type=float, default=BETA_DEFAULT)
    parser.add_argument("--max-class-frac", type=float, default=MAX_CLASS_FRAC_CAP_DEFAULT)
    parser.add_argument("--llm-judge", action="store_true")
    parser.add_argument("--operator", choices=["kmode", "weighted", "lca"], default="kmode")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-parallel", type=int, default=None)
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument("--run-id", default=None)
    return parser


# --------------------------------------------------------------------------- #
# Member planning                                                             #
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
        suffix = "_".join(
            f"{k}-{v}" for k, v in sorted(params.items()) if k != "cluster-method"
        )
        name = f"{method}_{suffix}" if suffix else method
        if name in seen:
            raise SystemExit(f"duplicate member name '{name}'")
        seen.add(name)
        out.append(ConsensusMember(name=name, skill_name=SKILL_NAME, params=params))
    return out


def _members_from_sweep(methods: list[str], resolutions: list[float]) -> list[ConsensusMember]:
    out: list[ConsensusMember] = []
    for method in methods:
        for r in resolutions:
            r_str = str(r)
            out.append(
                ConsensusMember(
                    name=f"{method}_resolution-{r_str}",
                    skill_name=SKILL_NAME,
                    params={"cluster-method": method, "resolution": r_str},
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
# BC selector                                                                 #
# --------------------------------------------------------------------------- #

def _make_bc_selector(args: argparse.Namespace) -> BCSelectorFn:
    def selector(scores: list[MemberScore], k: int) -> list[str]:
        default_pick = top_k_by_score(scores, k=k)
        if args.non_interactive or not sys.stdin.isatty():
            return default_pick
        print(
            f"[expert] base clusterings (default = top-{k} by score: "
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

    return selector


# --------------------------------------------------------------------------- #
# Entry                                                                       #
# --------------------------------------------------------------------------- #

async def _main_async(args: argparse.Namespace) -> int:
    output_dir = Path(args.output)
    members = _plan_members(args)
    if not members:
        print("[sc-consensus-clustering] no members planned.")
        return 2

    source = TYPED_CONSENSUS_REGISTRY[SKILL_NAME]
    plan_audit = {
        "run_id": args.run_id or output_dir.name,
        "operator": args.operator,
        "members": [{"name": m.name, "params": dict(m.params)} for m in members],
        "alpha": args.alpha,
        "beta": args.beta,
        "max_class_frac": args.max_class_frac,
    }

    try:
        run = await run_typed_consensus(
            members=members,
            source=source,
            input_path=args.input or "",
            output_dir=output_dir,
            operator=args.operator,
            bc_selector=_make_bc_selector(args),
            top_k_default=args.top_k,
            score_config=ScoreConfig(args.alpha, args.beta, args.max_class_frac),
            seed=args.seed,
            plan_audit=plan_audit,
            timeout_seconds=args.timeout,
            max_parallel=args.max_parallel,
        )
    except InsufficientSurvivorsError as exc:
        print(f"[sc-consensus-clustering] FATAL: {exc}", file=sys.stderr)
        return 3
    except InsufficientBCsError as exc:
        print(f"[sc-consensus-clustering] FATAL: {exc}", file=sys.stderr)
        return 5
    except LCAUnavailableError as exc:
        print(
            f"[sc-consensus-clustering] FATAL: LCA operator unavailable: {exc}\n"
            "Pass --operator kmode or --operator weighted to bypass.",
            file=sys.stderr,
        )
        return 6

    md = format_typed_report(run, title="Verified consensus — sc clustering")
    (run.output_dir / "report.md").write_text(md)
    print(
        f"[sc-consensus-clustering] OK: {run.team_result.n_survived}/{run.team_result.total} members; "
        f"{len(run.selected_bcs)} entered consensus; operator={args.operator}; "
        f"clusters_returned={run.consensus.n_clusters_returned}."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=os.environ.get("OMICSCLAW_LOG_LEVEL", "INFO"))
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
