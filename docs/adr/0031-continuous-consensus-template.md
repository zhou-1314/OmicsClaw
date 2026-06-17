# Continuous (rank-gauge) consensus template — per-cell pseudotime/score vector consensus

> Status: **Accepted** (2026-06-17). Adds a third typed Workflow template,
> `continuous`, to the `TEMPLATES` registry (ADR 0016 L2.5) alongside
> `categorical` and `narrative`. Leaves the banner / `analysis://` namespace /
> provenance / `team.py` back-compat contracts and every existing flavour
> byte-identical. A new template is an ADR-worthy event (ADR 0016 B4a: "adding a
> template means adding a new verified math guarantee").
>
> **This records the design** (a `grill-with-docs` session, 2026-06-17); the
> implementation — the `run_continuous_consensus` driver, the pseudotime reader,
> the operators, and the `sc-consensus-pseudotime` source row — is the **next step
> and has not yet landed**. Present-tense "Decision" text below describes the
> agreed design, not existing code; the reusable seams it depends on are verified
> present (see *Implementation surface*).

## Context

The `categorical` template (`run_typed_consensus`) is the only verified consensus
math shape today: it aligns **partitions** (Hungarian relabel), votes per cell,
reports per-cell support, scores `α·cross_NMI + β·intrinsic`, and runs an intrinsic
panel. Its members are clusterings / spatial domains — categorical label vectors.

A whole class of single-cell quantities is **continuous, not categorical**:
pseudotime, and other per-cell scalar scores. Robustness here is just as real a
question as for clustering ("does the trajectory ordering hold across DPT /
Palantir / VIA?"), but the `categorical` math does not apply, for one structural
reason: a pseudotime is only defined **up to a monotone reparameterisation and a
direction flip** (method A's `t` ≈ method B's `1−t`; only ranks are meaningful).
You cannot average raw pseudotimes — they must be made comparable first. This is
the continuous analog of label-matching, and it is a different math shape, so per
ADR 0016 it warrants its own template + ADR rather than a new `categorical` flavour.

(Design resolved via a `grill-with-docs` session, 2026-06-17.)

## Decision

### 1. New `continuous` Workflow template (typed, its own driver)

Add one `TEMPLATES` entry: `continuous` → `WorkflowTemplate(provenance="typed",
driver=run_continuous_consensus)`. It is a **new driver function** (not folded into
`run_typed_consensus`). It reuses the **domain-neutral** L2/L1 seams — BC top-K
selection, the mandatory banner + `analysis://typed/<id>` namespace, `fan_out` +
`MIN_CONSENSUS_MEMBERS`, and provenance/dispatch routing — but its scoring, operators,
member reader, artifact schema, and report sections are **new**, because the categorical
scoring scaffold hard-codes `cross_NMI` + a `MemberScore.n_clusters` that do not apply
(see *Implementation surface*). `provenance="typed"` because rank-correlation + median rank
aggregation is deterministic verified math; member-method stochasticity (Palantir/VIA
diffusion maps) is a documented "reproducible within tolerance" caveat, as scVI is
for `categorical` integration.

### 2. Scope: rank-gauge per-cell **vector** consensus (one vector per member)

Each member emits **one per-cell scalar** (a pseudotime, or any monotone-comparable
score). Branching **trajectory topology** (branch assignment, branch points) is
**explicitly out of scope** — it is a different, part-categorical shape (which lineage
a cell is on is a label), handled either by `categorical` on branch labels or by a
future `trajectory` template. v1 `continuous` is the scalar-vector case only.

### 3. Member axis: different pseudotime methods, shared root

The member axis is **different pseudotime methods** (the `sc-consensus-integration`
analog, not the `sc-consensus-clustering` param-sweep analog). v1 member set =
methods that emit a **single global pseudotime**: `dpt` / `palantir` / `via`.
Multi-lineage methods (`slingshot_r` / `monocle3_r` / `cellrank`) are **deferred** —
they re-introduce the branching topology scoped out in §2.

All members share **one user-specified root** (`--root-cluster` / `--root-cell`),
required in v1. This both matches biology (a trajectory needs a root) and **pins
direction**, reducing the gauge freedom to monotone-reparameterisation only and
making the consensus well-posed. (No root → fall back to direction-sign alignment;
v1 requires the root.)

### 4. Core math: rank-normalise + direction safeguard

- **Full coverage required.** All members run on the same input and share `obs_names`;
  v1 requires every member to cover **all** cells. A member with partial coverage is
  **dropped whole** (never unioned per-cell), so the aligned matrix carries no NaNs.
- **Canonical form:** rank-normalise each member's pseudotime to its empirical rank
  `/ (n−1)` ∈ `[0, 1]` (ties → average rank). Monotone-invariant, so it exactly cancels
  the residual gauge (min-max would only handle affine transforms).
- **Direction safeguard (single pass, non-circular).** The shared root already orients
  every member, so this is a guard, not the primary mechanism. Pick the **anchor** = the
  member with the highest mean pairwise `|ρ|` (the most central member — a reference that
  does **not** depend on flips); in one pass, flip any member whose rank vector
  anti-correlates (`ρ < 0`) with the anchor, then warn. No iteration. Flipped members are
  flagged. (Continuous analog of `categorical`'s relabel-then-vote.)

### 5. Cross-member agreement (the `α` term, replacing `cross_NMI`)

Form the pairwise Spearman matrix `ρ(i, j)` over members' (direction-aligned,
rank-normalised) pseudotimes. Two aggregations of the **same** matrix:
- **Member `i`'s agreement score** (drives BC selection + the `weighted` operator) =
  mean of `ρ(i, j)` over `j ≠ i` (row mean). This is the `α` term; composite =
  `α · agreement_i` (with `β = 0`, §7).
- **Cohort agreement** (drives the weak-agreement guard, §9) = mean over **all** distinct
  off-diagonal pairs `ρ(i, j)`.

(Spearman = Pearson-on-ranks, the natural partner of rank-normalisation.)

### 6. Operators (the finer grain inside the template; kmode/weighted analog)

- **`median`** (default): per-cell median of the aligned rank vectors — robust to a
  single outlier member.
- **`weighted`**: per-cell mean weighted by member composite score. (Accepted with the
  noted conformity-bias caveat — a member agreeing more with the pack gets more weight;
  acceptable here because down-weighting an outlier method is usually desirable, but
  recorded so it is revisited.)
- **No `lca` analog** in v1.
- The aggregated consensus vector is **re-rank-normalised to `[0, 1]`**: rank each cell's
  aggregate value across all cells, `(rank − 1) / (n_cells − 1)`. A per-cell median (or
  weighted mean) of rank vectors is **not itself a rank vector** — when members agree on
  order but disagree on *spacing* it produces **plateaus/ties** (a block of cells the
  methods place at 0.2 vs 0.4 collapses to a tied 0.3) — so re-ranking restores a monotone
  `[0, 1]` scale (ties → average rank). Re-ranking does **not** manufacture order members
  did not provide: v1 reports the consensus **tie fraction** so a largely-flat consensus is
  visible, not hidden.

### 7. Scoring: agreement-only in v1 (`β = 0`), intrinsic panel deferred

v1 composite = the cross-member agreement (§5) only; **`β = 0`**, and
`ConsensusSource.intrinsic_panel = ""` (no panel). An unsupervised intrinsic-quality
metric for a single pseudotime (e.g. graph-smoothness: pseudotime varying smoothly
over the kNN graph) is the leading candidate but is **deferred** — it carries the
exact risk this project just documented in **ADR 0029** (a trivially-flat pseudotime
scores high on smoothness while being meaningless; every GT-free intrinsic tried for
integration anti-correlated or was redundant). A `continuous` intrinsic axis must pass
the same **GT-validation gate** before it scores. v1 stays honest with agreement-only.
(Implementation note: the driver sets `ScoreConfig(alpha=1.0, beta=0.0)` **explicitly** —
`intrinsic_panel=""` skips the panel, but `BETA_DEFAULT` is 0.4, so β must be forced to 0.)

### 8. Per-cell support: MAD of aligned ranks

The continuous analog of per-cell vote support is per-cell **dispersion**. Ranks lie in
`[0, 1]`, so their MAD (median absolute deviation across members) is `≤ 0.5`; report
`pseudotime_mad = clip(2 · MAD, 0, 1)` ∈ `[0, 1]`, with support = `1 − pseudotime_mad`.
**Small-cohort caveat:** MAD is a *majority*-support metric, not full disagreement — with
3 members a `[0, 0, 1]` cell has `MAD = 0` (support 1) yet one method disagrees completely.
So v1 **also** reports the per-cell **range** (`max − min` of aligned ranks) as the
full-disagreement companion; a cell is "high support" only when both MAD and range are low.
Reported per cell.

### 9. Member selection + weak-agreement guard (the k-divergence analog)

- **BC selection:** reuse the L2 top-K selection, ranked by composite (= agreement).
  v1 default `top_k = all` for the 3 methods. **No diagnostic-baseline analog** (there
  is no unintegrated-baseline equivalent for pseudotime).
- **Weak-agreement guard** (continuous analog of the categorical k-divergence guard):
  when the cohort **mean pairwise Spearman `< 0.5`**, **report + warn** that the methods
  fundamentally disagree on the ordering, so a single consensus pseudotime may be
  ill-posed (no shared trajectory / multiple lineages). v1 is **report-only** — it does
  **not** drop sub-threshold members (mirroring k-divergence's report-only stance).

### 10. Member-side contract: canonical `pseudotime` key — already satisfied

The `continuous` `PseudotimeArtifactReader` reads a **canonical per-cell `pseudotime`**
keyed by `obs_names`. **No member-side change is needed**: `sc-pseudotime` already writes
`obs['pseudotime']` (the method-specific key normalised via `pseudotime_key`) to its
`processed.h5ad` for every method — verified at `sc_pseudotime.py:1234`, saved `:1346` —
the analog of how the integration reader keys off `representation_used`. v1 only adds the
reader that consumes it.

### 11. Flavour + artifact contract

- **Flavour:** `sc-consensus-pseudotime` (a 3-line shim over a `ConsensusSource`),
  single-cell, v1. Member skill = existing `sc-pseudotime --method {dpt,palantir,via}`
  + shared root. **Spatial-trajectory consensus** = a later **second source entry on the
  same `continuous` template**, not a new template.
- **Artifact** (`analysis://typed/<id>`, follows the existing contract): consensus
  `pseudotime` (`[0, 1]`, per cell) + per-cell `pseudotime_mad` dispersion + the member
  pairwise-Spearman agreement table + `member_scores.csv` (composite = agreement;
  intrinsic n/a) + a weak-agreement section in the report + the **mandatory first-line
  banner `[A: Verified consensus]`** (untouchable). `provenance = typed`.

### 12. Boundary vs the reserved `rank` template

`continuous` and the reserved `rank` (RRA, for DE) stay **distinct templates** despite
both touching "ranks". `continuous` aggregates a **per-cell scalar field** where every
member has a value at every cell (rank-normalised, then averaged). `rank` (RRA)
aggregates **ranked lists whose items differ per list** (e.g. top DE genes per
contrast) into a consensus ranking. Different data shape, different math; do not merge.

## Implementation surface (design, not yet built)

**Reused as-is** (domain-neutral, verified present): only `top_k_by_score` from
`scoring.py` (the rest — `MemberScore`, `score_all_members`, `cross_nmi_mean`,
`max_class_frac`, `n_clusters` — is categorical); the `[A: Verified consensus]` banner +
`analysis://typed/<id>` namespace (`report.py` / `dispatch.py`, keyed on
`provenance_of(template)`); `fan_out` + `MIN_CONSENSUS_MEMBERS=2` (`workflow` / `team.py`);
and the member-side canonical `obs['pseudotime']` — **already written by `sc-pseudotime` for
every method** (`sc_pseudotime.py:1234`, saved `:1346`), so there is **no member-side change**.

**New code to build**: the `run_continuous_consensus` driver + a `TEMPLATES['continuous']`
entry; a `ContinuousMemberScore` / `ContinuousConsensusRun` + continuous scoring
(`composite = α · mean_pairwise_spearman`, explicit `ScoreConfig(alpha=1.0, beta=0.0)`, no
`n_clusters`); continuous operators (`median` default, `weighted`) + the rank-normalise /
re-rank helpers, the direction safeguard, and per-cell MAD + range support; a
`PseudotimeArtifactReader` (`obs['pseudotime']` by `obs_names`; `read_intrinsic_quality()` →
`0.0`); a method-selection `MemberPlanner` (dpt/palantir/via) that **rejects a missing root**;
a `format_continuous_report` (the existing `report.py` hard-codes clusters / cross_NMI / base
clusterings); a **template-aware `run.py` CLI** (`--operator median|weighted`, root params,
continuous output — today it only accepts `kmode|weighted|lca` and prints `clusters_returned`);
and the `sc-consensus-pseudotime` `ConsensusSource` row + 3-line shim.

## Required implementation hardening

Edge cases the driver MUST handle (folded in from a Codex review, 2026-06-17):
- **Rank formula:** consensus rank = `(average_rank_1based − 1) / (n_cells − 1)`, ties →
  average rank; guard `n_cells < 2`.
- **Degenerate members:** finite-check on read; **drop whole** any member whose pseudotime is
  constant / has `< 2` unique values / is non-finite (Spearman is undefined there). If `< 2`
  members survive, **fail loud** (respects `MIN_CONSENSUS_MEMBERS = 2`).
- **Deterministic anchor tie-break:** when two members tie on mean `|ρ|`, break by lowest
  member name/index so the run is reproducible.
- **Non-negative `weighted` weights:** the composite (row-mean Spearman) can be negative and a
  negative-weighted mean of pseudotimes is meaningless — map `weight = max(agreement, 0)` (or
  `(agreement + 1) / 2`), then normalise; if all weights are 0, fall back to `median`.
- **Surface the worst pair:** the weak-agreement guard's cohort mean can hide one bad pair, so
  also report `min_pairwise_spearman` + the offending member pair(s).
- **Root enforcement lives in the flavour:** the `sc-consensus-pseudotime` planner rejects a
  run with no shared root — `sc-pseudotime` itself does not hard-require one.

## Consequences

### Positive
- Single-cell consensus now answers a meaningful **continuous** robustness question
  (does pseudotime ordering survive across method assumptions), with the gauge problem
  solved correctly (rank-normalisation), not papered over.
- Per-cell `pseudotime_mad` gives **ordering-uncertainty quantification** for free — a
  genuinely useful output the single-method skills lack.
- Adding the template is one `TEMPLATES` entry + one driver + one `ConsensusSource`;
  the categorical / spatial paths and all contracts are untouched.
- v1 is honest: agreement-only, no unvalidated intrinsic, after the ADR 0029 lesson.

### Negative / costs
- **No member-side change** — canonical `obs['pseudotime']` is already written by
  `sc-pseudotime`; all cost is consensus-side (new driver/scoring/reader/report — see
  *Implementation surface* and *Required implementation hardening*).
- No over-confidence guard beyond agreement (the `weighted` operator's conformity bias
  is accepted-and-recorded, not solved); the weak-agreement guard is report-only.
- Member-method stochasticity (Palantir/VIA) → reproducible **within tolerance**, not
  bit-identical (documented, as scVI is for integration).

### Deferred
- **A validated GT-free intrinsic axis** for pseudotime (graph-smoothness the
  candidate) — must pass the ADR 0029 GT-validation gate before it scores; until then
  `β = 0`.
- **Multi-lineage members** (`slingshot_r` / `monocle3_r` / `cellrank`) and the
  branching **trajectory topology** consensus (a future `trajectory` template).
- **Spatial-trajectory** consensus as a second `continuous` source entry.
- **Root consensus** (v1 requires a shared user-specified root; inferring/voting the
  root is future).
