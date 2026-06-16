# Generalize consensus orchestration into a thin `runtime/workflow/` topology layer + a two-axis (template × source) registry, collapse the per-flavour skill wrappers to shims, and fold typed/narrative dispatch into the registry — amends ADR 0010

## Status

Accepted (2026-05-30). Amends ADR 0010 (the "single-file verified/exploratory
audit surface" claim and the `TypedConsensusSource` registry shape).

## Context

A grilling session on 2026-05-30 (`/grill-with-docs`, ten branch-points)
examined a user thesis: *"expressing consensus through the **skill form** is
bad and hard to extend; Anthropic's new Dynamic Workflows
(https://x.com/riba2534/status/2060102236676792711 — orchestration written as
an executable script, so the orchestrator's context holds only the final
answer) fit consensus better."*

Reading the actual code reframed the premise. Three findings:

**Finding 1 — the consensus runtime is *already* a dynamic-workflow.**
`run_typed_consensus` (`omicsclaw/runtime/consensus/driver.py:172-294`) is a
fixed, linear, code-driven pipeline: `plan.json` audit → `run_team` fan-out →
`_gather_labels` → cross-method NMI → composite scoring → BC selection →
operator switch (`kmode`/`weighted`/`lca`) → artifact writes. Intermediates
live in local variables; only the `TypedConsensusRun` dataclass surfaces.
This is a near-exact description of the dynamic-workflows pattern — *already
realized*. The fan-out (`team.run_team`, `team.py:177`) is a generic
`asyncio.gather` over `ConsensusMember`s with an injectable `runner`, with no
consensus-specific logic beyond the "≥2 survivors" rule (which plan 0025 later
relocated to L2, leaving the lifted L1 `fan_out` fully domain-neutral).

**Finding 2 — the friction is boilerplate in the thin wrappers, not in how
consensus is computed.** `consensus_domains.py` (247 lines) and
`sc_consensus_clustering.py` (228 lines) are ~85-90% identical CLI boilerplate:
`_make_bc_selector` is **functionally identical** between them; `_main_async`
(build `plan_audit` → call `run_typed_consensus` → three `except` blocks →
`format_typed_report` → write `report.md`) is ~95% identical. The genuine
per-flavour differences are **two**: (1) member planning — `consensus-domains`
uses the evaluation-chair LLM (`plan.propose_members`), `sc-consensus-clustering`
uses a deterministic resolution sweep (`_members_from_sweep`); and (2)
`consensus-domains` exposes an interactive plan-confirmation gate
(`_maybe_confirm_plan`, `consensus_domains.py:166,187`, behind `--confirm-plan`)
that `sc-consensus-clustering` parses but silently ignores — a latent bug the
collapse fixes.

**Finding 3 — OmicsClaw is a standalone agent and cannot borrow the product
tool.** The main loop `llm_tool_loop` (`omicsclaw/runtime/agent/loop.py:1037`)
drives an **OpenAI-compatible** provider (`query_engine.py:1055`,
`chat.completions.create`); skills are exposed through one generic executor
(`agent_executors.py:211` `execute_omicsclaw`, dispatched by a `skill` arg,
`:2042`). The dynamic-workflows `Workflow` tool is a Claude-Code *product*
feature; OmicsClaw cannot import it. "A workflow capability like the post" =
**build a Python one**.

### Why this is not the speculative infrastructure ADR 0010/0006 rejected

ADR 0010 §"Why not a Gateway→Redis→Worker pipeline" and ADR 0006 §3 rejected
"speculative infrastructure no caller required" twice, on the single-machine /
"genetic data never leaves this machine" constraint. The user initially wanted
a *general* workflow capability (a real risk of repeating that rejection). The
grilling separated two things the post bundles:

1. **A workflow runtime / topology primitives** — reusable `fan_out` execution
   that *pre-written* workflows are built on.
2. **Dynamic authorship** — the LLM *writes* the orchestration code at runtime,
   executed by a sandbox.

On a tool whose safety rules forbid hallucinated parameters and mandate "never
guess — route to skills", (2) is the largest avoidable safety surface (the post
itself strips FS/shell from such scripts). Asked to name one OmicsClaw task that
needs the LLM to author *new control flow* at runtime — uncoverable by a
pre-written, LLM-parameterized workflow — the user could name none. So (2) is
rejected outright, and (1) is built **minimally** (one primitive, one client).
This is what keeps the decision on the right side of the prior rejections.

## Decision

Ten resolutions, organized by layer. The headline OmicsClaw contribution from
ADR 0010 (LLM evaluation-chair + verified/exploratory provenance split) is
preserved unchanged; this ADR only restructures *where orchestration lives* and
*what a contributor authors*.

### Shape: library, not generative; three layers

- **Library, not generative.** OmicsClaw builds a workflow *runtime + registry*
  of pre-written workflows the LLM selects and parameterizes — never a system
  where the LLM authors orchestration code. (Rejected alternative (a).)
- **Three layers**, with the "thin waist" cut at pure topology:

  | Layer | Owns | Shared by |
  |---|---|---|
  | **L1 Workflow runtime** (`runtime/workflow/`, new) | execution topology + lifecycle only — `fan_out`, cancellation, timeout, step-result types (`chain` + journal/resume planned, not yet implemented) | any client |
  | **L2 consensus-shared** (`runtime/consensus/`, exists) | operators (kmode/weighted/lca), composite scoring, BC selection, alignment, `spatial_metrics` | every consensus flavour |
  | **L3 workflow clients** | one declarative contract each | one flavour each |

  Scoring / BC-selection stay in **L2**, never L1 — `pipeline_runner` does not
  score, so promoting them would re-thicken the waist. (Rejected alt (f).)

- **L1 scope is minimal (YAGNI).** L1 is born as `runtime/workflow/fan_out.py`
  — `team.run_team` de-consensus'd (`member` → `step`) and lifted out of
  `runtime/consensus/`. It ships with **one** primitive (`fan_out`) and **one**
  client (consensus). `chain` and a second client (`pipeline_runner`
  re-platformed onto L1) grow together in a *later* PR, so no zero-client
  primitive is written speculatively. (Rejected alt (b): build a general
  `parallel`/`pipeline` engine now.)

### Unit of extension: a declarative two-axis contract

- **The unit a contributor authors is a declarative `ConsensusSource`**, not a
  hand-written orchestration script. (Rejected alt (d): a workflow-script per
  flavour re-introduces the very duplication being removed; consensus has a
  fixed shape — `driver.py:172-294` is one linear pipeline with a tail switch.)
  This is exactly the v1.x extension already forward-declared in
  `source_registry.py:68-70`.

- **Extension is two-axis: Workflow template × Source entry.**

  ```python
  # L2.5 — Workflow template: one consensus math/synthesis shape = one driver fn,
  #        each carrying an explicit provenance.
  @dataclass(frozen=True)
  class WorkflowTemplate:
      driver: Callable      # run_typed_consensus | run_narrative_consensus | ...
      provenance: Literal["typed", "exploratory"]

  TEMPLATES: dict[str, WorkflowTemplate] = {
      "categorical": WorkflowTemplate(provenance="typed",       driver=run_typed_consensus),  # v1
      "narrative":   WorkflowTemplate(provenance="exploratory", driver=None),                 # B-path*
      # "rank":      WorkflowTemplate(provenance="typed", driver=run_rank_consensus),  # v2 RRA  (own ADR)
      # "interval":  WorkflowTemplate(provenance="typed", driver=run_interval_consensus),  # v3 merge (own ADR)
  }
  # *narrative has no single run.py driver: the B-path executes via
  #  narrative/{extractor,synthesizer}. Binding it here would create a
  #  dispatch → templates → synthesizer → dispatch import cycle, so driver=None.

  # L3 — Source entry: the declarative artifact a contributor writes (pure data
  #      + one planner; touches no orchestration code).
  @dataclass(frozen=True)
  class ConsensusSource:
      name: str                      # "consensus-domains"  (== routable shim name)
      template: str                  # key into TEMPLATES
      member_skill: str              # "spatial-domains"    (the fanned-out skill)
      reader: MemberArtifactReader   # how to read this domain's labels + intrinsic
      planner: MemberPlanner         # the one genuine per-flavour behaviour
      domain: str                    # "spatial" | "scrna"  (chair + namespace)
      report_title: str
      param_hints_path: Path

  CONSENSUS_SOURCES: dict[str, ConsensusSource] = { ... }   # one data row per flavour
  ```

- **The `TEMPLATES` registry is open but controlled.** Adding a template means
  adding a new "verified" math guarantee, so it requires **its own ADR**.
  Everyday flavours only ever add a `ConsensusSource` row. (B4a.)

- **`MemberPlanner` unifies the two incompatible planning strategies** behind
  one protocol — two concrete planners, `ChairLLMPlanner` (evaluation chair,
  `--all` from param_hints) and `SweepPlanner` (parameter cartesian product),
  sharing one `_explicit_members` helper for the `--members` branch (the helper
  is parameterised by the member skill's method flag + whether a colon is
  required). This is the only real logic that differed between the wrappers.

### Collapse: kill the wrappers, keep the routing

- `consensus_domains.py` + `sc_consensus_clustering.py` (475 lines, ~90%
  duplicated) lose their bodies. The de-duplicated `_main_async` becomes one
  generic entry `omicsclaw/runtime/consensus/run.py` (look up
  `CONSENSUS_SOURCES[name]` → `source.planner.propose` →
  `TEMPLATES[source.template].driver(...)` → `format_typed_report(title=…)`).
  The generic entry also folds in the `_maybe_confirm_plan` gate so
  `--confirm-plan` is honoured uniformly — incidentally fixing
  `sc-consensus-clustering` silently ignoring the flag today.
- **Scope of "byte-identical".** The golden compare covers the five on-disk
  artifacts (`report.md`, `consensus_labels.tsv`, `member_scores.csv`,
  `cross_method_nmi.csv`, `plan.json`) on the success path — those are byte-for-byte
  identical. A few *console-text* strings on failure/interactive paths were
  deliberately consolidated (the confirm-plan prompt now lists `m.name`; the
  "no members" / `InsufficientSurvivors` wording uses neutral "step" language).
  Exit codes are preserved (`0/2/3/5/6/130`); a new `4` is added only for the
  narrative `driver is None` branch the two registered flavours never reach.
- Each flavour's `SKILL.md` + `parameters.yaml` **survive** (the capability
  resolver `resolve_capability` scores flavours independently by skill name +
  `parameters.yaml`); the `.py` shrinks to a **3-line shim** that binds the
  source name and forwards to `run.py`. The `run_skill` subprocess path is
  unchanged. (Rejected alt (e): collapsing to one `consensus` skill +
  `--source` sub-arg would erase routing discriminability between "consensus
  over spatial domains" and "over single-cell clustering".)
- **Net:** a new flavour = one `CONSENSUS_SOURCES` row + (often reused)
  reader/planner + one `SKILL.md`. Zero orchestration code; zero copy-paste.

### Invocation: a workflow is atomic to the LLM

- To the LLM/agent loop, a workflow is an **ordinary skill name** invoked
  through the existing `execute_omicsclaw(skill="consensus-domains")` — **not** a
  new tool category. Its internal multi-step structure is invisible. (Rejected
  alt (c): a first-class `run_workflow` tool only pays off if the LLM must
  *compose* multiple workflows in one turn; consensus needs no such thing, and
  the post itself notes workflows take no mid-run human input — i.e. they are
  atomic from outside.) Context hygiene (intermediates never enter LLM context)
  is delivered for free by the existing subprocess boundary, not by moving
  fan-out into the loop (which ADR 0010 §3.2(d) rejected).
- Multi-step progress for the desktop 待办 UI is a **decoupled presentation
  concern** carried by the existing `progress_fn` callbacks, independent of the
  tool surface.

### Provenance: fold dispatch into the registry (this is the amendment)

- `dispatch.select_consensus_mode` (`dispatch.py:32-47`) currently routes
  `typed` vs `narrative` by `skill_name in TYPED_CONSENSUS_REGISTRY`, and
  `output_banner` / `consensus_namespace` key off that mode. This logic **folds
  into the registry**: the verified/exploratory decision is now read from
  `TEMPLATES[source.template].provenance`. The `--mode narrative` force-override
  survives. Banners (`[A: Verified consensus]` / `[B: Exploratory synthesis]`)
  and the `analysis://typed|exploratory|interpreted/<id>` namespaces are kept
  byte-for-byte.

### Out of scope: `consensus-interpret` is untouched

- `consensus-interpret` is **not** a workflow — it is a downstream **Skill**
  that consumes a typed run's artifacts (`plan.json`, `consensus_labels.tsv`)
  and annotates them (inline DE + marker/LLM, `analysis://interpreted/<id>`,
  ADR 0012). Its 1804 lines are real domain logic, not wrapper duplication, so
  it stays a skill. The `consensus → interpret` chain is a **Pipeline**. (An
  optional, non-blocking hardening: have it read upstream via the
  `TypedConsensusRun` output contract instead of hardcoded filenames.)

## Amendment to ADR 0010

1. **Audit surface.** ADR 0010 made "the verified/exploratory boundary
   auditable from a **single file** (`TYPED_CONSENSUS_REGISTRY`)" a headline
   property. This ADR replaces that with **two explicit fields** —
   `source.template` (in `CONSENSUS_SOURCES`) and `template.provenance` (in
   `TEMPLATES`). The trade-off is deliberate: provenance moves from *implicit*
   set-membership to an *explicit first-class field*, strengthening
   auditability while trading single-file locality for two. The boundary
   remains statically auditable without running anything.

2. **Registry shape.** `TypedConsensusSource(reader=…)` grows into
   `ConsensusSource(name, template, member_skill, reader, planner, domain,
   report_title, param_hints_path)` — exactly the `planner` / `report_template`
   growth the `source_registry.py:68-70` docstring forward-declared. The
   registry key migrates from the fanned-out `member_skill` to the flavour
   `name` (1:1 today: `consensus-domains`↔`spatial-domains`,
   `sc-consensus-clustering`↔`sc-clustering`) — a behaviour-preserving rename
   (see §Consequences/Open).

Everything else in ADR 0010 (the evaluation-chair role, the ≥2-survivor rule,
the cancel chain, the operator semantics) and all of ADR 0011/0012 stand.

## Consequences

### Positive

- The duplication the user objected to is removed at the root: ~360 lines of
  copy-pasted CLI boilerplate become one generic entry + pure-data rows.
- A new consensus flavour is a declarative row, not new orchestration code —
  the genuine "hard to extend" complaint is resolved without a framework.
- `runtime/workflow/` gives the general workflow capability a real home and
  name, satisfying the user's ambition, while shipping exactly one primitive
  with exactly one client — defensibly not speculative.
- Provenance is now an explicit per-template field; the verified/exploratory
  boundary is harder to lose silently.
- No change to the LLM tool surface; the subprocess boundary keeps the
  dynamic-workflows context-hygiene win for free.

### Negative

- The verified/exploratory boundary is now read from two files instead of one
  (mitigated: both are static data, and provenance is explicit rather than
  implicit).
- One more layer to learn (L1/L2/L3). A contributor must know that "new math →
  template → new ADR" but "new flavour → source row → no ADR".
- `runtime/workflow/` ships with a single function — until `pipeline_runner`
  re-platforms, the "general runtime" is general only in intent. Accepted as
  the YAGNI-correct state.

### Open

- **`pipeline_runner` re-platform onto L1** (adds `chain`, validates the
  abstraction with a second structurally-different client) — a deliberate
  follow-up PR, not this one.
- **v2 `rank` (DE-RRA) and v3 `interval` (variant/SV merge) templates** — each
  is a new "verified" math guarantee and gets its own ADR per the open-but-
  controlled rule. They reuse L1 `fan_out`, the `ConsensusSource` contract, and
  `MemberPlanner` unchanged.
- **Registry-key migration** (`member_skill` → flavour `name`) must keep the
  ADR 0011 scoring harness and the `examples/consensus_benchmark/` /
  `tests/runtime/consensus/` suites green; the refactor is behaviour-preserving
  by construction (the `driver.py` body is unchanged except step 2 calling L1
  `fan_out`).

## Relationship to prior ADRs

- **ADR 0010** (consensus runtime layer): amended — see above. The typed-vs-
  narrative split and verified/exploratory namespaces are preserved; only their
  *encoding* (registry + template provenance vs a single allowlist + a dispatch
  function) changes.
- **ADR 0011 / 0012** (evaluation protocols): unaffected. `scoring.py`, the
  DLPFC hero benchmark, and `consensus-interpret`'s protocol all program against
  artifacts whose schema is unchanged.
- **ADR 0006** (`dispatch(envelope) -> AsyncIterator[Event]`): unaffected — no
  new dispatch events; fan-out stays out of `dispatch()` (still rejected per
  §3.2(d)). A workflow remains one `execute_omicsclaw` tool call.
- **ADR 0009** (cancel_event wiring): preserved — `fan_out` inherits
  `run_team`'s `cancel_event` chain verbatim on the lift to `runtime/workflow/`.
- **ADR 0005** (surfaces umbrella): unchanged — all three surfaces still reach
  consensus through the same thin-skill subprocess path.
