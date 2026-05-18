"""Narrative (B-path) consensus — LLM-mediated synthesis of N free-form skill reports.

Output is marked "exploratory" and routed to the
``analysis://exploratory/<run_id>`` graph-memory namespace per ADR 0010.
NOT for verified scientific claims; use the typed (A) path for those.
"""

from omicsclaw.runtime.consensus.narrative.extractor import (
    MemberExtraction,
    extract_member_findings,
)
from omicsclaw.runtime.consensus.narrative.synthesizer import (
    NarrativeReport,
    synthesize_narrative,
)

__all__ = [
    "MemberExtraction",
    "NarrativeReport",
    "extract_member_findings",
    "synthesize_narrative",
]
