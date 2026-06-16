"""Markdown rendering for a ``TypedConsensusRun``.

The banner is the first line of every report — non-configurable per ADR 0010.
Callers pass only a domain-specific ``title``. Sections are stable across thin
skills so downstream parsers (graph memory writer, paper-figure extractor) can
rely on the structure. Beyond the core result this renders the explainability
panel: failed members, a selected-vs-rejected table with reasons, per-spot
consensus confidence, the scoring thresholds (also in ``plan.json``), and fixed
interpretation notes (consensus-is-not-ground-truth + a calibrated NMI caveat).
"""

from __future__ import annotations

from omicsclaw.runtime.consensus.dispatch import output_banner
from omicsclaw.runtime.consensus.driver import TypedConsensusRun

#: Fixed disclaimer — a verified consensus is a reproducible estimate, not truth.
_DISCLAIMER = (
    "This verified consensus reflects agreement among selected computational "
    "members under the recorded plan and scoring rules. It should be interpreted "
    "as a reproducible consensus estimate, not as experimental ground truth."
)


def _failed_members_section(run: TypedConsensusRun) -> list[str]:
    failed = list(run.team_result.failed)
    # Members that exited 0 but whose artifacts had no readable labels are
    # excluded from consensus too — they are not in ``team_result.failed`` but
    # in ``run.missing_label_members``, so a partial run stays auditable here.
    missing = list(run.missing_label_members)
    lines = ["## Failed members", ""]
    if not failed and not missing:
        lines.append("None — every fanned-out member completed.")
        return lines
    lines.append(
        f"{len(failed) + len(missing)} member(s) did not produce a usable result:"
    )
    lines.append("")
    for r in failed:
        lines.append(f"- `{r.step.name}` — {r.status}: {r.error or '(no detail)'}")
    for name in missing:
        lines.append(
            f"- `{name}` — completed (exit 0) but produced no readable labels; "
            "excluded from consensus."
        )
    return lines


def _confidence_section(run: TypedConsensusRun) -> list[str]:
    conf = run.confidence
    lines = ["## Consensus confidence", ""]
    if conf is None or len(conf) == 0 or "support" not in getattr(conf, "columns", []):
        lines.append("Per-observation confidence not available for this run.")
        return lines
    support = conf["support"]
    lines += [
        f"- mean per-spot support (members agreeing with the consensus label): "
        f"**{float(support.mean()):.3f}**",
        f"- high-confidence spots (support ≥ 0.8): **{float((support >= 0.8).mean()) * 100:.1f}%**",
        f"- contested spots (support < 0.5): **{float((support < 0.5).mean()) * 100:.1f}%**",
        f"- mean per-spot label entropy across members: **{float(conf['entropy'].mean()):.3f}** bits",
        "",
        "_Per-observation `support` / `entropy` / `n_members` are in `consensus_labels.tsv`._",
    ]
    return lines


def _k_divergence_section(run: TypedConsensusRun) -> list[str]:
    k = run.k_stats
    if not k or "k_by_member" not in k:
        return []
    lines = ["## Cluster-count (k) across members", ""]
    pairs = ", ".join(f"`{m}`={v}" for m, v in k["k_by_member"].items())
    lines.append(f"- per-member k: {pairs}")
    lines.append(
        f"- k range **{k['k_min']}–{k['k_max']}** (CV {k['k_cv']:.2f})"
    )
    if k.get("diverged"):
        lines.append(
            "- ⚠️ **k diverges** (max/min > 2×): the consensus operator aligns "
            "members into a common label space, so where cluster counts differ "
            "widely the per-spot `support` can reflect operator-induced folding "
            "rather than biological uncertainty. Prefer members with comparable k, "
            "or fix the clustering resolution."
        )
    return lines


def format_typed_report(run: TypedConsensusRun, *, title: str) -> str:
    """Render a verified-consensus markdown report from a ``TypedConsensusRun``.

    The first line is always ``[A: Verified consensus]`` regardless of caller
    intent — banner enforcement lives here so no thin skill can render an
    A-path report without it.
    """
    banner = output_banner("typed")
    op = run.operator
    cfg = run.score_config
    lines: list[str] = [
        banner,
        "",
        f"# {title} ({op})",
        "",
        f"- members fanned out: **{run.team_result.total}**",
        f"- members surviving: **{run.team_result.n_survived}**",
        f"- members failed: **{run.team_result.n_failed}**",
        f"- members entering consensus (BC): **{len(run.selected_bcs)}**",
        f"- consensus clusters returned: **{run.consensus.n_clusters_returned}**",
        f"- operator: `{op}` (seed={run.consensus.seed})",
        "",
    ]

    lines += _failed_members_section(run)
    lines.append("")

    # Base clusterings — selected vs rejected, with the reason for each.
    lines += [
        "## Base clusterings",
        "",
        "| member | composite | cross_NMI | intrinsic | max_class_frac | selected | reason |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in run.scores:
        reason = s.selection_reason or ("passed" if s.selected else "")
        lines.append(
            f"| {s.member} | {s.composite:.4f} | {s.cross_nmi_mean:.4f} | "
            f"{s.intrinsic:.4f} | {s.max_class_frac:.3f} | "
            f"{'yes' if s.selected else 'no'} | {reason} |"
        )
    lines.append("")

    # Cross-method NMI matrix (+ heatmap figure when rendered).
    lines += [
        "## Cross-method NMI matrix",
        "",
        "```",
        run.nmi_matrix.round(3).to_string(),
        "```",
        "",
    ]
    if run.nmi_heatmap_path is not None:
        lines += [f"![Cross-method NMI heatmap]({run.nmi_heatmap_path.name})", ""]

    lines += _confidence_section(run)
    lines.append("")

    k_section = _k_divergence_section(run)
    if k_section:
        lines += k_section
        lines.append("")

    # Scoring parameters & thresholds (also recorded in plan.json; never hidden).
    lines += [
        "## Scoring parameters & thresholds",
        "",
        f"- composite score = α·cross_NMI + β·intrinsic, with **α={cfg.alpha}**, **β={cfg.beta}**",
        f"- max-class-fraction hard filter: a member whose largest cluster exceeds "
        f"**{cfg.max_class_frac_cap}** of observations is excluded (`composite = -inf`)",
        f"- base-clustering selection: top-**{run.top_k}** members by composite score",
        f"- operator: `{op}` (seed={run.consensus.seed})",
        "",
        "_All thresholds above are recorded in `plan.json`; none are hidden._",
        "",
    ]

    # Interpretation notes — the two fixed wordings.
    lines += [
        "## Interpretation notes",
        "",
        f"> {_DISCLAIMER}",
        "",
        "**On cross-method NMI:** high cross-method NMI means the methods produce a "
        "consistent partition under the *current input and parameters* — evidence of "
        "consensus stability, **not** biological correctness. NMI can be insensitive "
        "to cluster-count differences and to hierarchical correspondence between "
        "resolutions, and a bias shared across all methods can inflate it. Low "
        "cross-NMI indicates the members disagree, which may warrant refining the "
        "operator or the member pool.",
        "",
        f"_Audit_: A path; namespace `analysis://typed/{run.run_id}`. "
        f"Do not strip the `{banner}` banner — it is enforced by ADR 0010.",
    ]
    return "\n".join(lines)
