"""Orchestrate the 5 interpreted-layer artifact writes per ADR 0012.

T3 invariants enforced BEFORE any disk write — no partial output ever
hits disk if the interpreted_assignments dict would violate banner /
marker_grounding / evidence_refs invariants.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from _invariants import enforce_interpreted_invariants
from _report import (
    format_assignments_json,
    format_contradiction_regions,
    format_interpreted_report,
)

if TYPE_CHECKING:
    from _llm import ClusterAnnotation, NextStep
    from _run_reader import TypedRunBundle


def write_artifacts(
    *,
    output_dir: Path | str,
    bundle: "TypedRunBundle",
    annotations: "list[ClusterAnnotation]",
    next_steps: "list[NextStep]",
    de_df: pd.DataFrame,
    audit: dict,
    banner: str,
) -> list[Path]:
    """Write the 5 interpreted-layer artifacts to ``output_dir``.

    Order
    -----
    1. Enforce T3 invariants (raise before any write).
    2. Create ``output_dir`` if missing.
    3. Write interpreted_report.md, interpreted_assignments.json,
       de_per_cluster.csv, contradiction_regions.csv, audit.json.

    Returns the list of written paths.

    Raises
    ------
    InvariantViolationError
        Any T3 violation; nothing has been written when this fires.
    """
    enforce_interpreted_invariants(
        annotations=annotations, next_steps=next_steps, banner=banner,
    )

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []

    # 1. interpreted_report.md
    report_md = format_interpreted_report(
        bundle=bundle, annotations=annotations, next_steps=next_steps, banner=banner,
    )
    p = out / "interpreted_report.md"
    p.write_text(report_md)
    written.append(p)

    # 2. interpreted_assignments.json
    assignments = format_assignments_json(
        bundle=bundle, annotations=annotations, next_steps=next_steps, banner=banner,
    )
    p = out / "interpreted_assignments.json"
    p.write_text(json.dumps(assignments, indent=2))
    written.append(p)

    # 3. de_per_cluster.csv
    p = out / "de_per_cluster.csv"
    de_df.to_csv(p, index=False)
    written.append(p)

    # 4. contradiction_regions.csv
    contradictions = format_contradiction_regions(bundle)
    p = out / "contradiction_regions.csv"
    contradictions.to_csv(p, index=False)
    written.append(p)

    # 5. audit.json
    run_id = bundle.plan.get("run_id") or bundle.typed_run_dir.name
    audit_full = {
        "typed_run_id": run_id,
        "typed_run_dir": str(bundle.typed_run_dir),
        "adata_path": str(bundle.adata_path),
        "evidence_base_namespace": f"analysis://typed/{run_id}",
        "interpreted_namespace": f"analysis://interpreted/{run_id}",
        "banner": banner,
        **audit,
    }
    p = out / "audit.json"
    p.write_text(json.dumps(audit_full, indent=2))
    written.append(p)

    return written
