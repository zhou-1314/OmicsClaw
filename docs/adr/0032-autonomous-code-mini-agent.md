# Autonomous Code Mini-Agent for bounded no-skill analysis

## Status

Accepted (2026-06-21). Amends ADR 0013 by replacing the one-shot
`analysis_plan + code` Autonomous Code Runner loop with a bounded tactical
mini-agent loop for Partial / No skill fallback work. Supersedes ADR 0014's
central "runner has no brain" decision, while preserving ADR 0014's two outer
judgment seams: consequential ambiguity is still surfaced through the outer
preflight channel before execution is committed, and final result acceptance is
still owned by the outer loop. Does not supersede ADR 0013's skill-first router,
permission tiers, output-shape parity, or local-first constraints.

**Revised 2026-06-22 (single-engine consolidation).** The mini-agent is now the
**single** autonomous engine and runs **automatically** — the
`OMICSCLAW_AUTONOMOUS_MINI_AGENT` flag and the legacy one-shot engine were
removed: the `executor` / `permissions` / `policy` modules were deleted
outright, while `code_loop.py` survives as a thin capability-gate → run/refuse
dispatcher and `runtime_guard.py` was repurposed from the one-shot subprocess
guard into the non-bwrap in-kernel guard (`build_kernel_guard_code`). Isolation
is now **tiered**: the
bubblewrap OS envelope (ZMQ-IPC over a short bind-mounted socket dir, AF_UNIX
107-char limit) where available, otherwise a cross-platform in-kernel guard
(network + destructive-`os` block + workspace `chdir`); fail-closed is opt-in via
`OMICSCLAW_AUTONOMOUS_REQUIRE_SANDBOX=1`. Model capability is gated behaviourally
(pre-flight probe → run / refuse; in-loop `MODEL_INCAPABLE` warm-up backstop).
Implemented in `omicsclaw/autonomous/` (`protocol`, `budget`, `capability`,
`kernel_envelope`, `kernel_session`, `skill_facade`, `mini_agent`, `replay`,
`mini_agent_runner`); ~60 tests in `tests/test_mini_agent_*.py`. The
`off`/`assist`/`auto` analysis-router mode selector was then removed **entirely**
(2026-06-22 control-plane consolidation): backend `OMICSCLAW_ANALYSIS_ROUTER_MODE`
+ `analysis_router_mode` plumbing, the `auto` deterministic-dispatch path, and the
desktop app toggle/setting are all gone — routing is now unconditionally
assist-style with no mode knob and no ops kill-switch. This revision
**reverses §7**'s "keep the legacy engine as the flag-off default", **narrows §4**'s
fail-closed to the tiered model, and **changes §8**'s incapable action from
degrade-to-one-shot to refuse.

## Context

OmicsClaw's strongest safety property is still skill dispatch: a user request
maps to a registered `SKILL.md` methodology and a deterministic local script.
The Analysis Router already classifies analysis requests as Exact skill,
Partial skill, or No skill, and Exact routes stay on the shared skill runner.

The weak path is the fallback. Today the Autonomous Code Runner asks the LLM for
one JSON object, writes `scripts/attempt_N.py`, runs a fresh subprocess, and
allows at most two evidence-bound repairs. The implementation has no persistent
state, no curated skill-call surface, and no tactical feedback loop. Its safety
model depends on a subprocess wrapper: `_guarded_analysis_argv` prepends a
runtime guard to a generated script and runs `[python, _guarded_attempt_N.py]`.
That guard is process-local and does not transfer directly to an arbitrary live
kernel session.

The SpatialClaw reference demonstrates a better fallback shape for genuinely
bespoke work: a persistent Jupyter kernel, small injected tool surface, stepwise
`Purpose / Reasoning / Next Goal / Code` turns, execution feedback, and a
`ReturnAnswer` sentinel. The useful idea is not "let the model author arbitrary
workflows"; it is "make the only useful actions curated tools, and let small
glue code live between them."

There are two hard mismatches with OmicsClaw that the design must not hide:

1. OmicsClaw skills are CLI scripts. The registry stores script paths and
   `run_skill()` builds argv and spawns a subprocess. A representative skill
   (`skills/spatial/spatial-preprocess/spatial_preprocess.py`) exposes
   `argparse` + `main()` and writes `processed.h5ad`; it does not expose a
   stable `run(adata, **params)` API. A true in-process
   `oc.spatial_preprocess(adata)` facade would require a major migration across
   the catalog.
2. AST filtering is not a security boundary. SpatialClaw's `SecuritySandbox`
   is useful as a preflight lint, but name/regex checks are bypassable through
   Python introspection, dynamic attributes, mutated builtins, pickle-like
   mechanisms, and pre-existing kernel state. For "genetic data never leaves
   this machine", generated code needs a process/OS boundary as well.

This ADR accepts the mini-agent direction, but narrows v1 so it fits the live
OmicsClaw architecture.

## Decision

### 1. Scope stays fallback-only

The mini-agent is used only after the Analysis Router chooses a Partial skill
or No skill route. Exact skill matches continue to run through deterministic
skill dispatch and assisted parameterization. This is not a replacement for the
skill system.

Partial skill routes remain skill-first: run the nearest built-in skill first,
then hand the upstream artifacts to the mini-agent for the uncovered
post-processing, figure, table, or residual analysis step. If the built-in
skill fails and no usable artifact exists, do not silently fall through to the
mini-agent.

### 2. The runner becomes a tactical mini-agent

The Autonomous Code Runner becomes the **Autonomous Code Mini-Agent**: a bounded
inspect -> plan -> code -> execute -> feedback -> self-check loop inside the
fallback execution component. This intentionally reverses ADR 0014's "no brain"
runner decision.

The reversal is limited. The mini-agent owns tactical execution, not final
judgment. The outer loop still decides when fallback execution is allowed, what
question must be asked before consequential ambiguity, and whether the returned
artifacts satisfy the user's intent.

### 3. v1 skill handles are subprocess-backed, not live in-process APIs

The kernel receives a curated `oc` / `skills` facade, but v1 handles call the
existing shared skill runner rather than importing 95 skills in-process:

```python
adata = oc.spatial_preprocess(adata, method="scanpy")
```

is v1 shorthand for:

1. materialize the current `adata` handle to the autonomous run workspace;
2. call `run_skill()` / `arun_skill()` through the normal registry, argv
   builder, flag allowlist, subprocess driver, output finalizer, and
   `SKILL.md` contract;
3. record the skill call in an ordered nested skill manifest;
4. reload the declared primary artifact back into the kernel when the skill
   declares one (`processed.h5ad` / `saves_h5ad` or an explicit output mapping);
5. return a typed handle containing the loaded object plus the skill output
   directory and manifest metadata.

Generated code may call only this facade for skill execution. It may not call
`subprocess`, `os.system`, package installers, network clients, or arbitrary
skill script paths itself. The facade is trusted injected code and is outside
the AST blocklist; the LLM-authored cell is not.

A later skill-API migration may add true in-process handles for selected skills,
but that is not required for v1 and must be proven per skill. Refactoring the
whole catalog is explicitly out of this ADR.

### 4. Persistent kernel, but with a hard safety envelope

Use a persistent Jupyter kernel per autonomous run so variables, loaded data,
figures, and intermediate tables survive across mini-agent steps.

The kernel process must be launched through an **Autonomous Kernel Safety
Envelope**:

- no network by default;
- host write access limited to the autonomous run workspace (the deliverable);
  the kernel's ephemeral scratch `$HOME` (tool caches — see **Kernel scratch
  home** in CONTEXT.md) lives in the sandbox `/tmp` tmpfs or a throwaway temp
  dir, never in the run workspace nor on the host repo / inputs / system;
- read access limited to explicit input paths, upstream artifact references,
  and the run workspace;
- secrets and provider API keys stripped from the kernel environment;
- `PYTHONNOUSERSITE=1` unless the user explicitly chooses otherwise;
- CPU / memory / wall-clock limits where the platform supports them;
- cancellation and timeout interrupt the kernel and then restart it if it does
  not become idle.

> **Tiered isolation (2026-06-22 single-engine consolidation).** The guarantees
> above describe the **OS envelope (bubblewrap) tier**. When bubblewrap is
> unavailable and strict mode is off (`OMICSCLAW_AUTONOMOUS_REQUIRE_SANDBOX=0`,
> the cross-platform default), the mini-agent falls back to an **in-kernel
> guard** that enforces only a subset — no network and no destructive `os`
> calls, with the working directory pinned to the workspace — and does **not**
> hard-confine writes (`open()` is not intercepted, by design) or restrict
> reads. Strict mode (`=1`) makes a missing bubblewrap fail-closed instead.

AST validation remains a lint gate for generated cells, not the security
boundary — the kernel process envelope (or, in the non-bwrap tier, the in-kernel
guard above) is. Fail-closed behaviour follows the tiered-isolation note above,
not an unconditional rule.

> **Deliverable vs. machinery (2026-06-26 clarification).** "Write access limited
> to the run workspace" governs *deliverable* writes. Two write-scopes were
> always conflated: the deliverable (the navigable artifacts a user reads) and
> ephemeral kernel scratch (matplotlib / numba / ipython caches). Pinning the
> kernel `$HOME` to the run workspace pushed scratch into the deliverable,
> cluttering it with empty `.cache` / `.config` / `.ipython` trees. The envelope
> now routes scratch to a **Kernel scratch home** (sandbox tmpfs, or a throwaway
> temp dir without a sandbox) — no new host write-surface, since the sandbox
> tmpfs was always writable. Likewise the **Replay artifact**'s validation re-run
> now executes in throwaway scratch, not a `replay/` subdirectory of the
> deliverable; only its pass/fail status is surfaced. The run workspace therefore
> contains only deliverable artifacts.

### 5. Replay is a validation gate, not a best-effort artifact

Persistent kernels make hidden state easy: a cell can depend on a previous
failed cell, on mutated globals, or on execution order that a concatenated file
does not reproduce. Therefore the mini-agent does not accept `ReturnAnswer`
until replay has passed.

On every successful step, the runner records:

- a cell id, source code, stdout/stderr summary, produced variable diff, and
  artifact diff;
- every `oc` facade skill call with skill name, params, input artifact,
  output directory, primary returned artifact, status, and manifest path.

When the mini-agent calls `ReturnAnswer`, OmicsClaw emits:

- consolidated `analysis.py` containing only accepted cells in execution order;
- `skill_calls.jsonl` / ordered skill-call manifest (when skills were called);
- `manifest.json`, `completion_report.json`, `result_summary.md` (with the
  OmicsClaw disclaimer), and any `figures/` / `tables/` the run actually produced.

Only `result_summary.md` is required; the optional dirs are created lazily by
their writers, so a run never ships empty placeholder directories. (The
one-shot-era `scripts/`/`logs/`/`artifacts/` are gone — the persistent-kernel
engine never wrote them.)

Before the outer loop receives a successful result, the runner replays
`analysis.py` in a fresh isolated kernel/process against the recorded inputs and
skill-call facade. Replay failure makes the autonomous run fail, even if the
live kernel reached `ReturnAnswer`.

### 6. Preflight seams stay outer-owned

The existing ADR 0006 dispatch/event stream and
`pending_preflight_requests` machinery were built for the outer chat loop. They
store resumable tool arguments and re-enter a top-level tool call; they do not
lease or resume a nested live Jupyter kernel.

Therefore v1 supports preflight before the mini-agent starts: the outer loop
runs deterministic `inspect_data`, asks one focused question on consequential
ambiguity, and then starts the mini-agent with goal + schema + approved plan.

Mid-kernel "pause and resume the same live kernel after a user reply" is
deferred. If the mini-agent discovers consequential ambiguity after execution
has begun, it returns a structured `needs_user_input` outcome to the outer loop,
shuts down or snapshots the run as incomplete, and the resumed turn starts a
new mini-agent run using the prior artifacts as references. A future ADR may
add a kernel lease / resume protocol to the dispatch stream.

### 7. Replace the one-shot runner, but keep a cheap path inside one runner

Do not keep two independent generated-code runners. The one-shot
`code_loop.py` path is replaced by the mini-agent engine.

To avoid paying the full loop cost for trivial residual work, the mini-agent has
a cheap internal mode: when routing confidence is high, no skill call is needed,
and the requested residual is small, the same engine may run a one-cell plan
with `max_steps=1` or `2`. This preserves one provenance shape, one safety
envelope, one replay gate, and one code path, while avoiding a second runner.

### 8. Model and budget gates are part of the contract

The mini-agent is more demanding than the current one-shot generator. It must
declare and enforce a budget envelope:

- default v1 `max_steps=8`, upper bound 15 only behind benchmarking;
- `max_consecutive_failures=3`;
- raw generated cell timeout default 120 seconds;
- facade skill-call timeout may be longer, defaulting to the skill runner's
  analysis timeout policy rather than the raw-cell timeout;
- maximum nested skill calls per run;
- token budget / cost budget per autonomous run;
- bounded feedback summaries: schema and prior outputs are summarized or diffed
  after the first step rather than pasted in full each turn.

Provider/model capability is also explicit. If the active local or remote model
cannot reliably follow the markdown/code contract and recover from execution
feedback, the mini-agent **refuses** the route with a clear diagnostic (revised
2026-06-22: with a single engine there is nothing to degrade to, so the incapable
action is refuse, not degrade — see the *Model capability threshold* open
question and `capability.py`). The mini-agent itself has no on/off flag and runs
automatically on the fallback route. (Revised 2026-06-22: the analysis router's
`off`/`assist`/`auto` mode selector and `OMICSCLAW_ANALYSIS_ROUTER_MODE` were
removed in the control-plane consolidation — routing is now always assist-style;
see the Status note.) Reaching the fallback route does not imply that every model
can drive the mini-agent.

## Guardrails

- Skill dispatch remains primary. The mini-agent cannot reclassify an Exact
  skill route as generated code.
- The `oc` / `skills` facade is an allowlist generated from the skill registry;
  it uses skill metadata and `allowed_extra_flags`, not arbitrary script paths.
- v1 raw generated code is Python-only unless an equivalent R kernel safety
  envelope and replay gate are designed. R-based skills still run normally via
  the facade subprocess path.
- AST checks block unsafe generated cell shapes before execution, but all
  safety claims depend on the kernel process envelope.
- The kernel never receives provider API keys or unrelated environment secrets.
- Network is blocked in generated-code execution by default. Network-backed
  downloads and package installation remain `system_mutation` and are not
  available to the local autonomous runtime.
- All writes go under the autonomous run workspace; upstream skill outputs and
  inputs are referenced by manifest entry by default, not copied.
- Replay in a fresh isolated process is required before a successful
  `ReturnAnswer` is accepted.
- The outer loop performs final result validation against the user intent,
  schema-grounded plan, artifacts, and replay report before presenting an
  interpretation.
- Every report separates computed results from interpretive claims and keeps
  the OmicsClaw disclaimer.

## Considered alternatives

### Keep ADR 0014 unchanged: outer brain, runner sandbox only

Rejected. It avoids nested-agent complexity, but it leaves the fallback path
too weak for the use cases that motivated ADR 0013: multi-step bespoke analysis,
artifact inspection, and residual methods between existing skills. The current
one-shot runner has no persistent state and no curated skill surface.

### Port SpatialClaw literally

Rejected. SpatialClaw assumes code is the only action interface and its curated
tools are native in-process objects. OmicsClaw's primary interface is the skill
registry, and those skills are subprocess CLI programs. Literal porting would
either bypass the skill runner or require refactoring the whole catalog before
v1.

### Refactor all skills to importable `run(adata, **params)` APIs first

Rejected for v1. It is architecturally attractive for selected Python skills,
but it turns a fallback-runtime improvement into a 95-skill migration. The
subprocess-backed facade gives the mini-agent the safety benefit of "skills as
tools" immediately, with known I/O cost.

### Use AST sandboxing as the only boundary

Rejected. Static checks are too easy to bypass in Python, especially in a
persistent kernel. They remain useful for clear diagnostics and early rejection,
but the hard boundary must be the kernel process envelope plus restricted
filesystem/network/resource access.

### Maintain the old one-shot runner beside the mini-agent

Rejected as a product/runtime shape. Two generated-code runners would split
policy, provenance, replay, tests, and future bug fixes. The accepted compromise
is a cheap one-cell mode inside the mini-agent engine.

### Treat mini-agent code as a Workflow

Rejected. A Workflow in `omicsclaw/runtime/CONTEXT.md` is a pre-written,
registered orchestration over deterministic skill subprocesses. The mini-agent
writes one-off analysis glue for one fallback run and emits replay artifacts;
it does not author reusable `fan_out` / `chain` topology, register a workflow,
or become a consensus/pipeline client.

## Consequences

### Positive

- The No skill / Partial fallback becomes stateful enough to inspect artifacts,
  compose multiple vetted skills, repair from concrete feedback, and produce
  richer figures/tables without replacing deterministic skill dispatch.
- "Skills as tools" is achievable in v1 without refactoring the entire catalog:
  the facade shells out through the existing runner and reloads artifacts.
- Replay validation turns a persistent-kernel trace into a reproducibility
  contract rather than a notebook transcript that may or may not rerun.
- The ADR 0014 outer seams survive: execution is handed off, but acceptance and
  consequential ambiguity remain outside the mini-agent.

### Negative / costs

- Subprocess-backed skill handles are slower than true in-process handles:
  materialize object -> skill subprocess -> reload output. This is accepted for
  v1 because it preserves existing skill contracts.
- The hard kernel safety envelope is new infrastructure and must be implemented
  per platform. Linux can be hardened more cheaply than macOS/Windows.
- Replay doubles some work and may rerun expensive skill calls unless the facade
  supports deterministic reuse of recorded nested outputs. The implementation
  must decide when reuse is valid and record that decision.
- v1 likely narrows raw generated language support to Python. The old runner's
  generated R script path should not be carried forward without an equivalent
  R isolation/replay design.
- Local weak models may fail this loop more often than they fail simple
  tool-calling. Capability diagnostics become user-visible product behavior.

### Implementation surface

- Slim `omicsclaw/autonomous/code_loop.py` to a capability-gate dispatcher and
  add `mini_agent_runner.py` as the single engine — not a parallel second runner.
- Add an autonomous kernel manager and kernel safety envelope under
  `omicsclaw/autonomous/`.
- Add the registry-backed `oc` / `skills` facade over `run_skill()` /
  `arun_skill()`, nested output directories, primary-artifact reloading, and
  ordered skill-call manifests.
- Add markdown response validation for `Purpose / Reasoning / Next Goal /
  Code`, execution feedback summarization, variable/artifact diffs, and
  `ReturnAnswer`.
- Add replay emission and fresh-process replay validation before success.
- Extend autonomous manifest/completion metadata to include step traces,
  replay status, model/budget usage, and nested skill calls.
- Keep router integration behind the existing analysis-router mode until
  benchmarks establish default-on behavior.

## Open questions

- **Kernel hardening portability.** *Resolved (2026-06-22).* The default Linux
  primitive is `bubblewrap`; platforms without it (macOS/Windows, or any host
  lacking bwrap) use the cross-platform **in-kernel guard** tier rather than
  failing closed, unless `OMICSCLAW_AUTONOMOUS_REQUIRE_SANDBOX=1` is set (then a
  missing bwrap fails closed). See §4 *Tiered isolation*.
- **Primary artifact mapping.** `saves_h5ad` is not enough for every skill.
  Which metadata field declares the artifact that a facade handle reloads?
- **Nested skill-call replay.** Should replay rerun nested skills by default,
  or reuse recorded outputs when their command, input checksum, and skill
  version match? The answer affects both cost and reproducibility semantics.
- **Mid-loop user input.** A true pause/resume of a live nested kernel requires
  a kernel lease, durable run state, and new dispatch events. v1 restarts from
  artifacts instead.
- **Model capability threshold.** *Partly resolved.* Capability is now gated
  behaviourally rather than heuristically (`capability.py`): a cheap pre-flight
  probe (1–2 completions testing whether the model emits a valid
  `Purpose/Reasoning/Next Goal/Code` turn) decides **run / refuse**
  (`OMICSCLAW_MINI_AGENT_PROBE`; an incapable model is *refused* with a clear
  diagnostic — the 2026-06-22 single-engine consolidation removed the old
  `degrade` action and the `OMICSCLAW_MINI_AGENT_ON_INCAPABLE` switch, since
  there is no legacy engine to fall back to), and an in-loop `WARMUP_STEPS`
  backstop aborts with `MODEL_INCAPABLE` if the model never produces a usable
  turn in the opening steps. Still open: a curated benchmark suite / explicit
  provider-capability tags to pre-empt the probe for known models.
- **Generated R.** Raw R code should remain out of v1 unless an R kernel/process
  envelope and replay story match the Python path.
