"""Deterministic extraction of v2 ``interface`` facts from v1 sources (ADR 0037).

The v1→v2 migrator leaves ``interface.inputs`` / ``interface.outputs`` empty
because they have no single structured v1 source. This module recovers what is
*reliably* extractable so a migrated ``skill.yaml`` carries the output contract
instead of leaving it stranded in the (slated-for-removal) ``SKILL.md`` table:

- ``interface.outputs.files`` ← ``references/output_contract.md`` — itself
  auto-generated from the script's string literals, so it is the AUTHORITATIVE,
  non-drifting file list (the SKILL.md prose table can be stale).
- ``interface.outputs.anndata.{obs,obsm,var}`` ← the ``## Inputs & Outputs``
  table's ``obs["x"]`` / ``obsm["x"]`` / ``var["x"]`` tokens (structured enough
  to regex).
- ``interface.inputs.file_types`` ← file extensions named in the input table.

Fields with no reliable v1 source (``inputs.modalities``,
``outputs.result_json.required_keys``) are NOT guessed here — they stay for
human/Codex curation. Every extractor is a pure function over text so it is
trivially testable and reusable by ``migrate_to_skill_yaml.py``.
"""

from __future__ import annotations

import re

# Output-file extraction ------------------------------------------------------

# "## File contents" bullet:  - `tables/qc_summary.csv` — written by ...
_FILE_BULLET = re.compile(r"^- `([^`]+)`", re.MULTILINE)
# Fallback for hand-written contracts: a markdown table row whose first cell is a
# backticked filename-with-extension, e.g. `| `audit.json` | provenance |`.
_TABLE_FILE = re.compile(r"^\|\s*`([^`]+\.[a-z0-9]+)`\s*\|", re.MULTILINE)
# Reproducibility sidecars the framework writes for EVERY skill — not part of a
# skill's semantic output contract, so they are excluded from interface.outputs.
# (requirements.txt is written to reproducibility/requirements.txt but some
# output_contract.md list it at the top level, hence matched by basename.)
_FRAMEWORK_SIDECARS = frozenset(
    {"commands.sh", "environment.txt", "manifest.json", "r_visualization.sh", "requirements.txt"}
)


def extract_output_files(output_contract_md: str, *, include_figures: bool = True) -> list[str]:
    """Return the skill's semantic output files from ``output_contract.md`` text.

    Includes ``figures/*`` (they are real produced artifacts) but excludes the
    ``### Demo-only outputs`` section and framework reproducibility sidecars.
    Order-preserving + de-duplicated.
    """
    # Only the real "## File contents" block; stop at demo-only / notes.
    body = output_contract_md
    for stop in ("### Demo-only outputs", "## Notes"):
        idx = body.find(stop)
        if idx != -1:
            body = body[:idx]
    contents_idx = body.find("## File contents")
    if contents_idx != -1:
        body = body[contents_idx:]

    paths = _FILE_BULLET.findall(body)
    if not paths:
        # Hand-written contract (no "## File contents" bullets): fall back to the
        # first-column filenames of any markdown file table.
        paths = _TABLE_FILE.findall(body)

    seen: set[str] = set()
    out: list[str] = []
    for path in paths:
        path = path.strip()
        if not path or path in seen:
            continue
        base = path.rsplit("/", 1)[-1]
        if base in _FRAMEWORK_SIDECARS:
            continue
        if not include_figures and path.startswith("figures/"):
            continue
        seen.add(path)
        out.append(path)
    return out


# AnnData schema extraction ---------------------------------------------------

_IO_SECTION = re.compile(r"^## Inputs & Outputs\b", re.MULTILINE)
_NEXT_SECTION = re.compile(r"^## ", re.MULTILINE)


def _io_section(skill_md_body: str) -> str:
    """Return the text of the ``## Inputs & Outputs`` section (or '')."""
    m = _IO_SECTION.search(skill_md_body)
    if not m:
        return ""
    rest = skill_md_body[m.end():]
    nxt = _NEXT_SECTION.search(rest)
    return rest[: nxt.start()] if nxt else rest


# Negation words that, when they precede a slot mention, mean the key is NOT
# produced. Two families:
#   - plain negations: 'There is no unified obsm["cell_type_probabilities"] key'.
#   - removal verbs: a key the skill deletes from the saved object to keep it
#     small, e.g. sc-markers 'drops `uns["rank_genes_groups"]` to keep file small'
#     — the processed.h5ad does NOT carry it, so it is not a produced output.
_NEGATION_RE = re.compile(
    r"\b(?:no|not|without|never|n't"
    r"|drops?|dropped|purges?|purged|deletes?|deleted"
    r"|removes?|removed|discards?|discarded|strips?|stripped)\b",
    re.IGNORECASE,
)


def _slot_keys(text: str, slot: str) -> list[str]:
    """All distinct keys named as ``slot["key"]`` (e.g. obsm["X_pca"]), in order.

    A mention negated within the preceding ~40 chars (same clause) is skipped so
    a documented absence ('no obsm["x"]') is not mistaken for a produced key.
    """
    seen: set[str] = set()
    out: list[str] = []
    # Optional ``f`` prefix captures templated keys, e.g. obsm[f"deconvolution_{method}"]
    # or obs[f"local_moran_<gene>"], which document the produced-key pattern.
    for m in re.finditer(rf'{slot}\[f?"([^"]+)"\]', text):
        window = text[max(0, m.start() - 40): m.start()]
        # only the text since the last clause boundary (comma / sentence / cell)
        # counts as negation context, so a 'no obsm["a"], obsm["b"]' negates only a.
        window = re.split(r"[.;,|]", window)[-1]
        if _NEGATION_RE.search(window):
            continue
        key = m.group(1)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def extract_anndata_keys(skill_md_body: str) -> dict[str, list[str]]:
    """Return the PRODUCED ``{"obs", "obsm", "var", "layers"}`` keys from the I&O table.

    Reads only the OUTPUT rows (after the first ``| Output`` header) of the
    ``## Inputs & Outputs`` section, so a preserved *input* slot (e.g. the input
    ``obsm["spatial"]`` coordinate) is not misreported as a produced output.
    """
    section = _io_section(skill_md_body)
    out_idx = section.find("| Output")
    outputs_text = section[out_idx:] if out_idx != -1 else section
    return {
        "obs": _slot_keys(outputs_text, "obs"),
        "obsm": _slot_keys(outputs_text, "obsm"),
        "var": _slot_keys(outputs_text, "var"),
        "layers": _slot_keys(outputs_text, "layers"),
        "uns": _slot_keys(outputs_text, "uns"),
    }


def extract_input_anndata_obsm(skill_md_body: str) -> list[str]:
    """Return ``obsm`` keys named in the INPUT rows (data-shape preconditions)."""
    section = _io_section(skill_md_body)
    out_idx = section.find("| Output")
    inputs_text = section[:out_idx] if out_idx != -1 else section
    return _slot_keys(inputs_text, "obsm")


# Modality extraction ---------------------------------------------------------

# Known omics platform/assay names. A skill's modalities are the subset of its
# tags that name a real platform — reliable because it only ever returns tags
# that ARE modalities (no free-text guessing).
_KNOWN_MODALITIES = frozenset(
    {
        # spatial
        "visium", "xenium", "merfish", "slide-seq", "slideseq", "cosmx",
        "stereo-seq", "stereoseq", "dbit-seq", "seqfish",
        # single-cell
        "10x", "drop-seq", "smart-seq", "smart-seq2", "cite-seq", "multiome",
        "scatac", "snrna", "scrna",
        # bulk / other omics
        "rna-seq", "atac-seq", "chip-seq", "wgs", "wes", "lc-ms", "ms",
        "tmt", "dia", "lfq",
    }
)


def extract_modalities(tags: list[str]) -> list[str]:
    """Return the tags that name a known platform/assay (order-preserving)."""
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        key = str(t).strip().lower()
        if key in _KNOWN_MODALITIES and key not in seen:
            seen.add(key)
            out.append(str(t).strip())
    return out


# Input file-type extraction --------------------------------------------------

# ``h5ad`` MUST precede ``h5``, and ``fasta`` MUST precede ``fa``, so the longer
# token wins; the ``\b`` after each alternative also prevents the short token from
# matching inside the long one (``h5`` in ``h5ad``, ``fa`` in ``fasta``).
_KNOWN_EXTS = (
    "h5ad", "h5", "csv", "tsv", "txt", "json", "loom", "mtx", "feather",
    "fastq", "fasta", "fa", "sam", "bam", "bed", "vcf", "rds", "log", "out", "pdf",
)
_EXT_RE = re.compile(r"\.(" + "|".join(_KNOWN_EXTS) + r")\b")


def extract_input_file_types(skill_md_body: str) -> list[str]:
    """Return known input file extensions named in the ``## Inputs & Outputs``
    INPUT rows (the rows above the first ``| Output |`` header)."""
    section = _io_section(skill_md_body)
    out_idx = section.find("| Output")
    inputs_text = section[:out_idx] if out_idx != -1 else section
    seen: set[str] = set()
    ordered: list[str] = []
    for ext in _EXT_RE.findall(inputs_text):
        if ext not in seen:
            seen.add(ext)
            ordered.append(ext)
    return ordered
