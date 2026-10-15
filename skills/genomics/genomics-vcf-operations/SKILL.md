---
name: genomics-vcf-operations
description: Load for file operations on an existing VCF — classification (SNP / MNP / INS / DEL), QUAL
  / DP filtering, merging or subsetting via bcftools. Skip when computing Ti/Tv statistics (use
  genomics-variant-calling); variant calling from BAM; annotation (use
  genomics-variant-annotation).
trigger: VCF, bcftools, variant filter, merge VCF
tags:
- genomics
- vcf
- bcftools
- filter
- snv
- indel
---

# VCF Normalization, Filtering & Set Operations (`genomics-vcf-operations`)

## 1. Method Decision Matrix & When to Use

Select the `bcftools` / `htslib` operation according to the VCF manipulation task:

| VCF Task | Upstream Command (`bcftools` / `htslib`) | Why |
|---|---|---|
| **Multi-allelic splitting & indel left-alignment** | `bcftools norm -m -any -f ref.fa in.vcf.gz -Oz -o norm.vcf.gz` | Decomposes `A,G` sites and left-aligns indels so identical variants match across callsets and ClinVar/gnomAD |
| **Site & genotype expression filtering** | `bcftools view -i 'QUAL>=30 && INFO/DP>=10 && FILTER="PASS"'` or `bcftools filter -s LowQual` | Streams bgzipped VCF/BCF at C speed while preserving all `FORMAT` sample genotypes |
| **Multi-sample cohort merging vs. intersection** | `bcftools merge` (non-overlapping samples) vs. `bcftools isec -p dir/` (concordance) | `merge` combines distinct samples into a multi-sample VCF; `isec` finds shared/private sites across callers |
| **Chromosome renaming (`1` $\leftrightarrow$ `chr1`)** | `bcftools annotate --rename-chrs chr_map.txt in.vcf.gz` | Safely updates both `##contig=` header lines and body `#CHROM` records simultaneously |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: `bcftools` Normalization, Soft/Hard Filtering & `cyvcf2`/`pysam` Streaming

```bash
# 1. Left-align indels, split multi-allelic records, and filter by QUAL and depth (preserving genotypes)
bcftools norm -m -any -f ref.fa raw.vcf.gz -Ou \
  | bcftools view -i 'QUAL >= 30 && INFO/DP >= 10' -Oz -o filtered.vcf.gz
bcftools index -t filtered.vcf.gz
```

```python
import pysam

# 2. Stream bgzipped VCF records with pysam.VariantFile
with pysam.VariantFile("filtered.vcf.gz") as vcf:
    for rec in vcf.fetch():
        dp = rec.info.get("DP", 0)
        gt = rec.samples[0]["GT"] if len(rec.samples) else (None, None)
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("genomics-vcf-operations")`)

```python
# Calling use_skill("genomics-vcf-operations") auto-injects kernel.py helpers into the python kernel:
records = read_records("data/variants.vcf")
filtered_df = analyze(records, min_qual=30.0, min_dp=10)
summary = vcf_summary(filtered_df)
```

## 3. Output Format & Verification Contract

- **Filtered Variant Table** (`tables/variants.csv`):
  - Contains `chrom`, `pos`, `id`, `ref`, `alt`, `qual`, `filter`, `dp` (`INFO/DP`), and `type` (`SNP`, `INDEL`, `MNP`), retaining only records satisfying `qual >= min_qual` and `dp >= min_dp`.
- **VCF Header Preservation**:
  - `read_records` stores original `##` metadata and `#CHROM` header lines in `df.attrs["vcf_headers"]`.
- **Verification Invariants**:
  - All retained rows satisfy `qual >= min_qual` and `dp >= min_dp`.
  - VCF coordinates are 1-based inclusive (`pos >= 1`), and multi-allelic `ALT` records are decomposed consistently.

## 4. Gotchas & Troubleshooting

1. **Standard `gzip` vs. `bgzip` Block Compression**:
   - Standard `gzip` files cannot be indexed by `tabix` or `bcftools index`. Always compress VCFs with `bgzip` (or `bcftools view -Oz`) before indexing with `bcftools index -t`.
2. **`INFO/DP` (Site-Level Combined Depth) vs. `FORMAT/DP` (Per-Sample Depth)**:
   - In multi-sample VCFs, `INFO/DP` is the sum of read depths across all samples (so 100 samples at $1\times$ depth give `INFO/DP=100`), whereas `FORMAT/DP` is the depth in each individual sample. Use `FMT/DP >= 10` in `bcftools view -i` when filtering per-sample genotype support.
3. **`bcftools isec` Without Prior `bcftools norm` Misses Equivalent Indels**:
   - Two callers may represent the same 2-bp dinucleotide deletion at different positions in a tandem repeat or pack it inside a multi-allelic site. Always run `bcftools norm -m -any -f ref.fa` on both VCFs before computing intersections.

## Dependencies

`numpy`, `pandas`, `matplotlib`
