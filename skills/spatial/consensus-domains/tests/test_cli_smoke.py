"""Smoke tests for the consensus-domains CLI wrapper.

These tests stub the underlying spatial-domains runner so the consensus
skill can be exercised end-to-end without invoking real subprocesses.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))


def _make_stub_runner(output_root: Path, label_arrays: dict[str, list[int]]) -> object:
    """Return a callable that fakes ``run_skill`` by writing
    figure_data/spatial_full.csv per member."""

    class _StubResult:
        exit_code = 0

    def _runner(**kwargs):
        out = Path(kwargs["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        figure_dir = out / "figure_data"
        figure_dir.mkdir(parents=True, exist_ok=True)
        method = next(
            (a.split("=", 1)[1] if "=" in a else None for a in kwargs.get("extra_args", []) if a.startswith("--method")),
            None,
        )
        if method is None:
            extra = kwargs.get("extra_args", [])
            for i, tok in enumerate(extra):
                if tok == "--method" and i + 1 < len(extra):
                    method = extra[i + 1]
                    break
        if method is None:
            method = out.name
        labels = label_arrays.get(method) or label_arrays.get(out.name) or [0, 0, 1, 1]
        df = pd.DataFrame(
            {
                "observation": [f"obs_{i}" for i in range(len(labels))],
                "spatial_domain": labels,
            }
        )
        df.to_csv(figure_dir / "spatial_full.csv", index=False)
        (out / "summary.json").write_text(json.dumps({"summary": {"mean_local_purity": 0.7}}))
        return _StubResult()

    return _runner


def test_consensus_domains_writes_report_and_labels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Import inside the test so the SKILL_DIR-prepended sys.path takes effect.
    import consensus_domains as cd  # type: ignore[import-not-found]

    label_arrays = {
        "banksy":  [0, 0, 0, 1, 1, 1, 2, 2, 2],
        "graphst": [0, 0, 0, 1, 1, 1, 2, 2, 2],
        "leiden":  [0, 0, 0, 1, 1, 1, 2, 2, 2],
    }
    stub_runner = _make_stub_runner(tmp_path, label_arrays)
    from omicsclaw.runtime.consensus import team as team_mod
    real_run_team = team_mod.run_team

    async def patched_run_team(*args, **kwargs):
        kwargs.setdefault("runner", stub_runner)
        return await real_run_team(*args, **kwargs)

    monkeypatch.setattr("consensus_domains.run_team", patched_run_team)

    argv = [
        "--input",
        str(tmp_path / "fake.h5ad"),
        "--output",
        str(tmp_path / "out"),
        "--members",
        "banksy,graphst,leiden",
        "--non-interactive",
        "--operator",
        "kmode",
        "--seed",
        "0",
    ]
    rc = cd.main(argv)
    assert rc == 0
    out = tmp_path / "out"
    assert (out / "report.md").exists()
    assert (out / "consensus_labels.tsv").exists()
    assert (out / "member_scores.csv").exists()
    assert (out / "cross_method_nmi.csv").exists()
    assert (out / "plan.json").exists()
    banner = (out / "report.md").read_text().splitlines()[0]
    assert banner.startswith("[A: Verified consensus]")


def test_members_from_explicit_list_parses_params() -> None:
    import consensus_domains as cd  # type: ignore[import-not-found]

    members = cd._members_from_explicit_list("banksy,leiden:resolution=0.5;spatial-weight=0.7")
    assert [m.name for m in members] == ["banksy", "leiden_resolution-0.5_spatial-weight-0.7"]
    assert members[1].params["resolution"] == "0.5"
    assert members[1].params["spatial-weight"] == "0.7"


def test_members_explicit_rejects_duplicate_name() -> None:
    import consensus_domains as cd  # type: ignore[import-not-found]

    with pytest.raises(SystemExit, match="duplicate"):
        cd._members_from_explicit_list("banksy,banksy")


def test_lca_unavailable_returns_clean_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """I1 regression: --operator lca with R missing must print a readable
    install hint and exit non-zero, NOT bleed a Python traceback to the user.
    """
    import consensus_domains as cd  # type: ignore[import-not-found]
    from omicsclaw.runtime.consensus import team as team_mod
    from omicsclaw.runtime.consensus.operators.lca_r import LCAUnavailableError
    real_run_team = team_mod.run_team

    label_arrays = {
        "banksy":  [0, 0, 0, 1, 1, 1, 2, 2, 2],
        "graphst": [0, 0, 0, 1, 1, 1, 2, 2, 2],
        "leiden":  [0, 0, 0, 1, 1, 1, 2, 2, 2],
    }
    stub_runner = _make_stub_runner(tmp_path, label_arrays)

    async def patched(*args, **kwargs):
        kwargs.setdefault("runner", stub_runner)
        return await real_run_team(*args, **kwargs)

    monkeypatch.setattr("consensus_domains.run_team", patched)

    # Force lca_consensus to raise LCAUnavailableError.
    def _raise_unavailable(*args, **kwargs):
        raise LCAUnavailableError("Rscript not found on PATH; install r-dicer.")

    monkeypatch.setattr(
        "omicsclaw.runtime.consensus.operators.lca_r.lca_consensus",
        _raise_unavailable,
    )

    rc = cd.main([
        "--input", str(tmp_path / "fake.h5ad"),
        "--output", str(tmp_path / "out"),
        "--members", "banksy,graphst,leiden",
        "--non-interactive",
        "--operator", "lca",
        "--seed", "0",
    ])
    captured = capsys.readouterr()
    # Must exit non-zero (we picked 6 in the fix; just assert non-zero here).
    assert rc != 0
    # Must NOT bleed a Python traceback. We surface "LCA" in the message so the
    # user knows which operator is unavailable.
    assert "Traceback" not in (captured.err + captured.out)
    assert "LCA" in captured.err or "lca" in captured.err.lower()
