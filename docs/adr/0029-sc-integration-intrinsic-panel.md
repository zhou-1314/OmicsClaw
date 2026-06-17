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

### 5. Diagnostic baseline is excluded from the consensus vote (B2)

The unintegrated `method=none` baseline is a **reference control** — it exists to
expose batch-artifact clusters by comparison, not to compete as an integration
method. On panc8 it had the worst cell-type recovery (ARI 0.314) and the most
clusters (k=34, batch artifacts), yet a near-top composite (its cross-NMI is
inflated by the shared Leiden algorithm), so voting it as an equal **dragged the
consensus below its own best members** (ARI 0.425/0.482 vs harmony 0.532,
scanorama 0.579) and inflated the consensus cluster count (k≈33). The driver
therefore **excludes diagnostic baselines from the consensus vote by default**
(generic mechanism: `run_typed_consensus(non_voting_members=...)`; `run.py`
derives it from `method=none` members): the baseline is still fanned out, scored,
paneled and reported (its row carries `selection_reason = "baseline (diagnostic;
excluded from consensus vote)"`), but the operator votes over the integration
members only. `--vote-baseline` opts it back in. A guard re-includes the baseline
if excluding it would leave `< MIN_CONSENSUS_MEMBERS` voters. On panc8 —
recomputing kmode on the cached member labels to isolate the operator effect (not
a fresh end-to-end run) — excluding the baseline raises the consensus from ARI
**0.425 → 0.532** (matching the integration members) and drops the consensus `k`
from 33 to 19. A fresh run is needed before quoting this as end-to-end performance.

## Consequences

### Positive
- Single-cell consensus answers a meaningful question (robustness across
  integration methods) and exposes batch-artifact clusters via the unintegrated
  baseline — by comparing the baseline's clustering to the consensus (it is
  reported but, after B2, does not vote, so per-cell `support` reflects agreement
  among the *voting* integration members only).
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
- **scVI is slow** (~10-15 min on ~15k cells), well over the 600s CPU per-member
  default — `--include-scvi` therefore raises the default `--timeout` to
  `SCVI_DEFAULT_TIMEOUT_SECONDS` (1800s) and warns (B4); a member that still
  exceeds it is dropped by timeout (status `timeout`, distinct from a
  missing-dependency `failed`), and the report adds a `--timeout` hint. Increase
  `--timeout` further for very large datasets.
- iLISI (harmonypy) + per-batch kNN add cost per member; acceptable for the
  typical small fan-out.

### Deferred
- **A validated GT-free structure / bio-conservation axis.** The original
  `knn_preservation` counterweight was invalidated on panc8 (see Amendment) and
  demoted to a diagnostic, so the score no longer penalises over-integration.
  Graph connectivity (scIB-style: per-cluster connected-component fraction of the
  kNN subgraph) was the leading candidate. **Investigated and closed as not-viable
  — see the Amendment "GT-free structure / bio-conservation axis" (2026-06-16):**
  every GT-free graph-connectivity form (and every other GT-free bio-conservation
  metric surveyed, including cell-cycle conservation) either anti-correlates with
  GT recovery, is redundant with iLISI / `cross_NMI`, or needs ground-truth labels.
  The panel stays iLISI-only; do not re-attempt this without a batch-independent
  biological reference.
- `kBET` and graph-iLISI (need `scib_metrics`, not installed) and a
  graph-only-member panel path (BBKNN returns `X_pca` + a rebuilt graph, so it is
  excluded from the default set) are future axes — addable by adding a metric to
  `integration_panel.py` and a weight.
- k-divergence **downweighting/filtering** (v1 only reports + warns) and a
  one-command multi-embedding prep helper are deferred.
- **Hard timeout enforcement.** B4 raises the per-member *timeout value* for scVI,
  but the generic L1 `fan_out` marks a member `timeout` without reliably killing
  its subprocess (`asyncio.wait_for` cancelling an `asyncio.to_thread` does not
  interrupt the blocking `run_skill`; the timeout branch deliberately does not set
  the shared cancel event). A genuinely hung GPU member can therefore keep running
  after being recorded as timed-out. Fixing this needs a per-step cancellation
  signal in the L1 runtime (affects every flavour + the `team.py` back-compat),
  so it is deferred; for now prefer a correct `--timeout` over relying on the kill.

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

## Amendment (2026-06-16): GT-free structure / bio-conservation axis — investigated, no viable independent axis found; iLISI-only confirmed

The Deferred item "a validated GT-free structure / bio-conservation axis (graph
connectivity, the leading candidate)" was investigated end-to-end: the specific
graph-connectivity candidate, plus a literature sweep for **any** GT-free metric
that could serve as an independent bio-conservation counterweight to iLISI. Every
candidate was prototyped on the cached panc8 members (`none`/`harmony`/`scanorama`/
`scvi`) and Spearman-correlated against per-member GT ARI, with redundancy checks
against the two signals the composite already scores: batch mixing (iLISI) and
cross-method partition agreement (cross_NMI, the `α` term). **Outcome: no GT-free
metric is a usable independent scored axis. The panel stays iLISI-only.**

### Graph connectivity (the named candidate) — rejected

scIB graph connectivity (per-label largest-connected-component fraction of the
embedding kNN graph) only tracks GT recovery when computed on **ground-truth
cell-type labels** (`gc_gt`, Spearman `+0.4` across all `k∈{3..30}`) — which are
unavailable at voting time. Every GT-free label set fails:
- **member's own Leiden clusters** (the candidate proposed for this ADR): **circular**
  — Leiden clusters are connected components of the very graph being measured, so the
  metric saturates at `1.000` (the OR-symmetrised graph over-connects ~4.2× vs mutual
  kNN); desaturating it (mutual kNN) flips it to `rho=-1.0` (anti-correlated).
- **external surrogate labels** (baseline Leiden, or fresh k-means on `X_pca`):
  anti-correlate (`-0.4..-1.0`), exactly like `knn_preservation` — they encode the
  batch-contaminated pre-integration partition.
- **whole-graph connectivity**: degenerate (one giant component) or sign-unstable.
- **cross-method labels** (score a member's graph with *other* members' Leiden
  labels — non-circular, GT-free): positively correlates (`+1.0`), but is **redundant
  with `cross_NMI`** (Spearman `+0.8`, Pearson `~0.7` between them; `cross_NMI` alone
  already scores `+0.8` vs ARI), so it double-counts the `α` term and adds a
  conformity bias (penalises a correct-but-minority member). Not independent.

### Literature sweep for any other GT-free axis — only one structural candidate, and it fails on panc8

Across the scIB taxonomy (Luecken 2022, *Nat Methods*; theislab/`scib`,
YosefLab/`scib-metrics`), topology/manifold-preservation metrics, recent (2022–2025)
reference-free integration evaluators, and biological-anchor proxies, GT-free metrics
fall into three buckets — two of which are disqualified by construction:
- **redundant with batch mixing (iLISI):** PCR(batch), kBET, silhouette(batch),
  whole-graph connectivity, BRAS, CiLISI — all decompose to batch-variance removal.
- **need ground-truth labels:** cLISI / graph-cLISI, cell-type ASW, NMI/ARI-vs-truth,
  isolated-label F1/silhouette, trajectory conservation (needs a root/lineage), kBET-
  and graph-connectivity-per-label.
- **reference-trapped (anti-correlate like `knn_preservation`):** trustworthiness /
  continuity / co-ranking, RTD, DEMaP, scIB-E's Jaccard kNN-overlap and Corr-MSE —
  all reference the pre-integration `X_pca`, which is batch-contaminated, so
  "preserving structure" rewards preserving the batch effect.

The **one** GT-free metric that is embedding-deployable *and* structurally independent
of both iLISI and `cross_NMI` is **cell-cycle conservation** (scIB `cell_cycle`):
PC-regression of a known biological covariate's (S/G2M score) variance, embedding
vs `X_pca`. It is the bio-anchor idea — reward retaining real biological variance
while removing batch variance — and unlike `knn_preservation` it tolerates legitimate
re-organisation. We prototyped it with clean **full-transcriptome** cell-cycle scores
(panc8's 2000-HVG subset only retains 8/42 S + 17/51 G2M genes, so HVG-based scoring
is unreliable; scored instead on the 34,363-gene `norm_data`). Result: it
**anti-correlates on panc8** (`rho=-0.4`; `cc_cons` = 1.000/0.551/0.489/0.451 for
`none`/`harmony`/`scanorama`/`scvi`). Mechanism: panc8 is resting pancreatic islet
tissue (cell-cycle variance ≈1% of `X_pca` variance, mostly batch-confounded), and the
cc-explained variance is removed **monotonically** as integration strengthens
(`cc_after_S` 0.0104→0.0042→0.0030→0.0031) — i.e. the weak cell-cycle signal is
entangled with batch and correctly removed, so "conservation" tracks *under*-correction.
A fair test of cell-cycle conservation needs a **proliferating, cell-cycle-active**
multi-batch dataset (e.g. immune/bone-marrow); even then it would be valid only on
proliferating tissue and is therefore unsuitable as a *default* axis (it inverts on
resting tissue, as panc8 shows). Modern `scib-metrics` has also dropped cell-cycle/
HVG/PCR conservation entirely, and no source standalone-validates it as a monotone
tracker of per-method ARI.

### The unifying reason

Every GT-free "structure/biology preservation" metric references **pre-integration
structure** (X_pca neighbourhoods, cell-cycle variance, cell-cell correlations). In
real multi-batch data that reference is itself **batch-confounded**, so rewarding its
preservation rewards preserving the batch effect → anti-correlation (the same failure
that demoted `knn_preservation`). The only signals that are clean *without* GT labels
are batch mixing (iLISI) and cross-method agreement (`cross_NMI`) — both already in
the composite. An independent GT-free bio-conservation axis would need a biological
reference that is genuinely batch-independent, which the current literature does not
supply in an embedding-deployable form.

### Decision

Keep the panel **iLISI-only**; over-integration remains *flagged* (not scored) via
the `knn_preservation` diagnostic. This Deferred item is **closed as not-viable** for
a default scored axis — do not re-attempt graph connectivity or a pre-integration-
referenced preservation metric. Remaining untested long-shots, recorded with their
hard caveats rather than pursued:
- **Cross-batch topology agreement** (IMD/MSID, Tsitsulin ICLR 2020) — the only
  candidate whose reference is neither `X_pca` nor per-cell batch labels (it compares
  the manifold *shapes* of different batches within a member). Plausible escape from
  the trap, but unproven as an integration-QC axis, needs a ~80-line spectral
  reimplementation, and risks re-encoding iLISI (congruent batch shapes can also arise
  from over-mixing).
- **RBET** (Comm Biol 2025) — overcorrection-aware, GT-free, but operates on
  *corrected gene-space expression*, which scVI (latent-only) does not emit.
- **Opt-in GT bio-conservation axis** — when an upstream reference annotation exists
  (e.g. after `sc-cell-annotation`), `gc_gt` / cLISI / NMI-vs-ref are validated and
  could be added as an *optional* scored axis. Out of scope for the unsupervised
  default panel.
