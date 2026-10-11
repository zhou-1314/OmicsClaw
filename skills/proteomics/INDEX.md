# Proteomics — Skill Index

> The skill list below is derived from each skill's `SKILL.md` frontmatter
> and is checked by `tests/skills/test_domain_index_is_current.py`.
> Regenerate it with
> `OMICSCLAW_WRITE_SKILL_INDEX=1 pytest tests/skills/test_domain_index_is_current.py`.
> The prose above the list is written by hand.

**Domain key:** `proteomics`

**Skill count:** 8

**Primary data types:** mzml, mzxml, csv

Mass spec proteomics: raw MS QC, peptide/protein ID, LFQ/TMT/DIA quantification, differential abundance, PTM, pathway enrichment.

## Skills

- `proteomics-data-import` — Load when ingesting a MaxQuant `proteinGroups.txt`, FragPipe `combined_protein.tsv`, DIA-NN report, or generic CSV / TSV protein table — normalises columns to a standard schema. Skip when raw spectra are the input (run the search engine first); the file is already in OmicsClaw schema.
  triggers: data import, convert proteomics, format conversion
- `proteomics-de` — Load when computing two-group differential protein abundance (log2FC + p-value + BH-adjusted FDR) via Welch t-test, equal-variance t-test, or Mann-Whitney on a wide protein × sample CSV. Skip when you need multi-condition DE (run pairwise contrasts manually); label-based TMT linear-mixed models.
  triggers: differential abundance, protein expression, MSstats, limma, volcano
- `proteomics-enrichment` — Load for Fisher over-representation analysis of protein identifiers against caller-supplied pathways. Skip rank-based GSEA (use bulkrna-enrichment) or protein differential testing (use proteomics-de).
  triggers: proteomics enrichment, pathway analysis, ORA
- `proteomics-identification` — Load when summarising peptide identifications (PSM counts, unique peptides, score / charge distributions) of a peptide-level CSV from MaxQuant / FragPipe / DIA-NN. Skip when raw spectra are the input (run the search engine); protein-quantification tables (use proteomics-ms-qc).
  triggers: peptide identification, database search, MaxQuant, MS-GF+, Comet, Mascot
- `proteomics-ms-qc` — Load when computing protein-table QC — proteins × samples count, missing-value rate, intensity CV — from a MaxQuant / FragPipe / DIA-NN protein CSV. Skip when raw mzML / RAW spectra are the input (run a search engine first); peptide-level QC is needed (use proteomics-identification).
  triggers: MS QC, mass spec QC, PTXQC, rawTools
- `proteomics-ptm` — Load when summarising PTM sites (phosphorylation, acetylation, ubiquitination) from a per-site CSV — site-class assignment (Olsen I/II/III `localization_probability`), per-PTM-type counts. Skip when raw spectra are the input; only protein-level abundance is needed (use proteomics-quantification).
  triggers: PTM, phosphorylation, acetylation, ubiquitination, modification, motif
- `proteomics-quantification` — Load when computing per-protein abundance from a peptide / PSM table via LFQ (intensity summation), iBAQ (intensity / tryptic peptides), or spectral counting. Skip when the input is already protein-level (use proteomics-ms-qc); label-based TMT / iTRAQ workflows (search upstream first).
  triggers: protein quantification, LFQ, TMT, DIA, DIA-NN, Skyline
- `proteomics-structural` — Load when summarising cross-linking MS (XL-MS) results — intra/inter-protein link split, optional FDR filtering, distance-constraint validation against a per-crosslinker max distance. Skip when raw spectra are the input (run XlinkX / pLink / xiSEARCH); no XL-MS experiment ran.
  triggers: structural proteomics, cross-linking MS, XL-MS, XlinkX, pLink, xiSEARCH
