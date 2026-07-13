"""sc-consensus-clustering — thin shim over the generic consensus entry (ADR 0016).

All orchestration (resolution-sweep planning, fan-out, scoring, BC selection,
operator, report) lives in ``omicsclaw.runtime.consensus.run``; this binds the
flavour name so ``run_skill`` and the CLI can invoke it. See
``CONSENSUS_SOURCES["sc-consensus-clustering"]`` for the declarative contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Bootstrap sys.path so `omicsclaw` resolves on direct invocation
# (`python sc_consensus_clustering.py --help`) without an editable install.
_HERE = Path(__file__).resolve()
for _candidate in _HERE.parents:
    if (_candidate / "omicsclaw" / "__init__.py").exists():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

from omicsclaw.runtime.consensus.run import main as _run_main  # noqa: E402

SKILL_NAME = "sc-consensus-clustering"
SKILL_VERSION = "0.1.0"
SOURCE = "sc-consensus-clustering"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    return _run_main(["--source", SOURCE, *argv])


if __name__ == "__main__":
    raise SystemExit(main())
