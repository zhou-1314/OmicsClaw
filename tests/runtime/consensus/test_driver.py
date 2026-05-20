"""Tests for ``run_typed_consensus`` (Bundle B driver)."""

from __future__ import annotations

import json
import pandas as pd
import pytest
from dataclasses import dataclass
from pathlib import Path

from omicsclaw.runtime.consensus.member import ConsensusMember


# --------------------------------------------------------------------------- #
# helpers — synthetic source + stub runner                                    #
# --------------------------------------------------------------------------- #

@dataclass
class _StubResult:
    exit_code: int = 0


class _StubReader:
    """Reads a per-member synthetic artifact set written by ``_stub_runner``."""

    def __init__(self, label_arrays: dict[str, list[str]], intrinsic_map: dict[str, float]):
        self._labels = label_arrays
        self._intrinsic = intrinsic_map

    def read_labels(self, member: ConsensusMember, output_root: Path) -> pd.Series | None:
        arr = self._labels.get(member.name)
        if arr is None:
            return None
        idx = [f"obs_{i}" for i in range(len(arr))]
        return pd.Series(arr, index=idx, name="label").astype(str)

    def read_intrinsic_quality(self, member: ConsensusMember, output_root: Path) -> float:
        return float(self._intrinsic.get(member.name, 0.0))


def _stub_runner(**kwargs):
    """Fan-out runner double — never inspects extra_args, just succeeds."""
    out = Path(kwargs["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    return _StubResult(exit_code=0)


def _make_source(label_arrays, intrinsic_map):
    from omicsclaw.runtime.consensus.source_registry import TypedConsensusSource
    return TypedConsensusSource(reader=_StubReader(label_arrays, intrinsic_map))


def _members(names: list[str]) -> list[ConsensusMember]:
    return [ConsensusMember(name=n, skill_name="spatial-domains", params={"method": n}) for n in names]


# --------------------------------------------------------------------------- #
# Happy path                                                                  #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_driver_returns_complete_typed_consensus_run(tmp_path: Path) -> None:
    from omicsclaw.runtime.consensus.driver import (
        ScoreConfig,
        TypedConsensusRun,
        run_typed_consensus,
    )

    label_arrays = {
        "banksy":  ["A"] * 9 + ["B"] * 9 + ["C"] * 9,
        "graphst": ["A"] * 9 + ["B"] * 9 + ["C"] * 9,
        "leiden":  ["A"] * 9 + ["B"] * 9 + ["C"] * 9,
    }
    intrinsic = {"banksy": 0.81, "graphst": 0.77, "leiden": 0.65}

    members = _members(list(label_arrays.keys()))
    source = _make_source(label_arrays, intrinsic)

    def pick_all(scores, k):
        return [s.member for s in scores if not s.filtered]

    run = await run_typed_consensus(
        members=members,
        source=source,
        input_path=str(tmp_path / "data.h5ad"),
        output_dir=tmp_path / "out",
        operator="kmode",
        bc_selector=pick_all,
        score_config=ScoreConfig(),
        seed=0,
        runner=_stub_runner,
    )

    assert isinstance(run, TypedConsensusRun)
    assert run.team_result.n_survived == 3
    assert run.team_result.n_failed == 0
    assert run.labels_df.shape == (27, 3)
    assert set(run.intrinsic_map.keys()) == {"banksy", "graphst", "leiden"}
    assert len(run.selected_bcs) == 3
    assert run.consensus.n_clusters_returned == 3
    assert run.operator == "kmode"


# --------------------------------------------------------------------------- #
# Artifact writes                                                              #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_driver_writes_4_canonical_artifacts(tmp_path: Path) -> None:
    from omicsclaw.runtime.consensus.driver import ScoreConfig, run_typed_consensus

    label_arrays = {f"m{i}": ["A", "A", "B", "B"] for i in range(3)}
    intrinsic = {f"m{i}": 0.5 for i in range(3)}

    members = _members(list(label_arrays.keys()))
    source = _make_source(label_arrays, intrinsic)

    plan_audit = {"run_id": "abc123", "operator": "kmode", "members": [{"name": "m0"}]}

    run = await run_typed_consensus(
        members=members, source=source,
        input_path="/dev/null", output_dir=tmp_path,
        operator="kmode", bc_selector=lambda s, k: [x.member for x in s][:2],
        score_config=ScoreConfig(), seed=0, plan_audit=plan_audit,
        runner=_stub_runner,
    )

    for filename in ("plan.json", "consensus_labels.tsv", "member_scores.csv", "cross_method_nmi.csv"):
        assert (tmp_path / filename).exists(), f"missing artifact: {filename}"
        assert (tmp_path / filename) in run.artifacts_written

    # plan.json content is the audit dict
    assert json.loads((tmp_path / "plan.json").read_text())["run_id"] == "abc123"

    # consensus_labels.tsv has the expected columns
    consensus_df = pd.read_csv(tmp_path / "consensus_labels.tsv", sep="\t")
    assert "observation" in consensus_df.columns
    assert "consensus_kmode" in consensus_df.columns


# --------------------------------------------------------------------------- #
# Error paths                                                                 #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_driver_raises_insufficient_survivors_when_under_2(tmp_path: Path) -> None:
    from omicsclaw.runtime.consensus.driver import ScoreConfig, run_typed_consensus
    from omicsclaw.runtime.consensus.team import InsufficientSurvivorsError

    members = _members(["m0", "m1", "m2"])
    source = _make_source({m.name: ["A"] for m in members}, {m.name: 0.5 for m in members})

    def crashy(**kwargs):
        out = Path(kwargs["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        if out.name in ("m0", "m1"):
            raise RuntimeError("synthetic crash")
        return _StubResult(exit_code=0)

    with pytest.raises(InsufficientSurvivorsError):
        await run_typed_consensus(
            members=members, source=source, input_path="/dev/null",
            output_dir=tmp_path, operator="kmode",
            bc_selector=lambda s, k: [], score_config=ScoreConfig(),
            seed=0, runner=crashy,
        )


@pytest.mark.asyncio
async def test_driver_raises_insufficient_bcs_when_selector_picks_fewer_than_2(tmp_path: Path) -> None:
    from omicsclaw.runtime.consensus.driver import (
        InsufficientBCsError,
        ScoreConfig,
        run_typed_consensus,
    )

    label_arrays = {f"m{i}": ["A", "B"] for i in range(3)}
    intrinsic = {f"m{i}": 0.5 for i in range(3)}
    members = _members(list(label_arrays.keys()))
    source = _make_source(label_arrays, intrinsic)

    with pytest.raises(InsufficientBCsError):
        await run_typed_consensus(
            members=members, source=source, input_path="/dev/null",
            output_dir=tmp_path, operator="kmode",
            bc_selector=lambda s, k: ["m0"],   # only 1 picked
            score_config=ScoreConfig(), seed=0, runner=_stub_runner,
        )


@pytest.mark.asyncio
async def test_driver_catches_lca_unavailable_and_reraises_cleanly(tmp_path: Path, monkeypatch) -> None:
    from omicsclaw.runtime.consensus.driver import ScoreConfig, run_typed_consensus
    from omicsclaw.runtime.consensus.operators.lca_r import LCAUnavailableError

    label_arrays = {f"m{i}": ["A"] * 4 + ["B"] * 4 for i in range(3)}
    intrinsic = {f"m{i}": 0.5 for i in range(3)}
    members = _members(list(label_arrays.keys()))
    source = _make_source(label_arrays, intrinsic)

    def _raise(*args, **kwargs):
        raise LCAUnavailableError("Rscript missing")

    monkeypatch.setattr(
        "omicsclaw.runtime.consensus.operators.lca_r.lca_consensus", _raise
    )

    with pytest.raises(LCAUnavailableError):
        await run_typed_consensus(
            members=members, source=source, input_path="/dev/null",
            output_dir=tmp_path, operator="lca",
            bc_selector=lambda s, k: [x.member for x in s][:2],
            score_config=ScoreConfig(), seed=0, runner=_stub_runner,
        )


# --------------------------------------------------------------------------- #
# format_typed_report                                                         #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_format_typed_report_starts_with_banner(tmp_path: Path) -> None:
    from omicsclaw.runtime.consensus.driver import ScoreConfig, run_typed_consensus
    from omicsclaw.runtime.consensus.report import format_typed_report

    label_arrays = {f"m{i}": ["A"] * 6 + ["B"] * 6 for i in range(3)}
    intrinsic = {f"m{i}": 0.5 for i in range(3)}
    members = _members(list(label_arrays.keys()))
    source = _make_source(label_arrays, intrinsic)

    run = await run_typed_consensus(
        members=members, source=source, input_path="/dev/null",
        output_dir=tmp_path, operator="kmode",
        bc_selector=lambda s, k: [x.member for x in s][:2],
        score_config=ScoreConfig(), seed=0, runner=_stub_runner,
    )

    md = format_typed_report(run, title="Smoke test")
    first_line = md.splitlines()[0]
    assert first_line.startswith("[A: Verified consensus]"), f"banner missing: {first_line!r}"
    assert "Smoke test" in md
    assert "## Base clusterings" in md
    assert "## Cross-method NMI" in md
