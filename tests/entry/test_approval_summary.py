"""P6 item 1: the chat approval frame's ``summary`` (and the ``risk_level``
that was already there, now asserted as a pair).

The jobs plane has carried ``risk`` + ``summary`` on its
``tool_approval_request`` events since P1; this file pins the same pair
on the chat plane's ``permission_request`` payload — additive, so the
assertions about the fields that predate P6 stay exactly as they were.
"""

from __future__ import annotations

from omicsclaw.entry.desktop.turn_observation import desktop_chat_frame
from omicsclaw.entry.events import TurnEvent
from omicsclaw.entry.render import (
    HUMAN_DESCRIPTION_CHARS,
    human_tool_description,
    to_wire,
)
from omicsclaw.tools.base import ApprovalMode, RiskLevel
from omicsclaw.tools.context import ApprovalRequest


def _event(
    tool: str = "bash",
    arguments: str = "{}",
    reason: str = "",
    risk: RiskLevel = RiskLevel.HIGH,
) -> TurnEvent:
    return TurnEvent.approval_required(
        ApprovalRequest(
            tool_name=tool,
            arguments=arguments,
            reason=reason,
            risk_level=risk,
            approval_mode=ApprovalMode.ASK,
        ),
        "t#1",
        seq=3,
        session_id="s",
        turn_id="t",
    )


# ---- payload shape ----------------------------------------------------------


def test_the_payload_carries_risk_and_summary_beside_the_raw_arguments() -> None:
    wire = to_wire(_event(arguments='{"command": "ls -la"}'))
    assert wire["risk_level"] == "high"
    assert wire["summary"] == "run bash ls -la"
    # The pre-P6 fields keep their shape and their meaning.
    assert wire["arguments"] == '{"command": "ls -la"}'
    assert wire["approval_mode"] == "ask"
    assert wire["request_id"] == "t#1"


def test_every_risk_tier_maps_to_its_policy_string() -> None:
    for risk, expected in (
        (RiskLevel.LOW, "low"),
        (RiskLevel.MEDIUM, "medium"),
        (RiskLevel.HIGH, "high"),
    ):
        assert to_wire(_event(risk=risk))["risk_level"] == expected


def test_a_reason_the_tool_wrote_beats_the_derived_line() -> None:
    """Same ruling the jobs plane makes: the tool's own words about its
    own call are the best one-line account of the call."""
    wire = to_wire(_event(reason="delete 412 files under /data/run-7"))
    assert wire["summary"] == "delete 412 files under /data/run-7"


def test_the_summary_never_runs_past_the_card_line_ceiling() -> None:
    wire = to_wire(_event(arguments='{"command": "%s"}' % ("rm -rf " + "x" * 500)))
    assert len(wire["summary"]) <= HUMAN_DESCRIPTION_CHARS


# ---- summary content for representative tools -------------------------------


def test_a_skill_call_summarises_as_its_name_and_knobs() -> None:
    summary = human_tool_description(
        "use_skill", '{"skill": "spatial-domains", "n_domains": 12}'
    )
    assert summary == "run use skill spatial-domains n_domains=12"


def test_a_delete_tool_says_delete() -> None:
    summary = human_tool_description(
        "delete_file", '{"path": "old-run.h5ad"}'
    )
    assert summary.startswith("delete ")
    assert "old-run.h5ad" in summary


def test_an_unparseable_payload_degrades_to_the_tool_name() -> None:
    summary = human_tool_description("spatial_domains", "not json{")
    assert summary == "run spatial domains"


# ---- the no-secret guarantee ------------------------------------------------


def test_the_summary_cannot_show_a_credential_the_arguments_carry() -> None:
    """The derived line reads only the handful of keys a decision needs,
    so a credential under any other key never reaches it — the summary
    shows strictly less than the raw ``arguments`` beside it."""
    arguments = (
        '{"command": "curl -s http://x", "api_key": "sk-live-abc123", '
        '"token": "tok-1", "password": "hunter2"}'
    )
    summary = to_wire(_event(arguments=arguments))["summary"]
    assert "sk-live-abc123" not in summary
    assert "tok-1" not in summary
    assert "hunter2" not in summary


def test_a_data_blob_is_not_copied_into_the_summary() -> None:
    blob = "A" * 400
    summary = to_wire(_event(arguments=f'{{"data": "{blob}"}}'))["summary"]
    assert blob not in summary
    assert summary == "run bash"


# ---- the desktop frame and the jobs plane -----------------------------------


def test_the_permission_request_frame_carries_the_new_fields() -> None:
    name, data = desktop_chat_frame(_event(arguments='{"command": "ls"}'))
    assert name == "permission_request"
    assert data["risk_level"] == "high"
    assert data["summary"] == "run bash ls"
    assert data["can_remember"] is False  # the default when nobody ruled


def test_the_jobs_plane_keeps_the_same_function() -> None:
    """P1's import path still resolves — one implementation, two planes."""
    from omicsclaw.entry.desktop.jobs_manager import (  # noqa: F401
        human_tool_description as jobs_copy,
    )

    assert jobs_copy is human_tool_description
