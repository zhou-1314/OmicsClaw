# Govern earned Skill validation promotion in the Backend

## Status

Accepted and implemented for EVO-G1 (2026-07-15).
The narrow Backend EVO-G2 workspace-authority extension passed independent
Round 18 review with 0 Blocker/High/Medium/Low findings (2026-07-18); this does
not mark the broader Skill evolution stage complete.

Refines
[ADR 0042](0042-governed-candidate-plan-execution-and-skill-evolution.md),
[ADR 0057](0057-persist-minimal-run-lifecycle-receipts-in-control-plane-state.md),
and [ADR 0065](0065-verify-skill-output-guarantees-at-the-shared-runner.md).

## Context

The first evolution substrate could aggregate privacy-minimal Skill Run events
and persist a pending proposal, but its writeback Interface was shallow:
product callers supplied an arbitrary target path, patch callback, and three
validator callbacks. Generic successful runs could suggest a validation change
without proving that they were demo executions, and a proposal did not compare
its captured manifest hash immediately before writeback. Proposal synthesis
also had no production caller or Desktop Backend surface.

The Desktop UI lives in the separate `OmicsClaw-App` repository. Governance
policy and writes must not be duplicated there merely to make them visible.

## Decision

### The Backend owns one deep Skill evolution governance Module

`omicsclaw.skill.evolution_governance.SkillEvolutionGovernance` is the product
Interface. It exposes only `refresh`, `snapshot`, `approve`, `reject`, and the
operator recovery command `reconcile`.
Product callers cannot choose a file path, submit a patch function, or replace
the validators. `EvolutionProposalStore` remains append-only persistence
Implementation; its generic apply operation is private.

EVO-G1 supports exactly one writeback:

`smoke-only -> demo-validated`

A proposal is earned only when the current `mvp` or `stable` manifest has:

- explicit successful `demo` evidence;
- the exact Skill id, version, and manifest hash;
- the configured number of distinct execution identities; and
- no script or output-contract defect for that same version/hash.

The default threshold is one real demo because approval executes a fresh demo
again. Ordinary successful runs, old ledger rows without evidence semantics,
environment failures, cancellations, and framework-validator failures do not
become positive demo evidence. Proposal ids are deterministic for one exact
transition, so refresh is idempotent.

### Run identity is not invented by the audit ledger

`SkillRunEvent.run_id` is populated only from an authoritative Run ID. When a
shared-runner call has no such identity it remains blank. The ledger may store
a privacy-safe `execution_fingerprint` for deduplication, but that value is
explicitly non-authoritative and must never be presented as a Run ID. Events
also carry `evidence_kind`, currently `ordinary` or `demo`.

Unexpected output-contract validator failure is classified as
`contract_validator_failed` and aggregated as a framework failure, not a Skill
defect. This prevents an audit implementation failure from manufacturing a
Gotcha or demotion signal against scientific code.

### Approval has three fixed validators and compare-and-swap semantics

Approval requires a non-empty human approver label and review reason. The
Governance Module derives the target manifest and concrete semantic change
itself, then runs exactly:

1. **representation** — parse staged bytes and prove identity, version, the
   one-level validation transition, and durable evidence reference;
2. **execution** — run the Skill demo through the shared runner and unified
   execution contract; and
3. **retrieval** — parse the committed manifest, regenerate `catalog.json` and
   `skill_dag.json`, reload the runtime registry when it owns the same tree,
   and prove the promoted level is observable.

Representation and execution validate staged bytes before the live manifest
changes. Defect evidence for the exact Skill/version/hash is re-read before
approval, after the demo, and immediately before retrieval. A defect arriving
after proposal synthesis therefore makes the approval stale or rolls it back;
the proposal cannot rely on its original evidence snapshot.

The target bytes are compared again after execution and at the final guarded
write. Linux and macOS use an atomic path exchange to verify the exact prior
bytes without a compare/replace gap. A host/filesystem without that primitive
refuses the guarded manifest transition: a cooperative sidecar lock cannot
close the read/compare/replace window against an arbitrary external editor.
Before exchange, the Backend durably stages `after` at a deterministic,
journal-bound same-directory swap witness. It removes that witness only after
the exchanged-out predecessor is verified and the directory barrier succeeds;
an exchange-window process termination therefore leaves any displaced external
bytes available to ADR 0067 reconciliation instead of losing them in a random
temporary file. Predecessor mismatch or publish-barrier failure does not attempt
a userspace check-then-exchange rollback; it retains both paths and requires
reconciliation so a second external write cannot be overwritten by rollback.
Proposal/event JSONL transitions use a POSIX `flock` or Windows `msvcrt` byte
lock and fail closed when neither process-lock primitive exists.

Retrieval failure restores the exact manifest bytes and snapshots of both
generated projections; a same-hash defect is rechecked both before and after
projection refresh, and records `rolled_back` when proposal persistence remains
writable. The final same-hash defect recheck and the durable `approved` append
share the ledger's exclusive lock, so a ledger-governed defect cannot arrive in
the final check/write gap. Every production health-evidence writer must use
`SkillHealthLedger.append`; direct mutation of its JSONL file is unsupported
store corruption, not an alternate event-ingestion Interface. Projection
snapshot, refresh, and rollback all execute under the proposal store's same
exclusive approval transaction. A failed approval therefore cannot restore
stale catalog/DAG bytes after a later serialized approval succeeds. Successful
writeback appends an evidence reference to `validation.evidence` and records
approver, reason, before hash, and after hash.

### Cross-repository ownership stays explicit

The OmicsClaw Backend owns:

- evidence and proposal persistence;
- promotion policy and state transitions;
- manifest writeback and all three validators;
- registry/catalog/DAG refresh; and
- the authenticated `/skill-evolution/*` HTTP contract.

`OmicsClaw-App` owns presentation and interaction. Its implemented review
milestone adds thin Next.js proxy routes, runtime-validated TypeScript view
models, and review UI. It calls the Backend contract and does not directly edit
`skill.yaml`, derive promotion eligibility, compare hashes, or regenerate
projections. Monotonic request epochs prevent an older snapshot request from
clearing a later decision quarantine. Successful decision acknowledgements,
snapshots, and catalogs are parsed before use; missing or malformed readiness
fails closed to a visible error state.

The Backend contract used by that App milestone is:

- `GET /skill-evolution` — latest proposal states plus aggregate ledger health;
- `POST /skill-evolution/refresh` — synthesize missing deterministic proposals
  and return the refreshed snapshot;
- `POST /skill-evolution/proposals/gotcha` — create an evidence-bound Gotcha
  narrative proposal without moving policy or file ownership into the App;
- `POST /skill-evolution/proposals/deprecation` — create an evidence-bound
  deprecation/replacement proposal;
- `POST /skill-evolution/{proposal_id}/approve` — body `{approver, reason}`;
  returns the decision receipt and before/after hashes; and
- `POST /skill-evolution/{proposal_id}/reject` — body `{approver, reason}`;
  returns the persisted proposal state; and
- `POST /skill-evolution/reconcile` — body `{operator, reason}`; converges one
  durable interrupted-approval journal without inferring approval.

All seven routes use a dedicated fail-closed Skill Evolution Bearer dependency.
The Backend freezes that authority exactly once at lifespan startup. Capture
precedence is the one-shot descriptor named by
`OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD`, then the manually managed
`OMICSCLAW_SKILL_EVOLUTION_TOKEN`. `OMICSCLAW_REMOTE_AUTH_TOKEN` is never a
Skill Evolution authority. If neither dedicated source is configured, the
Backend freezes an unconfigured authority and every `/skill-evolution/*`
request returns `503`, even when it presents the ordinary remote bearer. The
descriptor token and every nonblank direct-development token must each be
exactly 64 lowercase hexadecimal characters (the descriptor may carry one
trailing newline). A malformed higher-priority descriptor or direct token
aborts Backend startup and never falls back to a lower-priority source. The
Backend consumes and closes the descriptor before initializing any Runtime.
The descriptor pointer and direct-development variable are removed from the
live environment. Descriptor reads have a two-second startup deadline: a
writer that remains open without delivering one complete token aborts startup
and cannot fall back to a lower-priority source. Audited Skill, adaptive-env,
direct-tool, Notebook, DeepAgents shell, GitHub acquisition, AutoAgent Git,
R/diagnostic, ccproxy, process-tree-helper and generic executor boundaries all
pass an explicit child environment with the three control names removed
case-insensitively. The same invariant is re-applied inside the low-level
sync/async runners, AnnData validator and evolution evidence probe so a caller
cannot reintroduce authority through an `env=` override. MCP interpolation
rejects and disables an entire server entry if any nested env, argument, URL or
header references one of those control variables; renaming `${TOKEN}` into an
ordinary key is not a bypass. The preferred OpenDataLoader PDF path runs in a
scrubbed Python wrapper before it starts Java, discards unbounded native output,
and owns timeout process-tree termination; PDF and consensus-R fallbacks are
also defensive. An unconfigured frozen authority returns `503`; a missing or
incorrect credential returns `401`. Runtime environment edits cannot rotate
the frozen authority.

This does not change the local-first authentication default of unrelated
remote routes. In a packaged local Desktop launch, Electron creates one token
per main-process lifetime and keeps it out of both server processes' initial
environment. It sends one strict versioned bootstrap message to the Next.js
UtilityProcess, which installs the token in server-only memory before loading
the standalone server; timeout or a malformed first message exits fail closed.
Electron sends the same token to Python once over child fd 3, after which both
ends close the pipe. Next/Python child restarts reuse the same Electron-memory
token; an App restart rotates it. The server-only Next.js proxy discards
Renderer-supplied authorization before attaching that token. Remote mode never
forwards it and continues to use connection-profile authorization through
`backendFetch`. A remote deployment must therefore make that connection-profile
credential match an explicitly configured dedicated Skill Evolution token.
The current App profile schema has one bearer field: an operator who wants the
same profile to reach both ordinary and Skill Evolution routes may explicitly
configure the same 64-character value in both remote and dedicated Backend
environment variables. That is two deliberate authority assignments, not an
implicit fallback. Using distinct values requires a future second App profile
credential; until then the remote Skill Evolution Surface is unavailable for
that profile and must fail closed.

App child environments distinguish authority from ordinary helpers. At this
ADR's original milestone, the managed Python Backend launch was permitted to
retain `OMICSCLAW_REMOTE_AUTH_TOKEN` because the Backend process owned remote
HTTP authorization; that was a historical transport allowance, not a required
source of authority. ADR 0071 later narrowed the packaged-child contract: the
current Electron-managed, loopback-only Python Backend removes the inherited
remote token as well as both Skill Evolution environment names, and receives
the local evolution credential only once over fd 3. A separately operated
remote Backend is not an App child and owns its own initial remote token. The
Next.js UtilityProcess and every ordinary App-owned Git/OS helper also remove
remote, evolution and descriptor variables case-insensitively. This includes
Git hooks, so a repository hook cannot turn a Desktop Git action into Backend
bearer authority. A static spawn audit requires an explicit child env, while
real Git hook and mixed-case tests provide the behavioral evidence for the
critical paths.

Direct-environment capture remains a lower-trust development compatibility
path so a separately launched `next dev` and Backend can share one explicit
64-character lowercase hexadecimal value. It is not the packaged transport.
Environment scrubbing prevents ordinary child inheritance, and the packaged
pipe/message design closes deterministic Linux `/proc/*/environ` disclosure of
the local token; neither is an OS sandbox against arbitrary code running as the
same account. PID/process isolation and the remote-token threat boundary remain
separate hardening work and must not be inferred from this ADR.

The 2026-07-19 [ADR 0071](0071-authenticate-the-desktop-api-before-routing.md)
refinement supersedes that historical packaged-child inheritance allowance and
the earlier remote-token compatibility fallback. It also closes the separate
route-wide remote-token boundary; separately operated remote Backends remain
reachable through App connection-profile authority, but that ordinary bearer
does not acquire Skill Evolution capability unless the operator separately and
explicitly assigns the same value to the dedicated authority.

The fd-3 handoff follows Node's extra-stdio descriptor contract and has a real
Linux process probe plus fake-process lifecycle coverage. macOS and Windows are
design-compatible but do not yet have a native packaged Electron-to-Python
handoff gate, so this milestone is not evidence that all three installer
targets have executed the transport end to end.

Approval runs a real scientific demo and may take substantially longer than
the catalog's ten-second read timeout; the App proxy must preserve that
long-running request or later observe a Backend-owned operation rather than
reimplementing the demo locally. `409` is a governance conflict/stale or
revalidation refusal, while request-shape errors remain `422`.

## Subsequent refinements

ADR 0068 and ADR 0069 later added governed demotion/deprecation/replacement and
evidence-bound Gotcha narratives, so the EVO-G1 follow-up list below is a
historical scope statement rather than the current whole-system status. Shared
runner `environment_id` evidence now comes from the actual selected producer
executable, environment, and working directory. A universally propagated
authoritative Run ID and governed parameter revision/writeback still remain
open.

AutoAgent `HarnessWorkspace` accepted-patch promotion is a separate bounded
code-harness mechanism. Its artifact/manifest publication, branch CAS,
source-file snapshots, rollback, and interruption journal do not change the
transaction semantics of `SkillEvolutionGovernance` and are not a
crash-atomic multi-file Skill manifest transaction. Before source promotion,
the harness authenticates every commit in the linear accepted chain against
its canonical, bounded, no-follow, regular single-link manifest and exact patch
artifact. It derives the promotable file set from baseline-to-head Git state;
the manual promotion endpoint derives the durable head itself and does not use
session-result `accepted_files` or `accepted_patches` as promotion authority.
The 2026-07-17 Backend work did not modify or revalidate the independent
OmicsClaw-App repository; Gotcha
materialization there remains a separate thin-client milestone.

The accepted-commit pre-CAS path additionally requires an exact canonical and
unique PatchPlan file set, deterministic hunk-to-blob replay, an otherwise clean
registered worktree, unchanged regular-file modes, direct tree/commit creation,
and a full-record evidence trailer. Canonical artifacts and the complete candidate
chain are authenticated before the accepted ref CAS; a standard Git ref lock
covers reachability proof and unreachable-evidence cleanup. Accepted iteration is
strictly monotonic and Git text evidence is UTF-8 independent of the caller locale.
Validation, rendering, and direct application use one exact-first hunk matcher:
only one normalized fallback is allowed, while multiple exact or whitespace-
normalized occurrences fail closed instead of silently selecting the first block.

Following the Round 15 ignored-state finding, "clean" is no longer inferred from
Git porcelain alone. Following Round 16b, the synthetic baseline is also no
longer a copy-all snapshot. It is constructed only from the source repository's
unique stage-zero tracked regular-file inventory, using current stable worktree
bytes so deliberately dirty tracked content is preserved. The source must be the
exact Git top level with a committed HEAD; non-Git input, missing files/objects,
non-blob index objects, unmerged stages, symlinks, gitlinks, unsupported modes,
aliases, and snapshot drift fail closed. Ordinary untracked files and files
excluded by `.gitignore`, `.git/info/exclude`, or global excludes are never copied
into the sandbox.

Baseline execution uses a detached baseline-commit worktree. Sandbox info
attributes explicitly unset text/EOL, filter, ident, and `working-tree-encoding`
conversion. Immediately after checkout, every raw seed file is hashed with Git's
actual object format and compared with its parent-tree blob OID before that seed
may be cached as authority. For a candidate, the harness freezes an exact raw
persistent-state witness after trusted patch application and before execution,
then requires the post-execution, pre-CAS tree to match it. The witness covers the
root, directories, regular files, symbolic links, inode identity, full mode, link
count, size, timestamps, and content/link-target digest. Raw target bytes must
equal deterministic PatchPlan output and the staged Git tree. Ignored/untracked
paths, empty directories, directory permission changes, post-witness mutation,
hidden index flags, and candidate bytecode are rejected or prevented.

Git authority is persisted as a bounded `git_control_state.json` state machine.
`clean` binds a generation, source/sandbox path and inode identities, accepted
commit, persisted config authority, and the complete common `.git` raw inventory;
`trial_open` binds a random process token, iteration, worktree, and previous clean
digest before `git worktree add`. Only the live process holding that token may use
Git while a trial is open. Every Git subprocess wrapper rejects a newly constructed
workspace until `create()` or `open_existing()` has established authority.
`open_existing()` authenticates an existing clean state without resealing it;
missing/corrupt/legacy/open state, Git maintenance drift, aliases, hard links,
locks, or linked-worktree residue fail closed. A crashed trial is not adopted.

The common Git control tree, linked-worktree marker, and persisted config witness
are compared before any Git command after candidate execution. Drift creates a
durable workspace-global compromise latch. A failure before clean publication
remains safe because durable state is still `trial_open`; clean publication is
the final checkpoint commit, after two matching Git-control snapshots, schema
validation, and accepted-ref equality. If the atomic writer reports an error
after `os.replace` made the requested state visible, the state writer performs a
bounded no-follow stable read: exact requested bytes close the outcome as a
successful publication under the non-power-loss-atomic model, while absent,
aliased, unstable, or different bytes retain the failure and require the latch.
Cleanup is itself an admission gate: promotion occurs only after `git worktree
remove` has succeeded, both the worktree path and its canonical registration are
absent, and the new clean checkpoint is published.
Only `create()` may replace the sandbox and clear a compromise latch, and only
after a fresh clean checkpoint succeeds. Old output directories without this
state have no migration/auto-seal path and must be rebuilt.

`AcceptedPatchRecord`, its manifest, evidence digest, and commit trailer bind the
canonical source root path and directory `(st_dev, st_ino)`; rehydration against
a foreign copy or replacement root fails before source mutation. The promotion
journal binds expected mode, stage and installed inode identities,
and the complete source-root-to-immediate-parent directory identity chain. Recovery
does not infer ownership from equal bytes: an unbound stage, replacement inode,
third hard link, mode drift, target drift, or changed parent chain enters
`recovery_required` and preserves evidence. Target authority is revalidated before
backup deletion and before success; each stage/install/rollback/cleanup/recovery
pathname mutation has an immediate cooperative parent-chain identity check.
Baseline and accepted trees must expose the same unique regular `100644`/`100755`
entry. Source and journal evidence retain their complete POSIX mode but must match
that Git entry's boolean executable class, and applied/installed phases require
`stage_identity == installed_identity`. Non-target tracked paths, HEAD, and the
stage-zero index are rechecked against the evaluated baseline at initial
promotion admission, before the first install, after installs, after cleanup,
and before interrupted/applied journal recovery mutates or reports success.
Transaction targets are excluded from that whole-tree byte comparison because
their partial/baseline/accepted states are authenticated separately by the
journal and per-target CAS. POSIX permission changes within one Git executable
class (for example `0644` to `0600`) are deliberately not treated as Git-program
drift; target mode is still retained and checked by the transaction. This
is not a `dirfd`/`openat`/`linkat`/`renameat` transaction: the final check-to-system-
call interval remains an OS-level TOCTOU boundary, and the mechanism is neither a
power-loss-atomic transaction nor a same-UID tamper seal or OS sandbox. The two
raw witnesses prove persistent endpoint state; they cannot prove that a same-UID
process did not create, consume, and delete transient state between observations.
The tracked-only baseline proves the evaluated Git program, not equality with the
entire final source filesystem: untracked/open-world source files remain outside
the baseline and may still affect a later runtime that discovers them. Likewise,
the durable state is a cooperative restart commitment; a same-UID writer able to
rewrite both the state and Git authority is outside this model.

## Consequences

- The evolution path now has Depth: one Backend policy change reaches CLI-run
  evidence, the audit ledger, generated representations, retrieval, and every
  Surface through a small Interface.
- A generic success can no longer silently raise retrieval rank.
- Desktop Backend list/detail responses expose `validation_level`,
  `superseded_by`, and an honest `readiness` name; legacy `health` remains a
  compatibility alias for file readiness, while ledger health is returned by
  the evolution snapshot.
- The App freezes all review actions after any decision whose authoritative
  snapshot cannot be reloaded, and only unfreezes after a successfully parsed
  and causally current Backend snapshot. Runtime-health evidence is displayed
  only against the exact Skill id/version/hash bucket; catalog refresh remains
  independent from decision-state reconciliation. Retry and modal dismissal
  remain disabled while an irreversible decision request is in flight.
- EVO-G1 is not general self-evolution. Gotcha synthesis/writeback, parameter
  revision, demotion, and deprecation/replacement approval remain follow-up work.
- The manifest, proposal JSONL, and two generated JSON files are not one
  crash-atomic filesystem transaction. [ADR 0067](0067-reconcile-interrupted-skill-evolution-approvals.md)
  now writes a durable pre-commit intent and provides explicit, drift-safe
  reconciliation after process termination or proposal-store repair.
- Acquisition, execution, and evolution still lack a universally propagated
  authoritative Run ID; the audit ledger now leaves this gap visible instead
  of fabricating an identity.
- The current remote auth contract is a shared Bearer secret, not a user
  identity provider. `approved_by` is therefore an authenticated caller's
  asserted audit label, not a cryptographically verified human principal.

## Alternatives considered

- **Keep caller-supplied validators.** Rejected because a product caller could
  pass three no-op functions and write an arbitrary file.
- **Automatically promote after a demo.** Rejected because fresh execution
  evidence still requires an explicit human decision.
- **Put promotion logic in OmicsClaw-App.** Rejected because remote, CLI, and
  Channel operation would diverge and the frontend must not own Backend files.
- **Use an output directory leaf as Run ID.** Rejected because ADR 0057 defines
  Run ID as an opaque control-plane identity, not a path convention.
