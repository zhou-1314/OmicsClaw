# Collapse the context-budget/compaction control plane onto one unit — tokens — so budget, status, and compaction thresholds derive from a single token budget, retire the parallel char machinery, and give the dark-shipped LLM collapse summary a real opt-in surface

## Status

Proposed (2026-07-03). Supersedes ADR 0024's char-budget stance (its
"Provider-blind `max_prompt_chars` — RESOLVED" open item and the
`DEFAULT_MAX_PROMPT_CHARS` policy text). New vocabulary reuses `docs/CONTEXT.md`
§"Prompt Prefix & Caching" unchanged; the caching decision and every prefix-cache
invariant of ADR 0024 remain in force.

## Context

The context-budget and compaction control plane is five half-migrated,
non-interlocking pieces that measure one concept — how full the prompt is — in
two different units with two competing statuses. This is a framework conflict,
not a set of independent bugs: each piece was added in a separate slice and never
reconciled, so the enforcement path, the observability path, and the
compress-to-target path do not share a denominator.

**Piece 1 — enforcement is CHAR-based.** The binding compaction budget is
`resolve_max_prompt_chars(model)` (`omicsclaw/engine/loop.py:199-208`), computed
as `min(DEFAULT_MAX_PROMPT_CHARS=256000, window_tokens × _CHARS_PER_TOKEN(3.0) ×
_PROMPT_BUDGET_FRACTION(0.5))` (`loop.py:185-187`;
`DEFAULT_MAX_PROMPT_CHARS` defined at
`omicsclaw/runtime/context/compaction.py:79`). Compaction triggers
`collapse_trigger_ratio=0.82` / `auto_compact_trigger_ratio=0.92`
(`compaction.py:89-90`) are ratios *of that char budget*.

**Piece 2 — the STATUS is TOKEN-based.** `ContextBudgetStatus`
(OK/WARNING/COMPRESS/CRITICAL/BLOCK, `omicsclaw/runtime/context/budget.py:16-23`)
is classified from `used_tokens / effective_context_capacity`
(`budget.py:33-40, 49-64`), with `CHARS_PER_TOKEN = 3.0` (`budget.py:30`) as the
bridge back to the char world. One concept, two units, joined by a rough constant
the code itself flags as a global proxy.

**Piece 3 — two statuses coexist, one of them inert.** `_budget_status`
(`compaction.py:160-168`) classifies against the *model window*, while
`local_budget_status` (`budget.py:67-81`) classifies against the *char budget*.
Both ride on `PreparedModelMessages` (`compaction.py:139-140`). The
window-relative one is decision-useless: its own docstring states the char budget
"already caps context to a few percent of the window, so that status is ~always
OK" (`budget.py:70-77`).

**Piece 4 — compress-to-target is wired separately.** `collapse_target_ratio` /
`auto_compact_target_ratio` default `None` on the dataclass
(`compaction.py:106-107`) and are set to `0.55` / `0.40` by the engine at
config-build time (`loop.py:195-196, 221-222`) — a third path, disconnected from
the status computation.

**Piece 5 — the LLM collapse summary is dark-shipped.**
`collapse_llm_summary_enabled` defaults `False` (`compaction.py:126`) and is read
at `compaction.py:331`, but **no config, env, or CLI surface can set it `True`** —
a repo-wide grep finds only those two lines outside tests. A shipped opt-in
feature with zero opt-in surface.

## Decision

Collapse the plane onto one unit. Everything that measures prompt fullness —
budget, status, trigger, target — derives from a single token budget. Five
resolutions.

1. **Token-native control plane.** Budget, status, and every compaction threshold
   are expressed in tokens. Introduce one token estimator (tiktoken where an
   encoding is known; a `chars / 4` fallback otherwise). State it honestly: for
   non-OpenAI providers this count is an **approximation**. The goal is a single
   coherent unit flowing budget → status → compaction, **not** per-provider
   exactness — the char↔token double-bookkeeping is what we are removing, and a
   second approximate unit would reintroduce it.

2. **Retire the char machinery.** Remove `DEFAULT_MAX_PROMPT_CHARS=256000`
   (`compaction.py:79`), `CHARS_PER_TOKEN=3.0` / `_CHARS_PER_TOKEN`
   (`budget.py:30`, `loop.py:185`), the char-based `resolve_max_prompt_chars`
   (`loop.py:199-208`) and the `max_prompt_chars` field it feeds, the char
   estimator's role as the *budget* denominator. Retire the inert window-relative
   status *classification* (`_budget_status`, `compaction.py:160-168`) and keep
   the single actionable status (formerly `local_budget_status`), now classified
   in tokens.

   **Wire-contract caveat (S3).** The compaction status crosses the Desktop
   boundary — `PreparedModelMessages.budget_status` → `CompactionEvent`/SSE
   payload key `budgetStatus` (`compaction.py:1007,1062`,
   `_compaction_event_bridge.py:46`), consumed by the App badge and asserted by
   `tests/test_server_compaction_event.py:96`. **Do not drop the wire key.** Keep
   `budgetStatus` (and the field feeding it) stable for at least one release, now
   populated by the single token-native actionable status — the badge becomes
   *more* useful (an actionable value instead of the ~always-`OK` window status);
   update the bridge and the SSE contract test accordingly.

   **Multimodal token accounting (S2).** `estimate_message_size` does not merely
   feed the token estimator — it *becomes* the per-message token estimator,
   walking the message structure and counting tokens per content block: tiktoken
   over text, a **bounded per-image token surcharge** `_IMAGE_BUDGET_TOKENS`
   (≈1300 — the token analogue of today's `_IMAGE_BUDGET_CHARS=4000`,
   `budget.py:84-90,110`, tested at `tests/test_context_budget.py:100`), and the
   tool-call payload. It must **not** tokenize serialized base64 (catastrophic
   over-count) nor flatten text past images (near-zero under-count): preserving
   F4's bounded-image-surcharge semantics is a hard requirement of the migration.

3. **Token budget with a latency-backstop cap.**
   `max_prompt_tokens = min(TOKEN_CAP, floor(effective_capacity ×
   PROMPT_BUDGET_FRACTION))`, where `effective_capacity = context_window −
   reserved_output`, `PROMPT_BUDGET_FRACTION = 0.5`. **PROPOSED — confirm
   `TOKEN_CAP = 85_000` and `reserved_output = 8192`.** Re-justify the cap
   against ADR 0024's own reasoning: prefix caching already makes accumulated
   history *cheap to bill* (a cache-hit token costs ~10% of a miss), so the cap is
   **not** a cost policy — it is a **latency** backstop. Caching does not bound the
   cold-turn / re-warm / per-miss latency of a very large prompt: without a cap, on
   a 1M-token window the uncapped budget is `floor(0.5 × ~992k) ≈ 496k` tokens, so
   collapse would not fire until `~0.82 × 496k ≈ 407k` tokens, making every cache
   miss and every deliberate re-warm pay a multi-hundred-k-token round trip.
   On estimator consistency (S1): the `256000 / 3.0 ≈ 85k` heuristic behind this
   number assumes the *old* `CHARS_PER_TOKEN = 3.0`, which is **not** the new
   estimator's ratio (tiktoken; `chars / 4` fallback). Under a `/4` fallback,
   `85_000` tokens ≈ 340k chars and collapse fires at `0.82 × 85k ≈ 70k` tokens
   ≈ ~278k chars — **larger** than today's char collapse point
   (`0.82 × 256000 ≈ 210k` chars). So `85_000` is **not** strict behaviour parity:
   it is a deliberate, modest budget increase (~30% more history before collapse)
   in the new unit, accepted because prefix caching makes the extra history cheap.
   If strict parity is preferred, set `TOKEN_CAP ≈ 52_000` (≈ 210k chars ÷ 4); the
   ADR proposes the larger `85_000` and states the change honestly. The `min()`
   keeps the window-relative shrink branch alive for small-window models (any
   window below `2 × TOKEN_CAP + reserved_output = 178_192` tokens gets the
   smaller window-relative budget). Unknown windows (Ollama reports `None`) fall back to
   `TOKEN_CAP`; the env override (renamed to `OMICSCLAW_MAX_PROMPT_TOKENS`) tunes
   per deployment; reactive-413 compaction remains the ultimate net.

4. **One coherent chain.** One token budget → one five-level status derived from
   the **same** budget (thresholds 65/80/90/96, `budget.py:43-46`) → compaction
   gated on that budget (`collapse@0.82`, `auto@0.92`) → **static** targets
   (`collapse → 0.55`, `auto → 0.40` of the budget) → summary. **PROPOSED —
   confirm: keep the targets STATIC, not status-driven.** Rationale:
   `target-ratio < trigger-ratio` is the byte-stability guardrail — after a
   collapse the re-warmed prompt must sit safely below the trigger so it does not
   immediately re-collapse (`compaction.py:97-105` — "one-compaction = one-rewarm").
   A dynamic, status-driven target risks a re-collapse loop that would churn the
   `system` prefix every turn and destroy the Stable prefix invariant. Coherence
   comes from **one unit + one budget** feeding both enforcement and the
   observability status (surfaced through the existing `CompactionEvent` / SSE
   path), **not** from status driving a moving target. The status thresholds and
   action triggers interleave into one escalation — `COMPRESS (80%) < collapse
   (82%) < CRITICAL (90%) < auto (92%) < BLOCK (96%)` — so the advisory status
   reads one notch ahead of each action, and `CRITICAL` / `BLOCK` are reached only
   when compaction could not reduce (irreducible content).

5. **Make the LLM collapse summary the default; keep a deterministic escape
   hatch.** Wire `collapse_llm_summary_enabled` **default ON**; the env toggle
   `OMICSCLAW_COLLAPSE_LLM_SUMMARY=0` turns it off for deterministic/replayable
   runs (CI, byte-exact regression fixtures). **RATIFIED — default ON as the
   target (2026-07-03), shipped ON only once the fidelity gate below lands:** the
   maintainer prioritises the LLM's language understanding for higher-fidelity
   collapse summaries over determinism/replay. The path is bounded and byte-safe:
   length cap, UTF-8-byte cap, anti-tool-mimicry, and fallback to the
   deterministic template on any timeout/error (`compaction.py:430-445`).

   **Ratification prerequisite (B1) — the content-fidelity gate does not exist
   yet.** The live acceptance path (`compaction.py:430-445`) checks only
   non-empty / length / UTF-8-byte / anti-tool-mimicry; there is **no** check that
   the summary preserves required content, so default-ON would **silently drop**
   `full_result_path` references, error markers, or pending-work markers that the
   deterministic template keeps. **Flipping the default to ON is therefore gated
   on, in one change:** (i) pinning the required-token set — at minimum every
   `full_result_path` ref, error/traceback marker, and pending-plan marker present
   in the deterministic template for the same episode; (ii) implementing a
   **no-retry content-fidelity gate** that falls back to the template when any
   required token is missing; (iii) a test asserting the gate rejects a
   fidelity-dropping summary. Until (i)–(iii) land, the default stays **OFF**.

   Two properties then make default-ON safe for the Stable prefix invariant:
   (a) the summary is generated **once per collapse and frozen** into the `system`
   tier — reused byte-identically until the next collapse (one re-warm, not
   per-turn regeneration); (b) the length/UTF-8 cap is **load-bearing on every
   collapse** — it holds the LLM summary no longer than the deterministic
   template, so the re-warmed prompt stays below the collapse trigger
   (`target < trigger`, Decision 4) and cannot re-collapse.
   Reactive-413 compaction stays LLM-free and deterministic regardless (the
   emergency path never blocks on an LLM call); the LLM path is structurally
   unreachable from the snip/micro every-turn hot path and from `/compact`.

### Invariants preserved

This ADR changes only the **unit** and the **internal coherence** of the
budget/compaction control plane. Every ADR 0024 prefix-cache invariant is kept
unchanged and un-weakened:

- the **Stable prefix invariant** (byte-identical Prompt prefix across a session's
  turns);
- the **Frozen tool list** (filtered once per Surface, static order, no re-sort);
- the two-tier `placement` boundary (**Session prefix snapshot** in `system`,
  **Volatile context** on the append-only message tail);
- **append-only history between re-warms**;
- **context collapse as the sole overflow handler** (one collapse = one **cache
  re-warm**);
- `target-ratio < trigger-ratio` byte-stability (the re-warmed prompt sits below
  the trigger and cannot immediately re-collapse).

The token cap of Decision 3 lowers *when* collapse fires on a large-window model;
it does not change *how* collapse folds history or re-warms the prefix.

## Consequences

### Positive

- One denominator. Budget, status, trigger, and target are all read in tokens
  from one number, so "how full is the prompt" has exactly one answer and one
  status — the framework conflict (two units, two statuses, a disconnected target
  path) is gone.
- The removed symbols (`DEFAULT_MAX_PROMPT_CHARS`, `CHARS_PER_TOKEN`,
  `resolve_max_prompt_chars`, the char estimators-as-budget, the inert
  window-relative status *classification*) delete a whole parallel machine rather
  than add one — while the `budgetStatus` wire key is preserved (Decision 2, S3).
- The token cap makes the latency backstop explicit and re-justified, rather than
  an accidental side effect of a char constant divided by `3.0`.
- The LLM collapse summary becomes an actually-shippable, actually-testable
  feature instead of dead-on-arrival code.

### Negative

- Token counting adds a tiktoken dependency (with a `chars / 4` fallback) on a
  per-assembly path; for non-OpenAI providers the count is an approximation, so
  the budget is approximate — but coherently approximate in one unit, which is the
  stated goal.
- `TOKEN_CAP = 85_000` deliberately keeps the working prompt well below a large
  model's true window; sessions that could technically hold more history collapse
  earlier than an uncapped budget would. This is the accepted latency trade.
- Renaming `OMICSCLAW_MAX_PROMPT_CHARS` → `OMICSCLAW_MAX_PROMPT_TOKENS` is a
  breaking env change. A one-release alias must **not** read the old char value as
  a token count (`200000` chars ≠ `200000` tokens): either convert it
  (`old_chars ÷ 4 → tokens`) with a deprecation warning, or hard-deprecate the old
  var with an error that names the replacement. State the chosen path in the
  changelog.
- Once the gated default-ON ships (after the fidelity gate, Decision 5), every
  *proactive* collapse (never the reactive-413 emergency path) makes one bounded LLM
  round-trip — added latency at collapse time and an extra call cost, mitigated by
  a timeout + deterministic-template fallback. Collapses are overflow-gated, not
  per-turn, so this is not a hot-path cost.
- After the gated default-ON ships, collapse is no longer deterministic/replayable
  by default: the same session can produce different summary bytes across separate runs. Byte-exact regression
  fixtures must set `OMICSCLAW_COLLAPSE_LLM_SUMMARY=0`. Within a single run,
  byte-stability is preserved (generate-once-frozen + length cap).

### Open

- **`TOKEN_CAP` and `reserved_output` values (Decision 3)** — PROPOSED
  `85_000` / `8192`; confirm or retune once token-native billing/latency
  telemetry lands.
- **Static vs status-driven targets (Decision 4)** — PROPOSED static; the
  re-collapse-loop argument stands, but a maintainer may still want a bounded,
  hysteresis-guarded dynamic target later.
- **LLM-summary content-fidelity gate (Decision 5)** — default ON is the ratified
  *target*, but ships OFF until the no-retry content-fidelity gate is specified,
  implemented, and tested (prerequisite B1). Pinning the exact required-token set
  (`full_result_path` refs / error markers / pending-work markers) is part of that
  prerequisite.
- **Per-provider token accuracy** — deliberately out of scope; if a provider's
  own tokenizer diverges materially from the estimate, a provider-specific counter
  can be slotted behind the same single-unit interface without reopening the
  control plane.

## Relationship to prior ADRs

- **ADR 0024** (prompt prefix caching): **supersedes** its "Provider-blind
  `max_prompt_chars` — RESOLVED" open item and the `DEFAULT_MAX_PROMPT_CHARS`
  policy text (including the 2026-07-02 raise to 256000). The caching decision and
  all prefix-cache invariants remain in force and are explicitly preserved above;
  this ADR only re-expresses the budget those mechanisms trigger against in tokens
  and makes the surrounding status/target/summary machinery coherent with it.
- **ADR 0008** (decompose `run_query_engine`): unchanged — the token budget is
  built in the same `_build_compaction_config` seam (`loop.py:211-223`), and
  reactive compaction remains the explicit re-warm it already is.
- **ADR 0005** (surfaces umbrella): unchanged — the single actionable status still
  rides `PreparedModelMessages` and reaches all three Surfaces through the shared
  loop, now carrying one unit instead of two.