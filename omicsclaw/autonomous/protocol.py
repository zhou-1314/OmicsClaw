"""Per-step LLM response contract for the Autonomous Code Mini-Agent.

ADR 0032 §8 requires a ``Purpose / Reasoning / Next Goal / Code`` markdown turn
for every mini-agent step, ported from the SpatialClaw reference. This module
parses and validates that contract and statically detects the ``ReturnAnswer``
sentinel so the loop can recognise a finished run.

It is intentionally dependency-free and side-effect-free so it can be unit
tested without a kernel.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import re

# Canonical headers, in display order. Each maps to the field on MiniAgentTurn.
_REQUIRED_SECTIONS = ("purpose", "reasoning", "next_goal", "code")

_HEADER_PATTERNS = {
    "purpose": r"\*\*\s*Purpose\s*\*\*",
    "reasoning": r"\*\*\s*Reasoning\s*\*\*",
    "next_goal": r"\*\*\s*Next[ _]?Goal\s*\*\*",
    "code": r"\*\*\s*Code\s*\*\*",
}

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_CODE_FENCE = re.compile(r"```(?:python|py)?\s*(.*?)\s*```", re.DOTALL)


class TurnFormatError(ValueError):
    """Raised when an LLM turn does not satisfy the mini-agent contract.

    Carries ``problems`` so the loop can feed the exact validation failures
    back to the model as an evidence-bound retry message.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems) or "invalid mini-agent turn")


@dataclass(slots=True)
class MiniAgentTurn:
    """One validated mini-agent step parsed from an LLM response."""

    purpose: str
    reasoning: str
    next_goal: str
    code: str
    raw: str

    @property
    def calls_return_answer(self) -> bool:
        return code_calls_return_answer(self.code)

    @property
    def return_answer_literal(self) -> str | None:
        return extract_return_answer_literal(self.code)


def strip_thinking(text: str) -> str:
    """Remove ``<think>...</think>`` spans emitted by reasoning models."""
    return _THINK_BLOCK.sub("", text or "").strip()


def parse_turn(text: str) -> MiniAgentTurn:
    """Parse and validate a mini-agent turn.

    Raises :class:`TurnFormatError` listing every missing or empty required
    section, and a code-fence / syntax problem when the ``Code`` block is not a
    parseable Python snippet.
    """
    cleaned = strip_thinking(text)
    sections = _split_sections(cleaned)

    problems: list[str] = []
    for name in _REQUIRED_SECTIONS:
        if not sections.get(name, "").strip():
            problems.append(f"missing or empty **{_display(name)}** section")

    code = ""
    if sections.get("code", "").strip():
        code = _extract_code(sections["code"])
        if not code.strip():
            problems.append("**Code** section has no ```python fenced block")
        else:
            try:
                ast.parse(code)
            except SyntaxError as exc:
                problems.append(f"**Code** has a syntax error: {exc.msg} (line {exc.lineno})")

    if problems:
        raise TurnFormatError(problems)

    return MiniAgentTurn(
        purpose=sections["purpose"].strip(),
        reasoning=sections["reasoning"].strip(),
        next_goal=sections["next_goal"].strip(),
        code=code.strip(),
        raw=cleaned,
    )


def code_calls_return_answer(code: str) -> bool:
    """True when *code* statically contains a ``ReturnAnswer(...)`` call."""
    return _find_return_answer_call(code) is not None


def extract_return_answer_literal(code: str) -> str | None:
    """Return the literal string argument of ``ReturnAnswer(...)`` if present.

    Returns ``None`` when there is no call, or when the argument is a computed
    expression rather than a string/f-string-free literal — in that case the
    answer must be read from the live kernel sentinel instead.
    """
    call = _find_return_answer_call(code)
    if call is None:
        return None
    if call.args:
        first = call.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    for kw in call.keywords:
        if kw.arg in {"text", "answer"} and isinstance(kw.value, ast.Constant):
            if isinstance(kw.value.value, str):
                return kw.value.value
    return None


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #


def _display(name: str) -> str:
    return {"next_goal": "Next Goal"}.get(name, name.capitalize())


def _split_sections(text: str) -> dict[str, str]:
    """Slice the text into the four labelled sections by their bold headers.

    Headers may appear in any order; each section runs until the next known
    header. Unknown text before the first header is ignored.
    """
    # Find every header occurrence with its span and field name.
    hits: list[tuple[int, int, str]] = []
    for name, pattern in _HEADER_PATTERNS.items():
        for match in re.finditer(pattern + r"\s*:?", text, re.IGNORECASE):
            hits.append((match.start(), match.end(), name))
    hits.sort()

    sections: dict[str, str] = {}
    for index, (_start, end, name) in enumerate(hits):
        next_start = hits[index + 1][0] if index + 1 < len(hits) else len(text)
        # Keep the first occurrence of each header if duplicated.
        sections.setdefault(name, text[end:next_start])
    return sections


def _extract_code(code_section: str) -> str:
    fence = _CODE_FENCE.search(code_section)
    if fence:
        return fence.group(1)
    # Tolerate a code section that is bare code without a fence.
    return code_section


def _find_return_answer_call(code: str) -> ast.Call | None:
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "ReturnAnswer":
                return node
            if isinstance(func, ast.Attribute) and func.attr == "ReturnAnswer":
                return node
    return None


__all__ = [
    "MiniAgentTurn",
    "TurnFormatError",
    "code_calls_return_answer",
    "extract_return_answer_literal",
    "parse_turn",
    "strip_thinking",
]
