# Conversational control-plane Phase 2 closure audit

Date: 2026-07-14

> **Historical closure snapshot.** The 2026-07-15 adversarial review found four
> omitted constraints and reopened this conclusion. The effective reclosure is
> [`2026-07-15-conversational-control-plane-phase-2-reclosure.md`](2026-07-15-conversational-control-plane-phase-2-reclosure.md).
> In particular, the statements below about Surface-supplied ingress ports,
> concurrent Deliveries, dynamic resource acquisition, and deferred Dataset
> Memory ownership are no longer effective.

## Outcome

Phase 2 implementation design is complete. The accepted conversational and Run
control-plane decisions now have one concrete, internally consistent design in
[`docs/design/conversational-control-plane.md`](../design/conversational-control-plane.md).

This is **design closure, not code completion**. The existing runtime still has
the implementation drift listed in `docs/ARCHITECTURE.md`; Phase 3 may now begin
without reopening identity, ownership, durability, replay, or deployment
semantics.

## Coverage of the agreed Phase 2 goals

| Goal | Design evidence | Result |
|---|---|---|
| Raw Inbound, Inbound Envelope and Dispatch Context schemas | Sections 4.1–4.7 | Closed, including process-local ingress ports, ReplyTarget, SourceNamespace, fingerprint and EventFrame |
| Project, Conversation, Turn and idempotency tables | Sections 5.1–5.3 | Closed with concrete SQLite DDL, indexes, immutability and transaction boundaries |
| Attachment ownership and staging | Sections 5.4 and 6.2 | Closed with separate metadata database, content-addressed layout and publish-before-control recovery |
| Transcript content and Turn attribution | Section 5.5 | Closed with immutable entries, replaceable active view and terminal-candidate reconciliation |
| Run Receipt, Manifest, submission and Assignment | Sections 5.2, 5.6 and 7 | Closed with typed scope, resource semantics and Assignment fencing |
| Atomic acceptance and crash points | Sections 6, 7, 11 | Closed for ingress, Turn, Run, Transcript, attachment and delivery commits |
| Sequencing, cancellation, approval and backpressure | Sections 6.2–6.5 and 8.3 | Closed with bounded process-local ownership and typed outcomes |
| Desktop submission and SSE observation | Sections 9.1–9.4 | Closed; submission, observation and cancellation are separate |
| Channel ReplyTarget and persistent Outbox | Sections 4.3, 8 and 9.5 | Closed with single-attempt adapter and unknown-outcome handling |
| Project archive/default/delete semantics | Section 10 | Closed; archive/restore only, no v1 delete, `default/` is Unassigned |
| Startup reconciliation | Section 11 | Closed with one ordered barrier before ingress |
| Legacy Session/Transcript/Project/Job/cache migration | Section 12 | Closed with dry-run, evidence rules, explicit conflicts, cutover and rollback boundary |

## Cross-decision consistency findings

The final audit resolved these implementation-level ambiguities without adding
or changing an ADR:

- A `/new` or Project-selection input receives an idempotent Turn Receipt but
  is handled by the control plane rather than the scientific Agent.
- A stable Attachment fetch capability travels in process-local ingress ports,
  not in serializable Raw Inbound or Inbound Envelope data.
- Transcript content is immutable while its active provider-context ordering is
  replaceable, preventing compaction from invalidating Outbox references.
- An Execution Reference may be absent when the sole Assignment commits and may
  be added only through a matching Assignment callback.
- Static Run resources are a union: one request for a simple Skill, a per-Step
  plan for a fixed Workflow, or a governed envelope for dynamic execution.
- A Delivery's Items execute in ordinal order; failed/unknown earlier Items
  block later Items, while different Deliveries may progress concurrently.
- Terminal Turn state and canonical Channel Delivery commit before the terminal
  Event; the Delivery Pump wakes only after the Event is published and the
  Conversation lease is released.
- Historical attachments without a provable canonical Turn are intentionally
  not imported. No legacy timestamp, filename or “latest file” pointer is
  promoted into false attachment provenance.

None of these choices changes the accepted single-Owner, single-process control
plane or the extensible Run execution seam.

## Validation performed

- Every local Markdown link in the design resolves.
- Every SQL code block in the design executes successfully against an empty
  SQLite database with strict parsing enabled.
- Whitespace validation passes for the new and updated documentation.
- The design contains no TODO/TBD/open architecture question.
- The decision-traceability table maps the retained ADR 0043 execution
  principle and every authoritative ADR 0044–0061 decision to concrete design
  sections.
- Crash matrices cover the durable boundary before and after Turn acceptance,
  Turn terminalization, Run acceptance/Assignment/completion, and every
  provider delivery attempt.

## Deliberately deferred, non-blocking work

These remain outside Phase 2 and do not block Phase 3:

- actual capacity values and provider-specific limits;
- permanent cross-store data purge;
- durable approval/input recovery;
- a measured scheduler policy beyond strict FIFO;
- independently deployed Workers or a distributed scheduler;
- provider-specific delivery reconciliation implementations.

Each can evolve beneath the design or require a future ADR if it changes a
hard-to-reverse guarantee.

## Phase 3 entry sequence

Implementation should proceed as vertical slices:

1. introduce control-domain types, `control.db` migrations/repository and
   transaction/fault tests without connecting a Surface;
2. implement shared ingress, Attachment/Transcript integration and a test
   Surface;
3. migrate CLI to canonical Conversation, Turn and Transcript behavior;
4. migrate Desktop submission, receipt, SSE, cancel and approval routes;
5. migrate Telegram to verify Owner admission, provider-message idempotency and
   Outbox delivery, then move the remaining Channel adapters;
6. introduce canonical Run admission, Dispatcher, shared resource scheduler and
   Remote Job compatibility projection;
7. run the explicit legacy importer/cutover and remove composite Session keys,
   direct terminal sends, mutable attachment registries, observation-triggered
   execution, and runtime legacy fallback.

The fault-injection and migration tests named in design section 14 are release
gates, not optional cleanup after feature work.
