import time
from pathlib import Path

import pytest

from omicsclaw.memory.database import Database
from omicsclaw.memory.turn_events import (
    DurableTurnEvent,
    DurableTurnEventLog,
    EventLogConflict,
    StreamOwnerConflict,
)


def event(seq):
    return DurableTurnEvent("stream", "s", "t", seq, "queued", {"seq": seq}, 0)


def test_cross_database_owner_and_release(tmp_path: Path):
    path = tmp_path / "events.db"
    first = DurableTurnEventLog(Database(path), owner_id="one")
    second = DurableTurnEventLog(Database(path), owner_id="two")
    first.append(event(1))
    with pytest.raises(StreamOwnerConflict):
        second.append(event(2))
    first.release_owner("stream")
    second.acquire_owner("stream")
    assert second.append(event(2)).seq == 2


def test_expired_lease_can_be_taken_over(tmp_path: Path):
    path = tmp_path / "events.db"
    first = DurableTurnEventLog(Database(path), owner_id="one", lease_s=0.01)
    second = DurableTurnEventLog(Database(path), owner_id="two", lease_s=1)
    first.append(event(1))
    time.sleep(0.03)
    second.acquire_owner("stream")
    assert second.append(event(2)).seq == 2


def test_sequence_gaps_are_rejected(tmp_path: Path):
    log = DurableTurnEventLog(Database(tmp_path / "events.db"), owner_id="one")
    with pytest.raises(EventLogConflict, match="sequence"):
        log.append(event(2))


def test_stale_owner_epoch_cannot_append_after_reacquire(tmp_path: Path):
    path = tmp_path / "events.db"
    log = DurableTurnEventLog(Database(path), owner_id="one")
    epoch_one = log.acquire_owner("stream")
    log.append(
        DurableTurnEvent("stream", "s", "t", 1, "queued", {"seq": 1}, 0, epoch_one)
    )
    log.release_owner("stream")
    epoch_two = log.acquire_owner("stream")
    assert epoch_two > epoch_one
    with pytest.raises(StreamOwnerConflict):
        log.append(
            DurableTurnEvent("stream", "s", "t", 2, "queued", {"seq": 2}, 0, epoch_one)
        )
    assert (
        log.append(
            DurableTurnEvent("stream", "s", "t", 2, "queued", {"seq": 2}, 0, epoch_two)
        ).seq
        == 2
    )
