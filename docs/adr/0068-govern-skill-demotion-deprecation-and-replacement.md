# Govern Skill demotion, deprecation, and replacement in the Backend

## Status

Accepted and implemented for EVO-06 (2026-07-16).

Refines
[ADR 0066](0066-govern-earned-skill-validation-promotion-in-the-backend.md)
and [ADR 0067](0067-reconcile-interrupted-skill-evolution-approvals.md).

## Context

Earned validation promotion already used exact Skill version/hash evidence,
human approval, fixed validators, guarded manifest publication, projection
refresh, and interrupted-approval reconciliation. The inverse lifecycle was
still only declarative: a health failure could not safely produce a demotion
candidate, no Backend Interface could propose replacement-backed deprecation,
and an explicitly named deprecated Skill could still reach the shared runner.

Ordinary failures are not sufficient evidence to lower a validation level.
Missing dependencies, resource exhaustion, cancellation, framework validator
failure, and a failure outside demo mode do not disprove the statement that a
Skill has a working demo. Deprecation also cannot be a free-form path edit: it
changes automatic routing and every execution Surface.

## Decision

### Demotion is earned only by a reproduced demo defect

The only automatic demotion candidate is:

`demo-validated -> smoke-only`

`refresh()` creates it only from a distinct, explicit `demo` Run event for the
exact Skill id, version, and manifest hash whose classified error is
`script_defect` or `contract_failure`. Ordinary Run failures and environment,
framework, or cancellation outcomes cannot create this candidate.

Approval runs the same demo again through the shared runner. The writeback is
allowed only when the fresh outcome again classifies as a Skill defect. A
successful demo or a non-Skill failure refuses the change and leaves the live
manifest unchanged. The historical evidence list records the approved
demotion; prior promotion evidence remains audit history.

### Deprecation requires the same conservative project revision and one live replacement

`SkillEvolutionGovernance.propose_deprecation()` accepts canonical Skill ids,
an asserted proposer label and reason, and selected health-event ids. It does
not accept a path, patch function, or validator. The Backend requires:

- a current `mvp` or `stable` source Skill;
- three distinct `script_defect` or `contract_failure` events bound to the
  exact Skill id, version, target manifest hash, and conservative project
  execution revision from ADR 0069 by default;
- one different canonical replacement whose lifecycle is currently `mvp` or
  `stable` and whose earned validation is `demo-validated` or higher; and
- the replacement's exact id, version, and manifest hash bound into the
  deterministic proposal.

Approval rechecks both manifests, reruns the replacement demo, then rechecks
the replacement before publication, during retrieval validation, and at the
final ledger fence. The approved source transition is:

```yaml
lifecycle:
  status: deprecated
  superseded_by: <canonical replacement id>
```

The v2 schema requires `superseded_by` exactly for deprecated Skills and
forbids self-replacement. Registry loading additionally rejects a missing,
alias-only, draft, deprecated, or merely `smoke-only` replacement.

### The same approval and recovery transaction applies to all three kinds

Promotion, demotion, and deprecation share the fixed representation,
execution, and retrieval validators, exact target CAS, projection snapshot and
rollback, durable decision audit fields, and ADR 0067 recovery journal. The
journal recomputes the kind-specific expected `after` bytes; it never infers a
decision or performs an unproved second exchange.

`catalog.json` now projects `superseded_by`. Registry reload, catalog, and
compatibility DAG rebuild remain Backend-owned.

### Lifecycle state has runtime consequences

- automatic capability resolution omits draft and deprecated Skills;
- an explicit mention of a deprecated canonical or legacy alias redirects to
  its governed replacement and records the reason;
- the LLM-facing canonical Skill enum omits draft and deprecated entries; and
- the shared runner rejects explicit deprecated execution before output
  allocation or process spawn, returning the replacement hint.

CLI, Desktop, Channel, agent tools, candidate planning, and remote callers
therefore consume one Backend lifecycle policy rather than implementing
Surface-local filters. Deprecated Skills remain visible in catalog and audit
views.

### Cross-repository ownership remains unchanged

The OmicsClaw Backend owns evidence selection rules, proposal persistence,
manifest writes, validation, projections, and the Bearer-protected
`POST /skill-evolution/proposals/deprecation` contract. OmicsClaw-App may proxy
that contract and present review interaction, but it must not derive
eligibility, edit manifests, or regenerate projections. This milestone does
not modify the App repository; its existing tolerant proposal reader can
review Backend-created demotion or deprecation proposals.

## Consequences

- Validation demotion now means the evidence that earned the level no longer
  reproduces; generic flakiness cannot silently lower it.
- A deprecated Skill has a machine-verifiable live successor and cannot be
  selected or executed accidentally.
- Strategic deprecation without exact execution defects is intentionally not
  represented by this first evidence-bound policy; it needs a separate
  evidence type rather than weakening Run-event semantics.
- An approval already durably appended before process interruption remains an
  approved human decision. Recovery finalizes its journal when the approved
  manifest bytes are live; later source or replacement-validation drift does
  not retroactively revoke that decision. Persisted post-approval validation
  state and a replacement-drift-triggered `review-required` transition remain
  follow-up governance work.
- Gotcha synthesis/writeback and parameter revision remain follow-up work.
- `approved_by` and `proposed_by` remain authenticated callers' asserted audit
  labels under the shared Bearer-secret contract, not verified human identity.

## Alternatives considered

- **Demote after repeated ordinary failures.** Rejected because those failures
  do not invalidate demo evidence and may be environmental.
- **Let approval choose an arbitrary replacement path.** Rejected because it
  bypasses canonical identity, registry validation, and recovery.
- **Silently execute the deprecated source after showing a warning.** Rejected
  because Channel and agent Surfaces could ignore the warning.
- **Move lifecycle policy into OmicsClaw-App.** Rejected because CLI, Channel,
  remote, and headless Backend callers would diverge.
