"""A harness for benchmarking agents on tasks with a known answer.

A campaign is described by a manifest (:mod:`.manifest`). Each of its runs
gets a new workspace holding only the case's public files (:mod:`.stage`),
is carried out by one agent process started through an adapter
(:mod:`.adapters`, :mod:`.process`), and is recorded with how it ended
(:mod:`.outcome`), what it cost and whether it reached for anything it
should not have (:mod:`.access`). Finished runs are then graded by
deterministic graders against oracles the agent never saw (:mod:`.grade`).

Command line::

    python -m omicsclaw.bench plan  <manifest>
    python -m omicsclaw.bench stage <manifest> --cases <dir> --out <dir>
    python -m omicsclaw.bench run   <manifest> --cases <dir> --out <dir>
    python -m omicsclaw.bench grade <manifest> --cases <dir> --out <dir>

The harness reaches an agent only through its process and the files that
process writes. This package imports nothing else from ``omicsclaw``, and
nothing in ``omicsclaw`` imports it.
"""

from .grade import Control, Grade, Grader, GraderError, Submission, grade_campaign
from .layout import Campaign
from .manifest import Manifest, ManifestError, load_manifest
from .run import run_campaign, stage_campaign

__all__ = [
    "Campaign",
    "Control",
    "Grade",
    "Grader",
    "GraderError",
    "Manifest",
    "ManifestError",
    "Submission",
    "grade_campaign",
    "load_manifest",
    "run_campaign",
    "stage_campaign",
]
