"""Writing run records: a reader sees the old file or the new one."""

from __future__ import annotations

from pathlib import Path

import pytest

from omicsclaw.bench.layout import read_json, write_json


def test_a_write_that_fails_midway_leaves_the_previous_record(tmp_path, monkeypatch):
    """``done.json`` decides whether a run is finished and how it ended. A
    write cut short, by a full disk or a killed harness, must not leave
    half a record where a whole one was; and it must not leave its
    temporary file behind either.
    """
    path = tmp_path / "done.json"
    write_json(path, {"outcome": "completed"})
    whole = Path.write_text

    def half(self, data, *args, **kwargs):
        whole(self, data[: len(data) // 2], *args, **kwargs)
        raise OSError("no space left on device")

    monkeypatch.setattr(Path, "write_text", half)
    with pytest.raises(OSError, match="no space left"):
        write_json(path, {"outcome": "infra_failure", "reason": "x" * 200})
    monkeypatch.undo()

    assert read_json(path) == {"outcome": "completed"}
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["done.json"]


def test_a_missing_or_broken_record_reads_as_none(tmp_path):
    (tmp_path / "broken.json").write_text('{"outcome": "comp')
    (tmp_path / "list.json").write_text("[1, 2]")

    assert read_json(tmp_path / "absent.json") is None
    assert read_json(tmp_path / "broken.json") is None
    assert read_json(tmp_path / "list.json") is None
