# Autonomous Analysis Path and Autonomous Code Runner

## Status

Accepted (2026-05-28). Amended by [ADR 0014](0014-outer-owned-autonomous-understanding.md)
(2026-05-29): intent understanding, data inspection, and result validation move
to the outer agent loop; `Evidence-bound repair` is narrowed to a
runner-internal rule (stderr, exit code, injected schema). Further amended
by [ADR 0032](0032-autonomous-code-mini-agent.md) (2026-06-21): the one-shot
`code_loop.py` Autonomous Code Runner is replaced by a bounded persistent-kernel
Autonomous Code Mini-Agent (curated skill-handle facade + replay validation);
skill-first routing, permission tiers, and output-shape parity are unchanged.
Control-plane consolidation (2026-06-22): the `off`/`assist`/`auto` analysis-router
mode selector, `OMICSCLAW_ANALYSIS_ROUTER_MODE`, and the `auto` deterministic-dispatch
path were removed — routing is now unconditionally assist-style (no mode knob).

## Context

OmicsClaw's current chat flow is strongest when a user request maps to one of the registered analysis skills, but it is less flexible for requests that need bespoke code, post-processing, custom figures, or methods not yet packaged as skills. A reference implementation's `DecisionExecutionEngine` demonstrates a useful structured plan/tool/repair loop, but replacing OmicsClaw's `run_query_engine` would discard existing Surface, tool-policy, approval, memory, completion-report, and skill-runner contracts.

## Decision

Introduce an **Autonomous Analysis Path** as a first-class analysis route without replacing the existing chat engine. An **Analysis Router** will classify analysis-intent requests after slash-command and preflight handling in `runtime/agent/loop.py`, before `run_engine_loop`:

- **Exact skill match**: the router deterministically selects the skill; LLM-assisted preflight fills parameters and asks for confirmation when needed.
- **Partial skill match**: use **Skill-first composition**; run the nearest built-in skill first, then pass upstream artifact references to the autonomous path for the uncovered work.
- **No skill match**: enter the autonomous path directly.
- Non-analysis chat continues through the existing `run_engine_loop` / `run_query_engine` path.

The first integration slice wired route-context injection behind `OMICSCLAW_ANALYSIS_ROUTER_ENABLED`. The current opt-in slice adds a deterministic dispatcher at the same point: it builds planned tool calls for exact, partial, and no-skill routes and executes them through the existing `ToolExecutionRequest` / tool-policy / approval / transcript pipeline. The flag still defaults off, so live prompts do not change by default.

Generated-code execution will live under a new first-class package, `omicsclaw/autonomous/`, as an **Autonomous Code Runner**. It is independent of the skill runner, owns a bounded plan-write-run-inspect-revise-report loop, and uses a controlled `exec_command`-style command plane rather than the current notebook-only `custom_analysis_execute` path.

The Autonomous Code Runner will create an isolated run workspace named `autonomous-code__<timestamp>__<id>` under the output root. It keeps output-shape parity with skill runs (`manifest.json`, `completion_report.json`, logs, generated code, summary, figures/tables/artifacts) while labeling the source as autonomous code, not as a skill. Upstream skill outputs and input data are recorded as references by default, not copied.

The runner's internal LLM calls default to the current request's provider and model, with optional per-run overrides. The first runnable loop uses the shared provider runtime resolver; a later slice should attach the same token-usage accumulator used by the outer chat engine.

The first implementation supports generated Python and R scripts, defaulting to Python. Shell is an execution wrapper, not the generated analysis language: attempts are written as `scripts/attempt_0.py` / `scripts/attempt_0.R` style files, and the selected language is recorded in run metadata.

The runner may consume web or literature context supplied by the outer chat flow, but generated code does not get direct network access by default. Downloads, package installation, and other network-backed mutations are `system_mutation` actions and require explicit approval.

Permission approval reuses the existing tool policy / `request_tool_approval` channel with autonomous-specific policy tags. The runner does not introduce a second approval UI; approvals and denials are recorded in attempt metadata. `analysis_write` now goes through this channel before execution; `system_mutation` remains disabled in the local autonomous runtime even if a Surface returns approval.

When an exact skill route lacks required parameters or needs confirmation, the router reuses OmicsClaw's existing preflight/request-confirmation path. The router emits structured preflight state; Surfaces remain responsible for presenting it.

The dispatcher does not call Python executors directly. It submits planned tool calls to the same query-engine execution path used by LLM-selected tools, preserving Surface callbacks, policy decisions, approval requests, lifecycle hooks, tool result storage, and transcript shape.

`AnalysisRoute.kind` adds a non-analysis `chat` route outside `CapabilityDecision.coverage`; the capability resolver keeps its existing `exact_skill | partial_skill | no_skill` coverage vocabulary.

If the skill step in a partial route fails, the router does not automatically fall through to autonomous code. It stops with the skill failure and completion evidence unless usable core artifacts already exist or the user explicitly asks to bypass the skill and try an autonomous route.

Autonomous reports may include LLM-written scientific interpretation, but generated summaries must separate computed results from interpretive claims. Interpretive claims should cite concrete artifacts, tables, columns, figures, or logs where possible, and every report keeps the OmicsClaw disclaimer.

## Guardrails

- Default repair policy is evidence-bound and bounded: initial execution plus at most two repair attempts based on stderr, exit code, artifact checks, file schema, the user request, and upstream artifacts.
- Commands are classified into three permission tiers: `read_only_probe`, `analysis_write`, and `system_mutation`.
- `system_mutation` is blocked in the local autonomous runtime; it covers package installation, network download, service startup, workspace-external writes, broad deletion, and unknown binary execution.
- `analysis_write` executes only after the existing approval channel returns allow. Python attempts are wrapped with a runtime guard that blocks shell/network actions and constrains writes to the autonomous run workspace.
- The first LLM loop asks for JSON plan/code, writes `scripts/attempt_N.py` or `scripts/attempt_N.R`, executes the script, and performs at most two evidence-bound repairs.
- Autonomous runs expose a job-shaped lifecycle with status, logs, cancellation, retry, artifacts, exit code, and terminal outcome.
- Lifecycle records should align with the existing remote job/artifact/log shape but must not force local CLI or chat flows through the remote jobs router.

## Consequences

The existing `custom_analysis_execute` tool becomes a **Legacy custom analysis adapter**: it may remain for compatibility and may later forward into the Autonomous Code Runner, but it is no longer the recommended generated-code route.

This preserves OmicsClaw's skill-first reproducibility where skills apply, adds reference-style flexible code generation where they do not, and avoids coupling autonomous execution to the skill runner or to the remote HTTP jobs router.
