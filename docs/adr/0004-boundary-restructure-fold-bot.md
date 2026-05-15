# Boundary-led restructure: fold `bot/` into `omicsclaw/`, split `runtime/` and `core/` by responsibility

## Status

Accepted (2026-05-15).

## Context

OmicsClaw's top-level layout had drifted into three places that no longer
matched what each was responsible for:

1. **`omicsclaw/runtime/`** had grown to ~30 flat files where the
   "Python package" axis no longer corresponded to a behavioral
   boundary. New code landed there because the directory name was
   generic enough to absorb anything: tool registry, context assembly,
   policy evaluation, persistent stores, the agent loop, output
   styling, and ~2,400 lines of two giant per-surface tool-spec
   builders (``bot_tools.py`` 1,011 lines, ``engineering_tools.py``
   1,375 lines).

2. **`omicsclaw/core/`** had grown to ~20 files mixing two unrelated
   concerns: skill registry / capability resolver / skill runner /
   subprocess execution helpers vs. LLM provider registry / OpenAI
   client adapters / ccproxy management. ``ls omicsclaw/core/`` did
   not communicate what "core" meant — it was a junk drawer.

3. **`bot/`** existed as a sibling of ``omicsclaw/`` and held seven
   different concerns: channel adapters (Telegram / Feishu / etc.),
   cross-cutting infrastructure (audit log, token billing, rate
   limit, path sandbox), the multi-round LLM dispatch loop, slash
   commands, the interactive setup wizard, skill orchestration, and
   the per-tool executor dispatch. Files inside ``bot/`` had been
   carved out of ``bot/core.py`` over multiple slices (see ADR 0001)
   but still late-imported globals from ``bot.core`` via 7
   ``from bot.core import OMICSCLAW_DIR, transcript_store, …`` sites.
   The package boundary did nothing to enforce which file owns what.

The first explicit cue for this restructure came from comparing
``omicsclaw/`` against a reference repository (CellClaw) whose
top-level layout cleanly separated `agent/`, `gateway/`, `providers/`,
`storage/`, `services/`, `models/`, and `web/`. The point was *not*
the name parity but the **boundary parity**: in the reference layout,
opening `gateway/telegram.py` made it obvious the file would never do
LLM dispatch; opening `providers/litellm.py` made it obvious that file
would never know about skills. OmicsClaw lacked that property because
the package names admitted anything.

## Decision

Adopt a boundary-led split: keep ``omicsclaw/`` as the only top-level
package, but inside it carve out the boundaries that exist in practice
but had no enforced home. Fold ``bot/`` away in the same change.

The agreed splits, in execution order:

**P1 — providers/** (B). Extract the 6 LLM-provider files from
``omicsclaw/core/`` into a new ``omicsclaw/providers/`` package and
drop the now-redundant ``provider_``/``llm_`` filename prefixes:

    providers/{registry, runtime, models, patches, timeout, ccproxy}.py

**P2 — skill/** (C). Consolidate the 9 ``core/skill_*`` files, the
``core/runtime/`` subdirectory (skill subprocess infrastructure), and
the 3 ``runtime/skill_*`` helpers plus ``runtime/preflight/`` into a
single ``omicsclaw/skill/`` package. ``core/runtime/`` is renamed to
``skill/execution/`` to disambiguate from ``omicsclaw/runtime/``.

**P3 — runtime/** (A). Split the flat 30-file runtime/ into 5 named
sub-packages:

    runtime/agent/      — multi-round LLM dispatch loop
    runtime/context/    — prompt-context assembly, compaction, budgets, layers
    runtime/tools/      — tool spec/registry/execution/hooks + concrete builders
    runtime/policy/     — tool approval, condition matching, workspace verification
    runtime/storage/    — transcript / tool-result / task stores

Three merges that consolidated fragmented siblings:
- ``token_budget.py`` absorbed into ``context/budget.py``.
- ``events.py`` (~30 lines of constants) and ``hook_payloads.py``
  (~16 lines of helpers) absorbed into ``tools/hooks.py``.
- ``runtime/predicates.py`` (renamed to ``policy/conditions.py``) to
  avoid a name collision with ``runtime/tools/predicates.py``
  (different concept: the former matches request context to predicate
  functions; the latter gates which tools are exposed to the LLM).

``runtime/__init__.py`` was rewritten to re-export the same public
surface from the new sub-package paths, so external callers using
``from omicsclaw.runtime import X`` keep working unchanged.

**P4 — fold bot/** (D). Move every file in ``bot/`` to its real home
inside ``omicsclaw/``:

    bot/channels/*.py         -> omicsclaw/channels/
    bot/commands/             -> omicsclaw/channels/commands/
    bot/audit.py              -> omicsclaw/services/audit.py
    bot/billing.py            -> omicsclaw/services/billing.py
    bot/rate_limit.py         -> omicsclaw/services/rate_limit.py
    bot/path_validation.py    -> omicsclaw/services/path_validation.py
    bot/onboard.py            -> omicsclaw/setup_wizard.py
    bot/run.py                -> omicsclaw/run_channels.py
    bot/skill_orchestration.py-> omicsclaw/skill/orchestration.py
    bot/preflight.py          -> omicsclaw/runtime/agent/parameter_loop.py
    bot/tool_executors.py     -> omicsclaw/runtime/tools/builders/agent_executors.py
    bot/agent_loop.py         -> omicsclaw/runtime/agent/loop.py
    bot/core.py               -> omicsclaw/runtime/agent/state.py
    bot/session.py            -> omicsclaw/runtime/agent/session.py

``bot/`` is removed from the repository.

## Consequences

**Wins**

- ``ls omicsclaw/`` now reads as a responsibility list rather than an
  infrastructure soup. A new contributor can answer "where does X
  go?" by reading the directory names alone.
- Each boundary has a sharp test:
  - ``channels/<platform>.py`` does not import the LLM client.
  - ``providers/`` does not know about skills.
  - ``skill/`` does not import ``channels/`` or ``runtime/agent/``.
  - ``runtime/storage/`` does not depend on ``runtime/agent/``.
- ``runtime/`` is no longer a junk drawer. New code lands in one of
  the 5 sub-packages or doesn't land in ``runtime/`` at all.
- ``core/`` shrinks from 20 mixed-concern files to 5 actual
  base-layer files (``dependency_manager``, ``external_env``,
  ``r_*``). It is now a thin foundation, not a kitchen sink.
- Two callable concepts that had been smeared across multiple files
  — "the agent's tool set" (``bot_tools.py``) and "engineering /
  infrastructure tools" (``engineering_tools.py``) — now sit
  side-by-side in ``runtime/tools/builders/`` where the parallel is
  visible.

**Costs**

- ~150 import sites updated across production + tests. The mechanical
  bulk was sed-able; ~10 sites with ``import X as Y`` aliases or
  ``import X.Y as Z`` parent-attribute resolution needed hand-fixes.
- ~20 docstring / log-name / string-mock paths needed manual cleanup
  (sed could have done most; the rest were inside multi-line strings
  or fnmatched test fixtures).
- ``test_interactive_loop.py``'s sys.modules mocks broke against the
  new path because Python's ``import X.Y as Z`` prefers the parent
  package's ``Y`` attribute over ``sys.modules[X.Y]`` after the first
  import. Eight test setups needed an additional
  ``monkeypatch.setattr(omicsclaw.runtime.agent, "state", fake,
  raising=False)`` to keep mocks intact across calls. This was not a
  production-code issue.
- ``omicsclaw/runtime/agent/state.py`` (formerly ``bot/core.py``)
  retains the ADR-0001 late-import pattern unchanged. The further
  4-way split of its globals into ``omicsclaw/core/paths.py``,
  ``omicsclaw/core/config.py``, and ``omicsclaw/runtime/storage/`` is
  *deferred to a follow-up* — splitting state ownership across four
  modules in the same commit multiplied the risk of breaking the
  late-import contract that 7 other modules rely on. The follow-up
  will retire the late-import pattern entirely.
- ``graphify-out/`` anchors target file paths that moved. A
  ``/graphify`` re-index is required before knowledge-graph queries
  return correct citations.

**Alternatives considered**

- **Mirror CellClaw's top-level layout 1:1** (``agent/``, ``gateway/``,
  ``providers/``, etc. at the repo root). Rejected: ``omicsclaw/`` is
  the public Python package; renaming it would break every external
  ``import omicsclaw…`` site without buying anything beyond
  superficial name parity.
- **Move ``bot/`` to ``omicsclaw/bot/`` unchanged** (the "X" form
  considered during planning). Rejected: this would have moved the
  problem inward without solving it — ``omicsclaw/bot/core.py``
  would still hold seven different concerns under a generic name and
  would still need to be split later. Per ADR 0003's logic ("no
  half-finished migrations"), it is better to do the boundary cut
  once and atomically.
- **Keep ``runtime/`` as an umbrella with 6 sub-packages inside, do
  not promote them to top-level** (the "Y1" form). Rejected because
  the umbrella name was itself the source of today's failure mode —
  new code keeps landing in ``runtime/`` because the directory
  admits anything. Promoting the sub-packages to ``omicsclaw/{tools,
  context, …}`` top-level was the better fix; we partially did this
  for ``skill/`` and ``providers/`` and kept ``runtime/`` only for
  the truly agent-loop-internal pieces (``agent``, ``context``,
  ``tools``, ``policy``, ``storage``) which still benefit from being
  grouped.
- **Split ``bot/core.py`` into 4 stable-owner modules in this same
  commit** (the original plan). Reduced scope to a 1-to-1 file move
  during execution because the late-import contract spans 7 files;
  splitting the owner across 4 modules atomically multiplied the risk
  of breaking it. The deferred follow-up will do that split once the
  current layout has settled.

## Verification

The full test suite was run on this branch and on `main` after each
of the four phase commits. The failure set on each branch is
compared by `comm -23`:

- P1: 270 passed, 4 pre-existing failed (identical to main).
- P2: 437 passed, 2 pre-existing failed (identical to main).
- P3: 2,560 passed, 25 pre-existing failed (identical to main, after
  fixing PROJECT_ROOT depth and one lazy ``from . import events``).
- P4: zero P4-introduced failures (full diff vs main empty after test
  mock hardening for the ``import X.Y as Z`` quirk).

Each phase landed as one commit with a descriptive message; commits
were never amended.
