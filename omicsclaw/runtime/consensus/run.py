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

from omicsclaw.common.checksums import sha256_file
from omicsclaw.common.output_claim import (
    collect_output_claim_identities,
    is_scientific_output_file,
)
from omicsclaw.common.report import write_result_json
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

#: Per-member timeout default when a GPU/scVI member is in the set. scVI training
#: is ~10-15 min on ~15k cells, well over the CPU default (DEFAULT_TIMEOUT_SECONDS
#: = 600s), so an --include-scvi run would otherwise drop the scVI member by
#: timeout (ADR 0029 B4). The user can still override with --timeout.
SCVI_DEFAULT_TIMEOUT_SECONDS = 1800.0


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
    # Accepted for CLI back-compat with the v1 wrappers but NOT consumed: these
    # two flags are reserved (the driver never reads them). --n-clusters would
    # force a target cluster count; --llm-judge would enable a chair-LLM
    # veto/reweight. Their final disposition is still open.
    parser.add_argument(
        "--n-clusters", type=int, default=None,
        help="Reserved: accepted but not consumed (would override the target cluster count).",
    )
    parser.add_argument(
        "--llm-judge", action="store_true",
        help="Reserved: accepted but not consumed (would enable a chair-LLM veto/reweight).",
    )
    parser.add_argument(
        "--operator", choices=["kmode", "weighted", "lca", "median"], default=None,
        help="Consensus operator. Default kmode (categorical) / median (continuous). "
             "Allowed per template: categorical=kmode|weighted|lca, continuous=median|weighted.",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--no-spatial-panel", action="store_false", dest="spatial_panel",
        help="Disable the multi-metric spatial intrinsic panel (chaos/pas/mlami) "
             "and score with the reader's single intrinsic signal instead. "
             "Spatial sources only — integration sources always score with their "
             "batch-mixing panel (this flag is a no-op for them).",
    )
    parser.add_argument(
        "--timeout", type=float, default=None,
        help="Per-member timeout (s). Default 600; raised to "
             f"{SCVI_DEFAULT_TIMEOUT_SECONDS:.0f} when --include-scvi (scVI is slow).",
    )
    parser.add_argument("--max-parallel", type=int, default=None)
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument("--run-id", default=None)
    # ChairLLMPlanner (consensus-domains)
    parser.add_argument("--query", default="", help="Optional NL query for the chair LLM")
    # SweepPlanner (sc-consensus-clustering)
    parser.add_argument("--resolutions", default="0.5,0.8,1.0,1.4,2.0")
    parser.add_argument("--cluster-methods", default="leiden")
    # IntegrationRepSweepPlanner (sc-consensus-integration)
    parser.add_argument(
        "--integration-methods", default=None,
        help="Comma list of integration backends (none,harmony,scanorama,scvi); "
             "default none,harmony,scanorama (+scvi with --include-scvi).",
    )
    parser.add_argument(
        "--resolution", default=None,
        help="Fixed clustering resolution for sc-consensus-integration members.",
    )
    parser.add_argument(
        "--batch-key", default="batch",
        help="obs batch column (sc-consensus-integration: members integrate + the "
             "intrinsic panel scores batch mixing against this key).",
    )
    parser.add_argument(
        "--include-scvi", action="store_true",
        help="Add the GPU/stochastic scVI member to the default integration set "
             "(serialise GPU members with --max-parallel 1).",
    )
    parser.add_argument(
        "--vote-baseline", action="store_true",
        help="Include the unintegrated baseline (method=none) in the consensus "
             "vote (sc-consensus-integration). Default: the baseline is scored + "
             "reported but excluded from the vote — it is a diagnostic control, "
             "and voting it as an equal drags the consensus (ADR 0029 B2).",
    )
    # PseudotimeMethodPlanner (sc-consensus-pseudotime, ADR 0031)
    parser.add_argument(
        "--pseudotime-methods", default=None,
        help="Comma list of pseudotime methods (dpt,palantir,via); default all three.",
    )
    parser.add_argument(
        "--root-cluster", default=None,
        help="Shared root cluster for sc-consensus-pseudotime members "
             "(required — pass this or --root-cell; a shared root pins direction).",
    )
    parser.add_argument(
        "--root-cell", default=None,
        help="Shared root cell (obs_name or integer index) for sc-consensus-pseudotime members.",
    )
    return parser


#: Operators valid per Workflow template (ADR 0031 — continuous adds median).
_OPERATORS_BY_TEMPLATE: dict[str, tuple[str, ...]] = {
    "categorical": ("kmode", "weighted", "lca"),
    "continuous": ("median", "weighted"),
}
_DEFAULT_OPERATOR_BY_TEMPLATE: dict[str, str] = {
    "categorical": "kmode",
    "continuous": "median",
}


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


def _derive_non_voting(source, members, args) -> tuple[str, ...]:
    """Members excluded from the consensus vote (diagnostic baselines, ADR 0029 B2).

    Only ``sc-consensus-integration`` has a ``method=none`` reference baseline that
    should be scored + reported but not voted; the check is **gated on the source**
    so a future flavour that happens to use ``method=none`` is not silently
    affected. ``--vote-baseline`` opts the baseline back into the vote.
    """
    if source.name != "sc-consensus-integration" or getattr(args, "vote_baseline", False):
        return ()
    return tuple(m.name for m in members if str(m.params.get("method", "")) == "none")


def _resolve_timeout(args, members) -> float:
    """Per-member timeout (s): explicit ``--timeout`` wins; else the CPU default,
    raised when an **scVI member is actually planned** because scVI GPU training is
    far slower than the 600s CPU default and would otherwise be dropped by timeout
    (ADR 0029 B4). Keyed off the planned members — not the ``--include-scvi`` flag —
    so it also covers ``--all`` / ``--integration-methods scvi``, and does not bump
    when ``--include-scvi`` is passed but the explicit method set excludes scVI."""
    if args.timeout is not None:
        return float(args.timeout)
    has_scvi = any(
        str(getattr(m, "params", {}).get("method", "")) == "scvi" for m in members
    )
    if has_scvi:
        logger.warning(
            "scVI member planned: raising the default per-member timeout to %.0fs "
            "(scVI GPU training is slow — ~10-15 min on ~15k cells). Pass "
            "--timeout to override; increase it further for larger datasets.",
            SCVI_DEFAULT_TIMEOUT_SECONDS,
        )
        return SCVI_DEFAULT_TIMEOUT_SECONDS
    return DEFAULT_TIMEOUT_SECONDS


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


def _write_consensus_result(
    *,
    source_name: str,
    input_path: str,
    output_dir: Path,
    operator: str,
    run,
) -> None:
    """Emit the same standard result envelope as ordinary leaf skills."""
    from omicsclaw.skill.registry import ensure_registry_loaded

    info = ensure_registry_loaded().skills.get(source_name, {})
    version = str(info.get("version") or "0.1.0")
    checksum = (
        sha256_file(input_path)
        if input_path and Path(input_path).is_file()
        else ""
    )
    summary = {
        "method": operator,
        "members_planned": len(run.members),
        "members_survived": int(run.team_result.n_survived),
        "base_clusterings": len(run.selected_bcs),
    }
    claim_identities = collect_output_claim_identities(output_dir)
    data = {
        "run_id": run.run_id,
        "selected_bcs": list(run.selected_bcs),
        "artifacts": [
            Path(path).name
            for path in run.artifacts_written
            if is_scientific_output_file(
                Path(path),
                output_root=output_dir,
                claim_identities=claim_identities,
            )
        ],
        "params": {"operator": operator},
    }
    write_result_json(
        output_dir,
        source_name,
        version,
        summary,
        data,
        input_checksum=checksum,
    )


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

    # Resolve + validate the operator for this template (ADR 0031: continuous
    # uses median|weighted, categorical uses kmode|weighted|lca). --operator
    # defaults to None so each template picks its own default.
    allowed_ops = _OPERATORS_BY_TEMPLATE.get(source.template, ("kmode", "weighted", "lca"))
    operator = args.operator or _DEFAULT_OPERATOR_BY_TEMPLATE.get(source.template, "kmode")
    if operator not in allowed_ops:
        print(
            f"[{args.source}] operator {operator!r} is not valid for template "
            f"{source.template!r}; choose from {sorted(allowed_ops)}.",
            file=sys.stderr,
        )
        return 2

    # Scoring thresholds (alpha/beta/max_class_fraction_cap/top_k) are written to
    # plan.json authoritatively by the driver from the ScoreConfig it actually
    # used — don't duplicate them here (avoids divergent keys in plan.json).
    plan_audit = {
        "run_id": args.run_id or output_dir.name,
        "operator": operator,
        "members": [{"name": m.name, "params": dict(m.params)} for m in members],
    }
    # The continuous driver writes report.md itself (AC2) and needs the flavour title;
    # pass it via plan_audit (the driver pops it before writing plan.json). Categorical
    # renders the report in run.py below, so it does not need this key.
    if source.template == "continuous":
        plan_audit["report_title"] = source.report_title

    non_voting = _derive_non_voting(source, members, args)

    try:
        run = await template.driver(
            members=members,
            source=source,
            input_path=args.input or "",
            output_dir=output_dir,
            operator=operator,
            bc_selector=_make_bc_selector(args),
            top_k_default=args.top_k,
            score_config=ScoreConfig(args.alpha, args.beta, args.max_class_frac),
            seed=args.seed,
            plan_audit=plan_audit,
            timeout_seconds=_resolve_timeout(args, members),
            max_parallel=args.max_parallel,
            use_spatial_panel=args.spatial_panel,
            batch_key=args.batch_key,
            non_voting_members=non_voting,
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

    if source.template != "continuous":
        md = format_typed_report(run, title=source.report_title)
        (run.output_dir / "report.md").write_text(md)

    _write_consensus_result(
        source_name=args.source,
        input_path=args.input or "",
        output_dir=run.output_dir,
        operator=operator,
        run=run,
    )

    if source.template == "continuous":
        # report.md is written by run_continuous_consensus itself (AC2).
        print(
            f"[{args.source}] OK: {run.team_result.n_survived}/{run.team_result.total} members; "
            f"{len(run.selected_bcs)} entered consensus; operator={operator}; "
            f"weak_agreement={run.weak_agreement['diverged']} "
            f"(mean rho={run.weak_agreement['cohort_mean_spearman']:.3f})."
        )
        return 0

    print(
        f"[{args.source}] OK: {run.team_result.n_survived}/{run.team_result.total} members; "
        f"{len(run.selected_bcs)} entered consensus; operator={operator}; "
        f"clusters_returned={run.consensus.n_clusters_returned}."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=os.environ.get("OMICSCLAW_LOG_LEVEL", "INFO"))
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
