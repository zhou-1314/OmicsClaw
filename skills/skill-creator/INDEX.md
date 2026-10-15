# Skill Creator Index

**Domain key:** `skill-creator`
**Primary data types:** `SKILL.md`, `kernel.py`, trigger evaluation JSON, step benchmark ledgers
**Skill count:** 1

Meta-skill for authoring, refactoring, validating, and benchmarking OmicsClaw skills according to the Claude Science 4-part `SKILL.md` and `sidecar_gate` `kernel.py` specification.

## Skills

- `skill-creator` — Load when creating or refactoring an OmicsClaw skill, validating SKILL.md and kernel.py, or comparing skill routing and benchmark runs. Skip when analyzing biological data directly (use the relevant domain skill instead).
  triggers: create skill, skill creator, refactor skill, validate skill, kernel sidecar gate, optimize skill description, skill benchmark
