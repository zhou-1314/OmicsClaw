# Outer-Owned Understanding for the Autonomous Analysis Path

## Status

Accepted (2026-05-29). Amends ADR 0013 (which remains Accepted). This ADR
revises *where* intent understanding, data inspection, and result validation
live for the Autonomous Analysis Path, and narrows ADR 0013's `Evidence-bound
repair` rule. Its central "runner has no brain" decision is superseded by
[ADR 0032](0032-autonomous-code-mini-agent.md) (2026-06-21), which gives the
runner a bounded tactical mini-agent loop; ADR 0014's two outer judgment seams
(a pre-handoff preflight question on consequential ambiguity, and outer-owned
result validation) are preserved. Control-plane consolidation (2026-06-22): the
`off`/`assist`/`auto` analysis-router mode selector and `OMICSCLAW_ANALYSIS_ROUTER_MODE`
were removed; routing is now unconditionally assist-style (the two outer seams are
unaffected).

## Context

ADR 0013 introduced the Autonomous Analysis Path and the Autonomous Code
Runner, describing the Autonomous Code Loop as `plan-write-run-inspect-revise-report`
with an `Evidence-bound repair` policy over "stderr, exit code, artifact
checks, file schema, the user request, and upstream artifacts."

The shipped implementation under-delivers against that contract. Once a
request reaches the autonomous path:

- **The input file is never opened.** `goal` plus a list of path *strings* are
  the only context for a single codegen LLM call
  (`omicsclaw/autonomous/code_loop.py:124`; prompt at `:191-209`). The capable
  `inspect_data` tool (`omicsclaw/runtime/agent/agent_executors.py:1189`,
  reads `obs/var/obsm/layers/uns`, shape, platform) is not wired into this path.
- **Intent is the raw user text, verbatim** (`omicsclaw/analysis_router/dispatcher.py:226`).
- **Repair feeds back only stderr / exit code / error** (`code_loop.py:295-310`);
  nothing checks that the result answers the question.
- **The reply is mechanical** — artifact paths plus a template
  `result_summary.md` (`omicsclaw/autonomous/runner.py:236-283`); there is no
  interpretation pass.

Observed symptom: a real request (e.g. "spatial niche identification" on a
Slide-seqV2 `.h5ad`) that lands on the autonomous path produces a script
generated with no knowledge of the data and no confirmation that intent was
understood. By contrast, a reference implementation's `DecisionExecutionEngine` keeps all
understanding in its outer ReAct loop (native tool/skill catalog,
`data_inspect`, `ask_user_question`, history, adaptive re-planning) and has no
thin codegen sub-runner at all.

So the gap is not only "OmicsClaw chose a weaker design than a reference design" — the
implementation under-delivers against OmicsClaw's own documented contract.

## Decision

Understanding for the Autonomous Analysis Path is **owned by the outer agent
loop**; the Autonomous Code Runner stays a bounded sandbox executor with no
"brain."

Guiding principle: **deterministically guarantee the *inputs* to
understanding; let the LLM do the *judging*.** The harness forces data into
context; the LLM owns planning, ambiguity decisions, and result validation.

When the Analysis Router returns a **No skill match** or **Partial skill
match** AND a trusted input file is present:

1. **Data-grounded autonomous planning (deterministic).** Before any
   autonomous codegen, the harness runs `inspect_data` on the trusted input
   (for a Partial skill match, also the upstream skill artifacts) and injects
   the real schema — `obs/var/obsm/layers/uns`, shape, platform — into context.
2. **Plan (LLM).** The outer LLM must emit a schema-grounded plan. It asks the
   user exactly one focused question only on *consequential* ambiguity — a
   choice that materially changes results and cannot be safely defaulted (e.g.
   which `condition` column to compare; which of two plausible target cell
   types). Otherwise it proceeds with documented defaults and explicitly stated
   assumptions. The plan is shown but does not block.
3. **Delegate.** The outer LLM calls `autonomous_analysis_execute` with
   goal + schema + plan + paths. The tool signature is extended to carry the
   schema and plan as first-class context.
4. **Execute (runner = sandbox).** write → run → repair. Repair stays
   mechanical and bounded (initial + ≤2), but now also has the injected schema,
   so it can fix key/shape errors (e.g. a wrong `obsm` key) instead of guessing.
5. **Autonomous result validation (LLM, outer loop).** After the runner
   returns, the outer LLM judges the produced artifacts against the plan and
   intent. If they do not satisfy it, it triggers a bounded re-delegation
   rather than trusting exit code 0. Judgment-based; no rigid expected-output
   contract.
6. **Interpretation (LLM, outer loop).** On pass, the outer LLM writes the
   scientific interpretation over the artifacts, separating computed results
   from interpretive claims and citing artifacts, keeping the OmicsClaw
   disclaimer.

This **narrows `Evidence-bound repair`** (ADR 0013) to a runner-internal rule
over stderr, exit code, and the injected schema. The "user request / artifact
checks / upstream artifacts" evidence moves out of the runner into
`Autonomous result validation` in the outer loop. The Autonomous Code Loop's
mid-loop "inspect" (of run output) remains distinct from the new pre-codegen
data inspection at the outer layer.

Canonical terms added/changed in `docs/CONTEXT.md`: **Data-grounded autonomous
planning**, **Autonomous result validation**, and the narrowed **Evidence-bound
repair**.

## Considered alternatives

- **Make the Autonomous Code Runner its own mini-agent** (inspect → plan →
  codegen → intent-bound repair → interpret, all inside the loop). Rejected:
  creates two intelligent loops (outer engine + runner) that duplicate planning
  and result judgement, and contradicts ADR 0013's choice that the runner is
  independent of — not a second copy of — the chat engine. It also diverges
  from the reference implementation, whose strength is a *single* outer ReAct brain.
- **Pure prompt-resident understanding (reference-faithful).** Strengthen the
  route-context injection so the model is told to inspect + clarify + plan, but
  force nothing. Rejected as the *sole* mechanism: it relies on exactly the
  model discipline that is currently failing — the bug recurs on a "lazy" turn.
  We keep prompt guidance but back it with a deterministic inspect.
- **Minimal contract-compliance only** (inject schema into the one codegen
  call, make repair evidence-bound, add one interpretation pass; no inspect
  step, no clarification, no real planning). Rejected: closes the doc-vs-code
  gap but not the user-visible "didn't understand intent" problem.
- **Rigid expected-output contract for validation** (the plan declares expected
  artifacts; a deterministic existence/shape check gates interpretation).
  Considered and deferred in favour of judgement-based validation; recorded as a
  possible future hardening if judgement-based validation proves too lax.

## Consequences

- `omicsclaw/runtime/agent/loop.py` gains a deterministic understanding step on
  no_skill / partial routes that carry a trusted input file: run `inspect_data`,
  inject schema, require a plan before any `autonomous_analysis_execute` call.
- `autonomous_analysis_execute` (`agent_executors.py`) and `AutonomousRunRequest`
  gain schema/plan context fields; the `code_loop.py` initial and repair prompts
  include the schema.
- The outer loop gains an `Autonomous result validation` step and a bounded
  re-delegation path; interpretation becomes an explicit outer-loop
  responsibility rather than a template dump.
- `inspect_data` becomes load-bearing for the autonomous path (previously
  optional chat tooling).
- Latency / cost: one forced `inspect_data` round-trip per autonomous run, plus
  a possible validation re-delegation. Accepted as the price of reliable
  understanding.
- ADR 0013 stays in force except for the repair-evidence split and the new
  mandatory pre-codegen inspection; its sandboxing, permission tiers, approval
  reuse, output-shape parity, and bounded-repair count are unchanged.

## Open questions

- **Auto mode.** The deterministic dispatch route
  (`OMICSCLAW_ANALYSIS_ROUTER_MODE=auto`) currently builds `goal=user_text` and
  fires the runner with no outer LLM at all (`dispatcher.py:220-234`), which
  bypasses Data-grounded autonomous planning entirely. Desktop defaults to
  `assist`, so impact is limited today. Pending: route auto mode through the
  same understanding preflight, or define `auto` as an explicit
  "no-understanding, run-as-typed" mode.
- **Partial skill match.** The upstream skill runs first; the pre-codegen
  inspection should cover the upstream skill artifacts, not only the original
  input. The exact inspection surface for the partial continuation is not yet
  specified.
