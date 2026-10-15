---
name: genomics-sv-detection
description: Load when summarising structural variants from an SV VCF (DEL / DUP / INV / TRA) — INFO/SVTYPE-based
  classification, size classification, per-type counts. Skip when working with small SNVs / indels (use genomics-variant-calling);
  calling SVs from BAM (run Manta / Delly / Sniffles first).
trigger: structural variant, SV, Manta, Delly, Lumpy, Sniffles
tags:
- genomics
- structural-variants
- sv
- manta
- delly
- sniffles
- bnd
---

# Structural Variant Detection & Breakpoint QC (`genomics-sv-detection`)

## 1. Method Decision Matrix & When to Use

Select the structural variant ($\ge 50$ bp) caller according to sequencing read technology and experimental design:

| Sequencing Technology & Design | Recommended SV Caller | Signal Used | Why |
|---|---|---|---|
| **Illumina paired-end WGS** (germline or matched tumor-normal) | **Manta** / **DELLY2** / **GRIDSS2** | Discordant read pairs + split-read assembly | Local breakpoint graph assembly resolves DEL, DUP, INV, INS, and inter-chromosomal BND events |
| **PacBio HiFi / ONT long reads** (germline or mosaic) | **Sniffles2** (`sniffles --input bam --vcf out.vcf`) / **cuteSV** | Intra-read CIGAR indels + split alignments | Long reads span full insertions (Alu/LINE1/VNTR) and complex inversions missed by short-read callers |
| **Multi-caller cohort merging** | **SURVIVOR** / **Jasmine** / **Truvari** (`truvari bench`) | Reciprocal overlap + breakpoint distance + sequence similarity | Deduplicates redundant SV calls across callers or samples while enforcing SVTYPE consistency |
| **SV VCF inspection & classification** | **VCF 4.3 `INFO/SVTYPE` & `INFO/SVLEN`** | `cyvcf2` / `pysam.VariantFile` / `bcftools query` | Stratifies calls by class (`DEL`, `INS`, `DUP`, `INV`, `BND`), FILTER status, and size regime |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: `Sniffles2` / `Manta` + `bcftools` SV Extraction

```python
import pandas as pd

# 1. Call long-read SVs with Sniffles2 (CLI):
# sniffles --input aligned.bam --vcf sv_calls.vcf.gz --reference ref.fa --threads 8

# 2. Query SV fields with bcftools:
# bcftools query -f '%CHROM\t%POS\t%ID\t%FILTER\t%INFO/SVTYPE\t%INFO/SVLEN[\t%GT]\n' sv_calls.vcf.gz > sv_table.tsv
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("genomics-sv-detection")`)

```python
# Calling use_skill("genomics-sv-detection") auto-injects kernel.py helpers into the python kernel:
records = read_records("data/structural_variants.vcf")
sv_df = analyze(records)
summary = sv_summary(sv_df)
```

## 3. Output Format & Verification Contract

- **Structural Variants Table** (`tables/structural_variants.csv`):
  - Contains `chrom`, `pos`, `sv_type` (`DEL`, `INS`, `DUP`, `INV`, `BND`), `sv_len` (absolute length in bp), `filter`, `genotype`, and `size_class` (`"small_50_1kb"`, `"medium_1kb_10kb"`, `"large_10kb_plus"`, or `"translocation"` for `BND`).
- **Verification Invariants**:
  - Non-translocation SVs satisfy `abs(sv_len) >= 50` (standard 50-bp operational boundary between indels and SVs).
  - `sv_len` is stored as a non-negative integer span (`abs(INFO/SVLEN)` or `INFO/END - POS`), while `sv_type` preserves the direction of change (`DEL` vs. `INS`/`DUP`).

## 4. Gotchas & Troubleshooting

1. **Signed `INFO/SVLEN` Conventions Vary Across Callers**:
   - VCF specifications store negative `SVLEN` for deletions (`SVLEN=-350`, `SVTYPE=DEL`) and positive `SVLEN` for insertions/duplications, while breakend (`SVTYPE=BND`) records omit `SVLEN` altogether. Always normalize with `abs(int(svlen))` when binning by SV size, and handle `SVTYPE=BND` separately.
2. **Short-Read Insertion Drop-Off Above Read Length**:
   - Short-read callers (`Manta`, `DELLY`) lose sensitivity for insertions larger than the fragment insert size ($\ge 500$ bp), producing unanchored breakends (`<INS>` with unknown length). Long reads (`Sniffles2`) are required to fully assemble large repeat insertions.
3. **Alu (~300 bp) and LINE-1 (~6 kb) Peaks Are Expected in Human Germline SV Size Distributions**:
   - A human germline SV callset naturally exhibits sharp spikes at ~300 bp (Alu mobile elements) and ~6 kb (full-length L1 elements) in both `DEL` and `INS` size histograms; absence of the 300-bp Alu peak indicates aggressive size filtering or poor split-read sensitivity.

## Dependencies

`numpy`, `pandas`, `matplotlib`
