from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import re
import threading

import pytest

from omicsclaw.attachments import SourceAttachmentDescriptorV1
from omicsclaw.control import (
    ControlIntegrityError,
    ControlStateRepository,
    IngressBackendConfig,
    IngressNormalizer,
    RawContentBlockV1,
    RawInboundV1,
    TurnAcceptanceStatus,
    TurnSequencer,
    TurnTerminalOutcome,
    semantic_fingerprint_v1,
)


_DESKTOP_NAMESPACE = "desktop/v1/test-installation/owner"
_CLI_NAMESPACE = "cli/v1/test-installation/owner"


class _TestSurface:
    """In-process Surface harness; it never imports a production Surface."""

    def __init__(self, normalizer: IngressNormalizer) -> None:
        self._normalizer = normalizer

    def raw(
        self,
        request_id: str,
        *,
        text: str | None = "hello",
        surface: str = "desktop",
        source_namespace: str | None = None,
        subject: dict[str, str] | None = None,
        project_command: dict[str, str] | None = None,
        requested_options: dict[str, object] | None = None,
    ) -> RawInboundV1:
        if surface == "channel":
            reply_target = {
                "schema_version": 1,
                "kind": "channel",
                "adapter": "telegram",
                "account_namespace": "primary",
                "destination_id": "chat-7",
            }
        else:
            reply_target = {
                "schema_version": 1,
                "kind": surface,
                "installation_id": "test-installation",
                "profile_id": "owner",
                "slot": "main",
            }
        return RawInboundV1(
            schema_version=1,
            surface=surface,
            source_namespace=(
                source_namespace
                or (
                    "channel/telegram/v1/primary"
                    if surface == "channel"
                    else f"{surface}/v1/test-installation/owner"
                )
            ),
            source_request_id=request_id,
            external_subject=subject,
            reply_target=reply_target,
            content=(
                (RawContentBlockV1(kind="text", text=text),) if text is not None else ()
            ),
            project_command=project_command,
            requested_options=requested_options or {},
        )

    def submit(self, raw: RawInboundV1):
        return self._normalizer.accept(raw)


def _harness(
    tmp_path,
    *,
    total_capacity: int = 4,
    per_conversation_capacity: int = 2,
    owner_identities: dict[str, frozenset[str]] | None = None,
):
    repository = ControlStateRepository(tmp_path)
    sequencer = TurnSequencer(
        repository,
        max_entries_per_conversation=per_conversation_capacity,
        max_entries_total=total_capacity,
    )
    sequencer.reconcile_startup()
    normalizer = IngressNormalizer(
        repository,
        sequencer,
        IngressBackendConfig(
            workspace_id="workspace-test",
            trusted_local_source_namespaces={
                "desktop": frozenset({_DESKTOP_NAMESPACE}),
                "cli": frozenset({_CLI_NAMESPACE}),
            },
            owner_identities=owner_identities or {},
        ),
    )
    return repository, sequencer, _TestSurface(normalizer)


def _execute_next(
    sequencer: TurnSequencer,
    conversation_id: str,
):
    captured = None

    async def worker(context):
        nonlocal captured
        captured = context.envelope
        return TurnTerminalOutcome("succeeded")

    result = asyncio.run(sequencer.execute_next(conversation_id, worker))
    return captured if result.state == "executed" else None


def test_text_ingress_uses_backend_owned_opaque_ids_and_builds_envelope(tmp_path):
    repository, sequencer, surface = _harness(tmp_path)
    try:
        result = surface.submit(
            surface.raw(
                "request-1",
                requested_options={
                    "model_override": "local-model",
                    "max_tokens": 2_048,
                },
            )
        )

        assert result.status is TurnAcceptanceStatus.ACCEPTED
        assert re.fullmatch(r"[0-9a-f]{32}", result.turn_id)
        assert re.fullmatch(r"[0-9a-f]{32}", result.conversation_id)
        assert repository.get_turn(result.turn_id).status == "queued"
        envelope = _execute_next(sequencer, result.conversation_id)
        assert envelope is not None
        assert envelope.turn_id == result.turn_id
        assert envelope.conversation_id == result.conversation_id
        assert envelope.content == ({"kind": "text", "text": "hello"},)
        assert envelope.requested_options == {
            "model_override": "local-model",
            "max_tokens": 2_048,
        }
    finally:
        repository.close()


def test_duplicate_and_conflict_do_not_enqueue_another_envelope(tmp_path):
    repository, sequencer, surface = _harness(tmp_path)
    try:
        raw = surface.raw("request-1")
        accepted = surface.submit(raw)
        duplicate = surface.submit(raw)
        conflict = surface.submit(
            replace(
                raw,
                content=(RawContentBlockV1(kind="text", text="changed"),),
            )
        )

        assert accepted.status is TurnAcceptanceStatus.ACCEPTED
        assert duplicate.status is TurnAcceptanceStatus.DUPLICATE
        assert duplicate.turn_id == accepted.turn_id
        assert conflict.status is TurnAcceptanceStatus.CONFLICT
        assert conflict.turn_id == accepted.turn_id
        assert (
            _execute_next(sequencer, accepted.conversation_id).turn_id
            == accepted.turn_id
        )
        assert _execute_next(sequencer, accepted.conversation_id) is None
    finally:
        repository.close()


def test_duplicate_bypasses_full_capacity_and_concurrent_delivery_queues_once(
    tmp_path,
):
    repository, admission_queue, surface = _harness(
        tmp_path, total_capacity=1, per_conversation_capacity=1
    )
    try:
        raw = surface.raw("same-request")
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _index: surface.submit(raw), range(8)))

        assert (
            sum(result.status is TurnAcceptanceStatus.ACCEPTED for result in results)
            == 1
        )
        assert (
            sum(result.status is TurnAcceptanceStatus.DUPLICATE for result in results)
            == 7
        )
        assert len({result.turn_id for result in results}) == 1
        conversation_id = results[0].conversation_id
        assert (
            _execute_next(admission_queue, conversation_id).turn_id
            == results[0].turn_id
        )
        assert _execute_next(admission_queue, conversation_id) is None
    finally:
        repository.close()


def test_channel_delivery_capacity_check_is_serialized_across_real_threads(
    tmp_path,
    monkeypatch,
):
    repository = ControlStateRepository(tmp_path)
    admission_queue = TurnSequencer(
        repository,
        max_entries_per_conversation=4,
        max_entries_total=4,
    )
    admission_queue.reconcile_startup()
    normalizer = IngressNormalizer(
        repository,
        admission_queue,
        IngressBackendConfig(
            workspace_id="workspace-test",
            owner_identities={
                "channel/telegram/primary/telegram_user": frozenset({"owner"})
            },
            channel_delivery_enabled=True,
            max_outstanding_deliveries_total=1,
            max_outstanding_deliveries_per_account=1,
        ),
    )
    surface = _TestSurface(normalizer)
    original_capacity_check = repository.has_delivery_capacity
    first_inside_capacity = threading.Event()
    release_first = threading.Event()
    second_inside_capacity = threading.Event()
    calls_lock = threading.Lock()
    capacity_calls = 0

    def controlled_capacity_check(*args, **kwargs):
        nonlocal capacity_calls
        with calls_lock:
            capacity_calls += 1
            call_number = capacity_calls
        if call_number == 1:
            first_inside_capacity.set()
            assert release_first.wait(timeout=2)
        else:
            second_inside_capacity.set()
        return original_capacity_check(*args, **kwargs)

    monkeypatch.setattr(
        repository,
        "has_delivery_capacity",
        controlled_capacity_check,
    )
    first_raw = surface.raw(
        "channel-capacity-first",
        surface="channel",
        subject={"kind": "telegram_user", "value": "owner"},
    )
    second_raw = surface.raw(
        "channel-capacity-second",
        surface="channel",
        subject={"kind": "telegram_user", "value": "owner"},
    )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(surface.submit, first_raw)
            assert first_inside_capacity.wait(timeout=1)
            second = executor.submit(surface.submit, second_raw)
            assert not second_inside_capacity.wait(timeout=0.05)
            release_first.set()
            results = (first.result(timeout=2), second.result(timeout=2))

        assert second_inside_capacity.is_set()
        assert [result.status for result in results].count(
            TurnAcceptanceStatus.ACCEPTED
        ) == 1
        rejected = [
            result
            for result in results
            if result.status is TurnAcceptanceStatus.REJECTED
        ]
        assert len(rejected) == 1
        assert rejected[0].code == "delivery_backpressure"
    finally:
        release_first.set()
        repository.close()


def test_two_normalizers_share_admission_order_through_envelope_commit(tmp_path):
    first_at_commit = threading.Event()
    release_first_commit = threading.Event()

    class BlockingCommitSequencer(TurnSequencer):
        def _commit(self, reservation, envelope) -> None:
            if envelope.content[0]["text"] == "first":
                first_at_commit.set()
                assert release_first_commit.wait(timeout=5)
            super()._commit(reservation, envelope)

    repository = ControlStateRepository(tmp_path)
    admission_queue = BlockingCommitSequencer(
        repository,
        max_entries_per_conversation=2,
        max_entries_total=2,
    )
    admission_queue.reconcile_startup()
    config = IngressBackendConfig(
        workspace_id="workspace-test",
        trusted_local_source_namespaces={"desktop": frozenset({_DESKTOP_NAMESPACE})},
    )
    surfaces = (
        _TestSurface(IngressNormalizer(repository, admission_queue, config)),
        _TestSurface(IngressNormalizer(repository, admission_queue, config)),
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(
                surfaces[0].submit,
                surfaces[0].raw("request-first", text="first"),
            )
            assert first_at_commit.wait(timeout=5)
            second_future = executor.submit(
                surfaces[1].submit,
                surfaces[1].raw("request-second", text="second"),
            )
            with pytest.raises(TimeoutError):
                second_future.result(timeout=0.05)
            release_first_commit.set()
            results = (
                first_future.result(timeout=5),
                second_future.result(timeout=5),
            )

        assert all(result.status is TurnAcceptanceStatus.ACCEPTED for result in results)
        assert results[0].conversation_id == results[1].conversation_id
        assert len(repository.list_conversations()) == 1
        queued = [
            _execute_next(admission_queue, results[0].conversation_id),
            _execute_next(admission_queue, results[0].conversation_id),
        ]
        assert [envelope.content[0]["text"] for envelope in queued] == [
            "first",
            "second",
        ]
        assert [envelope.turn_id for envelope in queued] == [
            results[0].turn_id,
            results[1].turn_id,
        ]
    finally:
        release_first_commit.set()
        repository.close()


def test_enqueue_failure_terminalizes_the_accepted_turn_without_replay(tmp_path):
    class FailingAdmissionQueue(TurnSequencer):
        def _commit(self, reservation, envelope) -> None:
            raise RuntimeError("injected enqueue failure")

    repository = ControlStateRepository(tmp_path)
    admission_queue = FailingAdmissionQueue(
        repository,
        max_entries_per_conversation=1,
        max_entries_total=1,
    )
    admission_queue.reconcile_startup()
    normalizer = IngressNormalizer(
        repository,
        admission_queue,
        IngressBackendConfig(
            workspace_id="workspace-test",
            trusted_local_source_namespaces={
                "desktop": frozenset({_DESKTOP_NAMESPACE})
            },
        ),
    )
    surface = _TestSurface(normalizer)
    try:
        raw = surface.raw("enqueue-failure")
        accepted = surface.submit(raw)
        duplicate = surface.submit(raw)

        assert accepted.status is TurnAcceptanceStatus.ACCEPTED
        assert accepted.code == "dispatch_enqueue_failed"
        assert repository.get_turn(accepted.turn_id).status == "failed"
        assert duplicate.status is TurnAcceptanceStatus.DUPLICATE
        assert duplicate.turn_id == accepted.turn_id
        assert _execute_next(admission_queue, accepted.conversation_id) is None
    finally:
        repository.close()


def test_partial_envelope_commit_is_removed_after_durable_compensation(tmp_path):
    class PartialCommitSequencer(TurnSequencer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.fail_once = True

        def _commit(self, reservation, envelope) -> None:
            if self.fail_once:
                self.fail_once = False
                with self._lock:
                    assert reservation._state == "reserved"
                    reservation._state = "committing"
                    reservation._turn_id = envelope.turn_id
                    self._queues[reservation.conversation_id].append(envelope)
                    raise RuntimeError("index write failed after append")
            super()._commit(reservation, envelope)

    repository = ControlStateRepository(tmp_path)
    sequencer = PartialCommitSequencer(
        repository,
        max_entries_per_conversation=1,
        max_entries_total=1,
    )
    sequencer.reconcile_startup()
    surface = _TestSurface(
        IngressNormalizer(
            repository,
            sequencer,
            IngressBackendConfig(
                workspace_id="workspace-test",
                trusted_local_source_namespaces={
                    "desktop": frozenset({_DESKTOP_NAMESPACE})
                },
            ),
        )
    )
    try:
        compensated = surface.submit(surface.raw("partial-commit"))
        successor = surface.submit(surface.raw("successor"))
        envelope = _execute_next(sequencer, successor.conversation_id)

        assert compensated.status is TurnAcceptanceStatus.ACCEPTED
        assert compensated.code == "dispatch_enqueue_failed"
        assert repository.get_turn(compensated.turn_id).status == "failed"
        assert successor.status is TurnAcceptanceStatus.ACCEPTED
        assert envelope.turn_id == successor.turn_id
        assert repository.get_turn(successor.turn_id).status == "succeeded"
        assert _execute_next(sequencer, successor.conversation_id) is None
    finally:
        repository.close()


def test_enqueue_and_terminal_compensation_failure_quarantines_until_restart(tmp_path):
    class FailingAdmissionQueue(TurnSequencer):
        def _commit(self, reservation, envelope) -> None:
            raise RuntimeError("injected enqueue failure")

    def fault_hook(name: str) -> None:
        if name == "terminalize_turn.before_commit":
            raise RuntimeError("injected terminal compensation failure")

    repository = ControlStateRepository(tmp_path, fault_hook=fault_hook)
    admission_queue = FailingAdmissionQueue(
        repository,
        max_entries_per_conversation=1,
        max_entries_total=1,
    )
    admission_queue.reconcile_startup()
    surface = _TestSurface(
        IngressNormalizer(
            repository,
            admission_queue,
            IngressBackendConfig(
                workspace_id="workspace-test",
                trusted_local_source_namespaces={
                    "desktop": frozenset({_DESKTOP_NAMESPACE})
                },
            ),
        )
    )
    raw = surface.raw("combined-failure")
    try:
        with pytest.raises(ControlIntegrityError, match="Conversation quarantined"):
            surface.submit(raw)

        duplicate = surface.submit(raw)
        blocked_same_conversation = surface.submit(surface.raw("must-not-bypass"))
        secondary_raw = replace(
            surface.raw("capacity-stays-reserved"),
            reply_target={
                "schema_version": 1,
                "kind": "desktop",
                "installation_id": "test-installation",
                "profile_id": "owner",
                "slot": "secondary",
            },
        )
        blocked_other_conversation = surface.submit(secondary_raw)

        assert duplicate.status is TurnAcceptanceStatus.DUPLICATE
        assert repository.get_turn(duplicate.turn_id).status == "queued"
        assert blocked_same_conversation.status is TurnAcceptanceStatus.REJECTED
        assert blocked_same_conversation.code == "turn_execution_unavailable"
        assert blocked_other_conversation.status is TurnAcceptanceStatus.REJECTED
        assert blocked_other_conversation.code == "turn_backpressure"
        with pytest.raises(ControlIntegrityError, match="quarantined"):
            asyncio.run(
                admission_queue.execute_next(
                    duplicate.conversation_id,
                    lambda _context: asyncio.sleep(
                        0, result=TurnTerminalOutcome("succeeded")
                    ),
                )
            )
    finally:
        repository.close()

    with ControlStateRepository(tmp_path) as recovered_repository:
        recovered_sequencer = TurnSequencer(
            recovered_repository,
            max_entries_per_conversation=1,
            max_entries_total=1,
        )
        reconciled = recovered_sequencer.reconcile_startup()

        assert reconciled.interrupted_turn_ids == (duplicate.turn_id,)
        assert recovered_repository.get_turn(duplicate.turn_id).status == "interrupted"


def test_base_exception_during_terminal_compensation_still_quarantines(tmp_path):
    class FailingAdmissionQueue(TurnSequencer):
        def _commit(self, reservation, envelope) -> None:
            raise RuntimeError("injected enqueue failure")

    def fault_hook(name: str) -> None:
        if name == "terminalize_turn.before_commit":
            raise KeyboardInterrupt

    repository = ControlStateRepository(tmp_path, fault_hook=fault_hook)
    sequencer = FailingAdmissionQueue(
        repository,
        max_entries_per_conversation=2,
        max_entries_total=2,
    )
    sequencer.reconcile_startup()
    surface = _TestSurface(
        IngressNormalizer(
            repository,
            sequencer,
            IngressBackendConfig(
                workspace_id="workspace-test",
                trusted_local_source_namespaces={
                    "desktop": frozenset({_DESKTOP_NAMESPACE})
                },
            ),
        )
    )
    try:
        with pytest.raises(KeyboardInterrupt):
            surface.submit(surface.raw("base-exception"))

        blocked = surface.submit(surface.raw("must-not-bypass"))
        assert blocked.status is TurnAcceptanceStatus.REJECTED
        assert blocked.code == "turn_execution_unavailable"
    finally:
        repository.close()


def test_raw_and_envelope_detach_and_deep_freeze_nested_transport_data(tmp_path):
    reply_target = {
        "schema_version": 1,
        "kind": "desktop",
        "installation_id": "test-installation",
        "profile_id": "owner",
        "slot": "main",
    }
    raw = RawInboundV1(
        schema_version=1,
        surface="desktop",
        source_namespace=_DESKTOP_NAMESPACE,
        source_request_id="immutable-request",
        reply_target=reply_target,
        content=(RawContentBlockV1(kind="text", text="hello"),),
        requested_options={"model_override": "local-model"},
    )
    reply_target["slot"] = "mutated-by-caller"

    assert raw.reply_target["slot"] == "main"
    with pytest.raises(TypeError):
        raw.reply_target["slot"] = "cannot-mutate"  # type: ignore[index]

    repository, admission_queue, surface = _harness(tmp_path)
    try:
        result = surface.submit(raw)
        envelope = _execute_next(admission_queue, result.conversation_id)
        assert envelope is not None
        with pytest.raises(TypeError):
            envelope.content[0]["text"] = "cannot-mutate"  # type: ignore[index]
        projection = envelope.to_json_dict()
        projection["content"][0]["text"] = "detached-copy"
        assert envelope.content[0]["text"] == "hello"
    finally:
        repository.close()


def test_semantic_fingerprint_ignores_request_identity_and_transport_facts():
    base = RawInboundV1(
        schema_version=1,
        surface="desktop",
        source_namespace=_DESKTOP_NAMESPACE,
        source_request_id="request-a",
        reply_target={
            "schema_version": 1,
            "kind": "desktop",
            "installation_id": "test-installation",
            "profile_id": "owner",
            "slot": "main",
        },
        content=(RawContentBlockV1(kind="text", text="same intent"),),
        transport_facts={"client_version": "one"},
    )
    same_semantics = replace(
        base,
        source_request_id="request-b",
        transport_facts={"client_version": "two"},
    )
    changed_semantics = replace(
        base,
        content=(RawContentBlockV1(kind="text", text="different intent"),),
    )

    assert semantic_fingerprint_v1(base) == semantic_fingerprint_v1(same_semantics)
    assert semantic_fingerprint_v1(base) != semantic_fingerprint_v1(changed_semantics)


def test_same_target_reuses_conversation_and_capacity_rejection_has_no_receipt(
    tmp_path,
):
    repository, sequencer, surface = _harness(tmp_path, per_conversation_capacity=2)
    try:
        first = surface.submit(surface.raw("request-1", text="first"))
        second = surface.submit(surface.raw("request-2", text="second"))
        blocked_raw = surface.raw("request-3", text="third")
        blocked = surface.submit(blocked_raw)

        assert first.status is TurnAcceptanceStatus.ACCEPTED
        assert second.status is TurnAcceptanceStatus.ACCEPTED
        assert second.conversation_id == first.conversation_id
        assert blocked.status is TurnAcceptanceStatus.REJECTED
        assert blocked.code == "turn_backpressure"
        assert len(repository.list_conversations()) == 1
        assert (
            repository.inspect_ingress(
                surface=blocked_raw.surface,
                source_namespace=blocked_raw.source_namespace,
                source_request_id=blocked_raw.source_request_id,
                fingerprint_version=1,
                fingerprint_sha256=semantic_fingerprint_v1(blocked_raw),
            ).state
            == "novel"
        )

        assert _execute_next(sequencer, first.conversation_id).turn_id == first.turn_id
        assert _execute_next(sequencer, first.conversation_id).turn_id == second.turn_id
        retry = surface.submit(blocked_raw)
        assert retry.status is TurnAcceptanceStatus.ACCEPTED
    finally:
        repository.close()


def test_zero_capacity_creates_neither_conversation_nor_receipt(tmp_path):
    repository, _sequencer, surface = _harness(tmp_path, total_capacity=0)
    try:
        raw = surface.raw("request-1")
        result = surface.submit(raw)

        assert result.status is TurnAcceptanceStatus.REJECTED
        assert result.code == "turn_backpressure"
        assert repository.list_conversations() == ()
        assert (
            repository.inspect_ingress(
                surface=raw.surface,
                source_namespace=raw.source_namespace,
                source_request_id=raw.source_request_id,
                fingerprint_version=1,
                fingerprint_sha256=semantic_fingerprint_v1(raw),
            ).state
            == "novel"
        )
    finally:
        repository.close()


def test_local_namespace_and_channel_owner_admission_fail_closed(tmp_path):
    repository, sequencer, surface = _harness(
        tmp_path,
        owner_identities={
            "channel/telegram/primary/telegram_user": frozenset({"owner-7"})
        },
    )
    try:
        untrusted_local = surface.submit(
            surface.raw(
                "local-denied",
                source_namespace="desktop/v1/untrusted/profile",
            )
        )
        wrong_owner = surface.submit(
            surface.raw(
                "channel-denied",
                surface="channel",
                subject={"kind": "telegram_user", "value": "other"},
            )
        )
        owner_but_delivery_unavailable = surface.submit(
            surface.raw(
                "channel-owner",
                surface="channel",
                subject={"kind": "telegram_user", "value": "owner-7"},
            )
        )

        assert untrusted_local.code == "owner_denied"
        assert wrong_owner.code == "owner_denied"
        assert owner_but_delivery_unavailable.code == "channel_delivery_not_supported"
        assert repository.list_conversations() == ()
        assert _execute_next(sequencer, "missing") is None
    finally:
        repository.close()


def test_channel_owner_scope_binds_adapter_account_and_source_namespace(tmp_path):
    repository, _admission_queue, surface = _harness(
        tmp_path,
        owner_identities={
            "channel/telegram/primary/telegram_user": frozenset({"owner-7"})
        },
    )
    try:
        wrong_namespace = surface.submit(
            surface.raw(
                "wrong-namespace",
                surface="channel",
                source_namespace="channel/telegram/v1/secondary",
                subject={"kind": "telegram_user", "value": "owner-7"},
            )
        )
        slack_target = replace(
            surface.raw(
                "cross-adapter",
                surface="channel",
                subject={"kind": "telegram_user", "value": "owner-7"},
            ),
            source_namespace="channel/slack/v1/primary",
            reply_target={
                "schema_version": 1,
                "kind": "channel",
                "adapter": "slack",
                "account_namespace": "primary",
                "destination_id": "channel-7",
            },
        )
        cross_adapter = surface.submit(slack_target)

        assert wrong_namespace.code == "owner_denied"
        assert cross_adapter.code == "owner_denied"
        assert repository.list_conversations() == ()
    finally:
        repository.close()


def test_unsupported_files_and_unsafe_requested_options_are_rejected(tmp_path):
    repository, _sequencer, surface = _harness(tmp_path)
    try:
        with pytest.raises(TypeError, match="SourceAttachmentDescriptorV1"):
            replace(surface.raw("malformed-attachment"), attachments=({"name": "x"},))
        attachment = surface.submit(
            replace(
                surface.raw("attachment"),
                attachments=(
                    SourceAttachmentDescriptorV1(
                        schema_version=1,
                        ordinal=0,
                        source_attachment_id="photo-1",
                        display_name="photo.jpg",
                        declared_media_type="image/jpeg",
                        declared_size=4,
                        declared_sha256=None,
                    ),
                ),
            )
        )
        file_selection = surface.submit(
            replace(surface.raw("selection"), file_selections=({"path": "x"},))
        )
        credentials = surface.submit(
            surface.raw("credential", requested_options={"api_key": "secret"})
        )
        structured_option = surface.submit(
            surface.raw(
                "structured",
                requested_options={"model_override": {"id": "x"}},
            )
        )
        nested_credentials = surface.submit(
            surface.raw(
                "nested-credential",
                requested_options={"provider_options": {"api_key": "secret"}},
            )
        )
        disguised_credentials = surface.submit(
            surface.raw(
                "disguised-credential",
                requested_options={"provider_options": {"api-key": "secret"}},
            )
        )
        oversized_max_tokens = surface.submit(
            surface.raw(
                "oversized-max-tokens",
                requested_options={"max_tokens": 0x1_0000_0000},
            )
        )

        assert attachment.code == "attachments_not_supported"
        assert file_selection.code == "file_selections_not_supported"
        assert credentials.code == "invalid_inbound"
        assert structured_option.code == "invalid_inbound"
        assert nested_credentials.code == "invalid_inbound"
        assert disguised_credentials.code == "invalid_inbound"
        assert oversized_max_tokens.code == "invalid_inbound"
        assert repository.list_conversations() == ()
    finally:
        repository.close()


def test_backend_authority_configuration_is_strict_and_fail_closed(tmp_path):
    with pytest.raises(ValueError, match="workspace_id"):
        IngressBackendConfig(workspace_id=7)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="scope must be a string"):
        IngressBackendConfig(
            workspace_id="workspace-test",
            owner_identities={7: frozenset({"owner"})},  # type: ignore[dict-item]
        )
    with pytest.raises(ValueError, match="values must be non-empty"):
        IngressBackendConfig(
            workspace_id="workspace-test",
            owner_identities={
                "channel/telegram/primary/user": frozenset({7})  # type: ignore[arg-type]
            },
        )
    with pytest.raises(ValueError, match="must be a boolean"):
        IngressBackendConfig(
            workspace_id="workspace-test",
            channel_delivery_enabled=1,  # type: ignore[arg-type]
        )
    with ControlStateRepository(tmp_path) as repository:
        with pytest.raises(ValueError, match="must be a non-negative integer"):
            TurnSequencer(
                repository,
                max_entries_per_conversation=True,  # type: ignore[arg-type]
                max_entries_total=1,
            )


def test_bounded_parse_rejects_oversized_identity_and_content_before_acceptance(
    tmp_path,
):
    repository = ControlStateRepository(tmp_path)
    admission_queue = TurnSequencer(
        repository,
        max_entries_per_conversation=1,
        max_entries_total=1,
    )
    admission_queue.reconcile_startup()
    normalizer = IngressNormalizer(
        repository,
        admission_queue,
        IngressBackendConfig(
            workspace_id="workspace-test",
            trusted_local_source_namespaces={
                "desktop": frozenset({_DESKTOP_NAMESPACE})
            },
            max_source_identity_chars=64,
            max_content_blocks=1,
            max_text_chars=5,
            max_raw_json_bytes=1_024,
        ),
    )
    surface = _TestSurface(normalizer)
    try:
        oversized_identity = surface.submit(
            surface.raw("x" * 65, source_namespace=_DESKTOP_NAMESPACE)
        )
        oversized_text = surface.submit(surface.raw("bounded", text="123456"))
        too_many_blocks = surface.submit(
            replace(
                surface.raw("blocks"),
                content=(
                    RawContentBlockV1(kind="text", text="a"),
                    RawContentBlockV1(kind="text", text="b"),
                ),
            )
        )

        assert oversized_identity.code == "invalid_inbound"
        assert oversized_text.code == "invalid_inbound"
        assert too_many_blocks.code == "invalid_inbound"
        assert repository.list_conversations() == ()
    finally:
        repository.close()


def test_project_command_has_structured_command_and_result_block(tmp_path):
    repository, sequencer, surface = _harness(tmp_path)
    try:
        project = repository.create_project("PBMC")
        command = {"kind": "bind", "project_id": project.project_id}
        result = surface.submit(
            surface.raw(
                "bind-project",
                text=None,
                project_command=command,
            )
        )

        assert result.status is TurnAcceptanceStatus.ACCEPTED
        assert repository.get_turn(result.turn_id).turn_kind == "control_command"
        envelope = _execute_next(sequencer, result.conversation_id)
        assert envelope.content == (
            {
                "kind": "control_command",
                "command": command,
                "result": {
                    "conversation_id": result.conversation_id,
                    "project_id": project.project_id,
                },
            },
        )
    finally:
        repository.close()


def test_project_command_schema_rejects_extra_or_non_opaque_fields(tmp_path):
    repository, _admission_queue, surface = _harness(tmp_path)
    try:
        extra = surface.submit(
            surface.raw(
                "extra-command-field",
                text=None,
                project_command={
                    "kind": "new_conversation",
                    "api_key": "must-not-cross-ingress",
                },
            )
        )
        malformed = surface.submit(
            surface.raw(
                "malformed-project",
                text=None,
                project_command={"kind": "bind", "project_id": "not-opaque"},
            )
        )

        assert extra.code == "invalid_inbound"
        assert malformed.code == "invalid_inbound"
        assert repository.list_conversations() == ()
    finally:
        repository.close()
