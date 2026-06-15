# sc-consensus-clustering — Output Contract

Written by the shared consensus runtime (`omicsclaw/runtime/consensus/`), not by
the shim. The artifact shape is common to every consensus flavour; only the
fanned-out member skill differs (`sc-clustering` here).

## Output directory layout

```
<output>/
├── consensus_labels.tsv        # cell_id -> consensus_<operator> label
├── member_scores.csv           # ADR 0011 composite score per surviving member
├── member_intrinsic_panel.csv  # per-member intrinsic breakdown (silhouette)
├── cross_method_nmi.csv         # square cross-method NMI matrix
├── cross_method_nmi.png         # heatmap of the NMI matrix
├── plan.json                    # resolution sweep, chosen operator, audit trail
├── member_<name>/...            # per-member sc-clustering artifacts (passthrough)
├── report.md                    # report; starts with the [A: Verified consensus] banner
└── result.json                  # machine-readable run summary
```

## Key files

| File | Contents |
|---|---|
| `consensus_labels.tsv` | one row per cell: `cell_id`, `consensus_<operator>` |
| `member_scores.csv` | per-surviving-member composite score (`alpha·cross_NMI + beta·silhouette`) |
| `cross_method_nmi.csv` | symmetric NMI matrix over the member labellings |
| `plan.json` | resolution × method grid, filtered members, chosen operator, rationale |
| `report.md` | human report; the leading `[A: Verified consensus]` banner is non-configurable |
| `result.json` | summary: surviving members, operator, cluster count, run id |

`report.md` and `result.json` are framework-standard. All paths are produced by
the runtime; this is a `type: workflow` shim, so the per-file substring contract
check is delegated to the runtime rather than enforced against the shim.
