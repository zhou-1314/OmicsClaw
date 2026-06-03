# Surface the full OmicsClaw-KG capability set in the Desktop App

**Status:** proposed (2026-06-03)

The Desktop backend already mounts the **entire** OmicsClaw-KG HTTP API under `/kg`
(`omicsclaw/surfaces/desktop/server.py:200`, `build_kg_router(enable_writes=True)`, gated by
`_register_optional_kg_router` at `:146`/`:540`), and the chat agent can reach ten `kg_*` tools.
The **frontend surfaces almost none of it**: there is no `/api/kg/*` proxy at all — the only
KG-backed Next.js routes are `/api/thread/*` (hypotheses, formalize, confirm-verdict, route-preview)
and `/api/memory`. So today a researcher can only touch the **hypotheses** sliver of KG (the Ideate
panel, ADR 0021), plus the binary KG-up/down gate (`useKGStatus`, ADR 0019); the other ~90% —
search, the page corpus, the graph, bulk ideation, the activity log, experiments — is reachable by
the backend and the agent but has **no UI entry point** unless the researcher drops to the CLI. This
ADR closes that gap; the tier table below is the structure.

Ground truth (verified 2026-06-03): KG exposes **38 capabilities** behind **15 HTTP routes** —

- **GET** `/kg/search`, `/kg/pages/{type}`, `/kg/pages/{type}/{slug}`, `/kg/graph/neighbors/{id}`,
  `/kg/graph/communities`, `/kg/log/recent`, `/kg/status`, `/kg/health`
- **POST** `/kg/handoff`, `/kg/ideate/hypotheses`, `/kg/ideate/questions`, `/kg/ideate/syntheses`,
  `/kg/ideate/formalize`, `/kg/record-result`, `/kg/hypothesis/{slug}/confirm-verdict`

(`/kg/ideate/formalize` and `/kg/hypothesis/{slug}/confirm-verdict` are at `routes.py:268,283` —
multi-line decorators; they back the existing Ideate UI today, but via the in-process `/thread/*`
wrappers rather than these `/kg` routes.) `/kg/pages/{type}` is **GET-only**
(`omicsclaw_kg/http_api/routes.py:133,147`) — there is no page authoring over HTTP. Experiments (the
multi-step DAG of ADR KG-0001), topics / cross-pollinate, graph export/rebuild/insights, and
`ingest` are **CLI/agent-tool only, no HTTP route**.

## Capability exposure today (three tiers)

| Tier | Meaning | Capabilities |
|---|---|---|
| **A — backend-ready, frontend only needs a proxy + component** | KG HTTP route ships; no `/api/kg/*` proxy or UI yet | search; list/get pages; graph neighbors; graph communities; bulk ideate questions/hypotheses/syntheses; recent activity log; status; health |
| **B — already surfaced** | Exposed via curated `/thread/*` + Ideate UI | formalize hunch; confirm-verdict; record-result → suggested-verdict loop; route-preview |
| **C — no KG HTTP route; needs KG-side work first** | CLI/agent-only today | experiment DAG (design/new/add-step/submit/status/eval); topics / from-topics / cross-pollinate; graph export/rebuild/insights; `ingest` over HTTP; **page authoring** (`PUT /kg/pages/...`) |

## Decisions

**1. A dedicated `/kg-explorer` page, peer to `/bench` and `/chat` — not folded into the Bench
stages.** KG becomes an always-available "knowledge library" mode: a three-zone workspace —
*left* (global search + page-type tabs) | *center* (page detail ⇄ graph canvas) | *right*
(workspace stats + activity feed). Rationale: the explore/browse task is distinct from
hypothesis-testing; a standalone surface keeps Bench's Read→Ideate→Analyze→Write stages focused.
**Boundary vs. the Read stage (ADR 0019 says "Read *is* the KG"):** the Read stage is the
*thread-scoped* research entry point ("papers in my project"); `/kg-explorer` is the
*workspace-wide* discovery/reference surface ("the whole knowledge base"). Different scope, no
duplication. (Rejected — *fold every KG capability into the Bench stages* (the "workflow-native"
option): scatters KG fragments across Read (search) / Ideate (ideate) / Analyze (graph) / Write
(author), coupling KG concerns to each stage and making them hard to evolve independently; the
strongest pieces survive as the D6 shortcuts.)

**2. Surface Tier-A through thin `/api/kg/*` proxies — no new backend logic.** Because `/kg` is
already fully mounted with writes enabled, each Tier-A capability needs only a `bench-proxy`-style
Next.js route forwarding to the existing `/kg/<route>` (preserving the backend status code), a
`bench-api-client` result-object method, and a component. The new proxy namespace is
`src/app/api/kg/...`:

```
GET  /api/kg/search?q&type&state&limit&offset
GET  /api/kg/pages/[type]?state&limit&offset
GET  /api/kg/pages/[type]/[slug]
GET  /api/kg/graph/neighbors/[node_id]?depth
GET  /api/kg/graph/communities?algorithm&limit
GET  /api/kg/log?limit
GET  /api/kg/status
GET  /api/kg/health
POST /api/kg/ideate/{questions|hypotheses|syntheses}
```

**3. Reuse the established frontend patterns; gate everything on `useKGStatus`.** Artifact-shell
cards (as `HypothesisCard`/`RouterRecommendationCard`), Radix Tabs, the result-object client,
en/zh i18n with compile-enforced parity, and the same graceful-degradation gate as Read/Ideate
(ADR 0019): every KG surface hides or shows a one-click "install/recheck KG" prompt when
`available=false`. No new architectural primitives.

**4. Graph rendering — Cytoscape.js recommended for v1 (pending Open Question 2).** Built-in
force-directed layouts suit the concept graph and experiment DAGs; richer custom interaction can
migrate to D3 later. This is the recommendation the open question weighs, not yet a ratified choice.

**5. KG is workspace-wide in v1.** `/kg-explorer` searches/browses the whole workspace KG (matching
the agent tools — `kg_search`/`kg_graph_neighbors` are not thread-scoped — and the `/kg` mount's
single resolved home). **Relationship to ADR 0018 (thread = project):** this is a deliberate,
permissive divergence (ADR 0020 — the surface is permissive, not jailed): Explorer is a global
reference mode, while thread-scoped lineage (hypotheses/verdicts) stays thread-local as today.
Thread-anchoring the Read stage ("papers my project cited") is a Read-stage refinement deferred to
v2 (see Non-Goals).

**6. Light in-Bench shortcuts, not a second home.** Convenience entry points only: a "view network"
popover on Ideate hypothesis cards and Analyze source cards (reusing the graph component), and a
"generate ideas from corpus" modal in Ideate over `/api/kg/ideate/*`. These never become the
primary entry point, and they preserve ADR 0023 §6 — the UI prefills/hands off; the agent loop
still authors any analysis path.

**7. Tier-C is gated on KG-side HTTP work (sequencing — Open Question 3).** Surfacing experiments,
topics, cross-pollinate, graph export, HTTP ingest, and page authoring each require new KG routes
first. The desktop UI skeletons can be built ahead and wired on arrival; *whether* to push KG for
those routes now or ship all of Tier-A first is left open (OQ 3). Ownership and the Write-stage
dependency are recorded under Consequences, not committed here.

## Non-Goals (v1)

v1 does **not** include: thread-scoped KG filtering in `/kg-explorer` (a Read-stage refinement, v2);
KG content authoring or deletion UI (blocked on a KG `PUT /kg/pages` route); a drag-drop `ingest`
upload UI (routes to the existing `kg_ingest` agent tool, or awaits a KG ingest route — OQ 4); the
experiment-DAG viewer (blocked on KG experiment routes); cross-study hypothesis aggregation (threads
stay isolated per ADR 0018); and KG maintenance UI (rebuild / export / catalog-sync — stay CLI for
now).

## Phased rollout

- **Phase 1 — "KG library is legible" (Tier-A reads; no new KG HTTP routes required).**
  `/kg-explorer` three-zone shell; `KGSearchPanel`, `KGPageBrowser` (type tabs), `KGPageDetail`
  (+ "cite in thread"), `KGActivityLog`, `KGStatsCard`; five read proxies (search, pages
  list/detail, log, status). Covers the bulk of unaided discovery.
- **Phase 2 — "structural insight + bulk ideation."** `KGNeighborhoodExplorer` (graph canvas, 1–3-hop
  depth slider), `KGCommunitiesPanel`, `KGHealthDashboard` (+ the graph/communities/health proxies);
  the D6 "view network" popover and the `IdeationWizard` modal over `/api/kg/ideate/*` (these POST
  routes already ship — no KG-side work).
- **Phase 3 — "authoring + experiments + polish" (blocked on Tier-C KG routes).** `WritePanel`
  editor skeleton (wired once `PUT /kg/pages` lands), an experiment-DAG viewer (once
  `/kg/experiment/*` lands), KG settings/maintenance, and cross-links (Read ↔ Explorer, Analyze
  provenance → graph).

## Acceptance criteria

- **Phase 1:** every `/api/kg/*` read proxy forwards the backend status verbatim (200 on a healthy
  KG, 503 when down); `/kg-explorer` hides/degrades gracefully via `useKGStatus` when KG is dark;
  new en/zh i18n keys pass the compile-enforced parity check; the page browser/search render typical
  workspace volumes without UI lag.
- **Phase 2:** the graph canvas renders 1–3-hop neighborhoods and the community/depth controls work;
  the `IdeationWizard` modal calls `/api/kg/ideate/*` **without** changing the agent loop (ADR 0023 §6).
- **Phase 3:** `WritePanel` binds to `PUT /kg/pages` once KG ships it; the experiment-DAG viewer
  renders once KG exposes experiment routes.

## Open questions (to resolve before this moves to `accepted`)

1. **IA**: dedicated `/kg-explorer` page (D1, recommended) vs. folding KG into the Bench stages.
2. **Graph library**: Cytoscape.js (D4, recommended) vs. D3.
3. **KG-side investment order**: push KG to add the Tier-C HTTP routes now (esp. the experiment DAG,
   KG-0001's flagship, today entirely UI-less) vs. ship all of Tier-A first and revisit.
4. **`ingest` entry**: a drag-drop upload (needs a KG HTTP ingest route) vs. routing through the
   existing `kg_ingest` agent tool for v1.

## Consequences & relationships

- Makes good on **ADR 0019** (KG as a first-class Bench dependency) by giving KG a UI commensurate
  with that status, instead of only the hypotheses sliver.
- Independent of, but complementary to, **ADR 0023** (three-zone Bench frontend): `/kg-explorer` is
  a peer surface, reusing the same shell/gating conventions.
- **KG-side ownership.** Tier-C prerequisites (experiment-DAG routes, topics / cross-pollinate
  routes, `PUT /kg/pages/{type}/{slug}`, an HTTP `ingest` route) are owned by the **OmicsClaw-KG**
  repo and tracked there (no issue filed yet — a follow-up to this ADR). Desktop builds UI skeletons
  ahead but wires nothing until the routes land and announce a schema version.
- Surfaces the **ADR 0022** Write blocker concretely: page authoring needs a KG `PUT /kg/pages`
  route. Write is a *forward* dependency (v2/Phase 3), not v1-critical.
- Net new desktop work is mostly **frontend** (proxies + components); the backend `/kg` mount and
  the curated `/thread/*` endpoints are unchanged. Tier-C requires coordinated KG-repo work.
