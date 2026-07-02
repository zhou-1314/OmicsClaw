---
# AUTO-GENERATED header from skill.yaml — do not edit by hand.
# Edit skill.yaml, then run: python scripts/generate_skill_md.py <skill_dir>
name: omics-skill-builder
description: Load when scaffolding a NEW OmicsClaw skill from a natural-language request — generates the
  skill directory layout (skill.yaml, SKILL.md, references/, tests/) under the chosen domain. Skip when
  modifying an existing skill (edit its files directly); only routing a query (use orchestrator).
version: 0.5.0
author: OmicsClaw
license: MIT
emoji: 🛠
tags:
- orchestrator
- scaffold
- skill-builder
- meta
- code-generation
requires:
- PyYAML
---

# omics-skill-builder

## When to use

The user wants to add a NEW skill to the OmicsClaw catalog from a
natural-language request. This skill produces a directory scaffold
under the chosen `--domain` (`spatial` / `singlecell` / `genomics`
/ `proteomics` / `metabolomics` / `bulkrna` / `orchestrator`) with
the v2 layout: `skill.yaml`, `SKILL.md`, `references/`,
`tests/`, plus a reproducibility manifest.

For modifying an existing skill, edit its files directly — this
skill is for net-new additions only. For dispatching queries to
existing skills, use `orchestrator`.

## Inputs & Outputs

<!-- AUTO-GENERATED from skill.yaml (interface) — do not edit by hand. Regenerate: python scripts/generate_skill_md.py <skill_dir> -->

**Outputs**

- `output_dir/SCAFFOLD_SUMMARY.md`
- `output_dir/report.md`
- `output_dir/result.json`
- `skills/<domain>/<skill-name>/`

## Flow

1. Parse `--request` (or `--demo`); raise `SystemExit("--request is required unless --demo is used.")` at `omics_skill_builder.py:75` when missing.
2. Optionally promote a previous autonomous-analysis output via `--source-analysis-dir <path>` or `--promote-from-latest`.
3. Call `create_skill_scaffold` (`omicsclaw.core.skill_scaffolder`); it writes the new skill directory under `skills/<domain>/<skill-name>/`.
4. Write `SCAFFOLD_SUMMARY.md` + `report.md` + `reproducibility/commands.sh` + `result.json` into `--output`.

## Gotchas

- **`--request` REQUIRED unless `--demo` — raises `SystemExit` (exit 1).** `omics_skill_builder.py:75` raises `SystemExit("--request is required unless --demo is used.")`. Different from most OmicsClaw skills which use `ValueError` / `parser.error`; the exit code is 1, not 2.
- **`--domain` defaults to `orchestrator` — usually NOT what you want.** `omics_skill_builder.py:30` defaults to `orchestrator`; pass `--domain spatial` (or whichever) explicitly. Choices are 7 fixed values; an unknown domain is rejected by argparse.
- **The new skill is written to `skills/<domain>/<skill-name>/`, not `--output`.** `--output` only receives the scaffold summary + report + commands.sh; the actual skill code goes under `skills/`. Don't confuse the two.
- **`--trigger-keyword`, `--method`, `--input-format`, `--output-item` are REPEATABLE flags.** Pass `--trigger-keyword kw1 --trigger-keyword kw2` to add multiple. Single quoting won't help — argparse honours `action="append"`.
- **`--promote-from-latest` requires a recent autonomous-analysis output.** If no recent output exists, the promotion silently no-ops.
- **`--demo` lands in the orchestrator domain, NOT the implied target domain.** `omics_skill_builder.py:65` resolves `domain = args.domain or "spatial"`, but `args.domain` defaults to `"orchestrator"` (truthy), so the demo scaffold is written to `skills/orchestrator/spatial-cellcharter-domains/` rather than `skills/spatial/...`. For real scaffolds always pass `--domain <target>` explicitly.

## Key CLI

```bash
# Demo (built-in scaffold example)
python omicsclaw.py run omics-skill-builder --demo --output /tmp/builder_demo

# Real scaffold for a spatial skill
python omicsclaw.py run omics-skill-builder \
  --request "Compute Moran's I per gene on Visium data" \
  --domain spatial --skill-name spatial-moran \
  --summary "Per-gene spatial autocorrelation via Moran's I" \
  --trigger-keyword "Moran" --trigger-keyword "spatial autocorrelation" \
  --method "moran-i" --input-format "h5ad" --output-item "tables/moran_per_gene.csv" \
  --output /tmp/scaffold_out

# Promote from a successful autonomous analysis
python omicsclaw.py run omics-skill-builder \
  --request "Promote the Moran analysis from yesterday into a real skill" \
  --domain spatial --source-analysis-dir /path/to/autonomous_run \
  --output /tmp/promoted
```

## See also

- `references/parameters.md` — every CLI flag, repeatable behaviour
- `references/methodology.md` — scaffold layout, when to scaffold vs edit
- `references/output_contract.md` — `SCAFFOLD_SUMMARY.md` + `result.json` schema
- Adjacent skills: `orchestrator` (parallel — routes queries to EXISTING skills)
