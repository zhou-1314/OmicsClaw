# Authenticate the Desktop API before routing

## Status

Accepted.

Implementation: Round 5 Ask Codex `gpt-5.6-sol/high` session
`019f7703-a891-70b2-bdd8-188e967da743` returned
`0 Blocker / 1 High / 2 Medium / 1 Low — NO SHIP`. Its four findings have local
TDD closures across the App/Backend contract. Additional operation-local
authority, fixed-route, response-lifecycle, managed-child, and tunnel-generation
hardening and all final local gates are complete: Tunnel **43/43**, App full unit
**1690/1690**, typecheck, scoped ESLint, Electron build, deterministic-font Next
webpack compile/typecheck/static generation for **98/98** routes, Backend
docs/route-wide selection **67/67**, and both-repository diff/audit gates passed.
No post-Round-5 approval has run.

Refines the remote-access boundary described by
[ADR 0044](0044-single-owner-control-plane-and-owner-only-channel-ingress.md)
and the Skill Evolution credential boundary in
[ADR 0066](0066-govern-earned-skill-validation-promotion-in-the-backend.md).

The operation-local limitation recorded here is refined by
[ADR 0072](0072-bind-desktop-operations-to-a-backend-epoch-and-govern-autoagent.md),
which adds durable cross-request bindings and receipt-bound AutoAgent recovery.

## Context

The Desktop Backend historically attached `require_bearer_token` only to the
remote execution routers. Most Desktop routes remained anonymous even when
`OMICSCLAW_REMOTE_AUTH_TOKEN` was configured, including Notebook execution,
provider probes, MCP configuration, Skill installation, AutoAgent mutation,
Memory, outputs, files, and bridge control. Route dependencies also run after
ASGI request parsing, so a rejected multipart request could consume or spool a
large body before authentication.

The App had a second authority ambiguity. Its generic Backend helper accepted
absolute URLs and let a caller-provided `Authorization` header replace the
configured connection-profile credential. The Notebook proxy independently
implemented the same behavior. These were not current Renderer exploits, but
they made future proxy changes capable of forwarding Backend authority to a
different origin or treating Renderer input as server authority.

## Decision

### One process-lifetime gate covers the Desktop API

The Backend captures `OMICSCLAW_REMOTE_AUTH_TOKEN` once at FastAPI lifespan
startup and stores an immutable, secret-redacted authority object on app state.
Runtime environment mutation cannot enable, disable, or rotate the authority;
rotation requires a Backend restart. A request that reaches middleware without
that captured app-state slot is an initialization failure and returns `503`;
it never falls back to reading a mutable process environment per request.

A pure ASGI middleware authenticates every HTTP request before routing,
dependency resolution, multipart parsing, or body reads. Unknown paths and
generated OpenAPI/documentation paths are covered by the same boundary. Future
WebSocket routes inherit the gate. Under Uvicorn, an authentication failure
before `websocket.accept()` is exposed to the client as an HTTP `403` handshake
rejection; it is not an accepted WebSocket closed with code `1008`. Duplicate,
missing, malformed, or incorrect bearer credentials fail closed.

When the token is unset, loopback behavior remains compatible with local-first
development. The canonical launcher still refuses wildcard or external binds
without a token. As defense in depth, the ASGI gate also returns 503 when a
direct server launch explicitly reports a wildcard or non-loopback socket and
no token was captured. This request-time check does not guess when an ASGI
server omits socket metadata; the canonical launcher remains authoritative in
that case.

### Public and delegated paths are explicit and narrow

`GET`/`HEAD /health` is the only public HTTP exception. With no configured
remote authority it preserves the full local health payload. With an authority
configured, an unauthenticated request receives only status, version, Desktop
launch id, and `auth_required: true`; a wrong presented credential receives
401; only the correct frozen credential receives provider, model, paths,
dependency state, and protocol contracts. App health and connection probes
must treat `auth_required` as an authentication failure rather than usable
Backend health. All App connection-health consumers use one strict semantic
oracle: HTTP 2xx alone is not healthy. The oracle accepts only the current full
health contract or the deliberately retained legacy-v1 `status + version`
shape; non-JSON, arrays, arbitrary status values, partial current payloads, and
`auth_required` liveness fail closed. Any own `auth_required` member excludes a
payload from both accepted shapes: exact `true` is classified as
authentication-required and every other value or type is invalid. Health-gated
workflows may commit health-derived setup, connection-result, heartbeat, or
`last_used_at` success state only after that validation. Ordinary successful
Backend calls intentionally remain eligible for generic usage tracking; such a
2xx is not treated as a health assertion. `/api/health` freezes exactly one
active dispatch owner before its first await and uses that same owner for
success and failure. If runtime state cannot establish one unique owner, it
writes no heartbeat rather than guessing.

`/skill-evolution` and its slash-delimited descendants are delegated to the
independently frozen Skill Evolution authority from ADR 0066. The pre-routing
gate selects that dedicated authority and authenticates the request before
routing, dependency resolution, or body parsing; it does not first require the
separate remote authority. A lookalike such as `/skill-evolutionary` is not
delegated. Public and delegated classification uses the same route-relative
path that Starlette derives after applying ASGI `root_path`, preventing a
deployment prefix from moving a dedicated credential onto an ordinary route.
Both exceptions require `raw_path` to be present ASCII bytes whose valid
percent escapes decode as strict UTF-8 to exactly the complete ASGI `path`.
Residual encoded or multiply encoded dot segments, separators, backslashes,
malformed escapes, non-ASCII raw bytes, missing evidence, and path mismatches
are unverified. An unverified route-relative delegated request first retains
ordinary-realm authentication semantics and then returns `400` even with a
correct ordinary credential; neither credential can reach routing or body
parsing. An unverified public candidate receives no public exception. True
browser CORS preflight remains handled by the outer CORS middleware, while a
bare `OPTIONS` request remains authenticated.

### The App is a credential-bearing adapter, not a second policy owner

OmicsClaw Backend continues to own authorization, execution, mutation, and
persistence. OmicsClaw-App owns only connection resolution, Electron child
lifecycle, and thin HTTP adaptation.

The generic App `backendFetch` accepts only Backend-relative paths, discards
caller-provided `Authorization`, and attaches only the active connection
profile authority. The Notebook proxy applies the same relative-path and
header rules. One explicit helper may carry the server-owned local Skill
Evolution launch credential, and that helper rejects every path outside the
exact `/skill-evolution` namespace. Remote Skill Evolution continues to use the
connection-profile credential as the value sent on the wire, but the Backend
authorizes it only when an operator explicitly configured a matching dedicated
`OMICSCLAW_SKILL_EVOLUTION_TOKEN`. The ordinary remote token is never an
implicit Skill Evolution authority; missing dedicated configuration returns
`503`. Because the current App profile has one bearer field, distinct remote
and Skill Evolution values require a future profile-contract milestone.

A non-empty selected connection-profile id is authoritative for target and
credential resolution. If its store is unavailable, its row is missing, its
direct URL is unusable, its referenced environment credential is unresolved,
or its SSH tunnel is inactive, requests and runtime projections fail closed.
They never fall through to legacy Stage-0 URL/token settings or localhost.
Stage-0 compatibility remains only when no profile is selected.

Local, Stage-0, persisted direct-profile, activation, projection, and probe
paths share one URL normalizer. It accepts only an absolute HTTP(S) root origin
with a host and no credentials, base path, query, fragment, backslash, or
encoded path ambiguity; validation precedes credential lookup and dispatch.
Profile kind is classified once as `ssh_alias > ssh_host > direct_url`, so an
alias-only saved profile never sends authority to a stale direct URL.

Active runtime ownership is selected from connection mode, the authoritative
selected profile, and an explicit launcher override. URL equality is only a
reachability fact: a remote target that happens to equal the local or Stage-0
URL cannot make two runtime descriptors active. Multi-request validation flows
such as setup and provider doctor freeze one target URL, bearer credential, and
profile identity for the entire sequence; health validation precedes all
health-derived state commits and response exposure, so an A-to-B settings
change between awaits cannot mix authorities or mutate B from A's result.

SSH profiles resolve and validate their target before materializing an
`env:VAR` credential, and the same resolved target is carried into the probe.
An invalid or unknown alias therefore cannot read a credential environment
variable or observe a different SSH configuration on a second lookup.

### One logical App operation keeps one Backend authority snapshot

`BackendRequestContext` captures connection mode, root Backend URL, private
bearer authority, profile id, and execution target before an operation's first
await. Jobs-to-SSE, remote chat Job-to-stream-to-cleanup, AutoAgent
start-to-abort, Notebook diagnostics, Workspace session operations, and Scoped
Memory Workspace-to-query/prune reuse that same snapshot. An A-to-B settings
change may affect a later operation, but cannot splice B's target or bearer into
an in-flight A operation.

Before dispatch, the generic Backend path guard iteratively decodes and rejects
raw or encoded dot segments, schemes, network paths, backslashes, fragments,
malformed escapes, and multiply encoded ambiguity. Dynamic Job ids have a
separate closed single-segment contract before encoding; each observation,
events, cancel, or retry helper captures its own dispatch context. Remote Chat's
multi-request flow reuses its one admitted Job id and one context through
stream binding and pre-stream cleanup.
Scoped Memory is narrower still: one method-scoped fixed-route classifier
returns both the canonical `/memory/...` path and its Workspace-authority mode.
Every already-decoded segment must be a canonical token and an own allowlist
member; unknown routes, wrong methods, embedded separators, residual percent
escapes, and prototype-property names fail before a body read or Backend fetch.
Only canonical `GET /memory/scoped` and `POST /memory/scoped/prune` inject the
server-resolved Workspace, overwriting caller query/body values and failing
closed when Workspace authority is unavailable.

Every intentionally discarded response or upstream reader is released through
one synchronous-trigger, rejection-safe, deadline-bounded cancellation helper.
Cleanup never extends the primary response path even when a custom stream's
`cancel()` promise rejects or never settles. This snapshot and cleanup contract
is operation-local; it is not a durable owner or lease across independent HTTP
requests.

Electron's auto-managed Python Backend is always loopback-bound, so its child
environment removes the inherited remote bearer as well as the Skill Evolution
environment names; the Skill Evolution credential still arrives once over fd
3. A separately operated remote Backend is not an App child and owns its own
initial `OMICSCLAW_REMOTE_AUTH_TOKEN`; the App reaches it through a saved
profile. Stored `env:VAR` token references are resolved before connection tests
and runtime pings, and a missing variable fails without sending a literal
reference.

### Managed process and tunnel generations fail closed

Managed Python startup readiness and periodic polling use the same bounded
current-full health parser and require the exact launch id. Legacy-v1, minimal
`status + launch_id`, `auth_required`, malformed, oversized, truncated, hung,
and stale-generation responses cannot make a managed child healthy. Starting,
stopping, or superseding a generation aborts its in-flight health socket
immediately.

The local lifecycle implementation retains the exact child as owner until both
that child and its process tree have bounded exit proof. POSIX launches use a
fresh detached process group and require the child to disappear plus an empty
group; Windows requires successful `taskkill /T` (and `/F` when forced) plus
exact child exit. A timeout or force failure leaves the owner recorded in an
error state and prevents replacement. Synchronous handoff, authority-fd, pipe,
and asynchronous spawn failures enter the same coalesced TERM-to-KILL path;
pid-less `ENOENT` is the only no-OS-owner shortcut.

SSH tunnel publication is likewise generation-bound. The atomically published
binding carries the selected profile, local port, process epoch, source and
resolved-target fingerprints, and profile revision; revocation must succeed
before Electron mutates or closes the tunnel. `TunnelManager` also fences each
open generation and aborts its predecessor so delayed ready/error/close,
forward callbacks, reconnects, or bootstrap polling cannot publish or poison a
replacement. The managed-child and tunnel-generation implementation and local
gates are complete; independent post-Round-5 review remains pending.

## Consequences

- Configuring the remote token now protects the complete Desktop HTTP surface,
  not only `/jobs`, `/datasets`, `/sessions`, `/artifacts`, and `/workspace`.
- Authentication rejects hostile large bodies before FastAPI can parse or
  spool them.
- Public liveness no longer discloses provider configuration or filesystem
  paths and cannot produce a green App status when authority is missing.
- Generic App proxies cannot send Backend credentials cross-origin or accept
  Renderer credentials as Backend authority.
- App request contexts and generation fences close one logical operation or
  one managed child/tunnel generation. They do not persist a durable owner or
  lease across independent HTTP requests; cross-request status, cancel, and
  interrupt ownership remains a separate next milestone.
- Existing per-router dependencies remain defense in depth and preserve their
  standalone-router tests; they are no longer the complete perimeter.
- The bearer protects application authority but does not provide TLS, user
  identity, per-route roles, rate limits, OS isolation, or same-UID process
  isolation. External deployments still require a trusted transport such as an
  SSH tunnel or TLS reverse proxy and appropriate host access controls.

## Alternatives considered

- **Add dependencies to every existing route.** Rejected because newly added
  routes could silently omit them and body parsing would still precede the
  dependency.
- **Protect `/health` exactly like every other route.** Rejected because the
  Electron process supervisor needs credential-free loopback liveness. A
  minimal authenticated-detail split retains that contract without exposing
  operational details.
- **Let caller `Authorization` override the profile.** Rejected because the
  App server, not the Renderer, owns the Backend credential boundary. The one
  independent Skill Evolution authority is represented by a narrow explicit
  interface instead.
- **Move authorization or Skill governance into OmicsClaw-App.** Rejected
  because it would duplicate Backend policy and violate the cross-repository
  ownership boundary.
