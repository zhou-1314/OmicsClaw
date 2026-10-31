# SPDX-License-Identifier: Apache-2.0
"""``python -m omicsclaw.connectors.bio <name> [--list]``."""

from __future__ import annotations

import json
import sys

from . import CONNECTOR_MODULES, connector_module
from ._server import serve


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(
            "usage: python -m omicsclaw.connectors.bio <name> [--list]\n"
            f"connectors: {', '.join(CONNECTOR_MODULES)}",
            file=sys.stderr,
        )
        return 0 if args else 2
    name = args[0]
    if name not in CONNECTOR_MODULES:
        print(f"unknown connector {name!r}; choose from {', '.join(CONNECTOR_MODULES)}", file=sys.stderr)
        return 2
    module = connector_module(name)
    tools = module.build_tools()
    if "--list" in args[1:]:
        print(json.dumps([tool.listing() for tool in tools], indent=2))
        return 0
    return serve(name, tools, instructions=getattr(module, "INSTRUCTIONS", ""))


if __name__ == "__main__":
    raise SystemExit(main())
