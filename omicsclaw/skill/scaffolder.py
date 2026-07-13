"""Helpers for creating OmicsClaw-native skill scaffolds."""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
import sys
import textwrap
from typing import Iterable

from omicsclaw.common.manifest import StepRecord
from omicsclaw.common.report import SCAFFOLD_STATUS, validate_result_envelope
from omicsclaw.runtime.tools.hooks import build_default_lifecycle_hook_runtime
from omicsclaw.runtime.policy.verification import (
    COMPLETION_REPORT_FILENAME,
    WORKSPACE_KIND_ANALYSIS_RUN,
    ArtifactRequirement,
    build_completion_report,
    format_completion_summary,
    isolated_workspace,
    update_workspace_manifest,
    write_completion_report,
)
from omicsclaw.version import __version__


def _resolve_omicsclaw_dir() -> Path:
    override = str(os.getenv("OMICSCLAW_DIR", "") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


OMICSCLAW_DIR = _resolve_omicsclaw_dir()
SKILLS_DIR = OMICSCLAW_DIR / "skills"
OUTPUT_DIR = OMICSCLAW_DIR / "output"
SKILL_TEMPLATE_PATH = OMICSCLAW_DIR / "templates" / "skill" / "SKILL.md"
STAGING_ROOT = OMICSCLAW_DIR / ".omicsclaw-staging" / "skill-scaffolds"
QUARANTINE_DIRNAME = ".quarantine"
SKILL_SCAFFOLDER_VERSION = __version__

# P1 acquisition gate (docs/proposals/skill-acquisition-p0-p1-landing.md): a
# --demo smoke run is a lightweight sanity check, not a real analysis, so it
# should finish in seconds; this bounds a genuine hang rather than a slow
# computation (MF4 — this is demo validation, not a sandboxed execution tier).
_DEMO_SMOKE_GATE_TIMEOUT_SECONDS = 120

VALID_DOMAINS = (
    "spatial",
    "singlecell",
    "genomics",
    "proteomics",
    "metabolomics",
    "bulkrna",
    "orchestrator",
)

_DOMAIN_PROFILES = {
    "spatial": {
        "title": "Spatial Transcriptomics",
        "emoji": "🧭",
        "input_formats": [
            ("AnnData", ".h5ad", "Spatial coordinates in obsm['spatial']", "data/sample_spatial.h5ad"),
            ("Visium-style directory", "folder", "Filtered matrix + spatial metadata", "data/visium_run/"),
            ("Demo", "n/a", "--demo", "Built-in scaffold demo"),
        ],
        "requirements": [
            ("Spatial coordinates", "obsm['spatial']", "Needed for neighborhood-aware analyses and plots"),
            ("Expression matrix", "adata.X or layers['counts']", "Needed for feature-level computations"),
            ("Observation metadata", "adata.obs", "Needed for grouping, QC, or annotation exports"),
        ],
    },
    "singlecell": {
        "title": "Single-Cell Omics",
        "emoji": "🧫",
        "input_formats": [
            ("AnnData", ".h5ad", "Cell x gene matrix with obs/var metadata", "data/sample_scrna.h5ad"),
            ("Sparse matrices", ".mtx/.tsv", "Matrix + barcodes + features", "data/filtered_feature_bc_matrix/"),
            ("Demo", "n/a", "--demo", "Built-in scaffold demo"),
        ],
        "requirements": [
            ("Expression matrix", "adata.X or layers['counts']", "Needed for normalization and downstream analysis"),
            ("Cell metadata", "adata.obs", "Needed for grouping, QC, and annotation"),
            ("Feature metadata", "adata.var", "Needed for gene selection and reporting"),
        ],
    },
    "genomics": {
        "title": "Genomics",
        "emoji": "🧬",
        "input_formats": [
            ("Variants", ".vcf", "Standard VCF header and variant records", "data/sample.vcf"),
            ("Alignments", ".bam/.cram", "Coordinate-sorted reads with index", "data/sample.bam"),
            ("Demo", "n/a", "--demo", "Built-in scaffold demo"),
        ],
        "requirements": [
            ("Primary genomic file", "VCF/BAM/CRAM/FASTA", "Needed to run the core genomics method"),
            ("Reference metadata", "Reference genome or annotations", "Needed for reproducible interpretation"),
            ("Sample identifiers", "File names or metadata table", "Needed for multi-sample reporting"),
        ],
    },
    "proteomics": {
        "title": "Proteomics",
        "emoji": "🧪",
        "input_formats": [
            ("Mass spectrometry", ".mzML/.mzXML", "Centroided or profile MS data", "data/sample.mzML"),
            ("Quantification table", ".csv/.tsv", "Sample x protein or peptide matrix", "data/protein_matrix.csv"),
            ("Demo", "n/a", "--demo", "Built-in scaffold demo"),
        ],
        "requirements": [
            ("Primary MS input", "mzML/mzXML or quantified table", "Needed for identification or quantification"),
            ("Feature metadata", "Protein or peptide annotations", "Needed for interpretation and exports"),
            ("Sample labels", "Metadata table or columns", "Needed for comparisons and summaries"),
        ],
    },
    "metabolomics": {
        "title": "Metabolomics",
        "emoji": "🧫",
        "input_formats": [
            ("Mass spectrometry", ".mzML/.cdf", "Raw or preprocessed metabolomics spectra", "data/sample.mzML"),
            ("Feature table", ".csv/.tsv", "Sample x metabolite/feature matrix", "data/metabolite_matrix.csv"),
            ("Demo", "n/a", "--demo", "Built-in scaffold demo"),
        ],
        "requirements": [
            ("Primary metabolomics input", "Raw spectra or feature matrix", "Needed for peak processing or statistics"),
            ("Feature annotations", "Compound IDs or putative metabolite labels", "Needed for downstream reporting"),
            ("Sample metadata", "Condition or batch columns", "Needed for contrasts and QC"),
        ],
    },
    "bulkrna": {
        "title": "Bulk RNA-seq",
        "emoji": "📚",
        "input_formats": [
            ("Count matrix", ".csv/.tsv", "Genes x samples count table", "data/counts.tsv"),
            ("Reads", ".fastq/.bam", "Aligned or raw sequencing files", "data/sample_R1.fastq.gz"),
            ("Demo", "n/a", "--demo", "Built-in scaffold demo"),
        ],
        "requirements": [
            ("Expression counts", "Count matrix or alignment-derived counts", "Needed for DE or enrichment"),
            ("Sample sheet", "Condition and replicate annotations", "Needed for model design"),
            ("Gene identifiers", "Gene symbols or Ensembl IDs", "Needed for interpretation"),
        ],
    },
    "orchestrator": {
        "title": "Orchestrator",
        "emoji": "🛠",
        "input_formats": [
            ("Natural language request", "text", "User goal or pipeline specification", "\"build a routing helper\""),
            ("Optional local file", "path", "Server-side reference file", "data/reference.json"),
            ("Demo", "n/a", "--demo", "Built-in scaffold demo"),
        ],
        "requirements": [
            ("User intent", "Prompt or spec document", "Needed to determine the workflow contract"),
            ("Optional config", "JSON/YAML or CLI flags", "Needed for reusable orchestrator behaviors"),
            ("Output contract", "Markdown/JSON artifacts", "Needed for agent-to-agent handoff"),
        ],
    },
}

_SLUG_TOKEN_RE = re.compile(r"[^a-z0-9]+")
_REQUEST_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-_.+]{1,}")
_REQUEST_STOPWORDS = {
    "a",
    "an",
    "analysis",
    "and",
    "build",
    "create",
    "for",
    "from",
    "generate",
    "in",
    "into",
    "new",
    "of",
    "omicsclaw",
    "or",
    "skill",
    "that",
    "the",
    "to",
    "workflow",
}


@dataclass
class SkillScaffoldResult:
    skill_name: str
    domain: str
    skill_dir: str
    script_path: str
    skill_md_path: str
    spec_path: str
    test_path: str = ""
    manifest_path: str = ""
    completion_report_path: str = ""
    completion: dict[str, object] = field(default_factory=dict)
    created_files: list[str] | None = None
    template_path: str = str(SKILL_TEMPLATE_PATH)
    registry_refreshed: bool = False
    # P1 --demo smoke gate outcome: "earned" (validation.level upgraded to
    # demo-validated) or "skipped" (env/input limitation or an unimplemented
    # placeholder — left at its prior validation level). A "rejected" verdict
    # never reaches this dataclass: create_skill_scaffold raises instead. See
    # _run_demo_smoke_gate.
    demo_gate_verdict: str = ""
    demo_gate_reason: str = ""
    # A promoted body whose required sandbox/demo gate was skipped is moved
    # under ``skills/.quarantine`` rather than the discoverable domain tree.
    quarantined: bool = False
    quarantine_reason_path: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["created_files"] = list(self.created_files or [])
        return data


@dataclass
class AutonomousAnalysisBundle:
    source_dir: str
    notebook_path: str
    analysis_plan: str
    result_summary: str
    web_sources: str
    capability_decision: dict
    python_code: str
    goal: str
    domain: str = ""
    input_file: str = ""
    context: str = ""
    # "mini_agent" code is authored against the oc/adata/show/ReturnAnswer facade
    # and needs a bootstrap in the promoted script; "notebook" code is self-contained.
    engine: str = "notebook"
    # Structured producer evidence used by the P2 abstraction pass.  Keep the
    # original dictionaries intact so every generated transformation can cite
    # the exact persisted record rather than a lossy re-parse of analysis.py.
    steps: list[dict[str, object]] = field(default_factory=list)
    skill_calls: list[dict[str, object]] = field(default_factory=list)
    trace_warnings: list[str] = field(default_factory=list)


@dataclass
class AcquisitionParameter:
    """One deterministic CLI parameter lifted from an executed skill call."""

    key: str
    flag: str
    dest: str
    default: object
    type: str
    call_indexes: list[int] = field(default_factory=list)


@dataclass
class AcquisitionCall:
    """A facade-free call in a promoted workflow.

    ``input_source`` is either ``input`` or ``step:<1-based-index>``.  The
    latter is emitted only when AST lineage proves that the call consumed a
    prior handle's ``.adata``; absence of proof rejects structured promotion.
    """

    index: int
    skill: str
    input_source: str
    parameter_bindings: dict[str, str] = field(default_factory=dict)


@dataclass
class AcquisitionAbstraction:
    """Auditable result of turning one accepted run into a reusable workflow."""

    strategy: str
    reusable: bool
    facade_free: bool
    reason: str
    source_code_sha256: str
    calls: list[AcquisitionCall] = field(default_factory=list)
    parameters: list[AcquisitionParameter] = field(default_factory=list)
    source_steps: list[dict[str, object]] = field(default_factory=list)
    source_skill_calls: list[dict[str, object]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self, *, applied: bool, fallback_reason: str = "") -> dict[str, object]:
        return {
            "schema_version": 1,
            "strategy": self.strategy,
            "reusable": self.reusable,
            "applied": applied,
            "facade_free": bool(applied and self.facade_free),
            "reason": self.reason,
            "fallback_reason": fallback_reason,
            "source_code_sha256": self.source_code_sha256,
            "calls": [asdict(call) for call in self.calls],
            "parameters": [asdict(param) for param in self.parameters],
            "source_steps": list(self.source_steps),
            "source_skill_calls": list(self.source_skill_calls),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class CorpusParamCandidate:
    """One methodology parameter extracted from a paper/tool-docs text.

    ``quote``/``char_span`` make this independently re-verifiable against the
    source text (P5 iron rule, docs/proposals/skill-acquisition-plan.md §P5):
    a lint check re-slices ``char_span`` out of the persisted source text and
    confirms it equals ``quote`` before trusting ``value`` as a live default.
    """

    param: str
    operator: str
    value: object  # int | float
    quote: str
    char_span: tuple[int, int]
    todo: bool = False


@dataclass
class CorpusDerivedBundle:
    """A paper/tool-docs text plus the methodology candidates extracted from it.

    Deliberately NOT a subtype of :class:`AutonomousAnalysisBundle` — there is
    no executable ``python_code`` here, only candidate parameter values, so
    ``create_skill_scaffold`` treats this as an independent third branch (see
    §P5's "corpus-derived scaffold" design in the acquisition plan) rather
    than layering it onto the promoted-skill path.
    """

    source_kind: str  # "paper" | "tool_docs" — narrative label only, not schema
    doc_ref: str  # DOI/URL/PMID, or a filename fallback
    corpus_text: str  # full input text — persisted verbatim to references/source_corpus.txt
    candidates: list[CorpusParamCandidate]
    goal: str = ""


def slugify_skill_name(name: str) -> str:
    text = (name or "").strip().lower()
    text = _SLUG_TOKEN_RE.sub("-", text)
    text = text.strip("-")
    return re.sub(r"-{2,}", "-", text)


def _unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def infer_skill_name(request: str, domain: str, preferred_name: str = "") -> str:
    slug = slugify_skill_name(preferred_name)
    if slug:
        return slug

    tokens = [
        token
        for token in _REQUEST_TOKEN_RE.findall((request or "").lower())
        if token not in _REQUEST_STOPWORDS and not token.isdigit()
    ]
    if tokens:
        return slugify_skill_name("-".join(tokens[:5]))

    return f"{domain}-custom-skill"


def _display_title(skill_name: str) -> str:
    return " ".join(part.capitalize() for part in skill_name.replace("_", "-").split("-") if part)


def _yaml_inline_list(items: Iterable[str]) -> str:
    values = [json.dumps(item, ensure_ascii=False) for item in _unique(items)]
    return "[" + ", ".join(values) + "]"


def _yaml_block_list(items: Iterable[str], indent: str = "      ") -> str:
    values = _unique(items)
    if not values:
        return indent + "[]"
    return "\n".join(f"{indent}- {json.dumps(item, ensure_ascii=False)}" for item in values)


def _markdown_bullets(items: Iterable[str], fallback: str) -> str:
    values = _unique(items)
    if not values:
        values = [fallback]
    return "\n".join(f"- {item}" for item in values)


def _input_table_rows(domain: str, extra_inputs: Iterable[str]) -> str:
    profile = _DOMAIN_PROFILES[domain]
    rows = list(profile["input_formats"])
    for item in _unique(extra_inputs):
        rows.insert(
            max(len(rows) - 1, 0),
            ("Additional input", "custom", item, item),
        )
    return "\n".join(
        f"| {label} | `{ext}` | {structure} | `{example}` |"
        for label, ext, structure, example in rows
    )


def _requirement_rows(domain: str) -> str:
    return "\n".join(
        f"| {req} | `{where}` | {why} |"
        for req, where, why in _DOMAIN_PROFILES[domain]["requirements"]
    )


def _render_v2_description(skill_name: str, domain: str) -> str:
    """Placeholder-frontmatter text only — inert past scaffold-render time.

    Its only call site (below, inside ``render_skill_markdown``) feeds a
    throwaway placeholder frontmatter that ``skill_md.render_frontmatter``
    fully overwrites afterward from the real manifest's ``Summary`` (see
    ``build_scaffold_manifest`` / ``_synthesize_load_when`` for the text that
    actually reaches SKILL.md/the catalog). Do not "fix" load_when here.
    """
    return (
        f"Load when the user explicitly asks to create a new {domain} skill "
        f"named '{skill_name}'. "
        f"Skip when an existing {domain} skill already covers the request."
    )


def render_skill_markdown(
    *,
    skill_name: str,
    domain: str,
    summary: str,
    request: str,
    methods: Iterable[str],
    input_formats: Iterable[str],
    primary_outputs: Iterable[str],
    trigger_keywords: Iterable[str],
    source_bundle: AutonomousAnalysisBundle | None = None,
    corpus_bundle: CorpusDerivedBundle | None = None,
) -> str:
    """Render the narrative SKILL.md consumed by ``skill_md.render_skill_md``.

    Supplies the 5 hand-written narrative sections (When to use / Flow /
    Gotchas / Key CLI / See also) plus a placeholder frontmatter and a
    hand-written ``## Inputs & Outputs`` table.  ``render_skill_md`` then
    replaces the frontmatter with a header generated from ``skill.yaml`` and
    swaps the table for the generated I/O summary, so the runtime contract
    lives in ``skill.yaml`` (ADR 0037), not in this body.
    """
    del input_formats, primary_outputs, trigger_keywords  # Live in sidecar / contract.
    title = _display_title(skill_name)
    profile = _DOMAIN_PROFILES[domain]
    methods_list = _unique(methods) or ["default"]
    default_method = methods_list[0]
    description = _render_v2_description(skill_name, domain)
    summary_text = (summary or "").strip() or f"Scaffold for a new {profile['title']} workflow."
    if source_bundle:
        promotion_note = f"Promoted from a successful autonomous analysis at `{source_bundle.source_dir}`."
    elif corpus_bundle:
        promotion_note = (
            f"Scaffolded from a {corpus_bundle.source_kind.replace('_', ' ')} "
            f"(`{corpus_bundle.doc_ref}`) — see `references/corpus_provenance.md` "
            "for every extracted parameter's source quote."
        )
    else:
        promotion_note = "Generated by `omics-skill-builder` from `templates/skill/`."
    input_rows = "\n".join(
        f"| {label} | `{ext}` | yes (unless `--demo`) |"
        for label, ext, _structure, _example in profile["input_formats"][:3]
    )

    return f"""---
name: {skill_name}
description: >-
  {description}
version: 0.1.0
author: OmicsClaw
license: MIT
tags:
- {domain}
- autogenerated
- skill-scaffold
requires:
- python3
---

# {profile["emoji"]} {title}

{promotion_note}

## When to use

The user has explicitly requested a new {domain} skill: {summary_text}
Pick this skill only when the request is **scaffold a new OmicsClaw skill**.
For running an existing {domain} workflow, dispatch to the appropriate
`{domain}-*` skill directly instead of re-scaffolding.

## Inputs & Outputs

| Input | Format | Required |
|---|---|---|
{input_rows}

| Output | Path | Notes |
|---|---|---|
| Scaffold report | `report.md` + `result.json` | always written by the report helper |
| Reproducibility log | `reproducibility/commands.sh` | always |

## Flow

1. Load user input (`--input <file>`) or generate a demo (`--demo`).
2. Parse the `--method` selection and validate against the scaffold's allowed list.
3. Run the placeholder method backend (`{default_method}`).
4. Write standard OmicsClaw outputs (`report.md`, `result.json`, `reproducibility/`).
5. Replace placeholders with the real scientific implementation before shipping.

## Gotchas

- _None yet — append as failure modes are reported._

## Key CLI

```bash
# Demo
python omicsclaw.py run {skill_name} --demo --output /tmp/{skill_name}_demo

# Real input
python omicsclaw.py run {skill_name} \\
  --input <data.ext> --output results/ \\
  --method {default_method}
```

## See also

- `references/parameters.md` — every CLI flag, per-method tunables
- `references/methodology.md` — the WHY behind the algorithm
- `references/output_contract.md` — `result.json` envelope + downstream paths
- `templates/skill/SKILL.md` — canonical v2 scaffold consumed by `omics-skill-builder`
"""


# Import roots whose PyPI distribution name differs from the module name.
# Mirrored (by hand — the library must NOT import from scripts/) from
# scripts/audit_skill_requires.py::COMMON_MODULE_TO_PKG so a promoted skill's
# seeded deps line up with what that audit later recomputes.
_IMPORT_ROOT_TO_PKG = {
    "sklearn": "scikit-learn",
    "skimage": "scikit-image",
    "skmisc": "scikit-misc",
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "igraph": "python-igraph",
    "mpl_toolkits": "matplotlib",
}
_STDLIB_MODULES = set(sys.stdlib_module_names) | {"__future__"}


def _scan_third_party_imports(source: str) -> list[str]:
    """Best-effort third-party import surface of a rendered skill script.

    AST-walks ALL imports — module-level AND nested, since the promotion
    bootstrap and the accepted analysis cells import inside ``main()`` — keeps
    each import ROOT that is not stdlib / ``omicsclaw`` / a relative or private
    (leading-underscore) module, maps roots to PyPI names, and returns them
    sorted + de-duplicated. A starting point for a promoted skill's
    ``deps.python``; the author finalizes it with
    ``scripts/audit_skill_requires.py`` (this stays decoupled from ``scripts/``).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # relative import — internal to the skill package
            if node.module:
                roots.add(node.module.split(".")[0])
    pkgs: set[str] = set()
    for root in roots:
        if not root or root.startswith("_"):
            continue
        if root in _STDLIB_MODULES or root == "omicsclaw":
            continue
        pkgs.add(_IMPORT_ROOT_TO_PKG.get(root, root))
    if "scanpy" in pkgs:
        pkgs.add("anndata")  # scanpy hard-depends on anndata (matches the audit)
    return sorted(pkgs)


_SCAFFOLD_TOPIC_SENTENCE_SPLIT = re.compile(r"[.!?\n]")
_SCAFFOLD_TOPIC_TRAILING_SKILL = re.compile(
    r"\s+(?:as\s+(?:a|an|the)\s+(?:reusable\s+|new\s+|successful\s+)?)?skill\.?\s*$",
    re.IGNORECASE,
)
_SCAFFOLD_TOPIC_LEADING_ARTICLE = re.compile(r"^(?:a|an|the)\s+", re.IGNORECASE)
# A sentence starting with one of these is a question or a relative clause
# ("Can you build...", "I need a skill that...") — gluing it after "the user
# needs to" reads worse than the generic fallback, so reject it outright
# rather than try to salvage it.
_SCAFFOLD_TOPIC_REJECTED_STARTS = frozenset(
    {
        "can", "could", "would", "will", "should", "do", "does", "is", "are",
        "i", "you", "we", "what", "why", "how", "when", "where", "which", "who",
        "please",
    }
)
_SCAFFOLD_TOPIC_MIN_LEN = 10
_SCAFFOLD_TOPIC_MAX_WORDS = 20


def _normalize_scaffold_topic(text: str) -> str | None:
    """First-sentence, trailing-"skill"-suffix-stripped topic from free text.

    Returns ``None`` when ``text`` is empty or its first sentence starts like
    a question/relative-clause (see ``_SCAFFOLD_TOPIC_REJECTED_STARTS``) — the
    caller should try a different source (or the generic fallback) rather
    than glue a rejected sentence into a template.

    Deliberately does NOT try to recognize/strip a leading creation verb
    ("create"/"build"/"detect"/"promote"/...): no finite verb whitelist
    covers arbitrary bio-analysis requests, and an earlier version of this
    function that tried broke on the exact `request` strings already used
    elsewhere in this test suite ("Detect spatial domains...", "Promote the
    failed analysis."). The caller picks a grammatically-compatible template
    based on which field the topic came from instead (see
    ``_synthesize_load_when``).
    """
    sentence = text.strip()
    if not sentence:
        return None
    match = _SCAFFOLD_TOPIC_SENTENCE_SPLIT.search(sentence)
    sentence = (sentence[: match.start()] if match else sentence).strip()
    if not sentence:
        return None
    # split(None, ...) breaks on ANY whitespace run (space, tab, ...), unlike
    # split(" ", ...) which only recognizes a literal space and would let a
    # tab-separated "Can\tyou build..." slip past the reject guard below.
    first_word = sentence.split(None, 1)[0].lower().strip("'\"")
    # A contraction ("I'd", "I've", "Who's") keeps the apostrophe INSIDE the
    # word, so a plain strip("'\"") (which only trims leading/trailing quote
    # characters) never catches it — check the part before the apostrophe too.
    first_word_root = first_word.split("'", 1)[0]
    if first_word in _SCAFFOLD_TOPIC_REJECTED_STARTS or first_word_root in _SCAFFOLD_TOPIC_REJECTED_STARTS:
        return None

    stripped = _SCAFFOLD_TOPIC_TRAILING_SKILL.sub("", sentence).strip().rstrip(".").strip()
    # "QC skill." stripped down to "QC" loses too much signal — revert to the
    # pre-strip sentence when stripping leaves next to nothing.
    topic = stripped if len(stripped) >= _SCAFFOLD_TOPIC_MIN_LEN else sentence
    words = topic.split()
    return " ".join(words[:_SCAFFOLD_TOPIC_MAX_WORDS])


def _synthesize_load_when(domain: str, skill_name: str, request: str, summary: str) -> str:
    """Synthesize a real ``load_when`` clause from ``request``/``summary``.

    P3 of the acquisition flywheel (docs/proposals/skill-acquisition-plan.md
    §P3): the previous hardcoded text ("the user explicitly asks to create a
    new {domain} skill named '{skill_name}'") is IDENTICAL for every
    scaffolded skill in a domain — it describes the act of scaffolding, not
    what the resulting skill does, so it carries zero routing signal once the
    skill exists. This is a deterministic normalization, not an LLM call —
    there is no LLM seam wired into ``create_skill_scaffold`` (same scope
    boundary as P2a deferring LLM-assisted abstraction).

    ``summary`` is preferred when present — both real callers
    (``execute_create_omics_skill``, ``omics_skill_builder.py``) already treat
    it as a clean capability phrase (e.g. "Kinase activity inference scaffold
    for phosphoproteomics matrices."), so it gets the noun-phrase template.
    ``request`` (more often an imperative sentence, e.g. "Detect spatial
    domains for a new demo assay.") gets the verb-phrase template instead —
    except a bare noun-phrase request ("A spatial domain detection skill")
    has its leading article stripped and falls through to the noun template.
    Falls back to the original generic text when neither field yields a
    usable topic (preserves prior behavior for the truly-no-info case).
    """
    generic = f"the user explicitly asks to create a new {domain} skill named '{skill_name}'"
    summary_topic = _normalize_scaffold_topic(summary)
    if summary_topic:
        return f"the user needs {summary_topic}"
    request_topic = _normalize_scaffold_topic(request)
    if request_topic:
        article_match = _SCAFFOLD_TOPIC_LEADING_ARTICLE.match(request_topic)
        if article_match:
            return f"the user needs {request_topic[article_match.end():]}"
        return f"the user needs to {request_topic}"
    return generic


def _build_corpus_hints(candidates: list[CorpusParamCandidate], *, method: str, doc_ref: str) -> dict:
    """Build the ``interface.parameters.hints`` reserved-key shape for P5.

    ``defaults`` only ever gets an entry for a sourced candidate — an
    unsourced param simply has no ``defaults`` entry, reusing the existing,
    already-legal "declared param, no forced default" pattern (e.g.
    ``spatial-preprocess``'s ``tissue``/``resolutions`` hints today). This
    makes the P5 iron rule a single lint invariant: for every name in
    ``defaults``, ``source_refs[name]`` must exist and be a well-formed,
    span-verified triple (see ``scripts/skill_lint._check_corpus_source_refs``).
    """
    if not candidates:
        return {}
    return {
        method: {
            "params": [c.param for c in candidates],
            "defaults": {c.param: c.value for c in candidates if not c.todo},
            "source_refs": {
                c.param: (
                    {"todo": True}
                    if c.todo
                    else {"quote": c.quote, "char_span": list(c.char_span), "doc_ref": doc_ref}
                )
                for c in candidates
            },
        }
    }


def build_scaffold_manifest(
    *,
    skill_name: str,
    domain: str,
    trigger_keywords: Iterable[str],
    source_bundle: AutonomousAnalysisBundle | None = None,
    corpus_bundle: CorpusDerivedBundle | None = None,
    deps_python: Iterable[str] | None = None,
    request: str = "",
    summary: str = "",
    method: str = "default",
):
    """Build a minimal valid v2 ``SkillManifest`` (ADR 0037) for a scaffold.

    A freshly-scaffolded skill is born v2: the machine contract is the
    ``SkillManifest`` below, serialized to ``skill.yaml``.  ``load_when`` is
    synthesized from ``request``/``summary`` by ``_synthesize_load_when`` (P3,
    docs/proposals/skill-acquisition-plan.md) when either is given, else it
    falls back to a generic templated clause.  Note: despite its name,
    ``_render_v2_description`` is NOT the source of this text — its output is
    placeholder frontmatter fully overwritten later by
    ``skill_md.render_frontmatter``, which reads
    ``LazySkillMetadata._reconstruct_description(manifest.summary)`` instead;
    ``_render_v2_description`` is inert scaffolding-time-only text. ``skip_when``
    stays a generic domain-level fallback — naming a *specific* overlapping
    skill needs registry-based similarity matching this function doesn't do.
    ``allowed_extra_flags`` is left empty: the runtime derives the accepted
    flags from the script's argparse surface (ADR 0041), so a scaffold need not
    mirror its own ``--method`` / ``--species``.  ``deps.python`` is ``deps_python`` when given
    (the promotion path seeds it from the rendered script's real import surface
    so ``audit_skill_requires`` starts clean) else empty (the default
    placeholder script imports only stdlib plus ``omicsclaw.common.report``,
    which ``_infer_python_deps`` excludes, so ``deps.python`` stays empty).
    ``corpus_bundle`` (P5), when given, sets ``provenance.origin="corpus"``
    plus ``provenance.source_ref`` and is the first case where this function
    ever populates a non-empty ``interface.parameters.hints`` (see
    ``_build_corpus_hints``).
    """
    # Lazy import: keeps the scaffolder importable without pydantic (mirrors the
    # deferred schema import in lazy_metadata / generate_parameters_md).
    from .schema import (
        SCHEMA_VERSION,
        Deps,
        Interface,
        Inputs,
        Lifecycle,
        Outputs,
        Parameters,
        Provenance,
        Resources,
        Runtime,
        SkillManifest,
        SkipRule,
        Summary,
    )

    profile = _DOMAIN_PROFILES[domain]
    script_name = f"{skill_name.replace('-', '_')}.py"
    keywords = _unique(trigger_keywords) or [skill_name, f"{domain} scaffold"]
    hints = (
        _build_corpus_hints(corpus_bundle.candidates, method=method, doc_ref=corpus_bundle.doc_ref)
        if corpus_bundle
        else {}
    )
    provenance = (
        Provenance(origin="corpus", source_ref=corpus_bundle.doc_ref)
        if corpus_bundle
        else Provenance(origin="promoted" if source_bundle else "scaffolded")
    )
    return SkillManifest(
        schema_version=SCHEMA_VERSION,
        id=skill_name,
        name=skill_name,
        domain=domain,
        type="leaf",
        version="0.1.0",
        author="OmicsClaw",
        license="MIT",
        emoji=profile["emoji"],
        summary=Summary(
            load_when=_synthesize_load_when(domain, skill_name, request, summary),
            skip_when=[
                SkipRule(condition=f"an existing {domain} skill already covers the request")
            ],
            trigger_keywords=keywords,
            tags=[domain, "autogenerated", "skill-scaffold"],
            aliases=[],
        ),
        interface=Interface(
            inputs=Inputs(),
            parameters=Parameters(hints=hints),
            outputs=Outputs(files=["report.md", "result.json"]),
        ),
        runtime=Runtime(language="python", entry=script_name),
        deps=Deps(python=list(deps_python or [])),
        resources=Resources(
            references=[
                "methodology.md",
                "output_contract.md",
                "parameters.md",
                "r_visualization.md",
            ],
        ),
        provenance=provenance,
        # Born unproven: a scaffold's science is a placeholder until the demo
        # smoke gate credits it. `draft` (non-default) persists under
        # to_yaml(exclude_defaults); skill_lint also exempts draft skills from the
        # "entry script must exist" check. It graduates to `mvp` once earned.
        lifecycle=Lifecycle(status="draft"),
    )


def render_skill_yaml(
    *,
    skill_name: str,
    domain: str,
    trigger_keywords: Iterable[str],
    source_bundle: AutonomousAnalysisBundle | None = None,
    deps_python: Iterable[str] | None = None,
) -> str:
    """Render the v2 ``skill.yaml`` machine contract (ADR 0037) for a scaffold."""
    return build_scaffold_manifest(
        skill_name=skill_name,
        domain=domain,
        trigger_keywords=trigger_keywords,
        source_bundle=source_bundle,
        deps_python=deps_python,
    ).to_yaml()


_REFERENCE_METHODOLOGY = """# Methodology

<!--
Replace this with the WHY behind the algorithm.  Methodology lives here
(lazy-loaded), NOT in SKILL.md's body — keep the body <=200 lines.

Cover:
- The biological / statistical rationale for the chosen approach.
- When each --method backend wins or loses (multi-method skills).
- Per-method assumptions (e.g. "Welch t-test assumes unequal variance").
- Citations to the canonical papers.
-->

## Background

(Replace with 1-3 paragraphs of motivation.)

## Method comparison

| Method | When to choose | Caveat |
|---|---|---|
| `<name>` | `<conditions>` | `<failure mode>` |

## Citations

- (Author, year). Paper title. Journal. DOI.
"""

_REFERENCE_OUTPUT_CONTRACT = """## Output Structure

```
output_directory/
├── report.md
├── result.json
└── reproducibility/
    └── commands.sh
```

## File contents

<!--
List ONLY the files the script actually writes.  PR-eval-2 added a lint
check that fails when a non-framework path mentioned here does not appear
in the script.  Framework files (report.md, result.json, commands.sh,
processed.h5ad, …) are exempt — they are written by the common report
helper.
-->

- `report.md` — Markdown summary written by the common report helper.
- `result.json` — standardised result envelope (`summary` + `data` keys).
- `reproducibility/commands.sh` — replay log for the run.

## Notes

Replace these placeholders with the script's actual writes
(e.g. `tables/<name>.csv`, `figures/<name>.png`) before relying on the
contract.  Downstream skills that read this output should be linked from
SKILL.md's `## See also` section.
"""

_REFERENCE_R_VISUALIZATION = """# R Enhanced Visualization

<!--
OPTIONAL.  Only fill in if this skill emits figure_data/*.json payloads that
an R post-renderer can consume to produce publication-quality figures.

Three-tier visualization flow (CLAUDE.md routing reference):
  1. First run: Python standard figures (matplotlib / seaborn).
  2. R Enhanced: omicsclaw.py replot <skill> --output dir/ re-renders
     ggplot2 figures from existing figure_data/.
  3. Parameter tuning: replot <skill> --output dir/ --renderer X --top-n N.
-->

This skill does not yet expose an R Enhanced renderer.  Skip this file until
a renderer is added under `r_visualization/<name>_publication_template.R`.
"""


def _render_parameters_md_from_manifest(manifest, script_text: str = "") -> str:
    """Render references/parameters.md from the v2 manifest (ADR 0037 dual-track).

    Uses `omicsclaw.skill.parameters_md.render_parameters_md` with ``source="v2"``
    — the exact path `scripts/generate_parameters_md.py` and `skill_lint._lint_v2`
    take for a `skill.yaml` — so the scaffolder's output stays byte-for-byte
    consistent with the generator's `--check` freshness gate.

    Since ADR 0041 the accepted flags are derived, not stored, so the empty
    `allowed_extra_flags` override is resolved here from the freshly-generated
    ``script_text`` (which the freshness gate later derives identically from the
    same bytes on disk). Consensus shims keep their explicit declared subset.
    """
    from .parameters_md import render_parameters_md
    from .execution.flag_introspection import effective_allowed_flags_from_script_text

    params = manifest.interface.parameters.model_dump()
    params["allowed_extra_flags"] = sorted(
        effective_allowed_flags_from_script_text(
            params.get("allowed_extra_flags"), script_text, manifest.type
        )
    )
    return render_parameters_md(params, source="v2")


def render_skill_script(
    *,
    skill_name: str,
    domain: str,
    summary: str,
    methods: Iterable[str],
) -> str:
    methods_list = _unique(methods) or ["default"]
    default_method = methods_list[0]
    title = _display_title(skill_name)
    summary = (summary or "").strip() or f"Autogenerated OmicsClaw scaffold for {title}."
    checklist_rows = [
        ("load_input", "todo"),
        ("validate_requirements", "todo"),
        ("implement_method", "todo"),
        ("write_standard_outputs", "done"),
    ]
    checklist_literal = repr(checklist_rows)

    return f"""#!/usr/bin/env python3
\"\"\"Autogenerated OmicsClaw scaffold for {title}.\"\"\"

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from omicsclaw.common.report import SCAFFOLD_STATUS, write_result_json


SKILL_NAME = "{skill_name}"
DOMAIN = "{domain}"
SUMMARY = {json.dumps(summary)}
DEFAULT_METHOD = "{default_method}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=SUMMARY)
    parser.add_argument("--input", dest="input_path", help="Path to input data")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--demo", action="store_true", help="Run the scaffold demo")
    parser.add_argument("--method", default=DEFAULT_METHOD, help="Method backend name")
    parser.add_argument("--species", default="", help="Optional species label")
    return parser.parse_args()


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_csv(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["step", "status"])
        writer.writerows({checklist_literal})


def main() -> None:
    args = parse_args()
    if not args.demo and not args.input_path:
        raise SystemExit("Provide --input or use --demo.")

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    readme = f\"\"\"# {{SKILL_NAME}}

This is an autogenerated OmicsClaw skill scaffold.

- Domain: {{DOMAIN}}
- Method: {{args.method}}
- Input: {{args.input_path or "demo"}}
- Species: {{args.species or "not provided"}}

Next step: replace the placeholder implementation in `{{SKILL_NAME.replace("-", "_")}}.py`.
\"\"\"

    report = f\"\"\"# Scaffold Report

The scientific implementation for `{{SKILL_NAME}}` has not been completed yet.
This scaffold exists so the skill can be edited, reviewed, and iterated inside the OmicsClaw repository.

Implementation checklist:
- wire real loaders
- validate domain-specific state
- implement the scientific backend
- expand tests beyond the scaffold smoke test
\"\"\"

    summary = {{"method": args.method, "implemented": False}}
    data = {{
        "skill": SKILL_NAME,
        "domain": DOMAIN,
        "input": args.input_path or "demo",
        "method": args.method,
        "species": args.species,
        "description": SUMMARY,
    }}

    _write_text(output_dir / "README.md", readme)
    _write_text(output_dir / "report.md", report)
    _write_text(
        output_dir / "reproducibility" / "commands.sh",
        f"oc run {{SKILL_NAME}} --demo --output {{output_dir}}\\n",
    )
    _write_csv(output_dir / "tables" / "scaffold_checklist.csv")
    result_path = write_result_json(
        output_dir, skill=SKILL_NAME, version="0.1.0", summary=summary, data=data
    )
    # Mark the placeholder as unimplemented so the promotion / demo gate keeps
    # this skill as `draft` rather than crediting a real run. write_result_json
    # omits `status`; mark_result_status only accepts run outcomes, so stamp the
    # scaffold sentinel directly.
    envelope = json.loads(result_path.read_text(encoding="utf-8"))
    envelope["status"] = SCAFFOLD_STATUS
    result_path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")

    print(f"Scaffold skill '{{SKILL_NAME}}' completed. Outputs written to {{output_dir}}")


if __name__ == "__main__":
    main()
"""


# Flags injected by the runner / always present in a corpus-derived scaffold's
# fixed argparse block — a candidate param colliding with one of these is
# skipped (defensive guard; unreachable with today's fixed extraction
# vocabulary, since none of _PARAM_ALIASES's keys collide with these names).
_RESERVED_CORPUS_FLAGS = frozenset({"input", "output", "demo", "method", "species"})


def _escape_comment_text(text: str) -> str:
    """Make ``text`` safe to embed in a single-line ``#`` comment.

    A raw ``\\n``/``\\r`` in the source text (e.g. a multi-line methodology
    statement the extractor's ``\\s*`` matched across) would otherwise land as
    a LITERAL newline inside the generated script's comment, prematurely
    ending it and turning the remainder of the quote into invalid top-level
    code — a real syntax error a prior codex adversarial pass reproduced.
    """
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _render_corpus_add_argument_line(cand: CorpusParamCandidate, *, doc_ref: str) -> str:
    """One argparse line (+ preceding comment) for a single extracted candidate.

    A sourced candidate's comment cites the exact quote/span/doc_ref it came
    from; a TODO candidate — never emitted by Tier 1's ``extract_methodology``
    today, but handled here for forward-compatibility — gets an explicit TODO
    comment instead of a fabricated default (P5 iron rule).
    """
    dest = cand.param
    if dest in _RESERVED_CORPUS_FLAGS:
        return f"    # skipped: '{dest}' collides with a reserved scaffold flag"
    flag = "--" + dest.replace("_", "-")
    if cand.todo:
        comment = (
            f"    # TODO(source_ref): '{dest}' was not found with a verifiable quote "
            f"in {doc_ref!r} — provide a real value before relying on this default.\n"
        )
        return comment + (
            f"    parser.add_argument({flag!r}, dest={dest!r}, type=float, default=None, "
            f"help=\"TODO: not sourced from the corpus text — fill in manually\")"
        )
    type_kwarg = "type=int, " if isinstance(cand.value, int) else "type=float, "
    quote_escaped = _escape_comment_text(cand.quote)
    comment = (
        f'    # source_ref: "{quote_escaped}" (chars {cand.char_span[0]}-{cand.char_span[1]}) '
        f"from {doc_ref!r}\n"
    )
    help_text = f"Extracted from corpus ({doc_ref})"
    return comment + (
        f"    parser.add_argument({flag!r}, dest={dest!r}, {type_kwarg}"
        f"default={cand.value!r}, help={help_text!r})"
    )


def render_corpus_skill_script(
    *,
    skill_name: str,
    domain: str,
    summary: str,
    corpus_bundle: CorpusDerivedBundle,
    method: str,
) -> str:
    """Render a corpus-derived scaffold's entry script (P5).

    Same placeholder skeleton as :func:`render_skill_script` — the science is
    still unimplemented (this pre-fills the PARAMETER surface, not the
    analysis logic) — but the argparse block is generated from
    ``corpus_bundle.candidates`` instead of a fixed ``--method``/``--species``
    pair alone. Diverges from :func:`render_promoted_skill_script` in that
    there is no ``body_code``, no mini-agent facade bootstrap, and no reuse of
    ``_render_lifted_add_argument_line`` (which only emits a ``help=`` phrase,
    never a source-quoting comment).
    """
    title = _display_title(skill_name)
    summary = (summary or "").strip() or corpus_bundle.goal or f"Autogenerated OmicsClaw scaffold for {title}."
    checklist_rows = [
        ("load_input", "todo"),
        ("validate_requirements", "todo"),
        ("implement_method", "todo"),
        ("write_standard_outputs", "done"),
    ]
    checklist_literal = repr(checklist_rows)
    doc_ref = corpus_bundle.doc_ref
    candidate_arg_lines = "\n".join(
        _render_corpus_add_argument_line(c, doc_ref=doc_ref) for c in corpus_bundle.candidates
    )
    candidate_dests = [c.param for c in corpus_bundle.candidates if c.param not in _RESERVED_CORPUS_FLAGS]
    candidate_dict_literal = "{" + ", ".join(f"{d!r}: args.{d}" for d in candidate_dests) + "}"

    return f"""#!/usr/bin/env python3
\"\"\"Autogenerated OmicsClaw scaffold for {title} (corpus-derived).\"\"\"

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from omicsclaw.common.report import SCAFFOLD_STATUS, write_result_json


SKILL_NAME = "{skill_name}"
DOMAIN = "{domain}"
SUMMARY = {json.dumps(summary)}
DEFAULT_METHOD = "{method}"
DOC_REF = {json.dumps(doc_ref, ensure_ascii=False)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=SUMMARY)
    parser.add_argument("--input", dest="input_path", help="Path to input data")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--demo", action="store_true", help="Run the scaffold demo")
    parser.add_argument("--method", default=DEFAULT_METHOD, help="Method backend name")
    parser.add_argument("--species", default="", help="Optional species label")
{candidate_arg_lines}
    return parser.parse_args()


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_csv(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["step", "status"])
        writer.writerows({checklist_literal})


def main() -> None:
    args = parse_args()
    if not args.demo and not args.input_path:
        raise SystemExit("Provide --input or use --demo.")

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    extracted_params = {candidate_dict_literal}

    readme = f\"\"\"# {{SKILL_NAME}}

This is an autogenerated OmicsClaw skill scaffold, pre-filled from a corpus source.

- Domain: {{DOMAIN}}
- Method: {{args.method}}
- Input: {{args.input_path or "demo"}}
- Source: {{DOC_REF}}

Next step: replace the placeholder implementation in `{{SKILL_NAME.replace("-", "_")}}.py`.
See `references/corpus_provenance.md` for every extracted parameter's source quote.
\"\"\"

    report = f\"\"\"# Scaffold Report

The scientific implementation for `{{SKILL_NAME}}` has not been completed yet.
This scaffold exists so the skill can be edited, reviewed, and iterated inside the OmicsClaw repository.

Extracted parameters: {{extracted_params}}

Implementation checklist:
- wire real loaders
- validate domain-specific state
- implement the scientific backend
- expand tests beyond the scaffold smoke test
\"\"\"

    summary = {{"method": args.method, "implemented": False}}
    data = {{
        "skill": SKILL_NAME,
        "domain": DOMAIN,
        "input": args.input_path or "demo",
        "method": args.method,
        "source": DOC_REF,
        "extracted_params": extracted_params,
        "description": SUMMARY,
    }}

    _write_text(output_dir / "README.md", readme)
    _write_text(output_dir / "report.md", report)
    _write_text(
        output_dir / "reproducibility" / "commands.sh",
        f"oc run {{SKILL_NAME}} --demo --output {{output_dir}}\\n",
    )
    _write_csv(output_dir / "tables" / "scaffold_checklist.csv")
    result_path = write_result_json(
        output_dir, skill=SKILL_NAME, version="0.1.0", summary=summary, data=data
    )
    # Mark the placeholder as unimplemented so the promotion / demo gate keeps
    # this skill as `draft` rather than crediting a real run. write_result_json
    # omits `status`; mark_result_status only accepts run outcomes, so stamp the
    # scaffold sentinel directly.
    envelope = json.loads(result_path.read_text(encoding="utf-8"))
    envelope["status"] = SCAFFOLD_STATUS
    result_path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")

    print(f"Scaffold skill '{{SKILL_NAME}}' completed. Outputs written to {{output_dir}}")


if __name__ == "__main__":
    main()
"""


# Recreates the mini-agent kernel namespace (oc/adata/show/ReturnAnswer) inside a
# promoted skill so its accepted code runs instead of crashing on NameError. Only
# injected for ``engine == "mini_agent"`` bundles; notebook code is self-contained.
_MINI_AGENT_FACADE_BOOTSTRAP = '''\
# --- mini-agent facade bootstrap --------------------------------------------
# This code was authored in the OmicsClaw Autonomous Code Mini-Agent kernel, which
# provides `oc`, `adata`, `show()` and `ReturnAnswer()`. They are recreated here so
# the promoted draft runs; adapt them as you harden it into a real skill.
import anndata as _ad
adata = _ad.read_h5ad(INPUT_FILE) if INPUT_FILE else None
from omicsclaw.autonomous.skill_facade import build_facade as _build_facade
oc = _build_facade(AUTONOMOUS_OUTPUT_DIR, max_skill_calls=20, skill_timeout_seconds=1800)
import matplotlib as _matplotlib
_matplotlib.use("Agg")
import matplotlib.pyplot as _plt
_oc_fig_count = [0]
def show(*_a, **_k):
    for _num in _plt.get_fignums():
        _oc_fig_count[0] += 1
        _plt.figure(_num).savefig(
            str(OUTPUT_PATH / ("fig_%02d.png" % _oc_fig_count[0])), dpi=120, bbox_inches="tight"
        )
    _plt.close("all")
def ReturnAnswer(text=""):
    (OUTPUT_PATH / "answer.txt").write_text(str(text), encoding="utf-8")
'''


def _render_lifted_add_argument_line(param: LiftedParam) -> str:
    if param.type == "int":
        type_kwarg = "type=int, "
    elif param.type == "float":
        type_kwarg = "type=float, "
    elif param.type == "bool":
        type_kwarg = "type=_parse_flag_bool, "
    else:
        type_kwarg = ""
    return (
        f"    parser.add_argument({param.flag!r}, dest={param.name!r}, "
        f"{type_kwarg}default={param.default!r}, help={param.help!r})"
    )


def render_promoted_skill_script(
    *,
    skill_name: str,
    domain: str,
    summary: str,
    source_bundle: AutonomousAnalysisBundle,
    body_code: str,
    lifted_params: list[LiftedParam] | None = None,
) -> str:
    """Render a promoted skill's entry script.

    ``body_code`` is the already-decided script body (P2a: either the
    verbatim-normalized accepted code, or that same code with literal
    ``oc.run(...)`` kwargs lifted by :func:`lift_oc_run_literals` — the
    caller, ``create_skill_scaffold``, owns that decision and any gate-driven
    fallback between the two, so this renderer stays a pure function of
    already-decided inputs). ``lifted_params`` (if any) get real
    ``argparse.add_argument`` lines in the generated ``parse_args()``,
    letting a user override at run time what was otherwise frozen at
    promotion time.
    """
    title = _display_title(skill_name)
    goal = source_bundle.goal or summary
    web_context = source_bundle.web_sources or ""
    analysis_context = source_bundle.context or ""
    default_input = source_bundle.input_file or ""
    requires_input = bool(default_input)
    indented_code = textwrap.indent(body_code.rstrip() + "\n", "    ")
    facade_bootstrap = ""
    if source_bundle.engine == "mini_agent":
        facade_bootstrap = textwrap.indent(_MINI_AGENT_FACADE_BOOTSTRAP, "    ") + "\n"

    lifted_params = lifted_params or []
    lifted_arg_lines = "\n".join(_render_lifted_add_argument_line(p) for p in lifted_params)
    bool_helper_block = (
        "def _parse_flag_bool(value: str) -> bool:\n"
        '    return value.strip().lower() not in {"0", "false", "no", ""}\n\n\n'
        if any(p.type == "bool" for p in lifted_params)
        else ""
    )

    return f"""#!/usr/bin/env python3
\"\"\"Promoted OmicsClaw skill for {title}.\"\"\"

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from omicsclaw.common.report import mark_result_status, write_result_json


SKILL_NAME = "{skill_name}"
DOMAIN = "{domain}"
SUMMARY = {json.dumps(summary or goal, ensure_ascii=False)}
ANALYSIS_GOAL = {json.dumps(goal, ensure_ascii=False)}
ANALYSIS_CONTEXT = {json.dumps(analysis_context, ensure_ascii=False)}
WEB_CONTEXT = {json.dumps(web_context, ensure_ascii=False)}
SOURCE_ANALYSIS_DIR = {json.dumps(source_bundle.source_dir, ensure_ascii=False)}
SOURCE_NOTEBOOK = {json.dumps(source_bundle.notebook_path, ensure_ascii=False)}
DEFAULT_INPUT_FILE = {json.dumps(default_input, ensure_ascii=False)}
REQUIRES_INPUT = {str(requires_input)}


{bool_helper_block}def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=SUMMARY)
    parser.add_argument("--input", dest="input_path", help="Path to input data")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--demo", action="store_true", help="Reuse the original autonomous-analysis input when available")
    parser.add_argument("--method", default="", help="Optional method backend name")
    parser.add_argument("--species", default="", help="Optional species label")
{lifted_arg_lines}
    return parser.parse_args()


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    effective_input = args.input_path or (DEFAULT_INPUT_FILE if args.demo else "")
    if REQUIRES_INPUT and not effective_input:
        raise SystemExit("Provide --input, or use --demo to reuse the original autonomous-analysis input.")

    skill_output_dir = Path(args.output)
    skill_output_dir.mkdir(parents=True, exist_ok=True)

    INPUT_FILE = effective_input
    AUTONOMOUS_OUTPUT_DIR = str(skill_output_dir)
    OUTPUT_PATH = skill_output_dir
    OUTPUT_PATH.mkdir(parents=True, exist_ok=True)

{facade_bootstrap}{indented_code}

    readme = f\"\"\"# {{SKILL_NAME}}

This skill was promoted from a successful `autonomous_analysis_execute` run.

- Domain: {{DOMAIN}}
- Input: {{effective_input or "none"}}
- Original source notebook: {{SOURCE_NOTEBOOK}}
- Original autonomous analysis directory: {{SOURCE_ANALYSIS_DIR}}

Inspect `report.md` and `references/` for the promotion provenance.
\"\"\"

    report = f\"\"\"# Promoted Skill Report

This skill was generated from a successful autonomous analysis notebook.

## Original Goal

{{ANALYSIS_GOAL}}

## Promotion Notes

- This script started from notebook code that previously ran successfully.
- Review imports, parameter handling, and output paths before considering it production-ready.
- Expand tests and tighten the OmicsClaw output contract in follow-up edits.
\"\"\"

    summary = {{"method": args.method, "input": effective_input}}
    data = {{
        "skill": SKILL_NAME,
        "domain": DOMAIN,
        "input": effective_input,
        "source_analysis_dir": SOURCE_ANALYSIS_DIR,
        "source_notebook": SOURCE_NOTEBOOK,
        "description": SUMMARY,
    }}

    _write_text(skill_output_dir / "README.md", readme)
    _write_text(skill_output_dir / "report.md", report)
    _write_text(
        skill_output_dir / "reproducibility" / "commands.sh",
        f"oc run {{SKILL_NAME}} --output {{skill_output_dir}}\\n",
    )
    write_result_json(
        skill_output_dir, skill=SKILL_NAME, version="0.1.0", summary=summary, data=data
    )
    # Reaching this line means the promoted body above ran to completion without
    # raising, so this is a genuine success signal (unlike the scaffold
    # placeholder's SCAFFOLD_STATUS sentinel, which marks unimplemented science).
    mark_result_status(skill_output_dir, "ok")

    print(f"Promoted skill '{{SKILL_NAME}}' completed. Outputs written to {{skill_output_dir}}")


if __name__ == "__main__":
    main()
"""


def _render_acquisition_argument_line(param: AcquisitionParameter) -> str:
    type_kwarg = ""
    if param.type == "int":
        type_kwarg = "type=int, "
    elif param.type == "float":
        type_kwarg = "type=float, "
    elif param.type == "bool":
        type_kwarg = "type=_parse_flag_bool, "
    return (
        f"    parser.add_argument({param.flag!r}, dest={param.dest!r}, "
        f"{type_kwarg}default={param.default!r}, "
        f"help='Acquired parameter for source call(s) {param.call_indexes}')"
    )


def render_structured_promoted_skill_script(
    *,
    skill_name: str,
    domain: str,
    summary: str,
    source_bundle: AutonomousAnalysisBundle,
    abstraction: AcquisitionAbstraction,
) -> str:
    """Render a facade-free workflow over the stable shared skill runner."""
    if not abstraction.reusable or not abstraction.facade_free:
        raise ValueError("structured renderer requires a reusable facade-free abstraction")

    goal = source_bundle.goal or summary
    default_input = source_bundle.input_file or ""
    arg_lines = "\n".join(
        _render_acquisition_argument_line(param) for param in abstraction.parameters
    )
    bool_helper = (
        "def _parse_flag_bool(value: str) -> bool:\n"
        '    return value.strip().lower() not in {"0", "false", "no", ""}\n\n\n'
        if any(param.type == "bool" for param in abstraction.parameters)
        else ""
    )
    call_specs = [asdict(call) for call in abstraction.calls]

    return f'''#!/usr/bin/env python3
"""Facade-free acquired OmicsClaw workflow for {_display_title(skill_name)}."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from omicsclaw.common.report import mark_result_status, write_result_json
from omicsclaw.skill.runner import run_skill


SKILL_NAME = {skill_name!r}
DOMAIN = {domain!r}
SUMMARY = {summary or goal!r}
ANALYSIS_GOAL = {goal!r}
SOURCE_ANALYSIS_DIR = {source_bundle.source_dir!r}
DEFAULT_INPUT_FILE = {default_input!r}
CALL_SPECS = json.loads({json.dumps(json.dumps(call_specs, ensure_ascii=False))})


{bool_helper}def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=SUMMARY)
    parser.add_argument("--input", dest="input_path", help="Path to input data")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--demo", action="store_true", help="Reuse the source run input")
{arg_lines}
    return parser.parse_args()


def _value_to_flags(key: str, value: object) -> list[str]:
    flag = "--" + key.replace("_", "-")
    if isinstance(value, bool):
        return [flag] if value else []
    if value is None:
        return []
    return [flag, str(value)]


def _primary_h5ad(output_dir: str | Path) -> Path | None:
    root = Path(output_dir)
    preferred = root / "processed.h5ad"
    if preferred.is_file():
        return preferred
    candidates = sorted(root.glob("*.h5ad"))
    return candidates[0] if len(candidates) == 1 else None


def main() -> None:
    args = parse_args()
    effective_input = args.input_path or (DEFAULT_INPUT_FILE if args.demo else "")
    if not effective_input:
        raise SystemExit("Provide --input, or use --demo to reuse the source run input.")

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    step_primary: dict[int, Path] = {{}}
    step_results: list[dict[str, object]] = []

    for spec in CALL_SPECS:
        index = int(spec["index"])
        source = str(spec["input_source"])
        if source == "input":
            step_input = Path(effective_input)
        elif source.startswith("step:"):
            upstream = int(source.split(":", 1)[1])
            if upstream not in step_primary:
                raise RuntimeError(
                    f"Step {{index}} needs the primary .h5ad from step {{upstream}}, "
                    "but that step did not produce one."
                )
            step_input = step_primary[upstream]
        else:
            raise RuntimeError(f"Unsupported acquired input source: {{source}}")

        extra_args: list[str] = []
        for key, dest in spec["parameter_bindings"].items():
            extra_args.extend(_value_to_flags(key, getattr(args, dest)))
        step_dir = output_dir / "steps" / f"{{index:02d}}_{{spec['skill']}}"
        result = run_skill(
            str(spec["skill"]),
            input_path=str(step_input),
            output_dir=str(step_dir),
            extra_args=extra_args,
        )
        if not result.success:
            raise RuntimeError(
                f"Acquired workflow step {{index}} ({{spec['skill']}}) failed: "
                f"{{result.stderr or result.stdout}}"
            )
        primary = _primary_h5ad(result.output_dir or step_dir)
        if primary is not None:
            step_primary[index] = primary
        step_results.append(
            {{
                "index": index,
                "skill": spec["skill"],
                "input": str(step_input),
                "output_dir": str(result.output_dir or step_dir),
                "primary_artifact": str(primary) if primary else "",
                "parameters": {{
                    key: getattr(args, dest)
                    for key, dest in spec["parameter_bindings"].items()
                }},
            }}
        )

    final_primary = step_primary.get(len(CALL_SPECS))
    if final_primary is not None:
        target = output_dir / "processed.h5ad"
        if final_primary.resolve() != target.resolve():
            shutil.copy2(final_primary, target)

    (output_dir / "README.md").write_text(
        f"# {{SKILL_NAME}}\\n\\nFacade-free workflow acquired from `{{SOURCE_ANALYSIS_DIR}}`.\\n",
        encoding="utf-8",
    )
    (output_dir / "report.md").write_text(
        f"# Acquired Workflow Report\\n\\nGoal: {{ANALYSIS_GOAL}}\\n\\nSteps: {{len(step_results)}}\\n",
        encoding="utf-8",
    )
    repro = output_dir / "reproducibility" / "commands.sh"
    repro.parent.mkdir(parents=True, exist_ok=True)
    repro.write_text(f"oc run {{SKILL_NAME}} --input <input> --output {{output_dir}}\\n", encoding="utf-8")
    write_result_json(
        output_dir,
        skill=SKILL_NAME,
        version="0.1.0",
        summary={{"steps": len(step_results), "input": effective_input}},
        data={{
            "skill": SKILL_NAME,
            "domain": DOMAIN,
            "source_analysis_dir": SOURCE_ANALYSIS_DIR,
            "steps": step_results,
        }},
    )
    mark_result_status(output_dir, "ok")
    print(f"Acquired skill '{{SKILL_NAME}}' completed. Outputs written to {{output_dir}}")


if __name__ == "__main__":
    main()
'''


def render_skill_test(skill_name: str) -> str:
    script_name = f"{skill_name.replace('-', '_')}.py"
    return f"""from pathlib import Path
import json
import subprocess
import sys


def test_scaffold_files_exist():
    root = Path(__file__).resolve().parents[1]
    assert (root / "SKILL.md").exists()
    assert (root / "{script_name}").exists()


def test_demo_produces_a_valid_result_envelope(tmp_path):
    \"\"\"Real smoke assertion (not just file existence): the entry script must
    actually run --demo and its result.json must satisfy the shared envelope
    shape (summary/data objects) that the P1 acquisition gate checked at
    creation time. Passes for both an unimplemented placeholder (status:
    scaffold) and a real/promoted body (status: ok) -- this is the smoke
    floor every skill clears; a durable input fixture in place of --demo
    (fixture-validated) is a stricter tier layered on top later.
    \"\"\"
    script = Path(__file__).resolve().parents[1] / "{script_name}"
    out_dir = tmp_path / "demo_out"
    proc = subprocess.run(
        [sys.executable, str(script), "--demo", "--output", str(out_dir)],
        capture_output=True,
        text=True,
        timeout={_DEMO_SMOKE_GATE_TIMEOUT_SECONDS},
    )
    assert proc.returncode == 0, f"stdout={{proc.stdout}}\\nstderr={{proc.stderr}}"
    envelope = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
    # Mirrors validate_result_envelope's shape check without importing across
    # the skill/package boundary (skill tests stay self-contained).
    assert isinstance(envelope.get("summary"), dict)
    assert isinstance(envelope.get("data"), dict)
"""


def _skill_scaffold_requirements(
    *,
    script_name: str,
    create_tests: bool,
    reference_paths: Iterable[str] | None = None,
) -> list[ArtifactRequirement]:
    requirements = [
        ArtifactRequirement(
            name="skill_markdown",
            path="SKILL.md",
            description="Generated skill contract markdown.",
        ),
        ArtifactRequirement(
            name="skill_script",
            path=script_name,
            description="Generated skill entrypoint.",
        ),
        ArtifactRequirement(
            name="skill_manifest",
            path="skill.yaml",
            description="v2 machine contract (ADR 0037): identity, summary, interface, runtime, deps.",
        ),
        ArtifactRequirement(
            name="reference_methodology",
            path="references/methodology.md",
            description="Algorithm rationale (lazy-loaded).",
        ),
        ArtifactRequirement(
            name="reference_output_contract",
            path="references/output_contract.md",
            description="Output schema (lint-validated against script writes).",
        ),
        ArtifactRequirement(
            name="reference_parameters",
            path="references/parameters.md",
            description="Auto-generated CLI flag reference.",
        ),
        ArtifactRequirement(
            name="reference_r_visualization",
            path="references/r_visualization.md",
            description="Optional R Enhanced renderer placeholder.",
        ),
        ArtifactRequirement(
            name="scaffold_spec",
            path="scaffold_spec.json",
            description="Structured scaffold specification.",
        ),
        ArtifactRequirement(
            name="workspace_manifest",
            path="manifest.json",
            description="Workspace lineage and verification ledger.",
        ),
    ]
    if create_tests:
        requirements.append(
            ArtifactRequirement(
                name="test_stub",
                path=f"tests/test_{script_name}",
                description="Generated smoke-test stub.",
            )
        )
    for rel_path in _unique(reference_paths or []):
        requirements.append(
            ArtifactRequirement(
                name=Path(rel_path).stem,
                path=rel_path,
                description="Reference artifact copied from the promoted autonomous analysis.",
            )
        )
    return requirements


def _load_completion_report(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_notebook(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


_ACCEPTED_STEP_RE = re.compile(r"^# === accepted step \d+ ===$", re.MULTILINE)


def _load_autonomous_bundle(path: Path) -> AutonomousAnalysisBundle:
    """Load a promotable autonomous run, in either supported layout.

    Since the ADR 0032 single-engine consolidation the Autonomous Code Mini-Agent
    is the only producer: it writes the consolidated accepted cells to
    ``<run_dir>/analysis.py`` (no notebook). The legacy one-shot
    ``custom_analysis_execute`` notebook layout is still read so older on-disk runs
    stay promotable.
    """
    completion_path = path / COMPLETION_REPORT_FILENAME
    if completion_path.exists():
        completion = _load_completion_report(completion_path)
        if not bool(completion.get("completed", False)):
            status = str(completion.get("status", "")).strip() or "incomplete"
            raise ValueError(
                f"Autonomous analysis at {path} is not promotable yet (completion status: {status})."
            )

    notebook_path = path / "reproducibility" / "analysis_notebook.ipynb"
    if notebook_path.exists():
        return _load_legacy_notebook_bundle(path, notebook_path)

    analysis_path = path / "analysis.py"
    if analysis_path.exists():
        return _load_mini_agent_bundle(path, analysis_path)

    raise FileNotFoundError(
        f"No promotable autonomous analysis found at {path}: expected a mini-agent "
        f"replay script ({analysis_path}) or a legacy notebook ({notebook_path})."
    )


def _load_mini_agent_bundle(path: Path, analysis_path: Path) -> AutonomousAnalysisBundle:
    """Build a promotion bundle from a mini-agent run (ADR 0032 layout)."""
    code = _extract_accepted_cells(analysis_path.read_text(encoding="utf-8"))
    if not code.strip():
        raise ValueError(f"No executable analysis code found in {analysis_path}")

    summary_path = path / "result_summary.md"
    result_summary = summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
    goal, input_file = _read_run_goal_and_input(path)
    steps, skill_calls, trace_warnings = _read_structured_run_trace(path)
    if not goal:
        goal = _goal_from_summary(result_summary)

    return AutonomousAnalysisBundle(
        source_dir=str(path),
        notebook_path="",
        analysis_plan="",
        result_summary=result_summary,
        web_sources="",
        capability_decision={},
        python_code=code.rstrip() + "\n",
        goal=goal,
        domain="",
        input_file=input_file,
        context="",
        engine="mini_agent",
        steps=steps,
        skill_calls=skill_calls,
        trace_warnings=trace_warnings,
    )


def _load_legacy_notebook_bundle(path: Path, notebook_path: Path) -> AutonomousAnalysisBundle:
    """Build a promotion bundle from a legacy ``custom_analysis_execute`` notebook."""
    plan_path = path / "analysis_plan.md"
    summary_path = path / "result_summary.md"
    sources_path = path / "web_sources.md"
    capability_path = path / "capability_decision.json"

    notebook = _load_notebook(notebook_path)
    code_cells = []
    goal = ""
    context = ""
    input_file = ""
    for cell in notebook.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        source = "".join(cell.get("source", []))
        if not source.strip():
            continue
        if "AUTONOMOUS_OUTPUT_DIR" in source and "ANALYSIS_GOAL" in source:
            extracted = _extract_setup_literals(source)
            goal = extracted.get("ANALYSIS_GOAL", "")
            context = extracted.get("ANALYSIS_CONTEXT", "")
            input_file = extracted.get("INPUT_FILE", "")
            continue
        code_cells.append(source.rstrip())

    if not code_cells:
        raise ValueError(f"No executable analysis code found in notebook: {notebook_path}")

    capability = {}
    if capability_path.exists():
        capability = json.loads(capability_path.read_text(encoding="utf-8"))

    return AutonomousAnalysisBundle(
        source_dir=str(path),
        notebook_path=str(notebook_path),
        analysis_plan=plan_path.read_text(encoding="utf-8") if plan_path.exists() else "",
        result_summary=summary_path.read_text(encoding="utf-8") if summary_path.exists() else "",
        web_sources=sources_path.read_text(encoding="utf-8") if sources_path.exists() else "",
        capability_decision=capability,
        python_code="\n\n".join(code_cells).strip() + "\n",
        goal=goal,
        domain=str(capability.get("domain", "") or ""),
        input_file=input_file,
        context=context,
    )


def _load_corpus_bundle(corpus_text: str, *, source_kind: str, doc_ref: str) -> CorpusDerivedBundle:
    """Extract methodology candidates from a paper/tool-docs text (P5).

    Imports ``extract_methodology`` from ``skills/literature/core/extractor``
    via the same ``sys.path`` idiom already used by
    ``agent_executors.execute_fetch_geo_metadata`` to reach into that
    cross-package module — ``skills/literature`` sits outside the
    ``omicsclaw`` package tree.
    """
    sys.path.insert(0, str(OMICSCLAW_DIR / "skills" / "literature"))
    from core.extractor import extract_methodology

    raw_candidates = extract_methodology(corpus_text)
    candidates = [
        CorpusParamCandidate(
            param=c["param"],
            operator=c["operator"],
            value=c["value"],
            quote=c["quote"],
            char_span=tuple(c["char_span"]),
            todo=c["todo"],
        )
        for c in raw_candidates
    ]
    goal = f"Corpus-derived scaffold extracted from a {source_kind.replace('_', ' ')} ({doc_ref})."
    return CorpusDerivedBundle(
        source_kind=source_kind,
        doc_ref=doc_ref,
        corpus_text=corpus_text,
        candidates=candidates,
        goal=goal,
    )


def _extract_accepted_cells(script: str) -> str:
    """Return the accepted-cell bodies from a mini-agent ``analysis.py``.

    The replay script is ``<generated init preamble>`` followed by one
    ``# === accepted step N ===`` block per accepted cell. Only the accepted
    blocks are the analyst-authored logic worth promoting; the init preamble is
    kernel scaffolding (``oc`` facade, ``adata`` load) and is dropped. The marker
    is matched line-anchored so a coincidental substring inside cell code or a
    comment cannot cut the extraction early.
    """
    match = _ACCEPTED_STEP_RE.search(script)
    return "" if match is None else script[match.start():]


def _read_structured_run_trace(
    path: Path,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[str]]:
    """Read mini-agent intent and executed-call evidence without guessing.

    The manifest is the source for accepted-cell ``metadata.steps``.  For
    nested calls, the append-only ``skill_calls.jsonl`` is more current than
    the manifest snapshot and therefore wins when it contains valid records;
    older runs that only embedded calls in metadata remain readable.

    A damaged optional trace does not make a historically successful run
    unloadable.  Warnings are retained on the bundle and later written into
    abstraction evidence, allowing the acquisition pass to fall back to the
    verbatim path without hiding why structured generalisation was skipped.
    """
    warnings: list[str] = []
    metadata: dict[str, object] = {}
    manifest_path = path / "manifest.json"
    if manifest_path.exists():
        try:
            raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            raw_metadata = raw_manifest.get("metadata", {}) if isinstance(raw_manifest, dict) else {}
            if isinstance(raw_metadata, dict):
                metadata = raw_metadata
            else:
                warnings.append("manifest metadata is not an object")
        except (json.JSONDecodeError, OSError) as exc:
            warnings.append(f"could not read manifest metadata: {type(exc).__name__}")

    raw_steps = metadata.get("steps", [])
    steps = [dict(item) for item in raw_steps if isinstance(item, dict)] if isinstance(raw_steps, list) else []
    if raw_steps and not steps:
        warnings.append("manifest metadata.steps contains no object records")

    raw_manifest_calls = metadata.get("skill_calls", [])
    manifest_calls = (
        [dict(item) for item in raw_manifest_calls if isinstance(item, dict)]
        if isinstance(raw_manifest_calls, list)
        else []
    )

    jsonl_calls: list[dict[str, object]] = []
    calls_path = path / "skill_calls.jsonl"
    if calls_path.exists():
        try:
            for line_number, line in enumerate(
                calls_path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    warnings.append(f"skill_calls.jsonl line {line_number} is invalid JSON")
                    continue
                if not isinstance(record, dict):
                    warnings.append(f"skill_calls.jsonl line {line_number} is not an object")
                    continue
                jsonl_calls.append(dict(record))
        except OSError as exc:
            warnings.append(f"could not read skill_calls.jsonl: {type(exc).__name__}")

    return steps, (jsonl_calls or manifest_calls), warnings


def _read_run_goal_and_input(path: Path) -> tuple[str, str]:
    """Best-effort goal + first input path for a mini-agent run."""
    goal = ""
    input_file = ""
    manifest_path = path / "manifest.json"
    if manifest_path.exists():
        try:
            meta = json.loads(manifest_path.read_text(encoding="utf-8")).get("metadata", {}) or {}
        except (json.JSONDecodeError, OSError):
            meta = {}
        goal = str(meta.get("goal", "") or "")
        inputs = meta.get("input_paths") or []
        if isinstance(inputs, list) and inputs:
            input_file = str(inputs[0])
    if not input_file:
        refs_path = path / "inputs" / "references.json"
        if refs_path.exists():
            try:
                refs = json.loads(refs_path.read_text(encoding="utf-8")).get("references") or []
            except (json.JSONDecodeError, OSError):
                refs = []
            if refs:
                input_file = str(refs[0])
    return goal, input_file


def _goal_from_summary(summary: str) -> str:
    """Pull the ``## Goal`` section body from a result summary (best-effort)."""
    lines = summary.splitlines()
    for i, line in enumerate(lines):
        if line.strip().lower() == "## goal":
            for body in lines[i + 1:]:
                if body.strip().startswith("##"):
                    break
                if body.strip():
                    return body.strip()
    return ""


def _extract_setup_literals(source: str) -> dict[str, str]:
    values: dict[str, str] = {}
    tree = ast.parse(source)
    wanted = {"ANALYSIS_GOAL", "ANALYSIS_CONTEXT", "WEB_CONTEXT", "INPUT_FILE", "AUTONOMOUS_OUTPUT_DIR"}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        name = node.targets[0].id
        if name not in wanted:
            continue
        try:
            values[name] = ast.literal_eval(node.value)
        except Exception:
            continue
    return values


def _strip_redundant_pathlib_import(code: str) -> str:
    """Drop a standalone ``from pathlib import Path`` / ``import pathlib`` line.

    This code is spliced into ``main()`` AFTER the promoted-script template's
    own ``skill_output_dir = Path(args.output)``, but ``Path`` is already a
    module global (the template imports it at module scope). A notebook/
    mini-agent cell re-importing the exact same name — a common, harmless
    habit in standalone code — makes ``Path`` local to ``main()`` for its
    ENTIRE body under Python's function-scoping rule, so the template's
    earlier use raises ``UnboundLocalError`` even though the import is
    textually later. ``Path`` is already available; drop the redundant import.

    Uses the AST (not a text/regex pass) so a string literal or comment that
    happens to contain this exact text is never touched — only a real
    top-level-in-its-scope import statement whose *only* imported name is
    ``Path``/``pathlib`` qualifies, so `from pathlib import Path, PurePath`
    (which the user's code may still need `PurePath` from) is left alone.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code

    drop_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "pathlib":
            if len(node.names) == 1 and node.names[0].name == "Path" and node.names[0].asname is None:
                drop_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        elif isinstance(node, ast.Import):
            if len(node.names) == 1 and node.names[0].name == "pathlib" and node.names[0].asname is None:
                drop_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))

    if not drop_lines:
        return code
    lines = code.splitlines()
    return "\n".join(line for i, line in enumerate(lines, start=1) if i not in drop_lines)


def _normalize_promoted_code(code: str, source_dir: str) -> str:
    normalized = code or ""
    if source_dir:
        normalized = normalized.replace(str(source_dir), "AUTONOMOUS_OUTPUT_DIR")
    normalized = _strip_redundant_pathlib_import(normalized)
    return normalized.rstrip() + "\n"


def _acquisition_failure(
    source_bundle: AutonomousAnalysisBundle,
    reason: str,
    *,
    warnings: Iterable[str] = (),
) -> AcquisitionAbstraction:
    return AcquisitionAbstraction(
        strategy=(
            "structured-skill-calls-v1"
            if source_bundle.skill_calls
            else "verbatim-replay-v1"
        ),
        reusable=False,
        facade_free=False,
        reason=reason,
        source_code_sha256=hashlib.sha256(
            source_bundle.python_code.encode("utf-8")
        ).hexdigest(),
        source_steps=list(source_bundle.steps),
        source_skill_calls=list(source_bundle.skill_calls),
        warnings=list(source_bundle.trace_warnings) + list(warnings),
    )


def _is_oc_run(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "oc"
    )


def _literal_call_skill(call: ast.Call) -> str:
    if not call.args:
        return ""
    try:
        value = ast.literal_eval(call.args[0])
    except Exception:
        return ""
    return str(value) if isinstance(value, str) else ""


def _call_input_source(
    call: ast.Call,
    *,
    data_sources: dict[str, str],
    handle_sources: dict[str, int],
    original_input: str,
) -> str:
    data_node: ast.AST | None = call.args[1] if len(call.args) >= 2 else None
    input_path_node: ast.AST | None = None
    for keyword in call.keywords:
        if keyword.arg == "data":
            data_node = keyword.value
        elif keyword.arg == "input_path":
            input_path_node = keyword.value

    if isinstance(data_node, ast.Name):
        return data_sources.get(data_node.id, "")
    if (
        isinstance(data_node, ast.Attribute)
        and data_node.attr == "adata"
        and isinstance(data_node.value, ast.Name)
        and data_node.value.id in handle_sources
    ):
        return f"step:{handle_sources[data_node.value.id]}"
    if input_path_node is not None:
        if isinstance(input_path_node, ast.Name):
            return data_sources.get(input_path_node.id, "")
        try:
            literal_path = ast.literal_eval(input_path_node)
        except Exception:
            literal_path = None
        if isinstance(literal_path, (str, Path)) and str(literal_path) == original_input:
            return "input"
    return ""


def _parameter_type(value: object) -> str:
    if type(value) is bool:
        return "bool"
    if type(value) is int:
        return "int"
    if type(value) is float:
        return "float"
    if type(value) is str:
        return "str"
    return ""


def _parameter_flag_key(key: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", key.strip().lower().replace("_", "-"))
    return value.strip("-")


def build_acquisition_abstraction(
    source_bundle: AutonomousAnalysisBundle,
) -> AcquisitionAbstraction:
    """Build a deterministic, fail-closed workflow from structured evidence.

    v1 intentionally accepts only *call-composition* programs: top-level
    ``oc.run`` assignments, explicit ``handle.adata`` propagation, and the
    presentation-only ``show``/``ReturnAnswer`` calls.  Arbitrary Python,
    control flow, imports, or ambiguous data lineage stay on the quarantined
    verbatim path.  This narrow contract makes removal of the kernel facade a
    semantics-preserving translation instead of an optimistic source rewrite.
    """
    if source_bundle.engine != "mini_agent":
        return _acquisition_failure(source_bundle, "source engine has no mini-agent call trace")
    if not source_bundle.skill_calls:
        return _acquisition_failure(source_bundle, "no executed skill_calls evidence is available")
    if source_bundle.trace_warnings:
        return _acquisition_failure(
            source_bundle,
            "structured trace contains read warnings",
        )

    try:
        tree = ast.parse(source_bundle.python_code)
    except SyntaxError as exc:
        return _acquisition_failure(source_bundle, f"accepted code does not parse: {exc.msg}")

    records = list(source_bundle.skill_calls)
    calls: list[AcquisitionCall] = []
    parameters: list[AcquisitionParameter] = []
    param_cache: dict[tuple[str, type, object], int] = {}
    used_flags = set(_RESERVED_PROMOTED_FLAGS - {"method", "species"})
    data_sources: dict[str, str] = {"adata": "input", "INPUT_FILE": "input"}
    handle_sources: dict[str, int] = {}

    def register_call(call: ast.Call, target_names: list[str]) -> str | None:
        call_index = len(calls) + 1
        if call_index > len(records):
            return "accepted code contains more oc.run calls than the execution trace"
        record = records[call_index - 1]
        status = str(record.get("status") or "").strip().lower()
        if status != "succeeded":
            return f"skill call #{call_index} was not successful (status={status or 'missing'})"
        source_skill = _literal_call_skill(call)
        recorded_skill = str(record.get("skill") or "").strip()
        if not source_skill or source_skill != recorded_skill:
            return (
                f"skill call #{call_index} source/trace mismatch "
                f"({source_skill or 'dynamic'} != {recorded_skill or 'missing'})"
            )
        input_source = _call_input_source(
            call,
            data_sources=data_sources,
            handle_sources=handle_sources,
            original_input=source_bundle.input_file,
        )
        if not input_source:
            return f"skill call #{call_index} has ambiguous input lineage"
        if input_source.startswith("step:"):
            try:
                upstream = int(input_source.split(":", 1)[1])
            except ValueError:
                return f"skill call #{call_index} has invalid input lineage"
            if upstream >= call_index:
                return f"skill call #{call_index} references a non-prior step"

        raw_params = record.get("params") or {}
        if not isinstance(raw_params, dict):
            return f"skill call #{call_index} params are not an object"
        params = dict(raw_params)
        if record.get("method") is not None and "method" not in params:
            params["method"] = record.get("method")

        bindings: dict[str, str] = {}
        for raw_key, value in params.items():
            key = str(raw_key).strip().replace("-", "_")
            type_name = _parameter_type(value)
            base_flag = _parameter_flag_key(key)
            if not key or not base_flag or not type_name:
                return (
                    f"skill call #{call_index} parameter {raw_key!r} has an "
                    "unsupported name or value type"
                )
            cache_key = (key, type(value), value)
            parameter_index = param_cache.get(cache_key)
            if parameter_index is None:
                flag_key = base_flag
                if flag_key in used_flags:
                    flag_key = f"step-{call_index}-{base_flag}"
                    suffix = 2
                    while flag_key in used_flags:
                        flag_key = f"step-{call_index}-{base_flag}-{suffix}"
                        suffix += 1
                dest = flag_key.replace("-", "_")
                parameters.append(
                    AcquisitionParameter(
                        key=key,
                        flag=f"--{flag_key}",
                        dest=dest,
                        default=value,
                        type=type_name,
                        call_indexes=[call_index],
                    )
                )
                parameter_index = len(parameters) - 1
                param_cache[cache_key] = parameter_index
                used_flags.add(flag_key)
            elif call_index not in parameters[parameter_index].call_indexes:
                parameters[parameter_index].call_indexes.append(call_index)
            bindings[key] = parameters[parameter_index].dest

        calls.append(
            AcquisitionCall(
                index=call_index,
                skill=recorded_skill,
                input_source=input_source,
                parameter_bindings=bindings,
            )
        )
        for name in target_names:
            handle_sources[name] = call_index
        return None

    for statement in tree.body:
        if isinstance(statement, ast.Assign) and _is_oc_run(statement.value):
            targets = [target.id for target in statement.targets if isinstance(target, ast.Name)]
            if len(targets) != len(statement.targets) or not targets:
                return _acquisition_failure(
                    source_bundle,
                    f"unsupported oc.run assignment at line {statement.lineno}",
                )
            error = register_call(statement.value, targets)
            if error:
                return _acquisition_failure(source_bundle, error)
            continue
        if isinstance(statement, ast.Expr) and _is_oc_run(statement.value):
            error = register_call(statement.value, [])
            if error:
                return _acquisition_failure(source_bundle, error)
            continue
        if isinstance(statement, ast.Assign):
            value = statement.value
            if (
                isinstance(value, ast.Attribute)
                and value.attr == "adata"
                and isinstance(value.value, ast.Name)
                and value.value.id in handle_sources
                and all(isinstance(target, ast.Name) for target in statement.targets)
            ):
                source = f"step:{handle_sources[value.value.id]}"
                for target in statement.targets:
                    data_sources[target.id] = source
                continue
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
            and statement.value.func.id in {"show", "ReturnAnswer"}
        ):
            continue
        if isinstance(statement, ast.Pass):
            continue
        return _acquisition_failure(
            source_bundle,
            f"unsupported non-workflow statement at line {getattr(statement, 'lineno', '?')}",
        )

    if len(calls) != len(records):
        return _acquisition_failure(
            source_bundle,
            f"execution trace contains {len(records)} calls but accepted code proves {len(calls)}",
        )
    return AcquisitionAbstraction(
        strategy="structured-skill-calls-v1",
        reusable=True,
        facade_free=True,
        reason="all executed skill calls and their input lineage were proven from structured trace + AST",
        source_code_sha256=hashlib.sha256(
            source_bundle.python_code.encode("utf-8")
        ).hexdigest(),
        calls=calls,
        parameters=parameters,
        source_steps=list(source_bundle.steps),
        source_skill_calls=list(source_bundle.skill_calls),
        warnings=[],
    )


# Flags the promoted-script template already declares (or param identity
# names that select WHAT to run, not a tunable value) — a kwarg lifted to one
# of these names is forced to a suffixed flag instead of colliding silently.
_RESERVED_PROMOTED_FLAGS = frozenset({"input", "output", "demo", "method", "species"})
# `skill`/`data` identify WHICH vetted skill to call and what AnnData to pass
# it (SkillFacade.run(self, skill, data=None, ...)) — never a tunable value.
_NON_LIFTABLE_OC_RUN_KWARGS = frozenset({"skill", "data"})

_UNSET = object()  # flag_key never seen before -> take it directly
_RESERVED = object()  # flag_key pre-seeded from _RESERVED_PROMOTED_FLAGS -> always suffix


@dataclass(frozen=True)
class LiftedParam:
    """A literal ``oc.run(...)`` keyword argument promoted to a CLI flag.

    ``name`` is the python kwarg name (and, absent a collision, the argparse
    ``dest``); ``call_index`` is the 1-based source-order occurrence of the
    originating ``oc.run(...)`` call, recorded for evidence/help text only —
    NOT part of flag naming (identical name+value across calls share one
    flag; see :func:`lift_oc_run_literals`).
    """

    name: str
    flag: str
    default: object
    type: str  # "int" | "float" | "str" | "bool"
    help: str
    call_index: int


@dataclass(frozen=True)
class LiftResult:
    code: str
    lifted: list[LiftedParam] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def lift_oc_run_literals(code: str, *, oc_name: str = "oc") -> LiftResult:
    """Lift literal keyword-argument values on ``oc.run(...)`` calls into CLI flags.

    P2a of the acquisition flywheel (docs/proposals/skill-acquisition-plan.md
    §P2): a promoted skill today replays ``oc.run('<skill>', adata,
    min_genes=200)`` with every threshold frozen at whatever value the
    original run happened to use. This rewrites only the literal token spans
    of such kwargs to ``args.<dest>``, so the promoted script's
    ``parse_args()`` can expose them as overridable flags — everything else
    in the code (the facade, ``show()``/``ReturnAnswer()``, non-``oc.run``
    calls, comments, formatting) is left untouched: this is a surgical span
    splice on the original source text, not a whole-module re-render.

    Deliberately narrow: only ``bool``/``int``/``float``/``str`` literal
    kwargs qualify (via ``ast.literal_eval``, the same idiom as
    ``_extract_setup_literals``); expressions, names, and list/dict literals
    (e.g. a gene panel) are left as-is and reported in ``skipped`` — lifting
    those needs a materially different (``nargs``-aware) flag shape with no
    motivating case yet. ``skill``/``data`` (the call's identity, not a
    tunable value) are never lift candidates.

    ``oc`` is trusted as a fixed name — the mini-agent kernel always binds it
    this way, and ``omicsclaw.autonomous.mini_agent``'s own
    ``_references_oc`` makes the same bare ``ast.Name`` assumption with no
    alias tracing — EXCEPT: if the module ever rebinds ``oc`` (an
    ``ast.Store`` context), the whole lift is skipped rather than risk
    misattributing a call to a name that may no longer mean what we think.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return LiftResult(code=code, skipped=["code does not parse; lift skipped"])

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id == oc_name:
            return LiftResult(code=code, skipped=[f"{oc_name!r} is rebound in this code; lift skipped"])

    calls = sorted(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == oc_name
        ),
        key=lambda call: (call.lineno, call.col_offset),
    )
    if not calls:
        return LiftResult(code=code)

    # The whole transform is best-effort: any unforeseen failure here must
    # degrade to "no lift" (never crash create_skill_scaffold), on top of the
    # ast.parse sanity check below and the P1 gate's own fallback path.
    try:
        return _apply_oc_run_lift(code, calls)
    except Exception as exc:  # noqa: BLE001 - intentionally broad, see above
        return LiftResult(code=code, skipped=[f"lift raised {exc!r}; left unchanged"])


def _line_byte_offset_to_char_offset(line: str, byte_col: int) -> int:
    """Convert a CPython ``ast`` ``col_offset`` (UTF-8 BYTES from line start)
    to a character offset into ``line``.

    For a ``str`` source containing non-ASCII characters, ``col_offset``/
    ``end_col_offset`` are measured in UTF-8 bytes, not characters — a
    verified CPython quirk (e.g. two CJK characters occupy 2 Python-string
    characters but 6 UTF-8 bytes). Treating them as character offsets
    silently corrupts the splice on any line with a preceding multi-byte
    character. A ``col_offset`` always falls on a token boundary, which is
    always a full-character boundary too, so slicing the line's UTF-8
    encoding at ``byte_col`` and decoding it back is exact, never partial.
    """
    return len(line.encode("utf-8")[:byte_col].decode("utf-8"))


def _apply_oc_run_lift(code: str, calls: list[ast.Call]) -> LiftResult:
    lines = code.splitlines(keepends=True)
    line_starts = [0]
    for line in lines:
        line_starts.append(line_starts[-1] + len(line))

    def offset(lineno: int, byte_col: int) -> int:
        return line_starts[lineno - 1] + _line_byte_offset_to_char_offset(lines[lineno - 1], byte_col)

    used: dict[str, object] = dict.fromkeys(_RESERVED_PROMOTED_FLAGS, _RESERVED)
    # Cache keyed by (original flag_key, literal value): every occurrence of
    # the SAME kwarg name with the SAME value — whether or not the name
    # collided with a reserved flag and had to be suffixed — must resolve to
    # the exact same LiftedParam, not mint a fresh suffix each time.
    resolved: dict[tuple[str, object], LiftedParam] = {}
    splices: list[tuple[int, int, str]] = []
    skipped: list[str] = []

    for call_index, call in enumerate(calls, start=1):
        try:
            skill_label = ast.literal_eval(call.args[0]) if call.args else "?"
        except Exception:
            skill_label = "?"
        for kw in call.keywords:
            if kw.arg is None or kw.arg in _NON_LIFTABLE_OC_RUN_KWARGS:
                continue  # **kwargs spread, or the call's identity (skill/data)
            try:
                value = ast.literal_eval(kw.value)
            except Exception:
                skipped.append(f"oc.run call #{call_index} kwarg {kw.arg!r}: not a literal, left as-is")
                continue
            if type(value) not in (bool, int, float, str):
                skipped.append(
                    f"oc.run call #{call_index} kwarg {kw.arg!r}: "
                    f"{type(value).__name__} literal not liftable, left as-is"
                )
                continue

            original_key = kw.arg.lower().replace("_", "-")
            # Include the literal's TYPE, not just its value: Python's `==`
            # (and hash) treat `True == 1` and `1 == 1.0`, so a bare
            # (name, value) key would wrongly collapse e.g. `cutoff=True`
            # and a later `cutoff=1` into one shared flag/default.
            cache_key = (original_key, type(value), value)
            param = resolved.get(cache_key)
            if param is None:
                flag_key = original_key
                if used.get(flag_key, _UNSET) is not _UNSET:
                    n = 2
                    while f"{flag_key}-{n}" in used:
                        n += 1
                    flag_key = f"{flag_key}-{n}"
                # Preserve the kwarg's original casing in `dest` even when
                # suffixed (flag_key is lowercased for the CLI flag string).
                dest = kw.arg if flag_key == original_key else f"{kw.arg}_{flag_key.rsplit('-', 1)[-1]}"
                type_name = "bool" if isinstance(value, bool) else type(value).__name__
                param = LiftedParam(
                    name=dest,
                    flag=f"--{flag_key}",
                    default=value,
                    type=type_name,
                    help=f"Override {kw.arg} for oc.run({skill_label!r}, ...) (call #{call_index})",
                    call_index=call_index,
                )
                used[flag_key] = value
                resolved[cache_key] = param

            start = offset(kw.value.lineno, kw.value.col_offset)
            end = offset(kw.value.end_lineno, kw.value.end_col_offset)
            splices.append((start, end, f"args.{param.name}"))

    if not splices:
        return LiftResult(code=code, skipped=skipped)

    rewritten = code
    for start, end, replacement in sorted(splices, key=lambda triple: triple[0], reverse=True):
        rewritten = rewritten[:start] + replacement + rewritten[end:]

    try:
        ast.parse(rewritten)
    except SyntaxError:
        return LiftResult(code=code, skipped=skipped + ["rewritten code failed to parse; lift discarded"])

    lifted = list({param.flag: param for param in resolved.values()}.values())
    lifted.sort(key=lambda param: param.call_index)
    return LiftResult(code=rewritten, lifted=lifted, skipped=skipped)


def _is_autonomous_run_dir(child: Path) -> bool:
    """Identity gate for the weaker mini-agent signal (a root ``analysis.py``).

    A regular skill output can also carry ``result_summary.md``, so a mini-agent
    run must additionally look autonomous: the canonical run-dir prefix, or a
    manifest whose ``metadata.source`` is the autonomous runner. The legacy
    notebook+plan layout is self-identifying and does not go through this gate.
    """
    from omicsclaw.autonomous.contracts import (
        AUTONOMOUS_CODE_RUNNER_SOURCE,
        AUTONOMOUS_RUN_DIR_PREFIX,
    )

    if child.name.startswith(AUTONOMOUS_RUN_DIR_PREFIX) or child.name.startswith("autonomous-analysis"):
        return True
    manifest = child / "manifest.json"
    if manifest.is_file():
        try:
            meta = json.loads(manifest.read_text(encoding="utf-8")).get("metadata", {}) or {}
        except (json.JSONDecodeError, OSError):
            return False
        return str(meta.get("source", "")) == AUTONOMOUS_CODE_RUNNER_SOURCE
    return False


def _autonomous_run_candidate(child: Path) -> tuple[float, Path] | None:
    """Return ``(mtime, dir)`` when ``child`` is a promotable autonomous run."""
    if not child.is_dir():
        return None
    completion_path = child / COMPLETION_REPORT_FILENAME
    if completion_path.is_file():
        try:
            completion = _load_completion_report(completion_path)
        except json.JSONDecodeError:
            return None
        if not bool(completion.get("completed", False)):
            return None
    summary_path = child / "result_summary.md"
    if not summary_path.is_file():
        return None
    legacy_nb = child / "reproducibility" / "analysis_notebook.ipynb"
    legacy_plan = child / "analysis_plan.md"
    mini_code = child / "analysis.py"
    if legacy_nb.is_file() and legacy_plan.is_file():
        code_path = legacy_nb
    elif mini_code.is_file() and _is_autonomous_run_dir(child):
        code_path = mini_code
    else:
        return None
    latest_ts = max(code_path.stat().st_mtime, summary_path.stat().st_mtime, child.stat().st_mtime)
    return (latest_ts, child)


def find_latest_autonomous_analysis(output_root: Path | None = None) -> Path | None:
    root = Path(output_root or OUTPUT_DIR)
    if not root.exists():
        return None
    from omicsclaw.common.run_paths import PROJECT_META_FILENAME

    candidates: list[tuple[float, Path]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        cand = _autonomous_run_candidate(child)
        if cand is not None:
            candidates.append(cand)
        # ADR 0035: a run nests under output_root/<project>/ when project_id is set.
        # Descend one level into real project dirs (they carry project_meta.json) so
        # Bench-thread runs are discoverable, without walking arbitrary subtrees.
        if (child / PROJECT_META_FILENAME).is_file():
            for grandchild in child.iterdir():
                cand = _autonomous_run_candidate(grandchild)
                if cand is not None:
                    candidates.append(cand)

    if not candidates:
        return None

    return max(candidates, key=lambda item: item[0])[1]


def refresh_registry() -> bool:
    try:
        from .registry import registry

        registry._loaded = False
        registry.skills.clear()
        registry.lazy_skills.clear()
        registry.load_all()
        return True
    except Exception:
        return False


@dataclass
class _DemoGateOutcome:
    """Outcome of the P1 acquisition gate's one-shot ``--demo`` smoke run.

    - ``earned``: the script ran to completion, its result.json satisfies
      :func:`~omicsclaw.common.report.validate_result_envelope`, and its
      status is not the scaffold-placeholder sentinel — a real or promoted
      body that actually works. The caller upgrades ``validation.level`` to
      ``demo-validated``.
    - ``skipped``: a legitimate reason NOT to judge this run — either an
      unimplemented placeholder (MF1: status == SCAFFOLD_STATUS is a
      deliberate "not implemented yet" signal, not a failure) or a promoted
      body that could not run for a reason outside this gate's control
      (missing dependency/input — MF3/MF6). A promoted body is quarantined;
      a first-party placeholder may still enter the developer catalog as a
      non-routable draft.
    - ``rejected``: a genuine crash, or a result.json that is missing,
      unparseable, or fails the envelope contract. The caller raises so
      ``isolated_workspace`` rmtree's the staging dir and the skill never
      lands in the catalog.
    """

    verdict: str
    reason: str
    envelope: dict | None = None


# Exception TYPES (anchored at the start of a traceback line — how Python
# prints an uncaught exception, e.g. "ModuleNotFoundError: No module named
# 'x'") that mean "this environment limitation is outside the gate's
# control", not "the promoted code is broken" (MF3 in the P0/P1 plan):
# the raw staged subprocess never reaches `resolve_skill_runtime`'s
# adaptive-env provisioning, so a promoted skill needing a heavy optional
# dependency is expected to fail here. A start-of-line anchor (not a bare
# substring) is deliberate: a genuine bug whose OWN message happens to
# mention "ImportError" (e.g. a RuntimeError with that word in its text)
# must not be misclassified as an environment limitation — that would let
# broken promoted code slip into the catalog.
_DEMO_GATE_SKIP_EXCEPTION_TYPES = re.compile(
    r"^(?:ModuleNotFoundError|ImportError):", re.MULTILINE
)
# FileNotFoundError is deliberately NOT in the type-name set above: unlike a
# missing optional dependency, a FileNotFoundError can come from ANYTHING the
# promoted body references — including a typo'd internal path in the
# model-authored code, which is a real bug, not an environment limitation.
# MF6's actual intent was narrower: "a promoted skill's original demo input
# can go stale between the source run and promotion" — i.e. only a
# FileNotFoundError that references that SPECIFIC known path (threaded in as
# ``original_input_file``, the same ``DEFAULT_INPUT_FILE`` --demo reuses per
# render_promoted_skill_script) should be tolerated as a skip. Any other
# FileNotFoundError is rejected like any other genuine crash.
_DEMO_GATE_FILE_NOT_FOUND = re.compile(r"^FileNotFoundError:", re.MULTILINE)
# The exact SystemExit message our own promoted-script template raises when
# no demo input is available at all (render_promoted_skill_script) — a plain
# substring is fine here since this is a long, specific, first-party string,
# not a generic exception type name a message could coincidentally contain.
_DEMO_GATE_SKIP_MESSAGE = "Provide --input, or use --demo to reuse the original autonomous-analysis input."


def _demo_gate_skip_reason(combined_output: str, original_input_file: str = "") -> str | None:
    """Classify a nonzero --demo exit as an environment limitation, if any."""
    match = _DEMO_GATE_SKIP_EXCEPTION_TYPES.search(combined_output)
    if match:
        return f"environment/input limitation ({match.group(0)[:-1]})"
    if _DEMO_GATE_SKIP_MESSAGE in combined_output:
        return "no demo input available"
    if (
        original_input_file
        and _DEMO_GATE_FILE_NOT_FOUND.search(combined_output)
        and original_input_file in combined_output
    ):
        return "environment/input limitation (stale original input, FileNotFoundError)"
    return None


def _run_demo_smoke_gate(
    script_path: Path,
    output_dir: Path,
    *,
    require_sandbox: bool = False,
    input_file: str = "",
) -> _DemoGateOutcome:
    """Run ``script_path --demo`` once in ``output_dir`` and classify the result.

    Two isolation tiers, matching the plan's MF4 decision (demo validation
    is not automatically sandbox validation, but untrusted code must be):

    - ``require_sandbox=False`` — a freshly-scaffolded or corpus-derived
      body (self-authored, not lifted from an untrusted source): light demo
      validation is enough. Runs in the base interpreter with only
      ``PYTHONPATH``/``PYTHONNOUSERSITE`` set, mirroring ``runner.py``'s
      subprocess env — no adaptive-env provisioning either (a raw staged
      subprocess never reaches ``resolve_skill_runtime`` — MF3).
    - ``require_sandbox=True`` — a skill promoted verbatim from an
      autonomous run, i.e. UNTRUSTED model-authored code: must go through an
      OS-level sandbox before its result is trusted. Reuses ADR 0032's
      already-built bwrap envelope (``omicsclaw.autonomous.kernel_envelope``
      — pure argv/env builders, decoupled from the mini-agent's persistent
      kernel loop). When bwrap is unavailable this does NOT fall back to an
      unsandboxed run: it returns ``skipped`` (the same "don't block
      creation, but don't award false credit" philosophy already used below
      for a missing optional dependency or missing input).

    ``input_file`` (the promoted bundle's original input path, when any) is
    threaded through for two reasons: it is the sandbox's one legitimate
    external read target, and it is the only path whose FileNotFoundError is
    tolerated as a "stale input" skip rather than a rejection (see
    ``_demo_gate_skip_reason``).
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = str(OMICSCLAW_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("PYTHONNOUSERSITE", "1")

    argv = [sys.executable, str(script_path), "--demo", "--output", str(output_dir)]

    if require_sandbox:
        from omicsclaw.autonomous.kernel_envelope import (
            EnvelopeConfig,
            build_bwrap_argv,
            build_launch_env,
            envelope_available,
        )

        if not envelope_available():
            return _DemoGateOutcome(
                verdict="skipped",
                reason=(
                    "bwrap unavailable; cannot sandbox untrusted promoted code "
                    "before trusting its --demo run"
                ),
            )

        input_path = Path(input_file) if input_file else None
        read_roots = []
        if input_path is not None:
            read_roots = [input_path if input_path.is_dir() else input_path.parent]

        # bwrap's --bind requires the source to already exist on the host;
        # the promoted script itself creates output_dir on startup, which is
        # too late for the bind to be set up.
        output_dir.mkdir(parents=True, exist_ok=True)
        config = EnvelopeConfig(
            workspace_root=output_dir,
            ipc_dir=output_dir,
            repo_root=OMICSCLAW_DIR,
            read_roots=read_roots,
            allow_network=False,
            extra_env={"PYTHONPATH": str(OMICSCLAW_DIR)},
        )
        argv = build_bwrap_argv(config, argv)
        env = build_launch_env(config)

    try:
        proc = subprocess.run(
            argv,
            cwd=str(OMICSCLAW_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=_DEMO_SMOKE_GATE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return _DemoGateOutcome(
            verdict="rejected",
            reason=f"--demo did not finish within {_DEMO_SMOKE_GATE_TIMEOUT_SECONDS}s",
        )

    if proc.returncode != 0:
        combined = f"{proc.stdout}\n{proc.stderr}"
        skip_reason = _demo_gate_skip_reason(combined, original_input_file=input_file)
        if skip_reason is not None:
            return _DemoGateOutcome(verdict="skipped", reason=f"--demo exited {proc.returncode}: {skip_reason}")
        return _DemoGateOutcome(
            verdict="rejected",
            reason=f"--demo exited {proc.returncode}:\n{proc.stderr.strip()[-2000:]}",
        )

    result_path = output_dir / "result.json"
    try:
        envelope = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _DemoGateOutcome(
            verdict="rejected",
            reason=f"--demo exited 0 but result.json is missing/unparseable: {exc}",
        )

    problems = validate_result_envelope(envelope)
    if problems:
        return _DemoGateOutcome(
            verdict="rejected",
            reason=f"result.json failed the envelope contract: {problems}",
            envelope=envelope,
        )

    if envelope.get("status") == SCAFFOLD_STATUS:
        return _DemoGateOutcome(
            verdict="skipped",
            reason="unimplemented scaffold placeholder (status: scaffold)",
            envelope=envelope,
        )

    return _DemoGateOutcome(
        verdict="earned", reason="--demo ran and produced a valid result.json", envelope=envelope
    )


def _render_validation_evidence(
    script_name: str, gate: _DemoGateOutcome, lift_result: LiftResult | None = None
) -> str:
    """Durable record of the P1 --demo smoke gate credit.

    SF1: the staging tmp dir this ran in is rmtree'd on ``create_skill_scaffold``
    exit, so the evidence a ``demo-validated`` skill.yaml points to must live
    here — a persisted file — rather than referencing the ephemeral tmp path.
    """
    envelope = gate.envelope or {}
    summary_json = json.dumps(envelope.get("summary", {}), indent=2, ensure_ascii=False)
    status = envelope.get("status", "")
    lifted_note = (
        "\nThis skill also had literal `oc.run(...)` parameters lifted to CLI "
        "flags — see `references/parameter_lift.md`.\n"
        if lift_result is not None and lift_result.lifted
        else ""
    )
    return f"""# Demo Validation Evidence

Earned `demo-validated` via the acquisition-flywheel P1 `--demo` smoke gate at
skill-creation time (see `docs/proposals/skill-acquisition-p0-p1-landing.md`).

**Command re-run for a fresh check:**

```bash
python {script_name} --demo --output <output_dir>
```

**Outcome**: {gate.reason}

**result.json status**: `{status}`

**result.json summary**:

```json
{summary_json}
```
{lifted_note}"""


def _render_quarantine_evidence(script_name: str, gate: _DemoGateOutcome) -> str:
    """Durable explanation for keeping an unverified promotion undiscoverable."""
    return f"""# Acquisition Quarantine Evidence

This promoted skill contains model-authored code and did not earn admission to
the discoverable skill tree because its required sandboxed `--demo` gate was
skipped.

**Attempted command:**

```bash
python {script_name} --demo --output <output_dir>
```

**Gate verdict:** `skipped`

**Reason:** {gate.reason}

Re-run the acquisition gate with the OS sandbox available, or perform an
explicit human review and validation before moving this skill into a domain
directory. Files under `skills/{QUARANTINE_DIRNAME}/` are intentionally ignored
by registry discovery and automatic routing.
"""


def _render_parameter_lift_evidence(lift_result: LiftResult, *, fallback_reason: str | None = None) -> str:
    """Durable record of the P2a literal-lift pass (acquisition-plan.md §P2).

    Lists which ``oc.run(...)`` kwargs became CLI flags, or — if the lift
    broke the --demo gate — that the promoted skill shipped its verbatim
    body instead and why, so the decision isn't silently invisible later.
    """
    lines = ["# Parameter Lift Evidence", ""]
    if fallback_reason is not None:
        lines += [
            "The literal-lift pass produced a script that failed the --demo smoke "
            "gate, so this skill shipped its **verbatim** (unlifted) body instead.",
            "",
            f"**Fallback reason**: {fallback_reason}",
        ]
    elif lift_result.lifted:
        lines.append("Lifted the following `oc.run(...)` literal keyword arguments into CLI flags:")
        lines.append("")
        lines.append("| flag | default | type | source |")
        lines.append("|---|---|---|---|")
        for param in lift_result.lifted:
            lines.append(f"| `{param.flag}` | `{param.default!r}` | {param.type} | {param.help} |")
    else:
        lines.append("No literal `oc.run(...)` keyword arguments were found to lift.")
    if lift_result.skipped:
        lines.append("")
        lines.append("Skipped (left as-is):")
        lines.extend(f"- {reason}" for reason in lift_result.skipped)
    return "\n".join(lines) + "\n"


def _render_corpus_provenance_evidence(bundle: CorpusDerivedBundle) -> str:
    """Durable record of P5's corpus extraction (acquisition-plan.md §P5).

    Lists every candidate's param/value-or-TODO/quote/span/doc_ref so the
    extraction is auditable without re-running it — the human-readable sibling
    of ``skill.yaml``'s machine-readable ``hints[method].source_refs``.
    """
    lines = ["# Corpus Provenance Evidence", ""]
    lines.append(f"**Source**: {bundle.source_kind.replace('_', ' ')} — `{bundle.doc_ref}`")
    lines.append("")
    if not bundle.candidates:
        lines.append("No methodology parameters were extracted from this source.")
        return "\n".join(lines) + "\n"
    lines.append("| param | value | quote | span | source |")
    lines.append("|---|---|---|---|---|")
    for c in bundle.candidates:
        value_cell = "TODO" if c.todo else repr(c.value)
        # A raw newline/tab in the quote would break this table row across
        # multiple physical lines (same root cause fixed in
        # _render_corpus_add_argument_line's comment embedding).
        quote_cell = _escape_comment_text(c.quote).replace("|", "\\|")
        lines.append(
            f"| `{c.param}` | {value_cell} | \"{quote_cell}\" | "
            f"{c.char_span[0]}-{c.char_span[1]} | `{bundle.doc_ref}` |"
        )
    return "\n".join(lines) + "\n"


def create_skill_scaffold(
    *,
    request: str,
    domain: str,
    skill_name: str = "",
    summary: str = "",
    input_formats: Iterable[str] | None = None,
    primary_outputs: Iterable[str] | None = None,
    methods: Iterable[str] | None = None,
    trigger_keywords: Iterable[str] | None = None,
    create_tests: bool = True,
    skills_root: Path | None = None,
    source_analysis_dir: Path | str | None = None,
    promote_from_latest: bool = False,
    output_root: Path | None = None,
    from_corpus: Path | str | None = None,
    corpus_source_kind: str = "paper",
    doc_ref: str = "",
) -> SkillScaffoldResult:
    hook_runtime = build_default_lifecycle_hook_runtime(OMICSCLAW_DIR)
    source_bundle: AutonomousAnalysisBundle | None = None
    resolved_source_dir: Path | None = None

    # P5 (acquisition-plan.md §P5): corpus-derived scaffolding is a third,
    # independent branch — mutually exclusive with the promotion path since a
    # paper/tool-docs text has no executable python_code to promote. Check the
    # raw arguments before source loading and before rejecting the legacy global
    # latest flag, so callers always get the most specific contract error.
    if from_corpus and (source_analysis_dir or promote_from_latest):
        raise ValueError(
            "from_corpus is mutually exclusive with source_analysis_dir/promote_from_latest."
        )

    if promote_from_latest:
        raise ValueError(
            "promote_from_latest is disabled because a global mtime scan can select "
            "another session's run; provide the exact source_analysis_dir instead."
        )

    if source_analysis_dir:
        resolved_source_dir = Path(source_analysis_dir)
        if not resolved_source_dir.is_absolute():
            resolved_source_dir = (OMICSCLAW_DIR / resolved_source_dir).resolve()

    if resolved_source_dir is not None:
        source_bundle = _load_autonomous_bundle(resolved_source_dir)

    corpus_bundle: CorpusDerivedBundle | None = None
    resolved_doc_ref = ""
    if from_corpus:
        corpus_path = Path(from_corpus)
        if not corpus_path.is_absolute():
            corpus_path = (OMICSCLAW_DIR / corpus_path).resolve()
        corpus_text = corpus_path.read_text(encoding="utf-8")
        resolved_doc_ref = (doc_ref or "").strip() or corpus_path.name
        corpus_bundle = _load_corpus_bundle(
            corpus_text, source_kind=corpus_source_kind, doc_ref=resolved_doc_ref
        )

    if source_bundle and not domain:
        domain = source_bundle.domain
    domain = (domain or "").strip().lower()
    if not domain:
        raise ValueError("A target domain is required when it cannot be inferred from the source analysis.")
    if domain not in VALID_DOMAINS:
        raise ValueError(f"Unsupported domain: {domain}")

    resolved_root = Path(skills_root or SKILLS_DIR)
    if not resolved_root.is_absolute():
        resolved_root = (OMICSCLAW_DIR / resolved_root).resolve()
    target_root = resolved_root / domain
    target_root.mkdir(parents=True, exist_ok=True)

    resolved_skill_name = infer_skill_name(request, domain, preferred_name=skill_name)
    final_skill_dir = target_root / resolved_skill_name
    if final_skill_dir.exists():
        raise FileExistsError(f"Skill directory already exists: {final_skill_dir}")

    script_name = f"{resolved_skill_name.replace('-', '_')}.py"
    spec_payload = {
        "request": request,
        "summary": summary,
        "skill_name": resolved_skill_name,
        "domain": domain,
        "methods": _unique(methods or []),
        "input_formats": _unique(input_formats or []),
        "primary_outputs": _unique(primary_outputs or []),
        "trigger_keywords": _unique(trigger_keywords or []),
        "template_path": str(SKILL_TEMPLATE_PATH),
        "source_analysis_dir": str(resolved_source_dir) if resolved_source_dir else "",
        "promoted_from_autonomous_analysis": bool(source_bundle),
        "corpus_derived": bool(corpus_bundle),
        "corpus_source_kind": corpus_source_kind if corpus_bundle else "",
        "doc_ref": resolved_doc_ref,
    }
    manifest_metadata = {
        "domain": domain,
        "skill_name": resolved_skill_name,
        "request": request,
        "promoted_from_autonomous_analysis": bool(source_bundle),
        "source_analysis_dir": str(resolved_source_dir) if resolved_source_dir else "",
        "corpus_derived": bool(corpus_bundle),
        "doc_ref": resolved_doc_ref,
    }
    relative_created_paths: list[Path] = []
    committed_skill_dir = final_skill_dir
    quarantined = False
    quarantine_reason_path = ""

    with isolated_workspace(STAGING_ROOT, prefix="skill-scaffold") as staging_root:
        skill_dir = staging_root / resolved_skill_name
        skill_dir.mkdir(parents=True, exist_ok=False)

        skill_md_path = skill_dir / "SKILL.md"
        script_path = skill_dir / script_name
        spec_path = skill_dir / "scaffold_spec.json"
        test_path = skill_dir / "tests" / f"test_{script_name}"

        # v2 layout (ADR 0037): skill.yaml is the machine contract; SKILL.md is a
        # narrative card whose header + I/O summary are generated FROM the manifest.
        from .skill_md import render_skill_md

        # Render the entry script first so a PROMOTED skill can seed deps.python
        # from its real (bootstrap + accepted-cell) import surface. The default
        # placeholder script is stdlib-only, so its deps stay empty.
        normalized_code = ""
        lift_result: LiftResult | None = None
        abstraction: AcquisitionAbstraction | None = None
        abstraction_applied = False
        abstraction_fallback_reason = ""
        corpus_method = "default"
        if source_bundle is not None:
            normalized_code = _normalize_promoted_code(source_bundle.python_code, source_bundle.source_dir)
            abstraction = build_acquisition_abstraction(source_bundle)
            if abstraction.reusable:
                script_text = render_structured_promoted_skill_script(
                    skill_name=resolved_skill_name,
                    domain=domain,
                    summary=summary or source_bundle.goal,
                    source_bundle=source_bundle,
                    abstraction=abstraction,
                )
                abstraction_applied = True
            else:
                # P2a fallback for arbitrary Python whose workflow semantics
                # cannot be proven from trace + AST.  It remains facade-backed,
                # sandbox-gated, and its narrow literal kwargs are still lifted.
                lift_result = (
                    lift_oc_run_literals(normalized_code)
                    if source_bundle.engine == "mini_agent"
                    else LiftResult(code=normalized_code)
                )
                script_text = render_promoted_skill_script(
                    skill_name=resolved_skill_name,
                    domain=domain,
                    summary=summary or source_bundle.goal,
                    source_bundle=source_bundle,
                    body_code=lift_result.code,
                    lifted_params=lift_result.lifted,
                )
            deps_python = _scan_third_party_imports(script_text)
        elif corpus_bundle is not None:
            corpus_method = (_unique(methods or []) or ["default"])[0]
            script_text = render_corpus_skill_script(
                skill_name=resolved_skill_name,
                domain=domain,
                summary=summary or corpus_bundle.goal,
                corpus_bundle=corpus_bundle,
                method=corpus_method,
            )
            deps_python = []
        else:
            script_text = render_skill_script(
                skill_name=resolved_skill_name,
                domain=domain,
                summary=summary,
                methods=methods or [],
            )
            deps_python = []

        manifest = build_scaffold_manifest(
            skill_name=resolved_skill_name,
            domain=domain,
            trigger_keywords=trigger_keywords or [],
            source_bundle=source_bundle,
            corpus_bundle=corpus_bundle,
            deps_python=deps_python,
            request=request,
            summary=summary,
            method=corpus_method,
        )
        (skill_dir / "skill.yaml").write_text(manifest.to_yaml(), encoding="utf-8")
        relative_created_paths.append(Path("skill.yaml"))

        _fallback_goal = source_bundle.goal if source_bundle else (corpus_bundle.goal if corpus_bundle else "")
        narrative_md = render_skill_markdown(
            skill_name=resolved_skill_name,
            domain=domain,
            summary=summary or _fallback_goal,
            request=request or _fallback_goal,
            methods=methods or [],
            input_formats=input_formats or [],
            primary_outputs=primary_outputs or [],
            trigger_keywords=trigger_keywords or [],
            source_bundle=source_bundle,
            corpus_bundle=corpus_bundle,
        )
        skill_md_path.write_text(
            render_skill_md(manifest, narrative_md),
            encoding="utf-8",
        )
        relative_created_paths.append(Path("SKILL.md"))

        references_dir = skill_dir / "references"
        references_dir.mkdir(parents=True, exist_ok=True)
        v2_reference_files = {
            "methodology.md": _REFERENCE_METHODOLOGY,
            "output_contract.md": _REFERENCE_OUTPUT_CONTRACT,
            "r_visualization.md": _REFERENCE_R_VISUALIZATION,
        }
        for fname, content in v2_reference_files.items():
            (references_dir / fname).write_text(content, encoding="utf-8")
            relative_created_paths.append(Path("references") / fname)

        # parameters.md is auto-generated from the v2 manifest so it stays in
        # sync with `skill_lint._lint_v2` + `generate_parameters_md --check`
        # (byte-for-byte diff on the v2 track).
        (references_dir / "parameters.md").write_text(
            _render_parameters_md_from_manifest(manifest, script_text),
            encoding="utf-8",
        )
        relative_created_paths.append(Path("references") / "parameters.md")

        script_path.write_text(script_text, encoding="utf-8")
        relative_created_paths.append(Path(script_name))

        if create_tests:
            test_path.parent.mkdir(parents=True, exist_ok=True)
            (test_path.parent / "__init__.py").write_text("", encoding="utf-8")
            test_path.write_text(render_skill_test(resolved_skill_name), encoding="utf-8")
            relative_created_paths.extend(
                [
                    Path("tests") / "__init__.py",
                    Path("tests") / f"test_{script_name}",
                ]
            )

        reference_relative_paths: list[str] = []
        if source_bundle is not None:
            # references_dir already created above as part of the v2 layout.
            reference_targets = {
                "source_analysis_notebook.ipynb": Path(source_bundle.notebook_path),
                "source_result_summary.md": resolved_source_dir / "result_summary.md",
                "source_analysis_plan.md": resolved_source_dir / "analysis_plan.md",
                "source_web_sources.md": resolved_source_dir / "web_sources.md",
                "source_skill_calls.jsonl": resolved_source_dir / "skill_calls.jsonl",
                "source_manifest.json": resolved_source_dir / "manifest.json",
                "source_completion_report.json": resolved_source_dir / COMPLETION_REPORT_FILENAME,
            }
            for filename, source_path in reference_targets.items():
                # is_file() (not exists()): a mini-agent bundle has no notebook, so
                # notebook_path is "" → Path("") == Path(".") which exists as a dir
                # and would make shutil.copy2 raise IsADirectoryError.
                if source_path.is_file():
                    dest = references_dir / filename
                    shutil.copy2(source_path, dest)
                    rel_path = Path("references") / filename
                    reference_relative_paths.append(str(rel_path))
                    relative_created_paths.append(rel_path)

        if corpus_bundle is not None:
            # references_dir already created above as part of the v2 layout.
            # The raw corpus text is what lets skill_lint's
            # _check_corpus_source_refs re-slice each char_span and confirm it
            # actually equals its quote — not just check the structural shape.
            (references_dir / "source_corpus.txt").write_text(
                corpus_bundle.corpus_text, encoding="utf-8"
            )
            reference_relative_paths.append(str(Path("references") / "source_corpus.txt"))
            relative_created_paths.append(Path("references") / "source_corpus.txt")

            (references_dir / "corpus_provenance.md").write_text(
                _render_corpus_provenance_evidence(corpus_bundle), encoding="utf-8"
            )
            reference_relative_paths.append(str(Path("references") / "corpus_provenance.md"))
            relative_created_paths.append(Path("references") / "corpus_provenance.md")

        spec_path.write_text(
            json.dumps(spec_payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        relative_created_paths.append(Path("scaffold_spec.json"))

        requirements = _skill_scaffold_requirements(
            script_name=script_name,
            create_tests=create_tests,
            reference_paths=reference_relative_paths,
        )
        staged_manifest_path = update_workspace_manifest(
            skill_dir,
            workspace_kind=WORKSPACE_KIND_ANALYSIS_RUN,
            workspace_purpose=(
                "skill_promotion"
                if source_bundle is not None
                else "skill_scaffold"
            ),
            requirements=requirements,
            step=StepRecord(
                skill="create_omics_skill",
                version=SKILL_SCAFFOLDER_VERSION,
                input_file=str(resolved_source_dir) if resolved_source_dir else request,
                output_file=str(final_skill_dir),
                params={
                    "domain": domain,
                    "skill_name": resolved_skill_name,
                    "create_tests": create_tests,
                    "promoted_from_autonomous_analysis": bool(source_bundle),
                },
            ),
            isolation_mode="staging_copy",
            metadata=manifest_metadata,
        )
        relative_created_paths.append(Path("manifest.json"))

        completion_report = build_completion_report(
            skill_dir,
            workspace_kind=WORKSPACE_KIND_ANALYSIS_RUN,
            workspace_purpose=(
                "skill_promotion"
                if source_bundle is not None
                else "skill_scaffold"
            ),
            requirements=requirements,
            manifest_path=str(staged_manifest_path),
            metadata=manifest_metadata,
        )
        if not completion_report.completed:
            raise RuntimeError(
                "Skill scaffold verification failed.\n"
                + format_completion_summary(completion_report)
            )
        write_completion_report(
            skill_dir,
            completion_report,
            hook_runtime=hook_runtime,
            hook_context={
                "workspace": str(skill_dir),
                "source": "skill_scaffolder",
            },
        )
        update_workspace_manifest(
            skill_dir,
            workspace_kind=WORKSPACE_KIND_ANALYSIS_RUN,
            workspace_purpose=(
                "skill_promotion"
                if source_bundle is not None
                else "skill_scaffold"
            ),
            requirements=requirements,
            completion_report=completion_report,
            isolation_mode="staging_copy",
            metadata=manifest_metadata,
            append_step=False,
        )
        relative_created_paths.append(Path(COMPLETION_REPORT_FILENAME))

        # P1 acquisition gate: run --demo once, in staging, before this skill
        # is allowed to enter the catalog. A genuine crash raises here so
        # isolated_workspace rmtree's the staging dir (never reaches move); a
        # skip (placeholder / env-limited promoted body) proceeds unchanged;
        # an earn upgrades validation.level and rewrites skill.yaml in place.
        # require_sandbox=True whenever this is a promotion (source_bundle is
        # not None): that body is untrusted model-authored code, not a
        # self-authored scaffold/corpus template.
        demo_gate = _run_demo_smoke_gate(
            script_path,
            staging_root / "_demo_smoke_gate_output",
            require_sandbox=source_bundle is not None,
            input_file=source_bundle.input_file if source_bundle is not None else "",
        )
        lift_fallback_reason: str | None = None
        if (
            demo_gate.verdict == "rejected"
            and abstraction is not None
            and abstraction_applied
        ):
            # The structured translation is useful only if it independently
            # clears the same sandboxed execution gate.  On any semantic
            # regression, retry the untouched accepted code once and retain
            # the reason in acquisition_abstraction.json.
            abstraction_fallback_reason = demo_gate.reason
            verbatim_text = render_promoted_skill_script(
                skill_name=resolved_skill_name,
                domain=domain,
                summary=summary or source_bundle.goal,
                source_bundle=source_bundle,
                body_code=normalized_code,
                lifted_params=[],
            )
            script_path.write_text(verbatim_text, encoding="utf-8")
            script_text = verbatim_text
            abstraction_applied = False
            manifest.deps.python = _scan_third_party_imports(verbatim_text)
            (skill_dir / "skill.yaml").write_text(manifest.to_yaml(), encoding="utf-8")
            (references_dir / "parameters.md").write_text(
                _render_parameters_md_from_manifest(manifest, verbatim_text),
                encoding="utf-8",
            )
            demo_gate = _run_demo_smoke_gate(
                script_path,
                staging_root / "_demo_smoke_gate_output_abstraction_fallback",
                require_sandbox=True,
                input_file=source_bundle.input_file,
            )
        if demo_gate.verdict == "rejected" and lift_result is not None and lift_result.lifted:
            # P2a: the literal-lift pass can silently break otherwise-working
            # code (acquisition-plan.md §3 point 5) — fall back to the
            # untouched verbatim body and re-gate it once before giving up.
            # parameters.md is regenerated because it's derived from the
            # script's real argparse surface; leaving it stale would
            # advertise flags the shipped (verbatim) script doesn't have.
            verbatim_text = render_promoted_skill_script(
                skill_name=resolved_skill_name,
                domain=domain,
                summary=summary or source_bundle.goal,
                source_bundle=source_bundle,
                body_code=normalized_code,
                lifted_params=[],
            )
            script_path.write_text(verbatim_text, encoding="utf-8")
            script_text = verbatim_text
            (references_dir / "parameters.md").write_text(
                _render_parameters_md_from_manifest(manifest, verbatim_text), encoding="utf-8"
            )
            lift_fallback_reason = demo_gate.reason
            demo_gate = _run_demo_smoke_gate(
                script_path,
                staging_root / "_demo_smoke_gate_output_fallback",
                require_sandbox=True,
                input_file=source_bundle.input_file,
            )
            lift_result = LiftResult(code=normalized_code, skipped=[f"fell back to verbatim: {lift_fallback_reason}"])
        if demo_gate.verdict == "rejected":
            raise RuntimeError(f"Skill scaffold failed the --demo smoke gate: {demo_gate.reason}")

        if abstraction is not None:
            (references_dir / "acquisition_abstraction.json").write_text(
                json.dumps(
                    abstraction.to_dict(
                        applied=abstraction_applied,
                        fallback_reason=abstraction_fallback_reason,
                    ),
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            relative_created_paths.append(
                Path("references") / "acquisition_abstraction.json"
            )

        if source_bundle is not None and source_bundle.engine == "mini_agent" and lift_result is not None:
            (references_dir / "parameter_lift.md").write_text(
                _render_parameter_lift_evidence(lift_result, fallback_reason=lift_fallback_reason),
                encoding="utf-8",
            )
            relative_created_paths.append(Path("references") / "parameter_lift.md")

        if demo_gate.verdict == "earned":
            from .schema import Lifecycle, Validation

            evidence_path = references_dir / "validation.md"
            evidence_path.write_text(
                _render_validation_evidence(script_name, demo_gate, lift_result), encoding="utf-8"
            )
            relative_created_paths.append(Path("references") / "validation.md")
            manifest.validation = Validation(
                level="demo-validated", evidence=["references/validation.md"]
            )
            # A real implementation that passed its source-appropriate gate is
            # eligible for normal routing. Placeholders never reach ``earned``
            # because their result status is the explicit scaffold sentinel.
            manifest.lifecycle = Lifecycle(status="mvp")
            (skill_dir / "skill.yaml").write_text(manifest.to_yaml(), encoding="utf-8")

        if source_bundle is not None and demo_gate.verdict == "skipped":
            quarantined = True
            committed_skill_dir = (
                resolved_root / QUARANTINE_DIRNAME / domain / resolved_skill_name
            )
            if committed_skill_dir.exists():
                raise FileExistsError(
                    f"Quarantined skill directory already exists: {committed_skill_dir}"
                )
            committed_skill_dir.parent.mkdir(parents=True, exist_ok=True)
            quarantine_evidence = references_dir / "quarantine.md"
            quarantine_evidence.write_text(
                _render_quarantine_evidence(script_name, demo_gate), encoding="utf-8"
            )
            relative_created_paths.append(Path("references") / "quarantine.md")
            quarantine_reason_path = str(
                committed_skill_dir / "references" / "quarantine.md"
            )

        shutil.move(str(skill_dir), str(committed_skill_dir))

    created_files = [str(committed_skill_dir / rel_path) for rel_path in relative_created_paths]
    manifest_path = committed_skill_dir / "manifest.json"
    completion_report_path = committed_skill_dir / COMPLETION_REPORT_FILENAME

    refreshed = False
    if not quarantined and resolved_root.resolve() == SKILLS_DIR.resolve():
        refreshed = refresh_registry()

    return SkillScaffoldResult(
        skill_name=resolved_skill_name,
        domain=domain,
        skill_dir=str(committed_skill_dir),
        script_path=str(committed_skill_dir / script_name),
        skill_md_path=str(committed_skill_dir / "SKILL.md"),
        test_path=str(committed_skill_dir / "tests" / f"test_{script_name}" if create_tests else ""),
        spec_path=str(committed_skill_dir / "scaffold_spec.json"),
        manifest_path=str(manifest_path),
        completion_report_path=str(completion_report_path),
        completion=completion_report.to_dict(),
        created_files=created_files,
        registry_refreshed=refreshed,
        demo_gate_verdict=demo_gate.verdict,
        demo_gate_reason=demo_gate.reason,
        quarantined=quarantined,
        quarantine_reason_path=quarantine_reason_path,
    )


__all__ = [
    "AcquisitionAbstraction",
    "AcquisitionCall",
    "AcquisitionParameter",
    "AutonomousAnalysisBundle",
    "CorpusDerivedBundle",
    "CorpusParamCandidate",
    "SKILL_TEMPLATE_PATH",
    "SKILLS_DIR",
    "QUARANTINE_DIRNAME",
    "VALID_DOMAINS",
    "SkillScaffoldResult",
    "build_acquisition_abstraction",
    "create_skill_scaffold",
    "find_latest_autonomous_analysis",
    "infer_skill_name",
    "refresh_registry",
    "render_structured_promoted_skill_script",
    "slugify_skill_name",
]
