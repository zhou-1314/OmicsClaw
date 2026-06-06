# Phantom-completion as a termination-branch pathology (local providers)

## Status

Accepted (2026-06-06). Extends ADR 0007 (loop-state and pathology
detection); scoped by ADR 0026 (local LLM is a Provider).

## Context

A user running OmicsClaw on the Desktop Surface with a **local Ollama Gemma 4
12b** asked it to "run preprocessing on `slideseqv2_mouse_hippocampus.h5ad`".
The reply was a confident, detailed report — *"I have started spatial
preprocessing … QC charts generated … low-expression genes removed …"* — but
**no analysis ran**. The model fabricated a completion.

Reproduced against the real model + real tool set (49 tools) + real system
prompt: gemma4:12b reliably emits a tool call on turn 0, but in the multi-turn
loop it intermittently (~1-in-4) terminates a turn with a prose narration that
*announces or claims* the analysis instead of calling the `omicsclaw`
skill-runner. The narration even borrows real skill metadata (the `qc` / `count`
renderer names from context), so it reads as a genuine result.

Two facts make this a local-model problem specifically:

1. **The loop trusts a no-tool-call response as final.** `run_query_engine`
   (`query_engine.py`, the `if not last_message.tool_calls:` branch) returns
   the message content as the answer, with no check that the requested work
   was actually performed.
2. **Local models silently truncate and miss tool calls.** ADR 0026 already
   documents this as expected for the local path: *"local small models drive
   the multi-turn, multi-tool agent loop less reliably … occasional missed
   tool calls."* Cloud providers reliably emit tool calls **and** raise real
   context-overflow errors, so they do not exhibit this failure.

ADR 0007 built the machinery for exactly this class of problem: `LoopState` +
`loop_pathology.detect()` + a **soft-correction** reaction (inject a corrective
message, let the model recover, `MAX_TOOL_ITERATIONS = 20` as the terminal
backstop), surfaced as a `PathologyDetected` event. The two existing signals
(`pingpong`, `repeated_failure`) read the **post-execution history** and fire in
the branch *after* a tool runs. This failure is the opposite shape: it is the
**absence** of a tool call at the **termination branch**.

## Decision

Add a third pathology, **`phantom_completion`**, detected at the no-tool-call
termination branch of `run_query_engine`, reacting through ADR 0007's existing
soft-correction path. Five decisions, resolved in a grilling session, in
dependency order:

**Q1 — Home: a pathology signal in the ADR 0007 framework**, not an ad-hoc
`if` at the termination branch. It is `PathologySignal(kind="phantom_completion",
tool_name=None, count=1)`, reuses `_is_new_pathology` dedup, the
`append_user_message` correction, and the `PathologyDetected` event. The only
extension is the **detection touchpoint**: a sibling pure function
`detect_phantom_completion(content, state, enabled)` evaluated against the
*current* message rather than `detect(state)`'s post-execution history — the
symptom is the missing tool call, not a pattern over prior calls.

**Q2 — Predicate: structure gate + intent marker.** The detector fires only
when ALL hold: (a) the guard is enabled, (b) **no execution tool** has run this
loop (`EXECUTION_TOOLS = {omicsclaw, custom_analysis_execute,
autonomous_analysis_execute, replot_skill}`), and (c) the message **announces
or claims action** via a curated EN+ZH intent-marker list — first-person
commitment ("I will run …", "我将采用 …") or a completion claim ("已生成 …",
"here are the results"). The asymmetry chosen: a **false positive** (nudging a
good reply) costs one wasted slow local turn, while a **false negative** merely
degrades to today's behaviour — so the predicate favours recall on the failure
while keeping false positives cheap. To bound them: EN markers pair the
commitment with an action verb ("i will *run/start/proceed*…") so a generic "I
will help you" does not trip; ZH markers use bare first-person commitment
("我将/我会/我已/正在") because a capability-describing intro uses "可以/能",
not "我将"; the execution-tool gate keeps a genuine post-run summary from being
flagged; and the nudge copy ("if no tool is needed, say so plainly") lets the
rare mis-fire resolve gracefully in one turn. A conversational reply with no
commitment (e.g. "introduce yourself" → "我是 OmicsBot，可以帮你…") is left
alone.

**Q3 — Reaction: nudge once.** `_is_new_pathology` (which dedups on
`(kind, tool_name)`, here `(phantom_completion, None)`) bounds it to a single
nudge per loop. The fabricated narration is **not** accumulated into the final
answer. If the model ignores the nudge and narrates again, control falls
through to the normal budget/return path — no two-stage escalation, consistent
with ADR 0007's single-backstop stance.

**Q4 — Scope: Ollama only, via a config flag.** A new
`QueryEngineConfig.phantom_completion_guard: bool` (mirroring
`deepseek_reasoning_passback`) is set at config construction
(`engine/loop.py`) from `provider_has_unreliable_tool_calling(provider_name)`
(`providers/patches.py`, today `name == "ollama"`). The detector stays a pure
function with no provider knowledge; the **call site** gates on the flag. Cloud
providers never arm the guard and are byte-for-byte unaffected.

**Q5 — Reaction only; no system-prompt hardening (v1).** A static
anti-fabrication instruction in the base system prompt was considered and
deferred: the nudge is the validated fix, the marginal benefit of hardening was
not supported by the repro data, and a global prompt change would touch the
prefix cache (ADR 0024) and add tokens for cloud. If traces later show the
wasted-turn rate is painful, add a **local-only** instruction via
`system_prompt_append` in a follow-up — the ADR 0007 "tune in a follow-up PR"
discipline.

## Considered Options

- **Ad-hoc guard at line 1365** (no framework). Rejected: re-creates the
  local-variable accretion ADR 0007 was written to stop, and loses the
  telemetry (`state.signals`) and `PathologyDetected` surfacing for free.
- **Extend `detect(state)` to catch a run of no-tool-call turns.** Rejected: a
  single premature termination must be caught immediately, not after a window;
  and `detect(state)` reads histories, not the current message.
- **Tool-result injection (ADR 0007's stated channel).** Not applicable: a
  `role="tool"` message needs a `tool_call_id`, which does not exist when the
  model called no tool. A `user`-role nudge is the valid channel — and is in
  fact what the *implemented* `_format_pathology_correction` already uses via
  `append_user_message`.
- **Pure-structure predicate** ("called a tool but no execution tool, then
  terminated"). Rejected alone: cannot distinguish a turn-0 fabrication from a
  legitimate conversational reply (both have zero tools), and over-fires on
  pure-information questions. The intent marker is the precision filter.
- **System-prompt hardening (global or local).** Deferred per Q5.
- **All providers, not just Ollama.** Rejected per Q4: cloud models emit tool
  calls reliably; arming the guard there is unnecessary surface and risks
  nudging a correct cloud answer.

## Consequences

**Wins**
- The headline bug — a local model fabricating a completed analysis — is caught
  and corrected in-loop; the reproduced ~25% narration rate is recovered to a
  real tool call by a single nudge.
- Reuses ADR 0007 wholesale: `LoopState`, dedup, correction, event, telemetry.
  The only new surface is one pure detector function and one config flag.
- The pattern generalises further: any future "terminated without doing the
  work" detector is another pure function over `(content, state)`.

**Costs**
- `PathologySignal.kind` grows to three; exhaustive matches on it gain one arm
  (only `_format_pathology_correction` matches exhaustively today).
- The intent-marker list is a curated heuristic that drifts with phrasing and
  language. By construction a miss is harmless (degrades to prior behaviour),
  but the list needs occasional extension as new phrasings surface.
- A false positive costs one extra local turn. Mitigated by the precision-first
  predicate and the execution-tool gate; bounded to one nudge.

## Verification

Per ADR 0007's L0/L1 gates.

- **L0** (`tests/test_loop_pathology.py`): `detect_phantom_completion` fires on
  a claim with no execution tool (multi-turn and turn-0); stays silent on a
  conversational reply, after an execution tool ran, on empty content, and when
  disabled; EN+ZH marker coverage.
- **L1** (`tests/test_query_engine_pathology.py`): against a mock LLM —
  narrate-then-recover injects exactly one correction and the model reaches the
  execution tool; a stubborn model is nudged at most once and its narration is
  returned (no loop); guard off leaves the cloud path untouched; a genuine
  post-execution summary is not nudged.
- **Manual real-model repro** (not CI; needs Ollama + gemma4:12b): the
  multi-turn harness over the real 49-tool set confirms the detector fires on
  real gemma narrations and the real correction recovers them.

## Open questions

- **Marker tuning.** The EN+ZH list is v1; extend from real traces. A follow-up
  PR, not a follow-up ADR.
- **Other local providers.** `provider_has_unreliable_tool_calling` is Ollama
  only today. A future `custom` self-hosted llama.cpp path (ADR 0026's advanced
  alternative) may want arming; add it to the helper when it lands.
- **Surfacing copy.** `PathologyDetected` for `phantom_completion` renders via
  the existing per-Surface warning line. Whether the Desktop Surface wants
  distinct copy ("re-running: the model did not execute the analysis") is a
  Surface decision, deferred.
