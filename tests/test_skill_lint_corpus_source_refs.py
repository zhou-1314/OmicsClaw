"""_check_corpus_source_refs — P5 iron rule enforcement (acquisition-plan.md §P5).

A corpus-derived skill (provenance.origin == "corpus") must never ship a
numeric default in `interface.parameters.hints.*.defaults` without a matching,
well-formed, span-verified `source_refs` entry.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from omicsclaw.skill.schema import parse_skill_manifest  # noqa: E402
from scripts import skill_lint  # noqa: E402


def _v2_doc(**over) -> dict:
    data = {
        "schema_version": 2,
        "id": "spatial-demo",
        "name": "spatial-demo",
        "domain": "spatial",
        "version": "1.0.0",
        "summary": {
            "load_when": "demoing the corpus source_refs lint rule",
            "skip_when": [{"condition": "single-cell data", "use": "sc-de"}],
        },
        "runtime": {"language": "python", "entry": "spatial_demo.py"},
        "lifecycle": {"status": "draft"},  # exempts the missing-entry-script check
    }
    data.update(over)
    return data


def _write(skill_dir: Path, doc: dict, *, corpus_text: str | None = None) -> Path:
    skill_dir.mkdir(parents=True, exist_ok=True)
    manifest = parse_skill_manifest(doc)
    (skill_dir / "skill.yaml").write_text(manifest.to_yaml(), encoding="utf-8")
    if corpus_text is not None:
        refs = skill_dir / "references"
        refs.mkdir(parents=True, exist_ok=True)
        (refs / "source_corpus.txt").write_text(corpus_text, encoding="utf-8")
    return skill_dir


_CORPUS_TEXT = "resolution=0.8 was used for clustering."
_GOOD_HINTS = {
    "default": {
        "params": ["resolution"],
        "defaults": {"resolution": 0.8},
        "source_refs": {
            "resolution": {"quote": "resolution=0.8", "char_span": [0, 14], "doc_ref": "10.1038/xyz"},
        },
    }
}


def test_pass_case_well_formed_and_span_verified(tmp_path):
    sd = _write(
        tmp_path / "spatial-demo",
        _v2_doc(
            provenance={"origin": "corpus", "source_ref": "10.1038/xyz"},
            interface={"parameters": {"hints": _GOOD_HINTS}},
        ),
        corpus_text=_CORPUS_TEXT,
    )
    errs = skill_lint.lint_skill(sd)
    assert not any("source_ref" in e or "hints.default" in e for e in errs)


def test_fail_missing_source_refs_entry(tmp_path):
    hints = {
        "default": {
            "params": ["resolution"],
            "defaults": {"resolution": 0.8},
            "source_refs": {},  # no entry at all for 'resolution'
        }
    }
    sd = _write(
        tmp_path / "spatial-demo",
        _v2_doc(
            provenance={"origin": "corpus", "source_ref": "10.1038/xyz"},
            interface={"parameters": {"hints": hints}},
        ),
        corpus_text=_CORPUS_TEXT,
    )
    errs = skill_lint.lint_skill(sd)
    assert any("'resolution' has a default" in e and "no source_ref" in e for e in errs)


def test_fail_todo_alongside_live_default(tmp_path):
    hints = {
        "default": {
            "params": ["resolution"],
            "defaults": {"resolution": 0.8},
            "source_refs": {"resolution": {"todo": True}},
        }
    }
    sd = _write(
        tmp_path / "spatial-demo",
        _v2_doc(
            provenance={"origin": "corpus", "source_ref": "10.1038/xyz"},
            interface={"parameters": {"hints": hints}},
        ),
        corpus_text=_CORPUS_TEXT,
    )
    errs = skill_lint.lint_skill(sd)
    assert any("no source_ref" in e for e in errs)


def test_fail_malformed_source_ref_triple(tmp_path):
    hints = {
        "default": {
            "params": ["resolution"],
            "defaults": {"resolution": 0.8},
            "source_refs": {"resolution": {"quote": "resolution=0.8"}},  # missing char_span/doc_ref
        }
    }
    sd = _write(
        tmp_path / "spatial-demo",
        _v2_doc(
            provenance={"origin": "corpus", "source_ref": "10.1038/xyz"},
            interface={"parameters": {"hints": hints}},
        ),
        corpus_text=_CORPUS_TEXT,
    )
    errs = skill_lint.lint_skill(sd)
    assert any("malformed" in e for e in errs)


def test_fail_span_does_not_slice_out_quote(tmp_path):
    hints = {
        "default": {
            "params": ["resolution"],
            "defaults": {"resolution": 0.8},
            "source_refs": {
                "resolution": {"quote": "resolution=0.8", "char_span": [0, 5], "doc_ref": "10.1038/xyz"},
            },
        }
    }
    sd = _write(
        tmp_path / "spatial-demo",
        _v2_doc(
            provenance={"origin": "corpus", "source_ref": "10.1038/xyz"},
            interface={"parameters": {"hints": hints}},
        ),
        corpus_text=_CORPUS_TEXT,  # span [0,5] slices "resol", not "resolution=0.8"
    )
    errs = skill_lint.lint_skill(sd)
    assert any("does not slice out its own quote" in e for e in errs)


def test_fail_doc_level_source_ref_unset(tmp_path):
    sd = _write(
        tmp_path / "spatial-demo",
        _v2_doc(
            provenance={"origin": "corpus"},  # source_ref left unset
            interface={"parameters": {"hints": {}}},
        ),
    )
    errs = skill_lint.lint_skill(sd)
    assert any("source_ref (DOI/URL/PMID) is not set" in e for e in errs)


def test_non_corpus_skill_with_arbitrary_hints_is_never_flagged(tmp_path):
    # Regression guard: a plain human/scaffolded/promoted skill's hints shape
    # (unsourced defaults are normal there) must never trip this rule.
    hints = {
        "default": {
            "params": ["resolution"],
            "defaults": {"resolution": 0.8},
            # no source_refs at all — legal for a non-corpus skill
        }
    }
    sd = _write(
        tmp_path / "spatial-demo",
        _v2_doc(interface={"parameters": {"hints": hints}}),  # default origin: "human"
    )
    errs = skill_lint.lint_skill(sd)
    assert not any("source_ref" in e for e in errs)


def test_missing_source_corpus_txt_is_rejected(tmp_path):
    # create_skill_scaffold(from_corpus=...) always writes this file, so a
    # corpus-origin skill without it can only mean the persisted evidence was
    # deleted after the fact — exactly the anti-fabrication bypass the iron
    # rule must catch, not tolerate. (Unlike _check_allowed_extra_flags's
    # file-exists tolerance, there's no legitimate "predates the rule" case
    # here.)
    sd = _write(
        tmp_path / "spatial-demo",
        _v2_doc(
            provenance={"origin": "corpus", "source_ref": "10.1038/xyz"},
            interface={"parameters": {"hints": _GOOD_HINTS}},
        ),
        corpus_text=None,  # no references/source_corpus.txt written
    )
    errs = skill_lint.lint_skill(sd)
    assert any("source_corpus.txt is missing" in e for e in errs)


# ---- adversarial regressions (found by codex cross-validation) ----


def test_fail_negative_and_out_of_bounds_span_is_rejected_not_clamped(tmp_path):
    # Python slicing SILENTLY CLAMPS negative/out-of-range indices instead of
    # raising — a naive `span[0] < span[1]` check alone let a bogus span like
    # [-14, 999] sail through as "verified" against a short corpus text.
    hints = {
        "default": {
            "params": ["resolution"],
            "defaults": {"resolution": 0.8},
            "source_refs": {
                "resolution": {"quote": "resolution=0.8", "char_span": [-14, 999], "doc_ref": "10.1038/xyz"},
            },
        }
    }
    sd = _write(
        tmp_path / "spatial-demo",
        _v2_doc(
            provenance={"origin": "corpus", "source_ref": "10.1038/xyz"},
            interface={"parameters": {"hints": hints}},
        ),
        corpus_text=_CORPUS_TEXT,
    )
    errs = skill_lint.lint_skill(sd)
    assert any("malformed" in e for e in errs)


def test_fail_span_beyond_corpus_text_length_is_rejected(tmp_path):
    hints = {
        "default": {
            "params": ["resolution"],
            "defaults": {"resolution": 0.8},
            "source_refs": {
                "resolution": {"quote": "resolution=0.8", "char_span": [0, 9999], "doc_ref": "10.1038/xyz"},
            },
        }
    }
    sd = _write(
        tmp_path / "spatial-demo",
        _v2_doc(
            provenance={"origin": "corpus", "source_ref": "10.1038/xyz"},
            interface={"parameters": {"hints": hints}},
        ),
        corpus_text=_CORPUS_TEXT,
    )
    errs = skill_lint.lint_skill(sd)
    assert any("does not slice out its own quote" in e for e in errs)


def test_source_refs_as_a_list_does_not_crash_lint(tmp_path):
    # A malformed skill.yaml must produce a lint ERROR STRING, never an
    # uncaught exception that crashes the whole lint run.
    hints = {
        "default": {
            "params": ["resolution"],
            "defaults": {"resolution": 0.8},
            "source_refs": ["not", "a", "dict"],
        }
    }
    sd = _write(
        tmp_path / "spatial-demo",
        _v2_doc(
            provenance={"origin": "corpus", "source_ref": "10.1038/xyz"},
            interface={"parameters": {"hints": hints}},
        ),
        corpus_text=_CORPUS_TEXT,
    )
    errs = skill_lint.lint_skill(sd)  # must not raise
    assert any("no source_ref" in e for e in errs)


def test_float_char_span_elements_do_not_crash_lint(tmp_path):
    # Slicing with float indices raises TypeError — must be caught as a
    # structural "malformed" lint error instead, never propagate.
    hints = {
        "default": {
            "params": ["resolution"],
            "defaults": {"resolution": 0.8},
            "source_refs": {
                "resolution": {"quote": "resolution=0.8", "char_span": [0.0, 14.0], "doc_ref": "10.1038/xyz"},
            },
        }
    }
    sd = _write(
        tmp_path / "spatial-demo",
        _v2_doc(
            provenance={"origin": "corpus", "source_ref": "10.1038/xyz"},
            interface={"parameters": {"hints": hints}},
        ),
        corpus_text=_CORPUS_TEXT,
    )
    errs = skill_lint.lint_skill(sd)  # must not raise
    assert any("malformed" in e for e in errs)
