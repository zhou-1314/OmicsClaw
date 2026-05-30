"""consensus-domains — thin shim over the generic consensus entry (ADR 0016).

All orchestration (fan-out, scoring, BC selection, operator, report) lives in
``omicsclaw.runtime.consensus.run``; this binds the flavour name so ``run_skill``
and the CLI can invoke it. See ``CONSENSUS_SOURCES["consensus-domains"]`` for the
declarative contract.
"""

from __future__ import annotations

import sys

from omicsclaw.runtime.consensus.run import main as _run_main

SOURCE = "consensus-domains"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    return _run_main(["--source", SOURCE, *argv])


if __name__ == "__main__":
    raise SystemExit(main())
