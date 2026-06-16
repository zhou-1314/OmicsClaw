# Plan 0025 — Consensus Runtime: Genericity + De-engineering (Preliminary Options Plan)

> Status: **Preliminary / options-only** (user scope: "list options + candidates,
> do not presuppose conclusions; real forks stay PENDING").
> Cross-reviewed by Codex (gpt-5.5) on 2026-06-13.
> Vocabulary: [`omicsclaw/runtime/CONTEXT.md`](../../omicsclaw/runtime/CONTEXT.md).
> Prior decisions: ADR [0010](../adr/0010-consensus-runtime-layer.md) ·
> [0011](../adr/0011-consensus-evaluation-protocol.md) ·
> [0016](../adr/0016-consensus-as-workflow-runtime.md).

## Goal Description

Optimize the consensus runtime (`omicsclaw/runtime/workflow/` L1 +
`omicsclaw/runtime/consensus/` L2 + the shim skills L3) along four user-raised
axes: (1) L1 under-abstracts / lacks capability, (2) the Workflow-template unit
is too heavy, (3) governance and docs are over-weight or drifted, (4) the
implementation is over-engineered relative to its tiny population (1 topology
primitive, 1 real merge driver — `narrative` is `driver=None` — and 2 flavours).

This loop produces a **converged, code-anchored options document**, not an
implementation. It commits only a **no-regret floor** that both Claude and Codex
agree on independent of any open fork (Codex's "H+"): make L1 domain-neutral,
align drifted docs, and reconcile stale CLI/ADR contracts — while preserving the
non-negotiable verified/exploratory **provenance + banner + namespace** safety
contract and the `omicsclaw.runtime.consensus.team` back-compat surface. The
larger architectural choices (generalize vs retract L1; decompose vs defer the
template; governance scope; flag disposition; back-compat policy) are recorded as
explicit **PENDING decisions** for the user, each with its concrete impact
surface.

**Out of scope (user-excluded):** widening member types — members stay
deterministic skill subprocesses; no LLM-agent members, no heterogeneous/remote
compute. **Non-negotiable:** the verified/exploratory provenance, the
`[A: Verified consensus]` / `[B: Exploratory synthesis …]` banners, and the
`analysis://typed|exploratory|interpreted/<id>` namespaces (ADR 0010) are not a
simplification target.

## Acceptance Criteria

The `AC-*` items are the current-loop completion gate for the **no-regret floor**.
They presuppose none of the open forks; all conclusion-bearing choices live under
`## Pending User Decisions`.

- **AC-1: L1 `fan_out` is domain-neutral; the consensus survivor policy lives in L2.**
  The `≥2 survivors → raise` rule and the narrative/ADR-0010 wording are removed
  from `omicsclaw/runtime/workflow/fan_out.py`; the survivor threshold becomes a
  caller-supplied input (parameter, callback, or full relocation to the consensus
  driver). Consensus end-to-end behavior is unchanged because the equivalent
  `< 2 readable labels → InsufficientSurvivorsError` gate already exists in L2
  (`driver.py:230-234`).
  - Positive Tests (expected to PASS):
    - A non-consensus caller invoking `fan_out` with exactly 1 surviving step
      receives a `FanOutResult` (no exception) when it does not opt into a
      survivor minimum.
    - The consensus path still raises `InsufficientSurvivorsError` when fewer than
      2 members yield readable labels (rule enforced by L2).
    - A demo `consensus-domains` run still produces the same survivor/error
      semantics as before (golden behavior).
  - Negative Tests (expected to FAIL):
    - `grep -E "narrative|ADR 0010|≥2 survivor" omicsclaw/runtime/workflow/fan_out.py`
      returns any match.
    - `fan_out` raises `InsufficientSurvivorsError` from an L1 hard-coded constant
      for a caller that did not request a survivor minimum.

- **AC-2: Doc-truth — `runtime/CONTEXT.md` and the ADRs describe L1 as it actually is.**
  The claim that L1 owns `journal/resume` (`CONTEXT.md:29-40`) is removed or
  explicitly marked "planned, not implemented"; the "general runtime" language is
  reconciled with the single-primitive reality; the `plan.json`-written-by-L2
  fact (`driver.py:203-213`) is reflected.
  - Positive Tests:
    - The L1 section of `runtime/CONTEXT.md` enumerates only capabilities present
      in `fan_out.py` (fan-out, cancellation, timeout, semaphore, step-result
      types); any unimplemented item is labeled "planned".
    - A doc-fact check finds no assertion that journal/resume is a *current* L1
      responsibility.
  - Negative Tests:
    - `runtime/CONTEXT.md` still asserts journal/resume as an implemented L1
      responsibility.
    - The docs still describe `plan.json` as an L1-owned artifact.

- **AC-3: The `--n-clusters` / `--llm-judge` doc↔code↔metadata contract is consistent and truthful.**
  ADR 0010 documents `--n-clusters` as an override (`docs/adr/0010-…:193-203`) but
  `run.py:56-59` accepts and ignores it. This loop makes the three surfaces agree
  on a single truthful status (minimally: "accepted, reserved, not consumed"),
  including any skill `parameters.yaml` / allowed-flag metadata. The *eventual*
  disposition (implement / deprecate / remove) is deferred to DEC-5.
  - Positive Tests:
    - ADR 0010, `run.py --help` text, and the shim skills' flag metadata describe
      `--n-clusters` / `--llm-judge` identically (all "reserved / not consumed",
      pending DEC-5).
    - A test asserting `--n-clusters` is currently honored is expected to fail,
      and the docs match that reality.
  - Negative Tests:
    - Any surface (ADR, CLI help, skill metadata) still claims `--n-clusters`
      changes behavior.

- **AC-4: Safety and back-compat invariants are preserved across every floor change.**
  - Positive Tests:
    - `tests/runtime/consensus/test_templates.py` and `test_team_runtime.py`
      (provenance routing, banner, namespace lineage) pass unchanged.
    - `from omicsclaw.runtime.consensus.team import run_team, TeamRunResult, _compute_max_parallel`
      still imports successfully.
    - A demo typed run's five on-disk artifacts (`report.md`,
      `consensus_labels.tsv`, `member_scores.csv`, `cross_method_nmi.csv`,
      `plan.json`) are byte-identical to a pre-change run (ADR 0016 golden compare).
  - Negative Tests:
    - Any change to a banner string, an `analysis://` namespace URI, or a
      `templates.provenance` value.
    - Removal of a `team.py` re-export that an existing test/import depends on.

## Path Boundaries

### Upper Bound (Maximum Acceptable Scope)
The full no-regret floor: AC-1 through AC-4 implemented with tests; L1 left
genuinely domain-neutral and *ready for* (but not committed to) FUT-1
generalization; `runtime/CONTEXT.md` + ADR drift fully corrected; the stale flag
contract reconciled across docs, CLI, and skill metadata. No speculative
`chain`/DAG primitive, no `pipeline_runner` migration, no template decomposition.

### Lower Bound (Minimum Acceptable Scope)
The `≥2-survivor` threshold and domain wording are removed from L1 `fan_out`
(AC-1) and the journal/resume false claim is removed from `CONTEXT.md` (AC-2),
with consensus behavior and the provenance/banner/namespace + `team.py` import
tests green (AC-4). AC-3 may be satisfied doc-only (state the flags as
reserved/unconsumed without removing them).

### Allowed Choices
- Can use, for AC-1: a `required_survivors` / `min_survivors` parameter on
  `fan_out`, **or** a caller-supplied validation callback, **or** relocating the
  threshold check entirely into the L2 driver — any option that keeps L1 free of
  consensus concepts and keeps consensus behavior byte-identical.
- Can use, for AC-2: marking journal/resume "planned (not implemented)" **or**
  deleting the line outright.
- Cannot use: a general `chain` / `parallel` / DAG primitive, `pipeline_runner`
  re-platforming, or template-driver decomposition (all deferred to Future Work).
- Cannot use: any change to banner strings, `analysis://` namespace URIs,
  `templates.provenance` fields, or the readers' real production-vs-legacy
  filename fallback logic (`source_registry.py:99-231`).
- Cannot use: breaking `omicsclaw.runtime.consensus.team` re-exports — unless
  DEC-6 explicitly resolves to a breaking internal refactor.

## Feasibility Hints and Suggestions

> Reference only — not prescriptive.

### Conceptual Approach
1. AC-1: delete `MIN_SURVIVING_STEPS`-driven raise from `fan_out` (`fan_out.py:27,189-194,245-257`);
   the L2 gate at `driver.py:230-234` already enforces the consensus `≥2`
   requirement, so consensus stays byte-identical. If a configurable minimum is
   desired at L1, add an opt-in `required_survivors: int | None = None` that
   defaults to "never raise".
2. AC-2/AC-3: text-only edits to `runtime/CONTEXT.md`, `docs/adr/0010-…`, `run.py`
   help, and the two shim skills' `parameters.yaml`.
3. AC-4: lean on the existing ADR 0016 golden-compare harness and
   `tests/runtime/consensus/` for the regression gate.

### Relevant References
- `omicsclaw/runtime/workflow/fan_out.py` — the L1 leak (`:27`, `:189-194`, `:245-257`).
- `omicsclaw/runtime/consensus/driver.py:230-234` — the L2 survivor gate that
  already preserves consensus behavior.
- `omicsclaw/runtime/consensus/team.py:1-23` — back-compat re-export surface (Codex).
- `omicsclaw/runtime/CONTEXT.md:29-40` — the journal/resume drift.
- `omicsclaw/runtime/consensus/run.py:56-59` — the unconsumed flags.
- `omicsclaw/runtime/consensus/dispatch.py` — load-bearing safety helpers
  (banner used by both report paths; namespace lineage tested) — **do not** treat
  as ceremony.
- `omicsclaw/skill/execution/pipeline_runner.py:57-151` — the deferred, structurally
  different second client (sync baton-pass, fail-fast, `pipeline_summary.json`).

## Dependencies and Sequence

### Milestones
1. **De-leak L1** (AC-1): move the survivor policy to L2; strip domain wording.
   - Phase A: relocate / parameterize the threshold.
   - Phase B: audit L1 for residual consensus terms; confirm `team.py` re-exports.
2. **Doc-truth + contract reconciliation** (AC-2, AC-3): independent of Milestone 1.
3. **Regression gate** (AC-4): runs after Milestones 1–2; provenance/banner/
   namespace tests + golden compare + import check.

Milestone 2 has no dependency on Milestone 1. Milestone 3 depends on both.

## Task Breakdown

| Task ID | Description | Target AC | Tag | Depends On |
|---------|-------------|-----------|-----|------------|
| task1 | Remove the `≥2-survivor` raise + narrative/ADR wording from `fan_out`; make the minimum caller-supplied; confirm L2 `driver.py:230-234` preserves consensus behavior | AC-1 | coding | - |
| task2 | Audit L1 for residual consensus terminology and confirm `team.py` re-exports + consensus survivor semantics are unchanged | AC-1, AC-4 | analyze | task1 |
| task3 | Doc-truth pass: correct `runtime/CONTEXT.md` + ADR drift (journal/resume, "general runtime" wording, plan.json ownership) | AC-2 | coding | - |
| task4 | Reconcile `--n-clusters` / `--llm-judge` to a single truthful status across `run.py` help, ADR 0010, and shim skill metadata | AC-3 | coding | - |
| task5 | Safety + back-compat regression: run provenance/banner/namespace tests, golden 5-artifact compare, and the `team` import check | AC-4 | analyze | task1, task4 |

## Future Work / Out of Scope

- **FUT-1: Generalize L1 into a real runtime** — add a `chain` primitive and
  migrate `pipeline_runner` as the validating, structurally-different second
  client (Codex: the shared abstraction must be lifecycle/result normalization,
  not merely "add chain", because `pipeline_runner` is sync/fail-fast/baton-pass).
  - Source DEC: DEC-1
  - Current-loop handoff: AC-1 (a de-leaked, neutral L1 is the prerequisite).
  - Promotion trigger: a concrete near-term second L1 client or chain requirement.
- **FUT-2: Template granularity** — extract a "typed-consensus phases" interface
  (plan-audit · execute-members · gather-artifacts · score/select · reduce ·
  write-artifacts) so a new math overrides only the phases that genuinely differ.
  - Source DEC: DEC-2
  - Current-loop handoff: none (no current AC; documented direction only).
  - Promotion trigger: `rank` or `interval` consensus is scheduled with a known
    artifact schema, with evidence it shares the spine beyond fan-out + final writes.
- **FUT-3: Implement `journal/resume` in L1.**
  - Source DEC: DEC-1, DEC-4
  - Current-loop handoff: AC-2 (the false claim is first removed).
  - Promotion trigger: DEC-1 resolves to "generalize" and a resumability need exists.
- **FUT-4: Final disposition of `--n-clusters` / `--llm-judge`** (implement, or
  remove from CLI + docs + metadata).
  - Source DEC: DEC-5
  - Current-loop handoff: AC-3 (the contract is first made truthful/consistent).
  - Promotion trigger: DEC-5 resolves to implement or remove.

## Claude-Codex Deliberation

### Agreements
- The L1 policy leak is real: `MIN_SURVIVING_STEPS = 2` (`fan_out.py:27`),
  narrative/ADR-0010 wording in `InsufficientSurvivorsError` (`:189-194`), and the
  raise-on-threshold inside `fan_out` (`:245-257`) are not domain-neutral.
- The doc drift is real: `CONTEXT.md` claims L1 owns journal/resume; `fan_out.py`
  has no such behavior; `plan.json` is written by the L2 driver.
- Provenance / banner / namespace is load-bearing and must remain a **tested**
  invariant (`test_templates.py:10-33`, `test_team_runtime.py:74-95`), not prose.
- Reject a speculative DAG / general engine absent a real caller (re-affirms
  ADR 0006/0010).
- Narrow "ADR per template" toward "ADR per new verified guarantee".
- Address the stale CLI/ADR flag contract; mark all architectural forks PENDING.

### Resolved Disagreements
- **"Two registries" (Claude) → corrected (Codex).** `TYPED_CONSENSUS_REGISTRY` is
  *derived* from `CONSENSUS_SOURCES` (`sources.py:32-62`), a single source of
  truth — not duplicated. Claim dropped from the over-engineering case.
- **"Protocol-for-1-impl" (Claude) → corrected (Codex).** `MemberPlanner` and
  `MemberArtifactReader` each have two concrete implementations
  (`planners.py:92-142`, `source_registry.py:99-231`). The Protocol critique
  cannot rest on "one impl"; claim dropped.
- **"merge_fn extraction is trivial" (Claude) → corrected (Codex).**
  `run_typed_consensus` carries categorical-specific phases (label-df assembly,
  NMI matrix, composite scoring, BC selection, operator output schema;
  `driver.py:228-277`). Rank/interval may not share these. Resolution: B1 reframed
  to a **phases interface** (FUT-2) and gated on *evidence* of spine-sharing; the
  "wait" option (B0) gains weight.
- **Framing "L1-centric G/S/H" (Claude) → improved to "separate policy / topology
  / provenance" (Codex), with "H+" as the no-regret default.** Adopted: the floor
  (AC-1..AC-4) is H+; G/S remain the DEC-1 fork on top of it.
- **`team.py` back-compat (Codex, newly surfaced).** Path S ("inline `fan_out`
  back into consensus") is a public import/contract decision, not a free move.
  Captured as AC-4 (preserve by default) + DEC-6 (explicit break policy).

### Convergence Status
- Final Status: `partially_converged` — six genuine user decisions remain PENDING
  **by design** (the user's "list options, do not presuppose conclusions" scope).
  No unresolved Claude-vs-Codex technical disagreement remains; the open items are
  user-knowledge/governance forks, not architectural standoffs.

## Pending User Decisions

- **DEC-1 | L1 fate beyond the floor.**
  - Claude Position: Stay at the H+ floor now; defer generalization (FUT-1) until a
    concrete second client exists; full retraction (S) is optional and constrained
    by DEC-6.
  - Codex Position: H+ is the no-regret default; pursue G only when a real caller
    needs it; don't assume `pipeline_runner` migration validates L1 without
    lifecycle/result normalization.
  - Tradeoff Summary: G pays for genericity (real second client validates the
    abstraction) but is speculative without a caller; S reduces over-engineering
    but touches the `team.py` public surface. Decision hinges on: **is there a
    concrete near-term second L1 client?** Impact surface: `runtime/workflow/`,
    `pipeline_runner.py`, `team.py`, `tests/runtime/consensus/test_team_runtime.py`.
  - Decision Status: `PENDING`

- **DEC-2 | Template granularity.**
  - Claude Position: Decompose into a reusable spine now (B1) to cut future
    duplication.
  - Codex Position: Prefer a phases interface over a raw `merge_fn`, and require
    evidence that `rank`/`interval` actually share the spine before investing;
    otherwise wait (B0).
  - Tradeoff Summary: Decomposing now risks building a spine the future math
    doesn't fit (categorical-specific NMI/scoring/BC steps). Hinges on: **are
    `rank`/`interval` actually planned with known artifact schemas?** Impact:
    `driver.py`, `templates.py`, future-template tests.
  - Decision Status: `PENDING`

- **DEC-3 | Governance scope.**
  - Claude Position: Narrow "ADR per template" to "ADR per new verified guarantee";
    a reused, documented math family needs only a short design note.
  - Codex Position: Same direction — "new provenance class or statistical claim
    needs an ADR; a reused family needs a shorter note."
  - Tradeoff Summary: Lower contributor friction vs slightly weaker single-rule
    auditability. Both reviewers align; user owns the governance call. Impact:
    ADR 0016 §B4a wording, `templates.py` docstring.
  - Decision Status: `PENDING`

- **DEC-4 | journal/resume after the false claim is removed.**
  - Claude Position: Removing the claim (AC-2) is enough now; implement only under G.
  - Codex Position: Not separately raised; consistent with "no speculative work".
  - Tradeoff Summary: Implementing journal/resume only pays off if L1 generalizes
    (DEC-1=G) and a resumability requirement exists. Coupled to DEC-1. Impact:
    `runtime/workflow/`.
  - Decision Status: `PENDING`

- **DEC-5 | `--n-clusters` / `--llm-judge` disposition.**
  - Claude Position: Tentatively remove the dead surface.
  - Codex Position: N/A — open question (implement now / formally deprecate /
    remove from docs+CLI).
  - Tradeoff Summary: Removal cleans the surface but may need skill-metadata /
    allowed-flag updates, not just `run.py`; back-compat for any caller passing the
    flags. Impact: `run.py:56-59`, ADR 0010 §193-203, shim `parameters.yaml`,
    CLI smoke tests.
  - Decision Status: `PENDING`

- **DEC-6 | `omicsclaw.runtime.consensus.team` back-compat policy.**
  - Claude Position: Preserve the re-exports (AC-4 default).
  - Codex Position: Decide explicitly before any simplification — it is an explicit
    back-compat shim (`team.py:1-8`) with dependent tests.
  - Tradeoff Summary: Preserving avoids breaking downstream importers; allowing a
    break simplifies Path S. Impact: `team.py`, `test_team_runtime.py:25-30`, any
    external importer.
  - Decision Status: `PENDING`

## Implementation Notes

### Code Style Requirements
- Implementation code and comments must NOT contain plan-specific terminology such
  as `AC-`, `Milestone`, `Phase`, `Step`, or similar workflow markers. These are
  for this plan document only.
- Use descriptive, domain-appropriate naming in code (e.g. `required_survivors`,
  not "AC-1 threshold").
- Per Codex: each simplification must name the import paths, tests, docs, and CLI
  flags it affects (captured in the DEC impact lines above); line count alone does
  not justify removing derived registries or small safety helpers.

---

--- Original Design Draft Start ---

User framing (2026-06-13): "I feel the current Consensus runtime design is not
generic and reasonable enough — discuss and design a preliminary optimization
plan with me." Followed by a fourth concern: "the consensus-runtime code
implementation is over-engineered (工程化过于严重) — help me optimize it, then run
a Codex cross-review, then commit."

Scope chosen by user: target pains ① L1 under-abstracts/lacks capability,
② template too heavy, ③ governance/docs over-weight or drifted, ④ over-engineered.
Member-type widening explicitly excluded. Ambition: "list options + candidates,
do not presuppose conclusions" — forks left as PENDING decisions.

(The full code-anchored synthesis that seeded this plan is preserved at
`/tmp/consensus_opt_draft.md` and reproduced in substance throughout the sections
above: the L1 leak evidence, the five-axis `fan_out`-vs-`pipeline_runner`
incompatibility, the heavy-template analysis, the doc-drift findings, the
over-engineering audit corrected by Codex, the central "policy/topology/
provenance separation" thesis, the G/S/H fork, and the DEC list.)

--- Original Design Draft End ---
