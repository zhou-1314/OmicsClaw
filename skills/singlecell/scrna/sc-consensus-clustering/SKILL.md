---
# AUTO-GENERATED header from skill.yaml — do not edit by hand.
# Edit skill.yaml, then run: python scripts/generate_skill_md.py <skill_dir>
name: sc-consensus-clustering
description: Load when you want resolution-robust single-cell clusters on a preprocessed scRNA AnnData
  — fanning out leiden/louvain across a resolution sweep, scoring members by silhouette + cross-method
  NMI, and voting a typed consensus. Skip when one resolution suffices (use sc-clustering); spatial domains
  (use consensus-domains).
version: 0.1.0
author: OmicsClaw
license: Apache-2.0
emoji: 🧩
tags:
- singlecell
- consensus
- typed-consensus
- multi-resolution
- silhouette
- bc-ranking
- kmode
- lca
- weighted
requires:
- anndata
- numpy
- pandas
- PyYAML
- scanpy
- scikit-learn
- scipy
---

# sc-consensus-clustering

## When to use

The user has a preprocessed scRNA AnnData (PCA + neighborhood graph
already computed via `sc-preprocessing`) and wants robust cell-cluster
assignments insensitive to the chosen `resolution`. Single-resolution
Leiden/Louvain results are notoriously resolution-sensitive — at
`r=0.4` you get 6 broad types, at `r=1.5` you get 22 sub-states. This
skill runs a SACCELERATOR-style consensus across a resolution sweep
(and optionally across `leiden` vs `louvain`) and reports the **stable
core** of the labels.

It does NOT replace `sc-clustering`; it wraps it.

## Inputs & Outputs

<!-- AUTO-GENERATED from skill.yaml (interface) — do not edit by hand. Regenerate: python scripts/generate_skill_md.py <skill_dir> -->

**Inputs**

- File types: `.h5ad`
- Requires a preprocessed AnnData (`X` normalised, PCA/neighbours present)
- Expects `obsm`: `X_pca`

**Outputs**

- `consensus_labels.tsv`
- `member_scores.csv`
- `cross_method_nmi.csv`
- `plan.json`
- `report.md`
- `result.json`

## Flow

1. **Plan members** — either user-supplied (`--members` / `--all`) or
   derived from the resolution sweep × cluster-methods combinations.
2. **Fan out** — runtime invokes `sc-clustering` once per member.
3. **Score** — `silhouette_score` from each member's
   `clustering_summary.csv` is the intrinsic-quality signal; cross-method
   NMI is computed across members.
4. **BC pick** — top-K-by-composite-score default; CLI interactive
   override allowed.
5. **Consensus** — kmode / weighted / LCA on the selected base
   clusterings.
6. **Report** — banner + score table + NMI matrix.

## Gotchas

- **`--cluster-methods` defaults to `leiden` ONLY**, not both, because
  `louvain` and `leiden` agree to within 1–2% on most datasets and the
  consensus signal comes mostly from the resolution sweep.
- **Resolutions must span at least one factor of 2** for the consensus
  to be informative; default sweep covers 0.5–2.0.
- The mandatory banner is enforced by
  `runtime/consensus/dispatch.output_banner`. Do NOT strip it.
- `requires_preprocessed: true` — run `sc-preprocessing` first.

## Key CLI

```bash
# Default sweep (leiden at 5 resolutions)
oc run sc-consensus-clustering --input preprocessed.h5ad --output out/

# Both methods × 5 resolutions = 10 members; SACCELERATOR-style benchmark
oc run sc-consensus-clustering --input preprocessed.h5ad --output out/ \
  --cluster-methods leiden,louvain --resolutions 0.5,0.8,1.0,1.4,2.0

# Explicit
oc run sc-consensus-clustering --input preprocessed.h5ad --output out/ \
  --members leiden:resolution=0.5,leiden:resolution=1.0,louvain:resolution=1.0
```

## See also

- `references/methodology.md` — the resolution-sweep consensus rationale
- `references/output_contract.md` — `consensus_labels.tsv` / `member_scores.csv` / `plan.json` schema
- `references/parameters.md` — every CLI flag (generated from `skill.yaml`)
- Adjacent skills: `sc-preprocessing` (upstream — produces the input), `sc-clustering` (the per-member method this wraps), `consensus-domains` (parallel — the spatial analogue), `sc-consensus-integration` (parallel — consensus over integration backends)
- ADR 0010/0011/0016 — runtime layer, scoring protocol, workflow-runtime generalisation
