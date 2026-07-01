# sc-integrate-cluster — Methodology

A **consensus member skill** (leaf): one self-contained *integrate + cluster*
unit fanned out by `sc-consensus-integration` (ADR 0016 / 0029). It is the
single-cell analog of `spatial-domains --method <m>`.

## Why integrate + cluster in one unit

For the integration consensus, each "member" must be a genuinely different
batch-correction *representation* clustered the same way, so that differences
between members reflect the integration method — not a different clustering
recipe. Bundling integrate + cluster into one member guarantees that, and emits
the exact `sc-clustering` artifact schema so the consensus reader
(`ScClusteringArtifactReader`) consumes it unchanged.

## Steps

1. **Baseline** — ensure an `X_pca` baseline exists (compute if absent).
2. **Integrate** — run `--method` (`none` keeps `X_pca`; `harmony` / `scanorama`
   / `scvi` produce `X_<method>`), recording the embedding key in
   `representation_used`.
3. **Cluster** — neighbour graph on `obsm[representation_used]`, then
   `--cluster-method` at the **fixed** `--resolution` (comparable cluster counts
   across members are required by the consensus operator).
4. **Emit** — `figure_data/embedding_points.csv` (labels the consensus reads),
   `figure_data/clustering_summary.csv`, `processed.h5ad` (the embedding the
   driver scores with the ADR 0029 panel), and `result.json`.

## Methods (`--method`)

| method | backend | device | obsm key | notes |
|---|---|---|---|---|
| `none` | unintegrated baseline | CPU | `X_pca` | reveals batch-artifact clusters |
| `harmony` | Harmony | CPU | `X_harmony` | fast, deterministic |
| `scanorama` | Scanorama | CPU | `X_scanorama` | needs shared genes across batches |
| `scvi` | scVI VAE | GPU | `X_scvi` | **stochastic** (reproducible within tolerance); opt-in, serialise GPU members |

## Notes

- `none` is the unintegrated baseline; clusters that appear only on `none` are
  batch artifacts the consensus is designed to flag.
- `scvi` is GPU/stochastic — opt in only when reproducible-within-tolerance is
  acceptable; serialise GPU members at the consensus level.

See `references/parameters.md` for every flag and `references/output_contract.md`
for the artifact schema.
