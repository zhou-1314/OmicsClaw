"""Tests for chat context assembly and prompt preparation."""

import asyncio
import json

from omicsclaw.surfaces.cli import _mcp
from omicsclaw.extensions import write_extension_state, write_install_record
from omicsclaw.runtime.context.layers import (
    build_mcp_instructions_block,
    should_prefetch_knowledge_guidance,
    should_prefetch_skill_context,
)
from omicsclaw.runtime.context.assembler import (
    ContextAssemblyRequest,
    assemble_chat_context,
    assemble_prompt_context,
    build_user_message_content,
    extract_analysis_hints,
    should_attach_capability_context,
)


def test_build_user_message_content_converts_multimodal_blocks():
    content = build_user_message_content(
        [
            {"type": "text", "text": "hello"},
            {
                "type": "image",
                "source": {
                    "media_type": "image/png",
                    "data": "abc123",
                },
            },
        ]
    )

    assert content == [
        {"type": "text", "text": "hello"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,abc123"},
        },
    ]


def test_extract_analysis_hints_and_capability_trigger_are_stable():
    skill_hint, domain_hint = extract_analysis_hints(
        "Please run spatial-preprocess on this Visium dataset."
    )

    assert skill_hint == "spatial-preprocess"
    assert domain_hint == "spatial"
    assert should_attach_capability_context("analyze sample.h5ad") is True
    assert should_attach_capability_context("hello there") is False


def test_assemble_prompt_context_layers_are_ordered_and_accounted():
    assembly = assemble_prompt_context(
        request=ContextAssemblyRequest(
            surface="interactive",
            base_persona="BASE PERSONA",
            memory_context="preferred language: Chinese",
            skill="spatial-preprocess",
            query="Analyze sample.h5ad with spatial-preprocess",
            domain="spatial",
            capability_context="## Deterministic Capability Assessment\n- coverage: exact_skill",
            workspace="/tmp/session",
            pipeline_workspace="/tmp/pipeline",
            mcp_servers=("seq-think", "bio-tools"),
            include_knowhow=True,
            knowhow_loader=lambda **_: "⚠️ MANDATORY SCIENTIFIC CONSTRAINTS\n- Validate QC first.",
        )
    )

    # Test request triggers anndata_or_file_path_in_query (h5ad in query)
    # and workspace_active (workspace + pipeline_workspace set), so the
    # corresponding predicate-gated rule layers appear. Other predicate
    # layers stay quiet (no implementation/memory/pdf keywords; capability
    # context already present; surface=interactive so chat_mode_rule off).
    assert assembly.layer_names == (
        "base_persona",
        "surface_voice_rules",
        "file_path_and_inspect_rule",
        "output_format",
        "workspace_continuity_rule",
        "memory_context",
        "skill_context",
        "capability_assessment",
        "knowhow_constraints",
        "workspace_context",
        "mcp_instructions",
    )
    assert assembly.layer_stats["workspace_context"]["placement"] == "system"
    assert assembly.layer_stats["mcp_instructions"]["order"] > assembly.layer_stats["workspace_context"]["order"]
    assert assembly.layer_stats["knowhow_constraints"]["cost_chars"] > 0
    assert assembly.total_estimated_tokens >= len(assembly.layer_names)
    assert "BASE PERSONA" in assembly.system_prompt
    assert "## Output Style Profile" in assembly.system_prompt
    assert "preferred language: Chinese" in assembly.system_prompt
    assert "seq-think" in assembly.system_prompt
    # ADR 0024 — query-volatile layers now render into message_context (the
    # user turn), not the system prefix; the system prefix stays cache-stable.
    assert "## Prefetched Skill Context" in assembly.message_context
    assert "Selected skill: `spatial-preprocess`" in assembly.message_context
    # file_path_and_inspect_rule + workspace_continuity_rule fired (h5ad in
    # query, workspace set) and now ride the user turn.
    assert "Workspace Continuity" in assembly.message_context
    assert "## Prefetched Skill Context" not in assembly.system_prompt
    assert assembly.message_context != ""


def test_assemble_prompt_context_can_route_workspace_to_message_context():
    assembly = assemble_prompt_context(
        request=ContextAssemblyRequest(
            surface="bot",
            base_persona="BASE PERSONA",
            workspace="/tmp/session",
            include_knowhow=False,
            workspace_placement="message",
        )
    )

    # Bot mode includes output_format layer, so system prompt is not just base persona
    assert "BASE PERSONA" in assembly.system_prompt
    assert "## Output Style Profile" in assembly.system_prompt
    assert "Surface Adapter (bot)" in assembly.system_prompt
    # ADR 0024 — other gated rule layers also render to message now, so
    # workspace_context is present but no longer necessarily first.
    assert "## Workspace Context" in assembly.message_context
    assert "/tmp/session" in assembly.message_context
    assert assembly.layer_stats["workspace_context"]["placement"] == "message"


# Phase 4 retired ``get_execution_discipline`` and the
# ``include_execution_discipline`` / ``include_skill_contract`` toggles.
# The chat-mode and workspace-continuity rules they used to ship now live
# in dedicated predicate-gated layers; their content/trigger logic is
# covered in ``tests/test_predicate_gated_injectors.py``.


def test_assemble_chat_context_loads_memory_and_builds_prompt():
    calls = {
        "session": [],
        "resolver": None,
    }

    class FakeSessionManager:
        async def get_or_create(self, user_id, platform, chat_id, thread_id=""):
            calls["session"].append(("get_or_create", user_id, platform, chat_id))

        async def load_context(self, session_id, thread_id=""):
            calls["session"].append(("load_context", session_id))
            return "preferred language: Chinese"

    class FakeDecision:
        chosen_skill = "spatial-preprocess"
        domain = "spatial"

        def to_prompt_block(self):
            return "## Deterministic Capability Assessment\n- coverage: exact_skill"

    def fake_capability_resolver(query, *, domain_hint=""):
        calls["resolver"] = (query, domain_hint)
        return FakeDecision()

    context = asyncio.run(
        assemble_chat_context(
            chat_id="chat-1",
            user_content="Analyze sample.h5ad with spatial-preprocess",
            user_id="user-1",
            platform="telegram",
            session_manager=FakeSessionManager(),
            capability_resolver=fake_capability_resolver,
            skill_aliases=("spatial-preprocess", "sc-qc"),
        )
    )

    assert context.session_id == "telegram:user-1:chat-1"
    assert context.memory_context == "preferred language: Chinese"
    assert context.user_text == "Analyze sample.h5ad with spatial-preprocess"
    # ADR 0024 — query-volatile layers (here the file-path rule, triggered by
    # "sample.h5ad") now ride the user turn as Volatile context, prepended to
    # the raw user text, and are frozen into history append-only.
    assert "Analyze sample.h5ad with spatial-preprocess" in context.user_message_content
    assert "## File Path Discipline" in context.user_message_content
    assert context.skill_hint == "spatial-preprocess"
    assert context.domain_hint == "spatial"
    assert context.capability_context.startswith("## Deterministic Capability Assessment")
    assert calls["session"] == [
        ("get_or_create", "user-1", "telegram", "chat-1"),
        ("load_context", "telegram:user-1:chat-1"),
    ]
    assert calls["resolver"] == (
        "Analyze sample.h5ad with spatial-preprocess",
        "spatial",
    )
    # F3 — assert the REAL assembled context instead of discarded builder kwargs.
    # memory_context is a SYSTEM layer: it renders into the system prompt.
    assert "preferred language: Chinese" in context.system_prompt
    assert "preferred language: Chinese" in context.memory_context
    assert context.scoped_memory_context == ""
    assert context.skill_hint == "spatial-preprocess"
    request = context.prompt_context.request
    assert request.skill_candidates == ("spatial-preprocess",)
    assert request.query == "Analyze sample.h5ad with spatial-preprocess"
    # Also assert the request DTO itself carries the scalars that downstream
    # layer/tool predicates consume — guards against a stale request assembled
    # under an otherwise-correct AssembledChatContext scalar (codex x-val P2).
    assert request.skill == "spatial-preprocess"
    assert request.domain == "spatial"
    assert (
        request.capability_context
        == "## Deterministic Capability Assessment\n- coverage: exact_skill"
    )
    assert context.domain_hint == "spatial"
    assert (
        context.capability_context
        == "## Deterministic Capability Assessment\n- coverage: exact_skill"
    )
    assert request.surface == "bot"
    assert request.output_style == ""
    assert request.workspace == ""
    assert request.pipeline_workspace == ""
    assert request.mcp_servers == ()
    layer_stats = context.prompt_context.layer_stats
    assert "plan_context" not in layer_stats
    assert "transcript_context" not in layer_stats
    # skill_context is a MESSAGE layer: it renders into message_context, not system.
    assert context.skill_context.startswith("## Prefetched Skill Context")
    assert "Selected skill: `spatial-preprocess`" in context.skill_context
    assert "- Domain: `spatial`" in context.skill_context
    assert "- Summary:" in context.skill_context
    assert context.skill_context in context.prompt_context.message_context
    assert "workspace_context" not in context.prompt_context.layer_stats


def test_assemble_chat_context_injects_research_stance_in_single_assembly():
    # F3: research_stance must render into the system prompt via the SINGLE
    # injector assembly — even with no custom system_prompt_builder. This lets
    # the engine drop the redundant legacy second assembly for the default path.
    async def stance_loader(session_id):
        return "RESEARCH_STANCE_MARKER_XYZ"

    context = asyncio.run(
        assemble_chat_context(
            chat_id="chat-1",
            user_content="analyze data",
            user_id="user-1",
            platform="cli",
            research_stance_loader=stance_loader,
        )
    )

    assert "RESEARCH_STANCE_MARKER_XYZ" in context.system_prompt


def test_single_assembly_system_prompt_equals_legacy_builder():
    # F3 contract: the single injector assembly must produce a BYTE-IDENTICAL
    # system prompt to the legacy build_system_prompt path, so dropping the
    # second assembly (engine default path) changes nothing. Exercise the two
    # fields that differ: research_stance (now folded into the single request)
    # and knowledge_context (legacy-only — must NOT leak into system, since
    # knowledge_guidance is a message-placement layer).
    from omicsclaw.runtime.context.assembler import assemble_prompt_context
    from omicsclaw.runtime.context.layers import ContextAssemblyRequest
    from omicsclaw.runtime.context.system_prompt import build_system_prompt

    common = dict(
        surface="bot",
        memory_context="MEM",
        skill_context="## Prefetched Skill Context\n- x",
        skill="spatial-preprocess",
        query="analyze",
        domain="spatial",
        capability_context="## Deterministic Capability Assessment",
        plan_context="PLAN",
        transcript_context="TX",
        output_style="",
        research_stance="STANCE MARKER",
        prompt_pack_context="PACK",
    )
    legacy = build_system_prompt(
        **common,
        knowledge_context="KNOWLEDGE GUIDANCE BODY",
        include_knowledge_guidance=True,
    )
    single = assemble_prompt_context(request=ContextAssemblyRequest(**common)).system_prompt

    assert single == legacy
    assert "STANCE MARKER" in single  # research_stance renders into system
    assert "KNOWLEDGE GUIDANCE BODY" not in single  # knowledge stays out of system


def test_assemble_chat_context_forwards_thread_id_to_session_memory():
    """AN-CTXRECALL-11: the active investigation thread_id reaches both the
    session stamp (get_or_create) and the passive memory load (load_context),
    so per-turn injection is thread-scoped."""
    seen = {"get_or_create": None, "load_context": None}

    class FakeSessionManager:
        async def get_or_create(self, user_id, platform, chat_id, thread_id=""):
            seen["get_or_create"] = thread_id

        async def load_context(self, session_id, thread_id=""):
            seen["load_context"] = thread_id
            return "scoped memory"

    context = asyncio.run(
        assemble_chat_context(
            chat_id="chat-9",
            user_content="hello",
            user_id="user-9",
            platform="app",
            thread_id="t-glioma",
            session_manager=FakeSessionManager(),
        )
    )

    assert context.memory_context == "scoped memory"
    assert seen["get_or_create"] == "t-glioma"
    assert seen["load_context"] == "t-glioma"


def test_assemble_chat_context_passes_interactive_surface_to_assembly():
    context = asyncio.run(
        assemble_chat_context(
            chat_id="chat-2",
            user_content="hello there",
            user_id="user-2",
            platform="cli",
            output_style="teaching",
            workspace="/tmp/chat-workspace",
            pipeline_workspace="/tmp/pipeline-workspace",
            mcp_servers=("seq-think", "bio-tools"),
        )
    )

    request = context.prompt_context.request
    assert request.surface == "interactive"
    assert request.output_style == "teaching"
    assert request.workspace == "/tmp/chat-workspace"
    assert request.pipeline_workspace == "/tmp/pipeline-workspace"
    assert request.mcp_servers == ("seq-think", "bio-tools")
    layer_stats = context.prompt_context.layer_stats
    assert "workspace_context" in layer_stats
    assert "mcp_instructions" in layer_stats


def test_assemble_chat_context_loads_scoped_memory_and_forwards_to_message_context():
    class FakeRecall:
        def to_context_text(self):
            return "1. PBMC QC defaults\n   scope=project | owner=tester | freshness=evolving | updated=2026-04-02"

    context = asyncio.run(
        assemble_chat_context(
            chat_id="chat-scoped-memory",
            user_content="Continue the PBMC QC analysis",
            user_id="user-scoped-memory",
            platform="cli",
            workspace="/tmp/project",
            scoped_memory_scope="project",
            scoped_memory_loader=lambda **_: FakeRecall(),
        )
    )

    assert "PBMC QC defaults" in context.scoped_memory_context
    assert context.scoped_memory_context.startswith("1. PBMC QC defaults")
    # scoped_memory_context is a MESSAGE layer: it renders into message_context.
    assert "PBMC QC defaults" in context.prompt_context.message_context


def test_assemble_prompt_context_prefetches_knowledge_guidance_for_method_questions():
    calls = []

    def fake_knowledge_loader(**kwargs):
        calls.append(kwargs)
        return "## Preloaded Knowledge Guidance\n\nPrefer Harmony for batch correction."

    assembly = assemble_prompt_context(
        request=ContextAssemblyRequest(
            surface="interactive",
            base_persona="BASE PERSONA",
            query="Which method should I use for batch correction?",
            domain="singlecell",
            include_knowhow=False,
            knowledge_loader=fake_knowledge_loader,
        )
    )

    assert "knowledge_guidance" in assembly.layer_stats
    # ADR 0024 — per-query knowledge prefetch is Volatile context (message).
    assert "Prefer Harmony for batch correction." in assembly.message_context
    assert calls == [
        {
            "query": "Which method should I use for batch correction?",
            "skill": "",
            "domain": "singlecell",
        }
    ]


def test_should_prefetch_knowledge_guidance_covers_more_real_world_queries():
    assert should_prefetch_knowledge_guidance(
        query="Can you recommend a suitable workflow for single-cell batch correction?"
    ) is True
    assert should_prefetch_knowledge_guidance(
        query="Seurat vs Harmony for integration, which should I choose?"
    ) is True
    assert should_prefetch_knowledge_guidance(
        query="帮我推荐适合这个数据的空间变异基因方法"
    ) is True
    assert should_prefetch_knowledge_guidance(
        query="这两种方法有什么区别，参数怎么选？"
    ) is True
    assert should_prefetch_knowledge_guidance(
        query="Analyze sample.h5ad and save outputs."
    ) is False


def test_should_prefetch_skill_context_tracks_skill_or_capability_hits():
    assert should_prefetch_skill_context(
        skill="spatial-preprocess",
        query="Analyze sample.h5ad",
    ) is True
    assert should_prefetch_skill_context(
        query="Analyze sample.h5ad",
        capability_context="## Deterministic Capability Assessment\n- coverage: exact_skill",
    ) is True
    assert should_prefetch_skill_context(
        query="hello there",
    ) is False


def test_assemble_prompt_context_skips_knowledge_guidance_for_generic_requests():
    calls = []

    def fake_knowledge_loader(**kwargs):
        calls.append(kwargs)
        return "## Preloaded Knowledge Guidance\n\nThis should not be used."

    assembly = assemble_prompt_context(
        request=ContextAssemblyRequest(
            surface="interactive",
            base_persona="BASE PERSONA",
            query="Analyze sample.h5ad",
            domain="singlecell",
            include_knowhow=False,
            knowledge_loader=fake_knowledge_loader,
        )
    )

    assert "knowledge_guidance" not in assembly.layer_stats
    assert "This should not be used." not in assembly.system_prompt
    assert calls == []


def test_assemble_prompt_context_includes_skill_context_only_when_relevant():
    assembly = assemble_prompt_context(
        request=ContextAssemblyRequest(
            surface="interactive",
            base_persona="BASE PERSONA",
            skill="spatial-preprocess",
            query="Analyze sample.h5ad with spatial-preprocess",
            domain="spatial",
            include_knowhow=False,
        )
    )

    assert "skill_context" in assembly.layer_stats
    # ADR 0024 — matched-skill context is Volatile context (message), not system.
    assert "## Prefetched Skill Context" in assembly.message_context
    assert "Selected skill: `spatial-preprocess`" in assembly.message_context


def test_build_mcp_instructions_block_skips_inactive_entries():
    block = build_mcp_instructions_block(
        [
            {"name": "offline-http", "transport": "http", "active": False},
            {"name": "seq-think", "transport": "stdio", "active": True},
        ]
    )

    assert "seq-think" in block
    assert "offline-http" not in block
    assert "Active MCP transports: stdio" in block


def test_load_active_mcp_server_entries_for_prompt_filters_per_server(monkeypatch):
    monkeypatch.setattr(
        _mcp,
        "load_mcp_config",
        lambda: {
            "seq-think": {"transport": "stdio", "command": "npx"},
            "offline-http": {"transport": "http", "url": "http://offline"},
        },
    )

    async def _fake_probe(name, server):
        if name == "seq-think":
            return {
                "name": name,
                "transport": server["transport"],
                "active": True,
                "loaded": True,
            }
        return None

    monkeypatch.setattr(_mcp, "_probe_mcp_server_entry_for_prompt", _fake_probe)
    monkeypatch.setattr(_mcp, "_PROMPT_STATUS_CACHE_KEY", None)
    monkeypatch.setattr(_mcp, "_PROMPT_STATUS_CACHE_VALUE", ())
    monkeypatch.setattr(_mcp, "_PROMPT_STATUS_CACHE_AT", 0.0)

    entries = asyncio.run(_mcp.load_active_mcp_server_entries_for_prompt())

    assert entries == (
        {
            "name": "seq-think",
            "transport": "stdio",
            "active": True,
            "loaded": True,
        },
    )


def test_mcp_runtime_config_skips_disabled_entries_and_forwards_headers(monkeypatch):
    monkeypatch.setenv("MCP_AUTH", "Bearer token")
    monkeypatch.setattr(
        _mcp,
        "_load_raw",
        lambda: {
            "disabled-remote": {
                "transport": "sse",
                "url": "https://disabled.example/sse",
                "headers": {"Authorization": "${MCP_AUTH}"},
                "enabled": False,
            },
            "secure-remote": {
                "transport": "http",
                "url": "https://enabled.example/mcp",
                "headers": {"Authorization": "${MCP_AUTH}"},
            },
        },
    )

    runtime_config = _mcp.load_mcp_config()
    all_config = _mcp.load_mcp_config(include_disabled=True)
    connection = _mcp._build_mcp_connection(all_config["secure-remote"])

    assert "disabled-remote" not in runtime_config
    assert all_config["disabled-remote"]["enabled"] is False
    assert all_config["secure-remote"]["headers"] == {"Authorization": "Bearer token"}
    assert connection == {
        "transport": "http",
        "url": "https://enabled.example/mcp",
        "headers": {"Authorization": "Bearer token"},
    }
    assert _mcp._build_mcp_connection(all_config["disabled-remote"]) is None


def test_assemble_chat_context_forwards_knowledge_guidance_into_message_context(monkeypatch):
    monkeypatch.setattr(
        "omicsclaw.runtime.context.layers.load_knowledge_guidance",
        lambda **_: "## Preloaded Knowledge Guidance\n\nPrefer Harmony for batch correction.",
    )

    context = asyncio.run(
        assemble_chat_context(
            chat_id="chat-knowledge",
            user_content="Which method should I use for batch correction?",
            user_id="user-knowledge",
            platform="cli",
        )
    )

    # knowledge_guidance is a MESSAGE layer: it renders into message_context and
    # must stay OUT of the byte-stable system prefix.
    assert "knowledge_guidance" in context.prompt_context.layer_stats
    message_context = context.prompt_context.message_context
    assert "## Preloaded Knowledge Guidance" in message_context
    assert "Prefer Harmony for batch correction." in message_context
    assert "## Preloaded Knowledge Guidance" not in context.system_prompt


def test_assemble_prompt_context_includes_plan_context_layer():
    assembly = assemble_prompt_context(
        request=ContextAssemblyRequest(
            surface="interactive",
            base_persona="BASE PERSONA",
            plan_context="## Active Plan Mode\n\n- Status: approved",
            include_knowhow=False,
        )
    )

    assert "plan_context" in assembly.layer_stats
    # ADR 0024 — evolving plan state is Volatile context (message).
    assert "## Active Plan Mode" in assembly.message_context


def test_system_prompt_is_byte_stable_across_query_intents():
    """ADR 0024 Phase 2 core invariant: the system prefix must be
    byte-identical regardless of the query, so the provider's prefix cache
    stays warm. Query-volatile content rides message_context instead."""

    def _assemble(query: str):
        return assemble_prompt_context(
            request=ContextAssemblyRequest(
                surface="bot",
                base_persona="BASE PERSONA",
                include_knowhow=False,
                query=query,
            )
        )

    queries = (
        "explain UMAP",
        "run sc-de on /tmp/x.h5ad",  # file-path rule
        "extract GEO accession from /tmp/paper.pdf",  # pdf rule
        "search the web for deconvolution methods",  # (web is a tool, not a layer)
        "请记住我喜欢 DESeq2",  # memory-hygiene rule
        "implement a custom QC step in python",  # implementation rule
    )
    systems = {q: _assemble(q).system_prompt for q in queries}
    baseline = systems[queries[0]]
    for q, system_prompt in systems.items():
        assert system_prompt == baseline, (
            f"system prefix drifted for query {q!r} — a query-volatile layer "
            f"leaked into the system prefix and will break the cache."
        )

    # And the volatile rule genuinely appeared — in the message, not the system.
    file_turn = _assemble("run sc-de on /tmp/x.h5ad")
    assert "## File Path Discipline" in file_turn.message_context
    assert "## File Path Discipline" not in file_turn.system_prompt


def test_system_prompt_byte_stable_with_knowhow_and_scoped_memory():
    """ADR 0024 — knowhow_constraints (query/skill/domain-matched) and
    scoped_memory_context (query-RANKED) are Volatile context. Even with knowhow
    ON and scoped memory present, the system prefix must stay byte-identical
    across queries. This catches a query-varying layer wrongly left in the system
    tier: it fails if either layer is placement="system" (the original Phase 2
    mis-classification the adversarial review found)."""

    def _assemble(query: str, skill: str, domain: str):
        return assemble_prompt_context(
            request=ContextAssemblyRequest(
                surface="bot",
                base_persona="BASE PERSONA",
                query=query,
                skill=skill,
                domain=domain,
                scoped_memory_context="## Workspace Notes\n\n- prefer leiden clustering",
                include_knowhow=True,
                # Content keyed on skill/domain so it genuinely varies per turn —
                # were this layer in the system tier, the prefix would drift.
                knowhow_loader=lambda **kw: (
                    f"⚠️ CONSTRAINTS skill={kw.get('skill')} "
                    f"domain={kw.get('domain')}: validate QC first"
                ),
            )
        )

    a = _assemble("run sc-de on /tmp/x.h5ad", "sc-de", "singlecell")
    b = _assemble("do enrichment analysis", "sc-enrichment", "singlecell")
    c = _assemble("explain UMAP", "", "")

    assert a.system_prompt == b.system_prompt == c.system_prompt, (
        "system prefix drifted across query/skill/domain — a query-varying layer "
        "(knowhow_constraints or scoped_memory_context) leaked into the system tier."
    )
    # The volatile knowhow + scoped-memory content rides the user turn, not system.
    assert "CONSTRAINTS skill=sc-de" in a.message_context
    assert "Workspace Notes" in a.message_context
    assert "CONSTRAINTS" not in a.system_prompt
    assert "Workspace Notes" not in a.system_prompt


def test_assemble_prompt_context_routes_transcript_context_to_message_layer():
    assembly = assemble_prompt_context(
        request=ContextAssemblyRequest(
            surface="interactive",
            base_persona="BASE PERSONA",
            transcript_context="## Selective Transcript Replay\n\n- omitted older refs",
            include_knowhow=False,
        )
    )

    assert "transcript_context" in assembly.layer_stats
    assert assembly.layer_stats["transcript_context"]["placement"] == "message"
    assert "## Selective Transcript Replay" in assembly.message_context


def test_assemble_prompt_context_includes_active_prompt_pack_layer(tmp_path):
    prompt_pack = tmp_path / "installed_extensions" / "prompt-packs" / "local-rules"
    prompt_pack.mkdir(parents=True)
    (prompt_pack / "rules.md").write_text(
        "Use concise scientific tone.\nPreserve exact output paths.\n",
        encoding="utf-8",
    )
    (prompt_pack / "omicsclaw-extension.json").write_text(
        json.dumps(
            {
                "name": "local-rules",
                "version": "1.0.0",
                "type": "prompt-pack",
                "entrypoints": ["rules.md"],
                "trusted_capabilities": ["prompt-rules"],
            }
        ),
        encoding="utf-8",
    )
    write_install_record(
        prompt_pack,
        extension_name="local-rules",
        source_kind="local",
        source="/tmp/local-rules",
        extension_type="prompt-pack",
        relative_install_path="installed_extensions/prompt-packs/local-rules",
    )
    write_extension_state(prompt_pack, enabled=True)

    assembly = assemble_prompt_context(
        request=ContextAssemblyRequest(
            surface="interactive",
            omicsclaw_dir=str(tmp_path),
            base_persona="BASE PERSONA",
            include_knowhow=False,
        )
    )

    assert "extension_prompt_packs" in assembly.layer_stats
    assert "## Active Local Prompt Packs" in assembly.system_prompt
    assert "Use concise scientific tone." in assembly.system_prompt
    assert assembly.layer_stats["extension_prompt_packs"]["placement"] == "system"


def test_assemble_chat_context_forwards_prompt_pack_context_to_system_prompt(tmp_path):
    prompt_pack = tmp_path / "installed_extensions" / "prompt-packs" / "local-rules"
    prompt_pack.mkdir(parents=True)
    (prompt_pack / "rules.md").write_text(
        "Prefer exact status summaries.\n",
        encoding="utf-8",
    )
    (prompt_pack / "omicsclaw-extension.json").write_text(
        json.dumps(
            {
                "name": "local-rules",
                "version": "1.0.0",
                "type": "prompt-pack",
                "entrypoints": ["rules.md"],
                "trusted_capabilities": ["prompt-rules"],
            }
        ),
        encoding="utf-8",
    )
    write_install_record(
        prompt_pack,
        extension_name="local-rules",
        source_kind="local",
        source="/tmp/local-rules",
        extension_type="prompt-pack",
        relative_install_path="installed_extensions/prompt-packs/local-rules",
    )
    write_extension_state(prompt_pack, enabled=True)

    context = asyncio.run(
        assemble_chat_context(
            chat_id="chat-pack",
            user_content="hello there",
            user_id="user-pack",
            platform="cli",
            omicsclaw_dir=str(tmp_path),
        )
    )

    assert context.prompt_context.request.omicsclaw_dir == str(tmp_path)
    # extension_prompt_packs is a SYSTEM layer: it renders into the system prompt.
    assert "## Active Local Prompt Packs" in context.system_prompt
    assert "Prefer exact status summaries." in context.system_prompt


def test_assemble_chat_context_cancels_pending_background_tasks():
    # F9: when the assembly coroutine is cancelled mid-flight, it must reap its
    # in-flight background tasks (the cancellable session-memory coroutine in
    # particular) via a finally, rather than leaking them to run detached.
    async def _scenario():
        started = asyncio.Event()
        cancelled = asyncio.Event()

        class _BlockingSessionManager:
            async def get_or_create(self, *args, **kwargs):
                return None

            async def load_context(self, *args, **kwargs):
                started.set()
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                return "mem"  # pragma: no cover

        task = asyncio.create_task(
            assemble_chat_context(
                chat_id="c",
                user_content="hello",
                user_id="u",
                platform="cli",
                session_manager=_BlockingSessionManager(),
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert cancelled.is_set(), "pending memory task was not cancelled by cleanup"

    asyncio.run(_scenario())


def test_assemble_chat_context_reaps_non_awaited_task_on_cancel():
    # F9 (the real leak): a background task created but NOT the one currently being
    # awaited when cancellation hits is orphaned (asyncio only auto-cancels the
    # awaited fut_waiter). Here capability_task is spawned, then we cancel while
    # parked at `await memory_task`; the finally must reap the pending capability
    # task so no orphaned Task is left in the loop.
    import threading

    async def _scenario():
        started_mem = asyncio.Event()
        cap_entered = threading.Event()

        def _slow_capability(query, *, domain_hint=""):
            cap_entered.set()
            threading.Event().wait(1.0)  # bounded so loop teardown can't hang

            class _Decision:
                chosen_skill = ""
                domain = ""
                skill_candidates = ()

                def to_prompt_block(self):
                    return ""

            return _Decision()

        class _BlockingSessionManager:
            async def get_or_create(self, *args, **kwargs):
                return None

            async def load_context(self, *args, **kwargs):
                started_mem.set()
                await asyncio.sleep(3600)

        task = asyncio.create_task(
            assemble_chat_context(
                chat_id="c",
                user_content="run differential expression analysis",  # triggers capability
                user_id="u",
                platform="cli",
                skill_aliases=("noop",),  # avoid loading the real registry
                session_manager=_BlockingSessionManager(),
                capability_resolver=_slow_capability,
            )
        )
        await asyncio.wait_for(started_mem.wait(), timeout=2)
        await asyncio.to_thread(cap_entered.wait, 2)  # capability thread is running
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        leftover = [
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        assert leftover == [], f"leaked pending background tasks: {leftover}"

    asyncio.run(_scenario())


def test_volatile_memory_does_not_churn_system_prefix():
    # Decision-2 placement split: within a session, evolving memory (dataset /
    # analysis / insight) is written between turns, so load_context returns updated
    # content. Those volatile blocks must ride the MESSAGE layer, not the system
    # prefix — otherwise every memory write re-warms the ADR 0024 prefix. Durable
    # identity (preferences / project_context) stays in the cache-warm system layer.
    _STABLE = "**User Preferences**:\n- lang: zh"

    def _sm(volatile):
        class _SM:
            async def get_or_create(self, *a, **k):
                return None

            async def load_context_layers(self, session_id, thread_id=""):
                return _STABLE, volatile

            async def load_context(self, session_id, thread_id=""):
                return "\n".join(p for p in (_STABLE, volatile) if p)

        return _SM()

    def _turn(volatile):
        return asyncio.run(
            assemble_chat_context(
                chat_id="c",
                user_content="continue",
                user_id="u",
                platform="cli",
                session_manager=_sm(volatile),
            )
        )

    t1 = _turn("**Recent Analyses**:\n1. sc-de (wilcoxon) - running")
    t2 = _turn("**Recent Analyses**:\n1. sc-de (wilcoxon) - complete")

    # The volatile analysis-status change must NOT alter the system prefix.
    assert t1.system_prompt == t2.system_prompt
    # Durable identity stays in system; volatile work-state does not.
    assert "**User Preferences**" in t1.system_prompt
    assert "sc-de" not in t1.system_prompt
    # Volatile work-state rides the message layer instead.
    assert "sc-de" in t2.prompt_context.message_context
