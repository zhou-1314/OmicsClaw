"""Tests for unified capability resolution."""

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
    # An omics-shaped query that doesn't match any skill (single-cell
    # doublet detection — the resolver lacks a strong-signal skill, so it
    # falls through to no_skill). No web/literature wording → False.
    decision = resolve_capability(
        "Detect doublets in my single-cell data using scDblFinder"
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
        "Detect doublets in my single-cell data; check the latest documentation about scDblFinder"
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
