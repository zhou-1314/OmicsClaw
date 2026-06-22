# Bench v2: heartbeat + episodic memory

**Status:** accepted (2026-05-30). *Renumbered from 0024 → 0034 on 2026-06-22 to
resolve a number collision with [`0024-prompt-prefix-caching.md`](0024-prompt-prefix-caching.md),
now the sole ADR 0024.*

The v2 proactive layer is deliberately minimal and read-only — ADR 0017 chose research-continuity over a
companion. Eight decisions. Ground truth: the codebase has **no scheduler anywhere** (backend or
Electron), **no temporal-decay/recency ranking** in recall, and `insight://` / `project://` are
**defined-but-unused** domains (Bench is their first writer).

## Heartbeat (F1–F4)

**1. Trigger = on-open check, no scheduler.** On opening a thread, compare a durable per-thread
"last-heartbeat-date" against today's local date; if different, run the heartbeat once. Zero infra. It
is a "welcome-back briefing when you sit down," not a push notification — matching continuity-not-
companion. (Rejected: a true background scheduler + OS notifications = net-new infra at odds with the
non-intrusive posture.)

**2. Read-only is mechanical, not prompted.** The tick runs the agent loop with a read-only tool subset
(recall/search/read-provenance/read-hypotheses/propose-QuickAction) — no skill execution, no memory
writes — with exactly ONE permitted write: the last-heartbeat timestamp. It reuses the stage→tool-subset
mechanism (ADR 0020) as a `heartbeat` pseudo-stage. A skill the tick "wants" to run is simply absent from
its toolset, so "propose, never execute" is a structural guarantee. (Rejected: prompt-only "please don't
run anything".)

**3. Scope = per-thread, fired on thread-open, with opportunistic cross-thread hints.** Each thread has
its own last-heartbeat stamp; the briefing is about the active thread. A lean, collapsible cross-thread
hint surfaces only when another thread has genuinely actionable pending work (the `cross_thread:true`
recall, ADR 0018) — not repeated in every thread's briefing. (Rejected: a global digest fights strict
`get_recent` and the "one thread at a time" model.)

**4. Silent path + structured notability.** The tick computes a diff from five concrete, on-disk sources
— the provenance index (new runs, ADR 0022 decision 0), open/untested Hypotheses, stale manuscript
sections (ADR 0022 decision 4), unfinished runs, and recent episodic entries — and returns a STRUCTURED
`{notable, proposals[]}`. `notable:false` → stamp the timestamp, show NO chat bubble; only `notable:true`
surfaces a briefing with QuickAction proposals. A structured flag, not a prose `HEARTBEAT_OK` sentinel
(detection-fragile). (Rejected: always-visible briefings train dismissal.)

## Episodic memory (F5–F8)

**5. Lives in the graph under the thread.** Episodic daily memory is `project://<thread_id>/daily/<date>`
(graph-resident, thread-scoped recall works, one store). Entries carry an `episodic`/`volatile` mark so
they decay (decision 7) and are down-weighted/excluded from normal FTS search (no pollution). Daily
entries are **overwrite** (accumulated within a day), so the versioning policy is refined:
`project://<thread>` (node metadata) is versioned, `project://<thread>/daily/*` is overwrite. (Rejected:
a new `journal://` domain = not "under the thread"; ScopedMemory filesystem = cross-process sync with the
desktop server.)

**6. Written per-event (mechanical spine) + read-time narrative.** Three layers: the **spine** is
lightweight mechanical event stamps (run completed, hypothesis formed, section drafted) written as each
real event happens — crash-safe, piggybacking the existing write hooks (`_auto_capture_analysis` etc.),
needing no scheduler or check-in; the **reasoning / decisions / dead-ends** (the value-add over the
structured stores, which record *what* ran, not *why*) are captured via an explicit "remember this"; the
**narrative** is generated at read time, not persisted per-event. (Rejected: end-of-day summary = needs a
scheduler; per-event LLM prose = cost + the read-only heartbeat can't write it.)

**7. Decay = read-time down-weighting, episodic-only, no GC.** A 30-day-half-life age multiplier
(tunable) is applied at recall/search time to `episodic`-marked entries only; `insight://`, `analysis://`,
Hypotheses, etc. are never decayed. No deletion/GC (no scheduler). Promotion (decision 8) is the escape
hatch — important findings are lifted into non-decaying `insight://`, so an aggressive journal decay is
fine. (Rejected: prune/GC = needs a scheduler.)

**8. Promotion to `insight://` is suggested, written on human confirm.** A durable finding is distilled at
read time (a heartbeat QuickAction "promote this to a finding?" or an explicit user mark), but the WRITE
to `insight://` requires explicit human confirmation — consistent with ADR 0017 (heartbeat read-only), the
Ideate verdict-confirm rule (ADR 0021 decision 6), and the safety posture. Promotion creates an **edge**
(the insight links back to the daily entry / run it was distilled from) + a `promoted_date`; it is never
automatic. The edge makes a wrong insight retractable by its source (run/day). (Rejected: auto-distill =
the read-only heartbeat mutating memory.)

## Consequences

- Net-new but small: a per-thread `last-heartbeat-date` stamp + on-open check (no scheduler); a
  `heartbeat` pseudo-stage tool subset; an `episodic`/`volatile` mark + a read-time decay multiplier in
  recall/search; a per-event spine write piggybacking existing hooks; a human-confirmed promotion edge.
- Bench is the **first writer** of `insight://` and `project://<thread>/daily/*` — clean slate, no
  precedent to copy.
- The decay multiplier is the **first temporal-ranking signal** in the memory engine; scope it to
  `episodic`-marked rows so it does not change global search behavior.
