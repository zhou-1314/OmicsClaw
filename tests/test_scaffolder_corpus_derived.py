"""P5 — corpus-derived skill scaffolding (docs/proposals/skill-acquisition-plan.md §P5).

The iron rule: never fabricate a numeric default. Every ``defaults`` entry a
corpus-derived scaffold ships must have a matching ``source_refs`` entry that
is a real, re-verifiable (quote, char_span, doc_ref) triple — never
``{"todo": True}``.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from omicsclaw.skill.scaffolder import (
    CorpusDerivedBundle,
    CorpusParamCandidate,
    _build_corpus_hints,
    _load_corpus_bundle,
    _render_corpus_provenance_evidence,
    create_skill_scaffold,
    render_corpus_skill_script,
)
from omicsclaw.skill.schema import validate_skill_yaml
from omicsclaw.common.report import SCAFFOLD_STATUS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from skill_lint import lint_skill  # noqa: E402

FIXTURE_PAPER = ROOT / "tests" / "fixtures" / "demo_paper.txt"


# ---- _load_corpus_bundle ----


def test_load_corpus_bundle_extracts_candidates_and_metadata():
    text = "Leiden clustering used resolution=0.8 for downstream analysis."
    bundle = _load_corpus_bundle(text, source_kind="paper", doc_ref="10.1038/xyz")
    assert bundle.source_kind == "paper"
    assert bundle.doc_ref == "10.1038/xyz"
    assert bundle.corpus_text == text
    assert len(bundle.candidates) == 1
    assert bundle.candidates[0].param == "resolution"
    assert bundle.candidates[0].value == 0.8
    assert "10.1038/xyz" in bundle.goal


def test_load_corpus_bundle_handles_no_candidates():
    bundle = _load_corpus_bundle("plain prose, nothing to extract", source_kind="tool_docs", doc_ref="readme.txt")
    assert bundle.candidates == []


# ---- _build_corpus_hints ----


def test_build_corpus_hints_only_sourced_candidates_get_defaults():
    candidates = [
        CorpusParamCandidate(
            param="resolution", operator="=", value=0.8,
            quote="resolution=0.8", char_span=(0, 14), todo=False,
        ),
        CorpusParamCandidate(
            param="min_genes", operator="", value=None,
            quote="", char_span=(0, 0), todo=True,
        ),
    ]
    hints = _build_corpus_hints(candidates, method="default", doc_ref="10.1038/xyz")
    info = hints["default"]
    assert info["params"] == ["resolution", "min_genes"]
    assert info["defaults"] == {"resolution": 0.8}  # TODO candidate excluded
    assert info["source_refs"]["resolution"] == {
        "quote": "resolution=0.8", "char_span": [0, 14], "doc_ref": "10.1038/xyz",
    }
    assert info["source_refs"]["min_genes"] == {"todo": True}


def test_build_corpus_hints_empty_candidates_returns_empty_dict():
    assert _build_corpus_hints([], method="default", doc_ref="x") == {}


# ---- render_corpus_skill_script ----


def _make_bundle(**overrides) -> CorpusDerivedBundle:
    defaults = dict(
        source_kind="paper",
        doc_ref="10.1038/xyz",
        corpus_text="resolution=0.8 was used.",
        candidates=[
            CorpusParamCandidate(
                param="resolution", operator="=", value=0.8,
                quote="resolution=0.8", char_span=(0, 14), todo=False,
            )
        ],
        goal="Corpus-derived scaffold.",
    )
    defaults.update(overrides)
    return CorpusDerivedBundle(**defaults)


def test_render_corpus_skill_script_embeds_source_ref_comment():
    bundle = _make_bundle()
    script = render_corpus_skill_script(
        skill_name="demo-skill", domain="spatial", summary="Demo", corpus_bundle=bundle, method="default",
    )
    assert '# source_ref: "resolution=0.8" (chars 0-14) from \'10.1038/xyz\'' in script
    assert "parser.add_argument('--resolution', dest='resolution'" in script
    assert "default=0.8" in script
    ast.parse(script)  # must be valid Python


def test_render_corpus_skill_script_todo_candidate_gets_explicit_todo():
    bundle = _make_bundle(
        candidates=[
            CorpusParamCandidate(param="min_genes", operator="", value=None, quote="", char_span=(0, 0), todo=True)
        ]
    )
    script = render_corpus_skill_script(
        skill_name="demo-skill", domain="spatial", summary="Demo", corpus_bundle=bundle, method="default",
    )
    assert "# TODO(source_ref): 'min_genes' was not found with a verifiable quote" in script
    assert "default=None" in script
    ast.parse(script)


def test_render_corpus_skill_script_still_stamps_scaffold_status():
    bundle = _make_bundle()
    script = render_corpus_skill_script(
        skill_name="demo-skill", domain="spatial", summary="Demo", corpus_bundle=bundle, method="default",
    )
    assert 'envelope["status"] = SCAFFOLD_STATUS' in script
    assert SCAFFOLD_STATUS  # sentinel imported/used, not hardcoded string drift


def test_render_corpus_skill_script_reserved_flag_collision_is_skipped():
    bundle = _make_bundle(
        candidates=[
            CorpusParamCandidate(param="method", operator="=", value=1, quote="method=1", char_span=(0, 8), todo=False)
        ]
    )
    script = render_corpus_skill_script(
        skill_name="demo-skill", domain="spatial", summary="Demo", corpus_bundle=bundle, method="default",
    )
    assert "# skipped: 'method' collides with a reserved scaffold flag" in script
    ast.parse(script)


# ---- _render_corpus_provenance_evidence ----


def test_render_corpus_provenance_evidence_lists_every_candidate():
    bundle = _make_bundle()
    evidence = _render_corpus_provenance_evidence(bundle)
    assert "10.1038/xyz" in evidence
    assert "resolution" in evidence
    assert "resolution=0.8" in evidence


def test_render_corpus_provenance_evidence_no_candidates():
    bundle = _make_bundle(candidates=[])
    evidence = _render_corpus_provenance_evidence(bundle)
    assert "No methodology parameters were extracted" in evidence


# ---- create_skill_scaffold end-to-end ----


def test_create_skill_scaffold_from_corpus_end_to_end(tmp_path: Path):
    result = create_skill_scaffold(
        request="Cluster spots by expression using thresholds from the paper",
        domain="spatial",
        skill_name="corpus-cluster-test",
        from_corpus=FIXTURE_PAPER,
        doc_ref="10.1038/xyz",
        skills_root=tmp_path / "skills",
        create_tests=False,
    )
    assert result.demo_gate_verdict == "skipped"

    skill_dir = Path(result.skill_dir)
    assert validate_skill_yaml(skill_dir / "skill.yaml") == []
    assert lint_skill(skill_dir) == []

    import yaml

    manifest = yaml.safe_load((skill_dir / "skill.yaml").read_text(encoding="utf-8"))
    assert manifest["provenance"]["origin"] == "corpus"
    assert manifest["provenance"]["source_ref"] == "10.1038/xyz"
    hints = manifest["interface"]["parameters"]["hints"]
    assert hints  # non-empty — first time build_scaffold_manifest ever populates this

    source_corpus = (skill_dir / "references" / "source_corpus.txt").read_text(encoding="utf-8")
    assert source_corpus == FIXTURE_PAPER.read_text(encoding="utf-8")
    assert (skill_dir / "references" / "corpus_provenance.md").exists()

    # Iron rule, independently re-verified: every char_span really slices out
    # its own quote from the persisted source text.
    for method_info in hints.values():
        for param, ref in method_info.get("source_refs", {}).items():
            if ref.get("todo"):
                continue
            start, end = ref["char_span"]
            assert source_corpus[start:end] == ref["quote"]


def test_create_skill_scaffold_from_corpus_falls_back_to_filename_doc_ref(tmp_path: Path):
    result = create_skill_scaffold(
        request="Cluster spots by expression",
        domain="spatial",
        skill_name="corpus-cluster-no-docref",
        from_corpus=FIXTURE_PAPER,
        skills_root=tmp_path / "skills",
        create_tests=False,
    )
    skill_dir = Path(result.skill_dir)
    import yaml

    manifest = yaml.safe_load((skill_dir / "skill.yaml").read_text(encoding="utf-8"))
    assert manifest["provenance"]["source_ref"] == "demo_paper.txt"


def test_create_skill_scaffold_from_corpus_mutually_exclusive_with_source_analysis_dir(tmp_path: Path):
    source_dir = tmp_path / "output" / "some-run"
    source_dir.mkdir(parents=True)
    with pytest.raises(ValueError, match="mutually exclusive"):
        create_skill_scaffold(
            request="x",
            domain="spatial",
            from_corpus=FIXTURE_PAPER,
            source_analysis_dir=source_dir,
            skills_root=tmp_path / "skills",
        )


def test_create_skill_scaffold_from_corpus_mutually_exclusive_with_promote_from_latest(tmp_path: Path):
    # Regression (codex cross-validation): promote_from_latest's own
    # resolution (find_latest_autonomous_analysis) raises FileNotFoundError
    # when no prior run exists, BEFORE resolved_source_dir was ever set — an
    # earlier version of this check ran too late and let that exception mask
    # the intended ValueError. Must raise ValueError, not FileNotFoundError,
    # even when there is truly no autonomous run to find.
    with pytest.raises(ValueError, match="mutually exclusive"):
        create_skill_scaffold(
            request="x",
            domain="spatial",
            from_corpus=FIXTURE_PAPER,
            promote_from_latest=True,
            output_root=tmp_path / "empty-output-root",
            skills_root=tmp_path / "skills",
        )


def test_render_corpus_skill_script_handles_newline_in_quote(tmp_path: Path):
    # Regression (codex cross-validation): a multi-line methodology mention
    # (extract_methodology's \s* can match across a literal newline) must not
    # produce a generated script with a broken # comment / syntax error.
    bundle = _load_corpus_bundle(
        "resolution\n=\n0.8 was used", source_kind="paper", doc_ref="paper1"
    )
    assert bundle.candidates and "\n" in bundle.candidates[0].quote
    script = render_corpus_skill_script(
        skill_name="demo-skill", domain="spatial", summary="Demo", corpus_bundle=bundle, method="default",
    )
    ast.parse(script)  # must still be valid Python
    assert "\\n" in script  # escaped, not a literal embedded newline
    for line in script.splitlines():
        if "source_ref" in line:
            assert line.startswith("    # source_ref:")
