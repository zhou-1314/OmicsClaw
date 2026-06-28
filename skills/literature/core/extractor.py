"""Extract GEO accessions and metadata from text."""

import re
from typing import Dict, List, Set


def extract_geo_accessions(text: str) -> Dict[str, List[str]]:
    """Extract GEO accessions (GSE, GSM, GPL) from text.

    Returns:
        Dict with keys 'gse', 'gsm', 'gpl' containing lists of accessions
    """
    # GEO accession patterns
    gse_pattern = r'\b(GSE\d{3,})\b'
    gsm_pattern = r'\b(GSM\d{3,})\b'
    gpl_pattern = r'\b(GPL\d{3,})\b'

    gse_ids = list(set(re.findall(gse_pattern, text, re.IGNORECASE)))
    gsm_ids = list(set(re.findall(gsm_pattern, text, re.IGNORECASE)))
    gpl_ids = list(set(re.findall(gpl_pattern, text, re.IGNORECASE)))

    # Normalize to uppercase
    return {
        'gse': [x.upper() for x in gse_ids],
        'gsm': [x.upper() for x in gsm_ids],
        'gpl': [x.upper() for x in gpl_ids],
    }


def extract_organism(text: str) -> str:
    """Extract organism/species from text."""
    organisms = {
        'homo sapiens': ['human', 'homo sapiens', 'h. sapiens'],
        'mus musculus': ['mouse', 'mus musculus', 'm. musculus', 'mice'],
        'rattus norvegicus': ['rat', 'rattus norvegicus', 'r. norvegicus'],
        'danio rerio': ['zebrafish', 'danio rerio', 'd. rerio'],
        'drosophila melanogaster': ['fly', 'drosophila', 'd. melanogaster'],
    }

    text_lower = text.lower()
    for canonical, aliases in organisms.items():
        for alias in aliases:
            if alias in text_lower:
                return canonical

    return 'unknown'


def extract_tissue(text: str) -> str:
    """Extract tissue type from text."""
    tissues = [
        'brain', 'heart', 'liver', 'kidney', 'lung', 'spleen',
        'muscle', 'skin', 'blood', 'bone', 'pancreas', 'intestine',
        'stomach', 'colon', 'breast', 'prostate', 'ovary', 'testis',
        'thyroid', 'adrenal', 'pituitary', 'retina', 'cornea',
        'tumor', 'cancer', 'carcinoma', 'lymphoma', 'leukemia',
    ]

    text_lower = text.lower()
    for tissue in tissues:
        if tissue in text_lower:
            return tissue

    return 'unknown'


def extract_technology(text: str) -> str:
    """Extract sequencing / assay technology from text.

    Detection is first-match-wins by dict order, so the MORE SPECIFIC spatial &
    single-cell platforms are checked BEFORE the broader families (a paper that
    says "10x" or "single-cell RNA-seq" must not be read as bulk RNA-seq). The
    bulk/genomics/proteomics/metabolomics families use deliberately specific
    aliases (e.g. "bulk rna-seq", not a bare "rna-seq") to avoid mis-routing.
    """
    technologies = {
        # Spatial (most specific first)
        'Visium': ['visium', 'spatial transcriptomics'],
        'Xenium': ['xenium'],
        'MERFISH': ['merfish'],
        'seqFISH': ['seqfish'],
        'Slide-seq': ['slide-seq', 'slideseq'],
        # Single-cell platforms + generic single-cell (before bulk)
        '10x Genomics': ['10x', '10x genomics', 'chromium'],
        'Smart-seq': ['smart-seq', 'smartseq'],
        'Drop-seq': ['drop-seq', 'dropseq'],
        'single-cell RNA-seq': ['single-cell rna', 'single cell rna', 'scrna-seq', 'scrna seq',
                                'snrna-seq', 'single-nucleus rna', 'single nucleus rna'],
        # Bulk RNA-seq — require an explicit "bulk" so scRNA papers aren't captured
        'Bulk RNA-seq': ['bulk rna-seq', 'bulk rna seq', 'bulk rna sequencing', 'bulk transcriptom'],
        # Genomics / DNA — short aliases (wgs/wes) rely on WORD-BOUNDARY matching below
        # so they don't substring-hit ordinary words ("western", "we").
        'WGS/WES': ['whole-genome sequencing', 'whole genome sequencing', 'whole-exome',
                    'whole exome', 'wgs', 'wes', 'dna-seq', 'dna sequencing'],
        'ATAC-seq': ['atac-seq', 'atacseq', 'atac sequencing'],
        'ChIP-seq': ['chip-seq', 'chipseq', 'chip sequencing'],
        # Metabolomics BEFORE proteomics: LC-MS is used by BOTH, so a metabolomics
        # anchor must win first ("LC-MS/MS-based metabolomics" is metabolomics, not
        # proteomics). Anchor on metabolomics-specific terms only.
        'Metabolomics': ['metabolomic', 'metabolome', 'metabolite', 'lipidomic', 'xcms', 'gc-ms'],
        # Proteomics (mass-spec) — proteomics-specific anchors (drop bare LC-MS, which
        # is ambiguous). 'mass spectrometry' is reached only after metabolomics anchors.
        'Mass spectrometry': ['proteomic', 'peptide', 'tmt', 'label-free quantification',
                              'tandem mass spectrom', 'dia-ms', 'shotgun proteom', 'mass spectrometry'],
    }

    text_lower = text.lower()
    for tech, aliases in technologies.items():
        for alias in aliases:
            # Word-boundary match with an optional trailing plural ``s``: matches
            # stems ("metabolomic" → "metabolomics", "proteomic" → "proteomics")
            # while still rejecting substring false-positives ("wes" ∉ "western",
            # "10x" ∉ "210x"). re.escape handles hyphens/slashes in aliases.
            if re.search(rf"\b{re.escape(alias)}s?\b", text_lower):
                return tech

    return 'unknown'


# Detected-technology → omics domain. The report's "Next Steps" maps each domain
# to its entry skill (DOMAIN_ENTRY below) so all 6 domains route correctly.
_TECH_TO_DOMAIN = {
    'Visium': 'spatial', 'Xenium': 'spatial', 'MERFISH': 'spatial',
    'seqFISH': 'spatial', 'Slide-seq': 'spatial',
    '10x Genomics': 'singlecell', 'Smart-seq': 'singlecell', 'Drop-seq': 'singlecell',
    'single-cell RNA-seq': 'singlecell',
    'Bulk RNA-seq': 'bulkrna',
    'WGS/WES': 'genomics', 'ATAC-seq': 'genomics', 'ChIP-seq': 'genomics',
    'Mass spectrometry': 'proteomics',
    'Metabolomics': 'metabolomics',
}

# domain → (entry skill, human label). Skill names verified against skills/<domain>/.
DOMAIN_ENTRY = {
    'spatial': ('spatial-preprocess', 'spatial transcriptomics'),
    'singlecell': ('sc-preprocessing', 'single-cell'),
    'bulkrna': ('bulkrna-qc', 'bulk RNA-seq'),
    'genomics': ('genomics-alignment', 'genomics / DNA-seq'),
    'proteomics': ('proteomics-identification', 'mass-spec proteomics'),
    'metabolomics': ('metabolomics-peak-detection', 'LC-MS metabolomics'),
}


def infer_domain(technology: str) -> str | None:
    """Map a detected technology to its omics domain, or None if unrecognized."""
    return _TECH_TO_DOMAIN.get(technology)


def extract_metadata(text: str) -> Dict[str, any]:
    """Extract all metadata from text.

    Returns:
        Dict containing geo_accessions, organism, tissue, technology
    """
    return {
        'geo_accessions': extract_geo_accessions(text),
        'organism': extract_organism(text),
        'tissue': extract_tissue(text),
        'technology': extract_technology(text),
    }
