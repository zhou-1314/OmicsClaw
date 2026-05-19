"""Markdown rendering for a ``TypedConsensusRun``.

The banner is the first line of every report — non-configurable per
ADR 0010. Callers pass only a domain-specific ``title`` (e.g. "Verified
consensus — spatial domains"). Sections are stable across thin skills so
downstream parsers (graph memory writer, paper-figure extractor) can rely
on the structure.
"""

from __future__ import annotations

from omicsclaw.runtime.consensus.dispatch import output_banner
from omicsclaw.runtime.consensus.driver import TypedConsensusRun


def format_typed_report(run: TypedConsensusRun, *, title: str) -> str:
    """Render a verified-consensus markdown report from a ``TypedConsensusRun``.

    The first line is always ``[A: Verified consensus]`` regardless of caller
    intent — banner enforcement lives here so no thin skill can render an
    A-path report without it.
    """
    banner = output_banner("typed")
    lines: list[str] = [
        banner,
        "",
        f"# {title} ({run.operator})",
        "",
        f"- members fanned out: **{run.team_result.total}**",
        f"- members surviving: **{run.team_result.n_survived}**",
        f"- members entering consensus (BC): **{len(run.selected_bcs)}**",
        f"- consensus clusters returned: **{run.consensus.n_clusters_returned}**",
        f"- operator: `{run.operator}` (seed={run.consensus.seed})",
        "",
        "## Base clusterings",
        "",
        "| member | composite | cross_NMI | intrinsic | max_class_frac | filtered |",
        "|---|---|---|---|---|---|",
    ]
    selected_set = set(run.selected_bcs)
    for s in run.scores:
        chk = "✓" if s.member in selected_set else ""
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
        run.nmi_matrix.round(3).to_string(),
        "```",
        "",
        f"_Audit_: A path; namespace `analysis://typed/{run.run_id}`. "
        f"Do not strip the `{banner}` banner — it is enforced by ADR 0010.",
    ]
    return "\n".join(lines)
