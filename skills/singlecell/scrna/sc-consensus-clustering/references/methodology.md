# sc-consensus-clustering — Methodology

This skill is a **workflow shim** (ADR 0016/0030): `sc_consensus_clustering.py`
binds the flavour name and delegates to the shared consensus runtime
`omicsclaw.runtime.consensus.run` (`--source sc-consensus-clustering`).

## Why consensus over a resolution sweep

Single-resolution Leiden/Louvain is notoriously resolution-sensitive: `r=0.4`
yields ~6 broad types, `r=1.5` yields ~22 sub-states. Picking one resolution
bakes an arbitrary granularity into the result. A consensus across a resolution
sweep (and optionally `leiden` vs `louvain`) reports the **stable core** of the
labelling and quantifies where the methods disagree.

## The pipeline (delegated to the runtime)

1. **Plan members** — from the `--resolutions` × `--cluster-methods` grid, or an
   explicit `--members` spec, or `--all`.
2. **Fan out** — run `sc-clustering` once per member in parallel (per-member
   timeout; <2 survivors fails loudly).
3. **Score** — each member's `silhouette_score` (from its
   `clustering_summary.csv`) is the intrinsic signal; cross-method NMI is
   computed across members; ranked by `alpha · cross_NMI + beta · intrinsic`
   with a `max_class_frac` degeneracy cap.
4. **Select base clusterings** — top-K by score (interactive confirm on CLI).
5. **Consensus** — `kmode` / `weighted` / `lca` operator.
6. **Report** — mandatory `[A: Verified consensus]` banner + score/NMI tables.

## When to override defaults

- `--resolutions` should span at least one factor of 2 for an informative
  consensus (default `0.5,0.8,1.0,1.4,2.0`).
- `--cluster-methods leiden,louvain` adds the louvain axis (they usually agree
  to 1–2%, so most signal comes from the resolution sweep).
- `--members` to pin an exact, reproducible member set.

See `references/parameters.md` for every flag and `references/output_contract.md`
for the artifact schema. Scoring is ADR 0011; the workflow runtime is ADR 0016.
