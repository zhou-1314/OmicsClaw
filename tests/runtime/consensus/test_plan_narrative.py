"""Tests for plan (chair) + narrative (B path)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from omicsclaw.common.output_claim import OUTPUT_CLAIM_FILENAME
from omicsclaw.runtime.consensus.dispatch import output_banner
from omicsclaw.runtime.consensus.narrative.extractor import (
    MemberExtraction,
    extract_member_findings,
    render_extract_prompt,
)
from omicsclaw.runtime.consensus.narrative.synthesizer import (
    synthesize_narrative,
)
from omicsclaw.runtime.consensus.plan import (
    PlannedMember,
    load_param_hints,
    propose_members,
)


def _write_params_yaml(tmp_path: Path) -> Path:
    yaml_text = textwrap.dedent(
        """
        domain: spatial
        param_hints:
          banksy:
            params: [lambda_param, k_nn]
            defaults: {lambda_param: 0.3, k_nn: 18}
          graphst:
            params: [epochs]
            defaults: {epochs: 600}
          leiden:
            params: [resolution]
            defaults: {resolution: 1.0}
          sedr:
            params: [epochs]
            defaults: {epochs: 200}
          spagcn:
            params: [n_domains]
            defaults: {n_domains: 7}
        """
    ).strip()
    p = tmp_path / "parameters.yaml"
    p.write_text(yaml_text)
    return p


# ----------------------- plan ---------------------- #

def test_load_param_hints_returns_methods(tmp_path: Path) -> None:
    p = _write_params_yaml(tmp_path)
    hints = load_param_hints(p)
    assert set(hints.keys()) == {"banksy", "graphst", "leiden", "sedr", "spagcn"}


def test_load_param_hints_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_param_hints(tmp_path / "nope.yaml") == {}


def test_deterministic_fallback_picks_alphabetic_first_n(tmp_path: Path) -> None:
    p = _write_params_yaml(tmp_path)
    plan = propose_members(
        query="test",
        skill_name="spatial-domains",
        parameters_yaml_path=p,
        n=3,
        chair_llm=lambda prompt: None,  # force fallback
    )
    methods = [m.method for m in plan]
    assert methods == ["banksy", "graphst", "leiden"]
    assert plan[0].params == {"lambda-param": "0.3", "k-nn": "18"}


def test_offline_mode_raises_when_disabled(tmp_path: Path) -> None:
    p = _write_params_yaml(tmp_path)
    with pytest.raises(RuntimeError, match="LLM plan unavailable"):
        propose_members(
            query="x",
            skill_name="spatial-domains",
            parameters_yaml_path=p,
            chair_llm=lambda prompt: None,
            allow_offline=False,
        )


def test_llm_plan_parsed_and_unknown_methods_dropped(tmp_path: Path) -> None:
    p = _write_params_yaml(tmp_path)
    payload = json.dumps(
        {
            "members": [
                {"method": "banksy", "params": {"lambda-param": "0.5"}, "rationale": "graph"},
                {"method": "bogus", "params": {}, "rationale": "n/a"},
                {"method": "graphst", "params": {"epochs": "400"}, "rationale": "gnn"},
            ]
        }
    )
    plan = propose_members(
        query="x",
        skill_name="spatial-domains",
        parameters_yaml_path=p,
        n=5,
        chair_llm=lambda prompt: payload,
    )
    methods = [m.method for m in plan]
    assert methods == ["banksy", "graphst"]


def test_planned_member_converts_to_consensus_member(tmp_path: Path) -> None:
    pm = PlannedMember(method="leiden", params={"resolution": "1.0"}, rationale="x")
    cm = pm.to_consensus_member(skill_name="spatial-domains")
    assert cm.name == "leiden_resolution-1.0"
    assert cm.params["method"] == "leiden"
    assert cm.params["resolution"] == "1.0"


def test_invalid_llm_json_falls_back(tmp_path: Path) -> None:
    p = _write_params_yaml(tmp_path)
    plan = propose_members(
        query="x",
        skill_name="spatial-domains",
        parameters_yaml_path=p,
        n=2,
        chair_llm=lambda prompt: "this-is-not-json",
    )
    assert [m.method for m in plan] == ["banksy", "graphst"]


# ---------------- C2 regression: prompts must survive curly braces ---------- #

def test_plan_prompt_survives_curly_braces_in_query(tmp_path: Path) -> None:
    """User queries can legitimately contain '{' or '}' (e.g. JSON snippets,
    gene-set syntax). The rendering must not raise ``KeyError``/``IndexError``
    just because Python's ``.format()`` would interpret them as placeholders.
    """
    p = _write_params_yaml(tmp_path)
    captured: dict[str, str] = {}

    def capture(prompt: str) -> None:
        captured["p"] = prompt
        return None  # force fallback

    plan = propose_members(
        query="give me a {gene}: TP53-like behaviour with 'params': {n: 7}",
        skill_name="spatial-domains",
        parameters_yaml_path=p,
        n=2,
        chair_llm=capture,
    )
    # The fallback list should still produce 2 members AND the prompt should
    # have been rendered without raising.
    assert [m.method for m in plan] == ["banksy", "graphst"]
    assert "{gene}" in captured["p"]
    assert "{n: 7}" in captured["p"]


def test_extract_prompt_survives_curly_braces_in_report(tmp_path: Path) -> None:
    """An upstream skill report can legitimately contain JSON / templated
    strings with literal braces; rendering must not crash on them.
    """
    report = tmp_path / "report.md"
    report.write_text("- {literal_curly_in_finding}\n- json snippet: {\"key\": 1}\n")
    out = extract_member_findings(
        member_name="m1",
        skill_name="spatial-domains",
        report_path=report,
        llm=lambda prompt: None,  # offline path renders prompt then falls back
    )
    # Offline heuristic should successfully pick up the bullet lines that
    # contain braces.
    assert any("{literal_curly_in_finding}" in f for f in out.key_findings)


@pytest.mark.parametrize(
    "alias_kind",
    ("symlink", "hardlink", "claim", "escaping_symlink"),
)
def test_extract_member_findings_rejects_unowned_report_aliases(
    tmp_path: Path,
    alias_kind: str,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    owned_report = run_dir / "owned-report.md"
    owned_report.write_text("- trusted finding\n", encoding="utf-8")

    if alias_kind == "claim":
        report_path = run_dir / OUTPUT_CLAIM_FILENAME
        report_path.write_text("- internal metadata\n", encoding="utf-8")
    else:
        report_path = run_dir / "report.md"
        if alias_kind == "symlink":
            report_path.symlink_to(owned_report.name)
        elif alias_kind == "hardlink":
            report_path.hardlink_to(owned_report)
        else:
            outside_report = tmp_path / "outside-report.md"
            outside_report.write_text("- outside finding\n", encoding="utf-8")
            report_path.symlink_to(outside_report)

    with pytest.raises(FileNotFoundError, match="report not found"):
        extract_member_findings(
            member_name="m1",
            skill_name="spatial-domains",
            report_path=report_path,
            llm=lambda prompt: pytest.fail("untrusted report reached the LLM"),
        )


def test_synthesize_prompt_survives_curly_braces_in_extractions() -> None:
    """Per-member findings may contain '{' or '}' (e.g. gene-set literal);
    the synthesiser must render its prompt without raising.
    """
    captured: dict[str, str] = {}

    def capture(prompt: str) -> str:
        captured["p"] = prompt
        return "## Agreement\n- ok"

    extractions = [
        MemberExtraction("m1", "spatial-domains",
                         ("found cluster {region: cortex}",),
                         {"weird {key}": 3},
                         "medium"),
        MemberExtraction("m2", "spatial-domains",
                         ("found {other}",),
                         {},
                         "low"),
    ]
    report = synthesize_narrative(
        query="explore {tissue: brain}",
        skill_name="spatial-domains",
        extractions=extractions,
        llm=capture,
    )
    assert report.used_llm is True
    # The braces from query + extractions must appear verbatim in the rendered prompt.
    assert "{tissue: brain}" in captured["p"]
    assert "{region: cortex}" in captured["p"]


# ----------------------- narrative ----------------- #

def test_extract_prompt_renders_template_with_fields(tmp_path: Path) -> None:
    out = render_extract_prompt(
        member_name="banksy_default",
        skill_name="spatial-domains",
        report_text="- finding alpha\n- finding beta",
    )
    assert "banksy_default" in out
    assert "spatial-domains" in out
    assert "finding alpha" in out


def test_extract_offline_heuristic_pulls_bullets(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text(
        textwrap.dedent(
            """
            # report

            - identified 7 spatial domains
            - mean local purity 0.81
            * tissue: cortex
            1. layer 1 dominated by neurons
            """
        )
    )
    extraction = extract_member_findings(
        member_name="m1",
        skill_name="spatial-domains",
        report_path=report,
        llm=lambda prompt: None,
    )
    assert extraction.confidence == "low"
    assert len(extraction.key_findings) >= 3
    assert any("spatial domains" in f for f in extraction.key_findings)
    assert "offline heuristic" in " ".join(extraction.caveats)


def test_extract_returns_offline_extraction_override(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text("contents")
    custom = MemberExtraction(
        member_name="m1",
        skill_name="spatial-domains",
        key_findings=("preset finding",),
        key_numbers={"n_obs": 100},
        confidence="high",
        caveats=(),
    )
    out = extract_member_findings(
        member_name="m1",
        skill_name="spatial-domains",
        report_path=report,
        llm=lambda prompt: None,
        offline_extraction=custom,
    )
    assert out is custom


def test_extract_uses_llm_when_available(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text("- A\n- B")
    payload = json.dumps(
        {
            "key_findings": ["LLM-extracted 1", "LLM-extracted 2"],
            "key_numbers": {"k": 7},
            "confidence": "high",
            "caveats": ["small sample"],
        }
    )
    out = extract_member_findings(
        member_name="m1",
        skill_name="spatial-domains",
        report_path=report,
        llm=lambda prompt: payload,
    )
    assert out.confidence == "high"
    assert "LLM-extracted 1" in out.key_findings
    assert out.key_numbers == {"k": 7}


def test_synthesize_offline_uses_template_banner() -> None:
    extractions = [
        MemberExtraction("m1", "spatial-domains", ("layer 1 found",), {}, "medium"),
        MemberExtraction("m2", "spatial-domains", ("layer 1 found",), {}, "high"),
    ]
    report = synthesize_narrative(
        query="show me layers",
        skill_name="spatial-domains",
        extractions=extractions,
        llm=lambda prompt: None,
    )
    assert report.used_llm is False
    assert report.markdown.startswith(output_banner("narrative"))
    assert "## Agreement" in report.markdown
    assert "## Contradictions" in report.markdown
    assert "## Per-member confidence" in report.markdown


def test_synthesize_llm_path_force_prepends_banner() -> None:
    extractions = [
        MemberExtraction("m1", "spatial-domains", ("only-finding",), {}, "low"),
        MemberExtraction("m2", "spatial-domains", ("other-finding",), {}, "low"),
    ]
    report = synthesize_narrative(
        query="x",
        skill_name="spatial-domains",
        extractions=extractions,
        llm=lambda prompt: "## Agreement\n- both agree on nothing",
    )
    assert report.used_llm is True
    assert report.markdown.startswith(output_banner("narrative"))


def test_synthesize_rejects_empty_extractions() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        synthesize_narrative(query="x", skill_name="s", extractions=[])
