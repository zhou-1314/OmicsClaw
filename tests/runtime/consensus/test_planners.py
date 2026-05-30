"""Tests for the member planners + CONSENSUS_SOURCES registry (ADR 0016 T4).

Each planner must reproduce the v1 wrapper's ``_plan_members`` behaviour for
the same args (explicit / --all / default).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omicsclaw.runtime.consensus.planners import ChairLLMPlanner, SweepPlanner
from omicsclaw.runtime.consensus.sources import CONSENSUS_SOURCES

DOMAINS = CONSENSUS_SOURCES["consensus-domains"]
SC = CONSENSUS_SOURCES["sc-consensus-clustering"]


def _args(**kw) -> SimpleNamespace:
    base = dict(
        members=None,
        all=False,
        query="",
        cluster_methods="leiden",
        resolutions="0.5,0.8,1.0,1.4,2.0",
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------- registry shape -------------------------------- #

def test_registry_has_two_flavours_keyed_by_name() -> None:
    assert set(CONSENSUS_SOURCES) == {"consensus-domains", "sc-consensus-clustering"}
    assert DOMAINS.member_skill == "spatial-domains"
    assert SC.member_skill == "sc-clustering"
    assert isinstance(DOMAINS.planner, ChairLLMPlanner)
    assert isinstance(SC.planner, SweepPlanner)
    assert DOMAINS.template == "categorical" and SC.template == "categorical"


# --------------------------- SweepPlanner (sc) ----------------------------- #

def test_sweep_default_five_resolutions() -> None:
    members = SweepPlanner().propose(_args(), source=SC)
    assert [m.name for m in members] == [
        "leiden_resolution-0.5",
        "leiden_resolution-0.8",
        "leiden_resolution-1.0",
        "leiden_resolution-1.4",
        "leiden_resolution-2.0",
    ]
    assert all(m.skill_name == "sc-clustering" for m in members)
    assert members[0].params == {"cluster-method": "leiden", "resolution": "0.5"}


def test_sweep_all_is_leiden_and_louvain_cartesian() -> None:
    members = SweepPlanner().propose(_args(all=True), source=SC)
    assert len(members) == 10
    assert {m.params["cluster-method"] for m in members} == {"leiden", "louvain"}


def test_sweep_explicit_two_members() -> None:
    members = SweepPlanner().propose(
        _args(members="leiden:resolution=0.5,louvain:resolution=1.0"), source=SC
    )
    assert [m.name for m in members] == ["leiden_resolution-0.5", "louvain_resolution-1.0"]


def test_sweep_explicit_requires_colon() -> None:
    # Migrated from the old sc smoke test: a bare method (no resolution) is rejected.
    with pytest.raises(SystemExit, match="Invalid member"):
        SweepPlanner().propose(_args(members="just_method_no_colon"), source=SC)


# --------------------------- ChairLLMPlanner (domains) --------------------- #

def test_chair_explicit_allows_bare_method() -> None:
    members = ChairLLMPlanner().propose(
        _args(members="banksy,leiden:resolution=0.5"), source=DOMAINS
    )
    assert members[0].name == "banksy"
    assert members[0].params == {"method": "banksy"}
    assert members[1].name == "leiden_resolution-0.5"
    assert all(m.skill_name == "spatial-domains" for m in members)


def test_chair_explicit_multi_param_semicolon() -> None:
    # Migrated from the old consensus-domains smoke test: ``method:k=v;k=v``.
    members = ChairLLMPlanner().propose(
        _args(members="banksy,leiden:resolution=0.5;spatial-weight=0.7"), source=DOMAINS
    )
    assert [m.name for m in members] == ["banksy", "leiden_resolution-0.5_spatial-weight-0.7"]
    assert members[1].params["resolution"] == "0.5"
    assert members[1].params["spatial-weight"] == "0.7"


def test_chair_explicit_duplicate_raises() -> None:
    with pytest.raises(SystemExit, match="duplicate"):
        ChairLLMPlanner().propose(_args(members="banksy,banksy"), source=DOMAINS)


def test_chair_all_from_param_hints() -> None:
    members = ChairLLMPlanner().propose(_args(all=True), source=DOMAINS)
    assert len(members) >= 1
    assert all(m.skill_name == "spatial-domains" for m in members)
    # --all uses param_hints method names directly as member names.
    assert all(m.params == {"method": m.name} for m in members)


def test_chair_default_offline_returns_members() -> None:
    members = ChairLLMPlanner().propose(_args(), source=DOMAINS)
    assert len(members) >= 1
    assert len(members) <= 5
    assert all(m.skill_name == "spatial-domains" for m in members)
