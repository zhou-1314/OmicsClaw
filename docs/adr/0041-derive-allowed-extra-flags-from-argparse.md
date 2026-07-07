# Derive `allowed_extra_flags` from the script; keep it only as a narrowing override

## Status

**Accepted (2026-07-06).** Implemented in-session. Refines ADR 0037
(unified-declarative-skill-representation), which made `skill.yaml` the single
source of truth, and builds on the flag-gate introduced for the runner. The
existing runtime gate behaviour is byte-preserved for every skill.

## Context

`interface.parameters.allowed_extra_flags` listed the `--flags` a skill's script
accepts. The runner filters LLM-supplied extra args against this list
(`argv_builder.filter_forwarded_args`, called from `runner.py`) so a
hallucinated flag is dropped rather than crashing the subprocess — all 90 leaf
scripts use `argparse.parse_args()`, which `sys.exit(2)`s on an unknown flag.

The list was **hand-maintained and lint-locked to equal the script's argparse
surface** (`skill_lint._check_allowed_extra_flags`: errors on both a missing and
an extra flag). That makes it a redundant mirror of information the script
already owns — the exact duplication ADR 0037 set out to remove, kept in sync by
hand and by CI. A corpus scan confirmed the redundancy is real and total:

- **91 / 95 leaf skills**: `allowed_extra_flags` == the script's argparse flags
  (minus the runner-blocked `--input/--output/--demo`), exactly, by construction
  of the equality lint. Fully derivable.
- **4 / 95 consensus skills** (`consensus-domains`, `sc-consensus-clustering`,
  `sc-consensus-integration`, `sc-consensus-pseudotime`): the list is a
  hand-picked **subset** of the shared `runtime/consensus/run` parser (e.g.
  `sc-consensus-clustering` exposes 17 of the parser's ~29 flags, deliberately
  hiding other flavours' flags plus `--help`/`--source`). This subset is genuine,
  non-derivable information.

It was also unguarded in one direction: nothing checked `hints.*.params` against
the list, so a per-method tuning hint could name a flag the gate drops (found:
`sc-consensus-clustering` hinted `--resolution` vs the parser's `--resolutions`).

## Decision

1. **Derive the gate's allow-list at runtime** from the script's argparse surface
   (leaf) or the consensus run parser (consensus), in the new single-source
   module `omicsclaw/skill/execution/flag_introspection.py`
   (`extract_argparse_flags`, `consensus_parser_flags`, `derive_accepted_flags`,
   `effective_allowed_flags`). `lazy_metadata` resolves the effective set once at
   load; the registry → runner → gate path is otherwise unchanged.
2. **`allowed_extra_flags` becomes an optional *narrowing override*.** Empty /
   absent (the leaf default) → derive. Non-empty → use verbatim (how a consensus
   shim exposes its curated subset). `skill_lint` imports the same module, so the
   lint and the runtime never diverge.
3. **Remove the field from the 91 leaf skills.** Behaviour-preserving by
   construction: the derived set equals the removed value (verified: all 95
   skills' resolved `allowed_extra_flags` are identical before/after).
4. **Lint semantics change** (`_check_allowed_extra_flags`): a *missing* flag is
   no longer an error (it is derived); a declared override that lists a flag the
   script lacks still is (stale/typo guard). Consensus skills must still declare
   the list (deriving all parser flags would over-expose `--help`/`--source`).
5. `references/parameters.md` sources the derived set (`params_dump_with_effective_flags`),
   so the doc still lists the accepted flags. New scaffolds emit no override.
6. **New lint `_check_hint_flags_accepted`** (Codex cross-validation): a per-method
   hint (`hints.*.params` / `advanced_params`) must not name a flag outside the
   effective allow-list — the parameter card / autoagent turn hint keys into
   `--kebab` CLI flags, and one the gate drops is a silent no-op. Found & fixed a
   live instance: `sc-consensus-clustering` hinted per-member `resolution` (→
   `--resolution`) while the shim only accepts the `--resolutions` sweep.
7. **Consensus fails closed** (Codex cross-validation): `derive_accepted_flags`
   returns an empty set for a consensus skill with no declared override, so the
   gate drops all extra flags rather than exposing the full run parser
   (`--help`/`--source`/other flavours). The lint still requires the override, so
   this is a runtime backstop, not the primary guard.

## Consequences

- **Removed the duplication.** The script's argparse is the sole source for
  "which flags exist"; there is no hand-maintained mirror to drift.
- **Gate behaviour unchanged.** Same flags accepted/dropped for every skill
  (end-to-end verified through the real derivation → `filter_forwarded_args` path).
- **Consensus curation preserved.** The 4 shims keep their explicit subset; the
  lint now *requires* it rather than merely bounding it.
- **A skill can still restrict its surface** below what the script accepts by
  declaring the override — the capability is retained, just no longer mandatory.
- Trade-off: the runtime now reads + regex-scans each leaf script's argparse once
  at metadata load (cached), instead of reading a literal list. Negligible, and
  the same static extractor CI already relied on.
- Not adopted: deleting the field entirely (consensus needs it) or dropping the
  gate and letting argparse fail loudly (a behaviour change for the agent loop,
  tracked separately).
