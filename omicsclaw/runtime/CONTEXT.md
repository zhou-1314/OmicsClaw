# Runtime — Orchestration Language

The vocabulary OmicsClaw uses for code-driven orchestration: workflows,
their runtime, and how they relate to skills and the agent loop. This
context exists because OmicsClaw is a standalone Python agent (its own
`llm_tool_loop`, OpenAI-compatible) and must build its own orchestration
layer — it cannot borrow Claude Code's product-level Workflow tool.

## Language

**Workflow**:
A pre-written, code-driven, in-process orchestration over deterministic
skill subprocesses, registered and invoked by the agent loop as a single
tool — the LLM selects and parameterizes it, but never authors its control
flow.
_Avoid_: "dynamic workflow" (see below — the excluded LLM-authored sense),
"agent team", "pipeline" (reserved, see below).

**Dynamic workflow** _(excluded capability)_:
The Anthropic / Claude-Code sense where the LLM **writes** the orchestration
script at runtime and a sandbox executes it. OmicsClaw deliberately does
**not** do this (decided 2026-05-30): on a "genetic data never leaves this
machine / never hallucinate parameters" tool, letting the LLM author
executable orchestration is the largest avoidable safety surface, and
OmicsClaw's domain is regular (knowable-in-advance) pipelines, not the
irregular one-off tasks dynamic authorship is for.
_Avoid_: using "workflow" to imply this — they are different.

**Workflow runtime** _(L1)_:
The reusable in-process execution layer that workflows are built on. The
"thin waist": it owns **execution topology + lifecycle only**. Today that is
exactly what `fan_out` implements — parallel fan-out, a concurrency semaphore,
cancellation, per-step timeout, an opt-in caller-supplied survivor minimum,
and step-result types — and nothing domain-specific (decided 2026-05-30). A
`chain` primitive and journal/resume are **planned, not yet implemented**.
Reductions like consensus scoring / top-K selection live one layer up, never
here — and so does every artifact: `plan.json` and the consensus tables are
written by the L2 driver, not the runtime — so the runtime stays usable by any
client (consensus, pipeline, …), not just fan-out-then-score ones. It is
`team.py`'s `run_team` de-consensus'd and lifted to a neutral package
`runtime/workflow/`. v1 is born with **one** primitive, `fan_out`, and **one**
client, consensus (decided 2026-05-30, scope "(i)"): `chain` and a second
client (`pipeline_runner` re-platformed) grow together in a later PR, so no
zero-client primitive is written speculatively.
_Avoid_: "workflow engine" (implies generative), "orchestrator" (already the
routing skill).

**Skill**:
A single deterministic analysis step run as a subprocess
(`<skill>.py --input … --output …`), with a fixed-schema artifact contract.
The atom a workflow fans out or chains.
_Avoid_: "tool" (overloaded with the LLM function-calling sense), "agent".

**Workflow template** _(L2.5)_:
One consensus math/synthesis *shape* expressed as a fixed driver function —
`categorical` (`run_typed_consensus`, v1), `continuous` (rank-gauge per-cell
*vector* consensus — one pseudotime/score vector per member, direction +
monotone gauge resolved before aggregation; branching trajectory *topology* is
out of scope — ADR 0031), and reserved `rank` (RRA) / `interval` (merge). The
`TEMPLATES` registry is open but **controlled**:
adding a template means adding a new "verified" math guarantee, so it
requires its own ADR (decided 2026-05-30). Everyday flavours never add one.
Each template carries an explicit **provenance** field
(`typed`/verified vs `exploratory`) — categorical/rank/interval are `typed`,
**narrative** is `exploratory`. Banner + graph-memory namespace key off
`template.provenance`, so the old `dispatch.py` typed-vs-narrative router
folds into the registry (decided 2026-05-30, amends ADR 0010 — the
verified/exploratory boundary is now two explicit fields, `source.template`
+ `template.provenance`, rather than one allowlist `set`).
_Avoid_: "operator" (that's the kmode/weighted/lca switch *inside* the
categorical template, a finer grain).

**Source entry** _(L3, the declarative contract)_:
The artifact a contributor authors to add a consensus flavour — a
`ConsensusSource` binding a **Workflow template** + the fanned-out
`member_skill` + a `MemberArtifactReader` + a `MemberPlanner` + metadata
(domain, title, param-hints). It is pure data plus one planner; it touches
no orchestration code. This is the **unit of extension** (B0).
_Avoid_: "consensus skill" (the routable name is a thin shim over a source
entry, not the entry itself).

**MemberPlanner**:
The pluggable strategy that turns CLI args into the `list[ConsensusMember]`
to fan out — two concrete planners, `ChairLLMPlanner` (evaluation-chair LLM,
`--all` from param_hints) and `SweepPlanner` (parameter cartesian product),
sharing one `_explicit_members` helper for the `--members` branch. The one
piece of genuine per-flavour logic; everything else in a source entry is data.
_Avoid_: "selector" (that's BC selection, a later step), "router".

**Pipeline** _(existing client)_:
A YAML-declared baton-pass chain of skills (A→B→C, each consuming the
prior's artifact), executed by `skill/execution/pipeline_runner.run_pipeline`.
One workflow shape, not the general term.
_Avoid_: using "pipeline" as a synonym for "workflow".

## Relationships

- A **Workflow** fans out / chains many **Skill** runs; it holds the loop,
  branching, and intermediate results, so the agent loop's context holds
  only the final answer (the one property OmicsClaw keeps from the
  dynamic-workflows idea).
- **Consensus** (`runtime/consensus/driver.run_typed_consensus`) and
  **Pipeline** (`pipeline_runner.run_pipeline`) are the two concrete
  **Workflow** clients today; the **Workflow runtime** is the abstraction
  they will share.
- A **Workflow** is invoked by the **agent loop** as an *ordinary skill name*
  through the one generic executor (`execute_omicsclaw(skill=…)`,
  `agent_executors.py:211`), never a distinct tool — to the LLM a workflow is
  an atomic skill (decided 2026-05-30, "(A)"); its internal multi-step
  structure is invisible. Multi-step progress for the desktop 待办 UI is a
  decoupled presentation concern carried by `progress_fn` callbacks, not a
  tool-surface concern. The LLM is the *selector*; the workflow code is the
  *orchestrator*.
- Three layers (decided 2026-05-30): **L1 Workflow runtime** (topology only)
  ← **L2 consensus-shared** (`runtime/consensus/`: scoring, BC selection,
  operators, alignment, spatial_metrics — shared by every consensus flavour)
  ← **L3 workflow clients** (one declarative contract each). `driver.py` is
  the consensus workflow: it wires L2 operators with L1 `fan_out`.
- A consensus **flavour** = one **Workflow template** × one **Source entry**.
  Adding a flavour writes a **Source entry** (data + planner) when an existing
  template fits; a new **Workflow template** is written only when the
  consensus *math* is genuinely new (and gets an ADR). The routable skill
  name (`consensus-domains`) survives as a 3-line shim over its source entry,
  so the capability resolver still scores flavours independently.
- **Narrative** (B-path: LLM extract + synthesize) is just the `narrative`
  **Workflow template** with `provenance=exploratory`; it lives in the same
  registry as the typed templates, not a separate router.
- **consensus-interpret** is *not* a workflow — it is a downstream **Skill**
  that consumes a typed run's artifacts and annotates them (DE + marker/LLM,
  `analysis://interpreted/<id>`, ADR 0012). The `consensus → interpret` chain
  is a **Pipeline**; the redesign leaves the skill itself untouched (its bulk
  is real domain logic, not wrapper duplication).

## Example dialogue

> **Dev:** "Is consensus a workflow or a skill?"
> **Architect:** "Consensus is a **Workflow** — it fans out many
> spatial-domains **Skill** runs and merges them. `spatial-domains` itself
> is a **Skill**. The LLM picks the consensus workflow and fills its params;
> it does not write the fan-out — that's a **Dynamic workflow**, which we
> explicitly don't build."

## Flagged ambiguities

- "workflow" was colliding with the Claude-Code **Dynamic workflow** (LLM
  authors the script). Resolved: OmicsClaw "Workflow" = pre-written library
  invoked by the loop; the generative sense is explicitly out of scope.
- "pipeline" was at risk of becoming a synonym for "workflow". Resolved:
  Pipeline is *one* workflow shape (YAML baton-pass), not the general term.
- "continuous" vs the reserved "rank" template — both touch ranks, at risk of
  being merged. Resolved (ADR 0031): `continuous` aggregates a per-cell scalar
  *field* (every member has a value at every cell, rank-normalised — pseudotime/
  scores); `rank` (RRA) aggregates ranked *lists* whose items differ per list
  (DE). Distinct templates, distinct math — do not merge.
