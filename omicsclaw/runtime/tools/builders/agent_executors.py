"""All ``execute_*`` async tool implementations + dispatch table.

Carved out of ``bot/core.py`` per ADR 0001 (#120). 24 OpenAI-function-
calling executors live here in a single-file checkpoint; per-tool split
is deferred to a follow-up. The dispatch builder
(``_available_tool_executors`` / ``_build_tool_runtime`` /
``get_tool_runtime`` / ``get_tool_executors``) also lives here so the
runtime stays close to its handlers.

Cross-module access pattern:

* Stable omicsclaw.runtime.agent.state symbols (path constants, registry, ``audit``,
  ``logger``, ``received_files`` and other in-place-mutated dicts)
  are imported at module top — works because omicsclaw.runtime.agent.state defines them
  before the re-export line that pulls in this module.
* Runtime-reassigned globals (``memory_store``, ``llm``,
  ``OMICSCLAW_MODEL``, ``LLM_PROVIDER_NAME``, ``session_manager``)
  are accessed via ``_core.<name>`` at call time so they see the
  values ``omicsclaw.runtime.agent.session.init()`` writes after the modules finish
  loading.
* Helpers from sibling modules (``omicsclaw.skill.orchestration``,
  ``omicsclaw.skill.preflight.sc_batch``) are imported from their
  canonical home, not via the omicsclaw.runtime.agent.state re-export — a clearer
  dependency graph.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import io
import json
import logging
from difflib import get_close_matches
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

# Late-binding handle for runtime-mutated omicsclaw.runtime.agent.state globals (memory_store,
# llm, OMICSCLAW_MODEL, LLM_PROVIDER_NAME, session_manager). Set by
# omicsclaw.runtime.agent.session.init() after the modules load.
import omicsclaw.runtime.agent.state as _core

# Stable omicsclaw.runtime.agent.state symbols — defined early in omicsclaw.runtime.agent.state, before this module
# is pulled in via re-export.
from omicsclaw.runtime.agent.state import (
    DATA_DIR,
    DEEP_LEARNING_METHODS,
    EXAMPLES_DIR,
    OMICSCLAW_DIR,
    OMICSCLAW_PY,
    OUTPUT_DIR,
    _path_names,
    audit,
    get_skill_runner_python,
    pending_media,
    pending_preflight_requests,
    received_files,
)

# Path-validation helpers carved out to omicsclaw.services.path_validation per ADR 0001.
# Import directly (not via omicsclaw.runtime.agent.state) since omicsclaw.runtime.agent.state only re-exports them
# *after* this module finishes loading — by the time these executors run,
# the omicsclaw.runtime.agent.state re-export has resolved but lookups inside execute_* bodies
# resolve against this module's globals, not omicsclaw.runtime.agent.state's.
from omicsclaw.services.path_validation import (
    TRUSTED_DATA_DIRS,
    _ensure_trusted_dirs,
    discover_file,
    resolve_dest,
    sanitize_filename,
    validate_input_path,
    validate_path,
)

from omicsclaw.common import run_paths
from omicsclaw.common.user_guidance import (
    extract_user_guidance_lines,
    extract_user_guidance_payloads,
    format_user_guidance_payload,
    render_guidance_block,
    strip_user_guidance_lines,
)
from omicsclaw.skill.registry import ensure_registry_loaded, registry
from omicsclaw.runtime.tools.builders.agent import build_bot_tool_registry, BotToolContext
from omicsclaw.runtime.tools.builders.engineering import build_engineering_tool_executors
from omicsclaw.runtime.tools.kg_tools import KG_TOOL_EXECUTORS
from omicsclaw.runtime.policy.verification import format_completion_mapping_summary

# Helpers from canonical homes (post-decomposition siblings).
from omicsclaw.skill.orchestration import (
    _AUTO_DISAMBIGUATE_GAP,
    _auto_capture_analysis,
    _auto_capture_consensus,
    _auto_capture_dataset,
    _capture_thread_source,
    _build_method_preview,
    _build_param_hint,
    _classify_env_error,
    _collect_output_media_paths,
    _format_auto_disambiguation,
    _format_auto_route_banner,
    _format_next_steps,
    _infer_skill_for_method,
    _lookup_skill_info,
    _normalize_extra_args,
    _read_result_json,
    _resolve_last_output_dir,
    _run_omics_skill_step,
    _run_skill_via_shared_runner,
    _update_preprocessing_state,
    OutputMediaPaths,
)
from omicsclaw.skill.preflight.sc_batch import (
    _auto_prepare_sc_batch_integration,
    _maybe_require_batch_integration_workflow,
    _maybe_require_batch_key_selection,
    _resolve_requested_batch_key,
)

logger = logging.getLogger("omicsclaw.omicsclaw.runtime.tools.builders.agent_executors")

# Returned by execute_remember / execute_recall / execute_forget when
# omicsclaw.runtime.agent.state.memory_store is None. Default is OMICSCLAW_MEMORY_ENABLED=true,
# so reaching this branch usually means init failed silently at startup
# (e.g. stale import, missing dep, unreachable DB) — the bot logs carry
# the actual reason. Naming the real env vars saves users a doc dive
# through .env.example.
_MEMORY_DISABLED_HINT = (
    "Memory system is not initialized. It defaults to enabled, so this "
    "usually means initialization failed silently at startup — check the "
    'bot logs for "Memory init failed" or "Memory dependencies not '
    'installed". Verify OMICSCLAW_MEMORY_ENABLED is not set to "false" '
    "and that OMICSCLAW_MEMORY_DB_URL points at a reachable database "
    '(see .env.example § "Graph Memory System").'
)


# Runtime-internal flags injected by the preflight chain (not part of
# the LLM-facing tool schema). Validator accepts them so auto-prep
# self-recursion via execute_omicsclaw doesn't trip its own guard, but
# they're omitted from error messages so the LLM doesn't try to set them.
_OMICSCLAW_INTERNAL_ARG_KEYS: frozenset[str] = frozenset({"confirmed_preflight"})


def _compute_omicsclaw_schema_arg_keys() -> frozenset[str]:
    """Derive the LLM-facing arg-key set from the tool spec.

    Single source of truth: adding a new property to the ``omicsclaw``
    tool spec auto-extends what the validator accepts and surfaces in
    error messages — no parallel hardcoded list to drift.
    """
    from omicsclaw.runtime.tools.builders.agent import BotToolContext, build_bot_tool_specs

    # Pass a placeholder domain_briefing so this import-time probe does NOT force a
    # registry load: an empty briefing makes build_bot_tool_specs call
    # build_domain_briefing(ensure_loaded=True) -> OmicsRegistry.load_all. We only
    # need the omicsclaw param-key set, which is briefing-independent.
    probe_ctx = BotToolContext(skill_names=(), domain_briefing="(schema-probe)")
    for spec in build_bot_tool_specs(probe_ctx):
        if spec.name == "omicsclaw":
            return frozenset(spec.parameters["properties"].keys())
    raise RuntimeError("omicsclaw tool spec missing from build_bot_tool_specs output")


_OMICSCLAW_SCHEMA_ARG_KEYS: frozenset[str] = _compute_omicsclaw_schema_arg_keys()
_OMICSCLAW_ALLOWED_ARG_KEYS: frozenset[str] = (
    _OMICSCLAW_SCHEMA_ARG_KEYS | _OMICSCLAW_INTERNAL_ARG_KEYS
)


def _validate_omicsclaw_args(args: dict) -> str:
    """Catch malformed ``omicsclaw`` tool args before any I/O.

    Returns an empty string when ``args`` only contains keys declared
    by the tool's OpenAI schema (plus a small set of runtime-internal
    flags); otherwise returns an LLM-readable error string naming the
    unknown keys and — when applicable — pointing at the closest
    matching schema key.
    """
    unknown = sorted(set(args.keys()) - _OMICSCLAW_ALLOWED_ARG_KEYS)
    if not unknown:
        return ""

    lines = [f"Unknown parameter(s) in omicsclaw call: {unknown}."]

    suggestions: list[str] = []
    for key in unknown:
        matches = get_close_matches(
            key, _OMICSCLAW_SCHEMA_ARG_KEYS, n=1, cutoff=0.6
        )
        if matches:
            suggestions.append(f"  '{key}' → did you mean '{matches[0]}'?")
    if suggestions:
        lines.append("Did you mean:")
        lines.extend(suggestions)

    if "params" in unknown:
        lines.append(
            "Note: the omicsclaw tool does not accept nested 'params'. "
            "Pass path as `file_path='/abs/path.h5ad'` at the top level."
        )

    lines.append(f"Accepted keys: {sorted(_OMICSCLAW_SCHEMA_ARG_KEYS)}.")
    return "\n".join(lines)


async def execute_omicsclaw(
    args: dict,
    session_id: str = None,
    chat_id: int | str = 0,
    cancel_event: threading.Event | None = None,
    thread_id: str = "",
) -> str:
    """Execute an OmicsClaw skill via the shared runner contract.

    ``thread_id`` (Bench, ADR 0018) arrives from ``tool_runtime_context`` via the
    tool's ``context_params`` and scopes the auto-captured analysis lineage under
    the active investigation thread; empty = legacy un-scoped behaviour.
    """
    arg_shape_error = _validate_omicsclaw_args(args)
    if arg_shape_error:
        return arg_shape_error

    skill_key = args.get("skill", "auto")
    mode = args.get("mode", "demo")
    query = args.get("query", "")
    method = args.get("method", "")
    data_type = args.get("data_type", "")
    file_path_arg = args.get("file_path", "")
    # Banner prepended to successful-execution output when we auto-routed.
    # Empty when the caller passed a specific skill.
    auto_route_banner: str = ""

    # --- Resolve input file for path mode ---
    resolved_path: Path | None = None
    if mode == "path" or file_path_arg:
        mode = "path"
        if file_path_arg:
            resolved_path = validate_input_path(file_path_arg, allow_dir=True)
            if resolved_path is None:
                found = discover_file(file_path_arg)
                if found:
                    resolved_path = found[0]
                    if len(found) > 1:
                        listing = "\n".join(f"  - {f}" for f in found[:8])
                        return (
                            f"Multiple files match '{file_path_arg}':\n{listing}\n\n"
                            "Please specify the full path."
                        )
                else:
                    _ensure_trusted_dirs()
                    dirs_str = ", ".join(str(d) for d in TRUSTED_DATA_DIRS)
                    return (
                        f"File not found: '{file_path_arg}'\n\n"
                        f"Place your data files in one of these directories:\n{dirs_str}\n\n"
                        "Then tell me the filename and I'll find it automatically."
                    )
            logger.info(f"Resolved input path: {resolved_path}")
            audit("file_resolve", file_path=str(resolved_path), original=file_path_arg)

    # --- Auto-routing via capability resolver ---
    if skill_key == "auto":
        from omicsclaw.skill.capability_resolver import resolve_capability

        capability_input = query
        if resolved_path:
            capability_input = str(resolved_path)
        elif mode == "file":
            for _cid, info in received_files.items():
                capability_input = info["path"]
                break

        if not capability_input:
            return "Error: skill='auto' requires either a file, a file_path, or a query to route."

        try:
            decision = resolve_capability(
                query or capability_input,
                file_path=str(resolved_path or capability_input or ""),
            )
            if decision.chosen_skill:
                if getattr(decision, "should_create_skill", False):
                    return (
                        "This request is asking to add a reusable OmicsClaw skill.\n\n"
                        "Use create_omics_skill instead of auto-running an analysis skill."
                    )
                # Close-tie disambiguation: refuse to execute when top-1 and
                # top-2 candidates are within _AUTO_DISAMBIGUATE_GAP, so the
                # LLM (or user) picks between them explicitly. Costs one extra
                # tool round but avoids running a multi-minute analysis on the
                # wrong skill.
                cands = list(decision.skill_candidates or [])
                if len(cands) >= 2:
                    gap = float(cands[0].score) - float(cands[1].score)
                    if gap < _AUTO_DISAMBIGUATE_GAP:
                        logger.info(
                            "Auto-routing refused to execute: close tie "
                            "%s (%.2f) vs %s (%.2f), gap=%.2f < %.2f",
                            cands[0].skill, cands[0].score,
                            cands[1].skill, cands[1].score,
                            gap, _AUTO_DISAMBIGUATE_GAP,
                        )
                        return _format_auto_disambiguation(decision, query or capability_input)
                skill_key = decision.chosen_skill
                auto_route_banner = _format_auto_route_banner(decision)
                logger.info(
                    "Auto-routed via capability resolver to: %s (%s, %.2f)",
                    skill_key,
                    decision.coverage,
                    decision.confidence,
                )
            else:
                missing = "; ".join(decision.missing_capabilities) or "no matching skill"
                return (
                    "No existing OmicsClaw skill fully matches this request.\n"
                    f"Coverage: {decision.coverage}\n"
                    f"Reason: {missing}\n\n"
                    "If the user wants a reusable repository skill, use create_omics_skill. "
                    "Otherwise use web_method_search and autonomous_analysis_execute for controlled fallback."
                )
        except Exception as e:
            return f"Error resolving skill automatically: {e}"

    # --- Resolve input for file/path mode ---
    input_path = str(resolved_path) if resolved_path else None
    session_path = None

    if not input_path and session_id:
        file_info = received_files.get(session_id)
        if file_info:
            input_path = file_info.get("path")
            session_path = file_info.get("session_path")

    if mode in ("file", "path") and not input_path and not session_path:
        _ensure_trusted_dirs()
        dirs_str = ", ".join(str(d) for d in TRUSTED_DATA_DIRS)
        return (
            "No input file available. You can either:\n"
            "1. Upload a file via messaging (if small enough)\n"
            f"2. Place your file in a data directory ({dirs_str}) "
            "and tell me the filename\n"
            "3. Provide the full server path to the file"
        )

    if bool(args.get("auto_prepare")) and input_path:
        prepared = await _auto_prepare_sc_batch_integration(
            args=args,
            skill_key=skill_key,
            input_path=input_path,
            session_id=session_id,
            chat_id=chat_id,
            output_root=OUTPUT_DIR,
        )
        if prepared is not None:
            if "final_message" in prepared:
                return prepared["final_message"]
            # Auto-prep succeeded: run the chained args (auto_prepare=False
            # in chained_args, so this self-call cannot recurse further)
            # and prefix the response with the summary of what we just did.
            final_result = await execute_omicsclaw(
                prepared["chained_args"],
                session_id=session_id,
                chat_id=chat_id,
            )
            return prepared["summary_prefix"] + "\n\n---\n" + final_result

    workflow_clarification = _maybe_require_batch_integration_workflow(skill_key, input_path, args)
    if workflow_clarification:
        return workflow_clarification

    batch_key_clarification = _maybe_require_batch_key_selection(skill_key, input_path, args)
    if batch_key_clarification:
        return batch_key_clarification

    # Output directory (ADR 0035): place the Run under its Project. The active
    # Bench thread (``thread_id``, already on the wire for memory scoping) is the
    # ``project_id``; a thread-less /chat or channel run falls to ``default``. The
    # readable ``<dataset>-<uid8>`` leaf keeps ``run_id`` globally unique.
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = run_paths.resolve_run_dir(
        output_root=OUTPUT_DIR,
        skill=skill_key,
        project_id=thread_id,
        input_path=str(input_path) if input_path else None,
        demo=(mode == "demo"),
        method=method,
        timestamp=ts,
    ).run_dir

    batch_key = _resolve_requested_batch_key(args)

    skill_info = _lookup_skill_info(skill_key)
    canonical_skill = skill_info.get("alias") or skill_key
    n_epochs = args.get("n_epochs")
    extra_args = list(args.get("extra_args") or [])
    if args.get("confirmed_preflight"):
        extra_args.append("--confirmed-preflight")

    # Build a parameter hint block so the LLM can relay it to the user
    hint_cmd = ["oc", "run", "spatial-pipeline" if skill_key == "pipeline" else skill_key]
    if mode == "demo":
        hint_cmd.append("--demo")
    elif input_path:
        hint_cmd.extend(["--input", str(input_path)])
    hint_cmd.extend(["--output", str(out_dir)])
    if method:
        hint_cmd.extend(["--method", method])
    if data_type:
        hint_cmd.extend(["--data-type", data_type])
    if batch_key:
        hint_cmd.extend(["--batch-key", batch_key])
    if n_epochs is not None:
        # ``argv_builder.filter_forwarded_args`` rewrites ``--n-epochs`` to
        # ``--epochs`` (or vice versa) per the receiving skill's
        # ``allowed_extra_flags``; the legacy
        # ``if canonical_skill == "spatial-domain-identification"`` branch
        # never fired because the registry resolves that alias to
        # ``spatial-domains`` before this code runs.
        hint_cmd.extend(["--n-epochs", str(int(n_epochs))])
    hint_cmd.extend(_normalize_extra_args(extra_args))
    param_hint = _build_param_hint(skill_key, method, hint_cmd)

    try:
        is_dl = method.lower() in DEEP_LEARNING_METHODS
        if is_dl:
            logger.info(f"Starting {skill_key} with {method} (no timeout, may take 10-60 minutes)")

        runner_result = await _run_skill_via_shared_runner(
            skill_key=skill_key,
            input_path=input_path,
            session_path=session_path,
            mode=mode,
            method=method,
            data_type=data_type,
            batch_key=batch_key,
            n_epochs=n_epochs,
            extra_args=extra_args,
            out_dir=out_dir,
            cancel_event=cancel_event,
        )
        out_dir = Path(runner_result.get("out_dir") or out_dir)
        stdout_str = str(runner_result.get("stdout") or "")
        stderr_str = str(runner_result.get("stderr") or "")
        returncode = int(runner_result.get("returncode") or 0)
    except Exception as e:
        import traceback as _tb
        # Clean up empty output directory on crash
        if out_dir.exists():
            shutil.rmtree(out_dir, ignore_errors=True)
        return f"{skill_key} crashed:\n{_tb.format_exc()[-1500:]}"

    if returncode != 0:
        payloads = extract_user_guidance_payloads(stderr_str)
        payload_prefix = "\n".join(format_user_guidance_payload(payload) for payload in payloads if isinstance(payload, dict))
        guidance_block = render_guidance_block(
            extract_user_guidance_lines(stderr_str),
            payloads=payloads,
        )
        clean_stderr = strip_user_guidance_lines(stderr_str)
        clean_stdout = strip_user_guidance_lines(stdout_str)
        err = clean_stderr[-1500:] if clean_stderr else clean_stdout[-1500:] if clean_stdout else "unknown error"
        # Clean up empty output directory on failure
        if out_dir.exists():
            shutil.rmtree(out_dir, ignore_errors=True)
        # Capture failed analysis to memory (so we remember what was tried)
        if session_id:
            await _auto_capture_analysis(session_id, skill_key, args, None, False, thread_id=thread_id)
        # Environment errors take priority — user needs to know it's not their data
        env_msg = _classify_env_error(err)
        if env_msg:
            return env_msg
        if guidance_block and "preflight check failed" in err.lower():
            return auto_route_banner + (payload_prefix + "\n" if payload_prefix else "") + guidance_block
        if guidance_block:
            rendered = guidance_block + f"\n\n---\n{skill_key} failed (exit {returncode}):\n{err}"
            return auto_route_banner + (payload_prefix + "\n" if payload_prefix else "") + rendered
        plain = f"{skill_key} failed (exit {returncode}):\n{err}"
        return auto_route_banner + (payload_prefix + "\n" if payload_prefix else "") + plain

    # Collect report + figures from output directory
    return_media = str(args.get("return_media", "")).strip().lower()
    collected = _collect_output_media_paths(out_dir)
    figure_paths = collected.figure_paths
    table_paths = collected.table_paths
    notebook_paths = collected.notebook_paths
    figure_names = _path_names(figure_paths)
    table_names = _path_names(table_paths)
    notebook_names = _path_names(notebook_paths)
    sent_names = []
    media_items = collected.media_items
    if out_dir.exists():
        if return_media and media_items:
            if return_media == "all":
                filtered = media_items
            else:
                keywords = [k.strip() for k in return_media.split(",") if k.strip()]
                filtered = [
                    item for item in media_items
                    if any(kw in Path(item["path"]).stem.lower() for kw in keywords)
                ]
            if filtered:
                pending_media[session_id] = pending_media.get(session_id, []) + filtered
                sent_names = [Path(item["path"]).name for item in filtered]
                logger.info(f"return_media='{return_media}': sending {len(filtered)}/{len(media_items)} items")

    # Read report for chat display
    report_text = ""
    if out_dir.exists():
        for pattern in ["report.md", "*_report.md", "*.md"]:
            for md_file in sorted(out_dir.glob(pattern)):
                if md_file.name.startswith("."):
                    continue
                report_text = md_file.read_text(encoding="utf-8")
                break
            if report_text:
                break

    payloads = extract_user_guidance_payloads(stderr_str)
    payload_prefix = "\n".join(format_user_guidance_payload(payload) for payload in payloads if isinstance(payload, dict))
    guidance_block = render_guidance_block(
        extract_user_guidance_lines(stderr_str),
        payloads=payloads,
    )
    if not report_text:
        if guidance_block and stdout_str:
            rendered = guidance_block + "\n\n---\n" + stdout_str
            return (payload_prefix + "\n" if payload_prefix else "") + rendered
        if guidance_block:
            rendered = guidance_block + f"\n\n---\n{skill_key} completed. Output: {out_dir}"
            return (payload_prefix + "\n" if payload_prefix else "") + rendered
        plain = stdout_str if stdout_str else f"{skill_key} completed. Output: {out_dir}"
        return (payload_prefix + "\n" if payload_prefix else "") + plain

    # Trim verbose sections for chat readability; full report is on disk.
    keep_lines = []
    skip = False
    for line in report_text.split("\n"):
        if line.startswith("## Methods") or line.startswith("## Reproducibility"):
            skip = True
        elif line.startswith("## Disclaimer"):
            skip = False
        if line.startswith("!["):
            continue
        if not skip:
            keep_lines.append(line)

    # Auto-capture dataset + analysis memory
    if session_id:
        if input_path:
            await _auto_capture_dataset(session_id, input_path, data_type, thread_id=thread_id)
        # Bench (AN-ROUTER-10): a successful typed-consensus run records its
        # lineage at the canonical thread-scoped namespace
        # (analysis://<thread_id>/typed/<run_id>, ADR 0010/0018) — one record per
        # run, readable by future meta-analysis. Returns False for non-consensus
        # skills (and failed consensus runs), which keep the generic per-skill capture.
        if not await _auto_capture_consensus(
            session_id, skill_key, out_dir, True, thread_id=thread_id
        ):
            await _auto_capture_analysis(session_id, skill_key, args, out_dir, True, thread_id=thread_id)

    # Read result.json for preprocessing_state update and next_steps
    result_json = _read_result_json(out_dir)
    result_data = result_json.get("data", {}) if result_json else {}

    # Update per-dataset preprocessing_state if the skill provides it
    if session_id and result_data.get("preprocessing_state_after"):
        await _update_preprocessing_state(session_id, result_data)

    # Format next_steps recommendation block
    next_steps_block = _format_next_steps(result_data)

    result_text = "\n".join(keep_lines).strip()
    if guidance_block:
        result_text = guidance_block + "\n\n---\n" + result_text
    if payload_prefix:
        result_text = payload_prefix + "\n" + result_text
    if auto_route_banner:
        result_text = auto_route_banner + result_text
    notebook_path = out_dir / "reproducibility" / "analysis_notebook.ipynb"
    if notebook_path.exists():
        result_text += (
            "\n\n---\n"
            f"[Reproducibility notebook available: {notebook_path}. "
            "Tell the user they can open it in Jupyter to inspect code, outputs, and rerun the analysis.]"
        )

    # Prepend parameter hint so the LLM relays it to the user
    if param_hint:
        result_text = param_hint + "\n---\n" + result_text

    # Append media delivery status so the LLM knows what happened
    # and does NOT attempt to browse output directories itself.
    all_names = figure_names + table_names + notebook_names
    if sent_names:
        result_text += (
            "\n\n---\n"
            f"[MEDIA DELIVERY: {len(sent_names)} file(s) already queued for the user: "
            f"{', '.join(sent_names)}. DO NOT use list_directory or other tools to find/send "
            "these files — they will be delivered automatically.]"
        )
        unsent = [n for n in all_names if n not in sent_names]
        if unsent:
            result_text += (
                f"\n[Other available outputs not requested: {', '.join(unsent)}.]"
            )
    elif not return_media and all_names:
        # Emit absolute paths wrapped in backticks so the desktop UI's
        # `injectInlineImages` regex can render them as inline <img>
        # elements when the LLM quotes them verbatim in later replies.
        hints = []
        if figure_paths:
            paths = "\n  ".join(f"- `{path}`" for path in figure_paths)
            hints.append("Figures:\n  " + paths)
        if table_paths:
            paths = "\n  ".join(f"- `{path}`" for path in table_paths)
            hints.append("Tables:\n  " + paths)
        if notebook_paths:
            paths = "\n  ".join(f"- `{path}`" for path in notebook_paths)
            hints.append("Notebooks:\n  " + paths)
        result_text += (
            "\n\n---\n"
            "[Available outputs (absolute paths):\n"
            + "\n".join(hints)
            + "\n\nWhen the user asks to see a figure, quote its backtick path verbatim "
            "(e.g. `/abs/path/to/figure.png`) in your reply — the UI renders any "
            "backtick-quoted image path as an inline preview. Do NOT call "
            "list_directory or other tools to locate these files.]"
        )

    # Stage 2+4: Emit AdvisoryEvent and resolve post-execution knowledge
    try:
        from omicsclaw.knowledge.resolver import AdvisoryEvent, get_resolver

        # Determine domain from skill registry
        _skill_domain = "general"
        try:
            skill_info = _lookup_skill_info(skill_key)
            _skill_domain = skill_info.get("domain", "general")
        except Exception:
            pass

        event = AdvisoryEvent(
            skill=skill_key,
            phase="post_run",
            domain=_skill_domain,
            toolchain=method or "",
            signals=[method, data_type] if method else [],
            severity="info",
            metrics={},
            message=f"Completed {skill_key}" + (f" with method={method}" if method else ""),
        )
        resolver = get_resolver()
        advice = resolver.resolve(
            event,
            session_id=session_id or str(chat_id),
        )
        if advice:
            advice_text = resolver.format_advice(advice, channel="bot")
            if advice_text:
                result_text += f"\n\n{advice_text}"
                logger.info("Post-execution advice appended for %s (%d snippets)",
                            skill_key, len(advice))
    except Exception as e:
        logger.debug("Post-execution advisory skipped: %s", e)

    # Append next_steps recommendations from result.json (if available)
    if next_steps_block:
        result_text += f"\n\n{next_steps_block}"

    return result_text


# ---------------------------------------------------------------------------
# execute_replot_skill
# ---------------------------------------------------------------------------


async def execute_replot_skill(args: dict, session_id: str = None, chat_id: int | str = 0) -> str:
    """Re-render R Enhanced plots from an existing skill output directory."""
    skill_key = args.get("skill", "")
    output_path_arg = args.get("output_path", "").strip()
    renderer = args.get("renderer", "")
    return_media = str(args.get("return_media", "all")).strip().lower()

    if not skill_key:
        return "Error: 'skill' is required (e.g. 'sc-qc', 'sc-de')."

    # Resolve output directory — explicit path > session history fallback
    out_dir: Path | None = None
    if output_path_arg:
        out_dir = Path(output_path_arg).resolve()
        if not out_dir.exists():
            candidate = OUTPUT_DIR / output_path_arg
            if candidate.exists():
                out_dir = candidate.resolve()
            else:
                out_dir = None
    if out_dir is None and session_id:
        out_dir = await _resolve_last_output_dir(session_id, skill_key)
    if out_dir is None or not out_dir.exists():
        return (
            f"Cannot find output directory for `{skill_key}`.\n\n"
            "Please provide the `output_path` from a previous skill run, "
            f"or run the skill first: `omicsclaw(skill='{skill_key}', mode='...')`"
        )

    figure_data_dir = out_dir / "figure_data"
    if not figure_data_dir.exists():
        return (
            f"figure_data/ not found in {out_dir}\n\n"
            f"Re-run {skill_key} first to generate the figure data needed for R Enhanced plots."
        )

    # Build command
    cmd = [get_skill_runner_python(), str(OMICSCLAW_PY), "replot", skill_key, "--output", str(out_dir)]
    if renderer:
        cmd.extend(["--renderer", renderer])

    # Pass optional plot params
    plot_param_map = {
        "top_n": "--top-n",
        "font_size": "--font-size",
        "width": "--width",
        "height": "--height",
        "palette": "--palette",
        "dpi": "--dpi",
        "title": "--title",
    }
    for key, flag in plot_param_map.items():
        val = args.get(key)
        if val is not None:
            cmd.extend([flag, str(val)])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        stdout_str = stdout_bytes.decode(errors="replace")
        stderr_str = stderr_bytes.decode(errors="replace")
    except Exception:
        import traceback as _tb
        return f"replot crashed:\n{_tb.format_exc()[-1500:]}"

    if proc.returncode != 0:
        err = stderr_str[-1500:] if stderr_str else stdout_str[-1500:] if stdout_str else "unknown error"
        env_msg = _classify_env_error(err)
        if env_msg:
            return env_msg
        return f"replot {skill_key} failed (exit {proc.returncode}):\n{err}"

    # Collect generated R Enhanced figures
    r_enhanced_dir = out_dir / "figures" / "r_enhanced"
    figure_names = []
    media_items = []
    if r_enhanced_dir.exists():
        for f in sorted(r_enhanced_dir.rglob("*.png")):
            media_items.append({"type": "photo", "path": str(f)})
            figure_names.append(f.name)

    if not figure_names:
        # R renderer may have silently failed (exit 0 but no PNG produced).
        # Check both stderr AND stdout — R errors from call_r_plot() are
        # wrapped in Python warnings and may appear in either stream.
        combined_output = f"{stderr_str}\n{stdout_str}"
        env_msg = _classify_env_error(combined_output) if combined_output.strip() else None
        if env_msg:
            return env_msg

        # Check for common R-side warnings/errors that _classify_env_error missed
        r_hints: list[str] = []
        if "there is no package called" in combined_output:
            pkgs = re.findall(r"there is no package called '([^']+)'", combined_output)
            if pkgs:
                install_cmd = ", ".join(f'"{p}"' for p in pkgs)
                r_hints.append(
                    f"**R 缺少依赖包:** {', '.join(pkgs)}\n\n"
                    f"**修复方法（在终端运行）:**\n"
                    f"```\nRscript -e 'install.packages(c({install_cmd}))'\n```"
                )
        if "Rscript" in combined_output and ("not found" in combined_output or "No such file" in combined_output):
            r_hints.append(
                "**Rscript 未安装或不在 PATH 中。**\n\n"
                "**修复方法:**\n"
                "```\nsudo apt install r-base  # Ubuntu/Debian\n# 或 conda install -c conda-forge r-base\n```"
            )

        if r_hints:
            return (
                f"**R Enhanced 渲染失败（不是你的数据问题）**\n\n"
                + "\n\n".join(r_hints)
                + f"\n\n修复后重试: 再次要求 replot {skill_key} 即可。"
            )

        # Distinguish "no renderers registered" vs "renderers exist but all failed"
        no_renderers = "No R Enhanced renderers registered" in stdout_str
        stderr_snippet = stderr_str[-500:].strip() if stderr_str else ""
        detail = f"\n\n**技术详情:**\n```\n{stderr_snippet}\n```" if stderr_snippet else ""

        if no_renderers:
            return (
                f"{skill_key} 目前没有注册 R Enhanced 渲染器。\n\n"
                "当前支持 R Enhanced replot 的 scRNA 技能包括: "
                "sc-qc, sc-de, sc-markers, sc-clustering, sc-preprocessing, "
                "sc-cell-annotation, sc-enrichment, sc-velocity, sc-pseudotime 等 22 个。\n\n"
                "如需其他绘图方式，请明确告诉我（如 'use matplotlib'）。"
            )

        return (
            f"replot {skill_key} 的 R Enhanced 渲染器全部失败，没有生成图片。\n\n"
            f"**最可能的原因：R 环境未正确配置。**\n\n"
            f"**修复方法（在终端运行）:**\n"
            f"```\nconda install -c conda-forge r-base r-ggplot2 r-dplyr r-tidyr\n```\n\n"
            f"修复后重试: 再次要求 replot {skill_key} 即可。"
            f"{detail}\n\n"
            f"请将修复方法告诉用户，不要自行尝试其他绘图工具替代。"
        )

    # Queue figures for delivery
    if return_media and media_items and session_id:
        if return_media == "all":
            filtered = media_items
        else:
            keywords = [k.strip() for k in return_media.split(",") if k.strip()]
            filtered = [
                item for item in media_items
                if any(kw in Path(item["path"]).stem.lower() for kw in keywords)
            ]
        if filtered:
            pending_media[session_id] = pending_media.get(session_id, []) + filtered
            sent_names = [Path(item["path"]).name for item in filtered]
            result = (
                f"R Enhanced re-render complete for **{skill_key}**.\n\n"
                f"{len(sent_names)} figure(s) generated: {', '.join(sent_names)}\n"
                f"Figures saved to: {r_enhanced_dir}"
            )
            result += (
                f"\n\n---\n[MEDIA DELIVERY: {len(sent_names)} R Enhanced figure(s) queued: "
                f"{', '.join(sent_names)}. They will be delivered automatically.]"
            )
            return result

    # No session — return paths for inline rendering
    hints = "\n".join(f"- `{r_enhanced_dir / n}`" for n in figure_names)
    return (
        f"R Enhanced re-render complete for **{skill_key}**.\n\n"
        f"{len(figure_names)} figure(s) generated:\n{hints}"
    )


# ---------------------------------------------------------------------------
# execute_save_file
# ---------------------------------------------------------------------------


async def execute_save_file(args: dict) -> str:
    file_info = None
    for _cid, info in received_files.items():
        file_info = info
        break

    if not file_info:
        return "No recently received file to save. Send a file first."

    src_path = Path(file_info["path"])
    if not src_path.exists():
        return "The temporary file has expired. Please send it again."

    dest_path = resolve_dest(args.get("destination_folder"))
    filename = sanitize_filename(args.get("filename") or file_info["filename"])
    final_path = dest_path / filename

    if not validate_path(final_path, dest_path):
        return f"Error: filename '{filename}' would escape the destination directory."

    shutil.copy2(str(src_path), str(final_path))
    logger.info(f"Saved file: {final_path}")
    try:
        src_path.unlink()
    except OSError:
        pass
    return f"File saved to {final_path}"


# ---------------------------------------------------------------------------
# execute_write_file
# ---------------------------------------------------------------------------


async def execute_write_file(args: dict) -> str:
    content = args.get("content")
    filename = args.get("filename")
    if not content:
        return "Error: 'content' is required."
    if not filename:
        return "Error: 'filename' is required."

    dest = resolve_dest(args.get("destination_folder"), default=OUTPUT_DIR)
    filename = sanitize_filename(filename)
    filepath = dest / filename

    if not validate_path(filepath, dest):
        return f"Error: filename '{filename}' would escape the destination directory."

    filepath.write_text(content, encoding="utf-8")
    logger.info(f"Wrote file: {filepath} ({len(content)} chars)")
    return f"File written to {filepath} ({len(content)} chars)"


# ---------------------------------------------------------------------------
# execute_generate_audio
# ---------------------------------------------------------------------------


async def execute_generate_audio(args: dict) -> str:
    text = args.get("text")
    filename = args.get("filename")
    if not text:
        return "Error: 'text' is required."
    if not filename:
        return "Error: 'filename' is required."
    if not filename.endswith(".mp3"):
        filename += ".mp3"

    filename = sanitize_filename(filename)
    voice = args.get("voice", "en-GB-RyanNeural")
    rate = args.get("rate", "-5%")
    dest = resolve_dest(args.get("destination_folder"))
    filepath = dest / filename

    if not validate_path(filepath, dest):
        return f"Error: filename '{filename}' would escape the destination directory."

    text_path = dest / f".tmp_{filename}.txt"
    text_path.write_text(text, encoding="utf-8")

    try:
        proc = await asyncio.create_subprocess_exec(
            "edge-tts",
            "--voice", voice,
            f"--rate={rate}",
            "--file", str(text_path),
            "--write-media", str(filepath),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        try:
            text_path.unlink()
        except OSError:
            pass

        if proc.returncode != 0:
            err = stderr.decode()[-300:] if stderr else "unknown error"
            return f"Audio generation failed (exit {proc.returncode}): {err}"

        size_mb = filepath.stat().st_size / (1024 * 1024)
        word_count = len(text.split())
        est_minutes = word_count / 150
        logger.info(f"Generated audio: {filepath} ({size_mb:.1f} MB)")
        return f"Audio saved to {filepath} ({size_mb:.1f} MB, ~{word_count} words, ~{est_minutes:.0f} min)"

    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        try:
            text_path.unlink()
        except OSError:
            pass
        return "Audio generation timed out after 5 minutes."
    except FileNotFoundError:
        try:
            text_path.unlink()
        except OSError:
            pass
        return "edge-tts not found. Install with: pip install edge-tts"


# ---------------------------------------------------------------------------
# execute_parse_literature
# ---------------------------------------------------------------------------


async def _register_literature_datasets(
    out_dir: Path, session_id: str, thread_id: str
) -> None:
    """Register literature-downloaded datasets under the active thread (Phase 3.3b).

    Reads the literature skill's ``result.json`` and captures each downloaded data
    file as a ``DatasetMemory`` scoped to ``thread_id`` (so it lands under
    ``dataset://<thread_id>/<basename>`` and Analyze in this thread can reference
    it). The per-GSE ``metadata.json`` sidecar is skipped — it is not a dataset.
    Never raises into the loop.
    """
    try:
        result_path = out_dir / "result.json"
        if not result_path.exists():
            return
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        data = payload.get("data", {}) or {}
        platform = (data.get("metadata", {}) or {}).get("technology") or ""
        for dl in data.get("download_results", []) or []:
            if dl.get("status") not in ("success", "partial"):
                continue
            for f in dl.get("files", []) or []:
                if Path(f).name == "metadata.json":
                    continue
                await _auto_capture_dataset(session_id, str(f), platform, thread_id=thread_id)
    except Exception as e:
        logger.warning(f"Literature dataset registration failed: {e}")


# Bound the background KG ingest's await so a slow provider can't keep the asyncio
# task pending forever. (The underlying ``to_thread`` worker still runs to
# completion — the timeout frees the awaiter, not the OS thread.)
_LIT_KG_INGEST_TIMEOUT = 120  # seconds
# Strong refs to in-flight background ingest tasks so they aren't GC'd mid-run.
_LITERATURE_KG_TASKS: set[asyncio.Task] = set()


async def _ingest_literature_into_kg(
    out_dir: Path, thread_id: str = "", session_id: str = ""
) -> None:
    """Converge literature extraction onto the canonical KG ingest (audit D-1).

    The literature skill persists the parsed source text it already obtained to
    ``out_dir/source.txt`` (one server-controlled artifact for every input type —
    file/url/doi/pubmed/text). We ingest THAT into the KG so the paper becomes an
    ideation/formalize-groundable Source (``wiki/sources/*`` + concept/claim graph
    nodes) instead of dead regex metadata. Ingesting the persisted text (rather
    than re-fetching a URL or re-parsing a PDF) keeps this safe (no SSRF, no
    re-fetch) and uniform. Best-effort: KG/LLM may be absent; bounded by
    ``_LIT_KG_INGEST_TIMEOUT`` and never raises (caller spawns it in the
    background so it can't delay the literature tool's response).
    """
    try:
        from omicsclaw.runtime.tools import kg_tools

        result_path = out_dir / "result.json"
        if not result_path.exists():
            return
        data = (json.loads(result_path.read_text(encoding="utf-8")) or {}).get("data", {}) or {}
        source_text_path = data.get("source_text_path")
        if isinstance(source_text_path, str) and source_text_path and Path(source_text_path).is_file():
            result = await asyncio.wait_for(
                kg_tools.ingest_source_into_kg(source_text_path),
                timeout=_LIT_KG_INGEST_TIMEOUT,
            )
            # ingest_source_into_kg returns None (KG/LLM absent) or a result dict;
            # a recorded {"status":"failed"} must be surfaced, not silently dropped.
            if isinstance(result, dict) and result.get("status") == "failed":
                logger.warning(
                    "Literature→KG ingest recorded a failed result: %s",
                    result.get("reason", "unknown"),
                )
            # 批7: record the thread<->source link off the returned slug so the
            # paper is groundable in THIS thread's Ideate. Both "ingested" and
            # "skipped" (cache hit — cross-thread reuse) carry a slug now. No-op
            # without a thread (legacy/IM) — _capture_thread_source guards.
            elif isinstance(result, dict) and result.get("status") in ("ingested", "skipped"):
                slug = result.get("slug")
                if isinstance(slug, str) and slug and thread_id and session_id:
                    await _capture_thread_source(
                        session_id, thread_id, slug, str(result.get("source_page") or "")
                    )
    except Exception as e:  # incl. TimeoutError — never break the literature tool over a KG hiccup
        logger.warning(f"Literature→KG ingest failed (non-fatal): {e}")


def _spawn_literature_kg_ingest(
    out_dir: Path, thread_id: str = "", session_id: str = ""
) -> None:
    """Fire-and-forget the D-1 KG ingest so KG/LLM latency never blocks the
    literature tool's return. Keeps a strong ref until the task finishes.

    ``thread_id``/``session_id`` (批7) are bound into the coroutine args HERE (at
    spawn time) — the spawning tool call has already returned by the time the
    task runs, so the per-thread linkage must travel as values, not be read later
    from a mutated context."""
    try:
        task = asyncio.ensure_future(
            _ingest_literature_into_kg(out_dir, thread_id=thread_id, session_id=session_id)
        )
    except RuntimeError:  # no running loop (defensive — the tool path is async)
        return
    _LITERATURE_KG_TASKS.add(task)
    task.add_done_callback(_LITERATURE_KG_TASKS.discard)


async def execute_parse_literature(
    args: dict, session_id: str | None = None, thread_id: str = ""
) -> str:
    """Execute literature parsing skill.

    Bench (Phase 3.3b): on a successful download, each downloaded dataset is
    registered under the active investigation thread (``dataset://<thread_id>/*``)
    so Analyze in the same thread can reference it. The download itself is
    permission-gated at the ToolSpec layer (approval_mode=ASK, ADR 0021).
    """
    input_value = args.get("input_value", "")
    input_type = args.get("input_type", "auto")
    auto_download = args.get("auto_download", True)

    # Check for uploaded PDF files
    if not input_value:
        for _cid, info in received_files.items():
            file_path = info.get("path", "")
            if file_path and Path(file_path).suffix.lower() == ".pdf":
                input_value = file_path
                input_type = "file"
                logger.info(f"Detected uploaded PDF: {file_path}")
                break

    if not input_value:
        return "Error: input_value is required."

    # Output directory
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = OUTPUT_DIR / f"literature-parse_{ts}"

    # Build command
    lit_script = OMICSCLAW_DIR / "skills" / "literature" / "literature_parse.py"
    if not lit_script.exists():
        return "Error: literature parsing skill not found."

    cmd = [get_skill_runner_python(), str(lit_script)]
    cmd.extend(["--input", input_value])
    cmd.extend(["--input-type", input_type])
    cmd.extend(["--output", str(out_dir)])
    cmd.extend(["--data-dir", str(DATA_DIR)])

    if not auto_download:
        cmd.append("--no-download")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=180,
        )
        stdout_str = stdout_bytes.decode(errors="replace")
        stderr_str = stderr_bytes.decode(errors="replace")
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return "Literature parsing timed out after 180 seconds."
    except Exception as e:
        import traceback as _tb
        return f"Literature parsing crashed:\n{_tb.format_exc()[-1500:]}"

    if proc.returncode != 0:
        err = stderr_str[-1500:] if stderr_str else stdout_str[-1500:] if stdout_str else "unknown error"
        env_msg = _classify_env_error(err)
        if env_msg:
            return env_msg
        return f"Literature parsing failed (exit {proc.returncode}):\n{err}"

    # Bench Phase 3.3b: register downloaded datasets under the active thread so
    # Analyze in this thread can reference them (dataset://<thread_id>/<basename>).
    if auto_download and session_id:
        await _register_literature_datasets(out_dir, session_id, thread_id)

    # D-1: converge literature onto the canonical KG ingest so the parsed paper
    # becomes an ideation-groundable Source. 批7: the KG Source page stays
    # workspace-shared (ADR 0019), but we record a per-thread link off the
    # ingest's slug so the paper is groundable in THIS thread's Ideate. Best-effort,
    # spawned in the BACKGROUND (bounded + self-logging) so KG/LLM latency never
    # delays this user-facing tool's response; thread_id/session_id bound at spawn.
    _spawn_literature_kg_ingest(out_dir, thread_id=thread_id, session_id=session_id or "")

    # Read report
    report_file = out_dir / "report.md"
    if report_file.exists():
        return report_file.read_text(encoding="utf-8")
    else:
        return stdout_str if stdout_str else "Literature parsing completed but no report generated."


# ---------------------------------------------------------------------------
# execute_fetch_geo_metadata
# ---------------------------------------------------------------------------


async def execute_fetch_geo_metadata(args: dict) -> str:
    """Fetch GEO metadata for a specific accession."""
    accession = args.get("accession", "").strip().upper()
    download = args.get("download", False)

    if not accession:
        return "Error: accession is required."

    # Import downloader functions
    sys.path.insert(0, str(OMICSCLAW_DIR / "skills" / "literature"))
    try:
        from core.downloader import fetch_geo_metadata, download_geo_dataset
    except ImportError as e:
        return f"Error importing GEO tools: {e}"

    # Fetch metadata
    try:
        metadata = fetch_geo_metadata(accession)
        if not metadata:
            return f"Failed to fetch metadata for {accession}. Please check the accession ID."

        # Format response
        lines = [
            f"# GEO Metadata: {accession}",
            f"\n**Title**: {metadata.get('title', 'N/A')}",
            f"\n**Organism**: {metadata.get('organism', 'N/A')}",
            f"\n**Platform**: {metadata.get('platform', 'N/A')}",
        ]

        summary = metadata.get('summary', '')
        if summary:
            lines.append(f"\n**Summary**: {summary[:300]}{'...' if len(summary) > 300 else ''}")

        samples = metadata.get('samples', [])
        if samples:
            lines.append(f"\n**Samples**: {len(samples)} samples")
            lines.append(f"- {', '.join(samples[:5])}")
            if len(samples) > 5:
                lines.append(f"- ... and {len(samples) - 5} more")

        # Download if requested
        if download and accession.startswith('GSE'):
            lines.append(f"\n## Downloading {accession}...")
            result = download_geo_dataset(accession, DATA_DIR)
            if result['status'] == 'success':
                lines.append(f"\n✓ Downloaded {len(result['files'])} files to data/{accession}/")
            else:
                lines.append(f"\n✗ Download failed: {', '.join(result.get('errors', ['Unknown error']))}")

        return '\n'.join(lines)

    except Exception as e:
        return f"Error fetching GEO metadata: {e}"


# ---------------------------------------------------------------------------
# execute_list_directory
# ---------------------------------------------------------------------------


async def execute_list_directory(args: dict) -> str:
    """List directory contents (restricted to trusted directories)."""
    path_arg = args.get("path", "")
    target_path = Path(path_arg) if path_arg else DATA_DIR

    if not target_path.is_absolute():
        target_path = DATA_DIR / target_path

    # Validate against trusted directories
    _ensure_trusted_dirs()
    resolved = target_path.resolve()
    if not any(
        resolved == td.resolve() or str(resolved).startswith(str(td.resolve()) + os.sep)
        for td in TRUSTED_DATA_DIRS
    ):
        dirs_str = ", ".join(str(d) for d in TRUSTED_DATA_DIRS)
        return f"Access denied: {target_path} is not in trusted directories ({dirs_str})"

    if not target_path.exists():
        return f"Directory not found: {target_path}"

    if not target_path.is_dir():
        return f"Not a directory: {target_path}"

    try:
        items = []
        for item in sorted(target_path.iterdir()):
            if item.is_dir():
                items.append(f"📁 {item.name}/")
            else:
                size = item.stat().st_size / (1024 * 1024)
                items.append(f"📄 {item.name} ({size:.2f} MB)")

        if not items:
            return f"Empty directory: {target_path}"

        return f"Contents of {target_path}:\n" + "\n".join(items[:50])
    except Exception as e:
        return f"Error listing directory: {e}"


# ---------------------------------------------------------------------------
# execute_inspect_file
# ---------------------------------------------------------------------------


async def execute_inspect_file(args: dict) -> str:
    """Inspect file contents."""
    file_path_arg = args.get("file_path", "")
    lines_limit = args.get("lines", 20)

    if not file_path_arg:
        return "Error: file_path is required."

    file_path = validate_input_path(file_path_arg)
    if not file_path:
        return f"File not found or not accessible: {file_path_arg}"

    try:
        suffix = file_path.suffix.lower()
        content = file_path.read_text(encoding="utf-8")
        lines = content.split("\n")

        preview = "\n".join(lines[:lines_limit])
        total = len(lines)

        return f"File: {file_path.name}\nShowing {min(lines_limit, total)} of {total} lines:\n\n{preview}"
    except Exception as e:
        return f"Error reading file: {e}"


# ---------------------------------------------------------------------------
# execute_inspect_data
# ---------------------------------------------------------------------------


async def execute_inspect_data(args: dict) -> str:
    """Inspect an h5ad AnnData file's metadata without loading the expression matrix."""
    file_path_arg = args.get("file_path", "")
    skill_arg = str(args.get("skill", "")).strip()
    method_arg = str(args.get("method", "")).strip().lower()
    preview_params = bool(args.get("preview_params", False) or method_arg)
    if not file_path_arg:
        return "Error: file_path is required."

    file_path = validate_input_path(file_path_arg)
    if not file_path:
        return f"File not found or not accessible: {file_path_arg}"

    if file_path.suffix.lower() != ".h5ad":
        return f"inspect_data only supports .h5ad files. Got: {file_path.suffix}"

    try:
        import h5py

        info: dict = {}

        with h5py.File(file_path, "r") as f:
            # n_obs / n_vars from index arrays (faster than loading X)
            if "obs" in f and "_index" in f["obs"]:
                info["n_obs"] = len(f["obs"]["_index"])
            elif "X" in f:
                info["n_obs"] = f["X"].shape[0]

            if "var" in f and "_index" in f["var"]:
                info["n_vars"] = len(f["var"]["_index"])
            elif "X" in f:
                info["n_vars"] = f["X"].shape[1]

            # obs/var column names (drop internal HDF5 keys)
            _skip = {"_index", "__categories"}
            info["obs_columns"] = [k for k in f["obs"].keys() if k not in _skip] if "obs" in f else []
            info["var_columns"] = [k for k in f["var"].keys() if k not in _skip] if "var" in f else []

            info["obsm_keys"] = list(f["obsm"].keys()) if "obsm" in f else []
            info["obsp_keys"] = list(f["obsp"].keys()) if "obsp" in f else []
            info["layers"] = list(f["layers"].keys()) if "layers" in f else []
            info["uns_keys"] = list(f["uns"].keys()) if "uns" in f else []
            info["has_raw"] = "raw" in f

    except ImportError:
        # Fallback: use anndata backed mode (no full matrix loaded)
        try:
            import anndata as ad
            adata = ad.read_h5ad(file_path, backed="r")
            info = {
                "n_obs": adata.n_obs,
                "n_vars": adata.n_vars,
                "obs_columns": list(adata.obs.columns),
                "var_columns": list(adata.var.columns),
                "obsm_keys": list(adata.obsm.keys()),
                "obsp_keys": list(adata.obsp.keys()),
                "layers": list(adata.layers.keys()),
                "uns_keys": list(adata.uns.keys()),
                "has_raw": adata.raw is not None,
            }
            adata.file.close()
        except Exception as e2:
            return f"Error inspecting {file_path.name}: {e2}"
    except Exception as e:
        return f"Error inspecting {file_path.name}: {e}"

    # Platform detection (heuristic, no model execution)
    obsm_keys_lower = [k.lower() for k in info.get("obsm_keys", [])]
    obs_cols_lower = [c.lower() for c in info.get("obs_columns", [])]

    if "spatial" in obsm_keys_lower:
        platform = "Spatial transcriptomics"
        suggestions = [
            "- **Spatial preprocessing** (QC → normalization → clustering): `spatial-preprocessing`",
            "- **Spatial domain identification** (tissue regions/niches): `spatial-domain-identification`",
            "- **Spatially variable genes** (SpatialDE, SPARK-X): `spatial-svg-detection`",
            "- **Cell type annotation** (Tangram, scANVI): `spatial-cell-annotation`",
            "- **Cell-cell communication** (LIANA, CellPhoneDB): `spatial-cell-communication`",
            "- **Pathway enrichment** (GSEA, ORA): `spatial-enrichment`",
        ]
    elif any(c in obs_cols_lower for c in ("leiden", "louvain", "cell_type", "celltype", "cluster")):
        platform = "Single-cell RNA-seq (already clustered/annotated)"
        suggestions = [
            "- **Differential expression** between groups: `sc-de`",
            "- **Marker gene detection**: `sc-markers`",
            "- **Trajectory / pseudotime** (DPT, PAGA): `sc-pseudotime`",
            "- **RNA velocity** (scVelo): `sc-velocity`",
            "- **Cell-cell communication** (LIANA, CellChat): `sc-cell-communication`",
            "- **Gene regulatory networks** (SCENIC): `sc-grn`",
        ]
    elif any(c in obs_cols_lower for c in ("pct_counts_mt", "n_genes_by_counts", "total_counts")):
        platform = "Single-cell RNA-seq (raw / QC stage)"
        suggestions = [
            "- **QC metrics & visualization**: `sc-qc`",
            "- **Cell filtering** (QC thresholds): `sc-filter`",
            "- **Doublet detection** (Scrublet, scDblFinder): `sc-doublet-detection`",
            "- **Full preprocessing** (QC → normalization → clustering → UMAP): `sc-preprocessing`",
            "- **Ambient RNA removal** (CellBender): `sc-ambient-removal`",
        ]
    else:
        platform = "Single-cell / generic h5ad"
        suggestions = [
            "- **Full preprocessing** (QC → normalization → clustering → UMAP): `sc-preprocessing`",
            "- **QC metrics**: `sc-qc`",
            "- **Cell type annotation**: `sc-cell-annotation`",
            "- **Batch integration** (Harmony, scVI): `sc-batch-integration`",
        ]

    domain_hint = ""
    if "spatial" in platform.lower():
        domain_hint = "spatial"
    elif "single-cell" in platform.lower() or "singlecell" in platform.lower():
        domain_hint = "singlecell"

    preview_skill = skill_arg
    if preview_params and not preview_skill and method_arg:
        preview_skill = _infer_skill_for_method(method_arg, preferred_domain=domain_hint)

    # Format report
    n_obs = info.get("n_obs", "?")
    n_vars = info.get("n_vars", "?")
    obs_cols = ", ".join(info.get("obs_columns", [])) or "none"
    var_cols = ", ".join(info.get("var_columns", [])) or "none"
    obsm = ", ".join(info.get("obsm_keys", [])) or "none"
    obsp = ", ".join(info.get("obsp_keys", [])) or "none"
    layers = ", ".join(info.get("layers", [])) or "none (X only)"
    uns = ", ".join(info.get("uns_keys", [])) or "none"
    has_spatial = "spatial" in obsm_keys_lower
    has_x_pca = "x_pca" in obsm_keys_lower
    has_counts_layer = "counts" in [k.lower() for k in info.get("layers", [])]
    has_raw = bool(info.get("has_raw", False))

    lines = [
        f"## Data Inspection: `{file_path.name}`",
        f"",
        f"| Property | Value |",
        f"|---|---|",
        f"| **Shape** | {n_obs:,} cells × {n_vars:,} genes |" if isinstance(n_obs, int) else f"| **Shape** | {n_obs} cells × {n_vars} genes |",
        f"| **Platform** | {platform} |",
        f"| **Cell metadata (obs)** | {obs_cols} |",
        f"| **Gene metadata (var)** | {var_cols} |",
        f"| **Embeddings / coords (obsm)** | {obsm} |",
        f"| **Graph matrices (obsp)** | {obsp} |",
        f"| **Layers** | {layers} |",
        f"| **uns keys** | {uns} |",
    ]

    if preview_params and method_arg:
        preview_block = _build_method_preview(
            skill_key=preview_skill or "",
            method=method_arg,
            n_obs=n_obs if isinstance(n_obs, int) else None,
            has_spatial=has_spatial,
            has_x_pca=has_x_pca,
            has_raw=has_raw,
            has_counts_layer=has_counts_layer,
            platform=platform,
        )
        lines.append("")
        if preview_block:
            lines.append(preview_block)
        else:
            lines.append("### Method Suitability & Parameter Preview")
            lines.append("- No `param_hints` found for this `skill/method` combination.")
            lines.append("- Add method hints in SKILL.md: `metadata.omicsclaw.param_hints.<method>`.")
            if not preview_skill:
                lines.append("- Tip: pass `skill` with `inspect_data` for accurate method preview.")

    lines.extend([
        "",
        "**Suggested analyses for this dataset:**",
    ])
    lines.extend(suggestions)
    lines.extend([
        "",
        "Tell me which analysis you'd like to run and I'll get started.",
    ])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# execute_make_directory
# ---------------------------------------------------------------------------


async def execute_make_directory(args: dict) -> str:
    """Create a new directory (restricted to trusted directories)."""
    path_arg = args.get("path", "")

    if not path_arg:
        return "Error: path is required."

    target_path = Path(path_arg)
    if not target_path.is_absolute():
        target_path = OUTPUT_DIR / target_path

    # Validate against trusted directories
    _ensure_trusted_dirs()
    resolved = target_path.resolve() if target_path.exists() else target_path.parent.resolve() / target_path.name
    if not any(
        str(resolved).startswith(str(td.resolve()))
        for td in TRUSTED_DATA_DIRS
    ):
        dirs_str = ", ".join(str(d) for d in TRUSTED_DATA_DIRS)
        return f"Access denied: {target_path} is not in trusted directories ({dirs_str})"

    try:
        target_path.mkdir(parents=True, exist_ok=True)
        return f"Directory created: {target_path}"
    except Exception as e:
        return f"Error creating directory: {e}"


# ---------------------------------------------------------------------------
# execute_move_file
# ---------------------------------------------------------------------------


async def execute_move_file(args: dict) -> str:
    """Move or rename a file."""
    source_arg = args.get("source", "")
    dest_arg = args.get("destination", "")

    if not source_arg or not dest_arg:
        return "Error: source and destination are required."

    source_path = validate_input_path(source_arg)
    if not source_path:
        return f"Source file not found: {source_arg}"

    dest_path = Path(dest_arg)
    if not dest_path.is_absolute():
        dest_path = DATA_DIR / dest_path

    try:
        shutil.move(str(source_path), str(dest_path))
        return f"Moved: {source_path} → {dest_path}"
    except Exception as e:
        return f"Error moving file: {e}"


# ---------------------------------------------------------------------------
# execute_remove_file
# ---------------------------------------------------------------------------


async def execute_remove_file(args: dict) -> str:
    """Remove a file or directory."""
    path_arg = args.get("path", "")

    if not path_arg:
        return "Error: path is required."

    target_path = validate_input_path(path_arg)
    if not target_path:
        return f"Path not found: {path_arg}"

    try:
        if target_path.is_dir():
            shutil.rmtree(target_path)
            return f"Removed directory: {target_path}"
        else:
            target_path.unlink()
            return f"Removed file: {target_path}"
    except Exception as e:
        return f"Error removing: {e}"


# ---------------------------------------------------------------------------
# execute_get_file_size
# ---------------------------------------------------------------------------


async def execute_get_file_size(args: dict) -> str:
    """Get file size."""
    file_path_arg = args.get("file_path", "")

    if not file_path_arg:
        return "Error: file_path is required."

    file_path = validate_input_path(file_path_arg)
    if not file_path:
        return f"File not found: {file_path_arg}"

    try:
        size_bytes = file_path.stat().st_size
        size_mb = size_bytes / (1024 * 1024)
        return f"File: {file_path.name}\nSize: {size_mb:.2f} MB ({size_bytes:,} bytes)"
    except Exception as e:
        return f"Error getting file size: {e}"


# ---------------------------------------------------------------------------
# execute_remember — LLM tool for saving persistent memories
# ---------------------------------------------------------------------------


async def execute_remember(args: dict, session_id: str = None) -> str:
    """Save information to persistent memory (preferences, insights, project context)."""
    if not _core.memory_store:
        return _MEMORY_DISABLED_HINT
    if not session_id:
        return "Memory save requires an active session (user_id + platform)."

    mem_type = args.get("memory_type", "")

    try:
        if mem_type == "preference":
            from omicsclaw.memory.compat import PreferenceMemory

            key = args.get("key", "")
            value = args.get("value", "")
            domain = args.get("domain", "global")

            if not key or not value:
                return "Error: preference requires 'key' and 'value'."

            pref = PreferenceMemory(
                domain=domain,
                key=key,
                value=value,
                is_strict=False,
            )
            mem_id = await _core.memory_store.save_memory(session_id, pref)
            logger.info(f"Memory saved: preference {key}={value} (domain={domain})")
            return f"✓ Preference saved: {key} = {value} (scope: {domain})"

        elif mem_type == "insight":
            from omicsclaw.memory.compat import InsightMemory

            entity_id = args.get("key", "")
            label = args.get("value", "")
            entity_type = args.get("entity_type", "cluster")
            source_id = args.get("source_analysis_id", "")
            confidence = args.get("confidence", "ai_predicted")

            if not entity_id or not label:
                return "Error: insight requires 'key' (entity ID) and 'value' (label)."

            insight = InsightMemory(
                source_analysis_id=source_id or "",
                entity_type=entity_type,
                entity_id=entity_id,
                biological_label=label,
                confidence=confidence,
            )
            mem_id = await _core.memory_store.save_memory(session_id, insight)
            logger.info(f"Memory saved: insight {entity_type} {entity_id} = {label}")
            return f"✓ Insight saved: {entity_type} '{entity_id}' → {label} ({confidence})"

        elif mem_type == "project_context":
            from omicsclaw.memory.compat import ProjectContextMemory

            ctx = ProjectContextMemory(
                project_goal=args.get("project_goal", ""),
                species=args.get("species"),
                tissue_type=args.get("tissue_type"),
                disease_model=args.get("disease_model"),
            )

            if not any([ctx.project_goal, ctx.species, ctx.tissue_type, ctx.disease_model]):
                return "Error: project_context requires at least one of: project_goal, species, tissue_type, disease_model."

            mem_id = await _core.memory_store.save_memory(session_id, ctx)
            parts = []
            if ctx.project_goal:
                parts.append(f"Goal: {ctx.project_goal}")
            if ctx.species:
                parts.append(f"Species: {ctx.species}")
            if ctx.tissue_type:
                parts.append(f"Tissue: {ctx.tissue_type}")
            if ctx.disease_model:
                parts.append(f"Disease: {ctx.disease_model}")
            logger.info(f"Memory saved: project context ({', '.join(parts)})")
            return f"✓ Project context saved: {' | '.join(parts)}"

        else:
            return f"Error: unknown memory_type '{mem_type}'. Use: preference, insight, project_context."

    except Exception as e:
        logger.error(f"Memory save failed: {e}", exc_info=True)
        return f"Error saving memory: {e}"


async def _recall_fetch(sid: str, query: str, mem_type: str, limit: int, thread_id: str):
    """One recall pass: query → search, else list-by-type/all. ``thread_id``
    scopes to that thread (empty = unscoped, the cross-thread fallback pass)."""
    store = _core.memory_store
    if query:
        return await store.search_memories(
            sid, query, memory_type=mem_type or None, thread_id=thread_id
        )
    elif mem_type:
        return await store.get_memories(sid, mem_type, limit=limit, thread_id=thread_id)
    else:
        return await store.get_memories(sid, limit=limit, thread_id=thread_id)


async def execute_recall(args: dict, session_id: str = None, thread_id: str = "") -> str:
    """Retrieve memories from persistent storage.

    Bench (BE-RECALL-6): when a ``thread_id`` is active, recall defaults to that
    investigation thread's memories, then appends cross-thread hits ranked lower
    (ADR 0018 — cross-thread recall is a feature, not isolation). Empty thread_id
    is the legacy unscoped recall.
    """
    if not _core.memory_store:
        return _MEMORY_DISABLED_HINT

    try:
        mem_type = args.get("memory_type", "")
        query = args.get("query", "")
        sid = session_id or ""
        limit = int(args.get("limit", 10))

        if thread_id:
            def _gid(m):
                return getattr(m, "memory_id", None)

            primary = await _recall_fetch(sid, query, mem_type, limit, thread_id)
            seen = {_gid(m) for m in primary}

            # The user's explicitly-saved global memories (preference / insight /
            # project_context) carry no thread_id, so the thread-scoped primary
            # excludes them. On a no-query, no-type listing a busy thread's
            # auto-captured dataset/analysis rows would otherwise crowd them out of
            # the shared budget — so fetch them explicitly and always keep them.
            globals_extra: list = []
            if not query and not mem_type:
                for gt in ("preference", "insight", "project_context"):
                    for m in await _core.memory_store.get_memories(sid, gt, limit=limit):
                        if _gid(m) not in seen:
                            seen.add(_gid(m))
                            globals_extra.append(m)

            # Cross-thread hits (ranked lowest), de-duplicated by memory_id.
            fallback = await _recall_fetch(sid, query, mem_type, limit, "")
            cross = [m for m in fallback if _gid(m) not in seen]

            # Thread rows + user globals are always shown; cross-thread fills the
            # remaining budget up to ``limit``.
            keep = primary + globals_extra
            room = max(0, limit - len(keep))
            memories = keep + cross[:room]
        else:
            memories = await _recall_fetch(sid, query, mem_type, limit, "")

        if not memories:
            return "No memories found."

        parts = []
        for m in memories:
            if hasattr(m, "memory_type"):
                if m.memory_type == "preference":
                    parts.append(f"[preference] {m.key}: {m.value} (scope: {m.domain})")
                elif m.memory_type == "insight":
                    confidence = "confirmed" if m.confidence == "user_confirmed" else "predicted"
                    parts.append(f"[insight] {m.entity_type} {m.entity_id}: {m.biological_label} ({confidence})")
                elif m.memory_type == "project_context":
                    ctx_parts = []
                    if m.project_goal:
                        ctx_parts.append(f"Goal: {m.project_goal}")
                    if m.species:
                        ctx_parts.append(f"Species: {m.species}")
                    if m.tissue_type:
                        ctx_parts.append(f"Tissue: {m.tissue_type}")
                    if m.disease_model:
                        ctx_parts.append(f"Disease: {m.disease_model}")
                    parts.append(f"[project_context] {' | '.join(ctx_parts)}")
                elif m.memory_type == "dataset":
                    parts.append(f"[dataset] {m.file_path} (preprocessed={m.preprocessing_state})")
                elif m.memory_type == "analysis":
                    parts.append(f"[analysis] {m.skill} ({m.method}) - {m.status}")
                else:
                    parts.append(f"[{m.memory_type}] {m.model_dump_json()}")

        return f"Found {len(parts)} memories:\n" + "\n".join(parts)

    except Exception as e:
        logger.error(f"Memory recall failed: {e}", exc_info=True)
        return f"Error recalling memory: {e}"


async def execute_forget(args: dict, session_id: str = None) -> str:
    """Delete a specific memory by searching for it."""
    if not _core.memory_store:
        return _MEMORY_DISABLED_HINT

    memory_id = args.get("memory_id", "")
    query = args.get("query", "")

    if not memory_id and not query:
        return "Error: provide either 'memory_id' or 'query' to identify the memory to forget."

    try:
        search_term = memory_id or query
        memories = await _core.memory_store.search_memories(session_id or "", search_term)

        if not memories:
            return f"No memory found matching '{search_term}'."

        # Delete the first match
        target = memories[0]
        from omicsclaw.memory.compat import _TYPE_TO_DOMAIN, _memory_to_uri_path
        domain = _TYPE_TO_DOMAIN.get(target.memory_type, "core")
        path = _memory_to_uri_path(target)
        uri = f"{domain}://{path}"
        await _core.memory_store._client.forget(uri)
        return f"✓ Forgotten: {uri}"

    except Exception as e:
        logger.error(f"Memory forget failed: {e}", exc_info=True)
        return f"Error forgetting memory: {e}"


async def execute_read_knowhow(args: dict, **kwargs) -> str:
    """Fetch the full markdown body of a KH guard by name.

    Pairs with the headline-only ``MANDATORY SCIENTIFIC CONSTRAINTS`` block
    in the system prompt: the model sees ``→ {label}: {critical_rule}``
    summaries up front and calls ``read_knowhow(name=...)`` only when more
    detail (thresholds, code examples, edge cases) is actually needed.
    """
    try:
        from omicsclaw.knowledge.knowhow import get_knowhow_injector

        name = (args or {}).get("name", "")
        if not name:
            return "Error: 'name' parameter is required."
        body = get_knowhow_injector().read_knowhow(str(name))
        if not body:
            return (
                f"No KnowHow guard matched '{name}'. The Active Guards block "
                "in the system prompt lists the available labels; pass one of "
                "them, the doc_id, or the KH-*.md filename."
            )
        return body
    except Exception as e:
        logger.error(f"read_knowhow failed: {e}", exc_info=True)
        return f"Error reading KH: {e}"


async def execute_consult_knowledge(args: dict, **kwargs) -> str:
    """Query the OmicsClaw knowledge base for analysis guidance."""
    try:
        import time as _t
        _ck_start = _t.monotonic()

        from omicsclaw.knowledge import KnowledgeAdvisor
        from omicsclaw.knowledge.semantic_bridge import (
            generate_query_rewrites,
            rerank_candidates_with_llm,
        )

        advisor = KnowledgeAdvisor()
        query = args.get("query", "")
        if not query:
            return "Error: 'query' parameter is required."
        if not advisor.ensure_available(auto_build=True):
            return "Knowledge base not built yet. Run: python omicsclaw.py knowledge build"

        domain = args.get("domain", "all")
        category = args.get("category", "all")
        domain_filter = domain if domain != "all" else None
        category_filter = category if category != "all" else None

        rewrites = await generate_query_rewrites(
            query=query,
            domain=domain_filter or "",
            doc_type=category_filter or "",
            llm_client=_core.llm,
            model=_core.OMICSCLAW_MODEL,
            available_topics=advisor.list_topics(domain_filter),
            max_queries=4,
        )
        results = advisor.search(
            query=query,
            domain=domain_filter,
            doc_type=category_filter,
            limit=8,
            extra_queries=rewrites,
        )
        results = await rerank_candidates_with_llm(
            query=query,
            candidates=results,
            llm_client=_core.llm,
            model=_core.OMICSCLAW_MODEL,
            limit=5,
        )
        result = advisor.format_results(query, results)

        # Stage 0: Telemetry
        _ck_elapsed_ms = (_t.monotonic() - _ck_start) * 1000
        try:
            from omicsclaw.knowledge.telemetry import get_telemetry
            results_count = result.count("--- Result") if result else 0
            get_telemetry().log_consult_knowledge(
                session_id=kwargs.get("session_id", "unknown"),
                query=query,
                category=category,
                domain=domain,
                results_count=results_count,
                latency_ms=_ck_elapsed_ms,
            )
        except Exception:
            pass

        return result
    except Exception as e:
        logger.error(f"Knowledge query failed: {e}", exc_info=True)
        return f"Error querying knowledge base: {e}"


async def execute_resolve_capability(args: dict, **kwargs) -> str:
    """Resolve whether a request maps to an existing skill or needs fallback."""
    try:
        from omicsclaw.skill.capability_resolver import resolve_capability

        query = args.get("query", "")
        if not query:
            return "Error: 'query' parameter is required."

        file_path_arg = args.get("file_path", "")
        resolved_path = validate_input_path(file_path_arg) if file_path_arg else None
        decision = resolve_capability(
            query,
            file_path=str(resolved_path or file_path_arg or ""),
            domain_hint=args.get("domain_hint", ""),
        )
        return decision.to_json()
    except Exception as e:
        logger.error(f"Capability resolution failed: {e}", exc_info=True)
        return f"Error resolving capability: {e}"


async def execute_list_skills_in_domain(args: dict, **kwargs) -> str:
    """Return a markdown listing of all skills in a single OmicsClaw domain.

    Lazy counterpart to the 7-domain briefing embedded in the ``omicsclaw``
    tool description: the LLM pays the per-domain detail only when it
    actually needs it.
    """
    try:
        from omicsclaw.skill.listing import list_skills_in_domain

        domain = args.get("domain", "")
        if not domain:
            return (
                "Error: 'domain' parameter is required. "
                "Pick one of: spatial, singlecell, genomics, proteomics, "
                "metabolomics, bulkrna, orchestrator."
            )
        filter_text = args.get("filter", "") or ""
        return list_skills_in_domain(domain, filter_text)
    except Exception as e:
        logger.error(f"list_skills_in_domain failed: {e}", exc_info=True)
        return f"Error listing skills: {e}"


async def execute_create_omics_skill(args: dict, **kwargs) -> str:
    """Create a new OmicsClaw skill scaffold inside the repository."""
    try:
        from omicsclaw.skill.scaffolder import create_skill_scaffold

        request = args.get("request", "")
        domain = args.get("domain", "")
        if not request:
            return "Error: 'request' parameter is required."

        result = create_skill_scaffold(
            request=request,
            domain=domain,
            skill_name=args.get("skill_name", ""),
            summary=args.get("summary", ""),
            source_analysis_dir=args.get("source_analysis_dir", ""),
            promote_from_latest=bool(args.get("promote_from_latest", False)),
            output_root=OUTPUT_DIR,
            input_formats=args.get("input_formats") or [],
            primary_outputs=args.get("primary_outputs") or [],
            methods=args.get("methods") or [],
            trigger_keywords=args.get("trigger_keywords") or [],
            create_tests=bool(args.get("create_tests", True)),
        )
        created = "\n".join(f"- {path}" for path in result.created_files or [])
        completion_summary = format_completion_mapping_summary(result.completion)
        return (
            "Created OmicsClaw skill scaffold.\n"
            f"Skill: {result.skill_name}\n"
            f"Domain: {result.domain}\n"
            f"Directory: {result.skill_dir}\n"
            f"Registry refreshed: {result.registry_refreshed}\n"
            f"Manifest: {result.manifest_path or '<none>'}\n"
            f"Completion report: {result.completion_report_path or '<none>'}\n"
            f"Gate:\n{completion_summary or '<unavailable>'}\n"
            f"Source analysis: {args.get('source_analysis_dir') or ('<latest autonomous analysis>' if args.get('promote_from_latest') else '<none>')}\n"
            "Files:\n"
            f"{created}"
        )
    except FileExistsError as e:
        return f"Error creating OmicsClaw skill: {e}"
    except Exception as e:
        logger.error(f"Create OmicsClaw skill failed: {e}", exc_info=True)
        return f"Error creating OmicsClaw skill: {e}"


async def execute_web_method_search(args: dict, **kwargs) -> str:
    """Search the web for methods/docs to support custom analysis fallback."""
    try:
        from omicsclaw.research import search_web_markdown

        query = args.get("query", "")
        if not query:
            return "Error: 'query' parameter is required."

        max_results = int(args.get("max_results", 3) or 3)
        topic = args.get("topic", "general") or "general"
        return await search_web_markdown(query, max_results=max_results, topic=topic)
    except ImportError as e:
        return (
            "Error: web search dependencies are not installed. "
            'Install with: pip install -e ".[autonomous]" or pip install -e ".[research]". '
            f"Details: {e}"
        )
    except Exception as e:
        logger.error(f"Web method search failed: {e}", exc_info=True)
        return f"Error searching the web for methods: {e}"


def _resolve_trusted_data_paths(
    raw_paths: list[str], *, allow_dir: bool = False
) -> tuple[list[str], list[str]]:
    """Resolve model/user-supplied data paths to absolute, trusted paths.

    The autonomous engine binds inputs into a sandbox and its kernel chdir's into
    the run workspace, so a *relative* path (e.g. ``data/x.h5ad`` copied from the
    Desktop workspace file browser) never resolves: the bwrap bind source is taken
    against the server cwd and the in-kernel ``read_h5ad`` against the run
    workspace — both miss the real file, leaving ``adata=None`` and a run that
    dies on ``'NoneType' has no attribute 'shape'``. Resolve each path the same
    way the data-inspection / custom-analysis tools do so the engine receives an
    absolute path that survives the cwd change.

    ``allow_dir`` permits directory targets — spatial inputs may be a 10x/Visium
    directory, and ``upstream_paths`` are defined by the tool schema as prior-skill
    output *directories*. Returns ``(resolved_absolute, problems)``; ``problems``
    are human-readable strings for paths that are missing or ambiguous (a bare name
    matching several files, which we refuse rather than silently pick by mtime —
    mirroring the custom-analysis tool). Blank entries are skipped.
    """
    resolved: list[str] = []
    problems: list[str] = []
    for raw in raw_paths:
        candidate = str(raw).strip()
        if not candidate:
            continue
        hit = validate_input_path(candidate, allow_dir=allow_dir)
        if hit is not None:
            resolved.append(str(hit))
            continue
        found = discover_file(candidate)
        if len(found) == 1:
            resolved.append(str(found[0]))
        elif len(found) > 1:
            sample = ", ".join(str(f) for f in found[:6])
            problems.append(f"{candidate} (matches multiple files — pass a full path: {sample})")
        else:
            problems.append(f"{candidate} (not found in the workspace or a trusted data directory)")
    return resolved, problems


# Inline-result digest for the autonomous run. The tool result uses
# RESULT_POLICY_SUMMARY_OR_MEDIA, which spills outputs over ~5000 bytes to disk
# and replaces them with a preview — so the digest is byte-capped below that to
# stay fully in-context (otherwise the outer agent would re-fetch it).
_AUTONOMOUS_DIGEST_MAX_BYTES = 4800
_AUTONOMOUS_ARTIFACT_SUFFIXES = frozenset(
    {".png", ".pdf", ".svg", ".csv", ".tsv", ".html", ".h5ad", ".xlsx"}
)
_AUTONOMOUS_BOOKKEEPING_FILES = frozenset(
    {"completion_report.json", "manifest.json", "analysis.py"}
)


def _clip_chars(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + " …[truncated]"


def _clip_to_bytes(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    suffix = "\n…[digest truncated — open the completion report for full detail]"
    # Reserve room for the suffix so the total stays at/under max_bytes — the
    # whole point of the cap is to keep the digest inline (not spilled to disk).
    budget = max(0, max_bytes - len(suffix.encode("utf-8")))
    clipped = encoded[:budget].decode("utf-8", errors="ignore").rstrip()
    return clipped + suffix


def _autonomous_artifacts(workspace_root: str, *, limit: int = 40) -> list[str]:
    """List the analysis outputs (figures + data files), skipping bookkeeping.

    Replaces the glob/list_directory calls the outer agent used to issue to
    discover what a run produced.
    """
    from pathlib import Path

    out: list[str] = []
    try:
        root = Path(workspace_root)
        figures = root / "figures"
        if figures.is_dir():
            out.extend(
                f"figures/{p.name}" for p in sorted(figures.iterdir()) if p.is_file()
            )
        for p in sorted(root.iterdir()):
            if (
                p.is_file()
                and p.suffix.lower() in _AUTONOMOUS_ARTIFACT_SUFFIXES
                and p.name not in _AUTONOMOUS_BOOKKEEPING_FILES
            ):
                out.append(p.name)
    except OSError:
        pass
    return out[:limit]


def _format_autonomous_digest(result) -> str:
    """Compact, self-contained digest of an autonomous run for the outer agent.

    ADR 0014 asks the outer LLM to judge the produced artifacts after the run.
    When the tool returned only file paths, the model satisfied that by reading
    completion_report.json / result_summary.md and globbing figures across many
    turns — a token-heavy "verification storm" (diagnose 2026-06-26). This
    inlines the compact content the run already produced (computed results,
    answer, artifact list) so the model can verify WITHOUT re-reading raw files,
    byte-capped to stay under the tool-result inline threshold.
    """
    meta = result.metadata or {}
    status = "completed" if result.ok else "failed"
    parts: list[str] = [f"Autonomous analysis {status} (run {result.run_id})."]

    computed = str(meta.get("computed_results", "") or "").strip()
    answer = str(meta.get("answer", "") or "").strip()
    notes = str(meta.get("interpretive_notes", "") or "").strip()

    if computed:
        parts.append("## Computed results\n" + _clip_chars(computed, 1600))
    if answer:
        parts.append("## Answer\n" + _clip_chars(answer, 1600))
    if notes and notes != answer:
        parts.append("## Interpretive notes\n" + _clip_chars(notes, 600))

    artifacts = _autonomous_artifacts(result.workspace_root)
    if artifacts:
        parts.append("## Artifacts produced\n" + "\n".join(f"- {a}" for a in artifacts))

    attempt_lines = [
        f"- attempt {a.attempt_index}: {a.status.value}, "
        f"tier={a.permission_tier.value}, exit={a.exit_code}"
        for a in result.attempts
    ]
    parts.append("## Attempts\n" + ("\n".join(attempt_lines) or "- none"))

    if result.error:
        parts.append("## Error\n" + _clip_chars(str(result.error), 600))

    parts.append(
        "## Raw artifacts (already summarized above — read only if you need "
        "detail beyond this digest)\n"
        f"- Output dir: {result.workspace_root}\n"
        f"- Completion report: {result.completion_report_path}\n"
        f"- Manifest: {result.manifest_path}"
    )

    return _clip_to_bytes("\n\n".join(parts), _AUTONOMOUS_DIGEST_MAX_BYTES)


def _register_autonomous_media(session_id: str, workspace_root: str) -> list[dict]:
    """Queue an autonomous run's figures + a run-dir anchor onto the
    ``pending_media`` side-channel (audit A-3).

    The verification-storm fix made the autonomous tool return a compact text
    digest with no machine-readable producer field, so ``on_tool_result`` could
    neither inline the run's figures nor stamp the producing session (the run
    never appeared under 本对话). The digest stays the LLM-facing return value;
    the figures plus the readable ``result_summary.md`` travel through
    ``pending_media`` exactly like the skill executor's ``return_media`` path —
    so the desktop inlines plots and links the Run WITHOUT re-bloating the
    model's context. Best-effort: never raises into the tool loop.
    """
    if not session_id or not workspace_root:
        return []
    root = Path(workspace_root)
    try:
        collected = _collect_output_media_paths(root).media_items
    except Exception:
        logger.debug("autonomous media collection failed", exc_info=True)
        collected = []
    items: list[dict] = [it for it in collected if it.get("type") == "photo"]
    summary = root / "result_summary.md"
    if summary.is_file():
        # The readable report doubles as the run-dir anchor that lets text-only
        # runs (no figures) still link to their conversation.
        items.append({"type": "document", "path": str(summary)})
    if not items and collected:
        items.append(collected[0])
    if items:
        pending_media[session_id] = pending_media.get(session_id, []) + items
    return items


async def execute_autonomous_analysis_execute(args: dict, **kwargs) -> str:
    """Run the first-class Autonomous Code Runner loop."""
    try:
        from omicsclaw.autonomous import (
            AutonomousRunRequest,
            run_autonomous_code_loop_async,
        )

        goal = str(args.get("goal", "") or "").strip()
        if not goal:
            return "Error: 'goal' parameter is required."

        input_paths, input_problems = _resolve_trusted_data_paths(
            [str(item) for item in args.get("input_paths", []) or []], allow_dir=True
        )
        upstream_paths, upstream_problems = _resolve_trusted_data_paths(
            [str(item) for item in args.get("upstream_paths", []) or []], allow_dir=True
        )
        problems = input_problems + upstream_problems
        if problems:
            return (
                "Error: could not resolve these path(s) for the autonomous run:\n- "
                + "\n- ".join(problems)
                + "\nPass a path under the active workspace (e.g. its data/ folder) "
                "or an absolute path."
            )
        language = str(args.get("language", "python") or "python").strip().lower()
        if language in {"r", "rscript"}:
            return (
                "Error: the Autonomous Code Mini-Agent runs Python only (ADR 0032 v1); "
                "R is not supported (the kernel, system prompt and code lint are all "
                "Python). Re-run with language='python', or use a built-in R-backed "
                "skill for an R analysis."
            )
        if language != "python":
            return "Error: language must be 'python'."

        max_repair_attempts = int(args.get("max_repair_attempts", 2) or 2)
        max_repair_attempts = max(0, min(max_repair_attempts, 2))
        request = AutonomousRunRequest(
            goal=goal,
            output_root=str(OUTPUT_DIR),
            input_paths=input_paths,
            upstream_paths=upstream_paths,
            project_id=str(kwargs.get("thread_id", "") or ""),  # ADR 0035: nest under active project
            language=language,
            max_repair_attempts=max_repair_attempts,
            context=str(args.get("context", "") or ""),
            web_context=str(args.get("web_context", "") or ""),
            data_schema=str(args.get("data_schema", "") or ""),
            analysis_plan=str(args.get("analysis_plan", "") or ""),
            model_override=str(kwargs.get("model_override", "") or ""),
            provider_override=str(kwargs.get("provider_override", "") or ""),
            metadata={
                "surface": str(kwargs.get("surface", "") or ""),
                "chat_id": str(kwargs.get("chat_id", "") or ""),
                "session_id": str(kwargs.get("session_id", "") or ""),
            },
        )
        # No mid-run approval kwarg: the whole autonomous tool call is gated once
        # at the outer agent loop (ADR 0008 L2); request.metadata already carries
        # surface/chat_id/session_id. (Dropped the dead request_tool_approval/
        # runtime_context that code_loop never consulted — see code_loop docstring.)
        result = await run_autonomous_code_loop_async(request)

        digest = _format_autonomous_digest(result)
        # A-3: surface the run's figures + report and link the producing session
        # via the pending_media side-channel (the compact digest carries no
        # machine-readable paths). Best-effort; must not fail the run.
        try:
            _register_autonomous_media(
                str(kwargs.get("session_id", "") or ""), result.workspace_root
            )
        except Exception:
            logger.debug("autonomous media registration failed", exc_info=True)
        return digest
    except Exception as e:
        logger.error(f"Autonomous analysis execution failed: {e}", exc_info=True)
        return f"Error running autonomous analysis: {e}"




# ---------------------------------------------------------------------------
# Dispatch table + tool runtime builder
# ---------------------------------------------------------------------------

def _available_tool_executors() -> dict[str, object]:
    executors = {
        "omicsclaw": execute_omicsclaw,
        "replot_skill": execute_replot_skill,
        "save_file": execute_save_file,
        "write_file": execute_write_file,
        "generate_audio": execute_generate_audio,
        "parse_literature": execute_parse_literature,
        "fetch_geo_metadata": execute_fetch_geo_metadata,
        "list_directory": execute_list_directory,
        "inspect_file": execute_inspect_file,
        "make_directory": execute_make_directory,
        "move_file": execute_move_file,
        "remove_file": execute_remove_file,
        "get_file_size": execute_get_file_size,
        "remember": execute_remember,
        "recall": execute_recall,
        "forget": execute_forget,
        "consult_knowledge": execute_consult_knowledge,
        "read_knowhow": execute_read_knowhow,
        "resolve_capability": execute_resolve_capability,
        "list_skills_in_domain": execute_list_skills_in_domain,
        "create_omics_skill": execute_create_omics_skill,
        "web_method_search": execute_web_method_search,
        "autonomous_analysis_execute": execute_autonomous_analysis_execute,
        "inspect_data": execute_inspect_data,
    }
    # Bench Phase 3.1 (ADR 0019) — KG read tools, always registered; each
    # executor soft-fails when the optional ``omicsclaw_kg`` package is absent.
    executors.update(KG_TOOL_EXECUTORS)
    executors.update(
        build_engineering_tool_executors(
            omicsclaw_dir=OMICSCLAW_DIR,
            tool_specs_supplier=lambda: _core.get_tool_registry().specs,
        )
    )
    return executors


def _build_tool_runtime():
    return _core.get_tool_registry().build_runtime(_available_tool_executors())


_TOOL_RUNTIME_CACHE = None


def get_tool_runtime():
    global _TOOL_RUNTIME_CACHE
    if _TOOL_RUNTIME_CACHE is None:
        _TOOL_RUNTIME_CACHE = _build_tool_runtime()
    return _TOOL_RUNTIME_CACHE


def get_tool_executors() -> dict[str, object]:
    return dict(get_tool_runtime().executors)
