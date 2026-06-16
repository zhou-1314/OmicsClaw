# Integration-embedding consensus + a batch-mixing intrinsic panel (amends ADR 0011, extends ADR 0016/0028)

> Status: **Accepted** (2026-06-15). Adds the `sc-consensus-integration` flavour
> and an integration intrinsic-quality panel; generalises the driver's panel
> gate from a spatial domain-check to a per-source `intrinsic_panel` dispatch.
> Leaves the `α·cross_NMI + β·intrinsic` formula, the banner/namespace/provenance
> contracts, and all existing flavours byte-identical.

## Context

The existing single-cell consensus flavour `sc-consensus-clustering` is a
**resolution sweep of one method** (Leiden at `{0.5,0.8,1.0,1.4,2.0}`). For
single-cell data this is the wrong axis of variation, and its intrinsic term is
silently dead:

- **Wrong axis.** Members differ only in granularity (nested partitions), so
  cross-NMI is structurally high and does not discriminate quality. More
  importantly it ignores **batch effect**, which dominates real multi-sample
  data: clustering uncorrected `X_pca` of multi-sample data clusters *batches*,
  not cell types. The scientifically meaningful question is which populations are
  stable across **integration methods** (Harmony / Scanorama / scVI / …), which
  produce genuinely different embeddings and so different clusterings.
- **Dead intrinsic (the trigger bug).** Production `sc-clustering` never writes a
  `silhouette_score` row to `clustering_summary.csv` (it is computed only under
  `--resolution auto`, on a co-clustering distance, into `result.json`), so
  `ScClusteringArtifactReader.read_intrinsic_quality` fails-soft to `0.0` for
  every member and the `β·intrinsic` term vanishes. (A unit fixture fakes the
  row, so tests were green while production scored on cross-NMI alone.)

This mirrors ADR 0028's spatial situation: a single intrinsic scalar is
insufficient, and the fix is a normalized, unsupervised, multi-metric panel —
but the single-cell quantity of interest is integration quality, not spatial
coherence, and there are no ground-truth cell types at voting time (only batch
labels).

## Decision

### 1. New flavour `sc-consensus-integration` (member axis = integration representation)

Add one `CONSENSUS_SOURCES` row (ADR 0016: a new flavour needs no ADR of its
own; this ADR is warranted only by the new **verified intrinsic panel** below).
Members fan out a new self-contained member skill `sc-integrate-cluster
--method <m>` (`none`/`harmony`/`scanorama`/`scvi`), each producing a
batch-correction representation **and** clustering on it at a **fixed**
resolution, emitting the standard `sc-clustering` artifact schema (reader
reused). `none` is the unintegrated `X_pca` baseline — included so the per-cell
`support` exposes clusters that exist only before integration (batch artifacts).

Integration lives in the member skill (composing `_lib/integration.py`), not in
`sc-clustering`: integration is a separate responsibility and coupling it into
the clustering skill would bloat it. Because integration runs **inside each
member subprocess**, the representation (`X_harmony`/`X_scvi`/…) exists only in
that member's own `processed.h5ad` — the shared input does not carry it.

### 2. Integration intrinsic panel (`runtime/consensus/integration_panel.py`)

Replace the reader's single intrinsic with a normalized batch-mixing panel,
computed per member on **that member's** embedding + the batch key (no
ground-truth cell types — mirroring ADR 0028's labels-only constraint):

| Metric | Angle | Native range | Normalisation | Direction | Weight |
|---|---|---|---|---|---|
| `ilisi_norm` | batch-neighbourhood mixing | `[1, n_batches]` | `log(iLISI)/log(n_batches)` | higher better | **1.0** |
| `knn_preservation_norm` | within-batch structure preserved vs `X_pca` | `[0, 1]` | identity | higher better | 0 (diagnostic) |
| `batch_asw_norm` | global batch separation | `[-1, 1]` | `1−|ASW|` | higher better | 0 (diagnostic) |
| `cluster_asw_norm` | label compactness | `[-1, 1]` | `(ASW+1)/2` | higher better | 0 (diagnostic) |

> **Calibration amended 2026-06-16 after panc8 real-data validation** (see the
> Amendment below). The original design scored a *balanced* panel
> (`{ilisi: 0.5, knn_preservation: 0.5}`, linear iLISI). Validation against
> ground-truth cell types showed `knn_preservation` anti-correlated with recovery
> and the linear iLISI compressed the score, so the panel is now **iLISI-only,
> log-normalised**, with `knn_preservation` demoted to a reported diagnostic.

**Why iLISI is the single scored axis (revised).** The original theory was that a
pure batch-mixing panel rewards *over-integration*, so it should be balanced by a
GT-free structure metric — `knn_preservation_norm` (within-batch `X_pca` neighbour
retention; non-circular because the reference is external `X_pca`, not the
member's own labels). On panc8 that theory **failed**: `knn_preservation`
anti-correlated with cell-type recovery (`r=-0.74`) because within-batch `X_pca`
neighbourhoods carry technical variation, not only biology — a method that
legitimately reorganises the embedding to merge cell types lowers the metric. The
one axis that tracked recovery was `ilisi` (`r=+0.99`). So `ilisi` is now the sole
scored axis; `knn_preservation` is **reported** (it still flags over-integration in
the report) but does not select. A *validated* GT-free structure axis (graph
connectivity) is deferred.

**Comparability.** Each metric is direction-aligned and mapped to `[0, 1]` by its
**theoretical** range — never a data-snooped threshold. iLISI uses a **log** map
(`log(iLISI)/log(n_batches)`) rather than the linear `(iLISI−1)/(n_batches−1)`,
because real-world iLISI sits near 1 (e.g. ~1.5/5 for a decent integration) and
the linear map compresses every method into the bottom of `[0, 1]`, barely
separating good from poor integration. iLISI is undefined for a single batch (the
panel then has no scored axis → intrinsic `0.0`; an integration consensus on one
batch is a misconfiguration and the driver warns). A failed metric is dropped and
weights renormalise. Per-metric values (scored + diagnostic) are written to
`member_intrinsic_panel.csv`.

**The weight is experimental.** `{ilisi: 1.0}` is recorded in `plan.json`. iLISI
is the one axis validated against ground truth (panc8); treat the score as a
*relative mixing rank* and read `knn_preservation` alongside it to catch
over-integration. The panel does **not** itself penalise over-integration in the
score (that guard left with `knn_preservation`); this is an accepted limitation
pending a validated structure axis.

### 3. Driver panel dispatch (generalises ADR 0028's gate)

The driver's intrinsic-panel step is dispatched on a new
`ConsensusSource.intrinsic_panel` field (`"spatial"` | `"integration"` | `""`)
rather than a `domain == "spatial"` check. The spatial row is set to `"spatial"`
(behaviour byte-identical); the new row is `"integration"`. For the integration
panel the driver loads each member's embedding from its **own** `processed.h5ad`
(keyed by the `representation_used` recorded in the member's `result.json`) plus
the batch key, fail-soft per member.

### 4. k-divergence guard

Fixing the resolution makes member cluster counts comparable, but different
embeddings can still yield different `k`, and kmode/weighted Hungarian alignment
+ majority vote is only well-posed when `k` values are close. The driver records
per-member `n_clusters` in `member_scores.csv`, computes `k_range`/`k_cv`, and
**warns + reports** when `k_max/k_min > 2` (the report's interpretation note flags
that per-spot `support` may then be operator-induced rather than biological). v1
reports and warns; it does **not** downweight or filter on `k`.

## Consequences

### Positive
- Single-cell consensus answers a meaningful question (robustness across
  integration methods) and exposes batch-artifact clusters via the unintegrated
  baseline + per-cell support.
- The dead-intrinsic bug is bypassed: the driver computes the panel; it never
  reads the (missing) `silhouette_score` row.
- Adding a panel family is now a one-field change (`intrinsic_panel`), not a
  domain check — the spatial path is unchanged.
- No new GT requirement; comparability without hallucinated thresholds; weights
  auditable in `plan.json`.

### Negative / costs
- A new member skill (`sc-integrate-cluster`) and panel module. scVI is GPU and
  **stochastic** — opt-in via `--include-scvi`, GPU members should be serialised
  (`--max-parallel 1`), and the consensus is *reproducible within tolerance, not
  bit-identical* when scVI is included. `MemberScore` gains an `n_clusters` field
  (a new `member_scores.csv` column).
- iLISI (harmonypy) + per-batch kNN add cost per member; acceptable for the
  typical small fan-out.

### Deferred
- **A validated GT-free structure / bio-conservation axis.** The original
  `knn_preservation` counterweight was invalidated on panc8 (see Amendment) and
  demoted to a diagnostic, so the score no longer penalises over-integration.
  Graph connectivity (scIB-style: per-cluster connected-component fraction of the
  kNN subgraph) is the leading candidate — it measures manifold continuity rather
  than raw `X_pca` neighbour overlap and so should not punish legitimate
  reorganisation — but it must itself be validated against ground truth on
  several datasets before it scores.
- `kBET` and graph-iLISI (need `scib_metrics`, not installed) and a
  graph-only-member panel path (BBKNN returns `X_pca` + a rebuilt graph, so it is
  excluded from the default set) are future axes — addable by adding a metric to
  `integration_panel.py` and a weight.
- k-divergence **downweighting/filtering** (v1 only reports + warns) and a
  one-command multi-embedding prep helper are deferred.

## Amendment (2026-06-16): panc8 real-data validation → iLISI-only, log-normalised

The panel was validated on **panc8** (5 sequencing technologies, 14,890 cells,
13 ground-truth cell types), running `none`/`harmony`/`scanorama` and correlating
each panel metric with per-member ARI vs the held-out cell types.

Findings (each independently recomputed):
- `knn_preservation` **anti-correlated** with bio-recovery (Spearman `r=-0.74` vs
  ARI). Scanorama recovered cell types best (ARI 0.579) but got the *worst*
  `knn_preservation` (0.334) — within-batch `X_pca` neighbourhoods carry technical
  variation, so a method that legitimately reorganises the embedding to merge cell
  types across batches lowers the metric. The original balanced panel therefore
  **ranked the best integrator last**.
- `ilisi` **correlated** with recovery (`r=+0.99`).
- The linear `(iLISI−1)/(n_batches−1)` compressed real iLISI (≈1.0–1.5 over 5
  batches) into the bottom ~14% of `[0,1]`, so harmony (0.137) barely beat the
  unintegrated baseline (0.009).

Decision: **demote `knn_preservation` to a weight-0 diagnostic (B1)** and **switch
iLISI to `log(iLISI)/log(n_batches)` (B3)**.

Attribution (important — they fix different things):
- **B1 fixes the ranking.** Removing `knn_preservation` from the score is what
  un-inverts the panel: `scanorama 0.353 > harmony 0.271 > unintegrated 0.023`
  now **matches** the GT ARI ranking `0.579 > 0.532 > 0.314`.
- **B3 only changes spacing, not ranking.** `log/log` is strictly monotone in
  iLISI, so an iLISI-only score has the same member order with either map; B3's
  value is *dynamic range* — harmony is now well-separated from the baseline
  (0.271 vs 0.023) instead of compressed (0.137 vs 0.009 under the linear map).

Scope of the improvement (do not overstate): this changes the **member ranking**
in `member_scores.csv` and the **weights the `weighted` operator would use**. It
does **not** necessarily change the `kmode` consensus labels: in a default run
(3 members, `top_k=4`) all unfiltered members enter consensus regardless of rank,
and `kmode` ignores scores after selection. The ranking matters when `top_k <
n_members`, when a member is hard-filtered, or under the `weighted` operator.

Cost: the score no longer penalises over-integration (that guard left with
`knn_preservation`); over-integration is now only *flagged* — via the
`knn_preservation` diagnostic, surfaced in the report's "Intrinsic panel
diagnostics" section (high `ilisi_norm` + low `knn_preservation_norm`) and in
`member_intrinsic_panel.csv` — pending a validated structure axis (see Deferred).
If iLISI (the sole scored axis) cannot compute for a member (e.g. harmonypy
missing), that member keeps its reader intrinsic and the run warns, rather than
silently scoring a misleading 0.0. The panel remains **experimental** — validated
on one real dataset, not yet calibrated across several.
