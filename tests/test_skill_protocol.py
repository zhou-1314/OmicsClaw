"""Tests that all skill scripts conform to the SkillProtocol conventions.

Uses AST-based validation (no heavy imports needed) to check that
every skill defines SKILL_NAME, SKILL_VERSION, and main().
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from omicsclaw.skill.registry import OmicsRegistry, SKILLS_DIR  # noqa: E402
from omicsclaw.skill.protocol import validate_skill_module  # noqa: E402


def _find_all_skill_scripts() -> list[Path]:
    """Discover Python Skill entries through the canonical Registry."""
    reg = OmicsRegistry()
    reg.load_all(SKILLS_DIR)
    return sorted(
        Path(info["script"])
        for _alias, info in reg.iter_primary_skills()
        if Path(info["script"]).suffix == ".py"
    )


_SKILL_SCRIPTS = _find_all_skill_scripts()


def test_skill_scripts_found():
    """Sanity check: we should find a reasonable number of skill scripts."""
    assert len(_SKILL_SCRIPTS) >= 20, f"Only found {len(_SKILL_SCRIPTS)} scripts"


def test_all_skills_have_skill_name():
    """Every skill script must define SKILL_NAME."""
    missing = []
    for script in _SKILL_SCRIPTS:
        result = validate_skill_module(script)
        if any("Missing SKILL_NAME" in e for e in result.errors):
            missing.append(script.stem)
    assert missing == [], f"Skills missing SKILL_NAME: {missing}"


def test_all_skills_have_skill_version():
    """Every skill script must define SKILL_VERSION."""
    missing = []
    for script in _SKILL_SCRIPTS:
        result = validate_skill_module(script)
        if any("Missing SKILL_VERSION" in e for e in result.errors):
            missing.append(script.stem)
    assert missing == [], f"Skills missing SKILL_VERSION: {missing}"


def test_all_skills_have_main():
    """Every skill script must define main()."""
    missing = []
    for script in _SKILL_SCRIPTS:
        result = validate_skill_module(script)
        if any("Missing main()" in e for e in result.errors):
            missing.append(script.stem)
    assert missing == [], f"Skills missing main(): {missing}"


def test_all_skills_pass_validation():
    """Every skill script must pass all required checks (no errors)."""
    failures = []
    for script in _SKILL_SCRIPTS:
        result = validate_skill_module(script)
        if not result.passed:
            failures.append(f"{script.stem}: {result.errors}")
    assert failures == [], "Skills failing validation:\n" + "\n".join(
        str(failure) for failure in failures
    )
