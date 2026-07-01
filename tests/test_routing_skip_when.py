"""Deterministic routing tests driven by the Skip-when eval snapshot.

ADR 2026-05-11 / ADR 0037: negative routing cases are extracted from each
skill's Skip-when clause (v2 `skill.yaml summary` / v1 `SKILL.md` frontmatter,
read via the same dual-track reader as the extractor) at write-time by an LLM,
then frozen as JSON.  This test reads the snapshot only — it does NOT call any
LLM — so CI is deterministic and offline-safe.

When a skill's description changes, `description_hash` in the snapshot goes stale
and this test fails with guidance to run `make eval-snapshot`.  The maintainer
reviews the regenerated snapshot's diff before commit, which is the
human-in-the-loop step the ADR mandates.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = ROOT / "tests" / "eval" / "skip_when_cases.json"


def _hash_description(description: str) -> str:
    normalised = " ".join(description.split())
    return "sha256:" + hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:32]


def _load_snapshot() -> dict:
    if not SNAPSHOT_PATH.exists():
        pytest.skip(
            f"Snapshot not found at {SNAPSHOT_PATH.relative_to(ROOT)}. "
            "Run `make eval-snapshot DOMAIN=spatial` to generate it."
        )
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _current_descriptions(domain: str) -> dict[str, str]:
    """Skill name -> description via the SAME dual-track reader the extractor
    uses (v2 `skill.yaml summary` / v1 `SKILL.md` frontmatter), so the test's
    hash matches the extractor's and v2/skill.yaml-only skills are covered
    (ADR 0037)."""
    from scripts.extract_skip_when_cases import _load_skill_entries

    return {e.skill: e.description for e in _load_skill_entries(domain)}


# --------------------------------------------------------------------------- #
# Drift detection — guarantees the snapshot reflects current SKILL.md content.
# --------------------------------------------------------------------------- #

def test_snapshot_hashes_match_current_skill_descriptions():
    """If a description changed without re-running the extractor, this fails
    with a clear instruction.  This is the ADR's "human reviews snapshot
    diff" gate enforced as code."""
    snapshot = _load_snapshot()
    domain = snapshot["domain"]
    descriptions = _current_descriptions(domain)
    stale: list[tuple[str, str, str]] = []
    for entry in snapshot["skills"]:
        skill = entry["skill"]
        recorded_hash = entry["description_hash"]
        current_desc = descriptions.get(skill, "")
        if not current_desc:
            stale.append((skill, recorded_hash, "DESCRIPTION_MISSING"))
            continue
        current_hash = _hash_description(current_desc)
        if current_hash != recorded_hash:
            stale.append((skill, recorded_hash, current_hash))
    assert not stale, (
        "Snapshot stale — skill descriptions changed without re-extracting.\n"
        "Run `make eval-snapshot DOMAIN=" + domain + "` and review the diff "
        "before committing.\n"
        f"Stale entries: {stale}"
    )


def test_current_descriptions_dual_track_v2(tmp_path, monkeypatch):
    """The drift check sources descriptions via the SAME dual-track reader as the
    extractor, so a v2 skill.yaml-only skill is covered with its reconstructed
    description (no false DESCRIPTION_MISSING / hash mismatch) — ADR 0037."""
    from scripts import extract_skip_when_cases as extractor
    from omicsclaw.skill.schema import parse_skill_manifest

    monkeypatch.setattr(extractor, "_ROOT", tmp_path)
    sd = tmp_path / "skills" / "spatial" / "sk"
    sd.mkdir(parents=True)
    doc = {
        "schema_version": 2, "id": "sk", "name": "sk", "domain": "spatial",
        "version": "1.0.0",
        "summary": {"load_when": "clustering a spatial AnnData",
                    "skip_when": [{"condition": "single-cell data", "use": "sc-de"}]},
        "runtime": {"language": "python", "entry": "sk.py"},
    }
    (sd / "skill.yaml").write_text(parse_skill_manifest(doc).to_yaml(), encoding="utf-8")

    descriptions = _current_descriptions("spatial")
    assert "sk" in descriptions
    assert descriptions["sk"].startswith("Load when clustering a spatial AnnData")
    # the test's own hash matches the extractor's entry hash (same reader + fn)
    assert _hash_description(descriptions["sk"]) == \
        extractor._hash_description(descriptions["sk"])


def test_snapshot_has_no_silent_extraction_failures():
    """If the LLM returned non-JSON for some skill, the extractor marks
    that entry with extraction_failed=true.  We surface those here so
    they don't masquerade as "this skill has no Skip-when redirects"
    and silently shrink eval coverage.  Operator must either re-run
    extraction or annotate the failure as expected before commit."""
    snapshot = _load_snapshot()
    failed = [
        e["skill"] for e in snapshot["skills"]
        if e.get("extraction_failed")
    ]
    assert not failed, (
        f"Snapshot has {len(failed)} skill(s) with extraction_failed=true: "
        f"{failed}.  Re-run `make eval-snapshot DOMAIN={snapshot['domain']}` "
        "(LLM may have been transiently broken) or investigate the "
        "`error` field on each failed entry."
    )


# --------------------------------------------------------------------------- #
# Case-level assertions — driven by the snapshot, executed against the
# real capability resolver.  Each case turns into a parametrized test;
# adding a Skip-when clause → re-extract → tests grow automatically.
# --------------------------------------------------------------------------- #

def _collect_cases() -> list[tuple[str, str, dict]]:
    """Flatten snapshot into a list of (case_id, host_skill, case_dict)."""
    if not SNAPSHOT_PATH.exists():
        return []
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    flat: list[tuple[str, str, dict]] = []
    for entry in snapshot["skills"]:
        skill = entry["skill"]
        for i, case in enumerate(entry.get("cases", [])):
            flat.append((f"{skill}#{i}", skill, case))
    return flat


_CASES = _collect_cases()


@pytest.mark.parametrize(
    "case_id, host_skill, case",
    _CASES,
    ids=[cid for cid, _, _ in _CASES] or ["no_cases_yet"],
)
def test_negative_case_does_not_route_to_host_skill(case_id, host_skill, case):
    """Negative half: the host skill MUST NOT be the chosen route for this
    trigger.  This catches off-target activation — Perplexity's
    "action at distance" failure mode."""
    if not _CASES:
        pytest.skip("Snapshot has no cases yet — run extractor with LLM_API_KEY set")
    if case.get("manual_override"):
        pytest.skip(
            f"manual_override: {case.get('override_reason', 'no reason given')}"
        )

    from omicsclaw.skill.capability_resolver import resolve_capability

    result = resolve_capability(query=case["trigger"])
    chosen = getattr(result, "chosen_skill", None) or (
        result.get("chosen_skill") if isinstance(result, dict) else None
    )
    assert chosen != case["must_not_pick"], (
        f"[{case_id}] Off-target activation: query {case['trigger']!r} "
        f"routed to {case['must_not_pick']!r} but Skip-when clause says it "
        f"should not.  Expected sibling: {case.get('expected_pick')!r}."
    )


@pytest.mark.parametrize(
    "case_id, host_skill, case",
    [(cid, h, c) for cid, h, c in _CASES if c.get("expected_pick")],
    ids=[cid for cid, _, c in _CASES if c.get("expected_pick")] or ["no_redirect_cases"],
)
def test_redirect_case_routes_to_expected_sibling(case_id, host_skill, case):
    """Positive half: when Skip-when explicitly names a sibling skill (the
    common case), routing MUST land on that sibling.  Cases without a
    sibling (hard preconditions) are excluded from this test."""
    if not _CASES:
        pytest.skip("Snapshot has no cases yet")
    if case.get("manual_override"):
        pytest.skip(f"manual_override: {case.get('override_reason', 'no reason given')}")

    from omicsclaw.skill.capability_resolver import resolve_capability

    result = resolve_capability(query=case["trigger"])
    chosen = getattr(result, "chosen_skill", None) or (
        result.get("chosen_skill") if isinstance(result, dict) else None
    )
    assert chosen == case["expected_pick"], (
        f"[{case_id}] Skip-when redirect failed: query {case['trigger']!r} "
        f"should have routed to {case['expected_pick']!r} but got {chosen!r}."
    )
