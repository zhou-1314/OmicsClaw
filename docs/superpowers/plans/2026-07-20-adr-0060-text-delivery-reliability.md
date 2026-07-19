# ADR 0060 Text Delivery Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Telegram/Feishu text delivery satisfy ADR 0060/0063 lifecycle, ordering, capacity, and audit invariants while outbound media remains fail-closed.

**Architecture:** Keep the single shared `ControlRuntime` and persistent Delivery Pump. Add a runner-owned ingress activation barrier, make Feishu readiness/shutdown prove the actual SDK connection lifecycle, and enforce Delivery mutation authority at repository/runtime boundaries. Preserve existing persisted text Items through additive range metadata and reject unsafe historical resends.

**Tech Stack:** Python 3.11, asyncio, threading, sqlite3, lark-oapi, python-telegram-bot, pytest/pytest-asyncio.

---

### Task 1: All-Channel readiness and shutdown barrier

**Files:**
- Modify: `omicsclaw/surfaces/channels/base.py`
- Modify: `omicsclaw/surfaces/channels/manager.py`
- Modify: `omicsclaw/surfaces/channels/__main__.py`
- Modify: `omicsclaw/surfaces/channels/telegram.py`
- Modify: `omicsclaw/surfaces/channels/feishu.py`
- Test: `tests/bot/test_shared_control_runtime.py`
- Test: `tests/bot/test_feishu_control_runtime.py`
- Test: `tests/bot/test_telegram_control_runtime.py`

- [ ] **Step 1: Write failing activation-barrier tests**

Add tests proving a started Telegram handler and Feishu callback submit nothing before `ChannelManager.start_all()` activates every successful Channel, and that a later Feishu startup failure leaves zero submitted Turns.

```python
@pytest.mark.asyncio
async def test_manager_activates_ingress_only_after_every_channel_is_ready():
    ready: set[str] = set()

    class BarrierChannel(Channel):
        authoritative_ingress = True

        def __init__(self, name: str):
            self.name = name
            super().__init__(BaseChannelConfig())
            self.activation_snapshot: set[str] = set()

        async def start(self):
            ready.add(self.name)
            self._running = True

        async def stop(self):
            self._running = False

        def activate_ingress(self):
            self.activation_snapshot = set(ready)
            super().activate_ingress()

    first = BarrierChannel("telegram")
    second = BarrierChannel("feishu")
    manager = ChannelManager()
    manager.register(first)
    manager.register(second)

    await manager.start_all()

    assert first.ingress_active is True
    assert second.ingress_active is True
    assert first.activation_snapshot == {"telegram", "feishu"}
```

```python
@pytest.mark.asyncio
async def test_telegram_does_not_submit_before_shared_activation():
    runtime = SimpleNamespace(submit_and_wait=AsyncMock())
    channel = TelegramChannel(TelegramConfig(bot_token="token", admin_chat_id=7))
    channel.tg_config.account_namespace = "bot-1"
    channel.bind_control_runtime(runtime, loop=asyncio.get_running_loop())
    update = SimpleNamespace(
        message=SimpleNamespace(message_id=1, message_thread_id=None),
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=9),
    )

    assert await channel._submit_control_text(update, "early") is None
    runtime.submit_and_wait.assert_not_awaited()
```

- [ ] **Step 2: Verify RED**

Run:

```bash
python -m pytest -q tests/bot/test_shared_control_runtime.py tests/bot/test_telegram_control_runtime.py
```

Expected: FAIL because `Channel` has no activation state and Telegram submits as soon as polling starts.

- [ ] **Step 3: Implement the activation contract**

Add the following base behavior and call it only after every `_start_one` succeeds:

```python
def __init__(self, config: BaseChannelConfig | None = None):
    self.config = config or BaseChannelConfig()
    self._running = False
    self._ingress_active = False

@property
def ingress_active(self) -> bool:
    return self._ingress_active

def activate_ingress(self) -> None:
    if not self._running:
        raise RuntimeError("Channel transport is not ready")
    self._ingress_active = True

def deactivate_ingress(self) -> None:
    self._ingress_active = False
```

In `ChannelManager.start_all()`, activate every Channel only after the gathered starts have no failures. In Telegram `_submit_control_inbound` and Feishu `_handle_event`, return before constructing/submitting a Turn unless `ingress_active` is true. Every stop path deactivates ingress first.

- [ ] **Step 4: Make shutdown failure observable**

Change `ChannelManager.stop_all()` to gather failures, retain failed Channels as running, and raise one sanitized `RuntimeError`. In runner cleanup, close `ControlRuntime` only after `stop_all()` succeeds; a live provider listener must keep the shared runtime bound.

```python
results = await asyncio.gather(*tasks, return_exceptions=True)
failed = [name for name, result in zip(self._channels, results)
          if isinstance(result, BaseException)]
if failed:
    raise RuntimeError("Channel shutdown failed for: " + ", ".join(failed))
```

- [ ] **Step 5: Verify GREEN**

Run the command from Step 2 plus:

```bash
python -m pytest -q tests/bot/test_channel_cutover_gate.py
```

Expected: all pass.

### Task 2: Feishu connection readiness and SDK-owned shutdown

**Files:**
- Modify: `omicsclaw/surfaces/channels/feishu.py`
- Test: `tests/bot/test_feishu_control_runtime.py`

- [ ] **Step 1: Write failing SDK-shaped lifecycle tests**

Cover a delayed connection failure, successful `_connect()` readiness, async `_disconnect()`, and a non-cooperative listener that must make `stop()` fail while retaining client/thread/runtime references.

```python
@pytest.mark.asyncio
async def test_stop_executes_async_disconnect_on_websocket_loop():
    disconnected = threading.Event()
    release = threading.Event()

    class Client:
        async def _disconnect(self):
            disconnected.set()
            release.set()

    channel = FeishuChannel(
        FeishuConfig(
            app_id="app",
            app_secret="secret",
            allowed_senders={"owner"},
            ws_join_seconds=1.0,
        )
    )
    channel._control_runtime = object()
    channel._ws_client = Client()
    ws_ready = threading.Event()
    channel._ws_loop = asyncio.new_event_loop()

    def run_ws_loop():
        asyncio.set_event_loop(channel._ws_loop)
        ws_ready.set()
        channel._ws_loop.run_forever()

    channel._ws_thread = threading.Thread(target=run_ws_loop, daemon=True)
    channel._ws_thread.start()
    assert ws_ready.wait(1)
    await channel.stop()
    assert disconnected.is_set()
    assert channel._ws_thread is None
```

```python
@pytest.mark.asyncio
async def test_stop_retains_ownership_when_listener_does_not_terminate():
    release = threading.Event()
    channel = FeishuChannel(
        FeishuConfig(
            app_id="app",
            app_secret="secret",
            allowed_senders={"owner"},
            ws_join_seconds=0.01,
        )
    )
    runtime = object()
    channel._control_runtime = runtime
    channel._ws_client = object()
    channel._ws_loop = asyncio.new_event_loop()
    channel._ws_thread = threading.Thread(target=release.wait, daemon=True)
    channel._ws_thread.start()
    with pytest.raises(RuntimeError, match="did not stop"):
        await channel.stop()
    assert channel._ws_client is not None
    assert channel._ws_thread.is_alive()
    assert channel._control_runtime is runtime
    release.set()
    channel._ws_thread.join(1)
```

- [ ] **Step 2: Verify RED**

```bash
python -m pytest -q tests/bot/test_feishu_control_runtime.py -k 'websocket or listener'
```

Expected: async disconnect is closed without execution and non-cooperative shutdown returns success.

- [ ] **Step 3: Implement real readiness**

Store `_ws_loop`, `_ws_ready`, `_ws_exit`, and `_ws_stopping`. Wrap the SDK client's async `_connect` on the WebSocket thread and set ready only after it returns with a live connection. Reject SDK versions without the required lifecycle seam rather than guessing from thread survival.

```python
original_connect = getattr(client, "_connect", None)
if not callable(original_connect):
    raise RuntimeError("Feishu SDK lacks a verifiable connection seam")

async def connect_and_signal():
    await original_connect()
    if getattr(client, "_conn", None) is None:
        raise RuntimeError("Feishu WebSocket did not establish a connection")
    self._ws_ready.set()

client._connect = connect_and_signal
```

Wait for `_ws_ready` or `_ws_exit` up to the configured startup timeout. Any non-ready result invokes rollback and raises.

- [ ] **Step 4: Implement SDK-loop shutdown**

For async `_disconnect`, submit it with `asyncio.run_coroutine_threadsafe` to `_ws_loop`, await the concurrent future with a bounded timeout, stop the loop, and join the thread. Clear references and detach the runtime only after termination is proved. Raise while retaining ownership otherwise.

```python
future = asyncio.run_coroutine_threadsafe(client._disconnect(), ws_loop)
await asyncio.wait_for(asyncio.wrap_future(future), timeout=join_seconds)
ws_loop.call_soon_threadsafe(ws_loop.stop)
await asyncio.to_thread(thread.join, join_seconds)
if thread.is_alive():
    raise RuntimeError("Feishu WebSocket thread did not stop")
```

- [ ] **Step 5: Verify GREEN**

Run Step 2 command. Expected: all selected tests pass without coroutine warnings.

### Task 3: Provider-call termination and Feishu fail-closed input

**Files:**
- Modify: `omicsclaw/surfaces/channels/feishu_delivery.py`
- Modify: `omicsclaw/surfaces/channels/feishu.py`
- Test: `tests/bot/test_feishu_delivery.py`
- Test: `tests/bot/test_feishu_control_runtime.py`

- [ ] **Step 1: Write failing repeated-cancellation and input tests**

```python
task.cancel()
await asyncio.sleep(0)
task.cancel()
await asyncio.sleep(0.05)
assert not task.done()
assert provider_thread_is_blocked()
```

Parameterize `chat_type` with `None`, `""`, and `"future_group"`, and text with `null`, arrays, and objects; assert no rate-limit consumption and no runtime call.

- [ ] **Step 2: Verify RED**

```bash
python -m pytest -q tests/bot/test_feishu_delivery.py tests/bot/test_feishu_control_runtime.py -k 'cancel or chat_type or non_string'
```

Expected: second cancellation finishes the Adapter; malformed inputs submit Turns.

- [ ] **Step 3: Make executor termination cancellation-resistant**

Remember the first cancellation and continue shielding until `worker.done()`. Only then re-raise cancellation.

```python
cancelled: asyncio.CancelledError | None = None
while not worker.done():
    try:
        await asyncio.shield(worker)
    except asyncio.CancelledError as exc:
        cancelled = cancelled or exc
if cancelled is not None:
    raise cancelled
response = worker.result()
```

- [ ] **Step 4: Enforce closed Feishu input types**

Reject chat types outside `{"p2p", "group"}` before parsing/rate limiting. Require `parsed.get("text")` to be `str` before `_normalize_text`.

- [ ] **Step 5: Verify GREEN**

Run Step 2 command. Expected: all selected tests pass.

### Task 4: Delivery operator authority and historical bounds

**Files:**
- Modify: `omicsclaw/control/models.py`
- Modify: `omicsclaw/control/repository.py`
- Modify: `omicsclaw/control/runtime.py`
- Test: `tests/control/test_delivery_operator.py`
- Test: `tests/control/test_delivery_resend_retry.py`

- [ ] **Step 1: Write failing tests**

Add an upgrade fixture that creates a 65-Item Delivery with the baseline schema and asserts current resend raises `ControlIntegrityError`. Create a local runtime without a Pump and assert resend/retry return `delivery_unavailable` and create no Delivery.

- [ ] **Step 2: Verify RED**

```bash
python -m pytest -q tests/control/test_delivery_operator.py tests/control/test_delivery_resend_retry.py
```

Expected: oversized resend succeeds and local runtime creates a queued Delivery.

- [ ] **Step 3: Enforce bounds and runtime ownership**

Before copying Items in `insert_resend_delivery`, reject `len(item_rows) > MAX_DELIVERY_ITEMS`. In `ControlRuntime.resend_delivery` and `retry_delivery`, return `DeliveryOperationOutcome("delivery_unavailable")` unless a Delivery Pump and both capacity limits exist. Update the outcome docstring's closed code set.

- [ ] **Step 4: Verify GREEN**

Run Step 2 command. Expected: all pass, including the real two-thread capacity-one test.

### Task 5: Bounded text fallback and Desktop/Health cleanup

**Files:**
- Modify: `omicsclaw/control/delivery_content.py`
- Modify: `omicsclaw/surfaces/desktop/server.py`
- Modify: `omicsclaw/surfaces/channels/manager.py`
- Test: `tests/control/test_delivery_content.py`
- Test: `tests/test_active_workspace_authority.py`
- Test: `tests/bot/test_channel_cutover_gate.py`

- [ ] **Step 1: Write failing tests**

Assert `max_chunk_codepoints=8` resolves to exactly eight codepoints and that legacy truncated ranges without `notice_end` still append the full notice. Assert Desktop start failure calls `close()`. Start/stop a Health server on an ephemeral port and rebind that same port successfully.

- [ ] **Step 2: Verify RED**

```bash
python -m pytest -q tests/control/test_delivery_content.py tests/test_active_workspace_authority.py tests/bot/test_channel_cutover_gate.py
```

- [ ] **Step 3: Add bounded notice metadata**

Freeze `notice_end=min(len(notice), max_chunk_codepoints)` and include it only on new truncated records. Resolve absent `notice_end` as the full legacy notice; validate present values as integers within the notice bounds.

- [ ] **Step 4: Close failed Desktop runtime directly**

Wrap only `ControlRuntime.start()` so failure clears the global and closes that runtime before generic AutoAgent shutdown logic runs.

```python
runtime = _desktop_control_runtime
try:
    await runtime.start()
except BaseException:
    _desktop_control_runtime = None
    await runtime.close()
    raise
```

- [ ] **Step 5: Give Health server explicit ownership**

Store the server on `ChannelManager`, make duplicate starts fail closed, and close/wait it during `stop_all()` even when Channel shutdown reports a failure.

- [ ] **Step 6: Verify GREEN**

Run Step 2 command. Expected: all pass.

### Task 6: Packaging, documentation, and final verification

**Files:**
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `README_zh-CN.md`
- Modify: `AGENTS.md`
- Modify: `omicsclaw/surfaces/channels/README.md`
- Modify: `docs/adr/0060-deliver-terminal-channel-replies-through-a-persistent-outbox.md`
- Test: `tests/test_documentation_facts.py`
- Test: `tests/test_pyproject_thin_pip_layer.py`
- Test: `tests/test_control_plane_documentation_contract.py`

- [ ] **Step 1: Write failing documentation/package contract tests**

Assert `project.optional-dependencies.channels` contains `python-telegram-bot>=21.0` and `lark-oapi>=1.3.0`; assert every README names Telegram text/photo and Feishu text-only, mandatory `FEISHU_ALLOWED_SENDERS`, the shared runner, and outbound media as incomplete.

- [ ] **Step 2: Verify RED**

```bash
python -m pytest -q tests/test_documentation_facts.py tests/test_pyproject_thin_pip_layer.py tests/test_control_plane_documentation_contract.py
```

- [ ] **Step 3: Update packaging and docs**

Add:

```toml
channels = [
    "python-telegram-bot>=21.0",
    "lark-oapi>=1.3.0",
]
```

Remove stale Telegram-only claims, retire the direct `ChannelManager.start_all()` usage example, document required Feishu Owner/Bot IDs, and replace ADR audit claims with commands and results actually observed after this implementation.

- [ ] **Step 4: Run focused verification**

```bash
python -m pytest -q tests/control tests/bot tests/test_active_workspace_authority.py tests/test_control_plane_documentation_contract.py tests/test_documentation_facts.py tests/test_pyproject_thin_pip_layer.py tests/test_outbox_executor.py tests/test_run_hypothesis_endpoint.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Run static checks**

```bash
python -m compileall -q omicsclaw/control omicsclaw/surfaces/channels omicsclaw/surfaces/desktop
git diff --check
```

Expected: both commands exit zero with no output.
