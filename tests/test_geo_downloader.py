"""Regression test for the GEO supplementary-file downloader (audit A-4).

The HTML directory-listing regex used TWO capture groups, so ``re.findall``
returned ``(filename, ext)`` tuples; the loop then did ``ftp_base + filename``
and ``output_dir / filename`` on a *tuple*, raising ``TypeError`` that the outer
``except`` swallowed — leaving only ``metadata.json`` and delivering zero data
files to downstream analysis. These tests pin the fix: the listing parses into
plain filenames and the files are actually written.
"""

from __future__ import annotations

from pathlib import Path

import skills.literature.core.downloader as dl


class _Resp:
    def __init__(self, *, text: str = "", status: int = 200, content: bytes = b""):
        self.text = text
        self.status_code = status
        self._content = content

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int = 8192):
        yield self._content


def _fake_get_factory(listing: str):
    def fake_get(url: str, **_kw):
        if url.endswith("/suppl/"):
            return _Resp(text=listing, status=200)
        return _Resp(status=200, content=b"DATA")

    return fake_get


def test_supplementary_listing_parses_to_filenames(monkeypatch, tmp_path: Path):
    listing = (
        '<a href="GSE123456_matrix.mtx.gz">matrix</a>\n'
        '<a href="GSE123456_barcodes.tsv.gz">barcodes</a>\n'
        '<a href="GSE123456_features.tsv.gz">features</a>\n'
        '<a href="filelist.txt">filelist</a>\n'
        '<a href="GSE123456_RAW.tar">raw</a>\n'
    )
    monkeypatch.setattr(dl.requests, "get", _fake_get_factory(listing))

    out = dl.download_supplementary_files("GSE123456", tmp_path, max_retries=1)

    names = sorted(Path(p).name for p in out)
    assert "GSE123456_matrix.mtx.gz" in names
    assert "GSE123456_barcodes.tsv.gz" in names
    assert "GSE123456_RAW.tar" in names
    assert "filelist.txt" in names
    # Every returned path is a real written file, and none is a stringified tuple.
    for p in out:
        assert Path(p).is_file()
        assert "(" not in Path(p).name and "'" not in Path(p).name


def test_supplementary_download_delivers_nonempty(monkeypatch, tmp_path: Path):
    """The whole point of A-4: literature must deliver *data*, not just
    metadata. With files present in the listing, the function returns a
    non-empty list (pre-fix it silently returned [])."""
    listing = '<a href="GSE1_counts.csv">c</a>'
    monkeypatch.setattr(dl.requests, "get", _fake_get_factory(listing))
    out = dl.download_supplementary_files("GSE1000", tmp_path, max_retries=1)
    assert out, "supplementary download must deliver at least one data file"
    assert Path(out[0]).read_bytes() == b"DATA"
