# OmicsClaw Architecture Decision Records

This directory preserves the history of consequential architecture decisions.
It is not the source of truth for the system's current structure, a backlog, or
an implementation log.

## What belongs in an ADR

Record a decision here only when all three conditions hold:

1. Reversing it later would be meaningfully expensive.
2. The chosen shape would be surprising without its rationale.
3. Real alternatives existed and were rejected for explicit reasons.

Routine refactors, implementation notes, test plans, review findings, and open
work belong in design, plan, review, or issue-tracking documents instead.

## Document roles

- `CONTEXT.md` files define the project's domain language and relationships.
- The architecture ledger separates verified as-built behavior, accepted
  target behavior, and explicit drift.
- ADRs explain why consequential choices were made at a point in time.
- `docs/design/` holds detailed designs that may evolve without changing an
  architecture decision.
- `docs/plans/`, `docs/proposals/`, and `docs/reviews/` hold execution work,
  proposals that are not yet decisions, and review evidence respectively.

The canonical current-state document is
[`docs/ARCHITECTURE.md`](../ARCHITECTURE.md). No dated snapshot or public
overview should be treated as current merely because it is named `current` or
`overview`.

## Effective conversational-control-plane decision chain

The latest control-plane ADRs are cumulative, not a flat list of equally
current prose. Read them in this effective order:

| Area | Effective decision |
|---|---|
| Historical starting point | ADR 0043 is superseded by ADR 0044 for Owner/tenancy semantics; only its local-first control-plane and extensible Run-execution principle is retained |
| Owner, ingress, Conversation, Turn | ADR 0044–0052 |
| Authority, persistence, Project and Run identity | ADR 0053–0058 |
| Attachments and terminal Channel delivery | ADR 0059, refined by ADR 0073 for Attachment Store ID minting; ADR 0060 for terminal delivery |
| Run dispatch and global capacity | ADR 0061, refined by ADR 0062 for dynamic governed Runs |
| Same-target Delivery ordering | ADR 0063 refines ADR 0060 |
| Scientific Memory ownership and archive-safe projection | ADR 0064 refines ADR 0045 and ADR 0053–0055 |

Outside the conversational-control-plane chain, ADR 0065 defines the current
shared-runner Skill output-verification contract and the honest interpretation
of declarative Skill security metadata. ADR 0066–0069 define evidence-bound
Skill evolution governance, while ADR 0070 requires fresh, exclusively claimed
output directories beneath every production execution Surface. ADR 0071
defines the route-wide, pre-body Desktop remote-authentication boundary and the
corresponding thin App credential adapter. ADR 0072 refines that boundary with
durable cross-request operation bindings, receipt-bound AutoAgent recovery, and
a governed AutoAgent process owner. ADR 0073 refines ADR 0059 by moving opaque
Attachment ID minting from the control plane to the Attachment Store.
ADR 0075 replaces the standard Skill runner's synthesized notebook with a
runner-owned Skill Replay Capsule and explicit fresh-Run verification; genuine
Autonomous Code Mini-Agent notebooks and Replay artifacts remain separate.

ADR 0074 is **Proposed**, not part of the effective decision chain. It proposes
a Backend-owned `SkillAuditRuntime`, protocol-bound continuous evaluation,
derived Skill Experience Views, declared/effective validation separation, and
an additive OmicsClaw-App-compatible `/skill-evolution` contract. Experience
Views, protocol/result evidence, bounded local content-addressed evaluation
artifacts, content-bound case evaluation and offline Benchmark Campaign analysis
are phased implementations; RunRuntime-backed AuditOperation and the remaining
proposal/App slices are not. ADR 0065–0069 and the current audit baseline remain
authoritative while ADR 0074 is Proposed.

The concrete integrated contract is
[`docs/design/conversational-control-plane.md`](../design/conversational-control-plane.md).
When an older ADR body contains wording explicitly refined by a newer ADR, the
newer decision and the integrated design are effective; the older wording is
retained only as decision history.

## Status model

Every ADR must have one of these statuses:

- **Proposed** — under active consideration; not authoritative.
- **Accepted** — the decision is authoritative, whether or not implementation
  is complete.
- **Rejected** — considered and explicitly not chosen.
- **Deprecated** — still present but no longer recommended; no single successor
  fully replaces it.
- **Superseded by ADR-NNNN** — replaced by a newer decision.

Implementation state is separate from decision status. If useful, record it as
an `Implementation` note such as `Not started`, `In progress`, `Implemented`, or
`Implementation drift detected`; do not invent a second status vocabulary.

## History-preserving rule

Accepted ADRs are historical records. Do not rewrite them to make an old
decision appear consistent with today's code.

Allowed edits to an accepted ADR are limited to:

- correcting broken links, spelling, or unambiguous factual errors;
- adding an explicitly dated clarification that does not change the decision;
- changing its status to `Deprecated` or `Superseded by ADR-NNNN`;
- linking to implementation evidence or a newer ADR.

When the decision itself changes, create a new ADR and link both directions.
Do not accumulate inline amendments until the original choice and the current
choice can no longer be distinguished.

## Lifecycle

1. Write a concise `Proposed` ADR only after the decision qualifies for an ADR.
2. Resolve its alternatives and consequences before marking it `Accepted`.
3. Track implementation work outside the ADR; link to it when useful.
4. Update the current architecture document when the implemented system changes.
5. If a later decision replaces it, create a new ADR and mark the old one
   `Superseded by ADR-NNNN`.

An accepted ADR may contain known consequences, but unresolved design choices
must remain outside its accepted decision. An ADR that still asks the reader to
confirm a core choice is `Proposed`, not `Accepted`.

## Existing-record audit

The pre-existing ADRs are grandfathered as historical material while the
architecture re-baselining audit is in progress. They will be classified and
cross-linked incrementally; none will be silently deleted or wholesale
rewritten. The audit will distinguish:

- valid current decisions;
- decisions with stale assumptions;
- decisions superseded by later ADRs;
- implementation drift;
- detailed designs or plans that were misfiled as ADRs;
- missing decisions that require a new ADR.

**Audit pass 2026-07-21 (ADR 0003–0073, full sweep).** The overwhelming majority
are valid current decisions. The only decision-status changes:

- **Superseded:** ADR 0043 (already self-marked, by ADR 0044); ADR 0014's core
  "runner has no brain" decision is superseded in part by ADR 0032 — its two
  outer judgment seams are retained.
- **Stale (partial):** ADR 0015's `auto` = run-as-typed half is retired (the
  analysis-router mode selector was removed 2026-06-22); its assisted-
  parameterization half remains current.
- **Cross-links added (no status change):** ADR 0024 → ADR 0039 (char-budget
  stance superseded, all prefix-cache invariants retained); ADR 0030 → ADR 0037
  (`type`/`validation_level` fields moved to the `skill.yaml` v2 carrier).
- **Status hygiene (no decision change):** ADR 0012 and ADR 0031 relabelled to
  match their shipped implementations; ADR 0010 now back-links ADR 0016; ADR 0034
  carries an explicit `Implementation: Not started` note (accepted roadmap target,
  not drift).

No ADR files were moved, renumbered, or deleted; every change is an in-place edit
permitted by the History-preserving rule above.
