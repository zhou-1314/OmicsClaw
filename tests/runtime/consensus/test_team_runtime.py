"""Tests for the team-runtime fan-out (Slice 3).

The runner is monkey-patched with a controllable in-memory stub so the test
can drive crashes, timeouts, and cancellation without spawning real
subprocesses.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from omicsclaw.runtime.consensus.dispatch import (
    TYPED_CONSENSUS_REGISTRY,
    consensus_namespace,
    output_banner,
    select_consensus_mode,
)
from omicsclaw.runtime.consensus.member import ConsensusMember
from omicsclaw.runtime.consensus.team import (
    InsufficientSurvivorsError,
    MemberRunResult,
    TeamRunResult,
    run_team,
)


# ----------------------------- helpers ------------------------------------ #

@dataclass
class _StubResult:
    exit_code: int = 0


def _make_member(name: str, **params) -> ConsensusMember:
    return ConsensusMember(
        name=name,
        skill_name="spatial-domains",
        params=params,
    )


# ----------------------------- dispatch tests ------------------------------ #

def test_typed_registry_contains_v1_skills() -> None:
    assert "spatial-domains" in TYPED_CONSENSUS_REGISTRY
    assert "sc-clustering" in TYPED_CONSENSUS_REGISTRY


def test_select_mode_routes_known_skill_to_typed() -> None:
    assert select_consensus_mode("spatial-domains") == "typed"


def test_select_mode_routes_unknown_skill_to_narrative() -> None:
    assert select_consensus_mode("spatial-velocity") == "narrative"


def test_select_mode_force_override() -> None:
    assert select_consensus_mode("spatial-velocity", force_mode="typed") == "typed"
    assert select_consensus_mode("spatial-domains", force_mode="narrative") == "narrative"


def test_select_mode_rejects_invalid_force() -> None:
    with pytest.raises(ValueError):
        select_consensus_mode("anything", force_mode="weird")  # type: ignore[arg-type]


def test_namespace_split_per_adr_0010() -> None:
    assert consensus_namespace("run42", "typed") == "analysis://typed/run42"
    assert consensus_namespace("run42", "narrative") == "analysis://exploratory/run42"


def test_namespace_scopes_under_thread_id_when_set() -> None:
    # Bench (ADR 0018): a set thread_id scopes the run's lineage under the
    # investigation thread; empty thread_id preserves the legacy URIs.
    assert consensus_namespace("run42", "typed", thread_id="") == "analysis://typed/run42"
    assert (
        consensus_namespace("run42", "typed", thread_id="t-glioma")
        == "analysis://t-glioma/typed/run42"
    )
    assert (
        consensus_namespace("run42", "narrative", thread_id="t-glioma")
        == "analysis://t-glioma/exploratory/run42"
    )


def test_namespace_encodes_each_identity_as_one_path_segment() -> None:
    assert consensus_namespace(
        "run/../../forged",
        "typed",
        thread_id="thread/other",
    ) == "analysis://thread%2Fother/typed/run%2F..%2F..%2Fforged"


def test_output_banner_mentions_verification_state() -> None:
    assert "Verified" in output_banner("typed")
    assert "Exploratory" in output_banner("narrative")


# ----------------------------- member tests -------------------------------- #

def test_member_to_extra_args_skips_empty_value_for_flag() -> None:
    m = ConsensusMember(name="x", skill_name="spatial-domains", params={"all": "", "method": "banksy"})
    assert m.to_extra_args() == ["--all", "--method", "banksy"]


# Intrinsic-quality reading lives in per-source MemberArtifactReader adapters
# (see source_registry.py + test_source_registry.py); the old member-local
# read_intrinsic_quality() helper was removed when the spatial / sc readers
# absorbed the JSON-vs-CSV difference.

# Concurrency is no longer a fan-out-local cpu-count cap: capacity is gated by
# the process-global Execution Resource Scheduler (ADR 0061 D2) and the local
# fairness window is resolved by ``fan_out._resolve_parallel_window``. Those are
# covered where the primitive lives — see tests/runtime/workflow/test_fan_out.py.


# ----------------------------- run_team tests ------------------------------ #

@pytest.mark.asyncio
async def test_run_team_five_members_all_succeed(tmp_path: Path) -> None:
    members = [_make_member(f"m{i}", method=f"x{i}") for i in range(5)]

    def fake_runner(**kwargs):
        return _StubResult(exit_code=0)

    result = await run_team(
        members,
        input_path="/dev/null",
        output_root=tmp_path,
        runner=fake_runner,
    )
    assert result.total == 5
    assert result.n_survived == 5
    assert result.n_failed == 0


@pytest.mark.asyncio
async def test_run_team_one_crash_four_survive(tmp_path: Path) -> None:
    members = [_make_member(f"m{i}", method=f"x{i}") for i in range(5)]

    def fake_runner(**kwargs):
        if kwargs["output_dir"].endswith("m2"):
            raise RuntimeError("synthetic crash")
        return _StubResult(exit_code=0)

    result = await run_team(
        members,
        input_path="/dev/null",
        output_root=tmp_path,
        runner=fake_runner,
    )
    assert result.n_survived == 4
    assert result.n_failed == 1
    assert any(r.status == "failed" and "synthetic crash" in (r.error or "") for r in result.failed)


@pytest.mark.asyncio
async def test_run_team_nonzero_exit_counts_as_failed(tmp_path: Path) -> None:
    members = [_make_member(f"m{i}", method=f"x{i}") for i in range(3)]

    def fake_runner(**kwargs):
        if kwargs["output_dir"].endswith("m0"):
            return _StubResult(exit_code=2)
        return _StubResult(exit_code=0)

    result = await run_team(
        members,
        input_path="/dev/null",
        output_root=tmp_path,
        runner=fake_runner,
    )
    assert result.n_survived == 2
    assert any("exit_code=2" in (r.error or "") for r in result.failed)


@pytest.mark.asyncio
async def test_run_team_raises_when_below_min_survivors(tmp_path: Path) -> None:
    members = [_make_member(f"m{i}", method=f"x{i}") for i in range(3)]

    def fake_runner(**kwargs):
        if kwargs["output_dir"].endswith("m0"):
            return _StubResult(exit_code=0)
        raise RuntimeError("synthetic crash")

    with pytest.raises(InsufficientSurvivorsError) as exc_info:
        await run_team(
            members,
            input_path="/dev/null",
            output_root=tmp_path,
            runner=fake_runner,
        )
    assert "Only 1" in str(exc_info.value)


@pytest.mark.asyncio
async def test_run_team_timeout_marks_member(tmp_path: Path) -> None:
    members = [_make_member(f"m{i}", method=f"x{i}") for i in range(3)]

    def fake_runner(**kwargs):
        if kwargs["output_dir"].endswith("m1"):
            time.sleep(2.0)
        return _StubResult(exit_code=0)

    result = await run_team(
        members,
        input_path="/dev/null",
        output_root=tmp_path,
        runner=fake_runner,
        timeout_seconds=0.3,
    )
    timed_out = [r for r in result.failed if r.status == "timeout"]
    assert len(timed_out) == 1
    assert timed_out[0].step.name == "m1"


@pytest.mark.asyncio
async def test_timeout_does_not_cancel_sibling_members(tmp_path: Path) -> None:
    """C1 regression: one member timing out must NOT take down the team.

    ADR 0010 requires: a member timing out is marked ``failed`` (status="timeout"),
    surviving members continue. The bug was that the timeout branch called
    ``cancel_event.set()`` on the shared event, cascading into all siblings.
    """
    members = [_make_member(f"m{i}", method=f"x{i}") for i in range(5)]
    cancel_event = threading.Event()

    def fake_runner(**kwargs):
        if kwargs["output_dir"].endswith("slow"):
            time.sleep(2.0)
        return _StubResult(exit_code=0)

    # Make m2 the slow one.
    slow_member = ConsensusMember(name="slow", skill_name="spatial-domains", params={"method": "x"})
    members = [_make_member(f"m{i}", method=f"x{i}") for i in range(4)] + [slow_member]

    result = await run_team(
        members,
        input_path="/dev/null",
        output_root=tmp_path,
        runner=fake_runner,
        cancel_event=cancel_event,
        timeout_seconds=0.3,
    )

    # The slow one times out; the four fast ones must still succeed.
    assert result.n_survived == 4, (
        f"timeout of one member cascaded; only {result.n_survived}/5 survived. "
        f"Statuses: {[(r.step.name, r.status, r.error) for r in result.steps]}"
    )
    timed_out = [r for r in result.failed if r.status == "timeout"]
    assert len(timed_out) == 1
    assert timed_out[0].step.name == "slow"
    # cancel_event must remain UNSET — a per-member timeout is NOT user cancellation.
    assert not cancel_event.is_set(), (
        "cancel_event was set by the timeout branch; this would cascade-cancel "
        "future runs that share the same envelope."
    )


@pytest.mark.asyncio
async def test_run_team_cancel_event_set_before_start(tmp_path: Path) -> None:
    members = [_make_member(f"m{i}", method=f"x{i}") for i in range(3)]
    cancel_event = threading.Event()
    cancel_event.set()

    def fake_runner(**kwargs):
        raise AssertionError("runner should not have been called")

    with pytest.raises(InsufficientSurvivorsError):
        await run_team(
            members,
            input_path="/dev/null",
            output_root=tmp_path,
            cancel_event=cancel_event,
            runner=fake_runner,
        )


@pytest.mark.asyncio
async def test_run_team_propagates_cancel_to_runner(tmp_path: Path) -> None:
    members = [_make_member(f"m{i}", method=f"x{i}") for i in range(2)]
    cancel_event = threading.Event()
    received: list[threading.Event | None] = []

    def fake_runner(**kwargs):
        received.append(kwargs.get("cancel_event"))
        return _StubResult(exit_code=0)

    await run_team(
        members,
        input_path="/dev/null",
        output_root=tmp_path,
        cancel_event=cancel_event,
        runner=fake_runner,
    )
    assert received == [cancel_event, cancel_event]


@pytest.mark.asyncio
async def test_run_team_rejects_duplicate_member_names(tmp_path: Path) -> None:
    members = [_make_member("dup"), _make_member("dup")]
    with pytest.raises(ValueError, match="unique"):
        await run_team(members, input_path="/dev/null", output_root=tmp_path, runner=lambda **k: None)


@pytest.mark.asyncio
async def test_run_team_rejects_empty(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        await run_team([], input_path="/dev/null", output_root=tmp_path, runner=lambda **k: None)


@pytest.mark.asyncio
async def test_run_team_writes_per_member_output_dirs(tmp_path: Path) -> None:
    members = [_make_member(f"m{i}", method=f"x{i}") for i in range(3)]

    def fake_runner(**kwargs):
        out = Path(kwargs["output_dir"])
        (out / "marker.txt").write_text("ran")
        return _StubResult(exit_code=0)

    result = await run_team(
        members,
        input_path="/dev/null",
        output_root=tmp_path,
        runner=fake_runner,
    )
    assert result.n_survived == 3
    for r in result.survived:
        assert (r.output_dir / "marker.txt").read_text() == "ran"
