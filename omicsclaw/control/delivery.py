"""Durable Outbox delivery orchestration over typed Adapter boundaries."""

from __future__ import annotations

import asyncio
import math
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping

from .models import (
    DeliveryAdapterResult,
    DeliveryAttemptOutcome,
    DeliveryAttemptRequest,
    DeliveryCandidate,
    DeliveryStartupRecoveryResult,
)
from .delivery_content import DeliveryContentIntegrityError
from .repository import ControlStateRepository


DeliveryAdapter = Callable[
    [DeliveryAttemptRequest],
    Awaitable[DeliveryAdapterResult],
]
DeliveryAdapterAccount = tuple[str, str]
DeliveryContentResolver = Callable[[DeliveryCandidate], str]


class DeliveryAdapterTerminationError(RuntimeError):
    """A timed-out Adapter ignored cancellation, so the Pump halted closed."""


def _default_clock_ms() -> int:
    return time.time_ns() // 1_000_000


def _consume_detached_task(task: asyncio.Future[object]) -> None:
    """Retrieve a timed-out provider task's eventual result or cancellation."""

    try:
        task.result()
    except BaseException:
        pass


def _default_retry_jitter_ms(window_ms: int) -> int:
    return secrets.randbelow(window_ms + 1)


class DeliveryPump:
    """Claim durable Outbox heads and make at most one Adapter call per Attempt."""

    def __init__(
        self,
        repository: ControlStateRepository,
        *,
        adapters: Mapping[DeliveryAdapterAccount, DeliveryAdapter],
        content_resolver: DeliveryContentResolver,
        max_active_attempts: int = 16,
        max_attempts: int = 3,
        retry_base_ms: int = 1_000,
        retry_max_ms: int = 60_000,
        retry_hint_max_ms: int = 7 * 24 * 60 * 60 * 1_000,
        attempt_timeout_seconds: float = 30.0,
        cancellation_grace_seconds: float = 1.0,
        retry_jitter_ms: Callable[[int], int] = _default_retry_jitter_ms,
        clock_ms: Callable[[], int] = _default_clock_ms,
    ) -> None:
        if not isinstance(repository, ControlStateRepository):
            raise TypeError("repository must be a ControlStateRepository")
        if (
            not isinstance(max_active_attempts, int)
            or isinstance(max_active_attempts, bool)
            or max_active_attempts <= 0
        ):
            raise ValueError("max_active_attempts must be a positive integer")
        if (
            not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or max_attempts <= 0
        ):
            raise ValueError("max_attempts must be a positive integer")
        for name, value in (
            ("retry_base_ms", retry_base_ms),
            ("retry_max_ms", retry_max_ms),
            ("retry_hint_max_ms", retry_hint_max_ms),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if retry_max_ms < retry_base_ms:
            raise ValueError(
                "retry_max_ms must be greater than or equal to retry_base_ms"
            )
        if (
            not isinstance(attempt_timeout_seconds, (int, float))
            or isinstance(attempt_timeout_seconds, bool)
            or not math.isfinite(attempt_timeout_seconds)
            or attempt_timeout_seconds <= 0
        ):
            raise ValueError("attempt_timeout_seconds must be finite and positive")
        if (
            not isinstance(cancellation_grace_seconds, (int, float))
            or isinstance(cancellation_grace_seconds, bool)
            or not math.isfinite(cancellation_grace_seconds)
            or cancellation_grace_seconds <= 0
        ):
            raise ValueError("cancellation_grace_seconds must be finite and positive")
        if not callable(retry_jitter_ms):
            raise TypeError("retry_jitter_ms must be callable")
        self._repository = repository
        self._adapters: dict[DeliveryAdapterAccount, DeliveryAdapter] = {}
        for scope, adapter_call in adapters.items():
            if (
                not isinstance(scope, tuple)
                or len(scope) != 2
                or not all(isinstance(part, str) and part.strip() for part in scope)
            ):
                raise ValueError(
                    "Delivery Adapter keys must be (adapter, account_namespace)"
                )
            if not callable(adapter_call):
                raise TypeError("Delivery Adapter values must be callable")
            normalized_scope = (scope[0].strip(), scope[1].strip())
            if normalized_scope in self._adapters:
                raise ValueError("duplicate Delivery Adapter account")
            self._adapters[normalized_scope] = adapter_call
        if not self._adapters:
            raise ValueError("at least one Delivery Adapter account is required")
        self._adapter_accounts = tuple(sorted(self._adapters))
        if not callable(content_resolver):
            raise TypeError("content_resolver must be callable")
        self._content_resolver = content_resolver
        self._max_active_attempts = max_active_attempts
        self._max_attempts = max_attempts
        self._retry_base_ms = retry_base_ms
        self._retry_max_ms = retry_max_ms
        self._retry_hint_max_ms = retry_hint_max_ms
        self._attempt_timeout_seconds = float(attempt_timeout_seconds)
        self._cancellation_grace_seconds = float(cancellation_grace_seconds)
        self._retry_jitter_ms = retry_jitter_ms
        self._clock_ms = clock_ms
        self._wake_event = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._fatal_error: BaseException | None = None
        self._started = False
        self._closed = False

    async def start(self) -> DeliveryStartupRecoveryResult:
        if self._closed:
            raise RuntimeError("DeliveryPump is closed")
        if self._started:
            raise RuntimeError("DeliveryPump is already started")
        recovery = self._repository.reconcile_delivery_startup()
        self._started = True
        self.wake()
        return recovery

    def wake(self) -> None:
        if not self._started:
            raise RuntimeError("DeliveryPump has not started")
        if self._closed:
            raise RuntimeError("DeliveryPump is closed")
        if self._fatal_error is not None:
            # A new Pump/process is the recovery boundary. Never restart an
            # in-process runner while an unproven provider call may still live.
            return
        self._wake_event.set()
        if self._runner is None or self._runner.done():
            self._runner = asyncio.create_task(self._run(), name="delivery-pump")

    async def wait_idle(self) -> None:
        if not self._started:
            raise RuntimeError("DeliveryPump has not started")
        while self._runner is not None:
            runner = self._runner
            await asyncio.shield(runner)
            if self._runner is runner:
                return

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._wake_event.set()
        if self._runner is not None:
            await asyncio.shield(self._runner)

    async def _run(self) -> None:
        active: set[asyncio.Task[None]] = set()
        try:
            while not self._closed:
                self._wake_event.clear()
                available = self._max_active_attempts - len(active)
                content_progress = False
                if available > 0:
                    candidates = self._repository.list_due_delivery_candidates(
                        limit=available,
                        adapter_accounts=self._adapter_accounts,
                    )
                    for candidate in candidates:
                        try:
                            text = self._content_resolver(candidate)
                            if not isinstance(text, str):
                                raise TypeError("content_resolver must return a string")
                        except DeliveryContentIntegrityError:
                            failed = self._repository.fail_delivery_content(
                                candidate.item_id
                            )
                            content_progress = content_progress or failed is not None
                            continue
                        claim = self._repository.claim_delivery_attempt(
                            candidate,
                            text=text,
                        )
                        if claim.request is not None:
                            active.add(
                                asyncio.create_task(
                                    self._deliver(claim.request),
                                    name=f"delivery-attempt-{claim.request.attempt_id}",
                                )
                            )

                # A failed content head can immediately reveal another target-local
                # candidate. Fill that slot before yielding to provider I/O.
                if content_progress and len(active) < self._max_active_attempts:
                    continue

                retry_at_ms = (
                    self._repository.next_delivery_retry_at_ms(
                        adapter_accounts=self._adapter_accounts
                    )
                    if len(active) < self._max_active_attempts
                    else None
                )
                if not active and retry_at_ms is None:
                    if self._wake_event.is_set():
                        continue
                    return

                wake_waiter = asyncio.create_task(self._wake_event.wait())
                transient_waiters: set[asyncio.Task[object]] = {wake_waiter}
                if retry_at_ms is not None:
                    delay_seconds = (
                        max(
                            0,
                            retry_at_ms - self._clock_ms(),
                        )
                        / 1_000
                    )
                    transient_waiters.add(
                        asyncio.create_task(asyncio.sleep(delay_seconds))
                    )
                done: set[asyncio.Task[object]] = set()
                try:
                    done, _ = await asyncio.wait(
                        {*active, *transient_waiters},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for waiter in transient_waiters:
                        if not waiter.done():
                            waiter.cancel()
                    await asyncio.gather(
                        *transient_waiters,
                        return_exceptions=True,
                    )

                completed_attempts = active.intersection(done)
                active.difference_update(completed_attempts)
                for attempt in completed_attempts:
                    await attempt
        except BaseException as error:
            self._fatal_error = error
            raise
        finally:
            if active:
                await asyncio.gather(*active, return_exceptions=True)

    async def _deliver(self, request: DeliveryAttemptRequest) -> None:
        adapter_name = request.candidate.reply_target.get("adapter")
        account_namespace = request.candidate.reply_target.get("account_namespace")
        adapter = self._adapters.get(
            (str(adapter_name), str(account_namespace)),
        )
        termination_error = False
        if adapter is None:
            result = DeliveryAdapterResult(
                DeliveryAttemptOutcome.REJECTED_PERMANENT,
                error_code="delivery_adapter_not_found",
            )
        else:
            provider_task: asyncio.Future[DeliveryAdapterResult] | None = None
            try:
                provider_task = asyncio.ensure_future(adapter(request))
                done, _ = await asyncio.wait(
                    {provider_task},
                    timeout=self._attempt_timeout_seconds,
                )
                if not done:
                    terminated = await self._cancel_provider_task(provider_task)
                    result = DeliveryAdapterResult(
                        DeliveryAttemptOutcome.ACCEPTANCE_UNKNOWN,
                        error_code="delivery_adapter_timeout",
                    )
                    termination_error = not terminated
                else:
                    result = await provider_task
            except asyncio.CancelledError:
                if provider_task is not None and not provider_task.done():
                    provider_task.cancel()
                    provider_task.add_done_callback(_consume_detached_task)
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    self._repository.finish_delivery_attempt(
                        request.attempt_id,
                        DeliveryAttemptOutcome.ACCEPTANCE_UNKNOWN,
                        error_code="delivery_attempt_canceled",
                    )
                    raise
                result = DeliveryAdapterResult(
                    DeliveryAttemptOutcome.ACCEPTANCE_UNKNOWN,
                    error_code="delivery_adapter_canceled",
                )
            except Exception:
                result = DeliveryAdapterResult(
                    DeliveryAttemptOutcome.ACCEPTANCE_UNKNOWN,
                    error_code="delivery_adapter_exception",
                )
            if not isinstance(result, DeliveryAdapterResult):
                result = DeliveryAdapterResult(
                    DeliveryAttemptOutcome.ACCEPTANCE_UNKNOWN,
                    error_code="invalid_delivery_adapter_result",
                )
        retry_at_ms: int | None = None
        retry_exhausted = False
        error_code = result.error_code
        if result.outcome is DeliveryAttemptOutcome.NOT_ACCEPTED_RETRYABLE:
            retry_exhausted = request.attempt_no >= self._max_attempts
            if retry_exhausted:
                error_code = "delivery_attempts_exhausted"
            else:
                exponential_ms = min(
                    self._retry_max_ms,
                    self._retry_base_ms * (2 ** (request.attempt_no - 1)),
                )
                jitter_window_ms = max(1, exponential_ms // 5)
                jitter_ms = self._retry_jitter_ms(jitter_window_ms)
                if not isinstance(jitter_ms, int) or isinstance(jitter_ms, bool):
                    jitter_ms = 0
                jitter_ms = min(jitter_window_ms, max(0, jitter_ms))
                local_delay_ms = min(
                    self._retry_max_ms,
                    exponential_ms + jitter_ms,
                )
                provider_hint_ms = min(
                    self._retry_hint_max_ms,
                    result.retry_after_ms or 0,
                )
                delay_ms = max(local_delay_ms, provider_hint_ms)
                retry_at_ms = self._clock_ms() + delay_ms
        self._repository.finish_delivery_attempt(
            request.attempt_id,
            result.outcome,
            error_code=error_code,
            provider_evidence=result.provider_evidence,
            retry_at_ms=retry_at_ms,
            retry_exhausted=retry_exhausted,
        )
        if termination_error:
            raise DeliveryAdapterTerminationError(
                "timed-out Delivery Adapter ignored cancellation; Pump halted"
            )

    async def _cancel_provider_task(
        self,
        provider_task: asyncio.Future[DeliveryAdapterResult],
    ) -> bool:
        """Request cancellation and prove termination before releasing a target."""

        provider_task.cancel()
        done, _ = await asyncio.wait(
            {provider_task},
            timeout=self._cancellation_grace_seconds,
        )
        if done:
            await asyncio.gather(provider_task, return_exceptions=True)
            return True
        provider_task.add_done_callback(_consume_detached_task)
        return False


__all__ = [
    "DeliveryAdapter",
    "DeliveryAdapterAccount",
    "DeliveryAdapterTerminationError",
    "DeliveryAdapterResult",
    "DeliveryAttemptRequest",
    "DeliveryContentResolver",
    "DeliveryPump",
]
