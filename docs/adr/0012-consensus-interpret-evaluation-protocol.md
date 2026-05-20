# Interpreted layer on verified consensus — `consensus-interpret` as a downstream skill (γ + β) with a 4-axis evaluation panel and grep-tested invariants

## Status

Proposed (2026-05-20). Builds on ADR 0010 (typed-vs-narrative consensus
runtime) and ADR 0011 (typed evaluation protocol). **Does not amend** the
A/B binary established in ADR 0010 — the interpreted layer is a strictly
downstream consumer of A-path output.

## Context

ADR 0010 + ADR 0011 land a typed-vs-narrative consensus paradigm with a
verified-vs-exploratory boundary the user can audit (`analysis://typed/*`
vs `analysis://exploratory/*`, mandatory banners, A-path failure semantics
that refuse to silently degrade). The paradigm produces statistically
defensible cluster labels — but stops short of biology. After the
`consensus-domains` / `sc-consensus-clustering` runs reported by ADR 0010
finish, users still face four manual steps before any analysis figure is
publishable:

1. Read `cross_method_nmi.csv` by hand to find contested regions.
2. Run `spatial-de` (or sc equivalent) with the consensus labels as
   `--groupby` to find per-cluster markers.
3. Cross-reference the top markers against a marker database (PanglaoDB /
   CellMarker / tissue-specific compendia) to name each cluster.
4. Decide which next-step skill to run for which cluster.

All four steps are mechanical and pattern-matched against the consensus
output — exactly the kind of task an LLM does well when grounded in
artifacts. The grilling session that produced this ADR established four
positions:

- **γ (biological annotation)** is the primary value — pure pattern
  description (α) is not interesting; LLM-knowledge cell-type naming (β
  in the grilling table) is hallucination-prone; LLM refining the
  consensus itself (δ in the grilling table) **must be refused** because
  it would break the §11.4 invariant "LLM never participates in
  statistical merging" that the entire consensus innovation rests on.

- **β (proof-driven next-step recommendation)** is secondary value — top-3
  next-step suggestions where each MUST cite ≥1 typed artifact row as
  `evidence_refs`. This distinguishes the recommendation from the existing
  `orchestrator` skill (forward `query → skill` routing); here the
  direction is backward (`result_artifacts → (skill, evidence)`), and the
  per-recommendation evidence binding prevents the feature from
  degenerating into a generic OmicsClaw catalog advertisement.

- **Architectural placement**: a new standalone skill at
  `skills/spatial/consensus-interpret/` (sibling to `consensus-domains`),
  NOT a new path in the runtime, NOT an agent-only tool. Rejected
  alternatives include "co-equal Path C inside `runtime/consensus/`"
  (would break the A/B binary that is ADR 0010's load-bearing claim) and
  "agent-only tool with no CLI" (would lose batch-pipeline reproducibility
  story).

- **Input surface**: `--input <typed_run_dir> --tissue <name>` minimum;
  `--adata` defaults to the path persisted in the typed run's `plan.json`
  (precondition: `plan.json` must persist absolute adata path — slice 0
  of the implementation plan). Marker DB defaults to a bundled
  tissue-keyed TSV (brain / immune / kidney / liver shipped with the
  skill); user can override with `--markers`.

What is missing — and what this ADR contributes — is the **evaluation
protocol** for the interpreted layer. Without it the headline claim
("verified consensus can be biologically interpreted without
hallucination") is just an assertion.

### Why a new ADR rather than amending ADR 0011

ADR 0011 codifies the typed-consensus evaluation contract: "is this
consensus statistically defensible?" Interpreted-layer evaluation is a
distinct question: "is the LLM's biological interpretation grounded in
the typed run's artifacts, not in training-data priors?" Conflating the
two into one ADR mixes statistical evaluation with hallucination
evaluation; the two falsifiability surfaces are best kept independent so
each can evolve on its own. ADR 0012 stands alongside ADR 0011; neither
subsumes the other.

## Decision

### Skill placement & banner

- Skill path: `skills/spatial/consensus-interpret/` (sibling to
  `consensus-domains`). Domain-agnostic in implementation — dispatches on
  `plan.json`'s `skill_name` to read either spatial-domains or
  sc-clustering typed runs; relocates to `skills/orchestrator/` if v2
  introduces non-spatial-primary typed sources.
- Output namespace: `analysis://interpreted/<typed_run_id>` — MUST cite
  `analysis://typed/<typed_run_id>` as evidence base in the report
  footer (audit line) and in `audit.json`.
- Mandatory report banner (first line, non-configurable):
  - `[A+I: Interpreted on verified consensus]` — default operating mode.
  - `[I-noLLM: Structural patterns only — biology annotation disabled]`
    — `--no-llm` degrade mode (CI / offline / structural-only).

### Failure semantics (three-tier model, exit codes extend ADR 0010's)

| Tier | Trigger | Behavior | Exit |
|---|---|---|---|
| **T1 — Preflight** | typed_run_dir invalid / no `plan.json`; adata at `plan.json:input_path` missing; adata `obs` index ≠ `consensus_labels.tsv` `observation`; requested `--tissue` has no bundled marker DB and no `--markers` override | fail-fast; no artifacts written | 3 / 4 / 5 |
| **T2 — Per-cluster** | A cluster has < 3 cells → DE undefined; a cluster's top markers all miss the marker DB; a candidate next-step has only `priority=3` evidence | degrade: mark cluster `"interpretation_status": "low_confidence"` or `"failed"`; continue. **Floor**: if interpretable_cluster_frac < 0.5 → escalate to T1 and exit 8 (CoverageBelowThreshold) | 0 / 8 |
| **T3 — Invariant** | LLM cell-type claim without `evidence.markers[]`; LLM next-step without `evidence_refs[]`; LLM-emitted text overwrites typed artifact data; banner missing / wrong | fail-fast; `InvariantViolationError` | 7 |

Default LLM-unavailable behavior is fail-fast (exit 6) for batch
pipeline trust. `--no-llm` is the explicit degrade flag that produces
structural-only output with the distinct `[I-noLLM: ...]` banner so the
two products are visually unconfusable.

Complete exit-code table (continuous with ADR 0010's `consensus-domains`
convention 3 / 5 / 6):

```
0  success
2  argparse error
3  TypedRunInvalid
4  AdataMismatch
5  MarkerDBUnavailable
6  LLMUnavailable
7  InvariantViolation
8  CoverageBelowThreshold
```

### 4-axis evaluation panel

The interpreted layer is evaluated along four independent axes:

#### Axis 1 — `interpretation_faithfulness` (structural; always-run)

```
faithfulness = (
    LLM-generated sentences in interpreted_report.md
    that contain ≥1 verbatim citation
    of a typed artifact value
    (cluster id / NMI value / marker name / p-value / member name)
) / (total LLM-generated sentences)
```

- **Implementation**: regex over the markdown body after the H2 sections
  matching cluster references (`cluster\s+\d+`, `NMI=[0-9.]+`, etc.).
- **Floor**: **1.00** (it is an *invariant*, not a soft metric — grep
  tested in T3). Exposed as a metric only so a regression detector can
  fire when an LLM output schema change reduces it.
- **Cost**: in-process, deterministic, < 100 ms. Always runs in default
  CI.

#### Axis 2 — `marker_grounding_rate` (DE-tied; LLM-stubbed always-run + real-LLM gated)

For each interpreted cluster `c`:

```
LLM_markers(c)  = top-K markers the LLM cites for cluster c's cell type
DE_markers(c)   = top-K markers from inline rank_genes_groups for cluster c
grounding(c)    = |LLM_markers(c) ∩ DE_markers(c)| / K
marker_grounding_rate = mean over interpreted clusters
```

- **K**: 20 by default (LLM cites up to 20; DE returns top-20).
- **Floor**: **0.60** (≥ 60% of LLM-claimed markers must be in the
  cluster's actual DE top-20).
- **Real-LLM gate**: `RUN_INTERPRET_LLM=1` env var. Default CI uses a
  recorded LLM-output fixture asserting the *parser* extracts markers
  correctly even when LLM picks contradicting ones.
- **Cost**: stubbed ≈ 100 ms; real LLM ≈ 5 s per cluster.

#### Axis 3 — `interpret_self_consistency` (LLM-stability; env-gated)

```
For 3 LLM seeds {0, 1, 2}, same typed_run + same marker DB:
  assignment_i = cluster_id → cell_type (LLM output of run i)

agreement = (
    fraction of clusters where majority of 3 runs
    agree on the same cell_type
)
```

- **Floor**: **0.70**.
- **Gate**: `RUN_INTERPRET_CONSISTENCY=1` (3× real-LLM calls).
- **Cost**: 3× expert_concordance_hero is too high — uses a smaller
  fixture run (8 synthetic clusters) rather than full DLPFC.

#### Axis 4 — `expert_concordance_hero` (DLPFC 151673 ground-truth; env-gated)

```
typed_run = consensus-domains output on DLPFC 151673 (ADR 0011 hero)
interpreted = consensus-interpret(--input typed_run --tissue brain)

Per-cluster predicted cell_type ↔ Maynard et al. 2021 layer GT
→ many-to-one mapping → ARI of (LLM-mapped cluster labels) vs (GT layer)
```

- **Floor**: **ARI ≥ 0.45** (target: outperform the best single-method
  ARI baseline measured by ADR 0011 typed hero; below this, interpreted
  adds no value over reading the typed report by hand).
- **Gate**: `RUN_INTERPRET_DLPFC=1` (full pipeline + real LLM ≈ 10 min).
- **Cost**: highest; runs nightly / before release, not per commit.

### Pass rule (hard pass AND, mirroring ADR 0011)

```
interpreted hero benchmark passes IFF:
  faithfulness            == 1.00     (invariant; T3 grep test)
  marker_grounding_rate   ≥ 0.60      (gated real-LLM run)
  expert_concordance_hero ≥ 0.45      (gated DLPFC run)
self_consistency          ≥ 0.70      (gated; reported separately)
```

A single axis below floor blocks the "interpreted consensus is publish-
ready" claim. Reporting axes (axis 3 reported alongside hero) are not
gated by hard pass.

### Bundled marker DBs (vendored, attribution-required)

```
skills/spatial/consensus-interpret/data/markers/
├── README.md              # acquisition + LICENSE attribution
├── panglaodb_brain.tsv    # ~600 genes / ~50 cell types  (CC-BY-4.0)
├── panglaodb_immune.tsv   # ~400 / ~30
├── panglaodb_kidney.tsv   # ~300 / ~20
├── cellmarker_liver.tsv   # ~250 / ~15                   (CC0)
```

Schema: `gene\tcell_type\tsource\tspecies\ttissue\tweight` (TSV;
pure-CSV, no new dependency). License compatible with Apache-2.0.

Override: `--markers <path.tsv>` accepts any user-provided TSV in the
same schema.

### Grep-tested invariants (T3 anchor)

Three invariants, each with a regression-locked unit test:

```python
def test_no_celltype_claim_without_markers(interpreted_dir):
    """For every cluster with interpretation_status == 'interpreted',
    evidence.markers MUST be non-empty."""

def test_no_nextstep_without_evidence_refs(interpreted_dir):
    """For every next_steps[*] entry, evidence_refs MUST be non-empty."""

def test_banner_present_in_interpreted_report(interpreted_dir):
    """First line of interpreted_report.md is exactly one of two banners."""
```

These run in default CI; they are the structural enforcement of
"interpretation faithfulness == 1.00".

## Consequences

### Positive

- **Boundary integrity preserved.** The A/B binary in ADR 0010 §11.3 is
  unchanged. Interpreted is a downstream consumer of A, not a third
  path. `analysis://interpreted/<run_id>` MUST point at
  `analysis://typed/<run_id>` — auditors can ignore interpreted entirely
  and still have the full verified consensus story.
- **Cross-source compatibility for free.** `consensus-interpret`
  dispatches on `plan.json`'s `skill_name` — same skill handles
  spatial-domains and sc-clustering typed runs. v2's consensus-celltypes
  / consensus-de extensions inherit the interpret layer without further
  ADR.
- **Falsifiability surface extended by 4 testable claims** (ADR 0010
  §11.5 table grows by 4 rows). Each new claim is operationalized in
  default or gated CI; no purely-rhetorical claims added.
- **β does not duplicate orchestrator.** Direction (backward vs forward),
  input surface (typed artifacts vs natural language query), and binding
  (mandatory evidence_refs vs none) are all distinct.

### Negative

- **More moving parts.** Adds a new skill, four marker TSVs, four
  evaluation tests, three env-gated long-runs. The bundled marker DB
  introduces vendoring discipline (LICENSE attribution must stay in sync
  with PanglaoDB / CellMarker releases).
- **Default CI does not catch LLM-output regressions on the cell-type
  level.** Axis 2 stubbed-mode catches *parser* regressions only; real
  marker-grounding shifts only show up when `RUN_INTERPRET_LLM=1` runs
  (nightly). Trade-off accepted because the 5 s/cluster cost is prohibitive
  in per-commit CI.
- **Interpretation faithfulness as both invariant and metric is awkward.**
  Axis 1's floor is 1.00 and equals the T3 grep test. Reported as a
  metric only for the regression-detector benefit. Documented inline.

### Neutral

- **No change to existing ADRs.** ADR 0010 and ADR 0011 stand. This ADR
  is strictly additive.
- **No change to runtime/consensus/.** All implementation lives in the
  new skill directory; `runtime/consensus/source_registry.py`,
  `driver.py`, `report.py` etc. are untouched.

### Rejected alternatives

1. **Co-equal Path C inside `runtime/consensus/`.** Rejected: would
   force amending ADR 0010, would weaken §11.3 "audit only typed/* by
   default" claim, would require a 3-banner system (A / B / C) instead
   of 2 + 1-downstream.

2. **Agent-tool-only (no standalone CLI).** Rejected: loses
   reproducibility story (batch pipelines / Makefiles / paper repro
   scripts cannot invoke an agent-loop tool from outside). The skill
   form gives both (chat-loop calls `run_skill`, CLI users call
   directly).

3. **LLM cell-type naming without marker grounding.** Rejected as
   hallucination factory; no testable invariant possible.

4. **Pre-computed DE as required input.** Rejected: would force users to
   run `spatial-de` first with the correct `--groupby consensus_kmode`;
   easily mis-specified. Inline DE inside `consensus-interpret` keeps
   the contract "DE is on the consensus labels we just produced".

5. **Amend ADR 0011 rather than new ADR 0012.** Rejected: typed
   statistical evaluation and interpreted hallucination evaluation are
   different falsifiability surfaces; combining them into one document
   makes future amendments to either touch both.

## Vocabulary (canonical; cross-referenced from `docs/CONTEXT.md`)

- **Interpreted consensus** — output of the `consensus-interpret` skill;
  the LLM-grounded biological interpretation of a verified typed
  consensus run. Lives at `analysis://interpreted/<typed_run_id>` with
  banner `[A+I: Interpreted on verified consensus]`. Not a path in the
  consensus runtime; a downstream skill consuming A-path output.

- **Verified consensus run** — synonym for a typed consensus output
  directory at `analysis://typed/<run_id>`. The thing `consensus-interpret`
  interprets.

- **Interpretation faithfulness** — invariant + soft metric measuring
  the fraction of LLM-generated claim sentences that cite verbatim
  typed-run values. Floor 1.00 (invariant).

- **Marker grounding** — invariant that every LLM cell-type claim must
  cite ≥1 marker drawn from inline per-cluster DE ∩ bundled marker DB.
  Quantified by `marker_grounding_rate`. Floor 0.60.

- **Backward proof-driven recommendation** — `consensus-interpret`'s β
  output: top-3 next-step skill suggestions, each with mandatory
  `evidence_refs[]` citing typed-run artifact rows. Direction
  (backward) distinguishes it from `orchestrator`'s forward
  `query → skill` routing.

- **`analysis://interpreted/<run_id>`** — graph-memory namespace for
  interpreted outputs. Always cites its `analysis://typed/<run_id>`
  evidence base.

- **`InvariantViolationError`** — raised when LLM output violates the
  marker-grounding or evidence-ref contract; non-recoverable; exit 7.

- **`CoverageBelowThreshold`** — raised when fewer than 50% of
  consensus clusters are interpretable (all markers miss DB, all
  next-steps fall to priority 3, etc.); exit 8.
