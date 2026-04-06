# Singlecell Skill Template Addendum

Use this together with `templates/SKILL-TEMPLATE.md` when creating or refactoring a scRNA skill.

## Required Additions For `SKILL.md`

### Data / State Requirements

Every scRNA skill should declare:
- whether it needs raw counts or normalized expression
- where it expects them from (`layers["counts"]`, `X`, `raw`)
- whether clustering / labels / batch metadata must already exist
- if the skill has multiple methods, which parameters are shared and which parameters are method-specific

### User Guidance Requirements

Every scRNA skill should explain the workflow in a novice-friendly way:

- if the user only says “I want to do X”, the skill should state what upstream step usually comes first
- if the current skill can safely continue with defaults, it should say which defaults will be used
- if a choice changes the biological meaning, the skill should stop and ask the user to confirm
- if a side branch is common but optional (for example doublets, batch correction), the skill should mention it explicitly instead of assuming the user already knows
- if the skill is not the right next step, the docs should say what skill the user should run next

### Workflow Section

For scRNA skills, the workflow should usually mention:
1. load
2. preflight
3. canonicalize or validate matrix state
4. run method
5. persist results
6. render gallery
7. export `processed.h5ad`

### Output Contract

Use `processed.h5ad` as the standard output object name.

## Required Additions For Code

- write `adata.uns["omicsclaw_input_contract"]`
- write `adata.uns["omicsclaw_matrix_contract"]`
- ensure matrix semantics match `templates/singlecell/SC-MATRIX-CONTRACT.md`
- ensure preflight follows `templates/singlecell/SC-PREFLIGHT-RULES.md`
- if multiple methods exist, map method-specific CLI parameters explicitly instead of hiding them behind one generic parameter set
- make novice-facing guidance part of preflight/runtime behavior, not only prose in `SKILL.md`

## Required Additions For Companion Docs

Every scRNA skill should have:
- a guardrail file
- a skill-guide file

Those files should mention the method-specific matrix expectation explicitly.
They should also mention:

- what a beginner user should do before this skill
- what this skill will do automatically
- what the key parameters mean in plain language
- what the usual next step is after the skill finishes

## Anti-Patterns

Do not do these:
- assume `adata.raw` always means normalized expression
- assume `adata.X` always means normalized expression
- silently use count-like `X` for normalized-only methods
- persist `scaled_expression` as the public output meaning of `X`
- expose only a generic parameter set when different methods have different real tuning knobs
- assume the user already understands the full scRNA workflow and skip upstream/downstream guidance
