# Separate Run dispatch from process-local resource scheduling

## Status

Accepted (2026-07-14).

Refines
[ADR 0042](0042-governed-candidate-plan-execution-and-skill-evolution.md),
[ADR 0043](0043-local-first-control-plane-extensible-run-execution.md),
[ADR 0057](0057-persist-minimal-run-lifecycle-receipts-in-control-plane-state.md), and
[ADR 0058](0058-bind-retried-run-submissions-to-one-fenced-execution-assignment.md).

**Dynamic-Run refinement (2026-07-15):**
[ADR 0062](0062-reserve-one-governed-resource-envelope-for-dynamic-runs.md)
preserves this ADR's one global capacity authority but forbids a live governed
parent from submitting nested global tickets. A dynamic Run reserves one
aggregate envelope globally and suballocates it with Run-local allocations;
fixed plans continue to obtain one global Resource Lease per process.

Every accepted top-level Skill, Workflow, Candidate-plan and Autonomous Run
enters one bounded process-local **Run Dispatcher**. The Dispatcher owns Run
order, active-Run admission and the sole Assignment transition; one shared
process-local **Execution Resource Scheduler** independently owns atomic
multidimensional Resource Leases for every scientific process. Neither is a
durable executable queue, and a Resource Lease never authorizes Run execution.

## Implementation

Canonical Simple Skill tracer plus Desktop, prompt-toolkit demo, root `oc run`
exact-demo Scope family and Remote exact-demo submission Adapters implemented
(root explicit Scope extended 2026-07-18), all-kind convergence incomplete.
`omicsclaw/control/run_dispatcher.py` now provides one bounded
process-local FIFO with explicit pre-acceptance reservations, active-Run slots,
queued removal, per-Submission admission guards and quarantine on uncertain
enqueue compensation. `RunRuntime` obtains a correlated first-unit
`ResourceLease`, then performs the sole `queued -> running + Assignment ID`
transition, and only the winning Assignment invokes the shared Skill runner.
Desktop Receipt reads and CLI terminal-result observation never enter that
path.

The canonical Linux Assignment also atomically persists one unique write-once
`linux-user-systemd-bwrap-v1` Execution Reference before launch. The executor
uses that exact user-systemd scope, a parent-death-bound trusted launcher and a
bubblewrap PID/cgroup namespace; descendants cannot create a usable nested
user scope or migrate outside the owner. Stop confirmation requires the unit to
be absent or its cgroup to report `populated=0`. Startup and shutdown verify
that owner before applying Assignment-fenced Manifest evidence. Unknown owner,
cgroup occupancy, completion evidence or terminal Control commit keeps the Run
nonterminal and quarantines both Dispatcher admission and the process-global
Scheduler; executable payloads are never rebuilt or replayed.

`ExecutionResourceScheduler` remains strict FIFO and atomically accounts
process slots, CPU, memory, GPU devices, threads and temporary disk. It now
accepts a correlated `ResourceTicket` and is shared by Candidate plans and the
canonical Simple Skill Runtime used by the Desktop tracer and exact
prompt-toolkit `/run <canonical-skill> --demo`. The three root exact-demo Scope
wires and strict Remote `POST /jobs` exact-demo submissions bind to that same
Runtime, Dispatcher, Scheduler, sole Assignment and shared runner; Remote
Job-shaped identity is only `run-<run_id>` compatibility projection. Workflow,
Autonomous, root non-demo/unsupported-option,
Textual TUI, other prompt-toolkit Run forms and Agent/Bench paths do not all
pass through the Dispatcher/Scheduler pair yet.

Candidate-plan execution also has a separate `max_concurrency` semaphore around
resource waiting and execution. It is useful as a per-plan in-flight-step
bound, but must not remain a second authority for global process capacity.

Canonical Remote scientific submission no longer persists executable Job JSON,
creates an independent Job identity, clones retry payloads or starts work while
opening SSE. Detail/list/SSE/artifact are bounded pure observations, SSE is
Receipt-revision snapshot-first, disconnect has no lifecycle effect, cancel
delegates to the Runtime, and canonical retry fails closed. Historical
terminal Job JSON remains bounded read-only compatibility data; historical
active scientific Jobs become `interrupted/legacy_execution_unrecoverable` at
startup and are never replayed. Arbitrary Remote inputs/parameters, Remote
Project Scope, Workflow/Autonomous/dynamic Runs, distributed Workers and live stdout
remain outside this tracer.

Every Remote compatibility Adapter that requires Workspace state uses the one
absolute Workspace frozen when the Backend binds its sole `RunRuntime`.
Request fields, later environment mutation, legacy Session identity and Job
JSON cannot retarget that Runtime or become path/execution authority. The
binding anchors compatibility-state storage and Runtime composition; it is not
filesystem confinement. The retired Session-resume route intentionally needs
no binding and performs no Workspace lookup, observation, scheduling or
recovery work; reconnecting an observer means opening the known Run's
Receipt/SSE Interface, never resuming execution.

## Context

ADR 0058 left exact process-local Run scheduling policy as a later decision. A
single queue or semaphore cannot safely represent all of the required facts:

- accepted Run intent and FIFO order;
- the bounded number of active Run orchestrators;
- authorization for exactly one executor invocation;
- current CPU, memory, GPU, thread, temporary-disk and process capacity;
- bounded ready-step parallelism inside one Workflow or Autonomous Run.

Conflating them creates either false execution ownership from a resource token,
unbounded process-local orchestration, or duplicated concurrency limits whose
meaning differs by Run kind. Persisting the executable queue would also
contradict the non-replayable Run Receipt and make Backend restart capable of
silently repeating scientific side effects.

The local-first single-Owner product does not currently justify distributed
fair scheduling, preemption or a cross-process Worker lease protocol. It does
need one deterministic admission path that can later sit in front of a
different Run Executor without changing Run identity or control-plane state.

## Decision

### Run Dispatcher owns Run order and Assignment eligibility

One Backend runtime has exactly one process-local **Run Dispatcher** for every
accepted top-level Skill, Workflow, Candidate-plan and Autonomous Run. No
Surface, Analysis Router, Workflow, Autonomous runner or Remote compatibility
endpoint may keep an independent top-level execution queue.

The Dispatcher owns only:

- the bounded process-local executable Run buffer reserved during novel Run
  acceptance;
- strict FIFO order by completed Run admission;
- a bounded `max_active_runs` count for live top-level Run orchestrators;
- removal of a waiting Run when `queued -> canceled` wins;
- coordination of the sole `queued + no assignment -> running + Assignment
  ID` transition.

It does not allocate CPU, memory, GPU, threads or temporary disk. It does not
persist the typed Run Request, infer work from a Run Receipt, reconstruct a
queue after restart, retry a terminal Run, or create another Assignment.
Queue position and current wait reason are observational projections over live
Dispatcher state, not new durable Run lifecycle states.

The process-local buffer remains the only holder of executable top-level Run
payloads. If Backend ownership is lost, ADR 0058 reconciliation moves prior
process-owned `queued` or unsupported non-terminal Receipts to `interrupted`
without replay.

### Execution Resource Scheduler owns compute admission only

One shared process-local **Execution Resource Scheduler** is the sole global
capacity authority for every scientific process launched by a Run Executor.
Its budget covers:

- active scientific process count;
- CPU cores;
- memory;
- GPU device count and assigned physical GPU identifiers;
- threads;
- temporary disk.

Each request carries its owning Run ID and, for nested work, optional Run Step
identity for cancellation, observability and audit correlation. Admission is
atomic across every dimension: a process receives the complete reservation or
none of it.

A **Resource Lease** remains an in-memory accounting capability. It neither
changes Run lifecycle nor authorizes executor invocation, and it cannot fence a
callback, survive restart, prove that a process stopped or grant permission to
reassign a Run. The scheduler provides admission accounting and governed
environment values; it does not claim cgroup, container, operating-system or
remote-cluster quota enforcement. Process-tree ownership is a separate
canonical-executor responsibility: the first Linux tracer uses systemd and
bubblewrap for lifecycle containment, not for resource-quantity enforcement.

### Complete resource semantics are validated before novel acceptance

Duplicate Run Submission lookup still precedes every current lifecycle,
capacity and resource gate. A matching duplicate returns its original Run even
when current policy or capacity has changed.

For a novel submission, the complete static execution resource semantics must
be available and structurally valid before Run acceptance:

- a simple Skill provides one complete Execution Resource Request;
- a Workflow or confirmed Candidate plan provides an immutable per-Step
  resource plan;
- an Autonomous Run provides a fixed governed execution envelope before
  acceptance, and every dynamically produced child Step must fit inside it.

The canonical static request, plan or versioned digest is accepted Run
semantics and enters the Run Request Fingerprint and Run Manifest. Current
availability, wait duration, configured runtime budget, physical GPU IDs,
executor instance and actual scheduling order do not.

Missing resource semantics fail closed with `resource_contract_missing`. A
request that no configured v1 execution budget can ever accommodate fails
before Run creation with `resource_unsupported`. A valid request that fits the
budget but cannot fit current remaining capacity waits in the bounded
scheduler; temporary contention is not scientific failure.

### Resource readiness precedes the Assignment transition

Novel Run processing follows this ownership order:

1. authenticate the caller, normalize Submission ID and Fingerprint, and return
   a matching duplicate before novel-admission gates;
2. validate complete static resource semantics against the configured hard
   budget;
3. reserve bounded Run-buffer capacity and atomically commit the `queued` Run
   Receipt plus Run Submission Binding as specified by ADR 0058;
4. enqueue the typed Run Request in the Dispatcher FIFO;
5. when an active-Run slot is available, request a Resource Lease for the first
   dependency-ready execution unit;
6. after that Lease is acquired, compare-and-set the queued Run to `running`
   with its sole Assignment ID and fixed executor kind;
7. only after the Assignment commits, invoke the executor using that first
   Lease; fixed-plan dependency-ready Run Steps later acquire and release their
   own global Resource Leases, while a governed dynamic Run follows ADR 0062's
   aggregate-envelope rule and never performs nested global acquisition.

This order prevents a Run from appearing `running` while it is indefinitely
waiting for its first compute capacity and guarantees that an assigned Run can
make immediate progress. A Resource Lease obtained before the compare-and-set
is provisional: if queued cancellation or another invalidating transition wins,
the Lease is immediately released and no executor starts.

If Assignment commits but spawn or setup fails, the same Run follows ADR 0058's
guarded failure/cancellation rules; it never receives a replacement Assignment.
Scientific side effects remain forbidden before the Assignment commit.

### Resource Leases follow real process lifetime

A Run Step acquires its Resource Lease immediately before scientific process
startup and releases it only after that process is confirmed stopped, whether
the Step succeeds, fails or is canceled. `cancel_requested` is not proof of
termination and cannot release resources still used by a process.

Waiting cancellation removes the request from the FIFO. Dependency gaps,
approval waits and user-input waits hold no Resource Lease only when no process
remains alive. Under ADR 0062, a paused live governed kernel keeps its aggregate
Lease; a fully stopped governed executor may release and later reacquire the
same immutable envelope. Any process spawn failure releases the provisional
reservation after the failed process is known not to be running.

### Strict FIFO is the explicit v1 fairness policy

The Run Dispatcher uses strict FIFO admission order. The Execution Resource
Scheduler separately uses strict FIFO Resource-Request arrival order. v1 has no
Surface, Project or Run-kind priority, oldest-fit bypass, aging, preemption or
deadline scheduling.

Strict FIFO is deterministic and starvation-free, but can deliberately leave
capacity idle when the queue head cannot currently fit even though a later,
smaller request could. A GPU-bound head may therefore delay CPU-only work. The
single-Owner local-first product accepts this head-of-line blocking for v1;
bounded bypass requires measurements and a later policy decision.

A multi-Step Run may expose only a bounded number of dependency-ready pending
resource requests at once. This per-Run ready-step window prevents one large
Workflow from flooding the global FIFO. It is not another global process-count
authority: the Execution Resource Scheduler's `max_processes` and resource
dimensions remain authoritative. Existing Candidate-plan `max_concurrency`
should be renamed or reinterpreted accordingly rather than competing with the
global scheduler.

### v1 remains one local scheduling domain

The accepted v1 scheduler represents capacity owned by the one Backend runtime
and its directly controlled execution environment. Running the Backend on a
remote machine still creates one local scheduling domain on that machine; it
does not create a distributed Worker fleet.

A future independently deployed Worker or external durable scheduler requires
a separate decision for domain selection, remote capacity evidence, claims,
heartbeats, reattachment and fencing. That future mechanism must remain behind
the Run Executor Seam and preserve the same Run ID, Submission Binding and
single-Assignment rules unless explicitly superseded.

### Observation and compatibility paths never start work

Status, log, artifact and SSE reads are pure observation of existing Run state.
They cannot enqueue a Run, obtain a Resource Lease, create an Assignment or
resume execution.

Canonical Remote exact-demo submission is now behind the Run Dispatcher and
Run Repository. A compatibility Job may project a canonical Run and its
Execution Reference, but Job JSON is not execution authority, Job retry cannot
clone or reset an old Run, and opening an event stream cannot call a start
helper. Broader Remote input/Run shapes remain future cutover work. Historical
Job records without trustworthy Run identity remain legacy aliases rather than
fabricated canonical Runs.

## Consequences

- Every top-level scientific execution gains one bounded, deterministic start
  path without introducing a broker or persistent work queue.
- `max_active_runs` bounds orchestration pressure while the shared scheduler
  independently bounds actual scientific processes and physical resources.
- Existing Candidate-plan multidimensional accounting becomes reusable across
  Run kinds instead of remaining an isolated implementation.
- A queued Run means accepted but not yet assigned; a running Run has one
  Assignment and had capacity to start its first execution unit.
- Large Workflow and Autonomous executions remain bounded without making every
  nested Step another top-level Run.
- Strict FIFO may reduce utilization through head-of-line blocking; that cost
  is visible and intentionally preferred over unmeasured priority complexity.
- Current Skill declarations need complete resource contracts, and broader
  Remote Run shapes still require explicit canonical Adapters; exact-demo Job
  submission/SSE no longer owns an independent start path.
- The design preserves an execution-plane extension seam but makes no claim of
  distributed scheduling, OS-level quota enforcement or automatic recovery.

## Rejected alternatives

- **Use one queue token for Run order, resources and ownership.** Rejected
  because these facts have different durability, cancellation and safety
  semantics.
- **Assign the Run before first compute capacity is available.** Rejected
  because `running` would include indefinite first-start resource waiting and
  Assignment would no longer mean the executor is ready to progress.
- **Let each Run kind own a scheduler or semaphore.** Rejected because ordinary
  Skills, Candidate plans and Autonomous Runs could overcommit the same host
  while reporting locally valid limits.
- **Keep Candidate `max_concurrency` as another global process limit.** Rejected
  because process capacity has one authority; only a bounded per-Run pending
  Step window remains legitimate.
- **Use optimistic resource defaults when a contract is absent.** Rejected
  because missing GPU, memory or disk requirements can make admission unsafe
  and non-auditable.
- **Use current free capacity in the Run Fingerprint.** Rejected because
  transient machine state is not Owner-declared scientific intent.
- **Use oldest-fit, priorities or preemption in v1.** Rejected until measured
  head-of-line blocking justifies the added fairness, starvation and
  cancellation policy.
- **Persist the Dispatcher queue or rebuild it from queued Receipts.** Rejected
  because the durable Receipt intentionally contains no executable payload and
  scientific work is not automatically replayable.
- **Let SSE or status observation start a queued Job.** Rejected because
  observation cannot manufacture execution authority.
- **Treat the current Remote Job router as the future Worker protocol.**
  Rejected because its Job identity, persisted request, retry and start
  semantics do not provide Run Assignment fencing or safe remote ownership.
