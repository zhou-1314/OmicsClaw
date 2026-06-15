# sc-integrate-cluster — Output Contract

Written into the member directory passed as `--output`.

## Output directory layout

```
<member_dir>/
├── figure_data/
│   ├── embedding_points.csv      # cell_id, embedding_key, coord1, coord2, <cluster-method>, batch
│   └── clustering_summary.csv    # method, representation_used, cluster_method, n_cells,
│                                 #   n_clusters, resolution, batch_key, n_batches
├── processed.h5ad                # obsm[representation_used] + obsm["X_pca"] + labels + batch key
└── result.json                   # standardised result envelope
```

## Key files

| File | Contents |
|---|---|
| `figure_data/embedding_points.csv` | per-cell 2-D embedding + the cluster label the consensus `ScClusteringArtifactReader` reads |
| `figure_data/clustering_summary.csv` | run summary row: `method`, `representation_used`, `n_clusters`, `resolution`, `batch_key`, `n_batches` |
| `processed.h5ad` | AnnData with `obsm[representation_used]` — the embedding the consensus driver scores with the integration intrinsic panel (ADR 0029) |
| `result.json` | framework-standard envelope; `summary.representation_used` / `summary.n_clusters` |

`processed.h5ad` and `result.json` are framework-standard. The two `figure_data/`
CSVs are exactly the `sc-clustering` artifact schema, which is why the consensus
reads this member unchanged.
