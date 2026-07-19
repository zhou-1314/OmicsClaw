# Desktop route-wide authentication closure

## Scope and ownership

This review covers ADR 0071 across the OmicsClaw Backend and the separate
OmicsClaw-App repository. The Backend remains the sole owner of authorization,
Skill governance, execution, persistence, scientific validation, and stable
HTTP contracts. The App owns Electron lifecycle, connection resolution,
TypeScript view models, UI interaction, and thin credential-bearing proxies.

The milestone is a narrow Desktop API and connection-authority closure. It does
not claim completion of the four-stage Skill audit system.

## Independent review rounds

| Round | Ask Codex session | Model | Result |
|---|---|---|---|
| 1 | `019f7674-687b-7f70-a4a6-8175b43d4c8e` | `gpt-5.6-sol/high` | `1 High / 2 Medium / 3 Low — NO SHIP` |
| 2 | `019f768e-cb90-7ee1-af8f-c9e312f1e4f4` | `gpt-5.6-sol/high` | `1 High / 3 Medium / 2 Low — NO SHIP` |
| 3 | `019f76bd-6847-73a2-b162-28f4904785b5` | `gpt-5.6-sol/high` | `0 Blocker / 0 High / 4 Medium / 1 Low — NO SHIP` |
| 4 | `019f76e0-b4bb-7dd1-bc11-b1d8a4615195` | `gpt-5.6-sol/high` | `0 Blocker / 0 High / 2 Medium / 1 Low — NO SHIP` |
| 5 | `019f7703-a891-70b2-bdd8-188e967da743` | `gpt-5.6-sol/high` | `0 Blocker / 1 High / 2 Medium / 1 Low — NO SHIP` |
| ADR 0072 pre-final | `019f776b-4377-7511-9a9e-2106c7ef23df` | `gpt-5.6-sol/high` | `0 Blocker / 1 High / 1 Medium / 0 Low — NO SHIP` |
| ADR 0072 final attempt A | `019f79ad-a155-7503-b126-41cbadb218b2` | `gpt-5.6-sol/high` | No verdict — account usage limit before source inspection |
| ADR 0072 final attempt B | `019f79af-6eae-7f70-8aba-dba6b16976c6` | `gpt-5.6-sol/high` | No verdict — alternate local login had the same usage limit |
| ADR 0072 final follow-up | Pending | `gpt-5.6-sol/high` | Pending |

Every round was read-only. Findings were fixed locally with regressions before
the next review; no earlier `NO SHIP` result is represented as approval.

## Round 3 closure matrix

| Finding | Production closure | Negative proof |
|---|---|---|
| Non-empty malformed direct targets could still look usable | One root-origin-only `normalizeBackendBaseUrl` is used by local, Stage-0, saved-profile, settings, activation, projection, and probe paths before credential lookup | Rejects userinfo, paths, queries, fragments, malformed ports, backslashes, parser-normalized dot paths, and invalid persisted rows; invalid targets are unavailable and inactive |
| Alias-only SSH profile could probe a stale direct URL with bearer authority | All target selection uses `ssh_alias > ssh_host > direct_url`; saved test and runtime ping do not resolve a direct target for SSH | Alias-only profile with stale URL performs no direct fetch and does not resolve its credential before reporting the missing tunnel |
| Mistyped `auth_required` could pass health validation | Any present property excludes both health shapes: exact `true` is authentication-required and every other value/type is invalid | Legacy-v1 and current-full matrices cover `false`, strings, numbers, `null`, arrays, and objects across health, setup, tracking, and profile probes |
| `/api/health` could attribute A's delayed result to newly selected B | Runtime ids are captured synchronously after dispatch and before the first await, then reused for every success and failure branch | Delayed success and network failure across A to B write only A; local and Stage-0 identities are separately pinned |
| Delegated path proof did not require strict raw/decoded equivalence | Backend requires ASCII raw bytes, valid one-layer percent syntax, strict UTF-8, full `raw_path == path` equivalence, and bounded residual-ambiguity rejection | Missing, malformed, non-ASCII, invalid UTF-8, mismatched, encoded separator/dot/backslash, and multiply encoded forms cannot select delegated authority or read a body |

## Additional pre-final finding

A separate source-level audit after Round 3 reproduced a `root_path` Medium:
the middleware classified the complete ASGI path while Starlette routed the
route-relative path. With `root_path=/skill-evolution`, a dedicated credential
could reach an ordinary route; with `root_path=/api`, the real delegated route
was misclassified.

The gate now reuses Starlette `get_route_path(scope)` for public and delegated
selection while proving the complete raw/decoded path pair. An unverified
route-relative delegated request cannot downgrade to ordinary access: after
ordinary-realm authentication it still returns `400` before routing or a body
read. Regressions cover both prefix directions, public `/api/health`, invalid
raw evidence with a correct ordinary token, and body non-consumption.

## Round 4 closure matrix

| Finding | Production closure | Negative proof |
|---|---|---|
| A remote target equal to a local default could mark two runtime descriptors active | Active ownership now follows connection mode, the authoritative selected profile, and explicit launcher override; URL equality is only a reachability check | Selected-profile and Stage-0 loopback targets produce exactly one owner; stale launcher state cannot create a second owner; success and failure remain bound across A-to-B changes |
| Setup and provider doctor could mutate usage/setup state before strict health validation or mix A and B across awaits | A validation context freezes URL, bearer, and profile for the complete multi-request flow; setup-derived state, response exposure, default project, and health-derived usage commit only after strict health succeeds | Preliminary `/workspace` or `/providers` 2xx followed by invalid health performs no health-derived write; A-to-B changes retain A's URL and bearer; workspace failure cannot downgrade the stored project |
| Invalid SSH aliases were rejected only after `env:` credential materialization | SSH target resolution now precedes credential lookup and the resolved target is passed through to the probe | Unknown aliases do not materialize credentials or invoke a probe; the valid-alias path resolves once and probes the exact resolved host |

## Round 5 closure matrix

| Finding | Production closure | Negative proof |
|---|---|---|
| **High:** the global active tunnel port was not bound to the SSH profile and process generation that owned it | One atomic V1 binding pins selected profile id, local port, Next/Backend process epoch, source and resolved-target fingerprints, and profile revision. Electron-main-only CAS publication/revocation is required; Backend config rejects stale or unbound state before credential materialization | A-to-B switching, App/Next restart, same-id target or auth-reference edits, stale source revision, and non-selected publication cannot reuse the old port or bearer |
| **Medium:** Provider Doctor could use or expose preliminary `/providers` data after strict `/health` failed | One validation context carries the same URL, bearer, and profile; provider data remains provisional until strict same-context health succeeds. Provider arrays are shape-normalized and discarded-body cancellation is bounded and rejection-safe | Provider 2xx followed by redacted, malformed, invalid, or never-settling health/body cleanup produces no provider exposure or health-derived state; an A-to-B switch remains entirely on A |
| **Medium:** an inactive selected SSH profile could resolve an `env:` credential before target/binding validity was proved | The exact selected target and profile-bound tunnel binding are proved first; only then is the environment reference materialized, and the same resolved target is carried through the probe | Inactive, stale, mismatched, missing, and unknown-alias profiles do not read the credential environment or invoke a probe |
| **Low:** managed readiness and periodic polling used wider or incomplete health acceptance | Startup and periodic monitoring share one bounded current-full parser plus exact `launch_id`, with every result fenced to the captured child generation | Minimal status payloads, `auth_required`, legacy-v1, stale launch ids, malformed/oversized/truncated/hung responses fail closed; a stale poll cannot stop a replacement or an already stopped launch |

## Post-Round-5 local hardening

These are additional local closures, not an independent approval and not a
claim that Round 5 shipped:

- An operation-local `BackendRequestContext` freezes mode, root URL, private
  bearer, profile id, and execution target across Jobs-to-SSE, Chat
  Job-to-stream-to-cleanup, AutoAgent start-to-abort, Notebook diagnostics,
  Workspace sessions, and Scoped Memory Workspace-to-query/prune.
- Scoped Memory now uses one method-scoped fixed-route allowlist whose result
  supplies both the canonical Backend path and Workspace-authority mode.
  Embedded slash/backslash, residual or multiply encoded percent ambiguity,
  wrong methods, unknown routes, and inherited object-property names return
  `400` before body consumption or Backend dispatch; all existing fixed Memory
  routes retain one canonical outbound path.
- Dynamic Job ids are closed to one bounded route segment before encoding, and
  each status/events/cancel/retry helper captures its dispatch context and
  propagates downstream abort where applicable. Remote Chat reuses one context
  and admitted Job id, cancels only that Job on pre-stream failure, abort, or
  supersession, and cannot let a late predecessor overwrite its replacement.
- Ignored responses and upstream readers share a synchronous-trigger,
  rejection-safe, deadline-bounded cancellation helper. Never-settling custom
  cancellation cannot hold the primary Chat, Notebook, Provider Doctor, or Job
  SSE response open.
- Managed Python keeps the exact child owner until child and process-tree exit
  are both proved; POSIX detached groups and Windows `taskkill /T` plus exact
  child exit have separate evidence. Stop/replace failure retains the owner and
  blocks replacement, while start/stop supersession immediately aborts startup
  and periodic health sockets.
- `TunnelManager` fences every open generation, aborts its predecessor, closes
  stale local/SSH streams, and prevents delayed ready/error/close, bootstrap,
  reconnect, or forward callbacks from publishing or poisoning a replacement.
  Its final targeted gate passes **43/43**.

Those results were the Round-5 local checkpoint. The later ADR 0072 pre-final
review remained `NO SHIP`, so they are not current final-gate evidence.

## ADR 0072 local-candidate closure matrix

These rows describe code implemented after the pre-final review. They are not
an independent approval and do not replace the pending final gates.

| Boundary | Local candidate closure | Remaining release condition |
|---|---|---|
| Stale tunnel after loss or local-port reuse | Loss destroys active sockets/channels; a generation-bound retired rejecting listener retains the port until exact SSH child exit, and active plus retired capacity is bounded | Final race gates and independent review |
| Cross-request resource retargeting | Job, Chat, permission and AutoAgent rows carry immutable non-secret target/profile/fingerprint/process/binding evidence; Job/Chat remain process-epoch-bound | Final operation-binding gates and independent review |
| Persistent AutoAgent authority | Backend Migration 12 creates an immutable 64-lowercase-hex `control_authority_id`; AutoAgent may re-attest a changed process epoch only for the exact same Control database, while legacy null bindings fail content-free as `legacy_control_authority_unavailable` | Final migration/restart/restore gates and independent review |
| Unknown creation outcome | Novel-only reservation atomically installs the immutable binding plus `reserved + pending` guard; a 10-second response-header deadline precedes the 12-second guard, and duplicate, Stop, unknown, claim or App death cannot erase cancellation intent | Final timing/fault gates and independent review |
| Cancellation and terminal evidence | Receipt cancellation uses only bounded `health -> abort-receipt`; separate non-pending status recovery may use `/reconcile`. Only exact matching SSE/status terminal wires retire authority; `/results`, EOF, malformed/oversized input and HTTP/transport failure do not | Final negative-path gates and independent review |
| Governed worker and bounded transports | Linux uses user-systemd scope plus bubblewrap with no thread/process fallback. Backend validates identity, proves tree absence and persists stop evidence before terminal commit. Request, IPC, SSE and App readers have explicit byte/time/event bounds and demand-driven forwarding | Final owner-stop/transport gates, packaged-platform smoke where applicable, and independent review |
| Terminal-commit availability | A stopped owner with a transient database commit fault remains in the same worker as one bounded intent, emits one nonterminal notice, quarantines novel admission and retries with capped exponential backoff. Status, cancellation and shutdown may wake but never duplicate the loop; shutdown may durably reconcile interruption | Transient-success, persistent-fault, admission-quarantine and shutdown-reconciliation regressions plus independent review |
| Receipt-confirmed observer loss | AutoAgent SSE disconnect, iterator cancellation, EOF and observer bounds detach only observation; only explicit session- or receipt-bound abort Interfaces may persist cancellation or signal the governed owner | Iterator-close/cancellation races, later status/results recovery, and explicit-abort control regressions plus independent review |

## Consolidated final checklist

The final reviewer must inspect source and re-evaluate all of these properties:

1. one lifespan-frozen authority gates every Desktop HTTP route and future
   WebSockets before routing and body parsing;
2. only verified route-relative `GET`/`HEAD /health` is public, and its redacted
   payload cannot count as healthy in the App;
3. `root_path`, raw bytes, decoded paths, encoded ambiguity, lookalikes, and
   unknown descendants cannot cross authority realms;
4. `/skill-evolution` uses only its independently captured dedicated authority,
   with no ordinary-token fallback;
5. Electron-managed ordinary children inherit none of the remote, evolution,
   or fd control authorities;
6. generic and Notebook proxies accept only relative Backend paths, discard
   caller authorization, and use server-selected credentials;
7. a selected profile is authoritative and never falls through to Stage-0;
8. one semantic oracle governs every connection-health consumer;
9. health-gated setup, `last_used_at`, connection results, and heartbeats are
   committed only after semantic success and to dispatch-bound identities,
   while generic successful non-health Backend use remains ordinary usage;
10. one strict target parser and one SSH/direct routing classifier are used at
    every creation, mutation, resolution, projection, activation, and probe
    seam;
11. the tunnel binding is profile-, process-, source-, target-, and
    revision-bound, and publication/revocation cannot cross generations;
12. one request context owns every Backend await and cleanup in a logical
    multi-request operation;
13. Memory fixed-route classification and the actual outbound path are the same
    canonical object, with Workspace injection selected only from that object;
14. dynamic Job ids remain one closed route segment and abort/status/events/
    cleanup stay on the captured owner;
15. discarded response and reader cancellation is synchronous-trigger,
    bounded, and rejection-safe;
16. managed health requires current-full plus exact launch id, and exact child
    ownership persists through process-tree exit proof;
17. Tunnel open, bootstrap, reconnect, close, and forwarding callbacks are
    fenced to one generation; and
18. Backend/App ownership and user-facing remote probe documentation match the
    implementation without moving policy into the App;
19. full authenticated health and AutoAgent capabilities expose the immutable
    `control_authority_id`, public minimal health does not, and process-epoch
    re-attestation requires an exact same-Control match;
20. legacy AutoAgent bindings without Control authority remain content-free
    quarantined and cannot status, abort, reconcile, or retarget;
21. novel reservation and the 12-second creation guard are atomic, duplicate
    reservation cannot mutate an existing binding, and the 10-second start
    deadline stops at response headers;
22. cancellation delivery never invokes `/start` or `/reconcile`, while the
    separately authorized non-pending status-recovery path may reconcile;
23. only exact session-bound closed SSE/status evidence terminalizes App state,
    Backend persists owner-stop evidence before terminal commit, and loss or
    cancellation of a receipt-confirmed SSE observer never cancels execution;
    and
24. request, worker IPC, SSE and App proxy/observer limits hold at boundaries,
    including Unicode finite JSON, oversized-progress resynchronization and no
    credential persistence outside nonce-bound worker IPC.

## Historical verification through Round 4

- Backend route-wide, Skill Evolution, remote-auth, Desktop server, OAuth, and
  documentation contract selection: **342 passed, 1 skipped**.
- The route-wide file alone: **55 passed**; the independent auth selection:
  **215 passed**.
- Backend targeted Ruff, Ruff format, Python compilation, and repository
  `git diff --check`: passed.
- OmicsClaw-App complete unit suite: **1581 passed, 0 failed** across 6 suites.
- App TypeScript typecheck, scoped ESLint, and repository `git diff --check`:
  passed.
- Next 16 webpack production compilation, TypeScript, all **97** pages, and
  build traces passed with deterministic local font responses; Electron
  compilation passed. The ordinary live-font build remains dependent on
  reaching Google Fonts and is not claimed as an offline release build.

These numbers are retained as a historical Round-4 checkpoint. In particular,
**1581** is not the final post-Round-5 App total.

## Post-Round-5 local verification

- Operation-local Backend authority/path/Memory/response work checkpoint:
  **80/80** targeted tests.
- Provider Doctor strict-health and malformed/cancellation boundary:
  **50/50** targeted tests.
- Memory fixed-route authority plus `backendFetch` and Memory regressions:
  **82/82** targeted tests.
- Shared Chat/Job/Notebook/Provider response cancellation work:
  **92/92** targeted tests.
- Managed Python exact-owner, process-tree, and health-abort work:
  **62/62** across three targeted files; TypeScript, scoped ESLint,
  `git diff --check`, and Electron bundling passed at that checkpoint.
- Tunnel generation closure: **43/43** targeted tests.
- Consolidated App unit suite: **1690/1690**; typecheck, scoped ESLint, and
  Electron build passed.
- Deterministic-font Next webpack compile, typecheck, and static generation:
  **98/98** routes passed.
- Backend documentation/route-wide selection: **67/67**; both-repository
  `git diff --check` and audit gates passed.
- The mandatory independent post-Round-5 review remains pending. These complete
  local gates do not convert the Round-5 `NO SHIP` verdict into approval.

## Current ADR 0072 verification status

The pre-final ADR 0072 review remains the latest independent verdict and is
still `0 Blocker / 1 High / 1 Medium / 0 Low — NO SHIP`. Its findings have been
repaired locally. Post-repair Backend verification passes 657 AutoAgent tests,
450 Desktop tests plus one optional skip, 327 Control tests, and 219 selected
documentation/contract tests; changed-file Ruff, format, compilation and diff
checks pass. Post-repair App verification passes 1834/1834 unit tests,
TypeScript, scoped ESLint, Electron compilation, diff check, and the
deterministic-font Next webpack production build with 99/99 static pages.
These are local-candidate gates only. A fresh mandatory independent review is
still required before this document or either README may record `SHIP`. Two
read-only `gpt-5.6-sol/high` attempts reached Codex but exhausted the account
usage allowance before any source inspection or report; they are audit-trail
entries, not review rounds or substitute verdicts.

## Residual boundaries

- The bearer gate does not provide TLS, user identity, per-route RBAC, rate
  limiting, OS isolation, or same-UID process isolation.
- When ASGI socket metadata is absent, external-bind rejection still relies on
  the canonical launcher.
- The App profile schema still has one bearer field; distinct ordinary and
  Skill Evolution remote credentials need a future contract revision.
- Legacy-v1 health is a deliberately retained, weaker compatibility shape.
- Native packaged macOS and Windows fd handoff still need platform smoke tests.
- Root-origin-only Backend URLs intentionally drop compatibility with reverse
  proxies mounted below a URL base path.
- The Round-5 process/operation-local limitation is historical. ADR 0072 now
  implements a local-candidate durable binding for independently addressed Job,
  Chat, permission and AutoAgent requests, but the independent final review is
  still required before relying on it as a release guarantee.
- Governed AutoAgent execution is Linux-only. The systemd scope proves owner
  absence rather than calibrated CPU/memory quota, and bubblewrap plus
  `SO_PEERCRED` does not isolate a malicious same-UID process.
- Native packaged macOS/Windows lifecycle and evolved-config safety still need
  platform smoke where those features are enabled.
- This Desktop/AutoAgent slice does not complete Skill representation,
  acquisition, retrieval, validation, promotion/demotion or whole-catalog
  scientific calibration.
- The ADR 0072 local gates pass; the mandatory independent follow-up remains
  open because both final-review attempts exhausted account usage before
  source inspection.

## Final verdict

Round 5 and the ADR 0072 pre-final review both remain `NO SHIP`. The current
two-repository code is an ADR 0072 local candidate that addresses the recorded
stale-tunnel and cross-request authority findings and adds persistent Control
identity, atomic unknown-start protection, strict terminal evidence, bounded
transports, and a governed Linux owner. Local gates pass, but a fresh mandatory
Ask Codex `gpt-5.6-sol/high` review remains blocked by account usage until a
compliant review can run. This document must not record `SHIP` until that
independent review reports zero Blocker, High, and Medium findings.
