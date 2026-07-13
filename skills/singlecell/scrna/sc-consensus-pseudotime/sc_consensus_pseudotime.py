"""sc-consensus-pseudotime — thin shim over the generic consensus entry (ADR 0016/0031).

Verified **continuous** (rank-gauge) consensus over pseudotime *methods*: members
fan out `sc-pseudotime --method <m>` (dpt/palantir/via) from a shared root, the
runtime rank-normalises + direction-aligns their per-cell pseudotimes, scores them
by mean pairwise Spearman, and aggregates a consensus pseudotime (median/weighted)
with per-cell dispersion. All orchestration lives in
``omicsclaw.runtime.consensus.run``; this binds the flavour name. See
``CONSENSUS_SOURCES["sc-consensus-pseudotime"]``.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Bootstrap sys.path so `omicsclaw` resolves on direct invocation
# (`python sc_consensus_pseudotime.py --help`) without an editable install.
_HERE = Path(__file__).resolve()
for _candidate in _HERE.parents:
    if (_candidate / "omicsclaw" / "__init__.py").exists():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

from omicsclaw.runtime.consensus.run import main as _run_main  # noqa: E402

SKILL_NAME = "sc-consensus-pseudotime"
SKILL_VERSION = "0.1.0"
SOURCE = "sc-consensus-pseudotime"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    return _run_main(["--source", SOURCE, *argv])


if __name__ == "__main__":
    raise SystemExit(main())
