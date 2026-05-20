"""consensus-domains — multi-method consensus over spatial-domains.

Thin CLI wrapper. Argument parsing + member planning + BC-picker
construction; the rest of the orchestration (fan-out, scoring, BC pick,
operator, artifact writes) is delegated to ``runtime/consensus/driver``.
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
from omicsclaw.runtime.consensus.plan import (
    PlannedMember,
    load_param_hints,
    propose_members,
)
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

logger = logging.getLogger("consensus-domains")

SKILL_NAME = "spatial-domains"


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Multi-method consensus over spatial-domains (ADR 0010 typed A path)."
    )
    parser.add_argument("--input", required=False, help="Preprocessed AnnData (.h5ad)")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--members", default=None, help="Comma-separated method names")
    parser.add_argument("--all", action="store_true", help="Fan out every method in param_hints")
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
    parser.add_argument("--query", default="", help="Optional NL query for the chair LLM")
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument("--run-id", default=None)
    return parser


# --------------------------------------------------------------------------- #
# Member planning                                                              #
# --------------------------------------------------------------------------- #

def _spatial_domains_parameters_yaml() -> Path:
    return Path(__file__).resolve().parent.parent / "spatial-domains" / "parameters.yaml"


def _members_from_explicit_list(spec: str) -> list[ConsensusMember]:
    """Parse ``--members banksy,leiden:resolution=0.5`` into a member list."""
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
        out.append(ConsensusMember(name=name, skill_name=SKILL_NAME, params=params))
    return out


def _plan_members(args: argparse.Namespace) -> list[ConsensusMember]:
    if args.members:
        return _members_from_explicit_list(args.members)
    params_yaml = _spatial_domains_parameters_yaml()
    if args.all:
        hints = load_param_hints(params_yaml)
        return [
            ConsensusMember(name=method, skill_name=SKILL_NAME, params={"method": method})
            for method in sorted(hints.keys())
        ]
    planned: list[PlannedMember] = propose_members(
        query=args.query,
        skill_name=SKILL_NAME,
        parameters_yaml_path=params_yaml,
        n=5,
        domain="spatial",
        allow_offline=True,
    )
    return [p.to_consensus_member(skill_name=SKILL_NAME) for p in planned]


# --------------------------------------------------------------------------- #
# BC selector — CLI interactive vs non-interactive                             #
# --------------------------------------------------------------------------- #

def _make_bc_selector(args: argparse.Namespace) -> BCSelectorFn:
    """Return a callable that picks BCs from a scored member list.

    CLI surface defaults to interactive prompt on a TTY; ``--non-interactive``
    forces top-K-by-score. The contract matches ``BCSelectorFn`` so the
    driver doesn't need to know surface details.
    """
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


def _maybe_confirm_plan(members: list[ConsensusMember], confirm: bool) -> bool:
    if not confirm or not sys.stdin.isatty():
        return True
    print("[plan] proposed members:")
    for i, m in enumerate(members, 1):
        params_str = " ".join(f"--{k} {v}" for k, v in m.params.items() if k != "method")
        print(f"  {i}. {m.params.get('method', '?'):<10} {params_str}")
    raw = input("Proceed? [Y/n] ").strip().lower()
    return raw in ("", "y", "yes")


# --------------------------------------------------------------------------- #
# Entry                                                                       #
# --------------------------------------------------------------------------- #

async def _main_async(args: argparse.Namespace) -> int:
    output_dir = Path(args.output)
    members = _plan_members(args)
    if not members:
        print("[consensus-domains] no members planned. Check --members or param_hints.")
        return 2
    if not _maybe_confirm_plan(members, args.confirm_plan):
        print("[consensus-domains] aborted by user.")
        return 130

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
        print(f"[consensus-domains] FATAL: {exc}", file=sys.stderr)
        return 3
    except InsufficientBCsError as exc:
        print(f"[consensus-domains] FATAL: {exc}", file=sys.stderr)
        return 5
    except LCAUnavailableError as exc:
        print(
            f"[consensus-domains] FATAL: LCA operator unavailable: {exc}\n"
            "Pass --operator kmode or --operator weighted to bypass.",
            file=sys.stderr,
        )
        return 6

    md = format_typed_report(run, title="Verified consensus — spatial domains")
    (run.output_dir / "report.md").write_text(md)
    print(
        f"[consensus-domains] OK: {run.team_result.n_survived}/{run.team_result.total} members; "
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
