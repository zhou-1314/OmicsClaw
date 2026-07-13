"""Unified capability resolution for OmicsClaw chat and automation flows.

Determines whether a user request is:
- fully covered by an existing skill
- partially covered and needs custom post-processing
- not covered and should fall back to web-guided custom analysis

Scoring weights and decision thresholds are kept as module-level named
constants (see ``_SCORE_*`` / ``_DOMAIN_SCORE_*`` / ``_RESOLVE_*`` below).
Previous revisions buried those magic numbers inside arithmetic in
``_candidate_score`` and ``resolve_capability`` — OMI-12 P2.8 lifted them
out so future weight tuning is reviewable diff-by-diff, and the matching
golden routing snapshot (``tests/test_capability_resolver_golden.py``) flags
any silent re-ranking that slips through.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
from typing import Any

from .preconditions import (
    InputProfile,
    evaluate_skill_preconditions,
    probe_input_profile,
)
from .registry import OmicsRegistry, ensure_registry_loaded

try:
    from omicsclaw.loaders import detect_domain_from_path
except Exception:  # pragma: no cover - fallback for partial installs
    detect_domain_from_path = None


# ---------------------------------------------------------------------------
# Scoring weights (lifted from inline magic numbers — OMI-12 P2.8).
#
# Each constant documents what its signal source is and why the weight has
# the value it does. Changing any of these will likely re-rank some queries;
# the golden routing test snapshots ``chosen_skill`` for ~20 representative
# queries so re-rankings surface as a single, reviewable test diff.
# ---------------------------------------------------------------------------

# ----- _candidate_score: per-skill scoring -----

# A direct mention of a skill's canonical alias in the query is the strongest
# signal we have — the user named the skill they want. Outweighs every other
# signal so a single alias hit can dominate even a long description overlap.
_SCORE_ALIAS_MENTION = 12.0

# Pre-rename / shorthand aliases (SKILL.md ``legacy_aliases``) score slightly
# lower than the canonical alias so canonical wins on ties.
_SCORE_LEGACY_ALIAS_MENTION = 9.0

# Each shared token between the query and the skill's SKILL.md description
# adds a small bonus. Capped so a very long description can't dominate
# alias mentions or trigger keywords.
_SCORE_DESCRIPTION_OVERLAP_PER_TOKEN = 0.85
_SCORE_DESCRIPTION_OVERLAP_CAP = 8  # max overlap tokens counted

# Trigger keywords (SKILL.md ``trigger_keywords``) get a length-weighted bonus
# so a multi-word phrase ("differential expression") is worth more than a
# generic single word ("run"). Bounded to keep the keyword signal in the
# same order of magnitude as the alias signal.
_SCORE_TRIGGER_KEYWORD_MIN = 1.5
_SCORE_TRIGGER_KEYWORD_MAX = 4.5
_SCORE_TRIGGER_KEYWORD_LENGTH_DIVISOR = 6.0
_SCORE_TRIGGER_KEYWORD_LIMIT = 3  # max distinct keywords counted per skill

# Param-hint match: the user named a specific method (``--method leiden``)
# and that method appears as a parameter hint for the skill's SKILL.md
# methodology block.
_SCORE_PARAM_HINT_MATCH = 3.0

# Explicitly naming the leaf task type (the final alias component, e.g.
# ``preprocessing`` in ``sc-preprocessing``) is a soft but meaningful signal.
# This lets "scRNA-seq preprocessing ... PCA/UMAP/Leiden" select the requested
# preprocessing workflow instead of over-weighting downstream method tokens.
_SCORE_TASK_TYPE_EXPLICIT = 8.0

# Within the single-cell domain, the registry deliberately separates scRNA
# skills (``sc-*``) from scATAC skills (``scatac-*``).  Preserve that ontology
# in scoring so a shared stage word such as "preprocessing" cannot route an
# explicitly-scRNA request to the ATAC implementation, or vice versa.
_SCORE_MODALITY_MATCH = 5.0
_SCORE_MODALITY_CONFLICT = 5.0
_SINGLECELL_MODALITY_PHRASES: dict[str, tuple[str, ...]] = {
    "scrna": ("scrna", "single cell rna", "single-cell rna"),
    "scatac": ("scatac", "single cell atac", "single-cell atac"),
}


# ----- _detect_domain: per-domain scoring -----

# File path with a known extension is the strongest domain signal.
_DOMAIN_SCORE_FILE_PATH = 5.0
# Per-domain mirrors of the per-skill scores, intentionally weaker so the
# domain detector is more forgiving than the skill picker.
_DOMAIN_SCORE_ALIAS_MENTION = 8.0
_DOMAIN_SCORE_LEGACY_ALIAS_MENTION = 3.0
_DOMAIN_SCORE_DESCRIPTION_OVERLAP_PER_TOKEN = 0.6
_DOMAIN_SCORE_DESCRIPTION_OVERLAP_CAP = 5
_DOMAIN_SCORE_TRIGGER_KEYWORD_LIMIT = 3
# The user typed the domain name verbatim ("bulk RNA-seq", "spatial").
_DOMAIN_SCORE_DOMAIN_NAME_MATCH = 10.0

# Human-facing names are not always the registry keys (``singlecell`` vs
# "single-cell", ``bulkrna`` vs "bulk RNA-seq").  Explicit domain wording is
# a strong scoping instruction and must beat generic cross-domain terms such
# as "cluster", "integrate", or "annotation".
_DOMAIN_QUERY_ALIASES: dict[str, tuple[str, ...]] = {
    "spatial": ("spatial transcriptomics", "spatial omics", "visium"),
    "singlecell": (
        "single cell",
        "single-cell",
        "scrna",
        "scRNA-seq",
        "scatac",
        "single cell atac",
        "single-cell atac",
    ),
    "genomics": ("genomics", "genomic"),
    "proteomics": ("proteomics", "proteomic"),
    "metabolomics": ("metabolomics", "metabolomic", "metabolite", "metabolites"),
    "bulkrna": ("bulk rna", "bulk RNA-seq", "bulk rnaseq"),
    "orchestrator": ("orchestrate", "routing", "route this query"),
    "literature": ("literature", "paper", "pubmed", "doi"),
}


# ----- resolve_capability: decision thresholds -----

# Below this top-1 score the resolver returns ``coverage="no_skill"`` instead
# of guessing. Tuned so a single description-token overlap (~0.85) or a
# single short keyword hit (~1.5) is not enough to commit.
_RESOLVE_NO_SKILL_THRESHOLD = 3.0

# If top-1 minus top-2 is smaller than this and the query also has composite
# wording ("and then ...", "再 ..."), the resolver downgrades from
# ``exact_skill`` to ``partial_skill`` rather than blindly picking top-1.
_RESOLVE_CLOSE_SECOND_GAP = 1.5

# Top-1 score divided by this gives the reported confidence ∈ [0, 1].
# Chosen so a single strong alias hit (12) + a few description tokens lands
# near 1.0 without saturating on every clear-intent query.
_RESOLVE_CONFIDENCE_DIVISOR = 14.0

# Same idea but tighter, used only on the ``no_skill`` fallback path so that
# a marginal top score doesn't claim higher confidence than the cap below.
_RESOLVE_NO_SKILL_CONFIDENCE_DIVISOR = 10.0

# Confidence ceiling for the ``no_skill`` fallback path; we never claim
# strong confidence when we're below the no-skill threshold.
_RESOLVE_NO_SKILL_CONFIDENCE_CAP = 0.35

# Only governed, released lifecycle stages participate in automatic routing.
# ``draft`` remains available to explicit developer workflows through the
# registry, while ``deprecated`` remains inspectable in catalogs/audit views.
_ROUTABLE_LIFECYCLE_STATUSES = frozenset({"mvp", "stable"})

# A structured Skip-when redirect is stronger than an alias mention: the user
# may explicitly name a skill and then state the exact condition under which
# that skill says not to use it. Keep the bonus named and regression-tested.
_SCORE_SKIP_WHEN_REDIRECT_BONUS = 2.0
_SCORE_ORCHESTRATOR_INTENT_BONUS = 9.0
_SCORE_SKILL_CREATION_TARGET_BONUS = 12.0
_SKIP_WHEN_TOKEN_COVERAGE = 0.65
_SKIP_WHEN_MIN_SHARED_TOKENS = 3
_NEGATIVE_CONDITION_TOKENS = frozenset(
    {"not", "no", "without", "missing", "lack", "lacks", "failed", "undecided"}
)

# Validation is a bounded tie-break, never a substitute for semantic fit. The
# full ladder spans less than one description-token match (0.85), so a highly
# validated irrelevant skill cannot overtake a genuinely better candidate.
_VALIDATION_SCORE = {
    "smoke-only": 0.0,
    "demo-validated": 0.15,
    "fixture-validated": 0.30,
    "benchmarked": 0.45,
    "production": 0.60,
}


_NON_ANALYSIS_HINTS = (
    "what is omicsclaw",
    "help",
    "usage",
    "install",
    "version",
)

_CUSTOM_FALLBACK_HINTS = (
    "custom",
    "bespoke",
    "from scratch",
    "independent",
    "not in omicsclaw",
    "not available in omicsclaw",
    "outside the skill",
    "post-process",
    "post process",
    "after that",
    "then compute",
    "then generate",
    "extra step",
    "additional step",
    "再做",
    "然后再",
    "额外",
    "自定义",
    "独立生成",
    "skill里没有",
    "skill 里没有",
)

_WEB_HINTS = (
    "latest",
    "recent",
    "newest",
    "up-to-date",
    "documentation",
    "docs",
    "paper",
    "papers",
    "literature",
    "web",
    "internet",
    "联网",
    "最新",
    "文献",
    "论文",
    "官网",
)

_IMPLEMENTATION_FROM_LITERATURE_HINTS = (
    "implement",
    "build",
    "develop",
    "code",
    "from latest literature",
    "from recent literature",
    "from literature",
    "基于最新文献实现",
    "根据最新文献实现",
    "按文献实现",
)


def _trigger_keyword_score(phrase: str) -> float:
    """Length-weighted keyword score, clamped to ``[MIN, MAX]``."""
    return max(
        _SCORE_TRIGGER_KEYWORD_MIN,
        min(_SCORE_TRIGGER_KEYWORD_MAX, len(phrase) / _SCORE_TRIGGER_KEYWORD_LENGTH_DIVISOR),
    )


def _score_trigger_keyword_matches(
    query_lower: str,
    keywords: list[str] | tuple[str, ...],
    *,
    limit: int = _SCORE_TRIGGER_KEYWORD_LIMIT,
) -> tuple[float, list[str]]:
    matches: list[str] = []
    score = 0.0
    for keyword in keywords:
        phrase = str(keyword).strip().lower()
        if not phrase or not _mentions_phrase(query_lower, phrase):
            continue
        matches.append(phrase)
        score += _trigger_keyword_score(phrase)
        if len(matches) >= limit:
            break
    return score, matches


def _requests_new_literature_implementation(query_lower: str) -> bool:
    """Return True for requests to implement new methods from literature."""
    has_implementation = any(
        _mentions_phrase(query_lower, hint)
        for hint in _IMPLEMENTATION_FROM_LITERATURE_HINTS[:4]
    )
    has_literature_source = any(
        _mentions_phrase(query_lower, hint)
        for hint in _IMPLEMENTATION_FROM_LITERATURE_HINTS[4:]
    ) or any(_mentions_phrase(query_lower, hint) for hint in _WEB_HINTS)
    return has_implementation and has_literature_source


_SKILL_CREATION_HINTS = (
    "create skill",
    "create a skill",
    "new skill",
    "add skill",
    "build skill",
    "scaffold skill",
    "skill scaffold",
    "generate skill",
    "package as skill",
    "turn into a skill",
    "reusable skill",
    "integrate into omicsclaw",
    "add to omicsclaw",
    "新增skill",
    "新增 skill",
    "创建skill",
    "创建 skill",
    "新建skill",
    "新建 skill",
    "做成skill",
    "做成 skill",
    "封装成skill",
    "封装成 skill",
    "封装为skill",
    "封装为 skill",
    "沉淀成skill",
    "沉淀成 skill",
    "加入omicsclaw",
    "加入 omicsclaw",
)

_COMPOSITE_HINTS = (
    " and then ",
    " followed by ",
    " combine ",
    " plus ",
    " with an extra ",
    "然后",
    "再",
)

_COMPOSITE_SPLIT_RE = re.compile(
    r"\s+(?:and\s+then|and|followed\s+by|plus|with\s+an\s+extra)\s+|然后|再",
    flags=re.IGNORECASE,
)

_COMPOSITE_EXECUTION_HINTS = (
    "run ",
    "perform ",
    "execute ",
    "apply ",
    "use ",
    "combine ",
    "运行",
    "执行",
)

_COMPOSITE_ADVISORY_RE = re.compile(
    r"^\s*(?:please\s+)?(?:should\b|how\b|explain\b|compare\b|describe\b|tell\s+me\b)",
    flags=re.IGNORECASE,
)

_GENERIC_ANALYSIS_HINTS = (
    "analy",
    "run ",
    "perform ",
    "compute ",
    "microenvironment",
    "neighborhood",
    "微环境",
    "邻域",
    "preprocess",
    "qc",
    "cluster",
    "differential",
    "deconvolution",
    "trajectory",
    "velocity",
    "pathway",
    "enrichment",
    "survival",
    "spatial",
    "single cell",
    "single-cell",
    "proteomics",
    "metabolomics",
    "genomics",
    "bulk rna",
    "空间",
    "单细胞",
    "蛋白",
    "代谢",
    "基因组",
    "差异",
    "富集",
)

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_\-+.]{1,}")
_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "into",
    "then",
    "that",
    "this",
    "using",
    "use",
    "analysis",
    "model",
    "run",
    "perform",
    "skill",
    "skills",
}


@dataclass
class CapabilityCandidate:
    skill: str
    domain: str
    score: float
    reasons: list[str] = field(default_factory=list)
    precondition_status: str = "eligible"
    precondition_evaluated: bool = False
    execution_ready: bool = True
    missing_preconditions: list[str] = field(default_factory=list)
    precondition_reasons: list[str] = field(default_factory=list)
    recommended_preparation: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["score"] = round(float(self.score), 3)
        return data


@dataclass
class CapabilityDecision:
    query: str
    domain: str = ""
    coverage: str = "no_skill"
    confidence: float = 0.0
    chosen_skill: str = ""
    should_search_web: bool = False
    should_create_skill: bool = False
    skill_candidates: list[CapabilityCandidate] = field(default_factory=list)
    missing_capabilities: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    precondition_status: str = "eligible"
    precondition_evaluated: bool = False
    execution_ready: bool = True
    missing_preconditions: list[str] = field(default_factory=list)
    precondition_reasons: list[str] = field(default_factory=list)
    recommended_preparation: list[str] = field(default_factory=list)
    candidate_chain: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "domain": self.domain,
            "coverage": self.coverage,
            "confidence": round(float(self.confidence), 3),
            "chosen_skill": self.chosen_skill,
            "should_search_web": self.should_search_web,
            "should_create_skill": self.should_create_skill,
            "skill_candidates": [c.to_dict() for c in self.skill_candidates],
            "missing_capabilities": list(self.missing_capabilities),
            "reasoning": list(self.reasoning),
            "precondition_status": self.precondition_status,
            "precondition_evaluated": self.precondition_evaluated,
            "execution_ready": self.execution_ready,
            "missing_preconditions": list(self.missing_preconditions),
            "precondition_reasons": list(self.precondition_reasons),
            "recommended_preparation": list(self.recommended_preparation),
            "candidate_chain": dict(self.candidate_chain),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    def to_prompt_block(self) -> str:
        lines = [
            "## Deterministic Capability Assessment",
            f"- coverage: {self.coverage}",
            f"- chosen_skill: {self.chosen_skill or 'none'}",
            f"- domain: {self.domain or 'unknown'}",
            f"- confidence: {round(float(self.confidence), 3)}",
            f"- should_search_web: {self.should_search_web}",
            f"- should_create_skill: {self.should_create_skill}",
            f"- precondition_status: {self.precondition_status}",
            f"- precondition_evaluated: {self.precondition_evaluated}",
            f"- execution_ready: {self.execution_ready}",
        ]
        if self.missing_capabilities:
            lines.append("- missing_capabilities: " + "; ".join(self.missing_capabilities))
        if self.reasoning:
            lines.append("- reasoning:")
            for item in self.reasoning[:4]:
                lines.append(f"  * {item}")
        if self.missing_preconditions:
            lines.append("- missing_preconditions: " + "; ".join(self.missing_preconditions))
        if self.recommended_preparation:
            lines.append(
                "- recommended_preparation: " + "; ".join(self.recommended_preparation)
            )
        if self.skill_candidates:
            preview = ", ".join(
                f"{c.skill} ({round(float(c.score), 2)})"
                for c in self.skill_candidates[:3]
            )
            lines.append(f"- candidate_skills: {preview}")
        if self.candidate_chain:
            if self.candidate_chain.get("validated_order"):
                lines.append(
                    "- candidate_topo_chain: "
                    + " -> ".join(self.candidate_chain.get("skills", []))
                )
            else:
                lines.append(
                    "- unresolved_candidate_intents: "
                    + "; ".join(self.candidate_chain.get("requested_skills", []))
                )
        return "\n".join(lines)


def _tokenize(text: str) -> set[str]:
    return {
        token
        for token in _TOKEN_RE.findall(text.lower())
        if token not in _STOPWORDS
    }


def _mentions_phrase(text: str, phrase: str) -> bool:
    phrase = (phrase or "").strip().lower()
    if not phrase:
        return False
    if " " not in phrase and phrase.replace("-", "").replace("_", "").isalnum():
        plural = "" if phrase.endswith("s") else "s?"
        pattern = rf"(?<![a-z0-9]){re.escape(phrase)}{plural}(?![a-z0-9])"
        return bool(re.search(pattern, text))
    # Treat whitespace, hyphen, and underscore as equivalent word separators
    # for metadata phrases: "cell type" should match "cell-type", and
    # "phosphorylation site" should match "phosphorylation-site".  Keep
    # punctuation-heavy phrases on the literal fallback below.
    if re.fullmatch(r"[a-z0-9 _-]+", phrase):
        words = [word for word in re.split(r"[ _-]+", phrase) if word]
        if words:
            last = re.escape(words[-1]) + ("" if words[-1].endswith("s") else "s?")
            pattern = (
                r"(?<![a-z0-9])"
                + r"[\s_-]+".join(
                    [*(re.escape(word) for word in words[:-1]), last]
                )
                + r"(?![a-z0-9])"
            )
            return bool(re.search(pattern, text))
    return phrase in text


def _looks_like_analysis_request(query: str) -> bool:
    lower = query.lower()
    if any(h in lower for h in _NON_ANALYSIS_HINTS):
        return False
    if any(h in lower for h in _GENERIC_ANALYSIS_HINTS):
        return True
    return bool(re.search(r"\.(h5ad|h5|loom|mzml|fastq|fq|bam|vcf|csv|tsv)\b", lower))


def _method_mentions(query: str) -> set[str]:
    return {
        token
        for token in _tokenize(query)
        if len(token) >= 3 and not token.isdigit()
    }


def _normalise_condition_text(text: str) -> str:
    """Normalise common spelling variants before Skip-when token matching."""
    value = (text or "").lower()
    return (
        value.replace("normalisation", "normalization")
        .replace("normalised", "normalized")
        .replace("normalise", "normalize")
    )


def _matching_skip_rule(info: dict[str, Any], query_lower: str) -> dict[str, str] | None:
    """Return the first structured negative-routing rule supported by the query.

    This deliberately uses condition-token coverage rather than arbitrary
    semantic similarity. A rule only fires when most of its own meaningful
    tokens are present, with a minimum of three shared tokens, which keeps
    generic words in long queries from suppressing unrelated skills.
    """
    normalised_query = _normalise_condition_text(query_lower)
    query_tokens = _tokenize(normalised_query)
    for raw_rule in info.get("skip_when", []):
        if not isinstance(raw_rule, dict):
            continue
        condition = str(raw_rule.get("condition") or "").strip()
        if not condition:
            continue
        normalised_condition = _normalise_condition_text(condition)
        condition_tokens = _tokenize(normalised_condition)
        if not condition_tokens:
            continue
        shared = condition_tokens & query_tokens
        exact = normalised_condition in normalised_query
        # Preserve polarity. Token coverage alone would treat "QC/PCA have
        # already run" as equivalent to "QC/PCA have not run yet" because
        # nearly every content word overlaps. If the rule is explicitly
        # negative, the query must carry a negative marker too.
        condition_negatives = condition_tokens & _NEGATIVE_CONDITION_TOKENS
        query_negatives = query_tokens & _NEGATIVE_CONDITION_TOKENS
        if condition_negatives and not query_negatives:
            continue
        coverage = len(shared) / len(condition_tokens)
        if exact or (
            len(shared) >= _SKIP_WHEN_MIN_SHARED_TOKENS
            and coverage >= _SKIP_WHEN_TOKEN_COVERAGE
        ):
            return {
                "condition": condition,
                "use": str(raw_rule.get("use") or "").strip(),
                "rationale": str(raw_rule.get("rationale") or "").strip(),
            }
    return None


def _requests_skill_creation(query: str) -> bool:
    lower = (query or "").lower()
    if any(hint in lower for hint in _SKILL_CREATION_HINTS):
        return True

    if "skill" in lower and any(
        verb in lower for verb in ("create", "add", "build", "scaffold", "package", "persist")
    ):
        return True

    if "skill" in lower and any(
        verb in lower for verb in ("创建", "新增", "新建", "封装", "沉淀", "加入")
    ):
        return True

    return False


def _requests_orchestration(query: str) -> bool:
    """Detect requests whose task is choosing/routing a skill, not analysis."""
    lower = (query or "").lower()
    which_or_choose_skill = bool(
        re.search(r"\b(?:which|choose)\b.{0,80}\bskills?\b", lower)
    )
    route_request = bool(
        re.search(
            r"\broute\s+(?:(?:this|the|my|a|an)\s+)?(?:query|request)\b",
            lower,
        )
    )
    explicit_orchestration = bool(
        re.search(r"\borchestrat(?:e|es|ed|ing|ion)\b", lower)
        and any(_mentions_phrase(lower, noun) for noun in ("request", "pipeline", "analysis"))
    )
    return which_or_choose_skill or route_request or explicit_orchestration


def _query_mentions_domain(registry: OmicsRegistry, domain: str, query_lower: str) -> bool:
    info = registry.domains.get(domain, {})
    domain_name = str(info.get("name", domain)).lower()
    phrases = (domain_name, domain.lower(), *_DOMAIN_QUERY_ALIASES.get(domain, ()))
    return any(_mentions_phrase(query_lower, phrase.lower()) for phrase in phrases)


def _detect_domain(
    registry: OmicsRegistry,
    query: str,
    file_path: str = "",
    domain_hint: str = "",
) -> str:
    """Pick the most likely omics domain for ``query``.

    Kept for callers that want domain detection on its own. The main
    ``resolve_capability`` flow no longer calls this — it uses
    :func:`_score_skills_and_detect_domain` to compute the domain and the
    candidate scores in a single walk over ``iter_primary_skills`` (OMI-12
    audit P1 #1).
    """
    if domain_hint:
        return domain_hint
    domain, _candidates = _score_skills_and_detect_domain(
        registry, query, file_path=file_path
    )
    return domain


def _score_skills_and_detect_domain(
    registry: OmicsRegistry,
    query: str,
    *,
    file_path: str = "",
) -> tuple[str, list["CapabilityCandidate"]]:
    """Single-pass scoring: walk every primary skill exactly once and emit
    both the detected domain *and* every skill's per-candidate score.

    Pre-refactor the resolver walked ``iter_primary_skills`` twice — once
    inside ``_detect_domain`` (95 skills × per-domain accumulation), then
    again inside ``resolve_capability`` (the same skills, this time with
    the per-skill weights). The two passes used different weights but read
    the same SKILL.md fields (alias, legacy aliases, description tokens,
    trigger keywords), so the duplicate filesystem-derived work was pure
    overhead. This helper accumulates both score sets in one pass.

    Returns ``(detected_domain, candidates)``. ``candidates`` is the full
    list of skills that scored positively (unsorted, unfiltered); callers
    that want only the detected-domain slice filter it themselves.
    """
    query_lower = query.lower()
    query_tokens = _tokenize(query_lower)
    method_tokens = _method_mentions(query_lower)

    # File-path domain detection seeds the domain accumulator before any
    # skill-level signals fire.
    domain_scores: dict[str, float] = {domain: 0.0 for domain in registry.domains}
    if file_path and detect_domain_from_path is not None:
        detected = str(detect_domain_from_path(file_path, fallback="")).strip()
        if detected:
            domain_scores[detected] = (
                domain_scores.get(detected, 0.0) + _DOMAIN_SCORE_FILE_PATH
            )

    candidates: list[CapabilityCandidate] = []
    for alias, skill_info in registry.iter_primary_skills():
        lifecycle_status = str(skill_info.get("lifecycle_status") or "mvp")
        if lifecycle_status not in _ROUTABLE_LIFECYCLE_STATUSES:
            continue
        # Iterate each skill's OWN trigger_keywords once and reuse the
        # match list on both the per-skill (limit=3) and per-domain
        # (limit=2) sides. The previous "compute keyword matches via
        # ``registry.build_keyword_map(domain=None)``" optimisation was
        # incorrect: ``build_keyword_map`` overwrites shared keywords by
        # last-write-wins, so a keyword that lives on multiple skills
        # silently dropped its score from every skill except the last
        # inserted (e.g. ``bulkrna-de`` lost ~3.8 points on shared
        # "differential" keywords because some other-domain skill happened
        # to be inserted later).
        _, all_kw_matches = _score_trigger_keyword_matches(
            query_lower,
            skill_info.get("trigger_keywords", []),
            limit=_SCORE_TRIGGER_KEYWORD_LIMIT,
        )

        # ---- candidate-side scoring (per-skill weights, up to 3 keywords) ----
        candidate = _candidate_score(
            alias,
            skill_info,
            query_lower,
            query_tokens,
            method_tokens,
            keyword_matches=all_kw_matches,
        )
        if candidate is not None:
            candidates.append(candidate)

        # ---- domain-side scoring (per-domain weights; keyword limit drops
        # to 3).  Keep the strongest leaf signal instead of summing every
        # positive skill in a domain. Summation makes domain size a hidden
        # prior (34 single-cell leaves can swamp 1 literature leaf) and lets
        # shared generic words pull queries into the largest domain.
        skill_domain = str(skill_info.get("domain", ""))
        if not skill_domain:
            continue
        delta = 0.0
        if _mentions_phrase(query_lower, alias.lower()):
            delta += _DOMAIN_SCORE_ALIAS_MENTION
        for legacy in skill_info.get("legacy_aliases", []):
            legacy_lower = str(legacy).lower()
            if legacy_lower and _mentions_phrase(query_lower, legacy_lower):
                delta += _DOMAIN_SCORE_LEGACY_ALIAS_MENTION
        description = str(skill_info.get("description", "")).lower()
        overlap = query_tokens & _tokenize(description)
        delta += (
            min(len(overlap), _DOMAIN_SCORE_DESCRIPTION_OVERLAP_CAP)
            * _DOMAIN_SCORE_DESCRIPTION_OVERLAP_PER_TOKEN
        )
        # Reuse the same keyword matches with the tighter per-domain limit
        # so the loop body never rescans this skill's trigger_keywords.
        for kw_phrase in all_kw_matches[:_DOMAIN_SCORE_TRIGGER_KEYWORD_LIMIT]:
            delta += _trigger_keyword_score(kw_phrase)
        domain_scores[skill_domain] = max(
            domain_scores.get(skill_domain, 0.0), delta
        )

    # Domain-name and domain-key textual matches contribute independently of
    # any skill — keep these post-loop so the loop body has one concern.
    for domain_key in registry.domains:
        if _query_mentions_domain(registry, domain_key, query_lower):
            domain_scores[domain_key] = (
                domain_scores.get(domain_key, 0.0) + _DOMAIN_SCORE_DOMAIN_NAME_MATCH
            )

    # Pick the highest-scoring domain. Strict ``>`` matches the old
    # ``_detect_domain`` behaviour: ties go to the first-seen domain in
    # ``registry.domains`` insertion order, which is deterministic because
    # the domain list is hardcoded in ``registry._HARDCODED_DOMAINS``.
    best_domain = ""
    best_score = 0.0
    for d, s in domain_scores.items():
        if s > best_score:
            best_score = s
            best_domain = d
    return best_domain, candidates


def _candidate_score(
    alias: str,
    info: dict[str, Any],
    query_lower: str,
    query_tokens: set[str],
    method_tokens: set[str],
    *,
    keyword_matches: list[str] | None = None,
) -> CapabilityCandidate | None:
    score = 0.0
    reasons: list[str] = []

    alias_lower = alias.lower()
    if alias == "omics-skill-builder" and _requests_skill_creation(query_lower):
        score += _SCORE_SKILL_CREATION_TARGET_BONUS
        reasons.append("query explicitly requests skill creation")
    elif alias == "orchestrator" and _requests_orchestration(query_lower):
        score += _SCORE_ORCHESTRATOR_INTENT_BONUS
        reasons.append("query explicitly requests skill routing/orchestration")

    alias_parts = alias_lower.split("-")
    task_type = alias_parts[-1]
    task_variants = {task_type}
    if task_type.endswith("ing") and len(task_type) > 6:
        task_variants.add(task_type[:-3])
    # Only two-component aliases encode a clean ``domain -> task`` shape.
    # A deeper alias such as ``sc-integrate-cluster`` must not win merely
    # because the query says "cluster" while omitting the required integrate
    # intent.
    task_type_explicit = len(alias_parts) == 2 and any(
        _mentions_phrase(query_lower, variant) for variant in task_variants
    )
    if len(task_type) >= 5 and task_type_explicit:
        score += _SCORE_TASK_TYPE_EXPLICIT
        reasons.append(f"query explicitly names task type '{task_type}'")

    query_modalities = {
        modality
        for modality, phrases in _SINGLECELL_MODALITY_PHRASES.items()
        if any(_mentions_phrase(query_lower, phrase) for phrase in phrases)
    }
    skill_modality = ""
    if str(info.get("domain") or "") == "singlecell":
        if alias_lower.startswith("scatac-"):
            skill_modality = "scatac"
        elif alias_lower.startswith("sc-"):
            skill_modality = "scrna"
    if len(query_modalities) == 1 and skill_modality:
        query_modality = next(iter(query_modalities))
        if query_modality == skill_modality:
            score += _SCORE_MODALITY_MATCH
            reasons.append(f"query explicitly names modality '{query_modality}'")
        else:
            score -= _SCORE_MODALITY_CONFLICT
            reasons.append(
                f"query modality '{query_modality}' conflicts with '{skill_modality}'"
            )

    if _mentions_phrase(query_lower, alias_lower):
        score += _SCORE_ALIAS_MENTION
        reasons.append(f"query explicitly mentions skill '{alias}'")

    for legacy in info.get("legacy_aliases", []):
        legacy_lower = str(legacy).lower()
        if legacy_lower and _mentions_phrase(query_lower, legacy_lower):
            score += _SCORE_LEGACY_ALIAS_MENTION
            reasons.append(f"query mentions legacy alias '{legacy}'")

    description = str(info.get("description", ""))
    description_lower = description.lower()
    overlap = query_tokens & _tokenize(description_lower)
    if overlap:
        overlap_score = (
            min(len(overlap), _SCORE_DESCRIPTION_OVERLAP_CAP)
            * _SCORE_DESCRIPTION_OVERLAP_PER_TOKEN
        )
        score += overlap_score
        reasons.append("description token overlap: " + ", ".join(sorted(list(overlap))[:5]))

    if keyword_matches is None:
        keyword_score, kw_match_list = _score_trigger_keyword_matches(
            query_lower,
            info.get("trigger_keywords", []),
        )
    else:
        kw_match_list = keyword_matches
        keyword_score = sum(_trigger_keyword_score(kw) for kw in kw_match_list)
    if keyword_score:
        score += keyword_score
        reasons.append(
            "trigger keyword match: " + ", ".join(kw_match_list[:3])
        )

    for kw in info.get("param_hints", {}):
        kw_lower = str(kw).lower()
        if kw_lower in method_tokens:
            score += _SCORE_PARAM_HINT_MATCH
            reasons.append(f"requested method '{kw_lower}' appears in param hints")

    validation_level = str(info.get("validation_level") or "smoke-only")
    validation_score = _VALIDATION_SCORE.get(validation_level, 0.0)
    if score > 0 and validation_score:
        score += validation_score
        reasons.append(
            f"validation level {validation_level} tie-break +{validation_score}"
        )

    if score <= 0:
        return None

    return CapabilityCandidate(
        skill=alias,
        domain=str(info.get("domain", "")),
        score=score,
        reasons=reasons,
    )


def _resolve_composite_candidate_chain(
    query: str,
    *,
    file_path: str,
    domain_hint: str,
    input_profile: InputProfile | dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve atomic clauses, then order only graph-connected selections.

    Clause resolution deliberately reuses the ordinary resolver with graph
    expansion disabled.  This keeps lifecycle, skip_when, and routing scores
    identical to single-intent requests while avoiding recursive composition.
    """

    clauses = [part.strip(" ,.;") for part in _COMPOSITE_SPLIT_RE.split(query) if part.strip(" ,.;")]
    if len(clauses) < 2:
        return {}

    selected: list[str] = []
    for clause in clauses:
        decision = resolve_capability(
            clause,
            file_path=file_path,
            domain_hint=domain_hint,
            input_profile=input_profile,
            _build_composite_chain=False,
        )
        if not decision.chosen_skill or decision.chosen_skill in selected:
            continue
        selected.append(decision.chosen_skill)
    if len(selected) < 2:
        return {}

    registry = ensure_registry_loaded()
    return registry.build_candidate_skill_chain(selected)


def _explicit_multi_skill_request(registry: OmicsRegistry, query: str) -> bool:
    """Recognize two named skills after the caller proves execution intent.

    Scientific method descriptions routinely join operations with ``and``
    (for example, "PCA and Leiden"). Those phrases are one skill, not two
    candidate intents. Strong sequencing words remain sufficient on their
    own; this guard only disambiguates the otherwise generic conjunction.
    """

    if " and " not in query:
        return False
    mentioned = {
        name
        for name, _info in registry.iter_primary_skills()
        if _mentions_phrase(query, name.lower())
    }
    return len(mentioned) >= 2


def resolve_capability(
    query: str,
    *,
    file_path: str = "",
    domain_hint: str = "",
    input_profile: InputProfile | dict[str, Any] | None = None,
    _build_composite_chain: bool = True,
) -> CapabilityDecision:
    """Resolve a user request into exact/partial/no-skill coverage."""
    query = (query or "").strip()
    if not query and not file_path:
        return CapabilityDecision(
            query=query,
            reasoning=["empty request"],
        )

    # A caller-supplied profile is advisory.  When the referenced local input
    # exists, observed path facts always win so direct resolver/AnalysisRouter
    # callers cannot accidentally treat assertions as execution evidence.
    if file_path:
        candidate_path = Path(file_path).expanduser()
        if candidate_path.exists():
            input_profile = probe_input_profile(candidate_path)

    registry = ensure_registry_loaded()

    skill_creation_requested = _requests_skill_creation(query)

    # Keep explicit help/install chatter out, but do not use the small
    # hand-written `_GENERIC_ANALYSIS_HINTS` list as a hard pre-filter.  The
    # registry's descriptions + trigger keywords are the richer, extensible
    # source of truth; an early return here previously made valid intents such
    # as PSM identification, BAM alignment QC, PTM sites, and GEO extraction
    # unreachable before their metadata was ever scored.
    if (
        not file_path
        and not skill_creation_requested
        and any(_mentions_phrase(query.lower(), hint) for hint in _NON_ANALYSIS_HINTS)
    ):
        return CapabilityDecision(
            query=query,
            reasoning=["request does not look like an omics analysis task"],
        )

    query_lower = query.lower()

    # Domain detection + per-skill candidate scoring share a single walk
    # over ``iter_primary_skills`` (OMI-12 audit P1 #1) — the pre-refactor
    # code visited each skill twice with different weights for what was
    # effectively the same set of SKILL.md reads.
    control_domain = (
        "orchestrator"
        if skill_creation_requested or _requests_orchestration(query)
        else ""
    )
    if domain_hint or control_domain:
        domain = domain_hint or control_domain
        # When the caller forces a domain, we still need candidate scores
        # for that domain; do the single-pass scoring and discard the
        # detection result.
        _, all_candidates = _score_skills_and_detect_domain(
            registry, query, file_path=file_path
        )
    else:
        domain, all_candidates = _score_skills_and_detect_domain(
            registry, query, file_path=file_path
        )

    if _requests_new_literature_implementation(query_lower):
        return CapabilityDecision(
            query=query,
            domain=domain,
            coverage="no_skill",
            confidence=0.0,
            should_search_web=True,
            should_create_skill=skill_creation_requested,
            missing_capabilities=[
                "request asks to implement a new method from external literature",
            ],
            reasoning=[
                "request asks for new method implementation rather than literature parsing",
            ],
        )

    # Filter the precomputed candidates to the detected domain — same
    # restriction the pre-refactor ``iter_primary_skills(domain=...)`` loop
    # applied; we just compute candidates for every domain up front so the
    # walk happens once.
    if domain:
        candidates = [c for c in all_candidates if c.domain == domain]
    else:
        candidates = list(all_candidates)

    # Consume structured negative-routing rules after domain narrowing but
    # before ranking. A matched host is removed; its declared ``use`` target is
    # inserted/boosted even when the target had no lexical score of its own.
    # Only already-meaningful host candidates are considered, avoiding rules
    # on unrelated zero-signal skills firing because a long query shares a few
    # generic words.
    redirected: dict[str, CapabilityCandidate] = {}
    retained: list[CapabilityCandidate] = []
    for candidate in candidates:
        info = registry.skills.get(candidate.skill, {})
        rule = (
            _matching_skip_rule(info, query_lower)
            if candidate.score >= _RESOLVE_NO_SKILL_THRESHOLD
            else None
        )
        if rule is None:
            retained.append(candidate)
            continue

        target_key = rule.get("use", "")
        target_info = registry.skills.get(target_key, {}) if target_key else {}
        target_status = str(target_info.get("lifecycle_status") or "mvp")
        if not target_info or target_status not in _ROUTABLE_LIFECYCLE_STATUSES:
            continue

        target_alias = str(target_info.get("alias") or target_key)
        existing = next((c for c in candidates if c.skill == target_alias), None)
        redirect_reason = (
            f"structured skip_when on '{candidate.skill}' matched "
            f"'{rule['condition']}'; use '{target_alias}'"
        )
        redirected_candidate = CapabilityCandidate(
            skill=target_alias,
            domain=str(target_info.get("domain", "")),
            score=max(
                existing.score if existing is not None else 0.0,
                candidate.score + _SCORE_SKIP_WHEN_REDIRECT_BONUS,
            ),
            reasons=[redirect_reason] + (list(existing.reasons) if existing else []),
        )
        previous = redirected.get(target_alias)
        if previous is None or redirected_candidate.score > previous.score:
            redirected[target_alias] = redirected_candidate

    redirected_aliases = set(redirected)
    candidates = [c for c in retained if c.skill not in redirected_aliases]
    candidates.extend(redirected.values())

    # Sort by score DESC with a stable alphabetical tie-break on the skill
    # alias. Without the tie-break, the post-PR audit caught WGCNA flapping
    # between ``bulkrna-coexpression`` and ``bulkrna-ppi-network`` depending
    # on the order ``registry.iter_primary_skills`` happened to return them
    # in — which itself depends on filesystem traversal at registry load
    # time. Alphabetical order gives a deterministic winner.
    candidates.sort(key=lambda c: (-c.score, c.skill))

    custom_requested = any(h in query_lower for h in _CUSTOM_FALLBACK_HINTS)
    web_requested = any(h in query_lower for h in _WEB_HINTS)
    composite_requested = (
        not _COMPOSITE_ADVISORY_RE.search(query_lower)
        and any(hint in query_lower for hint in _COMPOSITE_EXECUTION_HINTS)
        and (
            any(h in query_lower for h in _COMPOSITE_HINTS)
            or _explicit_multi_skill_request(registry, query_lower)
        )
    )

    if not candidates or candidates[0].score < _RESOLVE_NO_SKILL_THRESHOLD:
        # Registry scoring must run before this check so niche but well-described
        # omics intents are still discoverable.  Once every candidate remains
        # below the commitment threshold, however, preserve the chat boundary:
        # generic conversation must not become an autonomous analysis merely
        # because common words overlap a SKILL.md description.
        if (
            not file_path
            and not domain_hint
            and not control_domain
            and not _looks_like_analysis_request(query)
        ):
            return CapabilityDecision(
                query=query,
                domain="",
                skill_candidates=candidates,
                reasoning=["request does not look like an omics analysis task"],
            )
        reasons = ["no skill achieved a meaningful semantic match"]
        if skill_creation_requested:
            reasons.append("query explicitly asks to create or package a reusable skill")
        missing = ["no existing OmicsClaw skill sufficiently matches the requested task"]
        if web_requested:
            missing.append("request explicitly asks for external literature or documentation lookup")
        # OMI-12 audit P1 #3: ``should_search_web`` used to be hard-coded
        # ``True`` for every ``no_skill`` outcome. That defaulted the bot's
        # LLM tool-use loop into a web-search step even when the user asked
        # a perfectly normal omics question that just didn't match any
        # currently-registered skill ("do PCA on my data, no skill needed"
        # → False is correct). Now the flag fires only when the query
        # explicitly mentions web/literature wording (the ``_WEB_HINTS``
        # corpus is in this file). The literature-from-paper branch above
        # still sets it ``True`` directly because that path is the explicit
        # "go look up external literature" case.
        return CapabilityDecision(
            query=query,
            # A weak lexical overlap should not invent a domain for unrelated
            # chatter (e.g. weather). Preserve the domain only when the user or
            # file explicitly scoped it; strong semantic cases route above.
            domain=(
                domain
                if domain
                and (
                    bool(file_path)
                    or bool(domain_hint)
                    or bool(control_domain)
                    or _query_mentions_domain(registry, domain, query_lower)
                )
                else ""
            ),
            coverage="no_skill",
            confidence=(
                0.0
                if not candidates
                else min(
                    candidates[0].score / _RESOLVE_NO_SKILL_CONFIDENCE_DIVISOR,
                    _RESOLVE_NO_SKILL_CONFIDENCE_CAP,
                )
            ),
            should_search_web=web_requested,
            should_create_skill=skill_creation_requested,
            skill_candidates=candidates[:5],
            missing_capabilities=missing,
            reasoning=reasons,
        )

    top = candidates[0]
    second = candidates[1] if len(candidates) > 1 else None
    confidence = min(1.0, top.score / _RESOLVE_CONFIDENCE_DIVISOR)
    close_second = bool(second and (top.score - second.score) < _RESOLVE_CLOSE_SECOND_GAP)

    reasoning = [f"top candidate '{top.skill}' scored {round(top.score, 2)}"]
    reasoning.extend(top.reasons[:3])
    missing_capabilities: list[str] = []

    if custom_requested:
        missing_capabilities.append("custom or post-skill analysis step requested")
        reasoning.append("query contains explicit custom-analysis wording")
    if web_requested:
        missing_capabilities.append("latest external methods or documentation requested")
        reasoning.append("query requests web/literature lookups")
    if composite_requested and close_second:
        missing_capabilities.append("request appears to combine multiple analysis intents")
        reasoning.append("query appears composite and candidate gap is narrow")
    if skill_creation_requested:
        reasoning.append("query explicitly asks for a reusable OmicsClaw skill scaffold")

    # Literature lookup is not an uncovered add-on when the selected skill is
    # itself the first-party literature extractor.  The old blanket
    # `web_requested -> partial_skill` rule made every successful literature
    # route look incomplete.
    uncovered_web_requested = web_requested and top.skill != "literature"

    if custom_requested or uncovered_web_requested or (composite_requested and close_second):
        coverage = "partial_skill"
        should_search_web = uncovered_web_requested or not top.reasons
    else:
        coverage = "exact_skill"
        should_search_web = False

    if close_second and coverage == "exact_skill":
        reasoning.append(
            f"second candidate '{second.skill}' is close ({round(second.score, 2)}), but no extra custom step was requested"
        )

    assessment = None
    if input_profile is not None:
        assessment = evaluate_skill_preconditions(
            top.skill,
            input_profile,
            registry=registry,
        )
        top.precondition_status = assessment.status.value
        top.precondition_evaluated = assessment.evaluated
        top.execution_ready = assessment.execution_ready
        top.missing_preconditions = list(assessment.missing)
        top.precondition_reasons = list(assessment.reasons)
        top.recommended_preparation = list(assessment.recommended_preparation)
        reasoning.append(
            f"selected skill preconditions evaluated as '{assessment.status.value}'"
        )

    candidate_chain = (
        _resolve_composite_candidate_chain(
            query,
            file_path=file_path,
            domain_hint=domain_hint,
            input_profile=input_profile,
        )
        if composite_requested and _build_composite_chain
        else {}
    )
    if candidate_chain:
        coverage = "partial_skill"
        composite_gap = "request combines multiple resolved analysis intents"
        if composite_gap not in missing_capabilities:
            missing_capabilities.append(composite_gap)
        if candidate_chain.get("validated_order"):
            reasoning.append(
                "compatibility graph produced composite candidate plan: "
                + " -> ".join(candidate_chain["skills"])
            )
        else:
            reasoning.append(
                "compatibility graph preserved unresolved composite intents: "
                + "; ".join(candidate_chain["requested_skills"])
            )

    return CapabilityDecision(
        query=query,
        # Normally top.domain equals the detected domain because candidates are
        # narrowed above. A structured Skip-when redirect may intentionally
        # cross domains (e.g. bulk enrichment on single-cell input), in which
        # case the chosen skill's domain is the truthful final decision.
        domain=top.domain or domain,
        coverage=coverage,
        confidence=confidence,
        chosen_skill=top.skill,
        should_search_web=should_search_web,
        should_create_skill=skill_creation_requested,
        skill_candidates=candidates[:5],
        missing_capabilities=missing_capabilities,
        reasoning=reasoning,
        precondition_status=(assessment.status.value if assessment else "eligible"),
        precondition_evaluated=(assessment.evaluated if assessment else False),
        execution_ready=(assessment.execution_ready if assessment else True),
        missing_preconditions=(list(assessment.missing) if assessment else []),
        precondition_reasons=(list(assessment.reasons) if assessment else []),
        recommended_preparation=(
            list(assessment.recommended_preparation) if assessment else []
        ),
        candidate_chain=candidate_chain,
    )


__all__ = [
    "CapabilityCandidate",
    "CapabilityDecision",
    "resolve_capability",
]
