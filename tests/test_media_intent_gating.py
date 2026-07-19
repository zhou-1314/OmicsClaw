"""Media intent gating (no auto-display + collapsed-summary contract).

The desktop bug: a skill / autonomous run's figures auto-appeared inline in chat
even though the user never asked to see them. The fix centralises the policy in
``build_media_delivery_plan`` — the single place that decides, from the user's
``return_media`` intent, which artifacts are queued for display (as interactive
cards) versus collapsed into an ``output_summary`` entry. These tests pin that
policy at the helper level so both the skill executor and the autonomous path
inherit it.
"""

from __future__ import annotations

from pathlib import Path

from omicsclaw.skill.orchestration import (
    build_media_delivery_plan,
    _collect_output_media_paths,
)


def _make_outputs(out_dir: Path, *, figures=("umap.png", "qc.png"), tables=("metrics.csv",),
                  reports=("report.md",), notebooks=()) -> Path:
    (out_dir / "figures").mkdir(parents=True, exist_ok=True)
    for f in figures:
        (out_dir / "figures" / f).write_bytes(b"\x89PNG\r\n")
    for t in tables:
        (out_dir / t).write_text("a,b\n1,2\n", encoding="utf-8")
    for r in reports:
        (out_dir / r).write_text("# report\n", encoding="utf-8")
    for nb in notebooks:
        (out_dir / nb).write_text("{}", encoding="utf-8")
    return out_dir


def test_no_return_media_emits_summary_not_files(tmp_path: Path):
    """Default (no return_media) → nothing is queued for display; an
    output_summary counting the un-requested artifacts is the only item."""
    out = _make_outputs(tmp_path / "run")
    collected = _collect_output_media_paths(out)
    plan = build_media_delivery_plan(collected, "", out)

    assert plan.sent_names == []
    assert plan.summary is not None
    assert plan.summary["type"] == "output_summary"
    assert plan.summary["figures"] == 2
    assert plan.summary["tables"] == 1
    assert plan.summary["notebooks"] == 0
    assert plan.summary["run_dir"] == str(out)
    # The summary is the ONLY pending item — no figure/file dicts queued.
    assert plan.pending_items == [plan.summary]


def test_return_media_all_queues_files_no_summary(tmp_path: Path):
    """return_media='all' → every artifact is queued for display, nothing left
    to summarise."""
    out = _make_outputs(tmp_path / "run")
    collected = _collect_output_media_paths(out)
    plan = build_media_delivery_plan(collected, "all", out)

    queued_names = {Path(i["path"]).name for i in plan.pending_items if i.get("path")}
    assert "umap.png" in queued_names
    assert "qc.png" in queued_names
    assert "metrics.csv" in queued_names
    assert plan.summary is None
    assert "umap.png" in plan.sent_names


def test_media_collection_rejects_claim_aliases(tmp_path: Path):
    from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME

    out = _make_outputs(tmp_path / "run", figures=("umap.png",))
    claim = out / OUTPUT_CLAIM_FILENAME
    claim.write_text("{}\n", encoding="utf-8")
    (out / "figures" / "claim.png").hardlink_to(claim)
    (out / "claim.csv").hardlink_to(claim)

    collected = _collect_output_media_paths(out)

    assert [path.name for path in collected.figure_paths] == ["umap.png"]
    assert [path.name for path in collected.table_paths] == ["metrics.csv"]
    assert not any("claim" in Path(item["path"]).name for item in collected.media_items)


def test_keyword_sends_match_and_summarises_rest(tmp_path: Path):
    """A keyword sends only matching figures as cards; the remainder become the
    collapsed summary."""
    out = _make_outputs(tmp_path / "run")
    collected = _collect_output_media_paths(out)
    plan = build_media_delivery_plan(collected, "umap", out)

    assert plan.sent_names == ["umap.png"]
    assert plan.summary is not None
    assert plan.summary["figures"] == 1  # qc.png unsent
    assert plan.summary["tables"] == 1  # metrics.csv unsent
    # umap.png queued as a real file item + the summary
    queued_names = {Path(i["path"]).name for i in plan.pending_items if i.get("path")}
    assert queued_names == {"umap.png"}
    assert plan.summary in plan.pending_items


def test_always_anchor_emits_zero_count_summary_for_textonly_run(tmp_path: Path):
    """A text-only run (only a report, no figures/tables) must still emit a
    run-dir anchor so the producing session can be stamped (本对话). The summary
    carries the run_dir with zero counts."""
    out = _make_outputs(tmp_path / "run", figures=(), tables=(), reports=("result_summary.md",))
    collected = _collect_output_media_paths(out)
    plan = build_media_delivery_plan(collected, "", out, always_anchor=True)

    assert plan.summary is not None
    assert plan.summary["figures"] == 0
    assert plan.summary["tables"] == 0
    assert plan.summary["run_dir"] == str(out)
    assert plan.pending_items == [plan.summary]


def test_no_anchor_no_artifacts_is_empty(tmp_path: Path):
    """Without always_anchor and with no figures/tables/notebooks, the plan is
    empty (a report-only skill run leaves no chat artifact)."""
    out = _make_outputs(tmp_path / "run", figures=(), tables=(), reports=("report.md",))
    collected = _collect_output_media_paths(out)
    plan = build_media_delivery_plan(collected, "", out, always_anchor=False)

    assert plan.summary is None
    assert plan.pending_items == []
    assert plan.sent_names == []
