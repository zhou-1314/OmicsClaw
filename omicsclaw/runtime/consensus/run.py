"""Generic typed-consensus entry (ADR 0016 L3) — replaces the per-flavour wrappers.

``python -m omicsclaw.runtime.consensus.run --source <flavour> ...`` looks up the
flavour's ``ConsensusSource``, plans members via its ``MemberPlanner``, runs the
bound Workflow-template driver, and renders the report. The two thin skills
(``consensus-domains``, ``sc-consensus-clustering``) are 3-line shims over this.

This is the de-duplicated ``_main_async`` the two wrappers used to copy-paste —
including the ``_maybe_confirm_plan`` gate, now honoured uniformly (fixing
``sc-consensus-clustering`` silently ignoring ``--confirm-plan``).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from omicsclaw.runtime.consensus.driver import InsufficientBCsError, ScoreConfig
from omicsclaw.runtime.consensus.operators.lca_r import LCAUnavailableError
from omicsclaw.runtime.consensus.report import format_typed_report
from omicsclaw.runtime.consensus.scoring import (
    ALPHA_DEFAULT,
    BETA_DEFAULT,
    MAX_CLASS_FRAC_CAP_DEFAULT,
    MemberScore,
    top_k_by_score,
)
from omicsclaw.runtime.consensus.sources import CONSENSUS_SOURCES
from omicsclaw.runtime.consensus.templates import TEMPLATES
from omicsclaw.runtime.workflow import DEFAULT_TIMEOUT_SECONDS, InsufficientSurvivorsError

logger = logging.getLogger("consensus-run")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Typed consensus over a registered source (ADR 0016)."
    )
    parser.add_argument(
        "--source", required=True, choices=sorted(CONSENSUS_SOURCES),
        help="Consensus flavour (registered in CONSENSUS_SOURCES).",
    )
    parser.add_argument("--input", required=False, help="Preprocessed AnnData (.h5ad)")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--members", default=None, help="Comma-separated member spec")
    parser.add_argument("--all", action="store_true", help="Fan out the flavour's full set")
    parser.add_argument("--confirm-plan", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--alpha", type=float, default=ALPHA_DEFAULT)
    parser.add_argument("--beta", type=float, default=BETA_DEFAULT)
    parser.add_argument("--max-class-frac", type=float, default=MAX_CLASS_FRAC_CAP_DEFAULT)
    # Accepted for CLI back-compat with the v1 wrappers but not yet consumed
    # (reserved: --n-clusters target override, --llm-judge chair veto/reweight).
    parser.add_argument("--n-clusters", type=int, default=None)
    parser.add_argument("--llm-judge", action="store_true")
    parser.add_argument("--operator", choices=["kmode", "weighted", "lca"], default="kmode")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-parallel", type=int, default=None)
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument("--run-id", default=None)
    # ChairLLMPlanner (consensus-domains)
    parser.add_argument("--query", default="", help="Optional NL query for the chair LLM")
    # SweepPlanner (sc-consensus-clustering)
    parser.add_argument("--resolutions", default="0.5,0.8,1.0,1.4,2.0")
    parser.add_argument("--cluster-methods", default="leiden")
    return parser


def _make_bc_selector(args: argparse.Namespace):
    """Return a callable that picks BCs from a scored member list.

    CLI defaults to interactive prompt on a TTY; ``--non-interactive`` (or a
    non-TTY stdin) forces top-K-by-score. Matches ``BCSelectorFn`` so the driver
    stays surface-agnostic.
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


def _maybe_confirm_plan(members, confirm: bool) -> bool:
    """Interactive pre-run plan confirmation (opt-in via ``--confirm-plan``).

    Folded into the generic entry so every flavour honours ``--confirm-plan``
    uniformly — ``sc-consensus-clustering`` parsed but ignored it before.
    """
    if not confirm or not sys.stdin.isatty():
        return True
    print("[plan] proposed members:")
    for i, m in enumerate(members, 1):
        print(f"  {i}. {m.name}")
    raw = input("Proceed? [Y/n] ").strip().lower()
    return raw in ("", "y", "yes")


async def _run(args: argparse.Namespace) -> int:
    source = CONSENSUS_SOURCES[args.source]
    output_dir = Path(args.output)

    members = source.planner.propose(args, source=source)
    if not members:
        print(f"[{args.source}] no members planned. Check --members or param_hints.")
        return 2
    if not _maybe_confirm_plan(members, args.confirm_plan):
        print(f"[{args.source}] aborted by user.")
        return 130

    template = TEMPLATES[source.template]
    if template.driver is None:
        print(
            f"[{args.source}] template '{source.template}' has no run-driver "
            "(B-path templates execute elsewhere).",
            file=sys.stderr,
        )
        return 4

    plan_audit = {
        "run_id": args.run_id or output_dir.name,
        "operator": args.operator,
        "members": [{"name": m.name, "params": dict(m.params)} for m in members],
        "alpha": args.alpha,
        "beta": args.beta,
        "max_class_frac": args.max_class_frac,
    }

    try:
        run = await template.driver(
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
        print(f"[{args.source}] FATAL: {exc}", file=sys.stderr)
        return 3
    except InsufficientBCsError as exc:
        print(f"[{args.source}] FATAL: {exc}", file=sys.stderr)
        return 5
    except LCAUnavailableError as exc:
        print(
            f"[{args.source}] FATAL: LCA operator unavailable: {exc}\n"
            "Pass --operator kmode or --operator weighted to bypass.",
            file=sys.stderr,
        )
        return 6

    md = format_typed_report(run, title=source.report_title)
    (run.output_dir / "report.md").write_text(md)
    print(
        f"[{args.source}] OK: {run.team_result.n_survived}/{run.team_result.total} members; "
        f"{len(run.selected_bcs)} entered consensus; operator={args.operator}; "
        f"clusters_returned={run.consensus.n_clusters_returned}."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=os.environ.get("OMICSCLAW_LOG_LEVEL", "INFO"))
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
