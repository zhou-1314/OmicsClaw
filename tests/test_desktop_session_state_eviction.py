"""Per-session policy/profile maps are bounded so a long-running desktop backend
doesn't leak one entry per session forever (audit F).

The fix is an LRU/insertion-order cap — NOT popping in the run-loop finally, which
would discard cross-turn "Allow <tool> for this session" approvals (read back at
the start of the next turn). TEST 2 guards that contract.
"""

from __future__ import annotations

import omicsclaw.surfaces.desktop.server as server
from omicsclaw.runtime.policy.state import ToolPolicyState


def test_session_state_maps_are_bounded():
    saved_p = dict(server._session_policy_states)
    saved_q = dict(server._session_permission_profiles)
    server._session_policy_states.clear()
    server._session_permission_profiles.clear()
    try:
        n = server._MAX_TRACKED_SESSIONS
        for i in range(n + 50):
            server._set_session_permission_profile(f"s{i}", "default")
        assert len(server._session_policy_states) <= n
        assert len(server._session_permission_profiles) <= n
        # newest kept, oldest evicted
        assert f"s{n + 49}" in server._session_policy_states
        assert "s0" not in server._session_policy_states
        assert "s0" not in server._session_permission_profiles
    finally:
        server._session_policy_states.clear()
        server._session_policy_states.update(saved_p)
        server._session_permission_profiles.clear()
        server._session_permission_profiles.update(saved_q)


def test_cross_turn_tool_approvals_are_preserved():
    # Regression guard: a "fix" that popped/cleared the entry would forget
    # per-session tool approvals every turn (cross-turn read in
    # _permission_profile_to_policy_state). The next turn must carry them forward.
    saved = dict(server._session_policy_states)
    try:
        server._session_policy_states["sA"] = ToolPolicyState(
            surface="app", approved_tool_names=frozenset({"Bash"})
        )
        state = server._set_session_permission_profile("sA", "default")
        assert "Bash" in state.approved_tool_names
        assert "Bash" in server._session_policy_states["sA"].approved_tool_names
    finally:
        server._session_policy_states.clear()
        server._session_policy_states.update(saved)
