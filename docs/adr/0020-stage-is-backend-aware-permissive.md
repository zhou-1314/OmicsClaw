# Stage is a backend-aware control, permissive not jailed

**Status:** accepted (2026-05-30)

A Bench **stage** (Read / Ideate / Analyze / Write) is a *backend-aware* concept, not a
frontend-only prompt skin. The desktop chat request carries a `stage` field (alongside the
existing `system_prompt_append` and the new `thread_id`); the backend uses it to inject the
stage's system-prompt fragment and to select the **default tool subset** exposed to the LLM
that turn. This preserves OmicsClaw's deterministic-control posture: the Read stage does not,
by default, expose the heavyweight analysis / file-writing tools.

Stages are **permissive, not jailed**: each stage's tool subset is a *default*, not a hard
boundary. When a user expresses analysis intent inside Read, the agent proposes a one-click
switch to Analyze (or the UI offers it) and the full toolset unlocks — switching stage never
switches engine. A pure frontend-only stage (swap `system_prompt_append`, leave every tool
live) was rejected: it gives no guarantee a Read turn won't launch a long analysis,
collapsing the distinct "read" vs "compute" mental states the stages exist to separate.

**Authority vs. order.** The stage fragment is *subordinate in authority* to SOUL.md and the
research-stance persona — it is additive guidance and cannot override them. In the
concatenated system prompt the layers run **SOUL.md (immutable safety) → core://agent (base
persona) → research-stance persona → stage fragment → `system_prompt_append` (user tail)**,
and every non-SOUL layer is explicitly framed as guidance that must not contradict the layers
above it. ("Under" means lower authority, not earlier text.)

## Consequences

- New backend surface: a `stage` request field + a stage→tool-subset map. That map is the
  single source of per-stage capability; the frontend never gates tools itself.
- The stage fragment is subordinate to the research-stance persona and SOUL.md — it can steer
  behaviour but cannot override safety rules (see "Authority vs. order" above).
- `stage` rides the same request→envelope→loop plumbing template as the existing
  `scoped_memory_scope` envelope field (`omicsclaw/runtime/agent/envelope.py:29`); it is not
  new transport machinery.
