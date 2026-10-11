"""The ``search_skills`` tool: find skills by keyword, on demand.

The system prompt carries the catalogue — names under ``compact``,
names and descriptions under ``full`` — and this tool is how the model
asks it a question, rather than reading all of it looking for one row.
``/skills`` at a prompt and this tool share
:meth:`~omicsclaw.skills.index.SkillIndex.search` as their one code
path, which is why a trigger keyword reaches a skill whose name does
not contain it.
"""

from __future__ import annotations

from omicsclaw.tools.base import ApprovalMode, RiskLevel, ToolPolicy
from omicsclaw.tools.function_tool import FunctionTool

from .index import SkillIndex

__all__ = ["SEARCH_SKILL_SCHEMA", "SEARCH_SKILL_TOOL_NAME", "search_skill_tool"]

SEARCH_SKILL_TOOL_NAME = "search_skills"

_DESCRIPTION = (
    "Search the skill catalogue by keyword and get back each match's "
    "name, domain and description. Call this before carrying out a "
    "domain task when the catalogue in the system prompt does not make "
    "the right skill obvious: a keyword, a method name or a domain term "
    "is enough. Load the full instructions of a result with "
    "`use_skill`."
)

SEARCH_SKILL_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Keywords to look for in each skill's name, domain, tags "
                "and triggers. An empty string lists the whole catalogue."
            ),
        },
        "domain": {
            "type": "string",
            "description": (
                "Restrict the results to one domain, as spelled in the "
                "skill catalogue (for example `spatial`). Omit to search "
                "every domain."
            ),
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

_POLICY = ToolPolicy(
    risk_level=RiskLevel.LOW,
    approval_mode=ApprovalMode.AUTO,
    read_only=True,
    concurrency_safe=True,
    allowed_in_background=True,
    tags=frozenset({"skills", "inspection"}),
)
"""Declared rather than defaulted, for the same reason as ``use_skill``'s.

:class:`~omicsclaw.tools.base.ToolPolicy` defaults to ``HIGH`` + ``ASK``,
and approval fails closed when no channel is bound, so omitting a policy
would put a prompt in front of every search. The tool reads only
metadata already indexed at load time, and reads nothing the caller
names — unlike ``use_skill`` it does not even open a file.
"""


def search_skill_tool(index: SkillIndex) -> FunctionTool:
    """Build a ``search_skills`` tool bound to *index*.

    The index is bound, not copied, so the tool answers from the scan
    the deployment started with; a mid-run rescan reaches it only
    through a new tool, exactly as with ``use_skill``.

    No ``policy`` parameter: a deployment that disagrees with the default
    passes its own to
    :meth:`~omicsclaw.tools.registry.ToolRegistry.register`.
    """

    def run(query: str, domain: str = "") -> str:
        return _search(index, query, domain)

    return FunctionTool(
        SEARCH_SKILL_TOOL_NAME,
        _DESCRIPTION,
        run,
        parameters=SEARCH_SKILL_SCHEMA,
        policy=_POLICY,
    )


def _search(index: SkillIndex, query: str, domain: str) -> str:
    """Every hit as one loadable entry, or how to retry with better words.

    A *domain* that names no indexed skill narrows the results to
    nothing, which is reported the same way an empty *query* match is:
    the model can correct either argument from the message.
    """
    if not index.skills:
        return "The skill index is empty."

    wanted_domain = domain.strip().lower()
    hits = index.search(query)
    if wanted_domain:
        hits = tuple(skill for skill in hits if skill.domain.lower() == wanted_domain)
    if not hits:
        return _missed(index, query, domain)

    lines = [
        f"- {skill.name} ({skill.domain}): {skill.description}\n"
        f"  load with use_skill"
        for skill in hits
    ]
    return "\n".join(lines)


def _missed(index: SkillIndex, query: str, domain: str) -> str:
    """What to tell the model when nothing matched.

    Close names come from
    :meth:`~omicsclaw.skills.index.SkillIndex.close_names`, the same
    helper ``use_skill`` offers a misspelled ``skill_name``. Word
    overlap is blind to synonyms — the honest fix is to say so and ask
    for the domain's terminology, which the model has and a substring
    matcher does not.
    """
    target = domain.strip() or query.strip()
    near = index.close_names(target) if target else ()
    lines = [f"No skills matched query {query.strip()!r}"]
    if domain.strip():
        lines[0] += f" in domain {domain.strip()!r}"
    if near:
        lines.append(f"Did you mean: {', '.join(near)}?")
    lines.append(
        "Retry with a domain term (a method, an assay, a data kind) or a "
        "word from a skill's tags or triggers."
    )
    return " ".join(lines)
