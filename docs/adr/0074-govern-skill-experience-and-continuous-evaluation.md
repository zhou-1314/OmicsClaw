# Govern Skill experience and continuous evaluation in the Backend

## Status

Proposed (2026-07-22).

Implementation: Not started.

Refines the target direction of
[ADR 0030](0030-first-class-skill-type-system.md),
[ADR 0037](0037-unified-declarative-skill-representation.md), and
[ADR 0065–0069](0065-verify-skill-output-guarantees-at-the-shared-runner.md).
It does not supersede their implemented execution or governance decisions.

Detailed design:
[Skill audit continuous evaluation and experience governance](../design/skill-audit-continuous-evaluation.md).

## Context

OmicsClaw already treats a Skill as a governed scientific asset rather than an
unvalidated prompt folder. `skill.yaml` is the sole machine contract; the
shared runner verifies dynamic output guarantees; execution events bind exact
Skill and source identity; and earned promotion, demotion, Gotcha, deprecation,
replacement, guarded writeback and recovery remain Backend-owned and
human-gated.

The current audit system is nevertheless event- and proposal-centric. It has
no unified per-Skill experience view, no versioned evaluation protocol, no
first-class stability result, and no distinction between a validation level
written after an earlier approval and the level supported by current source
and protocol evidence. Runtime health, routing feedback, evaluation scripts
and governance are therefore harder to inspect as one lifecycle.

MUSE-Autoskill correctly argues that Skills should accumulate experience and
be evaluated throughout creation, use, management and refinement. Its direct
mechanisms are not sufficient for OmicsClaw's domain: a free-form `.memory.md`
can drift away from the current source; generated unit tests are not a proof of
scientific validity; and automatic refinement, merge or forgetting would
bypass the existing evidence, approval and local-first safety model.

The Backend and the separate OmicsClaw-App also already have a live contract.
The App strictly reads `proposals` and `health`, proxies only Backend-owned
commands, uses a dedicated Skill Evolution authority, and freezes decisions
until authoritative state is reloaded. A new design must preserve old-App/new-
Backend and new-App/old-Backend behavior instead of replacing that contract in
one coordinated release.

## Decision

### Introduce one deep `SkillAuditRuntime` Module

The Backend will own one Module that accepts typed execution, evaluation,
routing and decision evidence and produces bounded audit read models and
evolution candidates.

Its public Interface will expose only typed evidence ingestion, evaluation
operation commands, bounded snapshots and candidate refresh. It will own
identity normalization, privacy filtering, error attribution, aggregation,
evidence freshness, effective validation and candidate policy. Product callers
will not provide target paths, patch functions, validators, thresholds or
validation levels.

The existing `SkillHealthLedger` is an initial evidence Adapter. Historical
events remain readable according to the evidence they actually contain; the
new Module will not invent missing source, protocol or Run identity.

`SkillEvolutionGovernance` remains the sole mutation authority. Every Skill
change still requires a Backend-generated proposal, human approval, the
kind-specific fixed validators, guarded publication, projection refresh and
reconciliation. `SkillAuditRuntime` cannot modify canonical Skill files.

### Bind every conclusion to an exact Skill revision

The audit identity is:

`skill_id + version + manifest_hash + source_hash`.

Environment identity, evaluation protocol digest and authoritative Run ID are
orthogonal evidence dimensions and may not substitute for that revision.

New evidence uses a versioned envelope with a unique id, timestamp, kind,
revision, environment, optional Run and protocol references, closed outcome
and reason codes, bounded allowlisted metrics and privacy-safe evidence
identifiers. Equal event ids with different bytes are conflicts. Skipped events
never earn validation.

Raw scientific data, full prompts, credentials, arbitrary paths, arbitrary
stderr and arbitrary object stringification are forbidden in the audit store
and Desktop read models.

### Replace free-form Skill memory with a derived Experience View

For each exact Skill revision, the Backend will derive a rebuildable
`Skill Experience View` containing usage, health by environment, stability,
validation freshness, approved Gotchas, coverage gaps and pending proposal
identifiers.

The view is a projection over the audit ledger, not a sibling `.memory.md`, a
new Skill source of truth or a Graph Memory fact. The same discipline applies to
any global free-text experience sink, not only a per-Skill note file — the
running risk is an agent-appended, prompt-injected free-text memory, of which a
per-Skill `.memory.md` is only the narrowest form. The Experience View is a
typed, evidence-derived projection and never becomes a free-text note field.
Graph Memory may receive a rebuildable view projection, but it never owns audit
events, validation, proposal state or decisions.

Free-form experience wording may enter canonical Skill documentation only
through an evidence-bound narrative proposal with source anchors,
counterexamples and human approval, following ADR 0069.

### Make evaluation protocol-bound and continuous

`skill.yaml.validation` will be extended with optional Evaluation Protocol
references. A protocol declares a stable id, kind, entry, durable dataset
reference and repeat policy. Its digest binds the executable protocol,
declared assets, relevant dependencies, pass conditions and dataset reference.
The dataset reference binds a content identity — reusing the ADR 0064
dataset-observation identity (store id, relative path, `content_sha256`) — so
the digest changes when the evaluation dataset changes and a
`fixture-validated`/`benchmarked` result stays reproducible; small fixtures are
committed while representative benchmark data lives in a durable store, never an
ad-hoc path. Per-Skill-type default protocol templates are provided and
overridable, so reaching `benchmarked`/`production` does not require every Skill
to hand-author stability and environment thresholds. Only a declared protocol
can earn a validation level.

The validation ladder remains:

- `smoke-only`: compatibility floor; not proof that a demo passed;
- `demo-validated`: explicit demo passed the shared runner contract;
- `fixture-validated`: committed fixture and deterministic assertions passed;
- `benchmarked`: representative data, statistical invariants and pinned tool
  versions passed;
- `production`: benchmarked plus declared stability and target-environment
  coverage, no open high-risk audit item, and human approval.

Stability is an orthogonal result, not one global score. Evaluation Protocols
declare repeats and tolerances; results may report execution success, envelope
and artifact consistency, allowlisted scientific metric dispersion, resource
quantiles and target-environment consistency. There is no universal five-run
or zero-variance requirement.

### Separate declared validation from effective validation

The manifest continues to record the last human-approved
`declared_validation_level`. The Backend derives an
`effective_validation_level` and one of `current`, `stale`,
`evaluation_required` or `review_required` from evidence applicable to the
current source, protocol and relevant dependency revision.

Source or protocol drift does not automatically rewrite the manifest, erase
history or demote a Skill. It does prevent old evidence from being presented
as current evidence.

The evidence-derived `effective_validation_level` changes as new evidence
arrives with no manifest write, so it lives only in the audit snapshot and Skill
Experience View — rebuildable, non-CAS read models. It is not written into
`catalog.json`: that projection is regenerated only under the guarded manifest
CAS at approval time (ADR 0066/0068) and must keep exposing the approved
`declared_validation_level`. Catalog may carry an opaque pointer or a `stale`
flag, but never a live effective level. The Desktop snapshot exposes both levels
together.

The router uses current/effective evidence only as a bounded, deterministic soft
tiebreaker inside the compatible candidate set already established by the
resolver; it never invents a new equivalence relationship, never excludes a
candidate, and the ranking must not compound — a less-routed Skill cannot lose
rank purely because it accrued less routing evidence. Stale evidence alone does
not hide a unique scientific capability or block explicit runs.

### Keep refinement and Skill-bank management human-gated

The Runtime may generate candidates for validation changes, Gotchas,
applicability, parameters, evaluation protocols, merge/replacement and
strategic deprecation. Each candidate binds its exact target revision,
supporting events, counterexamples, risk, expected impact, required evaluation
and stale conditions.

Failure evidence may generate a structured remediation brief, but it cannot
directly change scientific code or defaults. AutoAgent may use that brief to
prepare a patch or replacement in a governed Workspace; it cannot approve or
publish the result.

Merge is a two-stage process. The Backend first identifies overlap. A new or
existing replacement must then pass controlled acquisition, reach at least
`demo-validated`, and pass routing regression before each old Skill can receive
a replacement-backed deprecation proposal. The Backend never concatenates two
Skill implementations or methodology documents.

Low usage alone never deletes, hides or demotes a Skill. Strategic deprecation
requires a separately defined evidence kind, one validated replacement and the
same human-gated lifecycle consequences as ADR 0068.

### Reuse RunRuntime for evaluation execution

Long-running evaluation is represented by a bounded `AuditOperation` with
`accepted`, `running`, `succeeded`, `failed`, `canceled` and `interrupted`
states. It is an orchestration and observation record, not a new executable
queue.

Every demo, fixture, benchmark and stability repetition executes through the
existing RunRuntime, Run Dispatcher and Execution Resource Scheduler. An
evaluation Run carries no owning Project assignment (or a governance-only
context): it passes Dispatcher admission (ADR 0061) and receives a fresh output
claim (ADR 0070) like any Run, but freezes no ADR 0064 analysis-lineage Memory
projection, because an evaluation is not user science and must never enter a
Project's scientific continuity. An App disconnect releases only observation.
Cancellation is a separate explicit command. Restart does not reconstruct
executable payloads or grant another Assignment.

**Phased implementation (2026-07-23).** The first implementation runs evaluations
through the existing shared-runner primitive rather than the RunRuntime governed
queue: a `demo` protocol runs through the shared runner (`run_skill`), and a
test-backed protocol's entry runs in a bounded, credential-scrubbed subprocess;
`SkillEvolutionGovernance.evaluate()` stores digest-bound results that lift
effective validation, and `POST /skill-evolution/evaluations` runs synchronously
in a worker thread. This deliberately defers the RunRuntime-queue integration —
resource scheduling, the async `AuditOperation` lifecycle (`accepted` → …), the
explicit-cancel/observation contract, the unassigned-Run/no-0064-projection
plumbing, and the `202 AuditOperationReceipt` wire — to a follow-up, so protocols
can be evaluated and earn levels now without the full control-plane change. The
result store, digest freshness, and effective-validation derivation are already
queue-agnostic, so migrating the executor does not change the read model.

### Extend the Desktop contract additively

The existing `GET /skill-evolution` response retains `proposals` and `health`
with their current required fields. The Backend adds `schema_version`, an
opaque `authority_epoch`, a `snapshot_revision` that is monotonic within that
epoch, `generated_at`, `capabilities` and a bounded summary. Backend restart or
authority replacement creates a new epoch; clients compare the epoch/revision
pair rather than treating revisions from different epochs as ordered. Additive
fields are permitted within the contract major version; breaking changes
require a new major route or media type.

The Backend adds bounded paginated detail Interfaces under the existing
authority prefix:

- `GET /skill-evolution/skills`
- `GET /skill-evolution/skills/{skill_id}`
- `GET /skill-evolution/proposals/{proposal_id}`
- `GET /skill-evolution/operations/{operation_id}`
- `POST /skill-evolution/evaluations`
- `POST /skill-evolution/operations/{operation_id}/cancel`

The existing `/skill-evolution/refresh` continues to synthesize candidates
from available evidence and does not implicitly launch expensive evaluation.

Old Apps continue to consume `proposals` and `health`. New Apps treat a missing
schema/capability set as a legacy Backend and hide unsupported controls rather
than deriving policy locally. Existing decision acknowledgements, proposal
CAS, catalog reconciliation and the App's uncertain-decision quarantine remain
current. A new App may submit an expected snapshot revision as an additional
fence together with the expected authority epoch, but old Apps are not
required to do so.

All new routes remain inside the existing `/skill-evolution` authentication
authority. OmicsClaw-App remains a thin server-side proxy and UI Adapter: it
may render kind-specific views, but it does not own attribution, thresholds,
eligibility, validation, replacement policy or file mutation.

### Fail closed for governance without rewriting completed science

An audit append failure after a scientific Run has otherwise completed does
not retroactively change that Run to failed. The missing event cannot become
validation evidence and produces a Backend framework incident.

A corrupt ledger, inconsistent snapshot, invalid protocol or failed audit
projection blocks audit reads, candidate creation and governance decisions. It
does not silently skip evidence. Ordinary explicit Skill execution may remain
available because audit storage is not its execution authority.

Only a fully constructed and consistency-checked authoritative snapshot may
return HTTP 200. The Backend does not serve a stale cached snapshot as an
authoritative success. This preserves the App rule that review stays frozen
until a causally current snapshot is loaded.

## Consequences

- Per-Skill experience becomes inspectable without adding an uncontrolled
  memory file or second Skill truth source.
- Validation communicates both the historical approved claim and what current
  source/protocol evidence supports.
- Generated unit tests become one evidence class rather than an overclaimed
  proof of scientific correctness.
- MUSE-style refinement, merge and pruning are retained as evidence-driven
  maintenance signals while remaining human-gated.
- The App can adopt Overview, Skills, Evaluations and Proposals views without
  moving Backend policy into TypeScript.
- Existing Apps and Backends have an explicit compatibility path; cross-repo
  rollout can occur in independent milestones.
- The Backend gains another durable evidence/read-model responsibility. Event
  schema, protocol hashing, pagination, operation observation and projection
  recovery require focused implementation and contract tests.
- Declared and effective validation may temporarily disagree. That
  disagreement is intentional evidence visibility, not automatic mutation.
- This proposal does not close the existing M1/M2/M3 partial status by itself.
  Implementation and the AUD-01 through AUD-10 acceptance evidence remain
  future work.
- The lowest-risk first implementation slice is the Skill Experience View, the
  declared/effective validation separation, and the additive snapshot fields,
  all computed over the existing `SkillHealthLedger` evidence with no new
  Evaluation Protocol schema or `AuditOperation` yet. It delivers honest
  validation freshness and inspectable experience while validating AUD-01/02/04
  and the AUD-07 additive contract before the heavier protocol and operation
  machinery.
- Moving candidate synthesis out of `SkillEvolutionGovernance.refresh()` into
  `SkillAuditRuntime` is a careful refactor of already-shipped ADR 0066/0068/0069
  code: `SkillAuditRuntime` produces only deterministic candidate inputs, while
  `SkillEvolutionGovernance` retains proposal persistence, deterministic proposal
  ids, idempotent refresh, and the ledger-exclusive-lock evidence recheck those
  ADRs established.

## Alternatives considered

### Add `.memory.md` beside every Skill

Rejected. It is easy to implement but creates free-form state that is not
necessarily bound to the current Skill revision, is difficult to redact, and
can be mistaken for a transferable Skill contract.

### Extend only the existing M0–M3 checklist

Rejected as the architecture. Documentation-only fields would leave evidence
normalization, evaluation, experience, freshness and proposal policy spread
across existing callers. The Runtime Module is required for Locality and
Leverage. The M0–M3 checklist remains the acceptance baseline and will link to
the new AUD criteria.

### Allow failed evaluation to invoke an automatic Refiner

Rejected. A generated patch can change scientific meaning and would bypass
human approval, exact-source revalidation and guarded writeback. Automatic
systems may prepare a remediation candidate only.

### Automatically merge or delete unused Skills

Rejected. Similar descriptions do not prove equivalent scientific methods,
and low usage does not prove low value. Replacement-backed deprecation retains
history and makes runtime consequences explicit.

### Put experience and evaluation policy in OmicsClaw-App

Rejected. CLI, Channel, Remote and headless Backend callers would diverge; the
App cannot own scientific execution, manifest writes, registry refresh or
evidence eligibility.

### Create a separate evaluation scheduler

Rejected. It would duplicate the Run Dispatcher, Resource Scheduler and
Assignment authority and create conflicting cancellation/restart semantics.
