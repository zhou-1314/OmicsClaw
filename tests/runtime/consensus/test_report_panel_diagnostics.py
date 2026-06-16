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


def test_failed_members_timeout_hint_vs_crash_cause() -> None:
    # B4 (codex review): a timeout failure gets the --timeout hint; a crash (e.g.
    # missing scvi-tools) surfaces the actual error as a cause, NOT the timeout hint.
    from omicsclaw.runtime.consensus.report import _failed_members_section

    class _SR:
        def error_text(self, default="", tail_chars=None):
            return "Traceback (most recent call last):\nModuleNotFoundError: No module named 'scvi'"

    def _step(name):
        return SimpleNamespace(name=name)

    failed = [
        SimpleNamespace(step=_step("scvi_timeout"), status="timeout",
                        error="exceeded 600.0s", skill_result=None),
        SimpleNamespace(step=_step("scvi_missing"), status="failed",
                        error="skill exit_code=1", skill_result=_SR()),
    ]
    run = SimpleNamespace(
        team_result=SimpleNamespace(failed=failed), missing_label_members=[]
    )
    text = "\n".join(_failed_members_section(run))
    # timeout member -> actionable --timeout hint
    assert "larger `--timeout`" in text
    # crashed member -> the real cause (missing dep), not a timeout hint
    assert "ModuleNotFoundError" in text and "scvi" in text
    # the crash must not be mislabelled with the timeout hint
    crash_block = text.split("scvi_missing", 1)[1]
    assert "--timeout" not in crash_block


def test_no_flag_for_legitimate_reorganisation() -> None:
    # scanorama on panc8: low knn (0.33) but only moderate mixing (0.35 < 0.5) —
    # legitimate reorganisation, NOT over-integration. Must NOT be flagged.
    lines = _panel_diagnostics_section(_run({
        "scanorama": {"ilisi_norm": 0.353, "knn_preservation_norm": 0.334},
    }))
    text = "\n".join(lines)
    assert "⚠️" not in text  # no warning fired
    assert "No over-integration pattern" in text
