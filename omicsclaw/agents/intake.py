"""Input preprocessing and PDF→Markdown conversion.

Handles three input modes:
- Mode A: PDF + idea (data obtainable from paper / GEO)
- Mode B: PDF + idea + h5ad (user-provided data)
- Mode C: idea only (no PDF, no data — pure research from scratch)

Converts PDF to structured Markdown for agent consumption.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class IntakeResult:
    """Structured output from the intake stage."""

    # Paper content (present in Mode A/B, empty in Mode C)
    paper_markdown: str = ""
    paper_title: str = ""

    # User input (always present)
    idea: str = ""

    # Extracted metadata (from PDF; empty in Mode C)
    geo_accessions: list[str] = field(default_factory=list)
    organism: str = ""
    tissue: str = ""
    technology: str = ""

    # Data description (Mode B — user-provided h5ad)
    h5ad_metadata: dict[str, Any] | None = None
    input_mode: str = "A"  # "A", "B", or "C"

    # File paths
    paper_md_path: str = ""
    h5ad_path: str = ""


# =========================================================================
# PDF → Markdown conversion
# =========================================================================


def _convert_pdf_opendataloader(pdf_path: str, output_dir: str) -> str | None:
    """Convert PDF to Markdown using opendataloader-pdf (preferred engine).

    Returns the **post-processed** Markdown content string on success,
    or ``None`` if opendataloader-pdf is not installed or conversion fails.

    All warnings from the Java/ODL runtime (which write directly to the
    OS-level stderr via ``java.util.logging``) are suppressed by
    temporarily redirecting file-descriptor 2 to ``/dev/null``.

    Requires:
        pip install opendataloader-pdf   # + Java 11+
    """
    try:
        import opendataloader_pdf
    except ImportError:
        logger.debug("opendataloader-pdf not installed — skipping")
        return None

    import logging as _logging
    import os
    import sys
    import tempfile
    import warnings

    # ── 1. Suppress Python-level loggers ──────────────────────────
    _odl_loggers = [
        "opendataloader_pdf",
        "opendataloader",
        "jpype",
        "java",
    ]
    saved_levels: dict[str, int] = {}
    for name in _odl_loggers:
        lg = _logging.getLogger(name)
        saved_levels[name] = lg.level
        lg.setLevel(_logging.ERROR)

    # ── 2. Redirect OS-level stdout (fd 1) and stderr (fd 2) ───────────
    # Java's java.util.logging or sub-processes might write to native 
    # file descriptors (fd 1 or 2) directly. Python's logging/warnings 
    # cannot intercept this. We point fd 1 and 2 to /dev/null temporarily.
    saved_stdout_fd = os.dup(1)          # save original fd 1
    saved_stderr_fd = os.dup(2)          # save original fd 2
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    
    # Also redirect Python's sys.stdout and sys.stderr
    saved_sys_stdout = sys.stdout
    saved_sys_stderr = sys.stderr

    try:
        os.dup2(devnull_fd, 1)           # stdout → /dev/null
        os.dup2(devnull_fd, 2)           # stderr → /dev/null
        sys.stdout = open(os.devnull, "w")  # noqa: SIM115
        sys.stderr = open(os.devnull, "w")  # noqa: SIM115

        with tempfile.TemporaryDirectory(prefix="oc_odl_") as tmp_dir:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                opendataloader_pdf.convert(
                    input_path=[pdf_path],
                    output_dir=tmp_dir,
                    format="markdown",
                )
            # Find the generated markdown file
            md_files = list(Path(tmp_dir).glob("**/*.md"))
            if not md_files:
                logger.warning("opendataloader-pdf produced no .md output")
                return None
            md_content = md_files[0].read_text(encoding="utf-8")
            
            # Since we have logging redirected, we must restore before logging
            sys.stdout.close()
            sys.stderr.close()
            sys.stdout = saved_sys_stdout
            sys.stderr = saved_sys_stderr
            os.dup2(saved_stdout_fd, 1)
            os.dup2(saved_stderr_fd, 2)

            if md_content.strip():
                logger.info(
                    "opendataloader-pdf converted %s (%d chars → post-process)",
                    pdf_path, len(md_content),
                )
                md_content = _postprocess_odl_markdown(md_content)
                logger.info(
                    "Post-processed to %d chars",
                    len(md_content),
                )
                return md_content
            
            logger.warning("opendataloader-pdf output was empty")
            return None
    except Exception as e:
        # Restore before logging
        try:
            sys.stdout = saved_sys_stdout
            sys.stderr = saved_sys_stderr
            os.dup2(saved_stdout_fd, 1)
            os.dup2(saved_stderr_fd, 2)
        except Exception:
            pass
        logger.warning("opendataloader-pdf failed for %s: %s", pdf_path, e)
        return None
    finally:
        # ── Restore stdout & stderr ────────────────────────────────────────
        try:
            sys.stdout = saved_sys_stdout
            sys.stderr = saved_sys_stderr
            os.dup2(saved_stdout_fd, 1)
            os.dup2(saved_stderr_fd, 2)
            os.close(saved_stdout_fd)
            os.close(saved_stderr_fd)
            os.close(devnull_fd)
        except Exception:
            pass
            
        # Restore Python logger levels
        for name, level in saved_levels.items():
            _logging.getLogger(name).setLevel(level)


# ── ODL Markdown post-processing ──────────────────────────────────────────


# Sections that are typically not useful for downstream research analysis.
# We match on heading text (case-insensitive) and remove everything from
# that heading until the next same-or-higher-level heading.
_STRIP_SECTIONS = {
    # Exact heading matches (lowercased)
    "references",
    "bibliography",
    "acknowledgements",
    "acknowledgments",
    "author contributions",
    "competing interests",
    "additional information",
    "supplementary information",
    "supplementary materials",
    "supplementary figures",
    "supplementary tables",
    "code availability",
    "data availability",
    "reporting summary",
    "extended data",
    "extended data figures",
    "extended data tables",
    "online methods",       # sometimes main Methods are useful, but
                            # 'Online Methods' in Nature supplements are not
    "ethics oversight",
    "peer review information",
    "reprints and permissions",
    "nature portfolio",     # reporting checklist header
}

# Patterns for lines to remove individually (not section-based)
_STRIP_LINE_PATTERNS = [
    re.compile(r"^!\[image\s+\d+\]", re.IGNORECASE),          # ![image 1](...)
    re.compile(r"^!\[.*?\]\(.*?imageFile.*?\)", re.IGNORECASE), # ODL image refs
    re.compile(r"^\|\s*\|\s*$"),                                # empty table rows |  |
    re.compile(r"^\|---\|$"),                                   # minimal table separators
    re.compile(r"^Check for updates\s*$", re.IGNORECASE),
    re.compile(r"^nature portfolio.*reporting summary", re.IGNORECASE),
    re.compile(r"^Tick this box to confirm", re.IGNORECASE),
]


def _postprocess_odl_markdown(md: str) -> str:
    """Clean opendataloader-pdf Markdown output for research use.

    Removes:
    - Image embed lines ``![image N](...)``
    - Entire sections: References, Acknowledgements, Extended Data, etc.
    - Reporting summary / checklist boilerplate
    - Empty / decorative table rows
    - Redundant whitespace runs

    Keeps:
    - Title, authors, abstract
    - Main body sections (Introduction, Results, Discussion, Methods)
    - Inline citations (superscript numbers)
    """
    lines = md.split("\n")
    result: list[str] = []
    skip_until_level: int | None = None  # heading level to stop skipping at

    for line in lines:
        stripped = line.strip()

        # ── Check if we enter a section to skip ──────────────────
        heading_match = re.match(r"^(#{1,6})\s+(.*)", stripped)
        if heading_match:
            level = len(heading_match.group(1))
            heading_text = heading_match.group(2).strip()
            heading_lower = heading_text.lower().rstrip(".")

            # Are we currently skipping? Check if we should stop.
            if skip_until_level is not None:
                if level <= skip_until_level:
                    # Same or higher level heading → stop skipping
                    skip_until_level = None
                else:
                    continue  # still inside a skipped section

            # Should we start skipping this heading?
            if heading_lower in _STRIP_SECTIONS or any(
                heading_lower.startswith(s) for s in _STRIP_SECTIONS
            ):
                skip_until_level = level
                continue

            # Also skip generic figure legend headings like
            # "Fig. 1 | Description" or "Extended Data Fig. X"
            if re.match(
                r"^(extended\s+data\s+)?fig(\.|ure)\s*\d",
                heading_lower,
            ):
                skip_until_level = level
                continue

        # If we're in a skipped section, drop the line
        if skip_until_level is not None:
            continue

        # ── Drop individual noisy lines ──────────────────────────
        if any(pat.match(stripped) for pat in _STRIP_LINE_PATTERNS):
            continue

        # ── Drop lines that are only affiliation / footnote markers ──
        # e.g. "1School of Biological Sciences, Department of..."
        if re.match(r"^\d+[A-Z][a-z]+.*(University|Institute|Department)", stripped):
            continue

        result.append(line)

    # ── Collapse excessive blank lines ────────────────────────────
    cleaned = "\n".join(result)
    cleaned = re.sub(r"\n{4,}", "\n\n\n", cleaned)

    return cleaned.strip()


def _extract_pdf_text(pdf_path: str) -> str:
    """Extract raw text from PDF using pypdf (fallback engine)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            logger.warning("No PDF library found (pypdf/PyPDF2), using fallback")
            return _fallback_pdf_text(pdf_path)

    try:
        reader = PdfReader(pdf_path)
        pages = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if text.strip():
                pages.append(f"<!-- Page {i + 1} -->\n{text}")
        if pages:
            return "\n\n".join(pages)
    except Exception as e:
        logger.warning("pypdf failed to read %s: %s — trying fallback", pdf_path, e)

    return _fallback_pdf_text(pdf_path)


def _fallback_pdf_text(pdf_path: str) -> str:
    """Last resort: use pdftotext CLI if available."""
    import subprocess

    try:
        result = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return f"[Unable to extract text from PDF: {pdf_path}]"


def _extract_geo_accessions(text: str) -> list[str]:
    """Extract GEO accession numbers from text."""
    gse = re.findall(r"\b(GSE\d{4,8})\b", text, re.IGNORECASE)
    gsm = re.findall(r"\b(GSM\d{4,8})\b", text, re.IGNORECASE)
    # Deduplicate preserving order
    seen = set()
    result = []
    for acc in gse + gsm:
        acc_upper = acc.upper()
        if acc_upper not in seen:
            seen.add(acc_upper)
            result.append(acc_upper)
    return result


def _extract_organism(text: str) -> str:
    """Extract organism(s) from text.  Returns all detected organisms."""
    lower = text.lower()
    organisms: list[str] = []
    if any(w in lower for w in ["homo sapiens", "human patient", "human sample",
                                 "human tissue"]):
        organisms.append("Homo sapiens")
    if any(w in lower for w in ["mus musculus", "mouse", "murine"]):
        organisms.append("Mus musculus")
    if not organisms:
        # Broad fallback — only if nothing specific was found
        if "human" in lower:
            organisms.append("Homo sapiens (inferred)")
    return ", ".join(organisms) if organisms else "Unknown"


def _extract_technology(text: str) -> str:
    """Extract sequencing technology(ies) from text.  Returns all matches."""
    lower = text.lower()
    tech_patterns = [
        ("Visium HD", ["visium hd", "visiumhd"]),
        ("10x Visium", ["visium", "10x visium"]),
        ("Xenium", ["xenium"]),
        ("MERSCOPE", ["merscope"]),
        ("MERFISH", ["merfish"]),
        ("seqFISH", ["seqfish"]),
        ("Slide-seq", ["slide-seq", "slideseq"]),
        ("STORM-seq", ["storm-seq"]),
        ("10x Chromium", ["10x chromium", "10x genomics", "chromium"]),
        ("Smart-seq2", ["smart-seq2", "smartseq2"]),
        ("Drop-seq", ["drop-seq", "dropseq"]),
        ("snRNA-seq", ["snrna-seq", "single-nucleus rna"]),
        ("scRNA-seq", ["scrna-seq", "single-cell rna"]),
        ("Bulk RNA-seq", ["bulk rna-seq", "bulk rna seq"]),
    ]
    found: list[str] = []
    for name, patterns in tech_patterns:
        if any(p in lower for p in patterns):
            # Avoid duplicates (e.g. "Visium HD" already covers "visium")
            if not any(name in f or f in name for f in found):
                found.append(name)
    return ", ".join(found) if found else "Unknown"


def _extract_tissue(text: str) -> str:
    """Extract tissue type from text."""
    lower = text.lower()
    tissues = [
        "small intestine", "intestine", "colon",
        "brain", "liver", "lung", "kidney", "heart", "skin",
        "breast", "pancreas", "ovary", "prostate",
        "muscle", "bone marrow", "lymph node", "spleen",
        "tumor", "cancer",
    ]
    found = [t for t in tissues if t in lower]
    # Deduplicate substrings (e.g. "small intestine" includes "intestine")
    deduped: list[str] = []
    for t in found:
        if not any(t != other and t in other for other in found):
            deduped.append(t)
    return ", ".join(deduped[:3]) if deduped else "Unknown"


# ── Noise filtering helpers ────────────────────────────────────────────────


def _is_noise_line(line: str) -> bool:
    """Detect lines that are figure/table axis labels or other PDF noise."""
    stripped = line.strip()
    if not stripped:
        return False
    # Pure numbers, number sequences, or very short non-word fragments
    if re.fullmatch(r"[\d\s.,;:–−\-/]+", stripped):
        return True
    # Very short fragments that are likely axis ticks or legend labels
    if len(stripped) <= 3 and not re.search(r"[a-zA-Z]{2,}", stripped):
        return True
    return False


def _clean_body_text(text: str) -> str:
    """Remove figure/table noise from extracted PDF text.

    Keeps meaningful paragraphs and strips:
    - Runs of short numeric-only lines (axis ticks)
    - Isolated coordinate / dimension fragments
    """
    lines = text.split("\n")
    cleaned: list[str] = []
    noise_run = 0
    for line in lines:
        if _is_noise_line(line):
            noise_run += 1
            # Allow isolated noise lines (could be part of a list)
            if noise_run <= 2:
                cleaned.append(line)
            # Skip longer noise runs
            continue
        noise_run = 0
        cleaned.append(line)
    return "\n".join(cleaned)


def _extract_title(raw_text: str, pdf_path: str) -> str:
    """Extract paper title from raw PDF text.

    Strategy:
    1. Skip HTML page comments, journal headers and short noise lines.
    2. Pick the first 'substantial' line (>=20 chars, mostly letters).
    3. If it spans multiple lines before a blank line, join them.
    """
    lines = raw_text.split("\n")
    candidate_lines: list[str] = []
    collecting = False

    for line in lines:
        stripped = line.strip()
        # Skip empty / page markers / journal header lines
        if not stripped:
            if collecting:
                break  # end of title block
            continue
        if stripped.startswith("<!--"):
            continue
        # Skip typical journal header patterns
        if re.match(r"^(Nature|Science|Cell|Article|Letter)\s*\|", stripped):
            continue
        if re.match(r"^(Nature|Science|Cell)\s*$", stripped, re.IGNORECASE):
            continue
        # Skip short numeric / noise lines
        if _is_noise_line(stripped):
            continue
        # Must contain enough letters to be a title
        alpha_ratio = sum(c.isalpha() for c in stripped) / max(len(stripped), 1)
        if alpha_ratio < 0.4:
            continue
        if len(stripped) < 15 and not collecting:
            continue

        collecting = True
        candidate_lines.append(stripped)

        # Title usually doesn't exceed 3 lines
        if len(candidate_lines) >= 3:
            break

    if candidate_lines:
        return " ".join(candidate_lines)
    return Path(pdf_path).stem


def _extract_abstract(raw_text: str) -> str:
    """Extract abstract from raw PDF text using multiple strategies."""
    # Strategy 1: explicit "Abstract" header
    for pattern in [
        # "Abstract" followed by text, ending at Introduction/Keywords/etc.
        r"(?:^|\n)\s*(?:Abstract|ABSTRACT)\s*\n+(.*?)(?:\n\s*(?:Introduction|INTRODUCTION|Keywords|KEYWORDS|Main|Background|BACKGROUND|1[\s.]+\w))",
        # Abstract followed by text ending at a double newline + section header
        r"(?:^|\n)\s*(?:Abstract|ABSTRACT)\s*\n+(.*?)(?:\n\n+\s*[A-Z][A-Za-z\s]{5,}\n)",
    ]:
        m = re.search(pattern, raw_text, re.IGNORECASE | re.DOTALL)
        if m:
            text = m.group(1).strip()
            # Validate: real abstracts are >50 chars and have sentences
            if len(text) > 50 and "." in text:
                # Clean up page markers inside abstract
                text = re.sub(r"<!--.*?-->", "", text).strip()
                return text

    # Strategy 2: Nature-style inline abstract (no explicit header)
    # Look for a substantial paragraph early in the text that contains
    # typical abstract vocabulary
    paragraphs = re.split(r"\n\s*\n", raw_text[:8000])
    for para in paragraphs[:10]:
        para_clean = para.strip()
        # Skip short paragraphs and page markers
        if len(para_clean) < 200:
            continue
        if para_clean.startswith("<!--"):
            continue
        # Look for hallmarks of an abstract: "Here we", "we show", "our study",
        # "we demonstrate", "we developed", etc.
        abstract_signals = [
            "here we", "we show", "our study", "we demonstrate",
            "we developed", "we propose", "we report", "this study",
            "we present", "we found", "our results", "we identify",
            "we reveal", "our framework",
        ]
        lower_para = para_clean.lower()
        if any(sig in lower_para for sig in abstract_signals):
            # Clean up
            para_clean = re.sub(r"<!--.*?-->", "", para_clean).strip()
            return para_clean

    return ""


def _extract_sections(raw_text: str) -> list[tuple[str, str]]:
    """Extract major sections from paper body text.

    Returns list of (heading, content) tuples.
    Skips reference lists and acknowledgements.
    """
    # Remove page markers for cleaner section detection
    text = re.sub(r"<!--\s*Page\s*\d+\s*-->", "", raw_text)

    # Section heading pattern: line that looks like a heading
    # (title case or short uppercase, followed by body text)
    section_pattern = re.compile(
        r"\n\s*((?:[A-Z][a-z]+(?:\s+[a-zA-Z]+){1,10}|[A-Z\s]{10,50}))\s*\n",
    )

    sections: list[tuple[str, str]] = []
    matches = list(section_pattern.finditer(text))

    # Sections to skip
    skip_headings = {
        "references", "bibliography", "acknowledgements", "acknowledgments",
        "author contributions", "competing interests", "additional information",
        "supplementary information", "code availability", "data availability",
        "extended data", "reporting summary",
    }

    for i, m in enumerate(matches):
        heading = m.group(1).strip()
        if heading.lower() in skip_headings:
            continue
        # Content extends to next section or end
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        # Skip if content is too short or looks like a figure legend
        if len(content) < 100:
            continue
        sections.append((heading, content[:3000]))  # Cap per-section

    return sections[:15]  # Max 15 sections


# =========================================================================
# Concise paper summary builder
# =========================================================================


def _build_paper_summary(
    *,
    title: str,
    pdf_name: str,
    raw_text: str,
    organism: str,
    technology: str,
    tissue: str,
    geo_ids: list[str],
    fulltext_path: str,
) -> str:
    """Build a concise paper_summary.md from extracted text.

    The summary is designed for quick human review and includes only:
    - Title, authors, DOI
    - Structured metadata (organism, technology, tissue, GEO)
    - Abstract
    - Key sections (Methods, Results, Discussion) — capped in length
    - Pointer to the full-text file for detailed reference

    This keeps paper_summary.md under ~200 lines regardless of paper size.
    """
    parts: list[str] = []

    # ── Header ────────────────────────────────────────────────────
    parts.append(f"# {title}\n")
    parts.append(f"**Source**: {pdf_name}")

    # DOI
    doi_match = re.search(r"(https?://doi\.org/\S+)", raw_text)
    if doi_match:
        doi = doi_match.group(1).rstrip(".)")
        parts.append(f"**DOI**: {doi}")

    # Authors — first line after title that looks like an author list
    _author_line = _extract_authors(raw_text, title)
    if _author_line:
        parts.append(f"\n**Authors**: {_author_line}")

    parts.append("")

    # ── Metadata ──────────────────────────────────────────────────
    parts.append("## Metadata\n")
    parts.append(f"| Field | Value |")
    parts.append(f"|-------|-------|")
    parts.append(f"| Organism | {organism} |")
    parts.append(f"| Technology | {technology} |")
    parts.append(f"| Tissue | {tissue} |")
    if geo_ids:
        parts.append(f"| GEO Accessions | {', '.join(geo_ids)} |")
    parts.append("")

    # ── Abstract ──────────────────────────────────────────────────
    abstract = _extract_abstract(raw_text) or ""
    if abstract:
        parts.append("## Abstract\n")
        # Cap abstract to ~500 words
        words = abstract.split()
        if len(words) > 500:
            abstract = " ".join(words[:500]) + " [...]"
        parts.append(abstract)
        parts.append("")

    # ── Key Sections (capped) ─────────────────────────────────────
    sections = _extract_sections(raw_text)

    # Priority order: keep the most scientifically relevant sections
    _KEY_SECTIONS = [
        "results", "discussion", "methods", "introduction",
        "conclusion", "conclusions",
    ]

    added_sections: set[str] = set()
    max_words_per_section = 500

    for key in _KEY_SECTIONS:
        for heading, content in sections:
            heading_lower = heading.lower().strip()
            if key in heading_lower and heading_lower not in added_sections:
                added_sections.add(heading_lower)
                words = content.split()
                if len(words) > max_words_per_section:
                    content = " ".join(words[:max_words_per_section]) + " [...]"
                parts.append(f"## {heading}\n")
                parts.append(content.strip())
                parts.append("")
                break

    # ── Footer ────────────────────────────────────────────────────
    parts.append("---\n")
    parts.append(
        f"> **Full text**: See [{Path(fulltext_path).name}]"
        f"({fulltext_path}) for the complete paper content.\n"
    )

    return "\n".join(parts)


def _extract_authors(raw_text: str, title: str) -> str:
    """Try to extract the author line from near the title.

    Looks for lines with multiple comma- or &-separated names
    (containing uppercase initials typical of author lists).
    """
    lines = [l.strip() for l in raw_text.split("\n") if l.strip()]

    # Find the title line first, then look at the next few lines
    title_lower = title.lower().strip()
    start_idx = 0
    for i, line in enumerate(lines):
        if title_lower in line.lower():
            start_idx = i + 1
            break

    # Scan at most 5 lines after the title for author-like content
    for line in lines[start_idx:start_idx + 5]:
        # Skip short lines, URLs, page markers
        if len(line) < 20:
            continue
        if line.startswith("http") or line.startswith("#"):
            continue
        # Author lines typically have commas and uppercase words
        comma_count = line.count(",")
        has_ampersand = "&" in line
        # Crude heuristic: >2 commas or has & and commas
        if comma_count >= 2 or (has_ampersand and comma_count >= 1):
            # Remove affiliations (superscript numbers)
            author_line = re.sub(r"\d+,\d+|\d{1,2}(?=[A-Z])", "", line)
            # Truncate if very long
            if len(author_line) > 300:
                author_line = author_line[:300] + " et al."
            return author_line.strip()

    return ""


def _pdf_to_markdown(pdf_path: str, raw_text: str) -> str:
    """Convert extracted PDF text to structured Markdown.

    Produces a clean, well-organized summary with:
    - Proper title extraction (skips page markers and noise)
    - Robust abstract detection (multiple strategies)
    - Extracted metadata (organism, technology, tissue, GEO)
    - Cleaned body text (figure/table noise filtered)
    """
    # ── Title ──────────────────────────────────────────────────────
    title = _extract_title(raw_text, pdf_path)

    # ── Abstract ───────────────────────────────────────────────────
    abstract = _extract_abstract(raw_text)

    # ── Metadata ───────────────────────────────────────────────────
    geo_ids = _extract_geo_accessions(raw_text)
    organism = _extract_organism(raw_text)
    technology = _extract_technology(raw_text)
    tissue = _extract_tissue(raw_text)

    # ── DOI ────────────────────────────────────────────────────────
    doi_match = re.search(r"(https?://doi\.org/\S+)", raw_text)
    doi = doi_match.group(1).rstrip(".)") if doi_match else ""

    # ── Authors (first occurrence of institutional affiliations) ──
    # Look for a block of names followed by affiliation superscripts
    authors = ""
    author_match = re.search(
        r"([A-Z][a-z]+ [A-Z][a-z\-]+(?:\d|,).*?(?:University|Institute|Department|School))",
        raw_text[:3000],
        re.DOTALL,
    )
    if author_match:
        author_block = author_match.group(1)
        # Take just first ~300 chars to capture author names
        author_lines = author_block[:300].split("\n")
        authors = " ".join(l.strip() for l in author_lines if l.strip())

    # ── Build structured markdown ──────────────────────────────────
    md_parts = [
        f"# {title}\n",
        f"**Source**: {Path(pdf_path).name}",
    ]
    if doi:
        md_parts.append(f"**DOI**: {doi}")
    if authors:
        md_parts.append(f"**Authors**: {authors[:300]}{'...' if len(authors) > 300 else ''}")
    md_parts.append("")

    if abstract:
        md_parts.append(f"## Abstract\n\n{abstract}\n")

    md_parts.append("## Extracted Metadata\n")
    md_parts.append(f"- **Organism**: {organism}")
    md_parts.append(f"- **Technology**: {technology}")
    md_parts.append(f"- **Tissue**: {tissue}")
    if geo_ids:
        md_parts.append(f"- **GEO Accessions**: {', '.join(geo_ids)}")
    md_parts.append("")

    # ── Key sections (cleaned) ─────────────────────────────────────
    sections = _extract_sections(raw_text)
    if sections:
        md_parts.append("## Key Sections\n")
        for heading, content in sections:
            # Clean noise from section content
            content_clean = _clean_body_text(content)
            # Collapse multiple blank lines
            content_clean = re.sub(r"\n{3,}", "\n\n", content_clean).strip()
            if len(content_clean) > 50:
                md_parts.append(f"### {heading}\n")
                md_parts.append(content_clean)
                md_parts.append("")

    # ── Full text (cleaned, truncated) ─────────────────────────────
    full_text = _clean_body_text(raw_text)
    if len(full_text) > 50000:
        full_text = full_text[:50000] + "\n\n[... text truncated at 50,000 chars ...]"

    md_parts.append("## Full Text\n")
    md_parts.append(full_text)

    return "\n".join(md_parts)


# =========================================================================
# h5ad metadata extraction
# =========================================================================


def _extract_h5ad_metadata(h5ad_path: str) -> dict[str, Any]:
    """Extract metadata from an h5ad file without loading full data."""
    try:
        import anndata
        adata = anndata.read_h5ad(h5ad_path, backed="r")
        meta = {
            "n_obs": adata.n_obs,
            "n_vars": adata.n_vars,
            "obs_columns": list(adata.obs.columns)[:20],
            "var_columns": list(adata.var.columns)[:20],
            "obsm_keys": list(adata.obsm.keys()) if adata.obsm else [],
            "uns_keys": list(adata.uns.keys())[:20] if adata.uns else [],
            "has_spatial": "spatial" in (adata.obsm.keys() if adata.obsm else []),
            "file_path": str(h5ad_path),
        }
        adata.file.close()
        return meta
    except Exception as e:
        logger.warning("Failed to read h5ad metadata: %s", e)
        return {"error": str(e), "file_path": str(h5ad_path)}


# =========================================================================
# Main intake function
# =========================================================================


def prepare_intake(
    idea: str,
    pdf_path: str | None = None,
    h5ad_path: str | None = None,
    output_dir: str | None = None,
) -> IntakeResult:
    """Prepare intake: convert PDF to Markdown and assemble context.

    Supports three input modes:
    - Mode A: ``pdf_path`` + ``idea`` (data from paper / GEO)
    - Mode B: ``pdf_path`` + ``idea`` + ``h5ad_path`` (user-provided data)
    - Mode C: ``idea`` only (no PDF — pure research from scratch)

    Parameters
    ----------
    idea : str
        User's research idea / hypothesis (always required).
    pdf_path : str, optional
        Path to the scientific paper PDF (Mode A/B).
    h5ad_path : str, optional
        Path to user-provided h5ad data file (Mode B).
    output_dir : str, optional
        Directory to save generated Markdown files.
        Defaults to current directory for Mode C, or
        alongside the PDF for Mode A/B.

    Returns
    -------
    IntakeResult
        Structured intake output ready for pipeline consumption.
    """
    # ── Mode C: idea only ─────────────────────────────────────────
    if not pdf_path:
        return _prepare_intake_idea_only(idea, output_dir)

    # ── Mode A / B: PDF-based ─────────────────────────────────────
    pdf_path = str(Path(pdf_path).resolve())
    if not Path(pdf_path).exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    # Determine output directory early (needed by opendataloader)
    if output_dir:
        out_dir = Path(output_dir)
    else:
        out_dir = Path(pdf_path).parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Strategy 1: opendataloader-pdf (best quality) ─────────────
    odl_md = _convert_pdf_opendataloader(pdf_path, str(out_dir))

    if odl_md:
        raw_text = odl_md
        title = _extract_title(raw_text, pdf_path)
        logger.info("Using opendataloader-pdf output for %s", pdf_path)
    else:
        # ── Strategy 2: pypdf / pdftotext fallback ────────────────
        logger.info("Extracting text from PDF (fallback): %s", pdf_path)
        raw_text = _extract_pdf_text(pdf_path)
        title = _extract_title(raw_text, pdf_path)

    # Extract metadata
    geo_ids = _extract_geo_accessions(raw_text)
    organism = _extract_organism(raw_text)
    technology = _extract_technology(raw_text)
    tissue = _extract_tissue(raw_text)

    # ── Save full text to paper_fulltext.md (reference only) ──────
    if odl_md:
        fulltext_md = raw_text
    else:
        fulltext_md = _pdf_to_markdown(pdf_path, raw_text)
    fulltext_path = out_dir / "paper_fulltext.md"
    fulltext_path.write_text(fulltext_md, encoding="utf-8")
    logger.info("Full paper text saved to: %s (%d chars)", fulltext_path, len(fulltext_md))

    # ── Build concise paper_summary.md ────────────────────────────
    paper_md = _build_paper_summary(
        title=title,
        pdf_name=Path(pdf_path).name,
        raw_text=raw_text,
        organism=organism,
        technology=technology,
        tissue=tissue,
        geo_ids=geo_ids,
        fulltext_path=str(fulltext_path),
    )
    md_path = out_dir / "paper_summary.md"
    md_path.write_text(paper_md, encoding="utf-8")
    logger.info("Paper summary saved to: %s (%d chars)", md_path, len(paper_md))

    # 6. Handle h5ad (Mode B)
    h5ad_meta = None
    input_mode = "A"
    if h5ad_path:
        h5ad_path = str(Path(h5ad_path).resolve())
        if not Path(h5ad_path).exists():
            raise FileNotFoundError(f"h5ad file not found: {h5ad_path}")
        h5ad_meta = _extract_h5ad_metadata(h5ad_path)
        input_mode = "B"
        logger.info("Input mode B: user-provided h5ad data")
    else:
        logger.info("Input mode A: data from paper/GEO")

    # 7. Build research request document
    request_md = _build_research_request(
        title=title,
        idea=idea,
        paper_md_path=str(md_path),
        geo_ids=geo_ids,
        organism=organism,
        technology=technology,
        tissue=tissue,
        h5ad_meta=h5ad_meta,
        input_mode=input_mode,
    )
    request_path = out_dir / "research_request.md"
    request_path.write_text(request_md, encoding="utf-8")

    return IntakeResult(
        paper_markdown=paper_md,
        paper_title=title,
        idea=idea,
        geo_accessions=geo_ids,
        organism=organism,
        tissue=tissue,
        technology=technology,
        h5ad_metadata=h5ad_meta,
        input_mode=input_mode,
        paper_md_path=str(md_path),
        h5ad_path=h5ad_path or "",
    )


def _prepare_intake_idea_only(
    idea: str,
    output_dir: str | None = None,
) -> IntakeResult:
    """Mode C: prepare intake with only a research idea.

    The research-agent will handle literature discovery and data sourcing
    autonomously via web search.
    """
    out_dir = Path(output_dir) if output_dir else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Build a minimal research request
    request_md = (
        "# Research Request\n\n"
        "## Input Mode: C (idea only — no reference paper)\n\n"
        f"## User Idea\n\n{idea}\n\n"
        "## Instructions\n\n"
        "No reference paper was provided. The research-agent should:\n"
        "1. Search for relevant literature and prior work on this topic.\n"
        "2. Identify suitable datasets (GEO, public repositories).\n"
        "3. Recommend appropriate analysis methods and OmicsClaw skills.\n"
        "4. The planner-agent should then create the experimental plan.\n"
    )
    request_path = out_dir / "research_request.md"
    request_path.write_text(request_md, encoding="utf-8")
    logger.info("Mode C: idea-only intake saved to %s", request_path)

    return IntakeResult(
        paper_markdown="",
        paper_title="",
        idea=idea,
        input_mode="C",
        paper_md_path="",
    )


def _build_research_request(
    title: str,
    idea: str,
    paper_md_path: str,
    geo_ids: list[str],
    organism: str,
    technology: str,
    tissue: str,
    h5ad_meta: dict | None,
    input_mode: str,
) -> str:
    """Build the research_request.md that feeds into the pipeline."""
    _MODE_LABELS = {"A": "A (data from paper/GEO)", "B": "B (user-provided h5ad)", "C": "C (idea only)"}
    mode_label = _MODE_LABELS.get(input_mode, input_mode)
    parts = [
        "# Research Request\n",
        f"## Paper: {title}\n",
        f"Full paper summary: [{Path(paper_md_path).name}]({paper_md_path})\n",
        f"## User Idea\n\n{idea}\n",
        f"## Input Mode: {mode_label}\n",
        "## Extracted Metadata\n",
        f"- **Organism**: {organism}",
        f"- **Technology**: {technology}",
        f"- **Tissue**: {tissue}",
    ]
    if geo_ids:
        parts.append(f"- **GEO Accessions**: {', '.join(geo_ids)}")
    parts.append("")

    if h5ad_meta:
        parts.append("## User-Provided Dataset\n")
        parts.append(f"- **File**: {h5ad_meta.get('file_path', 'N/A')}")
        parts.append(f"- **Cells**: {h5ad_meta.get('n_obs', 'N/A')}")
        parts.append(f"- **Genes**: {h5ad_meta.get('n_vars', 'N/A')}")
        if h5ad_meta.get("has_spatial"):
            parts.append("- **Spatial data**: Yes")
        obs_cols = h5ad_meta.get("obs_columns", [])
        if obs_cols:
            parts.append(f"- **Annotations**: {', '.join(obs_cols[:10])}")
        parts.append("")

    return "\n".join(parts)
