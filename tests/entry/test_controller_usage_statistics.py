from datetime import datetime, timezone
from omicsclaw.memory.database import Database
from omicsclaw.memory.turn_events import DurableTurnEvent, DurableTurnEventLog
from omicsclaw.entry.desktop.usage_service import usage_statistics


def test_usage_rebuilds_from_authority_once_and_drops_incomplete_exchanges():
    database = Database(":memory:")
    log = DurableTurnEventLog(database)
    now = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
    stamp = now.timestamp() - 100

    def append(turn, seq, kind, payload):
        return log.append(
            DurableTurnEvent(f'["s","{turn}"]', "s", turn, seq, kind, payload, stamp)
        )

    log.claim_request(
        "s",
        "source",
        "hash",
        "complete",
        {"values": {"__desktop_model_snapshot__": {"model": "m"}}},
    )
    event = append(
        "complete",
        1,
        "turn_end",
        {
            "usage_reported": True,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_tokens": 7,
                "cache_write_tokens": 5,
            },
        },
    )
    log.append(event)
    append("complete", 2, "exchange_end", {"terminal": "converged"})
    append(
        "incomplete",
        1,
        "turn_end",
        {"usage_reported": True, "usage": {"input_tokens": 999}},
    )
    append("incomplete", 2, "turn_end", {"usage_reported": False, "usage": None})
    append("incomplete", 3, "exchange_end", {"terminal": "converged"})
    append(
        "running",
        1,
        "turn_end",
        {"usage_reported": True, "usage": {"input_tokens": 999}},
    )
    stats = usage_statistics(database, days=1, now=now)
    assert stats["summary"] == {
        "total_input_tokens": 100,
        "total_output_tokens": 20,
        "total_cost": 0,
        "total_sessions": 1,
        "cache_read_tokens": 7,
        "cache_creation_tokens": 5,
    }
    assert stats["daily"] == [
        {
            "date": "2026-10-14",
            "model": "m",
            "input_tokens": 100,
            "output_tokens": 20,
            "cost": 0,
        }
    ]
    assert stats["date_range"] == {"start": "2026-10-14", "end": "2026-10-14"}
    database.close()


def test_usage_retains_the_mac_day_boundary_without_using_mac_message_cache():
    database = Database(":memory:")
    log = DurableTurnEventLog(database)
    stamp = datetime(2026, 10, 13, 17, tzinfo=timezone.utc).timestamp()
    log.append(
        DurableTurnEvent(
            "stream",
            "s",
            "t",
            1,
            "turn_end",
            {"usage_reported": True, "usage": {"input_tokens": 1}},
            stamp,
        )
    )
    log.append(
        DurableTurnEvent(
            "stream", "s", "t", 2, "exchange_end", {"terminal": "converged"}, stamp
        )
    )
    stats = usage_statistics(
        database,
        days=1,
        timezone_offset_minutes=480,
        now=datetime(2026, 10, 14, 1, tzinfo=timezone.utc),
    )
    assert stats["daily"][0]["date"] == "2026-10-14"
    assert stats["summary"]["total_input_tokens"] == 1
    database.close()
