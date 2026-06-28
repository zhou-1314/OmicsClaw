"""`oc run literature` must not mangle DOI/URL/text inputs into file paths (audit B).

`_prepare_skill_run` used to unconditionally `Path(input).resolve()`, turning a
documented `--input "10.1038/..."` / `--input "https://..."` into a bogus
`<cwd>/10.1038/...` path so the literature skill mis-detected the input type and
fell back to 'text' (no GEO). The fix resolves only inputs that are real local
files/dirs; free-form inputs pass through verbatim.
"""

from __future__ import annotations

from pathlib import Path

from omicsclaw.skill.runner import _PreparedSkillRun, _prepare_skill_run


def _prep(input_path: str, output_dir: Path) -> _PreparedSkillRun | object:
    return _prepare_skill_run(
        "literature",
        input_path=input_path,
        input_paths=None,
        output_dir=str(output_dir),
        demo=False,
        session_path=None,
        extra_args=None,
        log_banner=False,
    )


def test_literature_doi_input_is_not_mangled(tmp_path):
    prep = _prep("10.1038/s41586-021-03689-7", tmp_path / "out_doi")
    assert isinstance(prep, _PreparedSkillRun), getattr(prep, "stderr", prep)
    assert prep.resolved_input == "10.1038/s41586-021-03689-7"
    assert "10.1038/s41586-021-03689-7" in prep.cmd  # forwarded raw to --input


def test_literature_url_input_is_not_mangled(tmp_path):
    url = "https://www.nature.com/articles/s41586-021-03689-7"
    prep = _prep(url, tmp_path / "out_url")
    assert isinstance(prep, _PreparedSkillRun), getattr(prep, "stderr", prep)
    assert prep.resolved_input == url
    assert url in prep.cmd


def test_literature_existing_file_input_is_still_resolved(tmp_path):
    f = tmp_path / "paper.pdf"
    f.write_text("x", encoding="utf-8")
    prep = _prep(str(f), tmp_path / "out_file")
    assert isinstance(prep, _PreparedSkillRun), getattr(prep, "stderr", prep)
    # A real local file is still made absolute (the subprocess runs from a
    # different cwd), so file inputs keep working.
    assert prep.resolved_input == str(f.resolve())
