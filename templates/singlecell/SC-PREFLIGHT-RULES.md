# Singlecell Preflight Rules

Use this file when adding or refactoring preflight checks for scRNA skills.

## Decision Levels

- `proceed`
- `proceed_with_guidance`
- `needs_user_input`
- `blocked`

## What Preflight Must Check

1. matrix contract
2. missing metadata
3. method-specific prerequisites
4. whether the requested question is scientifically ambiguous
5. whether a method-specific parameter set must be surfaced instead of using one generic default
6. whether the user appears to be asking for a step that normally needs an upstream explanation first

## Ask The User When

- more than one `groupby`, `batch_key`, `sample_key`, `cluster_key`, or `cell_type_key` is plausible
- more than one embedding / representation is plausible and the choice changes the result
- reference or model choice changes the scientific meaning
- a fallback would change the biological interpretation
- the user did not provide key parameters, and accepting defaults would materially change interpretation without making that clear

## Block When

- a raw-count method has no usable raw counts
- a normalized-expression method only sees a count-oriented object
- required metadata is absent, not merely ambiguous
- required layers such as `spliced/unspliced` do not exist

## Guidance Rule

Do not tell users to run a helper skill by default when the current workflow can safely canonicalize the object automatically.
Only mention `sc-standardize-input` when the user wants an explicit exported canonical object or when debugging provenance is the main task.

When the user sounds novice or only asks for a broad step such as “do preprocessing”, “do clustering”, or “filter this data”:

- explain the usual upstream step in one plain sentence
- explain whether the current skill can continue automatically or should stop first
- explicitly state the key defaults before using them
- mention common optional side branches when they materially affect interpretation

Examples:

- before `sc-filter`, usually explain that `sc-qc` helps choose thresholds
- before `sc-clustering`, usually explain whether batch integration should happen first
- before downstream interpretation, mention doublet removal when it is a common concern

## Matrix-Specific Rule

Preflight should trust `adata.uns["omicsclaw_matrix_contract"]` first and only fall back to heuristics when that contract is missing.
