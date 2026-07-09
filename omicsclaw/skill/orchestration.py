"""Skill orchestration helpers — output media collection + auto-routing
banner / disambiguation rendering.

Carved out of ``bot/core.py`` per ADR 0001. **Reduced scope**: this slice
moves only the externally-tested public API (``OutputMediaPaths`` +
``_collect_output_media_paths``; the auto-routing banner / disambiguation
formatters and ``_AUTO_DISAMBIGUATE_GAP``). The remaining skill-execution
machinery the issue spec lists (``_run_omics_skill_step``,
``_run_skill_via_shared_runner``, ``_lookup_skill_info``,
``_resolve_param_hint_info``, ``_infer_skill_for_method``,
``_build_method_preview``, ``_build_param_hint``, the
``_auto_capture_*`` async helpers, the env-error parsing helpers, and
``_resolve_last_output_dir`` / ``_read_result_json`` /
``_update_preprocessing_state`` / ``_format_next_steps``) stays in
``omicsclaw.runtime.agent.state`` for now — they have no external test imports and moving the
~700 LOC en bloc would require many late-import surgery passes that buy
no immediate win. A follow-up issue can complete the migration once the
agent-loop slice (#121) has consolidated the LLM client globals.

The functions in this module are pure formatters; ``_format_auto_*``
late-imports ``_skill_registry`` from ``omicsclaw.runtime.agent.state`` to look up skill
descriptions for the disambiguation block.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OutputMediaPaths:
    figure_paths: list[Path]
    table_paths: list[Path]
    notebook_paths: list[Path]
    media_items: list[dict]


def _collect_output_media_paths(out_dir: Path) -> OutputMediaPaths:
    figure_paths: list[Path] = []
    table_paths: list[Path] = []
    notebook_paths: list[Path] = []
    media_items: list[dict] = []

    if not out_dir.exists():
        return OutputMediaPaths(figure_paths, table_paths, notebook_paths, media_items)

    for f in sorted(out_dir.rglob("*")):
        if not f.is_file():
            continue
        if f.suffix in (".md", ".html"):
            media_items.append({"type": "document", "path": str(f)})
        elif f.suffix == ".ipynb":
            media_items.append({"type": "document", "path": str(f)})
            notebook_paths.append(f)
        elif f.suffix == ".png":
            media_items.append({"type": "photo", "path": str(f)})
            figure_paths.append(f)
        elif f.suffix == ".csv":
            media_items.append({"type": "document", "path": str(f)})
            table_paths.append(f)

    return OutputMediaPaths(figure_paths, table_paths, notebook_paths, media_items)


@dataclass(frozen=True)
class MediaDeliveryPlan:
    """The single decision of what a finished run shows the user.

    ``pending_items`` are appended to the ``pending_media`` side-channel that the
    desktop surface drains: requested artifacts (queued as interactive file
    cards) followed by at most one ``output_summary`` item (the collapsed
    "N outputs" entry counting the artifacts the user did NOT ask to see).
    ``sent_names`` are the basenames queued for display (for the LLM-facing
    note). ``summary`` is the output_summary dict (also the last pending item
    when present), or ``None``.
    """

    pending_items: list[dict]
    sent_names: list[str]
    summary: dict | None


def _filter_requested_media(media_items: list[dict], return_media: str) -> list[dict]:
    """Select the media items the user explicitly asked for.

    ``return_media`` is the intent signal: empty → nothing; ``"all"`` →
    everything; otherwise a comma-separated keyword list matched against each
    file stem."""
    rm = (return_media or "").strip().lower()
    if not rm:
        return []
    if rm == "all":
        return list(media_items)
    keywords = [k.strip() for k in rm.split(",") if k.strip()]
    if not keywords:
        return []
    return [
        item
        for item in media_items
        if any(kw in Path(str(item.get("path", ""))).stem.lower() for kw in keywords)
    ]


def build_media_delivery_plan(
    collected: OutputMediaPaths,
    return_media: str,
    run_dir: Path | str | None,
    *,
    always_anchor: bool = False,
) -> MediaDeliveryPlan:
    """Decide what a finished run delivers to chat, gated on user intent.

    Figures/tables are NEVER auto-shown: only the artifacts named by
    ``return_media`` are queued for display (rendered as interactive cards).
    Everything the user did not request is collapsed into a single
    ``output_summary`` item carrying the counts + ``run_dir`` (the desktop
    renders it as a "view outputs" entry and uses ``run_dir`` to link the run to
    its conversation). ``always_anchor`` forces a summary even when nothing is
    left to count, so text-only runs still leave a run-dir anchor for 本对话
    session stamping.
    """
    sent = _filter_requested_media(collected.media_items, return_media)
    sent_paths = {str(item.get("path", "")) for item in sent}
    sent_names = [Path(str(item["path"])).name for item in sent if item.get("path")]

    fig_unsent = sum(1 for p in collected.figure_paths if str(p) not in sent_paths)
    tab_unsent = sum(1 for p in collected.table_paths if str(p) not in sent_paths)
    nb_unsent = sum(1 for p in collected.notebook_paths if str(p) not in sent_paths)

    summary: dict | None = None
    if fig_unsent or tab_unsent or nb_unsent or (always_anchor and run_dir is not None):
        summary = {
            "type": "output_summary",
            "figures": fig_unsent,
            "tables": tab_unsent,
            "notebooks": nb_unsent,
            "run_dir": str(run_dir) if run_dir is not None else "",
        }

    pending_items: list[dict] = list(sent)
    if summary is not None:
        pending_items.append(summary)
    return MediaDeliveryPlan(pending_items=pending_items, sent_names=sent_names, summary=summary)


# Auto-routing disambiguation block: emitted when the capability resolver's
# top-2 candidates are within ~``_AUTO_DISAMBIGUATE_GAP`` of each other.
# Tuned against ``capability_resolver._candidate_score`` output magnitudes
# (single keyword match is worth ~0.85 points; an alias hit is worth ~10).
_AUTO_DISAMBIGUATE_GAP = 2.0


def _format_auto_disambiguation(decision, query_text: str) -> str:
    """Return a human-readable disambiguation block for close-tie auto routing."""
    from omicsclaw.runtime.agent.state import _skill_registry  # late import — defined later in omicsclaw.runtime.agent.state

    candidates = list(decision.skill_candidates or [])[:3]
    if not candidates:
        return ""
    reg = _skill_registry()
    lines = [
        "🤔 **Auto-routing found multiple close candidates** — I won't execute yet.",
        f"Query: `{query_text.strip()[:200]}`",
        "",
        "**Top candidates (score — higher is better):**",
    ]
    for i, c in enumerate(candidates, 1):
        info = reg.skills.get(c.skill, {}) or {}
        desc = (info.get("description") or "").strip().replace("\n", " ")
        if len(desc) > 140:
            desc = desc[:137] + "…"
        reason = c.reasons[0] if c.reasons else ""
        lines.append(f"{i}. `{c.skill}` (score {c.score:.2f}) — {desc}")
        if reason:
            lines.append(f"   matched: {reason}")
    lines.extend([
        "",
        "**Next step:** re-invoke `omicsclaw` with `skill='<chosen alias above>'` "
        "and the same `mode`/`query`. Pick based on the user's data modality "
        "(H&E+coordinates → spatial; h5ad single-cell counts → singlecell; "
        "bulk counts csv → bulkrna; raw MS/LC-MS → proteomics/metabolomics).",
    ])
    return "\n".join(lines)


def _format_auto_route_banner(decision) -> str:
    """Return a short banner prepended to tool output when auto routing chose a skill."""
    chosen = decision.chosen_skill
    conf = float(getattr(decision, "confidence", 0.0) or 0.0)
    candidates = list(decision.skill_candidates or [])
    alts = [c.skill for c in candidates[1:3] if c.skill != chosen]
    alt_str = f" Close alternatives: {', '.join(alts)}." if alts else ""
    return (
        f"📍 Auto-routed to `{chosen}` (confidence {conf:.2f}).{alt_str} "
        "If this doesn't match the user's intent, re-invoke with an explicit `skill`.\n---\n"
    )


# ===========================================================================
# Memory auto-capture, env-error parsing, output state, skill execution.
# Migrated from bot/core.py per ADR 0001 (#119 reduced-scope follow-up).
#
# These were the internal helpers that no external test imports directly —
# the regression net (tests/test_bot_*.py + tests/test_oauth_regressions.py
# + tests/test_app_server.py + tests/test_notebook_files.py + tests/bot/)
# is the contract.
# ===========================================================================

import asyncio
import logging
import re
import threading
from datetime import datetime

from omicsclaw.common.report import build_output_dir_name
from omicsclaw.common.user_guidance import (
    extract_user_guidance_lines,
    render_guidance_block,
    strip_user_guidance_lines,
)
from omicsclaw.skill.registry import registry
from omicsclaw.skill.chain import (
    normalize_extra_args as _normalize_extra_args,
    run_omics_skill_step,
    run_skill_via_shared_runner as _run_skill_via_shared_runner,
)
from omicsclaw.skill.lookup import lookup_skill_info

logger = logging.getLogger("omicsclaw.omicsclaw.skill.orchestration")


# ---------------------------------------------------------------------------
# Memory auto-capture helpers
# ---------------------------------------------------------------------------


_MAX_ARTIFACT_NAMES = 200


def _output_artifact_names(output_dir: Path) -> list[str]:
    """Top-level output file names in a run dir (AN-PROV-CAPTURE-13 — provenance
    references files by name; bulky contents stay on disk). Capped at
    ``_MAX_ARTIFACT_NAMES`` so a run dir with thousands of files can't bloat the
    memory record — ``output_path`` remains the source of truth for the full set;
    a truncation marker signals when more exist. Empty on any error."""
    try:
        names = sorted(p.name for p in Path(output_dir).iterdir() if p.is_file())
    except Exception:
        return []
    if len(names) > _MAX_ARTIFACT_NAMES:
        extra = len(names) - _MAX_ARTIFACT_NAMES
        return names[:_MAX_ARTIFACT_NAMES] + [f"…(+{extra} more files; see output_path)"]
    return names


def _param_values_equal(a: object, b: object) -> bool:
    """Equality tolerant of the YAML→JSON type drift between a SKILL.md param-hint
    default and a run's effective param (AN-PROV-CAPTURE-13): ``7 == 7.0`` and
    ``"0.05" == 0.05`` must NOT read as overrides. Falls back to exact equality for
    non-numerics."""
    if a == b:  # exact (also covers 7 == 7.0 and True == 1 via Python equality)
        return True
    try:
        return float(a) == float(b)  # type: ignore[arg-type]  ("0.05" vs 0.05, "7" vs 7)
    except (TypeError, ValueError):
        return False


def _assisted_param_decision(skill: str, method: str, effective_params: dict) -> dict | None:
    """Compare the run's effective params to the SKILL.md param-hint DEFAULTS for
    (skill, method) — the provenance "method-choice" signal (AN-PROV-CAPTURE-13).

    Returns ``{method, basis, recommended, accepted, overrides}`` or ``None`` when
    the skill/method has no param-hint recommendation.

    PRECISION (ADR 0015): ``basis="skill_md_param_hint_defaults"`` — ``recommended``
    is the DETERMINISTIC SKILL.md default per param, recomputed at capture time. It
    is the deterministic *floor* of ADR 0015's recommendation, NOT the LLM's
    ephemeral, data-grounded recommendation actually shown to the agent (that would
    require instrumenting the loop's tool-result callback at show-time, a non-goal
    of the recompute-at-capture design). So ``accepted=True`` means the run used the
    SKILL.md defaults; an ``override`` means the run deviated from a default — which
    is a useful, honest provenance signal, but not literally "accepted/rejected the
    recommendation the agent saw". Future Write must read it with that basis in mind.
    """
    try:
        method_lower, tip_info, _ = _resolve_param_hint_info(skill, method)
        if not isinstance(tip_info, dict) or not tip_info:
            return None
        defaults = tip_info.get("defaults", {}) or {}
        params = tip_info.get("params", []) or []
        recommended = {p: defaults[p] for p in params if p in defaults}
        if not recommended:
            return None
        overrides: dict = {}
        for p, rec_val in recommended.items():
            if p in effective_params and not _param_values_equal(effective_params[p], rec_val):
                overrides[p] = {"recommended": rec_val, "effective": effective_params[p]}
        return {
            "method": method_lower,
            "basis": "skill_md_param_hint_defaults",
            "recommended": recommended,
            "accepted": not overrides,
            "overrides": overrides,
        }
    except Exception as exc:
        logger.debug("assisted-param decision recompute failed (non-fatal): %s", exc)
        return None


async def _auto_capture_dataset(
    session_id: str, input_path: str, data_type: str = "", thread_id: str = ""
):
    """Auto-capture dataset memory when a file is processed.

    ``thread_id`` (Bench, ADR 0018) scopes the dataset under the active
    investigation thread (``dataset://<thread_id>/<basename>``) so Analyze in
    that thread can reference it; empty preserves the legacy un-scoped URI.
    """
    from omicsclaw.runtime.agent.state import OMICSCLAW_DIR, memory_store
    if not memory_store or not session_id or not input_path:
        return

    try:
        from omicsclaw.memory.compat import DatasetMemory

        try:
            rel_path = str(Path(input_path).relative_to(OMICSCLAW_DIR))
        except ValueError:
            rel_path = Path(input_path).name

        n_obs = None
        n_vars = None
        try:
            suffix = Path(input_path).suffix.lower()
            if suffix in (".h5ad",):
                import h5py
                with h5py.File(input_path, "r") as h5:
                    if "obs" in h5 and hasattr(h5["obs"], "attrs"):
                        shape = h5["obs"].attrs.get("_index", h5["obs"].attrs.get("encoding-type", None))
                    if "X" in h5:
                        x = h5["X"]
                        if hasattr(x, "shape"):
                            n_obs, n_vars = x.shape
        except Exception:
            pass

        ds_mem = DatasetMemory(
            file_path=rel_path,
            platform=data_type or None,
            n_obs=n_obs,
            n_vars=n_vars,
            preprocessing_state="raw",
            thread_id=thread_id,
        )
        await memory_store.save_memory(session_id, ds_mem)
        logger.debug(f"Auto-captured dataset: {rel_path}")
    except Exception as e:
        logger.warning(f"Auto-capture dataset failed: {e}")


async def _capture_thread_source(
    session_id: str, thread_id: str, slug: str, source_page: str = ""
) -> None:
    """Record a thread<->KG-source link for per-thread grounding (批7, ADR 0019/0021).

    Writes a ``ThreadSourceMemory`` at ``thread_source://<thread_id>/<slug>`` so the
    thread's Read/Ideate surfaces can enumerate the sources it ingested and pass
    them as the formalize citation allow-list. One independent overwrite-mode node
    per (thread, source) — no read-modify-write (mirrors ``_auto_capture_dataset``).

    Best-effort and never raises: skips when memory is disabled (``memory_store``
    is None), the session/thread is unknown, or no ``slug`` was produced (KG/LLM
    absent, or a cache hit that could not recover one). The broad ``except`` also
    swallows the ``LookupError`` ``_client_for_session`` raises for an unknown
    session — the caller (a fire-and-forget ingest) must not break over it.
    """
    from omicsclaw.runtime.agent.state import memory_store
    if not memory_store or not session_id or not thread_id or not slug:
        return
    try:
        from omicsclaw.memory.compat import ThreadSourceMemory

        await memory_store.save_memory(
            session_id,
            ThreadSourceMemory(thread_id=thread_id, slug=slug, source_page=source_page or ""),
        )
        logger.debug("Captured thread source: %s/%s", thread_id, slug)
    except Exception as e:
        logger.warning(f"Capture thread source failed: {e}")


async def _auto_capture_analysis(
    session_id: str,
    skill: str,
    args: dict,
    output_dir: Path,
    success: bool,
    thread_id: str = "",
):
    """Auto-capture analysis memory after skill execution.

    ``thread_id`` (Bench, ADR 0018) scopes the captured run's lineage under the
    active investigation thread (``analysis://<thread_id>/<skill>/<id>``); empty
    preserves the legacy un-scoped URI.

    AN-PROV-CAPTURE-13 (ADR 0022): on a successful run we also read the skill's
    ``result.json`` ({version, input_checksum, data.params}) and record the
    effective params, checksum, version, output artifact names, and the
    assisted-parameterization decision (recommendation vs effective) so the Write
    phase has a queryable, memory-resident provenance record. Failed runs keep the
    legacy lightweight record.
    """
    from omicsclaw.runtime.agent.state import memory_store
    if not memory_store or not session_id:
        return

    try:
        from omicsclaw.memory.compat import AnalysisMemory

        method = args.get("method", "default")
        input_path = args.get("file_path", "")

        source_dataset_id = ""
        try:
            datasets = await memory_store.get_memories(session_id, "dataset", limit=1)
            if datasets:
                source_dataset_id = datasets[0].memory_id
        except Exception:
            pass

        # AN-PROV-CAPTURE-13 — post-run provenance from result.json + the
        # recompute-at-capture assisted-parameterization decision.
        effective_params: dict[str, Any] = {}
        input_checksum = ""
        skill_version = ""
        artifacts: list[str] = []
        if success and output_dir:
            result_json = _read_result_json(output_dir)
            if result_json:
                skill_version = str(result_json.get("version") or "")
                input_checksum = str(result_json.get("input_checksum") or "")
                data = result_json.get("data") or {}
                if isinstance(data, dict) and isinstance(data.get("params"), dict):
                    effective_params = dict(data["params"])
            artifacts = _output_artifact_names(output_dir)
        decision = _assisted_param_decision(skill, method, effective_params)

        memory = AnalysisMemory(
            source_dataset_id=source_dataset_id if source_dataset_id else "",
            skill=skill,
            method=method,
            parameters={"input": input_path} if input_path else {},
            output_path=str(output_dir) if output_dir else "",
            status="completed" if success else "failed",
            thread_id=thread_id,
            effective_params=effective_params,
            input_checksum=input_checksum,
            skill_version=skill_version,
            artifacts=artifacts,
            assisted_param_decision=decision,
        )

        await memory_store.save_memory(session_id, memory)
        logger.debug(f"Auto-captured analysis: {skill} ({method})")
    except Exception as e:
        logger.warning(f"Auto-capture analysis failed: {e}")


# P4 (docs/proposals/skill-acquisition-plan.md §P4): adaptive promotion
# signal — after a successful autonomous-analysis (mini-agent) run, notice
# when a SIMILAR goal has already succeeded before in the same thread and
# proactively suggest promoting it to a reusable skill, instead of relying
# purely on the user explicitly asking. Sibling of ``_auto_capture_analysis``
# above, but for the free-form code-loop flow (ADR 0032) rather than fixed
# skill execution — no ``skill``/``method`` identity, just a free-text goal.

# 3rd occurrence of a similar goal (2 PRIOR successes + this one) reads as a
# pattern rather than a coincidence. Named so it's easy to retune later
# without re-deriving the reasoning.
_PROMOTION_SUGGESTION_MIN_PRIOR_SUCCESSES = 2
# Hand-verified against realistic goal text: a genuine reword of the same
# goal scores ~0.6-0.8, an unrelated goal scores ~0.0-0.1.
_GOAL_SIMILARITY_THRESHOLD = 0.5
_GOAL_SIMILARITY_STOPWORDS = frozenset(
    {"the", "a", "an", "and", "by", "in", "of", "to", "for", "with", "on"}
)


def _ordinal(n: int) -> str:
    """"1st"/"2nd"/"3rd"/"4th"/... — 11th/12th/13th are the "teen" exception."""
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _goal_similarity(a: str, b: str) -> float:
    """Jaccard token overlap between two goal strings, stopwords excluded.

    Reuses the memory subsystem's own tokenizer (``SearchTokenizer`` —
    handles normalization + CJK segmentation) so this stays consistent with
    how the rest of the system tokenizes text, rather than a second scheme.
    Deliberately NOT an FTS5 search: ``SearchIndexer``'s ``MATCH`` query ANDs
    every tokenized term with no stopword removal, so two genuinely-reworded
    goals often fail to match on a missing connector word — too strict for
    "has the user done something like this before."  No embedding-based
    semantic similarity either: no such infra exists in this codebase, and
    this deterministic token-overlap heuristic is proportionate to the ask.
    """
    from omicsclaw.memory.search_terms import SearchTokenizer

    tokens_a = {t.lower() for t in SearchTokenizer.tokenize(a)} - _GOAL_SIMILARITY_STOPWORDS
    tokens_b = {t.lower() for t in SearchTokenizer.tokenize(b)} - _GOAL_SIMILARITY_STOPWORDS
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


async def _auto_capture_autonomous_run(
    session_id: str,
    thread_id: str,
    goal: str,
    run_id: str,
    workspace_root: str,
    raw_status: str,
) -> None:
    """Record an autonomous mini-agent run's lineage (success or failure).

    Mirrors ``_auto_capture_analysis``'s early-return-if-disabled pattern.
    Records EVERY completed run, not just successes — only successes count
    toward the promotion signal (``_compute_promotion_suggestion``), but
    capturing failures too means a future consumer (e.g. "N failures then a
    success") doesn't need a backfill.
    """
    from omicsclaw.runtime.agent.state import memory_store
    if not memory_store or not session_id:
        return

    try:
        from omicsclaw.memory.compat import AutonomousRunMemory

        memory = AutonomousRunMemory(
            goal=goal,
            run_id=run_id,
            workspace_root=workspace_root,
            status="succeeded" if raw_status == "succeeded" else "failed",
            raw_status=raw_status,
            thread_id=thread_id,
        )
        await memory_store.save_memory(session_id, memory)
        logger.debug(f"Auto-captured autonomous run: {run_id} ({raw_status})")
    except Exception as e:
        logger.warning(f"Auto-capture autonomous run failed: {e}")


async def _compute_promotion_suggestion(
    session_id: str,
    thread_id: str,
    goal: str,
    run_id: str,
    workspace_root: str,
) -> str | None:
    """Suggest promoting to a skill when a similar goal has succeeded before.

    Only meaningful to call after a SUCCESSFUL run (the caller checks
    ``result.ok``). Fetches this thread's prior ``autonomous_run`` records
    (``get_memories``, not the FTS5 ``search_memories`` — see
    ``_goal_similarity``'s docstring for why), counts those that succeeded
    and are similar to ``goal`` (excluding ``run_id`` itself, in case this
    exact run was somehow already captured), and — if at least
    ``_PROMOTION_SUGGESTION_MIN_PRIOR_SUCCESSES`` qualify — returns a
    markdown suggestion naming ``workspace_root`` as the exact
    ``source_analysis_dir`` for ``execute_create_omics_skill``.

    Deliberately never mentions ``promote_from_latest``: that still resolves
    via ``find_latest_autonomous_analysis``'s global mtime scan across ALL
    sessions' output directories, which is exactly the concurrent-session
    race this feature must not reintroduce — anchoring to this run's own
    ``workspace_root`` (known immediately after the run, no disk scan)
    keeps the suggestion correct even with other sessions running at once.
    """
    from omicsclaw.runtime.agent.state import memory_store
    if not memory_store or not session_id:
        return None
    if not thread_id:
        # get_memories treats an empty thread_id as "no filter" (by design,
        # for its general listing use — see its own docstring), which would
        # silently let this feature cross-contaminate suggestions across
        # UNRELATED threads/sessions for any caller that has no Bench thread
        # context. The plan requires thread-scoped only; without a real
        # thread_id there's no safe scope to count within, so decline rather
        # than guess.
        return None

    try:
        prior = await memory_store.get_memories(
            session_id, "autonomous_run", limit=50, thread_id=thread_id
        )
    except Exception as e:
        logger.warning(f"Promotion-suggestion lookup failed: {e}")
        return None

    similar_successes = sum(
        1
        for record in prior
        if getattr(record, "run_id", "") != run_id
        and getattr(record, "status", "") == "succeeded"
        and _goal_similarity(goal, getattr(record, "goal", "")) >= _GOAL_SIMILARITY_THRESHOLD
    )
    if similar_successes < _PROMOTION_SUGGESTION_MIN_PRIOR_SUCCESSES:
        return None

    occurrence = similar_successes + 1
    # repr() (not raw f-string interpolation) so a quote character inside the
    # goal text can't produce a syntactically broken/misleading snippet —
    # e.g. a goal containing `"tumor"` must not close the string early.
    return (
        "## Promotion candidate\n\n"
        f"This is the {_ordinal(occurrence)} time a similar goal has succeeded in "
        "this thread. Consider promoting it to a reusable skill:\n\n"
        "```\n"
        f"execute_create_omics_skill(request={goal!r}, source_analysis_dir={workspace_root!r})\n"
        "```\n\n"
        "(Anchored to this exact run's own workspace, not `promote_from_latest` — "
        "stays correct even if other analyses are running concurrently.)"
    )


async def _auto_capture_consensus(
    session_id: str,
    skill: str,
    output_dir: Path,
    success: bool,
    thread_id: str = "",
) -> bool:
    """Auto-capture a typed-consensus run's lineage at its canonical URI.

    AN-ROUTER-10 (Bench, ADR 0010/0018): a consensus skill (``consensus-domains``
    / ``sc-consensus-clustering``) runs in a subprocess that writes only disk
    artifacts — the in-loop agent is the only place that holds BOTH the active
    ``thread_id`` and the graph-memory store. After a successful run we record
    the run's lineage at the canonical, thread-scoped consensus namespace
    ``analysis://<thread_id>/typed/<run_id>`` (``consensus_namespace`` — ADR 0010:
    meta-analysis reads ``typed/*``; ADR 0018: thread scoping). Empty
    ``thread_id`` keeps the legacy un-scoped ``analysis://typed/<run_id>``.

    Returns ``True`` when this WAS a registered consensus flavour and the lineage
    was captured, so the caller skips the generic ``_auto_capture_analysis``
    (one record per run, at its canonical URI); ``False`` otherwise (the caller
    falls back to the generic capture). A failed run also returns ``False``: a
    non-run has no verified ``typed/`` lineage, so failures stay on the generic
    per-skill capture.

    This is the lightweight lineage marker (a plain ``AnalysisMemory`` at the
    canonical URI, with the real consensus skill name); the richer
    ``TypedConsensusRun`` provenance index (effective params, checksums, the
    assisted-parameterization decision) lands at the SAME URI in
    AN-PROV-CAPTURE-13.
    """
    from omicsclaw.runtime.agent.state import memory_store

    try:
        from omicsclaw.runtime.consensus.sources import CONSENSUS_SOURCES
    except Exception:
        return False
    source = CONSENSUS_SOURCES.get(skill)
    if source is None:
        # Not a consensus flavour — the caller uses the generic capture.
        return False
    if not memory_store or not session_id or not success or not output_dir:
        return False

    try:
        import json

        from omicsclaw.memory.compat import AnalysisMemory
        from omicsclaw.runtime.consensus.dispatch import consensus_namespace
        from omicsclaw.runtime.consensus.templates import provenance_of

        mode = "typed" if provenance_of(source.template) == "typed" else "narrative"

        # run_id + operator come from the driver's plan.json audit (run.py); the
        # agent loop never passes --run-id, so run_id == output_dir.name, but we
        # read the audit to honour an explicit --run-id if one is ever wired.
        run_id = output_dir.name
        operator = "kmode"
        # AN-PROV-CAPTURE-13 — the consensus run's effective config IS the plan
        # audit (operator + planned members + score weights); there is no SKILL.md
        # param-hint recommendation for a planner-driven flavour, so the
        # assisted-parameterization decision stays None.
        effective_params: dict[str, Any] = {}
        try:
            plan = json.loads((output_dir / "plan.json").read_text(encoding="utf-8"))
            run_id = str(plan.get("run_id") or run_id)
            operator = str(plan.get("operator") or operator)
            effective_params = {
                k: plan[k]
                for k in (
                    "operator", "members", "alpha", "beta",
                    # ``max_class_fraction_cap`` is the current plan.json key for the
                    # hard-filter threshold; ``max_class_frac`` kept for older plans.
                    "max_class_frac", "max_class_fraction_cap",
                    # Panel family + weights drive panel-based rankings (ADR 0028/0029).
                    "intrinsic_panel", "panel_weights",
                )
                if k in plan
            }
        except Exception:
            pass
        if not run_id:
            return False
        effective_params.setdefault("operator", operator)

        source_dataset_id = ""
        try:
            datasets = await memory_store.get_memories(
                session_id, "dataset", limit=1, thread_id=thread_id
            )
            if datasets:
                source_dataset_id = datasets[0].memory_id
        except Exception:
            pass

        memory = AnalysisMemory(
            memory_id=run_id,
            source_dataset_id=source_dataset_id,
            skill=skill,
            method=operator,
            parameters={"run_id": run_id, "consensus_mode": mode},
            output_path=str(output_dir),
            status="completed",
            thread_id=thread_id,
            effective_params=effective_params,
            artifacts=_output_artifact_names(output_dir),
        )
        # Land at the explicit consensus URI (which the AnalysisMemory's own
        # <skill>/<id> path shape can't express) via the same per-session client
        # CompatMemoryStore.save_memory uses — no new store API, no model change.
        client = await memory_store._client_for_session(session_id)
        await client.remember(
            uri=consensus_namespace(run_id, mode, thread_id),
            content=memory.model_dump_json(),
            disclosure=f"Consensus lineage from session {session_id}",
        )
        logger.debug(
            f"Auto-captured consensus lineage: {skill} run {run_id} ({mode})"
        )
        return True
    except Exception as e:
        logger.warning(f"Auto-capture consensus failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Env-error parsing
# ---------------------------------------------------------------------------


_ENV_ERROR_PATTERNS: list[tuple[str, str]] = [
    (r"compiled using NumPy 1\.x",                    "NumPy 版本冲突"),
    (r"cannot be run in\s+NumPy\s+\d",               "NumPy 版本冲突"),
    (r"downgrade to ['\"]?numpy[<>=]",                "NumPy 版本冲突"),
    (r"CUDA.*out of memory|out of memory.*CUDA",      "GPU 显存不足"),
    (r"Rscript[:\s]+command not found",               "R 未安装或不在 PATH 中"),
    (r"Rscript.*not found",                           "R 未安装或不在 PATH 中"),
    (r"cannot find R",                                "R 未安装或不在 PATH 中"),
    (r"there is no package called",                   "缺少 R 包"),
    (r"No space left on device",                      "服务器磁盘空间不足"),
    (r"(?<!\w)Killed(?!\w)",                          "进程被 OOM Killer 终止（内存不足）"),
    (r"cannot allocate.*memory|MemoryError",          "内存不足"),
    (r"ModuleNotFoundError",                          "缺少 Python 包"),
    (r"ImportError",                                  "缺少 Python 包（版本冲突或未安装）"),
]


def _extract_env_snippet(full_err: str) -> str:
    """Show the beginning (package/file name) and end (error message) of stderr."""
    lines = [l for l in full_err.strip().splitlines() if l.strip()]
    if len(lines) <= 15:
        return full_err.strip()
    head = "\n".join(lines[:6])
    tail = "\n".join(lines[-8:])
    return f"{head}\n...\n{tail}"


def _extract_fix_hint(label: str, err: str) -> str:
    """Return a specific, actionable fix command based on the error type and content."""
    if "NumPy" in label:
        return "pip install 'numpy<2'  # 或: conda install 'numpy<2'"
    if "R 未安装" in label:
        return "conda install -c conda-forge r-base  # 或: sudo apt install r-base"
    if "磁盘" in label:
        return "df -h  # 检查磁盘空间，清理 output/ 或 /tmp 下的大文件"
    if "OOM" in label:
        return "增加服务器内存，或减小输入数据规模后重试"
    if "GPU" in label:
        return "减少 batch_size，或在 extra_args 中加 --device cpu 使用 CPU 模式"
    if "内存不足" in label:
        return "增加服务器内存，或减小输入数据规模后重试"
    if "R 包" in label:
        r_pkgs = re.findall(r"there is no package called '([^']+)'", err)
        if r_pkgs:
            install_cmd = ", ".join(f'"{p}"' for p in r_pkgs)
            return f"Rscript -e 'install.packages(c({install_cmd}))'"
        return "Rscript -e 'install.packages(\"<包名>\")' # 检查上方报错确认具体包名"
    m = re.search(r"No module named ['\"]?([a-zA-Z0-9_\-\.]+)['\"]?", err)
    if m:
        pkg = m.group(1).split(".")[0]
        return f"pip install {pkg}  # 或: conda install {pkg}"
    return "pip install <缺少的包名>  # 检查上方报错确认具体包名"


def _classify_env_error(err: str) -> str | None:
    """Return a user-friendly message if the error is environment-related, else None."""
    for pattern, label in _ENV_ERROR_PATTERNS:
        if re.search(pattern, err, re.IGNORECASE):
            snippet = _extract_env_snippet(err)
            fix = _extract_fix_hint(label, err)
            return (
                f"**环境错误（不是你的数据问题）: {label}**\n\n"
                "分析环境有配置问题，你的数据文件完全没问题。\n\n"
                f"**修复方法（在终端运行）:**\n```\n{fix}\n```\n\n"
                f"**技术详情:**\n```\n{snippet}\n```"
            )
    return None


# ---------------------------------------------------------------------------
# Output state helpers
# ---------------------------------------------------------------------------


async def _resolve_last_output_dir(session_id: str, skill: str) -> Path | None:
    """Find the most recent completed output directory for a skill from session memory."""
    from omicsclaw.runtime.agent.state import memory_store
    if not memory_store or not session_id:
        return None
    try:
        from omicsclaw.memory.compat import AnalysisMemory
        analyses = await memory_store.get_memories(session_id, "analysis", limit=20)
        for mem in analyses:
            if (
                isinstance(mem, AnalysisMemory)
                and mem.skill == skill
                and mem.output_path
                and getattr(mem, "status", None) == "completed"
            ):
                p = Path(mem.output_path)
                if p.exists():
                    return p
    except Exception as e:
        logger.debug(f"_resolve_last_output_dir failed: {e}")
    return None


def _read_result_json(out_dir: Path) -> dict | None:
    """Read result.json from the skill output directory, return parsed dict or None."""
    result_path = out_dir / "result.json"
    if not result_path.exists():
        return None
    try:
        import json as _json
        return _json.loads(result_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug(f"Failed to read result.json: {e}")
        return None


async def _update_preprocessing_state(session_id: str, result_data: dict):
    """Update the current dataset's preprocessing_state from result.json data."""
    from omicsclaw.runtime.agent.state import memory_store
    if not memory_store or not session_id:
        return
    new_state = result_data.get("preprocessing_state_after")
    if not new_state:
        return
    try:
        datasets = await memory_store.get_memories(session_id, "dataset", limit=1)
        if not datasets:
            return
        ds = datasets[0]
        if ds.preprocessing_state == new_state:
            return
        ds.preprocessing_state = new_state
        await memory_store.save_memory(session_id, ds)
        logger.info(f"Updated preprocessing_state: {new_state} for {ds.file_path}")
    except Exception as e:
        logger.warning(f"Failed to update preprocessing_state: {e}")


def _format_next_steps(result_data: dict) -> str:
    """Format next_steps from result.json data into a user-friendly recommendation block."""
    next_steps = result_data.get("next_steps")
    if not next_steps:
        return ""
    if isinstance(next_steps, list) and all(isinstance(s, str) for s in next_steps):
        lines = ["\n**Suggested next steps:**"]
        for step in next_steps:
            lines.append(f"- {step}")
        return "\n".join(lines)
    if isinstance(next_steps, list) and all(isinstance(s, dict) for s in next_steps):
        lines = ["\n**Suggested next steps:**"]
        for step in next_steps:
            skill_name = step.get("skill", "")
            desc = step.get("description", "")
            priority = step.get("priority", "")
            tag = f" ({priority})" if priority else ""
            if skill_name and desc:
                lines.append(f"- **{skill_name}** — {desc}{tag}")
            elif skill_name:
                lines.append(f"- **{skill_name}**{tag}")
            else:
                lines.append(f"- {desc}{tag}")
        lines.append("\nTell me which one you'd like to run!")
        return "\n".join(lines)
    return ""


# ---------------------------------------------------------------------------
# Skill execution + lookup
# ---------------------------------------------------------------------------


async def _run_omics_skill_step(**kwargs) -> dict:
    """Bot-side adapter for ``run_omics_skill_step``: defaults the
    output root to the bot's ``OUTPUT_DIR`` so existing callers don't
    have to thread the path through."""
    from omicsclaw.runtime.agent.state import OUTPUT_DIR

    kwargs.setdefault("output_root", OUTPUT_DIR)
    return await run_omics_skill_step(**kwargs)


# Re-export from the canonical home (omicsclaw.skill.lookup) so
# existing ``omicsclaw.skill.orchestration._lookup_skill_info`` / ``omicsclaw.runtime.agent.state``
# call sites keep working without churn.
_lookup_skill_info = lookup_skill_info


def load_skill_md(skill_key: str, *, max_chars: int = 8000) -> str:
    """Return the matched skill's ``SKILL.md`` text, capped, as the method menu.

    Per ADR 0015, exact-skill assisted parameterization feeds the matched
    skill's SKILL.md (its method menu, defaults, parameters, preconditions) into
    the outer LLM as a deterministic input. Returns ``""`` when the skill or its
    SKILL.md is missing — callers treat that as "no menu to inject" and degrade
    gracefully rather than failing the turn.
    """
    try:
        info = _lookup_skill_info(skill_key, force_reload=False) or {}
        script = info.get("script")
        if not script:
            return ""
        skill_md_path = Path(script).parent / "SKILL.md"
        if not skill_md_path.is_file():
            return ""
        text = skill_md_path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as exc:
        logger.warning("load_skill_md(%s) failed (non-fatal): %s", skill_key, exc)
        return ""
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n\n…(SKILL.md truncated)…"
    return text


def _resolve_param_hint_info(skill_key: str, method: str) -> tuple[str, dict, dict]:
    """Return (method_lower, tip_info, skill_info) from SKILL.md param_hints."""
    method_lower = (method or "").lower().strip()
    if not method_lower:
        return "", {}, {}

    try:
        skill_info = _lookup_skill_info(skill_key, force_reload=False)
        hints = skill_info.get("param_hints", {}) if skill_info else {}
        tip_info = hints.get(method_lower)

        if not tip_info:
            skill_info = _lookup_skill_info(skill_key, force_reload=True)
            hints = skill_info.get("param_hints", {}) if skill_info else {}
            tip_info = hints.get(method_lower)

        logger.info(
            "param_hint lookup: skill_key=%s, method=%s, skill_found=%s, hints_keys=%s",
            skill_key,
            method_lower,
            bool(skill_info),
            list(hints.keys()) if hints else "EMPTY",
        )
        return method_lower, (tip_info or {}), (skill_info or {})
    except Exception as e:
        logger.warning("param_hint loading failed: %s", e)
        return method_lower, {}, {}


def _infer_skill_for_method(method: str, preferred_domain: str = "") -> str:
    """Infer a skill alias from method name using registry param_hints."""
    method_lower = (method or "").strip().lower()
    if not method_lower:
        return ""
    try:
        from omicsclaw.runtime.agent.state import _skill_registry
        skill_registry = _skill_registry()

        candidates: list[str] = []
        for alias, info in skill_registry.skills.items():
            if alias != info.get("alias", alias):
                continue
            if preferred_domain and info.get("domain", "") != preferred_domain:
                continue
            hints = info.get("param_hints", {}) or {}
            if method_lower in hints:
                candidates.append(alias)

        if len(candidates) == 1:
            return candidates[0]
        return sorted(candidates)[0] if candidates else ""
    except Exception:
        return ""


def _build_method_preview(
    *,
    skill_key: str,
    method: str,
    n_obs: int | None,
    has_spatial: bool,
    has_x_pca: bool,
    has_raw: bool,
    has_counts_layer: bool,
    platform: str,
) -> str:
    """Build pre-run suitability + default parameter preview for inspect_data."""
    method_lower, tip_info, skill_info = _resolve_param_hint_info(skill_key, method)
    if not method_lower or not tip_info:
        return ""

    checks: list[str] = []
    seen_checks: set[str] = set()

    def _add_check(line: str) -> None:
        if line not in seen_checks:
            checks.append(line)
            seen_checks.add(line)

    suitable = True

    requires = tip_info.get("requires", []) if isinstance(tip_info, dict) else []
    requires_tokens = {str(r).strip().lower() for r in requires}

    if "spatial" in platform.lower() and "obsm.spatial" not in requires_tokens:
        if has_spatial:
            _add_check("- `obsm['spatial']`: found ✅")
        else:
            _add_check("- `obsm['spatial']`: missing ❌ (spatial methods usually require this)")
            suitable = False

    for req in requires:
        req_token = str(req).strip().lower()
        if req_token == "obsm.spatial":
            ok = has_spatial
            text = "`obsm['spatial']`"
        elif req_token == "obsm.x_pca":
            ok = has_x_pca
            text = "`obsm['X_pca']`"
        elif req_token == "raw":
            ok = has_raw
            text = "`adata.raw`"
        elif req_token == "layers.counts":
            ok = has_counts_layer
            text = "`layers['counts']`"
        elif req_token == "raw_or_counts":
            ok = has_raw or has_counts_layer
            text = "`adata.raw` or `layers['counts']`"
        else:
            _add_check(f"- Requirement `{req}`: please verify manually")
            continue

        if ok:
            _add_check(f"- Requirement {text}: found ✅")
        else:
            _add_check(f"- Requirement {text}: missing ⚠️")
            suitable = False

    if has_x_pca:
        _add_check("- `obsm['X_pca']`: found ✅")
    if isinstance(n_obs, int):
        size_note = f"{n_obs:,} cells/spots"
        if n_obs > 30000 and "epochs" in (tip_info.get("params", []) or []):
            size_note += " (large; start with fewer epochs)"
        _add_check(f"- Dataset size: {size_note}")

    defaults = tip_info.get("defaults", {}) if isinstance(tip_info, dict) else {}
    param_lines = []
    recommended_args = [f"--method {method_lower}"]
    for p in tip_info.get("params", []):
        default_val = defaults.get(p, "default")
        if (
            p == "epochs"
            and isinstance(n_obs, int)
            and n_obs > 30000
        ):
            default_text = f"{default_val} (for large data, try 50-100 first)"
            recommended_val = 50
        else:
            default_text = str(default_val)
            recommended_val = default_val
        param_lines.append(f"- `{p}`: {default_text}")

        if recommended_val != "default":
            cli_flag = f"--{p.replace('_', '-')}"
            recommended_args.append(f"{cli_flag} {recommended_val}")

    suitability_text = "✅ Suitable for a first run" if suitable else "❌ Not suitable yet"
    canonical_alias = skill_info.get("alias", skill_key) if skill_info else skill_key

    lines = [
        "### Method Suitability & Parameter Preview",
        f"- Requested skill: `{canonical_alias}`",
        f"- Requested method: `{method_lower}`",
        f"- Suitability: {suitability_text}",
    ]
    if checks:
        lines.append("- Checks:")
        lines.extend([f"  {c}" for c in checks])
    if param_lines:
        lines.append("- Default parameter preview:")
        lines.extend([f"  {p}" for p in param_lines])
    lines.append(f"- Tuning priority: {tip_info.get('priority', 'N/A')}")
    lines.append(f"- Suggested first run: `{ ' '.join(recommended_args) }`")
    return "\n".join(lines)


def _build_param_hint(skill_key: str, method: str, cmd: list[str]) -> str:
    """Build a two-tier parameter card from SKILL.md-declared param_hints."""
    method_lower, tip_info, skill_info = _resolve_param_hint_info(skill_key, method)
    if not tip_info:
        hints = skill_info.get("param_hints", {}) if skill_info else {}
        logger.info("param_hint: no hints for method '%s' in %s", method_lower, list(hints.keys()))
        return ""

    params_found = {}
    for i, arg in enumerate(cmd):
        if arg.startswith("--") and i + 1 < len(cmd) and not cmd[i + 1].startswith("--"):
            params_found[arg.lstrip("-").replace("-", "_")] = cmd[i + 1]

    defaults_map: dict = tip_info.get("defaults", {})

    def _fmt_param(key: str) -> str:
        if key in params_found:
            return f"{key}={params_found[key]}"
        dval = defaults_map.get(key)
        return f"{key}={dval}" if dval is not None else f"{key}=default"

    core_keys: list[str] = tip_info.get("params", [])
    adv_keys: list[str] = tip_info.get("advanced_params", [])

    lines = [f"📋 **{skill_key} · {method_lower}**", ""]

    if core_keys:
        lines.append("核心参数: " + " | ".join(_fmt_param(k) for k in core_keys))
    if adv_keys:
        lines.append("高级参数: " + " | ".join(_fmt_param(k) for k in adv_keys))

    priority = tip_info.get("priority", "")
    if priority:
        lines.append(f"调参优先级: {priority.replace(' -> ', ' → ')}")

    for t in tip_info.get("tips", []):
        lines.append(f"  · {t}")

    lines.append("")
    lines.append(
        "[PARAM CARD: show the parameter lines above verbatim to the user — "
        "do not paraphrase, abbreviate, or omit the 高级参数 line]"
    )
    lines.append("")
    return "\n".join(lines)
