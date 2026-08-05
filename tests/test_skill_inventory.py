from __future__ import annotations

from pathlib import Path
import json

import pytest

from omicsclaw.skill.inventory import (
    RUN_DERIVED_COLLECTION,
    discover_skill_inventory,
)


def _write_v2_skill(skill_dir: Path, *, skill_id: str, domain: str) -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    entry = f"{skill_id.replace('-', '_')}.py"
    (skill_dir / entry).write_text("if __name__ == '__main__':\n    pass\n")
    (skill_dir / "skill.yaml").write_text(
        "\n".join(
            [
                "schema_version: 2",
                f"id: {skill_id}",
                f"name: {skill_id}",
                f"domain: {domain}",
                "version: 1.0.0",
                "summary:",
                "  load_when: exercising Skill inventory discovery",
                "runtime:",
                f"  entry: {entry}",
                "resources:",
                "  compute:",
                "    cpu_cores: 1",
                "    memory_mib: 64",
                "    gpu_devices: 0",
                "    threads: 1",
                "    temporary_disk_mib: 16",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_inventory_discovers_domain_first_run_derived_skill(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    skill_dir = (
        skills_root
        / "bulkrna"
        / "run-derived"
        / "bulkrna-cosinor-rhythm"
    )
    _write_v2_skill(
        skill_dir,
        skill_id="bulkrna-cosinor-rhythm",
        domain="bulkrna",
    )

    inventory = discover_skill_inventory(skills_root)

    assert len(inventory.locations) == 1
    location = inventory.locations[0]
    assert location.skill_dir == skill_dir.resolve()
    assert location.domain == "bulkrna"
    assert location.collection == RUN_DERIVED_COLLECTION
    assert location.relative_path.as_posix() == (
        "bulkrna/run-derived/bulkrna-cosinor-rhythm"
    )


def test_inventory_rejects_run_derived_manifest_domain_mismatch(
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / "skills"
    _write_v2_skill(
        skills_root / "bulkrna" / "run-derived" / "wrong-domain",
        skill_id="wrong-domain",
        domain="genomics",
    )

    with pytest.raises(ValueError, match="path domain 'bulkrna'.*manifest domain 'genomics'"):
        discover_skill_inventory(skills_root)


def test_inventory_rejects_duplicate_id_across_collections(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _write_v2_skill(
        skills_root / "bulkrna" / "shared-skill",
        skill_id="shared-skill",
        domain="bulkrna",
    )
    _write_v2_skill(
        skills_root / "bulkrna" / "run-derived" / "second-copy",
        skill_id="shared-skill",
        domain="bulkrna",
    )

    with pytest.raises(ValueError, match="duplicate Skill identity 'shared-skill'"):
        discover_skill_inventory(skills_root)


def test_inventory_preserves_legacy_script_only_skill(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "genomics" / "legacy-tool"
    skill_dir.mkdir(parents=True)
    (skill_dir / "legacy_tool.py").write_text(
        "if __name__ == '__main__':\n    pass\n",
        encoding="utf-8",
    )

    inventory = discover_skill_inventory(skills_root)

    assert [(item.canonical_id, item.domain, item.collection) for item in inventory.locations] == [
        ("legacy-tool", "genomics", "curated")
    ]


def test_inventory_reports_disabled_user_installed_skill(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "user" / "local-pack"
    _write_v2_skill(skill_dir, skill_id="local-pack", domain="bulkrna")
    (skill_dir / ".omicsclaw-extension-state.json").write_text(
        json.dumps({"enabled": False, "disabled_reason": "operator disabled"}),
        encoding="utf-8",
    )

    inventory = discover_skill_inventory(skills_root)

    assert len(inventory.locations) == 1
    assert inventory.locations[0].enabled is False
    assert inventory.locations[0].collection == "user-installed"
    assert inventory.locations[0].domain == "bulkrna"


def test_inventory_rejects_visible_symlinked_collection_entry(
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / "skills"
    outside = tmp_path / "outside-skill"
    _write_v2_skill(outside, skill_id="linked-skill", domain="bulkrna")
    link = skills_root / "bulkrna" / "linked-skill"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link"):
        discover_skill_inventory(skills_root)
