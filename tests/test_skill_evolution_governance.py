from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import threading

import pytest
import yaml

import omicsclaw.skill.evolution as evolution_module
import omicsclaw.skill.evolution_governance as governance_module
from omicsclaw.skill.evolution import (
    EvolutionProposalStore,
    SkillHealthLedger,
    SkillRunEvent,
    compute_execution_source_hash,
)
from omicsclaw.skill.evolution_governance import (
    EvolutionRecoveryJournal,
    EvolutionRevalidationError,
    RegistryProjectionAdapter,
    SkillEvolutionGovernance,
)
from omicsclaw.skill.registry import OmicsRegistry
from omicsclaw.skill.schema import load_skill_yaml
from omicsclaw.skill.skill_md import render_skill_md


def _write_skill(
    root: Path,
    *,
    level: str = "smoke-only",
    skill_id: str = "evolution-test",
    skill_type: str = "leaf",
    domain: str = "spatial",
    subdomain: str | None = None,
) -> Path:
    skill_dir = root / domain
    if subdomain:
        skill_dir /= subdomain
    skill_dir /= skill_id
    skill_dir.mkdir(parents=True)
    script_name = skill_id.replace("-", "_") + ".py"
    (skill_dir / script_name).write_text(
        "if __name__ == '__main__':\n    pass\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 2,
        "id": skill_id,
        "name": skill_id,
        "domain": domain,
        "version": "1.2.3",
        "summary": {
            "load_when": "testing skill evolution governance",
            "skip_when": [
                {
                    "condition": "not exercising governance behavior",
                    "use": "another test fixture",
                }
            ],
            "trigger_keywords": ["evolution test"],
        },
        "runtime": {"entry": script_name},
        "type": skill_type,
        "lifecycle": {"status": "mvp"},
        "validation": {"level": level},
    }
    path = skill_dir / "skill.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return path


def _hash(path: Path) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write_gotcha_skill_md(manifest: Path) -> Path:
    path = manifest.with_name("SKILL.md")
    narrative = (
        "## When to use\n\nUse for tests.\n\n"
        "## Flow\n\n1. Run the test entry.\n\n"
        "## Gotchas\n\n- _None yet — append as failure modes are reported._\n\n"
        "## Key CLI\n\n`python evolution_test.py --demo`\n\n"
        "## See also\n\n- None.\n"
    )
    path.write_text(
        render_skill_md(load_skill_yaml(manifest), narrative),
        encoding="utf-8",
    )
    return path


def _skills_root_for_manifest(manifest: Path) -> Path:
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    domain = str(raw["domain"])
    for ancestor in manifest.parents:
        if ancestor.name == domain:
            return ancestor.parent
    raise AssertionError(f"test manifest is not below its domain: {manifest}")


def _event(
    event_id: str,
    manifest: Path,
    *,
    evidence_kind: str = "demo",
    outcome: str = "succeeded",
    error_kind: str = "none",
    execution_fingerprint: str | None = None,
    skill_id: str = "evolution-test",
    evidence_refs: list[str] | None = None,
) -> SkillRunEvent:
    return SkillRunEvent(
        event_id=event_id,
        occurred_at="2026-07-15T00:00:00+00:00",
        run_id="",
        skill_id=skill_id,
        skill_version="1.2.3",
        skill_hash=_hash(manifest),
        environment_id="env:test",
        outcome=outcome,
        error_kind=error_kind,
        exit_code=0 if outcome == "succeeded" else 1,
        duration_seconds=1.0,
        evidence_kind=evidence_kind,
        execution_fingerprint=execution_fingerprint or f"execution:{event_id}",
        source_hash=compute_execution_source_hash(
            manifest.parent / (skill_id.replace("-", "_") + ".py"),
            skills_root=_skills_root_for_manifest(manifest),
        ),
        evidence_refs=evidence_refs or [],
    )


class _ExecutionAdapter:
    def __init__(self, error: str = "") -> None:
        self.error = error
        self.calls: list[str] = []

    def validate_demo(self, skill_id: str) -> None:
        self.calls.append(skill_id)
        if self.error:
            raise EvolutionRevalidationError(self.error)

    def validate_demo_defect(self, skill_id: str) -> None:
        self.calls.append(f"defect:{skill_id}")
        if self.error:
            raise EvolutionRevalidationError(self.error)


class _ConcurrentEditExecutionAdapter(_ExecutionAdapter):
    def __init__(self, manifest: Path) -> None:
        super().__init__()
        self.manifest = manifest

    def validate_demo(self, skill_id: str) -> None:
        super().validate_demo(skill_id)
        raw = yaml.safe_load(self.manifest.read_text(encoding="utf-8"))
        raw["summary"]["tags"] = ["edited-during-demo"]
        self.manifest.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


class _SourceEditExecutionAdapter(_ExecutionAdapter):
    def __init__(self, source: Path) -> None:
        super().__init__()
        self.source = source

    def validate_demo(self, skill_id: str) -> None:
        super().validate_demo(skill_id)
        self.source.write_text("REVISION = 2\n", encoding="utf-8")

    def validate_demo_defect(self, skill_id: str) -> None:
        super().validate_demo_defect(skill_id)
        self.source.write_text("REVISION = 2\n", encoding="utf-8")


class _ReplacementEditExecutionAdapter(_ExecutionAdapter):
    def __init__(self, replacement_manifest: Path) -> None:
        super().__init__()
        self.replacement_manifest = replacement_manifest

    def validate_demo(self, skill_id: str) -> None:
        super().validate_demo(skill_id)
        raw = yaml.safe_load(self.replacement_manifest.read_text(encoding="utf-8"))
        raw["summary"]["tags"] = ["changed-during-revalidation"]
        self.replacement_manifest.write_text(
            yaml.safe_dump(raw, sort_keys=False),
            encoding="utf-8",
        )


class _ReplacementSourceEditExecutionAdapter(_ExecutionAdapter):
    def __init__(self, replacement_source: Path) -> None:
        super().__init__()
        self.replacement_source = replacement_source

    def validate_demo(self, skill_id: str) -> None:
        super().validate_demo(skill_id)
        self.replacement_source.write_text("REVISION = 2\n", encoding="utf-8")


class _ProjectionAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def refresh(self, skills_root: Path, skill_id: str) -> None:
        self.calls.append((str(skills_root), skill_id))

    def rebuild(self, skills_root: Path) -> None:
        self.calls.append((str(skills_root), "rebuild"))


class _FailingProjectionAdapter(_ProjectionAdapter):
    def refresh(self, skills_root: Path, skill_id: str) -> None:
        super().refresh(skills_root, skill_id)
        (skills_root / "catalog.json").write_text("partial", encoding="utf-8")
        raise EvolutionRevalidationError("projection refresh failed")


class _ProcessExitProjectionAdapter(_ProjectionAdapter):
    def refresh(self, skills_root: Path, skill_id: str) -> None:
        super().refresh(skills_root, skill_id)
        (skills_root / "catalog.json").write_text("partial", encoding="utf-8")
        raise SystemExit("simulated process termination")


class _LateDefectProjectionAdapter(_ProjectionAdapter):
    def __init__(self, ledger: SkillHealthLedger, defect: SkillRunEvent) -> None:
        super().__init__()
        self.ledger = ledger
        self.defect = defect

    def refresh(self, skills_root: Path, skill_id: str) -> None:
        super().refresh(skills_root, skill_id)
        (skills_root / "catalog.json").write_text("partial", encoding="utf-8")
        self.ledger.append(self.defect)


class _SourceEditProjectionAdapter(_ProjectionAdapter):
    def __init__(self, source: Path) -> None:
        super().__init__()
        self.source = source

    def refresh(self, skills_root: Path, skill_id: str) -> None:
        super().refresh(skills_root, skill_id)
        self.source.write_text("REVISION = 2\n", encoding="utf-8")


class _LateCommitDefectLedger(SkillHealthLedger):
    """Inject one exact-hash defect only at the final approval fence."""

    def __init__(self, path: Path, defect: SkillRunEvent) -> None:
        super().__init__(path)
        self.defect = defect
        self.inject_on_lock = False
        self.injected = False

    @contextmanager
    def locked_events(self):
        if self.inject_on_lock and not self.injected:
            self.injected = True
            self.append(self.defect)
        with super().locked_events() as events:
            yield events


class _ExitOnClearRecoveryJournal(EvolutionRecoveryJournal):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.exit_on_clear = True

    def clear(self, proposal_id: str) -> None:
        if self.exit_on_clear:
            self.exit_on_clear = False
            raise SystemExit("simulated termination after proposal commit")
        super().clear(proposal_id)


class _ConcurrentProjectionAdapter:
    def refresh(self, skills_root: Path, skill_id: str) -> None:
        catalog = skills_root / "catalog.json"
        graph = skills_root / "skill_dag.json"
        if skill_id == "evolution-a":
            catalog.write_text("a-complete", encoding="utf-8")
            graph.write_text("a-complete", encoding="utf-8")
            return
        catalog.write_text("b-partial", encoding="utf-8")
        graph.write_text("b-partial", encoding="utf-8")
        raise EvolutionRevalidationError("projection refresh failed for B")


def _governance(
    tmp_path: Path,
    *,
    minimum_demo_executions: int = 1,
    execution_error: str = "",
):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root)
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    execution = _ExecutionAdapter(execution_error)
    projections = _ProjectionAdapter()
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=execution,
        projection_adapter=projections,
        minimum_demo_executions=minimum_demo_executions,
    )
    return governance, manifest, ledger, store, execution, projections


def _proposed_gotcha(tmp_path: Path, *, execution_error: str = ""):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root)
    skill_md = _write_gotcha_skill_md(manifest)
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    execution = _ExecutionAdapter(execution_error)
    projections = _ProjectionAdapter()
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=execution,
        projection_adapter=projections,
    )
    for event_id in ("defect-a", "defect-b", "defect-c"):
        ledger.append(
            _event(
                event_id,
                manifest,
                evidence_kind="ordinary",
                outcome="failed",
                error_kind="contract_failure",
                evidence_refs=["trace:evolution_test.py:1"],
            )
        )
    ledger.append(_event("conditional-success", manifest, evidence_kind="ordinary"))
    proposal = governance.propose_gotcha(
        target_skill="evolution-test",
        proposer="maintainer",
        reason="reviewable conditional failure cluster",
        support_event_ids=["defect-a", "defect-b", "defect-c"],
        entry={
            "lead": "Dense-only branch rejects sparse input.",
            "condition": "This occurs when the selected matrix remains sparse.",
            "guidance": "Densify only the bounded slice before this branch.",
            "anchors": ["evolution_test.py:1"],
        },
    )
    return (
        governance,
        manifest,
        skill_md,
        ledger,
        store,
        execution,
        projections,
        proposal,
    )


def _gotcha_evidence_draft(tmp_path: Path):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root)
    skill_md = _write_gotcha_skill_md(manifest)
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
    )
    for event_id in ("defect-a", "defect-b", "defect-c"):
        ledger.append(
            _event(
                event_id,
                manifest,
                evidence_kind="ordinary",
                outcome="failed",
                error_kind="script_defect",
                evidence_refs=["trace:evolution_test.py:1"],
            )
        )
    ledger.append(_event("conditional-success", manifest, evidence_kind="ordinary"))
    candidate = governance.refresh()[0]
    return governance, manifest, skill_md, ledger, store, candidate


def test_refresh_requires_distinct_explicit_demo_evidence_and_is_idempotent(tmp_path: Path):
    governance, manifest, ledger, store, _execution, _projections = _governance(
        tmp_path,
        minimum_demo_executions=2,
    )
    ledger.append(_event("ordinary", manifest, evidence_kind="ordinary"))
    ledger.append(_event("demo-a", manifest, execution_fingerprint="execution:a"))
    ledger.append(_event("demo-a-duplicate", manifest, execution_fingerprint="execution:a"))

    assert governance.refresh() == []

    ledger.append(_event("demo-b", manifest, execution_fingerprint="execution:b"))
    created = governance.refresh()
    duplicate_refresh = governance.refresh()

    assert len(created) == 1
    assert duplicate_refresh == []
    proposal = store.get(created[0].proposal_id)
    assert proposal is not None
    assert proposal.kind == "validation_promotion"
    assert proposal.proposed_change == {
        "field": "validation.level",
        "from": "smoke-only",
        "to": "demo-validated",
        "evidence_event_ids": ["demo-a", "demo-b"],
    }


def test_product_writeback_is_owned_by_governance_not_the_store(tmp_path: Path):
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")

    assert not hasattr(store, "approve_and_apply")


def test_product_approval_requires_human_label_and_review_reason(tmp_path: Path):
    governance, manifest, ledger, store, execution, projections = _governance(tmp_path)
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    before = manifest.read_bytes()

    with pytest.raises(ValueError, match="review reason"):
        governance.approve(proposal.proposal_id, approver="human", reason="")

    assert manifest.read_bytes() == before
    assert store.get(proposal.proposal_id).status == "pending"
    assert execution.calls == []
    assert projections.calls == []


def test_refresh_does_not_promote_a_current_version_with_skill_defects(tmp_path: Path):
    governance, manifest, ledger, _store, _execution, _projections = _governance(tmp_path)
    ledger.append(_event("demo", manifest))
    ledger.append(
        _event(
            "defect",
            manifest,
            outcome="failed",
            error_kind="contract_failure",
        )
    )

    assert governance.refresh() == []


def test_refresh_proposes_demotion_only_for_explicit_demo_skill_defects(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root, level="demo-validated")
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
    )
    ledger.append(
        _event(
            "ordinary-defect",
            manifest,
            evidence_kind="ordinary",
            outcome="failed",
            error_kind="contract_failure",
        )
    )
    ledger.append(
        _event(
            "environment-demo-failure",
            manifest,
            evidence_kind="demo",
            outcome="failed",
            error_kind="missing_dependency",
        )
    )

    assert governance.refresh() == []

    ledger.append(
        _event(
            "demo-defect",
            manifest,
            evidence_kind="demo",
            outcome="failed",
            error_kind="contract_failure",
        )
    )
    created = governance.refresh()

    assert len(created) == 1
    assert created[0].kind == "validation_demotion"
    assert created[0].support_event_ids == ["demo-defect"]
    assert created[0].proposed_change == {
        "field": "validation.level",
        "from": "demo-validated",
        "to": "smoke-only",
        "evidence_event_ids": ["demo-defect"],
    }


def test_operator_can_propose_evidence_bound_deprecation_with_routable_replacement(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root, skill_id="legacy-skill")
    replacement_manifest = _write_skill(
        skills_root,
        skill_id="replacement-skill",
        level="demo-validated",
    )
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
        minimum_deprecation_defects=2,
    )
    for event_id in ("defect-a", "defect-b"):
        ledger.append(
            _event(
                event_id,
                manifest,
                evidence_kind="ordinary",
                outcome="failed",
                error_kind="script_defect",
                skill_id="legacy-skill",
            )
        )

    proposal = governance.propose_deprecation(
        target_skill="legacy-skill",
        replacement_skill="replacement-skill",
        proposer="maintainer",
        reason="replacement has the maintained implementation",
        support_event_ids=["defect-a", "defect-b"],
    )

    assert proposal.kind == "skill_deprecation"
    assert proposal.proposed_by == "maintainer"
    assert proposal.proposal_reason == "replacement has the maintained implementation"
    assert proposal.proposed_change == {
        "field": "lifecycle",
        "from": "mvp",
        "to": "deprecated",
        "superseded_by": "replacement-skill",
        "replacement_version": "1.2.3",
        "replacement_hash": _hash(replacement_manifest),
        "evidence_event_ids": ["defect-a", "defect-b"],
    }
    assert proposal.source_hash == compute_execution_source_hash(
        manifest.with_name("legacy_skill.py"),
        skills_root=skills_root,
        skill_dir=manifest.parent,
    )
    assert store.get(proposal.proposal_id) == proposal


def test_deprecation_proposal_rejects_defects_from_an_old_target_source(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root, skill_id="legacy-skill")
    _write_skill(
        skills_root,
        skill_id="replacement-skill",
        level="demo-validated",
    )
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=EvolutionProposalStore(tmp_path / "proposals.jsonl"),
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
        minimum_deprecation_defects=1,
    )
    ledger.append(
        _event(
            "old-source-defect",
            manifest,
            outcome="failed",
            error_kind="script_defect",
            skill_id="legacy-skill",
        )
    )
    manifest.with_name("legacy_skill.py").write_text(
        "REVISION = 2\n",
        encoding="utf-8",
    )

    with pytest.raises(EvolutionRevalidationError, match="exact source"):
        governance.propose_deprecation(
            target_skill="legacy-skill",
            replacement_skill="replacement-skill",
            proposer="maintainer",
            reason="old defects must not govern new source",
            support_event_ids=["old-source-defect"],
        )


def test_deprecation_counterexamples_are_bound_to_the_proposed_target_source(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root, skill_id="legacy-skill")
    _write_skill(
        skills_root,
        skill_id="replacement-skill",
        level="demo-validated",
    )
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=EvolutionProposalStore(tmp_path / "proposals.jsonl"),
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
        minimum_deprecation_defects=1,
    )
    ledger.append(
        _event(
            "old-source-success",
            manifest,
            evidence_kind="ordinary",
            skill_id="legacy-skill",
        )
    )
    manifest.with_name("legacy_skill.py").write_text(
        "REVISION = 2\n",
        encoding="utf-8",
    )
    ledger.append(
        _event(
            "current-source-defect",
            manifest,
            evidence_kind="ordinary",
            outcome="failed",
            error_kind="contract_failure",
            skill_id="legacy-skill",
        )
    )

    proposal = governance.propose_deprecation(
        target_skill="legacy-skill",
        replacement_skill="replacement-skill",
        proposer="maintainer",
        reason="only current-source evidence is relevant",
        support_event_ids=["current-source-defect"],
    )

    assert proposal.counterexample_event_ids == []


@pytest.mark.parametrize("drift_kind", ["missing", "source"])
def test_deprecation_approval_revalidates_exact_source_counterexamples(
    tmp_path: Path,
    drift_kind: str,
):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root, skill_id="legacy-skill")
    _write_skill(
        skills_root,
        skill_id="replacement-skill",
        level="demo-validated",
    )
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=EvolutionProposalStore(tmp_path / "proposals.jsonl"),
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
        minimum_deprecation_defects=1,
    )
    counterexample = _event(
        "counterexample",
        manifest,
        evidence_kind="ordinary",
        skill_id="legacy-skill",
    )
    defect = _event(
        "defect",
        manifest,
        evidence_kind="ordinary",
        outcome="failed",
        error_kind="contract_failure",
        skill_id="legacy-skill",
    )
    ledger.append(counterexample)
    ledger.append(defect)
    proposal = governance.propose_deprecation(
        target_skill="legacy-skill",
        replacement_skill="replacement-skill",
        proposer="maintainer",
        reason="counterexample audit must remain exact",
        support_event_ids=["defect"],
    )
    assert proposal.counterexample_event_ids == ["counterexample"]
    remaining = [] if drift_kind == "missing" else [
        replace(counterexample, source_hash="sha256:" + "0" * 64)
    ]
    remaining.append(defect)
    ledger.path.write_text(
        "".join(
            json.dumps(event.to_dict(), sort_keys=True) + "\n"
            for event in remaining
        ),
        encoding="utf-8",
    )

    with pytest.raises(EvolutionRevalidationError, match="counterexample"):
        governance.approve(
            proposal.proposal_id,
            approver="reviewer",
            reason="evidence and counterexamples reviewed",
        )


def test_operator_can_propose_evidence_bound_conditional_gotcha(tmp_path: Path):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root)
    skill_md = _write_gotcha_skill_md(manifest)
    before = skill_md.read_bytes()
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
    )
    for event_id in ("defect-a", "defect-b", "defect-c"):
        ledger.append(
            _event(
                event_id,
                manifest,
                evidence_kind="ordinary",
                outcome="failed",
                error_kind="script_defect",
                evidence_refs=[
                    "stderr:sha256:opaque",
                    "trace:evolution_test.py:1",
                ],
            )
        )
    ledger.append(
        _event(
            "conditional-success",
            manifest,
            evidence_kind="ordinary",
        )
    )

    proposal = governance.propose_gotcha(
        target_skill="evolution-test",
        proposer="maintainer",
        reason="three exact-hash runs expose one conditional failure mode",
        support_event_ids=["defect-a", "defect-b", "defect-c"],
        entry={
            "lead": "Dense-only branch rejects sparse input.",
            "condition": "This occurs when the selected matrix remains sparse.",
            "guidance": "Densify only the bounded slice before this branch.",
            "anchors": ["evolution_test.py:1"],
        },
    )

    assert proposal.kind == "gotcha"
    assert proposal.skill_hash == _hash(manifest)
    assert proposal.source_hash == compute_execution_source_hash(
        manifest.with_name("evolution_test.py"),
        skills_root=_skills_root_for_manifest(manifest),
    )
    assert proposal.target_content_hash == _hash(skill_md)
    assert proposal.counterexample_event_ids == ["conditional-success"]
    assert proposal.proposed_change["field"] == "SKILL.md.Gotchas"
    assert proposal.proposed_change["entry"] == {
        "lead": "Dense-only branch rejects sparse input",
        "condition": "This occurs when the selected matrix remains sparse.",
        "guidance": "Densify only the bounded slice before this branch.",
        "anchors": ["evolution_test.py:1"],
    }
    assert store.get(proposal.proposal_id) == proposal
    assert skill_md.read_bytes() == before


def test_refresh_synthesizes_stable_gotcha_evidence_cluster_without_mutation(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root)
    skill_md = _write_gotcha_skill_md(manifest)
    manifest_before = manifest.read_bytes()
    skill_md_before = skill_md.read_bytes()
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
    )
    for event_id in ("defect-a", "defect-b", "defect-c"):
        ledger.append(
            _event(
                event_id,
                manifest,
                evidence_kind="ordinary",
                outcome="failed",
                error_kind="script_defect",
                evidence_refs=[
                    "stderr:sha256:opaque",
                    "trace:evolution_test.py:1",
                ],
            )
        )
    ledger.append(_event("conditional-success", manifest, evidence_kind="ordinary"))

    created = governance.refresh()
    duplicate_refresh = governance.refresh()

    assert len(created) == 1
    assert duplicate_refresh == []
    candidate = created[0]
    assert candidate.kind == "gotcha_evidence"
    assert candidate.status == "draft"
    assert candidate.support_event_ids == ["defect-a", "defect-b", "defect-c"]
    assert candidate.counterexample_event_ids == ["conditional-success"]
    assert candidate.source_hash == compute_execution_source_hash(
        manifest.with_name("evolution_test.py"),
        skills_root=_skills_root_for_manifest(manifest),
    )
    assert candidate.target_content_hash == _hash(skill_md)
    assert candidate.proposed_change == {
        "field": "SKILL.md.Gotchas",
        "action": "request_structured_entry",
        "evidence_error_kind": "script_defect",
        "evidence_environment_id": "env:test",
        "evidence_anchor": "evolution_test.py:1",
        "evidence_event_ids": ["defect-a", "defect-b", "defect-c"],
    }
    with pytest.raises(EvolutionRevalidationError, match="not pending"):
        governance.approve(
            candidate.proposal_id,
            approver="reviewer",
            reason="drafts cannot bypass structured narrative",
        )
    materialized = governance.propose_gotcha(
        target_skill="evolution-test",
        proposer="maintainer",
        reason="reviewed automatic evidence cluster",
        support_event_ids=["defect-a", "defect-b", "defect-c"],
        entry={
            "lead": "Dense-only branch rejects sparse input",
            "condition": "This occurs when the selected matrix remains sparse.",
            "guidance": "Densify only the bounded slice before this branch.",
            "anchors": ["evolution_test.py:1"],
        },
    )
    assert materialized.proposal_id != candidate.proposal_id
    assert materialized.proposed_change["source_candidate_id"] == candidate.proposal_id
    assert store.get(candidate.proposal_id).kind == "gotcha_evidence"
    assert store.get(candidate.proposal_id).status == "draft"
    assert store.get(materialized.proposal_id).kind == "gotcha"
    assert manifest.read_bytes() == manifest_before
    assert skill_md.read_bytes() == skill_md_before


def test_manifest_snapshot_parses_the_exact_bytes_it_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root)
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=SkillHealthLedger(tmp_path / "events.jsonl"),
        proposals=EvolutionProposalStore(tmp_path / "proposals.jsonl"),
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
    )

    def reject_second_path_read(_path: Path):
        raise AssertionError("manifest snapshot must parse its captured payload")

    monkeypatch.setattr(governance_module, "load_skill_yaml", reject_second_path_read)

    snapshots = governance._manifest_snapshots()

    assert [(path, digest) for path, _parsed, digest in snapshots] == [
        (manifest, _hash(manifest))
    ]


def test_refresh_stales_gotcha_evidence_draft_after_skill_md_content_drift(
    tmp_path: Path,
):
    governance, _manifest, skill_md, _ledger, store, candidate = (
        _gotcha_evidence_draft(tmp_path)
    )
    skill_md.write_text(
        skill_md.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )

    created = governance.refresh()

    assert len(created) == 1
    replacement = created[0]
    stale = store.get(candidate.proposal_id)
    assert stale.status == "stale"
    assert "target" in stale.validation_error
    assert replacement.kind == "gotcha_evidence"
    assert replacement.status == "draft"
    assert replacement.proposal_id != candidate.proposal_id
    assert replacement.target_content_hash == _hash(skill_md)
    materialized = governance.propose_gotcha(
        target_skill="evolution-test",
        proposer="maintainer",
        reason="materialize the refreshed target-bound candidate",
        support_event_ids=["defect-a", "defect-b", "defect-c"],
        entry={
            "lead": "Dense-only branch rejects sparse input",
            "condition": "This occurs when the selected matrix remains sparse.",
            "guidance": "Densify only the bounded slice before this branch.",
            "anchors": ["evolution_test.py:1"],
        },
    )
    assert materialized.proposed_change["source_candidate_id"] == (
        replacement.proposal_id
    )


def test_refresh_stales_old_gotcha_draft_after_execution_source_drift(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root)
    _write_gotcha_skill_md(manifest)
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
    )
    for event_id in ("defect-a", "defect-b", "defect-c"):
        ledger.append(
            _event(
                event_id,
                manifest,
                evidence_kind="ordinary",
                outcome="failed",
                error_kind="script_defect",
                evidence_refs=["trace:evolution_test.py:1"],
            )
        )
    ledger.append(_event("conditional-success", manifest, evidence_kind="ordinary"))
    old_candidate = governance.refresh()[0]

    manifest.with_name("evolution_test.py").write_text(
        "if __name__ == '__main__':\n    print('new revision')\n",
        encoding="utf-8",
    )

    assert governance.refresh() == []
    assert store.get(old_candidate.proposal_id).status == "stale"
    assert "source" in store.get(old_candidate.proposal_id).validation_error

    for event_id in ("new-defect-a", "new-defect-b", "new-defect-c"):
        ledger.append(
            _event(
                event_id,
                manifest,
                evidence_kind="ordinary",
                outcome="failed",
                error_kind="script_defect",
                evidence_refs=["trace:evolution_test.py:1"],
            )
        )
    ledger.append(_event("new-success", manifest, evidence_kind="ordinary"))

    new_candidate = governance.refresh()[0]
    assert new_candidate.kind == "gotcha_evidence"
    assert new_candidate.proposal_id != old_candidate.proposal_id
    assert new_candidate.source_hash == compute_execution_source_hash(
        manifest.with_name("evolution_test.py"),
        skills_root=_skills_root_for_manifest(manifest),
    )


def test_refresh_stales_nested_singlecell_gotcha_draft_after_domain_library_drift(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    domain_lib = skills_root / "singlecell" / "_lib"
    subdomain_lib = skills_root / "singlecell" / "scrna" / "_lib"
    outer_lib = tmp_path / "_lib"
    domain_lib.mkdir(parents=True)
    subdomain_lib.mkdir(parents=True)
    outer_lib.mkdir(parents=True)
    shared_source = domain_lib / "shared.py"
    subdomain_source = subdomain_lib / "shared.py"
    outer_source = outer_lib / "shared.py"
    shared_source.write_text("REVISION = 1\n", encoding="utf-8")
    subdomain_source.write_text("REVISION = 1\n", encoding="utf-8")
    outer_source.write_text("REVISION = 1\n", encoding="utf-8")
    manifest = _write_skill(
        skills_root,
        domain="singlecell",
        subdomain="scrna",
    )
    _write_gotcha_skill_md(manifest)
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
    )
    for event_id in ("defect-a", "defect-b", "defect-c"):
        ledger.append(
            _event(
                event_id,
                manifest,
                evidence_kind="ordinary",
                outcome="failed",
                error_kind="script_defect",
                evidence_refs=["trace:evolution_test.py:1"],
            )
        )
    ledger.append(_event("conditional-success", manifest, evidence_kind="ordinary"))
    candidate = governance.refresh()[0]

    outer_source.write_text("REVISION = 2\n", encoding="utf-8")
    assert governance.refresh() == []
    assert store.get(candidate.proposal_id).status == "draft"

    shared_source.write_text("REVISION = 2\n", encoding="utf-8")

    assert governance.refresh() == []
    stale = store.get(candidate.proposal_id)
    assert stale.status == "stale"
    assert "source" in stale.validation_error
    assert compute_execution_source_hash(
        manifest.with_name("evolution_test.py"),
        skills_root=skills_root,
    ) != candidate.source_hash


def test_refresh_stales_pending_nested_gotcha_only_for_root_bounded_source_drift(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    domain_lib = skills_root / "singlecell" / "_lib"
    subdomain_lib = skills_root / "singlecell" / "scrna" / "_lib"
    outer_lib = tmp_path / "_lib"
    for path in (domain_lib, subdomain_lib, outer_lib):
        path.mkdir(parents=True)
    domain_source = domain_lib / "shared.py"
    subdomain_source = subdomain_lib / "shared.py"
    outer_source = outer_lib / "shared.py"
    domain_source.write_text("REVISION = 1\n", encoding="utf-8")
    subdomain_source.write_text("REVISION = 1\n", encoding="utf-8")
    outer_source.write_text("REVISION = 1\n", encoding="utf-8")
    manifest = _write_skill(
        skills_root,
        domain="singlecell",
        subdomain="scrna",
    )
    _write_gotcha_skill_md(manifest)
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
    )
    for event_id in ("defect-a", "defect-b", "defect-c"):
        ledger.append(
            _event(
                event_id,
                manifest,
                evidence_kind="ordinary",
                outcome="failed",
                error_kind="contract_failure",
                evidence_refs=["trace:evolution_test.py:1"],
            )
        )
    ledger.append(_event("conditional-success", manifest, evidence_kind="ordinary"))
    proposal = governance.propose_gotcha(
        target_skill="evolution-test",
        proposer="maintainer",
        reason="root-bounded pending provenance",
        support_event_ids=["defect-a", "defect-b", "defect-c"],
        entry={
            "lead": "Dense-only branch rejects sparse input",
            "condition": "This occurs when the selected matrix remains sparse.",
            "guidance": "Densify only the bounded slice before this branch.",
            "anchors": ["evolution_test.py:1"],
        },
    )

    outer_source.write_text("REVISION = 2\n", encoding="utf-8")
    assert governance.refresh() == []
    assert store.get(proposal.proposal_id).status == "pending"

    domain_source.write_text("REVISION = 2\n", encoding="utf-8")
    assert governance.refresh() == []
    stale = store.get(proposal.proposal_id)
    assert stale.status == "stale"
    assert "source" in stale.validation_error


def test_nested_domain_library_drift_blocks_direct_gotcha_approval(
    tmp_path: Path,
):
    """Approval must revalidate the real domain lib without a prior refresh."""
    skills_root = tmp_path / "skills"
    domain_lib = skills_root / "singlecell" / "_lib"
    subdomain_lib = skills_root / "singlecell" / "scrna" / "_lib"
    domain_lib.mkdir(parents=True)
    subdomain_lib.mkdir(parents=True)
    domain_source = domain_lib / "shared.py"
    subdomain_source = subdomain_lib / "shared.py"
    domain_source.write_text("REVISION = 1\n", encoding="utf-8")
    subdomain_source.write_text("DECOY_REVISION = 1\n", encoding="utf-8")
    manifest = _write_skill(
        skills_root,
        domain="singlecell",
        subdomain="scrna",
    )
    skill_md = _write_gotcha_skill_md(manifest)
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    execution = _ExecutionAdapter()
    projections = _ProjectionAdapter()
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=execution,
        projection_adapter=projections,
    )
    for event_id in ("defect-a", "defect-b", "defect-c"):
        ledger.append(
            _event(
                event_id,
                manifest,
                evidence_kind="ordinary",
                outcome="failed",
                error_kind="contract_failure",
                evidence_refs=["trace:evolution_test.py:1"],
            )
        )
    ledger.append(_event("conditional-success", manifest, evidence_kind="ordinary"))
    proposal = governance.propose_gotcha(
        target_skill="evolution-test",
        proposer="maintainer",
        reason="direct approval must retain nested provenance",
        support_event_ids=["defect-a", "defect-b", "defect-c"],
        entry={
            "lead": "Dense-only branch rejects sparse input",
            "condition": "This occurs when the selected matrix remains sparse.",
            "guidance": "Densify only the bounded slice before this branch.",
            "anchors": ["evolution_test.py:1"],
        },
    )
    before = skill_md.read_bytes()

    domain_source.write_text("REVISION = 2\n", encoding="utf-8")

    with pytest.raises(EvolutionRevalidationError, match="source changed"):
        governance.approve(
            proposal.proposal_id,
            approver="reviewer",
            reason="attempt direct approval after drift",
        )

    assert store.get(proposal.proposal_id).status == "stale"
    assert skill_md.read_bytes() == before
    assert execution.calls == []
    assert projections.calls == []


def test_refresh_rebinds_stale_pending_gotcha_evidence_after_target_relocation(
    tmp_path: Path,
):
    (
        governance,
        manifest,
        _skill_md,
        _ledger,
        store,
        _execution,
        _projections,
        proposal,
    ) = _proposed_gotcha(tmp_path)
    relocated = governance.skills_root / "relocated" / manifest.parent.name
    relocated.parent.mkdir(parents=True)
    manifest.parent.rename(relocated)

    created = governance.refresh()

    assert len(created) == 1
    rebound = created[0]
    assert rebound.kind == "gotcha_evidence"
    assert rebound.status == "draft"
    assert rebound.proposal_id != proposal.proposed_change["source_candidate_id"]
    assert rebound.target_path_hash != proposal.target_path_hash
    stale = store.get(proposal.proposal_id)
    assert stale.status == "stale"
    assert "target" in stale.validation_error


def test_gotcha_materialization_rejects_evidence_candidate_after_target_drift(
    tmp_path: Path,
):
    governance, _manifest, skill_md, _ledger, store, candidate = (
        _gotcha_evidence_draft(tmp_path)
    )
    skill_md.write_text(
        skill_md.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        EvolutionRevalidationError,
        match="evidence candidate no longer matches",
    ):
        governance.propose_gotcha(
            target_skill="evolution-test",
            proposer="maintainer",
            reason="must remain bound to the nominated target",
            support_event_ids=["defect-a", "defect-b", "defect-c"],
            entry={
                "lead": "Dense-only branch rejects sparse input",
                "condition": "This occurs when the selected matrix remains sparse.",
                "guidance": "Densify only the bounded slice before this branch.",
                "anchors": ["evolution_test.py:1"],
            },
        )

    assert store.get(candidate.proposal_id).status == "draft"
    assert len(store.list_latest()) == 1


def test_gotcha_materialization_rejects_relocated_evidence_candidate(
    tmp_path: Path,
):
    governance, manifest, _skill_md, _ledger, store, candidate = (
        _gotcha_evidence_draft(tmp_path)
    )
    relocated = governance.skills_root / "relocated" / manifest.parent.name
    relocated.parent.mkdir(parents=True)
    manifest.parent.rename(relocated)

    with pytest.raises(
        EvolutionRevalidationError,
        match="evidence candidate no longer matches",
    ):
        governance.propose_gotcha(
            target_skill="evolution-test",
            proposer="maintainer",
            reason="must remain bound to the nominated canonical path",
            support_event_ids=["defect-a", "defect-b", "defect-c"],
            entry={
                "lead": "Dense-only branch rejects sparse input",
                "condition": "This occurs when the selected matrix remains sparse.",
                "guidance": "Densify only the bounded slice before this branch.",
                "anchors": ["evolution_test.py:1"],
            },
        )

    assert store.get(candidate.proposal_id).status == "draft"
    assert len(store.list_latest()) == 1


def test_gotcha_candidate_rejects_generic_result_key_as_structural_anchor(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root)
    _write_gotcha_skill_md(manifest)
    manifest.with_name("evolution_test.py").write_text(
        "success = True\nif __name__ == '__main__':\n    pass\n",
        encoding="utf-8",
    )
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=EvolutionProposalStore(tmp_path / "proposals.jsonl"),
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
    )
    for event_id in ("defect-a", "defect-b", "defect-c"):
        ledger.append(
            _event(
                event_id,
                manifest,
                evidence_kind="ordinary",
                outcome="failed",
                error_kind="contract_failure",
                evidence_refs=["result_keys:success,outputs,artifacts"],
            )
        )
    ledger.append(_event("conditional-success", manifest, evidence_kind="ordinary"))

    assert governance.refresh() == []
    with pytest.raises(ValueError, match="file:line"):
        governance.propose_gotcha(
            target_skill="evolution-test",
            proposer="maintainer",
            reason="generic envelope keys are not a root-cause signature",
            support_event_ids=["defect-a", "defect-b", "defect-c"],
            entry={
                "lead": "Generic result keys are not evidence",
                "condition": "This entry has no source-local signature.",
                "guidance": "Use an exact traceback anchor instead.",
                "anchors": ['result.json["success"]'],
            },
        )


def test_gotcha_rejects_conflicting_outcomes_for_one_execution_identity(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root)
    _write_gotcha_skill_md(manifest)
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=EvolutionProposalStore(tmp_path / "proposals.jsonl"),
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
    )
    for event_id, fingerprint in (
        ("defect-a", "execution:contradiction"),
        ("defect-b", "execution:b"),
        ("defect-c", "execution:c"),
    ):
        ledger.append(
            _event(
                event_id,
                manifest,
                evidence_kind="ordinary",
                outcome="failed",
                error_kind="script_defect",
                execution_fingerprint=fingerprint,
                evidence_refs=["trace:evolution_test.py:1"],
            )
        )
    ledger.append(
        _event(
            "conditional-success",
            manifest,
            evidence_kind="ordinary",
            execution_fingerprint="execution:contradiction",
        )
    )

    assert governance.refresh() == []
    with pytest.raises(EvolutionRevalidationError, match="conflicting execution identity"):
        governance.propose_gotcha(
            target_skill="evolution-test",
            proposer="maintainer",
            reason="contradictory ledger identities must fail closed",
            support_event_ids=["defect-a", "defect-b", "defect-c"],
            entry={
                "lead": "Dense-only branch rejects sparse input",
                "condition": "This occurs when the selected matrix remains sparse.",
                "guidance": "Densify only the bounded slice before this branch.",
                "anchors": ["evolution_test.py:1"],
            },
        )


def test_gotcha_governance_excludes_consensus_without_demo_revalidation(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root, skill_type="consensus")
    _write_gotcha_skill_md(manifest)
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=EvolutionProposalStore(tmp_path / "proposals.jsonl"),
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
    )
    for event_id in ("defect-a", "defect-b", "defect-c"):
        ledger.append(
            _event(
                event_id,
                manifest,
                evidence_kind="ordinary",
                outcome="failed",
                error_kind="script_defect",
                evidence_refs=["trace:evolution_test.py:1"],
            )
        )
    ledger.append(_event("conditional-success", manifest, evidence_kind="ordinary"))

    assert governance.refresh() == []
    with pytest.raises(EvolutionRevalidationError, match="does not support governed demo"):
        governance.propose_gotcha(
            target_skill="evolution-test",
            proposer="maintainer",
            reason="cannot create an unapprovable proposal",
            support_event_ids=["defect-a", "defect-b", "defect-c"],
            entry={
                "lead": "Consensus runner rejects this input",
                "condition": "This occurs for an unsupported input shape.",
                "guidance": "Use a compatible execution surface.",
                "anchors": ["evolution_test.py:1"],
            },
        )


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "See ,/home/alice/patient.h5ad.",
        "See path:/home/alice/patient.h5ad.",
        "See //server/share/patient.h5ad.",
        "/home/alice/patient_A.h5ad must be copied first.",
        "/tmp must be inspected before running.",
        'Read "/home/alice/patient_A.h5ad" before running.',
        "Read '/home/alice/patient_A.h5ad' before running.",
        "C:\\temp must be inspected before running.",
        "C:/Users/Alice/patient_A.h5ad must be inspected before running.",
        "See ;C:/Users/Alice/patient.h5ad.",
        "token=super-secret-value must be set.",
        "client_secret=super-secret-value must be set.",
        "AWS_SECRET_ACCESS_KEY=super-secret-value must be set.",
        "authorization: Bearer-super-secret-value must be set.",
        "API key = super-secret-value must be set.",
        "api key: super-secret-value must be set.",
        "API key：super-secret-value must be set.",
        "API_KEY＝super-secret-value must be set.",
        "ＡＰＩ＿ＫＥＹ＝super-secret-value must be set.",
        "api.key=super-secret-value must be set.",
        "API‐key=super-secret-value must be set.",
        "API‑key=super-secret-value must be set.",
        "API–key=super-secret-value must be set.",
        "API·key=super-secret-value must be set.",
        "access key = super-secret-value must be set.",
        "refresh token = super-secret-value must be set.",
        "private key = super-secret-value must be set.",
        "AWS secret access key = super-secret-value must be set.",
        "SECRET_KEY = super-secret-value must be set.",
        "secret-key: super-secret-value must be set.",
        "secret.key = super-secret-value must be set.",
        "DJANGO_SECRET_KEY = super-secret-value must be set.",
        "CLIENT SECRET KEY = super-secret-value must be set.",
        "JWT-SECRET-KEY = super-secret-value must be set.",
        "ＳＥＣＲＥＴ＿ＫＥＹ＝super-secret-value must be set.",
        "ＤＪＡＮＧＯ＿ＳＥＣＲＥＴ＿ＫＥＹ＝super-secret-value must be set.",
        "Hidden\u2028line separator is unsafe.",
        "Open https://private.example/patient before running.",
        "Open _https://private.example/patient before running.",
        "Use _private emphasis_ here.",
        "Open ftp://private.example/patient before running.",
        "Open s3://private-bucket/patient before running.",
    ],
)
def test_gotcha_narrative_rejects_sensitive_or_non_single_line_text(
    unsafe_text: str,
):
    with pytest.raises(ValueError, match="unsafe"):
        SkillEvolutionGovernance._normalize_gotcha_entry(
            {
                "lead": "Safe generalized lead",
                "condition": unsafe_text,
                "guidance": "Use a generalized remediation.",
                "anchors": ["evolution_test.py:1"],
            }
        )


@pytest.mark.parametrize(
    "scientific_text",
    [
        "HLA_DRA = high expression after log1p normalization.",
        "HLA DRA = high expression after log1p normalization.",
        "MS4A1: high expression after log1p normalization.",
        "p_value = 0.05 after adjustment.",
        "secretory_marker = high in this cell state.",
        "tokenization_score = 0.9 for this representation.",
        "accessibility_score = high in the selected peak set.",
        "API key expression = low in this annotation vocabulary.",
        "obs_key = cell_type in this annotation table.",
        "cluster_key = leiden in this analysis.",
        "feature_key = gene_id in this matrix.",
        "secret_key_gene = high expression in this cell state.",
    ],
)
def test_gotcha_narrative_allows_plain_scientific_identifiers(
    scientific_text: str,
):
    normalized = SkillEvolutionGovernance._normalize_gotcha_entry(
        {
            "lead": "Marker expression differs across cell states",
            "condition": scientific_text,
            "guidance": "Compare the bounded cell subset with an adjusted p value.",
            "anchors": ["evolution_test.py:1"],
        }
    )

    assert normalized["condition"] == scientific_text


@pytest.mark.parametrize(
    ("evidence_refs", "include_success", "expected_error"),
    [
        (["stderr:sha256:opaque"], True, "every Gotcha anchor"),
        (["trace:evolution_test.py:1"], False, "success counterexample"),
    ],
)
def test_gotcha_candidate_requires_structural_evidence_and_counterexample(
    tmp_path: Path,
    evidence_refs: list[str],
    include_success: bool,
    expected_error: str,
):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root)
    _write_gotcha_skill_md(manifest)
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=EvolutionProposalStore(tmp_path / "proposals.jsonl"),
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
    )
    for event_id in ("defect-a", "defect-b", "defect-c"):
        ledger.append(
            _event(
                event_id,
                manifest,
                evidence_kind="ordinary",
                outcome="failed",
                error_kind="script_defect",
                evidence_refs=evidence_refs,
            )
        )
    if include_success:
        ledger.append(_event("conditional-success", manifest, evidence_kind="ordinary"))

    assert governance.refresh() == []
    with pytest.raises(EvolutionRevalidationError, match=expected_error):
        governance.propose_gotcha(
            target_skill="evolution-test",
            proposer="maintainer",
            reason="insufficient candidate evidence",
            support_event_ids=["defect-a", "defect-b", "defect-c"],
            entry={
                "lead": "Dense-only branch rejects sparse input",
                "condition": "This occurs when the selected matrix remains sparse.",
                "guidance": "Densify only the bounded slice before this branch.",
                "anchors": ["evolution_test.py:1"],
            },
        )


def test_gotcha_narrative_has_versioned_revision_identity(tmp_path: Path):
    (
        governance,
        _manifest,
        _skill_md,
        _ledger,
        store,
        _execution,
        _projections,
        first,
    ) = _proposed_gotcha(tmp_path)
    exact_retry = governance.propose_gotcha(
        target_skill="evolution-test",
        proposer="maintainer",
        reason="reviewable conditional failure cluster",
        support_event_ids=["defect-a", "defect-b", "defect-c"],
        entry={
            "lead": "Dense-only branch rejects sparse input.",
            "condition": "This occurs when the selected matrix remains sparse.",
            "guidance": "Densify only the bounded slice before this branch.",
            "anchors": ["evolution_test.py:1"],
        },
    )
    assert exact_retry == first

    with pytest.raises(EvolutionRevalidationError, match="revision is already pending"):
        governance.propose_gotcha(
            target_skill="evolution-test",
            proposer="maintainer",
            reason="revised guidance",
            support_event_ids=["defect-a", "defect-b", "defect-c"],
            entry={
                "lead": "Dense-only branch rejects sparse input.",
                "condition": "This occurs when the selected matrix remains sparse.",
                "guidance": "Convert only the bounded slice before this branch.",
                "anchors": ["evolution_test.py:1"],
            },
        )

    governance.reject(
        first.proposal_id,
        approver="reviewer",
        reason="guidance needs a narrower remediation",
    )
    revised = governance.propose_gotcha(
        target_skill="evolution-test",
        proposer="maintainer",
        reason="revised guidance",
        support_event_ids=["defect-a", "defect-b", "defect-c"],
        entry={
            "lead": "Dense-only branch rejects sparse input.",
            "condition": "This occurs when the selected matrix remains sparse.",
            "guidance": "Convert only the bounded slice before this branch.",
            "anchors": ["evolution_test.py:1"],
        },
    )

    assert revised.proposal_id != first.proposal_id
    assert revised.proposed_change["source_candidate_id"] == first.proposed_change[
        "source_candidate_id"
    ]
    assert store.get(first.proposal_id).status == "rejected"
    assert store.get(revised.proposal_id).status == "pending"


def test_approval_appends_gotcha_once_without_changing_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root)
    skill_md = _write_gotcha_skill_md(manifest)
    manifest_before = manifest.read_bytes()
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    execution = _ExecutionAdapter()
    projections = _ProjectionAdapter()
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=execution,
        projection_adapter=projections,
    )
    for event_id in ("defect-a", "defect-b", "defect-c"):
        ledger.append(
            _event(
                event_id,
                manifest,
                evidence_kind="ordinary",
                outcome="failed",
                error_kind="contract_failure",
                evidence_refs=["trace:evolution_test.py:1"],
            )
        )
    ledger.append(_event("conditional-success", manifest, evidence_kind="ordinary"))
    proposal = governance.propose_gotcha(
        target_skill="evolution-test",
        proposer="maintainer",
        reason="reviewable conditional failure cluster",
        support_event_ids=["defect-a", "defect-b", "defect-c"],
        entry={
            "lead": "Dense-only branch rejects sparse input",
            "condition": "This occurs when the selected matrix remains sparse.",
            "guidance": "Densify only the bounded slice before this branch.",
            "anchors": ["evolution_test.py:1"],
        },
    )
    from omicsclaw.runtime.context import layers as context_layers

    real_load_skill_context = context_layers.load_skill_context
    consumed_contexts: list[str] = []

    def observe_runtime_consumption(**kwargs):
        value = real_load_skill_context(**kwargs)
        consumed_contexts.append(value)
        return value

    monkeypatch.setattr(
        context_layers,
        "load_skill_context",
        observe_runtime_consumption,
    )

    receipt = governance.approve(
        proposal.proposal_id,
        approver="reviewer",
        reason="evidence and guidance verified",
    )

    text = skill_md.read_text(encoding="utf-8")
    assert receipt.status == "approved"
    assert manifest.read_bytes() == manifest_before
    assert "_None yet" not in text
    assert text.count("**Dense-only branch rejects sparse input.**") == 1
    assert render_skill_md(load_skill_yaml(manifest), text) == text
    assert execution.calls == ["evolution-test"]
    assert projections.calls == []
    assert len(consumed_contexts) == 1
    assert "Dense-only branch rejects sparse input." in consumed_contexts[0]
    assert "This occurs when the selected matrix remains sparse." in consumed_contexts[0]
    assert "Densify only the bounded slice before this branch." in consumed_contexts[0]
    persisted = store.get(proposal.proposal_id)
    assert persisted is not None
    assert persisted.status == "approved"
    assert persisted.proposed_by == "maintainer"
    assert persisted.approved_by == "reviewer"
    assert persisted.before_hash == proposal.target_content_hash
    probe = OmicsRegistry()
    probe.load_all(skills_root)
    assert probe.skills["evolution-test"]["gotchas"] == (
        "Dense-only branch rejects sparse input.",
    )
    assert probe.skills["evolution-test"]["gotcha_details"] == (
        "**Dense-only branch rejects sparse input.** "
        "This occurs when the selected matrix remains sparse. "
        "Densify only the bounded slice before this branch. "
        "Evidence: `evolution_test.py:1`.",
    )
    assert governance.refresh() == []


def test_gotcha_approval_marks_proposal_stale_when_runtime_source_changed(
    tmp_path: Path,
):
    (
        governance,
        manifest,
        skill_md,
        _ledger,
        store,
        execution,
        projections,
        proposal,
    ) = _proposed_gotcha(tmp_path)
    before = skill_md.read_bytes()
    manifest.with_name("evolution_test.py").write_text(
        "if __name__ == '__main__':\n    print('changed')\n",
        encoding="utf-8",
    )

    with pytest.raises(EvolutionRevalidationError, match="source changed"):
        governance.approve(
            proposal.proposal_id,
            approver="reviewer",
            reason="reviewed before source drift",
        )

    assert skill_md.read_bytes() == before
    assert store.get(proposal.proposal_id).status == "stale"
    assert execution.calls == []
    assert projections.calls == []


def test_refresh_creates_audited_review_after_approved_gotcha_source_drift(
    tmp_path: Path,
):
    (
        governance,
        manifest,
        _skill_md,
        _ledger,
        store,
        _execution,
        _projections,
        proposal,
    ) = _proposed_gotcha(tmp_path)
    governance.approve(
        proposal.proposal_id,
        approver="reviewer",
        reason="original source and narrative reviewed",
    )
    approved = store.get(proposal.proposal_id)
    assert approved.status == "approved"

    manifest.with_name("evolution_test.py").write_text(
        "if __name__ == '__main__':\n    print('new behavior')\n",
        encoding="utf-8",
    )
    current_source_hash = compute_execution_source_hash(
        manifest.with_name("evolution_test.py"),
        skills_root=_skills_root_for_manifest(manifest),
    )

    created = governance.refresh()

    assert len(created) == 1
    review = created[0]
    assert review.kind == "gotcha_review"
    assert review.status == "draft"
    assert review.source_hash == current_source_hash
    assert review.proposed_change == {
        "field": "SKILL.md.Gotchas",
        "action": "review_after_source_drift",
        "source_proposal_id": proposal.proposal_id,
        "approved_manifest_hash": proposal.skill_hash,
        "approved_source_hash": proposal.source_hash,
        "approved_target_path_hash": proposal.target_path_hash,
        "approved_target_content_hash": approved.after_hash,
        "current_state": "current",
        "current_manifest_hash": _hash(manifest),
        "current_source_hash": current_source_hash,
        "current_target_path_hash": proposal.target_path_hash,
        "current_target_content_hash": approved.after_hash,
        "rendered_bullet": proposal.proposed_change["rendered_bullet"],
    }
    assert store.get(proposal.proposal_id).status == "approved"
    with pytest.raises(EvolutionRevalidationError, match="not pending"):
        governance.approve(
            review.proposal_id,
            approver="reviewer",
            reason="review drafts cannot mutate automatically",
        )


def test_refresh_reviews_approved_nested_gotcha_after_domain_library_drift(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    domain_lib = skills_root / "singlecell" / "_lib"
    subdomain_lib = skills_root / "singlecell" / "scrna" / "_lib"
    outer_lib = tmp_path / "_lib"
    domain_lib.mkdir(parents=True)
    subdomain_lib.mkdir(parents=True)
    outer_lib.mkdir(parents=True)
    shared_source = domain_lib / "shared.py"
    subdomain_source = subdomain_lib / "shared.py"
    outer_source = outer_lib / "shared.py"
    shared_source.write_text("REVISION = 1\n", encoding="utf-8")
    subdomain_source.write_text("REVISION = 1\n", encoding="utf-8")
    outer_source.write_text("REVISION = 1\n", encoding="utf-8")
    manifest = _write_skill(
        skills_root,
        domain="singlecell",
        subdomain="scrna",
    )
    _write_gotcha_skill_md(manifest)
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
    )
    for event_id in ("defect-a", "defect-b", "defect-c"):
        ledger.append(
            _event(
                event_id,
                manifest,
                evidence_kind="ordinary",
                outcome="failed",
                error_kind="contract_failure",
                evidence_refs=["trace:evolution_test.py:1"],
            )
        )
    ledger.append(_event("conditional-success", manifest, evidence_kind="ordinary"))
    proposal = governance.propose_gotcha(
        target_skill="evolution-test",
        proposer="maintainer",
        reason="review nested domain-library evidence",
        support_event_ids=["defect-a", "defect-b", "defect-c"],
        entry={
            "lead": "Dense-only branch rejects sparse input",
            "condition": "This occurs when the selected matrix remains sparse.",
            "guidance": "Densify only the bounded slice before this branch.",
            "anchors": ["evolution_test.py:1"],
        },
    )
    governance.approve(
        proposal.proposal_id,
        approver="reviewer",
        reason="source and narrative reviewed",
    )

    outer_source.write_text("REVISION = 2\n", encoding="utf-8")
    assert governance.refresh() == []
    assert store.get(proposal.proposal_id).status == "approved"

    shared_source.write_text("REVISION = 2\n", encoding="utf-8")
    current_source_hash = compute_execution_source_hash(
        manifest.with_name("evolution_test.py"),
        skills_root=skills_root,
    )
    created = governance.refresh()

    assert len(created) == 1
    review = created[0]
    assert review.kind == "gotcha_review"
    assert review.status == "draft"
    assert review.source_hash == current_source_hash
    assert review.source_hash != proposal.source_hash
    assert review.proposed_change["action"] == "review_after_source_drift"
    assert review.proposed_change["current_state"] == "current"
    assert review.proposed_change["current_source_hash"] == current_source_hash
    assert store.get(proposal.proposal_id).status == "approved"


def test_refresh_does_not_reopen_the_materialized_cluster_after_approval(
    tmp_path: Path,
):
    (
        governance,
        _manifest,
        _skill_md,
        _ledger,
        _store,
        _execution,
        _projections,
        proposal,
    ) = _proposed_gotcha(tmp_path)
    governance.approve(
        proposal.proposal_id,
        approver="reviewer",
        reason="exact-source cluster and narrative reviewed",
    )

    assert governance.refresh() == []


@pytest.mark.parametrize(
    ("transition", "expected_state"),
    [
        ("deprecated", "non_routable_lifecycle:deprecated"),
        ("consensus", "unsupported_consensus"),
        ("missing_source", "missing_source"),
        ("missing_target", "missing_target"),
        ("missing_manifest", "missing_manifest"),
    ],
)
def test_refresh_reviews_approved_gotcha_for_explicit_non_current_state(
    tmp_path: Path,
    transition: str,
    expected_state: str,
):
    (
        governance,
        manifest,
        skill_md,
        _ledger,
        store,
        _execution,
        _projections,
        proposal,
    ) = _proposed_gotcha(tmp_path)
    governance.approve(
        proposal.proposal_id,
        approver="reviewer",
        reason="original source and narrative reviewed",
    )

    if transition in {"deprecated", "consensus"}:
        raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        if transition == "deprecated":
            raw["lifecycle"] = {
                "status": "deprecated",
                "superseded_by": "replacement-skill",
            }
        else:
            raw["type"] = "consensus"
        manifest.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    elif transition == "missing_source":
        manifest.with_name("evolution_test.py").unlink()
    elif transition == "missing_target":
        skill_md.unlink()
    else:
        manifest.unlink()

    created = governance.refresh()

    assert len(created) == 1
    review = created[0]
    assert review.kind == "gotcha_review"
    assert review.status == "draft"
    assert review.proposed_change["current_state"] == expected_state
    assert review.proposed_change["source_proposal_id"] == proposal.proposal_id
    assert (
        review.proposed_change["current_target_path_hash"]
        == review.target_path_hash
    )
    assert (
        review.proposed_change["current_target_content_hash"]
        == review.target_content_hash
    )
    if transition == "missing_source":
        assert review.source_hash == ""
    if transition == "missing_target":
        assert review.target_content_hash == ""
    assert store.get(proposal.proposal_id).status == "approved"
    assert governance.refresh() == []
    assert store.get(review.proposal_id).status == "draft"


def test_gotcha_approval_does_not_rewrite_catalog_or_compatibility_dag(
    tmp_path: Path,
):
    (
        governance,
        _manifest,
        _skill_md,
        _ledger,
        _store,
        _execution,
        _projections,
        proposal,
    ) = _proposed_gotcha(tmp_path)
    catalog = governance.skills_root / "catalog.json"
    graph = governance.skills_root / "skill_dag.json"
    catalog.write_bytes(b"unrelated-catalog-bytes\n")
    graph.write_bytes(b"unrelated-dag-bytes\n")
    governance.projection_adapter = RegistryProjectionAdapter()

    governance.approve(
        proposal.proposal_id,
        approver="reviewer",
        reason="narrative-only change reviewed",
    )

    assert catalog.read_bytes() == b"unrelated-catalog-bytes\n"
    assert graph.read_bytes() == b"unrelated-dag-bytes\n"


def test_gotcha_execution_gate_failure_leaves_skill_md_unchanged(tmp_path: Path):
    (
        governance,
        _manifest,
        skill_md,
        _ledger,
        store,
        execution,
        projections,
        proposal,
    ) = _proposed_gotcha(tmp_path, execution_error="demo failed")
    before = skill_md.read_bytes()

    with pytest.raises(EvolutionRevalidationError, match="demo failed"):
        governance.approve(
            proposal.proposal_id,
            approver="reviewer",
            reason="reviewed conditional failure",
        )

    assert skill_md.read_bytes() == before
    assert store.get(proposal.proposal_id).status == "rolled_back"
    assert execution.calls == ["evolution-test"]
    assert projections.calls == []
    assert governance.recovery_journal.read() is None


def test_gotcha_staged_lint_rejects_before_demo_or_live_write(tmp_path: Path):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root)
    skill_md = _write_gotcha_skill_md(manifest)
    text = skill_md.read_text(encoding="utf-8").replace(
        "- _None yet — append as failure modes are reported._",
        "- **Existing bounded warning.** This applies to the existing path. "
        "Keep the bounded remediation. Evidence: `evolution_test.py:1`.",
    )
    marker = "## See also"
    skill_md.write_text(text, encoding="utf-8")
    from scripts.skill_lint import _parse_skill_md

    initial = _parse_skill_md(skill_md.parent)
    assert initial is not None
    # Canonical rendering inserts one separating blank line before See also.
    filler_count = 199 - len(initial[1].splitlines())
    assert filler_count > 0
    text = text.replace(marker, ("Filler.\n" * filler_count) + marker)
    text = render_skill_md(load_skill_yaml(manifest), text)
    skill_md.write_text(text, encoding="utf-8")

    parsed = _parse_skill_md(skill_md.parent)
    assert parsed is not None
    assert len(parsed[1].splitlines()) == 200

    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    execution = _ExecutionAdapter()
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=execution,
        projection_adapter=_ProjectionAdapter(),
    )
    for event_id in ("defect-a", "defect-b", "defect-c"):
        ledger.append(
            _event(
                event_id,
                manifest,
                evidence_kind="ordinary",
                outcome="failed",
                error_kind="contract_failure",
                evidence_refs=["trace:evolution_test.py:1"],
            )
        )
    ledger.append(_event("conditional-success", manifest, evidence_kind="ordinary"))
    proposal = governance.propose_gotcha(
        target_skill="evolution-test",
        proposer="maintainer",
        reason="representation boundary test",
        support_event_ids=["defect-a", "defect-b", "defect-c"],
        entry={
            "lead": "Dense-only branch rejects sparse input",
            "condition": "This occurs when the selected matrix remains sparse.",
            "guidance": "Densify only the bounded slice before this branch.",
            "anchors": ["evolution_test.py:1"],
        },
    )
    before = skill_md.read_bytes()

    with pytest.raises(EvolutionRevalidationError, match="staged governed Gotcha"):
        governance.approve(
            proposal.proposal_id,
            approver="reviewer",
            reason="must fail before executing the demo",
        )

    assert skill_md.read_bytes() == before
    assert execution.calls == []
    assert store.get(proposal.proposal_id).status == "rolled_back"


def test_gotcha_runtime_consumption_failure_rolls_back_skill_md(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    (
        governance,
        _manifest,
        skill_md,
        _ledger,
        store,
        execution,
        projections,
        proposal,
    ) = _proposed_gotcha(tmp_path)
    before = skill_md.read_bytes()
    from omicsclaw.runtime.context import layers as context_layers

    monkeypatch.setattr(context_layers, "load_skill_context", lambda **_kwargs: "")

    with pytest.raises(EvolutionRevalidationError, match="runtime context"):
        governance.approve(
            proposal.proposal_id,
            approver="reviewer",
            reason="reviewed conditional failure",
        )

    assert skill_md.read_bytes() == before
    assert store.get(proposal.proposal_id).status == "rolled_back"
    assert execution.calls == ["evolution-test"]
    assert projections.calls == []
    assert governance.recovery_journal.read() is None


def test_reconcile_restores_process_interrupted_gotcha_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    (
        governance,
        manifest,
        skill_md,
        ledger,
        store,
        _execution,
        _projections,
        proposal,
    ) = _proposed_gotcha(tmp_path)
    manifest_before = manifest.read_bytes()
    skill_md_before = skill_md.read_bytes()
    from omicsclaw.runtime.context import layers as context_layers

    def terminate_during_runtime_consumption(**_kwargs):
        raise SystemExit("simulated process termination")

    monkeypatch.setattr(
        context_layers,
        "load_skill_context",
        terminate_during_runtime_consumption,
    )

    with pytest.raises(SystemExit, match="simulated process termination"):
        governance.approve(
            proposal.proposal_id,
            approver="reviewer",
            reason="reviewed conditional failure",
        )

    assert "Dense-only branch rejects sparse input" in skill_md.read_text(
        encoding="utf-8"
    )
    assert manifest.read_bytes() == manifest_before
    assert store.get(proposal.proposal_id).status == "pending"
    assert governance.recovery_journal.read() is not None

    restarted = SkillEvolutionGovernance(
        skills_root=governance.skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
    )
    reconciled = restarted.reconcile(
        operator="recovery-operator",
        reason="backend restarted during Gotcha approval",
    )

    assert reconciled == {
        "status": "rolled_back",
        "proposal_id": proposal.proposal_id,
        "action": "restored_interrupted_approval",
    }
    assert skill_md.read_bytes() == skill_md_before
    assert manifest.read_bytes() == manifest_before
    persisted = store.get(proposal.proposal_id)
    assert persisted.status == "rolled_back"
    assert persisted.before_hash == proposal.target_content_hash
    assert persisted.reconciled_by == "recovery-operator"
    assert restarted.recovery_journal.read() is None


def test_deprecation_refuses_replacement_without_earned_demo_validation(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root, skill_id="legacy-skill")
    _write_skill(skills_root, skill_id="replacement-skill", level="smoke-only")
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=EvolutionProposalStore(tmp_path / "proposals.jsonl"),
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
        minimum_deprecation_defects=1,
    )
    ledger.append(
        _event(
            "defect",
            manifest,
            outcome="failed",
            error_kind="script_defect",
            skill_id="legacy-skill",
        )
    )

    with pytest.raises(EvolutionRevalidationError, match="demo-validated"):
        governance.propose_deprecation(
            target_skill="legacy-skill",
            replacement_skill="replacement-skill",
            proposer="maintainer",
            reason="replacement is not ready",
            support_event_ids=["defect"],
        )


def test_approval_deprecates_source_and_revalidates_replacement_demo(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root, skill_id="legacy-skill")
    _write_skill(
        skills_root,
        skill_id="replacement-skill",
        level="demo-validated",
    )
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    execution = _ExecutionAdapter()
    projections = _ProjectionAdapter()
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=execution,
        projection_adapter=projections,
        minimum_deprecation_defects=2,
    )
    for event_id in ("defect-a", "defect-b"):
        ledger.append(
            _event(
                event_id,
                manifest,
                outcome="failed",
                error_kind="script_defect",
                skill_id="legacy-skill",
            )
        )
    proposal = governance.propose_deprecation(
        target_skill="legacy-skill",
        replacement_skill="replacement-skill",
        proposer="maintainer",
        reason="maintained replacement is ready",
        support_event_ids=["defect-a", "defect-b"],
    )

    receipt = governance.approve(
        proposal.proposal_id,
        approver="reviewer",
        reason="defects and replacement demo reviewed",
    )

    lifecycle = yaml.safe_load(manifest.read_text(encoding="utf-8"))["lifecycle"]
    assert receipt.status == "approved"
    assert lifecycle == {
        "status": "deprecated",
        "superseded_by": "replacement-skill",
    }
    assert execution.calls == ["replacement-skill"]
    assert projections.calls == [(str(skills_root), "legacy-skill")]


def test_deprecation_refuses_replacement_drift_before_target_write(tmp_path: Path):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root, skill_id="legacy-skill")
    replacement = _write_skill(
        skills_root,
        skill_id="replacement-skill",
        level="demo-validated",
    )
    before = manifest.read_bytes()
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=_ReplacementEditExecutionAdapter(replacement),
        projection_adapter=_ProjectionAdapter(),
        minimum_deprecation_defects=2,
    )
    for event_id in ("defect-a", "defect-b"):
        ledger.append(
            _event(
                event_id,
                manifest,
                outcome="failed",
                error_kind="script_defect",
                skill_id="legacy-skill",
            )
        )
    proposal = governance.propose_deprecation(
        target_skill="legacy-skill",
        replacement_skill="replacement-skill",
        proposer="maintainer",
        reason="maintained replacement",
        support_event_ids=["defect-a", "defect-b"],
    )

    with pytest.raises(EvolutionRevalidationError, match="rolled back"):
        governance.approve(
            proposal.proposal_id,
            approver="reviewer",
            reason="reviewed before replacement changed",
        )

    assert manifest.read_bytes() == before
    assert store.get(proposal.proposal_id).status == "rolled_back"


def test_deprecation_approval_rejects_target_source_drift_after_proposal(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root, skill_id="legacy-skill")
    _write_skill(
        skills_root,
        skill_id="replacement-skill",
        level="demo-validated",
    )
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
        minimum_deprecation_defects=1,
    )
    ledger.append(
        _event(
            "defect",
            manifest,
            outcome="failed",
            error_kind="script_defect",
            skill_id="legacy-skill",
        )
    )
    proposal = governance.propose_deprecation(
        target_skill="legacy-skill",
        replacement_skill="replacement-skill",
        proposer="maintainer",
        reason="replacement is ready",
        support_event_ids=["defect"],
    )
    manifest.with_name("legacy_skill.py").write_text(
        "REVISION = 2\n",
        encoding="utf-8",
    )
    before = manifest.read_bytes()

    with pytest.raises(EvolutionRevalidationError, match="rolled back"):
        governance.approve(
            proposal.proposal_id,
            approver="reviewer",
            reason="evidence reviewed",
        )

    assert manifest.read_bytes() == before
    assert store.get(proposal.proposal_id).status == "rolled_back"


def test_deprecation_approval_rejects_replacement_source_drift_during_demo(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root, skill_id="legacy-skill")
    replacement = _write_skill(
        skills_root,
        skill_id="replacement-skill",
        level="demo-validated",
    )
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=_ReplacementSourceEditExecutionAdapter(
            replacement.with_name("replacement_skill.py")
        ),
        projection_adapter=_ProjectionAdapter(),
        minimum_deprecation_defects=1,
    )
    ledger.append(
        _event(
            "defect",
            manifest,
            outcome="failed",
            error_kind="contract_failure",
            skill_id="legacy-skill",
        )
    )
    proposal = governance.propose_deprecation(
        target_skill="legacy-skill",
        replacement_skill="replacement-skill",
        proposer="maintainer",
        reason="replacement is ready",
        support_event_ids=["defect"],
    )
    before = manifest.read_bytes()

    with pytest.raises(EvolutionRevalidationError, match="rolled back"):
        governance.approve(
            proposal.proposal_id,
            approver="reviewer",
            reason="replacement demo reviewed",
        )

    assert manifest.read_bytes() == before
    assert store.get(proposal.proposal_id).status == "rolled_back"


def test_approved_deprecation_reaches_catalog_registry_and_automatic_routing(
    tmp_path: Path,
    monkeypatch,
):
    from omicsclaw.skill import capability_resolver as resolver_module
    from omicsclaw.skill.registry import OmicsRegistry

    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root, skill_id="legacy-skill")
    _write_skill(
        skills_root,
        skill_id="replacement-skill",
        level="demo-validated",
    )
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=EvolutionProposalStore(tmp_path / "proposals.jsonl"),
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=RegistryProjectionAdapter(),
        minimum_deprecation_defects=2,
    )
    for event_id in ("defect-a", "defect-b"):
        ledger.append(
            _event(
                event_id,
                manifest,
                outcome="failed",
                error_kind="contract_failure",
                skill_id="legacy-skill",
            )
        )
    proposal = governance.propose_deprecation(
        target_skill="legacy-skill",
        replacement_skill="replacement-skill",
        proposer="maintainer",
        reason="maintained replacement is ready",
        support_event_ids=["defect-a", "defect-b"],
    )

    governance.approve(
        proposal.proposal_id,
        approver="reviewer",
        reason="replacement demo and evidence reviewed",
    )

    catalog = json.loads((skills_root / "catalog.json").read_text(encoding="utf-8"))
    catalog_entries = {item["name"]: item for item in catalog["skills"]}
    registry = OmicsRegistry()
    registry.load_all(skills_root)
    monkeypatch.setattr(
        resolver_module,
        "ensure_registry_loaded",
        lambda: registry,
    )
    decision = resolver_module.resolve_capability("run legacy-skill")

    assert catalog_entries["legacy-skill"]["status"] == "deprecated"
    assert catalog_entries["legacy-skill"]["superseded_by"] == "replacement-skill"
    assert registry.canonical_skill_aliases() == ["replacement-skill"]
    assert decision.chosen_skill == "replacement-skill"
    assert any("superseded" in reason for reason in decision.reasoning)


def test_approval_promotes_one_level_and_records_evidence(tmp_path: Path):
    governance, manifest, ledger, store, execution, projections = _governance(tmp_path)
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]

    receipt = governance.approve(
        proposal.proposal_id,
        approver="local-human",
        reason="demo evidence reviewed",
    )

    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    assert receipt.status == "approved"
    assert raw["validation"]["level"] == "demo-validated"
    assert raw["validation"]["evidence"] == [
        f"evolution:{proposal.proposal_id}:events=demo"
    ]
    persisted = store.get(proposal.proposal_id)
    assert persisted.status == "approved"
    assert persisted.approved_by == "local-human"
    assert persisted.approval_reason == "demo evidence reviewed"
    assert execution.calls == ["evolution-test"]
    assert projections.calls == [(str(manifest.parents[2]), "evolution-test")]


def test_approval_demotes_one_level_only_after_fresh_demo_defect_confirmation(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root, level="demo-validated")
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    execution = _ExecutionAdapter()
    projections = _ProjectionAdapter()
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=execution,
        projection_adapter=projections,
    )
    ledger.append(
        _event(
            "demo-defect",
            manifest,
            evidence_kind="demo",
            outcome="failed",
            error_kind="contract_failure",
        )
    )
    proposal = governance.refresh()[0]

    receipt = governance.approve(
        proposal.proposal_id,
        approver="local-human",
        reason="reproduced demo contract failure",
    )

    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    assert receipt.status == "approved"
    assert raw["validation"]["level"] == "smoke-only"
    assert raw["validation"]["evidence"] == [
        f"evolution:{proposal.proposal_id}:demotion-events=demo-defect"
    ]
    assert execution.calls == ["defect:evolution-test"]
    assert projections.calls == [(str(skills_root), "evolution-test")]


def test_demotion_keeps_manifest_unchanged_when_demo_defect_does_not_reproduce(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root, level="demo-validated")
    before = manifest.read_bytes()
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=_ExecutionAdapter("demo defect did not reproduce"),
        projection_adapter=_ProjectionAdapter(),
    )
    ledger.append(
        _event(
            "demo-defect",
            manifest,
            evidence_kind="demo",
            outcome="failed",
            error_kind="contract_failure",
        )
    )
    proposal = governance.refresh()[0]

    with pytest.raises(EvolutionRevalidationError, match="rolled back"):
        governance.approve(
            proposal.proposal_id,
            approver="reviewer",
            reason="verify whether the defect persists",
        )

    assert manifest.read_bytes() == before
    assert store.get(proposal.proposal_id).status == "rolled_back"


def test_demotion_rolls_back_when_runtime_entry_changes_after_defect_validation(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root, level="demo-validated")
    source = manifest.with_name("evolution_test.py")
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=_SourceEditExecutionAdapter(source),
        projection_adapter=_ProjectionAdapter(),
    )
    ledger.append(
        _event(
            "demo-defect",
            manifest,
            evidence_kind="demo",
            outcome="failed",
            error_kind="contract_failure",
        )
    )
    proposal = governance.refresh()[0]
    before = manifest.read_bytes()

    with pytest.raises(EvolutionRevalidationError, match="rolled back"):
        governance.approve(
            proposal.proposal_id,
            approver="reviewer",
            reason="reproduced demo defect reviewed",
        )

    assert manifest.read_bytes() == before
    assert store.get(proposal.proposal_id).status == "rolled_back"


def test_default_projection_adapter_refreshes_catalog_and_compatibility_dag(tmp_path: Path):
    governance, manifest, ledger, _store, _execution, _projections = _governance(tmp_path)
    governance.projection_adapter = RegistryProjectionAdapter()
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]

    governance.approve(
        proposal.proposal_id,
        approver="local-human",
        reason="demo evidence reviewed",
    )

    catalog = json.loads((manifest.parents[2] / "catalog.json").read_text(encoding="utf-8"))
    graph = json.loads((manifest.parents[2] / "skill_dag.json").read_text(encoding="utf-8"))
    assert catalog["skill_count"] == 1
    assert catalog["skills"][0]["validation_level"] == "demo-validated"
    assert graph["summary"]["node_count"] == 1


def test_approval_marks_changed_manifest_stale_without_running_or_writing(tmp_path: Path):
    governance, manifest, ledger, store, execution, projections = _governance(tmp_path)
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    raw["summary"]["tags"] = ["concurrent-edit"]
    manifest.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    changed = manifest.read_bytes()

    with pytest.raises(EvolutionRevalidationError, match="stale"):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )

    assert manifest.read_bytes() == changed
    assert store.get(proposal.proposal_id).status == "stale"
    assert execution.calls == []
    assert projections.calls == []


def test_approval_marks_proposal_stale_when_exact_hash_defect_arrives_later(
    tmp_path: Path,
):
    governance, manifest, ledger, store, execution, projections = _governance(tmp_path)
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    ledger.append(
        _event(
            "later-defect",
            manifest,
            outcome="failed",
            error_kind="contract_failure",
        )
    )
    before = manifest.read_bytes()

    with pytest.raises(EvolutionRevalidationError, match="disqualifying"):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )

    assert manifest.read_bytes() == before
    assert store.get(proposal.proposal_id).status == "stale"
    assert execution.calls == []
    assert projections.calls == []


def test_final_manifest_compare_and_swap_preserves_edit_in_commit_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    governance, manifest, ledger, store, _execution, projections = _governance(tmp_path)
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    original_atomic_write = evolution_module._atomic_write
    injected = False

    def racing_write(
        path: Path,
        payload: bytes,
        *,
        mode: int,
        expected: bytes | None = None,
        swap_path: str | Path | None = None,
    ) -> None:
        nonlocal injected
        if not injected and Path(path) == manifest:
            injected = True
            raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
            raw["summary"]["tags"] = ["edit-in-final-cas-window"]
            original_atomic_write(
                manifest,
                yaml.safe_dump(raw, sort_keys=False).encode(),
                mode=mode,
            )
        if expected is None:
            original_atomic_write(Path(path), payload, mode=mode)
        else:
            original_atomic_write(
                Path(path),
                payload,
                mode=mode,
                expected=expected,
                swap_path=swap_path,
            )

    monkeypatch.setattr(evolution_module, "_atomic_write", racing_write)

    with pytest.raises(EvolutionRevalidationError, match="could not be committed"):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )

    record = governance.recovery_journal.read()
    assert record is not None
    swap_path = manifest.parents[2] / record["swap_relative_path"]
    swapped_external = yaml.safe_load(swap_path.read_text(encoding="utf-8"))
    assert swapped_external["summary"]["tags"] == ["edit-in-final-cas-window"]
    assert swapped_external["validation"]["level"] == "smoke-only"
    assert manifest.read_bytes() == record["after_bytes"]
    assert store.get(proposal.proposal_id).status == "pending"
    assert projections.calls == []


def test_approval_fails_closed_when_guarded_atomic_exchange_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    governance, manifest, ledger, store, _execution, projections = _governance(tmp_path)
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    before = manifest.read_bytes()
    monkeypatch.setattr(evolution_module, "_rename_exchange", lambda _left, _right: False)

    with pytest.raises(EvolutionRevalidationError, match="stale"):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )

    assert manifest.read_bytes() == before
    assert store.get(proposal.proposal_id).status == "stale"
    assert projections.calls == []


def test_approval_keeps_recovery_intent_when_commit_and_rollback_fsync_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    governance, manifest, ledger, store, _execution, projections = _governance(tmp_path)
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    before = manifest.read_bytes()
    manifest_directory_fsyncs = 0
    original_fsync_directory = evolution_module._fsync_directory

    def fail_manifest_directory_fsync(path: Path) -> None:
        nonlocal manifest_directory_fsyncs
        if Path(path) == manifest.parent:
            manifest_directory_fsyncs += 1
            raise OSError("simulated manifest directory fsync failure")
        original_fsync_directory(path)

    monkeypatch.setattr(
        evolution_module,
        "_fsync_directory",
        fail_manifest_directory_fsync,
    )

    with pytest.raises(EvolutionRevalidationError, match="could not be committed"):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )

    assert manifest_directory_fsyncs == 2
    assert manifest.read_bytes() == before
    assert store.get(proposal.proposal_id).status == "pending"
    assert governance.recovery_journal.read() is not None
    assert projections.calls == []


def test_queued_approval_cannot_clear_prior_durability_recovery_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    governance, manifest, ledger, store, execution, _projections = _governance(tmp_path)
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    before = manifest.read_bytes()
    both_prechecks_read = threading.Barrier(2)
    first_execution_started = threading.Event()
    read_counts: dict[str, int] = {}
    original_read = governance.recovery_journal.read
    original_validate_demo = execution.validate_demo
    original_fsync_directory = evolution_module._fsync_directory

    def coordinated_read():
        result = original_read()
        thread_name = threading.current_thread().name
        read_counts[thread_name] = read_counts.get(thread_name, 0) + 1
        if thread_name in {"approval-a", "approval-b"} and read_counts[thread_name] == 1:
            both_prechecks_read.wait(timeout=2)
            if thread_name == "approval-b":
                assert first_execution_started.wait(timeout=2)
        return result

    def signalling_validate_demo(skill_id: str) -> None:
        if threading.current_thread().name == "approval-a":
            first_execution_started.set()
        original_validate_demo(skill_id)

    def fail_first_approval_manifest_fsync(path: Path) -> None:
        if (
            threading.current_thread().name == "approval-a"
            and Path(path) == manifest.parent
        ):
            raise OSError("simulated manifest directory fsync failure")
        original_fsync_directory(path)

    monkeypatch.setattr(governance.recovery_journal, "read", coordinated_read)
    monkeypatch.setattr(execution, "validate_demo", signalling_validate_demo)
    monkeypatch.setattr(
        evolution_module,
        "_fsync_directory",
        fail_first_approval_manifest_fsync,
    )
    errors: dict[str, Exception] = {}

    def approve() -> None:
        try:
            governance.approve(
                proposal.proposal_id,
                approver=threading.current_thread().name,
                reason="concurrent recovery fence test",
            )
        except Exception as exc:
            errors[threading.current_thread().name] = exc

    thread_a = threading.Thread(target=approve, name="approval-a")
    thread_b = threading.Thread(target=approve, name="approval-b")
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)

    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert set(errors) == {"approval-a", "approval-b"}
    assert manifest.read_bytes() == before
    assert store.get(proposal.proposal_id).status == "pending"
    assert original_read() is not None


def test_rejection_rechecks_recovery_journal_inside_proposal_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    governance, manifest, ledger, store, _execution, _projections = _governance(tmp_path)
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    before = manifest.read_bytes()
    governance.recovery_journal.prepare(
        proposal=proposal,
        target_relative_path=manifest.relative_to(manifest.parents[2]).as_posix(),
        before=before,
        after=governance._promoted_manifest_bytes(before, proposal),
        mode=manifest.stat().st_mode,
        approver="approval-a",
        reason="interrupted approval",
    )
    original_read = governance.recovery_journal.read
    reads = 0

    def stale_then_current_read():
        nonlocal reads
        reads += 1
        if reads == 1:
            return None
        return original_read()

    monkeypatch.setattr(
        governance.recovery_journal,
        "read",
        stale_then_current_read,
    )

    with pytest.raises(EvolutionRevalidationError, match="requires reconciliation"):
        governance.reject(
            proposal.proposal_id,
            approver="reviewer",
            reason="must not overtake interrupted approval",
        )

    assert reads == 2
    assert store.get(proposal.proposal_id).status == "pending"
    assert original_read() is not None


def test_approval_rechecks_hash_after_demo_and_preserves_concurrent_edit(tmp_path: Path):
    governance, manifest, ledger, store, _execution, projections = _governance(tmp_path)
    concurrent = _ConcurrentEditExecutionAdapter(manifest)
    governance.execution_adapter = concurrent
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]

    with pytest.raises(EvolutionRevalidationError, match="stale"):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )

    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    assert raw["validation"]["level"] == "smoke-only"
    assert raw["summary"]["tags"] == ["edited-during-demo"]
    assert store.get(proposal.proposal_id).status == "stale"
    assert concurrent.calls == ["evolution-test"]
    assert projections.calls == []


def test_approval_rolls_back_when_runtime_entry_changes_after_demo_validation(
    tmp_path: Path,
):
    governance, manifest, ledger, store, _execution, projections = _governance(tmp_path)
    source = manifest.with_name("evolution_test.py")
    governance.execution_adapter = _SourceEditExecutionAdapter(source)
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    manifest_before = manifest.read_bytes()

    with pytest.raises(EvolutionRevalidationError, match="rolled back"):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )

    assert manifest.read_bytes() == manifest_before
    assert source.read_text(encoding="utf-8") == "REVISION = 2\n"
    assert store.get(proposal.proposal_id).status == "rolled_back"
    assert projections.calls == []


@pytest.mark.parametrize("source_kind", ["domain_lib", "project_runtime"])
def test_approval_final_fence_rolls_back_shared_execution_source_drift(
    tmp_path: Path,
    source_kind: str,
):
    governance, manifest, ledger, store, _execution, _projections = _governance(
        tmp_path
    )
    if source_kind == "domain_lib":
        source = governance.skills_root / "spatial" / "_lib" / "shared.py"
    else:
        source = governance.skills_root.parent / "omicsclaw" / "runtime.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("REVISION = 1\n", encoding="utf-8")
    governance.projection_adapter = _SourceEditProjectionAdapter(source)
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    manifest_before = manifest.read_bytes()

    with pytest.raises(EvolutionRevalidationError, match="rolled back"):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )

    assert manifest.read_bytes() == manifest_before
    assert source.read_text(encoding="utf-8") == "REVISION = 2\n"
    assert store.get(proposal.proposal_id).status == "rolled_back"


def test_failed_demo_revalidation_leaves_live_manifest_unchanged(tmp_path: Path):
    governance, manifest, ledger, store, execution, projections = _governance(
        tmp_path,
        execution_error="demo failed",
    )
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    before = manifest.read_bytes()

    with pytest.raises(EvolutionRevalidationError, match="demo failed"):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )

    assert manifest.read_bytes() == before
    persisted = store.get(proposal.proposal_id)
    assert persisted.status == "rolled_back"
    assert persisted.validation_error == (
        "execution_validation_failed:EvolutionRevalidationError"
    )
    assert str(tmp_path) not in persisted.validation_error
    assert execution.calls == ["evolution-test"]
    assert projections.calls == []


def test_failed_projection_restores_manifest_and_generated_files(tmp_path: Path):
    governance, manifest, ledger, store, execution, _projections = _governance(tmp_path)
    catalog = manifest.parents[2] / "catalog.json"
    catalog.write_text("original", encoding="utf-8")
    failing = _FailingProjectionAdapter()
    governance.projection_adapter = failing
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    before = manifest.read_bytes()

    with pytest.raises(EvolutionRevalidationError, match="projection refresh failed"):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )

    assert manifest.read_bytes() == before
    assert catalog.read_text(encoding="utf-8") == "original"
    persisted = store.get(proposal.proposal_id)
    assert persisted.status == "rolled_back"
    assert persisted.validation_error == (
        "retrieval_validation_failed:EvolutionRevalidationError"
    )
    assert str(tmp_path) not in persisted.validation_error
    assert execution.calls == ["evolution-test"]
    assert failing.calls == [(str(manifest.parents[2]), "evolution-test")]


def test_projection_rollback_fsyncs_directory_after_removing_new_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    governance, manifest, ledger, store, _execution, _projections = _governance(tmp_path)
    skills_root = manifest.parents[2]
    governance.projection_adapter = _FailingProjectionAdapter()
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    fsynced_directories: list[Path] = []
    original_fsync_directory = governance_module._fsync_directory

    def observed_fsync_directory(path: Path) -> None:
        fsynced_directories.append(Path(path))
        original_fsync_directory(path)

    monkeypatch.setattr(
        governance_module,
        "_fsync_directory",
        observed_fsync_directory,
    )

    with pytest.raises(EvolutionRevalidationError, match="projection refresh failed"):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )

    assert not (skills_root / "catalog.json").exists()
    assert store.get(proposal.proposal_id).status == "rolled_back"
    assert skills_root in fsynced_directories


def test_projection_rollback_failure_keeps_recovery_journal_for_reconcile(
    tmp_path: Path,
):
    governance, manifest, ledger, store, _execution, _projections = _governance(tmp_path)
    catalog = manifest.parents[2] / "catalog.json"
    catalog.write_text("original", encoding="utf-8")
    governance.projection_adapter = _FailingProjectionAdapter()
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    before = manifest.read_bytes()

    def fail_projection_rollback(_snapshot) -> None:
        raise OSError("simulated projection rollback failure")

    governance._restore_projection_files = fail_projection_rollback  # type: ignore[method-assign]

    with pytest.raises(EvolutionRevalidationError, match="rollback also failed"):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )

    assert manifest.read_bytes() == before
    assert store.get(proposal.proposal_id).status == "rolled_back"
    assert governance.recovery_journal.read() is not None
    governance.projection_adapter = _ProjectionAdapter()
    result = governance.reconcile(
        operator="recovery-operator",
        reason="repair failed projection rollback",
    )
    assert result["status"] == "rolled_back"
    assert governance.recovery_journal.read() is None


def test_defect_arriving_during_projection_refresh_rolls_back_approval(tmp_path: Path):
    governance, manifest, ledger, store, execution, _projections = _governance(tmp_path)
    catalog = manifest.parents[2] / "catalog.json"
    catalog.write_text("original", encoding="utf-8")
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    late_defect = _event(
        "late-defect-during-refresh",
        manifest,
        outcome="failed",
        error_kind="contract_failure",
    )
    governance.projection_adapter = _LateDefectProjectionAdapter(ledger, late_defect)
    before = manifest.read_bytes()

    with pytest.raises(EvolutionRevalidationError, match="disqualifying"):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )

    assert manifest.read_bytes() == before
    assert catalog.read_text(encoding="utf-8") == "original"
    persisted = store.get(proposal.proposal_id)
    assert persisted.status == "rolled_back"
    assert persisted.validation_error == (
        "retrieval_validation_failed:EvolutionRevalidationError"
    )
    assert execution.calls == ["evolution-test"]


def test_defect_arriving_at_final_approval_commit_rolls_back_approval(
    tmp_path: Path,
):
    governance, manifest, _ledger, store, execution, projections = _governance(tmp_path)
    late_defect = _event(
        "late-defect-at-commit",
        manifest,
        outcome="failed",
        error_kind="contract_failure",
    )
    ledger = _LateCommitDefectLedger(tmp_path / "late-commit-events.jsonl", late_defect)
    ledger.append(_event("demo", manifest))
    governance.ledger = ledger
    proposal = governance.refresh()[0]
    # Ordinary reads do not route through locked_events(); arming this seam
    # therefore injects the defect specifically at the final approval fence.
    ledger.inject_on_lock = True
    before = manifest.read_bytes()

    with pytest.raises(EvolutionRevalidationError, match="disqualifying"):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )

    assert ledger.injected
    assert manifest.read_bytes() == before
    persisted = store.get(proposal.proposal_id)
    assert persisted.status == "rolled_back"
    assert persisted.validation_error == (
        "proposal_state_validation_failed:EvolutionRevalidationError"
    )
    assert execution.calls == ["evolution-test"]
    assert projections.calls == [(str(manifest.parents[2]), "evolution-test")]


def test_concurrent_failed_approval_cannot_erase_prior_successful_projections(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    manifest_a = _write_skill(skills_root, skill_id="evolution-a")
    manifest_b = _write_skill(skills_root, skill_id="evolution-b")
    catalog = skills_root / "catalog.json"
    graph = skills_root / "skill_dag.json"
    catalog.write_text("initial", encoding="utf-8")
    graph.write_text("initial", encoding="utf-8")
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    ledger.append(_event("demo-a", manifest_a, skill_id="evolution-a"))
    ledger.append(_event("demo-b", manifest_b, skill_id="evolution-b"))
    b_snapshot_captured = threading.Event()
    a_finished = threading.Event()
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ConcurrentProjectionAdapter(),
    )
    proposals = {
        proposal.target_skill: proposal.proposal_id
        for proposal in governance.refresh()
    }
    original_snapshot = governance._snapshot_projection_files
    original_restore = governance._restore_projection_files
    b_restore_count = 0

    def observed_snapshot():
        snapshot = original_snapshot()
        if threading.current_thread().name == "approval-b":
            b_snapshot_captured.set()
            # Before the fix B snapshots outside the approval lock, so A can
            # finish while B waits. After the fix B owns the lock here; the
            # bounded wait expires, B rolls back, then A proceeds.
            a_finished.wait(timeout=0.5)
        return snapshot

    governance._snapshot_projection_files = observed_snapshot  # type: ignore[method-assign]

    def observed_restore(snapshot):
        nonlocal b_restore_count
        if threading.current_thread().name == "approval-b":
            b_restore_count += 1
            if b_restore_count > 1:
                # A second restore after the proposal lock is released can
                # race a following approval and erase its projection refresh.
                a_finished.wait(timeout=2)
        original_restore(snapshot)

    governance._restore_projection_files = observed_restore  # type: ignore[method-assign]
    errors: dict[str, Exception] = {}

    def approve(skill_id: str) -> None:
        try:
            governance.approve(
                proposals[skill_id],
                approver="local-human",
                reason=f"reviewed {skill_id}",
            )
            if skill_id == "evolution-a":
                a_finished.set()
        except Exception as exc:  # expected for B only
            errors[skill_id] = exc

    thread_b = threading.Thread(
        target=approve,
        args=("evolution-b",),
        name="approval-b",
    )
    thread_b.start()
    assert b_snapshot_captured.wait(timeout=2)
    thread_a = threading.Thread(
        target=approve,
        args=("evolution-a",),
        name="approval-a",
    )
    thread_a.start()
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)

    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert "evolution-a" not in errors
    assert isinstance(errors.get("evolution-b"), EvolutionRevalidationError)
    assert b_restore_count == 1
    assert yaml.safe_load(manifest_a.read_text(encoding="utf-8"))["validation"]["level"] == "demo-validated"
    assert yaml.safe_load(manifest_b.read_text(encoding="utf-8"))["validation"]["level"] == "smoke-only"
    assert store.get(proposals["evolution-a"]).status == "approved"
    assert store.get(proposals["evolution-b"]).status == "rolled_back"
    assert catalog.read_text(encoding="utf-8") == "a-complete"
    assert graph.read_text(encoding="utf-8") == "a-complete"


def test_persistent_proposal_store_failure_restores_files_but_cannot_record_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    governance, manifest, ledger, store, _execution, _projections = _governance(tmp_path)
    catalog = manifest.parents[2] / "catalog.json"
    catalog.write_text("original", encoding="utf-8")
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    before = manifest.read_bytes()
    original_append = store._append_unlocked

    def unavailable(_proposal):
        raise OSError("proposal store unavailable")

    monkeypatch.setattr(store, "_append_unlocked", unavailable)
    with pytest.raises(EvolutionRevalidationError, match="could not be committed"):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )

    monkeypatch.setattr(store, "_append_unlocked", original_append)
    assert manifest.read_bytes() == before
    assert catalog.read_text(encoding="utf-8") == "original"
    # The store outage also prevents persisting the rolled_back state. Files
    # fail safe, while the durable proposal remains pending for later repair.
    assert store.get(proposal.proposal_id).status == "pending"
    reconciled = governance.reconcile(
        operator="recovery-operator",
        reason="proposal store repaired",
    )
    assert reconciled["status"] == "rolled_back"
    assert store.get(proposal.proposal_id).status == "rolled_back"


def test_one_shot_approval_state_failure_persists_rolled_back_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    governance, manifest, ledger, store, _execution, _projections = _governance(tmp_path)
    catalog = manifest.parents[2] / "catalog.json"
    catalog.write_text("original", encoding="utf-8")
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    before = manifest.read_bytes()
    original_append = store._append_unlocked
    failed_once = False

    def fail_approved_once(next_state):
        nonlocal failed_once
        if next_state.status == "approved" and not failed_once:
            failed_once = True
            raise OSError("one-shot proposal state failure")
        original_append(next_state)

    monkeypatch.setattr(store, "_append_unlocked", fail_approved_once)
    with pytest.raises(EvolutionRevalidationError, match="rolled back"):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )

    assert manifest.read_bytes() == before
    assert catalog.read_text(encoding="utf-8") == "original"
    persisted = store.get(proposal.proposal_id)
    assert persisted.status == "rolled_back"
    assert persisted.validation_error == "proposal_state_validation_failed:OSError"


def test_reconcile_rolls_back_process_interrupted_pending_approval(tmp_path: Path):
    governance, manifest, ledger, store, _execution, _projections = _governance(tmp_path)
    catalog = manifest.parents[2] / "catalog.json"
    catalog.write_text("original", encoding="utf-8")
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    before = manifest.read_bytes()
    governance.projection_adapter = _ProcessExitProjectionAdapter()

    with pytest.raises(SystemExit, match="simulated process termination"):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )

    assert yaml.safe_load(manifest.read_text(encoding="utf-8"))["validation"][
        "level"
    ] == "demo-validated"
    assert store.get(proposal.proposal_id).status == "pending"

    restarted = SkillEvolutionGovernance(
        skills_root=manifest.parents[2],
        ledger=ledger,
        proposals=store,
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
    )
    reconciled = restarted.reconcile(
        operator="recovery-operator",
        reason="backend restarted during approval",
    )

    assert reconciled == {
        "status": "rolled_back",
        "proposal_id": proposal.proposal_id,
        "action": "restored_interrupted_approval",
    }
    assert manifest.read_bytes() == before
    persisted = store.get(proposal.proposal_id)
    assert persisted.status == "rolled_back"
    assert persisted.validation_error == "interrupted_approval_reconciled"
    assert persisted.reconciled_by == "recovery-operator"
    assert persisted.reconciliation_reason == "backend restarted during approval"
    assert restarted.reconcile(
        operator="recovery-operator",
        reason="confirm clean state",
    ) == {"status": "clean", "proposal_id": "", "action": "none"}


def test_reconcile_preserves_swapped_external_edit_after_commit_window_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    governance, manifest, ledger, store, _execution, _projections = _governance(tmp_path)
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    external = b"external: exchanged-before-cas-verification\n"
    real_exchange = evolution_module._rename_exchange
    crashed = False

    def exchange_then_crash(left: Path, right: Path) -> bool:
        nonlocal crashed
        if not crashed and Path(right) == manifest:
            crashed = True
            manifest.write_bytes(external)
            assert real_exchange(Path(left), Path(right))
            raise SystemExit("simulated termination after manifest exchange")
        return real_exchange(Path(left), Path(right))

    monkeypatch.setattr(evolution_module, "_rename_exchange", exchange_then_crash)
    with pytest.raises(SystemExit, match="after manifest exchange"):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )

    record = governance.recovery_journal.read()
    assert record is not None
    swap_path = manifest.parents[2] / record["swap_relative_path"]
    assert swap_path.read_bytes() == external
    promoted = record["after_bytes"]
    assert manifest.read_bytes() == promoted
    assert store.get(proposal.proposal_id).status == "pending"

    monkeypatch.setattr(evolution_module, "_rename_exchange", real_exchange)
    restarted = SkillEvolutionGovernance(
        skills_root=manifest.parents[2],
        ledger=ledger,
        proposals=store,
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
        recovery_journal=EvolutionRecoveryJournal(governance.recovery_journal.path),
    )
    result = restarted.reconcile(
        operator="recovery-operator",
        reason="inspect exchange witness",
    )

    assert result == {
        "status": "conflict",
        "proposal_id": proposal.proposal_id,
        "action": "manual_recovery_required",
    }
    assert manifest.read_bytes() == promoted
    assert swap_path.read_bytes() == external
    assert store.get(proposal.proposal_id).status == "pending"
    assert restarted.recovery_journal.read() is not None


def test_reconcile_restores_verified_predecessor_from_exchange_witness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    governance, manifest, ledger, store, _execution, _projections = _governance(tmp_path)
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    before = manifest.read_bytes()
    real_exchange = evolution_module._rename_exchange
    crashed = False

    def exchange_then_crash(left: Path, right: Path) -> bool:
        nonlocal crashed
        if not crashed and Path(right) == manifest:
            crashed = True
            assert real_exchange(Path(left), Path(right))
            raise SystemExit("simulated termination after verified exchange")
        return real_exchange(Path(left), Path(right))

    monkeypatch.setattr(evolution_module, "_rename_exchange", exchange_then_crash)
    with pytest.raises(SystemExit, match="after verified exchange"):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )

    record = governance.recovery_journal.read()
    assert record is not None
    swap_path = manifest.parents[2] / record["swap_relative_path"]
    assert swap_path.read_bytes() == before
    assert manifest.read_bytes() == record["after_bytes"]

    monkeypatch.setattr(evolution_module, "_rename_exchange", real_exchange)
    restarted = SkillEvolutionGovernance(
        skills_root=manifest.parents[2],
        ledger=ledger,
        proposals=store,
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
        recovery_journal=EvolutionRecoveryJournal(governance.recovery_journal.path),
    )
    result = restarted.reconcile(
        operator="recovery-operator",
        reason="restore verified predecessor",
    )

    assert result == {
        "status": "rolled_back",
        "proposal_id": proposal.proposal_id,
        "action": "restored_interrupted_approval",
    }
    assert manifest.read_bytes() == before
    assert not swap_path.exists()
    assert restarted.recovery_journal.read() is None
    assert store.get(proposal.proposal_id).status == "rolled_back"


def test_legacy_untracked_exchange_journal_fails_closed_on_live_after_bytes(
    tmp_path: Path,
):
    governance, manifest, ledger, store, _execution, _projections = _governance(tmp_path)
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    governance.projection_adapter = _ProcessExitProjectionAdapter()

    with pytest.raises(SystemExit, match="simulated process termination"):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )

    journal_path = governance.recovery_journal.path
    legacy = json.loads(journal_path.read_text(encoding="utf-8"))
    legacy["schema_version"] = 1
    legacy.pop("swap_relative_path")
    journal_path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    promoted = manifest.read_bytes()
    restarted = SkillEvolutionGovernance(
        skills_root=manifest.parents[2],
        ledger=ledger,
        proposals=store,
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
        recovery_journal=EvolutionRecoveryJournal(journal_path),
    )

    result = restarted.reconcile(
        operator="recovery-operator",
        reason="inspect legacy untracked exchange",
    )

    assert result == {
        "status": "conflict",
        "proposal_id": proposal.proposal_id,
        "action": "manual_recovery_required",
    }
    assert manifest.read_bytes() == promoted
    assert store.get(proposal.proposal_id).status == "pending"
    assert restarted.recovery_journal.read() is not None


def test_reconcile_rebuilds_projections_from_current_canonical_manifests(
    tmp_path: Path,
):
    governance, manifest, ledger, store, _execution, _projections = _governance(tmp_path)
    skills_root = manifest.parents[2]
    unrelated = _write_skill(skills_root, skill_id="unrelated-skill")
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    governance.projection_adapter = _ProcessExitProjectionAdapter()

    with pytest.raises(SystemExit, match="simulated process termination"):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )

    unrelated_payload = yaml.safe_load(unrelated.read_text(encoding="utf-8"))
    unrelated_payload["summary"]["trigger_keywords"] = [
        "changed-after-interrupted-approval"
    ]
    unrelated.write_text(
        yaml.safe_dump(unrelated_payload, sort_keys=False),
        encoding="utf-8",
    )
    restarted = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=RegistryProjectionAdapter(),
    )

    result = restarted.reconcile(
        operator="recovery-operator",
        reason="rebuild from canonical manifests",
    )

    assert result["status"] == "rolled_back"
    catalog = json.loads((skills_root / "catalog.json").read_text(encoding="utf-8"))
    entries = {entry["name"]: entry for entry in catalog["skills"]}
    assert entries["unrelated-skill"]["trigger_keywords"] == [
        "changed-after-interrupted-approval"
    ]
    assert entries["evolution-test"]["validation_level"] == "smoke-only"
    graph = json.loads((skills_root / "skill_dag.json").read_text(encoding="utf-8"))
    assert {node["skill"] for node in graph["nodes"]} == {
        "evolution-test",
        "unrelated-skill",
    }


def test_reconcile_preserves_external_manifest_drift_for_manual_recovery(
    tmp_path: Path,
):
    governance, manifest, ledger, store, _execution, _projections = _governance(tmp_path)
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    governance.projection_adapter = _ProcessExitProjectionAdapter()
    with pytest.raises(SystemExit):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )
    external = b"external: owner-edit\n"
    manifest.write_bytes(external)

    restarted = SkillEvolutionGovernance(
        skills_root=manifest.parents[2],
        ledger=ledger,
        proposals=store,
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
    )
    result = restarted.reconcile(
        operator="recovery-operator",
        reason="inspect interrupted approval",
    )

    assert result == {
        "status": "conflict",
        "proposal_id": proposal.proposal_id,
        "action": "manual_recovery_required",
    }
    assert manifest.read_bytes() == external
    assert store.get(proposal.proposal_id).status == "pending"
    with pytest.raises(EvolutionRevalidationError, match="requires reconciliation"):
        restarted.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="must not bypass conflict",
        )


def test_reconcile_returns_conflict_for_external_edit_in_restore_cas_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    governance, manifest, ledger, store, _execution, _projections = _governance(tmp_path)
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    governance.projection_adapter = _ProcessExitProjectionAdapter()
    with pytest.raises(SystemExit):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )
    record = governance.recovery_journal.read()
    assert record is not None
    external = b"external: restore-cas-window\n"
    original_atomic_write = governance_module._atomic_write

    def racing_atomic_write(
        path: Path,
        payload: bytes,
        *,
        mode: int,
        expected: bytes | None = None,
        swap_path: str | Path | None = None,
    ) -> None:
        manifest.write_bytes(external)
        original_atomic_write(
            Path(path),
            payload,
            mode=mode,
            expected=expected,
            swap_path=swap_path,
        )

    monkeypatch.setattr(governance_module, "_atomic_write", racing_atomic_write)
    restarted = SkillEvolutionGovernance(
        skills_root=manifest.parents[2],
        ledger=ledger,
        proposals=store,
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
    )

    result = restarted.reconcile(
        operator="recovery-operator",
        reason="inspect restore CAS race",
    )

    assert result == {
        "status": "conflict",
        "proposal_id": proposal.proposal_id,
        "action": "manual_recovery_required",
    }
    swap_path = manifest.parents[2] / record["swap_relative_path"]
    assert manifest.read_bytes() == record["before_bytes"]
    assert swap_path.read_bytes() == external
    assert store.get(proposal.proposal_id).status == "pending"
    assert restarted.recovery_journal.read() is not None


def test_reconcile_finalizes_durable_approval_after_cleanup_interruption(
    tmp_path: Path,
):
    governance, manifest, ledger, store, _execution, projections = _governance(tmp_path)
    journal = _ExitOnClearRecoveryJournal(tmp_path / "approval-inflight.json")
    governance.recovery_journal = journal
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]

    with pytest.raises(SystemExit, match="after proposal commit"):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )

    assert store.get(proposal.proposal_id).status == "approved"
    promoted = manifest.read_bytes()
    restarted = SkillEvolutionGovernance(
        skills_root=manifest.parents[2],
        ledger=ledger,
        proposals=store,
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=projections,
        recovery_journal=EvolutionRecoveryJournal(journal.path),
    )
    result = restarted.reconcile(
        operator="recovery-operator",
        reason="finish durable approval",
    )

    assert result == {
        "status": "approved",
        "proposal_id": proposal.proposal_id,
        "action": "finalized_committed_approval",
    }
    assert manifest.read_bytes() == promoted
    assert store.get(proposal.proposal_id).status == "approved"


def test_reconcile_preserves_durable_deprecation_when_target_source_drifted(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    manifest = _write_skill(skills_root, skill_id="legacy-skill")
    _write_skill(
        skills_root,
        skill_id="replacement-skill",
        level="demo-validated",
    )
    ledger = SkillHealthLedger(tmp_path / "events.jsonl")
    store = EvolutionProposalStore(tmp_path / "proposals.jsonl")
    journal = _ExitOnClearRecoveryJournal(tmp_path / "approval-inflight.json")
    governance = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
        minimum_deprecation_defects=1,
        recovery_journal=journal,
    )
    ledger.append(
        _event(
            "defect",
            manifest,
            outcome="failed",
            error_kind="script_defect",
            skill_id="legacy-skill",
        )
    )
    proposal = governance.propose_deprecation(
        target_skill="legacy-skill",
        replacement_skill="replacement-skill",
        proposer="maintainer",
        reason="maintained replacement is ready",
        support_event_ids=["defect"],
    )

    with pytest.raises(SystemExit, match="after proposal commit"):
        governance.approve(
            proposal.proposal_id,
            approver="reviewer",
            reason="defect and replacement demo reviewed",
        )

    assert store.get(proposal.proposal_id).status == "approved"
    promoted = manifest.read_bytes()
    source = manifest.with_name("legacy_skill.py")
    source.write_text("REVISION = 2\n", encoding="utf-8")
    restarted = SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=ledger,
        proposals=store,
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=_ProjectionAdapter(),
        recovery_journal=EvolutionRecoveryJournal(journal.path),
    )

    result = restarted.reconcile(
        operator="recovery-operator",
        reason="inspect durable deprecation source drift",
    )

    assert result == {
        "status": "approved",
        "proposal_id": proposal.proposal_id,
        "action": "finalized_committed_approval",
    }
    assert manifest.read_bytes() == promoted
    assert source.read_text(encoding="utf-8") == "REVISION = 2\n"
    assert store.get(proposal.proposal_id).status == "approved"
    assert restarted.recovery_journal.read() is None


@pytest.mark.parametrize("drift_kind", ["before", "external"])
def test_reconcile_reports_conflict_for_durable_approval_manifest_drift(
    tmp_path: Path,
    drift_kind: str,
):
    governance, manifest, ledger, store, _execution, projections = _governance(tmp_path)
    journal = _ExitOnClearRecoveryJournal(tmp_path / "approval-inflight.json")
    governance.recovery_journal = journal
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    before = manifest.read_bytes()

    with pytest.raises(SystemExit, match="after proposal commit"):
        governance.approve(
            proposal.proposal_id,
            approver="local-human",
            reason="demo evidence reviewed",
        )

    assert store.get(proposal.proposal_id).status == "approved"
    drift = before if drift_kind == "before" else b"external: owner-edit\n"
    manifest.write_bytes(drift)
    restarted = SkillEvolutionGovernance(
        skills_root=manifest.parents[2],
        ledger=ledger,
        proposals=store,
        execution_adapter=_ExecutionAdapter(),
        projection_adapter=projections,
        recovery_journal=EvolutionRecoveryJournal(journal.path),
    )

    result = restarted.reconcile(
        operator="recovery-operator",
        reason="inspect durable approval drift",
    )

    assert result == {
        "status": "conflict",
        "proposal_id": proposal.proposal_id,
        "action": "manual_recovery_required",
    }
    assert manifest.read_bytes() == drift
    assert store.get(proposal.proposal_id).status == "approved"
    assert restarted.recovery_journal.read() is not None


def test_reconcile_rejects_journal_target_not_bound_to_proposal(tmp_path: Path):
    governance, manifest, ledger, store, _execution, _projections = _governance(tmp_path)
    ledger.append(_event("demo", manifest))
    proposal = governance.refresh()[0]
    other = manifest.parent.parent / "other-skill" / "skill.yaml"
    other.parent.mkdir(parents=True)
    other_before = b"other: before\n"
    other_after = b"other: after\n"
    other.write_bytes(other_after)
    governance.recovery_journal.prepare(
        proposal=proposal,
        target_relative_path=other.relative_to(manifest.parents[2]).as_posix(),
        before=other_before,
        after=other_after,
        mode=other.stat().st_mode,
        approver="local-human",
        reason="tampered target",
    )

    with pytest.raises(EvolutionRevalidationError, match="does not match proposal"):
        governance.reconcile(
            operator="recovery-operator",
            reason="inspect recovery journal",
        )

    assert other.read_bytes() == other_after
    assert store.get(proposal.proposal_id).status == "pending"


def test_snapshot_contains_aggregated_health_and_no_raw_paths(tmp_path: Path):
    governance, manifest, ledger, _store, _execution, _projections = _governance(tmp_path)
    ledger.append(_event("demo", manifest))
    governance.refresh()

    snapshot = governance.snapshot()
    serialized = json.dumps(snapshot, sort_keys=True)

    assert snapshot["health"][0]["success_count"] == 1
    assert snapshot["proposals"][0]["target_skill"] == "evolution-test"
    assert str(tmp_path) not in serialized


# ── ADR 0074 §8.2 — stage-one merge_candidate advisories ──────────────────────


def _write_capability_skill(
    root: Path,
    *,
    skill_id: str,
    load_when: str,
    trigger_keywords: list[str] | None = None,
    domain: str = "spatial",
    level: str = "smoke-only",
    status: str = "mvp",
) -> Path:
    """A skill whose capability text (load_when + keywords) is caller-controlled."""
    skill_dir = root / domain / skill_id
    skill_dir.mkdir(parents=True)
    script_name = skill_id.replace("-", "_") + ".py"
    (skill_dir / script_name).write_text(
        "if __name__ == '__main__':\n    pass\n", encoding="utf-8"
    )
    manifest = {
        "schema_version": 2,
        "id": skill_id,
        "name": skill_id,
        "domain": domain,
        "version": "1.0.0",
        "summary": {
            "load_when": load_when,
            "trigger_keywords": trigger_keywords or [],
        },
        "runtime": {"entry": script_name},
        "type": "leaf",
        "lifecycle": {"status": status},
        "validation": {"level": level},
    }
    path = skill_dir / "skill.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return path


_CLUSTER_A = (
    "cluster spatial transcriptomics spots into tissue domains via leiden "
    "louvain graph modularity neighborhood"
)
_CLUSTER_B = (
    "cluster spatial transcriptomics spots into tissue domains via leiden "
    "louvain graph modularity neighborhood resolution"
)
_VELOCITY = (
    "estimate rna velocity latent time from spliced unspliced counts dynamical "
    "trajectory"
)


def _merge_governance(tmp_path: Path, skills_root: Path) -> SkillEvolutionGovernance:
    return SkillEvolutionGovernance(
        skills_root=skills_root,
        ledger=SkillHealthLedger(tmp_path / "events.jsonl"),
        proposals=EvolutionProposalStore(tmp_path / "proposals.jsonl"),
        execution_adapter=_ExecutionAdapter(),
    )


def _merge_candidates(created: list) -> list:
    return [p for p in created if p.kind == "merge_candidate"]


def test_refresh_flags_near_duplicate_skills_as_advisory_merge_candidate(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    _write_capability_skill(
        skills_root, skill_id="spatial-cluster-a", load_when=_CLUSTER_A
    )
    _write_capability_skill(
        skills_root, skill_id="spatial-cluster-b", load_when=_CLUSTER_B
    )
    governance = _merge_governance(tmp_path, skills_root)

    merges = _merge_candidates(governance.refresh())

    assert len(merges) == 1
    candidate = merges[0]
    assert candidate.kind == "merge_candidate"
    assert candidate.status == "draft"
    # Evidence is the static manifest overlap, not ledger events.
    assert candidate.support_event_ids == []
    change = candidate.proposed_change
    assert change["advisory_only"] is True
    assert change["action"] == "review_capability_overlap"
    assert change["overlap_skills"] == ["spatial-cluster-a", "spatial-cluster-b"]
    assert change["domain"] == "spatial"
    assert change["shared_token_count"] >= 6
    assert change["similarity"] >= 0.5
    # The advisory points at the two-stage resolution and forbids concatenation.
    assert "deprecation" in change["resolution_path"].lower()
    assert "never concatenate" in change["resolution_path"].lower()
    # It is surfaced through the existing additive snapshot proposals array.
    kinds = {p["kind"] for p in governance.snapshot()["proposals"]}
    assert "merge_candidate" in kinds

    # Re-running refresh is idempotent: the same pair is not re-proposed.
    assert _merge_candidates(governance.refresh()) == []
    latest = [
        p for p in governance.proposals.list_latest() if p.kind == "merge_candidate"
    ]
    assert len(latest) == 1


def test_merge_candidate_excludes_distinct_cross_domain_and_non_routable(
    tmp_path: Path,
):
    # Distinct capabilities in the same domain: no overlap advisory.
    root_a = tmp_path / "distinct"
    _write_capability_skill(root_a, skill_id="spatial-cluster-a", load_when=_CLUSTER_A)
    _write_capability_skill(root_a, skill_id="spatial-velocity", load_when=_VELOCITY)
    assert _merge_candidates(_merge_governance(tmp_path / "a", root_a).refresh()) == []

    # Identical capability text but different domains: domains are disjoint
    # capability spaces, so cross-domain overlap is never a merge signal.
    root_b = tmp_path / "cross"
    _write_capability_skill(
        root_b, skill_id="spatial-cluster-a", load_when=_CLUSTER_A, domain="spatial"
    )
    _write_capability_skill(
        root_b, skill_id="genomics-cluster-a", load_when=_CLUSTER_A, domain="genomics"
    )
    assert _merge_candidates(_merge_governance(tmp_path / "b", root_b).refresh()) == []

    # Overlapping pair where one side is non-routable: no routable pair forms.
    root_c = tmp_path / "nonroutable"
    _write_capability_skill(
        root_c, skill_id="spatial-cluster-a", load_when=_CLUSTER_A, status="mvp"
    )
    _write_capability_skill(
        root_c, skill_id="spatial-cluster-b", load_when=_CLUSTER_B, status="draft"
    )
    assert _merge_candidates(_merge_governance(tmp_path / "c", root_c).refresh()) == []


def test_merge_candidate_is_non_approvable_and_deprecation_stays_two_stage(
    tmp_path: Path,
):
    skills_root = tmp_path / "skills"
    _write_capability_skill(
        skills_root, skill_id="spatial-cluster-a", load_when=_CLUSTER_A
    )
    _write_capability_skill(
        skills_root, skill_id="spatial-cluster-b", load_when=_CLUSTER_B
    )
    # A routable-but-unvalidated replacement and a non-routable replacement.
    _write_capability_skill(
        skills_root,
        skill_id="spatial-cluster-unified",
        load_when=_VELOCITY,
        level="smoke-only",
        status="mvp",
    )
    _write_capability_skill(
        skills_root,
        skill_id="spatial-cluster-retired",
        load_when=_VELOCITY,
        level="demo-validated",
        status="draft",
    )
    governance = _merge_governance(tmp_path, skills_root)
    merges = _merge_candidates(governance.refresh())
    assert len(merges) == 1

    # Stage one is advisory: the merge_candidate itself can never be approved,
    # so it cannot retire, hide, or rewrite either Skill.
    with pytest.raises(EvolutionRevalidationError):
        governance.approve(
            merges[0].proposal_id, approver="local-human", reason="merge them"
        )

    # Stage two — the ONLY retirement path — is the replacement-backed
    # deprecation, which refuses an under-validated replacement ...
    with pytest.raises(EvolutionRevalidationError, match="demo-validated"):
        governance.propose_deprecation(
            target_skill="spatial-cluster-a",
            replacement_skill="spatial-cluster-unified",
            proposer="local-human",
            reason="merge overlap into one skill",
            support_event_ids=[],
        )
    # ... and a non-routable replacement.
    with pytest.raises(EvolutionRevalidationError, match="routable"):
        governance.propose_deprecation(
            target_skill="spatial-cluster-a",
            replacement_skill="spatial-cluster-retired",
            proposer="local-human",
            reason="merge overlap into one skill",
            support_event_ids=[],
        )


# ── ADR 0074 §8.1 — protocol_revision advisories (coverage gap / invalid) ─────


def _write_protocol_skill(root, *, skill_id, level, protocols, make_entries=True,
                          domain="spatial", status="mvp"):
    """A skill with caller-controlled level + declared protocols (entries optional)."""
    skill_dir = root / domain / skill_id
    skill_dir.mkdir(parents=True)
    script = skill_id.replace("-", "_") + ".py"
    (skill_dir / script).write_text("if __name__ == '__main__':\n    pass\n", encoding="utf-8")
    if make_entries:
        for proto in protocols:
            entry = skill_dir / proto["entry"]
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.write_text("# protocol\n", encoding="utf-8")
    manifest = {
        "schema_version": 2, "id": skill_id, "name": skill_id, "domain": domain,
        "version": "1.0.0",
        "summary": {"load_when": f"{skill_id} distinct unique isolated capability {skill_id}",
                    "trigger_keywords": [skill_id]},
        "runtime": {"entry": script}, "type": "leaf",
        "lifecycle": {"status": status}, "validation": {"level": level, "protocols": protocols},
    }
    (skill_dir / "skill.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return skill_dir


def _protocol_revisions(created):
    return [p for p in created if p.kind == "protocol_revision"]


def test_protocol_revision_flags_coverage_gap_and_is_advisory(tmp_path):
    skills_root = tmp_path / "skills"
    _write_protocol_skill(skills_root, skill_id="gap-skill",
                          level="fixture-validated", protocols=[])
    governance = _merge_governance(tmp_path, skills_root)

    revisions = _protocol_revisions(governance.refresh())
    assert len(revisions) == 1
    candidate = revisions[0]
    assert candidate.kind == "protocol_revision" and candidate.status == "draft"
    assert candidate.support_event_ids == []
    change = candidate.proposed_change
    assert change["advisory_only"] is True
    assert change["problem_kind"] == "coverage_gap"
    assert change["declared_level"] == "fixture-validated"
    assert "fixture" in change["required_protocol_kinds"]
    assert "benchmark" in change["required_protocol_kinds"]
    assert change["action"] == "declare_evaluation_protocol"
    # surfaced through the additive snapshot; advisory cannot be approved.
    assert "protocol_revision" in {p["kind"] for p in governance.snapshot()["proposals"]}
    with pytest.raises(EvolutionRevalidationError):
        governance.approve(candidate.proposal_id, approver="local-human", reason="fix it")
    # idempotent
    assert _protocol_revisions(governance.refresh()) == []


def test_protocol_revision_flags_invalid_protocol_entry(tmp_path):
    skills_root = tmp_path / "skills"
    _write_protocol_skill(
        skills_root, skill_id="broken-skill", level="fixture-validated",
        protocols=[{"id": "f2", "kind": "fixture", "entry": "tests/missing.py"}],
        make_entries=False,
    )
    revisions = _protocol_revisions(_merge_governance(tmp_path, skills_root).refresh())
    # Declares a fixture protocol (coverage satisfied) but its entry cannot load.
    assert len(revisions) == 1
    change = revisions[0].proposed_change
    assert change["problem_kind"] == "protocol_invalid"
    assert change["action"] == "repair_evaluation_protocol"
    assert [p["id"] for p in change["invalid_protocols"]] == ["f2"]


def test_protocol_revision_ignores_covered_low_level_and_non_routable(tmp_path):
    # Covered: fixture-validated WITH a valid fixture protocol -> no advisory.
    root_a = tmp_path / "covered"
    _write_protocol_skill(root_a, skill_id="ok-skill", level="fixture-validated",
                          protocols=[{"id": "f1", "kind": "fixture", "entry": "tests/f1.py"}])
    assert _protocol_revisions(_merge_governance(tmp_path / "a", root_a).refresh()) == []

    # Low levels never gap (demo-validated is earned from demo events; smoke-only
    # needs nothing).
    root_b = tmp_path / "low"
    _write_protocol_skill(root_b, skill_id="demo-skill", level="demo-validated", protocols=[])
    _write_protocol_skill(root_b, skill_id="smoke-skill", level="smoke-only", protocols=[])
    assert _protocol_revisions(_merge_governance(tmp_path / "b", root_b).refresh()) == []

    # Non-routable is out of scope even with a gap.
    root_c = tmp_path / "draft"
    _write_protocol_skill(root_c, skill_id="draft-skill", level="fixture-validated",
                          protocols=[], status="draft")
    assert _protocol_revisions(_merge_governance(tmp_path / "c", root_c).refresh()) == []
