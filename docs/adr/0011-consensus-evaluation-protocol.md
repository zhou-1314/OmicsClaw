# Composite member score (cross-method NMI × intrinsic quality × class-imbalance hard filter) + DLPFC 151673 hero benchmark + self-consistency unit tests as the v1 consensus evaluation contract

## Status

Accepted (2026-05-18). Amended (2026-05-18) per the metric-panel
grilling session: ARI alone is task-targeted-replaced by a panel
(ARI + AMI + V-measure + MLAMI for spatial), self-consistency moved
from ARI to AMI, ported MLAMI/CHAOS/PAS added to the runtime layer.
See "Metric panel rationale" below.

## Context

ADR 0010 lands a typed-vs-narrative consensus runtime, with
post-run base-clustering (BC) selection as the load-bearing
expert-in-the-loop step. Two questions remain operational:

1. **Which members go into "top-K by score" when the CLI user accepts
   the default, or when Desktop/Channel falls back to non-interactive
   mode?** A wrong default propagates straight into the verified
   consensus output. "Top-K by mean cross-method NMI" has a known
   failure mode: five low-quality methods that agree with each other
   score high.

2. **How does v1 demonstrate the consensus is actually better than any
   single method?** Without a defensible evaluation, the headline claim
   ("first to operationalize SACCELERATOR's expert-in-the-loop with
   an LLM") collapses to "we built a wrapper".

### Existing signals already in the tree

The grilling session that produced ADR 0010 also discovered that the
two ranking signals SACCELERATOR's `consensus/02_BC_ranking` combines
are *already emitted* by every current OmicsClaw member:

- **Cross-method consistency**: produced on the fly by the operator
  layer (pairwise NMI/ARI matrix of N members' labels — pure scipy,
  ~10 lines).
- **Intrinsic quality, spatial**: `domain_local_purity` per spot
  averaged into `mean_local_purity`
  (`spatial_domains.py:148, 178, 472`).
- **Intrinsic quality, scRNA**: `silhouette_score` per resolution
  (`sc_cluster.py:18, 248-295`).
- **Class-imbalance signal**: trivially computable from the labels
  themselves; SACCELERATOR uses `max_class_frac > 0.8` as the hard
  filter (`consensus/02_BC_ranking/BC_ranking.r`).

The four pieces are present; what's missing is the formula combining
them and the regression evidence that the combination works.

## Decision

### Composite score (member ranking input for top-K)

```python
def member_score(
    member_labels: np.ndarray,
    sibling_labels: list[np.ndarray],
    intrinsic_quality: float,
    *,
    alpha: float = 0.6,
    beta: float = 0.4,
    max_class_frac_cap: float = 0.8,
) -> float:
    """Composite ranking score, SACCELERATOR-style.

    Returns -inf if class imbalance exceeds the hard-filter cap, so the
    member is automatically excluded from any top-K selection.
    """
    counts = np.bincount(_normalize_labels(member_labels))
    if counts.max() / counts.sum() > max_class_frac_cap:
        return float("-inf")
    cross = float(np.mean([
        normalized_mutual_info_score(member_labels, other)
        for other in sibling_labels
    ]))
    return alpha * cross + beta * float(intrinsic_quality)
```

The `intrinsic_quality` argument is keyed by the member's skill:
- `spatial-domains` → `mean_local_purity` from `summary.json`
- `sc-clustering` → `silhouette_score` from `clustering_summary.csv`
- Future skills must register an `intrinsic_quality_key` in their
  `parameters.yaml` to be added to `TYPED_CONSENSUS_REGISTRY`.

`alpha = 0.6, beta = 0.4, max_class_frac_cap = 0.8` are the
SACCELERATOR-paper defaults. All three are exposed as CLI flags
(`--alpha`, `--beta`, `--max-class-frac`) on `consensus-domains` and
`sc-consensus-clustering` for sensitivity analysis without re-running
fan-out.

### Evaluation chair's degree of freedom

The default ranking is deterministic. `--llm-judge` opt-in lets the
evaluation-chair LLM see the full composite-score table plus the
cross-method NMI matrix and **veto up to two members or rebalance α/β
within ±0.2**. The chair cannot synthesize scores out of thin air —
its inputs are the deterministic numbers above. This is the
"reviewer with statistical reasoning support" stance: the chair has
final say, but it must justify divergence from the formula in the
written report.

### v1 evaluation contract

Two artifacts must ship with the v1 PR. Together they answer the
"is the consensus better than the best single method" question.

**(i) Self-consistency unit tests** — `tests/runtime/consensus/`:

| File | What it asserts |
|---|---|
| `test_categorical_operators.py` | kmode/weighted Python output matches hand-computed deterministic reference values on synthetic inputs. v1 uses simplified per-row mode + weighted-majority operators rather than bit-exact ports of SACCELERATOR's `diceR::k_modes` (iterative refinement) or EnSDD (NMF + Leiden); the headline contribution (LLM evaluation chair + verified/exploratory boundary) is independent of operator-level equivalence with the R reference. Determinism is locked-in: same input → same output, no RNG; `seed` on kmode/weighted is recorded for traceability only (LCA seed does control the underlying R EM init). |
| `test_alignment.py` | `scipy.optimize.linear_sum_assignment` produces a permutation that maximises co-occurrence of source labels against the reference; verified against hand-constructed permutations |
| `test_member_scoring.py` | Class-imbalance hard filter at 0.8 excludes the right members; α/β weighting matches hand-computed expected values; mismatched-shape sibling labels raise `ValueError` (no silent skip) |
| `test_team_runtime.py` | 5 parallel members under `asyncio.gather`; one synthetic crash leaves the other 4 producing a valid consensus; one timeout marks only that member as `timeout` and does **not** propagate `cancel_event` to siblings (ADR 0010 "≥2 survivors continue"); `cancel_event` set from outside still cancels all in-flight members via the ADR 0009 `killpg` chain |
| `test_self_consistency.py` | On synthetic noisy clusterings perturbed across 10 seeds, **stdev(ARI(consensus_seed_i, consensus_seed_0)) ≤ stdev(ARI(best_member_seed_i, best_member_seed_0)) + tolerance**. Synthetic-data harness is used instead of vendored DLPFC h5ad — the assertion is on operator stability, not on real-tissue accuracy. |

The self-consistency test is the regression baseline — a future change
that degrades consensus stability vs. the best single method fails CI.

**(ii) DLPFC 151673 hero benchmark** —
`examples/consensus_benchmark/`:

| File | Purpose |
|---|---|
| `README.md` | One hero figure, one paragraph |
| `run_dlpfc_151673.py` | Pulls the DLPFC sample 151673 from SACCELERATOR `data/` (the most-cited Visium sample with manual layer annotations), fans out 5 members (BANKSY / GraphST / SEDR / Leiden / SpaGCN), runs kmode consensus, computes ARI vs ground-truth layer labels |
| `expected_metrics.json` | Asserts `ARI(consensus, gt) >= max(ARI(method_i, gt)) - 0.02` — consensus must be no worse than the best single method, within a 2% noise floor |
| CI hook | The expected-metrics assertion runs in CI on every consensus-runtime PR |

Why DLPFC 151673 specifically: it is the only Visium sample with
manual cortex-layer ground truth that SACCELERATOR, BANKSY, GraphST,
SEDR, and STAGATE all benchmark on independently. The community
already has reference ARI numbers per method (≈0.55–0.65 range in
published tables), making divergence easy to detect.

### What v1 explicitly does not do

- Does **not** reproduce SACCELERATOR's full 28-dataset × 22-method
  benchmark. That is a v2 paper-grade effort and would block v1.
- Does **not** evaluate the B path (narrative consensus). B path is
  framed as "exploratory" and its quality is intentionally not
  quantified — only its output banner and namespace separation are.
- Does **not** publish updated weights or a learned scoring model.
  α/β stay at SACCELERATOR's published defaults until a v2
  ablation study justifies otherwise.

## Metric panel rationale (amendment, 2026-05-18)

A code-review pass surfaced that the v1 evaluation contract leaned on
**ARI alone** in three distinct places — DLPFC hero benchmark assertion,
self-consistency stability check, and the implied "score" the LLM
evaluation chair narrates. ARI has well-documented biases (preference
for few clusters, no over-merge / over-split signal, no spatial
awareness), and SACCELERATOR itself ships 17 metrics rather than one.

We adopt a **task-targeted** metric policy: the metric panel at each
use-site is the smallest set that covers what *that* axis is asking.

### Three axes, three metric sets

| Use site                    | What axis it measures           | Has GT? | Metric set                                                 |
|---                          |---                              |---      |---                                                         |
| **DLPFC hero benchmark**    | Agreement with truth + structure| ✓       | ARI + AMI + V (+ MLAMI for spatial); H, C, CHAOS, PAS report-only |
| **Self-consistency test**   | Stability across seeds          | ✗       | AMI (chance-corrected; ARI reserved for GT axis)           |
| **Composite member score**  | Member quality for BC ranking   | ✗       | α·cross_NMI + β·intrinsic — unchanged (already 2-axis)     |

The hero benchmark is the only place a panel-and-AND-pass-rule
applies. Self-consistency and member-score both target a single
deterministic signal at a time.

### Why this exact hero-benchmark panel

Three of the four hard metrics (ARI, AMI, V-measure) are sklearn
one-liners that are zero-cost to compute and cover three distinct
concerns:

- **ARI** — most-cited spatial-clustering metric; keep for community
  comparability with BANKSY / GraphST / SEDR / SpaGCN reports.
- **AMI** — chance-corrected mutual information; the de-biased
  ARI/NMI replacement. Less sensitive to cluster count than ARI.
- **V-measure** — harmonic mean of homogeneity (H) and completeness (C).
  H and C are exposed report-only so a failure direction (over-merge
  vs over-split) is immediately legible.

The fourth, **MLAMI**, is the spatial-only addition: it builds a
spatial k-NN graph, runs Leiden at multiple resolutions, and reports
the maximum AMI between the consensus labels and those spatial-graph
clusterings. Unlike CHAOS/PAS (1-hop only), MLAMI captures multi-scale
spatial coherence. Importantly, it is **unsupervised** — it does not
require the GT annotation, so it cross-checks consensus quality
against the spatial signal even when the GT itself is biased
(SACCELERATOR's published concern about DLPFC layer annotations).

CHAOS and PAS join the report-only diagnostic set: 1-hop spatial
agreement aggregates that complement MLAMI's multi-scale view.

### Why each metric is hard vs report-only

| Metric        | Tier         | Why                                                                                      |
|---            |---           |---                                                                                       |
| ARI           | hard         | Community baseline; PR must not silently regress it.                                     |
| AMI           | hard         | De-biased replacement for ARI; must not regress.                                         |
| V_measure     | hard         | Composite of H+C; sensitive to both directions of failure.                               |
| MLAMI         | hard (spatial)| Multi-scale spatial coherence; unsupervised so GT bias does not influence pass.         |
| Homogeneity   | report-only  | Diagnostic — exposes over-merge direction when consensus disagrees with GT.              |
| Completeness  | report-only  | Diagnostic — exposes over-split direction.                                               |
| CHAOS         | report-only  | 1-hop label agreement; useful for spotting salt-and-pepper outputs.                      |
| PAS           | report-only  | 1-hop abnormal-spot fraction; complement to CHAOS.                                       |

AND across the four hard metrics is the pass rule. K-of-N voting
would only mask regressions: the four hard metrics are correlated
enough that a genuinely better consensus passes all four, while a
regression usually breaks several together.

### Why ported, not depended-on

MLAMI is ported from **nichecompass v1.x** (Sebastian Birk · Carlos
Talavera-López · Mohammad Lotfollahi, BSD 3-Clause) and CHAOS/PAS are
SACCELERATOR-equivalent Python re-implementations. We **do not** add
``nichecompass[benchmarking]`` as a dependency because it pulls jax /
mlflow / scib-metrics transitively. The port lives in a single new
file ``omicsclaw/runtime/consensus/spatial_metrics.py`` (~150 lines
including attribution), depends only on numpy / scipy / scanpy /
sklearn (already in the OmicsClaw core), and preserves the BSD-3
copyright notice + disclaimer per redistribution clauses.

This mirrors the same pattern used for SACCELERATOR's R consensus
operators in ADR 0010 (vendor + attribute, do not depend).

### Updated test matrix

The test files that anchor this contract are unchanged in identity but
their assertions tighten:

| File                          | Asserts                                                                                                                                                                                          |
|---                            |---                                                                                                                                                                                               |
| `test_alignment.py`           | (unchanged) Hungarian permutation recovery on hand-constructed inputs.                                                                                                                            |
| `test_categorical_operators.py` | (unchanged) Deterministic kmode/weighted output on synthetic inputs; tiebreak is deterministic earliest-column wins; `seed` is traceability-only for these two operators.                       |
| `test_member_scoring.py`      | (unchanged) Class-imbalance hard filter; α/β weighting; mismatched-shape sibling raises ValueError.                                                                                              |
| `test_team_runtime.py`        | (unchanged) Per-member timeout does not cascade-cancel siblings; cancel_event propagates only on real user cancel.                                                                                |
| `test_spatial_metrics.py`     | **new** — MLAMI/CHAOS/PAS on synthetic data: perfect spatial alignment scores at the metric's "good" extreme; uniform random labels score at the "bad" extreme; bounded in unit interval.        |
| `test_self_consistency.py`    | **amended** — stdev across seeds via **AMI** rather than ARI. ARI is retained only for the GT-comparison sanity assertion.                                                                       |
| `test_dlpfc_benchmark.py`     | **amended** — schema validation enforces the list-of-metrics shape (`hard_metrics` + `report_only_metrics` + `pass_rule="all_hard_pass"`); dry-run reports panel sizes.                          |

### Updated `expected_metrics.json` schema

```jsonc
{
  "hard_metrics": [
    {"name": "ARI",       "rule": "noise_floor", "noise_floor": 0.02, "min_absolute": 0.45, "applies_to": "all"},
    {"name": "AMI",       "rule": "noise_floor", "noise_floor": 0.02, "min_absolute": 0.40, "applies_to": "all"},
    {"name": "V_measure", "rule": "noise_floor", "noise_floor": 0.02, "min_absolute": 0.45, "applies_to": "all"},
    {"name": "MLAMI",     "rule": "noise_floor", "noise_floor": 0.03, "min_absolute": 0.30, "applies_to": "spatial_only"}
  ],
  "report_only_metrics": ["Homogeneity", "Completeness", "CHAOS", "PAS"],
  "pass_rule": "all_hard_pass"
}
```

## Consequences

### Positive

- Top-K default is defensible: it is the SACCELERATOR-published
  formula run on inputs OmicsClaw already emits.
- A single CI assertion (`ARI(consensus, gt) >= best_method - 0.02`
  on DLPFC 151673) catches regressions before they ship.
- Self-consistency tests anchor the unit-test layer at the operator,
  not the runtime, so re-implementing `team.py` in the future does
  not invalidate algorithm tests.
- Evaluation-chair LLM scope is bounded: it cannot invent scores,
  only veto/reweight within published-default ranges. Keeps the
  "verified" path reproducible across model versions.

### Negative

- Two paper-style evaluation defaults (α/β/max_class_frac, DLPFC
  hero) freeze early. A v2 ablation that overturns them needs a
  superseding ADR.
- The DLPFC dataset must be vendored or fetched at test time.
  Vendoring adds ~50MB to the repo; fetching at test time adds
  network dependence to CI. The PR will pick one (default lean:
  fetch with a `pytest.mark.requires_network` gate; skip when
  offline; cache locally after first fetch).

## Relationship to prior ADRs

- **ADR 0010** (consensus runtime layer): this ADR specifies the
  scoring function used by `scoring.py` and the evaluation harness
  living under `tests/runtime/consensus/` + `examples/consensus_benchmark/`.
- **ADR 0009** (cancel_event wiring): the self-consistency runtime
  test `test_team_runtime.py` exercises the cancel path end-to-end —
  it is the first cross-layer regression test for that wiring.
