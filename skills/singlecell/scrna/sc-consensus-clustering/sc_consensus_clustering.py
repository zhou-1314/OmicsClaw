"""sc-consensus-clustering — thin shim over the generic consensus entry (ADR 0016).

All orchestration (resolution-sweep planning, fan-out, scoring, BC selection,
operator, report) lives in ``omicsclaw.runtime.consensus.run``; this binds the
flavour name so ``run_skill`` and the CLI can invoke it. See
``CONSENSUS_SOURCES["sc-consensus-clustering"]`` for the declarative contract.
"""

from __future__ import annotations

import sys

from omicsclaw.runtime.consensus.run import main as _run_main

SOURCE = "sc-consensus-clustering"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    return _run_main(["--source", SOURCE, *argv])


if __name__ == "__main__":
    raise SystemExit(main())
