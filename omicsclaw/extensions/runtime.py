"""Runtime activation helpers for installable OmicsClaw extensions."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .loader import list_installed_extensions
from .manifest import discover_extension_manifest, load_extension_manifest

PROMPT_PACK_EXTENSION_TYPE = "prompt-pack"
PROMPT_PACK_RUNTIME_CAPABILITY = "prompt-rules"


@dataclass(frozen=True, slots=True)
class PromptPackRuleEntry:
    relative_path: str
    content: str
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class LoadedPromptPack:
    name: str
    version: str
    path: Path
    source_kind: str
    trusted_capabilities: tuple[str, ...] = ()
    rules: tuple[PromptPackRuleEntry, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PromptPackRuntimeContext:
    content: str = ""
    active_prompt_packs: tuple[str, ...] = ()
    omitted_prompt_packs: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)


def _read_text_entrypoint(
    path: Path,
    *,
    max_chars: int,
) -> PromptPackRuleEntry | None:
    try:
        content = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, IsADirectoryError, OSError, UnicodeDecodeError):
        return None

    if not content:
        return None

    truncated = False
    if max_chars > 0 and len(content) > max_chars:
        truncated = True
        content = content[:max_chars].rstrip() + "\n\n[truncated]"

    return PromptPackRuleEntry(
        relative_path=path.name,
        content=content,
        truncated=truncated,
    )


def load_enabled_prompt_packs(
    omicsclaw_dir: str | Path,
    *,
    max_chars_per_entry: int = 2000,
) -> list[LoadedPromptPack]:
    """Load enabled, tracked local prompt packs for runtime activation."""

    loaded: list[LoadedPromptPack] = []
    for item in list_installed_extensions(
        omicsclaw_dir,
        extension_types=(PROMPT_PACK_EXTENSION_TYPE,),
    ):
        if item.extension_type != PROMPT_PACK_EXTENSION_TYPE:
            continue
        if not item.state.enabled or item.record is None:
            continue
        if item.record.source_kind and item.record.source_kind != "local":
            continue

        manifest_path = discover_extension_manifest(item.path)
        if manifest_path is None:
            continue
        try:
            manifest = load_extension_manifest(manifest_path)
        except ValueError:
            continue
        if manifest.type != PROMPT_PACK_EXTENSION_TYPE:
            continue

        trusted_capabilities = tuple(
            dict.fromkeys(
                capability
                for capability in (
                    manifest.trusted_capabilities or item.record.trusted_capabilities
                )
                if str(capability).strip()
            )
        )
        if trusted_capabilities and PROMPT_PACK_RUNTIME_CAPABILITY not in trusted_capabilities:
            continue

        rules: list[PromptPackRuleEntry] = []
        for relative_path in manifest.entrypoints:
            entry = _read_text_entrypoint(
                item.path / relative_path,
                max_chars=max_chars_per_entry,
            )
            if entry is None:
                continue
            rules.append(
                PromptPackRuleEntry(
                    relative_path=relative_path,
                    content=entry.content,
                    truncated=entry.truncated,
                )
            )

        if not rules:
            continue

        loaded.append(
            LoadedPromptPack(
                name=manifest.name,
                version=manifest.version,
                path=item.path,
                source_kind=item.record.source_kind,
                trusted_capabilities=trusted_capabilities,
                rules=tuple(rules),
                metadata={
                    "manifest_path": str(manifest_path),
                    "relative_install_path": item.record.relative_install_path,
                },
            )
        )

    return sorted(loaded, key=lambda pack: (pack.name.lower(), str(pack.path)))


def load_prompt_pack_runtime_context(
    omicsclaw_dir: str | Path,
    *,
    max_chars_per_entry: int = 2000,
    max_total_chars: int = 6000,
    surface: str = "",
    skill: str = "",
    query: str = "",
    domain: str = "",
) -> PromptPackRuntimeContext:
    """Build a context block for enabled prompt packs.

    The additional selection hints are accepted for future filtering logic; the
    current implementation activates all enabled local prompt packs.
    """

    del surface, skill, query, domain

    packs = load_enabled_prompt_packs(
        omicsclaw_dir,
        max_chars_per_entry=max_chars_per_entry,
    )
    if not packs:
        return PromptPackRuntimeContext()

    header_lines = [
        "## Active Local Prompt Packs",
        "",
        "Enabled local prompt packs are active for this session. Core OmicsClaw guardrails override them on conflict.",
    ]
    content_parts = ["\n".join(header_lines).strip()]
    active_names: list[str] = []
    omitted_names: list[str] = []

    for index, pack in enumerate(packs):
        version_suffix = f" v{pack.version}" if pack.version else ""
        pack_lines = [f"### {pack.name}{version_suffix}"]
        for rule in pack.rules:
            pack_lines.append(f"[{rule.relative_path}]")
            pack_lines.append(rule.content)
        pack_block = "\n\n".join(pack_lines).strip()

        candidate_parts = [*content_parts, pack_block]
        candidate_text = "\n\n".join(candidate_parts).strip()
        if max_total_chars > 0 and len(candidate_text) > max_total_chars:
            if active_names:
                omitted_names.extend(remaining.name for remaining in packs[index:])
                break

            available_chars = max(max_total_chars - len(content_parts[0]) - 32, 0)
            if available_chars:
                truncated = pack_block[:available_chars].rstrip()
                if truncated:
                    content_parts.append(truncated + "\n\n[truncated due to context budget]")
                    active_names.append(pack.name)
                else:
                    omitted_names.append(pack.name)
            else:
                omitted_names.append(pack.name)
            omitted_names.extend(remaining.name for remaining in packs[index + 1:])
            break

        content_parts.append(pack_block)
        active_names.append(pack.name)

    if omitted_names:
        content_parts.append(
            "Additional prompt packs omitted due to context budget: "
            + ", ".join(omitted_names)
        )

    return PromptPackRuntimeContext(
        content="\n\n".join(part for part in content_parts if part).strip(),
        active_prompt_packs=tuple(active_names),
        omitted_prompt_packs=tuple(omitted_names),
        metadata={
            "pack_count": len(active_names),
            "available_pack_names": tuple(pack.name for pack in packs),
            "max_chars_per_entry": max_chars_per_entry,
            "max_total_chars": max_total_chars,
        },
    )


def build_prompt_pack_context(
    omicsclaw_dir: str | Path,
    *,
    max_chars_per_entry: int = 2000,
    max_total_chars: int = 6000,
    surface: str = "",
    skill: str = "",
    query: str = "",
    domain: str = "",
) -> str:
    return load_prompt_pack_runtime_context(
        omicsclaw_dir,
        max_chars_per_entry=max_chars_per_entry,
        max_total_chars=max_total_chars,
        surface=surface,
        skill=skill,
        query=query,
        domain=domain,
    ).content


__all__ = [
    "LoadedPromptPack",
    "PROMPT_PACK_EXTENSION_TYPE",
    "PROMPT_PACK_RUNTIME_CAPABILITY",
    "PromptPackRuleEntry",
    "PromptPackRuntimeContext",
    "build_prompt_pack_context",
    "load_enabled_prompt_packs",
    "load_prompt_pack_runtime_context",
]
