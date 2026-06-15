"""Tests for the multi-metric spatial intrinsic panel (chaos/pas/mlami)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from omicsclaw.runtime.consensus.member import ConsensusMember
from omicsclaw.runtime.consensus.spatial_panel import (
    DEFAULT_PANEL_WEIGHTS,
    combine_panel,
    intrinsic_spatial_panel,
)


# --------------------------- combine_panel (unit) -------------------------- #

def test_combine_perfect_and_worst() -> None:
    assert combine_panel({"chaos": 1.0, "pas": 0.0, "mlami": 1.0}) == 1.0
    assert combine_panel({"chaos": 0.0, "pas": 1.0, "mlami": 0.0}) == 0.0


def test_combine_flips_lower_better_pas() -> None:
    # pas is lower-better: pas=0 -> 1.0 contribution; only pas present.
    assert combine_panel({"pas": 0.0}) == 1.0
    assert combine_panel({"pas": 1.0}) == 0.0


def test_combine_clips_out_of_range_inputs() -> None:
    # raw outside [0,1] is clipped before weighting (no negative credit, no >1).
    assert combine_panel({"chaos": 5.0, "pas": 0.0, "mlami": -3.0}) == pytest.approx(
        (0.4 * 1.0 + 0.2 * 1.0 + 0.4 * 0.0) / 1.0
    )


def test_combine_renormalises_over_present_metrics() -> None:
    # mlami missing -> weights renormalise over chaos+pas only.
    val = combine_panel({"chaos": 0.5, "pas": 0.5})
    expected = (0.4 * 0.5 + 0.2 * 0.5) / (0.4 + 0.2)
    assert val == pytest.approx(expected)


def test_combine_ignores_nan_and_empty() -> None:
    assert combine_panel({}) == 0.0
    assert combine_panel({"chaos": float("nan"), "mlami": float("nan")}) == 0.0


def test_combine_respects_custom_weights() -> None:
    w = {"chaos": 1.0, "pas": 0.0, "mlami": 0.0}
    assert combine_panel({"chaos": 0.3, "pas": 0.9, "mlami": 0.1}, weights=w) == pytest.approx(0.3)


# --------------------- intrinsic_spatial_panel (behavior) ------------------ #

def _square_grid(side: int) -> np.ndarray:
    xs, ys = np.meshgrid(np.arange(side), np.arange(side))
    return np.column_stack([xs.ravel(), ys.ravel()]).astype(float)


def test_panel_scalar_in_unit_interval_and_reports_metrics() -> None:
    coords = _square_grid(10)  # 100 points
    # 3 coherent horizontal bands.
    labels = (coords[:, 1] // 4).astype(int).astype(str)
    scalar, raw = intrinsic_spatial_panel(labels, coords, seed=0)
    assert 0.0 <= scalar <= 1.0
    assert set(raw).issubset({"chaos", "pas", "mlami"})
    assert "chaos" in raw  # chaos never fails on valid input


def test_panel_coherent_scores_higher_than_random() -> None:
    coords = _square_grid(12)  # 144 points
    coherent = (coords[:, 1] // 4).astype(int).astype(str)
    rng = np.random.default_rng(0)
    shuffled = rng.permutation(coherent)

    good, _ = intrinsic_spatial_panel(coherent, coords, seed=0)
    bad, _ = intrinsic_spatial_panel(shuffled, coords, seed=0)
    assert good > bad, f"coherent panel {good} should beat random {bad}"


def test_panel_failsoft_on_shape_mismatch() -> None:
    coords = _square_grid(6)  # 36 points
    labels = np.array(["a", "b", "c"])  # wrong length -> every metric raises
    scalar, raw = intrinsic_spatial_panel(labels, coords, seed=0)
    assert scalar == 0.0
    assert raw == {}


# ----------------------------- driver integration -------------------------- #

@dataclass
class _StubResult:
    exit_code: int = 0


class _BandReader:
    """Returns coherent horizontal-band labels (indexed by obs id) + a FIXED
    reader intrinsic, so a test can prove the panel REPLACED that value."""

    FIXED_INTRINSIC = 0.123

    def __init__(self, coords: np.ndarray, obs_ids: list[str]) -> None:
        self._coords = coords
        self._obs_ids = obs_ids

    def read_labels(self, member: ConsensusMember, output_root: Path) -> pd.Series | None:
        # Slightly different band widths per member so they are not identical.
        width = 3 if member.name.endswith("0") else 4
        labels = (self._coords[:, 1] // width).astype(int).astype(str)
        return pd.Series(labels, index=self._obs_ids, name="label")

    def read_intrinsic_quality(self, member: ConsensusMember, output_root: Path) -> float:
        return self.FIXED_INTRINSIC


def _members(names: list[str]) -> list[ConsensusMember]:
    return [ConsensusMember(name=n, skill_name="spatial-domains", params={"method": n}) for n in names]


def _write_spatial_h5ad(path: Path, coords: np.ndarray, obs_ids: list[str]) -> None:
    import anndata

    adata = anndata.AnnData(X=np.zeros((len(obs_ids), 2), dtype=np.float32))
    adata.obs_names = obs_ids
    adata.obsm["spatial"] = coords.astype(np.float32)
    adata.write_h5ad(path)


@pytest.mark.asyncio
async def test_driver_uses_spatial_panel_when_coords_present(tmp_path: Path) -> None:
    from omicsclaw.runtime.consensus.driver import ScoreConfig, run_typed_consensus
    from omicsclaw.runtime.consensus.source_registry import ConsensusSource

    coords = _square_grid(10)
    obs_ids = [f"obs_{i}" for i in range(coords.shape[0])]
    h5ad = tmp_path / "input.h5ad"
    _write_spatial_h5ad(h5ad, coords, obs_ids)

    members = _members(["m0", "m1", "m2"])
    source = ConsensusSource(reader=_BandReader(coords, obs_ids), domain="spatial")

    def runner(**kwargs):
        Path(kwargs["output_dir"]).mkdir(parents=True, exist_ok=True)
        return _StubResult(exit_code=0)

    run = await run_typed_consensus(
        members=members, source=source, input_path=str(h5ad),
        output_dir=tmp_path / "out", operator="kmode",
        bc_selector=lambda s, k: [x.member for x in s][:2],
        score_config=ScoreConfig(), seed=0, runner=runner,
    )

    # Panel ran: per-member raw metrics present, artifact written.
    assert set(run.intrinsic_panel_raw) == {"m0", "m1", "m2"}
    for raw in run.intrinsic_panel_raw.values():
        # chaos never fails on valid input; mlami may be fail-soft-dropped if its
        # optional scanpy dep is unavailable, so don't require it here.
        assert "chaos" in raw
    panel_csv = (tmp_path / "out" / "member_intrinsic_panel.csv")
    assert panel_csv.exists()
    cols = set(pd.read_csv(panel_csv).columns)
    assert {"member", "chaos", "pas", "mlami", "intrinsic_panel"} <= cols

    # The panel REPLACED the reader's fixed intrinsic.
    assert all(v != _BandReader.FIXED_INTRINSIC for v in run.intrinsic_map.values())
    assert all(0.0 <= v <= 1.0 for v in run.intrinsic_map.values())


@pytest.mark.asyncio
async def test_driver_no_spatial_panel_keeps_reader_intrinsic(tmp_path: Path) -> None:
    from omicsclaw.runtime.consensus.driver import ScoreConfig, run_typed_consensus
    from omicsclaw.runtime.consensus.source_registry import ConsensusSource

    coords = _square_grid(8)
    obs_ids = [f"obs_{i}" for i in range(coords.shape[0])]
    h5ad = tmp_path / "input.h5ad"
    _write_spatial_h5ad(h5ad, coords, obs_ids)

    source = ConsensusSource(reader=_BandReader(coords, obs_ids), domain="spatial")

    def runner(**kwargs):
        Path(kwargs["output_dir"]).mkdir(parents=True, exist_ok=True)
        return _StubResult(exit_code=0)

    run = await run_typed_consensus(
        members=_members(["m0", "m1"]), source=source, input_path=str(h5ad),
        output_dir=tmp_path / "out", operator="kmode",
        bc_selector=lambda s, k: [x.member for x in s][:2],
        score_config=ScoreConfig(), seed=0, runner=runner,
        use_spatial_panel=False,
    )
    # Disabled -> reader's fixed intrinsic used, no panel artifact.
    assert run.intrinsic_panel_raw == {}
    assert all(v == _BandReader.FIXED_INTRINSIC for v in run.intrinsic_map.values())
    assert not (tmp_path / "out" / "member_intrinsic_panel.csv").exists()


@pytest.mark.asyncio
async def test_driver_skips_panel_for_non_spatial_flavour_even_with_coords(
    tmp_path: Path,
) -> None:
    """A non-spatial flavour keeps its reader intrinsic even when the input has
    spatial coords — sc-clustering on a spatially-annotated dataset must NOT get
    spatial-domain metrics (gate is the flavour's domain, not coords-presence)."""
    from omicsclaw.runtime.consensus.driver import ScoreConfig, run_typed_consensus
    from omicsclaw.runtime.consensus.source_registry import ConsensusSource

    coords = _square_grid(8)
    obs_ids = [f"obs_{i}" for i in range(coords.shape[0])]
    h5ad = tmp_path / "input.h5ad"
    _write_spatial_h5ad(h5ad, coords, obs_ids)

    source = ConsensusSource(reader=_BandReader(coords, obs_ids), domain="singlecell")

    def runner(**kwargs):
        Path(kwargs["output_dir"]).mkdir(parents=True, exist_ok=True)
        return _StubResult(exit_code=0)

    run = await run_typed_consensus(
        members=_members(["m0", "m1"]), source=source, input_path=str(h5ad),
        output_dir=tmp_path / "out", operator="kmode",
        bc_selector=lambda s, k: [x.member for x in s][:2],
        score_config=ScoreConfig(), seed=0, runner=runner,  # panel default ON
    )
    # domain != 'spatial' -> panel skipped despite coords present.
    assert run.intrinsic_panel_raw == {}
    assert all(v == _BandReader.FIXED_INTRINSIC for v in run.intrinsic_map.values())
    assert not (tmp_path / "out" / "member_intrinsic_panel.csv").exists()
