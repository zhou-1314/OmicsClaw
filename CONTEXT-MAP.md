# Context Map

OmicsClaw has multiple bounded contexts. Each owns its vocabulary in a local
`CONTEXT.md`; this map says where they live and how they relate.

## Contexts

- [Memory & Surfaces](./docs/CONTEXT.md) — the graph-backed agent memory and the three
  user-facing Surfaces (Channel / Desktop / CLI), plus analysis routing.
- [Runtime](./omicsclaw/runtime/CONTEXT.md) — code-driven orchestration: workflows, the
  workflow runtime, and consensus.
- [Bench](./docs/bench/CONTEXT.md) — the desktop research-assistant workspace
  (read → ideate → analyze → write).

## Relationships

- **Bench → Memory & Surfaces**: Bench is a *page on the Desktop Surface*, not a new
  Surface. It binds each investigation thread to a `project://<id>` subtree of the graph
  Memory System.
- **Bench → Runtime**: Bench's Analyze stage invokes workflows (consensus / pipeline) and
  skills through the existing Analysis Router; it never authors orchestration.
- **Memory & Surfaces ↔ Runtime**: a workflow / consensus run writes to `analysis://`
  memory; the Desktop Surface streams its progress.
