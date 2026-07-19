# ADR 0060 Text Delivery Reliability Design

## Goal

Close the remaining reliability gaps in ADR 0060's production text-delivery
slice for Telegram and Feishu without adding outbound media delivery.

## Scope

This change covers Channel startup and shutdown barriers, Feishu provider-call
termination, operator resend authority, bounded Delivery plans, Desktop Control
startup cleanup, Health server ownership, fail-closed Feishu input, bounded
over-length text fallback, packaging, and documentation.

Outbound media Items, Feishu inbound attachments, rich posts/cards, and every
non-Telegram/non-Feishu Channel remain fail-closed.

## Channel Lifecycle

The runner remains the sole composition root for one shared `ControlRuntime`.
No Channel may submit a Turn until every requested Channel has proved transport
readiness. Channels expose a process-local ingress activation gate: provider
callbacks received before activation are rejected without entering
`ControlRuntime`. The runner activates all Channels only after every `start()`
has completed successfully.

Feishu must prove its initial WebSocket connection instead of inferring success
from thread survival. The wrapper around the SDK's async `_connect()` sets a
thread-safe ready event only after the connection exists. Startup fails on a
bounded timeout, listener exit, or connection error, and the shared composition
rolls back as one unit.

Feishu stores the WebSocket event loop and client until shutdown is proved. Its
async `_disconnect()` runs on that owning loop, the loop is stopped, and the
thread is joined. A timeout is a shutdown failure: references and runtime
binding are retained, the Channel remains nonterminal, and the runner must not
close the shared runtime underneath a live provider callback.

`ChannelManager` owns its Health `asyncio.Server` and closes it during
`stop_all()`. Programmatic documentation names the authoritative runner rather
than presenting `start_all()` as a standalone composition API.

## Delivery Attempt Termination

The Feishu Delivery Adapter continues to execute one synchronous SDK request in
an executor. Cancellation is remembered but not allowed to finish the Adapter
until the executor future has actually completed. Repeated cancellations are
absorbed while waiting; after termination is proved, the original cancellation
is propagated. This preserves the ADR 0063 Reply Target barrier.

## Operator Authority And Bounds

`resend_delivery` and `retry_delivery` are mutations owned by a running Channel
runtime with a Delivery Pump and configured capacity limits. Local
CLI/Desktop runtimes may inspect Delivery state but must return a closed
`delivery_unavailable` outcome for these mutations.

The repository validates `MAX_DELIVERY_ITEMS` both for terminal insertion and
for resend copying. This includes Deliveries created by older versions before
the bound existed. Oversized historical Deliveries remain inspectable but
cannot create another oversized Delivery.

## Input And Content Bounds

Feishu admits only explicit `p2p` and `group` chat types. Group messages require
an @mention of the configured Bot. The decoded `text` field must be a string;
null, arrays, objects, malformed JSON, and unknown chat types fail closed before
rate-limit consumption or Turn submission.

The over-length text fallback never exceeds `max_chunk_codepoints`. Its frozen
range records how much of the deterministic truncation notice was included.
Existing fallback records without that field continue to resolve using the
full legacy notice.

## Desktop Cleanup

If `ControlRuntime.start()` fails during Desktop lifespan startup, that newly
created runtime is closed immediately and removed from global ownership before
the exception propagates. Normal shutdown's AutoAgent ownership rules remain
unchanged.

## Packaging And Documentation

`pyproject.toml` defines a real `channels` optional dependency group for the two
authoritative SDKs. README, README_zh-CN, AGENTS, Channel README, and ADR 0060
consistently describe Telegram text/single-photo and Feishu text-only support,
the shared runner entrypoint, mandatory Owner configuration, and outbound media
as explicitly incomplete.

ADR audit history reports only verified results; it must not claim a failing
Desktop lifecycle test did not reproduce.

## Verification

Every behavior is implemented test-first. Required regressions cover:

- no Telegram Turn before Feishu readiness and all-Channel activation;
- delayed Feishu connection failure and async SDK shutdown;
- join timeout retaining ownership and blocking runtime close;
- repeated Adapter cancellation while the provider thread is still running;
- historical oversized Delivery resend rejection;
- resend/retry rejection without a Delivery Pump;
- Desktop Control startup failure cleanup;
- Health port reuse after `stop_all()`;
- unknown chat types and non-string text rejection;
- fallback output shorter than its normal notice;
- documented optional extras and authoritative Channel facts.

The completion gate is the focused regression set followed by the complete
`tests/control`, `tests/bot`, and directly affected Desktop/documentation tests.
