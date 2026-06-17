"""Markdown rendering for a ``ContinuousConsensusRun`` (ADR 0031).

Mirrors ``report.format_typed_report``: the ``[A: Verified consensus]`` banner is
the first line of every report — non-configurable per ADR 0010, enforced here so
no caller can render an A-path continuous report without it. Continuous-specific
sections replace the categorical ones: a pairwise-Spearman agreement matrix (not
cross-NMI), member agreement scores, the weak-agreement guard, and consensus
pseudotime dispersion (not cluster support).
"""

from __future__ import annotations

import numpy as np

from omicsclaw.runtime.consensus.continuous_driver import ContinuousConsensusRun
from omicsclaw.runtime.consensus.dispatch import output_banner

_DISCLAIMER = (
    "This verified consensus reflects agreement among selected computational "
    "members under the recorded plan and scoring rules. It should be interpreted "
    "as a reproducible consensus estimate, not as experimental ground truth."
)


def _failed_members_section(run: ContinuousConsensusRun) -> list[str]:
    failed = list(run.team_result.failed)
    missing = list(run.missing_members)
    partial = list(run.partial_excluded)
    dropped = list(run.dropped_degenerate)
    lines = ["## Failed / excluded members", ""]
    if not failed and not missing and not partial and not dropped:
        lines.append("None — every fanned-out member produced a usable pseudotime.")
        return lines
    for r in failed:
        lines.append(f"- `{r.step.name}` — {r.status}: {r.error or '(no detail)'}")
    for name in missing:
        lines.append(f"- `{name}` — completed but produced no readable pseudotime; excluded.")
    for name in partial:
        lines.append(
            f"- `{name}` — incomplete cell coverage / non-finite pseudotime; dropped whole "
            "(full coverage required, ADR 0031 §4)."
        )
    for name in dropped:
        lines.append(
            f"- `{name}` — degenerate pseudotime (constant / <2 unique / non-finite); "
            "dropped (Spearman undefined)."
        )
    return lines


def _weak_agreement_section(run: ContinuousConsensusRun) -> list[str]:
    w = run.weak_agreement
    lines = ["## Cross-method agreement (Spearman)", ""]
    lines.append(f"- cohort mean pairwise Spearman (voters): **{w['cohort_mean_spearman']:.3f}**")
    lines.append(
        f"- worst pair: `{w['min_pair'][0]}`–`{w['min_pair'][1]}` ρ=**{w['min_pairwise_spearman']:.3f}**"
        if w["min_pair"] else "- worst pair: n/a"
    )
    if w["diverged"]:
        lines.append(
            f"- ⚠️ **weak agreement** (mean ρ < {w['threshold']:.2f}): the methods disagree "
            "on the ordering, so a single consensus pseudotime may be ill-posed — the data "
            "may have no shared trajectory or multiple lineages. Inspect the worst pair(s); "
            "v1 reports this but does not drop members."
        )
    if w["weak_pairs"]:
        pairs = ", ".join(f"`{a}`–`{b}` ({v})" for a, b, v in w["weak_pairs"])
        lines.append(f"- sub-threshold pairs: {pairs}")
    lines += [
        "",
        "```",
        run.agreement_matrix.round(3).to_string(),
        "```",
    ]
    return lines


def _dispersion_section(run: ContinuousConsensusRun) -> list[str]:
    c = run.consensus
    mad = c.pseudotime_mad.to_numpy()
    rng = c.value_range.to_numpy()
    return [
        "## Consensus pseudotime + dispersion",
        "",
        f"- operator: `{c.operator}` over **{c.n_voting}** voting member(s)",
        f"- consensus tie fraction (flatness): **{c.tie_fraction:.3f}**",
        f"- mean per-cell `pseudotime_mad` (majority dispersion, 2·MAD): **{float(np.mean(mad)):.3f}**",
        f"- mean per-cell `range` (full disagreement): **{float(np.mean(rng)):.3f}**",
        f"- high-support cells (mad < 0.2 *and* range < 0.4): "
        f"**{float(np.mean((mad < 0.2) & (rng < 0.4))) * 100:.1f}%**",
        "",
        "_Per-cell `consensus_pseudotime` / `pseudotime_mad` / `range` are in "
        "`consensus_pseudotime.tsv`._",
    ]


def format_continuous_report(run: ContinuousConsensusRun, *, title: str) -> str:
    """Render a verified continuous-consensus markdown report.

    The first line is always ``[A: Verified consensus]`` — banner enforcement
    lives here so no caller can render an A-path report without it.
    """
    banner = output_banner("typed")
    op = run.operator
    lines: list[str] = [
        banner,
        "",
        f"# {title} ({op})",
        "",
        f"- members fanned out: **{run.team_result.total}**",
        f"- members surviving: **{run.team_result.n_survived}**",
        f"- members failed: **{run.team_result.n_failed}**",
        f"- members entering consensus: **{len(run.selected_bcs)}**",
        f"- direction anchor: `{run.anchor}`"
        + (f"; flipped: {', '.join(f'`{m}`' for m in run.flipped_members)}" if run.flipped_members else "; no flips"),
        f"- operator: `{op}` (seed={run.consensus.seed})",
        "",
    ]

    lines += _failed_members_section(run)
    lines.append("")

    # Member agreement scores — selected vs rejected.
    lines += [
        "## Member agreement scores",
        "",
        "| member | agreement (mean ρ) | selected | reason |",
        "|---|---|---|---|",
    ]
    for s in run.scores:
        reason = s.selection_reason or ("passed" if s.selected else "")
        lines.append(
            f"| {s.member} | {s.agreement_mean:.4f} | "
            f"{'yes' if s.selected else 'no'} | {reason} |"
        )
    lines.append("")

    lines += _weak_agreement_section(run)
    lines.append("")
    lines += _dispersion_section(run)
    lines.append("")

    lines += [
        "## Scoring parameters & method",
        "",
        "- composite score = **mean pairwise Spearman** agreement (v1 is "
        "agreement-only: **α=1.0, β=0.0**; the intrinsic panel is deferred, ADR 0031).",
        "- members are made comparable by **rank-normalisation** (cancels the monotone "
        "pseudotime gauge) + a **direction safeguard** (anchor = highest mean |ρ|; flip "
        "ρ<0). The consensus is **re-ranked** to [0, 1].",
        f"- base-clustering selection: top-**{run.top_k}** members by agreement.",
        "",
        "_All thresholds above are recorded in `plan.json`; none are hidden._",
        "",
        "## Interpretation notes",
        "",
        f"> {_DISCLAIMER}",
        "",
        "**On agreement:** high mean pairwise Spearman means the pseudotime methods order "
        "the cells consistently under the *current input, root and parameters* — evidence "
        "of consensus stability, **not** biological correctness. Low agreement (see the "
        "weak-agreement guard) warns the single-trajectory assumption may not hold.",
        "",
        f"_Audit_: A path; namespace `analysis://typed/{run.run_id}`. "
        f"Do not strip the `{banner}` banner — it is enforced by ADR 0010.",
    ]
    return "\n".join(lines)
