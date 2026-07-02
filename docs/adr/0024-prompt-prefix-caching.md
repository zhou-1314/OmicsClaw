# Maximize LLM automatic prefix-cache hit rate by enforcing a Stable prefix invariant: freeze the tool list per session, re-tier system layers by volatility via `placement`, make history append-only between deliberate re-warms, and add prefix-segment cache diagnostics

## Status

Accepted (2026-05-30). New subsystem vocabulary lands in `docs/CONTEXT.md`
§"Prompt Prefix & Caching". Reverses the per-turn tool-list-compression
(`predicates.py`, Phase 1) and the per-turn history sliding window
(`budget.trim_history_to_budget`) for cache-supporting providers.

## Context

A grilling session on 2026-05-30 (`/grill-with-docs`, seven branch-points)
examined a user thesis: *"`DeepSeek-Reasonix` has an excellent cache-hit-rate
design; adapt it to improve OmicsClaw."* Reasonix reaches a 99.82% prompt-cache
hit rate by treating the request as three regions with hard invariants — an
**immutable prefix** (`system + tool_specs + few_shots`, frozen at session
start), an **append-only log** (messages only appended, never edited in place),
and a **volatile scratch** (reasoning, never serialized) — plus per-turn
diagnostics that hash prefix segments and *infer* the reason for any provider
cache miss.

OmicsClaw drives an **OpenAI-compatible** provider, defaulting to DeepSeek
(`query_engine.py:1055-1062`, `chat.completions.create` with **no** cache
parameters). DeepSeek and OpenAI both do **automatic prefix caching** keyed on
the byte-exact request prefix — so OmicsClaw needs no `cache_control`
annotations (the Anthropic mechanism); it needs only a byte-stable prefix.
Reading the code found three places that actively destroy that stability,
turning what should be ~90%+ hits into near-total misses:

**Finding 1 — per-turn tool-list-compression churns the tool segment.**
`predicates.py` gates 33 of 41 tools behind predicates that read the *current
user message* (`anndata_or_file_path_in_query`, `pdf_or_paper_intent`,
`web_or_url_intent`, …); `select_tool_specs` (`registry.py:13-74`) recomputes
the subset every request (`engine/loop.py:170-174` →
`query_engine.py:1046-1054`). The tool list is serialized at the **front** of
the cached prefix, so a per-turn change in membership breaks the cache at the
tools segment — discarding the system prompt and the entire history that
follow. The user confirmed the feature's motive was **token cost**, which
automatic caching already solves: a DeepSeek cache-hit token costs ~10% of a
miss token, so once tools sit in a cached prefix, compressing them is a net
loss.

**Finding 2 — the system prompt interleaves volatile layers into the prefix.**
The prompt is assembled from ordered layers (`context/layers/__init__.py:996-1155`,
sorted by `(order, name)` at `assembler.py:298`). Stable layers (`base_persona`
o10, `surface_voice_rules` o11, `output_format` o15, `knowhow_constraints` o60,
`mcp_instructions` o80) are interleaved with **query-volatile** layers — the
predicate-gated rule layers (o12-19), `memory_context` (o40), `skill_context`
(o42), `capability_assessment` (o50), `knowledge_guidance` (o52), `plan_context`
(o55) — all at `placement="system"`. The first volatile layer to change breaks
the cache from that order onward. Notably the architecture **already** supports
the fix: `placement` can be `"system"` or `"message"`, and `transcript_context`
(o58) is already `"message"` (rendered into the user turn via
`build_user_message_content`, `assembler.py:216-254`).

**Finding 3 — history is not append-only; a sliding window shifts it every
turn.** `trim_history_to_budget` (`budget.py:61-107`) returns "the newest
contiguous history suffix that fits the budget" (default `max_messages=50`),
dropping the oldest block. For any session past the window, every new turn
pushes out the oldest message, so the first message after the prefix changes
every turn — discarding *all* history caching. (By contrast, tool-result
micro-compaction, `compaction.py:231-270`, is already cache-safe: its full→compact
transition is monotonic and deterministic, so old history freezes; only the
`keep_recent` tail is re-sent. And `apply_deepseek_reasoning_passback`,
`patches.py:29-58`, is idempotent — it stamps missing `reasoning_content` once
and never rewrites it, so it does not churn the prefix.) The existing
`CONTEXT_COLLAPSE`/`AUTO_COMPACT` (`compaction.py:542-581`) already handle
overflow by folding old messages into a frozen `system` summary — a deliberate
re-warm — making the per-turn slide redundant *and* harmful.

**Finding 4 — cache is unobserved.** Only the Desktop surface reads cache
tokens back (`surfaces/desktop/server.py:802-822`, covering DeepSeek
`prompt_cache_hit_tokens`, OpenAI `cached_tokens`, Anthropic
`cache_read_input_tokens`) and only for billing totals — no hit ratio, no
miss-reason inference, and CLI/Channel surfaces see nothing.

### Mechanism choice

OmicsClaw targets **automatic prefix caching** (DeepSeek default + OpenAI),
**not** Anthropic `cache_control` breakpoints. Byte-stability of the prefix is
provider-neutral and is the prerequisite for *every* caching provider
(including Anthropic); explicit breakpoints are deferred as an optional later
adapter. This is also a deliberate divergence from Reasonix on one point:
Reasonix sorts tools with a locale-independent codepoint compare because its
tools arrive via dynamic MCP registration; OmicsClaw's tools are **statically
registered**, so their order is already deterministic and needs **no** re-sort —
the only requirement is to stop varying the subset per request.

## Decision

Enforce one **Stable prefix invariant**: within a session, the **Prompt prefix**
(serialized `tools` + the `system` message) is byte-identical across
consecutive requests; it changes only at deliberate, logged **cache re-warm**
events. Five resolutions:

1. **Frozen tool list.** Drop per-turn query-keyword gating. Filter the tool
   payload once by `surface` (a session constant) at session start, freeze it in
   static registration order, and reuse it verbatim for every turn. No re-sort.
   (Rejected alternatives: *monotonic activation* — gated tools stay once
   triggered — bounds breaks to O(categories) but adds session state for a
   cost-only win caching already covers; *provider-conditional compression* —
   keep per-turn gating on non-caching providers — doubles the code path to
   maintain for a local-only token-window concern, deferred under YAGNI.)

2. **`placement` is the cache boundary.** Re-tier every system layer by
   *session-volatility*, not semantic role. Session-stable layers stay
   `placement="system"` and are snapshotted once at session start. Per-turn
   layers — the predicate-gated rule layers (o12-19), `skill_context`,
   `capability_assessment`, `knowledge_guidance`, `plan_context` — move to
   `placement="message"`, rendering as **Volatile context** into the latest user
   message (the append-only tail), where they cost nothing in cache.
   (Rejected alternative: freeze the whole assembled `system` string per session
   and drop per-turn memory/capability entirely — simplest, but loses the
   per-turn relevance signal and the system-instruction authority of preferences.)

3. **Memory: session-scoped stays in the prefix, query-ranked is Volatile.**
   `memory_context` is session-scoped, not query-scoped
   (`session_manager.load_context(session_id)`, `assembler.py:330-332`), changing
   only on a memory **write** — so it stays in the `system` tier and is
   byte-stable between writes; a mid-session write is one explicit, logged
   re-warm (the analogue of Reasonix's pinned-memory blocks). **Correction (post
   adversarial review):** `scoped_memory_context` is *query-RANKED* (its content
   reorders per query), so it is **Volatile context** (`placement="message"`),
   **not** snapshotted into the prefix — the original grill assumption that it was
   session-stable was wrong. Likewise `knowhow_constraints` is query/skill/domain-
   matched and is Volatile context. (Rejected alternative: move all memory to the
   message tail — that would demote session-scoped preferences from system
   instruction to per-turn context unnecessarily.)
   **Refinement (Decision-2, 2026-07-02):** `memory_context` is split by
   *write-frequency*, not moved wholesale. Durable identity — `project_context` +
   user `preference` — rarely changes, so it stays in the `system` tier `## Your
   Memory` block (cache-warm + authoritative). The *volatile work-state* —
   `dataset` (preprocessing_state), recent `analysis` runs, and `insight` — is
   written repeatedly mid-session, so it now rides the message tail as
   `project_state_context` (`placement="message"`, `## Current Work State`) via
   `session_manager.load_context_layers()`. Consequence: a mid-session write to
   dataset/analysis/insight no longer re-warms the prefix; only a write to
   preferences/project_context does. (Legacy managers exposing only `load_context`
   fall back to whole-memory-in-`system`, byte-identical to the prior behavior.)

4. **History append-only between collapses.** Remove the per-turn sliding
   `trim_history_to_budget` slide from the model path. History grows append-only;
   overflow is handled *only* by context collapse, which folds old messages into
   a frozen `system` summary (one re-warm). A large `max_chars` ceiling is kept
   as a backstop, but it **triggers a collapse**, never a silent per-turn slide.
   Tool-result micro-compaction is kept as-is (already monotonic/deterministic);
   `keep_recent` may be tuned. (Rejected alternative: keep the sliding window —
   minimal change, but long-session history hit rate cannot rise.)

5. **Cache-hit diagnostics (full).** Read `hit`/`miss` tokens in the loop's
   usage callback (lift the per-provider extraction out of the Desktop surface so
   all surfaces share it via the existing `usage_accumulator`); compute per-turn
   and per-session hit ratio; hash the **Frozen tool list** and the stable
   `system` prefix each turn and compare against the prior turn to infer the miss
   reason (`cold-start`, `tool-list-changed`, `system-changed`, `history-shifted`);
   expose it on the Desktop banner / CLI / logs; and land a **CI regression
   assertion** (a multi-turn demo session must hold hit ratio above a floor from
   turn 2 on) so re-introducing per-turn prefix variation fails the build.

### Deferred to sensible defaults (not separately grilled)

- Predicate-gated *rule* layers move to `placement="message"` as Volatile
  context (keeps adaptivity, zero prefix cost).
- Model is fixed within a session; switching model mid-session is one re-warm.
- Diagnostics hash exactly two segments (tool list + stable system prefix).
- Historical `reasoning_content` is left in place (stable bytes, cached cheaply),
  not stripped — stripping would rewrite history bytes and break the prefix.

## Consequences

### Positive

- The dominant cache-busters (per-turn tool churn, per-turn history slide) are
  removed. **Measured** on real DeepSeek (`scripts/measure_prefix_cache.py`, a
  6-turn omics dialogue, `deepseek-v4-flash`): session prompt-cache hit ratio rose
  from **12.1% → 81.8%** (turns 2+ run 94–98%; turn 1 is the cold-start), and
  input-token billing fell **~44%** (cache-hit tokens cost ~10% of a miss) even
  though the new path sends the full frozen tool list + accumulating context every
  turn.
- The fix reuses existing machinery (`placement`, context collapse,
  `usage_accumulator`); it is re-tiering and call-site removal, not new
  infrastructure.
- Diagnostics make the Stable prefix invariant a *measured, CI-asserted*
  property, not a hope — the invariant cannot silently regress.

### Negative

- The first (cold) turn of every session pays full price for the whole tool list
  + stable prefix; the design only wins from turn 2 on. Single-turn sessions see
  no benefit (and a slightly larger prompt).
- Per-turn tool/rule adaptivity is lost from the prefix — all surface-eligible
  tools are always visible. Acceptable: the user's motive was cost (cache solves
  it), not selection quality; smaller local models on Ollama (no provider cache)
  keep a larger prompt, a deferred concern.
- Between collapses, the history (and thus per-turn uncached cost on a miss) can
  grow larger than the old sliding window allowed before a collapse fires.
- Memory writes and model switches now have a visible one-turn cost cliff
  (the re-warm), surfaced by diagnostics as `system-changed`.

### Open

- **Anthropic `cache_control` adapter** — layering explicit breakpoints on top of
  the byte-stable prefix for the Anthropic provider; deferred, not required by
  the default DeepSeek path.
- **Ollama / non-caching providers** — whether to retain per-turn compression
  there as a context-window (not cost) measure; deferred under YAGNI.
- **`keep_recent` tuning** — the perpetually-re-sent recent tool-result tail is
  bounded but non-zero; its size is a cost knob to measure once diagnostics land.
- **Provider-blind `max_prompt_chars`** (review finding 6) — **RESOLVED.**
  `engine/loop.resolve_max_prompt_chars(model)` now derives the context-collapse
  budget from `get_context_window(model)` (`window × 3.0 chars/tok × 0.5`),
  **never exceeding** the deliberate `DEFAULT_MAX_PROMPT_CHARS` budget (so an
  over-optimistic reported window can't disable collapse) and **shrinking** for
  known-small windows.
  *(2026-07-02: `DEFAULT_MAX_PROMPT_CHARS` raised 96000 → 256000 and made a single
  named policy constant in `compaction.py`. Git archaeology found 96000 was a
  legacy, uncalibrated pre-ADR-0024 default (= a round 32k tokens) that capped the
  entire fleet at ~3% of window and left the window-relative shrink branch dead;
  256000 (~85k tok) recovers long-session capability — cheap under prefix caching
  (extra history billed at the cache-hit rate, ~no added latency, fewer re-warm
  collapses) — and `min(256000, window×1.5)` revives the shrink branch for
  windows below ~170k tokens. reactive-413 compaction remains the net.)*
  Unknown windows (e.g. Ollama, which report `None`) keep the default and are
  tuned via the new `OMICSCLAW_MAX_PROMPT_CHARS` env override; reactive
  compaction on a context error remains the ultimate net. Test:
  `tests/engine/test_loop.py::test_resolve_max_prompt_chars_scales_with_window`.
- **Per-turn system-prefix mutations** (review finding 8) — **RESOLVED (session
  hooks).** The session-start/resume hook fragments (`## Active Session Hooks`)
  now render into the user turn as **Volatile context** (`query_engine`’s
  `_prepend_volatile_to_user_content`) instead of being appended to the `system`
  prefix, so a resume hook that injects changing content can no longer churn the
  prefix. The **mode hint** is deliberately left in the `system` tier: it is
  session-stable and a mode switch is a rare, sanctioned **cache re-warm** (a per
  turn duplication in the user turn would be wasteful). Test updated:
  `tests/test_query_engine.py::test_run_query_engine_applies_session_context_hooks_and_tool_notices`.

## Relationship to prior ADRs

- **ADR 0015** (exact-skill assisted parameterization): the Analysis Router's
  route context becomes **Volatile context** rendered into the user message
  (`placement="message"`), not a `system_prompt_append` ahead of history — a
  placement change only; the assisted-parameterization behavior is unchanged.
- **ADR 0013/0014** (autonomous paths): unaffected — the autonomous data-grounded
  context they inject is per-turn and follows the same Volatile-context rule.
- **ADR 0008** (decompose `run_query_engine`): the diagnostics hook attaches to
  the existing usage-delta path; reactive compaction stays the explicit re-warm
  it already is.
- **ADR 0005** (surfaces umbrella): unchanged — lifting cache-token extraction
  from `surfaces/desktop/server.py` into the loop makes all three surfaces share
  one diagnostics path, consistent with the single-engine-entry principle.
