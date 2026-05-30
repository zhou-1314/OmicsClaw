"""Smoke tests for the sc-consensus-clustering shim (ADR 0016).

The shim forwards to ``omicsclaw.runtime.consensus.run``; ``run_skill`` is stubbed
via the late-imported ``omicsclaw.skill.runner.run_skill`` inside ``fan_out``.
Sweep / explicit member planning is covered by
``tests/runtime/consensus/test_planners.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))


def _make_stub_runner() -> object:
    class _StubResult:
        exit_code = 0

    def _runner(**kwargs):
        out = Path(kwargs["output_dir"])
        figure_dir = out / "figure_data"
        figure_dir.mkdir(parents=True, exist_ok=True)
        extra = kwargs.get("extra_args", [])
        method = "leiden"
        for i, tok in enumerate(extra):
            if tok == "--cluster-method" and i + 1 < len(extra):
                method = extra[i + 1]
                break
        labels = [0, 0, 0, 1, 1, 1, 2, 2, 2]
        pd.DataFrame(
            {
                "cell_id": [f"cell_{i}" for i in range(len(labels))],
                "embedding_key": "X_umap",
                "coord1": list(range(len(labels))),
                "coord2": list(range(len(labels))),
                method: labels,
            }
        ).to_csv(figure_dir / "embedding_points.csv", index=False)
        pd.DataFrame(
            [{"metric": "n_cells", "value": len(labels)}, {"metric": "silhouette_score", "value": 0.55}]
        ).to_csv(figure_dir / "clustering_summary.csv", index=False)
        return _StubResult()

    return _runner


def test_consensus_sc_writes_report_and_labels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sc_consensus_clustering as scc  # type: ignore[import-not-found]

    monkeypatch.setattr("omicsclaw.skill.runner.run_skill", _make_stub_runner())

    out = tmp_path / "out"
    rc = scc.main(
        [
            "--input", str(tmp_path / "fake.h5ad"),
            "--output", str(out),
            "--resolutions", "0.5,1.0,1.4",
            "--cluster-methods", "leiden",
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
    audit = json.loads((out / "plan.json").read_text())
    assert audit["operator"] == "kmode"
    assert {m["name"] for m in audit["members"]} == {
        "leiden_resolution-0.5",
        "leiden_resolution-1.0",
        "leiden_resolution-1.4",
    }


def test_sc_now_honors_confirm_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR 0016 bug fix: sc-consensus-clustering used to *parse* ``--confirm-plan``
    but never invoke the gate. The generic entry honours it for every flavour, so
    a 'no' at the prompt now aborts (exit 130)."""
    import sc_consensus_clustering as scc  # type: ignore[import-not-found]

    class _TTY:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", _TTY())
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")

    rc = scc.main(["--output", str(tmp_path / "out"), "--confirm-plan"])
    assert rc == 130  # aborted at the confirm gate — proves the flag is now honoured
