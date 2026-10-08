"""The OmicsClaw adapter: one ``oc cli --prompt-file`` process per run.

The process is started from a chosen source tree with telemetry written to
standard error, and everything the harness learns about the run is read
from three files it leaves in ``meta``: ``stderr.txt`` (one JSON line per
telemetry span), ``stdout.txt`` (the terminal transcript) and
``audit.jsonl`` (one line per tool call).

Arm options, all optional:

``python``
    The interpreter to run. Default: the one running the harness.
``source_root``
    The checkout whose ``omicsclaw`` package the process imports. Default:
    the checkout this module belongs to. An empty string uses whatever the
    interpreter has installed.
``capture_content``
    Whether telemetry records tool arguments, which the access audit
    reads. Default ``true``.

Usage is the sum over ``omicsclaw.llm_request`` spans, one per model call.
That includes the calls sub-agents make, which the per-turn token lines on
standard output leave out.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..layout import RunPaths
from ..manifest import Arm, Budget, ManifestError, RunSpec
from ..outcome import Command, Evidence, ProcessExit, Usage
from ..process import MARKER_VARIABLE
from . import Launch

__all__ = ["OmicsClawAdapter", "Trace", "read_trace"]

_BOOTSTRAP = (
    "import sys\n"
    "root = sys.argv.pop(1)\n"
    "if root:\n"
    "    sys.path.insert(0, root)\n"
    "import omicsclaw\n"
    "print('bench-launch omicsclaw=' + str(omicsclaw.__file__),"
    " file=sys.stderr, flush=True)\n"
    "from omicsclaw.launch import main\n"
    "raise SystemExit(main())\n"
)
"""Runs the launch shell after putting the source tree first on the import
path, and says on standard error which ``omicsclaw`` was imported."""

_LAUNCH_LINE = re.compile(r"^bench-launch omicsclaw=(.*)$", re.MULTILINE)
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_APPROVAL = re.compile(r"^Approval (required|denied|granted) \[", re.MULTILINE)
_FAILED = re.compile(r"^Failed: (\S+)", re.MULTILINE)

_SPAN_INTERACTION = "omicsclaw.interaction"
_SPAN_LLM = "omicsclaw.llm_request"
_SPAN_TOOL = "omicsclaw.tool"
_TOKENS_IN = "llm.tokens.input"
_TOKENS_OUT = "llm.tokens.output"
_TOKENS_CACHED = "llm.tokens.cache_read"
_CONTENT_IN = "langfuse.observation.input"
_CONTENT_OUT = "langfuse.observation.output"

_CANCELLED = frozenset({"CancelledError", "GeneratorExit", "KeyboardInterrupt"})
"""Errors a model call ends with when the run around it is being stopped."""

_OPTIONS = frozenset({"python", "source_root", "capture_content"})


@dataclass(frozen=True)
class Trace:
    """The telemetry of one run, as read from its standard error.

    :param spans: Every span line, in the order the process wrote them,
        which is the order the spans ended in.
    :param metrics: The metric summary the process writes on a clean
        shutdown; empty when it did not get that far.
    :param launched_from: The ``omicsclaw/__init__.py`` the process
        imported, or ``""`` when it did not say.
    """

    spans: tuple[Mapping[str, Any], ...] = ()
    metrics: Mapping[str, Any] = field(default_factory=dict)
    launched_from: str = ""

    def named(self, name: str) -> list[Mapping[str, Any]]:
        """The spans called *name*, in order."""
        return [span for span in self.spans if span.get("name") == name]


def read_trace(stderr: Path) -> Trace:
    """Parse a run's ``stderr.txt``. Never raises.

    Lines that are not a JSON object with a ``name`` or a ``metrics`` key
    are skipped, so log lines and a cut-off last line do no harm. A missing
    file is an empty trace.
    """
    try:
        text = stderr.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return Trace()
    spans: list[Mapping[str, Any]] = []
    metrics: Mapping[str, Any] = {}
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if isinstance(record.get("metrics"), dict):
            metrics = record["metrics"]
        elif isinstance(record.get("name"), str):
            spans.append(record)
    launched = _LAUNCH_LINE.search(text)
    return Trace(
        spans=tuple(spans),
        metrics=metrics,
        launched_from=launched.group(1).strip() if launched else "",
    )


class OmicsClawAdapter:
    """Runs and reads back ``oc cli --prompt-file``. See the module docstring."""

    reserved_env = frozenset({
        "OMICSCLAW_AUDIT_LOG",
        "OMICSCLAW_OTEL_CAPTURE_CONTENT",
        "OMICSCLAW_WORKSPACE",
        "OTEL_ENABLED",
        "OTEL_EXPORTER_TYPE",
        MARKER_VARIABLE,
    })
    workspace_state = (".omicsclaw",)

    def __init__(self, arm: Arm) -> None:
        """
        :raises ManifestError: The arm sets an option this adapter does not
            have, or one of the wrong type.
        """
        unknown = sorted(set(arm.options) - _OPTIONS)
        if unknown:
            raise ManifestError(
                f"arm {arm.id!r}: unknown omicsclaw option(s) {', '.join(unknown)}"
            )
        python = arm.options.get("python", sys.executable)
        source_root = arm.options.get("source_root", str(_checkout()))
        capture = arm.options.get("capture_content", True)
        if not isinstance(python, str) or not python:
            raise ManifestError(f"arm {arm.id!r}: options.python must be a path")
        if not isinstance(source_root, str):
            raise ManifestError(f"arm {arm.id!r}: options.source_root must be a path")
        if not isinstance(capture, bool):
            raise ManifestError(
                f"arm {arm.id!r}: options.capture_content must be true or false"
            )
        self._arm = arm
        self._python = python
        self._source_root = str(Path(source_root).resolve()) if source_root else ""
        self._capture = capture

    def launch(
        self,
        run: RunSpec,
        paths: RunPaths,
        budget: Budget,
        base_env: Mapping[str, str],
    ) -> Launch:
        """One ``oc cli`` exchange over the staged prompt, in the workspace.

        The arm's ``env`` overrides the defaults set here (auto-approve
        permission mode, the skills root, the turn ceiling); the telemetry
        and audit-log settings cannot be overridden.
        """
        arm = self._arm
        harness: dict[str, str] = {"OMICSCLAW_PERMISSION_MODE": "auto-approve"}
        skills = arm.skills_dir
        if skills is None and self._source_root:
            skills = Path(self._source_root) / "skills"
        if skills is not None:
            harness["OMICSCLAW_SKILLS_DIR"] = str(skills)
        if budget.max_turns is not None:
            harness["OMICSCLAW_MAX_TURNS"] = str(budget.max_turns)
        if arm.permission_rules is not None:
            harness["OMICSCLAW_PERMISSION_RULES"] = str(arm.permission_rules)
        harness.update(arm.env)
        harness.update({
            "OMICSCLAW_AUDIT_LOG": str(paths.audit_log),
            "OTEL_ENABLED": "true",
            "OTEL_EXPORTER_TYPE": "stdout",
            "OMICSCLAW_OTEL_CAPTURE_CONTENT": "true" if self._capture else "false",
        })
        env = {
            name: value
            for name, value in base_env.items()
            if name != "OMICSCLAW_WORKSPACE"
        }
        env.update(harness)

        argv = [
            self._python, "-P", "-c", _BOOTSTRAP, self._source_root,
            "cli", "--workspace", str(paths.workspace),
        ]
        if run.model.provider:
            argv += ["--provider", run.model.provider]
        if run.model.model:
            argv += ["--model", run.model.model]
        argv += ["--", "--prompt-file", str(paths.prompt)]
        return Launch(
            argv=tuple(argv), env=env, cwd=paths.workspace, harness_env=harness
        )

    def collect(self, run: RunSpec, paths: RunPaths, exit: ProcessExit) -> Evidence:
        """Read the run's telemetry and transcript. Never raises."""
        trace = read_trace(paths.stderr)
        transcript = _ANSI.sub("", _read(paths.stdout))

        calls = trace.named(_SPAN_LLM)
        tools = trace.named(_SPAN_TOOL)
        tool_ids = {str(span.get("span_id", "")) for span in tools}
        interactions = trace.named(_SPAN_INTERACTION)
        ending = _attributes(interactions[-1]) if interactions else {}

        approvals = [match.group(1) for match in _APPROVAL.finditer(transcript)]
        required = approvals.count("required")
        denied = approvals.count("denied")
        settled = denied + approvals.count("granted")

        # The command prints ``Failed: <Error>`` for a failed exchange and
        # then exits 1. With any other exit status the words are the
        # agent's own text.
        failed = _FAILED.search(transcript) if exit.returncode == 1 else None
        failure = f"Failed: {failed.group(1)}" if failed else ""
        stop_reason = str(ending.get("agent.stop_reason", ""))
        if failed and failed.group(1) == "TimeoutError":
            stop_reason = "timeout"

        models = sorted({
            str(_attributes(span)["llm.model"])
            for span in calls
            if "llm.model" in _attributes(span)
        })
        turns = ending.get("agent.turns")
        return Evidence(
            stop_reason=stop_reason,
            infra_reason=self._infra_reason(trace, calls, tool_ids),
            failure=failure,
            approvals_required=required,
            approvals_denied=denied,
            approvals_pending=max(0, required - settled),
            turns=turns if isinstance(turns, int) else None,
            model_resolved=",".join(models),
            usage=_usage(calls, tool_ids),
            commands=_commands(calls, tools),
            notes={
                "omicsclaw_file": trace.launched_from,
                "session_id": str(ending.get("session.id", "")),
                "ended_with": str(interactions[-1].get("error", ""))
                if interactions
                else "",
                "meter": _meter_check(trace.metrics, calls),
            },
        )

    def _infra_reason(
        self,
        trace: Trace,
        calls: list[Mapping[str, Any]],
        tool_ids: set[str],
    ) -> str:
        """Why this run is not the agent's doing, or ``""``.

        Two things qualify. The process imported ``omicsclaw`` from
        somewhere other than the configured source tree. Or a model call
        was not answered and nothing recovered it. That is judged per
        parent span (a turn of the main loop, or the tool call a sub-agent
        runs in) on the last call made under it, leaving out calls that
        were cancelled because the run was being stopped:

        - the call ended in an error: ``provider_error: <Error>``;
        - the call ended without an error and reported no input tokens:
          ``provider_error: empty_response``. A backend that answers has
          read the prompt, so this is a reply with nothing behind it, such
          as a ``200`` carrying an error body or a stream cut short.

        Either way the agent may carry on and exit ``0``, so neither shows
        in the exit status. A reply that reports its input tokens and has
        no text is left alone; it may be what the model said.
        """
        imported = trace.launched_from
        if self._source_root and imported:
            root = Path(self._source_root)
            if imported == "None" or root not in Path(imported).resolve().parents:
                return f"source_mismatch: imported omicsclaw from {imported}"
        last: dict[str, Mapping[str, Any]] = {}
        for span in calls:
            if _error(span) not in _CANCELLED:
                last[str(span.get("parent_span_id", ""))] = span
        for parent, span in last.items():
            where = " in a sub-agent" if parent in tool_ids else ""
            error = _error(span)
            if error:
                status = _attributes(span).get("error.status_code")
                detail = f" status={status}" if status is not None else ""
                return f"provider_error: {error}{detail}{where}"
            if not _answered(span):
                return f"provider_error: empty_response{where}"
        return ""


def _checkout() -> Path:
    """The source tree this module was imported from."""
    return Path(__file__).resolve().parents[3]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _attributes(span: Mapping[str, Any]) -> Mapping[str, Any]:
    attributes = span.get("attributes")
    return attributes if isinstance(attributes, dict) else {}


def _error(span: Mapping[str, Any]) -> str:
    return str(span.get("error") or _attributes(span).get("error.type") or "")


def _number(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def _answered(span: Mapping[str, Any]) -> bool:
    """Whether a model call reported input tokens, which an answer always has."""
    return _number(_attributes(span).get(_TOKENS_IN)) > 0


def _usage(calls: list[Mapping[str, Any]], tool_ids: set[str]) -> Usage:
    """Token and call totals over every model call of the run.

    A call belongs to a sub-agent when its parent span is a tool call. A
    call counts towards ``calls_without_usage`` when it reported no input
    tokens: it failed, was cancelled, came back empty, or its backend
    reports no usage.
    """
    reported = [span for span in calls if _answered(span)]
    errors = [_error(span) for span in calls]
    return Usage(
        input_tokens=sum(_number(_attributes(s)[_TOKENS_IN]) for s in reported),
        cached_input_tokens=sum(
            _number(_attributes(s).get(_TOKENS_CACHED)) for s in reported
        ),
        output_tokens=sum(_number(_attributes(s).get(_TOKENS_OUT)) for s in reported),
        llm_calls=len(calls),
        llm_errors=sum(1 for error in errors if error and error not in _CANCELLED),
        llm_cancelled=sum(1 for error in errors if error in _CANCELLED),
        subagent_llm_calls=sum(
            1 for span in calls if str(span.get("parent_span_id", "")) in tool_ids
        ),
        calls_without_usage=len(calls) - len(reported),
        includes_subagents=True,
        source="telemetry spans (omicsclaw.llm_request)",
    )


def _commands(
    calls: list[Mapping[str, Any]], tools: list[Mapping[str, Any]]
) -> tuple[Command, ...]:
    """The tool calls the model asked for, as far as telemetry kept them.

    Calls that ran come from their tool span. A call that was refused
    before it ran has no tool span, so the model's own output is read as
    well and contributes the calls not already seen. Each recorded value
    holds at most 4096 bytes; output that was cut is kept as raw text.
    Empty when the run did not capture content.
    """
    commands = [
        Command(str(_attributes(span).get("tool.name", "")), str(text))
        for span in tools
        if (text := _attributes(span).get(_CONTENT_IN))
    ]
    seen = {(command.tool, command.text) for command in commands}
    for span in calls:
        output = _attributes(span).get(_CONTENT_OUT)
        if not isinstance(output, str) or not output.startswith("[{"):
            continue
        try:
            requested = json.loads(output)
        except ValueError:
            commands.append(Command("model_output", output))
            continue
        for call in requested if isinstance(requested, list) else []:
            if not isinstance(call, dict):
                continue
            command = Command(str(call.get("name", "")), str(call.get("arguments", "")))
            if (command.tool, command.text) not in seen:
                seen.add((command.tool, command.text))
                commands.append(command)
    return tuple(commands)


def _meter_check(metrics: Mapping[str, Any], calls: list[Mapping[str, Any]]) -> str:
    """Whether the process's own token counters match the span totals.

    ``agrees``, ``differs``, or ``absent`` when the process wrote no metric
    summary, which is what a killed process leaves.
    """
    if not metrics:
        return "absent"
    for metric, key in (
        ("omicsclaw.llm.tokens.input", _TOKENS_IN),
        ("omicsclaw.llm.tokens.output", _TOKENS_OUT),
    ):
        series = metrics.get(metric, [])
        metered = sum(_number(entry.get("total")) for entry in series)
        if metered != sum(_number(_attributes(span).get(key)) for span in calls):
            return "differs"
    return "agrees"
