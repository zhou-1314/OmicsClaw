"""Multi-turn OmicsClaw benchmark adapter for E1 state-contamination & revision stress tests.

Drives a two-turn (or N-turn) scripted interaction in a single workspace:
- Turn 1 runs the initial prompt (``prompt.md`` or the part before ``<!-- TURN_2 -->``).
- Optional mid-turn workspace perturbation (``F1``–``F4``) runs between Turn 1 and Turn 2.
- Turn 2 runs the revision prompt (``turn2_prompt.md``, ``turns.json``, ``<!-- TURN_2 -->``, or ``arm.options["turn2_prompt"]``).
- Optional clean-room replay gate (``arm.options["enforce_replay_gate"]``) exports and verifies ``output/reproduce.py``.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..layout import RunPaths
from ..manifest import Arm, Budget, ManifestError, RunSpec
from ..outcome import Evidence, ProcessExit
from ..process import MARKER_VARIABLE
from . import Launch
from .omicsclaw import OmicsClawAdapter, _checkout

__all__ = ["OmicsClawMultiTurnAdapter", "_MULTITURN_DRIVER", "_PARTICIPANT_SCRIPT"]

_MT_OPTIONS = frozenset({
    "python",
    "source_root",
    "capture_content",
    "turn2_prompt",
    "perturbation",
    "enforce_replay_gate",
    "reasoning_effort",
})

_MULTITURN_DRIVER = r"""import asyncio, json, os, pathlib, sys

cfg = json.loads(sys.argv[1])
source_root = cfg.get("source_root") or ""
if source_root:
    sys.path.insert(0, source_root)

import omicsclaw
print("bench-launch omicsclaw=" + str(omicsclaw.__file__), file=sys.stderr, flush=True)

ws = pathlib.Path(cfg["workspace"]).resolve()
turns = list(cfg.get("turns") or [])
perturbation = cfg.get("perturbation") or {}

def _apply_perturbation(spec, workspace):
    if not spec:
        return
    kind = spec.get("kind") if isinstance(spec, dict) else str(spec)
    if kind == "F3":
        target = workspace / str(spec.get("target", "output/intermediate_cache.txt") if isinstance(spec, dict) else "output/intermediate_cache.txt")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("CORRUPTED_CACHE_NANS\n", encoding="utf-8")
    elif kind == "F4":
        target = workspace / str(spec.get("target", "data/covariates.csv") if isinstance(spec, dict) else "data/covariates.csv")
        if target.is_file():
            target.unlink()

def _resolve_artifact_store(app):
    kb = getattr(app, "kernel_binding", None)
    if kb is not None and hasattr(kb, "_ensure_store"):
        return kb._ensure_store()
    kernels = getattr(app, "kernels", None)
    if kernels is not None and hasattr(kernels, "_ensure_store"):
        return kernels._ensure_store()
    return getattr(app, "artifact_store", None)

async def _run_multiturn() -> int:
    from omicsclaw.entry.assembly import open_app
    from omicsclaw.entry.cli import Repl, Screen, ScriptedSource, terminal_owned_logging
    from omicsclaw.entry.config import resolve_app_config
    from omicsclaw.entry.notebook.service import export_and_replay
    from omicsclaw.entry.session import attach_sessions
    from omicsclaw.launch._surfaces import _release, _replay, _with_the_cli_permission_mode, surface_config

    deployment = ["--workspace", str(ws)]
    if cfg.get("provider"):
        deployment += ["--provider", str(cfg["provider"])]
    if cfg.get("model"):
        deployment += ["--model", str(cfg["model"])]

    config = surface_config(
        resolve_app_config(deployment, _with_the_cli_permission_mode(os.environ)),
        "once",
    )
    records = None
    code = 0
    try:
        with terminal_owned_logging() as records:
            app = attach_sessions(await open_app(config))
            try:
                repl = Repl(
                    app,
                    source=ScriptedSource(()),
                    screen=Screen(),
                    show_reasoning=False,
                )
                for idx, prompt_file in enumerate(turns):
                    if idx == 1 and perturbation:
                        _apply_perturbation(perturbation, ws)
                    prompt_text = pathlib.Path(prompt_file).read_text(encoding="utf-8")
                    handle = await repl.ask(prompt_text)
                    if handle is None or handle.terminal != "converged":
                        code = 1
                        break

                if code == 0 and cfg.get("enforce_replay_gate"):
                    store = _resolve_artifact_store(app)
                    receipt = await asyncio.to_thread(
                        export_and_replay,
                        repl.state.session_id,
                        ws,
                        artifact_store=store,
                    )
                    if receipt.get("status") != "ok":
                        repair_prompt = (
                            "Clean-room replay verification failed on output/reproduce.py: "
                            f"changed_outputs={receipt.get('changed_outputs')}, "
                            f"missing_outputs={receipt.get('missing_outputs')}, "
                            f"orphan_outputs={receipt.get('orphan_outputs')}, "
                            f"stale_inputs={receipt.get('stale_inputs')}. "
                            "Re-run or update the notebook cells so all outputs in output/ "
                            "and output/REPORT.md are cleanly reproduced from raw data."
                        )
                        handle = await repl.ask(repair_prompt)
                        if handle is None or handle.terminal != "converged":
                            code = 1
                        else:
                            post_receipt = await asyncio.to_thread(
                                export_and_replay,
                                repl.state.session_id,
                                ws,
                                artifact_store=store,
                            )
                            post_status = (
                                post_receipt.get("status", "unknown")
                                if isinstance(post_receipt, dict)
                                else "unknown"
                            )
                            print(
                                f"bench-multiturn post-repair replay status={post_status}",
                                file=sys.stderr,
                                flush=True,
                            )
                            summary_path = ws / "provenance" / "post_repair_replay_summary.json"
                            summary_path.parent.mkdir(parents=True, exist_ok=True)
                            summary_path.write_text(
                                json.dumps(
                                    {
                                        "initial_status": receipt.get("status"),
                                        "post_repair_status": post_status,
                                        "post_repair_verified": post_status == "ok",
                                        "post_repair_receipt": (
                                            post_receipt if isinstance(post_receipt, dict) else {}
                                        ),
                                    },
                                    indent=2,
                                    ensure_ascii=False,
                                ),
                                encoding="utf-8",
                            )
            finally:
                released = await _release(app)
                if not released:
                    code = 1
    finally:
        if records is not None:
            _replay(records)
    return code

raise SystemExit(asyncio.run(_run_multiturn()))
"""

_PARTICIPANT_SCRIPT = _MULTITURN_DRIVER


def _resolve_turn_prompts(paths: RunPaths, default_turn2: str = "") -> tuple[list[Path], dict[str, Any]]:
    prompt_text = paths.prompt.read_text(encoding="utf-8") if paths.prompt.is_file() else ""
    perturbation: dict[str, Any] = {}
    turns_dir = paths.meta / "turns"
    turns_dir.mkdir(parents=True, exist_ok=True)

    turns_json = paths.workspace / "turns.json"
    if not turns_json.is_file():
        turns_json = paths.prompt.parent / "turns.json"
    if turns_json.is_file():
        try:
            spec = json.loads(turns_json.read_text(encoding="utf-8"))
            if isinstance(spec, dict):
                perturbation = spec.get("perturbation") or {}
                raw_turns = spec.get("turns") or []
                if raw_turns:
                    files: list[Path] = []
                    for i, content in enumerate(raw_turns, 1):
                        p = turns_dir / f"turn_{i}.md"
                        p.write_text(str(content), encoding="utf-8")
                        files.append(p)
                    return files, perturbation
        except (OSError, ValueError):
            pass

    for sep in ("<!-- TURN_2 -->", "\n---TURN_2---\n"):
        if sep in prompt_text:
            t1, t2 = prompt_text.split(sep, 1)
            p1 = turns_dir / "turn_1.md"
            p2 = turns_dir / "turn_2.md"
            p1.write_text(t1.strip() + "\n", encoding="utf-8")
            p2.write_text(t2.strip() + "\n", encoding="utf-8")
            return [p1, p2], perturbation

    for cand in (paths.workspace / "turn2_prompt.md", paths.prompt.parent / "turn2_prompt.md"):
        if cand.is_file():
            return [paths.prompt, cand], perturbation

    if default_turn2.strip():
        p2 = turns_dir / "turn_2.md"
        p2.write_text(default_turn2.strip() + "\n", encoding="utf-8")
        return [paths.prompt, p2], perturbation

    return [paths.prompt], perturbation


class OmicsClawMultiTurnAdapter(OmicsClawAdapter):
    """Multi-turn wrapper over :class:`OmicsClawAdapter` for E1 revision tasks."""

    reserved_env = OmicsClawAdapter.reserved_env
    workspace_state = OmicsClawAdapter.workspace_state

    def __init__(self, arm: Arm) -> None:
        unknown = sorted(set(arm.options) - _MT_OPTIONS)
        if unknown:
            raise ManifestError(
                f"arm {arm.id!r}: unknown omicsclaw-multiturn option(s) {', '.join(unknown)}"
            )
        base_options = {
            k: v for k, v in arm.options.items() if k in {"python", "source_root", "capture_content"}
        }
        base_arm = Arm(
            id=arm.id,
            adapter="omicsclaw",
            env=arm.env,
            options=base_options,
            skills_dir=arm.skills_dir,
            permission_rules=arm.permission_rules,
        )
        super().__init__(base_arm)
        self._is_multiturn = True
        self._mt_arm = arm
        self._turn2_prompt = str(arm.options.get("turn2_prompt") or "")
        self._perturbation = arm.options.get("perturbation") or {}
        self._enforce_replay = bool(arm.options.get("enforce_replay_gate", False))
        self._reasoning_effort = str(arm.options.get("reasoning_effort") or "")

    def launch(
        self,
        run: RunSpec,
        paths: RunPaths,
        budget: Budget,
        base_env: Mapping[str, str],
    ) -> Launch:
        single = super().launch(run, paths, budget, base_env)
        turn_files, file_perturbation = _resolve_turn_prompts(paths, self._turn2_prompt)
        perturbation = self._perturbation or file_perturbation
        driver_cfg = {
            "source_root": self._source_root,
            "workspace": str(paths.workspace),
            "provider": run.model.provider,
            "model": run.model.model,
            "turns": [str(p) for p in turn_files],
            "perturbation": perturbation,
            "enforce_replay_gate": self._enforce_replay,
            "reasoning_effort": self._reasoning_effort,
        }
        argv = (
            self._python,
            "-P",
            "-c",
            _MULTITURN_DRIVER,
            json.dumps(driver_cfg),
        )
        env = dict(single.env)
        if self._reasoning_effort:
            env["OMICSCLAW_REASONING_EFFORT"] = self._reasoning_effort
        return Launch(
            argv=argv,
            env=env,
            cwd=single.cwd,
            harness_env=single.harness_env,
            provenance={**single.provenance, "multiturn_count": len(turn_files)},
        )

    def run(
        self,
        run: RunSpec,
        paths: RunPaths,
        budget: Budget,
        base_env: Mapping[str, str] | None = None,
    ) -> ProcessExit:
        import os
        from ..process import run_process

        spec = self.launch(run, paths, budget, base_env if base_env is not None else os.environ)
        paths.meta.mkdir(parents=True, exist_ok=True)
        paths.command.write_text(
            json.dumps(
                {
                    "argv": list(spec.argv),
                    "cwd": str(spec.cwd),
                    "harness_env": dict(spec.harness_env),
                    "provenance": dict(spec.provenance),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return run_process(
            spec.argv,
            env=spec.env,
            cwd=spec.cwd,
            stdout=paths.stdout,
            stderr=paths.stderr,
            wall_clock_s=float(budget.wall_clock_s),
            kill_grace_s=float(getattr(budget, "kill_grace_s", 5.0)),
        )
