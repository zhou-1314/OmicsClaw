"""Rank indexed skills with lexical retrieval for the current request.

Weighted BM25 and substring rankings are combined with reciprocal-rank
fusion. Retrieval does not resolve arbitrary synonyms or call another model.
Low-confidence guidance can direct the agent to search_skills.

Historical design notes: docs/engineering/runtime-framework-design-notes.md.
"""

from __future__ import annotations

import functools
import json
import math
import re
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

from omicsclaw.schema import Message, Role, ToolDefinition

from .index import SkillIndex
from .skill import Skill

__all__ = [
    "DEFAULT_SKILL_DISCOVERY_TOP_K",
    "SKILL_DISCOVERY_MIN_SCORE",
    "SPAN_SKILL_DISCOVERY",
    "SkillDiscovery",
]

DEFAULT_SKILL_DISCOVERY_TOP_K = 3
"""Hits per discovery block. Three is the ceiling, not the target: the
score cutoff below is allowed to leave slots empty, because three
tentative guesses cost the model more attention than they repay."""

SKILL_DISCOVERY_MIN_SCORE = 0.02
"""The fused score a hit must reach to be worth a line.

The cutoff sits between the two ways a skill can appear in the fused
score. A whole query term that matches a corpus token lands the skill
in *both* rankings — BM25 scores it and the substring ranking sees the
same word — which is worth at least ``1/61 + 1/(60+88) ≈ 0.023`` even
at the extremes, and passes. A partial-word accident ("plan" inside
"planned") lands in the substring ranking only, worth at most
``1/61 ≈ 0.016`` even at rank one, and fails. That is the whole
ruling: a line in the block certifies that a word of the request
matched a word of a skill, not that some letters did."""

SPAN_SKILL_DISCOVERY = "omicsclaw.skill.discovery"
"""The span an actual injection opens. Tool calls already carry their own
spans through the tracing hook; opening one here for ``use_skill`` or
``search_skills`` would count the same act twice."""

_K1 = 1.5
_B = 0.75
_RRF_K = 60
"""The standard BM25 pair and the standard fusion constant, kept as
module constants rather than constructor knobs: none of them has a
tuning story in this repository, and a knob nobody turns is an
interface cost paid for nothing."""

_DESCRIPTION_LIMIT = 300

_STOPWORDS = frozenset(
    {
        "a", "after", "an", "and", "are", "as", "at", "be", "been", "before", "but", "by",
        "during",
        "can", "could", "did", "do", "does", "for", "from", "had", "has",
        "have", "how", "i", "in", "is", "it", "its", "me", "my", "not", "please",
        "of", "on", "or", "our", "should", "that", "the", "their", "them",
        "then", "there", "these", "they", "this", "to", "was", "we",
        "were", "what", "when", "which", "will", "with", "would", "you",
        "your",
    }
)
"""Function words only. Domain words like ``data`` or ``analysis`` are
deliberately absent: they are weak discriminants, but removing them
would also remove them from the corpus side, and a query and a skill
description sharing them is evidence, not noise. The task-meta
vocabulary that names the session or the deliverable is handled on the
query side instead, by ``_WEAK_TERMS`` below: it stays in the corpus,
where it is evidence, and leaves only the serving gate."""

_WEAK_TERMS = frozenset(
    {
        # session / task bookkeeping
        "continue", "previou", "step", "next", "back", "off", "left", "start",
        "stop", "done", "finish", "work", "working", "task", "session",
        "summary", "summarize", "bullet", "produce", "produced",
        # request framing
        "what", "how", "which", "why", "when", "where", "give", "show",
        "tell", "make", "made", "use", "using", "want", "need", "would",
        "could", "should", "please", "help", "something", "anything",
        "like", "look", "quick", "briefly", "simple", "simply", "just",
        "really", "maybe", "perhap", "also", "still", "then", "than",
        "per", "via", "based", "without", "any", "between", "difference",
        "explain", "example", "case", "way", "thing", "part", "bit", "ok",
        "thank", "two", "three", "four", "five", "sentence", "match",
        "some", "far", "so", "grab", "coffee", "talk", "meeting", "ide",
        "theme", "dark", "light", "prefer", "aside", "while", "coding",
        # workflow verbs
        "run", "go", "going", "route", "appropriate", "appropriately",
        "implement", "recent", "brand", "new", "read", "find", "parse",
        "extract", "fetch", "download", "list", "check",
        # files, formats and deliverables
        "file", "path", "folder", "directory", "format", "convert",
        "converting", "conversion", "rename", "renaming", "change",
        "changing", "output",
        "result", "table", "csv", "tsv", "parquet", "png", "jpg", "jpeg",
        "tiff", "svg", "figure", "plot", "title", "font", "color",
        "palette", "viridis", "recolor", "lowercase", "today", "date",
        "prefix", "column", "row", "size", "big", "bigger", "smaller",
        "large",
        # generic analysis vocabulary
        "data", "dataset", "analysi", "analyse", "analyze", "method",
        "approach", "workflow", "pipeline", "metadata", "value", "error",
        "rate", "family", "wise",
    }
)
"""Task-meta vocabulary: words that name the session, the workspace or
the deliverable rather than a domain.

The corpus keeps these — skill descriptions are full of them, and two
skills sharing one is evidence, not noise — but they are removed from
the query side of the serving gate. A request built entirely of them
("continue where we left off", "recolor the figure", "rename the
outputs") is not a description of a domain, and a discovery block
under it would be guesswork the model would have to argue with; one
domain word ("leiden", "volcano", "wgcna") is enough to pass. This is
the corpus-tuned stopword patch the lexical blind spot expects, in the
same family as ``tags`` and ``triggers``: the list grows as the
catalogue does, and every entry is a word a real request used to dress
up a non-domain ask.

Entries are the tokenizer's output forms — camel-split, lowercased,
naively singularised ("analysis" arrives as ``analysi``, "datasets" as
``dataset``) — because the gate compares them against
:func:`tokenize` output, not against raw text.
"""


_WORD = re.compile(r"[A-Za-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

_FENCED = re.compile(r"```.*?```", re.DOTALL)
_TAG_BLOCK = re.compile(r"<[A-Za-z_][\w.-]*>.*?</[A-Za-z_][\w.-]*>", re.DOTALL)
_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_LONG_ID = re.compile(r"\b[A-Za-z0-9_-]{24,}\b")
"""What a pasted tool transcript carries and a request does not: fenced
blocks, tag-delimited blocks, UUIDs, and id strings long enough to be
handles rather than words. Stripped before tokenizing because a
42-character run of base64 tokenizes into exactly the kind of rare-term
noise BM25 rewards most."""

_AttributeValue = str | bool | int | float
"""Spelled here rather than imported from the recorder, for the same
reason the tracer protocols below are spelled here."""


class _Span(Protocol):
    """The recorder's span, taken structurally."""

    def set_attributes(self, attributes: Mapping[str, _AttributeValue]) -> None: ...

    def end(self) -> None: ...


class _Tracer(Protocol):
    """The recorder's tracer, taken structurally.

    One method, matching
    :class:`~omicsclaw.observability.contract.Tracer` call for call, so
    ``app.telemetry.tracer`` can be handed over unchanged at the
    composition root without this package importing the recorder's.
    """

    def start_span(
        self,
        name: str,
        *,
        parent: "_Span | None" = None,
        attributes: Mapping[str, _AttributeValue] | None = None,
    ) -> "_Span": ...


class _NoopSpan:
    __slots__ = ()

    def set_attributes(self, attributes: Mapping[str, _AttributeValue]) -> None:
        return None

    def end(self) -> None:
        return None


class _NoopTracer:
    """The unconfigured deployment's whole discovery telemetry."""

    __slots__ = ()

    def start_span(
        self,
        name: str,
        *,
        parent: "_Span | None" = None,
        attributes: Mapping[str, _AttributeValue] | None = None,
    ) -> "_Span":
        return _NoopSpan()


def _singular(word: str) -> str:
    """Drop a trailing ``s`` — the naive way, on purpose.

    Only for words of four letters or more (``ls`` and ``qc`` survive),
    never for ``-ss`` (``class`` does not become ``clas``), and with no
    irregulars: ``genes`` and ``samples`` are the plurals this corpus
    actually carries, and a stemmer's correctness on English nouns is
    not worth a dependency the leaf layer forbids.
    """
    if len(word) >= 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokenize(text: str) -> tuple[str, ...]:
    """*text* as retrieval tokens, in order.

    Lowercase, camelCase split (``spatialDE`` → ``spatial`` ``de``),
    non-alphanumerics dropped (``spatial-de`` → ``spatial`` ``de``),
    function words dropped, naive singulars folded onto their stem so a
    query's ``domains`` meets a description's ``domain``.
    """
    words: list[str] = []
    for chunk in _WORD.findall(text):
        for part in _CAMEL.split(chunk):
            word = part.lower()
            if word in _STOPWORDS:
                continue
            word = _singular(word)
            if word in _STOPWORDS or len(word) < 2:
                continue
            words.append(word)
    return tuple(words)


def _strong_terms(terms: Sequence[str]) -> tuple[str, ...]:
    """*terms* minus the task-meta vocabulary of :data:`_WEAK_TERMS`.

    The surviving terms are what the serving gate looks at: none left
    means the request names no domain, and the block is withheld
    silently — like the thin-query guard above it, silence beats a
    block of guesses. The weak terms leave the gate only, never the
    ranking, where they still help order the skills a strong term
    matched.
    """
    return tuple(term for term in terms if term not in _WEAK_TERMS)


def clean_query(text: str) -> str:
    """*text* without the cargo a pasted transcript brings along."""
    text = _FENCED.sub(" ", text)
    text = _TAG_BLOCK.sub(" ", text)
    text = _UUID.sub(" ", text)
    return _LONG_ID.sub(" ", text)


@dataclass(frozen=True)
class _Corpus:
    """One BM25 index over a :class:`SkillIndex`, keyed by skill position.

    Field weighting is expressed as term repetition — ``name`` thrice,
    ``domain``/``tags``/``triggers`` twice, ``description`` once — which
    keeps the scoring one standard formula over one bag of tokens rather
    than a per-field score in need of its own fusion.
    """

    bags: tuple[tuple[tuple[str, ...], dict[str, int]], ...]
    """Each skill's weighted token bag and its term counts, in index
    order."""

    lengths: tuple[int, ...]
    """Bag lengths, the ``dl`` of the length normaliser."""

    average_length: float
    """Mean bag length, the ``avgdl`` of the length normaliser."""

    document_frequency: dict[str, int]
    """How many bags carry each term."""

    def scores(self, terms: Sequence[str]) -> list[float]:
        """One BM25 score per skill for *terms*, taken as a set.

        Query-side repetition is ignored: a user who writes ``cluster``
        three times is not three times as interested in clustering, and
        counting it would let word repetition beat field weighting.
        """
        total = len(self.bags)
        results = [0.0] * total
        for term in dict.fromkeys(terms):
            df = self.document_frequency.get(term, 0)
            if not df:
                continue
            idf = math.log(1.0 + (total - df + 0.5) / (df + 0.5))
            for position, (_, counts) in enumerate(self.bags):
                tf = counts.get(term, 0)
                if not tf:
                    continue
                normaliser = _K1 * (
                    1.0 - _B + _B * self.lengths[position] / self.average_length
                )
                results[position] += idf * tf * (_K1 + 1.0) / (tf + normaliser)
        return results


def _weighted_bag(skill: Skill) -> tuple[str, ...]:
    """One skill as a retrieval bag, fields repeated by their weight."""
    bag: list[str] = []
    bag.extend(tokenize(skill.name) * 3)
    bag.extend(tokenize(skill.domain) * 2)
    for tag in skill.tags:
        bag.extend(tokenize(tag) * 2)
    for trigger in skill.triggers:
        bag.extend(tokenize(trigger) * 2)
    bag.extend(tokenize(skill.description))
    return tuple(bag)


def _substring_ranking(skills: Sequence[Skill], terms: Sequence[str]) -> list[int]:
    """Skill positions by how many query terms occur in their fields.

    The guard ranking under the BM25 one: a term occurring in a *name*,
    *tag* or *trigger* is evidence even when BM25's document frequency
    arithmetic ranks it slowly, and fusing the two orders is cheaper
    than proving which one is right. Ties keep index order, so the
    fused result is deterministic across machines.
    """
    positions: list[int] = []
    counts: list[int] = []
    for position, skill in enumerate(skills):
        haystack = "\n".join(
            (skill.name, skill.domain, skill.trigger, *skill.tags, skill.description)
        ).lower()
        matched = sum(1 for term in set(terms) if term in haystack)
        if matched:
            positions.append(position)
            counts.append(matched)
    ranked = sorted(
        ((-count, position) for count, position in zip(counts, positions))
    )
    return [position for _, position in ranked]


@functools.lru_cache(maxsize=8)
def _corpus_for(index: SkillIndex) -> _Corpus:
    """The BM25 corpus for *index*, built at most once per index object.

    ``SkillIndex`` is frozen and hashable, so the identity of the cache
    key is the identity of the scan — which is the semantics wanted: an
    augmentor is built per exchange, and every exchange of one
    deployment shares the app's one index. Bounded at eight so a test
    run full of throwaway indices cannot grow it without end; a
    deployment's real index evicts the tests' in one entry.
    """
    bags = [_weighted_bag(skill) for skill in index.skills]
    frequency: dict[str, int] = {}
    for bag in bags:
        for term in set(bag):
            frequency[term] = frequency.get(term, 0) + 1
    lengths = tuple(len(bag) for bag in bags)
    average = (sum(lengths) / len(lengths)) if lengths else 0.0
    return _Corpus(
        bags=tuple((tuple(bag), _counts(bag)) for bag in bags),
        lengths=lengths,
        average_length=average or 1.0,
        document_frequency=frequency,
    )


def _counts(bag: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for term in bag:
        counts[term] = counts.get(term, 0) + 1
    return counts


def _loaded_names(history: Sequence[Message], use_skill: str) -> frozenset[str]:
    """Every skill the conversation already loaded with *use_skill*.

    ``ToolCall.arguments`` is the raw JSON string, and a payload that
    does not parse is skipped rather than raised: the augmentor runs
    inside the turn's critical path, and a malformed historical call —
    which the tool layer would have answered with an error anyway — is
    not allowed to cost the model its discovery block.
    """
    loaded: set[str] = set()
    for message in history:
        if message.role is not Role.ASSISTANT:
            continue
        for call in message.tool_calls:
            if call.name != use_skill:
                continue
            try:
                payload = json.loads(call.arguments or "{}")
            except ValueError:
                continue
            if isinstance(payload, dict):
                name = payload.get("skill_name")
                if isinstance(name, str) and name:
                    loaded.add(name)
    return frozenset(loaded)


def _named_in(text: str, name: str) -> bool:
    """Whether *name* occurs in *text* as a whole skill name.

    Hyphens belong to skill names, so the guards refuse a neighbouring
    letter, digit or hyphen: ``spatial-de`` does not fire on
    ``spatial-deconv`` — that is a different skill's name — and does
    fire on ``use spatial-de please``.
    """
    pattern = rf"(?<![\w-]){re.escape(name)}(?![\w-])"
    return re.search(pattern, text, re.IGNORECASE) is not None


class SkillDiscovery:
    """Appends the skills this request probably wants, as one user message.

    Satisfies the engine's ``TurnAugmentor`` protocol structurally: the
    returned message goes to the sent copy only, never to history, and
    every call re-derives its state from the conversation it is given —
    which is what makes the class safe to rebuild per exchange and
    correct after a compaction.

    :param index: The scan to retrieve over. An empty index disables
        the augmentor outright (the composition root skips it, and the
        class honours the same ruling if handed one anyway).
    :param top_k: The ceiling on hits per block. The score cutoff is
        applied first and may leave slots empty; filling them with
        dubious matches is the failure mode ``min_score`` exists for.
    :param min_score: The fused score a hit must reach. A skill the
        user named by full name bypasses this — a direct reference is
        evidence no score can outvote.
    :param use_skill_tool: The name of the loading tool, both to detect
        that the surface offers it (without it there is nothing to load
        and nothing to say) and to scan history for already-loaded
        skills.
    :param search_tool: The name of the search tool, quoted in the
        block's closing line and in the low-confidence notice.
    :param tracer: Opens one span per actual injection. ``None`` means
        the no-op defined here; the composition root passes the
        deployment's recorder, which matches the same shape.
    """

    __slots__ = (
        "_corpus",
        "_index",
        "_min_score",
        "_search_tool",
        "_top_k",
        "_tracer",
        "_use_skill_tool",
    )

    def __init__(
        self,
        index: SkillIndex,
        *,
        top_k: int = DEFAULT_SKILL_DISCOVERY_TOP_K,
        min_score: float = SKILL_DISCOVERY_MIN_SCORE,
        use_skill_tool: str = "use_skill",
        search_tool: str = "search_skills",
        tracer: "_Tracer | None" = None,
    ) -> None:
        self._index = index
        self._corpus = _corpus_for(index)
        self._top_k = top_k
        self._min_score = min_score
        self._use_skill_tool = use_skill_tool
        self._search_tool = search_tool
        self._tracer = tracer if tracer is not None else _NoopTracer()

    async def augment(
        self,
        history: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
    ) -> tuple[Message, ...]:
        """The discovery block for this call, or nothing.

        Nothing rather than an empty block when the surface offers no
        ``use_skill`` (a sub-agent's narrowed face, or
        ``skills_index=off``), when the conversation holds no user
        message, or when the last user message is too thin to retrieve
        with — "continue" is a request to keep going, not a description
        of a domain, and a block of guesses under it is noise the model
        would have to argue with — or when its terms are all task-meta
        words: "rename the output files" is about the workspace, not
        about metabolomics, and the weak-term gate withholds it the
        same way.

        :param history: The conversation this call will be sent, after
            compaction.
        :param tools: The tool definitions this call will carry.
        :returns: One user message, or an empty tuple.
        """
        if self._top_k <= 0 or self._index.is_empty:
            return ()
        if not any(tool.name == self._use_skill_tool for tool in tools):
            return ()
        request = next(
            (m.content for m in reversed(history) if m.role is Role.USER), None
        )
        if request is None:
            return ()
        terms = tokenize(clean_query(request))
        if len(terms) < 2:
            return ()
        if not _strong_terms(terms):
            return ()

        loaded = _loaded_names(history, self._use_skill_tool)
        order, scores = self._rank(request, terms, loaded)
        if not order:
            self._trace((), (), low_confidence=True)
            return (self._message(self._low_confidence()),)
        body = self._block(order)
        self._trace(order, scores, low_confidence=False)
        return (self._message(body),)

    def _rank(
        self, request: str, terms: Sequence[str], loaded: frozenset[str]
    ) -> tuple[list[int], list[float]]:
        """Skill positions to show, and their fused scores.

        A skill the request names in full is pinned to the front
        whatever its score; the rest are ranked by fused BM25 and
        substring order, cut at ``min_score`` before ``top_k`` is
        applied — an empty block beats a full one built from matches
        nobody believes in. Skills already loaded in this conversation
        are dropped on either path: their bodies are in the context or
        were compacted away, and in the second case the model will ask
        for them again the moment it needs them.
        """
        bm25 = self._corpus.scores(terms)
        ranked_bm25 = sorted(
            (position for position, score in enumerate(bm25) if score > 0.0),
            key=lambda position: (-bm25[position], position),
        )
        ranked_substring = _substring_ranking(self._index.skills, terms)
        fused: dict[int, float] = {}
        for ranking in (ranked_bm25, ranked_substring):
            for rank, position in enumerate(ranking, start=1):
                fused[position] = fused.get(position, 0.0) + 1.0 / (_RRF_K + rank)

        pinned = [
            position
            for position, skill in enumerate(self._index.skills)
            if skill.name not in loaded and _named_in(request, skill.name)
        ]
        scored = sorted(
            (
                (position, score)
                for position, score in fused.items()
                if score >= self._min_score
                and position not in pinned
                and self._index.skills[position].name not in loaded
            ),
            key=lambda pair: (-pair[1], pair[0]),
        )
        remaining = max(0, self._top_k - len(pinned))
        order = pinned + [position for position, _ in scored[:remaining]]
        return order, [fused.get(position, 0.0) for position in order]

    def _block(self, order: Sequence[int]) -> str:
        """The served list: one line per hit, the closing how-to-load."""
        skills = self._index.skills
        lines = [
            f"- {skills[position].name} — "
            f"{skills[position].description[:_DESCRIPTION_LIMIT]}"
            for position in order
        ]
        return (
            '<skill_discovery signal="turn">\n'
            "Skills that may answer this request "
            f"(descriptions truncated to {_DESCRIPTION_LIMIT} chars):\n"
            + "\n".join(lines)
            + f"\nLoad one with {self._use_skill_tool}(name); "
            f"search more with {self._search_tool}(query).\n"
            "</skill_discovery>"
        )

    def _low_confidence(self) -> str:
        """The one-line honest downgrade: nothing matched well."""
        return (
            '<skill_discovery signal="turn">\n'
            "No skill matched this request confidently; if it is a domain "
            f"task, search with `{self._search_tool}` using the domain's own "
            "terminology.\n"
            "</skill_discovery>"
        )

    def _message(self, body: str) -> Message:
        """The block as a user message — the role every injected reminder
        in this codebase takes. There is no precedent for a mid-
        conversation system message, and a user-role block the model can
        quote back beats a privileged one it cannot."""
        return Message(role=Role.USER, content=body)

    def _trace(
        self, order: Sequence[int], scores: Sequence[float], *, low_confidence: bool
    ) -> None:
        """Record one served block. Tool-use spans are *not* opened here."""
        skills = self._index.skills
        self._tracer.start_span(
            SPAN_SKILL_DISCOVERY,
            attributes={
                "skill.discovery.signal": "turn",
                "skill.discovery.hits": ",".join(skills[p].name for p in order),
                "skill.discovery.scores": ",".join(f"{s:.4f}" for s in scores),
                "skill.discovery.low_confidence": low_confidence,
            },
        ).end()
