# Autonomous Analysis Implementation Plan

## Overview

Build the Autonomous Analysis Path as a first-class route that keeps
OmicsClaw's skill-first reproducibility when skills match, and adds an
independent Autonomous Code Runner for generated Python/R analysis when skills
only partially cover or do not cover a request.

## Architecture Decisions

- `AnalysisRouter` chooses between `chat`, `exact_skill`, `partial_skill`, and
  `no_skill`; non-analysis chat continues through the existing chat engine.
- `AutonomousCodeRunner` lives under `omicsclaw/autonomous/` and is independent
  of the skill runner.
- Partial routes use skill-first composition and pass upstream artifacts by
  reference.
- Autonomous runs use output-shape parity with skill runs and job-shaped
  lifecycle records.
- `custom_analysis_execute` remains temporarily as a legacy adapter.

## Task List

### Phase 1: Contracts

#### Task 1: Analysis route contract

**Description:** Add route dataclasses/enums that wrap `CapabilityDecision`
without changing the existing resolver vocabulary.

**Acceptance criteria:**
- `AnalysisRoute.kind` supports `chat`, `exact_skill`, `partial_skill`, and
  `no_skill`.
- Existing `CapabilityDecision.coverage` values remain unchanged.
- Non-analysis requests route to `chat`.

**Verification:**
- `python -m pytest tests/test_analysis_router.py -q`

**Dependencies:** None

**Files likely touched:**
- `omicsclaw/analysis_router/`
- `tests/test_analysis_router.py`

**Estimated scope:** Small

#### Task 2: Autonomous runner contracts

**Description:** Add data contracts for run requests, attempts, status,
permission tiers, and command outcomes.

**Acceptance criteria:**
- Permission tiers are `read_only_probe`, `analysis_write`, `system_mutation`.
- Run status/lifecycle fields align with existing job expectations.
- Contracts expose artifact root, stdout/stderr logs, exit code, and error.
- Foundation execution runs only `read_only_probe`; `analysis_write` is gated
  until approval and controlled write execution are wired.

**Verification:**
- `python -m pytest tests/test_autonomous_code_runner.py -q`

**Dependencies:** None

**Files likely touched:**
- `omicsclaw/autonomous/`
- `tests/test_autonomous_code_runner.py`

**Estimated scope:** Medium

### Checkpoint: Contracts

- `python -m pytest tests/test_analysis_router.py tests/test_autonomous_code_runner.py -q`

### Phase 2: Runner Core

#### Task 3: Workspace and command execution core

**Description:** Create isolated autonomous run workspaces and execute bounded
argv commands with captured logs.

**Acceptance criteria:**
- Workspaces are named `autonomous-code__<timestamp>__<id>`.
- Required subdirectories exist: `scripts`, `logs`, `figures`, `tables`,
- Commands execute with cwd set to the run workspace.
- Attempt stdout/stderr logs are written under `logs/`.
- Paths with `../` or absolute paths outside the run workspace are rejected for
  foundation probe commands.

**Verification:**
- `python -m pytest tests/test_autonomous_code_runner.py -q`

**Dependencies:** Task 2

**Estimated scope:** Medium

#### Task 4: Manifest and completion report

**Description:** Write output-shape parity artifacts for autonomous runs using
existing manifest and completion-report helpers.

**Acceptance criteria:**
- `manifest.json` and `completion_report.json` are written for success and
  failure.
- Metadata labels source as `autonomous_code_runner`.
- `result_summary.md` exists even on failure.

**Verification:**
- `python -m pytest tests/test_autonomous_code_runner.py tests/test_verification.py -q`

**Dependencies:** Task 3

**Estimated scope:** Medium

### Checkpoint: Runner Core

- `python -m pytest tests/test_autonomous_code_runner.py tests/test_analysis_router.py -q`

### Phase 3: Autonomous Code Loop

#### Task 5: LLM plan/write/run loop

**Description:** Add a bounded plan-write-run-inspect-revise-report loop using
the current request's provider/model by default.

**Acceptance criteria:**
- Supports Python and Rscript, defaulting to Python.
- Writes `scripts/attempt_N.py` or `scripts/attempt_N.R`.
- Repairs are evidence-bound and capped at two attempts after the initial run.
- `analysis_write` goes through existing `request_tool_approval` with
  autonomous-specific policy tags before execution.
- Python scripts run behind a runtime guard that blocks shell/network actions
  and workspace-external writes.
- Provider/model resolution reuses OmicsClaw's provider runtime by default,
  with optional request-level model/provider metadata preserved.

**Verification:**
- Unit tests with a fake LLM client and fake approval callback.
- `python -m pytest tests/test_autonomous_code_runner.py -q`

**Dependencies:** Tasks 2-4

**Estimated scope:** Medium

### Phase 4: Routing Integration

#### Task 5.5: Recommended autonomous tool entry

**Description:** Add `autonomous_analysis_execute` as the recommended tool
entry for generated-code analysis while keeping `custom_analysis_execute` as a
legacy adapter.

**Acceptance criteria:**
- Tool visibility is gated by the same implementation-intent predicate as the
  legacy custom analysis tool.
- The executor calls the Autonomous Code Runner loop and returns output,
  manifest, completion report, attempts, and error summary.
- Existing `request_tool_approval` is available to the nested autonomous
  runner through tool runtime context.

**Verification:**
- `python -m pytest tests/test_tool_list_lazy_exposure.py tests/test_query_engine.py::test_run_query_engine_passes_approval_callback_to_tool_runtime_context -q`

**Dependencies:** Task 5

**Estimated scope:** Small

#### Task 6: `llm_tool_loop` router hook

**Description:** Invoke `AnalysisRouter` after slash/preflight handling and
before `run_engine_loop`, then submit deterministic planned tool calls through
the existing query-engine tool execution pipeline.

**Acceptance criteria:**
- `chat` routes continue through `run_engine_loop`.
- `no_skill` routes invoke Autonomous Code Runner through
  `autonomous_analysis_execute`.
- `exact_skill` uses deterministic route with existing parameter/preflight
  support.
- Partial route does not auto-fallback when the skill step fails.
- Dispatcher and route-context injection remain behind
  `OMICSCLAW_ANALYSIS_ROUTER_ENABLED`.
- Planned tool calls preserve policy, approval, Surface callbacks,
  tool-result storage, and transcript shape.

**Verification:**
- `python -m pytest tests/test_analysis_dispatcher_contract.py tests/bot/test_agent_loop.py::test_llm_tool_loop_dispatches_no_skill_route_without_llm -q`
- Existing interactive/desktop routing tests still pass where targeted.

**Dependencies:** Tasks 1-5

**Estimated scope:** Medium

### Phase 5: Migration And Polish

#### Task 7: Legacy adapter and docs

**Description:** Downgrade `custom_analysis_execute` to a legacy adapter path
and update contributor/user documentation.

**Acceptance criteria:**
- New prompts prefer Autonomous Code Runner.
- Existing `custom_analysis_execute` tests continue to pass or are updated to
  reflect adapter behavior.
- README mentions the new autonomous route without overstating completion.

**Verification:**
- Targeted legacy custom analysis tests.
- Documentation fact tests, if affected.

**Dependencies:** Tasks 1-6

**Estimated scope:** Medium

## Risks And Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Generated commands mutate files outside the run workspace | High | Permission tiers, path checks, and existing approval channel |
| Router hijacks ordinary chat | Medium | Separate `chat` route outside `CapabilityDecision.coverage` |
| Partial route silently replaces failed skill execution | High | Stop on failed skill unless usable artifacts exist or user explicitly bypasses |
| Long autonomous runs block Surfaces | Medium | Job-shaped lifecycle with logs/cancel/retry semantics |
| Output readers fork on autonomous vs skill runs | Medium | Output-shape parity through manifest/completion-report helpers |

## Open Questions

- Should the first runnable integration support `exact_skill`, `partial_skill`,
  and `no_skill` together, or ship `no_skill` first behind a feature flag?
- Should autonomous routes be opt-in initially via configuration while the
  command permission classifier matures?
