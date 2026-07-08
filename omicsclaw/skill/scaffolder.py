"""Helpers for creating OmicsClaw-native skill scaffolds."""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, field
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
    promotion_note = (
        f"Promoted from a successful autonomous analysis at `{source_bundle.source_dir}`."
        if source_bundle
        else "Generated by `omics-skill-builder` from `templates/skill/`."
    )
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


def build_scaffold_manifest(
    *,
    skill_name: str,
    domain: str,
    trigger_keywords: Iterable[str],
    source_bundle: AutonomousAnalysisBundle | None = None,
    deps_python: Iterable[str] | None = None,
):
    """Build a minimal valid v2 ``SkillManifest`` (ADR 0037) for a scaffold.

    A freshly-scaffolded skill is born v2: the machine contract is the
    ``SkillManifest`` below, serialized to ``skill.yaml``.  ``load_when`` /
    ``skip_when`` mirror the scaffold's v2 description (`_render_v2_description`).
    ``allowed_extra_flags`` is left empty: the runtime derives the accepted
    flags from the script's argparse surface (ADR 0041), so a scaffold need not
    mirror its own ``--method`` / ``--species``.  ``deps.python`` is ``deps_python`` when given
    (the promotion path seeds it from the rendered script's real import surface
    so ``audit_skill_requires`` starts clean) else empty (the default
    placeholder script imports only stdlib plus ``omicsclaw.common.report``,
    which ``_infer_python_deps`` excludes, so ``deps.python`` stays empty).
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
            load_when=(
                f"the user explicitly asks to create a new {domain} skill "
                f"named '{skill_name}'"
            ),
            skip_when=[
                SkipRule(condition=f"an existing {domain} skill already covers the request")
            ],
            trigger_keywords=keywords,
            tags=[domain, "autogenerated", "skill-scaffold"],
            aliases=[],
        ),
        interface=Interface(
            inputs=Inputs(),
            parameters=Parameters(hints={}),
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
        provenance=Provenance(origin="promoted" if source_bundle else "scaffolded"),
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


def render_promoted_skill_script(
    *,
    skill_name: str,
    domain: str,
    summary: str,
    source_bundle: AutonomousAnalysisBundle,
) -> str:
    title = _display_title(skill_name)
    goal = source_bundle.goal or summary
    web_context = source_bundle.web_sources or ""
    analysis_context = source_bundle.context or ""
    default_input = source_bundle.input_file or ""
    requires_input = bool(default_input)
    normalized_code = _normalize_promoted_code(source_bundle.python_code, source_bundle.source_dir)
    indented_code = textwrap.indent(normalized_code.rstrip() + "\n", "    ")
    facade_bootstrap = ""
    if source_bundle.engine == "mini_agent":
        facade_bootstrap = textwrap.indent(_MINI_AGENT_FACADE_BOOTSTRAP, "    ") + "\n"

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=SUMMARY)
    parser.add_argument("--input", dest="input_path", help="Path to input data")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--demo", action="store_true", help="Reuse the original autonomous-analysis input when available")
    parser.add_argument("--method", default="", help="Optional method backend name")
    parser.add_argument("--species", default="", help="Optional species label")
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
      (missing dependency/input — MF3/MF6). The skill still enters the
      catalog at its current validation level.
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
# 'x'") that mean "this environment/input limitation is outside the gate's
# control", not "the promoted code is broken" (MF3/MF6 in the P0/P1 plan):
#   - ModuleNotFoundError/ImportError: the raw staged subprocess never reaches
#     `resolve_skill_runtime`'s adaptive-env provisioning (MF3), so a promoted
#     skill needing a heavy optional dependency is expected to fail here.
#   - FileNotFoundError: a promoted skill's original demo input can go stale
#     between the source run and promotion (its autonomous-analysis workspace
#     may already be cleaned up) — MF6 skips on missing input, not just the
#     empty-input case our own template's SystemExit guard below catches.
# A start-of-line anchor (not a bare substring) is deliberate: a genuine bug
# whose OWN message happens to mention "ImportError" (e.g. a RuntimeError
# with that word in its text) must not be misclassified as an environment
# limitation — that would let broken promoted code slip into the catalog.
_DEMO_GATE_SKIP_EXCEPTION_TYPES = re.compile(
    r"^(?:ModuleNotFoundError|ImportError|FileNotFoundError):", re.MULTILINE
)
# The exact SystemExit message our own promoted-script template raises when
# no demo input is available at all (render_promoted_skill_script) — a plain
# substring is fine here since this is a long, specific, first-party string,
# not a generic exception type name a message could coincidentally contain.
_DEMO_GATE_SKIP_MESSAGE = "Provide --input, or use --demo to reuse the original autonomous-analysis input."


def _demo_gate_skip_reason(combined_output: str) -> str | None:
    """Classify a nonzero --demo exit as an environment limitation, if any."""
    match = _DEMO_GATE_SKIP_EXCEPTION_TYPES.search(combined_output)
    if match:
        return f"environment/input limitation ({match.group(0)[:-1]})"
    if _DEMO_GATE_SKIP_MESSAGE in combined_output:
        return "no demo input available"
    return None


def _run_demo_smoke_gate(script_path: Path, output_dir: Path) -> _DemoGateOutcome:
    """Run ``script_path --demo`` once in ``output_dir`` and classify the result.

    This is demo *validation*, not a sandbox (MF4): it runs in the base
    interpreter with only ``PYTHONPATH``/``PYTHONNOUSERSITE`` set, mirroring
    ``runner.py``'s subprocess env — no OS-level isolation, and no
    adaptive-env provisioning (a raw staged subprocess never reaches
    ``resolve_skill_runtime`` — MF3). Sandboxing model-authored promoted code
    before execution is a separate, not-yet-built P2 concern.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = str(OMICSCLAW_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("PYTHONNOUSERSITE", "1")

    try:
        proc = subprocess.run(
            [sys.executable, str(script_path), "--demo", "--output", str(output_dir)],
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
        skip_reason = _demo_gate_skip_reason(combined)
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


def _render_validation_evidence(script_name: str, gate: _DemoGateOutcome) -> str:
    """Durable record of the P1 --demo smoke gate credit.

    SF1: the staging tmp dir this ran in is rmtree'd on ``create_skill_scaffold``
    exit, so the evidence a ``demo-validated`` skill.yaml points to must live
    here — a persisted file — rather than referencing the ephemeral tmp path.
    """
    envelope = gate.envelope or {}
    summary_json = json.dumps(envelope.get("summary", {}), indent=2, ensure_ascii=False)
    status = envelope.get("status", "")
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
"""


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
) -> SkillScaffoldResult:
    hook_runtime = build_default_lifecycle_hook_runtime(OMICSCLAW_DIR)
    source_bundle: AutonomousAnalysisBundle | None = None
    resolved_source_dir: Path | None = None
    if source_analysis_dir:
        resolved_source_dir = Path(source_analysis_dir)
        if not resolved_source_dir.is_absolute():
            resolved_source_dir = (OMICSCLAW_DIR / resolved_source_dir).resolve()
    elif promote_from_latest:
        resolved_source_dir = find_latest_autonomous_analysis(output_root=output_root)
        if resolved_source_dir is None:
            raise FileNotFoundError("No autonomous analysis output was found to promote.")

    if resolved_source_dir is not None:
        source_bundle = _load_autonomous_bundle(resolved_source_dir)

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
    }
    manifest_metadata = {
        "domain": domain,
        "skill_name": resolved_skill_name,
        "request": request,
        "promoted_from_autonomous_analysis": bool(source_bundle),
        "source_analysis_dir": str(resolved_source_dir) if resolved_source_dir else "",
    }
    relative_created_paths: list[Path] = []
    manifest_path = final_skill_dir / "manifest.json"
    completion_report_path = final_skill_dir / COMPLETION_REPORT_FILENAME

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
        if source_bundle is not None:
            script_text = render_promoted_skill_script(
                skill_name=resolved_skill_name,
                domain=domain,
                summary=summary or source_bundle.goal,
                source_bundle=source_bundle,
            )
            deps_python = _scan_third_party_imports(script_text)
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
            deps_python=deps_python,
        )
        (skill_dir / "skill.yaml").write_text(manifest.to_yaml(), encoding="utf-8")
        relative_created_paths.append(Path("skill.yaml"))

        narrative_md = render_skill_markdown(
            skill_name=resolved_skill_name,
            domain=domain,
            summary=summary or (source_bundle.goal if source_bundle else ""),
            request=request or (source_bundle.goal if source_bundle else ""),
            methods=methods or [],
            input_formats=input_formats or [],
            primary_outputs=primary_outputs or [],
            trigger_keywords=trigger_keywords or [],
            source_bundle=source_bundle,
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
        demo_gate = _run_demo_smoke_gate(script_path, staging_root / "_demo_smoke_gate_output")
        if demo_gate.verdict == "rejected":
            raise RuntimeError(f"Skill scaffold failed the --demo smoke gate: {demo_gate.reason}")
        if demo_gate.verdict == "earned":
            from .schema import Validation

            evidence_path = references_dir / "validation.md"
            evidence_path.write_text(
                _render_validation_evidence(script_name, demo_gate), encoding="utf-8"
            )
            relative_created_paths.append(Path("references") / "validation.md")
            manifest.validation = Validation(
                level="demo-validated", evidence=["references/validation.md"]
            )
            (skill_dir / "skill.yaml").write_text(manifest.to_yaml(), encoding="utf-8")

        shutil.move(str(skill_dir), str(final_skill_dir))

    created_files = [str(final_skill_dir / rel_path) for rel_path in relative_created_paths]

    refreshed = False
    if resolved_root.resolve() == SKILLS_DIR.resolve():
        refreshed = refresh_registry()

    return SkillScaffoldResult(
        skill_name=resolved_skill_name,
        domain=domain,
        skill_dir=str(final_skill_dir),
        script_path=str(final_skill_dir / script_name),
        skill_md_path=str(final_skill_dir / "SKILL.md"),
        test_path=str(final_skill_dir / "tests" / f"test_{script_name}" if create_tests else ""),
        spec_path=str(final_skill_dir / "scaffold_spec.json"),
        manifest_path=str(manifest_path),
        completion_report_path=str(completion_report_path),
        completion=completion_report.to_dict(),
        created_files=created_files,
        registry_refreshed=refreshed,
        demo_gate_verdict=demo_gate.verdict,
        demo_gate_reason=demo_gate.reason,
    )


__all__ = [
    "AutonomousAnalysisBundle",
    "SKILL_TEMPLATE_PATH",
    "SKILLS_DIR",
    "VALID_DOMAINS",
    "SkillScaffoldResult",
    "create_skill_scaffold",
    "find_latest_autonomous_analysis",
    "infer_skill_name",
    "refresh_registry",
    "slugify_skill_name",
]
