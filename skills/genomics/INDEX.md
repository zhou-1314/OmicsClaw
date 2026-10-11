# Genomics — Skill Index

> The skill list below is derived from each skill's `SKILL.md` frontmatter
> and is checked by `tests/skills/test_domain_index_is_current.py`.
> Regenerate it with
> `OMICSCLAW_WRITE_SKILL_INDEX=1 pytest tests/skills/test_domain_index_is_current.py`.
> The prose above the list is written by hand.

**Domain key:** `genomics`

**Skill count:** 10

**Primary data types:** vcf, bam, cram, fasta, fastq, bed

Bulk DNA-seq: FASTQ QC, alignment, SNV/indel/SV/CNV calling, VCF ops, variant annotation, phasing, de novo assembly, ATAC/ChIP peak calling.

## Skills

- `genomics-alignment` — Load when computing alignment QC metrics (mapping rate, MAPQ, insert size, duplicates, proper pairs) from a text SAM file (BWA / Bowtie2 / Minimap2); convert a BAM with samtools view -h first. Skip when running the alignment itself; only FASTQ-level QC is needed (use genomics-qc).
  triggers: alignment, BWA, Bowtie2, Minimap2, map reads
- `genomics-assembly` — Load when computing genome-assembly QC metrics — N50/N90, L50/L90, total length, contig count, GC content, longest-contig — from a FASTA produced by any assembler (SPAdes / Megahit / Flye / Canu). Skip when running the assembly itself; assessing alignment quality (use genomics-alignment).
  triggers: genome assembly, de novo, SPAdes, Megahit, Flye, Canu
- `genomics-cnv-calling` — Load when calling CNV segments via CBS-style segmentation on a bin-level log2-ratio CSV from exome / WGS coverage — emits 5-class CN state (`amplification` / `gain` / `neutral` / `loss`) and genome-fraction summary. Skip when working with single-cell / spatial CNV (use spatial-cnv).
  triggers: CNV, copy number, amplification, deletion, CNVkit
- `genomics-epigenomics` — Load when summarising a peak file (BED / narrowPeak) from ATAC-seq / ChIP-seq / CUT&Tag — peak count, width distribution, per-chromosome counts, score statistics. Skip when calling peaks from BAM (run MACS / Genrich externally first); working with single-cell ATAC (use scatac-preprocessing).
  triggers: epigenomics, ATAC-seq, ChIP-seq, peak calling, MACS, motif, chromatin
- `genomics-phasing` — Load when summarising a phased VCF (output of WhatsHap / SHAPEIT5 / Eagle2) — phased fraction of het variants, phase-block N50, PS-field parsing, pipe-delimited genotype detection. Skip when the input is unphased (run a phaser first); calling small variants (use genomics-variant-calling).
  triggers: haplotype phasing, WhatsHap, SHAPEIT, Eagle, phasing
- `genomics-qc` — Load when running pre-alignment FASTQ quality control — Phred scores, Q20/Q30 rates, GC / N content, read-length distribution, adapter detection. Skip when working with already-aligned BAMs (use genomics-alignment); peak or variant files are the input (use the relevant downstream skill).
  triggers: sequencing QC, FastQC, read quality, adapter trimming, fastp
- `genomics-sv-detection` — Load when summarising structural variants from an SV VCF (DEL / DUP / INV / TRA) — INFO/SVTYPE-based classification, size classification, per-type counts. Skip when working with small SNVs / indels (use genomics-variant-calling); calling SVs from BAM (run Manta / Delly / Sniffles first).
  triggers: structural variant, SV, Manta, Delly, Lumpy, Sniffles
- `genomics-variant-annotation` — Load when summarising functional impact of an annotated CSV — per-IMPACT counts (HIGH / MODERATE / LOW), top consequences. Skip when the input is a raw VCF (convert with `bcftools +split-vep` first); calling variants (use genomics-variant-calling); filtering VCFs (use genomics-vcf-operations).
  triggers: variant annotation, VEP, snpEff, ANNOVAR, functional effect
- `genomics-variant-calling` — Load when summarising small variants (SNVs / indels) from a VCF or computing variant statistics like Ti/Tv. Skip when filtering or merging VCFs (use genomics-vcf-operations); calling structural variants (use genomics-sv-detection); annotating variants (use genomics-variant-annotation).
  triggers: variant calling, SNV, indel, GATK, DeepVariant, FreeBayes, Mutect2, VQSR
- `genomics-vcf-operations` — Load for file operations on an existing VCF — classification (SNP / MNP / INS / DEL), QUAL / DP filtering, merging or subsetting via bcftools. Skip when computing Ti/Tv statistics (use genomics-variant-calling); variant calling from BAM; annotation (use genomics-variant-annotation).
  triggers: VCF, bcftools, variant filter, merge VCF
