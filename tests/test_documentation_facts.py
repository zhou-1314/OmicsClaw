from __future__ import annotations

from collections import Counter
from pathlib import Path

from omicsclaw.skill.registry import OmicsRegistry


ROOT = Path(__file__).resolve().parent.parent
CHANNEL_SCOPE_SECTIONS = {
    "README.md": (
        "The production Channel scope is",
        "## 📦 Installation",
    ),
    "README_zh-CN.md": (
        "生产 Channel 范围由",
        "## 📦 安装",
    ),
    "AGENTS.md": (
        "### Channel Surface — authoritative Telegram text/photo + Feishu text",
        "### CLI Surface",
    ),
    "omicsclaw/surfaces/channels/README.md": (
        "# OmicsClaw Channel Surface",
        "## Interactive Terminal Chat",
    ),
    "docs/adr/0060-deliver-terminal-channel-replies-through-a-persistent-outbox.md": (
        "The production scope is the shared runner",
        "This slice is text-only for *outbound* delivery.",
    ),
    "docs/CONTEXT.md": (
        "#### Current production Channel scope",
        "**Desktop Surface**",
    ),
    "docs/ARCHITECTURE.md": (
        "### Current production Channel scope",
        "## Module ownership ledger",
    ),
}


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
    expected_facts = {
        "shared runner": (("shared runner",),),
        "ControlRuntime": (("controlruntime",),),
        "Owner-only": (("owner-only",), ("仅 owner",)),
        "Telegram ordinary photo": (
            ("telegram", "one ordinary photo"),
            ("telegram", "单张普通图片"),
        ),
        "Feishu text-only": (("feishu", "text-only"), ("飞书", "纯文本")),
        "both Feishu identities mandatory": (
            (
                "feishu_allowed_senders",
                "feishu_bot_open_id",
                "mandatory",
            ),
            (
                "feishu_allowed_senders",
                "feishu_bot_open_id",
                "required",
            ),
            (
                "feishu_allowed_senders",
                "feishu_bot_open_id",
                "requires both",
            ),
            ("feishu_allowed_senders", "feishu_bot_open_id", "必须"),
        ),
        "group mention proves this Bot": (
            ("feishu_bot_open_id", "mention", "this bot"),
            ("feishu_bot_open_id", "@", "this bot"),
            ("feishu_bot_open_id", "@", "当前 bot"),
        ),
        "other Adapters gated": (
            ("other channel adapters", "gated"),
            ("remaining channel adapters", "gated"),
            ("其他 channel adapter", "gated"),
        ),
        "outbound media incomplete and fail-closed": (
            ("outbound media", "incomplete", "fail-closed"),
            ("出站媒体", "未完成", "fail-closed"),
        ),
        "not full completion": (("not full adr",), ("不代表 adr",)),
    }

    failures: list[str] = []
    for relative_path, (start, end) in CHANNEL_SCOPE_SECTIONS.items():
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        if start not in text or end not in text:
            failures.append(f"{relative_path}: missing current-scope markers")
            continue
        section = start + text.split(start, 1)[1].split(end, 1)[0]
        normalized = " ".join(section.split()).lower()
        for fact, alternatives in expected_facts.items():
            if not any(all(term in normalized for term in terms) for terms in alternatives):
                failures.append(f"{relative_path}: {fact}")

    assert not failures, "\n".join(failures)


def test_channel_docs_do_not_bypass_the_shared_runner():
    found: list[str] = []
    for relative_path, (start, end) in CHANNEL_SCOPE_SECTIONS.items():
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        if start not in text or end not in text:
            continue
        section = start + text.split(start, 1)[1].split(end, 1)[0]
        if "ChannelManager.start_all(" in section:
            found.append(f"{relative_path}: ChannelManager.start_all(")

    assert not found, "\n".join(found)
