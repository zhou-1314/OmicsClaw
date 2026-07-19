from __future__ import annotations

import pytest

from omicsclaw.common.windows_directory_guard import (
    hold_windows_plain_directory_authority,
)


class _FakeDirectoryApi:
    def __init__(self, *, fail_at: str | None = None) -> None:
        self.fail_at = fail_at
        self.opened: list[str] = []
        self.closed: list[int] = []

    def open_plain_directory(self, path: str) -> int:
        self.opened.append(path)
        if path == self.fail_at:
            raise RuntimeError("not a plain directory")
        return len(self.opened)

    def close(self, handle: int) -> None:
        self.closed.append(handle)


def test_windows_directory_guard_holds_every_component_until_exit() -> None:
    api = _FakeDirectoryApi()
    created: list[str] = []

    with hold_windows_plain_directory_authority(
        r"C:\Users\owner\workspace",
        ".omicsclaw",
        "evolved",
        _api=api,
        _mkdir=lambda path: created.append(path),
    ) as destination:
        assert destination == r"C:\Users\owner\workspace\.omicsclaw\evolved"
        assert api.closed == []

    assert api.opened == [
        "C:\\",
        r"C:\Users",
        r"C:\Users\owner",
        r"C:\Users\owner\workspace",
        r"C:\Users\owner\workspace\.omicsclaw",
        r"C:\Users\owner\workspace\.omicsclaw\evolved",
    ]
    assert created == [
        r"C:\Users\owner\workspace\.omicsclaw",
        r"C:\Users\owner\workspace\.omicsclaw\evolved",
    ]
    assert api.closed == [6, 5, 4, 3, 2, 1]


def test_windows_directory_guard_closes_ancestors_when_child_is_rejected() -> None:
    rejected = r"C:\workspace\.omicsclaw\evolved"
    api = _FakeDirectoryApi(fail_at=rejected)

    with pytest.raises(RuntimeError, match="plain directory"):
        with hold_windows_plain_directory_authority(
            r"C:\workspace",
            ".omicsclaw",
            "evolved",
            _api=api,
            _mkdir=lambda _path: None,
        ):
            raise AssertionError("unreachable")

    assert api.opened[-1] == rejected
    assert api.closed == [3, 2, 1]


@pytest.mark.parametrize(
    "root,part",
    [
        (r"relative\workspace", "evolved"),
        (r"C:\workspace", ".."),
        (r"C:\workspace", r"nested\evolved"),
    ],
)
def test_windows_directory_guard_rejects_ambiguous_paths(
    root: str,
    part: str,
) -> None:
    api = _FakeDirectoryApi()

    with pytest.raises(RuntimeError):
        with hold_windows_plain_directory_authority(
            root,
            part,
            _api=api,
            _mkdir=lambda _path: None,
        ):
            raise AssertionError("unreachable")
