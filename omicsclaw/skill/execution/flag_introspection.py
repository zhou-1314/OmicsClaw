"""Derive the CLI flags a skill actually accepts, from the skill's own script.

Single source of truth for "which ``--flags`` are legitimate" — consumed by
both the runtime gate (``argv_builder.filter_forwarded_args`` via the registry)
and ``scripts/skill_lint.py``.  Before ADR 0041 every skill hand-listed this in
``skill.yaml``'s ``allowed_extra_flags`` and a lint enforced it equalled the
script's argparse surface — a redundant mirror kept in sync by hand.  Now the
runtime derives it directly and ``allowed_extra_flags`` survives only as an
optional *narrowing override* (see :func:`effective_allowed_flags`).

Two flag sources exist:

* **leaf / default** — the flags the script declares via ``add_argument``.
* **consensus** (ADR 0016) — a thin shim delegates to the shared
  ``omicsclaw.runtime.consensus.run`` parser, so the accepted flags live there,
  not in the shim.  Consensus skills expose a hand-picked *subset* of those
  flags, so their ``allowed_extra_flags`` is genuine, non-derivable information
  and is always kept.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

# Framework-injected flags the runner resolves itself; a skill never needs to
# (and must not) re-declare them.  Mirrors ``schema.RESERVED_FLAGS`` and
# ``argv_builder._BLOCKED_FLAGS``.
RUNNER_BLOCKED_FLAGS = frozenset({"--input", "--output", "--demo"})

# Locate each ``add_argument(`` call so we can scan its body for every
# ``--flag`` literal — handles short+long pairs like ``add_argument("-m",
# "--method")`` and multi-line calls.  Requires a literal ``--`` so single-dash
# short flags (e.g. ``-m``) are correctly NOT captured.
_ADD_ARGUMENT_OPEN_RE = re.compile(r"add_argument\s*\(")
_FLAG_LITERAL_RE = re.compile(r"""["'](--[\w-]+)["']""")


def extract_argparse_flags(script_text: str) -> set[str]:
    """Find every ``--flag`` literal inside an ``add_argument(...)`` call body.

    Walks the source balancing parens so ``default=foo(bar)`` and similar
    nested calls don't truncate the body early.
    """
    flags: set[str] = set()
    i = 0
    n = len(script_text)
    while i < n:
        match = _ADD_ARGUMENT_OPEN_RE.search(script_text, i)
        if not match:
            break
        body_start = match.end()
        depth = 1
        j = body_start
        while j < n and depth > 0:
            if script_text[j] == "(":
                depth += 1
            elif script_text[j] == ")":
                depth -= 1
            j += 1
        body = script_text[body_start : j - 1]
        for fm in _FLAG_LITERAL_RE.finditer(body):
            flags.add(fm.group(1))
        i = j
    return flags


def consensus_parser_flags() -> set[str] | None:
    """Long-option flags accepted by the shared consensus run parser.

    Returns ``None`` when the consensus runtime cannot be imported (optional
    dependency surface), so callers can degrade rather than hard-fail.
    """
    try:
        from omicsclaw.runtime.consensus.run import _build_parser

        parser = _build_parser()
    except Exception:
        return None
    return {
        opt
        for action in parser._actions
        for opt in action.option_strings
        if opt.startswith("--")
    }


def derive_accepted_flags(skill_dir: Path, script_name: str, skill_type: str) -> set[str]:
    """Compute the flags a skill accepts, minus the runner-blocked trio.

    ``consensus`` skills resolve against the shared consensus parser; every
    other type reads the co-located script's argparse surface.  Returns an
    empty set when the source is unavailable (missing script / un-importable
    consensus runtime) — the gate then fails closed, dropping all extra flags.
    """
    if skill_type == "consensus":
        # A consensus shim's accepted surface is a hand-curated *subset* of the
        # shared run parser (the parser also exposes --help/--source and every
        # other flavour's flags). It cannot be derived, so it MUST be declared
        # in ``allowed_extra_flags`` (skill_lint enforces this). With no override
        # we fail *closed* — return empty so the gate drops all extra flags —
        # rather than fail *open* by exposing the whole parser.
        return set()

    if not script_name:
        return set()
    script_path = skill_dir / script_name
    if not script_path.exists():
        return set()
    try:
        text = script_path.read_text(encoding="utf-8")
    except OSError:
        return set()
    return extract_argparse_flags(text) - RUNNER_BLOCKED_FLAGS


def effective_allowed_flags(
    declared: Iterable[str] | None,
    skill_dir: Path,
    script_name: str,
    skill_type: str,
) -> set[str]:
    """Resolve the flag allow-list the runtime gate should enforce.

    A non-empty ``declared`` (``skill.yaml``'s ``allowed_extra_flags``) is an
    explicit *narrowing override* and wins verbatim — this is how consensus
    skills expose a curated subset of the parser's flags.  When ``declared`` is
    empty or absent (the common leaf case after ADR 0041) the accepted flags
    are derived from the script.
    """
    declared_set = {f for f in (declared or ()) if f}
    if declared_set:
        return declared_set
    return derive_accepted_flags(skill_dir, script_name, skill_type)


def effective_allowed_flags_from_script_text(
    declared: Iterable[str] | None,
    script_text: str,
    skill_type: str,
) -> set[str]:
    """Same override-else-derive rule as :func:`effective_allowed_flags`, but for
    callers holding the script source in memory before it is on disk (the
    scaffolder writes ``parameters.md`` before the script). Consensus fails
    closed — it must declare its curated subset.
    """
    declared_set = {f for f in (declared or ()) if f}
    if declared_set:
        return declared_set
    if skill_type == "consensus":
        return set()
    return extract_argparse_flags(script_text) - RUNNER_BLOCKED_FLAGS


def params_dump_with_effective_flags(
    params_dump: dict,
    skill_dir: Path,
    script_name: str,
    skill_type: str,
) -> dict:
    """A manifest ``parameters`` dump with ``allowed_extra_flags`` resolved.

    Replaces the (now usually empty) declared list with the effective
    derived-or-override set so ``references/parameters.md`` documents the flags
    the runtime gate actually accepts, not the empty override field.
    """
    out = dict(params_dump)
    out["allowed_extra_flags"] = sorted(
        effective_allowed_flags(
            params_dump.get("allowed_extra_flags"), skill_dir, script_name, skill_type
        )
    )
    return out
