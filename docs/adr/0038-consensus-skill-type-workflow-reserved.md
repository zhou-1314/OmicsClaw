# Rename the consensus skill type to `consensus`; reserve `workflow` for composition

## Status

**Accepted (2026-07-01).** Implemented in-session. Refines the naming in
ADR 0030 (first-class-skill-type-system) and ADR 0016
(consensus-as-workflow-runtime): both introduced the first non-leaf skill shape
under the name `workflow`, but that name conflates two distinct concepts. This
ADR splits them. The existing single-script leaf skills are byte-unchanged.

## Context

ADR 0030 gave skills an optional `type` sidecar field, and named the first
non-leaf type `workflow`. In practice **every** `type: workflow` skill is a
*consensus* shim — a thin binding over `omicsclaw.runtime.consensus.run` that
declares a `SOURCE ∈ CONSENSUS_SOURCES` (ADR 0016): `consensus-domains`,
`sc-consensus-clustering`, `sc-consensus-integration`, `sc-consensus-pseudotime`.
The lint profile (`scripts/skill_lint.py`) that `type: workflow` triggers is
literally *"validate a shim against the consensus runtime."*

Two different ideas were sharing one label:

- **consensus** — *one* analysis type, run with *many methods/parameters*,
  scored and voted into a typed consensus (an ensemble over methods).
- **workflow** — *many different* analysis skills chained into a *pipeline*
  (a composition over skills). This does not exist as a skill yet; the
  `spatial-pipeline` chain is an `omicsclaw.py run` target, not a skill.

Calling the consensus shims `workflow` blocks the natural, future use of
`workflow` for real skill-composition, and misdescribes what the shims do.

## Decision

1. Introduce **`type: consensus`** for the consensus shims. The consensus-shim
   profile moves here: AST shim-wiring validation (`_check_consensus_shim`),
   `SOURCE ∈ CONSENSUS_SOURCES`, no `--demo` (the shim runs on real preprocessed
   data via the consensus runtime), and no own `SKILL_VERSION` (it delegates).
2. Keep **`type: workflow`** as a valid, **reserved** type for a future
   composition profile that chains different analysis skills. No skill uses it
   today; until a profile is defined it lints as `leaf`.
3. `SkillType = Literal["leaf", "workflow", "consensus"]`; `leaf` stays the
   default and self-contained-analysis shape.

## Consequences

- `omicsclaw/skill/schema.py` — `SkillType` gains `consensus` (keeps `workflow`).
- `omicsclaw/skill/lazy_metadata.py` — `SKILL_TYPES` gains `consensus` (so a
  `type: consensus` sidecar is not coerced back to `leaf` at runtime).
- Type-keyed behaviour moves `workflow → consensus`: `scripts/skill_lint.py`
  (`_check_consensus_shim`, `_analyse_consensus_shim`, `_CONSENSUS_RUN_MODULE`,
  the type-dispatch + entry-existence checks), `scripts/sync_skill_version.py`
  (SKILL_VERSION exemption), `scripts/generate_catalog.py` (`has_demo`),
  `omicsclaw/skill/registry.py` (`demo_args`), `omicsclaw/skill/runner.py`
  (`--demo` refusal message).
- The 4 shim `parameters.yaml` change `type: workflow → type: consensus`;
  `catalog.json` regenerated. Tests updated (`test_lazy_metadata`,
  `test_skill_lint` consensus-shim suite).
- Migrating the 4 consensus shims to v2 (skill.yaml) is unblocked and unchanged
  in mechanics — they carry `type: consensus`, still have no `SKILL_VERSION`,
  still validate as consensus shims.

Refines ADR 0016 and ADR 0030.
