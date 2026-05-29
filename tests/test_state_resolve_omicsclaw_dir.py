"""Regression: ``_resolve_omicsclaw_dir`` must locate the project root by
walking up to the ``omicsclaw.py`` + ``omicsclaw/`` sentinels, not by a
hardcoded ``parent.parent``.

The function began life at ``bot/core.py`` — one level under the project root,
where ``parent.parent`` was correct. The ADR 0001 carve-out relocated it to
``omicsclaw/runtime/agent/state.py`` — three levels under the root — which made
the old ``parent.parent`` check land on ``omicsclaw/runtime/`` and never find
``omicsclaw.py``. Every source-tree / editable install therefore fell through
to the ``~/.omicsclaw`` bundled-runtime fallback: wrong ``OMICSCLAW_DIR``, the
project ``.env`` never loaded, the project ``data/`` dir not trusted.
"""

from __future__ import annotations

from pathlib import Path

from omicsclaw.runtime.agent.state import _resolve_omicsclaw_dir


def _make_source_tree(root: Path) -> Path:
    """Lay down a minimal source tree; return its nested ``state.py`` path."""
    agent_dir = root / "omicsclaw" / "runtime" / "agent"
    agent_dir.mkdir(parents=True)
    (root / "omicsclaw.py").write_text("# CLI entrypoint\n", encoding="utf-8")
    (root / "omicsclaw" / "__init__.py").write_text("", encoding="utf-8")
    state_file = agent_dir / "state.py"
    state_file.write_text("# state\n", encoding="utf-8")
    return state_file


def test_resolve_finds_root_from_deeply_nested_module(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OMICSCLAW_DIR", raising=False)
    root = tmp_path / "OmicsClaw"
    state_file = _make_source_tree(root)
    assert _resolve_omicsclaw_dir(start=state_file) == root.resolve()


def test_resolve_falls_back_when_no_source_marker(tmp_path, monkeypatch) -> None:
    # Mimic a non-editable pip install: no top-level omicsclaw.py exists above
    # the package tree, so resolution must fall back to ~/.omicsclaw. The
    # site-packages dir holds an ``omicsclaw/`` package but no ``omicsclaw.py``
    # file, so the two-sentinel check must not match it.
    monkeypatch.delenv("OMICSCLAW_DIR", raising=False)
    pkg_state = (
        tmp_path / "site-packages" / "omicsclaw" / "runtime" / "agent" / "state.py"
    )
    pkg_state.parent.mkdir(parents=True)
    pkg_state.write_text("# state\n", encoding="utf-8")
    assert (
        _resolve_omicsclaw_dir(start=pkg_state)
        == (Path.home() / ".omicsclaw").resolve()
    )


def test_resolve_honours_env_override(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OMICSCLAW_DIR", str(tmp_path))
    # The explicit override wins even when ``start`` points at an unrelated tree.
    assert _resolve_omicsclaw_dir(start=tmp_path / "x" / "state.py") == tmp_path.resolve()
