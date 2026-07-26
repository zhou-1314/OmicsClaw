"""Package-level CLI entrypoint for OmicsClaw.

Target of the ``omicsclaw`` and ``oc`` console scripts declared in
``pyproject.toml``. The CLI body lives in :mod:`omicsclaw.surfaces.cli._main`;
this module only re-exports its ``main`` so the entrypoint path in
``[project.scripts]`` stays stable.

History: this used to locate the repo-root ``omicsclaw.py`` on disk — walking
up from ``__file__``, then from the working directory, with an
``OMICSCLAW_CLI_PATH`` override — and load it through ``importlib``. That only
ever worked inside a source checkout, because ``omicsclaw.py`` is not part of
the installed distribution (see ``[tool.setuptools.packages.find]`` — it ships
``omicsclaw`` and ``skills``, not top-level modules). Every pip / npm /
bundled-runtime install therefore raised ``FileNotFoundError`` unless the user
happened to be standing in a checkout. Moving the body into the package removed
the search entirely, along with the ``OMICSCLAW_CLI_PATH`` escape hatch that
existed only to work around it.
"""

from __future__ import annotations

from omicsclaw.surfaces.cli._main import main

__all__ = ["main"]
