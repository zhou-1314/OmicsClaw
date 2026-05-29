"""LLM-driven plan/write/run/repair loop for autonomous analysis."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Protocol

from .contracts import (
    AutonomousAttempt,
    AutonomousRunRequest,
    AutonomousRunResult,
    AutonomousRunStatus,
)
from .executor import execute_command_with_approval
from .runner import _first_error, write_run_records
from .validation import validate_generated_code
from .workspace import create_workspace


class AutonomousLLMClient(Protocol):
    """Minimal protocol used by the autonomous code loop."""

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str | None:
        ...


class ProviderChatClient:
    """OpenAI-compatible one-shot client using OmicsClaw provider defaults."""

    def __init__(self, *, model: str = "", provider: str = "") -> None:
        self.model = str(model or "").strip()
        self.provider = str(provider or "").strip()

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str | None:
        from omicsclaw.providers.runtime import resolve_provider_runtime
        import requests

        runtime = resolve_provider_runtime(provider=self.provider, model=self.model)
        if not runtime.api_key:
            return None
        try:
            response = requests.post(
                f"{(runtime.base_url or 'https://api.openai.com/v1').rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {runtime.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": runtime.model or self.model or "gpt-5-mini",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                },
                timeout=30.0,
            )
        except Exception:
            return None
        if response.status_code != 200:
            return None
        try:
            return str(response.json()["choices"][0]["message"]["content"]).strip()
        except (KeyError, TypeError, ValueError, IndexError):
            return None


def run_autonomous_code_loop(
    request: AutonomousRunRequest,
    *,
    llm_client: AutonomousLLMClient | None = None,
    request_tool_approval: Any = None,
    runtime_context: dict[str, Any] | None = None,
) -> AutonomousRunResult:
    """Synchronous wrapper around ``run_autonomous_code_loop_async``."""
    import asyncio

    return asyncio.run(
        run_autonomous_code_loop_async(
            request,
            llm_client=llm_client,
            request_tool_approval=request_tool_approval,
            runtime_context=runtime_context,
        )
    )


async def run_autonomous_code_loop_async(
    request: AutonomousRunRequest,
    *,
    llm_client: AutonomousLLMClient | None = None,
    request_tool_approval: Any = None,
    runtime_context: dict[str, Any] | None = None,
) -> AutonomousRunResult:
    """Run the first real autonomous code generation loop.

    The loop is deliberately compact: ask for JSON plan+code, execute it,
    and allow at most ``request.max_repair_attempts`` evidence-bound repairs.
    """
    client = llm_client or ProviderChatClient(
        model=request.model_override,
        provider=request.provider_override,
    )
    workspace = create_workspace(request)
    attempts: list[AutonomousAttempt] = []
    plan = ""
    code = ""
    last_error = ""
    repair_rounds_used = 0
    generated_code_path = ""

    for repair_index in range(max(0, request.max_repair_attempts) + 1):
        repair_rounds_used = repair_index
        prompt = (
            _build_initial_prompt(request)
            if repair_index == 0
            else _build_repair_prompt(
                request,
                previous_plan=plan,
                previous_code=code,
                attempts=attempts,
                last_error=last_error,
            )
        )
        raw_response = client.complete(prompt, temperature=0.0)
        parsed = _parse_llm_response(raw_response or "")
        plan = parsed.get("analysis_plan", plan or request.goal)
        code = parsed.get("code", "")
        if not code:
            last_error = "LLM did not return executable code."
            break
        static_issues = validate_generated_code(code, language=request.language)
        if static_issues:
            last_error = "Generated code failed static validation:\n- " + "\n- ".join(static_issues)
            if repair_index >= max(0, request.max_repair_attempts):
                break
            continue

        script_path = _write_script(
            workspace.root,
            language=request.language,
            attempt_index=repair_index,
            code=code,
        )
        generated_code_path = str(script_path)
        attempt = await _run_generated_script(
            workspace,
            request=request,
            script_path=script_path,
            attempt_index=repair_index,
            request_tool_approval=request_tool_approval,
            runtime_context=runtime_context,
        )
        attempts.append(attempt)
        if attempt.status.value == "succeeded":
            last_error = ""
            break
        last_error = _attempt_evidence(attempt)

    status = attempts[-1].status if attempts else AutonomousRunStatus.FAILED
    metadata = {
        **dict(request.metadata),
        "analysis_plan": plan,
        "generated_code_language": request.language,
        "generated_code_path": generated_code_path,
        "repair_attempts_used": repair_rounds_used,
        "computed_results": _collect_computed_results(workspace.root),
        "interpretive_notes": _collect_interpretive_notes(workspace.root),
        "last_repair_evidence": last_error,
        "llm_loop": True,
    }
    result = AutonomousRunResult(
        run_id=workspace.run_id,
        workspace_root=str(workspace.root),
        status=status,
        attempts=attempts,
        error=_first_error(attempts) or last_error or "No generated code was executed.",
        metadata=metadata,
    )
    if status.value == "succeeded":
        result.error = ""
    manifest_path, completion_report_path = write_run_records(
        workspace,
        request=request,
        result=result,
    )
    result.manifest_path = str(manifest_path)
    result.completion_report_path = str(completion_report_path)
    return result


def _build_initial_prompt(request: AutonomousRunRequest) -> str:
    lines = [
        "You are OmicsClaw Autonomous Code Runner.",
        "Return ONLY a JSON object with keys: analysis_plan, code, notes.",
        "Generate one self-contained analysis script.",
        "Use Python unless the requested language is R.",
        "Write all outputs inside AUTONOMOUS_OUTPUT_DIR / OUTPUT_PATH.",
        "Do not install packages, use shell commands, or access the network.",
        "Separate computed outputs from interpretation in files when possible.",
    ]
    if request.data_schema.strip():
        lines.append(
            "Treat the input data schema below as ground truth for keys, columns, "
            "and shapes; read real obs/var/obsm/layers names from it instead of guessing."
        )
    lines.extend(
        [
            "",
            f"Requested language: {request.language}",
            f"Goal: {request.goal}",
        ]
    )
    if request.analysis_plan.strip():
        lines.append(f"Approved analysis plan:\n{request.analysis_plan}")
    if request.data_schema.strip():
        lines.append(f"Input data schema:\n{request.data_schema}")
    lines.extend(
        [
            f"Input path references: {[str(item) for item in request.input_paths]}",
            f"Upstream artifact references: {[str(item) for item in request.upstream_paths]}",
            f"Local context: {request.context}",
            f"External/web context supplied by outer flow: {request.web_context}",
        ]
    )
    return "\n".join(lines)


def _build_repair_prompt(
    request: AutonomousRunRequest,
    *,
    previous_plan: str,
    previous_code: str,
    attempts: list[AutonomousAttempt],
    last_error: str,
) -> str:
    evidence = {
        "last_error": last_error,
        "attempts": [attempt.to_dict() for attempt in attempts],
    }
    lines = [
        "Repair the OmicsClaw autonomous analysis code using only this evidence.",
        "Return ONLY a JSON object with keys: analysis_plan, code, notes.",
        "Do not introduce network access, package installation, shell commands, or external writes.",
        "",
        f"Goal: {request.goal}",
    ]
    if request.data_schema.strip():
        lines.append(f"Input data schema:\n{request.data_schema}")
    lines.extend(
        [
            f"Previous plan: {previous_plan}",
            f"Previous code:\n{previous_code}",
            f"Failure evidence JSON:\n{json.dumps(evidence, indent=2, ensure_ascii=False)}",
        ]
    )
    return "\n".join(lines)


def _parse_llm_response(raw: str) -> dict[str, str]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"analysis_plan": "", "code": text, "notes": ""}
    if not isinstance(payload, dict):
        return {"analysis_plan": "", "code": "", "notes": ""}
    return {
        "analysis_plan": str(payload.get("analysis_plan", "") or ""),
        "code": str(payload.get("code", "") or ""),
        "notes": str(payload.get("notes", "") or ""),
    }


def _write_script(
    workspace_root: Path,
    *,
    language: str,
    attempt_index: int,
    code: str,
) -> Path:
    scripts_dir = workspace_root / "scripts"
    suffix = ".R" if language.lower() in {"r", "rscript"} else ".py"
    path = scripts_dir / f"attempt_{attempt_index}{suffix}"
    path.write_text(code.rstrip() + "\n", encoding="utf-8")
    return path


async def _run_generated_script(
    workspace,
    *,
    request: AutonomousRunRequest,
    script_path: Path,
    attempt_index: int,
    request_tool_approval: Any,
    runtime_context: dict[str, Any] | None,
) -> AutonomousAttempt:
    if request.language.lower() in {"r", "rscript"}:
        argv = ["Rscript", str(script_path)]
    else:
        argv = [sys.executable, str(script_path)]
    return await execute_command_with_approval(
        workspace,
        argv,
        attempt_index=attempt_index,
        request=request,
        timeout_seconds=request.timeout_seconds,
        request_tool_approval=request_tool_approval,
        runtime_context=runtime_context,
    )


def _attempt_evidence(attempt: AutonomousAttempt) -> str:
    stderr = ""
    try:
        stderr = Path(attempt.stderr_log).read_text(encoding="utf-8")[-4000:]
    except OSError:
        stderr = ""
    return "\n".join(
        part
        for part in [
            f"status={attempt.status.value}",
            f"exit_code={attempt.exit_code}",
            attempt.error,
            stderr,
        ]
        if part
    )


def _collect_computed_results(workspace_root: Path) -> str:
    computed_path = workspace_root / "result_computed.md"
    if computed_path.exists():
        return computed_path.read_text(encoding="utf-8")[:12000]
    tables_dir = workspace_root / "tables"
    table_names = sorted(path.name for path in tables_dir.glob("*") if path.is_file())
    if table_names:
        return "Generated tables:\n" + "\n".join(f"- tables/{name}" for name in table_names)
    return ""


def _collect_interpretive_notes(workspace_root: Path) -> str:
    notes_path = workspace_root / "interpretive_notes.md"
    if notes_path.exists():
        return notes_path.read_text(encoding="utf-8")[:12000]
    return ""
