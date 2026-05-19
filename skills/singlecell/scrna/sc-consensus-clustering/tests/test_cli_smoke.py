"""Smoke tests for the sc-consensus-clustering CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))


def _make_stub_runner(member_labels: dict[str, list[int]]) -> object:
    class _StubResult:
        exit_code = 0

    def _runner(**kwargs):
        out = Path(kwargs["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        figure_dir = out / "figure_data"
        figure_dir.mkdir(parents=True, exist_ok=True)
        # Resolve cluster method from --cluster-method extra arg
        extra = kwargs.get("extra_args", [])
        method = "leiden"
        for i, tok in enumerate(extra):
            if tok == "--cluster-method" and i + 1 < len(extra):
                method = extra[i + 1]
                break
        labels = member_labels.get(out.name) or [0, 0, 1, 1]
        emb = pd.DataFrame(
            {
                "cell_id": [f"cell_{i}" for i in range(len(labels))],
                "embedding_key": "X_umap",
                "coord1": list(range(len(labels))),
                "coord2": list(range(len(labels))),
                method: labels,
            }
        )
        emb.to_csv(figure_dir / "embedding_points.csv", index=False)
        clustering_summary = pd.DataFrame(
            [
                {"metric": "n_cells", "value": len(labels)},
                {"metric": "silhouette_score", "value": 0.55},
            ]
        )
        clustering_summary.to_csv(figure_dir / "clustering_summary.csv", index=False)
        return _StubResult()

    return _runner


def test_consensus_sc_writes_report_and_labels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sc_consensus_clustering as scc  # type: ignore[import-not-found]
    from omicsclaw.runtime.consensus import driver as driver_mod
    real_run_typed = driver_mod.run_typed_consensus

    member_labels = {
        "leiden_resolution-0.5":  [0, 0, 0, 1, 1, 1, 2, 2, 2],
        "leiden_resolution-1.0":  [0, 0, 0, 1, 1, 1, 2, 2, 2],
        "leiden_resolution-1.4":  [0, 0, 0, 1, 1, 1, 2, 2, 2],
    }
    stub_runner = _make_stub_runner(member_labels)

    async def patched(*args, **kwargs):
        kwargs.setdefault("runner", stub_runner)
        return await real_run_typed(*args, **kwargs)

    monkeypatch.setattr("sc_consensus_clustering.run_typed_consensus", patched)

    argv = [
        "--input",
        str(tmp_path / "fake.h5ad"),
        "--output",
        str(tmp_path / "out"),
        "--resolutions",
        "0.5,1.0,1.4",
        "--cluster-methods",
        "leiden",
        "--non-interactive",
        "--operator",
        "kmode",
        "--seed",
        "0",
    ]
    rc = scc.main(argv)
    assert rc == 0
    out = tmp_path / "out"
    assert (out / "report.md").exists()
    assert (out / "consensus_labels.tsv").exists()
    assert (out / "member_scores.csv").exists()
    assert (out / "cross_method_nmi.csv").exists()
    assert (out / "plan.json").exists()
    banner = (out / "report.md").read_text().splitlines()[0]
    assert banner.startswith("[A: Verified consensus]")
    audit = json.loads((out / "plan.json").read_text())
    assert audit["operator"] == "kmode"
    assert {m["name"] for m in audit["members"]} == set(member_labels.keys())


def test_members_from_explicit_parses_resolutions(tmp_path: Path) -> None:
    import sc_consensus_clustering as scc  # type: ignore[import-not-found]
    members = scc._members_from_explicit("leiden:resolution=0.5,louvain:resolution=1.0")
    assert [m.name for m in members] == ["leiden_resolution-0.5", "louvain_resolution-1.0"]
    # Label-column resolution lives in ScClusteringArtifactReader now; the
    # member only carries `cluster-method` in its params.
    assert members[0].params["cluster-method"] == "leiden"
    assert members[1].params["cluster-method"] == "louvain"


def test_members_explicit_invalid_token_exits(tmp_path: Path) -> None:
    import sc_consensus_clustering as scc  # type: ignore[import-not-found]
    with pytest.raises(SystemExit, match="Invalid member"):
        scc._members_from_explicit("just_method_no_colon")


def test_members_from_sweep_default_5_resolutions(tmp_path: Path) -> None:
    import sc_consensus_clustering as scc  # type: ignore[import-not-found]
    members = scc._members_from_sweep(["leiden"], [0.5, 0.8, 1.0, 1.4, 2.0])
    assert len(members) == 5
    assert members[0].name == "leiden_resolution-0.5"
    assert members[-1].name == "leiden_resolution-2.0"
