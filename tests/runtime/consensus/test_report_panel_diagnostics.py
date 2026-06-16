"""Tests for the report's intrinsic-panel diagnostics section (ADR 0029).

The integration panel scores on ``ilisi_norm`` only; ``knn_preservation_norm`` is
a weight-0 diagnostic whose job is to *flag over-integration in the report*. These
tests lock that the section renders the metrics and fires (only) on a genuine
over-integration pattern (high mixing + low within-batch structure).
"""

from __future__ import annotations

from types import SimpleNamespace

from omicsclaw.runtime.consensus.report import _panel_diagnostics_section


def _run(raw):
    return SimpleNamespace(intrinsic_panel_raw=raw)


def test_no_section_when_no_panel() -> None:
    assert _panel_diagnostics_section(_run({})) == []


def test_section_renders_per_member_metrics() -> None:
    lines = _panel_diagnostics_section(_run({
        "harmony": {"ilisi_norm": 0.27, "knn_preservation_norm": 0.90},
        "scanorama": {"ilisi_norm": 0.35, "knn_preservation_norm": 0.33},
    }))
    text = "\n".join(lines)
    assert "## Intrinsic panel diagnostics" in text
    assert "ilisi_norm" in text and "knn_preservation_norm" in text
    assert "harmony" in text and "scanorama" in text


def test_over_integration_flag_fires_on_high_mix_low_structure() -> None:
    lines = _panel_diagnostics_section(_run({
        "overmix": {"ilisi_norm": 0.9, "knn_preservation_norm": 0.2},
    }))
    text = "\n".join(lines)
    assert "over-integration" in text and "`overmix`" in text


def test_no_flag_for_legitimate_reorganisation() -> None:
    # scanorama on panc8: low knn (0.33) but only moderate mixing (0.35 < 0.5) —
    # legitimate reorganisation, NOT over-integration. Must NOT be flagged.
    lines = _panel_diagnostics_section(_run({
        "scanorama": {"ilisi_norm": 0.353, "knn_preservation_norm": 0.334},
    }))
    text = "\n".join(lines)
    assert "⚠️" not in text  # no warning fired
    assert "No over-integration pattern" in text
