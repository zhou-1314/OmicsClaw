---
# AUTO-GENERATED header from skill.yaml — do not edit by hand.
# Edit skill.yaml, then run: python scripts/generate_skill_md.py <skill_dir>
name: genomics-variant-calling
description: Load when summarising small variants (SNVs / indels) from a VCF or computing demo-pattern
  variant statistics (Ti/Tv ratio, per-chromosome distribution, SNP / indel split). Skip when filtering
  / merging VCFs (use genomics-vcf-operations); calling structural variants (use genomics-sv-detection);
  adding functional annotations (use genomics-variant-annotation).
version: 0.5.0
author: OmicsClaw
license: MIT
emoji: 🔎
tags:
- genomics
- variant-calling
- snv
- indel
- vcf
- ti-tv
requires:
- numpy
- pandas
---

# genomics-variant-calling

## When to use

The user has a VCF (or a BAM intended for calling) and wants
small-variant summary statistics: total variant count, SNP / indel
split, Ti/Tv ratio, per-chromosome distribution. The script does
**not** invoke an external caller (GATK HaplotypeCaller, Mutect2,
DeepVariant, FreeBayes, etc.) — it summarises an existing VCF or
generates a demo VCF for downstream-skill smoke tests.

For variant filtering / normalisation use `genomics-vcf-operations`;
for SVs use `genomics-sv-detection`; for functional annotation use
`genomics-variant-annotation`.

## Inputs & Outputs

<!-- AUTO-GENERATED from skill.yaml (interface) — do not edit by hand. Regenerate: python scripts/generate_skill_md.py <skill_dir> -->

**Inputs**

- File types: `.vcf`, `.bam`

**Outputs**

- `tables/variants.csv`
- `tables/variants_per_chrom.csv`
- `report.md`
- `result.json`
- Produces artifact `genomics.variant_table` as `tables/variants.csv` (`csv`)

## Flow

1. Load VCF (`--input <file.vcf>`) or generate a demo VCF at `output_dir/demo_variants.vcf` with `--n-variants` records (`genomics_variant_calling.py:94`).
2. Parse records; classify SNP vs indel; compute Ti/Tv on biallelic SNPs.
3. Aggregate per-chromosome counts.
4. Write `tables/variants.csv` (`genomics_variant_calling.py:300`) + `tables/variants_per_chrom.csv` (`:308`) + `report.md` + `result.json` envelope (`:314`).

## Gotchas

- **No external caller is invoked.** This skill does NOT run GATK / Mutect2 / DeepVariant / FreeBayes — it ingests a VCF and summarises it. To actually CALL variants, run an external pipeline first; this skill consumes the resulting VCF.
- **`--input` REQUIRED unless `--demo`.** `genomics_variant_calling.py:289` raises `ValueError("--input required when not using --demo")`; non-existent paths raise `FileNotFoundError` at `:292`.
- **`--n-variants` only affects `--demo`** (`genomics_variant_calling.py:278`, default 500). It is silently ignored when `--input` is set.
- **Multi-allelic VCF rows ARE split per-ALT.** `genomics_variant_calling.py:181-182` iterates `for a in alt.split(","):` and emits one CSV row per ALT allele. Output row counts therefore exceed input VCF line counts on multi-allelic data — no need to pre-normalise unless your downstream consumer requires one row per VCF line.
- **Demo VCF is a minimal SNV set** (no indels, no structural variants, no genotype fields). Useful for orchestrator smoke tests; do NOT use for biological inference.

## Key CLI

```bash
# Demo (500 synthetic SNVs)
python omicsclaw.py run genomics-variant-calling --demo --output /tmp/var_demo

# Custom demo size
python omicsclaw.py run genomics-variant-calling --demo --n-variants 2000 \
  --output /tmp/var_demo_large

# Real VCF
python omicsclaw.py run genomics-variant-calling \
  --input cohort.vcf --output results/
```

## See also

- `references/parameters.md` — every CLI flag
- `references/methodology.md` — SNP / indel / Ti-Tv definitions
- `references/output_contract.md` — `tables/variants.csv` schema
- Adjacent skills: `genomics-alignment` (upstream — produces the BAM that calling consumes), `genomics-vcf-operations` (downstream — VCF filtering / merging), `genomics-variant-annotation` (downstream — functional impact), `genomics-sv-detection` (parallel — SVs instead of small variants)
