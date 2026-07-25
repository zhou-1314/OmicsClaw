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


# --- the key the runtime writes must be the key the Surface drains -----------
#
# The tests above (and the media ones) key both sides with the same literal, so
# they never checked the one thing that actually broke: the runtime hands tools a
# NAMESPACED session id (``f"{platform}:{user_id}:{chat_id}"`` — see
# ``runtime/context/assembler.py``), while the desktop Surface used to drain with
# the bare ``chat_id`` from the request. The candidate was queued forever under a
# key nobody read, so a successful run silently produced no card (and no media).


def test_runtime_session_key_is_namespaced_not_the_bare_chat_id() -> None:
    from omicsclaw.runtime.agent.session import build_agent_session_id

    key = build_agent_session_id("app", "desktop-user", "chat-42")
    assert key == "app:desktop-user:chat-42"
    assert key != "chat-42"
    # Missing platform/user must not fabricate a half-formed namespace.
    assert build_agent_session_id("", "desktop-user", "chat-42") is None
    assert build_agent_session_id("app", "", "chat-42") is None


def test_assembler_and_helper_agree_on_the_session_key() -> None:
    # Both sides deriving the format independently is what allowed the drift.
    import inspect

    from omicsclaw.runtime.agent.session import build_agent_session_id
    from omicsclaw.runtime.context import assembler

    assert "build_agent_session_id" in inspect.getsource(assembler), (
        "the assembler must derive the session key from the shared helper, "
        "not re-spell the f-string"
    )
    assert build_agent_session_id("app", "u", "c") == "app:u:c"


def test_desktop_drains_the_key_the_runtime_actually_wrote() -> None:
    from omicsclaw.memory import desktop_chat_user_id
    from omicsclaw.runtime.agent.session import build_agent_session_id

    _clear()
    chat_id = "chat-xyz"
    runtime_key = build_agent_session_id("app", desktop_chat_user_id(), chat_id)

    # Queue exactly as the executor does during a real turn: under the id the
    # runtime put in the tool's kwargs, NOT under the bare chat id.
    _register_skill_promotion_candidate(
        runtime_key, "cluster synthetic sc data", "run-1", "/ws/run-1"
    )
    assert runtime_key in core.pending_skill_promotion
    assert chat_id not in core.pending_skill_promotion

    # The Surface must resolve the same key from the chat id it has on hand.
    assert server._agent_session_key(chat_id) == runtime_key
    drained = core.pending_skill_promotion.pop(server._agent_session_key(chat_id), [])
    assert len(drained) == 1
    assert drained[0]["workspace_root"] == "/ws/run-1"
    _clear()


def test_drain_takes_the_namespaced_key_and_still_honours_the_bare_one() -> None:
    # Both must drain: the runtime writes the namespaced key, while a producer
    # outside the context assembler (or state queued by an older build) uses the
    # bare chat id. An undrained item never reaches the App, so the safe move is
    # to accept either rather than guess.
    pending: dict[str, list[str]] = {}
    chat_id = "chat-both"
    pending[server._agent_session_key(chat_id)] = ["namespaced"]
    pending[chat_id] = ["bare"]

    drained = server._drain_session_side_channel(pending, chat_id)

    assert drained == ["namespaced", "bare"]
    assert pending == {}, "both keys must be consumed, not left to leak"


# --- the authoritative (ControlRuntime) path --------------------------------
#
# The Desktop production path reports ``authoritative_ingress: true``, and there
# the runtime builds its envelope with ``chat_id=envelope.conversation_id`` —
# NOT the Surface's ``req.session_id``. So the tool-side key is derived from the
# Control conversation id, and a Surface that rebuilds the key from its own
# session id still drains nothing. The Surface must learn the conversation id
# instead of guessing it.


def test_authoritative_turn_keys_state_by_conversation_id_not_session_id() -> None:
    from omicsclaw.memory import desktop_chat_user_id
    from omicsclaw.runtime.agent.session import build_agent_session_id

    _clear()
    req_session_id = "chat-slot-1"
    conversation_id = "0123456789abcdef0123456789abcdef"
    assert conversation_id != req_session_id

    # What the tools actually write on the authoritative path.
    runtime_key = build_agent_session_id("app", desktop_chat_user_id(), conversation_id)
    _register_skill_promotion_candidate(runtime_key, "goal", "run-1", "/ws/run-1")

    # Guessing from the Surface's own session id must NOT find it...
    assert core.pending_skill_promotion.get(
        server._agent_session_key(req_session_id)
    ) is None
    # ...but resolving the key from the conversation id the runtime reported does.
    assert server._agent_session_key(conversation_id) == runtime_key
    drained = server._drain_session_side_channel(
        core.pending_skill_promotion, conversation_id
    )
    assert len(drained) == 1
    _clear()


def test_conversation_id_lookup_is_live_only_and_never_invents_an_id() -> None:
    # The Surface only receives ``turn_id`` from ``on_accepted``; without a way to
    # resolve the conversation id it cannot build the key the tools used. The
    # lookup is live-only: an unknown or already-retired Turn must answer None so
    # the Surface falls back to its own session id instead of draining a
    # fabricated key (it caches the id at acceptance for exactly this reason).
    from omicsclaw.control.runtime import ControlRuntime

    # Exercise the real method body against a minimal holder — building a full
    # ControlRuntime needs a repository/sequencer/transcript stack this contract
    # does not depend on.
    lookup = ControlRuntime.conversation_id_for_turn
    holder = type("_Holder", (), {"_live_turns": {}})()

    assert lookup(holder, "no-such-turn") is None
    assert lookup(holder, "") is None

    # A registered live Turn resolves to its Conversation.
    holder._live_turns["turn-1"] = type("_Live", (), {"conversation_id": "conv-42"})()
    assert lookup(holder, "turn-1") == "conv-42"

    # Once the Turn is retired the lookup stops answering, rather than returning
    # a stale id the Surface would drain with.
    holder._live_turns.pop("turn-1")
    assert lookup(holder, "turn-1") is None
