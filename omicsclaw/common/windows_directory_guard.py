"""Windows kernel-handle guard for path-based owned output mutation.

Python does not expose ``dir_fd`` mutation APIs on Windows.  A directory
opened by ``CreateFileW`` without ``FILE_SHARE_DELETE`` cannot be renamed or
deleted while its handle is retained; ``FILE_FLAG_OPEN_REPARSE_POINT`` lets us
inspect and reject the directory entry itself instead of following it.  Hold
one such handle for every lexical component while a same-directory atomic
write is in progress.

This module is imported lazily by the cross-platform writer.  Keeping Win32
loading inside ``_Win32DirectoryApi`` makes the traversal contract testable on
non-Windows CI without pretending that those tests are a native Windows smoke.
"""

from __future__ import annotations

from contextlib import contextmanager
import ntpath
import os
from typing import Iterator, Protocol


class _DirectoryHandleApi(Protocol):
    def open_plain_directory(self, path: str) -> int: ...

    def close(self, handle: int) -> None: ...


def _extended_windows_path(path: str) -> str:
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


class _Win32DirectoryApi:
    """Small ctypes boundary around CreateFileW/GetFileInformationByHandle."""

    _FILE_READ_ATTRIBUTES = 0x0080
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows directory handles are unavailable")

        import ctypes
        from ctypes import wintypes

        class _ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        self._ctypes = ctypes
        self._kernel32 = kernel32
        self._information_type = _ByHandleFileInformation
        self._invalid_handle = ctypes.c_void_p(-1).value

    def open_plain_directory(self, path: str) -> int:
        handle = self._kernel32.CreateFileW(
            _extended_windows_path(path),
            self._FILE_READ_ATTRIBUTES,
            self._FILE_SHARE_READ | self._FILE_SHARE_WRITE,
            None,
            self._OPEN_EXISTING,
            self._FILE_FLAG_BACKUP_SEMANTICS | self._FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        handle_value = getattr(handle, "value", handle)
        raw_handle = int(handle_value) if handle_value is not None else 0
        if raw_handle in {0, self._invalid_handle}:
            error = self._ctypes.get_last_error()
            raise OSError(error, self._ctypes.FormatError(error), path)

        information = self._information_type()
        if not self._kernel32.GetFileInformationByHandle(
            handle,
            self._ctypes.byref(information),
        ):
            error = self._ctypes.get_last_error()
            self._kernel32.CloseHandle(handle)
            raise OSError(error, self._ctypes.FormatError(error), path)
        attributes = int(information.dwFileAttributes)
        if (
            attributes & self._FILE_ATTRIBUTE_REPARSE_POINT
            or not attributes & self._FILE_ATTRIBUTE_DIRECTORY
        ):
            self._kernel32.CloseHandle(handle)
            raise RuntimeError(f"owned output path is not a plain directory: {path}")
        return raw_handle

    def close(self, handle: int) -> None:
        self._kernel32.CloseHandle(handle)


def _absolute_windows_components(path: str) -> tuple[str, tuple[str, ...]]:
    raw = os.fspath(path)
    drive, tail = ntpath.splitdrive(raw)
    if not drive or not tail.startswith(("\\", "/")):
        raise RuntimeError(f"owned output root is not absolute: {raw}")
    parts = tuple(part for part in tail.replace("/", "\\").split("\\") if part)
    if any(part in {".", ".."} for part in parts):
        raise RuntimeError("owned output root has an unsafe path component")
    anchor = drive + "\\"
    return anchor, parts


@contextmanager
def hold_windows_plain_directory_authority(
    output_root: str | os.PathLike[str],
    *relative_parts: str,
    _api: _DirectoryHandleApi | None = None,
    _mkdir: object = os.mkdir,
) -> Iterator[str]:
    """Hold non-delete-sharing handles through one Windows output directory.

    The yielded string is presentation metadata for path-based file APIs.  The
    retained handles are the authority: every component is opened as a plain
    non-reparse directory and cannot be renamed until the context exits.
    """

    api = _api or _Win32DirectoryApi()
    anchor, root_parts = _absolute_windows_components(os.fspath(output_root))
    handles: list[int] = []
    current = anchor
    try:
        handles.append(api.open_plain_directory(current))
        for part in root_parts:
            current = ntpath.join(current, part)
            handles.append(api.open_plain_directory(current))
        for part in relative_parts:
            if (
                not isinstance(part, str)
                or part in {"", ".", ".."}
                or ntpath.basename(part) != part
                or "\x00" in part
            ):
                raise RuntimeError("unsafe owned output directory component")
            current = ntpath.join(current, part)
            try:
                _mkdir(current)  # type: ignore[operator]
            except FileExistsError:
                pass
            handles.append(api.open_plain_directory(current))
        yield current
    finally:
        for handle in reversed(handles):
            api.close(handle)


__all__ = ["hold_windows_plain_directory_authority"]
