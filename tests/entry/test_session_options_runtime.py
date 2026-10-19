"""End-to-end runtime tests for the six OmicsClaw session options."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Sequence

import pytest

from omicsclaw.context import PromptAssembler
from omicsclaw.entry.assembly import build_app, build_prompt, default_sections
from omicsclaw.entry.compaction import build_compactor
from omicsclaw.entry.config import AppConfig
from omicsclaw.entry.desktop.chat_service import _maybe_fire_shadow_review
from omicsclaw.entry.memory import (
    MEMORY_SEARCH_TOOL_NAME,
    MEMORY_WRITE_TOOL_NAME,
    PrecisRefreshingExtractor,
    memory_search_tool,
    memory_section,
    memory_write_tool,
    open_memory,
)
from omicsclaw.entry.nudges import (
     _memory_nudge,
    _precis_block,
    _shadow_review_nudge,
    build_augmentor,
)
from omicsclaw.entry.session import InMemorySessionStore, SessionRegistry
from omicsclaw.entry.session_options import (
    REVIEWER_MODEL_ALIASES,
    SPECIALIST_PROFILES,
    SessionOptionsState,
    bind_reviewer_provider,
    clear_session_options_ledger,
    compute_session_option_transitions,
    record_session_options_turn,
    resolve_reviewer_model,
    resolve_specialist_profile,
)
from omicsclaw.entry.shadow_review import (
    ShadowReview,
    ShadowReviewVerdict,
)
from omicsclaw.entry.ssh.model_bridge import ModelSnapshot, RpcTransport
from omicsclaw.entry.subagent import (
    GENERAL_PURPOSE,
    MODULE_REVIEWER,
    ChildRunner,
)
from omicsclaw.entry.turn import run_turn
from omicsclaw.memory.artifacts import ArtifactRecord, ArtifactStore
from omicsclaw.memory.database import Database
from omicsclaw.provider import Completion, ProviderDialect
from omicsclaw.schema import Message, Role, StreamChunk, StreamChunkType, Usage
from omicsclaw.skills import SkillIndex
from omicsclaw.tools import FunctionTool, ToolRegistry
from omicsclaw.tools.context import use_tool_context
from omicsclaw.tools.function_tool import ToolArgumentError


@dataclass
class _RecordingProvider:
    """Protocol-clean provider double that records calls and bound overrides."""

    name: str = "recording"
    _reply_text: str = "completed analysis"
    _bound_model: str = ""
    _binds: list[dict[str, Any]] = field(default_factory=list)
    _calls: list[list[Message]] = field(default_factory=list)
    _client_factory: Any = None

    def bind(self, **overrides: Any) -> "_RecordingProvider":
        self._binds.append(dict(overrides))
        return replace(
            self,
            _bound_model=str(overrides.get("model") or self._bound_model),
        )

    async def generate(
        self, messages: Sequence[Message], tools: Any = None
    ) -> Completion:
        self._calls.append(list(messages))
        return Completion(
            message=Message(role=Role.ASSISTANT, content=self._reply_text),
            usage=Usage(input_tokens=10, output_tokens=5),
        )

    async def _stream(
        self, messages: Sequence[Message], tools: Any = None
    ) -> AsyncIterator[StreamChunk]:
        completion = await self.generate(messages, tools)
        yield StreamChunk(
            type=StreamChunkType.DONE,
            message=completion.message,
            finish_reason=completion.finish_reason,
        )

    def generate_stream(self, messages: Sequence[Message], tools: Any = None):
        return self._stream(messages, tools)


def _make_artifact(index: int, *, session_id: str = "s1") -> ArtifactRecord:
    return ArtifactRecord(
        id=f"art-{index}",
        job_id="job-1",
        session_id=session_id,
        kind="table",
        path=f"/tmp/out_{index}.csv",
        title=f"Table {index}",
        mime="text/csv",
        sha256=f"{index:064x}",
        size=128,
        produced_by="save_artifact",
        meta={},
        created_at=float(1000 + index),
    )


# ---------------------------------------------------------------------------
# 1. ultra_mode
# ---------------------------------------------------------------------------


def test_ultra_mode_injects_directives_into_tools_and_planning_sections(tmp_path: Path):
    config = AppConfig(workspace=tmp_path, planning=True)
    assembler = build_prompt(
        default_sections(config, skills=SkillIndex(), plan_tool=True)
    )

    baseline = assembler.render().system_prompt
    assert "Ultra Mode (ACTIVE)" not in baseline
    assert "Ultra Mode (OFF)" not in baseline

    with use_tool_context(values={"ultra_mode": True}):
        rendered_on = assembler.render().system_prompt
    assert "Ultra Mode (ACTIVE)" in rendered_on
    assert "Ultra Mode Decomposition Protocol" in rendered_on
    assert "general-purpose" in rendered_on

    with use_tool_context(values={"ultra_mode": False}):
        rendered_off = assembler.render().system_prompt
    assert "Ultra Mode (OFF)" in rendered_off
    assert "Direct Execution Protocol (Ultra Mode Off)" in rendered_off


def test_ultra_mode_gates_general_purpose_subagent_delegation(tmp_path: Path):
    parent = ToolRegistry()
    provider = _RecordingProvider(_reply_text="subagent conclusion")
    runner = ChildRunner(
        provider=provider,
        parent=parent,
        config=AppConfig(workspace=tmp_path),
    )

    # When ultra_mode=False, general-purpose delegation is short-circuited
    with use_tool_context(values={"ultra_mode": False}):
        blocked = asyncio.run(runner.delegate(GENERAL_PURPOSE, "investigate cluster 3"))
    assert "Delegation (ultra_mode) is currently disabled" in blocked
    assert len(provider._calls) == 0

    # When ultra_mode=True, general-purpose delegation executes normally
    with use_tool_context(values={"ultra_mode": True}):
        allowed = asyncio.run(runner.delegate(GENERAL_PURPOSE, "investigate cluster 3"))
    assert allowed == "subagent conclusion"
    assert len(provider._calls) == 1


# ---------------------------------------------------------------------------
# 2. verifier_mode
# ---------------------------------------------------------------------------


def test_verifier_mode_enables_shadow_review_when_global_config_is_off(tmp_path: Path):
    seen_models: list[str] = []

    async def fake_review(
        session_id: str,
        records: Sequence[ArtifactRecord],
        history: Sequence[Message],
        *,
        reviewer_model: str = "",
    ) -> ShadowReviewVerdict:
        seen_models.append(reviewer_model)
        return ShadowReviewVerdict("warn", "check batch effect covariate in DE design")

    app = build_app(
        AppConfig(workspace=tmp_path, shadow_review=False),
        provider=_RecordingProvider(),
        review_call=fake_review,
    )
    try:
        # Global shadow_review remains None when config.shadow_review=False
        assert app.shadow_review is None
        assert app.verifier_review is not None

        db = Database(tmp_path / "artifacts.db")
        store = ArtifactStore(db)
        app.verifier_review.bind_store(store)
        store.insert_artifact(_make_artifact(1, session_id="s-verify"))
        app.verifier_review.evaluate("s-verify")
        for idx in (2, 3, 4):
            store.insert_artifact(_make_artifact(idx, session_id="s-verify"))

        registry = SessionRegistry(app, store=InMemorySessionStore())

        async def run_check():
            # With verifier_mode=False, nothing fires
            none_task = _maybe_fire_shadow_review(
                app,
                registry,
                "s-verify",
                values={"verifier_mode": False},
            )
            assert none_task is None
            assert seen_models == []

            # With verifier_mode=True and reviewer_model="opus", standby verifier_review fires
            task = _maybe_fire_shadow_review(
                app,
                registry,
                "s-verify",
                values={"verifier_mode": True, "reviewer_model": "opus"},
            )
            assert task is not None
            await task
            assert seen_models == ["claude-opus-4-6"]

            # The queued verdict is delivered into the next turn's augmentor
            with use_tool_context(values={"verifier_mode": True}):
                augmentor = build_augmentor(app, session_id="s-verify")
                assert augmentor is not None
                messages = await augmentor.augment(())
            assert any(
                "check batch effect covariate" in m.content for m in messages
            )

        asyncio.run(run_check())
        db.close()
    finally:
        asyncio.run(app.aclose())


def test_verifier_mode_false_suppresses_shadow_review_when_global_config_is_on(
    tmp_path: Path,
):
    called = False

    async def fake_review(session_id, records, history):
        nonlocal called
        called = True
        return ShadowReviewVerdict("fail", "should not run")

    app = build_app(
        AppConfig(workspace=tmp_path, shadow_review=True),
        provider=_RecordingProvider(),
        review_call=fake_review,
    )
    try:
        assert app.shadow_review is not None
        db = Database(tmp_path / "artifacts2.db")
        store = ArtifactStore(db)
        app.shadow_review.bind_store(store)
        store.insert_artifact(_make_artifact(1, session_id="s-off"))
        app.shadow_review.evaluate("s-off")
        for idx in (2, 3, 4):
            store.insert_artifact(_make_artifact(idx, session_id="s-off"))

        registry = SessionRegistry(app, store=InMemorySessionStore())
        task = _maybe_fire_shadow_review(
            app, registry, "s-off", values={"verifier_mode": False}
        )
        assert task is None
        assert not called

        # Even if a note was queued earlier, verifier_mode=False suppresses the nudge
        app.shadow_review._notes["s-off"] = "queued warning"
        with use_tool_context(values={"verifier_mode": False}):
            assert _shadow_review_nudge(app, "s-off") is None
        db.close()
    finally:
        asyncio.run(app.aclose())


def test_verifier_mode_allows_desktop_module_reviewer_delegation(tmp_path: Path):
    parent = ToolRegistry()
    provider = _RecordingProvider(_reply_text="VERDICT: PASS - module validated")
    runner = ChildRunner(
        provider=provider,
        parent=parent,
        config=AppConfig(workspace=tmp_path),
    )

    # Desktop turn without module_review_requested or verifier_mode raises ToolArgumentError
    with use_tool_context(values={"surface": "desktop", "verifier_mode": False}):
        with pytest.raises(ToolArgumentError, match="Independent review is off"):
            asyncio.run(runner.delegate(MODULE_REVIEWER, "audit qc step"))

    # Desktop turn with verifier_mode=True allows module-reviewer without button flag
    with use_tool_context(
        values={
            "surface": "desktop",
            "verifier_mode": True,
            "reviewer_model": "sonnet",
        }
    ):
        verdict = asyncio.run(runner.delegate(MODULE_REVIEWER, "audit qc step"))
    assert "VERDICT: PASS" in verdict
    assert provider._binds[-1] == {"model": "claude-sonnet-4-6"}


# ---------------------------------------------------------------------------
# 3. reviewer_model
# ---------------------------------------------------------------------------


def test_reviewer_model_resolution_and_provider_binding(tmp_path: Path):
    assert resolve_reviewer_model("sonnet") == REVIEWER_MODEL_ALIASES["sonnet"]
    assert resolve_reviewer_model("Claude Sonnet 4.6") == "claude-sonnet-4-6"
    assert resolve_reviewer_model("DeepSeek R1") == "deepseek-reasoner"
    assert resolve_reviewer_model("GPT-4.1") == "gpt-4.1"
    assert resolve_reviewer_model("OPUS") == REVIEWER_MODEL_ALIASES["opus"]
    assert resolve_reviewer_model("Claude Opus 4.7") == "claude-opus-4-7"
    assert resolve_reviewer_model("haiku") == REVIEWER_MODEL_ALIASES["haiku"]
    assert resolve_reviewer_model("deepseek-reasoner") == "deepseek-reasoner"
    assert resolve_reviewer_model(None, fallback_model="gpt-5.4") == "gpt-5.4"
    assert resolve_reviewer_model("default", fallback_model="gpt-5.4") == "gpt-5.4"
    assert resolve_reviewer_model("Default", fallback_model="gpt-5.4") == "gpt-5.4"

    base_prov = _RecordingProvider()
    bound = bind_reviewer_provider(base_prov, "opus")
    assert bound._bound_model == "claude-opus-4-6"

    # ShadowReview._pick_provider uses reviewer_model override over config.shadow_review_model
    app = SimpleNamespace(
        config=AppConfig(workspace=tmp_path, shadow_review_model="claude-haiku-4-5-20251001"),
        provider=base_prov,
        provider_for_role=None,
    )
    review = ShadowReview(app)
    picked_default = review._pick_provider()
    assert picked_default._bound_model == "claude-haiku-4-5-20251001"

    picked_override = review._pick_provider(reviewer_model="sonnet")
    assert picked_override._bound_model == "claude-sonnet-4-6"


def test_reviewer_model_ssh_snapshot_allowlist_and_graceful_fallback():
    # 1. bind_reviewer_provider falls back to snapshot.model if target is not in allowed_models
    snapshot_narrow = ModelSnapshot(
        "deepseek",
        ProviderDialect.OPENAI_CHAT,
        "deepseek-chat",
        ("deepseek-chat",),
        128000,
        4096,
    )
    prov_narrow = _RecordingProvider(
        _client_factory=SimpleNamespace(snapshot=snapshot_narrow)
    )
    bound_fallback = bind_reviewer_provider(prov_narrow, "opus")
    assert bound_fallback._bound_model == "deepseek-chat"

    # 2. When snapshot.allowed_models includes preset models, alternate model binds directly
    snapshot_wide = ModelSnapshot(
        "deepseek",
        ProviderDialect.OPENAI_CHAT,
        "deepseek-chat",
        ("deepseek-chat", "deepseek-reasoner"),
        128000,
        4096,
    )
    prov_wide = _RecordingProvider(
        _client_factory=SimpleNamespace(snapshot=snapshot_wide)
    )
    bound_reasoner = bind_reviewer_provider(prov_wide, "deepseek-reasoner")
    assert bound_reasoner._bound_model == "deepseek-reasoner"
    bound_r1_label = bind_reviewer_provider(prov_wide, "DeepSeek R1")
    assert bound_r1_label._bound_model == "deepseek-reasoner"

    # 2b. Dated/prefixed allowlist entry matches UI preset label
    snapshot_dated = ModelSnapshot(
        "anthropic",
        ProviderDialect.OPENAI_CHAT,
        "claude-haiku-4-5-20251001",
        ("claude-haiku-4-5-20251001", "claude-sonnet-4-6-20250514"),
        200000,
        8192,
    )
    prov_dated = _RecordingProvider(
        _client_factory=SimpleNamespace(snapshot=snapshot_dated)
    )
    bound_dated = bind_reviewer_provider(prov_dated, "Claude Sonnet 4.6")
    assert bound_dated._bound_model == "claude-sonnet-4-6-20250514"

    # 3. RpcTransport.open falls back to snapshot.model for shadow_review/subagent roles
    calls: list[dict[str, Any]] = []

    class _Peer:
        async def call(self, method: str, params: dict[str, Any], **kwargs: Any):
            calls.append(params)
            return {"streamId": "s-1", "status": 200, "headers": {}}

    identity = {
        "controllerInstanceId": "c1",
        "epoch": "e1",
        "requestId": "r1",
        "sessionId": "sess1",
        "turnId": "t1",
    }
    bridge = RpcTransport(
        _Peer(), snapshot_narrow, role="shadow_review", identity=lambda: identity
    )
    request = SimpleNamespace(
        method="POST",
        url=SimpleNamespace(host="bridge.invalid", path="/v1/chat/completions"),
    )

    async def aread():
        return json.dumps({"model": "claude-opus-4-6"}).encode("utf-8")

    request.aread = aread
    asyncio.run(bridge.open(request))
    assert calls[0]["body"]["model"] == "deepseek-chat"


# ---------------------------------------------------------------------------
# 4. memory_mode
# ---------------------------------------------------------------------------


def test_memory_mode_gates_section_nudge_tools_subagent_and_compaction(tmp_path: Path):
    config = AppConfig(
        workspace=tmp_path,
        memory=True,
        memory_nudge_turns=1,
        precis_via_augmentor=True,
    )
    binding = open_memory(config)
    assert binding is not None
    try:
        search_tool = memory_search_tool(binding)
        write_tool = memory_write_tool(binding)

        # Write a memory entry while memory_mode=True
        with use_tool_context(values={"memory_mode": True}):
            write_res = asyncio.run(
                write_tool.execute(
                    json.dumps(
                        {
                            "action": "add",
                            "title": "QC threshold",
                            "content": "Use min_genes=200 and pct_counts_mt < 10%.",
                            "category": "preference",
                            "importance": 9,
                        }
                    )
                )
            )
            assert "QC threshold" in write_res or '"action": "add"' in write_res
            sec_on = memory_section(binding).source()
            assert "min_genes=200" in sec_on

        # With memory_mode=False:
        # (a) memory_section returns ""
        # (b) memory_search and memory_write return disabled notice without touching store
        with use_tool_context(values={"memory_mode": False}):
            assert memory_section(binding).source() == ""
            blocked_search = asyncio.run(
                search_tool.execute(json.dumps({"query": "min_genes"}))
            )
            assert "Memory is disabled for this session" in blocked_search
            blocked_write = asyncio.run(
                write_tool.execute(
                    json.dumps(
                        {
                            "action": "add",
                            "content": "Should not be stored",
                        }
                    )
                )
            )
            assert "Memory is disabled for this session" in blocked_write

        # Verify "Should not be stored" was never written
        with use_tool_context(values={"memory_mode": True}):
            found = asyncio.run(
                search_tool.execute(json.dumps({"query": "Should not be stored"}))
            )
            assert found == "[]"

        # (c) _memory_nudge, _precis_block, and build_compactor respect memory_mode=False
        app = build_app(config, provider=_RecordingProvider())
        try:
            with use_tool_context(values={"memory_mode": False}):
                assert _memory_nudge(app) is None
                assert _precis_block(app) is None
                compactor = build_compactor(app, session_id="s-mem")
                assert compactor._extractor is None

                # PrecisRefreshingExtractor also short-circuits if called directly
                extractor_called = False

                class _StubExtractor:
                    async def extract(self, messages):
                        nonlocal extractor_called
                        extractor_called = True

                refreshing = PrecisRefreshingExtractor(
                    _StubExtractor(), binding.precis  # type: ignore[arg-type]
                )
                ext_res = asyncio.run(
                    refreshing.extract([Message(role=Role.USER, content="hi")])
                )
                assert ext_res.stored == ()
                assert not extractor_called

                # (d) ChildRunner._child_registry strips memory_search & memory_write
                parent_reg = ToolRegistry()
                parent_reg.register(search_tool)
                parent_reg.register(write_tool)
                parent_reg.register(
                    FunctionTool("bash", "run bash", lambda: "ok", parameters={"type": "object"})
                )
                child_runner = ChildRunner(
                    provider=_RecordingProvider(),
                    parent=parent_reg,
                    config=config,
                )
                child_reg = child_runner._child_registry(GENERAL_PURPOSE)
                assert MEMORY_SEARCH_TOOL_NAME not in child_reg.names()
                assert MEMORY_WRITE_TOOL_NAME not in child_reg.names()
                assert "bash" in child_reg.names()
        finally:
            asyncio.run(app.aclose())
    finally:
        binding.close()


# ---------------------------------------------------------------------------
# 5. specialist & 6. reasoning_effort
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("specialist_key", "expected_snippet"),
    [
        ("scanpy", "Active Specialist Profile: Scanpy"),
        ("seurat", "Active Specialist Profile: Seurat"),
        ("bioconductor", "Active Specialist Profile: Bioconductor"),
        ("spatial", "Active Specialist Profile: Spatial Omics"),
        ("multiomics", "Active Specialist Profile: Multi-Omics Integration"),
        ("Single-Cell Omics", "Active Specialist Profile: Single-Cell Omics"),
        ("Spatial Transcriptomics", "Active Specialist Profile: Spatial Transcriptomics"),
        ("Multi-Omics Integration", "Active Specialist Profile: Multi-Omics Integration"),
        ("custom-proteomics", "Active Specialist Profile: custom-proteomics"),
    ],
)
def test_specialist_and_reasoning_effort_render_in_planning_section(
    tmp_path: Path, specialist_key: str, expected_snippet: str
):
    assert {
        "scanpy",
        "seurat",
        "bioconductor",
        "spatial",
        "multiomics",
        "single-cell omics",
        "spatial transcriptomics",
        "multi-omics integration",
    } <= set(SPECIALIST_PROFILES)
    assert resolve_specialist_profile(None) == ""
    assert resolve_specialist_profile("none") == ""

    config = AppConfig(workspace=tmp_path, planning=True)
    assembler = build_prompt(
        default_sections(config, skills=SkillIndex(), plan_tool=True)
    )
    with use_tool_context(
        values={"specialist": specialist_key, "reasoning_effort": "max"}
    ):
        rendered = assembler.render().system_prompt

    assert expected_snippet in rendered
    assert "Reasoning Effort (max)" in rendered


# ---------------------------------------------------------------------------
# 7. Mid-session option transitions & end-to-end turns
# ---------------------------------------------------------------------------


def test_mid_session_option_transition_injects_system_reminder_on_change(
    tmp_path: Path,
):
    clear_session_options_ledger()
    provider = _RecordingProvider(_reply_text="step done")
    app = build_app(
        AppConfig(workspace=tmp_path, planning=True),
        provider=provider,
    )
    try:
        # Turn 1: initial options on session "sess-trans" -> no transition reminder
        outcome1 = asyncio.run(
            run_turn(
                app,
                (),
                "start scanpy QC",
                session_id="sess-trans",
                values={
                    "ultra_mode": True,
                    "verifier_mode": False,
                    "specialist": "scanpy",
                },
            )
        )
        assert "Ultra Mode (ACTIVE)" in outcome1.prompt.system_prompt
        assert "Active Specialist Profile: Scanpy" in outcome1.prompt.system_prompt
        first_call_msgs = provider._calls[-1]
        assert not any(
            "Session Options Updated Mid-Session" in (m.content or "")
            for m in first_call_msgs
        )

        # Turn 2: toggle ultra_mode->False, verifier_mode->True, specialist->seurat
        outcome2 = asyncio.run(
            run_turn(
                app,
                outcome1.history,
                "switch to Seurat integration",
                session_id="sess-trans",
                values={
                    "ultra_mode": False,
                    "verifier_mode": True,
                    "reviewer_model": "opus",
                    "specialist": "seurat",
                },
            )
        )
        assert "Ultra Mode (OFF)" in outcome2.prompt.system_prompt
        assert "Active Specialist Profile: Seurat" in outcome2.prompt.system_prompt
        second_call_msgs = provider._calls[-1]
        reminder_msgs = [
            m.content
            for m in second_call_msgs
            if "Session Options Updated Mid-Session" in (m.content or "")
        ]
        assert len(reminder_msgs) == 1
        assert "Ultra mode was turned OFF" in reminder_msgs[0]
        assert "Verifier mode (Auto-review) was turned ON" in reminder_msgs[0]
        assert "Specialist focus changed to `seurat`" in reminder_msgs[0]

        # Turn 3: identical options -> no transition reminder repeated
        asyncio.run(
            run_turn(
                app,
                outcome2.history,
                "continue Seurat clustering",
                session_id="sess-trans",
                values={
                    "ultra_mode": False,
                    "verifier_mode": True,
                    "reviewer_model": "opus",
                    "specialist": "seurat",
                },
            )
        )
        third_call_msgs = provider._calls[-1]
        assert not any(
            "Session Options Updated Mid-Session" in (m.content or "")
            for m in third_call_msgs
        )
    finally:
        clear_session_options_ledger()
        asyncio.run(app.aclose())


def test_session_registry_submit_persists_state_and_clear_resets_it(tmp_path: Path):
    clear_session_options_ledger()
    provider = _RecordingProvider(_reply_text="done")
    app = build_app(
        AppConfig(workspace=tmp_path, planning=True),
        provider=provider,
    )
    registry = SessionRegistry(app, store=InMemorySessionStore())
    try:
        async def drive():
            h1 = await registry.submit(
                "s-reg",
                "step 1",
                values={"ultra_mode": True, "memory_mode": True},
            )
            await h1.wait()
            sess = registry.session("s-reg")
            assert sess is not None
            assert sess.values.get("session_options_state") == {
                "ultra_mode": True,
                "memory_mode": True,
            }

            h2 = await registry.submit(
                "s-reg",
                "step 2",
                values={"ultra_mode": False, "memory_mode": False},
            )
            await h2.wait()
            second_msgs = provider._calls[-1]
            assert any(
                "Ultra mode was turned OFF" in (m.content or "")
                and "Memory mode was turned OFF" in (m.content or "")
                for m in second_msgs
            )

            hc = await registry.clear("s-reg")
            await hc.wait()
            assert hc.terminal == "converged"
            sess_after = registry.session("s-reg")
            assert sess_after is not None
            assert "session_options_state" not in sess_after.values

        asyncio.run(drive())
    finally:
        clear_session_options_ledger()
        asyncio.run(registry.shutdown(0.1))
        asyncio.run(app.aclose())


def test_transition_edge_cases_omitted_and_reset_to_defaults():
    # Turn 1: Single-Cell Omics + Claude Sonnet 4.6
    s1 = SessionOptionsState(
        ultra_mode=True,
        reviewer_model="Claude Sonnet 4.6",
        specialist="Single-Cell Omics",
    )
    # Turn 2: caller omits specialist and reviewer_model -> no false reset notes
    s2_omitted = SessionOptionsState(ultra_mode=True)
    assert compute_session_option_transitions(s1, s2_omitted) == ()

    # Turn 3: caller explicitly resets reviewer_model='Default' and specialist='None'
    s3_reset = SessionOptionsState(
        ultra_mode=True,
        reviewer_model="Default",
        specialist="None",
    )
    notes = compute_session_option_transitions(s1, s3_reset)
    assert any("Reviewer model reset to `default` (from `claude-sonnet-4-6`)" in n for n in notes)
    assert any("Specialist focus was cleared" in n for n in notes)

    # Turn 4: staying on Default / None emits no transition notes
    assert compute_session_option_transitions(s3_reset, s3_reset) == ()
