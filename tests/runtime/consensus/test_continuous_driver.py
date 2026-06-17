"""End-to-end tests for ``run_continuous_consensus`` (ADR 0031 driver)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from omicsclaw.runtime.consensus.member import ConsensusMember
from omicsclaw.runtime.consensus.source_registry import ConsensusSource


# --------------------------------------------------------------------------- #
# helpers — synthetic source + stub runner (mirrors test_driver.py)           #
# --------------------------------------------------------------------------- #

@dataclass
class _StubResult:
    exit_code: int = 0


class _PseudotimeStubReader:
    """Returns a per-member pseudotime vector written by ``_stub_runner``."""

    def __init__(self, pt_by_member: dict[str, np.ndarray]):
        self._pt = pt_by_member

    def read_labels(self, member: ConsensusMember, output_root: Path) -> pd.Series | None:
        arr = self._pt.get(member.name)
        if arr is None:
            return None
        idx = [f"obs_{i}" for i in range(len(arr))]
        return pd.Series(np.asarray(arr, dtype=float), index=idx, name="pseudotime")

    def read_intrinsic_quality(self, member: ConsensusMember, output_root: Path) -> float:
        return 0.0


def _stub_runner(**kwargs):
    out = Path(kwargs["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    return _StubResult(exit_code=0)


def _members(names: list[str]) -> list[ConsensusMember]:
    return [ConsensusMember(name=n, skill_name="sc-pseudotime", params={"method": n}) for n in names]


def _source(pt_by_member) -> ConsensusSource:
    return ConsensusSource(reader=_PseudotimeStubReader(pt_by_member), template="continuous")


def _pick_all(scores, k):
    return [s.member for s in scores if not s.filtered]


async def _run(pt_by_member, tmp_path, *, operator="median", bc=_pick_all):
    from omicsclaw.runtime.consensus.continuous_driver import run_continuous_consensus

    return await run_continuous_consensus(
        members=_members(list(pt_by_member.keys())),
        source=_source(pt_by_member),
        input_path=str(tmp_path / "data.h5ad"),
        output_dir=tmp_path / "out",
        operator=operator,
        bc_selector=bc,
        seed=0,
        plan_audit={"run_id": "ptrun", "operator": operator},
        runner=_stub_runner,
    )


# --------------------------------------------------------------------------- #
# happy path                                                                  #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_happy_path_writes_banner_and_artifacts(tmp_path: Path):
    from omicsclaw.runtime.consensus.continuous_driver import ContinuousConsensusRun
    from omicsclaw.runtime.consensus.continuous_report import format_continuous_report

    n = 50
    base = np.arange(n, dtype=float)
    rng = np.random.default_rng(0)
    pt = {
        "dpt": base,
        "palantir": base + rng.normal(0, 0.3, n),
        "via": base * 2.0 + 5.0,                 # monotone reparam -> same ranks
    }
    run = await _run(pt, tmp_path)

    assert isinstance(run, ContinuousConsensusRun)
    assert run.team_result.n_survived == 3
    assert run.operator == "median"
    # consensus pseudotime is a clean [0,1] vector
    assert run.consensus.pseudotime.min() == 0.0 and run.consensus.pseudotime.max() == 1.0
    # agreement-only scoring: composite == agreement_mean, alpha=1/beta=0
    assert run.score_config.alpha == 1.0 and run.score_config.beta == 0.0
    assert all(s.composite == s.agreement_mean for s in run.scores)

    out = tmp_path / "out"
    for name in ("plan.json", "member_scores.csv", "member_agreement_spearman.csv",
                 "consensus_pseudotime.tsv", "selection_audit.json"):
        assert (out / name).exists(), name
    cons = pd.read_csv(out / "consensus_pseudotime.tsv", sep="\t")
    assert list(cons.columns) == ["observation", "consensus_pseudotime", "pseudotime_mad", "range"]
    assert len(cons) == n  # full cell coverage

    # AC2: report.md is written by the DRIVER (not just the CLI), banner-first.
    assert (out / "report.md").exists()
    report = (out / "report.md").read_text()
    assert report.splitlines()[0] == "[A: Verified consensus]"
    assert "analysis://typed/ptrun" in report
    # in-memory render matches (same formatter)
    md = format_continuous_report(run, title="t")
    assert md.splitlines()[0] == "[A: Verified consensus]"


@pytest.mark.asyncio
async def test_direction_safeguard_flips_anticorrelated(tmp_path: Path):
    n = 40
    base = np.arange(n, dtype=float)
    pt = {"dpt": base, "palantir": base, "via": -base}   # via is reversed
    run = await _run(pt, tmp_path)
    assert "via" in run.flipped_members
    # after the flip every voter pair agrees positively
    assert (run.agreement_matrix.loc[run.selected_bcs, run.selected_bcs].to_numpy() > 0).all()


# --------------------------------------------------------------------------- #
# hardening                                                                    #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_degenerate_member_dropped(tmp_path: Path):
    n = 30
    base = np.arange(n, dtype=float)
    pt = {"dpt": base, "palantir": base + 1.0, "flat": np.full(n, 0.7)}  # flat is constant
    run = await _run(pt, tmp_path)
    assert "flat" in run.dropped_degenerate
    assert "flat" not in run.aligned_df.columns
    assert set(run.selected_bcs) == {"dpt", "palantir"}


@pytest.mark.asyncio
async def test_fail_loud_when_too_few_nondegenerate(tmp_path: Path):
    from omicsclaw.runtime.workflow import InsufficientSurvivorsError

    n = 20
    pt = {"dpt": np.arange(n, dtype=float), "flat": np.full(n, 1.0)}  # only 1 usable
    with pytest.raises(InsufficientSurvivorsError):
        await _run(pt, tmp_path)


@pytest.mark.asyncio
async def test_weak_agreement_guard_flags_disagreement(tmp_path: Path):
    n = 60
    base = np.arange(n, dtype=float)
    rng = np.random.default_rng(1)
    # three mutually near-uncorrelated orderings -> low cohort agreement
    pt = {
        "dpt": rng.permutation(base).astype(float),
        "palantir": rng.permutation(base).astype(float),
        "via": rng.permutation(base).astype(float),
    }
    run = await _run(pt, tmp_path)
    assert run.weak_agreement["diverged"] is True
    assert run.weak_agreement["cohort_mean_spearman"] < 0.5
    assert len(run.weak_agreement["min_pair"]) == 2


@pytest.mark.asyncio
async def test_weighted_operator_runs(tmp_path: Path):
    n = 40
    base = np.arange(n, dtype=float)
    rng = np.random.default_rng(2)
    pt = {"dpt": base, "palantir": base + rng.normal(0, 0.5, n), "via": base + rng.normal(0, 0.5, n)}
    run = await _run(pt, tmp_path, operator="weighted")
    assert run.operator == "weighted"
    assert run.consensus.pseudotime.min() == 0.0 and run.consensus.pseudotime.max() == 1.0


# --------------------------------------------------------------------------- #
# full-coverage enforcement (ADR 0031 §4 — drop the member whole, not cells)  #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_partial_coverage_member_dropped_whole(tmp_path: Path):
    n = 50
    base = np.arange(n, dtype=float)
    # 'via' covers only n-1 cells (the stub reader indexes obs_0..obs_{len-1})
    pt = {"dpt": base, "palantir": base + 1.0, "via": base[: n - 1]}
    run = await _run(pt, tmp_path)
    assert "via" in run.partial_excluded
    assert set(run.selected_bcs) == {"dpt", "palantir"}
    # the consensus keeps the FULL reference cell count, not the intersection
    assert len(run.consensus.pseudotime) == n
    cons = pd.read_csv(tmp_path / "out" / "consensus_pseudotime.tsv", sep="\t")
    assert len(cons) == n


@pytest.mark.asyncio
async def test_nonfinite_member_dropped_whole(tmp_path: Path):
    n = 40
    base = np.arange(n, dtype=float)
    bad = base.copy()
    bad[5] = np.nan  # full coverage but a non-finite value -> drop whole
    pt = {"dpt": base, "palantir": base + 1.0, "via": bad}
    run = await _run(pt, tmp_path)
    assert "via" in run.partial_excluded
    assert len(run.consensus.pseudotime) == n


@pytest.mark.asyncio
async def test_too_few_full_coverage_members_raises(tmp_path: Path):
    from omicsclaw.runtime.workflow import InsufficientSurvivorsError

    n = 30
    base = np.arange(n, dtype=float)
    # only 'dpt' is full-coverage; 'via' is partial -> < 2 usable -> fail loud
    pt = {"dpt": base, "via": base[: n - 1]}
    with pytest.raises(InsufficientSurvivorsError):
        await _run(pt, tmp_path)
