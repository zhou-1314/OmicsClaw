import sqlite3

import pytest

from omicsclaw.entry.stream import TurnStream
from omicsclaw.memory.turn_events import StreamOwnerConflict


class BrokenLog:
    def __init__(self, stage):
        self.stage = stage

    def acquire_owner(self, stream):
        if self.stage == "owner":
            raise sqlite3.OperationalError("database unavailable")
        if self.stage == "observer":
            raise StreamOwnerConflict("another producer owns this stream")
        return 1

    def bounds(self, stream):
        if self.stage == "bounds":
            raise sqlite3.OperationalError("database unavailable")
        return (1, 7)


@pytest.mark.parametrize("stage", ["owner", "bounds"])
def test_storage_failure_during_stream_initialization_is_visible(stage):
    with pytest.raises(sqlite3.OperationalError, match="database unavailable"):
        TurnStream("session", "turn", durable_log=BrokenLog(stage))


def test_other_owner_still_allows_a_replay_observer():
    stream = TurnStream("session", "turn", durable_log=BrokenLog("observer"))
    assert stream.latest_seq == 7
