# Canonical desktop write surface + single idea→analysis path

**Status:** accepted (2026-06-28). Resolves audit items C-1 (KG write-route double
contract) and D-2 (two competing idea→analysis surfaces) from
`docs/UNIFIED_PLATFORM_AUDIT_TODO.md`. Builds on批7 (thread↔source scoping), which
made the backend `/thread/*` formalize route thread-grounded.

## Context

Two contract-drift problems surfaced in the tri-repo audit:

**C-1 — duplicated write surfaces.** The same write capabilities exist as *two*
HTTP surfaces: the KG-native router (mounted at `/kg/*` by the desktop server, and
also served standalone by `oc kg http`) exposes `POST /ideate/formalize` and
`POST /hypothesis/{slug}/confirm-verdict`; the backend re-implements the same
capabilities at `POST /thread/{id}/formalize` and
`POST /thread/{id}/hypothesis/{slug}/confirm-verdict`. The desktop App only ever
calls the backend `/thread/*` routes (`bench-api-client.ts`); the KG-native write
routes are unreachable from the App. The two request models had already diverged
(`FormalizeRequest{hunch, thread_source_slugs, stub}` vs
`ThreadFormalizeRequest{hunch}`), and批7 widened the difference: `/thread/formalize`
now resolves the thread's bound Source slugs server-side and computes
`cross_study`, while the KG-native route trusts a caller-supplied slug list and
returns the raw KG dict.

**D-2 — two idea→analysis surfaces.** The App has two places an idea becomes an
analysis: the Bench `IdeatePanel` (hands a hypothesis to the chat/Analyze agent via
the message composer — wired, working) and the KG Explorer (`KGExperimentDAG` +
`KGIdeationWizard`, an independent ideation→experiment flow). They share KG data but
have no navigation or handoff between them, so an idea discovered in KG Explorer
cannot enter the same analysis path the rest of the platform uses.

Note: `/handoff` and `/record-result` are NOT pure duplicates — the App's KG
Explorer consumes them via dedicated `/api/kg/*` proxies (audit C-2), so they stay
first-class.

## Decision

1. **The backend `/thread/*` routes are the canonical desktop write surface.** The
   App writes hypotheses/verdicts exclusively through `/thread/{id}/formalize` and
   `/thread/{id}/hypothesis/{slug}/confirm-verdict`. These resolve thread-scoped
   grounding (批7) and return the frontend hypothesis shape.

2. **The KG-native `/kg/ideate/formalize` and `/kg/hypothesis/{slug}/confirm-verdict`
   are headless/standalone-only.** They remain registered (the standalone `oc kg
   http` server and KG CLI callers rely on them) but are documented as not part of
   the desktop contract — the App must not call them, and their request/response
   shape is kept in sync with the canonical backend routes to prevent drift. We do
   NOT unregister them (that would break headless KG users); we demote them by
   documentation + route docstrings.

3. **Bench / the chat-agent is the single canonical idea→analysis path.** KG
   Explorer remains for graph exploration + experiment bookkeeping, but its
   idea→analysis affordances cross-link into Bench rather than forking a second
   analysis dispatch. New idea→analysis dispatch logic is added to Bench, not the
   Explorer.

## Consequences

- Lowest-disruption convergence: no working surface is removed, the App is
  unchanged, and headless KG keeps its HTTP write API.
- The dual contract is now *documented and directional* (canonical vs headless)
  rather than silently drifting. Future write capabilities are added to `/thread/*`
  first; the KG-native mirror is updated only to stay shape-compatible.
- KG Explorer is not deleted; an explicit cross-link + this ADR mark its isolated
  experiment-run path as the non-canonical one, to be folded into Bench over time.
- Risk: the two formalize implementations can still drift. Mitigation: the
  headless route docstring points back here, and the contract note lives in both
  `omicsclaw_kg/http_api/routes.py` and `omicsclaw/surfaces/desktop/server.py`.
