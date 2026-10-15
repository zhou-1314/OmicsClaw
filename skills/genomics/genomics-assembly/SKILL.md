---
name: genomics-assembly
description: Load when computing genome-assembly QC metrics — N50/N90, L50/L90, total length, contig count,
  GC content, longest-contig — from a FASTA produced by any assembler (SPAdes / Megahit / Flye / Canu).
  Skip when running the assembly itself; assessing alignment quality (use genomics-alignment).
trigger: genome assembly, de novo, SPAdes, Megahit, Flye, Canu
tags:
- genomics
- assembly
- n50
- l50
- contig
- quast
- spades
- flye
---

# Genome Assembly Contiguity & Completeness QC (`genomics-assembly`)

## 1. Method Decision Matrix & When to Use

Choose the de novo assembler and assembly evaluation strategy according to sequencing technology and ploidy:

| Sequencing Input | Recommended Assembler | Evaluation Suite | Why |
|---|---|---|---|
| **PacBio HiFi + Hi-C / Ultra-Long ONT** (diploid/polyploid) | **hifiasm** (`hifiasm -o asm -t 32 --h1 ... --h2 ...`) | `QUAST` + `compleasm` / `BUSCO` + `Merqury` | Produces haplotype-resolved primary/alternate assemblies with megabase N50 |
| **ONT long reads** (bacterial / eukaryotic draft) | **Flye** (`flye --nano-hq`) | `QUAST` + `CheckM2` (microbial) / `BUSCO` | Repeat graph assembler resilient to ONT homopolymer errors |
| **Illumina short paired-end reads** (isolate / metagenome) | **SPAdes** (`spades.py --isolate` / `metaSPAdes`) | `QUAST` + `N50/NG50/L50` metrics | Multi-$k$-mer de Bruijn graph assembly for short-read contigs |
| **Contig FASTA contiguity audit** | **N50 / NG50 / L50 / AuN** | `pyfastx` / `Biopython SeqIO` / `QUAST` | Evaluates contiguity (`N50`, `N90`, `L50`), base composition (`GC%`, `N` gaps), and expected genome completeness (`NG50`) |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Contig FASTA Contiguity (`N50`, `NG50`, `L50`, `GC%`) in Python

```python
import numpy as np
import pandas as pd

# 1. Parse FASTA contigs and compute non-N GC% and sorted lengths
records = []
with open("assembly.fasta") as fh:
    name, chunks = None, []
    for line in fh:
        line = line.strip()
        if line.startswith(">"):
            if name is not None:
                records.append({"contig": name, "sequence": "".join(chunks).upper()})
            name, chunks = line[1:].split()[0], []
        elif line:
            chunks.append(line)
    if name is not None:
        records.append({"contig": name, "sequence": "".join(chunks).upper()})

df = pd.DataFrame(records)
lengths = df["sequence"].str.len().sort_values(ascending=False).to_numpy()
csum = np.cumsum(lengths)
n50_idx = int(np.searchsorted(csum, csum[-1] * 0.5))
n50, l50 = int(lengths[n50_idx]), n50_idx + 1
```

### Pattern B: Lightweight `kernel.py` Micro-Helper (`use_skill("genomics-assembly")`)

```python
# Calling use_skill("genomics-assembly") auto-injects kernel.py helpers into the python kernel:
contigs = read_records("data/contigs.fasta")
metrics_df = analyze(contigs, genome_size=4_600_000)
summary = assembly_summary(contigs, genome_size=4_600_000)
```

## 3. Output Format & Verification Contract

- **Assembly Contiguity Summary** (`tables/assembly_metrics.csv`):
  - Contains `n_contigs`, `total_length`, `largest_contig`, `n50`, `n90`, `l50`, `l90`, `gc_pct`, `n_bases`, and (when `genome_size > 0`) `ng50` and `completeness_pct`.
- **Per-Contig Table** (`tables/contig_lengths.csv`):
  - Sorted descending by `length`, with columns `contig`, `length`, `gc_pct`, and `n_count`.
- **Verification Invariants**:
  - `largest_contig >= n50 >= n90 > 0` and `1 <= l50 <= l90 <= n_contigs`.
  - `gc_pct` is calculated strictly over unambiguous `(A, C, G, T)` bases (`(G + C) / (A + C + G + T) * 100`), excluding scaffold gap `N` characters.

## 4. Gotchas & Troubleshooting

1. **N50 vs. NG50 Confusion When Comparing Assemblies**:
   - `N50` is computed relative to the **assembled total length**, so aggressive filtering of short contigs artificially inflates `N50`. Always pass the known reference `genome_size` to compute `NG50` when comparing across assemblers or parameter sweeps.
2. **Scaffold Gap `N` Bases Bias GC Percentage**:
   - Scaffolded assemblies contain stretches of `N` bases from Hi-C or mate-pair gap filling. Dividing `(G + C)` by `len(sequence)` instead of `A + C + G + T` underestimates true GC content when gap fraction is high.
3. **Contiguity (`N50`) Does Not Measure Gene-Space Completeness or Misassemblies**:
   - Chimeric joins increase `N50` while introducing structural misassemblies. Always pair contiguity statistics with single-copy ortholog completeness (`compleasm` / `BUSCO`) and $k$-mer spectra (`Merqury` QV).

## Dependencies

`numpy`, `pandas`, `matplotlib`
