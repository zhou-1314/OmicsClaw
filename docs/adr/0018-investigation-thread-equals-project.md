# An investigation thread equals one research project

**Status:** accepted (2026-05-30)

A Bench **investigation thread** is scoped to exactly one research project (课题),
backed by a `project://<id>` memory subtree. Two distinct topics (e.g. a glioma study
vs. a liver-cancer study) are two threads. A **manuscript** is a *child write-target*
of a thread, not a thread itself: a single project that later splits into two papers
for different journals remains **one thread with two manuscript children**, and the
Write stage targets a named manuscript within the thread.

We considered thread = manuscript and thread = hypothesis. Both fragment the shared
reading / hypotheses / analyses across a project, or force the user to re-explain
context, defeating the continuity goal of ADR 0017. Project granularity is the coarsest
scope that still lets the Write stage assemble one paper.

## Consequences

- A thread rolls up many KG **Source** pages, **Hypothesis** pages, `analysis://` runs,
  and `insight://` notes under one `project://<id>`.
- This `project://<id>` binding is the durable object the whole feature hangs on, and it
  is **net-new plumbing**: today the desktop frontend sends only `system_prompt_append`
  and the backend binds memory to a single startup-derived `desktop_namespace()`
  (`omicsclaw/surfaces/desktop/server.py`), with no per-thread scope. Threading a
  `project://` scope frontend→backend is the first engineering task of v1.

## Thread binding & cross-thread recall

A thread is **not** a memory namespace. The desktop namespace (`app/<user_id>` via
`desktop_namespace()`) is deliberately stable across launches — it ignores the per-launch
random `OMICSCLAW_DESKTOP_LAUNCH_ID` (an earlier version used it as the namespace token and
orphaned every previous launch's writes) — so it remains the single per-user partition. A
thread is a **soft grouping inside** that partition: a `project://<thread_id>` plus
thread-scoped lineage written under a `<thread_id>` path segment (reusing the
`analysis://typed/*`-style prefix convention already in the codebase).

Default recall is thread-scoped, but **cross-thread recall is deliberately allowed**: the
agent may surface a method or result from another thread ("you used CARD with params X in
your glioma study") because cross-project method transfer is high-value. Hard per-thread
isolation (a namespace per thread) was rejected — it would fragment the stable per-user
partition, break `core://my_user` profile alignment, and bend the namespace/domain
distinction the memory glossary guards.
