# Implementation Plan: ADR 0016 — Consensus as a Workflow Runtime

## Overview

Restructure consensus orchestration per ADR 0016: extract a thin topology
layer `runtime/workflow/` (L1, `fan_out` only), grow the source registry into a
declarative `ConsensusSource` contract (L3), unify member planning behind a
`MemberPlanner` protocol, fold typed/narrative dispatch into a template registry
carrying explicit `provenance`, and collapse the two per-flavour CLI wrappers
(`consensus_domains.py`, `sc_consensus_clustering.py`) into one generic entry +
3-line shims. **This is a behaviour-preserving refactor**: external CLI output,
artifact schemas, banners, and namespaces stay byte-identical; the only
intentional behaviour *change* is fixing `sc-consensus-clustering` silently
ignoring `--confirm-plan`.

## Architecture Decisions (from ADR 0016 + CONTEXT.md)

- **L1 = topology only**, born with one primitive (`fan_out`), one client
  (consensus). No `chain`, no `pipeline_runner` re-platform (deferred PR).
- **Two-axis extension**: `WorkflowTemplate` (math shape + provenance) ×
  `ConsensusSource` (declarative data + one `MemberPlanner`).
- **Provenance moves to the template**; `dispatch.py` logic folds into the
  registry. Amends ADR 0010's single-file audit claim → two explicit fields.
- **Workflow is atomic to the LLM**; invoked via existing `execute_omicsclaw`.
  No new tool, no agent-loop changes.
- **Per-flavour `SKILL.md` + `parameters.yaml` survive**; `.py` → 3-line shim.

## Guiding constraint: keep the suite green at every task

The contract that must never go red (run after each task):

```
pytest tests/runtime/consensus/ -q
pytest skills/spatial/consensus-domains/tests/ skills/singlecell/scrna/sc-consensus-clustering/tests/ -q
```

Untouched math (L2) — these must NOT change at all:
`test_alignment.py`, `test_categorical_operators.py`, `test_member_scoring.py`,
`test_spatial_metrics.py`, `test_lca_wrapper.py`, `test_dlpfc_benchmark.py`,
`test_self_consistency.py`.

---

## Task List

### Phase 1 — L1 foundation (behaviour-preserving extraction)

#### Task 1: Extract `fan_out` to `runtime/workflow/`, de-consensus'd

**Description:** Create `omicsclaw/runtime/workflow/` with a neutral
`WorkflowStep` type and `fan_out` = today's `run_team` with `member`→`step`
naming. Make `runtime/consensus/team.py` re-export `run_team`,
`InsufficientSurvivorsError`, `MemberRunResult`, `TeamRunResult` from the new
location so no consensus caller changes. `ConsensusMember` becomes a
`WorkflowStep` (satisfies the protocol: `name`, `skill_name`, `to_extra_args()`,
`step_output_dir()` aliasing `member_output_dir()`).

**Acceptance criteria:**
- [ ] `omicsclaw/runtime/workflow/fan_out.py` defines `fan_out(...)`,
  `WorkflowStep`, `StepRunResult`, `FanOutResult`, `InsufficientSurvivorsError`
  with the same semantics as `run_team` (≥2-survivors rule, semaphore cap,
  cancel/timeout, injectable `runner`).
- [ ] `runtime/consensus/team.py` is a thin re-export; `ConsensusMember`
  is recognized by `fan_out` unchanged.
- [ ] No file outside `runtime/workflow/` and `runtime/consensus/team.py`
  changed.

**Verification:**
- [ ] `pytest tests/runtime/consensus/test_team_runtime.py -q` passes unchanged.
- [ ] `pytest tests/runtime/consensus/ -q` fully green.
- [ ] `grep -rn "from omicsclaw.runtime.consensus.team import" omicsclaw tests`
  — every importer still resolves.

**Dependencies:** None.
**Files likely touched:** `runtime/workflow/__init__.py`,
`runtime/workflow/fan_out.py`, `runtime/consensus/team.py`,
`runtime/consensus/member.py`. **Scope:** M.

#### Task 2: Point `driver.run_typed_consensus` at L1 `fan_out`

**Description:** Replace the `run_team(...)` call in `driver.py` step 2 with
`fan_out(...)`; adjust local names only. Pure internal swap, no signature
change to `run_typed_consensus`.

**Acceptance criteria:**
- [ ] `driver.py` imports from `runtime/workflow/`, not `runtime/consensus/team`.
- [ ] `run_typed_consensus` signature + `TypedConsensusRun` unchanged.

**Verification:**
- [ ] `pytest tests/runtime/consensus/test_driver.py -q` passes unchanged.

**Dependencies:** Task 1.
**Files likely touched:** `runtime/consensus/driver.py`. **Scope:** S.

### Checkpoint: L1 extracted
- [ ] Full `tests/runtime/consensus/` suite green.
- [ ] `runtime/workflow/` exists with exactly one primitive; nothing else moved.

---

### Phase 2 — L3 contract + planners (additive; old wrappers still drive the old way)

#### Task 3: Grow `TypedConsensusSource` → `ConsensusSource`; add `CONSENSUS_SOURCES`

**Description:** Extend the registry value type with `name`, `template`,
`member_skill`, `planner`, `domain`, `report_title`, `param_hints_path`
(all with safe defaults preserving current behaviour). Add
`CONSENSUS_SOURCES: dict[str, ConsensusSource]` keyed by **flavour name**
(`"consensus-domains"`, `"sc-consensus-clustering"`). Keep
`TYPED_CONSENSUS_REGISTRY` (keyed by `member_skill`) as a derived compat alias
during transition.

**Acceptance criteria:**
- [ ] `ConsensusSource` carries all eight fields; `reader` semantics unchanged.
- [ ] `CONSENSUS_SOURCES["consensus-domains"].member_skill == "spatial-domains"`
  and `["sc-consensus-clustering"].member_skill == "sc-clustering"`.
- [ ] `TYPED_CONSENSUS_REGISTRY` still resolves the two member-skill keys
  (compat) and `select_consensus_mode` behaviour is unchanged.

**Verification:**
- [ ] `pytest tests/runtime/consensus/test_source_registry.py -q` green
  (extend, don't break, its assertions).

**Dependencies:** None (parallelizable with Task 1/2).
**Files likely touched:** `runtime/consensus/source_registry.py`. **Scope:** M.

#### Task 4: `MemberPlanner` protocol + three implementations

**Description:** Introduce `MemberPlanner` (Protocol) and
`ChairLLMPlanner` / `SweepPlanner` / `ExplicitPlanner`, lifting the exact
member-construction logic from `consensus_domains._plan_members`
(chair + `--all` + explicit) and `sc_consensus_clustering._plan_members`
(sweep + explicit). Each planner produces `list[ConsensusMember]` identical to
what the wrapper produced for the same args.

**Acceptance criteria:**
- [ ] `SweepPlanner` reproduces `_members_from_sweep` output for the default
  resolutions/methods and for `--all`.
- [ ] `ChairLLMPlanner` reproduces `propose_members(...)` selection (offline
  fallback) and `--all` from `param_hints`.
- [ ] `ExplicitPlanner` reproduces both wrappers' `--members` parsing,
  including the duplicate-name `SystemExit`.

**Verification:**
- [ ] New `tests/runtime/consensus/test_planners.py` asserts member-list
  equivalence against the old wrapper functions on fixed args.
- [ ] Full suite green.

**Dependencies:** Task 3 (planner referenced by `ConsensusSource`).
**Files likely touched:** `runtime/consensus/planners.py`,
`tests/runtime/consensus/test_planners.py`. **Scope:** M.

### Checkpoint: contract + planners exist, fully unit-tested
- [ ] Wrappers still untouched and green; new contract/planners covered by tests.

---

### Phase 3 — L2.5 templates + provenance (the ADR-0010 amendment)

#### Task 5: `WorkflowTemplate` + `TEMPLATES`; fold dispatch into the registry

**Description:** Add `WorkflowTemplate(driver, provenance)` and
`TEMPLATES = {"categorical": (run_typed_consensus, "typed"), "narrative":
(run_narrative_consensus, "exploratory")}`. Reimplement
`select_consensus_mode`, `output_banner`, `consensus_namespace` to derive from a
source's template provenance (`CONSENSUS_SOURCES[name].template` →
`TEMPLATES[...].provenance`), preserving identical return values and the
`force_mode` override. `dispatch.py` keeps its public function names.

**Acceptance criteria:**
- [ ] `output_banner`/`consensus_namespace` return byte-identical strings for
  typed and exploratory as before.
- [ ] Provenance is read from `TEMPLATES`, not from `set` membership.
- [ ] `--mode narrative` force-override still works.

**Verification:**
- [ ] `pytest tests/runtime/consensus/test_plan_narrative.py -q` green.
- [ ] Any dispatch/banner test green; add a test asserting
  `TEMPLATES["narrative"].provenance == "exploratory"`.

**Dependencies:** Task 3.
**Files likely touched:** `runtime/consensus/templates.py`,
`runtime/consensus/dispatch.py`. **Scope:** M.

### Checkpoint: provenance flows from templates
- [ ] Banners/namespaces unchanged externally; suite green; ADR 0010 amendment
  is live and auditable from `CONSENSUS_SOURCES` + `TEMPLATES`.

---

### Phase 4 — Generic entry + shim collapse (the payoff)

#### Task 6: Generic `runtime/consensus/run.py`

**Description:** Write the de-duplicated `_main_async` as a generic
`run.py` that: parse args → look up `CONSENSUS_SOURCES[--source]` →
`source.planner.propose(args)` → `_maybe_confirm_plan` (folded in, honoured for
all flavours) → `TEMPLATES[source.template].driver(...)` →
`format_typed_report(title=source.report_title)` → write `report.md` + print OK.
Same three `except` blocks and exit codes as the wrappers.

**Acceptance criteria:**
- [ ] `python -m omicsclaw.runtime.consensus.run --source consensus-domains
  --demo`-equivalent produces output byte-identical to the old
  `consensus_domains.py` (golden compare on a fixed seed/input).
- [ ] Exit codes 2/3/5/6 preserved for the four failure modes.
- [ ] `_maybe_confirm_plan` runs for any source when `--confirm-plan` + TTY.

**Verification:**
- [ ] Golden-output test: run old wrapper vs new entry on the same args
  (captured before Task 7/8 delete the old bodies), diff `report.md` +
  `consensus_labels.tsv` + `member_scores.csv` → identical.

**Dependencies:** Tasks 3, 4, 5.
**Files likely touched:** `runtime/consensus/run.py`,
`tests/runtime/consensus/test_run_entry.py`. **Scope:** M.

#### Task 7: Collapse `consensus-domains` to a shim

**Description:** Replace `consensus_domains.py`'s body with a 3-line shim
forwarding to `run.py --source consensus-domains`. Keep `SKILL.md`,
`parameters.yaml`, and `tests/`.

**Acceptance criteria:**
- [ ] `consensus_domains.py` ≤ ~6 lines, zero orchestration logic.
- [ ] `run_skill("consensus-domains", ...)` still resolves and runs.

**Verification:**
- [ ] `pytest skills/spatial/consensus-domains/tests/test_cli_smoke.py -q` green.

**Dependencies:** Task 6.
**Files likely touched:** `skills/spatial/consensus-domains/consensus_domains.py`.
**Scope:** S.

#### Task 8: Collapse `sc-consensus-clustering` to a shim + fix the `--confirm-plan` bug

**Description:** Replace the body with a shim forwarding to
`run.py --source sc-consensus-clustering`. The shim path inherits the folded-in
`_maybe_confirm_plan`, so `--confirm-plan` now works (was silently ignored).

**Acceptance criteria:**
- [ ] `sc_consensus_clustering.py` ≤ ~6 lines.
- [ ] `--confirm-plan` now triggers the interactive gate for sc (regression
  test for the bug fix).

**Verification:**
- [ ] `pytest skills/singlecell/scrna/sc-consensus-clustering/tests/test_cli_smoke.py -q`
  green.
- [ ] New assertion: sc honours `--confirm-plan`.

**Dependencies:** Task 6.
**Files likely touched:**
`skills/singlecell/scrna/sc-consensus-clustering/sc_consensus_clustering.py`,
its `tests/`. **Scope:** S.

### Checkpoint: duplication gone, both flavours through the generic entry
- [ ] Both smoke tests green; golden outputs identical; `_make_bc_selector` /
  `_main_async` duplication deleted; `sc --confirm-plan` fixed.

---

### Phase 5 — Cleanup + docs reconciliation

#### Task 9: Remove transition aliases; reconcile docs

**Description:** If `grep` shows no remaining importers, drop the
`TYPED_CONSENSUS_REGISTRY` compat alias (or keep + mark deprecated with a
comment). Verify `omicsclaw/runtime/CONTEXT.md` and ADR 0016 match the landed
code (file names, `runtime/workflow/`, `CONSENSUS_SOURCES`). Update the CLAUDE.md
CLI reference only if a command string changed.

**Acceptance criteria:**
- [ ] No dead code / unused aliases (or each is explicitly deprecation-marked).
- [ ] CONTEXT.md + ADR 0016 reference only names that exist in the tree.

**Verification:**
- [ ] Full `tests/runtime/consensus/` + both smoke suites green.
- [ ] `grep` for deleted symbols returns no live references.

**Dependencies:** Tasks 7, 8.
**Files likely touched:** `source_registry.py`, docs. **Scope:** S.

### Checkpoint: Complete
- [ ] All ADR 0016 decisions realized; suite green; docs match code; ready for
  Phase-3 code review.

---

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Collapse drifts output (banners/labels/scores) | High | Golden-compare old wrapper vs `run.py` (Task 6) BEFORE deleting old bodies (Tasks 7-8) |
| Registry re-key (`member_skill`→`name`) breaks `source` lookups in `driver`/tests | Med | Keep both keyings via compat alias through Phase 4; remove only in Task 9 after grep |
| `fan_out` rename breaks a non-consensus importer of `team.py` | Med | Task 1 keeps `team.py` as a re-export shim; grep all importers |
| Narrative provenance refactor changes a banner string | Med | Assert byte-identical banner/namespace strings in Task 5 |
| `run_skill` can't resolve a 6-line shim | Low | Skill registry loads by directory/`SKILL.md`; the `.py` stays a valid entrypoint — smoke tests prove it (Tasks 7-8) |

## Out of scope (per ADR 0016 §Open)

- `chain` primitive + `pipeline_runner` re-platform onto L1.
- v2 `rank` (DE-RRA) / v3 `interval` templates (each its own ADR).
- `consensus-interpret` changes (stays a downstream skill).

## Open Questions — RESOLVED (2026-05-30)

- **`TYPED_CONSENSUS_REGISTRY`**: grep showed it is *not* a removable alias —
  `dispatch` + tests consume it. "Hard-delete" was realised as **de-duplication**:
  it became a derived view (`{s.member_skill: s for s in CONSENSUS_SOURCES.values()}`)
  in `sources.py`, deleting the hand-maintained second copy + the
  `TypedConsensusSource` class alias. Single source of truth = `CONSENSUS_SOURCES`.
- **`runtime/workflow/CONTEXT.md`**: deferred — inherits `runtime/CONTEXT.md`
  until `chain` lands (user decision).
- **`team.py`**: kept as a documented back-compat re-export of the lifted
  `fan_out` (used by `test_team_runtime` / `test_driver`); not a stale alias.
