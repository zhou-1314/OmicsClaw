"""Regression tests for scripts/generate_catalog.py.

ADR 2026-05-11 (#1) drift-check rollout uncovered a worktree bug:
the hidden-directory filter used `skill_dir.parts` (absolute path
components), so a checkout at `~/.worktrees/<branch>/skills/...`
matched the `.startswith(".")` rule on the `.worktrees` ancestor and
the generator silently emitted a 0-skill catalog.  Fix: filter the
path RELATIVE to SKILLS_DIR.

This test pins the fix in place — running the generator against a
SKILLS_DIR whose ABSOLUTE path contains a `.hidden` ancestor must still
discover skills under it.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import generate_catalog  # noqa: E402


_FAKE_SKILL_MD = """\
---
name: fake-skill
description: Load when running the fake regression skill. Skip when never.
version: 0.1.0
---

# fake-skill

## When to use

Synthetic test fixture for catalog generator regression.
"""


def test_generator_discovers_skills_under_dotted_ancestor(tmp_path, monkeypatch):
    """SKILLS_DIR whose absolute path contains a `.hidden` ancestor MUST
    not filter out skills below it.  Reproduces the worktree-incompat bug
    fixed in scripts/generate_catalog.py."""
    # Set up: tmp_path/.worktrees-like-ancestor/skills/<domain>/<skill>/SKILL.md
    hidden_ancestor = tmp_path / ".worktrees" / "branch-x"
    skills_dir = hidden_ancestor / "skills"
    skill_dir = skills_dir / "fake-domain" / "fake-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_FAKE_SKILL_MD, encoding="utf-8")
    # The skill needs a Python file so has_script is True.
    (skill_dir / "fake_skill.py").write_text("# stub\n", encoding="utf-8")

    # Monkeypatch SKILLS_DIR and stub the alias map (which would otherwise
    # try to load the real registry, irrelevant for this filter test).
    monkeypatch.setattr(generate_catalog, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(generate_catalog, "build_cli_alias_map", lambda: {})

    catalog = generate_catalog.generate_catalog()
    assert catalog["skill_count"] == 1, (
        f"hidden-dir filter regression — catalog should have 1 skill under "
        f"{skills_dir} (which has '.worktrees' as an absolute-path ancestor), "
        f"got {catalog['skill_count']}: {[s.get('name') for s in catalog['skills']]}"
    )
    assert catalog["skills"][0]["name"] == "fake-skill"
    assert catalog["compatibility_graph"]["node_count"] == 1


def _build_catalog_skill(skills_dir, name, sidecar_yaml=None):
    """Fabricate one skill dir (SKILL.md + script stub + optional sidecar)."""
    skill_dir = skills_dir / "fake-domain" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        _FAKE_SKILL_MD.replace("fake-skill", name), encoding="utf-8"
    )
    (skill_dir / f"{name.replace('-', '_')}.py").write_text("# stub\n", encoding="utf-8")
    if sidecar_yaml is not None:
        (skill_dir / "parameters.yaml").write_text(sidecar_yaml, encoding="utf-8")
    return skill_dir


def _gen_one(tmp_path, monkeypatch, name, sidecar_yaml=None):
    skills_dir = tmp_path / "skills"
    _build_catalog_skill(skills_dir, name, sidecar_yaml)
    monkeypatch.setattr(generate_catalog, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(generate_catalog, "build_cli_alias_map", lambda: {})
    return generate_catalog.generate_catalog()["skills"][0]


def test_catalog_emits_validation_level_default_and_preserves_status(tmp_path, monkeypatch):
    """ADR 0030 §3: validation_level defaults to smoke-only and is emitted
    ALONGSIDE status (availability) — status must not be replaced."""
    entry = _gen_one(tmp_path, monkeypatch, "no-sidecar")
    assert entry["validation_level"] == "smoke-only"
    assert entry["status"] == "mvp"  # availability semantics preserved
    assert entry["superseded_by"] is None


def test_catalog_emits_explicit_validation_level(tmp_path, monkeypatch):
    entry = _gen_one(
        tmp_path, monkeypatch, "graded",
        "domain: d\nvalidation_level: benchmarked\n",
    )
    assert entry["validation_level"] == "benchmarked"
    assert entry["status"] == "mvp"  # still emitted, unchanged


def test_catalog_clamps_unknown_validation_level(tmp_path, monkeypatch):
    entry = _gen_one(
        tmp_path, monkeypatch, "bad",
        "domain: d\nvalidation_level: bogus-level\n",
    )
    assert entry["validation_level"] == "smoke-only"


def test_catalog_exposes_explicit_security_without_claiming_enforcement(
    tmp_path,
    monkeypatch,
):
    from omicsclaw.skill.schema import parse_skill_manifest

    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "spatial" / "secure-skill"
    skill_dir.mkdir(parents=True)
    manifest = parse_skill_manifest(
        {
            "schema_version": 2,
            "id": "secure-skill",
            "name": "secure-skill",
            "domain": "spatial",
            "version": "1.0.0",
            "summary": {"load_when": "testing explicit security metadata"},
            "runtime": {"entry": "secure_skill.py"},
            "security": {
                "data_egress": "optional",
                "network": "optional",
                "writes": "output_dir_only",
            },
        }
    )
    (skill_dir / "skill.yaml").write_text(manifest.to_yaml(), encoding="utf-8")
    (skill_dir / "secure_skill.py").write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(generate_catalog, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(generate_catalog, "build_cli_alias_map", lambda: {})

    entry = generate_catalog.generate_catalog()["skills"][0]

    assert entry["security"] == {
        "reviewed": True,
        "enforcement": "declarative",
        "data_egress": "optional",
        "network": "optional",
        "writes": "output_dir_only",
    }


def test_parse_yaml_frontmatter_handles_indented_list_syntax():
    """YAML list-syntax (`key:\\n- item`) is the standard form used in every
    SKILL.md `tags:` block.  The custom parser must produce a list, not the
    empty string.  Reproducing CodeRabbit's finding on PR #170 — without
    this, 89/89 catalog entries had `tags: ""` and every downstream
    consumer that expected a list (e.g. `tags[0] if tags`)
    received a string-of-chars or empty."""
    frontmatter = (
        "---\n"
        "name: fake-skill\n"
        "description: Load when X. Skip when Y (use sibling).\n"
        "tags:\n"
        "- spatial\n"
        "- velocity\n"
        "- harmony\n"
        "---\n"
        "\n"
        "# fake-skill\n"
    )
    parsed = generate_catalog.parse_yaml_frontmatter(frontmatter)
    assert parsed.get("tags") == ["spatial", "velocity", "harmony"], (
        f"YAML list syntax must parse to a list, got {parsed.get('tags')!r}"
    )


def test_generator_still_filters_hidden_dirs_relative_to_skills_dir(tmp_path, monkeypatch):
    """The filter must still exclude `.hidden` / `__dunder__` directories
    when they appear INSIDE the skills tree (e.g. `__pycache__`)."""
    skills_dir = tmp_path / "skills"
    # Real skill
    real_dir = skills_dir / "real-domain" / "real-skill"
    real_dir.mkdir(parents=True)
    (real_dir / "SKILL.md").write_text(_FAKE_SKILL_MD, encoding="utf-8")
    (real_dir / "real_skill.py").write_text("# stub\n", encoding="utf-8")
    # Hidden directory inside skills tree — must be excluded
    hidden_dir = skills_dir / "__pycache__" / "cached-skill"
    hidden_dir.mkdir(parents=True)
    (hidden_dir / "SKILL.md").write_text(_FAKE_SKILL_MD, encoding="utf-8")
    (hidden_dir / "cached_skill.py").write_text("# stub\n", encoding="utf-8")

    monkeypatch.setattr(generate_catalog, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(generate_catalog, "build_cli_alias_map", lambda: {})

    catalog = generate_catalog.generate_catalog()
    assert catalog["skill_count"] == 1, (
        "hidden dirs INSIDE the skills tree must still be filtered"
    )
    assert catalog["skills"][0]["name"] == "fake-skill"


def test_committed_catalog_is_fresh():
    """The committed skills/catalog.json must match a freshly generated one.

    Guards against catalog drift: when a skill is added without re-running
    `scripts/generate_catalog.py --apply`, the committed catalog goes stale
    while the runtime registry discovers the new skill, and the Skill Catalog
    health check flags `Registry/catalog drift detected`. This mirrors the
    script's own `--check` mode (string-equal on the same indent=2 dump).

    If this fails, run: python scripts/generate_catalog.py --apply
    """
    import json

    catalog_path = generate_catalog.SKILLS_DIR / "catalog.json"
    assert catalog_path.exists(), f"missing {catalog_path}"
    expected = json.dumps(generate_catalog.generate_catalog(), indent=2)
    current = catalog_path.read_text(encoding="utf-8")
    assert current.rstrip() == expected.rstrip(), (
        "skills/catalog.json is out of date — run "
        "`python scripts/generate_catalog.py --apply`."
    )
