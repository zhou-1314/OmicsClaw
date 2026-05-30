"""Smoke tests for the generic consensus entry ``run.py`` (ADR 0016 T6).

Stubs ``omicsclaw.skill.runner.run_skill`` (the late-imported default runner
inside ``fan_out``) so the whole typed pipeline runs without real subprocesses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from omicsclaw.runtime.consensus import run as run_mod


def _spatial_stub(label_arrays: dict[str, list[int]]):
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


def test_run_entry_consensus_domains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omicsclaw.skill.runner.run_skill", _spatial_stub(_LABELS))
    out = tmp_path / "out"
    rc = run_mod.main(
        [
            "--source", "consensus-domains",
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
    # Title comes from the source row, not a hardcoded wrapper string.
    assert "spatial domains" in (out / "report.md").read_text()


def test_run_entry_unknown_source_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit):
        run_mod.main(["--source", "nope", "--output", "/tmp/x"])
