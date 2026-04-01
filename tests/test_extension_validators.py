import json

from omicsclaw.extensions import (
    EXTENSION_MANIFEST_FILENAME,
    discover_extension_manifest,
    load_extension_manifest,
    validate_extension_directory,
    validate_skill_pack_directory,
)


def test_discover_and_load_extension_manifest(tmp_path):
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    manifest_path = skill_dir / EXTENSION_MANIFEST_FILENAME
    manifest_path.write_text(
        json.dumps(
            {
                "name": "my-skill",
                "version": "1.0.0",
                "type": "skill-pack",
                "entrypoints": ["run.py"],
                "required_files": ["SKILL.md"],
                "trusted_capabilities": ["skill-run"],
            }
        ),
        encoding="utf-8",
    )

    discovered = discover_extension_manifest(skill_dir)
    manifest = load_extension_manifest(manifest_path)

    assert discovered == manifest_path
    assert manifest.name == "my-skill"
    assert manifest.version == "1.0.0"
    assert manifest.entrypoints == ["run.py"]
    assert manifest.required_files == ["SKILL.md"]
    assert manifest.trusted_capabilities == ["skill-run"]


def test_validate_skill_pack_directory_accepts_valid_manifest_and_skill_files(tmp_path):
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "run.py").write_text("print('ok')\n", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: test skill\nversion: 1.0.0\n---\n",
        encoding="utf-8",
    )
    (skill_dir / EXTENSION_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "name": "my-skill",
                "version": "1.0.0",
                "type": "skill-pack",
                "entrypoints": ["run.py"],
                "required_files": ["SKILL.md"],
            }
        ),
        encoding="utf-8",
    )

    report = validate_skill_pack_directory(skill_dir)

    assert report.valid is True
    assert report.manifest is not None
    assert report.extension_type == "skill-pack"
    assert report.errors == []
    assert report.warnings == []


def test_validate_skill_pack_directory_reports_manifest_contract_errors(tmp_path):
    skill_dir = tmp_path / "broken-skill"
    skill_dir.mkdir()
    (skill_dir / "run.py").write_text("print('ok')\n", encoding="utf-8")
    (skill_dir / EXTENSION_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "name": "broken-skill",
                "version": "1.0.0",
                "type": "skill-pack",
                "entrypoints": ["missing.py"],
                "required_files": ["SKILL.md"],
            }
        ),
        encoding="utf-8",
    )

    report = validate_skill_pack_directory(skill_dir)

    assert report.valid is False
    assert "Extension manifest requires missing file: SKILL.md" in report.errors
    assert "Extension manifest entrypoint not found: missing.py" in report.errors


def test_validate_extension_directory_rejects_untrusted_privileged_capabilities(tmp_path):
    extension_dir = tmp_path / "remote-skill"
    extension_dir.mkdir()
    (extension_dir / "run.py").write_text("print('ok')\n", encoding="utf-8")
    (extension_dir / EXTENSION_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "name": "remote-skill",
                "version": "1.0.0",
                "type": "skill-pack",
                "entrypoints": ["run.py"],
                "trusted_capabilities": ["skill-run", "hooks"],
            }
        ),
        encoding="utf-8",
    )

    report = validate_extension_directory(extension_dir, source_kind="github")

    assert report.valid is False
    assert report.restricted_capabilities == ["hooks"]
    assert any("privileged capabilities" in error for error in report.errors)


def test_validate_extension_directory_rejects_untrusted_non_skill_pack(tmp_path):
    extension_dir = tmp_path / "prompt-pack"
    extension_dir.mkdir()
    (extension_dir / "rules.md").write_text("# rules\n", encoding="utf-8")
    (extension_dir / EXTENSION_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "name": "prompt-pack",
                "version": "1.0.0",
                "type": "prompt-pack",
                "entrypoints": ["rules.md"],
            }
        ),
        encoding="utf-8",
    )

    report = validate_extension_directory(extension_dir, source_kind="github")

    assert report.valid is False
    assert "Untrusted extension sources may only install 'skill-pack' extensions." in report.errors


def test_validate_extension_directory_accepts_local_prompt_pack(tmp_path):
    extension_dir = tmp_path / "prompt-pack"
    extension_dir.mkdir()
    (extension_dir / "rules.md").write_text("# rules\n", encoding="utf-8")
    (extension_dir / EXTENSION_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "name": "prompt-pack",
                "version": "1.0.0",
                "type": "prompt-pack",
                "entrypoints": ["rules.md"],
            }
        ),
        encoding="utf-8",
    )

    report = validate_extension_directory(extension_dir, source_kind="local")

    assert report.valid is True
    assert report.extension_type == "prompt-pack"
    assert report.entrypoint_paths[0].name == "rules.md"
