# consensus-domains — Methodology

This skill is a **workflow shim** (ADR 0016/0030): the script
`consensus_domains.py` only binds the flavour name and delegates to the shared
consensus runtime `omicsclaw.runtime.consensus.run` (`--source consensus-domains`).
All orchestration lives in `omicsclaw/runtime/consensus/`.

## Why consensus over a single domain method

Single spatial-domain methods (BANKSY, GraphST, SEDR, Leiden, SpaGCN…) disagree
on non-standard tissues (tumour, low-UMI sections). A consensus that fans the
members out, scores them on an unsupervised panel, and votes a joint labelling
is more trustworthy than any one method — and, crucially, it surfaces the
**cross-method disagreement** instead of hiding it.

## The pipeline (delegated to the runtime)

1. **Plan** — propose N member methods (LLM evaluation-chair or deterministic
   fallback) from `spatial-domains` `param_hints`.
2. **Fan out** — run `spatial-domains` per member in parallel, with a per-member
   timeout; members that crash/timeout are dropped (A-path fails loudly if fewer
   than 2 survive).
3. **Score** — rank survivors by `alpha · cross_method_NMI + beta · intrinsic`,
   where the intrinsic is the normalized multi-metric spatial panel
   (`chaos`/`pas`/`mlami`, ADR 0028) unless `--no-spatial-panel` is set; a
   `max_class_frac` cap filters degenerate clusterings.
4. **Select base clusterings** — top-K by score (interactive confirm on the CLI
   surface; automatic on Desktop/Channel or `--non-interactive`).
5. **Consensus** — apply the chosen operator (`kmode` / `weighted` / `lca`).
6. **Report** — emit the mandatory `[A: Verified consensus]` banner + audit trail.

## When to override defaults

- `--members` to pin the exact method set (reproducibility / known-good subset).
- `--operator weighted` when member quality is uneven and you trust the score.
- `--all` for a SACCELERATOR-style benchmark over every eligible method.
- `--no-spatial-panel` to fall back to the single `mean_local_purity` signal.

See `references/parameters.md` for every flag and `references/output_contract.md`
for the artifact schema. The scoring contract is ADR 0011; the workflow-runtime
generalisation is ADR 0016.
