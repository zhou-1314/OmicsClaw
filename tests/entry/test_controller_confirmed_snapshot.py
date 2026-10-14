"""Confirmed read projections cover original raw events without execution."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from omicsclaw.entry.desktop.event_service import replay_frames
from omicsclaw.entry.events import TurnEventType
from omicsclaw.memory.database import Database
from omicsclaw.memory.turn_events import DurableTurnEvent, DurableTurnEventLog


def fixture(tmp_path, count=1):
    db = Database(tmp_path / "memory.db")
    log = DurableTurnEventLog(db)
    log.claim_request(
        "s",
        "i:r",
        "a" * 64,
        "t",
        {
            "text": "user",
            "values": {
                "__desktop_model_snapshot__": {
                    "provider": "accepted",
                    "model": "accepted-model",
                }
            },
        },
    )
    for seq in range(1, count + 1):
        log.append(
            DurableTurnEvent(
                '["s","t"]',
                "s",
                "t",
                seq,
                TurnEventType.TEXT_DELTA.value,
                {"delta": "synthetic"},
                0,
            )
        )
    log.append(
        DurableTurnEvent(
            '["s","t"]',
            "s",
            "t",
            count + 1,
            TurnEventType.TURN_END.value,
            {"usage_reported": True, "usage": {"input_tokens": 3, "output_tokens": 2}},
            0,
        )
    )
    log.append(
        DurableTurnEvent(
            '["s","t"]',
            "s",
            "t",
            count + 2,
            TurnEventType.EXCHANGE_END.value,
            {"terminal": "converged"},
            0,
        )
    )
    app = SimpleNamespace(
        memory=SimpleNamespace(database=db),
        config=SimpleNamespace(provider="new", model="new"),
        resolved_model="new",
    )
    return db, log, app


def projected(app):
    frames = asyncio.run(replay_frames(app, "s", "t"))["frames"]
    return [
        {"seq": row["cursor"], "subframe": row["subframe"], "frame": row["frame"]}
        for row in frames
    ]


def test_raw_coverage_includes_unrendered_usage_and_snapshot_survives_source_gap(
    tmp_path,
):
    db, log, app = fixture(tmp_path)
    try:
        source = log.projection_source("s", "t")
        checkpoint = log.confirm_projection(source, projected(app))
        assert checkpoint["coverage"] == {
            "firstSeq": 1,
            "lastSeq": 3,
            "lastSubframe": 1,
        }
        db.run(
            lambda c: c.execute(
                "DELETE FROM turn_events WHERE stream_id=? AND seq=1", ('["s","t"]',)
            )
        )
        page = log.projection_page("s", "t", limit=1)
        token = page["nextPage"]
        frames = page["frames"]
        while token:
            page = log.projection_page("s", "t", limit=1, cursor=token)
            frames += page["frames"]
            token = page["nextPage"]
        result = json.loads(
            json.loads(frames[-2]["frame"].split("data: ", 1)[1])["data"]
        )
        assert result["usage_reported"] and result["model_calls"] == 1
        assert result["provider"] == "accepted" and result["model"] == "accepted-model"
    finally:
        db.close()


def test_missing_raw_prefix_never_self_confirms_a_new_snapshot(tmp_path):
    db, log, _ = fixture(tmp_path)
    try:
        db.run(lambda c: c.execute("DELETE FROM turn_events WHERE seq=1"))
        with pytest.raises(ValueError, match="incomplete_history"):
            log.projection_source("s", "t")
        assert log.projection_page("s", "t")["snapshot"] is None
    finally:
        db.close()


def test_source_and_owner_generation_are_checked_in_confirmation_transaction(tmp_path):
    db, log, app = fixture(tmp_path)
    try:
        source = log.projection_source("s", "t")
        frames = projected(app)
        db.run(lambda c: c.execute("UPDATE turn_stream_owners SET epoch=epoch+1"))
        with pytest.raises(ValueError, match="projection_source_changed"):
            log.confirm_projection(source, frames)
        assert log.projection_page("s", "t")["snapshot"] is None
    finally:
        db.close()


def test_identical_checkpoint_is_reused_and_conflicting_stable_frame_is_refused(
    tmp_path,
):
    db, log, app = fixture(tmp_path)
    try:
        source, frames = log.projection_source("s", "t"), projected(app)
        original = log.confirm_projection(source, frames)
        assert log.confirm_projection(source, frames) == original
        changed = [dict(row) for row in frames]
        changed[0]["frame"] = 'data: {"type":"text","data":"changed"}\n\n'
        with pytest.raises(ValueError, match="projection_frame_conflict"):
            log.confirm_projection(source, changed)
    finally:
        db.close()


def test_partial_terminal_and_nonterminal_eof_cannot_confirm_coverage(tmp_path):
    db, log, app = fixture(tmp_path)
    try:
        source, frames = log.projection_source("s", "t"), projected(app)
        with pytest.raises(ValueError, match="incomplete_projection"):
            log.confirm_projection(source, frames[:-1])
        with pytest.raises(ValueError, match="incomplete_projection"):
            log.confirm_projection(source, [frames[0], frames[-1]])
        db.run(lambda c: c.execute("DELETE FROM turn_events WHERE seq=3"))
        with pytest.raises(ValueError, match="incomplete_history"):
            log.projection_source("s", "t")
    finally:
        db.close()


def test_snapshot_cursor_pins_revision_source_and_frame_digest(tmp_path):
    db, log, app = fixture(tmp_path, count=70)
    try:
        source = log.projection_source("s", "t")
        # The ordinary projector pages are collected with their original cursor.
        frames, cursor = [], {"seq": 0, "subframe": -1}
        while True:
            page = asyncio.run(
                replay_frames(
                    app,
                    "s",
                    "t",
                    after_seq=cursor["seq"],
                    after_subframe=cursor["subframe"],
                )
            )
            frames += [
                {"seq": r["cursor"], "subframe": r["subframe"], "frame": r["frame"]}
                for r in page["frames"]
            ]
            cursor = page["nextCursor"]
            if not page["hasMore"]:
                break
        log.confirm_projection(source, frames)
        first = log.projection_page("s", "t")
        assert len(first["frames"]) == 64 and first["nextPage"]
        with pytest.raises(ValueError, match="snapshot_cursor"):
            log.projection_page("s", "other", cursor=first["nextPage"])
        db.run(
            lambda c: c.execute(
                "UPDATE turn_projection_checkpoints SET revision=revision+1"
            )
        )
        with pytest.raises(ValueError, match="snapshot_cursor"):
            log.projection_page("s", "t", cursor=first["nextPage"])
    finally:
        db.close()


def test_source_size_is_rejected_before_payload_json_is_loaded(tmp_path):
    db, log, _ = fixture(tmp_path)
    try:
        db.run(
            lambda c: c.execute(
                "UPDATE turn_events SET payload_json=? WHERE seq=1",
                ("x" * (4 * 1024 * 1024 + 1),),
            )
        )
        with pytest.raises(ValueError, match="projection_requires_data_channel"):
            log.projection_source("s", "t")
    finally:
        db.close()


def test_forward_export_uses_the_same_keyset_with_a_fixed_latest_boundary(tmp_path):
    db = Database(tmp_path / "memory.db")
    log = DurableTurnEventLog(db)
    try:
        for index in range(101):
            log.claim_request("s", str(index), "a" * 64, str(index), {"text": "short"})
        latest = log.request_page("s", limit=1)["nextBefore"]
        first = log.request_page("s", limit=100, forward=True, through=latest)
        log.claim_request("s", "new", "a" * 64, "new", {"text": "new"})
        second = log.request_page(
            "s", limit=100, forward=True, before=first["nextBefore"], through=latest
        )
        assert first["hasMore"] and len(second["requests"]) == 1
        assert (
            second["requests"][0]["source_request_id"] != "new"
            and not second["hasMore"]
        )
    finally:
        db.close()


@pytest.mark.parametrize("count", [0, 1000, 1003])
def test_receipt_keyset_pages_use_true_lookahead_and_survive_concurrent_append(
    tmp_path, count
):
    db = Database(tmp_path / "memory.db")
    log = DurableTurnEventLog(db)
    try:
        for index in range(count):
            log.claim_request(
                "s", f"i:{index:032x}", "a" * 64, str(index), {"text": "short"}
            )
        page = log.request_page("s", limit=30)
        assert len(page["requests"]) == min(30, count)
        assert page["hasMore"] is (count > 30)
        if count:
            before = page["nextBefore"]
            log.claim_request("s", "i:new", "a" * 64, "new", {"text": "new"})
            older = log.request_page("s", limit=30, before=before)
            assert all(row["source_request_id"] != "i:new" for row in older["requests"])
    finally:
        db.close()
