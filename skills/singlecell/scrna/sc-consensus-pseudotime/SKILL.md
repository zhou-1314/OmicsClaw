---
name: sc-consensus-pseudotime
description: 'Load when you want a single-cell pseudotime ordering robust to the choice of trajectory method — fanning out DPT/Palantir/VIA from a shared root, rank-aligning them, and voting a consensus pseudotime with per-cell uncertainty. Skip when you have branching multi-lineage trajectories or no defined root.'
version: 0.1.0
author: OmicsClaw
license: MIT
tags:
- singlecell
- scrna
- consensus
- pseudotime
- trajectory
---

# sc-consensus-pseudotime

## When to use

Verified **continuous** consensus over **pseudotime methods** (ADR 0031). A single
pseudotime is sensitive to which algorithm produced it: DPT, Palantir and VIA make
different assumptions and can order cells differently. Use this when you have a
preprocessed single-cell AnnData and a **defined root** (a `--root-cluster` or
`--root-cell`) and want a pseudotime that is **not an artifact of one method**, with
a per-cell uncertainty band.

A pseudotime is only defined up to a monotone reparameterisation and a direction
flip, so the runtime makes members comparable by **rank-normalisation** (cancels the
monotone gauge) + a **direction safeguard** (anchored flip), then aggregates a
consensus by per-cell `median` (default) or agreement-`weighted` mean, re-ranked to
`[0, 1]`. v1 is agreement-only (scored by mean pairwise Spearman).

## Inputs & Outputs

| Input | Format | Required |
|---|---|---|
| Preprocessed AnnData | `--input <preprocessed.h5ad>` (a neighbour graph / `X_pca`) | yes |
| Output directory | `--output <dir>` | yes |
| Shared root | `--root-cluster <name>` **or** `--root-cell <id>` | yes (one of) |
| Pseudotime methods | `--pseudotime-methods dpt,palantir,via` | no (default all three) |

Outputs (under `analysis://typed/<run_id>`): `consensus_pseudotime.tsv`
(`consensus_pseudotime` + per-cell `pseudotime_mad` + `range`),
`member_agreement_spearman.csv`, `member_scores.csv`, `plan.json`, `report.md`
(first line `[A: Verified consensus]`).

## Flow

1. Plan one member per pseudotime method, all sharing the user's root.
2. Fan out `sc-pseudotime --method <m>` and gather each member's canonical
   `obs['pseudotime']`.
3. Rank-normalise + direction-align; drop any degenerate (constant) member.
4. Score members by mean pairwise Spearman; select the top-K.
5. Aggregate via `median`/`weighted`, re-rank to `[0, 1]`, and report per-cell
   dispersion + a weak-agreement guard.

## Gotchas

- A **shared root is required** — without `--root-cluster`/`--root-cell` the run
  aborts (the root pins pseudotime direction so the consensus is well-posed).
- v1 members are **single-global-pseudotime** methods (dpt/palantir/via); multi-lineage
  methods (slingshot/monocle3/cellrank) are deferred — they re-introduce branching
  topology, which is out of scope.
- A **weak-agreement** warning (mean Spearman < 0.5) means the methods disagree on the
  ordering: the data may have no single shared trajectory. It is reported, not fatal.
- `pseudotime_mad` is a majority-support metric; read the per-cell `range` alongside it
  to catch a lone strongly-disagreeing method.

## Key CLI

```bash
python omicsclaw.py run sc-consensus-pseudotime \
  --input preprocessed.h5ad --output out/ --root-cluster Stem
# choose methods + the weighted operator
python omicsclaw.py run sc-consensus-pseudotime --input preprocessed.h5ad \
  --output out/ --root-cell 42 --pseudotime-methods dpt,palantir --operator weighted
```

## See also

- `sc-pseudotime` — the per-method member skill fanned out here.
- `sc-consensus-integration` / `sc-consensus-clustering` — the categorical consensus
  flavours (clustering robustness); ADR 0016 (templates), ADR 0031 (this flavour).
