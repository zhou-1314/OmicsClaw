---
name: literature
description: Load when extracting GEO accessions, dataset metadata, and downloadable references from a
  scientific paper (PDF / URL / DOI / PubMed ID / raw text) for downstream omics analysis. Skip
  when the dataset is already in hand or the paper names no dataset to fetch.
trigger: parse paper, literature, GEO accession, download dataset, PDF extract, PubMed, DOI
tags:
- literature
- pdf
- doi
- pubmed
- geo
- metadata
---

# Scientific Literature & Omics Accession Parser (`literature`)

## 1. Method Decision Matrix & When to Use

Select the text extraction and metadata resolution strategy according to the source document format (local PDF/manuscript vs. remote DOI/PMID/GEO accession):

| Input Source | Extraction / Query Method | Upstream Package / API | Why |
|---|---|---|---|
| **Local PDF manuscript or supplement** (`.pdf` or `.txt`) | Page-wise text extraction + regex accession/parameter mining | `pypdf.PdfReader` + `re` | Extracts full text, Data Availability statements, and exact stated method parameters locally without network calls |
| **PubMed ID (`PMID`) or PubMed Central ID (`PMCID`)** | NCBI E-utilities (`esummary` / `efetch`) or Europe PMC REST API | `requests` (`eutils.ncbi.nlm.nih.gov` / `ebi.ac.uk/europepmc`) | Retrieves structured XML/JSON abstracts, full-text OA sections, DOIs, and linked GEO/SRA cross-references |
| **DOI (`10.xxxx/...`)** | Crossref / Europe PMC metadata resolution | `requests` (`https://api.crossref.org/works/{doi}`) | Resolves article title, journal, publication year, and full-text links from a raw DOI string |
| **Extracted GEO / SRA / ArrayExpress accessions** (`GSE*`, `GSM*`, `PRJNA*`, `SRP*`, `E-MTAB-*`) | NCBI GEO soft/matrix metadata query | `GEOparse` or NCBI E-utilities `gds` database | Maps paper-reported accession numbers to downloadable count matrices and sample metadata |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Idiomatic `pypdf` Local PDF Extraction + Europe PMC / NCBI E-Utilities Query

```python
import re
from pathlib import Path
import pandas as pd
from pypdf import PdfReader
import requests

# 1. Extract text from a local scientific PDF (skipping empty pages)
reader = PdfReader("papers/manuscript.pdf")
pages_text = [page.extract_text() or "" for page in reader.pages]
full_text = "\n".join(t for t in pages_text if t.strip())

# 2. Mine Data Availability accessions (GEO, BioProject, SRA, ArrayExpress, Zenodo DOIs)
acc_pattern = re.compile(r"\b(GSE\d+|GSM\d+|GPL\d+|PRJNA\d+|SRP\d+|SRR\d+|E-MTAB-\d+)\b")
accessions = list(dict.fromkeys(acc_pattern.findall(full_text)))

# 3. Query Europe PMC REST API for structured metadata and database cross-references by PMID/DOI
resp = requests.get(
    "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
    params={"query": "EXT_ID:34567890", "resultType": "core", "format": "json"},
    timeout=20,
)
resp.raise_for_status()
article_meta = resp.json().get("resultList", {}).get("result", [])
```

### Pattern B: Lightweight Accession & Method Parameter Helper via `use_skill("literature")`

```python
# Calling use_skill("literature") auto-injects kernel.py helpers into the python kernel:
accessions = extract_accessions(full_text)
identifiers = extract_dois_pmids(full_text)
params_df = extract_method_parameters(full_text)
```

## 3. Output Format & Verification Contract

- **Extracted Metadata & Accessions** (`extracted_metadata.json` and `tables/accessions.csv`):
  - Contains deduplicated `accessions` by repository kind (`GSE`, `GSM`, `GPL`, `PRJNA`, `SRP`, `E-MTAB`), `dois`, `pmids`, and inferred study heuristics (`organism`, `assay_type`).
- **Stated Method Parameters** (`tables/method_parameters.csv`):
  - Columns: `param`, `operator`, `value`, `quote`, `start`, `end` recording exact character spans in `source.txt`.
- **Verification Invariants**:
  - Accession identifiers are uppercase, deduplicated, and match canonical repository regex patterns.
  - Numeric method parameters include exact verbatim source quotes (`source_text[start:end] == quote`) and never fabricate missing defaults.

## 4. Gotchas & Troubleshooting

1. **Line-Break Hyphenation in Two-Column PDF Layouts**:
   - PDF text extractors (`pypdf`) frequently split accession IDs or DOIs across column line wraps (e.g., `GSE123-\n456` or `E-MTAB-\n8901`). Normalize soft hyphenation (`re.sub(r"([A-Z]+)-\s*\n\s*(\d+)", r"\1-\2", text)`) before running regex extraction.
2. **Distinguishing SuperSeries vs. SubSeries (`GSE`) and Reference Citations**:
   - Papers often cite historical benchmark datasets (e.g., in the Introduction or Discussion) alongside the newly generated dataset in the *Data Availability* section. Prioritize accessions appearing within `Data Availability` / `Accession Numbers` / `Code and Data` sections before downloading gigabytes of data.
3. **Never Treat HTTP Error Pages as Article Text**:
   - Publisher landing pages behind paywalls or Cloudflare challenges return `403 Forbidden` or JavaScript interstitials. Always call `response.raise_for_status()` and query open-access APIs (NCBI E-utilities, Europe PMC REST, or Unpaywall) rather than scraping publisher HTML directly.
4. **Heuristic Parameter Spans vs. Validated Pipeline Configs**:
   - Statements like `"padj < 0.05 and |log2FC| > 1.0"` extracted from prose may refer to a specific figure panel rather than every analysis in the paper. Always verify extracted cutoffs against the surrounding sentence context (`quote` column).

## Dependencies

`pandas`, `matplotlib`, `pypdf`, `requests`
