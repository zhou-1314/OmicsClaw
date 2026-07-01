---
# AUTO-GENERATED header from skill.yaml — do not edit by hand.
# Edit skill.yaml, then run: python scripts/generate_skill_md.py <skill_dir>
name: proteomics-data-import
description: Load when ingesting a MaxQuant `proteinGroups.txt`, FragPipe `combined_protein.tsv`, DIA-NN
  report, or generic CSV / TSV protein-quantification table — normalises columns to a standard schema,
  emits `tables/proteins.csv`. Skip when raw spectra are the input (run the search engine first); the
  file is already OmicsClaw schema.
version: 0.5.0
author: OmicsClaw
license: MIT
emoji: 📥
tags:
- proteomics
- import
- maxquant
- fragpipe
- diann
- spectronaut
requires:
- numpy
- pandas
---

# proteomics-data-import

## When to use

The user has a search-engine output (MaxQuant `proteinGroups.txt`,
FragPipe `combined_protein.tsv`, DIA-NN main report, or a generic
CSV / TSV protein table) and wants it normalised into OmicsClaw's
standard schema (lowercase `protein_id` plus `LFQ_<sample>` /
`Int_<sample>` intensity columns derived from MaxQuant's
`LFQ intensity ...` / `Intensity ...` headers).
Pick the format with `--format {maxquant,fragpipe,diann,generic}`
(default `maxquant`).

For raw MS spectra (mzML / RAW), run a search engine first
(MaxQuant / FragPipe / DIA-NN) and feed THIS skill the resulting
table.

## Inputs & Outputs

<!-- AUTO-GENERATED from skill.yaml (interface) — do not edit by hand. Regenerate: python scripts/generate_skill_md.py <skill_dir> -->

**Inputs**

- File types: `.txt`, `.tsv`, `.csv`

**Outputs**

- `tables/proteins.csv`
- `report.md`
- `result.json`

## Flow

1. Load input (`--input <file>`) or generate a demo MaxQuant-shaped file (`--demo`).
2. Dispatch to the format-specific importer (`proteomics_data_import.py:164-174` `_dispatch_import`); supported keys are `maxquant`, `fragpipe`, `diann`, `generic`.
3. Rename columns: `LFQ intensity <sample>` → `LFQ_<sample>` and `Intensity <sample>` → `Int_<sample>` (`proteomics_data_import.py:85`); `Majority protein IDs` → `protein_id`; `Gene names` → `gene_name`; etc.
4. Write `tables/proteins.csv` (`proteomics_data_import.py:284`) + `report.md` + `result.json` (`:299`).

## Gotchas

- **`--format` value must match `_dispatch_import` keys exactly.** `proteomics_data_import.py:166-171` registers `maxquant`, `fragpipe`, `diann`, `generic`. An unknown value raises `ValueError("Unsupported format: ... Supported: ['maxquant', 'fragpipe', 'diann', 'generic']")` at `:173`. There is no `spectronaut` importer despite the legacy SKILL.md mention — use `--format generic` for Spectronaut and rename columns yourself.
- **`--input` REQUIRED unless `--demo`.** `proteomics_data_import.py:275` raises `ValueError("--input required when not using --demo")`. Non-existent paths raise `FileNotFoundError` from `pd.read_csv`.
- **Output schema is LOWERCASE.** Column renaming targets `protein_id`, `intensity_<sample>`, `gene_name` etc. Downstream skills (`proteomics-quantification`, `proteomics-de`) assume this casing. Verify after import with `head tables/proteins.csv`.
- **No deduplication of contaminants / decoys.** Contaminant (`CON_*`) and decoy (`REV_*`) rows are passed through unchanged. Filter them upstream with the search engine's `--keep-contaminants false` flag, or add a downstream `df = df[~df["protein_id"].str.startswith(("CON_", "REV_"))]` step.

## Key CLI

```bash
# Demo (synthetic MaxQuant-style)
python omicsclaw.py run proteomics-data-import --demo --output /tmp/import_demo

# Real MaxQuant output
python omicsclaw.py run proteomics-data-import \
  --input proteinGroups.txt --output results/ --format maxquant

# FragPipe combined_protein
python omicsclaw.py run proteomics-data-import \
  --input combined_protein.tsv --output results/ --format fragpipe

# DIA-NN main report
python omicsclaw.py run proteomics-data-import \
  --input report.tsv --output results/ --format diann

# Generic / Spectronaut (rename columns yourself first)
python omicsclaw.py run proteomics-data-import \
  --input my_table.csv --output results/ --format generic
```

## See also

- `references/parameters.md` — every CLI flag
- `references/methodology.md` — per-format column-mapping rules
- `references/output_contract.md` — `tables/proteins.csv` schema
- Adjacent skills: `proteomics-ms-qc` (downstream — QC the imported table), `proteomics-quantification` (downstream — compute LFQ / iBAQ / spectral count), `proteomics-identification` (parallel — peptide-level summary), `proteomics-de` (downstream — differential abundance after import)
