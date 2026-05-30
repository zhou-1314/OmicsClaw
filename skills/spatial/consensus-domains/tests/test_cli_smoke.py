"""Smoke tests for the consensus-domains shim (ADR 0016).

The shim forwards to ``omicsclaw.runtime.consensus.run``; the underlying
``run_skill`` is stubbed (via the late-imported ``omicsclaw.skill.runner.run_skill``
inside ``fan_out``) so the typed pipeline runs without real subprocesses.
Explicit ``--members`` parsing is covered by ``tests/runtime/consensus/test_planners.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))


def _make_stub_runner(label_arrays: dict[str, list[int]]) -> object:
    """Fake ``run_skill`` — writes figure_data/spatial_full.csv + summary.json."""

    class _StubResult:
        exit_code = 0

    def _runner(**kwargs):
        out = Path(kwargs["output_dir"])
        figure_dir = out / "figure_data"
        figure_dir.mkdir(parents=True, exist_ok=True)
        extra = kwargs.get("extra_args", [])
        method = None
        for i, tok in enumerate(extra):
            if tok == "--method" and i + 1 < len(extra):
                method = extra[i + 1]
                break
        method = method or out.name
        labels = label_arrays.get(method) or label_arrays.get(out.name) or [0, 0, 1, 1]
        pd.DataFrame(
            {"observation": [f"obs_{i}" for i in range(len(labels))], "spatial_domain": labels}
        ).to_csv(figure_dir / "spatial_full.csv", index=False)
        (out / "summary.json").write_text(json.dumps({"summary": {"mean_local_purity": 0.7}}))
        return _StubResult()

    return _runner


_LABELS = {
    "banksy": [0, 0, 0, 1, 1, 1, 2, 2, 2],
    "graphst": [0, 0, 0, 1, 1, 1, 2, 2, 2],
    "leiden": [0, 0, 0, 1, 1, 1, 2, 2, 2],
}


def test_consensus_domains_writes_report_and_labels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import consensus_domains as cd  # type: ignore[import-not-found]

    monkeypatch.setattr("omicsclaw.skill.runner.run_skill", _make_stub_runner(_LABELS))

    out = tmp_path / "out"
    rc = cd.main(
        [
            "--input", str(tmp_path / "fake.h5ad"),
            "--output", str(out),
            "--members", "banksy,graphst,leiden",
            "--non-interactive",
            "--operator", "kmode",
            "--seed", "0",
        ]
    )
    assert rc == 0
    for name in ("report.md", "consensus_labels.tsv", "member_scores.csv", "cross_method_nmi.csv", "plan.json"):
        assert (out / name).exists(), f"missing {name}"
    banner = (out / "report.md").read_text().splitlines()[0]
    assert banner.startswith("[A: Verified consensus]")


def test_lca_unavailable_returns_clean_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """I1 regression: --operator lca with R missing must print a readable hint
    and exit non-zero, NOT bleed a Python traceback."""
    import consensus_domains as cd  # type: ignore[import-not-found]
    from omicsclaw.runtime.consensus.operators.lca_r import LCAUnavailableError

    monkeypatch.setattr("omicsclaw.skill.runner.run_skill", _make_stub_runner(_LABELS))

    def _raise_unavailable(*args, **kwargs):
        raise LCAUnavailableError("Rscript not found on PATH; install r-dicer.")

    monkeypatch.setattr(
        "omicsclaw.runtime.consensus.operators.lca_r.lca_consensus", _raise_unavailable
    )

    rc = cd.main(
        [
            "--input", str(tmp_path / "fake.h5ad"),
            "--output", str(tmp_path / "out"),
            "--members", "banksy,graphst,leiden",
            "--non-interactive",
            "--operator", "lca",
            "--seed", "0",
        ]
    )
    captured = capsys.readouterr()
    assert rc != 0
    assert "Traceback" not in (captured.err + captured.out)
    assert "LCA" in captured.err or "lca" in captured.err.lower()
