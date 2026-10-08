"""The manifest: what it accepts, what it refuses, and the run order."""

from __future__ import annotations

from pathlib import Path

import pytest

from omicsclaw.bench.manifest import ManifestError, load_manifest

BASE = """
name = "suite"
seed = 11
repeats = 2

[budget]
wall_clock_s = 60

[[models]]
id = "m1"
provider = "p"
model = "model-one"

[[models]]
id = "m2"
provider = "p"
model = "model-two"

[[arms]]
id = "plain"
adapter = "omicsclaw"

[[arms]]
id = "tuned"
adapter = "omicsclaw"
skills_dir = "roots/tuned"
permission_rules = "rules.json"
env = { OMICSCLAW_SKILL_ENV = "off" }

[[cases]]
id = "c1"
prompt = "Do the first thing."
deliverables = ["output/a.json"]
grader = "pkg.mod:Grader"

[[cases]]
id = "c2"
prompt = "Do the second thing."
deliverables = ["output/b.json"]
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "suite" / "manifest.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_a_manifest_is_read_with_paths_made_absolute(tmp_path):
    manifest = load_manifest(write(tmp_path, BASE))

    assert manifest.name == "suite"
    assert manifest.repeats == 2
    assert manifest.budget.wall_clock_s == 60
    assert manifest.budget.kill_grace_s == 20
    assert manifest.budget.max_turns is None
    tuned = manifest.arms[1]
    assert tuned.skills_dir == tmp_path / "suite" / "roots" / "tuned"
    assert tuned.permission_rules == tmp_path / "suite" / "rules.json"
    assert tuned.env == {"OMICSCLAW_SKILL_ENV": "off"}
    assert manifest.cases[0].grader == "pkg.mod:Grader"
    assert manifest.cases[1].grader == ""


def test_every_arm_model_case_and_repeat_is_run_once(tmp_path):
    manifest = load_manifest(write(tmp_path, BASE))

    keys = [run.key for run in manifest.runs()]

    assert len(keys) == len(set(keys)) == 2 * 2 * 2 * 2
    assert "tuned/m2/c1/r2" in keys


def test_the_order_is_fixed_by_the_seed(tmp_path):
    first = [run.key for run in load_manifest(write(tmp_path, BASE)).runs()]
    again = [run.key for run in load_manifest(write(tmp_path, BASE)).runs()]
    reseeded = BASE.replace("seed = 11", "seed = 12")
    other = [run.key for run in load_manifest(write(tmp_path, reseeded)).runs()]

    assert first == again
    assert first != other
    assert sorted(first) == sorted(other)


def test_the_arms_of_one_case_and_repeat_run_next_to_each_other(tmp_path):
    """Each block of four consecutive runs is one case and one repeat with
    every arm and model in it, so no arm's runs of a case are bunched at
    one end of the campaign. Across blocks the arm that goes first varies.
    """
    runs = load_manifest(write(tmp_path, BASE)).runs()

    blocks = [runs[index : index + 4] for index in range(0, len(runs), 4)]
    for block in blocks:
        assert len({(run.case.id, run.repeat) for run in block}) == 1
        assert {(run.arm.id, run.model.id) for run in block} == {
            ("plain", "m1"), ("plain", "m2"), ("tuned", "m1"), ("tuned", "m2"),
        }
    assert len({block[0].arm.id for block in blocks}) == 2


def test_an_arm_with_its_own_model_runs_only_that_model(tmp_path):
    text = BASE + """
[[arms]]
id = "fixed"
adapter = "omicsclaw"
provider = "other"
model = "vendor/model three"
"""
    manifest = load_manifest(write(tmp_path, text))

    fixed = [run for run in manifest.runs() if run.arm.id == "fixed"]

    assert {run.model.id for run in fixed} == {"vendor-model-three"}
    assert {run.model.provider for run in fixed} == {"other"}
    assert len(fixed) == 2 * 2


def test_a_prompt_can_come_from_a_file_beside_the_manifest(tmp_path):
    path = write(tmp_path, BASE.replace(
        'prompt = "Do the first thing."', 'prompt_file = "prompts/first.md"'
    ))
    (path.parent / "prompts").mkdir()
    (path.parent / "prompts" / "first.md").write_text("From a file.\n")

    assert load_manifest(path).cases[0].prompt == "From a file.\n"


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('name = "suite"', 'name = "suite"\ncolour = "red"', "unknown key"),
        ('id = "c2"', 'id = "c1"', "duplicate case id"),
        ('id = "c2"', 'id = "../c2"', "not an identifier"),
        ('"output/b.json"', '"../outside.json"', "inside the workspace"),
        ('"output/b.json"', '"/abs/b.json"', "inside the workspace"),
        ("wall_clock_s = 60", "wall_clock_s = 0", "positive number"),
        ("wall_clock_s = 60", "kill_grace_s = 5", "wall_clock_s is required"),
        ("repeats = 2", "repeats = 0", "at least 1"),
        ('prompt = "Do the second thing."', "", "exactly one of prompt"),
        ('prompt = "Do the second thing."', 'prompt = "  "', "prompt is empty"),
        ('adapter = "omicsclaw"\nskills_dir', "skills_dir", "adapter is required"),
        ('{ OMICSCLAW_SKILL_ENV = "off" }', "{ N = 1 }", "must be a string"),
        ("[budget]", '[audit]\npatterns = ["("]\n[budget]', "regular expression"),
    ],
)
def test_a_manifest_that_cannot_be_read_as_written_is_refused(
    tmp_path, old, new, message
):
    assert old in BASE
    with pytest.raises(ManifestError, match=message):
        load_manifest(write(tmp_path, BASE.replace(old, new)))


def test_an_arm_without_any_model_is_refused(tmp_path):
    start, end = BASE.index("[[models]]"), BASE.index("[[arms]]")
    with pytest.raises(ManifestError, match="has no model"):
        load_manifest(write(tmp_path, BASE[:start] + BASE[end:]))


def test_a_missing_manifest_is_a_manifest_error(tmp_path):
    with pytest.raises(ManifestError, match="cannot read"):
        load_manifest(tmp_path / "absent.toml")
