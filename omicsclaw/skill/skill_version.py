"""Keep a skill script's ``SKILL_VERSION`` constant in sync with ``skill.yaml`` (ADR 0037).

A v2 leaf script declares ``SKILL_VERSION = "x.y.z"`` and emits it into
``result.json`` + the visualization contract. ``skill.yaml.version`` is the single
source of truth, so the constant must equal it — otherwise downstream consumers
see a version that disagrees with the manifest (a real, widespread v1 drift:
many scripts were never bumped when the frontmatter version was). This module
reads and rewrites only that one constant line; everything else is untouched.
"""

from __future__ import annotations

import re

# Matches a top-level `SKILL_VERSION = "x"` / `'x'` assignment, capturing the
# prefix, the quote char, and the value so a rewrite preserves quoting/spacing.
_VERSION_RE = re.compile(
    r'^(?P<prefix>SKILL_VERSION\s*=\s*)(?P<q>["\'])(?P<val>[^"\']*)(?P=q)',
    re.MULTILINE,
)


def read_script_version(script_text: str) -> str | None:
    """Return the script's declared ``SKILL_VERSION``, or None if it has none."""
    m = _VERSION_RE.search(script_text)
    return m.group("val") if m else None


def sync_script_version(script_text: str, target_version: str) -> tuple[str, bool]:
    """Rewrite ``SKILL_VERSION`` to ``target_version``.

    Returns ``(new_text, changed)``. ``changed`` is False when there is no
    ``SKILL_VERSION`` constant (nothing to sync — e.g. consensus shims) or it
    already equals the target.
    """
    m = _VERSION_RE.search(script_text)
    if m is None or m.group("val") == target_version:
        return script_text, False
    new_text = (
        script_text[: m.start()]
        + m.group("prefix")
        + m.group("q")
        + target_version
        + m.group("q")
        + script_text[m.end():]
    )
    return new_text, True
