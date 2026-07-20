from __future__ import annotations

from collections import Counter
from pathlib import Path

from omicsclaw.skill.registry import OmicsRegistry


ROOT = Path(__file__).resolve().parent.parent
AUTHORITATIVE_CHANNEL_DOCS = (
    "README.md",
    "README_zh-CN.md",
    "AGENTS.md",
    "omicsclaw/surfaces/channels/README.md",
    "docs/adr/0060-deliver-terminal-channel-replies-through-a-persistent-outbox.md",
)


def _registry_counts() -> tuple[int, dict[str, int]]:
    reg = OmicsRegistry()
    reg.load_all()
    items = reg.iter_primary_skills()
    counts = Counter(str(info.get("domain", "")).strip() for _, info in items)
    return len(items), dict(counts)


def test_public_docs_match_registry_skill_counts():
    total, counts = _registry_counts()
    domain_count = len(counts)

    expected_fragments = {
        "README.md": [
            f"**{total} registered skills**",
        ],
        "README_zh-CN.md": [
            f"{total} 个分析技能",
            f"全部 {total} 个技能的机器可读注册表",
        ],
        "AGENTS.md": [
            f"{counts['spatial']} spatial transcriptomics skills",
            f"{counts['singlecell']} single-cell omics skills",
            f"{counts['genomics']} genomics skills",
            f"{counts['proteomics']} proteomics skills",
            f"{counts['metabolomics']} metabolomics skills",
            f"{counts['bulkrna']} bulk RNA skills",
            f"{counts['orchestrator']} orchestration skills",
            f"{counts['literature']} literature skill",
        ],
        "docs/architecture/overview.mdx": [
            f"{total} 个 skill",
            f"{domain_count} 个 domain",
        ],
        "docs/architecture/skill-system.mdx": [
            f"{total} 个 skill",
            f"{domain_count} 个领域",
            f"（共 {counts['spatial']} 个）",
            f"singlecell/    （{counts['singlecell']} 个）",
            f"literature/    （{counts['literature']} 个）",
        ],
        "docs/architecture/orchestrator.mdx": [
            f"{total} 个 skill",
            f"{domain_count} 个组学/支撑领域",
        ],
    }

    missing: list[str] = []
    for relative_path, fragments in expected_fragments.items():
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        for fragment in fragments:
            if fragment not in text:
                missing.append(f"{relative_path}: {fragment}")

    assert not missing, "\n".join(missing)


def test_public_docs_do_not_claim_legacy_framework_facts():
    stale_fragments = {
        "AGENTS.md": [
            "supporting 5 domains",
            "List all 50+ skills across 5 domains",
            "`execute_omicsclaw()` — runs `omicsclaw.py run <skill>` as subprocess",
        ],
        "omicsclaw.py": [
            "List all 50+ available analysis skills",
        ],
        "omicsclaw/surfaces/cli/_constants.py": [
            "50+ analysis skills",
        ],
        "tests/test_execution_default_executor.py": [
            "default ``SubprocessExecutor`` wiring used by /jobs",
            "test_build_default_executor_returns_subprocess_executor",
            "test_jobs_router_default_executor_is_subprocess_executor",
        ],
        "docs/architecture/overview.mdx": [
            "cmd 拼接 → skill 入口脚本路径 + flags",
            "subprocess 执行 skill 脚本",
        ],
    }

    found: list[str] = []
    for relative_path, fragments in stale_fragments.items():
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        for fragment in fragments:
            if fragment in text:
                found.append(f"{relative_path}: {fragment}")

    assert not found, "\n".join(found)


def test_channel_docs_match_authoritative_production_scope():
    common_fragments = [
        "ControlRuntime",
        "python -m omicsclaw.surfaces.channels --channels telegram",
        "python -m omicsclaw.surfaces.channels --channels feishu",
        "FEISHU_ALLOWED_SENDERS",
        "FEISHU_BOT_OPEN_ID",
    ]
    localized_fragments = {
        "README.md": [
            "shared runner",
            "Owner-only Telegram text plus one ordinary photo",
            "Owner-only Feishu text-only",
            "`FEISHU_ALLOWED_SENDERS` and `FEISHU_BOT_OPEN_ID` are mandatory",
            "the latter is the identity used to prove a group message mentioned this Bot",
            "other Channel Adapters remain gated",
            "outbound media remains incomplete and fail-closed",
            "not full ADR or media completion",
        ],
        "README_zh-CN.md": [
            "shared runner",
            "仅 Owner 可用的 Telegram 文本与单张普通图片",
            "仅 Owner 可用的飞书纯文本",
            "飞书必须配置 `FEISHU_ALLOWED_SENDERS` 与 `FEISHU_BOT_OPEN_ID`",
            "后者用于证明群消息确实 @ 了当前 Bot",
            "其他 Channel Adapter 仍保持 gated",
            "出站媒体仍未完成并保持 fail-closed",
            "不代表 ADR 或媒体能力已全部完成",
        ],
        "AGENTS.md": [
            "shared runner",
            "Owner-only Telegram text plus one ordinary photo",
            "Owner-only Feishu text-only",
            "`FEISHU_ALLOWED_SENDERS` and `FEISHU_BOT_OPEN_ID` are mandatory",
            "the Bot open ID proves a group message mentioned this Bot",
            "other Channel Adapters remain gated",
            "outbound media remains incomplete and fail-closed",
            "not full ADR or media completion",
        ],
        "omicsclaw/surfaces/channels/README.md": [
            "shared runner",
            "Owner-only Telegram text plus one ordinary photo",
            "Owner-only Feishu text-only",
            "`FEISHU_ALLOWED_SENDERS` and `FEISHU_BOT_OPEN_ID` are mandatory",
            "`FEISHU_BOT_OPEN_ID` proves that a group @mention names this Bot",
            "other Channel Adapters remain gated",
            "outbound media remains incomplete and fail-closed",
            "not full ADR or media completion",
        ],
        "docs/adr/0060-deliver-terminal-channel-replies-through-a-persistent-outbox.md": [
            "shared runner",
            "Owner-only Telegram text plus one ordinary photo",
            "Owner-only Feishu text-only",
            "`FEISHU_ALLOWED_SENDERS` and `FEISHU_BOT_OPEN_ID` are mandatory",
            "the latter proves that a group message mentions this Bot",
            "other Channel Adapters remain gated",
            "outbound media remains incomplete and fail-closed",
            "not full ADR or media completion",
        ],
    }

    missing: list[str] = []
    for relative_path, specific_fragments in localized_fragments.items():
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        normalized = " ".join(text.split()).lower()
        for fragment in [*common_fragments, *specific_fragments]:
            if " ".join(fragment.split()).lower() not in normalized:
                missing.append(f"{relative_path}: {fragment}")

    assert not missing, "\n".join(missing)


def test_channel_docs_do_not_bypass_the_shared_runner():
    forbidden_fragments = [
        "ChannelManager.start_all(",
        "Telegram text and one ordinary photo per message are the only production-enabled inputs",
        "Only Owner-only Telegram text and one ordinary photo with an optional caption are enabled production inputs",
        "Except for Telegram text/single-photo input, this matrix describes migration-source declarations",
        "Feishu is currently disabled",
        "Telegram is the only authoritative Channel",
        "Telegram is currently the only production-enabled Channel",
        "all non-Telegram Channel Adapters remain fail-closed",
        "Channel Surface — authoritative Telegram text/single-photo + gated legacy adapters",
        "Channel Surface — Telegram text + single photo authoritative; legacy adapters gated",
        "当前已切换的 Telegram 文本/单图通道",
        "Telegram 文本 + 单图；其余适配器关闭待迁移",
        "仅 Owner 可用的 Telegram 文本 + 单图/caption；其他媒体及适配器显式关闭",
    ]

    found: list[str] = []
    for relative_path in AUTHORITATIVE_CHANNEL_DOCS:
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        normalized = " ".join(text.split()).lower()
        for fragment in forbidden_fragments:
            if " ".join(fragment.split()).lower() in normalized:
                found.append(f"{relative_path}: {fragment}")

    assert not found, "\n".join(found)
