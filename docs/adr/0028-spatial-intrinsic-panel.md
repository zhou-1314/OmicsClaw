# Multi-metric, normalized spatial intrinsic-quality panel for BC ranking (amends ADR 0011)

> Status: **Accepted** (2026-06-15). Amends the intrinsic-quality input of the
> ADR 0011 composite score for spatial-domains; leaves the `α·cross_NMI +
> β·intrinsic` formula and all other consensus contracts unchanged.

## Context

ADR 0011's composite member score is `α·cross_NMI + β·intrinsic`. The
`intrinsic` term is a single per-member scalar read by each flavour's
`MemberArtifactReader`:

- `spatial-domains` → `mean_local_purity` (mean fraction of `k=8` spatial
  neighbours sharing a spot's domain label).
- `sc-clustering` → `silhouette_score`.

A spatial **domain** clustering cannot be judged well from one angle:
`mean_local_purity` only measures 1-hop label homogeneity. Two clusterings with
the same purity can differ sharply in anomaly rate or multi-scale spatial
organisation. The runtime already ships three unsupervised (labels + coords
only) spatial-domain metrics in `runtime/consensus/spatial_metrics.py` — used
only for the benchmark panel, not for scoring.

A survey of the nichecompass benchmarking suite found its metrics are mostly
ground-truth/cell-type based (CAS, CLISIS, CNMI, CARI, CASW, CLISI) or compare a
**learned latent** to space (GCS, NASW) — neither transfers to the consensus
label-voting layer, which has no latent and no cell-type GT. The genuinely
intrinsic, label+coords-only spatial-domain signals are exactly CHAOS, PAS, and
MLAMI, which OmicsClaw already implements.

## Decision

For spatial runs, replace the single `mean_local_purity` intrinsic with a
**normalized multi-metric panel** (`runtime/consensus/spatial_panel.py`):

| Metric | Angle | Native range | Direction |
|---|---|---|---|
| `chaos` | 1-hop spatial coherence | `[0, 1]` | higher better |
| `pas`   | abnormal-spot rate | `[0, 1]` | **lower** better |
| `mlami` | multi-scale spatial-graph AMI | `[0, 1]` (AMI, clipped at 0) | higher better |

**Comparability (the crux).** Each metric is direction-aligned (so higher =
better) and mapped to `[0, 1]` by its **theoretical** range — never a
data-snooped or hallucinated absolute threshold (per the project safety rule).
`chaos`/`mlami` are already `[0, 1]` higher-better; `pas` is flipped (`1 - pas`);
`mlami` (an AMI) is clipped at `0` so a worse-than-chance clustering earns no
credit. The normalized axes are combined as a weighted mean:

```
intrinsic = Σ wᵢ·normᵢ / Σ wᵢ            (over the metrics that computed)
default weights = {chaos: 0.4, pas: 0.2, mlami: 0.4}   # topology-biased
```

Weights are explicit knobs (like ADR 0011's `α`/`β`), not derived from data; a
failed metric is dropped and the weights renormalise over the survivors (so a
single metric error does not bias the score); if none compute, intrinsic is `0.0`.

**Scope is by the flavour's declared spatial domain (`source.domain == "spatial"`),
plus coords-presence.** The driver activates the panel only for a spatial-domain
flavour whose shared input AnnData also carries `obsm['spatial']` (reindexed to
the gathered observation ids). Gating on the domain — not merely on
coords-presence — is deliberate: an `sc-clustering` run on a *spatially-annotated*
single-cell AnnData has coordinates but must keep its own `silhouette_score`
intrinsic, because the panel measures spatial-domain coherence, not cell-cluster
compactness. A spatial flavour with missing/misaligned coords falls back to the
reader signal. `--no-spatial-panel` opts out entirely.

The scoring layer is unchanged and stays metric-agnostic: it still takes one
`intrinsic_quality: float`. The panel scalar simply becomes that float for
spatial runs. The per-metric breakdown is written to a new artifact
`member_intrinsic_panel.csv` (member, chaos, pas, mlami, intrinsic_panel).

## Consequences

### Positive
- Spatial-domain quality is judged from three orthogonal angles instead of one.
- Reuses existing, attributed metric implementations; no new GT requirement.
- Comparability handled without hallucinated thresholds; weights are auditable.
- Back-compat: non-spatial flavours unchanged; stub/coords-less tests unchanged
  (no `obsm['spatial']` → panel skipped). `--no-spatial-panel` restores the
  exact prior `mean_local_purity` scoring.

### Negative / costs
- `member_scores.csv` `intrinsic` column and downstream composite scores change
  for spatial runs (intended: the prior single-metric scoring was the deficiency
  this ADR fixes). A new `member_intrinsic_panel.csv` artifact appears.
- `mlami` runs a seeded Leiden sweep per member (~5–10 s each); CHAOS/PAS are
  near-free. Acceptable for the typical ≤10-member fan-out.

### Deferred
- Empirical weight calibration on an annotated corpus, and extra GT-free axes
  (spatial ASW, Moran's I), are future work — the panel interface admits them by
  adding a metric to `spatial_panel.py` and a weight.
- `mean_local_purity` is retained as the reader signal (panel-disabled fallback)
  and is now ≈ the panel's `chaos` axis; it is no longer a distinct scoring input.
