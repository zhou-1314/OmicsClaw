# Bench is a research-continuity workspace, not a daily companion

**Status:** accepted (2026-05-30)

OmicsClaw-App already ships a stateless, task-scoped `/chat` analysis console. The new
desktop "research assistant" (working codename **Bench / 研究台**) is positioned as a
*study-scoped research-continuity workspace*: its differentiator is a durable
**investigation thread** that carries one research project across the
Read → Ideate → Analyze → Write lifecycle — **not** a CodePilot-style persona
companion with a proactive daily heartbeat.

We considered two framings: (A) research continuity ("never re-explain my project")
and (B) companion warmth (named persona + daily heartbeat, à la the admired CodePilot
design). We chose (A). The user's purpose is a multi-day, stateful research lifecycle,
which the per-task `/chat` structurally cannot hold; and a "warm daily mascot" tensions
OmicsClaw's "never guess, trace to skill" safety posture. Persona is kept thin — an
additive *research-stance* system fragment over `core://agent` (SOUL.md) that can shape
tone only and cannot override its safety rules. The proactive heartbeat and any
scheduler are deferred to v2 and, when built, stay strictly read-only (propose, never
auto-execute).

## Consequences

- Bench reuses the single `core.llm_tool_loop`, the Desktop Surface SSE/permission/
  tool-use plumbing, and the graph Memory System. There is **one agent engine at two
  zoom levels**: `/chat` = task console, Bench = study workspace, bridged bidirectionally
  ("Run in Chat" / "Save to thread"). No new agent runtime is introduced.
- Bench is a **page on the existing Desktop Surface**, not a fourth Surface.
