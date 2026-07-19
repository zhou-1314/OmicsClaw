"""Tests for unified capability resolution."""

import pytest

from omicsclaw.skill.preconditions import InputProfile
from omicsclaw.skill.registry import OmicsRegistry

from omicsclaw.skill.capability_resolver import resolve_capability


def test_resolve_capability_exact_skill():
    decision = resolve_capability("Run spatial preprocessing on my Visium dataset")
    assert decision.coverage == "exact_skill"
    assert decision.chosen_skill == "spatial-preprocess"
    assert decision.confidence > 0


def test_resolve_capability_partial_skill():
    decision = resolve_capability(
        "Run spatial preprocessing and then compute a custom neighborhood entropy score not in OmicsClaw"
    )
    assert decision.coverage == "partial_skill"
    assert decision.chosen_skill == "spatial-preprocess"
    assert any("custom" in item.lower() for item in decision.missing_capabilities)


def test_resolve_capability_no_skill():
    decision = resolve_capability(
        "Implement a hidden Markov model for chromatin state transition analysis from latest literature"
    )
    assert decision.coverage == "no_skill"
    assert decision.chosen_skill == ""
    assert decision.should_search_web is True


def test_resolve_capability_marks_skill_creation_requests():
    decision = resolve_capability(
        "Create a new OmicsClaw skill for CellCharter-based spatial domain analysis"
    )
    assert decision.should_create_skill is True


def test_resolve_capability_detects_domain_from_multi_suffix_file_path():
    decision = resolve_capability(
        "run variant analysis",
        file_path="/tmp/sample.vcf.gz",
    )
    assert decision.domain == "genomics"


def test_resolve_capability_detects_spatial_microenvironment_subset_skill():
    decision = resolve_capability(
        "Extract a tumor microenvironment neighborhood subset within 50 microns around tumor cells"
    )
    assert decision.coverage == "exact_skill"
    assert decision.chosen_skill == "spatial-microenvironment-subset"


def test_resolve_capability_routes_singular_spatial_niche_to_domains():
    """Regression: a 'spatial niche' request must reach ``spatial-domains``.

    Singular "niche" used to score 2.35 (one description token ``spatial`` at
    0.85 + the floor-weight ``niche`` keyword at 1.5) — just under the 3.0
    no-skill threshold — because the skill's description/tags only carry the
    *plural* "niches", so the singular form earned no description-overlap
    credit. The request silently fell through to the autonomous code path
    instead of the spatial niche/domain skill. Canonical singular phrasings
    are now registered as trigger keywords on ``spatial-domains``.
    """
    for query in (
        "spatial niche identification",
        "perform spatial niche identification on slideseqv2_mouse_hippocampus.h5ad",
        "niche identification",
        "detect spatial niches",
    ):
        decision = resolve_capability(query, file_path="slideseqv2_mouse_hippocampus.h5ad")
        assert decision.coverage == "exact_skill", f"{query!r} -> {decision.coverage}"
        assert decision.chosen_skill == "spatial-domains", (
            f"{query!r} -> {decision.chosen_skill!r}"
        )


def test_resolve_capability_keeps_local_niche_extraction_on_microenvironment_subset():
    """Adding niche keywords to ``spatial-domains`` must not steal the local
    'extract the niche *around* a cell type' intent from
    ``spatial-microenvironment-subset``."""
    decision = resolve_capability(
        "extract the niche around T cells by spatial radius",
        file_path="x.h5ad",
    )
    assert decision.chosen_skill == "spatial-microenvironment-subset"


def test_resolve_capability_routes_spatial_raw_fastq_requests_to_spatial_domain():
    decision = resolve_capability(
        "Run st_pipeline on these Visium spatial FASTQs with barcode coordinates",
        file_path="/tmp/sample_R1.fastq.gz",
    )
    assert decision.domain == "spatial"
    assert decision.chosen_skill == "spatial-raw-processing"


def test_resolve_capability_no_skill_fallback_defaults_should_search_web_to_false():
    """``should_search_web`` used to be hard-coded ``True`` on every
    ``no_skill`` outcome, defaulting the bot's LLM tool-use loop into a
    web-search step even when the query had no web/literature wording.
    After OMI-12 audit P1 #3 the flag only fires when ``_WEB_HINTS``
    matches (or the request is the explicit "implement from literature"
    case, handled by ``_requests_new_literature_implementation``).
    """
    # An omics-shaped query that doesn't match any skill (transcriptional
    # bursting kinetics — no OmicsClaw skill covers it, so it falls through to
    # no_skill). No web/literature wording → False.
    decision = resolve_capability(
        "Estimate transcriptional bursting kinetics from my single-cell data"
    )
    assert decision.coverage == "no_skill"
    assert decision.should_search_web is False


def test_resolve_capability_no_skill_with_web_hints_still_searches_web():
    """The flip in the test above must NOT mute the path the user actually
    asked for web search on. Adding ``documentation`` / ``latest`` /
    ``literature`` / etc. to an omics-shaped no_skill query reverts the
    flag to ``True``.
    """
    decision = resolve_capability(
        "Estimate transcriptional bursting kinetics from my single-cell data; "
        "check the latest documentation"
    )
    assert decision.coverage == "no_skill"
    assert decision.should_search_web is True


def test_resolve_capability_literature_implementation_still_searches_web():
    """The "implement a new method from latest literature" path is
    independent of ``_WEB_HINTS`` — it has its own explicit branch (via
    ``_requests_new_literature_implementation``) and must keep
    ``should_search_web=True``.
    """
    decision = resolve_capability(
        "Implement a hidden Markov model for chromatin state transition analysis from latest literature"
    )
    assert decision.coverage == "no_skill"
    assert decision.should_search_web is True


def test_score_skills_and_detect_domain_visits_each_skill_once():
    """The pre-refactor resolver walked ``iter_primary_skills`` twice for
    every chat turn — once in ``_detect_domain``, once in
    ``resolve_capability``. The single-pass helper introduced in
    OMI-12 audit P1 #1 must visit each skill exactly one time per call.

    Asserted by monkey-patching ``iter_primary_skills`` to count calls.
    """
    from omicsclaw.skill import capability_resolver as cr

    real_registry = cr.ensure_registry_loaded()
    call_counts: list[int] = []
    real_iter = real_registry.__class__.iter_primary_skills

    def counting_iter(self, domain=None):
        items = real_iter(self, domain=domain)
        call_counts.append(len(items))
        return items

    # Patch the bound method on the singleton.
    original = real_registry.iter_primary_skills
    real_registry.iter_primary_skills = counting_iter.__get__(real_registry)
    try:
        cr.resolve_capability("preprocess my Visium spatial transcriptomics dataset")
    finally:
        real_registry.iter_primary_skills = original

    # One scoring pass over every primary skill — no domain filter inside
    # ``_score_skills_and_detect_domain``; the per-domain filter happens
    # on the precomputed candidate list, not via a second walk.
    assert len(call_counts) == 1, (
        f"resolve_capability invoked iter_primary_skills {len(call_counts)} "
        f"times (per-call sizes={call_counts}); the post-refactor flow must "
        f"call it exactly once."
    )


def test_resolve_capability_breaks_score_ties_alphabetically():
    """The candidate sort must tie-break on the canonical alias so the same
    query produces the same ``chosen_skill`` regardless of which order
    ``registry.iter_primary_skills`` happens to return tied skills in.

    Pre-fix this test failed about half the time depending on filesystem
    traversal at registry load — ``bulkrna-coexpression`` and
    ``bulkrna-ppi-network`` both score 7.25 for this WGCNA query, and
    Python's stable sort kept whichever the registry yielded first.
    """
    decision = resolve_capability(
        "Build a WGCNA co-expression network on my bulk RNA-seq counts, return hub genes"
    )
    top_two = [c.skill for c in decision.skill_candidates[:2]]
    if len(top_two) >= 2 and decision.skill_candidates[0].score == decision.skill_candidates[1].score:
        assert top_two == sorted(top_two), (
            f"tied top candidates must be sorted alphabetically; got {top_two}"
        )
    # And the specific query that originally exposed the flake must produce
    # ``bulkrna-coexpression`` (alphabetically before ``bulkrna-ppi-network``)
    # under any registry iteration order.
    assert decision.chosen_skill == "bulkrna-coexpression"


def test_resolve_capability_excludes_draft_and_deprecated_skills(monkeypatch):
    """Governance metadata must affect automatic routing, not just catalog UI."""
    from omicsclaw.skill import capability_resolver as cr

    registry = OmicsRegistry()
    registry.domains = {"spatial": {"name": "Spatial Transcriptomics", "skills": []}}
    registry.skills = {
        "draft-cluster": {
            "alias": "draft-cluster",
            "domain": "spatial",
            "description": "Load when clustering spatial transcriptomics data.",
            "trigger_keywords": ["clustering"],
            "legacy_aliases": [],
            "param_hints": {},
            "lifecycle_status": "draft",
        },
        "deprecated-cluster": {
            "alias": "deprecated-cluster",
            "domain": "spatial",
            "description": "Load when clustering spatial transcriptomics data.",
            "trigger_keywords": ["clustering"],
            "legacy_aliases": [],
            "param_hints": {},
            "lifecycle_status": "deprecated",
            "superseded_by": "stable-cluster",
        },
        "stable-cluster": {
            "alias": "stable-cluster",
            "domain": "spatial",
            "description": "Load when clustering spatial transcriptomics data.",
            "trigger_keywords": ["clustering"],
            "legacy_aliases": [],
            "param_hints": {},
            "lifecycle_status": "stable",
        },
    }
    registry.canonical_aliases = list(registry.skills)
    registry._loaded = True
    monkeypatch.setattr(cr, "ensure_registry_loaded", lambda: registry)

    decision = cr.resolve_capability("clustering spatial transcriptomics data")

    aliases = [candidate.skill for candidate in decision.skill_candidates]
    assert decision.chosen_skill == "stable-cluster"
    assert "draft-cluster" not in aliases
    assert "deprecated-cluster" not in aliases


def test_explicit_deprecated_alias_routes_to_its_governed_replacement(monkeypatch):
    from omicsclaw.skill import capability_resolver as cr

    registry = OmicsRegistry()
    registry.domains = {"spatial": {"name": "Spatial Transcriptomics", "skills": []}}
    registry.skills = {
        "old-cluster": {
            "alias": "old-cluster",
            "domain": "spatial",
            "description": "Retired implementation.",
            "trigger_keywords": [],
            "legacy_aliases": ["legacy-cluster"],
            "param_hints": {},
            "lifecycle_status": "deprecated",
            "superseded_by": "new-cluster",
        },
        "new-cluster": {
            "alias": "new-cluster",
            "domain": "spatial",
            "description": "Current implementation.",
            "trigger_keywords": [],
            "legacy_aliases": [],
            "param_hints": {},
            "lifecycle_status": "stable",
            "validation_level": "demo-validated",
        },
    }
    registry.canonical_aliases = list(registry.skills)
    registry._loaded = True
    monkeypatch.setattr(cr, "ensure_registry_loaded", lambda: registry)

    decision = cr.resolve_capability("run old-cluster")
    legacy_decision = cr.resolve_capability("run legacy-cluster")

    assert decision.chosen_skill == "new-cluster"
    assert legacy_decision.chosen_skill == "new-cluster"
    assert any("superseded" in reason for reason in decision.reasoning)
    assert all(
        candidate.skill != "old-cluster"
        for candidate in decision.skill_candidates
    )


def test_resolve_capability_consumes_structured_skip_when():
    decision = resolve_capability(
        "Use sc-clustering, but QC normalization HVG and PCA have not run yet"
    )

    assert decision.chosen_skill == "sc-preprocessing"
    assert any("skip_when" in reason for reason in decision.reasoning)


def test_resolve_capability_skip_when_respects_negation_polarity():
    decision = resolve_capability(
        "Use sc-clustering after QC normalization HVG and PCA have already run"
    )

    assert decision.chosen_skill == "sc-clustering"
    assert not any("skip_when" in reason for reason in decision.reasoning)


def test_resolve_capability_cross_domain_skip_redirect_updates_domain():
    decision = resolve_capability(
        "Use bulkrna-enrichment, but the input is single-cell"
    )

    assert decision.chosen_skill == "sc-enrichment"
    assert decision.domain == "singlecell"


def test_composite_plan_binds_an_explicitly_named_skill_method():
    inferred = resolve_capability(
        "run sc-preprocessing with scanpy and then sc-clustering"
    )

    assert inferred.candidate_chain["method_bindings"] == {
        "sc-preprocessing": "scanpy"
    }

    supplied = resolve_capability(
        "run sc-preprocessing and then sc-clustering",
        method_bindings={"sc-preprocessing": "scanpy"},
    )
    assert supplied.candidate_chain["method_bindings"] == {
        "sc-preprocessing": "scanpy"
    }

    negated = resolve_capability(
        "run sc-preprocessing without scanpy and then sc-clustering"
    )
    assert "method_bindings" not in negated.candidate_chain


def test_composite_profile_without_unified_method_flag_fails_closed():
    decision = resolve_capability(
        "run sc-preprocessing and then sc-clustering with tsne"
    )

    assert decision.candidate_chain == {}


def test_resolve_capability_uses_singlecell_modality_to_disambiguate_preprocessing():
    """A downstream-method mention must not erase an explicit scRNA modality.

    The wording intentionally differs from the oracle corpus: this guards the
    ontology-level scRNA/scATAC distinction rather than one benchmark phrase.
    """
    scrna = resolve_capability(
        "Use a preprocessing workflow for scRNA-seq; UMAP and Leiden will run later."
    )
    scatac = resolve_capability(
        "Use a preprocessing workflow for scATAC peak counts before UMAP and Leiden."
    )

    assert scrna.chosen_skill == "sc-preprocessing"
    assert scatac.chosen_skill == "scatac-preprocessing"
    assert any("modality 'scrna'" in reason for reason in scrna.reasoning)
    assert any("modality 'scatac'" in reason for reason in scatac.reasoning)


@pytest.mark.parametrize(
    ("query", "expected_skill"),
    [
        (
            "What is the best route for RNA velocity analysis in scRNA-seq data?",
            "sc-velocity",
        ),
        (
            "Please choose the optimal single-cell clustering resolution for this pipeline.",
            "sc-clustering",
        ),
        (
            "Route this scRNA-seq differential expression analysis appropriately.",
            "sc-de",
        ),
    ],
)
def test_analysis_wording_does_not_force_the_orchestrator_control_domain(
    query: str,
    expected_skill: str,
):
    decision = resolve_capability(query)

    assert decision.chosen_skill == expected_skill
    assert decision.domain != "orchestrator"


def test_ambiguous_parameter_choice_is_not_misclassified_as_meta_routing():
    ambiguous = resolve_capability(
        "Please choose the optimal clustering resolution for this pipeline."
    )
    assert ambiguous.domain != "orchestrator"


def test_resolve_capability_uses_validation_as_a_small_tie_break(monkeypatch):
    from omicsclaw.skill import capability_resolver as cr

    registry = OmicsRegistry()
    registry.domains = {"spatial": {"name": "Spatial Transcriptomics", "skills": []}}
    common = {
        "domain": "spatial",
        "description": "Load when clustering spatial transcriptomics data.",
        "trigger_keywords": ["clustering"],
        "legacy_aliases": [],
        "param_hints": {},
        "lifecycle_status": "mvp",
    }
    registry.skills = {
        "a-smoke": {**common, "alias": "a-smoke", "validation_level": "smoke-only"},
        "z-fixture": {
            **common,
            "alias": "z-fixture",
            "validation_level": "fixture-validated",
        },
    }
    registry.canonical_aliases = list(registry.skills)
    registry._loaded = True
    monkeypatch.setattr(cr, "ensure_registry_loaded", lambda: registry)

    decision = cr.resolve_capability("clustering spatial transcriptomics data")

    assert decision.chosen_skill == "z-fixture"
    assert "validation level fixture-validated" in " ".join(decision.reasoning)


def test_candidate_wide_precondition_penalty_reranks_but_keeps_incompatible_candidates(
    monkeypatch,
):
    from omicsclaw.skill import capability_resolver as cr

    registry = OmicsRegistry()
    registry.domains = {"spatial": {"name": "Spatial Transcriptomics", "skills": []}}
    common = {
        "domain": "spatial",
        "description": "Run analysis on a scientific data table.",
        "trigger_keywords": ["analysis"],
        "legacy_aliases": [],
        "param_hints": {},
        "lifecycle_status": "mvp",
        "validation_level": "smoke-only",
        "output_contract": {},
    }
    registry.skills = {
        "a-analysis": {
            **common,
            "alias": "a-analysis",
            "input_contract": {
                "file_types": ["vcf"],
                "path_kinds": ["file"],
                "preconditions": {"data_shape": {}},
            },
        },
        "z-analysis": {
            **common,
            "alias": "z-analysis",
            "input_contract": {
                "file_types": ["csv"],
                "path_kinds": ["file"],
                "preconditions": {"data_shape": {}},
            },
        },
    }
    registry.canonical_aliases = list(registry.skills)
    registry._loaded = True
    monkeypatch.setattr(cr, "ensure_registry_loaded", lambda: registry)

    decision = cr.resolve_capability(
        "run analysis",
        domain_hint="spatial",
        input_profile=InputProfile(file_type="csv", path_kind="file"),
    )

    assert decision.chosen_skill == "z-analysis"
    by_skill = {candidate.skill: candidate for candidate in decision.skill_candidates}
    assert set(by_skill) == {"a-analysis", "z-analysis"}
    assert by_skill["a-analysis"].precondition_status == "blocked"
    assert by_skill["a-analysis"].precondition_penalty > 0
    assert by_skill["a-analysis"].semantic_score == by_skill["z-analysis"].semantic_score
    assert by_skill["z-analysis"].precondition_status == "eligible"
    assert by_skill["z-analysis"].precondition_penalty == 0
