---
name: genomics-variant-annotation
description: Load when summarising functional impact of an annotated CSV — per-IMPACT counts (HIGH /
  MODERATE / LOW), top consequences. Skip when the input is a raw VCF (convert with `bcftools
  +split-vep` first); calling variants (use genomics-variant-calling); filtering VCFs (use
  genomics-vcf-operations).
trigger: variant annotation, VEP, snpEff, ANNOVAR, functional effect
tags:
- genomics
- annotation
- vep
- snpeff
- annovar
- consequence
- impact
---

# Functional Variant Annotation & Pathogenicity Prioritization (`genomics-variant-annotation`)

## 1. Method Decision Matrix & When to Use

Select the functional annotation engine and clinical/pathogenicity database according to study goals:

| Annotation Goal | Recommended Upstream Tool | Key Fields Produced | Why |
|---|---|---|---|
| **Comprehensive transcript & regulatory annotation** | **Ensembl VEP** (`vep --everything --mane`) | `Consequence`, `IMPACT`, `SYMBOL`, `HGVSc`, `HGVSp`, `MANE_SELECT` | Official Ensembl/GENCODE consequence engine with native support for MANE Select canonical transcripts and plugins |
| **Fast lightweight Java VCF annotation** | **SnpEff + SnpSift** (`snpEff ann GRCh38.105`) | `INFO/ANN` (`Allele|Annotation|Annotation_Impact|Gene_Name|...`) | High-throughput single-command VCF annotation for bacterial, plant, and vertebrate genomes |
| **Deleteriousness & splice scoring** | **CADD v1.7** (`cadd_phred`), **SpliceAI**, **AlphaMissense** | `CADD_PHRED` ($\ge 20$ top 1%, $\ge 30$ top 0.1%), `$\Delta$` score | Integrates conservation, epigenetic, and protein-structure features across coding and non-coding SNVs/indels |
| **Clinical & population allele frequency filtering** | **ClinVar** + **gnomAD v4** (`bcftools annotate`) | `CLNSIG`, `gnomAD_AF`, `gnomAD_nhomalt` | Filters common benign polymorphisms (`AF > 0.01`) and flags known pathogenic alleles |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: `Ensembl VEP` CLI + Parsing `CSQ` / Annotated Table

```bash
# Annotate normalized VCF with Ensembl VEP using MANE Select canonical transcripts and CADD plugin
vep -i normalized.vcf.gz -o annotated.vcf.gz --vcf --compress_output bgzip \
    --cache --offline --assembly GRCh38 --mane --pick_allele_gene \
    --plugin CADD,/path/to/whole_genome_SNVs.tsv.gz
```

```python
import pandas as pd

# Prioritize HIGH-impact LoF or high-CADD missense variants in MANE Select transcripts
df = pd.read_csv("tables/annotated_variants.csv")
candidates = df[(df["impact"] == "HIGH") | ((df["impact"] == "MODERATE") & (df["cadd_phred"] >= 20.0))]
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("genomics-variant-annotation")`)

```python
# Calling use_skill("genomics-variant-annotation") auto-injects kernel.py helpers into the python kernel:
records = read_records("data/annotated_variants.csv")
annotated_df = analyze(records)
impact_df = impact_distribution(annotated_df)
```

## 3. Output Format & Verification Contract

- **Annotated Variants Table** (`tables/annotated_variants.csv`):
  - Contains `chrom`, `pos`, `ref`, `alt`, `gene`, `consequence` (Sequence Ontology term, e.g., `stop_gained`, `missense_variant`), `impact` (`HIGH`, `MODERATE`, `LOW`, `MODIFIER`), and `cadd_phred`.
- **Impact Tier Breakdown** (`tables/impact_distribution.csv`):
  - Counts and percentages of variants stratified by VEP/SnpEff `impact` tier (`HIGH`, `MODERATE`, `LOW`, `MODIFIER`).
- **Verification Invariants**:
  - `impact` values belong strictly to `{"HIGH", "MODERATE", "LOW", "MODIFIER"}`.
  - `cadd_phred` scores are finite non-negative floats ($\ge 0$), where `cadd_phred >= 20` denotes the top 1% most predicted deleterious substitutions genome-wide.

## 4. Gotchas & Troubleshooting

1. **Transcript Isoform Explosion (`1 variant -> 20 CSQ entries`)**:
   - By default, VEP and SnpEff emit a separate consequence for every overlapping Ensembl transcript isoform, turning 1 variant into dozens of rows. Use `--mane --pick_allele_gene` (or filter to `MANE_SELECT` / canonical transcripts) before counting coding consequences per gene.
2. **Unnormalized Indels Receive Wrong HGVS and Consequence Calls**:
   - Multi-allelic or right-shifted indels in repetitive regions fail to match ClinVar/gnomAD coordinates and produce wrong frameshift positions. Always left-align and normalize indels with `bcftools norm -m -any -f ref.fa` **before** running VEP/SnpEff.
3. **`IMPACT=HIGH` Does Not Automatically Equal Clinical Pathogenicity (`ACMG P/LP`)**:
   - Terminal-exon `stop_gained` variants escaping Nonsense-Mediated Decay (last 50 bp of penultimate exon or final exon) and LoF variants in olfactory receptors are frequently benign in healthy cohorts (`LOEUF` tolerant). Always cross-check `gnomAD` allele frequency and gene constraint (`pLI` / `LOEUF`).

## Dependencies

`numpy`, `pandas`, `matplotlib`
