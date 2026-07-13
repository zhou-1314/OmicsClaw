---
# AUTO-GENERATED header from skill.yaml — do not edit by hand.
# Edit skill.yaml, then run: python scripts/generate_skill_md.py <skill_dir>
name: consensus-interpret
description: Load when biologically interpreting a finished verified consensus run (consensus-domains
  / sc-consensus-clustering) — inline DE, marker-DB lookup, and LLM cell-type naming with mandatory marker
  citations + evidence-bound next-step recommendations. Skip when the consensus run failed (fix it first);
  forward query→skill routing (use orchestrator).
version: 0.1.0
author: OmicsClaw
license: Apache-2.0
tags:
- spatial
- consensus
- interpreted-layer
- biology-annotation
- marker-grounded
- backward-proof-driven-recommendation
requires:
- anndata
- numpy
- pandas
- scanpy
- scikit-learn
---

# consensus-interpret

## When to use

The user has just finished a verified typed consensus run
(`consensus-domains` or `sc-consensus-clustering`) and wants the next
manual step (read `cross_method_nmi.csv` → run `spatial-de` →
cross-reference markers → name cell types → decide downstream skill) done
automatically with **falsifiable evidence** binding every LLM claim.

This skill does NOT replace the typed run. It is a strictly downstream
consumer: it reads `<typed_run_dir>/{plan.json, consensus_labels.tsv,
member_scores.csv, cross_method_nmi.csv}` plus the original adata, and
writes its output to a *different* directory under
`analysis://interpreted/<typed_run_id>`. The verified-vs-exploratory
boundary established by ADR 0010 is preserved.

Skip when:

- The typed run did not produce `consensus_labels.tsv` (i.e.
  `consensus-domains` exited non-zero — fix the typed run first).
- You are looking for a generic `query → skill` dispatcher; use
  `orchestrator` for that (forward direction).
- You want to refine the consensus itself based on LLM judgment; that is
  explicitly forbidden by §11.4 "LLM never participates in statistical
  merging" and would be rejected by this skill's T3 invariants.

## Inputs & Outputs

<!-- AUTO-GENERATED from skill.yaml (interface) — do not edit by hand. Regenerate: python scripts/generate_skill_md.py <skill_dir> -->

**Inputs**

- Input kinds: `directory`
- File types: `.json`

**Outputs**

- `interpreted_report.md`
- `interpreted_assignments.json`
- `de_per_cluster.csv`
- `contradiction_regions.csv`
- `audit.json`

## Flow

```
1. Preflight (T1 — fail-fast if any fail)
   ├─ Load plan.json from --input; assert schema_version + typed run integrity
   ├─ Locate adata at plan.json.input_path (or --adata override); check exists
   ├─ Load consensus_labels.tsv; assert observation column ⊆ adata.obs.index
   ├─ Resolve marker DB:
   │    --markers <path> if given;
   │    else bundled `data/markers/panglaodb_<tissue>.tsv` for --tissue;
   │    else exit 5 (MarkerDBUnavailable)
   └─ If LLM required and unreachable AND --no-llm not set → exit 6 (LLMUnavailable)

2. Per-cluster differential expression (deterministic, scanpy)
   └─ scanpy.tl.rank_genes_groups(adata, groupby=consensus_<operator>, method='wilcoxon')
       → de_per_cluster.csv with top-K markers per cluster (K=20 default)

3. Marker → cell-type lookup (deterministic, pre-LLM)
   └─ For each cluster, compute candidate cell types by ranking DB entries
       whose gene appears in the cluster's top-K markers (weighted by db.weight × 1/de_rank).

4. LLM grounded interpretation (γ + β; one call per cluster + one synthesis call)
   ├─ Prompt template embeds (per cluster):
   │    cluster_id, n_cells, top-K DE markers,
   │    DB candidate cell types (ranked),
   │    member_agreement summary, cross_method_nmi neighbors
   ├─ LLM must return JSON conforming to interpreted_assignments.json
   │   schema; mandatory evidence.markers[] with non-empty
   │   {gene, db_source, db_celltype}
   └─ After all clusters: one synthesis call to produce next_steps[]
       with mandatory evidence_refs[] (capped at top-3 by priority)

5. Invariant enforcement (T3 — fail-fast if violated)
   ├─ Every cluster.evidence.markers != []      → else exit 7
   ├─ Every next_steps[*].evidence_refs != []   → else exit 7
   └─ Banner present and matches one of two allowed values → else exit 7

6. Coverage check (T2 — escalate to T1 if floor breached)
   └─ interpretable_cluster_frac < --coverage-floor → exit 8

7. Artifact writes
   ├─ interpreted_report.md (banner enforced in format_interpreted_report)
   ├─ interpreted_assignments.json
   ├─ de_per_cluster.csv
   ├─ contradiction_regions.csv
   └─ audit.json
```

## Gotchas

- **It never refines the consensus.** The LLM names cell types and recommends
  next steps but is forbidden from touching the statistical merge — the T3
  invariants reject any attempt (ADR 0012 §11.4). Treat the consensus labels as
  fixed input.
- **Marker citations are mandatory.** Every cluster's `evidence.markers[]` and
  every next-step's `evidence_refs[]` must be non-empty, or the run exits 7
  (InvariantViolation). Ungrounded LLM output is rejected, not silently kept.
- **`--no-llm` changes the banner, not just the content.** Structural-only mode
  emits `[I-noLLM: ...]` and drops all cell-type claims; downstream consumers
  must branch on the banner, not assume biology is present.
- **Output lands in a separate namespace.** Interpreted artifacts go to
  `analysis://interpreted/<run_id>`, never overwriting the verified
  `analysis://typed/<run_id>` evidence base (the ADR 0010 boundary).

## Failure modes (per ADR 0012)

| Exit | Name | Meaning |
|---|---|---|
| 0 | success | All clusters interpreted (or `low_confidence`), invariants intact, no degradation triggered |
| 2 | argparse | CLI error |
| 3 | TypedRunInvalid | `plan.json` missing / malformed / not from a typed run |
| 4 | AdataMismatch | adata `obs` index disjoint from `consensus_labels.tsv` `observation` |
| 5 | MarkerDBUnavailable | `--tissue` not in bundled DBs and `--markers` not provided |
| 6 | LLMUnavailable | LLM endpoint unreachable and `--no-llm` not given |
| 7 | InvariantViolation | LLM violated marker-grounding or evidence-ref contract (T3) |
| 8 | CoverageBelowThreshold | < 50% of clusters interpretable (after T2 degradation) |

## Key CLI

### Default usage (after a typed run completes)

```bash
oc run consensus-domains --input preprocessed.h5ad --output run1/ \
  --members banksy,graphst,leiden:resolution=0.5,leiden:resolution=1.0 \
  --non-interactive --operator kmode --seed 0

oc run consensus-interpret --input run1/ --output run1_interpreted/ \
  --tissue brain
# → run1_interpreted/interpreted_report.md begins with [A+I: ...]
```

### CI / offline (structural-only)

```bash
oc run consensus-interpret --input run1/ --output run1_struct/ \
  --tissue brain --no-llm
# → run1_struct/interpreted_report.md begins with [I-noLLM: ...]
# → no cell-type claims, only cluster sizes / NMI summary / contradiction regions
```

### User-provided marker DB (non-bundled tissue)

```bash
oc run consensus-interpret --input run1/ --output run1_interp/ \
  --markers ~/markers/mouse_intestine.tsv
# → bypasses --tissue requirement; uses user's custom DB
```

## See also

- `references/methodology.md` — the γ (naming) + β (recommendation) protocol and grounding rules
- `references/output_contract.md` — `interpreted_assignments.json` schema + the 5 written artifacts
- `references/parameters.md` — every CLI flag (generated from `skill.yaml`)
- Adjacent skills: `consensus-domains` / `sc-consensus-clustering` (upstream — produce the verified run this interprets), `orchestrator` (sibling — forward `query → skill`; this does backward `result → skill+evidence`), `spatial-de` / `spatial-deconv` / `spatial-communication` (downstream — next-step skills β may recommend, each with mandatory evidence)
- ADR 0010/0011/0012 — consensus runtime boundary, evaluation protocol, this skill's 4-axis + T3 invariants
