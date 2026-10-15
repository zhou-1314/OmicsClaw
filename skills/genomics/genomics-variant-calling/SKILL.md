---
name: genomics-variant-calling
description: Load when summarising small variants (SNVs / indels) from a VCF or computing variant
  statistics like Ti/Tv. Skip when filtering or merging VCFs (use genomics-vcf-operations);
  calling structural variants (use genomics-sv-detection); annotating variants (use
  genomics-variant-annotation).
trigger: variant calling, SNV, indel, GATK, DeepVariant, FreeBayes, Mutect2, VQSR
tags:
- genomics
- variant-calling
- snv
- indel
- vcf
- ti-tv
---

# Small Variant Calling (SNVs & Indels) & Ti/Tv QC (`genomics-variant-calling`)

## 1. Method Decision Matrix & When to Use

Select the small-variant caller (<50 bp SNVs and indels) according to study design and sequencing modality:

| Study Design & Platform | Recommended Caller | Upstream Command / Model | Why |
|---|---|---|---|
| **Single-sample Germline WGS / WES** (Illumina, PacBio HiFi, ONT R10.4) | **DeepVariant** (`run_deepvariant`) | CNN pileup classifier (`--model_type=WGS/WES/PACBIO/ONT_R104`) | Highest SNP and indel F1 accuracy in PrecisionFDA Truth Challenges without requiring VQSR |
| **Joint-genotyped Germline Cohort** ($N > 10$ to $10^5$ samples) | **GATK HaplotypeCaller** (`-ERC GVCF`) + `GenomicsDBImport` + `GenotypeGVCFs` | Local de Bruijn graph re-assembly + VQSR / CNNScoreVariants | Joint calling rescues low-coverage sites across family pedigrees and cohorts |
| **Fast lightweight Germline / Microbial / Haploid** | **bcftools mpileup + call** or **FreeBayes** | `bcftools mpileup -Ou -f ref.fa aln.bam \| bcftools call -mv -Oz` | Fast C implementation ideal for bacterial/viral genomes, targeted amplicons, and rapid QC |
| **Matched Tumor-Normal Somatic SNVs/Indels** | **GATK Mutect2** + `FilterMutectCalls` or **Strelka2** | Somatic log-odds (`TLOD`) + Panel of Normals + orientation bias model (`LearnReadOrientationModel`) | Distinguishes low-VAF somatic mutations from FFPE deamination (`C>T`) and oxoG (`G>T`) artifacts |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: `bcftools mpileup + call + norm` End-to-End Variant Calling

```bash
# 1. Call SNVs and indels, split multi-allelic sites, and left-align indels against reference FASTA
bcftools mpileup -Ou -f ref/genome.fa -q 20 -Q 20 --annotate FORMAT/DP,FORMAT/AD aligned.bam \
  | bcftools call -mv -Ou \
  | bcftools norm -m -any -f ref/genome.fa -Oz -o variants.norm.vcf.gz
bcftools index -t variants.norm.vcf.gz

# 2. Compute Ti/Tv ratio, indel size distribution, and per-sample stats
bcftools stats variants.norm.vcf.gz > vcf_stats.txt
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("genomics-variant-calling")`)

```python
# Calling use_skill("genomics-variant-calling") auto-injects kernel.py helpers into the python kernel:
records = read_records("data/variants.vcf")
variants_df = analyze(records)
summary = variant_summary(variants_df)
```

## 3. Output Format & Verification Contract

- **Normalized Variant Table** (`tables/variants.csv`):
  - Contains `chrom`, `pos`, `ref`, `alt`, `qual`, `filter`, and `type` (`"SNP"`, `"INDEL"`, or `"MNP"`), with multi-allelic `ALT` records split into one row per alternate allele.
- **Per-Chromosome Variant Counts** (`tables/variants_per_chrom.csv`):
  - Summarizes total variants, SNPs, indels, and PASS rate per chromosome.
- **Verification Invariants**:
  - `ti_tv_ratio` (Transitions $A \leftrightarrow G, C \leftrightarrow T$ divided by Transversions) is finite and within expected biological bounds (~$2.0 - 2.1$ for human WGS, ~$2.8 - 3.2$ for coding WES).
  - Multi-allelic `ALT` strings (`A,G`) are decomposed so each row represents a single bi-allelic `(ref, alt)` transition/transversion or indel event.

## 4. Gotchas & Troubleshooting

1. **Reference FASTA Chromosome Naming Mismatch (`chr1` vs. `1`)**:
   - Calling variants with a UCSC `chr1`-prefixed FASTA against an Ensembl `1`-named BAM silently produces 0 variants or crashes immediately. Always verify `samtools view -H aligned.bam | grep @SQ` matches `ref.fa.fai`.
2. **Ti/Tv Ratio Below ~1.5 Indicates Severe False-Positive Noise**:
   - Random sequencing errors have 4 transversion paths for every 2 transition paths ($	ext{Ti/Tv} = 0.5$), whereas true vertebrate germline variants are biased toward CpG deamination transitions ($	ext{Ti/Tv} pprox 2.05$ in WGS, $pprox 3.0$ in exomes). A callset with $	ext{Ti/Tv} < 1.5$ requires stricter `QUAL` / `QD` / strand-bias filtering.
3. **Hard-Filtering `QUAL` Alone Fails on High-Depth Repetitive Regions**:
   - In collapse-prone segmental duplications, 200 misaligned reads easily generate `QUAL > 1000`. Always filter on **depth-normalized quality** (`QD = QUAL / DP >= 2.0`), strand bias (`FS <= 60.0`, `SOR <= 3.0`), and mapping quality (`MQ >= 40.0`).

## Dependencies

`numpy`, `pandas`, `matplotlib`
