"""The per-turn skill discovery block."""

from __future__ import annotations

import asyncio
import json
import pathlib

from omicsclaw.schema import Message, Role, ToolCall, ToolDefinition

from omicsclaw.skills import SkillDiscovery, SkillIndex, load_skills

from .test_loader import write_skill

_SOURCE = pathlib.Path(__file__).resolve().parents[2] / "omicsclaw" / "skills" / "discovery.py"


def build(tmp_path):
    """A four-skill index wide enough to rank, dedup and truncate over."""
    write_skill(
        tmp_path / "spatial" / "spatial-de",
        "spatial-de",
        "Find differentially expressed genes between spatial regions of a slide.",
        trigger="DE, differentially expressed",
    )
    write_skill(
        tmp_path / "spatial" / "spatial-domains",
        "spatial-domains",
        "Detect tissue domains with spatial neighbors and Leiden.",
    )
    write_skill(
        tmp_path / "singlecell" / "sc-clustering",
        "sc-clustering",
        "Cluster single-cell expression profiles with a neighbor graph.",
    )
    write_skill(
        tmp_path / "genomics" / "genomics-qc",
        "genomics-qc",
        "Alignment quality metrics from a BAM file.",
    )
    return load_skills(tmp_path)


def _tools(*names: str) -> tuple[ToolDefinition, ...]:
    return tuple(
        ToolDefinition(name=name, description="", input_schema={"type": "object"})
        for name in names
    )


def _user(text: str) -> Message:
    return Message(role=Role.USER, content=text)


def _loaded(*names: str) -> Message:
    """One assistant turn that called ``use_skill`` on each *name*."""
    return Message(
        role=Role.ASSISTANT,
        tool_calls=tuple(
            ToolCall(
                id=f"c-{name}",
                name="use_skill",
                arguments=json.dumps({"skill_name": name}),
            )
            for name in names
        ),
    )


def discover(discovery, history, tools=None):
    """One augment call, with ``use_skill`` offered unless *tools* says otherwise."""
    offered = _tools("use_skill") if tools is None else _tools(*tools)
    return asyncio.run(discovery.augment(tuple(history), offered))


def hits(block) -> list[str]:
    """The skill names in one served block, in served order."""
    return [
        line[2:].split(" — ", 1)[0]
        for line in block.content.splitlines()
        if line.startswith("- ")
    ]


# ---- ranking and pinning -------------------------------------------------


def test_a_domain_request_ranks_its_own_skill_first(tmp_path):
    discovery = SkillDiscovery(build(tmp_path))

    block = discover(
        discovery, [_user("find differentially expressed genes across the tissue regions")]
    )

    assert hits(block[0])[0] == "spatial-de"
    assert "load with" not in block[0].content
    assert block[0].content.startswith('<skill_discovery signal="turn">')
    assert block[0].content.rstrip().endswith("</skill_discovery>")


def test_a_named_skill_is_pinned_whatever_its_score(tmp_path):
    """A full-name reference is evidence no score gets to outvote."""
    discovery = SkillDiscovery(build(tmp_path), min_score=1.0)

    block = discover(discovery, [_user("run spatial-de on this slide")])

    assert hits(block[0]) == ["spatial-de"]


def test_a_partial_name_is_not_a_reference(tmp_path):
    """``spatial-deconv`` is a different skill's name, not ``spatial-de``."""
    discovery = SkillDiscovery(build(tmp_path), min_score=1.0)

    block = discover(discovery, [_user("run spatial-deconv style deconvolution")])

    assert hits(block[0]) == []


def test_top_k_caps_the_block(tmp_path):
    discovery = SkillDiscovery(build(tmp_path))

    block = discover(discovery, [_user("spatial analysis of the spatial slide's spatial regions")])

    assert 0 < len(hits(block[0])) <= 3


def test_the_score_cutoff_leaves_slots_empty(tmp_path):
    """A full block of matches nobody believes in is the failure mode."""
    discovery = SkillDiscovery(build(tmp_path), min_score=1.0)

    block = discover(
        discovery, [_user("find differentially expressed genes across the tissue")]
    )

    assert len(block) == 1
    assert hits(block[0]) == []
    assert "No skill matched" in block[0].content


# ---- giving up ------------------------------------------------------------


def test_a_short_message_is_left_alone(tmp_path):
    """``continue`` asks to keep going; it does not describe a domain."""
    discovery = SkillDiscovery(build(tmp_path))

    assert discover(discovery, [_user("continue")]) == ()
    assert discover(discovery, [_user("go on")]) == ()
    assert discover(discovery, [_user("please continue")]) == ()


def test_without_the_load_tool_there_is_no_block(tmp_path):
    discovery = SkillDiscovery(build(tmp_path))

    assert discover(
        discovery, [_user("find differentially expressed genes")], tools=("read_file", "bash")
    ) == ()


def test_a_history_with_no_user_message_is_left_alone(tmp_path):
    discovery = SkillDiscovery(build(tmp_path))

    assert discover(discovery, [_loaded("spatial-de")]) == ()


def test_an_empty_index_discovers_nothing():
    assert discover(SkillDiscovery(SkillIndex()), [_user("find differentially expressed genes")]) == ()


def test_a_zero_top_k_disables_the_block(tmp_path):
    discovery = SkillDiscovery(build(tmp_path), top_k=0)

    assert discover(discovery, [_user("find differentially expressed genes")]) == ()


# ---- dedup against history ------------------------------------------------


def test_an_already_loaded_skill_is_not_repeated(tmp_path):
    discovery = SkillDiscovery(build(tmp_path))

    block = discover(
        discovery,
        [
            _user("find differentially expressed genes across the tissue"),
            _loaded("spatial-de"),
            _user("now show me the differentially expressed genes as a table"),
        ],
    )

    assert "spatial-de" not in hits(block[0])


def test_an_unparseable_load_call_costs_nothing(tmp_path):
    """A malformed historical payload is skipped, not raised — and dedups nothing."""
    discovery = SkillDiscovery(build(tmp_path))
    history = [
        _user("find differentially expressed genes across the tissue"),
        Message(
            role=Role.ASSISTANT,
            tool_calls=(
                ToolCall(id="c-1", name="use_skill", arguments='{"skill_name": "spatial-de'),
            ),
        ),
        _user("show the differentially expressed genes again"),
    ]

    block = discover(discovery, history)

    assert "spatial-de" in hits(block[0])


def test_a_compacted_away_load_lets_the_skill_back_in(tmp_path):
    """Dedup is over the calls the history still carries, not a tally the
    augmentor keeps — a summary that replaced an earlier ``use_skill``
    cannot keep suppressing what it no longer mentions."""
    discovery = SkillDiscovery(build(tmp_path))

    block = discover(
        discovery,
        [
            Message(role=Role.ASSISTANT, content="(summary) earlier: spatial domains"),
            _loaded("spatial-de"),
            _user("find domains and differentially expressed genes in the tissue"),
        ],
    )

    served = hits(block[0])
    assert "spatial-de" not in served
    assert "spatial-domains" in served


# ---- the served lines ------------------------------------------------------


def test_descriptions_are_truncated_to_300_characters(tmp_path):
    write_skill(
        tmp_path / "singlecell" / "sc-markers",
        "sc-markers",
        "marker gene finding. " * 60,
    )
    discovery = SkillDiscovery(load_skills(tmp_path))

    block = discover(discovery, [_user("find marker genes for each cluster")])

    (line,) = [l for l in block[0].content.splitlines() if l.startswith("- ")]
    description = line.split(" — ", 1)[1]
    assert len(description) == 300


def test_the_block_names_both_tools(tmp_path):
    discovery = SkillDiscovery(build(tmp_path))

    block = discover(discovery, [_user("find differentially expressed genes")])

    assert "use_skill(name)" in block[0].content
    assert "search_skills(query)" in block[0].content


def test_the_tool_names_come_from_the_constructor(tmp_path):
    discovery = SkillDiscovery(build(tmp_path), use_skill_tool="load", search_tool="seek")

    block = discover(discovery, [_user("find differentially expressed genes")], tools=("load",))

    assert "load(name)" in block[0].content
    assert "seek(query)" in block[0].content


# ---- telemetry -------------------------------------------------------------


class _Span:
    def __init__(self, tracer, name, attributes):
        self._tracer = tracer
        self.name = name
        self.attributes = dict(attributes or {})
        self.ended = False

    def set_attributes(self, attributes):
        self.attributes.update(attributes)

    def end(self):
        self.ended = True


class _Tracer:
    """Records every span it is asked to start, the way a test wants a recorder to."""

    def __init__(self):
        self.spans: list[_Span] = []

    def start_span(self, name, *, parent=None, attributes=None):
        span = _Span(self, name, attributes)
        self.spans.append(span)
        return span


def test_one_span_per_served_block(tmp_path):
    tracer = _Tracer()
    discovery = SkillDiscovery(build(tmp_path), tracer=tracer)

    block = discover(discovery, [_user("find differentially expressed genes across the tissue")])

    (span,) = tracer.spans
    assert span.ended
    assert span.name == "omicsclaw.skill.discovery"
    assert span.attributes["skill.discovery.signal"] == "turn"
    assert "spatial-de" in span.attributes["skill.discovery.hits"]
    assert span.attributes["skill.discovery.low_confidence"] is False
    assert len(hits(block[0])) == len(span.attributes["skill.discovery.hits"].split(","))


def test_a_low_confidence_block_traces_with_no_hits(tmp_path):
    tracer = _Tracer()
    discovery = SkillDiscovery(build(tmp_path), tracer=tracer)

    discover(discovery, [_user("book a restaurant table for tonight")])

    (span,) = tracer.spans
    assert span.attributes["skill.discovery.hits"] == ""
    assert span.attributes["skill.discovery.low_confidence"] is True


def test_no_span_without_an_injection(tmp_path):
    tracer = _Tracer()
    discovery = SkillDiscovery(build(tmp_path), tracer=tracer)

    discover(discovery, [_user("continue")])

    assert tracer.spans == []


def test_the_default_tracer_costs_nothing(tmp_path):
    """An unconfigured deployment runs the whole chain without a branch."""
    discovery = SkillDiscovery(build(tmp_path))

    block = discover(discovery, [_user("find differentially expressed genes across the tissue")])

    assert hits(block[0])


# ---- the leaf-layer ruling --------------------------------------------------


def test_discovery_does_not_import_the_engine():
    """The augmentor satisfies the protocol structurally or not at all.

    The leaf-layer guard already parametrizes over every module in the
    package; this names the one neighbour whose absence is the design,
    so a reader of this file sees the contract without opening that one.
    """
    source = _SOURCE.read_text(encoding="utf-8")

    assert "from omicsclaw.engine" not in source
    assert "import omicsclaw.engine" not in source


# ---- the tokenizer's edges ---------------------------------------------------


def test_tokenize_splits_camel_case_and_folds_naive_plurals():
    from omicsclaw.skills.discovery import tokenize

    assert tokenize("spatialDE Genes") == ("spatial", "de", "gene")
    assert tokenize("the glass QC runs") == ("glass", "qc", "run")


def test_clean_query_strips_transcript_cargo():
    from omicsclaw.skills.discovery import clean_query

    cleaned = clean_query(
        "run this ```python\nprint(1)\n``` and this "
        "<tool_call>{\"a\": 1}</tool_call> for request 550e8400-e29b-41d4"
        "-a716-446655440000 and job c3RhYmxlLWRhdGEtcGlwZWxpbmU="
    )

    assert "python" not in cleaned
    assert "tool_call" not in cleaned
    assert "550e8400" not in cleaned
    assert "request" in cleaned
