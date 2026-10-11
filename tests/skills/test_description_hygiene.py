"""Hygiene of the description field the model routes on.

A ``SKILL.md`` description is the highest-weight *retrieval* text in the
catalogue — with ``skills_index=full`` it is the line the model reads to
pick a skill, and with the per-turn discovery block it is the corpus the
lexical ranker scores. A description that is too long costs tokens
without evidence, one that points at a skill that does not exist sends
the model after a name ``use_skill`` will reject, and one that advertises
a package nobody can install turns "just run this skill" into a support
ticket. These three checks exist because each failure mode is a
*drift*: the file that introduces it passes every other test in this
directory, since none of them read the corpus as promises.

**Length.** P2-A tightened every description to 300 characters or fewer;
300 is the target the discovery block truncates served lines at. The
lint pins the 350-character hard cap instead, so a future skill has
headroom to say what it must without flirting with the ceiling the
design review agreed on.

**Dangling references.** The corpus's convention is that a description
ends with "Skip when … (use <skill>)" exclusions, and each of those
names must resolve in the index — the model is told to load it by that
name, so a misspelling or a deleted sibling fails at load time, not at
routing time. A small explicit set of verbs ("the", "fetch",
"external", "upstream") is excluded because those phrases point at
things other than skills: "use the relevant downstream skill", "run an
external caller", "use fetch_mapping explicitly". Everything else must
name a skill.

**Advertised packages.** A description that says "via scVelo" promises
scVelo. The environment system can only install what
``skills/_sdk/deps.py`` registers, so the cross-check extracts
package-shaped candidates from every description and asks each one to
resolve against the registry — keys, ``module`` values and ``also``
lists — or against an explicit allowlist of tool names that resolve
somewhere else: the conda CLI tier, the R dependency tiers, names that
ship inside a registered package, and upstream tools whose output the
skill merely consumes. The extraction is deliberately conservative;
a looser one would keep finding names in ordinary prose. Candidates
are (a) the identifier directly after an install-ish verb
("pip install / import / via / with / using / calls / uses"), and
(b) camelCase tokens — mixed-case words are how the corpus spells brand
names ("CellTypist", "scVelo") and how it does not spell prose. Both
are filtered through a small vocabulary of ordinary technical words
(the corpus's way of saying "dataset", "method", "statistic"), because
a false positive here is worse than a miss: this test fails only when a
description genuinely promises a name that no channel can deliver.

The registry is read as text and ``ast.literal_eval``'d, never
imported — importing it pulls its dependencies into the test process,
which is exactly what a registry reader should not do.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from omicsclaw.skills import SkillIndex, load_skills

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SKILLS_ROOT = REPO_ROOT / "skills"
DEPS_PATH = SKILLS_ROOT / "_sdk" / "deps.py"

HARD_LIMIT = 350
"""The character ceiling a description may not cross.

300 is the retrieval target (and the width the discovery block serves);
350 is the cap the design review fixed. The lint enforces the cap so a
future description can use the headroom without being able to sprawl.
"""

pytestmark = pytest.mark.skipif(
    not SKILLS_ROOT.is_dir(), reason="the skills corpus is not present"
)

requires_deps = pytest.mark.skipif(
    not DEPS_PATH.is_file(), reason="the dependency registry is not present"
)


@pytest.fixture(scope="module")
def corpus() -> SkillIndex:
    return load_skills(SKILLS_ROOT)


# ---------------------------------------------------------------------------
# length ---------------------------------------------------------------------


def test_every_description_fits_the_hard_limit(corpus: SkillIndex):
    """The route text stays inside the ceiling the design review agreed."""
    too_long = [
        (skill.name, len(skill.description))
        for skill in corpus.skills
        if len(skill.description) > HARD_LIMIT
    ]

    assert too_long == [], (
        f"descriptions over {HARD_LIMIT} characters: {too_long}; tighten them "
        "(target 300) without touching the body"
    )


# ---------------------------------------------------------------------------
# dangling "use <skill>" references -------------------------------------------


_USE_REFERENCE = re.compile(r"use ([a-z0-9-]+)")

_USE_VERBS = frozenset({"the", "fetch", "external", "upstream"})
"""Words after "use" that are not skill names.

"the" precedes "the relevant downstream skill", "fetch" comes from
"use fetch_mapping explicitly", and "external"/"upstream" name things
deliberately out of reach: "use external mummichog / FELLA", "run an
external caller". A real skill reference is a lowercase-hyphenated
name; these four are the corpus's documented exceptions.
"""


def test_every_use_reference_names_a_skill(corpus: SkillIndex):
    """A description may only hand the model names the index can load."""
    names = set(corpus.names())
    dangling = sorted(
        {
            (skill.name, reference)
            for skill in corpus.skills
            for reference in _USE_REFERENCE.findall(skill.description)
            if reference not in _USE_VERBS and reference not in names
        }
    )

    assert dangling == [], (
        f'"use X" references that name no skill: {dangling}; '
        f"the lint regex is {_USE_REFERENCE.pattern!r} with the verb exclusions "
        f"{sorted(_USE_VERBS)}"
    )


# ---------------------------------------------------------------------------
# advertised packages ---------------------------------------------------------


_APPROACH_SUFFIXES = frozenset(
    {
        "style", "simple", "based", "derived", "ready", "level", "adjusted",
        "field", "backed",
    }
)
"""Suffixes that mark an algorithm description, not a package name.

"Signac-style", "CytoTRACE-simple", "CBS-style", "GRN-based" and their
kin describe how a skill computes something in-house; the brand they
carry is an algorithmic reference. Extracting those brands as packages
would fail the check on a name nobody is promised.

"""

_VERB_LEAD = re.compile(
    r"(?:pip install|import|via|with|using|calls|uses)\s+([A-Za-z][A-Za-z0-9.]*)"
)
"""The identifier that follows an install-ish verb.

The character class takes dots too, so "``scipy.signal.find_peaks``"
and sentence-final "bcftools." arrive whole; the first dot-separated
component is the package name.

"""

_CAMEL_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z][A-Za-z0-9]*)*")

_ORDINARY = frozenset(
    {
        # sentence openers every description uses
        "load", "skip",
        # the corpus's ordinary mixed-case vocabulary: data containers,
        # formats, platforms, methods, statistics — words, not packages
        "a", "already", "an", "atac-seq", "bams", "bh", "biological",
        "builtin", "cell", "chip-seq", "combat", "cox", "crispr-screen",
        "default", "deterministic", "dpt", "drug", "ensembl", "entrez",
        "fastqs", "fisher", "gene", "genome-dna", "genetic-ko", "group",
        "grnboost2", "grns", "ids", "ibaq", "itraq", "kaplan",
        "kaplan-meier", "kmeans", "kruskal-wallis", "leiden", "lfq",
        "log2fc", "louvain", "low-d", "mann-whitney", "marker", "median",
        "monte-carlo", "moran", "mygene", "mz", "mzml", "narrowpeak",
        "omicsclaw", "olsen", "over", "pca", "pearson", "per-impact",
        "per-ptm-type", "perturb-seq", "phred", "prominence", "proteingroups",
        "pubmed", "python", "qc", "r", "ranger", "raw", "replicates",
        "ripley", "rmats", "rna-seq", "sample", "scrna", "scrna-reference",
        "scrna-seq", "sgrna", "single", "small", "spatial", "spliced",
        "ssgsea", "svs", "snvs", "t-sne", "tag", "the", "ti", "tv",
        "vcfs", "visium", "welch", "wilcoxon", "xenium", "cistarget",
    }
)
"""Ordinary technical vocabulary the extraction is not allowed to flag.

The camelCase rule alone would read "Leiden", "Wilcoxon", "AnnData" or
"RNA-seq" as brands. This list is the corpus's own vocabulary for
methods, statistics, formats and platforms — deliberately kept in one
place and alphabetised, because every entry is a word a real request
might use and none of them is a package the environment must deliver.
Words here are exempt from the registry check, not resolved by it.

"""

_ALLOWLIST: dict[str, str] = {
    # --- the core scientific stack, installed with the package ---------------
    "anndata": "core data container; environment.yml Tier 3 installs it with the package",
    "scanpy": "core single-cell library; environment.yml Tier 3 installs it with the package",
    "scipy": "core scientific library; environment.yml Tier 3 installs it with the package",
    # --- conda CLI tools (environment.yml Tier 1, pip cannot ship them) ------
    "samtools": "CLI installed from conda (environment.yml Tier 1); genomics-alignment shells out to it",
    "bcftools": "CLI installed from conda (environment.yml Tier 1); genomics-vcf-operations shells out to it",
    # --- R packages behind the advertised R-backed methods -------------------
    # (declared in skills/_sdk/r_dependency_manager.py R_TIER_PACKAGES)
    "seurat": "R package in the singlecell-preprocessing tier (r_dependency_manager)",
    "sctransform": "R package behind the SCTransform path, shipped with the Seurat stack in that tier",
    "soupx": "R package in the singlecell-ambient tier (r_dependency_manager)",
    "doubletfinder": "R package in the singlecell-doublet-detection tier (r_dependency_manager)",
    "scdblfinder": "R package in the singlecell-doublet-detection tier (r_dependency_manager)",
    "nichenet": "the nichenetr R package in the singlecell-communication tier; NicheNet also needs user-supplied .rds files",
    "slingshot": "R package in the singlecell-pseudotime tier (r_dependency_manager)",
    "monocle3": "R backend with an explicit runtime package check; user-installed, documented in sc-pseudotime",
    "scmap": "R package in the singlecell-cell-annotation tier (r_dependency_manager)",
    "aucell": "R package in the singlecell-pathway-scoring tier; the Python side is implemented in-house",
    # --- names that resolve inside a registered entry ------------------------
    "harmony": "method name of the harmonypy package (registry key harmonypy)",
    "scanvi": "scANVI ships inside scvi-tools (registry key scvi-tools, module scvi)",
    "destvi": "DestVI ships inside scvi-tools (registry key scvi-tools, module scvi)",
    "stagate": "installed from the git registry entry STAGATE-pyG (module STAGATE_pyG)",
    "milo": "run through the pertpy backend (registry key pertpy), not a standalone install",
    "mixscape": "the pertpy Mixscape workflow (registry key pertpy)",
    # --- methods the skill implements in-house, with no install at all -------
    "knnpredict": "KNNPredict-style nearest-neighbour transfer implemented in-house; no install",
    "scsa": "SCSA Fisher-test scoring implemented in-house against the CellMarker DB; no install",
    "cellassign": "CellAssign scoring implemented in-house (skills/spatial/_lib/annotation.py); no install",
    "dpt": "diffusion pseudotime, scanpy's in-house method; no install",
    # --- user-supplied, and documented as such -------------------------------
    "cadrres-sc": "runs user-supplied local CaDRReS-Sc models; documented as never auto-installed",
    "sctenifoldknk": "R package the wrapper expects the user to provide; documented as not installed by the skill",
    "st-pipeline": "external pipeline; the skill shells out to a user-provided stpipeline binary",
    # --- upstream tools whose output the skills consume ----------------------
    # The description names them as input producers ("summarise the output of
    # X"), and the skill never installs or runs them.
    "cellranger": "upstream count-matrix producer; its output is consumed, never installed",
    "starsolo": "upstream count-matrix producer; its output is consumed, never installed",
    "simpleaf": "upstream count-matrix producer; its output is consumed, never installed",
    "bowtie2": "upstream aligner; its SAM output is consumed, never installed",
    "minimap2": "upstream aligner; its SAM output is consumed, never installed",
    "salmon": "upstream quantifier; its log output is consumed, never installed",
    "delly": "upstream SV caller; its VCF output is consumed, never installed",
    "manta": "upstream SV caller; its VCF output is consumed, never installed",
    "sniffles": "upstream SV caller; its VCF output is consumed, never installed",
    "eagle2": "upstream phaser; its VCF output is consumed, never installed",
    "whatshap": "upstream phaser; its VCF output is consumed, never installed",
    "genrich": "upstream peak caller, run externally per genomics-epigenomics",
    "canu": "upstream assembler; its FASTA output is consumed, never installed",
    "flye": "upstream assembler; its FASTA output is consumed, never installed",
    "megahit": "upstream assembler; its FASTA output is consumed, never installed",
    "spades": "upstream assembler; its FASTA output is consumed, never installed",
    "fragpipe": "upstream search engine; its tables are consumed, never installed",
    "maxquant": "upstream search engine; its tables are consumed, never installed",
    "xlinkx": "external XL-MS search engine run upstream; its output is consumed",
    "plink": "external XL-MS search engine run upstream; its output is consumed",
    "xisearch": "external XL-MS search engine run upstream; its output is consumed",
}


def _registry_names() -> set[str]:
    """The names ``deps.py`` can deliver: keys, modules and ``also`` lists.

    The file documents itself as a pure-literal registry readable with
    ``ast.literal_eval``; importing it would pull its dependency-probing
    machinery into this process, so the text is parsed instead.
    """
    source = DEPS_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "DEPENDENCIES"
        ):
            registry = ast.literal_eval(node.value)
            break
    else:
        raise AssertionError(f"{DEPS_PATH.name} no longer defines DEPENDENCIES as a literal")

    names: set[str] = set()
    for key, entry in registry.items():
        names.add(key.lower())
        if isinstance(entry, dict):
            module = entry.get("module")
            if module:
                names.add(str(module).lower())
            for extra in entry.get("also") or ():
                names.add(str(extra).lower())
    return names


def _candidates(description: str) -> set[str]:
    """Package-shaped tokens in *description*, lowercased.

    Source (a): the identifier after an install-ish verb, minus any
    ``-style``-family suffix that marks an in-house algorithm. The first
    dot-separated component is the name — ``scipy.signal.find_peaks``
    advertises scipy, and a sentence-final "bcftools." is bcftools.

    Source (b): camelCase tokens — the corpus spells brand names with
    mixed case and prose without it, so a mixed-case word is evidence a
    name is being dropped. All-caps tokens (PCA, GATK, LIANA) are left
    alone: they are overwhelmingly acronyms of methods, not packages.
    """
    found: set[str] = set()

    for match in _VERB_LEAD.finditer(description):
        token = match.group(1).split(".")[0]
        suffix = re.match(r"-([a-z]+)\b", description[match.end() :])
        if suffix and suffix.group(1) in _APPROACH_SUFFIXES:
            continue
        found.add(token)

    for token in _CAMEL_TOKEN.findall(description):
        if not (re.search(r"[a-z]", token) and re.search(r"[A-Z]", token)):
            continue
        if token.rsplit("-", 1)[-1] in _APPROACH_SUFFIXES:
            continue
        found.add(token)

    return {token.lower() for token in found}


@requires_deps
def test_every_advertised_package_resolves(corpus: SkillIndex):
    """A description may only promise a package some channel can deliver."""
    known = _registry_names()
    offenders = sorted(
        {
            (skill.name, candidate)
            for skill in corpus.skills
            for candidate in _candidates(skill.description)
            if candidate not in _ORDINARY
            and candidate not in known
            and candidate not in _ALLOWLIST
        }
    )

    assert offenders == [], (
        f"descriptions advertise names no channel can deliver: {offenders}. "
        f"Register the package in {DEPS_PATH.relative_to(REPO_ROOT)} (key, "
        f"module or also), add it to _ALLOWLIST with a reason, or reword the "
        f"description to stop promising it."
    )
