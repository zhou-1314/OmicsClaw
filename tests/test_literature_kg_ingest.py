"""Literature extraction converges onto the canonical KG ingest (audit D-1).

Before D-1 the literature skill produced regex metadata that never reached the
KG, so a literature run built ZERO graph and never grounded ideation. Now the
skill persists its parsed text (``source.txt``) and the backend ingests that one
artifact into the KG via the shared in-process bridge — so the paper becomes an
ideation/formalize-groundable Source.

These tests pin: (1) the shared bridge `kg_tools.ingest_source_into_kg`
soft-fails without KG/LLM and otherwise dispatches `cmd_ingest.ingest`; (2) the
backend post-run trigger `_ingest_literature_into_kg` ingests the persisted
source and is non-fatal; (3) the skill persists `source.txt` for every input.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import types
from pathlib import Path

# Import the agent state first so the state↔agent_executors↔kg_tools import cycle
# resolves in the right order (mirrors the other runtime-tools tests).
import omicsclaw.runtime.agent.state as _state  # noqa: F401
import omicsclaw.runtime.tools.kg_tools as kg_tools
from omicsclaw.runtime.tools.builders import agent_executors

ROOT = Path(__file__).resolve().parent.parent


def _run(coro):
    return asyncio.run(coro)


# --- the shared bridge: kg_tools.ingest_source_into_kg --------------------------


def test_ingest_source_into_kg_soft_fails_when_kg_absent(monkeypatch):
    monkeypatch.setattr(kg_tools, "_import_kg_ingest", lambda: None)
    assert _run(kg_tools.ingest_source_into_kg("/data/source.txt")) is None


def test_ingest_source_into_kg_soft_fails_without_llm(monkeypatch):
    monkeypatch.setattr(kg_tools, "_import_kg_ingest", lambda: (object(), object()))
    monkeypatch.setattr(kg_tools, "_build_kg_extractor", lambda: None)
    assert _run(kg_tools.ingest_source_into_kg("/data/source.txt")) is None


def test_ingest_source_into_kg_dispatches_cmd_ingest(monkeypatch):
    calls = {}
    fake_cmd = types.SimpleNamespace(
        ingest=lambda source, cfg, llm: calls.update(source=source, cfg=cfg, llm=llm)
        or {"status": "ingested", "slug": "s1"}
    )
    fake_cfg = types.SimpleNamespace(resolve=lambda home: f"cfg:{home}")
    monkeypatch.setattr(kg_tools, "_import_kg_ingest", lambda: (fake_cfg, fake_cmd))
    monkeypatch.setattr(kg_tools, "_build_kg_extractor", lambda: "EXTRACTOR")
    monkeypatch.setattr(kg_tools, "_resolve_kg_home", lambda: "/kg/home")

    res = _run(kg_tools.ingest_source_into_kg("/data/source.txt"))
    assert res == {"status": "ingested", "slug": "s1"}
    assert calls == {"source": "/data/source.txt", "cfg": "cfg:/kg/home", "llm": "EXTRACTOR"}


def test_ingest_source_into_kg_soft_fails_on_ingest_error(monkeypatch):
    def boom(source, cfg, llm):
        raise RuntimeError("ingest blew up")

    monkeypatch.setattr(kg_tools, "_import_kg_ingest", lambda: (types.SimpleNamespace(resolve=lambda h: h), types.SimpleNamespace(ingest=boom)))
    monkeypatch.setattr(kg_tools, "_build_kg_extractor", lambda: "EXTRACTOR")
    monkeypatch.setattr(kg_tools, "_resolve_kg_home", lambda: "/kg/home")
    assert _run(kg_tools.ingest_source_into_kg("/data/source.txt")) is None


# --- the backend post-run trigger: _ingest_literature_into_kg ------------------


def _write_result(out_dir: Path, data: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "result.json").write_text(json.dumps({"data": data}), encoding="utf-8")


def test_literature_trigger_ingests_persisted_source(monkeypatch, tmp_path):
    out = tmp_path / "lit"
    out.mkdir()
    src = out / "source.txt"
    src.write_text("paper body text", encoding="utf-8")
    _write_result(out, {"source_text_path": str(src), "source": {"type": "text", "value": "..."}})

    seen = {}

    async def fake_ingest(source):
        seen["source"] = source
        return {"status": "ingested"}

    monkeypatch.setattr(kg_tools, "ingest_source_into_kg", fake_ingest)
    _run(agent_executors._ingest_literature_into_kg(out))
    assert seen["source"] == str(src)


def test_literature_trigger_noop_without_source_text(monkeypatch, tmp_path):
    out = tmp_path / "lit"
    _write_result(out, {"metadata": {}})
    called = {"n": 0}

    async def fake_ingest(source):
        called["n"] += 1

    monkeypatch.setattr(kg_tools, "ingest_source_into_kg", fake_ingest)
    _run(agent_executors._ingest_literature_into_kg(out))
    assert called["n"] == 0


def test_literature_trigger_nonfatal_on_error(monkeypatch, tmp_path):
    out = tmp_path / "lit"
    out.mkdir()
    src = out / "source.txt"
    src.write_text("x", encoding="utf-8")
    _write_result(out, {"source_text_path": str(src)})

    async def boom(source):
        raise RuntimeError("kg down")

    monkeypatch.setattr(kg_tools, "ingest_source_into_kg", boom)
    # Must not raise — KG ingest is best-effort, never breaks the literature tool.
    _run(agent_executors._ingest_literature_into_kg(out))


def test_literature_trigger_logs_a_failed_ingest_result(monkeypatch, tmp_path, caplog):
    """A returned {"status":"failed"} from ingest must be surfaced (logged), not
    silently dropped by the best-effort literature hook."""
    import logging

    out = tmp_path / "lit"
    out.mkdir()
    src = out / "source.txt"
    src.write_text("x", encoding="utf-8")
    _write_result(out, {"source_text_path": str(src)})

    async def failed(source):
        return {"status": "failed", "reason": "bad json from model"}

    monkeypatch.setattr(kg_tools, "ingest_source_into_kg", failed)
    with caplog.at_level(logging.WARNING):
        _run(agent_executors._ingest_literature_into_kg(out))
    assert "bad json from model" in caplog.text


def test_fmt_ingest_surfaces_failed_results():
    """_fmt_ingest must report failed single + batch results, not 'completed'."""
    from omicsclaw.runtime.tools.kg_tools import _fmt_ingest

    single = _fmt_ingest({"status": "failed", "reason": "extraction failed: bad"}, "src.txt")
    assert "failed" in single.lower() and "bad" in single

    batch = _fmt_ingest(
        {
            "status": "batch_complete",
            "dir": "/d",
            "results": [{"status": "ingested"}, {"status": "failed"}],
            "failed": 1,
            "failures": [("b.txt", "bad")],
        },
        "/d",
    )
    assert "1 failed" in batch


def test_literature_trigger_is_bounded_by_timeout(monkeypatch, tmp_path):
    """A slow/hung KG ingest must not hang the trigger: it's bounded by
    _LIT_KG_INGEST_TIMEOUT and the TimeoutError is swallowed (non-fatal)."""
    import time

    out = tmp_path / "lit"
    out.mkdir()
    src = out / "source.txt"
    src.write_text("x", encoding="utf-8")
    _write_result(out, {"source_text_path": str(src)})

    monkeypatch.setattr(agent_executors, "_LIT_KG_INGEST_TIMEOUT", 0.05)

    async def slow(source):
        await asyncio.sleep(5)

    monkeypatch.setattr(kg_tools, "ingest_source_into_kg", slow)
    t0 = time.monotonic()
    _run(agent_executors._ingest_literature_into_kg(out))  # returns ~0.05s, no raise
    assert time.monotonic() - t0 < 2.0


# --- the skill persists source.txt (subprocess, --demo) -----------------------


def test_literature_skill_persists_source_txt(tmp_path):
    out = tmp_path / "demo"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "skills" / "literature" / "literature_parse.py"), "--demo", "--output", str(out)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert (out / "source.txt").is_file()
    data = json.loads((out / "result.json").read_text(encoding="utf-8"))["data"]
    assert str(data.get("source_text_path", "")).endswith("source.txt")
    assert data.get("source", {}).get("type")


# --- 批7: per-thread source linkage on ingest ---------------------------------


def test_literature_trigger_records_thread_source_on_ingested(monkeypatch, tmp_path):
    """A literature ingest performed inside a thread records a thread<->source
    link off the returned slug (per-thread grounding)."""
    out = tmp_path / "lit"
    out.mkdir()
    src = out / "source.txt"
    src.write_text("paper body", encoding="utf-8")
    _write_result(out, {"source_text_path": str(src)})

    async def fake_ingest(source):
        return {"status": "ingested", "slug": "ruan2024", "source_page": "wiki/sources/ruan2024.md"}

    captured = []

    async def fake_capture(session_id, thread_id, slug, source_page=""):
        captured.append((session_id, thread_id, slug, source_page))

    monkeypatch.setattr(kg_tools, "ingest_source_into_kg", fake_ingest)
    monkeypatch.setattr(agent_executors, "_capture_thread_source", fake_capture)
    _run(agent_executors._ingest_literature_into_kg(out, thread_id="A", session_id="sess"))
    assert captured == [("sess", "A", "ruan2024", "wiki/sources/ruan2024.md")]


def test_literature_trigger_records_thread_source_on_cache_hit(monkeypatch, tmp_path):
    """A cache-hit ingest (the cross-thread reuse path) now carries a slug, so
    the new thread still associates the source."""
    out = tmp_path / "lit"
    out.mkdir()
    src = out / "source.txt"
    src.write_text("paper body", encoding="utf-8")
    _write_result(out, {"source_text_path": str(src)})

    async def fake_ingest(source):
        return {"status": "skipped", "reason": "cache hit", "slug": "ruan2024"}

    captured = []

    async def fake_capture(session_id, thread_id, slug, source_page=""):
        captured.append((session_id, thread_id, slug, source_page))

    monkeypatch.setattr(kg_tools, "ingest_source_into_kg", fake_ingest)
    monkeypatch.setattr(agent_executors, "_capture_thread_source", fake_capture)
    _run(agent_executors._ingest_literature_into_kg(out, thread_id="A", session_id="sess"))
    assert captured == [("sess", "A", "ruan2024", "")]


def test_literature_trigger_skips_capture_without_thread(monkeypatch, tmp_path):
    """No thread context (legacy / IM path) → no link recorded."""
    out = tmp_path / "lit"
    out.mkdir()
    src = out / "source.txt"
    src.write_text("paper body", encoding="utf-8")
    _write_result(out, {"source_text_path": str(src)})

    async def fake_ingest(source):
        return {"status": "ingested", "slug": "ruan2024"}

    captured = []

    async def fake_capture(session_id, thread_id, slug, source_page=""):
        captured.append(slug)

    monkeypatch.setattr(kg_tools, "ingest_source_into_kg", fake_ingest)
    monkeypatch.setattr(agent_executors, "_capture_thread_source", fake_capture)
    _run(agent_executors._ingest_literature_into_kg(out))  # no thread_id/session_id
    assert captured == []


def test_kg_ingest_tool_records_thread_source(monkeypatch):
    """The kg_ingest LLM tool records a thread<->source link when invoked inside
    a thread (thread_id arrives via ToolSpec context_params)."""
    import omicsclaw.skill.orchestration as orchestration

    fake_cmd = types.SimpleNamespace(
        ingest=lambda source, cfg, llm: {"status": "ingested", "slug": "s1", "source_page": "wiki/sources/s1.md"}
    )
    fake_cfg = types.SimpleNamespace(resolve=lambda home: "cfg")
    monkeypatch.setattr(kg_tools, "_import_kg_ingest", lambda: (fake_cfg, fake_cmd))
    monkeypatch.setattr(kg_tools, "_build_kg_extractor", lambda: "EXTRACTOR")
    monkeypatch.setattr(kg_tools, "_resolve_kg_home", lambda: "/kg/home")

    captured = []

    async def fake_capture(session_id, thread_id, slug, source_page=""):
        captured.append((session_id, thread_id, slug, source_page))

    monkeypatch.setattr(orchestration, "_capture_thread_source", fake_capture)
    out = _run(kg_tools.execute_kg_ingest({"source": "https://example.com/p"}, session_id="sess", thread_id="A"))
    assert "Ingested" in out
    assert captured == [("sess", "A", "s1", "wiki/sources/s1.md")]


def test_kg_ingest_tool_skips_capture_without_thread(monkeypatch):
    import omicsclaw.skill.orchestration as orchestration

    fake_cmd = types.SimpleNamespace(ingest=lambda source, cfg, llm: {"status": "ingested", "slug": "s1"})
    fake_cfg = types.SimpleNamespace(resolve=lambda home: "cfg")
    monkeypatch.setattr(kg_tools, "_import_kg_ingest", lambda: (fake_cfg, fake_cmd))
    monkeypatch.setattr(kg_tools, "_build_kg_extractor", lambda: "EXTRACTOR")
    monkeypatch.setattr(kg_tools, "_resolve_kg_home", lambda: "/kg/home")

    captured = []

    async def fake_capture(session_id, thread_id, slug, source_page=""):
        captured.append(slug)

    monkeypatch.setattr(orchestration, "_capture_thread_source", fake_capture)
    _run(kg_tools.execute_kg_ingest({"source": "https://example.com/p"}, session_id="sess"))  # no thread_id
    assert captured == []
