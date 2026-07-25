"""Convert-to-skill side-channel (MUSE creation loop / ADR 0013+0032).

A SUCCESSFUL autonomous mini-agent run should offer a user-gated "promote this
analysis into a reusable skill?" card. The candidate rides the same
``pending_media``-style side-channel: the executor queues it on
``state.pending_skill_promotion`` and the desktop Surface drains it onto the
just-finished autonomous tool's ``tool_result`` event as ``skill_promotion``.

These tests pin (a) the write side (the executor helper queues exactly one
candidate keyed by session, with the fields a later ``create_omics_skill`` call
needs — the ``workspace_root`` anchor above all), and (b) the App wire contract
(the module-level mapper's key names + malformed-item rejection). Nothing here
mutates a skill: the card only OFFERS ``create_omics_skill`` (APPROVAL_MODE_ASK).
"""

from __future__ import annotations

import omicsclaw.surfaces.desktop.server as server
from omicsclaw.runtime.agent import state as core
from omicsclaw.runtime.tools.builders.agent_executors import (
    _register_skill_promotion_candidate,
    pending_skill_promotion,
)
from omicsclaw.skill.scaffolder import VALID_DOMAINS


def _clear() -> None:
    core.pending_skill_promotion.clear()


# --- write side: executor queues the candidate ------------------------------


def test_write_side_shares_one_dict_with_core() -> None:
    # The executor mutates the imported name; the Surface drains core's attr.
    # A drift here (a re-bound dict) would silently drop every card.
    assert pending_skill_promotion is core.pending_skill_promotion


def test_successful_run_queues_one_candidate_with_needed_fields() -> None:
    _clear()
    cand = _register_skill_promotion_candidate(
        "sess-1", "spatial niche detection", "run-abc", "/out/autonomous-code__x"
    )
    assert cand is not None
    queued = core.pending_skill_promotion["sess-1"]
    assert len(queued) == 1
    assert queued[0]["workspace_root"] == "/out/autonomous-code__x"
    assert queued[0]["goal"] == "spatial niche detection"
    assert queued[0]["run_id"] == "run-abc"
    _clear()


def test_valid_domains_are_backend_authoritative() -> None:
    _clear()
    cand = _register_skill_promotion_candidate("s", "g", "r", "/out/w")
    assert cand is not None
    # The card's domain picker must not drift from the scaffolder's truth (an
    # autonomous bundle carries no domain, so the user MUST pick a valid one).
    assert tuple(cand["valid_domains"]) == tuple(VALID_DOMAINS)
    _clear()


def test_missing_session_or_workspace_is_a_noop() -> None:
    _clear()
    assert _register_skill_promotion_candidate("", "g", "r", "/out/w") is None
    assert _register_skill_promotion_candidate("s", "g", "r", "") is None
    assert core.pending_skill_promotion == {}


# --- read side: the App wire contract ---------------------------------------


def test_wire_block_maps_keys_the_app_depends_on() -> None:
    block = server._skill_promotion_wire_block(
        {
            "goal": "cell communication",
            "run_id": "run-9",
            "workspace_root": "/out/autonomous-code__y",
            "valid_domains": ["spatial", "singlecell"],
        }
    )
    assert block == {
        "goal": "cell communication",
        "runId": "run-9",
        "workspaceRoot": "/out/autonomous-code__y",
        "validDomains": ["spatial", "singlecell"],
    }


def test_wire_block_rejects_malformed_or_anchorless_items() -> None:
    assert server._skill_promotion_wire_block("not-a-dict") is None
    assert server._skill_promotion_wire_block({}) is None
    # No workspace anchor => no source_analysis_dir => nothing to promote.
    assert server._skill_promotion_wire_block({"goal": "x", "run_id": "r"}) is None
    # Non-list valid_domains degrades to empty, not a crash.
    assert (
        server._skill_promotion_wire_block(
            {"workspace_root": "/w", "valid_domains": "spatial"}
        )["validDomains"]
        == []
    )


def test_end_to_end_write_then_wire_shape() -> None:
    _clear()
    _register_skill_promotion_candidate("sess-e2e", "trajectory", "run-e", "/out/w-e")
    # Mirror the Surface drain: pop the session's items, map each to wire shape.
    items = core.pending_skill_promotion.pop("sess-e2e", [])
    blocks = [server._skill_promotion_wire_block(i) for i in items]
    assert blocks[0]["workspaceRoot"] == "/out/w-e"
    assert blocks[0]["goal"] == "trajectory"
    _clear()
