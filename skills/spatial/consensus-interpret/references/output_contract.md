# consensus-interpret — Output Contract

Five artifacts are written to `--output` (the writes are delegated to
`_artifacts.py`).

## Output directory layout

```
<output>/
├── interpreted_report.md         # banner first line; [A+I: ...] or [I-noLLM: ...]
├── interpreted_assignments.json  # machine-readable cell-type assignments (schema below)
├── de_per_cluster.csv            # inline scanpy.tl.rank_genes_groups, top-K per cluster
├── contradiction_regions.csv     # rows where cross_method_nmi flags disagreement
└── audit.json                    # typed_run_id, adata checksum, marker DB, LLM model/seed, namespaces
```

| File | Contents |
|---|---|
| `interpreted_report.md` | human report; non-configurable banner first line (ADR 0012) |
| `interpreted_assignments.json` | structured assignments + next steps (schema below) |
| `de_per_cluster.csv` | per-cluster top-K markers from inline DE |
| `contradiction_regions.csv` | cross-method disagreement regions, LLM-narrated in the report |
| `audit.json` | provenance: `typed_run_id`, adata checksum, marker DB, LLM model/seed, `interpreted_namespace`, `evidence_base_namespace` |

## `interpreted_assignments.json` schema

```json
{
  "schema_version": "0.1",
  "typed_run_id": "<plan.json:run_id>",
  "evidence_base_namespace": "analysis://typed/<run_id>",
  "interpreted_namespace": "analysis://interpreted/<run_id>",
  "banner": "[A+I: Interpreted on verified consensus]",
  "operator": "<from typed plan.json>",
  "clusters": [
    {
      "id": 5,
      "n_cells": 778,
      "interpretation_status": "interpreted | low_confidence | failed",
      "cell_type": "CA1 pyramidal",
      "confidence": 0.84,
      "evidence": {
        "markers": [
          {"gene": "Pvrl3", "de_rank": 1, "db_source": "panglaodb_brain", "db_celltype": "CA1 pyramidal neuron", "weight": 0.9}
        ],
        "mean_local_purity": 0.617,
        "member_agreement": [
          {"member": "leiden_resolution-0.5", "label_overlap": 0.92}
        ]
      },
      "narrative_md_anchor": "#cluster-5"
    }
  ],
  "next_steps": [
    {
      "skill": "spatial-de",
      "args_hint": "--groupby consensus_kmode --comparisons cluster_3_vs_5",
      "priority": 1,
      "evidence_refs": [
        "cross_method_nmi.csv:row=leiden_resolution-0.5,col=leiden_resolution-1.5,value=0.597"
      ],
      "reason": "Lowest pair-wise NMI; marker disambiguation resolves whether clusters 3 and 5 are CA1 sub-types or a transition zone."
    }
  ]
}
```

`evidence.markers[]` and `next_steps[*].evidence_refs[]` MUST be non-empty (T3
invariant) — an ungrounded run exits 7.
