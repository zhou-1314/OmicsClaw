# Surface the full OmicsClaw-KG capability set in the Desktop App

**Status:** proposed (2026-06-03)

The Desktop backend already mounts the **entire** OmicsClaw-KG HTTP API under `/kg`
(`omicsclaw/surfaces/desktop/server.py:200`, `build_kg_router(enable_writes=True)`, gated by
`_register_optional_kg_router` at `:146`/`:540`), and the chat agent can reach ten `kg_*` tools.
Yet the **frontend surfaces almost none of it**: there is no `/api/kg/*` proxy at all — the only
KG-backed Next.js routes are `/api/thread/*` (hypotheses, formalize, confirm-verdict, route-preview)
and `/api/memory`. So today a researcher can only touch the **hypotheses** line of KG (the Ideate
panel), plus the binary KG-up/down gate (`useKGStatus`, ADR 0019). The other ~90% of KG — search,
the page corpus, the graph, bulk ideation, the activity log, experiments — is reachable by the
backend and the agent but has **no UI entry point**.

This ADR records the decision to close that gap and the shape of the work. Ground truth (verified
2026-06-03): KG exposes **38 capabilities** but only **13 HTTP routes** —

- **GET** `/kg/search`, `/kg/pages/{type}`, `/kg/pages/{type}/{slug}`, `/kg/graph/neighbors/{id}`,
  `/kg/graph/communities`, `/kg/log/recent`, `/kg/status`, `/kg/health`
- **POST** `/kg/handoff`, `/kg/ideate/hypotheses`, `/kg/ideate/questions`, `/kg/ideate/syntheses`,
  `/kg/record-result`

`/kg/pages/{type}` is **GET-only** (`omicsclaw_kg/http_api/routes.py:133,147`) — there is no page
authoring over HTTP. Experiments (the multi-step DAG of ADR KG-0001), topics / cross-pollinate,
graph export/rebuild/insights, and `ingest` are **CLI/agent-tool only, no HTTP route**.

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
hypothesis-testing; a standalone surface keeps Bench's Read→Ideate→Analyze→Write stages focused and
avoids each stage growing scattered KG sub-panels. (Rejected — *fold every KG capability into the
Bench stages* (the "workflow-native" option): risks UX bloat and makes KG feel incidental rather
than a first-class asset; the strongest pieces of it survive as D6 shortcuts.)

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

**4. Graph rendering = Cytoscape.js for v1.** Built-in force-directed layouts suit the concept
graph and experiment DAGs; richer custom interaction can migrate to D3 later if needed. (Rejected
for v1 — *D3 from the start*: more custom work than v1 warrants.)

**5. KG is workspace-wide in v1.** `/kg-explorer` searches/browses the whole workspace KG (matching
the agent tools and the `/kg` mount's single resolved home). Thread-scoped filtering (a Read-stage
concern) is deferred to a later phase; hypotheses/verdicts remain thread-local as today.

**6. Light in-Bench shortcuts, not a second home.** Convenience entry points only: a "view network"
popover on Ideate hypothesis cards and Analyze source cards (reusing the graph component), and a
"generate ideas from corpus" modal in Ideate over `/api/kg/ideate/*`. These never become the
primary entry point, and they preserve ADR 0023 §6 — the UI prefills/hands off; the agent loop
still authors any analysis path.

**7. Tier-C is gated on KG-side HTTP work, sequenced after Tier-A.** Surfacing experiments, topics,
cross-pollinate, graph export, HTTP ingest, and **page authoring** each require new KG routes
first. In particular the **Bench Write stage (ADR 0022) is blocked** until KG ships
`PUT /kg/pages/{type}/{slug}`. These are recorded as prerequisites, not desktop tasks; the desktop
UI skeletons can be built ahead and wired on arrival.

## Phased rollout

- **Phase 1 — "KG library is legible" (Tier-A reads, zero KG-side dependency).**
  `/kg-explorer` three-zone shell; `KGSearchPanel`, `KGPageBrowser` (type tabs), `KGPageDetail`
  (+ "cite in thread"), `KGActivityLog`, `KGStatsCard`; the seven read proxies above. Covers the
  bulk of unaided discovery.
- **Phase 2 — "structural insight + bulk ideation."** `KGNeighborhoodExplorer` (Cytoscape, 1–3-hop
  depth slider), `KGCommunitiesPanel`, `KGHealthDashboard`; the D6 "view network" popover and the
  `IdeationWizard` modal over `/api/kg/ideate/*` (these POST routes already ship).
- **Phase 3 — "authoring + experiments + polish" (blocked on Tier-C KG routes).** `WritePanel`
  editor skeleton (wired once `PUT /kg/pages` lands), an experiment-DAG viewer (once
  `/kg/experiment/*` lands), KG settings/maintenance, and cross-links (Read ↔ Explorer, Analyze
  provenance → graph).

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
- Surfaces the **ADR 0022** Write blocker concretely: page authoring needs a KG `PUT /kg/pages`
  route; the desktop side is otherwise ready.
- Net new desktop work is mostly **frontend** (proxies + components); the backend `/kg` mount and
  the curated `/thread/*` endpoints are unchanged. Tier-C requires coordinated KG-repo work.
