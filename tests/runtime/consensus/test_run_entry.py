"""Smoke tests for the generic consensus entry ``run.py`` (ADR 0016 T6).

Stubs ``omicsclaw.skill.runner.run_skill`` (the late-imported default runner
inside ``fan_out``) so the whole typed pipeline runs without real subprocesses.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from omicsclaw.common.report import validate_result_envelope
from omicsclaw.runtime.consensus import run as run_mod


def _spatial_stub(label_arrays: dict[str, list[int]]):
    class _StubResult:
        exit_code = 0

    def _runner(**kwargs):
        out = Path(kwargs["output_dir"])
        figure_dir = out / "figure_data"
        figure_dir.mkdir(parents=True, exist_ok=True)
        extra = kwargs.get("extra_args", [])
        method = None
        for i, tok in enumerate(extra):
            if tok == "--method" and i + 1 < len(extra):
                method = extra[i + 1]
                break
        method = method or out.name
        labels = label_arrays.get(method) or label_arrays.get(out.name) or [0, 0, 1, 1]
        pd.DataFrame(
            {"observation": [f"obs_{i}" for i in range(len(labels))], "spatial_domain": labels}
        ).to_csv(figure_dir / "spatial_full.csv", index=False)
        (out / "summary.json").write_text(json.dumps({"summary": {"mean_local_purity": 0.7}}))
        return _StubResult()

    return _runner


_LABELS = {
    "banksy": [0, 0, 0, 1, 1, 1, 2, 2, 2],
    "graphst": [0, 0, 0, 1, 1, 1, 2, 2, 2],
    "leiden": [0, 0, 0, 1, 1, 1, 2, 2, 2],
}


def test_run_entry_consensus_domains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omicsclaw.skill.runner.run_skill", _spatial_stub(_LABELS))
    out = tmp_path / "out"
    rc = run_mod.main(
        [
            "--source", "consensus-domains",
            "--input", str(tmp_path / "fake.h5ad"),
            "--output", str(out),
            "--members", "banksy,graphst,leiden",
            "--non-interactive",
            "--operator", "kmode",
            "--seed", "0",
        ]
    )
    assert rc == 0
    for name in ("report.md", "consensus_labels.tsv", "member_scores.csv", "cross_method_nmi.csv", "plan.json"):
        assert (out / name).exists(), f"missing {name}"
    banner = (out / "report.md").read_text().splitlines()[0]
    assert banner.startswith("[A: Verified consensus]")
    # Title comes from the source row, not a hardcoded wrapper string.
    assert "spatial domains" in (out / "report.md").read_text()
    result_payload = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert validate_result_envelope(result_payload) == []
    assert result_payload["summary"]["method"] == "kmode"


def test_reserved_flags_are_accepted_but_not_consumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--n-clusters`` / ``--llm-judge`` parse and run, but change no output.

    They are documented as reserved (accepted, not consumed) across run.py help,
    ADR 0010, and the shim parameters.yaml. This pins that contract: passing them
    must neither be rejected nor alter the consensus labels. If a future change
    wires either flag into the driver, this comparison fails — a signal to update
    the docs to match.
    """
    monkeypatch.setattr("omicsclaw.skill.runner.run_skill", _spatial_stub(_LABELS))
    common = [
        "--source", "consensus-domains",
        "--input", str(tmp_path / "fake.h5ad"),
        "--members", "banksy,graphst,leiden",
        "--non-interactive", "--operator", "kmode", "--seed", "0",
    ]

    out_base = tmp_path / "base"
    assert run_mod.main(common + ["--output", str(out_base)]) == 0

    out_flags = tmp_path / "flagged"
    rc_flags = run_mod.main(
        common + ["--output", str(out_flags), "--n-clusters", "7", "--llm-judge"]
    )
    assert rc_flags == 0  # reserved flags are accepted, not rejected

    # Reserved => not consumed: the consensus labels are byte-identical.
    assert (out_flags / "consensus_labels.tsv").read_text() == (
        out_base / "consensus_labels.tsv"
    ).read_text()


def test_run_entry_unknown_source_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit):
        run_mod.main(["--source", "nope", "--output", "/tmp/x"])


def test_consensus_result_inventory_rejects_claim_aliases(tmp_path: Path) -> None:
    from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    claim = output_dir / OUTPUT_CLAIM_FILENAME
    claim.write_text("{}\n", encoding="utf-8")
    alias = output_dir / "consensus_labels.tsv"
    alias.hardlink_to(claim)
    run = SimpleNamespace(
        run_id="consensus-test",
        members=(),
        team_result=SimpleNamespace(n_survived=0),
        selected_bcs=(),
        artifacts_written=(alias,),
    )

    run_mod._write_consensus_result(
        source_name="consensus-domains",
        input_path="",
        output_dir=output_dir,
        operator="kmode",
        run=run,
    )

    payload = json.loads((output_dir / "result.json").read_text(encoding="utf-8"))
    assert payload["data"]["artifacts"] == []
