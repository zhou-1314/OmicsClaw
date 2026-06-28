"""`discover_file` must not bypass the trusted-dir gate (audit B-1).

`validate_input_path` rejects absolute paths outside the trusted data dirs, but
`discover_file`'s absolute-path branch returned ANY existing file. Agent callers
(the skill executor and `_resolve_trusted_data_paths`) fall back to
`discover_file` when `validate_input_path` returns None and use the result
without re-validating — so a prompt-injected / remote-job absolute path could be
read despite the trust gate (arbitrary file read, amplified by the remote job
routers). The fix makes the absolute branch enforce `_is_trusted_root`.
"""

from __future__ import annotations

from pathlib import Path

import omicsclaw.services.path_validation as pv


def test_discover_file_absolute_path_must_be_trusted(tmp_path: Path, monkeypatch):
    trusted = tmp_path / "trusted_data"
    trusted.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    inside_file = trusted / "counts.h5ad"
    inside_file.write_bytes(b"\x00")
    outside_file = outside / "secret.h5ad"
    outside_file.write_bytes(b"\x00")

    # Make `trusted` the only trusted data dir for this test.
    monkeypatch.setattr(pv, "TRUSTED_DATA_DIRS", [trusted])

    # Absolute path INSIDE a trusted dir stays discoverable.
    assert pv.discover_file(str(inside_file)) == [inside_file]

    # Absolute path OUTSIDE every trusted dir must be rejected (the B-1 bypass).
    assert pv.discover_file(str(outside_file)) == []


def test_discover_file_absolute_missing_is_empty(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(pv, "TRUSTED_DATA_DIRS", [tmp_path])
    assert pv.discover_file(str(tmp_path / "does-not-exist.h5ad")) == []


def test_discover_file_relative_traversal_is_rejected(tmp_path: Path, monkeypatch):
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    secret = tmp_path / "secret.h5ad"  # one level ABOVE the trusted dir
    secret.write_bytes(b"\x00")
    monkeypatch.setattr(pv, "TRUSTED_DATA_DIRS", [trusted])
    # ``trusted / "../secret.h5ad"`` exists on disk but resolves outside the
    # trusted root — must not be returned.
    assert pv.discover_file("../secret.h5ad") == []


def test_discover_file_symlink_escape_is_rejected(tmp_path: Path, monkeypatch):
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    real = outside / "real.h5ad"
    real.write_bytes(b"\x00")
    link = trusted / "link.h5ad"
    link.symlink_to(real)  # a symlink INSIDE the trusted dir pointing OUT
    monkeypatch.setattr(pv, "TRUSTED_DATA_DIRS", [trusted])
    # The symlink is_file() within the trusted dir, but resolves outside it.
    assert pv.discover_file("link.h5ad") == []
