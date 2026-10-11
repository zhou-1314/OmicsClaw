"""The closed alphabets environment and package names are held to.

One implementation of a security rule two layers need: the tools leaf
(``manage_environments`` validates what the model sends) and the
Desktop jobs plane (``env_install`` validates what a client posted).
A name that becomes a directory under the env root, or a distribution
name that reaches an install command line, must not be able to escape
either — no path separators, no ``..``, no specifiers, no URLs, no
shell metacharacters. Holding the patterns here (the schema package is
the one place every layer may import) means the two validators cannot
drift apart on the day one of them is tightened.

Standard library only, like the rest of this package.
"""

from __future__ import annotations

import re

__all__ = [
    "ENV_NAME_PATTERN",
    "PACKAGE_NAME_PATTERN",
]

ENV_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
"""A named environment: one word that becomes ``<env root>/<name>``.
The first character is alnum so ``.``-hidden names and ``..`` are both
refused outright."""

PACKAGE_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
"""A bare distribution name as an install command line may see it:
letters, digits, dots, underscores, dashes — no specifiers (``>=``),
no extras (``[]``), no URL schemes, no whitespace, nothing a shell
would reinterpret."""
