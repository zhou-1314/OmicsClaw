"""AN-PROV-CAPTURE-13 (Bench Phase 4, ADR 0022) — the provenance index.

A successful run's AnalysisMemory now carries the effective params, input
checksum, skill version, output artifact names, and the assisted-parameterization
decision (recommendation vs effective), so the Write phase has a queryable,
memory-resident provenance record. These tests drive the capture helpers in
``omicsclaw.skill.orchestration`` against a real ``CompatMemoryStore`` and unit-test
the recompute-at-capture decision logic.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from omicsclaw.memory.compat import CompatMemoryStore


@pytest_asyncio.fixture
async def store(tmp_path):
    store = CompatMemoryStore(database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await store.initialize()
    yield store


# --------------------------------------------------------------------------- #
# _assisted_param_decision — recompute-at-capture comparison (pure logic)      #
# --------------------------------------------------------------------------- #

def test_assisted_param_decision_none_without_hints(monkeypatch):
    from omicsclaw.skill import orchestration

    monkeypatch.setattr(orchestration, "_resolve_param_hint_info", lambda s, m: (m, {}, {}))
    assert orchestration._assisted_param_decision("sk", "meth", {"x": 1}) is None


def test_assisted_param_decision_none_when_hints_have_no_defaults(monkeypatch):
    from omicsclaw.skill import orchestration

    monkeypatch.setattr(
        orchestration, "_resolve_param_hint_info",
        lambda s, m: (m, {"params": ["a"], "defaults": {}}, {}),
    )
    assert orchestration._assisted_param_decision("sk", "meth", {"a": 1}) is None


def test_assisted_param_decision_accepted_when_effective_matches(monkeypatch):
    from omicsclaw.skill import orchestration

    monkeypatch.setattr(
        orchestration, "_resolve_param_hint_info",
        lambda s, m: (m, {"params": ["a", "b"], "defaults": {"a": 10, "b": 20}}, {}),
    )
    d = orchestration._assisted_param_decision("sk", "meth", {"a": 10, "b": 20, "c": 99})
    assert d["accepted"] is True
    assert d["overrides"] == {}
    assert d["recommended"] == {"a": 10, "b": 20}
    assert d["method"] == "meth"


def test_assisted_param_decision_flags_override(monkeypatch):
    from omicsclaw.skill import orchestration

    monkeypatch.setattr(
        orchestration, "_resolve_param_hint_info",
        lambda s, m: (m, {"params": ["a", "b"], "defaults": {"a": 10, "b": 20}}, {}),
    )
    d = orchestration._assisted_param_decision("sk", "meth", {"a": 10, "b": 999})
    assert d["accepted"] is False
    assert d["overrides"] == {"b": {"recommended": 20, "effective": 999}}


def test_assisted_param_decision_recommended_absent_from_effective_counts_as_accepted(monkeypatch):
    """A recommended param the run never reported is NOT an override — the skill
    used its own default (which is the recommendation), so it stays 'accepted'."""
    from omicsclaw.skill import orchestration

    monkeypatch.setattr(
        orchestration, "_resolve_param_hint_info",
        lambda s, m: (m, {"params": ["a", "b"], "defaults": {"a": 10, "b": 20}}, {}),
    )
    d = orchestration._assisted_param_decision("sk", "meth", {"a": 10})  # 'b' absent
    assert d["accepted"] is True
    assert d["overrides"] == {}


def test_assisted_param_decision_tolerates_yaml_json_numeric_drift(monkeypatch):
    """The SKILL.md default (YAML) vs the run's effective param (JSON) can drift in
    type — int 7 vs float 7.0, str "0.05" vs float 0.05 — and must NOT read as an
    override. A genuinely different value still does."""
    from omicsclaw.skill import orchestration

    monkeypatch.setattr(
        orchestration, "_resolve_param_hint_info",
        lambda s, m: (m, {"params": ["n", "cut"], "defaults": {"n": 7, "cut": "0.05"}}, {}),
    )
    d = orchestration._assisted_param_decision("sk", "meth", {"n": 7.0, "cut": 0.05})
    assert d["accepted"] is True, d
    assert d["overrides"] == {}

    d2 = orchestration._assisted_param_decision("sk", "meth", {"n": 8, "cut": 0.05})
    assert d2["accepted"] is False
    assert set(d2["overrides"]) == {"n"}


def test_param_values_equal_unit():
    from omicsclaw.skill.orchestration import _param_values_equal

    assert _param_values_equal(7, 7.0) is True
    assert _param_values_equal("0.05", 0.05) is True
    assert _param_values_equal("leiden", "leiden") is True
    assert _param_values_equal(7, 8) is False
    assert _param_values_equal("a", "b") is False
    assert _param_values_equal(None, None) is True
    assert _param_values_equal(None, "x") is False


def test_output_artifact_names_caps_large_dir(tmp_path):
    from omicsclaw.skill.orchestration import _MAX_ARTIFACT_NAMES, _output_artifact_names

    d = tmp_path / "big"
    d.mkdir()
    for i in range(_MAX_ARTIFACT_NAMES + 5):
        (d / f"f{i:04d}.txt").write_text("x", encoding="utf-8")
    names = _output_artifact_names(d)
    assert len(names) == _MAX_ARTIFACT_NAMES + 1  # cap + 1 truncation marker
    assert "more files" in names[-1]


# --------------------------------------------------------------------------- #
# _auto_capture_analysis — provenance from result.json                         #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_auto_capture_analysis_records_provenance(store, tmp_path, monkeypatch):
    import omicsclaw.runtime.agent.state as _state
    from omicsclaw.skill import orchestration

    monkeypatch.setattr(_state, "memory_store", store, raising=False)
    monkeypatch.setattr(
        orchestration, "_resolve_param_hint_info",
        lambda s, m: (m, {"params": ["padj_cutoff"], "defaults": {"padj_cutoff": 0.05}}, {}),
    )
    session = await store.create_session("u", "telegram")
    sid = session.session_id

    out_dir = tmp_path / "run-de"
    out_dir.mkdir()
    (out_dir / "result.json").write_text(json.dumps({
        "skill": "bulkrna-de", "version": "0.5.0", "input_checksum": "abc123",
        "data": {"params": {"method": "deseq2", "padj_cutoff": 0.05, "lfc_cutoff": 1.0}},
    }), encoding="utf-8")
    (out_dir / "de_results.csv").write_text("gene,padj\n", encoding="utf-8")

    await orchestration._auto_capture_analysis(
        sid, "bulkrna-de", {"method": "deseq2", "file_path": "x.csv"}, out_dir, True, thread_id="t-A"
    )

    recs = await store.get_memories(sid, "analysis", thread_id="t-A")
    assert len(recs) == 1
    rec = recs[0]
    assert rec.skill_version == "0.5.0"
    assert rec.input_checksum == "abc123"
    assert rec.effective_params["method"] == "deseq2"
    assert rec.effective_params["padj_cutoff"] == 0.05
    assert "result.json" in rec.artifacts and "de_results.csv" in rec.artifacts
    # effective matched the recommended default → accepted.
    assert rec.assisted_param_decision is not None
    assert rec.assisted_param_decision["accepted"] is True
    assert rec.assisted_param_decision["recommended"] == {"padj_cutoff": 0.05}


@pytest.mark.asyncio
async def test_auto_capture_analysis_decision_records_override(store, tmp_path, monkeypatch):
    import omicsclaw.runtime.agent.state as _state
    from omicsclaw.skill import orchestration

    monkeypatch.setattr(_state, "memory_store", store, raising=False)
    monkeypatch.setattr(
        orchestration, "_resolve_param_hint_info",
        lambda s, m: (m, {"params": ["padj_cutoff"], "defaults": {"padj_cutoff": 0.05}}, {}),
    )
    session = await store.create_session("u", "telegram")
    sid = session.session_id

    out_dir = tmp_path / "run-de2"
    out_dir.mkdir()
    (out_dir / "result.json").write_text(json.dumps({
        "version": "0.5.0", "input_checksum": "", "data": {"params": {"padj_cutoff": 0.01}},
    }), encoding="utf-8")

    await orchestration._auto_capture_analysis(
        sid, "bulkrna-de", {"method": "deseq2"}, out_dir, True, thread_id="t-A"
    )
    rec = (await store.get_memories(sid, "analysis", thread_id="t-A"))[0]
    assert rec.assisted_param_decision["accepted"] is False
    assert rec.assisted_param_decision["overrides"] == {
        "padj_cutoff": {"recommended": 0.05, "effective": 0.01}
    }


@pytest.mark.asyncio
async def test_auto_capture_analysis_failed_run_has_no_provenance(store, tmp_path, monkeypatch):
    """A failed run keeps the legacy lightweight record: no effective params,
    no decision, no artifacts, status=failed."""
    import omicsclaw.runtime.agent.state as _state
    from omicsclaw.skill import orchestration

    monkeypatch.setattr(_state, "memory_store", store, raising=False)
    session = await store.create_session("u", "telegram")
    sid = session.session_id

    out_dir = tmp_path / "run-fail"
    out_dir.mkdir()
    (out_dir / "result.json").write_text(json.dumps({"version": "9", "data": {"params": {"x": 1}}}), encoding="utf-8")

    await orchestration._auto_capture_analysis(
        sid, "bulkrna-de", {"method": "deseq2"}, out_dir, False, thread_id="t-A"
    )
    rec = (await store.get_memories(sid, "analysis", thread_id="t-A"))[0]
    assert rec.status == "failed"
    assert rec.effective_params == {}
    assert rec.skill_version == ""
    assert rec.artifacts == []
    assert rec.assisted_param_decision is None


@pytest.mark.asyncio
async def test_auto_capture_analysis_success_without_result_json(store, tmp_path, monkeypatch):
    """A successful run whose dir has NO result.json: effective_params stays empty
    (no override detectable), but artifacts are still listed and the record persists."""
    import omicsclaw.runtime.agent.state as _state
    from omicsclaw.skill import orchestration

    monkeypatch.setattr(_state, "memory_store", store, raising=False)
    monkeypatch.setattr(
        orchestration, "_resolve_param_hint_info",
        lambda s, m: (m, {"params": ["padj_cutoff"], "defaults": {"padj_cutoff": 0.05}}, {}),
    )
    session = await store.create_session("u", "telegram")
    sid = session.session_id

    out_dir = tmp_path / "run-noresult"
    out_dir.mkdir()
    (out_dir / "report.md").write_text("# report\n", encoding="utf-8")

    await orchestration._auto_capture_analysis(
        sid, "bulkrna-de", {"method": "deseq2"}, out_dir, True, thread_id="t-A"
    )
    rec = (await store.get_memories(sid, "analysis", thread_id="t-A"))[0]
    assert rec.effective_params == {}
    assert rec.skill_version == ""
    assert "report.md" in rec.artifacts
    # recommendation exists but effective is empty → nothing diverges → accepted.
    assert rec.assisted_param_decision["accepted"] is True
    assert rec.assisted_param_decision["overrides"] == {}


@pytest.mark.asyncio
async def test_auto_capture_analysis_malformed_result_json_params(store, tmp_path, monkeypatch):
    """result.json present but data.params is not a dict → effective_params stays
    empty (guarded), version/checksum still captured."""
    import omicsclaw.runtime.agent.state as _state
    from omicsclaw.skill import orchestration

    monkeypatch.setattr(_state, "memory_store", store, raising=False)
    session = await store.create_session("u", "telegram")
    sid = session.session_id

    out_dir = tmp_path / "run-malformed"
    out_dir.mkdir()
    (out_dir / "result.json").write_text(json.dumps({
        "version": "1.2", "input_checksum": "deadbeef", "data": {"params": "not-a-dict"},
    }), encoding="utf-8")

    await orchestration._auto_capture_analysis(
        sid, "bulkrna-de", {"method": "deseq2"}, out_dir, True, thread_id="t-A"
    )
    rec = (await store.get_memories(sid, "analysis", thread_id="t-A"))[0]
    assert rec.effective_params == {}
    assert rec.skill_version == "1.2"
    assert rec.input_checksum == "deadbeef"


# --------------------------------------------------------------------------- #
# _auto_capture_consensus — effective config from plan.json                    #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_auto_capture_consensus_records_plan_as_effective_params(store, tmp_path, monkeypatch):
    """The typed-consensus lineage record carries the plan audit (operator +
    members + score weights) as effective_params and lists artifacts; a
    planner-driven flavour has no param-hint recommendation, so the decision is None."""
    import omicsclaw.runtime.agent.state as _state
    from omicsclaw.skill import orchestration

    monkeypatch.setattr(_state, "memory_store", store, raising=False)
    session = await store.create_session("u", "telegram")
    sid = session.session_id

    out_dir = tmp_path / "run-cons"
    out_dir.mkdir()
    (out_dir / "plan.json").write_text(json.dumps({
        "run_id": "run-cons", "operator": "weighted", "alpha": 1.0, "beta": 0.5,
        "members": [{"name": "leiden_r1"}, {"name": "leiden_r2"}],
    }), encoding="utf-8")
    (out_dir / "consensus_labels.tsv").write_text("obs\tlabel\n", encoding="utf-8")

    captured = await orchestration._auto_capture_consensus(
        sid, "consensus-domains", out_dir, True, thread_id="t-A"
    )
    assert captured is True
    rec = (await store.get_memories(sid, "analysis", thread_id="t-A"))[0]
    assert rec.effective_params["operator"] == "weighted"
    assert rec.effective_params["alpha"] == 1.0
    assert [m["name"] for m in rec.effective_params["members"]] == ["leiden_r1", "leiden_r2"]
    assert "plan.json" in rec.artifacts and "consensus_labels.tsv" in rec.artifacts
    assert rec.assisted_param_decision is None
