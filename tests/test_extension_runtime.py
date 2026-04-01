import json

from omicsclaw.extensions import (
    ExtensionManifest,
    extension_store_dir,
    load_enabled_prompt_packs,
    load_prompt_pack_runtime_context,
    write_extension_state,
    write_install_record,
)


def _write_prompt_pack(
    tmp_path,
    name: str,
    *,
    source_kind: str = "local",
    enabled: bool = True,
    trusted_capabilities: list[str] | None = None,
    rules_text: str = "Use concise lab language.\n",
):
    pack_dir = extension_store_dir(tmp_path, "prompt-pack") / name
    pack_dir.mkdir(parents=True)
    (pack_dir / "rules.md").write_text(rules_text, encoding="utf-8")
    manifest = ExtensionManifest(
        name=name,
        version="1.0.0",
        type="prompt-pack",
        entrypoints=["rules.md"],
        trusted_capabilities=list(trusted_capabilities or []),
    )
    (pack_dir / "omicsclaw-extension.json").write_text(
        json.dumps(
            {
                "name": manifest.name,
                "version": manifest.version,
                "type": manifest.type,
                "entrypoints": manifest.entrypoints,
                "trusted_capabilities": manifest.trusted_capabilities,
            }
        ),
        encoding="utf-8",
    )
    write_install_record(
        pack_dir,
        extension_name=name,
        source_kind=source_kind,
        source=f"/tmp/{name}",
        manifest=manifest,
        extension_type="prompt-pack",
        relative_install_path=f"installed_extensions/prompt-packs/{name}",
    )
    write_extension_state(
        pack_dir,
        enabled=enabled,
        disabled_reason="" if enabled else "disabled in test",
    )
    return pack_dir


def test_load_enabled_prompt_packs_applies_tracking_enablement_and_trust_filters(tmp_path):
    _write_prompt_pack(tmp_path, "active-rules", trusted_capabilities=["prompt-rules"])
    _write_prompt_pack(tmp_path, "disabled-rules", enabled=False)
    _write_prompt_pack(tmp_path, "remote-rules", source_kind="github")
    _write_prompt_pack(tmp_path, "wrong-capability", trusted_capabilities=["skill-run"])

    loaded = load_enabled_prompt_packs(tmp_path)

    assert [pack.name for pack in loaded] == ["active-rules"]
    assert loaded[0].rules[0].relative_path == "rules.md"
    assert "Use concise lab language." in loaded[0].rules[0].content


def test_load_prompt_pack_runtime_context_builds_budgeted_context_block(tmp_path):
    _write_prompt_pack(
        tmp_path,
        "analysis-style",
        rules_text="Prioritize exact file paths.\nAvoid redundant restatements.\n",
    )

    runtime_context = load_prompt_pack_runtime_context(tmp_path, max_total_chars=1200)

    assert runtime_context.active_prompt_packs == ("analysis-style",)
    assert runtime_context.omitted_prompt_packs == ()
    assert "## Active Local Prompt Packs" in runtime_context.content
    assert "analysis-style v1.0.0" in runtime_context.content
    assert "Avoid redundant restatements." in runtime_context.content
