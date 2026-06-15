# sc-consensus-integration — Output Contract

Written by the shared consensus runtime (`omicsclaw/runtime/consensus/`), not by
the shim. The artifact shape is common to every consensus flavour; only the
fanned-out member skill differs (`sc-integrate-cluster` here).

## Output directory layout

```
<output>/
├── consensus_labels.tsv        # cell_id -> consensus_<operator>, support, entropy
├── member_scores.csv           # composite score + per-member n_clusters
├── member_intrinsic_panel.csv  # iLISI / kNN-preservation per member (ADR 0029)
├── cross_method_nmi.csv         # square cross-method NMI matrix
├── cross_method_nmi.png         # heatmap of the NMI matrix
├── plan.json                    # members, operator, experimental panel weights
├── member_<name>/...            # per-member sc-integrate-cluster artifacts
├── report.md                    # report; [A: Verified consensus] banner + k-divergence section
└── result.json                  # machine-readable run summary
```

## Key files

| File | Contents |
|---|---|
| `consensus_labels.tsv` | per cell: `cell_id`, `consensus_<operator>`, `support`, `entropy` |
| `member_scores.csv` | per-surviving-member composite score + `n_clusters` |
| `member_intrinsic_panel.csv` | per-member `ilisi_norm`, `knn_preservation_norm`, `batch_asw_norm`, `cluster_asw_norm` |
| `cross_method_nmi.csv` | symmetric NMI matrix over the member labellings |
| `plan.json` | members, chosen operator, experimental intrinsic-panel weights |
| `report.md` | human report; leading `[A: Verified consensus]` banner is non-configurable; includes a k-divergence section |
| `result.json` | summary: surviving members, operator, cluster count, run id |

`report.md` and `result.json` are framework-standard. All paths are produced by
the runtime; this is a `type: workflow` shim, so the per-file substring contract
check is delegated to the runtime rather than enforced against the shim.
