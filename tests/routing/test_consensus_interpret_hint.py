"""Slice 10.A — Detection helper for routing layer / agent loop.

is_typed_consensus_run() answers "did this path just become a typed
consensus run output?". suggest_interpret() returns a ready-to-surface
suggestion when it has. Used by /interpret slash command (10.B), can
also be wired into agent-loop after-tool hooks in the future.
"""

from __future__ import annotations

import json
from pathlib import Path


_REQUIRED_ARTIFACTS = ("plan.json", "consensus_labels.tsv", "member_scores.csv", "cross_method_nmi.csv")


def _layout_typed_run(tmp_path: Path, *, run_id: str = "rid", missing: tuple[str, ...] = ()) -> Path:
    d = tmp_path / "typed_run"
    d.mkdir(parents=True, exist_ok=True)
    payloads: dict[str, str] = {
        "plan.json": json.dumps({"run_id": run_id, "operator": "kmode", "members": [], "input_path": str(tmp_path / "fake.h5ad")}),
        "consensus_labels.tsv": "observation\tconsensus_kmode\nobs_0\t0\n",
        "member_scores.csv": "member,composite,cross_nmi_mean,intrinsic,max_class_frac,filtered,filter_reason\nm0,0.6,0.6,0.5,0.3,False,\n",
        "cross_method_nmi.csv": ",m0\nm0,1.0\n",
    }
    for fname, content in payloads.items():
        if fname in missing:
            continue
        (d / fname).write_text(content)
    return d


# --------------------------------------------------------------------------- #
# is_typed_consensus_run                                                      #
# --------------------------------------------------------------------------- #

def test_is_typed_consensus_run_true_when_all_artifacts_present(tmp_path: Path) -> None:
    from omicsclaw.routing.consensus_interpret_hint import is_typed_consensus_run

    d = _layout_typed_run(tmp_path)
    assert is_typed_consensus_run(d) is True


def test_is_typed_consensus_run_false_when_any_artifact_missing(tmp_path: Path) -> None:
    from omicsclaw.routing.consensus_interpret_hint import is_typed_consensus_run

    for missing in _REQUIRED_ARTIFACTS:
        d = _layout_typed_run(tmp_path / f"missing_{missing}", missing=(missing,))
        assert is_typed_consensus_run(d) is False, f"should be False when {missing} absent"


def test_is_typed_consensus_run_false_for_non_directory(tmp_path: Path) -> None:
    from omicsclaw.routing.consensus_interpret_hint import is_typed_consensus_run

    f = tmp_path / "plain_file.txt"
    f.write_text("hi")
    assert is_typed_consensus_run(f) is False


def test_is_typed_consensus_run_false_for_missing_path(tmp_path: Path) -> None:
    from omicsclaw.routing.consensus_interpret_hint import is_typed_consensus_run

    assert is_typed_consensus_run(tmp_path / "nope") is False


# --------------------------------------------------------------------------- #
# suggest_interpret                                                            #
# --------------------------------------------------------------------------- #

def test_suggest_interpret_returns_suggestion_for_typed_run(tmp_path: Path) -> None:
    from omicsclaw.routing.consensus_interpret_hint import suggest_interpret

    d = _layout_typed_run(tmp_path, run_id="test123")
    suggestion = suggest_interpret(d)

    assert suggestion is not None
    assert suggestion.typed_run_dir == d.resolve()
    assert suggestion.typed_run_id == "test123"
    # args_hint embeds both --input and --output paths
    assert "--input" in suggestion.args_hint
    assert str(d.resolve()) in suggestion.args_hint
    assert "_interpreted" in suggestion.args_hint


def test_suggest_interpret_returns_none_for_non_typed_run(tmp_path: Path) -> None:
    from omicsclaw.routing.consensus_interpret_hint import suggest_interpret

    assert suggest_interpret(tmp_path) is None


def test_suggest_interpret_run_id_falls_back_to_dirname_when_plan_lacks_run_id(tmp_path: Path) -> None:
    from omicsclaw.routing.consensus_interpret_hint import suggest_interpret

    d = _layout_typed_run(tmp_path)
    # Overwrite plan.json with a copy that omits run_id
    (d / "plan.json").write_text(json.dumps({"operator": "kmode", "members": []}))

    suggestion = suggest_interpret(d)
    assert suggestion is not None
    assert suggestion.typed_run_id == d.name  # falls back to dir name


def test_suggest_interpret_typed_run_id_falls_back_when_plan_malformed(tmp_path: Path) -> None:
    """Malformed plan.json should not cause an exception — fall back gracefully."""
    from omicsclaw.routing.consensus_interpret_hint import suggest_interpret

    d = _layout_typed_run(tmp_path)
    (d / "plan.json").write_text("{not valid")
    suggestion = suggest_interpret(d)
    assert suggestion is not None
    assert suggestion.typed_run_id == d.name


def test_suggest_interpret_args_hint_is_ready_to_paste_into_slash_run(tmp_path: Path) -> None:
    """args_hint should be a valid /run argument string."""
    from omicsclaw.routing.consensus_interpret_hint import suggest_interpret

    d = _layout_typed_run(tmp_path)
    suggestion = suggest_interpret(d)
    # First token is skill name
    assert suggestion.args_hint.startswith("consensus-interpret")
    # Includes --input and --output flags
    assert "--input" in suggestion.args_hint
    assert "--output" in suggestion.args_hint
