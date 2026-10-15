---
name: genomics-phasing
description: Load when summarising a phased VCF (output of WhatsHap / SHAPEIT5 / Eagle2) — phased fraction
  of het variants, phase-block N50, PS-field parsing, pipe-delimited genotype detection. Skip when the
  input is unphased (run a phaser first); calling small variants (use genomics-variant-calling).
trigger: haplotype phasing, WhatsHap, SHAPEIT, Eagle, phasing
tags:
- genomics
- phasing
- haplotype
- whatshap
- shapeit
- eagle
- ps
---

# Haplotype Phasing & Phase Block Contiguity (`genomics-phasing`)

## 1. Method Decision Matrix & When to Use

Select the phasing algorithm according to whether physical long reads, population reference panels, or parental trio genotypes are available:

| Evidence Type | Recommended Tool | Phasing Range | Why |
|---|---|---|---|
| **Read-backed physical phasing** (PacBio HiFi, ONT, or Illumina + BAM) | **WhatsHap** (`whatshap phase`) or **HapCUT2** | Kb to Mb phase blocks (`FORMAT/PS`) | Solves the Minimum Error Correction (MEC) problem directly from sequencing reads spanning $\ge 2$ heterozygous sites |
| **Joint SNV + SV + methylation long-read phasing** | **HiPhase** (PacBio) / **LongPhase** | Multi-megabase blocks | Simultaneously phases SNVs, indels, and structural variants with haplotagged BAM output |
| **Statistical population phasing** (biobank cohort / array / WGS) | **SHAPEIT5** / **Beagle 5.4** | Chromosome-scale | Uses Li-Stephens Hidden Markov Models on haplotype reference panels (e.g., 1000G, TOPMed) |
| **Pedigree / Trio Mendelian phasing** | **WhatsHap** (`--ped`) / **bcftools +trio-phase** | Chromosome-scale | Resolves maternal/paternal transmission (`0|1` vs. `1|0`) without switch errors at non-triple-het sites |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: `WhatsHap` Read-Backed Phasing + Haplotagging CLI

```bash
# 1. Phase heterozygous variants in VCF using long/short reads from BAM
whatshap phase --reference ref.fa --ignore-read-groups -o phased.vcf.gz unphased.vcf.gz alignments.bam
tabix -p vcf phased.vcf.gz

# 2. Tag BAM alignments with HP (haplotype 1/2) and PS (phase set) tags for IGV / allele-specific analysis
whatshap haplotag --reference ref.fa -o haplotagged.bam phased.vcf.gz alignments.bam

# 3. Compute phase block N50 and switch error rates
whatshap stats --tsv phase_stats.tsv phased.vcf.gz
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("genomics-phasing")`)

```python
# Calling use_skill("genomics-phasing") auto-injects kernel.py helpers into the python kernel:
variants = read_records("data/phased.vcf")
phased_df = analyze(variants)
blocks_df = phase_blocks(phased_df)
```

## 3. Output Format & Verification Contract

- **Phased Variant Table** (`tables/phased_variants.csv`):
  - Contains `chrom`, `pos`, `gt` (`0|1`, `1|0`, `0/1`, `1/1`), `is_het`, `is_phased`, and `phase_set` (`FORMAT/PS`).
- **Phase Block Contiguity Table** (`tables/phase_blocks.csv`):
  - Grouped by `(chrom, phase_set)` with `n_variants`, `start_pos`, `end_pos`, and `span_bp` (`end_pos - start_pos`).
- **Verification Invariants**:
  - Phasing rate is computed strictly over **heterozygous** variants (`n_phased_het / n_het`), as homozygous variants (`1/1`, `0/0`) carry no haplotype phase ambiguity.
  - Every phased heterozygous variant (`|` separator in `GT`) is assigned to a non-null `phase_set` (`PS`).

## 4. Gotchas & Troubleshooting

1. **Pipe (`0|1`) Without `FORMAT/PS` Does Not Mean Chromosome-Wide Phasing**:
   - In read-backed phasing (`WhatsHap`, `HapCUT2`), `0|1` alleles are only phased relative to other variants sharing the **exact same `FORMAT/PS` (Phase Set) integer** on that chromosome. Comparing `0|1` across two different `PS` blocks has a 50% switch probability.
2. **Homozygous Variants (`1/1`) Inflate Apparent Phasing Rate**:
   - Some callers write `1|1` for all homozygous ALT calls. Always compute phasing completeness strictly over heterozygous genotypes (`0/1`, `0|1`, `1|0`), not total VCF rows.
3. **Phase Block `N50` (Span in bp) vs. Variant Count per Block**:
   - Two heterozygous variants 50 kb apart bridged by a single ultra-long ONT read create a 50 kb phase block even if intermediate variants failed QC. Always check both `span_bp` (block N50) and `n_variants` per block.
4. **Mismatched Sample Read Groups (`@RG SM`) Cause `WhatsHap` to Skip Reads**:
   - By default, `WhatsHap` matches the VCF sample column name against BAM `@RG SM`. If BAM `@RG` tags are inconsistent or absent, pass `--ignore-read-groups` explicitly.

## Dependencies

`numpy`, `pandas`, `matplotlib`
