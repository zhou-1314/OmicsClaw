# Implementation Plan: ADR 0024 — Prompt Prefix Caching

## Overview

Enforce the **Stable prefix invariant** (ADR 0024): within a session the Prompt
prefix (serialized `tools` + the `system` message) is byte-identical across
turns, changing only at deliberate, logged cache re-warms. Five workstreams:
(1) freeze the tool list per session, (2) re-tier system layers by volatility
via `placement`, (3) snapshot session-scoped memory with write-triggered
re-warm, (4) make history append-only between context collapses, (5) add
cache-hit diagnostics with prefix-segment miss-reason inference and a CI floor.

**Diagnostics ship first** (Phase 0): they are the measurement oracle for every
later phase — you cannot verify a hit-rate improvement you cannot read. Capture
a "before" number on a demo multi-turn session, then watch each phase move it.

## Implementation status (2026-05-30)

Phases 0–3 implemented and verified; Phase 4 invariant enforced by tests.

| Phase | Status | Evidence |
|---|---|---|
| 0 — Diagnostics | ✅ | `omicsclaw/runtime/agent/cache_diagnostics.py` (`extract_cache_tokens`, `compute_segment_hash`, `infer_miss_reason`, `CacheDiagnosticsStore`); wired in `query_engine._emit_cache_diagnostics`; `on_cache_diagnostics` callback. Tests: `tests/test_cache_diagnostics.py` (19), `tests/test_query_engine_cache_diagnostics.py` (7). |
| 1 — Frozen tools | ✅ | `select_tool_specs(..., surface_only=True)` from `engine/loop.py`. Tests: `test_tool_list_predicate.py` (surface_only), `test_tool_list_snapshots.py` (query-independence + full-surface-set). |
| 2 — placement | ✅ | 11 volatile layers → `placement="message"`; route/understanding/assisted-param context → `user_turn_context` (user turn). Test: `test_context_assembler.py::test_system_prompt_is_byte_stable_across_query_intents`. |
| 3 — append-only | ✅ | `prepare_history` no longer slides; collapse is sole overflow handler. Test: `test_query_engine_cache_diagnostics.py::test_history_is_append_only_across_turns` (max_history=4 would fail under the old slide). |
| 4 — lock-in | ✅ (structural) | `test_regression_floor_stable_prefix_holds_high_hit_ratio` + the miss-reason suite enforce the invariant in CI. |

**Empirical before/after (Task 0.3 / 4.2): measured on real DeepSeek.** A controlled
A/B over one 6-turn omics dialogue (same conversation, real OmicsClaw system prompt
+ tool list), run via `scripts/measure_prefix_cache.py` against `api.deepseek.com`
(`deepseek-v4-flash`), reading DeepSeek's actual `prompt_cache_hit_tokens` /
`prompt_cache_miss_tokens`:

| arm | session hit-ratio | per-turn |
|---|---|---|
| **before** (pre-ADR-0024: per-turn tool re-gating + volatile content in the system prefix) | **12.1%** | ~11–15% every turn — the prefix changes each turn, so almost nothing is cached |
| **after** (ADR-0024: frozen tool list + stable system prefix + append-only history) | **81.8%** | turn 1 cold (2%), turns 2–6 **94–98%**; hit tokens grow each turn (11.0k → 12.8k) as append-only history extends the cached prefix |

Input-token billing (DeepSeek charges a cache-hit token at ~10% of a miss):
before ≈ 34,758 vs after ≈ 19,376 miss-equivalent units → **~44% lower input cost**,
even though the "after" arm sends the *full* frozen tool list + accumulating context
every turn. Steady-state (turns 2+) hit ratio is ~96%; the turn-1 cold-start drags
the session aggregate to 81.8%. Re-run `scripts/measure_prefix_cache.py` to refresh
on your live model.

## Architecture Decisions (from ADR 0024 + CONTEXT.md §"Prompt Prefix & Caching")

- Target **automatic prefix caching** (DeepSeek default + OpenAI). **No**
  `cache_control` annotations. **No** tool re-sort (static registration order is
  already deterministic).
- `placement` is the cache boundary: `"system"` = session-stable (snapshotted),
  `"message"` = Volatile context rendered into the latest user turn.
- A cache re-warm is one of: model switch, memory write, context collapse. Each
  is logged and shows up in diagnostics as `system-changed` / `history-shifted`.
- Reuse existing machinery: `placement` (`context/layers`), context collapse
  (`compaction.py`), `usage_accumulator`. This is re-tiering + call-site removal,
  not new infrastructure.

## Guiding constraint: keep the suite green + watch the hit-rate dial

Run after each task:

```
pytest tests/runtime/ -q
pytest tests/ -q -k "context or prompt or compaction or tool_registry or usage"
```

The verification oracle for Phases 1–4 is the Phase 0 diagnostic: a fixed
demo multi-turn session's per-turn miss reasons must improve as documented
(e.g. `tool-list-changed` must vanish after Phase 1).

---

## Task List

### Phase 0 — Diagnostics (the measurement oracle)

#### Task 0.1: Shared cache-token extraction in the loop usage callback

**Description:** Lift the per-provider cache-token read from
`surfaces/desktop/server.py:802-822` into a provider-neutral helper
(`omicsclaw/runtime/agent/cache_diagnostics.py`) and call it from the loop's
`on_usage_delta` path (`query_engine.py`, the `_materialize_message` usage
hook). Accumulate `cache_hit_tokens` / `cache_miss_tokens` into the existing
`usage_accumulator` so all three surfaces share one path.

**Acceptance criteria:**
- [ ] `extract_cache_tokens(usage) -> (hit, miss)` handles DeepSeek
  (`prompt_cache_hit_tokens`/`prompt_cache_miss_tokens`), OpenAI
  (`prompt_tokens_details.cached_tokens`), Anthropic (`cache_read_input_tokens`).
- [ ] The loop computes per-turn `hit_ratio = hit / (hit + miss)` and a session
  running ratio; both flow through `usage_accumulator`.
- [ ] `surfaces/desktop/server.py` consumes the shared helper (no duplicated
  extraction); CLI + Channel now receive the same usage fields.

**Verification:**
- [ ] Unit test feeds synthetic DeepSeek/OpenAI/Anthropic usage objects → correct
  `(hit, miss)`.
- [ ] A scripted 3-turn demo session logs a per-turn and session hit ratio on the
  CLI surface.

#### Task 0.2: Prefix-segment hashing + miss-reason inference

**Description:** Each turn, hash two segments — the serialized Frozen tool list
and the stable `system` prefix — store them on `LoopState`, and compare against
the previous turn. When the provider reports a miss, infer the reason.

**Acceptance criteria:**
- [ ] `infer_miss_reason(prev_hashes, cur_hashes, hit, miss) -> str` returns one
  of `cold-start`, `tool-list-changed`, `system-changed`, `history-shifted`,
  `none` (hit).
- [ ] Hashes are SHA-256 over the exact serialized bytes sent to the provider.
- [ ] Reason is attached to the per-turn usage event.

**Verification:**
- [ ] Unit tests: changed tool hash → `tool-list-changed`; changed system hash →
  `system-changed`; both unchanged + miss → `history-shifted`; no prior → `cold-start`.

#### Task 0.3: Expose + capture the baseline

**Description:** Surface the ratio + miss reason on the Desktop usage banner, CLI
output, and a `cache.diagnostics` log line. Record the **current** (pre-change)
hit rate of a fixed demo multi-turn session into this plan.

**Acceptance criteria:**
- [ ] Desktop usage payload carries `cache_hit_ratio` + `cache_miss_reason`.
- [ ] CLI prints a one-line per-turn cache summary under a verbosity flag.
- [ ] **Baseline recorded here:** demo session hit ratio turns 2–N = `____`
  (expected near 0 given Findings 1–3).

---

### Phase 1 — Frozen tool list

#### Task 1.1: Freeze the session tool payload (drop per-turn gating)

**Description:** Stop varying the tool subset per request. Filter the tool
payload once by `surface` at session start and freeze it; reuse verbatim every
turn. Concretely: make `select_tool_specs` (`registry.py:13-74`) gate on
`surface` only (skip `predicate(request)` for the cached path), or compute
`request_tools` once at session start in `engine/loop.py:170-174` and reuse.
Keep static registration order — **no re-sort**.

**Acceptance criteria:**
- [ ] Within one session, `request_tools` is byte-identical across turns
  regardless of the current user message.
- [ ] `predicates.py` query-keyword gating no longer affects the sent tool list
  (the predicate map may stay for non-cached uses, but the cached path ignores it).
- [ ] Different surfaces still get different tool sets (surface gating preserved).

**Verification:**
- [ ] Phase 0 diagnostic: across a demo session that mentions a file path on one
  turn and not the next, `tool-list-changed` **never** fires.
- [ ] `pytest tests/ -q -k tool_registry` green (update tests asserting per-turn
  subsetting to assert per-session freezing).

---

### Phase 2 — `placement` = cache boundary (system prompt re-tiering)

#### Task 2.1: Move volatile layers to `placement="message"`

**Description:** In `context/layers/__init__.py:996-1155`, change `placement`
from `"system"` to `"message"` for the per-turn layers: gated rule layers
(o12,13,14,16,18,19), `skill_context` (o42), `capability_assessment` (o50),
`knowledge_guidance` (o52), `plan_context` (o55). They render into the user
message via `build_user_message_content` (`assembler.py:216-254`), joining the
already-`message` `transcript_context` (o58).

**Acceptance criteria:**
- [ ] Listed layers are absent from `system_prompt` and present in
  `message_context` for the current turn.
- [ ] Stable layers (o10,11,15,17,35,60,70,80) remain `placement="system"`.
- [ ] A past turn's message_context is frozen in history (append-only): only the
  current turn carries fresh Volatile context.

**Verification:**
- [ ] Phase 0 diagnostic: the stable-system hash is unchanged across turns whose
  only difference is query intent.
- [ ] `pytest tests/ -q -k "context or prompt"` green.

#### Task 2.2: Session prefix snapshot

**Description:** Snapshot the stable `system` layers once at session start and
reuse the assembled string verbatim each turn (rather than re-assembling). A
change requires an explicit re-warm (Task 2.3 / model switch).

**Acceptance criteria:**
- [ ] The stable `system` prefix string is computed once per session and cached
  on session state.
- [ ] Re-assembly does not run per turn for the stable tier.

**Verification:**
- [ ] Phase 0 diagnostic: `system-changed` does not fire on ordinary turns.

#### Task 2.3: Memory snapshot + write-triggered re-warm

**Description:** Include `memory_context` + `scoped_memory_context` in the
Session prefix snapshot (they are session-scoped, `assembler.py:330-332`). On a
memory **write** during the session, invalidate + rebuild the snapshot and log a
re-warm.

**Acceptance criteria:**
- [ ] Preferences/memory appear in the stable `system` prefix, not the message tail.
- [ ] A memory write triggers exactly one snapshot rebuild + one logged re-warm.
- [ ] No memory write → snapshot stays byte-identical.

**Verification:**
- [ ] Test: state a preference mid-session → diagnostic shows one `system-changed`
  turn, then stable again.

---

### Phase 3 — History append-only between collapses

#### Task 3.1: Remove the per-turn sliding window from the model path

**Description:** Stop applying `trim_history_to_budget`'s newest-suffix slide
per turn on the model path. History grows append-only; overflow is handled only
by `CONTEXT_COLLAPSE`/`AUTO_COMPACT` (`compaction.py:542-581`), which folds old
messages into a frozen `system` summary. Keep a large `max_chars` ceiling that
**triggers a collapse**, not a silent slide. Leave tool-result micro-compaction
(`compaction.py:231-270`) as-is.

**Acceptance criteria:**
- [ ] For a session past the old 50-message window with no overflow collapse, the
  first post-prefix message is byte-identical across turns.
- [ ] Overflow produces one collapse (frozen summary), after which history is
  append-only again.
- [ ] Display-only history trimming (if any) is separated from the model path.

**Verification:**
- [ ] Phase 0 diagnostic: a 60-turn demo session holds a high hit ratio with
  `history-shifted` firing **only** on collapse turns.
- [ ] `pytest tests/ -q -k compaction` green.

---

### Phase 4 — Lock it in

#### Task 4.1: CI regression assertion

**Description:** Add a test that runs a fixed multi-turn demo session through the
loop with a stub provider echoing usage, and asserts the per-turn miss reasons +
a hit-ratio floor from turn 2 on.

**Acceptance criteria:**
- [ ] Test asserts: turn 1 = `cold-start`; turns 2–N (no memory write, no
  collapse) = `none`/hit; `tool-list-changed` never appears.
- [ ] Re-introducing per-turn tool gating or a volatile system layer fails this test.

**Verification:**
- [ ] `pytest tests/runtime/ -q -k cache` green; intentionally reverting Phase 1
  makes it red (sanity check, then revert the revert).

#### Task 4.2: Record before/after

**Description:** Fill in the after-numbers and a token-cost delta in this plan +
ADR 0024 Consequences.

**Acceptance criteria:**
- [ ] Demo session hit ratio: before `____` → after `____`.
- [ ] Estimated per-turn input-token cost delta recorded (hit tokens ~10%).

---

## Phase ordering rationale

Phase 0 first (oracle). Then Phase 1 (biggest, simplest win — tools), Phase 2
(system re-tiering), Phase 3 (history) — each independently visible on the
diagnostic dial, so a regression localizes to its phase. Phase 4 freezes the
invariant. Phases 1–3 are independently shippable; each should raise the hit
ratio monotonically.
