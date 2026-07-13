"""Backward-compat contract for omicsclaw.runtime.tools.builders.agent_executors.

The module collects the 24 ``execute_*`` async tool implementations + the
dispatch table builder, carved out of bot/core.py per ADR 0001 (#120).
External tests (``tests/test_bot_completion_messages.py``,
``tests/test_skill_listing.py``) import these names from ``omicsclaw.runtime.agent.state``;
this identity test guarantees the two paths point at the same callable.
"""

from __future__ import annotations


TOOL_EXECUTORS_REEXPORTS = (
    # 24 execute_* async functions
    "execute_omicsclaw",
    "execute_replot_skill",
    "execute_save_file",
    "execute_write_file",
    "execute_generate_audio",
    "execute_parse_literature",
    "execute_fetch_geo_metadata",
    "execute_list_directory",
    "execute_inspect_file",
    "execute_inspect_data",
    "execute_make_directory",
    "execute_move_file",
    "execute_remove_file",
    "execute_get_file_size",
    "execute_remember",
    "execute_recall",
    "execute_forget",
    "execute_read_knowhow",
    "execute_consult_knowledge",
    "execute_resolve_capability",
    "execute_list_skills_in_domain",
    "execute_create_omics_skill",
    "execute_web_method_search",
    # Dispatch surface
    "_available_tool_executors",
    "_build_tool_runtime",
    "get_tool_runtime",
    "get_tool_executors",
)


def test_tool_executors_re_exports_share_identity_with_bot_core():
    """Every previously-public symbol must resolve to the *same object*
    when looked up via ``omicsclaw.runtime.agent.state`` or via ``omicsclaw.runtime.tools.builders.agent_executors``."""
    import omicsclaw.runtime.agent.state
    import omicsclaw.runtime.tools.builders.agent_executors

    missing_on_tool_exec = [
        name for name in TOOL_EXECUTORS_REEXPORTS
        if not hasattr(omicsclaw.runtime.tools.builders.agent_executors, name)
    ]
    assert not missing_on_tool_exec, (
        f"Missing on omicsclaw.runtime.tools.builders.agent_executors: {missing_on_tool_exec}"
    )

    missing_on_core = [
        name for name in TOOL_EXECUTORS_REEXPORTS
        if not hasattr(omicsclaw.runtime.agent.state, name)
    ]
    assert not missing_on_core, (
        f"Missing on omicsclaw.runtime.agent.state (re-export): {missing_on_core}"
    )

    mismatched_identity = [
        name for name in TOOL_EXECUTORS_REEXPORTS
        if getattr(omicsclaw.runtime.agent.state, name) is not getattr(omicsclaw.runtime.tools.builders.agent_executors, name)
    ]
    assert not mismatched_identity, (
        f"Parallel copies (must be same object): {mismatched_identity}"
    )


# ---------------------------------------------------------------------------
# T2 S1 — bot's manage_memory tool path lands writes in session namespace
# ---------------------------------------------------------------------------


import pytest
import pytest_asyncio
import sqlalchemy as sa


@pytest_asyncio.fixture
async def memory_store(tmp_path, monkeypatch):
    """A real CompatMemoryStore wired to ``omicsclaw.runtime.agent.state.memory_store``.

    The bot tool ``execute_remember`` reads ``_core.memory_store`` (late
    binding from omicsclaw.runtime.agent.state's module-level global), so the test must mutate
    that global to inject a temp-DB store. ``monkeypatch`` restores it
    cleanly.
    """
    from omicsclaw.memory.compat import CompatMemoryStore
    import omicsclaw.runtime.agent.state

    store = CompatMemoryStore(database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await store.initialize()
    monkeypatch.setattr(omicsclaw.runtime.agent.state, "memory_store", store)
    try:
        yield store
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_execute_remember_lands_in_session_namespace(memory_store):
    """When the LLM tool ``execute_remember`` saves a preference for an
    active Telegram session, the resulting row must land in the
    session-derived namespace ``f"{platform}/{user_id}"`` — the
    production guarantee that two bot users cannot see each other's
    preferences. This test exercises the real bot→CompatMemoryStore→
    MemoryEngine→DB path; the only thing it skips is the LLM emitting
    the tool call."""
    from omicsclaw.runtime.tools.builders.agent_executors import execute_remember
    from omicsclaw.memory.models import Path

    session = await memory_store.create_session("alice", "telegram")

    result = await execute_remember(
        args={
            "memory_type": "preference",
            "domain": "global",
            "key": "qc_threshold",
            "value": "20%",
        },
        session_id=session.session_id,
    )

    assert "✓" in result or "saved" in result.lower(), (
        f"execute_remember reported failure: {result!r}"
    )

    async with memory_store._db.session() as s:
        rows = (
            await s.execute(
                sa.select(Path).where(
                    Path.domain == "preference",
                    Path.path == "global/qc_threshold",
                )
            )
        ).scalars().all()

    assert len(rows) == 1, (
        f"Expected exactly 1 path row, got {len(rows)}: "
        f"{[(r.namespace, r.domain, r.path) for r in rows]}"
    )
    assert rows[0].namespace == "telegram/alice", (
        f"Preference landed in {rows[0].namespace!r}; expected 'telegram/alice'"
    )


@pytest.mark.asyncio
async def test_execute_remember_preference_update_versions_existing_value(
    memory_store,
):
    """User says 'remember reply in Chinese' then later 'change to English'.
    Both messages route to ``execute_remember`` with the same
    ``(memory_type=preference, domain=global, key=language)`` triple but
    different ``value``. The desktop preference panel must reflect the new
    value, not the old one.

    Contract:
      - Exactly one Path row at ``preference/global/language``
      - Path resolves to a node whose **active** memory carries the new
        value (``English``)
      - The old value (``Chinese``) is preserved as a deprecated row in
        the chain (preference://* lives in VERSIONED_PREFIXES so the
        rollback UI can restore it)

    Regression: the user reported the desktop preference panel didn't
    update after the second tool call. If ``execute_remember`` no-ops on
    the second call (e.g., dedupe on URI without re-reading content) or
    if the path's edge_id never repoints to the new active memory, the
    panel keeps showing the stale value.
    """
    from omicsclaw.runtime.tools.builders.agent_executors import execute_remember
    from omicsclaw.memory.models import Edge, Memory, Path

    session = await memory_store.create_session("alice", "telegram")

    result_v1 = await execute_remember(
        args={
            "memory_type": "preference",
            "domain": "global",
            "key": "language",
            "value": "Chinese",
        },
        session_id=session.session_id,
    )
    assert "✓" in result_v1 or "saved" in result_v1.lower(), (
        f"v1 save failed: {result_v1!r}"
    )

    result_v2 = await execute_remember(
        args={
            "memory_type": "preference",
            "domain": "global",
            "key": "language",
            "value": "English",
        },
        session_id=session.session_id,
    )
    assert "✓" in result_v2 or "saved" in result_v2.lower(), (
        f"v2 save failed: {result_v2!r}"
    )

    async with memory_store._db.session() as s:
        path_rows = (
            await s.execute(
                sa.select(Path).where(
                    Path.domain == "preference",
                    Path.path == "global/language",
                    Path.namespace == "telegram/alice",
                )
            )
        ).scalars().all()
        assert len(path_rows) == 1, (
            f"Expected exactly one preference/global/language path; got "
            f"{len(path_rows)}. The bot is creating a sibling instead of "
            f"updating in place."
        )

        edge = (
            await s.execute(sa.select(Edge).where(Edge.id == path_rows[0].edge_id))
        ).scalar_one()

        memories = (
            await s.execute(
                sa.select(Memory)
                .where(Memory.node_uuid == edge.child_uuid)
                .order_by(Memory.id)
            )
        ).scalars().all()

    active = [m for m in memories if not m.deprecated]
    assert len(active) == 1, (
        f"Expected exactly one active memory after the second remember; "
        f"got {len(active)}. Active rows: "
        f"{[(m.id, m.deprecated, m.content[:60]) for m in active]}"
    )
    assert "English" in active[0].content, (
        f"Active preference still has the old value. Active content: "
        f"{active[0].content!r} (expected 'English' to appear). This is "
        f"the user-visible bug: the desktop panel keeps showing Chinese "
        f"after the user asks the bot to switch to English."
    )

    deprecated = [m for m in memories if m.deprecated]
    assert len(deprecated) == 1, (
        f"Expected exactly one deprecated memory (the prior Chinese row "
        f"so the rollback UI can restore it); got {len(deprecated)}."
    )
    assert "Chinese" in deprecated[0].content


# ---------------------------------------------------------------------------
# Disabled-memory-store user message: actionable + names real env vars
# (regression for "Set OMICSCLAW_MEMORY_BACKEND=sqlite in .env" — that env
# var never existed; the real switches are OMICSCLAW_MEMORY_ENABLED and
# OMICSCLAW_MEMORY_DB_URL, documented under §"Graph Memory System" in
# .env.example.)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "executor_name, kwargs",
    [
        ("execute_remember", {"args": {"memory_type": "preference"}, "session_id": "s1"}),
        ("execute_recall", {"args": {}, "session_id": "s1"}),
        ("execute_forget", {"args": {"query": "x"}, "session_id": "s1"}),
    ],
)
async def test_memory_tools_disabled_message_names_real_env_vars(
    executor_name, kwargs, monkeypatch
):
    import omicsclaw.runtime.agent.state
    import omicsclaw.runtime.tools.builders.agent_executors

    monkeypatch.setattr(omicsclaw.runtime.agent.state, "memory_store", None)
    executor = getattr(omicsclaw.runtime.tools.builders.agent_executors, executor_name)

    result = await executor(**kwargs)

    assert isinstance(result, str)
    assert "OMICSCLAW_MEMORY_BACKEND" not in result, (
        f"{executor_name} still names the bogus env var "
        f"OMICSCLAW_MEMORY_BACKEND. Real switches are "
        f"OMICSCLAW_MEMORY_ENABLED + OMICSCLAW_MEMORY_DB_URL."
    )
    assert "OMICSCLAW_MEMORY_ENABLED" in result, (
        f"{executor_name} disabled-message should name the real switch "
        f"OMICSCLAW_MEMORY_ENABLED so users know what to inspect."
    )
    assert "OMICSCLAW_MEMORY_DB_URL" in result, (
        f"{executor_name} disabled-message should name the DB URL var so "
        f"users can verify it points at a reachable database."
    )


def test_tool_executors_dispatch_table_lists_all_24_executors():
    """``_available_tool_executors()`` returns the full dispatch map.
    The lazy ``omicsclaw.runtime.agent.state.TOOL_EXECUTORS`` attribute also adds the
    engineering tool executors (file_read / write_file / list_directory /
    edit_file / shell). Pin the count so an accidental dropped registration
    (e.g. typo on ``execute_X.__name__``) is caught."""
    import omicsclaw.runtime.tools.builders.agent_executors

    table = omicsclaw.runtime.tools.builders.agent_executors._available_tool_executors()
    # 24 native executors are mapped; engineering tools are added on top
    # by ``executors.update(build_engineering_tool_executors(...))``.
    assert len(table) >= 24
    # Spot-check a few canonical entries
    for name in ("omicsclaw", "save_file", "inspect_data", "remember", "consult_knowledge"):
        assert name in table, f"Tool name '{name}' missing from dispatch table"


# ---------------------------------------------------------------------------
# Regression: path_validation helpers must be importable from omicsclaw.runtime.tools.builders.agent_executors
# ---------------------------------------------------------------------------


PATH_VALIDATION_SYMBOLS = (
    "_ensure_trusted_dirs",
    "TRUSTED_DATA_DIRS",
    "validate_input_path",
    "discover_file",
    # Also missed by the carve-out — used by execute_save_file /
    # execute_write_file / execute_generate_audio (lines ~758, 787, 814).
    "resolve_dest",
    "sanitize_filename",
    "validate_path",
)


# Symbols from omicsclaw.runtime.agent.state / omicsclaw.skill.orchestration / preflight.sc_batch that
# the carve-out left as bare references inside ``execute_*`` bodies. Each
# one is an unexercised NameError waiting for the right tool call. Pinning
# them at module level catches the whole class of bug, not just the
# _ensure_trusted_dirs incident.
CARVED_OUT_SIBLING_SYMBOLS = (
    # omicsclaw.runtime.agent.state
    "DEEP_LEARNING_METHODS",
    "OMICSCLAW_PY",
    "_path_names",
    # omicsclaw.skill.orchestration
    "_infer_skill_for_method",
    "_run_skill_via_shared_runner",
    # omicsclaw.skill.preflight.sc_batch
    "_resolve_requested_batch_key",
)


def test_tool_executors_imports_path_validation_helpers():
    """omicsclaw.runtime.tools.builders.agent_executors uses ``_ensure_trusted_dirs`` / ``TRUSTED_DATA_DIRS``
    / ``validate_input_path`` / ``discover_file`` in several executors (lines
    ~140, 224, 1012, 1057, 1088, 1277, 1307, 1334, 1361, 1666, 1786, 1788 at
    the time of writing). When the module was carved out of omicsclaw.runtime.agent.state per
    ADR 0001, the import of these names was left behind in omicsclaw.runtime.agent.state. Every
    call into ``execute_omicsclaw`` with ``mode='file'`` (no staged input)
    fires ``_ensure_trusted_dirs()`` and raises ``NameError`` instead of
    returning the friendly 'place your file in a data directory' message.

    Pin the contract: the four names must resolve as module-level
    attributes on omicsclaw.runtime.tools.builders.agent_executors."""
    import omicsclaw.runtime.agent.state  # noqa: F401 — load omicsclaw.runtime.agent.state first (production order)
    import omicsclaw.runtime.tools.builders.agent_executors

    missing = [
        name for name in PATH_VALIDATION_SYMBOLS
        if not hasattr(omicsclaw.runtime.tools.builders.agent_executors, name)
    ]
    assert not missing, (
        f"omicsclaw.runtime.tools.builders.agent_executors is missing path_validation symbols: {missing}. "
        f"Add them to the `from omicsclaw.services.path_validation import ...` block."
    )


def test_tool_executors_imports_carved_out_sibling_helpers():
    """Same bug class as ``test_tool_executors_imports_path_validation_helpers``,
    but covers the carve-out leftovers from omicsclaw.runtime.agent.state / omicsclaw.skill.orchestration
    / preflight.sc_batch. Each is referenced bare inside an ``execute_*``
    body and would raise NameError on the first call that exercises it
    (pyflakes audit on 2026-05-13)."""
    import omicsclaw.runtime.agent.state  # noqa: F401 — load omicsclaw.runtime.agent.state first (production order)
    import omicsclaw.runtime.tools.builders.agent_executors

    missing = [
        name for name in CARVED_OUT_SIBLING_SYMBOLS
        if not hasattr(omicsclaw.runtime.tools.builders.agent_executors, name)
    ]
    assert not missing, (
        f"omicsclaw.runtime.tools.builders.agent_executors is missing carve-out sibling symbols: {missing}. "
        f"Each one is an unexercised NameError waiting for the right tool "
        f"call. Add them to the existing per-module import blocks at the "
        f"top of bot/tool_executors.py."
    )


def test_trusted_data_dirs_mutation_visible_to_tool_executors():
    """``_ensure_trusted_dirs()`` populates ``omicsclaw.services.path_validation.TRUSTED_DATA_DIRS``,
    but if it *rebinds* the global instead of mutating in place, modules
    that imported the name (omicsclaw.runtime.tools.builders.agent_executors, omicsclaw.runtime.agent.state,
    omicsclaw.surfaces.desktop.server) stay stuck on the original empty list.

    Symptom observed via the executor probe on 2026-05-13:
        Trusted data dirs: ['/.../data', '/.../examples', ...]   (path_validation)
        Access denied: /.../data is not in trusted directories ()  (tool_executors)

    omicsclaw/app/server.py:686-691 also relies on appending to
    ``omicsclaw.runtime.agent.state.TRUSTED_DATA_DIRS`` being visible to ``validate_input_path``
    — same root cause.

    Contract: after the helper runs, every module's view is non-empty and
    is the *same list object* as ``omicsclaw.services.path_validation.TRUSTED_DATA_DIRS``."""
    import omicsclaw.runtime.agent.state
    import omicsclaw.services.path_validation
    import omicsclaw.runtime.tools.builders.agent_executors

    omicsclaw.services.path_validation._ensure_trusted_dirs()

    pv = omicsclaw.services.path_validation.TRUSTED_DATA_DIRS
    assert pv, "Sanity: path_validation.TRUSTED_DATA_DIRS unexpectedly empty after _ensure"

    assert omicsclaw.runtime.tools.builders.agent_executors.TRUSTED_DATA_DIRS, (
        "omicsclaw.runtime.tools.builders.agent_executors.TRUSTED_DATA_DIRS is empty after _ensure_trusted_dirs() "
        "ran in omicsclaw.services.path_validation. The helper rebinds the global instead of "
        "mutating in place — fix it with `TRUSTED_DATA_DIRS[:] = _build_trusted_dirs()`."
    )
    assert omicsclaw.runtime.tools.builders.agent_executors.TRUSTED_DATA_DIRS is pv, (
        "omicsclaw.runtime.tools.builders.agent_executors.TRUSTED_DATA_DIRS diverged from "
        "omicsclaw.services.path_validation.TRUSTED_DATA_DIRS — they must be the same list "
        "object so server.py's runtime append() is visible to validate_input_path."
    )
    assert omicsclaw.runtime.agent.state.TRUSTED_DATA_DIRS is pv, (
        "omicsclaw.runtime.agent.state.TRUSTED_DATA_DIRS diverged from path_validation. server.py "
        "appends workspace dirs to omicsclaw.runtime.agent.state.TRUSTED_DATA_DIRS — that mutation "
        "must propagate to validate_input_path."
    )


@pytest.mark.asyncio
async def test_execute_list_directory_allows_data_dir_after_ensure():
    """Behavioral consequence of the rebind bug: ``execute_list_directory``
    rejects the legit DATA_DIR with 'Access denied ... is not in trusted
    directories ()' because its module-local TRUSTED_DATA_DIRS view is still
    the empty list. Once the helper mutates in place, DATA_DIR is trusted
    and the listing proceeds."""
    import omicsclaw.runtime.agent.state
    from omicsclaw.runtime.tools.builders.agent_executors import execute_list_directory

    result = await execute_list_directory({"path": str(omicsclaw.runtime.agent.state.DATA_DIR)})
    assert "Access denied" not in result, (
        f"execute_list_directory denied access to DATA_DIR — the trusted-dirs "
        f"check is reading a stale empty list. Got: {result!r}"
    )


@pytest.mark.asyncio
async def test_execute_omicsclaw_file_mode_without_input_returns_friendly_error():
    """End-to-end regression for the 2026-05-13 incident: user invoked
    ``omicsclaw({"mode": "file", "skill": "sc-standardize-input", ...})``
    and got ``NameError: name '_ensure_trusted_dirs' is not defined``
    instead of the documented 'no input file' guidance.

    Contract: when ``mode='file'`` and nothing is staged for the session,
    the executor returns a human-readable error string starting with
    'No input file available' — never raises NameError."""
    import omicsclaw.runtime.agent.state  # noqa: F401 — load omicsclaw.runtime.agent.state first (production order)
    from omicsclaw.runtime.tools.builders.agent_executors import execute_omicsclaw

    result = await execute_omicsclaw(
        args={"mode": "file", "skill": "sc-standardize-input"},
        session_id="missing-session-for-regression-test",
    )
    assert isinstance(result, str), f"expected str, got {type(result).__name__}"
    assert "No input file available" in result, (
        f"execute_omicsclaw did not return the friendly no-input-file guidance; "
        f"got: {result!r}"
    )


@pytest.mark.asyncio
async def test_autonomous_analysis_resolves_relative_input_paths(tmp_path, monkeypatch):
    """Regression (2026-06-24): execute_autonomous_analysis_execute must resolve
    relative input_paths to ABSOLUTE trusted paths before handing them to the
    engine.

    A Desktop user asked to QC ``data/slideseqv2_mouse_hippocampus.h5ad``; the
    model passed that *workspace-relative* path through verbatim. The sandbox
    kernel chdir's into its run workspace, so the in-kernel ``read_h5ad('data/..')``
    missed the file -> ``adata=None`` -> every step died on ``'NoneType' object
    has no attribute 'shape'`` and the run failed (consecutive_failures). The fix
    resolves the path the same way inspect_data / custom_analysis already do.
    """
    import omicsclaw.runtime.agent.state  # noqa: F401 — load state first (production order)
    import omicsclaw.autonomous as autonomous_pkg
    import omicsclaw.services.path_validation as pv
    from omicsclaw.autonomous.contracts import AutonomousRunResult, AutonomousRunStatus
    from omicsclaw.runtime.tools.builders.agent_executors import (
        execute_autonomous_analysis_execute,
    )

    # A trusted workspace with a data file under data/, registered the way
    # server.py's _apply_runtime_workspace() registers a live Desktop workspace.
    ws = tmp_path / "omicsclaw-workspace"
    (ws / "data").mkdir(parents=True)
    data_file = ws / "data" / "demo.h5ad"
    data_file.write_bytes(b"\x89HDF\r\n\x1a\n")  # engine is mocked; contents irrelevant
    pv._ensure_trusted_dirs()
    monkeypatch.setattr(pv, "TRUSTED_DATA_DIRS", [*pv.TRUSTED_DATA_DIRS, ws])

    captured: dict = {}

    async def _fake_loop(request, **kwargs):
        captured["request"] = request
        return AutonomousRunResult(
            run_id="t",
            workspace_root=str(ws / "output"),
            status=AutonomousRunStatus.SUCCEEDED,
        )

    monkeypatch.setattr(autonomous_pkg, "run_autonomous_code_loop_async", _fake_loop)

    out = await execute_autonomous_analysis_execute(
        {"goal": "QC the dataset", "input_paths": ["data/demo.h5ad"]}
    )

    assert "request" in captured, f"engine was never called; executor returned: {out!r}"
    got = captured["request"].input_paths
    assert got == [str(data_file.resolve())], (
        "execute_autonomous_analysis_execute handed an unresolved/relative input "
        f"path to the engine: {got!r}. A relative path never resolves inside the "
        "sandbox kernel (cwd = run workspace), so adata stays None and the run dies."
    )


@pytest.mark.asyncio
async def test_autonomous_analysis_reports_unresolvable_input_path(tmp_path, monkeypatch):
    """A path that exists nowhere trusted must yield a clear error and NOT start
    the engine — so the model gets actionable feedback instead of a sandboxed run
    that confabulates 'the sandbox blocked file access'."""
    import omicsclaw.runtime.agent.state  # noqa: F401 — load state first (production order)
    import omicsclaw.autonomous as autonomous_pkg
    from omicsclaw.runtime.tools.builders.agent_executors import (
        execute_autonomous_analysis_execute,
    )

    called = {"engine": False}

    async def _fake_loop(request, **kwargs):  # pragma: no cover - must not run
        called["engine"] = True
        raise AssertionError("engine started despite an unresolvable input path")

    monkeypatch.setattr(autonomous_pkg, "run_autonomous_code_loop_async", _fake_loop)

    out = await execute_autonomous_analysis_execute(
        {"goal": "QC", "input_paths": ["data/does_not_exist_anywhere.h5ad"]}
    )
    assert called["engine"] is False
    assert "Error" in out and "does_not_exist_anywhere.h5ad" in out, (
        f"expected a clear unresolved-path error naming the file; got: {out!r}"
    )


@pytest.mark.asyncio
async def test_autonomous_analysis_resolves_upstream_directory(tmp_path, monkeypatch):
    """Codex finding 2 regression: upstream_paths are prior-skill output DIRECTORIES
    (per the tool schema), so the resolver must accept directories with
    allow_dir=True — not reject them as unresolved and refuse to start the engine."""
    import omicsclaw.runtime.agent.state  # noqa: F401 — load state first (production order)
    import omicsclaw.autonomous as autonomous_pkg
    import omicsclaw.services.path_validation as pv
    from omicsclaw.autonomous.contracts import AutonomousRunResult, AutonomousRunStatus
    from omicsclaw.runtime.tools.builders.agent_executors import (
        execute_autonomous_analysis_execute,
    )

    ws = tmp_path / "omicsclaw-workspace"
    (ws / "data").mkdir(parents=True)
    (ws / "data" / "demo.h5ad").write_bytes(b"\x89HDF\r\n\x1a\n")
    upstream_dir = ws / "output" / "prior_run"
    upstream_dir.mkdir(parents=True)
    pv._ensure_trusted_dirs()
    monkeypatch.setattr(pv, "TRUSTED_DATA_DIRS", [*pv.TRUSTED_DATA_DIRS, ws])

    captured: dict = {}

    async def _fake_loop(request, **kwargs):
        captured["request"] = request
        return AutonomousRunResult(
            run_id="t", workspace_root=str(ws / "output"),
            status=AutonomousRunStatus.SUCCEEDED,
        )

    monkeypatch.setattr(autonomous_pkg, "run_autonomous_code_loop_async", _fake_loop)

    out = await execute_autonomous_analysis_execute(
        {
            "goal": "QC",
            "input_paths": ["data/demo.h5ad"],
            "upstream_paths": ["output/prior_run"],
        }
    )
    assert "request" in captured, f"engine was not started; got: {out!r}"
    assert captured["request"].upstream_paths == [str(upstream_dir.resolve())], (
        "upstream output directory was rejected instead of resolved; got: "
        f"{captured['request'].upstream_paths!r}"
    )


@pytest.mark.asyncio
async def test_autonomous_analysis_rejects_ambiguous_bare_filename(tmp_path, monkeypatch):
    """Codex finding 1 regression: a bare filename matching several files must be
    refused with a clear 'pass a full path' error (not silently resolved to the
    newest by mtime), and the engine must not start."""
    import omicsclaw.runtime.agent.state  # noqa: F401 — load state first (production order)
    import omicsclaw.autonomous as autonomous_pkg
    import omicsclaw.services.path_validation as pv
    from omicsclaw.runtime.tools.builders.agent_executors import (
        execute_autonomous_analysis_execute,
    )

    ws = tmp_path / "omicsclaw-workspace"
    (ws / "sub1").mkdir(parents=True)
    (ws / "sub2").mkdir(parents=True)
    (ws / "sub1" / "dup.h5ad").write_bytes(b"\x89HDF\r\n\x1a\n")
    (ws / "sub2" / "dup.h5ad").write_bytes(b"\x89HDF\r\n\x1a\n")
    pv._ensure_trusted_dirs()
    # Scope trust to just this workspace so discover_file only rglobs the tmp tree.
    monkeypatch.setattr(pv, "TRUSTED_DATA_DIRS", [ws])

    called = {"engine": False}

    async def _fake_loop(request, **kwargs):  # pragma: no cover - must not run
        called["engine"] = True
        raise AssertionError("engine started on an ambiguous bare filename")

    monkeypatch.setattr(autonomous_pkg, "run_autonomous_code_loop_async", _fake_loop)

    out = await execute_autonomous_analysis_execute(
        {"goal": "QC", "input_paths": ["dup.h5ad"]}
    )
    assert called["engine"] is False
    assert "Error" in out and "multiple files" in out, (
        f"expected an ambiguous-match error; got: {out!r}"
    )


def test_validate_input_path_prefers_active_workspace_over_repo(tmp_path, monkeypatch):
    """Finding 1 fix: when the same relative name exists in BOTH the project root
    and the active Desktop workspace, validate_input_path resolves to the WORKSPACE
    copy (the file the user actually loaded), not the project-root copy."""
    import omicsclaw.runtime.agent.state as state
    import omicsclaw.services.path_validation as pv

    repo_dir = tmp_path / "repo"
    (repo_dir / "data").mkdir(parents=True)
    repo_copy = repo_dir / "data" / "dup.h5ad"
    repo_copy.write_bytes(b"repo")
    ws = tmp_path / "omicsclaw-workspace"
    (ws / "data").mkdir(parents=True)
    ws_copy = ws / "data" / "dup.h5ad"
    ws_copy.write_bytes(b"workspace")

    monkeypatch.setattr(state, "OMICSCLAW_DIR", repo_dir)
    pv._ensure_trusted_dirs()
    monkeypatch.setattr(pv, "TRUSTED_DATA_DIRS", [repo_dir / "data", ws])
    monkeypatch.setenv("OMICSCLAW_WORKSPACE", str(ws))

    got = pv.validate_input_path("data/dup.h5ad")
    assert got == ws_copy.resolve(), (
        f"expected the workspace copy {ws_copy}, got {got} — a workspace-relative "
        "path must prefer the active workspace over the project-root copy"
    )


def test_validate_input_path_ignores_untrusted_workspace(tmp_path, monkeypatch):
    """Guard for the Codex-flagged shadowing regression: when OMICSCLAW_WORKSPACE is
    set but is NOT a trusted dir, it must not shadow a valid trusted-dir match — the
    relative path resolves to the trusted (project-root) copy, never None."""
    import omicsclaw.runtime.agent.state as state
    import omicsclaw.services.path_validation as pv

    repo_dir = tmp_path / "repo"
    (repo_dir / "data").mkdir(parents=True)
    repo_copy = repo_dir / "data" / "dup.h5ad"
    repo_copy.write_bytes(b"repo")
    untrusted_ws = tmp_path / "untrusted-ws"
    (untrusted_ws / "data").mkdir(parents=True)
    (untrusted_ws / "data" / "dup.h5ad").write_bytes(b"workspace")

    monkeypatch.setattr(state, "OMICSCLAW_DIR", repo_dir)
    pv._ensure_trusted_dirs()
    monkeypatch.setattr(pv, "TRUSTED_DATA_DIRS", [repo_dir / "data"])  # ws NOT trusted
    monkeypatch.setenv("OMICSCLAW_WORKSPACE", str(untrusted_ws))

    got = pv.validate_input_path("data/dup.h5ad")
    assert got == repo_copy.resolve(), (
        f"an untrusted OMICSCLAW_WORKSPACE shadowed the trusted copy; got {got} "
        f"(expected the project-root copy {repo_copy})"
    )


@pytest.mark.asyncio
async def test_autonomous_analysis_rejects_r_language(monkeypatch):
    """Finding 5 fix: the mini-agent is Python-only (mini_agent.py hardcodes
    validate_generated_code(language='python') and a Python prompt/init), so a
    language='r' request must return a clear Python-only error and NOT start the
    engine — instead of silently running Python under an R label."""
    import omicsclaw.runtime.agent.state  # noqa: F401 — load state first (production order)
    import omicsclaw.autonomous as autonomous_pkg
    from omicsclaw.runtime.tools.builders.agent_executors import (
        execute_autonomous_analysis_execute,
    )

    called = {"engine": False}

    async def _fake_loop(request, **kwargs):  # pragma: no cover - must not run
        called["engine"] = True
        raise AssertionError("engine started for an R request")

    monkeypatch.setattr(autonomous_pkg, "run_autonomous_code_loop_async", _fake_loop)

    out = await execute_autonomous_analysis_execute({"goal": "QC", "language": "r"})
    assert called["engine"] is False
    assert "Error" in out and "Python only" in out, (
        f"expected a clear Python-only rejection; got: {out!r}"
    )


@pytest.mark.asyncio
async def test_autonomous_analysis_appends_a_promotion_suggestion_on_the_third_similar_success(
    memory_store, monkeypatch
):
    """P4 (docs/proposals/skill-acquisition-plan.md §P4) end-to-end wiring: a
    3rd similar-goal success in the same thread must append a promotion
    suggestion to the digest, anchored to THIS run's own workspace_root. The
    unsafe global `promote_from_latest` admission path is disabled."""
    import omicsclaw.autonomous as autonomous_pkg
    from omicsclaw.autonomous.contracts import AutonomousRunResult, AutonomousRunStatus
    from omicsclaw.runtime.tools.builders.agent_executors import (
        execute_autonomous_analysis_execute,
    )
    from omicsclaw.skill.orchestration import _auto_capture_autonomous_run

    session = await memory_store.create_session("u", "telegram")
    sid = session.session_id
    thread_id = "thread-promo"
    goal = "cluster cells by cell type and annotate"

    # Seed 2 PRIOR similar successes in the same thread.
    await _auto_capture_autonomous_run(sid, thread_id, "cluster the cells by cell type", "run-1", "/tmp/run-1", "succeeded")
    await _auto_capture_autonomous_run(sid, thread_id, "Cluster cells by type and annotate.", "run-2", "/tmp/run-2", "succeeded")

    async def _fake_loop(request, **kwargs):
        return AutonomousRunResult(
            run_id="run-3", workspace_root="/tmp/run-3", status=AutonomousRunStatus.SUCCEEDED
        )

    monkeypatch.setattr(autonomous_pkg, "run_autonomous_code_loop_async", _fake_loop)

    out = await execute_autonomous_analysis_execute(
        {"goal": goal}, session_id=sid, thread_id=thread_id
    )

    assert "Promotion candidate" in out
    assert "3rd time" in out
    assert "source_analysis_dir='/tmp/run-3'" in out
    assert "promote_from_latest=True" not in out
    # Fix 6: an autonomous-analysis bundle never carries a domain, so the
    # snippet as printed would raise if run verbatim — the suggestion must
    # say so explicitly instead of shipping a silently-broken command.
    assert 'domain="..."' in out
    assert "spatial" in out and "singlecell" in out

    # The 3rd run's own lineage must also now be on record.
    recs = await memory_store.get_memories(sid, "autonomous_run", thread_id=thread_id)
    assert len(recs) == 3
    assert any(r.run_id == "run-3" and r.status == "succeeded" for r in recs)


@pytest.mark.asyncio
async def test_autonomous_analysis_no_promotion_suggestion_below_threshold(memory_store, monkeypatch):
    """Only 1 prior similar success exists — must not suggest promotion yet,
    and the digest must be unaffected (no "Promotion candidate" section)."""
    import omicsclaw.autonomous as autonomous_pkg
    from omicsclaw.autonomous.contracts import AutonomousRunResult, AutonomousRunStatus
    from omicsclaw.runtime.tools.builders.agent_executors import (
        execute_autonomous_analysis_execute,
    )
    from omicsclaw.skill.orchestration import _auto_capture_autonomous_run

    session = await memory_store.create_session("u", "telegram")
    sid = session.session_id
    thread_id = "thread-promo-2"

    await _auto_capture_autonomous_run(sid, thread_id, "cluster the cells by cell type", "run-1", "/tmp/run-1", "succeeded")

    async def _fake_loop(request, **kwargs):
        return AutonomousRunResult(
            run_id="run-2", workspace_root="/tmp/run-2", status=AutonomousRunStatus.SUCCEEDED
        )

    monkeypatch.setattr(autonomous_pkg, "run_autonomous_code_loop_async", _fake_loop)

    out = await execute_autonomous_analysis_execute(
        {"goal": "cluster cells by type"}, session_id=sid, thread_id=thread_id
    )
    assert "Promotion candidate" not in out


def test_autonomous_analysis_execute_toolspec_declares_thread_id_in_context_params():
    """Regression guard: the two tests above call ``execute_autonomous_analysis_execute``
    directly with a hand-passed ``thread_id=`` kwarg, which bypasses the real
    ``ToolSpec.context_params`` -> ``build_executor_kwargs`` -> executor seam
    entirely. That bypass is exactly how the production wiring gap (thread_id
    silently missing from the real ToolSpec, so ``kwargs.get("thread_id")`` was
    always ``""`` in production) went undetected. Pin the declaration itself so
    it cannot regress silently again."""
    from omicsclaw.runtime.tools.builders.agent import BotToolContext, build_bot_tool_specs

    specs = build_bot_tool_specs(BotToolContext(skill_names=()))
    spec = next(s for s in specs if s.name == "autonomous_analysis_execute")
    assert "thread_id" in spec.context_params


@pytest.mark.asyncio
async def test_autonomous_analysis_promotion_suggestion_reaches_executor_through_the_real_toolspec_seam(
    memory_store, monkeypatch
):
    """End-to-end through the REAL dispatch seam (ToolSpec.context_params ->
    build_executor_kwargs -> invoke_tool), not a hand-rolled bypass. Must fail
    on the pre-fix code (thread_id absent from context_params -> executor sees
    thread_id="" -> _compute_promotion_suggestion declines) and pass once
    "thread_id" is declared."""
    import omicsclaw.autonomous as autonomous_pkg
    from omicsclaw.autonomous.contracts import AutonomousRunResult, AutonomousRunStatus
    from omicsclaw.runtime.tools.builders.agent import BotToolContext, build_bot_tool_specs
    from omicsclaw.runtime.tools.builders.agent_executors import (
        execute_autonomous_analysis_execute,
    )
    from omicsclaw.runtime.tools.executor import invoke_tool
    from omicsclaw.skill.orchestration import _auto_capture_autonomous_run

    session = await memory_store.create_session("u", "telegram")
    sid = session.session_id
    thread_id = "thread-promo-seam"
    goal = "cluster cells by cell type and annotate"

    await _auto_capture_autonomous_run(sid, thread_id, "cluster the cells by cell type", "run-1", "/tmp/run-1", "succeeded")
    await _auto_capture_autonomous_run(sid, thread_id, "Cluster cells by type and annotate.", "run-2", "/tmp/run-2", "succeeded")

    async def _fake_loop(request, **kwargs):
        return AutonomousRunResult(
            run_id="run-3", workspace_root="/tmp/run-3", status=AutonomousRunStatus.SUCCEEDED
        )

    monkeypatch.setattr(autonomous_pkg, "run_autonomous_code_loop_async", _fake_loop)

    specs = build_bot_tool_specs(BotToolContext(skill_names=()))
    spec = next(s for s in specs if s.name == "autonomous_analysis_execute")

    out = await invoke_tool(
        spec,
        execute_autonomous_analysis_execute,
        {"goal": goal},
        runtime_context={
            "session_id": sid,
            "thread_id": thread_id,
            "chat_id": "",
            "surface": "",
            "policy_state": {},
            "model_override": "",
            "provider_override": "",
        },
    )

    assert "Promotion candidate" in out
