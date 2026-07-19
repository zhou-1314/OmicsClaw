# OmicsClaw-KG is a first-class dependency of Bench, with graceful degradation

**Status:** accepted (2026-05-30)

**Control-state refinement (2026-07-14):**
[ADR 0053](0053-make-control-plane-state-authoritative-for-project-conversation-and-turn.md)
preserves the Memory/KG knowledge boundary below but narrows "agent/study
state": Control Plane State owns Project and Conversation identity/lifecycle,
Memory owns associated Project knowledge, and KG owns scientific reading
knowledge.

Bench treats OmicsClaw-KG as an expected, bundled dependency rather than an optional
add-on. Its **Read** and **Ideate** stages *are* the KG (paper ingest, `kg_search`,
`ideate`), so without KG the lifecycle framing collapses. KG is mounted into the same
Desktop Surface FastAPI host under `/kg` (already wired at
`omicsclaw/surfaces/desktop/server.py:181` via `build_kg_router(enable_writes=True)`),
so the App talks to one origin on `:8765` and KG stays an independent process/release.

This is recorded because the code today registers KG **optionally**
(`_register_optional_kg_router` skips `/kg` when the package is absent), which
contradicts Bench's posture — a future reader needs to know the contradiction is
intentional. We considered making KG fully optional with Bench degrading to "an analysis
thread manager," but that empties three of four stages and reduces Bench to
"`/chat` with memory."

## Consequences

- Bench ships with KG and still **degrades gracefully**: when `/kg` is dark, the
  Read/Ideate UI is disabled with a one-click install prompt, while **Analyze keeps
  running pure-backend**. The Ideate→Analyze handoff packet carries a `schema_version`
  handshake to fail safe on KG schema skew.
- **KG access splits by capability**: reads go through the MCP-registered tools
  (`kg_search`, `kg_get_page`, `kg_graph_neighbors`, …); the lifecycle *write* loop
  (`ideate`, `build_packet`, `write_packet`, `record_result`) is reached via the HTTP
  router, not MCP.
- **Two-store boundary**: the graph Memory System owns agent/study *state*
  (`core://`, `project://`, `analysis://`, `insight://`, `preference://`, `session://`);
  OmicsClaw-KG owns cross-study scientific *reading knowledge*
  (Source/Entity/Concept/Method/Hypothesis pages). They are not merged.
