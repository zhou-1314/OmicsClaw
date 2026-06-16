"""Tests for the member planners + CONSENSUS_SOURCES registry (ADR 0016 T4).

Each planner must reproduce the v1 wrapper's ``_plan_members`` behaviour for
the same args (explicit / --all / default).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omicsclaw.runtime.consensus.planners import (
    ChairLLMPlanner,
    IntegrationRepSweepPlanner,
    SweepPlanner,
)
from omicsclaw.runtime.consensus.sources import CONSENSUS_SOURCES

DOMAINS = CONSENSUS_SOURCES["consensus-domains"]
SC = CONSENSUS_SOURCES["sc-consensus-clustering"]
INTEGRATION = CONSENSUS_SOURCES["sc-consensus-integration"]


def _args(**kw) -> SimpleNamespace:
    base = dict(
        members=None,
        all=False,
        query="",
        cluster_methods="leiden",
        resolutions="0.5,0.8,1.0,1.4,2.0",
        # IntegrationRepSweepPlanner args
        integration_methods=None,
        resolution=None,
        batch_key="batch",
        include_scvi=False,
        vote_baseline=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------- registry shape -------------------------------- #

def test_registry_has_flavours_keyed_by_name() -> None:
    assert set(CONSENSUS_SOURCES) == {
        "consensus-domains", "sc-consensus-clustering", "sc-consensus-integration",
    }
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


# ---------------------- IntegrationRepSweepPlanner ------------------------- #

def test_integration_default_members() -> None:
    members = IntegrationRepSweepPlanner().propose(_args(), source=INTEGRATION)
    assert [m.name for m in members] == ["unintegrated", "harmony", "scanorama"]
    assert all(m.skill_name == "sc-integrate-cluster" for m in members)
    # Each member carries the integration method + a FIXED shared resolution.
    assert members[0].params == {
        "cluster-method": "leiden", "method": "none", "resolution": "1.0", "batch-key": "batch",
    }
    assert {m.params["resolution"] for m in members} == {"1.0"}  # comparable k


def test_integration_include_scvi_appends_member() -> None:
    members = IntegrationRepSweepPlanner().propose(_args(include_scvi=True), source=INTEGRATION)
    assert [m.name for m in members] == ["unintegrated", "harmony", "scanorama", "scvi"]


def test_integration_all_selects_every_backend() -> None:
    """`--all` must actually fan out all available backends (default + scvi),
    not silently behave like the default run."""
    members = IntegrationRepSweepPlanner().propose(_args(all=True), source=INTEGRATION)
    assert [m.name for m in members] == ["unintegrated", "harmony", "scanorama", "scvi"]


def test_integration_explicit_methods_and_resolution() -> None:
    members = IntegrationRepSweepPlanner().propose(
        _args(integration_methods="harmony,scanorama", resolution="0.8", batch_key="donor"),
        source=INTEGRATION,
    )
    assert [m.name for m in members] == ["harmony", "scanorama"]
    assert all(m.params["resolution"] == "0.8" for m in members)
    assert all(m.params["batch-key"] == "donor" for m in members)


def test_integration_rejects_duplicate_method() -> None:
    with pytest.raises(SystemExit):
        IntegrationRepSweepPlanner().propose(
            _args(integration_methods="harmony,harmony"), source=INTEGRATION
        )


def test_derive_non_voting_baseline_gated_on_integration_source() -> None:
    # B2: run.py excludes the unintegrated (method=none) baseline from the vote by
    # default — but only for the integration flavour, and only without --vote-baseline.
    from omicsclaw.runtime.consensus.member import ConsensusMember
    from omicsclaw.runtime.consensus.run import _derive_non_voting

    members = IntegrationRepSweepPlanner().propose(_args(), source=INTEGRATION)
    assert _derive_non_voting(INTEGRATION, members, _args()) == ("unintegrated",)
    # --vote-baseline opts it back into the vote.
    assert _derive_non_voting(INTEGRATION, members, _args(vote_baseline=True)) == ()
    # gated on the source: another flavour is never affected, even if a member
    # happens to use method=none.
    fake = [ConsensusMember(name="x", skill_name="spatial-domains", params={"method": "none"})]
    assert _derive_non_voting(DOMAINS, fake, _args()) == ()


def test_integration_rejects_parameterized_member_spec() -> None:
    """Integration fixes resolution for all members (ADR 0029), so a per-member
    ``method:param=...`` spec is rejected early — it must NOT be forwarded as an
    invalid ``--method harmony:resolution=0.8`` that fails during fan-out."""
    with pytest.raises(SystemExit) as exc:
        IntegrationRepSweepPlanner().propose(
            _args(members="harmony:resolution=0.8,scanorama:resolution=0.8"),
            source=INTEGRATION,
        )
    assert "per-member params are not supported" in str(exc.value)
    # plain method names still work.
    members = IntegrationRepSweepPlanner().propose(
        _args(members="harmony,scanorama,none"), source=INTEGRATION
    )
    assert [m.name for m in members] == ["harmony", "scanorama", "unintegrated"]
