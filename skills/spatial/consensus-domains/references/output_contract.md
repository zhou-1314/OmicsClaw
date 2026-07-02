# consensus-domains — Output Contract

Written by the shared consensus runtime (`omicsclaw/runtime/consensus/`), not by
the shim. The artifacts are identical in shape across consensus flavours; the
flavour only changes the member skill that is fanned out.

## Output directory layout

```
<output>/
├── consensus_labels.tsv        # observation -> consensus_<operator> label
├── member_scores.csv           # ADR 0011 composite score per surviving member
├── member_intrinsic_panel.csv  # per-member intrinsic-panel breakdown (ADR 0028)
├── cross_method_nmi.csv         # square cross-method NMI matrix
├── cross_method_nmi.png         # heatmap of the NMI matrix
├── plan.json                    # planned members, chosen operator, audit trail
├── member_<name>/...            # per-member spatial-domains artifacts (passthrough)
├── report.md                    # report; starts with the [A: Verified consensus] banner
└── result.json                  # machine-readable run summary
```

## Key files

| File | Contents |
|---|---|
| `consensus_labels.tsv` | one row per spot: `observation`, `consensus_<operator>` |
| `member_scores.csv` | per-surviving-member composite score (`alpha·cross_NMI + beta·intrinsic`) |
| `member_intrinsic_panel.csv` | per-member `chaos` / `pas` / `mlami` + combined intrinsic |
| `cross_method_nmi.csv` | symmetric NMI matrix over the member labellings |
| `plan.json` | planned members, filtered members, chosen operator, LLM rationale |
| `report.md` | human report; the leading `[A: Verified consensus]` banner is non-configurable |
| `result.json` | summary: surviving members, operator, cluster count, run id |

`report.md` and `result.json` are framework-standard. All paths are produced by
the runtime; this skill is a `type: consensus` shim, so the per-file substring
contract check is delegated to the runtime rather than enforced against the shim.
