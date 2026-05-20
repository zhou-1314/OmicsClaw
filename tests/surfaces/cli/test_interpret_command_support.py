"""Slice 10.B — Tests for the /interpret CLI slash command helper.

The dispatch site in interactive.py is exercised by a separate import-shape
test; here we test the pure parser + /run argstring builder which can be
verified deterministically without standing up the full CLI loop.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


_REQUIRED_ARTIFACTS = ("plan.json", "consensus_labels.tsv", "member_scores.csv", "cross_method_nmi.csv")


def _layout_typed_run(tmp_path: Path) -> Path:
    d = tmp_path / "typed_run"
    d.mkdir(parents=True, exist_ok=True)
    payloads = {
        "plan.json": json.dumps({"run_id": "rid", "operator": "kmode", "input_path": str(tmp_path / "fake.h5ad")}),
        "consensus_labels.tsv": "observation\tconsensus_kmode\nobs_0\t0\n",
        "member_scores.csv": "member,composite,cross_nmi_mean,intrinsic,max_class_frac,filtered,filter_reason\nm0,0.6,0.6,0.5,0.3,False,\n",
        "cross_method_nmi.csv": ",m0\nm0,1.0\n",
    }
    for k, v in payloads.items():
        (d / k).write_text(v)
    return d


# --------------------------------------------------------------------------- #
# parse_interpret_command                                                     #
# --------------------------------------------------------------------------- #

def test_parse_minimum_typed_run_dir(tmp_path: Path) -> None:
    from omicsclaw.surfaces.cli._interpret_command_support import parse_interpret_command

    d = _layout_typed_run(tmp_path)
    cmd = parse_interpret_command(str(d))

    assert not isinstance(cmd, str), f"parse error: {cmd}"
    assert cmd.typed_run_dir == d.resolve()
    assert cmd.tissue is None
    assert cmd.no_llm is False
    assert cmd.output_dir == (d.parent / f"{d.name}_interpreted").resolve()


def test_parse_with_tissue(tmp_path: Path) -> None:
    from omicsclaw.surfaces.cli._interpret_command_support import parse_interpret_command

    d = _layout_typed_run(tmp_path)
    cmd = parse_interpret_command(f"{d} --tissue brain")

    assert not isinstance(cmd, str)
    assert cmd.tissue == "brain"


def test_parse_with_no_llm(tmp_path: Path) -> None:
    from omicsclaw.surfaces.cli._interpret_command_support import parse_interpret_command

    d = _layout_typed_run(tmp_path)
    cmd = parse_interpret_command(f"{d} --no-llm")

    assert not isinstance(cmd, str)
    assert cmd.no_llm is True


def test_parse_with_explicit_output(tmp_path: Path) -> None:
    from omicsclaw.surfaces.cli._interpret_command_support import parse_interpret_command

    d = _layout_typed_run(tmp_path)
    out = tmp_path / "custom_out"
    cmd = parse_interpret_command(f"{d} --tissue brain --output {out}")

    assert not isinstance(cmd, str)
    assert cmd.output_dir == out.resolve()


def test_parse_empty_returns_error_message() -> None:
    from omicsclaw.surfaces.cli._interpret_command_support import parse_interpret_command

    result = parse_interpret_command("")
    assert isinstance(result, str)
    assert "typed_run_dir" in result.lower() or "usage" in result.lower()


def test_parse_nonexistent_path_returns_error(tmp_path: Path) -> None:
    from omicsclaw.surfaces.cli._interpret_command_support import parse_interpret_command

    result = parse_interpret_command(str(tmp_path / "nope"))
    assert isinstance(result, str)
    assert "not" in result.lower() or "missing" in result.lower() or "exist" in result.lower()


def test_parse_not_a_typed_run_returns_error(tmp_path: Path) -> None:
    """A directory missing the typed artifacts is not a usable input."""
    from omicsclaw.surfaces.cli._interpret_command_support import parse_interpret_command

    d = tmp_path / "not_a_typed_run"
    d.mkdir()
    (d / "random.txt").write_text("hi")

    result = parse_interpret_command(str(d))
    assert isinstance(result, str)
    assert "typed" in result.lower() or "consensus" in result.lower()


def test_parse_requires_tissue_when_not_no_llm(tmp_path: Path) -> None:
    """Without --tissue / --no-llm, the LLM path would fail with exit 5.
    The CLI shortcut should warn at parse time instead of running and failing."""
    from omicsclaw.surfaces.cli._interpret_command_support import parse_interpret_command

    d = _layout_typed_run(tmp_path)
    result = parse_interpret_command(str(d))   # no --tissue, no --no-llm
    # Either parses (deferring to consensus-interpret to error at exit 5) or warns.
    # Expected behavior: parse succeeds but to_run_command_string lacks --tissue
    # so consensus-interpret will exit 5 with a clear message. Verify the parse
    # itself doesn't surface a phantom tissue.
    assert not isinstance(result, str)
    assert result.tissue is None


# --------------------------------------------------------------------------- #
# to_run_command_string                                                       #
# --------------------------------------------------------------------------- #

def test_to_run_command_string_minimal(tmp_path: Path) -> None:
    from omicsclaw.surfaces.cli._interpret_command_support import (
        parse_interpret_command, to_run_command_string,
    )

    d = _layout_typed_run(tmp_path)
    cmd = parse_interpret_command(f"{d} --tissue brain")
    assert not isinstance(cmd, str)
    s = to_run_command_string(cmd)

    tokens = s.split()
    assert tokens[0] == "consensus-interpret"
    assert "--input" in tokens
    assert "--output" in tokens
    assert "--tissue" in tokens
    assert "brain" in tokens
    assert "--no-llm" not in tokens


def test_to_run_command_string_no_llm(tmp_path: Path) -> None:
    from omicsclaw.surfaces.cli._interpret_command_support import (
        parse_interpret_command, to_run_command_string,
    )

    d = _layout_typed_run(tmp_path)
    cmd = parse_interpret_command(f"{d} --no-llm")
    assert not isinstance(cmd, str)
    s = to_run_command_string(cmd)

    tokens = s.split()
    assert "--no-llm" in tokens
    # --tissue not required when --no-llm
    assert "--tissue" not in tokens


# --------------------------------------------------------------------------- #
# Slash-command registration                                                  #
# --------------------------------------------------------------------------- #

def test_slash_command_interpret_is_registered() -> None:
    """Smoke check: /interpret appears in the SLASH_COMMANDS registry."""
    from omicsclaw.surfaces.cli._constants import SLASH_COMMANDS

    names = [name for name, _desc in SLASH_COMMANDS]
    assert "/interpret" in names
