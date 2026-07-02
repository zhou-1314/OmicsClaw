"""Tests for omicsclaw.skill.skill_version (SKILL_VERSION sync, ADR 0037)."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from omicsclaw.skill.skill_version import read_script_version, sync_script_version  # noqa: E402


def test_read_version():
    assert read_script_version('A=1\nSKILL_VERSION = "0.2.0"\nB=2\n') == "0.2.0"
    assert read_script_version("SKILL_VERSION = '1.0'\n") == "1.0"
    assert read_script_version("no version here\n") is None


def test_sync_bumps_and_preserves_quoting():
    text = 'X=1\nSKILL_VERSION = "0.2.0"\nY=2\n'
    new, changed = sync_script_version(text, "0.5.0")
    assert changed
    assert 'SKILL_VERSION = "0.5.0"' in new
    assert new.startswith("X=1\n") and new.endswith("Y=2\n")  # nothing else touched


def test_sync_single_quotes_preserved():
    new, changed = sync_script_version("SKILL_VERSION = '0.1.0'\n", "0.3.0")
    assert changed and new == "SKILL_VERSION = '0.3.0'\n"


def test_sync_noop_when_already_equal():
    text = 'SKILL_VERSION = "0.5.0"\n'
    assert sync_script_version(text, "0.5.0") == (text, False)


def test_sync_noop_when_no_constant():
    text = "def main():\n    pass\n"
    assert sync_script_version(text, "0.5.0") == (text, False)


def test_sync_is_idempotent():
    text = 'SKILL_VERSION = "0.2.0"\n'
    once, _ = sync_script_version(text, "0.9.0")
    twice, changed = sync_script_version(once, "0.9.0")
    assert not changed and twice == once
