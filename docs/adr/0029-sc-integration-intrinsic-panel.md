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
| `ilisi_norm` | batch-neighbourhood mixing | `[1, n_batches]` | `(iLISI−1)/(n_batches−1)` | higher better | **0.50** |
| `knn_preservation_norm` | within-batch structure preserved vs `X_pca` | `[0, 1]` | identity | higher better | **0.50** |
| `batch_asw_norm` | global batch separation | `[-1, 1]` | `1−|ASW|` | higher better | 0 (diagnostic) |
| `cluster_asw_norm` | label compactness | `[-1, 1]` | `(ASW+1)/2` | higher better | 0 (diagnostic) |

**Why these two scored axes.** A pure batch-mixing panel rewards
*over-integration* — a method that mashes all cells together maximises iLISI but
destroys biology. We have no GT cell types for a bio-conservation metric, but we
do have an external structural reference: `X_pca`. `knn_preservation_norm` is the
fraction of each cell's **within-batch** `X_pca` nearest neighbours that survive
in the integrated embedding. Within-batch ⇒ no batch effect between the cells ⇒ a
clean biological-structure signal, and the reference is `X_pca` (not the member's
own labels), so it is **not circular** — unlike a same-label silhouette, which is
why `cluster_asw` is demoted to a reported diagnostic. Mixing (iLISI) balanced
against within-batch preservation penalises both over- and under-integration —
the scIB batch-removal-vs-bio-conservation trade-off, done without GT.

**Comparability.** Each metric is direction-aligned and mapped to `[0, 1]` by its
**theoretical** range — never a data-snooped threshold. iLISI is undefined for a
single batch (the panel then drops it; an integration consensus on one batch is a
misconfiguration and the driver warns). Combined as a weighted mean over the
metrics that computed; a failed metric is dropped and weights renormalise; if
none compute, intrinsic is `0.0`. Per-metric values are written to
`member_intrinsic_panel.csv`.

**Weights are experimental.** `{ilisi: 0.5, knn_preservation: 0.5}` are explicit
knobs (like ADR 0011's `α`/`β`), recorded in `plan.json` — **not** empirically
calibrated. They are presented as a relative ranking aid, not a validated score.

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
- **Empirical weight calibration and scientific validation on real multi-batch
  data.** Synthetic pseudo-batches are CI smoke only (they favour methods that
  undo the synthetic shift); the panel stays **experimental** until validated on
  a real multi-donor / multi-technology dataset with held-out labels, including
  over-integration negative controls (rare population in one batch only). Until
  then the score is a relative ranking, not a verified quality measure.
- `kBET` and graph-iLISI (need `scib_metrics`, not installed) and a
  graph-only-member panel path (BBKNN returns `X_pca` + a rebuilt graph, so it is
  excluded from the default set) are future axes — addable by adding a metric to
  `integration_panel.py` and a weight.
- k-divergence **downweighting/filtering** (v1 only reports + warns) and a
  one-command multi-embedding prep helper are deferred.
