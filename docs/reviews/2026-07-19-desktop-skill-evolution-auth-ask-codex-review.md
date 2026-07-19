# Desktop Skill Evolution authority and child-credential closure

## Scope

This review covers the narrow cross-repository authority boundary used by the
Desktop Skill Evolution review flow. The OmicsClaw Backend owns the credential,
governance policy, execution, persistence, manifest writes, and stable HTTP
contract. OmicsClaw-App owns the Electron/Next.js transport, runtime-validated
view models, and user interaction.

The milestone closes these specific properties:

- the packaged App generates one process-lifetime Skill Evolution token and
  hands it to Python over fd 3 rather than its initial environment;
- the Backend consumes the fd before Runtime startup, rejects malformed or
  incomplete higher-priority input without fallback, and applies a bounded
  read deadline;
- all seven Backend `/skill-evolution/*` routes require the frozen authority;
- the App proxy discards Renderer-supplied `Authorization` before attaching
  server-held local or configured remote authority;
- ordinary Backend and App child processes remove the remote token, evolution
  token, and fd pointer case-insensitively; and
- policy, validation, manifest mutation, registry refresh, and scientific
  execution remain Backend-only responsibilities.

This is not a claim that every Desktop route is remotely authenticated or that
the complete four-stage Skill audit system is finished.

Current-state note: ADR 0071 later removed inherited remote authority from the
Electron-managed loopback Python child and added a route-wide Desktop
authentication boundary. The review below intentionally records the earlier,
pre-ADR-0071 snapshot; its Python-child exception and route-wide residual are
historical rather than current guidance.

## Review record

- Reviewer model: `gpt-5.5`; the primary implementation model was unchanged.
- Round 1 read-only session: `019f75a9-2087-7083-877e-e770a4f0a34e`.
  It blocked shipment on a High credential-inheritance path through the
  `edge-tts` helper. The helper now receives an explicit scrubbed environment,
  with a regression at the real spawn boundary.
- Round 2 read-only session: `019f75c4-70df-7d63-87d9-77f17922fbca`.
  It found a High `ccproxy` inheritance path and Medium gaps in unbounded fd
  startup reads plus diagnostic/external-environment subprocesses. Those were
  closed, then the same invariant was extended through adjacent shared
  executors, R helpers, Git acquisition/AutoAgent, DeepAgents, MCP, OAuth,
  notebook, PDF, and operating-system helper boundaries.
- Round 3 final read-only session:
  `019f761d-0dcf-7ca0-8b96-fa0cc8b780a1`.
  It inspected both repositories, the installed MCP client source, authority
  precedence, all governance routes, proxy behavior, and the deliberate Python
  authority exception. Result: **0 findings; `SHIP`**.

The final reviewer did not modify files and did not substitute documentation or
test counts for source inspection. It also did not rerun the complete suites;
the verification below was run in the implementation environment before the
final read-only review.

## Implemented closure

### Backend

- fd authority capture uses a bounded two-second read, consumes and closes the
  descriptor before Runtime initialization, and never falls back after a
  malformed, incomplete, or timed-out higher-priority source.
- `scrub_internal_control_credentials()` recognizes all three control names
  case-insensitively. Low-level sync/async Skill drivers, the AnnData validator,
  adaptive import probes, evolution evidence probes, and generic subprocess
  executors reapply the scrub even when callers provide an explicit `env`.
- Direct tool, notebook, DeepAgents, R, diagnostics, external-env, ccproxy,
  OAuth, iMessage, GitHub acquisition, AutoAgent Git, and PDF paths now pass an
  explicit scrubbed child environment.
- MCP configuration rejects any nested interpolation of a control credential,
  including aliases in env values, stdio arguments, URLs, and HTTP headers. A
  violating server entry is disabled as a whole. Configured stdio environments
  are scrubbed before reaching the optional adapter.
- OpenDataLoader PDF conversion runs in a scrubbed Python wrapper that owns
  bounded output and process-tree termination before third-party Java startup.

### OmicsClaw-App

- The Next UtilityProcess and ordinary Git, hook, file-open, trash, platform,
  interpreter-probe, signing, and taskkill children remove all three control
  names case-insensitively.
- The Python Backend child is the sole documented exception: it may retain the
  remote bearer needed for Backend HTTP policy, but receives local Skill
  Evolution authority only through fd 3.
- A real Git hook regression proves that ordinary App Git actions cannot expose
  any of the three control credentials.
- The file-open route uses argument-vector execution instead of a shell command,
  closing the adjacent command-injection seam while applying the same scrub.

## Verification evidence

- Backend expanded credential, execution, MCP, OAuth, PDF, acquisition,
  AutoAgent, and process-boundary selection: **592 passed, 4 skipped**.
- OmicsClaw-App complete unit suite: **1511 passed, 0 failed**.
- OmicsClaw-App `typecheck`, `lint`, and `electron:build`: passed. The build kept
  one pre-existing non-fatal Turbopack/NFT broad-trace warning.
- Focused Backend Ruff, Python compilation, and both repositories'
  `git diff --check`: passed.
- The Backend combined run also exposed and fixed test-order pollution in OAuth
  tests by restoring provider-related environment state after every test.

## Residual work

The final reviewer classified the following as non-blocking for this
milestone:

- optional `langchain-mcp-adapters` / `mcp` versions are not pinned; the
  installed implementation uses a safe default environment allowlist, but a
  dependency upgrade should be guarded by a real stdio-child contract test;
- acquisition and AutoAgent Git calls scrub credentials, but remote-helper tree
  termination is not yet proven under timeout;
- DeepAgents has source/fake-backed coverage but not a mandatory installed-
  dependency integration gate;
- the App static spawn audit checks explicit environment use but is not a full
  semantic data-flow proof;
- Linux exercises the real fd handoff; native packaged macOS/Windows handoff
  and Windows OpenDataLoader tree termination still need smoke coverage; and
- route-wide remote bearer enforcement for unrelated legacy Desktop endpoints
  is a separate security milestone.

Recommended next order: close the independent Desktop route-wide remote bearer
gate first, then harden acquisition Git helper-tree ownership and pin/test the
MCP spawn contract, followed by native packaged transport smoke coverage.
