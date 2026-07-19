from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DESIGN = ROOT / "docs" / "design" / "conversational-control-plane.md"


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _section(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def test_ingress_policy_is_backend_owned_not_surface_supplied():
    design = DESIGN.read_text(encoding="utf-8")
    ingress = _section(
        design,
        "### 4.2 Process-local attachment byte source",
        "### 4.3 Reply targets and source namespaces",
    )

    assert "class InboundAttachmentSource(Protocol):" in ingress
    assert "Backend-owned dependencies" in ingress
    assert "A Surface cannot provide," in ingress
    assert "replace, or parameterize either dependency" in ingress
    assert "class IngressPorts" not in ingress
    assert "owner_authenticator" not in ingress
    assert "workspace_resolver:" not in ingress


def test_resource_contract_matches_skill_metadata_and_forbids_nested_global_acquire():
    design = DESIGN.read_text(encoding="utf-8")
    run_contract = _section(
        design,
        "### 7.1 Run submission contract",
        "### 7.2 Run acceptance order",
    )

    for field in (
        "cpu_cores: UInt32",
        "memory_mib: UInt64",
        "gpu_devices: UInt32",
        "threads: UInt32",
        "temporary_disk_mib: UInt64",
    ):
        assert field in run_contract

    for stale_field in (
        "PositiveDecimal",
        "memory_bytes",
        "worker_threads",
        "temporary_disk_bytes",
        "execution_timeout_ms",
    ):
        assert stale_field not in run_contract

    assert "aggregate: ExecutionResourceBudgetV1" in run_contract
    adr = _read(
        "docs/adr/0062-reserve-one-governed-resource-envelope-for-dynamic-runs.md"
    )
    assert "MUST NOT submit a global resource ticket" in adr
    assert "Run-local allocations" in adr


def test_delivery_contract_serializes_per_target_and_suppresses_suffix():
    design = DESIGN.read_text(encoding="utf-8")
    schema = _section(
        design, "### 5.2 `control.db` schema", "### 5.3 Control transactions"
    )
    outbox = _section(design, "### 8.2 Delivery Pump", "### 8.3 Capacity")

    assert "target_sequence" in schema
    assert "deliveries_target_sequence" in schema
    assert "'unknown','suppressed'" in schema
    assert "blocked_by_item_id" in schema
    assert "at most one active provider call per\nReply Target" in outbox
    assert "atomically suppress every higher" in outbox
    assert "Only then may the next target sequence proceed" in outbox


def test_project_memory_scope_and_projection_fence_are_closed_decisions():
    design = DESIGN.read_text(encoding="utf-8")
    context = _read("docs/CONTEXT.md")
    open_questions = _section(
        context, "## Open questions", "## Resolved (kept here for tombstone)"
    )

    assert "CREATE TABLE project_projection_intents" in design
    assert "Workspace scope holds observations" in design
    assert "pre-existing frozen Project Projection Intent" in design
    assert "`_auto_capture_dataset` policy" not in open_questions
    assert "Project Projection Intent" in context
    assert "Workspace dataset observation" in context


def test_architecture_ledger_distinguishes_scheme_4_cutover_from_remaining_target():
    architecture = _read("docs/ARCHITECTURE.md")
    normalized = " ".join(architecture.split())

    assert "# OmicsClaw Architecture Ledger" in architecture
    assert "## Accepted target at a glance" in architecture
    assert "The diagram below is the accepted control-plane target" in architecture
    assert (
        "Prompt-toolkit and single-shot CLI, the Desktop text/multipart-image paths, and "
        "Owner-only Telegram text/single-photo input now use those slices "
        "through `ControlRuntime`"
    ) in normalized
    assert "Scheme 3 adds the independent Attachment Store" in normalized
    assert "Scheme 4 adds the strict Desktop multipart Adapter" in normalized
    assert "persistent text Delivery Outbox" in normalized
    assert "all other Channel Adapters remain uncut" in normalized
    assert "independent canonical Transcript Store" in normalized
    assert "CLI attachment input, every File Reference path" in normalized
    assert "profile-driven one-shot importer" in normalized
    assert "runtime never falls back to that legacy store" in normalized


def test_root_exact_demo_scope_slice_is_documented_without_overclaiming() -> None:
    readme = _read("README.md")
    context = _read("docs/CONTEXT.md")
    architecture = _read("docs/ARCHITECTURE.md")
    agents = _read("AGENTS.md")
    adr_0056 = _read(
        "docs/adr/0056-keep-unassigned-runs-outside-project-lifecycle-and-freeze-run-scope.md"
    )
    adr_0061 = _read(
        "docs/adr/0061-separate-run-dispatch-from-process-local-resource-scheduling.md"
    )

    for document in (readme, context, architecture, agents):
        assert "--demo --project" in document
        assert "--demo --no-project" in document
    assert "project_not_found" in adr_0056
    assert "project_archived" in adr_0056
    assert "explicit Unassigned never reads current navigation" in adr_0056
    assert "broader caller migration remains incomplete" in adr_0056
    assert "all-kind convergence incomplete" in adr_0061
    assert "four production submission Adapters" in adr_0056


def test_scheme_4_claims_whole_turn_only_for_cut_over_paths():
    design = DESIGN.read_text(encoding="utf-8")
    adr_0050 = _read(
        "docs/adr/0050-serialize-turns-per-conversation-with-bounded-fifo.md"
    )
    adr_0051 = _read(
        "docs/adr/0051-opaque-turn-id-and-durable-non-replayable-turn-receipt.md"
    )
    implementation_0050 = _section(adr_0050, "## Implementation", "## Context")
    implementation_0051 = _section(adr_0051, "## Implementation", "## Context")
    normalized_0050 = " ".join(implementation_0050.split())
    normalized_0051 = " ".join(implementation_0051.split())
    normalized_design = " ".join(design.split())

    assert "`ControlRuntime` now composes the Sequencer" in normalized_0050
    assert "one active lease per Conversation" in normalized_0050
    assert (
        "single-writer guarantee is active for canonical CLI, Desktop text/multipart-image and "
        "Telegram text/single-photo Conversations"
    ) in normalized_0050
    assert "non-cut-over Channel handlers retain legacy" in normalized_0050
    assert "`pop_next`" not in implementation_0050
    assert "local `queued|running` Receipts" in normalized_0051
    assert "the FIFO is not rebuilt" in normalized_0051
    assert "Attachment reconciliation runs before interrupted-Turn" in normalized_0051
    assert "Scheme 1 (2026-07-16)" in normalized_design
    assert "terminal candidate -> terminal Receipt" in normalized_design
    assert "prompt-toolkit/single-shot CLI" in normalized_design
    assert "Desktop text path" in normalized_design
    assert "strict Desktop multipart images" in normalized_design
    assert "Scheme 3 (2026-07-16)" in normalized_design
    assert "Scheme 4 (2026-07-17)" in normalized_design
    assert "Textual TUI" in normalized_design
    assert "CLI attachment input, every File Reference" in normalized_design
    assert "non-cut-over Adapters" in normalized_design
    assert "Attachment Store" in normalized_design
    assert "`authoritative_ingress=true`" in design
    assert "`durable_ingress_idempotency=true`" in design


def test_adr_0059_documents_the_narrow_attachment_production_slice():
    adr = _read(
        "docs/adr/0059-store-accepted-inbound-attachments-as-immutable-per-turn-records.md"
    )
    implementation = _section(adr, "## Implementation", "## Context")
    normalized = " ".join(implementation.split())

    assert (
        "Production vertical slices implemented (2026-07-16 and 2026-07-17)"
        in normalized
    )
    assert "checksum-pinned `attachments.db`" in normalized
    assert "content-free" in normalized
    assert "accept_async(raw_inbound, attachment_source=...)" in normalized
    assert "configured-Owner Telegram" in normalized
    assert "Desktop `POST /v1/turns` multipart image submission" in normalized
    assert "without opening the upload source" in normalized
    assert (
        "media groups, documents, audio, video and outbound media remain fail-closed"
        in normalized
    )
    assert "Not implemented" not in implementation


def test_scheme_1_documents_canonical_transcript_cutover_and_observer_boundary():
    readme = _read("README.md")
    context = _read("docs/CONTEXT.md")
    design = DESIGN.read_text(encoding="utf-8")
    adr_0040 = _read("docs/adr/0040-restart-resilient-transcript-persistence.md")
    adr_0052 = _read(
        "docs/adr/0052-bind-retried-ingress-to-one-turn-and-resume-observation.md"
    )
    adr_0054 = _read(
        "docs/adr/0054-persist-authoritative-control-state-in-backend-exclusive-sqlite.md"
    )

    assert (
        "terminal candidate -> Receipt + Transcript ref -> promotion -> Event" in readme
    )
    assert "不代表 ADR 0042–0068 全量完成" in readme
    for term in (
        "**Canonical Transcript Store**",
        "**Terminal Transcript Candidate**",
        "**Turn Terminal Transcript Reference**",
        "**Turn Event Hub**",
        "**Transcript Migration Profile**",
    ):
        assert term in context
    assert "profile-driven one-shot importer" in design
    assert "`plan/apply/verify`" in design
    assert "runtime has no legacy Transcript fallback" in design
    assert "no runtime dual-read, dual-write or" in adr_0040
    assert "`source_request_id_required=true`" in adr_0052
    assert "disconnect or renderer" in adr_0052.lower()
    assert "one opaque Transcript Store binding" in " ".join(adr_0054.split())


def test_effective_decision_chain_includes_reclosure_adrs():
    index = _read("docs/adr/README.md")
    design = DESIGN.read_text(encoding="utf-8")

    for number in ("0062", "0063", "0064"):
        assert f"ADR {number}" in index
        assert f"ADR {number}" in design

    for relative_path in (
        "docs/adr/0062-reserve-one-governed-resource-envelope-for-dynamic-runs.md",
        "docs/adr/0063-serialize-outbound-deliveries-per-reply-target.md",
        "docs/adr/0064-scope-scientific-memory-and-fence-project-projections.md",
    ):
        assert "Accepted (2026-07-15)." in _read(relative_path)
