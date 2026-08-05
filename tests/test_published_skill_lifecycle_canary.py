"""Small real-run canary for the published run-derived lifecycle slice."""

from pathlib import Path

from omicsclaw.skill.execution.reproducibility import (
    load_skill_replay_capsule,
    verify_skill_replay_capsules,
)
from omicsclaw.skill.registry import SKILLS_DIR
from omicsclaw.skill.runner import run_skill
from omicsclaw.skill.schema import load_skill_yaml


def test_published_run_derived_skill_repeats_with_semantic_replay(
    tmp_path: Path,
) -> None:
    """Exercise the real shared runner twice with no evaluation seam or notebook."""

    skill_id = "bulkrna-cosinor-rhythm"
    skill_dir = SKILLS_DIR / "bulkrna" / "run-derived" / skill_id
    manifest = load_skill_yaml(skill_dir / "skill.yaml")
    assert manifest.provenance.origin == "promoted"
    assert manifest.provenance.source_ref.startswith("run:")
    assert manifest.lifecycle.status == "mvp"
    assert manifest.validation.level == "demo-validated"
    assert manifest.validation.protocols[0].runner == "shared_runner"
    assert manifest.validation.protocols[0].repeats == 2

    first = run_skill(skill_id, demo=True, output_dir=str(tmp_path / "first"))
    second = run_skill(skill_id, demo=True, output_dir=str(tmp_path / "second"))

    assert first.success is True, first.stderr
    assert second.success is True, second.stderr
    assert first.replay_path
    assert second.replay_path
    expected = load_skill_replay_capsule(first.replay_path)
    observed = load_skill_replay_capsule(second.replay_path)
    assert verify_skill_replay_capsules(expected, observed) == ()
    assert expected["verification"]["result_semantic_sha256"] == (
        observed["verification"]["result_semantic_sha256"]
    )
    assert not tuple(tmp_path.rglob("*.ipynb"))
