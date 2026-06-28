"""parse_doi must not corrupt a DOI's registrant (audit F).

`doi.lstrip('10.')` strips the CHARACTER SET {'1','0','.'}, so a DOI missing the
`10.` prefix like `1038/foo` became `38/foo` → `10.38/foo` (wrong registrant).
The fix uses ``removeprefix`` (exact-prefix), so `1038/foo` → `10.1038/foo`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "literature"))

import core.parser as parser  # noqa: E402


def _captured_doi(monkeypatch, doi_in: str) -> str:
    seen = {}
    monkeypatch.setattr(parser, "parse_url", lambda url: seen.setdefault("url", url))
    parser.parse_doi(doi_in)
    # url is https://doi.org/<doi>
    return seen["url"].rsplit("/", 1)[-1] if "doi.org/" not in seen["url"] else seen["url"].split("doi.org/", 1)[1]


def test_parse_doi_missing_prefix_keeps_registrant(monkeypatch):
    assert _captured_doi(monkeypatch, "1038/foo") == "10.1038/foo"


def test_parse_doi_already_prefixed_unchanged(monkeypatch):
    assert _captured_doi(monkeypatch, "10.1038/nature12345") == "10.1038/nature12345"


def test_parse_doi_strips_surrounding_whitespace(monkeypatch):
    assert _captured_doi(monkeypatch, "  10.1126/science.abc  ") == "10.1126/science.abc"
