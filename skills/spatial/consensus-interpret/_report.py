"""Report + JSON structure for the interpreted layer.

Banner enforcement happens here exclusively — no other module renders
the interpreted_report.md first line. Mirrors `runtime/consensus/report.py`
discipline for the typed report's banner.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from _llm import ClusterAnnotation, NextStep
    from _run_reader import TypedRunBundle


SCHEMA_VERSION = "0.1"
_DEFAULT_NMI_CONTRADICTION_THRESHOLD = 0.65


# --------------------------------------------------------------------------- #
# Markdown report                                                             #
# --------------------------------------------------------------------------- #

def format_interpreted_report(
    *,
    bundle: "TypedRunBundle",
    annotations: "list[ClusterAnnotation]",
    next_steps: "list[NextStep]",
    banner: str,
) -> str:
    """Render the interpreted report markdown.

    First line is exactly ``banner`` — Slice 6 invariant lock.
    Audit footer cites both ``analysis://typed/<run_id>`` (evidence
    base) and ``analysis://interpreted/<run_id>`` (this run).
    """
    run_id = bundle.plan.get("run_id") or bundle.typed_run_dir.name
    operator = bundle.plan.get("operator", "?")

    lines: list[str] = [banner, ""]
    lines += [
        f"# Interpreted consensus — {run_id} ({operator})",
        "",
        f"- Source typed run: `{bundle.typed_run_dir}`",
        f"- Adata: `{bundle.adata_path}`",
        f"- Consensus operator: `{operator}`",
        f"- Total clusters in consensus: **{bundle.consensus_labels[bundle.consensus_label_column].nunique()}**",
        f"- Clusters interpreted: **{sum(1 for a in annotations if a.cell_type != 'Unknown')}**",
        f"- Clusters Unknown / low confidence: **{sum(1 for a in annotations if a.cell_type == 'Unknown')}**",
        "",
    ]

    if not annotations:
        lines += [
            "## Structural summary",
            "",
            "_No LLM annotations available (degrade mode or zero clusters interpretable)._",
            "",
            "Cluster sizes:",
            "",
            "```",
            bundle.consensus_labels[bundle.consensus_label_column].value_counts().sort_index().to_string(),
            "```",
            "",
        ]
    else:
        lines += ["## Per-cluster annotations", ""]
        for a in annotations:
            anchor = f"cluster-{a.cluster_id}"
            lines += [
                f"### Cluster {a.cluster_id} — {a.cell_type} (confidence {a.confidence:.2f}) {{#" + anchor + "}}",
                "",
                f"- Cells: {a.n_cells}",
            ]
            if a.evidence_markers:
                marker_strs = [
                    f"`{m.gene}` (DE rank {m.de_rank}, source={m.db_source}, weight={m.weight:.2f})"
                    for m in a.evidence_markers
                ]
                lines.append(f"- Evidence markers: {', '.join(marker_strs)}")
            lines += ["", a.narrative or "_(no narrative)_", ""]

    if next_steps:
        lines += ["## Suggested follow-ups (evidence-tied)", ""]
        for i, ns in enumerate(next_steps, start=1):
            evidence_block = "\n".join(f"   - `{ref}`" for ref in ns.evidence_refs)
            lines += [
                f"{i}. **{ns.skill}** (priority {ns.priority}) — `{ns.args_hint}`",
                "   Evidence:",
                evidence_block,
                f"   Reason: {ns.reason}",
                "",
            ]

    lines += [
        "## Audit",
        "",
        f"- Evidence base: `analysis://typed/{run_id}`  ← read this if you only trust statistically verified output",
        f"- This interpretation: `analysis://interpreted/{run_id}`",
        f"- Banner: `{banner}` (enforced; do not strip — ADR 0012)",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Machine-readable JSON                                                       #
# --------------------------------------------------------------------------- #

def format_assignments_json(
    *,
    bundle: "TypedRunBundle",
    annotations: "list[ClusterAnnotation]",
    next_steps: "list[NextStep]",
    banner: str,
) -> dict:
    """Return interpreted_assignments.json structure (schema_version=0.1)."""
    run_id = bundle.plan.get("run_id") or bundle.typed_run_dir.name
    operator = bundle.plan.get("operator", "?")

    clusters_data: list[dict] = []
    for a in annotations:
        status = "interpreted" if a.cell_type not in ("Unknown", "") else "low_confidence"
        clusters_data.append({
            "id": a.cluster_id,
            "n_cells": a.n_cells,
            "interpretation_status": status,
            "cell_type": a.cell_type,
            "confidence": a.confidence,
            "evidence": {
                "markers": [
                    {
                        "gene": m.gene,
                        "de_rank": m.de_rank,
                        "db_source": m.db_source,
                        "db_celltype": m.db_celltype,
                        "weight": m.weight,
                    }
                    for m in a.evidence_markers
                ],
            },
            "narrative_md_anchor": f"#cluster-{a.cluster_id}",
            "narrative": a.narrative,
        })

    next_steps_data: list[dict] = [
        {
            "skill": ns.skill,
            "args_hint": ns.args_hint,
            "priority": ns.priority,
            "evidence_refs": list(ns.evidence_refs),
            "reason": ns.reason,
        }
        for ns in next_steps
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "typed_run_id": run_id,
        "evidence_base_namespace": f"analysis://typed/{run_id}",
        "interpreted_namespace": f"analysis://interpreted/{run_id}",
        "banner": banner,
        "operator": operator,
        "clusters": clusters_data,
        "next_steps": next_steps_data,
    }


# --------------------------------------------------------------------------- #
# Contradiction regions                                                       #
# --------------------------------------------------------------------------- #

def format_contradiction_regions(
    bundle: "TypedRunBundle",
    *,
    threshold: float = _DEFAULT_NMI_CONTRADICTION_THRESHOLD,
) -> pd.DataFrame:
    """Return rows where pair-wise cross-method NMI < threshold."""
    nmi = bundle.nmi_matrix
    if nmi.empty:
        return pd.DataFrame(columns=["member_i", "member_j", "nmi"])

    cols = list(nmi.columns)
    rows: list[dict] = []
    for i, mi in enumerate(cols):
        for mj in cols[i + 1:]:
            try:
                v = float(nmi.loc[mi, mj])
            except (KeyError, ValueError):
                continue
            if v < threshold:
                rows.append({"member_i": mi, "member_j": mj, "nmi": v})

    rows.sort(key=lambda r: r["nmi"])
    return pd.DataFrame(rows, columns=["member_i", "member_j", "nmi"])
